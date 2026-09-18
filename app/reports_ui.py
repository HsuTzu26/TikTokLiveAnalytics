import json
from datetime import date, timedelta
from html import escape
from pathlib import Path
import altair as alt
import streamlit as st
from src.reports import build_report, period_bounds
from src.streamer_insights import creator_summary
from src.report_html import build_streamer_html

def render_creator_view(events):
    summary = creator_summary(events)
    charts = []
    st.subheader('觀眾有沒有參與？')
    cols = st.columns(3)
    cols[0].metric('參與留言的人', summary['chatters'], help='期間內可辨識的不重複留言帳號；不是不重複觀眾。')
    cols[1].metric('分享事件', int(events.shares.sum()), help='捕獲的分享事件，不代表不重複分享人數。')
    cols[2].metric('跨場互動者', summary['cross_room_engagers'], help='選定期間內，至少在兩個已知 Room ID 留言或送禮的可辨識帳號；不是全體回訪觀眾。')
    st.caption('留言人數比留言筆數更能看出參與範圍。缺少官方不重複觀眾，暫時不計算留言轉換率。')
    daily = summary['daily']
    for title, fields, labels in [
        ('參與範圍：每天有多少人留言、送禮？', ['chatters','gifters'], {'chatters':'留言者','gifters':'送禮者'}),
        ('粉絲成長訊號：追蹤與訂閱事件', ['follows','subscribes'], {'follows':'追蹤事件','subscribes':'訂閱事件'}),
    ]:
        plot = daily.melt(id_vars='date',value_vars=fields,var_name='metric',value_name='count')
        plot['metric'] = plot.metric.map(labels)
        chart = alt.Chart(plot).mark_line(point=True).encode(x=alt.X('date:T',title='台灣日期'),
            y=alt.Y('count:Q',title='觀察人數／事件數'),color=alt.Color('metric:N',title='指標'),
            tooltip=['date:T','metric:N','count:Q']).properties(height=240)
        st.subheader(title)
        st.altair_chart(chart,use_container_width=True)
        charts.append((title,chart))
    st.caption('追蹤／訂閱事件是成長訊號，不是官方新增粉絲淨值；每天收集時長不同，不能直接比較轉換率。')
    st.subheader('支持是否穩定？')
    cols = st.columns(3)
    cols[0].metric('可辨識送禮者', summary['gifters'])
    cols[1].metric('捕獲 Diamonds', f'{events.diamonds.sum():,.0f}', help='不是官方可提領收入。')
    cols[2].metric('最高送禮者占比', f"{summary['top_share']:.1f}%" if summary['top_share'] is not None else '無可計算資料')
    st.caption('重點不只是送禮總量，也要看支持來自多少人，是否過度集中於少數帳號。')
    gifters = events[(events.gifts == 1) & (events.user != 'unknown')].groupby('user').diamonds.sum().sort_values(ascending=False)
    if not gifters.empty:
        top = gifters.head(5).rename_axis('user').reset_index()
        if len(gifters)>5:
            import pandas as pd
            top = pd.concat([top,pd.DataFrame([{'user':'其他送禮者合計','diamonds':gifters.iloc[5:].sum()}])],ignore_index=True)
        chart = alt.Chart(top).mark_bar().encode(x=alt.X('diamonds:Q',title='捕獲 Diamonds'),
            y=alt.Y('user:N',sort='-x',title='送禮者'),tooltip=['user:N','diamonds:Q']).properties(height=250)
        st.altair_chart(chart,use_container_width=True)
        charts.append(('送禮支持分布',chart))
    st.subheader('需要注意什麼？下一場可以測試什麼？')
    for title, evidence, action, uncertainty in summary['actions']:
        with st.container(border=True):
            st.markdown('**' + title + '**')
            st.write('依據：' + evidence)
            st.write('下一場：' + action)
            st.caption('不確定性：' + uncertainty)
    return charts, summary['actions']

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
    cols = st.columns(2)
    username = cols[0].selectbox('分析哪位直播主？', sorted(names), key='report_username') if names else None
    mode = cols[1].selectbox('報表期間', ['Week', 'Month', 'Custom'], key='report_period', format_func=lambda x: {'Week':'週報','Month':'月報','Custom':'自訂日期'}[x], help='週報為週一至週日；月報為整個曆月。')
    anchor = date.today() - timedelta(days=7)
    custom = None
    if mode == 'Custom':
        custom = st.date_input('自訂日期範圍', (anchor, anchor + timedelta(days=6)), key='report_custom_dates')
        if isinstance(custom, (tuple, list)) and len(custom) == 2:
            st.caption(f'即將分析：{custom[0]} ～ {custom[1]}（台灣時間）')
    else:
        anchor = st.date_input('選擇該週／月中的任一天', anchor, key='report_anchor')
        preview_start, preview_end = period_bounds(mode, anchor)
        st.caption(f'即將分析：{preview_start} ～ {preview_end}（台灣時間；週報固定週一至週日）')
    submitted = st.button('產生報表', type='primary')
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
    charts, actions = render_creator_view(events)
    st.subheader('有多少人來？有沒有留下來？')
    st.info('官方觀看次數、不重複觀眾及平均觀看時間目前未取得。下方同時觀看曲線可看人流變化，但不能還原每個人是否留下。')
    cols = st.columns(3)
    cols[0].metric('觀察到的直播場次', int((rooms.room_id != 'unknown').sum()), help='依不同 Room ID 計算；跨午夜仍算同一場，不含無法辨識的房間。')
    cols[1].metric('最高同時觀看人數', int(events.viewer_count.max()) if events.viewer_count.notna().any() else '無採樣', help='收集期間觀察到的最大人數，不是觀看總次數或不重複觀眾。')
    cols[2].metric('平均同時觀看（採樣）', f'{events.viewer_count.mean():.1f}' if events.viewer_count.notna().any() else '無採樣', help='全部已捕獲樣本取平均；不同場次的採樣頻率與缺口會影響結果，並非官方 ACU。')
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
    activity = daily.melt(id_vars='date', value_vars=['chat', 'follows', 'shares', 'subscribes'], var_name='metric', value_name='count')
    activity['metric'] = activity.metric.map(LABELS)
    chart = alt.Chart(activity).mark_line(point=True).encode(x=alt.X('date:T', title='台灣日期'), y=alt.Y('count:Q', title='收集到的事件數'), color=alt.Color('metric:N', title='互動類型'), tooltip=['date:T', 'metric:N', 'count:Q'])
    st.subheader('互動走勢：觀眾做了什麼？')
    st.caption('圖例區分留言、追蹤、分享與訂閱；進場事件移到明細，避免高數量掩蓋其他互動。各日期收集時長不同，總量不宜直接當作轉換率。')
    st.altair_chart(chart, use_container_width=True)
    charts.append(('Daily interaction', chart))
    chart = alt.Chart(daily).mark_bar().encode(x=alt.X('date:T', title='台灣日期'), y=alt.Y('diamonds:Q', title='捕獲 Diamonds'), tooltip=['date:T', 'diamonds:Q'])
    st.subheader('送禮走勢：哪天捕獲的 Gift 價值較高？')
    st.caption('每根柱子代表當天捕獲的 Diamonds。零值只代表沒有收集到已完成 Gift，不保證整場無人送禮。')
    st.altair_chart(chart, use_container_width=True)
    charts.append(('Diamonds', chart))
    sources = events.entry_source.dropna().value_counts().rename_axis('entry_source').reset_index(name='joins')
    if not sources.empty:
        st.subheader('人從哪裡來？')
        st.caption('顯示捕獲到的前 10 種進場來源；同一人重複進入會重複計數，來源缺失的事件不在圖中。')
        chart = alt.Chart(sources.head(10)).mark_bar().encode(
            x=alt.X('joins:Q', title='進場事件數'), y=alt.Y('entry_source:N', sort='-x', title='TikTok 來源代碼'),
            tooltip=['entry_source:N', 'joins:Q']).properties(height=280)
        st.altair_chart(chart, use_container_width=True)
        charts.append(('捕獲進場來源', chart))
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
    html = build_streamer_html(username, start, end, report, charts, notes, actions, tables)
    left, right = st.columns(2)
    left.download_button('下載每日統計 CSV', readable_table(daily).to_csv(index=False).encode('utf-8-sig'), f'{stem}.csv', 'text/csv')
    right.download_button('下載圖表報告 HTML', html.encode('utf-8'), f'{stem}.html', 'text/html')
