import os
import gzip
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import numpy as np
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='NSE Options | Chain & Trend', page_icon='📊', layout='wide')
IST = ZoneInfo('Asia/Kolkata')
API = 'https://api.upstox.com'
MASTER = 'https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz'
INDEXES = {'NIFTY 50':'NSE_INDEX|Nifty 50','BANK NIFTY':'NSE_INDEX|Nifty Bank','FIN NIFTY':'NSE_INDEX|Nifty Fin Service'}


def n(v, default=np.nan):
    try:
        x = float(v)
        return x if np.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def request_data(path, token, params=None):
    r = requests.get(API + path, params=params, headers={'Authorization':f'Bearer {token}', 'Accept':'application/json'}, timeout=25)
    if not r.ok:
        raise RuntimeError(f'Upstox HTTP {r.status_code}: {r.text[:220]}')
    data = r.json()
    if data.get('status') != 'success':
        raise RuntimeError(str(data.get('errors') or data.get('message') or 'API error'))
    return data.get('data') or {}


@st.cache_data(ttl=86400, show_spinner='Loading NSE instrument list...')
def instruments():
    r = requests.get(MASTER, timeout=40)
    r.raise_for_status()
    items = json.loads(gzip.decompress(r.content))
    rows = []
    for x in items:
        if x.get('segment') != 'NSE_EQ':
            continue
        key = x.get('instrument_key')
        symbol = x.get('trading_symbol') or x.get('tradingsymbol')
        if key and symbol:
            rows.append({'symbol':str(symbol), 'name':str(x.get('name') or symbol), 'key':key})
    return pd.DataFrame(rows).drop_duplicates('key').sort_values('symbol')


@st.cache_data(ttl=86400, show_spinner='Loading F&O universe...')
def fo_universe():
    r = requests.get(MASTER, timeout=45)
    r.raise_for_status()
    items = json.loads(gzip.decompress(r.content))
    equities = {}
    for x in items:
        if x.get('segment') == 'NSE_EQ' and x.get('instrument_key'):
            sym = str(x.get('trading_symbol') or x.get('tradingsymbol') or '').upper()
            if sym:
                equities[x['instrument_key']] = {'symbol':sym,'key':x['instrument_key']}
    option_underlyings = set()
    option_names = set()
    for x in items:
        if x.get('segment') != 'NSE_FO':
            continue
        typ = str(x.get('instrument_type') or '').upper()
        if typ not in ('CE','PE'):
            continue
        ukey = x.get('underlying_key')
        if ukey:
            option_underlyings.add(ukey)
        uname = str(x.get('name') or x.get('underlying_symbol') or '').upper()
        if uname:
            option_names.add(uname)
    found = [v for k,v in equities.items() if k in option_underlyings or v['symbol'] in option_names]
    if not found:
        raise RuntimeError('F&O underlying mapping missing in instrument master. Scanner will not assume all equities are F&O.')
    return pd.DataFrame(found).drop_duplicates('key').sort_values('symbol').reset_index(drop=True)


def scan_quotes(token, universe):
    # Snapshot scan: Upstox full quotes in batches, not a full 5-minute candle scan.
    rows=[]; failures=[]
    keys=universe.key.tolist()
    for start in range(0,len(keys),40):
        batch=keys[start:start+40]
        try:
            response=request_data('/v2/market-quote/quotes',token,{'instrument_key':','.join(batch)})
            for item in response.values():
                if not isinstance(item,dict):
                    continue
                key=item.get('instrument_token') or item.get('instrument_key')
                if not key:
                    continue
                last=n(item.get('last_price'))
                ohlc=item.get('ohlc') or {}
                prev=n(ohlc.get('close'))
                day_open=n(ohlc.get('open'))
                high=n(ohlc.get('high'))
                low=n(ohlc.get('low'))
                volume=n(item.get('volume'),0)
                if not (np.isfinite(last) and np.isfinite(prev) and prev>0):
                    continue
                pct=100*(last/prev-1)
                range_pos=100*(last-low)/(high-low) if np.isfinite(high) and np.isfinite(low) and high>low else np.nan
                open_pct=100*(last/day_open-1) if np.isfinite(day_open) and day_open>0 else np.nan
                rows.append({'key':key,'LTP':last,'Change %':pct,'From Open %':open_pct,'Range Position %':range_pos,'Volume':volume})
        except Exception as exc:
            failures.append(str(exc)[:110])
    if not rows:
        raise RuntimeError('No usable quotes returned. '+('; '.join(failures[:2]) if failures else ''))
    frame=pd.DataFrame(rows).drop_duplicates('key')
    frame=universe.merge(frame,on='key',how='inner')
    if frame.empty:
        raise RuntimeError('Quote instrument keys did not match F&O underlying keys.')
    # Transparent, snapshot-only momentum ranking; not a validated BUY signal.
    frame['Direction']=np.where((frame['Change %']>0.35)&(frame['From Open %']>0), 'CE WATCH',
                        np.where((frame['Change %']< -0.35)&(frame['From Open %']<0),'PE WATCH','NEUTRAL'))
    strength=np.minimum(frame['Change %'].abs()/2.5,1)*60
    position=np.where(frame.Direction=='CE WATCH',frame['Range Position %'],
             np.where(frame.Direction=='PE WATCH',100-frame['Range Position %'],50))
    frame['Score / 100']=(strength+np.clip(position,0,100)*0.4).clip(0,100).round(0).astype(int)
    frame.loc[frame.Direction=='NEUTRAL','Score / 100']=0
    frame=frame.sort_values(['Score / 100','Volume'],ascending=[False,False]).reset_index(drop=True)
    frame.insert(0,'Rank',np.arange(1,len(frame)+1))
    return frame,failures


def contracts(token, key):
    raw = request_data('/v2/option/contract', token, {'instrument_key':key})
    if not isinstance(raw, list):
        raise RuntimeError('Unexpected option contract response')
    today = datetime.now(IST).date()
    dates = set()
    for item in raw:
        d = item.get('expiry')
        if d:
            try:
                if datetime.strptime(d, '%Y-%m-%d').date() >= today:
                    dates.add(d)
            except ValueError:
                pass
    return sorted(dates)


def option_chain(token, key, expiry):
    raw = request_data('/v2/option/chain', token, {'instrument_key':key, 'expiry_date':expiry})
    rows = []
    for x in raw:
        for side, field in [('CE','call_options'),('PE','put_options')]:
            o = x.get(field) or {}
            m, g = o.get('market_data') or {}, o.get('option_greeks') or {}
            bid, ask = n(m.get('bid_price')), n(m.get('ask_price'))
            spread = 100*(ask-bid)/((ask+bid)/2) if np.isfinite(bid) and np.isfinite(ask) and ask >= bid > 0 else np.nan
            oi, prev = n(m.get('oi'),0), n(m.get('prev_oi'),0)
            rows.append({'strike':n(x.get('strike_price')),'side':side,'spot':n(x.get('underlying_spot_price')),
                'ltp':n(m.get('ltp')),'oi':oi,'prev_oi':prev,'oi_change':oi-prev,
                'volume':n(m.get('volume'),0),'bid':bid,'ask':ask,'spread_pct':spread,
                'iv':n(g.get('iv')),'delta':n(g.get('delta')),'gamma':n(g.get('gamma')),
                'theta':n(g.get('theta')),'vega':n(g.get('vega')),
                'instrument':o.get('instrument_key') or ''})
    return pd.DataFrame(rows)


def candidate_list(df, side, old, old_time, now, min_vol, max_spread, max_iv, rr):
    spot = n(df.spot.dropna().iloc[0]) if df.spot.notna().any() else np.nan
    strikes = sorted(df.strike.dropna().unique())
    gaps = [b-a for a,b in zip(strikes, strikes[1:]) if b>a]
    step = min(gaps) if gaps else 50
    subset = df[(df.side==side) & ((df.strike-spot).abs()<=3*step) & (df.ltp>0)]
    opp = df[df.side==('PE' if side=='CE' else 'CE')]
    wall = opp[(opp.strike<=spot) & (opp.strike>=spot-2*step)] if side=='CE' else opp[(opp.strike>=spot) & (opp.strike<=spot+2*step)]
    fresh = old_time is not None and 15 <= (now-old_time).total_seconds() <= 180
    out=[]
    for _,r in subset.iterrows():
        prev = old.get(r.instrument) if fresh else None
        premium_up = prev is not None and r.ltp > prev
        checks = {
            'Opposite OI change vs previous close':wall.oi_change.sum()>0,
            'Selected OI change vs previous close':r.oi_change>0,
            'Volume':r.volume>=min_vol and r.volume>0,
            'Bid/ask spread':np.isfinite(r.spread_pct) and 0<=r.spread_pct<=max_spread,
            'Delta':np.isfinite(r.delta) and 0.35<=abs(r.delta)<=0.75,
            'IV':np.isfinite(r.iv) and 0<r.iv<=max_iv,
            'Premium up vs recent snapshot':premium_up,
        }
        entry = r.ask if np.isfinite(r.ask) and r.ask>0 else r.ltp
        risk = max(entry*0.12,1.0)
        out.append({'strike':r.strike,'side':side,'score':sum(checks.values()),'ltp':r.ltp,
                    'delta':r.delta,'iv':r.iv,'spread':r.spread_pct,'oi':r.oi,'volume':r.volume,
                    'premium_up':premium_up,'fresh':fresh,'entry':entry,'sl':max(0.05,entry-risk),
                    'target':entry+risk*rr,'checks':checks,'distance':abs(r.strike-spot)})
    return sorted(out,key=lambda x:(-x['score'],x['distance']))[:3]


def parse_candles(payload):
    raw = payload.get('candles',[]) if isinstance(payload,dict) else []
    records=[]
    for c in raw:
        try:
            records.append([pd.to_datetime(c[0],utc=True).tz_convert(IST),*[float(v) for v in c[1:6]]])
        except (IndexError, ValueError, TypeError):
            continue
    if not records:
        return pd.DataFrame()
    return (pd.DataFrame(records,columns=['timestamp','open','high','low','close','volume'])
            .drop_duplicates('timestamp').sort_values('timestamp').set_index('timestamp'))


def candle_data(token,key,now):
    encoded=quote(key,safe='')
    today=now.date()
    previous=(today-timedelta(days=12)).isoformat()
    yesterday=(today-timedelta(days=1)).isoformat()
    # Historical V3 supplies previous sessions to initialize EMA/RSI/ADX.
    historical = request_data(f'/v3/historical-candle/{encoded}/minutes/1/{yesterday}/{previous}',token)
    history=parse_candles(historical)
    # Current-session V2 endpoint may return empty for some symbols/accounts.
    intraday=request_data(f'/v2/historical-candle/intraday/{encoded}/1minute',token)
    current=parse_candles(intraday)
    return history,current


def trend_calc(history,current,now):
    if current.empty:
        return None,'Current-session 1-minute candles unavailable'
    today=now.date()
    current=current[current.index.date==today]
    if current.empty:
        return None,'No candles from today'
    if now.time()<time(9,30):
        return {'direction':'OPENING / WAIT','confidence':'LOW'},None
    if history.empty:
        return None,'Previous-session history unavailable'
    merged=pd.concat([history,current]).sort_index()
    merged=merged[~merged.index.duplicated(keep='last')].between_time('09:15','15:29')
    counts=merged.close.resample('5min').count()
    bars=merged.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
    bars=bars[(counts==5) & (bars.index+pd.Timedelta(minutes=5)<=pd.Timestamp(now))].dropna(subset=['close'])
    if len(bars)<40:
        return None,f'Only {len(bars)} complete 5-minute bars (need 40 including history)'
    bars['ema9']=bars.close.ewm(span=9,adjust=False).mean()
    bars['ema20']=bars.close.ewm(span=20,adjust=False).mean()
    change=bars.close.diff()
    gain=change.clip(lower=0).ewm(alpha=1/9,adjust=False,min_periods=9).mean()
    loss=(-change.clip(upper=0)).ewm(alpha=1/9,adjust=False,min_periods=9).mean()
    bars['rsi']=100-100/(1+gain/loss.replace(0,np.nan))
    bars.loc[(loss==0)&(gain>0),'rsi']=100
    bars.loc[(gain==0)&(loss>0),'rsi']=0
    up=bars.high.diff(); down=-bars.low.diff()
    pdm=pd.Series(np.where((up>down)&(up>0),up,0),index=bars.index)
    mdm=pd.Series(np.where((down>up)&(down>0),down,0),index=bars.index)
    tr=pd.concat([bars.high-bars.low,(bars.high-bars.close.shift()).abs(),(bars.low-bars.close.shift()).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    plus=100*pdm.ewm(alpha=1/14,adjust=False,min_periods=14).mean()/atr.replace(0,np.nan)
    minus=100*mdm.ewm(alpha=1/14,adjust=False,min_periods=14).mean()/atr.replace(0,np.nan)
    dx=100*(plus-minus).abs()/(plus+minus).replace(0,np.nan)
    bars['adx']=dx.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    bars['plus']=plus;bars['minus']=minus
    today_bars=bars[bars.index.date==today]
    if len(today_bars)<3:
        return {'direction':'OPENING / WAIT','confidence':'LOW'},None
    last=today_bars.iloc[-1]
    if now-last.name.to_pydatetime()>timedelta(minutes=12):
        return None,'Latest completed 5-minute candle is stale'
    opening=today_bars.iloc[:3]
    opening_high=opening.high.max();opening_low=opening.low.min()
    bull=last.close>last.ema9>last.ema20 and last.rsi>52
    bear=last.close<last.ema9<last.ema20 and last.rsi<48
    direction='BULLISH' if bull else 'BEARISH' if bear else 'NEUTRAL'
    strong=np.isfinite(last.adx) and last.adx>=25 and ((bull and last.plus>last.minus) or (bear and last.minus>last.plus))
    confidence='EARLY / LOW' if len(today_bars)<=4 else 'STRONG' if strong else 'MODERATE' if direction!='NEUTRAL' else 'MIXED'
    valid_minutes=current[current.index+pd.Timedelta(minutes=1)<=pd.Timestamp(now)]
    volume=valid_minutes.volume.clip(lower=0)
    vwap=(((valid_minutes.high+valid_minutes.low+valid_minutes.close)/3)*volume).sum()/volume.sum() if volume.sum()>0 else np.nan
    return {'direction':direction,'confidence':confidence,'close':last.close,'ema9':last.ema9,'ema20':last.ema20,
            'rsi':last.rsi,'adx':last.adx,'vwap':vwap,'opening_high':opening_high,'opening_low':opening_low,
            'bar':str(last.name),'bars_today':len(today_bars)},None


st.title('NSE Options | Option Chain + Independent Trend')
st.caption('NIFTY / BANK NIFTY / NSE stock options • Research only • No automatic orders')
with st.sidebar:
    token=st.text_input('Upstox Algo Trading Access Token',value=os.getenv('UPSTOX_ACCESS_TOKEN',''),type='password')
    category=st.radio('Instrument type',['Index Options','Stock Options'])
    if category=='Index Options':
        name=st.selectbox('Index',list(INDEXES))
        key=INDEXES[name]
    else:
        try:
            universe=instruments()
            selected=st.selectbox('Search NSE stock',universe.index.tolist(),format_func=lambda i:f"{universe.loc[i,'symbol']} — {universe.loc[i,'name']}")
            name=universe.loc[selected,'symbol'];key=universe.loc[selected,'key']
        except Exception as exc:
            st.error(f'Could not load NSE stocks: {exc}')
            st.stop()
    st.caption('Only stocks with available option contracts will be supported.')
    expiry_choice=st.selectbox('Expiry',['Auto: nearest available','Enter manually'])
    expiry_manual=st.text_input('Expiry YYYY-MM-DD',value='2026-11-23') if expiry_choice=='Enter manually' else ''
    st.header('Option Chain filters')
    min_vol=st.number_input('Minimum option volume',min_value=0,value=10000,step=1000)
    max_spread=st.slider('Maximum bid/ask spread %',0.5,10.0,3.0,0.5)
    max_iv=st.slider('Maximum IV %',10,100,40)
    min_score=st.slider('BUY screening score / 7',4,7,6)
    rr=st.slider('Illustrative reward/risk',1.0,3.0,1.75,0.25)
    auto=st.toggle('Auto-refresh every 30 seconds',value=False)

for field,default in [('identity',''),('prior',{}),('prior_time',None)]:
    if field not in st.session_state:st.session_state[field]=default

@st.fragment(run_every='30s' if auto else None)
def dashboard():
    clicked=st.button('Fetch / Refresh Now',type='primary')
    if not token:
        st.info('Enter your token in the sidebar. Never share it in chat or screenshots.');return
    if not auto and not clicked:
        st.info('Click Fetch / Refresh Now or enable auto-refresh.');return
    now=datetime.now(IST)
    open_window=now.weekday()<5 and time(9,15)<=now.time()<=time(15,30)
    st.caption(f'Last fetch: {now:%d %b %Y %I:%M:%S %p} IST')
    if not open_window:st.warning('Outside regular NSE hours. Live BUY disabled; quotes may be stale.')
    st.header('0. F&O STOCK OPPORTUNITY SCANNER')
    st.caption('Snapshot momentum ranking from stock quotes, NOT confirmed 5-minute breakout or option BUY signals. Live data only during market hours.')
    if st.button('Scan all available F&O stocks',key='scan_fo'):
        try:
            universe_fo=fo_universe()
            with st.spinner(f'Scanning {len(universe_fo)} F&O underlyings...'):
                ranking,errors=scan_quotes(token,universe_fo)
            st.session_state['scanner_ranking']=ranking
            st.session_state['scanner_time']=now
            st.session_state['scanner_errors']=errors
        except Exception as exc:
            st.error(f'Scanner unavailable: {exc}')
    ranking=st.session_state.get('scanner_ranking')
    if ranking is not None and not ranking.empty:
        scan_time=st.session_state.get('scanner_time')
        st.caption(f'Snapshot time: {scan_time:%d %b %Y %I:%M:%S %p} IST | {len(ranking)} stocks with usable quotes')
        if not open_window:st.warning('Market closed: ranking is historical/stale, not a live opportunity.')
        if st.session_state.get('scanner_errors'):
            st.warning(f"Some quote batches failed ({len(st.session_state['scanner_errors'])}); ranking may be incomplete.")
        display=['Rank','symbol','Direction','Score / 100','LTP','Change %','From Open %','Range Position %','Volume']
        st.subheader('Top 10 CE / PE Watchlist')
        leaders=ranking[ranking.Direction!='NEUTRAL'].head(10)
        st.dataframe(leaders[display],hide_index=True,use_container_width=True)
        st.subheader('Full F&O Ranking')
        filter_choice=st.selectbox('Ranking filter',['All','CE WATCH','PE WATCH','NEUTRAL'],key='rank_filter')
        filtered=ranking if filter_choice=='All' else ranking[ranking.Direction==filter_choice]
        st.dataframe(filtered[display],hide_index=True,use_container_width=True)
        st.download_button('Download ranking CSV',ranking[display].to_csv(index=False),file_name='fo_scanner_ranking.csv',mime='text/csv')
        st.info('To inspect a ranked stock option chain, select Stock Options and its symbol in the sidebar, then Fetch / Refresh Now.')
    else:
        st.info('Press Scan all available F&O stocks to create a ranking. The scan is manual to avoid unnecessary API requests.')
    st.divider()
    st.header('1. OPTION CHAIN + GREEKS — INDEPENDENT')
    chain=pd.DataFrame(); ce=[];pe=[]
    try:
        expiries=contracts(token,key)
        if not expiries:raise RuntimeError('No currently listed option contracts for this stock/index. Choose an F&O instrument.')
        expiry=expiries[0] if expiry_choice=='Auto: nearest available' else expiry_manual.strip()
        if expiry not in expiries:raise RuntimeError(f'Expiry {expiry} not available. Available: {", ".join(expiries[:8])}')
        identity=f'{key}:{expiry}'
        if identity!=st.session_state.identity:
            st.session_state.identity=identity;st.session_state.prior={};st.session_state.prior_time=None
        chain=option_chain(token,key,expiry)
        if chain.empty:raise RuntimeError('Option chain returned no rows')
        st.caption(f'Instrument: {name} | Expiry: {expiry}')
        spot=n(chain.spot.dropna().iloc[0]) if chain.spot.notna().any() else np.nan
        ce_oi=chain.loc[chain.side=='CE','oi'].sum();pe_oi=chain.loc[chain.side=='PE','oi'].sum()
        a,b,c,d=st.columns(4)
        a.metric('Spot',f'{spot:,.2f}' if np.isfinite(spot) else 'N/A')
        b.metric('PCR',f'{pe_oi/ce_oi:.2f}' if ce_oi>0 else 'N/A')
        c.metric('CE OI',f'{ce_oi:,.0f}');d.metric('PE OI',f'{pe_oi:,.0f}')
        prior=st.session_state.prior if open_window else {}
        prior_time=st.session_state.prior_time if open_window else None
        ce=candidate_list(chain,'CE',prior,prior_time,now,min_vol,max_spread,max_iv,rr)
        pe=candidate_list(chain,'PE',prior,prior_time,now,min_vol,max_spread,max_iv,rr)
        left,right=st.columns(2)
        for col,label,items in [(left,'CALL (CE)',ce),(right,'PUT (PE)',pe)]:
            with col:
                st.subheader(label+' Top 3')
                if not items:
                    st.info('No near-ATM candidate with positive premium');continue
                rows=[{'Strike':f"{x['strike']:g} {x['side']}",'Score':f"{x['score']}/7",'LTP':x['ltp'],
                       'Delta':x['delta'],'IV':x['iv'],'Spread %':x['spread'],
                       'Entry*':round(x['entry'],2),'SL*':round(x['sl'],2),'Target*':round(x['target'],2)} for x in items]
                st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
                best=items[0]
                status='BUY CANDIDATE' if open_window and best['fresh'] and best['premium_up'] and best['score']>=min_score else 'WAIT'
                st.write(f"**Chain status:** {status} | **Best:** {best['strike']:g} {best['side']} ({best['score']}/7)")
                with st.expander('Why this score?'):
                    for check,passed in best['checks'].items():
                        st.write(('✅ ' if passed else '❌ ')+check)
        with st.expander('Full Option Chain + Greeks'):
            view=chain.drop(columns=['instrument'])
            st.dataframe(view,hide_index=True,use_container_width=True)
            st.download_button('Download CSV',view.to_csv(index=False),file_name='option_chain.csv',mime='text/csv')
        if open_window:
            st.session_state.prior={r.instrument:r.ltp for _,r in chain.iterrows() if r.instrument and np.isfinite(r.ltp) and r.ltp>0}
            st.session_state.prior_time=now
        else:
            st.session_state.prior={};st.session_state.prior_time=None
    except Exception as exc:
        st.error(f'Option Chain unavailable: {exc}')
    st.caption('*Entry/SL/Target are illustrative option-premium levels, not validated trading levels. OI change is vs previous close.')
    st.divider()
    st.header('2. MARKET / STOCK TREND — INDEPENDENT')
    trend=None
    if not open_window:
        st.info('TREND: UNAVAILABLE — market closed')
    else:
        try:
            history,current=candle_data(token,key,now)
            trend,error=trend_calc(history,current,now)
            if error:st.warning('TREND: UNAVAILABLE — '+error)
            elif trend:
                status=trend['direction']
                if status=='BULLISH':st.success(f"TREND: {status} | {trend['confidence']}")
                elif status=='BEARISH':st.error(f"TREND: {status} | {trend['confidence']}")
                else:st.info(f"TREND: {status} | {trend['confidence']}")
                if 'close' in trend:
                    a,b,c,d=st.columns(4)
                    a.metric('Last 5m close',f"{trend['close']:.2f}")
                    b.metric('EMA 9',f"{trend['ema9']:.2f}")
                    c.metric('EMA 20',f"{trend['ema20']:.2f}")
                    d.metric('RSI 9',f"{trend['rsi']:.1f}")
                    st.write(f"ADX: {trend['adx']:.1f} | VWAP: {trend['vwap']:.2f}" if np.isfinite(trend['vwap']) else f"ADX: {trend['adx']:.1f} | VWAP unavailable (index volume may be zero)")
                    st.write(f"Opening high: {trend['opening_high']:.2f} | Opening low: {trend['opening_low']:.2f}")
                    st.caption(f"Last completed bar: {trend['bar']}")
        except Exception as exc:
            st.warning(f'TREND: UNAVAILABLE — {exc}')
    st.divider()
    st.header('3. SIGNAL AGREEMENT — INFORMATION ONLY')
    if not ce or not pe:st.info('Chain comparison unavailable')
    elif not trend or 'close' not in trend:st.info('Trend unavailable; CE/PE chain candidates above remain unblocked')
    else:
        bias='CE LEADING' if ce[0]['score']>pe[0]['score'] else 'PE LEADING' if pe[0]['score']>ce[0]['score'] else 'NO DIRECTIONAL EDGE'
        direction=trend['direction']
        st.write(f'Chain score comparison: **{bias}** | Price trend: **{direction}**')
        if bias=='NO DIRECTIONAL EDGE':st.info('Equal scores — no directional preference from chain score')
        elif (bias=='CE LEADING' and direction=='BULLISH') or (bias=='PE LEADING' and direction=='BEARISH'):
            st.success('ALIGNED (informational only)')
        elif direction=='NEUTRAL':st.info('MIXED — neutral trend')
        else:st.warning('CONFLICT — chain and trend disagree; chain signals are not blocked')
    st.warning('Research only; heuristic signals are not backtested or guaranteed. No orders are placed. Weekday/time check does not verify exchange holidays.')

dashboard()