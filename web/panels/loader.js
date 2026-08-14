/* ============================================================
 * Agent MCP · 面板加载器（loader）v2
 * 职责：
 *  1. 注入 /css/panels.css（幂等）；
 *  2. 创建全屏舞台（.am-stage）：顶部导航 Dock + 三个分页 pane
 *     （协作泳道 / 策略可视化 / 工作区视图）——页面级分页，非覆盖抽屉；
 *  3. 面板**常驻**：首次切换懒加载 mount，之后切换只 toggle 可见性
 *     （不 unmount/不重建 DOM → 消除闪烁）；
 *  4. 可见性通知：切走的面板收到 setVisible(false)，暂停 DOM 渲染
 *     （数据仍更新，切回时一次渲染 → 性能）；
 *  5. 共享 SSE 连接：复用 window.__amSse（去重），断线自动重连。
 * 接口：面板导出 { mount(container, sse, { setVisible }), unmount() }。
 * ============================================================ */

const SSE_URL = "/api/events";
const CSS_URL = "/css/panels.css";
const CSS_ID = "am-panels-css";
// 面板模块版本化：daemon 已发 no-store，但浏览器可能仍持有 no-cache 头生效前的
// 旧缓存响应——版本后缀保证 URL 变化必然绕过缓存（防旧模块/旧逻辑）
const PANEL_V = "v3";
const TAB_DEFS = [
  { key: "dashboard",    label: "总览", module: `./dashboard.js?v=${PANEL_V}` },
  { key: "tokens",       label: "Token 用量", module: `./tokens.js?v=${PANEL_V}` },
  { key: "collaboration", label: "协作泳道", module: `./collaboration.js?v=${PANEL_V}` },
  { key: "policies",      label: "策略可视化", module: `./policies.js?v=${PANEL_V}` },
  { key: "workspaces",    label: "工作区视图", module: `./workspaces.js?v=${PANEL_V}` },
];

let inited = false;
let sse = null;
let stage = null, dock = null, dotEl = null;
let panes = new Map();      // key -> { paneEl, module, setVisible }
let currentKey = null;

/* ---------- 样式注入（幂等） ---------- */

function injectCss(){
  if(document.getElementById(CSS_ID)) return;
  const link = document.createElement("link");
  link.id = CSS_ID;
  link.rel = "stylesheet";
  link.href = CSS_URL;
  document.head.appendChild(link);
}

/* ---------- SSE：复用 window 级连接，去重 ---------- */

function getSse(){
  const existing = window.__amSse;
  if(existing && (existing.readyState === EventSource.CONNECTING || existing.readyState === EventSource.OPEN)){
    sse = existing;
  }else{
    sse = new EventSource(SSE_URL);
    window.__amSse = sse;
  }
  sse.onopen = () => { if(dotEl){ dotEl.className = "am-dot on"; dotEl.title = "SSE 已连接"; } };
  sse.onerror = () => { if(dotEl){ dotEl.className = "am-dot off"; dotEl.title = "SSE 连接异常（自动重连中）"; } };
  return sse;
}

/* ---------- DOM 骨架 ---------- */

function buildDom(){
  // 全屏舞台：顶部导航 + 分页容器
  stage = document.createElement("div");
  stage.className = "am-stage";
  stage.setAttribute("aria-label", "Agent MCP 仪表盘面板");
  stage.innerHTML = `
    <nav class="am-topdock" role="tablist" aria-label="Agent MCP 面板">
      <span class="am-brand">Agent MCP <span class="am-brand-sub">仪表盘</span></span>
      <span class="am-dock-tabs">
        ${TAB_DEFS.map(d => `<button class="am-tab" data-key="${d.key}" role="tab" aria-selected="false">${d.label}</button>`).join("")}
      </span>
      <span class="am-dock-right">
        <span class="am-dot" title="SSE 未连接"></span>
        <button class="am-stage-close" id="am-stage-close" title="关闭仪表盘（Esc）">✕</button>
      </span>
    </nav>
    <div class="am-panes">
      ${TAB_DEFS.map(d => `<section class="am-pane" data-key="${d.key}" role="tabpanel" hidden></section>`).join("")}
    </div>`;
  document.body.appendChild(stage);
  dock = stage.querySelector(".am-topdock");
  dotEl = stage.querySelector(".am-dot");

  dock.addEventListener("click", onTabClick);
  stage.querySelector("#am-stage-close").addEventListener("click", closeStage);
  document.addEventListener("keydown", e => {
    if(e.key === "Escape" && stage.classList.contains("open")) closeStage();
  });
}

/* ---------- 分页切换 ---------- */

function paneFor(key){
  if(!panes.has(key)){
    const paneEl = stage.querySelector(`.am-pane[data-key="${key}"]`);
    panes.set(key, { paneEl, module: null, setVisible: null });
  }
  return panes.get(key);
}

async function openPane(key){
  if(currentKey === key){
    // 已打开：保持（防重复动画）
    stage.classList.add("open");
    return;
  }
  // 收起旧 pane（不 unmount，通知面板隐藏）
  if(currentKey){
    const old = panes.get(currentKey);
    if(old && old.setVisible) old.setVisible(false);
    if(old && old.paneEl) old.paneEl.classList.remove("active");
  }
  currentKey = key;
  const rec = paneFor(key);
  rec.paneEl.hidden = false;
  // 强制重排以触发进入动画（remove→add 同一帧会被合并）
  void rec.paneEl.offsetWidth;
  rec.paneEl.classList.add("active");
  stage.classList.add("open");
  setActiveTab(key);

  if(!rec.module){
    rec.paneEl.innerHTML = '<div class="am-panel"><div class="am-empty">加载面板…</div></div>';
    try{
      const mod = await import(TAB_DEFS.find(d => d.key === key).module);
      if(currentKey !== key) return; // 加载期间用户已切走
      rec.paneEl.innerHTML = "";
      rec.module = mod;
      rec.setVisible = v => { if(mod.setVisible) mod.setVisible(v); };
      if(mod.mount) mod.mount(rec.paneEl, sse, { setVisible: rec.setVisible });
      if(rec.setVisible) rec.setVisible(true);
    }catch(err){
      if(currentKey !== key) return;
      rec.paneEl.innerHTML = `<div class="am-panel"><div class="am-err">面板模块加载失败：${String(err.message || err)}</div></div>`;
    }
  }else{
    if(rec.setVisible) rec.setVisible(true);
  }
}

function closeStage(){
  if(currentKey){
    const rec = panes.get(currentKey);
    if(rec && rec.setVisible) rec.setVisible(false);
    if(rec && rec.paneEl) rec.paneEl.classList.remove("active");
  }
  stage.classList.remove("open");
  setActiveTab(null);
  currentKey = null;
}

function setActiveTab(key){
  if(!dock) return;
  dock.querySelectorAll(".am-tab").forEach(b => {
    const active = b.dataset.key === key && stage.classList.contains("open");
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
}

async function onTabClick(e){
  const btn = e.target.closest(".am-tab");
  if(!btn) return;
  const key = btn.dataset.key;
  // 点击当前激活标签 → 收起舞台（回对话图）
  if(key === currentKey && stage.classList.contains("open")){
    closeStage();
    return;
  }
  await openPane(key);
}

/* ---------- 初始化（幂等） ---------- */

export function init(){
  if(inited) return;
  if(!document.body) return;
  inited = true;
  injectCss();
  buildDom();
  getSse();
  // 仪表盘入口：index.html header 的 #dashboard-btn（或任意元素）点击打开面板
  window.__amOpenDashboard = (key) => { openPane(key || currentKey || TAB_DEFS[0].key); };
  const btn = document.getElementById("dashboard-btn");
  if(btn) btn.addEventListener("click", () => { openPane(currentKey || TAB_DEFS[0].key); });
}

if(document.readyState === "loading"){
  document.addEventListener("DOMContentLoaded", init);
}else{
  init();
}
