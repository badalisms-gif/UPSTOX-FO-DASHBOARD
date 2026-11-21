

import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='Upstox F&O Dashboard V2', page_icon='📊', layout='wide')
IST = ZoneInfo('Asia/Kolkata')
BASE = 'https://api.upstox.com/v2'


def num(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def api_get(path, token, params):
    response = requests.get(BASE + path, params=params,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f'Upstox HTTP {response.status_code}: {response.text[:250]}')
    payload = response.json()
    if payload.get('status') != 'success':
        raise RuntimeError(str(payload.get('errors') or payload.get('message') or 'API error'))
    return payload.get('data') or []


def unpack(chain):
    records = []
    for item in chain:
        strike = num(item.get('strike_price'))
        spot = num(item.get('underlying_spot_price'))
        for side, key in [('CE', 'call_options'), ('PE', 'put_options')]:
            opt = item.get(key) or {}
            md = opt.get('market_data') or {}
            gr = opt.get('option_greeks') or {}
            bid, ask = num(md.get('bid_price')), num(md.get('ask_price'))
            oi, prev_oi = num(md.get('oi')), num(md.get('prev_oi'))
            records.append(dict(
                strike=strike, side=side, spot=spot,
                instrument=opt.get('instrument_key') or '',
                ltp=num(md.get('ltp')), oi=oi, prev_oi=prev_oi,
                oi_change_close=oi-prev_oi, volume=num(md.get('volume')),
                bid=bid, ask=ask,
                spread_pct=(100*(ask-bid)/((ask+bid)/2) if ask >= bid > 0 else None),
                iv=num(gr.get('iv'), None), delta=num(gr.get('delta'), None),
                gamma=num(gr.get('gamma'), None), theta=num(gr.get('theta'), None),
                vega=num(gr.get('vega'), None)))
    return pd.DataFrame(records)


def market_open(now):
    return now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 30)


def choose(df, side, previous, max_spread, max_iv, min_vol, min_score, steps, spot_threshold):
    if df.empty:
        return None
    spot = num(df.spot.iloc[0])
    strikes = sorted(df.strike.unique())
    step = min((b-a for a, b in zip(strikes, strikes[1:]) if b > a), default=50)
    subset = df[(df.side == side) & (abs(df.strike-spot) <= steps*step)].copy()
    if subset.empty:
        return None

    prior_spot = previous.get('spot') if previous else None
    prior_rows = previous.get('rows', {}) if previous else {}
    spot_move = (spot-prior_spot) if prior_spot is not None else None
    opposite = 'PE' if side == 'CE' else 'CE'
    opp = df[df.side == opposite]
    wall = opp[(opp.strike <= spot) if side == 'CE' else (opp.strike >= spot)]
    wall = wall[abs(wall.strike-spot) <= 2*step]
    wall_change = wall.oi_change_close.sum() if not wall.empty else 0
    results = []

    for _, r in subset.iterrows():
        old = prior_rows.get(r.instrument) if r.instrument else None
        valid_pair = old is not None and prior_spot is not None
        premium_move = r.ltp-old['ltp'] if valid_pair else None
        oi_move = r.oi-old['oi'] if valid_pair else None
        vol_move = r.volume-old['volume'] if valid_pair else None
        # 7 interpretable checks. Snapshot changes are distinct from changes since yesterday's close.
        checks = [
            ('NIFTY spot moved in selected direction', spot_move is not None and
                (spot_move >= spot_threshold if side == 'CE' else spot_move <= -spot_threshold)),
            ('Option premium increased since prior refresh', premium_move is not None and premium_move > 0),
            ('Option OI increased since prior refresh', oi_move is not None and oi_move > 0),
            ('Fresh option volume since prior refresh', vol_move is not None and vol_move > 0),
            ('Opposite-side support/resistance OI buildup vs previous close', wall_change > 0),
            ('Delta within 0.35 to 0.75', r.delta is not None and 0.35 <= abs(r.delta) <= 0.75),
            ('IV within configured ceiling', r.iv is not None and 0 < r.iv <= max_iv),
        ]
        score = sum(ok for _, ok in checks)
        liquid = (r.ltp > 0 and r.volume >= min_vol and r.spread_pct is not None
                  and 0 <= r.spread_pct <= max_spread and r.ask > 0 and r.bid > 0)
        # Hard gates prevent a misleading high score from triggering a BUY.
        directional = all(ok for _, ok in checks[:4])
        ready = bool(valid_pair and liquid and directional and score >= min_score)
        results.append(dict(row=r, score=score, checks=checks, liquid=liquid,
            ready=ready, spot_move=spot_move, premium_move=premium_move,
            oi_move=oi_move, vol_move=vol_move, wall_change=wall_change,
            distance=abs(r.strike-spot)))

    # Prioritize valid setups, then score, then nearest-to-ATM contract.
    return sorted(results, key=lambda x: (x['ready'], x['score'], -x['distance']), reverse=True)[0]


st.title('Upstox F&O | Option Chain + Greeks Dashboard V2')
st.caption('NIFTY CE/PE screening • Actual Upstox option chain • No automatic orders • Not TradingView-connected')

with st.sidebar:
    st.header('Connection')
    token = st.text_input('Upstox Algo Trading Access Token',
        value=os.environ.get('UPSTOX_ACCESS_TOKEN', ''), type='password')
    underlying = st.selectbox('Underlying', ['NIFTY 50', 'BANK NIFTY', 'FIN NIFTY'])
    keys = {'NIFTY 50': 'NSE_INDEX|Nifty 50',
            'BANK NIFTY': 'NSE_INDEX|Nifty Bank',
            'FIN NIFTY': 'NSE_INDEX|Nifty Fin Service'}
    instrument_key = keys[underlying]
    expiry = st.text_input('Expiry (YYYY-MM-DD)', '2026-11-23').strip()
    st.header('Signal filters')
    max_spread = st.slider('Maximum bid/ask spread %', 0.5, 10.0, 3.0, 0.5)
    max_iv = st.slider('Maximum IV (%)', 10, 100, 40)
    min_vol = st.number_input('Minimum cumulative option volume', min_value=0, value=10000, step=1000)
    min_score = st.slider('Minimum BUY score (out of 7)', 4, 7, 6)
    steps = st.slider('Strikes to scan on each side of ATM', 1, 10, 3)
    spot_threshold = st.number_input('Minimum spot move between snapshots (points)',
        min_value=0.0, value=2.0, step=0.5)
    rr = st.slider('Illustrative reward/risk', 1.0, 3.0, 1.5, 0.25)
    auto = st.toggle('Auto-refresh every 30 seconds', value=False)
    st.caption('Refresh only while the dashboard is open. API limits apply.')

if 'previous' not in st.session_state:
    st.session_state.previous = None
if 'identity' not in st.session_state:
    st.session_state.identity = ''


@st.fragment(run_every='30s' if auto else None)
def dashboard():
    manual = st.button('Fetch / Refresh Now', type='primary')
    if not token:
        st.info('Enter your Upstox Access Token in the sidebar.')
        return
    if not expiry or expiry == 'current_week':
        st.warning('Enter an actual listed expiry date, e.g. 2026-11-23. Update it for future weeks.')
        return
    try:
        datetime.strptime(expiry, '%Y-%m-%d')
    except ValueError:
        st.error('Expiry must be YYYY-MM-DD.')
        return
    identity = f'{instrument_key}|{expiry}'
    if identity != st.session_state.identity:
        st.session_state.identity = identity
        st.session_state.previous = None
    if not (manual or auto):
        st.info('Ready. Click Fetch / Refresh Now.')
        return

    try:
        raw = api_get('/option/chain', token,
            {'instrument_key': instrument_key, 'expiry_date': expiry})
        df = unpack(raw)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        st.error(f'Could not fetch Upstox chain: {exc}')
        return
    if df.empty:
        st.warning('No option-chain rows returned. Check expiry and contract availability.')
        return

    now = datetime.now(IST)
    in_hours = market_open(now)
    spot = num(df.spot.iloc[0])
    ce_oi = df[df.side == 'CE'].oi.sum()
    pe_oi = df[df.side == 'PE'].oi.sum()
    pcr = pe_oi / ce_oi if ce_oi > 0 else None
    previous = st.session_state.previous
    st.caption(f'Fetched {now:%d %b %Y %I:%M:%S %p} IST | '
        + ('Normal market hours' if in_hours else 'Market closed / quotes may be stale'))
    if not in_hours:
        st.warning('Market is closed. BUY signals are disabled; data may be stale.')
    if previous is None:
        st.info('First snapshot saved. A second refresh is required for directional confirmation.')

    a, b, c, d = st.columns(4)
    a.metric('Underlying spot', f'{spot:,.2f}')
    b.metric('Put/Call OI ratio', f'{pcr:.2f}' if pcr is not None else 'N/A')
    c.metric('CE OI (all strikes)', f'{ce_oi:,.0f}')
    d.metric('PE OI (all strikes)', f'{pe_oi:,.0f}')

    ce = choose(df, 'CE', previous, max_spread, max_iv, min_vol,
                min_score, steps, spot_threshold)
    pe = choose(df, 'PE', previous, max_spread, max_iv, min_vol,
                min_score, steps, spot_threshold)
    # Both sides cannot be confirmed in the same snapshot.
    conflict = bool(ce and pe and ce['ready'] and pe['ready'])
    st.subheader('Independent CE / PE assessment')
    if conflict:
        st.warning('Conflicting CE and PE candidates: both blocked. Wait for clear direction.')
    left, right = st.columns(2)
    for col, title, item in [(left, 'CALL (CE) BUY', ce), (right, 'PUT (PE) BUY', pe)]:
        with col:
            if item is None:
                st.warning(f'{title}: No candidate')
                continue
            r = item['row']
            buy = bool(item['ready'] and in_hours and not conflict)
            st.markdown(f"### {title}: {'BUY CANDIDATE' if buy else 'WAIT'}")
            st.metric('Confirmation score', f"{item['score']}/7")
            st.write(f'**Strike:** {r.strike:g} {r.side} | **LTP:** ₹{r.ltp:.2f}')
            st.write(f'**Delta:** {r.delta} | **IV:** {r.iv} | **Spread:** '
                     + (f'{r.spread_pct:.2f}%' if r.spread_pct is not None else 'N/A'))
            st.write(f'**OI:** {r.oi:,.0f} | **OI vs previous close:** {r.oi_change_close:+,.0f}')
            st.write(f"**Since last refresh:** Spot {item['spot_move'] if item['spot_move'] is not None else 'N/A'} | "
                     f"Premium {item['premium_move'] if item['premium_move'] is not None else 'N/A'} | "
                     f"OI {item['oi_move'] if item['oi_move'] is not None else 'N/A'}")
            st.write(f'**Gamma:** {r.gamma} | **Theta:** {r.theta} | **Vega:** {r.vega}')
            if buy:
                entry = r.ask
                sl_distance = max(entry * 0.12, 1.0)
                sl = max(0.05, entry-sl_distance)
                target = entry+sl_distance*rr
                st.write(f'**Illustrative entry (ask):** ₹{entry:.2f} | '
                         f'**SL:** ₹{sl:.2f} | **Target:** ₹{target:.2f}')
            else:
                st.write('No actionable entry / SL / target while WAIT.')
            with st.expander('Why this score?'):
                for label, passed in item['checks']:
                    st.write(('✅ ' if passed else '❌ ') + label)
                st.write('Liquidity/spread gate: ' + ('PASS' if item['liquid'] else 'FAIL'))
                st.caption('Spot, premium, OI and volume changes are measured between dashboard snapshots.')

    st.warning('Research-only heuristic, not a validated profitable strategy. '
        'OI buildup alone cannot establish whether buyers or writers initiated positions. '
        'Bid/ask prices and SL/target can change rapidly. Do not trade from this screen alone.')
    st.subheader('Full option chain and Greeks')
    columns = ['strike', 'side', 'ltp', 'oi', 'prev_oi', 'oi_change_close',
        'volume', 'bid', 'ask', 'spread_pct', 'iv', 'delta', 'gamma', 'theta', 'vega']
    view = df[columns].sort_values(['strike', 'side'])
    st.dataframe(view, use_container_width=True, hide_index=True, height=430)
    st.download_button('Download current chain as CSV', view.to_csv(index=False),
        file_name='upstox_option_chain.csv', mime='text/csv')

    # Save only after scoring: comparisons are against the immediately previous successful fetch.
    st.session_state.previous = {
        'spot': spot,
        'rows': {r.instrument: {'ltp': r.ltp, 'oi': r.oi, 'volume': r.volume}
                 for _, r in df.iterrows() if r.instrument and r.ltp > 0},
    }


dashboard()
st.caption('Source: Upstox Option Chain API v2. No broker orders are placed.')
