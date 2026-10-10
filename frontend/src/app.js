/* Everest Textile Mills · operations frontend.
   All data lives on the mill server (backend/). This file only renders it and sends what people enter.
   API base: window.EVEREST_API if set, else /api (the Vite dev server proxies /api to localhost:8000). */
(()=>{'use strict';
const API=window.EVEREST_API||'/api';
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const today=()=>new Date().toISOString().slice(0,10),thisMonth=()=>today().slice(0,7);
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
 Owner:['Dashboard','Orders','Production','Inventory','Quality','Traceability','Dispatch','Tracking','Inbox','Reviews','Workforce','Accounts','Masters','Reports'],
 'Office / Admin':['Dashboard','Orders','Production','Inventory','Quality','Traceability','Dispatch','Tracking','Inbox','Reviews','Workforce','Accounts','Masters'],
 'Store Keeper':['Dashboard','Inventory','Orders','Dispatch','Traceability','Tracking'],
 'Floor Supervisor':['Dashboard','Production','Orders','Traceability','Tracking','Reviews','Workforce'],
 'QC Inspector':['Dashboard','Quality','Traceability','Tracking','Reviews'],
 Buyer:['Tracking']};
const navGroups=[['FACTORY',['Dashboard','Orders','Production','Inventory']],['QUALITY & DELIVERY',['Quality','Traceability','Dispatch','Tracking']],['BUYERS & AI',['Inbox','Reviews']],['PEOPLE & FINANCE',['Workforce','Accounts']],['SETUP',['Masters','Reports']]];
const icons={Dashboard:'◫',Orders:'▤',Production:'▥',Inventory:'◈',Quality:'◉',Traceability:'⌕',Dispatch:'⇢',Tracking:'↗',Inbox:'✉',Reviews:'◐',Workforce:'♙',Accounts:'₹',Masters:'▦',Reports:'⌁'};
const descriptions={Dashboard:'The shift, the order book and the floor at one glance.',Orders:'Create buyer orders, check yarn feasibility and follow each stage.',Production:'Assign job cards, update machines, record shift output and create QR rolls.',Inventory:'Receive yarn, issue material to production and act on shortfalls.',Quality:'Inspect rolls, capture defects and keep the rework loop moving.',Traceability:'Scan a roll and follow its order, machine, shift, operator and material history.',Dispatch:'Open a truck, scan passed rolls onto it, then close it to raise the invoice.',Tracking:'Buyer-facing order progress, share links and enquiry view.',Inbox:'Buyer messages, routed by Jev, with reply drafts from live order data.',Reviews:'AI decisions that were not confident enough. Accept or correct them.',Workforce:'Shift attendance and monthly wages.',Accounts:'Order costs, supplier bills, buyer receivables and monthly P&L.',Masters:'Buyers, suppliers, yarns, fabrics, recipes, machines, workers and shifts.',Reports:'Machine output, wages and month-end figures.'};
const tamil={Dashboard:'கண்ணோட்டம்',Orders:'ஆர்டர்கள்',Production:'உற்பத்தி',Inventory:'சரக்கு',Quality:'தரம்',Traceability:'தடமறிதல்',Dispatch:'அனுப்புதல்',Tracking:'ஆர்டர் கண்காணிப்பு',Inbox:'செய்திகள்',Reviews:'AI சரிபார்ப்பு',Workforce:'பணியாளர்கள்',Accounts:'கணக்குகள்',Masters:'அடிப்படை தரவு',Reports:'அறிக்கைகள்'};
const STAGES=['Pending','Spinning','Weaving','Dyeing','In QC','Ready','Dispatched'];
const ENUM={dept:['Office','QC','Production','Dispatch','Accounts'],intent:['status','quality_complaint','payment','new_order','delivery_change','other'],urgency:['low','med','high'],category:['mechanical','electrical','yarn_quality','operator'],severity:['low','med','high'],defect_type:['hole','colour_mismatch','uneven_weave','stain','other'],stage_at_fault:['spinning','weaving','dyeing']};
const REVIEW_FIELDS={message:['dept','intent','urgency'],breakdown:['category','severity'],defect:['defect_type','stage_at_fault','severity']};
const label=s=>String(s??'').replaceAll('_',' ');

let role='Owner',page='Dashboard',me=null,lang='en',currentOrder='',currentToken='',traceRoll='',pendingRoll='',editMasterId='',masterKey='buyers',reportMonth=thisMonth(),flash='',afterRender=null,renderSeq=0,toastTimer=null;
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
const toast=text=>{const t=$('#toast');t.textContent=text;t.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.classList.remove('show'),3200)};
function field(lbl,name,placeholder='',type='text',value='',required=true){const extra=type==='number'?' step="any" min="0"':'';return`<label class="field"><span>${esc(lbl)}</span><input name="${esc(name)}" type="${type}" placeholder="${esc(placeholder)}" value="${esc(value)}"${extra} ${required?'required':''}></label>`}
function select(lbl,name,items,selected='',required=true){return`<label class="field"><span>${esc(lbl)}</span><select name="${esc(name)}" ${required?'required':''}>${items.map(([v,t])=>`<option value="${esc(v)}" ${String(v)===String(selected)?'selected':''}>${esc(t)}</option>`).join('')}</select></label>`}
function textarea(lbl,name,placeholder='',required=false){return`<label class="field"><span>${esc(lbl)}</span><textarea name="${esc(name)}" placeholder="${esc(placeholder)}" ${required?'required':''}></textarea></label>`}
function head(title,kicker,actions=''){return`<div class="page-head"><div><div class="eyebrow"><i></i> ${esc(kicker)}</div><h1>${esc(title)}</h1><p>${esc(descriptions[page]||'')}</p></div><div class="head-actions">${actions}</div></div>`}
function panel(title,body,tag=''){return`<section class="panel"><div class="panel-head"><h2>${title}</h2>${tag?`<span class="mono">${tag}</span>`:''}</div>${body}</section>`}
function kpi(lbl,value,note='',tone=''){return`<article class="panel kpi"><label>${lbl}</label><strong>${value}</strong><small class="${tone}">${note}</small></article>`}
function table(headers,rows,empty='Nothing here yet.'){if(!rows.length)return`<div class="empty">${empty}</div>`;return`<div class="table-wrap"><table class="table"><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`}
function badge(text,tone=''){const t=label(text);let c=t.match(/Break|Reject|Failed|Blocked|Overdue|high|short/i)?'bad':t.match(/Idle|Rework|Hold|Short|Risk|Maintenance|Pending|review|In QC|med|open/i)?'warn':t.match(/Pass|Running|Ready|Dispatched|Available|Can start|In range|routed|replied|closed|low/i)?'':'neutral';return`<span class="badge ${tone||c}">${esc(t)}</span>`}
function formStart(kind,title,fields,submit='SAVE ENTRY',extra='',hidden=''){return`<form class="panel form-section" data-form="${kind}">${hidden}<div class="panel-head"><h2>${title}</h2><span class="mono">${extra}</span></div><div class="form-grid">${fields}</div><div class="form-actions"><button class="primary">${submit}</button></div></form>`}
function loomCanvas(){return`<div class="loom-canvas-wrap"><canvas id="loomModel" aria-label="Interactive 3D view of a textile loom. Drag to rotate."></canvas></div><div class="loom-caption"><span>LOOM 04 · WEAVING</span><span>DRAG TO ROTATE</span><span>● RUNNING</span></div>`}
const isLate=o=>o.deadline<today()&&!['Ready','Dispatched'].includes(o.stage);
const monthForm=()=>`<form data-form="month" class="inline-actions"><input type="month" name="month" value="${esc(reportMonth)}" aria-label="Month"><button class="secondary">SHOW MONTH</button></form>`;
function aiLine(r,what){if(!r)return'';if(!r.confidence)return'The AI is unavailable, so this went to <b>AI review</b> for a person to decide.';const s=Object.entries(r.suggestion||{}).map(([k,v])=>`${label(k)}: <b>${esc(label(v))}</b>`).join(' · ');return r.auto?`Jev ${what}: ${s} <span class="mono">(${pct(r.confidence)})</span>`:`Jev was not sure (${pct(r.confidence)}), so this went to <b>AI review</b>. Suggestion: ${s||'none'}`}
function lowStockLine(list){return list?.length?`<b>LOW STOCK</b>: ${list.map(y=>`${esc(yarnName(y))} (${num(y.stock_kg)} kg, reorder at ${num(y.reorder_kg)})`).join(', ')}`:''}

function navRender(){const nav=$('#sideNav');const groups=role==='Buyer'?[['BUYER PORTAL',['Tracking']]]:navGroups;nav.innerHTML=groups.map(([g,items])=>{const vis=items.filter(x=>allowed[role].includes(x));return vis.length?`<div class="nav-label">${g}</div>`+vis.map(n=>`<button class="nav-item ${page===n?'active':''}" data-page="${n}"><span class="nav-icon">${icons[n]}</span>${lang==='ta'?tamil[n]:n}</button>`).join(''):''}).join('');$('#roleName').textContent=(me?.username?me.username+' · ':'')+role.toUpperCase();$('#pageCrumb').textContent=page.toUpperCase()}

// ---------- Pages ----------
async function dashboard(){
 const [floor,orders,stock,queue]=await Promise.all([api('/floor'),api('/orders'),api('/stock'),api('/qc/queue')]);
 const m=floor.machines,running=m.filter(x=>x.status==='Running').length,out=m.reduce((s,x)=>s+x.today_output,0),low=stock.filter(y=>y.low).length;
 const cards=m.map(x=>`<article class="machine-card"><div class="machine-top"><strong>${esc(x.name)}</strong><span class="status-dot ${x.status==='Breakdown'?'bad':x.idle?'warn':''}"></span></div><small>${esc(x.kind)} · ${x.order_id?'ORDER #'+x.order_id+' · '+x.order_progress_pct+'%':'NO JOB ASSIGNED'}</small><div style="margin-top:9px">${badge(x.status)}</div></article>`).join('');
 const outRows=m.filter(x=>x.today_output).map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.kind)}</td><td>${num(x.today_output)}</td></tr>`);
 return head('Shift overview','FACTORY / LIVE SHIFT',`<button class="secondary" data-page="Production">OPEN FLOOR ↗</button>`)+
  `<div class="grid four">${kpi('TODAY’S OUTPUT',num(out)+' m','PRODUCTION ENTRIES')}${kpi('MACHINES RUNNING',`${running} / ${m.length}`,`${floor.idle_count} NOT RUNNING · ${m.filter(x=>x.status==='Breakdown').length} BREAKDOWN`)}${kpi('ORDERS IN PROGRESS',orders.filter(o=>!['Pending','Ready','Dispatched'].includes(o.stage)).length,'LIVE ORDER BOOK')}${kpi('QC QUEUE',queue.length,`${low} LOW STOCK ALERTS`,low?'warn':'')}</div>`+
  `<div class="dashboard-grid">${panel('Output today by machine',table(['MACHINE','STAGE','OUTPUT'],outRows,'No production recorded today yet.'),'FROM SHIFT ENTRIES')}<section class="panel loom-panel"><div class="panel-head"><h2>Loom model</h2><span class="mono">3D · INTERACTIVE</span></div>${loomCanvas()}</section></div>`+
  `<section class="panel" style="margin-top:11px"><div class="panel-head"><h2>Machine status board</h2><span class="mono">LIVE FROM THE FLOOR</span></div><div class="machine-grid">${cards||'<div class="empty">Add machines in Masters.</div>'}</div></section>`+
  `<div class="dashboard-grid">${panel('Orders needing attention',table(['ORDER','BUYER','DEADLINE','STAGE','PROGRESS','RISK'],orders.filter(o=>o.stage!=='Dispatched').slice(0,6).map(o=>`<tr><td class="mono-cell">#${o.id}</td><td>${esc(o.buyer)}</td><td>${esc(o.deadline)}</td><td>${badge(o.stage)}</td><td>${o.progress_pct}%</td><td>${isLate(o)?badge('Overdue'):badge('On plan')}</td></tr>`),'No open orders.'),`<button class="inline-actions" data-page="Orders">VIEW ALL ↗</button>`)}${panel('Stock watch',table(['YARN','ON HAND','REORDER','STATUS'],stock.filter(y=>y.reorder_kg&&y.stock_kg<y.reorder_kg*1.35).map(y=>`<tr><td>${esc(yarnName(y))}</td><td>${num(y.stock_kg)} kg</td><td>${num(y.reorder_kg)} kg</td><td>${badge(y.low?'Low stock':'Watch')}</td></tr>`),'All yarns above watch level.'),'REORDER POINTS')}</div>`;
}
async function roleDashboard(){
 if(role==='Owner')return dashboard();
 if(role==='Office / Admin'){const [orders,review]=await Promise.all([api('/orders'),api('/messages?status=review')]);const risk=orders.filter(isLate).length;return head('Office overview','ORDERS / DELIVERY / BUYERS')+'<div class="grid four">'+kpi('OPEN ORDERS',orders.filter(o=>!['Ready','Dispatched'].includes(o.stage)).length,'CURRENT ORDER BOOK')+kpi('OVERDUE',risk,'NEEDS FOLLOW-UP',risk?'warn':'')+kpi('READY TO SHIP',orders.filter(o=>o.stage==='Ready').length,'PASSED QC')+kpi('MESSAGES TO CHECK',review.length,'AI WAS NOT SURE',review.length?'warn':'')+'</div>'+panel('Office focus','<p class="help">Create orders, answer buyer messages, and prepare dispatch paperwork.</p><div class="head-actions"><button class="secondary" data-page="Orders">ORDERS ↗</button><button class="secondary" data-page="Inbox">INBOX ↗</button><button class="secondary" data-page="Dispatch">DISPATCH ↗</button></div>')}
 if(role==='Store Keeper'){const [stock,grns]=await Promise.all([api('/stock'),api('/grn')]);const low=stock.filter(y=>y.low).length;return head('Store overview','YARN / RECEIPTS / ISSUES')+'<div class="grid three">'+kpi('YARN TYPES',stock.length,'ACTIVE MATERIALS')+kpi('LOW STOCK',low,'REORDER ALERTS',low?'warn':'')+kpi('RECEIVED TODAY',num(grns.filter(g=>g.received_on===today()).reduce((s,g)=>s+g.qty_kg,0))+' kg','GOODS RECEIVED')+'</div>'+panel('Store focus','<p class="help">Record goods receipts, issue yarn against an order, watch reorder levels, and load passed rolls onto trucks.</p><div class="head-actions"><button class="secondary" data-page="Inventory">INVENTORY ↗</button><button class="secondary" data-page="Dispatch">DISPATCH ↗</button></div>')}
 if(role==='Floor Supervisor'){const f=await api('/floor');return head('Floor overview','MACHINES / JOBS / SHIFT OUTPUT')+'<div class="grid three">'+kpi('RUNNING',f.machines.filter(m=>m.status==='Running').length,'OF '+f.machines.length+' MACHINES')+kpi('NOT RUNNING',f.idle_count,f.machines.filter(m=>m.status==='Breakdown').length+' BREAKDOWN')+kpi('TODAY’S OUTPUT',num(f.machines.reduce((s,m)=>s+m.today_output,0))+' m','SHIFT ENTRIES')+'</div>'+panel('Floor focus','<p class="help">Assign machine jobs, update status with one tap, record shift output and create QR-linked rolls.</p><div class="head-actions"><button class="secondary" data-page="Production">OPEN PRODUCTION ↗</button></div>')}
 if(role==='QC Inspector'){const [queue,reviews]=await Promise.all([api('/qc/queue'),api('/reviews')]);return head('Quality overview','ROLL QUEUE / INSPECTION / TRACEABILITY')+'<div class="grid three">'+kpi('WAITING FOR QC',queue.filter(r=>r.status==='pending_qc').length,'ROLLS TO INSPECT')+kpi('REWORK LOOP',queue.filter(r=>r.status==='rework').length,'RETURNED TO QC')+kpi('DEFECTS TO CONFIRM',reviews.filter(r=>r.kind==='defect').length,'AI REVIEW')+'</div>'+panel('Quality focus','<p class="help">Inspect rolls, record defects, and scan QR labels to see machine, shift and operator.</p><div class="head-actions"><button class="secondary" data-page="Quality">QUALITY CONTROL ↗</button><button class="secondary" data-page="Traceability">TRACE A ROLL ↗</button></div>')}
 return dashboard();
}

async function orderPage(){
 const orders=await api('/orders');
 const rows=orders.map(o=>`<tr><td class="mono-cell">#${o.id}<span class="sub">${esc(o.colour)} · ${num(o.qty_m)} m</span></td><td>${esc(o.buyer)}</td><td>${esc(o.fabric)}<span class="sub">${money(o.price_per_m)} / m</span></td><td>${esc(o.deadline)}</td><td>${badge(o.stage)}</td><td>${o.progress_pct}%<span class="sub">${num(o.passed_m)} m passed</span></td><td>${isLate(o)?badge('Overdue'):badge('On plan')}</td><td><div class="inline-actions"><button data-act="order-link" data-token="${esc(o.token)}">TRACK LINK</button><button data-act="feasibility" data-id="${o.id}">YARN</button><button data-act="risk" data-id="${o.id}">RISK</button></div></td></tr>`);
 const can=['Owner','Office / Admin'].includes(role);
 return head('Buyer orders','ORDER MANAGEMENT')+
  (can?formStart('order','Create a buyer order',select('BUYER','buyer_id',choices(M.buyers))+select('FABRIC','fabric_type_id',choices(M.fabric_types))+field('COLOUR','colour','e.g. Indigo')+field('QUANTITY (M)','qty_m','','number')+field('DEADLINE','deadline','','date',today())+field('PRICE PER METRE (₹)','price_per_m','','number'),'CREATE ORDER · CHECK YARN'):'')+
  panel('Feasibility & risk','<div id="feasibilityResult" class="notice">Choose YARN on an order to compare its bill of materials with stock, or RISK for Jev’s deadline estimate.</div>','BILL OF MATERIALS / JEV')+
  panel('Order book',table(['ORDER / QTY','BUYER','FABRIC / PRICE','DEADLINE','STAGE','PROGRESS','DEADLINE','ACTIONS'],rows,'No orders yet.'),'STAGE ROLLS UP FROM JOBS, ROLLS, QC AND DISPATCH');
}

async function inventoryPage(){
 const [stock,grns,issues,orders]=await Promise.all([api('/stock'),api('/grn'),api('/issues'),api('/orders')]);
 const yarnChoices=choices(M.yarn_types,yarnName),openOrders=orderChoices(orders.filter(o=>o.stage!=='Dispatched'));
 const stockRows=stock.map(y=>`<tr><td>${esc(y.count)} ${esc(y.material)}</td><td>${esc(y.colour)}</td><td>${num(y.stock_kg)} kg</td><td>${num(y.reorder_kg)} kg</td><td>${badge(y.low?'Low stock':y.reorder_kg&&y.stock_kg<y.reorder_kg*1.3?'Watch':'In range')}</td><td>${y.avg_rate==null?'—':money(y.avg_rate)+'/kg'}</td></tr>`);
 const grnRows=grns.map(g=>`<tr><td class="mono-cell">GRN-${g.id}</td><td>${esc(g.supplier)}</td><td>${esc(yarnName(byId(M.yarn_types,g.yarn_type_id)))}<span class="sub">CHALLAN ${esc(g.challan_no||'—')}</span></td><td>${num(g.qty_kg)} kg</td><td>${money(g.rate)}/kg</td><td>${esc(g.received_on)}</td></tr>`);
 const issueRows=issues.map(i=>`<tr><td>${esc(i.issued_on)}</td><td class="mono-cell">#${i.order_id}</td><td>${esc(yarnName(byId(M.yarn_types,i.yarn_type_id)))}</td><td>${esc(nameOf(M.machines,i.machine_id))}</td><td>${num(i.qty_kg)} kg</td></tr>`);
 const grnFields=select('SUPPLIER','supplier_id',choices(M.suppliers))+select('YARN TYPE','yarn_type_id',yarnChoices)+field('QUANTITY (KG)','qty_kg','','number')+field('RATE / KG (₹)','rate','','number')+field('CHALLAN NUMBER','challan_no','Supplier challan','text','',false)+select('LINK ORDER (OPTIONAL)','order_id',optional(openOrders,'No linked order'),'',false)+`<label class="field"><span>CHALLAN PHOTO → FILL THE FORM</span><input type="file" name="challan_photo" accept="image/*" data-challan></label>`;
 const issueFields=select('YARN TYPE','yarn_type_id',yarnChoices)+select('ORDER','order_id',openOrders)+select('MACHINE (OPTIONAL)','machine_id',optional(choices(M.machines)),'',false)+field('QUANTITY (KG)','qty_kg','','number');
 return head('Yarn at the gate','RAW MATERIAL INVENTORY')+'<div class="grid three">'+kpi('YARN TYPES',stock.length,'MASTER RECORDS')+kpi('GRNS',grns.length,'RECEIPTS LOGGED')+kpi('LOW STOCK',stock.filter(y=>y.low).length,'REORDER ALERTS')+'</div><div class="split" style="margin-top:11px">'+formStart('grn','Goods receipt note',grnFields,'RECORD GRN')+formStart('issue','Issue yarn to production',issueFields,'ISSUE MATERIAL')+'</div>'+panel('Live yarn stock',table(['YARN','COLOUR','AVAILABLE','REORDER LEVEL','ALERT','AVG RATE'],stockRows,'Add yarn types in Masters.'),'GRN − ISSUES = ON HAND')+'<div class="split">'+panel('Material issues',table(['DATE','ORDER','YARN','MACHINE','QTY'],issueRows,'No yarn issued yet.'),'STOCK DEDUCTIONS')+panel('Goods receipt history',table(['GRN','SUPPLIER','YARN / CHALLAN','QUANTITY','RATE','DATE'],grnRows,'No receipts yet.'),'RECEIVING LOG')+'</div>';
}

async function productionPage(){
 const [floor,orders,entries]=await Promise.all([api('/floor'),api('/orders'),api('/production')]);
 const openOrders=orderChoices(orders.filter(o=>!['Ready','Dispatched'].includes(o.stage)));
 const cards=floor.machines.map(m=>`<article class="machine-card"><div class="machine-top"><strong>${esc(m.name)}</strong><span class="status-dot ${m.status==='Breakdown'?'bad':m.idle?'warn':''}"></span></div><small>${esc(m.kind)} · ${m.order_id?'ORDER #'+m.order_id+' · '+m.order_progress_pct+'%':'NO ORDER'} · TODAY ${num(m.today_output)}</small><label class="field" style="margin-top:10px"><span>ONE-TAP STATUS</span><select data-machine-status="${m.id}">${['Running','Idle','Breakdown','Maintenance'].map(s=>`<option ${s===m.status?'selected':''}>${s}</option>`).join('')}</select></label><label class="field" style="margin-top:6px"><span>ASSIGN ORDER</span><select data-machine-order="${m.id}"><option value="">${m.order_id?'#'+m.order_id+' (current)':'Choose an order…'}</option>${openOrders.filter(([id])=>id!==m.order_id).map(([v,t])=>`<option value="${v}">${esc(t)}</option>`).join('')}</select></label></article>`).join('');
 const people=select('MACHINE','machine_id',choices(M.machines,m=>`${m.name} · ${m.kind}`))+select('ORDER','order_id',openOrders)+select('OPERATOR','operator_id',choices(M.workers))+select('SHIFT','shift_id',choices(M.shifts));
 const outputFields=people+field('OUTPUT (M, OR KG ON SPINNING)','output','','number')+field('MACHINE HOURS','hours','','number','8',false)+`<label class="field"><span>ALSO CREATE A ROLL WITH THIS OUTPUT</span><input type="checkbox" name="make_roll"></label>`;
 const shiftRows=entries.map(p=>`<tr><td>${esc(p.entry_date)}</td><td>${esc(p.machine)}</td><td class="mono-cell">#${p.order_id}</td><td>${esc(p.operator)}</td><td>${esc(p.shift)}</td><td>${num(p.output)}</td><td>${num(p.hours)}</td></tr>`);
 return head('Production floor','MACHINES / JOB CARDS')+'<div class="grid three">'+kpi('RUNNING',floor.machines.filter(m=>m.status==='Running').length,'MACHINES ON JOB')+kpi('NOT RUNNING',floor.idle_count,'IDLE / DOWN / MAINTENANCE')+kpi('TODAY’S OUTPUT',num(floor.machines.reduce((s,m)=>s+m.today_output,0)),'SHIFT ENTRIES')+'</div>'+panel('Machine status board',`<div class="machine-grid">${cards||'<div class="empty">Add machines in Masters.</div>'}</div>`,'A BREAKDOWN NOTE GOES TO JEV FOR TRIAGE')+'<div class="split">'+formStart('job','Assign order to machine (job card)',select('ORDER','order_id',openOrders)+select('MACHINE','machine_id',choices(M.machines,m=>`${m.name} · ${m.kind}`)),'ASSIGN JOB CARD','STAGE = MACHINE KIND')+formStart('breakdown','Breakdown log',select('MACHINE','machine_id',choices(M.machines))+textarea('WHAT HAPPENED?','note','e.g. yarn keeps snapping on the warp',true),'LOG & TRIAGE','JEV · CATEGORY / SEVERITY')+'</div>'+formStart('production','Production entry · per shift',outputFields,'RECORD SHIFT OUTPUT')+panel('Shift entries',table(['DATE','MACHINE','ORDER','OPERATOR','SHIFT','OUTPUT','HOURS'],shiftRows,'Production output will appear here.'),'LATEST 200');
}

async function qualityPage(){
 const [queue,fg]=await Promise.all([api('/qc/queue'),api('/finished-goods')]);
 const rows=queue.map(r=>`<tr><td class="mono-cell">${esc(r.qr)}</td><td>#${r.order_id}</td><td>${esc(r.machine)}</td><td>${num(r.metres)} m</td><td>${badge(r.status)}</td><td><div class="inline-actions"><button data-act="scan-roll" data-qr="${esc(r.qr)}">INSPECT</button><button data-act="trace" data-qr="${esc(r.qr)}">TRACE</button></div></td></tr>`);
 const fields=field('ROLL QR','qr','Scan or type e.g. ETM-…')+select('RESULT','result',[['pass','Pass'],['rework','Rework'],['reject','Reject']])+select('DEFECT TYPE','defect_type',optional(ENUM.defect_type.map(d=>[d,label(d)]),'Let Jev decide from the notes'),'',false)+textarea('NOTES','notes','What did you see? Needed for rework / reject.');
 return head('Quality control','ROLL INSPECTION / TRACEABILITY')+'<div class="grid three">'+kpi('WAITING FOR QC',queue.filter(r=>r.status==='pending_qc').length,'NEW ROLLS')+kpi('REWORK LOOP',queue.filter(r=>r.status==='rework').length,'BACK FOR RE-INSPECTION')+kpi('PASSED IN STORE',fg.reduce((s,x)=>s+x.rolls,0),num(fg.reduce((s,x)=>s+x.metres,0))+' M READY TO SHIP')+'</div><div class="split" style="margin-top:11px">'+formStart('qc','Scan roll · record inspection',fields,'SAVE QC RESULT','NOTES → JEV CLASSIFIES THE DEFECT')+panel('QC queue',table(['ROLL','ORDER','MACHINE','QTY','STATUS','ACTION'],rows,'No rolls waiting for inspection.')+'<div class="notice">Rework returns to this queue. Rejected rolls are blocked from dispatch.</div>','SCAN → RESULT → TRACE')+'</div>';
}

async function tracePage(){
 let content=`<form class="panel form-grid two" data-form="trace"><label class="field"><span>ROLL QR / SCAN</span><input name="qr" value="${esc(traceRoll)}" placeholder="ETM-…" autofocus></label><div class="form-actions" style="align-items:end"><button class="primary">TRACE ROLL</button></div></form>`;
 if(!traceRoll)return head('Roll traceability','QR SCAN / MATERIAL HISTORY')+content+panel('Trace any roll','<p class="help">Scan its QR code or type the roll code to see its order, yarn issued, machine, shift, operator, QC history and dispatch.</p>');
 let r;try{r=await api('/rolls/'+encodeURIComponent(traceRoll.trim()))}catch(e){return head('Roll traceability','QR SCAN / MATERIAL HISTORY')+content+`<div class="alert">${esc(e.message==='not found'?'No roll found with that code.':e.message)}</div>`}
 const yarn=r.yarn_issued.map(i=>`${esc(i.count)} ${esc(i.material)} · ${esc(i.colour)} · ${num(i.qty_kg)} kg · ${esc(i.issued_on)}`).join('<br>')||'No yarn issued to this order yet.';
 const qrows=r.qc_history.map(q=>`<tr><td>${esc(q.at)}</td><td>${badge(q.result)}</td><td>${esc(label(q.defect_type||'—'))}</td><td>${esc(q.severity||'—')}</td><td>${esc(q.stage_at_fault||'—')}</td><td>${esc(q.inspector||'—')}</td><td>${esc(q.notes||'')}</td></tr>`);
 content+=`<section class="panel" style="margin-top:12px"><div class="panel-head"><h2>${esc(r.qr)}</h2><div class="inline-actions">${badge(r.status)}<button data-act="roll-qr" data-qr="${esc(r.qr)}" data-order="${r.order_id}" data-metres="${r.metres}" data-status="${esc(r.status)}">QR LABEL</button></div></div><div class="grid four">${kpi('ORDER','#'+r.order_id,esc(r.buyer))}${kpi('FABRIC',esc(r.order.fabric),esc(r.order.colour))}${kpi('ROLL QTY',num(r.metres)+' m','MADE '+esc(r.made_on))}${kpi('MACHINE / SHIFT',esc(r.machine),esc(r.shift)+' · '+esc(r.operator))}</div><div class="split" style="margin-top:12px">${panel('Yarn issued to the order',`<p class="help">${yarn}</p><p class="help">Dispatch: ${r.dispatch?`Truck ${esc(r.dispatch.vehicle_no)} · ${esc(r.dispatch.driver)} · ${esc(r.dispatch.status)}`:'Not dispatched.'}</p>`)}${panel('QC history',table(['TIME','RESULT','DEFECT','SEVERITY','STAGE AT FAULT','INSPECTOR','NOTES'],qrows,'Not inspected yet.'))}</div></section>`;
 return head('Roll traceability','QR SCAN / MATERIAL HISTORY')+content;
}

async function dispatchPage(){
 const [orders,fg,ds]=await Promise.all([api('/orders'),api('/finished-goods'),api('/dispatches')]);
 const open=await Promise.all(ds.filter(d=>d.status==='open').map(d=>api('/dispatches/'+d.id)));
 const ready=orderChoices(orders.filter(o=>o.stage!=='Dispatched'&&fg.some(f=>f.order_id===o.id)));
 const trucks=open.map(d=>`<section class="panel" style="margin-top:11px"><div class="panel-head"><h2>Truck ${esc(d.vehicle_no)} · ${esc(d.driver)}</h2><span class="mono">DISPATCH ${d.id} · ORDER #${d.order_id} · ${esc(d.buyer)}</span></div><form data-form="scan" class="form-grid two"><input type="hidden" name="did" value="${d.id}"><label class="field"><span>SCAN ROLL QR ONTO THIS TRUCK</span><input name="qr" placeholder="ETM-…" required autofocus></label><div class="form-actions" style="align-items:end"><button class="primary">ADD ROLL</button><button type="button" class="secondary" data-act="close-dispatch" data-id="${d.id}">CLOSE TRUCK & INVOICE</button></div></form>${table(['ROLL','METRES'],d.packing_list.map(p=>`<tr><td class="mono-cell">${esc(p.qr)}</td><td>${num(p.metres)} m</td></tr>`),'No rolls scanned yet.')}<p class="help">${d.rolls} rolls · ${num(d.metres)} m on this truck.</p></section>`).join('');
 const fgRows=fg.map(f=>`<tr><td class="mono-cell">#${f.order_id}</td><td>${esc(f.buyer)}</td><td>${esc(f.fabric)} · ${esc(f.colour)}</td><td>${f.rolls}</td><td>${num(f.metres)} m</td></tr>`);
 const hist=ds.map(d=>`<tr><td class="mono-cell">${d.id}</td><td>${esc(d.created_at)}</td><td>#${d.order_id} · ${esc(d.buyer)}</td><td>${esc(d.vehicle_no)} · ${esc(d.driver)}</td><td>${badge(d.status)}</td><td>${d.total==null?'—':money(d.total)}</td><td><div class="inline-actions"><button data-act="print-dispatch" data-id="${d.id}">PACKING LIST</button>${d.invoice_id?`<button data-act="print-invoice" data-id="${d.id}">INVOICE</button>`:''}</div></td></tr>`);
 return head('Finished goods & dispatch','PASSED ROLLS / SHIPMENTS')+'<div class="grid three">'+kpi('PASSED ROLLS IN STORE',fg.reduce((s,f)=>s+f.rolls,0),'READY TO SHIP')+kpi('TRUCKS LOADING',open.length,'OPEN DISPATCHES')+kpi('READY METRES',num(fg.reduce((s,f)=>s+f.metres,0))+' m','QC PASSED')+'</div>'+
  formStart('dispatch','Open a truck',select('ORDER','order_id',ready.length?ready:[['','No order has passed rolls yet']])+field('VEHICLE NUMBER','vehicle_no','TN 00 AA 0000')+field('DRIVER','driver','Name / phone'),'OPEN DISPATCH','THEN SCAN ROLLS · DUPLICATES & UNPASSED ROLLS ARE BLOCKED')+trucks+
  panel('Finished goods',table(['ORDER','BUYER','FABRIC','ROLLS','METRES'],fgRows,'No passed rolls in store.'),'QC-PASSED, NOT YET DISPATCHED')+panel('Dispatch history',table(['DISPATCH','OPENED','ORDER','VEHICLE / DRIVER','STATUS','INVOICE TOTAL','DOCUMENTS'],hist,'No dispatches yet.'));
}

function renderTracking(t,link=''){
 const idx=Math.max(0,STAGES.indexOf(t.stage));
 const disp=t.dispatches.map(d=>`${esc(d.created_at)} · ${esc(d.vehicle_no)} · ${d.rolls} rolls · ${num(d.metres)} m (${esc(d.status)})`).join(' / ')||'Not dispatched yet.';
 return`<section class="panel tracking-card"><div class="panel-head"><div><span class="mono">ORDER #${t.order_no}</span><h2 style="margin:8px 0">${esc(t.fabric)} · ${esc(t.colour)}</h2></div>${badge(t.stage)}</div><div class="tracking-timeline" style="grid-template-columns:repeat(${STAGES.length},1fr)">${STAGES.map((x,i)=>`<div class="timeline-step ${i<=idx?'done':''}">${x.toUpperCase()}</div>`).join('')}</div><div class="grid three">${kpi('PRODUCED',num(t.produced_m)+' m',`TARGET ${num(t.target_m)} m · ${t.progress_pct}%`)}${kpi('PASSED QC',num(t.passed_m)+' m',`${t.rolls_passed} OF ${t.rolls_produced} ROLLS`)}${kpi('EXPECTED READY',esc(t.expected_ready||'Not enough data yet'),'DEADLINE '+esc(t.deadline))}</div><p class="help">Dispatch: ${disp}</p>${link?`<div class="form-actions"><button class="secondary" data-act="order-link" data-token="${esc(link)}">COPY SHARE LINK</button></div>`:''}</section>`;
}
async function trackingPage(){
 if(role==='Buyer'){
  if(!currentToken)return head('Your order','ORDER TRACKING')+`<form data-form="tracking-token" class="panel form-grid two"><label class="field"><span>TRACKING LINK OR CODE</span><input name="token" placeholder="Paste the link the mill sent you" required></label><div class="form-actions" style="align-items:end"><button class="primary">VIEW ORDER</button></div></form>`;
  try{return head('Your order','ORDER TRACKING')+renderTracking(await api('/track/'+encodeURIComponent(currentToken)))}catch{currentToken='';return head('Your order','ORDER TRACKING')+'<div class="alert">That tracking link is not valid. Ask the mill for a new one.</div>'}
 }
 const orders=await api('/orders');
 let card='';if(currentOrder){const o=await api('/orders/'+currentOrder);card=renderTracking(o,o.token)}
 return head('Order tracking','SHAREABLE STATUS / BUYER ENQUIRY')+panel('Look up an order',`<form data-form="tracking-select" class="form-grid two">${select('ORDER','order',orderChoices(orders),currentOrder)}<div class="form-actions" style="align-items:end"><button class="primary">OPEN BUYER VIEW</button></div></form>`)+card+
  panel('Enquiries for the office',table(['ORDER','BUYER','STAGE','TARGET / PASSED','READY BY','DEADLINE','ACTION'],orders.map(x=>`<tr><td class="mono-cell">#${x.id}</td><td>${esc(x.buyer)}</td><td>${badge(x.stage)}</td><td>${num(x.passed_m)} / ${num(x.qty_m)} m</td><td>${esc(x.expected_ready||'—')}</td><td>${esc(x.deadline)}</td><td><div class="inline-actions"><button data-act="open-track" data-id="${x.id}">VIEW</button><button data-act="order-link" data-token="${esc(x.token)}">COPY LINK</button></div></td></tr>`),'No orders yet.'),'BUYER CALLS / QUICK ANSWERS');
}

async function inboxPage(){
 const [msgs,orders]=await Promise.all([api('/messages'),api('/orders')]);
 const fields=select('BUYER (OPTIONAL)','buyer_id',optional(choices(M.buyers)),'',false)+select('ORDER (OPTIONAL)','order_id',optional(orderChoices(orders)),'',false)+select('CHANNEL','channel',[['whatsapp','WhatsApp'],['email','Email'],['phone','Phone call']])+textarea('MESSAGE','text','Paste the buyer’s message',true);
 const rows=msgs.map(m=>`<tr><td>${esc(m.created_at)}<span class="sub">${esc(m.channel)}</span></td><td>${esc(nameOf(M.buyers,m.buyer_id))}${m.order_id?`<span class="sub">ORDER #${m.order_id}</span>`:''}</td><td>${esc(m.text)}${m.reply?`<span class="sub">REPLY: ${esc(m.reply)}</span>`:''}</td><td>${m.dept?`<b>${esc(m.dept)}</b><span class="sub">${esc(label(m.intent))} · ${esc(m.urgency)}</span>`:'—'}</td><td>${m.confidence==null?'—':pct(m.confidence)}</td><td>${badge(m.status)}</td><td><div class="inline-actions"><select data-lang-for="${m.id}" aria-label="Reply language"><option value="en">EN</option><option value="ta">TA</option><option value="hi">HI</option></select><button data-act="draft" data-id="${m.id}">DRAFT REPLY</button></div></td></tr>`);
 return head('Buyer inbox','JEV ROUTING / REPLY DRAFTS')+'<div class="grid three">'+kpi('MESSAGES',msgs.length,'ALL CHANNELS')+kpi('NEED A HUMAN',msgs.filter(m=>m.status==='review').length,'JEV WAS NOT SURE',msgs.some(m=>m.status==='review')?'warn':'')+kpi('REPLIED',msgs.filter(m=>m.status==='replied').length,'ANSWERED')+'</div>'+formStart('message','Log an incoming message',fields,'ROUTE WITH JEV','DEPARTMENT · INTENT · URGENCY')+panel('Messages',table(['RECEIVED','FROM','MESSAGE','ROUTED TO','CONFIDENCE','STATUS','REPLY'],rows,'No buyer messages yet.'),'REPLIES ARE RECORDED HERE; SEND THEM FROM WHATSAPP / EMAIL');
}

async function reviewsPage(){
 const items=await api('/reviews');
 const rows=items.map(r=>`<tr data-review="${r.id}"><td>${badge(r.kind)}<span class="sub">#${r.ref_id} · ${esc(r.created_at)}</span></td><td>${r.confidence?pct(r.confidence):'AI unavailable'}</td><td><div class="inline-actions">${REVIEW_FIELDS[r.kind].map(f=>`<label class="field"><span>${esc(label(f).toUpperCase())}</span><select data-field="${f}">${ENUM[f].map(v=>`<option value="${v}" ${r.suggestion[f]===v?'selected':''}>${esc(label(v))}</option>`).join('')}</select></label>`).join('')}</div></td><td><div class="inline-actions"><button data-act="review-save" data-id="${r.id}">SAVE</button></div></td></tr>`);
 return head('AI review queue','HUMAN IN THE LOOP')+panel('Waiting for a person',table(['ITEM','JEV CONFIDENCE','DECISION (JEV’S SUGGESTION PRESELECTED WHEN IT HAD ONE)','ACTION'],rows,'Nothing to review. Jev was confident about everything.'),'SAVE APPLIES THE VALUES SHOWN');
}

async function workforcePage(){
 const canWages=['Owner','Office / Admin'].includes(role),wages=canWages?await api('/wages?month='+reportMonth):null;
 const workers=M.workers.map(w=>`<label class="roll-card"><input type="checkbox" name="worker_ids" value="${w.id}"><strong>${esc(w.name)}</strong><small>${esc(w.wage_type)} · ${money(w.rate)}${w.wage_type==='daily'?'/day':'/unit'}</small></label>`).join('')||'<div class="empty">Add workers in Masters.</div>';
 const attendance=`<form class="panel form-section" data-form="attendance"><div class="panel-head"><h2>Record shift attendance</h2><span class="mono">TICK EVERYONE PRESENT</span></div><div class="form-grid">${select('SHIFT','shift_id',choices(M.shifts))+field('DATE','work_date','','date',today())}</div><div class="roll-cards" style="margin-top:12px">${workers}</div><div class="form-actions"><button class="primary">SAVE ATTENDANCE</button></div></form>`;
 const wageRows=wages?wages.workers.map(w=>`<tr><td>${esc(w.name)}</td><td>${esc(w.wage_type)} · ${money(w.rate)}</td><td>${w.days}</td><td>${num(w.output)}</td><td>${money(w.wage)}</td></tr>`):[];
 return head('People, shifts & wages','ATTENDANCE / PAYROLL',canWages?monthForm():'')+'<div class="grid three">'+kpi('WORKERS',M.workers.length,'ON THE ROSTER')+(wages?kpi('WAGES · '+esc(wages.month),money(wages.total),'ATTENDANCE + PIECE OUTPUT'):'')+'</div>'+attendance+(wages?panel('Monthly wages',table(['WORKER','RATE RULE','DAYS PRESENT','OUTPUT','WAGE'],wageRows,'No workers yet.'),'DAILY × DAYS, OR PIECE × OUTPUT'):'');
}

async function accountsPage(){
 const owner=role==='Owner';
 const [recv,pay,pnl,costs]=await Promise.all([api('/reports/receivables'),api('/reports/payables'),owner?api('/reports/pnl?month='+reportMonth):null,owner?api('/reports/order-cost'):null]);
 const outstanding=recv.reduce((s,x)=>s+x.outstanding,0),payable=pay.reduce((s,x)=>s+x.outstanding,0);
 const payFields=select('PARTY','party',[['buyer','Buyer'],['supplier','Supplier']])+select('BUYER / SUPPLIER','party_id',[...M.buyers.map(b=>['buyer:'+b.id,'Buyer · '+b.name]),...M.suppliers.map(s=>['supplier:'+s.id,'Supplier · '+s.name])])+select('INVOICE (BUYER PAYMENTS)','invoice_id',optional(recv.map(i=>[i.id,`INV-${String(i.id).padStart(5,'0')} · ${i.buyer} · ${money(i.outstanding)} due`]),'No invoice'),'',false)+field('AMOUNT (₹)','amount','','number')+field('DATE','paid_on','','date',today())+field('REFERENCE','ref','Bank / UTR','text','',false);
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
 machines:['Machines',[['name','NAME','text',1],['kind','KIND',['spinning','weaving','dyeing'],1],['overhead_per_hour','OVERHEAD ₹ / HOUR','number']]],
 workers:['Workers',[['name','NAME','text',1],['phone','PHONE'],['wage_type','WAGE TYPE',['daily','piece'],1],['rate','RATE ₹ (PER DAY, OR PER METRE)','number',1]]],
 shifts:['Shifts',[['name','NAME','text',1],['starts','STARTS','time'],['ends','ENDS','time']]]};
const refName=(type,v)=>type==='fabric_types'?nameOf(M.fabric_types,v):type==='yarn_types'?yarnName(byId(M.yarn_types,v)):v;
async function masterPage(){
 const [title,cols]=MASTERS[masterKey],data=M[masterKey],editing=byId(data,editMasterId),canEdit=['Owner','Office / Admin'].includes(role);
 const inputs=cols.map(([k,lbl,type='text',req])=>{const v=editing?.[k]??'';if(Array.isArray(type))return select(lbl,k,type.map(x=>[x,x]),v);if(type==='fabric_types')return select(lbl,k,choices(M.fabric_types),v);if(type==='yarn_types')return select(lbl,k,choices(M.yarn_types,yarnName),v);return field(lbl,k,'',type,v,!!req)}).join('');
 const tabs=Object.entries(MASTERS).map(([k,v])=>`<button class="tab-button master-tab ${k===masterKey?'active selected':''}" data-master="${k}">${v[0]}</button>`).join('');
 const rows=data.map(x=>`<tr><td class="mono-cell">${x.id}</td>${cols.map(([k,,type])=>`<td>${esc(refName(type,x[k]))}</td>`).join('')}${masterKey==='machines'?`<td>${badge(x.status)}</td>`:''}<td>${canEdit?`<button class="inline-actions" data-act="edit-master" data-id="${x.id}">EDIT</button>`:''}</td></tr>`);
 return head('Master data','FACTORY DIRECTORY / RECIPES')+`<div class="tabs">${tabs}</div><section class="panel"><div class="panel-head"><h2>${title}</h2><span class="mono">${editing?'EDITING #'+editing.id:'SHARED WITH EVERY SCREEN'}</span></div>`+(canEdit?`<form class="form-grid" data-form="master">${inputs}<div class="form-actions"><button class="primary">${editing?'SAVE CHANGES':'ADD RECORD'}</button>${editing?'<button type="button" class="secondary" data-act="cancel-master-edit">CANCEL</button>':''}</div></form>`:'')+table(['ID',...cols.map(c=>c[1]),...(masterKey==='machines'?['STATUS']:[]),''],rows,'Nothing added yet.')+'</section>';
}

async function reportsPage(){
 const [machines,wages,pnl,orders]=await Promise.all([api('/reports/machines?month='+reportMonth),api('/wages?month='+reportMonth),api('/reports/pnl?month='+reportMonth),api('/orders')]);
 const rows=machines.map(m=>`<tr><td>${esc(m.name)}</td><td>${esc(m.kind)}</td><td>${num(m.output)}</td><td>${num(m.hours)} h</td><td>${m.breakdowns}</td></tr>`);
 return head('Reports','OUTPUT / WAGES / MONTH-END',monthForm())+'<div class="grid four">'+kpi('OUTPUT · '+esc(reportMonth),num(machines.reduce((s,m)=>s+m.output,0)),'ALL MACHINES')+kpi('TOTAL WAGES',money(wages.total),'ATTENDANCE + PIECE')+kpi('PROFIT / LOSS',money(pnl.profit),'REVENUE '+money(pnl.revenue),pnl.profit<0?'warn':'')+kpi('ORDERS OVERDUE',orders.filter(isLate).length,'PAST DEADLINE, NOT READY')+'</div>'+panel('Output per machine',table(['MACHINE','KIND','OUTPUT','HOURS','BREAKDOWNS'],rows,'No machines yet.'),'FROM PRODUCTION ENTRIES')+'<div class="notice">EXPORT ↓ downloads the tables on screen as CSV. ⤓ prints or saves a PDF.</div>';
}

const pages={Dashboard:roleDashboard,Orders:orderPage,Production:productionPage,Inventory:inventoryPage,Quality:qualityPage,Traceability:tracePage,Dispatch:dispatchPage,Tracking:trackingPage,Inbox:inboxPage,Reviews:reviewsPage,Workforce:workforcePage,Accounts:accountsPage,Masters:masterPage,Reports:reportsPage};
async function render(){
 if(!allowed[role].includes(page))page=role==='Buyer'?'Tracking':'Dashboard';
 navRender();
 const seq=++renderSeq;let html;
 try{html=await pages[page]()}catch(e){html=head(page,'COULD NOT LOAD')+`<div class="alert">${esc(e.message)}</div>`}
 if(seq!==renderSeq)return;
 $('#view').innerHTML=(flash?`<div class="notice" style="margin-bottom:11px">${flash}</div>`:'')+html;flash='';
 $('#lastSaved').textContent='UPDATED '+new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
 if(page==='Dashboard')drawLoom();
 if(afterRender){const f=afterRender;afterRender=null;f()}
}
function setPage(p){if(!allowed[role].includes(p)){toast('Your role does not have access to '+p+'.');return}page=p;render();window.scrollTo({top:0,behavior:'smooth'})}

// ---------- Kept from the original prototype: landing animation and 3D loom ----------
function showOpening(){const opening=$('#opening'),tile=$('#videoTile'),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches,zoom=()=>{const r=tile.getBoundingClientRect(),o=opening.getBoundingClientRect(),x=r.left-o.left,y=r.top-o.top;tile.style.position='absolute';tile.style.left=x+'px';tile.style.top=y+'px';tile.style.width=r.width+'px';tile.style.height=r.height+'px';tile.style.transformOrigin='top left';opening.appendChild(tile);if(reduced){opening.classList.add('zoom');tile.style.left='0px';tile.style.top='0px';tile.style.width='100%';tile.style.height='100%';return}requestAnimationFrame(()=>{opening.classList.add('zoom');const sx=opening.clientWidth/r.width,sy=opening.clientHeight/r.height;tile.animate([{transform:'translate3d(0,0,0) scale(1)'},{transform:`translate3d(${-x}px,${-y}px,0) scale(${sx},${sy})`}],{duration:2200,easing:'cubic-bezier(.16,1,.3,1)',fill:'forwards'})})};if(reduced){zoom();return}setTimeout(zoom,4100)}
function drawLoom(){const c=$('#loomModel');if(!c)return;const ctx=c.getContext('2d'),wrap=c.parentElement,dpr=Math.min(devicePixelRatio||1,2);let w=wrap.clientWidth,h=wrap.clientHeight,drag=false,lastX=0,rotation=-.3,anim=0,raf;const resize=()=>{w=wrap.clientWidth;h=wrap.clientHeight;c.width=w*dpr;c.height=h*dpr;c.style.width=w+'px';c.style.height=h+'px';ctx.setTransform(dpr,0,0,dpr,0,0)};resize();const proj=(x,y,z)=>{let a=rotation,xx=x*Math.cos(a)-z*Math.sin(a),zz=x*Math.sin(a)+z*Math.cos(a),tilt=.58,yy=y*Math.cos(tilt)-zz*Math.sin(tilt),depth=y*Math.sin(tilt)+zz*Math.cos(tilt),scale=1/(1+depth*.0018);return[w/2+xx*scale,h*.47+yy*scale+depth*.17]};function line(a,b,color,width=2){let A=proj(...a),B=proj(...b);ctx.strokeStyle=color;ctx.lineWidth=width;ctx.beginPath();ctx.moveTo(...A);ctx.lineTo(...B);ctx.stroke()}function frame(){ctx.clearRect(0,0,w,h);ctx.lineCap='round';let L=-125,R=125,Y=-66,Z=-82,F=82;[[L,Y,Z],[R,Y,Z],[L,-Y,Z],[R,-Y,Z],[L,Y,F],[R,Y,F],[L,-Y,F],[R,-Y,F]].forEach(()=>{});const steel='#a9aaab';[[[L,Y,Z],[R,Y,Z]],[[L,-Y,Z],[R,-Y,Z]],[[L,Y,F],[R,Y,F]],[[L,-Y,F],[R,-Y,F]],[[L,Y,Z],[L,-Y,Z]],[[R,Y,Z],[R,-Y,Z]],[[L,Y,F],[L,-Y,F]],[[R,Y,F],[R,-Y,F]],[[L,Y,Z],[L,Y,F]],[[R,Y,Z],[R,Y,F]],[[L,-Y,Z],[L,-Y,F]],[[R,-Y,Z],[R,-Y,F]]].forEach(([a,b])=>line(a,b,steel,3));for(let i=0;i<19;i++){let x=L+10+i*13.1;line([x,0,-55],[x,0,56],i%2?'#d9cec1':'#d49a62',1.4)}for(let j=0;j<10;j++){let z=-48+j*11;line([L+8,-3,z],[R-8,-3,z],j%2?'#bda68f':'#777a7c',2)}const sx=L+20+((anim%2200)/2200)*(R-L-40);line([sx,-8,-74],[sx,-8,75],'#e4f8dc',4);line([L-10,0,-83],[R+10,0,-83],'#737578',7);line([L-10,0,83],[R+10,0,83],'#737578',7);ctx.fillStyle='#9da0a1';ctx.font='9px monospace';ctx.fillText('DRAG TO ROTATE · SHUTTLE IN MOTION',12,h-13);if(!drag)rotation+=.0018;anim+=16;raf=requestAnimationFrame(frame)}frame();wrap.onpointerdown=e=>{drag=true;lastX=e.clientX;wrap.setPointerCapture(e.pointerId)};wrap.onpointermove=e=>{if(drag){rotation+=(e.clientX-lastX)*.008;lastX=e.clientX}};wrap.onpointerup=()=>drag=false;wrap.onpointercancel=()=>drag=false;}

// ---------- Forms ----------
const formData=form=>Object.fromEntries(new FormData(form).entries());
const handlers={
 async order(d){const o=await post('/orders',{buyer_id:+d.buyer_id,fabric_type_id:+d.fabric_type_id,colour:d.colour,qty_m:+d.qty_m,deadline:d.deadline,price_per_m:+d.price_per_m});flash=`Order <b>#${o.id}</b> created · yarn check: <b>${esc(o.feasibility.verdict)}</b>`;return'Order saved.'},
 async grn(d){const r=await post('/grn',{supplier_id:+d.supplier_id,yarn_type_id:+d.yarn_type_id,qty_kg:+d.qty_kg,rate:+d.rate,order_id:intOrNull(d.order_id),challan_no:d.challan_no||null});flash=`GRN-${r.grn.id} recorded · ${esc(yarnName(r.stock))} now ${num(r.stock.stock_kg)} kg. `+lowStockLine(r.low_stock);return'Goods receipt recorded.'},
 async issue(d){const r=await post('/issues',{order_id:+d.order_id,yarn_type_id:+d.yarn_type_id,qty_kg:+d.qty_kg,machine_id:intOrNull(d.machine_id)});flash=lowStockLine(r.low_stock);return'Yarn issued.'},
 async job(d){await post('/jobs',{order_id:+d.order_id,machine_id:+d.machine_id});return'Job card assigned.'},
 async breakdown(d){const r=await post(`/machines/${d.machine_id}/status`,{status:'Breakdown',note:d.note});flash=`${esc(nameOf(M.machines,d.machine_id))} marked as breakdown. `+aiLine(r.triage,'triage')+(r.log.assignee?` · send: <b>${esc(r.log.assignee)}</b>`:'');return'Breakdown logged.'},
 async production(d,form){
  const body={shift_id:+d.shift_id,machine_id:+d.machine_id,order_id:+d.order_id,operator_id:+d.operator_id,output:+d.output};
  if(d.hours)body.hours=+d.hours;
  await post('/production',body);
  if(form.querySelector('[name=make_roll]')?.checked){const roll=await post('/rolls',{order_id:body.order_id,machine_id:body.machine_id,shift_id:body.shift_id,operator_id:body.operator_id,metres:body.output});afterRender=()=>qrModal(roll);return'Roll '+roll.qr+' created.'}
  return'Shift output recorded.'},
 async qc(d){const r=await post('/qc/'+encodeURIComponent(d.qr.trim()),{result:d.result,defect_type:d.defect_type||null,notes:d.notes||null});flash=`Roll ${esc(d.qr)}: <b>${esc(r.qc.result)}</b>. `+aiLine(r.classification,'classified it');return'QC result saved.'},
 async trace(d){traceRoll=d.qr.trim();return''},
 async 'tracking-token'(d){const t=d.token.includes('track=')?decodeURIComponent(d.token.split('track=')[1]):d.token.trim();currentToken=t;history.replaceState(null,'','#track='+encodeURIComponent(t));return''},
 async 'tracking-select'(d){currentOrder=d.order;return''},
 async dispatch(d){if(!d.order_id)throw Error('No order has QC-passed rolls to ship yet.');await post('/dispatches',{order_id:+d.order_id,vehicle_no:d.vehicle_no,driver:d.driver});return'Truck opened. Scan rolls onto it.'},
 async scan(d){try{await post(`/dispatches/${d.did}/scan`,{qr:d.qr.trim()})}catch(e){flash=`<b>${esc(e.message)}</b>`;await render();throw e}return'Roll '+d.qr.trim()+' loaded.'},
 async attendance(d,form){const ids=[...form.querySelectorAll('[name=worker_ids]:checked')].map(x=>+x.value);if(!ids.length)throw Error('Tick at least one worker.');await post('/attendance',{shift_id:+d.shift_id,work_date:d.work_date,worker_ids:ids});return`Attendance saved for ${ids.length} worker(s).`},
 async payment(d){const [party,pid]=d.party_id.split(':');if(party!==d.party)throw Error(`Pick a ${d.party} in "Buyer / Supplier".`);await post('/payments',{party,party_id:+pid,amount:+d.amount,invoice_id:party==='buyer'?intOrNull(d.invoice_id):null,paid_on:d.paid_on,ref:d.ref||null});return'Payment recorded.'},
 async master(d){
  const cols=MASTERS[masterKey][1],body={};
  for(const [k,,type='text']of cols){const v=d[k];if(v===''||v==null){if(editMasterId)body[k]=null;continue}body[k]=type==='number'||type==='fabric_types'||type==='yarn_types'?Number(v):v}
  if(editMasterId)await put(`/master/${masterKey}/${editMasterId}`,body);else await post('/master/'+masterKey,body);
  editMasterId='';await loadMasters();return'Saved.'},
 async message(d){const r=await post('/messages',{text:d.text,channel:d.channel,buyer_id:intOrNull(d.buyer_id),order_id:intOrNull(d.order_id)});flash=r.message.status==='routed'?`Routed to <b>${esc(r.message.dept)}</b> · ${esc(label(r.message.intent))} · urgency ${esc(r.message.urgency)} <span class="mono">(${pct(r.routing.confidence)})</span>`:aiLine(r.routing,'routing');return'Message logged.'},
 async month(d){reportMonth=d.month||thisMonth();return''},
};
async function submit(form,e){
 e.preventDefault();const type=form.dataset.form,btn=form.querySelector('button.primary');if(btn)btn.disabled=true;
 try{const msg=await handlers[type](formData(form),form);await render();if(msg)toast(msg)}
 catch(err){toast(err.message||'Could not save this entry.')}
 finally{if(btn)btn.disabled=false}
}

// ---------- Buttons ----------
function qrModal(roll){$('#modalContent').innerHTML=`<div class="eyebrow"><i></i> ROLL IDENTIFICATION</div><h2>${esc(roll.qr)}</h2><p class="help">Scan to open its traceability record. You can also print the roll card.</p><div class="qr-box" id="qrCode"></div><p class="mono">ORDER #${esc(roll.order_id)} · ${num(roll.metres)} M · ${esc(label(roll.status))}</p><button type="button" class="secondary" onclick="window.print()">PRINT QR LABEL</button>`;$('#modal').showModal();if(window.QRCode)new QRCode($('#qrCode'),{text:location.origin+location.pathname+'#roll='+encodeURIComponent(roll.qr),width:120,height:120,correctLevel:QRCode.CorrectLevel.M});else $('#qrCode').innerHTML=`<span class="qr-fallback">QR library offline<br>${esc(roll.qr)}</span>`}
function copyLink(t){const url=location.origin+location.pathname+'#track='+encodeURIComponent(t);navigator.clipboard?.writeText(url).then(()=>toast('Buyer tracking link copied.')).catch(()=>toast(url))}
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
  if(act==='cancel-master-edit'){editMasterId='';return render()}
  if(act==='order-link')return copyLink(el.dataset.token);
  if(act==='open-track'){currentOrder=id;return render()}
  if(act==='feasibility'){const f=await api(`/orders/${id}/feasibility`);$('#feasibilityResult').innerHTML=`<b>ORDER #${id}: ${esc(f.verdict.toUpperCase())}</b><br>`+f.lines.map(l=>`${esc(l.count)} ${esc(l.material)} · ${esc(l.colour)}: need ${num(l.required_kg)} kg · issued ${num(l.issued_kg)} · in stock ${num(l.available_kg)}${l.short_kg?` · <b class="warn">SHORT ${num(l.short_kg)} kg</b>`:''}`).join('<br>');return}
  if(act==='risk'){$('#feasibilityResult').textContent='Asking Jev…';const r=await api(`/orders/${id}/risk`);$('#feasibilityResult').innerHTML=r.probability==null?`Order #${id}: risk unknown (AI unavailable). ${r.progress_pct}% done, ${r.days_left} days left, yarn short ${num(r.yarn_short_kg)} kg.`:`<b>ORDER #${id}: ${r.at_risk?'AT RISK':'ON TRACK'}</b> · Jev puts the chance of missing the deadline at <b>${pct(r.probability)}</b>.<br>${r.progress_pct}% done · ${r.days_left} days left · yarn short ${num(r.yarn_short_kg)} kg · ${r.free_machines} idle machines`;return}
  if(act==='scan-roll'){const f=$('form[data-form=qc] [name=qr]');if(f){f.value=el.dataset.qr;f.focus()}return}
  if(act==='trace'){traceRoll=el.dataset.qr;return setPage('Traceability')}
  if(act==='roll-qr')return qrModal({qr:el.dataset.qr,order_id:el.dataset.order,metres:el.dataset.metres,status:el.dataset.status});
  if(act==='close-dispatch'){if(!confirm('Close this truck and raise the invoice? No more rolls can be added.'))return;const d=await post(`/dispatches/${id}/close`);flash=`Dispatch ${d.id} closed · invoice INV-${String(d.invoice.id).padStart(5,'0')} for <b>${money(d.invoice.total)}</b> (${d.rolls} rolls, ${num(d.metres)} m).`;await render();return dispatchModal(id,'invoice')}
  if(act==='print-invoice')return dispatchModal(id,'invoice');
  if(act==='print-dispatch')return dispatchModal(id,'packing');
  if(act==='draft'){const l=$(`[data-lang-for="${id}"]`)?.value||'en';toast('Drafting reply from live order data…');const r=await post(`/messages/${id}/draft`,{lang:l});$('#modalContent').innerHTML=`<div class="eyebrow"><i></i> REPLY DRAFT · ${esc(l.toUpperCase())}</div><h2>Check, edit, then send</h2><label class="field"><span>REPLY</span><textarea id="replyText" rows="6">${esc(r.draft)}</textarea></label><p class="help">Copy it into WhatsApp or email, then mark it as sent so the inbox knows it was answered.</p><div class="form-actions"><button type="button" class="secondary" data-act="copy-reply">COPY</button><button type="button" class="primary" data-act="send-reply" data-id="${id}">MARK AS SENT</button></div>`;$('#modal').showModal();return}
  if(act==='copy-reply'){navigator.clipboard?.writeText($('#replyText').value).then(()=>toast('Reply copied.'));return}
  if(act==='send-reply'){await post(`/messages/${id}/reply`,{text:$('#replyText').value});$('#modal').close();await render();return toast('Reply recorded.')}
  if(act==='review-save'){const row=el.closest('[data-review]'),fields=Object.fromEntries([...row.querySelectorAll('[data-field]')].map(s=>[s.dataset.field,s.value]));const r=await post(`/reviews/${id}`,fields);await render();return toast('Applied: '+Object.entries(r.applied).map(([k,v])=>label(k)+' '+label(v)).join(', '))}
 }catch(e){toast(e.message)}
}
async function machineStatus(id,status,el){
 let note=null;
 if(status==='Breakdown'){note=prompt('What happened? Jev will triage it. (Leave empty to skip triage.)')||null}
 try{const r=await post(`/machines/${id}/status`,{status,note});if(r.triage)flash=`${esc(nameOf(M.machines,id))}: `+aiLine(r.triage,'triage')+(r.log.assignee?` · send: <b>${esc(r.log.assignee)}</b>`:'');await render();toast(nameOf(M.machines,id)+' → '+status)}
 catch(e){toast(e.message);render()}
}

// ---------- Export / language ----------
function doCSV(){const rows=$$('#view .table tbody tr').map(tr=>[...tr.children].map(td=>td.innerText.trim()));const csv=[['EVEREST TEXTILE MILLS',page,today()],...rows].map(r=>r.map(v=>'"'+String(v).replaceAll('"','""')+'"').join(',')).join('\r\n'),a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download=`everest-${page.toLowerCase()}-${today()}.csv`;a.click();URL.revokeObjectURL(a.href)}
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
 if(pendingRoll&&allowed[role].includes('Traceability')){traceRoll=pendingRoll;page='Traceability'}
 pendingRoll='';openApp();
}
async function submitLogin(e){
 e.preventDefault();const identity=$('#loginIdentity').value.trim(),password=$('#loginPassword').value;
 if(accessMode==='Buyer'){currentToken=identity.includes('track=')?decodeURIComponent(identity.split('track=')[1]):identity;role='Buyer';page='Tracking';history.replaceState(null,'','#track='+encodeURIComponent(currentToken));return openApp()}
 try{const r=await post('/auth/login',{username:identity,password});await startSession(r.token)}catch(err){showAuthMessage(err.message)}
}
async function firstSetup(){
 const username=$('#loginIdentity').value.trim(),password=$('#loginPassword').value;
 if(!username||password.length<8)return showAuthMessage('Type the owner username and a password of at least 8 characters first.');
 try{const r=await post('/auth/setup',{username,password});await startSession(r.token);toast('Owner account created.')}catch(err){showAuthMessage(err.message)}
}
function signOut(){if(token)fetch(API+'/auth/logout',{method:'POST',headers:{Authorization:'Bearer '+token}}).catch(()=>{});token=null;me=null;store.set('everest-token',null);currentOrder='';currentToken='';$('#app').classList.add('hidden');$('#gate').classList.remove('hidden');showGate()}

// ---------- Events ----------
$('#sideNav').addEventListener('click',e=>{const b=e.target.closest('[data-page]');if(b)setPage(b.dataset.page)});
$('#view').addEventListener('click',e=>{const nav=e.target.closest('[data-page]');if(nav)return setPage(nav.dataset.page);const act=e.target.closest('[data-act]');if(act)return action(act.dataset.act,act);const mt=e.target.closest('[data-master]');if(mt){masterKey=mt.dataset.master;editMasterId='';render()}});
$('#modal').addEventListener('click',e=>{const act=e.target.closest('[data-act]');if(act){e.preventDefault();action(act.dataset.act,act)}});
$('#view').addEventListener('submit',e=>{const f=e.target.closest('form[data-form]');if(f)submit(f,e)});
$('#view').addEventListener('change',async e=>{
 const s=e.target.dataset.machineStatus,o=e.target.dataset.machineOrder;
 if(s)return machineStatus(s,e.target.value,e.target);
 if(o&&e.target.value){try{await post('/jobs',{order_id:+e.target.value,machine_id:+o});await render();toast('Job card assigned.')}catch(err){toast(err.message)}return}
 if(e.target.matches('[data-challan]')&&e.target.files[0]){
  const file=e.target.files[0],form=e.target.closest('form');toast('Reading the challan…');
  const b64=await new Promise((ok,bad)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(',')[1]);r.onerror=bad;r.readAsDataURL(file)});
  try{const r=await post('/grn/challan',{image_b64:b64,mime:file.type||'image/jpeg'}),s=r.suggested;for(const [k,v]of Object.entries(s))if(v!=null&&form.elements[k])form.elements[k].value=v;toast(`Filled from the challan${r.extracted.supplier?' ('+r.extracted.supplier+')':''}. Check it before saving.`)}catch(err){toast(err.message)}
 }
});
$('#exportButton').addEventListener('click',doCSV);
$('#printButton').addEventListener('click',()=>window.print());
$('#roleSwitch').addEventListener('click',signOut);
$('#languageToggle').addEventListener('click',()=>switchLanguage(lang==='en'?'ta':'en'));
$('#loginForm').addEventListener('submit',submitLogin);
$('#gate').addEventListener('click',e=>{
 const tab=e.target.closest('[data-access]');if(tab){setLoginMode(tab.dataset.access);return}
 if(e.target.closest('#firstSetup')){firstSetup();return}
 if(e.target.id==='backToStory'){e.preventDefault();$('#gate').classList.add('hidden');$('#landing').classList.remove('hidden');location.hash='story';$('#story').scrollIntoView({behavior:'smooth'})}
});
$('#enterWorkspace').addEventListener('click',()=>{$('#landing').classList.add('hidden');$('#gate').classList.remove('hidden');window.scrollTo({top:0,behavior:'smooth'})});

// ---------- Start ----------
(async()=>{
 showGate();
 if(location.hash.startsWith('#track=')){currentToken=decodeURIComponent(location.hash.slice(7));role='Buyer';page='Tracking';return openApp()}
 if(location.hash.startsWith('#roll='))pendingRoll=decodeURIComponent(location.hash.slice(6));
 if(token){try{return await startSession(token)}catch{token=null;store.set('everest-token',null)}}
 if(pendingRoll){$('#landing').classList.add('hidden');$('#gate').classList.remove('hidden');return}
 showOpening();
})();
})();
