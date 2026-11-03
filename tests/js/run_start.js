// Runs the start screen's code with a fake browser: a syntax check cannot see an undefined name, and this can.
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const page = html.slice(html.indexOf('<script>'));
const a = page.indexOf('function hintHtml(text) {');
const b = page.indexOf('async function loadPersona() {', a);
const src = page.slice(a, b);

function element(tag) {
  const el = { tag, children: [], className: '', hidden: false, attrs: {}, open: false, title: '' };
  let words = '';
  Object.defineProperty(el, 'textContent', {  // as in a browser: setting it replaces the children
    get: () => words + el.children.map(k => k.textContent).join(''),
    set: v => { words = v; el.children = []; },
  });
  el.append = (...kids) => el.children.push(...kids);
  el.prepend = (...kids) => el.children.unshift(...kids);
  el.setAttribute = (k, v) => { el.attrs[k] = v; };
  el.getAttribute = k => (k in el.attrs ? el.attrs[k] : null);
  el.remove = () => { el.removed = true; };
  el.decode = () => Promise.resolve();
  el.classList = { add() {}, remove() {} };
  el.style = { setProperty(k, v) { el.attrs[k] = v; } };
  return el;
}
const asked = [];
const lately = { total: 13, since: '2026-10-19T00:00:00Z', checked: '2026-11-02T17:00:00Z', rows: [
  { title: 'Moot 458', label: 'Moot 458', status: '2 new', when: '2026-11-02T16:00:00Z', note: 'A moot.', members: [{ label: 'Shops', title: 'Moot 458/Shops' }] },
  { title: 'Abysselle fiber', label: 'Abysselle', status: '2 new', when: '2026-11-02T16:00:00Z', note: 'A fiber.', members: [{ label: 'fiber', title: 'Abysselle fiber' }, { label: 'lawn', title: 'Abysselle lawn' }] },
  ...Array.from({ length: 9 }, (_, i) => ({ title: 'Page ' + i, label: 'Page ' + i, status: 'new', when: '2026-11-01T16:00:00Z', note: '', members: [] })),
] };
const answers = {
  '/api/starters': { topics: [{ key: 'places', label: 'Places', questions: ['Where is Shard?', 'What is Aesry?'] }, { key: 'people', label: 'People', questions: ['Who is Kssarh?'] }] },
  '/api/recent-questions': { questions: ['One?', 'Two?', 'Three?', 'Four?', 'Five?', 'Six?', 'Seven?'], hot: ['Two?'] },
  '/api/lately': lately,
};
const fetchFake = async url => ({ ok: true, json: async () => answers[url] });
const documentFake = { createElement: element, getElementById: () => element('div'), documentElement: { dataset: { view: 'desktop' } } };
const persona = { enabled: true, name: 'Fennick' };
const build = new Function('document', 'fetch', 'prefill', 'persona', src + ';return hintHtml;');
const check = (name, ok) => console.log((ok ? 'ok ' : 'BAD ') + name);
const deep = (el, cls) => (el.className || '').split(' ').includes(cls) ? [el, ...el.children.flatMap(k => deep(k, cls))] : el.children.flatMap(k => deep(k, cls));

async function run(view) {
  documentFake.documentElement.dataset.view = view;
  const hint = build(documentFake, fetchFake, q => asked.push(q), persona)('Hey.');
  await hint.ready;
  return hint;
}
(async () => {
  const wide = await run('desktop');
  const topics = deep(wide, 'topic');
  check('subjects drawn', topics.map(t => t.textContent).join() === 'Places,People');
  const list = deep(wide, 'topic-qs')[0];
  check('questions wait for a click', list.hidden && !list.children.length);
  topics[0].onclick();
  check('a subject shows its questions', !list.hidden && list.children.map(c => c.textContent).join() === 'Where is Shard?,What is Aesry?');
  topics[1].onclick();
  check('one subject at a time', list.children.length === 1 && topics[0].attrs['aria-expanded'] === 'false');
  list.children[0].onclick();
  check('a question goes in the box', asked.pop() === 'Who is Kssarh?');
  topics[1].onclick();
  check('a second click folds it', list.hidden);

  const fold = deep(wide, 'lately-fold')[0];
  check('desktop opens the wiki list', fold.open === true);
  const rows = deep(wide, 'lately-row');
  check('a row for each, the extra ones hidden', rows.length === 11 && rows.filter(r => !r.hidden).length === 8);
  const [main, body] = rows[0].children;
  check('detail waits for a click', body.hidden);
  main.onclick();
  check('a click opens it', !body.hidden && main.attrs['aria-expanded'] === 'true');
  const parts = deep(rows[0], 'chip');
  parts[0].onclick();
  check('a page of its own goes in the box', asked.pop() === 'Tell me about Moot 458' && parts[0].textContent === 'Ask about this');
  parts[1].onclick();
  check('so can its subpage', asked.pop() === 'Tell me about Moot 458/Shops');
  check('a family without one offers only its members', deep(rows[1], 'lately-ask').length === 0 && deep(rows[1], 'chip').length === 2);
  const more = deep(fold, 'lately-more')[0];
  check('the button says the heading\'s count', more.textContent === 'Show all 13 pages');
  more.onclick();
  check('and shows them', rows.every(r => !r.hidden));
  check('others are asking, with a way to all of them', deep(wide, 'recent')[0].children.length === 3);

  const phone = await run('mobile');
  check('a phone folds the wiki list', deep(phone, 'lately-fold')[0].open === false);
})().catch(e => { console.log('BAD THREW ' + e.message); process.exit(1); });
