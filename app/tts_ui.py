"""Streamlit controls for the optional local LIVE chat TTS worker."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
import asyncio
from datetime import datetime
from pathlib import Path

import streamlit as st

from src.tts.pipeline import TTSSettings, spoken_gift_name
from src.tts.gift_catalog import GIFT_CATALOG, gift_metadata_for
from src.tts.audio import EdgeTTSBackend, PygameAudioPlayer
from app.i18n import tr


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
TTS_ROOT = DATA_ROOT / "tts"
RAW_ROOT = DATA_ROOT / "raw"
BENCHMARK_ROOT = DATA_ROOT / "v2_provider_benchmark"
BROWSER_PROBE_ROOT = DATA_ROOT / "browser_ws_probe"
GIFT_SOUND_ROOT = TTS_ROOT / "sounds"
WATCHER_ROOT = DATA_ROOT / "watcher"
WATCHER_STATE = WATCHER_ROOT / "watcher_state.json"
WATCHER_PID = WATCHER_ROOT / "watcher.pid"
WORKER_SCRIPT = ROOT / "src" / "tts_worker.py"
CONFIG_PATH = TTS_ROOT / "config.json"
PREVIEW_PATH = TTS_ROOT / "preview_request.json"
STATE_PATH = TTS_ROOT / "state.json"
STOP_PATH = TTS_ROOT / "stop.json"
LOG_PATH = TTS_ROOT / "worker.log"

VOICE_LABELS = {
    "zh-TW-HsiaoChenNeural": "Chinese (Taiwan) · HsiaoChen · female",
    "zh-TW-HsiaoYuNeural": "Chinese (Taiwan) · HsiaoYu · female",
    "zh-TW-YunJheNeural": "Chinese (Taiwan) · YunJhe · male",
    "en-US-AriaNeural": "English (US) · Aria · female",
    "en-US-JennyNeural": "English (US) · Jenny · female",
    "en-US-GuyNeural": "English (US) · Guy · male",
    "en-US-DavisNeural": "English (US) · Davis · male",
    "en-GB-SoniaNeural": "English (UK) · Sonia · female",
    "en-GB-RyanNeural": "English (UK) · Ryan · male",
}
VOICE_PREVIEWS = {
    "zh-TW": "\u4f60\u597d\uff0c\u9019\u662f\u76f4\u64ad\u8a9e\u97f3\u9810\u89bd\u3002",
    "en-US": "Hello, this is a preview of your live stream voice.",
}


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def available_voices(language: str, current_voice: str) -> list[str]:
    prefix = "zh-TW-" if language == "zh-TW" else "en-"
    voices = [voice for voice in VOICE_LABELS if voice.startswith(prefix)]
    if current_voice and current_voice not in voices:
        voices.append(current_voice)
    return voices


def voice_label(voice: str) -> str:
    parts = VOICE_LABELS.get(voice, f"Custom · {voice}").split(" · ")
    family, name = parts[0], parts[1]
    gender = parts[2] if len(parts) > 2 else ""
    if family == "Chinese (Taiwan)":
        family = tr(family, "中文（台灣）")
    elif family.startswith("English"):
        family = tr(family, "英文")
    else:
        family = tr(family, "自訂")
    gender = tr(gender, "女聲" if gender == "female" else "男聲") if gender in {"female", "male"} else gender
    return f"{family} · {name} · {gender}" if gender else f"{family} · {name}"


async def _play_preview(text: str, voice: str, rate_percent: int, volume: int) -> None:
    backend = EdgeTTSBackend()
    player = PygameAudioPlayer()
    audio_path = None
    try:
        player.initialize()
        audio_path = await backend.synthesize(text, voice, rate_percent)
        await player.play(audio_path, volume)
    finally:
        if audio_path is not None:
            audio_path.unlink(missing_ok=True)
        player.close()


def apply_voice_selection(language: str, state_key: str) -> None:
    voice = str(st.session_state.get(state_key) or "").strip()
    if not voice:
        return
    config = read_json(CONFIG_PATH, {})
    setting_key = "zh_voice" if language == "zh-TW" else "en_voice"
    config[setting_key] = voice
    write_json(CONFIG_PATH, config)

    preview = {
        "id": uuid.uuid4().hex,
        "language": language,
        "voice": voice,
        "text": VOICE_PREVIEWS[language],
    }
    worker_state = read_json(STATE_PATH, {})
    if isinstance(worker_state, dict) and pid_is_running(worker_state.get("pid")):
        write_json(PREVIEW_PATH, preview)
        st.session_state["tts_voice_preview_status"] = (
            f"{tr('Preview queued', '已加入語音預覽佇列')}：{voice_label(voice)}"
        )
        return

    settings = TTSSettings.from_mapping(config)
    try:
        asyncio.run(
            _play_preview(
                preview["text"], voice, settings.rate_percent, settings.volume
            )
        )
        st.session_state["tts_voice_preview_status"] = tr("Voice preview played.", "語音預覽已播放。")
    except Exception as error:
        st.session_state["tts_voice_preview_status"] = (
            f"{tr('Voice preview failed', '語音預覽失敗')}：{type(error).__name__}"
        )


def pid_is_running(pid: object) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def watcher_pid() -> int | None:
    try:
        return int(WATCHER_PID.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def benchmark_sessions(username: str) -> list[Path]:
    if not BENCHMARK_ROOT.exists():
        return []
    suffix = f"_{username.casefold()}"
    result = []
    try:
        for provider_dir in BENCHMARK_ROOT.iterdir():
            if not provider_dir.is_dir():
                continue
            for session_dir in provider_dir.iterdir():
                if (
                    session_dir.is_dir()
                    and session_dir.name.casefold().endswith(suffix)
                    and (session_dir / "events.ndjson").is_file()
                    and not (session_dir / "summary.json").exists()
                ):
                    result.append(session_dir)
    except OSError:
        return []
    return sorted(result, key=lambda path: path.stat().st_mtime, reverse=True)


def browser_sessions(username: str) -> list[Path]:
    if not BROWSER_PROBE_ROOT.exists():
        return []
    suffix = f"_{username.casefold()}"
    try:
        result = [
            path for path in BROWSER_PROBE_ROOT.iterdir()
            if path.is_dir()
            and path.name.casefold().endswith(suffix)
            and (path / "events.ndjson").is_file()
        ]
    except OSError:
        return []
    return sorted(result, key=lambda path: path.stat().st_mtime, reverse=True)


def discovered_gifts(username: str) -> list[tuple[str, str]]:
    """Merge catalogued Gift IDs with recent names, without sender/chat fields."""
    suffix = f"_{username.casefold()}"
    sessions = browser_sessions(username)[:8]
    for root in (RAW_ROOT,):
        if root.exists():
            try:
                sessions.extend(
                    path for path in root.iterdir()
                    if path.is_dir()
                    and path.name.casefold().endswith(suffix)
                    and (path / "events.ndjson").is_file()
                )
            except OSError:
                pass
    gifts: dict[str, str] = {}
    for gift_id, metadata in GIFT_CATALOG.items():
        catalog_name = (
            metadata.get("original_name")
            or metadata.get("name_zh_display")
            or metadata.get("name_en")
            or metadata.get("standout_name")
        )
        if isinstance(catalog_name, str) and catalog_name:
            gifts[gift_id] = catalog_name
    for session_dir in sorted(
        set(sessions), key=lambda path: path.stat().st_mtime, reverse=True
    )[:8]:
        event_path = session_dir / "events.ndjson"
        try:
            with event_path.open("rb") as handle:
                size = event_path.stat().st_size
                if size > 2_000_000:
                    handle.seek(size - 2_000_000)
                    handle.readline()
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines[-5000:]):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") != "gift":
                continue
            gift_id = str(event.get("gift_id") or "").strip()
            gift_name = str(event.get("gift_name") or "").strip()
            if gift_id and gift_name:
                gifts.setdefault(gift_id, gift_name)
    return sorted(gifts.items(), key=lambda item: (item[1].casefold(), item[0]))


def discovered_chat_users(username: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return recent sender IDs and labels without retaining any message text."""
    cache_key = f"tts_known_users_{username.casefold()}"
    cached = st.session_state.get(cache_key)
    if isinstance(cached, dict) and time.monotonic() - cached.get("loaded_at", 0) < 5:
        return cached.get("users", {}), cached.get("labels", {})

    suffix = f"_{username.casefold()}"
    sessions = browser_sessions(username)[:2]
    if RAW_ROOT.exists():
        try:
            sessions.extend(
                path for path in RAW_ROOT.iterdir()
                if path.is_dir()
                and path.name.casefold().endswith(suffix)
                and (path / "events.ndjson").is_file()
            )
        except OSError:
            pass
    users: dict[str, str] = {}
    labels: dict[str, str] = {}
    for session_dir in sorted(
        set(sessions), key=lambda path: path.stat().st_mtime, reverse=True
    )[:3]:
        event_path = session_dir / "events.ndjson"
        try:
            with event_path.open("rb") as handle:
                size = event_path.stat().st_size
                if size > 500_000:
                    handle.seek(size - 500_000)
                    handle.readline()
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines[-1500:]):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") not in {"chat", "member", "gift"}:
                continue
            identity = str(event.get("unique_id") or event.get("user_id") or "").strip().lstrip("@").casefold()
            if identity:
                users.setdefault(identity, str(event.get("unique_id") or event.get("user_id") or identity))
                nickname = str(event.get("nickname") or "").strip()
                labels.setdefault(identity, f"@{users[identity]}" + (f" · {nickname}" if nickname else ""))
    st.session_state[cache_key] = {
        "loaded_at": time.monotonic(),
        "users": users,
        "labels": labels,
    }
    return users, labels


def start_worker(
    username: str,
    settings: TTSSettings,
    session_dir: Path | None = None,
) -> tuple[bool, str]:
    TTS_ROOT.mkdir(parents=True, exist_ok=True)
    current_state = read_json(STATE_PATH, {})
    current_pid = current_state.get("pid") if isinstance(current_state, dict) else None
    if pid_is_running(current_pid):
        return False, tr(
            f"TTS worker already running (PID {current_pid}). Stop it before switching streamer.",
            f"TTS Worker 已在執行（PID {current_pid}）。切換直播主前請先停止。",
        )

    write_json(CONFIG_PATH, settings.to_mapping())
    STOP_PATH.unlink(missing_ok=True)
    log_handle = LOG_PATH.open("ab")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    command = [sys.executable, "-X", "utf8", "-m", "src.tts_worker", "--username", username]
    if session_dir is not None:
        resolved = session_dir.resolve()
        session_flag = (
            "--browser-session-dir"
            if resolved.is_relative_to(BROWSER_PROBE_ROOT.resolve())
            else "--session-dir"
        )
        command.extend([session_flag, str(resolved)])
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except OSError as error:
        log_handle.close()
        return False, tr(
            f"Could not start TTS worker ({type(error).__name__}).",
            f"無法啟動 TTS Worker（{type(error).__name__}）。",
        )
    finally:
        if not log_handle.closed:
            log_handle.close()

    time.sleep(0.15)
    if process.poll() is not None:
        failed = read_json(STATE_PATH, {})
        reason = failed.get("last_error") if isinstance(failed, dict) else None
        return False, tr(
            f"TTS worker exited during startup{': ' + str(reason) if reason else ''}; check its log below.",
            f"TTS Worker 啟動時結束{('：' + str(reason)) if reason else ''}；請查看下方紀錄。",
        )

    state = read_json(STATE_PATH, {})
    if not isinstance(state, dict) or state.get("pid") != process.pid:
        state = {
            "pid": process.pid,
            "username": username,
            "source_session_dir": str(session_dir.resolve()) if session_dir else None,
            "status": "starting",
            "started_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
            "updated_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
            "metrics": {},
        }
        write_json(STATE_PATH, state)
    if session_dir is None:
        source = tr("Watcher collector", "Watcher Collector")
    elif session_dir.resolve().is_relative_to(BROWSER_PROBE_ROOT.resolve()):
        source = tr(f"Browser Network session {session_dir.name}", f"Browser Network \u5834\u6b21 {session_dir.name}")
    else:
        source = tr(f"benchmark session {session_dir.name}", f"Benchmark \u5834\u6b21 {session_dir.name}")
    return True, tr(
        f"TTS worker started for @{username} from {source} (PID {process.pid}).",
        f"已為 @{username} 啟動 TTS Worker，來源：{source}（PID {process.pid}）。",
    )


def _store_tts_audio(uploaded, category: str, key: str) -> str:
    suffix = Path(uploaded.name).suffix.casefold()
    if suffix not in {".wav", ".mp3", ".ogg"}:
        raise ValueError("Choose a WAV, MP3, or OGG audio file.")
    content = uploaded.getvalue()
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("Audio files must be 20 MB or smaller.")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(uploaded.name).stem).strip("_")[:48]
    safe_key = re.sub(r"[^A-Za-z0-9_-]+", "_", key).strip("_")[:64] or "default"
    GIFT_SOUND_ROOT.mkdir(parents=True, exist_ok=True)
    path = GIFT_SOUND_ROOT / f"{category}_{safe_key}_{stem or 'sound'}{suffix}"
    path.write_bytes(content)
    return path.relative_to(ROOT).as_posix()


def _store_gift_audio(uploaded, gift_id: str) -> str:
    return _store_tts_audio(uploaded, "gift", gift_id)


def render_gift_configuration(username: str, settings: TTSSettings, running: bool) -> None:
    with st.expander(tr("Gift pronunciation and sounds", "禮物中文名稱與音效設定")):
        st.caption(tr(
            "Choose a Gift captured from this streamer, upload an audio file, and optionally set the Chinese name TTS should say. No special file name is required. The sound starts after the completed Gift announcement; later announcements can continue while it plays.",
            "選擇這位直播主實際出現過的 Gift，上傳音效檔，也可設定 TTS 要念的中文名稱。不需要特定檔名；音效會在 Gift 播報結束後開始，後續 TTS 不必等音效播完。",
        ))
        st.caption(tr(
            "Gift names and sound mappings reload in a running TTS worker.",
            "\u79ae\u7269\u540d\u7a31\u8207\u97f3\u6548\u8a2d\u5b9a\u6703\u81ea\u52d5\u5957\u7528\u5230\u57f7\u884c\u4e2d\u7684 TTS worker\u3002",
        ))
        discovered = dict(discovered_gifts(username))
        sound_by_id = dict(settings.gift_sound_by_id)
        name_by_id = dict(settings.gift_name_by_id)
        for gift_id in (*sound_by_id.keys(), *name_by_id.keys()):
            discovered.setdefault(gift_id, f"Gift ID {gift_id}")
        target = st.radio(
            tr("Apply the sound to", "音效套用範圍"),
            options=("specific", "all"),
            format_func=lambda value: tr("One selected Gift", "指定一種 Gift") if value == "specific" else tr("Every completed Gift", "所有完成的 Gift"),
            horizontal=True,
            key=f"tts_gift_target_{username}",
        )
        gift_id = ""
        spoken_name = ""
        current_sound = settings.gift_followup_audio_path
        if target == "specific":
            if discovered:
                gift_ids = sorted(discovered, key=lambda value: (discovered[value].casefold(), value))
                manual_option = "__manual_gift_id__"

                def gift_option_label(value: str) -> str:
                    if value == manual_option:
                        return tr("Enter another Gift ID", "手動輸入其他 Gift ID")
                    raw_name = discovered[value]
                    metadata = gift_metadata_for(value, raw_name)
                    speech_name = spoken_gift_name(
                        value, raw_name, settings.gift_name_by_id
                    )
                    chinese_name = metadata.get("name_zh_display") or speech_name
                    english_name = metadata.get("name_en")
                    if english_name and str(chinese_name).casefold() != str(english_name).casefold():
                        names = f"{chinese_name} / {english_name}"
                    elif english_name:
                        names = str(english_name)
                    else:
                        names = f"{chinese_name} / 英文名稱待補"
                    return f"{names} · ID {value}"

                gift_id = st.selectbox(
                    tr("Captured Gift", "已擷取的 Gift"),
                    options=[*gift_ids, manual_option],
                    format_func=gift_option_label,
                    key=f"tts_gift_id_{username}",
                )
                if gift_id == manual_option:
                    gift_id = st.text_input(
                        tr("Gift ID", "Gift ID"),
                        key=f"tts_gift_id_manual_{username}",
                    ).strip()
            else:
                gift_id = st.text_input(
                    tr("Gift ID", "Gift ID"),
                            key=f"tts_gift_id_manual_{username}",
                ).strip()
            if gift_id:
                suggested_name = name_by_id.get(gift_id) or spoken_gift_name(
                    gift_id,
                    discovered.get(gift_id, ""),
                    settings.gift_name_by_id,
                    tts_name=gift_metadata_for(
                        gift_id, discovered.get(gift_id, "")
                    ).get("name_zh_tts"),
                )
                spoken_name = st.text_input(
                    tr("Chinese name to speak (optional)", "TTS 中文播報名稱（選填）"),
                    value=suggested_name,
                    help=tr("Common Gifts already have Traditional Chinese speech names. You can change the name here; captured events and reports keep TikTok's original Gift name.", "常見禮物已設繁體中文播報名稱，也可在這裡修改；擷取事件和報表仍保留 TikTok 原始禮物名稱。"),
                            key=f"tts_gift_name_{username}_{gift_id}",
                )
                current_sound = sound_by_id.get(gift_id, "")
        st.caption(
            tr("Current sound: ", "目前音效：")
            + (current_sound or tr("off", "關閉"))
        )
        upload = st.file_uploader(
            tr("Choose a WAV, MP3, or OGG sound file", "選擇 WAV、MP3 或 OGG 音效檔"),
            type=("wav", "mp3", "ogg"),
            key=f"tts_gift_audio_upload_{username}_{target}_{gift_id or 'all'}",
        )
        clear_sound = st.checkbox(
            tr("Remove this sound mapping", "移除此音效設定"),
            value=False,
            key=f"tts_gift_audio_clear_{username}_{target}_{gift_id or 'all'}",
        )
        if st.button(
            tr("Save Gift setting", "儲存 Gift 設定"),
            key=f"tts_gift_audio_save_{username}_{target}_{gift_id or 'all'}",
        ):
            config = settings.to_mapping()
            sounds = dict(config.get("gift_sound_by_id") or {})
            names = dict(config.get("gift_name_by_id") or {})
            try:
                if target == "specific":
                    if not gift_id or not gift_id.isdigit():
                        raise ValueError("Select a captured Gift or enter a numeric Gift ID.")
                    if spoken_name.strip():
                        names[gift_id] = spoken_name.strip()
                    else:
                        names.pop(gift_id, None)
                    if upload is not None:
                        sounds[gift_id] = _store_gift_audio(upload, gift_id)
                    elif clear_sound:
                        sounds.pop(gift_id, None)
                else:
                    if upload is not None:
                        config["gift_followup_audio_path"] = _store_gift_audio(upload, "all")
                    elif clear_sound:
                        config["gift_followup_audio_path"] = ""
                config["gift_sound_by_id"] = sounds
                config["gift_name_by_id"] = names
                write_json(CONFIG_PATH, TTSSettings.from_mapping(config).to_mapping())
                st.success(tr("Gift setting saved.", "Gift 設定已儲存。"))
                st.rerun()
            except (OSError, ValueError) as error:
                st.error(str(error))


def render_member_sound_configuration(
    username: str, settings: TTSSettings, running: bool
) -> None:
    with st.expander(tr("Entry sounds", "進場音效")):
        st.caption(tr(
            "Upload sounds for any member entry, a minimum fan badge level, or one chosen TikTok user. A user-specific sound takes priority. No special file name is required; sounds mix with speech so they do not hold the TTS queue.",
            "可分別為所有人進場、指定最低粉絲燈牌等級、或特定 TikTok 使用者上傳音效。指定使用者音效優先；不需要特定檔名，音效會與語音混音，不會卡住 TTS 佇列。",
        ))
        target = st.selectbox(
            tr("Entry sound rule", "進場音效規則"),
            options=("all", "fan_club", "user"),
            format_func=lambda value: {
                "all": tr("Any member entry", "任何人進場"),
                "fan_club": tr("Fan badge level threshold", "粉絲燈牌等級門檻"),
                "user": tr("Specific user", "指定使用者"),
            }[value],
            disabled=running,
            key=f"tts_entry_sound_target_{username}",
        )
        user_key = ""
        config = settings.to_mapping()
        user_sounds = dict(settings.member_entry_sound_by_user)
        if target == "fan_club":
            fan_level = st.number_input(
                tr("Minimum fan badge level", "最低粉絲燈牌等級"),
                min_value=1,
                max_value=50,
                value=settings.fan_club_entry_min_level,
                disabled=running,
                help=tr("Set 1 for any recognized fan-club badge; use a higher level for an iron-fan style trigger.", "設為 1 可套用所有可辨識的粉絲燈牌；可提高門檻作為鐵粉進場觸發。"),
                key=f"tts_entry_sound_fan_level_{username}",
            )
            current_sound = settings.fan_club_entry_sound_path
        elif target == "user":
            known_users, known_labels = discovered_chat_users(username)
            user_options = sorted(set(known_users) | set(user_sounds))
            if user_options:
                user_key = st.selectbox(
                    tr("Choose a recently seen user", "選擇近期出現的使用者"),
                    options=user_options,
                    format_func=lambda value: known_labels.get(value, f"@{value}"),
                    disabled=running,
                    key=f"tts_entry_sound_user_choice_{username}",
                )
                manual_user = st.text_input(
                    tr("Or enter an account ID", "或輸入帳號 ID"),
                    disabled=running,
                    key=f"tts_entry_sound_user_manual_{username}",
                ).strip().lstrip("@").casefold()
                user_key = manual_user or user_key
            else:
                user_key = st.text_input(
                    tr("TikTok unique ID or user ID", "TikTok 帳號 ID 或使用者 ID"),
                    disabled=running,
                    key=f"tts_entry_sound_user_{username}",
                    help=tr("Use the account ID shown in captured events, with or without @.", "輸入事件紀錄中的帳號 ID，可加或不加 @。"),
                ).strip().lstrip("@").casefold()
            current_sound = user_sounds.get(user_key, "")
        else:
            fan_level = settings.fan_club_entry_min_level
            current_sound = settings.member_entry_sound_path
        st.caption(tr("Current sound: ", "目前音效：") + (current_sound or tr("off", "關閉")))
        upload = st.file_uploader(
            tr("Choose a WAV, MP3, or OGG sound file", "選擇 WAV、MP3 或 OGG 音效檔"),
            type=("wav", "mp3", "ogg"),
            disabled=running,
            key=f"tts_entry_audio_upload_{username}_{target}_{user_key or 'default'}",
        )
        clear_sound = st.checkbox(
            tr("Remove this sound mapping", "移除此音效設定"),
            value=False,
            disabled=running or not current_sound,
            key=f"tts_entry_audio_clear_{username}_{target}_{user_key or 'default'}",
        )
        if st.button(
            tr("Save entry sound", "儲存進場音效"),
            disabled=running or (target == "user" and not user_key),
            key=f"tts_entry_audio_save_{username}_{target}_{user_key or 'default'}",
        ):
            try:
                if upload is not None:
                    path = _store_tts_audio(upload, "entry", target if target != "user" else user_key)
                else:
                    path = ""
                if target == "all":
                    config["member_entry_sound_path"] = path if upload is not None else ("" if clear_sound else current_sound)
                elif target == "fan_club":
                    config["fan_club_entry_sound_path"] = path if upload is not None else ("" if clear_sound else current_sound)
                    config["fan_club_entry_min_level"] = int(fan_level)
                else:
                    if upload is not None:
                        user_sounds[user_key] = path
                    elif clear_sound:
                        user_sounds.pop(user_key, None)
                    config["member_entry_sound_by_user"] = user_sounds
                write_json(CONFIG_PATH, TTSSettings.from_mapping(config).to_mapping())
                st.success(tr("Entry sound saved.", "進場音效已儲存。"))
                st.rerun()
            except (OSError, ValueError) as error:
                st.error(str(error))


def request_worker_stop() -> None:
    write_json(STOP_PATH, {"requested_at_local": datetime.now().astimezone().isoformat(timespec="seconds")})


def _tail_log(path: Path, limit: int = 12) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.readlines()[-limit:]
    except OSError:
        return []


def render_tts(streamers: list[str], watcher_states: dict) -> None:
    st.title(tr("Live Chat TTS", "直播聊天室語音"))
    st.caption(tr(
        "Optional bilingual text-to-speech for chat from a Collector or selected benchmark session. Status refreshes every 2 seconds.",
        "可將 Collector 或所選 Benchmark session 的聊天轉成語音；狀態每 2 秒更新。",
    ))
    st.caption(
        tr(
            "Sticker messages are spoken as 'send a sticker'. Common emoji are converted to short spoken words, mixed Chinese and English use matching voices, and abbreviations such as IG, FB, YT, DM, and VIP are read as separate letters. The @ symbol is spoken as 'at'; the chat display stays unchanged.",
            "表情貼會朗讀為「送出表情貼」。IG、FB、YT、DM、VIP 等縮寫會逐字母念出；@ 會念成 at，畫面上的聊天文字不變。",
        )
    )
    st.info(
        tr(
            "This worker reads an existing local events.ndjson file. It does not open another TikTok connection, starts at the end of the file, and never sends messages to the chat.",
            "此 Worker 讀取現有的 events.ndjson，不會另開 TikTok 連線，從檔案尾端開始讀取，也不會傳送聊天室訊息。",
        )
    )
    st.warning(
        tr(
            "Audio plays through this computer's default sound output. To include it in LIVE Studio/OBS, capture desktop audio or route it through a virtual audio cable.",
            "語音會從這台電腦的預設音訊裝置播放。若要送進 LIVE Studio／OBS，請擷取桌面音訊或使用虛擬音源線。",
        )
    )

    choices = [name for name in streamers if name]
    if not choices:
        st.error(tr("No streamer is configured in the Watch list.", "Watch list 尚未設定直播主。"))
        return

    state = read_json(STATE_PATH, {})
    current_pid = state.get("pid") if isinstance(state, dict) else None
    running = pid_is_running(current_pid)
    current_username = str((state or {}).get("username") or "")
    selected = st.selectbox(
        tr("Streamer", "直播主"),
        options=choices,
        index=choices.index(current_username) if current_username in choices else 0,
        format_func=lambda value: f"@{value}",
        disabled=running,
        key="tts_streamer",
    )

    watcher_state = watcher_states.get(selected, {}) if isinstance(watcher_states, dict) else {}
    watcher_pid_value = watcher_pid()
    watcher_running = pid_is_running(watcher_pid_value)
    benchmark_paths = benchmark_sessions(selected)
    browser_paths = browser_sessions(selected)
    event_paths = browser_paths + benchmark_paths
    if browser_paths:
        source_options = [str(path.resolve()) for path in event_paths]
        source_labels = {
            str(path.resolve()): f"Browser Network ? {path.name}"
            for path in browser_paths
        }
        source_labels.update({
            str(path.resolve()): f"Benchmark ? {path.parent.name} ? {path.name}"
            for path in benchmark_paths
        })
        st.caption(tr(
            "Using a captured Browser Network session. No Watcher probe is needed.",
            "\u4f7f\u7528 Browser Network \u64f7\u53d6\u5834\u6b21\uff0c\u4e0d\u9700\u8981 Watcher \u63a2\u6e2c\u3002",
        ))
    else:
        source_options = ["watcher"] + [str(path.resolve()) for path in event_paths]
        source_labels = {"watcher": tr("Watcher-managed Collector", "? Watcher ??? Collector")}
        source_labels.update({
            str(path.resolve()): f"Benchmark ? {path.parent.name} ? {path.name}"
            for path in benchmark_paths
        })
        st.caption(
            f"{tr('Collector', 'Collector')}: {watcher_state.get('status', 'unknown')}"
            + (f" ? PID {watcher_state.get('collector_pid')}" if watcher_state.get("collector_pid") else "")
        )
    default_source = (
        str(event_paths[0].resolve())
        if browser_paths
        else (str(event_paths[0].resolve()) if not watcher_running and event_paths else "watcher")
    )
    selected_source = st.selectbox(
        tr("Event source", "\u4e8b\u4ef6\u4f86\u6e90"),
        options=source_options,
        index=source_options.index(default_source),
        format_func=source_labels.__getitem__,
        disabled=running,
        key="tts_event_source_id",
    )
    selected_session_path = None
    if selected_source != "watcher":
        selected_session_path = Path(selected_source)

    saved = read_json(CONFIG_PATH, {})
    settings = TTSSettings.from_mapping(saved)
    status_tab, speech_tab, gifts_tab, entry_tab = st.tabs([
        tr("Status and controls", "\u72c0\u614b\u8207\u63a7\u5236"),
        tr("Speech settings", "\u8a9e\u97f3\u8207\u804a\u5929\u5ba4"),
        tr("Gift names and sounds", "\u79ae\u7269\u540d\u7a31\u8207\u97f3\u6548"),
        tr("Entry sounds", "\u9032\u5834\u97f3\u6548"),
    ])

    with speech_tab:
        st.subheader(tr("Voice and moderation", "聲線與訊息過濾"))
        voice_cols = st.columns(2)
        zh_options = available_voices("zh-TW", settings.zh_voice)
        en_options = available_voices("en-US", settings.en_voice)
        zh_voice = voice_cols[0].selectbox(
            tr("Traditional Chinese voice", "繁體中文聲線"),
            options=zh_options,
            index=zh_options.index(settings.zh_voice),
            format_func=voice_label,
            key="tts_zh_voice",
            on_change=apply_voice_selection,
            args=("zh-TW", "tts_zh_voice"),
            help=tr("Changing this voice automatically plays a short Traditional Chinese preview.", "切換聲線後會自動播放一段繁體中文預覽。"),
        )
        en_voice = voice_cols[1].selectbox(
            tr("English voice", "英文聲線"),
            options=en_options,
            index=en_options.index(settings.en_voice),
            format_func=voice_label,
            key="tts_en_voice",
            on_change=apply_voice_selection,
            args=("en-US", "tts_en_voice"),
            help=tr("Changing this voice automatically plays a short English preview.", "切換聲線後會自動播放一段英文預覽。"),
        )
        preview_status = st.session_state.get("tts_voice_preview_status")
        if preview_status:
            st.caption(preview_status)

        with st.form("tts_settings_form"):
            setting_cols = st.columns(4)
            rate = setting_cols[0].number_input(
                tr("Speech rate (%)", "語速（%）"), min_value=-50, max_value=100,
                value=settings.rate_percent, step=5, disabled=running, key="tts_rate_percent",
            )
            volume = setting_cols[1].slider(
                tr("Playback volume", "播放音量"), min_value=0, max_value=100,
                value=settings.volume, disabled=running, key="tts_volume",
            )
            max_length = setting_cols[2].number_input(
                tr("Max characters", "每則最多字數"), min_value=1, max_value=200,
                value=settings.max_text_length, step=5, disabled=running, key="tts_max_text_length",
            )
            queue_size = setting_cols[3].number_input(
                tr("Queue size", "佇列容量"), min_value=1, max_value=5000,
                value=settings.queue_size, step=1, disabled=running, key="tts_queue_size",
            )
            guard_cols = st.columns(6)
            cooldown = guard_cols[0].number_input(
                tr("Per-user cooldown (seconds)", "每位使用者冷卻秒數"), min_value=0.0, max_value=300.0,
                value=float(settings.user_cooldown_seconds), step=0.5, disabled=running, key="tts_user_cooldown",
            )
            duplicate_window = guard_cols[1].number_input(
                tr("Duplicate filter window (seconds)", "重複訊息過濾秒數"), min_value=0.0, max_value=600.0,
                value=float(settings.duplicate_window_seconds), step=1.0, disabled=running, key="tts_duplicate_window",
            )
            chat_ttl = guard_cols[2].number_input(
                tr("Chat freshness limit (seconds)", "聊天新鮮度時限（秒）"), min_value=5.0, max_value=15.0,
                value=float(settings.chat_ttl_seconds), step=1.0, disabled=running,
                help=tr("Only queued Chat expires after this age. Captured Gifts stay queued until spoken or a backend failure is recorded.", "只有排隊中的聊天會在超過時限後過期；已捕獲禮物會保留至播出，或明確記錄語音服務失敗。"),
                key="tts_chat_ttl",
            )
            ignore_emoji = guard_cols[3].checkbox(
                tr("Ignore emoji", "略過 Emoji"), value=settings.ignore_emoji, disabled=running, key="tts_ignore_emoji",
            )
            request_gap = guard_cols[4].number_input(
                tr("Minimum TTS request gap (seconds)", "TTS 請求最小間隔（秒）"), min_value=0.1, max_value=30.0,
                value=float(settings.min_request_interval_seconds), step=0.5, disabled=running,
                help=tr("Spaces remote speech synthesis requests; transient failures retry the current line up to three times.", "拉開遠端語音合成請求間隔；暫時性錯誤會重試目前句子，最多三次。"),
                key="tts_request_gap",
            )
            speak_common_emoji = guard_cols[5].checkbox(
                tr("Speak common emoji", "朗讀常見 Emoji"),
                value=settings.speak_common_emoji,
                disabled=running,
                help=tr(
                    "Common emoji are replaced with short words before speech; unknown emoji follow the Ignore emoji setting.",
                    "常見 Emoji 會先轉成簡短詞語；未收錄的 Emoji 依照略過設定處理。",
                ),
                key="tts_speak_common_emoji",
            )
            blacklist = st.text_area(
                tr("Blocked words or phrases (one per line)", "封鎖詞句（每行一項）"),
                value="\n".join(settings.blacklist_terms),
                disabled=running,
                help=tr("A matching message is skipped before it reaches the speech service.", "符合封鎖詞句的訊息不會送到語音服務。"),
                key="tts_blacklist",
            )
            chat_filter = st.selectbox(
                tr("Who receives Chat TTS", "哪些人的聊天室訊息播放 TTS"),
                options=("all", "fan_club_only", "selected_users"),
                index=("all", "fan_club_only", "selected_users").index(settings.chat_tts_filter),
                format_func=lambda value: {
                    "all": tr("Everyone (default)", "所有人（預設）"),
                    "fan_club_only": tr("Fan club badge members only", "只有粉絲燈牌成員"),
                    "selected_users": tr("Selected users only", "只有指定使用者"),
                }[value],
                disabled=running,
                key="tts_chat_filter",
            )
            known_users, known_user_labels = discovered_chat_users(selected)
            user_options = sorted(
                set(known_users) | set(settings.chat_tts_user_allowlist)
            )
            selected_chat_users = st.multiselect(
                tr("Choose recently seen users", "選擇近期出現的使用者"),
                options=user_options,
                default=[user for user in settings.chat_tts_user_allowlist if user in user_options],
                format_func=lambda value: known_user_labels.get(value, f"@{value}"),
                disabled=running or chat_filter != "selected_users",
                key="tts_chat_user_selection",
            )
            manual_chat_users = st.text_area(
                tr("Additional account IDs (one per line)", "其他帳號 ID（每行一個）"),
                disabled=running or chat_filter != "selected_users",
                help=tr("Accepts TikTok unique IDs or numeric user IDs. Gifts continue to be announced for everyone.", "可填 TikTok 帳號 ID 或數字 user ID；禮物播報仍不受此聊天篩選影響。"),
                key="tts_chat_users_manual",
            )
            saved_settings = st.form_submit_button(tr("Save settings", "儲存設定"), disabled=running)
            form_values = {
                "zh_voice": st.session_state.get("tts_zh_voice", zh_voice),
                "en_voice": st.session_state.get("tts_en_voice", en_voice),
                "rate_percent": int(rate),
                "volume": int(volume),
                "max_text_length": int(max_length),
                "queue_size": int(queue_size),
                "chat_ttl_seconds": float(chat_ttl),
                "user_cooldown_seconds": float(cooldown),
                "duplicate_window_seconds": float(duplicate_window),
                "min_request_interval_seconds": float(request_gap),
                "ignore_emoji": bool(ignore_emoji),
                "speak_common_emoji": bool(speak_common_emoji),
                "gift_followup_audio_path": settings.gift_followup_audio_path,
                "gift_sound_by_id": dict(settings.gift_sound_by_id),
                "gift_name_by_id": dict(settings.gift_name_by_id),
                "chat_tts_filter": chat_filter,
                "chat_tts_user_allowlist": [*selected_chat_users, *manual_chat_users.splitlines()],
                "member_entry_sound_path": settings.member_entry_sound_path,
                "member_entry_sound_by_user": dict(settings.member_entry_sound_by_user),
                "fan_club_entry_sound_path": settings.fan_club_entry_sound_path,
                "fan_club_entry_min_level": settings.fan_club_entry_min_level,
                "blacklist_terms": blacklist.splitlines(),
            }
            if saved_settings:
                normalized = TTSSettings.from_mapping(form_values)
                write_json(CONFIG_PATH, normalized.to_mapping())
                st.success(tr("TTS settings saved locally.", "TTS 設定已儲存至本機。"))

    with speech_tab:
        st.caption(
            tr(
                "Captured Chat and Gift announcements stay in timestamp order. Chat expires after the selected freshness limit; captured Gifts do not expire. Mixed Chinese and English comments are synthesized in language-matched segments.",
                "已捕獲的聊天與禮物依事件時間排序；聊天超過新鮮度時限會過期，禮物不會因等待時間而丟棄。中英文訊息各自選擇一種聲線。",
            )
        )

    with gifts_tab:
        render_gift_configuration(selected, settings, running)

    with entry_tab:
        render_member_sound_configuration(selected, settings, running)



    with status_tab:
        source_available = watcher_running or selected_session_path is not None
        metrics = (state or {}).get("metrics", {}) if isinstance(state, dict) else {}
        st.subheader(tr("Delivery health", "\u64ad\u5831\u5065\u5eb7\u72c0\u614b"))
        st.caption(tr(
            "Speech uses Edge TTS online. After an Edge synthesis failure, the worker tries an installed Windows offline voice for Chat and Gifts.",
            "\u8a9e\u97f3\u9810\u8a2d\u4f7f\u7528\u7dda\u4e0a Edge TTS\u3002\u5408\u6210\u5931\u6557\u6642\uff0cWorker \u6703\u5148\u5617\u8a66\u5df2\u5b89\u88dd\u7684 Windows \u96e2\u7dda\u8a9e\u97f3\u64ad\u5831\u804a\u5929\u8207\u79ae\u7269\u3002",
        ))
        status_cols = st.columns(3)
        gift_status_cols = st.columns(5)
        status_cols[0].metric(tr("Worker", "\u64ad\u5831\u7a0b\u5e8f"), state.get("status", "stopped"))
        status_cols[1].metric(tr("Queue", "\u4f47\u5217"), f"{metrics.get('queue_depth', 0)} / {metrics.get('queue_capacity', settings.queue_size)}")
        status_cols[2].metric(tr("Oldest item", "\u6700\u820a\u9805\u76ee"), f"{(metrics.get('oldest_queue_age_seconds') or 0):.1f} s")
        gift_status_cols[0].metric(tr("Gift failures", "\u79ae\u7269\u64ad\u5831\u5931\u6557"), metrics.get("gift_delivery_failures", 0))
        gift_status_cols[1].metric(tr("Failure alerts", "\u5931\u6557\u63d0\u793a\u97f3"), metrics.get("gift_failure_alerts_played", 0))
        gift_status_cols[2].metric(tr("Alert errors", "\u63d0\u793a\u97f3\u932f\u8aa4"), metrics.get("gift_failure_alert_errors", 0))
        gift_status_cols[3].metric(tr("Pending Gift replays", "\u5f85\u91cd\u64ad\u79ae\u7269"), metrics.get("pending_gift_replay_count", 0))
        gift_status_cols[4].metric(tr("Replay unavailable", "\u7121\u6cd5\u81ea\u52d5\u91cd\u64ad"), metrics.get("gift_replay_unavailable", 0))
        load_cols = st.columns(6)
        load_cols[0].metric(
            tr("Queue peak", "\u4f47\u5217\u5c16\u5cf0"),
            f"{metrics.get('max_queue_depth', 0)} / {metrics.get('queue_capacity', settings.queue_size)}",
        )
        load_cols[1].metric(
            tr("Full queue waits", "\u4f47\u5217\u6eff\u6642\u7b49\u5f85"),
            metrics.get("queue_full_waits", 0),
        )
        load_cols[2].metric(
            tr("Offline fallback audio", "\u96e2\u7dda\u5099\u63f4\u97f3\u8a0a\u5408\u6210"),
            metrics.get("gift_offline_fallback_succeeded", 0),
        )
        load_cols[3].metric(
            tr("Offline fallback errors", "\u96e2\u7dda\u5099\u63f4\u5931\u6557"),
            metrics.get("gift_offline_fallback_failures", 0),
        )
        load_cols[4].metric(
            tr("Chat offline fallback", "\u804a\u5929\u96e2\u7dda\u5099\u63f4"),
            metrics.get("chat_offline_fallback_succeeded", 0),
        )
        load_cols[5].metric(
            tr("Chat fallback errors", "\u804a\u5929\u5099\u63f4\u5931\u6557"),
            metrics.get("chat_offline_fallback_failures", 0),
        )
        if metrics.get("gift_delivery_failures", 0):
            if "gift_failure_alerts_played" in metrics:
                st.error(tr(
                    "A Gift was not spoken after retries. A local alert was attempted; Gifts with event IDs are saved for replay when TTS starts again.",
                    "\u79ae\u7269\u591a\u6b21\u91cd\u8a66\u5f8c\u4ecd\u672a\u80fd\u8a9e\u97f3\u64ad\u5831\u3002\u7cfb\u7d71\u5df2\u5617\u8a66\u64ad\u653e\u672c\u6a5f\u63d0\u793a\u97f3\uff1b\u6709\u4e8b\u4ef6 ID \u7684\u79ae\u7269\u5df2\u4fdd\u5b58\uff0c\u4f9b\u4e0b\u6b21\u555f\u52d5 TTS \u6642\u91cd\u64ad\u3002",
                ))
                if metrics.get("gift_replay_unavailable", 0):
                    st.warning(tr(
                        "Some failed Gifts have no event ID and cannot be replayed automatically.",
                        "\u90e8\u5206\u5931\u6557\u79ae\u7269\u6c92\u6709\u4e8b\u4ef6 ID\uff0c\u7121\u6cd5\u81ea\u52d5\u91cd\u64ad\u3002",
                    ))
            else:
                st.error(tr(
                    "A Gift failed in this older running worker. Its fallback alert and immediate replay saving will be active after a safe TTS restart.",
                    "\u76ee\u524d\u57f7\u884c\u7684\u820a\u7248 Worker \u767c\u751f\u79ae\u7269\u64ad\u5831\u5931\u6557\u3002\u5f85\u5b89\u5168\u91cd\u65b0\u555f\u52d5 TTS \u5f8c\uff0c\u624d\u6703\u555f\u7528\u5931\u6557\u63d0\u793a\u97f3\u8207\u5373\u6642\u4fdd\u5b58\u91cd\u64ad\u3002",
                ))
        elif metrics.get("gift_retry_queued", 0):
            st.warning(tr(
                "At least one Gift is waiting for its delayed retry.",
                "\u81f3\u5c11\u6709\u4e00\u7b46\u79ae\u7269\u6b63\u5728\u7b49\u5f85\u5ef6\u9072\u91cd\u8a66\u3002",
            ))
        elif running and "gift_failure_alerts_played" not in metrics:
            st.info(tr(
                "This worker predates the new Gift failure fallback. It will be available after a safe restart.",
                "\u76ee\u524d\u7684 Worker \u662f\u820a\u7248\uff1b\u65b0\u7684\u79ae\u7269\u5931\u6557\u4fdd\u5e95\u6703\u5728\u5b89\u5168\u91cd\u65b0\u555f\u52d5\u5f8c\u751f\u6548\u3002",
            ))
        if metrics.get("last_error"):
            st.caption(f"{tr('Last synthesis or playback error', '\u6700\u8fd1\u7684\u5408\u6210\u6216\u64ad\u653e\u932f\u8aa4')}: {metrics['last_error']}")

        controls = st.columns(2)
        if controls[0].button(
            tr("Start TTS worker", "啟動 TTS Worker"),
            type="primary",
            disabled=running or not source_available,
            use_container_width=True,
        ):
            normalized = TTSSettings.from_mapping(form_values)
            ok, message = start_worker(selected, normalized, selected_session_path)
            (st.success if ok else st.error)(message)
            st.rerun()

        stop_pending = STOP_PATH.exists()
        if controls[1].button(
            tr("Stop TTS worker", "停止 TTS Worker"),
            disabled=not running or stop_pending,
            use_container_width=True,
        ):
            request_worker_stop()
            st.warning(tr("Graceful stop requested. Any current speech will be stopped; collection continues.", "已要求平順停止。當前語音會停止，但資料收集會繼續。"))
            st.rerun()

        if not source_available:
            st.info(tr("Start the Watcher, or select an available benchmark session as the event source.", "請啟動 Watcher，或選擇可用的 Benchmark session 作為事件來源。"))

        if running:
            st.success(f"{tr('TTS worker running for', 'TTS Worker 執行中：')} @{current_username} · PID {current_pid} · {tr('status', '狀態')}：{state.get('status', 'starting')}")
        elif isinstance(state, dict) and state.get("status") == "error":
            st.error(f"{tr('Last TTS worker failed', '上次 TTS Worker 失敗')}：{state.get('last_error') or tr('see worker log', '請查看 Worker 紀錄')}")
        elif isinstance(state, dict) and state.get("status") == "stopped":
            st.caption(f"{tr('Last worker stopped at', '上次 Worker 停止時間')} {state.get('stopped_at_local', 'unknown')}.")

        metric_cols = st.columns(7)
        metric_cols[0].metric(tr("Chats seen", "收到聊天"), metrics.get("chat_events_seen", 0))
        metric_cols[1].metric(tr("Spoken", "已朗讀"), metrics.get("spoken", 0))
        metric_cols[2].metric(tr("Queued", "佇列中"), f"{metrics.get('queue_depth', 0)} / {metrics.get('queue_capacity', settings.queue_size)}")
        oldest_age = metrics.get("oldest_queue_age_seconds") or 0
        metric_cols[3].metric(tr("Oldest queue age", "最久等待"), f"{oldest_age:.1f} s")
        p50 = metrics.get("p50_event_to_playback_ms")
        metric_cols[4].metric(tr("Event-to-playback p50", "事件至播報 p50"), f"{p50} ms" if p50 is not None else "N/A")
        p95 = metrics.get("p95_event_to_playback_ms")
        metric_cols[5].metric(tr("Event-to-playback p95", "事件至播報 p95"), f"{p95} ms" if p95 is not None else "N/A")
        metric_cols[6].metric(tr("Skipped", "略過"), sum((metrics.get("skipped") or {}).values()))
        latency_cols = st.columns(3)
        latency_cols[0].metric(
            tr("Queue wait p95", "佇列等待 p95"),
            f"{metrics['p95_queue_wait_ms']} ms" if metrics.get("p95_queue_wait_ms") is not None else "N/A",
        )
        latency_cols[1].metric(
            tr("Synthesis p95", "語音合成 p95"),
            f"{metrics['p95_synthesis_ms']} ms" if metrics.get("p95_synthesis_ms") is not None else "N/A",
        )
        latency_cols[2].metric(
            tr("Playback p95", "音訊播放 p95"),
            f"{metrics['p95_audio_playback_ms']} ms" if metrics.get("p95_audio_playback_ms") is not None else "N/A",
        )
        gift_cols = st.columns(5)
        gift_cols[0].metric(tr("Gift events seen", "收到禮物事件"), metrics.get("gift_events_seen", 0))
        gift_cols[1].metric(tr("Gift announcements queued", "禮物已排入佇列"), metrics.get("gift_queued", 0))
        gift_cols[2].metric(tr("Gift announcements spoken", "已朗讀禮物"), metrics.get("gift_spoken", 0))
        gift_cols[3].metric(tr("Gift delivery failures", "禮物播報失敗"), metrics.get("gift_delivery_failures", 0))
        gift_cols[4].metric(tr("Synthesis / playback errors", "合成／播放錯誤"), metrics.get("synthesis_or_playback_errors", 0))
        st.caption(
            tr("Gift follow-up sounds played", "Gift 後續音效已播放")
            + f": {metrics.get('gift_followup_sounds_played', 0)}"
            + " · "
            + tr("sound errors", "音效錯誤")
            + f": {metrics.get('gift_followup_sound_errors', 0)}"
        )
        st.caption(
            tr("Chat filtered by sender rules", "依使用者規則略過的聊天")
            + f": {metrics.get('chat_filtered_by_settings', 0)} · "
            + tr("Entry sounds played", "進場音效已播放")
            + f": {metrics.get('member_entry_sounds_played', 0)}"
        )
        if metrics.get("skipped"):
            with st.expander(tr("Skipped message counts", "略過訊息統計")):
                st.json(metrics["skipped"])
        if metrics.get("last_error"):
            st.warning(f"{tr('Last TTS/audio error', '最近一次 TTS／音訊錯誤')}：{metrics['last_error']}")
        retry_after = metrics.get("tts_retry_after_local")
        if retry_after:
            try:
                retry_time = datetime.fromisoformat(str(retry_after))
                now = datetime.now(retry_time.tzinfo) if retry_time.tzinfo else datetime.now()
                if retry_time > now:
                    st.info(f"{tr('TTS service backoff active until', 'TTS 服務暫停重試至')} {retry_after} ({tr('Taiwan time', '台灣時間')})。")
                else:
                    st.caption(
                        f"{tr('Last TTS backoff ended at', '上次 TTS 暫停重試已於')} {retry_after}；{tr('the worker will retry on the next accepted chat.', 'Worker 會在下一則通過篩選的聊天時重試。')}"
                    )
            except ValueError:
                    st.info(f"{tr('Last TTS service retry deadline', 'TTS 服務下次重試時間')}：{retry_after} ({tr('Taiwan time', '台灣時間')})。")
        if LOG_PATH.exists():
            with st.expander(tr("TTS worker log", "TTS Worker 紀錄")):
                st.code("".join(_tail_log(LOG_PATH)).strip() or "(empty)")
        gift_log_lines = [
            line for line in _tail_log(LOG_PATH, 100) if "[tts-gift]" in line
        ]
        if gift_log_lines:
            with st.expander(tr("Recent Gift delivery stages", "\u6700\u8fd1\u79ae\u7269\u64ad\u5831\u968e\u6bb5")):
                st.code("".join(gift_log_lines).strip())
