import os
from datetime import datetime, time
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='Upstox F&O Independent Signals', page_icon='📊', layout='wide')
IST = ZoneInfo('Asia/Kolkata')
BASE = 'https://api.upstox.com/v2'


def num(x, default=0.0):
    try:
        return float(x) if x is not None else default
    except (ValueError, TypeError):
        return default


def api_get(path, token, params):
    response = requests.get(BASE + path, params=params,
                            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f'Upstox HTTP {response.status_code}: {response.text[:240]}')
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
            md, gr = opt.get('market_data') or {}, opt.get('option_greeks') or {}
            oi = num(md.get('oi'))
            prev = num(md.get('prev_oi'))
            ltp = num(md.get('ltp'))
            bid, ask = num(md.get('bid_price')), num(md.get('ask_price'))
            records.append(dict(strike=strike, side=side, spot=spot, ltp=ltp, oi=oi,
                prev_oi=prev, oi_change=oi-prev, volume=num(md.get('volume')),
                bid=bid, ask=ask, spread_pct=((ask-bid)/((ask+bid)/2)*100 if ask>=bid>0 else None),
                iv=num(gr.get('iv'), None), delta=num(gr.get('delta'), None),
                gamma=num(gr.get('gamma'), None), theta=num(gr.get('theta'), None),
                vega=num(gr.get('vega'), None), instrument=opt.get('instrument_key', '')))
    return pd.DataFrame(records)


def analyze(df, side, max_spread, max_iv, min_vol, risk_multiple, snapshot=None):
    if df.empty:
        return None
    spot = df.spot.iloc[0]
    step = min((abs(a-b) for a,b in zip(sorted(df.strike.unique())[1:], sorted(df.strike.unique())[:-1]) if a!=b), default=50)
    candidates = df[(df.side==side) & (abs(df.strike-spot)<=step*3)].copy()
    if candidates.empty:
        return None
    opposite = 'PE' if side=='CE' else 'CE'
    opp = df[df.side==opposite]
    results = []
    for _, r in candidates.iterrows():
        reasons, failures = [], []
        # A bullish CE is supported by PUT writing below/at spot; bearish PE by CALL writing above/at spot.
        wall = opp[(opp.strike <= spot) if side=='CE' else (opp.strike >= spot)]
        wall = wall[abs(wall.strike-spot)<=2*step]
        wall_oi = wall.oi_change.sum() if not wall.empty else 0
        if wall_oi>0: reasons.append('Opposite-side OI buildup supports direction')
        else: failures.append('No supporting opposite-side OI buildup')
        if r.oi_change>0: reasons.append('Selected option OI increased vs previous close')
        else: failures.append('Selected option OI not increasing')
        if r.volume>=min_vol and r.volume>0: reasons.append('Volume threshold passed')
        else: failures.append('Insufficient option volume')
        if r.spread_pct is not None and 0<=r.spread_pct<=max_spread: reasons.append('Bid/ask spread acceptable')
        else: failures.append('Spread missing or too wide')
        if r.delta is not None and 0.35<=abs(r.delta)<=0.75: reasons.append('Delta within chosen range')
        else: failures.append('Delta outside 0.35–0.75 or missing')
        if r.iv is not None and 0<r.iv<=max_iv: reasons.append('IV below chosen ceiling')
        else: failures.append('IV too high or unavailable')
        # Theta is reported in provider units; only display it, do not impose an unsupported unit conversion.
        # Require a positive actual option-price move from an earlier observed snapshot for a BUY signal.
        previous = (snapshot or {}).get(r.instrument)
        if previous is not None and r.ltp>previous:
            reasons.append('Premium rising since prior dashboard refresh')
        else:
            failures.append('No observed premium rise yet (needs 2 refreshes)')
        score = len(reasons)
        entry = r.ask if r.ask>0 else r.ltp
        # Illustrative volatility-based premium risk, NOT an order or backtested target.
        sl_distance = max(entry*0.12, 1.0)
        sl = max(0.05, entry-sl_distance)
        target = entry + sl_distance*risk_multiple
        results.append(dict(row=r, score=score, reasons=reasons, failures=failures,
                            entry=entry, sl=sl, target=target))
    return sorted(results, key=lambda v:(v['score'], -abs(v['row'].strike-spot)), reverse=True)[0]


st.title('Upstox F&O | Option Chain + Greeks Dashboard')
st.caption('Independent of TradingView • Research-only signals • No order placement')
with st.sidebar:
    st.header('Connection')
    token = st.text_input('Upstox Algo Trading Access Token', value=os.environ.get('UPSTOX_ACCESS_TOKEN',''), type='password', help='Never share your token. Enter it only in your own dashboard.')
    underlying = st.selectbox('Underlying', ['NIFTY 50', 'BANK NIFTY', 'FIN NIFTY'])
    keys = {'NIFTY 50':'NSE_INDEX|Nifty 50', 'BANK NIFTY':'NSE_INDEX|Nifty Bank', 'FIN NIFTY':'NSE_INDEX|Nifty Fin Service'}
    instrument_key = keys[underlying]
    expiry = st.text_input('Expiry (YYYY-MM-DD or current_week)', 'current_week')
    st.header('Signal filters (adjustable)')
    max_spread = st.slider('Maximum bid/ask spread %', 0.5, 10.0, 3.0, 0.5)
    max_iv = st.slider('Maximum IV (%)', 10, 100, 40)
    min_vol = st.number_input('Minimum traded volume', min_value=0, value=10000, step=1000)
    min_score = st.slider('Minimum BUY score (out of 7)', 4, 7, 6)
    rr = st.slider('Illustrative reward/risk', 1.0, 3.0, 1.5, 0.25)
    auto = st.toggle('Auto-refresh every 30 seconds', value=False)
    st.caption('Refreshes use the Upstox API. Avoid excessive polling and respect API limits.')

if 'prior' not in st.session_state: st.session_state.prior = {}
if 'last_key' not in st.session_state: st.session_state.last_key = ''
if 'last_frame' not in st.session_state: st.session_state.last_frame = None

@st.fragment(run_every='30s' if auto else None)
def dashboard():
    if st.button('Fetch / Refresh Now', type='primary'):
        st.session_state.force_refresh = True
    if not token:
        st.info('Enter your own Upstox Access Token in the sidebar, then click Fetch / Refresh Now.')
        return
    identity = instrument_key + ':' + expiry
    if identity != st.session_state.last_key:
        st.session_state.prior = {}
        st.session_state.last_frame = None
        st.session_state.last_key = identity
    if not (auto or st.session_state.pop('force_refresh', False)):
        st.info('Ready. Click Fetch / Refresh Now to get actual Upstox data.')
        return
    try:
        raw = api_get('/option/chain', token, {'instrument_key':instrument_key,'expiry_date':expiry.strip()})
        df = unpack(raw)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        st.error(f'Could not fetch Upstox chain: {exc}')
        return
    if df.empty:
        st.warning('No option-chain rows returned. Check the expiry and contract availability.')
        return
    now = datetime.now(IST)
    open_hours = now.weekday()<5 and time(9,15)<=now.time()<=time(15,30)
    st.caption(f'Last API fetch: {now:%d %b %Y %I:%M:%S %p} IST | '+('Within normal market hours' if open_hours else 'Outside normal market hours — historical/stale quotes possible'))
    spot = df.spot.iloc[0]
    ce_oi = df[df.side=='CE'].oi.sum()
    pe_oi = df[df.side=='PE'].oi.sum()
    pcr = pe_oi/ce_oi if ce_oi>0 else 0
    a,b,c,d = st.columns(4)
    a.metric('Underlying spot', f'{spot:,.2f}')
    b.metric('Put/Call OI Ratio', f'{pcr:.2f}')
    c.metric('CE OI (all strikes)', f'{ce_oi:,.0f}')
    d.metric('PE OI (all strikes)', f'{pe_oi:,.0f}')

    old = st.session_state.prior
    ce = analyze(df,'CE',max_spread,max_iv,min_vol,rr,old)
    pe = analyze(df,'PE',max_spread,max_iv,min_vol,rr,old)
    st.subheader('Independent signal assessment')
    left,right = st.columns(2)
    for col, title, item in [(left,'CALL (CE) BUY',ce),(right,'PUT (PE) BUY',pe)]:
        with col:
            if item is None:
                st.warning(f'{title}: No eligible contracts')
                continue
            r = item['row']
            # No actionable BUY outside normal trading hours, and no BUY without 2 snapshots.
            status = 'BUY CANDIDATE' if item['score']>=min_score and open_hours else 'WAIT'
            st.markdown(f'### {title}: {status}')
            st.metric('Confirmation score', f"{item['score']}/7")
            st.write(f"**Strike:** {r.strike:g} {r.side} | **LTP:** ₹{r.ltp:.2f} | **Delta:** {r.delta if r.delta is not None else 'N/A'} | **IV:** {r.iv if r.iv is not None else 'N/A'}")
            st.write(f"**OI:** {r.oi:,.0f} | **OI change vs previous close:** {r.oi_change:+,.0f} | **Spread:** {r.spread_pct:.2f}%" if r.spread_pct is not None else f'**OI:** {r.oi:,.0f} | Spread: unavailable')
            st.write(f"**Gamma:** {r.gamma} | **Theta:** {r.theta} | **Vega:** {r.vega}")
            if status=='BUY CANDIDATE':
                st.write(f"**Illustrative entry (ask):** ₹{item['entry']:.2f} | **SL:** ₹{item['sl']:.2f} | **Target:** ₹{item['target']:.2f}")
            else:
                st.write('No actionable entry/SL/target while WAIT.')
            with st.expander('Why this score?'):
                for s in item['reasons']: st.success(s)
                for s in item['failures']: st.warning(s)
    st.warning('Heuristic screening only; not validated as a profitable trading strategy. OI change is vs previous close, not live incremental OI. Signals are not trade recommendations. Never place orders from a single snapshot.')
    st.subheader('Full option chain and Greeks')
    view = df[['strike','side','ltp','oi','prev_oi','oi_change','volume','bid','ask','spread_pct','iv','delta','gamma','theta','vega']].sort_values(['strike','side'])
    st.dataframe(view, use_container_width=True, hide_index=True, height=430)
    st.download_button('Download current chain as CSV', view.to_csv(index=False), file_name='upstox_option_chain.csv', mime='text/csv')
    st.session_state.prior = {r.instrument:r.ltp for _,r in df.iterrows() if r.instrument and r.ltp>0}
    st.session_state.last_frame = df

dashboard()
st.caption('Source: Upstox Option Chain API v2. No TradingView integration or broker order APIs are used.')
