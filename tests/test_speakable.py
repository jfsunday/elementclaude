from __future__ import annotations

from app.voice.tts import speakable


def test_strips_fenced_code_blocks() -> None:
    out = speakable("Here you go:\n\n```python\nprint('secret')\n```\n\nDone.")
    assert "print" not in out
    assert "```" not in out
    assert "Here you go" in out
    assert "Done." in out


def test_strips_inline_code_but_keeps_the_word() -> None:
    assert speakable("run `!cancel` to stop") == "run !cancel to stop"


def test_strips_urls_and_keeps_link_text() -> None:
    out = speakable("See [the docs](https://example.com/a/b) and https://example.com/raw")
    assert "https://" not in out
    assert "the docs" in out


def test_strips_markdown_noise() -> None:
    out = speakable("## Heading\n\n- **bold** item\n- _italic_ item")
    assert out == "Heading\nbold item\nitalic item"


def test_truncates_at_max_chars_on_a_boundary() -> None:
    text = "Alpha beta gamma. " * 40
    out = speakable(text, max_chars=100)
    assert len(out) <= 102  # room for the appended ellipsis
    assert out.endswith("…")
    assert not out.startswith(" ")


def test_short_text_is_untouched() -> None:
    assert speakable("just a sentence", max_chars=1000) == "just a sentence"


def test_code_only_answer_becomes_empty() -> None:
    assert speakable("```\nls -la\n```") == ""
