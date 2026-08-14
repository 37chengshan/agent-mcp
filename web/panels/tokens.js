/* ============================================================
 * Agent MCP · Token 用量面板（tokens）
 * 完整用量页：全局汇总卡 + 按 agent 明细表 + 成本占比条形图。
 * 数据源：GET /api/snapshot（usage.totals + usage.per_agent）+ agents join。
 * 接口：{ mount(container, sse, opts), unmount(), setVisible() }。
 * ============================================================ */

const POLL_MS = 5000;

let root = null, pollTimer = null;
let disposed = true, visible = true, renderPending = false;
let lastFp = "";

/* ---------- 小工具 ---------- */

function esc(v){ return String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function fmtInt(n){ return Number(n || 0).toLocaleString("zh-CN"); }
function fmtUsd(v){ return "$" + (Number(v) || 0).toFixed(2); }
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
  const d = window.__amTok || {};
  const agents = d.agents || [], usage = d.usage || {};
  const totals = usage.totals || {};
  const per = usage.per_agent || [];

  const fp = `${totals.input_tokens}|${totals.output_tokens}|${totals.cost_usd}|${per.length}|${agents.length}`;
  if(fp === lastFp) return;
  lastFp = fp;

  // 全局汇总卡
  const totalTok = (totals.input_tokens||0) + (totals.output_tokens||0);
  const et = (totals.input_tokens||0) - (totals.cache_read||0) * 0.9 + (totals.output_tokens||0) * 4;
  const stats = [
    { k:"输入", v:fmtInt(totals.input_tokens||0), cls:"in" },
    { k:"输出", v:fmtInt(totals.output_tokens||0), cls:"out" },
    { k:"缓存读", v:fmtInt(totals.cache_read||0), cls:"cache" },
    { k:"缓存写", v:fmtInt(totals.cache_creation||0), cls:"cache" },
    { k:"总 Token", v:fmtInt(totalTok), cls:"sum" },
    { k:"成本", v:fmtUsd(totals.cost_usd||0), cls:"cost" },
    { k:"ET 有效", v:fmtInt(Math.round(et)), cls:"et" },
  ];
  root.querySelector(".am-tok-stats").innerHTML = stats.map(s => `
    <div class="am-tok-stat ${s.cls}"><b>${esc(s.v)}</b><span>${esc(s.k)}</span></div>`).join("");

  // per-agent 明细（join agents 拿 task_name/cli；按 cost 降序）
  const rows = per.map(u => {
    const a = agents.find(x => x.id === u.agent_id) || {};
    return { id: u.agent_id, task: a.task_name || "", cli: a.cli || "?",
             status: a.status || "", ...u };
  }).sort((x, y) => (y.cost_usd||0) - (x.cost_usd||0));
  const maxCost = Math.max(1, ...rows.map(r => r.cost_usd||0));

  const tbody = root.querySelector(".am-tok-table tbody");
  tbody.innerHTML = rows.length ? rows.map(r => `
    <tr>
      <td class="am-tok-id">#${r.id}</td>
      <td class="am-tok-task" title="${esc(r.task)}">
        <span class="am-cli" style="background:${cliColor(r.cli)}">${esc(r.cli)}</span>
        ${esc(r.task) || "—"}
      </td>
      <td class="am-tok-num">${fmtInt(r.input_tokens||0)}</td>
      <td class="am-tok-num">${fmtInt(r.output_tokens||0)}</td>
      <td class="am-tok-num">${fmtInt(r.cache_read||0)}</td>
      <td class="am-tok-num">${fmtUsd(r.cost_usd||0)}</td>
      <td class="am-tok-bar-cell"><div class="am-tok-bar"><i style="width:${Math.round((r.cost_usd||0)/maxCost*100)}%"></i></div></td>
    </tr>`).join("")
    : '<tr><td colspan="7" class="am-empty">暂无用量数据</td></tr>';

  root.querySelector(".am-tok-count").textContent = `共 ${rows.length} 个 agent`;
}

/* ---------- 数据 ---------- */

async function poll(){
  if(disposed) return;
  try{
    const d = await apiFetch("/api/snapshot");
    if(disposed) return;
    window.__amTok = d;
    render();
  }catch(err){
    if(disposed) return;
    const box = root.querySelector(".am-err");
    if(box) box.textContent = "用量数据拉取失败：" + err.message;
    else root.insertAdjacentHTML("afterbegin", `<div class="am-err">用量数据拉取失败：${esc(err.message)}</div>`);
  }
}

/* ---------- 面板接口 ---------- */

export function mount(container, sse, opts){
  unmount();
  disposed = false; visible = true; renderPending = false; lastFp = "";
  root = document.createElement("div");
  root.className = "am-panel";
  root.innerHTML = `
    <div class="am-dk">全局用量</div>
    <div class="am-tok-stats"></div>
    <div class="am-dk">按 Agent 明细 <span class="am-tok-count"></span></div>
    <table class="am-tok-table">
      <thead><tr>
        <th>ID</th><th>任务 / CLI</th><th>输入</th><th>输出</th><th>缓存读</th><th>成本</th><th>占比</th>
      </tr></thead>
      <tbody></tbody>
    </table>`;
  container.appendChild(root);
  poll();
  pollTimer = setInterval(poll, POLL_MS);
}

export function unmount(){
  disposed = true; visible = true; renderPending = false;
  if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
  if(root){ root.remove(); root = null; }
}

export function setVisible(v){
  visible = !!v;
  if(visible && renderPending){ lastFp = ""; render(); }
}
