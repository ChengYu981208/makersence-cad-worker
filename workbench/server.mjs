// MAKERSENSE 工作台 — 本機伺服器（零依賴）
// 只在 127.0.0.1 運作；呼叫本機已登入的 Claude Code（claude）與 Codex CLI（codex），都用月費帳號，不用付費 API。
import http from 'node:http';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { spawn, execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { extractZip, zipFolder, zipTopFolder } from './lib/zip.mjs';

const APP_DIR = path.dirname(fileURLToPath(import.meta.url));
const HOME = os.homedir();
const IS_WIN = process.platform === 'win32';
const PORT = Number(process.env.MS_WB_PORT || 5888);
const APP_VERSION = '2026.11.10d';
const CLOUD = process.env.MS_WB_CLOUD === '1';
// GitHub Codespaces：連接埠是「私人」的，只有登入你 GitHub 帳號的人才進得來，所以不用另外設密碼
const CODESPACES = process.env.CODESPACES === 'true';
const CS_ORIGIN = CODESPACES && process.env.CODESPACE_NAME ? `https://${process.env.CODESPACE_NAME}-${process.env.MS_WB_PORT || 5888}.${process.env.GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN || 'app.github.dev'}` : null;
const DATA_DIR = process.env.MS_WB_DATA || path.join(HOME, '.makersense-workbench');
const THREADS_DIR = path.join(DATA_DIR, 'threads');
const CONFIG_FILE = path.join(DATA_DIR, 'config.json');
const LOG_FILE = path.join(DATA_DIR, 'server.log');
fs.mkdirSync(THREADS_DIR, { recursive: true });

const log = (...a) => { const s = `[${new Date().toISOString()}] ${a.join(' ')}\n`; try { fs.appendFileSync(LOG_FILE, s); } catch {} if (process.env.MS_WB_DEBUG) process.stdout.write(s); };

// ---------------- 設定 ----------------
const DEFAULT_CONFIG = {
  root: process.env.MS_WB_ROOT || (fs.existsSync('D:\\FDM\\Claude') ? 'D:\\FDM\\Claude' : path.join(HOME, 'MAKERSENSE')),
  theme: 'light',
  claudePermission: 'bypassPermissions',          // bypassPermissions = 可執行指令（全自動）；acceptEdits = 只讀寫檔案
  claudeModels: [
    { id: '', label: '預設模型' },
    { id: 'opus', label: 'Opus（最強）' },
    { id: 'sonnet', label: 'Sonnet（平衡）' },
    { id: 'haiku', label: 'Haiku（最快）' },
  ],
  codexModels: [
    { id: '', label: '預設模型' },
  ],
  codexEfforts: ['', 'low', 'medium', 'high', 'xhigh'],
  last: { claudeModel: '', codexModel: '', codexEffort: '', mode: 'claude', rounds: 1 },
  remote: { enabled: false, autostart: false },
  auth: null,
  claudeToken: null,
};
function loadConfig() {
  try { return { ...DEFAULT_CONFIG, ...JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8')) }; } catch { return { ...DEFAULT_CONFIG }; }
}
let config = loadConfig();
const saveConfig = () => fs.writeFileSync(CONFIG_FILE, JSON.stringify(config, null, 2));
if (!fs.existsSync(CONFIG_FILE)) saveConfig();

// Codex 也遵守同一套規則：第一次啟動時，若沒有 ~/.codex/AGENTS.md，複製 Claude 的全域規則過去
try {
  const agents = path.join(HOME, '.codex', 'AGENTS.md');
  const claudeGlobal = path.join(HOME, '.claude', 'CLAUDE.md');
  if (!fs.existsSync(agents) && fs.existsSync(claudeGlobal)) {
    fs.mkdirSync(path.dirname(agents), { recursive: true });
    fs.copyFileSync(claudeGlobal, agents);
    log('copied global rules to', agents);
  }
} catch (e) { log('AGENTS.md copy failed', e.message); }

// ---------------- 小工具 ----------------
function agentEnv() {
  const env = { ...process.env };
  if (config.claudeToken) env.CLAUDE_CODE_OAUTH_TOKEN = config.claudeToken;  // 雲端：用 setup-token 登入時
  return env;
}
const uid = (n = 10) => crypto.randomBytes(n).toString('base64url').slice(0, n);
const now = () => new Date().toISOString();
const safeName = (s) => String(s || '').replace(/[<>:"/\\|?*\x00-\x1f]/g, '').trim().slice(0, 80);
function inside(base, p) {
  const r = path.resolve(base, p);
  const b = path.resolve(base);
  return (r === b || r.startsWith(b + path.sep)) ? r : null;
}
function projectDir(name) {
  const n = safeName(name);
  if (!n) return null;
  return inside(config.root, n);
}
function winQuote(a) {
  const s = String(a);
  if (!IS_WIN) return s;
  if (/^[A-Za-z0-9_\-.,:=\/@+]+$/.test(s)) return s;
  return '"' + s.replace(/"/g, '""') + '"';
}
function spawnCli(cmd, args, opts) {
  // Windows 上 claude / codex 可能是 .cmd，需要 shell；所有使用者文字都走 stdin，不放在參數裡
  if (IS_WIN) return spawn(/\s/.test(cmd) ? `"${cmd}"` : cmd, args.map(winQuote), { ...opts, shell: true, windowsHide: true });
  return spawn(cmd, args, { ...opts });
}
function killTree(child) {
  if (!child || child.exitCode !== null) return;
  try {
    if (IS_WIN) execFile('taskkill', ['/pid', String(child.pid), '/T', '/F'], () => {});
    else child.kill('SIGTERM');
  } catch {}
}
function runQuick(cmd, args, timeout = 15000) {
  return new Promise((resolve) => {
    let out = '', err = '';
    let p;
    try { p = spawnCli(cmd, args, { cwd: HOME, env: process.env }); } catch (e) { return resolve({ ok: false, out: '', err: e.message, code: -1 }); }
    const t = setTimeout(() => killTree(p), timeout);
    p.stdout.on('data', (d) => out += d);
    p.stderr.on('data', (d) => err += d);
    p.on('error', (e) => { clearTimeout(t); resolve({ ok: false, out, err: e.message, code: -1 }); });
    p.on('close', (code) => { clearTimeout(t); resolve({ ok: code === 0, out: out.trim(), err: err.trim(), code }); });
    try { p.stdin.end(); } catch {}
  });
}

// ---------------- 紀錄（每個對話一個 JSON） ----------------
const threadPath = (id) => path.join(THREADS_DIR, `${id.replace(/[^A-Za-z0-9_-]/g, '')}.json`);
function readThread(id) { try { return JSON.parse(fs.readFileSync(threadPath(id), 'utf8')); } catch { return null; } }
function writeThread(t) { t.updated = now(); fs.writeFileSync(threadPath(t.id), JSON.stringify(t, null, 1)); }
function listThreads(project) {
  const out = [];
  for (const f of fs.readdirSync(THREADS_DIR)) {
    if (!f.endsWith('.json')) continue;
    try {
      const t = JSON.parse(fs.readFileSync(path.join(THREADS_DIR, f), 'utf8'));
      if (project && t.project !== project) continue;
      const lastMsg = t.messages[t.messages.length - 1];
      out.push({ id: t.id, project: t.project, title: t.title, created: t.created, updated: t.updated, count: t.messages.length,
        preview: lastMsg ? String(lastMsg.text || '').replace(/\s+/g, ' ').slice(0, 80) : '' });
    } catch {}
  }
  return out.sort((a, b) => (b.updated || '').localeCompare(a.updated || ''));
}

// ---------------- 即時事件（SSE） ----------------
const subscribers = new Map(); // threadId -> Set(res)
function emit(threadId, ev) {
  const set = subscribers.get(threadId);
  if (!set) return;
  const data = `data: ${JSON.stringify(ev)}\n\n`;
  for (const res of set) { try { res.write(data); } catch {} }
}
const running = new Map(); // threadId -> { children:Set, stopped:boolean }

// ---------------- Claude Code ----------------
function summarizeToolInput(name, input = {}) {
  const pick = input.file_path || input.path || input.pattern || input.command || input.url || input.query || input.description || input.skill || '';
  return String(pick).replace(/\s+/g, ' ').slice(0, 160);
}
function runClaude(thread, msg, prompt, opts) {
  return new Promise((resolve) => {
    const dir = projectDir(thread.project);
    const args = ['-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
      '--permission-mode', config.claudePermission === 'acceptEdits' ? 'acceptEdits' : 'bypassPermissions'];
    if (opts.claudeModel) args.push('--model', opts.claudeModel);
    if (thread.claudeSession) args.push('--resume', thread.claudeSession);
    log('claude', args.join(' '), 'cwd=', dir);
    let child;
    try { child = spawnCli('claude', args, { cwd: dir, env: agentEnv() }); }
    catch (e) { msg.status = 'error'; msg.text += `\n\n無法啟動 Claude Code：${e.message}`; return resolve(); }
    running.get(thread.id)?.children.add(child);
    let buf = '', stderr = '', gotDelta = false, segText = '', started = Date.now();
    const toolIndex = new Map();
    const addText = (t) => { msg.text += t; emit(thread.id, { kind: 'delta', msgId: msg.id, text: t }); };
    const handle = (ev) => {
      if (ev.type === 'system' && ev.subtype === 'init') {
        if (ev.session_id) thread.claudeSession = ev.session_id;
        if (ev.model) { msg.model = ev.model; emit(thread.id, { kind: 'meta', msgId: msg.id, model: ev.model }); }
      } else if (ev.type === 'stream_event' && ev.event) {
        const e = ev.event;
        if (e.type === 'message_start') { gotDelta = false; if (msg.text && !msg.text.endsWith('\n\n')) addText('\n\n'); }
        if (e.type === 'content_block_delta' && e.delta?.type === 'text_delta') { gotDelta = true; addText(e.delta.text); }
      } else if (ev.type === 'assistant' && ev.message?.content) {
        for (const b of ev.message.content) {
          if (b.type === 'tool_use') {
            const tool = { id: b.id, name: b.name, detail: summarizeToolInput(b.name, b.input), status: 'running' };
            toolIndex.set(b.id, tool); msg.tools.push(tool);
            emit(thread.id, { kind: 'tool', msgId: msg.id, tool });
          } else if (b.type === 'text' && !gotDelta && b.text) {
            if (msg.text && !msg.text.endsWith('\n\n')) addText('\n\n');
            addText(b.text);
          }
        }
      } else if (ev.type === 'user' && ev.message?.content) {
        for (const b of ev.message.content) {
          if (b.type === 'tool_result' && toolIndex.has(b.tool_use_id)) {
            const tool = toolIndex.get(b.tool_use_id); tool.status = b.is_error ? 'error' : 'done';
            emit(thread.id, { kind: 'tool', msgId: msg.id, tool });
          }
        }
      } else if (ev.type === 'result') {
        if (ev.session_id) thread.claudeSession = ev.session_id;
        if (!msg.text.trim() && ev.result) addText(String(ev.result));
        msg.meta = { durationMs: ev.duration_ms ?? (Date.now() - started), turns: ev.num_turns, usage: ev.usage || null };
        if (ev.is_error) { msg.status = 'error'; if (ev.result && !msg.text.includes(ev.result)) addText(`\n\n${ev.result}`); }
      }
    };
    child.stdout.on('data', (d) => {
      buf += d.toString('utf8');
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line) continue;
        try { handle(JSON.parse(line)); } catch { /* 非 JSON 行忽略 */ }
      }
    });
    child.stderr.on('data', (d) => { stderr += d.toString('utf8'); });
    child.on('error', (e) => { stderr += e.message; });
    child.on('close', (code) => {
      running.get(thread.id)?.children.delete(child);
      if (buf.trim()) { try { handle(JSON.parse(buf.trim())); } catch {} }
      if (code !== 0 && !msg.text.trim() && !running.get(thread.id)?.stopped) {
        msg.status = 'error';
        addText(/ENOENT|not recognized|不是內部或外部命令/i.test(stderr) ? '找不到 claude 指令。請到右側「環境」頁檢查 Claude Code 是否已安裝。' : `Claude Code 沒有回應（代碼 ${code}）。\n\n${stderr.slice(-1500) || '請到右側「環境」頁檢查 Claude Code 是否已安裝並登入。'}`);
      }
      if (msg.status === 'streaming') msg.status = running.get(thread.id)?.stopped ? 'stopped' : 'done';
      resolve();
    });
    child.stdin.end(prompt, 'utf8');
  });
}

// ---------------- Codex CLI ----------------
function runCodex(thread, msg, prompt, opts, images = []) {
  return new Promise((resolve) => {
    const dir = projectDir(thread.project);
    // 新舊版 Codex 都通用的寫法（新版已移除 --full-auto）：自動執行、可寫產品資料夾、可上網裝套件
    const common = ['--json', '--skip-git-repo-check', '-c', 'approval_policy=never', '-c', 'sandbox_mode=workspace-write', '-c', 'sandbox_workspace_write.network_access=true'];
    if (opts.codexModel) common.push('-m', opts.codexModel);
    if (opts.codexEffort) common.push('-c', `model_reasoning_effort=${opts.codexEffort}`);
    for (const im of images) common.push('-i', im);
    const tryResume = !!thread.codexThread;
    const args = tryResume ? ['exec', 'resume', ...common, thread.codexThread, '-'] : ['exec', ...common, '-'];
    log('codex', args.join(' '), 'cwd=', dir);
    let child;
    try { child = spawnCli('codex', args, { cwd: dir, env: agentEnv() }); }
    catch (e) { msg.status = 'error'; msg.text += `\n\n無法啟動 Codex：${e.message}`; return resolve(); }
    running.get(thread.id)?.children.add(child);
    let buf = '', stderr = '', raw = '', sawEvent = false, started = Date.now();
    const items = new Map();
    const addText = (t) => { msg.text += t; emit(thread.id, { kind: 'delta', msgId: msg.id, text: t }); };
    const toolOf = (item) => {
      const t = item.type;
      if (t === 'command_execution') return { name: '執行指令', detail: String(item.command || '').slice(0, 160) };
      if (t === 'file_change') return { name: '修改檔案', detail: (item.changes || []).map((c) => c.path).join('、').slice(0, 160) };
      if (t === 'web_search') return { name: '網路搜尋', detail: String(item.query || '').slice(0, 160) };
      if (t === 'mcp_tool_call') return { name: '工具', detail: `${item.server || ''} ${item.tool || ''}`.trim() };
      if (t === 'reasoning') return { name: '思考', detail: String(item.text || '').replace(/\s+/g, ' ').slice(0, 160) };
      if (t === 'todo_list' || t === 'plan_update') return { name: '規劃', detail: (item.items || []).map((x) => x.text).join('／').slice(0, 160) };
      return null;
    };
    const handle = (ev) => {
      sawEvent = true;
      if (ev.type === 'thread.started' && ev.thread_id) thread.codexThread = ev.thread_id;
      else if ((ev.type === 'item.started' || ev.type === 'item.updated' || ev.type === 'item.completed') && ev.item) {
        const it = ev.item;
        if (it.type === 'agent_message') {
          if (ev.type === 'item.completed' && it.text) { if (msg.text && !msg.text.endsWith('\n\n')) addText('\n\n'); addText(it.text); }
          return;
        }
        const base = toolOf(it);
        if (!base) return;
        let tool = items.get(it.id);
        if (!tool) { tool = { id: it.id, ...base, status: 'running' }; items.set(it.id, tool); msg.tools.push(tool); }
        else Object.assign(tool, base);
        if (ev.type === 'item.completed') tool.status = (it.status === 'failed' || (it.exit_code && it.exit_code !== 0)) ? 'error' : 'done';
        emit(thread.id, { kind: 'tool', msgId: msg.id, tool });
      } else if (ev.type === 'turn.completed') {
        msg.meta = { durationMs: Date.now() - started, usage: ev.usage || null };
      } else if (ev.type === 'turn.failed' || ev.type === 'error') {
        msg.status = 'error';
        addText(`\n\nCodex 錯誤：${ev.message || ev.error?.message || JSON.stringify(ev).slice(0, 400)}`);
      }
    };
    child.stdout.on('data', (d) => {
      buf += d.toString('utf8');
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line) continue;
        let ev = null; try { ev = JSON.parse(line); } catch { raw += line + '\n'; }
        if (ev) handle(ev);
      }
    });
    child.stderr.on('data', (d) => { stderr += d.toString('utf8'); });
    child.on('error', (e) => { stderr += e.message; });
    child.on('close', async (code) => {
      running.get(thread.id)?.children.delete(child);
      if (buf.trim()) { try { handle(JSON.parse(buf.trim())); } catch {} }
      if (tryResume && !sawEvent && code === 0 && raw.trim()) {
        // 某些 Codex 版本續接時不輸出 JSON：直接把文字當回覆
        addText(raw.trim());
      } else if (tryResume && !sawEvent && !running.get(thread.id)?.stopped) {
        // 舊的 Codex 對話接不上：開新的，並附上近期對話紀錄
        log('codex resume failed, starting fresh', stderr.slice(-300));
        thread.codexThread = null;
        const recap = recentTranscript(thread, msg.id, 12);
        await runCodex(thread, msg, (recap ? `（之前的對話紀錄，供你參考）\n${recap}\n\n（現在的任務）\n` : '') + prompt, opts, images);
        return resolve();
      }
      if (code !== 0 && !msg.text.trim() && !running.get(thread.id)?.stopped) {
        msg.status = 'error';
        addText(/ENOENT|not recognized|不是內部或外部命令/i.test(stderr) ? '找不到 codex 指令。請到右側「環境」頁檢查 Codex CLI 是否已安裝。' : `Codex 沒有回應（代碼 ${code}）。\n\n${stderr.slice(-1500) || '請到右側「環境」頁檢查 Codex 是否已安裝並登入。'}`);
      }
      if (msg.status === 'streaming') msg.status = running.get(thread.id)?.stopped ? 'stopped' : 'done';
      resolve();
    });
    child.stdin.end(prompt, 'utf8');
  });
}

// ---------------- 對話流程 ----------------
const AGENT_NAME = { claude: 'Claude', codex: 'Codex（GPT）', user: '你' };
function recentTranscript(thread, excludeId, n = 12) {
  const msgs = thread.messages.filter((m) => m.id !== excludeId && m.text).slice(-n);
  return msgs.map((m) => `【${AGENT_NAME[m.role] || m.role}${m.step ? '・' + m.step : ''}】\n${String(m.text).slice(0, 4000)}`).join('\n\n');
}
function unseenFor(thread, agent, excludeIds) {
  const seen = thread.seen?.[agent] ?? 0;
  const msgs = thread.messages.slice(seen).filter((m) => m.role !== agent && !excludeIds.includes(m.id) && m.text);
  if (!msgs.length) return '';
  return msgs.map((m) => `【${AGENT_NAME[m.role] || m.role}${m.step ? '・' + m.step : ''}】\n${String(m.text).slice(0, 6000)}`).join('\n\n');
}
function markSeen(thread, agent) { thread.seen = thread.seen || {}; thread.seen[agent] = thread.messages.length; }
function newMsg(thread, role, step, extra = {}) {
  const m = { id: uid(), role, step: step || '', text: '', tools: [], ts: now(), status: 'streaming', ...extra };
  thread.messages.push(m);
  emit(thread.id, { kind: 'msg_start', msg: m });
  return m;
}
function finishMsg(thread, m) { writeThread(thread); emit(thread.id, { kind: 'msg_end', msg: m }); }

async function askAgent(thread, agent, step, body, opts, userMsg) {
  const ctx = unseenFor(thread, agent, [userMsg?.id].filter(Boolean));
  const m = newMsg(thread, agent, step, { model: agent === 'claude' ? (opts.claudeModel || '') : (opts.codexModel || ''), effort: agent === 'codex' ? opts.codexEffort : undefined });
  const prompt = (ctx ? `（以下是你不在場時這個對話的內容，請先了解）\n${ctx}\n\n────────\n` : '') + body;
  const images = (userMsg?.attachments || []).filter((a) => /\.(png|jpe?g|webp|gif)$/i.test(a)).map((a) => path.join(projectDir(thread.project), a));
  if (agent === 'claude') await runClaude(thread, m, prompt, opts);
  else await runCodex(thread, m, prompt, opts, images);
  if (running.get(thread.id)?.stopped && m.status === 'streaming') m.status = 'stopped';
  markSeen(thread, agent);
  finishMsg(thread, m);
  return m;
}
function withAttachments(text, att) {
  if (!att?.length) return text;
  return `${text}\n\n（使用者附上的檔案，在專案資料夾內：${att.join('、')}）`;
}
async function runTurn(thread, userMsg, opts) {
  const R = { children: new Set(), stopped: false };
  running.set(thread.id, R);
  emit(thread.id, { kind: 'run_start' });
  const userText = withAttachments(userMsg.text, userMsg.attachments);
  try {
    if (opts.mode === 'claude') {
      await askAgent(thread, 'claude', '', userText, opts, userMsg);
    } else if (opts.mode === 'codex') {
      await askAgent(thread, 'codex', '', userText, opts, userMsg);
    } else if (opts.mode === 'both') {
      await Promise.all([
        askAgent(thread, 'claude', '各自回答', userText, opts, userMsg),
        askAgent(thread, 'codex', '各自回答', userText, opts, userMsg),
      ]);
    } else if (opts.mode === 'cross') {
      const rounds = Math.max(1, Math.min(3, Number(opts.rounds) || 1));
      let cl = await askAgent(thread, 'claude', '初稿', userText, opts, userMsg);
      for (let r = 1; r <= rounds && !R.stopped; r++) {
        const review = await askAgent(thread, 'codex', `審查 ${r}`,
`你是獨立的第二意見（交叉驗證）。使用者的需求：
${userText}

Claude 的最新回覆／成果：
${cl.text}

請：
1. 必要時直接讀取專案資料夾裡的檔案（STEP、圖、規格）自己驗證，不要只看文字。
2. 逐點列出你同意與不同意的地方，指出錯誤、遺漏、風險（強度、列印、公差、外觀、是否符合使用者規則）。
3. 給出具體修正建議（數字、尺寸）。若你產出檔案，放在 _codex/out_${Date.now().toString(36)}/。
4. 用繁體中文，表格整理數字；「已量測／估算／待實印」分開寫。`, opts, null);
        if (R.stopped) break;
        cl = await askAgent(thread, 'claude', r === rounds ? '驗證與結論' : `驗證與修正 ${r}`,
`Codex（GPT）對你的成果提出了審查意見（在上方）。請：
1. 逐點用你的工具實際驗證（量測 STEP、干涉、切片、規則），不要照單全收。
2. 合理的接受並修正檔案；不合理的反駁並附證據。
3. ${r === rounds ? '最後給使用者清楚的結論：雙方同意什麼、不同意什麼、你驗證到什麼、還沒驗證的，以及需要使用者決定的事（最多 3 個，用 A／B／C 讓他選）。' : '整理修正後的版本，準備給 Codex 再審一次。'}
請用繁體中文。`, opts, null);
      }
    }
  } catch (e) {
    log('runTurn error', e.stack || e.message);
    const m = newMsg(thread, 'system', '錯誤'); m.text = `工作台發生錯誤：${e.message}`; m.status = 'error'; finishMsg(thread, m);
  } finally {
    running.delete(thread.id);
    writeThread(thread);
    emit(thread.id, { kind: 'run_end' });
  }
}

// ---------------- 額度 ----------------
let usageCache = { t: 0, data: null };
async function claudeUsage() {
  const credFile = path.join(HOME, '.claude', '.credentials.json');
  let tok = null, plan = null, expiresAt = null;
  try {
    const j = JSON.parse(await fsp.readFile(credFile, 'utf8'));
    tok = j.claudeAiOauth?.accessToken; plan = j.claudeAiOauth?.subscriptionType || null; expiresAt = j.claudeAiOauth?.expiresAt || null;
  } catch { if (config.claudeToken) tok = config.claudeToken; else return { ok: false, reason: CLOUD ? '還沒登入 Claude：到「帳號」分頁登入' : '找不到 Claude Code 登入資料（請先在 Claude Code 登入一次）' }; }
  if (!tok) return { ok: false, reason: 'Claude Code 尚未登入' };
  try {
    const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 6000);
    const r = await fetch('https://api.anthropic.com/api/oauth/usage', {
      headers: { Authorization: `Bearer ${tok}`, 'anthropic-beta': 'oauth-2025-04-20', 'Content-Type': 'application/json', 'User-Agent': 'claude-code/2.1' },
      signal: ctl.signal,
    });
    clearTimeout(t);
    if (r.status === 401) return { ok: false, plan, reason: '登入憑證過期：在工作台問 Claude 一句話後會自動更新' };
    if (!r.ok) return { ok: false, plan, reason: `查詢失敗（HTTP ${r.status}）` };
    const j = await r.json();
    const win = (w) => (w && typeof w.utilization === 'number') ? { used: w.utilization, resetsAt: w.resets_at || null } : null;
    const extra = {};
    for (const [k, v] of Object.entries(j)) {
      if (/^seven_day_/.test(k) && v && typeof v.utilization === 'number') extra[k.replace('seven_day_', '')] = win(v);
    }
    return { ok: true, plan, fiveHour: win(j.five_hour), weekly: win(j.seven_day), weeklyByModel: extra, updated: now() };
  } catch (e) {
    return { ok: false, plan, reason: `查詢失敗：${e.name === 'AbortError' ? '逾時' : e.message}` };
  }
}
async function newestFiles(dir, depth, acc) {
  let ents; try { ents = await fsp.readdir(dir, { withFileTypes: true }); } catch { return acc; }
  for (const e of ents) {
    const p = path.join(dir, e.name);
    if (e.isDirectory() && depth > 0) await newestFiles(p, depth - 1, acc);
    else if (e.isFile() && /^rollout-.*\.jsonl$/.test(e.name)) { try { acc.push({ p, m: (await fsp.stat(p)).mtimeMs }); } catch {} }
  }
  return acc;
}
async function codexUsage() {
  const root = path.join(HOME, '.codex', 'sessions');
  // 只看最近 10 天的資料夾
  const files = [];
  for (let d = 0; d < 10; d++) {
    const dt = new Date(Date.now() - d * 86400000);
    const sub = path.join(root, String(dt.getFullYear()), String(dt.getMonth() + 1).padStart(2, '0'), String(dt.getDate()).padStart(2, '0'));
    await newestFiles(sub, 0, files);
  }
  if (!files.length) await newestFiles(root, 3, files);
  files.sort((a, b) => b.m - a.m);
  for (const f of files.slice(0, 15)) {
    let txt; try { txt = await fsp.readFile(f.p, 'utf8'); } catch { continue; }
    const lines = txt.trimEnd().split('\n');
    for (let i = lines.length - 1; i >= 0; i--) {
      if (!lines[i].includes('rate_limits')) continue;
      try {
        const j = JSON.parse(lines[i]);
        const p = j.payload || j;
        const rl = p.rate_limits || p.info?.rate_limits;
        if (!rl) continue;
        const ts = Date.parse(j.timestamp || '') || f.m;
        const win = (w) => {
          if (!w || typeof w.used_percent !== 'number') return null;
          let resetsAt = null;
          if (w.resets_at) resetsAt = new Date((w.resets_at > 1e12 ? w.resets_at : w.resets_at * 1000)).toISOString();
          else if (typeof w.resets_in_seconds === 'number') resetsAt = new Date(ts + w.resets_in_seconds * 1000).toISOString();
          return { used: w.used_percent, windowMinutes: w.window_minutes || null, resetsAt };
        };
        let a = win(rl.primary), b = win(rl.secondary);
        // 依視窗長度判斷哪個是 5 小時、哪個是每週
        let fiveHour = a, weekly = b;
        if (a && b && (a.windowMinutes || 0) > (b.windowMinutes || 0)) { fiveHour = b; weekly = a; }
        return { ok: true, plan: rl.plan_type || null, fiveHour, weekly, updated: new Date(ts).toISOString(), note: '來自 Codex 最後一次使用時的紀錄' };
      } catch {}
    }
  }
  return { ok: false, reason: '還沒有 Codex 的使用紀錄（用一次 Codex 後就會出現）' };
}
async function getUsage(force) {
  if (!force && usageCache.data && Date.now() - usageCache.t < 60000) return usageCache.data;
  const [claude, codex] = await Promise.all([claudeUsage(), codexUsage()]);
  usageCache = { t: Date.now(), data: { claude, codex } };
  return usageCache.data;
}

// ---------------- 環境檢查 ----------------
async function doctor() {
  const [cv, xv, xl] = await Promise.all([runQuick('claude', ['--version']), runQuick('codex', ['--version']), runQuick('codex', ['login', 'status'])]);
  const cred = fs.existsSync(path.join(HOME, '.claude', '.credentials.json'));
  let codexDefaultModel = null;
  try { const toml = fs.readFileSync(path.join(HOME, '.codex', 'config.toml'), 'utf8'); codexDefaultModel = (toml.match(/^\s*model\s*=\s*"([^"]+)"/m) || [])[1] || null; } catch {}
  return {
    claude: { installed: cv.ok, version: cv.out.split('\n')[0] || null, loggedIn: cred },
    codex: { installed: xv.ok, version: xv.out.split('\n')[0] || null, loggedIn: /logged in/i.test(xl.out + xl.err) && !/not logged in/i.test(xl.out + xl.err), status: (xl.out || xl.err).split('\n')[0] || null, defaultModel: codexDefaultModel },
    root: config.root, rootExists: fs.existsSync(config.root), node: process.version, dataDir: DATA_DIR,
  };
}

// ---------------- 產品資料夾 ----------------
const BUILTIN_TEMPLATE = `# {{NAME}} — 產品專案（建立於 {{DATE}}）

全域規則在 Claude Code 的全域 CLAUDE.md，這裡只記這個產品自己的東西。每次有決定或進度，Claude 要更新這個檔案。

## 使用者的需求與決定
- （待填）

## 目前版本與檔案
- （待填）

## 待解決
- （待填）

## 驗證與實印紀錄
- （待填）
`;
function listProjects() {
  try {
    return fs.readdirSync(config.root, { withFileTypes: true })
      .filter((e) => e.isDirectory() && !e.name.startsWith('_') && !e.name.startsWith('.') && !/^MAKERSENSE_(Workbench|DevKit)/i.test(e.name) && path.resolve(config.root, e.name) !== APP_DIR)
      .map((e) => { const p = path.join(config.root, e.name); let m = 0; try { m = fs.statSync(p).mtimeMs; } catch {} return { name: e.name, mtime: m, hasClaudeMd: fs.existsSync(path.join(p, 'CLAUDE.md')) }; })
      .sort((a, b) => b.mtime - a.mtime);
  } catch { return []; }
}
function createProject(name) {
  const n = safeName(name);
  if (!n) throw new Error('名稱不能空白');
  const dir = projectDir(n);
  if (!dir) throw new Error('名稱不合法');
  fs.mkdirSync(path.join(dir, 'RevA'), { recursive: true });
  fs.mkdirSync(path.join(dir, '_codex'), { recursive: true });
  const md = path.join(dir, 'CLAUDE.md');
  if (!fs.existsSync(md)) {
    let tpl = BUILTIN_TEMPLATE;
    try { tpl = fs.readFileSync(path.join(config.root, '_template', 'product_CLAUDE.md'), 'utf8'); } catch {}
    fs.writeFileSync(md, tpl.replaceAll('{{NAME}}', n).replaceAll('{{DATE}}', new Date().toISOString().slice(0, 10)));
  }
  return n;
}
function listFiles(dir, rel = '', depth = 0, out = []) {
  if (out.length > 600) return out;
  let ents; try { ents = fs.readdirSync(path.join(dir, rel), { withFileTypes: true }); } catch { return out; }
  ents.sort((a, b) => (b.isDirectory() - a.isDirectory()) || a.name.localeCompare(b.name, 'zh-Hant'));
  for (const e of ents) {
    if (e.name.startsWith('.') || e.name === 'node_modules' || e.name === '__pycache__') continue;
    const r = rel ? `${rel}/${e.name}` : e.name;
    if (e.isDirectory()) { out.push({ path: r, dir: true, depth }); if (depth < 3) listFiles(dir, r, depth + 1, out); }
    else { let st = null; try { st = fs.statSync(path.join(dir, r)); } catch {} out.push({ path: r, dir: false, depth, size: st?.size || 0, mtime: st?.mtimeMs || 0 }); }
  }
  return out;
}



// ---------------- 帳號登入（雲端主機用：在網頁上完成 Claude／Codex 登入）----------------
const stripAnsi = (t) => String(t).replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, '').replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, '').replace(/\x1b[=>()][0-9A-Za-z]?/g, '').replace(/\r/g, '');
const logins = { claude: null, codex: null }; // { child, out, state: running|done|failed, url, code }
async function accountStatus() {
  const [cs, xs] = await Promise.all([runQuick('claude', ['auth', 'status', '--json'], 15000), runQuick('codex', ['login', 'status'], 15000)]);
  let cj = null; try { cj = JSON.parse(cs.out); } catch {}
  const claudeIn = !!(cj && (cj.loggedIn ?? cj.logged_in ?? cj.authenticated)) || !!config.claudeToken || fs.existsSync(path.join(HOME, '.claude', '.credentials.json'));
  const xo = `${xs.out}\n${xs.err}`;
  const codexIn = /logged in/i.test(xo) && !/not logged in/i.test(xo);
  const view = (k) => logins[k] ? { state: logins[k].state, url: logins[k].url || null, code: logins[k].code || null, output: stripAnsi(logins[k].out).split('\n').filter((l) => l.trim()).slice(-14).join('\n') } : null;
  return {
    cloud: CLOUD,
    claude: { installed: cs.code !== -1 && !/ENOENT|not found|not recognized/i.test(cs.err), loggedIn: claudeIn, method: config.claudeToken ? 'token' : (claudeIn ? 'login' : null), email: cj?.email || cj?.account?.email || null, plan: cj?.subscriptionType || cj?.plan || null, login: view('claude') },
    codex: { installed: xs.code !== -1 && !/ENOENT|not found|not recognized/i.test(xs.err), loggedIn: codexIn, status: xo.trim().split('\n').filter((l) => !/^WARNING/.test(l))[0] || null, login: view('codex') },
  };
}
function startLogin(kind) {
  const cur = logins[kind];
  if (cur?.child && cur.state === 'running') { try { cur.child.kill(); } catch {} }
  let child;
  if (kind === 'claude') {
    // Claude 的登入需要終端機：用 script 模擬一個，網址印出來給使用者點，再把代碼貼回去
    child = IS_WIN ? spawnCli('claude', ['auth', 'login', '--claudeai'], { cwd: HOME, env: { ...process.env, BROWSER: 'none' } })
      : spawn('script', ['-qfec', 'stty cols 4000 rows 50; claude auth login --claudeai', '/dev/null'], { cwd: HOME, env: { ...process.env, BROWSER: 'true' } });
  } else {
    child = spawnCli('codex', ['login', '--device-auth'], { cwd: HOME, env: { ...process.env } });
  }
  const L = { child, out: '', state: 'running', url: null, code: null };
  logins[kind] = L;
  const onData = (d) => {
    L.out += d.toString('utf8'); if (L.out.length > 20000) L.out = L.out.slice(-20000);
    const t = stripAnsi(L.out);
    if (!L.url) L.url = (t.match(kind === 'claude' ? /https:\/\/claude\.(?:com|ai)\/\S+/ : /https:\/\/auth\.openai\.com\/\S+/) || [])[0] || null;
    if (kind === 'codex' && !L.code) L.code = (t.match(/\b([A-Z0-9]{4,5}-[A-Z0-9]{4,5})\b/) || [])[1] || null;
    if (/login successful|logged in successfully|successfully logged in|signed in/i.test(t)) L.state = 'done';
  };
  child.stdout.on('data', onData); child.stderr?.on('data', onData);
  child.on('close', (code) => { if (L.state === 'running') L.state = code === 0 ? 'done' : 'failed'; usageCache.t = 0; });
  child.on('error', (e) => { L.out += `\n${e.message}`; L.state = 'failed'; });
  setTimeout(() => { if (L.state === 'running') { try { child.kill(); } catch {} L.state = 'failed'; L.out += '\n（逾時，請重新開始）'; } }, 15 * 60000).unref();
}
function submitClaudeCode(code) {
  const L = logins.claude; if (!L || L.state !== 'running') throw new Error('請先按「開始登入」');
  L.child.stdin.write(String(code).trim() + '\r');
}

// ---------------- 規則、技能、範例專案（第一次在雲端啟動時安裝）----------------
function installRules() {
  const src = path.join(APP_DIR, 'rules');
  if (!fs.existsSync(src)) return;
  const claudeDir = path.join(HOME, '.claude');
  try {
    fs.mkdirSync(path.join(claudeDir, 'skills'), { recursive: true });
    const g = path.join(claudeDir, 'CLAUDE.md');
    if (CLOUD && !fs.existsSync(g) && fs.existsSync(path.join(src, 'CLAUDE.md'))) fs.copyFileSync(path.join(src, 'CLAUDE.md'), g);
    const sk = path.join(src, 'skills');
    if (fs.existsSync(sk)) for (const name of fs.readdirSync(sk)) {
      const from = path.join(sk, name, 'SKILL.md'), to = path.join(claudeDir, 'skills', name, 'SKILL.md');
      if (!fs.existsSync(from)) continue;
      if (!fs.existsSync(to)) { if (CLOUD) { fs.mkdirSync(path.dirname(to), { recursive: true }); fs.copyFileSync(from, to); } }
      else if (/codex exec --full-auto/.test(fs.readFileSync(to, "utf8"))) {
        // 新版 Codex 移除了 --full-auto：自動換成新寫法（舊檔備份）
        fs.copyFileSync(to, to + '.bak'); fs.copyFileSync(from, to); log('updated skill', name);
      }
    }
    const agents = path.join(HOME, '.codex', 'AGENTS.md');
    if (!fs.existsSync(agents) && fs.existsSync(g)) { fs.mkdirSync(path.dirname(agents), { recursive: true }); fs.copyFileSync(g, agents); }
  } catch (e) { log('installRules failed', e.message); }
}
function seedProjects() {
  const seed = path.join(APP_DIR, 'seed');
  if (!CLOUD || !fs.existsSync(seed)) return;
  let map = {}; try { map = JSON.parse(fs.readFileSync(path.join(seed, 'seed.json'), 'utf8')); } catch {}
  fs.mkdirSync(config.root, { recursive: true });
  const marker = path.join(DATA_DIR, 'seeded');
  if (fs.existsSync(marker)) return;
  for (const name of fs.readdirSync(seed)) {
    const from = path.join(seed, name); if (!fs.statSync(from).isDirectory()) continue;
    const to = path.join(config.root, map[name] || name);
    if (!fs.existsSync(to)) fs.cpSync(from, to, { recursive: true });
  }
  fs.mkdirSync(path.join(config.root, '_template'), { recursive: true });
  fs.writeFileSync(marker, now());
}
installRules();
seedProjects();

// ---------------- 登入（遠端用）----------------
// 本機捷徑用 local.key 自動登入；從公司／手機連進來要輸入密碼
const KEY_FILE = path.join(DATA_DIR, 'local.key');
const SESS_FILE = path.join(DATA_DIR, 'sessions.json');
let LOCAL_KEY; try { LOCAL_KEY = fs.readFileSync(KEY_FILE, 'utf8').trim(); } catch {}
if (!LOCAL_KEY) { LOCAL_KEY = crypto.randomBytes(24).toString('hex'); fs.writeFileSync(KEY_FILE, LOCAL_KEY); }
let sessions = {}; try { sessions = JSON.parse(fs.readFileSync(SESS_FILE, 'utf8')); } catch {}
const SESSION_DAYS = 30;
const saveSessions = () => { try { fs.writeFileSync(SESS_FILE, JSON.stringify(sessions)); } catch {} };
function newSession(kind) {
  const t = crypto.randomBytes(32).toString('hex');
  sessions[t] = { kind, exp: Date.now() + SESSION_DAYS * 86400000 };
  for (const [k, v] of Object.entries(sessions)) if (v.exp < Date.now()) delete sessions[k];
  saveSessions(); return t;
}
function getSession(req) {
  const m = /(?:^|;\s*)msw=([a-f0-9]{64})/.exec(req.headers.cookie || '');
  const v = m && sessions[m[1]];
  return v && v.exp > Date.now() ? { token: m[1], ...v } : null;
}
const cookieFor = (t) => `msw=${t}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${SESSION_DAYS * 86400}`;
const safeEq = (a, b) => { const x = Buffer.from(String(a)), y = Buffer.from(String(b)); return x.length === y.length && crypto.timingSafeEqual(x, y); };
function hashPw(pw, salt) { return crypto.scryptSync(String(pw), salt, 32).toString('hex'); }
function checkPw(pw) { return !!config.auth && safeEq(hashPw(pw, config.auth.salt), config.auth.hash); }
const loginFails = new Map(); // ip -> { n, until }
const clientIp = (req) => String(req.headers['x-forwarded-for'] || req.socket.remoteAddress || '').split(',')[0].trim();

// ---------------- 遠端存取（Tailscale Funnel，免費）----------------
function tsBin() {
  if (IS_WIN) { for (const p of ['C:\\Program Files\\Tailscale\\tailscale.exe', 'C:\\Program Files (x86)\\Tailscale\\tailscale.exe']) if (fs.existsSync(p)) return p; }
  return 'tailscale';
}
async function remoteStatus() {
  const base = { hasPassword: !!config.auth, enabled: !!config.remote?.enabled, autostart: !!config.remote?.autostart };
  const v = await runQuick(tsBin(), ['version'], 8000);
  if (!v.ok) return { ...base, installed: false };
  const st = await runQuick(tsBin(), ['status', '--json'], 8000);
  let j = null; try { j = JSON.parse(st.out); } catch {}
  const dns = j?.Self?.DNSName ? String(j.Self.DNSName).replace(/\.$/, '') : null;
  let funnelOn = false;
  const fst = await runQuick(tsBin(), ['funnel', 'status', '--json'], 8000);
  try { const f = JSON.parse(fst.out || '{}'); funnelOn = !!(f.AllowFunnel && Object.values(f.AllowFunnel).some(Boolean)); } catch {}
  return { ...base, installed: true, version: v.out.split('\n')[0], state: j?.BackendState || 'Unknown', running: j?.BackendState === 'Running', url: dns ? `https://${dns}` : null, funnelOn };
}
function setAutostart(on) {
  if (!IS_WIN) return Promise.resolve(false);
  const vbs = path.join(APP_DIR, 'launch.vbs').replace(/'/g, "''");
  const script = on
    ? `$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'MAKERSENSE Workbench Server.lnk'));$s.TargetPath="$env:WINDIR\\System32\\wscript.exe";$s.Arguments='"${vbs}" server';$s.WorkingDirectory='${APP_DIR.replace(/'/g, "''")}';$s.Save()`
    : `Remove-Item -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath('Startup')) 'MAKERSENSE Workbench Server.lnk')`;
  return new Promise((resolve) => execFile('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script], { windowsHide: true }, (e) => resolve(!e)));
}
async function remoteEnable() {
  if (!config.auth) throw new Error('請先設定登入密碼，再開啟遠端存取');
  const r = await runQuick(tsBin(), ['funnel', '--bg', String(PORT)], 30000);
  const all = `${r.out}\n${r.err}`;
  const link = (all.match(/https:\/\/login\.tailscale\.com\/\S+/) || [])[0];
  if (!r.ok) {
    if (link) return { ok: false, needApprove: link };
    if (/not logged in|NeedsLogin|logged out/i.test(all)) return { ok: false, reason: 'Tailscale 還沒登入：請點右下角工作列的 Tailscale 圖示登入（用 Google 帳號即可）' };
    return { ok: false, reason: all.trim().slice(-600) || 'Tailscale 沒有回應' };
  }
  config.remote = { ...(config.remote || {}), enabled: true, autostart: await setAutostart(true) };
  saveConfig();
  return { ok: true, status: await remoteStatus() };
}
async function remoteDisable() {
  await runQuick(tsBin(), ['funnel', '--https=443', 'off'], 15000);
  config.remote = { ...(config.remote || {}), enabled: false, autostart: (await setAutostart(false)) ? false : !!config.remote?.autostart };
  saveConfig();
  return { ok: true, status: await remoteStatus() };
}

// ---------------- HTTP ----------------
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
  '.json': 'application/json; charset=utf-8', '.md': 'text/plain; charset=utf-8', '.txt': 'text/plain; charset=utf-8', '.csv': 'text/plain; charset=utf-8', '.py': 'text/plain; charset=utf-8',
  '.pdf': 'application/pdf', '.woff2': 'font/woff2' };
const send = (res, code, obj, headers = {}) => { res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers }); res.end(JSON.stringify(obj)); };
async function body(req, limit = 60 * 1024 * 1024) {
  const chunks = []; let n = 0;
  for await (const c of req) { n += c.length; if (n > limit) throw new Error('檔案太大'); chunks.push(c); }
  return Buffer.concat(chunks);
}
const jsonBody = async (req) => { const b = await body(req, 5 * 1024 * 1024); return b.length ? JSON.parse(b.toString('utf8')) : {}; };
function okOrigin(req) {
  const o = req.headers.origin; if (!o) return true;
  const host = req.headers.host || '', fwd = req.headers['x-forwarded-host'] || '';
  return o === `http://127.0.0.1:${PORT}` || o === `http://localhost:${PORT}` || o === `https://${host}` || o === `http://${host}` || (fwd && o === `https://${fwd}`) || (CS_ORIGIN && o === CS_ORIGIN);
}

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const p = u.pathname;
  try {
    if (req.method !== 'GET' && !okOrigin(req)) return send(res, 403, { error: 'forbidden' });
    const host = String(req.headers.host || '').toLowerCase();
    const loopHost = /^(127\.0\.0\.1|localhost)(:\d+)?$/.test(host) && !req.headers['x-forwarded-for'];
    // 本機捷徑自動登入
    if (p === '/' && u.searchParams.get('k')) {
      if (safeEq(u.searchParams.get('k'), LOCAL_KEY)) { res.writeHead(302, { 'Set-Cookie': cookieFor(newSession('local')), Location: '/' }); return res.end(); }
    }
    const sess = getSession(req);
    // Codespaces：沒設密碼時靠 GitHub 私人連接埠擋人；設了密碼（例如連接埠改成公開）就要輸入密碼
    const authed = (CODESPACES && !config.auth) || !!sess || (!config.auth && loopHost);
    const openPath = ['/login.html', '/icon.png', '/api/login', '/api/ping', '/api/authinfo'].includes(p);
    if (!authed && !openPath) {
      if (p.startsWith('/api/')) return send(res, 401, { error: '請先登入' });
      res.writeHead(302, { Location: '/login.html' }); return res.end();
    }
    if (p === '/api/authinfo') return send(res, 200, { hasPassword: !!config.auth || CODESPACES, authed });
    if (p === '/api/login' && req.method === 'POST') {
      const ip = clientIp(req); const f = loginFails.get(ip) || { n: 0, until: 0 };
      if (f.until > Date.now()) return send(res, 429, { error: `嘗試太多次，請 ${Math.ceil((f.until - Date.now()) / 60000)} 分鐘後再試` });
      const b = await jsonBody(req);
      if (!config.auth) return send(res, 400, { error: '還沒設定密碼：請在家裡電腦的工作台「設定 → 遠端存取」設定' });
      if (!checkPw(b.password || '')) { f.n++; if (f.n >= 5) { f.until = Date.now() + 10 * 60000; f.n = 0; } loginFails.set(ip, f); log('login failed', ip); return send(res, 401, { error: '密碼不對' }); }
      loginFails.delete(ip);
      return send(res, 200, { ok: true }, { 'Set-Cookie': cookieFor(newSession('remote')) });
    }
    if (p === '/api/logout' && req.method === 'POST') { if (sess) { delete sessions[sess.token]; saveSessions(); } return send(res, 200, { ok: true }, { 'Set-Cookie': 'msw=; Path=/; Max-Age=0' }); }
    if (p === '/api/password' && req.method === 'POST') {
      const b = await jsonBody(req);
      if (config.auth && !(sess?.kind === 'local' || loopHost) && !checkPw(b.current || '')) return send(res, 401, { error: '目前的密碼不對' });
      const pw = String(b.password || '');
      if (pw.length < 8) return send(res, 400, { error: '密碼至少 8 個字' });
      const salt = crypto.randomBytes(16).toString('hex');
      config.auth = { salt, hash: hashPw(pw, salt) }; saveConfig();
      // 換密碼：其他裝置要重新登入
      for (const [k, v] of Object.entries(sessions)) if (v.kind === 'remote' && k !== sess?.token) delete sessions[k];
      saveSessions();
      return send(res, 200, { ok: true });
    }

    if (p === '/api/accounts' && req.method === 'GET') return send(res, 200, await accountStatus());
    if (p === '/api/accounts' && req.method === 'POST') {
      const b = await jsonBody(req);
      if (b.action === 'start' && ['claude', 'codex'].includes(b.kind)) { startLogin(b.kind); await new Promise((r) => setTimeout(r, 4000)); return send(res, 200, await accountStatus()); }
      if (b.action === 'code') { submitClaudeCode(b.code || ''); await new Promise((r) => setTimeout(r, 5000)); return send(res, 200, await accountStatus()); }
      if (b.action === 'token') {
        const t = String(b.token || '').trim();
        if (!/^sk-ant-oat\d*-[\w-]{20,}$/.test(t)) return send(res, 400, { error: '這不像 Claude 的長效登入碼（應該是 sk-ant-oat 開頭）' });
        config.claudeToken = t; saveConfig(); return send(res, 200, await accountStatus());
      }
      if (b.action === 'cleartoken') { config.claudeToken = null; saveConfig(); return send(res, 200, await accountStatus()); }
      return send(res, 400, { error: 'bad action' });
    }
    if (p === '/api/accounts/file' && req.method === 'POST') {
      // 上傳家裡電腦的登入檔：claude=.credentials.json，codex=auth.json
      const kind = u.searchParams.get('kind'); const data = await body(req, 1024 * 1024);
      let j; try { j = JSON.parse(data.toString('utf8')); } catch { return send(res, 400, { error: '不是有效的 JSON 檔' }); }
      if (kind === 'claude') { if (!j.claudeAiOauth) return send(res, 400, { error: '這不是 Claude 的 .credentials.json' }); fs.mkdirSync(path.join(HOME, '.claude'), { recursive: true }); fs.writeFileSync(path.join(HOME, '.claude', '.credentials.json'), data, { mode: 0o600 }); }
      else if (kind === 'codex') { if (!j.tokens && !j.OPENAI_API_KEY) return send(res, 400, { error: '這不是 Codex 的 auth.json' }); fs.mkdirSync(path.join(HOME, '.codex'), { recursive: true }); fs.writeFileSync(path.join(HOME, '.codex', 'auth.json'), data, { mode: 0o600 }); }
      else return send(res, 400, { error: 'bad kind' });
      usageCache.t = 0; return send(res, 200, await accountStatus());
    }
    if (p === '/api/import' && req.method === 'POST') {
      // 上傳 zip 匯入產品：zip 裡只有一個資料夾 → 用那個名字；否則用使用者給的名字
      const buf = await body(req, 500 * 1024 * 1024);
      const top = zipTopFolder(buf);
      const name = safeName(decodeURIComponent(u.searchParams.get('name') || '')) || safeName(top) || `匯入_${new Date().toISOString().slice(0, 10)}`;
      const dir = projectDir(name); if (!dir) return send(res, 400, { error: '名稱不合法' });
      const merge = u.searchParams.get('merge') === '1';
      if (fs.existsSync(dir) && !merge) return send(res, 409, { error: `已經有「${name}」這個產品`, name });
      fs.mkdirSync(dir, { recursive: true });
      const r = extractZip(buf, dir, { stripTop: !!top });
      return send(res, 200, { ok: true, name, files: r.files, projects: listProjects() });
    }
    if (p === '/api/export-project') {
      const dir = projectDir(u.searchParams.get('project')); if (!dir || !fs.existsSync(dir)) return send(res, 404, {});
      const name = path.basename(dir);
      const zip = zipFolder(dir, { baseName: name, skip: (r) => /(^|\/)(node_modules|__pycache__|\.git)(\/|$)/.test(r) });
      res.writeHead(200, { 'Content-Type': 'application/zip', 'Content-Length': zip.length, 'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(name + '_' + new Date().toISOString().slice(0, 10) + '.zip')}` });
      return res.end(zip);
    }
    if (p === '/api/update' && req.method === 'POST') {
      // 上傳新版 MAKERSENSE_Workbench.zip 更新工作台；雲端會自動重啟
      const buf = await body(req, 200 * 1024 * 1024);
      // 先解到暫存資料夾檢查版本，避免用舊版蓋掉新版
      const tmp = path.join(os.tmpdir(), 'ms-wb-update-' + uid(6));
      try { extractZip(buf, tmp, { stripTop: true }); } catch (e) { return send(res, 400, { error: e.message }); }
      const srv = path.join(tmp, 'server.mjs');
      if (!fs.existsSync(srv)) { fs.rmSync(tmp, { recursive: true, force: true }); return send(res, 400, { error: '這不是工作台的更新檔' }); }
      const nv = (fs.readFileSync(srv, 'utf8').match(/const APP_VERSION = '([^']+)'/) || [])[1] || '0';
      if (nv < APP_VERSION && u.searchParams.get('force') !== '1') { fs.rmSync(tmp, { recursive: true, force: true }); return send(res, 409, { error: `這個檔案是舊版（${nv}），目前是 ${APP_VERSION}，沒有更新` }); }
      fs.cpSync(tmp, APP_DIR, { recursive: true }); fs.rmSync(tmp, { recursive: true, force: true });
      const r = { files: true };
      log('updated app to', nv);
      send(res, 200, { ok: true, version: nv, restart: CLOUD });
      if (CLOUD) setTimeout(() => process.exit(0), 800);
      return;
    }
    if (p === '/api/remote' && req.method === 'GET') return send(res, 200, await remoteStatus());
    if (p === '/api/remote' && req.method === 'POST') {
      const b = await jsonBody(req);
      if (b.action === 'enable') return send(res, 200, await remoteEnable());
      if (b.action === 'disable') return send(res, 200, await remoteDisable());
      if (b.action === 'nosleep' && IS_WIN) { const r = await runQuick('powercfg', ['/change', 'standby-timeout-ac', '0'], 10000); return send(res, 200, { ok: r.ok }); }
      return send(res, 400, { error: 'bad action' });
    }
    // ----- 靜態 -----
    if (req.method === 'GET' && !p.startsWith('/api/')) {
      const f = inside(path.join(APP_DIR, 'public'), p === '/' ? 'index.html' : decodeURIComponent(p.slice(1)));
      if (!f || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); return res.end('not found'); }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(f).toLowerCase()] || 'application/octet-stream', 'Cache-Control': 'no-cache' });
      return fs.createReadStream(f).pipe(res);
    }
    // ----- API -----
    if (p === '/api/ping') return send(res, 200, { ok: true, app: 'makersense-workbench', version: APP_VERSION });
    if (p === '/api/state' && req.method === 'GET') { const { auth, claudeToken, ...pub } = config; return send(res, 200, { config: pub, projects: listProjects(), local: !CLOUD && (loopHost || sess?.kind === 'local'), cloud: CLOUD, codespaces: CODESPACES, hasPassword: !!auth }); }
    if (p === '/api/config' && req.method === 'POST') {
      const b = await jsonBody(req);
      for (const k of ['root', 'theme', 'claudePermission', 'claudeModels', 'codexModels', 'last']) if (k in b) config[k] = b[k];
      saveConfig(); const { auth, claudeToken, ...pub } = config; return send(res, 200, { ok: true, config: pub });
    }
    if (p === '/api/doctor') return send(res, 200, await doctor());
    if (p === '/api/usage') return send(res, 200, await getUsage(u.searchParams.get('force') === '1'));
    if (p === '/api/projects' && req.method === 'POST') { const b = await jsonBody(req); const n = createProject(b.name); return send(res, 200, { ok: true, name: n, projects: listProjects() }); }
    if (p === '/api/files') {
      const dir = projectDir(u.searchParams.get('project')); if (!dir) return send(res, 400, { error: 'bad project' });
      return send(res, 200, { files: listFiles(dir) });
    }
    if (p === '/api/file') {
      const dir = projectDir(u.searchParams.get('project')); const f = dir && inside(dir, u.searchParams.get('path') || '');
      if (!f || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); return res.end(); }
      const hd = { 'Content-Type': MIME[path.extname(f).toLowerCase()] || 'application/octet-stream', 'Cache-Control': 'no-cache', 'Content-Length': fs.statSync(f).size };
      if (u.searchParams.get('dl') === '1') hd['Content-Disposition'] = `attachment; filename*=UTF-8''${encodeURIComponent(path.basename(f))}`;
      res.writeHead(200, hd);
      return fs.createReadStream(f).pipe(res);
    }
    if (p === '/api/open' && req.method === 'POST') {
      const b = await jsonBody(req); const dir = projectDir(b.project); if (!dir) return send(res, 400, { error: 'bad project' });
      const f = b.path ? inside(dir, b.path) : dir; if (!f) return send(res, 400, { error: 'bad path' });
      if (IS_WIN) { if (b.path && fs.existsSync(f) && !fs.statSync(f).isDirectory()) spawn('explorer.exe', [`/select,${f}`], { detached: true }); else spawn('explorer.exe', [f], { detached: true }); }
      return send(res, 200, { ok: true });
    }
    if (p === '/api/upload' && req.method === 'POST') {
      const dir = projectDir(u.searchParams.get('project')); if (!dir) return send(res, 400, { error: 'bad project' });
      const name = safeName(decodeURIComponent(u.searchParams.get('name') || 'file')) || 'file';
      const inbox = path.join(dir, '_inbox'); fs.mkdirSync(inbox, { recursive: true });
      let target = path.join(inbox, name); let k = 1;
      while (fs.existsSync(target)) { const e = path.extname(name); target = path.join(inbox, `${path.basename(name, e)}_${k++}${e}`); }
      fs.writeFileSync(target, await body(req));
      return send(res, 200, { ok: true, path: path.relative(dir, target).split(path.sep).join('/') });
    }
    if (p === '/api/threads' && req.method === 'GET') return send(res, 200, { threads: listThreads(u.searchParams.get('project')) });
    if (p === '/api/threads' && req.method === 'POST') {
      const b = await jsonBody(req); if (!projectDir(b.project)) return send(res, 400, { error: 'bad project' });
      const t = { id: uid(12), project: safeName(b.project), title: b.title || '新對話', created: now(), updated: now(), messages: [], claudeSession: null, codexThread: null, seen: {} };
      writeThread(t); return send(res, 200, { thread: t });
    }
    if (p === '/api/thread' && req.method === 'GET') { const t = readThread(u.searchParams.get('id') || ''); return t ? send(res, 200, { thread: t, running: running.has(t.id) }) : send(res, 404, { error: 'not found' }); }
    if (p === '/api/thread' && req.method === 'PATCH') {
      const b = await jsonBody(req); const t = readThread(b.id || ''); if (!t) return send(res, 404, {});
      if (b.title) t.title = String(b.title).slice(0, 80);
      if (b.resetSessions) { t.claudeSession = null; t.codexThread = null; t.seen = {}; }
      writeThread(t); return send(res, 200, { thread: t });
    }
    if (p === '/api/thread' && req.method === 'DELETE') {
      const id = u.searchParams.get('id') || ''; if (running.has(id)) return send(res, 409, { error: '執行中' });
      try { fs.unlinkSync(threadPath(id)); } catch {} return send(res, 200, { ok: true });
    }
    if (p === '/api/events') {
      const id = u.searchParams.get('thread') || '';
      res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-store', Connection: 'keep-alive' });
      res.write(`data: ${JSON.stringify({ kind: 'hello', running: running.has(id) })}\n\n`);
      if (!subscribers.has(id)) subscribers.set(id, new Set());
      subscribers.get(id).add(res);
      const ka = setInterval(() => { try { res.write(': ka\n\n'); } catch {} }, 20000);
      req.on('close', () => { clearInterval(ka); subscribers.get(id)?.delete(res); });
      return;
    }
    if (p === '/api/send' && req.method === 'POST') {
      const b = await jsonBody(req);
      const t = readThread(b.threadId || ''); if (!t) return send(res, 404, { error: '找不到對話' });
      if (running.has(t.id)) return send(res, 409, { error: '這個對話還在執行中' });
      if (!projectDir(t.project) || !fs.existsSync(projectDir(t.project))) return send(res, 400, { error: '產品資料夾不存在' });
      const text = String(b.text || '').trim(); if (!text) return send(res, 400, { error: '內容是空的' });
      const opts = { mode: ['claude', 'codex', 'cross', 'both'].includes(b.mode) ? b.mode : 'claude', claudeModel: String(b.claudeModel || ''), codexModel: String(b.codexModel || ''), codexEffort: String(b.codexEffort || ''), rounds: b.rounds };
      config.last = { claudeModel: opts.claudeModel, codexModel: opts.codexModel, codexEffort: opts.codexEffort, mode: opts.mode, rounds: Number(b.rounds) || 1 }; saveConfig();
      const userMsg = { id: uid(), role: 'user', text, attachments: Array.isArray(b.attachments) ? b.attachments.slice(0, 20) : [], mode: opts.mode, ts: now(), status: 'done', tools: [] };
      t.messages.push(userMsg);
      if (t.messages.filter((m) => m.role === 'user').length === 1 && (t.title === '新對話' || !t.title)) t.title = text.replace(/\s+/g, ' ').slice(0, 28);
      writeThread(t);
      emit(t.id, { kind: 'msg_start', msg: userMsg }); emit(t.id, { kind: 'msg_end', msg: userMsg });
      runTurn(t, userMsg, opts);
      return send(res, 200, { ok: true, title: t.title });
    }
    if (p === '/api/stop' && req.method === 'POST') {
      const b = await jsonBody(req); const R = running.get(b.threadId || '');
      if (R) { R.stopped = true; for (const c of R.children) killTree(c); }
      return send(res, 200, { ok: true });
    }
    if (p === '/api/export' && req.method === 'POST') {
      const b = await jsonBody(req); const t = readThread(b.threadId || ''); if (!t) return send(res, 404, {});
      const dir = projectDir(t.project); const outDir = path.join(dir, '_records'); fs.mkdirSync(outDir, { recursive: true });
      const md = [`# ${t.title}`, '', `產品：${t.project}　建立：${t.created}　更新：${t.updated}`, ''];
      for (const m of t.messages) {
        md.push(`## ${AGENT_NAME[m.role] || m.role}${m.step ? '・' + m.step : ''}${m.model ? `（${m.model}）` : ''}　${m.ts}`, '');
        if (m.tools?.length) md.push(`> 工具：${m.tools.map((x) => `${x.name} ${x.detail || ''}`.trim()).join('；').slice(0, 1500)}`, '');
        md.push(m.text || '', '');
      }
      const f = path.join(outDir, `${new Date().toISOString().slice(0, 10)}_${safeName(t.title) || 'record'}.md`);
      fs.writeFileSync(f, md.join('\n'));
      return send(res, 200, { ok: true, path: path.relative(dir, f).split(path.sep).join('/') });
    }
    if (p === '/api/search') {
      const q = (u.searchParams.get('q') || '').toLowerCase(); if (!q) return send(res, 200, { results: [] });
      const results = [];
      for (const f of fs.readdirSync(THREADS_DIR)) {
        try {
          const t = JSON.parse(fs.readFileSync(path.join(THREADS_DIR, f), 'utf8'));
          const hit = t.messages.find((m) => String(m.text || '').toLowerCase().includes(q)) || (t.title.toLowerCase().includes(q) ? t.messages[0] : null);
          if (hit) { const s = String(hit.text || ''); const i = Math.max(0, s.toLowerCase().indexOf(q) - 30); results.push({ id: t.id, project: t.project, title: t.title, snippet: s.slice(i, i + 120).replace(/\s+/g, ' '), updated: t.updated }); }
        } catch {}
      }
      return send(res, 200, { results: results.sort((a, b) => (b.updated || '').localeCompare(a.updated || '')).slice(0, 40) });
    }
    send(res, 404, { error: 'not found' });
  } catch (e) {
    log('http error', p, e.stack || e.message);
    send(res, 500, { error: e.message });
  }
});

// 由捷徑啟動時：視窗都關掉、也沒有任務在跑，閒置 20 分鐘後自動結束（下次開捷徑會再啟動）
let lastActive = Date.now();
if (process.env.MS_WB_AUTOEXIT === '1') {
  setInterval(() => {
    let clients = 0; for (const s of subscribers.values()) clients += s.size;
    if (clients > 0 || running.size > 0 || config.remote?.enabled) { lastActive = Date.now(); return; }
    if (Date.now() - lastActive > 20 * 60000) { log('idle, exiting'); process.exit(0); }
  }, 60000).unref();
}

server.on('error', (e) => { log('server error', e.message); if (e.code === 'EADDRINUSE') { console.log('工作台已經在執行中'); process.exit(0); } });
server.listen(PORT, '127.0.0.1', () => { log(`MAKERSENSE 工作台 http://127.0.0.1:${PORT}  data=${DATA_DIR}  root=${config.root}`); console.log(`MAKERSENSE 工作台已啟動：http://127.0.0.1:${PORT}`); });
