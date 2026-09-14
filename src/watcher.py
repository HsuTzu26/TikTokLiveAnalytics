import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from tiktok_live_events import TikTokLive
try:
    from .health_monitor import write_health_report
except ImportError:
    from health_monitor import write_health_report


WATCHER_VERSION = "0.2"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


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
        "streamers": [],
    }

    for key, value in defaults.items():
        config.setdefault(key, value)

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

    A successful TikTools WebSocket connection / roomInfo means LIVE.
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

    live = TikTokLive(
        username,
        auto_reconnect=False,
        max_reconnect_attempts=0,
    )

    @live.on("connected")
    def on_connected(_):
        result["connected"] = True
        result["confirmed_live"] = True

    @live.on("roomInfo")
    def on_room_info(e):
        room_id = e.get("roomId")
        if room_id is not None:
            result["room_id"] = str(room_id)
        result["ws_host"] = e.get("wsHost")
        result["cluster_region"] = e.get("clusterRegion")
        result["confirmed_live"] = True
        live.stop()

    @live.on("error")
    def on_error(e):
        result["error"] = str(e.get("error") or e)

    try:
        await asyncio.wait_for(
            live.run(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        result["error"] = result["error"] or "probe_timeout"
    except Exception as exc:
        result["error"] = (
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        try:
            live.stop()
        except Exception:
            pass

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
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)

        self.base_dir = self.config_path.parent.resolve()
        self.log_dir = self.base_dir / "data" / "watcher"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.log_path = self.log_dir / "watcher.log"
        self.state_path = self.log_dir / "watcher_state.json"
        self.pid_path = self.log_dir / "watcher.pid"
        self.stop_path = self.log_dir / "watcher.stop"

        self.states = {
            item["username"]: self._initial_state(item)
            for item in self.config["streamers"]
        }

        self.processes = {}
        self.fallback_last_started = {}
        self.last_health_check = 0.0
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
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def save_state(self):
        payload = {
            "watcher_version": WATCHER_VERSION,
            "updated_at_utc": utc_now(),
            "config_path": str(self.config_path),
            "streamers": self.states,
        }

        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_path)

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

        return [
            sys.executable,
            str(collector_script),
            username,
            "--output-root",
            str(output_root),
            "--offline-confirmations",
            str(self.config.get("offline_confirmations", 3)),
        ]

    def start_collector(self, username, room_id=None):
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
        if not self.config.get("start_collector_on_probe_error", True):
            return False
        text = str(error or "").lower()
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
        if events_path.exists() and events_path.stat().st_size > 0:
            for script in ("analyzer.py", "plot_session.py"):
                analyzer = (self.base_dir / "src" / script).resolve()
                analyzed = subprocess.run(
                    [sys.executable, str(analyzer), str(target), "--window", "60"],
                    cwd=str(self.base_dir), capture_output=True, text=True, check=False
                )
                if analyzed.returncode != 0:
                    self.log(f"[AGGREGATE] @{username} {script} failed: {analyzed.stderr.strip()}")
                    return
        try:
            write_health_report(target)
        except OSError as exc:
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
                write_health_report(source)
            except OSError as exc:
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

            # If the previous probe still said LIVE, restart after a short
            # delay. The next probe will decide whether collection continues.
            if (
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

        # No LIVE confirmation. This may be actual offline, timeout, TikTools
        # failure, or network failure. Require several consecutive misses.
        state["consecutive_misses"] += 1

        self.log(
            f"[MISS] @{username} "
            f"{state['consecutive_misses']}/"
            f"{self.config['offline_confirmations']} "
            f"error={result['error'] or 'none'}"
        )

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
            "[WATCHER] started | "
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
                probe_targets = [
                    item for item in enabled
                    if not (
                        self.processes.get(item["username"])
                        and self.processes[item["username"]].poll() is None
                    )
                ]
                if probe_targets:
                    results = asyncio.run(
                        probe_all(
                            probe_targets,
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
        description="Watch TikTok IDs and auto-start LIVE collection."
    )
    parser.add_argument(
        "--config",
        default="watchlist.json",
        help="Watcher JSON config (default: watchlist.json)",
    )
    args = parser.parse_args()

    Watcher(args.config).run()


if __name__ == "__main__":
    main()
