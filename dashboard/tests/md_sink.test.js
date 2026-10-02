// md_sink.test.js -- the dashboard's one HTML sink, run against the real memory.js.
//
//   node dashboard/tests/md_sink.test.js               (from the repo root)
//   node dashboard/tests/md_sink.test.js <memory.js>  (any other copy, e.g. a git show)
//
// A review finding (2026-09-25): mdToHtml escapes & < > and not quotes, and its output reaches
// innerHTML through the MD component. Traced 2026-10-01 (dashboard/optimizations/
// 2026-10-01-dashboard-client/PLAN.md §2.3): the converter never emits an attribute, so a quote has
// no attribute context to break out of, and every byte of text is escaped before a tag is inserted.
// That is true by construction today and nothing enforced it, so this test does: every tag the
// converter emits for hostile inputs must be on the allow-list, none may carry an attribute, and
// every '<' from the input must come out escaped. The counterfactual mutates the converter to emit
// a link from [text](url) and must FAIL, which proves the assertions bite.
'use strict';
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(process.argv[2] || path.join(__dirname, '..', 'static', 'memory.js'), 'utf8');
const m = src.match(/  function mdToHtml\(src\) \{[\s\S]*?\n  \}\n/);
if (!m) { console.error('FAIL: mdToHtml not found in memory.js'); process.exit(1); }
const mdToHtml = new Function(m[0] + '\nreturn mdToHtml;')();

const ALLOWED = /^<\/?(p|ul|ol|li|code|strong|em|blockquote)>$/;
const CASES = {
  'attribute breakout via link syntax': '[x](" onmouseover="alert(1))',
  'script and img onerror': '<script>alert(1)</script> <img src=x onerror=alert(1)>',
  'quotes inside code': '`"` and `\'` and `&quot;`',
  'javascript: urls': 'javascript:alert(1) [a](javascript:alert(1)) ![i](javascript:alert(1))',
  'tag inside bold': '**<b onclick=x>**',
  'tag inside a list item': '- <li onmouseover=1>item\n- *em*\n1. <ol>x',
  'entities are text': '&lt;b&gt; &amp; &#60; &#x3c;',
  'svg and unicode': '<svg/onload=alert(1)> <b> > not a quote',
  'blockquote': '> <q onclick=1>quoted\n> **bold**',
};
let failed = 0;
function check(name, out, input) {
  const tags = out.match(/<[^>]*>/g) || [];
  const stray = tags.filter((t) => !ALLOWED.test(t));
  const rawLt = (out.match(/</g) || []).length - tags.length;
  const inputLt = (input.match(/</g) || []).length;
  const escapedLt = (out.match(/&lt;/g) || []).length;
  const ok = stray.length === 0 && rawLt === 0 && escapedLt === inputLt;
  if (!ok) failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}: ${tags.length} tags, stray ${JSON.stringify(stray)}, '<' in ${inputLt} / escaped ${escapedLt}`);
}
for (const [name, input] of Object.entries(CASES)) check(name, mdToHtml(input), input);

// Counterfactual: a converter that grows a link feature without escaping quotes. The same
// assertions must catch it, or this test proves nothing.
const linky = new Function(
  m[0].replace("escape(s)\n        .replace(/`([^`]+?)`/g, '<code>$1</code>')",
               "escape(s)\n        .replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href=\"$2\">$1</a>')\n        .replace(/`([^`]+?)`/g, '<code>$1</code>')")
  + '\nreturn mdToHtml;')();
const cf = linky('[x](" onmouseover="alert(1))');
const cfStray = (cf.match(/<[^>]*>/g) || []).filter((t) => !ALLOWED.test(t));
const cfCaught = cfStray.length > 0 && /onmouseover=/.test(cf);
if (!cfCaught) failed++;
console.log(`${cfCaught ? 'PASS' : 'FAIL'} counterfactual: a link-emitting converter is caught (stray ${JSON.stringify(cfStray)})`);

console.log(failed ? `FAIL: ${failed}` : 'ALL PASS');
process.exit(failed ? 1 : 0);
