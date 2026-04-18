// SMURF SPA · vanilla JS · single-page router con vistas dinámicas.
import { api } from './api.js';
import { SipWsPhone } from './sip-ws.js';

const $ = (sel, root=document) => root.querySelector(sel);
const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));
const e = (tag, attrs={}, ...children) => {
  const el = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else if (v != null) el.setAttribute(k, v);
  }
  for (const ch of children.flat()) {
    if (ch == null) continue;
    el.appendChild(typeof ch === 'string' ? document.createTextNode(ch) : ch);
  }
  return el;
};

const NAV = [
  ['/dashboard',   'Dashboard',     '📊'],
  ['/extensions',  'Extensiones',   '👤'],
  ['/trunks',      'Trunks',        '🌐'],
  ['/dialplan',    'Dial Plan',     '☎️'],
  ['/queues',      'Colas / IVRs',  '📞'],
  ['/cdrs',        'CDR · Llamadas','📜'],
  ['/recordings',  'Grabaciones',   '🎙️'],
  ['/voicemail',   'Voicemail',     '📨'],
  ['/chat',        'Chat',          '💬'],
  ['/softphone',   'Softphone',     '🎧'],
  ['/settings',    'Ajustes',       '⚙️'],
];

let currentUser = null;
let currentSocket = null;
const eventListeners = [];

window.addEventListener('smurf:logout', () => render());

function fmtDur(s) {
  if (!s) return '0s';
  s = Math.floor(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), x = s%60;
  return (h ? `${h}h ` : '') + (m ? `${m}m ` : '') + `${x}s`;
}
function fmtTs(t) {
  if (!t) return '—';
  const d = new Date(t * 1000);
  return d.toLocaleString();
}

function showModal(title, contentEl, footerBtns=[]) {
  const back = e('div', {class:'modal-back', onclick: (ev)=>{ if(ev.target===back) back.remove(); }},
    e('div', {class:'modal'},
      e('h2', {}, title),
      contentEl,
      e('div', {class:'row', style:{justifyContent:'flex-end', marginTop:'1rem', gap:'.5rem'}},
        ...footerBtns,
      ),
    ),
  );
  document.body.appendChild(back);
  return back;
}

function notify(msg, ok=true) {
  const t = e('div', {class:'card', style:{
    position:'fixed', bottom:'1.2rem', right:'1.2rem',
    padding:'.7rem 1rem', zIndex:9999, borderColor: ok ? '#4ade80' : '#f87171', color: ok ? '#4ade80' : '#f87171'
  }}, msg);
  document.body.appendChild(t);
  setTimeout(()=>t.remove(), 4000);
}

// =========================== ROUTER ===========================

async function render() {
  // ¿logueado?
  if (!api.token) return renderLogin();
  try {
    if (!currentUser) currentUser = await api.me();
  } catch (e) {
    return renderLogin();
  }
  if (!currentSocket) {
    currentSocket = api.events((ev) => {
      eventListeners.forEach(l => { try { l(ev); } catch(e){} });
    });
  }
  await renderShell();
}

function renderLogin() {
  currentUser = null;
  if (currentSocket) { try { currentSocket.close(); } catch(e){} currentSocket = null; }
  const root = $('#app');
  root.innerHTML = '';
  const errLabel = e('div', {class:'error'});
  const card = e('div', {class:'login-card'},
    e('div', {class:'brand'}, e('img', {src:'/static/img/logo.svg'}), 'SMURF'),
    e('p', {class:'muted', style:{textAlign:'center', marginTop:0}},
      'Inicia sesión en el panel'),
    e('div', {class:'field'}, e('label', {}, 'Usuario'),
      e('input', {class:'input', id:'u', autofocus:true, value:'admin'})),
    e('div', {class:'field'}, e('label', {}, 'Contraseña'),
      e('input', {class:'input', id:'p', type:'password'})),
    e('div', {class:'field'}, e('label', {}, 'Código TOTP (opcional)'),
      e('input', {class:'input', id:'t', placeholder:'000000'})),
    errLabel,
    e('button', {class:'btn primary', style:{width:'100%'}, onclick: async () => {
      errLabel.textContent = '';
      try {
        await api.login($('#u').value.trim(), $('#p').value, $('#t').value.trim() || undefined);
        currentUser = null;
        await render();
      } catch (ex) { errLabel.textContent = ex.message; }
    }}, 'Entrar'),
    e('p', {class:'muted', style:{textAlign:'center', marginTop:'1rem', fontSize:'.82rem'}},
      'admin / smurf-admin (cámbialo desde Ajustes)'),
  );
  card.querySelector('#p').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') card.querySelector('button').click();
  });
  root.appendChild(e('div', {class:'login-wrap'}, card));
}

async function renderShell() {
  const path = location.pathname === '/' ? '/dashboard' : location.pathname;
  const view = path.replace(/^\//,'');
  const root = $('#app');
  root.innerHTML = '';
  const main = e('main', {class:'main'});
  const sidebar = e('aside', {class:'sidebar'},
    e('div', {class:'brand'}, e('img', {src:'/static/img/logo.svg'}), 'SMURF'),
    ...NAV.map(([href, label, ic]) =>
      e('a', {class:'nav-item' + (href.endsWith(view) ? ' active' : ''), href,
              onclick: (ev) => { ev.preventDefault(); history.pushState({}, '', href); renderShell(); }},
        e('span', {class:'ic'}, ic), e('span', {}, label),
      )
    ),
    e('div', {class:'footer'},
      e('div', {}, `v1.0.0 · ${currentUser?.username||''}`),
      e('a', {href:'#', onclick: async (ev) => { ev.preventDefault(); await api.logout(); render(); }}, 'Cerrar sesión'),
    ),
  );
  root.appendChild(e('div', {class:'shell'}, sidebar, main));

  const VIEWS = {
    dashboard: viewDashboard,
    extensions: viewExtensions,
    trunks: viewTrunks,
    dialplan: viewDialplan,
    queues: viewQueues,
    cdrs: viewCdrs,
    recordings: viewRecordings,
    voicemail: viewVoicemail,
    chat: viewChat,
    softphone: viewSoftphone,
    settings: viewSettings,
  };
  const fn = VIEWS[view] || viewDashboard;
  await fn(main);
}

window.addEventListener('popstate', () => renderShell());

// =========================== VIEWS ===========================

async function viewDashboard(root) {
  const data = await api.get('/dashboard');
  root.appendChild(e('div', {class:'topbar'}, e('h1', {}, 'Dashboard')));
  const cards = e('div', {class:'grid cards-3'},
    kpi('Llamadas activas', data.active_calls),
    kpi('Endpoints registrados', data.registered_endpoints),
    kpi('Extensiones', data.extensions_total),
    kpi('Trunks activos', data.trunks_active),
    kpi('Llamadas (24h)', data.calls_24h),
    kpi('Tiempo conversación (24h)', fmtDur(data.talk_seconds_24h)),
  );
  root.appendChild(cards);

  const callsCard = e('div', {class:'card', style:{marginTop:'1rem'}},
    e('h3', {}, 'Llamadas activas'),
    e('div', {id:'active-calls'}),
  );
  root.appendChild(callsCard);

  const eventsCard = e('div', {class:'card', style:{marginTop:'1rem'}},
    e('h3', {}, 'Eventos en vivo'),
    e('pre', {id:'event-stream', style:{maxHeight:'300px', overflow:'auto', background:'#0a0f1c', padding:'.7rem', borderRadius:'8px', fontSize:'.78rem'}}),
  );
  root.appendChild(eventsCard);

  async function refreshActive() {
    try {
      const cs = await api.get('/calls/active');
      const c = $('#active-calls', root);
      c.innerHTML = '';
      if (cs.length === 0) { c.appendChild(e('p', {class:'muted'}, 'Sin llamadas activas.')); return; }
      const t = e('table', {},
        e('thead', {}, e('tr', {},
          e('th',{},'Origen'), e('th',{},'Destino'), e('th',{},'Estado'),
          e('th',{},'Inicio'), e('th',{},'Acciones'))),
        e('tbody', {}, ...cs.map(c => e('tr', {},
          e('td',{},c.src), e('td',{},c.dst),
          e('td',{}, e('span',{class:'badge ok'}, c.state)),
          e('td',{}, fmtTs(c.started_at)),
          e('td',{}, e('button', {class:'btn small danger', onclick: async ()=>{
            await api.post(`/calls/${c.id}/hangup`); refreshActive();
          }}, 'Colgar'))
        ))),
      );
      c.appendChild(t);
    } catch(e){}
  }
  refreshActive();
  const interval = setInterval(refreshActive, 4000);
  eventListeners.push((ev) => {
    const pre = $('#event-stream', root);
    if (!pre) return;
    pre.textContent = `[${new Date(ev.ts*1000).toLocaleTimeString()}] ${ev.type} ${JSON.stringify(ev.payload)}\n` + pre.textContent;
    pre.textContent = pre.textContent.split('\n').slice(0, 200).join('\n');
    if (['call.start','call.end','call.answered','call.ringing'].includes(ev.type)) refreshActive();
  });
}

function kpi(label, value, sub) {
  return e('div', {class:'card kpi'},
    e('span', {class:'label'}, label),
    e('span', {class:'value'}, String(value)),
    sub ? e('span', {class:'sub'}, sub) : null,
  );
}

async function viewExtensions(root) {
  root.appendChild(e('div', {class:'topbar'},
    e('h1', {}, 'Extensiones'),
    e('button', {class:'btn primary', onclick:()=>extensionForm()}, '+ Nueva extensión'),
  ));
  const tbl = e('div', {class:'card'});
  root.appendChild(tbl);
  await refreshExt(tbl);
}

async function refreshExt(holder) {
  const exts = await api.get('/extensions');
  holder.innerHTML = '';
  const t = e('table', {},
    e('thead', {}, e('tr', {},
      e('th',{},'Nº'), e('th',{},'Nombre'), e('th',{},'Email'), e('th',{},'Reg.'),
      e('th',{},'VM'), e('th',{},'Grabar'), e('th',{},'Acciones'))),
    e('tbody', {}, ...exts.map(x => e('tr', {},
      e('td',{}, x.number), e('td',{}, x.display_name || ''),
      e('td',{}, x.email || ''),
      e('td',{}, x.registered ? e('span',{class:'badge ok'},'Online') : e('span',{class:'badge'},'Offline')),
      e('td',{}, x.voicemail_enabled ? '✓' : '—'),
      e('td',{}, x.record_calls ? '●' : '—'),
      e('td',{class:'row'},
        e('button',{class:'btn small', onclick:()=>extensionForm(x)}, 'Editar'),
        e('button',{class:'btn small', onclick:()=>showSecret(x)}, 'Credenciales'),
        e('button',{class:'btn small danger', onclick: async ()=>{
          if (!confirm(`¿Borrar extensión ${x.number}?`)) return;
          await api.del(`/extensions/${x.number}`); refreshExt(holder);
        }}, 'Borrar'),
      ),
    ))),
  );
  holder.appendChild(t);
}

function extensionForm(ext=null) {
  const isNew = !ext;
  const f = e('div', {});
  const fields = [
    ['number','Número', ''],
    ['display_name','Nombre',''],
    ['email','Email',''],
    ['voicemail_pin','PIN buzón',''],
    ['no_answer_seconds','Sin respuesta (s)',25],
    ['max_concurrent_calls','Llamadas simultáneas máx.',5],
    ['sip_password','Contraseña SIP (vacío = generar)',''],
  ];
  const inputs = {};
  for (const [k, label, def] of fields) {
    const inp = e('input', {class:'input', value: ext ? (ext[k] ?? '') : def});
    if (k==='number' && ext) inp.disabled = true;
    inputs[k] = inp;
    f.appendChild(e('div', {class:'field'}, e('label',{},label), inp));
  }
  const recCb = e('input', {type:'checkbox'});
  if (ext && ext.record_calls) recCb.checked = true;
  const enCb = e('input', {type:'checkbox'});
  if (!ext || ext.enabled) enCb.checked = true;
  const vmCb = e('input', {type:'checkbox'});
  if (!ext || ext.voicemail_enabled) vmCb.checked = true;
  f.appendChild(e('div',{class:'row'},
    e('label',{},vmCb,' Buzón de voz'),
    e('label',{},recCb,' Grabar llamadas'),
    e('label',{},enCb,' Activa'),
  ));
  const back = showModal(isNew ? 'Nueva extensión' : `Editar ${ext.number}`, f, [
    e('button',{class:'btn',onclick:()=>back.remove()},'Cancelar'),
    e('button',{class:'btn primary', onclick: async () => {
      const data = {};
      for (const k of Object.keys(inputs)) {
        const v = inputs[k].value.trim();
        if (v !== '') data[k] = (k.endsWith('_seconds') || k.endsWith('_calls')) ? parseInt(v,10) : v;
      }
      data.voicemail_enabled = vmCb.checked ? 1 : 0;
      data.record_calls = recCb.checked ? 1 : 0;
      data.enabled = enCb.checked ? 1 : 0;
      try {
        if (isNew) {
          const r = await api.post('/extensions', data);
          notify(`Extensión ${data.number} creada · pwd: ${r.sip_password}`);
        } else {
          await api.put(`/extensions/${ext.number}`, data);
          notify(`Extensión ${ext.number} actualizada`);
        }
        back.remove();
        renderShell();
      } catch (ex) { alert(ex.message); }
    }}, 'Guardar'),
  ]);
}

function showSecret(ext) {
  const realm = location.hostname || 'smurf.local';
  const txt = `Servidor SIP : ${realm}\nUsuario     : ${ext.number}\nDisplay     : ${ext.display_name||ext.number}\nContraseña  : ${ext.sip_password||'(usa el formulario para generar)'}\nTransporte  : UDP/TCP/WS/WSS`;
  showModal(`Credenciales SIP · ${ext.number}`,
    e('pre', {style:{background:'#0a0f1c', padding:'1rem', borderRadius:'8px'}}, txt),
    [e('button',{class:'btn',onclick:(ev)=>ev.target.closest('.modal-back').remove()},'Cerrar')]);
}

async function viewTrunks(root) {
  root.appendChild(e('div', {class:'topbar'},
    e('h1', {}, 'Trunks SIP'),
    e('button', {class:'btn primary', onclick:()=>trunkForm()}, '+ Nuevo trunk'),
  ));
  const tbl = e('div', {class:'card'});
  root.appendChild(tbl);
  await refreshTrunks(tbl);
}
async function refreshTrunks(holder) {
  const ts = await api.get('/trunks');
  holder.innerHTML = '';
  if (ts.length === 0) { holder.appendChild(e('p',{class:'muted'},'Sin trunks. Añade uno para llamadas a la PSTN.')); return; }
  const t = e('table', {},
    e('thead', {}, e('tr', {},
      e('th',{},'Nombre'),e('th',{},'Host'),e('th',{},'Transport'),
      e('th',{},'Auth'),e('th',{},'Activo'),e('th',{},'Acciones'))),
    e('tbody',{}, ...ts.map(x => e('tr',{},
      e('td',{},x.name), e('td',{},`${x.host}:${x.port}`), e('td',{}, x.transport),
      e('td',{}, x.auth_mode), e('td',{}, x.enabled ? '✓' : '—'),
      e('td',{class:'row'},
        e('button',{class:'btn small', onclick:()=>trunkForm(x)},'Editar'),
        e('button',{class:'btn small danger', onclick: async ()=>{
          if (!confirm(`¿Borrar trunk ${x.name}?`)) return;
          await api.del(`/trunks/${x.id}`); refreshTrunks(holder);
        }},'Borrar'),
      ),
    ))),
  );
  holder.appendChild(t);
}
function trunkForm(t=null) {
  const isNew = !t;
  const fields = [
    ['name','Nombre',''], ['host','Host',''], ['port','Puerto',5060],
    ['transport','Transport (udp/tcp/tls)','udp'],
    ['username','Usuario auth',''], ['password','Contraseña auth',''],
    ['realm','Realm (opcional)',''],
    ['auth_mode','Modo auth (credentials/ip)','credentials'],
    ['from_user','From user',''], ['from_domain','From domain',''],
    ['priority','Prioridad', 100],
  ];
  const inputs = {};
  const f = e('div',{});
  for (const [k,label,def] of fields) {
    const inp = e('input',{class:'input', value: t ? (t[k] ?? def) : def});
    inputs[k] = inp;
    f.appendChild(e('div',{class:'field'}, e('label',{},label), inp));
  }
  const reg = e('input',{type:'checkbox'}); if (!t || t.register) reg.checked = true;
  const en  = e('input',{type:'checkbox'}); if (!t || t.enabled) en.checked = true;
  f.appendChild(e('div',{class:'row'},
    e('label',{},reg,' Registrar en el peer'),
    e('label',{},en, ' Activo'),
  ));
  const back = showModal(isNew ? 'Nuevo trunk' : `Editar trunk ${t.name}`, f, [
    e('button',{class:'btn',onclick:()=>back.remove()},'Cancelar'),
    e('button',{class:'btn primary', onclick: async () => {
      const data = {};
      for (const k of Object.keys(inputs)) {
        let v = inputs[k].value.trim();
        if (['port','priority'].includes(k)) v = parseInt(v,10);
        data[k] = v === '' ? null : v;
      }
      data.register = reg.checked;
      data.enabled  = en.checked;
      if (isNew) await api.post('/trunks', data);
      else      await api.put(`/trunks/${t.id}`, data);
      back.remove(); renderShell();
    }}, 'Guardar'),
  ]);
}

async function viewDialplan(root) {
  root.appendChild(e('div', {class:'topbar'},
    e('h1', {}, 'Dial Plan'),
    e('button', {class:'btn primary', onclick:()=>dpForm()}, '+ Nueva regla'),
  ));
  const tbl = e('div', {class:'card'}); root.appendChild(tbl);
  await refreshDp(tbl);
}
async function refreshDp(holder) {
  const rows = await api.get('/dialplan');
  holder.innerHTML = '';
  const t = e('table', {},
    e('thead', {}, e('tr',{},
      e('th',{},'Prio'), e('th',{},'Nombre'), e('th',{},'Dirección'),
      e('th',{},'Pattern'), e('th',{},'Target'), e('th',{},'Activa'), e('th',{},'Acciones'))),
    e('tbody',{}, ...rows.map(x => e('tr',{},
      e('td',{},x.priority), e('td',{},x.name), e('td',{},x.direction),
      e('td',{}, e('code',{},x.pattern)),
      e('td',{}, `${x.target_type}:${x.target_value}`),
      e('td',{}, x.enabled ? '✓' : '—'),
      e('td',{class:'row'},
        e('button',{class:'btn small', onclick:()=>dpForm(x)},'Editar'),
        e('button',{class:'btn small danger', onclick: async ()=>{
          if (!confirm('¿Borrar regla?')) return;
          await api.del(`/dialplan/${x.id}`); refreshDp(holder);
        }},'Borrar'),
      ),
    ))),
  );
  holder.appendChild(t);
}
function dpForm(r=null) {
  const isNew = !r;
  const fields = [
    ['name','Nombre',''], ['direction','Dirección (internal/inbound/outbound)','internal'],
    ['pattern','Pattern regex',''],
    ['target_type','Target type (extension/queue/ivr/ringgroup/trunk/voicemail/conference/hangup)','extension'],
    ['target_value','Target value',''],
    ['priority','Prioridad', 100],
    ['strip_digits','Strip digits', 0], ['prepend','Prepend',''],
  ];
  const inputs = {};
  const f = e('div',{});
  for (const [k,label,def] of fields) {
    const inp = e('input',{class:'input', value: r ? (r[k] ?? def) : def});
    inputs[k] = inp;
    f.appendChild(e('div',{class:'field'}, e('label',{},label), inp));
  }
  const en = e('input',{type:'checkbox'}); if (!r || r.enabled) en.checked = true;
  f.appendChild(e('div',{class:'row'}, e('label',{},en,' Activa')));
  const back = showModal(isNew ? 'Nueva regla' : 'Editar regla', f, [
    e('button',{class:'btn',onclick:()=>back.remove()},'Cancelar'),
    e('button',{class:'btn primary', onclick: async () => {
      const data = {};
      for (const k of Object.keys(inputs)) {
        let v = inputs[k].value.trim();
        if (['priority','strip_digits'].includes(k)) v = parseInt(v||'0', 10);
        data[k] = v;
      }
      data.enabled = en.checked ? 1 : 0;
      if (isNew) await api.post('/dialplan', data);
      else      await api.put(`/dialplan/${r.id}`, data);
      back.remove(); renderShell();
    }}, 'Guardar'),
  ]);
}

async function viewQueues(root) {
  root.appendChild(e('div',{class:'topbar'}, e('h1',{},'Colas, IVRs y Ring-groups')));
  const sec = async (title, api_path, render_row) => {
    const card = e('div',{class:'card', style:{marginBottom:'1rem'}},
      e('h3',{}, title));
    root.appendChild(card);
    const rows = await api.get(api_path);
    if (!rows.length) card.appendChild(e('p',{class:'muted'},'Sin entradas.'));
    else {
      const t = e('table',{}, e('thead',{},e('tr',{},
        e('th',{},'#'),e('th',{},'Nombre/Datos'),e('th',{},'Acciones'))),
        e('tbody',{}, ...rows.map(r => e('tr',{}, ...render_row(r, api_path)))));
      card.appendChild(t);
    }
  };
  await sec('Colas', '/queues', r => [
    e('td',{},r.number), e('td',{}, `${r.name} · ${r.strategy} · miembros: ${r.members_csv||'—'}`),
    e('td',{}, e('button',{class:'btn small danger', onclick: async ()=>{
      if (!confirm('¿Borrar?')) return; await api.del(`/queues/${r.id}`); renderShell(); }}, 'Borrar')),
  ]);
  await sec('Ring-groups', '/ringgroups', r => [
    e('td',{},r.number), e('td',{}, `${r.name} · ${r.strategy} · miembros: ${r.members_csv||'—'}`),
    e('td',{}, e('button',{class:'btn small danger', onclick: async ()=>{
      if (!confirm('¿Borrar?')) return; await api.del(`/ringgroups/${r.id}`); renderShell(); }}, 'Borrar')),
  ]);
  await sec('IVRs', '/ivrs', r => [
    e('td',{},r.number), e('td',{}, `${r.name} · timeout ${r.timeout}s`),
    e('td',{}, e('button',{class:'btn small danger', onclick: async ()=>{
      if (!confirm('¿Borrar?')) return; await api.del(`/ivrs/${r.id}`); renderShell(); }}, 'Borrar')),
  ]);
}

async function viewCdrs(root) {
  root.appendChild(e('div',{class:'topbar'},
    e('h1',{},'CDR · Llamadas'),
    e('div', {class:'row'},
      e('a', {class:'btn', href:'/api/v1/cdr.csv?token='+(api.token||'')}, 'Descargar CSV'),
    ),
  ));
  const card = e('div',{class:'card'}); root.appendChild(card);
  const rows = await api.get('/cdr?limit=200');
  if (!rows.length) { card.appendChild(e('p',{class:'muted'},'Sin registros.')); return; }
  const t = e('table',{}, e('thead',{},e('tr',{},
    e('th',{},'Inicio'), e('th',{},'Origen'), e('th',{},'Destino'),
    e('th',{},'Disp.'), e('th',{},'Duración'), e('th',{},'Trunk'), e('th',{},'Grabación'))),
    e('tbody',{}, ...rows.map(r => e('tr',{},
      e('td',{}, fmtTs(r.started_at)),
      e('td',{}, r.src_number || '—'),
      e('td',{}, r.dst_number || '—'),
      e('td',{}, e('span',{class:'badge ' + (r.disposition === 'ANSWERED' ? 'ok' : 'warn')}, r.disposition || '?')),
      e('td',{}, fmtDur(r.bill_seconds || r.duration)),
      e('td',{}, r.via_trunk || '—'),
      e('td',{}, r.recording_path
        ? e('a',{href:`/api/v1/recordings/${r.id}`, target:'_blank'},'⬇')
        : '—'),
    ))),
  );
  card.appendChild(t);
}

async function viewRecordings(root) { return viewCdrs(root); }

async function viewVoicemail(root) {
  root.appendChild(e('div',{class:'topbar'}, e('h1',{},'Buzón de voz')));
  const exts = await api.get('/extensions');
  for (const ext of exts) {
    const vms = await api.get(`/voicemail/${ext.number}`);
    if (vms.length === 0) continue;
    const card = e('div',{class:'card', style:{marginBottom:'.8rem'}},
      e('h3',{}, `Extensión ${ext.number} (${vms.length})`),
      e('table',{}, e('thead',{},e('tr',{},
        e('th',{},'De'),e('th',{},'Recibido'),e('th',{},'Dur.'),e('th',{},'Audio'),e('th',{},''))),
        e('tbody',{}, ...vms.map(v => e('tr',{},
          e('td',{}, v.caller || '—'),
          e('td',{}, fmtTs(v.received_at)),
          e('td',{}, fmtDur(v.duration)),
          e('td',{}, e('audio',{controls:'', src:`/api/v1/voicemail/${ext.number}/${v.id}/audio`})),
          e('td',{}, e('button',{class:'btn small danger', onclick: async ()=>{
            if (!confirm('¿Borrar?')) return; await api.del(`/voicemail/${ext.number}/${v.id}`); renderShell(); }},'Borrar')),
        ))),
      ),
    );
    root.appendChild(card);
  }
  if (root.children.length === 1) root.appendChild(e('p',{class:'muted'},'Sin mensajes en buzón.'));
}

async function viewChat(root) {
  root.appendChild(e('div',{class:'topbar'}, e('h1',{},'Chat interno')));
  const exts = await api.get('/extensions');
  const card = e('div',{class:'card'},
    e('div',{class:'row'},
      e('label',{},'Como: ', e('input',{class:'input', id:'chat-from', value: currentUser.username, style:{width:'140px'}})),
      e('label',{},'Para: ', e('input',{class:'input', id:'chat-to', value: exts[0]?.number || '', style:{width:'140px'}})),
    ),
    e('textarea',{class:'input', id:'chat-msg', rows:3, style:{marginTop:'.5rem'}}),
    e('div',{class:'row', style:{marginTop:'.5rem'}},
      e('button',{class:'btn primary', onclick: async () => {
        const from = $('#chat-from', card).value, to = $('#chat-to', card).value, body = $('#chat-msg', card).value;
        if (!body) return;
        await api.post('/chat/send', {from, to, body});
        $('#chat-msg', card).value = '';
        notify('Mensaje enviado');
      }}, 'Enviar'),
    ),
  );
  root.appendChild(card);
}

async function viewSoftphone(root) {
  root.appendChild(e('div',{class:'topbar'}, e('h1',{},'Softphone WebRTC')));
  const exts = await api.get('/extensions');
  const card = e('div',{class:'softphone'});

  const select = e('select', {class:'input'},
    ...exts.map(x => e('option',{value:x.number, 'data-pwd': x.sip_password||''},
                       `${x.number} · ${x.display_name||''}`)));
  card.appendChild(e('div',{class:'field'}, e('label',{},'Identificarse como'), select));

  const status = e('div',{class:'call-status'}, 'Desconectado');
  const display = e('div',{class:'call-display'}, '');
  card.appendChild(display); card.appendChild(status);

  const dialed = { value: '' };
  function setDisp() { display.textContent = dialed.value || '—'; }

  const dialpad = e('div',{class:'dialpad'});
  const buttons = [
    ['1',''], ['2','ABC'], ['3','DEF'],
    ['4','GHI'], ['5','JKL'], ['6','MNO'],
    ['7','PQRS'], ['8','TUV'], ['9','WXYZ'],
    ['*',''], ['0','+'], ['#',''],
  ];
  for (const [d, sub] of buttons) {
    dialpad.appendChild(e('button',{onclick:()=>{ dialed.value += d; setDisp();
      if (window.__phone && window.__phone.activeCall && window.__phone.activeCall.state === 'answered') {
        window.__phone.activeCall.sendDtmf(d);
      }
    }}, d, e('span',{class:'sub'}, sub)));
  }
  card.appendChild(dialpad);

  const ctrl = e('div',{class:'call-controls'},
    e('button',{class:'btn-circle ghost', onclick:()=>{ dialed.value = dialed.value.slice(0,-1); setDisp(); }}, '⌫'),
    e('button',{class:'btn-circle green', onclick: async ()=>{
      const phone = window.__phone;
      if (!phone || !phone.registered) { notify('Conéctate primero', false); return; }
      if (!dialed.value) return;
      try {
        const c = await phone.call(dialed.value);
        c.addEventListener('state', (e) => status.textContent = e.detail);
        c.addEventListener('end', () => status.textContent = 'Finalizada');
        status.textContent = 'Llamando…';
      } catch (e) { notify(e.message, false); }
    }}, '📞'),
    e('button',{class:'btn-circle red', onclick:()=>{
      window.__phone?.hangup();
    }}, '✖'),
  );
  card.appendChild(ctrl);

  const connectBtn = e('button',{class:'btn primary', style:{marginTop:'1rem', width:'100%'},
    onclick: async () => {
    const opt = select.options[select.selectedIndex];
    const ext = opt.value, pwd = opt.dataset.pwd;
    if (!pwd) { notify('Esa extensión no tiene password en panel; edítala primero', false); return; }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    // por defecto el WS de SMURF está en :5062 (ws) o :5063 (wss)
    const wsPort = location.protocol === 'https:' ? '5063' : '5062';
    const wsUrl = `${proto}://${location.hostname}:${wsPort}`;
    const phone = new SipWsPhone({ wsUrl, realm: location.hostname, username: ext, password: pwd, displayName: ext });
    window.__phone = phone;
    phone.addEventListener('registered', () => status.textContent = 'Registrado · listo');
    phone.addEventListener('disconnected', () => status.textContent = 'Desconectado');
    phone.addEventListener('incoming', (ev) => {
      const c = ev.detail;
      status.textContent = `Llamada entrante de ${c.from}`;
      if (confirm(`Llamada entrante de ${c.from}. ¿Aceptar?`)) {
        c.accept();
        c.addEventListener('state', (e)=>status.textContent = e.detail);
      } else c.reject();
    });
    try {
      status.textContent = 'Conectando WS…';
      await phone.connect();
      status.textContent = 'Registrando…';
      await phone.register();
    } catch (e) { notify('Error: ' + e.message, false); status.textContent = 'Error'; }
  }}, 'Conectar / Registrar');
  card.appendChild(connectBtn);
  root.appendChild(card);
}

async function viewSettings(root) {
  root.appendChild(e('div',{class:'topbar'}, e('h1',{},'Ajustes')));
  const s = await api.get('/settings');
  root.appendChild(e('div',{class:'card'},
    e('h3',{},'SIP'),
    e('pre',{},JSON.stringify(s.sip, null, 2)),
  ));
  root.appendChild(e('div',{class:'card', style:{marginTop:'1rem'}},
    e('h3',{},'RTP'),
    e('pre',{},JSON.stringify(s.rtp, null, 2)),
  ));
  root.appendChild(e('div',{class:'card', style:{marginTop:'1rem'}},
    e('h3',{},'Cambiar contraseña'),
    e('div',{class:'field'}, e('label',{},'Actual'), e('input',{class:'input', id:'pw-old', type:'password'})),
    e('div',{class:'field'}, e('label',{},'Nueva'),  e('input',{class:'input', id:'pw-new', type:'password'})),
    e('button',{class:'btn primary', onclick: async ()=>{
      const old = $('#pw-old', root).value, neu = $('#pw-new', root).value;
      try { await api.post('/auth/change-password', {old, new: neu}); notify('Contraseña cambiada'); }
      catch (e) { notify(e.message, false); }
    }}, 'Cambiar contraseña'),
  ));
  root.appendChild(e('div',{class:'card', style:{marginTop:'1rem'}},
    e('h3',{},'Backup / Restore'),
    e('div',{class:'row'},
      e('a',{class:'btn', href:'/api/v1/backup', target:'_blank'}, 'Descargar backup'),
      e('input',{type:'file', id:'bk', accept:'.json'}),
      e('button',{class:'btn primary', onclick: async () => {
        const f = $('#bk', root).files[0];
        if (!f) return;
        const fd = new FormData(); fd.append('file', f);
        const resp = await fetch('/api/v1/restore', {method:'POST', headers:{Authorization:`Bearer ${api.token}`}, body: fd});
        const j = await resp.json();
        notify(resp.ok ? `Restaurado ${(j.tables||[]).length} tablas` : 'Error', resp.ok);
      }}, 'Restaurar'),
    ),
  ));
}

// arranque
render();
