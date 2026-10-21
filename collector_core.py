"""Visible UI collection with bounded bootstrap history; no private chart APIs."""
import hashlib,json,os,re,time
from datetime import datetime,timezone,timedelta
from pathlib import Path
from urllib.parse import quote
from session_rules import gap_records,session_health
from private_session import load_private_session,may_resume_authenticated,public_ohlc_observation

ROOT=Path(__file__).resolve().parent
UTC=timezone.utc

def health(directory,state,**extra):
    directory.mkdir(exist_ok=True,parents=True)
    value={'state':state,'utc':datetime.now(UTC).isoformat(),**extra}
    (directory/'health.json').write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')

def delivery_identity(label,expected):
    match=re.fullmatch(r'Chart for ([^,]+), 5 minutes',label or '')
    if not match:return False
    provider=match[1]
    if ':' not in provider:return False
    exchange,symbol=provider.split(':',1)
    # These are DOM delivery labels returned by the public chart, not endpoints
    # requested by this client. Removing the suffix only validates identity.
    base=re.sub(r'_(?:NAUTH|DLY)$','',exchange)
    return base+':'+symbol==expected

def gate(page):
    if page.get_by_text('Verify you are human',exact=False).is_visible():return 'HUMAN_VERIFICATION_REQUIRED'
    if page.get_by_role('button',name='Join for free',exact=True).is_visible():return 'ORDINARY_LOGIN_REQUIRED'
    dialogs=page.get_by_role('dialog')
    for dialog in dialogs.all():
        if dialog.is_visible() and re.search(r'upgrade|historical data|bar limit',dialog.inner_text(),re.I):return 'HISTORY_PLAN_LIMIT'
    return None

def collect_stream(stream,parse_visible,retry_seconds):
    from playwright.sync_api import sync_playwright
    output=ROOT/stream.get('transportDirectory','data/'+stream['symbol']);output.mkdir(exist_ok=True,parents=True)
    hp=output/'health.json';old_health=json.loads(hp.read_text()) if hp.exists() else {}
    private=load_private_session() if stream.get('historyTargetBars',0) else None
    if not may_resume_authenticated(old_health,private):raise RuntimeError('ACCESS_BLOCKED_REQUIRES_OPERATOR')
    if old_health.get('nextAllowedUTC') and datetime.fromisoformat(old_health['nextAllowedUTC'])>datetime.now(UTC):
        return {'symbol':stream['symbol'],'state':'RATE_LIMIT_BACKOFF'}
    dest=output/'latest.json';prior=json.loads(dest.read_text()) if dest.exists() else {'rows':[]}
    history=dict(prior.get('history',{}));target=int(stream.get('historyTargetBars',0))
    first_authenticated=bool(private and old_health.get('authState')!='VERIFIED')
    if private and history.get('boundary')=='ORDINARY_LOGIN_REQUIRED':history['completed']=False
    bootstrap=target>0 and not history.get('completed') and not first_authenticated
    max_pages=int(stream.get('historyMaxPages',55)) if bootstrap else int(stream.get('recoveryMaxPages',8))
    deadline=time.monotonic()+int(stream.get('historyTimeBudgetSeconds',420))
    raw_samples=[];observed={};pages=0;boundary=None;displayed=None;delayed=False;market='NON_VERIFICATO';blocked=None
    with sync_playwright() as p:
        browser=p.chromium.launch()
        options={'viewport':{'width':1280,'height':900},'locale':'en-US','timezone_id':'UTC'}
        if private:options['storage_state']=private
        context=browser.new_context(**options)
        page=context.new_page();page.set_default_timeout(15000)
        try:
            response=page.goto('https://www.tradingview.com/chart/?symbol='+quote(stream['providerSymbol'],safe='')+'&interval=5',wait_until='domcontentloaded',timeout=45000)
            if response and response.status in (401,403,429):
                if response.status==429:
                    now=datetime.now(UTC);wait=retry_seconds(response.headers.get('retry-after',''),now)
                    health(output,'RATE_LIMIT',nextAllowedUTC=(now+timedelta(seconds=wait)).isoformat())
                else:health(output,'ACCESS_BLOCKED',httpStatus=response.status)
                raise RuntimeError('HTTP_ACCESS_OR_RATE_LIMIT_'+str(response.status))
            reject=page.get_by_role('button',name="Don't allow",exact=True)
            if reject.is_visible():reject.click()
            if private:
                try:page.get_by_role('button',name=re.compile(r'^Logged in as ')).wait_for(state='visible',timeout=15000)
                except Exception:raise RuntimeError('PRIVATE_SESSION_NOT_ACCEPTED_LOGIN_REQUIRED') from None
            page.get_by_role('button',name='Object tree and data window',exact=True).click()
            page.get_by_role('tab',name='Data window',exact=True).click()
            grid=page.get_by_role('treegrid');grid.wait_for(state='visible')
            charts=page.get_by_label(re.compile(r'^Chart for ')).all()
            chart=None
            for candidate in charts:
                label=candidate.get_attribute('aria-label')
                if delivery_identity(label,stream['providerSymbol']):chart=candidate;displayed=label;break
            if chart is None:
                labels=[c.get_attribute('aria-label') for c in charts]
                (output/'chart_labels.json').write_text(json.dumps(labels,indent=2))
                raise RuntimeError('CHART_IDENTITY_NOT_MATCHED_'+str(labels)[:200])
            box=chart.bounding_box()
            if not box or box['width']<300:raise RuntimeError('CHART_GEOMETRY_UNAVAILABLE')
            delayed=page.get_by_role('button',name=re.compile('Data is delayed')).count()>0
            market='CLOSED_DISPLAYED' if page.get_by_text('Market closed',exact=True).count() and page.get_by_text('Market closed',exact=True).first.is_visible() else 'NON_VERIFICATO'
            prior_min=min((r[0] for r in prior['rows']),default=None);prior_last=max((r[0] for r in prior['rows']),default=None);no_progress=0;oldest=None
            for page_index in range(max_pages):
                access=gate(page)
                if access:
                    boundary=access
                    if access!='HISTORY_PLAN_LIMIT':blocked=access
                    if page_index==0:raise RuntimeError(access)
                    break
                # Skip already archived middle windows during continuation. The
                # latest window is always read to recover new closed bars.
                skip=False
                if bootstrap and page_index and prior_min:
                    page.mouse.move(box['x']+28,box['y']+box['height']*0.65);page.wait_for_timeout(50)
                    try:
                        edge=parse_visible(grid.inner_text(),datetime.now(UTC))
                        skip=bool(edge and edge[0]>=prior_min)
                    except (ValueError,IndexError):pass
                if not skip:
                    before=min(observed,default=None)
                    for x in range(int(box['x']+box['width']-3),int(box['x']+25),-4):
                        access=gate(page)
                        if access:
                            boundary=access;blocked=access if access!='HISTORY_PLAN_LIMIT' else None;break
                        page.mouse.move(x,box['y']+box['height']*0.65);page.wait_for_timeout(40)
                        raw=grid.inner_text();captured=datetime.now(UTC)
                        raw_samples.append({'capturedAt':captured.isoformat(),'page':page_index,'x':x,'raw':public_ohlc_observation(raw) if private else raw})
                        try:row=parse_visible(raw,captured)
                        except (ValueError,IndexError):continue
                        if row:
                            existing=observed.get(row[0])
                            if existing and existing!=row:raise RuntimeError('INTRA_RUN_REVISION')
                            observed[row[0]]=row
                    pages+=1
                    if boundary:break
                    now_oldest=min(observed,default=None)
                    no_progress=no_progress+1 if page_index and before==now_oldest else 0
                    oldest=now_oldest
                    if no_progress>=2:boundary='NO_EARLIER_VISIBLE_BARS';break
                combined={r[0] for r in prior['rows']}|set(observed)
                if not bootstrap and prior_last and len(observed)>=12 and min(observed)<=prior_last:
                    boundary='INCREMENTAL_OVERLAP_REACHED';break
                if bootstrap and len(combined)>=target:boundary='BASIC_BAR_TARGET_REACHED';break
                if time.monotonic()>deadline:boundary='TIME_BUDGET_CONTINUE_NEXT_HOUR';break
                if page_index+1<max_pages:
                    y=box['y']+box['height']*0.65
                    page.mouse.move(box['x']+45,y);page.mouse.down()
                    page.mouse.move(box['x']+box['width']-40,y,steps=12);page.mouse.up();page.wait_for_timeout(250)
            if len(observed)<12:raise RuntimeError('LESS_THAN_TWELVE_VALID_CLOSED_BARS')
            if not private:page.screenshot(path=str(output/'success.png'))
            (output/'chart_identity.json').write_text(json.dumps({'requested':stream['providerSymbol'],'displayed':displayed,'title':page.title(),'delayedDisplayed':delayed},indent=2)+'\n')
        except Exception:
            if not private:page.screenshot(path=str(output/'failure.png'))
            raise
        finally:
            (output/'observations.json').write_text(json.dumps(raw_samples,indent=2)+'\n',encoding='utf-8')
            browser.close()
    merged={r[0]:r for r in prior['rows']};revisions=[]
    for t,row in observed.items():
        if t in merged and merged[t]!=row:revisions.append({'timestamp':t,'retained':merged[t],'observed':row})
        else:merged[t]=row
    rows=sorted(merged.values())[-int(stream.get('maxTransportBars',1500)):]
    captured=datetime.now(UTC);session=session_health(rows,stream,captured)
    if first_authenticated:
        history={**history,'completed':False,'boundary':'AUTHENTICATED_INCREMENTAL_PROOF_BEFORE_HISTORY',
                 'availableCapturedBars':len(rows),'firstUTC':rows[0][0]}
    elif bootstrap:
        completed=boundary not in ('TIME_BUDGET_CONTINUE_NEXT_HOUR',None)
        history={'requestedDays':stream.get('historyRequestDays',365),'planIntradayBarLimit':5000,'targetBars':target,'availableCapturedBars':len(rows),'scannedPagesThisRun':pages,'boundary':boundary or 'PAGE_BUDGET_CONTINUE_NEXT_HOUR','completed':completed,'firstUTC':rows[0][0]}
    result={'source':stream['source'],'providerSymbol':stream['providerSymbol'],'symbol':stream['symbol'],'timeframe':stream['timeframe'],'capturedAt':captured.isoformat(),'closureGraceSeconds':60,'method':'visible_data_window','authState':'VERIFIED' if private else 'PUBLIC_VISIBLE','instrumentNature':stream.get('instrumentNature','NON_VERIFICATO'),'valueUnit':stream.get('valueUnit','NON_VERIFICATO'),'referenceMarket':stream.get('referenceMarket'),'calendar':stream.get('calendar'),'marketStatus':market,'displayedChartIdentity':displayed,'delayedDisplayed':delayed,'sessionHealth':session,'history':history,'execution':{'event':os.getenv('GITHUB_EVENT_NAME','local'),'runId':os.getenv('GITHUB_RUN_ID')},'rows':rows,'observedClosedBars':len(observed),'gaps':gap_records(rows,stream),'revisions':revisions}
    tmp=output/'latest.pending.json';tmp.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');tmp.replace(dest)
    if blocked:health(output,'ACCESS_BLOCKED',reason=blocked,lastBarUTC=rows[-1][0],history=history)
    else:health(output,session['state'],**{k:v for k,v in session.items() if k!='state'},authState='VERIFIED' if private else 'PUBLIC_VISIBLE',runId=os.getenv('GITHUB_RUN_ID'),marketStatus=market,history=history)
    summary={'symbol':stream['symbol'],'observed':len(observed),'rows':len(rows),'first':rows[0][0],'last':rows[-1][0],'history':history,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),'state':'ACCESS_BLOCKED' if blocked else 'COLLECTED'}
    print(json.dumps(summary),flush=True)
    return summary

def safe_collect(stream,parse_visible,retry_seconds):
    output=ROOT/stream.get('transportDirectory','data/'+stream['symbol'])
    try:return collect_stream(stream,parse_visible,retry_seconds)
    except Exception as exc:
        hp=output/'health.json';old=json.loads(hp.read_text()) if hp.exists() else {}
        reason=str(exc)[:180]
        if stream.get('historyTargetBars',0) and os.getenv('TV_STORAGE_STATE_JSON') and not re.fullmatch(r'[A-Z][A-Z0-9_]{2,160}',reason):
            reason='PRIVATE_CAPTURE_ERROR_'+type(exc).__name__
        state='ACCESS_BLOCKED' if any(k in reason for k in ['LOGIN_REQUIRED','HUMAN_VERIFICATION','ACCESS_BLOCKED']) else 'ERROR_PREVIOUS_FEED_PRESERVED'
        if old.get('state') not in ('ACCESS_BLOCKED','RATE_LIMIT'):
            health(output,state,errorType=type(exc).__name__,reason=reason,lastBarUTC=old.get('lastBarUTC'))
        result={'symbol':stream['symbol'],'state':'FAILED_PREVIOUS_DATA_PRESERVED','error':reason};print(json.dumps(result),flush=True);return result

