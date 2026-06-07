"""Integration tests for format_for_telegram pipeline."""
import sys, os, unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.modules["config"] = unittest.mock.MagicMock()
sys.modules["config.settings"] = unittest.mock.MagicMock()
sys.modules["config.settings"].settings = unittest.mock.MagicMock()
sys.modules["config.settings"].settings.TELEGRAM_BOT_TOKEN = "fake:token"
sys.modules["config.settings"].settings.TELEGRAM_CHAT_ID = 0

import telegram_bot

fmt = telegram_bot.format_for_telegram


def test_plain_strips_html():
    r = fmt("<b>x</b> & <i>y</i>", context="plain")
    assert "<b>" not in r and "<i>" not in r


def test_message_converts_markdown():
    r = fmt("**b** *a* `c` _i_")
    assert "<b>b</b>" in r
    assert "<code>c</code>" in r
    assert "<i>i</i>" in r


def test_message_truncates():
    r = fmt("x" * 5000)
    assert len(r) <= 4096
    assert "truncado" in r


def test_caption_no_truncate():
    r = fmt("x" * 5000, context="caption")
    assert len(r) > 4096


def test_html_preserved():
    r = fmt("<b>keep</b>")
    assert "<b>keep</b>" in r


def test_mixed_markdown_html():
    r = fmt("<b>bold</b> **also**")
    assert "<b>also</b>" in r


def test_backtick_code():
    r = fmt("use `code` here")
    assert "<code>code</code>" in r


def test_ampersand_escaped_once():
    r = fmt("AT&T")
    assert "AT&amp;T" in r
    assert "&amp;amp;" not in r


def test_empty_and_none():
    for ctx in ("message", "caption", "edit", "plain"):
        assert fmt("", ctx) == ""
    assert fmt(None) == ""


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} passed")
    sys.exit(0 if failed == 0 else 1)
