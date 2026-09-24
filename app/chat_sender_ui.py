import os
import subprocess
import sys
import time

import streamlit as st

from src.chat_sender import HOME, ROOT, enqueue, read_json, validate_job


def render_chat_sender():
    st.title('聊天室訊息')
    st.caption('專用 Chrome 手動登入；不使用 TikTool 發送 API，不影響 Watcher。')
    st.warning('僅在自己的或已取得同意的直播間使用。驗證碼與登入需手動處理；不要頻繁重複留言。')
    state = read_json(HOME / 'state.json', {})
    running = state.get('running', True) and time.time() - state.get('heartbeat', 0) < 20
    if st.button('1. 開啟專用 Chrome', disabled=running, key='chat_launch'):
        HOME.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [sys.executable, '-m', 'src.chat_sender'], cwd=ROOT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        st.info('正在啟動，請稍候。在新 Chrome 視窗手動登入 TikTok。')
    st.write('狀態：', state.get('status', '尚未啟動'))
    if state.get('reason') and state.get('status') in ('paused', 'error'):
        st.error(state['reason'])
    st.caption('排程：' + ('啟用中' if running and state.get('schedule_enabled') else '關閉') + '｜下次發送：' + str(state.get('next_send') or '—'))
    username = st.text_input('Streamer username', value='chloe_o723_', key='chat_username')
    message = st.text_input('訊息預覽', value='大家幫忙點點關注點點讚哦', max_chars=150, key='chat_message')
    interval = st.number_input('間隔（分鐘，至少 5）', min_value=5, max_value=1440, value=10, key='chat_interval')
    limit = st.number_input('本次排程最多發送幾則', min_value=1, max_value=20, value=3, key='chat_limit')
    job = {'username': username, 'message': message, 'interval': int(interval) * 60, 'limit': int(limit)}

    def request(action):
        try:
            validate_job(job)
            enqueue(action, job)
            st.success('操作已提交；稍候查看狀態。')
        except ValueError as error:
            st.error(str(error))

    cols = st.columns(2)
    if cols[0].button('2. 開啟指定直播間', disabled=not running):
        request('open')
    if cols[1].button('3. 檢查直播間', disabled=not running):
        request('check')
    consent = st.checkbox('我確認上述直播間與訊息，允許使用此登入帳號發送。', key='chat_consent')
    cols = st.columns(2)
    if cols[0].button('4. 確認單次發送', disabled=not (running and consent)):
        request('once')
    if cols[1].button('5. 啟用定時發送', disabled=not (running and consent)):
        request('enable')
    if st.button('停止排程（不停止 Watcher）', disabled=not running):
        enqueue('stop')
        st.success('已要求停用；已開始送出的操作無法撤回。')
    st.info('visible_echo：頁面出現相同留言，不等於伺服器送達保證。submitted_unconfirmed：已嘗試送出但未確認，排程會停用，請勿直接重送。重啟後不會自動恢復排程。')
    st.caption('登入設定檔：data/chat_sender/chrome_profile。請勿分享此資料夾。發送紀錄目前不會自動從既有分析中排除。')
    lines = st.number_input('紀錄顯示筆數', min_value=1, max_value=100, value=10, key='chat_log_limit')
    path = HOME / 'history.ndjson'
    if path.exists():
        from collections import deque
        with path.open(encoding='utf-8') as handle:
            st.code(''.join(deque(handle, maxlen=int(lines))))
