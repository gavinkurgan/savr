/* savr client: dependency-free. Cart lives on the device; household data lives on the server. */
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
function toast(msg, ms = 1900) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toast._h); toast._h = setTimeout(() => t.classList.remove('show'), ms);
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body ? 'POST' : 'GET'), headers: { 'Content-Type': 'application/json' } };
  if (opts.body) init.body = JSON.stringify(opts.body);
  const r = await fetch(path, init);
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}
const member = () => store.get('savr_member', '') || 'me';

/* ---------- voice: standard vs unhinged ---------- */
const Voice = {
  get() { try { return localStorage.getItem('savr_voice') || 'standard'; } catch { return 'standard'; } },
  set(v) {
    try { localStorage.setItem('savr_voice', v); } catch {}
    document.documentElement.classList.toggle('voice-unhinged', v === 'unhinged');
    Voice.paint();
    if (v === 'unhinged' && !store.get('savr_warned', false)) { toast('Unhinged mode: explicit language ahead 🔥', 2600); store.set('savr_warned', true); }
    document.dispatchEvent(new CustomEvent('voicechange'));
  },
  paint() {
    const v = Voice.get();
    document.querySelectorAll('[data-voice]').forEach(b => { const on = b.dataset.voice === v; b.classList.toggle('on', on); b.setAttribute('aria-checked', on); });
  },
};

/* ---------- cart ---------- */
const Cart = {
  items() { return store.get('cart', []); },
  save(items) { store.set('cart', items); Cart.badge(); },
  add(id, name, servings, quiet) {
    const items = Cart.items();
    if (items.find(i => i.id === id)) { if (!quiet) toast('Already in cart'); return false; }
    items.push({ id, name, servings: servings || null }); Cart.save(items);
    if (!quiet) toast(`Added ${name}`);
    return true;
  },
  badge() {
    const n = Cart.items().length;
    document.querySelectorAll('.cart-count').forEach(b => { b.textContent = n; b.hidden = !n; });
  },
  renderPage(krogerEnabled) {
    Cart.kroger = krogerEnabled;
    const wrap = document.getElementById('cart-recipes');
    const items = Cart.items();
    document.getElementById('cart-options').hidden = !items.length;
    if (!items.length) { wrap.innerHTML = '<p class="empty">Your cart is empty. Tap ＋ on any recipe, or <a href="/plan">plan your week</a>.</p>'; document.getElementById('list-out').hidden = true; return; }
    wrap.innerHTML = items.map((i, n) => `
      <div class="row"><a href="/recipe/${i.id}">${esc(i.name)}</a>
        <label>Serves <input type="number" min="1" value="${i.servings || ''}" data-serv="${n}"></label>
        <button data-rm="${n}" aria-label="Remove">✕</button></div>`).join('');
    wrap.querySelectorAll('[data-serv]').forEach(inp => inp.onchange = () => { const it = Cart.items(); it[+inp.dataset.serv].servings = +inp.value || null; Cart.save(it); });
    wrap.querySelectorAll('[data-rm]').forEach(b => b.onclick = () => { const it = Cart.items(); it.splice(+b.dataset.rm, 1); Cart.save(it); Cart.renderPage(Cart.kroger); });
    document.getElementById('build').onclick = Cart.build;
    document.getElementById('clear-cart').onclick = () => { Cart.save([]); Cart.renderPage(Cart.kroger); };
    document.querySelectorAll('[data-export]').forEach(b => b.onclick = () => Cart.export(b.dataset.export));
    document.getElementById('bought').onclick = Cart.bought;
  },
  body(format) {
    return {
      recipes: Cart.items().map(i => ({ id: i.id, servings: i.servings })),
      exclude_pantry: document.getElementById('exclude-pantry').checked,
      use_pantry: document.getElementById('use-pantry').checked,
      have: document.getElementById('have').value.split(',').map(s => s.trim()).filter(Boolean),
      format,
    };
  },
  async raw(format, extra = {}) {
    const r = await fetch('/api/grocery', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...Cart.body(format), ...extra }) });
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    return r;
  },
  async build() {
    try {
      const data = await (await Cart.raw('json')).json();
      let aisle = null, html = '';
      for (const it of data.items) {
        if (it.aisle !== aisle) { aisle = it.aisle; html += `<div class="aisle">${esc(aisle)}</div>`; }
        html += `<label class="shop-item"><input type="checkbox"><span>${esc(it.display)}<small>${esc(it.recipes.join(', '))}</small></span></label>`;
      }
      document.getElementById('list-items').innerHTML = html || '<p class="empty">You already have everything. Nice.</p>';
      document.getElementById('list-count').textContent = `(${data.count} items)`;
      document.getElementById('shared-out').innerHTML = data.shared && data.shared.length
        ? `<p class="fine">Shared across recipes: ${data.shared.slice(0, 6).map(s => esc(s.item)).join(', ')}</p>` : '';
      document.getElementById('list-out').hidden = false;
      document.getElementById('export-out').innerHTML = '';
      if (Cart.kroger) Kroger.render();
    } catch (e) { toast(e.message); }
  },
  download(text, filename, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type })); a.download = filename; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  },
  async export(kind) {
    const out = document.getElementById('export-out');
    try {
      if (kind === 'csv' || kind === 'text') {
        const text = await (await Cart.raw(kind)).text();
        Cart.download(text, kind === 'csv' ? 'savr-shopping-list.csv' : 'savr-shopping-list.txt', kind === 'csv' ? 'text/csv' : 'text/plain');
        return;
      }
      if (kind === 'share') {
        const text = await (await Cart.raw('text')).text();
        if (navigator.share) await navigator.share({ title: 'savr shopping list', text });
        else { await navigator.clipboard.writeText(text); toast('Copied to clipboard'); }
        return;
      }
      if (kind === 'instacart') {
        const data = await (await Cart.raw('instacart', { create_link: true })).json();
        out.innerHTML = data.ok && data.url
          ? `<p><a class="btn primary" href="${esc(data.url)}" target="_blank" rel="noopener">Open in Instacart → pick store & pickup time</a></p>`
          : `<p class="fine">${esc(data.reason || '')}</p><pre>${esc(JSON.stringify(data.payload, null, 2))}</pre>`;
        return;
      }
      const data = await (await Cart.raw(kind)).json();
      out.innerHTML = `<ul>${data.links.map(l => `<li><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.item)}</a></li>`).join('')}</ul>`;
    } catch (e) { toast(e.message); }
  },
  async bought() {
    try {
      const d = await api('/api/pantry/bought', { body: Cart.body('json') });
      toast(`Added ${d.added} items to your pantry`);
    } catch (e) { toast(e.message); }
  },
};

/* ---------- Kroger ---------- */
const Kroger = {
  async render() {
    const el = document.getElementById('kroger-state');
    if (!el) return;
    const s = await api('/api/kroger/status');
    if (!s.connected) {
      el.innerHTML = `<p class="fine">Sign in once and savr fills your Kroger pickup cart for you (King Soopers, Ralphs, Fred Meyer, Smith's and more).</p>
        <a class="btn primary" href="/kroger/connect?next=/cart">Connect Kroger</a>`;
      return;
    }
    el.innerHTML = `<p>Store: <b>${esc(s.location_name || 'not chosen yet')}</b></p>
      <form id="kz" class="row-form"><input name="zip" inputmode="numeric" maxlength="5" placeholder="ZIP to find stores"><button class="btn">Find</button></form>
      <div id="kstores"></div>
      <button class="btn primary" id="kpush" ${s.location_id ? '' : 'disabled'}>Add everything to my Kroger pickup cart</button>
      <div id="kresult"></div>`;
    document.getElementById('kz').onsubmit = async (e) => {
      e.preventDefault();
      try {
        const d = await api('/api/kroger/locations?zip=' + encodeURIComponent(e.target.zip.value));
        document.getElementById('kstores').innerHTML = d.locations.map(l =>
          `<button class="btn small store" data-id="${esc(l.id)}" data-name="${esc(l.name)}">${esc(l.name)} · ${esc(l.address)}</button>`).join('') || '<p class="fine">No stores found.</p>';
        document.querySelectorAll('.store').forEach(b => b.onclick = async () => {
          await api('/api/kroger/location', { body: { id: b.dataset.id, name: b.dataset.name } }); Kroger.render();
        });
      } catch (err) { toast(err.message); }
    };
    document.getElementById('kpush').onclick = async (e) => {
      e.target.disabled = true; e.target.textContent = 'Matching products…';
      try {
        const d = await api('/api/kroger/cart', { body: Cart.body('json') });
        document.getElementById('kresult').innerHTML = `<p><b>${d.added.length} items added</b>${d.estimated_total ? ` · about $${d.estimated_total.toFixed(2)}` : ''}. Open the Kroger app to choose a pickup time and check out.</p>
          <ul class="fine">${d.added.map(a => `<li>${esc(a.item)} → ${esc(a.product)} ${a.size ? '(' + esc(a.size) + ')' : ''} ×${a.quantity}</li>`).join('')}</ul>
          ${d.unmatched.length ? `<p class="fine">Couldn't match: ${d.unmatched.map(esc).join(', ')}</p>` : ''}`;
        e.target.textContent = 'Added ✓';
      } catch (err) { toast(err.message); e.target.disabled = false; e.target.textContent = 'Try again'; }
    };
  },
};

/* ---------- ratings ---------- */
async function vote(group, value) {
  const recipe_id = +group.dataset.recipe;
  const btns = group.querySelectorAll('[data-vote]');
  const current = [...btns].find(b => b.classList.contains('on'));
  const v = current && +current.dataset.vote === value ? 0 : value;  // tap again to clear
  try {
    const d = await api('/api/ratings', { body: { recipe_id, value: v, member: member() } });
    btns.forEach(b => {
      b.classList.toggle('on', +b.dataset.vote === v);
      const small = b.querySelector('small');
      if (small) small.textContent = (+b.dataset.vote > 0 ? d.ratings.up : d.ratings.down) || '';
    });
    toast(v > 0 ? 'Saved to household favorites' : v < 0 ? "Got it, savr won't suggest that again" : 'Rating cleared');
  } catch (e) { toast(e.message); }
}

/* ---------- timers (shared by recipe page and cook mode) ---------- */
const Timers = {
  start(btn) {
    if (btn._iv) { clearInterval(btn._iv); btn._iv = null; btn.classList.remove('running'); btn.textContent = `⏱ ${btn.dataset.minutes} min`; return; }
    let left = +btn.dataset.minutes * 60; btn.classList.add('running');
    const tick = () => {
      const m = Math.floor(left / 60), s = String(left % 60).padStart(2, '0');
      btn.textContent = `⏱ ${m}:${s} · tap to stop`;
      if (left-- <= 0) {
        clearInterval(btn._iv); btn._iv = null; btn.textContent = '✅ Done'; btn.classList.remove('running');
        navigator.vibrate?.([300, 120, 300]);
        const msg = Voice.get() === 'unhinged' ? `${btn.dataset.label || 'Timer'}: DONE. Go check that shit.` : `${btn.dataset.label || 'Timer'} is done`;
        toast(msg, 4000);
        if (window.speechSynthesis && CookMode.speaking) speechSynthesis.speak(new SpeechSynthesisUtterance(msg));
      }
    };
    tick(); btn._iv = setInterval(tick, 1000);
  },
};

/* ---------- plan ---------- */
const Plan = {
  init() {
    const form = document.getElementById('plan-form');
    form.onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(form);
      const btn = form.querySelector('button[type=submit]'); btn.disabled = true; btn.textContent = 'Planning…';
      try {
        await api('/api/plan', { body: { text: f.get('text'), nights: +f.get('nights'), servings: +f.get('servings') || null,
          max_minutes: +f.get('max_minutes') || null, diet: f.getAll('diet') } });
        location.reload();
      } catch (err) { toast(err.message); btn.disabled = false; btn.textContent = 'Plan my week'; }
    };
    document.querySelectorAll('.swap').forEach(b => b.onclick = async () => {
      b.disabled = true; b.textContent = '…';
      try { await api('/api/plan/swap', { body: { slot: +b.dataset.slot } }); location.reload(); }
      catch (err) { toast(err.message); b.disabled = false; b.textContent = '↻ Swap'; }
    });
    const all = document.getElementById('plan-to-cart');
    if (all) all.onclick = () => {
      const items = JSON.parse(all.dataset.items);
      let n = 0; items.forEach(i => { if (Cart.add(i.id, i.name, i.servings, true)) n++; });
      toast(n ? `Added ${n} recipes. Head to Cart to build one order.` : 'Already in your cart');
    };
  },
};

/* ---------- pantry ---------- */
const Pantry = {
  init() {
    const form = document.getElementById('pantry-form');
    form.onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(form);
      try { await api('/api/pantry', { body: { items: f.get('items'), use_by: f.get('use_by') || null } }); location.reload(); }
      catch (err) { toast(err.message); }
    };
    document.querySelectorAll('[data-remove]').forEach(b => b.onclick = async () => {
      await api('/api/pantry?id=' + b.dataset.remove, { method: 'DELETE' }); b.closest('li').remove();
    });
  },
};

/* ---------- household ---------- */
const Household = {
  init() {
    const m = document.getElementById('member');
    m.value = store.get('savr_member', '');
    m.onchange = () => { store.set('savr_member', m.value.trim()); toast('Saved'); };
    const code = document.getElementById('hh-code').textContent.trim();
    document.getElementById('share-code').onclick = async () => {
      const text = `Join our savr household: ${code}  (${location.origin}/household)`;
      if (navigator.share) await navigator.share({ title: 'savr household', text }).catch(() => {});
      else { await navigator.clipboard.writeText(code); toast('Code copied'); }
    };
    document.getElementById('rename').onclick = async () => {
      const name = prompt('Household name', document.querySelector('h1').textContent);
      if (name) { await api('/api/household/name', { body: { name } }); location.reload(); }
    };
    document.getElementById('join-form').onsubmit = async (e) => {
      e.preventDefault();
      try { await api('/api/household/join', { body: { code: e.target.code.value } }); location.reload(); }
      catch (err) { toast(err.message, 3500); }
    };
    document.getElementById('new-hh').onclick = async () => {
      if (!confirm('Start a fresh household? You can rejoin this one later with its code.')) return;
      await api('/api/household/new', { body: {} }); location.reload();
    };
    const off = document.getElementById('kroger-off');
    if (off) off.onclick = async () => { await api('/api/kroger/disconnect', { body: {} }); location.reload(); };
  },
};

/* ---------- cook mode ---------- */
const CookMode = {
  i: 0, n: 0, speaking: false, wake: null,
  init(n) {
    CookMode.n = n;
    const steps = [...document.querySelectorAll('.cook-step')];
    const show = (i) => {
      CookMode.i = Math.max(0, Math.min(n, i));
      steps.forEach((s, k) => s.hidden = k !== CookMode.i);
      document.getElementById('progress').style.width = `${(CookMode.i / n) * 100}%`;
      document.getElementById('prev').disabled = CookMode.i === 0;
      document.getElementById('next').textContent = CookMode.i >= n ? 'Done' : CookMode.i === n - 1 ? 'Finish' : 'Next →';
      if (CookMode.speaking) CookMode.say();
    };
    CookMode.show = show;
    document.getElementById('next').onclick = () => CookMode.i >= n ? (location.href = location.pathname.replace('/cook', '')) : show(CookMode.i + 1);
    document.getElementById('prev').onclick = () => show(CookMode.i - 1);
    document.addEventListener('keydown', (e) => { if (e.key === 'ArrowRight' || e.key === ' ') show(CookMode.i + 1); if (e.key === 'ArrowLeft') show(CookMode.i - 1); });
    let x0 = null;
    const stage = document.getElementById('stage');
    stage.addEventListener('touchstart', (e) => { x0 = e.touches[0].clientX; }, { passive: true });
    stage.addEventListener('touchend', (e) => {
      if (x0 === null) return; const dx = e.changedTouches[0].clientX - x0; x0 = null;
      if (Math.abs(dx) > 50) show(CookMode.i + (dx < 0 ? 1 : -1));
    });
    const drawer = document.getElementById('drawer'), ib = document.getElementById('ing-btn');
    ib.onclick = () => { drawer.hidden = !drawer.hidden; ib.setAttribute('aria-expanded', !drawer.hidden); };
    // keep the screen awake while cooking
    const lock = async () => { try { CookMode.wake = await navigator.wakeLock?.request('screen'); } catch {} };
    lock(); document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') lock(); });
    // read aloud
    const sp = document.getElementById('speak');
    if (!window.speechSynthesis) sp.hidden = true;
    sp.onclick = () => { CookMode.speaking = !CookMode.speaking; sp.setAttribute('aria-pressed', CookMode.speaking); sp.textContent = CookMode.speaking ? '🔊' : '🔈'; CookMode.speaking ? CookMode.say() : speechSynthesis.cancel(); };
    document.addEventListener('voicechange', () => { if (CookMode.speaking) CookMode.say(); });
    // voice commands where the browser supports them
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const li = document.getElementById('listen');
    if (SR) {
      li.hidden = false;
      li.onclick = () => {
        if (CookMode.rec) { CookMode.rec.onend = null; CookMode.rec.stop(); CookMode.rec = null; li.setAttribute('aria-pressed', false); li.classList.remove('on'); return; }
        const rec = new SR(); rec.continuous = true; rec.lang = 'en-US';
        rec.onresult = (ev) => {
          const said = ev.results[ev.results.length - 1][0].transcript.toLowerCase();
          if (/next|done|continue/.test(said)) show(CookMode.i + 1);
          else if (/back|previous/.test(said)) show(CookMode.i - 1);
          else if (/repeat|again|what/.test(said)) CookMode.say(true);
          else if (/timer/.test(said)) { const t = steps[CookMode.i].querySelector('.timer'); if (t) Timers.start(t); }
        };
        rec.onend = () => { if (CookMode.rec) rec.start(); };
        rec.start(); CookMode.rec = rec; li.setAttribute('aria-pressed', true); li.classList.add('on');
        toast('Listening: say "next", "back", "repeat" or "timer"', 3000);
      };
    }
    show(0);
  },
  say(force) {
    if (!window.speechSynthesis) return;
    speechSynthesis.cancel();
    const step = document.querySelectorAll('.cook-step')[CookMode.i];
    const p = step.querySelector(Voice.get() === 'unhinged' ? '.v-unh' : '.v-std');
    if (p) speechSynthesis.speak(new SpeechSynthesisUtterance(p.textContent.trim()));
  },
};

/* ---------- global wiring ---------- */
document.addEventListener('click', (e) => {
  const add = e.target.closest('[data-add]');
  if (add) { e.preventDefault(); Cart.add(+add.dataset.add, add.dataset.name, +add.dataset.servings || null); add.classList.add('added'); return; }
  const v = e.target.closest('[data-voice]');
  if (v) { Voice.set(v.dataset.voice); return; }
  const th = e.target.closest('[data-vote]');
  if (th) { vote(th.closest('[data-recipe]'), +th.dataset.vote); return; }
  const ex = e.target.closest('[data-example]');
  if (ex) { const ta = document.querySelector('.mood textarea'); ta.value = ex.dataset.example; ta.form.submit(); return; }
  const t = e.target.closest('.timer');
  if (t) { Timers.start(t); return; }
  if (e.target.id === 'add-all') {
    let n = 0; document.querySelectorAll('.card [data-add]').forEach(b => { if (Cart.add(+b.dataset.add, b.dataset.name, +b.dataset.servings || null, true)) n++; });
    toast(`Added ${n} recipes`);
  }
});
Voice.paint();
Cart.badge();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
