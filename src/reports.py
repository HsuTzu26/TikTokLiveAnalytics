from __future__ import annotations
import calendar
import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd

TZ = ZoneInfo('Asia/Taipei')

def period_bounds(kind, anchor):
    if kind == 'Week':
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    if kind == 'Month':
        return anchor.replace(day=1), anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
    return anchor, anchor

def event_key(event, folder):
    source = event.get('source_session_id') or event.get('session_id') or folder
    seq = event.get('source_seq', event.get('seq'))
    if seq is not None:
        return str(source), str(seq)
    clean = {k: v for k, v in event.items() if k not in {'session_id', 'source_session_id', 'source_seq', 'seq', 'connection_id'}}
    return hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()

def build_report(root: Path, username: str, start: date, end: date):
    if start > end:
        raise ValueError('Start date must not be after end date.')
    lower = datetime.combine(start, time.min, TZ)
    upper = datetime.combine(end + timedelta(days=1), time.min, TZ)
    seen, rows = set(), []
    duplicates = invalid = missing_time = 0
    for meta_path in sorted(root.rglob('session.json')):
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if meta.get('username') != username:
            continue
        path = meta_path.parent / 'events.ndjson'
        if not path.exists():
            continue
        with path.open(encoding='utf-8', errors='replace') as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError('Not an object')
                except ValueError:
                    invalid += 1
                    continue
                key = event_key(event, meta_path.parent.name)
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                stamp = event.get('timestamp_ms') or event.get('received_at_ms')
                try:
                    if isinstance(stamp, (int, float)):
                        when = datetime.fromtimestamp(stamp / 1000, TZ)
                    else:
                        raw = event.get('timestamp_local') or event.get('timestamp_utc') or event.get('received_at_local') or event.get('received_at_utc')
                        when = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                        if when.tzinfo is None:
                            raise ValueError('Timezone missing')
                        when = when.astimezone(TZ)
                except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                    missing_time += 1
                    continue
                if not lower <= when < upper:
                    continue
                kind = event.get('type') or 'unknown'
                viewer = event.get('viewer_count')
                counted = kind == 'gift' and bool(event.get('counted'))
                rows.append({'time': when, 'date': when.date().isoformat(), 'room_id': str(event.get('room_id') or meta.get('room_id') or 'unknown'),
                    'type': kind, 'user': str(event.get('unique_id') or event.get('user_id') or 'unknown'),
                    'viewer_count': viewer if kind == 'viewer' and isinstance(viewer, (int, float)) else None,
                    'chat': int(kind == 'chat'), 'joins': int(kind == 'member'),
                    'likes': int(event.get('like_count') or 0) if kind == 'like' else 0,
                    'gifts': int(counted), 'diamonds': float(event.get('diamond_total') or 0) if counted else 0,
                    'follows': int(kind == 'social' and event.get('social_action') == 'follow'),
                    'shares': int(kind == 'social' and event.get('social_action') == 'share'), 'subscribes': int(kind == 'subscribe'),
                    'entry_source': event.get('entry_source') if kind == 'member' else None})
    frame = pd.DataFrame(rows)
    quality = {'overlapping_records_skipped': duplicates, 'invalid_lines': invalid, 'untimed_records': missing_time,
        'scope': 'All scanned source dates; not official completeness or selected-period error totals.'}
    if frame.empty:
        return {'events': frame, 'daily': pd.DataFrame(), 'rooms': pd.DataFrame(), 'gifters': pd.DataFrame(), 'quality': quality}
    frame = frame.sort_values('time')
    sums = ['chat', 'joins', 'likes', 'gifts', 'diamonds', 'follows', 'shares', 'subscribes']
    def summarize(group):
        values = group['viewer_count'].dropna()
        return {**{k: group[k].sum() for k in sums}, 'viewer_samples': len(values), 'sample_avg_viewers': values.mean(), 'peak_viewers': values.max(),
            'first_event': group['time'].min().isoformat(), 'last_event': group['time'].max().isoformat(),
            'unique_chatters_observed': group.loc[(group.chat == 1) & (group.user != 'unknown'), 'user'].nunique(),
            'gifters_observed': group.loc[(group.gifts == 1) & (group.user != 'unknown'), 'user'].nunique()}
    analytics = frame[~frame.type.isin(['system', 'unknown'])]
    daily = pd.DataFrame([{'date': day, **summarize(g)} for day, g in analytics.groupby('date')])
    rooms = pd.DataFrame([{'room_id': room, **summarize(g)} for room, g in analytics.groupby('room_id')])
    gifters = frame[frame.gifts == 1].groupby('user')[['gifts', 'diamonds']].sum().reset_index().sort_values('diamonds', ascending=False)
    return {'events': frame, 'daily': daily, 'rooms': rooms, 'gifters': gifters, 'quality': quality}
