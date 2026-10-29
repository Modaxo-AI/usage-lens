// ===================== Helpers =====================
const SERIES_VARS = ['--series-1','--series-2','--series-3','--series-4','--series-5','--series-6','--series-7','--series-8'];
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
// Session titles, project paths, branch names and skill/tool names all originate
// in your own transcripts, so anything containing markup would otherwise be
// injected straight into the page by the innerHTML templates below.
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
// PR links come from the logs too; only allow plain http(s) targets.
function safeUrl(u) { return /^https?:\/\//i.test(u || '') ? esc(u) : '#'; }
// Number formatters. A missing value counts as 0; a value that is not finite
// (NaN, Infinity) shows as a dash, never "$∞" or "NaN%". Rounding happens before
// the unit is chosen (999,950 is "1.0M", not "1000.0k"), and -0 shows as 0.
const asNum = n => n == null ? 0 : Number(n);
function fmtInt(n) {
  n = asNum(n);
  return Number.isFinite(n) ? (Math.round(n) || 0).toLocaleString('en-US') : '—';
}
function fmtTokens(n) {
  n = asNum(n);
  if (!Number.isFinite(n)) return '—';
  if (n >= 1e9) return (n/1e9).toFixed(2) + 'B';
  if (n >= 1e6) {
    // 999,999,999 rounds to "1000.00M" at two places: step up to B instead.
    const m = (n/1e6).toFixed(2);
    return +m >= 1000 ? (n/1e9).toFixed(2) + 'B' : m + 'M';
  }
  if (Math.round(n) < 1e3) return fmtInt(n);
  const k = Math.round(n/100) / 10;
  return k >= 1000 ? (n/1e6).toFixed(1) + 'M' : k.toFixed(1) + 'k';
}
function fmtCost(n) {
  n = asNum(n);
  if (!Number.isFinite(n)) return '—';
  return '$' + (Math.round(n*100) / 100 || 0).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function fmtPct(n) {
  if (n == null || !Number.isFinite(Number(n))) return '—';
  const s = (Number(n)*100).toFixed(1);
  return (+s === 0 ? '0.0' : s) + '%';
}
function median(sorted) {
  if (!sorted.length) return null;
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}
// A missing duration is a dash; zero is a real "0s" (a session with no gaps
// between its records), not the same as unknown. Each unit is chosen after
// rounding, so 59.6 s reads "1m" and 59.6 min reads "1.0h", never "60s"/"60m".
function fmtDuration(ms) {
  if (ms == null || !Number.isFinite(Number(ms))) return '—';
  ms = Number(ms);
  if (ms <= 0) return '0s';
  // Under a minute is seconds: rounding 40s to "1m" overstates it by half.
  const secs = Math.round(ms / 1000);
  if (secs < 60) return Math.max(1, secs) + 's';
  const m = ms / 60000;
  if (Math.round(m) >= 60) return (m/60).toFixed(1) + 'h';
  return Math.round(m) + 'm';
}
function shortProjectLabel(path) {
  if (path === 'Other' || !path) return path || '(unknown)';
  // Synthetic names like "(temp e2e)" or "(unknown project)" aren't paths and
  // must not pick up a leading slash.
  if (!path.includes('/')) return path;
  const parts = path.split('/').filter(Boolean);
  const tail = parts.slice(-2).join('/');
  const prefix = path.startsWith('~') ? '' : '/';
  return parts.length > 2 ? '.../' + tail : prefix + tail;
}
// Trailing 6-hex-character suffix on an auto-generated branch name, e.g.
// "claude/repo-overview-d339f9". Only Claude Code's own `claude/` branches get
// one; a branch you named "fix-deadbee" keeps its name.
const _BRANCH_SUFFIX = /-[0-9a-f]{6}$/;

// "~/code/team/myrepo" -> "myrepo"
function repoName(project) {
  if (!project) return '(unknown)';
  const parts = project.replace(/\/+$/, '').split('/').filter(Boolean);
  return parts.length ? parts[parts.length - 1] : project;
}

function cleanBranchName(raw) {
  // Not a branch: git records HEAD whenever the checkout is detached, which is
  // what worktrees, rebases and bisects all look like.
  if (!raw) return '(no branch)';
  if (raw === 'HEAD') return '(detached)';
  let name = raw;
  if (name.startsWith('claude/')) name = name.slice('claude/'.length).replace(_BRANCH_SUFFIX, '');
  // Never clean a name down to nothing.
  return name || raw;
}

// Two lines — repo above, branch below. A single "repo · branch" string is long
// enough to get clipped by the axis; Chart.js renders an array as two lines, so
// neither part has to be truncated.
function branchLabel(meta) {
  if (!meta) return '(unknown)';
  return [repoName(meta.project), '· ' + cleanBranchName(meta.raw)];
}

// For tooltips on times shown in the viewer's zone ("start" column).
function viewerZoneNote() {
  let zone = '';
  try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { /* no Intl */ }
  return 'Shown in your timezone' + (zone ? ' (' + zone + ')' : '') + '. Filters and day totals use the day on the machine that built this data.';
}
function localDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
// A session's or limit event's calendar day. Usage rows are bucketed by the
// local day of the machine that built the payload, so these must be too: a
// static dashboard opened in another timezone would otherwise put a session on
// a different day from its own cost rows. `*_day` is that day; localDate() is
// the fallback for a payload written before the fields existed (and is exact
// whenever the viewer is in the build zone, as with the live server).
function sessionRawStartDay(s) { return s.start_day || localDate(s.start); }
// A fork or resume starts with a copy of the original's records, so its `start`
// is the original's: up to two weeks before the work it did itself. `active_start`
// is its own first record. Display and the date filter use that; only the CLI
// counter's window (which really began with the original) uses the raw start.
// `start_day` is a day on the build machine, and active_start is an instant, so
// the shift between the two is measured in the viewer's zone and added to it.
function sessionStartDay(s) {
  const base = sessionRawStartDay(s);
  if (s.active_start_day) return s.active_start_day;
  if (!base || !s.active_start || !s.start) return base;
  const a = localDate(s.start), b = localDate(s.active_start);
  if (!a || !b || a === 'NaN-NaN-NaN' || b === 'NaN-NaN-NaN') return base;
  const shift = daysBetween(a, b) - 1;
  return shift > 0 ? addDays(base, shift) : base;
}
// The instant a session's own work began (for the Start column and sorting).
function sessionStartTime(s) { return s.active_start || s.start; }
function sessionEndDay(s) { return s.end_day || localDate(s.end); }
function limitHitDay(e) { return e.hit_day || localDate(e.hit_at); }
// ===================== Time granularity =====================
// Minutes per bucket. MONTH is a sentinel: calendar months aren't a fixed
// number of minutes, so it's handled separately in rollup()/bucketLabel().
const MONTH = 'month';
const GRAINS = [
  { key: 5,     label: '5m',    noun: '5 minutes'  },
  { key: 10,    label: '10m',   noun: '10 minutes' },
  { key: 15,    label: '15m',   noun: '15 minutes' },
  { key: 30,    label: '30m',   noun: '30 minutes' },
  { key: 60,    label: '1h',    noun: 'hour'       },
  { key: 240,   label: '4h',    noun: '4 hours'    },
  { key: 480,   label: '8h',    noun: '8 hours'    },
  { key: 1440,  label: 'Day',   noun: 'day'        },
  { key: 10080, label: 'Week',  noun: 'week'       },
  { key: MONTH, label: 'Month', noun: 'month'      },
];
// Beyond this many bars a chart stops being readable and starts being slow,
// so finer grains are disabled for the selected range rather than rendered.
const MAX_BUCKETS = 500;

function grainInfo(key) { return GRAINS.find(g => g.key === key) || GRAINS[6]; }

// Payload column 0 is a base bucket ("2026-10-02T14:35"); its first 10
// characters are the local date, which is what the range filter compares on.
function dayOf(key) { return key ? String(key).slice(0, 10) : null; }

function minutesOf(key) {
  const s = String(key);
  if (s.length <= 10) return 0;
  return parseInt(s.slice(11, 13), 10) * 60 + parseInt(s.slice(14, 16), 10);
}

// Roll a base bucket up into the active grain.
function rollup(key, grain) {
  const day = dayOf(key);
  if (grain === MONTH) return day.slice(0, 7);
  if (grain === 10080) return weekOf(day);
  if (grain === 1440) return day;
  const slot = Math.floor(minutesOf(key) / grain) * grain;
  return day + 'T' + String(Math.floor(slot / 60)).padStart(2, '0') +
    ':' + String(slot % 60).padStart(2, '0');
}

function shortDay(day) {
  return new Date(day + 'T00:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

// Axis label. Sub-day buckets show the clock time, with the date added as a
// second line only when the day changes -- repeating it on every bar is noise.
function bucketLabel(key, grain, prevKey) {
  if (grain === MONTH) {
    return new Date(key + '-01T00:00:00').toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
  }
  if (grain === 10080) return shortDay(key);
  if (grain === 1440) return shortDay(key);
  const time = String(key).slice(11, 16);
  const day = dayOf(key);
  return (prevKey && dayOf(prevKey) === day) ? time : [time, shortDay(day)];
}

// Full, unambiguous datetime for tooltips.
function bucketTitle(key, grain) {
  if (grain === MONTH) {
    return new Date(key + '-01T00:00:00').toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  }
  if (grain === 10080) return 'Week of ' + shortDay(key);
  if (grain === 1440) return new Date(key + 'T00:00:00').toLocaleDateString();
  const d = new Date(key);
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) + ', ' +
    d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

// Number of buckets the grain would produce across the selected range. Used to
// disable grains that would render thousands of bars.
// Counted the way axisKeys() lays the axis out (whole calendar weeks and
// months, so a range that straddles a boundary draws one more bar than its
// length in days suggests).
function bucketCountFor(grain) {
  const days = daysBetween(state.from, state.to);
  if (grain === MONTH) {
    const [fy, fm] = state.from.slice(0, 7).split('-').map(Number);
    const [ty, tm] = state.to.slice(0, 7).split('-').map(Number);
    return (ty - fy) * 12 + (tm - fm) + 1;
  }
  if (grain === 10080) return (daysBetween(weekOf(state.from), weekOf(state.to)) - 1) / 7 + 1;
  return Math.ceil(days * 1440 / grain);
}

// Every bucket across the selected range, in order -- including the empty ones.
// Emitting only buckets that hold data would silently close up idle periods, so
// a quiet weekend would look like it never happened and the shape of the series
// would be a lie. Gaps are the point of a time axis.
function axisKeys(grain) {
  const keys = [];
  const pad = n => String(n).padStart(2, '0');
  if (grain === MONTH) {
    const d = new Date(state.from.slice(0, 7) + '-01T00:00:00');
    const end = new Date(state.to.slice(0, 7) + '-01T00:00:00');
    while (d <= end) {
      keys.push(d.getFullYear() + '-' + pad(d.getMonth() + 1));
      d.setMonth(d.getMonth() + 1);
    }
  } else if (grain === 10080) {
    const d = new Date(weekOf(state.from) + 'T00:00:00');
    const end = new Date(weekOf(state.to) + 'T00:00:00');
    while (d <= end) { keys.push(localDate(d.toISOString())); d.setDate(d.getDate() + 7); }
  } else if (grain === 1440) {
    let day = state.from;
    while (day <= state.to) { keys.push(day); day = addDays(day, 1); }
  } else {
    let day = state.from;
    while (day <= state.to) {
      for (let m = 0; m < 1440; m += grain) {
        keys.push(day + 'T' + pad(Math.floor(m / 60)) + ':' + pad(m % 60));
      }
      day = addDays(day, 1);
    }
  }
  return keys;
}

// Builds {labels, titles, keys} for a continuous x axis over the current range.
function axisFor(grain) {
  const keys = axisKeys(grain);
  return {
    keys,
    labels: keys.map((k, i) => bucketLabel(k, grain, i ? keys[i - 1] : null)),
    titles: keys.map(k => bucketTitle(k, grain)),
  };
}

function weekOf(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  const day = d.getDay();
  const diff = (day === 0 ? -6 : 1) - day; // Monday start
  d.setDate(d.getDate() + diff);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
function topNPlusOther(totals, n) {
  const entries = Object.entries(totals).sort((a,b) => b[1]-a[1]);
  const top = entries.slice(0, n);
  const rest = entries.slice(n).reduce((s,[,v]) => s+v, 0);
  if (rest > 0) top.push(['Other', rest]);
  return top;
}
function seqColor(frac) {
  // Simplified single-hue sequential ramp: mix the blue series color toward
  // the surface. frac in [0,1], 0 = near-surface (low), 1 = full hue (high).
  const pct = Math.round(10 + frac * 80);
  return `color-mix(in srgb, ${cssVar('--series-1')} ${pct}%, ${cssVar('--surface-1')})`;
}

// ===================== State =====================
// collect.py --static flips this to false when it bakes the data in: a
// standalone file has no server behind it, so the export routes don't exist and
// the button must not be offered.
let SERVED_BY_DASHBOARD = true;
let PAYLOAD = null;
let sessionById = {};
let modelColor = {}, skillColor = {}, agentTypeColor = {};
const state = { from: null, to: null, projects: [], models: [], metric: 'tokens', grain: 480, skillsMetric: 'tokens', agentsMetric: 'tokens', showForks: false };
let currentTab = 'overview';
const dirty = { overview: true, habits: true, skills: true, tools: true, agents: true, sessions: true, env: true };
let sessionSort = { key: 'start', dir: -1 };
const charts = {};

// Requests with no `attributionSkill` on the log line. Not a skill -- it is
// ordinary work happening outside any skill, and on a real log set it is ~91%
// of cost, which flattens every real skill to an unreadable sliver. The Skills
// tab therefore charts attributed work only and reports this share separately.
const NO_SKILL = '(none)';

// A subagent whose parent transcript is missing has no session row; its agent
// row carries the project it was placed in.
let agentProject = {};
function indexAgentProjects() {
  agentProject = Object.fromEntries((PAYLOAD.agents || []).filter(a => a.project).map(a => [a.session_id, a.project]));
}
function projectOf(sid) { return (sessionById[sid] && sessionById[sid].project) || agentProject[sid] || '(unknown)'; }
function modelsOfSession(sid) { return (sessionById[sid] && sessionById[sid].models) || []; }
function inDateRange(key) { const d = dayOf(key); return d && d >= state.from && d <= state.to; }
function projectOk(proj) { return !state.projects.length || state.projects.includes(proj); }
function modelOk(model) { return !state.models.length || state.models.includes(model); }
function modelsOk(models) { return !state.models.length || (models || []).some(m => state.models.includes(m)); }

function usageRows() {
  return PAYLOAD.usage.filter(r => inDateRange(r[0]) && projectOk(projectOf(r[1])) && modelOk(r[2]));
}
// Tool rows: [date, session, name, calls, errors, is_sub, model]. The model is
// the one whose response made the call, so the model filter keeps only those
// calls; a payload from before the column existed falls back to "sessions that
// used the model".
function toolModelOk(r) { return r[6] != null ? modelOk(r[6]) : modelsOk(modelsOfSession(r[1])); }
function toolRows() {
  return PAYLOAD.tools.filter(r => inDateRange(r[0]) && projectOk(projectOf(r[1])) && toolModelOk(r));
}
// Skill-call rows: [date, session, skill, calls] then, in either order, the
// calling model and how it started ("tool" for the Skill tool, "command" for a
// slash command). Told apart by value, so the column order isn't load-bearing.
const SKILL_VIA = ['tool', 'command'];
function skillCallParts(r) {
  const extra = r.slice(4);
  const via = extra.find(v => SKILL_VIA.includes(v)) || 'tool';
  const model = extra.find(v => typeof v === 'string' && !SKILL_VIA.includes(v));
  return { model: model == null ? null : model, via };
}
function skillCallModelOk(r) {
  const m = skillCallParts(r).model;
  return m != null ? modelOk(m) : modelsOk(modelsOfSession(r[1]));
}
function skillCallRows() {
  return PAYLOAD.skillCalls.filter(r => inDateRange(r[0]) && projectOk(projectOf(r[1])) && skillCallModelOk(r));
}
function agentRows() {
  return PAYLOAD.agents.filter(a => inDateRange(a.date) && projectOk(projectOf(a.session_id)) && modelsOk(a.models));
}
// Forks/resumes are copies of a transcript that is already listed, so they are
// hidden by default -- counting them would double-count sessions and their
// active time. The Sessions tab has a toggle to show them, and that toggle
// changes that table only: every count elsewhere (Overview, Output & env) is of
// the originals either way.
function sessionInFilters(s) {
  const sd = sessionStartDay(s), ed = sessionEndDay(s) || sd;
  return sd && ed >= state.from && sd <= state.to && projectOk(s.project) && modelsOk(s.models);
}
function sessionRows() {
  return PAYLOAD.sessions.filter(s => (!s.derived || state.showForks) && sessionInFilters(s));
}
// Sessions that made at least one request, originals only: what "a session"
// means in the header and every tile. A transcript with no reply is listed on
// the Sessions tab, with a badge, and counted nowhere else.
function countedSessions() {
  return PAYLOAD.sessions.filter(s => !s.derived && didWork(s) && sessionInFilters(s));
}
// Friction and limit hits belong to a session rather than a model, so the model
// filter keeps the ones from sessions that used a selected model.
function sessionOk(sid) { return projectOk(projectOf(sid)) && modelsOk(modelsOfSession(sid)); }
// Pull requests and lines changed aren't recorded per model, so a model filter
// can't split them: they follow the project and dates only, and the tiles say so
// (a session-level model rule would show Haiku-only work as 61% of all lines).
function prRows() {
  return PAYLOAD.prs.filter(p => inDateRange(p.date) && projectOk(projectOf(p.session_id)));
}
function tokenTotal(r) { return r[5] + r[6] + r[7] + r[8] + r[9]; }
// Cost per session from the *filtered* usage rows, so a session's figure
// matches Total cost: only its work inside the date range and on the selected
// models. A session's est_cost_main/est_cost_sub are whole-session, all-model.
function costBySession(rows) {
  const out = {};
  rows.forEach(r => { out[r[1]] = (out[r[1]] || 0) + r[13]; });
  return out;
}
function wholeSessionCost(s) { return s.est_cost_main + s.est_cost_sub; }

// ===================== Boot =====================
// A non-2xx answer carries a JSON error, not a payload; treat it as a failure
// rather than rendering an empty dashboard from it.
function jsonOrThrow(r) {
  if (r.ok) return r.json();
  return r.json().catch(() => ({})).then(body => {
    throw new Error(body.error || `the server answered ${r.status}`);
  });
}

// Every request the page makes to its own server goes through here. The server
// refuses /api/* requests that don't prove they came from this page; browsers
// that send neither Sec-Fetch-Site nor Origin on a same-origin GET (older
// Safari) are covered by this header, which another site's page can't add
// without a CORS preflight the server never grants.
function apiFetch(url, opts) {
  return fetch(url, Object.assign({}, opts, {
    headers: Object.assign({}, opts && opts.headers, { 'X-Claude-Usage': '1' }),
  }));
}

// The one place data is loaded -- boot and Refresh both come through here, and
// collect.py --static replaces this exact fetch with the baked-in payload. It
// uses the parse cache, so a Refresh only re-reads files that have changed.
function loadData() {
  return apiFetch('/api/data').then(jsonOrThrow);
}

// Shown in the notices strip, so a failed load or refresh says so instead of
// leaving the skeleton or a "Refreshing…" button up indefinitely.
function showLoadError(what, err) {
  const el = document.getElementById('notices');
  if (!el) return;
  el.querySelectorAll('.notice.load-error').forEach(n => n.remove());
  const note = document.createElement('div');
  note.className = 'notice critical load-error';
  note.innerHTML = `<span>${esc(what)}: ${esc((err && err.message) || err)}. ` +
    `Check that <code>serve.py</code> is still running, then reload the page.</span>`;
  el.prepend(note);
}

loadData().then(data => {
  PAYLOAD = data;
  init();
}).catch(err => {
  document.getElementById('loadingSkeleton').hidden = true;
  showLoadError('Could not load usage data', err);
});

let booted = false;

// The palette comes from CSS variables that change with the OS light/dark
// setting, but a chart reads them once when it is made. When the setting flips,
// the charts are rebuilt so they don't sit in the old theme until a reload.
function rethemeCharts() {
  assignColors();
  Object.keys(charts).forEach(k => { try { charts[k].destroy(); } catch (e) { /* already gone */ } delete charts[k]; });
  markAllDirty();
}
function watchColorScheme() {
  try {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    if (mq.addEventListener) mq.addEventListener('change', rethemeCharts);
    else if (mq.addListener) mq.addListener(rethemeCharts);
  } catch (e) { /* no matchMedia: charts keep the theme they were made in */ }
}

function init() {
  sessionById = Object.fromEntries(PAYLOAD.sessions.map(s => [s.id, s]));
  indexAgentProjects();

  document.getElementById('loadingSkeleton').hidden = true;
  document.getElementById('appBody').hidden = false;

  renderHeaderMeta();
  renderNotices();

  assignColors();
  populateFilters();
  if (!booted) {
    wireFilterControls();
    wireTabs();
    watchColorScheme();
    booted = true;
  }
  markAllDirty();
}

function renderHeaderMeta() {
  const sum = PAYLOAD.summary;
  const forkNote = sum.derived_sessions
    ? ` <span class="badge" title="Fork/resume copies of a transcript already counted — excluded from the session count.">+${sum.derived_sessions} copies</span>` : '';
  document.getElementById('subtitle').innerHTML =
    `<span><strong>${fmtInt(sum.total_sessions)}</strong> sessions${forkNote}</span>` +
    `<span class="sep">&middot;</span>` +
    `<span><strong>${fmtInt(sum.total_subagents)}</strong> subagents</span>` +
    `<span class="sep">&middot;</span>` +
    `<span>${esc(new Date(PAYLOAD.generated_at).toLocaleString())}</span>` +
    `<span class="sep">&middot;</span>` +
    `<span title="${PAYLOAD.files_parsed} file(s) parsed, ${PAYLOAD.files_cached} served from cache">` +
    `scan ${fmtInt(PAYLOAD.scan_ms)}ms</span>`;

  wireExport();

  const btn = document.getElementById('refreshBtn');
  // A standalone file has the data baked in: "refreshing" would reload the same
  // numbers, so the button is offered only where a server can read new logs.
  btn.hidden = !SERVED_BY_DASHBOARD;
  if (SERVED_BY_DASHBOARD && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => {
      btn.disabled = true;
      btn.innerHTML = '<span aria-hidden="true">&#8635;</span> Refreshing…';
      const done = () => {
        btn.disabled = false;
        btn.innerHTML = '<span aria-hidden="true">&#8635;</span> Refresh';
      };
      loadData().then(data => {
        PAYLOAD = data;
        done();
        init();
      }).catch(err => {
        done();
        showLoadError('Refresh failed', err);
      });
    });
  }
}

// The export is the one output of this tool meant to be handed to someone else,
// so the button says what the files contain rather than leaving that to the
// README. Only the date range is applied: a project or model filter left on by
// accident would produce a partial file that still looks complete.
// One click, one download: every CSV arrives inside a zip.
//
// This cannot be two separate CSV downloads from one button. Browsers refuse
// multiple automatic downloads arising from a single user gesture, so the first
// file landed and the second was silently dropped. A zip is one download, so the
// gesture limit never applies.
//
// Only the date range is applied. A project or model filter left on by accident
// would produce a partial file that still looks complete.
function wireExport() {
  const btn = document.getElementById('exportBtn');
  if (!btn) return;
  if (!SERVED_BY_DASHBOARD) {
    // Standalone build: no /api/export to download from.
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  // The page shows this machine's days, but the files are bucketed in the export
  // zone (a team setting), and the selected dates are read in it too.
  const zone = PAYLOAD && PAYLOAD.export_zone;
  const zoneSentence = zone
    ? `Exported days and weeks use ${zone} (set CLAUDE_USAGE_EXPORT_TZ to change); ` +
      'your dashboard shows local days.'
    : '';
  btn.title =
    'Downloads one zip holding the six business-review CSVs: a person x project x ' +
    'week summary, a per-session sheet, and the breakdown, limits, PRs and tools ' +
    'files.\n\nCovers the selected date range only: the project and model filters are ' +
    'ignored, and the dates are read as days in the export zone' +
    (zone ? ` (${zone})` : ' (UTC unless CLAUDE_USAGE_EXPORT_TZ is set)') + ', so a total can differ slightly ' +
    'from this page\'s. The All range exports everything. ' +
    'Identifies you by email and includes session titles, project paths, branch ' +
    'names and PR links.\nContains no prompts, no replies and no code.' +
    (zoneSentence ? '\n\n' + zoneSentence : '');
  const note = document.getElementById('exportNote');
  if (note) {
    note.hidden = !zone;
    note.title = zoneSentence;
    note.innerHTML = zone ? `Export days and weeks: <strong>${esc(zone)}</strong>` : '';
  }
  if (btn.dataset.wired) return;
  btn.dataset.wired = '1';
  btn.addEventListener('click', () => {
    const a = document.createElement('a');
    a.href = exportUrl();
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
    // A download gives no page-visible feedback, so acknowledge it briefly.
    const original = btn.innerHTML;
    btn.innerHTML = '<span aria-hidden="true">&#10003;</span> Exported';
    setTimeout(() => { btn.innerHTML = original; }, 1600);
  });
}

// The All range sends no dates at all. This page's first and last day are local
// days, and the server reads dates as days in the export zone, so turning "All"
// into those two dates would drop the hours at either edge that fall on the
// other side of midnight there.
function exportUrl() {
  const all = state.from === state._minDate && state.to === state._maxDate;
  return '/api/export/all.zip' + (all ? '' :
    `?from=${encodeURIComponent(state.from)}&to=${encodeURIComponent(state.to)}`);
}

// Anything that silently makes the numbers wrong gets said out loud, rather than
// only being printed by collect.py where a dashboard user never sees it.
function renderNotices() {
  const el = document.getElementById('notices');
  if (!el) return;
  const notes = [];
  const unknown = PAYLOAD.unknown_models || [];
  if (unknown.length) {
    notes.push(['critical',
      `No pricing for ${unknown.map(esc).join(', ')} — requests on ${unknown.length === 1 ? 'this model' : 'these models'} ` +
      `are counted at $0, so every cost below is understated. Add ${unknown.length === 1 ? 'it' : 'them'} to claude_usage/pricing.py.`]);
  }
  const history = PAYLOAD.history || {};
  // A standalone file can't re-check anything: the price comparison and the
  // price age below are as of the day it was built, and it says so.
  const builtOn = PAYLOAD.generated_at ? localDate(PAYLOAD.generated_at) : null;
  const frozen = SERVED_BY_DASHBOARD ? ''
    : ` <em>(as of ${esc(builtOn || 'when this file was built')}, the day it was built; this file does not re-check.)</em>`;
  if (history.cli_history_truncated) {
    // The day the oldest CLI transcript begins, not the day it was last written
    // (which the payload's own field is).
    const cliStarts = PAYLOAD.sessions.filter(s => !s.derived &&
      Object.keys(s.entrypoints || {}).some(k => /cli/i.test(k))).map(sessionRawStartDay).filter(Boolean).sort();
    const cliFrom = cliStarts.length ? cliStarts[0] : history.cli_history_from;
    notes.push(['warn',
      `CLI history on this machine starts ${esc(cliFrom)}. Claude Code deletes CLI ` +
      `transcripts after ${history.retention_days_default} days by default, so CLI work before then is ` +
      `missing here and only Desktop sessions remain. To keep it from now on, add ` +
      `<code>"cleanupPeriodDays": 365</code> to <code>~/.claude/settings.json</code>.`]);
  }
  const prices = PAYLOAD.pricing || {};
  const check = PAYLOAD.price_check || {};
  // "differs" also covers a published-but-unpriced model in the logs, which has
  // no rate differences to list; the banner below reports that case.
  if (['differs', 'failed'].includes(check.status) && (check.differences || []).length) {
    // A rate, or (unit "tokens") a long-prompt threshold; null is "none".
    const amount = (d, v) => v == null ? 'none'
      : d.unit === 'tokens' ? `over ${Number(v).toLocaleString()} tokens` : `$${v}`;
    const rows = check.differences.map(d =>
      `${esc(d.model)} ${esc(d.field.replace(/_/g, ' '))}: ${esc(amount(d, d.ours))} here, ` +
      `${esc(amount(d, d.published))} published` +
      (d.used ? '' : ' (not in your logs)')).join('; ');
    notes.push(['warn',
      `Anthropic's <a href="${safeUrl(check.url)}" target="_blank" rel="noopener">published prices</a> differ from this ` +
      `tool's rate table — ${rows}. Costs below still use the table. Pull the latest version of the tool; ` +
      `if it still differs, the table needs a dated row (see docs/cost.md).${frozen}`]);
  }
  // The page parsed only in part, so "prices match" can't be said even if every
  // readable rate does.
  const unread = [
    ...(check.unparsed_tiers || []).map(m => `unparsed tiers: ${esc(m)}`),
    ...(check.skipped || []).map(n => `unrecognised model row: ${esc(n)}`),
    ...(check.multi_price || []).map(c => `more than one price in one cell: ${esc(c)}`),
    ...(check.unverified || []).map(m => `no row on the page for ${esc(m)}, which is in your logs`)];
  if (check.status === 'failed' && unread.length) {
    notes.push(['warn',
      `Couldn't fully read Anthropic's <a href="${safeUrl(check.url)}" target="_blank" rel="noopener">published prices</a> ` +
      `(${unread.join('; ')}), so they can't be confirmed to match this tool's rate table.`]);
  }
  const unpricedUsed = [...(check.missing || []), ...(check.tiered || [])].filter(m => m.used).map(m => m.model);
  if (unpricedUsed.length) {
    notes.push(['warn',
      `${unpricedUsed.map(esc).join(', ')} ${unpricedUsed.length === 1 ? 'is' : 'are'} published by Anthropic but not ` +
      `priced by this tool, and ${unpricedUsed.length === 1 ? 'is' : 'are'} in your logs.`]);
  }
  // A check that succeeded is a better guarantee than the date someone last
  // looked by hand, so the age warning only shows without one.
  if (prices.stale && !['ok', 'differs'].includes(check.status)) {
    notes.push(['warn',
      `Prices were last checked against <a href="https://claude.com/pricing" target="_blank" rel="noopener">claude.com/pricing</a> ` +
      `on ${esc(prices.checked)}, ${fmtInt(prices.age_days)} days ago. Re-check them; if any price changed, add a dated row to ` +
      `<code>claude_usage/pricing.py</code> (never edit a past row) and set <code>PRICES_CHECKED</code> to the day you checked.${frozen}`]);
  }
  if (PAYLOAD.summary.fast_mode_requests) {
    notes.push(['warn',
      `${fmtInt(PAYLOAD.summary.fast_mode_requests)} request(s) ran in fast mode and are priced at 2× standard rates.`]);
  }
  el.innerHTML = notes.map(([kind, text]) =>
    `<div class="notice ${kind}"><span>${text}</span></div>`).join('');
}

function assignColors() {
  const modelCost = {}, skillCost = {}, agentTypeCost = {};
  PAYLOAD.usage.forEach(r => {
    const [, sid, model, skill, agentType, , , , , , , , , cost] = r;
    modelCost[model] = (modelCost[model]||0) + cost;
    const sk = skill || NO_SKILL;
    skillCost[sk] = (skillCost[sk]||0) + cost;
    if (agentType !== -1) agentTypeCost[agentType] = (agentTypeCost[agentType]||0) + cost;
  });

  const models = Object.keys(modelCost).sort((a,b) => modelCost[b]-modelCost[a]);
  models.forEach((m,i) => modelColor[m] = cssVar(SERIES_VARS[i % SERIES_VARS.length]));

  skillColor[NO_SKILL] = cssVar('--other');
  Object.keys(skillCost).filter(s => s !== NO_SKILL).sort((a,b) => skillCost[b]-skillCost[a])
    .forEach((s,i) => skillColor[s] = cssVar(SERIES_VARS[i % SERIES_VARS.length]));

  agentTypeColor['unknown'] = cssVar('--other');
  Object.keys(agentTypeCost).filter(t => t !== 'unknown').sort((a,b) => agentTypeCost[b]-agentTypeCost[a])
    .forEach((t,i) => agentTypeColor[t] = cssVar(SERIES_VARS[i % SERIES_VARS.length]));
}

function colorFor(map, key) { return map[key] || cssVar('--other'); }

// The biggest projects by cost among the rows on screen. Ranked from the
// filtered rows, not all time: otherwise a small project you filtered to on
// purpose is folded into "Other" because it isn't a top project overall.
function topProjectsOf(rows, n = 7) {
  const cost = {};
  rows.forEach(r => { const p = projectOf(r[1]); cost[p] = (cost[p] || 0) + r[13]; });
  return Object.keys(cost).sort((a, b) => cost[b] - cost[a]).slice(0, n);
}

let projectMS = null, modelMS = null;

function populateFilters() {
  // Projects are absolute paths; show the short tail and keep the full path as
  // the tooltip, so the control stays readable without losing information.
  const projectOpts = PAYLOAD.dims.projects.map(p => ({
    value: p, label: shortProjectLabel(p), title: p,
  }));
  const modelOpts = PAYLOAD.dims.models.map(m => ({ value: m, label: m, title: m }));

  if (!projectMS) {
    projectMS = new MultiSelect(document.getElementById('projectFilterMount'), {
      label: 'Project', allLabel: 'All projects', noun: 'project', options: projectOpts,
      onChange: values => { state.projects = values; onFiltersChanged(); },
    });
    modelMS = new MultiSelect(document.getElementById('modelFilterMount'), {
      label: 'Model', allLabel: 'All models', noun: 'model', options: modelOpts,
      onChange: values => { state.models = values; onFiltersChanged(); },
    });
  } else {
    // A Refresh can change the available dimensions; keep valid selections.
    projectMS.setOptions(projectOpts);
    modelMS.setOptions(modelOpts);
    state.projects = projectMS.getValues();
    state.models = modelMS.getValues();
  }

  // Column 0 is a 5-minute bucket, so take the day part -- the range inputs and
  // presets all work in whole local days.
  const dates = PAYLOAD.usage.map(r => dayOf(r[0])).filter(Boolean).sort();
  const minDate = dates[0] || PAYLOAD.summary.date_min || '2020-01-01';
  const maxDate = dates[dates.length-1] || PAYLOAD.summary.date_max || minDate;
  const prevMin = state._minDate, prevMax = state._maxDate;
  state._minDate = minDate; state._maxDate = maxDate;
  let firstLoad = false;
  if (!state.from) {
    // Default view is the last 5 days: at the default 8-hour bucket that's a
    // readable 15 bars, and it's the window most usage questions are about.
    state.from = addDays(maxDate, -(DEFAULT_RANGE_DAYS - 1));
    if (state.from < minDate) state.from = minDate;
    state.to = maxDate;
    applyUrlState(minDate, maxDate);
    firstLoad = true;
  } else {
    // A range that ran up to the latest data keeps doing so after a Refresh
    // brings in a new day; otherwise the newest work would fall just outside
    // it. A quick range ("last 7 days") slides along with it, and "All" also
    // keeps its start pinned to the earliest data.
    const preset = document.querySelector('#presetRow button.active');
    const presetDays = preset && preset.dataset.preset !== 'all' ? parseInt(preset.dataset.preset, 10) : null;
    if (state.to === prevMax) {
      state.to = maxDate;
      if (presetDays) state.from = addDays(maxDate, -(presetDays - 1));
    }
    if (state.from === prevMin) state.from = minDate;
    // Clamp what is left into the new bounds rather than resetting it.
    state.from = state.from > minDate ? state.from : minDate;
    state.to = state.to < maxDate ? state.to : maxDate;
    if (state.from > state.to) state.from = state.to;
  }

  const fromEl = document.getElementById('dateFrom'), toEl = document.getElementById('dateTo');
  fromEl.min = minDate; fromEl.max = maxDate; toEl.min = minDate; toEl.max = maxDate;
  fromEl.value = state.from; toEl.value = state.to;
  syncPresetAvailability();
  ensureGrainFits();
  renderGrainControl();
  renderFilterSummary();
  // What was asked for in the address may have been corrected (a bad date, an
  // unknown project, a grain too fine for the range): write back what is
  // actually shown, so the address never claims a view the page isn't in.
  if (firstLoad) writeUrlState();
}

const DEFAULT_RANGE_DAYS = 5;

// The view a fresh page opens on: the last DEFAULT_RANGE_DAYS days of data. It is
// the "no filter" state, so the date chip and the URL leave it out.
function defaultRange() {
  const from = addDays(state._maxDate, -(DEFAULT_RANGE_DAYS - 1));
  return { from: from < state._minDate ? state._minDate : from, to: state._maxDate };
}

// The filters live in the address (?from=&to=&grain=&project=&model=), so a view
// can be bookmarked or shared and survives a reload. Read once, at the first
// load, and validated against what this data holds; anything else is ignored.
function applyUrlState(minDate, maxDate) {
  let q;
  try { q = new URLSearchParams(window.location.search || ''); } catch (e) { return; }
  const day = v => /^\d{4}-\d{2}-\d{2}$/.test(v || '') && v >= minDate && v <= maxDate ? v : null;
  const from = day(q.get('from')), to = day(q.get('to'));
  if (from && to && from <= to) {
    state.from = from; state.to = to;
    setPresetActive(from === minDate && to === maxDate ? 'all' : null);
  }
  const g = q.get('grain');
  const grain = g === MONTH ? MONTH : parseInt(g, 10);
  if (GRAINS.some(x => x.key === grain)) state.grain = grain;
  const known = (all, picked) => picked.filter(v => all.includes(v));
  const projects = known(PAYLOAD.dims.projects, q.getAll('project'));
  const models = known(PAYLOAD.dims.models, q.getAll('model'));
  if (projects.length) { projectMS.setValues(projects); state.projects = projectMS.getValues(); }
  if (models.length) { modelMS.setValues(models); state.models = modelMS.getValues(); }
}
function writeUrlState() {
  try {
    const q = new URLSearchParams();
    const d = defaultRange();
    if (state.from !== d.from || state.to !== d.to) { q.set('from', state.from); q.set('to', state.to); }
    if (state.grain !== 480) q.set('grain', state.grain);
    state.projects.forEach(p => q.append('project', p));
    state.models.forEach(m => q.append('model', m));
    const qs = q.toString();
    window.history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : '') + (window.location.hash || ''));
  } catch (e) { /* a file:// page or an old browser: the filters just aren't in the address */ }
}
function addDays(day, n) {
  const d = new Date(day + 'T00:00:00');
  // A non-date input would make toISOString() throw and take the whole render
  // down; returning the input unchanged degrades to "no shift" instead.
  if (isNaN(d.getTime())) return day;
  d.setDate(d.getDate() + n);
  return localDate(d.toISOString());
}

function renderGrainControl() {
  const row = document.getElementById('grainRow');
  if (!row) return;
  row.innerHTML = GRAINS.map(g => {
    const n = bucketCountFor(g.key);
    const tooMany = n > MAX_BUCKETS;
    return `<button type="button" data-grain="${esc(g.key)}"` +
      `${g.key === state.grain ? ' class="active"' : ''}${tooMany ? ' disabled' : ''}` +
      ` title="${tooMany ? `${fmtInt(n)} buckets over this range — too many to chart` : `${fmtInt(n)} bucket${n === 1 ? '' : 's'}`}"` +
      `>${esc(g.label)}</button>`;
  }).join('');
  const g = grainInfo(state.grain);
  document.getElementById('grainSummary').innerHTML =
    `one bar per <strong>${esc(g.noun)}</strong> &middot; ${fmtInt(bucketCountFor(state.grain))} bar${bucketCountFor(state.grain) === 1 ? '' : 's'}`;
}

// If the range changes such that the active grain is now too fine, step to the
// next coarser grain that fits rather than rendering thousands of bars.
function ensureGrainFits() {
  if (bucketCountFor(state.grain) <= MAX_BUCKETS) return false;
  const idx = GRAINS.findIndex(g => g.key === state.grain);
  for (let i = idx + 1; i < GRAINS.length; i++) {
    if (bucketCountFor(GRAINS[i].key) <= MAX_BUCKETS) { state.grain = GRAINS[i].key; return true; }
  }
  state.grain = MONTH;
  return true;
}

function daysBetween(a, b) {
  return Math.round((new Date(b + 'T00:00:00') - new Date(a + 'T00:00:00')) / 86400000) + 1;
}
function prettyDate(d) {
  return new Date(d + 'T00:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

// Greys out a quick range that reaches further back than the data goes, so the
// buttons never promise a window that doesn't exist.
function syncPresetAvailability() {
  const span = daysBetween(state._minDate, state._maxDate);
  document.querySelectorAll('#presetRow button').forEach(b => {
    const p = b.dataset.preset;
    b.disabled = p !== 'all' && parseInt(p) > span;
    // Ranges end on the last day that has data, not on today.
    b.title = b.disabled ? `Only ${span} days of data available`
      : p === 'all' ? 'Every day with data'
      : `The last ${p} day${p === '1' ? '' : 's'} of data, ending ${state._maxDate}`;
  });
}

function renderFilterSummary() {
  const n = daysBetween(state.from, state.to);
  // Same year on both ends? Print it once, so the summary fits its cell.
  const sameYear = state.from.slice(0, 4) === state.to.slice(0, 4);
  const fromTxt = sameYear ? shortDay(state.from) : prettyDate(state.from);
  document.getElementById('rangeSummary').innerHTML =
    `<strong>${esc(fromTxt)}</strong> – <strong>${esc(prettyDate(state.to))}</strong>` +
    ` &middot; ${n} day${n === 1 ? '' : 's'}`;

  // Active-filter chips: only the things that differ from the default view.
  const chips = [];
  // The default view (and "All") isn't a filter someone applied, so no chip.
  const isFullRange = state.from === state._minDate && state.to === state._maxDate;
  const dflt = defaultRange();
  const isDefaultRange = state.from === dflt.from && state.to === dflt.to;
  if (!isFullRange && !isDefaultRange) {
    chips.push({ kind: 'date', label: `${prettyDate(state.from)} – ${prettyDate(state.to)}`, title: 'Date range' });
  }
  state.projects.forEach(p => chips.push({ kind: 'project', value: p, label: shortProjectLabel(p), title: p }));
  state.models.forEach(m => chips.push({ kind: 'model', value: m, label: m, title: m }));
  if (state.showForks) chips.push({ kind: 'forks', label: 'Including fork copies', title: 'Fork/resume copies are shown in the Sessions table' });

  const row = document.getElementById('activeFilters');
  row.classList.toggle('shown', chips.length > 0);
  document.getElementById('activeChips').innerHTML = chips.map((c, i) =>
    `<span class="chip" title="${esc(c.title)}"><span class="chip-text">${esc(c.label)}</span>` +
    `<button class="chip-x" type="button" data-chip="${i}" aria-label="Remove filter ${esc(c.label)}">&times;</button></span>`
  ).join('');
  document.querySelectorAll('#activeChips .chip-x').forEach(btn => {
    btn.addEventListener('click', () => removeChip(chips[parseInt(btn.dataset.chip)]));
  });
}

function removeChip(chip) {
  if (!chip) return;
  if (chip.kind === 'date') {
    const d = defaultRange();
    setRange(d.from, d.to, String(DEFAULT_RANGE_DAYS));
  } else if (chip.kind === 'project') {
    projectMS.setValues(state.projects.filter(p => p !== chip.value));
    state.projects = projectMS.getValues();
    onFiltersChanged();
  } else if (chip.kind === 'model') {
    modelMS.setValues(state.models.filter(m => m !== chip.value));
    state.models = modelMS.getValues();
    onFiltersChanged();
  } else if (chip.kind === 'forks') {
    state.showForks = false;
    onFiltersChanged();
  }
}

function setRange(from, to, preset) {
  state.from = from; state.to = to;
  document.getElementById('dateFrom').value = from;
  document.getElementById('dateTo').value = to;
  setPresetActive(preset);
  onFiltersChanged();
}

function onFiltersChanged() {
  const coarsened = ensureGrainFits();
  writeUrlState();
  renderGrainControl();
  renderFilterSummary();
  if (coarsened) {
    const g = grainInfo(state.grain);
    document.getElementById('grainSummary').innerHTML +=
      ` <span class="badge warn">widened to ${esc(g.label)}</span>`;
  }
  markAllDirty();
}

function wireFilterControls() {
  const fromEl = document.getElementById('dateFrom'), toEl = document.getElementById('dateTo');
  // A date input's `min`/`max` are not enforced when the value is typed or
  // stepped with the arrow keys, so both ends are clamped here as well.
  const clamp = v => v < state._minDate ? state._minDate : v > state._maxDate ? state._maxDate : v;
  fromEl.addEventListener('change', e => {
    state.from = clamp(e.target.value || state._minDate);
    // Keep the pair ordered rather than silently rendering an empty range.
    if (state.from > state.to) state.to = state.from;
    fromEl.value = state.from; toEl.value = state.to;
    setPresetActive(null); onFiltersChanged();
  });
  toEl.addEventListener('change', e => {
    state.to = clamp(e.target.value || state._maxDate);
    if (state.to < state.from) state.from = state.to;
    fromEl.value = state.from; toEl.value = state.to;
    setPresetActive(null); onFiltersChanged();
  });
  document.getElementById('presetRow').addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn || btn.disabled) return;
    const preset = btn.dataset.preset;
    if (preset === 'all') { setRange(state._minDate, state._maxDate, 'all'); return; }
    const to = new Date(state._maxDate + 'T00:00:00');
    const from = new Date(to); from.setDate(from.getDate() - (parseInt(preset) - 1));
    const fromStr = localDate(from.toISOString());
    setRange(fromStr < state._minDate ? state._minDate : fromStr, state._maxDate, preset);
  });
  document.getElementById('resetFilters').addEventListener('click', () => {
    projectMS.clear(); modelMS.clear();
    state.projects = []; state.models = []; state.showForks = false;
    state.grain = 480;
    const d = defaultRange();
    setRange(d.from, d.to, String(DEFAULT_RANGE_DAYS));
  });
  document.getElementById('grainRow').addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn || btn.disabled) return;
    const raw = btn.dataset.grain;
    state.grain = raw === MONTH ? MONTH : parseInt(raw, 10);
    onFiltersChanged();
  });
  bindToggle('metricTokens','metricCost', () => state.metric='tokens', () => state.metric='cost', () => dirty.overview = true);
  document.getElementById('overviewTableToggle').addEventListener('click', () => toggleTable('overviewTableWrap','overviewTableToggle'));
  bindToggle('skillsMetricTokens','skillsMetricCost', () => state.skillsMetric='tokens', () => state.skillsMetric='cost', () => dirty.skills = true);
  bindToggle('agentsMetricTokens','agentsMetricCost', () => state.agentsMetric='tokens', () => state.agentsMetric='cost', () => dirty.agents = true);
}
function setPresetActive(preset) {
  document.querySelectorAll('#presetRow button').forEach(b => b.classList.toggle('active', b.dataset.preset === String(preset)));
}
function bindToggle(idA, idB, onA, onB, after) {
  document.getElementById(idA).addEventListener('click', () => {
    document.getElementById(idA).classList.add('active'); document.getElementById(idB).classList.remove('active');
    onA(); after(); renderActiveTab();
  });
  document.getElementById(idB).addEventListener('click', () => {
    document.getElementById(idB).classList.add('active'); document.getElementById(idA).classList.remove('active');
    onB(); after(); renderActiveTab();
  });
}
function toggleTable(wrapId, btnId) {
  const wrap = document.getElementById(wrapId);
  const show = wrap.style.display === 'none';
  wrap.style.display = show ? 'block' : 'none';
  document.getElementById(btnId).textContent = show ? 'Hide table' : 'Show table';
}

const VALID_TABS = new Set(['overview','habits','skills','tools','agents','sessions','env']);

function showTab(tab) {
  if (!VALID_TABS.has(tab)) tab = 'overview';
  currentTab = tab;
  document.querySelectorAll('#tabstrip button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tabpanel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + tab));
  renderActiveTab();
}

function wireTabs() {
  document.getElementById('tabstrip').addEventListener('click', e => {
    if (e.target.tagName !== 'BUTTON') return;
    const tab = e.target.dataset.tab;
    if (window.location.hash === '#' + tab) showTab(tab);
    else window.location.hash = tab; // triggers hashchange -> showTab
  });
  window.addEventListener('hashchange', () => showTab(window.location.hash.slice(1)));
  showTab(window.location.hash.slice(1));
}

// Subagent share of one token column (6 = output, 10 = thinking) in usage rows.
function tokenShare(rows, col) {
  let all = 0, sub = 0;
  rows.forEach(r => { all += r[col]; if (r[4] !== -1) sub += r[col]; });
  return all > 0 ? sub / all : null;
}

function markAllDirty() {
  Object.keys(dirty).forEach(k => dirty[k] = true);
  renderGlobalStats();
  renderActiveTab();
}
const RENDERERS = {
  overview: renderOverview, habits: renderHabits, skills: renderSkills, tools: renderTools,
  agents: renderAgents, sessions: renderSessions, env: renderEnv,
};
function renderActiveTab() {
  if (!dirty[currentTab]) return;
  RENDERERS[currentTab]();
  dirty[currentTab] = false;
}

// ===================== Global stat tiles =====================
function renderGlobalStats() {
  const rows = usageRows();
  const totalCost = rows.reduce((s,r) => s+r[13], 0);
  const subCost = rows.filter(r => r[4] !== -1).reduce((s,r) => s+r[13], 0);
  // Subagents mostly run on cheaper models, so their share of the *work* (output
  // and thinking tokens) is far higher than their share of cost -- on a real
  // log set 59% of output and 70% of thinking against 36% of cost. On a seat
  // plan, tokens are what draws down the allowance, so show both.
  const outShare = tokenShare(rows, 6), thinkShare = tokenShare(rows, 10);
  const totalTokens = rows.reduce((s,r) => s+tokenTotal(r), 0);
  const totalRequests = rows.reduce((s,r) => s+r[12], 0);
  const cacheRead = rows.reduce((s,r) => s+r[7], 0);
  const cacheWrite = rows.reduce((s,r) => s+r[8]+r[9], 0);
  const inputTok = rows.reduce((s,r) => s+r[5], 0);
  const cacheHit = (inputTok+cacheRead+cacheWrite) > 0 ? cacheRead/(inputTok+cacheRead+cacheWrite) : null;
  // A transcript that never got a reply isn't a session (aggregate.py counts
  // the header's sessions the same way), though the Sessions tab lists it.
  const sessions = countedSessions().length;
  const thinking = rows.reduce((s,r) => s+r[10], 0);
  // Responses that logged no thinking count: their thinking is unknown, not 0.
  const thinkingUnlogged = rows.reduce((s,r) => s + (r[17] || 0), 0);
  // Counted from tool calls, not usage.server_tool_use. That field tracks the
  // Messages API's *server-side* web search tool, where Anthropic runs the
  // query. Claude Code's WebSearch/WebFetch are client-side tools -- it fetches
  // itself and returns a tool_result -- so the server-side counter is
  // structurally always zero here, and the tile read 0 while 832 lookups had
  // happened.
  // Per-session cost built from the *filtered* rows, so these stay consistent
  // with Total cost above when a date or project filter is on. Using whole-
  // session totals would count work from outside the selected range.
  const sessionCosts = Object.values(costBySession(rows)).sort((a,b) => b - a);
  const medianCost = median([...sessionCosts].sort((a,b) => a - b));
  const topN = Math.min(5, sessionCosts.length);
  const topShare = totalCost > 0
    ? sessionCosts.slice(0, topN).reduce((s,v) => s+v, 0) / totalCost : null;

  // Net of the premium paid to write the cache, so it can't overstate itself.
  const cacheSaved = rows.reduce((s,r) => s + (r[15] || 0), 0);
  const uncached = totalCost + cacheSaved;

  let webSearch = 0, webFetch = 0;
  toolRows().forEach(r => {
    if (r[2] === 'WebSearch') webSearch += r[3];
    else if (r[2] === 'WebFetch') webFetch += r[3];
  });

  // Output imputed for subagent responses logged with a placeholder count:
  // already inside the totals, shown so it is never mistaken for logged.
  const imputed = rows.reduce((s,r) => s + (r[16] || 0), 0);
  // What Claude Code's own counter recorded and no transcript did. Kept out of
  // Total cost (every chart adds up to that) and shown beside it. A session
  // without a counter is left out, not counted as zero. A counter covers its
  // session's whole life and can't be split by day, so only sessions wholly
  // inside the selected dates count (the reconciliation panel's rule): a
  // month-long session's shortfall doesn't land on the last five days.
  // Fork copies are included whether or not the Sessions table shows them: the
  // counter sits on whichever transcript owns it, and the toggle must not move
  // this figure.
  const withCounter = PAYLOAD.sessions.filter(s => s.est_cost_unlogged != null && sessionInFilters(s));
  // The span is the whole counter's (every transcript on it, counterSpan), as in
  // the reconciliation table, not the owner transcript's own days.
  const counted = withCounter.filter(s => {
    const span = counterSpan(s);
    return span.from && span.from >= state.from && span.to <= state.to;
  });
  const unlogged = counted.reduce((s,x) => s + x.est_cost_unlogged, 0);
  const straddling = withCounter.length - counted.length;

  const tiles = [
    // Transcripts don't record every model call Claude Code makes (see
    // docs/limitations.md), so the transcript total is a floor. Where a
    // session kept a counter, the calls it saw and the transcripts didn't are
    // priced and shown as the caption.
    ['Total cost', fmtCost(totalCost),
     counted.length
       ? `+ ${fmtCost(unlogged)} not in transcripts (${fmtInt(counted.length)} session${counted.length === 1 ? '' : 's'} with a counter` +
         (straddling ? `; ${fmtInt(straddling)} more run past these dates)` : ')')
       : 'from transcripts · may read low',
     UNLOGGED_CALLS_NOTE],
    // Cache reads are nearly all of it (the whole conversation is re-read each
    // turn), so the caption gives the rest: closer to the work done.
    ['Total tokens', fmtTokens(totalTokens),
     [`${fmtTokens(totalTokens - cacheRead)} excl. cache reads`,
      imputed > 0 ? `incl. ${fmtTokens(imputed)} imputed` : null].filter(Boolean).join(' · '),
     UNLOGGED_CALLS_NOTE],
    ['Sessions', fmtInt(sessions)],
    ['Requests', fmtInt(totalRequests)],
    // The output share includes imputed output; the thinking share is logged
    // thinking only (see THINKING_NOTE), so it says so.
    ['Subagent cost share', fmtPct(totalCost > 0 ? subCost/totalCost : null),
     outShare == null ? null : `${fmtPct(outShare)} of output · ${fmtPct(thinkShare)} of logged thinking`],
    ['Median session', medianCost == null ? '—' : fmtCost(medianCost),
     sessionCosts.length ? `mean ${fmtCost(totalCost/sessionCosts.length)}` : null],
    // With five sessions or fewer the top five are all of them: 100% says
    // nothing, so the tile waits for more.
    ['Cost concentration', sessionCosts.length > 5 ? fmtPct(topShare) : '—',
     // "with spend" because a session that recorded no priced usage is not in
     // this denominator, and the Sessions tile beside it counts differently.
     sessionCosts.length > 5 ? `top ${topN} of ${fmtInt(sessionCosts.length)} sessions with spend`
       : `needs more than 5 sessions with spend (${fmtInt(sessionCosts.length)} here)`],
    ['Cache hit rate', fmtPct(cacheHit), 'share of prompt tokens read from cache', CACHE_HIT_NOTE],
    ['Cache saved', fmtCost(cacheSaved),
     uncached > 0 ? `${Math.round(100*cacheSaved/uncached)}% below ${fmtCost0(uncached)} uncached, at list price` : null,
     CACHE_SAVED_NOTE],
    ['Thinking tokens', fmtTokens(thinking),
     thinkingUnlogged > 0 ? `at least · ${fmtInt(thinkingUnlogged)} responses didn't log it` : 'as logged',
     THINKING_NOTE],
    ['Web lookups', fmtInt(webSearch + webFetch),
     `${fmtInt(webSearch)} search · ${fmtInt(webFetch)} fetch`],
  ];
  renderTiles('statTiles', tiles);
}

// tiles: [label, value, sub?] — `sub` renders as a smaller caption so a
// qualifier never has to be crammed into the headline number.
// How many tiles per row. Prefers the densest layout that leaves at most one
// empty cell on the last row, so the grid is tight without stranding a lone
// tile. 11 -> 6 (6+5), 9 -> 5 (5+4), 8 -> 4 (4+4), 4 -> 4, 3 -> 3.
// Capped at 6 because the labels stop being readable much below that width.
function tileColumns(n) {
  const candidates = [6, 5, 4, 3];
  const gap = c => (c - (n % c)) % c;
  const tidy = candidates.filter(c => c <= n && gap(c) <= 1);
  if (tidy.length) return Math.max(...tidy);
  return candidates.reduce((best, c) => gap(c) < gap(best) ? c : best, 4);
}

const UNLOGGED_CALLS_NOTE =
  'Counted from the session transcripts. From Claude Code 2.1.284 until 2.1.307 fixed it, most ' +
  'subagent responses were logged with a placeholder output count; their output is imputed from how ' +
  'much each response wrote (within about 5% in aggregate for most models, about 8% low for Sonnet ' +
  '5.5) and included here. Claude Code also makes model calls that are never written to the ' +
  'transcripts. Where a session kept Claude Code\'s own cost counter, what it recorded beyond the ' +
  'transcripts is priced at list rates and shown as "not in transcripts", for sessions that lie ' +
  'wholly inside the selected dates (a counter can\'t be split by day); for a session without one ' +
  '(most desktop sessions) that part is unknown, so the total is a floor. See docs/limitations.md.';
const CACHE_HIT_NOTE =
  'Share of prompt tokens read from the cache: cache reads / (input + cache reads + cache writes). ' +
  'Claude Code re-sends the whole conversation each turn, so this sits near 97–98% for almost ' +
  'everyone and barely moves. The Habits strip\'s cold-cache card shows the misses that cost money.';
const CACHE_SAVED_NOTE =
  'What the cache reads would have cost as plain input, less the extra paid to write the cache, at ' +
  'API list prices. "Uncached" is that same stream of tokens priced with no cache: a list-price ' +
  'comparison, not money saved on a seat plan, and not what a run without caching would have cost.';
const THINKING_NOTE =
  'Claude Code logs a thinking-token count on each response (inside the output count). Responses ' +
  'that don\'t log one (placeholder subagent lines, older builds) count as zero here, so this is a ' +
  'floor.';

function renderTiles(mountId, tiles) {
  const el = document.getElementById(mountId);
  el.style.setProperty('--tile-cols', tileColumns(tiles.length));
  // An optional fourth element is a tooltip, for a caveat too long for the tile.
  el.innerHTML = tiles.map(([l, v, sub, tip]) =>
    `<div class="stat-tile"${tip ? ` title="${esc(tip)}"` : ''}><div class="label">${esc(l)}</div>` +
    `<div class="value">${v}</div>` +
    (sub ? `<div class="sub">${esc(sub)}</div>` : '') +
    `</div>`).join('');
}

// Whole dollars: cents are noise on a five-figure secondary figure.
function fmtCost0(n) {
  return Number.isFinite(asNum(n)) ? '$' + fmtInt(n) : '—';
}

// `empty` ({title, body}) replaces the generic "Nothing in this range" when the
// chart has no marks for a reason the filters didn't cause (calls but no errors).
function upsertChart(key, canvasId, cfg, empty) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  // A chart with no data points renders as an empty grid, which reads as a bug.
  // Swap in a message instead, and swap it back out when data returns.
  const datasets = (cfg.data && cfg.data.datasets) || [];
  const hasData = datasets.some(d => (d.data || []).some(v => {
    const n = (v && typeof v === 'object') ? v.y : v;
    return typeof n === 'number' && n !== 0;
  }));
  setChartEmpty(canvas, !hasData, empty);
  if (!hasData) return;
  if (charts[key]) { charts[key].data = cfg.data; charts[key].options = cfg.options; charts[key].update(); }
  else { charts[key] = new Chart(canvas, cfg); }
}

function setChartEmpty(canvas, isEmpty, msg) {
  const box = canvas.parentElement;
  let note = box.querySelector(':scope > .empty-state');
  canvas.style.visibility = isEmpty ? 'hidden' : '';
  if (isEmpty) {
    if (!note) {
      note = document.createElement('div');
      note.className = 'empty-state';
      box.appendChild(note);
    }
    note.innerHTML = `<div class="es-title">${esc(msg ? msg.title : 'Nothing in this range')}</div>` +
      `<div class="es-body">${esc(msg ? msg.body : 'No data matches the current filters.')}</div>`;
  } else if (note) {
    note.remove();
  }
}

// For tables: render a single full-width row rather than an empty tbody.
function emptyRow(colspan, message) {
  return `<tr><td colspan="${colspan}"><div class="empty-state">` +
    `<div class="es-title">No rows</div><div class="es-body">${esc(message)}</div>` +
    `</div></td></tr>`;
}

// ===================== Overview =====================
function renderOverview() {
  const rows = usageRows();
  renderHabitStrip();
  renderTimeChart(rows);
  renderLimits();
  renderProjectChart(rows);
  renderModelChart(rows);
  renderTokenTypeChart(rows);
  renderReconciliation();
  renderOverviewTable(rows);
}

// ===================== Limits & friction =====================
// Friction rows: [bucket, session_id, "retry"|"turn"|"compact", value].
function frictionRows() {
  return (PAYLOAD.friction || []).filter(r => inDateRange(r[0]) && sessionOk(r[1]));
}
// Limit episodes, by the local day the limit was hit.
function limitRows() {
  return (PAYLOAD.limits || []).filter(e => inDateRange(limitHitDay(e)) && sessionOk(e.session_id));
}
const LIMIT_LABELS = { weekly: 'Weekly', five_hour: '5-hour', spend_cap: 'Spend cap', opus: 'Opus', other: 'Other' };
function fmtWhen(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}
function fmtHours(h) {
  if (h == null) return '—';
  return h >= 1 ? h.toFixed(1) + 'h' : Math.round(h * 60) + 'm';
}

function renderLimits() {
  renderAllowance();
  const eps = limitRows();
  const fr = frictionRows();
  let retries = 0, retryMs = 0, compactions = 0, longTurns = 0;
  const turns = [];
  fr.forEach(([, , kind, v]) => {
    if (kind === 'retry') { retries++; retryMs += v; }
    else if (kind === 'compact') compactions++;
    else if (kind === 'turn') { turns.push(v); if (v >= 600000) longTurns++; }
  });
  const lockouts = eps.filter(e => e.locked_out);
  const lockedHours = lockouts.reduce((s, e) => s + e.blocked_hours, 0);
  const extra = eps.filter(e => e.kind === 'weekly').reduce((s, e) => s + (e.usage_after_hit_usd || 0), 0);
  // Automatic and manual compactions come from the health events (they carry
  // the trigger); the friction rows count the same records without it.
  let compactAuto = 0, compactManual = 0;
  ((PAYLOAD.health && PAYLOAD.health.events) || []).forEach(([bucket, sid, kind, value, n]) => {
    if (kind !== 'compact' || !inDateRange(bucket) || !sessionOk(sid)) return;
    if (value === 'manual') compactManual += n; else compactAuto += n;
  });
  const compactSplit = [`${fmtInt(compactAuto)} automatic`, `${fmtInt(compactManual)} manual`];
  if (compactions > compactAuto + compactManual) compactSplit.push(`${fmtInt(compactions - compactAuto - compactManual)} unclassified`);
  const compactSub = compactions && !(PAYLOAD.health && PAYLOAD.health.events)
    ? 'automatic or manual not recorded' : compactSplit.join(' · ');
  // The median is read from turn_duration records, which Claude Code has stopped
  // writing: too few of them, or none for the latest stretch of the range, and it
  // says so instead of standing in for all turns.
  const MIN_TURNS = 10;
  const medTurn = turns.length >= MIN_TURNS ? median([...turns].sort((a, b) => a - b)) : null;
  const turnDays = fr.filter(r => r[2] === 'turn').map(r => dayOf(r[0])).sort();
  const lastTurnDay = turnDays.length ? turnDays[turnDays.length - 1] : null;
  const rangeEnd = (PAYLOAD.summary && PAYLOAD.summary.date_max && PAYLOAD.summary.date_max < state.to)
    ? PAYLOAD.summary.date_max : state.to;
  const staleDays = lastTurnDay && rangeEnd ? (new Date(rangeEnd) - new Date(lastTurnDay)) / 86400000 : 0;
  const turnSub = !turns.length ? 'no turn durations recorded'
    : medTurn == null ? `only ${fmtInt(turns.length)} turn${turns.length === 1 ? '' : 's'} recorded`
    : `from ${fmtInt(turns.length)} turns · ${fmtInt(longTurns)} over 10 min` +
      (staleDays > 7 ? ` · none after ${new Date(lastTurnDay + 'T12:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}` : '');
  renderTiles('limitTiles', [
    ['Limit hits', fmtInt(eps.length), eps.length ? eps.map(e => LIMIT_LABELS[e.kind] || e.kind).join(', ') : 'none in range'],
    ['Locked out', fmtHours(lockouts.length ? lockedHours : 0),
     lockouts.length ? `${lockouts.length} lock-out${lockouts.length > 1 ? 's' : ''} until reset` : 'never locked out'],
    ['Extra usage (est.)', fmtCost(extra), 'after the weekly limit, list price'],
    ['API retries', fmtInt(retries), retries ? `${fmtDuration(retryMs)} waiting` : null],
    ['Context compactions', fmtInt(compactions), compactions ? compactSub : 'none in range'],
    ['Median turn', medTurn == null ? '—' : fmtDuration(medTurn), turnSub],
  ]);

  const wrap = document.getElementById('limitsTableWrap');
  if (!eps.length) { wrap.innerHTML = ''; return; }
  wrap.innerHTML = `<div class="table-scroll"><table class="data-table">
    <thead><tr><th>Limit</th><th>Hit</th><th>Resets</th><th class="num">Notices</th><th>Locked out</th>
      <th class="num">Blocked</th><th class="num">Used before hit</th><th class="num">After hit</th><th>Project</th></tr></thead>
    <tbody>${eps.map(e => `<tr>
      <td>${esc(LIMIT_LABELS[e.kind] || e.kind)}</td><td>${esc(fmtWhen(e.hit_at))}</td><td>${esc(fmtWhen(e.resets_at))}</td>
      <td class="num">${fmtInt(e.notices)}</td>
      <td>${e.locked_out ? '<span class="badge warn">yes</span>' : 'no'}</td>
      <td class="num">${esc(fmtHours(e.blocked_hours))}</td>
      <td class="num">${e.usage_before_hit_usd == null ? '—' : fmtCost(e.usage_before_hit_usd)}</td>
      <td class="num">${e.usage_after_hit_usd == null ? '—' : fmtCost(e.usage_after_hit_usd)}</td>
      <td>${esc(shortProjectLabel(projectOf(e.session_id)))}</td></tr>`).join('')}</tbody></table></div>`;
}

// Where this limit week stands against the level at which the weekly limit ran
// out last time. It is a reading of the current limit week, so it shows only
// while the selected dates overlap that week (compared as local days);
// otherwise a note says the week is outside them.
function renderAllowance() {
  const el = document.getElementById('allowanceWrap');
  const a = PAYLOAD.allowance;
  if (!a) {
    el.innerHTML = `<p class="foot-note" style="margin-top:0">No weekly limit has been hit in these logs, so there is
      nothing yet to estimate the allowance from.</p>`;
    return;
  }
  const weekFrom = localDate(a.week_start), weekTo = localDate(a.week_end);
  if (weekFrom && weekTo && state.from && state.to && (weekTo < state.from || weekFrom > state.to)) {
    el.innerHTML = `<p class="foot-note" style="margin-top:0">The current limit week
      (${esc(prettyDate(weekFrom))} &ndash; ${esc(prettyDate(weekTo))}) is outside the selected dates.</p>`;
    return;
  }
  // The allowance is a rough estimate (one hit is one data point, and list-price
  // dollars aren't a stable measure of it), so it is rounded, shown as a range
  // where a completed week without a limit notice used more, and never as
  // "on track". Older payloads have none of the range fields.
  const roughUsd = n => n >= 1000 ? '$' + (n / 1000).toFixed(1) + 'k'
    : n >= 100 ? '$' + (Math.round(n / 10) * 10).toLocaleString('en-US') : '$' + Math.round(n);
  const roughPct = p => p < 0.01 ? 'under 1%' : Math.round(p * 100) + '%';
  const low = a.allowance_est_usd, high = a.allowance_high_usd;
  const ranged = !!(a.contradicted && high);
  const estText = ranged ? `${roughUsd(low)}–${roughUsd(high)}` : roughUsd(low);
  const shareText = ranged && a.used_pct_of_high != null
    ? `${roughPct(a.used_pct_of_high)}–${roughPct(a.used_pct || 0)}` : roughPct(a.used_pct || 0);
  const pct = Math.min(a.used_pct || 0, 1);
  const status = a.status === 'over' ? ['bad', 'Past the usage level at which the weekly limit was hit before']
    : a.status === 'on_pace_to_hit' ? ['warn', `At this pace the usage level of the last limit hit would be reached around ${fmtWhen(a.projected_hit_at)}`]
    : ['ok', `About ${shareText} of the estimate used so far`];
  const hits = a.weekly_hits || 1;
  const shortDay = iso => new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  el.innerHTML = `<div class="allowance">
    <div class="allowance-head"><span><strong>This limit week</strong> · ${esc(fmtWhen(a.week_start))} – ${esc(fmtWhen(a.week_end))}</span>
      <span>about ${esc(roughUsd(a.used_usd))} used of a rough ${ranged ? 'range of ' : 'estimate of ~'}${esc(estText)}</span></div>
    <div class="meter ${status[0]}"><div style="width:${(pct * 100).toFixed(1)}%"></div></div>
    <div class="allowance-foot ${status[0]}">${esc(status[1])}. This is a rough estimate, from ${hits} weekly-limit hit${hits === 1 ? '' : 's'}
      (${esc(fmtWhen(a.calibrated_from))}): the list-price usage Claude Code had logged when the limit ran out.
      ${ranged ? `A completed week without a limit notice (from ${esc(shortDay(a.high_week_start))}) used about ${esc(roughUsd(high))},
      so the estimate is contradicted and the range runs up to that: list-price dollars are not a steady measure of the allowance
      (the model mix changes what a request counts for). ` : ''}Week boundaries keep the reset's clock time across daylight-saving changes,
      taken from this machine's time zone.</div></div>`;
}

// The CLI's model names are normalized the way claude_usage/pricing.py's
// normalize_model() does it, unlike the raw names the model filter offers --
// so compare the two in that form. Same rules, same order: no "[1m]", no
// Bedrock ARN/region/"anthropic."/"-v1:0", no Vertex "@date", no date suffix,
// no "-latest", "-4-0" read as "-4", and Claude 3.x's
// "claude-3-5-haiku" read as "claude-haiku-3-5".
// tests/test_pricing.py runs one set of cases through both.
function normalizeModel(m) {
  return String(m || '')
    .replace(/\[[^\]]*\]$/, '')
    .replace(/^arn:[^/]*\//, '')
    .replace(/^(?:[a-z-]+\.)?anthropic\./, '')
    .replace(/-v\d+(?::\d+)?$/, '')
    .replace(/@\d{8}$/, '')
    .replace(/-\d{8}$/, '')
    .replace(/-latest$/, '')
    .replace(/^(claude-(?:opus|sonnet))-4-0$/, '$1-4')
    .replace(/^claude-(\d+(?:-\d)?)-(opus|sonnet|haiku)$/, 'claude-$2-$1');
}

// The per-model comparison for the sessions the filters select, summed from each
// session's own rows -- the payload's ready-made table covers every session.
// The CLI counter is whole-session and cannot be split by day, so a session is
// in or out as a whole: only sessions that started and ended inside the
// selected dates count. One that began before the range or ran past its end is
// left out, rather than its whole total being credited to the range.
// Fork copies count even when hidden, because the counter's figures sit on
// whichever transcript owns it, and that can be a copy.
// The days a CLI counter covers: from the earliest start to the latest end of
// every transcript on it, not just the owner's. A resumed or forked transcript
// carries on its parent's counter, so a member that runs past the range means
// the counter does too, and its figures can't be credited to the range.
function counterSpan(s) {
  const members = [s, ...(s.cli_counter_shared_with || []).map(id => sessionById[id]).filter(Boolean)];
  let from = null, to = null;
  members.forEach(m => {
    const a = sessionRawStartDay(m), b = sessionEndDay(m) || a;
    if (a && (!from || a < from)) from = a;
    if (b && (!to || b > to)) to = b;
  });
  return { from, to };
}

function reconciliationRows() {
  const selected = new Set(state.models.map(normalizeModel));
  const byModel = {};
  PAYLOAD.sessions.filter(s => {
    const span = counterSpan(s);
    return span.from && span.from >= state.from && span.to <= state.to && projectOk(s.project) && modelsOk(s.models);
  }).forEach(s => {
    (s.cli_models || []).filter(m => !selected.size || selected.has(normalizeModel(m.model))).forEach(m => {
      const acc = byModel[m.model] = byModel[m.model] ||
        { model: m.model, est_cost: 0, est_tokens: 0, cli_cost: 0, cli_tokens: 0, cli_repriced: 0, sessions: 0 };
      acc.est_cost += m.est_cost; acc.est_tokens += m.est_tokens;
      acc.cli_cost += m.cli_cost; acc.cli_tokens += m.cli_tokens;
      acc.cli_repriced += m.cli_repriced || 0;
      acc.sessions += 1 + (s.cli_counter_shared_with || []).length;
    });
  });
  return Object.values(byModel).sort((a, b) => b.cli_cost - a.cli_cost);
}

// The CLI records its own per-model token and cost accounting in `cost-state`
// entries. That is the only first-party number available, so it is shown as a
// direct per-model comparison instead of a single opaque divergence badge.
function renderReconciliation() {
  const el = document.getElementById('reconcileWrap');
  if (!el) return;
  const rec = reconciliationRows();
  if (!rec.length) {
    el.innerHTML = `<p class="foot-note">No session wholly inside these dates recorded a <code>cost-state</code> entry with
      per-model usage, so there is nothing to reconcile against.</p>`;
    return;
  }
  // Δ as a share of the second figure; blank when there is nothing to divide by.
  const delta = (a, b) => {
    const gap = b > 0 ? (a - b) / b : null;
    const cls = gap == null ? '' : Math.abs(gap) <= 0.05 ? 'ok' : Math.abs(gap) <= 0.25 ? 'warn' : 'bad';
    return `<td class="num ${cls}">${gap == null ? '—' : (gap > 0 ? '+' : '') + (gap * 100).toFixed(1) + '%'}</td>`;
  };
  const body = rec.map(r => `<tr><td>${esc(r.model)}</td>
      <td class="num">${fmtTokens(r.est_tokens)}</td><td class="num">${fmtTokens(r.cli_tokens)}</td>${delta(r.est_tokens, r.cli_tokens)}
      <td class="num">${fmtCost(r.est_cost)}</td><td class="num">${fmtCost(r.cli_cost)}</td>${delta(r.est_cost, r.cli_cost)}
      <td class="num">${fmtCost(r.cli_repriced)}</td>${delta(r.cli_repriced, r.cli_cost)}
      <td class="num">${esc(r.sessions)}</td></tr>`).join('');
  el.innerHTML = `<div class="table-scroll"><table class="data-table"><thead><tr>
      <th rowspan="2">Model</th><th class="num" colspan="3">Tokens</th><th class="num" colspan="3">Cost</th>
      <th class="num" colspan="2">Rate check</th><th class="num" rowspan="2">Sessions</th></tr><tr>
      <th class="num">Logged here</th><th class="num">Claude Code counted</th><th class="num">Δ</th>
      <th class="num">Logged here</th><th class="num">Claude Code's $</th><th class="num">Δ</th>
      <th class="num">Its tokens at list price ($)</th><th class="num">Δ vs its $</th>
    </tr></thead><tbody>${body}</tbody></table></div>
    <p class="foot-note"><strong>Logged here</strong> is what the transcripts hold, priced at the list rates, for
    the requests Claude Code's counter could have seen: from the counter's start to its last snapshot.
    <strong>Claude Code counted</strong> and <strong>Claude Code's $</strong> are the counter's own tokens and
    dollars. <strong>Its tokens at list price</strong> (in dollars) prices the counter's tokens at the published rates (the
    counter doesn't split its cache writes into 5-minute and 1-hour, so they take the transcripts' own split). A positive token or cost Δ means the transcripts hold
    calls the counter didn't, for instance a counter that restarted on resume in a build before 2.1.277. A
    negative one means the counter saw calls no transcript holds; those are priced separately as “not in
    transcripts”. A Δ in the last column means Claude Code charges a rate other than the published one (it
    prices Sonnet 5.5 cache reads at $0.20/MTok against the published $0.10), or, for Haiku 5.5, that some
    calls were over its 100,000-token prompt line, which summed tokens can't show; only that column tests
    the rate table. Only counters that lie wholly inside the selected dates count (a counter can't be split
    by day): every transcript on it, including a resumed or forked one, has to start and end inside. A transcript carrying on its parent's counter is counted once, with the
    parent. The 1M-context <code>[1m]</code> model is folded into the base model.</p>`;
}

function renderTimeChart(rows) {
  const grain = state.grain;
  document.getElementById('grainNoteOverview').textContent = 'per ' + grainInfo(grain).noun;
  const buckets = {};
  const models = Array.from(new Set(rows.map(r => r[2])));
  rows.forEach(r => {
    const k = rollup(r[0], grain);
    buckets[k] = buckets[k] || {};
    buckets[k][r[2]] = (buckets[k][r[2]] || 0) + (state.metric === 'cost' ? r[13] : tokenTotal(r));
  });
  const axis = axisFor(grain);
  const labels = axis.labels;
  const datasets = models.map(m => ({
    label: m, backgroundColor: colorFor(modelColor, m), stack: 'stack',
    data: axis.keys.map(k => (buckets[k] || {})[m] || 0),
  }));
  upsertChart('time', 'timeChart', {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { stacked: true, grid: { color: cssVar('--gridline') }, ticks: { color: cssVar('--text-muted') } },
        y: { stacked: true, grid: { color: cssVar('--gridline') }, ticks: { color: cssVar('--text-muted'),
          callback: v => state.metric === 'cost' ? fmtCost(v) : fmtTokens(v) } },
      },
      plugins: { legend: { display: false },
        tooltip: { callbacks: {
          title: ctx => axis.titles[ctx[0].dataIndex],
          label: ctx => ctx.raw ? `${ctx.dataset.label}: ${state.metric === 'cost' ? fmtCost(ctx.raw) : fmtTokens(ctx.raw)}` : null,
        } } },
    },
  });
  document.getElementById('timeLegend').innerHTML = models.map(m =>
    `<span><span class="swatch" style="background:${colorFor(modelColor,m)}"></span>${esc(m)}</span>`).join('');
}

function renderProjectChart(rows) {
  const topProjects = topProjectsOf(rows);
  const totals = {};
  rows.forEach(r => {
    const proj = projectOf(r[1]);
    const key = topProjects.includes(proj) ? proj : 'Other';
    totals[key] = (totals[key]||0) + (state.metric === 'cost' ? r[13] : tokenTotal(r));
  });
  const fullLabels = Object.keys(totals).sort((a,b) => totals[b]-totals[a]);
  const labels = fullLabels.map(shortProjectLabel);
  const projectColor = { Other: cssVar('--other') };
  topProjects.forEach((p, i) => projectColor[p] = cssVar(SERIES_VARS[i % SERIES_VARS.length]));
  upsertChart('project', 'projectChart', {
    type: 'bar',
    data: { labels, datasets: [{ data: fullLabels.map(l=>totals[l]), backgroundColor: fullLabels.map(l=>colorFor(projectColor,l)) }] },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      scales: {
        x: { grid: { color: cssVar('--gridline') }, ticks: { color: cssVar('--text-muted'), callback: v => state.metric==='cost'?fmtCost(v):fmtTokens(v) } },
        y: { grid: { display: false }, ticks: { color: cssVar('--text-secondary'), font: { size: 11 } } },
      },
      plugins: { legend: { display: false }, tooltip: { callbacks: {
        title: ctx => fullLabels[ctx[0].dataIndex], label: ctx => state.metric==='cost'?fmtCost(ctx.raw):fmtTokens(ctx.raw),
      } } },
    },
  });
}

function renderModelChart(rows) {
  const totals = {};
  rows.forEach(r => { totals[r[2]] = (totals[r[2]]||0) + (state.metric==='cost'?r[13]:tokenTotal(r)); });
  const labels = Object.keys(totals).sort((a,b) => totals[b]-totals[a]);
  upsertChart('model', 'modelChart', {
    type: 'bar',
    data: { labels, datasets: [{ data: labels.map(l=>totals[l]), backgroundColor: labels.map(l=>colorFor(modelColor,l)) }] },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      scales: {
        x: { grid: { color: cssVar('--gridline') }, ticks: { color: cssVar('--text-muted'), callback: v => state.metric==='cost'?fmtCost(v):fmtTokens(v) } },
        y: { grid: { display: false }, ticks: { color: cssVar('--text-secondary'), font: { size: 11 } } },
      },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => state.metric==='cost'?fmtCost(ctx.raw):fmtTokens(ctx.raw) } } },
    },
  });
}

function renderTokenTypeChart(rows) {
  const TYPES = [
    {label:'Input', idx:5, color: cssVar('--series-1')},
    {label:'Output', idx:6, color: cssVar('--series-2')},
    {label:'Cache read', idx:7, color: cssVar('--series-3')},
    {label:'Cache write (5m)', idx:8, color: cssVar('--series-4')},
    {label:'Cache write (1h)', idx:9, color: cssVar('--series-5')},
  ];
  const totals = TYPES.map(t => rows.reduce((s,r) => s+r[t.idx], 0));
  upsertChart('tokenType', 'tokenTypeChart', {
    type: 'bar',
    data: { labels: ['All sessions'], datasets: TYPES.map((t,i) => ({ label: t.label, data: [totals[i]], backgroundColor: t.color, stack: 's' })) },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      scales: {
        x: { stacked: true, grid: { color: cssVar('--gridline') }, ticks: { color: cssVar('--text-muted'), callback: v=>fmtTokens(v) } },
        y: { stacked: true, grid: { display: false }, ticks: { color: cssVar('--text-secondary') } },
      },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${fmtTokens(ctx.raw)}` } } },
    },
  });
  document.getElementById('tokenTypeLegend').innerHTML = TYPES.map((t,i) =>
    `<span><span class="swatch" style="background:${t.color}"></span>${t.label}: ${fmtTokens(totals[i])}</span>`).join('');
}

// A transcript that carried on another one's CLI counter has its CLI figure
// (and lines changed) on that session's row, so it isn't counted twice.
function cliElsewhere(s) {
  if (!s.cli_counted_in) return '—';
  const owner = (PAYLOAD.sessions || []).find(x => x.id === s.cli_counted_in);
  const name = owner ? owner.title : s.cli_counted_in;
  return `<span class="foot-note" style="margin:0" title="${esc('Shares a CLI cost counter with “' + name + '”; the CLI figure is on that row.')}">shared</span>`;
}

function renderOverviewTable(rows) {
  const tbody = document.querySelector('#overviewTable tbody');
  const sorted = [...rows].sort((a,b) => b[0].localeCompare(a[0]));
  if (!sorted.length) { tbody.innerHTML = emptyRow(9, 'No usage matches the current filters.'); return; }
  tbody.innerHTML = sorted.slice(0, 500).map(r => `
    <tr><td>${esc(bucketTitle(r[0], 5))}</td><td>${esc(shortProjectLabel(projectOf(r[1])))}</td><td>${esc(r[2])}</td><td>${esc(r[3])}</td>
      <td class="num">${fmtInt(r[5])}</td><td class="num">${fmtInt(r[6])}</td>
      <td class="num">${fmtInt(r[7])}</td><td class="num">${fmtInt(r[8]+r[9])}</td>
      <td class="num">${fmtCost(r[13])}</td></tr>`).join('');
}

// ===================== Skills =====================
function renderSkills() {
  const rows = usageRows();
  const calls = skillCallRows();
  const metric = state.skillsMetric; // 'tokens' | 'cost'
  const valOf = r => metric === 'cost' ? r[13] : tokenTotal(r);
  const fmt = metric === 'cost' ? fmtCost : fmtTokens;
  document.getElementById('skillCostTitle').textContent = metric === 'cost' ? 'Cost by skill' : 'Tokens by skill';
  document.getElementById('skillWeeklyTitle').textContent =
    (metric === 'cost' ? 'Cost by skill' : 'Tokens by skill') + ' per ' + grainInfo(state.grain).noun;
  document.getElementById('skillHeatmapTitle').textContent =
    'Skill \u00d7 project (' + (metric === 'cost' ? 'cost' : 'tokens') + ')';

  // Everything below charts attributed work only; the unattributed share is
  // reported in the tiles above instead of competing for the same axis.
  const attributed = rows.filter(r => (r[3] || NO_SKILL) !== NO_SKILL);
  renderSkillCoverage(rows, attributed, calls, valOf, fmt);

  const costBySkill = {};
  attributed.forEach(r => { costBySkill[r[3]] = (costBySkill[r[3]]||0) + valOf(r); });
  const costEntries = topNPlusOther(costBySkill, 8);
  upsertChart('skillCost', 'skillCostChart', {
    type: 'bar',
    data: { labels: costEntries.map(e=>e[0]), datasets: [{ data: costEntries.map(e=>e[1]), backgroundColor: costEntries.map(e=>colorFor(skillColor,e[0])) }] },
    options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      scales: { x: { ticks: { color: cssVar('--text-muted'), callback: v=>fmt(v) }, grid: { color: cssVar('--gridline') } },
                y: { ticks: { color: cssVar('--text-secondary'), font: {size:11} }, grid: { display:false } } },
      plugins: { legend:{display:false}, tooltip:{ callbacks:{ label: ctx => fmt(ctx.raw) } } } },
  });

  const invBySkill = {};
  calls.forEach(r => { invBySkill[r[2]] = (invBySkill[r[2]]||0) + r[3]; });
  const invEntries = topNPlusOther(invBySkill, 8);
  upsertChart('skillInv', 'skillInvocationsChart', {
    type: 'bar',
    data: { labels: invEntries.map(e=>e[0]), datasets: [{ data: invEntries.map(e=>e[1]), backgroundColor: invEntries.map(e=>colorFor(skillColor,e[0])) }] },
    options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      scales: { x: { ticks: { color: cssVar('--text-muted') }, grid: { color: cssVar('--gridline') } },
                y: { ticks: { color: cssVar('--text-secondary'), font: {size:11} }, grid: { display:false } } },
      plugins: { legend:{display:false} } },
  });

  // Weekly stacked by skill (top 7 + Other). The seven are coloured by their
  // place in this chart, one hue each: the all-time colour map cycles after eight
  // and would give two of these the same one.
  const top7 = Object.entries(costBySkill).sort((a,b)=>b[1]-a[1]).slice(0,7).map(e=>e[0]);
  const grain = state.grain;
  const weekly = {};
  attributed.forEach(r => {
    const wk = rollup(r[0], grain); const sk = r[3];
    const key = top7.includes(sk) ? sk : 'Other';
    weekly[wk] = weekly[wk] || {};
    weekly[wk][key] = (weekly[wk][key]||0) + valOf(r);
  });
  const skAxis = axisFor(grain);
  const wkLabels = skAxis.labels;
  // "Other" only when something fell into it: a lone grey legend entry over an
  // empty chart says nothing.
  const hasOther = Object.values(weekly).some(b => b.Other);
  const wkSeries = hasOther ? [...top7, 'Other'] : top7;
  const wkColor = sk => sk === 'Other' ? cssVar('--other') : cssVar(SERIES_VARS[top7.indexOf(sk) % SERIES_VARS.length]);
  upsertChart('skillWeekly', 'skillWeeklyChart', {
    type: 'bar',
    data: { labels: wkLabels, datasets: wkSeries.map(sk => ({
      label: sk, stack: 's', backgroundColor: wkColor(sk),
      data: skAxis.keys.map(w => (weekly[w] && weekly[w][sk]) || 0),
    })) },
    options: { responsive: true, maintainAspectRatio: false,
      scales: { x: { stacked:true, ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} },
                y: { stacked:true, ticks:{color:cssVar('--text-muted'), callback: v=>fmt(v)}, grid:{color:cssVar('--gridline')} } },
      plugins: { legend:{display:false}, tooltip:{ callbacks:{
        title: ctx => skAxis.titles[ctx[0].dataIndex],
        label: ctx => ctx.raw ? `${ctx.dataset.label}: ${fmt(ctx.raw)}` : null,
      } } } },
  });
  document.getElementById('skillWeeklyLegend').innerHTML = wkSeries.map(sk =>
    `<span><span class="swatch" style="background:${wkColor(sk)}"></span>${esc(sk)}</span>`).join('');

  renderSkillHeatmap(attributed, valOf, fmt);
  renderSkillsTable(attributed, calls);
  renderSkillsFootnote(attributed);
}

// Output tokens Claude Code logged as a placeholder and this tool imputed are
// inside every token and dollar figure on the Skills and Agents tabs; say so.
function imputedNote(rows) {
  const n = rows.reduce((s, r) => s + (r[16] || 0), 0);
  return n > 0 ? `${fmtTokens(n)} imputed output tokens are included in these figures` : null;
}
function renderSkillsFootnote(attributed) {
  const el = document.getElementById('skillsFootnote');
  if (!el) return;
  const imp = imputedNote(attributed);
  el.textContent = 'Invocations count a skill Claude started through the Skill tool and one you started with a ' +
    'slash command. Requests are the ones Claude Code tagged with the skill; a skill invoked but never tagged ' +
    'shows 0 requests. ' +
    (imp ? imp[0].toUpperCase() + imp.slice(1) + ': responses logged with a placeholder output count (most subagent ' +
      'responses from Claude Code 2.1.284 until 2.1.307 fixed it) are counted at an imputed size, in tokens and in cost; ' +
      'see docs/limitations.md.' : '');
}

// Says plainly how much of the work the rest of this tab covers, so the
// excluded 90% is visible rather than silently missing.
function renderSkillCoverage(allRows, attributed, calls, valOf, fmt) {
  const word = fmt === fmtCost ? 'Cost' : 'Tokens';
  const total = allRows.reduce((s,r) => s + valOf(r), 0);
  const inSkill = attributed.reduce((s,r) => s + valOf(r), 0);
  const names = new Set(attributed.map(r => r[3]));
  // The table lists every skill with requests or invocations; so does this count.
  const invoked = new Set(calls.map(r => r[2]));
  const all = new Set([...names, ...invoked]);
  const invocations = calls.reduce((s,r) => s + r[3], 0);
  const byCommand = calls.filter(r => skillCallParts(r).via === 'command').reduce((s,r) => s + r[3], 0);
  const imp = imputedNote(attributed);
  renderTiles('skillStatTiles', [
    [`${word} inside a skill`, fmt(inSkill),
     total > 0 ? `${fmtPct(inSkill/total)} of ${fmt(total)} in range` : null, imp],
    ['Skills used', fmtInt(all.size),
     `${fmtInt(names.size)} with requests · ${fmtInt(invocations)} invocation${invocations === 1 ? '' : 's'}` +
     (byCommand ? ` (${fmtInt(byCommand)} by slash command)` : '')],
    ['Outside any skill', fmt(total - inSkill),
     total > 0 ? `${fmtPct((total-inSkill)/total)} — charted below excludes this` : null],
  ]);
}

function renderSkillHeatmap(rows, valOf, fmt) {
  const skills = Array.from(new Set(rows.map(r => r[3]))).filter(Boolean).sort();
  // The biggest projects, then one "Other" column for the rest, so no spend
  // drops out of the grid.
  const top = topProjectsOf(rows);
  const hasOther = rows.some(r => !top.includes(projectOf(r[1])));
  const projects = hasOther ? [...top, 'Other'] : top;
  const matrix = {};
  let max = 0;
  rows.forEach(r => {
    const sk = r[3]; const proj = top.includes(projectOf(r[1])) ? projectOf(r[1]) : 'Other';
    const key = sk + '|' + proj;
    matrix[key] = (matrix[key]||0) + valOf(r);
    if (matrix[key] > max) max = matrix[key];
  });
  const el = document.getElementById('skillHeatmap');
  if (!rows.length) {
    el.style.gridTemplateColumns = '1fr';
    el.innerHTML = `<div class="empty-state"><div class="es-title">Nothing in this range</div>` +
      `<div class="es-body">No work inside a skill matches the current filters.</div></div>`;
    return;
  }
  el.style.gridTemplateColumns = `120px repeat(${projects.length}, 1fr)`;
  let html = '<div></div>' + projects.map(p => `<div class="hm-label">${esc(shortProjectLabel(p))}</div>`).join('');
  skills.forEach(sk => {
    html += `<div class="hm-label">${esc(sk)}</div>`;
    projects.forEach(p => {
      const v = matrix[sk+'|'+p] || 0;
      const frac = max > 0 ? v / max : 0;
      html += `<div class="hm-cell" style="background:${seqColor(frac)}" title="${esc(sk)} × ${esc(shortProjectLabel(p))}: ${fmt(v)}">${v ? fmt(v) : ''}</div>`;
    });
  });
  el.innerHTML = html;
}

function renderSkillsTable(rows, calls) {
  const blank = () => ({ invocations:0, byCommand:0, requests:0, tokens:0, cost:0, sessions:new Set() });
  const bySkill = {};
  rows.forEach(r => {
    const sk = r[3];
    bySkill[sk] = bySkill[sk] || blank();
    bySkill[sk].requests += r[12]; bySkill[sk].tokens += tokenTotal(r); bySkill[sk].cost += r[13];
    bySkill[sk].sessions.add(r[1]);
  });
  calls.forEach(r => {
    bySkill[r[2]] = bySkill[r[2]] || blank();
    bySkill[r[2]].invocations += r[3];
    if (skillCallParts(r).via === 'command') bySkill[r[2]].byCommand += r[3];
    // A skill that was invoked but whose requests carry no skill tag still
    // belongs to the session that invoked it.
    bySkill[r[2]].sessions.add(r[1]);
  });
  const sorted = Object.entries(bySkill).sort((a,b) => b[1].cost - a[1].cost || a[0].localeCompare(b[0]));
  const skillsBody = document.querySelector('#skillsTable tbody');
  if (!sorted.length) { skillsBody.innerHTML = emptyRow(6, 'No skill usage matches the current filters.'); return; }
  skillsBody.innerHTML = sorted.map(([sk,v]) => `
    <tr><td>${esc(sk)}</td><td class="num"${v.byCommand ? ` title="${esc(fmtInt(v.invocations - v.byCommand) + ' by Claude through the Skill tool, ' + fmtInt(v.byCommand) + ' by slash command')}"` : ''}>${fmtInt(v.invocations)}</td><td class="num">${fmtInt(v.requests)}</td>
      <td class="num">${fmtTokens(v.tokens)}</td><td class="num">${fmtCost(v.cost)}</td><td class="num">${fmtInt(v.sessions.size)}</td></tr>`).join('');
}

// ===================== Tools & errors =====================
// The server an MCP tool belongs to, tidied so one service isn't split across
// bars: Claude Code names the same connector "claude_ai_Atlassian",
// "plugin_atlassian_atlassian" or a bare id, depending on how it was added. A
// UUID-named connector can't be told by name, so it is labelled by its id.
function mcpServerOf(name) {
  if (!name.startsWith('mcp__')) return '(core tools)';
  const raw = name.split('__')[1] || '(mcp)';
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(raw)) return 'connector ' + raw.slice(0, 8);
  // claude_ai_Atlassian, claude_ai_Atlassian_2 and plugin_atlassian_atlassian are one service
  return raw.replace(/^claude_ai_/i, '').replace(/^plugin_/i, '').replace(/_\d+$/, '')
            .replace(/^(.+)_\1$/i, '$1').toLowerCase() || '(mcp)';
}

function renderTools() {
  const rows = toolRows();
  const byTool = {};
  rows.forEach(r => {
    const [, , name, calls, errors, isSub] = r;
    byTool[name] = byTool[name] || { calls:0, errors:0, main:0, sub:0 };
    byTool[name].calls += calls; byTool[name].errors += errors;
    byTool[name][isSub ? 'sub' : 'main'] += calls;
  });

  const topCalls = Object.entries(byTool).sort((a,b)=>b[1].calls-a[1].calls).slice(0,15);
  upsertChart('toolCalls', 'toolCallsChart', {
    type: 'bar',
    data: { labels: topCalls.map(e=>e[0]), datasets: [{ data: topCalls.map(e=>e[1].calls), backgroundColor: cssVar('--series-solo') }] },
    options: { indexAxis:'y', responsive:true, maintainAspectRatio:false,
      scales: { x:{ ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} }, y:{ ticks:{color:cssVar('--text-secondary'),font:{size:11}}, grid:{display:false} } },
      plugins:{ legend:{display:false} } },
  });

  // Ranked by errors, not by rate: a rarely-called tool that failed a few
  // times can't outrank the one behind most of the failures. The bar is the
  // error count, coloured by rate. A name that isn't in Claude Code's documented tool list and
  // never once succeeded (made up by the model, or not available) is left out and
  // named below, and so is an MCP tool name that never worked while its server's
  // other tools did; see inventedTool and strayMcpTools in habits.js.
  const stray = strayMcpTools();
  const MIN_CALLS = 10, TOP = 15;
  const withErrors = Object.entries(byTool).filter(([,v]) => v.errors > 0);
  const ranked = withErrors.filter(([,v]) => v.calls >= MIN_CALLS);
  const invented = ranked.filter(([name,v]) => inventedTool(name, v));
  const strays = withErrors.filter(([name]) => stray.has(name));
  const eligible = ranked.filter(([name,v]) => !inventedTool(name, v) && !stray.has(name))
    .map(([name,v]) => [name, v.errors, v.calls, v.errors/v.calls])
    .sort((a,b) => b[1]-a[1] || a[0].localeCompare(b[0]));
  const errorEntries = eligible.slice(0, TOP);
  const allErrors = Object.values(byTool).reduce((s,v) => s + v.errors, 0);
  const allCalls = Object.values(byTool).reduce((s,v) => s + v.calls, 0);
  const small = withErrors.filter(([,v]) => v.calls < MIN_CALLS);
  const smallErrors = small.reduce((n,[,v]) => n + v.errors, 0);
  const hiddenTop = eligible.slice(TOP);
  const noErrorsMsg = allCalls ? {
    title: allErrors ? 'No tool is ranked here' : 'No tool errors in this range',
    body: allErrors ? `Errors were on tools with fewer than ${MIN_CALLS} calls, or on names left out below.`
                    : `${fmtInt(allCalls)} tool call${allCalls === 1 ? '' : 's'}, none failed.`,
  } : null;
  upsertChart('toolErrorRate', 'toolErrorRateChart', {
    type: 'bar',
    data: { labels: errorEntries.map(e=>e[0]), datasets: [{
      data: errorEntries.map(e=>e[1]),
      backgroundColor: errorEntries.map(e => e[3] >= 0.15 ? cssVar('--critical') : e[3] >= 0.05 ? cssVar('--warning') : cssVar('--good')),
    }] },
    options: { indexAxis:'y', responsive:true, maintainAspectRatio:false,
      scales: { x:{ min:0, ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} },
                y:{ ticks:{color:cssVar('--text-secondary'),font:{size:11}}, grid:{display:false} } },
      plugins:{ legend:{display:false}, tooltip:{ callbacks:{ label: ctx => {
        const [, errors, calls, rate] = errorEntries[ctx.dataIndex];
        return `${fmtInt(errors)} errors of ${fmtInt(calls)} calls (${(rate*100).toFixed(1)}%)` +
               (allErrors ? ` · ${(100*errors/allErrors).toFixed(1)}% of all errors` : '');
      } } } } },
  }, noErrorsMsg);
  const note = document.getElementById('toolErrorNote');
  if (note) note.textContent = [
    hiddenTop.length
      ? `Showing the ${TOP} tools with the most errors of ${fmtInt(eligible.length)}; ${fmtInt(hiddenTop.length)} more ` +
        `(${fmtInt(hiddenTop.reduce((n,e) => n + e[1], 0))} errors) not shown.` : '',
    small.length
      ? `${fmtInt(small.length)} tool${small.length === 1 ? '' : 's'} with fewer than ${MIN_CALLS} calls ` +
        `(${fmtInt(smallErrors)} error${smallErrors === 1 ? '' : 's'}) aren't ranked.` : '',
    invented.length
      ? `Left out: ${invented.map(([name,v]) => `${name} (${fmtInt(v.calls)} ${v.calls === 1 ? "call" : "calls"}, none succeeded)`).join(', ')}, ` +
        `a tool name that isn't in Claude Code's documented tool list and never succeeded (made up by the model, or not available).` : '',
    strays.length
      ? `Also left out: ${strays.map(([name,v]) => `${name} (${fmtInt(v.calls)} ${v.calls === 1 ? "call" : "calls"}, none succeeded)`).join(', ')}, ` +
        `an MCP tool name that never worked while other tools on its server did.` : '',
  ].filter(Boolean).join(' ');

  // Core tools would drown the integrations (most calls are Read, Bash and
  // Edit), so the rollup is MCP servers only, with the core total said below.
  const byServer = {};
  let coreCalls = 0, coreErrors = 0;
  Object.entries(byTool).forEach(([name,v]) => {
    if (!isMcpTool(name)) { coreCalls += v.calls; coreErrors += v.errors; return; }
    if (stray.has(name)) return;
    const srv = mcpServerOf(name);
    byServer[srv] = byServer[srv] || {calls:0, errors:0};
    byServer[srv].calls += v.calls; byServer[srv].errors += v.errors;
  });
  const serverEntries = Object.entries(byServer).sort((a,b)=>b[1].calls-a[1].calls);
  upsertChart('mcpServer', 'mcpServerChart', {
    type: 'bar',
    data: { labels: serverEntries.map(e=>e[0]), datasets: [{ data: serverEntries.map(e=>e[1].calls), backgroundColor: cssVar('--series-solo') }] },
    options: { indexAxis:'y', responsive:true, maintainAspectRatio:false,
      scales: { x:{ ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} }, y:{ ticks:{color:cssVar('--text-secondary'),font:{size:11}}, grid:{display:false} } },
      plugins:{ legend:{display:false}, tooltip:{ callbacks:{ label: ctx => `${fmtInt(ctx.raw)} calls, ${fmtInt(serverEntries[ctx.dataIndex][1].errors)} errors` } } } },
  }, coreCalls ? { title: 'No MCP tool calls in this range', body: `Only Claude Code's own tools were used (${fmtInt(coreCalls)} calls).` } : null);
  const mcpNote = document.getElementById('mcpNote');
  if (mcpNote) mcpNote.textContent = `MCP servers only. Claude Code's own tools (${fmtInt(coreCalls)} calls, ` +
    `${fmtInt(coreErrors)} errors) are left out so they don't swamp the integrations. Server names are tidied so ` +
    `one service isn't split by how it was added (a claude_ai_ or plugin_ prefix and a trailing _2 are dropped); a connector named ` +
    `only by an id shows as "connector" and its first characters, and can't be matched to the same service under its name.`;

  const grain = state.grain;
  document.getElementById('grainNoteTools').textContent = 'per ' + grainInfo(grain).noun;
  const weekly = {};
  rows.forEach(r => { const wk = rollup(r[0], grain); weekly[wk] = (weekly[wk]||0) + r[4]; });
  const errAxis = axisFor(grain);
  const errTotal = rows.reduce((n, r) => n + r[4], 0);
  const noWeeklyErrors = rows.length && !errTotal
    ? { title: 'No tool errors in this range', body: `${fmtInt(allCalls)} tool call${allCalls === 1 ? '' : 's'}, none failed.` } : null;
  upsertChart('errorsWeekly', 'errorsWeeklyChart', {
    type: 'bar',
    data: { labels: errAxis.labels, datasets: [{ data: errAxis.keys.map(w=>weekly[w]||0), backgroundColor: cssVar('--critical') }] },
    options: { responsive:true, maintainAspectRatio:false,
      scales: { x:{ ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} }, y:{ ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} } },
      plugins:{ legend:{display:false}, tooltip:{ callbacks:{
        title: ctx => errAxis.titles[ctx[0].dataIndex],
        label: ctx => `${fmtInt(ctx.raw)} error${ctx.raw === 1 ? '' : 's'}`,
      } } } },
  }, noWeeklyErrors);

  const sorted = Object.entries(byTool).sort((a,b)=>b[1].calls-a[1].calls);
  const toolsBody = document.querySelector('#toolsTable tbody');
  if (!sorted.length) { toolsBody.innerHTML = emptyRow(7, 'No tool calls match the current filters.'); return; }
  toolsBody.innerHTML = sorted.map(([name,v]) => `
    <tr><td>${esc(name)}</td><td>${esc(mcpServerOf(name))}</td><td class="num">${fmtInt(v.calls)}</td>
      <td class="num">${fmtInt(v.errors)}</td><td class="num">${fmtPct(v.calls?v.errors/v.calls:null)}</td>
      <td class="num">${fmtInt(v.main)}</td><td class="num">${fmtInt(v.sub)}</td></tr>`).join('');
}

// ===================== Agents =====================
function renderAgents() {
  const agents = agentRows();
  const rows = usageRows();
  const metric = state.agentsMetric; // 'tokens' | 'cost'
  const valOf = r => metric === 'cost' ? r[13] : tokenTotal(r);
  const fmt = metric === 'cost' ? fmtCost : fmtTokens;
  document.getElementById('agentTypeMetricTitle').textContent = `Subagents by type (${metric})`;
  document.getElementById('mainVsSubTitle').textContent =
    `Main vs subagent ${metric} per ${grainInfo(state.grain).noun}`;

  const subCost = rows.filter(r=>r[4]!==-1).reduce((s,r)=>s+r[13],0);
  const mainCost = rows.filter(r=>r[4]===-1).reduce((s,r)=>s+r[13],0);
  const subTokens = rows.filter(r=>r[4]!==-1).reduce((s,r)=>s+tokenTotal(r),0);
  const mainTokens = rows.filter(r=>r[4]===-1).reduce((s,r)=>s+tokenTotal(r),0);

  // Imputed output (placeholder counts replaced by an estimate) is in the output
  // share, the token total and the cost; the thinking share is logged thinking
  // only, so it says so (THINKING_NOTE).
  const subImputed = rows.filter(r => r[4] !== -1).reduce((s, r) => s + (r[16] || 0), 0);
  const allImputed = rows.reduce((s, r) => s + (r[16] || 0), 0);
  const impText = n => n > 0 ? `incl. ${fmtTokens(n)} imputed` : null;
  renderTiles('agentStatTiles', [
    ['Subagents', fmtInt(agents.length), 'started in these dates'],
    ['Subagent tokens', fmtTokens(subTokens), impText(subImputed), UNLOGGED_CALLS_NOTE],
    ['Subagent cost', fmtCost(subCost), subImputed > 0 ? 'incl. the imputed output' : null, UNLOGGED_CALLS_NOTE],
    ['Subagent cost share', fmtPct((mainCost+subCost)>0 ? subCost/(mainCost+subCost) : null)],
    ['Subagent output share', fmtPct(tokenShare(rows, 6)),
     ['of output tokens', impText(allImputed)].filter(Boolean).join(' · '), UNLOGGED_CALLS_NOTE],
    ['Subagent thinking share', fmtPct(tokenShare(rows, 10)), 'of logged thinking', THINKING_NOTE],
  ]);
  const agentsNote = document.getElementById('agentsFootnote');
  if (agentsNote) agentsNote.textContent =
    'Tokens and costs are the requests inside the selected dates and models, as on the Overview, and include ' +
    (allImputed > 0 ? `${fmtTokens(allImputed)} output tokens imputed for responses logged with a placeholder count ` +
      '(see the note on Total tokens). ' : 'any imputed output. ') +
    'Subagents are counted by the day they started, so the count and these totals can cover different work; ' +
    'the table at the bottom gives each subagent\'s whole run on every model.';

  const countByType = {}, valByType = {};
  agents.forEach(a => { countByType[a.agent_type] = (countByType[a.agent_type]||0) + 1; });
  // From the usage rows, like the tiles above, so the bars add up to Subagent
  // cost: an agent's whole-run figure would count work outside the date range
  // and on models the filter excludes, and miss agents that started before it.
  rows.filter(r => r[4] !== -1).forEach(r => {
    valByType[r[4]] = (valByType[r[4]]||0) + valOf(r);
  });
  const countEntries = Object.entries(countByType).sort((a,b)=>b[1]-a[1]);
  upsertChart('agentTypeCount', 'agentTypeCountChart', {
    type:'bar', data:{ labels:countEntries.map(e=>e[0]), datasets:[{ data:countEntries.map(e=>e[1]), backgroundColor:countEntries.map(e=>colorFor(agentTypeColor,e[0])) }] },
    options:{ indexAxis:'y', responsive:true, maintainAspectRatio:false,
      scales:{ x:{ ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} }, y:{ ticks:{color:cssVar('--text-secondary'),font:{size:11}}, grid:{display:false} } },
      plugins:{legend:{display:false}} },
  });
  const valEntries = Object.entries(valByType).sort((a,b)=>b[1]-a[1]);
  upsertChart('agentTypeCost', 'agentTypeCostChart', {
    type:'bar', data:{ labels:valEntries.map(e=>e[0]), datasets:[{ data:valEntries.map(e=>e[1]), backgroundColor:valEntries.map(e=>colorFor(agentTypeColor,e[0])) }] },
    options:{ indexAxis:'y', responsive:true, maintainAspectRatio:false,
      scales:{ x:{ ticks:{color:cssVar('--text-muted'), callback:v=>fmt(v)}, grid:{color:cssVar('--gridline')} }, y:{ ticks:{color:cssVar('--text-secondary'),font:{size:11}}, grid:{display:false} } },
      plugins:{legend:{display:false}, tooltip:{callbacks:{label:ctx=>fmt(ctx.raw)}}} },
  });

  const depthBuckets = {'1':0, '2':0, '3+':0};
  agents.forEach(a => { const d = a.spawn_depth >= 3 ? '3+' : String(a.spawn_depth); depthBuckets[d] = (depthBuckets[d]||0)+1; });
  upsertChart('spawnDepth', 'spawnDepthChart', {
    type:'bar', data:{ labels:Object.keys(depthBuckets), datasets:[{ data:Object.values(depthBuckets), backgroundColor:cssVar('--series-solo') }] },
    options:{ responsive:true, maintainAspectRatio:false,
      scales:{ x:{ ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} }, y:{ ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} } },
      plugins:{legend:{display:false}} },
  });

  const grain = state.grain;
  const weekly = {};
  rows.forEach(r => {
    const wk = rollup(r[0], grain); weekly[wk] = weekly[wk] || {main:0, sub:0};
    weekly[wk][r[4]===-1 ? 'main' : 'sub'] += valOf(r);
  });
  const agAxis = axisFor(grain);
  upsertChart('mainVsSub', 'mainVsSubChart', {
    type:'bar',
    data:{ labels:agAxis.labels, datasets:[
      { label:'Main', stack:'s', backgroundColor:cssVar('--series-1'), data:agAxis.keys.map(w=>(weekly[w]||{}).main||0) },
      { label:'Subagents', stack:'s', backgroundColor:cssVar('--series-2'), data:agAxis.keys.map(w=>(weekly[w]||{}).sub||0) },
    ]},
    options:{ responsive:true, maintainAspectRatio:false,
      scales:{ x:{stacked:true, ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')}}, y:{stacked:true, ticks:{color:cssVar('--text-muted'),callback:v=>fmt(v)}, grid:{color:cssVar('--gridline')}} },
      plugins:{ legend:{position:'top', labels:{color:cssVar('--text-secondary')}}, tooltip:{callbacks:{
        title: ctx => agAxis.titles[ctx[0].dataIndex],
        label: ctx => ctx.raw ? `${ctx.dataset.label}: ${fmt(ctx.raw)}` : null,
      }} } },
  });

  const agentsBody = document.querySelector('#agentsTable tbody');
  const tableNote = document.getElementById('agentsTableNote');
  const AGENT_ROWS = 500;
  if (!agents.length) {
    agentsBody.innerHTML = emptyRow(8, 'No subagents match the current filters.');
    if (tableNote) tableNote.textContent = '';
    return;
  }
  // Newest first, with the agent's id to break ties so the order (and which 500
  // are cut) never depends on the order the rows arrived in.
  const newest = [...agents].sort((a,b) => (b.date||'').localeCompare(a.date||'') ||
    String(a.agent_id || '').localeCompare(String(b.agent_id || '')));
  if (tableNote) tableNote.textContent = (newest.length > AGENT_ROWS
    ? `Showing the newest ${fmtInt(AGENT_ROWS)} of ${fmtInt(newest.length)} subagents. ` : '') +
    'Tokens and cost are the whole run, on every model, so they can exceed what the charts count inside these dates and models.';
  agentsBody.innerHTML = newest
    .slice(0, AGENT_ROWS).map(a => `
    <tr><td>${esc(a.date||'—')}</td><td>${esc((sessionById[a.session_id]&&sessionById[a.session_id].title)||a.real_session_id||a.session_id)}</td>
      <td>${esc(a.agent_type)}</td><td class="num">${esc(a.spawn_depth)}</td><td>${esc(a.description||'')}</td>
      <td>${esc(a.models.join(', '))}</td><td class="num">${fmtTokens(a.tokens)}</td><td class="num">${fmtCost(a.cost)}</td></tr>`).join('');
}

// ===================== Sessions =====================
function sessionRowsWithCost() {
  const cost = costBySession(usageRows());
  return sessionRows().map(s => ({ ...s, est_total: cost[s.id] || 0, est_whole: wholeSessionCost(s) }));
}

function renderSessions() {
  const rows = sessionRowsWithCost();

  upsertChart('sessionScatter', 'sessionScatterChart', {
    type: 'scatter',
    data: { datasets: [{
      label: 'Sessions',
      // Both axes are the whole session: the range-filtered cost would sit
      // against an active time that isn't filtered.
      data: rows.map(s => ({ x: s.active_ms/60000, y: s.est_whole, label: s.title })),
      backgroundColor: cssVar('--series-solo'),
    }] },
    options: { responsive:true, maintainAspectRatio:false,
      scales: {
        x: { title:{display:true, text:'Active minutes (whole session)', color:cssVar('--text-muted')}, ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} },
        y: { title:{display:true, text:'Est. cost (whole session)', color:cssVar('--text-muted')}, ticks:{color:cssVar('--text-muted'), callback:v=>fmtCost(v)}, grid:{color:cssVar('--gridline')} },
      },
      plugins: { legend:{display:false}, tooltip:{ callbacks:{ label: ctx => `${ctx.raw.label}: ${Math.round(ctx.raw.x)}m, ${fmtCost(ctx.raw.y)}` } } },
    },
  });

  renderSessionsTable(rows);
  wireSessionSort();

  // Say how many of the listed transcripts are sessions in the totals above:
  // the Overview counts those that got a reply, and copies not at all.
  const originals = rows.filter(s => !s.derived);
  const noReply = originals.filter(s => !didWork(s)).length;
  const copies = rows.length - originals.length;
  const caption = document.getElementById('sessionsCaption');
  if (caption) caption.textContent =
    `${fmtInt(originals.length - noReply)} session${originals.length - noReply === 1 ? '' : 's'}` +
    (noReply ? `, plus ${fmtInt(noReply)} transcript${noReply === 1 ? '' : 's'} that never got a reply ` +
      `(badged "no reply", not counted in the totals above)` : '') +
    (copies ? `, plus ${fmtInt(copies)} fork/resume cop${copies === 1 ? 'y' : 'ies'}` : '') +
    '. Cost is inside the selected dates and models; every other column is the whole session.';

  // The toggle counts the copies the current filters would show, not every copy
  // there is.
  const btn = document.getElementById('forkToggle');
  if (btn) {
    const n = PAYLOAD.sessions.filter(s => s.derived && sessionInFilters(s)).length;
    btn.textContent = state.showForks ? `Hide ${n} fork/resume cop${n === 1 ? 'y' : 'ies'}` : `Show ${n} fork/resume cop${n === 1 ? 'y' : 'ies'}`;
    btn.style.display = n || state.showForks ? '' : 'none';
    btn.classList.toggle('active', state.showForks);
    btn.onclick = () => { state.showForks = !state.showForks; onFiltersChanged(); };
  }
}

// What a column sorts on. Project sorts by the label the row shows (not the full
// path, which orders by a folder the table never displays), and Start by the
// session's own first record.
function sessionSortValue(s, key) {
  if (key === 'project') return shortProjectLabel(s.project);
  if (key === 'start') return sessionStartTime(s);
  return s[key];
}
// Text columns open ascending (A first); numbers and dates open with the biggest.
const TEXT_SORT_KEYS = new Set(['title', 'project']);

function sortSessions(rows) {
  const { key, dir } = sessionSort;
  // A total order: a missing value (null, undefined, NaN) sorts after every
  // real one in either direction, so comparing it with a string can't yield NaN
  // and the result never depends on the order the rows arrived in.
  const missing = v => v == null || (typeof v === 'number' && Number.isNaN(v));
  return [...rows].sort((a,b) => {
    const av = sessionSortValue(a, key), bv = sessionSortValue(b, key);
    const am = missing(av), bm = missing(bv);
    if (am || bm) return am === bm ? 0 : (am ? 1 : -1);
    if (typeof av === 'string' && typeof bv === 'string') return dir * av.localeCompare(bv);
    return dir * (av < bv ? -1 : av > bv ? 1 : 0);
  });
}

function toggleSessionDetail(tr, sorted) {
  const existing = tr.nextElementSibling;
  if (existing && existing.classList.contains('expand-row')) {
    existing.remove();
    tr.setAttribute('aria-expanded', 'false');
    return;
  }
  const s = sorted[parseInt(tr.dataset.idx)];
  const byModel = {};
  // Deliberately unfiltered by date: this is the whole session's breakdown,
  // not just the part inside the current range.
  PAYLOAD.usage.filter(r => r[1] === s.id).forEach(r => {
    byModel[r[2]] = byModel[r[2]] || {tokens:0, cost:0};
    byModel[r[2]].tokens += tokenTotal(r); byModel[r[2]].cost += r[13];
  });
  const perModel = Object.entries(byModel)
    .map(([m,v]) => `${esc(m)}: ${fmtTokens(v.tokens)}, ${fmtCost(v.cost)}`).join(' &middot; ')
    || 'No usage recorded for this session';
  let detail = `<div><strong>Whole session, by model:</strong> ${perModel}</div>`;
  if (s.main_tool_calls != null && s.tool_calls != null) {
    detail += `<div><strong>Tool calls:</strong> ${fmtInt(s.main_tool_calls)} in the main conversation · ` +
      `${fmtInt(Math.max(0, s.tool_calls - s.main_tool_calls))} by subagents ` +
      `(${fmtInt(s.tool_errors)} error${s.tool_errors === 1 ? '' : 's'} in all)</div>`;
  }
  if (s.placeholder_responses) {
    detail += `<div><strong>Imputed output:</strong> ${fmtTokens(s.imputed_output_tokens)} tokens for ` +
      `${fmtInt(s.placeholder_responses)} subagent response${s.placeholder_responses === 1 ? '' : 's'} ` +
      `logged with a placeholder count (included above)</div>`;
  }
  if (s.est_cost_unlogged != null) {
    const parts = (s.unlogged_models || []).map(m => `${esc(m.model)}: ${fmtCost(m.cost)}`).join(' &middot; ');
    detail += `<div><strong>Not in the transcripts:</strong> ${fmtCost(s.est_cost_unlogged)}` +
      `${parts ? ` (${parts})` : ''} <span class="foot-note">— what Claude Code's counter recorded ` +
      `beyond the transcripts, at list rates; not included above</span></div>`;
  }
  if ((s.cli_models || []).length) {
    const rows = s.cli_models.map(m => {
      const gap = m.cli_cost > 0 ? (m.est_cost - m.cli_cost) / m.cli_cost : null;
      return `<tr><td>${esc(m.model)}</td><td class="num">${fmtCost(m.est_cost)}</td>` +
             `<td class="num">${fmtCost(m.cli_cost)}</td>` +
             `<td class="num">${gap==null ? '—' : (gap>0?'+':'') + (gap*100).toFixed(1) + '%'}</td>` +
             `<td class="num">${m.cli_repriced == null ? '—' : fmtCost(m.cli_repriced)}</td>` +
             `<td class="num">${fmtTokens(m.est_tokens)}</td><td class="num">${fmtTokens(m.cli_tokens)}</td></tr>`;
    }).join('');
    detail += `<div style="margin-top:8px;"><strong>Logged here vs Claude Code's own counter</strong>` +
      ` <span class="foot-note">(both over the counter's window, from its start to its last snapshot; ` +
      `"its tokens at list price" (in dollars) re-prices the counter's own tokens at this tool's rates)</span>` +
      `<table class="data-table mini"><thead><tr><th>Model</th><th class="num">Logged here</th>` +
      `<th class="num">Claude Code's $</th><th class="num">Δ</th><th class="num">Its tokens at list price ($)</th>` +
      `<th class="num">Logged tok</th><th class="num">Counted tok</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  const row = document.createElement('tr');
  row.className = 'expand-row';
  row.innerHTML = `<td colspan="13">${detail}</td>`;
  tr.after(row);
  tr.setAttribute('aria-expanded', 'true');
}

function renderSessionsTable(rows) {
  const sorted = sortSessions(rows);
  const tbody = document.querySelector('#sessionsTable tbody');
  if (!sorted.length) {
    tbody.innerHTML = emptyRow(13, 'No sessions match the current filters.');
    return;
  }
  tbody.innerHTML = sorted.map((s,i) => {
    const diverge = s.cost_diverges ? ' <span class="badge warn" title="Over the counter\'s own window, Claude Code\'s cost and this tool\'s estimate differ by more than 15%. The Overview\'s reconciliation table shows the models behind it.">Δ&gt;15%</span>' : '';
    const fork = s.derived ? ' <span class="badge" title="Every request in this transcript also appears in another one — a fork/resume copy, not a separate session.">fork</span>' : '';
    const noReply = !s.derived && !didWork(s)
      ? ' <span class="badge" title="This transcript never got a reply, so it is not counted as a session in the totals above.">no reply</span>' : '';
    // stopPropagation: following a link must not also open the row.
    const prLinks = (s.prs||[]).map(p => `<a href="${safeUrl(p.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">#${esc(p.number)}</a>`).join(' ');
    const linesStr = (s.lines_added!=null) ? `+${fmtInt(s.lines_added)}/-${fmtInt(s.lines_removed)}` : '—';
    // The filters can cut a session down to part of its work; say how much of
    // the whole it is, since the CLI figure beside it is always whole-session.
    const partial = Math.abs(s.est_whole - s.est_total) >= 0.005
      ? `<div class="foot-note" style="margin:0" title="Whole session, every model">of ${fmtCost(s.est_whole)}</div>` : '';
    const start = sessionStartTime(s);
    return `<tr class="expandable" data-idx="${i}" tabindex="0" aria-expanded="false">
      <td>${esc(s.title)}${fork}${noReply}</td><td>${esc(shortProjectLabel(s.project))}</td><td title="${esc(viewerZoneNote())}">${esc(start ? new Date(start).toLocaleString() : '—')}</td>
      <td class="num">${fmtDuration(s.active_ms)}</td><td>${esc(s.models.join(', '))}</td><td class="num">${fmtInt(s.requests)}</td>
      <td class="num">${fmtCost(s.est_total)}${partial}</td><td class="num">${s.cli_cost!=null?fmtCost(s.cli_cost):cliElsewhere(s)}${diverge}</td>
      <td class="num">${fmtInt(s.n_subagents)}</td><td class="num">${fmtInt(s.tool_calls)} (${fmtInt(s.tool_errors)} err)</td>
      <td class="num">${linesStr}</td><td>${prLinks}</td><td class="habit-tags">${tagsHtml(sessionHabitTags(s))}</td></tr>`;
  }).join('');

  tbody.querySelectorAll('tr.expandable').forEach(tr => {
    tr.addEventListener('click', () => toggleSessionDetail(tr, sorted));
    // The row is a button for keyboard users: Enter or Space opens it.
    tr.addEventListener('keydown', e => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target === tr) { e.preventDefault(); toggleSessionDetail(tr, sorted); }
    });
  });
}

function wireSessionSort() {
  document.querySelectorAll('#sessionsTable th[data-sort]').forEach(th => {
    const key = th.dataset.sort;
    const active = key === sessionSort.key;
    th.classList.toggle('sorted', active);
    th.classList.toggle('sorted-asc', active && sessionSort.dir === 1);
    th.setAttribute('aria-sort', active ? (sessionSort.dir === 1 ? 'ascending' : 'descending') : 'none');
    th.setAttribute('tabindex', '0');
    const sort = () => {
      // The same column flips; a new one opens in its natural direction.
      sessionSort.dir = (sessionSort.key === key) ? -sessionSort.dir : (TEXT_SORT_KEYS.has(key) ? 1 : -1);
      sessionSort.key = key;
      renderSessionsTable(sessionRowsWithCost());
      wireSessionSort();
    };
    th.onclick = sort;
    th.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); } };
  });
}

// ===================== Output & env =====================
function renderEnv() {
  // The sessions counted here are the ones the header counts: originals that got
  // a reply. The Sessions tab's fork toggle and its no-reply transcripts don't
  // move these charts.
  const rows = countedSessions();
  // Lines changed are counted per edit (Edit, Write and the like), dated by the
  // edit itself, so the tile sums the edits inside the range and a long session
  // counts only its work there. Rows: [bucket, session_id, added, removed].
  // They aren't recorded per model, so a model filter can't split them; the
  // tiles say so rather than quietly using a session-level rule.
  const lineRows = (PAYLOAD.lines || []).filter(r => inDateRange(r[0]) && projectOk(projectOf(r[1])));
  const linesAdded = lineRows.reduce((s,r)=>s+r[2],0);
  const linesRemoved = lineRows.reduce((s,r)=>s+r[3],0);
  const modelFiltered = state.models.length > 0;
  const notByModel = 'all models: not recorded per model';
  const prs = prRows().sort((a,b) => (b.date||'').localeCompare(a.date||''));

  renderTiles('envStatTiles', [
    ['Lines added', `+${fmtInt(linesAdded)}`,
     modelFiltered ? notByModel : 'from the edits Claude made in these dates',
     'Counted from the line changes in each Edit and Write result, dated when the edit was made: only the two ' +
     'numbers are kept, never the text. A session that made no edits has none.'],
    ['Lines removed', `-${fmtInt(linesRemoved)}`, modelFiltered ? notByModel : null],
    ['Pull requests', fmtInt(prs.length), modelFiltered ? notByModel : null],
  ]);

  const prsBody = document.querySelector('#prsTable tbody');
  prsBody.innerHTML = prs.length ? prs.map(p => `
    <tr><td>${esc(p.date||'—')}</td><td>${esc(p.repo||'—')}</td><td>#${esc(p.number)}</td>
      <td><a href="${safeUrl(p.url)}" target="_blank" rel="noopener">link</a></td>
      <td>${esc((sessionById[p.session_id]&&sessionById[p.session_id].title)||p.real_session_id||p.session_id)}</td></tr>`).join('')
    : emptyRow(5, 'No pull requests opened in this range.');

  const entryCount = {};
  rows.forEach(s => {
    const entries = Object.entries(s.entrypoints || {});
    const dominant = entries.length ? entries.sort((a,b)=>b[1]-a[1])[0][0] : '(unknown)';
    entryCount[dominant] = (entryCount[dominant]||0) + 1;
  });
  const entryEntries = Object.entries(entryCount).sort((a,b)=>b[1]-a[1]);
  upsertChart('entrypoint', 'entrypointChart', {
    type:'bar', data:{ labels:entryEntries.map(e=>e[0]), datasets:[{ data:entryEntries.map(e=>e[1]), backgroundColor:cssVar('--series-solo') }] },
    options:{ indexAxis:'y', responsive:true, maintainAspectRatio:false,
      // Session counts, as on the permission mode chart below.
      scales:{ x:{ ticks:{color:cssVar('--text-muted'), precision:0}, grid:{color:cssVar('--gridline')} }, y:{ ticks:{color:cssVar('--text-secondary')}, grid:{display:false} } },
      plugins:{legend:{display:false}} },
  });

  // One bar per week of the range, empty weeks included, each session in the week
  // it started (clamped into the range, so a session that began before it doesn't
  // add a week the axis doesn't show). A session counts under the version it ran
  // most; the legend lists the versions actually plotted, the seven most common,
  // with the rest as "Other".
  const dominantVersion = s => {
    const versions = Object.keys(s.versions || {});
    return versions.length ? versions.sort((a,b)=>(s.versions[b]-s.versions[a]) || a.localeCompare(b))[0] : '(unknown)';
  };
  const clampDay = d => d < state.from ? state.from : d > state.to ? state.to : d;
  const versionWeekly = {}, plotted = {};
  rows.forEach(s => {
    const wk = weekOf(clampDay(sessionStartDay(s)));
    const v = dominantVersion(s);
    versionWeekly[wk] = versionWeekly[wk] || {};
    versionWeekly[wk][v] = (versionWeekly[wk][v]||0) + 1;
    plotted[v] = (plotted[v]||0) + 1;
  });
  const top7v = Object.entries(plotted).sort((a,b)=>b[1]-a[1] || a[0].localeCompare(b[0])).slice(0,7).map(e=>e[0]);
  const vAxis = axisFor(10080);
  const vkLabels = vAxis.keys;
  const hasOtherVersion = Object.keys(plotted).some(v => !top7v.includes(v));
  const vSeries = hasOtherVersion ? [...top7v, 'Other'] : top7v;
  upsertChart('versions', 'versionsChart', {
    type:'bar',
    data:{ labels:vkLabels, datasets: vSeries.map((v,i) => ({
      label:v, stack:'s', backgroundColor: v==='Other'?cssVar('--other'):cssVar(SERIES_VARS[i%SERIES_VARS.length]),
      data: vkLabels.map(wk => {
        const dayBucket = versionWeekly[wk] || {};
        if (v === 'Other') return Object.entries(dayBucket).filter(([k])=>!top7v.includes(k)).reduce((s,[,n])=>s+n,0);
        return dayBucket[v] || 0;
      }),
    })) },
    options:{ responsive:true, maintainAspectRatio:false,
      scales:{ x:{stacked:true, ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')}}, y:{stacked:true, ticks:{color:cssVar('--text-muted'), precision:0}, grid:{color:cssVar('--gridline')}} },
      plugins:{ legend:{position:'top', labels:{color:cssVar('--text-secondary'), boxWidth:10, font:{size:10}}},
        tooltip:{ callbacks:{ title: ctx => vAxis.titles[ctx[0].dataIndex] } } } },
  });

  // Branch cost is keyed by project *and* branch. Keying on the branch name
  // alone merged unrelated repos into one bar -- "HEAD" especially, which every
  // detached checkout records, and which is not a branch name at all.
  // The cost is the filtered one -- each session's work inside the date range
  // and on the selected models -- so the bars add up to Total cost.
  const sessionCost = costBySession(usageRows());
  const branchCost = {}, branchMeta = {};
  rows.forEach(s => {
    if (!sessionCost[s.id]) return;
    const branches = Object.entries(s.branches||{});
    const raw = branches.length ? branches.sort((a,b)=>b[1]-a[1])[0][0] : null;
    const key = s.project + '\u0000' + (raw || '');
    branchCost[key] = (branchCost[key]||0) + sessionCost[s.id];
    branchMeta[key] = { project: s.project, raw };
  });
  const branchEntries = topNPlusOther(branchCost, 8);
  const branchLabels = branchEntries.map(([key]) =>
    key === 'Other' ? 'Other' : branchLabel(branchMeta[key]));
  upsertChart('branches', 'branchesChart', {
    type:'bar', data:{ labels:branchLabels, datasets:[{ data:branchEntries.map(e=>e[1]), backgroundColor:cssVar('--series-solo') }] },
    options:{ indexAxis:'y', responsive:true, maintainAspectRatio:false,
      scales:{ x:{ ticks:{color:cssVar('--text-muted'), callback:v=>fmtCost(v)}, grid:{color:cssVar('--gridline')} },
                // Two-line labels double the space each tick needs, so without
                // autoSkip:false Chart.js silently drops some of them.
                y:{ ticks:{color:cssVar('--text-secondary'),font:{size:11}, autoSkip:false}, grid:{display:false} } },
      plugins:{legend:{display:false}, tooltip:{callbacks:{
        // The label is shortened for the axis; the tooltip keeps the full
        // project path and the branch name exactly as git recorded it.
        title: ctx => {
          const meta = branchMeta[branchEntries[ctx[0].dataIndex][0]];
          if (!meta) return 'Other branches';
          return meta.project + '\n' + (meta.raw || '(no branch recorded)');
        },
        label: ctx => fmtCost(ctx.raw),
      }}} },
  });

  const modeCounts = {};
  rows.forEach(s => Object.keys(s.permission_modes||{}).forEach(m => modeCounts[m] = (modeCounts[m]||0)+1));
  const modeEntries = Object.entries(modeCounts).sort((a,b)=>b[1]-a[1]);
  upsertChart('permissionMode', 'permissionModeChart', {
    type:'bar', data:{ labels:modeEntries.map(e=>MODE_LABELS[e[0]] || e[0]), datasets:[{ data:modeEntries.map(e=>e[1]), backgroundColor:cssVar('--series-solo') }] },
    options:{ responsive:true, maintainAspectRatio:false,
      // Session counts: precision 0 keeps a small axis from reading 0, 1, 1, 2, 2.
      scales:{ x:{ ticks:{color:cssVar('--text-muted')}, grid:{color:cssVar('--gridline')} }, y:{ ticks:{color:cssVar('--text-muted'), precision:0}, grid:{color:cssVar('--gridline')} } },
      plugins:{legend:{display:false}} },
  });
}
