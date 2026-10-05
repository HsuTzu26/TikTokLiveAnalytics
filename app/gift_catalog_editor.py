"""Local visual editor for Gift ID Chinese display and TTS names."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from src.tts.gift_catalog_editor import (
    EDITOR_COLUMNS,
    apply_translation_edits,
    build_editor_rows,
    catalog_statistics,
    merge_observed_gifts,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "src" / "tts" / "gift_catalog.json"
UNMAPPED_GIFTS_PATH = ROOT / "data" / "tts" / "unmapped_gifts.ndjson"
def _read_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _write_catalog(catalog: dict) -> None:
    temp = CATALOG_PATH.with_name(f"{CATALOG_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, CATALOG_PATH)
    finally:
        if temp.exists():
            temp.unlink()


st.set_page_config(page_title="TikTok Gift 中英文對照", layout="wide")
st.title("TikTok Gift 中英文對照")
st.caption("依 Gift ID 編輯中文顯示名與 TTS 發音名。圖片從來源 CDN 載入，不會下載到本機。")

try:
    catalog = _read_catalog()
except (OSError, ValueError) as exc:
    st.error(f"無法讀取禮物目錄：{exc}")
    st.stop()

if "gift_catalog_saved_notice" in st.session_state:
    st.success(st.session_state.pop("gift_catalog_saved_notice"))

def _read_unmapped_gifts() -> list[dict]:
    observations = []
    try:
        with UNMAPPED_GIFTS_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    observations.append(row)
    except OSError:
        pass
    return observations


observations = _read_unmapped_gifts()
if observations:
    catalog, imported_count = merge_observed_gifts(
        catalog,
        observations,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    if imported_count:
        try:
            _write_catalog(catalog)
        except OSError as exc:
            st.warning(f"\u65b0\u79ae\u7269\u5df2\u986f\u793a\uff0c\u4f46\u7121\u6cd5\u5beb\u56de\u6e05\u55ae\uff1a{exc}")
        else:
            for key in list(st.session_state.keys()):
                if key.startswith("gift_translation_editor_"):
                    st.session_state.pop(key, None)
            st.info(f"\u5df2\u5f9e\u76f4\u64ad\u8a18\u9304\u532f\u5165 {imported_count} \u7a2e Gift ID\uff0c\u8acb\u5230\u300c\u5f85\u88dc\u300d\u6216\u300c\u5168\u90e8\u300d\u586b\u5beb\u4e2d\u6587\u540d\u7a31\u3002")


statistics = catalog_statistics(catalog)
st.caption(
    f"網站清單 {statistics['website']} 種；直播實測 {statistics['observed']} 種，"
    f"其中網站未列出的 {statistics['event_only']} 種會標為「直播實測追加」。"
    "同名但 Gift ID 不同的項目會分開保留。"
)
source_labels = {
    f"網站清單（{statistics['website']} 種）": "網站清單",
    f"全部來源（含實測追加 {statistics['event_only']} 種）": "全部來源",
    f"只看實測追加（{statistics['event_only']} 種）": "直播實測追加",
}
source_choice = st.selectbox("資料範圍", tuple(source_labels), index=1)
source_scope = source_labels[source_choice]
view = st.radio("顯示", ("已儲存", "待補", "全部"), horizontal=True, index=1)
search = st.text_input("搜尋 Gift ID 或名稱", placeholder="例如：Rose、玫瑰、5655")
scope_rows = build_editor_rows(catalog, view="全部", source_scope=source_scope)
records = build_editor_rows(
    catalog, view=view, query=search, source_scope=source_scope
)
scope_saved = sum(row["狀態"] == "已儲存" for row in scope_rows)
scope_pending = len(scope_rows) - scope_saved
st.caption(
    f"目前範圍共 {len(scope_rows)} 種；已儲存 {scope_saved} 種；"
    f"待補 {scope_pending} 種。套用狀態與搜尋後顯示 {len(records)} 種。"
)

frame = pd.DataFrame(records, columns=EDITOR_COLUMNS)
editor_key = f"gift_translation_editor_{source_scope}_{view}_{search.casefold().strip()}"
edited = st.data_editor(
    frame,
    key=editor_key,
    hide_index=True,
    num_rows="fixed",
    height=720,
    width="stretch",
    column_config={
        "圖片": st.column_config.ImageColumn("圖片", width="small"),
        "Gift ID": st.column_config.TextColumn("Gift ID", disabled=True, width="small"),
        "Gift Name": st.column_config.TextColumn("Gift Name", disabled=True, width="medium"),
        "直播原名": st.column_config.TextColumn("直播原名", disabled=True, width="medium"),
        "Coin": st.column_config.NumberColumn("Coin", disabled=True, format="%d", width="small"),
        "資料來源": st.column_config.TextColumn("資料來源", disabled=True, width="medium"),
        "中文顯示名": st.column_config.TextColumn("中文顯示名", help="報表與 UI 使用"),
        "TTS 發音名": st.column_config.TextColumn("TTS 發音名", help="語音實際唸出的名稱"),
        "狀態": st.column_config.TextColumn("狀態", disabled=True, width="small"),
    },
)

if st.button("儲存中文對照", type="primary", disabled=edited.empty):
    updates = edited[["Gift ID", "中文顯示名", "TTS 發音名"]].to_dict("records")
    timestamp = datetime.now(timezone.utc).isoformat()
    updated_catalog, changed = apply_translation_edits(
        catalog, updates, updated_at=timestamp
    )
    try:
        _write_catalog(updated_catalog)
    except OSError as exc:
        st.error(f"儲存失敗：{exc}")
    else:
        st.session_state["gift_catalog_saved_notice"] = f"已儲存 {changed} 筆 Gift ID 對照。"
        st.session_state.pop(editor_key, None)
        st.rerun()

st.info("空白名稱會保留為待補；原始禮物名稱、Coin 與實測鑽石數不會被翻譯欄位覆蓋。")
