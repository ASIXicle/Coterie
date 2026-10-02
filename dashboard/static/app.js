/* persMEM Dashboard v3 — app shell.
   Depends on memory.js (h, hSvg, setChildren, time helpers, primitives).
   Renders the SideRail / TopBar / Footer chrome and the active tab.
*/
(function () {
  'use strict';

  // Convenience locals
  const { h, setChildren, fmtDuration, relTime, countdown, StatusDot } = window;

  // ==========================================================================
  // API helpers
  // ==========================================================================

  /* api(path) -- every read goes through here. Since 2026-09-25 the server gates reads with
     the same token as writes (the full memory export had been readable by every local account
     and the LAN). The token is attached when the browser holds one; on 401 the token that
     request sent is forgotten and one is asked for; a declined prompt stops further prompts
     until the page is reloaded, so the refresh loops do not nag. Several reads start together
     at page load and all 401 together: only the first prompts, and the rest retry with what
     was entered (forgetToken). */
  let tokenDeclined = false;
  let authFailed = false;   // a read ended in 401 since a token last worked
  async function api(path) {
    const hdr = (t) => (t ? { 'Authorization': 'Bearer ' + t } : {});
    let t = readToken();
    let res = await fetch(path, { cache: 'no-store', headers: hdr(t) });
    if (res.status === 401 && !tokenDeclined) {
      forgetToken(t);
      t = writeToken();
      if (!t) { tokenDeclined = true; throw new Error('API ' + path + ' needs the dashboard token (prompt declined)'); }
      res = await fetch(path, { cache: 'no-store', headers: hdr(t) });
    }
    if (!res.ok) {
      if (res.status === 401) authFailed = true;
      throw new Error('API ' + path + ' returned ' + res.status);
    }
    if (authFailed) {
      // A token works after reads already failed on a refused or declined one. The views
      // fetch once when drawn (the overview then stays static), so redraw the current one
      // now instead of leaving it on its failure until the next tab switch.
      authFailed = false;
      setTimeout(function () { renderRoute(); }, 0);
    }
    return res.json();
  }

  /* postJSON(path, payload) -- every write goes through here. The server refuses any
     POST without the write token (a root-installed file it reads at startup), so the
     browser asks for it once, keeps it in localStorage, and forgets it on the first 401.
     Returns the raw Response (the chat route streams), or throws on a refused token. */
  const WRITE_TOKEN_KEY = 'dashboard.write-token';   // key kept: a browser that held the write token needs no re-entry
  function readToken() {
    try { return localStorage.getItem(WRITE_TOKEN_KEY) || null; } catch (e) { return null; }
  }
  /* Forget the stored token only if it is the one this request sent. A 401 that was already
     in flight while the operator entered a new token must not delete it: that deletion made
     every concurrent page-load read prompt again (the operator, 2026-09-26, "the input field simply
     refreshes", with a token the server accepts). */
  function forgetToken(sent) {
    try {
      if ((localStorage.getItem(WRITE_TOKEN_KEY) || null) === (sent || null)) localStorage.removeItem(WRITE_TOKEN_KEY);
    } catch (e) { /* storage unavailable: nothing held, nothing to forget */ }
  }
  function writeToken() {
    try {
      let t = localStorage.getItem(WRITE_TOKEN_KEY);
      if (!t) {
        t = window.prompt('Dashboard token (the contents of the dashboard\'s post.token file; it gates reads and writes):');
        if (!t) return null;
        t = t.trim();
        localStorage.setItem(WRITE_TOKEN_KEY, t);
      }
      return t;
    } catch (e) {
      return null;
    }
  }
  async function postJSON(path, payload) {
    const t = writeToken();
    if (!t) throw new Error('write token required');
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + t },
      body: JSON.stringify(payload)
    });
    if (res.status === 401) {
      forgetToken(t);
      throw new Error('write token refused (401); it has been forgotten, try again');
    }
    return res;
  }

  /* subscribe(url, onData, intervalMs) — periodic refetch loop.
     Returns a stop fn. Errors are logged and the loop continues. */
  function subscribe(url, onData, intervalMs) {
    let stopped = false;
    let timer = null;
    async function tick() {
      if (stopped) return;
      try {
        const data = await api(url);
        if (!stopped) onData(data);
      } catch (e) {
        console.error('subscribe error:', url, e.message);
      }
      if (!stopped) timer = setTimeout(tick, intervalMs || 30000);
    }
    tick();
    return () => { stopped = true; if (timer) clearTimeout(timer); };
  }

  // ==========================================================================
  // App state
  // ==========================================================================

  const state = {
    route: 'overview',
    system: null,
    rings: null,
    // Tab data cached as it's fetched. Each tab populates its own slice.
    overview: null,
    amq: null,
    index: null,
  };

  let mountRoot = null;
  let mainEl = null;

  const TITLE_MAP = {
    overview: 'Overview',
    amq:      'AMQ Live Feed',
    index:    'Index',
  };

  // ==========================================================================
  // Boot
  // ==========================================================================

  function init(root) {
    mountRoot = root;
    setChildren(root, [renderShell()]);
    mainEl = root.querySelector('#main-content');

    // Always-visible data: the system line and the doorbell receipts strip.
    subscribe('/api/system', (d) => { state.system = d; updateTopBar(); }, 30000);
    subscribe('/api/rings',  (d) => { state.rings = d;  updateTopBar(); }, 30000);
    // subscribe() swallows errors and keeps polling, so an unreachable /api/rings would leave
    // the strip on "loading" forever (seen live 2026-09-25 while the old process served the
    // new files). Probe once and say so.
    api('/api/rings').catch(function (e) {
      if (!state.rings) { state.rings = { error: e.message }; updateTopBar(); }
    });

    // Tick the relative-time strings every second so "5m ago" updates live.
    setInterval(updateTopBar, 1000);

    // Keyboard nav.
    document.addEventListener('keydown', onKey);

    renderRoute();
  }

  function setRoute(r) {
    if (state.route === r) return;
    state.route = r;
    // Update side rail active highlight.
    const buttons = mountRoot.querySelectorAll('[data-route]');
    buttons.forEach((b) => {
      const active = b.dataset.route === r;
      b.classList.toggle('side-active', active);
      b.style.background = active ? 'var(--bg-panel)' : 'transparent';
      b.style.borderColor = active ? 'var(--line-strong)' : 'transparent';
      b.style.color = active ? 'var(--text-strong)' : 'var(--text-body)';
      b.style.fontWeight = active ? '500' : '400';
    });
    // Update title and subtitle.
    const title = mountRoot.querySelector('#topbar-title');
    if (title) title.textContent = TITLE_MAP[r] || r;
    updateTopBar();
    renderRoute();
  }

  function onKey(e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === '1') setRoute('overview');
    if (e.key === '2') setRoute('amq');
    if (e.key === '3') setRoute('index');
  }

  // ==========================================================================
  // Shell — sidebar + topbar + main + footer
  // ==========================================================================

  function renderShell() {
    return h('div', { className: 'flex', style: { minHeight: '100vh' } },
      renderSideRail(),
      h('div', { className: 'flex flex-col', style: { flex: '1', minWidth: '0' } },
        renderTopBar(),
        h('main', {
          id: 'main-content',
          style: {
            flex: '1', padding: '16px',
            maxWidth: '1600px', width: '100%', margin: '0 auto',
          }
        }),
        renderFooter()
      )
    );
  }

  function renderSideRail() {
    const items = [
      { id: 'overview', label: 'Overview', key: '1' },
      { id: 'amq',      label: 'AMQ',      key: '2' },
      { id: 'index',    label: 'Index',    key: '3' },
    ];
    return h('nav', {
      className: 'flex flex-col',
      style: {
        width: '184px', flexShrink: '0',
        background: 'var(--bg-inset)',
        borderRight: '1px solid var(--line)',
        position: 'sticky', top: '0', height: '100vh',
      }
    },
      h('div', {
        className: 'flex items-center gap-2',
        style: { padding: '16px' }
      },
        renderLogo(),
        h('div', { className: 'flex flex-col' },
          h('span', { style: { fontSize: '14px', fontWeight: '600', letterSpacing: '-0.01em' } }, 'persMEM'),
          h('span', {
            className: 'font-mono',
            style: { fontSize: '9.5px', color: 'var(--text-faint)', textTransform: 'uppercase', letterSpacing: '0.08em' }
          }, 'v3')
        )
      ),
      h('div', { className: 'flex flex-col', style: { padding: '4px 8px', gap: '2px' } },
        items.map((it) => renderNavButton(it))
      ),
      h('div', { style: { marginTop: 'auto', padding: '12px', borderTop: '1px solid var(--line)' } },
        h('div', { id: 'agent-legend' }, renderAgentLegend()),
        h('div', {
          id: 'site-label',
          className: 'font-mono',
          style: { marginTop: '12px', fontSize: '9.5px', color: 'var(--text-faint)', textTransform: 'uppercase', letterSpacing: '0.08em' }
        }, (state.system && state.system.site) || '')
      )
    );
  }

  function renderNavButton(it) {
    const active = state.route === it.id;
    return h('button', {
      'data-route': it.id,
      className: (active ? 'side-active ' : '') + 'flex items-center gap-2',
      style: {
        padding: '6px 10px',
        borderRadius: '5px',
        background: active ? 'var(--bg-panel)' : 'transparent',
        border: '1px solid ' + (active ? 'var(--line-strong)' : 'transparent'),
        color: active ? 'var(--text-strong)' : 'var(--text-body)',
        fontSize: '13px', fontWeight: active ? '500' : '400',
        textAlign: 'left',
        cursor: 'pointer',
      },
      onClick: () => setRoute(it.id)
    },
      h('span', { style: { flex: '1' } }, it.label),
      // A count and a shortcut are different facts and used to share a shape --
      // the numbered badges read as unread counts and were keyboard keys.
      it.count ? h('span', { className: 'count-pill', title: it.count + ' unread' }, it.count) : null,
      h('span', { className: 'kbd', title: 'press ' + it.key }, it.key)
    );
  }

  // The roster's HUE for one agent, undefined off-roster. tokens.css derives the
  // --id-* family from it per theme; the old raw-hex --c-* vars could not be
  // retuned and broke in dark mode.
  function rosterHue(name) {
    const roster = (state.system && state.system.roster) || [];
    const bird = roster.find(function (b) { return b.name === name; });
    return bird && bird.hue;
  }
  // agentVars ALWAYS pairs with data-agent on the same element -- tokens.css
  // declares the --id-* family in [data-agent], and a var() inside a custom
  // property resolves where it is declared, not where it is used.
  function agentVars(name) {
    return window.agentColorVars(name, rosterHue(name));
  }
  // Mailbox name of the human operator (roster file "operator" key, else the
  // server's DASHBOARD_OPERATOR, else "operator"); served as sys.operator.
  function operatorName() {
    return (state.system && state.system.operator) || 'operator';
  }

  function renderAgentLegend() {
    // The roster is defined once, in the portal's roster file (served as sys.roster).
    const agents = (state.system && state.system.roster) || [];
    return h('div', null,
      h('div', { className: 'eyebrow', style: { marginBottom: '6px' } }, 'agents'),
      h('div', { className: 'flex flex-col', style: { gap: '4px' } },
        agents.map((a) => h('div', {
          className: 'flex items-center gap-2',
          'data-agent': a.name,
          style: agentVars(a.name)
        },
          h('span', { className: 'agent-dot' }),
          h('span', {
            className: 'font-mono',
            style: { fontSize: '11px', color: 'var(--text-body)' }
          }, a.label)
        ))
      )
    );
  }

  function renderLogo() {
    return h('div', {
      style: {
        width: '28px', height: '28px', borderRadius: '6px',
        background: 'linear-gradient(135deg, oklch(0.55 0.13 245), oklch(0.55 0.13 310))',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'white', fontFamily: 'var(--font-mono)', fontWeight: '600', fontSize: '13px',
        boxShadow: '0 1px 2px oklch(0 0 0 / 0.12), inset 0 1px 0 oklch(1 0 0 / 0.2)',
      }
    }, '\u25C7');
  }

  function renderTopBar() {
    return h('header', {
      className: 'glass',
      style: {
        position: 'sticky', top: '0', zIndex: '30',
        borderBottom: '1px solid var(--line)'
      }
    },
      h('div', {
        className: 'flex items-center gap-4',
        style: { padding: '10px 16px' }
      },
        h('div', { className: 'flex items-baseline gap-2', style: { minWidth: '0' } },
          h('span', {
            id: 'topbar-title',
            style: { fontSize: '18px', fontWeight: '600', letterSpacing: '-0.02em' }
          }, TITLE_MAP[state.route]),
          h('span', {
            id: 'topbar-sub',
            className: 'font-mono',
            style: { fontSize: '11px', color: 'var(--text-quiet)' }
          })
        ),
        h('div', { style: { flex: '1' } }),
        h('div', { id: 'receipt-strip' })
      )
    );
  }

  // updateTopBar re-renders the always-visible "critic strip" pill and the
  // overview-only subtitle. Called on data refresh and once per second
  // (so relTime / countdown strings tick smoothly).
  function updateTopBar() {
    if (!mountRoot) return;
    const sub = mountRoot.querySelector('#topbar-sub');
    if (sub) {
      if (state.route === 'overview' && state.system) {
        sub.textContent = '\u00B7 host up ' + fmtDuration(state.system.uptime_sec || 0) +
                          ' \u00B7 embed ' + (state.system.model_name || state.system.model || 'unknown');
      } else {
        sub.textContent = '';
      }
    }
    const strip = mountRoot.querySelector('#receipt-strip');
    if (strip) setChildren(strip, [renderReceiptStrip()]);
    const legend = mountRoot.querySelector('#agent-legend');
    if (legend) setChildren(legend, [renderAgentLegend()]);
    const site = mountRoot.querySelector('#site-label');
    if (site) site.textContent = (state.system && state.system.site) || '';
  }

  // The always-visible receipt: is the doorbell ringing, and are rings being answered.
  // Read from the orchestrator's ledger (/api/rings), never guessed.
  function renderReceiptStrip() {
    const r = state.rings;
    const box = function (kind, parts) {
      return h('div', {
        className: 'flex items-center gap-3',
        style: {
          padding: '6px 12px', border: '1px solid var(--line)', borderRadius: 'var(--radius-2)',
          background: 'var(--bg-inset)', fontSize: '11.5px'
        }
      }, parts);
    };
    if (!r) return box('warn', [h('span', { className: 'eyebrow' }, 'doorbell'), h('span', { className: 'font-mono', style: { color: 'var(--text-faint)' } }, 'loading')]);
    if (r.error) return box('err', [StatusDot({ kind: 'err' }), h('span', { className: 'eyebrow' }, 'doorbell'),
      h('span', { className: 'font-mono', style: { color: 'var(--err-text)' }, title: r.error },
        r.ledger ? 'ledger unreadable' : (/ returned 401$|needs the dashboard token/.test(r.error) ? 'token refused' : 'endpoint unreachable'))]);
    const t = r.totals || {};
    const num = function (label, v, unit) {
      return h('span', { className: 'font-mono', style: { color: 'var(--text-quiet)' } },
        label + ' ', h('span', { style: { color: 'var(--text-strong)' } }, v == null ? '\u2014' : v + (unit || '')));
    };
    const fresh = r.last && (Date.now() - new Date(r.last).getTime()) < 7 * 86400000;
    return box('ok', [
      h('span', { className: 'flex items-center gap-1.5' },
        StatusDot({ kind: fresh ? 'ok' : 'warn' }),
        h('span', { className: 'eyebrow' }, 'doorbell'),
        h('span', { className: 'font-mono', style: { color: 'var(--text-body)' } }, fresh ? 'ringing' : 'quiet')),
      stripDivider(),
      num('rings 24h', t.rings_24h),
      stripDivider(),
      num('answered 7d', t.answered_7d != null ? t.answered_7d + '/' + t.rings_7d : null),
      stripDivider(),
      num('median', t.median_of_medians_7d, 's')
    ]);
  }

  function stripDivider() {
    return h('span', { style: { width: '1px', height: '14px', background: 'var(--line)' } });
  }

  function renderFooter() {
    return h('footer', {
      className: 'flex items-center justify-between font-mono',
      style: {
        padding: '12px 16px',
        fontSize: '10.5px',
        color: 'var(--text-faint)',
        borderTop: '1px solid var(--line)'
      }
    },
      h('span', null, 'persMEM \u00B7 flask \u00B7 chromadb \u00B7 debian-lxc'),
      h('span', { className: 'flex items-center gap-3' },
        h('span', null,
          h('span', { className: 'kbd' }, '1'),
          h('span', { className: 'kbd' }, '2'),
          h('span', { className: 'kbd' }, '3'),
          ' tabs'
        ),
        h('span', null, h('span', { className: 'kbd' }, '/'), ' search')
      )
    );
  }

  // ==========================================================================
  // Tab routing — each tab is a render function that owns mainEl
  // ==========================================================================

  function renderRoute() {
    if (!mainEl) return;
    const route = state.route;
    if (route === 'overview') return renderOverview();
    if (route === 'amq')      return renderAMQ();
    if (route === 'index')    return renderIndex();
  }

  // ==========================================================================
  // Overview tab
  // ==========================================================================

  // Stat tiles + activity + ranked types + bootstrap health + boots/hooks + news.
  // Fetches all 4 endpoints in parallel, paints once, then is static
  // (the polling subscriptions on /api/system update the topbar; if you switch
  // tabs and come back the data refreshes via re-call of renderOverview).

  function renderOverview() {
    setChildren(mainEl, [
      h('div', {
        id: 'overview-content',
        style: { display: 'flex', flexDirection: 'column', gap: '12px' }
      },
        h('div', {
          className: 'eyebrow',
          style: { padding: '40px 0', textAlign: 'center', color: 'var(--text-faint)' }
        }, 'loading…')
      )
    ]);

    Promise.all([
      api('/api/system').catch(() => null),
      api('/api/activity').catch(() => null),
      api('/api/stats').catch(() => null),
      api('/api/news').catch(() => null),
      api('/api/boots').catch(() => null),
      api('/api/reviews').catch(() => null),
      api('/api/hooks').catch(() => null),
      api('/api/edits').catch(() => null),
      api('/api/rings').catch(() => null),
    ]).then((results) => {
      const sys = results[0];
      const daysObj = results[1] || {};
      const stats = results[2] || {};
      const news = results[3] || [];
      const boots = results[4] || [];
      const reviews = results[5] || null;
      const hooks = results[6] || null;
      const edits = results[7] || null;
      const rings = results[8] || null;
      if (rings) state.rings = rings;
      if (sys) state.system = sys;
      // Endpoint returns { 'YYYY-MM-DD': {memories, news, amq} }; convert to array.
      const daysArr = Object.keys(daysObj).sort().map(function (d) {
        const c = daysObj[d] || {};
        return { date: d, memories: c.memories || 0, amq: c.amq || 0, news: c.news || 0 };
      });
      state.activityAll = daysArr;
      const n = state.activityRange == null ? 30 : state.activityRange;
      const windowed = n ? daysArr.slice(-n) : daysArr;
      paintOverview(state.system, windowed, stats, news, boots, reviews, hooks, edits, rings);
    }).catch(function (e) {
      const target = mountRoot.querySelector('#overview-content');
      if (target) target.textContent = 'error loading overview: ' + e.message;
    });
  }

  function paintOverview(sys, days, stats, news, boots, reviews, hooks, edits, rings) {
    const target = mountRoot.querySelector('#overview-content');
    if (!target) return;
    // Without the system read there is nothing to paint; say so rather than leave "loading…".
    if (!sys) { target.textContent = 'error loading overview: /api/system did not answer (see the console)'; return; }

    const sum = (arr, k, n) => arr.slice(-n).reduce((a, d) => a + (d[k] || 0), 0);
    const sparkM = days.map(function (d) { return d.memories; });
    const sparkA = days.map(function (d) { return d.amq; });
    const sparkN = days.map(function (d) { return d.news; });
    // online = roster agents that have an AMQ mailbox; total = roster size
    const roster = sys.roster || [];
    const mailboxes = sys.mailboxes || [];
    // "booted" is a manifest hash logged for that agent, not a mailbox directory --
    // a retired seat keeps its mailbox forever, so mailboxes never meant online.
    const booted = (boots || []).filter(function (b) { return b.last_boot; });
    const newest = booted.slice().sort(function (a, b) {
      return (b.last_boot || '').localeCompare(a.last_boot || '');
    })[0];
    const missing = roster.filter(function (r) {
      return !booted.some(function (b) { return b.agent === r.name; });
    }).map(function (r) { return r.name; });
    const ago = function (iso) {
      if (!iso) return '';
      const mins = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
      return mins < 90 ? mins + ' min ago' : Math.round(mins / 60) + ' h ago';
    };

    setChildren(target, [
      // Row 1 — 4 stat cards
      h('div', {
        style: {
          display: 'grid',
          gridTemplateColumns: 'repeat(4, minmax(0, 1fr))',
          gap: '12px'
        }
      },
        statCard({ label: 'memories',     value: sys.memories_total || 0, color: 'var(--series-0)', spark: sparkM,
                   today: sum(days, 'memories', 1), week: sum(days, 'memories', 7) }),
        statCard({ label: 'amq messages', value: sys.amq_total || 0,      color: 'var(--series-1)', spark: sparkA,
                   note: mailboxes.length + ' mailboxes',
                   today: sum(days, 'amq', 1),      week: sum(days, 'amq', 7) }),
        statCard({ label: 'news items',   value: sys.news_total || 0,     color: 'var(--series-2)', spark: sparkN,
                   today: sum(days, 'news', 1),     week: sum(days, 'news', 7) }),
        statCard({ label: 'boots',
                   display: booted.length + ' of ' + roster.length,
                   note: missing.length ? missing.join(', ') + ' not booted'
                                        : (newest ? 'last ' + newest.agent + ' ' + ago(newest.last_boot) : 'no boots recorded'),
                   noteWarn: missing.length > 0 })
      ),
      // Activity is primary now: it is the audit-trail view and it had the least room.
      activityCard(days),
      // Receipts: the four claims COMPARISON.md makes against the field, each read
      // from the file that records it. Boots (manifest hashes), rings (the
      // orchestrator's ledger), edits (pre-image sidecars), hooks (the scanner's log).
      h('div', { className: 'flex items-baseline gap-3', style: { marginTop: '4px' } },
        h('span', { className: 'panel-title' }, 'Receipts'),
        h('span', { style: { fontSize: '11px', color: 'var(--text-faint)' } },
          'every boot, ring, edit and scan, read from the record it left')),
      // Three short cards in one row (the operator, 2026-09-25: read them side by side), the
      // tall one full width beneath with its entries in two columns.
      h('div', { className: 'receipts-row' }, bootsCard(boots), ringsCard(rings), hooksCard(hooks)),
      editsCard(edits),
      bootstrapHealthCard(reviews),
      // The treemap is gone. by_type has 46 categories, the top 6 are 82% of the
      // corpus, and the smallest 20 hold 26 entries between them -- it spent its area
      // on rounding errors and drew cells smaller than their own labels.
      rankedTypesCard(stats),
      recentNewsCard(news)
    ]);
  }

  // A count, a sparkline, and what changed. The old tile showed "44% of 7,361" --
  // memories as a fraction of memories + amq + news, which are not parts of one
  // whole, so the bar under it encoded nothing. A delta is the operational fact.
  function statCard(p) {
    const delta = (n, word) => n == null ? null : h('span', {
      className: 'font-mono',
      style: { fontSize: '11px', color: n > 0 ? 'var(--ok-text)' : 'var(--text-faint)' }
    }, (n > 0 ? '+' : '') + n.toLocaleString() + ' ' + word);
    return h('div', {
      className: 'card density-pad',
      style: { display: 'flex', flexDirection: 'column', gap: '6px' }
    }, [
      h('div', { className: 'flex items-center justify-between' },
        h('span', { className: 'eyebrow' }, p.label),
        p.spark && p.spark.length > 1 ? window.MicroSpark({ data: p.spark, color: p.color }) : null
      ),
      h('div', { className: 'flex items-baseline gap-2' },
        h('span', {
          style: {
            fontSize: '28px', fontWeight: '600',
            letterSpacing: '-0.01em', color: 'var(--text-strong)'
          }
        }, p.display != null ? p.display : (p.value || 0).toLocaleString())
      ),
      h('div', { className: 'flex items-baseline gap-3' },
        p.note != null
          ? h('span', { className: 'font-mono',
              style: { fontSize: '11px', color: p.noteWarn ? 'var(--warn-text)' : 'var(--text-faint)' } }, p.note)
          : null,
        delta(p.today, 'today'),
        delta(p.week, 'this week')
      )
    ]);
  }

  // Primary now, and with a range: /api/activity holds daily series back to
  // 2026-04-08 and the chart was showing a fixed 30 and squeezed under the tiles.
  function activityCard(days) {
    if (!days || days.length === 0) {
      return h('div', { className: 'card density-pad' },
        h('div', { className: 'eyebrow' }, 'activity'),
        h('div', { style: { padding: '24px', textAlign: 'center', color: 'var(--text-faint)' } }, 'no data')
      );
    }
    const legend = h('div', {
      className: 'flex items-center gap-3 font-mono no-sel',
      style: { fontSize: '10.5px', color: 'var(--text-quiet)' }
    },
      legendSwatch('var(--series-0)', 'memories'),
      legendSwatch('var(--series-1)', 'amq'),
      legendSwatch('var(--series-2)', 'news')
    );
    const ranges = [['30', 30], ['90', 90], ['all', 0]];
    const active = state.activityRange == null ? 30 : state.activityRange;
    const seg = h('div', { className: 'seg-range', role: 'radiogroup', 'aria-label': 'Range' },
      ranges.map(function (r) {
        return h('button', {
          type: 'button', role: 'radio',
          'aria-checked': String(active === r[1]),
          onClick: function () {
            state.activityRange = r[1];
            renderOverview();          // one source of truth; repaint from state
          }
        }, r[0]);
      }));
    const peak = days.reduce(function (m, d) {
      return Math.max(m, d.memories + d.amq + d.news);
    }, 0);
    const zeroDays = days.filter(function (d) { return !(d.memories + d.amq + d.news); }).length;
    return window.Card({ children: [
      window.PanelHeader({
        title: 'Activity',
        sub: 'memories · amq · news per day · ' + days.length + ' days'
             + (zeroDays ? ' · ' + zeroDays + ' quiet' : ''),
        right: h('div', { className: 'flex items-center gap-3' }, legend, seg)
      }),
      h('div', { className: 'density-pad' },
        h('div', { className: 'flex justify-between font-mono',
                   style: { fontSize: '10px', color: 'var(--text-faint)', marginBottom: '3px' } },
          h('span', null, 'peak ' + peak.toLocaleString() + '/day'), h('span', null, '')),
        window.SparkBars({ days: days, height: 150 }),
        h('div', {
          className: 'flex justify-between font-mono',
          style: { marginTop: '8px', fontSize: '10px', color: 'var(--text-faint)' }
        },
          h('span', null, days[0].date),
          h('span', null, days[Math.floor(days.length / 2)].date),
          h('span', null, days[days.length - 1].date)
        )
      )
    ]});
  }

  function legendSwatch(color, label) {
    return h('span', { className: 'flex items-center gap-1.5' },
      h('i', {
        style: { width: '10px', height: '10px', background: color, borderRadius: '2px', display: 'inline-block' }
      }),
      label
    );
  }

  // ==========================================================================
  // Ranked type list — replaced the hand-rolled squarify treemap
  // ==========================================================================

  // Six named categories plus one honest remainder. The bar is relative to the
  // largest, not to the total, because the question is "what dominates", and the
  // colours are --series-0..5 -- the same scale the activity chart and the legend
  // use. The treemap had its own unrelated palette.
  function rankedTypesCard(stats) {
    const by = (stats && stats.by_type) || {};
    const rows = Object.keys(by).map(function (k) { return { name: k, n: by[k] }; })
      .sort(function (a, b) { return b.n - a.n; });
    if (!rows.length) {
      return window.Card({ children: [
        window.PanelHeader({ title: 'Types', sub: 'what the corpus is made of' }),
        h('div', { className: 'density-pad', style: { color: 'var(--text-faint)' } }, 'no type data')
      ]});
    }
    const top = rows.slice(0, 6);
    const rest = rows.slice(6);
    const restN = rest.reduce(function (a, r) { return a + r.n; }, 0);
    const total = rows.reduce(function (a, r) { return a + r.n; }, 0);
    const max = Math.max(top[0].n, restN);
    const line = function (label, n, colour) {
      return h('div', {
        style: { display: 'grid', gridTemplateColumns: '150px 1fr 64px 48px',
                 alignItems: 'center', gap: '10px' }
      },
        h('span', { style: { fontSize: '12.5px', color: 'var(--text-body)',
                             overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, label),
        h('div', { style: { height: '10px', background: 'var(--bg-inset)', borderRadius: '2px', overflow: 'hidden' } },
          h('div', { style: { width: Math.max(1, (n / max) * 100) + '%', height: '100%', background: colour } })),
        h('span', { className: 'font-mono', style: { fontSize: '11.5px', textAlign: 'right',
                    color: 'var(--text-body)', fontVariantNumeric: 'tabular-nums' } }, n.toLocaleString()),
        h('span', { className: 'font-mono', style: { fontSize: '11px', textAlign: 'right',
                    color: 'var(--text-faint)', fontVariantNumeric: 'tabular-nums' } },
          Math.round((n / total) * 100) + '%')
      );
    };
    return window.Card({ children: [
      window.PanelHeader({ title: 'Types', sub: rows.length + ' in the corpus, top 6 shown' }),
      h('div', { className: 'density-pad', style: { display: 'flex', flexDirection: 'column', gap: '7px' } },
        top.map(function (r, i) { return line(r.name, r.n, 'var(--series-' + i + ')'); })
          .concat(rest.length
            ? [line('other (' + rest.length + ' types)', restN, 'var(--ink-4)')]
            : []))
    ]});
  }

  // At N=14 the design is the documents themselves, not buckets and bars. The 8
  // with no date are shown and sorted last: a bootstrap document nobody has agreed
  // to re-read is a finding at the same weight as an overdue one.
  function bootstrapHealthCard(reviews) {
    if (!reviews || reviews.error) {
      return window.Card({ children: [
        window.PanelHeader({ title: 'Bootstrap health', sub: 'review-after across the boot payload' }),
        h('div', { className: 'density-pad', style: { color: 'var(--err-text)', fontSize: '12.5px' } },
          reviews ? 'cannot read: ' + reviews.error : 'endpoint unreachable')
      ]});
    }
    const rows = reviews.reviews || [];
    const chip = function (txt, colour) {
      return h('span', { className: 'font-mono',
        style: { fontSize: '11px', color: colour, background: 'var(--bg-inset)',
                 padding: '2px 6px', borderRadius: 'var(--radius-1)' } }, txt);
    };
    return window.Card({ children: [
      window.PanelHeader({ title: 'Bootstrap health', sub: rows.length + ' documents carry a review-after' }),
      h('div', { className: 'density-pad', style: { display: 'flex', gap: '8px', flexWrap: 'wrap' } },
        chip(reviews.overdue + ' overdue', reviews.overdue ? 'var(--err-text)' : 'var(--text-faint)'),
        chip(reviews.due_soon + ' due soon', reviews.due_soon ? 'var(--warn-text)' : 'var(--text-faint)'),
        chip(rows.length + ' dated', 'var(--text-quiet)')
      ),
      h('div', { className: 'density-pad', style: { display: 'flex', flexDirection: 'column', gap: '5px' } },
        rows.map(function (r) {
          const colour = r.status === 'overdue' ? 'var(--err-text)'
                       : r.status === 'due-soon' ? 'var(--warn-text)' : 'var(--text-faint)';
          return h('div', { style: { display: 'grid', gridTemplateColumns: '1fr 110px 92px 64px',
                                     gap: '10px', alignItems: 'baseline' } },
            h('span', { style: { fontSize: '12px', color: 'var(--text-body)', overflow: 'hidden',
                                 textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, r.title || r.id),
            window.TypeBadge({ type: r.type || 'unknown' }),
            h('span', { className: 'font-mono', style: { fontSize: '11px', color: 'var(--text-quiet)' } }, r.review_after),
            h('span', { className: 'font-mono', style: { fontSize: '11px', color: colour, textAlign: 'right' } },
              r.days >= 0 ? r.days + 'd' : Math.abs(r.days) + 'd over')
          );
        }))
    ]});
  }

  // Drift is the column that earns this card: a hash is a fact, a hash that changed
  // is a question, and the answer is whether an edit was recorded between the two
  // boots. Changed-after-an-edit is the system working; changed-with-no-edit is the
  // signal COMPARISON.md promises to surface.
  function bootsCard(boots) {
    const rows = boots || [];
    const when = function (iso) { return iso ? relTime(iso) : ''; };
    return window.Card({ children: [
      window.PanelHeader({ title: 'Boots', sub: 'manifest hash per agent, and why it moved' }),
      h('div', { className: 'density-pad', style: { display: 'flex', flexDirection: 'column', gap: '6px' } },
        rows.length ? rows.map(function (b) {
          let verdict, colour;
          if (b.drifted === false) { verdict = 'steady'; colour = 'var(--text-faint)'; }
          else if (b.drifted === true && b.explained === true) {
            verdict = 'changed \u00B7 ' + b.edits_between + (b.edits_between === 1 ? ' edit' : ' edits') + ' recorded';
            colour = 'var(--text-quiet)';
          } else if (b.drifted === true) { verdict = 'drifted \u00B7 no edit recorded'; colour = 'var(--warn-text)'; }
          else { verdict = b.boots ? 'first boot' : 'never booted'; colour = 'var(--text-faint)'; }
          return h('div', {
            'data-agent': b.agent,
            style: Object.assign({ display: 'grid', gridTemplateColumns: '86px 96px 64px 1fr',
                                   gap: '10px', alignItems: 'baseline' }, agentVars(b.agent))
          },
            h('span', { style: { fontSize: '12.5px', fontWeight: '600', color: 'var(--id-text, var(--text-strong))' } }, b.agent),
            h('span', { className: 'font-mono', title: b.last_hash || '', style: { fontSize: '11px', color: 'var(--text-faint)' } },
              b.last_hash ? b.last_hash.slice(0, 12) + '\u2026' : '\u2014'),
            h('span', { className: 'font-mono', title: b.last_boot ? window.absTime(b.last_boot) : '',
              style: { fontSize: '11px', color: 'var(--text-faint)' } }, when(b.last_boot)),
            h('span', { className: 'font-mono', style: { fontSize: '11px', color: colour, textAlign: 'right' } }, verdict)
          );
        }) : [h('span', { style: { color: 'var(--text-faint)' } }, 'no boot history')])
    ]});
  }

  // A ring is a ledger line and an answer has a measured latency: this card shows
  // both, per agent, from the orchestrator's own file.
  function ringsCard(rings) {
    if (!rings || rings.error) {
      return window.Card({ children: [
        window.PanelHeader({ title: 'Rings', sub: 'doorbell ledger' }),
        h('div', { className: 'density-pad', style: { color: 'var(--err-text)', fontSize: '12.5px' } },
          rings ? 'cannot read the ledger: ' + rings.error : 'endpoint unreachable')
      ]});
    }
    const t = rings.totals || {};
    const secs = function (v) { return v == null ? '\u2014' : Math.round(v) + 's'; };
    return window.Card({ children: [
      window.PanelHeader({ title: 'Rings', sub: (rings.records || 0) + ' ledger lines read \u00B7 this week rung ' +
        (t.rings_7d || 0) + ', answered ' + (t.answered_7d || 0) +
        ((t.answered_7d || 0) > (t.rings_7d || 0) ? ' (' + ((t.answered_7d || 0) - (t.rings_7d || 0)) + ' to older rings)' : '') +
        (t.rerings_7d ? ' \u00B7 ' + t.rerings_7d + ' re-rung' : '') }),
      h('div', { className: 'density-pad', style: { display: 'flex', flexDirection: 'column', gap: '6px' } },
        // each label in its own element: a grid folds adjacent bare text into ONE cell,
        // which put all five headings over the first column (the operator, 2026-09-25)
        h('div', { style: { display: 'grid', gridTemplateColumns: '86px 56px 64px 72px 1fr', gap: '10px' } },
          ['agent', '24h', 'week', 'median', 'last answer'].map(function (t) { return h('span', { className: 'eyebrow' }, t); })),
        (rings.agents || []).map(function (a) {
          return h('div', {
            'data-agent': a.agent,
            style: Object.assign({ display: 'grid', gridTemplateColumns: '86px 56px 64px 72px 1fr',
                                   gap: '10px', alignItems: 'baseline' }, agentVars(a.agent))
          },
            h('span', { style: { fontSize: '12.5px', fontWeight: '600', color: 'var(--id-text, var(--text-strong))' } }, a.agent),
            h('span', { className: 'font-mono', style: { fontSize: '11px', color: 'var(--text-body)' } }, a.rings_24h),
            h('span', { className: 'font-mono', style: { fontSize: '11px', color: 'var(--text-quiet)' } },
              a.answered_7d + '/' + a.rings_7d),
            h('span', { className: 'font-mono', style: { fontSize: '11px', color: 'var(--text-quiet)' } }, secs(a.median_latency_7d)),
            h('span', { className: 'font-mono', title: a.last_answer || '', style: { fontSize: '11px', color: 'var(--text-faint)' } },
              a.last_answer ? relTime(a.last_answer) + ' (' + secs(a.last_latency) + ')' : '\u2014')
          );
        }))
    ]});
  }

  // Every edit to what an agent boots on left a pre-image, a reason and an author;
  // this is that file, newest first. "pinned" means the writer staged the after-image
  // and the server verified it before the upsert.
  function editsCard(edits) {
    if (!edits || edits.error) {
      return window.Card({ children: [
        window.PanelHeader({ title: 'Edits', sub: 'bootstrap pre-images' }),
        h('div', { className: 'density-pad', style: { color: 'var(--err-text)', fontSize: '12.5px' } },
          edits ? 'cannot read: ' + edits.error : 'endpoint unreachable')
      ]});
    }
    const rows = (edits.edits || []).slice(0, 8);
    return window.Card({ children: [
      window.PanelHeader({ title: 'Edits', sub: (edits.total || 0) + ' recorded \u00B7 newest ' + rows.length }),
      h('div', { className: 'density-pad edits-grid' },
        rows.length ? rows.map(function (e) {
          return h('div', {
            'data-agent': e.author,
            style: Object.assign({ display: 'flex', flexDirection: 'column', gap: '2px' }, agentVars(e.author))
          },
            h('div', { className: 'flex items-baseline gap-2', style: { minWidth: '0' } },
              h('span', { className: 'font-mono', style: { fontSize: '11.5px', color: 'var(--text-strong)', fontWeight: '500' } }, e.entry),
              h('span', { className: 'font-mono', style: { fontSize: '10.5px', color: 'var(--text-faint)' } },
                'v' + (e.version == null ? '?' : e.version) + ' \u00B7 ' + e.before + '\u2026 \u2192 ' + e.after + '\u2026'),
              h('span', { className: 'font-mono', style: { fontSize: '10.5px', color: e.after_verified ? 'var(--ok-text)' : 'var(--text-faint)' } },
                e.after_verified ? 'pinned' : 'unpinned'),
              h('span', { style: { flex: '1' } }),
              h('span', { style: { fontSize: '11.5px', fontWeight: '600', color: 'var(--id-text, var(--text-strong))' } }, e.author),
              h('span', { className: 'font-mono', title: e.at, style: { fontSize: '10.5px', color: 'var(--text-faint)' } }, e.at ? relTime(e.at) : '')),
            h('div', { style: { fontSize: '12px', color: 'var(--text-body)', lineHeight: '1.4',
                                overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: '2', WebkitBoxOrient: 'vertical' },
                       title: e.reason }, e.reason || '(no reason recorded)')
          );
        }) : [h('span', { style: { color: 'var(--text-faint)' } }, 'no edits recorded')])
    ]});
  }

  // The scan is read from a log, not computed: every agent home is mode 700 so this
  // process cannot hash anyone's settings.json. Which makes the AGE load-bearing --
  // a stale CLEAN is not the same claim as a fresh one.
  function hooksCard(hooks) {
    if (!hooks || hooks.error) {
      return window.Card({ children: [
        window.PanelHeader({ title: 'Hooks', sub: 'config integrity' }),
        h('div', { className: 'density-pad', style: { color: 'var(--err-text)', fontSize: '12.5px' } },
          hooks ? 'cannot read: ' + hooks.error : 'endpoint unreachable')
      ]});
    }
    const stale = (hooks.age_days || 0) >= 7;
    return window.Card({ children: [
      window.PanelHeader({ title: 'Hooks', sub: 'config integrity' }),
      h('div', { className: 'density-pad' },
        h('span', { className: 'font-mono',
          style: { fontSize: '11px', color: stale ? 'var(--warn-text)' : 'var(--text-faint)' } },
          hooks.scanned_at ? 'scanned ' + hooks.age_days + ' days ago' : 'never scanned')),
      h('div', { className: 'density-pad', style: { display: 'flex', flexDirection: 'column', gap: '6px' } },
        (hooks.agents || []).map(function (a) {
          const colour = a.state === 'ALERT' ? 'var(--err-text)'
                       : a.state === 'CLEAN' ? 'var(--ok-text)' : 'var(--text-faint)';
          return h('div', {
            'data-agent': a.agent,
            style: Object.assign({ display: 'grid', gridTemplateColumns: '86px 1fr', gap: '10px' }, agentVars(a.agent))
          },
            h('span', { style: { fontSize: '12.5px', fontWeight: '600', color: 'var(--id-text, var(--text-strong))' } }, a.agent),
            h('span', { className: 'font-mono', style: { fontSize: '11px', color: colour } }, a.state)
          );
        }))
    ]});
  }

  // ==========================================================================
  // Recent news (6-up two-column grid)
  // ==========================================================================

  // News sources: a hue per source, same [data-type] mechanism as the badges above.
  const NEWS_HUES = { rss: 30, arxiv: 290, github: 200 };

  function recentNewsCard(news) {
    if (!news || news.length === 0) {
      return h('div', { className: 'card density-pad' },
        h('div', { className: 'eyebrow' }, 'recent news'),
        h('div', {
          style: { padding: '20px', textAlign: 'center', color: 'var(--text-faint)' }
        }, 'no news items')
      );
    }
    const sorted = news.slice().sort(function (a, b) {
      return (b.stored_at || '').localeCompare(a.stored_at || '');
    });
    const top = sorted.slice(0, 6);
    const total = (state.system && state.system.news_total) || news.length;

    return window.Card({ children: [
      window.PanelHeader({
        title: 'Recent News',
        sub: total + ' total · feed: news',
        right: h('div', {
          className: 'flex items-center gap-3 font-mono',
          style: { fontSize: '10.5px', color: 'var(--text-faint)' }
        },
          h('span', null, 'updated ' + (top[0] ? relTime(top[0].stored_at) : '\u2014'))
        )
      }),
      h('div', {
        className: 'body-pad',
        style: { paddingTop: '8px', paddingBottom: '8px' }
      },
        h('div', {
          style: {
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            columnGap: '32px'
          }
        },
          top.map(function (n, i) { return newsRow(n, i, top.length); })
        )
      )
    ]});
  }

  function newsRow(n, i, totalCount) {
    const hue = NEWS_HUES[n.type] == null ? NEWS_HUES.rss : NEWS_HUES[n.type];
    return h('div', {
      className: 'copy-host',
      style: {
        padding: '14px 0',
        borderBottom: i < totalCount - 2 ? '1px solid var(--line)' : 'none',
        display: 'grid', gridTemplateColumns: '64px 1fr auto',
        gap: '14px', alignItems: 'baseline'
      }
    },
      h('span', {
        className: 'type-badge',
        'data-type': n.type || 'rss',
        style: { '--type-h': String(hue), textTransform: 'uppercase', justifyContent: 'center' }
      }, n.type || 'rss'),
      h('div', { style: { minWidth: '0' } },
        window.MD({
          source: n.content || '',
          className: ''
        }),
        h('div', {
          className: 'flex items-center gap-2 font-mono',
          style: { marginTop: '6px', fontSize: '10.5px', color: 'var(--text-faint)' }
        },
          h('span', null, n.project || 'general'),
          h('span', null, '\u00B7'),
          h('span', null, (n.tags || '').split(',').slice(0, 3).map(function (t) { return t.trim(); }).filter(Boolean).join(' \u00B7 '))
        )
      ),
      h('div', {
        className: 'flex items-center gap-2',
        style: { flexShrink: '0' }
      },
        window.CopyBtn({ text: n.content || '' }),
        h('span', {
          className: 'font-mono',
          style: { fontSize: '10.5px', color: 'var(--text-faint)' }
        }, relTime(n.stored_at))
      )
    );
  }

  // ==========================================================================
  // AMQ tab — live feed + filters + compose
  // ==========================================================================

  // AMQ kinds are text on a tint, like memory types: a HUE per kind, and tokens.css
  // derives --type-wash / --type-ink from it on [data-type], retuned in dark. The old
  // table painted literal light washes with no dark variant (cold read 2026-09-25).
  // Kinds without a hue take the neutral fallback and read as quiet.
  const KIND_HUES = {
    answer: 152, question: 248, directive: 30, analysis: 220, decision: 248,
    review_request: 75, review_response: 75
  };

  const amqUI = {
    filter: { from: null },
    expanded: new Set(),
    composeOpen: false,
    search: '',
    messages: []
  };

  function renderAMQ() {
    setChildren(mainEl, [
      h('div', {
        id: 'amq-content',
        style: { display: 'flex', flexDirection: 'column', gap: '12px' }
      },
        h('div', {
          className: 'eyebrow',
          style: { padding: '40px 0', textAlign: 'center', color: 'var(--text-faint)' }
        }, 'loading\u2026')
      )
    ]);

    api('/api/amq').then(function (msgs) {
      amqUI.messages = msgs || [];
      // Auto-expand the most recent message so there's something to read on land.
      if (amqUI.messages.length > 0 && amqUI.expanded.size === 0) {
        amqUI.expanded.add(amqUI.messages[0].id);
      }
      paintAMQ();
    }).catch(function (e) {
      const target = mountRoot.querySelector('#amq-content');
      if (target) target.textContent = 'error loading AMQ: ' + e.message;
    });
  }

  function paintAMQ() {
    const target = mountRoot.querySelector('#amq-content');
    if (!target) return;
    const sys = state.system || {};
    const mailboxes = sys.mailboxes || [];  // AMQ dirs, not the roster
    const msgs = amqUI.messages;

    // Per-agent send counts for the chip row
    const counts = {};
    for (let i = 0; i < mailboxes.length; i++) counts[mailboxes[i]] = 0;
    for (let i = 0; i < msgs.length; i++) {
      const f = msgs[i].from;
      counts[f] = (counts[f] || 0) + 1;
    }

    // Filter: by from-agent and by search query.
    const q = (amqUI.search || '').toLowerCase();
    const filtered = msgs.filter(function (m) {
      if (amqUI.filter.from && m.from !== amqUI.filter.from) return false;
      if (q) {
        const blob = ((m.subject || '') + ' ' + (m.body || '') + ' ' + (m.from || '')).toLowerCase();
        if (blob.indexOf(q) === -1) return false;
      }
      return true;
    });

    const headerCard = window.Card({ children: [
      window.PanelHeader({
        title: h('span', { className: 'flex items-center gap-2' },
          h('span', { className: 'live-dot' }),
          'AMQ Live Feed'
        ),
        sub: msgs.length + ' messages \u00B7 ' + mailboxes.length + ' mailboxes',
        right: h('div', { className: 'flex items-center gap-2' },
          h('span', { className: 'eyebrow', style: { marginRight: '4px' } }, 'from'),
          mailboxes.map(function (a) {
            return h('button', {
              style: { padding: '0', border: 'none', background: 'transparent', cursor: 'pointer' },
              onClick: function () {
                amqUI.filter.from = (amqUI.filter.from === a) ? null : a;
                paintAMQ();
              }
            }, window.AgentBadge({ name: a, count: counts[a] || 0, active: amqUI.filter.from === a,
                                   hue: rosterHue(a) }));
          })
        )
      }),
      h('div', {
        className: 'flex items-center gap-3',
        style: { padding: '8px 12px', borderBottom: '1px solid var(--line)' }
      },
        h('button', {
          className: 'btn ' + (amqUI.composeOpen ? 'btn-primary' : ''),
          onClick: function () {
            amqUI.composeOpen = !amqUI.composeOpen;
            paintAMQ();
          }
        }, (amqUI.composeOpen ? '\u2212' : '+') + ' compose'),
        h('input', {
          className: 'input',
          style: { flex: '1' },
          placeholder: 'search subject / body / sender\u2026',
          value: amqUI.search,
          onInput: function (e) {
            amqUI.search = e.target.value;
            paintAMQ();
          }
        }),
        h('span', {
          className: 'font-mono',
          style: { fontSize: '10.5px', color: 'var(--text-faint)' }
        }, filtered.length + ' of ' + msgs.length)
      ),
      amqUI.composeOpen ? composeForm(mailboxes) : null
    ]});

    const list = h('div', {
      style: { display: 'flex', flexDirection: 'column', gap: '8px' }
    },
      filtered.map(function (m, i) {
        return amqCard(m, i, filtered);
      })
    );

    setChildren(target, [headerCard, list]);

    // After paint, the search input lost focus from re-render. Restore caret if present.
    if (amqUI.search) {
      const searchEl = target.querySelector('input.input');
      if (searchEl) {
        searchEl.focus();
        searchEl.setSelectionRange(amqUI.search.length, amqUI.search.length);
      }
    }
  }

  function amqCard(m, i, list) {
    const prev = list[i - 1];
    const showDate = !prev || new Date(prev.created).toDateString() !== new Date(m.created).toDateString();
    const expanded = amqUI.expanded.has(m.id);

    const dateBar = showDate ? h('div', {
      className: 'flex items-center gap-2',
      style: { padding: '4px' }
    },
      h('div', { style: { flex: '1', height: '1px', background: 'var(--line)' } }),
      h('span', {
        className: 'font-mono',
        style: { fontSize: '10.5px', color: 'var(--text-faint)' }
      }, new Date(m.created).toDateString()),
      h('div', { style: { flex: '1', height: '1px', background: 'var(--line)' } })
    ) : null;

    const headerRow = h('div', { className: 'flex items-center justify-between gap-3' },
      h('div', { className: 'flex items-center gap-2', style: { minWidth: '0' } },
        h('span', { className: 'agent-dot' }),
        h('span', {
          className: 'font-mono',
          style: { fontSize: '11.5px', color: 'var(--text-body)', fontWeight: '500' }
        }, m.from),
        h('span', { style: { color: 'var(--text-faint)' } }, '\u2192'),
        h('span', {
          className: 'font-mono',
          style: { fontSize: '11.5px', color: 'var(--text-body)', fontWeight: '500' }
        }, m.to),
        h('span', { style: { width: '1px', height: '12px', background: 'var(--line-strong)', margin: '0 4px' } }),
        kindBadge(m.kind, m.priority),
        m.status === 'unread' ? h('span', {
          className: 'font-mono',
          style: {
            fontSize: '9.5px', fontWeight: '600', letterSpacing: '0.06em',
            padding: '2px 6px', marginLeft: '4px',
            background: 'var(--accent)', color: 'white', borderRadius: '3px'
          }
        }, 'NEW') : null
      ),
      h('div', { className: 'flex items-center gap-3', style: { flexShrink: '0' } },
        window.CopyBtn({ text: m.body || '' }),
        h('span', {
          className: 'font-mono',
          style: { fontSize: '10.5px', color: 'var(--text-faint)' },
          title: window.absTime(m.created)
        }, relTime(m.created)),
        h('span', {
          style: { color: 'var(--text-faint)', fontSize: '11px' }
        }, expanded ? '\u25BE' : '\u25B8')
      )
    );

    const subjectRow = h('div', {
      style: {
        marginTop: '6px', paddingRight: '24px',
        fontSize: '13.5px', color: 'var(--text-strong)', lineHeight: '1.4'
      }
    }, m.subject || '(no subject)');

    const card = h('div', {
      className: 'card copy-host',
      style: Object.assign({
        borderLeft: '3px solid var(--id-edge, var(--line-strong))',
        background: 'color-mix(in oklch, var(--id-wash, var(--bg-inset)) 60%, var(--bg-inset))'
      }, Object.assign({ 'data-agent': m.from }, agentVars(m.from)))
    },
      h('button', {
        style: {
          width: '100%', textAlign: 'left',
          padding: '12px 14px',
          background: 'transparent', border: 'none',
          cursor: 'pointer', display: 'block'
        },
        onClick: function () {
          if (amqUI.expanded.has(m.id)) amqUI.expanded.delete(m.id);
          else amqUI.expanded.add(m.id);
          paintAMQ();
        }
      },
        headerRow,
        subjectRow
      ),
      expanded ? h('div', {
        className: 'body-pad md',
        style: {
          borderTop: '1px solid var(--id-edge)',
          background: 'var(--bg-inset)',
          fontSize: '13.5px', color: 'var(--text-body)',
          paddingTop: '12px', paddingBottom: '12px'
        }
      },
        window.MD({ source: m.body || '' }),
        h('div', {
          className: 'flex items-center justify-between',
          style: { marginTop: '20px', paddingTop: '12px', borderTop: '1px dashed var(--line)' }
        },
          h('span', {
            className: 'font-mono',
            style: { fontSize: '10.5px', color: 'var(--text-faint)' }
          }, m.id)
        )
      ) : null
    );

    return h('div', null, dateBar, card);
  }

  function kindBadge(kind, priority) {
    const hue = KIND_HUES[kind];
    return h('span', { className: 'flex items-center gap-1.5' },
      h('span', {
        className: 'type-badge',
        'data-type': kind || 'note',          // required: tokens.css hangs --type-* on it
        style: hue == null ? {} : { '--type-h': String(hue) }
      }, kind || 'note'),
      priority === 'urgent' ? h('span', {
        className: 'type-badge',
        'data-type': 'urgent',
        style: { '--type-h': '25', color: 'var(--err-text)' }
      }, '!urgent') : null
    );
  }

  function composeForm(agents) {
    // The sender is always the operator mailbox; the server decides that, not this form
    // (a caller-chosen sender let anyone on the LAN ring an agent under another's name).
    // Recipients are roster agents only: the server refuses anything else, so the list
    // here is the roster, not every mailbox the AMQ root happens to contain.
    const op = operatorName();
    const rosterNames = ((state.system && state.system.roster) || []).map(function (b) { return b.name; })
      .filter(function (n) { return n !== op; });
    const toOptions = rosterNames.concat(['all']);
    const defaultTo = rosterNames.filter(function (n) { return agents.indexOf(n) !== -1; })[0] || rosterNames[0] || 'all';
    let selTo, inSubject, inBody;

    function send() {
      const payload = {
        to: selTo.value,
        subject: inSubject.value,
        body: inBody.value
      };
      if (!payload.body) {
        inBody.focus();
        return;
      }
      postJSON('/api/amq/send', payload).then(function (r) { return r.json(); }).then(function (resp) {
        if (resp.ok) {
          amqUI.composeOpen = false;
          // Refresh feed to pick up the new message
          api('/api/amq').then(function (msgs) {
            amqUI.messages = msgs || [];
            paintAMQ();
          });
        } else {
          alert('Send failed: ' + (resp.error || 'unknown'));
        }
      }).catch(function (e) {
        alert('Send error: ' + e.message);
      });
    }

    const fromLabel = h('span', { className: 'font-mono', style: { fontSize: '12px' } }, op + ' (you)');

    selTo = h('select', { className: 'input' },
      toOptions.map(function (a) {
        return h('option', { value: a }, a + (a === 'all' ? ' (broadcast)' : ''));
      })
    );
    setTimeout(function () { selTo.value = defaultTo; }, 0);

    inSubject = h('input', {
      className: 'input',
      style: { flex: '1' },
      placeholder: 'subject\u2026'
    });

    inBody = h('textarea', {
      className: 'input',
      rows: '4',
      placeholder: 'markdown body\u2026',
      style: {
        width: '100%',
        fontSize: '12px',
        fontFamily: '"IBM Plex Mono", monospace',
        resize: 'vertical',
        marginTop: '8px'
      }
    });

    inBody.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        send();
      }
    });

    return h('div', {
      className: 'density-pad',
      style: { borderBottom: '1px solid var(--line)', background: 'var(--bg-well)' }
    },
      h('div', { className: 'flex items-center gap-2' },
        h('span', { className: 'eyebrow' }, 'from'),
        fromLabel,
        h('span', { style: { color: 'var(--text-faint)' } }, '\u2192'),
        h('span', { className: 'eyebrow' }, 'to'),
        selTo,
        inSubject
      ),
      inBody,
      h('div', {
        className: 'flex items-center justify-between',
        style: { marginTop: '8px' }
      },
        h('span', {
          className: 'font-mono',
          style: { fontSize: '10.5px', color: 'var(--text-faint)' }
        }, '\u2318\u21B5 to send \u00B7 markdown supported'),
        h('div', { className: 'flex gap-2' },
          h('button', {
            className: 'btn btn-ghost',
            onClick: function () {
              amqUI.composeOpen = false;
              paintAMQ();
            }
          }, 'cancel'),
          h('button', {
            className: 'btn btn-primary',
            onClick: send
          }, 'Send \u2192')
        )
      )
    );
  }

  // ==========================================================================
  // Index tab — paginated browsable index of memories + news with filters
  // ==========================================================================

  const indexUI = {
    tab: 'memories',          // 'memories' | 'news'
    query: '',
    project: null,            // null = all
    type: null,               // null = all
    page: 1,
    expandedTags: new Set(),  // memory IDs whose tag overflow is expanded
    memories: [],
    news: [],
    stats: null
  };

  const INDEX_PAGE_SIZE = 50;

  function renderIndex() {
    setChildren(mainEl, [
      h('div', {
        id: 'index-content',
        style: { display: 'flex', flexDirection: 'column', gap: '0' }
      },
        h('div', {
          className: 'eyebrow',
          style: { padding: '40px 0', textAlign: 'center', color: 'var(--text-faint)' }
        }, 'loading\u2026')
      )
    ]);

    Promise.all([
      api('/api/memories').catch(() => []),
      api('/api/news').catch(() => []),
      api('/api/stats').catch(() => null)
    ]).then(function (results) {
      // Sort newest-first by stored_at so the list reads chronologically
      // descending. /api/memories doesn't guarantee any order.
      const byStoredAtDesc = function (a, b) {
        return (b.stored_at || '').localeCompare(a.stored_at || '');
      };
      indexUI.memories = (results[0] || []).slice().sort(byStoredAtDesc);
      indexUI.news = (results[1] || []).slice().sort(byStoredAtDesc);
      indexUI.stats = results[2] || null;
      paintIndex();
    }).catch(function (e) {
      const t = mountRoot.querySelector('#index-content');
      if (t) t.textContent = 'error loading index: ' + e.message;
    });
  }

  function paintIndex() {
    const target = mountRoot.querySelector('#index-content');
    if (!target) return;

    const items = indexUI.tab === 'memories' ? indexUI.memories : indexUI.news;

    // Filter
    const q = (indexUI.query || '').toLowerCase();
    const filtered = items.filter(function (m) {
      if (indexUI.project && m.project !== indexUI.project) return false;
      if (indexUI.type && m.type !== indexUI.type) return false;
      if (q) {
        const blob = ((m.content || '') + ' ' + (m.tags || '') + ' ' + (m.id || '')).toLowerCase();
        if (blob.indexOf(q) === -1) return false;
      }
      return true;
    });

    // Pagination
    const totalPages = Math.max(1, Math.ceil(filtered.length / INDEX_PAGE_SIZE));
    if (indexUI.page > totalPages) indexUI.page = totalPages;
    if (indexUI.page < 1) indexUI.page = 1;
    const startIdx = (indexUI.page - 1) * INDEX_PAGE_SIZE;
    const pageItems = filtered.slice(startIdx, startIdx + INDEX_PAGE_SIZE);

    // Project / type chip data — pulled from /api/stats
    const stats = indexUI.stats || {};
    const byProject = stats.by_project || {};
    const byType = stats.by_type || {};
    const projects = Object.keys(byProject).sort(function (a, b) { return byProject[b] - byProject[a]; });
    const types = Object.keys(byType).sort(function (a, b) { return byType[b] - byType[a]; }).slice(0, 14);

    setChildren(target, [
      window.Card({ children: [
        window.PanelHeader({
          title: 'Index',
          sub: (indexUI.memories.length + indexUI.news.length) + ' entries \u00B7 paginated ' + INDEX_PAGE_SIZE + '/page',
          right: h('div', { className: 'flex items-center gap-2' },
            h('button', {
              className: 'btn',
              style: { fontSize: '11px' },
              onClick: function () { exportIndex('json', filtered); }
            }, '\u2913 Export JSON'),
            h('button', {
              className: 'btn',
              style: { fontSize: '11px' },
              onClick: function () { exportIndex('md', filtered); }
            }, '\u2913 Export Markdown')
          )
        }),
        // Tabs row (memories vs news)
        h('div', {
          style: { padding: '6px 12px 0 12px', borderBottom: '1px solid var(--line)' }
        },
          window.Tabs({
            tabs: [
              { id: 'memories', label: 'Memories', count: indexUI.memories.length },
              { id: 'news',     label: 'News',     count: indexUI.news.length }
            ],
            value: indexUI.tab,
            onChange: function (id) {
              indexUI.tab = id;
              indexUI.page = 1;
              paintIndex();
            }
          })
        ),
        // Search row
        searchRow(filtered.length, items.length),
        // Project chips
        chipRow('project', indexUI.project, projects, byProject, function (p) {
          indexUI.project = p;
          indexUI.page = 1;
          paintIndex();
        }),
        // Type chips
        typeChipRow(indexUI.type, types, function (t) {
          indexUI.type = t;
          indexUI.page = 1;
          paintIndex();
        }),
        // Item list
        h('div', {
          className: 'density-pad',
          style: { display: 'flex', flexDirection: 'column', gap: '8px' }
        },
          pageItems.length > 0
            ? pageItems.map(function (m) { return indexCard(m); })
            : h('div', {
                className: 'font-mono',
                style: {
                  textAlign: 'center', padding: '32px 0',
                  fontSize: '11.5px', color: 'var(--text-faint)'
                }
              }, 'no matches \u00B7 clear filters')
        ),
        // Pagination footer
        paginationFooter(filtered.length, totalPages)
      ]})
    ]);

    // Restore search input focus after re-render.
    if (indexUI.query) {
      const searchEl = target.querySelector('input.input');
      if (searchEl && document.activeElement !== searchEl) {
        searchEl.focus();
        searchEl.setSelectionRange(indexUI.query.length, indexUI.query.length);
      }
    }
  }

  function searchRow(filteredCount, totalCount) {
    return h('div', {
      className: 'flex items-center gap-2',
      style: { padding: '8px 12px', borderBottom: '1px solid var(--line)' }
    },
      h('input', {
        className: 'input',
        style: { flex: '1' },
        placeholder: 'search content / tag / id\u2026',
        value: indexUI.query,
        onInput: function (e) {
          indexUI.query = e.target.value;
          indexUI.page = 1;
          paintIndex();
        }
      }),
      h('span', {
        className: 'font-mono',
        style: { fontSize: '10.5px', color: 'var(--text-faint)' }
      }, filteredCount + ' / ' + totalCount)
    );
  }

  function chipRow(label, current, options, counts, onPick) {
    const activeStyle = {
      background: 'var(--accent-ghost)',
      color: 'var(--accent-press)',
      borderColor: 'color-mix(in oklch, var(--accent) 25%, var(--line))'
    };
    return h('div', {
      className: 'flex items-center',
      style: {
        padding: '8px 12px', gap: '6px', flexWrap: 'wrap',
        borderBottom: '1px solid var(--line)'
      }
    },
      h('span', { className: 'eyebrow', style: { marginRight: '4px' } }, label),
      h('button', {
        className: 'chip',
        style: !current ? activeStyle : {},
        onClick: function () { onPick(null); }
      }, 'all'),
      options.map(function (opt) {
        const active = current === opt;
        return h('button', {
          className: 'chip',
          style: active ? activeStyle : {},
          onClick: function () { onPick(active ? null : opt); }
        },
          opt,
          h('span', {
            style: { color: 'var(--text-faint)', marginLeft: '3px' }
          }, counts[opt] || 0)
        );
      })
    );
  }

  function typeChipRow(current, types, onPick) {
    const activeStyle = {
      background: 'var(--accent-ghost)',
      color: 'var(--accent-press)',
      borderColor: 'color-mix(in oklch, var(--accent) 25%, var(--line))'
    };
    return h('div', {
      className: 'flex items-center',
      style: {
        padding: '8px 12px', gap: '6px', flexWrap: 'wrap',
        borderBottom: '1px solid var(--line)'
      }
    },
      h('span', { className: 'eyebrow', style: { marginRight: '4px' } }, 'type'),
      h('button', {
        className: 'chip',
        style: !current ? activeStyle : {},
        onClick: function () { onPick(null); }
      }, 'all'),
      types.map(function (t) {
        const active = current === t;
        return h('button', {
          style: active ? Object.assign({ padding: '0', border: 'none', background: 'transparent' }, activeStyle)
                       : { padding: '0', border: 'none', background: 'transparent' },
          onClick: function () { onPick(active ? null : t); }
        },
          window.TypeBadge({ type: t })
        );
      })
    );
  }

  function indexCard(m) {
    const tags = (m.tags || '').split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    const open = indexUI.expandedTags.has(m.id);
    const visibleTags = open ? tags : tags.slice(0, 6);
    const hiddenCount = tags.length - 6;

    return h('div', {
      className: 'card-sunk copy-host density-pad',
      style: { background: 'var(--bg-inset)' }
    },
      h('div', {
        className: 'flex items-center gap-2',
        style: { flexWrap: 'wrap', marginBottom: '6px' }
      },
        window.TypeBadge({ type: m.type || 'note' }),
        h('span', { className: 'chip' }, m.project || 'general'),
        visibleTags.map(function (t) { return window.Chip({ children: t }); }),
        !open && hiddenCount > 0 ? h('button', {
          className: 'chip',
          onClick: function () {
            indexUI.expandedTags.add(m.id);
            paintIndex();
          }
        }, '+' + hiddenCount) : null,
        h('span', {
          className: 'flex items-center gap-2',
          style: { marginLeft: 'auto' }
        },
          window.CopyBtn({ text: m.content || '' }),
          h('span', {
            className: 'font-mono',
            style: { fontSize: '10.5px', color: 'var(--text-faint)' },
            title: window.absTime(m.stored_at)
          }, relTime(m.stored_at))
        )
      ),
      window.MD({ source: m.content || '' }),
      h('div', {
        className: 'font-mono',
        style: { marginTop: '6px', fontSize: '10.5px', color: 'var(--text-faint)' }
      }, m.id)
    );
  }

  function paginationFooter(filteredCount, totalPages) {
    return h('div', {
      className: 'density-pad flex items-center justify-between',
      style: { borderTop: '1px solid var(--line)' }
    },
      h('span', {
        className: 'font-mono',
        style: { fontSize: '10.5px', color: 'var(--text-faint)' }
      }, 'page ' + indexUI.page + ' of ' + totalPages + ' \u00B7 ' + filteredCount + ' filtered'),
      h('div', { className: 'flex gap-1' },
        h('button', {
          className: 'btn',
          style: { fontSize: '11px' },
          disabled: indexUI.page <= 1,
          onClick: function () {
            if (indexUI.page > 1) {
              indexUI.page--;
              paintIndex();
              window.scrollTo({ top: 0, behavior: 'smooth' });
            }
          }
        }, '\u2190 prev'),
        h('button', {
          className: 'btn',
          style: { fontSize: '11px' },
          disabled: indexUI.page >= totalPages,
          onClick: function () {
            if (indexUI.page < totalPages) {
              indexUI.page++;
              paintIndex();
              window.scrollTo({ top: 0, behavior: 'smooth' });
            }
          }
        }, 'next \u2192')
      )
    );
  }

  function exportIndex(format, items) {
    let blob, filename;
    if (format === 'json') {
      blob = new Blob([JSON.stringify(items, null, 2)], { type: 'application/json' });
      filename = 'memory-' + indexUI.tab + '-' + new Date().toISOString().slice(0, 10) + '.json';
    } else {
      // Markdown — one entry per ## section
      const md = items.map(function (m) {
        const tags = (m.tags || '').split(',').map(function (s) { return s.trim(); }).filter(Boolean);
        return '## ' + (m.id || '?') + '\n\n' +
               '- type: `' + (m.type || '') + '`\n' +
               '- project: `' + (m.project || '') + '`\n' +
               '- stored: ' + (m.stored_at || '') + '\n' +
               (tags.length ? '- tags: ' + tags.map(function (t) { return '`' + t + '`'; }).join(', ') + '\n' : '') +
               '\n' + (m.content || '') + '\n';
      }).join('\n---\n\n');
      blob = new Blob([md], { type: 'text/markdown' });
      filename = 'memory-' + indexUI.tab + '-' + new Date().toISOString().slice(0, 10) + '.md';
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 0);
  }

  // ==========================================================================
  // Export
  // ==========================================================================

  window.App = { init, setRoute, state, api, subscribe };
})();
