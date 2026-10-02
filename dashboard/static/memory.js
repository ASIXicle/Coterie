/* persMEM Dashboard v3 — vanilla JS bundle.
   Sections: helpers, h() element builder, markdown, primitives, exports.
   No framework. Attaches everything to window.
*/
(function () {
  'use strict';

  // ==========================================================================
  // Time helpers
  // ==========================================================================

  function relTime(iso) {
    const t = new Date(iso).getTime();
    const now = (window.DASHBOARD && window.DASHBOARD.NOW || new Date()).getTime();
    const s = Math.max(0, Math.round((now - t) / 1000));
    if (s < 60) return s + 's ago';
    const m = Math.round(s / 60);
    if (m < 60) return m + 'm ago';
    const h = Math.round(m / 60);
    if (h < 24) return h + 'h ago';
    const d = Math.round(h / 24);
    return d + 'd ago';
  }

  function absTime(iso) {
    const d = new Date(iso);
    return d.toISOString().replace('T', ' ').slice(0, 19) + 'Z';
  }

  function fmtDuration(sec) {
    sec = Math.round(sec);
    const d = Math.floor(sec / 86400); sec %= 86400;
    const h = Math.floor(sec / 3600);  sec %= 3600;
    const m = Math.floor(sec / 60);    sec %= 60;
    const out = [];
    if (d) out.push(d + 'd');
    if (h) out.push(h + 'h');
    if (m && !d) out.push(m + 'm');
    if (!d && !h && !m) out.push(sec + 's');
    return out.join(' ');
  }

  function countdown(iso) {
    const t = new Date(iso).getTime();
    const now = (window.DASHBOARD && window.DASHBOARD.NOW || new Date()).getTime();
    const s = Math.max(0, Math.round((t - now) / 1000));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h ? h + 'h ' + m + 'm' : m + 'm';
  }

  // ==========================================================================
  // h() — DOM element builder. Replaces JSX createElement.
  //
  // Usage:  h('div', { className: 'card', onClick: fn }, 'text', otherEl, [...])
  // Returns a real DOM Element you append to the document or another element.
  // Children may be: strings, numbers, Nodes, arrays of any of these, null/false.
  // ==========================================================================

  function h(tag, props /* ...children */) {
    const children = Array.prototype.slice.call(arguments, 2);
    const el = document.createElement(tag);
    if (props) applyProps(el, props);
    appendChildren(el, children);
    return el;
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';
  function hSvg(tag, props /* ...children */) {
    const children = Array.prototype.slice.call(arguments, 2);
    const el = document.createElementNS(SVG_NS, tag);
    if (props) {
      for (const k in props) {
        const v = props[k];
        if (v == null || v === false || k === 'key') continue;
        if (k.startsWith('on') && typeof v === 'function') {
          el.addEventListener(k.slice(2).toLowerCase(), v);
        } else {
          el.setAttribute(k, v);
        }
      }
    }
    appendChildren(el, children);
    return el;
  }

  function applyProps(el, props) {
    for (const k in props) {
      const v = props[k];
      if (v == null || v === false || k === 'key') continue;
      if (k === 'class' || k === 'className') {
        el.className = v;
      } else if (k === 'style' && typeof v === 'object') {
        for (const sk in v) {
          // custom properties (--agent-h etc.) need setProperty; el.style['--x'] is a no-op
          if (sk.startsWith('--')) el.style.setProperty(sk, v[sk]);
          else el.style[sk] = v[sk];
        }
      } else if (k === 'dangerouslySetInnerHTML') {
        el.innerHTML = (v && v.__html) || '';
      } else if (k.startsWith('on') && typeof v === 'function') {
        el.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (k === 'ref' && typeof v === 'function') {
        v(el);
      } else {
        el.setAttribute(k, v === true ? '' : v);
      }
    }
  }

  function appendChildren(el, children) {
    for (let i = 0; i < children.length; i++) {
      const c = children[i];
      if (c == null || c === false || c === true) continue;
      if (Array.isArray(c)) { appendChildren(el, c); continue; }
      if (c instanceof Node) { el.appendChild(c); continue; }
      el.appendChild(document.createTextNode(String(c)));
    }
  }

  // Convenience: empty an element and replace its children.
  function setChildren(el, children) {
    while (el.firstChild) el.removeChild(el.firstChild);
    appendChildren(el, Array.isArray(children) ? children : [children]);
  }

  // ==========================================================================
  // Markdown → HTML (very small, safe escaping)
  // ==========================================================================

  function mdToHtml(src) {
    if (!src) return '';
    const escape = (s) => s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
    const inline = (s) =>
      escape(s)
        .replace(/`([^`]+?)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|\W)\*([^*]+?)\*(?=\W|$)/g, '$1<em>$2</em>');
    const lines = src.split('\n');
    let out = '';
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*$/.test(line)) { i++; continue; }
      if (/^>\s/.test(line)) {
        let block = '';
        while (i < lines.length && /^>\s?/.test(lines[i])) {
          block += inline(lines[i].replace(/^>\s?/, '')) + ' ';
          i++;
        }
        out += '<blockquote>' + block.trim() + '</blockquote>';
        continue;
      }
      if (/^\s*[-*]\s/.test(line)) {
        out += '<ul>';
        while (i < lines.length && /^\s*[-*]\s/.test(lines[i])) {
          out += '<li>' + inline(lines[i].replace(/^\s*[-*]\s/, '')) + '</li>';
          i++;
        }
        out += '</ul>';
        continue;
      }
      if (/^\s*\d+\.\s/.test(line)) {
        out += '<ol>';
        while (i < lines.length && /^\s*\d+\.\s/.test(lines[i])) {
          out += '<li>' + inline(lines[i].replace(/^\s*\d+\.\s/, '')) + '</li>';
          i++;
        }
        out += '</ol>';
        continue;
      }
      let para = '';
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*[-*]\s/.test(lines[i]) && !/^\s*\d+\.\s/.test(lines[i]) && !/^>\s/.test(lines[i])) {
        para += (para ? ' ' : '') + lines[i];
        i++;
      }
      out += '<p>' + inline(para) + '</p>';
    }
    return out;
  }

  function MD(props) {
    return h('div', {
      className: 'md ' + (props.className || ''),
      dangerouslySetInnerHTML: { __html: mdToHtml(props.source || '') }
    });
  }

  // ==========================================================================
  // Primitives
  // ==========================================================================

  function Card(props) {
    const tag = props.tag || 'div';
    const children = props.children || [];
    return h(tag, { className: 'card ' + (props.className || '') }, children);
  }

  function PanelHeader(props) {
    return h('div', {
      className: 'flex items-center justify-between density-pad',
      style: { borderBottom: '1px solid var(--line)' }
    },
      h('div', { className: 'flex items-baseline gap-3' },
        h('span', { className: 'panel-title flex items-center gap-2' },
          props.icon && h('span', { 'aria-hidden': 'true', style: { color: 'var(--text-quiet)' } }, props.icon),
          props.title
        ),
        props.sub && h('span', { style: { fontSize: '11px', color: 'var(--text-faint)' } }, props.sub)
      ),
      props.right && h('div', { className: 'flex items-center gap-2' }, props.right)
    );
  }

  function Chip(props) {
    return h('span', {
      className: 'chip ' + (props.accent ? 'chip-accent ' : '') + (props.className || '')
    }, props.children || []);
  }

  // Six named categories carry a hue; everything else takes the neutral fallback
  // and reads as quiet rather than disabled -- which is what the three entries that
  // set their ink to var(--text-body) over a coloured wash were trying to do.
  //
  // A badge is TEXT on a tint, so it needs 4.5:1, which is why it cannot reuse the
  // --series-* ladder built for 3.0 fills. tokens.css derives --type-wash/--type-ink
  // on [data-type]; measured 6.22-7.39 light, 9.10-9.78 dark.
  const TYPE_HUES = {
    decision: 248,
    session_summary: 175,
    insight: 310,
    bug: 25,
    architecture: 145,
    code_change: 200,
  };

  function TypeBadge(props) {
    const hue = TYPE_HUES[props.type];
    return h('span', {
      className: 'type-badge ' + (props.className || ''),
      'data-type': props.type,          // required: tokens.css hangs --type-* on it
      style: Object.assign(
        { background: 'var(--type-wash)', color: 'var(--type-ink)', borderColor: 'transparent' },
        hue == null ? {} : { '--type-h': String(hue) })
    }, props.type);
  }

  function CopyBtn(props) {
    const label = props.label || 'copy';
    let timer = null;
    const btn = h('button', {
      className: 'copy-btn btn btn-ghost',
      style: { fontSize: '11px', padding: '3px 7px' },
      onClick: (e) => {
        e.stopPropagation();
        try { navigator.clipboard && navigator.clipboard.writeText(props.text || ''); } catch (_) {}
        btn.textContent = '✓ copied';
        clearTimeout(timer);
        timer = setTimeout(() => { btn.textContent = label; }, 900);
      }
    }, label);
    return btn;
  }

  // Per-agent identity is roster-driven: the hue comes inline from the roster file
  // (served as sys.roster), so no per-agent CSS exists and adding an agent needs no
  // CSS edit. A name that is not on the roster gets no hue and falls back to the
  // neutral default in the consumers (var(--id-edge, var(--text-quiet))).
  // Identity is a HUE, not a colour. tokens.css derives --id-wash / --id-text /
  // --id-fill from it in [data-agent] blocks and retunes them per theme; a raw hex
  // cannot be retuned, which is why the old --c-line/--c-bg pair broke in dark
  // (one agent's border measured 2.86:1 on the dark panel) and why --c-ink was declared
  // and never consumed -- a hex could not safely carry text.
  //
  // THE ELEMENT GETTING THESE MUST ALSO CARRY data-agent. A var() inside a custom
  // property is substituted where the property is DECLARED, so without the attribute
  // the hue is out of scope and every agent renders the neutral fallback.
  function agentColorVars(name, hue) {
    if (hue == null) return {};      // news, echo, the operator, retired seats
    return { '--agent-h': String(hue) };
  }

  function AgentBadge(props) {
    return h('span', {
      className: 'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full no-sel',
      'data-agent': props.name,          // required: tokens.css hangs --id-* on it
      style: Object.assign({
        background: props.active ? 'var(--id-wash, var(--bg-well))' : 'transparent',
        border: '1px solid ' + (props.active ? 'var(--id-edge, var(--text-quiet))' : 'var(--line)'),
        fontSize: '11.5px',
      }, agentColorVars(props.name, props.hue))
    },
      h('span', { className: 'agent-dot' }),
      h('span', { style: { color: 'var(--text-body)', fontWeight: '500' } }, props.name),
      props.count != null && h('span', {
        className: 'font-mono',
        style: { color: 'var(--text-quiet)', fontSize: '10.5px' }
      }, props.count)
    );
  }

  function StatusDot(props) {
    const kind = props.kind || 'ok';
    // an 8px dot: non-text, so the -fill variants (3.0 bar), never the -text ones
    const c = kind === 'ok' ? 'var(--ok-fill)' : kind === 'warn' ? 'var(--warn-fill)' : 'var(--err-fill)';
    return h('span', {
      style: {
        display: 'inline-block', width: '8px', height: '8px', borderRadius: '999px',
        background: c, boxShadow: '0 0 0 2px color-mix(in oklch, ' + c + ' 16%, transparent)'
      }
    });
  }

  function MicroSpark(props) {
    const data = props.data || [];
    const color = props.color || 'var(--accent)';
    const height = props.height || 22;
    const width = props.width || 80;
    if (data.length < 2) return hSvg('svg', { width: width, height: height });
    const max = Math.max.apply(null, data.concat([1]));
    const pts = data.map((v, i) =>
      ((i / (data.length - 1)) * width) + ',' + (height - (v / max) * (height - 2) - 1)
    ).join(' ');
    return hSvg('svg', { width: width, height: height, viewBox: '0 0 ' + width + ' ' + height, 'aria-hidden': 'true' },
      hSvg('polyline', {
        points: pts, fill: 'none', stroke: color,
        'stroke-width': '1.25', 'stroke-linejoin': 'round', 'stroke-linecap': 'round'
      })
    );
  }

  function ShareBar(props) {
    const value = props.value || 0;
    const total = props.total || 1;
    const color = props.color || 'var(--accent)';
    const pct = Math.min(1, value / Math.max(1, total));
    return h('div', {
      style: {
        height: '3px', background: 'var(--bg-well)',
        borderRadius: '2px', overflow: 'hidden'
      }
    },
      h('div', {
        style: {
          width: (pct * 100) + '%', height: '100%', background: color,
          transition: 'width 600ms cubic-bezier(.2,.8,.2,1)'
        }
      })
    );
  }

  function IconBtn(props) {
    return h('button', {
      className: 'btn btn-ghost ' + (props.className || ''),
      style: { padding: '5px 7px' },
      title: props.title || '',
      onClick: props.onClick
    }, props.children || []);
  }

  // Tabs renders a strip of tab buttons. Active tab gets `tab-active` class.
  // tabs: [{ id, label, count? }]
  // value: current active id
  // onChange: (newId) => void
  function Tabs(props) {
    return h('div', {
      className: 'flex items-center gap-1 px-2',
      style: { borderBottom: '1px solid var(--line)' }
    },
      props.tabs.map(t =>
        h('button', {
          className: 'px-3 py-2 ' + (props.value === t.id ? 'tab-active' : ''),
          style: {
            fontSize: '13px', fontWeight: '500',
            color: props.value === t.id ? 'var(--text-strong)' : 'var(--text-quiet)',
            background: 'transparent', border: 'none'
          },
          onClick: () => props.onChange(t.id)
        },
          t.label,
          t.count != null && h('span', {
            className: 'font-mono ml-1.5',
            style: { color: 'var(--text-faint)', fontSize: '11px' }
          }, t.count)
        )
      )
    );
  }

  function SparkBars(props) {
    const days = props.days || [];
    const height = props.height || 84;
    const max = Math.max.apply(null, days.map(d => d.memories + d.amq + d.news).concat([1]));
    return h('div', {
      className: 'flex items-end',
      style: { gap: '3px', height: height + 'px', width: '100%' }
    },
      days.map((d) => {
        const real = d.memories + d.amq + d.news;
        // A day with nothing is a FACT, and it used to render as a 1px empty sliver --
        // indistinguishable from a day with no data at all. It gets a baseline tick.
        if (!real) {
          return h('div', {
            title: d.date + ': nothing',
            style: { flex: '1 1 0', minWidth: '0', height: '2px', background: 'var(--rule-hair)' }
          });
        }
        const total = real;
        const totalH = (total / max) * height;
        return h('div', {
          className: 'flex flex-col-reverse',
          title: d.date + ': ' + real,
          style: { flex: '1 1 0', minWidth: '0', height: totalH + 'px' }
        },
          h('div', { style: { height: ((d.amq / total) * totalH) + 'px', background: 'var(--series-1)' } }),
          h('div', { style: { height: ((d.memories / total) * totalH) + 'px', background: 'var(--series-0)' } }),
          h('div', { style: { height: ((d.news / total) * totalH) + 'px', background: 'var(--series-2)' } })
        );
      })
    );
  }

  // ==========================================================================
  // Export to window
  // ==========================================================================

  Object.assign(window, {
    h, hSvg, setChildren,
    relTime, absTime, fmtDuration, countdown,
    MD, mdToHtml,
    Card, PanelHeader, Chip, TypeBadge, CopyBtn, AgentBadge, agentColorVars,
    StatusDot, MicroSpark, ShareBar, IconBtn, Tabs, SparkBars,
    TYPE_HUES,
  });
})();
