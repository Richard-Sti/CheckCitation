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
    auto: true, manual: false, issueish: true, accepted: false, identical: false, skip: false, ...over,
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
  assert.deepEqual(call('queueKeys', rows, [], [], {}), ['A2020', 'B2019', 'C2018'],
    'only entries with an issue are queued');
  assert.deepEqual(call('queueKeys', rows, ['A2020'], [], {}), ['B2019', 'C2018', 'A2020'], 'a deferred entry goes last');
  assert.deepEqual(call('queueKeys', rows, [], ['C2018'], {}), ['C2018', 'A2020', 'B2019'], 'a focused entry goes first');
  assert.deepEqual(call('queueKeys', rows, [], [], {B2019: '@ARTICLE{B2019, title = {new}}'}), ['A2020', 'C2018'],
    'a staged entry is decided, even though nothing is written yet');

  const checked = rows.map(r => r.key === 'B2019' ? {...r, accepted: true} : r);
  assert.deepEqual(call('queueKeys', checked, [], [], {}), ['A2020', 'C2018'],
    'an entry accepted on a previous run is never raised again');
  assert.deepEqual(call('queueKeys', checked, [], ['B2019'], {}), ['B2019', 'A2020', 'C2018'],
    'unless you deliberately pull it back in');
  console.log('ok: the queue honours acceptance that outlived the tab');
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
  const done = entry({accepted: true});
  assert.ok(call('matchesFilter', done, 'accepted') && !call('matchesFilter', issue, 'accepted'));
  assert.ok(!call('matchesFilter', done, 'issues'), 'an accepted entry is no longer outstanding');
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
  assert.match(safe, /l stage/, 'and keeps the keyboard');

  const risky = call('cardBody', entry({auto: false, status: 'ADS_RECORD_CONFLICT', conflicts: ['title']}));
  assert.match(risky, /class="btn danger" data-apply/, 'a possible different paper gets the danger button');
  assert.match(risky, /needs the button/, 'and says the keyboard will not do it');
  assert.ok(!risky.includes('l stage'), 'the stage key must not be advertised on a risky card');

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
  assert.match(lazy, /data-apply="Smith2020"\s+disabled/, 'and Stage stays disabled until there is text');

  set("drafts = {Smith2020: '@ARTICLE{Smith2020, title = {pasted}}'};");
  const pasted = call('cardBody', entry({ads_bibtex: '', auto: false}));
  assert.ok(pasted.includes('pasted'), 'a draft outranks the proposal');
  assert.ok(!/data-apply="Smith2020"\s+disabled/.test(pasted), 'and enables Stage');
  console.log('ok: a lazily fetched or pasted replacement drives the buttons');
}

/* ---- the entry as it stands, which is what the proposal would overwrite ---- */
{
  const {call, set} = page();
  set('drafts = {}; fetchedAuto = {}; confirming = null; history = [];');
  const raw = '@ARTICLE{Springel2005,\n  title = {GADGET-2},\n  doi = {10.9999/typo}\n}';
  const body = call('cardBody', entry({raw, line: 12}));
  assert.match(body, /Current entry, line 12/);
  assert.match(body, /<pre class="bib">@ARTICLE\{Springel2005,/, 'the old key must be visible, not just the new one');
  assert.match(body, /10\.9999\/typo/, 'and the field being replaced');
  assert.ok(body.indexOf('Current entry') < body.indexOf('Proposed replacement'),
    'the current entry comes before what would replace it');
  const hostile = call('cardBody', entry({raw: '@ARTICLE{X, note = {<img onerror=x>}}'}));
  assert.ok(!hostile.includes('<img'), 'the file\'s own text is escaped too');
  console.log('ok: the card shows the entry it would overwrite, old key included');
}

/* ---- undo must tell the server it is an undo ---- */
(async () => {
  const sent = [];
  const {read, call, set} = page({
    fetch: async (url, options = {}) => {
      sent.push(JSON.parse(options.body));
      return {ok: true, status: 200, json: async () => ({
        revision: 'r2', entries: [], counts: [], duplicates: [], warnings: [], tex: null,
        replaced: 0, skipped: 0, source: 'ref.bib', notice: 'Smith2020 restored to what was in the file',
      })};
    },
  });
  set(`entries = ${JSON.stringify([entry()])}; revision = 'r1'; busy = false; drafts = {};`);
  set("history = [{key: 'Smith2020', raw: '@ARTICLE{Smith2020, title = {old}}'}];");
  await read('undo(0)');
  assert.equal(sent.length, 1);
  assert.equal(sent[0].undo, true, 'without this the server records the restored entry as checked');
  assert.equal(sent[0].bibtex, '@ARTICLE{Smith2020, title = {old}}', 'undo sends the text it is restoring');
  assert.deepEqual(read('history'), [], 'and the entry leaves the undo list');
  console.log('ok: undo restores the old text and withdraws the acceptance');
})().catch(e => { console.error(e); process.exitCode = 1; });

(async () => {
  // A refused undo must not take the only record of the previous text with it.
  const {nodes, read, set} = page({
    fetch: async () => ({ok: false, status: 409, json: async () => ({error: 'The .bib changed since this tab loaded it.'})}),
  });
  set(`entries = ${JSON.stringify([entry()])}; revision = 'r1'; busy = false; drafts = {}; stale = false;`);
  set("history = [{key: 'Smith2020', raw: '@ARTICLE{Smith2020, title = {old}}'}];");
  await read('undo(0)');
  assert.equal(read('history').length, 1, 'the undo record survived a refused write');
  assert.equal(read('history')[0].raw, '@ARTICLE{Smith2020, title = {old}}', 'and still holds the old text');
  assert.match(nodes.note.textContent, /changed since this tab/);
  console.log('ok: a refused undo keeps the text it was going to restore');
})().catch(e => { console.error(e); process.exitCode = 1; });

/* ---- the stats are navigation, and the clear button appears only when useful ---- */
{
  const {nodes, read, call, set} = page();
  set(`entries = ${JSON.stringify([
    entry({key: 'A2020'}),
    entry({key: 'B2019', status: 'OK', issueish: false}),
    entry({key: 'C2018', accepted: true}),
  ])}; replaced = 0; skipped = 0; history = []; drafts = {}; fetchedAuto = {}; confirming = null;`);
  call('render');
  assert.equal(nodes['s-ok'].textContent, 1);
  assert.equal(nodes['s-issues'].textContent, 1, 'an accepted entry is not still outstanding');
  assert.equal(nodes['s-accepted'].textContent, 1);
  assert.equal(nodes['btn-clear'].hidden, false, 'there is something to clear');

  nodes['all-rows'].rendered = [];
  call('setFilter', 'accepted');
  assert.equal(read('filter'), 'accepted');
  assert.equal(nodes['all-rows'].rendered.length, 1, 'and the All view follows the filter');
  assert.match(nodes['all-rows'].rendered[0], /C2018/, 'showing the one that was decided');

  set(`entries = ${JSON.stringify([entry({key: 'A2020'})])};`);
  call('render');
  assert.equal(nodes['btn-clear'].hidden, true, 'nothing decided, nothing to clear');
  assert.equal(nodes['clear-confirm'].hidden, true, 'and the confirm cannot be left open');
  console.log('ok: stats drive the filter, and Clear decisions hides when there is nothing to clear');
}

/* ---- staging decides; Write is the only thing that touches the file ---- */
(async () => {
  const sent = [];
  const {nodes, read, call, set} = page({
    fetch: async (url, options = {}) => {
      sent.push({url, body: JSON.parse(options.body)});
      return {ok: true, status: 200, json: async () => ({
        revision: 'r2', entries: [], counts: [], duplicates: [], warnings: [], tex: null,
        replaced: 2, skipped: 0, source: 'ref.bib', notice: 'Wrote 2 changes to ref.bib; backup at ref.bib.bak',
      })};
    },
  });
  const rows = [
    entry({key: 'A2020', auto: true, raw: '@ARTICLE{A2020, title = {old}}', ads_bibtex: '@ARTICLE{A2020, title = {new}}'}),
    entry({key: 'B2019', auto: true, raw: '@ARTICLE{B2019, title = {old}}', ads_bibtex: '@ARTICLE{B2019, title = {new}}'}),
  ];
  set(`entries = ${JSON.stringify(rows)}; source = 'ref.bib'; filePath = '/p/ref.bib'; revision = 'r1'; busy = false;`);
  set("deferred = []; focus = []; drafts = {}; staged = {}; fetchedAuto = {}; vetted = {}; history = []; confirming = null; blockedDrafts = true;");

  call('stage', 'A2020', '@ARTICLE{A2020, title = {new}}');
  call('stage', 'B2019', '@ARTICLE{B2019, title = {new}}');
  assert.equal(sent.length, 0, 'staging must not touch the file');
  assert.deepEqual(read('stagedKeys(staged, entries)'), ['A2020', 'B2019']);
  assert.deepEqual(read('queueKeys(entries, deferred, focus, staged)'), [], 'a staged entry is decided');
  assert.match(nodes.source.textContent, /2 staged, not written/);
  assert.equal(nodes['btn-write'].disabled, false);
  assert.match(nodes['btn-write'].textContent, /Write 2 changes to ref\.bib/);

  // With nothing staged it stays on show, disabled, so it is never "missing".
  set('staged = {};');
  call('stats');
  assert.equal(nodes['btn-write'].hidden, false, 'the Write button is always visible');
  assert.equal(nodes['btn-write'].disabled, true);
  assert.equal(nodes['btn-write'].textContent, 'Nothing to write');
  set("staged = {A2020: '@ARTICLE{A2020, title = {new}}', B2019: '@ARTICLE{B2019, title = {new}}'};");
  call('stats');

  call('unstage', 'B2019');
  assert.deepEqual(read('stagedKeys(staged, entries)'), ['A2020'], 'unstaging takes it back off the pile');

  set('originals = Object.fromEntries(entries.map(e => [e.key, e.raw]));');
  await read('writeAll()');
  assert.deepEqual(sent.map(s => s.url), ['/api/commit'], 'one request writes everything');
  assert.deepEqual(Object.keys(sent[0].body.edits), ['A2020']);
  assert.deepEqual(read('staged'), {}, 'staged edits clear once the server has them');
  assert.deepEqual(read('history').map(h => h.key), ['A2020'], 'and become undoable');
  console.log('ok: staging decides, and one Write is the only thing that touches the file');
})().catch(e => { console.error(e); process.exitCode = 1; });

(async () => {
  // A refused write must keep every staged edit: it is the only copy.
  const {nodes, read, call, set} = page({
    fetch: async () => ({ok: false, status: 409, json: async () => ({error: 'The .bib changed since this tab loaded it.'})}),
  });
  const rows = [entry({key: 'A2020', auto: true, raw: '@ARTICLE{A2020, title = {old}}'})];
  set(`entries = ${JSON.stringify(rows)}; source = 'ref.bib'; filePath = '/p/ref.bib'; revision = 'r1'; busy = false;`);
  set("deferred = []; focus = []; drafts = {}; staged = {A2020: '@ARTICLE{A2020, title = {new}}'}; fetchedAuto = {}; vetted = {}; history = []; blockedDrafts = true;");
  set('originals = Object.fromEntries(entries.map(e => [e.key, e.raw]));');
  await read('writeAll()');
  assert.deepEqual(read('stagedKeys(staged, entries)'), ['A2020'], 'a refused write must not lose the review');
  assert.deepEqual(read('history'), [], 'and must not claim anything was written');
  assert.match(nodes.note.textContent, /changed since this tab/);
  console.log('ok: a refused write keeps every staged edit');
})().catch(e => { console.error(e); process.exitCode = 1; });

/* ---- drafts belong to one bibliography, not to every "ref.bib" ---- */
{
  const stored = {};
  const {read, call, set} = page({
    localStorage: {getItem: k => stored[k] ?? null, setItem: (k, v) => { stored[k] = v; }},
  });
  set("source = 'ref.bib'; filePath = '/Users/me/PaperA/ref.bib';");
  set("drafts = {Riess2022: 'A-only text'}; blockedDrafts = false; persistDrafts();");
  assert.ok(stored['ads-review-drafts:/Users/me/PaperA/ref.bib'],
    'keying on the file name offers one paper\'s edit on another paper\'s entry');
  assert.ok(Object.keys(stored).every(k => k.endsWith('/Users/me/PaperA/ref.bib')),
    'both drafts and staged edits are scoped to the path');

  set("filePath = '/Users/me/PaperB/ref.bib'; drafts = {};");
  call('readDrafts');
  assert.deepEqual(read('drafts'), {}, 'a different bibliography starts clean');
  console.log('ok: drafts are scoped to the bibliography, not to its file name');
}

/* ---- a replacement that would change nothing is not offered ---- */
{
  const {call, set} = page();
  set('drafts = {}; staged = {}; fetchedAuto = {}; vetted = {}; confirming = null; history = [];');
  const raw = '@ARTICLE{Hoffman2014,\n  title = {NUTS}\n}';
  const same = call('cardBody', entry({
    key: 'Hoffman2014', raw, ads_bibtex: raw, identical: true,
    status: 'ADS_RECORD_CONFLICT', conflicts: ['key'], auto: false,
  }));
  assert.match(same, /identical to what is in the file/, 'the card has to say the replacement is a no-op');
  assert.match(same, /ADS disagrees on <b>key<\/b>/, 'and name what actually has to change');
  assert.match(same, /data-apply="Hoffman2014"\s+disabled/, 'and not invite the click');
  assert.match(same, /data-keep="Hoffman2014"/, 'Checked and Defer still work');

  // Typing something different re-enables it.
  set("drafts = {Hoffman2014: '@ARTICLE{Hoffman2014, title = {something else}}'};");
  const edited = call('cardBody', entry({
    key: 'Hoffman2014', raw, ads_bibtex: raw, identical: true,
    status: 'ADS_RECORD_CONFLICT', conflicts: ['key'], auto: false,
  }));
  assert.ok(!/data-apply="Hoffman2014"\s+disabled/.test(edited), 'a real edit is still applicable');

  // An entry whose export genuinely differs is untouched by this.
  const differs = call('cardBody', entry({identical: false}));
  assert.ok(!differs.includes('identical to what is in the file'));
  set('drafts = {};');
  const keyOnly = call('cardBody', entry({
    key: 'Hoffman2014', raw, ads_bibtex: raw, identical: true,
    status: 'CITATION_KEY_CONFLICT', conflicts: ['key'], auto: false, candidate: '',
  }));
  assert.match(keyOnly, /only the key/, 'the card names the key as the thing to change');
  assert.match(keyOnly, /rename the key/);
  assert.match(keyOnly, /data-apply="Hoffman2014"\s+disabled/, 'and does not offer the ADS export');
  set("drafts = {Hoffman2014: '@ARTICLE{Hoffman2014, title = {the record the key names}}'};");
  assert.ok(!/data-apply="Hoffman2014"\s+disabled/.test(call('cardBody', entry({
    key: 'Hoffman2014', raw, ads_bibtex: raw, identical: true,
    status: 'CITATION_KEY_CONFLICT', conflicts: ['key'], auto: false, candidate: '',
  }))), 'but pasting the record the key names is still allowed');
  assert.equal(call('tone', 'CITATION_KEY_CONFLICT'), 'warn');
  console.log('ok: a replacement that would rewrite the same bytes is not offered');
}

/* ---- an entry that already matches ADS cannot be staged, by any route ---- */
(async () => {
  const sent = [];
  const {nodes, read, call, set} = page({
    fetch: async (url, options = {}) => { sent.push({url, body: JSON.parse(options.body)}); return {ok: true, status: 200, json: async () => ({})}; },
  });
  const raw = '@software{Bradbury2021,\n  title = {JAX}\n}';
  const rows = [entry({key: 'Bradbury2021', raw, ads_bibtex: raw, identical: true,
                       status: 'IDENTIFIER_MISMATCH', conflicts: [], auto: true})];
  set(`entries = ${JSON.stringify(rows)}; source = 'ref.bib'; filePath = '/p/ref.bib'; revision = 'r1'; busy = false;`);
  set("deferred = []; focus = []; drafts = {}; staged = {}; fetchedAuto = {}; vetted = {}; history = []; confirming = null; blockedDrafts = true;");

  const card = call('cardBody', rows[0]);
  assert.match(card, /Nothing to stage — already matches ADS/, 'the button has to say why it is dead');
  assert.match(card, /data-apply="Bradbury2021"\s+disabled/);
  assert.ok(!card.includes('l stage'), 'and the keys line must not advertise a key that will not work');

  // The keyboard goes through the same guard as the button.
  call('act', 'replace');
  assert.deepEqual(read('staged'), {}, 'the stage key must not bypass the disabled button');
  assert.match(nodes.note.textContent, /already matches ADS/);
  assert.equal(sent.length, 0);
  console.log('ok: an entry that already matches ADS cannot be staged by button or key');
})().catch(e => { console.error(e); process.exitCode = 1; });

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

/* ---- a staged row offers the way out, not the way in ---- */
{
  const {nodes, call, set, read} = page();
  set(`entries = ${JSON.stringify([
    entry({key: 'A2020', line: 1}),
    entry({key: 'B2019', line: 9, status: 'OK', issueish: false}),
    entry({key: 'C2018', line: 20, status: 'OK', issueish: false, accepted: true}),
  ])};
  staged = {A2020: '@ARTICLE{A2020, title = {new}}', B2019: '@ARTICLE{B2019, title = {new}}'};
  filter = 'all';`);
  call('renderAll');
  const [a, b, c] = nodes['all-rows'].rendered;
  assert.match(a, /data-unstage="A2020"/, 'a staged issue offers Unstage, not Review');
  assert.ok(!a.includes('data-review'), 'Review is no use when the point is to drop the edit');
  // The trap this exists for: a re-check settles the entry to OK, so the row had
  // no button at all - while the stage still blocked the all-or-nothing write.
  assert.match(b, /data-unstage="B2019"/, 'a stage on a settled entry must still be reachable');
  assert.match(c, /data-unaccept="C2018"/, 'an unstaged acceptance is untouched');

  nodes['all-rows'].rendered = [];
  set("filter = 'staged';");
  call('renderAll');
  assert.equal(nodes['all-rows'].rendered.length, 2, 'the staged filter shows exactly what is staged');
  assert.deepEqual(read("entries.filter(e => matchesFilter(e, 'staged')).map(e => e.key)"), ['A2020', 'B2019']);
  console.log('ok: a staged row offers Unstage, and the staged filter finds them all');
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
  set('deferred = []; focus = []; drafts = {}; fetchedAuto = {}; vetted = {}; busy = false;');
  call('act', 'replace');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(called, 0, 'the replace key must not write a risky candidate');
  assert.match(nodes.note.textContent, /use the button/);

  set("fetchedAuto = {Smith2020: true}; vetted = {Smith2020: '@ARTICLE{Smith2020, title = {ads}}'}; drafts = {};");
  assert.match(read('cardBody(entries[0])'), /class="btn good"/,
    'once the server vets a fetched export it is one keypress again');

  // ...but only for that exact text.
  set("drafts = {Smith2020: '@ARTICLE{Smith2020, title = {something I pasted}}'};");
  assert.match(read('cardBody(entries[0])'), /class="btn danger" data-apply/,
    'text typed over a vetted proposal must go back to needing a confirmation');
  console.log('ok: the keyboard respects the server\'s risk verdict');
})().catch(e => { console.error(e); process.exitCode = 1; });

/* ---- accepting is recorded; deferring is not ---- */
(async () => {
  const sent = [];
  const {read, call, set} = page({
    fetch: async (url, options = {}) => {
      sent.push({url, body: JSON.parse(options.body)});
      return {ok: true, status: 200, json: async () => ({
        revision: 'r2', entries: [], counts: [], duplicates: [], warnings: [], tex: null,
        replaced: 0, skipped: 0, source: 'ref.bib', notice: 'A2020 accepted as correct',
      })};
    },
  });
  set(`entries = ${JSON.stringify([entry({key: 'A2020'}), entry({key: 'B2019'})])};`);
  set("deferred = []; focus = []; drafts = {}; staged = {}; fetchedAuto = {}; vetted = {}; busy = false; revision = 'r1';");

  call('act', 'keep');
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(sent, [{url: '/api/accept', body: {key: 'A2020', accepted: true}}],
    'accepting has to reach the server, or it dies with the tab');

  set(`entries = ${JSON.stringify([entry({key: 'A2020'}), entry({key: 'B2019'})])}; busy = false;`);
  call('act', 'defer');
  assert.deepEqual(read('deferred'), ['A2020'], 'deferring is a this-session ordering, not a decision');
  assert.equal(sent.length, 1, 'and never a write');
  console.log('ok: acceptance is persisted, deferral is not');
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

/* ---- review regressions: defer order, editable unstaging, and stale stages ---- */
(async () => {
  const stored = {};
  let requests = 0;
  const {read, call, set, nodes} = page({
    localStorage: {getItem: k => stored[k] ?? null, setItem: (k, v) => { stored[k] = v; }},
    fetch: async () => { requests++; throw new Error('unexpected write'); },
  });
  const rows = [entry({key: 'A'}), entry({key: 'B'})];
  set(`entries = ${JSON.stringify(rows)}; filePath = '/paper/ref.bib';`);
  call('act', 'defer'); call('act', 'defer'); call('act', 'defer');
  assert.deepEqual(read('queueKeys(entries, deferred, focus, staged)'), ['B', 'A']);
  const pasted = '@ARTICLE{A, title={Carefully edited replacement}}';
  call('stage', 'A', pasted);
  call('unstage', 'A');
  assert.equal(read('drafts.A'), pasted);
  call('readDrafts');
  assert.equal(read('drafts.A'), pasted, 'unstaged text survives reopening');
  call('stage', 'A', pasted);
  call('readDrafts');
  assert.equal(read('originals.A'), rows[0].raw);
  set('entries[0].raw = "@ARTICLE{A, title={External correction}}"; revision = "new";');
  await read('writeAll()');
  assert.equal(requests, 0);
  assert.match(nodes.note.textContent, /Unstage and review/);
  assert.equal(read('staged.A'), pasted);
  // Older saved stages have no original: retain the text, require another review.
  stored['ads-review-staged:/paper/ref.bib'] = JSON.stringify({A: pasted});
  call('readDrafts');
  await read('writeAll()');
  assert.equal(requests, 0);
  call('unstage', 'A');
  assert.equal(read('drafts.A'), pasted);
  console.log('ok: defer cycles, unstage preserves text, and reload never authorises stale stages');
})().catch(e => { console.error(e); process.exitCode = 1; });
