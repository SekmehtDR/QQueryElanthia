// Runs the page's start-screen list with a fake browser and a fake server, and prints each chip with its fire, if any.
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const page = html.slice(html.indexOf('<script>'));
const a = page.indexOf('const ASKED_MORE_MAX');
const b = page.indexOf('async function loadPersona(', a);
const src = page.slice(a, b);
function element() {
  const el = { children: [], textContent: '', attrs: {}, classList: { remove() {} }, style: { setProperty(k, v) { el.attrs[k] = v; } } };
  el.append = (...kids) => el.children.push(...kids);
  el.prepend = (...kids) => el.children.unshift(...kids);
  el.setAttribute = (k, v) => { el.attrs[k] = v; };
  return el;
}
const document = { createElement: element, getElementById: () => null, documentElement: { dataset: { view: 'desktop' } } };
const fetch = async () => ({
  ok: true,
  json: async () => ({ questions: ['Who are the dragon priests?', 'What metals are yellow?', 'Where is Shard?'], hot: ['Who are the dragon priests?'],
                       counts: { 'Who are the dragon priests?': 4, 'What metals are yellow?': 25 } }),
});
const loadRecent = new Function('document', 'fetch', 'prefill', src + ';return loadRecent;')(document, fetch, () => {});
const box = element();
loadRecent(box).then(() => {
  const chips = box.children.find(c => c.className === 'chips').children;
  process.stdout.write(JSON.stringify(chips.map(c => {
    const fire = c.children.find(k => k.className === 'hot');
    const times = c.children.find(k => k.className === 'times');
    return { q: c.textContent, fire: fire ? fire.textContent : null, times: times.textContent, more: c.attrs['--asked-more'], first: c.children[0].className };
  })));
}, e => { console.log('THREW ' + e.message); process.exit(1); });
