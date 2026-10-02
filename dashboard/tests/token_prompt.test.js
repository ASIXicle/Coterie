// token_prompt.test.js -- the dashboard's token prompt, run against the real app.js.
//
//   node dashboard/tests/token_prompt.test.js            (from the repo root)
//   node dashboard/tests/token_prompt.test.js <app.js>   (any other copy, e.g. a git show)
//
// The page starts several reads at once, and before the token is stored they all 401
// together. Each 401 handler used to delete the stored token and prompt, so a token
// entered at the first prompt was deleted by the second handler: the prompt came back
// once per concurrent read (the operator, 2026-09-26, with a token the server accepted).
// Only a browser shows that, so this lifts the API-helper block out of app.js and runs
// it with a stub fetch that answers each request in its own task, the way the network does.
// It also checks the redraw: once reads have failed on a refused token, the first read that
// works redraws the current view, once (the operator, 2026-09-26: the overview sat on "loading…"
// with a good token until a tab switch).
'use strict';
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(process.argv[2] || path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
// The block runs from the section banner to the subscribe() helper that follows it.
const START = '  // API helpers\n', END = '  /* subscribe(url, onData, intervalMs)';
const a = src.indexOf(START), b = src.indexOf(END);
if (a < 0 || b < 0 || b < a) { console.error('FAIL: API-helper block not found in app.js'); process.exit(1); }
const block = src.slice(a, b);

const GOOD = 'good-token', OLD = 'old-token', WRONG = 'wrong-token';

function harness(opts) {
  const store = new Map();
  if (opts.stored) store.set('dashboard.write-token', opts.stored);
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
  const answers = (opts.answers || []).slice();
  let prompts = 0;
  const window = { prompt: () => { prompts++; return answers.length ? answers.shift() : null; } };
  const accepted = opts.accepted || GOOD;
  const fetch = (url, init) => new Promise((resolve) => setTimeout(() => {
    const auth = (init && init.headers && init.headers.Authorization) || '';
    const status = auth === 'Bearer ' + accepted ? 200 : 401;
    resolve({ status, ok: status === 200, json: async () => ({ url }) });
  }, 0));
  let renders = 0;   // the view redraw api() schedules once a token works after 401s
  const renderRoute = () => { renders++; };
  const fns = new Function('window', 'localStorage', 'fetch', 'renderRoute',
    block + '\nreturn { api, postJSON, readToken };')(window, localStorage, fetch, renderRoute);
  return { ...fns, localStorage, prompts: () => prompts, renders: () => renders };
}

// Settle, then let any redraw api() scheduled with setTimeout run before counting.
const settle = (ps) => Promise.allSettled(ps)
  .then((r) => new Promise((ok) => setTimeout(() => ok(r.map((x) => x.status)), 5)));
let failed = 0;
function check(name, cond, detail) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '  ' + detail));
  if (!cond) failed++;
}
const READS = ['/api/system', '/api/rings', '/api/rings'];   // what the page starts at load

(async () => {
  // T1: nothing stored, the page's concurrent reads, the right token at the prompt.
  {
    const t = harness({ answers: [GOOD, GOOD, GOOD] });
    const r = await settle(READS.map((p) => t.api(p)));
    check('T1 empty store: one prompt for concurrent reads', t.prompts() === 1, `prompts=${t.prompts()}`);
    check('T1 every read succeeds', r.every((s) => s === 'fulfilled'), r.join(','));
    check('T1 entered token kept', t.readToken() === GOOD, `stored=${t.readToken()}`);
    check('T1 no redraw: nothing had failed', t.renders() === 0, `renders=${t.renders()}`);
  }
  // T2: a stale token stored (rotated server-side): one prompt, all retry with the new one.
  {
    const t = harness({ stored: OLD, answers: [GOOD, GOOD, GOOD] });
    const r = await settle(READS.map((p) => t.api(p)));
    check('T2 stale store: one prompt', t.prompts() === 1, `prompts=${t.prompts()}`);
    check('T2 every read succeeds', r.every((s) => s === 'fulfilled'), r.join(','));
    check('T2 new token kept', t.readToken() === GOOD, `stored=${t.readToken()}`);
  }
  // T3: prompt declined: one prompt, the reads fail, no prompt on the next poll.
  {
    const t = harness({ answers: [null, null, null] });
    const r = await settle(READS.map((p) => t.api(p)));
    await settle([t.api('/api/system')]);
    check('T3 declined: one prompt, none on the next poll', t.prompts() === 1, `prompts=${t.prompts()}`);
    check('T3 reads fail', r.every((s) => s === 'rejected'), r.join(','));
  }
  // T4: a wrong token entered: one prompt now, the reads fail, and the next poll asks again once.
  {
    const t = harness({ answers: [WRONG, GOOD, GOOD] });
    const r = await settle(READS.map((p) => t.api(p)));
    check('T4 wrong token: one prompt', t.prompts() === 1, `prompts=${t.prompts()}`);
    check('T4 reads fail', r.every((s) => s === 'rejected'), r.join(','));
    const r2 = await settle([t.api('/api/system')]);
    check('T4 next poll forgets it and asks once', t.prompts() === 2 && r2[0] === 'fulfilled', `prompts=${t.prompts()} ${r2}`);
    check('T4 the view redraws once the token works', t.renders() === 1, `renders=${t.renders()}`);
    await settle([t.api('/api/rings')]);
    check('T4 later reads do not redraw again', t.renders() === 1, `renders=${t.renders()}`);
  }
  // T5: a write that 401s with an old token must not delete a newer token stored meanwhile.
  {
    const t = harness({ stored: OLD });
    const p = t.postJSON('/api/amq/send', {});
    t.localStorage.setItem('dashboard.write-token', GOOD);   // entered by a read's prompt while the POST was in flight
    const r = await settle([p]);
    check('T5 POST 401 rejects', r[0] === 'rejected', r.join(','));
    check('T5 newer token survives the old POST 401', t.readToken() === GOOD, `stored=${t.readToken()}`);
  }
  console.log(failed ? `${failed} FAILED` : 'ALL PASS');
  process.exit(failed ? 1 : 0);
})();
