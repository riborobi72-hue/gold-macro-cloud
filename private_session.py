"""Opt-in ordinary TradingView authentication; never logs or persists cookies.

The only accepted secret is a restricted Playwright storage state supplied by
the account owner. No password, private API or browser-profile extraction.
"""
import json, os, time

class PrivateSessionError(ValueError):
    pass

def load_private_session(value=None, now=None):
    raw=os.getenv('TV_STORAGE_STATE_JSON','') if value is None else value
    if not raw:return None
    if len(raw)>32768:raise PrivateSessionError('PRIVATE_SESSION_SCHEMA_INVALID')
    try:state=json.loads(raw)
    except (TypeError,ValueError):raise PrivateSessionError('PRIVATE_SESSION_SCHEMA_INVALID') from None
    if not isinstance(state,dict) or state.get('origins',[])!=[]:
        raise PrivateSessionError('PRIVATE_SESSION_SCHEMA_INVALID')
    cookies=state.get('cookies')
    if not isinstance(cookies,list) or not 1<=len(cookies)<=2:
        raise PrivateSessionError('PRIVATE_SESSION_SCHEMA_INVALID')
    result=[];names=set();clock=time.time() if now is None else now
    for cookie in cookies:
        if not isinstance(cookie,dict):raise PrivateSessionError('PRIVATE_SESSION_SCHEMA_INVALID')
        name=cookie.get('name');domain=cookie.get('domain')
        if name not in ('sessionid','sessionid_sign') or name in names or domain not in ('.tradingview.com','www.tradingview.com','tradingview.com'):
            raise PrivateSessionError('PRIVATE_SESSION_SCHEMA_INVALID')
        value=cookie.get('value')
        if not isinstance(value,str) or not value or len(value)>8192 or any(ord(c)<33 for c in value):
            raise PrivateSessionError('PRIVATE_SESSION_SCHEMA_INVALID')
        expiry=cookie.get('expires',-1)
        if not isinstance(expiry,(float,int)) or (expiry!=-1 and expiry<=clock):
            raise PrivateSessionError('PRIVATE_SESSION_EXPIRED')
        names.add(name)
        result.append({'name':name,'value':value,'domain':domain,'path':'/','secure':True,
                       'httpOnly':True,'sameSite':'Lax','expires':expiry})
    if 'sessionid' not in names:raise PrivateSessionError('PRIVATE_SESSION_SCHEMA_INVALID')
    return {'cookies':result,'origins':[]}

def may_resume_authenticated(previous_health, session):
    """A saved stop can be cleared only by supplying ordinary authentication."""
    if previous_health.get('state')!='ACCESS_BLOCKED':return True
    return bool(session) and previous_health.get('reason') in ('ORDINARY_LOGIN_REQUIRED','PRIVATE_SESSION_NOT_ACCEPTED_LOGIN_REQUIRED')

def public_ohlc_observation(raw):
    """Exclude account/layout/indicator content from authenticated artifacts."""
    lines=[s.strip() for s in raw.splitlines() if s.strip()]
    fields=[]
    for key in ('Date','Time','Open','High','Low','Close'):
        if key not in lines:return ''
        index=lines.index(key)
        if index+1>=len(lines):return ''
        fields.extend((key,lines[index+1]))
    return '\n'.join(fields)

