/* ============================================================
 * Agent MCP · 协作泳道面板
 * 纵向泳道列表（每个 agent 一条：agent_id / cli / status / 活动摘要），
 * 数据源：POST /api/agents/list {fields:"all"}（回退 GET ?fields=all / GET）
 *        + GET /api/snapshot 元数据补全 + GET /api/agents/activity。
 * SSE：agent.* 命名事件实时更新状态与 CLI（spawned/activity 合并）。
 * review_requested 无生产者时不造假数据 —— 仅在有真实卡片时渲染，
 * 否则显示「等待审查事件」。
 * import 版本必须与 loader.js PANEL_V 一致。
 * ============================================================ */

import { esc, fmtTime, apiFetch, apiPost, cliColor, emptyState, errorState,
         isDeadEvent, toast } from "./components.js?v=v7";

const AGENT_EVENTS = [
  "agent.spawned","agent.user_turn","agent.running","agent.message","agent.message_delta",
  "agent.tool_use","agent.tool_result","agent.usage",
  "agent.terminated","agent.error",
  "agent.cancelled","agent.orphaned","agent.needs_advisor","agent.verify_failed",
  "agent.verify_passed","agent.budget_downgrade","agent.ingest_failed",
  // 死事件（无生产者，UI 不等待）：agent.thread_message_sent / _received / agent.idle
  // 见 components.js DEAD_EVENTS —— 订阅侧仍注册以便将来启用，入列前过滤。
];
const MAX_ACTIVITY = 20;
const MAX_REVIEWS = 3;
const DIFF_MAX = 140;

/* 状态 → 中文标签 / 徽章类 */
const STATUS_LABEL = {
  running:"运行中", terminated:"完成", queued:"排队", error:"失败", cancelled:"已取消",
  incomplete:"超时/失联", needs_advisor:"需决策", idle:"空闲", reviewing:"审查中",
};
function statusLabel(s){ return STATUS_LABEL[s] || s || "—"; }
function statusClass(s){
  return s==="running" ? "run" : s==="terminated" ? "ok" : s==="error" ? "err"
       : s==="needs_advisor" ? "warn" : s==="reviewing" ? "warn" : "soft";
}

/* agent.* 事件 → 活动摘要文本 */
function eventText(type, payload){
  const p = payload || {};
  switch(type){
    case "agent.spawned":            return "创建 · " + (p.task_name || p.task || "派发任务");
    case "agent.user_turn":          return "用户回合";
    case "agent.running":            return "开始运行";
    case "agent.message":            return p.text || p.message || "新消息";
    case "agent.message_delta":      return null;
    case "agent.tool_use":           return "▸ 工具 " + (p.name || "tool");
    case "agent.tool_result":        return "工具结果" + (p.name ? " " + p.name : "") + (p.ok === false ? " 失败" : "");
    case "agent.usage":              return "用量 " + (p.tokens != null ? p.tokens + " tok" : "");
    case "agent.thread_message_sent":return null; // 死事件
    case "agent.thread_message_received": return null;
    case "agent.idle":               return null;
    case "agent.terminated":         return "完成" + (p.stop_reason ? " · " + p.stop_reason : "");
    case "agent.error":              return "错误：" + (p.error || p.message || "未知");
    case "agent.cancelled":          return "已取消";
    case "agent.orphaned":           return "失联（orphaned）";
    case "agent.needs_advisor":      return "需决策：" + (p.question || "");
    case "agent.verify_failed":      return "验证失败（attempt " + (p.attempt || 1) + "）";
    case "agent.verify_passed":      return "验证通过";
    case "agent.budget_downgrade":   return "预算降级";
    case "agent.ingest_failed":      return "上下文注入失败";
    default:                         return String(type || "").replace(/^agent\./, "");
  }
}

/* ---------- 模块状态 ---------- */

let root = null;
let lanes = null;
let unsubs = null;
let disposed = true;
let visible = true;
let renderPending = false;
let filter = "all";
let hasAnyReview = false;

/* ---------- 数据归一化 ---------- */

function normalizeAgent(a){
  if(!a || typeof a !== "object") return null;
  return {
    id: String(a.id ?? a.agent_id ?? a.agentId ?? ""),
    cli: a.cli || a.target_cli || "",
    status: a.status || "idle",
    task: a.task_name || a.task || a.name || "",
    created_at: a.created_at || a.created || a.started_at || null,
    stop_reason: a.stop_reason || "",
  };
}

function normalizeActivity(x){
  const p = x.payload || {};
  return {
    agent_id: String(x.agent_id ?? x.id ?? p.agent_id ?? ""),
    type: x.type || x.event || x.kind || "event",
    ts: x.ts ?? x.time ?? x.created_at ?? x.updated_at ?? Date.now(),
    text: x.text ?? x.message ?? x.summary ?? "",
    tool: x.tool ?? x.name ?? p.name ?? p.tool ?? "",
    cli: x.cli || p.cli || p.target_cli || "",
    seq: x.seq,
  };
}

function extractList(d){
  if(Array.isArray(d)) return d;
  return d.agents || d.list || d.data || [];
}

async function fetchAgentsFull(){
  // 1) POST fields=all（P3 全量：含 cli / created_at）
  try{
    const d = await apiPost("/api/agents/list", { fields: "all" });
    const list = extractList(d).map(normalizeAgent).filter(Boolean);
    if(list.length) return list;
  }catch{ /* fall through */ }
  // 2) GET ?fields=all（后端若支持 query）
  try{
    const d = await apiFetch("/api/agents/list?fields=all");
    const list = extractList(d).map(normalizeAgent).filter(Boolean);
    if(list.length) return list;
  }catch{ /* fall through */ }
  // 3) GET 默认轻量字段
  try{
    const d = await apiFetch("/api/agents/list");
    return extractList(d).map(normalizeAgent).filter(Boolean);
  }catch(err){
    throw err;
  }
}

async function fetchSnapshotAgents(){
  try{
    const d = await apiFetch("/api/snapshot");
    return extractList(d).map(normalizeAgent).filter(Boolean);
  }catch{ return []; }
}

async function fetchActivity(){
  try{
    const d = await apiFetch("/api/agents/activity");
    const list = Array.isArray(d) ? d : (d.activity || d.events || []);
    return (list || []).map(normalizeActivity).filter(x => x.agent_id && !isDeadEvent(x.type));
  }catch{ return []; }
}

/* ---------- 渲染 ---------- */

function laneEl(id){
  const lane = lanes.get(id);
  const cli = lane.cli || "—";
  const head = `<div class="am-swimlane-head">
      <span class="am-lane-id">#${esc(id)}</span>
      <span class="am-cli" style="background:${cliColor(lane.cli)}" title="${esc(lane.cli || "CLI 未知")}">${esc(cli)}</span>
      <span class="am-badge ${statusClass(lane.status)}">${esc(statusLabel(lane.status))}</span>
    </div>
    <div class="am-lane-task" title="${esc(lane.task)}">${esc(lane.task) || '<span class="am-empty">（无任务描述）</span>'}</div>`;

  // 审查卡片：有真实事件才渲染（review_requested 当前无生产者，不造假）
  const reviews = lane.reviews.length ? lane.reviews.map(rv => `
    <div class="am-review${rv.flash ? " flash" : ""}" title="diff 预览：${esc(rv.diff)}">
      <div class="am-rev-title">审查请求</div>
      <div class="am-rev-flow">
        <span class="am-cli" style="background:${cliColor(rv.writer_cli)}">${esc(rv.writer_cli)}</span>
        <span class="am-rev-arrow">→</span>
        <span class="am-cli" style="background:${cliColor(rv.reviewer_cli)}">${esc(rv.reviewer_cli)}</span>
        <span class="am-rev-agent">#${esc(id)}</span>
      </div>
      <div class="am-diff">${esc(rv.diff)}</div>
    </div>`).join("") : "";

  const last = lane.activity[0];
  const act = last ? `<div class="am-lane-act">${
      last.type === "agent.running" ? '<span class="am-dot-live"></span>' : ""
    }${esc(fmtTime(last.ts))} · ${esc(last.text)}</div>` : "";

  return `<div class="am-swimlane ${lane.status === "running" ? "run" : ""} am-row-in" data-id="${esc(id)}">${head}${reviews}${act}</div>`;
}

function render(){
  if(disposed || !root) return;
  if(!visible){ renderPending = true; return; }
  renderPending = false;
  const filterFn = {
    all: () => true,
    running: l => ["running","queued"].includes(l.status),
    done: l => l.status === "terminated",
    bad: l => ["error","cancelled","incomplete","needs_advisor"].includes(l.status),
  }[filter] || (() => true);

  const ids = [...lanes.values()]
    .filter(filterFn)
    .sort((a,b) => {
      const ta = Date.parse(a.created_at || "") || 0;
      const tb = Date.parse(b.created_at || "") || 0;
      return tb - ta || String(a.id).localeCompare(String(b.id), "zh");
    })
    .map(l => l.id);

  const box = root.querySelector(".am-swimlanes");
  if(!ids.length){
    box.innerHTML = emptyState("暂无 agent 泳道，等待派发…");
    return;
  }
  const reviewHint = hasAnyReview ? "" :
    `<div class="am-review-pending" title="review_requested 事件当前无生产者">等待审查事件</div>`;
  // 注意：不再嵌套 .am-swimlanes（此前会双重套壳）
  box.innerHTML = reviewHint + ids.map(laneEl).join("");
}

function bindFilter(){
  const box = root.querySelector(".am-collab-filters");
  if(!box || box.dataset.bound) return;
  box.dataset.bound = "1";
  box.addEventListener("click", e => {
    const btn = e.target.closest("button[data-f]");
    if(!btn) return;
    filter = btn.dataset.f;
    box.querySelectorAll("button").forEach(b => {
      b.classList.toggle("active", b === btn);
      b.setAttribute("aria-pressed", b === btn ? "true" : "false");
    });
    render();
  });
}

function scheduleRender(){
  if(disposed) return;
  if(!visible){ renderPending = true; return; }
  render();
}

export function setVisible(v){
  visible = !!v;
  if(visible && renderPending){ renderPending = false; lastFpForce(); render(); }
}
function lastFpForce(){ /* 占位：泳道无指纹，切回直接渲染 */ }

/* ---------- 状态更新 ---------- */

function upsertLane(agent){
  if(!agent || !agent.id) return null;
  let lane = lanes.get(agent.id);
  if(!lane){
    lane = { id: agent.id, cli: agent.cli || "", status: agent.status || "idle",
             task: agent.task || "", created_at: agent.created_at || null,
             activity: [], reviews: [] };
    lanes.set(agent.id, lane);
  }
  if(agent.cli) lane.cli = agent.cli;
  if(agent.task) lane.task = agent.task;
  if(agent.status) lane.status = agent.status;
  if(agent.created_at) lane.created_at = agent.created_at;
  return lane;
}

function pushActivity(agentId, entry){
  const lane = lanes.get(agentId);
  if(!lane) return;
  if(entry.seq != null){
    const dup = lane.activity.some(a => a.seq === entry.seq);
    if(dup) return;
  }
  if(isDeadEvent(entry.type)) return;
  lane.activity.unshift(entry);
  if(lane.activity.length > MAX_ACTIVITY) lane.activity.length = MAX_ACTIVITY;
}

function onAgentEvent(type, data){
  if(isDeadEvent(type)) return;
  const payload = data.payload || {};
  const aid = String(data.agent_id ?? payload.agent_id ?? "");
  if(!aid) return;
  // SSE 可能先于 list 返回 → 建占位泳道，并尽量补 CLI
  if(!lanes.has(aid)){
    upsertLane({ id: aid, cli: payload.cli || payload.target_cli || "", status: "running", task: payload.task_name || "" });
  }
  const lane = lanes.get(aid);
  if(!lane) return;
  if(payload.cli || payload.target_cli) lane.cli = payload.cli || payload.target_cli;
  if(payload.task_name || payload.task) lane.task = payload.task_name || payload.task;
  if(type === "agent.running") lane.status = "running";
  else if(type === "agent.terminated") lane.status = payload.stop_reason === "timeout" ? "incomplete" : "terminated";
  else if(type === "agent.error") lane.status = "error";
  else if(type === "agent.cancelled") lane.status = "cancelled";
  else if(type === "agent.orphaned") lane.status = "incomplete";
  else if(type === "agent.needs_advisor") lane.status = "needs_advisor";
  else if(type === "agent.spawned" && !lane.task && payload.task_name) lane.task = payload.task_name;
  if(type === "agent.message_delta") return;
  const text = eventText(type, payload);
  if(text) pushActivity(aid, { type, ts: payload.ts ?? data.ts ?? Date.now(), text, seq: data.seq, cli: lane.cli });
  scheduleRender();
}

function onReviewRequested(data){
  const payload = data.payload || {};
  const aid = String(data.agent_id ?? payload.agent_id ?? payload.writer_agent_id ?? "");
  const diff = truncateDiff(payload.diff_preview || payload.diff || "");
  if(aid && !lanes.has(aid)) upsertLane({ id: aid, cli: payload.writer_cli || "", status: "reviewing", task: payload.task || "" });
  if(!aid) return;
  const lane = lanes.get(aid);
  hasAnyReview = true;
  lane.reviews.unshift({
    writer_cli: payload.writer_cli || "writer",
    reviewer_cli: payload.reviewer_cli || "reviewer",
    diff, flash: true,
  });
  if(lane.reviews.length > MAX_REVIEWS) lane.reviews.length = MAX_REVIEWS;
  scheduleRender();
}

function truncateDiff(text){
  const s = String(text || "");
  const lines = s.split("\n").slice(0, 3);
  let out = lines.join("\n");
  if(out.length > DIFF_MAX) out = out.slice(0, DIFF_MAX);
  if(out.length < s.length) out += "\n…";
  return out;
}

/* ---------- SSE 订阅 ---------- */

function subscribe(sse, type, fn){
  if(!sse) return;
  const named = ev => {
    let d; try { d = JSON.parse(ev.data); } catch { return; }
    if(!d || d.type) return;
    fn(d, ev);
  };
  const msg = ev => {
    let d; try { d = JSON.parse(ev.data); } catch { return; }
    if(!d || d.type !== type) return;
    fn(d, ev);
  };
  sse.addEventListener(type, named);
  sse.addEventListener("message", msg);
  unsubs.add(() => { sse.removeEventListener(type, named); sse.removeEventListener("message", msg); });
}

/* ---------- 面板接口 ---------- */

export function mount(container, sse, opts){
  unmount();
  disposed = false;
  visible = true;
  renderPending = false;
  hasAnyReview = false;
  lanes = new Map();
  unsubs = new Set();
  root = document.createElement("div");
  root.className = "am-panel";
  root.innerHTML = `
    <div class="am-panel-hd"><span class="am-ph-title">协作泳道</span><span class="am-ph-sub">Collaboration</span>
      <span class="am-ph-ops am-collab-filters">
        <button class="am-chip active" data-f="all" aria-pressed="true">全部</button>
        <button class="am-chip" data-f="running" aria-pressed="false">运行中</button>
        <button class="am-chip" data-f="done" aria-pressed="false">完成</button>
        <button class="am-chip" data-f="bad" aria-pressed="false">异常</button>
      </span>
    </div>
    <div class="am-swimlanes">${emptyState("加载泳道数据…")}</div>`;
  container.appendChild(root);
  bindFilter();

  Promise.all([fetchAgentsFull(), fetchSnapshotAgents(), fetchActivity()])
    .then(([agents, snapAgents, acts]) => {
      if(disposed) return;
      // snapshot 元数据补全 CLI / created_at
      const byId = new Map();
      for(const a of [...agents, ...snapAgents]){
        if(!a.id) continue;
        const prev = byId.get(a.id) || {};
        byId.set(a.id, {
          ...prev, ...a,
          cli: a.cli || prev.cli || "",
          task: a.task || prev.task || "",
          created_at: a.created_at || prev.created_at || null,
          status: a.status || prev.status || "idle",
        });
      }
      byId.forEach(a => upsertLane(a));
      acts.forEach(x => {
        if(!lanes.has(x.agent_id)) upsertLane({ id: x.agent_id, cli: x.cli || "", status: "idle", task: "" });
        if(x.cli) {
          const lane = lanes.get(x.agent_id);
          if(lane && !lane.cli) lane.cli = x.cli;
        }
        pushActivity(x.agent_id, {
          type: x.type, ts: x.ts,
          text: x.text || eventText(x.type, { name: x.tool }),
          seq: x.seq,
        });
      });
      render();
    })
    .catch(err => {
      if(disposed) return;
      const box = root.querySelector(".am-swimlanes");
      if(box) box.innerHTML = errorState("泳道数据加载失败：" + (err.message || err));
      toast("泳道数据加载失败：" + (err.message || err), "error");
    });

  for(const t of AGENT_EVENTS) subscribe(sse, t, (d) => onAgentEvent(t, d));
  subscribe(sse, "review_requested", onReviewRequested);
}

export function unmount(){
  disposed = true;
  visible = true;
  renderPending = false;
  if(unsubs){ for(const fn of unsubs) fn(); unsubs = null; }
  if(root){ root.remove(); root = null; }
  lanes = null;
}
