// examples/plugins/jira-source/connector/index.mjs
// Jira task source (worca-cc plugin API v1), backed by the jtr CLI — jtr owns
// auth (SSO cookies / PAT behind a WebSEAL gateway, or Cloud email + API
// token) and the REST dialect; this connector only shells out with --json.
// Injected exec, zero dependencies. Task ids round-trip as plain Jira keys
// ("PROJ-123"). Write-back is opt-in per RUN (source panel select, default
// off): when a run chose it, the result is posted as a ticket comment,
// optionally followed by a configured workflow transition on success.
//
// Both Jira deployments are supported, and which one we are talking to is
// never guessed here: `jtr init --json` reports it and everything that differs
// — the credentials, the paging model — follows from that one value.
//
// Setup is entirely UI-driven: validateConfig is a STATE MACHINE that the
// settings pane's Connect button polls (see the comment on validateConfig).

import {
  execJtr, runJtr, ensureHome, jtrEnv, ssoLogPath, startSsoLogin, readSsoLog, clearSsoLog,
} from './jtr-cli.mjs';

// jtr 0.10.0 is the floor: `--deployment`, `auth token` and cursor paging are
// all new there, and the deployment this connector branches on only appears in
// `init --json` from that release. (0.9.0 brought `init --json`, `--bare` and
// `$JTR_CONFIG_DIR`.) Checked once during Connect so the error is a sentence
// rather than an "unknown option" from argparse.
const MIN_JTR = [0, 10, 0];
const PAGE = 50;
const SERVER = 'server';
const CLOUD = 'cloud';
// Which credentials each deployment can actually take. Atlassian doesn't
// accept browser cookies or PATs as Cloud REST credentials, and a Cloud API
// token means nothing to a Server/DC instance — so an impossible pairing is
// refused here, in the same call that discovered the deployment, instead of
// being deferred to a 401 (or, worse, to a browser login jtr will refuse).
// First entry is the default when Authentication is "auto".
const METHODS = { [SERVER]: ['sso', 'pat'], [CLOUD]: ['token'] };
// An empty filter box means NO filter — not a hidden default. Nothing is
// listed until the user gives something to go on (a filter, search text, or a
// pasted ticket key), because inventing a query for them is the same
// assumption the removed default was making, just harder to see.
// jtr's search JSON carries the status NAME only (no statusCategory).
const CLOSED_STATUS = /^(done|closed|resolved)$/i;
const KEY_RE = /^[A-Za-z][A-Za-z0-9_]*-\d+$/;
// Regex split mis-fires on "order by" inside a quoted JQL string — accepted edge.
const ORDER_BY_RE = /\border\s+by\b/i;
// Without an explicit sort Jira picks its own, which happily puts six-year-old
// tickets at the top of a text search. Newest-touched-first matches what the
// rows actually show ("updated 3d ago"). Only applied when the user's JQL
// doesn't already say how to sort.
const DEFAULT_ORDER = 'ORDER BY updated DESC';

export function withDefaultOrder(jql) {
  const q = String(jql || '').trim();
  return !q || ORDER_BY_RE.test(q) ? q : `${q} ${DEFAULT_ORDER}`;
}

/** ORDER BY is only legal at the very end, so every rewrite peels it off first. */
function splitOrderBy(jql) {
  const q = String(jql || '').trim();
  const m = ORDER_BY_RE.exec(q);
  return m
    ? { core: q.slice(0, m.index).trim(), order: q.slice(m.index).trim() }
    : { core: q, order: '' };
}

/**
 * Constrain a query to the configured project.
 *
 * `mentionsProject` must be computed from the JQL the USER typed, never from
 * the whole query: jtr's own scoping tests the entire string for "project",
 * so searching for the words "project plan" builds `text ~ "project plan"` and
 * silently switches scoping off. Checking only the typed JQL avoids that.
 */
export function withProject(jql, project, mentionsProject = false) {
  const { core, order } = splitOrderBy(jql);
  if (!core || !project || mentionsProject) return jql;
  return `project = ${project} AND (${core})${order ? ` ${order}` : ''}`;
}

/** Does the user's own JQL constrain the project itself? Word-boundary, so
 *  "projectType = X" is not mistaken for a project constraint. */
export function mentionsProject(jql) {
  return /\bproject\b/i.test(String(jql || ''));
}
const BROWSE_URL_RE = /\/browse\/([A-Za-z][A-Za-z0-9_]*-\d+)/;
const SSO_WINDOW_MS = 5 * 60 * 1000; // jtr's own SSO timeout — how long a login may be in flight

/** "jtr 0.9.0" -> [0,9,0]; null when unparseable. */
export function parseVersion(text) {
  const m = /(\d+)\.(\d+)\.(\d+)/.exec(String(text || ''));
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

/** jtr's own value, narrowed to the two we branch on. Anything unknown (or a
 *  profile initialised before this connector stored it) is Server/DC — what a
 *  self-hosted URL almost always is, and what this plugin only ever spoke. */
export function normalizeDeployment(value) {
  return String(value || '').trim().toLowerCase() === CLOUD ? CLOUD : SERVER;
}

/** The auth method to use: the configured one, or the deployment's default. */
export function resolveMethod(configured, deployment) {
  const m = String(configured || 'auto').trim().toLowerCase();
  if (m && m !== 'auto') return m;
  return METHODS[normalizeDeployment(deployment)][0];
}

/** Why this method cannot work on this deployment, or null if it can. */
export function methodError(method, deployment) {
  if (METHODS[normalizeDeployment(deployment)].includes(method)) return null;
  return normalizeDeployment(deployment) === CLOUD
    ? `Jira Cloud cannot authenticate with "${method}" — Atlassian accepts only an account email plus an API token. Set Authentication to token (or auto).`
    : `Jira Server/Data Center cannot authenticate with "${method}" — it takes a personal access token or a browser SSO login. Set Authentication to sso or pat (or auto).`;
}

/**
 * The cursor for the NEXT page, or null on the last one.
 *
 * The two deployments page differently and neither field appears on the other:
 * Server/DC counts offsets, Cloud hands back an opaque token and reports no
 * total at all. Reading the wrong field yields null, which looks exactly like
 * "last page" — so this picks by deployment rather than by whichever field
 * happens to be present.
 */
export function nextCursor(res, deployment) {
  if (normalizeDeployment(deployment) === CLOUD) {
    const token = res?.next_page_token;
    return typeof token === 'string' && token ? token : null;
  }
  return typeof res?.next_start_at === 'number' ? String(res.next_start_at) : null;
}

export function versionAtLeast(got, min) {
  if (!got) return false;
  for (let i = 0; i < min.length; i += 1) {
    if (got[i] > min[i]) return true;
    if (got[i] < min[i]) return false;
  }
  return true;
}

/**
 * Quick-pick filters. The manifest carries only opaque values, so the query
 * syntax lives here rather than in plugin metadata. No ORDER BY — that is
 * withDefaultOrder's job, and stating it here would suppress it.
 */
const PRESETS = {
  none: '',
  mine: 'assignee = currentUser()',
  'reported-by-me': 'reporter = currentUser()',
  'updated-week': 'updated >= -7d',
};

/**
 * The filter to run, before any free-text search is folded in.
 * A typed JQL always wins: an input the user filled in must never be ignored,
 * which is also why there is no "Custom" entry in the preset list — the box
 * overriding the dropdown IS custom mode.
 */
export function baseJql(inputs = {}) {
  const typed = String(inputs.jql || '').trim();
  if (typed) return typed;
  return PRESETS[String(inputs.filter || 'none')] || '';
}

/** A pasted ticket key or browse URL -> the bare key. Otherwise null. */
export function ticketKeyFrom(text) {
  const s = String(text || '').trim();
  if (!s) return null;
  if (KEY_RE.test(s)) return s.toUpperCase();
  const m = BROWSE_URL_RE.exec(s);
  return m ? m[1].toUpperCase() : null;
}

/**
 * Combine the filter box with the browser's free-text search.
 * Either may be empty: with no filter the search stands alone, with no search
 * the filter is sent verbatim. Both empty is the caller's job to short-circuit.
 */
export function jqlWithSearch(jql, search) {
  const base = String(jql || '').trim();
  const needle = String(search || '').trim();
  if (!needle) return base;
  const esc = needle.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const clause = `text ~ "${esc}"`;
  if (!base) return clause;
  const { core, order } = splitOrderBy(base);
  return `(${core}) AND ${clause}${order ? ` ${order}` : ''}`;
}

/** configSchema `select` fields deliver strings; coerce yes/true -> boolean. */
export function toBool(v) {
  return v === true || v === 'yes' || v === 'true';
}

/** Minimal markdown -> Jira wiki, the inverse of wikiToMarkdown for the ONE
 *  document this connector ever writes: the host's result summary
 *  (sources.mjs#buildResultSummary — headings, bold, bullets, inline code,
 *  fences, [text](url) links). Deliberately partial, same stance as the
 *  read direction: leftover markdown reads fine in a Jira comment. */
export function markdownToWiki(src) {
  // Line-by-line so fenced code passes through VERBATIM: a `# comment` or
  // `- flag` inside a fence must reach Jira as code, not become h1./bullets.
  const out = [];
  let inFence = false;
  for (const line of String(src || '').replace(/\r\n/g, '\n').split('\n')) {
    const fence = /^```([\w+-]+)?\s*$/.exec(line);
    if (fence) {
      out.push(!inFence && fence[1] ? `{code:${fence[1]}}` : '{code}');
      inFence = !inFence;
      continue;
    }
    if (inFence) {
      out.push(line);
      continue;
    }
    out.push(line
      .replace(/^(#{1,6})\s+/, (_, h) => `h${h.length}. `)
      .replace(/^- /, '* ')
      .replace(/\*\*([^*\n]+)\*\*/g, '*$1*')
      .replace(/`([^`\n]+)`/g, '{{$1}}')
      .replace(/\[([^\]|]+)\]\((https?:[^)\s]+)\)/g, '[$1|$2]'));
  }
  return out.join('\n');
}

/** Minimal Jira wiki -> markdown: headings, {code}/{noformat} fences, [text|url] links.
 *  Deliberately partial — the body is LLM prompt text, leftover wiki syntax reads fine. */
export function wikiToMarkdown(src) {
  return String(src || '')
    .replace(/\r\n/g, '\n')
    .replace(/^h([1-6])\.\s+/gm, (_, n) => '#'.repeat(Number(n)) + ' ')
    .replace(/\{code(?::([^}]*))?\}/g, (_, spec) => {
      const lang = String(spec || '').split(/[|,=]/)[0];
      return '```' + (/^[\w+-]+$/.test(lang) ? lang : '');
    })
    .replace(/\{noformat\}/g, '```')
    .replace(/\[([^\]|]+)\|(https?:[^\]]+)\]/g, '[$1]($2)');
}

/** Jira dates look like 2026-06-12T14:23:01.000+0200 — normalize to ISO. */
function isoDate(s) {
  const d = new Date(s || '');
  return Number.isNaN(d.getTime()) ? (s || null) : d.toISOString();
}

function userName(u) {
  return u?.display_name || u?.name || null;
}

function toSummary(t, baseUrl) {
  return {
    id: t.key,
    title: t.summary,
    url: baseUrl ? `${baseUrl}/browse/${t.key}` : null,
    state: CLOSED_STATUS.test(t.status || '') ? 'closed' : 'open',
    labels: t.labels || [],
    updatedAt: isoDate(t.updated),
  };
}

const DEFAULT_DEPS = {
  exec: execJtr,
  ensureHome,
  logPath: ssoLogPath,
  startSso: startSsoLogin,
  readLog: readSsoLog,
  clearLog: clearSsoLog,
  now: () => Date.now(),
};

export default function createTaskSource(ctx, deps = DEFAULT_DEPS) {
  const d = { ...DEFAULT_DEPS, ...deps };
  const cfg = ctx.config || {};
  const jtrPath = String(cfg.jtrPath || 'jtr');
  const authMethod = String(cfg.authMethod || 'auto');
  const browser = String(cfg.browser || 'auto');
  const ticketUrl = String(cfg.ticketUrl || '').trim();
  // Write-back is a per-RUN choice: the source panel's "Write result back"
  // select (default "no") travels with the run's inputs, pinned on the row at
  // fetch time. Only the transition NAME is profile config — it's an instance
  // policy (workflow-specific), not a per-run decision.
  const transitionOnComplete = String(cfg.transitionOnComplete || '').trim();
  // jtr detects the deployment from the hostname, which is a guess: a Cloud
  // tenant can sit on a vanity domain and a DC instance on a Cloud-shaped one.
  // "auto" leaves the guess alone; anything else pins it.
  const deploymentCfg = String(cfg.deployment || 'auto').trim().toLowerCase();
  // Which configuration this op runs against. ctx.config/ctx.state are already
  // scoped to it by the host; the id matters here because jtr keeps its OWN
  // storage, which has to be keyed the same way or two profiles share a session.
  const profile = ctx.profile || 'default';
  // $JTR_CONFIG_DIR pins .env, cookies and audit log inside this profile's
  // directory, so nothing depends on the server's working directory.
  const env = jtrEnv(d.ensureHome(profile));
  const jtr = (args) => runJtr(d.exec, jtrPath, args, { env });
  // `--version` is the one call that isn't JSON.
  const jtrText = (args) => runJtr(d.exec, jtrPath, args, { env, expectJson: false });

  /** Which Jira jtr is pointed at, cached in state (config show is local + auth-free).
   *  `deployment` joined the cache when Cloud support landed, so a profile
   *  connected before that has a baseUrl and no deployment — normalize treats
   *  that as Server/DC, which is what those profiles are. */
  async function instanceInfo() {
    const cached = await ctx.state.get('baseUrl');
    if (cached) {
      return {
        baseUrl: cached,
        project: await ctx.state.get('project'),
        deployment: normalizeDeployment(await ctx.state.get('deployment')),
      };
    }
    try {
      const cfgShow = await jtr(['config', 'show', '--json']);
      if (cfgShow?.base_url) {
        const info = {
          baseUrl: cfgShow.base_url,
          project: cfgShow.project || null,
          deployment: normalizeDeployment(cfgShow.deployment),
        };
        await ctx.state.set('baseUrl', info.baseUrl);
        await ctx.state.set('project', info.project);
        await ctx.state.set('deployment', info.deployment);
        return info;
      }
    } catch { /* cosmetic; never fail the op for it */ }
    return { baseUrl: null, project: null, deployment: SERVER };
  }

  /** Browse-URL prefix only. */
  async function baseUrl() {
    return (await instanceInfo()).baseUrl;
  }

  /**
   * `jtr init` unless this exact ticket URL was already initialised here, and
   * with the same deployment override — flipping that from auto to cloud has
   * to reach jtr's config, not just this connector's branching.
   *
   * @returns the deployment jtr resolved, which the caller branches on.
   */
  async function ensureInitialised() {
    const done = await ctx.state.get('initUrl');
    // `??` and not `||`: absent means "initialised before this setting existed",
    // which must not force a re-init on upgrade.
    const doneDeployment = (await ctx.state.get('initDeployment')) ?? deploymentCfg;
    if (done === ticketUrl && doneDeployment === deploymentCfg) {
      return normalizeDeployment(await ctx.state.get('deployment'));
    }
    // --bare: config only, no .gitignore edit and no bundled Claude skill —
    // this directory belongs to the plugin, not to a user's project.
    // --json: a single parseable object, and it never prompts.
    const args = ['init', '--ticket', ticketUrl, '--force', '--bare', '--json'];
    if (deploymentCfg !== 'auto') args.push('--deployment', deploymentCfg);
    if (browser !== 'auto') args.push('--browser', browser);
    // Config only, never credentials: which credentials are even legal is a
    // function of the deployment this very call is about to report back, and
    // passing an impossible one (--auth pat at a Cloud URL) makes jtr exit on
    // a usage error — argparse text on stderr, no JSON to turn into a message.
    args.push('--no-auth');
    const state = await jtr(args);
    const deployment = normalizeDeployment(state?.deployment);
    await ctx.state.set('initUrl', ticketUrl);
    await ctx.state.set('initDeployment', deploymentCfg);
    // init reports the config it just wrote, so the instance is known without
    // a follow-up `config show`.
    await ctx.state.set('baseUrl', state?.base_url || null);
    await ctx.state.set('project', state?.project || null);
    await ctx.state.set('deployment', deployment);
    return deployment;
  }

  /** Which settings field an error belongs on, so the pane can point at it. */
  function fieldFor(e) {
    if (e?.code === 'jtr_missing') return 'jtrPath';
    if (e?.code === 'unsupported_deployment') return 'deployment';
    return 'ticketUrl';
  }

  /** Missing credentials, as field errors — asked for before jtr 401s over them. */
  function credentialErrors(method) {
    const token = String(cfg.pat || '').trim();
    const email = String(cfg.email || '').trim();
    const errors = [];
    if (method === 'token' && !email) {
      errors.push({ field: 'email', message: 'Jira Cloud identifies you by the Atlassian account email that owns the API token.' });
    }
    if (!token) {
      errors.push({
        field: 'pat',
        message: method === 'token'
          ? 'Paste a Jira Cloud API token — create one at https://id.atlassian.com/manage-profile/security/api-tokens.'
          : 'Paste a personal access token, or set Authentication to sso to sign in through the browser instead.',
      });
    }
    return errors.length ? errors : null;
  }

  /** The one command that stores and verifies this deployment's credentials. */
  function authArgs(method) {
    return method === 'token'
      ? ['auth', 'token', '--email', String(cfg.email).trim(), '--token', String(cfg.pat), '--json']
      : ['auth', 'pat', '--pat', String(cfg.pat), '--json'];
  }

  /** One-time guard so an old jtr fails with a sentence, not an argparse error. */
  async function ensureVersion() {
    if (await ctx.state.get('versionOk')) return null;
    const { text } = await jtrText(['--version']);
    const got = parseVersion(text);
    if (!versionAtLeast(got, MIN_JTR)) {
      return `jtr ${got ? got.join('.') : '(unknown version)'} is too old — this plugin needs ${MIN_JTR.join('.')} or newer. Upgrade with: uv tool upgrade jtr`;
    }
    await ctx.state.set('versionOk', true);
    return null;
  }

  return {
    /**
     * Connect. Each call advances setup by one step and reports where it is,
     * so the settings pane can simply poll it:
     *   1. no ticket URL           -> ok:false, field error
     *   2. not initialised         -> `jtr init` (config only), which also
     *                                 answers which deployment this is
     *   3. method impossible here  -> ok:false, field error (immediately: a
     *                                 Cloud SSO login is refused by jtr, and
     *                                 launching it would hide that behind a
     *                                 five-minute "waiting" state)
     *   4. already authenticated   -> ok:true
     *   5. pat / token             -> store + verify them, then ok:true, or a
     *                                 field error on the credential
     *   6. SSO, no login running   -> launch it detached, ok:false + pending
     *   7. SSO, login in flight    -> ok:false + pending (poll again)
     *   8. SSO, login gave up      -> ok:false with the login's own log tail
     */
    async validateConfig() {
      if (!ticketUrl) {
        return { ok: false, errors: [{ field: 'ticketUrl', message: 'Paste any ticket URL from your Jira instance (used to derive the server address and project).' }] };
      }
      if (!ticketKeyFrom(ticketUrl)) {
        return { ok: false, errors: [{ field: 'ticketUrl', message: `"${ticketUrl}" does not look like a ticket URL (expected .../browse/PROJ-123).` }] };
      }

      let deployment;
      try {
        const tooOld = await ensureVersion();
        if (tooOld) return { ok: false, errors: [{ field: 'jtrPath', message: tooOld }] };
        deployment = await ensureInitialised();
      } catch (e) {
        return { ok: false, errors: [{ field: fieldFor(e), message: e.message }] };
      }

      const method = resolveMethod(authMethod, deployment);
      const wrongMethod = methodError(method, deployment);
      if (wrongMethod) return { ok: false, errors: [{ field: 'authMethod', message: wrongMethod }] };

      /** ok:true, with who signed in and which instance answered. */
      async function connected(identity) {
        await ctx.state.set('ssoStartedAt', null);
        d.clearLog(d.logPath(profile));
        const { baseUrl: url, project } = await instanceInfo();
        return {
          ok: true,
          identity: identity || 'authenticated',
          // Show what was actually configured — the ticket URL only *implies* it.
          instance: url ? { baseUrl: url, project: project || null } : null,
        };
      }

      try {
        const me = await jtr(['whoami', '--json']);
        return await connected(me.display_name || me.name || me.key);
      } catch (e) {
        if (e.kind !== 'auth') {
          return { ok: false, errors: [{ field: fieldFor(e), message: e.message }] };
        }
      }

      // No session. Both headless methods store AND verify in one call, so a
      // success here is already proof — no second whoami to confirm it.
      if (method !== 'sso') {
        const missing = credentialErrors(method);
        if (missing) return { ok: false, errors: missing };
        try {
          const res = await jtr(authArgs(method));
          return await connected(res?.user);
        } catch (e) {
          // Rejected credentials belong on the credential field; anything else
          // (jtr missing, a URL that is not a Jira) belongs where it came from.
          return { ok: false, errors: [{ field: e.kind === 'auth' ? 'pat' : fieldFor(e), message: e.message }] };
        }
      }

      // SSO from here: the login runs outside this op's 30s budget.
      const startedAt = Number(await ctx.state.get('ssoStartedAt')) || 0;
      const elapsed = d.now() - startedAt;
      if (startedAt && elapsed < SSO_WINDOW_MS) {
        return { ok: false, pending: true, message: 'Waiting for the browser login to finish…' };
      }
      if (startedAt) {
        // The window closed without cookies — surface what the login itself said.
        await ctx.state.set('ssoStartedAt', null);
        const log = d.readLog(d.logPath(profile));
        return {
          ok: false,
          errors: [{ field: 'authMethod', message: `The browser login did not complete. Click Connect to try again.${log ? `\n\njtr said: ${log}` : ''}` }],
        };
      }
      d.clearLog(d.logPath(profile));
      d.startSso(jtrPath, { env, logPath: d.logPath(profile), browser });
      await ctx.state.set('ssoStartedAt', d.now());
      return { ok: false, pending: true, message: 'Browser opening — sign in there, then this will connect automatically.' };
    },

    async listTasks({ inputs = {}, search, cursor } = {}) {
      const { baseUrl: url, project, deployment } = await instanceInfo();
      // A pasted key or browse URL means "this exact ticket" — the filter would
      // otherwise hide it (text ~ never matches on issue key), and so would the
      // project scope, which is the whole point of pasting a key from
      // somewhere else. Deliberately left unscoped.
      const key = ticketKeyFrom(search);
      let raw;
      if (key) {
        raw = `key = ${key}`;
      } else {
        const combined = jqlWithSearch(baseJql(inputs), search);
        // Filter "None", no JQL, no search -> nothing to ask for. Listing every
        // ticket in the instance would be the wrong guess, and so would
        // quietly substituting a filter the user didn't pick.
        if (!combined) return { tasks: [] };
        raw = withProject(combined, project, mentionsProject(inputs.jql));
      }
      const jql = withDefaultOrder(raw);
      const args = ['search', jql, '--json', '--limit', String(PAGE)];
      // The paging flags are mutually exclusive per deployment — jtr rejects
      // --start-at on Cloud and --cursor on Server/DC with `unsupported_option`
      // rather than quietly serving page one. Our own cursor stays opaque; only
      // the offset dialect reads it as a number.
      if (deployment === CLOUD) {
        if (cursor) args.push('--cursor', String(cursor));
      } else {
        args.push('--start-at', String(Math.max(0, Number(cursor) || 0)));
      }
      // --all: keep the JQL WYSIWYG — jtr otherwise silently ANDs its default project.
      args.push('--all');
      const res = await jtr(args);
      const tickets = res.tickets || [];
      // Cloud's search endpoint reports no total at all, hence the fallback.
      ctx.log('info', `jira: ${tickets.length} of ${res.total ?? tickets.length} ticket(s) for ${res.jql || jql}`);
      const next = nextCursor(res, deployment);
      return {
        tasks: tickets.map((t) => toSummary(t, url)),
        ...(next === null ? {} : { cursor: next }),
      };
    },

    async getTask(id) {
      const key = ticketKeyFrom(id);
      if (!key) throw Object.assign(new Error(`bad Jira task id "${id}" (expected PROJ-123)`), { kind: 'plugin' });
      let res;
      try {
        res = await jtr(['view', key, '--json']);
      } catch (e) {
        if (e.code === 'not_found') return null; // host renders "task not found"
        throw e;
      }
      const t = res.ticket || {};
      let body = wikiToMarkdown(t.description || '');
      const comments = res.comments || [];
      if (comments.length) {
        body += '\n\n## Comments\n';
        for (const c of comments) {
          body += `\n**${userName(c.author) || 'unknown'}** (${isoDate(c.created)}):\n\n${wikiToMarkdown(c.body || '')}\n`;
        }
      }
      return {
        ...toSummary(t, await baseUrl()),
        body,
        meta: {
          key: t.key,
          project: t.project_key || null,
          issueType: t.issue_type || null,
          status: t.status || null,
          priority: t.priority || null,
          assignee: userName(t.assignee),
          reporter: userName(t.reporter),
          labels: t.labels || [],
          fixVersions: t.fix_versions || [],
          created: isoDate(t.created),
          updated: isoDate(t.updated),
        },
      };
    },

    async reportResult(id, { status, summary, links = [], inputs } = {}) {
      // Belt-and-braces: the host already skips when capabilities({inputs})
      // said no, but its capability probe defaults to writeBack:true on a
      // transport error — re-checking the run's own choice here means an
      // opted-out run can never be written to.
      if (!toBool(inputs?.writeBack)) return;
      const key = ticketKeyFrom(id);
      if (!key) throw Object.assign(new Error(`bad Jira task id "${id}" (expected PROJ-123)`), { kind: 'plugin' });
      let body = markdownToWiki(summary || `worca-cc run finished: ${status}`);
      if (links.length) {
        body += '\n\n';
        for (const l of links) body += `* [${l.title}|${l.url}]\n`;
      }
      // Comment first, transition second: a failed transition (names are
      // per-workflow) must never lose the summary that was already written.
      await jtr(['comment', key, body, '--yes', '--json']);
      if (status === 'completed' && transitionOnComplete) {
        try {
          await jtr(['transition', key, transitionOnComplete, '--yes', '--json']);
        } catch (e) {
          throw Object.assign(
            new Error(`comment posted, but the transition to "${transitionOnComplete}" failed: ${e.message} — \`jtr transition ${key}\` lists this ticket's valid transitions`),
            { kind: e.kind || 'plugin' },
          );
        }
      }
    },

    capabilities({ inputs } = {}) {
      // Write-back is answered per RUN: the host passes the run's pinned
      // source-panel inputs, and skips reportResult entirely on false. With no
      // inputs (a run predating the field, or none given) the answer is the
      // field's own default — no.
      return { writeBack: toBool(inputs?.writeBack), incrementalSync: false };
    },
  };
}
