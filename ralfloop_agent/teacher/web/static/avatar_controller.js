/* Stable avatar contract; visual skin is intentionally replaceable. */
export class BottazziAvatarController {
  constructor({avatar_enabled=true, reduced_motion=globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false, onChange=()=>{}}={}) { this.enabled=avatar_enabled; this.reducedMotion=reduced_motion; this.onChange=onChange; this.state='idle'; this.mouth_level=0; this.smoothedMouth=0; this.frame=null; this.analyser=null; this.samples=null; }
  setState(state) {
    if (!['idle','speaking','listening','thinking','success','error'].includes(state)) throw new Error('invalid_avatar_state');
    this.stop(); this.state=state; if(this.enabled)this.onChange(this);
    if(state==='speaking' && this.enabled && !this.reducedMotion){
      const tick=t=>{let level=.18+.48*Math.abs(Math.sin(t/95));if(this.analyser&&this.samples){this.analyser.getByteTimeDomainData(this.samples);const rms=Math.sqrt(this.samples.reduce((sum,x)=>sum+((x-128)/128)**2,0)/this.samples.length);const target=Math.max(0,Math.min(1,(rms-.012)*7));this.smoothedMouth=this.smoothedMouth*.58+target*.42;level=this.smoothedMouth;}this.setMouthLevel(level);this.frame=requestAnimationFrame(tick);};
      this.frame=requestAnimationFrame(tick);
    }
  }
  stop(){if(this.frame!==null)cancelAnimationFrame(this.frame);this.frame=null;this.mouth_level=0;this.smoothedMouth=0;}
  setMouthLevel(level) { this.mouth_level=this.enabled&&!this.reducedMotion?Math.max(0,Math.min(1,Number(level)||0)):0; if(this.enabled) this.onChange(this); }
  bindSpeech(utterance){
    for(const event of ['start','resume'])utterance.addEventListener(event,()=>this.setState('speaking'));
    for(const event of ['end','error','pause'])utterance.addEventListener(event,()=>this.reset());
  }
  bindRecognition(recognition){recognition.addEventListener('start',()=>this.setState('listening'));for(const event of ['end','error'])recognition.addEventListener(event,()=>this.reset());}
  bindAudio(audio){
    const AudioContextClass=globalThis.AudioContext||globalThis.webkitAudioContext;
    if(!AudioContextClass){const start=()=>this.setState('speaking'),end=()=>this.reset();audio.addEventListener('playing',start);for(const event of ['pause','ended','error'])audio.addEventListener(event,end);return ()=>{audio.removeEventListener('playing',start);for(const event of ['pause','ended','error'])audio.removeEventListener(event,end);this.reset();};}
    const context=new AudioContextClass(), source=context.createMediaElementSource(audio);
    const analyser=context.createAnalyser();analyser.fftSize=256;
    this.analyser=analyser;this.samples=new Uint8Array(analyser.fftSize);
    source.connect(analyser);analyser.connect(context.destination);
    const start=()=>{context.resume().catch(()=>{});this.setState('speaking');},end=()=>this.reset();
    audio.addEventListener('playing',start);for(const event of ['pause','ended','error'])audio.addEventListener(event,end);
    return ()=>{audio.removeEventListener('playing',start);for(const event of ['pause','ended','error'])audio.removeEventListener(event,end);this.reset();source.disconnect();analyser.disconnect();this.analyser=null;context.close();};
  }
  reset() { this.stop();this.state='idle'; if(this.enabled)this.onChange(this); }
}

export function installBottazziAvatarSurface() {
  if (typeof document === 'undefined') return null;
  const source=document.querySelector('.brand img');
  const surfaces=()=>[...document.querySelectorAll('[data-teacher-avatar-surface]')];
  if(!source || !surfaces().length) return null;
  const labels={idle:'Pronto',listening:'Ti ascolto',thinking:'Sto pensando…',speaking:'Ti sto parlando',success:'Bene!',error:'Riproviamo'};
  const rigFace=face=>{
    if(!face)return null;
    if(face.parentElement?.classList.contains('teacher-face-rig'))return face.parentElement;
    const parent=face.parentNode;if(!parent)return null;
    const rig=document.createElement('span');rig.className='teacher-face-rig';
    parent.insertBefore(rig,face);rig.append(face);face.classList.add('teacher-face-base');
    const jaw=face.cloneNode(true);jaw.classList.add('teacher-face-jaw');jaw.alt='';jaw.setAttribute('aria-hidden','true');
    rig.append(jaw);return rig;
  };
  const setRig=(face,state,mouthLevel=0)=>{
    const rig=rigFace(face);if(!rig)return;
    const level=state==='speaking'?Math.max(0,Math.min(1,Number(mouthLevel)||0)):0;
    rig.dataset.state=state;rig.style.setProperty('--mouth-level',String(level));
    rig.style.setProperty('--jaw-scale',String(1+level*.15));
    rig.style.setProperty('--jaw-shift',`${(level*2.4).toFixed(3)}%`);
  };
  rigFace(source);
  const apply=(state,mouthLevel=0)=>{
    const selected=labels[state]?state:'idle';setRig(source,selected,mouthLevel);
    for(const surface of surfaces()){
      const face=surface.querySelector('img:not(.teacher-face-jaw)');
      const status=surface.querySelector('[data-teacher-avatar-status]');
      surface.dataset.state=selected;
      if(status)status.textContent=labels[selected];
      setRig(face,selected,mouthLevel);
    }
  };
  const sync=()=>apply(source.dataset.state||'idle',Number(source.dataset.mouthLevel||0));
  const decorateFeedback=feedback=>{
    if(!(feedback instanceof HTMLElement) || feedback.dataset.teacherAvatarDecorated==='1')return;
    if(feedback.closest('.tutor-dialogue')){feedback.dataset.teacherAvatarDecorated='1';return;}
    const text=feedback.textContent||'';
    const surface=document.createElement('span');
    surface.className='teacher-avatar-inline';
    surface.dataset.teacherAvatarSurface='1';
    surface.dataset.state=source.dataset.state||'idle';
    surface.setAttribute('aria-hidden','true');
    const face=document.createElement('img');
    face.src='/assets/bot-tazzi.jpeg';
    face.alt='';
    face.width=84;
    face.height=84;
    const copy=document.createElement('span');
    copy.className='teacher-feedback-text';
    copy.textContent=text;
    surface.append(face);
    feedback.dataset.teacherAvatarDecorated='1';
    feedback.replaceChildren(surface,copy);
    sync();
  };
  const observer=new MutationObserver(sync);
  observer.observe(source,{attributes:true,attributeFilter:['data-state','data-mouth-level']});
  const feedbackObserver=new MutationObserver(records=>{
    for(const record of records){
      for(const node of record.addedNodes){
        if(!(node instanceof HTMLElement))continue;
        if(node.matches('p.feedback'))decorateFeedback(node);
        node.querySelectorAll?.('p.feedback').forEach(decorateFeedback);
        if(node.matches('[data-teacher-avatar-surface]')||node.querySelector?.('[data-teacher-avatar-surface]'))sync();
      }
    }
  });
  feedbackObserver.observe(document.body,{childList:true,subtree:true});
  document.querySelectorAll('p.feedback').forEach(decorateFeedback);
  sync();

  const marker='__ralfTeacherAvatarFetchWrapped';
  if(typeof globalThis.fetch==='function' && !globalThis[marker]){
    const originalFetch=globalThis.fetch.bind(globalThis);
    let pending=0;
    globalThis.fetch=async (...args)=>{
      const input=args[0];
      const url=typeof input==='string'?input:(input?.url||'');
      const apiCall=url.startsWith('/api');
      if(apiCall){pending+=1;if((source.dataset.state||'idle')==='idle')apply('thinking');}
      try{return await originalFetch(...args);}
      finally{if(apiCall){pending=Math.max(0,pending-1);if(pending===0&&surfaces().some(surface=>surface.dataset.state==='thinking'))sync();}}
    };
    globalThis[marker]=true;
  }
  return ()=>{observer.disconnect();feedbackObserver.disconnect();};
}

if(typeof document!=='undefined')queueMicrotask(()=>installBottazziAvatarSurface());
