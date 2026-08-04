from pathlib import Path

from agent_mcp.events import EVENT_TYPES

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_HTML = PROJECT_ROOT / "web" / "index.html"


def test_web_has_core_elements():
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "EventSource" in html
    assert "snapshot" in html
    assert "spawned" in html
    assert "token" in html.lower()


def test_web_no_external_deps():
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html
    assert "<script src" not in html and "<link rel" not in html


def test_web_handles_all_event_types():
    """事件分发必须覆盖 events.py 定义的全集（含 message_delta 双轨）。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    for typ in sorted(EVENT_TYPES):
        assert typ in html, f"missing event dispatch for {typ}"


def test_web_has_authenticated_operator_actions():
    """操作写请求使用本机 token，覆盖 steer/followup/interrupt。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    assert 'method:"POST"' in html
    assert '"X-Auth-Token":S.token' in html
    assert '"/api/agents/steer"' in html
    assert '"/api/agents/followup"' in html
    assert '"/api/agents/interrupt"' in html


def test_web_narrow_drawer_detail():
    """窄屏下详情变为底部抽屉：media query + drawer-open 切换 + 切换/关闭按钮。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "@media (max-width:860px)" in html
    assert "drawer-open" in html
    assert 'id="drawer-toggle"' in html
    assert 'id="detail-close"' in html
    assert "translateY" in html


def test_web_keyboard_focus_and_reduced_motion():
    """键盘焦点（tabindex/role/Enter/空格/Esc）与 prefers-reduced-motion。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    assert 'setAttribute("tabindex"' in html
    assert 'setAttribute("role","button")' in html
    assert 'e.key!=="Enter"' in html
    assert 'e.key!==" "' in html
    assert 'e.key==="Escape"' in html
    assert "prefers-reduced-motion" in html
    assert "focus-visible" in html


def test_web_truthful_live_buffered_hints():
    """数据新鲜度如实标注：SSE 在线才标 LIVE，断线/未连接一律 BUFFERED（快照缓存非实时）。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    assert 'id="live-tag"' in html
    assert "LIVE" in html
    assert "BUFFERED" in html
    assert "快照缓存" in html
    assert "非实时" in html


def test_web_atomcode_capability_hint():
    """AtomCode 能力如实提示：one-shot、verbose usage 已解析、无 stable resume。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "AtomCode" in html
    assert "one-shot" in html
    assert "verbose token usage 已解析" in html
    assert "resume" in html
    assert 'a.cli==="atomcode"' in html


def test_web_sse_error_and_empty_states():
    """SSE 断线/快照错误/对话图空态文案齐全。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "断线重连" in html
    assert "快照不可用" in html
    assert "无法连接 daemon" in html
    assert "暂无对话节点" in html


def test_web_detail_retention():
    """详情保留：选中节点写入 localStorage，清状态/切会话不得重置选中。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    assert 'SEL_KEY="amcp_sel"' in html
    assert "localStorage.setItem(SEL_KEY" in html
    assert "S.selected=null" not in html


def test_web_sse_timeout_maps_to_incomplete():
    """agent.terminated + stop_reason=timeout 必须即时映射为 incomplete（超时）：
    不得显示绿色完成、不得计入已完成（done 只统计 status==="terminated"）。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    assert 'payload.stop_reason==="timeout"' in html
    assert 'a.status=payload.stop_reason==="timeout"?"incomplete":"terminated"' in html


def test_web_drawer_controls_rerender_aria_expanded_on_close():
    """关闭按钮与 Escape 关闭抽屉后必须重渲染 aria-expanded=false。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    assert 'id==="detail-close"){$("detail-pane").classList.remove("drawer-open");scheduleRender();return;}' in html
    assert 'e.key==="Escape"){$("detail-pane").classList.remove("drawer-open");scheduleRender();}' in html


def test_web_is_operator_console_with_steer_followup_and_stop():
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "Conversation graph" in html
    assert 'data-mode="steer"' in html
    assert 'data-mode="followup"' in html
    assert 'id="op-message"' in html
    assert 'id="op-send"' in html
    assert 'id="op-stop"' in html
    assert '"/api/agents/steer"' in html
    assert '"/api/agents/followup"' in html
    assert '"/api/agents/interrupt"' in html


def test_web_operator_uses_fragment_auth_and_recovery_feedback():
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "location.hash" in html
    assert "history.replaceState" in html
    assert 'fetch("/api/config")' not in html
    assert '"X-Auth-Token":S.token' in html
    assert "失败：请输入新的方向或下一步任务" in html
    assert "已恢复原会话" in html
    assert "AtomCode 将新开 one-shot" in html


def test_web_graph_is_horizontal_only_and_models_user_turns():
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "overflow-x:auto" in html
    assert "overflow-y:hidden" in html
    assert "agent.user_turn" in html
    assert "每次用户输入生成一个节点" in html
    assert "function userTurns" in html
    assert "X_STEP=226" in html


def test_web_operator_scopes_writes_to_selected_session():
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "session_id:a.session_id" in html


def test_web_dense_columns_expand_horizontally_without_vertical_scroll():
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "maxRows" in html
    assert "columnGroups" in html
    assert "overflow-y:hidden" in html


def test_web_mobile_form_controls_avoid_ios_focus_zoom():
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "@media (max-width:639px)" in html
    assert "font-size:16px" in html
