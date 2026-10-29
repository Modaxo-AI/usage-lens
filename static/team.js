'use strict';
// Team dashboard: everything here comes from people's merged export files
// (claude_usage/team.py), never from this machine's own logs.

const SERIES_VARS = ['--series-1','--series-2','--series-3','--series-4','--series-5','--series-6','--series-7','--series-8'];
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
// Names, emails, project paths and file names all come from other people's
// files, so nothing reaches innerHTML unescaped.
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
// Number formatters. A missing value counts as 0 (a missing percentage is a
// dash); a value that is not finite (NaN, Infinity) shows as a dash, never
// "$∞" or "NaN%".
// Rounding happens before the unit is chosen (999,999,999 is "1.0B", not
// "1000.0M"), and -0 shows as 0.
const asNum = n => n == null ? 0 : Number(n);
function fmtInt(n) {
  n = asNum(n);
  return Number.isFinite(n) ? (Math.round(n) || 0).toLocaleString('en-US') : '—';
}
function fmtCost(n) {
  n = asNum(n);
  if (!Number.isFinite(n)) return '—';
  return '$' + (Math.round(n * 100) / 100 || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
const fmtCost0 = n => Number.isFinite(asNum(n)) ? '$' + fmtInt(n) : '—';
function fmtPct(n) {
  if (n == null || !Number.isFinite(Number(n))) return '—';
  n = Number(n);
  const s = (n * 100).toFixed(n < 0.1 ? 1 : 0);
  return (+s === 0 ? s.replace('-', '') : s) + '%';
}
function fmtTokens(n) {
  n = asNum(n);
  if (!Number.isFinite(n)) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (Math.round(n) < 1e3) return fmtInt(n);
  const k = Math.round(n / 100) / 10;
  const m = Math.round(n / 1e5) / 10;
  if (m >= 1000) return (n / 1e9).toFixed(1) + 'B';
  if (k >= 1000) return (n / 1e6).toFixed(1) + 'M';
  return n >= 1e6 ? m.toFixed(1) + 'M' : k.toFixed(1) + 'k';
}
function fmtX(n) { return (Number(n) || 0).toFixed(1) + '×'; }
function fmtHours(h) { return !h ? '0h' : h >= 1 ? h.toFixed(1) + 'h' : Math.round(h * 60) + 'm'; }
// A moment in the viewer's own zone, with the year and the zone's name, so it is
// never read as a date in the export's zone.
function fmtWhen(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
    timeZoneName: 'short' });
}
// An export's timestamp as its own machine wrote it ("2026-10-29T10:00:00-07:00"):
// date with year, time and offset, the zone the export's days are cut in -- not
// the viewer's, which would put a date beside it that its own weeks don't match.
function fmtAsWritten(iso) {
  if (!iso) return '—';
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/.exec(String(iso));
  if (!m) return String(iso);
  const zone = !m[6] ? '' : m[6] === 'Z' ? ' UTC' : ' UTC' + m[6].replace(/^([+-]\d{2}):?(\d{2})$/, '$1:$2');
  return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}${zone}`;
}
const plural = (n, one, many) => `${fmtInt(n)} ${Math.round(n) === 1 ? one : many || one + 's'}`;
function shortDay(day) {
  return day ? new Date(day + 'T00:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '—';
}
function median(xs) {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b), m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
function mondayOf(iso) {
  const d = new Date(iso);
  const local = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  local.setDate(local.getDate() - ((local.getDay() + 6) % 7));
  return `${local.getFullYear()}-${String(local.getMonth() + 1).padStart(2, '0')}-${String(local.getDate()).padStart(2, '0')}`;
}
function addDays(day, n) {
  const d = new Date(day + 'T00:00:00'); d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function emptyRow(colspan, message) {
  return `<tr><td colspan="${colspan}"><div class="empty-state"><div class="es-title">No rows</div>` +
    `<div class="es-body">${esc(message)}</div></div></td></tr>`;
}

let PAYLOAD = null;
let peopleMS = null;
const charts = {};
const state = { people: [], from: null, to: null, metric: 'cost', mix: 'model', modelMetric: 'cost' };

const TEAM_TABS = ['overview', 'spend', 'seats', 'habits', 'output', 'data'];
let currentTab = 'overview';
// Each tab renders only when shown; the filters re-render the one on screen.
const RENDERERS = {
  overview: () => { renderFindings(); renderTiles(); renderTrend(); renderModelTrend(); renderFreshness(); },
  spend: () => { renderRepoSpend(); renderRepoTrend(); renderMix(); renderPerDay(); },
  seats: () => { renderHeatmap(); renderLimits(); renderOverage(); renderValue(); },
  habits: () => { renderTeamHabits(); renderFriction(); },
  output: () => { renderPrs(); renderOutput(); },
  data: () => { renderPeople(); renderSources(); },
};
function showTab(tab) {
  if (!TEAM_TABS.includes(tab)) tab = 'overview';
  currentTab = tab;
  document.querySelectorAll('#tabstrip button').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
    b.setAttribute('aria-selected', b.dataset.tab === tab ? 'true' : 'false');
  });
  document.querySelectorAll('.tabpanel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + tab));
  RENDERERS[tab]();
}
function wireTabs() {
  document.getElementById('tabstrip').addEventListener('click', e => {
    const tab = e.target && e.target.dataset && e.target.dataset.tab;
    if (!tab) return;
    if (window.location.hash === '#' + tab) showTab(tab);
    else window.location.hash = tab;
  });
  window.addEventListener('hashchange', () => showTab(window.location.hash.slice(1)));
}
// Once per page: Refresh calls load() again, which must not wire twice. The
// first tab is the one the address names, so a link to #spend opens Spend.
function initTabs() {
  if (initTabs.wired) return;
  wireTabs();
  initTabs.wired = true;
  const hash = window.location.hash.slice(1);
  currentTab = TEAM_TABS.includes(hash) ? hash : 'overview';
}

// Plain text: every caller escapes it, or hands it to a canvas.
function personLabel(p) { return displayName(p); }
function nameOf(person) {
  const p = PAYLOAD.people.find(x => x.person === person);
  return p ? displayName(p) : person;
}
function selectedPeople() {
  return PAYLOAD.people.filter(p => !state.people.length || state.people.includes(p.person));
}
function personOk(person) { return !state.people.length || state.people.includes(person); }
function weekOk(week) { return week && week >= state.from && week <= state.to; }

// ---------------------------------------------------------------- loading
// The server refuses /api/* requests that don't prove they came from this page;
// browsers that send neither Sec-Fetch-Site nor Origin on a same-origin GET
// (older Safari) are covered by this header, which another site's page can't
// add without a CORS preflight the server never grants.
function apiFetch(url, opts) {
  return fetch(url, Object.assign({}, opts, {
    headers: Object.assign({}, opts && opts.headers, { 'X-Claude-Usage': '1' }),
  }));
}

async function load() {
  const res = await apiFetch('/api/team', { cache: 'no-store' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  PAYLOAD = await res.json();
  const weeks = allWeeks();
  if (!state.from || !weeks.includes(state.from)) state.from = weeks[0] || null;
  if (!state.to || !weeks.includes(state.to)) state.to = weeks[weeks.length - 1] || null;
  setupFilters();
  renderNotices();
  initTabs();
  renderAll();
  document.getElementById('appBody').hidden = false;
}

function allWeeks() {
  // The server skips files with bad dates; this guard keeps one that got
  // through from looping forever (addDays of a non-date never passes the end).
  const set = new Set(PAYLOAD.weekly.map(r => r[1])
    .filter(w => /^\d{4}-\d{2}-\d{2}$/.test(w || '') && addDays(w, 0) === w));
  if (!set.size) return [];
  const sorted = [...set].sort();
  const out = [];
  for (let w = sorted[0]; w <= sorted[sorted.length - 1] && out.length < 5200; w = addDays(w, 7)) out.push(w);
  return out;
}

function setupFilters() {
  const opts = PAYLOAD.people.map(p => ({ value: p.person, label: personLabel(p), title: p.person }));
  if (!peopleMS) {
    peopleMS = new MultiSelect(document.getElementById('peopleFilterMount'), {
      label: 'People', allLabel: 'Everyone', noun: 'person', options: opts,
      onChange: values => { state.people = values; renderAll(); },
    });
  } else {
    peopleMS.setOptions(opts);
    state.people = peopleMS.getValues();
  }
  const from = document.getElementById('dateFrom'), to = document.getElementById('dateTo');
  from.value = state.from || ''; to.value = state.to || '';
  // An end before the start is a slip, not a request for nothing: the two weeks
  // are swapped, the boxes follow, and the summary says so.
  const settle = () => {
    const [first, last, swapped] = orderWeeks(state.from, state.to);
    state.from = first; state.to = last; state.swapped = swapped;
    if (swapped) { from.value = first; to.value = last; }
    clearPreset(); renderAll();
  };
  from.onchange = () => { if (from.value) { state.from = mondayOf(from.value + 'T12:00:00'); settle(); } };
  to.onchange = () => { if (to.value) { state.to = mondayOf(to.value + 'T12:00:00'); settle(); } };
  document.querySelectorAll('#presetRow button').forEach(b => {
    b.onclick = () => {
      const weeks = allWeeks();
      const n = b.dataset.weeks;
      state.to = weeks[weeks.length - 1];
      state.from = n === 'all' ? weeks[0] : weeks[Math.max(0, weeks.length - Number(n))];
      state.swapped = false;
      from.value = state.from; to.value = state.to;
      document.querySelectorAll('#presetRow button').forEach(x => x.classList.toggle('active', x === b));
      renderAll();
    };
  });
  document.querySelectorAll('#heatMetricRow button').forEach(b => {
    b.onclick = () => { state.metric = b.dataset.metric; toggleActive('#heatMetricRow', b); renderHeatmap(); };
  });
  document.querySelectorAll('#modelMetricRow button').forEach(b => {
    b.onclick = () => { state.modelMetric = b.dataset.modelMetric; toggleActive('#modelMetricRow', b); renderModelTrend(); };
  });
  document.querySelectorAll('#mixRow button').forEach(b => {
    b.onclick = () => { state.mix = b.dataset.mix; toggleActive('#mixRow', b); renderMix(); };
  });
}
// The two weeks in order: [first, last, whether they had to be swapped].
function orderWeeks(a, b) { return a > b ? [b, a, true] : [a, b, false]; }
function clearPreset() { document.querySelectorAll('#presetRow button').forEach(x => x.classList.remove('active')); }
function toggleActive(sel, btn) { document.querySelectorAll(sel + ' button').forEach(x => x.classList.toggle('active', x === btn)); }

function renderNotices() {
  const el = document.getElementById('notices');
  const notes = (PAYLOAD.warnings || []).map(w =>
    `<div class="notice ${w.level === 'warn' ? 'warn' : ''}"><span>${esc(w.message)}</span></div>`);
  if (!PAYLOAD.sources.length) {
    notes.unshift(`<div class="notice critical"><span>No exports found in ${esc(PAYLOAD.folder.join(', '))}.
      Ask each person to click <strong>Export (.zip)</strong> in their own dashboard and drop the zip here.</span></div>`);
  }
  el.innerHTML = notes.join('');
  const sub = document.getElementById('subtitle');
  const skipped = (PAYLOAD.skipped || []).length;
  sub.textContent = `${PAYLOAD.people.length} people · ${PAYLOAD.sources.length} export(s)` +
    (skipped ? ` · ${skipped} skipped (see People & data)` : '') + ` · merged ${fmtWhen(PAYLOAD.generated_at)}`;
}

function renderAll() {
  const weeks = allWeeks().filter(weekOk);
  document.getElementById('rangeSummary').textContent =
    weeks.length ? `${shortDay(state.from)} – ${shortDay(addDays(state.to, 6))} · ${weeks.length} weeks` +
      (state.swapped ? ' · start and end were the wrong way round, so they were swapped' : '') : '';
  showTab(currentTab);
}

// ---------------------------------------------------------------- views
function weeklyRows() { return (PAYLOAD.weekly || []).filter(r => personOk(r[W.person]) && weekOk(r[W.week])); }
// The selected weeks as dates: the first Monday to the last Sunday.
function teamRange() { return { from: state.from, to: addDays(state.to, 6) }; }
// What rangeFor filters per person, computed once per render: the limits and
// PRs in range; each person's coverage (the union of their exports' windows);
// and their active-days basis, from all their weekly rows in any week.
function rangeCtx() {
  const intervals = coverageIntervals(PAYLOAD.sources);
  return { limits: limitRows(), intervals,
    basis: activeBasis(PAYLOAD.people, PAYLOAD.weekly, intervals),
    prs: (PAYLOAD.prs || []).filter(r => personOk(r.person) && r.opened_on && weekOk(mondayOf(r.opened_on + 'T12:00:00'))) };
}
// One person's figures for the selected weeks (team-findings.js personInRange).
function rangeFor(p, ctx) {
  ctx = ctx || rangeCtx();
  const rows = PAYLOAD.weekly.filter(r => r[W.person] === p.person && weekOk(r[W.week]));
  return personInRange(p, rows, ctx.limits.filter(l => l.person === p.person),
    ctx.prs.filter(r => r.person === p.person), teamRange(), PAYLOAD.prices,
    { intervals: ctx.intervals[p.person], basis: ctx.basis[p.person] });
}
// Seat use as a percentage. "≈" when some weeks' days aren't exact (an export
// from before exact days): the title gives the range the person's own total
// leaves, or says the days are estimated when there is no total to go on. "≥"
// when the only doubt is that the person worked on two machines: the exports
// count days per machine, so a day worked on both can't be told from two days,
// and the figure is the busier machine's days (a floor).
function seatUseCell(r) {
  const known = r.seatUse != null;
  const mark = !known ? '' : r.lower ? '≥' : r.approx ? '≈' : '';
  let title = '';
  if (known && r.lower) {
    const share = d => fmtPct(Math.min(d / r.workdays, 1));
    title = ` title="${esc(`At least this: they worked on more than one machine, and the exports count days per machine, ` +
      `so days worked on both can't be told from different days. Between ${fmtInt(r.daysLo)} and ${fmtInt(r.daysHi)} active days ` +
      `in these weeks, so ${share(r.daysLo)} to ${share(r.daysHi)}.`)}"`;
  } else if (known && r.approx) {
    title = r.activeLo != null
      ? ` title="${esc(`Between ${fmtInt(r.activeLo)} and ${fmtInt(r.activeHi)} active days in these weeks, from the export's own total; a re-export gives the exact count.`)}"`
      : ' title="Active days are estimated for some weeks; a re-export gives the exact count."';
  }
  return `<td class="num"${title}>${mark}${fmtPct(r.seatUse)}</td>`;
}

function renderFindings() {
  const el = document.getElementById('findingsList');
  const today = todayUTC();
  const ctx = rangeCtx(), range = teamRange();
  const list = findings({
    people: selectedPeople().map(p => Object.assign({}, p, { idle_in_range: rangeFor(p, ctx).idle === true,
      snapshot_in_range: snapshotInRange(p, range) })),
    weekly: weeklyRows(), limits: limitRows(), dayBasis: ctx.basis,
    tools: (PAYLOAD.tools || []).filter(r => personOk(r[0]) && weekOk(r[1])),
    health: ((PAYLOAD.health || {}).rows || []).filter(r => personOk(r.person) && weekOk(r.week)),
    sources: PAYLOAD.sources.filter(s => personOk(s.person)),
  }, today);
  if (!list.length) {
    el.innerHTML = '<div class="empty-state"><div class="es-title">Nothing stands out in these weeks</div></div>';
    return;
  }
  const items = fs => fs.map(f => `<li><a href="#${esc(f.tab)}">${esc(f.text)}</a></li>`).join('');
  const more = list.length - 6;
  el.innerHTML = `<ul class="findings">${items(list.slice(0, 6))}</ul>` +
    (more > 0 ? `<details><summary>+${esc(more)} more</summary><ul class="findings">${items(list.slice(6))}</ul></details>` : '');
}

function renderTrend() {
  const weeks = allWeeks().filter(weekOk);
  const rows = weeklyRows();
  const people = selectedPeople();
  const intervals = coverageIntervals(PAYLOAD.sources);
  const cost = dict(), active = dict(), total = dict();
  rows.forEach(r => {
    cost[r[W.person] + '|' + r[W.week]] = (cost[r[W.person] + '|' + r[W.week]] || 0) + r[W.cost];
    total[r[W.person]] = (total[r[W.person]] || 0) + r[W.cost];
    if (r[W.cost] > 0) (active[r[W.week]] || (active[r[W.week]] = new Set())).add(r[W.person]);
  });
  // A week outside a person's exports is unknown, not $0: not drawn. People
  // with no spend in the range aren't in the legend; the 8 biggest are named
  // and the rest are Other (a ninth is named rather than made an "Other" of one).
  const covered = (p, w) => coveredWeek(personIntervals(p, intervals[p.person]), w);
  const ranked = people.filter(p => total[p.person] > 0).sort((a, b) => total[b.person] - total[a.person] ||
    personLabel(a).localeCompare(personLabel(b)));
  const named = ranked.slice(0, ranked.length === 9 ? 9 : 8), rest = ranked.slice(named.length);
  const series = (list, i, label, color) => ({ type: 'bar', label, stack: 'cost', yAxisID: 'y', backgroundColor: color,
    data: weeks.map(w => { const c = list.filter(p => covered(p, w)); return c.length ? c.reduce((s, p) => s + (cost[p.person + '|' + w] || 0), 0) : null; }) });
  // The newest week is marked when the exports end before its Sunday: part of it is unseen.
  const ends = people.flatMap(p => personIntervals(p, intervals[p.person]).map(iv => iv.to)).sort();
  const seenTo = ends.length ? ends[ends.length - 1] : null;
  const label = w => shortDay(w) + (seenTo && addDays(w, 6) > seenTo ? ' (part)' : '');
  const data = {
    labels: weeks.map(label),
    datasets: [
      ...named.map((p, i) => series([p], i, personLabel(p), cssVar(SERIES_VARS[i % SERIES_VARS.length]))),
      ...(rest.length ? [series(rest, 0, `Other (${rest.length} people)`, cssVar('--other'))] : []),
      { type: 'line', label: 'Active people', yAxisID: 'y1', data: weeks.map(w => (active[w] ? active[w].size : 0)),
        borderColor: cssVar('--modaxo-orange') || '#FB4F00', pointRadius: 3, borderWidth: 2 },
    ],
  };
  const options = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true } },
    scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { callback: v => fmtCost0(v) } },
      y1: { position: 'right', beginAtZero: true, grid: { drawOnChartArea: false }, ticks: { precision: 0 } } } };
  upsert('trend', document.getElementById('trendChart'), { data, options });
}

// Team-wide model use by week: everyone selected, added together, stacked by
// exact model. Cost or output tokens (every request draws on usage, its whole
// context included, so output tokens alone don't measure the allowance).
// The 8 biggest models over the selected weeks are named; the rest are Other
// (a ninth model is named rather than folded into an "Other" of one).
function renderModelTrend() {
  const weeks = allWeeks().filter(weekOk);
  const tokens = state.modelMetric === 'tokens';
  const by = dict(), totals = dict();
  (PAYLOAD.models || []).filter(r => personOk(r[0]) && weekOk(r[1])).forEach(r => {
    const label = modelLabel(r[7] || r[2]);
    const value = (tokens ? r[5] : r[4]) || 0;
    by[label + '|' + r[1]] = (by[label + '|' + r[1]] || 0) + value;
    totals[label] = (totals[label] || 0) + value;
  });
  const ranked = Object.keys(totals).filter(k => totals[k] > 0)
    .sort((a, b) => totals[b] - totals[a] || a.localeCompare(b));
  const named = ranked.slice(0, ranked.length === 9 ? 9 : 8), rest = ranked.slice(named.length);
  const datasets = named.map((label, i) => ({ label, backgroundColor: cssVar(SERIES_VARS[i % SERIES_VARS.length]),
    data: weeks.map(w => by[label + '|' + w] || 0) }));
  if (rest.length) datasets.push({ label: 'Other', backgroundColor: cssVar('--other'),
    data: weeks.map(w => rest.reduce((s, label) => s + (by[label + '|' + w] || 0), 0)) });
  const fmt = tokens ? fmtTokens : fmtCost0;
  const options = { responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: true }, tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmt(c.raw)}` } } },
    scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { callback: v => fmt(v) } } } };
  upsert('modelTrend', document.getElementById('modelTrendChart'), { data: { labels: weeks.map(shortDay), datasets }, options });
}

function renderFreshness() {
  const el = document.getElementById('freshness');
  // One row per person and machine: a second machine's old export is not
  // hidden by the first one's fresh one. The same ages feed the stale finding.
  const today = todayUTC();
  const rows = latestExports(PAYLOAD.sources.filter(s => personOk(s.person)), today);
  if (!rows.length) { el.innerHTML = '<div class="empty-state"><div class="es-title">No exports</div></div>'; return; }
  el.innerHTML = `<div class="table-scroll"><table class="data-table"><thead><tr><th>Person</th><th>Exported</th>` +
    `<th>Window</th><th>Bucketed in</th></tr></thead><tbody>` + rows.map(g => {
      const s = g.source;
      // "more than N days", as the stale finding words it: 7.04 days is past 7.
      const days = g.age == null ? 0 : Math.floor(g.age);
      const badge = g.stale ? ` <span class="badge warn">more than ${esc(days)} days old</span>` : '';
      const zone = s.bucket_zone || 'machine zone (pre-schema 8)';
      const who = nameOf(s.person);
      const machine = s.machine_id ? `<div class="foot-note" style="margin:0">${esc(s.machine_id)}</div>` : '';
      return `<tr><td>${esc(who)}${machine}</td><td>${esc(fmtAsWritten(s.exported_at))}${badge}</td>` +
        `<td>${esc(s.window_from || '?')} – ${esc(s.window_to || '?')}</td><td>${esc(zone)}</td></tr>`;
    }).join('') + `</tbody></table></div>`;
}

function renderRepoSpend() {
  const tbody = document.querySelector('#repoTable tbody');
  const { repos } = repoSpend(weeklyRows());
  if (!repos.length) { tbody.innerHTML = emptyRow(4, 'No spend in range.'); return; }
  tbody.innerHTML = repos.map(r => {
    const label = repoLabel(r);
    // Under half a dollar rounds to "$0": a name next to $0 says nothing.
    const people = r.people.filter(([, c]) => c >= 0.5);
    const who = people.length ? people.map(([p, c]) => `${esc(nameOf(p))} ${fmtCost0(c)}`).join(', ') : '—';
    return `<tr><td>${esc(label)}</td><td class="num">${fmtPct(r.share)}</td>` +
      `<td class="num">${fmtCost0(r.cost)}</td><td>${who}</td></tr>`;
  }).join('');
}

// The table's and legend's name for a repoSpend entry: the folded-in tail is
// "Other (N repos)", so a real repository called "Other" keeps its own name.
function repoLabel(r) { return r.other ? `Other (${r.count} repos)` : r.repo; }

function renderRepoTrend() {
  const weeks = allWeeks().filter(weekOk);
  const rows = weeklyRows();
  const top = repoSpend(rows).repos;
  const named = new Set(top.filter(r => !r.other).map(r => r.repo));
  const OTHER = '\u0000other';  // a key no repository name can be
  const by = dict();
  rows.forEach(r => {
    const repo = r[W.repo] || NO_PROJECT;
    const key = named.has(repo) ? repo : OTHER;
    by[key + '|' + r[W.week]] = (by[key + '|' + r[W.week]] || 0) + r[W.cost];
  });
  const data = { labels: weeks.map(shortDay), datasets: top.map((r, i) => ({ label: repoLabel(r),
    data: weeks.map(w => by[(r.other ? OTHER : r.repo) + '|' + w] || 0),
    backgroundColor: r.other ? cssVar('--other') : cssVar(SERIES_VARS[i % SERIES_VARS.length]) })) };
  const options = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true } },
    scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, ticks: { callback: v => fmtCost0(v) } } } };
  upsert('repoTrend', document.getElementById('repoTrendChart'), { data, options });
}

// Spend per active day. Where a person's days aren't exact (an older export, or
// two machines whose days can overlap) the days and the per-day figure are
// shown as a range with "≈": the most days possible give the lowest rate.
function renderPerDay() {
  const tbody = document.querySelector('#perDayTable tbody');
  const rows = perActiveDay(weeklyRows(), rangeCtx().basis).filter(d => d.cost > 0);
  if (!rows.length) { tbody.innerHTML = emptyRow(5, 'No spend in range.'); return; }
  const max = Math.max(BENCHMARK.p90 * 1.2, ...rows.map(d => d.perDay || 0));
  const pct = v => (100 * v / max).toFixed(1) + '%';
  tbody.innerHTML = rows.sort((a, b) => (b.perDay || 0) - (a.perDay || 0)).map(d => {
    const lo = d.perDayLo == null ? d.perDay : d.perDayLo;
    // Above the line only if even the most days possible leave them over it.
    const verdict = d.perDay == null ? '—'
      : lo > BENCHMARK.p90 ? '<span class="badge warn">above 90th percentile</span>'
      : d.perDay > BENCHMARK.p90 ? 'may be above the 90th percentile'
      : d.perDay > BENCHMARK.average ? 'above average' : 'typical';
    const bar = d.perDay == null ? '' : `<div class="perday-bar"><span style="width:${pct(d.perDay)}"></span>` +
      `<i class="bench-mark" style="left:${pct(BENCHMARK.average)}" title="$${BENCHMARK.average} average"></i>` +
      `<i class="bench-mark p90" style="left:${pct(BENCHMARK.p90)}" title="$${BENCHMARK.p90}: 90th percentile"></i></div>`;
    const who = nameOf(d.person);
    const ranged = d.approx && d.daysLo !== d.daysHi;
    const daysCell = d.approx ? '≈' + (ranged ? `${fmtInt(d.daysLo)}–${fmtInt(d.daysHi)}` : fmtInt(d.days)) : fmtInt(d.days);
    const perDay = d.perDay == null ? '—'
      : !d.approx ? fmtCost0(d.perDay)
      : '≈' + (d.perDayHi == null ? `at least ${fmtCost0(lo)}`
        : Math.round(lo) === Math.round(d.perDayHi) ? fmtCost0(d.perDay) : `${fmtCost0(lo)}–${fmtCost0(d.perDayHi)}`);
    const why = d.approx ? ' title="Estimated active days: the exports give days per machine, or only per project, so the range is what they allow. A re-export of every machine gives the exact days."' : '';
    return `<tr><td>${esc(who)}</td><td class="num">${fmtCost0(d.cost)}</td><td class="num"${why}>${daysCell}</td>` +
      `<td class="num"${why}>${perDay}</td><td>${bar}${verdict}</td></tr>`;
  }).join('');
}

function renderOutput() {
  const tbody = document.querySelector('#outputTable tbody');
  const prs = (PAYLOAD.prs || []).filter(r => personOk(r.person) && r.opened_on && weekOk(mondayOf(r.opened_on + 'T12:00:00')));
  // What the sessions that started in these weeks cost in all, for the line-data share.
  const started = PAYLOAD.session_costs ? PAYLOAD.session_costs.filter(r => personOk(r[0]) && weekOk(r[1])) : null;
  const out = outputByPerson(weeklyRows(), prs, started);
  const people = Object.keys(out).filter(p => out[p].cost > 0 || out[p].prs > 0);
  if (!people.length) { tbody.innerHTML = emptyRow(7, 'No usage or PRs in range.'); return; }
  const blank = v => v == null ? '—' : fmtInt(v);
  tbody.innerHTML = people.sort((a, b) => nameOf(a).localeCompare(nameOf(b))).map(p => {
    const o = out[p];
    return `<tr><td>${esc(nameOf(p))}</td><td class="num">${fmtInt(o.prs)}</td>` +
      `<td class="num">${o.costPerPr == null ? '—' : fmtCost0(o.costPerPr)}</td>` +
      `<td class="num">${blank(o.linesAdded)}</td><td class="num">${blank(o.linesRemoved)}</td>` +
      `<td class="num">${o.perKLines == null ? '—' : fmtCost0(o.perKLines)}</td>` +
      `<td class="num">${o.lineCoverage == null ? '—' : fmtPct(o.lineCoverage)}</td></tr>`;
  }).join('');
}

function renderValue() {
  const tbody = document.querySelector('#valueTable tbody');
  const people = selectedPeople();
  if (!PAYLOAD.prices) {
    tbody.innerHTML = emptyRow(6, 'Add a seat-prices.json next to the exports (see docs/team.md) to compare usage with what each seat costs.');
    return;
  }
  if (!people.length) { tbody.innerHTML = emptyRow(6, 'No people selected.'); return; }
  const ctx = rangeCtx();
  tbody.innerHTML = people.map(p => {
    const tier = tierLabel(p.rate_limit_tier || p.seat_tier), r = rangeFor(p, ctx);
    return `<tr><td>${esc(personLabel(p))}</td><td>${esc(tier)}</td><td class="num">${r.cost == null ? '—' : fmtCost0(r.cost)}</td>` +
      `<td class="num">${r.seatCost == null ? '—' : fmtCost0(r.seatCost)}</td>` +
      `<td class="num">${r.extra ? fmtCost(r.extra) : '—'}</td>` +
      `<td class="num">${r.value == null ? '—' : `<strong>${fmtX(r.value)}</strong>`}</td></tr>`;
  }).join('');
}

// ---------------------------------------------------------------- PRs
function renderPrs() {
  const tbody = document.querySelector('#prTable tbody');
  const prs = (PAYLOAD.prs || []).filter(r => personOk(r.person) && r.opened_on && weekOk(mondayOf(r.opened_on + 'T12:00:00')));
  const by = dict();
  prs.forEach(r => {
    const b = by[r.pr_repo || '(unknown)'] || (by[r.pr_repo || '(unknown)'] = { n: 0, people: dict(), elsewhere: 0, known: 0, costed: 0, cost: 0 });
    b.n++; b.people[r.person] = (b.people[r.person] || 0) + 1;
    // "yes"/"no" is whether the PR's repo is the folder's; blank is not recorded (not "no").
    if (r.in_project_repo === 'no' || r.in_project_repo === 'yes') b.known++;
    if (r.in_project_repo === 'no') b.elsewhere++;
    // A blank session cost is not a free session: that PR has no cost to add or average.
    if (r.session_cost_usd != null) { b.costed++; b.cost += r.session_cost_usd / (r.session_prs || 1); }
  });
  const rows = Object.entries(by).sort((a, b) => b[1].n - a[1].n);
  if (!rows.length) { tbody.innerHTML = emptyRow(6, 'No pull requests logged in range.'); return; }
  tbody.innerHTML = rows.map(([repo, b]) => {
    const unknown = b.n - b.known;
    const another = !b.known ? '<span title="Not recorded: these exports don\'t say which folder the session ran in.">?</span>'
      : fmtInt(b.elsewhere) + (unknown ? ` <span class="foot-note" style="margin:0">${fmtInt(unknown)} not recorded</span>` : '');
    const costNote = b.costed < b.n && b.costed ? ` <span class="foot-note" style="margin:0">${fmtInt(b.costed)} of ${fmtInt(b.n)} PRs</span>` : '';
    return `<tr><td>${esc(repo)}</td><td class="num">${fmtInt(b.n)}</td>
    <td>${Object.entries(b.people).sort((x, y) => y[1] - x[1]).map(([p, n]) => `${esc(nameOf(p))} (${fmtInt(n)})`).join(', ')}</td>
    <td class="num">${another}</td>
    <td class="num">${b.costed ? fmtCost0(b.cost) : '—'}${costNote}</td><td class="num">${b.costed ? fmtCost0(b.cost / b.costed) : '—'}</td></tr>`;
  }).join('');
}

// ---------------------------------------------------------------- habits
// Per person, in the open: each person's habits next to the team median, so a
// manager can see who might need help -- docs/team.md says to tell the team
// first. These are coaching signals, not scores: only the two shares with a
// sourced line (Anthropic's 10% rule) are flagged; the rest are a profile.
function quantile(xs, q) {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b), i = (s.length - 1) * q, lo = Math.floor(i);
  return s[lo] + (s[Math.min(lo + 1, s.length - 1)] - s[lo]) * (i - lo);
}
const FLAG_SHARE = 0.10;
const ratioOf = (a, b) => b > 0 ? a / b : null;
// [column, header, tooltip, value(person sums), cell(value, person sums), flagged]
const HABIT_COLUMNS = [
  ['context', 'Context tax', 'Share of main-conversation spend on requests over 500k tokens. Flagged at Anthropic’s /usage 10% line; the 500k cutoff is this tool’s. Blank for exports older than schema 10: re-export to measure.',
   p => p.very_long_main == null ? null : ratioOf(p.very_long_context_cost_usd, p.very_long_main), v => fmtPct(v), true],
  ['context200', 'Over 200k', 'Share of main-conversation spend on requests over 200k tokens, for context. Not flagged: on 1M-context models most work is over 200k.',
   p => ratioOf(p.long_context_cost_usd, p.est_cost_main_usd), v => fmtPct(v), false],
  ['cold', 'Cold-cache tax', 'Share of spend re-caching after the prompt cache expired, usually from reopening a big session after a break. Anthropic flags 10% or more.',
   p => ratioOf(p.cold_cache_usd, p.est_cost_usd), v => fmtPct(v), true],
  ['checked', 'Checked after last edit', 'Sessions that edited code and then ran a test, lint or build. Not every edit needs one.',
   p => ratioOf(p.sessions_checked_after_edit, p.sessions_edited),
   (v, p) => p ? `${fmtPct(v)} <span class="foot-note" style="margin:0">${fmtInt(p.sessions_checked_after_edit)} of ${fmtInt(p.sessions_edited)}</span>` : fmtPct(v), false],
  ['interrupts', 'Interrupts', 'Per 100 prompts. Experienced users interrupt more, not less (Anthropic, 2026).',
   p => p.prompts > 0 ? 100 * p.interrupts / p.prompts : null, v => v.toFixed(1), false],
  ['plan', 'Plan mode', 'Share of sessions that used plan mode. Worth it for multi-file or unfamiliar work, not one-line fixes.',
   p => ratioOf(p.sessions_plan_mode, p.sessions_started), v => fmtPct(v), false],
  ['subagents', 'Subagents', 'Share of sessions that spawned a subagent.',
   p => ratioOf(p.sessions_with_subagents, p.sessions_started), v => fmtPct(v), false],
  ['claude_md', 'Project CLAUDE.md', 'Share of sessions with a project CLAUDE.md loaded, of those whose Claude Code version records it.',
   p => ratioOf(p.sessions_with_project_claude_md, p.sessions_instructions_recorded), v => fmtPct(v), false],
];
function habitCell(col, v, p) {
  const [key, , , , cell, flagged] = col;
  // Only context tax is blank because an export predates it; elsewhere a dash
  // means nothing to measure (no edits, no prompts), which re-exporting won't change.
  const why = key === 'context' ? 'Not measured: re-export with the current tool' : 'Nothing to measure in these weeks';
  if (v == null) return `<td class="num" title="${why}">—</td>`;
  const text = cell(v, p);
  // Flagged by the figure as shown: 9.96% reads "10.0%", so it carries the badge.
  return `<td class="num">${flagged && Math.round(v * 1000) / 1000 >= FLAG_SHARE ? `<span class="badge warn">${text}</span>` : text}</td>`;
}
function renderTeamHabits() {
  const el = document.getElementById('teamHabits');
  const health = PAYLOAD.health || { rows: [] };
  const by = dict();
  health.rows.filter(r => personOk(r.person) && weekOk(r.week)).forEach(r => {
    const p = by[r.person] || (by[r.person] = { person: r.person });
    Object.entries(r).forEach(([k, v]) => { if (typeof v === 'number') p[k] = (p[k] || 0) + v; });
    // The context tax is measured only on rows that carry it (schema 10), so
    // its denominator is those rows' main-conversation spend, not every week's.
    if (r.very_long_context_cost_usd != null) p.very_long_main = (p.very_long_main || 0) + (r.est_cost_main_usd || 0);
  });
  // An export covers every week in its window, so someone who didn't use
  // Claude Code that week still has a row -- of zeros. They aren't listed.
  const people = Object.values(by).filter(p => p.prompts > 0 || p.est_cost_usd > 0);
  // Who used Claude Code in these weeks but has no habit data (exports from before
  // schema 7 carry none): named, so they aren't mistaken for people who didn't use it.
  const used = new Set(weeklyRows().filter(r => r[W.cost] > 0).map(r => r[W.person]));
  const have = new Set(people.map(p => p.person));
  const missing = [...used].filter(p => !have.has(p)).map(nameOf).sort((a, b) => a.localeCompare(b));
  const missingNote = missing.length
    ? ` Not shown, with no habit data in these weeks (an export from before schema 7: ask them to re-export): ${missing.map(esc).join(', ')}.` : '';
  if (!people.length) {
    el.innerHTML = `<div class="empty-state"><div class="es-title">No one with habit data</div>` +
      `<div class="es-body">No selected person used Claude Code in these weeks with an export that carries habit data (schema 7 or later).${missingNote}</div></div>`;
    return;
  }
  const labels = dict();
  PAYLOAD.people.forEach(p => { labels[p.person] = personLabel(p); });
  people.forEach(p => { p.name = String(labels[p.person] || p.person); });
  people.sort((a, b) => a.name.localeCompare(b.name));
  const head = `<thead><tr><th>Person</th>` + HABIT_COLUMNS.map(([, h, tip]) =>
    `<th class="num" title="${esc(tip)}">${esc(h)}</th>`).join('') + `</tr></thead>`;
  const median = `<tr class="median"><td><strong>Team median</strong></td>` + HABIT_COLUMNS.map(col => {
    const v = quantile(people.map(col[3]).filter(x => x != null), 0.5);
    return habitCell(col, v, null);
  }).join('') + `</tr>`;
  const rows = people.map(p => `<tr data-person="${esc(p.person)}"><td title="${esc(p.person)}">${esc(p.name)}</td>` +
    HABIT_COLUMNS.map(col => habitCell(col, col[3](p), p)).join('') + `</tr>`).join('');
  el.innerHTML = `<div class="table-scroll"><table class="data-table">${head}<tbody>${median}${rows}</tbody></table></div>` +
    `<p class="foot-note">${used.size > people.length
      ? `${people.length} of the ${used.size} people who used Claude Code in the selected weeks have habit data.`
      : `${people.length} ${people.length === 1 ? 'person' : 'people'} used Claude Code in the selected weeks.`}${missingNote} ` +
    `Hover a column for what it means. Highlighted: at or above Anthropic’s 10% line. ` +
    `Context tax is blank for exports made before schema 10 — re-export to measure.</p>`;
}

// Tool errors for the selected people. MCP servers first, every one that meets
// the threshold (a finding names a server, so its row must be here), then the
// built-in tools; the rest past 12 rows sit under "Show more". Claude Code's own
// tools are in their own table. The People column names who hit errors, with
// how many. Tool names that never succeeded are listed apart (analyzeTools).
const FRICTION_ROWS = 12;
function renderFriction() {
  const tbody = document.querySelector('#frictionTable tbody');
  const ccBody = document.querySelector('#ccToolsTable tbody');
  const moreBody = document.querySelector('#frictionMoreBody');
  const moreWrap = document.getElementById('frictionMoreWrap');
  const notes = document.getElementById('frictionNotes');
  const rowsIn = (PAYLOAD.tools || []).filter(r => personOk(r[0]) && weekOk(r[1]));
  const res = analyzeTools(rowsIn);
  const byErrors = (a, b) => b.errors - a.errors || a.label.localeCompare(b.label);
  const hit = res.rows.filter(r => r.calls >= 20 && r.errors > 0);
  const mcp = hit.filter(r => r.mcp && !r.cc).sort(byErrors);
  const builtin = hit.filter(r => !r.mcp && !r.cc).sort(byErrors);
  const cc = hit.filter(r => r.cc).sort(byErrors);
  const ordered = mcp.concat(builtin);
  const shown = Math.max(FRICTION_ROWS, mcp.length);
  const html = rows => rows.map(b => `<tr><td>${esc(b.label)}</td><td class="num">${fmtInt(b.calls)}</td>` +
    `<td class="num">${fmtInt(b.errors)}</td><td class="num">${fmtPct(b.errors / b.calls)}</td>` +
    `<td>${Object.entries(b.people).filter(([, n]) => n > 0).sort((x, y) => y[1] - x[1] || nameOf(x[0]).localeCompare(nameOf(y[0])))
      .map(([p, n]) => `${esc(nameOf(p))} (${fmtInt(n)})`).join(', ')}</td></tr>`).join('');
  tbody.innerHTML = ordered.length ? html(ordered.slice(0, shown))
    : emptyRow(5, 'No integration with 20+ calls returned errors in range, or no export carries tool data yet (schema 7).');
  const rest = ordered.slice(shown);
  if (moreBody) moreBody.innerHTML = html(rest);
  if (moreWrap) {
    moreWrap.hidden = !rest.length;
    const sum = moreWrap.querySelector('summary');
    if (sum) sum.textContent = `Show ${rest.length} more tool${rest.length === 1 ? '' : 's'}`;
  }
  ccBody.innerHTML = cc.length ? html(cc) : emptyRow(5, "No Claude Code tool with 20+ calls returned errors in range.");

  const parts = [];
  const names = (list, fmt) => list.slice(0, 10).map(fmt).join('; ') + (list.length > 10 ? `; and ${list.length - 10} more` : '');
  if (res.neverWorked.length) parts.push(`MCP tool names that never once succeeded while their server's other tools did (a name the ` +
    `model got wrong, or a tool that isn't available), left out of the server's rate: ` +
    esc(names(res.neverWorked, t => `${t.server}: ${t.tool} (${fmtInt(t.calls)} ${t.calls === 1 ? 'call' : 'calls'})`)) + '.');
  if (res.invented.length) parts.push(`Tool names that aren't Claude Code built-ins and never succeeded (made up by the model, or not ` +
    `available), left out: ${esc(names(res.invented, t => `${t.tool} (${fmtInt(t.calls)})`))}.`);
  // Exports from before schema 7 carry no tool data: their people aren't zero-error people.
  const usedNow = new Set(weeklyRows().filter(r => r[W.cost] > 0).map(r => r[W.person]));
  const hasTools = new Set((PAYLOAD.sources || []).filter(s => (s.files || []).includes('tools')).map(s => s.person));
  const noTools = [...usedNow].filter(p => !hasTools.has(p)).map(nameOf).sort((a, b) => a.localeCompare(b));
  if (noTools.length) parts.push(`No tool data from ${noTools.map(esc).join(', ')} (their exports are from before schema 7: ask them to re-export), so their errors are not counted here.`);
  if (notes) notes.innerHTML = parts.map(p => `<p class="foot-note">${p}</p>`).join('');
}

// ---------------------------------------------------------------- tiles
// A limit's hit_at is a UTC instant, so its date prefix is the UTC date and the
// week is the UTC week (not the browser's).
function limitRows() {
  return PAYLOAD.limits.filter(l => personOk(l.person) && l.hit_at && weekOk(mondayOfDay(l.hit_at)));
}
function renderTiles() {
  const people = selectedPeople();
  const weekly = PAYLOAD.weekly.filter(r => personOk(r[0]) && weekOk(r[1]));
  const cost = weekly.reduce((s, r) => s + r[3], 0);
  const limits = limitRows();
  const locked = limits.filter(l => l.locked_out);
  const extra = limits.filter(l => l.limit_type === 'weekly').reduce((s, l) => s + (l.usage_after_hit_usd || 0), 0);
  const ctx = rangeCtx();
  const ranged = people.map(p => rangeFor(p, ctx));
  const idle = ranged.filter(r => r.idle === true).length;
  const uncovered = ranged.filter(r => !r.covered).length;
  // Weeks whose CLI history is already deleted say nothing about use: not idle, not active.
  const deleted = ranged.filter(r => r.historyDeleted).length;
  const noLogs = ranged.filter(r => r.noLogs).length;
  const active = ranged.length - idle - uncovered - deleted - noLogs;
  const use = median(ranged.map(r => r.seatUse).filter(v => v != null));
  const hitPeople = new Set(limits.map(l => l.person)).size;
  const valueMedian = median(ranged.map(r => r.value).filter(v => v != null));
  const value = teamValue(ranged);
  const tiles = [
    ['People', fmtInt(people.length), `${active} active · ${idle} no activity in these weeks` + (uncovered ? ` · ${uncovered} not covered by these weeks` : '') +
      (deleted ? ` · ${deleted} with CLI history deleted` : '') + (noLogs ? ` · ${noLogs} export with no logs` : '')],
    ['Median seat use', fmtPct(use), 'active days ÷ workdays, selected weeks'],
    ['Usage (list price)', fmtCost0(cost), 'in the selected weeks'],
    ['Limit hits', fmtInt(limits.length), `${fmtInt(hitPeople)} ${hitPeople === 1 ? 'person' : 'people'}`],
    ['Locked out', fmtHours(locked.reduce((s, l) => s + l.blocked_hours, 0)),
     `${locked.length} lock-out${locked.length === 1 ? '' : 's'} until reset`],
    ['Extra usage (est.)', fmtCost(extra), 'after weekly limits, list price'],
    ['Value per seat', value == null ? '—' : value.toFixed(1) + '×',
     !PAYLOAD.prices ? 'add seat-prices.json' : 'team usage ÷ (seats + extra), selected weeks' +
       (valueMedian == null ? '' : ` · median seat ${valueMedian.toFixed(1)}×`)],
  ];
  const el = document.getElementById('teamTiles');
  el.style.setProperty('--tile-cols', String(Math.min(tiles.length, 7)));
  el.innerHTML = tiles.map(([l, v, sub]) =>
    `<div class="stat-tile"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div>` +
    (sub ? `<div class="sub">${esc(sub)}</div>` : '') + `</div>`).join('');
}

// ---------------------------------------------------------------- heatmap
function renderHeatmap() {
  const weeks = allWeeks().filter(weekOk);
  const people = selectedPeople();
  const cells = dict();
  PAYLOAD.weekly.forEach(r => {
    const [person, week, , cost, , requests, , , partial, cliGap] = r;
    if (!personOk(person) || !weekOk(week)) return;
    const c = cells[person + '|' + week] || (cells[person + '|' + week] = { cost: 0, requests: 0, rows: [], machines: dict(), cliGap: false });
    c.cost += cost; c.requests += requests; c.rows.push(r);
    // Part-week only if every machine's days of the week were cut short: one machine
    // that covers the whole week makes the week whole.
    const m = r[W.machine] || '';
    c.machines[m] = c.machines[m] || !!partial;
    c.cliGap = c.cliGap || cliGap;
  });
  // The same per-week rule as the People table and spend per active day.
  const ctx = rangeCtx(), basis = ctx.basis;
  Object.values(cells).forEach(c => {
    Object.assign(c, weekDaysFigure(basis[c.rows[0][W.person]], c.rows[0][W.week], c.rows));
    c.partial = Object.values(c.machines).every(Boolean);
  });
  const metric = state.metric;
  const max = Math.max(0, ...Object.values(cells).map(c => c[metric]));
  const table = document.getElementById('heatmap');
  if (!people.length || !weeks.length) { table.innerHTML = emptyRow(1, 'No people or weeks in range.'); return; }
  const fmt = metric === 'cost' ? fmtCost0 : fmtInt;
  const head = `<thead><tr><th>Person</th>${weeks.map(w => `<th title="Week of ${esc(w)}">${esc(shortDay(w))}</th>`).join('')}</tr></thead>`;
  const body = people.map(p => {
    const covers = personIntervals(p, ctx.intervals[p.person]);
    const tds = weeks.map(w => {
      const c = cells[p.person + '|' + w];
      if (!c || !c[metric]) {
        // A week no export of theirs touches is unknown, not a week of no activity.
        if (!c && !coveredWeek(covers, w)) {
          return `<td class="hm-cell uncovered" title="${esc(personLabel(p))} · week of ${esc(w)}: not covered by their exports"></td>`;
        }
        const cls = c && c.cliGap ? ' cli-gap' : '';
        return `<td class="hm-cell${cls}" title="${esc(personLabel(p))} · week of ${esc(w)}: no activity${c && c.cliGap ? ' recorded (CLI history deleted)' : ''}"></td>`;
      }
      const level = max ? Math.max(0.08, c[metric] / max) : 0;
      const cls = (c.partial ? ' partial' : '') + (c.cliGap ? ' cli-gap' : '');
      const tip = `${personLabel(p)} · week of ${w}: ${fmtCost(c.cost)}, ${plural(c.requests, 'request')}, ` +
        `${c.approx ? '≈' : ''}${plural(c.days, 'active day')}${c.approx ? ' (estimated)' : ''}${c.partial ? ' · part-week' : ''}${c.cliGap ? ' · CLI history deleted' : ''}`;
      const text = (metric === 'days' && c.approx ? '≈' : '') + fmt(c[metric]);
      return `<td class="hm-cell${cls}" style="--hm:${level.toFixed(3)}" title="${esc(tip)}">${esc(text)}</td>`;
    }).join('');
    return `<tr><th scope="row" title="${esc(p.person)}">${esc(personLabel(p))}</th>${tds}</tr>`;
  }).join('');
  table.innerHTML = head + `<tbody>${body}</tbody>`;
}

// ---------------------------------------------------------------- limits
const STATUS = { on_track: ['ok', 'On track'], on_pace_to_hit: ['warn', 'On pace to hit'], over: ['warn', 'Over'] };
function renderLimits() {
  const limits = limitRows();
  const by = dict();
  limits.forEach(l => {
    const b = by[l.person] || (by[l.person] = { hits: 0, lockouts: 0, hours: 0, extra: 0 });
    b.hits++;
    if (l.locked_out) { b.lockouts++; b.hours += l.blocked_hours; }
    if (l.limit_type === 'weekly') b.extra += l.usage_after_hit_usd || 0;
  });
  const range = teamRange(), ctx = rangeCtx();
  // A person the selected weeks don't reach (before, after or between their
  // exports) has no hits to count: dashes, listed after everyone who is covered.
  const people = selectedPeople().map(p => ({ p, b: by[p.person] || { hits: 0, lockouts: 0, hours: 0, extra: 0 },
    inRange: snapshotInRange(p, range), covered: rangeFor(p, ctx).covered }))
    .sort((x, y) => (y.covered - x.covered) || y.b.hours - x.b.hours || y.b.hits - x.b.hits ||
      (y.inRange ? y.p.allowance_week_used_pct || 0 : 0) - (x.inRange ? x.p.allowance_week_used_pct || 0 : 0));
  const tbody = document.querySelector('#limitsTable tbody');
  if (!people.length) { tbody.innerHTML = emptyRow(9, 'No people selected.'); return; }
  const notCovered = esc("Their export doesn't cover the selected weeks");
  tbody.innerHTML = people.map(({ p, b, inRange, covered }) => {
    // This week, Status and Projected hit are the person's allowance as of
    // their export: they say nothing about weeks other than the one it ends in.
    const title = esc('As of their export' + (p.window_to ? ' (week of ' + mondayOfDay(p.window_to) + ')' : '') +
      ', outside the selected weeks');
    const st = Object.prototype.hasOwnProperty.call(STATUS, p.allowance_status) ? STATUS[p.allowance_status] : null;
    const pct = p.allowance_week_used_pct == null || p.allowance_week_used_pct === '' ? null : Number(p.allowance_week_used_pct);
    const seat = p.rate_limit_tier ? tierLabel(p.rate_limit_tier) : tierLabel(p.seat_tier);
    const counts = covered
      ? `<td class="num">${fmtInt(b.hits)}</td><td class="num">${fmtInt(b.lockouts)}</td>
      <td class="num">${b.hours ? `<strong>${esc(fmtHours(b.hours))}</strong>` : '—'}</td>
      <td class="num">${b.extra ? fmtCost(b.extra) : '—'}</td>`
      : `<td class="num" title="${notCovered}">—</td>`.repeat(4);
    return `<tr><td title="${esc(p.person)}">${esc(personLabel(p))}</td>
      <td>${esc(seat)}</td>
      ${counts}
      ${inRange ? `<td class="num">${pct == null ? '—' : fmtPct(pct)}</td>
      <td>${st ? `<span class="badge ${st[0]}">${esc(st[1])}</span>` : '<span class="foot-note" style="margin:0" title="No allowance estimate: it needs a weekly-limit hit to calibrate from, and exports from before the allowance columns have none.">no estimate</span>'}</td>
      <td>${esc(fmtWhen(p.projected_limit_hit_at))}</td>`
        : `<td class="num" title="${title}">—</td><td title="${title}">—</td><td title="${title}">—</td>`}</tr>`;
  }).join('');
}

// ---------------------------------------------------------------- overage
// UTC today as YYYY-MM-DD, read through one function so tests can fix it.
function todayUTC() { return new Date().toISOString().slice(0, 10); }
function renderOverage() {
  const weeks = allWeeks().filter(weekOk);
  // The run-rate is for the selected people and weeks (rangeRunRate follows
  // team.overage_forecast): only complete weeks the selected exports cover, so
  // a part-week or a week after the exports isn't averaged in. A week no
  // selected person's export touches (before the exports, or in a gap between
  // two) is unknown, not $0: it is left out of the series and of the basis.
  const today = todayUTC();
  const intervals = coverageIntervals(PAYLOAD.sources);
  const covers = selectedPeople().flatMap(p => personIntervals(p, intervals[p.person]));
  const ends = covers.map(iv => iv.to).sort();
  const coverageTo = ends.length ? (ends[ends.length - 1] < today ? ends[ends.length - 1] : today) : null;
  const rr = rangeRunRate(limitRows(), weeks.filter(w => coveredWeek(covers, w)), coverageTo, today);
  const perWeek = Object.fromEntries(rr.weekly);   // uncovered weeks are absent: unknown, not $0
  const rate = rr.rate, episodes = rr.episodes;
  const projectable = rate != null && episodes > 1;
  const note = document.getElementById('forecastNote');
  note.textContent = !rr.basisWeeks
    ? 'No complete weeks in the selected weeks to compute a run-rate from.'
    : !projectable
      ? `${episodes === 1 ? 'One episode so far' : 'No extra usage so far'}: too little to project a run-rate for the ` +
        `selected people and weeks. Extra usage only happens in weeks someone runs out of allowance.`
      : `Run-rate ${fmtCost(rate)}/week from ${episodes} episodes over the last ${rr.basisWeeks} complete week(s) the ` +
        `exports cover, for the selected people and weeks: about ${fmtCost0(rate * 4)} over the next 4 weeks and ` +
        `${fmtCost0(rate * 13)} over a quarter, if the pattern holds. ` +
        `Extra usage is spiky, so treat this as a run-rate, not a prediction. Each person's extra usage stops at their spend cap.`;
  const canvas = document.getElementById('overageChart');
  const data = {
    labels: weeks.map(shortDay),
    datasets: [
      { type: 'bar', label: 'Extra usage', data: weeks.map(w => w in perWeek ? perWeek[w] : null), backgroundColor: cssVar('--series-1') },
      ...(!projectable ? [] : [{ type: 'line', label: 'Run-rate', data: weeks.map(() => rate),
        borderColor: cssVar('--modaxo-orange') || '#FB4F00', borderDash: [6, 4], pointRadius: 0, borderWidth: 2 }]),
    ],
  };
  const options = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true } },
    scales: { y: { beginAtZero: true, ticks: { callback: v => fmtCost0(v) } } } };
  upsert('overage', canvas, { data, options });
}

// ---------------------------------------------------------------- mix
function renderMix() {
  const people = selectedPeople();
  const rows = PAYLOAD.models.filter(r => personOk(r[0]) && weekOk(r[1]));
  // By model is by the exact model, as the Overview's model chart is (not by family);
  // the 8 biggest are named and the rest are Other.
  const exact = state.mix !== 'thread';
  const keyOf = !exact ? (r => r[3]) : (r => modelLabel(r[7] || r[2]));
  const valOf = state.mix === 'tokens' ? (r => r[5]) : (r => r[4]);
  const groups = dict();
  rows.forEach(r => { const k = keyOf(r); groups[k] = (groups[k] || 0) + valOf(r); });
  const ranked = Object.keys(groups).sort((a, b) => groups[b] - groups[a] || a.localeCompare(b));
  const named = !exact ? ranked : ranked.slice(0, ranked.length === 9 ? 9 : 8);
  const rest = new Set(ranked.slice(named.length));
  const keys = rest.size ? named.concat(['\u0000other']) : named;
  const shownKey = r => rest.has(keyOf(r)) ? '\u0000other' : keyOf(r);
  document.getElementById('mixTitle').textContent =
    state.mix === 'thread' ? 'Main vs subagent cost by person' : state.mix === 'tokens' ? 'Output tokens by model, by person' : 'Cost by model, by person';
  const fmt = state.mix === 'tokens' ? fmtTokens : fmtCost0;
  const data = {
    labels: people.map(personLabel),
    datasets: keys.map((k, i) => ({
      label: k === '\u0000other' ? 'Other' : k,
      data: people.map(p => rows.filter(r => r[0] === p.person && shownKey(r) === k).reduce((s, r) => s + valOf(r), 0)),
      backgroundColor: k === '\u0000other' ? cssVar('--other') : cssVar(SERIES_VARS[i % SERIES_VARS.length]),
    })),
  };
  const options = { indexAxis: 'y', responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: true }, tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmt(c.raw)}` } } },
    scales: { x: { stacked: true, ticks: { callback: v => fmt(v) } }, y: { stacked: true } } };
  upsert('mix', document.getElementById('mixChart'), { type: 'bar', data, options });
}

function upsert(key, canvas, cfg) {
  const has = cfg.data.datasets.some(d => d.data.some(v => v));
  canvas.style.visibility = has ? '' : 'hidden';
  let note = canvas.parentElement.querySelector(':scope > .empty-state');
  if (!has && !note) {
    note = document.createElement('div');
    note.className = 'empty-state';
    note.innerHTML = '<div class="es-title">Nothing in this range</div><div class="es-body">No data matches the current filters.</div>';
    canvas.parentElement.appendChild(note);
  } else if (has && note) note.remove();
  if (!has) return;
  if (charts[key]) { charts[key].destroy(); }
  charts[key] = new Chart(canvas, { type: cfg.type || 'bar', data: cfg.data, options: cfg.options });
}

// ---------------------------------------------------------------- tables
function renderPeople() {
  const tbody = document.querySelector('#peopleTable tbody');
  const people = selectedPeople();
  if (!people.length) { tbody.innerHTML = emptyRow(17, 'No people selected.'); return; }
  const ctx = rangeCtx();
  const num = (v, f) => v == null ? '—' : f(v);
  // Every schema version a person's exports carry: the newest is the row's, the
  // others are named beside it, since older ones are what to re-export.
  const versions = dict();
  PAYLOAD.sources.forEach(s => { (versions[s.person] || (versions[s.person] = new Set())).add(Number(s.schema_version)); });
  tbody.innerHTML = people.map(p => { const seat = tierLabel(p.seat_tier), usage = tierLabel(p.rate_limit_tier), r = rangeFor(p, ctx);
    const lastActive = r.lastActiveWeek ? 'week of ' + shortDay(r.lastActiveWeek) : '—';
    const note = r.covered ? '' : ' <span class="foot-note" style="margin:0">not covered by these weeks</span>';
    const badges = (r.idle ? ' <span class="badge warn">idle</span>' : '') +
      (r.historyDeleted ? ' <span class="badge warn" title="Claude Code had already deleted this person\'s CLI history for these weeks, so only Desktop work shows: whether they used the seat isn\'t known.">history deleted</span>' : '') +
      (p.no_usage_window ? ' <span class="badge warn" title="Their export holds no usage and covers a single day; re-export with --since.">no usage in export</span>' : '');
    const others = [...(versions[p.person] || [])].filter(v => v !== Number(p.schema_version)).sort((a, b) => a - b);
    const windows = (p.windows && p.windows.length ? p.windows : (p.window_from && p.window_to ? [[p.window_from, p.window_to]] : []))
      .map(([a, b]) => `${esc(a)} – ${esc(b)}`).join('<br>') || '? – ?';
    const gap = p.gap_days ? `<div class="foot-note" style="margin:0" title="${esc((p.gaps || []).map(g => g.join(' – ')).join('; '))}">${esc(plural(p.gap_days, 'day'))} between exports not covered</div>` : '';
    return `<tr>
    <td title="${esc(p.person)}">${esc(personLabel(p))}<div class="foot-note" style="margin:0">${esc(p.person)}</div></td>
    <td>${esc(seat)}</td><td>${esc(usage)}</td><td>${esc(p.extra_usage_enabled || '—')}</td>
    ${seatUseCell(r)}
    <td>${esc(lastActive)}${badges}${note}</td>
    <td class="num">${num(r.sessions, fmtInt)}</td><td class="num">${num(r.cost, fmtCost0)}</td>
    <td class="num">${fmtPct(r.subShare)}</td><td class="num">${num(r.prs, fmtInt)}</td>
    <td class="num">${r.seatCost == null ? '—' : fmtCost0(r.seatCost)}</td>
    <td class="num">${r.value == null ? '—' : `<strong>${fmtX(r.value)}</strong>`}</td>
    <td class="num">${num(r.retries, fmtInt)}</td>
    <td>${p.cli_history_truncated === 'yes' ? `<span class="badge warn">from ${esc(p.cli_history_from)}</span>` : 'complete'}</td>
    <td>${windows}${gap}</td>
    <td class="num">${fmtInt(p.exports)}${p.machines > 1 ? `<div class="foot-note" style="margin:0">${fmtInt(p.machines)} machines</div>` : ''}</td>
    <td class="num">${esc(p.schema_version)}${others.length ? `<div class="foot-note" style="margin:0">also ${esc(others.join(', '))}</div>` : ''}</td></tr>`; }).join('');
}

function renderSources() {
  const tbody = document.querySelector('#sourcesTable tbody');
  const rows = PAYLOAD.sources.filter(s => personOk(s.person));
  // Exports left out whole (a file that couldn't be used) are listed too: they are not
  // in the figures, and the person is missing from them. With a person filter they stay.
  const skipped = (PAYLOAD.skipped || []);
  if (!rows.length && !skipped.length) { tbody.innerHTML = emptyRow(7, 'No exports.'); return; }
  const who = s => {
    const name = nameOf(s.person);
    return esc(name) + (name !== s.person ? `<div class="foot-note" style="margin:0">${esc(s.person)}</div>` : '');
  };
  tbody.innerHTML = rows.map(s => `<tr><td>${who(s)}</td><td>${esc(s.source)}</td>
    <td>${esc(fmtAsWritten(s.exported_at))}</td><td>${esc(s.window_from || '?')} – ${esc(s.window_to || '?')}</td>
    <td class="num">${esc(s.schema_version)}</td><td><code>${esc(s.machine_id || '—')}</code></td>
    <td>${esc(s.files.join(', '))}</td></tr>`).join('') +
    skipped.map(k => `<tr><td>${esc(k.person)}</td><td>${esc(k.source)}</td>
    <td colspan="5"><span class="badge warn">skipped</span> ${esc(k.reason)}: not in any figure on this page.</td></tr>`).join('');
}

// ---------------------------------------------------------------- wiring
document.getElementById('refreshBtn').addEventListener('click', () => load().catch(showError));
document.getElementById('exportBtn').addEventListener('click', () => {
  const a = document.createElement('a');
  a.href = '/api/team/export.zip';
  a.download = 'claude-usage-team-merged.zip';
  document.body.appendChild(a); a.click(); a.remove();
});
function showError(err) {
  document.getElementById('notices').innerHTML =
    `<div class="notice critical"><span>Could not load the team data: ${esc(err.message)}</span></div>`;
}
load().catch(showError);
