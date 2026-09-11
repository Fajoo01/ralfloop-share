/* Stable avatar contract; visual skin is intentionally replaceable. */
export class BottazziAvatarController {
  constructor({avatar_enabled=true, reduced_motion=globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false, onChange=()=>{}}={}) { this.enabled=avatar_enabled; this.reducedMotion=reduced_motion; this.onChange=onChange; this.state='idle'; this.mouth_level=0; this.frame=null; }
  setState(state) {
    if (!['idle','speaking','listening','thinking','success','error'].includes(state)) throw new Error('invalid_avatar_state');
    this.stop(); this.state=state; if(this.enabled)this.onChange(this);
    if(state==='speaking' && this.enabled && !this.reducedMotion){
      const tick=t=>{let level=.25+.55*Math.abs(Math.sin(t/110));if(this.analyser){this.analyser.getByteTimeDomainData(this.samples);level=Math.sqrt(this.samples.reduce((s,x)=>s+((x-128)/128)**2,0)/this.samples.length)*4;}this.setMouthLevel(level);this.frame=requestAnimationFrame(tick);};
      this.frame=requestAnimationFrame(tick);
    }
  }
  stop(){if(this.frame!==null)cancelAnimationFrame(this.frame);this.frame=null;this.mouth_level=0;}
  setMouthLevel(level) { this.mouth_level=this.enabled&&!this.reducedMotion?Math.max(0,Math.min(1,Number(level)||0)):0; if(this.enabled) this.onChange(this); }
  bindSpeech(utterance){
    for(const event of ['start','resume'])utterance.addEventListener(event,()=>this.setState('speaking'));
    for(const event of ['end','error','pause'])utterance.addEventListener(event,()=>this.reset());
  }
  bindRecognition(recognition){recognition.addEventListener('start',()=>this.setState('listening'));for(const event of ['end','error'])recognition.addEventListener(event,()=>this.reset());}
  bindAudio(audio){
    const context=new AudioContext(), source=context.createMediaElementSource(audio);
    const analyser=context.createAnalyser();analyser.fftSize=256;
    this.analyser=analyser;this.samples=new Uint8Array(analyser.fftSize);
    source.connect(analyser);analyser.connect(context.destination);
    const start=()=>{context.resume().catch(()=>{});this.setState('speaking');},end=()=>this.reset();
    audio.addEventListener('playing',start);for(const event of ['pause','ended','error'])audio.addEventListener(event,end);
    return ()=>{audio.removeEventListener('playing',start);for(const event of ['pause','ended','error'])audio.removeEventListener(event,end);this.reset();source.disconnect();analyser.disconnect();this.analyser=null;context.close();};
  }
  reset() { this.stop();this.state='idle'; if(this.enabled)this.onChange(this); }
}
