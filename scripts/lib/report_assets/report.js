(function () {
'use strict';
// sessionretro · скрипт отчёта. Данные — JSON в #data (уже отфильтрован по режиму в Python).
// Строки интерфейса — только функцией t с литеральным ключом секции report; имена осей,
// уровней, вердиктов и статусов — прямым чтением чужих секций словаря. Данные в DOM —
// через esc() или textContent; через innerHTML идут только поля *_html.
const DATA = JSON.parse(document.getElementById('data').textContent);
const I18N = DATA.i18n || {};
const META = DATA.meta;
const MODE = META.mode;
const PERSONAL = MODE === 'personal';
const GROUP = MODE === 'group';
const REVIEWER = MODE === 'reviewer';
const LANGS = ['ru', 'en'];
const STORE_KEY = 'sessionretro-ui';
const S = {
  lang: LANGS.indexOf(META.lang) >= 0 ? META.lang : 'ru', theme: 'auto',
  view: PERSONAL ? 'person' : 'overview', person: PERSONAL && DATA.people[0] ? DATA.people[0].id : null,
  axis: 'all', period: 'last', search: '', run: -1, review: null,
  asPerson: PERSONAL, showScores: !!META.show_scores, filtersOpen: false,
};

try {
  const st = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
  if (st.theme === 'light' || st.theme === 'dark') S.theme = st.theme;
  if (LANGS.indexOf(st.lang) >= 0) S.lang = st.lang;
} catch (e) { /* хранилище недоступно: работаем без запомненных настроек */ }

function persist() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify({ theme: S.theme, lang: S.lang })); }
  catch (e) { /* хранилище недоступно: настройка живёт до перезагрузки */ }
}

function applyTheme() {
  const root = document.documentElement;
  if (S.theme === 'auto') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', S.theme);
}

// --- словарь ---
// Строка интерфейса из секции report; нет ключа — сам ключ на экране и предупреждение в консоли.
const t = key => {
  const entry = (I18N.report || {})[key];
  if (!entry) { console.warn('sessionretro: нет строки интерфейса', key); return key; }
  return entry[S.lang] || entry.ru || key;
};
const humanize = id => String(id == null ? '' : id).replace(/_/g, ' ');
function pick(entry, id, section) {
  if (!entry) { console.warn('sessionretro: нет перевода', section, id); return humanize(id); }
  return entry[S.lang] || entry.ru || humanize(id);
}
const axName = id => pick((I18N.axes || {})[id], id, 'axes');
const lvlName = id => (id ? pick((I18N.levels || {})[id], id, 'levels') : '');
const recVerdictName = v => pick((I18N.rec_verdicts || {})[v], v, 'rec_verdicts');
const recStatusName = v => pick((I18N.rec_statuses || {})[v], v, 'rec_statuses');
const reviewStatusName = v => pick((I18N.review_statuses || {})[v], v, 'review_statuses');
// Причины «данных не было»: известные коды — своей строкой, прочие — как есть, «_» → пробел.
function reasonName(code) {
  if (!code) return t('no_data');
  const known = {
    no_context_report: t('reason_no_context_report'), no_repo_access: t('reason_no_repo_access'),
    log_truncated: t('reason_log_truncated'), nothing_to_verify: t('reason_nothing_to_verify'),
    quote_unverified: t('reason_quote_unverified'),
    behaviour_absent: t('reason_behaviour_absent'), work_outside_transcript: t('reason_work_outside_transcript'),
    tool_has_no_trace: t('reason_tool_has_no_trace'), short_log: t('reason_short_log'),
    other: t('reason_other'),
  };
  return known[code] || humanize(code);
}

function esc(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function fmtDate(iso) {
  if (!iso) return '';
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/); if (!m) return String(iso);
  return S.lang === 'en' ? `${m[1]}-${m[2]}-${m[3]}` : `${m[3]}.${m[2]}.${m[1]}`;
}
const MONTHS_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function fmtShort(iso) {
  if (!iso) return '';
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/); if (!m) return String(iso);
  return S.lang === 'en' ? `${parseInt(m[3], 10)} ${MONTHS_EN[parseInt(m[2], 10) - 1]}` : `${m[3]}.${m[2]}`;
}
function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
function person() { return DATA.people.find(p => p.id === S.person) || null; }
function lastIndex(p) { return p.points.length - 1; }
function runIndex(p) { return S.run >= 0 && S.run < p.points.length ? S.run : lastIndex(p); }
function sessShort(id) { return id ? String(id).slice(0, 12) : ''; }
function sum(list, f) { return list.reduce((n, x) => n + (f(x) || 0), 0); }
function recStatus(r) {
  if (r.status) return r.status;
  let st = 'open';
  (r.verdicts || []).forEach(v => { if (v.verdict === 'followed' || v.verdict === 'ignored') st = v.verdict; });
  return st;
}
const CHIP_CLASS = { followed: 'good', partial: 'warn', ignored: 'bad', not_observable: 'neutral', open: 'open', retired: 'neutral' };
const CHIP_GLYPH = { followed: '✓', partial: '◐', ignored: '✕', not_observable: '○', open: '●', retired: '·' };

// --- тултип: мышь, фокус с клавиатуры ---
const tip = document.getElementById('tip');
function showTip(target, x, y) {
  tip.textContent = target.getAttribute('data-tip') || '';
  if (!tip.textContent) return;
  tip.hidden = false; placeTip(x, y, target);
}
function placeTip(x, y, target) {
  let px = x, py = y;
  if (px == null && target) { const r = target.getBoundingClientRect(); px = r.left + r.width / 2; py = r.bottom; }
  const w = tip.offsetWidth, h = tip.offsetHeight, vw = window.innerWidth, vh = window.innerHeight;
  let left = px + 14, top = py + 16;
  if (left + w > vw - 8) left = Math.max(8, px - w - 14);
  if (top + h > vh - 8) top = Math.max(8, py - h - 12);
  tip.style.left = left + 'px'; tip.style.top = top + 'px';
}
function hideTip() { tip.hidden = true; }
const tipTarget = e => (e.target && e.target.closest ? e.target.closest('[data-tip]') : null);
document.addEventListener('pointerover', e => { const tg = tipTarget(e); if (tg) showTip(tg, e.clientX, e.clientY); else hideTip(); });
document.addEventListener('pointermove', e => { if (!tip.hidden) placeTip(e.clientX, e.clientY, null); });
document.addEventListener('pointerout', e => { const tg = tipTarget(e); if (tg && !(e.relatedTarget && tg.contains(e.relatedTarget))) hideTip(); });
document.addEventListener('focusin', e => { const tg = tipTarget(e); if (tg) showTip(tg, null, null); else hideTip(); });
document.addEventListener('focusout', hideTip);
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideTip(); });
document.addEventListener('scroll', hideTip, true);

// --- контролы ---
function head(title, hint) {
  const h = el('div', 'card-head'); h.appendChild(el('h2', null, esc(title)));
  if (hint) h.appendChild(el('span', 'hint', esc(hint)));
  return h;
}
function field(label, control) {
  const f = el('div', 'field'); const l = el('label', null, esc(label)); l.htmlFor = control.id;
  f.appendChild(l); f.appendChild(control); return f;
}
function selectEl(id, options, value, onchange) {
  const s = el('select'); s.id = 'f-' + id;
  options.forEach(([v, l]) => { const o = el('option'); o.value = String(v); o.textContent = l; s.appendChild(o); });
  s.value = String(value); s.onchange = e => onchange(e.target.value); return s;
}
function inputEl(id, value, ph, oninput) {
  const i = el('input'); i.id = 'f-' + id; i.type = 'search'; i.value = value; i.placeholder = ph;
  i.setAttribute('autocomplete', 'off'); i.oninput = e => oninput(e.target.value); return i;
}
function wrapBtn(b) { const f = el('div', 'field'); f.appendChild(el('label', null, '&nbsp;')); f.appendChild(b); return f; }
function button(label, pressed, onclick) {
  const b = el('button', 'btn'); b.type = 'button'; b.textContent = label;
  if (pressed != null) b.setAttribute('aria-pressed', String(pressed));
  b.onclick = onclick; return b;
}
function axisSelect() {
  return field(t('filter_axis'), selectEl('axis', [['all', t('all_axes')]].concat(DATA.axes.map(a => [a, axName(a)])), S.axis, v => { S.axis = v; render(); }));
}

// --- шапка ---
function modeLabel() {
  const labels = { group: t('mode_group'), reviewer: t('mode_reviewer'), personal: t('mode_personal') };
  return labels[MODE] || MODE;
}
function rangeText(from, to) {
  return from ? fmtDate(from) + (to && to !== from ? ' – ' + fmtDate(to) : '') : '';
}
// Даты шапки: при date_source 'logs' это даты логов периода, подписи про логи, а даты запуска
// оценки (run_from/run_to есть только в отчёте ревьюера) идут мелкой припиской; без дат логов —
// даты запусков и прежние подписи.
function dateLabels(m) {
  const logs = m.date_source === 'logs';
  return {
    range: logs ? t('logs_range') : t('period_range'),
    tile: logs ? t('tile_period_logs') : t('tile_period'),
    dates: rangeText(m.date_from, m.date_to),
    run: logs && m.run_from ? rangeText(m.run_from, m.run_to) : '',
  };
}
function topbar() {
  const m = META;
  const box = el('header', 'topbar');
  const brand = el('div', 'brand');
  brand.appendChild(el('h1', null, esc(m.title || m.workspace_title)));
  if (m.workspace_title && m.workspace_title !== m.title) {
    // Подзаголовок, который уже есть в заголовке, на узком экране прячется (класс dup).
    const dup = String(m.title || '').indexOf(m.workspace_title) >= 0;
    brand.appendChild(el('div', 'sub' + (dup ? ' dup' : ''), esc(m.workspace_title)));
  }
  const dl = dateLabels(m);
  const bits = [`<span class="chip">${esc(modeLabel())}</span>`];
  if (dl.dates) bits.push(`${esc(dl.range)}: <span class="tnum">${esc(dl.dates)}</span>`);
  if (dl.run) bits.push(`<span class="muted small">${esc(t('run_range'))}: <span class="tnum">${esc(dl.run)}</span></span>`);
  if (m.generated_at) bits.push(`${esc(t('generated'))} <span class="tnum">${esc(fmtDate(m.generated_at))}</span>`);
  brand.appendChild(el('div', 'meta', bits.join(' · ')));
  const flags = el('div', 'chips');
  if (GROUP) flags.appendChild(el('span', 'banner', esc(t('not_for_distribution'))));
  if (REVIEWER) flags.appendChild(el('span', 'banner', esc(t('reviewer_banner'))));
  if (REVIEWER && m.reviews_unbound) {
    const u = el('span', 'chip warn', `${esc(t('unbound_reviews'))}: <b class="tnum">${esc(m.reviews_unbound)}</b>`);
    u.setAttribute('data-tip', t('unbound_reviews_tip')); u.tabIndex = 0; flags.appendChild(u);
  }
  if (flags.childNodes.length) brand.appendChild(flags);
  box.appendChild(brand);

  const tb = el('div', 'toolbar');
  const langSeg = el('div', 'seg'); langSeg.setAttribute('role', 'group'); langSeg.setAttribute('aria-label', t('lang'));
  LANGS.forEach(l => {
    const b = el('button'); b.type = 'button'; b.textContent = l.toUpperCase(); b.setAttribute('aria-pressed', String(S.lang === l));
    b.onclick = () => { S.lang = l; persist(); render(); }; langSeg.appendChild(b);
  });
  tb.appendChild(langSeg);
  const themeSeg = el('div', 'seg'); themeSeg.setAttribute('role', 'group'); themeSeg.setAttribute('aria-label', t('theme'));
  // На узком экране вместо подписи — значок (подпись остаётся в aria-label).
  const THEME_GLYPH = { auto: '◐', light: '○', dark: '●' };
  [['auto', t('theme_auto')], ['light', t('theme_light')], ['dark', t('theme_dark')]].forEach(([v, label]) => {
    const b = el('button', null, `<span class="lbl">${esc(label)}</span><span class="ico" aria-hidden="true">${THEME_GLYPH[v]}</span>`);
    b.type = 'button'; b.setAttribute('aria-label', label); b.setAttribute('aria-pressed', String(S.theme === v));
    b.onclick = () => { S.theme = v; applyTheme(); persist(); render(); }; themeSeg.appendChild(b);
  });
  tb.appendChild(themeSeg);
  box.appendChild(tb);
  return box;
}

// --- обзор (group, reviewer) ---
function periodList() {
  const seen = new Map();
  DATA.people.forEach(p => p.points.forEach(pt => {
    if (!seen.has(pt.period_key)) seen.set(pt.period_key, { key: pt.period_key, label: pt.period, date: pt.date || '' });
  }));
  return Array.from(seen.values()).sort((a, b) => a.date.localeCompare(b.date) || String(a.label).localeCompare(String(b.label)));
}
function pointFor(p, key) {
  if (key === 'last') return p.points[p.points.length - 1] || null;
  return p.points.filter(x => x.period_key === key).pop() || null;
}
function cellTip(a, c) {
  if (c.ie) return `${axName(a)}: ${t('no_data')}` + (c.reason ? '\n' + reasonName(c.reason) : '');
  return `${axName(a)}: ${c.score}` + (c.overridden ? ' · ' + t('override') : '') + (c.note ? '\n' + c.note : '');
}
function overview() {
  const frag = document.createDocumentFragment();
  const q = S.search;
  const people = DATA.people.filter(p => !q || String(p.name).toLowerCase().includes(q) || String(p.id).toLowerCase().includes(q));
  const rows = people.map(p => ({ p, pt: pointFor(p, S.period) }));
  const pts = rows.map(r => r.pt).filter(Boolean);
  const allPts = [].concat.apply([], DATA.people.map(p => p.points));

  const tiles = el('div', 'tiles');
  const tile = (label, value, sm) => {
    const d = el('div', 'tile'); d.appendChild(el('div', 'label', esc(label)));
    d.appendChild(el('div', 'value tnum' + (sm ? ' sm' : ''), esc(value))); return d;
  };
  tiles.appendChild(tile(t('tile_people'), DATA.people.length));
  tiles.appendChild(tile(t('tile_runs'), allPts.length));
  if (REVIEWER) tiles.appendChild(tile(t('tile_sessions'), sum(allPts, x => x.sessions)));
  const dl = dateLabels(META);
  tiles.appendChild(tile(dl.tile, META.date_from ? fmtDate(META.date_from) + ' – ' + fmtDate(META.date_to) : '—', true));
  if (REVIEWER) tiles.appendChild(tile(t('tile_open_recs'), sum(DATA.people, p => p.recs.filter(r => r.open && !r.pending_approval).length)));
  frag.appendChild(tiles);

  // На узком экране фильтры свёрнуты за кнопкой (на широком кнопка скрыта, поля видны всегда).
  const active = (S.search ? 1 : 0) + (S.axis !== 'all' ? 1 : 0) + (S.period !== 'last' ? 1 : 0);
  const ctl = el('div', 'controls' + (S.filtersOpen ? ' open' : '')); ctl.id = 'controls';
  const tg = el('button', 'btn filters-toggle'); tg.type = 'button';
  tg.textContent = t('filters') + (active ? ` · ${active}` : '');
  tg.setAttribute('aria-controls', 'controls'); tg.setAttribute('aria-expanded', String(S.filtersOpen));
  tg.onclick = () => { S.filtersOpen = !S.filtersOpen; ctl.classList.toggle('open', S.filtersOpen); tg.setAttribute('aria-expanded', String(S.filtersOpen)); };
  frag.appendChild(tg);
  ctl.appendChild(field(t('search'), inputEl('search', S.search, t('search_ph'), v => { S.search = v.trim().toLowerCase(); render(true); })));
  ctl.appendChild(axisSelect());
  const periods = periodList();
  ctl.appendChild(field(t('filter_period'), selectEl('period', [['last', t('last_each')]].concat(periods.map(x => [x.key, x.label + (x.date ? ' · ' + fmtDate(x.date) : '')])), S.period, v => { S.period = v; render(); })));
  frag.appendChild(ctl);

  const card = el('section', 'card');
  card.appendChild(head(t('heatmap'), t('heatmap_hint') + ' · ' + t('scale_hint')));
  const scroll = el('div', 'hm-scroll');
  const extra = REVIEWER ? 2 : 1;
  let h = `<table class="hm"><thead><tr><th scope="col">${esc(t('col_name'))}</th><th scope="col" class="per-col">${esc(t('col_period'))}</th>`;
  DATA.axes.forEach(a => { h += `<th scope="col" class="ax${S.axis === a ? ' hl' : ''}"><span>${esc(axName(a))}</span></th>`; });
  if (REVIEWER) h += `<th scope="col" class="sess-col">${esc(t('col_sessions'))}</th>`;
  h += `<th scope="col" class="lvl-col">${esc(t('col_level'))}</th></tr></thead><tbody>`;
  if (!rows.length) h += `<tr><td colspan="${DATA.axes.length + 2 + extra}" class="none">${esc(t('nothing_found'))}</td></tr>`;
  rows.forEach(({ p, pt }) => {
    const gateChip = pt && pt.gate ? ` <span class="chip gate" tabindex="0" data-tip="${esc(t('gate_tip'))}">${esc(t('gate'))}</span>` : '';
    const perInline = pt ? `${esc(pt.period)} · ${esc(fmtShort(pt.date))}${pt.gate ? ` <span class="gate-mark" aria-label="${esc(t('gate'))}">▲</span>` : ''}` : '—';
    h += `<tr class="row" tabindex="0" data-id="${esc(p.id)}" aria-label="${esc(p.name)}"><td class="name"><span>${esc(p.name)}</span>`;
    if (REVIEWER) h += `<span class="code">${esc(p.id)}</span>`;
    h += `<span class="per-inline">${perInline}</span></td>`;
    if (!pt) { h += `<td class="per per-col muted">—</td><td colspan="${DATA.axes.length + extra}" class="none">${esc(t('no_run_in_period'))}</td></tr>`; return; }
    h += `<td class="per per-col">${esc(pt.period)} <span class="d">${esc(fmtDate(pt.date))}</span>${gateChip}</td>`;
    DATA.axes.forEach(a => {
      const c = pt.axes[a] || { ie: true };
      const hl = S.axis === a ? ' hl' : '';
      const tipText = esc(cellTip(a, c));
      if (c.ie) h += `<td class="ax${hl}"><span class="cell nd" tabindex="0" role="img" aria-label="${tipText}" data-tip="${tipText}">—</span></td>`;
      else h += `<td class="ax${hl}"><span class="cell s${esc(c.score)}${c.overridden ? ' ov' : ''}" tabindex="0" role="img" aria-label="${tipText}" data-tip="${tipText}">${esc(c.score)}</span></td>`;
    });
    if (REVIEWER) h += `<td class="num sess-col tnum">${esc(pt.sessions == null ? '' : pt.sessions)}</td>`;
    h += `<td class="lvl-col">${pt.level ? `<span class="chip lvl">${esc(lvlName(pt.level))}</span>` : ''}</td></tr>`;
  });
  h += '</tbody></table>';
  scroll.innerHTML = h;
  scroll.querySelectorAll('tr.row').forEach(tr => {
    const go = () => { openPerson(tr.getAttribute('data-id')); };
    tr.addEventListener('click', e => { if (e.target.closest('.cell') && e.detail === 0) return; go(); });
    tr.addEventListener('keydown', e => { if (e.target === tr && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); go(); } });
  });
  card.appendChild(scroll);
  const lg = el('div', 'hm-legend');
  lg.innerHTML = `<span><span class="sw">${[0, 1, 2, 3, 4].map(i => `<i class="cell s${i}"></i>`).join('')}</span>0 → 4</span>` +
    `<span><span class="cell nd nd"></span>${esc(t('no_data'))}</span>` +
    `<span><span class="cell s3 ov legend-ov"></span>${esc(t('override'))}</span>` +
    `<span><span class="chip gate legend-gate">${esc(t('gate'))}</span>${esc(t('gate_tip'))}</span>`;
  card.appendChild(lg);
  frag.appendChild(card);

  const dcard = el('section', 'card');
  dcard.appendChild(head(t('distribution'), t('dist_hint') + ' · ' + pts.length + ' ' + t('count_people')));
  const grid = el('div', 'dist');
  DATA.axes.forEach(a => {
    const counts = [0, 0, 0, 0, 0]; let nd = 0;
    pts.forEach(pt => { const c = pt.axes[a]; if (!c || c.ie || c.score == null) nd++; else counts[c.score]++; });
    const d = el('div', 'd' + (S.axis === a ? ' hl' : ''));
    d.appendChild(el('h4', null, esc(axName(a))));
    d.insertAdjacentHTML('beforeend', histSVG(counts, axName(a)));
    const total = pts.length || 1;
    d.appendChild(el('div', 'nd-line', `<span class="cell nd nd-mini"></span>${esc(t('no_data_share'))}: <span class="tnum">${nd} (${Math.round(nd / total * 100)}%)</span>`));
    grid.appendChild(d);
  });
  dcard.appendChild(grid);
  frag.appendChild(dcard);
  return frag;
}
function histSVG(counts, title) {
  const W = 220, H = 74, padB = 16, padT = 14, bw = 30, gap = 8, x0 = 10;
  const max = Math.max(1, ...counts);
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title + ': ' + counts.map((c, i) => i + ' — ' + c).join(', '))}">`;
  s += `<line x1="${x0}" y1="${H - padB}" x2="${x0 + 5 * (bw + gap)}" y2="${H - padB}" style="stroke:var(--hairline)" stroke-width="1"/>`;
  counts.forEach((c, i) => {
    const x = x0 + i * (bw + gap), hh = c ? Math.max(3, (H - padB - padT) * c / max) : 0, y = H - padB - hh;
    if (c) s += `<rect x="${x}" y="${y}" width="${bw}" height="${hh}" rx="3" style="fill:var(--s${i})"/>`;
    s += `<text x="${x + bw / 2}" y="${y - 3}" text-anchor="middle" font-size="11" style="fill:var(--ink-2)">${c || ''}</text>`;
    s += `<text x="${x + bw / 2}" y="${H - 3}" text-anchor="middle" font-size="11" style="fill:var(--muted)">${i}</text>`;
  });
  return s + '</svg>';
}
function openPerson(id) {
  S.person = id; S.view = 'person'; S.run = -1; S.review = null; S.asPerson = false; S.showScores = !!META.show_scores;
  location.hash = 'p-' + encodeURIComponent(id);
  render(); window.scrollTo(0, 0);
}

// --- small multiples ---
function chartsCard(pts, selected, withNotes) {
  const card = el('section', 'card');
  card.appendChild(head(t('charts'), t('scale_hint')));
  const grid = el('div', 'charts');
  const run = selected >= 0 ? pts[selected] : null;
  DATA.axes.forEach(a => {
    const c = el('div', 'chart' + (S.axis === a ? ' hl' : S.axis !== 'all' ? ' dim' : ''));
    c.appendChild(el('h4', null, esc(axName(a))));
    const plot = el('div', 'plot'); plot.setAttribute('data-axis', a); c.appendChild(plot);
    const cur = run && run.axes[a];
    if (withNotes && cur && cur.note) {
      const n = el('div', 'note', `<span class="muted">${esc(run.period)}:</span> ${esc(cur.note)}`);
      c.appendChild(n);
      const b = el('button', 'note-btn'); b.type = 'button'; b.textContent = t('expand'); b.setAttribute('aria-expanded', 'false');
      b.onclick = () => { const open = n.classList.toggle('open'); b.textContent = open ? t('collapse') : t('expand'); b.setAttribute('aria-expanded', String(open)); };
      c.appendChild(b);
    } else if (cur && cur.ie) {
      c.appendChild(el('div', 'note', `<span class="muted">${esc(run.period)}:</span> ${esc(t('no_data'))}. ${esc(reasonName(cur.reason))}`));
    }
    grid.appendChild(c);
  });
  card.appendChild(grid);
  const lg = el('div', 'legend');
  lg.innerHTML = `<span><svg width="16" height="12" aria-hidden="true"><circle cx="8" cy="6" r="4.5" style="fill:var(--series)"/></svg>${esc(t('legend_score'))}</span>` +
    `<span><svg width="16" height="16" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="2" transform="rotate(45 8 8)" fill="url(#hatch)" style="stroke:var(--muted)" stroke-width="1"/></svg>${esc(t('no_data'))}</span>` +
    `<span><svg width="18" height="16" aria-hidden="true"><circle cx="9" cy="8" r="4" style="fill:var(--series)"/><circle cx="9" cy="8" r="7" fill="none" style="stroke:var(--series)" stroke-width="1.5"/></svg>${esc(t('override'))}</span>` +
    `<span><svg width="22" height="10" aria-hidden="true"><line x1="1" y1="5" x2="21" y2="5" style="stroke:var(--series)" stroke-width="2" stroke-dasharray="4 3"/></svg>${esc(t('legend_gap'))}</span>` +
    (run ? `<span><svg width="16" height="14" aria-hidden="true"><rect x="1" y="1" width="14" height="12" rx="2" style="fill:var(--accent-soft)"/></svg>${esc(t('legend_selected'))}</span>` : '');
  card.appendChild(lg);
  CHARTS = { pts, selected };
  requestAnimationFrame(drawCharts);
  return card;
}
let CHARTS = null;
function drawCharts() {
  if (!CHARTS) return;
  document.querySelectorAll('.plot[data-axis]').forEach(host => {
    const w = Math.max(220, host.clientWidth || 300);
    host.innerHTML = lineSVG(CHARTS.pts, host.getAttribute('data-axis'), w, CHARTS.selected);
  });
}
function lineSVG(pts, a, W, selected) {
  const n = pts.length;
  const padL = 26, padR = 14, padT = 18, plotH = 100, ndY = padT + plotH + 18, H = ndY + 46;
  const step = n > 1 ? (W - padL - padR) / (n - 1) : 0;
  const X = i => (n > 1 ? padL + i * step : padL + (W - padL - padR) / 2);
  const Y = v => padT + plotH - (v / 4) * plotH;
  let s = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(axName(a))}">`;
  if (selected >= 0 && selected < n) {
    const bw = n > 1 ? Math.min(step, 64) : 64;
    s += `<rect x="${X(selected) - bw / 2}" y="${padT - 14}" width="${bw}" height="${H - padT + 6}" rx="6" style="fill:var(--accent-soft)"/>`;
  }
  for (let v = 0; v <= 4; v++) {
    s += `<line x1="${padL}" y1="${Y(v)}" x2="${W - padR}" y2="${Y(v)}" style="stroke:var(--hairline)" stroke-width="1"/>`;
    s += `<text x="${padL - 8}" y="${Y(v) + 4}" text-anchor="end" font-size="11" style="fill:var(--muted)">${v}</text>`;
  }
  s += `<line x1="${padL}" y1="${ndY}" x2="${W - padR}" y2="${ndY}" style="stroke:var(--hairline)" stroke-width="1" stroke-dasharray="2 3"/>`;
  s += `<text x="${padL - 8}" y="${ndY + 4}" text-anchor="end" font-size="10" style="fill:var(--muted)">—</text>`;
  const isIE = c => !c || c.ie || c.score == null;
  let prev = null, gap = false;
  pts.forEach((pt, i) => {
    const c = pt.axes[a];
    if (isIE(c)) { gap = prev !== null; return; }
    if (prev) s += `<line x1="${prev.x}" y1="${prev.y}" x2="${X(i)}" y2="${Y(c.score)}" style="stroke:var(--series)" stroke-width="2" stroke-linecap="round"${gap ? ' stroke-dasharray="5 4"' : ''}/>`;
    prev = { x: X(i), y: Y(c.score) }; gap = false;
  });
  pts.forEach((pt, i) => {
    const c = pt.axes[a] || { ie: true }; const x = X(i);
    if (isIE(c)) {
      const tipText = esc(`${axName(a)} · ${pt.period} · ${fmtDate(pt.date)}\n${t('no_data')}: ${reasonName(c.reason)}`);
      s += `<g class="hit" tabindex="0" role="img" aria-label="${tipText}" data-tip="${tipText}"><rect x="${x - 14}" y="${ndY - 14}" width="28" height="28" fill="transparent"/><rect x="${x - 6}" y="${ndY - 6}" width="12" height="12" rx="2" transform="rotate(45 ${x} ${ndY})" fill="url(#hatch)" style="stroke:var(--muted)" stroke-width="1"/></g>`;
    } else {
      const y = Y(c.score);
      const tipText = esc(`${axName(a)}: ${c.score}${c.overridden ? ' · ' + t('override') : ''}\n${pt.period} · ${fmtDate(pt.date)}${c.note ? '\n' + c.note : ''}`);
      s += `<g class="hit" tabindex="0" role="img" aria-label="${tipText}" data-tip="${tipText}"><circle cx="${x}" cy="${y}" r="14" fill="transparent"/>`;
      if (c.overridden) s += `<circle cx="${x}" cy="${y}" r="9.5" fill="none" style="stroke:var(--series)" stroke-width="1.5"/>`;
      s += `<circle cx="${x}" cy="${y}" r="5" style="fill:var(--series);stroke:var(--surface)" stroke-width="2"/>`;
      s += `<text x="${x}" y="${y - 11}" text-anchor="middle" font-size="11.5" font-weight="600" style="fill:var(--ink)">${esc(c.score)}</text></g>`;
    }
    s += `<text x="${x}" y="${H - 22}" text-anchor="middle" font-size="11.5" style="fill:var(--ink-2)">${esc(pt.period)}</text>`;
    s += `<text x="${x}" y="${H - 8}" text-anchor="middle" font-size="10.5" style="fill:var(--muted)">${esc(fmtShort(pt.date))}</text>`;
  });
  return s + '</svg>';
}

// --- переходы ↑↓→◇ ---
function transitionsCard(pts) {
  const card = el('section', 'card');
  card.appendChild(head(t('transitions'), t('transitions_hint')));
  if (pts.length < 2) { card.appendChild(el('div', 'empty', esc(t('transitions_single')))); return card; }
  const kinds = {
    up: ['good', '↑', t('trans_up')], down: ['bad', '↓', t('trans_down')],
    flat: ['neutral', '→', t('trans_flat')], none: ['nd', '◇', t('trans_none')],
  };
  let h = `<div class="tbl-scroll"><table class="trend"><thead><tr><th scope="col">${esc(t('filter_axis'))}</th>`;
  for (let i = 1; i < pts.length; i++) h += `<th scope="col">${esc(pts[i - 1].period)} → ${esc(pts[i].period)}<br><span class="muted">${esc(fmtDate(pts[i].date))}</span></th>`;
  h += '</tr></thead><tbody>';
  DATA.axes.forEach(a => {
    h += `<tr${S.axis === a ? ' class="hl"' : ''}><th scope="row" class="ax">${esc(axName(a))}</th>`;
    for (let i = 1; i < pts.length; i++) {
      const kind = (pts[i].transitions || {})[a] || 'none';
      const [cls, glyph, label] = kinds[kind] || kinds.none;
      if (kind === 'none') {
        const cur = pts[i].axes[a] || {}, prev = pts[i - 1].axes[a] || {};
        const why = cur.ie ? reasonName(cur.reason) : prev.ie ? reasonName(prev.reason) : '';
        const tipText = esc(`${axName(a)}: ${t('no_data')}` + (why ? '\n' + why : ''));
        h += `<td><span class="chip ${cls}" tabindex="0" data-tip="${tipText}" aria-label="${tipText}">${glyph} ${esc(label)}</span></td>`;
      } else {
        h += `<td><span class="chip ${cls}">${glyph} ${esc(label)}</span></td>`;
      }
    }
    h += '</tr>';
  });
  h += '</tbody></table></div>';
  card.insertAdjacentHTML('beforeend', h);
  return card;
}

// --- ревью по периодам ---
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  return new Promise((resolve, reject) => {
    const ta = document.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', '');
    ta.style.position = 'fixed'; ta.style.top = '0'; ta.style.left = '0'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    if (ok) resolve(); else reject(new Error('execCommand copy'));
  });
}
function reviewsCard(pts, selected, full) {
  const card = el('section', 'card');
  card.appendChild(head(t('reviews'), full ? t('reviews_hint') : t('reviews_hint_personal')));
  const list = pts.map((pt, i) => ({ pt, i })).filter(x => x.pt.review && (full || x.pt.review.sent_html));
  if (!list.length) { card.appendChild(el('div', 'empty', esc(t('no_reviews')))); return card; }
  const active = list.find(x => x.i === S.review) || list.find(x => x.i === selected) || list[list.length - 1];
  if (list.length > 1) {
    const tabs = el('div', 'tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', t('reviews'));
    list.forEach(x => {
      const draft = full && x.pt.review.status !== 'approved';
      const b = el('button', 'tab', `${esc(x.pt.period)} <span class="d">${esc(fmtShort(x.pt.date))}</span>${draft ? ` <span class="draft-dot" aria-label="${esc(reviewStatusName('draft'))}">●</span>` : ''}`);
      b.type = 'button'; b.setAttribute('role', 'tab'); b.setAttribute('aria-selected', String(x === active));
      b.onclick = () => { S.review = x.i; render(); };
      tabs.appendChild(b);
    });
    card.appendChild(tabs);
  }
  const r = active.pt.review;
  const sent = el('div', 'md review-sent');
  if (r.sent_html) sent.innerHTML = r.sent_html;
  else sent.appendChild(el('div', 'empty', esc(t('sent_empty'))));
  if (full) {
    const bar = el('div', 'review-bar');
    const approved = r.status === 'approved';
    const st = el('span', 'chip ' + (approved ? 'good' : 'warn'), esc(reviewStatusName(approved ? 'approved' : 'draft')));
    if (!approved) { st.setAttribute('data-tip', t('draft_tip')); st.tabIndex = 0; }
    bar.appendChild(st);
    if (r.sent_html) {
      const msg = el('span', 'muted small copy-msg'); msg.setAttribute('role', 'status'); msg.setAttribute('aria-live', 'polite');
      const b = button(t('copy_sent'), null, () => {
        msg.textContent = '';
        copyText(sent.innerText).then(() => { msg.textContent = t('copied'); }, err => {
          console.error('sessionretro: копирование не удалось', err); msg.textContent = t('copy_failed');
        });
      });
      bar.appendChild(b); bar.appendChild(msg);
    }
    bar.appendChild(el('span', 'muted small', `${esc(active.pt.period)} · ${esc(fmtDate(active.pt.date))}`));
    if (r.path) bar.appendChild(el('span', 'src mono', `${esc(t('review_source'))}: ${esc(r.path)}`));
    card.appendChild(bar);
    if (r.warning) card.appendChild(el('p', 'review-warn', `<span class="chip warn">${esc(t('review_warning'))}</span> ${esc(r.warning)}`));
    if (r.service_html) {
      const d = el('details', 'service'); d.appendChild(el('summary', null, esc(t('service_header'))));
      const body = el('div', 'body'); body.appendChild(el('div', 'md wide', r.service_html)); d.appendChild(body);
      card.appendChild(d);
    }
  }
  card.appendChild(sent);
  if (full && (r.dossier_html || r.dossier_ref)) {
    const priv = el('div', 'review-private');
    priv.appendChild(el('span', 'tag', esc(t('reviewer_only'))));
    if (r.dossier_html) {
      const d = el('details'); d.appendChild(el('summary', null, esc(t('dossier'))));
      const body = el('div', 'body'); body.appendChild(el('div', 'md wide', r.dossier_html)); d.appendChild(body);
      priv.appendChild(d);
    } else {
      priv.appendChild(el('div', 'ctx', `<b>${esc(t('dossier'))}</b>${esc(t('dossier_ref'))}: <span class="mono">${esc(r.dossier_ref)}</span>`));
    }
    card.appendChild(priv);
  }
  return card;
}

// --- рекомендации ---
function recsCard(recs, full) {
  const card = el('section', 'card');
  const list = recs.filter(r => S.axis === 'all' || r.axis === S.axis);
  // Рекомендация неодобренного прогона адресату не ушла: в счётчик открытых не входит.
  const open = recs.filter(r => recStatus(r) === 'open' && !r.pending_approval).length;
  card.appendChild(head(t('recs'), `${t('open_now')}: ${open}`));
  if (!list.length) { card.appendChild(el('div', 'empty', esc(t('no_recs')))); return card; }
  const grid = el('div', 'recs');
  list.forEach(r => {
    const st = recStatus(r);
    const d = el('div', 'rec' + (S.axis === r.axis ? ' hl' : ''));
    const idChip = (full && r.id ? `<span class="chip id">${esc(r.id)}</span>` : '')
      + (r.pending_approval ? `<span class="chip warn">${esc(t('rec_from_draft'))}</span>` : '');
    const given = `${esc(t('rec_given'))} ${esc(r.given_in || '?')}` + (full && r.given_at ? ' · ' + esc(fmtDate(r.given_at)) : '');
    d.appendChild(el('div', 'head', `${idChip}<span>${esc(axName(r.axis))}</span><span>· ${given}</span><span class="chip ${CHIP_CLASS[st] || 'neutral'} rec-status">${CHIP_GLYPH[st] || '·'} ${esc(recStatusName(st))}</span>`));
    d.appendChild(el('div', 'text', esc(r.text)));
    if (full && r.verdicts && r.verdicts.length) {
      const hist = el('div', 'hist'); hist.appendChild(el('span', null, esc(t('verdict_history')) + ':'));
      r.verdicts.forEach(v => {
        const ch = el('span', 'chip ' + (CHIP_CLASS[v.verdict] || 'neutral'), `${esc(v.period || '?')} ${CHIP_GLYPH[v.verdict] || '·'} ${esc(recVerdictName(v.verdict))}`);
        if (v.note) { ch.setAttribute('data-tip', v.note); ch.tabIndex = 0; }
        hist.appendChild(ch);
      });
      d.appendChild(hist);
    }
    grid.appendChild(d);
  });
  card.appendChild(grid);
  return card;
}

// --- доказательства, антипаттерны, флаги, черновик (только ревьюер) ---
function evidenceCard(p, idx) {
  const run = p.points[idx];
  const card = el('section', 'card');
  card.appendChild(head(t('evidence'), `${run.period} · ${fmtDate(run.date)} · ${t('evidence_hint')}`));
  const addr = (sid, turn) => [sid ? `${t('session')} ${sessShort(sid)}` : null, turn != null ? `${t('turn')} ${turn}` : null].filter(Boolean).join(' · ');
  // Старший период вне лимита: цитаты, антипаттерны и черновик вырезаны при сборке.
  if (run.trimmed) {
    const ref = run.review && run.review.dossier_ref;
    card.appendChild(el('p', 'trimmed-note', esc(t('run_trimmed')) + (ref ? `: <span class="mono">${esc(ref)}</span>` : '')));
  }
  const total = sum(DATA.axes, a => ((run.axes[a] || {}).evidence || []).length);
  const ev = el('details');
  ev.appendChild(el('summary', null, `${esc(t('quotes_by_axis'))} <span class="cnt">${total} ${esc(t('quotes'))}</span>`));
  const body = el('div', 'body');
  const order = S.axis === 'all' ? DATA.axes : [S.axis].concat(DATA.axes.filter(a => a !== S.axis));
  order.forEach(a => {
    const c = run.axes[a] || { ie: true }; const qs = c.evidence || [];
    const sec = el('div', 'ev-axis' + (S.axis === a ? ' hl' : ''));
    const scoreTxt = c.ie ? t('no_data') : `${t('score')} ${c.score}`;
    sec.appendChild(el('h4', null, `${esc(axName(a))} <span class="cnt">${esc(scoreTxt)} · ${qs.length} ${esc(t('quotes'))}</span>`));
    if (!qs.length) sec.appendChild(el('div', 'muted small', esc(c.ie ? reasonName(c.reason) : '—')));
    qs.forEach(q => sec.appendChild(el('div', 'quote',
      `<span class="addr">${esc(addr(q.session_id, q.turn_index))}</span><span class="q">${esc(q.quote)}</span>` +
      (q.reading ? `<span class="r"><b>${esc(t('reading'))}:</b> ${esc(q.reading)}</span>` : ''))));
    body.appendChild(sec);
  });
  ev.appendChild(body); card.appendChild(ev);

  const aps = (run.antipatterns || []).filter(a => a.found !== false);
  const apd = el('details');
  apd.appendChild(el('summary', null, `${esc(t('antipatterns'))} <span class="cnt">${aps.length}</span>`));
  const ab = el('div', 'body');
  if (!aps.length) ab.appendChild(el('div', 'empty', '—'));
  aps.forEach(a => ab.appendChild(el('div', 'ap',
    `<div class="h"><b>${esc(humanize(a.id))}</b>${a.axis ? `<span class="chip">${esc(axName(a.axis))}</span>` : ''}<span class="chip id">${esc(a.id)}</span><span class="muted small">${esc(addr(a.session_id, a.turn_index))}</span></div>` +
    (a.quote ? `<div class="quote"><span class="q">${esc(a.quote)}</span></div>` : '') + (a.note ? `<div class="n">${esc(a.note)}</div>` : ''))));
  apd.appendChild(ab); card.appendChild(apd);

  const flags = (run.flags || []).map(f => ({ title: humanize(f.id), id: f.id, session_id: f.session_id, turn_index: f.turn_index, note: f.note }))
    .concat((run.gating_evidence || []).map(g => ({ title: t('gate') + ': ' + humanize(g.trigger), id: g.trigger, session_id: g.session_id, turn_index: g.turn_index, note: g.quote })));
  const fd = el('details');
  fd.appendChild(el('summary', null, `${esc(t('flags'))} <span class="cnt">${flags.length}</span>`));
  const fb = el('div', 'body');
  if (!flags.length) fb.appendChild(el('div', 'empty', '—'));
  flags.forEach(f => fb.appendChild(el('div', 'ap',
    `<div class="h"><b>${esc(f.title)}</b>${f.id ? `<span class="chip id">${esc(f.id)}</span>` : ''}<span class="muted small">${esc(addr(f.session_id, f.turn_index))}</span></div>` +
    (f.note ? `<div class="n">${esc(f.note)}</div>` : ''))));
  fd.appendChild(fb); card.appendChild(fd);

  if (run.feedback_draft) {
    const dd = el('details');
    dd.appendChild(el('summary', null, esc(t('feedback_draft'))));
    const db = el('div', 'body'); const pre = el('pre', 'draft'); pre.textContent = run.feedback_draft;
    db.appendChild(pre); dd.appendChild(db); card.appendChild(dd);
  }
  return card;
}

// --- карточка человека ---
function stackChips(points) {
  const stack = Array.from(new Set([].concat.apply([], points.map(x => x.agent_stack || []))));
  if (!stack.length) return null;
  const ch = el('div', 'chips'); ch.appendChild(el('span', 'muted small', esc(t('agent_stack')) + ':'));
  const short = stack.filter(s => s.length <= 34), long = stack.filter(s => s.length > 34);
  short.slice(0, 8).forEach(s => ch.appendChild(el('span', 'chip', esc(s))));
  const rest = short.slice(8).concat(long);
  if (rest.length) { const m = el('span', 'chip', `+${rest.length} ${esc(t('more'))}`); m.setAttribute('data-tip', rest.join('\n')); m.tabIndex = 0; ch.appendChild(m); }
  return ch;
}
// Переходы по осям между соседними точками списка — как transitions() при сборке отчёта.
function personTransitions(points) {
  return points.map((pt, i) => {
    const row = {};
    DATA.axes.forEach(a => {
      const cur = pt.axes[a] || { ie: true }; const prev = i ? (points[i - 1].axes[a] || { ie: true }) : null;
      row[a] = !prev || cur.ie || prev.ie ? 'none' : cur.score > prev.score ? 'up' : cur.score < prev.score ? 'down' : 'flat';
    });
    return row;
  });
}
// Как видит адресат: те же данные, что пишет личный отчёт (только одобренные прогоны и их
// рекомендации, статус — по вердиктам одобренных прогонов, одобренный отправляемый блок,
// баллы и уровень — только при show_scores, переходы — только при show_dynamics карточки).
function asPersonModel(p) {
  const scores = !!META.show_scores;
  const dynamics = p.show_dynamics !== false;
  const shown = p.points.filter(pt => pt.approved !== false);
  const trans = personTransitions(shown);
  return {
    id: p.id, name: p.name,
    points: shown.map((pt, i) => {
      const axes = {};
      DATA.axes.forEach(a => { const c = pt.axes[a] || { ie: true }; axes[a] = { ie: !!c.ie, reason: c.reason || null }; if (scores) axes[a].score = c.score; });
      const out = { period: pt.period, date: pt.date, sessions: pt.sessions, axes,
        review: pt.review && pt.review.status === 'approved' && pt.review.sent_html ? { sent_html: pt.review.sent_html } : null };
      if (dynamics) out.transitions = trans[i];
      if (scores) out.level = pt.level;
      return out;
    }),
    recs: p.recs.filter(r => !r.pending_approval).map(r => ({ text: r.text, axis: r.axis, given_in: r.given_in,
      status: recStatus({ verdicts: (r.verdicts || []).filter(v => v.approved !== false) }) })),
  };
}
function backLink(frag) {
  const back = el('p', 'back', `<a href="#overview">← ${esc(t('back'))}</a>`);
  back.querySelector('a').onclick = e => { e.preventDefault(); S.view = 'overview'; location.hash = 'overview'; render(); };
  frag.appendChild(back);
}
function personView() {
  const frag = document.createDocumentFragment();
  const p = person();
  if (!PERSONAL) backLink(frag);
  if (!p) { frag.appendChild(el('div', 'empty', esc(t('nothing_found')))); return frag; }
  if (PERSONAL) return personalView(p, frag);
  if (REVIEWER && S.asPerson) return personalView(asPersonModel(p), frag);
  const idx = runIndex(p); const run = p.points[idx] || null; const last = p.points[p.points.length - 1] || null;

  const card = el('section', 'card');
  const hd = el('div', 'person-head');
  const who = el('div', 'who');
  who.appendChild(el('h1', null, esc(p.name) + (REVIEWER ? ` <span class="muted small">${esc(p.id)}</span>` : '')));
  const facts = el('div', 'facts');
  if (last && last.level) facts.appendChild(el('span', 'chip lvl', esc(t('level_last') + ': ' + lvlName(last.level)) + (last.gate ? ` · ${esc(t('gate'))}` : '')));
  facts.appendChild(el('span', null, `${esc(t('tile_runs'))}: <b class="tnum">${p.points.length}</b>`));
  if (REVIEWER) facts.appendChild(el('span', null, `${esc(t('tile_sessions'))}: <b class="tnum">${sum(p.points, x => x.sessions)}</b>`));
  who.appendChild(facts);
  if (REVIEWER) { const st = stackChips(p.points); if (st) who.appendChild(st); }
  hd.appendChild(who);
  const tb = el('div', 'toolbar');
  if (REVIEWER && p.points.length > 1) {
    tb.appendChild(field(t('run'), selectEl('run', [[-1, t('last_run')]].concat(p.points.map((x, i) => [i, x.period + ' · ' + fmtDate(x.date)])), S.run, v => { S.run = parseInt(v, 10); S.review = null; render(); })));
  }
  tb.appendChild(axisSelect());
  if (REVIEWER) tb.appendChild(wrapBtn(button(t('as_person'), false, () => { S.asPerson = true; S.showScores = !!META.show_scores; render(); })));
  hd.appendChild(tb);
  card.appendChild(hd);
  if (REVIEWER && run && run.summary) card.appendChild(el('div', 'summary', `<b>${esc(t('summary'))} · ${esc(run.period)} · ${esc(fmtDate(run.date))}</b>${esc(run.summary)}`));
  if (REVIEWER && run && run.warnings && run.warnings.length) {
    const w = el('div', 'chips run-warn'); w.appendChild(el('span', 'muted small', esc(t('run_warnings')) + ':'));
    run.warnings.forEach(x => w.appendChild(el('span', 'chip warn', esc(x))));
    card.appendChild(w);
  }
  frag.appendChild(card);
  if (!p.points.length) { const c = el('section', 'card'); c.appendChild(el('div', 'empty', esc(t('no_points')))); frag.appendChild(c); return frag; }
  frag.appendChild(chartsCard(p.points, REVIEWER ? idx : -1, REVIEWER));
  if (GROUP) return frag;
  frag.appendChild(reviewsCard(p.points, idx, true));
  frag.appendChild(recsCard(p.recs, true));
  if (run) frag.appendChild(evidenceCard(p, idx));
  return frag;
}
function personalView(pp, frag) {
  const pts = pp.points; const last = pts[pts.length - 1] || null;
  const scores = !!META.show_scores && S.showScores;
  const card = el('section', 'card');
  const hd = el('div', 'person-head');
  const who = el('div', 'who');
  who.appendChild(el('h1', null, esc(pp.name)));
  const facts = el('div', 'facts');
  if (scores && last && last.level) facts.appendChild(el('span', 'chip lvl', esc(t('level_last') + ': ' + lvlName(last.level))));
  facts.appendChild(el('span', null, `${esc(t('tile_runs'))}: <b class="tnum">${pts.length}</b>`));
  facts.appendChild(el('span', null, `${esc(t('tile_sessions'))}: <b class="tnum">${sum(pts, x => x.sessions)}</b>`));
  who.appendChild(facts);
  who.appendChild(el('p', 'intro', esc(t('personal_intro'))));
  hd.appendChild(who);
  const tb = el('div', 'toolbar');
  tb.appendChild(axisSelect());
  if (META.show_scores) tb.appendChild(wrapBtn(button(S.showScores ? t('hide_scores') : t('show_scores'), !S.showScores, () => { S.showScores = !S.showScores; render(); })));
  if (REVIEWER) tb.appendChild(wrapBtn(button(t('as_reviewer'), true, () => { S.asPerson = false; render(); })));
  hd.appendChild(tb);
  card.appendChild(hd);
  frag.appendChild(card);
  if (!pts.length) { const c = el('section', 'card'); c.appendChild(el('div', 'empty', esc(t('no_points')))); frag.appendChild(c); return frag; }
  if (scores) frag.appendChild(chartsCard(pts, -1, false));
  if (pts.some(x => x.transitions)) frag.appendChild(transitionsCard(pts));
  frag.appendChild(reviewsCard(pts, pts.length - 1, false));
  frag.appendChild(recsCard(pp.recs, false));
  return frag;
}

// --- рендер ---
function render(keepFocus) {
  const active = keepFocus && document.activeElement ? document.activeElement.id : null;
  document.documentElement.lang = S.lang;
  hideTip();
  CHARTS = null;
  const app = document.getElementById('app'); app.innerHTML = '';
  app.appendChild(topbar());
  const main = el('main');
  main.appendChild(S.view === 'person' ? personView() : overview());
  app.appendChild(main);
  if (active) {
    const e = document.getElementById(active);
    if (e) { e.focus(); if (e.setSelectionRange && typeof e.value === 'string') e.setSelectionRange(e.value.length, e.value.length); }
  }
  const p = S.view === 'person' ? person() : null;
  const base = META.title || META.workspace_title || '';
  document.title = base + (p && !PERSONAL ? ' · ' + p.name : '');
}
function route() {
  if (PERSONAL) return;
  const h = location.hash.replace(/^#/, '');
  let id = null;
  if (h.indexOf('p-') === 0) { try { id = decodeURIComponent(h.slice(2)); } catch (e) { id = null; } }
  if (id && DATA.people.some(p => p.id === id)) {
    if (S.person !== id) { S.run = -1; S.review = null; S.asPerson = false; }
    S.view = 'person'; S.person = id;
  } else S.view = 'overview';
}
window.addEventListener('hashchange', () => { route(); render(); });
let rt = 0;
window.addEventListener('resize', () => { if (!CHARTS) return; cancelAnimationFrame(rt); rt = requestAnimationFrame(drawCharts); });
window.addEventListener('beforeprint', () => document.querySelectorAll('details').forEach(d => { d.open = true; }));

applyTheme();
route();
render();
})();
