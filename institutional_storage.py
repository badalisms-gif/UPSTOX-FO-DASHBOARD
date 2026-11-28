import hashlib
import json
import requests

TABLES={"institutional_history","institutional_watchlist","institutional_observations","institutional_paper_results"}

class Storage:
    def __init__(self,url,key):
        self.url=url.rstrip("/")+"/rest/v1/"
        self.headers={"apikey":key}
        if not key.startswith("sb_secret_"):
            self.headers["Authorization"]="Bearer "+key
    def save(self,table,records):
        if table not in TABLES: raise ValueError("Unknown table")
        for i in range(0,len(records),100):
            r=requests.post(self.url+table,params={"on_conflict":"record_id"},headers={**self.headers,"Prefer":"resolution=merge-duplicates,return=minimal"},json=records[i:i+100],timeout=20)
            r.raise_for_status()
    def load(self,table):
        if table not in TABLES: raise ValueError("Unknown table")
        rows=[]
        while True:
            r=requests.get(self.url+table,params={"select":"record_id,payload","order":"record_id","limit":500,"offset":len(rows)},headers=self.headers,timeout=20)
            r.raise_for_status()
            batch=r.json()
            rows.extend(batch)
            if len(batch)<500: return rows

def records(frame,keys):
    rows=json.loads(frame.to_json(orient="records",date_format="iso"))
    unique={}
    for r in rows:
        values=[float(r[k]) if k in ("quantity","price") and r.get(k) is not None else r.get(k) for k in keys]
        rid=hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()
        unique[rid]={"record_id":rid,"payload":r}
    return list(unique.values())
