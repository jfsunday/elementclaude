from __future__ import annotations

import pytest

from app.commands.builtin import parse_voice_args


def test_no_args_means_show_state() -> None:
    assert parse_voice_args("") is None
    assert parse_voice_args("   ") is None


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("on", {"stt_enabled": True, "tts_enabled": True}),
        ("off", {"stt_enabled": False, "tts_enabled": False}),
        ("stt on", {"stt_enabled": True}),
        ("stt off", {"stt_enabled": False}),
        ("tts on", {"tts_enabled": True}),
        ("TTS OFF", {"tts_enabled": False}),
        ("engine local", {"voice_engine": "local"}),
        ("engine cloud", {"voice_engine": "cloud"}),
        ("voice de-DE-KatjaNeural", {"tts_voice": "de-DE-KatjaNeural"}),
        ("voice default", {"tts_voice": None}),
        ("voice reset", {"tts_voice": None}),
    ],
)
def test_valid_args(args: str, expected: dict) -> None:
    assert parse_voice_args(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        "yes",
        "stt",
        "stt maybe",
        "engine",
        "engine remote",
        "voice",
        "voice a b",
        "on off",
        "engine local extra",
    ],
)
def test_invalid_args_raise(args: str) -> None:
    with pytest.raises(ValueError):
        parse_voice_args(args)


def test_parsed_fields_are_real_room_columns() -> None:
    from app.models import Room

    columns = set(Room.__table__.columns.keys())
    for args in ("on", "stt off", "engine local", "voice x"):
        assert set(parse_voice_args(args) or {}) <= columns
