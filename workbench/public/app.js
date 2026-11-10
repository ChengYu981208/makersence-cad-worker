/* MAKERSENSE 工作台 — 前端 */
'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v; else if (k === 'text') el.textContent = v; else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const store = { get(k, d) { try { const v = localStorage.getItem('msw.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } }, set(k, v) { try { localStorage.setItem('msw.' + k, JSON.stringify(v)); } catch {} } };

async function api(path, opts = {}) {
  const o = { method: opts.method || (opts.body ? 'POST' : 'GET'), headers: {} };
  if (opts.body !== undefined) {
    if (opts.raw) { o.body = opts.body; } else { o.body = JSON.stringify(opts.body); o.headers['Content-Type'] = 'application/json'; }
  }
  const r = await fetch(path, o);
  let j = null; try { j = await r.json(); } catch {}
  if (r.status === 401 && !path.startsWith('/api/password') && !path.startsWith('/api/login')) { location.href = '/login.html'; throw new Error('請先登入'); }
  if (!r.ok) throw new Error(j?.error || `HTTP ${r.status}`);
  return j;
}
function toast(msg, ms = 2200) { const t = $('#toast'); t.textContent = msg; t.classList.remove('hidden'); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add('hidden'), ms); }

const S = {
  local: true, hasPassword: false, remote: null,
  config: null, projects: [], project: null, threads: [], thread: null, running: false,
  es: null, msgEls: new Map(), attachments: [], doctor: null, usage: null, mode: 'claude', expanded: new Set(), files: [],
};
const NAMES = { claude: 'Claude', codex: 'Codex', user: '你', system: '系統' };
const MODE_HINT = {
  claude: '只問 Claude：直接在產品資料夾裡建模、驗證、切片。',
  codex: '只問 Codex（GPT）：用你的 ChatGPT 帳號，在產品資料夾裡工作。',
  cross: '交叉驗證：Claude 先做 → Codex 獨立審查 → Claude 用工具驗證並修正，最後給你結論。',
  both: '同時問：同一個問題兩邊各自回答，並排比較。',
};

// ---------------- Markdown ----------------
marked.setOptions({ gfm: true, breaks: true });
function fileUrl(p) { return `/api/file?project=${encodeURIComponent(S.project)}&path=${encodeURIComponent(p)}`; }
function renderMd(el, text) {
  el.innerHTML = DOMPurify.sanitize(marked.parse(text || ''), { ADD_ATTR: ['target'] });
  for (const img of $$('img', el)) {
    const src = img.getAttribute('src') || '';
    if (src && !/^(https?:|data:|\/api\/)/i.test(src)) img.src = fileUrl(src.replace(/^\.?\//, ''));
    img.style.cursor = 'zoom-in'; img.onclick = () => showImage(img.src, img.alt);
  }
  for (const a of $$('a', el)) {
    const href = a.getAttribute('href') || '';
    if (/^https?:/i.test(href)) { a.target = '_blank'; a.rel = 'noopener'; }
    else if (href && !href.startsWith('#')) { a.onclick = (e) => { e.preventDefault(); openFile(decodeURI(href.replace(/^\.?\//, ''))); }; }
  }
  for (const pre of $$('pre', el)) {
    const b = h('button', { class: 'copy', text: '複製', onclick: () => { navigator.clipboard.writeText(pre.querySelector('code')?.innerText || pre.innerText); b.textContent = '已複製'; setTimeout(() => b.textContent = '複製', 1200); } });
    pre.append(b);
  }
}

// ---------------- 時間 ----------------
const fmtTime = (iso) => { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', hour12: false }); };
const fmtDate = (iso) => { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleDateString('zh-TW', { month: 'numeric', day: 'numeric' }); };
function fmtDur(ms) { if (!ms && ms !== 0) return ''; const s = Math.round(ms / 1000); return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`; }
function untilText(iso) {
  if (!iso) return '';
  const ms = new Date(iso) - Date.now(); if (isNaN(ms)) return '';
  if (ms <= 0) return '已重置';
  const m = Math.round(ms / 60000), d = Math.floor(m / 1440), hh = Math.floor((m % 1440) / 60), mm = m % 60;
  const when = new Date(iso).toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
  return `${d ? d + ' 天 ' : ''}${hh ? hh + ' 小時 ' : ''}${d ? '' : mm + ' 分'}後重置（${when}）`;
}
function dayGroup(iso) {
  const d = new Date(iso), t = new Date(); t.setHours(0, 0, 0, 0);
  const diff = (t - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000;
  return diff <= 0 ? '今天' : diff <= 1 ? '昨天' : diff <= 7 ? '最近 7 天' : diff <= 30 ? '最近 30 天' : '更早';
}

// ---------------- 初始化 ----------------
async function init() {
  EMPTY_CLONE = document.getElementById('empty')?.cloneNode(true);
  applyTheme(store.get('theme', null) || 'light');
  const st = await api('/api/state');
  S.config = st.config; S.projects = st.projects; S.local = !!st.local; S.hasPassword = !!st.hasPassword;
  S.cloud = !!st.cloud;
  document.body.classList.toggle('remote', !S.local);
  document.body.classList.toggle('cloud', S.cloud);
  document.body.classList.toggle('codespaces', !!st.codespaces);
  applyTheme(store.get('theme', null) || S.config.theme || 'light');
  S.mode = S.config.last?.mode || 'claude';
  fillPickers();
  renderProjects();
  const saved = store.get('project', null);
  const first = S.projects.find((p) => p.name === saved)?.name || S.projects[0]?.name || null;
  if (first) await selectProject(first, store.get('thread.' + first, null));
  else showNoProject();
  setMode(S.mode);
  bindUi();
  refreshUsage(); setInterval(() => refreshUsage(), 90000);
  setInterval(() => { if (S.usage) renderUsage(); }, 30000);
  refreshDoctor();
  fillSettings();
  if (S.cloud) {
    // 雲端第一次使用：還沒登入就直接打開「帳號」分頁
    try { S.accounts = await api('/api/accounts'); if (!S.accounts.claude.loggedIn || !S.accounts.codex.loggedIn) { $('#tabs button[data-tab=accounts]').click(); toast('先在右邊「帳號」分頁登入 Claude 和 Codex', 4000); } } catch {}
  }
}

function applyTheme(t) { document.documentElement.dataset.theme = t; store.set('theme', t); }

// ---------------- 產品 ----------------
function renderProjects() {
  const sel = $('#projectSelect'); sel.innerHTML = '';
  if (!S.projects.length) sel.append(h('option', { value: '', text: '（還沒有產品）' }));
  for (const p of S.projects) sel.append(h('option', { value: p.name, text: p.name }));
  if (S.project) sel.value = S.project;
}
function showNoProject() {
  $('#threadList').innerHTML = '';
  $('#threadList').append(h('div', { class: 'muted small', style: 'padding:10px 12px;line-height:1.6' }, `在「${S.config.root}」還找不到產品資料夾。按上面的 ＋ 開一個新產品。`));
}
async function selectProject(name, threadId) {
  S.project = name; store.set('project', name);
  $('#projectSelect').value = name;
  $('#filesProject').textContent = name;
  await loadThreads();
  const t = (threadId && S.threads.find((x) => x.id === threadId)) ? threadId : S.threads[0]?.id;
  if (t) await openThread(t); else newThreadView();
  if (!$('[data-body=files]').classList.contains('hidden')) loadFiles();
}
async function newProject() {
  const name = await promptModal('開新產品', '產品名稱（會在總資料夾建立同名資料夾，含 CLAUDE.md、RevA、_codex）', '例如：羽球拍按壓吊飾');
  if (!name) return;
  try {
    const r = await api('/api/projects', { body: { name } });
    S.projects = r.projects; renderProjects(); await selectProject(r.name); toast(`已建立「${r.name}」`);
  } catch (e) { toast('建立失敗：' + e.message); }
}

// ---------------- 對話列表 ----------------
async function loadThreads() {
  const r = await api('/api/threads?project=' + encodeURIComponent(S.project));
  S.threads = r.threads; renderThreadList();
}
function renderThreadList(list = S.threads, isSearch = false) {
  const box = $('#threadList'); box.innerHTML = '';
  if (!list.length) { box.append(h('div', { class: 'muted small', style: 'padding:10px 12px' }, isSearch ? '找不到符合的對話' : '還沒有對話，直接在右邊輸入就會建立。')); return; }
  let g = null;
  for (const t of list) {
    if (!isSearch) { const gg = dayGroup(t.updated); if (gg !== g) { g = gg; box.append(h('div', { class: 'thread-group', text: g })); } }
    const on = S.thread?.id === t.id;
    box.append(h('button', { class: 'thread' + (on ? ' on' : ''), title: t.title, onclick: async () => {
      if (isSearch && t.project !== S.project) await selectProject(t.project, t.id); else await openThread(t.id);
      if (isSearch) { $('#searchInput').value = ''; renderThreadList(); }
    } },
      h('div', { class: 't1' }, (on && S.running) ? h('i', { class: 'spin' }) : null, h('span', { text: t.title || '新對話' })),
      h('div', { class: 't2', text: isSearch ? `${t.project}｜${t.snippet}` : `${fmtDate(t.updated)} ${fmtTime(t.updated)}・${t.preview || ''}` })));
  }
}
let searchT = null;
function onSearch() {
  clearTimeout(searchT);
  const q = $('#searchInput').value.trim();
  if (!q) return renderThreadList();
  searchT = setTimeout(async () => { const r = await api('/api/search?q=' + encodeURIComponent(q)); renderThreadList(r.results, true); }, 220);
}

// ---------------- 開啟對話 ----------------
function newThreadView() {
  closeStream();
  S.thread = null; S.running = false; S.msgEls.clear();
  $('#messages').innerHTML = ''; $('#messages').append(emptyEl());
  $('#threadTitle').value = ''; $('#threadTitle').placeholder = '新對話';
  $('#threadSub').textContent = S.project ? `產品：${S.project}` : '';
  setRunning(false); renderThreadList(); $('#input').focus();
}
let EMPTY_CLONE = null;
function emptyEl() { return document.getElementById('empty') || EMPTY_CLONE.cloneNode(true); }
async function openThread(id) {
  const r = await api('/api/thread?id=' + encodeURIComponent(id));
  S.thread = r.thread; store.set('thread.' + S.project, id);
  renderThread();
  await subscribe(id);
  setRunning(r.running);
  renderThreadList();
}
function renderThread() {
  const box = $('#messages'); box.innerHTML = ''; S.msgEls.clear();
  $('#threadTitle').value = S.thread.title || '';
  updateSub();
  if (!S.thread.messages.length) box.append(emptyEl());
  for (const m of S.thread.messages) addMsgEl(m, false);
  scrollBottom(true);
}
function updateSub() {
  if (!S.thread) return;
  const n = S.thread.messages.length;
  const sess = [S.thread.claudeSession ? 'Claude 有記憶' : null, S.thread.codexThread ? 'Codex 有記憶' : null].filter(Boolean).join('・');
  $('#threadSub').textContent = `產品：${S.thread.project}・${n} 則訊息${sess ? '・' + sess : ''}`;
}

// ---------------- 訊息 ----------------
function avatarFor(role) { return h('div', { class: 'avatar ' + role, text: role === 'claude' ? 'C' : role === 'codex' ? 'G' : '!' }); }
function addMsgEl(m, animate = true) {
  const box = $('#messages');
  $('#empty')?.remove();
  let el;
  if (m.role === 'user') {
    el = h('div', { class: 'msg user', 'data-id': m.id },
      h('div', { class: 'bubble' },
        h('div', { class: 'msg-head' }, m.mode && m.mode !== 'claude' ? h('span', { class: 'badge', text: { codex: '問 Codex', cross: '交叉驗證', both: '同時問' }[m.mode] }) : null, h('span', { class: 'time', text: fmtTime(m.ts) })),
        h('div', { class: 'md user-text' }),
        m.attachments?.length ? h('div', { class: 'attach-chips' }, m.attachments.map((a) => attachChip(a, false))) : null));
    $('.user-text', el).textContent = m.text;
  } else {
    const role = m.role === 'system' ? 'system' : m.role;
    el = h('div', { class: `msg agent ${role} status-${m.status}`, 'data-id': m.id, 'data-step': m.step || '', 'data-role': role },
      avatarFor(role),
      h('div', { class: 'bubble' },
        h('div', { class: 'msg-head' },
          h('span', { class: 'who', text: role === 'codex' ? 'Codex（GPT）' : NAMES[role] || role }),
          m.step ? h('span', { class: 'badge ' + role, text: m.step }) : null,
          h('span', { class: 'badge model', text: modelLabel(m) }),
          h('span', { class: 'time', text: fmtTime(m.ts) })),
        h('div', { class: 'card' },
          h('details', { class: 'tools hidden' }, h('summary'), h('ul', { class: 'tool-list' })),
          h('div', { class: 'md body' })),
        h('div', { class: 'msg-foot' })));
    if (!modelLabel(m)) $('.badge.model', el).remove();
  }
  if (!animate) el.style.animation = 'none';
  // 「同時問」並排
  const last = box.lastElementChild;
  if (m.step === '各自回答' && last?.classList.contains('msg') && last.dataset.step === '各自回答' && last.dataset.role !== m.role && !last.parentElement.classList.contains('pair')) {
    const pair = h('div', { class: 'pair' }); box.replaceChild(pair, last); pair.append(last, el);
  } else box.append(el);
  S.msgEls.set(m.id, { m, el });
  if (m.role !== 'user') { renderTools(m.id); renderBody(m.id); renderFoot(m.id); }
  return el;
}
function modelLabel(m) {
  if (m.role === 'claude') return shortModel(m.model) || '';
  if (m.role === 'codex') return [shortModel(m.model), m.effort].filter(Boolean).join(' · ');
  return '';
}
function shortModel(id) { if (!id) return ''; return String(id).replace(/^claude-/, '').replace(/-\d{8}$/, '').replace(/\[1m\]$/, ''); }
function renderBody(id) {
  const x = S.msgEls.get(id); if (!x) return;
  const body = $('.body', x.el);
  if (x.m.status === 'streaming' && !x.m.text) { body.innerHTML = '<div class="thinking"><i></i><i></i><i></i></div>'; return; }
  renderMd(body, x.m.text);
  if (x.m.status === 'streaming') { const last = body.lastElementChild || body; (last.tagName === 'PRE' || last.tagName === 'TABLE' ? body : last).append(h('span', { class: 'caret' })); }
  x.el.className = x.el.className.replace(/status-\S+/, 'status-' + x.m.status);
}
function renderTools(id) {
  const x = S.msgEls.get(id); if (!x) return;
  const d = $('.tools', x.el); const tools = x.m.tools || [];
  if (!tools.length) return;
  d.classList.remove('hidden');
  const run = tools.filter((t) => t.status === 'running').length, err = tools.filter((t) => t.status === 'error').length;
  $('summary', d).textContent = `用了 ${tools.length} 個工具${run ? `・執行中 ${run}` : ''}${err ? `・失敗 ${err}` : ''}${run ? '：' + (tools.filter((t) => t.status === 'running').pop().name) : ''}`;
  const ul = $('ul', d); ul.innerHTML = '';
  for (const t of tools) ul.append(h('li', { class: t.status }, h('i', { text: t.status === 'done' ? '✓' : t.status === 'error' ? '✕' : '●' }), h('b', { text: toolName(t.name) }), h('span', { text: t.detail || '', title: t.detail || '' })));
}
const TOOL_ZH = { Bash: '執行指令', Read: '讀檔', Write: '寫檔', Edit: '修改', MultiEdit: '修改', Glob: '找檔案', Grep: '搜尋內容', WebSearch: '網路搜尋', WebFetch: '讀網頁', Task: '子任務', Agent: '子任務', TodoWrite: '待辦', Skill: '技能', NotebookEdit: '筆記本' };
const toolName = (n) => TOOL_ZH[n] || n;
function renderFoot(id) {
  const x = S.msgEls.get(id); if (!x) return;
  const f = $('.msg-foot', x.el); f.innerHTML = '';
  if (x.m.status === 'streaming') { f.append(h('span', { text: '回覆中…' })); return; }
  if (x.m.status === 'stopped') f.append(h('span', { text: '已停止' }));
  if (x.m.status === 'error') f.append(h('span', { class: 'danger', text: '發生錯誤' }));
  const meta = x.m.meta || {};
  if (meta.durationMs) f.append(h('span', { text: '耗時 ' + fmtDur(meta.durationMs) }));
  const u = meta.usage; if (u) { const tok = (u.output_tokens || 0); if (tok) f.append(h('span', { text: `輸出 ${tok.toLocaleString()} tokens` })); }
  f.append(h('button', { text: '複製', onclick: () => { navigator.clipboard.writeText(x.m.text); toast('已複製'); } }));
  f.append(h('button', { text: '引用', title: '把這段回覆帶到輸入框', onclick: () => quote(x.m) }));
}
function quote(m) {
  const inp = $('#input'); const who = m.role === 'codex' ? 'Codex' : 'Claude';
  inp.value = `關於${who}這段：\n> ${m.text.slice(0, 600).split('\n').join('\n> ')}\n\n` + inp.value; autosize(); inp.focus();
}
const pending = new Set(); let raf = 0;
function scheduleRender(id) { pending.add(id); if (!raf) raf = requestAnimationFrame(() => { raf = 0; const stick = nearBottom(); for (const i of pending) renderBody(i); pending.clear(); if (stick) scrollBottom(); }); }
function nearBottom() { const b = $('#messages'); return b.scrollHeight - b.scrollTop - b.clientHeight < 140; }
function scrollBottom(force) { const b = $('#messages'); if (force || nearBottom()) b.scrollTop = b.scrollHeight; }

// ---------------- 即時串流 ----------------
function closeStream() { if (S.es) { S.es.close(); S.es = null; } }
function subscribe(id) {
  closeStream();
  return new Promise((resolve) => {
    let first = true;
    const es = new EventSource('/api/events?thread=' + encodeURIComponent(id));
    S.es = es;
    es.onmessage = async (e) => {
      const ev = JSON.parse(e.data);
      if (ev.kind === 'hello') {
        $('#connDot').classList.remove('off');
        if (first) { first = false; resolve(); }
        else { const r = await api('/api/thread?id=' + encodeURIComponent(id)).catch(() => null); if (r && S.thread?.id === id) { S.thread = r.thread; renderThread(); setRunning(r.running); } }
        return;
      }
      if (S.thread?.id !== id) return;
      onEvent(ev);
    };
    es.onerror = () => { $('#connDot').classList.add('off'); if (first) { first = false; resolve(); } };
  });
}
function onEvent(ev) {
  switch (ev.kind) {
    case 'run_start': setRunning(true); break;
    case 'run_end': setRunning(false); loadThreads(); updateSub(); setTimeout(() => refreshUsage(true), 1500); if (!$('[data-body=files]').classList.contains('hidden')) loadFiles(); break;
    case 'msg_start': {
      if (S.msgEls.has(ev.msg.id)) break;
      S.thread.messages.push(ev.msg); addMsgEl(ev.msg); scrollBottom(true); break;
    }
    case 'delta': { const x = S.msgEls.get(ev.msgId); if (!x) break; x.m.text += ev.text; scheduleRender(ev.msgId); break; }
    case 'meta': { const x = S.msgEls.get(ev.msgId); if (!x) break; if (ev.model) { x.m.model = ev.model; const head = $('.msg-head', x.el); let b = $('.badge.model', head); if (!b) { b = h('span', { class: 'badge model' }); head.insertBefore(b, $('.time', head)); } b.textContent = modelLabel(x.m); } break; }
    case 'tool': { const x = S.msgEls.get(ev.msgId); if (!x) break; const i = x.m.tools.findIndex((t) => t.id === ev.tool.id); if (i >= 0) x.m.tools[i] = ev.tool; else x.m.tools.push(ev.tool); renderTools(ev.msgId); if (nearBottom()) scrollBottom(); break; }
    case 'msg_end': {
      const x = S.msgEls.get(ev.msg.id); if (!x) break;
      const keepText = x.m.text.length > (ev.msg.text || '').length ? x.m.text : ev.msg.text;
      Object.assign(x.m, ev.msg, { text: keepText });
      const idx = S.thread.messages.findIndex((m) => m.id === ev.msg.id); if (idx >= 0) S.thread.messages[idx] = x.m;
      if (x.m.role !== 'user') { renderBody(ev.msg.id); renderTools(ev.msg.id); renderFoot(ev.msg.id); const b = $('.badge.model', x.el); if (b) b.textContent = modelLabel(x.m); }
      break;
    }
  }
}
function setRunning(v) {
  S.running = !!v;
  $('#sendBtn').classList.toggle('hidden', S.running);
  $('#stopBtn').classList.toggle('hidden', !S.running);
  $('#hint').textContent = S.running ? '執行中…可以先切到別的對話，回來會接著顯示。' : MODE_HINT[S.mode];
  renderThreadList();
}

// ---------------- 送出 ----------------
async function send(textOverride) {
  const inp = $('#input');
  const text = (textOverride ?? inp.value).trim();
  if (!text || S.running) return;
  if (!S.project) { toast('請先建立或選擇一個產品'); return; }
  try {
    if (!S.thread) {
      const r = await api('/api/threads', { body: { project: S.project } });
      S.thread = r.thread; S.msgEls.clear(); $('#messages').innerHTML = '';
      store.set('thread.' + S.project, S.thread.id);
      await subscribe(S.thread.id);
    }
    const body = { threadId: S.thread.id, text, mode: S.mode, attachments: S.attachments.map((a) => a.path),
      claudeModel: $('#claudeModel').value, codexModel: $('#codexModel').value, codexEffort: $('#codexEffort').value, rounds: Number($('#rounds').value) };
    inp.value = ''; autosize(); S.attachments = []; renderAttachments();
    setRunning(true);
    const r = await api('/api/send', { body });
    if (r.title) { S.thread.title = r.title; $('#threadTitle').value = r.title; }
    loadThreads();
  } catch (e) { setRunning(false); toast('送出失敗：' + e.message, 4000); }
}
async function stop() { if (S.thread) { await api('/api/stop', { body: { threadId: S.thread.id } }); toast('已要求停止'); } }

// ---------------- 附件 ----------------
async function uploadFiles(files) {
  if (!S.project) return toast('請先選擇產品');
  for (const f of files) {
    try {
      const name = f.name && f.name !== 'image.png' ? f.name : `pasted_${new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)}.png`;
      const r = await api(`/api/upload?project=${encodeURIComponent(S.project)}&name=${encodeURIComponent(name)}`, { body: f, raw: true });
      S.attachments.push({ path: r.path, preview: f.type.startsWith('image/') ? URL.createObjectURL(f) : null });
    } catch (e) { toast('上傳失敗：' + e.message); }
  }
  renderAttachments();
}
function attachChip(a, removable = true) {
  const p = typeof a === 'string' ? a : a.path;
  const isImg = /\.(png|jpe?g|webp|gif)$/i.test(p);
  const prev = typeof a === 'object' && a.preview ? a.preview : (isImg ? fileUrl(p) : null);
  return h('span', { class: 'chip', title: p },
    prev ? h('img', { src: prev, onclick: () => showImage(prev, p), style: 'cursor:zoom-in' }) : h('span', { text: '📄' }),
    h('span', { text: p.split('/').pop() }),
    removable ? h('button', { text: '✕', onclick: () => { S.attachments = S.attachments.filter((x) => x.path !== p); renderAttachments(); } }) : null);
}
function renderAttachments() { const row = $('#attachRow'); row.innerHTML = ''; for (const a of S.attachments) row.append(attachChip(a)); }

// ---------------- 模式與模型 ----------------
function setMode(m) {
  S.mode = m;
  for (const b of $$('#modeSeg button')) b.classList.toggle('on', b.dataset.mode === m);
  $('#claudePickerWrap').classList.toggle('hidden', m === 'codex');
  $('#codexPickerWrap').classList.toggle('hidden', m === 'claude');
  $('#roundsWrap').classList.toggle('hidden', m !== 'cross');
  if (!S.running) $('#hint').textContent = MODE_HINT[m];
}
function fillPickers() {
  const c = S.config, last = c.last || {};
  const cm = $('#claudeModel'); cm.innerHTML = '';
  for (const m of c.claudeModels) cm.append(h('option', { value: m.id, text: m.label || m.id || '預設' }));
  cm.value = last.claudeModel || ''; if (cm.selectedIndex < 0) cm.selectedIndex = 0;
  const xm = $('#codexModel'); xm.innerHTML = '';
  for (const m of c.codexModels) {
    let label = m.label || m.id || '預設';
    if (!m.id && S.doctor?.codex?.defaultModel) label = `預設（${S.doctor.codex.defaultModel}）`;
    xm.append(h('option', { value: m.id, text: label }));
  }
  xm.value = last.codexModel || ''; if (xm.selectedIndex < 0) xm.selectedIndex = 0;
  const xe = $('#codexEffort'); xe.innerHTML = '';
  const EZ = { '': '思考：預設', low: '思考：低', medium: '思考：中', high: '思考：高', xhigh: '思考：極高' };
  for (const e of c.codexEfforts || ['', 'low', 'medium', 'high']) xe.append(h('option', { value: e, text: EZ[e] || e }));
  xe.value = last.codexEffort || '';
  $('#rounds').value = String(last.rounds || 1);
}

// ---------------- 額度 ----------------
async function refreshUsage(force) {
  try { S.usage = await api('/api/usage' + (force ? '?force=1' : '')); renderUsage(); } catch (e) { $('#usageTime').textContent = '無法讀取額度：' + e.message; }
}
function meter(label, w) {
  if (!w) return null;
  const pct = Math.max(0, Math.min(100, Number(w.used) || 0));
  return h('div', { class: 'meter' },
    h('div', { class: 'row' }, h('span', { text: label }), h('b', { text: `已用 ${Math.round(pct)}%` })),
    h('div', { class: 'bar' }, h('i', { class: pct >= 85 ? 'high' : pct >= 60 ? 'mid' : '', style: `width:${pct}%` })),
    w.resetsAt ? h('div', { class: 'reset', text: untilText(w.resetsAt) }) : null);
}
function renderUsage() {
  const { claude, codex } = S.usage || {};
  const c = $('#usageClaude'); c.innerHTML = '';
  c.append(h('h4', {}, h('i', { class: 'dot claude' }), 'Claude', h('span', { class: 'plan', text: claude?.plan || '' })));
  if (claude?.ok) {
    c.append(meter('5 小時額度', claude.fiveHour), meter('每週額度（全部模型）', claude.weekly));
    const NM = { opus: '每週・Opus', sonnet: '每週・Sonnet', oauth_apps: '每週・其他應用' };
    for (const [k, v] of Object.entries(claude.weeklyByModel || {})) if (v) c.append(meter(NM[k] || '每週・' + k, v));
  } else c.append(h('div', { class: 'err', text: claude?.reason || '讀取中…' }));
  const x = $('#usageCodex'); x.innerHTML = '';
  x.append(h('h4', {}, h('i', { class: 'dot codex' }), 'Codex（ChatGPT）', h('span', { class: 'plan', text: codex?.plan || '' })));
  if (codex?.ok) {
    x.append(meter('5 小時額度', codex.fiveHour), meter('每週額度', codex.weekly));
    x.append(h('div', { class: 'note', text: `${codex.note || ''}：${new Date(codex.updated).toLocaleString('zh-TW', { hour12: false })}` }));
  } else x.append(h('div', { class: 'err', text: codex?.reason || '讀取中…' }));
  $('#usageTime').textContent = '更新於 ' + new Date().toLocaleTimeString('zh-TW', { hour12: false });
}

// ---------------- 檔案 ----------------
const FICON = (p, dir) => dir ? '📁' : /\.(png|jpe?g|webp|gif|svg)$/i.test(p) ? '🖼️' : /\.(step|stp|stl|3mf|obj)$/i.test(p) ? '🧊' : /\.(gcode|bgcode)$/i.test(p) ? '🖨️' : /\.(zip|7z)$/i.test(p) ? '🗜️' : /\.(md|txt|pdf|docx)$/i.test(p) ? '📄' : /\.(py|js|mjs|json|ini|csv)$/i.test(p) ? '⚙️' : '·';
const fsize = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;
async function loadFiles() {
  if (!S.project) return;
  try { const r = await api('/api/files?project=' + encodeURIComponent(S.project)); S.files = r.files; renderFiles(); } catch (e) { $('#fileTree').textContent = e.message; }
}
function renderFiles() {
  const box = $('#fileTree'); box.innerHTML = '';
  const q = $('#fileFilter').value.trim().toLowerCase();
  S.expanded = S.expanded || new Set();
  const visible = (f) => { const parts = f.path.split('/'); for (let i = 1; i < parts.length; i++) if (!S.expanded.has(parts.slice(0, i).join('/'))) return false; return true; };
  const list = q ? S.files.filter((f) => !f.dir && f.path.toLowerCase().includes(q)) : S.files.filter(visible);
  if (!list.length) { box.append(h('div', { class: 'muted small', text: q ? '沒有符合的檔案' : '資料夾是空的' })); return; }
  for (const f of list) {
    const name = q ? f.path : f.path.split('/').pop();
    const row = h('div', { class: 'frow' + (f.dir ? ' dir' : ''), style: `padding-left:${6 + (q ? 0 : f.depth * 14)}px`, title: f.path, onclick: () => {
      if (f.dir) { S.expanded = S.expanded || new Set(); S.expanded.has(f.path) ? S.expanded.delete(f.path) : S.expanded.add(f.path); renderFiles(); }
      else openFile(f.path);
    } },
      h('span', { text: f.dir ? (S.expanded?.has(f.path) ? '📂' : '📁') : FICON(f.path) }), h('span', { class: 'fn', text: name }),
      f.dir ? null : h('span', { class: 'fs', text: fsize(f.size) }),
      f.dir ? null : h('button', { class: 'fa', text: '附加', title: '附加到訊息', onclick: (e) => { e.stopPropagation(); if (!S.attachments.some((a) => a.path === f.path)) S.attachments.push({ path: f.path }); renderAttachments(); toast('已附加：' + f.path.split('/').pop()); } }),
      S.local ? h('button', { class: 'fa', text: '📂', title: '在檔案總管顯示', onclick: (e) => { e.stopPropagation(); api('/api/open', { body: { project: S.project, path: f.path } }); } })
        : (f.dir ? null : h('button', { class: 'fa', text: '⇩', title: '下載到這台裝置', onclick: (e) => { e.stopPropagation(); downloadFile(f.path); } })));
    box.append(row);
  }
}
function openFile(p) {
  if (/\.(png|jpe?g|webp|gif|svg)$/i.test(p)) return showImage(fileUrl(p), p);
  if (/\.(md|txt|py|js|mjs|json|ini|csv|log|svg|pdf)$/i.test(p)) return window.open(fileUrl(p), '_blank');
  if (!S.local) return downloadFile(p);
  api('/api/open', { body: { project: S.project, path: p } }).then(() => toast('已在檔案總管選取：' + p.split('/').pop()));
}
function downloadFile(p) { const a = h('a', { href: fileUrl(p) + '&dl=1', download: p.split('/').pop() }); document.body.append(a); a.click(); a.remove(); toast('下載中：' + p.split('/').pop()); }
function showImage(src, cap) { $('#viewerImg').src = src; $('#viewerCap').textContent = cap || ''; $('#viewer').classList.remove('hidden'); }

// ---------------- 環境狀態 ----------------
async function refreshDoctor() {
  try { S.doctor = await api('/api/doctor'); } catch (e) { $('#doctor').textContent = e.message; return; }
  const d = S.doctor; const box = $('#doctor'); box.innerHTML = '';
  const item = (ok, title, sub, fix) => h('div', { class: 'doc-item' }, h('span', { class: 'st ' + (ok === true ? 'ok' : ok === 'warn' ? 'warn' : 'bad'), text: ok === true ? '✓' : '!' }), h('div', {}, h('b', { text: title }), sub ? h('small', { text: sub }) : null, (!ok || ok === 'warn') && fix ? h('small', { html: fix }) : null));
  box.append(
    item(d.claude.installed && d.claude.loggedIn, 'Claude Code', d.claude.installed ? `${d.claude.version || ''}${d.claude.loggedIn ? '・已登入' : '・尚未登入'}` : '找不到 claude 指令', d.claude.installed ? '在終端機執行 <code>claude</code> 登入一次' : '安裝：<code>npm i -g @anthropic-ai/claude-code</code>'),
    item(d.codex.installed && d.codex.loggedIn ? true : d.codex.installed ? 'warn' : false, 'Codex CLI', d.codex.installed ? `${d.codex.version || ''}・${d.codex.status || ''}` : '找不到 codex 指令', d.codex.installed ? '在終端機執行 <code>codex login</code>（用 ChatGPT 帳號）' : '安裝：<code>npm i -g @openai/codex</code>'),
    item(d.rootExists, '產品總資料夾', d.root, '到「設定」改成正確的資料夾'),
    item(true, 'Node.js', d.node),
    item(true, '紀錄存放位置', d.dataDir));
  fillPickers();
}



// ---------------- 匯入產品 ----------------
async function importProject(file, nameOverride, merge) {
  toast(`上傳中：${file.name}（${fsize(file.size)}）…`, 60000);
  const q = new URLSearchParams(); if (nameOverride) q.set('name', nameOverride); if (merge) q.set('merge', '1');
  try {
    const r = await fetch('/api/import?' + q, { method: 'POST', body: file });
    const j = await r.json().catch(() => ({}));
    if (r.status === 409) {
      const ok = await confirmModal('已經有這個產品', `「${j.name}」已經存在。要把 zip 裡的檔案合併進去嗎？（同名檔案會被覆蓋）`, '合併');
      if (ok) return importProject(file, j.name, true); toast('已取消'); return;
    }
    if (!r.ok) throw new Error(j.error || r.status);
    S.projects = j.projects; renderProjects(); await selectProject(j.name); toast(`已匯入「${j.name}」，共 ${j.files} 個檔案`, 3500);
  } catch (e) { toast('匯入失敗：' + e.message, 5000); }
}

// ---------------- 帳號 ----------------
let acctPoll = null;
async function loadAccounts(quiet) {
  const box = $('#accountsBox');
  if (!quiet && !S.accounts) box.innerHTML = '<p class="muted small">檢查中…</p>';
  try { S.accounts = await api('/api/accounts'); } catch (e) { box.textContent = e.message; return; }
  renderAccounts();
  const waiting = ['claude', 'codex'].some((k) => S.accounts[k].login?.state === 'running' && !S.accounts[k].loggedIn);
  clearTimeout(acctPoll);
  if (waiting && !$('[data-body=accounts]').classList.contains('hidden')) acctPoll = setTimeout(() => loadAccounts(true), 3000);
}
async function acctAction(body, msg) {
  try { if (msg) toast(msg, 8000); S.accounts = await api('/api/accounts', { body }); renderAccounts(); loadAccounts(true); refreshUsage(true); } catch (e) { toast(e.message, 4000); }
}
async function acctUpload(kind, file) {
  try { const r = await fetch('/api/accounts/file?kind=' + kind, { method: 'POST', body: file }); const j = await r.json(); if (!r.ok) throw new Error(j.error); S.accounts = j; renderAccounts(); refreshUsage(true); toast('已匯入登入檔'); } catch (e) { toast('匯入失敗：' + e.message, 4000); }
}
function renderAccounts() {
  const A = S.accounts, box = $('#accountsBox'); box.innerHTML = '';
  box.append(h('p', { class: 'muted small', style: 'margin:0 0 12px;line-height:1.7' }, S.cloud ? '雲端主機用你自己的月費帳號登入 Claude 和 Codex（不是付費 API）。登入一次就會一直記住。' : '這台電腦上 Claude Code 和 Codex 的登入狀態。'));
  const fileBtn = (label, kind, accept) => { const inp = h('input', { type: 'file', accept, hidden: true, onchange: (e) => { const f = e.target.files[0]; e.target.value = ''; if (f) acctUpload(kind, f); } }); return h('span', {}, h('button', { class: 'ghost small', text: label, onclick: () => inp.click() }), inp); };

  // Claude
  const c = A.claude, cl = c.login;
  const cc = h('div', { class: 'acct' }, h('h4', {}, h('i', { class: 'dot claude' }), 'Claude', h('span', { class: 'pill ' + (c.loggedIn ? 'on' : 'off'), text: c.loggedIn ? '已登入' : '未登入' })));
  if (c.loggedIn) cc.append(h('p', { text: [c.email, c.plan && c.plan.toUpperCase(), c.method === 'token' ? '用長效登入碼' : null].filter(Boolean).join('・') || '可以使用' }));
  if (!c.installed) cc.append(h('p', { class: 'danger', text: '主機上找不到 Claude Code（安裝可能還沒完成，等幾分鐘再按重新檢查）。' }));
  if (S.cloud && c.installed) {
    if (cl?.state === 'running' && !c.loggedIn) {
      cc.append(h('p', { html: '<b>① 點下面按鈕</b>，用你的 Claude 帳號登入並按「授權」。' }));
      if (cl.url) cc.append(h('div', { class: 'row2' }, h('a', { class: 'btnlink', href: cl.url, target: '_blank', rel: 'noopener', text: '登入 Claude' })));
      else cc.append(h('p', { class: 'muted', text: '產生登入連結中…' }));
      const code = h('input', { placeholder: '把網頁上顯示的代碼貼在這裡' });
      cc.append(h('p', { html: '<b>② 授權後網頁會顯示一串代碼</b>，按複製，貼到下面再按送出。' }), code,
        h('div', { class: 'row2' }, h('button', { class: 'primary', text: '送出代碼', onclick: () => code.value.trim() ? acctAction({ action: 'code', code: code.value.trim() }, '驗證中…') : toast('請先貼上代碼') })));
    } else {
      if (cl?.state === 'failed' && !c.loggedIn) cc.append(h('p', { class: 'danger', text: '上次登入沒有成功，可以再試一次（詳細訊息在下面）。' }));
      cc.append(h('div', { class: 'row2' }, h('button', { class: c.loggedIn ? 'ghost small' : 'primary', text: c.loggedIn ? '重新登入' : '開始登入', onclick: () => acctAction({ action: 'start', kind: 'claude' }, '準備登入連結…') })));
    }
    if (cl?.output) cc.append(h('details', {}, h('summary', { text: '詳細訊息' }), h('pre', { text: cl.output })));
    const tok = h('input', { placeholder: 'sk-ant-oat01-…' });
    cc.append(h('details', {}, h('summary', { text: '其他登入方式' }),
      h('p', { html: '<b>A. 長效登入碼</b>：在任何一台有 Claude Code 的電腦執行 <code>claude setup-token</code>，把印出來的 sk-ant-oat 開頭的碼貼到這裡（有效約一年）。' }), tok,
      h('div', { class: 'row2' }, h('button', { class: 'ghost small', text: '儲存登入碼', onclick: () => acctAction({ action: 'token', token: tok.value }) }), c.method === 'token' ? h('button', { class: 'ghost small', text: '移除登入碼', onclick: () => acctAction({ action: 'cleartoken' }) }) : null),
      h('p', { html: '<b>B. 上傳登入檔</b>：家裡電腦的 <code>C:\\Users\\你的帳號\\.claude\\.credentials.json</code>。' }),
      h('div', { class: 'row2' }, fileBtn('⇪ 上傳 .credentials.json', 'claude', '.json'))));
  } else if (!S.cloud && !c.loggedIn) cc.append(h('p', { text: '在這台電腦打開終端機執行 claude 登入一次。' }));
  box.append(cc);

  // Codex
  const x = A.codex, xl = x.login;
  const xc = h('div', { class: 'acct' }, h('h4', {}, h('i', { class: 'dot codex' }), 'Codex（ChatGPT）', h('span', { class: 'pill ' + (x.loggedIn ? 'on' : 'off'), text: x.loggedIn ? '已登入' : '未登入' })));
  if (x.loggedIn) xc.append(h('p', { text: /api key/i.test(x.status || '') ? '用 API 金鑰登入（會另外計費）' : '用 ChatGPT 帳號登入' }));
  if (!x.installed) xc.append(h('p', { class: 'danger', text: '主機上找不到 Codex CLI（安裝可能還沒完成，等幾分鐘再按重新檢查）。' }));
  if (S.cloud && x.installed) {
    if (xl?.state === 'running' && !x.loggedIn) {
      xc.append(h('p', { html: '<b>① 打開下面的連結</b>，用你的 ChatGPT 帳號登入。' }));
      if (xl.url) xc.append(h('div', { class: 'row2' }, h('a', { class: 'btnlink', href: xl.url, target: '_blank', rel: 'noopener', text: '打開 ChatGPT 登入頁' })));
      if (xl.code) xc.append(h('p', { html: '<b>② 輸入這組代碼</b>（15 分鐘內有效）：' }), h('div', { class: 'bigcode', text: xl.code }), h('div', { class: 'row2' }, h('button', { class: 'ghost small', text: '複製代碼', onclick: () => { navigator.clipboard.writeText(xl.code); toast('已複製'); } })));
      if (!xl.url && !xl.code) xc.append(h('p', { class: 'muted', text: '產生登入代碼中…' }));
      xc.append(h('p', { class: 'muted', text: '完成後這裡會自動變成「已登入」。' }));
    } else {
      if (xl?.state === 'failed' && !x.loggedIn) xc.append(h('p', { class: 'danger', html: '上次登入沒有成功。如果訊息提到 device code，請到 ChatGPT 網頁「設定 → 安全性」開啟「Codex 的裝置代碼登入」後再試，或用下面的「上傳登入檔」。' }));
      xc.append(h('div', { class: 'row2' }, h('button', { class: x.loggedIn ? 'ghost small' : 'primary', text: x.loggedIn ? '重新登入' : '開始登入', onclick: () => acctAction({ action: 'start', kind: 'codex' }, '準備登入代碼…') })));
    }
    if (xl?.output) xc.append(h('details', {}, h('summary', { text: '詳細訊息' }), h('pre', { text: xl.output })));
    xc.append(h('details', {}, h('summary', { text: '其他登入方式' }),
      h('p', { html: '<b>上傳登入檔</b>：家裡電腦的 <code>C:\\Users\\你的帳號\\.codex\\auth.json</code>。' }),
      h('div', { class: 'row2' }, fileBtn('⇪ 上傳 auth.json', 'codex', '.json'))));
  } else if (!S.cloud && !x.loggedIn) xc.append(h('p', { text: '在這台電腦打開終端機執行 codex login 登入一次。' }));
  box.append(xc);
  box.append(h('button', { class: 'ghost small', text: '↻ 重新檢查', onclick: () => loadAccounts() }));
}

// ---------------- 遠端存取 ----------------
async function loadRemote() {
  const box = $('#remoteBox');
  if (!S.remote) box.innerHTML = '<p class="muted small">檢查中…</p>';
  try { S.remote = await api('/api/remote'); } catch (e) { box.textContent = e.message; return; }
  renderRemote();
}
function renderRemote(extra = {}) {
  const r = S.remote, box = $('#remoteBox'); box.innerHTML = '';
  box.append(h('p', { class: 'muted small', style: 'margin:0 0 12px;line-height:1.7' }, '讓你在公司或手機用瀏覽器連回家裡這台電腦的工作台。完全免費（Tailscale 個人版），公司電腦不用裝任何東西；家裡電腦要開著。'));
  if (!S.local) box.append(h('div', { class: 'step done' }, h('h4', {}, h('span', { class: 'n', text: '✓' }), '你現在就是從遠端連進來的'), h('p', { text: '要開關遠端存取或換密碼，請在家裡電腦操作。' })));

  // 1. 密碼
  const pw1 = h('input', { type: 'password', placeholder: '新密碼（至少 8 個字）', autocomplete: 'new-password' });
  const pw2 = h('input', { type: 'password', placeholder: '再輸入一次', autocomplete: 'new-password' });
  const cur = r.hasPassword && !S.local ? h('input', { type: 'password', placeholder: '目前的密碼' }) : null;
  box.append(h('div', { class: 'step' + (r.hasPassword ? ' done' : '') },
    h('h4', {}, h('span', { class: 'n', text: r.hasPassword ? '✓' : '1' }), '登入密碼', r.hasPassword ? h('span', { class: 'pill on', text: '已設定' }) : null),
    h('p', { text: r.hasPassword ? '從公司／手機連線時要輸入這組密碼。可以在下面更換。' : '先設定一組密碼，別人拿到網址也進不來。' }),
    cur, pw1, pw2,
    h('div', { class: 'row2' }, h('button', { class: 'primary', text: r.hasPassword ? '更換密碼' : '設定密碼', onclick: async () => {
      if (pw1.value !== pw2.value) return toast('兩次輸入的密碼不一樣');
      try { await api('/api/password', { body: { password: pw1.value, current: cur?.value } }); toast('密碼已設定'); S.hasPassword = true; loadRemote(); } catch (e) { toast(e.message, 3500); }
    } }))));

  // 2. Tailscale
  const tsOk = r.installed && r.running;
  const s2 = h('div', { class: 'step' + (tsOk ? ' done' : '') }, h('h4', {}, h('span', { class: 'n', text: tsOk ? '✓' : '2' }), '安裝並登入 Tailscale（免費）', tsOk ? h('span', { class: 'pill on', text: '已連線' }) : null));
  if (!r.installed) {
    s2.append(h('p', { html: '① 點下面按鈕，下載 <b>Windows</b> 版並安裝。<br>② 安裝好後，右下角工作列會出現 Tailscale 圖示，點它 → 登入（用 Google 帳號就可以）。<br>③ 回來這裡按「重新檢查」。' }),
      h('div', { class: 'row2' }, h('a', { class: 'btnlink', href: 'https://tailscale.com/download/windows', target: '_blank', rel: 'noopener', text: '下載 Tailscale' }), h('button', { class: 'ghost', text: '↻ 重新檢查', onclick: loadRemote })));
  } else if (!r.running) {
    s2.append(h('p', { html: `已安裝，但還沒登入（狀態：${esc(r.state)}）。<br>點右下角工作列的 Tailscale 圖示 → 登入，再回來按「重新檢查」。` }), h('div', { class: 'row2' }, h('button', { class: 'ghost', text: '↻ 重新檢查', onclick: loadRemote })));
  } else s2.append(h('p', { text: `這台電腦：${r.url || ''}` }));
  box.append(s2);

  // 3. 開關
  const on = r.enabled && r.funnelOn;
  const s3 = h('div', { class: 'step' + (on ? ' done' : '') }, h('h4', {}, h('span', { class: 'n', text: on ? '✓' : '3' }), '開啟遠端存取', h('span', { class: 'pill ' + (on ? 'on' : 'off'), text: on ? '開啟中' : '關閉' })));
  if (extra.needApprove) s3.append(h('p', { html: '第一次使用要在 Tailscale 網站按一次「允許 Funnel」：' }), h('div', { class: 'row2' }, h('a', { class: 'btnlink', href: extra.needApprove, target: '_blank', rel: 'noopener', text: '打開允許頁面' })), h('p', { text: '在那個頁面按允許後，回來再按一次「開啟遠端存取」。' }));
  if (extra.reason) s3.append(h('p', { class: 'danger', text: extra.reason }));
  if (on && r.url) {
    s3.append(h('p', { text: '在公司電腦或手機的瀏覽器打開這個網址，輸入密碼就能用：' }),
      h('div', { class: 'url-box' }, h('code', { text: r.url }), h('button', { class: 'ghost small', text: '複製', onclick: () => { navigator.clipboard.writeText(r.url); toast('已複製網址'); } })));
    try { const q = qrcode(0, 'M'); q.addData(r.url); q.make(); s3.append(h('div', { class: 'qr', html: q.createSvgTag({ cellSize: 4, margin: 2, scalable: true }) }), h('p', { class: 'muted', style: 'text-align:center', text: '手機相機掃描即可打開' })); } catch {}
    s3.append(h('p', { text: `${r.autostart ? '✓ 電腦開機後工作台會自動在背景啟動。' : '⚠ 開機自動啟動沒設定成功，重開機後要先開一次工作台。'}` }));
  }
  if (S.local) {
    const busy = h('button', { class: 'primary big-toggle' + (on ? ' off' : ''), text: on ? '關閉遠端存取' : '開啟遠端存取', disabled: (!r.hasPassword || !tsOk) && !on });
    busy.onclick = async () => {
      busy.disabled = true; busy.textContent = on ? '關閉中…' : '開啟中…（最多 30 秒）';
      try {
        const res = await api('/api/remote', { body: { action: on ? 'disable' : 'enable' } });
        if (res.status) S.remote = res.status;
        renderRemote(res.ok ? {} : { needApprove: res.needApprove, reason: res.reason });
        if (res.ok) toast(on ? '已關閉遠端存取' : '遠端存取已開啟');
      } catch (e) { toast(e.message, 4000); renderRemote(); }
    };
    s3.append(h('div', { class: 'row2' }, busy));
    if (!r.hasPassword || !tsOk) s3.append(h('p', { class: 'muted', text: '先完成上面兩步才能開啟。' }));
  }
  box.append(s3);

  // 4. 不睡眠
  if (S.local && on) box.append(h('div', { class: 'step' }, h('h4', {}, h('span', { class: 'n', text: '4' }), '別讓電腦睡著'),
    h('p', { text: '電腦睡眠時從外面就連不到。建議設定「插電時永不睡眠」（螢幕還是會自己關，省電）。' }),
    h('div', { class: 'row2' }, h('button', { class: 'ghost', text: '設定插電時不睡眠', onclick: async () => { const x = await api('/api/remote', { body: { action: 'nosleep' } }); toast(x.ok ? '已設定：插電時不會睡眠' : '設定失敗，請到 Windows 設定 → 電源 手動調整'); } }))));
}

// ---------------- 設定 ----------------
function fillSettings() {
  const c = S.config;
  $('#setRoot').value = c.root; $('#setPerm').value = c.claudePermission;
  $('#setClaudeModels').value = c.claudeModels.map((m) => `${m.id}|${m.label || ''}`).join('\n');
  $('#setCodexModels').value = c.codexModels.map((m) => `${m.id}|${m.label || ''}`).join('\n');
  $('#dataDir').textContent = '對話紀錄存在：' + (S.doctor?.dataDir || '使用者資料夾\\.makersense-workbench');
}
const parseModels = (s) => s.split('\n').map((l) => l.trim()).filter((l, i, a) => l || i === 0).map((l) => { const [id, ...rest] = l.split('|'); return { id: (id || '').trim(), label: rest.join('|').trim() }; }).filter((m, i) => m.id || m.label || i === 0);
async function saveSettings() {
  const body = { root: $('#setRoot').value.trim(), claudePermission: $('#setPerm').value, claudeModels: parseModels($('#setClaudeModels').value), codexModels: parseModels($('#setCodexModels').value) };
  if (!body.claudeModels.some((m) => !m.id)) body.claudeModels.unshift({ id: '', label: '預設模型' });
  if (!body.codexModels.some((m) => !m.id)) body.codexModels.unshift({ id: '', label: '預設模型' });
  const r = await api('/api/config', { body }); S.config = r.config; fillPickers(); fillSettings();
  const st = await api('/api/state'); S.projects = st.projects; renderProjects();
  if (!S.projects.find((p) => p.name === S.project)) { if (S.projects[0]) await selectProject(S.projects[0].name); else { S.project = null; showNoProject(); newThreadView(); } }
  refreshDoctor(); toast('設定已儲存');
}

// ---------------- 對話框 ----------------
function modal(title, bodyEl, okText = '確定') {
  return new Promise((resolve) => {
    $('#modalTitle').textContent = title; const b = $('#modalBody'); b.innerHTML = ''; b.append(bodyEl);
    $('#modalOk').textContent = okText; $('#modal').classList.remove('hidden');
    const done = (v) => { $('#modal').classList.add('hidden'); $('#modalOk').onclick = $('#modalCancel').onclick = null; resolve(v); };
    $('#modalOk').onclick = () => done(true); $('#modalCancel').onclick = () => done(false);
    const inp = $('input', b); if (inp) { setTimeout(() => inp.focus(), 30); inp.onkeydown = (e) => { if (e.key === 'Enter') done(true); if (e.key === 'Escape') done(false); }; }
  });
}
async function promptModal(title, desc, ph) {
  const inp = h('input', { placeholder: ph || '' });
  const ok = await modal(title, h('div', {}, h('p', { text: desc }), inp));
  return ok ? inp.value.trim() : null;
}
const confirmModal = (title, desc, okText) => modal(title, h('p', { text: desc }), okText);

// ---------------- 事件綁定 ----------------
function autosize() { const t = $('#input'); t.style.height = 'auto'; t.style.height = Math.min(240, t.scrollHeight) + 'px'; $('#sendBtn').disabled = !t.value.trim(); }
function bindUi() {
  $('#projectSelect').onchange = (e) => e.target.value && selectProject(e.target.value);
  $('#newProjectBtn').onclick = newProject;
  $('#newThreadBtn').onclick = newThreadView;
  $('#searchInput').oninput = onSearch;
  $('#themeBtn').onclick = () => { const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; applyTheme(t); api('/api/config', { body: { theme: t } }); };
  for (const b of $$('#modeSeg button')) b.onclick = () => setMode(b.dataset.mode);
  const inp = $('#input');
  if (matchMedia('(max-width: 760px)').matches) inp.placeholder = '輸入訊息…';
  inp.oninput = autosize; autosize();
  inp.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } };
  inp.onpaste = (e) => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); uploadFiles(fs); } };
  $('#sendBtn').onclick = () => send();
  $('#stopBtn').onclick = stop;
  $('#attachBtn').onclick = () => $('#fileInput').click();
  $('#fileInput').onchange = (e) => { uploadFiles([...e.target.files]); e.target.value = ''; };
  const comp = $('#composer');
  for (const t of ['dragenter', 'dragover']) document.addEventListener(t, (e) => { if (e.dataTransfer?.types?.includes('Files')) { e.preventDefault(); comp.classList.add('drag'); } });
  for (const t of ['dragleave', 'drop']) document.addEventListener(t, (e) => { if (t === 'dragleave' && e.relatedTarget) return; comp.classList.remove('drag'); });
  document.addEventListener('drop', (e) => { if (e.dataTransfer?.files?.length) { e.preventDefault(); uploadFiles([...e.dataTransfer.files]); } });
  $('#messages').addEventListener('click', (e) => { const tip = e.target.closest('.tip'); if (tip) { if (tip.dataset.mode) setMode(tip.dataset.mode); $('#input').value = tip.dataset.tip; autosize(); $('#input').focus(); } });

  $('#threadTitle').onchange = async (e) => { if (!S.thread) return; const r = await api('/api/thread', { method: 'PATCH', body: { id: S.thread.id, title: e.target.value.trim() || '新對話' } }); S.thread.title = r.thread.title; loadThreads(); };
  $('#threadTitle').onkeydown = (e) => { if (e.key === 'Enter') e.target.blur(); };
  $('#exportBtn').onclick = async () => { if (!S.thread) return toast('還沒有對話'); const r = await api('/api/export', { body: { threadId: S.thread.id } }); toast('已存到產品資料夾：' + r.path, 3500); if (!S.local) downloadFile(r.path); };
  $('#openFolderBtn').onclick = () => S.project && api('/api/open', { body: { project: S.project } });
  $('#moreBtn').onclick = (e) => { e.stopPropagation(); $('#moreMenu').classList.toggle('hidden'); };
  document.addEventListener('click', () => $('#moreMenu').classList.add('hidden'));
  $('#moreMenu').onclick = async (e) => {
    const act = e.target.dataset.act; if (!act || !S.thread) return;
    if (act === 'reset' && await confirmModal('重新開始記憶', 'Claude 和 Codex 會忘記這個對話之前說過的話（畫面上的紀錄會保留）。適合對話太長、或想讓它們重新看檔案時使用。', '重新開始')) {
      await api('/api/thread', { method: 'PATCH', body: { id: S.thread.id, resetSessions: true } }); S.thread.claudeSession = S.thread.codexThread = null; updateSub(); toast('已重新開始');
    }
    if (act === 'delete' && await confirmModal('刪除對話', `確定刪除「${S.thread.title}」？（產品資料夾裡的檔案不會被刪）`, '刪除')) {
      await api('/api/thread?id=' + encodeURIComponent(S.thread.id), { method: 'DELETE' }); store.set('thread.' + S.project, null); await loadThreads(); S.threads[0] ? openThread(S.threads[0].id) : newThreadView(); toast('已刪除');
    }
  };
  for (const b of $$('#tabs button')) b.onclick = () => {
    for (const x of $$('#tabs button')) x.classList.toggle('on', x === b);
    for (const s of $$('.tab-body')) s.classList.toggle('hidden', s.dataset.body !== b.dataset.tab);
    if (b.dataset.tab === 'files') loadFiles(); if (b.dataset.tab === 'usage') refreshUsage(); if (b.dataset.tab === 'settings') fillSettings(); if (b.dataset.tab === 'remote') loadRemote(); if (b.dataset.tab === 'accounts') loadAccounts();
  };
  $('#usageRefresh').onclick = () => refreshUsage(true);
  $('#filesRefresh').onclick = loadFiles;
  $('#filesOpen').onclick = () => S.project && api('/api/open', { body: { project: S.project } });
  $('#fileFilter').oninput = renderFiles;
  $('#doctorRefresh').onclick = refreshDoctor;
  $('#saveSettings').onclick = () => saveSettings().catch((e) => toast('儲存失敗：' + e.message));
  $('#viewer').onclick = () => $('#viewer').classList.add('hidden');
  const closeDrawers = () => { document.body.classList.remove('side-open', 'panel-open'); $('#scrim').classList.add('hidden'); };
  $('#sideToggle').onclick = () => { document.body.classList.add('side-open'); $('#scrim').classList.remove('hidden'); };
  $('#panelToggle').onclick = () => { document.body.classList.add('panel-open'); $('#scrim').classList.remove('hidden'); };
  $('#scrim').onclick = closeDrawers;
  $('#threadList').addEventListener('click', (e) => { if (e.target.closest('.thread')) closeDrawers(); });
  $('#newThreadBtn').addEventListener('click', closeDrawers);
  $('#importBtn').onclick = () => $('#importInput').click();
  $('#importInput').onchange = (e) => { const f = e.target.files[0]; e.target.value = ''; if (f) importProject(f); };
  $('#exportProjectBtn').onclick = () => { if (!S.project) return; const a = h('a', { href: '/api/export-project?project=' + encodeURIComponent(S.project) }); document.body.append(a); a.click(); a.remove(); toast('打包中，馬上開始下載…'); };
  $('#pwChange').onclick = async () => {
    if ($('#pwNew1').value !== $('#pwNew2').value) return toast('兩次輸入的新密碼不一樣');
    try { await api('/api/password', { body: { current: $('#pwCur').value, password: $('#pwNew1').value } }); toast('密碼已更換'); for (const id of ['#pwCur', '#pwNew1', '#pwNew2']) $(id).value = ''; } catch (e) { toast(e.message, 3500); }
  };
  $('#updateBtn').onclick = () => $('#updateInput').click();
  $('#updateInput').onchange = async (e) => {
    const f = e.target.files[0]; e.target.value = ''; if (!f) return;
    if (!await confirmModal('更新工作台', `要用「${f.name}」更新工作台嗎？更新後會自動重新啟動，約 10 秒。`, '更新')) return;
    try { const r = await api('/api/update', { body: f, raw: true }); toast(`已更新到 ${r.version}，重新啟動中…`, 5000); setTimeout(() => location.reload(), 9000); } catch (err) { toast('更新失敗：' + err.message, 5000); }
  };
  $('#logoutBtn').onclick = async () => { await api('/api/logout', { body: {} }); location.href = '/login.html'; };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $('#viewer').classList.add('hidden');
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') { e.preventDefault(); newThreadView(); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('#searchInput').focus(); }
  });
}

init().catch((e) => { document.body.innerHTML = `<div style="padding:40px;font-family:sans-serif"><h2>工作台啟動失敗</h2><p>${esc(e.message)}</p><p>請關掉視窗後再從桌面捷徑開一次。</p></div>`; });
