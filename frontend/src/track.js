/* The buyer's page, reached from their tracking link (#track=…): live order status, and one tap to call the mill's
   AI order desk (Retell). The backend creates the call and keeps the Retell key; the tracking token tells the desk
   which buyer is calling. Without a token the page asks for the link, and the desk asks for company and order. */
import { RetellWebClient } from 'retell-client-js-sdk';

const API = window.EVEREST_API || '/api';
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
let track = decodeURIComponent((location.hash.match(/track=([^&]+)/) || [])[1] || '');
let client = null, state = 'idle', started = 0, timer = null, volumeLoop = null;

function setState(next, text) {
  state = next;
  document.body.className = next === 'live' ? 'live' : 'idle';
  $('#status').innerHTML = text;
  $('#call').classList.toggle('end', next === 'live' || next === 'connecting');
  $('#call').setAttribute('aria-label', next === 'idle' ? 'Call the order desk' : 'End the call');
  $('#call').disabled = next === 'ending';
  $('#mute').hidden = next !== 'live';
}

const clock = () => { const s = Math.floor((Date.now() - started) / 1000); return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; };

// The rings follow the desk's voice, and the label says who is talking (the call itself has no talk events).
let lastVoice = 0;
function followVolume() {
  const rings = [...document.querySelectorAll('.orb i')];
  const tick = () => {
    const v = client?.analyzerComponent?.calculateVolume?.() || 0;
    if (v > 0.015) lastVoice = Date.now();
    const speaking = Date.now() - lastVoice < 450;
    document.body.classList.toggle('speaking', speaking);
    $('#who').textContent = speaking ? 'Desk speaking' : 'Listening';
    rings.forEach((r, i) => r.style.setProperty('--s', String(1 + Math.min(v * 3.2, 0.32) * (1 - i * 0.25))));
    volumeLoop = requestAnimationFrame(tick);
  };
  volumeLoop = requestAnimationFrame(tick);
}

function hangUp(text = 'Call ended') {
  clearInterval(timer); cancelAnimationFrame(volumeLoop);
  document.querySelectorAll('.orb i').forEach(r => r.style.removeProperty('--s'));
  document.body.classList.remove('speaking'); $('#who').textContent = '';
  client = null;
  setState('idle', text);
}

async function call() {
  if (state === 'live' || state === 'connecting') { setState('ending', 'Ending…'); client?.stopCall(); return; }
  setState('connecting', 'Allow the microphone…');
  try {
    // Microphone first: a refused mic must not open a (billed) call. The SDK opens its own stream afterwards.
    const mic = await navigator.mediaDevices.getUserMedia({audio: true});
    mic.getTracks().forEach(t => t.stop());
    setState('connecting', 'Connecting…');
    const r = await fetch(API + '/voice/web-call', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({track: track || null})});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(typeof d.detail === 'string' ? d.detail : 'The order desk is not reachable right now.');
    client = new RetellWebClient();
    client.on('call_started', () => {
      started = Date.now(); setState('live', `<b>Connected</b> · ${clock()}`);
      timer = setInterval(() => { if (state === 'live') $('#status').innerHTML = `<b>Connected</b> · ${clock()}`; }, 1000);
      followVolume();
    });
    client.on('call_ended', () => hangUp(started ? `Call ended · ${clock()}` : 'Call ended'));
    client.on('error', err => { client?.stopCall(); hangUp(`<span class="err">${esc(err?.message || err || 'Call failed')}</span>`); });
    started = 0;
    await client.startCall({accessToken: d.access_token, callId: d.call_id, transport: d.transport || undefined,
                            url: d.url || undefined, iceServers: d.ice_servers || undefined,
                            emitRawAudioSamples: true});  // needed for the volume analyser on this transport
  } catch (e) {
    const denied = /permission|notallowed|notfound/i.test(String(e?.name) + String(e?.message));
    hangUp(`<span class="err">${esc(denied ? 'Microphone blocked. Allow it in the address bar, then tap call.' : e.message)}</span>`);
  }
}

$('#call').addEventListener('click', call);
$('#mute').addEventListener('click', () => {
  const on = $('#mute').getAttribute('aria-pressed') !== 'true';
  on ? client?.mute() : client?.unmute();
  $('#mute').setAttribute('aria-pressed', String(on)); $('#mute').setAttribute('aria-label', on ? 'Unmute' : 'Mute');
});
window.addEventListener('beforeunload', () => client?.stopCall());

// ---------- the order ----------
const STAGES = ['Pending', 'Spinning', 'Weaving', 'Dyeing', 'In QC', 'Ready', 'Dispatched'];
const num = n => Number(n || 0).toLocaleString('en-IN', {maximumFractionDigits: 1});
const day = d => d ? new Date(d + 'T00:00').toLocaleDateString([], {day: 'numeric', month: 'short', year: 'numeric'}) : '—';
const when = v => v ? new Date(v.replace(' ', 'T') + 'Z').toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : '';

function showOrder(t) {
  const at = Math.max(0, STAGES.indexOf(t.stage));
  const trucks = t.dispatches.map(d => `<div><b>${esc(d.vehicle_no)}</b> · ${d.rolls} rolls · ${num(d.metres)} m · ${d.status === 'closed' ? 'left the mill' : 'loading'} · ${esc(when(d.created_at))}</div>`).join('');
  $('#order').innerHTML = `<div class="eyebrow">Order #${esc(t.order_no)}</div>
    <h1>${esc(t.fabric)} · ${esc(t.colour)}</h1>
    <div class="pill"><i></i>${esc(t.stage)}</div>
    <div class="bar" role="progressbar" aria-valuenow="${t.progress_pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${Math.min(100, t.progress_pct)}%"></i></div>
    <div class="barlabel"><span>${num(t.produced_m)} OF ${num(t.target_m)} M MADE</span><span>${t.progress_pct}%</span></div>
    <div class="steps">${STAGES.map((x, i) => `<div class="${i <= at ? 'done' : ''} ${i === at ? 'now' : ''}">${x}</div>`).join('')}</div>
    <div class="stats">
      <div class="stat"><small>Passed quality checks</small><b>${num(t.passed_m)} m</b><small>${t.rolls_passed} of ${t.rolls_produced} rolls</small></div>
      <div class="stat"><small>Dispatched</small><b>${num(t.dispatched_m)} m</b><small>${t.dispatches.length} truck${t.dispatches.length === 1 ? '' : 's'}</small></div>
      <div class="stat"><small>Expected ready</small><b>${t.expected_ready ? esc(day(t.expected_ready)) : '—'}</b><small>Deadline ${esc(day(t.deadline))}</small></div>
    </div>
    <div class="trucks">${trucks || 'Not dispatched yet.'}</div>
    <p class="note">Updates every minute. The desk can answer questions about this order.</p>`;
  $('#deskNote').textContent = `The desk knows you're calling about order #${t.order_no}. Calls are recorded and passed to the office, who can call you back.`;
}

function askForLink(message = '') {
  $('#order').innerHTML = `<div class="eyebrow">Order tracking</div><h1>Your order</h1>
    <p class="note ${message ? 'err' : ''}">${esc(message || 'Paste the tracking link the mill sent you.')}</p>
    <form class="find"><input name="link" placeholder="https://…#track=…" aria-label="Tracking link or code" required><button>Track</button></form>
    <p class="note">No link? Call the desk and have your company name and order number ready.</p>`;
  $('#order form').addEventListener('submit', e => {
    e.preventDefault();
    const v = e.target.link.value.trim();
    track = v.includes('track=') ? decodeURIComponent(v.split('track=')[1].split('&')[0]) : v;
    history.replaceState(null, '', '#track=' + encodeURIComponent(track));
    load();
  });
}

async function load(quiet = false) {
  if (!track) return askForLink();
  try {
    const r = await fetch(API + '/track/' + encodeURIComponent(track));
    if (!r.ok) throw Error(r.status === 404 ? 'That tracking link is not valid. Ask the mill for a new one.' : 'Could not load the order. Try again in a moment.');
    showOrder(await r.json());
  } catch (e) {
    if (!quiet) { track = ''; askForLink(e.message); }
  }
}
load();
window.addEventListener('hashchange', () => { track = decodeURIComponent((location.hash.match(/track=([^&]+)/) || [])[1] || ''); load(); });
setInterval(() => { if (track && !document.hidden) load(true); }, 60000);
