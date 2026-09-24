"""Opt-in Chrome chat worker. Never starts a schedule automatically."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / 'data' / 'chat_sender'
TZ = ZoneInfo('Asia/Taipei')


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)


def validate_job(value):
    username = str(value.get('username', '')).strip().lstrip('@')
    message = str(value.get('message', '')).strip()
    interval, limit = int(value.get('interval', 600)), int(value.get('limit', 3))
    if not re.fullmatch(r'[A-Za-z0-9_.]{1,64}', username):
        raise ValueError('請輸入有效的 Streamer username，不是網址。')
    if not message or len(message) > 150 or '\n' in message or '\r' in message:
        raise ValueError('訊息須為單行，長度 1–150 字。')
    if not 300 <= interval <= 86400 or not 1 <= limit <= 20:
        raise ValueError('間隔至少 5 分鐘，每次排程上限 1–20 則。')
    return dict(username=username, message=message, interval=interval, limit=limit)


class Schedule:
    def __init__(self, job, started):
        self.job = validate_job(job)
        self.count = 0
        self.next = started + self.job['interval']

    def due(self, stamp):
        return self.count < self.job['limit'] and stamp >= self.next

    def record(self, stamp):
        self.count += 1
        self.next = stamp + self.job['interval']


def enqueue(action, job=None):
    HOME.mkdir(parents=True, exist_ok=True)
    value = {'id': uuid.uuid4().hex, 'created': time.time(), 'action': action}
    if job is not None:
        value['job'] = validate_job(job)
    if action == 'stop':
        write_json(HOME / 'stop.json', value)
    else:
        # One outstanding request only; no delayed burst or backlog.
        if (HOME / 'command.json').exists():
            raise ValueError('上一個操作尚未完成，請稍候。')
        write_json(HOME / 'command.json', value)


def composer(page):
    candidates = page.locator(
        '[data-e2e="live-chat-input"] [contenteditable="true"], '
        '[data-e2e="live-chat-input"][contenteditable="true"], '
        '[contenteditable="true"][data-placeholder*="message" i], '
        '[contenteditable="true"][data-placeholder*="訊息"], '
        'textarea[placeholder*="message" i], textarea[placeholder*="訊息"]'
    )
    visible = [candidates.nth(i) for i in range(candidates.count())
               if candidates.nth(i).is_visible() and candidates.nth(i).is_enabled()]
    if len(visible) != 1:
        raise ValueError('找不到唯一可用的聊天室輸入框；請手動登入／確認介面。')
    return visible[0]


def check_room(page, username):
    from urllib.parse import urlparse
    url = urlparse(page.url)
    if url.hostname not in ('www.tiktok.com', 'tiktok.com') or url.path.rstrip('/') != f'/@{username}/live':
        raise ValueError('目前分頁不是指定直播間，已停止發送。')
    text = page.locator('body').inner_text(timeout=3000)
    if any(s.lower() in text.lower() for s in ('LIVE has ended', 'not currently LIVE', '直播已結束', '直播已结束', 'Verify to continue', '拖動滑塊')):
        raise ValueError('直播結束或出現驗證，需人工處理。')
    playing = page.locator('video').evaluate_all(
        '(videos) => videos.some(v => !v.paused && !v.ended && v.readyState >= 2)'
    )
    if not playing:
        raise ValueError('無法確認直播影片播放中；請手動播放後再測試。')
    return composer(page)


def send_once(page, job):
    box = check_room(page, job['username'])
    # Do not overwrite a user's unfinished draft.
    existing = box.input_value() if box.evaluate('(e) => e.tagName') == 'TEXTAREA' else box.inner_text()
    if existing.strip():
        raise ValueError('輸入框已有草稿，請先手動清空。')
    echo = page.get_by_text(job['message'], exact=True)
    before = echo.count()
    box.fill(job['message'], timeout=3000)
    check_room(page, job['username'])
    box.press('Enter', timeout=3000)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        if echo.count() > before:
            return 'visible_echo'
    # A timeout must never trigger a retry: the message may already be delivered.
    return 'submitted_unconfirmed'


def main():
    import msvcrt
    HOME.mkdir(parents=True, exist_ok=True)
    lock = (HOME / 'worker.lock').open('a+b')
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return
    state = {'pid': os.getpid(), 'status': 'starting', 'schedule_enabled': False}
    schedule = None

    def update(**values):
        state.update(values)
        state['updated'] = now()
        state['heartbeat'] = time.time()
        write_json(HOME / 'state.json', state)

    def log(action, result, job=None):
        entry = {'time': now(), 'action': action, 'result': result, 'username': (job or {}).get('username')}
        with (HOME / 'history.ndjson').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')

    # Discard stale commands from a previous worker. Never restore old schedules.
    for name in ('command.json', 'stop.json'):
        (HOME / name).unlink(missing_ok=True)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(HOME / 'chrome_profile'), channel='chrome', headless=False,
                viewport=None, accept_downloads=False,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto('https://www.tiktok.com/@chloe_o723_/live', wait_until='domcontentloaded', timeout=45000)
            update(status='請在專用 Chrome 手動登入，再按檢查直播間。')
            while context.pages:
                if (HOME / 'stop.json').exists():
                    (HOME / 'stop.json').unlink(missing_ok=True)
                    schedule = None
                    (HOME / 'command.json').unlink(missing_ok=True)
                    update(schedule_enabled=False, next_send=None, status='排程已停用')
                    log('stop', 'disabled')
                command = read_json(HOME / 'command.json')
                if command:
                    (HOME / 'command.json').unlink(missing_ok=True)
                try:
                    if command and time.time() - command.get('created', 0) < 60:
                        action, job = command['action'], validate_job(command['job'])
                        if action == 'open':
                            schedule = None
                            update(schedule_enabled=False, next_send=None)
                            page.goto(f"https://www.tiktok.com/@{job['username']}/live", wait_until='domcontentloaded', timeout=30000)
                            update(status='直播間已開啟，請手動登入／播放。')
                        elif action == 'check':
                            check_room(page, job['username'])
                            update(status='ready', checked_username=job['username'])
                        elif action == 'once':
                            schedule = None
                            update(schedule_enabled=False, next_send=None)
                            result = send_once(page, job)
                            log('once', result, job)
                            update(status=result, last_result=result, last_send=now(), tested_username=job['username'] if result == 'visible_echo' else None)
                        elif action == 'enable':
                            check_room(page, job['username'])
                            if state.get('tested_username') != job['username']:
                                raise ValueError('請先在同一直播間完成單次測試，確認留言出現。')
                            schedule = Schedule(job, time.monotonic())
                            update(schedule_enabled=True, sent_count=0, status='scheduled')
                            log('enable', 'enabled', job)
                    if schedule and schedule.due(time.monotonic()):
                        # Stop requests have priority over every scheduled send.
                        if (HOME / 'stop.json').exists():
                            continue
                        result = send_once(page, schedule.job)
                        log('scheduled', result, schedule.job)
                        schedule.record(time.monotonic())
                        update(status=result, last_result=result, last_send=now(), sent_count=schedule.count)
                        if result != 'visible_echo' or schedule.count >= schedule.job['limit']:
                            schedule = None
                    if schedule:
                        next_send = datetime.fromtimestamp(time.time() + max(0, schedule.next - time.monotonic()), TZ).isoformat(timespec='seconds')
                    else:
                        next_send = None
                    update(schedule_enabled=bool(schedule), next_send=next_send)
                except Exception as error:
                    schedule = None
                    log('error', type(error).__name__)
                    # Do not log browser exception text, cookies or page contents.
                    reason = str(error) if isinstance(error, ValueError) else '瀏覽器操作失敗，請確認分頁／登入狀態。'
                    update(status='paused', reason=reason, schedule_enabled=False, next_send=None)
                page.wait_for_timeout(1000)
            context.close()
    except Exception as error:
        log('worker', type(error).__name__)
        update(status='error', reason='無法啟動專用 Chrome；請確認 Chrome 與 Playwright 已安裝，且專用視窗未被其他程序占用。', schedule_enabled=False)
    finally:
        update(running=False, schedule_enabled=False, next_send=None)
        lock.close()


if __name__ == '__main__':
    main()
