// All text, including model and material output, uses textContent. No HTML evaluation.
import {BottazziAvatarController} from './avatar_controller.js';
const avatarImage=document.querySelector('.brand img');
const avatar=new BottazziAvatarController({onChange:controller=>{
  if(!avatarImage)return;
  avatarImage.dataset.state=controller.state;
  avatarImage.style.transform=`scaleY(${1+controller.mouth_level*.06})`;
}});
const main = document.querySelector('#main');
const notice = document.querySelector('#notice');
let home, activity, serverAudio, serverAudioCleanup, tutorVoiceGeneration=0;
const labels = {primary:'Primaria',middle:'Secondaria di primo grado',upper:'Secondaria di secondo grado',adult:'Adulto',university:'Università',postgraduate:'Post-laurea',master:'Master'};
const modes = {multiple_choice:'Scelta multipla',true_false:'Vero o falso',free_answer:'Risposta aperta',matching:'Abbinamenti',grouping:'Raggruppa',ordering:'Ordina',fill_blank:'Completa',flashcards:'Carte ripasso',memory:'Memory',definition_match:'Definizioni',sequence:'Sequenza',timed_challenge:'Sfida a tempo',guided_exercise:'Esercizio guidato',simulation:'Laboratorio'};
const tutorVoicePollDelays=[15000,30000,60000,120000,240000,480000];

function stopServerAudio(){
  if(serverAudio){serverAudio.pause();serverAudio.removeAttribute('src');serverAudio.load();serverAudio=null;}
  if(serverAudioCleanup){const cleanup=serverAudioCleanup;serverAudioCleanup=null;cleanup();}
}

function latestFeedbackNode(){
  const nodes=document.querySelectorAll('.feedback');
  return nodes.length?nodes[nodes.length-1]:null;
}

async function playTutorFeedbackVoice(url,generation,control){
  if(generation!==tutorVoiceGeneration || !url) return false;
  stopServerAudio();
  avatar.reset();
  serverAudio=new Audio(url);
  serverAudioCleanup=avatar.bindAudio(serverAudio);
  serverAudio.addEventListener('ended',()=>{if(generation===tutorVoiceGeneration)stopServerAudio();},{once:true});
  try{
    await serverAudio.play();
    if(control){control.hidden=false;control.textContent='Riascolta Bot-tazzi';}
    return true;
  }catch{
    stopServerAudio();
    if(control){control.hidden=false;control.disabled=false;control.textContent='Ascolta Bot-tazzi';}
    return false;
  }
}

async function speakTutorFeedback(result){
  const generation=++tutorVoiceGeneration;
  window.speechSynthesis?.cancel();
  stopServerAudio();
  avatar.reset();
  const voice=result?.voice;
  if(!voice?.id) return;
  const feedback=latestFeedbackNode();
  if(!feedback) return;
  const control=el('button',voice.status==='ready'?'Ascolta Bot-tazzi':'Controlla voce Bot-tazzi',{type:'button',class:'secondary tutor-voice'});
  feedback.insertAdjacentElement('afterend',control);
  const prepare=()=>api('/feedback-audio/'+encodeURIComponent(voice.id)+'/prepare',{});
  const usePrepared=async prepared=>{
    if(generation!==tutorVoiceGeneration || !control.isConnected)return true;
    if(prepared?.status==='ready'&&prepared.url){control.disabled=false;control.textContent='Ascolta Bot-tazzi';await playTutorFeedbackVoice(prepared.url,generation,control);return true;}
    if(prepared?.status!=='pending'){control.remove();return true;}
    control.disabled=false;control.textContent='Controlla voce Bot-tazzi';return false;
  };
  control.onclick=async()=>{
    if(generation!==tutorVoiceGeneration)return;
    control.disabled=true;
    try{await usePrepared(await prepare());}
    catch{if(control.isConnected){control.disabled=false;control.textContent='Riprova voce Bot-tazzi';}}
  };
  if(voice.status==='ready'&&voice.url){await playTutorFeedbackVoice(voice.url,generation,control);return;}
  for(const delay of tutorVoicePollDelays){
    await new Promise(resolve=>setTimeout(resolve,delay));
    if(generation!==tutorVoiceGeneration || !control.isConnected)return;
    try{if(await usePrepared(await prepare()))return;}catch{return;}
  }
}

export function el(tag, text, attrs={}) {
  const node=document.createElement(tag);
  if(text!==undefined && text!==null) node.textContent=text;
  for(const [k,v] of Object.entries(attrs)) node.setAttribute(k,v);
  return node;
}
function add(parent,...nodes){parent.append(...nodes);return parent;}
function button(text,fn,secondary=false){const b=el('button',text,{type:'button',...(secondary?{class:'secondary'}:{})});b.onclick=async()=>{b.disabled=true;try{await fn();}catch(e){showError(e);}finally{b.disabled=false;}};return b;}
function showError(error){notice.textContent=error.message||'Qualcosa non ha funzionato. Riprova.';notice.scrollIntoView({block:'nearest'});}
export async function api(path,data){
  const options={credentials:'same-origin',headers:{'X-Teacher-Request':'1'}};
  if(data!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(data);}
  const response=await fetch('/api'+path,options);
  let body;try{body=await response.json();}catch{throw Error('Risposta non disponibile. Riprova.');}
  if(!response.ok){if(response.status===401 && path!='/login'){navigate('/login');}throw Error(typeof body.detail==='string'?body.detail:body.error||'Controlla i campi e riprova.');}
  return body;
}

export async function apiStream(path,data,onEvent){
  const response=await fetch('/api'+path,{method:'POST',credentials:'same-origin',headers:{'X-Teacher-Request':'1','Content-Type':'application/json'},body:JSON.stringify(data)});
  if(!response.ok){let body={};try{body=await response.json();}catch{};throw Error(typeof body.detail==='string'?body.detail:body.error||'Streaming non disponibile. Riprova.');}
  if(!response.body)throw Error('Streaming non supportato dal browser.');
  const reader=response.body.getReader(),decoder=new TextDecoder();let pending='';
  while(true){const {value,done}=await reader.read();pending+=decoder.decode(value||new Uint8Array(),{stream:!done});let pos;while((pos=pending.indexOf('\n'))>=0){const line=pending.slice(0,pos).trim();pending=pending.slice(pos+1);if(line)await onEvent(JSON.parse(line));}if(done)break;}
  if(pending.trim())await onEvent(JSON.parse(pending));
}

function learnerAccess(){return home?.profile?.learner_profile?.accessibility_support||{};}
function applyLearnerAccess(){
  const a=learnerAccess(),root=document.documentElement,body=document.body;
  root.style.setProperty('--reader-font-scale',String(a.font_scale||1));
  root.style.setProperty('--reader-line-spacing',String(a.line_spacing||1.55));
  root.style.setProperty('--reader-column',(a.column_chars||72)+'ch');
  for(const [key,cls] of [['short_lines','reader-short-lines'],['line_focus','reader-line-focus'],['low_clutter','reader-low-clutter'],['enlarged_text','reader-enlarged']])body.classList.toggle(cls,!!a[key]);
}
function speakStreamSentence(text){
  if(!text||!('speechSynthesis' in window))return;
  const u=new SpeechSynthesisUtterance(text);u.lang='it-IT';u.rate=learnerAccess().text_to_speech?0.92:1;avatar.bindSpeech(u);speechSynthesis.speak(u);
}
async function streamHelp(a,mode,question,feedback){
  tutorVoiceGeneration+=1;stopServerAudio();window.speechSynthesis?.cancel();avatar.reset();avatar.thinking?.();
  const live=info('Sto preparando la spiegazione…');feedback.replaceChildren(live);let text='';let finalResult=null;
  const autoSpeak=!!learnerAccess().text_to_speech||home?.profile?.learner_profile?.education_level==='emergent_literacy';
  await apiStream('/activities/'+a.activity_id+'/help/stream',{mode,question},async event=>{
    if(event.type==='delta'){text+=event.text||'';live.textContent=text||'Sto preparando la spiegazione…';}
    else if(event.type==='voice'&&autoSpeak){speakStreamSentence(event.text||'');}
    else if(event.type==='done'){finalResult=event.result||null;}
  });
  if(finalResult){live.textContent=finalResult.feedback||text;if(!autoSpeak)speakTutorFeedback(finalResult);}
  avatar.reset();
  return finalResult;
}
function navigate(path){history.pushState({},'',path);render().then(()=>window.scrollTo(0,0)).catch(showError);}
document.addEventListener('click',e=>{const a=e.target.closest('a');if(a&&a.getAttribute('href')?.startsWith('/')&&!e.ctrlKey&&!e.metaKey){e.preventDefault();navigate(a.getAttribute('href'));}});
window.addEventListener('popstate',()=>render().catch(showError));
function heading(title,sub){main.append(el('p','IL TUO DOPOSCUOLA',{class:'eyebrow'}),el('h1',title));if(sub)main.append(el('p',sub));}
function field(form,label,name,type='text',value=''){const id='field-'+name;form.append(el('label',label,{for:id}));const input=el(type==='textarea'?'textarea':'input',null,{id,name,...(type==='textarea'?{rows:4}:{type}),required:'',maxlength:type==='textarea'?'10000':'256'});input.value=value;form.append(input);return input;}
function topicName(id){return home?.topics.find(t=>t.id===id)?.title||id;}
function info(text){return el('p',text,{class:'feedback',role:'status'});}
function stats(progress){const row=el('div',null,{class:'stats'});for(const [v,l] of [[progress.xp,'XP guadagnati'],[progress.level,'Livello personale'],[progress.streak,'Giorni di studio']])row.append(add(el('div'),el('strong',String(v)),el('span',l)));return row;}
async function start(topic,kind){activity=await api('/activities',{topic,...(kind?{activity_type:kind}:{})});navigate('/activity?id='+activity.activity_id);}

function login(){
  const form=el('form',null,{class:'card login'});add(form,el('img',null,{src:'/assets/bot-tazzi.jpeg',alt:'Bot-tazzi ti dà il benvenuto',class:'welcome-logo',width:160,height:160}),el('p','BENVENUTO',{class:'eyebrow'}),el('h1','Il tuo prossimo passo comincia qui.'),el('p','Usa la tessera e la credenziale ricevuta dal doposcuola.'));
  const card=field(form,'Tessera','card');card.autocomplete='username';const credential=field(form,'Credenziale','credential','password');credential.autocomplete='current-password';
  const submit=el('button','Entra',{type:'submit'});form.append(submit);
  form.onsubmit=async e=>{e.preventDefault();submit.disabled=true;try{await api('/login',{membership_card_id:card.value,credential:credential.value});credential.value='';navigate('/home');}catch(error){showError(error);}finally{submit.disabled=false;}};
  main.append(form);
}
function dashboard(){
  heading('Ciao, '+home.profile.display_name,'Che cosa vuoi capire oggi?');
  if(home.profile.demo)main.append(el('p','Profilo demo · dati di esempio',{class:'muted'}));
  const recommended=home.topics.find(t=>t.id==='fractions')||home.topics[0];
  const hero=el('section',null,{class:'hero'});add(hero,el('p','CONTINUA IL TUO PERCORSO',{class:'eyebrow'}),el('h2',recommended?.title||'Il tuo percorso'),el('p','Esplora un concetto, prova e scopri il prossimo passo.'));
  if(home.resume)hero.append(button('Riprendi attività',()=>navigate('/activity?id='+home.resume)));
  else if(recommended)hero.append(button('Cominciamo',()=>start(recommended.id)));
  main.append(hero);
  const mission=el('section',null,{class:'card'});const d=home.progress.daily;add(mission,el('h2','Missione di oggi'),el('p',`${d.completed} / ${d.target} attività diverse completate`),el('progress',null,{max:d.target,value:Math.min(d.target,d.completed),'aria-label':'Missione quotidiana'}),el('small','Scegli il tuo ritmo. Anche un solo passo conta.'));main.append(mission);
  main.append(el('h2','Le tue materie'));const grid=el('div',null,{class:'grid'});
  for(const subject of [...new Set(home.topics.map(t=>t.subject))]){const topics=home.topics.filter(t=>t.subject===subject);const card=add(el('section',null,{class:'card'}),el('h3',subject[0].toUpperCase()+subject.slice(1)));for(const t of topics)card.append(button(t.title,()=>start(t.id),true));grid.append(card);}main.append(grid,stats(home.progress));
  main.append(button('I miei libri',()=>navigate('/books'),true),button('I miei badge',()=>navigate('/badges'),true));
}
function study(onlyQuiz=false,onlySimulation=false){
  heading(onlySimulation?'Esplora e scopri':onlyQuiz?'Mettiti alla prova':'Scegli il prossimo passo',onlySimulation?'Prima prevedi. Poi modifica e osserva. Infine spiega.':'Attività brevi, costruite sui tuoi argomenti.');
  const grid=el('div',null,{class:'grid'});
  for(const topic of home.topics){if(onlySimulation&&!['fractions','motion'].includes(topic.id))continue;
    const card=add(el('section',null,{class:'card'}),el('h2',topic.title),el('p',topic.learning_objectives[0]));
    if(!onlyQuiz&&!onlySimulation)card.append(button('Percorso consigliato',()=>start(topic.id)));
    const select=el('select',null,{'aria-label':'Modalità per '+topic.title});const allowed=new Set(topic.suggested_activity_types||Object.keys(modes));
    for(const [value,label] of Object.entries(modes)){if(!allowed.has(value))continue;if(value==='simulation'&&!['fractions','motion'].includes(topic.id))continue;if(onlySimulation&&value!=='simulation')continue;if(onlyQuiz&&value!=='multiple_choice')continue;select.append(el('option',label,{value}));}
    add(card,select,button(onlySimulation?'Apri laboratorio':'Inizia',()=>start(topic.id,select.value),true));grid.append(card);
  }main.append(grid);
  if(!onlySimulation&&!onlyQuiz)main.append(button('Prepara un piano di 15 minuti',async()=>{const plan=await api('/study-plan',{minutes:15});main.append(info(plan.explanation));for(const item of plan.activities)main.append(button(topicName(item.topic),()=>start(item.topic,item.activity_type)));},true));
}

export function choiceRenderer(a,container){
  let value='';for(const [i,choice] of a.choices.entries()){const id='choice-'+i;const label=el('label',null,{class:'choice',for:id});const input=el('input',null,{type:'radio',id,name:'choice',value:choice});input.onchange=()=>{value=choice;};add(label,input,el('span',choice));container.append(label);}return()=>value;
}
export function pairsRenderer(a,container){
  const controls=[];for(const [i,item] of a.items.entries()){container.append(el('label',item,{for:'pair-'+i}));const select=el('select',null,{id:'pair-'+i});select.append(el('option','Scegli…',{value:''}));for(const c of a.choices)select.append(el('option',c,{value:c}));container.append(select);controls.push(select);}return()=>controls.map(c=>c.value);
}
export function orderingRenderer(a,container){
  const order=[...a.items];const draw=()=>{container.replaceChildren();order.forEach((text,i)=>{const row=el('div',null,{class:'order-item'});add(row,el('span',text),button('↑',()=>{if(i){[order[i-1],order[i]]=[order[i],order[i-1]];draw();}},true),button('↓',()=>{if(i<order.length-1){[order[i+1],order[i]]=[order[i],order[i+1]];draw();}},true));row.querySelectorAll('button')[0].setAttribute('aria-label','Sposta '+text+' prima');row.querySelectorAll('button')[1].setAttribute('aria-label','Sposta '+text+' dopo');container.append(row);});};draw();return()=>[...order];
}
export function freeRenderer(a,container){const input=field(container,a.activity_type==='fill_blank'?'Completa lo spazio':'La tua risposta','answer','textarea');input.maxLength=4000;return()=>input.value;}
function flashcardsRenderer(a,container){let index=0,flipped=false;const counter=el('p');const flip=button('',()=>{flipped=!flipped;draw();});flip.className='flash';const draw=()=>{counter.textContent=`Carta ${index+1} di ${a.items.length}`;flip.textContent=flipped?a.backs[index]:a.items[index];};add(container,counter,flip,button('Carta successiva',()=>{index=(index+1)%a.items.length;flipped=false;draw();},true),el('p','Le carte aiutano a ripassare. Verifica la comprensione con un quiz: girarle non assegna punti.',{class:'muted'}),button('Prova un quiz',()=>start(a.topic,'multiple_choice')));draw();return()=>'';}
function memoryRenderer(a,container){
  // Matching is evaluated on the server; flipping cards carries no learning reward.
  const deck=[...a.items.map(x=>({text:x,side:'Concetto'})),...a.choices.map(x=>({text:x,side:'Rappresentazione'}))];
  const grid=el('div',null,{class:'memory-grid'});deck.forEach((card,i)=>{let flipped=false;const b=button('Carta '+(i+1),()=>{flipped=!flipped;b.textContent=flipped?card.text:'Carta '+(i+1);b.setAttribute('aria-pressed',String(flipped));},true);b.setAttribute('aria-label',card.side+' '+(i+1));grid.append(b);});container.append(grid,el('p','Ricorda le carte, poi associa i concetti.'));return pairsRenderer(a,container);
}
export const renderers={multiple_choice:choiceRenderer,true_false:choiceRenderer,free_answer:freeRenderer,matching:pairsRenderer,grouping:pairsRenderer,ordering:orderingRenderer,fill_blank:freeRenderer,flashcards:flashcardsRenderer,memory:memoryRenderer,definition_match:pairsRenderer,sequence:orderingRenderer,timed_challenge:choiceRenderer,guided_exercise:freeRenderer};

function simulationRenderer(a,container){
  let flow=a.simulation||{};const prediction=field(container,'Che cosa prevedi che succederà cambiando il parametro?','prediction','textarea');prediction.maxLength=500;prediction.value=flow.prediction||'';prediction.disabled=!!flow.prediction;
  const controls=el('section',null,{class:flow.prediction?'':'hidden'});const visual=el('div',null,{class:a.topic==='fractions'?'fraction':'track','aria-label':'Risultato visuale'});
  const rangeInputs={};const ranges=a.topic==='fractions'?[['numerator','Parti colorate',0,12,1],['denominator','Parti totali',1,12,2]]:[['speed','Velocità (m/s)',0,20,2],['time','Tempo (s)',0,20,3]];
  for(const [name,label,min,max,value] of ranges){const input=field(controls,label,name,'range',String(flow.variables?.[name]??value));input.min=min;input.max=max;input.step=1;const display=el('output',input.value,{for:input.id});input.oninput=()=>{display.textContent=input.value;};controls.append(display);rangeInputs[name]=input;}
  const observed=el('div',null,{'aria-live':'polite'});const draw=()=>{if(!flow.observation)return;observed.replaceChildren(info(flow.observation.label),el('p',flow.observation.question));visual.replaceChildren();const fraction=a.topic==='fractions';const total=fraction?flow.variables.denominator:20;const filled=fraction?flow.variables.numerator:Math.round(flow.observation.value/20);for(let i=0;i<(fraction?12:20);i++){const cell=el('span',null,{class:i<filled?'filled':''});if(fraction&&i>=total)cell.hidden=true;visual.append(cell);}visual.setAttribute('aria-label',flow.observation.label);};
  const save=button('Salva previsione',async()=>{flow=await api('/activities/'+a.activity_id+'/simulation',{prediction:prediction.value});prediction.disabled=true;save.hidden=true;controls.classList.remove('hidden');});save.hidden=!!flow.prediction;
  add(container,save,controls);controls.append(button('Osserva il risultato',async()=>{const variables=Object.fromEntries(Object.entries(rangeInputs).map(([k,v])=>[k,Number(v.value)]));flow=await api('/activities/'+a.activity_id+'/simulation',{variables});draw();}),visual,observed);draw();
  container.append(el('h3','Spiega ciò che hai osservato'));return freeRenderer(a,container);
}

async function activityPage(){
  const id=new URLSearchParams(location.search).get('id');if(!id){study();return;}
  activity=await api('/activities/'+encodeURIComponent(id));const a=activity;
  const section=el('section',null,{class:'activity'});add(section,el('p',topicName(a.topic)+' · '+modes[a.activity_type],{class:'eyebrow'}),el('h1',a.instructions),el('p',a.learning_objective,{class:'muted'}));
  if(a.source==='original_fallback')section.append(el('p','Attività del percorso disponibile anche mentre il tutor si prepara.',{class:'muted'}));
  if(a.completed){section.append(info('Attività già completata.'),button('Prossima attività',()=>start(a.topic)));main.append(section);return;}
  const interaction=el('div',null,{class:'card'});const getter=a.activity_type==='simulation'?simulationRenderer(a,interaction):renderers[a.activity_type](a,interaction);section.append(interaction);
  if(a.activity_type==='timed_challenge'){
    const timer=el('p','Sfida personale: due minuti, al tuo ritmo.',{role:'timer','aria-label':'Tempo della sfida'});
    const begin=button('Avvia sfida',()=>{begin.hidden=true;let left=120;const interval=setInterval(()=>{if(!timer.isConnected){clearInterval(interval);return;}left-=1;timer.textContent=left>0?`Tempo: ${Math.floor(left/60)}:${String(left%60).padStart(2,'0')}`:'Tempo concluso. Puoi comunque terminare con calma.';if(left<=0)clearInterval(interval);},1000);});
    section.append(timer,begin,el('small','Il tempo non cambia la valutazione né toglie punti.'));
  }
  const feedback=el('div',null,{'aria-live':'polite'});
  let attemptKey=crypto.randomUUID();
  if(a.activity_type!=='flashcards'){const send=button('Invia risposta',async()=>{const result=await api('/activities/'+a.activity_id+'/answer',{answer:getter(),request_key:attemptKey});attemptKey=crypto.randomUUID();feedback.replaceChildren(info(result.feedback),el('p',(result.correct?'Risposta corretta. ':'Proviamo insieme. ')+`+${result.xp_awarded} XP`));speakTutorFeedback(result);if(result.correct){send.hidden=true;feedback.append(button('Prossima attività',()=>start(result.next?.topic||a.topic,result.next?.activity_type)),button('Vedi progressi',()=>navigate('/progress'),true));}else{feedback.append(el('p','Puoi correggere la risposta e inviarla di nuovo.'));if(result.attempts>=5){send.hidden=true;feedback.append(button('Nuova attività guidata',()=>start(a.topic,'guided_exercise')));}}});section.append(send);}
  const help=el('div',null,{class:'row'});for(const [mode,label] of [['hint','Suggerimento'],['different','Spiegamelo diversamente'],['explain','Fammi un esempio']])help.append(button(label,async()=>{await streamHelp(a,mode,'',feedback);},true));section.append(help,feedback);
  const chat=el('details',null,{class:'card'});chat.append(el('summary','Non ho capito: chiedi al tutor'));const question=field(chat,'La tua domanda','question','textarea');question.maxLength=2000;chat.append(button('Chiedi',async()=>{await streamHelp(a,'explain',question.value,feedback);}));section.append(chat,button('Scegli un’altra attività',()=>navigate('/study'),true));main.append(section);
}
async function progressPage(badgesOnly=false){
  const p=await api('/progress');heading(badgesOnly?'I tuoi traguardi':'Guarda quanta strada hai fatto');main.append(stats(p));
  if(!badgesOnly){for(const t of p.topics){const card=el('section',null,{class:'card'});add(card,el('h2',topicName(t.topic)),el('p',t.status.replaceAll('_',' ')),el('progress',null,{max:100,value:t.mastery,'aria-label':'Padronanza: '+t.status.replaceAll('_',' ')}),el('p',`${t.success_count} risposte corrette su ${t.attempt_count} tentativi`),button(t.needs_review?'Ripassa':'Continua',()=>start(t.topic),true));main.append(card);}if(!p.topics.length)main.append(el('p','Le tue prime attività appariranno qui.'));}
  main.append(el('h2','Badge'));if(!p.badges.length)main.append(el('p','Il primo passo arriva completando un’attività.'));for(const badge of p.badges)main.append(el('span',badge,{class:'badge'}));
}
async function books(){
  heading('I miei libri','Appunti e capitoli autorizzati, collegati al tuo percorso.');const materials=await api('/materials');
  const results=el('div',null,{'aria-live':'polite'});
  for(const material of materials){const card=add(el('section',null,{class:'card'}),el('h2',material.title),el('p',material.chapter+' · '+material.pages),el('p',material.topics.map(topicName).join(' · ')||'Nessun argomento del percorso riconosciuto'));
    for(const [action,label] of [['explain','Spiegami'],['summarize','Riassumi'],['exercise','Esercitati'],['quiz','Quiz'],['audio','Ascolta']])card.append(button(label,async()=>{const out=await api('/materials/'+material.id,{action});if(out.activity_id){navigate('/activity?id='+out.activity_id);}else if(out.audio_id){navigate('/audio');}else{results.replaceChildren(info(out.feedback));speakTutorFeedback(out);}},true));main.append(card);
  }main.append(results);
  const form=el('form',null,{class:'card'});form.append(el('h2','Aggiungi materiale'));const title=field(form,'Titolo','title');const text=field(form,'Testo o estratto autorizzato (massimo 10.000 caratteri)','text','textarea');const file=el('input',null,{type:'file',accept:'.txt,text/plain','aria-label':'Importa un file di testo'});file.onchange=async()=>{const f=file.files[0];if(f){if(f.size>20000){showError(Error('File troppo grande.'));return;}text.value=await f.text();}};form.append(file);const chapter=field(form,'Capitolo o sezione','chapter');chapter.required=false;const rights=el('select',null,{'aria-label':'Diritti sul materiale',required:''});for(const [value,label] of [['','Seleziona i diritti'],['own','Testo scritto da me'],['authorized','Ho autorizzazione per questi usi'],['public_domain','Pubblico dominio'],['compatible_license','Licenza compatibile']])rights.append(el('option',label,{value}));form.append(el('label','Diritti sul materiale'),rights,el('p','Carica solo materiale che puoi utilizzare. Nessuna riproduzione integrale di libri protetti.',{class:'muted'}),el('button','Aggiungi',{type:'submit'}));form.onsubmit=async e=>{e.preventDefault();const b=form.querySelector('button');b.disabled=true;try{await api('/materials',{title:title.value,text:text.value,rights:rights.value,chapter:chapter.value});await render();}catch(error){showError(error);}finally{b.disabled=false;}};main.append(form);
}
async function audioPage(){
  heading('Ascolta e ripassa','Bot-tazzi usa la voce Peppone quando è pronta; nel frattempo parte subito la voce del dispositivo.');
  const assets=await api('/audio');if(!assets.length)main.append(el('p','Apri un libro e scegli “Ascolta” per preparare la lettura.'));
  for(const asset of assets){const card=add(el('section',null,{class:'card'}),el('h2',asset.title));const select=el('select',null,{'aria-label':'Capitolo audio'});asset.tracks.forEach((t,i)=>select.append(el('option','Capitolo '+(i+1),{value:i})));select.value=asset.chapter;
    const speed=el('select',null,{'aria-label':'Velocità di lettura'});for(const rate of [.75,1,1.25,1.5])speed.append(el('option',rate+'×',{value:rate}));speed.value=1;
    const text=el('p',asset.tracks[asset.chapter]?.text||'');let position=asset.position||0;
    select.onchange=()=>{window.speechSynthesis?.cancel();stopServerAudio();avatar.reset();position=0;text.textContent=asset.tracks[Number(select.value)].text;};
    const save=()=>api('/audio/'+asset.id+'/position',{chapter:Number(select.value),position:Number(position)});
    const playBrowser=()=>{if(!('speechSynthesis' in window)||!speechSynthesis.getVoices().length)throw Error('Voce non disponibile sul dispositivo. Il testo è pronto; Bot-tazzi sta preparando l’audio.');stopServerAudio();speechSynthesis.cancel();avatar.reset();const track=asset.tracks[Number(select.value)];const offset=Math.min(position,track.text.length);const utterance=new SpeechSynthesisUtterance(track.text.slice(offset));avatar.bindSpeech(utterance);utterance.lang='it-IT';utterance.rate=Number(speed.value);utterance.onboundary=e=>{position=offset+e.charIndex;};utterance.onend=()=>{position=0;save().catch(showError);};speechSynthesis.speak(utterance);};
    const playServer=async url=>{window.speechSynthesis?.cancel();stopServerAudio();avatar.reset();position=0;serverAudio=new Audio(url);serverAudio.playbackRate=Number(speed.value);serverAudioCleanup=avatar.bindAudio(serverAudio);serverAudio.addEventListener('ended',()=>{position=0;save().catch(showError);stopServerAudio();},{once:true});try{await serverAudio.play();}catch(error){stopServerAudio();throw Error('Audio di Bot-tazzi non disponibile. Riprova.');}};
    const play=async()=>{const chapter=Number(select.value);const prepared=await api('/audio/'+asset.id+'/prepare',{chapter});if(position===0&&prepared.status==='ready'&&prepared.url){await playServer(prepared.url);return;}playBrowser();};
    add(card,select,speed,text,button('Riproduci',play),button('Pausa',async()=>{if(serverAudio&&!serverAudio.paused)serverAudio.pause();else window.speechSynthesis?.pause();await save();},true),button('Riprendi',async()=>{if(serverAudio?.paused)await serverAudio.play();else if(window.speechSynthesis?.paused)speechSynthesis.resume();else await play();},true),el('p','La preparazione Peppone avviene in background. Se non è ancora pronta, la lettura browser parte senza attese.',{class:'muted'}));main.append(card);
  }
}
async function profilePage(){
  heading('Il tuo profilo','Qui scegli come accedere ai contenuti. Queste sono preferenze didattiche, non diagnosi.');
  const profile=structuredClone(home.profile.learner_profile||{}),access=profile.accessibility_support||{};
  main.append(el('p',home.profile.display_name),el('p',(labels[home.profile.school_level]||home.profile.school_level)+' · livello '+home.profile.grade));
  const form=el('form',null,{class:'card reader-settings'});form.append(el('h2','Reader e accessibilità'));
  const checks=[['text_to_speech','Leggi automaticamente le spiegazioni'],['speech_to_text','Preferisco poter rispondere a voce'],['short_lines','Periodi e righe più brevi'],['line_focus','Focus su una riga/idea alla volta'],['enlarged_text','Testo ingrandito'],['low_clutter','Riduci elementi non essenziali'],['synchronized_highlight','Evidenziazione sincronizzata'],['alternative_response_modes','Mostra modalità di risposta alternative']];
  for(const [key,label] of checks){const id='access-'+key;const row=el('label',null,{class:'toggle',for:id});const input=el('input',null,{type:'checkbox',id});input.checked=!!access[key];input.onchange=()=>{access[key]=input.checked;};row.append(input,el('span',label));form.append(row);}
  const scale=el('input',null,{type:'range',min:'0.8',max:'2',step:'0.1',value:String(access.font_scale||1),'aria-label':'Dimensione testo'});scale.oninput=()=>{access.font_scale=Number(scale.value);};form.append(el('label','Dimensione testo'),scale);
  const spacing=el('input',null,{type:'range',min:'1',max:'3',step:'0.1',value:String(access.line_spacing||1.5),'aria-label':'Interlinea'});spacing.oninput=()=>{access.line_spacing=Number(spacing.value);};form.append(el('label','Interlinea'),spacing);
  const mode=el('select',null,{'aria-label':'Modalità sessione'});for(const [v,l] of [['auto','Adattiva'],['micro','Micro'],['standard','Standard'],['doposcuola','Doposcuola'],['exam','Esame'],['scholar','Scholar'],['literacy_l2','Literacy / L2']])mode.append(el('option',l,{value:v}));mode.value=profile.session_preference||'auto';mode.onchange=()=>{profile.session_preference=mode.value;};form.append(el('label','Modalità di studio'),mode);
  profile.accessibility_support=access;form.append(button('Salva preferenze',async()=>{home.profile.learner_profile=await api('/learner-profile',profile);applyLearnerAccess();notice.textContent='Preferenze salvate.';}));main.append(form);
  if(['university','postgraduate','master'].includes(home.profile.school_level))main.append(el('section',null,{class:'card scholar-card'}),button('Apri i materiali Scholar',()=>navigate('/books')));
  main.append(el('p','La tessera identifica il tuo profilo. La credenziale protegge l’accesso.'),button('Esci',async()=>{await api('/logout',{});window.speechSynthesis?.cancel();stopServerAudio();navigate('/login');},true));
}

async function render(){
  tutorVoiceGeneration+=1;
  window.speechSynthesis?.cancel();
  stopServerAudio();
  avatar.reset();
  notice.textContent='';main.replaceChildren(el('p','Un momento…'));const path=location.pathname;
  document.querySelectorAll('nav a').forEach(a=>{if(a.getAttribute('href')===path)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  if(path==='/login'){main.replaceChildren();login();return;}
  home=await api('/home');applyLearnerAccess();main.replaceChildren();
  if(path==='/'||path==='/home')dashboard();else if(path==='/study')study();else if(path==='/quiz')study(true);else if(path==='/simulations')study(false,true);else if(path==='/activity')await activityPage();else if(path==='/progress')await progressPage();else if(path==='/badges')await progressPage(true);else if(path==='/books')await books();else if(path==='/audio')await audioPage();else if(path==='/profile')await profilePage();
}
render().catch(showError);
