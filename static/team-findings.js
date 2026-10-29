'use strict';
// The team dashboard's figures and headline findings, with no DOM: team.js
// renders them, and tests/test_team_insights.py runs them under node.
// Names and repositories come from other people's files; callers escape.

// team.team_payload's weekly row layout.
const W = { person: 0, week: 1, repo: 2, cost: 3, tokens: 4, requests: 5, sessionsActive: 6,
  activeDays: 7, partial: 8, cliGap: 9, linesAdded: 10, linesRemoved: 11, costWithLines: 12,
  sessionsStarted: 13, weekDays: 14, subagentCost: 15, apiRetries: 16, weekActiveDays: 17 };

// The findings' thresholds.
// contextTokens is a line the export already applied (health.py's
// VERY_LONG_CONTEXT_TOKENS); it is here so the findings' text says the same number.
const FINDING = { concentration: 0.40, minCalls: 20, errorRate: 0.10, perDay: 30,
  contextShare: 0.10, contextTokens: 500000, staleDays: 7 };
// Spend the logs couldn't tie to a project folder: never "a repository" for
// the concentration rule.
const NO_PROJECT = '(no project)';
// Anthropic, "Manage costs effectively": about $13 per developer per active
// day on average, under $30 for 90% of users.
const BENCHMARK = { average: 13, p90: 30 };

// Claude Code's own tools: their errors are Claude Code's business, not an
// integration a manager can fix.
const CLAUDE_CODE_TOOLS = new Set(['TaskStop', 'TaskCreate', 'TaskUpdate', 'TaskList', 'TaskGet',
  'TaskOutput', 'ScheduleWakeup', 'Monitor', 'CronCreate', 'CronDelete', 'CronList',
  'EnterPlanMode', 'ExitPlanMode', 'EnterWorktree', 'ExitWorktree', 'ToolSearch', 'SendMessage',
  'PushNotification', 'RemoteTrigger']);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function tierLabel(id) {
  if (!id) return '—';
  const max = /^default_claude_max_(\d+)x$/.exec(id);
  if (max) return `Max ${max[1]}×`;
  const ent = /^enterprise_tier_(\d+)$/.exec(id);
  if (ent) return `Enterprise tier ${ent[1]}`;
  return String(id);
}
// "claude-opus-5-5" -> "Opus 5.5". A dated snapshot ("-20250805") and the
// 1M-context suffix ("[1m]") are the same model, so they share a label. An id
// that doesn't look like a Claude model is shown as it is.
function modelLabel(id) {
  if (!id) return '(unknown)';
  const base = String(id).replace(/\[[^\]]*\]$/, '').replace(/-\d{8}$/, '');
  const m = /^claude-([a-z]+)-(\d+(?:-\d+)?)$/.exec(base);
  if (!m) return String(id);
  return m[1][0].toUpperCase() + m[1].slice(1) + ' ' + m[2].replace('-', '.');
}
function displayName(p) { return String((p && (p.display_name || p.person_name || p.person)) || ''); }
function integrationLabel(integration, tool) {
  if (integration === 'built-in') return String(tool);
  return UUID_RE.test(integration || '') ? `MCP connector (${integration.slice(0, 8)}…)` : `${integration} (MCP)`;
}
function isClaudeCodeTool(integration, tool) { return integration === 'built-in' && CLAUDE_CODE_TOOLS.has(tool); }

// Keyed accumulators hold names from other people's files, so they have no
// prototype: a repo, person or tool called "toString" or "__proto__" is an
// ordinary key, not an Object.prototype member.
const dict = () => Object.create(null);

function repoSpend(rows, top = 8) {
  const by = dict();
  let total = 0;
  rows.forEach(r => {
    const cost = r[W.cost] || 0;
    if (!cost) return;
    const repo = r[W.repo] || NO_PROJECT;
    const b = by[repo] || (by[repo] = { repo, cost: 0, people: dict() });
    b.cost += cost;
    b.people[r[W.person]] = (b.people[r[W.person]] || 0) + cost;
    total += cost;
  });
  let list = Object.values(by).sort((a, b) => b.cost - a.cost || a.repo.localeCompare(b.repo));
  if (list.length > top) {
    const rest = list.slice(top);
    // Marked, not just named: a real repository can be called "Other".
    const other = { repo: 'Other', other: true, count: rest.length, cost: 0, people: dict() };
    rest.forEach(b => {
      other.cost += b.cost;
      Object.entries(b.people).forEach(([p, c]) => { other.people[p] = (other.people[p] || 0) + c; });
    });
    list = list.slice(0, top).concat([other]);
  }
  return {
    total,
    repos: list.map(b => Object.assign({ repo: b.repo, cost: b.cost, share: total ? b.cost / total : 0,
      people: Object.entries(b.people).sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0])) },
      b.other ? { other: true, count: b.count } : {})),
  };
}

// One person-week's active days from its rows (one per project per machine).
// Rows from two machines: the larger figure, never the sum. A row with the
// exact count (week_active_days) contributes to `exact`, the busiest
// machine's days; a row without it (an older export) falls back to its
// per-project days, summed and capped by the days of that week inside an
// export window (the widest window any machine had, never above 7; a row that
// doesn't say counts as a full week). The week's days are the larger of the
// two; `approx` is true when the fallback decided. One rule for the People
// table, the heatmap and spend per active day.
function weekDaysCap(current, weekDays) {
  return Math.max(current, weekDays == null ? 7 : Math.min(7, weekDays));
}
function weekActiveDays(rows) {
  let exact = null, sum = 0, cap = 0;
  rows.forEach(r => {
    if (r[W.weekActiveDays] != null) exact = Math.max(exact == null ? 0 : exact, r[W.weekActiveDays]);
    else { sum += r[W.activeDays] || 0; cap = weekDaysCap(cap, r[W.weekDays]); }
  });
  const e = exact == null ? 0 : exact, fallback = Math.min(sum, cap);
  return { days: Math.max(e, fallback), approx: fallback > e };
}
// The least and most days a week can have from its rows: the busiest single
// project's days (a lower bound: every project day is a day the person worked)
// and the capped sum of the projects' days (an upper bound: days with several
// projects are counted once per project). A week with an exact count has both
// equal to it.
function weekBounds(rows) {
  let exact = null, sum = 0, cap = 0, most = 0;
  rows.forEach(r => {
    if (r[W.weekActiveDays] != null) exact = Math.max(exact == null ? 0 : exact, r[W.weekActiveDays]);
    else {
      const d = r[W.activeDays] || 0;
      sum += d; most = Math.max(most, d); cap = weekDaysCap(cap, r[W.weekDays]);
    }
  });
  const e = exact == null ? 0 : exact;
  return { lo: Math.max(e, Math.min(most, cap)), hi: Math.max(e, Math.min(sum, cap)) };
}
function workdaysOf(intervals) {
  return intervals.reduce((s, iv) => s + overlap(iv.from, iv.to, iv.from, iv.to).workdays, 0);
}
// A person's active days per week, from all their weekly rows. When the export
// is from before exact days and is the person's only machine, the export still
// says how many days the person was active in its whole window (p.person_active_days);
// with the per-week bounds from weekBounds that total decides the weeks: a week
// the bounds pin is exact, the others share what is left in proportion to their
// upper bounds, never leaving their own bounds. Otherwise (two machines, a total
// that doesn't cover the same days, or nothing to go on) each week is the capped
// sum, marked approximate. p may be null (no total known).
function dayBasis(p, allRows, intervals) {
  const byWeek = dict();
  allRows.forEach(r => (byWeek[r[W.week]] || (byWeek[r[W.week]] = [])).push(r));
  const weeks = dict();
  let sumLo = 0, sumHi = 0, anyApprox = false;
  Object.keys(byWeek).forEach(w => {
    const wd = weekActiveDays(byWeek[w]), b = weekBounds(byWeek[w]);
    weeks[w] = { lo: b.lo, hi: b.hi, est: wd.days, approx: wd.approx };
    sumLo += b.lo; sumHi += b.hi;
    if (wd.approx) anyApprox = true;
  });
  const total = p ? p.person_active_days : null;
  const bounded = !!p && anyApprox && p.machines === 1 && typeof total === 'number' && Number.isFinite(total) &&
    sumLo <= total && total <= sumHi && workdaysOf(personIntervals(p, intervals)) === p.window_workdays;
  if (bounded) {
    let budget = total, free = [];
    Object.keys(weeks).forEach(w => {
      const e = weeks[w];
      e.approx = e.lo !== e.hi;
      if (e.approx) free.push(w); else { e.est = e.lo; budget -= e.lo; }
    });
    while (free.length) {
      const top = free.reduce((s, w) => s + weeks[w].hi, 0), scale = top ? budget / top : 0;
      const below = free.filter(w => weeks[w].hi * scale < weeks[w].lo);
      const above = free.filter(w => weeks[w].hi * scale > weeks[w].hi);
      const pin = below.length ? below : above;
      if (!pin.length) { free.forEach(w => { weeks[w].est = weeks[w].hi * scale; }); break; }
      pin.forEach(w => { weeks[w].est = below.length ? weeks[w].lo : weeks[w].hi; budget -= weeks[w].est; });
      free = free.filter(w => !pin.includes(w));
    }
  }
  return { weeks, bounded, total };
}
// Per person, the basis for each of `people` from all the weekly rows.
function activeBasis(people, allRows, intervalsByPerson) {
  const rows = dict();
  allRows.forEach(r => (rows[r[W.person]] || (rows[r[W.person]] = [])).push(r));
  const out = dict();
  (people || []).forEach(p => {
    out[p.person] = dayBasis(p, rows[p.person] || [], intervalsByPerson && intervalsByPerson[p.person]);
  });
  return out;
}
// The selected weeks' active days: {days, approx, lo, hi}. When the person's
// total is known (bounded), the bounds for these weeks follow from it:
//   lo = max(sum of their lower bounds, total - upper bounds of the other weeks)
//   hi = min(sum of their upper bounds, total - lower bounds of the other weeks)
// and days is the estimate held inside them; approx unless lo and hi meet.
// Otherwise days is the sum of the weeks' figures and lo, hi are null.
function rangeDays(basis, weeks) {
  const chosen = dict();
  weeks.forEach(w => { chosen[w] = true; });
  if (!basis.bounded) {
    let days = 0, approx = false;
    Object.keys(basis.weeks).forEach(w => {
      if (!chosen[w]) return;
      days += basis.weeks[w].est;
      if (basis.weeks[w].approx) approx = true;
    });
    return { days, approx, lo: null, hi: null };
  }
  let inLo = 0, inHi = 0, est = 0, outLo = 0, outHi = 0;
  Object.keys(basis.weeks).forEach(w => {
    const e = basis.weeks[w];
    if (chosen[w]) { inLo += e.lo; inHi += e.hi; est += e.est; } else { outLo += e.lo; outHi += e.hi; }
  });
  const lo = Math.max(inLo, basis.total - outHi), hi = Math.min(inHi, basis.total - outLo);
  return { days: Math.round(Math.min(Math.max(est, lo), hi)), approx: lo !== hi, lo, hi };
}
// One week's figure for the heatmap, the same rule as the totals; `rows` are the
// week's rows for a person with no basis.
function weekDaysFigure(basis, week, rows) {
  const e = basis && basis.weeks[week];
  if (e) return { days: Math.round(e.est), approx: e.approx };
  const w = weekActiveDays(rows);
  return { days: w.days, approx: w.approx };
}
// Active days per person over the given rows (the selected weeks' rows).
// bases: activeBasis for the people, so a total from the export can decide the
// estimated weeks; without one each week is its capped sum.
function activeDaysByPerson(rows, bases) {
  const byPerson = dict();
  rows.forEach(r => (byPerson[r[W.person]] || (byPerson[r[W.person]] = [])).push(r));
  const out = dict();
  Object.keys(byPerson).forEach(person => {
    const mine = byPerson[person];
    const basis = (bases && bases[person]) || dayBasis(null, mine, null);
    out[person] = rangeDays(basis, mine.map(r => r[W.week])).days;
  });
  return out;
}

function perActiveDay(rows, bases) {
  const days = activeDaysByPerson(rows, bases);
  const cost = dict();
  rows.forEach(r => { cost[r[W.person]] = (cost[r[W.person]] || 0) + (r[W.cost] || 0); });
  return Object.keys(cost).sort().map(person => ({
    person, cost: cost[person], days: days[person] || 0,
    perDay: days[person] ? cost[person] / days[person] : null }));
}

function outputByPerson(rows, prs) {
  const out = dict();
  const get = p => out[p] || (out[p] = { prs: 0, prCost: 0, linesAdded: null, linesRemoved: null,
    costWithLines: 0, cost: 0 });
  rows.forEach(r => {
    const o = get(r[W.person]);
    o.cost += r[W.cost] || 0;
    o.costWithLines += r[W.costWithLines] || 0;
    if (r[W.linesAdded] != null) o.linesAdded = (o.linesAdded || 0) + r[W.linesAdded];
    if (r[W.linesRemoved] != null) o.linesRemoved = (o.linesRemoved || 0) + r[W.linesRemoved];
  });
  prs.forEach(pr => {
    const o = get(pr.person);
    o.prs += 1;
    o.prCost += (pr.session_cost_usd || 0) / (pr.session_prs || 1);
  });
  Object.values(out).forEach(o => {
    const lines = (o.linesAdded || 0) + (o.linesRemoved || 0);
    o.costPerPr = o.prs ? o.prCost / o.prs : null;
    o.perKLines = o.linesAdded == null || !lines ? null : o.costWithLines / lines * 1000;
    o.lineCoverage = o.cost ? o.costWithLines / o.cost : null;
  });
  return out;
}

// Each person's latest export per machine (a source with no machine id is its
// own machine for that person), with its age in days from UTC midnight today:
// the page's badge and the stale finding both read this.
function latestExports(sources, today) {
  const t0 = Date.parse(today + 'T00:00:00Z');
  const by = dict();
  sources.forEach(s => {
    const k = s.machine_id ? s.person + '|' + s.machine_id : s.person;
    if (!by[k] || (s.exported_at || '') > (by[k].exported_at || '')) by[k] = s;
  });
  return Object.values(by).map(s => {
    const age = s.exported_at ? (t0 - Date.parse(s.exported_at)) / 864e5 : null;
    return { source: s, person: s.person, age, stale: age != null && age > FINDING.staleDays };
  });
}

// ---- range helpers: every team figure is computed for the selected weeks ----
function isoAddDays(day, n) {
  const d = new Date(day + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function mondayOfDay(day) {
  const d = new Date(day.slice(0, 10) + 'T00:00:00Z');
  return isoAddDays(day.slice(0, 10), -((d.getUTCDay() + 6) % 7));
}
// The days two inclusive date ranges share, and how many are Monday-Friday.
function overlap(aFrom, aTo, bFrom, bTo) {
  const from = aFrom > bFrom ? aFrom : bFrom, to = aTo < bTo ? aTo : bTo;
  if (!from || !to || from > to) return null;
  let days = 0, workdays = 0;
  for (let d = from; d <= to; d = isoAddDays(d, 1)) {
    days++;
    const wd = new Date(d + 'T00:00:00Z').getUTCDay();
    if (wd !== 0 && wd !== 6) workdays++;
  }
  return { from, to, days, workdays };
}
const PER_DAY = { month: 365.25 / 12, year: 365.25 };
// A person's snapshot (their export's window) counts for the range when the
// week it ends in is one of the selected weeks.
function snapshotInRange(p, range) {
  if (!p || !p.window_to) return false;
  const w = mondayOfDay(p.window_to);
  return w >= range.from && w <= range.to;
}
// Each person's coverage: the union of their own exports' windows (overlapping
// or touching ones merged), from team_payload's sources. The days between two
// exports are not covered: nothing is known about them.
function coverageIntervals(sources) {
  const by = dict();
  (sources || []).forEach(s => {
    if (!s.window_from || !s.window_to || s.window_from > s.window_to) return;
    (by[s.person] || (by[s.person] = [])).push({ from: s.window_from, to: s.window_to });
  });
  Object.keys(by).forEach(person => {
    const merged = [];
    by[person].sort((x, y) => x.from < y.from ? -1 : x.from > y.from ? 1 : 0).forEach(iv => {
      const last = merged[merged.length - 1];
      if (last && iv.from <= isoAddDays(last.to, 1)) { if (iv.to > last.to) last.to = iv.to; }
      else merged.push({ from: iv.from, to: iv.to });
    });
    by[person] = merged;
  });
  return by;
}
// The intervals to use for a person: their exports' windows, or (when none are
// known) the one window the people row summarises.
function personIntervals(p, intervals) {
  if (intervals && intervals.length) return intervals;
  return p && p.window_from && p.window_to ? [{ from: p.window_from, to: p.window_to }] : [];
}
// Whether any interval touches the week starting on `monday`.
function coveredWeek(intervals, monday) {
  const sunday = isoAddDays(monday, 6);
  return intervals.some(iv => overlap(monday, sunday, iv.from, iv.to) != null);
}
// One person's figures for the selected weeks. rows: their weekly rows in
// range; limits: their limit episodes with a hit in range; prs: their PRs
// opened in range; range: {from: first Monday, to: last Sunday}. opts:
// intervals (coverageIntervals for them), and either basis (dayBasis) or
// allRows (all their weekly rows, in any week) for the active-days bounds.
// A range that doesn't overlap any of the person's export windows says nothing
// about them: covered is false and every figure is null (missing is blank,
// never 0). Workdays and seat-cost days are the days of the range inside the
// windows, so a gap between two exports is neither counted nor paid for.
function personInRange(p, rows, limits, prs, range, prices, opts) {
  opts = opts || {};
  let calDays = 0, workdays = 0;
  personIntervals(p, opts.intervals).forEach(iv => {
    const ov = overlap(range.from, range.to, iv.from, iv.to);
    if (ov) { calDays += ov.days; workdays += ov.workdays; }
  });
  if (!calDays) return { covered: false, idle: null, sessions: null, cost: null, subCost: null, subShare: null,
    prs: null, retries: null, limitHits: null, lockouts: null, lockedHours: null, extra: null, activeDays: null,
    approx: false, activeLo: null, activeHi: null, workdays: 0, seatUse: null, seatCost: null, value: null,
    lastActiveWeek: null };
  const out = { covered: true, sessions: 0, cost: 0, subCost: 0, retries: 0, prs: prs.length, limitHits: limits.length,
    lockouts: 0, lockedHours: 0, extra: 0, activeDays: 0, approx: false, lastActiveWeek: null };
  const weeks = dict();
  rows.forEach(r => {
    out.sessions += r[W.sessionsStarted] || 0;
    out.cost += r[W.cost] || 0;
    out.subCost += r[W.subagentCost] || 0;
    out.retries += r[W.apiRetries] || 0;
    if ((r[W.cost] || 0) > 0 && (!out.lastActiveWeek || r[W.week] > out.lastActiveWeek)) out.lastActiveWeek = r[W.week];
    (weeks[r[W.week]] || (weeks[r[W.week]] = [])).push(r);
  });
  const basis = opts.basis || dayBasis(opts.allRows ? p : null, opts.allRows || rows, opts.intervals);
  const days = rangeDays(basis, Object.keys(weeks));
  out.activeDays = days.days; out.approx = days.approx; out.activeLo = days.lo; out.activeHi = days.hi;
  limits.forEach(l => {
    if (l.locked_out) { out.lockouts++; out.lockedHours += l.blocked_hours || 0; }
    if (l.limit_type === 'weekly') out.extra += l.usage_after_hit_usd || 0;
  });
  out.subShare = out.cost ? out.subCost / out.cost : null;
  out.idle = !(out.cost > 0);
  out.workdays = workdays;
  out.seatUse = workdays ? Math.min(out.activeDays / workdays, 1) : null;
  const per = prices && PER_DAY[prices.per];
  out.seatCost = per && p.seat_price != null ? p.seat_price / per * calDays : null;
  const paid = out.seatCost == null ? null : out.seatCost + out.extra;
  out.value = paid ? out.cost / paid : null;
  return out;
}

// Value per seat for the team: everyone's usage over everyone's seat cost
// plus extra usage, from personInRange results. Only people with a seat cost
// in the range (priced, and covered by it) count, on both sides, so an
// unpriced seat's usage isn't set against nothing. null when nobody has one.
function teamValue(ranged) {
  let cost = 0, paid = 0;
  ranged.forEach(r => { if (r.seatCost != null) { cost += r.cost || 0; paid += r.seatCost + (r.extra || 0); } });
  return paid ? cost / paid : null;
}

// Extra usage per selected week and its run-rate, by the rule of the server's
// team.overage_forecast but for the selected people and weeks. limits: the
// limit episodes already filtered to them; weeks: the selected Mondays;
// coverageTo: the latest export end among the selected people (null when none);
// today: "YYYY-MM-DD"; coverageFrom: the Monday of the earliest export start
// among them (null for no lower bound). A week before the exports begin is
// unknown, not $0: it is left out of the series and never complete. A week is complete when its Sunday is inside the exports
// (coverageTo) and before today; the basis is the last 4 complete weeks, so a
// part-seen week or a week after the exports isn't averaged in. `episodes` are
// the weekly-limit hits with extra usage inside the basis weeks (the ones the
// rate is made of). Weeks are placed by the UTC date of hit_at.
function rangeRunRate(limits, weeks, coverageTo, today, coverageFrom = null, basisWeeks = 4) {
  if (coverageFrom != null) weeks = weeks.filter(w => w >= coverageFrom);
  const perWeek = dict();
  weeks.forEach(w => { perWeek[w] = 0; });
  const weekly = limits.filter(l => l.limit_type === 'weekly' && l.hit_at);
  weekly.forEach(l => {
    const w = mondayOfDay(l.hit_at);
    if (w in perWeek) perWeek[w] += l.usage_after_hit_usd || 0;
  });
  const complete = coverageTo == null ? [] : weeks.filter(w => {
    const sunday = isoAddDays(w, 6);
    return sunday <= coverageTo && sunday < today;
  });
  const basis = complete.slice(-basisWeeks);
  const inBasis = new Set(basis);
  const episodes = weekly.filter(l => (l.usage_after_hit_usd || 0) > 0 && inBasis.has(mondayOfDay(l.hit_at))).length;
  return { weekly: weeks.map(w => [w, perWeek[w]]), basisWeeks: basis.length,
    rate: basis.length ? basis.reduce((s, w) => s + perWeek[w], 0) / basis.length : null, episodes };
}

// input: {people, weekly, limits, tools, health, sources}, already filtered to
// the selected people and weeks. today: "YYYY-MM-DD". Returns findings in the
// spec's order; text is plain (the caller escapes).
function findings(input, today) {
  const nameOf = dict();
  (input.people || []).forEach(p => { nameOf[p.person] = displayName(p); });
  const name = person => nameOf[person] || person;
  const list = names => names.length <= 3 ? names.join(', ') : `${names.slice(0, 3).join(', ')} and ${names.length - 3} more`;
  const money = n => '$' + Math.round(n).toLocaleString('en-US');
  const pct = share => `${Math.round(share * 100)}%`;
  const lockedFor = h => {
    if (h >= 1) return h.toFixed(1) + ' h';
    const minutes = Math.round(h * 60);
    return minutes < 1 ? 'under a minute' : minutes + ' min';
  };
  const out = [];

  // One finding for everyone locked out, worst first, so a bad week for many
  // people can't fill the page's six slots by itself.
  const locked = dict();
  (input.limits || []).filter(l => l.locked_out).forEach(l => {
    locked[l.person] = (locked[l.person] || 0) + (l.blocked_hours || 0);
  });
  const lockedOut = Object.entries(locked).sort((a, b) => b[1] - a[1]);
  if (lockedOut.length) out.push({ rule: 'lockout', tab: 'seats',
    text: `${list(lockedOut.map(([p, h]) => `${name(p)} (${lockedFor(h)})`))} ` +
          `${lockedOut.length === 1 ? 'was' : 'were'} locked out until a limit reset.` });

  // A snapshot describes the week of the export, so it only counts when that
  // week is selected (snapshot_in_range, from snapshotInRange).
  const pace = (input.people || []).filter(p => p.snapshot_in_range &&
    (p.allowance_status === 'on_pace_to_hit' || p.allowance_status === 'over'));
  if (pace.length) out.push({ rule: 'on_pace', tab: 'seats',
    text: `${list(pace.map(displayName))} ${pace.length === 1 ? 'is' : 'are'} on pace to hit or over the weekly limit (as of their export).` });

  const spend = repoSpend(input.weekly || [], Infinity);
  const named = spend.repos.filter(r => r.repo !== NO_PROJECT);
  if (named.length >= 2 && named[0].share >= FINDING.concentration) {
    const top = named[0];
    out.push({ rule: 'concentration', tab: 'spend',
      text: `${Math.round(top.share * 100)}% of spend (${money(top.cost)} of ${money(spend.total)}) is in ${top.repo}.` });
  }

  // Only MCP servers: a built-in tool's errors (a failing test run, a glob
  // with no match) aren't an integration a manager can fix. They stay in the
  // Friction table.
  const tools = dict();
  (input.tools || []).forEach(([, , tool, integration, calls, errors]) => {
    if (integration === 'built-in') return;
    const key = integrationLabel(integration, tool);
    const t = tools[key] || (tools[key] = { calls: 0, errors: 0 });
    t.calls += calls; t.errors += errors;
  });
  Object.entries(tools).filter(([, t]) => t.calls >= FINDING.minCalls && t.errors / t.calls >= FINDING.errorRate)
    .sort((a, b) => b[1].errors / b[1].calls - a[1].errors / a[1].calls)
    .forEach(([key, t]) => out.push({ rule: 'integration', tab: 'habits',
      text: `${key} fails ${Math.round(t.errors / t.calls * 100)}% of the time (${t.errors} of ${t.calls} calls).` }));

  const high = perActiveDay(input.weekly || [], input.dayBasis).filter(d => d.perDay != null && d.perDay > FINDING.perDay);
  if (high.length) out.push({ rule: 'per_day', tab: 'spend',
    text: `${list(high.map(d => `${name(d.person)} (${money(d.perDay)}/day)`))} spent more per active day than ` +
          `Anthropic's 90th percentile of $${BENCHMARK.p90} (list price).` });

  const ctx = dict();
  (input.health || []).forEach(r => {
    if (r.very_long_context_cost_usd == null) return;
    const c = ctx[r.person] || (ctx[r.person] = { long: 0, main: 0 });
    c.long += r.very_long_context_cost_usd; c.main += r.est_cost_main_usd || 0;
  });
  const heavy = Object.entries(ctx).filter(([, c]) => c.main > 0 && c.long / c.main >= FINDING.contextShare);
  if (heavy.length) out.push({ rule: 'context', tab: 'habits',
    text: `${list(heavy.map(([p, c]) => `${name(p)} (${Math.round(c.long / c.main * 100)}%)`))} spent ${pct(FINDING.contextShare)} or more ` +
          `of their main conversation on requests over ${Math.round(FINDING.contextTokens / 1000)}k tokens.` });

  const idle = (input.people || []).filter(p => p.idle_in_range);
  if (idle.length) out.push({ rule: 'idle', tab: 'seats',
    text: `${list(idle.map(displayName))} ${idle.length === 1 ? 'has' : 'have'} had no Claude Code activity in these weeks.` });

  // Any machine whose latest export is old, naming the person once.
  const stale = [];
  latestExports(input.sources || [], today).forEach(g => {
    if (g.stale && !stale.includes(g.person)) stale.push(g.person);
  });
  if (stale.length) out.push({ rule: 'stale', tab: 'overview',
    text: `${list(stale.map(name))}: a machine's latest export is more than ${FINDING.staleDays} days old; ` +
          `its recent work isn't here.` });
  return out;
}
