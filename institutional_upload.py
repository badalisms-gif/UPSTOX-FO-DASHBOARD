import pandas as pd
import streamlit as st
import numpy as np

def render(token, universe, technical, news, expiry, chain, now, minvol, spread, iv, option_setup, snapshots, futures):
    st.header("Institutional Intelligence")
    st.caption("Research watchlist only. Weekly short sales are not open short positions.")
    frames=[]
    archive=st.file_uploader("Restore historical archive (optional)",type=["csv"],key="inst_archive")
    if archive is not None:
        try:
            h=pd.read_csv(archive,dtype={"symbol":str,"client":str})
            required={"date","symbol","quantity","kind","client","side","price","value_cr"}
            if not required.issubset(h.columns): raise ValueError("Invalid archive schema")
            h["date"]=pd.to_datetime(h.date,errors="coerce")
            for col in ("quantity","price"): h[col]=pd.to_numeric(h[col],errors="coerce")
            h["symbol"]=h.symbol.fillna("").str.strip().str.upper()
            h["client"]=h.client.fillna("").str.strip()
            h["side"]=h.side.fillna("").str.upper()
            ok=h.date.notna() & h.date.le(pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None).normalize()) & h.symbol.ne("") & np.isfinite(h.quantity) & h.quantity.gt(0) & h.kind.isin(["Bulk","Block","Short"])
            ok &= h.kind.eq("Short") | (h.side.isin(["BUY","SELL"]) & h.client.ne("") & np.isfinite(h.price) & h.price.gt(0))
            h["value_cr"]=h.quantity*h.price/1e7
            frames.append(h.loc[ok,list(required)].copy())
        except Exception as e: st.error(f"Archive: {e}")
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
    for col in ("client","side","price","value_cr"):
        if col not in d: d[col]=np.nan
    for col in ("client","side"): d[col]=d[col].fillna("").str.strip()
    d=d.drop_duplicates(subset=["kind","date","symbol","client","side","quantity","price"])
    st.download_button("Save updated historical archive",d.to_csv(index=False,date_format="%Y-%m-%d"),"institutional_history.csv")
    st.caption("Download and restore this archive on your next visit. Streamlit session is not permanent storage.")
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
    st.warning("Deal totals may include both counterparties or overlapping reports. Activity is not directional evidence. Automatic permanent storage is not implemented. Futures confirmation requires fresh quotes and previous OI.")
    auto=st.checkbox("Auto scan every 5 minutes (keep dashboard open)",key="inst_auto")
    selected=st.multiselect("Stocks for live technical and news checks",table.Symbol.tolist(),default=table.Symbol.head(3).tolist(),max_selections=10)
    full=st.checkbox("Rotate through entire watchlist automatically",key="inst_full")
    @st.fragment(run_every="300s" if auto else None)
    def monitor():
        t=now()
        active=t.weekday()<5 and (9,15)<=(t.hour,t.minute)<(15,30)
        manual=st.button("Check technical and news")
        batch=selected
        if auto and active and full and not manual:
            symbols=table.Symbol.tolist()
            cursor=st.session_state.get("inst_cursor",0)%len(symbols)
            batch=symbols[cursor:cursor+10]
            st.session_state["inst_cursor"]=(cursor+len(batch))%len(symbols)
        if manual or (auto and active):
            if not batch: st.info("Select at least one stock"); return
            if not token: st.error("Upstox token required"); return
            results=[]
            for sym in batch:
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
                            full_chain=chain(token,mapping[sym],exp)
                            c=full_chain.copy()
                            if not c.empty:
                                c=c[(c.Side==side)&c.Delta.abs().between(.35,.75)&c.Volume.ge(minvol)&c["Spread %"].le(spread)&c.IV.gt(0)&c.IV.le(iv)&((c.Delta>0) if side=="CE" else (c.Delta<0))&c.Bid.gt(0)&c.Ask.ge(c.Bid)&c.OI.gt(0)].copy()
                                if not c.empty:
                                    c["Distance"]=(c.Delta.abs()-.55).abs()
                                    o=c.sort_values(["Spread %","Distance","Volume"],ascending=[True,True,False]).iloc[0]
                                    row.update({"Status":"CE/PE WATCH","Score":round(80+10*(1-o["Spread %"]/spread)+10*(1-min(1,o.Distance/.4)),1),"Option key":o.Key,"Strike":o.Strike,"Expiry":exp,"Ask":o.Ask,"Delta":o.Delta,"Theta":o.Theta,"Gamma":o.Gamma,"Vega":o.Vega,"IV":o.IV,"OI":o.OI,"OI Change":o["Daily OI Change"],"Spread %":o["Spread %"]})
                                    snapshot_time=now()
                                    sk=mapping[sym]+"|"+exp
                                    old=snapshots.get(sk,{})
                                    snapshots[sk]={x.Key:{"time":snapshot_time,"OI":x.OI,"Volume":x.Volume,"LTP":x.LTP} for _,x in full_chain.iterrows()}
                                    try:
                                        v=option_setup(o,checks,metrics,full_chain,exp,sym,old,n,"Institutional CSV",snapshot_time)
                                        row["V7 verification"]=v["Status"]
                                        if v["Status"]=="QUALIFIED":
                                            for field in ("Entry","SL","Target","Target 2","Lots","Lot Size","Planned Risk","Capital"):
                                                row[field]=v[field]
                                            row["Status"]="V7 QUALIFIED / Futures OI pending"
                                    except Exception as e:
                                        row["V7 verification"]=str(e)
                    try:
                        row.update(futures(token,mapping[sym],side))
                    except Exception as e:
                        row["Futures confirmed"]=False
                        row["Futures verification"]=str(e)
                    if row["Status"]=="V7 QUALIFIED / Futures OI pending":
                        row["Status"]="QUALIFIED RESEARCH" if row.get("Futures confirmed") else "WATCH ONLY / Futures unverified"
                        if not row.get("Futures confirmed"):
                            for field in ("Entry","SL","Target","Target 2","Lots","Lot Size","Planned Risk","Capital"): row.pop(field,None)
                    results.append(row)
                except Exception as e: results.append({"Symbol":sym,"Status":"UNVERIFIED","Reason":str(e)})
            out=pd.DataFrame(results)
            st.session_state["institutional_last_results"]=out
            observed=out.copy()
            observed["Observed at"]=now().isoformat()
            observed["Watchlist size"]=len(table)
            history=st.session_state.get("institutional_observations",[])
            history.append(observed)
            st.session_state["institutional_observations"]=history
            st.dataframe(out,use_container_width=True)
            for side in ("CE","PE"):
                st.subheader("Top "+side+" watchlist")
                if "Score" in out and "Side" in out:
                    st.dataframe(out[(out.Side==side)&out.Score.gt(0)].sort_values("Score",ascending=False),use_container_width=True)
            st.caption("Heuristic watch score, not probability. V7 risk and quote checks applied where available. Futures OI confirmation required for qualified research; missing data blocks qualification.")

        history=st.session_state.get("institutional_observations",[])
        if history:
            st.subheader("Forward observation funnel (latest scan)")
            latest=history[-1]
            status=latest.Status.fillna("")
            st.write({"Watchlist":int(latest["Watchlist size"].iloc[0]),"Scanned":len(latest),"Option watch or higher":int(status.str.contains("WATCH|QUALIFIED").sum()),"Qualified research":int(status.eq("QUALIFIED RESEARCH").sum())})
            st.dataframe(latest,use_container_width=True)
            st.download_button("Export timestamped forward observations",pd.concat(history,ignore_index=True).to_csv(index=False),"institutional_forward_observations.csv")
            st.caption("Automatic scans require an open dashboard session. Counts are observations, not completed trade outcomes. Export before closing session.")

        st.subheader("Paper signal outcome tracking")
        ledger=st.session_state.get("institutional_paper_ledger",{})
        if history:
            for _,r in history[-1].iterrows():
                if r.get("Status")!="QUALIFIED RESEARCH": continue
                key=str(r.get("Option key",""))
                if not key or key=="nan": continue
                stamp=str(r["Observed at"])
                signal_id=stamp[:10]+"|"+key
                if signal_id not in ledger:
                    ledger[signal_id]={"Symbol":r["Symbol"],"Side":r["Side"],"Option key":key,"Expiry":r["Expiry"],"Signal time":stamp,"Entry":float(r["Entry"]),"SL":float(r["SL"]),"Target":float(r["Target"]),"State":"WAIT ENTRY"}
        if ledger and (st.button("Refresh paper outcomes") or (auto and active)):
            for trade in ledger.values():
                if trade["State"] not in ("WAIT ENTRY","OPEN"): continue
                try:
                    t=now()
                    if t.date().isoformat()!=trade["Signal time"][:10]:
                        trade["State"]="UNRESOLVED / session ended"; continue
                    c=chain(token,mapping[trade["Symbol"]],trade["Expiry"])
                    q=c[c.Key.eq(trade["Option key"])]
                    if q.empty: continue
                    q=q.iloc[0]
                    bid,ask=float(q.Bid),float(q.Ask)
                    if not np.isfinite(bid) or not np.isfinite(ask) or bid<=0 or ask<bid: continue
                    trade["Last checked"]=t.isoformat()
                    if trade["State"]=="WAIT ENTRY" and trade["SL"]<ask<=trade["Entry"]:
                        trade.update({"State":"OPEN","Observed entry":ask,"Entry time":t.isoformat()})
                    elif trade["State"]=="OPEN":
                        outcome="TARGET" if bid>=trade["Target"] else "SL" if bid<=trade["SL"] else "EOD" if (t.hour,t.minute)>=(15,20) else None
                        if outcome:
                            trade.update({"State":outcome,"Observed exit":bid,"Exit time":t.isoformat(),"PnL per share":bid-trade["Observed entry"]})
                    if trade["State"]=="WAIT ENTRY" and (t.hour,t.minute)>=(15,20): trade["State"]="NO ENTRY / EOD"
                except Exception as e: trade["Last error"]=str(e)
        st.session_state["institutional_paper_ledger"]=ledger
        if ledger:
            paper=pd.DataFrame(ledger.values())
            st.dataframe(paper,use_container_width=True)
            st.download_button("Export paper outcomes",paper.to_csv(index=False),"institutional_paper_outcomes.csv")
        st.caption("Periodic quote observations only; no orders, no assumed fills between checks, no permanent storage. Missed intraday touches remain unknown.")

    monitor()
