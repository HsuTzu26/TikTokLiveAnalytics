"""Conservative, shared TikTool connection cooldown rules."""
from datetime import datetime, timedelta, timezone


def classify_limit(message):
    value = str(message or '').lower()
    if 'daily demo limit' in value or 'sessions / 24h' in value:
        return 'daily'
    if 'sandbox allows' in value and 'per hour' in value:
        return 'hourly'
    if '4429' in value or 'http error 429' in value or 'too many requests' in value or 'rate limit' in value:
        return 'generic'
    return None


def cooldown_seconds(kind):
    return {'daily': 86400, 'hourly': 3600, 'generic': 900}[kind]


def pause_until(previous, kind, now=None):
    now = now or datetime.now(timezone.utc)
    until = now + timedelta(seconds=cooldown_seconds(kind))
    if previous:
        try:
            old = datetime.fromisoformat(previous.replace('Z', '+00:00'))
            if old.tzinfo and old > until:
                until = old
        except ValueError:
            pass
    return until.astimezone(timezone.utc).isoformat()


def paused(until, now=None):
    if not until:
        return False
    try:
        end = datetime.fromisoformat(until.replace('Z', '+00:00'))
        return end.tzinfo is not None and (now or datetime.now(timezone.utc)) < end
    except ValueError:
        return False
