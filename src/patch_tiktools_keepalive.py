from pathlib import Path
import shutil

try:
    import tiktok_live_events.client as client_module
except ImportError as exc:
    raise SystemExit(
        "Cannot import tiktok_live_events. "
        "Activate the main .venv first."
    ) from exc

path = Path(client_module.__file__).resolve()
backup = path.with_suffix(path.suffix + ".before_keepalive_patch.bak")

text = path.read_text(encoding="utf-8")

old = (
    "websockets.connect(tt_url, extra_headers=extra_headers, "
    "max_size=8 * 1024 * 1024) as tt_ws"
)

new = (
    "websockets.connect(tt_url, extra_headers=extra_headers, "
    "max_size=8 * 1024 * 1024, ping_interval=None) as tt_ws"
)

if new in text:
    print("[ok] Patch is already applied.")
    print(f"[file] {path}")
    raise SystemExit(0)

if old not in text:
    print("[error] Expected TikTools WebSocket line was not found.")
    print(f"[file] {path}")
    print("No changes were made.")
    raise SystemExit(1)

if not backup.exists():
    shutil.copy2(path, backup)
    print(f"[backup] {backup}")

text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")

print("[patched] Disabled standard WebSocket ping on TikTok upstream socket.")
print("[reason] TikTok heartbeat is already sent separately every 10 seconds.")
print(f"[file]   {path}")
print()
print("Changed:")
print("  max_size=8 * 1024 * 1024")
print("to:")
print("  max_size=8 * 1024 * 1024, ping_interval=None")
