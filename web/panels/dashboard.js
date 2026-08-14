/* ============================================================
 * Agent MCP · 总览面板（dashboard）
 * 完整仪表盘首页：全局统计卡片 + 运行中 agent + 最近活动流。
 * 数据源：GET /api/snapshot（agents + events + usage totals/per_agent）。
 * 接口：{ mount(container, sse, opts), unmount(), setVisible() }。
 * ============================================================ */

const POLL_MS = 5000;
const MAX_EVENTS = 12;

let root = null, unsubs = null, pollTimer = null;
let disposed = true, visible = true, renderPending = false;
let lastFp = "";

/* ---------- 小工具 ---------- */

function esc(v){ return String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtInt(n){ return Number(n || 0).toLocaleString("zh-CN"); }
function fmtUsd(v){ return "$" + (Number(v) || 0).toFixed(2); }
function fmtTime(ts){
  if(ts == null) return "—";
  const n = (typeof ts === "number" || /^\d+$/.test(String(ts))) ? Number(ts) : Date.parse(ts);
  if(!Number.isFinite(n)) return String(ts);
  const d = new Date(n);
  const p = x => String(x).padStart(2,"0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function authToken(){
  if(window.__amToken) return window.__amToken;
  const m = (location.hash || "").match(/token=([^&]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}
async function apiFetch(path){
  const headers = {};
  const t = authToken();
  if(t) headers["X-Auth-Token"] = t;
  const r = await fetch(path, { headers });
  if(!r.ok) throw new Error(`${path} HTTP ${r.status}`);
  return r.json().catch(() => ({}));
}

/* 状态类/标签（与 index.html 对齐） */
const ST_LABEL = { running:"运行中", terminated:"完成", queued:"排队", error:"失败",
  cancelled:"已取消", incomplete:"超时/失联", needs_advisor:"需决策", idle:"空闲" };
const ST_CLS = { running:"run", terminated:"ok", error:"err", cancelled:"err",
  incomplete:"warn", needs_advisor:"warn", queued:"soft", idle:"soft" };

const EV_LABEL = {
  "agent.spawned":"创建","agent.user_turn":"用户回合","agent.running":"开始运行",
  "agent.message":"消息","agent.tool_use":"工具调用","agent.tool_result":"工具结果",
  "agent.usage":"用量","agent.terminated":"完成","agent.error":"失败",
  "agent.cancelled":"取消","agent.orphaned":"失联","agent.needs_advisor":"需决策",
  "agent.idle":"空闲","agent.verify_failed":"验证失败","agent.verify_passed":"验证通过",
  "agent.budget_downgrade":"降档","agent.ingest_failed":"解析失败",
};

function cliColor(cli){
  const m = { grok:"var(--grok,#C9A34F)", opencode:"var(--opencode,#6FA587)",
    omp:"var(--omp,#9A8EDA)", atomcode:"var(--atomcode,#5A9CD6)",
    codex:"var(--codex,#7FB5A0)", kimi:"var(--kimi,#C98A5A)" };
  return m[String(cli||"").toLowerCase()] || "var(--claude,#C87A5A)";
}

/* ---------- 渲染 ---------- */

function render(){
  if(disposed || !root) return;
  if(!visible){ renderPending = true; return; }
  renderPending = false;
  const d = window.__amDash || {};
  const agents = d.agents || [], usage = d.usage || {}, events = d.events || [];
  const totals = usage.totals || {};

  const nRunning = agents.filter(a => a.status === "running").length;
  const nTerm = agents.filter(a => a.status === "terminated").length;
  const nBad = agents.filter(a => ["error","cancelled","incomplete","needs_advisor"].includes(a.status)).length;
  const nQueued = agents.filter(a => a.status === "queued").length;
  const totalTok = (totals.input_tokens||0) + (totals.output_tokens||0);
  const cost = totals.cost_usd || 0;

  const fp = `${agents.length}|${nRunning}|${cost}|${totalTok}|${events.length}|${(events[0]||{}).seq}`;
  if(fp === lastFp) return;
  lastFp = fp;

  // 统计卡片
  const cards = [
    { k:"总 Agent", v:fmtInt(agents.length), cls:"", sub:"本会话" },
    { k:"运行中", v:fmtInt(nRunning), cls:"run", sub:`排队 ${nQueued}`, live: nRunning > 0 },
    { k:"已完成", v:fmtInt(nTerm), cls:"ok", sub:"end_turn" },
    { k:"异常", v:fmtInt(nBad), cls: nBad ? "err" : "ok", sub: nBad ? "error/cancelled/timeout" : "无" },
    { k:"总 Token", v:fmtInt(totalTok), cls:"", sub:`输入 ${fmtInt(totals.input_tokens||0)} · 输出 ${fmtInt(totals.output_tokens||0)}` },
    { k:"总成本", v:fmtUsd(cost), cls: cost > 0 ? "" : "soft", sub:`缓存读 ${fmtInt(totals.cache_read||0)}` },
  ];
  root.querySelector(".am-dash-cards").innerHTML = cards.map(c => `
    <div class="am-dash-card ${c.cls}">
      <div class="am-dash-card-v ${c.live ? "am-live" : ""}">${esc(c.v)}</div>
      <div class="am-dash-card-k">${esc(c.k)}</div>
      <div class="am-dash-card-s">${esc(c.sub)}</div>
    </div>`).join("");

  // 运行中 agent
  const running = agents.filter(a => a.status === "running" || a.status === "queued");
  root.querySelector(".am-dash-running").innerHTML = running.length ? running.map(a => `
    <div class="am-dash-run" data-id="${a.id}">
      <span class="am-dot-live" title="active"></span>
      <span class="am-cli" style="background:${cliColor(a.cli)}">${esc(a.cli)}</span>
      <span class="am-dash-run-t" title="${esc(a.task_name)}">${esc(a.task_name) || `#${a.id}`}</span>
      <span class="am-badge ${ST_CLS[a.status]||"soft"}">${esc(ST_LABEL[a.status]||a.status)}</span>
    </div>`).join("")
    : '<div class="am-empty">当前无运行中 agent</div>';

  // 最近活动流
  const evs = [...events].slice(-MAX_EVENTS).reverse();
  root.querySelector(".am-dash-events").innerHTML = evs.length ? evs.map(e => {
    const p = e.payload || {};
    const agent = agents.find(a => a.id === e.agent_id);
    let text = "";
    if(e.type === "agent.message" || e.type === "agent.user_turn") text = String(p.text||"").slice(0,60);
    else if(e.type === "agent.tool_use") text = (p.name||"tool") + (p.file ? " · " + p.file : "");
    else if(e.type === "agent.terminated") text = p.stop_reason || "";
    else if(e.type === "agent.usage") text = (p.input_tokens||0) + " in / " + (p.output_tokens||0) + " out";
    return `<div class="am-ev-row">
      <span class="am-ev-time">${fmtTime(e.created_at)}</span>
      <span class="am-ev-type">${esc(EV_LABEL[e.type] || e.type)}</span>
      <span class="am-ev-agent" style="color:${cliColor(agent?.cli)}">${esc(agent ? (agent.task_name || "#"+e.agent_id) : "#"+e.agent_id)}</span>
      <span class="am-ev-text">${esc(text || "")}</span>
    </div>`;
  }).join("") : '<div class="am-empty">暂无活动</div>';
}

/* ---------- 数据 ---------- */

async function poll(){
  if(disposed) return;
  try{
    const d = await apiFetch("/api/snapshot");
    if(disposed) return;
    window.__amDash = d;
    render();
  }catch(err){
    if(disposed) return;
    const box = root.querySelector(".am-err");
    if(box) box.textContent = "总览数据拉取失败：" + err.message;
    else root.insertAdjacentHTML("afterbegin", `<div class="am-err">总览数据拉取失败：${esc(err.message)}</div>`);
  }
}

/* ---------- 面板接口 ---------- */

export function mount(container, sse, opts){
  unmount();
  disposed = false; visible = true; renderPending = false; lastFp = "";
  unsubs = new Set();
  root = document.createElement("div");
  root.className = "am-panel";
  root.innerHTML = `
    <div class="am-dash-cards"></div>
    <div class="am-dk">运行中</div>
    <div class="am-dash-running"></div>
    <div class="am-dk">最近活动</div>
    <div class="am-dash-events"></div>`;
  container.appendChild(root);
  poll();
  pollTimer = setInterval(poll, POLL_MS);
}

export function unmount(){
  disposed = true; visible = true; renderPending = false;
  if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
  if(unsubs){ for(const fn of unsubs) fn(); unsubs = null; }
  if(root){ root.remove(); root = null; }
}

export function setVisible(v){
  visible = !!v;
  if(visible && renderPending){ lastFp = ""; render(); }
}
