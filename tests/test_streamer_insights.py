import unittest
import pandas as pd
from src.streamer_insights import creator_summary

class CreatorTests(unittest.TestCase):
    def test_unique_people_and_cross_room(self):
        rows=[]
        for room,user,diamonds in [('one','a',75),('two','a',0),('one','b',25),('two','unknown',0)]:
            rows.append({'user':user,'room_id':room,'diamonds':diamonds,'gifts':int(diamonds>0),'chat':1,'shares':0,'follows':0,'subscribes':0,'date':'2026-09-11','viewer_count':None,'time':'2026-09-11'})
        result=creator_summary(pd.DataFrame(rows))
        self.assertEqual(result['chatters'],2)
        self.assertEqual(result['cross_room_engagers'],1)
        self.assertEqual(result['top_share'],75)
        self.assertEqual(result['gifters'],2)
        self.assertTrue(any(action[0]=='送禮集中' for action in result['actions']))

    def test_no_gifts(self):
        frame=pd.DataFrame([{'user':'unknown','room_id':'unknown','diamonds':0,'gifts':0,'chat':0,'shares':0,'follows':0,'subscribes':0,'date':'2026-09-11','viewer_count':None,'time':'2026-09-11'}])
        result=creator_summary(frame)
        self.assertIsNone(result['top_share'])
        self.assertEqual(result['chatters'],0)

if __name__=='__main__':
    unittest.main()
