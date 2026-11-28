import io
import unittest
from unittest.mock import patch
import pandas as pd
import institutional_upload as mod

class UI:
    def __init__(self): self.tables=[]; self.errors=[]; self.exports=[]
    def file_uploader(self,*args,**kw):
        data={
          "inst_Bulk":"Date,Symbol,Client Name,Buy / Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n27-Nov-26,ABC,CLIENT,BUY,100,200\n27-Nov-26,ABC,CLIENT,BUY,100,200\n27-Nov-26,XYZ,CLIENT,BUY,100,200\n",
          "inst_Block":"Date,Symbol,Client Name,Buy / Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n27-Nov-26,ABC,CLIENT,SELL,50,200\n",
          "inst_Short":"Date,Symbol,Quantity\n27-Nov-26,ABC,25\n"}
        return io.StringIO(data[kw["key"]]) if kw["key"] in data else None
    def date_input(self,*args,**kw): return kw["value"]
    def dataframe(self,d,**kw): self.tables.append(d)
    def download_button(self,label,data,*args,**kw): self.exports.append((label,data))
    def error(self,msg): self.errors.append(msg)
    def multiselect(self,*args,**kw): return []
    def button(self,*args,**kw): return False
    def __getattr__(self,name): return lambda *args,**kw: None

class TestUpload(unittest.TestCase):
    def test_three_reports(self):
        ui=UI()
        with patch.object(mod,"st",ui):
            mod.render("",lambda:{"ABC":"key"},None,None,None,None,None,1,2,50,None,{},None)
        self.assertEqual(ui.errors,[])
        self.assertEqual(len(ui.tables),1)
        d=ui.tables[0]
        self.assertEqual(d.Symbol.tolist(),["ABC"])
        self.assertAlmostEqual(d.iloc[0]["Bulk BUY Cr"],.002)
        self.assertAlmostEqual(d.iloc[0]["Block SELL Cr"],.001)
        self.assertEqual(d.iloc[0]["Short qty"],25)
        archive=pd.read_csv(io.StringIO(ui.exports[0][1]))
        self.assertEqual(len(archive),4)

if __name__=="__main__": unittest.main()
