// Runs the sign-in page's script with fake elements (the page comes on stdin): what Fennick says for each outcome of /login.
const page = require('fs').readFileSync(0, 'utf8');
const src = page.slice(page.indexOf('<script>') + 8, page.indexOf('</script>'));
const check = (name, ok, got) => console.log((ok ? 'ok   ' : 'BAD  ') + name + (ok ? '' : '  [' + got + ']'));
async function attempt(fetchImpl) {
  const els = {};
  const el = () => ({ textContent: '', value: 'a code', disabled: false, listeners: {}, selected: false,
    addEventListener(k, f) { this.listeners[k] = f; }, select() { this.selected = true; } });
  const document = { getElementById: i => els[i] || (els[i] = el()) };
  const location = { reloaded: false, reload() { this.reloaded = true; } };
  new Function('document', 'fetch', 'location', src)(document, fetchImpl, location);
  await els.f.listeners.submit({ preventDefault() {} });
  return { err: els.err.textContent, reloaded: location.reloaded, disabled: els.go.disabled, selected: els.c.selected };
}
(async () => {
  let sent = null;
  let r = await attempt(async (u, o) => { sent = JSON.parse(o.body); return { ok: true, status: 200 }; });
  check('the right code goes in', r.reloaded && !r.err && sent.code === 'a code', JSON.stringify(r));
  r = await attempt(async () => ({ ok: false, status: 401 }));
  check('a wrong code says who to ask', /reach out to Sekmeht for the current valid access code/.test(r.err) && !r.reloaded, r.err);
  check('the button works again, ready to retype', !r.disabled && r.selected, JSON.stringify(r));
  r = await attempt(async () => ({ ok: false, status: 429 }));
  check('too many tries asks them to rest', /rest a few minutes/.test(r.err) && /Sekmeht/.test(r.err), r.err);
  r = await attempt(async () => { throw new Error('offline'); });
  check('a lost connection is not called a wrong code', /try again/.test(r.err) && !/Sekmeht/.test(r.err) && !r.disabled, r.err);
})().catch(e => { console.log('THREW ' + e.message); process.exit(1); });
