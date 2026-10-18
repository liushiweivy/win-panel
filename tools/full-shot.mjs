/* 一次性全页截图：独立 headless Edge + captureBeyondViewport */
import { spawn } from 'node:child_process';
import { mkdirSync, existsSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const URL = 'http://127.0.0.1:8830/';
const CDP_PORT = 9342;
const edge = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].find(existsSync);
const sleep = ms => new Promise(r => setTimeout(r, ms));

const profileDir = join(ROOT, 'tools', '.shot-profile');
rmSync(profileDir, { recursive: true, force: true });
mkdirSync(profileDir, { recursive: true });
const browser = spawn(edge, [
  '--headless=new', '--no-first-run', '--no-sandbox',
  '--remote-debugging-port=' + CDP_PORT,
  '--user-data-dir=' + profileDir,
  '--window-size=1500,1000', URL,
]);

async function cdpJson(p) { return (await fetch(`http://127.0.0.1:${CDP_PORT}${p}`)).json(); }
let target;
for (let i = 0; i < 60 && !target; i++) {
  try { target = (await cdpJson('/json')).find(t => t.type === 'page' && t.url.startsWith(URL)); } catch {}
  if (!target) await sleep(250);
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
let mid = 0; const pend = new Map();
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++mid; pend.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params }));
});
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); p.res(m.result); }
});
await new Promise(r => ws.addEventListener('open', r));
await send('Page.enable');
await sleep(2000);
const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
writeFileSync(join(ROOT, 'tools', 'full-shot.png'), Buffer.from(shot.data, 'base64'));
console.log('saved tools/full-shot.png');
ws.close(); browser.kill();
process.exit(0);
