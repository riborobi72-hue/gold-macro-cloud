"""Exchange session classification; never fabricates bars or infers unknown CFD hours."""
from datetime import datetime,timedelta,timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC=timezone.utc

@lru_cache(maxsize=32)
def exchange_calendar(name,year):
    import exchange_calendars as xc
    return xc.get_calendar(name,start=f'{year-2}-01-01',end=f'{year+1}-12-31')

def utc(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value

def state_at(moment,spec):
    c=spec.get('calendar',{})
    if c.get('type')!='exchange_calendars' or not c.get('id'):
        return 'UNVERIFIED_SESSION'
    moment=utc(moment);local=moment.astimezone(ZoneInfo(c['timezone']))
    try:
        import pandas as pd
        cal=exchange_calendar(c['id'],local.year);day=pd.Timestamp(local.date())
        if day<cal.first_session or day>cal.last_session:return 'CALENDAR_OUT_OF_RANGE'
        if day not in cal.schedule.index:
            return 'WEEKEND_CLOSED' if local.weekday()>4 else 'HOLIDAY_CLOSED'
        row=cal.schedule.loc[day];t=pd.Timestamp(moment)
        if t<row['open'] or t>=row['close']:return 'OUTSIDE_SESSION'
        if pd.notna(row['break_start']) and row['break_start']<=t<row['break_end']:return 'SCHEDULED_BREAK'
        return 'IN_SESSION'
    except Exception:
        return 'CALENDAR_UNAVAILABLE'

def gap_records(rows,spec):
    result=[]
    for a,b in zip(rows,rows[1:]):
        start,end=utc(a[0])+timedelta(minutes=5),utc(b[0])
        if start>=end:continue
        counts={};missing=[];cursor=start
        while cursor<end:
            state=state_at(cursor,spec);counts[state]=counts.get(state,0)+1
            if state=='IN_SESSION' and len(missing)<100:missing.append(cursor.strftime('%Y-%m-%dT%H:%M:%SZ'))
            cursor+=timedelta(minutes=5)
        n=counts.get('IN_SESSION',0)
        unknown=sum(v for k,v in counts.items() if k.startswith('CALENDAR_') or k=='UNVERIFIED_SESSION')
        closed=sum(counts.values())-n-unknown
        classification=('UNVERIFIED_SESSION' if unknown else ('MIXED_CLOSURE_AND_MISSING' if n and closed else 'MISSING_DURING_SESSION' if n else 'REGULAR_CLOSURE'))
        result.append({'after':a[0],'before':b[0],'classification':classification,'missingExpectedBars':n,'regularClosedSlots':closed,'unknownSessionSlots':unknown,'sessionCounts':counts,'missingExpectedTimestamps':missing,'cause':'UNVERIFIED_SOURCE_OR_UI_GAP' if n else classification})
    return result

def session_health(rows,spec,captured):
    captured=utc(captured);state=state_at(captured,spec)
    newest=utc(rows[-1][0]);age=(captured-newest).total_seconds()/60
    # Search only for exchange instruments: a closed Asian market must not be
    # called stale merely because its last valid quote is several hours old.
    expected=None
    if spec.get('calendar',{}).get('type')=='exchange_calendars':
        minute=int((captured-timedelta(minutes=6)).timestamp()//300)*300
        cursor=datetime.fromtimestamp(minute,UTC)
        for _ in range(14*24*12):
            if state_at(cursor,spec)=='IN_SESSION':expected=cursor;break
            if state_at(cursor,spec).startswith('CALENDAR_'):break
            cursor-=timedelta(minutes=5)
    behind=max(0,(expected-newest).total_seconds()/60) if expected else None
    status=('MISSING_EXPECTED_SESSION_DATA' if behind is not None and behind>30 else 'CLOSED_REGULAR' if state in ('WEEKEND_CLOSED','HOLIDAY_CLOSED','OUTSIDE_SESSION','SCHEDULED_BREAK') else 'OK' if age<=180 else 'NO_RECENT_BARS_SESSION_UNVERIFIED')
    return {'state':status,'sessionState':state,'lastBarUTC':rows[-1][0],'ageMinutes':round(age,2),'minutesBehindExpectedSessionBar':behind,'calendarId':spec.get('calendar',{}).get('id'),'calendarTimezone':spec.get('calendar',{}).get('timezone'),'calendarEngine':'exchange_calendars==4.13.2' if expected else 'NON_VERIFICATO'}
