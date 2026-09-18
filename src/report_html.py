"""Creator-facing, readable standalone HTML export."""
import json
from html import escape
from src.streamer_insights import creator_summary

class SafeChartJSON(json.JSONEncoder):
    def encode(self, value):
        return super().encode(value).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')

CHART_GUIDE = {
    'Viewer trend': ('什麼時候人最多？', '線越高，當時同時觀看的人越多。空白處是沒有採樣，不是零人；高點表示收集期間人流較多的時段。'),
    'Daily interaction': ('哪天互動較多？', '不同顏色代表不同互動。收集時長不同，總量不能直接當作內容優劣或轉換率。'),
    'Diamonds': ('哪天收集到較多送禮支持？', '柱子越高，當天捕獲的送禮價值越多。這不是現金收入，漏收事件會影響比較。'),
    '送禮支持分布': ('支持來自很多人，還是少數人？', '長條越長，該觀眾送禮價值越多。少數人的長條占大部分，表示捕獲到的送禮價值較集中。'),
    '捕獲進場來源': ('觀眾從哪裡進場？', '只顯示收到的來源代碼；同一人多次進入會重複計數，未知來源不在圖中。'),
    '參與範圍：每天有多少人留言、送禮？': ('觀眾有沒有參與？', '看每天有多少可辨識帳號留言或送禮，而不是單看事件筆數。人數上升可能表示參與範圍擴大，也可能是收集時段較長。'),
    '粉絲成長訊號：追蹤與訂閱事件': ('追蹤與訂閱如何變化？', '這是每天收到的追蹤與訂閱事件，不是官方粉絲淨增長。曲線呈現選定期間的成長訊號。'),
}

def build_streamer_html(username, start, end, report, charts, notes, tables):
    events = report['events']
    summary = creator_summary(events)
    viewers = events.viewer_count.dropna()
    peak = f'{int(viewers.max()):,}' if not viewers.empty else '未取得'
    css = '''body{margin:0;background:#f4f6fa;color:#243047;font:16px/1.7 system-ui,"Microsoft JhengHei",sans-serif}main{max-width:980px;margin:auto;padding:32px 20px}h1{font-size:32px;margin-bottom:8px}h2{font-size:24px}h3{font-size:19px}p{margin:8px 0}.muted{color:#64748b;font-size:14px}.card{background:white;border:1px solid #e2e8f0;border-radius:16px;padding:24px;margin:20px 0}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.kpi{background:#fff;border-radius:14px;padding:18px;border:1px solid #e2e8f0}.number{font-size:30px;font-weight:700;color:#125eb8}.tip{background:#eef5ff;border-left:4px solid #4688da;padding:12px 16px;border-radius:8px}.chart{overflow:auto;min-height:160px}details{margin:14px 0}summary{cursor:pointer;font-weight:600;padding:10px 0}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:10px;border-bottom:1px solid #e2e8f0;text-align:left;white-space:nowrap}th{background:#f8fafc}li{margin:10px 0}pre{white-space:pre-wrap}@media(max-width:650px){.kpis{grid-template-columns:repeat(2,1fr)}main{padding:18px 12px}.card{padding:18px}h1{font-size:25px}}'''
    html = '<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    html += '<title>直播回顧｜' + escape(username) + '</title><style>' + css + '</style></head><body><main>'
    html += f'<header><p class="muted">你的直播回顧 · 台灣時間</p><h1>@{escape(username)}，這段時間播得怎麼樣？</h1><p>{start} ～ {end}</p></header>'
    html += '<p class="tip">這是程式實際收集到的直播片段，不是 TikTok 官方整場總數。報告只整理這段期間的參與、成長、支持與人流，不提供直播策略建議。</p>'
    metrics = [('最多同時觀看',peak,'同一時間的人數，不是總觀看次數'),
        ('有留言的觀眾',f"{summary['chatters']:,}",'期間內可辨識的不重複留言帳號'),
        ('有送禮的觀眾',f"{summary['gifters']:,}",'期間內可辨識的不重複送禮帳號'),
        ('收集到的送禮價值',f'{events.diamonds.sum():,.0f}','單位 Diamonds，不是現金或官方總餘額')]
    html += '<section class="kpis">'
    for label, value, explanation in metrics:
        html += '<div class="kpi"><div>' + escape(label) + '</div><div class="number">' + value + '</div><div class="muted">' + escape(explanation) + '</div></div>'
    html += '</section><section class="card"><h2>花一分鐘看重點</h2><ul>'
    html += ''.join('<li>' + escape(note) + '</li>' for note in notes)
    html += '</ul></section><p class="muted">把滑鼠移到圖上可查看數值。圖表需要網路載入；畫面若空白，請確認網路連線。</p>'
    concentration = f"{summary['top_share']:.1f}%" if summary['top_share'] is not None else '未取得'
    groups = [
        ('參與', f"捕獲 {summary['chatters']:,} 位不重複留言者、{int(events.shares.sum()):,} 次分享事件，以及 {summary['cross_room_engagers']:,} 位跨場互動者。跨場指期間內至少兩個已知直播間留言或送禮，不等於全體回訪觀眾。", {'參與範圍：每天有多少人留言、送禮？','Daily interaction'}),
        ('成長', f"捕獲 {int(events.follows.sum()):,} 次追蹤事件、{int(events.subscribes.sum()):,} 次訂閱事件。這是收到的事件，不是官方粉絲淨增長。", {'粉絲成長訊號：追蹤與訂閱事件'}),
        ('支持', f"捕獲 {summary['gifters']:,} 位可辨識送禮者、{events.diamonds.sum():,.0f} Diamonds；最高送禮者占比為 {concentration}。占比描述送禮價值分布，不代表官方收益。", {'送禮支持分布','Diamonds'}),
        ('人流', f'觀察到最高同時觀看人數為 {peak} 人。觀看曲線呈現不同時間的房間人數，進場來源呈現捕獲的進場管道；都不是官方不重複觀眾或觀看時長。', {'Viewer trend','捕獲進場來源'}),
    ]
    for category, description, titles in groups:
        html += '<section class="card"><h2>' + category + '</h2><p>' + escape(description) + '</p>'
        for i, (title, chart) in enumerate(charts):
            if title not in titles:
                continue
            heading, guide = CHART_GUIDE[title]
            fragment = chart.properties(width=640).to_html(fullhtml=False, output_div=f'report_chart_{i}', embed_options={'actions':False}, json_kwds={'cls':SafeChartJSON})
            html += '<h3>' + escape(heading) + '</h3><p class="tip">怎麼看：' + escape(guide) + '</p><div class="chart">' + fragment + '</div>'
        html += '</section>'
    html += '<section class="card"><h2>哪些問題，這份報告還回答不了？</h2><p>目前沒有官方不重複觀眾與平均觀看時間，所以不能確定每個人是否留下來；沒有內容標記，也不能確定哪段內容造成變化。</p><p>跨場互動者只包含捕獲到的留言／送禮帳號；追蹤事件不是官方粉絲淨增長。不同場次的收集時長與資料缺口，也會影響總量。</p></section>'
    html += '<details class="card"><summary>附錄：想查數字時再展開</summary><p class="muted">首末事件是資料範圍，不是確認的開播／下播時間。平均人數是採樣平均，不是平均觀看時間。</p>'
    for title, table in tables:
        html += '<h3>' + escape(title) + '</h3><div class="scroll">' + table.to_html(index=False,escape=True) + '</div>'
    html += '<details><summary>技術檢查</summary><pre>' + escape(json.dumps(report['quality'],ensure_ascii=False,indent=2)) + '</pre></details></details>'
    html += '<footer class="muted">本報告包含觀眾帳號。分享前請確認隱私。這是匯出時的資料快照，不會自動更新。</footer></main></body></html>'
    return html
