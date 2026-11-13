// LocalVendor-Agent frontend (plain JavaScript). ASCII-only on purpose: the rupee sign is written as \u20b9.
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const R = '\u20b9';
const inr = x => { x = Number(x); return R + x.toLocaleString('en-IN', x % 1 ? {minimumFractionDigits: 2, maximumFractionDigits: 2} : {maximumFractionDigits: 0}); };
const fmt = t => esc(t).replace(/\*(.+?)\*/g, '<b>$1</b>').replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
const isMobile = /Android|iPhone|iPad/i.test(navigator.userAgent);
const LABEL = {pending: 'Awaiting your approval', approved: 'Approved', rejected: 'Rejected', failed: 'Failed'};

let CFG = {}, Q = null, SEL = 0, via = 'text';   // via = how the order was entered: 'text' or 'voice'

async function api(path, opt) {
  const r = await fetch(path, opt);
  let j = {};
  try { j = await r.json(); } catch (e) { /* no body */ }
  if (!r.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
  return j;
}
const post = (p, b) => api(p, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b || {})});

// ---------------------------------------------------------------- input panel
const PRESETS = {
  'Sample vendor order': 'Need 50kg tomatoes, 20kg onions and 10L milk by 6 AM tomorrow under ' + R + '3500',
  'Tight-budget stress test': 'Need 60kg tomatoes, 25kg onions, 15L milk and 10kg potatoes by 6 AM tomorrow under ' + R + '3800',
  'Cafe order': 'I run a cafe. Need 5kg paneer, 12L milk and 8kg capsicum by 6 AM tomorrow under ' + R + '3000',
  'Early delivery (5:15 AM)': 'Need 10L milk and 3kg paneer by 5:15 AM under ' + R + '1500'
};
for (const k in PRESETS) {
  const b = document.createElement('button');
  b.textContent = k;
  b.onclick = () => { $('#txt').value = PRESETS[k]; via = 'text'; };
  $('#pre').append(b);
}
$('#txt').value = PRESETS['Sample vendor order'];
$('#simtxt').value = PRESETS['Sample vendor order'];
$('#txt').addEventListener('input', () => { via = 'text'; });

// speech-to-text (browser Web Speech API; works in Chrome and Edge)
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (!SR) { $('#mic').disabled = true; $('#mic').title = 'Speech recognition needs Chrome or Edge'; }
else $('#mic').onclick = () => {
  const r = new SR();
  r.lang = 'en-IN';
  r.onresult = e => { $('#txt').value = e.results[0][0].transcript; via = 'voice'; };
  r.onstart = () => { $('#mic').textContent = 'Listening...'; };
  r.onend = () => { $('#mic').textContent = 'Speak (voice)'; };
  r.start();
};

$('#shuf').onclick = async () => {
  const r = await post('/api/shuffle');
  $('#shuf').textContent = 'Shuffled (#' + r.tick + ')';
};

// ---------------------------------------------------------------- reasoning panel
function addStep(e) {
  const d = document.createElement('div');
  d.className = 'step';
  let h = `<span class="tag">${esc(e.agent || 'System')}</span><b>${esc(e.title || 'Error')}</b>`;
  if (e.lines && e.lines.length) h += '<ul>' + e.lines.map(l => `<li>${esc(l)}</li>`).join('') + '</ul>';
  if (e.code) h += `<pre>${esc(e.code)}</pre>`;
  if (e.table) h += '<div class="tbl"><table><tr>' + e.table.h.map(x => `<th>${esc(x)}</th>`).join('') + '</tr>' +
    e.table.r.map(r => '<tr>' + r.map(x => `<td>${esc(x)}</td>`).join('') + '</tr>').join('') + '</table></div>';
  if (e.chat) h += e.chat.map(m => `<div class="b ${m.from === 'vendor' ? 'v' : 's'}">${esc(m.text)}</div>`).join('');
  d.innerHTML = h;
  $('#steps').append(d);
  d.scrollIntoView({behavior: 'smooth', block: 'end'});
}

$('#go').onclick = async () => {
  const text = $('#txt').value.trim();
  if (!text) return;
  $('#steps').innerHTML = '';
  $('#out').innerHTML = '<p class="mut">Agents working...</p>';
  $('#go').disabled = true;
  try {
    const r = await fetch('/api/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text, source: via})});
    if (!r.ok) {
      let j = {}; try { j = await r.json(); } catch (e) { /* ignore */ }
      addStep({title: 'Error', lines: [j.detail || 'Request failed']});
      $('#out').innerHTML = '<p class="mut">Results appear here.</p>';
    } else {
      const rd = r.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const {done, value} = await rd.read();
        if (done) break;
        buf += dec.decode(value, {stream: true});
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const l = buf.slice(0, i); buf = buf.slice(i + 1);
          if (!l.trim()) continue;
          const e = JSON.parse(l);
          if (e.type === 'step') addStep(e);
          else if (e.type === 'result') { Q = e.result; SEL = 0; renderQuote(); }
          else { addStep({title: 'Error', lines: [e.message]}); $('#out').innerHTML = '<p class="mut">No quote was created.</p>'; }
        }
      }
    }
  } catch (e) { addStep({title: 'Error', lines: [e.message]}); }
  $('#go').disabled = false;
  refreshOutbox(); loadMetrics();
};

// ---------------------------------------------------------------- approval panel
function waLink(to, text) {
  const t = encodeURIComponent(text);
  return isMobile ? `https://wa.me/${to}?text=${t}` : `https://web.whatsapp.com/send?phone=${to}&text=${t}`;
}

function planDetail(p) {
  return p.carts.map(c => {
    const vol = c.rows.filter(r => r.vp).map(r => `${esc(r.item)} ${r.vp}%`).join(', ') || 'none';
    return `<div class="cart"><div class="cart-h"><b>${esc(c.supplier)}</b><span class="mut">quality ${c.quality}/5 &middot; arrives ${esc(c.eta)}</span></div>
      <div class="tbl"><table><tr><th>Item</th><th>Qty</th><th>Unit price</th><th>Total</th></tr>` +
      c.rows.map(r => `<tr><td>${esc(r.item)}</td><td>${r.qty} ${esc(r.unit)}</td><td>${inr(r.eff)}</td><td>${inr(r.net)}</td></tr>`).join('') +
      `<tr><td colspan="3">Delivery</td><td>${c.fee ? inr(c.fee) : 'FREE'}</td></tr>
       <tr class="tot"><td colspan="3">Supplier total</td><td>${inr(c.total)}</td></tr></table></div>
      <div class="mut small">List ${inr(c.list_total)} &middot; volume: ${vol} &middot; bundle ${c.bp}% &middot; haggle ${c.hp}%</div></div>`;
  }).join('');
}

function notes(q, p) {
  const n = [];
  if (!p.within) n.push(`<div class="warn">Over budget by ${inr(-p.budget_left)}: reduce quantities or raise the budget</div>`);
  if (!p.ontime) n.push('<div class="warn">This deal arrives after your deadline</div>');
  p.violations.forEach(v => n.push(`<div class="warn">${esc(v)}</div>`));
  (q.missing || []).forEach(m => n.push(`<div class="warn">'${esc(m)}' could not be sourced and was dropped</div>`));
  if (q.stretch) n.push('<div class="note">Suppliers were asked to stretch to their limit to fit your budget.</div>');
  const seen = new Set();
  p.carts.forEach(c => c.rows.forEach(r => {
    if (r.shelf != null && r.shelf <= 2 && !seen.has(r.item)) {
      seen.add(r.item);
      n.push(`<div class="note">Waste risk: ${esc(r.item)} keeps only about ${r.shelf} days. Order only what you will sell by then.</div>`);
    }
  }));
  return n.join('');
}

function renderQuote() {
  const q = Q, pending = q.status === 'pending';
  if (!pending && q.chosen != null) SEL = q.chosen;
  const p = q.plans[SEL];
  let h = `<div class="card"><div class="mut">Quote #${esc(q.id)} <span class="badge ${esc(q.status)}">${esc(LABEL[q.status] || q.status)}</span></div>
    <div class="small mut">Order: ${esc(q.text)}</div></div>`;
  h += '<h3>Ranked offers</h3>' + q.plans.map((x, i) => `<label class="offer ${i === SEL ? 'sel' : ''}">
    <input type="radio" name="plan" value="${i}" ${i === SEL ? 'checked' : ''} ${pending ? '' : 'disabled'}>
    <div><div><b>#${x.rank} ${esc(x.suppliers.join(' + '))}</b>${i === 0 ? '<span class="badge rec">Recommended</span>' : ''}</div>
    <div class="mut">${inr(x.total)} &middot; quality ${x.quality}/5 &middot; arrives ${esc(x.eta)} &middot; ${x.feasible ? 'Feasible' : 'Not feasible'}</div></div></label>`).join('');
  h += '<p class="mut small">Ranking: feasible deals first, then lowest cost (adjusted 2% per quality star below 5), then fewest suppliers. The negotiation chat in step 2 is for the recommended deal.</p>';
  h += '<h3>Itemized quote</h3>' + planDetail(p);
  h += `<div class="final"><div><span class="mut">Final basket</span><div class="big">${inr(p.total)}</div>
    <div class="mut small">${p.savings >= 0 ? 'Saves ' + inr(p.savings) : inr(-p.savings) + ' above'} vs a typical market quote (average list prices + delivery)</div></div>
    <div><span class="mut">Budget remaining</span>` +
    (p.budget_left == null ? '<div class="big">-</div><div class="mut small">No budget given</div>'
      : `<div class="big" style="color:${p.within ? 'var(--g)' : 'var(--warn)'}">${inr(p.budget_left)}</div>
         <div class="mut small">${Math.abs(p.pct_below)}% ${p.within ? 'below' : 'above'} budget of ${inr(q.budget)}</div>`) +
    `</div></div><div>Delivery estimate: arrives by <b>${esc(p.eta)}</b>${q.deadline ? ' (deadline ' + esc(q.deadline) + ')' : ''}</div>`;
  h += notes(q, p);
  if (pending) {
    h += `<div class="actions"><button class="ok" id="ap">Approve &amp; send orders</button><button class="rej" id="rj">Reject</button></div>
      <p class="mut small">Nothing is sent to any supplier until you approve.</p>`;
  } else if (q.status === 'rejected') {
    h += '<p class="warn">You rejected this quote. Nothing was sent to any supplier.</p>';
  } else if (q.status === 'approved') {
    h += '<h3>Orders sent</h3>' + (q.dispatch || []).map(d => {
      const cart = q.plans[q.chosen].carts.find(c => c.supplier === d.supplier);
      return `<div class="disp"><b>${esc(d.supplier)}</b> <span class="badge ${esc(d.status)}">${esc(d.status)}</span>
        <div class="mut small">${esc(d.detail)}</div>
        <a class="wl" target="_blank" rel="noopener" href="${esc(waLink(d.to, cart ? cart.whatsapp : ''))}">Open in WhatsApp (manual fallback)</a></div>`;
    }).join('') || '<p class="mut">Sending...</p>';
  }
  $('#out').innerHTML = h;
  document.querySelectorAll('input[name=plan]').forEach(r => { r.onchange = () => { SEL = +r.value; renderQuote(); }; });
  if (pending) {
    $('#ap').onclick = () => decide('approve');
    $('#rj').onclick = () => decide('reject');
  }
}

async function decide(what) {
  $('#ap').disabled = $('#rj').disabled = true;
  try { Q = await post('/api/quotes/' + Q.id + '/' + what, what === 'approve' ? {plan: SEL} : {}); }
  catch (e) { alert(e.message); }
  renderQuote(); refreshOutbox(); loadMetrics();
}

// ---------------------------------------------------------------- WhatsApp simulator
async function refreshOutbox() {
  try {
    const list = await api('/api/outbox');
    $('#outbox').innerHTML = list.slice(-10).map(m => `<div class="wa"><div class="h">To ${esc(m.to)} &middot; ${esc(m.tag)} &middot; ${esc(m.ts)}
      <span class="badge ${esc(m.status)}">${esc(m.status)}</span></div><div class="b">${fmt(m.body)}</div></div>`).join('') ||
      '<p class="mut small">No WhatsApp messages yet.</p>';
    $('#outbox').scrollTop = $('#outbox').scrollHeight;
  } catch (e) { /* ignore */ }
}
$('#obr').onclick = refreshOutbox;
$('#simgo').onclick = async () => {
  const t = $('#simtxt').value.trim();
  if (!t) return;
  $('#simgo').disabled = true;
  try {
    const r = await post('/api/whatsapp/simulate', {text: t});
    await refreshOutbox();
    if (r.quote_id) { Q = await api('/api/quotes/' + r.quote_id); SEL = 0; renderQuote(); }
  } catch (e) { alert(e.message); }
  $('#simgo').disabled = false;
  loadMetrics();
};

// ---------------------------------------------------------------- success metrics
const pc = v => v == null ? 'n/a' : v + '%';
async function loadMetrics() {
  try {
    const m = await api('/api/metrics');
    const t = (label, val, sub) => `<div class="tile"><span class="mut">${label}</span><b>${val}</b><span class="mut small">${sub || ''}</span></div>`;
    $('#mt').innerHTML = t('Runs', m.runs, m.quotes + ' quotes built') +
      t('Extraction completeness', pc(m.extraction_completeness), 'items + budget + deadline found') +
      t('Budget compliance', pc(m.budget_compliance), 'recommended deal within budget') +
      t('On-time feasible offers', pc(m.on_time_feasible), 'all items, before the deadline') +
      t('Approval rate', pc(m.approval_rate), m.approved + ' approved, ' + m.rejected + ' rejected') +
      t('Avg savings', m.avg_savings == null ? 'n/a' : inr(m.avg_savings), 'vs average market quote') +
      t('Agent time', m.avg_agent_seconds == null ? 'n/a' : m.avg_agent_seconds + ' s', 'vs about ' + m.est_manual_minutes + ' min calling suppliers (estimate)');
  } catch (e) { /* ignore */ }
}
$('#st').onclick = async () => {
  $('#st').disabled = true; $('#stout').innerHTML = '<p class="mut">Running...</p>';
  try {
    const r = await post('/api/selftest');
    $('#stout').innerHTML = `<p><b>Order extraction accuracy: ${r.accuracy}%</b> <span class="mut small">(fields correct across ${r.cases.length} labelled requests)</span></p>` +
      '<div class="tbl"><table><tr><th>Request</th><th>Items</th><th>Budget</th><th>Deadline</th></tr>' +
      r.cases.map(c => `<tr><td>${esc(c.text)}</td>` + ['items', 'budget', 'deadline'].map(k => `<td class="${c[k] ? 'ck' : 'xx'}">${c[k] ? 'ok' : 'wrong'}</td>`).join('') + '</tr>').join('') + '</table></div>';
  } catch (e) { $('#stout').innerHTML = `<p class="warn">${esc(e.message)}</p>`; }
  $('#st').disabled = false;
};

// ---------------------------------------------------------------- start
const QID = new URLSearchParams(location.search).get('quote');
if (QID) document.body.classList.add('quote-mode');
api('/api/config').then(c => {
  CFG = c;
  $('#prov').textContent = 'LLM: ' + c.provider;
  $('#wam').textContent = 'WhatsApp: ' + c.whatsapp;
}).catch(() => {});
if (QID) {
  api('/api/quotes/' + QID).then(q => { Q = q; SEL = 0; renderQuote(); })
    .catch(e => { $('#out').innerHTML = `<p class="warn">${esc(e.message)}</p>`; });
} else { refreshOutbox(); loadMetrics(); }
