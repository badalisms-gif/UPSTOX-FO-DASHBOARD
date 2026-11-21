# Upstox F&O Independent Dashboard

An independent Streamlit research dashboard. **No TradingView integration and no order placement.**

## Run (on a computer with Python 3.10+)

```bash
pip install -r requirements.txt
streamlit run app.py
```

A browser opens with the dashboard. Enter your **Upstox Algo Trading Access Token** in the password field. The token is not stored in a file by the app. If you deploy publicly, restrict access to the dashboard and do not share the token.

Select underlying and expiry (e.g. `current_week`), click **Fetch / Refresh Now**. Fetch a second time during market hours to allow the premium-rise filter to compare consecutive observations. Optional 30-second refresh can be enabled.

The dashboard uses `GET https://api.upstox.com/v2/option/chain`, reading actual OI, previous-close OI, bid, ask, IV, delta, gamma, theta and vega. Its CE/PE score is a **custom, unvalidated heuristic**. OI change is versus previous close; not intraday incremental change. Theta/Gamma/Vega are shown without pretending their scale is directly comparable across expiries. BUY CANDIDATE is not an executable broker signal. Entry/SL/target are illustrative only.

No live account or token is bundled. Network/API access is required when running; a closed market can show stale quotes. The app cannot independently establish NSE holiday status, so it only checks weekday and clock hours. Check the exchange schedule before trading.

## Safety

Do not post API tokens in chats, screenshots, GitHub repositories or public links. Tokens expire and must be refreshed through your authorized Upstox login. Never use real-money trading solely based on this dashboard. Test on live paper observations and compare with TradingView first.

## Sources
- https://upstox.com/developer/api-documentation/get-pc-option-chain/
- https://upstox.com/developer/api-documentation/get-option-contracts/
