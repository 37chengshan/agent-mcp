/* ============================================================
 * Agent MCP · 总览面板（v4 Hero 仪表盘）
 * Hero 统计卡（带 sparkline）+ 双栏：左=运行泳道+活动时间线；
 * 右=预算环 + Token 构成 Donut。
 * 数据源：/api/snapshot + /api/usage/series?hours=24 + /api/policies/state。
 * 预算环从 policies/state 的 policy_configs.budget_limit_usd /
 * budget_usd / spent_usd 计算真实百分比（不再依赖 window.__amBudget 单向注入）。
 * import 版本必须与 loader.js PANEL_V 一致（components.js?v=v7）。
 * ============================================================ */

import { esc, fmtInt, fmtUsd, fmtTime, apiFetch, cliColor, ST_LABEL, ST_CLS,
         statCard, sparkline, donut, timeline, emptyState,
         toast, mapSeries, pickBudget, isDeadEvent, skeletonCards } from "./components.js?v=v7";

const POLL_MS = 5000;

let root = null, pollTimer = null;
let disposed = true, visible = true, renderPending = false;
let lastFp = "";
let firstPaint = true;
let dash = {}, series = [];
let budget = { limit_usd: 0, spent_usd: 0, budget_usd: 0 };
let errBox = null;

const EV_LABEL = {
  "agent.spawned":"创建","agent.user_turn":"用户回合","agent.running":"开始运行",
  "agent.message":"消息","agent.tool_use":"工具调用","agent.tool_result":"工具结果",
  "agent.usage":"用量","agent.terminated":"完成","agent.error":"失败",
  "agent.cancelled":"取消","agent.orphaned":"失联","agent.needs_advisor":"需决策",
  "agent.idle":"空闲","agent.verify_failed":"验证失败","agent.verify_passed":"验证通过",
  "agent.budget_downgrade":"降档","agent.ingest_failed":"解析失败",
};

/* ---------- 渲染 ---------- */

function render(){
  if(disposed || !root) return;
  if(!visible){ renderPending = true; return; }
  renderPending = false;
  const agents = dash.agents || [], usage = dash.usage || {};
  const events = (dash.events || []).filter(e => !isDeadEvent(e.type));
  const totals = usage.totals || {};

  const nRun = agents.filter(a => a.status === "running").length;
  const nTerm = agents.filter(a => a.status === "terminated").length;
  const nBad = agents.filter(a => ["error","cancelled","incomplete","needs_advisor"].includes(a.status)).length;
  const totalTok = (totals.input_tokens||0) + (totals.output_tokens||0);
  const cost = totals.cost_usd || 0;
  const inS = series.map(s => s.in || 0);
  const costS = series.map(s => s.cost || 0);

  const limit = budget.limit_usd, spent = budget.spent_usd || budget.budget_usd;
  const fp = `${agents.length}|${nRun}|${cost}|${totalTok}|${events.length}|${inS[inS.length-1]}|${limit}|${spent}`;
  if(fp === lastFp) return;
  lastFp = fp;
  firstPaint = false;
  clearError();

  const cards = [
    statCard({ k:"总 Agent", v:fmtInt(agents.length), sub:"本会话" }),
    statCard({ k:"运行中", v:fmtInt(nRun), cls:"run", live:nRun>0,
      sub:`排队 ${agents.filter(a=>a.status==="queued").length}`, spark: inS }),
    statCard({ k:"已完成", v:fmtInt(nTerm), cls:"ok", sub:"end_turn" }),
    statCard({ k:"异常", v:fmtInt(nBad), cls:nBad?"err":"ok", sub:nBad?"error/cancelled/timeout":"无" }),
    statCard({ k:"总 Token", v:fmtInt(totalTok), sub:`输入 ${fmtInt(totals.input_tokens||0)} · 输出 ${fmtInt(totals.output_tokens||0)}`,
      spark: inS }),
    statCard({ k:"总成本", v:fmtUsd(cost), cls:cost>0?"":"soft", sub:`缓存读 ${fmtInt(totals.cache_read||0)}`,
      spark: costS }),
  ];
  root.querySelector(".am-dash-cards").innerHTML = cards.join("");

  const running = agents.filter(a => a.status === "running" || a.status === "queued");
  root.querySelector(".am-run-list").innerHTML = running.length ? running.map((a, i) => `
    <div class="am-run-card am-row-in" data-id="${a.id}" style="animation-delay:${Math.min(i * 40, 200)}ms">
      <span class="am-run-bar" style="background:${cliColor(a.cli)}"></span>
      <span class="am-cli" style="background:${cliColor(a.cli)}">${esc(a.cli || "—")}</span>
      <span class="am-run-task" title="${esc(a.task_name||"")}">${esc(a.task_name) || `#${a.id}`}</span>
      <span class="am-badge ${ST_CLS[a.status]||"soft"}">${esc(ST_LABEL[a.status]||a.status)}</span>
    </div>`).join("") : emptyState("当前无运行中 agent");

  const evs = [...events].slice(-10).reverse();
  root.querySelector(".am-ev-tl").innerHTML = evs.length ? timeline(evs.map(e => {
    const p = e.payload || {};
    const agent = agents.find(a => a.id === e.agent_id);
    let text = "";
    if(e.type === "agent.message" || e.type === "agent.user_turn") text = String(p.text||"").slice(0,58);
    else if(e.type === "agent.tool_use") text = (p.name||"tool") + (p.file ? " · "+p.file : "");
    else if(e.type === "agent.terminated") text = p.stop_reason || "";
    else if(e.type === "agent.usage") text = `${fmtInt(p.input_tokens||0)} in / ${fmtInt(p.output_tokens||0)} out`;
    return { ts:e.created_at, type:EV_LABEL[e.type]||e.type,
             agent: agent ? (agent.task_name || "#"+e.agent_id) : "#"+e.agent_id, text,
             color:cliColor(agent?.cli) };
  })) : emptyState("暂无活动");

  // 预算环：真实百分比
  const pct = limit > 0 ? Math.min(spent / limit * 100, 100) : 0;
  const over = limit > 0 && spent > limit;
  const warn = !over && limit > 0 && pct >= 80;
  const RING_R = 56, RING_C = 2 * Math.PI * RING_R;
  const fg = root.querySelector(".am-budget-big .am-ring-fg");
  fg.setAttribute("stroke-dasharray",
    `${(pct/100*RING_C).toFixed(1)} ${RING_C.toFixed(1)}`);
  fg.classList.toggle("over", over);
  fg.classList.toggle("warn", warn);
  const pctB = root.querySelector(".am-budget-big .am-budget-num b");
  pctB.textContent = limit > 0 ? Math.round(pct)+"%" : "—";
  pctB.classList.toggle("over", over);
  root.querySelector(".am-budget-big .am-budget-num span").textContent =
    limit > 0 ? `${fmtUsd(spent)} / ${fmtUsd(limit)}` : "未设置预算上限";

  // Token 构成 Donut
  const don = donut({ size:110, stroke:13, slices: [
    { value:totals.input_tokens||0, color:"var(--green,#6FA587)", label:"输入" },
    { value:totals.output_tokens||0, color:"var(--accent,#D96B4F)", label:"输出" },
    { value:totals.cache_read||0, color:"var(--amber,#C9A34F)", label:"缓存读" },
  ]});
  root.querySelector(".am-donut-box").innerHTML = totalTok ? don + `
    <div class="am-donut-legend">
      <span><i style="background:var(--green)"></i>输入 ${fmtInt(totals.input_tokens||0)}</span>
      <span><i style="background:var(--accent)"></i>输出 ${fmtInt(totals.output_tokens||0)}</span>
      <span><i style="background:var(--amber)"></i>缓存读 ${fmtInt(totals.cache_read||0)}</span>
    </div>` : emptyState("暂无 Token 数据");
}

function showError(msg){
  if(!root) return;
  clearError();
  errBox = document.createElement("div");
  errBox.className = "am-err";
  errBox.textContent = msg;
  root.insertBefore(errBox, root.querySelector(".am-dash-cards"));
  toast(msg, "error");
}
function clearError(){
  if(errBox){ errBox.remove(); errBox = null; }
}

/* ---------- 数据 ---------- */

async function poll(){
  if(disposed) return;
  try{
    const [d, sRaw, pol] = await Promise.all([
      apiFetch("/api/snapshot"),
      apiFetch("/api/usage/series?hours=24").catch(() => ({ series: [] })),
      apiFetch("/api/policies/state").catch(() => ({})),
    ]);
    if(disposed) return;
    const list = Array.isArray(sRaw) ? sRaw : (sRaw.series || sRaw.points || []);
    series = mapSeries(list);
    dash = d;
    budget = pickBudget(pol);
    // 供其他面板 / 旧代码读取
    window.__amBudget = budget;
    render();
  }catch(err){
    if(disposed) return;
    showError("总览数据拉取失败：" + (err.message || err));
  }
}

/* ---------- 面板接口 ---------- */

export function mount(container, sse, opts){
  unmount();
  disposed = false; visible = true; renderPending = false; lastFp = ""; firstPaint = true;
  root = document.createElement("div");
  root.className = "am-panel";
  root.innerHTML = `
    <div class="am-panel-hd"><span class="am-ph-title">总览</span><span class="am-ph-sub">Overview</span></div>
    <div class="am-dash-cards">${skeletonCards(6)}</div>
    <div class="am-grid2">
      <div class="am-col">
        <div class="am-dk">运行中</div>
        <div class="am-run-list">${emptyState("加载中…")}</div>
        <div class="am-dk">最近活动</div>
        <div class="am-ev-tl">${emptyState("加载中…")}</div>
      </div>
      <div class="am-col">
        <div class="am-dk">预算</div>
        <div class="am-budget-big">
          <div class="am-budget-ring-wrap" style="width:132px;height:132px">
            <svg class="am-budget-ring" viewBox="0 0 132 132" aria-hidden="true">
              <circle class="am-ring-bg" cx="66" cy="66" r="56"></circle>
              <circle class="am-ring-fg" cx="66" cy="66" r="56" stroke-dasharray="0 400"></circle>
            </svg>
            <div class="am-budget-num"><b>—</b><span>加载预算…</span></div>
          </div>
        </div>
        <div class="am-dk">Token 构成</div>
        <div class="am-donut-box">${emptyState("加载中…")}</div>
      </div>
    </div>`;
  container.appendChild(root);
  poll();
  pollTimer = setInterval(poll, POLL_MS);
}

export function unmount(){
  disposed = true; visible = true; renderPending = false;
  if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
  if(root){ root.remove(); root = null; }
  errBox = null;
}

export function setVisible(v){
  visible = !!v;
  if(visible && renderPending){ lastFp = ""; render(); }
}
