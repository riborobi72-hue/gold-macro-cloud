"""TradingView visible data window collector. No private endpoints or session extraction.

Candidate cloud implementation; only a successful real workflow can qualify it.
Stops at login/CAPTCHA/upgrade gates. Does not refresh to evade a gate.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'data'
URL = 'https://www.tradingview.com/chart/?symbol=TVC%3ADXY&interval=5'


def health_record(state, **extra):
    OUTPUT.mkdir(exist_ok=True)
    value={'state':state,'utc':datetime.now(timezone.utc).isoformat(),**extra}
    (OUTPUT/'health.json').write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')


def retry_seconds(value, now):
    try:
        seconds=int(value) if str(value).isdigit() else int((parsedate_to_datetime(value)-now).total_seconds())
        return max(3600,seconds)
    except (ValueError,TypeError,OverflowError):
        return 3600


def parse_visible(raw, captured):
    lines = [s.strip() for s in raw.splitlines() if s.strip()]
    def value(key):
        return lines[lines.index(key) + 1]
    day = value('Date')
    match = re.fullmatch(r"\w+ (\d{1,2}) (\w{3}) '(\d{2})", day)
    if not match:
        raise ValueError('UNRECOGNIZED_DATE')
    months = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
    hour, minute = map(int, value('Time').split(':'))
    t = datetime(2000 + int(match[3]), months.index(match[2])+1, int(match[1]), hour, minute, tzinfo=timezone.utc)
    if t.minute % 5:
        raise ValueError('OFF_GRID')
    if t + timedelta(minutes=6) > captured:
        return None
    prices = [value(k).removesuffix('%').strip() for k in ['Open', 'High', 'Low', 'Close']]
    if not all(re.fullmatch(r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?', p) for p in prices):
        raise ValueError('PRICE_FORMAT')
    prices=[p.replace(',','') for p in prices]
    o, h, l, c = [Decimal(p) for p in prices]
    if not all(p.is_finite() and p > 0 for p in (o,h,l,c)) or not l <= min(o,c) <= max(o,c) <= h:
        raise ValueError('OHLC_INVALID')
    return [t.strftime('%Y-%m-%dT%H:%M:%SZ'), *prices]


def collect(stream):
    from collector_core import safe_collect
    return safe_collect(stream,parse_visible,retry_seconds)


if __name__ == '__main__':
    from concurrent.futures import ThreadPoolExecutor
    config=json.loads((ROOT/'instruments.json').read_text())
    streams=[s for s in config['streams'] if s.get('enabled') and s['adapter']=='publicCloudFeed']
    if any(s['timeframe']!='5min' for s in streams):raise ValueError('TIMEFRAME_NOT_IMPLEMENTED')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(collect,streams))
    failed=[r['symbol'] for r in results if r['state'] in ('FAILED_PREVIOUS_DATA_PRESERVED','ACCESS_BLOCKED')]
    if failed:raise RuntimeError('FAILED_STREAMS_'+','.join(failed))
