"""Run with: uv run --no-project --with playwright python tests/check_bottazzi_work_ui.py"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

html = (Path(__file__).parents[1] / "openshell_backend/bottazzi_ui.html").read_text().replace('<body>', '<body data-app-gateway="1">')
calls = []
errors = []

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""
      localStorage.setItem('bottazzi.assistant.v1',JSON.stringify({session:'one',chats:[
        {id:'one',title:'First work',messages:[{role:'user',text:'First'},{role:'assistant',text:'Read this response'}]},
        {id:'two',title:'Second work',messages:[{role:'user',text:'Second'}]}
      ]}));
      window.spoken=[];window.stops=0;window.trackStops=0;
      window.BotTazziNative={speak(t){window.spoken.push(t);return true},stop(){window.stops++}};
      Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia:async()=>({getTracks:()=>[{stop(){window.trackStops++}}]})}});
      window.MediaRecorder=class{static isTypeSupported(){return true}constructor(){this.state='inactive';this.mimeType='audio/webm'}start(){this.state='recording'}stop(){this.state='inactive';this.ondataavailable({data:new Blob(['test audio'],{type:'audio/webm'})});this.onstop()}};
    """)
    def route(request):
        url = request.request.url
        method = request.request.method
        calls.append((method, url, request.request.post_data if '/audio/' not in url else None))
        if request.request.resource_type == 'document':
            request.fulfill(status=200, content_type='text/html', body=html)
            return
        payload = {"ok": True, "tasks": [], "local_only": True}
        if url.endswith('/tasks') and method == 'POST':
            payload = {"task": {"task_id": "task-" + str(len(calls))}}
        if url.endswith('/audio/transcribe'):
            payload = {"text": "Voice work"}
        if url.endswith('/chat'):
            payload = {"response": "Voice received"}
        request.fulfill(status=200, content_type='application/json', body=json.dumps(payload))
    page.route('**/*', route)
    page.goto('http://127.0.0.1:18799/')
    page.locator('.mobile-conversation-row').first.click()
    page.get_by_role('button', name='🔊 Leggi', exact=True).click()
    assert page.evaluate('window.spoken') == ['Read this response']
    page.get_by_role('button', name='■ Stop', exact=True).click()
    assert page.evaluate('window.stops') >= 2
    page.locator('#voiceBtn').click()
    page.get_by_text('Registrazione 0 s', exact=True).wait_for()
    page.locator('#voiceStop').click()
    assert page.locator('#voicePreview').is_visible()
    assert not any('/audio/transcribe' in url for _, url, _ in calls)
    page.locator('#voiceCancel').click()
    assert not page.locator('#voiceDraft').is_visible()
    page.locator('#voiceBtn').click()
    page.get_by_text('Registrazione 0 s', exact=True).wait_for()
    page.locator('#voiceStop').click()
    page.locator('#voiceSend').click()
    page.locator('#chat .bubble').filter(has_text='Voice received').wait_for()
    assert sum('/audio/transcribe' in url for _, url, _ in calls) == 1
    page.locator('#mobileChatBack').click()
    page.locator('.work-actions').first.get_by_role('button', name='Chiudi lavoro').click()
    page.wait_for_function("state.chats[0].closedAt > 0")
    assert page.locator('.mobile-conversation-row').count() == 1
    assert any('/state' in url and 'completed' in (data or '') for _, url, data in calls)
    page.locator('#archiveChats').click()
    assert page.get_by_text('First work', exact=True).is_visible()
    page.get_by_role('button', name='Riapri', exact=True).click()
    page.wait_for_function('!state.chats[0].closedAt')
    assert any('/state' in url and 'queued' in (data or '') for _, url, data in calls)
    assert not errors, errors
    browser.close()
print('PASS: mobile queue, archive/reopen, speech/stop, microphone preview/cancel/send; no JS errors')
