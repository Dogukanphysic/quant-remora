import calendar
import io
import unittest
import zipfile
from datetime import datetime, timezone
from ada_archive import parse_archive, STEP


class ArchiveTests(unittest.TestCase):
    def blob(self, month, micros=False, duplicate=False):
        y,m=map(int,month.split('-'))
        ts=int(datetime(y,m,1,tzinfo=timezone.utc).timestamp()*1000)
        lines=[]
        for i in range(calendar.monthrange(y,m)[1]*96):
            t=ts+(i-(duplicate and i==2))*STEP
            lines.append(f'{t*(1000 if micros else 1)},1,2,0.5,1.5,100')
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as z:
            z.writestr('test.csv','\n'.join(lines))
        return buffer.getvalue()

    def test_microseconds_and_milliseconds(self):
        for month,micro in [('2024-12',False),('2025-01',True)]:
            rows=parse_archive(self.blob(month,micro),month)
            self.assertEqual(rows[1]['ts']-rows[0]['ts'],STEP)

    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            parse_archive(self.blob('2025-01',True,True),'2025-01')

    def test_btc_source_label(self):
        rows = parse_archive(self.blob('2025-01',True),'2025-01','BTCUSDT')
        self.assertEqual(rows[0]['symbol'],'BTCUSDT')


if __name__ == '__main__': unittest.main()
