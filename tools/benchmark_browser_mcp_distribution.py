#!/usr/bin/env python3
from __future__ import annotations
import json, os, shutil, statistics, time, urllib.request
from pathlib import Path
from src.mcp_transport import MCPClientSession, UnixMCPTransport

SOCKET='/run/ralf-browser-playwright-mcp/mcp.sock'
OUT=Path('/tmp/ralf-browser-benchmark-20260922.json')
N_READ=50
N_INIT=20
N_ROUTE=50

def ms(fn):
    t=time.perf_counter(); value=fn(); return (time.perf_counter()-t)*1000, value

def pct(xs,p):
    ys=sorted(xs); pos=(len(ys)-1)*p; lo=int(pos); hi=min(lo+1,len(ys)-1); f=pos-lo
    return ys[lo]*(1-f)+ys[hi]*f

def stats(xs):
    return {'n':len(xs),'mean_ms':round(statistics.fmean(xs),2),'p50_ms':round(pct(xs,.50),2),'p95_ms':round(pct(xs,.95),2),'p99_ms':round(pct(xs,.99),2),'min_ms':round(min(xs),2),'max_ms':round(max(xs),2)}

def post_route(goal):
    body=json.dumps({'user_goal':goal,'mode':'route_only','extra_context':{'source':'telegram_natural','telegram_user_id':11,'telegram_chat_id':22,'telegram_message_id':999}}).encode()
    req=urllib.request.Request('http://127.0.0.1:19090/tasks/run',data=body,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=10) as r: return json.load(r)
def main():
    results={'timestamp':'2026-09-22','read_iterations':N_READ,'init_iterations':N_INIT,'route_iterations':N_ROUTE}
    init=[]
    for _ in range(N_INIT):
        s=MCPClientSession(UnixMCPTransport(SOCKET),timeout=30,client_name='browser-bench-init')
        try:
            dt,_=ms(s.initialize); init.append(dt)
        finally: s.close()
    results['initialize_new_session']=stats(init)

    s=MCPClientSession(UnixMCPTransport(SOCKET),timeout=30,client_name='browser-bench-persistent')
    s.initialize(); tool_t=[]; tabs_t=[]; snap_t=[]
    try:
        for _ in range(N_READ):
            dt,_=ms(s.list_tools); tool_t.append(dt)
            dt,_=ms(lambda:s.call_tool('browser_tabs',{'action':'list'})); tabs_t.append(dt)
            dt,_=ms(lambda:s.call_tool('browser_snapshot',{})); snap_t.append(dt)
    finally: s.close()
    results['persistent_tools_list']=stats(tool_t)
    results['persistent_browser_tabs']=stats(tabs_t)
    results['persistent_browser_snapshot']=stats(snap_t)

    route_read=[]; route_write=[]
    for _ in range(N_ROUTE):
        dt,r=ms(lambda:post_route('mostrami le schede del browser')); assert r['capability_route']['skills_used']==['browser.inspect']; route_read.append(dt)
        dt,r=ms(lambda:post_route('browser clicca e3')); assert r['capability_route']['skills_used']==['browser.interact']; route_write.append(dt)
    results['backend_route_browser_inspect']=stats(route_read)
    results['backend_route_browser_interact']=stats(route_write)
    # Approval-bound benchmark is isolated from production session/approval state.
    bench=Path('/tmp/ralf-browser-bench-state'); shutil.rmtree(bench,ignore_errors=True); bench.mkdir()
    os.environ.update({
        'RALFLOOP_UNIFIED_ASSISTANT':'1','RALFLOOP_BROWSER_INTERACT_LIVE':'1',
        'RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE':'1','RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS':'11',
        'RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS':'22','RALFLOOP_TELEGRAM_APPROVAL_DB':str(bench/'approvals.sqlite'),
        'RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG':str(bench/'audit.jsonl'),'RALFLOOP_UNIFIED_SESSION_DIR':str(bench/'sessions'),
        'RALFLOOP_BROWSER_UPLOAD_ROOTS':'/tmp/ralf-browser-canary','RALFLOOP_BROWSER_UPLOAD_STAGING_DIR':'/tmp/ralf-playwright-mcp/approved-uploads-bench',
    })
    from ralfloop_agent.unified_assistant import runtime
    from ralfloop_agent.unified_assistant.browser_mcp_adapter import BrowserMCPApprovalProvider
    class TimedProvider(BrowserMCPApprovalProvider):
        def __init__(self): super().__init__(); self.snapshot_ms=[]; self.apply_ms=[]
        def snapshot(self):
            dt,v=ms(super().snapshot); self.snapshot_ms.append(dt); return v
        def apply(self,scope):
            dt,v=ms(lambda:super(TimedProvider,self).apply(scope)); self.apply_ms.append(dt); return v
    provider=TimedProvider(); runtime.BrowserMCPApprovalProvider=lambda:provider
    first=provider.snapshot(); assert 'Bot-tazzi Browser Canary' in first['text']
    provider.snapshot_ms.clear()
    cases=[('click','browser clicca e3','clicked'),('type','browser scrivi su e6 testo="Fabio"','Fabio'),('upload','browser carica e8 file="/tmp/ralf-browser-canary/upload.txt"','file:upload.txt'),('submit','browser submit e9','submitted:Fabio')]
    samples=[]; mid=10000; ctx={'source':'telegram_natural','telegram_user_id':11,'telegram_chat_id':22,'telegram_chat_type':'private'}
    for cycle in range(5):
        for action,request,marker in cases:
            snap0=len(provider.snapshot_ms); app0=len(provider.apply_ms); mid+=1
            p_ms,preview=ms(lambda:runtime.run_unified_telegram(request,{**ctx,'telegram_message_id':mid}))
            assert preview['metadata']['status']=='draft_pending_approval', preview
            mid+=1
            a_ms,approved=ms(lambda:runtime.run_unified_telegram('ok',{**ctx,'telegram_message_id':mid}))
            assert approved['metadata']['status']=='EXECUTED_VERIFIED', approved
            post=(approved['metadata'].get('result') or {}).get('post_snapshot') or ''; assert marker in post
            snaps=provider.snapshot_ms[snap0:]; apps=provider.apply_ms[app0:]
            assert len(snaps)==3 and len(apps)==1, (action,snaps,apps)
            component=sum(snaps)+apps[0]
            samples.append({'action':action,'preview_total_ms':p_ms,'approval_execute_total_ms':a_ms,
                'preview_snapshot_ms':snaps[0],'pre_execute_snapshot_ms':snaps[1],'action_apply_ms':apps[0],
                'post_readback_snapshot_ms':snaps[2],'approval_runtime_overhead_ms':max(0.0,a_ms-(snaps[1]+apps[0]+snaps[2]))})
    def field_stats(rows,key): return stats([float(x[key]) for x in rows])
    results['approval_bound_aggregate']={k:field_stats(samples,k) for k in ('preview_total_ms','approval_execute_total_ms','preview_snapshot_ms','pre_execute_snapshot_ms','action_apply_ms','post_readback_snapshot_ms','approval_runtime_overhead_ms')}
    results['approval_bound_by_action']={}
    for action,_,_ in cases:
        rows=[x for x in samples if x['action']==action]
        results['approval_bound_by_action'][action]={k:field_stats(rows,k) for k in ('preview_total_ms','approval_execute_total_ms','action_apply_ms','post_readback_snapshot_ms')}
    results['approval_bound_samples']=samples
    OUT.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in results.items() if k not in {'approval_bound_samples'}},ensure_ascii=False,indent=2))
    print('RESULT_FILE='+str(OUT))

if __name__=='__main__': main()
