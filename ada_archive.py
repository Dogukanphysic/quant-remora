"""Download public ADA candles with SHA256 verification; no account access."""
import argparse
import calendar
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
STEP = 900000


def parse_archive(blob, month, symbol='ADAUSDT'):
    year, number = map(int, month.split('-'))
    start = int(datetime(year, number, 1, tzinfo=timezone.utc).timestamp()*1000)
    expected = calendar.monthrange(year, number)[1]*96
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        if len(names) != 1 or not names[0].endswith('.csv'):
            raise ValueError('Unexpected archive contents')
        records = list(csv.reader(io.TextIOWrapper(archive.open(names[0]), encoding='utf-8')))
    rows = []
    for record in records:
        ts = int(record[0])
        ts = ts//1000 if ts > 10**14 else ts
        values = list(map(float, record[1:6]))
        o, h, l, c, v = values
        if not all(math.isfinite(a) for a in values) or not (0 < l <= min(o,c) <= max(o,c) <= h and v >= 0):
            raise ValueError('Invalid OHLCV')
        if ts != start+len(rows)*STEP:
            raise ValueError('Missing, duplicate or out-of-order candle')
        rows.append(dict(ts=ts, open=o, high=h, low=l, close=c, volume=v,
                         exchange='binance_spot', symbol=symbol))
    if len(rows) != expected:
        raise ValueError('Incomplete month')
    return rows


def download(start, end, output, symbol='ADAUSDT'):
    if symbol not in ('ADAUSDT','BTCUSDT'):
        raise ValueError('Unsupported research symbol')
    first = datetime.strptime(start, '%Y-%m')
    last = datetime.strptime(end, '%Y-%m')
    if first > last or last.strftime('%Y-%m') >= datetime.now(timezone.utc).strftime('%Y-%m'):
        raise ValueError('Use ordered, complete historical months')
    rows, sources = [], []
    current = first
    while current <= last:
        month = current.strftime('%Y-%m')
        url = f'https://data.binance.vision/data/spot/monthly/klines/{symbol}/15m/{symbol}-15m-{month}.zip'
        with urllib.request.urlopen(url+'.CHECKSUM', timeout=30) as response:
            expected = response.read().decode('ascii').split()[0]
        with urllib.request.urlopen(url, timeout=30) as response:
            blob = response.read()
        digest = hashlib.sha256(blob).hexdigest()
        if digest != expected:
            raise ValueError('Checksum mismatch')
        rows.extend(parse_archive(blob, month, symbol))
        sources.append(dict(url=url, sha256=digest))
        print(f'{month}: verified', flush=True)
        current = datetime(current.year+(current.month==12), current.month%12+1, 1)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp')
    with temporary.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(output)
    manifest = dict(rows=len(rows), start=start, end=end, sources=sources,
                    sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    output.with_suffix('.manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'Wrote {len(rows)} candles')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default='2024-09')
    parser.add_argument('--end', default='2026-08')
    parser.add_argument('--output', type=Path, default=ROOT/'data/adausdt-15m-long.csv')
    parser.add_argument('--symbol', choices=['ADAUSDT','BTCUSDT'], default='ADAUSDT')
    args = parser.parse_args()
    if args.symbol != 'ADAUSDT' and args.output == ROOT/'data/adausdt-15m-long.csv':
        args.output = ROOT/'data/btcusdt-15m-long.csv'
    download(args.start, args.end, args.output, args.symbol)
