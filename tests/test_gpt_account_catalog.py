"""Exercise the in-page adapter with native query functions, without credentials."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget, CdpError, ChromeCdp


SOURCE = Path(__file__).parents[1] / 'ralfloop_agent/integration/gpt_account_catalog.js'


def run_adapter(options=None, *, rate_limit=False, missing_project=False, setup=""):
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    expression = SOURCE.read_text().replace('__CATALOG_INPUT__', json.dumps({
        'query': '', 'project_id': '', 'cursor': '', 'limit': 50, **(options or {})}))
    script = r'''
const vm = require('vm');
const calls = [];
const project = id => ({gizmo:{gizmo:{id,short_url:id+'-name',display:{name:id}}}});
const mk = (key, pages) => ({queryKey:key,state:{data:{pages:[pages[0]],pageParams:[null]}}, options:{initialPageParam:null,
 getNextPageParam:p=>p.cursor,queryFn:async ctx=>{calls.push(ctx.pageParam);return pages[ctx.pageParam]}}});
const projects = mk(['snorlax-history'], [{items:[project('g-p-abcd')],cursor:1},{items:[project('g-p-ef01')],cursor:null}]);
const history = mk(['conversationHistory'], [{items:[{id:'old-123',title:'Old chat',gizmo_id:'g-p-abcd'}],cursor:1},{items:[{id:'older-456',title:'Older chat'}],cursor:null}]);
const scoped = mk(['snorlaxConversations',{gizmoId:'g-p-abcd'}],history.state.data.pages);
const queries = [projects,history];
if (!MISSING) queries.push(scoped);
if (RATE) history.state.data.pages[0].items=[];
SETUP
const client = {getQueryCache:()=>({getAll:()=>queries})};
const fiber = {memoizedProps:{client}};
const context = {window:{},document:{documentElement:{'__reactFiber$test':fiber},querySelector:()=>RATE?{}:null},
 AbortController,URL,setTimeout,clearTimeout};
vm.runInNewContext(EXPRESSION,context).then(s=>console.log(JSON.stringify({result:JSON.parse(s),calls})));
'''.replace('MISSING', json.dumps(missing_project)).replace('RATE', json.dumps(rate_limit)).replace('EXPRESSION', json.dumps(expression)).replace('SETUP', setup)
    return json.loads(subprocess.check_output(['node', '-e', script], text=True))


def test_native_directory_paginates_and_preserves_project_context():
    out = run_adapter()
    result = out['result']
    assert len(result['projects']) == 2
    assert result['projects_complete'] is True
    assert result['chats'][0]['context_url'] == 'https://chatgpt.com/g/g-p-abcd-name/c/old-123'
    assert result['chats'][0]['url'] == 'https://chatgpt.com/c/old-123'
    assert json.loads(result['next_cursor']) == {'page': 1, 'scope': '["",""]'}
    assert out['calls'] == [1]  # Cached first pages require no network call.


def test_title_search_walks_native_pages():
    result = run_adapter({'query': 'Older'})['result']
    assert [row['title'] for row in result['chats']] == ['Older chat']
    assert result['complete'] is True
    assert result['search_scope'] == 'title'


def test_rate_limit_is_not_empty_success_and_projects_survive():
    result = run_adapter(rate_limit=True)['result']
    assert result['warning'] == 'account_catalog_rate_limited'
    assert result['complete'] is False
    assert len(result['projects']) == 2


def test_unopened_project_requires_native_query_initialization():
    result = run_adapter({'project_id': 'g-p-abcd'}, missing_project=True)['result']
    assert result['warning'] == 'account_catalog_open_project_first'
    assert result['complete'] is False


def test_cursor_validation_happens_before_evaluation(monkeypatch):
    cdp = ChromeCdp()
    monkeypatch.setattr(cdp, '_wait_target', lambda _: BrowserTarget('test', 'page', 'https://chatgpt.com/', '', 'ws://test'))
    with pytest.raises(CdpError, match='cursor_invalid'):
        cdp.account_catalog('test', cursor='{"unexpected":1}')


def test_cursor_cannot_cross_project_or_search_scope():
    cursor = json.dumps({'page': 1, 'scope': '["","old"]'})
    result = run_adapter({'query': 'different', 'cursor': cursor})['result']
    assert result['error'] == 'account_catalog_cursor_scope_mismatch'


def test_search_batch_exposes_continuation_after_five_pages():
    setup = """
const pages = Array.from({length:7}, (_,i)=>({items:[{id:'chat-'+i,title:i===6?'Needle':'Other'}],cursor:i===6?null:i+1}));
history.state.data.pages=[pages[0]];
history.options.queryFn=async c=>pages[c.pageParam];
"""
    first = run_adapter({'query': 'Needle'}, setup=setup)['result']
    assert first['chats'] == []
    assert first['complete'] is False
    assert json.loads(first['next_cursor'])['page'] == 5
    second = run_adapter({'query': 'Needle', 'cursor': first['next_cursor']}, setup=setup)['result']
    assert second['chats'][0]['title'] == 'Needle'
    assert second['complete'] is True


def test_project_cap_never_claims_directory_complete():
    setup = """
projects.state.data.pages=[{items:[project('g-p-abcd')],cursor:1}];
projects.options.queryFn=async c=>({items:[project('g-p-abcd')],cursor:c.pageParam+1});
"""
    result = run_adapter(setup=setup)['result']
    assert result['projects_complete'] is False
    assert result['warning'] == 'account_catalog_projects_incomplete'


def test_missing_directory_retains_history_and_explicit_warning():
    result = run_adapter(setup="queries.splice(0,1);")['result']
    assert result['chats']
    assert result['projects_complete'] is False
    assert result['warning'] == 'account_catalog_projects_unavailable'


def test_network_error_does_not_serialize_exception_secrets():
    setup = "projects.options.queryFn=async()=>{throw new Error('credential must never escape')};"
    result = run_adapter(setup=setup)['result']
    assert result['warning'] == 'account_catalog_fetch_failed'
    assert 'credential' not in json.dumps(result)


def test_project_slug_in_history_is_normalized():
    result = run_adapter(setup="history.state.data.pages[0].items[0].gizmo_id='g-p-abcd-name';")['result']
    assert result['chats'][0]['project_id'] == 'g-p-abcd'
    assert '/g/g-p-abcd-name/c/' in result['chats'][0]['context_url']
