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


def test_web_is_readonly():
    """页面只读：仅 GET/SSE，不得出现任何 POST 调用。"""
    html = WEB_HTML.read_text(encoding="utf-8")
    assert "POST" not in html
