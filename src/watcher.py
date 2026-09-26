import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
try:
    from .quota_policy import classify_limit, pause_until, paused
    from .rotating_log import append_rotating_text
except ImportError:
    from quota_policy import classify_limit, pause_until, paused
    from rotating_log import append_rotating_text


WATCHER_VERSION = "0.2"
TAIPEI_TZ = timezone(timedelta(hours=8), name="Asia/Taipei")


def _write_health_report(session_dir):
    try:
        from .health_monitor import write_health_report
    except ImportError:
        from health_monitor import write_health_report
    return write_health_report(session_dir)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def taipei_now():
    return datetime.now(TAIPEI_TZ).isoformat()


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))

    defaults = {
        "poll_seconds": 60,
        "probe_timeout_seconds": 15,
        "offline_confirmations": 3,
        "collector_script": "src/collector.py",
        "output_root": "data/raw",
        "restart_collector_on_crash": True,
        "restart_delay_seconds": 10,
        "start_collector_on_probe_error": True,
        "collector_probe_error_cooldown_seconds": 300,
        "health_check_seconds": 60,
        "probe_min_interval_seconds": 120,
        "log_max_bytes": 5 * 1024 * 1024,
        "log_backup_count": 3,
        "capture_raw_events": False,
        "streamers": [],
    }

    for key, value in defaults.items():
        config.setdefault(key, value)

    try:
        config["log_max_bytes"] = max(0, int(config["log_max_bytes"]))
    except (TypeError, ValueError):
        config["log_max_bytes"] = defaults["log_max_bytes"]
    try:
        config["log_backup_count"] = max(0, int(config["log_backup_count"]))
    except (TypeError, ValueError):
        config["log_backup_count"] = defaults["log_backup_count"]
    config["capture_raw_events"] = config["capture_raw_events"] is True

    normalized = []
    for item in config["streamers"]:
        if isinstance(item, str):
            item = {"username": item, "enabled": True}

        username = str(item.get("username", "")).lstrip("@").strip()
        if not username:
            continue

        normalized.append({
            "username": username,
            "enabled": bool(item.get("enabled", True)),
            "label": item.get("label") or username,
        })

    config["streamers"] = normalized
    return config


async def probe_live(username, timeout_seconds):
    """
    Lightweight LIVE probe.

    A successful TikTokLive WebSocket connection with a room ID means LIVE.
    Failure / timeout is reported as not-confirmed rather than authoritative
    TikTok 'offline', because this is an unofficial endpoint.
    """
    result = {
        "username": username,
        "confirmed_live": False,
        "connected": False,
        "room_id": None,
        "ws_host": None,
        "cluster_region": None,
        "error": None,
        "checked_at_utc": utc_now(),
    }

    try:
        from TikTokLive import TikTokLiveClient
        from TikTokLive.client.errors import UserOfflineError
        from TikTokLive.events import ConnectEvent
    except ImportError as exc:
        result["error"] = f"TikTokLive unavailable: {exc}"
        return result

    client = TikTokLiveClient(unique_id=f"@{username}")
    connected = asyncio.Event()

    @client.on(ConnectEvent)
    async def on_connected(event):
        result["connected"] = True
        room_id = getattr(event, "room_id", None) or client.room_id
        if room_id is not None:
            result["room_id"] = str(room_id)
        result["confirmed_live"] = bool(result["room_id"])
        connected.set()

    try:
        probe_started = time.monotonic()
        await asyncio.wait_for(
            client.start(fetch_live_check=True), timeout=timeout_seconds
        )
        remaining = max(0.0, timeout_seconds - (time.monotonic() - probe_started))
        await asyncio.wait_for(connected.wait(), timeout=remaining)
    except asyncio.TimeoutError:
        result["error"] = "probe_timeout"
    except UserOfflineError:
        result["error"] = "User is not currently live"
    except Exception as exc:
        result["error"] = (
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        try:
            await asyncio.wait_for(
                client.disconnect(close_client=True), timeout=3
            )
        except Exception:
            pass

    if result["connected"] and not result["confirmed_live"] and not result["error"]:
        result["error"] = "probe_no_room_info"

    result["checked_at_utc"] = utc_now()
    return result


async def probe_all(streamers, timeout_seconds):
    tasks = [
        probe_live(item["username"], timeout_seconds)
        for item in streamers
        if item["enabled"]
    ]
    if not tasks:
        return []
    return await asyncio.gather(*tasks)


class Watcher:
    def __init__(self, config_path, *, probe_only=False):
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
        self.probe_only = bool(probe_only)

        self.base_dir = self.config_path.parent.resolve()
        self.log_dir = self.base_dir / "data" / "watcher"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.log_path = self.log_dir / "watcher.log"
        self.state_path = self.log_dir / "watcher_state.json"
        try:
            previous_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous_state = {}
        self.quota_pause_until_utc = previous_state.get("quota_pause_until_utc")
        self.next_probe_at_utc = previous_state.get("next_probe_at_utc")
        self.probe_cursor = int(previous_state.get("probe_cursor") or 0)
        self.pid_path = self.log_dir / "watcher.pid"
        self.stop_path = self.log_dir / "watcher.stop"

        self.states = {
            item["username"]: self._initial_state(item)
            for item in self.config["streamers"]
        }

        self.processes = {}
        self.fallback_last_started = {}
        self.last_health_check = 0.0
        self.stalled_collectors = set()
        self.running = True

    @staticmethod
    def _initial_state(item):
        return {
            "label": item["label"],
            "enabled": item["enabled"],
            "status": "waiting",
            "last_probe_at_utc": None,
            "last_confirmed_live_at_utc": None,
            "last_room_id": None,
            "consecutive_misses": 0,
            "collector_pid": None,
            "collector_started_at_utc": None,
            "collector_restart_count": 0,
            "last_probe_error": None,
        }

    def sync_config(self):
        try:
            latest = load_config(self.config_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.log(f"[WARN] watchlist reload failed: {exc}")
            return

        self.config = latest
        configured = {
            item["username"]: item for item in latest["streamers"]
        }
        for username, item in configured.items():
            if username not in self.states:
                self.states[username] = self._initial_state(item)
                self.log(f"[WATCH] added @{username}")
            else:
                self.states[username]["label"] = item["label"]
                self.states[username]["enabled"] = item["enabled"]

        for username, state in self.states.items():
            if username not in configured:
                state["enabled"] = False
            if state["enabled"]:
                continue
            process = self.processes.get(username)
            if process is not None and process.poll() is None:
                self.stop_collector(username, reason="watchlist_disabled")

    def acquire_pid(self):
        current_pid = os.getpid()
        try:
            existing_pid = int(self.pid_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, ValueError, OSError):
            existing_pid = None

        if existing_pid and existing_pid != current_pid:
            try:
                os.kill(existing_pid, 0)
            except OSError:
                pass
            else:
                raise SystemExit(
                    f"Watcher already running (PID {existing_pid})"
                )

        self.pid_path.write_text(str(current_pid), encoding="ascii")

    def release_pid(self):
        try:
            current_pid = int(self.pid_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, ValueError, OSError):
            return
        if current_pid == os.getpid():
            self.pid_path.unlink(missing_ok=True)

    def log(self, message):
        # Human-facing watcher logs use Taiwan time; state fields retain UTC.
        line = f"{taipei_now()} {message}"
        print(line, flush=True)
        append_rotating_text(
            self.log_path,
            line + "\n",
            max_bytes=self.config.get("log_max_bytes", 5 * 1024 * 1024),
            backup_count=self.config.get("log_backup_count", 3),
        )

    def save_state(self):
        payload = {
            "watcher_version": WATCHER_VERSION,
            "updated_at_utc": utc_now(),
            "config_path": str(self.config_path),
            "streamers": self.states,
            "quota_pause_until_utc": self.quota_pause_until_utc,
            "next_probe_at_utc": self.next_probe_at_utc,
            "probe_cursor": self.probe_cursor,
        }

        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_path)

    def note_quota(self, kind, source):
        previous = self.quota_pause_until_utc
        self.quota_pause_until_utc = pause_until(previous, kind)
        if self.quota_pause_until_utc != previous:
            local = datetime.fromisoformat(self.quota_pause_until_utc).astimezone(TAIPEI_TZ)
            self.log(f"[QUOTA] {kind} from {source}; new probes/collectors paused until {local.isoformat()}")

    def select_probe_target(self, enabled, now=None):
        now = now or datetime.now(timezone.utc)
        if paused(self.quota_pause_until_utc, now) or paused(self.next_probe_at_utc, now):
            return None
        eligible = [item for item in enabled if not (
            self.processes.get(item["username"])
            and self.processes[item["username"]].poll() is None
        )]
        if not eligible:
            return None
        item = eligible[self.probe_cursor % len(eligible)]
        self.probe_cursor += 1
        interval = max(60, int(self.config.get("probe_min_interval_seconds", 120)))
        self.next_probe_at_utc = (now + timedelta(seconds=interval)).isoformat()
        return item

    def collector_command(self, username):
        collector_script = Path(
            self.config["collector_script"]
        )
        if not collector_script.is_absolute():
            collector_script = (
                self.base_dir / collector_script
            ).resolve()

        output_root = Path(self.config["output_root"])
        if not output_root.is_absolute():
            output_root = (
                self.base_dir / output_root
            ).resolve()

        command = [
            sys.executable,
            str(collector_script),
            username,
            "--output-root",
            str(output_root),
            "--offline-confirmations",
            str(self.config.get("offline_confirmations", 3)),
            "--log-max-bytes",
            str(self.config.get("log_max_bytes", 5 * 1024 * 1024)),
            "--log-backups",
            str(self.config.get("log_backup_count", 3)),
        ]
        if self.config.get("capture_raw_events"):
            command.append("--capture-raw")
        return command

    def start_collector(self, username, room_id=None):
        if paused(self.quota_pause_until_utc):
            return
        existing = self.processes.get(username)
        if existing and existing.poll() is None:
            return

        cmd = self.collector_command(username)

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        process = subprocess.Popen(
            cmd,
            cwd=str(self.base_dir),
            creationflags=creationflags,
        )

        self.processes[username] = process

        state = self.states[username]
        state["status"] = "collecting"
        state["collector_pid"] = process.pid
        state["collector_started_at_utc"] = utc_now()
        if room_id:
            state["last_room_id"] = room_id

        self.log(
            f"[START] @{username} "
            f"room={room_id or 'unknown'} "
            f"pid={process.pid}"
        )

    def should_start_probe_error_fallback(self, username, error):
        if self.probe_only:
            return False
        if not self.config.get("start_collector_on_probe_error", True):
            return False
        text = str(error or "").lower()
        if paused(self.quota_pause_until_utc) or classify_limit(text):
            return False
        if self.states[username]["consecutive_misses"] < 2:
            return False
        if not text or text == "probe_no_room_info":
            return False
        # This is the only authoritative negative returned by the SDK. A
        # timeout, 429, or transport error must not prevent collection.
        if "is not currently live" in text:
            return False
        now = time.monotonic()
        cooldown = float(
            self.config.get("collector_probe_error_cooldown_seconds", 300)
        )
        previous = self.fallback_last_started.get(username)
        if previous is not None and now - previous < cooldown:
            return False
        self.fallback_last_started[username] = now
        return True

    def stop_collector(self, username, reason):
        process = self.processes.get(username)
        if process is None:
            return

        if process.poll() is not None:
            self.processes.pop(username, None)
            return

        self.log(
            f"[STOP] @{username} pid={process.pid} "
            f"reason={reason}"
        )

        graceful = False

        if os.name == "nt":
            # collector_v0_3 can finalize its session when CTRL_BREAK reaches
            # the Python process. CREATE_NEW_PROCESS_GROUP above makes this
            # possible without interrupting the watcher.
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                graceful = True
            except Exception as exc:
                self.log(
                    f"[WARN] CTRL_BREAK failed for @{username}: {exc}"
                )
        else:
            try:
                process.send_signal(signal.SIGINT)
                graceful = True
            except Exception as exc:
                self.log(
                    f"[WARN] SIGINT failed for @{username}: {exc}"
                )

        if graceful:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.log(
                    f"[WARN] graceful stop timeout @{username}; terminating"
                )
                process.terminate()
        else:
            process.terminate()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

        self.processes.pop(username, None)

        state = self.states[username]
        state["collector_pid"] = None
        state["collector_started_at_utc"] = None
        state["status"] = "waiting"

    def output_root(self):
        output_root = Path(self.config["output_root"])
        if not output_root.is_absolute():
            output_root = (self.base_dir / output_root).resolve()
        return output_root

    def latest_finished_source(self, username):
        root = self.output_root()
        if not root.exists():
            return None
        candidates = []
        for path in root.iterdir():
            if not path.is_dir() or not re.match(r"^\d{8}_\d{6}_", path.name):
                continue
            meta_path = path / "session.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if str(meta.get("username") or "").lstrip("@") != username:
                continue
            if meta.get("status") not in {"offline_confirmed", "live_end"}:
                continue
            candidates.append((str(meta.get("collector_ended_at_utc") or ""), path))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def aggregate_daily(self, username):
        source = self.latest_finished_source(username)
        if source is None:
            self.log(f"[AGGREGATE] @{username} no finalized source session found")
            return
        date = source.name[:8]
        raw_root = self.output_root()
        merge_script = (self.base_dir / "src" / "merge_daily_sessions.py").resolve()
        command = [
            sys.executable, str(merge_script),
            "--raw-root", str(raw_root),
            "--date", date,
            "--username", username,
            "--refresh", "--archive",
        ]
        result = subprocess.run(
            command, cwd=str(self.base_dir), capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            self.log(f"[AGGREGATE] @{username} failed: {result.stderr.strip() or result.stdout.strip()}")
            return

        target = raw_root / f"{date}_{username}"
        events_path = target / "events.ndjson"
        has_timestamped_event = False
        if events_path.exists() and events_path.stat().st_size > 0:
            with events_path.open("r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row.get("timestamp_ms"), (int, float)):
                        has_timestamped_event = True
                        break

        if events_path.exists() and events_path.stat().st_size > 0:
            analyzer = (self.base_dir / "src" / "analyzer.py").resolve()
            analyzed = subprocess.run(
                [sys.executable, str(analyzer), str(target), "--window", "60"],
                cwd=str(self.base_dir), capture_output=True, text=True, check=False
            )
            if analyzed.returncode != 0:
                self.log(f"[AGGREGATE] @{username} analyzer.py failed: {analyzed.stderr.strip()}")
                return
            if has_timestamped_event:
                plotter = (self.base_dir / "src" / "plot_session.py").resolve()
                plotted = subprocess.run(
                    [sys.executable, str(plotter), str(target), "--window", "60"],
                    cwd=str(self.base_dir), capture_output=True, text=True, check=False
                )
                if plotted.returncode != 0:
                    self.log(f"[AGGREGATE] @{username} plot_session.py failed: {plotted.stderr.strip()}")
                    return
            else:
                self.log(f"[AGGREGATE] @{username} no timestamped events; summary written, plots skipped")
        try:
            _write_health_report(target)
        except (ImportError, OSError) as exc:
            self.log(f"[HEALTH] @{username} report failed: {exc}")
        self.log(f"[AGGREGATE] @{username} daily={target.name} sources refreshed")

    def refresh_health_reports(self):
        for username, process in list(self.processes.items()):
            if process.poll() is not None:
                continue
            source = self.latest_source(username)
            if source is None:
                continue
            try:
                report = _write_health_report(source)
                stalled = bool(report.get("event_stalled"))
                if stalled and username not in self.stalled_collectors:
                    self.stalled_collectors.add(username)
                    self.log(f"[STALL] @{username} no captured events for "
                             f"{report['last_event_age_seconds']:.0f}s; collector kept running")
                elif not stalled and username in self.stalled_collectors:
                    self.stalled_collectors.discard(username)
                    self.log(f"[RECOVER] @{username} event flow resumed")
            except (ImportError, OSError) as exc:
                self.log(f"[HEALTH] @{username} report failed: {exc}")

    def latest_source(self, username):
        root = self.output_root()
        candidates = []
        if not root.exists():
            return None
        for path in root.iterdir():
            if not path.is_dir() or not re.match(r"^\d{8}_\d{6}_", path.name):
                continue
            meta_path = path / "session.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if str(meta.get("username") or "").lstrip("@") == username:
                candidates.append((str(meta.get("collector_started_at_utc") or ""), path))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def check_crashed_collectors(self):
        for username, process in list(self.processes.items()):
            code = process.poll()
            if code is None:
                continue

            self.log(
                f"[EXIT] @{username} collector exited code={code}"
            )

            self.processes.pop(username, None)
            state = self.states[username]
            state["collector_pid"] = None
            state["collector_started_at_utc"] = None

            finished_source = self.latest_finished_source(username)
            if finished_source is not None:
                finished_meta = json.loads((finished_source / "session.json").read_text(encoding="utf-8"))
                if finished_meta.get("status") in {"offline_confirmed", "live_end"}:
                    self.aggregate_daily(username)

            latest_source = self.latest_source(username)
            if latest_source is not None:
                try:
                    latest_meta = json.loads((latest_source / "session.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    latest_meta = {}
                if latest_meta.get("status") == "rate_limited":
                    self.note_quota(latest_meta.get("rate_limit_kind") or "generic", f"@{username} collector")

            # If the previous probe still said LIVE, restart after a short
            # delay. The next probe will decide whether collection continues.
            if paused(self.quota_pause_until_utc):
                state["status"] = "quota_paused"
            elif (
                self.config["restart_collector_on_crash"]
                and state["consecutive_misses"] == 0
                and state["last_confirmed_live_at_utc"]
            ):
                state["collector_restart_count"] += 1
                state["status"] = "restart_pending"
            else:
                state["status"] = "waiting"

    def apply_probe(self, result):
        username = result["username"]
        state = self.states[username]

        state["last_probe_at_utc"] = result["checked_at_utc"]
        state["last_probe_error"] = result["error"]

        limit_kind = classify_limit(result["error"])
        if limit_kind:
            self.note_quota(limit_kind, f"@{username} probe")
            state["status"] = "quota_paused"
            return

        if result["confirmed_live"]:
            state["last_confirmed_live_at_utc"] = result["checked_at_utc"]
            state["consecutive_misses"] = 0

            if result["room_id"]:
                previous_room = state["last_room_id"]
                state["last_room_id"] = result["room_id"]

                if previous_room and previous_room != result["room_id"]:
                    self.log(
                        f"[ROOM] @{username} "
                        f"{previous_room} -> {result['room_id']}"
                    )

            if self.probe_only:
                state["status"] = "live_confirmed"
                self.log(
                    f"[LIVE] @{username} room={result['room_id'] or 'unknown'} "
                    "(probe only; Collector not started)"
                )
                return

            process = self.processes.get(username)
            if process is None or process.poll() is not None:
                self.start_collector(
                    username,
                    room_id=result["room_id"],
                )

            self.log(
                f"[LIVE] @{username} "
                f"room={result['room_id'] or 'unknown'}"
            )
            return

        # No LIVE confirmation. This may be actual offline, timeout, TikTokLive
        # failure, or network failure. Require several consecutive misses.
        state["consecutive_misses"] += 1

        self.log(
            f"[MISS] @{username} "
            f"{state['consecutive_misses']}/"
            f"{self.config['offline_confirmations']} "
            f"error={result['error'] or 'none'}"
        )

        if self.probe_only:
            if "is not currently live" in str(result["error"] or "").casefold() and (
                state["consecutive_misses"] >= self.config["offline_confirmations"]
            ):
                state["status"] = "not_live"
            elif result["error"]:
                state["status"] = "probe_error"
            else:
                state["status"] = "waiting"
            return

        process = self.processes.get(username)
        if process is not None and process.poll() is None:
            # Probe failures are not authoritative offline signals. The
            # collector owns the WebSocket and must continue through transient
            # timeouts, 429s, and probe endpoint failures.
            state["status"] = "collecting"
            self.log(
                f"[KEEP] @{username} collector pid={process.pid} "
                "kept alive despite probe miss"
            )
            return

        if self.should_start_probe_error_fallback(username, result["error"]):
            self.start_collector(username)
            state["status"] = "connecting"
            self.log(
                f"[FALLBACK] @{username} collector started without LIVE probe; "
                "probe error is non-authoritative"
            )

    def shutdown(self):
        self.log("[WATCHER] shutting down")

        for username in list(self.processes):
            self.stop_collector(
                username,
                reason="watcher_shutdown",
            )

        self.save_state()
        self.stop_path.unlink(missing_ok=True)
        self.release_pid()

    def run(self):
        enabled = [
            s for s in self.config["streamers"]
            if s["enabled"]
        ]

        self.acquire_pid()
        self.stop_path.unlink(missing_ok=True)
        self.log(
            f"[WATCHER] started{' (probe only)' if self.probe_only else ''} | "
            + (", ".join(
                f"@{s['username']}" for s in enabled
            ) if enabled else "no enabled streamers")
        )
        self.log(
            f"[CONFIG] poll={self.config['poll_seconds']}s "
            f"probe_timeout={self.config['probe_timeout_seconds']}s "
            f"offline_confirmations="
            f"{self.config['offline_confirmations']} "
            f"timezone=Asia/Taipei "
            f"probe_error_fallback="
            f"{self.config.get('start_collector_on_probe_error', True)}"
        )

        try:
            while self.running:
                cycle_started = time.monotonic()

                if self.stop_path.exists():
                    self.log("[WATCHER] stop requested")
                    self.running = False
                    break

                self.sync_config()
                enabled = [
                    s for s in self.config["streamers"]
                    if s["enabled"]
                ]
                self.check_crashed_collectors()

                if time.monotonic() - self.last_health_check >= float(self.config.get("health_check_seconds", 60)):
                    self.refresh_health_reports()
                    self.last_health_check = time.monotonic()

                # Do not probe a streamer while its collector is healthy.
                # Probe failures (429/timeout) are not proof that a LIVE ended
                # and must never stop an active collection process.
                probe_target = self.select_probe_target(enabled)
                if probe_target:
                    results = asyncio.run(
                        probe_all(
                            [probe_target],
                            self.config["probe_timeout_seconds"],
                        )
                    )

                    for result in results:
                        self.apply_probe(result)

                self.save_state()

                elapsed = time.monotonic() - cycle_started
                sleep_seconds = max(
                    1,
                    self.config["poll_seconds"] - elapsed,
                )

                if self.stop_path.exists():
                    self.log("[WATCHER] stop requested")
                    self.running = False
                    break
                time.sleep(sleep_seconds)

        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()


def main():
    parser = argparse.ArgumentParser(
        description="Probe TikTok LIVE status and optionally auto-start collection."
    )
    parser.add_argument(
        "--config",
        default="watchlist.json",
        help="Watcher JSON config (default: watchlist.json)",
    )
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Only update LIVE probe status; never start a Collector.",
    )
    args = parser.parse_args()

    Watcher(args.config, probe_only=args.probe_only).run()


if __name__ == "__main__":
    main()
