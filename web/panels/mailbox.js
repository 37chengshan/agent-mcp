/* ============================================================
 * Agent MCP · 信箱与团队治理面板
 * 展示 Agent 间 Mailbox 消息流、投票治理看板、沙箱状态（诚实标注）。
 * 数据源：POST /api/agents/list {fields:"all"} · POST /api/mailbox/fetch
 * 沙箱：前端未接线真实隔离标志 —— 明确显示「未接线」，不宣称隔离生效。
 * import 版本必须与 loader.js PANEL_V 一致。
 * ============================================================ */

import { esc, fmtTime, apiPost, apiFetch, emptyState,
         toast, setBtnBusy, authToken } from "./components.js?v=v7";

let root = null, disposed = true;
let refreshBtn = null;

function card(title, bodyHtml, id){
  return `<section class="am-card am-mb-card" ${id ? `id="${id}"` : ""}>
    <h3 class="am-mb-title">${esc(title)}</h3>
    <div class="am-mb-body">${bodyHtml}</div>
  </section>`;
}

function renderShell(){
  root.innerHTML = `
    <div class="am-panel">
      <div class="am-panel-hd">
        <span class="am-ph-title">信箱与治理</span>
        <span class="am-ph-sub">Mailbox Governance</span>
        <span class="am-ph-ops">
          <button type="button" id="mb-refresh-btn" class="am-btn" title="重新拉取信箱与协作状态">刷新</button>
        </span>
      </div>
      <div class="am-mb-grid">
        ${card("团队共识治理投票", emptyState("暂无进行中的团队投票提案"), "mb-governance-list")}
        ${card("容器沙箱与文件审计状态", `
          <div class="am-mb-sandbox">
            <div class="am-mb-row"><span class="am-mb-k">隔离状态</span>
              <span class="am-badge warn">未接线</span></div>
            <div class="am-mb-row"><span class="am-mb-k">说明</span>
              <span class="am-mb-v">前端未读取真实沙箱标志；Docker / RLIMIT 等信息在未接线前不宣称生效。</span></div>
            <div class="am-mb-row"><span class="am-mb-k">自动回滚</span>
              <span class="am-mb-v">未接线</span></div>
            <div class="am-mb-row"><span class="am-mb-k">沙箱限额</span>
              <span class="am-mb-v">未接线</span></div>
          </div>`, "mb-sandbox-card")}
      </div>
      ${card("最近跨 Agent 消息广播流", emptyState("等待信箱消息"), "mb-msg-timeline")}
      <div class="am-mb-status" id="mb-status" aria-live="polite"></div>
    </div>`;
  refreshBtn = root.querySelector("#mb-refresh-btn");
  if(refreshBtn){
    refreshBtn.addEventListener("click", onRefresh);
  }
}

function setStatus(text, isErr){
  const el = root && root.querySelector("#mb-status");
  if(!el) return;
  el.textContent = text || "";
  el.className = "am-mb-status" + (isErr ? " error" : "");
}

async function onRefresh(){
  if(disposed || !refreshBtn || refreshBtn.disabled) return;
  setBtnBusy(refreshBtn, true, "刷新中…");
  setStatus("同步中…", false);
  try{
    // 治理：以当前 agent 列表规模作为协作节点概览（诚实：不是假投票）
    let agents = [];
    try{
      const d = await apiPost("/api/agents/list", { fields: "all" });
      agents = Array.isArray(d) ? d : (d.agents || []);
    }catch{
      const d = await apiFetch("/api/agents/list?fields=all");
      agents = Array.isArray(d) ? d : (d.agents || []);
    }
    const govBody = root.querySelector("#mb-governance-list .am-mb-body");
    if(govBody){
      govBody.innerHTML = agents.length
        ? emptyState(`已同步 ${agents.length} 个协作节点 · 暂无进行中的投票提案`)
        : emptyState("暂无进行中的团队投票提案");
    }

    // 信箱：mailbox_fetch 需要 agent_id；有 agent 时拉取最新一个的收件箱
    const msgBox = root.querySelector("#mb-msg-timeline .am-mb-body");
    if(agents.length){
      const aid = agents[agents.length - 1].id ?? agents[agents.length - 1].agent_id;
      try{
        const md = await apiPost("/api/mailbox/fetch", {
          agent_id: Number(aid), unread_only: false, limit: 20,
        });
        const msgs = md.messages || [];
        if(msgBox){
          msgBox.innerHTML = msgs.length
            ? `<div class="am-mb-msgs">${msgs.map(m => `
                <div class="am-mb-msg am-row-in">
                  <span class="am-mb-msg-meta">#${esc(m.from_agent_id ?? m.from ?? "?")} → #${esc(m.to_agent_id ?? aid)} · ${esc(fmtTime(m.ts || m.created_at))}</span>
                  <span class="am-mb-msg-text">${esc(m.message || m.content || "")}</span>
                </div>`).join("")}</div>`
            : emptyState("等待信箱消息");
        }
        setStatus(`已同步 · agent #${aid} 收件箱 ${msgs.length} 条`, false);
      }catch(err){
        if(msgBox) msgBox.innerHTML = emptyState("等待信箱消息");
        setStatus("信箱拉取失败：" + (err.message || err), true);
        toast("信箱拉取失败：" + (err.message || err), "error");
      }
    }else{
      if(msgBox) msgBox.innerHTML = emptyState("等待信箱消息");
      setStatus("已同步 · 当前无 agent 节点", false);
    }
  }catch(err){
    setStatus("同步失败：" + (err.message || err), true);
    toast("信箱同步失败：" + (err.message || err), "error");
  }finally{
    setBtnBusy(refreshBtn, false);
  }
}

export function mount(container, sse, opts){
  unmount();
  disposed = false;
  root = document.createElement("div");
  root.className = "am-panel-wrap";
  container.appendChild(root);
  renderShell();
  // 鉴权可达性探测：仅提示，不静默
  if(!authToken()){
    setStatus("未检测到 X-Auth-Token（window.__amToken / #token=），写操作可能被拒", true);
  }
  onRefresh();
}

export function unmount(){
  disposed = true;
  refreshBtn = null;
  if(root){ root.remove(); root = null; }
}

export function setVisible(vis){ /* 常驻数据已拉取，切换可见性无需重建 */ }
