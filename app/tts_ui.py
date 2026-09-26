"""Streamlit controls for the optional local LIVE chat TTS worker."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
import asyncio
from datetime import datetime
from pathlib import Path

import streamlit as st

from src.tts.pipeline import TTSSettings
from src.tts.audio import EdgeTTSBackend, PygameAudioPlayer
from app.i18n import tr


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data"
TTS_ROOT = DATA_ROOT / "tts"
RAW_ROOT = DATA_ROOT / "raw"
BENCHMARK_ROOT = DATA_ROOT / "v2_provider_benchmark"
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
    command = [sys.executable, "-m", "src.tts_worker", "--username", username]
    if session_dir is not None:
        command.extend(["--session-dir", str(session_dir.resolve())])
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
    source = (
        tr(f"benchmark session {session_dir.name}", f"Benchmark 場次 {session_dir.name}")
        if session_dir
        else tr("Watcher collector", "Watcher Collector")
    )
    return True, tr(
        f"TTS worker started for @{username} from {source} (PID {process.pid}).",
        f"已為 @{username} 啟動 TTS Worker，來源：{source}（PID {process.pid}）。",
    )


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
            "Sticker messages are spoken as 'send a sticker'. Common abbreviations such as IG, FB, YT, DM, and VIP are read as separate letters. The @ symbol is spoken as 'at'; the chat display stays unchanged.",
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
    st.caption(
        f"{tr('Collector', '收集器')}：{watcher_state.get('status', 'unknown')}"
        + (f" · PID {watcher_state.get('collector_pid')}" if watcher_state.get("collector_pid") else "")
    )

    benchmark_paths = benchmark_sessions(selected)
    source_options = ["watcher"] + [str(path.resolve()) for path in benchmark_paths]
    source_labels = {"watcher": tr("Watcher-managed Collector", "由 Watcher 管理的 Collector")}
    source_labels.update({
        str(path.resolve()): f"Benchmark · {path.parent.name} · {path.name}"
        for path in benchmark_paths
    })
    selected_source = st.selectbox(
        tr("Event source", "事件來源"),
        options=source_options,
        index=1 if not watcher_running and benchmark_paths else 0,
        format_func=source_labels.__getitem__,
        disabled=running,
        key="tts_event_source_id",
    )
    benchmark_path = None
    if selected_source != "watcher":
        benchmark_path = Path(selected_source)

    saved = read_json(CONFIG_PATH, {})
    settings = TTSSettings.from_mapping(saved)
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
        guard_cols = st.columns(5)
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
        blacklist = st.text_area(
            tr("Blocked words or phrases (one per line)", "封鎖詞句（每行一項）"),
            value="\n".join(settings.blacklist_terms),
            disabled=running,
            help=tr("A matching message is skipped before it reaches the speech service.", "符合封鎖詞句的訊息不會送到語音服務。"),
            key="tts_blacklist",
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
            "blacklist_terms": blacklist.splitlines(),
        }
        if saved_settings:
            normalized = TTSSettings.from_mapping(form_values)
            write_json(CONFIG_PATH, normalized.to_mapping())
            st.success(tr("TTS settings saved locally.", "TTS 設定已儲存至本機。"))

    st.caption(
        tr(
            "Captured Chat and Gift announcements stay in timestamp order. Chat expires after the selected freshness limit; captured Gifts do not expire. Chinese/English routing selects one voice for each whole message.",
            "已捕獲的聊天與禮物依事件時間排序；聊天超過新鮮度時限會過期，禮物不會因等待時間而丟棄。中英文訊息各自選擇一種聲線。",
        )
    )

    source_available = watcher_running or benchmark_path is not None
    controls = st.columns(2)
    if controls[0].button(
        tr("Start TTS worker", "啟動 TTS Worker"),
        type="primary",
        disabled=running or not source_available,
        use_container_width=True,
    ):
        normalized = TTSSettings.from_mapping(form_values)
        ok, message = start_worker(selected, normalized, benchmark_path)
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

    metrics = (state or {}).get("metrics", {}) if isinstance(state, dict) else {}
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
    gift_cols = st.columns(5)
    gift_cols[0].metric(tr("Gift events seen", "收到禮物事件"), metrics.get("gift_events_seen", 0))
    gift_cols[1].metric(tr("Gift announcements queued", "禮物已排入佇列"), metrics.get("gift_queued", 0))
    gift_cols[2].metric(tr("Gift announcements spoken", "已朗讀禮物"), metrics.get("gift_spoken", 0))
    gift_cols[3].metric(tr("Gift delivery failures", "禮物播報失敗"), metrics.get("gift_delivery_failures", 0))
    gift_cols[4].metric(tr("Synthesis / playback errors", "合成／播放錯誤"), metrics.get("synthesis_or_playback_errors", 0))
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
