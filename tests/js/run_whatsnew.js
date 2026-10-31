// Runs the page's What's new code with fake elements: the badge opens it, and it shows Fennick, then the plain changes.
const fs = require('fs');
const page = fs.readFileSync(process.argv[2], 'utf8');
const a = page.indexOf('const whatsnew = document.getElementById');
const b = page.indexOf('// About Fennick: a page from his notes.', a);
const src = page.slice(a, b);
class El {
  constructor() { this.kids = []; this.listeners = {}; this.textContent = ''; this.innerHTML = ''; this.scrollTop = 9; this.opened = false; }
  append(...k) { this.kids.push(...k); }
  addEventListener(k, f) { this.listeners[k] = f; }
  querySelector() { return this.sheet || (this.sheet = new El()); }
  showModal() { this.opened = true; }
  close() { this.opened = false; if (this.listeners.close) this.listeners.close(); }
  focus() { this.focused = true; }
  text() { return [this.textContent, ...this.kids.map(k => k.text())].join(' ').trim(); }
}
const els = {};
const document = { getElementById: i => els[i] || (els[i] = new El()), createElement: () => new El() };
const release = { version: '9.9', date: '2030-01-01', fennick: 'I rearranged <b>things</b>.', changes: ['A plain change.'] };
const run = async (persona, ok) => {
  Object.keys(els).forEach(k => delete els[k]);
  const fetch = async () => ({ ok, status: ok ? 200 : 500, json: async () => ({ releases: [release] }) });
  new Function('document', 'fetch', 'persona', src)(document, fetch, persona);
  await els.pver.listeners.click();
  return els.whatsnewlist;
};
(async () => {
  const check = (name, ok, got) => console.log((ok ? 'ok   ' : 'BAD  ') + name + (ok ? '' : '  [' + got + ']'));
  let list = await run({ enabled: true }, true);
  const inVoice = list.text();
  check('the badge opens the window', els.whatsnew.opened);
  check('Fennick comes first, then the plain changes', inVoice.indexOf('I rearranged') > -1 && inVoice.indexOf('I rearranged') < inVoice.indexOf('(OOC) What changed') && inVoice.indexOf('(OOC) What changed') < inVoice.indexOf('A plain change.'), inVoice);
  check('text is text, never markup', list.innerHTML === '' && inVoice.includes('<b>things</b>'), inVoice);
  els.whatsnew.close();
  check('closing returns to the badge', !els.whatsnew.opened && els.pver.focused);
  list = await run({ enabled: false }, true);
  check('with the persona off only the plain part shows', !list.text().includes('I rearranged') && list.text().includes('A plain change.'), list.text());
  list = await run({ enabled: true }, false);
  check('a failed load says so', list.textContent.startsWith('Could not load'), list.textContent);
})().catch(e => { console.log('THREW ' + e.message); process.exit(1); });
