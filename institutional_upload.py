import pandas as pd
import streamlit as st
import numpy as np

def render(token, universe, technical, news, expiry, chain, now, minvol, spread, iv):
    st.header("Institutional Intelligence")
    st.caption("Research watchlist only. Weekly short sales are not open short positions.")
    frames=[]
    for kind in ("Bulk","Block","Short"):
        f=st.file_uploader(kind+" Deals CSV" if kind!="Short" else "Weekly Short Selling CSV",type=["csv"],key="inst_"+kind)
        if f is None: continue
        try:
            d=pd.read_csv(f,encoding="utf-8-sig",dtype=str).fillna("")
            d.columns=d.columns.str.strip()
            d=d.rename(columns={"Date":"date","Symbol":"symbol","Client Name":"client","Buy / Sell":"side","Quantity Traded":"quantity","Quantity":"quantity","Trade Price / Wght. Avg. Price":"price"})
            cols=["date","symbol","quantity"]+([] if kind=="Short" else ["client","side","price"])
            d=d[cols].copy()
            for c in cols: d[c]=d[c].str.strip()
            d["date"]=pd.to_datetime(d.date,format="%d-%b-%y",errors="coerce")
            d["symbol"]=d.symbol.str.upper()
            d["quantity"]=pd.to_numeric(d.quantity.str.replace(",","",regex=False),errors="coerce")
            ok=d.date.notna() & d.date.le(pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None).normalize()) & d.symbol.ne("") & np.isfinite(d.quantity) & d.quantity.gt(0)
            if kind!="Short":
                d["price"]=pd.to_numeric(d.price.str.replace(",","",regex=False),errors="coerce")
                d["side"]=d.side.str.upper()
                ok &= d.side.isin(["BUY","SELL"]) & np.isfinite(d.price) & d.price.gt(0) & d.client.ne("")
                d["value_cr"]=d.quantity*d.price/1e7
            clean=d[ok].drop_duplicates().copy()
            clean["kind"]=kind
            frames.append(clean)
            st.write(kind, "valid:",len(clean),"rejected:",int((~ok).sum()),"duplicates:",int(ok.sum()-len(clean)))
        except Exception as e: st.error(f"{kind}: {e}")
    if not frames: return
    d=pd.concat(frames,ignore_index=True)
    try: mapping=universe()
    except Exception as e:
        st.error(f"F&O universe unavailable: {e}"); return
    d=d[d.symbol.isin(mapping)]
    if d.empty: st.info("No matching F&O disclosures"); return
    asof=st.date_input("Analysis date",value=d.date.max().date())
    day=pd.Timestamp(asof)
    rows=[]
    for sym,g in d.groupby("symbol"):
        row={"Symbol":sym}
        for k in ("Bulk","Block"):
            x=g[(g.kind==k)&g.date.eq(day)]
            for side in ("BUY","SELL"):
                row[k+" "+side+" Cr"]=x.loc[x.side.eq(side),"value_cr"].sum() if not x.empty else 0
        x=g[(g.kind=="Short")&g.date.le(day)&g.date.gt(day-pd.Timedelta(days=7))]
        row["Short qty"]=x.quantity.sum()
        row["Short latest"]=str(x.date.max().date()) if len(x) else ""
        row["Activity Cr"]=sum(v for k,v in row.items() if k.endswith(" Cr"))
        if row["Activity Cr"] or row["Short qty"]: rows.append(row)
    if not rows: st.info("No disclosures in selected window"); return
    table=pd.DataFrame(rows).sort_values(["Activity Cr","Short qty"],ascending=False)
    st.dataframe(table,use_container_width=True)
    st.download_button("Export disclosure shortlist",table.to_csv(index=False),"institutional_shortlist.csv")
    st.warning("Deal totals may include both counterparties or overlapping reports. Activity is not directional evidence. Historical storage and futures OI are not yet implemented.")
    selected=st.multiselect("Stocks for live technical and news checks",table.Symbol.tolist(),default=table.Symbol.head(3).tolist(),max_selections=10)
    if st.button("Check technical and news"):
        if not token: st.error("Upstox token required"); return
        results=[]
        for sym in selected:
            try:
                side,checks,metrics=technical(token,mapping[sym])
                if side not in ("CE","PE"): results.append({"Symbol":sym,"Status":"NO TRADE"}); continue
                n=news(sym,side)
                passed=sum(bool(v) for v in checks.values() if v is not None)
                total=sum(v is not None for v in checks.values())
                row={"Symbol":sym,"Side":side,"Technical checks":f"{passed}/{total}","News":n.get("status","UNKNOWN"),"Status":"NO TRADE","Score":0}
                if total and passed==total and not n.get("block") and n.get("complete"):
                    exp=expiry(token,mapping[sym],now().date())
                    if exp:
                        c=chain(token,mapping[sym],exp)
                        if not c.empty:
                            c=c[(c.Side==side)&c.Delta.abs().between(.35,.75)&c.Volume.ge(minvol)&c["Spread %"].le(spread)&c.IV.between(0,iv)&c.Bid.gt(0)&c.Ask.ge(c.Bid)&c.OI.gt(0)].copy()
                            if not c.empty:
                                c["Distance"]=(c.Delta.abs()-.55).abs()
                                o=c.sort_values(["Spread %","Distance","Volume"],ascending=[True,True,False]).iloc[0]
                                row.update({"Status":"CE/PE WATCH","Score":round(80+10*(1-o["Spread %"]/spread)+10*(1-min(1,o.Distance/.4)),1),"Strike":o.Strike,"Expiry":exp,"Ask":o.Ask,"Delta":o.Delta,"Theta":o.Theta,"Gamma":o.Gamma,"Vega":o.Vega,"IV":o.IV,"OI":o.OI,"OI Change":o["Daily OI Change"],"Spread %":o["Spread %"]})
                results.append(row)
            except Exception as e: results.append({"Symbol":sym,"Status":"UNVERIFIED","Reason":str(e)})
        out=pd.DataFrame(results)
        st.dataframe(out,use_container_width=True)
        for side in ("CE","PE"):
            st.subheader("Top "+side+" watchlist")
            if "Score" in out and "Side" in out:
                st.dataframe(out[(out.Side==side)&out.Score.gt(0)].sort_values("Score",ascending=False),use_container_width=True)
        st.caption("Heuristic watch score, not probability. No entry signal; futures OI and fresh option quote verification pending.")
