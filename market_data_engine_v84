
"""V8.4 priority live scanner. Requires existing market_data_engine.py (V8.2)."""
import json
import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import pandas as pd
import streamlit as st
import upstox_client
from market_data_engine import (IST, NSEMarketData, get_fo_universe, parse_time,
    is_fresh, validate_nse_stock, upstox_market_status)


def now():
    return datetime.now(IST)


def num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def fresh(q, t):
    return all(is_fresh(q.get(k), 15, t) for k in ('trade', 'feed', 'received'))


def momentum(q):
    p, cp, op, hi, lo, vol = (num(q.get(k)) for k in ('ltp','cp','open','high','low','volume'))
    if any(x is None or x <= 0 for x in (p,cp,op,hi,lo,vol)) or not lo <= p <= hi or hi <= lo or not lo <= op <= hi:
        return 'NEUTRAL', 0.0
    change = 100*(p/cp-1)
    pos = 100*(p-lo)/(hi-lo)
    side = 'CE' if change >= .35 and pos >= 60 and p > op else 'PE' if change <= -.35 and pos <= 40 and p < op else 'NEUTRAL'
    return side, round(min(abs(change)/2.5,1)*60 + (pos if side=='CE' else 100-pos)*.4,2) if side!='NEUTRAL' else 0.0


def compare(q, v, t):
    reasons=[]
    if not fresh(q,t): reasons.append('STALE LIVE DATA')
    if not v.get('valid'): reasons.append(v.get('error') or 'NSE PENDING OR INVALID')
    if not is_fresh(v.get('checked'),90,t): reasons.append('VERIFICATION EXPIRED')
    if not is_fresh(v.get('timestamp'),180,t): reasons.append('STALE NSE DATA')
    a,b=parse_time(q.get('trade')),parse_time(v.get('timestamp'))
    gap=abs((a-b).total_seconds()) if a is not None and b is not None else None
    if gap is None or gap>90: reasons.append('SOURCE TIME GAP')
    p,n=num(q.get('ltp')),num(v.get('price'))
    diff=100*abs(p-n)/n if p is not None and n is not None and min(p,n)>0 else None
    if diff is None or diff>.35: reasons.append('PRICE MISMATCH OR MISSING')
    cp,np=num(q.get('cp')),num(v.get('previous'))
    if cp is None or np is None or min(cp,np)<=0 or 100*abs(cp-np)/np>.10: reasons.append('PREVIOUS CLOSE MISMATCH OR MISSING')
    return not reasons, ' | '.join(reasons), diff, gap


class Engine:
    def __init__(self, token, universe):
        self.token=token; self.universe=universe
        self.lock=threading.RLock(); self.halt=threading.Event()
        self.live={}; self.nse={}; self.attempt={}; self.errors=[]
        self.connected=False; self.market=False; self.status_at=0.; self.heartbeat=time.monotonic()
        self.streamer=None; self.thread=None; self.generation=0
        self.priority=set(); self.requests=0

    def error(self,e):
        with self.lock: self.errors=(self.errors+[str(e).replace(self.token,'[REDACTED]')])[-50:]

    def opened(self,*args):
        with self.lock:
            self.generation+=1; self.live.clear(); self.nse.clear(); self.attempt.clear(); self.connected=not self.halt.is_set()

    def closed(self,*args):
        with self.lock:
            self.generation+=1; self.connected=False; self.live.clear(); self.nse.clear()

    def message(self,msg):
        if self.halt.is_set(): return
        try:
            if isinstance(msg,str): msg=json.loads(msg)
            t=now()
            with self.lock:
                if not self.connected: return
                for key,f in msg.get('feeds',{}).items():
                    m=f.get('fullFeed',{}).get('marketFF',{})
                    l=m.get('ltpc') or f.get('ltpc') or {}
                    p,cp=num(l.get('ltp')),num(l.get('cp'))
                    trade=parse_time(l.get('ltt')); feed=parse_time(msg.get('currentTs'))
                    if p is None or cp is None or min(p,cp)<=0 or trade is None or feed is None or trade>feed: continue
                    old=self.live.get(key,{})
                    ot=parse_time(old.get('trade'))
                    if ot is not None and trade<ot: continue
                    d=next((x for x in m.get('marketOHLC',{}).get('ohlc',[]) if x.get('interval')=='1d'),{})
                    q=dict(old) if ot is not None and ot.date()==trade.date() else {}
                    q.update(ltp=p,cp=cp,trade=l['ltt'],feed=msg['currentTs'],received=t.isoformat())
                    for k in ('open','high','low'):
                        if num(d.get(k)) is not None: q[k]=num(d[k])
                    if num(m.get('vtt')) is not None: q['volume']=num(m['vtt'])
                    self.live[key]=q
        except Exception as e: self.error(e)

    def verify_group(self,symbols,generation):
        client=None
        try:
            client=NSEMarketData()
            for s in symbols:
                if self.halt.is_set(): break
                with self.lock:
                    if generation!=self.generation or not self.connected: break
                    self.attempt[s]=time.monotonic(); self.requests+=1
                try:
                    r=client.stock_quote(s); stock=r.get('stock',{})
                    if stock.get('symbol')!=s: raise ValueError('NSE symbol mismatch')
                    t=now(); v=validate_nse_stock(stock,180,t)
                    result=dict(valid=bool(v['NSE Valid']),error=str(v.get('Errors') or ''),price=v.get('NSE LTP'),previous=v.get('Previous Close'),timestamp=v.get('NSE Timestamp'),checked=t.isoformat())
                except Exception as e:
                    result=dict(valid=False,error='NSE REQUEST FAILED'); self.error(e)
                with self.lock:
                    if generation==self.generation and not self.halt.is_set(): self.nse[s]=result
                if self.halt.wait(0.35): break
        except Exception as e: self.error(e)
        finally:
            if client is not None:
                try: client.close()
                except Exception as e: self.error(e)

    def plan(self):
        t=now(); clock=time.monotonic()
        with self.lock:
            live=dict(self.live); nse=dict(self.nse); attempts=dict(self.attempt)
        sides={'CE':[],'PE':[]}
        for s,k in self.universe.items():
            q=live.get(k,{})
            if not fresh(q,t): continue
            side,score=momentum(q)
            if side in sides: sides[side].append((score,s))
        priority=[s for side in sides.values() for _,s in sorted(side,reverse=True)[:12]]
        with self.lock: self.priority=set(priority)
        def due(s):
            age=clock-attempts.get(s,-1e9)
            ok,*_=compare(live.get(self.universe[s],{}),nse.get(s,{}),t)
            return age >= (45 if ok else 15)
        urgent=[s for s in priority if due(s)]
        urgent.sort(key=lambda s:attempts.get(s,-1e9))
        background=[s for s in self.universe if s not in priority and due(s) and fresh(live.get(self.universe[s],{}),t)]
        background.sort(key=lambda s:attempts.get(s,-1e9))
        return urgent[:8]+background[:2]+urgent[8:10-len(background[:2])] if len(urgent)>=8 else urgent+background[:10-len(urgent)]

    def loop(self):
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                while not self.halt.is_set():
                    if time.monotonic()-self.heartbeat>120: break
                    if time.monotonic()-self.status_at>=15:
                        try:
                            status=upstox_market_status(self.token)
                            with self.lock: self.market=str(status.get('status','')).upper()=='NORMAL_OPEN'; self.status_at=time.monotonic()
                        except Exception as e:
                            with self.lock: self.market=False; self.status_at=time.monotonic()
                            self.error(e)
                    with self.lock: active=self.connected and self.market; gen=self.generation
                    if active:
                        batch=self.plan()
                        jobs=[pool.submit(self.verify_group,batch[i::2],gen) for i in range(2) if batch[i::2]]
                        for job in jobs: job.result()
                    self.halt.wait(2)
        except Exception as e: self.error(e)
        finally: self.stop()

    def start(self):
        cfg=upstox_client.Configuration(); cfg.access_token=self.token
        self.streamer=upstox_client.MarketDataStreamerV3(upstox_client.ApiClient(cfg),list(self.universe.values()),'full')
        for event,fn in [('open',self.opened),('close',self.closed),('error',self.error),('message',self.message)]: self.streamer.on(event,fn)
        self.streamer.auto_reconnect(True,5,10); self.streamer.connect()
        self.thread=threading.Thread(target=self.loop,daemon=True); self.thread.start()

    def stop(self):
        self.halt.set(); self.closed()
        with self.lock: self.market=False
        if self.streamer:
            try: self.streamer.auto_reconnect(False); self.streamer.disconnect()
            except Exception as e: self.error(e)

    def snapshot(self):
        self.heartbeat=time.monotonic(); t=now()
        with self.lock:
            live=dict(self.live); nse=dict(self.nse); priority=set(self.priority)
            active=self.connected and self.market and time.monotonic()-self.status_at<=45 and not self.halt.is_set()
        rows=[]
        for s,k in self.universe.items():
            q=live.get(k,{}); v=nse.get(s,{})
            f=active and fresh(q,t); ok,reason,diff,gap=compare(q,v,t); ok=bool(f and ok)
            side,score=momentum(q) if f else ('NEUTRAL',0.)
            p,cp=q.get('ltp'),q.get('cp')
            rows.append({'Symbol':s,'LTP':p,'Previous Close':cp,'Change %':100*(p/cp-1) if p and cp else None,'Volume':q.get('volume'),'Live Fresh':f,'Priority':s in priority,'Raw Direction':side,'Currently Cross Verified':ok,'Direction':side+' WATCH' if ok and side!='NEUTRAL' else 'WAIT','Momentum Score':score,'Price Difference %':diff,'Source Gap Seconds':gap,'Trade Time':q.get('trade'),'Feed Time':q.get('feed'),'NSE Timestamp':v.get('timestamp'),'NSE Verified IST':v.get('checked'),'Reasons':reason if active else 'MARKET OR CONNECTION NOT CONFIRMED'})
        return pd.DataFrame(rows)


def render():
    st.title('NSE F&O PRO V8.4 — PRIORITY LIVE ENGINE')
    token=st.sidebar.text_input('Upstox Access Token',type='password').strip()
    a,b=st.columns(2)
    if a.button('START LIVE ENGINE',type='primary'):
        if not token: st.error('Enter token')
        else:
            old=st.session_state.pop('v84',None)
            if old: old.stop()
            engine=None
            try:
                universe=get_fo_universe()
                if not universe: raise ValueError('Empty F&O universe')
                engine=Engine(token,universe); engine.start(); st.session_state.v84=engine
            except Exception as e:
                if engine: engine.stop()
                st.error(str(e).replace(token,'[REDACTED]'))
    if b.button('STOP LIVE ENGINE'):
        old=st.session_state.pop('v84',None)
        if old: old.stop()
    @st.fragment(run_every='3s')
    def dashboard():
        e=st.session_state.get('v84')
        if not e: st.info('Start the live engine'); return
        d=e.snapshot(); total=len(d); p=d[d.Priority]; verified=d[d['Currently Cross Verified']]
        st.write('WebSocket: '+('CONNECTED' if e.connected else 'DISCONNECTED'))
        st.write('Market: '+('NORMAL OPEN' if e.market else 'NOT CONFIRMED OPEN'))
        a,b,c=st.columns(3)
        a.metric('Live Feed Received',int(d.LTP.notna().sum())); b.metric('Currently Fresh',int(d['Live Fresh'].sum())); c.metric('Cross Verified',len(verified))
        a,b=st.columns(2); a.metric('Fresh Coverage',f'{100*d["Live Fresh"].mean():.2f}%'); b.metric('Priority Verified',f'{int(p["Currently Cross Verified"].sum())}/{len(p)}')
        st.caption(f'Universe {total} | NSE requests {e.requests} | {now().isoformat()}')
        st.subheader('Final Qualified Calls'); st.info('Not generated by this data engine. Technical, news, option and risk gates remain mandatory.')
        cols=['Symbol','LTP','Previous Close','Change %','Momentum Score','Price Difference %','Source Gap Seconds']
        for side in ('CE','PE'):
            st.subheader('Live Verified '+side+' Watchlist')
            st.dataframe(d[d.Direction==side+' WATCH'].sort_values('Momentum Score',ascending=False)[cols].head(10),hide_index=True,use_container_width=True)
        with st.expander('Priority candidates including blocked'): st.dataframe(p,hide_index=True)
        with st.expander('Complete Live Market Data'): st.dataframe(d,hide_index=True)
        with st.expander('Engine Errors'): st.write(list(e.errors))
        st.download_button('DOWNLOAD LIVE VALIDATION CSV',d.to_csv(index=False).encode('utf-8-sig'),'v84_live_validation.csv','text/csv')
    dashboard()
    st.caption('Research only. No orders. Keep session active; idle engine stops after approximately two minutes, subject to outstanding requests.')

if __name__=='__main__':
    st.set_page_config(page_title='NSE F&O V8.4',layout='wide')
    render()
