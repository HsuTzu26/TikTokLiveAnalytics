import json
from datetime import date, timedelta
from html import escape
from pathlib import Path
import altair as alt
import streamlit as st
from src.reports import build_report, period_bounds

LABELS = {
    'date': '日期', 'room_id': '直播間 ID', 'chat': '留言數', 'joins': '進場事件',
    'likes': '收集期間按讚增量', 'gifts': '已完成送禮事件', 'diamonds': '已捕獲 Diamonds',
    'follows': '追蹤事件', 'shares': '分享事件', 'subscribes': '訂閱事件',
    'viewer_samples': '人數採樣筆數', 'sample_avg_viewers': '平均同時觀看（採樣）',
    'peak_viewers': '最高同時觀看（觀察值）', 'first_event': '首筆事件時間',
    'last_event': '末筆事件時間', 'unique_chatters_observed': '不重複留言者',
    'gifters_observed': '不重複送禮者', 'user': '觀眾帳號', 'entry_source': '進場來源',
}

def readable_table(frame):
    frame = frame.copy()
    if 'sample_avg_viewers' in frame:
        frame['sample_avg_viewers'] = frame['sample_avg_viewers'].round(1)
    for column in ('first_event', 'last_event'):
        if column in frame:
            frame[column] = frame[column].str.replace('T', ' ', regex=False).str.slice(0, 19)
    return frame.rename(columns=LABELS)

def report_insights(report, start, end):
    daily, gifters = report['daily'], report['gifters']
    if daily.empty:
        return []
    notes = [f'這段期間有 {len(daily)} 天取得互動或人流資料；選定範圍共 {(end-start).days+1} 天。沒有資料的日期不代表未開播。']
    sampled = daily.dropna(subset=['peak_viewers'])
    if not sampled.empty:
        best = sampled.loc[sampled.peak_viewers.idxmax()]
        notes.append(f"觀察到最高同時觀看人數在 {best['date']}，為 {int(best['peak_viewers'])} 人。這是收集到的尖峰，不保證等於官方整場尖峰。")
    total = float(gifters.diamonds.sum()) if not gifters.empty else 0
    if total > 0:
        share = float(gifters.iloc[0].diamonds) / total * 100
        notes.append(f'捕獲送禮價值最高的一位觀眾占 {share:.1f}% Diamonds。占比高表示送禮較集中；不能直接推論其他觀眾不支持直播。')
    notes.append('比較不同場次前，先查看人數採樣筆數及首末事件時間；收集較短的場次，留言或送禮總量可能較少。')
    return notes

def render_reports(root: Path, streamers: list[str]):
    st.title('直播回顧：週報與月報')
    st.caption('選一段時間，看看人流、互動與送禮表現。所有時間皆為台灣時間。')
    names = set(streamers)
    for path in root.glob('*/session.json'):
        try:
            name = json.loads(path.read_text(encoding='utf-8')).get('username')
            if name:
                names.add(name)
        except (OSError, ValueError):
            pass
    with st.form('report_controls'):
        cols = st.columns(3)
        username = cols[0].selectbox('分析哪位直播主？', sorted(names), key='report_username') if names else None
        mode = cols[1].selectbox('報表期間', ['Week', 'Month', 'Custom'], format_func=lambda x: {'Week':'週報','Month':'月報','Custom':'自訂日期'}[x], help='週報為週一至週日；月報為整個曆月。')
        anchor = cols[2].date_input('選擇該週／月中的任一天', date.today() - timedelta(days=7))
        custom = st.date_input('自訂日期範圍（只在自訂模式使用）', (anchor, anchor + timedelta(days=6)))
        submitted = st.form_submit_button('產生報表', type='primary')
    if submitted and username:
        if mode == 'Custom':
            if not isinstance(custom, (tuple, list)) or len(custom) != 2:
                st.error('請選擇開始及結束日期。')
                return
            start, end = custom
        else:
            start, end = period_bounds(mode, anchor)
        with st.spinner('正在整理資料並排除重複事件…'):
            st.session_state['generated_report'] = (username, start, end, build_report(root, username, start, end))
    saved = st.session_state.get('generated_report')
    if not saved:
        st.info('先選直播主及期間，再按「產生報表」。例如 Chloe 的第一份報表可使用自訂日期 2026-09-10～2026-09-16。')
        return
    username, start, end, report = saved
    st.subheader(f'@{username} · {start} — {end}')
    events, daily, rooms = report['events'], report['daily'], report['rooms']
    st.caption('目前顯示的是上次按「產生報表」時的結果；調整條件後請再次按下按鈕。')
    st.info('這份報表分析的是程式實際收集到的資料，不是 TikTok 官方整場總數。收集中斷或中途開始追蹤，都可能讓統計偏低。')
    if events.empty or daily.empty:
        st.info('這段期間尚無可分析的人流或互動資料。請改選其他日期；只有系統紀錄的日期不會產生趨勢。')
        return
    cols = st.columns(5)
    cols[0].metric('觀察到的直播場次', int((rooms.room_id != 'unknown').sum()), help='依不同 Room ID 計算；跨午夜仍算同一場，不含無法辨識的房間。')
    cols[1].metric('最高同時觀看人數', int(events.viewer_count.max()) if events.viewer_count.notna().any() else '無採樣', help='收集期間觀察到的最大人數，不是觀看總次數或不重複觀眾。')
    cols[2].metric('收集到的留言', f'{int(events.chat.sum()):,}', help='去除重複資料後的留言事件；未收集時段無法回補。')
    cols[3].metric('捕獲 Diamonds', f'{events.diamonds.sum():,.0f}', help='已完成 Gift 事件的價值加總；不等於可提領收入或官方總餘額。')
    cols[4].metric('收集期間按讚增量', f'{events.likes.sum():,}', help='加總 Like batches，不是直播間 totalLikes，也不包含追蹤前的按讚。')
    notes = report_insights(report, start, end)
    st.subheader('這份報表的重點')
    for note in notes:
        st.markdown('- ' + note)
    with st.expander('如何閱讀指標？第一次使用請看這裡'):
        st.markdown('平均同時觀看：對已收到的人數樣本取平均，不是每位觀眾的觀看時間。\n\n進場事件：可能包含同一人重複進入，不等於不重複觀眾。\n\n不重複留言者／送禮者：只統計可辨識的帳號，不代表整場所有觀眾。\n\n首末事件時間：表示資料涵蓋時段，不等於確認的開播／下播时间。\n\n圖表空白或断線：表示没有採樣，不代表觀看人數為零。')
    with st.expander('每日明細：查看哪天表現較好'):
        st.dataframe(readable_table(daily), hide_index=True, use_container_width=True)
    viewer_samples = events.dropna(subset=['viewer_count']).set_index('time')['viewer_count']
    viewer = viewer_samples.resample('1min').mean().reset_index()
    resolution = '1 minute'
    if viewer.viewer_count.notna().sum() > 4500:
        viewer = viewer_samples.resample('15min').mean().reset_index()
        resolution = '15 minutes'
    viewer['taipei_time'] = viewer.time.dt.strftime('%Y-%m-%dT%H:%M:%S')
    charts = []
    if not viewer.empty:
        # Break lines at missing minutes rather than imply continuous collection.
        viewer['segment'] = viewer.viewer_count.isna().cumsum()
        chart = alt.Chart(viewer.dropna(subset=['viewer_count'])).mark_line().encode(
            x=alt.X('taipei_time:T', title='台灣時間'), y=alt.Y('viewer_count:Q', title='同時觀看人數（觀察值）'),
            detail='segment:N', tooltip=['taipei_time:T', 'viewer_count:Q']).properties(height=300)
        st.subheader('人數走勢：什麼時候人最多？')
        st.caption(f'每 {resolution} 的樣本平均；空白處為資料缺口。上升代表當時同時觀看人數增加，不能單靠這張圖判定原因。')
        st.altair_chart(chart, use_container_width=True)
        charts.append(('Viewer trend', chart))
    activity = daily.melt(id_vars='date', value_vars=['chat', 'joins', 'follows', 'shares', 'subscribes'], var_name='metric', value_name='count')
    activity['metric'] = activity.metric.map(LABELS)
    chart = alt.Chart(activity).mark_line(point=True).encode(x=alt.X('date:T', title='台灣日期'), y=alt.Y('count:Q', title='收集到的事件數'), color=alt.Color('metric:N', title='互動類型'), tooltip=['date:T', 'metric:N', 'count:Q'])
    st.subheader('互動走勢：觀眾做了什麼？')
    st.caption('圖例可區分留言、進場、追蹤、分享與訂閱。進場量通常遠大於其他互動；各日期收集時長不同，總量不宜直接當作轉換率。')
    st.altair_chart(chart, use_container_width=True)
    charts.append(('Daily interaction', chart))
    chart = alt.Chart(daily).mark_bar().encode(x=alt.X('date:T', title='台灣日期'), y=alt.Y('diamonds:Q', title='捕獲 Diamonds'), tooltip=['date:T', 'diamonds:Q'])
    st.subheader('送禮走勢：哪天捕獲的 Gift 價值較高？')
    st.caption('每根柱子代表當天捕獲的 Diamonds。零值只代表沒有收集到已完成 Gift，不保證整場無人送禮。')
    st.altair_chart(chart, use_container_width=True)
    charts.append(('Diamonds', chart))
    sources = events.entry_source.dropna().value_counts().rename_axis('entry_source').reset_index(name='joins')
    tables = [('每日統計', readable_table(daily)), ('各場直播比較（只含選定期間）', readable_table(rooms)), ('送禮者排行（捕獲值）', readable_table(report['gifters'])), ('進場來源', readable_table(sources))]
    for title, table in tables[1:]:
        with st.expander(title):
            if title.startswith('送禮'):
                st.caption('同一帳號多次送禮會合併；事件數不是禮物件數，Diamonds 可用來比較送禮價值。')
            elif title == '進場來源':
                st.caption('來源代碼由 TikTok 事件提供，可能缺失；只代表捕獲到的進場來源，不代表完整觀眾來源。')
            else:
                st.caption('跨午夜同一 Room ID 保留為一場；首末事件只代表收集範圍，並非官方直播時長。')
            st.dataframe(table, hide_index=True, use_container_width=True)
    with st.expander('資料限制與技術檢查'):
        st.write('已自動排除每日彙整與原始片段的重複紀錄。以下檢查涵蓋掃描到的所有日期，不只是選定期間。')
        st.write(f"排除重複紀錄：{report['quality']['overlapping_records_skipped']:,}；格式錯誤：{report['quality']['invalid_lines']:,}；缺時間戳：{report['quality']['untimed_records']:,}。")
        st.caption('目前尚未計算完整收集覆蓋率，也無法還原官方不重複觀眾、平均觀看時間與未收集事件。')
    stem = f'{username}_{start}_{end}'
    # Embed chart fragments, not nested standalone HTML documents.
    html = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>LIVE Report</title></head><body>'
    html += f'<h1>{escape(username)} · {start} — {end}</h1><p>Asia/Taipei. Captured data, not official totals. Averages are sample-based; event spans are not broadcast duration. Missing days are not zero.</p>'
    for title, table in tables:
        html += f'<h2>{escape(title)}</h2>' + table.to_html(index=False, escape=True)
    html += '<h2>重點摘要</h2><ul>' + ''.join('<li>' + escape(note) + '</li>' for note in notes) + '</ul>'
    for title, chart in charts:
        html += f'<h2>{escape(title)}</h2>' + chart.to_html(fullhtml=False)
    html += '<h2>Source data quality</h2><pre>' + escape(json.dumps(report['quality'], indent=2)) + '</pre></body></html>'
    left, right = st.columns(2)
    left.download_button('下載每日統計 CSV', readable_table(daily).to_csv(index=False).encode('utf-8-sig'), f'{stem}.csv', 'text/csv')
    right.download_button('下載圖表報告 HTML', html.encode('utf-8'), f'{stem}.html', 'text/html')
