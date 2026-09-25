"""Localization helpers for the local Streamlit UI."""
from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st


LANGUAGE_KEY = "ui_language"
DEFAULT_LANGUAGE = "zh-TW"
LANGUAGES = {"zh-TW": "繁體中文", "en": "English"}
SETTINGS_PATH = Path(__file__).resolve().parents[1] / "data" / "ui_settings.json"


def _saved_language() -> str:
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_LANGUAGE
    value = settings.get("language") if isinstance(settings, dict) else None
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def _save_language() -> None:
    value = st.session_state.get(LANGUAGE_KEY, DEFAULT_LANGUAGE)
    if value not in LANGUAGES:
        return
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = SETTINGS_PATH.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps({"language": value}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, SETTINGS_PATH)


def language() -> str:
    value = st.session_state.get(LANGUAGE_KEY, _saved_language())
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def tr(english: str, traditional_chinese: str | None = None) -> str:
    """Return the selected-language label, defaulting to English when untranslated."""
    if language() == "en":
        return english
    return traditional_chinese if traditional_chinese is not None else english


def render_language_selector() -> None:
    st.session_state.setdefault(LANGUAGE_KEY, _saved_language())
    st.sidebar.selectbox(
        "介面語言 / UI language",
        options=list(LANGUAGES),
        format_func=LANGUAGES.__getitem__,
        key=LANGUAGE_KEY,
        on_change=_save_language,
    )
