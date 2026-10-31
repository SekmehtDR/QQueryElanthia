// Runs the page's sending function as far as fetch, with a fake browser. Every name the page's script declares is faked and
// nothing else, so a name declared nowhere still throws, which `node --check` cannot see (it stopped every question in 1.6.0).
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const page = html.slice(html.indexOf('<script>'));
const a = page.indexOf('async function runAsk(');
const b = page.indexOf('// A clarifying question', a);
const src = page.slice(a, b);
const stub = new Proxy(function () {}, {
  get: (t, k) => (k === 'then' ? undefined : k === Symbol.toPrimitive ? () => '' : stub),
  apply: () => stub, construct: () => stub, set: () => true,
});
const declared = new Set([...page.matchAll(/\b(?:function|const|let|var)\s+([A-Za-z_$][\w$]*)/g)].map(m => m[1]));
for (const m of page.matchAll(/\b(?:const|let|var)\s+\{?([^=;]+?)\}?\s*=/g)) {
  m[1].split(',').forEach(n => { const x = n.trim(); if (/^[A-Za-z_$][\w$]*$/.test(x)) declared.add(x); });
}
const own = new Set([...src.matchAll(/\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g)].map(m => m[1]));
const fakes = [...declared].filter(n => !own.has(n) && !['runAsk', 'pin', 'fetch', 'text', 'signal'].includes(n));
const setup = fakes.map(n => `var ${n}=stub;`).join('')
  + 'var sessionId="s1";var viewingVisitor=false;var document=stub;var requestAnimationFrame=stub;var cancelAnimationFrame=stub;';
let sent = null;
const run = new Function('stub', 'fetch', setup + src + ';return runAsk;')(stub, async (u, o) => {
  sent = JSON.parse(o.body);
  throw new Error('stop after the send');
});
run('hello', { aborted: false }, 7).then(() => {
  console.log(sent ? 'REACHED SEND ' + JSON.stringify(sent) : 'DID NOT SEND');
  process.exit(0);
}, e => { console.log('THREW ' + e.message); process.exit(1); });
setTimeout(() => { console.log('TIMED OUT'); process.exit(2); }, 5000);
