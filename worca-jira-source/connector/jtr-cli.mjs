// examples/plugins/jira-source/connector/jtr-cli.mjs
// Subprocess layer for the jtr CLI (github-api.mjs analog). Every call goes
// through runJtr so JSON parsing + error mapping live in exactly one place.
// Errors carry a `kind` the shim child forwards verbatim into the protocol
// frame: auth | timeout | plugin.

import { execFile, spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// jtr codes that mean "credentials/session problem", regardless of exit code.
// `unsupported_deployment` is deliberately NOT here: since jtr 0.10 it means
// "this URL is not a Jira I speak", which is a configuration error. Calling it
// auth would send the connector off to launch a browser login — the one remedy
// guaranteed not to help.
const AUTH_CODES = new Set(['not_authenticated']);

function err(kind, message, extra = {}) {
  return Object.assign(new Error(message), { kind, ...extra });
}

/**
 * One path segment for a profile id. The host validates ids before they ever
 * reach a connector, but this value becomes a filesystem path, and a traversal
 * would let the plugin write outside its own data directory — so it is enforced
 * at the point the path is built rather than trusted from upstream.
 */
function profileSegment(profile) {
  const p = String(profile || '').trim().toLowerCase();
  const safe = p.replace(/[^a-z0-9-]/g, '');
  return safe || 'default';
}

/**
 * The jtr config directory this plugin owns FOR ONE PROFILE — .env, cookies and
 * audit log all live directly inside it. Passed to jtr as $JTR_CONFIG_DIR
 * (jtr >= 0.9.0), which names the config dir outright and beats both ./.jtr/
 * and ~/.jtr/, so nothing depends on the working directory the worca server
 * happens to have. ctx carries no filesystem paths, so the host layout is
 * reconstructed from HOME; under a custom WORCA_HOME this simply lives at the
 * default path.
 *
 * Per PROFILE, not per plugin: two profiles are two Jira instances, and jtr
 * keys its whole session — including the cookie jar — off this directory. Share
 * it and the second `jtr init` overwrites the first instance's config while its
 * cookies stay behind, so one profile silently answers with the other's tickets.
 */
export function jtrHome(profile) {
  return path.join(os.homedir(), '.worca-cc', 'plugins', 'jira-source', 'data', 'jtr-home', profileSegment(profile));
}

/** Child env: the shim leaves us PATH+HOME, and we pin jtr's state on top. */
export function jtrEnv(configDir) {
  return { ...process.env, JTR_CONFIG_DIR: configDir };
}

export function ensureHome(profile) {
  const dir = jtrHome(profile);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  return dir;
}

export function ssoLogPath(profile) {
  return path.join(jtrHome(profile), 'sso.log');
}

/**
 * Run jtr once. Array argv, no shell — JQL is never shell-interpreted.
 * Never rejects; the outcome is a plain result object for runJtr to map.
 * 20s default stays under the shim's 30s SIGKILL so a hung jtr surfaces as
 * our `timeout` kind, not a host protocol error.
 * @returns {Promise<{code: number, enoent: boolean, timedOut: boolean, stdout: string, stderr: string}>}
 */
export function execJtr(cmd, args, { timeoutMs = 20000, env } = {}) {
  return new Promise((resolve) => {
    execFile(
      cmd,
      args,
      { timeout: timeoutMs, killSignal: 'SIGKILL', maxBuffer: 8 * 1024 * 1024, windowsHide: true, env },
      (error, stdout, stderr) => resolve({
        code: error ? (typeof error.code === 'number' ? error.code : 1) : 0,
        enoent: error?.code === 'ENOENT',
        timedOut: Boolean(error?.killed || error?.signal),
        stdout: String(stdout || ''),
        stderr: String(stderr || ''),
      }),
    );
  });
}

/**
 * Launch the interactive SSO login and return immediately.
 *
 * `jtr auth sso` drives a real browser and waits for a human — minutes, where
 * the shim SIGKILLs any op at 30s. `detached` puts the login in its OWN
 * process group so it survives that kill and finishes the handshake on its
 * own; the next validateConfig poll then sees the cookies. Output is appended
 * to sso.log rather than discarded, so a failed launch ("No installed browser
 * found") is still reportable in the UI on the next poll.
 */
export function startSsoLogin(jtrPath, { env, logPath, browser } = {}) {
  const args = ['auth', 'sso', '--force'];
  if (browser && browser !== 'auto') args.push('--browser', browser);
  const fd = fs.openSync(logPath, 'a');
  try {
    const child = spawn(jtrPath, args, { env, detached: true, stdio: ['ignore', fd, fd] });
    // spawn reports ENOENT asynchronously via 'error'; unhandled it would crash
    // the shim child. Route it into the log — the next poll reports it in the UI.
    child.on('error', (e) => {
      try { fs.appendFileSync(logPath, `failed to launch ${jtrPath}: ${e.message}\n`); } catch { /* best effort */ }
    });
    child.unref();
  } finally {
    fs.closeSync(fd);
  }
}

/** Tail of the detached login's output — the only feedback channel it has. */
export function readSsoLog(logPath, max = 400) {
  try {
    const text = fs.readFileSync(logPath, 'utf8').trim();
    return text.length > max ? text.slice(-max) : text;
  } catch {
    return '';
  }
}

export function clearSsoLog(logPath) {
  try { fs.rmSync(logPath, { force: true }); } catch { /* best effort */ }
}

/**
 * Run jtr and return its parsed --json payload.
 * @param {Function} exec injected execJtr (tests pass a fake)
 * @param {string} jtrPath command or absolute path to jtr
 * @param {string[]} args argv, e.g. ['view', 'PROJ-1', '--json']
 * @param {{expectJson?: boolean}} [opts] expectJson:false for the setup
 *   commands — `jtr init` has NO --json flag (unlike every read command) and
 *   prints human text to stdout, so demanding JSON there is a category error.
 *   Failures are still mapped: they exit non-zero with the reason on stderr.
 * jtr emits JSON on stdout even on failure ({error, message, fix?} with a
 * non-zero exit) — map its stable error codes onto shim kinds.
 */
export async function runJtr(exec, jtrPath, args, opts = {}) {
  const { expectJson = true } = opts;
  const r = await exec(jtrPath, args, opts);
  if (r.enoent) {
    throw err('plugin', `jtr not found at "${jtrPath}" — install jtr or set the "jtrPath" config field to its absolute path`, { code: 'jtr_missing' });
  }
  if (r.timedOut) throw err('timeout', `jtr ${args[0]} timed out`);
  let json = null;
  try { json = JSON.parse(r.stdout); } catch { /* mapped below */ }
  if (r.code !== 0) {
    if (json?.error) {
      const msg = json.fix ? `${json.message} — fix: ${json.fix}` : String(json.message || json.error);
      throw err(AUTH_CODES.has(json.error) ? 'auth' : 'plugin', msg, { code: json.error });
    }
    const detail = r.stderr || r.stdout;
    throw err('plugin', `jtr ${args.join(' ')} exited ${r.code}${detail ? ` — ${detail.trim().slice(0, 300)}` : ''}`);
  }
  // expectJson:false tolerates human output but still hands back anything that
  // DID parse — so a future jtr that grows `init --json` is picked up with no
  // change here, and 0.8.0 (text only) keeps working. json is null on 0.8.0.
  if (!expectJson) return { text: r.stdout, json };
  if (json === null) throw err('plugin', `jtr ${args[0]} emitted non-JSON output: ${r.stdout.slice(0, 200)}`);
  return json;
}
