/* ============================================================
 * Agent MCP · 仪表盘组件库（零依赖 SVG 自绘）
 * StatCard / Sparkline / BarStack / DonutChart / DataTable /
 * Timeline / 三态（empty/loading/error）/ Toast / Skeleton。
 * 纯函数渲染字符串，调用方负责注入 DOM。
 *
 * CACHE-BUST：本文件的 import URL 必须统一为 `./components.js?v=v7`，
 * 与 loader.js 的 PANEL_V 保持一致，避免双实例。
 * ============================================================ */

/* ---------- 工具 ---------- */

export function esc(v){ return String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
export function fmtInt(n){ return Number(n || 0).toLocaleString("zh-CN"); }
export function fmtUsd(v){ return "$" + (Number(v) || 0).toFixed(2); }
export function fmtTime(ts){
  if(ts == null) return "—";
  const n = (typeof ts === "number" || /^\d+$/.test(String(ts))) ? Number(ts) : Date.parse(ts);
  if(!Number.isFinite(n)) return String(ts);
  const d = new Date(n);
  const p = x => String(x).padStart(2,"0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
export function num(v){
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}
export function prefersReducedMotion(){
  try{ return matchMedia("(prefers-reduced-motion: reduce)").matches; }
  catch{ return false; }
}

/* ---------- 认证 / 请求（所有面板统一走这里） ---------- */

export function authToken(){
  if(window.__amToken) return window.__amToken;
  const m = (location.hash || "").match(/token=([^&]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export function authHeaders(extra){
  const headers = Object.assign({}, extra || {});
  const t = authToken();
  if(t) headers["X-Auth-Token"] = t;
  return headers;
}

export async function apiFetch(path, opts){
  const o = Object.assign({}, opts || {});
  o.headers = authHeaders(o.headers);
  const r = await fetch(path, o);
  const d = await r.json().catch(() => ({}));
  if(!r.ok) throw new Error(d.error || `${path} HTTP ${r.status}`);
  return d;
}

export async function apiPost(path, body){
  return apiFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

/** 按钮 loading/禁用，防双击双提交。label 可选恢复文案。 */
export function setBtnBusy(btn, busy, label){
  if(!btn) return;
  if(busy){
    if(btn.dataset.amIdle == null) btn.dataset.amIdle = btn.textContent;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.textContent = label || "处理中…";
  }else{
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
    btn.textContent = btn.dataset.amIdle || label || btn.textContent;
  }
}

/* ---------- Toast（滑入提示，绝不静默失败） ---------- */

let toastHost = null;
export function toast(msg, kind = "info"){
  try{
    if(!toastHost){
      toastHost = document.createElement("div");
      toastHost.id = "am-toasts";
      toastHost.setAttribute("aria-live", "polite");
      document.body.appendChild(toastHost);
    }
    const el = document.createElement("div");
    el.className = "am-toast " + (kind || "info");
    el.textContent = String(msg ?? "");
    toastHost.appendChild(el);
    requestAnimationFrame(() => el.classList.add("in"));
    const ttl = kind === "error" ? 4800 : 3000;
    setTimeout(() => {
      el.classList.remove("in");
      setTimeout(() => el.remove(), 240);
    }, ttl);
  }catch{ /* toast 失败不影响主流程 */ }
}

/* ---------- 字段归一化（兼容当前 / 修复后 API 形状） ---------- */

/** 趋势点：{ts, input, output, cache_read, cost} 或 {in, out, cache} */
export function normalizeSeriesPoint(b){
  if(!b || typeof b !== "object") return { ts: null, in: 0, out: 0, cache: 0, cost: 0 };
  return {
    ts: b.ts ?? b.time ?? b.created_at ?? null,
    in: num(b.in ?? b.input ?? b.input_tokens),
    out: num(b.out ?? b.output ?? b.output_tokens),
    cache: num(b.cache ?? b.cache_read ?? b.cache_creation ?? b.cacheRead),
    cost: num(b.cost ?? b.cost_usd),
  };
}
export function mapSeries(list){
  return (Array.isArray(list) ? list : (list && list.series) || []).map(normalizeSeriesPoint);
}

/** 预算：policy_configs.budget_limit_usd / budget_usd / spent_usd 多形状 */
export function pickBudget(d){
  d = d || {};
  const cfg = d.policy_configs || d.config || {};
  const limit = num(cfg.budget_limit_usd ?? cfg.limit_usd ?? d.limit_usd ?? d.budget_limit_usd);
  const spent = num(cfg.spent_usd ?? d.spent_usd ?? d.budget_usd ?? cfg.budget_usd);
  const alt = num(d.budget_usd ?? cfg.budget_usd);
  return {
    limit_usd: limit,
    spent_usd: spent || alt,
    budget_usd: alt || spent,
  };
}

/** 无生产者 / 死事件：UI 不依赖，过滤展示。 */
export const DEAD_EVENTS = new Set([
  "agent.thread_message_sent",
  "agent.thread_message_received",
  "agent.idle",
]);
export function isDeadEvent(type){ return DEAD_EVENTS.has(String(type || "")); }

export const CLI_COLORS = {
  grok:"var(--grok,#C9A34F)", opencode:"var(--opencode,#6FA587)",
  omp:"var(--omp,#9A8EDA)", atomcode:"var(--atomcode,#5A9CD6)",
  codex:"var(--codex,#7FB5A0)", kimi:"var(--kimi,#C98A5A)",
  copilot:"var(--copilot,#8AB4F8)", pi:"var(--pi,#B48CD9)",
};
export function cliColor(cli){ return CLI_COLORS[String(cli||"").toLowerCase()] || "var(--claude,#C87A5A)"; }

export const ST_LABEL = { running:"运行中", terminated:"完成", queued:"排队", error:"失败",
  cancelled:"已取消", incomplete:"超时/失联", needs_advisor:"需决策", idle:"空闲" };
export const ST_CLS = { running:"run", terminated:"ok", error:"err", cancelled:"err",
  incomplete:"warn", needs_advisor:"warn", queued:"soft", idle:"soft" };

/* ---------- 统计卡（Hero 数字 + 标签 + 副文案 + 可选 sparkline） ---------- */

export function statCard({ k, v, sub, cls="", live=false, spark=null }){
  const sparkHtml = spark && spark.length > 1
    ? `<div class="am-spark">${sparkline(spark, 96, 26)}</div>` : "";
  return `<div class="am-dash-card ${cls}">
    <div class="am-dash-card-v ${live ? "am-live" : ""}">${esc(v)}</div>
    <div class="am-dash-card-k">${esc(k)}</div>
    ${sub ? `<div class="am-dash-card-s">${esc(sub)}</div>` : ""}
    ${sparkHtml}
  </div>`;
}

/** 首屏骨架（dashboard tiles） */
export function skeletonCards(n = 6){
  return Array.from({ length: n }, (_, i) => `
    <div class="am-dash-card am-skel" style="animation-delay:${i * 40}ms" aria-hidden="true">
      <div class="am-skel-bar w50"></div>
      <div class="am-skel-bar w30"></div>
      <div class="am-skel-bar w70"></div>
    </div>`).join("");
}

/* ---------- Sparkline（迷你折线，SVG path，首次描边动画） ---------- */

export function sparkline(points, w=96, h=26, color="var(--accent,#D96B4F)"){
  const vals = points.map(Number);
  if(!vals.length) return "";
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = (max - min) || 1;
  const stepX = w / Math.max(vals.length - 1, 1);
  const coords = vals.map((v, i) => [
    (i * stepX).toFixed(1),
    (h - 2 - ((v - min) / range) * (h - 6)).toFixed(1),
  ]);
  const path = coords.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(" ");
  const area = `${path} L${w},${h} L0,${h} Z`;
  return `<svg class="am-spark-svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${area}" fill="${color}" opacity=".12"/>
    <path class="am-spark-line" pathLength="1" d="${path}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${coords[coords.length-1][0]}" cy="${coords[coords.length-1][1]}" r="2" fill="${color}"/>
  </svg>`;
}

/* ---------- 堆叠柱（输入/输出/缓存 三段） ----------
 * buckets 兼容 {in,out,cache} 与 API {input,output,cache_read}。
 * 颜色语义：in=绿 / out=accent / cache=琥珀（与 legend class 一致）。
 */

export function barStack({ buckets, w=100, h=40 }){
  const rows = (buckets || []).map(normalizeSeriesPoint);
  const max = Math.max(1, ...rows.map(b => b.in + b.out + b.cache));
  const bw = Math.max(3, Math.floor(w / Math.max(rows.length, 1)) - 2);
  const bars = rows.map((b, i) => {
    const hIn = Math.round(b.in / max * h);
    const hOut = Math.round(b.out / max * h);
    const hC = Math.round(b.cache / max * h);
    const x = i * (bw + 2);
    /* 自下而上：cache → out → in，grow-from-0 动画 */
    return `<g class="am-bar-col" transform="translate(${x},${h - hIn - hOut - hC})" style="animation-delay:${Math.min(i * 12, 280)}ms">
      <rect class="am-bar-cache" x="0" y="${hIn+hOut}" width="${bw}" height="${hC}" rx="1"/>
      <rect class="am-bar-out"  x="0" y="${hIn}"     width="${bw}" height="${hOut}" rx="1"/>
      <rect class="am-bar-in"   x="0" y="0"          width="${bw}" height="${hIn}" rx="1"/>
    </g>`;
  }).join("");
  return `<svg class="am-barstack" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">${bars}</svg>`;
}

/* ---------- Donut 环形图（多段） ---------- */

export function donut({ slices, size=120, stroke=14 }){
  const total = Math.max(1, ...slices.map(s => s.value || 0)) ;
  const r = (size - stroke) / 2, c = 2 * Math.PI * r, cx = size/2, cy = size/2;
  let acc = 0;
  const segs = slices.map((s, i) => {
    const frac = (s.value || 0) / total;
    const dash = frac * c;
    const off = -acc * c; acc += frac;
    return `<circle class="am-donut-seg" cx="${cx}" cy="${cy}" r="${r}" fill="none"
      stroke="${s.color || "var(--accent,#D96B4F)"}" stroke-width="${stroke}"
      stroke-dasharray="${dash.toFixed(1)} ${(c-dash).toFixed(1)}"
      stroke-dashoffset="${off.toFixed(1)}"
      transform="rotate(-90 ${cx} ${cy})" data-i="${i}">
      <title>${esc(s.label||"")}: ${fmtInt(s.value)}</title>
    </circle>`;
  }).join("");
  return `<svg class="am-donut" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true">${segs}</svg>`;
}

/* ---------- 可排序表格 ---------- */

export function sortableTable({ headers, rows, onSort, sortKey, sortDir }){
  const thead = headers.map(h => `
    <th data-key="${h.key}" class="${h.align === "right" ? "num" : ""} ${sortKey === h.key ? "sorted" : ""}"
        role="button" tabindex="0" aria-sort="${sortKey === h.key ? (sortDir === "asc" ? "ascending" : "descending") : "none"}">
      ${esc(h.label)}${sortKey === h.key ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
    </th>`).join("");
  const tbody = rows.map(r => `<tr>${headers.map(h => `<td class="${h.align === "right" ? "num" : ""}">${r[h.key] ?? "—"}</td>`).join("")}</tr>`).join("");
  return { html: `<table class="am-dtable"><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>`,
           theadEl: null, bind: null };
}

/* ---------- 时间线 ---------- */

export function timeline(items){
  return `<div class="am-timeline">${items.map((it, i) => `
    <div class="am-tl-item am-row-in" style="animation-delay:${Math.min(i * 28, 220)}ms">
      <span class="am-tl-dot" style="background:${it.color || "var(--accent,#D96B4F)"}"></span>
      <span class="am-tl-time">${fmtTime(it.ts)}</span>
      <span class="am-tl-type">${esc(it.type)}</span>
      <span class="am-tl-agent">${esc(it.agent || "")}</span>
      <span class="am-tl-text">${esc(it.text || "")}</span>
    </div>`).join("")}</div>`;
}

/* ---------- 三态 ---------- */

export function emptyState(msg, icon="◌"){ return `<div class="am-state empty"><span class="am-state-ico">${icon}</span><span>${esc(msg)}</span></div>`; }
export function loadingState(msg="加载中…"){ return `<div class="am-state loading">${esc(msg)}<span class="am-shimmer"></span></div>`; }
export function errorState(msg, onRetry){
  return `<div class="am-state error"><span class="am-state-ico">⚠</span><span>${esc(msg)}</span>
    ${onRetry ? `<button class="am-btn am-btn-retry" type="button">重试</button>` : ""}</div>`;
}
