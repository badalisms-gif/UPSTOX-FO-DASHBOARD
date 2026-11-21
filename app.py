
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='NIFTY F&O | Single Decision V2', page_icon='📊', layout='wide')
IST = ZoneInfo('Asia/Kolkata')
BASE = 'https://api.upstox.com/v2'
SESSION = requests.Session()


def num(x, default=np.nan):
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def api_get(path, token, params=None):
    r = SESSION.get(BASE + path, params=params or {}, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, timeout=20)
    if not r.ok:
        raise RuntimeError(f'Upstox HTTP {r.status_code}: {r.text[:200]}')
    payload = r.json()
    if payload.get('status') != 'success':
        raise RuntimeError(str(payload.get('errors') or payload.get('message') or 'API error'))
    return payload.get('data') or []


def chain_df(raw):
    out = []
    for item in raw:
        for side, key in [('CE', 'call_options'), ('PE', 'put_options')]:
            opt = item.get(key) or {}
            md = opt.get('market_data') or {}
            gr = opt.get('option_greeks') or {}
            bid, ask = num(md.get('bid_price')), num(md.get('ask_price'))
            spread = (100 * (ask - bid) / ((ask + bid) / 2)) if np.isfinite(bid) and np.isfinite(ask) and ask >= bid > 0 else np.nan
            oi, prev = num(md.get('oi'), 0), num(md.get('prev_oi'), 0)
            out.append({'strike': num(item.get('strike_price')), 'side': side,
                        'spot': num(item.get('underlying_spot_price')),
                        'ltp': num(md.get('ltp')), 'oi': oi, 'prev_oi': prev,
                        'oi_change_close': oi - prev, 'volume': num(md.get('volume'), 0),
                        'bid': bid, 'ask': ask, 'spread_pct': spread,
                        'iv': num(gr.get('iv')), 'delta': num(gr.get('delta')),
                        'gamma': num(gr.get('gamma')), 'theta': num(gr.get('theta')),
                        'vega': num(gr.get('vega')), 'instrument': opt.get('instrument_key', '')})
    return pd.DataFrame(out)


def candles(token, key):
    # Intraday candle endpoint returns the current session's 5-minute OHLCV.
    data = api_get('/historical-candle/intraday/' + requests.utils.quote(key, safe='') + '/5minute', token)
    rows = data.get('candles', []) if isinstance(data, dict) else []
    if not rows:
        raise RuntimeError('No 5-minute NIFTY candles available')
    frame = pd.DataFrame(rows)
    if frame.shape[1] < 6:
        raise RuntimeError('Unexpected candle format')
    frame = frame.iloc[:, :6]
    frame.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    frame['timestamp'] = pd.to_datetime(frame['timestamp'], utc=True).dt.tz_convert(IST)
    for c in ['open', 'high', 'low', 'close', 'volume']:
        frame[c] = pd.to_numeric(frame[c], errors='coerce')
    return frame.dropna(subset=['timestamp', 'open', 'high', 'low', 'close']).sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)


def rma(s, length):
    return s.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def indicators(df):
    x = df.copy()
    x['ema9'] = x.close.ewm(span=9, adjust=False, min_periods=9).mean()
    x['ema20'] = x.close.ewm(span=20, adjust=False, min_periods=20).mean()
    tr = pd.concat([(x.high - x.low), (x.high - x.close.shift()).abs(), (x.low - x.close.shift()).abs()], axis=1).max(axis=1)
    x['atr'] = rma(tr, 14)
    up = x.high.diff()
    down = -x.low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=x.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=x.index)
    sm_tr = rma(tr, 14)
    plus_di = 100 * rma(plus_dm, 14) / sm_tr.replace(0, np.nan)
    minus_di = 100 * rma(minus_dm, 14) / sm_tr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    x['adx'] = rma(dx, 14)
    x['plus_di'] = plus_di
    x['minus_di'] = minus_di
    # NSE_INDEX volume can be 0. In that case true volume-weighted VWAP cannot be calculated.
    x['vwap'] = np.nan
    for _, day in x.groupby(x.timestamp.dt.date):
        volume = day.volume.clip(lower=0)
        if volume.sum() > 0:
            tp = (day.high + day.low + day.close) / 3
            x.loc[day.index, 'vwap'] = (tp * volume).cumsum() / volume.cumsum().replace(0, np.nan)
    return x


def direction(x, adx_min):
    if len(x) < 35:
        return 'NONE', 'Not enough 5-minute candles for stable indicators', None
    r = x.iloc[-1]
    needed = ['ema9', 'ema20', 'atr', 'adx', 'plus_di', 'minus_di']
    if any(not np.isfinite(r[k]) for k in needed):
        return 'NONE', 'Trend indicators not ready', r
    if r.adx < adx_min:
        return 'NONE', f'ADX {r.adx:.1f} below threshold {adx_min}', r
    # NIFTY index often has zero volume, so VWAP is not a reliable mandatory filter.
    # If valid volume-based VWAP exists, require it; otherwise use EMA + DI only.
    bull_vwap = not np.isfinite(r.vwap) or r.close > r.vwap
    bear_vwap = not np.isfinite(r.vwap) or r.close < r.vwap
    if r.close > r.ema9 > r.ema20 and bull_vwap and r.plus_di > r.minus_di and r.ema9 > x.ema9.iloc[-2]:
        return 'CE', 'Bullish 5-minute EMA/DI alignment (VWAP used only when available)', r
    if r.close < r.ema9 < r.ema20 and bear_vwap and r.minus_di > r.plus_di and r.ema9 < x.ema9.iloc[-2]:
        return 'PE', 'Bearish 5-minute EMA/DI alignment (VWAP used only when available)', r
    return 'NONE', 'Trend filters disagree or market is sideways', r


def rank_options(df, side, spot, min_volume, max_spread, max_iv, prev_prices):
    strikes = sorted(df.strike.dropna().unique())
    steps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    step = min(steps) if steps else 50
    subset = df[(df.side == side) & ((df.strike - spot).abs() <= 3 * step)].copy()
    rows = []
    for _, r in subset.iterrows():
        if not (np.isfinite(r.ltp) and r.ltp > 0):
            continue
        checks = {
            'Delta 0.35-0.75': np.isfinite(r.delta) and 0.35 <= abs(r.delta) <= 0.75,
            'Volume': r.volume >= min_volume,
            'Spread': np.isfinite(r.spread_pct) and 0 <= r.spread_pct <= max_spread,
            'IV': np.isfinite(r.iv) and 0 < r.iv <= max_iv,
            'OI present': r.oi > 0,
            'Premium rising vs recent snapshot': r.instrument in prev_prices and r.ltp > prev_prices[r.instrument],
        }
        score = sum(bool(v) for v in checks.values())
        rows.append((score, abs(r.strike - spot), r, checks))
    rows.sort(key=lambda z: (-z[0], z[1]))
    return rows


st.title('NIFTY F&O | Single Final Decision V2')
st.caption('5-minute index trend + Upstox option chain. Research only. No order placement.')
with st.sidebar:
    st.header('Connection')
    token = st.text_input('Upstox Algo Trading Access Token', value=os.getenv('UPSTOX_ACCESS_TOKEN', ''), type='password')
    expiry = st.text_input('Expiry (YYYY-MM-DD)', value='2026-11-23')
    st.header('Filters')
    adx_min = st.slider('Minimum ADX', 15, 40, 25)
    max_spread = st.slider('Maximum bid/ask spread %', 0.5, 10.0, 3.0, 0.5)
    max_iv = st.slider('Maximum IV %', 10, 100, 40)
    min_volume = st.number_input('Minimum option volume', min_value=0, value=10000, step=1000)
    min_score = st.slider('Minimum option score / 6', 3, 6, 5)
    rr = st.slider('Illustrative reward/risk', 1.0, 3.0, 1.75, 0.25)
    auto = st.toggle('Auto-refresh every 30 seconds', value=False)
    st.caption('Token is not saved to source code. Keep this dashboard private.')

if 'last_prices' not in st.session_state:
    st.session_state.last_prices = {}
if 'last_identity' not in st.session_state:
    st.session_state.last_identity = ''
if 'last_market_snapshot_at' not in st.session_state:
    st.session_state.last_market_snapshot_at = None

@st.fragment(run_every='30s' if auto else None)
def dashboard():
    click = st.button('Fetch / Refresh Now', type='primary')
    if not token:
        st.info('Enter your own Upstox token in the sidebar.')
        return
    if not auto and not click:
        st.info('Click Fetch / Refresh Now to load current data.')
        return
    now = datetime.now(IST)
    trading_window = now.weekday() < 5 and time(9, 20) <= now.time() <= time(15, 25)
    if not trading_window:
        st.warning('Outside regular market window. NO TRADE. Data may be stale.')
    identity = 'NSE_INDEX|Nifty 50:' + expiry.strip()
    if identity != st.session_state.last_identity:
        st.session_state.last_prices = {}
        st.session_state.last_market_snapshot_at = None
        st.session_state.last_identity = identity
    try:
        date_value = datetime.strptime(expiry.strip(), '%Y-%m-%d').date()
        if date_value < now.date():
            st.error('Selected expiry is in the past. Choose a current listed expiry.')
            return
        raw = api_get('/option/chain', token, {'instrument_key': 'NSE_INDEX|Nifty 50', 'expiry_date': expiry.strip()})
        df = chain_df(raw)
        if df.empty:
            st.warning('No option chain returned for this expiry.')
            return
        x = indicators(candles(token, 'NSE_INDEX|Nifty 50'))
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        st.error(f'Data fetch/processing failed: {exc}')
        return
    spot = num(df.spot.dropna().iloc[0]) if df.spot.notna().any() else np.nan
    ce_oi = df.loc[df.side == 'CE', 'oi'].sum()
    pe_oi = df.loc[df.side == 'PE', 'oi'].sum()
    pcr = pe_oi / ce_oi if ce_oi > 0 else np.nan
    a, b, c = st.columns(3)
    a.metric('NIFTY spot', f'{spot:,.2f}' if np.isfinite(spot) else 'N/A')
    b.metric('Put/Call OI ratio', f'{pcr:.2f}' if np.isfinite(pcr) else 'N/A')
    c.metric('5-min candles', len(x))
    st.caption(f'API checked: {now:%d %b %Y %I:%M:%S %p} IST')

    # Require a fresh *completed* candle, not a partially formed bar.
    completed = x[x.timestamp + pd.Timedelta(minutes=5) <= pd.Timestamp(now)]
    if completed.empty:
        st.error('No completed 5-minute candle')
        return
    last_bar_time = completed.timestamp.iloc[-1]
    recent_bar = now - last_bar_time.to_pydatetime() <= timedelta(minutes=12)
    side, trend_reason, r = direction(completed, adx_min)
    st.subheader('NIFTY trend')
    st.write(f'**Direction:** {side} | **Reason:** {trend_reason}')
    if r is not None:
        st.write(f'EMA9: {r.ema9:.2f} | EMA20: {r.ema20:.2f} | VWAP: {r.vwap:.2f} | ADX: {r.adx:.1f} | ATR: {r.atr:.2f}')
    st.caption(f'Last completed 5-minute bar starts {last_bar_time:%d %b %H:%M} IST')

    old = st.session_state.last_prices
    prior_time = st.session_state.last_market_snapshot_at
    # A prior valid market-hours snapshot must exist, with a useful time gap.
    comparison_valid = trading_window and prior_time is not None and timedelta(seconds=20) <= now - prior_time <= timedelta(minutes=3)
    rankings = rank_options(df, side, spot, min_volume, max_spread, max_iv, old if comparison_valid else {}) if side in ('CE', 'PE') and np.isfinite(spot) else []
    st.subheader('FINAL TRADING DECISION')
    decision = 'NO TRADE'
    reasons = []
    if not trading_window:
        reasons.append('Market is closed or outside permitted trading window')
    if not recent_bar:
        reasons.append('5-minute market data is stale')
    if side == 'NONE':
        reasons.append(trend_reason)
    if not comparison_valid:
        reasons.append('Waiting for two recent market-hours option-chain snapshots 20-180 seconds apart')
    if not rankings:
        reasons.append('No eligible contract candidates for the confirmed direction')
    if rankings:
        score, _, best, checks = rankings[0]
        if score < min_score:
            reasons.append(f'Option score {score}/6 below minimum {min_score}/6')
        # Premium direction must actually improve between fresh snapshots.
        if not checks['Premium rising vs recent snapshot']:
            reasons.append('Option premium has not risen between recent snapshots')
        if trading_window and recent_bar and comparison_valid and score >= min_score and checks['Premium rising vs recent snapshot']:
            decision = f'{side} BUY CANDIDATE'
    if decision == 'NO TRADE':
        st.info('NO TRADE')
        for reason in reasons:
            st.write('• ' + reason)
    else:
        st.success(decision)
        entry = best.ask if np.isfinite(best.ask) and best.ask > 0 else best.ltp
        # Illustrative risk only; not ATR-derived option premium stops.
        sl = max(0.05, entry * 0.88)
        target = entry + (entry - sl) * rr
        st.write(f'**Strike:** {best.strike:g} {side} | **Option score:** {score}/6')
        st.write(f'**Illustrative entry:** ₹{entry:.2f} | **SL:** ₹{sl:.2f} | **Target:** ₹{target:.2f}')
        st.warning('Entry/SL/target are illustrative premium levels, not validated execution signals.')
    st.subheader('Top 3 option candidates in confirmed direction')
    if rankings:
        display = []
        for score, _, row, checks in rankings[:3]:
            display.append({'Strike': f'{row.strike:g} {row.side}', 'Score': f'{score}/6',
                            'LTP': row.ltp, 'Delta': row.delta, 'IV': row.iv,
                            'Spread %': row.spread_pct, 'Volume': row.volume,
                            'OI': row.oi, 'OI vs prev close': row.oi_change_close,
                            'Premium up': checks['Premium rising vs recent snapshot']})
        st.dataframe(pd.DataFrame(display), hide_index=True, use_container_width=True)
    else:
        st.caption('No directional candidates while trend is unconfirmed.')
    with st.expander('Full option chain and Greeks'):
        st.dataframe(df.drop(columns=['instrument']), use_container_width=True, hide_index=True)
        st.download_button('Download option chain CSV', df.to_csv(index=False), file_name='nifty_option_chain.csv', mime='text/csv')
    st.caption('OI change shown vs previous close, not fresh OI buildup. A positive OI change does not prove buying/writing. PCR is contextual, not a standalone direction signal.')
    if trading_window:
        st.session_state.last_prices = {r.instrument: r.ltp for _, r in df.iterrows() if r.instrument and np.isfinite(r.ltp) and r.ltp > 0}
        st.session_state.last_market_snapshot_at = now

dashboard()
