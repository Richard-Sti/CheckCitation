// Offline checks for the UI script in review.html. Run: node test_review.js
//
// The page's <script> is a flat run of declarations with no DOM access until the
// wiring at the very end, so that prefix runs in a vm context against a fake
// document. Renders build innerHTML strings and appendChild them, which is what
// makes the output assertable without a browser.
//
// `const` and `let` at a script's top level live in the context's lexical scope,
// not on the sandbox object, so everything is reached by evaluating an expression
// inside the context rather than as a property of it.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(`${__dirname}/review.html`, 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // The whole page script must at least parse.

// Everything above the first top-level DOM statement. The marker is anchored to
// the start of a line, because tab() uses VIEWS.forEach too.
const BODY = script.slice(0, script.indexOf('\nVIEWS.forEach'));
assert.ok(BODY.includes('function render()'), 'the slice marker moved');

function el() {
  return {
    value: 'default', innerHTML: '', textContent: '', hidden: false, disabled: false,
    dataset: {}, rendered: [], classList: {toggle() {}},
    appendChild(node) { this.rendered.push(node.innerHTML); },
    setAttribute() {}, querySelectorAll: () => [], querySelector: () => null,
  };
}

function page(extra = {}) {
  const nodes = new Proxy({}, {get: (store, id) => store[id] || (store[id] = el())});
  const ctx = vm.createContext({
    document: {getElementById: id => nodes[id], createElement: () => el(), querySelectorAll: () => []},
    localStorage: {getItem: () => null, setItem() {}},
    addEventListener() {}, console, ...extra,
  });
  vm.runInContext(BODY, ctx);
  const read = expr => {
    const value = vm.runInContext(expr, ctx);
    // Objects that cross the vm boundary keep the context's prototypes, and
    // deepStrictEqual compares those, so a plain array never equals a plain
    // array. A JSON round-trip brings them home; promises pass through.
    return value && typeof value === 'object' && typeof value.then !== 'function'
      ? JSON.parse(JSON.stringify(value)) : value;
  };
  const call = (fn, ...args) => read(`${fn}(${args.map(a => JSON.stringify(a)).join(', ')})`);
  const set = source => vm.runInContext(source, ctx);
  return {nodes, read, call, set};
}

function entry(over = {}) {
  return {
    key: 'Smith2020', kind: 'ARTICLE', line: 1, raw: '@ARTICLE{Smith2020, title = {A}}',
    status: 'ADS_BIBTEX_MISMATCH', message: 'local BibTeX differs', query: '',
    issue: 'fields differ from the current ADS export', action: 'use --replace',
    local: {title: 'A study', author: 'Smith, J.', year: '2020', doi: '', eprint: ''},
    bibcode: '2020ApJ...900....1S', ads: null, ads_bibtex: '@ARTICLE{Smith2020, title = {A}}',
    conflicts: [], matches: [], candidate: '2020ApJ...900....1S', search_url: '',
    auto: true, manual: false, issueish: true, skip: false, ...over,
  };
}

/* ---- pure helpers ---- */
{
  const {call} = page();
  assert.equal(call('esc', '<b>"x" & y</b>'), '&lt;b&gt;&quot;x&quot; &amp; y&lt;/b&gt;');
  assert.equal(call('tone', 'OK'), 'ok');
  assert.equal(call('tone', 'ADS_BIBTEX_MISMATCH'), 'warn');
  assert.equal(call('tone', 'ADS_RECORD_CONFLICT'), 'bad');
  assert.equal(call('tone', 'A_STATUS_ADDED_LATER'), 'bad', 'an unlisted status must read as bad, not as fine');
  assert.equal(call('label', 'NO_IDENTIFIER'), 'no identifier');
  console.log('ok: escaping, and an unknown status is never styled as clean');
}

{
  const {call} = page();
  const rows = [
    entry({key: 'A2020'}), entry({key: 'B2019'}), entry({key: 'C2018'}),
    entry({key: 'D2017', issueish: false}),
  ];
  assert.deepEqual(call('queueKeys', rows, [], [], []), ['A2020', 'B2019', 'C2018'],
    'only entries with an issue are queued');
  assert.deepEqual(call('queueKeys', rows, ['A2020'], [], []), ['B2019', 'C2018'], 'a kept entry leaves the queue');
  assert.deepEqual(call('queueKeys', rows, [], ['A2020'], []), ['B2019', 'C2018', 'A2020'], 'a deferred entry goes last');
  assert.deepEqual(call('queueKeys', rows, [], [], ['C2018']), ['C2018', 'A2020', 'B2019'], 'a focused entry goes first');
  console.log('ok: the queue derives from status, not from remembered clicks');
}

{
  const {call} = page();
  const rows = [entry({key: 'B2019', line: 9}), entry({key: 'A2020', line: 2})];
  assert.deepEqual(call('sortRows', rows, 'key:asc').map(r => r.key), ['A2020', 'B2019']);
  assert.deepEqual(call('sortRows', rows, 'line:asc').map(r => r.line), [2, 9]);
  assert.deepEqual(call('sortRows', rows, 'line:desc').map(r => r.line), [9, 2]);
  assert.deepEqual(call('sortRows', rows, 'default').map(r => r.key), ['B2019', 'A2020']);
  assert.deepEqual(rows.map(r => r.key), ['B2019', 'A2020'], 'sorting must not reorder the source list');
  console.log('ok: sorting is a copy and honours every mode');
}

{
  const {call} = page();
  const issue = entry();
  const clean = entry({status: 'OK', issueish: false});
  const skipped = entry({status: 'SKIPPED', issueish: false, skip: true});
  assert.ok(call('matchesFilter', issue, 'issues') && !call('matchesFilter', clean, 'issues'));
  assert.ok(call('matchesFilter', clean, 'ok') && !call('matchesFilter', issue, 'ok'));
  assert.ok(call('matchesFilter', skipped, 'skip') && !call('matchesFilter', clean, 'skip'));
  assert.ok([issue, clean, skipped].every(e => call('matchesFilter', e, 'all')));
  console.log('ok: every filter is exclusive and "all" hides nothing');
}

/* ---- the diff the CLI prints before it will accept a risky replacement ---- */
{
  const {call} = page();
  assert.equal(call('diffTable', entry()), '', 'no ADS export means nothing to compare');
  const table = call('diffTable', entry({
    ads: {title: 'A study, Paper II', author: 'Smith, J.', year: '2020', doi: '', eprint: ''},
    conflicts: ['title'],
  }));
  assert.match(table, /<tr class="clash"><td>title<\/td>/, 'the clashing field must be marked');
  assert.match(table, /<tr class=""><td>author<\/td>/, 'an agreeing field must not be');
  assert.ok(!table.includes('<td>doi</td>'), 'a field neither side has is not a row');
  const hostile = call('diffTable', entry({
    ads: {title: '<img onerror=x>', author: '', year: '', doi: '', eprint: ''}, conflicts: [],
  }));
  assert.ok(hostile.includes('&lt;img onerror=x&gt;') && !hostile.includes('<img'), 'ADS text must be escaped');
  console.log('ok: the local/ADS diff marks conflicts and escapes ADS text');
}

/* ---- the risk gate: this is the one that can overwrite a citation ---- */
{
  const {call, set} = page();
  set('drafts = {}; fetchedAuto = {}; confirming = null; history = [];');
  const safe = call('cardBody', entry());
  assert.match(safe, /class="btn good" data-apply/, 'a vetted export gets the plain Replace button');
  assert.match(safe, /l replace/, 'and keeps the keyboard');

  const risky = call('cardBody', entry({auto: false, status: 'ADS_RECORD_CONFLICT', conflicts: ['title']}));
  assert.match(risky, /class="btn danger" data-apply/, 'a possible different paper gets the danger button');
  assert.match(risky, /needs the button/, 'and says the keyboard will not do it');
  assert.ok(!risky.includes('l replace'), 'the replace key must not be advertised on a risky card');

  set("confirming = 'Smith2020';");
  assert.match(call('cardBody', entry({auto: false})), /data-confirm="Smith2020"/,
    'the inline confirm stands in for the CLI\'s typed "replace"');
  console.log('ok: a risky candidate is never offered as one keypress');
}

{
  const {call, set} = page();
  set('drafts = {}; fetchedAuto = {}; confirming = null; history = [];');
  const lazy = call('cardBody', entry({ads_bibtex: '', status: 'IDENTIFIER_MISMATCH', auto: false}));
  assert.match(lazy, /data-fetch="Smith2020" data-bibcode="2020ApJ\.\.\.900\.\.\.\.1S"/,
    'an ADS candidate with no export yet must be fetchable');
  assert.match(lazy, /data-apply="Smith2020" disabled/, 'and Replace stays disabled until there is text');

  set("drafts = {Smith2020: '@ARTICLE{Smith2020, title = {pasted}}'};");
  const pasted = call('cardBody', entry({ads_bibtex: '', auto: false}));
  assert.ok(pasted.includes('pasted'), 'a draft outranks the proposal');
  assert.ok(!/data-apply="Smith2020" disabled/.test(pasted), 'and enables Replace');
  console.log('ok: a lazily fetched or pasted replacement drives the buttons');
}

/* ---- the handoff, for entries no route resolved ---- */
{
  const {call} = page();
  assert.equal(call('handoff', entry()), '', 'an entry with a candidate is not a handoff');
  const dead = call('handoff', entry({
    status: 'MISSING', candidate: '', ads_bibtex: '', matches: [],
    search_url: 'https://ui.adsabs.harvard.edu/search/q=title%3A(a+study)',
  }));
  assert.match(dead, /No ADS route matched/);
  assert.match(dead, /href="https:\/\/ui\.adsabs\.harvard\.edu\/search/, 'and it must be clickable');
  assert.match(dead, /then paste the BibTeX below/, 'and say what to do next');

  const links = call('matchList', entry({matches: [
    {bibcode: '2020ApJ...900....1S', year: '2020', title: 'A study', doi: '',
     url: 'https://ui.adsabs.harvard.edu/abs/2020ApJ...900....1S/abstract'},
  ]}));
  assert.match(links, /<a href="https:\/\/ui\.adsabs\.harvard\.edu\/abs\/[^"]+"[^>]*><code>2020ApJ/,
    'every candidate links to its ADS abstract');
  console.log('ok: a dead end hands over to ADS instead of showing an empty card');
}

/* ---- the All table ---- */
{
  const {nodes, call, set} = page();
  set(`entries = ${JSON.stringify([
    entry({key: 'A2020', line: 1}),
    entry({key: 'B2019', line: 9, status: 'OK', issueish: false}),
  ])}; filter = 'issues';`);
  call('renderAll');
  assert.equal(nodes['all-rows'].rendered.length, 1, 'the issues filter must hide the clean entry');
  assert.match(nodes['all-rows'].rendered[0], /<code>A2020<\/code>/);
  assert.match(nodes['all-rows'].rendered[0], /data-review="A2020"/, 'an issue row offers a way back into the stack');

  nodes['all-rows'].rendered = [];
  set("filter = 'all';");
  call('renderAll');
  assert.equal(nodes['all-rows'].rendered.length, 2);
  assert.ok(!nodes['all-rows'].rendered[1].includes('data-review'), 'a clean entry has nothing to review');
  console.log('ok: the All view filters, and offers Review only where it means something');
}

/* ---- concurrency: If-Match, 409, recovery ---- */
(async () => {
  const sent = [];
  const {nodes, read, set} = page({
    fetch: async (url, options = {}) => {
      sent.push({url, options});
      if (url === '/api/state') return {ok: true, status: 200, json: async () => ({revision: 'r2'})};
      return {ok: false, status: 409, json: async () => ({error: 'The .bib changed since this tab loaded it.'})};
    },
  });
  set("revision = 'r1'; stale = false;");
  const conflict = await read("apiFetch('/api/replace', {method: 'POST', body: '{}'})");
  assert.equal(conflict.status, 409);
  assert.equal(sent[0].options.headers['If-Match'], 'r1', 'a write carries the last confirmed revision');
  assert.equal(nodes['btn-reload'].hidden, false, 'a 409 must surface the reload button');

  await assert.rejects(() => read("apiFetch('/api/replace', {method: 'POST', body: '{}'})"),
    /Reload from disk/, 'a stale tab must refuse to write again');
  assert.equal(sent.length, 1, 'and must not reach the server at all');

  await read("apiFetch('/api/state')");
  assert.equal(nodes['btn-reload'].hidden, true, 'a fresh read clears the stale flag');
  await read("apiFetch('/api/recheck', {method: 'POST', body: '{}'})");
  assert.equal(sent[sent.length - 1].options.headers['If-Match'], 'r2', 'the next write uses the new revision');
  console.log('ok: If-Match, 409 and recovery');
})().catch(e => { console.error(e); process.exitCode = 1; });

/* ---- drafts ---- */
(async () => {
  const stored = {};
  const {nodes, read, set} = page({
    localStorage: {getItem: k => stored[k] ?? null, setItem: (k, v) => { stored[k] = v; }},
    fetch: async () => ({ok: false, status: 400, json: async () => ({error: 'replacement must be exactly one entry'})}),
  });
  set(`source = 'ref.bib'; revision = 'r1'; entries = ${JSON.stringify([entry()])};`);
  set("drafts = {Smith2020: 'typed'}; persistDrafts();");
  assert.equal(JSON.parse(stored['ads-review-drafts:ref.bib']).Smith2020, 'typed', 'drafts are mirrored per file');

  await read("replace('Smith2020', 'typed')");
  assert.equal(read('drafts.Smith2020'), 'typed', 'a rejected save must not drop what was typed');
  assert.match(nodes.note.textContent, /exactly one entry/, 'and the server sentence is what the page shows');
  console.log('ok: a failed replacement keeps the draft and reports why');
})().catch(e => { console.error(e); process.exitCode = 1; });

{
  const {read, call, set} = page({
    localStorage: {getItem: () => '{not json', setItem() { throw new Error('damaged storage was overwritten'); }},
  });
  set("source = 'ref.bib';");
  call('readDrafts');
  assert.deepEqual(read('drafts'), {});
  assert.equal(read('blockedDrafts'), true);
  call('persistDrafts'); // must not throw
  console.log('ok: damaged draft storage is reported, not overwritten');
}

/* ---- the keyboard cannot apply a risky replacement ---- */
(async () => {
  let called = 0;
  const {nodes, read, call, set} = page({
    fetch: async () => { called += 1; return {ok: true, status: 200, json: async () => ({})}; },
  });
  set(`entries = ${JSON.stringify([entry({auto: false, status: 'ADS_RECORD_CONFLICT', conflicts: ['title']})])};`);
  set('dismissed = []; deferred = []; focus = []; drafts = {}; fetchedAuto = {}; busy = false;');
  call('act', 'replace');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(called, 0, 'the replace key must not write a risky candidate');
  assert.match(nodes.note.textContent, /use the button/);

  set('fetchedAuto = {Smith2020: true};');
  assert.match(read('cardBody(entries[0])'), /class="btn good"/,
    'once the server vets a fetched export it is one keypress again');
  console.log('ok: the keyboard respects the server\'s risk verdict');
})().catch(e => { console.error(e); process.exitCode = 1; });

/* ---- keep and defer never touch the file ---- */
(async () => {
  let called = 0;
  const {read, call, set} = page({fetch: async () => { called += 1; return {ok: true, status: 200, json: async () => ({})}; }});
  set(`entries = ${JSON.stringify([entry({key: 'A2020'}), entry({key: 'B2019'})])};`);
  set('dismissed = []; deferred = []; focus = []; drafts = {}; fetchedAuto = {}; busy = false;');
  call('act', 'keep');
  assert.deepEqual(read('dismissed'), ['A2020']);
  call('act', 'defer');
  assert.deepEqual(read('deferred'), ['B2019']);
  assert.deepEqual(read('queueKeys(entries, dismissed, deferred, focus)'), ['B2019'], 'only the deferred one is left');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(called, 0, 'keeping and deferring are page-local, never a write');
  console.log('ok: keep and defer never reach the server');
})().catch(e => { console.error(e); process.exitCode = 1; });

/* ---- a full render must not throw on any shape ---- */
{
  const {nodes, call, set} = page();
  set(`entries = ${JSON.stringify([entry(), entry({key: 'S1939', status: 'SKIPPED', issueish: false, skip: true})])};`);
  set("dupes = [{bibcode: '2020X', title: 'T', keys: ['A2020', 'B2019']}];");
  set("warns = [{key: 'K2020', line: 4, issue: 'literal et al.'}];");
  set("tex = {sources: ['paper.tex'], unreadable: [], undefined: ['Missing2001'], uncited: ['S1939']};");
  set('replaced = 2; skipped = 1; history = []; drafts = {}; fetchedAuto = {}; confirming = null;');
  call('render');
  assert.match(nodes['dupe-rows'].innerHTML, /2020X/);
  assert.match(nodes['warn-rows'].innerHTML, /K2020/);
  assert.match(nodes['tex-rows'].innerHTML, /Missing2001/);
  assert.match(nodes['tex-rows'].innerHTML, /S1939/);
  assert.equal(nodes['s-replaced'].textContent, 2);
  assert.equal(nodes['n-check'].textContent, 3, 'the tab badge counts duplicates, warnings and undefined keys');
  console.log('ok: a full render covers every cross-check section');
}
