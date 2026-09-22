from __future__ import annotations
import json,time,statistics
from pathlib import Path
from src.mcp_transport import MCPClientSession,UnixMCPTransport

def ms(fn):
 t=time.perf_counter(); v=fn(); return (time.perf_counter()-t)*1000,v
def pct(xs,p):
 ys=sorted(xs); z=(len(ys)-1)*p; lo=int(z); hi=min(lo+1,len(ys)-1); f=z-lo; return ys[lo]*(1-f)+ys[hi]*f
def st(xs): return {'n':len(xs),'p50_ms':round(pct(xs,.5),2),'p95_ms':round(pct(xs,.95),2),'p99_ms':round(pct(xs,.99),2),'mean_ms':round(statistics.fmean(xs),2),'max_ms':round(max(xs),2)}
root=Path('/tmp/ralf-playwright-mcp'); upload=root/'bench-upload.txt'; upload.write_text('benchmark\n')
s=MCPClientSession(UnixMCPTransport('/run/ralf-browser-playwright-mcp/mcp.sock'),timeout=30,client_name='browser-persistent-actions')
s.initialize(); s.list_tools(); s.call_tool('browser_snapshot',{})
r={'click':[],'type':[],'upload':[],'submit':[],'readback':[]}
for _ in range(5):
 dt,_=ms(lambda:s.call_tool('browser_click',{'target':'e3'})); r['click'].append(dt)
 dt,_=ms(lambda:s.call_tool('browser_snapshot',{})); r['readback'].append(dt)
 dt,_=ms(lambda:s.call_tool('browser_type',{'target':'e6','text':'Fabio','submit':False})); r['type'].append(dt)
 dt,_=ms(lambda:s.call_tool('browser_snapshot',{})); r['readback'].append(dt)
 dt,_=ms(lambda:s.call_tool('browser_click',{'target':'e8'}));
 dt2,_=ms(lambda:s.call_tool('browser_file_upload',{'paths':[str(upload)]})); r['upload'].append(dt+dt2)
 dt,_=ms(lambda:s.call_tool('browser_snapshot',{})); r['readback'].append(dt)
 dt,_=ms(lambda:s.call_tool('browser_click',{'target':'e9'})); r['submit'].append(dt)
 dt,_=ms(lambda:s.call_tool('browser_snapshot',{})); r['readback'].append(dt)
s.close(); print(json.dumps({k:st(v) for k,v in r.items()},indent=2))
