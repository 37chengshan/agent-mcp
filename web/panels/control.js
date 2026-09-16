/* ============================================================
 * Agent MCP · 编排控制面板（v4）
 * Run（唯一执行单位）列表 + Goal 持续目标 + Schedule 定时触发。
 * 数据源：GET /api/v4/runs · /api/v4/goals · /api/v4/schedules
 * 写操作：POST /api/v4/goals/create|update · /api/v4/schedules/create|cancel
 * ============================================================ */

import { esc, fmtInt, fmtTime, apiFetch, emptyState, loadingState, errorState,
         ST_LABEL, ST_CLS } from "./components.js?v=v6";

const POLL_MS = 8000;

let root = null, pollTimer = null, disposed = true, visible = true;
let data = { runs: [], goals: [], schedules: [] };
let busy = false, flash = "";

const RUN_ST = {
  PENDING: ["排队", "soft"], ADMITTED: ["已准入", "soft"], RUNNING: ["运行中", "run"],
  WAITING: ["等待", "warn"], COMPLETED: ["完成", "ok"], FAILED: ["失败", "err"],
  CANCELLED: ["取消", "err"], INTERRUPTED: ["中断", "err"], INCOMPLETE: ["未完成", "warn"],
};
const GOAL_ST = { active: ["进行中", "run"], paused: ["已暂停", "soft"], completed: ["已完成", "ok"] };

function apiPost(path, body){
  const headers = { "Content-Type": "application/json" };
  const t = (window.__amToken) || ((location.hash.match(/token=([^&]+)/) || [])[1]
    ? decodeURIComponent((location.hash.match(/token=([^&]+)/) || [])[1]) : "");
  if(t) headers["X-Auth-Token"] = t;
  return fetch(path, { method: "POST", headers, body: JSON.stringify(body) })
    .then(async r => {
      const j = await r.json().catch(() => ({}));
      if(!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      return j;
    });
}

function badge(label, cls){
  return `<span class="am-badge ${cls || "soft"}">${esc(label)}</span>`;
}

function render(){
  if(disposed || !root) return;
  if(!visible) return;
  const { runs, goals, schedules } = data;

  const nRun = runs.filter(r => r.status === "RUNNING" || r.status === "ADMITTED").length;
  const nGoal = goals.filter(g => g.status === "active").length;
  const nSched = schedules.filter(s => !s.paused).length;

  root.querySelector(".am-ctl-cards").innerHTML = `
    <div class="am-dash-card"><div class="am-dash-card-v">${fmtInt(runs.length)}</div>
      <div class="am-dash-card-k">Run 总数</div>
      <div class="am-dash-card-s">活跃 ${nRun}</div></div>
    <div class="am-dash-card ${nGoal ? "run" : ""}"><div class="am-dash-card-v">${fmtInt(nGoal)}</div>
      <div class="am-dash-card-k">活跃 Goal</div>
      <div class="am-dash-card-s">共 ${goals.length}</div></div>
    <div class="am-dash-card"><div class="am-dash-card-v">${fmtInt(nSched)}</div>
      <div class="am-dash-card-k">启用 Schedule</div>
      <div class="am-dash-card-s">共 ${schedules.length}</div></div>
    ${flash ? `<div class="am-ctl-flash">${esc(flash)}</div>` : ""}`;

  // Runs
  const runRows = runs.slice(0, 40).map(r => {
    const [label, cls] = RUN_ST[r.status] || [r.status, "soft"];
    const rid = String(r.id || "").slice(0, 8);
    return `<tr>
      <td class="mono" title="${esc(r.id)}">${esc(rid)}</td>
      <td>${esc(r.run_kind || "—")}</td>
      <td>${esc(r.trigger_type || "—")}${r.trigger_ref ? ` #${esc(r.trigger_ref)}` : ""}</td>
      <td>${r.agent_id != null ? `#${esc(r.agent_id)}` : "—"}</td>
      <td>${badge(label, cls)}</td>
      <td class="am-tok-num">${esc(r.stop_reason || "")}</td>
      <td class="am-tok-num">${fmtTime(r.updated_at || r.created_at)}</td>
    </tr>`;
  }).join("");
  root.querySelector(".am-ctl-runs tbody").innerHTML =
    runRows || `<tr><td colspan="7">${emptyState("暂无 Run——spawn 后会自动落库")}</td></tr>`;

  // Goals
  const goalRows = goals.slice(0, 30).map(g => {
    const [label, cls] = GOAL_ST[g.status] || [g.status, "soft"];
    return `<tr>
      <td class="mono">#${esc(g.id)}</td>
      <td title="${esc(g.objective)}">${esc(String(g.objective || "").slice(0, 48))}</td>
      <td>${g.agent_id != null ? `#${esc(g.agent_id)}` : "—"}</td>
      <td>${badge(label, cls)}</td>
      <td class="am-tok-num">${esc(g.rounds || 0)}</td>
      <td class="am-tok-num">${g.token_budget != null ? fmtInt(g.token_budget) : "∞"}</td>
      <td>
        ${g.status === "active"
          ? `<button class="am-btn am-btn-sm" data-goal="${g.id}" data-act="completed">完成</button>
             <button class="am-btn am-btn-sm" data-goal="${g.id}" data-act="paused">暂停</button>`
          : g.status === "paused"
            ? `<button class="am-btn am-btn-sm" data-goal="${g.id}" data-act="active">恢复</button>`
            : ""}
      </td>
    </tr>`;
  }).join("");
  root.querySelector(".am-ctl-goals tbody").innerHTML =
    goalRows || `<tr><td colspan="7">${emptyState("暂无 Goal")}</td></tr>`;

  // Schedules
  const schRows = schedules.slice(0, 30).map(s => {
    return `<tr>
      <td class="mono">#${esc(s.id)}</td>
      <td title="${esc(s.prompt)}">${esc(String(s.prompt || "").slice(0, 40))}</td>
      <td>${esc(s.kind || "—")}</td>
      <td class="mono">${esc(s.interval_expr || "—")}</td>
      <td>${s.agent_id != null ? `#${esc(s.agent_id)}` : "—"}</td>
      <td>${s.paused ? badge("已取消", "err") : badge("启用", "ok")}</td>
      <td class="am-tok-num">${fmtTime(s.next_tick)}</td>
      <td>${!s.paused
        ? `<button class="am-btn am-btn-sm" data-sched="${s.id}">取消</button>` : ""}</td>
    </tr>`;
  }).join("");
  root.querySelector(".am-ctl-scheds tbody").innerHTML =
    schRows || `<tr><td colspan="8">${emptyState("暂无 Schedule")}</td></tr>`;
}

async function load(){
  if(disposed) return;
  try{
    const [runs, goals, schedules] = await Promise.all([
      apiFetch("/api/v4/runs?limit=80"),
      apiFetch("/api/v4/goals?limit=50"),
      apiFetch("/api/v4/schedules?limit=50"),
    ]);
    data = {
      runs: runs.runs || [],
      goals: goals.goals || [],
      schedules: schedules.schedules || [],
    };
    if(root){
      const err = root.querySelector(".am-ctl-error");
      if(err) err.remove();
    }
  }catch(e){
    if(root){
      const box = root.querySelector(".am-ctl-error");
      if(box) box.textContent = "加载失败：" + (e.message || e);
      else {
        const d = document.createElement("div");
        d.className = "am-ctl-error";
        d.textContent = "加载失败：" + (e.message || e);
        root.prepend(d);
      }
    }
  }
  render();
}

async function refresh(){
  await load();
}

function bind(){
  root.addEventListener("click", async e => {
    const btn = e.target.closest("button");
    if(!btn || busy) return;
    if(btn.classList.contains("am-btn-retry")){ flash = ""; await refresh(); return; }
    if(btn.dataset.goal){
      busy = true; flash = "";
      try{
        await apiPost("/api/v4/goals/update",
                      { goal_id: Number(btn.dataset.goal), status: btn.dataset.act });
        flash = "Goal 已更新";
      }catch(err){ flash = "失败：" + err.message; }
      busy = false; await refresh(); return;
    }
    if(btn.dataset.sched){
      busy = true; flash = "";
      try{
        await apiPost("/api/v4/schedules/cancel", { schedule_id: Number(btn.dataset.sched) });
        flash = "Schedule 已取消";
      }catch(err){ flash = "失败：" + err.message; }
      busy = false; await refresh(); return;
    }
    if(btn.id === "am-ctl-create-goal"){
      const objective = (root.querySelector("#am-ctl-goal-obj")?.value || "").trim();
      const agentId = (root.querySelector("#am-ctl-goal-agent")?.value || "").trim();
      if(!objective){ flash = "请填写目标描述"; render(); return; }
      busy = true; flash = "";
      try{
        const body = { objective };
        if(agentId) body.agent_id = Number(agentId);
        await apiPost("/api/v4/goals/create", body);
        flash = "Goal 已创建";
        root.querySelector("#am-ctl-goal-obj").value = "";
      }catch(err){ flash = "失败：" + err.message; }
      busy = false; await refresh(); return;
    }
  });
}

export function mount(container, sse, opts){
  disposed = false; visible = true;
  root = container;
  root.innerHTML = `
    <div class="am-panel">
      <div class="am-panel-hd">
        <span class="am-panel-kicker">Control Plane</span>
        <h3>编排控制</h3>
        <p class="am-panel-desc">Run 是唯一执行单位；Goal / Schedule 只产 Intent，由 daemon 心泵驱动。</p>
      </div>
      <div class="am-ctl-cards am-dash-cards"></div>

      <section class="am-ctl-sec">
        <h4>Runs <span class="am-ctl-hint">最近 40</span></h4>
        <table class="am-dtable am-ctl-runs">
          <thead><tr><th>ID</th><th>Kind</th><th>触发</th><th>Agent</th><th>状态</th><th>原因</th><th>时间</th></tr></thead>
          <tbody></tbody>
        </table>
      </section>

      <section class="am-ctl-sec">
        <h4>Goals</h4>
        <div class="am-ctl-form">
          <input id="am-ctl-goal-obj" type="text" placeholder="持续目标描述（agent 空闲时自动续播）" />
          <input id="am-ctl-goal-agent" type="number" placeholder="agent_id" style="width:110px" />
          <button class="am-btn" id="am-ctl-create-goal" type="button">创建 Goal</button>
        </div>
        <table class="am-dtable am-ctl-goals">
          <thead><tr><th>ID</th><th>目标</th><th>Agent</th><th>状态</th><th>轮次</th><th>Token 预算</th><th>操作</th></tr></thead>
          <tbody></tbody>
        </table>
      </section>

      <section class="am-ctl-sec">
        <h4>Schedules</h4>
        <table class="am-dtable am-ctl-scheds">
          <thead><tr><th>ID</th><th>Prompt</th><th>Kind</th><th>Interval</th><th>Agent</th><th>状态</th><th>下次</th><th>操作</th></tr></thead>
          <tbody></tbody>
        </table>
      </section>
    </div>`;
  bind();
  refresh();
  pollTimer = setInterval(() => { if(visible) refresh(); }, POLL_MS);
}

export function setVisible(v){
  visible = v;
  if(v) refresh();
}

export function unmount(){
  disposed = true;
  if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
  root = null;
}
