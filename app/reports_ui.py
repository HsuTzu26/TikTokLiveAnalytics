import json
from datetime import date, timedelta
from html import escape
from pathlib import Path
import altair as alt
import streamlit as st
from src.reports import build_report, period_bounds

def render_reports(root: Path, streamers: list[str]):
    st.title('Reports · Weekly / Monthly')
    st.caption('Asia/Taipei · Captured data only, not TikTok official totals.')
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
        username = cols[0].selectbox('Report streamer', sorted(names), key='report_username') if names else None
        mode = cols[1].selectbox('Report period', ['Week', 'Month', 'Custom'])
        anchor = cols[2].date_input('Reference date', date.today() - timedelta(days=7))
        custom = st.date_input('Custom date range (used for Custom)', (anchor, anchor + timedelta(days=6)))
        submitted = st.form_submit_button('Generate report', type='primary')
    if submitted and username:
        if mode == 'Custom':
            if not isinstance(custom, (tuple, list)) or len(custom) != 2:
                st.error('Select both start and end dates.')
                return
            start, end = custom
        else:
            start, end = period_bounds(mode, anchor)
        with st.spinner('Reading and deduplicating captured events...'):
            st.session_state['generated_report'] = (username, start, end, build_report(root, username, start, end))
    saved = st.session_state.get('generated_report')
    if not saved:
        st.info('Choose a streamer and period, then Generate report. Use Custom for 2026-09-10 through 2026-09-16.')
        return
    username, start, end, report = saved
    st.subheader(f'@{username} · {start} — {end}')
    events, daily, rooms = report['events'], report['daily'], report['rooms']
    st.warning('Averages are sample-based. Event spans are not verified broadcast duration. Unique chatters are not unique viewers; missing events do not prove zero activity. Controls take effect after Generate report.')
    if events.empty or daily.empty:
        st.info('No timestamped analytics data in this period.')
        st.json(report['quality'])
        return
    cols = st.columns(5)
    cols[0].metric('Observed rooms', int((rooms.room_id != 'unknown').sum()))
    cols[1].metric('Peak viewers (observed)', int(events.viewer_count.max()) if events.viewer_count.notna().any() else 'N/A')
    cols[2].metric('Captured chat', int(events.chat.sum()))
    cols[3].metric('Captured diamonds', f'{events.diamonds.sum():,.0f}')
    cols[4].metric('Observed likes', f'{events.likes.sum():,}')
    st.subheader('Daily summary')
    st.dataframe(daily, hide_index=True, use_container_width=True)
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
            x=alt.X('taipei_time:T', title='Taiwan time (Asia/Taipei)'), y=alt.Y('viewer_count:Q', title='Observed viewers'),
            detail='segment:N', tooltip=['taipei_time:T', 'viewer_count:Q']).properties(height=300)
        st.subheader(f'Viewer trend · sample average ({resolution})')
        st.altair_chart(chart, use_container_width=True)
        charts.append(('Viewer trend', chart))
    activity = daily.melt(id_vars='date', value_vars=['chat', 'joins', 'follows', 'shares', 'subscribes'], var_name='metric', value_name='count')
    chart = alt.Chart(activity).mark_line(point=True).encode(x=alt.X('date:T', title='Taiwan date'), y=alt.Y('count:Q', title='Captured events'), color='metric:N', tooltip=['date:T', 'metric:N', 'count:Q'])
    st.subheader('Daily interaction trend')
    st.altair_chart(chart, use_container_width=True)
    charts.append(('Daily interaction', chart))
    chart = alt.Chart(daily).mark_bar().encode(x=alt.X('date:T', title='Taiwan date'), y=alt.Y('diamonds:Q', title='Captured Diamonds'), tooltip=['date:T', 'diamonds:Q'])
    st.subheader('Daily captured Diamonds')
    st.altair_chart(chart, use_container_width=True)
    charts.append(('Diamonds', chart))
    sources = events.entry_source.dropna().value_counts().rename_axis('entry_source').reset_index(name='joins')
    tables = [('Daily summary', daily), ('Room comparison (period-clipped)', rooms), ('Captured gifter ranking', report['gifters']), ('Observed entry sources', sources)]
    for title, table in tables[1:]:
        st.subheader(title)
        st.dataframe(table, hide_index=True, use_container_width=True)
    st.subheader('Data quality')
    st.json(report['quality'])
    st.caption('Coverage ratio is not yet calculated. Cross-midnight rooms remain one room. Missing days are absent, not zero.')
    stem = f'{username}_{start}_{end}'
    # Embed chart fragments, not nested standalone HTML documents.
    html = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>LIVE Report</title></head><body>'
    html += f'<h1>{escape(username)} · {start} — {end}</h1><p>Asia/Taipei. Captured data, not official totals. Averages are sample-based; event spans are not broadcast duration. Missing days are not zero.</p>'
    for title, table in tables:
        html += f'<h2>{escape(title)}</h2>' + table.to_html(index=False, escape=True)
    for title, chart in charts:
        html += f'<h2>{escape(title)}</h2>' + chart.to_html(fullhtml=False)
    html += '<h2>Source data quality</h2><pre>' + escape(json.dumps(report['quality'], indent=2)) + '</pre></body></html>'
    left, right = st.columns(2)
    left.download_button('Download daily CSV', daily.to_csv(index=False).encode('utf-8-sig'), f'{stem}.csv', 'text/csv')
    right.download_button('Download HTML report', html.encode('utf-8'), f'{stem}.html', 'text/html')
