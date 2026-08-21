// test/jira-source-connector.test.mjs — Jira (jtr) connector, pure unit tests
// with an injected fake exec (no subprocess, no jtr, no shim child) and a fake
// SSO launcher (no browser). The connector is plain ESM so it imports directly
// from examples/.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

import createTaskSource, {
  jqlWithSearch, wikiToMarkdown, markdownToWiki, ticketKeyFrom, withDefaultOrder, baseJql, withProject, mentionsProject,
  resolveMethod, methodError, nextCursor, normalizeDeployment,
} from '../connector/index.mjs';
import { jtrHome, ssoLogPath } from '../connector/jtr-cli.mjs';

const TICKET_URL = 'https://tracker.example.com/jira/browse/PROJ-1';

// ── harness ────────────────────────────────────────────────────────────────────
/** An execJtr result: exit 0 + JSON stdout unless overridden. */
function out(json, extra = {}) {
  return { code: 0, enoent: false, timedOut: false, stdout: JSON.stringify(json), stderr: '', ...extra };
}
function memState(seed = {}) {
  const m = new Map(Object.entries(seed));
  return { get: async (k) => (m.has(k) ? m.get(k) : null), set: async (k, v) => { m.set(k, v); }, _m: m };
}
function makeCtx(config = {}, state = memState()) {
  return {
    apiVersion: 1,
    config: { ticketUrl: TICKET_URL, jtrPath: 'jtr', ...config },
    state,
    log: () => {},
  };
}
/** Route table fake exec: [{ match: (args) => boolean, reply: result|fn(cmd,args) }]. Records calls. */
function fakeExec(routes) {
  const calls = [];
  const fn = async (cmd, args, opts) => {
    calls.push({ cmd, args, opts });
    for (const r of routes) {
      if (r.match(args)) return typeof r.reply === 'function' ? r.reply(cmd, args) : r.reply;
    }
    if (args[0] === '--version') return versionOut();   // harness default
    throw new Error(`unrouted exec: ${cmd} ${args.join(' ')}`);
  };
  fn.calls = calls;
  return fn;
}
/** Filesystem/clock/browser seams, all inert in tests. */
function fakeDeps(exec, { now = 1_000_000, log = '' } = {}) {
  const sso = { launched: 0, args: null };
  return {
    deps: {
      exec,
      ensureHome: () => '/tmp/jtr-home',
      logPath: () => '/tmp/jtr-home/sso.log',
      startSso: (jtrPath, opts) => { sso.launched += 1; sso.args = { jtrPath, ...opts }; },
      readLog: () => log,
      clearLog: () => {},
      now: () => now,
    },
    sso,
  };
}
const ticket = (key, summary, extra = {}) => ({
  key, summary, status: 'In Progress', priority: 'High', issue_type: 'Bug', project_key: key.split('-')[0],
  assignee: { name: 'jdoe', display_name: 'Jane Doe', key: 'jdoe', email: 'j@x.com' },
  reporter: { name: 'asmith', display_name: 'Al Smith', key: 'asmith', email: 'a@x.com' },
  created: '2026-06-01T09:00:00.000+0200', updated: '2026-06-12T14:23:01.000+0200',
  labels: ['release'], fix_versions: ['2026.07'], description: 'desc', ...extra,
});
const searchOut = (tickets) => out({ jql: 'sent-jql', count: tickets.length, tickets });
const viewOut = (t, comments = []) => out({ ticket: t, comments });
const jsonErr = (code, exit, message, fix) => out({ error: code, message, ...(fix ? { fix } : {}) }, { code: exit });
const configShow = out({ mode: 'global', base_url: 'https://tracker.example.com/jira', project: 'PROJ', pat_set: false });
const INIT = (a) => a[0] === 'init';
const VERSION = (a) => a[0] === '--version';
const versionOut = (v = '0.10.0') => out(null, { stdout: `jtr ${v}\n` });
/** init --json returns the config state it just wrote — including which
 *  deployment it resolved, which is the connector's whole source of truth. */
const initOut = (extra = {}) => out({
  mode: 'local', config_dir: '/home/u/jtr-home',
  base_url: 'https://tracker.example.com/jira', project: 'PROJ', deployment: 'server',
  auth_method: null, pat_set: false, authenticated: false, ...extra,
});
const cloudInitOut = (extra = {}) => initOut({
  base_url: 'https://acme.atlassian.net', deployment: 'cloud', ...extra,
});
const WHOAMI = (a) => a[0] === 'whoami';
const CONFIG = (a) => a[0] === 'config';
const SEARCH = (a) => a[0] === 'search';
const VIEW = (a) => a[0] === 'view';
const AUTH = (a) => a[0] === 'auth';
const NO_SESSION = jsonErr('not_authenticated', 2, 'No credentials.', 'jtr auth sso');

// ── validateConfig: the Connect state machine ──────────────────────────────────
test('Connect: no ticket URL -> field error, nothing executed', async () => {
  const exec = fakeExec([]);
  const { deps } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({ ticketUrl: '' }), deps).validateConfig();
  assert.equal(v.ok, false);
  assert.equal(v.errors[0].field, 'ticketUrl');
  assert.equal(exec.calls.length, 0);
});

test('Connect: a URL that is not a ticket URL is rejected before running jtr', async () => {
  const exec = fakeExec([]);
  const { deps } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({ ticketUrl: 'https://tracker.example.com/' }), deps).validateConfig();
  assert.equal(v.ok, false);
  assert.equal(v.errors[0].field, 'ticketUrl');
  assert.match(v.errors[0].message, /does not look like a ticket URL/);
  assert.equal(exec.calls.length, 0);
});

test('Connect: first call inits headlessly (--no-auth) then launches SSO detached', async () => {
  const state = memState();
  const exec = fakeExec([
    { match: INIT, reply: initOut() },
    { match: WHOAMI, reply: jsonErr('not_authenticated', 2, 'Not authenticated.', 'jtr auth sso') },
  ]);
  const { deps, sso } = fakeDeps(exec, { now: 5000 });
  const v = await createTaskSource(makeCtx({ browser: 'msedge' }, state), deps).validateConfig();

  const init = exec.calls.find((c) => INIT(c.args)).args;
  // --bare keeps init out of the user's project: no .gitignore edit, no skill.
  assert.deepEqual(init, ['init', '--ticket', TICKET_URL, '--force', '--bare', '--json', '--browser', 'msedge', '--no-auth']);
  assert.equal(v.ok, false);
  assert.equal(v.pending, true);
  assert.match(v.message, /Browser opening/);
  assert.equal(sso.launched, 1);
  assert.equal(sso.args.browser, 'msedge');
  assert.equal(await state.get('ssoStartedAt'), 5000);
});

test('Connect: an old jtr is refused with a sentence, not an argparse error', async () => {
  // 0.9.x is the interesting case, not 0.8: it is new enough for `init --json`
  // but has no --deployment, no `auth token` and no cursor paging, so it would
  // fail deep inside Connect instead of here.
  for (const old of ['0.8.0', '0.9.1']) {
    const exec = fakeExec([{ match: VERSION, reply: versionOut(old) }]);
    const { deps } = fakeDeps(exec);
    const v = await createTaskSource(makeCtx(), deps).validateConfig();
    assert.equal(v.ok, false);
    assert.equal(v.errors[0].field, 'jtrPath');
    assert.match(v.errors[0].message, new RegExp(`${old.replace(/\./g, '\\.')} is too old.*needs 0\\.10\\.0`));
    assert.equal(exec.calls.filter((c) => INIT(c.args)).length, 0, 'must not run init against an old jtr');
  }
});

test('Connect: the version probe runs once, then is cached in state', async () => {
  const state = memState();
  const exec = fakeExec([
    { match: VERSION, reply: versionOut('0.10.1') },
    { match: INIT, reply: initOut() },
    { match: WHOAMI, reply: out({ display_name: 'Jane Doe' }) },
  ]);
  const { deps } = fakeDeps(exec);
  const src = () => createTaskSource(makeCtx({}, state), deps);
  await src().validateConfig();
  await src().validateConfig();
  assert.equal(exec.calls.filter((c) => VERSION(c.args)).length, 1);
  assert.equal(await state.get('versionOk'), true);
});

test('Connect: init --json reports the config it wrote — no config-show round trip', async () => {
  const state = memState();
  const exec = fakeExec([
    { match: INIT, reply: initOut({ project: 'ACME' }) },
    { match: WHOAMI, reply: out({ display_name: 'Jane Doe' }) },
  ]);
  const { deps } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({}, state), deps).validateConfig();
  assert.equal(v.ok, true);
  assert.deepEqual(v.instance, { baseUrl: 'https://tracker.example.com/jira', project: 'ACME' });
  assert.equal(exec.calls.filter((c) => CONFIG(c.args)).length, 0, 'init already reported the state');
});

test('Connect: reports which instance was resolved, not just who signed in', async () => {
  const state = memState({ initUrl: TICKET_URL });
  const exec = fakeExec([
    { match: WHOAMI, reply: out({ display_name: 'Jane Doe' }) },
    { match: CONFIG, reply: configShow },
  ]);
  const { deps } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({}, state), deps).validateConfig();
  assert.deepEqual(v.instance, { baseUrl: 'https://tracker.example.com/jira', project: 'PROJ' });
});

test('Connect: a failing init surfaces jtr\'s own error message', async () => {
  const exec = fakeExec([
    { match: INIT, reply: jsonErr('input_required', 1, 'A base URL is required.', 'jtr init --ticket <url>') },
  ]);
  const { deps } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx(), deps).validateConfig();
  assert.equal(v.ok, false);
  assert.equal(v.errors[0].field, 'ticketUrl');
  assert.match(v.errors[0].message, /A base URL is required/);
});

test('Connect: while a login is in flight it reports pending and does NOT relaunch', async () => {
  const state = memState({ initUrl: TICKET_URL, ssoStartedAt: 1000 });
  const exec = fakeExec([{ match: WHOAMI, reply: jsonErr('not_authenticated', 2, 'Not authenticated.') }]);
  const { deps, sso } = fakeDeps(exec, { now: 1000 + 60_000 }); // 1 min in, window is 5
  const v = await createTaskSource(makeCtx({}, state), deps).validateConfig();
  assert.deepEqual({ ok: v.ok, pending: v.pending }, { ok: false, pending: true });
  assert.match(v.message, /Waiting for the browser login/);
  assert.equal(sso.launched, 0, 'a second browser must not be launched while one is pending');
  assert.equal(exec.calls.filter((c) => INIT(c.args)).length, 0, 'already initialised -> no re-init');
});

test('Connect: a login that never completed surfaces the launcher log and clears the flag', async () => {
  const state = memState({ initUrl: TICKET_URL, ssoStartedAt: 1000 });
  const exec = fakeExec([{ match: WHOAMI, reply: jsonErr('not_authenticated', 2, 'Not authenticated.') }]);
  const { deps, sso } = fakeDeps(exec, { now: 1000 + 6 * 60_000, log: 'No installed browser found for SSO login' });
  const v = await createTaskSource(makeCtx({}, state), deps).validateConfig();
  assert.equal(v.ok, false);
  assert.equal(v.pending, undefined);
  assert.equal(v.errors[0].field, 'authMethod');
  assert.match(v.errors[0].message, /No installed browser found/);
  assert.equal(sso.launched, 0);
  assert.equal(await state.get('ssoStartedAt'), null, 'cleared so the next Connect relaunches');
});

test('Connect: authenticated -> ok + identity, SSO flag cleared, base_url cached', async () => {
  const state = memState({ initUrl: TICKET_URL, ssoStartedAt: 1000 });
  const exec = fakeExec([
    { match: WHOAMI, reply: out({ name: 'jdoe', display_name: 'Jane Doe', key: 'jdoe', email: 'j@x.com' }) },
    { match: CONFIG, reply: configShow },
  ]);
  const { deps } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({}, state), deps).validateConfig();
  assert.equal(v.ok, true);
  assert.equal(v.identity, 'Jane Doe');
  assert.equal(await state.get('ssoStartedAt'), null);
  assert.equal(await state.get('baseUrl'), 'https://tracker.example.com/jira');
});

test('Connect: pat auth stores the token in its own step and never opens a browser', async () => {
  const state = memState();
  const exec = fakeExec([
    { match: INIT, reply: initOut() },
    { match: WHOAMI, reply: NO_SESSION },
    { match: AUTH, reply: out({ authenticated: true, auth_method: 'pat', user: 'jdoe' }) },
  ]);
  const { deps, sso } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({ authMethod: 'pat', pat: 'secret-token' }, state), deps).validateConfig();

  // init never carries credentials: which ones are legal is a function of the
  // deployment it is about to report, and an impossible pair makes jtr exit on
  // a usage error with no JSON to turn into a message.
  const init = exec.calls.find((c) => INIT(c.args)).args;
  assert.ok(init.includes('--no-auth'));
  assert.ok(!init.includes('--auth') && !init.includes('secret-token'));
  assert.deepEqual(exec.calls.find((c) => AUTH(c.args)).args, ['auth', 'pat', '--pat', 'secret-token', '--json']);
  assert.equal(sso.launched, 0, 'pat auth must never launch a browser');
  // `auth pat` stores AND verifies, so its own answer is the identity — no
  // second whoami to confirm what jtr just confirmed.
  assert.deepEqual({ ok: v.ok, identity: v.identity }, { ok: true, identity: 'jdoe' });
  assert.equal(exec.calls.filter((c) => WHOAMI(c.args)).length, 1);
});

test('Connect: a rejected token is reported on the token field, not the URL', async () => {
  const exec = fakeExec([
    { match: INIT, reply: initOut() },
    { match: WHOAMI, reply: NO_SESSION },
    { match: AUTH, reply: jsonErr('not_authenticated', 2, 'Auth failed (HTTP 401).') },
  ]);
  const { deps } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({ authMethod: 'pat', pat: 'wrong' }, memState()), deps).validateConfig();
  assert.equal(v.ok, false);
  assert.deepEqual(v.errors, [{ field: 'pat', message: 'Auth failed (HTTP 401).' }]);
});

test('Connect: pat with no token asks for one instead of letting jtr 401', async () => {
  const exec = fakeExec([
    { match: INIT, reply: initOut() },
    { match: WHOAMI, reply: NO_SESSION },
  ]);
  const { deps, sso } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({ authMethod: 'pat' }, memState()), deps).validateConfig();
  assert.equal(v.errors[0].field, 'pat');
  assert.match(v.errors[0].message, /Paste a personal access token/);
  assert.equal(exec.calls.filter((c) => AUTH(c.args)).length, 0, 'nothing to authenticate with');
  assert.equal(sso.launched, 0);
});

test('Connect: changing the ticket URL re-inits; an unchanged one does not', async () => {
  const state = memState({ initUrl: TICKET_URL });
  const exec = fakeExec([
    { match: INIT, reply: out({ ok: true }) },
    { match: WHOAMI, reply: out({ display_name: 'Jane Doe' }) },
    { match: CONFIG, reply: configShow },
  ]);
  const { deps } = fakeDeps(exec);
  await createTaskSource(makeCtx({}, state), deps).validateConfig();
  assert.equal(exec.calls.filter((c) => INIT(c.args)).length, 0);

  const other = 'https://tracker.example.com/jira/browse/OTHER-9';
  await createTaskSource(makeCtx({ ticketUrl: other }, state), deps).validateConfig();
  assert.equal(exec.calls.filter((c) => INIT(c.args)).length, 1);
  assert.equal(await state.get('initUrl'), other);
});

test('Connect: jtr missing -> the error is pinned to the jtrPath field', async () => {
  const exec = fakeExec([{ match: () => true, reply: out({}, { code: 1, enoent: true, stdout: '' }) }]);
  const { deps } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({ jtrPath: '/nope/jtr' }), deps).validateConfig();
  assert.equal(v.ok, false);
  assert.equal(v.errors[0].field, 'jtrPath');
  assert.match(v.errors[0].message, /jtr not found at "\/nope\/jtr"/);
});

// ── deployments ────────────────────────────────────────────────────────────────
// Server/DC and Cloud differ in the credentials they accept and the way they
// page, and the connector never guesses which is which: `jtr init --json`
// reports it and everything else follows from that one value.
test('Connect: Cloud is discovered from init and authenticates with email + API token', async () => {
  const state = memState();
  const exec = fakeExec([
    { match: INIT, reply: cloudInitOut() },
    { match: WHOAMI, reply: NO_SESSION },
    { match: AUTH, reply: out({ authenticated: true, auth_method: 'token', user: 'Jane Doe' }) },
  ]);
  const { deps, sso } = fakeDeps(exec);
  const ctx = makeCtx({ ticketUrl: 'https://acme.atlassian.net/browse/PROJ-1', email: 'j@acme.com', pat: 'api-token' }, state);
  const v = await createTaskSource(ctx, deps).validateConfig();

  // Authentication is "auto": Cloud has exactly one workable method, so the
  // user picking it would be a quiz with one right answer.
  assert.deepEqual(exec.calls.find((c) => AUTH(c.args)).args,
    ['auth', 'token', '--email', 'j@acme.com', '--token', 'api-token', '--json']);
  assert.deepEqual({ ok: v.ok, identity: v.identity }, { ok: true, identity: 'Jane Doe' });
  assert.equal(sso.launched, 0, 'Atlassian rejects browser cookies as REST credentials');
  assert.equal(await state.get('deployment'), 'cloud');
});

test('Connect: an impossible method is refused at once, not after a five-minute wait', async () => {
  // The failure this prevents: on Cloud, `jtr auth sso` refuses immediately —
  // launching it anyway parked the pane in "waiting" for the whole SSO window
  // before showing a message that existed on the first poll.
  for (const [deployment, reply, method, expect] of [
    ['cloud', cloudInitOut(), 'sso', /Jira Cloud cannot authenticate with "sso"/],
    ['cloud', cloudInitOut(), 'pat', /Jira Cloud cannot authenticate with "pat"/],
    ['server', initOut(), 'token', /Server\/Data Center cannot authenticate with "token"/],
  ]) {
    const exec = fakeExec([{ match: INIT, reply }, { match: WHOAMI, reply: NO_SESSION }]);
    const { deps, sso } = fakeDeps(exec);
    const v = await createTaskSource(makeCtx({ authMethod: method, pat: 'x', email: 'j@x.com' }, memState()), deps).validateConfig();
    assert.equal(v.ok, false, deployment);
    assert.equal(v.pending, undefined, 'never a pending state for a method that cannot work');
    assert.deepEqual(v.errors.map((e) => e.field), ['authMethod']);
    assert.match(v.errors[0].message, expect);
    assert.equal(sso.launched, 0);
    assert.equal(exec.calls.filter((c) => AUTH(c.args)).length, 0, 'nothing is stored for an impossible method');
    assert.equal(exec.calls.filter((c) => WHOAMI(c.args)).length, 0, 'refused before touching the network');
  }
});

test('Connect: Cloud without an account email asks for it rather than 401-ing', async () => {
  const exec = fakeExec([{ match: INIT, reply: cloudInitOut() }, { match: WHOAMI, reply: NO_SESSION }]);
  const { deps } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({ pat: 'api-token' }, memState()), deps).validateConfig();
  assert.deepEqual(v.errors.map((e) => e.field), ['email']);
  assert.match(v.errors[0].message, /account email/);
  assert.equal(exec.calls.filter((c) => AUTH(c.args)).length, 0);
});

test('Connect: the deployment override reaches jtr, and changing it re-inits', async () => {
  const state = memState();
  const exec = fakeExec([
    { match: INIT, reply: cloudInitOut() },
    { match: WHOAMI, reply: out({ display_name: 'Jane Doe' }) },
  ]);
  const { deps } = fakeDeps(exec);
  const src = (config) => createTaskSource(makeCtx(config, state), deps);

  await src({ deployment: 'cloud' }).validateConfig();
  assert.deepEqual(exec.calls.find((c) => INIT(c.args)).args,
    ['init', '--ticket', TICKET_URL, '--force', '--bare', '--json', '--deployment', 'cloud', '--no-auth']);

  // Same URL, so the memo would say "done" — but the pin is what jtr's own
  // config records, and flipping it has to get there.
  await src({ deployment: 'server' }).validateConfig();
  const inits = exec.calls.filter((c) => INIT(c.args));
  assert.equal(inits.length, 2);
  assert.ok(inits[1].args.includes('server'));

  // ...while an unchanged pin still does not re-init.
  await src({ deployment: 'server' }).validateConfig();
  assert.equal(exec.calls.filter((c) => INIT(c.args)).length, 2);
});

test('Connect: a profile initialised before Cloud support is still Server/DC', async () => {
  // Upgrade path: state written by the Server-only connector has a baseUrl and
  // an initUrl but no deployment, and must neither re-init nor become Cloud.
  const state = memState({ initUrl: TICKET_URL, baseUrl: 'https://tracker.example.com/jira' });
  const exec = fakeExec([{ match: WHOAMI, reply: NO_SESSION }]);
  const { deps, sso } = fakeDeps(exec, { now: 5000 });
  const v = await createTaskSource(makeCtx({}, state), deps).validateConfig();
  assert.equal(exec.calls.filter((c) => INIT(c.args)).length, 0, 'no forced re-init on upgrade');
  assert.equal(v.pending, true);
  assert.equal(sso.launched, 1, 'auto still means sso on Server/DC');
});

test('Connect: a base URL jtr does not speak lands on the deployment field, not on auth', async () => {
  // `unsupported_deployment` used to be treated as an auth failure, which sent
  // Connect off to launch a browser login — the one remedy that cannot help.
  const exec = fakeExec([
    { match: INIT, reply: jsonErr('unsupported_deployment', 2, 'Not a Jira this CLI speaks.', 'jtr config deployment server') },
  ]);
  const { deps, sso } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({}, memState()), deps).validateConfig();
  assert.equal(v.ok, false);
  assert.equal(v.errors[0].field, 'deployment');
  assert.match(v.errors[0].message, /Not a Jira this CLI speaks.*fix: jtr config deployment server/);
  assert.equal(sso.launched, 0);
});

test('listTasks: Cloud pages by cursor and never sends --start-at', async () => {
  // jtr answers --start-at on Cloud with `unsupported_option`, and Cloud's
  // next_start_at is always null — reading it would look like "last page" and
  // silently cap every query at fifty tickets.
  const state = memState({ baseUrl: 'https://acme.atlassian.net', deployment: 'cloud' });
  const page = (token) => out({
    jql: 'q', count: 1, total: null, next_start_at: null, next_page_token: token,
    tickets: [ticket(`PROJ-${token || 'last'}`, 'row')],
  });
  const exec = fakeExec([
    { match: (a) => SEARCH(a) && !a.includes('--cursor'), reply: page('tok-2') },
    { match: (a) => SEARCH(a) && a[a.indexOf('--cursor') + 1] === 'tok-2', reply: page(null) },
  ]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx({}, state), deps);

  const first = await src.listTasks({ inputs: { jql: 'project = X' } });
  assert.deepEqual(exec.calls[0].args,
    ['search', 'project = X ORDER BY updated DESC', '--json', '--limit', '50', '--all']);
  assert.equal(first.cursor, 'tok-2');

  const second = await src.listTasks({ inputs: { jql: 'project = X' }, cursor: first.cursor });
  assert.ok(!exec.calls[1].args.includes('--start-at'));
  assert.deepEqual(second.tasks.map((t) => t.id), ['PROJ-last']);
  assert.equal('cursor' in second, false, 'no token -> last page');
});

test('normalizeDeployment / resolveMethod / methodError: the deployment decides', () => {
  assert.equal(normalizeDeployment('cloud'), 'cloud');
  assert.equal(normalizeDeployment('CLOUD'), 'cloud');
  // Anything unrecognised is Server/DC — what a self-hosted URL almost always
  // is, and the only thing this plugin spoke before Cloud support.
  assert.equal(normalizeDeployment('server'), 'server');
  assert.equal(normalizeDeployment(null), 'server');
  assert.equal(normalizeDeployment('datacenter'), 'server');

  assert.equal(resolveMethod('auto', 'server'), 'sso');
  assert.equal(resolveMethod('', 'server'), 'sso');
  assert.equal(resolveMethod(undefined, 'cloud'), 'token');
  assert.equal(resolveMethod('pat', 'server'), 'pat', 'an explicit choice is never overridden');
  assert.equal(resolveMethod('sso', 'cloud'), 'sso', 'even a wrong one — methodError reports it');

  assert.equal(methodError('sso', 'server'), null);
  assert.equal(methodError('pat', 'server'), null);
  assert.equal(methodError('token', 'cloud'), null);
  assert.match(methodError('token', 'server'), /Server\/Data Center/);
  assert.match(methodError('sso', 'cloud'), /Jira Cloud/);
  assert.match(methodError('pat', 'cloud'), /Jira Cloud/);
});

test('nextCursor: each deployment reads only its own paging field', () => {
  assert.equal(nextCursor({ next_start_at: 50 }, 'server'), '50');
  assert.equal(nextCursor({ next_start_at: 0 }, 'server'), '0', 'offset zero is a page, not an absence');
  assert.equal(nextCursor({ next_start_at: null }, 'server'), null);
  assert.equal(nextCursor({ next_page_token: 'tok' }, 'cloud'), 'tok');
  assert.equal(nextCursor({ next_page_token: null }, 'cloud'), null);
  assert.equal(nextCursor({}, 'cloud'), null);
  // Crossed wires would read as "last page" and truncate the result set.
  assert.equal(nextCursor({ next_page_token: 'tok' }, 'server'), null);
  assert.equal(nextCursor({ next_start_at: 50 }, 'cloud'), null);
});

// ── listTasks ──────────────────────────────────────────────────────────────────
test('listTasks: exact argv, field mapping, status heuristic, ISO dates, no cursor', async () => {
  const state = memState({ initUrl: TICKET_URL, baseUrl: 'https://tracker.example.com/jira' });
  const exec = fakeExec([{ match: SEARCH, reply: searchOut([ticket('PROJ-1', 'Alpha'), ticket('PROJ-2', 'Beta', { status: 'Done' })]) }]);
  const { deps } = fakeDeps(exec);
  const jql = 'assignee = currentUser() ORDER BY updated DESC';
  const res = await createTaskSource(makeCtx({}, state), deps).listTasks({ inputs: { jql } });
  assert.deepEqual(exec.calls[0].args,
    ['search', `${jql}`, '--json', '--limit', '50', '--start-at', '0', '--all']);
  assert.deepEqual(res.tasks[0], {
    id: 'PROJ-1', title: 'Alpha', url: 'https://tracker.example.com/jira/browse/PROJ-1',
    state: 'open', labels: ['release'], updatedAt: '2026-06-12T12:23:01.000Z',
  });
  assert.equal(res.tasks[1].state, 'closed');
  assert.equal('cursor' in res, false); // jtr has no offset paging
});

test('listTasks: pages via --start-at, and stops when next_start_at goes null', async () => {
  const state = memState({ baseUrl: 'https://x/jira' });
  const page = (startAt, next) => out({
    jql: 'q', count: 1, start_at: startAt, max_results: 50, total: 60, next_start_at: next,
    tickets: [ticket(`PROJ-${startAt}`, `row ${startAt}`)],
  });
  const exec = fakeExec([
    { match: (a) => SEARCH(a) && a[a.indexOf('--start-at') + 1] === '0', reply: page(0, 50) },
    { match: (a) => SEARCH(a) && a[a.indexOf('--start-at') + 1] === '50', reply: page(50, null) },
  ]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx({}, state), deps);

  const first = await src.listTasks({ inputs: { jql: 'project = X' } });
  assert.equal(first.cursor, '50', 'more results -> cursor is the next offset');

  const second = await src.listTasks({ inputs: { jql: 'project = X' }, cursor: first.cursor });
  assert.deepEqual(second.tasks.map((t) => t.id), ['PROJ-50']);
  assert.equal('cursor' in second, false, 'last page -> no cursor');
});

test('listTasks: a pasted key or browse URL looks up that ticket, ignoring the JQL filter', async () => {
  const state = memState({ baseUrl: 'https://x/jira' });
  const exec = fakeExec([{ match: SEARCH, reply: searchOut([ticket('OTHER-13727', 'Found it')]) }]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx({}, state), deps);
  const filter = { jql: 'assignee = currentUser() AND statusCategory != Done' };

  await src.listTasks({ inputs: filter, search: 'other-13727' });
  assert.equal(exec.calls[0].args[1], 'key = OTHER-13727 ORDER BY updated DESC');

  await src.listTasks({ inputs: filter, search: 'https://tracker.example.com/jira/browse/OTHER-13727?focused=1' });
  assert.equal(exec.calls[1].args[1], 'key = OTHER-13727 ORDER BY updated DESC');
});

test('listTasks: filter "None" with nothing typed is NO filter — nothing listed, no query run', async () => {
  const state = memState({ baseUrl: 'https://x/jira' });
  const exec = fakeExec([{ match: SEARCH, reply: searchOut([]) }]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx({}, state), deps);

  assert.deepEqual(await src.listTasks({ inputs: {} }), { tasks: [] });
  assert.deepEqual(await src.listTasks({ inputs: { filter: 'none', jql: '   ' }, search: '  ' }), { tasks: [] });
  assert.equal(exec.calls.filter((c) => SEARCH(c.args)).length, 0,
    'no filter and no search means there is nothing to ask Jira for');
});

test('baseJql: presets resolve, and a typed JQL always overrides the dropdown', () => {
  assert.equal(baseJql({}), '');
  assert.equal(baseJql({ filter: 'none' }), '');
  assert.equal(baseJql({ filter: 'mine' }), 'assignee = currentUser()');
  assert.equal(baseJql({ filter: 'reported-by-me' }), 'reporter = currentUser()');
  assert.equal(baseJql({ filter: 'updated-week' }), 'updated >= -7d');
  // The override is the whole point: a filled-in box is never ignored.
  assert.equal(baseJql({ filter: 'mine', jql: 'project = ACME' }), 'project = ACME');
  assert.equal(baseJql({ filter: 'mine', jql: '   ' }), 'assignee = currentUser()');
  assert.equal(baseJql({ filter: 'nonsense-value' }), '', 'an unknown preset filters nothing rather than guessing');
});

test('listTasks: a preset becomes its JQL, and the JQL box overrides it', async () => {
  const state = memState({ baseUrl: 'https://x/jira' });
  const exec = fakeExec([{ match: SEARCH, reply: searchOut([]) }]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx({}, state), deps);
  const jqlOf = (i) => exec.calls.filter((c) => SEARCH(c.args))[i].args[1];

  await src.listTasks({ inputs: { filter: 'mine' } });
  assert.equal(jqlOf(0), 'assignee = currentUser() ORDER BY updated DESC');

  await src.listTasks({ inputs: { filter: 'mine', jql: 'status = Blocked' } });
  assert.equal(jqlOf(1), 'status = Blocked ORDER BY updated DESC');

  // Search text folds onto whichever of the two won.
  await src.listTasks({ inputs: { filter: 'mine' }, search: 'kafka' });
  assert.equal(jqlOf(2), '(assignee = currentUser()) AND text ~ "kafka" ORDER BY updated DESC');
});

test('listTasks: scopes to the configured project — except a pasted key', async () => {
  const state = memState({ baseUrl: 'https://x/jira', project: 'ACME' });
  const exec = fakeExec([{ match: SEARCH, reply: searchOut([]) }]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx({}, state), deps);
  const jqlOf = (i) => exec.calls.filter((c) => SEARCH(c.args))[i].args[1];

  await src.listTasks({ inputs: { filter: 'mine' } });
  assert.equal(jqlOf(0), 'project = ACME AND (assignee = currentUser()) ORDER BY updated DESC');

  // A key may belong to any project — scoping it would silently find nothing.
  await src.listTasks({ inputs: { filter: 'mine' }, search: 'OTHER-42' });
  assert.equal(jqlOf(1), 'key = OTHER-42 ORDER BY updated DESC');

  // The user naming a project takes over; no contradictory double constraint.
  await src.listTasks({ inputs: { jql: 'project = OTHER AND status = Open' } });
  assert.equal(jqlOf(2), 'project = OTHER AND status = Open ORDER BY updated DESC');

  // The word "project" INSIDE the search text must not disable scoping — jtr's
  // own substring rule gets this wrong, which is why we scope ourselves.
  await src.listTasks({ inputs: {}, search: 'project plan' });
  assert.equal(jqlOf(3), 'project = ACME AND (text ~ "project plan") ORDER BY updated DESC');

  // A user ORDER BY survives the rewrite and stays legal (never inside parens).
  await src.listTasks({ inputs: { jql: 'status = Open ORDER BY created ASC' } });
  assert.equal(jqlOf(4), 'project = ACME AND (status = Open) ORDER BY created ASC');
});

test('withProject / mentionsProject: word-boundary match, empty inputs pass through', () => {
  assert.equal(withProject('assignee = x', 'ACME'), 'project = ACME AND (assignee = x)');
  assert.equal(withProject('assignee = x', ''), 'assignee = x', 'no configured project -> unscoped');
  assert.equal(withProject('assignee = x', 'ACME', true), 'assignee = x', 'caller says the user handles it');
  assert.equal(withProject('', 'ACME'), '');
  assert.equal(mentionsProject('project = X'), true);
  assert.equal(mentionsProject('PROJECT in (A,B)'), true);
  assert.equal(mentionsProject('projectType = software'), false, 'not a project constraint');
  assert.equal(mentionsProject(''), false);
});

test('listTasks: filter alone, search alone, and both together', async () => {
  const state = memState({ baseUrl: 'https://x/jira' });
  const exec = fakeExec([{ match: SEARCH, reply: searchOut([]) }]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx({}, state), deps);
  const jqlOf = (i) => exec.calls.filter((c) => SEARCH(c.args))[i].args[1];

  // A typed filter keeps its own terms; only the sort is supplied.
  await src.listTasks({ inputs: { jql: 'project = ACME AND status = Blocked' } });
  assert.equal(jqlOf(0), 'project = ACME AND status = Blocked ORDER BY updated DESC');

  // Search with no filter stands on its own.
  await src.listTasks({ inputs: {}, search: 'kafka' });
  assert.equal(jqlOf(1), 'text ~ "kafka" ORDER BY updated DESC');

  // Both: the filter is parenthesised and the search ANDed onto it.
  await src.listTasks({ inputs: { jql: 'project = ACME' }, search: 'kafka' });
  assert.equal(jqlOf(2), '(project = ACME) AND text ~ "kafka" ORDER BY updated DESC');
});

test('listTasks: free text still ANDs text ~ "..." into the JQL, ORDER BY stays trailing', async () => {
  const state = memState({ baseUrl: 'https://x/jira' });
  const exec = fakeExec([{ match: SEARCH, reply: searchOut([]) }]);
  const { deps } = fakeDeps(exec);
  await createTaskSource(makeCtx({}, state), deps).listTasks({
    inputs: { jql: 'assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC' },
    search: 'he said "hi"',
  });
  assert.equal(
    exec.calls[0].args[1],
    '(assignee = currentUser() AND statusCategory != Done) AND text ~ "he said \\"hi\\"" ORDER BY updated DESC',
  );
});

// ── getTask ────────────────────────────────────────────────────────────────────
test('getTask: uppercases the key, converts wiki markup, assembles ## Comments + meta', async () => {
  const state = memState({ baseUrl: 'https://tracker.example.com/jira' });
  const exec = fakeExec([
    { match: VIEW, reply: viewOut(
      ticket('PROJ-7', 'Fix the flux', { description: 'h2. Repro\n{code:python}\nboom()\n{code}\nSee [docs|https://x/docs]' }),
      [
        { id: '1', author: { name: 'jdoe', display_name: 'Jane Doe' }, body: 'try X', created: '2026-06-02T10:00:00.000+0200', updated: null },
        { id: '2', author: null, body: 'h1. shout', created: '2026-06-03T10:00:00.000+0200', updated: null },
      ],
    ) },
  ]);
  const { deps } = fakeDeps(exec);
  const t = await createTaskSource(makeCtx({}, state), deps).getTask('proj-7');
  assert.deepEqual(exec.calls[0].args, ['view', 'PROJ-7', '--json']);
  assert.equal(t.id, 'PROJ-7');
  assert.equal(t.url, 'https://tracker.example.com/jira/browse/PROJ-7');
  assert.equal(t.body, [
    '## Repro\n```python\nboom()\n```\nSee [docs](https://x/docs)',
    '',
    '## Comments',
    '',
    '**Jane Doe** (2026-06-02T08:00:00.000Z):',
    '',
    'try X',
    '',
    '**unknown** (2026-06-03T08:00:00.000Z):',
    '',
    '# shout',
    '',
  ].join('\n'));
  assert.deepEqual(t.meta, {
    key: 'PROJ-7', project: 'PROJ', issueType: 'Bug', status: 'In Progress', priority: 'High',
    assignee: 'Jane Doe', reporter: 'Al Smith', labels: ['release'], fixVersions: ['2026.07'],
    created: '2026-06-01T07:00:00.000Z', updated: '2026-06-12T12:23:01.000Z',
  });
});

test('getTask: not_found -> null (host renders "task not found"); bad id -> plugin error, no exec', async () => {
  const state = memState({ baseUrl: 'https://x/jira' });
  const exec = fakeExec([{ match: VIEW, reply: jsonErr('not_found', 1, 'PROJ-999 not found') }]);
  const { deps } = fakeDeps(exec);
  assert.equal(await createTaskSource(makeCtx({}, state), deps).getTask('PROJ-999'), null);

  const noExec = fakeExec([]);
  const { deps: deps2 } = fakeDeps(noExec);
  await assert.rejects(() => createTaskSource(makeCtx({}, state), deps2).getTask('not a key'), (e) => {
    assert.equal(e.kind, 'plugin');
    assert.match(e.message, /bad Jira task id/);
    return true;
  });
  assert.equal(noExec.calls.length, 0);
});

// ── error kinds ────────────────────────────────────────────────────────────────
test('error kinds: jira_error -> plugin, non-JSON stdout -> plugin, timeout -> timeout, bare exit -> plugin+stderr', async () => {
  const state = memState({ baseUrl: 'https://x/jira' });
  const src = (reply) => {
    const { deps } = fakeDeps(fakeExec([{ match: () => true, reply }]));
    return createTaskSource(makeCtx({}, state), deps);
  };
  await assert.rejects(
    () => src(jsonErr('jira_error', 1, 'HTTP 400: bad field')).listTasks({ inputs: { jql: 'project = X' } }),
    (e) => e.kind === 'plugin' && /HTTP 400/.test(e.message),
  );
  await assert.rejects(
    () => src(out({}, { stdout: 'Rich table leaked\n' })).listTasks({ inputs: { jql: 'project = X' } }),
    (e) => e.kind === 'plugin' && /non-JSON/.test(e.message),
  );
  await assert.rejects(
    () => src(out({}, { code: 1, timedOut: true, stdout: '' })).listTasks({ inputs: { jql: 'project = X' } }),
    (e) => e.kind === 'timeout',
  );
  await assert.rejects(
    () => src(out({}, { code: 3, stdout: '', stderr: 'Traceback: boom' })).listTasks({ inputs: { jql: 'project = X' } }),
    (e) => e.kind === 'plugin' && /exited 3/.test(e.message) && /Traceback: boom/.test(e.message),
  );
});

// ── capabilities / write-back ──────────────────────────────────────────────────
test('capabilities: write-back is a per-RUN choice — off unless the run\'s inputs say yes', () => {
  const { deps } = fakeDeps(fakeExec([]));
  const src = createTaskSource(makeCtx(), deps);
  // No inputs (a run predating the field) falls back to the default: no.
  assert.deepEqual(src.capabilities(), { writeBack: false, incrementalSync: false });
  assert.deepEqual(src.capabilities({ inputs: {} }), { writeBack: false, incrementalSync: false });
  assert.deepEqual(src.capabilities({ inputs: { writeBack: 'no' } }), { writeBack: false, incrementalSync: false });
  assert.deepEqual(src.capabilities({ inputs: { writeBack: 'yes' } }), { writeBack: true, incrementalSync: false });
});

const COMMENT = (a) => a[0] === 'comment';
const TRANSITION = (a) => a[0] === 'transition';

test('reportResult: posts the summary as a wiki comment with --yes --json, links appended', async () => {
  const exec = fakeExec([{ match: COMMENT, reply: out({ ok: true }) }]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx(), deps);
  // The exact shape sources.mjs#buildResultSummary emits.
  const summary = [
    '### Worca CC run `p-1` — done', '', '**Fix the flux**', '',
    '- Diffstat: 3 changed, 1 new, 0 deleted, +120 / -45',
    '- Branch: `feat/flux`',
  ].join('\n');
  await src.reportResult('proj-7', {
    status: 'completed', summary, links: [{ title: 'Pull request', url: 'https://x/pr/1' }],
    inputs: { writeBack: 'yes' },
  });

  const call = exec.calls.find((c) => COMMENT(c.args));
  assert.deepEqual([call.args[0], call.args[1], ...call.args.slice(3)], ['comment', 'PROJ-7', '--yes', '--json']);
  assert.equal(call.args[2], [
    'h3. Worca CC run {{p-1}} — done', '', '*Fix the flux*', '',
    '* Diffstat: 3 changed, 1 new, 0 deleted, +120 / -45',
    '* Branch: {{feat/flux}}',
    '',
    '* [Pull request|https://x/pr/1]\n',
  ].join('\n'));
  assert.equal(exec.calls.filter((c) => TRANSITION(c.args)).length, 0, 'no transition configured -> comment only');
});

test('reportResult: transitions only on completed AND only when configured', async () => {
  const routes = [
    { match: COMMENT, reply: out({ ok: true }) },
    { match: TRANSITION, reply: out({ ok: true }) },
  ];
  const cfg = { transitionOnComplete: 'Done' };
  const OPTED_IN = { writeBack: 'yes' };

  const done = fakeExec(routes);
  await createTaskSource(makeCtx(cfg), fakeDeps(done).deps)
    .reportResult('PROJ-7', { status: 'completed', summary: 's', inputs: OPTED_IN });
  const t = done.calls.find((c) => TRANSITION(c.args));
  assert.deepEqual(t.args, ['transition', 'PROJ-7', 'Done', '--yes', '--json']);
  assert.ok(done.calls.findIndex((c) => COMMENT(c.args)) < done.calls.indexOf(t), 'comment first — a failed transition must not lose it');

  // A failed or needs-human run never moves the ticket, whatever is configured.
  for (const status of ['failed', 'needs-human']) {
    const exec = fakeExec(routes);
    await createTaskSource(makeCtx(cfg), fakeDeps(exec).deps).reportResult('PROJ-7', { status, summary: 's', inputs: OPTED_IN });
    assert.equal(exec.calls.filter((c) => TRANSITION(c.args)).length, 0, status);
  }
});

test('reportResult: a run that did not opt in is NEVER written to, even if called', async () => {
  // The host skips on capabilities({inputs}) — but its probe defaults to
  // writeBack:true on a transport error, so the connector re-checks the run's
  // own choice.
  const exec = fakeExec([]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx({ transitionOnComplete: 'Done' }), deps);
  await src.reportResult('PROJ-7', { status: 'completed', summary: 's' });
  await src.reportResult('PROJ-7', { status: 'completed', summary: 's', inputs: { writeBack: 'no' } });
  assert.equal(exec.calls.length, 0, 'no comment, no transition');
});

test('reportResult: a failed transition keeps the comment and says how to find valid names', async () => {
  const exec = fakeExec([
    { match: COMMENT, reply: out({ ok: true }) },
    { match: TRANSITION, reply: jsonErr('jira_error', 1, 'No transition "Done" from status "Open".') },
  ]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx({ transitionOnComplete: 'Done' }), deps);
  await assert.rejects(() => src.reportResult('PROJ-7', { status: 'completed', summary: 's', inputs: { writeBack: 'yes' } }), (e) => {
    assert.equal(e.kind, 'plugin');
    assert.match(e.message, /comment posted, but the transition to "Done" failed/);
    assert.match(e.message, /jtr transition PROJ-7/);
    return true;
  });
  assert.equal(exec.calls.filter((c) => COMMENT(c.args)).length, 1, 'the comment went through before the transition failed');
});

test('reportResult: empty summary falls back to a one-liner; bad id -> plugin error, no exec', async () => {
  const exec = fakeExec([{ match: COMMENT, reply: out({ ok: true }) }]);
  const { deps } = fakeDeps(exec);
  const src = createTaskSource(makeCtx(), deps);
  await src.reportResult('PROJ-7', { status: 'failed', summary: '', inputs: { writeBack: 'yes' } });
  assert.equal(exec.calls[0].args[2], 'worca-cc run finished: failed');

  const noExec = fakeExec([]);
  await assert.rejects(
    () => createTaskSource(makeCtx(), fakeDeps(noExec).deps).reportResult('not a key', { status: 'completed', summary: 's', inputs: { writeBack: 'yes' } }),
    (e) => e.kind === 'plugin' && /bad Jira task id/.test(e.message),
  );
  assert.equal(noExec.calls.length, 0);
});

test('markdownToWiki: the result-summary dialect round-trips; plain text untouched', () => {
  assert.equal(markdownToWiki('### Title\n# Top'), 'h3. Title\nh1. Top');
  assert.equal(markdownToWiki('**bold** and `code`'), '*bold* and {{code}}');
  assert.equal(markdownToWiki('- [major] X (`a/b.mjs`)'), '* [major] X ({{a/b.mjs}})');
  assert.equal(markdownToWiki('[PR](https://x/pr/1)'), '[PR|https://x/pr/1]');
  assert.equal(markdownToWiki('```js\nx()\n```'), '{code:js}\nx()\n{code}');
  assert.equal(markdownToWiki('plain text stays'), 'plain text stays');
});

// ── profiles ───────────────────────────────────────────────────────────────────
// Two profiles = two Jira instances on one machine. Everything jtr owns —
// .env, the COOKIE JAR, the audit log — is keyed by $JTR_CONFIG_DIR, so if the
// profiles shared one directory they would share one session: whichever was
// init'd last would silently answer for both. That is precisely the
// wrong-tracker failure source-bindings.mjs exists to prevent, so it must not
// be reintroduced one layer down in the connector.
test('jtrHome: each profile owns a directory; no profile means "default"', () => {
  const a = jtrHome('acme');
  const b = jtrHome('globex');
  assert.notEqual(a, b, 'two profiles never share a config dir');
  assert.match(a, /plugins[/\\]jira-source[/\\]data[/\\]jtr-home[/\\]acme$/);
  assert.equal(jtrHome(), jtrHome('default'), 'absent profile is the default bucket');
  assert.match(jtrHome(), /jtr-home[/\\]default$/);
});

test('jtrHome: a profile id can never escape the plugin directory', () => {
  // The host validates profile ids ([a-z0-9-]) before they reach us, but this
  // string becomes a filesystem path — a traversal here would let a plugin
  // write outside its own data dir, so it is checked where the path is built.
  const base = jtrHome('default').replace(/default$/, '');
  for (const evil of ['../../etc', 'a/b', '/abs', '..']) {
    assert.ok(jtrHome(evil).startsWith(base), `"${evil}" stays under ${base}`);
    assert.ok(!jtrHome(evil).includes('..'), `"${evil}" leaves no traversal`);
  }
});

test('ssoLogPath: the detached login logs inside ITS OWN profile home', () => {
  // Shared, two concurrent sign-ins interleave into one file and each poll
  // reports the other's error.
  assert.equal(ssoLogPath('acme'), path.join(jtrHome('acme'), 'sso.log'));
  assert.notEqual(ssoLogPath('acme'), ssoLogPath('globex'));
});

test('the connector runs jtr against ctx.profile\'s home, not a shared one', async () => {
  const seen = { homes: [], logs: [] };
  const exec = fakeExec([
    { match: INIT, reply: initOut() },
    { match: WHOAMI, reply: out({ display_name: 'Jane Doe' }) },
  ]);
  const { deps } = fakeDeps(exec);
  const spyDeps = {
    ...deps,
    ensureHome: (p) => { seen.homes.push(p); return `/tmp/jtr-home/${p}`; },
    logPath: (p) => { seen.logs.push(p); return `/tmp/jtr-home/${p}/sso.log`; },
  };
  const ctx = { ...makeCtx({}, memState()), profile: 'acme' };
  await createTaskSource(ctx, spyDeps).validateConfig();

  assert.deepEqual([...new Set(seen.homes)], ['acme'], 'the home is the profile’s');
  // ...and it is what jtr actually received, not merely what was computed.
  const env = exec.calls[0].opts?.env || {};
  assert.equal(env.JTR_CONFIG_DIR, '/tmp/jtr-home/acme');
});

test('a connector with no ctx.profile still works (single-profile hosts)', async () => {
  const seen = [];
  const exec = fakeExec([
    { match: INIT, reply: initOut() },
    { match: WHOAMI, reply: out({ display_name: 'Jane Doe' }) },
  ]);
  const { deps } = fakeDeps(exec);
  const v = await createTaskSource(makeCtx({}, memState()), {
    ...deps,
    ensureHome: (p) => { seen.push(p); return '/tmp/jtr-home/default'; },
  }).validateConfig();
  assert.equal(v.ok, true);
  assert.deepEqual([...new Set(seen)], ['default'], 'falls back to the default bucket');
});

// ── pure helpers ───────────────────────────────────────────────────────────────
test('ticketKeyFrom: bare keys, browse URLs, and non-matches', () => {
  assert.equal(ticketKeyFrom('PROJ-123'), 'PROJ-123');
  assert.equal(ticketKeyFrom('  other-1  '), 'OTHER-1');
  assert.equal(ticketKeyFrom('https://x/jira/browse/OTHER-13727'), 'OTHER-13727');
  assert.equal(ticketKeyFrom('login bug'), null);
  assert.equal(ticketKeyFrom(''), null);
});

test('withDefaultOrder: sorts newest-first unless the JQL already says how', () => {
  // The complaint this fixes: an unsorted text search puts six-year-old
  // tickets at the top.
  assert.equal(withDefaultOrder('text ~ "login"'), 'text ~ "login" ORDER BY updated DESC');
  assert.equal(withDefaultOrder('key = ACME-1'), 'key = ACME-1 ORDER BY updated DESC');
  assert.equal(withDefaultOrder(''), '');
  // An explicit sort is never second-guessed, in either case.
  assert.equal(withDefaultOrder('project = X ORDER BY created ASC'), 'project = X ORDER BY created ASC');
  assert.equal(withDefaultOrder('project = X order by priority desc'), 'project = X order by priority desc');
});

test('jqlWithSearch: empty stays empty, passthrough without search, escaping, ORDER BY', () => {
  assert.equal(jqlWithSearch('', ''), '');                    // no filter invented
  assert.equal(jqlWithSearch('project = X', ''), 'project = X');
  assert.equal(jqlWithSearch('', 'kafka'), 'text ~ "kafka"');
  assert.equal(jqlWithSearch('project = X', 'a\\b'), '(project = X) AND text ~ "a\\\\b"');
  assert.equal(
    jqlWithSearch('project = X ORDER BY updated DESC', 'kafka'),
    '(project = X) AND text ~ "kafka" ORDER BY updated DESC',
  );
});

test('wikiToMarkdown: headings, code fence toggle, noformat, links; plain text untouched', () => {
  assert.equal(wikiToMarkdown('h1. Title\nh3. Sub'), '# Title\n### Sub');
  assert.equal(wikiToMarkdown('{code:java|title=X}\nx\n{code}'), '```java\nx\n```');
  assert.equal(wikiToMarkdown('{noformat}\nraw\n{noformat}'), '```\nraw\n```');
  assert.equal(wikiToMarkdown('see [site|https://x.y]'), 'see [site](https://x.y)');
  assert.equal(wikiToMarkdown('plain *text* stays'), 'plain *text* stays');
});
