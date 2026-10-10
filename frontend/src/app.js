/* Everest Textile Mills · operations frontend.
   All data lives on the mill server (backend/). This file only renders it and sends what people enter.
   API base: window.EVEREST_API if set, else /api (the Vite dev server proxies /api to localhost:8000). */
import { gsap } from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import QRCode from 'qrcode';
import jsQR from 'jsqr';
gsap.registerPlugin(ScrollTrigger);
(()=>{'use strict';
const API=window.EVEREST_API||'/api';
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const today=()=>new Date().toLocaleDateString('en-CA'),thisMonth=()=>today().slice(0,7);  // en-CA = YYYY-MM-DD, local
const money=n=>'₹'+Number(n||0).toLocaleString('en-IN',{maximumFractionDigits:0});
const num=n=>Number(n||0).toLocaleString('en-IN',{maximumFractionDigits:1});
const pct=c=>Math.round(Number(c||0)*100)+'%';
const store={get:k=>{try{return sessionStorage.getItem(k)}catch{return null}},set:(k,v)=>{try{v==null?sessionStorage.removeItem(k):sessionStorage.setItem(k,v)}catch{}}};

// ---------- API ----------
let token=store.get('everest-token');
async function api(path,{method='GET',body}={}){
 let r;
 try{r=await fetch(API+path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:body===undefined?undefined:JSON.stringify(body)})}
 catch{throw Error('Cannot reach the mill server. Is the backend running?')}
 const data=await r.json().catch(()=>({}));
 if(r.status===401&&token&&!path.startsWith('/auth/')){signOut();throw Error('Your session has ended. Sign in again.')}
 if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:`Request failed (${r.status})`);
 return data;
}
const post=(path,body)=>api(path,{method:'POST',body}),put=(path,body)=>api(path,{method:'PUT',body});
const intOrNull=v=>v===''||v==null?null:Number(v);

// ---------- Roles & navigation ----------
const ROLE_LABEL={owner:'Owner',admin:'Office / Admin',store:'Store Keeper',supervisor:'Floor Supervisor',qc:'QC Inspector'};
const allowed={
 Owner:['Dashboard','Orders','Production','Inventory','Quality','Traceability','Dispatch','Tracking','Inbox','Reviews','Assistant','Workforce','Accounts','Masters','Reports'],
 'Office / Admin':['Dashboard','Orders','Production','Inventory','Quality','Traceability','Dispatch','Tracking','Inbox','Reviews','Assistant','Workforce','Accounts','Masters'],
 'Store Keeper':['Dashboard','Inventory','Orders','Dispatch','Traceability','Tracking','Assistant'],
 'Floor Supervisor':['Dashboard','Production','Orders','Traceability','Tracking','Reviews','Workforce','Assistant'],
 'QC Inspector':['Dashboard','Quality','Traceability','Tracking','Reviews','Assistant'],
 Buyer:['Tracking']};
const navGroups=[['FACTORY',['Dashboard','Orders','Production','Inventory']],['QUALITY & DELIVERY',['Quality','Traceability','Dispatch','Tracking']],['BUYERS & AI',['Inbox','Reviews','Assistant']],['PEOPLE & FINANCE',['Workforce','Accounts']],['SETUP',['Masters','Reports']]];
const icons={Dashboard:'◫',Orders:'▤',Production:'▥',Inventory:'◈',Quality:'◉',Traceability:'⌕',Dispatch:'⇢',Tracking:'↗',Inbox:'✉',Reviews:'◐',Workforce:'♙',Accounts:'₹',Masters:'▦',Reports:'⌁',Assistant:'✦'};
const descriptions={Dashboard:'The shift, the order book and the floor at one glance.',Orders:'Create buyer orders, check yarn feasibility and follow each stage.',Production:'Assign job cards, update machines, record shift output and create QR rolls.',Inventory:'Receive yarn, issue material to production and act on shortfalls.',Quality:'Inspect rolls, capture defects and keep the rework loop moving.',Traceability:'Scan a roll and follow its order, machine, shift, operator and material history.',Dispatch:'Open a truck, scan passed rolls onto it, then close it to raise the invoice.',Tracking:'Buyer-facing order progress, share links and enquiry view.',Inbox:'Buyer messages, routed by Jev, with reply drafts from live order data.',Reviews:'AI decisions that were not confident enough. Accept or correct them.',Workforce:'Shift attendance and monthly wages.',Accounts:'Order costs, supplier bills, buyer receivables and monthly P&L.',Masters:'Buyers, suppliers, yarns, fabrics, recipes, machines, workers and shifts.',Reports:'Machine output, wages and month-end figures.',Assistant:'Tell it what happened in plain words, as you would write in the notebook. It asks for anything missing, Jev checks the request, then it is saved through the same rules as the forms.'};
const tamil={Dashboard:'கண்ணோட்டம்',Orders:'ஆர்டர்கள்',Production:'உற்பத்தி',Inventory:'சரக்கு',Quality:'தரம்',Traceability:'தடமறிதல்',Dispatch:'அனுப்புதல்',Tracking:'ஆர்டர் கண்காணிப்பு',Inbox:'செய்திகள்',Reviews:'AI சரிபார்ப்பு',Workforce:'பணியாளர்கள்',Accounts:'கணக்குகள்',Masters:'அடிப்படை தரவு',Reports:'அறிக்கைகள்',Assistant:'உதவியாளர்'};
const STAGES=['Pending','Spinning','Weaving','Dyeing','In QC','Ready','Dispatched'];
const ENUM={dept:['Office','QC','Production','Dispatch','Accounts'],intent:['status','quality_complaint','payment','new_order','delivery_change','other'],urgency:['low','med','high'],category:['mechanical','electrical','yarn_quality','operator'],severity:['low','med','high'],defect_type:['hole','colour_mismatch','uneven_weave','stain','other'],stage_at_fault:['spinning','weaving','dyeing']};
const REVIEW_FIELDS={message:['dept','intent','urgency'],breakdown:['category','severity'],defect:['defect_type','stage_at_fault','severity']};
const label=s=>String(s??'').replaceAll('_',' ');
// A roll's code from what was typed or scanned: a scanner gun reading a label types its whole link (…#roll=ETM-…).
const rollCode=v=>{v=String(v??'').trim();return v.includes('roll=')?decodeURIComponent(v.split('roll=')[1].split('&')[0]).trim():v};
const rollUrl=qr=>location.origin+location.pathname+'#roll='+encodeURIComponent(qr);
const scanButton=(target,id='')=>`<button type="button" class="secondary scan-btn" data-act="scan" data-target="${target}" ${id?`data-id="${id}"`:''} aria-label="Scan a roll QR with the camera">SCAN</button>`;

let dirty=false,orderBuyer={},invoiceBuyer={},machineOrder={},role='Owner',page='Dashboard',me=null,lang='en',currentOrder='',currentToken='',traceRoll='',pendingRoll='',editMasterId='',masterKey='buyers',reportMonth=thisMonth(),shownFlash='',defectGroup='machine_shift',digestCache=null,poLines={},flash='',afterRender=null,renderSeq=0,toastTimer=null;
try{lang=localStorage.getItem('everest-lang')||'en'}catch{}

// ---------- Master data (read by every page for names) ----------
const M={buyers:[],suppliers:[],yarn_types:[],fabric_types:[],bom:[],machines:[],workers:[],shifts:[]};
async function loadMasters(){const keys=Object.keys(M),lists=await Promise.all(keys.map(k=>api('/master/'+k)));keys.forEach((k,i)=>M[k]=lists[i])}
const byId=(list,id)=>list.find(x=>String(x.id)===String(id));
const nameOf=(list,id)=>byId(list,id)?.name||'—';
const yarnName=y=>y?`${y.count} ${y.material} · ${y.colour}`:'—';
const choices=(list,text=x=>x.name)=>list.map(x=>[x.id,text(x)]);
const optional=(items,none='— none —')=>[['',none],...items];
const orderChoices=orders=>orders.map(o=>[o.id,`#${o.id} · ${o.buyer||nameOf(M.buyers,o.buyer_id)} · ${o.colour}`]);

// ---------- UI helpers ----------
const toast=(text,kind)=>{const t=$('#toast');t.textContent=text;t.classList.toggle('error',kind==='error');t.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove('show'),3200)};
function field(lbl,name,placeholder='',type='text',value='',required=true){const extra=type==='number'?' step="any" min="0"':'';return`<label class="field"><span>${esc(lbl)}</span><input name="${esc(name)}" type="${type}" placeholder="${esc(placeholder)}" value="${esc(value)}"${extra} ${required?'required':''}></label>`}
function select(lbl,name,items,selected='',required=true){return`<label class="field"><span>${esc(lbl)}</span><select name="${esc(name)}" ${required?'required':''}>${items.map(([v,t])=>`<option value="${esc(v)}" ${String(v)===String(selected)?'selected':''}>${esc(t)}</option>`).join('')}</select></label>`}
function textarea(lbl,name,placeholder='',required=false){return`<label class="field"><span>${esc(lbl)}</span><textarea name="${esc(name)}" placeholder="${esc(placeholder)}" ${required?'required':''}></textarea></label>`}
function head(title,kicker,actions=''){return`<div class="page-head"><div><div class="eyebrow"><i></i> ${esc(kicker)}</div><h1>${esc(title)}</h1><p>${esc(descriptions[page]||'')}</p></div><div class="head-actions">${actions}</div></div>`}
function panel(title,body,tag=''){return`<section class="panel"><div class="panel-head"><h2>${title}</h2>${tag?`<span class="mono">${tag}</span>`:''}</div>${body}</section>`}
function kpi(lbl,value,note='',tone=''){return`<article class="panel kpi"><label>${lbl}</label><strong>${value}</strong><small class="${tone}">${note}</small></article>`}
function table(headers,rows,empty='Nothing here yet.'){if(!rows.length)return`<div class="empty">${empty}</div>`;return`<div class="table-wrap"><table class="table"><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`}
function badge(text,tone=''){const t=label(text);let c=t.match(/Break|Reject|Failed|Blocked|Overdue|high|short/i)?'bad':t.match(/Idle|Rework|Hold|Short|Risk|Maintenance|Pending|review|In QC|med|open/i)?'warn':t.match(/Pass|Running|Ready|Dispatched|Available|Can start|In range|routed|replied|closed|low/i)?'':'neutral';return`<span class="badge ${tone||c}">${esc(t)}</span>`}
function formStart(kind,title,fields,submit='SAVE ENTRY',extra='',hidden=''){return`<form class="panel form-section" data-form="${kind}">${hidden}<div class="panel-head"><h2>${title}</h2><span class="mono">${extra}</span></div><div class="form-grid">${fields}</div><div class="form-actions"><button class="primary">${submit}</button></div></form>`}
const isLate=o=>o.deadline<today()&&!['Ready','Dispatched'].includes(o.stage);
const monthForm=()=>`<form data-form="month" class="inline-actions"><input type="month" name="month" value="${esc(reportMonth)}" aria-label="Month"><button class="secondary">SHOW MONTH</button></form>`;
function aiLine(r,what){if(!r)return'';if(!r.confidence)return'The AI is unavailable, so this went to <b>AI review</b> for a person to decide.';const s=Object.entries(r.suggestion||{}).map(([k,v])=>`${label(k)}: <b>${esc(label(v))}</b>`).join(' · ');return r.auto?`Jev ${what}: ${s} <span class="mono">(${pct(r.confidence)})</span>`:`Jev was not sure (${pct(r.confidence)}), so this went to <b>AI review</b>. Suggestion: ${s||'none'}`}
function alertLine(list){return list?.length?` <b>DEFECT ALERT</b>: ${list.map(a=>esc(a.suggestion)).join(' ')}`:''}
function poLine(r){return r?.drafted?.length?` Purchase-order drafts updated (${r.drafted.length} line${r.drafted.length>1?'s':''}) on Inventory.`:''}
// Owner digest: built once a day on the server, cached here so the 10 s refresh doesn't flicker it.
function digestPanel(){return panel('Today at a glance',`<div id="digest">${digestCache?.day===today()?digestCache.html:'<div class="notice">Building today’s digest…</div>'}</div>`,`<button class="secondary" data-act="digest-refresh">REBUILD</button>`)}
async function loadDigest(refresh=false){
 if(!refresh&&digestCache?.day===today())return;
 const el=$('#digest');if(el&&refresh)el.innerHTML='<div class="notice">Rebuilding…</div>';
 try{
  const d=refresh?await post('/digest/refresh'):await api('/digest/today'),f=d.facts;
  const chip=(n,t,go)=>`<button class="secondary" ${go?`data-page="${go}"`:''}>${n} ${t}</button>`;
  const html=`<p class="digest-summary">${esc(d.summary)}</p><div class="head-actions">${chip(f.orders_at_risk.count,'ORDERS AT RISK','Orders')}${chip(f.low_stock.count,'LOW STOCK','Inventory')}${chip(f.po_drafts_waiting.count,'PO DRAFTS','Inventory')}${chip(f.machines_not_running.count,'NOT RUNNING','Production')}${chip(f.overdue_receivables.count,'OVERDUE INVOICES','Accounts')}${chip(f.defect_alerts.count,'DEFECT ALERTS','Quality')}${chip(f.reviews_waiting,'AI REVIEWS','Reviews')}</div><p class="help">${d.source==='ai'?'Worded by AI from the numbers above; every number is checked against them.':'Written from the numbers above (AI wording unavailable).'}</p>`;
  digestCache={day:today(),html};
  if($('#digest'))$('#digest').innerHTML=html;
 }catch(e){if($('#digest'))$('#digest').innerHTML=`<div class="notice">${esc(e.message)}</div>`}
}
function lowStockLine(list){return list?.length?`<b>LOW STOCK</b>: ${list.map(y=>`${esc(yarnName(y))} (${num(y.stock_kg)} kg, reorder at ${num(y.reorder_kg)})`).join(', ')}`:''}

function navRender(){const nav=$('#sideNav');const groups=role==='Buyer'?[['BUYER PORTAL',['Tracking']]]:navGroups;nav.innerHTML=groups.map(([g,items])=>{const vis=items.filter(x=>allowed[role].includes(x));return vis.length?`<div class="nav-label">${g}</div>`+vis.map(n=>`<button class="nav-item ${page===n?'active':''}" data-page="${n}"><span class="nav-icon">${icons[n]}</span>${lang==='ta'?tamil[n]:n}</button>`).join(''):''}).join('');$('#roleName').textContent=(me?.username?me.username+' · ':'')+role.toUpperCase();$('#userChip').textContent=(me?.username?me.username+' · ':'')+role.toUpperCase();$('#pageCrumb').textContent=page.toUpperCase()}

// ---------- Pages ----------
const stamp=v=>v?new Date(v.replace(' ','T')+'Z').toLocaleString([],{dateStyle:'medium',timeStyle:'short'}):'—';  // UTC from the server -> local
const localTime=v=>{if(!v)return'';const d=new Date(v.replace(' ','T')+'Z'),same=d.toDateString()===new Date().toDateString();return same?d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):d.toLocaleString([],{weekday:'short',hour:'2-digit',minute:'2-digit'})};
// What a machine is doing right now: stage progress bar, speed, yarn use and at-loom stock, who is on it.
function liveInfo(m){
 if(!m.order_id)return (/^finished:/.test(m.last_note||'')?`<small class="stop-note">${esc(m.last_note.toUpperCase())}</small>`:'')+(m.rate_m_per_hour?`<small>${num(m.rate_m_per_hour)} M/H · NO JOB</small>`:'');
 const pctDone=m.qty_m?Math.min(100,Math.round(100*m.stage_done_m/m.qty_m)):0;
 let h=`<div class="live"><div class="live-bar" role="progressbar" aria-valuenow="${pctDone}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pctDone}%"></i></div><small>${esc(m.kind).toUpperCase()} ${num(m.stage_done_m)} / ${num(m.qty_m)} M · ${pctDone}%</small>`;
 if(m.status==='Running'&&m.rate_m_per_hour)h+=`<small>${num(m.rate_m_per_hour)} M/H${m.yarn_kg_per_hour?' · USES '+num(m.yarn_kg_per_hour)+' KG YARN/H':''} · SINCE ${localTime(m.running_since)}${m.finishes_at?' · DONE ~'+localTime(m.finishes_at):''}</small>`;
 else if(m.status==='Running')h+='<small>SET A SPEED IN MASTERS TO RUN LIVE</small>';
 if(m.yarn_at_loom.length)h+=`<small>YARN AT LOOM ${m.yarn_at_loom.map(y=>num(y.kg)+' KG').join(' + ')} · STORE ${m.yarn_at_loom.map(y=>num(y.store_kg)+' KG').join(' + ')}</small>`;
 if(m.operator)h+=`<small>${esc(m.operator)} · ${esc(m.shift)}</small>`;
 if(m.status!=='Running'&&/^(stopped|finished):/.test(m.last_note||''))h+=`<small class="stop-note">${esc(m.last_note.toUpperCase())}</small>`;
 return h+'</div>';
}
async function dashboard(){
 const [floor,orders,stock,queue]=await Promise.all([api('/floor'),api('/orders'),api('/stock'),api('/qc/queue')]);
 const m=floor.machines,running=m.filter(x=>x.status==='Running').length,out=m.reduce((s,x)=>s+x.today_output,0),low=stock.filter(y=>y.low).length;
 const cards=m.map(x=>`<article class="machine-card"><div class="machine-top"><strong>${esc(x.name)}</strong><span class="status-dot ${x.status==='Breakdown'?'bad':x.idle?'warn':''}"></span></div><small>${esc(x.kind)} · ${x.order_id?'ORDER #'+x.order_id:'NO JOB ASSIGNED'}</small>${liveInfo(x)}<div style="margin-top:9px">${badge(x.status)}</div></article>`).join('');
 const outRows=m.filter(x=>x.today_output).map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.kind)}</td><td>${num(x.today_output)}</td></tr>`);
 afterRender=()=>loadDigest();
 return head('Shift overview','FACTORY / LIVE SHIFT',`<button class="secondary" data-page="Production">OPEN FLOOR ↗</button>`)+digestPanel()+
  `<div class="grid four">${kpi('TODAY’S OUTPUT',num(out)+' m','PRODUCTION ENTRIES')}${kpi('MACHINES RUNNING',`${running} / ${m.length}`,`${floor.idle_count} NOT RUNNING · ${m.filter(x=>x.status==='Breakdown').length} BREAKDOWN`)}${kpi('ORDERS IN PROGRESS',orders.filter(o=>!['Pending','Ready','Dispatched'].includes(o.stage)).length,'LIVE ORDER BOOK')}${kpi('QC QUEUE',queue.length,`${low} LOW STOCK ALERTS`,low?'warn':'')}</div>`+
  `<div style="margin-top:11px">${panel('Output today by machine',table(['MACHINE','STAGE','OUTPUT'],outRows,'No production recorded today yet.'),'FROM SHIFT ENTRIES')}</div>`+
  `<section class="panel" style="margin-top:11px"><div class="panel-head"><h2>Machine status board</h2><span class="mono">LIVE FROM THE FLOOR</span></div><div class="machine-grid">${cards||'<div class="empty">Add machines in Masters.</div>'}</div></section>`+
  `<div class="dashboard-grid">${panel('Orders needing attention',table(['ORDER','BUYER','DEADLINE','STAGE','PROGRESS','RISK'],orders.filter(o=>o.stage!=='Dispatched').slice(0,6).map(o=>`<tr><td class="mono-cell">#${o.id}</td><td>${esc(o.buyer)}</td><td>${esc(o.deadline)}</td><td>${badge(o.stage)}</td><td>${o.progress_pct}%</td><td>${isLate(o)?badge('Overdue'):badge('On plan')}</td></tr>`),'No open orders.'),`<div class="inline-actions"><button data-page="Orders">VIEW ALL ↗</button></div>`)}${panel('Stock watch',table(['YARN','ON HAND','REORDER','STATUS'],stock.filter(y=>y.reorder_kg&&y.stock_kg<y.reorder_kg*1.35).map(y=>`<tr><td>${esc(yarnName(y))}</td><td>${num(y.stock_kg)} kg</td><td>${num(y.reorder_kg)} kg</td><td>${badge(y.low?'Low stock':'Watch')}</td></tr>`),'All yarns above watch level.'),'REORDER POINTS')}</div>`;
}
async function roleDashboard(){
 if(role==='Owner')return dashboard();
 if(role==='Office / Admin'){const [orders,review]=await Promise.all([api('/orders'),api('/messages?status=review')]);const risk=orders.filter(isLate).length;afterRender=()=>loadDigest();return head('Office overview','ORDERS / DELIVERY / BUYERS')+digestPanel()+'<div class="grid four">'+kpi('OPEN ORDERS',orders.filter(o=>!['Ready','Dispatched'].includes(o.stage)).length,'CURRENT ORDER BOOK')+kpi('OVERDUE',risk,'NEEDS FOLLOW-UP',risk?'warn':'')+kpi('READY TO SHIP',orders.filter(o=>o.stage==='Ready').length,'PASSED QC')+kpi('MESSAGES TO CHECK',review.length,'AI WAS NOT SURE',review.length?'warn':'')+'</div>'+panel('Office focus','<p class="help">Create orders, answer buyer messages, and prepare dispatch paperwork.</p><div class="head-actions"><button class="secondary" data-page="Orders">ORDERS ↗</button><button class="secondary" data-page="Inbox">INBOX ↗</button><button class="secondary" data-page="Dispatch">DISPATCH ↗</button></div>')}
 if(role==='Store Keeper'){const [stock,grns]=await Promise.all([api('/stock'),api('/grn')]);const low=stock.filter(y=>y.low).length;return head('Store overview','YARN / RECEIPTS / ISSUES')+'<div class="grid three">'+kpi('YARN TYPES',stock.length,'ACTIVE MATERIALS')+kpi('LOW STOCK',low,'REORDER ALERTS',low?'warn':'')+kpi('RECEIVED TODAY',num(grns.filter(g=>g.received_on===today()).reduce((s,g)=>s+g.qty_kg,0))+' kg','GOODS RECEIVED')+'</div>'+panel('Store focus','<p class="help">Record goods receipts, issue yarn against an order, watch reorder levels, and load passed rolls onto trucks.</p><div class="head-actions"><button class="secondary" data-page="Inventory">INVENTORY ↗</button><button class="secondary" data-page="Dispatch">DISPATCH ↗</button></div>')}
 if(role==='Floor Supervisor'){const f=await api('/floor');return head('Floor overview','MACHINES / JOBS / SHIFT OUTPUT')+'<div class="grid three">'+kpi('RUNNING',f.machines.filter(m=>m.status==='Running').length,'OF '+f.machines.length+' MACHINES')+kpi('NOT RUNNING',f.idle_count,f.machines.filter(m=>m.status==='Breakdown').length+' BREAKDOWN')+kpi('TODAY’S OUTPUT',num(f.machines.reduce((s,m)=>s+m.today_output,0))+' m','SHIFT ENTRIES')+'</div>'+panel('Floor focus','<p class="help">Assign machine jobs, update status with one tap, record shift output and create QR-linked rolls.</p><div class="head-actions"><button class="secondary" data-page="Production">OPEN PRODUCTION ↗</button></div>')}
 if(role==='QC Inspector'){const [queue,reviews]=await Promise.all([api('/qc/queue'),api('/reviews')]);return head('Quality overview','ROLL QUEUE / INSPECTION / TRACEABILITY')+'<div class="grid three">'+kpi('WAITING FOR QC',queue.filter(r=>r.status==='pending_qc').length,'ROLLS TO INSPECT')+kpi('REWORK LOOP',queue.filter(r=>r.status==='rework').length,'RETURNED TO QC')+kpi('DEFECTS TO CONFIRM',reviews.filter(r=>r.kind==='defect').length,'AI REVIEW')+'</div>'+panel('Quality focus','<p class="help">Inspect rolls, record defects, and scan QR labels to see machine, shift and operator.</p><div class="head-actions"><button class="secondary" data-page="Quality">QUALITY CONTROL ↗</button><button class="secondary" data-page="Traceability">TRACE A ROLL ↗</button></div>')}
 return dashboard();
}

async function orderPage(){
 const orders=await api('/orders');
 const rows=orders.map(o=>`<tr><td class="mono-cell">#${o.id}<span class="sub">${esc(o.colour)} · ${num(o.qty_m)} m</span></td><td>${esc(o.buyer)}</td><td>${esc(o.fabric)}<span class="sub">${money(o.price_per_m)} / m</span></td><td>${esc(o.deadline)}</td><td>${badge(o.stage)}</td><td>${o.progress_pct}%<span class="sub">${num(o.passed_m)} m passed</span></td><td>${isLate(o)?badge('Overdue'):badge('On plan')}</td><td><div class="inline-actions"><button data-act="open-track" data-id="${o.id}">VIEW</button><button data-act="order-link" data-token="${esc(o.token)}">TRACK LINK</button><button data-act="feasibility" data-id="${o.id}">YARN</button><button data-act="risk" data-id="${o.id}">RISK</button></div></td></tr>`);
 const can=['Owner','Office / Admin'].includes(role);
 return head('Buyer orders','ORDER MANAGEMENT')+
  (can?formStart('order','Create a buyer order',select('BUYER','buyer_id',choices(M.buyers))+select('FABRIC','fabric_type_id',choices(M.fabric_types))+field('COLOUR','colour','e.g. Indigo')+field('QUANTITY (M)','qty_m','','number')+field('DEADLINE','deadline','','date',today())+field('PRICE PER METRE (₹)','price_per_m','','number'),'CREATE ORDER · CHECK YARN'):'')+
  panel('Feasibility & risk','<div id="feasibilityResult" class="notice">Choose YARN on an order to compare its bill of materials with stock, or RISK for Jev’s deadline estimate.</div>','BILL OF MATERIALS / JEV')+
  panel('Order book',table(['ORDER / QTY','BUYER','FABRIC / PRICE','DEADLINE','STAGE','PROGRESS','DEADLINE','ACTIONS'],rows,'No orders yet.'),'STAGE ROLLS UP FROM JOBS, ROLLS, QC AND DISPATCH');
}

async function inventoryPage(){
 const [stock,grns,issues,orders,pos]=await Promise.all([api('/stock'),api('/grn'),api('/issues'),api('/orders'),api('/purchase-orders')]);
 const canPO=['Owner','Office / Admin'].includes(role),livePOs=pos.filter(p=>['draft','approved'].includes(p.status));
 poLines=Object.fromEntries(livePOs.filter(p=>p.status==='approved').flatMap(p=>p.lines.filter(l=>l.open_kg>0).map(l=>[l.id,{...l,supplier_id:p.supplier_id,po:p.id,supplier:p.supplier}])));
 const poRows=livePOs.map(p=>`<tr><td class="mono-cell">PO-${p.id}<span class="sub">${p.auto?'DRAFTED BY SYSTEM':'RAISED BY HAND'}</span></td><td>${esc(p.supplier)}</td><td>${p.lines.map(l=>`<div>${esc(l.count)} ${esc(l.material)} · ${esc(l.colour)}: ${p.status==='draft'&&canPO?`<input class="po-qty" type="number" min="1" step="any" value="${l.qty_kg}" data-po-line="${l.id}" aria-label="Kg"> kg`:`${num(l.qty_kg)} kg`} @ ${money(l.rate)}/kg${p.status==='approved'?` · ${num(l.received_kg)} received`:''}${l.reason?`<span class="sub">${esc(l.reason)}${l.locked&&p.status==='draft'?' · EDITED, NOT RECOMPUTED':''}</span>`:''}</div>`).join('')}</td><td>${money(p.value)}</td><td>${p.status==='draft'?badge('Draft · to approve','warn'):badge(p.status)}</td><td><div class="inline-actions">${canPO&&p.status==='draft'?`<button data-act="po-approve" data-id="${p.id}">APPROVE</button>`:''}${canPO?`<button data-act="po-cancel" data-id="${p.id}">CANCEL</button>`:''}</div></td></tr>`);
 const poPanel=panel('Purchase orders',table(['PO','SUPPLIER','LINES','VALUE','STATUS','ACTIONS'],poRows,'Nothing to buy: stock and yarn on order cover open orders and reorder levels.')+'<p class="help">Drafts are recomputed after every order, receipt and issue: open-order need + reorder level − stock − yarn already on order, from the supplier with the lowest recent rate. Editing a quantity stops the system recomputing that line. Approve = placed with the supplier; then book the delivery against it in the goods receipt form.</p>',`<button class="secondary" data-act="po-refresh">RECOMPUTE</button>`);
 const yarnChoices=choices(M.yarn_types,yarnName),openOrders=orderChoices(orders.filter(o=>o.stage!=='Dispatched'));
 const stockRows=stock.map(y=>`<tr><td>${esc(y.count)} ${esc(y.material)}</td><td>${esc(y.colour)}</td><td>${num(y.stock_kg)} kg</td><td>${num(y.reorder_kg)} kg</td><td>${badge(y.low?'Low stock':y.reorder_kg&&y.stock_kg<y.reorder_kg*1.3?'Watch':'In range')}</td><td>${y.avg_rate==null?'—':money(y.avg_rate)+'/kg'}</td></tr>`);
 const grnRows=grns.map(g=>`<tr><td class="mono-cell">GRN-${g.id}</td><td>${esc(g.supplier)}</td><td>${esc(yarnName(byId(M.yarn_types,g.yarn_type_id)))}<span class="sub">CHALLAN ${esc(g.challan_no||'—')}</span></td><td>${num(g.qty_kg)} kg</td><td>${money(g.rate)}/kg</td><td>${esc(g.received_on)}</td></tr>`);
 const issueRows=issues.map(i=>`<tr><td>${esc(i.issued_on)}</td><td class="mono-cell">#${i.order_id}</td><td>${esc(yarnName(byId(M.yarn_types,i.yarn_type_id)))}</td><td>${esc(nameOf(M.machines,i.machine_id))}</td><td>${num(i.qty_kg)} kg</td><td>${i.created_by?'Store':'Loom, live'}</td></tr>`);
 const poChoices=Object.values(poLines).map(l=>[l.id,`PO-${l.po} · ${l.supplier} · ${l.count} ${l.material} · ${num(l.open_kg)} kg open`]);
 const grnFields=(poChoices.length?select('AGAINST PO (OPTIONAL)','po_line_id',optional(poChoices,'Not against a PO'),'',false):'')+select('SUPPLIER','supplier_id',choices(M.suppliers))+select('YARN TYPE','yarn_type_id',yarnChoices)+field('QUANTITY (KG)','qty_kg','','number')+field('RATE / KG (₹)','rate','','number')+field('CHALLAN NUMBER','challan_no','Supplier challan','text','',false)+select('LINK ORDER (OPTIONAL)','order_id',optional(openOrders,'No linked order'),'',false)+`<label class="field"><span>CHALLAN PHOTO → FILL THE FORM</span><input type="file" name="challan_photo" accept="image/*" data-challan></label>`;
 const issueFields=select('YARN TYPE','yarn_type_id',yarnChoices)+select('ORDER','order_id',openOrders)+select('MACHINE (OPTIONAL)','machine_id',optional(choices(M.machines)),'',false)+field('QUANTITY (KG)','qty_kg','','number');
 return head('Yarn at the gate','RAW MATERIAL INVENTORY')+'<div class="grid three">'+kpi('YARN TYPES',stock.length,'MASTER RECORDS')+kpi('GRNS',grns.length,'RECEIPTS LOGGED')+kpi('LOW STOCK',stock.filter(y=>y.low).length,'REORDER ALERTS')+'</div><div class="split" style="margin-top:11px">'+formStart('grn','Goods receipt note',grnFields,'RECORD GRN')+formStart('issue','Issue yarn to production',issueFields,'ISSUE MATERIAL')+'</div>'+panel('Live yarn stock',table(['YARN','COLOUR','AVAILABLE','REORDER LEVEL','ALERT','AVG RATE'],stockRows,'Add yarn types in Masters.'),'GRN − ISSUES = ON HAND')+poPanel+'<div class="split">'+panel('Material issues',table(['DATE','ORDER','YARN','MACHINE','QTY','SOURCE'],issueRows,'No yarn issued yet.'),'STOCK DEDUCTIONS')+panel('Goods receipt history',table(['GRN','SUPPLIER','YARN / CHALLAN','QUANTITY','RATE','DATE'],grnRows,'No receipts yet.'),'RECEIVING LOG')+'</div>';
}

async function productionPage(){
 const [floor,orders,entries]=await Promise.all([api('/floor'),api('/orders'),api('/production')]);
 machineOrder=Object.fromEntries(floor.machines.filter(m=>m.order_id).map(m=>[m.id,m.order_id]));
 const openOrders=orderChoices(orders.filter(o=>!['Ready','Dispatched'].includes(o.stage)));
 const cards=floor.machines.map(m=>`<article class="machine-card"><div class="machine-top"><strong>${esc(m.name)}</strong><span class="status-dot ${m.status==='Breakdown'?'bad':m.idle?'warn':''}"></span></div><small>${esc(m.kind)} · ${m.order_id?'ORDER #'+m.order_id:'NO ORDER'} · TODAY ${num(m.today_output)}</small>${liveInfo(m)}<label class="field" style="margin-top:10px"><span>ONE-TAP STATUS</span><select data-machine-status="${m.id}">${['Running','Idle','Breakdown','Maintenance'].map(s=>`<option ${s===m.status?'selected':''}>${s}</option>`).join('')}</select></label><label class="field" style="margin-top:6px"><span>ASSIGN ORDER</span><select data-machine-order="${m.id}"><option value="">${m.order_id?'#'+m.order_id+' (current)':'Choose an order…'}</option>${openOrders.filter(([id])=>id!==m.order_id).map(([v,t])=>`<option value="${v}">${esc(t)}</option>`).join('')}</select></label></article>`).join('');
 const people=select('MACHINE','machine_id',choices(M.machines,m=>`${m.name} · ${m.kind}`))+select('ORDER','order_id',openOrders)+select('OPERATOR','operator_id',choices(M.workers))+select('SHIFT','shift_id',choices(M.shifts));
 const outputFields=people+field('OUTPUT (M, OR KG ON SPINNING)','output','','number')+field('MACHINE HOURS','hours','','number','8',false)+`<label class="field"><span>ALSO CREATE A ROLL WITH THIS OUTPUT</span><input type="checkbox" name="make_roll"></label>`;
 const shiftRows=entries.map(p=>`<tr><td>${esc(p.entry_date)}</td><td>${esc(p.machine)}</td><td class="mono-cell">#${p.order_id}</td><td>${esc(p.operator)}</td><td>${esc(p.shift)}</td><td>${num(p.output)}</td><td>${num(p.hours)}</td></tr>`);
 return head('Production floor','MACHINES / JOB CARDS',floor.sim_speed!==1?`<span class="mono speed-chip">DEMO SPEED ×${num(floor.sim_speed)}</span>`:'')+'<div class="grid three">'+kpi('RUNNING',floor.machines.filter(m=>m.status==='Running').length,'MACHINES ON JOB')+kpi('NOT RUNNING',floor.idle_count,'IDLE / DOWN / MAINTENANCE')+kpi('TODAY’S OUTPUT',num(floor.machines.reduce((s,m)=>s+m.today_output,0)),'SHIFT ENTRIES')+'</div>'+panel('Machine status board',`<div class="machine-grid">${cards||'<div class="empty">Add machines in Masters.</div>'}</div>`,'A BREAKDOWN NOTE GOES TO JEV FOR TRIAGE')+'<div class="split">'+formStart('job','Assign order to machine (job card)',select('ORDER','order_id',openOrders)+select('MACHINE','machine_id',choices(M.machines,m=>`${m.name} · ${m.kind}${m.rate_m_per_hour?' · '+num(m.rate_m_per_hour)+' m/h':''}`))+select('OPERATOR','operator_id',optional(choices(M.workers),'Last operator on this machine'),'',false)+select('SHIFT','shift_id',optional(choices(M.shifts),'Last shift on this machine'),'',false),'ASSIGN JOB CARD','RUNNING MACHINES PRODUCE AT THEIR SPEED')+formStart('breakdown','Breakdown log',select('MACHINE','machine_id',choices(M.machines))+textarea('WHAT HAPPENED?','note','e.g. yarn keeps snapping on the warp',true),'LOG & TRIAGE','JEV · CATEGORY / SEVERITY')+'</div>'+formStart('production','Production entry · per shift',outputFields,'RECORD SHIFT OUTPUT')+panel('Shift entries',table(['DATE','MACHINE','ORDER','OPERATOR','SHIFT','OUTPUT','HOURS'],shiftRows,'Production output will appear here.'),'LATEST 200');
}

async function qualityPage(){
 const [queue,fg,alerts,rep]=await Promise.all([api('/qc/queue'),api('/finished-goods'),api('/alerts/defects'),api('/reports/defects?group='+defectGroup)]);
 const alertRows=alerts.map(a=>`<tr><td><b>${esc(a.label)}</b><span class="sub">${a.scope==='operator'?'OPERATOR':'MACHINE / SHIFT'}</span></td><td>${a.defects} of ${a.checks} · ${pct(a.rate)}<span class="sub">MILL ELSEWHERE ${pct(a.baseline)}</span></td><td>${esc(a.suggestion)}</td><td>${a.status==='open'?`<div class="inline-actions"><button data-act="alert-ack" data-id="${a.id}" style="white-space:nowrap">WE’RE ON IT</button></div>`:badge('Acknowledged')}</td></tr>`);
 const groups=[['machine_shift','Machine + shift'],['machine','Machine'],['shift','Shift'],['operator','Operator'],['type','Defect type']];
 const repRows=rep.rows.map(r=>defectGroup==='type'?`<tr><td>${esc(label(r.label))}</td><td>${r.defects}</td><td>${pct(r.share)}</td></tr>`:`<tr><td>${esc(r.label)}</td><td>${r.checks}</td><td>${r.defects}</td><td>${pct(r.rate)}</td></tr>`);
 const repPanel=panel('Where defects come from',`<div class="inline-actions" style="margin-bottom:10px">${groups.map(([g,t])=>`<button data-act="defect-group" data-id="${g}" ${g===defectGroup?'class="active" aria-pressed="true"':''}>${t.toUpperCase()}</button>`).join('')}</div>`+table(defectGroup==='type'?['DEFECT','COUNT','SHARE OF DEFECTS']:['GROUP','QC CHECKS','FAILED','FAILURE RATE'],repRows,'No QC checks in the last '+rep.days+' days.'),`LAST ${rep.days} DAYS · MILL ${rep.mill.rate==null?'—':pct(rep.mill.rate)} FAILING`);
 const rows=queue.map(r=>`<tr><td class="mono-cell">${esc(r.qr)}</td><td>#${r.order_id}</td><td>${esc(r.machine)}</td><td>${num(r.metres)} m</td><td>${badge(r.status)}</td><td><div class="inline-actions"><button data-act="qc-pass" data-qr="${esc(r.qr)}">PASS</button><button data-act="scan-roll" data-qr="${esc(r.qr)}">INSPECT</button><button data-act="trace" data-qr="${esc(r.qr)}">TRACE</button></div></td></tr>`);
 const fields=`<label class="field"><span>ROLL QR</span><div class="scan-row"><input name="qr" placeholder="Scan or type e.g. ETM-…" required>${scanButton('qc')}</div></label>`+select('RESULT','result',[['pass','Pass'],['rework','Rework'],['reject','Reject']])+select('DEFECT TYPE','defect_type',optional(ENUM.defect_type.map(d=>[d,label(d)]),'Let Jev decide from the notes'),'',false)+textarea('NOTES','notes','What did you see? Needed for rework / reject.');
 return head('Quality control','ROLL INSPECTION / TRACEABILITY')+'<div class="grid three">'+kpi('WAITING FOR QC',queue.filter(r=>r.status==='pending_qc').length,'NEW ROLLS')+kpi('REWORK LOOP',queue.filter(r=>r.status==='rework').length,'BACK FOR RE-INSPECTION')+kpi('PASSED IN STORE',fg.reduce((s,x)=>s+x.rolls,0),num(fg.reduce((s,x)=>s+x.metres,0))+' M READY TO SHIP')+'</div><div class="split" style="margin-top:11px">'+formStart('qc','Scan roll · record inspection',fields,'SAVE QC RESULT','NOTES → JEV CLASSIFIES THE DEFECT')+panel('QC queue',table(['ROLL','ORDER','MACHINE','QTY','STATUS','ACTION'],rows,'No rolls waiting for inspection.')+'<div class="notice">Rework returns to this queue. Rejected rolls are blocked from dispatch.</div>','SCAN → RESULT → TRACE')+'</div>'+(alerts.length?panel('Defect hotspots',table(['WHERE','FAILED','WHAT TO DO','STATUS'],alertRows),'RAISED AUTOMATICALLY · CLEARS WHEN THE RATE IS NORMAL'):'')+repPanel;
}

async function tracePage(){
 let content=`<form class="panel form-grid two" data-form="trace"><label class="field"><span>ROLL QR / SCAN</span><div class="scan-row"><input name="qr" value="${esc(traceRoll)}" placeholder="ETM-…" autofocus>${scanButton('trace')}</div></label><div class="form-actions" style="align-items:end"><button class="primary">TRACE ROLL</button></div></form>`;
 if(!traceRoll)return head('Roll traceability','QR SCAN / MATERIAL HISTORY')+content+panel('Trace any roll','<p class="help">Scan its QR code or type the roll code to see its order, yarn issued, machine, shift, operator, QC history and dispatch.</p>');
 let r;try{r=await api('/rolls/'+encodeURIComponent(traceRoll.trim()))}catch(e){return head('Roll traceability','QR SCAN / MATERIAL HISTORY')+content+`<div class="alert">${esc(e.message==='not found'?'No roll found with that code.':e.message)}</div>`}
 const yarn=r.yarn_issued.map(i=>`${esc(i.count)} ${esc(i.material)} · ${esc(i.colour)} · ${num(i.qty_kg)} kg · ${esc(i.issued_on)}`).join('<br>')||'No yarn issued to this order yet.';
 const qrows=r.qc_history.map(q=>`<tr><td>${esc(stamp(q.at))}</td><td>${badge(q.result)}</td><td>${esc(label(q.defect_type||'—'))}</td><td>${esc(q.severity||'—')}</td><td>${esc(q.stage_at_fault||'—')}</td><td>${esc(q.inspector||'—')}</td><td>${esc(q.notes||'')}</td></tr>`);
 content+=`<section class="panel" style="margin-top:12px"><div class="panel-head"><h2>${esc(r.qr)}</h2><div class="inline-actions">${badge(r.status)}<button data-act="roll-qr" data-qr="${esc(r.qr)}" data-order="${r.order_id}" data-metres="${r.metres}" data-status="${esc(r.status)}">QR LABEL</button></div></div><div class="grid four">${kpi('ORDER','#'+r.order_id,esc(r.buyer))}${kpi('FABRIC',esc(r.order.fabric),esc(r.order.colour))}${kpi('ROLL QTY',num(r.metres)+' m','MADE '+esc(r.made_on))}${kpi('MACHINE / SHIFT',esc(r.machine),esc(r.shift)+' · '+esc(r.operator))}</div><div class="split" style="margin-top:12px">${panel('Yarn issued to the order',`<p class="help">${yarn}</p><p class="help">Dispatch: ${r.dispatch?`Truck ${esc(r.dispatch.vehicle_no)} · ${esc(r.dispatch.driver)} · ${esc(r.dispatch.status)}`:'Not dispatched.'}</p>`)}${panel('QC history',table(['TIME','RESULT','DEFECT','SEVERITY','STAGE AT FAULT','INSPECTOR','NOTES'],qrows,'Not inspected yet.'))}</div></section>`;
 return head('Roll traceability','QR SCAN / MATERIAL HISTORY')+content;
}

async function dispatchPage(){
 const [orders,fg,ds]=await Promise.all([api('/orders'),api('/finished-goods'),api('/dispatches')]);
 const open=await Promise.all(ds.filter(d=>d.status==='open').map(async d=>{const [detail,passed]=await Promise.all([api('/dispatches/'+d.id),api(`/rolls?order_id=${d.order_id}&status=passed`)]);const loaded=new Set(detail.packing_list.map(p=>p.qr));return{...detail,waiting:passed.filter(r=>!loaded.has(r.qr))}}));
 const ready=orderChoices(orders.filter(o=>o.stage!=='Dispatched'&&fg.some(f=>f.order_id===o.id)));
 const trucks=open.map(d=>`<section class="panel" style="margin-top:11px"><div class="panel-head"><h2>Truck ${esc(d.vehicle_no)} · ${esc(d.driver)}</h2><span class="mono">DISPATCH ${d.id} · ORDER #${d.order_id} · ${esc(d.buyer)}</span></div><form data-form="scan" class="form-grid two"><input type="hidden" name="did" value="${d.id}"><label class="field"><span>SCAN ROLL QR ONTO THIS TRUCK</span><div class="scan-row"><input name="qr" placeholder="ETM-…" required autofocus>${scanButton('dispatch',d.id)}</div></label><div class="form-actions" style="align-items:end"><button class="primary">ADD ROLL</button><button type="button" class="secondary" data-act="close-dispatch" data-id="${d.id}">CLOSE TRUCK & INVOICE</button></div></form>${d.waiting.length?`<p class="help">Or tap a passed roll of order #${d.order_id} to load it:</p><div class="pick-list">${d.waiting.map(r=>`<button type="button" data-act="load-roll" data-id="${d.id}" data-qr="${esc(r.qr)}">${esc(r.qr)} · ${num(r.metres)} m</button>`).join('')}</div>`:`<p class="help">No more passed rolls of order #${d.order_id} waiting.</p>`}${table(['ROLL','METRES'],d.packing_list.map(p=>`<tr><td class="mono-cell">${esc(p.qr)}</td><td>${num(p.metres)} m</td></tr>`),'No rolls scanned yet.')}<p class="help">${d.rolls} rolls · ${num(d.metres)} m on this truck.</p></section>`).join('');
 const fgRows=fg.map(f=>`<tr><td class="mono-cell">#${f.order_id}</td><td>${esc(f.buyer)}</td><td>${esc(f.fabric)} · ${esc(f.colour)}</td><td>${f.rolls}</td><td>${num(f.metres)} m</td></tr>`);
 const hist=ds.map(d=>`<tr><td class="mono-cell">${d.id}</td><td>${esc(stamp(d.created_at))}</td><td>#${d.order_id} · ${esc(d.buyer)}</td><td>${esc(d.vehicle_no)} · ${esc(d.driver)}</td><td>${badge(d.status)}</td><td>${d.total==null?'—':money(d.total)}</td><td><div class="inline-actions"><button data-act="print-dispatch" data-id="${d.id}">PACKING LIST</button>${d.invoice_id?`<button data-act="print-invoice" data-id="${d.id}">INVOICE</button>`:''}</div></td></tr>`);
 return head('Finished goods & dispatch','PASSED ROLLS / SHIPMENTS')+'<div class="grid three">'+kpi('PASSED ROLLS IN STORE',fg.reduce((s,f)=>s+f.rolls,0),'READY TO SHIP')+kpi('TRUCKS LOADING',open.length,'OPEN DISPATCHES')+kpi('READY METRES',num(fg.reduce((s,f)=>s+f.metres,0))+' m','QC PASSED')+'</div>'+
  formStart('dispatch','Open a truck',select('ORDER','order_id',ready.length?ready:[['','No order has passed rolls yet']])+field('VEHICLE NUMBER','vehicle_no','TN 00 AA 0000')+field('DRIVER','driver','Name / phone'),'OPEN DISPATCH','THEN SCAN ROLLS · DUPLICATES & UNPASSED ROLLS ARE BLOCKED')+trucks+
  panel('Finished goods',table(['ORDER','BUYER','FABRIC','ROLLS','METRES'],fgRows,'No passed rolls in store.'),'QC-PASSED, NOT YET DISPATCHED')+panel('Dispatch history',table(['DISPATCH','OPENED','ORDER','VEHICLE / DRIVER','STATUS','INVOICE TOTAL','DOCUMENTS'],hist,'No dispatches yet.'));
}

function renderTracking(t,link=''){
 const idx=Math.max(0,STAGES.indexOf(t.stage));
 const disp=t.dispatches.map(d=>`${esc(stamp(d.created_at))} · ${esc(d.vehicle_no)} · ${d.rolls} rolls · ${num(d.metres)} m (${esc(d.status)})`).join(' / ')||'Not dispatched yet.';
 return`<section class="panel tracking-card"><div class="panel-head"><div><span class="mono">ORDER #${t.order_no}</span><h2 style="margin:8px 0">${esc(t.fabric)} · ${esc(t.colour)}</h2></div>${badge(t.stage)}</div><div class="tracking-timeline" style="grid-template-columns:repeat(${STAGES.length},1fr)">${STAGES.map((x,i)=>`<div class="timeline-step ${i<=idx?'done':''}">${x.toUpperCase()}</div>`).join('')}</div><div class="grid three">${kpi('PRODUCED',num(t.produced_m)+' m',`TARGET ${num(t.target_m)} m · ${t.progress_pct}%`)}${kpi('PASSED QC',num(t.passed_m)+' m',`${t.rolls_passed} OF ${t.rolls_produced} ROLLS`)}${kpi('EXPECTED READY',t.expected_ready?esc(t.expected_ready):'—',(t.expected_ready?'':'NEEDS A FEW DAYS OF OUTPUT · ')+'DEADLINE '+esc(t.deadline))}</div><p class="help">Dispatch: ${disp}</p>${link?`<div class="form-actions"><a class="secondary" href="${esc(trackUrl(link))}" target="_blank" rel="noopener">OPEN BUYER PAGE ↗</a><button class="secondary" data-act="order-link" data-token="${esc(link)}">COPY SHARE LINK</button></div>`:''}</section>`;
}
async function trackingPage(){
 const orders=await api('/orders');
 let card='';if(currentOrder){const o=await api('/orders/'+currentOrder);card=renderTracking(o,o.token)}
 return head('Order tracking','SHAREABLE STATUS / BUYER ENQUIRY')+panel('Look up an order',`<form data-form="tracking-select" class="form-grid two">${select('ORDER','order',orderChoices(orders),currentOrder)}<div class="form-actions" style="align-items:end"><button class="primary">OPEN BUYER VIEW</button></div></form>`)+card+
  panel('Enquiries for the office',table(['ORDER','BUYER','STAGE','TARGET / PASSED','READY BY','DEADLINE','ACTION'],orders.map(x=>`<tr><td class="mono-cell">#${x.id}</td><td>${esc(x.buyer)}</td><td>${badge(x.stage)}</td><td>${num(x.passed_m)} / ${num(x.qty_m)} m</td><td>${esc(x.expected_ready||'—')}</td><td>${esc(x.deadline)}</td><td><div class="inline-actions"><button data-act="open-track" data-id="${x.id}">VIEW</button><button data-act="order-link" data-token="${esc(x.token)}">COPY LINK</button></div></td></tr>`),'No orders yet.'),'BUYER CALLS / QUICK ANSWERS');
}

// The chat assistant: same requests as the forms, in plain words. Jev picks the skill and checks the request before it runs.
async function assistantPage(){
 const office=['Owner','Office / Admin'].includes(role);
 const [threads,log]=await Promise.all([api('/assistant/chat'),office?api('/assistant/conversations'):null]);
 const open=s=>['collecting','confirming'].includes(s);
 const bubbles=threads.map(c=>c.messages.map(m=>`<div class="chat-msg ${m.from==='user'?'user':'bot'}">${esc(m.text)}</div>`).join('')+(open(c.status)?'':`<div class="chat-end">${esc(label(c.skill||'chat'))} · ${esc(c.status)}</div>`)).join('');
 const rows=(log||[]).map(c=>`<tr><td>${esc(stamp(c.updated_at))}<span class="sub">${esc(c.channel)}${c.peer?' · +'+esc(c.peer):''}</span></td><td>${esc(c.username)}</td><td>${esc(label(c.skill||'—'))}<span class="sub">${esc(c.difficulty||'')}${c.model?' · '+esc(c.model.split('/').pop()):''}</span></td><td style="white-space:pre-line">${esc(c.draft?.summary||c.messages[0]?.text||'')}</td><td>${c.jev==null?'—':pct(c.jev)}</td><td>${badge(c.status)}</td></tr>`);
 afterRender=()=>{const t=$('#chatThread');if(t)t.scrollTop=t.scrollHeight;$('#chatInput')?.focus()};
 return head('Assistant','PLAIN WORDS → CHECKED REQUEST')+
  panel('Chat',`<div class="chat" id="chatThread">${bubbles||'<div class="empty">Try: “Loom 04 did 120 m on night shift for order 1, Arun, 8 hours” or “how much polyester is left?”</div>'}</div><form data-form="assistant" class="chat-form"><input id="chatInput" name="text" autocomplete="off" maxlength="2000" placeholder="Tell me what happened, or ask…" required aria-label="Message"><button class="primary">SEND</button></form><p class="help">Say <b>cancel</b> to drop a request, <b>yes</b> to confirm one. Every request runs with your own permissions.</p>`,'JEV PICKS THE SKILL · JEV CHECKS THE REQUEST')+
  (log?panel('What the assistant did',table(['WHEN','WHO','SKILL · LEVEL · MODEL','REQUEST','JEV MATCH','RESULT'],rows,'Nothing yet.'),'WEB + WHATSAPP'):'');
}

async function inboxPage(){
 const [msgs,orders]=await Promise.all([api('/messages'),api('/orders')]);
 orderBuyer=Object.fromEntries(orders.map(o=>[o.id,o.buyer_id]));
 const fields=select('BUYER (OPTIONAL)','buyer_id',optional(choices(M.buyers)),'',false)+select('ORDER (OPTIONAL)','order_id',optional(orderChoices(orders)),'',false)+select('CHANNEL','channel',[['whatsapp','WhatsApp'],['email','Email'],['phone','Phone call']])+textarea('MESSAGE','text','Paste the buyer’s message',true);
 const rows=msgs.map(m=>`<tr><td>${esc(stamp(m.created_at))}<span class="sub">${esc(m.channel)}</span></td><td>${esc(nameOf(M.buyers,m.buyer_id))}${m.order_id?`<span class="sub">ORDER #${m.order_id}</span>`:''}</td><td>${esc(m.text)}${m.reply?`<span class="sub">${m.status==='replied'?'REPLY':'DRAFT, NOT SENT'}: ${esc(m.reply)}</span>`:''}</td><td>${m.dept?`<b>${esc(m.dept)}</b><span class="sub">${esc(label(m.intent))} · ${esc(m.urgency)}</span>`:'—'}</td><td>${m.confidence==null?'—':pct(m.confidence)}</td><td>${badge(m.status)}</td><td><div class="inline-actions"><select data-lang-for="${m.id}" aria-label="Reply language"><option value="en">EN</option><option value="ta">TA</option><option value="hi">HI</option></select><button data-act="draft" data-id="${m.id}">DRAFT REPLY</button></div></td></tr>`);
 return head('Buyer inbox','JEV ROUTING / REPLY DRAFTS')+'<div class="grid three">'+kpi('MESSAGES',msgs.length,'ALL CHANNELS')+kpi('NEED A HUMAN',msgs.filter(m=>m.status==='review').length,'JEV WAS NOT SURE',msgs.some(m=>m.status==='review')?'warn':'')+kpi('REPLIED',msgs.filter(m=>m.status==='replied').length,'ANSWERED')+'</div>'+formStart('message','Log an incoming message',fields,'ROUTE WITH JEV','DEPARTMENT · INTENT · URGENCY')+panel('Messages',table(['RECEIVED','FROM','MESSAGE','ROUTED TO','CONFIDENCE','STATUS','REPLY'],rows,'No buyer messages yet.'),'REPLIES ARE RECORDED HERE; SEND THEM FROM WHATSAPP / EMAIL');
}

async function reviewsPage(){
 const [open,settled]=await Promise.all([api('/reviews'),api('/reviews?status=resolved')]);
 const row=r=>{const cur=r.final||r.suggestion;return`<tr data-review="${r.id}"><td>${badge(r.kind)}<span class="sub">#${r.ref_id} · ${esc(stamp(r.created_at))}</span>${r.status==='resolved'?`<span class="sub">${r.final?'CHECKED BY A PERSON':'APPLIED AUTOMATICALLY'}</span>`:''}</td><td>${r.confidence?pct(r.confidence):'AI unavailable'}</td><td><div class="inline-actions">${REVIEW_FIELDS[r.kind].map(f=>`<label class="field"><span>${esc(label(f).toUpperCase())}</span><select data-field="${f}">${f in cur?'':'<option value="">— keep as is —</option>'}${ENUM[f].map(v=>`<option value="${v}" ${cur[f]===v?'selected':''}>${esc(label(v))}</option>`).join('')}</select></label>`).join('')}</div></td><td><div class="inline-actions"><button data-act="review-save" data-id="${r.id}">${r.status==='open'?'SAVE':'CORRECT'}</button></div></td></tr>`};
 return head('AI review queue','HUMAN IN THE LOOP')+panel('Waiting for a person',table(['ITEM','JEV CONFIDENCE','DECISION (JEV’S SUGGESTION PRESELECTED WHEN IT HAD ONE)','ACTION'],open.map(row),'Nothing to review. Jev was confident about everything.'),'SAVE APPLIES THE VALUES SHOWN')+panel('Recent decisions',table(['ITEM','JEV CONFIDENCE','APPLIED VALUES','ACTION'],settled.slice(0,25).map(row),'No settled decisions yet.'),'WRONG? CHANGE IT AND PRESS CORRECT');
}

// Attendance register: workers down the side, the month's days across; each mark is a shift present (D, N…).
function attendanceRegister(reg){
 if(reg.error)return panel('Attendance register',`<div class="notice">The register could not load (${esc(reg.error)}). If the server was started before the register existed, restart the backend.</div>`);
 const [y,mo]=reg.month.split('-').map(Number),n=new Date(y,mo,0).getDate(),days=[...Array(n)].map((_,i)=>i+1);
 const key=d=>`${reg.month}-${String(d).padStart(2,'0')}`,now=today(),dow=d=>new Date(y,mo-1,d).getDay();
 const tag=name=>esc(String(name).trim().slice(0,1).toUpperCase());  // ponytail: first letter; two shifts sharing a letter look alike (the cell's tooltip names them)
 const by={};for(const r of reg.records)((by[r.worker_id]??={})[r.work_date]??=[]).push(r.shift);
 const cls=d=>[dow(d)===0?'sun':'',key(d)===now?'today':''].join(' ');
 const head=`<tr><th class="who">WORKER</th>${days.map(d=>`<th class="${cls(d)}">${d}<small>${'SMTWTFS'[dow(d)]}</small></th>`).join('')}<th class="tot">DAYS</th></tr>`;
 const body=M.workers.map(w=>{const mine=by[w.id]||{};return`<tr><th class="who">${esc(w.name)}</th>${days.map(d=>{const s=mine[key(d)];return`<td class="${cls(d)} ${s?'on':''}" ${s?`title="${esc(key(d)+': '+s.join(', '))}"`:''}>${s?s.map(tag).join('·'):''}</td>`}).join('')}<td class="tot">${Object.keys(mine).length}</td></tr>`}).join('');
 const legend=M.shifts.map(s=>`<b>${tag(s.name)}</b> ${esc(s.name)}`).join(' · ');
 return panel('Attendance register · '+esc(reg.month),M.workers.length?`<div class="register-wrap"><table class="register"><thead>${head}</thead><tbody>${body}</tbody></table></div><p class="help">${legend} · a mark means present on that shift · DAYS = days present, as wages count them · pick another month at the top</p>`:'<div class="empty">Add workers in Masters.</div>','WHO WAS PRESENT, DAY BY DAY');
}
async function workforcePage(){
 const canWages=['Owner','Office / Admin'].includes(role);
 const [wages,register]=await Promise.all([canWages?api('/wages?month='+reportMonth):null,api('/attendance?month='+reportMonth).catch(e=>({error:e.message}))]);  // the register failing must not take the page down
 const workers=M.workers.map(w=>`<label class="roll-card"><input type="checkbox" name="worker_ids" value="${w.id}"><strong>${esc(w.name)}</strong><small>${esc(w.wage_type)} · ${money(w.rate)}${w.wage_type==='daily'?'/day':'/unit'}</small></label>`).join('')||'<div class="empty">Add workers in Masters.</div>';
 const attendance=`<form class="panel form-section" data-form="attendance"><div class="panel-head"><h2>Record shift attendance</h2><span class="mono">TICK EVERYONE PRESENT</span></div><div class="form-grid">${select('SHIFT','shift_id',choices(M.shifts))+field('DATE','work_date','','date',today())}</div><div class="roll-cards" style="margin-top:12px">${workers}</div><div class="form-actions"><button class="primary">SAVE ATTENDANCE</button></div></form>`;
 const wageRows=wages?wages.workers.map(w=>`<tr><td>${esc(w.name)}</td><td>${esc(w.wage_type)} · ${money(w.rate)}</td><td>${w.days}</td><td>${num(w.output)}</td><td>${money(w.wage)}</td></tr>`):[];
 return head('People, shifts & wages','ATTENDANCE / PAYROLL',monthForm())+'<div class="grid three">'+kpi('WORKERS',M.workers.length,'ON THE ROSTER')+(wages?kpi('WAGES · '+esc(wages.month),money(wages.total),'ATTENDANCE + PIECE OUTPUT'):'')+'</div>'+attendance+attendanceRegister(register)+(wages?panel('Monthly wages',table(['WORKER','RATE RULE','DAYS PRESENT','OUTPUT','WAGE'],wageRows,'No workers yet.'),'DAILY × DAYS, OR PIECE × OUTPUT'):'');
}

async function accountsPage(){
 const owner=role==='Owner';
 const [recv,pay,pnl,costs]=await Promise.all([api('/reports/receivables'),api('/reports/payables'),owner?api('/reports/pnl?month='+reportMonth):null,owner?api('/reports/order-cost'):null]);
 invoiceBuyer=Object.fromEntries(recv.map(i=>[i.id,i.buyer_id]));
 const outstanding=recv.reduce((s,x)=>s+x.outstanding,0),payable=pay.reduce((s,x)=>s+x.outstanding,0);
 const payFields=select('PARTY','party',[['buyer','Buyer'],['supplier','Supplier']])+select('BUYER / SUPPLIER','party_id',[...M.buyers.map(b=>['buyer:'+b.id,'Buyer · '+b.name]),...M.suppliers.map(s=>['supplier:'+s.id,'Supplier · '+s.name])])+select('INVOICE (BUYER PAYMENTS)','invoice_id',optional(recv.filter(i=>i.outstanding>0.005).map(i=>[i.id,`INV-${String(i.id).padStart(5,'0')} · ${i.buyer} · ${money(i.outstanding)} due`]),'Supplier payment (no invoice)'),'',false)+field('AMOUNT (₹)','amount','','number')+field('DATE','paid_on','','date',today())+field('REFERENCE','ref','Bank / UTR','text','',false);
 return head('Accounts','COSTING / PAYMENTS / MONTH-END',owner?monthForm():'')+'<div class="grid four">'+(pnl?kpi('REVENUE · '+esc(pnl.month),money(pnl.revenue),'INVOICED, EX-GST'):'')+kpi('BUYER RECEIVABLES',money(outstanding),recv.length+' INVOICES')+kpi('SUPPLIER PAYABLES',money(payable),'GRN BILLS − PAYMENTS')+(pnl?kpi('PROFIT / LOSS',money(pnl.profit),'REVENUE − YARN − WAGES − OVERHEAD',pnl.profit<0?'warn':''):'')+'</div>'+
  (pnl?panel('Monthly P&L · '+esc(pnl.month),table(['REVENUE','YARN','WAGES','OVERHEAD','TOTAL COSTS','PROFIT'],[`<tr><td>${money(pnl.revenue)}</td><td>${money(pnl.yarn)}</td><td>${money(pnl.wages)}</td><td>${money(pnl.overhead)}</td><td>${money(pnl.total_costs)}</td><td><b>${money(pnl.profit)}</b></td></tr>`])):'')+
  formStart('payment','Record a payment',payFields,'RECORD PAYMENT')+
  (costs?panel('Cost per order',table(['ORDER','YARN ISSUED','WAGES','MACHINE OVERHEAD','TOTAL COST','REVENUE','MARGIN'],costs.map(c=>`<tr><td class="mono-cell">#${c.order_id}</td><td>${money(c.yarn)}</td><td>${money(c.wages)}</td><td>${money(c.overhead)}</td><td>${money(c.total_cost)}</td><td>${money(c.revenue)}</td><td>${money(c.margin)}</td></tr>`),'No orders yet.'),'YARN + LABOUR + MACHINE HOURS'):'')+
  '<div class="split">'+panel('Buyer receivables',table(['INVOICE','BUYER','TOTAL','PAID','OUTSTANDING'],recv.map(i=>`<tr><td class="mono-cell">INV-${String(i.id).padStart(5,'0')}</td><td>${esc(i.buyer)}</td><td>${money(i.total)}</td><td>${money(i.paid)}</td><td>${money(i.outstanding)}</td></tr>`),'No invoices yet.'))+panel('Supplier payables',table(['SUPPLIER','BILLED (GRNS)','PAID','OUTSTANDING'],pay.map(s=>`<tr><td>${esc(s.name)}</td><td>${money(s.billed)}</td><td>${money(s.paid)}</td><td>${money(s.outstanding)}</td></tr>`),'No suppliers yet.'))+'</div>';
}

const MASTERS={
 buyers:['Buyers',[['name','NAME','text',1],['phone','PHONE'],['email','EMAIL','email'],['gstin','GSTIN'],['address','ADDRESS']]],
 suppliers:['Suppliers',[['name','NAME','text',1],['phone','PHONE'],['gstin','GSTIN']]],
 yarn_types:['Yarn types',[['count','COUNT','text',1],['material','MATERIAL','text',1],['colour','COLOUR','text',1],['reorder_kg','REORDER LEVEL (KG)','number']]],
 fabric_types:['Fabric types',[['name','NAME','text',1],['hsn','HSN'],['gst_rate','GST %','number']]],
 bom:['Fabric recipes (BOM)',[['fabric_type_id','FABRIC','fabric_types',1],['yarn_type_id','YARN','yarn_types',1],['kg_per_m','KG YARN PER METRE','number',1]]],
 machines:['Machines',[['name','NAME','text',1],['kind','KIND',['spinning','weaving','dyeing'],1],['overhead_per_hour','OVERHEAD ₹ / HOUR','number'],['rate_m_per_hour','SPEED (M / HOUR)','number']]],
 workers:['Workers',[['name','NAME','text',1],['phone','PHONE'],['wage_type','WAGE TYPE',['daily','piece'],1],['rate','RATE ₹ (PER DAY, OR PER METRE)','number',1]]],
 shifts:['Shifts',[['name','NAME','text',1],['starts','STARTS','time'],['ends','ENDS','time']]]};
const refName=(type,v)=>type==='fabric_types'?nameOf(M.fabric_types,v):type==='yarn_types'?yarnName(byId(M.yarn_types,v)):v;
async function masterPage(){
 const [title,cols]=MASTERS[masterKey],data=M[masterKey],editing=byId(data,editMasterId),canEdit=['Owner','Office / Admin'].includes(role);
 const inputs=cols.map(([k,lbl,type='text',req])=>{const v=editing?.[k]??'';if(Array.isArray(type))return select(lbl,k,type.map(x=>[x,x]),v);if(type==='fabric_types')return select(lbl,k,choices(M.fabric_types),v);if(type==='yarn_types')return select(lbl,k,choices(M.yarn_types,yarnName),v);return field(lbl,k,'',type,v,!!req)}).join('');
 const tabs=Object.entries(MASTERS).map(([k,v])=>`<button class="tab-button master-tab ${k===masterKey?'active selected':''}" data-master="${k}">${v[0]}</button>`).join('');
 const rows=data.map(x=>`<tr><td class="mono-cell">${x.id}</td>${cols.map(([k,,type])=>`<td>${esc(refName(type,x[k]))}</td>`).join('')}${masterKey==='machines'?`<td>${badge(x.status)}</td>`:''}<td>${canEdit?`<div class="inline-actions"><button data-act="edit-master" data-id="${x.id}">EDIT</button></div>`:''}</td></tr>`);
 return head('Master data','FACTORY DIRECTORY / RECIPES')+`<div class="tabs">${tabs}</div><section class="panel"><div class="panel-head"><h2>${title}</h2><span class="mono">${editing?'EDITING #'+editing.id:'SHARED WITH EVERY SCREEN'}</span></div>`+(canEdit?`<form class="form-grid" data-form="master">${inputs}<div class="form-actions"><button class="primary">${editing?'SAVE CHANGES':'ADD RECORD'}</button>${editing?'<button type="button" class="secondary" data-act="cancel-master-edit">CANCEL</button>':''}</div></form>`:'')+table(['ID',...cols.map(c=>c[1]),...(masterKey==='machines'?['STATUS']:[]),''],rows,'Nothing added yet.')+'</section>';
}

async function reportsPage(){
 const [machines,wages,pnl,orders]=await Promise.all([api('/reports/machines?month='+reportMonth),api('/wages?month='+reportMonth),api('/reports/pnl?month='+reportMonth),api('/orders')]);
 const rows=machines.map(m=>`<tr><td>${esc(m.name)}</td><td>${esc(m.kind)}</td><td>${num(m.output)}</td><td>${num(m.hours)} h</td><td>${m.breakdowns}</td></tr>`);
 return head('Reports','OUTPUT / WAGES / MONTH-END',monthForm())+'<div class="grid four">'+kpi('OUTPUT · '+esc(reportMonth),num(machines.reduce((s,m)=>s+m.output,0)),'ALL MACHINES')+kpi('TOTAL WAGES',money(wages.total),'ATTENDANCE + PIECE')+kpi('PROFIT / LOSS',money(pnl.profit),'REVENUE '+money(pnl.revenue),pnl.profit<0?'warn':'')+kpi('ORDERS OVERDUE',orders.filter(isLate).length,'PAST DEADLINE, NOT READY')+'</div>'+panel('Output per machine',table(['MACHINE','KIND','OUTPUT','HOURS','BREAKDOWNS'],rows,'No machines yet.'),'FROM PRODUCTION ENTRIES')+'<div class="notice">EXPORT ↓ downloads the tables on screen as CSV. ⤓ prints or saves a PDF.</div>';
}

const pages={Dashboard:roleDashboard,Orders:orderPage,Production:productionPage,Inventory:inventoryPage,Quality:qualityPage,Traceability:tracePage,Dispatch:dispatchPage,Tracking:trackingPage,Inbox:inboxPage,Reviews:reviewsPage,Workforce:workforcePage,Accounts:accountsPage,Masters:masterPage,Reports:reportsPage,Assistant:assistantPage};
async function render(quiet){
 if(!allowed[role].includes(page))page=role==='Buyer'?'Tracking':'Dashboard';
 navRender();
 const seq=++renderSeq;let html;if(!quiet)document.body.classList.add('busy');
 try{html=await pages[page]()}catch(e){html=head(page,'COULD NOT LOAD')+`<div class="alert">${esc(e.message)}</div>`}
 if(seq!==renderSeq)return;
 document.body.classList.remove('busy');
 if(!quiet)shownFlash=flash;  // a result notice stays through the 10 s auto-refresh until the next thing you do
 $('#view').innerHTML=(shownFlash?`<div class="notice" style="margin-bottom:11px">${shownFlash}</div>`:'')+html;flash='';dirty=false;
 $$('#view .table').forEach(t=>{const h=[...t.querySelectorAll('th')].map(th=>th.textContent.trim());t.querySelectorAll('tbody tr').forEach(tr=>[...tr.children].forEach((td,i)=>td.dataset.label=h[i]||''))});
 $('#lastSaved').textContent='UPDATED '+new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
 if(afterRender){const f=afterRender;afterRender=null;f()}
}
// Live screens refresh every 10 s, but never while someone is typing, has a form half-filled, or a dialog open.
const LIVE_PAGES=['Dashboard','Production','Orders','Inventory','Tracking','Quality','Dispatch'];
$('#view').addEventListener('input',()=>{dirty=true});
setInterval(()=>{if(document.hidden||$('#app').classList.contains('hidden')||!LIVE_PAGES.includes(page)||dirty||$('#modal').open||document.activeElement?.closest('#view form, #view select'))return;render(true)},10000);
function setPage(p){if(!allowed[role].includes(p)){toast('Your role does not have access to '+p+'.');return}page=p;render();window.scrollTo({top:0,behavior:'smooth'})}

// ---------- Kept from the original prototype: landing animation ----------
// The opening is driven by scroll, not a timer: the loom film grows from its frame to fill the screen,
// the stills step aside, then the headline arrives. Short pin so it feels quick.
function showOpening(){
 const opening=$('#opening'),tile=$('#videoTile'),copy=$('.opening-copy');
 gsap.set(copy,{xPercent:-50,yPercent:-50,x:0,y:24,autoAlpha:0});
 const tl=gsap.timeline({defaults:{ease:'none'}})
  .to(tile,{width:()=>opening.clientWidth,height:()=>opening.clientHeight,maxHeight:'none',minHeight:0,borderRadius:0,borderWidth:0,duration:1},0)
  .to('.opening-tile:not(.video-tile)',{autoAlpha:0,duration:.55},.1)
  .to(['.film-label','.opening-cue','.opening-mark'],{autoAlpha:0,duration:.25},0)
  .to(copy,{autoAlpha:1,y:0,duration:.4,ease:'power1.out'},.62)
  .fromTo('.opening-progress i',{scaleX:0},{scaleX:1,duration:.4},.62);
 if(matchMedia('(prefers-reduced-motion: reduce)').matches){tl.progress(1);return}
 ScrollTrigger.create({animation:tl,trigger:opening,start:'top top',end:'+=75%',pin:true,scrub:.4,anticipatePin:1,invalidateOnRefresh:true,refreshPriority:1});
 ScrollTrigger.refresh();
}

// ---------- Forms ----------
const formData=form=>Object.fromEntries(new FormData(form).entries());
const handlers={
 async order(d){const o=await post('/orders',{buyer_id:+d.buyer_id,fabric_type_id:+d.fabric_type_id,colour:d.colour,qty_m:+d.qty_m,deadline:d.deadline,price_per_m:+d.price_per_m});flash=`Order <b>#${o.id}</b> created · yarn check: <b>${esc(o.feasibility.verdict)}</b>.`+poLine(o.po_refresh);return'Order saved.'},
 async grn(d){const r=await post('/grn',{supplier_id:+d.supplier_id,yarn_type_id:+d.yarn_type_id,qty_kg:+d.qty_kg,rate:+d.rate,order_id:intOrNull(d.order_id),challan_no:d.challan_no||null,po_line_id:intOrNull(d.po_line_id)});flash=`GRN-${r.grn.id} recorded · ${esc(yarnName(r.stock))} now ${num(r.stock.stock_kg)} kg. `+lowStockLine(r.low_stock)+poLine(r.po_refresh);return'Goods receipt recorded.'},
 async issue(d){const r=await post('/issues',{order_id:+d.order_id,yarn_type_id:+d.yarn_type_id,qty_kg:+d.qty_kg,machine_id:intOrNull(d.machine_id)});flash=lowStockLine(r.low_stock)+poLine(r.po_refresh);return'Yarn issued.'},
 async job(d){await post('/jobs',{order_id:+d.order_id,machine_id:+d.machine_id,operator_id:intOrNull(d.operator_id),shift_id:intOrNull(d.shift_id)});return'Job card assigned.'},
 async breakdown(d){const r=await post(`/machines/${d.machine_id}/status`,{status:'Breakdown',note:d.note});flash=`${esc(nameOf(M.machines,d.machine_id))} marked as breakdown. `+aiLine(r.triage,'triage')+(r.log.assignee?` · send: <b>${esc(r.log.assignee)}</b>`:'');return'Breakdown logged.'},
 async production(d,form){
  const body={shift_id:+d.shift_id,machine_id:+d.machine_id,order_id:+d.order_id,operator_id:+d.operator_id,output:+d.output};
  if(d.hours)body.hours=+d.hours;
  await post('/production',body);
  if(form.querySelector('[name=make_roll]')?.checked){const roll=await post('/rolls',{order_id:body.order_id,machine_id:body.machine_id,shift_id:body.shift_id,operator_id:body.operator_id,metres:body.output});afterRender=()=>qrModal(roll);return'Roll '+roll.qr+' created.'}
  return'Shift output recorded.'},
 async qc(d){d.qr=rollCode(d.qr);const r=await post('/qc/'+encodeURIComponent(d.qr),{result:d.result,defect_type:d.defect_type||null,notes:d.notes||null});flash=`Roll ${esc(d.qr)}: <b>${esc(r.qc.result)}</b>. `+aiLine(r.classification,'classified it')+alertLine(r.defect_alerts);return'QC result saved.'},
 async trace(d){traceRoll=rollCode(d.qr);return''},
 async 'tracking-select'(d){currentOrder=d.order;return''},
 async dispatch(d){if(!d.order_id)throw Error('No order has QC-passed rolls to ship yet.');await post('/dispatches',{order_id:+d.order_id,vehicle_no:d.vehicle_no,driver:d.driver});return'Truck opened. Scan rolls onto it.'},
 async scan(d){const qr=rollCode(d.qr);try{await post(`/dispatches/${d.did}/scan`,{qr})}catch(e){flash=`<b>${esc(e.message)}</b>`;await render();throw e}return'Roll '+qr+' loaded.'},
 async attendance(d,form){const ids=[...form.querySelectorAll('[name=worker_ids]:checked')].map(x=>+x.value);if(!ids.length)throw Error('Tick at least one worker.');await post('/attendance',{shift_id:+d.shift_id,work_date:d.work_date,worker_ids:ids});return`Attendance saved for ${ids.length} worker(s).`},
 async payment(d){const [party,pid]=d.party_id.split(':');if(party!==d.party)throw Error(`Pick a ${d.party} in "Buyer / Supplier".`);await post('/payments',{party,party_id:+pid,amount:+d.amount,invoice_id:party==='buyer'?intOrNull(d.invoice_id):null,paid_on:d.paid_on,ref:d.ref||null});return'Payment recorded.'},
 async master(d){
  const cols=MASTERS[masterKey][1],body={};
  for(const [k,,type='text']of cols){const v=d[k];if(v===''||v==null){if(editMasterId&&type!=='number')body[k]=null;continue}body[k]=type==='number'||type==='fabric_types'||type==='yarn_types'?Number(v):v}
  if(editMasterId)await put(`/master/${masterKey}/${editMasterId}`,body);else await post('/master/'+masterKey,body);
  editMasterId='';await loadMasters();return'Saved.'},
 async message(d){const r=await post('/messages',{text:d.text,channel:d.channel,buyer_id:intOrNull(d.buyer_id),order_id:intOrNull(d.order_id)});flash=r.message.status==='routed'?`Routed to <b>${esc(r.message.dept)}</b> · ${esc(label(r.message.intent))} · urgency ${esc(r.message.urgency)} <span class="mono">(${pct(r.routing.confidence)})</span>`:aiLine(r.routing,'routing');return'Message logged.'},
 async assistant(d){const t=$('#chatThread');if(t){t.querySelector('.empty')?.remove();t.insertAdjacentHTML('beforeend',`<div class="chat-msg user">${esc(d.text)}</div><div class="chat-msg bot typing">…</div>`);t.scrollTop=t.scrollHeight}$('#chatInput').value='';await post('/assistant/chat',{text:d.text});return''},
 async month(d){reportMonth=d.month||thisMonth();return''},
};
async function submit(form,e){
 e.preventDefault();const type=form.dataset.form,btn=form.querySelector('button.primary');if(btn)btn.disabled=true;
 try{const msg=await handlers[type](formData(form),form);await render();if(msg)toast(msg)}
 catch(err){toast(err.message||'Could not save this entry.','error')}
 finally{if(btn)btn.disabled=false}
}

// ---------- Buttons ----------
// ---------- Roll labels and the camera scanner ----------
// The label's QR is the roll's link, so any phone camera or scanner gun opens its traceability record.
async function qrModal(roll){
 const r=await api('/rolls/'+encodeURIComponent(roll.qr)).catch(()=>roll);  // the full record, for the label text
 const svg=await QRCode.toString(rollUrl(r.qr),{type:'svg',margin:0,errorCorrectionLevel:'M'});
 const label=`<div class="roll-label"><div class="rl-qr">${svg}</div><div class="rl-text"><div class="rl-brand">EVEREST TEXTILE MILLS</div><div class="rl-code">${esc(r.qr)}</div><div class="rl-big">${num(r.metres)} m</div><div>ORDER #${esc(r.order_id)}${r.buyer?' · '+esc(r.buyer):''}</div>${r.order?`<div>${esc(r.order.fabric)} · ${esc(r.order.colour)}</div>`:''}${r.machine?`<div>${esc(r.machine)} · ${esc(r.shift)}${r.operator?' · '+esc(r.operator):''}</div>`:''}${r.made_on?`<div>MADE ${esc(r.made_on)}</div>`:''}</div></div>`;
 $('#labelSheet').innerHTML=label;
 $('#modalContent').innerHTML=`<div class="eyebrow"><i></i> ROLL LABEL · 100 × 75 MM</div><h2>${esc(r.qr)}</h2><p class="help">Stick it on the roll. Any phone camera or scanner gun opens its record.</p><div class="label-preview">${label}</div><div class="form-actions"><button type="button" class="primary" data-act="print-label">PRINT LABEL</button></div>`;
 $('#modal').showModal();
}
function printLabel(){document.body.classList.add('print-label');window.print();setTimeout(()=>document.body.classList.remove('print-label'),500)}

// Camera scanning: the browser's own QR detector where there is one (Chrome, Android), jsQR otherwise (iPhone).
let scanStream=null,scanLoop=null;
function stopScan(){cancelAnimationFrame(scanLoop);scanLoop=null;scanStream?.getTracks().forEach(t=>t.stop());scanStream=null}
async function startScan(target,id){
 $('#modalContent').innerHTML=`<div class="eyebrow"><i></i> SCAN A ROLL</div><h2>Point at the roll's QR</h2><div class="scan-view"><video id="scanVideo" playsinline muted></video><i></i></div><p class="help" id="scanStatus">Starting the camera…</p><div class="form-actions"><button type="button" class="secondary" data-act="scan-done">DONE</button></div>`;
 $('#modal').showModal();
 try{scanStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}},audio:false})}
 catch(e){$('#scanStatus').innerHTML=`<b>Camera unavailable.</b> ${/notallowed|permission/i.test(e.name+e.message)?'Allow the camera for this site.':'The camera needs HTTPS (or localhost).'} You can still type or use a scanner gun.`;return}
 const video=$('#scanVideo');video.srcObject=scanStream;await video.play().catch(()=>{});
 $('#scanStatus').textContent=target==='dispatch'?'Scan rolls one after another; each loads onto the truck.':'Hold steady…';
 const detector='BarcodeDetector' in window?new BarcodeDetector({formats:['qr_code']}):null;
 const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d',{willReadFrequently:true});
 let last='',lastAt=0,busy=false,next=0;
 const tick=async t=>{
  if(!scanStream)return;scanLoop=requestAnimationFrame(tick);
  if(busy||t<next||video.readyState<2)return;busy=true;next=t+150;
  try{
   let raw='';
   if(detector)raw=(await detector.detect(video))[0]?.rawValue||'';
   else{canvas.width=video.videoWidth;canvas.height=video.videoHeight;ctx.drawImage(video,0,0);raw=jsQR(ctx.getImageData(0,0,canvas.width,canvas.height).data,canvas.width,canvas.height)?.data||''}
   const code=rollCode(raw);
   if(code&&(code!==last||performance.now()-lastAt>3000)){last=code;lastAt=performance.now();navigator.vibrate?.(60);await scanned(target,id,code)}
  }catch{}finally{busy=false}
 };
 scanLoop=requestAnimationFrame(tick);
}
async function scanned(target,id,code){
 if(target==='dispatch'){  // keep scanning: each roll goes straight onto the truck
  try{await post(`/dispatches/${id}/scan`,{qr:code});$('#scanStatus').innerHTML=`<b>Loaded ${esc(code)}.</b> Next roll…`}
  catch(e){$('#scanStatus').innerHTML=`<b>${esc(code)}: ${esc(e.message)}</b>`}
  return}
 stopScan();$('#modal').close();
 if(target==='trace'){traceRoll=code;return render()}
 const form=$('form[data-form=qc]');if(form){form.elements.qr.value=code;form.elements.result.focus();toast('Scanned '+code+'. Choose the result.')}
}
// Buyers get their own page (track.html): their order and a call to the order desk, outside the staff app.
const trackUrl=t=>location.origin+'/track.html#track='+encodeURIComponent(t);
function copyLink(t){const url=trackUrl(t);navigator.clipboard?.writeText(url).then(()=>toast('Buyer tracking link copied.')).catch(()=>toast(url))}
async function dispatchModal(id,kind){
 const d=await api('/dispatches/'+id),inv=d.invoice;
 if(kind==='invoice'&&inv)$('#modalContent').innerHTML=`<div class="eyebrow"><i></i> EVEREST TEXTILE MILLS · TAX INVOICE</div><h2>INV-${String(inv.id).padStart(5,'0')}</h2><p>${esc(d.buyer)}${d.buyer_gstin?' · GSTIN '+esc(d.buyer_gstin):''}<br>${esc(d.buyer_address||'')}</p><p>Order #${d.order_id} · Dispatch ${d.id} · ${esc(inv.issued_on)} · Vehicle ${esc(d.vehicle_no)}</p>${table(['DESCRIPTION','QUANTITY','RATE','AMOUNT'],[`<tr><td>${esc(d.fabric)} · ${esc(d.colour)} (${d.rolls} rolls)</td><td>${num(inv.metres)} m</td><td>${money(inv.rate)} / m</td><td>${money(inv.subtotal)}</td></tr>`,`<tr><td colspan="3">GST @ ${inv.gst_rate}%</td><td>${money(inv.gst)}</td></tr>`,`<tr><td colspan="3"><b>TOTAL</b></td><td><b>${money(inv.total)}</b></td></tr>`])}<button class="secondary" type="button" onclick="window.print()">PRINT / SAVE PDF</button>`;
 else $('#modalContent').innerHTML=`<div class="eyebrow"><i></i> EVEREST TEXTILE MILLS · DISPATCH NOTE</div><h2>PACKING LIST · ${d.id}</h2><p>Order #${d.order_id} · ${esc(d.buyer)} · Vehicle ${esc(d.vehicle_no)} · Driver ${esc(d.driver)}</p>${table(['ROLL','METRES'],d.packing_list.map(p=>`<tr><td>${esc(p.qr)}</td><td>${num(p.metres)} m</td></tr>`))}<p>Total: ${d.rolls} rolls · ${num(d.metres)} m</p><button class="secondary" type="button" onclick="window.print()">PRINT / SAVE PDF</button>`;
 $('#modal').showModal();
}
async function action(act,el){
 const id=el.dataset.id;
 try{
  if(act==='edit-master'){editMasterId=id;return render()}
  if(act==='digest-refresh')return loadDigest(true);
  if(act==='defect-group'){defectGroup=id;return render()}
  if(act==='alert-ack'){await post(`/alerts/defects/${id}/ack`);await render();return toast('Marked as being handled. It clears by itself once the failure rate is back to normal.')}
  if(act==='po-refresh'){const r=await post('/purchase-orders/refresh');await render();return toast(`${r.drafted.length} draft line(s)`+(r.skipped.length?`, ${r.skipped.length} yarn(s) with no supplier history: raise those by hand.`:'.'))}
  if(act==='po-approve'){if(!confirm(`Approve PO-${id}? This means it has been placed with the supplier.`))return;await post(`/purchase-orders/${id}/approve`);await render();return toast(`PO-${id} approved. Book the delivery against it in the goods receipt form.`)}
  if(act==='po-cancel'){if(!confirm(`Cancel PO-${id}?`))return;await post(`/purchase-orders/${id}/cancel`);await render();return toast(`PO-${id} cancelled.`)}
  if(act==='cancel-master-edit'){editMasterId='';return render()}
  if(act==='order-link')return copyLink(el.dataset.token);
  if(act==='open-track'){currentOrder=id;if(page!=='Tracking')return setPage('Tracking');return render()}
  if(act==='feasibility'){const f=await api(`/orders/${id}/feasibility`);$('#feasibilityResult').innerHTML=`<b>ORDER #${id}: ${esc(f.verdict.toUpperCase())}</b><br>`+f.lines.map(l=>`${esc(l.count)} ${esc(l.material)} · ${esc(l.colour)}: need ${num(l.required_kg)} kg · issued ${num(l.issued_kg)} · in stock ${num(l.available_kg)}${l.short_kg?` · <b class="warn">SHORT ${num(l.short_kg)} kg</b>`:''}`).join('<br>');return}
  if(act==='risk'){$('#feasibilityResult').textContent='Asking Jev…';const r=await api(`/orders/${id}/risk`);$('#feasibilityResult').innerHTML=r.probability==null?`Order #${id}: risk unknown (AI unavailable). ${r.progress_pct}% done, ${r.days_left} days left, yarn short ${num(r.yarn_short_kg)} kg.`:`<b>ORDER #${id}: ${r.at_risk?'AT RISK':'ON TRACK'}</b> · Jev puts the chance of missing the deadline at <b>${pct(r.probability)}</b>.<br>${r.progress_pct}% done · ${r.days_left} days left · yarn short ${num(r.yarn_short_kg)} kg · ${r.free_machines} idle machines`;return}
  if(act==='scan-roll'){const form=$('form[data-form=qc]');if(form){form.elements.qr.value=el.dataset.qr;form.elements.result.value='reject';form.scrollIntoView({behavior:'smooth',block:'start'});form.elements.notes.focus({preventScroll:true})}return}
  if(act==='qc-pass'){await post('/qc/'+encodeURIComponent(el.dataset.qr),{result:'pass'});await render();return toast('Roll '+el.dataset.qr+' passed.')}
  if(act==='trace'){traceRoll=el.dataset.qr;return setPage('Traceability')}
  if(act==='roll-qr')return qrModal({qr:el.dataset.qr,order_id:el.dataset.order,metres:el.dataset.metres,status:el.dataset.status});
  if(act==='print-label')return printLabel();
  if(act==='scan')return startScan(el.dataset.target,el.dataset.id);
  if(act==='scan-done'){stopScan();$('#modal').close();return el.closest('#modal')&&render()}
  if(act==='close-dispatch'){if(!confirm('Close this truck and raise the invoice? No more rolls can be added.'))return;const d=await post(`/dispatches/${id}/close`);flash=`Dispatch ${d.id} closed · invoice INV-${String(d.invoice.id).padStart(5,'0')} for <b>${money(d.invoice.total)}</b> (${d.rolls} rolls, ${num(d.metres)} m).`;await render();return dispatchModal(id,'invoice')}
  if(act==='load-roll'){try{await post(`/dispatches/${id}/scan`,{qr:el.dataset.qr})}catch(e){flash=`<b>${esc(e.message)}</b>`;await render();throw e}await render();return toast('Roll '+el.dataset.qr+' loaded.')}
  if(act==='print-invoice')return dispatchModal(id,'invoice');
  if(act==='print-dispatch')return dispatchModal(id,'packing');
  if(act==='draft'){const l=$(`[data-lang-for="${id}"]`)?.value||'en';toast('Drafting reply from live order data…');const r=await post(`/messages/${id}/draft`,{lang:l});$('#modalContent').innerHTML=`<div class="eyebrow"><i></i> REPLY DRAFT · ${esc(l.toUpperCase())}</div><h2>Check, edit, then send</h2><label class="field"><span>REPLY</span><textarea id="replyText" rows="6">${esc(r.draft)}</textarea></label><p class="help">Copy it into WhatsApp or email, then mark it as sent so the inbox knows it was answered.</p><div class="form-actions"><button type="button" class="secondary" data-act="copy-reply">COPY</button><button type="button" class="primary" data-act="send-reply" data-id="${id}">MARK AS SENT</button></div>`;$('#modal').showModal();return}
  if(act==='copy-reply'){navigator.clipboard?.writeText($('#replyText').value).then(()=>toast('Reply copied.'));return}
  if(act==='send-reply'){await post(`/messages/${id}/reply`,{text:$('#replyText').value});$('#modal').close();await render();return toast('Reply recorded.')}
  if(act==='review-save'){const row=el.closest('[data-review]'),fields=Object.fromEntries([...row.querySelectorAll('[data-field]')].filter(s=>s.value).map(s=>[s.dataset.field,s.value]));const r=await post(`/reviews/${id}`,fields);await render();return toast('Applied: '+Object.entries(r.applied).map(([k,v])=>label(k)+' '+label(v)).join(', '))}
 }catch(e){toast(e.message,'error')}
}
async function machineStatus(id,status,el){
 let note=null;
 if(status==='Breakdown'){note=prompt('What happened? Jev will triage it. (Leave empty to skip triage.)')||null}
 try{const r=await post(`/machines/${id}/status`,{status,note});if(r.triage)flash=`${esc(nameOf(M.machines,id))}: `+aiLine(r.triage,'triage')+(r.log.assignee?` · send: <b>${esc(r.log.assignee)}</b>`:'');await render();toast(nameOf(M.machines,id)+' → '+status)}
 catch(e){toast(e.message,'error');render()}
}

// ---------- Export / language ----------
function doCSV(){const rows=$$('#view .table tbody tr, #view .register tr').map(tr=>[...tr.children].map(td=>td.innerText.trim()));const csv=[['EVEREST TEXTILE MILLS',page,today()],...rows].map(r=>r.map(v=>{v=String(v);if(/^[=+\-@\t\r]/.test(v))v="'"+v;return'"'+v.replaceAll('"','""')+'"'}).join(',')).join('\r\n'),a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download=`everest-${page.toLowerCase()}-${today()}.csv`;a.click();URL.revokeObjectURL(a.href)}
function switchLanguage(next){lang=next;try{localStorage.setItem('everest-lang',lang)}catch{}navRender();toast(lang==='ta'?'தமிழ் வழிசெலுத்தல் இயக்கப்பட்டது':'English navigation enabled')}

// ---------- Sign in / out ----------
let accessMode='Staff';
function setLoginMode(mode){
 accessMode=mode;const buyer=mode==='Buyer';
 $$('[data-access]').forEach(b=>{const active=b.dataset.access===mode;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active))});
 $('#loginTitle').textContent=buyer?'Track your order':'Staff sign in';
 $('#loginSubtitle').textContent=buyer?'Paste the tracking link or code the mill sent you. No account needed.':'Use your mill username and password.';
 $('#loginIdentityLabel').textContent=buyer?'TRACKING LINK OR CODE':'USERNAME';
 $('#loginIdentity').placeholder=buyer?'https://…#track=…':'e.g. owner';
 $('#loginSubmitLabel').textContent=buyer?'VIEW MY ORDER':'SIGN IN';
 const pw=$('#loginPassword');pw.closest('label').classList.toggle('hidden',buyer);pw.required=!buyer;
 $('#ownerLoginExtra').classList.toggle('hidden',buyer);$('#buyerLoginExtra').classList.toggle('hidden',!buyer);
 $('#loginMessage').textContent='';$('#loginIdentity').value='';pw.value='';
}
const showGate=()=>setLoginMode('Staff');
const showAuthMessage=m=>{$('#loginMessage').textContent=m};
function openApp(){$('#landing').classList.add('hidden');$('#gate').classList.add('hidden');$('#app').classList.remove('hidden');render()}
async function startSession(t){
 token=t;store.set('everest-token',t);
 me=await api('/me');role=ROLE_LABEL[me.role];await loadMasters();
 page='Dashboard';
 if(pendingRoll&&allowed[role].includes('Traceability')){traceRoll=rollCode(pendingRoll);page='Traceability'}
 pendingRoll='';openApp();
}
async function submitLogin(e){
 e.preventDefault();const identity=$('#loginIdentity').value.trim(),password=$('#loginPassword').value;
 if(accessMode==='Buyer'){location.href=trackUrl(identity.includes('track=')?decodeURIComponent(identity.split('track=')[1]):identity);return}
 try{const r=await post('/auth/login',{username:identity,password});await startSession(r.token)}catch(err){showAuthMessage(err.message)}
}
async function firstSetup(){
 const username=$('#loginIdentity').value.trim(),password=$('#loginPassword').value;
 if(!username||password.length<8)return showAuthMessage('Type the owner username and a password of at least 8 characters first.');
 try{const r=await post('/auth/setup',{username,password});await startSession(r.token);toast('Owner account created.')}catch(err){showAuthMessage(err.message)}
}
function signOut(){if(token)fetch(API+'/auth/logout',{method:'POST',headers:{Authorization:'Bearer '+token}}).catch(()=>{});token=null;me=null;store.set('everest-token',null);currentOrder='';currentToken='';traceRoll='';shownFlash='';editMasterId='';digestCache=null;poLines={};flash='';if(!$('#app').classList.contains('hidden')){$('#app').classList.add('hidden');$('#gate').classList.remove('hidden');showGate()}}

// ---------- Events ----------
$('#sideNav').addEventListener('click',e=>{const b=e.target.closest('[data-page]');if(b)setPage(b.dataset.page)});
$('#view').addEventListener('click',e=>{const nav=e.target.closest('[data-page]');if(nav)return setPage(nav.dataset.page);const act=e.target.closest('[data-act]');if(act)return action(act.dataset.act,act);const mt=e.target.closest('[data-master]');if(mt){masterKey=mt.dataset.master;editMasterId='';render()}});
$('#modal').addEventListener('close',()=>{const was=!!scanStream;stopScan();if(was&&page==='Dispatch')render(true)});  // camera off; show rolls loaded by camera
$('#modal').addEventListener('click',e=>{const act=e.target.closest('[data-act]');if(act){e.preventDefault();action(act.dataset.act,act)}});
$('#view').addEventListener('submit',e=>{const f=e.target.closest('form[data-form]');if(f)submit(f,e)});
$('#view').addEventListener('change',async e=>{
 const s=e.target.dataset.machineStatus,o=e.target.dataset.machineOrder;
 if(s)return machineStatus(s,e.target.value,e.target);
 if(e.target.dataset.poLine){try{await put('/po-lines/'+e.target.dataset.poLine,{qty_kg:+e.target.value});dirty=false;await render();toast('Quantity saved. The system will no longer recompute this line.')}catch(err){toast(err.message,'error')}return}
 const form=e.target.form;
 if(form?.dataset.form==='grn'&&e.target.name==='po_line_id'&&e.target.value){const l=poLines[e.target.value];if(l){form.elements.supplier_id.value=l.supplier_id;form.elements.yarn_type_id.value=l.yarn_type_id;form.elements.qty_kg.value=l.open_kg;form.elements.rate.value=l.rate}return}
 if(form?.dataset.form==='payment'&&e.target.name==='invoice_id'&&e.target.value){form.elements.party.value='buyer';form.elements.party_id.value='buyer:'+invoiceBuyer[e.target.value];return}  // an invoice names its buyer
 if(form?.dataset.form==='message'){
  if(e.target.name==='order_id'&&e.target.value)form.elements.buyer_id.value=orderBuyer[e.target.value]??'';
  if(e.target.name==='buyer_id'&&form.elements.order_id.value&&String(orderBuyer[form.elements.order_id.value])!==e.target.value)form.elements.order_id.value='';
  return}
 if(form&&['production','breakdown'].includes(form.dataset.form)&&e.target.name==='machine_id'&&form.elements.order_id){const o=machineOrder[e.target.value];if(o&&[...form.elements.order_id.options].some(x=>x.value==String(o)))form.elements.order_id.value=o;return}
 if(o&&e.target.value){try{await post('/jobs',{order_id:+e.target.value,machine_id:+o});await render();toast('Job card assigned.')}catch(err){toast(err.message,'error')}return}
 if(e.target.matches('[data-challan]')&&e.target.files[0]){
  const file=e.target.files[0],form=e.target.closest('form');toast('Reading the challan…');
  const b64=await new Promise((ok,bad)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(',')[1]);r.onerror=bad;r.readAsDataURL(file)});
  try{const r=await post('/grn/challan',{image_b64:b64,mime:file.type||'image/jpeg'}),s=r.suggested;for(const [k,v]of Object.entries(s))if(v!=null&&form.elements[k])form.elements[k].value=v;toast(`Filled from the challan${r.extracted.supplier?' ('+r.extracted.supplier+')':''}. Check it before saving.`)}catch(err){toast(err.message)}
 }
});
$('#exportButton').addEventListener('click',doCSV);
$('#printButton').addEventListener('click',()=>window.print());
$('#roleSwitch').addEventListener('click',signOut);
$('#topSignOut').addEventListener('click',signOut);
$('#topLang').addEventListener('click',()=>switchLanguage(lang==='en'?'ta':'en'));
$('#languageToggle').addEventListener('click',()=>switchLanguage(lang==='en'?'ta':'en'));
$('#loginForm').addEventListener('submit',submitLogin);
$('#gate').addEventListener('click',e=>{
 const tab=e.target.closest('[data-access]');if(tab){setLoginMode(tab.dataset.access);return}
 if(e.target.closest('#firstSetup')){firstSetup();return}
 if(e.target.id==='backToStory'){e.preventDefault();$('#gate').classList.add('hidden');$('#landing').classList.remove('hidden');ScrollTrigger.refresh();location.hash='story';$('#story').scrollIntoView({behavior:'smooth'})}
});
$('#enterWorkspace').addEventListener('click',()=>{$('#landing').classList.add('hidden');$('#gate').classList.remove('hidden');window.scrollTo({top:0,behavior:'smooth'})});
$('#topLogin').addEventListener('click',()=>$('#enterWorkspace').click());

// Scroll-linked editorial panels: each page rises over the previous one while its
// content and imagery move with the visitor, so the story feels like a sequence of scenes.
function initStoryScroll(){
 initLoomJourney();
 if(window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;
 const panels=gsap.utils.toArray('.story > section');
 const targetsByPanel=[
  '.story-intro .eyebrow, .story-intro h2, .story-intro-copy, .story-visual',
  '.story-flow .section-kicker, .flow-line article',
  '.capability-heading',
  '.owner-copy, .owner-chart',
  '.story-cta > *'
 ];
 panels.forEach((panel,index)=>{
  const targets=panel.querySelectorAll(targetsByPanel[index]||':scope > *');
  gsap.fromTo(targets,{y:64,autoAlpha:0},{y:0,autoAlpha:1,ease:'none',stagger:.1,scrollTrigger:{trigger:panel,start:'top 92%',end:'top 48%',scrub:.65,invalidateOnRefresh:true}});
  const image=panel.querySelector('.story-visual img');
  if(image)gsap.fromTo(image,{yPercent:-14,scale:1.18},{yPercent:14,scale:1.03,ease:'none',scrollTrigger:{trigger:panel,start:'top bottom',end:'bottom top',scrub:true,invalidateOnRefresh:true}});
  const capabilityCards=panel.querySelectorAll('.capability');
  capabilityCards.forEach(card=>gsap.fromTo(card,{y:52,autoAlpha:.15},{y:0,autoAlpha:1,ease:'none',scrollTrigger:{trigger:panel,start:'top 88%',end:'top 38%',scrub:.7,invalidateOnRefresh:true}}));
  const navLink=$$('.story-links a').find(link=>link.getAttribute('href')===`#${panel.id}`);
  if(navLink)ScrollTrigger.create({trigger:panel,start:'top 44%',onEnter:()=>setActiveStoryLink(navLink),onEnterBack:()=>setActiveStoryLink(navLink)});
  const trace=panel.querySelector('.owner-chart svg path');
  if(trace&&trace.getTotalLength){const length=trace.getTotalLength();gsap.set(trace,{strokeDasharray:length,strokeDashoffset:length});gsap.to(trace,{strokeDashoffset:0,ease:'none',scrollTrigger:{trigger:panel,start:'top 78%',end:'top 28%',scrub:true}})}
 });
 const progress=$('#storyProgress');
 if(progress)gsap.to(progress,{scaleX:1,ease:'none',scrollTrigger:{trigger:'.story',start:'top top',end:'bottom bottom',scrub:true}});
}
function initLoomJourney(){
 const canvas=$('#loomStoryCanvas'),journey=$('#loomJourney');
 if(!canvas||!journey)return;
 const ctx=canvas.getContext('2d');if(!ctx)return;
 const state={progress:0,width:0,height:0,dpr:1};
 const chapterLabels=['CHALLAN · SUPPLIER · RATE','RECIPE · SHELF · PROMISED','OPERATOR · METRES · HOURS','LABEL · BENCH · LOADING BAY'];
 const clamp=(value,min=0,max=1)=>Math.max(min,Math.min(max,value));
 function resize(){
  const rect=canvas.getBoundingClientRect();state.dpr=Math.min(window.devicePixelRatio||1,2);state.width=rect.width;state.height=rect.height;
  canvas.width=Math.round(rect.width*state.dpr);canvas.height=Math.round(rect.height*state.dpr);
  ctx.setTransform(state.dpr,0,0,state.dpr,0,0);draw();
 }
 function draw(){
  const w=state.width,h=state.height,p=state.progress;if(!w||!h)return;
  ctx.clearRect(0,0,w,h);
  const small=w<700,loomLeft=w*(small?.34:.57),loomRight=w*(small?.91:.96),loomTop=h*(small?.14:.19),loomBottom=h*(small?.49:.77),loomW=loomRight-loomLeft;
  const prep=clamp((p-.08)/.28),weave=clamp((p-.32)/.46),finish=clamp((p-.74)/.26);
  const ink='#39352e',copper='#a45c3b',gold='#bd865b',thread='#d4a875';
  // Quiet drafting marks give the moving yarn a sense of scale and direction.
  ctx.save();ctx.globalAlpha=.16;ctx.strokeStyle='#8f887c';ctx.lineWidth=1;
  for(let i=0;i<9;i++){const x=loomLeft+i*loomW/8;ctx.beginPath();ctx.moveTo(x,loomTop-18);ctx.lineTo(x,loomTop-8);ctx.stroke()}
  ctx.restore();
  // A cone of yarn and its continuous strand.
  const coneX=w*(small?.20:.51),coneY=h*(small?.34:.58),coneR=Math.min(w*(small?.052:.034),small?26:32);
  ctx.save();ctx.globalAlpha=1-finish*.38;
  ctx.fillStyle='#c58c61';ctx.beginPath();ctx.moveTo(coneX-coneR*.65,coneY+coneR*1.55);ctx.lineTo(coneX-coneR*.38,coneY-coneR*.72);ctx.quadraticCurveTo(coneX,coneY-coneR*1.05,coneX+coneR*.38,coneY-coneR*.72);ctx.lineTo(coneX+coneR*.65,coneY+coneR*1.55);ctx.closePath();ctx.fill();
  ctx.strokeStyle='#e5c4a1';ctx.lineWidth=2;
  for(let i=-3;i<=3;i++){ctx.beginPath();ctx.ellipse(coneX,coneY+i*coneR*.18,coneR*.48,coneR*.11,0,0,Math.PI*2);ctx.stroke()}
  ctx.fillStyle='#56463a';ctx.beginPath();ctx.ellipse(coneX,coneY+coneR*1.55,coneR*.66,coneR*.13,0,0,Math.PI*2);ctx.fill();
  ctx.restore();
  // The loom frame assembles as the story moves from raw material to setup.
  ctx.save();ctx.globalAlpha=.22+prep*.78;ctx.strokeStyle=ink;ctx.lineWidth=Math.max(3,w*.004);ctx.lineCap='round';
  ctx.strokeRect(loomLeft,loomTop,loomW,loomBottom-loomTop);
  ctx.lineWidth=Math.max(7,w*.009);ctx.strokeStyle='#8c7258';
  [loomTop+5,loomBottom-5].forEach(y=>{ctx.beginPath();ctx.moveTo(loomLeft-7,y);ctx.lineTo(loomRight+7,y);ctx.stroke()});
  ctx.restore();
  // Fine warp threads become visible one by one, then hold the woven cloth.
  const warpCount=small?15:27;
  ctx.save();ctx.globalAlpha=.12+prep*.88;ctx.lineWidth=small?1.2:1.55;
  for(let i=0;i<warpCount;i++){
   const x=loomLeft+10+(loomW-20)*i/(warpCount-1),visible=clamp(prep*1.55-i/(warpCount-1)*.18);
   ctx.globalAlpha=(.12+prep*.8)*visible;ctx.strokeStyle=i%3===0?gold:thread;ctx.beginPath();ctx.moveTo(x,loomTop+12);ctx.bezierCurveTo(x-4,loomTop+(loomBottom-loomTop)*.38,x+4,loomTop+(loomBottom-loomTop)*.64,x,loomBottom-12);ctx.stroke();
  }
  ctx.restore();
  // The shuttle travels back and forth, laying each weft across the warp.
  const rows=Math.floor(weave*(small?18:28));
  for(let row=0;row<rows;row++){
   const y=loomTop+18+(loomBottom-loomTop-36)*(row+1)/(small?19:29),wave=Math.sin(row*.55)*5;
   ctx.globalAlpha=.35+weave*.6;ctx.strokeStyle=row%2===0?copper:'#d0a16d';ctx.lineWidth=small?2:2.6;ctx.beginPath();ctx.moveTo(loomLeft+11,y);
   for(let k=1;k<=8;k++){const x=loomLeft+11+(loomW-22)*k/8;ctx.quadraticCurveTo(x-(loomW/16),y+wave*(k%2?1:-1),x,y)}ctx.stroke();
  }
  ctx.globalAlpha=1;
  // Yarn path visibly joins the cone to the loom; its bead follows scroll progress.
  const feed=clamp(p/.38),startX=coneX+coneR*.5,startY=coneY-coneR*.55,endX=loomLeft,endY=loomTop+12;
  ctx.strokeStyle=copper;ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(startX,startY);ctx.bezierCurveTo(startX+w*.08,startY-h*.2,endX-w*.08,endY-h*.12,endX,endY);ctx.stroke();
  const beadT=feed,beadX=startX+(endX-startX)*beadT,beadY=startY+(endY-startY)*beadT-Math.sin(beadT*Math.PI)*h*.11;
  ctx.fillStyle='#f8f2e9';ctx.strokeStyle=copper;ctx.lineWidth=2;ctx.beginPath();ctx.arc(beadX,beadY,5,0,Math.PI*2);ctx.fill();ctx.stroke();
  if(weave>.03){
   const shuttleT=(weave*8)%1,shuttleX=loomLeft+14+(loomW-28)*(Math.floor(weave*8)%2?1-shuttleT:shuttleT),shuttleY=loomTop+18+(loomBottom-loomTop-36)*clamp(weave);
   ctx.save();ctx.translate(shuttleX,shuttleY);ctx.fillStyle='#493e33';ctx.strokeStyle='#f1d0a5';ctx.lineWidth=1;ctx.beginPath();if(ctx.roundRect)ctx.roundRect(small?-18:-26,-7,small?36:52,14,7);else ctx.rect(small?-18:-26,-7,small?36:52,14);ctx.fill();ctx.stroke();ctx.restore();
  }
  // The finished length winds into a roll after inspection and traceability appear.
  if(finish>0){
   const rollX=loomRight+Math.min(w*.075,82)*finish,rollY=loomTop+(loomBottom-loomTop)*.62,rx=Math.min(w*.048,35),ry=rx*.48;
   ctx.save();ctx.globalAlpha=finish;ctx.fillStyle='#b77e53';ctx.fillRect(rollX-rx,rollY-ry,rx*2,ry*2);
   ctx.fillStyle='#e4c19a';ctx.beginPath();ctx.ellipse(rollX+rx,rollY,ry*.52,ry,0,0,Math.PI*2);ctx.fill();
   ctx.strokeStyle='#754f39';ctx.lineWidth=2;ctx.beginPath();ctx.ellipse(rollX+rx,rollY,ry*.3,ry*.58,0,0,Math.PI*2);ctx.stroke();
   ctx.restore();
  }
  // Stage marker progresses from loose yarn to a complete, traceable roll.
  const markerY=h*.87,markerX=w*.58,markerW=w*.31;ctx.strokeStyle='#29272230';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(markerX,markerY);ctx.lineTo(markerX+markerW,markerY);ctx.stroke();
  ctx.fillStyle=copper;ctx.beginPath();ctx.arc(markerX+markerW*p,markerY,4,0,Math.PI*2);ctx.fill();
  const chapter=Math.min(3,Math.floor(p*4));document.querySelectorAll('.journey-chapter').forEach((el,i)=>el.classList.toggle('active',i===chapter));
  const counter=$('#journeyCounter'),step=$('#journeyStep');if(counter)counter.textContent=`0${chapter+1} / 04`;if(step)step.textContent=chapterLabels[chapter];
 }
 resize();window.addEventListener('resize',resize,{passive:true});
 if(window.matchMedia('(prefers-reduced-motion: reduce)').matches){state.progress=1;draw();return}
 gsap.to(state,{progress:1,ease:'none',scrollTrigger:{trigger:journey,start:'top top',end:'bottom bottom',scrub:.45,invalidateOnRefresh:true,onUpdate:draw}});
}
function setActiveStoryLink(active){
 $$('.story-links a').forEach(link=>{const selected=link===active;link.classList.toggle('active',selected);if(selected)link.setAttribute('aria-current','location');else link.removeAttribute('aria-current')});
}
initStoryScroll();

// ---------- Start ----------
(async()=>{
 showGate();
 if(location.hash.startsWith('#track=')){location.replace('/track.html'+location.hash);return}  // links sent before the buyer page existed
 if(location.hash.startsWith('#roll='))pendingRoll=decodeURIComponent(location.hash.slice(6));
 if(token){try{return await startSession(token)}catch{token=null;store.set('everest-token',null)}}
 if(pendingRoll){$('#landing').classList.add('hidden');$('#gate').classList.remove('hidden');return}
 showOpening();
})();
})();
