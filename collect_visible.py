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
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'data'
URL = 'https://www.tradingview.com/chart/?symbol=TVC%3ADXY&interval=5'


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
    prices = [value(k) for k in ['Open', 'High', 'Low', 'Close']]
    if not all(re.fullmatch(r'\d+(?:\.\d+)?', p) for p in prices):
        raise ValueError('PRICE_FORMAT')
    o, h, l, c = [Decimal(p) for p in prices]
    if not all(p.is_finite() and p > 0 for p in (o,h,l,c)) or not l <= min(o,c) <= max(o,c) <= h:
        raise ValueError('OHLC_INVALID')
    return [t.strftime('%Y-%m-%dT%H:%M:%SZ'), *prices]


def collect():
    from playwright.sync_api import sync_playwright
    OUTPUT.mkdir(exist_ok=True)
    raw_samples, observed = [], {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width':1280,'height':900}, locale='en-US', timezone_id='UTC')
        page = context.new_page()
        page.set_default_timeout(15000)
        try:
            response = page.goto(URL, wait_until='domcontentloaded', timeout=45000)
            if response and response.status in (401,403,429):
                raise RuntimeError('HTTP_ACCESS_OR_RATE_LIMIT_'+str(response.status))
            reject = page.get_by_role('button', name="Don't allow", exact=True)
            if reject.is_visible():
                reject.click()
            page.get_by_role('button', name='Object tree and data window', exact=True).click()
            page.get_by_role('tab', name='Data window', exact=True).click()
            grid = page.get_by_role('treegrid')
            grid.wait_for(state='visible')
            chart = page.get_by_label('Chart for TVC:DXY, 5 minutes', exact=True)
            box = chart.bounding_box()
            if not box or box['width'] < 300:
                raise RuntimeError('CHART_GEOMETRY_UNAVAILABLE')
            # Coordinates derive from the actual chart bounds, not a hidden chart model.
            for x in range(int(box['x']+box['width']-75), int(box['x']+25), -3):
                if page.get_by_role('button', name='Join for free', exact=True).is_visible():
                    raise RuntimeError('ORDINARY_LOGIN_REQUIRED')
                if page.get_by_text('Verify you are human', exact=False).is_visible():
                    raise RuntimeError('HUMAN_VERIFICATION_REQUIRED')
                page.mouse.move(x, box['y']+box['height']*0.65)
                page.wait_for_timeout(60)  # animation/crosshair settlement, bounded per run
                raw = grid.inner_text()
                captured = datetime.now(timezone.utc)
                raw_samples.append({'capturedAt':captured.isoformat(), 'x':x, 'raw':raw})
                try:
                    row = parse_visible(raw, captured)
                except (ValueError, IndexError):
                    continue
                if row:
                    existing = observed.get(row[0])
                    if existing and existing != row:
                        raise RuntimeError('INTRA_RUN_REVISION')
                    observed[row[0]] = row
            if len(observed) < 12:
                raise RuntimeError('LESS_THAN_TWELVE_VALID_CLOSED_BARS')
            tail = sorted(observed)[-12:]
            if any(datetime.fromisoformat(b)-datetime.fromisoformat(a)!=timedelta(minutes=5)
                   for a,b in zip(tail,tail[1:])):
                raise RuntimeError('LATEST_TWELVE_BARS_NOT_CONSECUTIVE')
            newest = max(observed)
            if datetime.now(timezone.utc) - datetime.fromisoformat(newest.replace('Z','+00:00')) > timedelta(hours=3):
                raise RuntimeError('STALE_FEED_OR_MARKET_CLOSED_NEEDS_CLASSIFICATION')
            page.screenshot(path=str(OUTPUT/'success.png'))
        except Exception:
            page.screenshot(path=str(OUTPUT/'failure.png'))
            raise
        finally:
            (OUTPUT/'observations.json').write_text(json.dumps(raw_samples,indent=2)+'\n',encoding='utf-8')
            browser.close()
    dest = OUTPUT/'latest.json'
    prior = json.loads(dest.read_text()) if dest.exists() else {'rows':[]}
    merged = {r[0]:r for r in prior['rows']}
    revisions = []
    for t, row in observed.items():
        if t in merged and merged[t] != row:
            revisions.append({'timestamp':t,'retained':merged[t],'observed':row})
        else:
            merged[t] = row
    rows = sorted(merged.values())[-1500:]  # transport overlap cache, not archival retention
    gaps = [{'after':a[0],'before':b[0],'cause':'NON_VERIFICATO'} for a,b in zip(rows,rows[1:])
            if datetime.fromisoformat(b[0]) - datetime.fromisoformat(a[0]) != timedelta(minutes=5)]
    result = {'source':'TradingView','providerSymbol':'TVC:DXY','symbol':'DXY','timeframe':'5min',
              'capturedAt':datetime.now(timezone.utc).isoformat(),'closureGraceSeconds':60,
              'method':'visible_data_window','instrumentNature':'NON_VERIFICATO; UI previously Index Cfd',
              'execution':{'event':os.getenv('GITHUB_EVENT_NAME','local'),'runId':os.getenv('GITHUB_RUN_ID')},
              'rows':rows,'observedClosedBars':len(observed),'gaps':gaps,'revisions':revisions}
    tmp = OUTPUT/'latest.pending.json'
    tmp.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    tmp.replace(dest)
    print(json.dumps({'observed':len(observed),'first':rows[0][0],'last':rows[-1][0],
                      'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    collect()
