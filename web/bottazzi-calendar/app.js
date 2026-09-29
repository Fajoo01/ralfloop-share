const state={view:'month',cursor:new Date(),events:[],mini:new Date(),query:'',selected:null,drag:null};
const $=id=>document.getElementById(id),pad=n=>String(n).padStart(2,'0'),DAY=86400000,HOUR_PX=52;
const isMobile=()=>window.matchMedia('(max-width:850px)').matches;
const isoLocal=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`,dateOnly=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
function localDate(v){const [y,m,d]=v.split('-').map(Number);return new Date(y,m-1,d)} function startOfWeek(d){const x=new Date(d),day=(x.getDay()+6)%7;x.setDate(x.getDate()-day);x.setHours(0,0,0,0);return x} function sameDay(a,b){return a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()&&a.getDate()===b.getDate()} function fmtMonth(d){return d.toLocaleDateString('it-IT',{month:'long',year:'numeric'})}
function dayStart(d){return new Date(d.getFullYear(),d.getMonth(),d.getDate())} function dayEnd(d){const x=dayStart(d);x.setDate(x.getDate()+1);return x} function allDaySpanDays(a,b){const ua=Date.UTC(a.getFullYear(),a.getMonth(),a.getDate()),ub=Date.UTC(b.getFullYear(),b.getMonth(),b.getDate());return Math.max(1,Math.round((ub-ua)/DAY))} function unfoldIcs(text){return text.replace(/\r?\n[ \t]/g,'')} function unescapeIcs(s=''){return s.replace(/\\n/gi,'\n').replace(/\\,/g,',').replace(/\\;/g,';').replace(/\\\\/g,'\\')}
function parseDate(raw){if(!raw)return null;const v=raw.trim();if(/^\d{8}$/.test(v))return new Date(+v.slice(0,4),+v.slice(4,6)-1,+v.slice(6,8));const m=v.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$/);if(!m)return new Date(v);const p=m.slice(1,7).map(Number);return m[7]?new Date(Date.UTC(p[0],p[1]-1,p[2],p[3],p[4],p[5])):new Date(p[0],p[1]-1,p[2],p[3],p[4],p[5])}
function sourceOf(e){const m=e.description.match(/source=([a-z0-9_.-]+)/i);return m?m[1]:(e.id.startsWith('ui-')?'manual':'calendar')}
function parseIcs(text){return unfoldIcs(text).split('BEGIN:VEVENT').slice(1).map((chunk,i)=>{const body=chunk.split('END:VEVENT')[0],lines=body.split(/\r?\n/),o={id:'e'+i,title:'(senza titolo)',description:'',allDay:false,status:''};for(const line of lines){const p=line.indexOf(':');if(p<0)continue;const left=line.slice(0,p),value=line.slice(p+1),key=left.split(';')[0].toUpperCase();if(key==='UID')o.id=value.trim();if(key==='SUMMARY')o.title=unescapeIcs(value.trim());if(key==='DESCRIPTION')o.description=unescapeIcs(value.trim());if(key==='STATUS')o.status=value.trim();if(key==='DTSTART'){o.allDay=/VALUE=DATE/i.test(left)||/^\d{8}$/.test(value.trim());o.start=parseDate(value)}if(key==='DTEND')o.end=parseDate(value)}if(o.start&&!o.end)o.end=new Date(o.start.getTime()+(o.allDay?DAY:3600000));o.source=sourceOf(o);return o}).filter(e=>e.start&&e.status!=='CANCELLED')}
async function loadEvents(){const r=await fetch('/calendar.ics?ts='+Date.now(),{cache:'no-store'});if(!r.ok)throw new Error('feed');state.events=parseIcs(await r.text())} function visibleEvents(){const q=state.query.trim().toLocaleLowerCase('it');return q?state.events.filter(e=>(e.title+' '+e.description+' '+e.source).toLocaleLowerCase('it').includes(q)):state.events} function eventsOnDay(d){const s=dayStart(d),e=dayEnd(d);return visibleEvents().filter(x=>x.end>s&&x.start<e).sort((a,b)=>(b.allDay-a.allDay)||(a.start-b.start))}
function titleFor(){if(state.view==='month')return fmtMonth(state.cursor);if(state.view==='week'){const s=isMobile()?dayStart(state.cursor):startOfWeek(state.cursor),e=new Date(s);e.setDate(e.getDate()+(isMobile()?2:6));return `${s.getDate()} ${s.toLocaleDateString('it-IT',{month:'short'})} – ${e.getDate()} ${e.toLocaleDateString('it-IT',{month:'short',year:'numeric'})}`}if(state.view==='agenda')return 'Programmazione';return state.cursor.toLocaleDateString('it-IT',{weekday:'long',day:'numeric',month:'long'})}
function render(){ $('periodTitle').textContent=titleFor();document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===state.view));renderMini();if(state.view==='month')renderMonth();else if(state.view==='agenda')renderAgenda();else renderTimeView(state.view==='week'?(isMobile()?3:7):1)}
function renderMini(){const d=new Date(state.mini.getFullYear(),state.mini.getMonth(),1);$('miniTitle').textContent=fmtMonth(d);const grid=$('miniCalendar');grid.innerHTML='';const start=startOfWeek(d);for(let i=0;i<42;i++){const x=new Date(start);x.setDate(start.getDate()+i);const b=document.createElement('button');b.textContent=x.getDate();if(x.getMonth()!==d.getMonth())b.classList.add('other');if(sameDay(x,new Date()))b.classList.add('today');if(sameDay(x,state.cursor))b.classList.add('selected');b.onclick=()=>{state.cursor=new Date(x);state.mini=new Date(x);render()};grid.appendChild(b)}}
function eventButton(e){const b=document.createElement('button');b.className=`event-pill source-${e.source}`+(e.allDay?' all-day':'');b.textContent=(e.allDay?'':e.start.toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'})+' ')+e.title;b.draggable=true;b.ondragstart=ev=>{ev.stopPropagation();state.drag=e};b.onclick=ev=>{ev.stopPropagation();showEvent(e)};return b}
function shiftEventToDay(e,d){const dur=e.end-e.start;if(e.allDay){const s=dayStart(d),end=new Date(s);end.setDate(end.getDate()+allDaySpanDays(e.start,e.end));return {...e,start:s,end,allDay:true}}const s=new Date(d.getFullYear(),d.getMonth(),d.getDate(),e.start.getHours(),e.start.getMinutes());return {...e,start:s,end:new Date(s.getTime()+dur)}}
function renderMonth(){const cal=$('calendar');cal.innerHTML='';cal.className='month';const dow=document.createElement('div');dow.className='dow-row';['LUN','MAR','MER','GIO','VEN','SAB','DOM'].forEach(x=>{const d=document.createElement('div');d.textContent=x;dow.appendChild(d)});cal.appendChild(dow);const grid=document.createElement('div');grid.className='month-grid';const first=new Date(state.cursor.getFullYear(),state.cursor.getMonth(),1),start=startOfWeek(first);for(let i=0;i<42;i++){const d=new Date(start);d.setDate(start.getDate()+i);const cell=document.createElement('div');cell.className='day-cell';if(d.getMonth()!==state.cursor.getMonth())cell.classList.add('other');if(sameDay(d,new Date()))cell.classList.add('today');cell.onclick=ev=>{if(ev.target.closest('.event-pill'))return;openEditor(null,new Date(d.getFullYear(),d.getMonth(),d.getDate(),9,0))};cell.ondragover=ev=>ev.preventDefault();cell.ondrop=async ev=>{ev.preventDefault();ev.stopPropagation();if(!state.drag)return;try{await saveEvent(shiftEventToDay(state.drag,d))}finally{state.drag=null}};const n=document.createElement('div');n.className='day-num';n.textContent=d.getDate();cell.appendChild(n);const evs=eventsOnDay(d);evs.slice(0,4).forEach(e=>cell.appendChild(eventButton(e)));if(evs.length>4){const m=document.createElement('div');m.className='more';m.textContent=`+${evs.length-4} altri`;m.onclick=ev=>{ev.stopPropagation();state.cursor=new Date(d);state.view='agenda';render()};cell.appendChild(m)}grid.appendChild(cell)}cal.appendChild(grid);$('loading').style.display='none'}
function makeAllDayRow(start,cols){const row=document.createElement('div');row.className='all-day-row-grid';row.style.setProperty('--cols',cols);const label=document.createElement('div');label.className='all-day-label';label.textContent='tutto il giorno';row.appendChild(label);for(let i=0;i<cols;i++){const d=new Date(start);d.setDate(start.getDate()+i);const c=document.createElement('div');c.className='all-day-cell';eventsOnDay(d).filter(e=>e.allDay).forEach(e=>c.appendChild(eventButton(e)));c.ondragover=ev=>ev.preventDefault();c.ondrop=async ev=>{ev.preventDefault();if(!state.drag)return;const days=state.drag.allDay?allDaySpanDays(state.drag.start,state.drag.end):1,s=dayStart(d),end=new Date(s);end.setDate(end.getDate()+days);try{await saveEvent({...state.drag,start:s,end,allDay:true})}finally{state.drag=null}};c.onclick=ev=>{if(ev.target===c)openEditor(null,dayStart(d),true)};row.appendChild(c)}return row}
function renderTimeView(cols){
  const cal=$('calendar');cal.innerHTML='';cal.className=cols===7?'week':'day-view';cal.style.setProperty('--cols',cols);
  const head=document.createElement('div');head.className='time-head';head.appendChild(document.createElement('div'));
  const start=cols===7?startOfWeek(state.cursor):dayStart(state.cursor);
  for(let i=0;i<cols;i++){const d=new Date(start);d.setDate(start.getDate()+i);const h=document.createElement('div');h.className='head-day'+(sameDay(d,new Date())?' today':'');h.innerHTML=`${d.toLocaleDateString('it-IT',{weekday:'short'}).toUpperCase()}<strong>${d.getDate()}</strong>`;head.appendChild(h)}
  cal.appendChild(head);cal.appendChild(makeAllDayRow(start,cols));
  const body=document.createElement('div');body.className='time-grid';body.style.setProperty('--cols',cols);
  const hours=document.createElement('div');hours.className='hours';for(let h=0;h<24;h++){const l=document.createElement('div');l.className='hour-label';l.textContent=pad(h)+':00';hours.appendChild(l)}body.appendChild(hours);
  for(let i=0;i<cols;i++){const d=new Date(start);d.setDate(start.getDate()+i);body.appendChild(makeTimeColumn(d))}
  cal.appendChild(body);$('loading').style.display='none';
  setTimeout(()=>{const main=document.querySelector('.main');if(main.scrollTop<120)main.scrollTop=Math.max(0,(new Date().getHours()-2)*HOUR_PX)},0);
}
function makeTimeColumn(d){
  const col=document.createElement('div');col.className='day-column';
  col.onclick=ev=>{if(ev.target.closest('.timed-event'))return;const rect=col.getBoundingClientRect(),mins=Math.max(0,Math.min(1439,Math.round(((ev.clientY-rect.top)/HOUR_PX*60)/15)*15));openEditor(null,new Date(d.getFullYear(),d.getMonth(),d.getDate(),Math.floor(mins/60),mins%60))};
  col.ondragover=ev=>ev.preventDefault();
  col.ondrop=async ev=>{ev.preventDefault();if(!state.drag)return;const rect=col.getBoundingClientRect(),mins=Math.max(0,Math.min(1439,Math.round(((ev.clientY-rect.top)/HOUR_PX*60)/15)*15)),dur=state.drag.allDay?3600000:state.drag.end-state.drag.start,s=new Date(d.getFullYear(),d.getMonth(),d.getDate(),Math.floor(mins/60),mins%60);try{await saveEvent({...state.drag,start:s,end:new Date(s.getTime()+dur),allDay:false})}finally{state.drag=null}};
  for(let h=0;h<24;h++){const line=document.createElement('div');line.className='hour-line';col.appendChild(line)}
  const ds=dayStart(d),de=dayEnd(d),segments=[];eventsOnDay(d).filter(e=>!e.allDay).forEach(e=>{const s=new Date(Math.max(e.start.getTime(),ds.getTime())),en=new Date(Math.min(e.end.getTime(),de.getTime()));if(en>s)segments.push({e,s,en})});
  layoutTimedSegments(segments).forEach(x=>col.appendChild(makeTimedEvent(x.e,x.s,x.en,de,x.col,x.cols)));
  if(sameDay(d,new Date())){const now=new Date(),line=document.createElement('div');line.className='now-line';line.style.top=((now.getHours()+now.getMinutes()/60)*HOUR_PX)+'px';col.appendChild(line)}
  return col;
}
function layoutTimedSegments(items){const sorted=[...items].sort((a,b)=>a.s-b.s||a.en-b.en),out=[];let cluster=[],clusterEnd=null;const flush=()=>{if(!cluster.length)return;const ends=[];for(const x of cluster){let c=ends.findIndex(end=>end<=x.s);if(c<0)c=ends.length;ends[c]=x.en;x.col=c}const cols=Math.max(1,ends.length);cluster.forEach(x=>{x.cols=cols;out.push(x)});cluster=[];clusterEnd=null};for(const x of sorted){if(cluster.length&&x.s>=clusterEnd)flush();cluster.push(x);clusterEnd=!clusterEnd||x.en>clusterEnd?x.en:clusterEnd}flush();return out}
function makeTimedEvent(e,shownStart,shownEnd,dayLimit,colIndex=0,colCount=1){
  const el=document.createElement('div');el.className=`timed-event source-${e.source}`;el.draggable=true;el.ondragstart=ev=>{ev.stopPropagation();state.drag=e};const width=100/colCount;el.style.left=`calc(${colIndex*width}% + 3px)`;el.style.right=`calc(${100-(colIndex+1)*width}% + 3px)`;
  el.style.top=((shownStart.getHours()+shownStart.getMinutes()/60)*HOUR_PX)+'px';const mins=Math.max(15,(shownEnd-shownStart)/60000);el.style.height=Math.max(20,mins/60*HOUR_PX)+'px';
  const label=document.createElement('span');label.textContent=`${e.title}${mins>=45?' · '+e.start.toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'}):''}`;el.appendChild(label);
  if(e.end<=dayLimit){const resize=document.createElement('span');resize.className='resize-handle';resize.title='Trascina per cambiare durata';resize.onpointerdown=ev=>beginResize(ev,resize,el,e);el.appendChild(resize)}
  el.onclick=ev=>{ev.stopPropagation();showEvent(e)};return el;
}
function beginResize(ev,handle,el,e){
  ev.preventDefault();ev.stopPropagation();el.draggable=false;handle.setPointerCapture(ev.pointerId);const y0=ev.clientY,h0=el.offsetHeight,end0=new Date(e.end);
  handle.onpointermove=mv=>{el.style.height=Math.max(20,h0+mv.clientY-y0)+'px'};
  handle.onpointerup=async up=>{handle.releasePointerCapture(up.pointerId);handle.onpointermove=null;handle.onpointerup=null;el.draggable=true;const delta=Math.round(((el.offsetHeight-h0)/HOUR_PX*60)/15)*15,nextEnd=new Date(end0.getTime()+delta*60000);if(nextEnd>e.start){try{await saveEvent({...e,end:nextEnd})}catch(err){alert(String(err.message||err));render()}}else render()};
}
function renderAgenda(){
  const cal=$('calendar');cal.innerHTML='';cal.className='agenda';const from=dayStart(state.cursor),to=new Date(from);to.setDate(to.getDate()+60);
  const evs=visibleEvents().filter(e=>e.end>from&&e.start<to).sort((a,b)=>a.start-b.start);
  if(!evs.length){const empty=document.createElement('div');empty.className='agenda-empty';empty.textContent='Nessun evento nei prossimi 60 giorni.';cal.appendChild(empty);$('loading').style.display='none';return}
  const groups=new Map();for(const e of evs){const key=dateOnly(e.start);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(e)}
  for(const [key,list] of groups){cal.appendChild(makeAgendaDay(key,list))}
  $('loading').style.display='none';
}
function makeAgendaDay(key,list){
  const d=localDate(key),row=document.createElement('section');row.className='agenda-day';
  const date=document.createElement('div');date.className='agenda-date';const wd=document.createTextNode(d.toLocaleDateString('it-IT',{weekday:'short'}).toUpperCase()+' ');const num=document.createElement('strong');num.textContent=d.getDate();date.append(wd,num,document.createTextNode(d.toLocaleDateString('it-IT',{month:'long'})));row.appendChild(date);
  const items=document.createElement('div');items.className='agenda-events';for(const e of list){const b=document.createElement('button');b.className='agenda-event';const t=document.createElement('span');t.className='agenda-time';t.textContent=e.allDay?'Tutto il giorno':e.start.toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'});const title=document.createElement('span');title.className='agenda-title';title.textContent=e.title;b.append(t,title);b.onclick=()=>showEvent(e);items.appendChild(b)}row.appendChild(items);return row;
}
function showEvent(e){
  state.selected=e;$('eventTitle').textContent=e.title;const opts={weekday:'long',day:'numeric',month:'long'};
  $('eventWhen').textContent=e.allDay?`${e.start.toLocaleDateString('it-IT',opts)} · tutto il giorno`:`${e.start.toLocaleDateString('it-IT',opts)} · ${e.start.toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'})}–${e.end.toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit'})}`;
  $('eventDescription').textContent=e.description||'';$('eventSource').textContent=`Origine: ${e.source}`;$('eventDialog').showModal();
}
function setEditorMode(allDay,start,end){
  $('eventAllDay').checked=allDay;
  if(allDay){$('eventStart').type='date';$('eventEnd').type='date';$('eventStart').value=dateOnly(start);$('eventEnd').value=dateOnly(new Date(end.getTime()-1));}
  else{$('eventStart').type='datetime-local';$('eventEnd').type='datetime-local';$('eventStart').value=isoLocal(start);$('eventEnd').value=isoLocal(end);}
}
function openEditor(e=null,start=null,forceAllDay=false){
  state.selected=e;const s=e?new Date(e.start):start||new Date(),allDay=e?e.allDay:forceAllDay,end=e?new Date(e.end):new Date(s.getTime()+(allDay?DAY:3600000));
  $('editorTitle').textContent=e?'Modifica evento':'Nuovo evento';$('eventId').value=e?.id||'';$('eventName').value=e?.title||'';$('eventNotes').value=e?.description||'';$('editorError').textContent='';setEditorMode(allDay,s,end);$('editorDialog').showModal();setTimeout(()=>$('eventName').focus(),0);
}
async function saveEvent(e){
  const payload={id:e.id||'',title:e.title,description:e.description||'',allDay:!!e.allDay};
  if(e.allDay){payload.start=dateOnly(e.start);payload.end=dateOnly(e.end)}else{payload.start=e.start.toISOString();payload.end=e.end.toISOString()}
  const r=await fetch('/api/events',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const out=await r.json().catch(()=>({ok:false,error:'Risposta non valida'}));if(!r.ok||!out.ok)throw new Error(out.error||'Salvataggio fallito');
  await loadEvents();render();return out;
}
function readEditorEvent(){
  const allDay=$('eventAllDay').checked;let start,end;
  if(allDay){start=localDate($('eventStart').value);const last=localDate($('eventEnd').value);end=dayEnd(last)}else{start=new Date($('eventStart').value);end=new Date($('eventEnd').value)}
  return {id:$('eventId').value,title:$('eventName').value.trim(),start,end,description:$('eventNotes').value,allDay};
}
async function submitEditor(ev){
  ev.preventDefault();const obj=readEditorEvent();if(!obj.title){$('editorError').textContent='Inserisci un titolo.';return}if(!(obj.start<obj.end)){$('editorError').textContent='La fine deve essere successiva all’inizio.';return}
  try{$('editorError').textContent='Salvataggio…';await saveEvent(obj);$('editorDialog').close()}catch(err){$('editorError').textContent=String(err.message||err)}
}
async function deleteSelected(){
  const e=state.selected;if(!e||!confirm(`Eliminare “${e.title}”?`))return;
  const r=await fetch('/api/events',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:e.id})});
  const out=await r.json().catch(()=>({ok:false,error:'Risposta non valida'}));if(!r.ok||!out.ok){alert(out.error||'Eliminazione fallita');return}
  $('eventDialog').close();state.selected=null;await loadEvents();render();
}
function move(dir){
  if(state.view==='month')state.cursor.setMonth(state.cursor.getMonth()+dir);else if(state.view==='week')state.cursor.setDate(state.cursor.getDate()+(isMobile()?3:7)*dir);else if(state.view==='agenda')state.cursor.setDate(state.cursor.getDate()+30*dir);else state.cursor.setDate(state.cursor.getDate()+dir);
  state.mini=new Date(state.cursor);render();
}
function selectView(view){state.view=view;try{localStorage.setItem('bottazzi-calendar-view',view)}catch{}if(isMobile())$('shell').classList.add('sidebar-hidden');render()}
function toggleAllDay(){
  const makeAllDay=$('eventAllDay').checked;
  if(makeAllDay){const raw=$('eventStart').value||isoLocal(new Date()),s=new Date(raw);setEditorMode(true,dayStart(s),dayEnd(s));}
  else{const raw=$('eventStart').value||dateOnly(new Date()),d=localDate(raw),s=new Date(d.getFullYear(),d.getMonth(),d.getDate(),9,0);setEditorMode(false,s,new Date(s.getTime()+3600000));}
}
$('todayBtn').onclick=()=>{state.cursor=new Date();state.mini=new Date();render()};
$('prevBtn').onclick=()=>move(-1);$('nextBtn').onclick=()=>move(1);
$('miniPrev').onclick=()=>{state.mini.setMonth(state.mini.getMonth()-1);renderMini()};$('miniNext').onclick=()=>{state.mini.setMonth(state.mini.getMonth()+1);renderMini()};
$('refreshBtn').onclick=()=>boot();$('menuBtn').onclick=()=>$('shell').classList.toggle('sidebar-hidden');
$('helpBtn').onclick=()=>$('helpDialog').showModal();$('closeHelp').onclick=()=>$('helpDialog').close();
$('closeDialog').onclick=()=>$('eventDialog').close();$('createBtn').onclick=()=>openEditor();$('mobileCreateBtn').onclick=()=>openEditor();
$('editEventBtn').onclick=()=>{if(state.selected){$('eventDialog').close();openEditor(state.selected)}};$('deleteEventBtn').onclick=deleteSelected;
$('closeEditor').onclick=()=>$('editorDialog').close();$('cancelEditor').onclick=()=>$('editorDialog').close();$('eventForm').onsubmit=submitEditor;$('eventAllDay').onchange=toggleAllDay;
$('searchBox').oninput=e=>{state.query=e.target.value;render()};document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>selectView(b.dataset.view));
let touchX=null;document.querySelector('.main').addEventListener('touchstart',e=>{if(e.touches.length===1)touchX=e.touches[0].clientX},{passive:true});document.querySelector('.main').addEventListener('touchend',e=>{if(touchX===null||!e.changedTouches.length)return;const dx=e.changedTouches[0].clientX-touchX;touchX=null;if(Math.abs(dx)>70&&!document.querySelector('dialog[open]'))move(dx<0?1:-1)},{passive:true});
document.addEventListener('keydown',ev=>{
  if(ev.ctrlKey||ev.metaKey||ev.altKey)return;const tag=document.activeElement?.tagName;if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||document.querySelector('dialog[open]'))return;
  const k=ev.key.toLowerCase();if(k==='/'){ev.preventDefault();$('searchBox').focus();return}if(k==='c'){openEditor();return}if(k==='t'){$('todayBtn').click();return}if(k==='r'){boot();return}
  if(k==='d'||k==='1'){selectView('day');return}if(k==='w'||k==='2'){selectView('week');return}if(k==='m'||k==='3'){selectView('month');return}if(k==='a'||k==='5'){selectView('agenda');return}
  if(k==='j'||k==='n')move(1);else if(k==='k'||k==='p')move(-1);
});
async function boot(){
  try{$('loading').style.display='block';$('loading').textContent='Aggiornamento calendario…';await loadEvents();render()}catch(e){$('loading').style.display='block';$('loading').textContent='Calendario temporaneamente non disponibile.'}
}
try{const saved=localStorage.getItem('bottazzi-calendar-view');if(['day','week','month','agenda'].includes(saved))state.view=saved}catch{}
if(isMobile())$('shell').classList.add('sidebar-hidden');
boot();
