import unittest
from src.chat_sender import validate_job, Schedule


class SenderTests(unittest.TestCase):
    def test_validation(self):
        job = validate_job({'username': '@chloe_o723_', 'message': 'hello', 'interval': 600, 'limit': 3})
        self.assertEqual(job['username'], 'chloe_o723_')
        for change in ({'username': '../bad'}, {'message': ''}, {'interval': 1}, {'limit': 1000}):
            with self.assertRaises(ValueError):
                validate_job({**job, **change})

    def test_schedule_no_catchup_and_limit(self):
        schedule = Schedule({'username': 'a', 'message': 'hello', 'interval': 600, 'limit': 2}, 100)
        self.assertFalse(schedule.due(699))
        self.assertTrue(schedule.due(700))
        schedule.record(2000)
        self.assertFalse(schedule.due(2001))
        self.assertTrue(schedule.due(2600))
        schedule.record(2600)
        self.assertFalse(schedule.due(9999))
