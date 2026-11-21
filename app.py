
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='NIFTY F&O | Independent Chain & Trend', page_icon='📊', layout='wide')
IST = ZoneInfo('Asia/Kolkata')
BASE = 'https://api.upstox.com/v2'


def num(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def get_data(path, token, params=None):
    res = requests.get(BASE + path, params=params, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'}, timeout=20)
    if res.status_code != 200:
        raise RuntimeError(f'Upstox HTTP {res.status_code}: {res.text[:240]}')
    payload = res.json()
    if payload.get('status') != 'success':
        raise RuntimeError(str(payload.get('errors') or payload.get('message') or 'API error'))
    return payload.get('data') or []


def unpack(raw):
    rows = []
    for item in raw:
        strike = num(item.get('strike_price'))
        spot = num(item.get('underlying_spot_price'))
        for side, key in [('CE', 'call_options'), ('PE', 'put_options')]:
            opt = item.get(key) or {}
            md = opt.get('market_data') or {}
            gr = opt.get('option_greeks') or {}
            oi = num(md.get('oi'))
            prev = num(md.get('prev_oi'))
            bid, ask = num(md.get('bid_price')), num(md.get('ask_price'))
            spread = (ask - bid) / ((ask + bid) / 2) * 100 if ask >= bid > 0 else None
            rows.append({'strike': strike, 'side': side, 'spot': spot,
                         'ltp': num(md.get('ltp')), 'oi': oi, 'prev_oi': prev,
                         'oi_change': oi - prev, 'volume': num(md.get('volume')),
                         'bid': bid, 'ask': ask, 'spread_pct': spread,
                         'iv': num(gr.get('iv'), None), 'delta': num(gr.get('delta'), None),
                         'gamma': num(gr.get('gamma'), None), 'theta': num(gr.get('theta'), None),
                         'vega': num(gr.get('vega'), None),
                         'instrument': opt.get('instrument_key') or ''})
    return pd.DataFrame(rows)


def market_open(now):
    return now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 30)


def chain_candidates(df, side, spread_max, iv_max, vol_min, rr, previous):
    if df.empty:
        return []
    spot = num(df.spot.iloc[0])
    strikes = sorted(df.strike.unique())
    steps = [b-a for a, b in zip(strikes[:-1], strikes[1:]) if b > a]
    step = min(steps) if steps else 50
    subset = df[(df.side == side) & (abs(df.strike-spot) <= step*3)].copy()
    opposite = df[df.side == ('PE' if side == 'CE' else 'CE')]
    results = []
    for _, r in subset.iterrows():
        good, bad = [], []
        wall = opposite[(opposite.strike <= spot) if side == 'CE' else (opposite.strike >= spot)]
        wall = wall[abs(wall.strike-spot) <= step*2]
        checks = [
            (wall.oi_change.sum() > 0, 'Opposite-side OI buildup supports this side'),
            (r.oi_change > 0, 'Selected option OI higher than previous close'),
            (r.volume >= vol_min and r.volume > 0, 'Volume threshold passed'),
            (r.spread_pct is not None and 0 <= r.spread_pct <= spread_max, 'Bid/ask spread acceptable'),
            (r.delta is not None and 0.35 <= abs(r.delta) <= 0.75, 'Delta in preferred range'),
            (r.iv is not None and 0 < r.iv <= iv_max, 'IV below ceiling'),
        ]
        old = previous.get(r.instrument) if r.instrument else None
        checks.append((old is not None and r.ltp > old, 'Premium increased since previous live snapshot'))
        for passed, description in checks:
            (good if passed else bad).append(description)
        entry = r.ask if r.ask > 0 else r.ltp
        risk = max(entry * 0.12, 1.0)
        results.append({'row': r, 'score': len(good), 'good': good, 'bad': bad,
                        'entry': entry, 'sl': max(0.05, entry-risk), 'target': entry+risk*rr})
    return sorted(results, key=lambda x: (x['score'], -abs(x['row'].strike-spot)), reverse=True)[:3]


def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def rsi(series, length=9):
    diff = series.diff()
    up = diff.clip(lower=0).ewm(alpha=1/length, adjust=False).mean()
    down = (-diff.clip(upper=0)).ewm(alpha=1/length, adjust=False).mean()
    rs = up / down.replace(0, float('nan'))
    return (100 - 100/(1+rs)).fillna(50)


def adx_di(frame, length=14):
    high, low, close = frame.high, frame.low, frame.close
    plus_move = high.diff()
    minus_move = -low.diff()
    plus_dm = plus_move.where((plus_move > minus_move) & (plus_move > 0), 0.0)
    minus_dm = minus_move.where((minus_move > plus_move) & (minus_move > 0), 0.0)
    tr = pd.concat([(high-low), (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/length, adjust=False).mean().replace(0, float('nan'))
    plus_di = 100*plus_dm.ewm(alpha=1/length, adjust=False).mean()/atr
    minus_di = 100*minus_dm.ewm(alpha=1/length, adjust=False).mean()/atr
    dx = 100*(plus_di-minus_di).abs()/(plus_di+minus_di).replace(0, float('nan'))
    adx = dx.ewm(alpha=1/length, adjust=False).mean()
    return plus_di, minus_di, adx


def fetch_trend(token, instrument_key, now):
    # Upstox V2 intraday accepts 1minute and 30minute, not 5minute.
    # Resample 1-minute candles into 5-minute bars, without inventing missing data.
    raw = get_data(f'/historical-candle/intraday/{requests.utils.quote(instrument_key, safe="")}/1minute', token)
    candles = raw.get('candles', []) if isinstance(raw, dict) else []
    if not candles:
        return None, 'Upstox returned no intraday 1-minute candles.'
    data = []
    for c in candles:
        if len(c) >= 6:
            data.append([pd.to_datetime(c[0], utc=True).tz_convert(IST), *[num(v) for v in c[1:6]]])
    if not data:
        return None, 'No usable candle records.'
    frame = pd.DataFrame(data, columns=['ts','open','high','low','close','volume']).set_index('ts').sort_index()
    frame = frame[~frame.index.duplicated(keep='last')]
    # Keep only completed 5-minute candles, aligned to IST 09:15 market open.
    five = frame.resample('5min', origin='start_day', offset='0min', label='left', closed='left').agg(
        {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
    current_bucket = pd.Timestamp(now).floor('5min')
    five = five[five.index < current_bucket]
    if len(five) < 35:
        return None, f'Only {len(five)} completed 5-minute bars; need at least 35.'
    if five.index[-1].date() != now.date():
        return None, 'Candles are not from the current market day.'
    five['ema9'] = ema(five.close, 9)
    five['ema20'] = ema(five.close, 20)
    five['rsi9'] = rsi(five.close, 9)
    plus_di, minus_di, adx = adx_di(five)
    five['plus_di'], five['minus_di'], five['adx'] = plus_di, minus_di, adx
    # Intraday VWAP must reset each session; this endpoint returns today's bars.
    typical = (five.high+five.low+five.close)/3
    cumulative_volume = five.volume.cumsum()
    five['vwap'] = (typical*five.volume).cumsum()/cumulative_volume.replace(0, float('nan'))
    last = five.iloc[-1]
    bull = (last.close > last.ema9 > last.ema20 and last.close > last.vwap
            and last.rsi9 > 55 and last.adx >= 20 and last.plus_di > last.minus_di)
    bear = (last.close < last.ema9 < last.ema20 and last.close < last.vwap
            and last.rsi9 < 45 and last.adx >= 20 and last.minus_di > last.plus_di)
    direction = 'BULLISH' if bull else 'BEARISH' if bear else 'NEUTRAL'
    return {'direction': direction, 'close': last.close, 'ema9': last.ema9,
            'ema20': last.ema20, 'vwap': last.vwap, 'rsi': last.rsi9,
            'adx': last.adx, 'plus_di': last.plus_di, 'minus_di': last.minus_di,
            'last_bar': five.index[-1].strftime('%I:%M %p'), 'bars': len(five)}, None


st.title('NIFTY F&O | Independent Option Chain + Market Trend')
st.caption('Two independent systems • Research only • No automatic orders')

with st.sidebar:
    st.header('Connection')
    token = st.text_input('Upstox Algo Trading Access Token', value=os.environ.get('UPSTOX_ACCESS_TOKEN',''), type='password')
    underlying = st.selectbox('Underlying', ['NIFTY 50','BANK NIFTY','FIN NIFTY'])
    keys = {'NIFTY 50':'NSE_INDEX|Nifty 50', 'BANK NIFTY':'NSE_INDEX|Nifty Bank', 'FIN NIFTY':'NSE_INDEX|Nifty Fin Service'}
    key = keys[underlying]
    expiry = st.text_input('Expiry (YYYY-MM-DD or current_week)', 'current_week')
    st.header('Option Chain Filters')
    max_spread = st.slider('Maximum bid/ask spread %', 0.5, 10.0, 3.0, 0.5)
    max_iv = st.slider('Maximum IV (%)', 10, 100, 40)
    min_vol = st.number_input('Minimum traded volume', min_value=0, value=10000, step=1000)
    min_score = st.slider('Minimum BUY score (out of 7)', 4, 7, 6)
    rr = st.slider('Illustrative reward/risk', 1.0, 3.0, 1.75, 0.25)
    auto = st.toggle('Auto-refresh every 30 seconds', value=False)
    st.caption('Trend is display-only and never blocks option-chain candidates.')

for state, default in [('prior',{}), ('last_identity',''), ('last_fetch_time',None), ('last_chain',None)]:
    if state not in st.session_state:
        st.session_state[state] = default

@st.fragment(run_every='30s' if auto else None)
def dashboard():
    clicked = st.button('Fetch / Refresh Now', type='primary')
    if not token:
        st.info('Enter your Upstox access token in the sidebar.')
        return
    identity = f'{key}|{expiry.strip()}'
    if identity != st.session_state.last_identity:
        st.session_state.prior = {}
        st.session_state.last_chain = None
        st.session_state.last_fetch_time = None
        st.session_state.last_identity = identity
    if not (clicked or auto):
        st.info('Click Fetch / Refresh Now, or enable Auto-refresh.')
        return
    now = datetime.now(IST)
    live = market_open(now)
    st.caption(f'Last fetch attempt: {now:%d %b %Y %I:%M:%S %p} IST')
    if not live:
        st.warning('Market is closed. Historical/stale option data may appear; live BUY disabled.')

    # Section 1: independently fetch and render option chain.
    st.header('1. OPTION CHAIN + GREEKS (INDEPENDENT)')
    try:
        raw = get_data('/option/chain', token, {'instrument_key':key,'expiry_date':expiry.strip()})
        df = unpack(raw)
        if df.empty:
            raise ValueError('No option-chain rows returned. Check expiry date.')
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        st.error(f'Option Chain unavailable: {exc}')
        df = pd.DataFrame()
    if not df.empty:
        spot = num(df.spot.iloc[0])
        ce_oi = df.loc[df.side=='CE','oi'].sum()
        pe_oi = df.loc[df.side=='PE','oi'].sum()
        pcr = pe_oi/ce_oi if ce_oi > 0 else None
        a,b,c,d = st.columns(4)
        a.metric('Spot', f'{spot:,.2f}')
        b.metric('PCR', f'{pcr:.2f}' if pcr is not None else 'N/A')
        c.metric('CE total OI', f'{ce_oi:,.0f}')
        d.metric('PE total OI', f'{pe_oi:,.0f}')
        previous = st.session_state.prior if live else {}
        ce = chain_candidates(df,'CE',max_spread,max_iv,min_vol,rr,previous)
        pe = chain_candidates(df,'PE',max_spread,max_iv,min_vol,rr,previous)
        left,right = st.columns(2)
        for col, side, candidates in [(left,'CALL (CE)',ce),(right,'PUT (PE)',pe)]:
            with col:
                st.subheader(f'Top 3 {side} candidates')
                if not candidates:
                    st.info('No eligible contracts near ATM.')
                    continue
                table = []
                for item in candidates:
                    r = item['row']
                    table.append({'Strike':f'{r.strike:g} {r.side}', 'Score':f"{item['score']}/7",
                                  'LTP':round(r.ltp,2), 'Delta':r.delta, 'IV':r.iv,
                                  'Spread %':round(r.spread_pct,2) if r.spread_pct is not None else None,
                                  'Entry*':round(item['entry'],2), 'SL*':round(item['sl'],2),
                                  'Target*':round(item['target'],2)})
                st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)
                best = candidates[0]
                st.write(f"**Best candidate:** {best['row'].strike:g} {best['row'].side} | Score {best['score']}/7")
                st.write('**Chain status:** '+('SCREEN PASSED (not an order)' if live and best['score']>=min_score else 'WAIT'))
                with st.expander('Why this score?'):
                    for reason in best['good']: st.success(reason)
                    for reason in best['bad']: st.warning(reason)
        st.caption('*Entry uses ask/LTP; SL is illustrative 12% premium risk and Target uses selected R:R. These are not validated trade levels.')
        with st.expander('Full option chain and Greeks'):
            cols = ['strike','side','ltp','oi','prev_oi','oi_change','volume','bid','ask','spread_pct','iv','delta','gamma','theta','vega']
            st.dataframe(df[cols].sort_values(['strike','side']), use_container_width=True, hide_index=True)
            st.download_button('Download chain CSV', df[cols].to_csv(index=False), 'upstox_option_chain.csv', 'text/csv')
        # Only keep live snapshots; weekend data must not create artificial premium-rise scores.
        st.session_state.prior = ({r.instrument:r.ltp for _,r in df.iterrows() if r.instrument and r.ltp>0} if live else {})

    # Section 2: entirely separate trend fetch; failures do not hide chain.
    st.divider()
    st.header('2. MARKET TREND (INDEPENDENT)')
    trend = None
    trend_error = None
    if not live:
        trend_error = 'Market closed. Live trend unavailable.'
    else:
        try:
            trend, trend_error = fetch_trend(token,key,now)
        except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:
            trend_error = str(exc)
    if trend is None:
        st.info('**TREND: UNAVAILABLE**')
        st.caption(trend_error or 'No trend data')
    else:
        st.subheader('TREND: '+trend['direction'])
        x,y,z = st.columns(3)
        x.metric('Index close (last completed 5m)',f"{trend['close']:,.2f}")
        y.metric('RSI 9',f"{trend['rsi']:.1f}")
        z.metric('ADX 14',f"{trend['adx']:.1f}")
        st.write(f"EMA 9: {trend['ema9']:.2f} | EMA 20: {trend['ema20']:.2f} | VWAP: {trend['vwap']:.2f}")
        st.caption(f"Last completed 5m bar: {trend['last_bar']} IST | Bars: {trend['bars']}")
    st.divider()
    st.header('3. SIGNAL AGREEMENT (INFORMATION ONLY)')
    if df.empty:
        st.write('Option-chain data unavailable; no comparison possible.')
    elif trend is None:
        st.write('Trend unavailable. Option Chain candidates above remain visible and unblocked.')
    else:
        st.write('**BULLISH → CE aligned | BEARISH → PE aligned | NEUTRAL → neither direction confirmed.**')
        st.write(f"Current trend: **{trend['direction']}**. Option-chain scores remain unchanged.")
    st.warning('Research-only screening, not a validated profitable strategy. OI change is versus previous close, not necessarily fresh intraday writing. Never trade from a single snapshot.')

dashboard()
