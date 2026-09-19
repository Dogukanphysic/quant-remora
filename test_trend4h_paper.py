import tempfile
import unittest
from pathlib import Path
from trend4h_paper import connect, read, advance


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name)/'paper.db')
        self.s = read(self.db)
        self.bar = 300*14400000
        self.now = self.bar/1000+14400+10

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_startup_and_duplicate_never_buy(self):
        self.assertEqual(advance(self.s,self.bar,100,True,False,1,self.now), [])
        self.assertEqual(advance(self.s,self.bar,100,True,False,1,self.now+60), [])

    def test_buy_then_gap_stop_no_same_tick_reentry(self):
        self.s['last_bar'] = self.bar-14400000
        events = advance(self.s,self.bar,100,True,False,1,self.now)
        self.assertEqual(events[0]['side'],'BUY')
        cash = self.s['cash']
        self.assertEqual(advance(self.s,self.bar,100,True,False,1,self.now+60), [])
        self.assertEqual(self.s['cash'],cash)
        events = advance(self.s,self.bar+14400000,90,True,False,1,self.now+14400)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['reason'],'stop')
        self.assertLess(self.s['realized'],-2.5)
        self.assertIsNone(self.s['position'])

    def test_stale_signal_does_not_buy(self):
        self.s['last_bar'] = self.bar-14400000
        self.assertEqual(advance(self.s,self.bar,100,True,False,1,self.now+600), [])


if __name__ == '__main__':
    unittest.main()
