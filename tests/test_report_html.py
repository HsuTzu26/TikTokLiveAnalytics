import unittest
from datetime import date
import altair as alt
import pandas as pd
from src.report_html import build_streamer_html, SafeChartJSON

class HTMLTests(unittest.TestCase):
    def test_story_before_appendix_and_unique_charts(self):
        events=pd.DataFrame([{'user':'a','room_id':'room','chat':1,'gifts':1,'diamonds':10,'date':'2026-09-11','shares':0,'follows':0,'subscribes':0,'viewer_count':12,'time':'2026-09-11'}])
        chart=alt.Chart(pd.DataFrame({'x':[1],'y':[2]})).mark_bar().encode(x='x:Q',y='y:Q')
        report={'events':events,'quality':{}}
        html=build_streamer_html('<test>',date(2026,9,11),date(2026,9,17),report,[('Viewer trend',chart),('Diamonds',chart)],['摘要'],[],[('表格',pd.DataFrame({'數字':[1]}))])
        self.assertLess(html.index('花一分鐘'),html.index('<table'))
        self.assertIn('怎麼看',html)
        self.assertIn('report_chart_0',html)
        self.assertIn('report_chart_1',html)
        self.assertNotIn('@<test>',html)
        self.assertIn('&lt;test&gt;',html)

    def test_chart_json_safe(self):
        self.assertNotIn('</script>',SafeChartJSON().encode({'name':'</script>'}))

if __name__=='__main__':
    unittest.main()
