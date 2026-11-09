// Starts and stops the real stack for the e2e specs: llama-server (once, in global setup) and the backend (per spec
// file, so a spec can restart it). Settings come from backend/.env like start.ps1; uploads and labels go to
// logs/e2e/data so the real uploads folder is never swept.
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const E2E_DIR = path.join(ROOT, 'logs', 'e2e');
export const API_PORT = 8000;
const STATE_FILE = path.join(E2E_DIR, 'llama.pid');

export const readEnvFile = () => {
  const out = {};
  const file = path.join(ROOT, 'backend', '.env');
  if (!fs.existsSync(file)) return out;
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z_0-9]+)\s*=\s*(.*?)\s*$/);
    if (m) out[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
  return out;
};
const setting = (key, fallback) => process.env[key] || readEnvFile()[key] || fallback;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = async (url) => {
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(3000) });
    return { code: r.status, body: await r.text() };
  } catch { return null; }
};

export const killTree = (pid) => {
  try { execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }); } catch { /* already gone */ }
};

const llamaRoot = () => setting('LLM_BASE_URL', 'http://127.0.0.1:8080/v1').replace(/\/v1\/?$/, '');

const llamaReady = async () => {
  const h = await get(`${llamaRoot()}/health`);
  if (!h || h.code !== 200) return false;
  const m = await get(`${llamaRoot()}/v1/models`);
  return !!m && m.code === 200;
};

// Starts llama-server unless one is already healthy at LLM_BASE_URL. Returns true when this call started it.
export const ensureLlama = async () => {
  fs.mkdirSync(E2E_DIR, { recursive: true });
  if (await llamaReady()) return false;
  const exe = setting('LLAMA_SERVER_PATH');
  const model = setting('LLM_MODEL_PATH');
  const mmproj = setting('LLM_MMPROJ_PATH');
  for (const [name, p] of [['LLAMA_SERVER_PATH', exe], ['LLM_MODEL_PATH', model], ['LLM_MMPROJ_PATH', mmproj]]) {
    if (!p || !fs.existsSync(p)) throw new Error(`${name} is not set or missing in backend/.env (${p})`);
  }
  const u = new URL(llamaRoot());
  const log = fs.openSync(path.join(E2E_DIR, 'llama-server.log'), 'w');
  const child = spawn(exe, ['-m', model, '--mmproj', mmproj, '-c', setting('LLM_CTX', '8192'), '-np', '1', '-ngl', '99',
    '--host', u.hostname, '--port', u.port, '--jinja'], { stdio: ['ignore', log, log], windowsHide: true, detached: true });
  child.unref();
  fs.writeFileSync(STATE_FILE, String(child.pid));
  const deadline = Date.now() + 10 * 60 * 1000;
  while (!(await llamaReady())) {
    if (Date.now() > deadline) throw new Error('llama-server did not become healthy in 10 minutes');
    await sleep(2000);
  }
  return true;
};

export const stopLlama = () => {
  if (!fs.existsSync(STATE_FILE)) return;
  killTree(Number(fs.readFileSync(STATE_FILE, 'utf8')));
  fs.rmSync(STATE_FILE, { force: true });
};

let backend = null;

export const startBackend = async (extraEnv = {}) => {
  if (await get(`http://127.0.0.1:${API_PORT}/api/styles`)) throw new Error(`Something already answers on port ${API_PORT}. Stop it first.`);
  const data = path.join(E2E_DIR, 'data');
  fs.mkdirSync(path.join(data, 'uploads'), { recursive: true });
  const log = fs.openSync(path.join(E2E_DIR, 'backend.log'), 'a');
  const python = path.join(ROOT, 'backend', 'venv', 'Scripts', 'python.exe');
  backend = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--port', String(API_PORT)], {
    cwd: path.join(ROOT, 'backend'), stdio: ['ignore', log, log], windowsHide: true,
    env: { ...process.env, ...extraEnv, UPLOAD_DIR: path.join(data, 'uploads'), LABELS_DIR: path.join(data, 'labels') },
  });
  const deadline = Date.now() + 60 * 1000;
  while (!(await get(`http://127.0.0.1:${API_PORT}/api/styles`))) {
    if (backend.exitCode !== null || Date.now() > deadline) throw new Error('backend did not start (see logs/e2e/backend.log)');
    await sleep(500);
  }
};

export const stopBackend = async () => {
  if (backend && backend.exitCode === null) killTree(backend.pid);
  backend = null;
  for (let i = 0; i < 40 && (await get(`http://127.0.0.1:${API_PORT}/api/styles`)); i += 1) await sleep(250);
};
