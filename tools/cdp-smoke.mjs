/*
 * win-panel 前端冒烟：独立 headless Edge（独立 user-data-dir，不碰共用浏览器）
 * 只做零副作用验证：加载页面、只读状态、纯 UI 切换、前端校验拦截；
 * 绝不点击 定时/电源/启动/维护/快捷键 按钮（快捷键是真实按键，会影响桌面）。
 *   node tools/cdp-smoke.mjs
 */
import { spawn } from 'node:child_process';
import { mkdirSync, existsSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const URL = 'http://127.0.0.1:8830/';
const CDP_PORT = 9341;
const EDGE_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
];
const edge = EDGE_CANDIDATES.find(existsSync);
if (!edge) { console.error('no Edge/Chrome found'); process.exit(2); }

const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log('[smoke]', ...a);

const profileDir = join(ROOT, 'tools', '.cdp-profile');
rmSync(profileDir, { recursive: true, force: true });
mkdirSync(profileDir, { recursive: true });

const browser = spawn(edge, [
  '--headless=new', '--no-first-run', '--no-sandbox',
  '--remote-debugging-port=' + CDP_PORT,
  '--user-data-dir=' + profileDir,
  '--window-size=1440,1000',
  URL,
]);

async function cdpJson(path) {
  const r = await fetch(`http://127.0.0.1:${CDP_PORT}${path}`);
  return r.json();
}
for (let i = 0; i < 40; i++) {
  try { await cdpJson('/json/version'); break; } catch { await sleep(250); }
}
let target;
for (let i = 0; i < 40 && !target; i++) {
  const list = await cdpJson('/json');
  target = list.find(t => t.type === 'page' && t.url.startsWith(URL));
  if (!target) await sleep(250);
}
if (!target) { console.error('no page target'); process.exit(3); }

const ws = new WebSocket(target.webSocketDebuggerUrl);
let mid = 0;
const pend = new Map();
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++mid;
    pend.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
const consoleErrors = [];
ws.addEventListener('message', ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) {
    const p = pend.get(m.id); pend.delete(m.id);
    if (m.error) p.reject(new Error(JSON.stringify(m.error))); else p.resolve(m.result);
  }
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
    consoleErrors.push(m.params.args.map(a => a.value || a.description || a.type).join(' '));
  }
  if (m.method === 'Runtime.exceptionThrown') {
    consoleErrors.push('EXCEPTION: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
  }
});
await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
await send('Runtime.enable');
await send('Page.enable');
await sleep(1800);

const q = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value;

const checks = [];
function check(name, cond) { checks.push([name, !!cond]); log((cond ? 'PASS' : 'FAIL'), name); }

// 1. 只读状态渲染
check('title 正确', await q(`document.title === '系统控制台 · win-panel'`));
check('主机名已填充', (await q(`document.getElementById('host').textContent`)) !== '—');
check('用户名已填充', (await q(`document.getElementById('user').textContent`)) !== '—');
check('开机时长已渲染', /已开机/.test(await q(`document.getElementById('uptime').textContent`)));
check('时钟在走', /^\d{2}:\d{2}:\d{2}$/.test(await q(`document.getElementById('clock').textContent`)));

// 2. 按钮齐全
check('6 个定时预设', await q(`document.querySelectorAll('#presets button').length`) === 6);
check('6 个电源按钮', await q(`document.querySelectorAll('[data-act]').length`) === 9); // 6 电源 + 3 维护
check('13 个静态快捷启动（含环境变量）', await q(`document.querySelectorAll('#agrid .abtn').length`) === 13);

// 2b. 自定义链接渲染与服务端一致（links.json 默认播种）
const linkCounts = (await send('Runtime.evaluate', {
  expression: `fetch('/api/links').then(r=>r.json()).then(d=>[d.folders.length, d.sites.length])`,
  returnByValue: true, awaitPromise: true,   // q() 不带 awaitPromise，Promise 会拿到 undefined
})).result.value;
check('文件夹按钮数与服务端一致', await q(`document.querySelectorAll('#fgrid .abtn.link').length`) === linkCounts[0]);
check('网站按钮数与服务端一致', await q(`document.querySelectorAll('#wgrid .abtn.link').length`) === linkCounts[1]);
check('链接条目带删除钮', await q(`document.querySelectorAll('#fgrid .abtn.link .del').length`) === linkCounts[0]);

// 2c. 非法网址被服务端拒绝（不产生条目）
await q(`var n=document.getElementById('wName'),u=document.getElementById('wUrl'); n.value='evil'; u.value='javascript:alert(1)'; document.getElementById('wAdd').click(); 'ok'`);
await sleep(500);
check('javascript: 协议被拦截', /http/.test(await q(`document.getElementById('toast').textContent`)));
check('拦截后网站数不变', await q(`document.querySelectorAll('#wgrid .abtn.link').length`) === linkCounts[1]);

// 2d. 文件夹添加→删除 闭环（净零副作用：加了再删）
await q(`var n=document.getElementById('fName'),p=document.getElementById('fPath'); n.value='__smoke__'; p.value='E:\\\\1workspace'; document.getElementById('fAdd').click(); 'ok'`);
await sleep(600);
check('添加后文件夹数 +1', await q(`document.querySelectorAll('#fgrid .abtn.link').length`) === linkCounts[0] + 1);
await q(`var b=document.querySelector('#fgrid .abtn.link[data-name="__smoke__"]'); b ? b.querySelector('.del').click() : null; 'ok'`);
await sleep(600);
check('删除后恢复原数', await q(`document.querySelectorAll('#fgrid .abtn.link').length`) === linkCounts[0]);
check('34 个 Win 快捷键按钮', await q(`document.querySelectorAll('.hkbtn').length`) === 34);
check('5 个快捷键分组', await q(`document.querySelectorAll('.hksub').length`) === 5);
check('每个快捷键都有 kbd 键面', await q(`document.querySelectorAll('.hkbtn kbd').length`) === 34);
check('快捷键键面文本正确', (await q(`document.querySelector('[data-hk="gamerec"] kbd').textContent`)) === 'Win+Alt+R');

// 3. 纯 UI：切到重启
await q(`document.querySelector('#seg button[data-kind="restart"]').click()`);
check('重启分段高亮', await q(`document.querySelector('#seg button[data-kind="restart"]').classList.contains('on')`));
check('计划按钮变红', await q(`document.getElementById('btnPlan').classList.contains('red')`));
await q(`document.querySelector('#seg button[data-kind="shutdown"]').click()`);

// 4. 前端校验：非法分钟被本地拦截（不产生计划）
await q(`var i=document.getElementById('customMin'); i.value='99999'; document.getElementById('btnPlan').click(); 'ok'`);
await sleep(300);
check('非法输入弹错误 toast', /1.?1440/.test(await q(`document.getElementById('toast').textContent`)));
check('待计划区仍隐藏', !await q(`document.getElementById('pending').classList.contains('show')`));

// 5. 初始无本面板计划
check('无 pending 时倒计时区隐藏', !await q(`document.getElementById('pending').classList.contains('show')`));

// 6. 控制台零错误
check('无 console error / 异常', consoleErrors.length === 0);
if (consoleErrors.length) log('errors:', consoleErrors);

const shot = await send('Page.captureScreenshot', { format: 'png' });
const out = join(ROOT, 'tools', 'smoke-shot.png');
const { writeFileSync } = await import('node:fs');
writeFileSync(out, Buffer.from(shot.data, 'base64'));
log('screenshot:', out);

const failed = checks.filter(c => !c[1]).length;
ws.close(); browser.kill();
await sleep(300);
process.exit(failed ? 1 : 0);
