// Runs the Index panel's code with a fake browser: a syntax check cannot see an undefined name, and this can.
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const state = process.argv[3];
const page = html.slice(html.indexOf('<script>'));
const a = page.indexOf('// ---- Index panel:');
const b = page.indexOf('// The index details are hidden', a);
const src = page.slice(a, b);

const elements = {};
function element(id) {
  const el = { id, children: [], textContent: '', innerHTML: '', hidden: false, disabled: false, dataset: {}, style: {}, value: '',
    listeners: {}, scrollTop: 0, scrollHeight: 0 };
  el.classList = { names: new Set(), toggle(n, on) { if (on) this.names.add(n); else this.names.delete(n); }, contains(n) { return this.names.has(n); } };
  el.append = (...kids) => el.children.push(...kids);
  el.addEventListener = (name, fn) => { el.listeners[name] = fn; };
  return el;
}
const document = {
  activeElement: null,
  createElement: () => element(''),
  getElementById: id => { elements[id] = elements[id] || element(id); return elements[id]; },
};
const posts = [];
const fetchFake = async (url, opts) => {
  if (opts && opts.method === 'POST') posts.push(url + ' ' + opts.body);
  return { ok: true, json: async () => ({ namespaces: [], total: 1, fetched: 1, indexed: 1, errors: 0, crawling: false, complete: true }) };
};
const esc = s => String(s);
const timeAgo = () => '6 minutes ago';
const noTimer = () => 1;
const build = new Function('document', 'fetch', 'esc', 'timeAgo', 'confirm', 'prompt', 'setInterval', 'clearInterval', src + ';return { renderIndex, refreshStatus, drawNumbers };');
const panel = build(document, fetchFake, esc, timeAgo, () => true, () => null, noTimer, () => {});

const base = {
  chunks: 10, pending_crawl: 0, pending_index: 0, reindex: { state: 'finished', pct: 100, finished_at: '2026-10-24T02:15:20Z' }, source: 'mirror', delta_source: 'original',
  last_delta: { id: 3, at: '2026-11-02T17:07:36Z', changed: 2, deleted: 0, errors: 0 }, last_full: null, newest_edit_seen: '2026-11-02T17:03:32Z',
  auto_sync: { minutes: 60, next: '2999-01-01T00:00:00Z' }, history_edits: 15, history_stored: 500, history_pages_waiting: 0, knowledge: { facts: 5, hubs: 2 },
  job: { running: false, log: [] }, verdict: { level: 'ok', title: 'Everything is up to date', detail: '', action: null },
};
const states = {
  well: base,
  running: { ...base, job: { running: true, label: 'Check for changes', mode: 'delta', elapsed_s: 42, started_at: 't', log: ['one', 'two'] },
    live: { fetched: 4, indexed: 3, latest: [{ title: 'Tideskin' }] }, verdict: { level: 'working', title: 'Check for changes is running', detail: '', action: null } },
  empty: { ...base, chunks: 0, pending_index: 9, verdict: { level: 'bad', title: 'The search index is empty', detail: 'x', action: { mode: 'reindex', label: 'Rebuild search index' } } },
  doctor: { ...base, job: { running: false, label: 'Health check', mode: 'doctor', started_at: 't', finished_at: '2026-11-02T17:07:36Z', exit_code: 1, log: ['FAIL thing'] },
    verdict: { level: 'bad', title: 'The health check found a problem', detail: 'The results are below.', action: { mode: 'doctor', label: 'Check again' } } },
};
const check = (name, ok) => console.log((ok ? 'ok ' : 'BAD ') + name);
const el = id => document.getElementById(id);

panel.renderIndex(states[state]);
panel.refreshStatus().then(async () => {
  check('says it', el('idxvtitle').children.length === 2 && el('idxverdict').dataset.level === states[state].verdict.level);
  check('numbers drawn', el('idxinfo').innerHTML.includes('Elanthipedia') && el('idxinfo').innerHTML.includes('the mirror'));
  if (state === 'well') {
    check('routine check is the main button', !el('idxfix').hidden === false && el('idxdelta').classList.contains('idx-primary') && !el('idxdelta').disabled);
    check('nothing to stop', el('idxstop').hidden);
    check('last check said', el('idxvsub').innerHTML.includes('Last checked 6 minutes ago') && el('idxvsub').innerHTML.includes('data-run="3"'));
    check('nothing waiting, nothing to get', el('idxfill').disabled);
    check('settings filled', el('autom').value === 60 && el('histn').value === 15);
    el('idxdelta').listeners.click();
    await new Promise(r => setTimeout(r, 0));
    check('button starts the job', posts.some(p => p.includes('/api/index/start') && p.includes('"delta"')));
  }
  if (state === 'running') {
    check('can only stop', !el('idxstop').hidden && el('idxdelta').hidden && el('idxdoctor').disabled && el('idxclear').disabled);
    check('shows progress', el('idxvsub').innerHTML.includes('00:42') && el('idxvsub').innerHTML.includes('Tideskin'));
    check('shows its log', !el('idxlogbox').hidden && el('idxlog').textContent.includes('two'));
  }
  if (state === 'empty') {
    check('offers the fix first', !el('idxfix').hidden && el('idxfix').textContent === 'Rebuild search index' && !el('idxdelta').classList.contains('idx-primary'));
    check('nothing to clear', el('idxclear').disabled);
    el('idxfix').listeners.click({ target: el('idxfix') });
    await new Promise(r => setTimeout(r, 0));
    check('fix starts the rebuild, after asking', posts.some(p => p.includes('"reindex"')));
  }
  if (state === 'doctor') {
    check('results stay', !el('idxlogbox').hidden && el('idxlog').textContent.includes('FAIL thing'));
    el('idxlogclose').listeners.click();
    check('results can be closed', el('idxlogbox').hidden);
    panel.renderIndex(states[state]);
    check('and stay closed', el('idxlogbox').hidden);
  }
}).catch(e => { console.log('BAD THREW ' + e.message); process.exit(1); });
