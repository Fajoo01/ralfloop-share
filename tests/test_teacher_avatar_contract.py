from pathlib import Path
import subprocess

def test_avatar_events_and_accessibility():
    source = Path('ralfloop_agent/teacher/web/static/avatar_controller.js').resolve()
    script = r'''
import fs from 'node:fs';
import assert from 'node:assert/strict';
const {BottazziAvatarController:C}=await import('data:text/javascript;base64,'+fs.readFileSync(process.argv[1]).toString('base64'));
let scheduled=0, cancelled=0;
globalThis.requestAnimationFrame=()=>++scheduled;
globalThis.cancelAnimationFrame=()=>cancelled++;
const c=new C({reduced_motion:false});
for(const state of ['idle','speaking','listening','thinking','success','error']){c.setState(state);assert.equal(c.state,state);}
assert.throws(()=>c.setState('shell'));
const speech=new EventTarget();c.bindSpeech(speech);
speech.dispatchEvent(new Event('start'));assert.equal(c.state,'speaking');
speech.dispatchEvent(new Event('error'));assert.equal(c.state,'idle');assert.ok(cancelled>0);
const disabled=new C({avatar_enabled:false,onChange:()=>assert.fail()});disabled.setState('speaking');disabled.setMouthLevel(1);assert.equal(disabled.mouth_level,0);disabled.reset();
const reduced=new C({reduced_motion:true});const before=scheduled;reduced.setState('speaking');reduced.setMouthLevel(1);assert.equal(reduced.mouth_level,0);assert.equal(scheduled,before);
'''
    subprocess.run(['node','--input-type=module','-e',script,str(source)],check=True)


def test_visible_avatar_surface_contract():
    root = Path('ralfloop_agent/teacher/web/static')
    html = (root / 'index.html').read_text()
    css = (root / 'style.css').read_text()
    controller = (root / 'avatar_controller.js').read_text()
    assert 'id="teacher-avatar"' in html
    assert 'id="teacher-avatar-status"' in html
    assert '.teacher-avatar[data-state=speaking]' in css
    assert '.teacher-avatar[data-state=thinking]' in css
    assert 'installBottazziAvatarSurface' in controller
    assert "thinking:'Sto pensando…'" in controller
    assert "speaking:'Ti sto parlando'" in controller
