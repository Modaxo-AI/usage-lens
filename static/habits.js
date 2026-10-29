// ===================== Habits (usage health) =====================
// Reads PAYLOAD.health (claude_usage/health.py); the research behind each
// measure is in docs/usage-health.md.
//
// Loaded before app.js and uses its helpers (state, filters, formatters,
// upsertChart) only when called, so nothing here runs at load time.
//
// Rules the panel keeps:
//  - no composite score: each habit is priced or counted on its own;
//  - a status (healthy / worth a look) only where a sourced line exists --
//    Anthropic's 10%-of-spend rule for long context and cache misses, and its
//    $13 / $30 per-active-day spend benchmark. Everything else is a profile;
//  - a measure the user's Claude Code version doesn't record says so, rather
//    than showing zero.

const MISS_LABELS = {
  previous_message_not_found: 'Cache expired (idle)',
  messages_changed: 'Earlier messages changed',
  system_changed: 'System prompt changed',
  model_changed: 'Model switched mid-session',
  tools_changed: 'Tools or MCP servers changed',
  unavailable: 'Reason not given',
};
const DENIAL_LABELS = {
  'user-rejected': 'You rejected it',
  'automode-blocked': 'Auto mode blocked it',
  'automode-unavailable': 'Auto mode unavailable',
  'permission-rule': 'A permission rule',
  'interrupted': 'Interrupted',
};
const MODE_LABELS = {
  auto: 'Auto', acceptEdits: 'Accept edits', default: 'Ask (default)', normal: 'Ask (default)',
  plan: 'Plan', bypassPermissions: 'Bypass permissions', dontAsk: "Don't ask",
  unknown: 'Not recorded',
};
const EXPIRED_MISS = 'previous_message_not_found';

function habitsAvailable() { return !!(PAYLOAD && PAYLOAD.health); }

// ---------- windows and rows ---------------------------------------------- //

// The strip compares the selected range with an equal run of days just before
// it (a 10-day range against the 10 days before its start), so moving or
// resizing the range moves the comparison with it. The project and model
// filters apply as well.
function habitWindows() {
  const from = state.from, to = state.to;
  const n = daysBetween(from, to);
  return {
    recent: { from, to },
    baseline: { from: addDays(from, -n), to: addDays(from, -1) },
  };
}
// The Habits tab uses the dashboard's own date range.
function filterWindow() { return { from: state.from, to: state.to }; }

function inWindow(key, w) {
  const d = dayOf(key);
  return !!d && d >= w.from && d <= w.to;
}
// Context and cache-miss rows carry a model; the rest belong to a session and
// follow the same model rule as tools and PRs (sessions that used the model).
const HEALTH_MODEL_COLUMN = { context: true, cache_misses: true };
function healthRows(name, w) {
  const rows = (PAYLOAD.health && PAYLOAD.health[name]) || [];
  const byModel = HEALTH_MODEL_COLUMN[name];
  return rows.filter(r => inWindow(r[0], w) && projectOk(projectOf(r[1])) &&
    (byModel ? modelOk(r[2]) : modelsOk(modelsOfSession(r[1]))));
}
// Sessions that ended inside the window, originals only (a fork copy is a
// prefix of a session already listed).
function habitSessions(w) {
  return PAYLOAD.sessions.filter(s => !s.derived && s.health &&
    inWindow(sessionEndDay(s) || sessionStartDay(s), w) && projectOk(s.project) && modelsOk(s.models));
}

function longBand() {
  const t = PAYLOAD.health.thresholds;
  return t.context_bands.indexOf(t.long_context_tokens) + 1;
}

// Context tax is flagged on spend over flag_context_tokens (500k); the 200k
// share is shown for context. Older payloads have no flag line: use 200k.
function flagBand() {
  const t = PAYLOAD.health.thresholds;
  const flag = t.flag_context_tokens || t.long_context_tokens;
  return t.context_bands.indexOf(flag) + 1;
}

// ---------- the measures --------------------------------------------------- //

function habitMeasures(w) {
  const h = PAYLOAD.health;
  const usage = PAYLOAD.usage.filter(r => inWindow(r[0], w) && projectOk(projectOf(r[1])) && modelOk(r[2]));
  const spend = usage.reduce((s, r) => s + r[13], 0);
  const requests = usage.reduce((s, r) => s + r[12], 0);
  const activeDays = new Set(usage.filter(r => r[13] > 0).map(r => dayOf(r[0]))).size;

  const band = longBand();
  const veryBand = flagBand();
  let mainCost = 0, longCost = 0, veryLongCost = 0;
  healthRows('context', w).forEach(r => {
    mainCost += r[5];
    if (r[3] >= band) longCost += r[5];
    if (r[3] >= veryBand) veryLongCost += r[5];
  });

  let coldCost = 0, missCost = 0;
  const missByReason = {};
  healthRows('cache_misses', w).forEach(r => {
    missCost += r[5];
    if (r[3] === EXPIRED_MISS) coldCost += r[5];
    missByReason[r[3]] = (missByReason[r[3]] || 0) + r[5];
  });

  const ev = { prompt: 0, interrupt: 0, queued: 0, away: 0, auto: 0, manual: 0 };
  const denials = {}, modes = {};
  healthRows('events', w).forEach(([, , kind, value, n]) => {
    if (kind === 'compact') ev[value === 'manual' ? 'manual' : 'auto'] += n;
    else if (kind === 'denial') denials[value] = (denials[value] || 0) + n;
    else if (kind in ev) ev[kind] += n;
    if (kind === 'prompt') modes[value || 'unknown'] = (modes[value || 'unknown'] || 0) + n;
  });

  const checks = {};
  healthRows('checks', w).forEach(r => { checks[r[2]] = (checks[r[2]] || 0) + r[3]; });
  let streaks = 0;
  healthRows('error_streaks', w).forEach(r => { if (r[2] >= h.thresholds.error_streak) streaks += r[3]; });

  let toolCalls = 0, toolErrors = 0;
  const byTool = {};
  PAYLOAD.tools.filter(r => inWindow(r[0], w) && sessionOk(r[1])).forEach(r => {
    toolCalls += r[3]; toolErrors += r[4];
    const t = byTool[r[2]] = byTool[r[2]] || { calls: 0, errors: 0 };
    t.calls += r[3]; t.errors += r[4];
  });
  const worstTool = Object.entries(byTool)
    .filter(([, t]) => t.calls >= 10 && t.errors > 0)
    .map(([name, t]) => ({ name, rate: t.errors / t.calls, calls: t.calls }))
    .sort((a, b) => b.rate - a.rate)[0] || null;

  const sessions = habitSessions(w);
  const edited = sessions.filter(s => s.health.edits > 0);
  const withInstr = sessions.filter(s => s.health.instructions !== null);
  return {
    w, spend, requests, activeDays, mainCost, longCost, veryLongCost, coldCost, missCost, missByReason,
    ev, denials, modes, checks, streaks, toolCalls, toolErrors, worstTool,
    sessions,
    edited: edited.length,
    checked: edited.filter(s => s.health.checked_after_last_edit === true).length,
    withSubagents: sessions.filter(s => s.n_subagents > 0).length,
    withPlan: sessions.filter(s => s.health.plan_mode).length,
    instrRecorded: withInstr.length,
    withProjectMd: withInstr.filter(s => (s.health.instructions || []).includes('Project')).length,
    denialsRecorded: sessions.some(s => s.health.recorded && s.health.recorded.denials),
    compactionsRecorded: sessions.some(s => s.health.recorded && s.health.recorded.compactions),
    // Context tax is a share of main-conversation spend (docs/usage-health.md
    // §3.1), as in the session tags, the export and the team view: subagents
    // carry their own, smaller contexts.
    longShare: mainCost > 0 ? longCost / mainCost : null,
    veryLongShare: mainCost > 0 ? veryLongCost / mainCost : null,
    coldShare: spend > 0 ? coldCost / spend : null,
    costPerDay: activeDays ? spend / activeDays : null,
  };
}

function ratio(a, b) { return b > 0 ? a / b : null; }

// ---------- statuses ------------------------------------------------------- //

// Anthropic: /usage flags long context or cache misses once either is 10% or
// more of recent usage.
function shareStatus(share) {
  if (share == null) return null;
  return share >= PAYLOAD.health.thresholds.flag_share ? 'look' : 'ok';
}
// Anthropic: about $13 per developer per active day on average, under $30 for
// 90% of users. Above the 90th percentile is worth a look; it is not a verdict.
function spendStatus(perDay) {
  if (perDay == null) return null;
  const b = PAYLOAD.health.benchmarks.cost_per_active_day;
  return perDay > b.p90 ? 'look' : (perDay <= b.average ? 'ok' : 'typical');
}
function statusBadge(status) {
  if (status === 'look') return '<span class="badge warn habit-status">! Worth a look</span>';
  if (status === 'ok') return '<span class="badge ok habit-status">&#10003; Healthy</span>';
  if (status === 'typical') return '<span class="badge habit-status">Typical range</span>';
  return '';
}

// ---------- the Overview strip -------------------------------------------- //

function habitCards(r, b) {
  const h = PAYLOAD.health;
  const src = h.sources;
  const bench = h.benchmarks.cost_per_active_day;
  const pctVs = (now, base) => base == null ? 'no baseline yet' : `baseline ${fmtPct(base)}`;
  const n = r.sessions.length;
  const longK = Math.round(h.thresholds.long_context_tokens / 1000);
  const flagK = Math.round((h.thresholds.flag_context_tokens || h.thresholds.long_context_tokens) / 1000);
  const cards = [];

  cards.push({
    label: 'Context tax',
    status: shareStatus(r.veryLongShare),
    value: r.mainCost > 0 ? `${fmtPct(r.veryLongShare)} <span class="unit">of main spend</span>` : '—',
    sub: `${fmtCost(r.veryLongCost)} of ${fmtCost(r.mainCost)} on requests over ${flagK}k tokens ` +
         `(${fmtPct(r.longShare)} over ${longK}k) · ${pctVs(r.veryLongShare, b.veryLongShare)}` +
         (r.ev.auto ? ` · ${fmtInt(r.ev.auto)} auto-compaction${r.ev.auto === 1 ? '' : 's'}` : ''),
    fix: '<code>/clear</code> between unrelated tasks; for big work, write a spec and start a fresh session.',
    link: src.best_practices,
    tip: `Share of main-conversation spend on requests carrying more than ${flagK}k tokens of context ` +
         `(subagents excluded: each starts with a fresh, smaller context). Anthropic's /usage ` +
         `flags long context once it is 10% or more of recent usage. The flag is at ${flagK}k; the ` +
         `${longK}k share is shown for context. Both lines are this tool's choice, not Anthropic's.`,
  });
  cards.push({
    label: 'Cold-cache tax',
    status: shareStatus(r.coldShare),
    value: r.spend > 0 ? `${fmtPct(r.coldShare)} <span class="unit">of spend</span>` : '—',
    sub: `${fmtCost(r.coldCost)} re-caching after the cache expired · ${pctVs(r.coldShare, b.coldShare)}`,
    fix: 'After a long break, start fresh (or resume from a summary) instead of reopening a big session.',
    link: src.costs,
    tip: "Extra paid to rebuild the prompt cache when a session was picked up after its cache expired, " +
         "by Claude Code's own cache-miss label, over reading the same tokens from a warm cache. " +
         "Anthropic's /usage flags cache misses at 10% or more of recent usage; this card flags only " +
         "the expiry misses (previous_message_not_found), not misses with another reason.",
  });
  cards.push({
    label: 'Spend per active day',
    status: spendStatus(r.costPerDay),
    value: r.costPerDay == null ? '—' : fmtCost(r.costPerDay),
    sub: `Anthropic: ~${fmtCost0(bench.average)} average, under ${fmtCost0(bench.p90)} for 90% · ` +
         (b.costPerDay == null ? 'no baseline yet' : `baseline ${fmtCost(b.costPerDay)}`),
    fix: 'High spend usually traces to long uncleared sessions or the top model left on for everything.',
    link: src.costs,
    tip: 'List-price equivalent per day with any usage. On a seat plan this is not your bill; it shows ' +
         'how hard you draw on the allowance.',
  });
  cards.push({
    label: 'Checking work',
    status: null,
    value: r.edited ? `${fmtInt(r.checked)} <span class="unit">of ${fmtInt(r.edited)} sessions</span>` : '—',
    sub: (r.edited ? 'ran a test, lint or build after their last code edit' : 'no sessions edited code') +
         ` · ${pctVs(null, ratio(b.checked, b.edited))}`,
    fix: 'Give Claude a check it can run: tests, a build, a linter.',
    link: src.best_practices,
    tip: "Sessions that edited files and then ran a test, lint or build. Anthropic's research finds " +
         'verified success (passing tests, commits) is about twice as common for experienced users.',
  });
  cards.push({
    label: 'Friction',
    status: null,
    value: r.toolCalls ? `${fmtPct(ratio(r.toolErrors, r.toolCalls))} <span class="unit">tool errors</span>` : '—',
    sub: (r.streaks ? `${fmtInt(r.streaks)} run${r.streaks === 1 ? '' : 's'} of ${PAYLOAD.health.thresholds.error_streak}+ errors in a row` : 'no long error runs') +
         (r.worstTool && r.worstTool.rate >= 0.1 ? ` · ${esc(r.worstTool.name)} fails ${fmtPct(r.worstTool.rate)}` : '') +
         ` · ${pctVs(null, ratio(b.toolErrors, b.toolCalls))}`,
    fix: 'A tool that keeps failing is usually an integration or permission to fix, not a habit.',
    link: null,
    tip: 'Tool calls that returned an error, runs of consecutive failures, and the worst tool with at ' +
         'least 10 calls.',
  });
  cards.push({
    label: 'Workflow',
    status: null,
    value: n ? `${fmtInt(r.withSubagents)} <span class="unit">of ${fmtInt(n)} used subagents</span>` : '—',
    sub: (n ? `plan mode ${fmtInt(r.withPlan)} · project CLAUDE.md ` +
              (r.instrRecorded ? `${fmtInt(r.withProjectMd)} of ${fmtInt(r.instrRecorded)}` : 'not recorded') : 'no sessions') +
         ` · subagents ${pctVs(null, ratio(b.withSubagents, b.sessions.length))}`,
    fix: 'Plan mode for multi-file or unfamiliar work; subagents for research; a short CLAUDE.md.',
    link: src.best_practices,
    tip: 'Sessions that ended in the window. Whether a CLAUDE.md was loaded is only recorded by newer ' +
         'Claude Code versions.',
  });
  return cards;
}

function renderHabitStrip() {
  const card = document.getElementById('habitsCard');
  if (!card) return;
  if (!habitsAvailable()) { card.hidden = true; return; }
  card.hidden = false;
  const { recent, baseline } = habitWindows();
  const r = habitMeasures(recent), b = habitMeasures(baseline);
  // A baseline with no spend can't be compared against.
  if (!b.spend) { b.longShare = b.veryLongShare = b.coldShare = b.costPerDay = null; }
  const cards = habitCards(r, b);
  const flagged = cards.filter(c => c.status === 'look').length;

  const nDays = daysBetween(recent.from, recent.to);
  document.getElementById('habitsWindow').textContent =
    `${prettyDate(recent.from)} \u2013 ${prettyDate(recent.to)}, against the ` +
    `${nDays === 1 ? '1 day' : nDays + ' days'} before` +
    (r.spend ? (flagged ? ` · ${flagged} habit${flagged === 1 ? '' : 's'} worth a look` : ' · nothing flagged') : '');
  const strip = document.getElementById('habitsStrip');
  if (!r.spend && !r.sessions.length) {
    strip.innerHTML = `<div class="empty-state"><div class="es-title">No activity in these dates</div>` +
      `<div class="es-body">Widen the date range, or move it, to see a period with activity.</div></div>`;
    return;
  }
  strip.innerHTML = cards.map(c =>
    `<div class="habit${c.status === 'look' ? ' flagged' : ''}" title="${esc(c.tip)}">` +
      `<div class="habit-top"><span class="label">${esc(c.label)}</span>${statusBadge(c.status)}</div>` +
      `<div class="value">${c.value}</div>` +
      `<div class="sub">${c.sub}</div>` +
      `<div class="fix">${c.fix}${c.link ? ` <a href="${safeUrl(c.link)}" target="_blank" rel="noopener">Why</a>` : ''}</div>` +
    `</div>`).join('');
}

// ---------- session tags (Sessions table and recent-session cards) -------- //

// [text, tooltip, level]; level 'warn' for the habits with a cost attached.
function sessionHabitTags(s) {
  const h = s.health;
  if (!h) return [];
  const t = PAYLOAD.health.thresholds;
  const flagTokens = t.flag_context_tokens || t.long_context_tokens;
  const flagK = Math.round(flagTokens / 1000);
  // Sessions from a payload without the 500k measure fall back to the 200k share.
  const share = h.very_long_context_share !== undefined ? h.very_long_context_share : h.long_context_share;
  const tags = [];
  if (share != null && share >= t.flag_share && h.peak_context > flagTokens) {
    tags.push([`${fmtPct(share)} over ${flagK}k`,
      `${fmtPct(share)} of this session's main-conversation cost was on requests over ${flagK}k tokens; ` +
      `peak ${fmtTokens(h.peak_context)}.`, 'warn']);
  }
  if (h.compactions_auto) {
    tags.push([`auto-compacted ×${h.compactions_auto}`, 'The context filled up and Claude Code summarised it automatically.', '']);
  }
  if (h.cold_cache_cost >= 0.5) {
    tags.push([`cold cache ${fmtCost(h.cold_cache_cost)}`,
      'Extra paid to rebuild the cache after it expired between requests.', 'warn']);
  }
  if (h.max_error_streak >= t.error_streak) {
    tags.push([`${h.max_error_streak} errors in a row`, 'The longest run of consecutive failed tool calls.', '']);
  }
  if (h.checked_after_last_edit === false) {
    tags.push(['unchecked edits', 'Edited files without running a test, lint or build afterwards.', '']);
  }
  if (h.ended_on_interrupt) {
    tags.push(['ended on interrupt', 'The last thing in the session was you stopping Claude.', '']);
  }
  return tags;
}
function tagsHtml(tags) {
  return tags.map(([text, tip, level]) =>
    `<span class="badge${level ? ' ' + level : ''}" title="${esc(tip)}">${esc(text)}</span>`).join(' ');
}

// ---------- the Habits tab ------------------------------------------------ //

function renderHabits() {
  const panel = document.getElementById('tab-habits');
  if (!habitsAvailable()) {
    panel.querySelector('.habits-body').hidden = true;
    return;
  }
  const w = filterWindow();
  const m = habitMeasures(w);
  const h = PAYLOAD.health;
  const prompts = m.ev.prompt;

  const promptsPerSession = median(m.sessions.map(s => s.health.prompts).filter(n => n > 0).sort((a, b) => a - b));
  renderTiles('habitTiles', [
    ['Prompts', fmtInt(prompts), `${fmtInt(m.ev.queued)} typed while Claude was busy`],
    ['Prompts per session', promptsPerSession == null ? '—' : fmtInt(promptsPerSession), 'median'],
    ['Requests per prompt', prompts ? (m.requests / prompts).toFixed(1) : '—',
     'Anthropic: ~5 actions for novices, ~12 for experts',
     'API requests (main and subagent) per prompt you sent. Anthropic counts actions, which differs, ' +
     'so read it as a profile, not a target. Bigger delegation goes with expertise.'],
    ['Interrupts', prompts ? `${fmtInt(m.ev.interrupt)}` : '—',
     prompts ? `${(100 * m.ev.interrupt / prompts).toFixed(1)} per 100 prompts` : null,
     'Experienced users interrupt more, not less (Anthropic, 2026): this is a profile, not a score.'],
    ['Compactions', m.compactionsRecorded ? fmtInt(m.ev.auto + m.ev.manual) : '—',
     m.compactionsRecorded ? `${fmtInt(m.ev.auto)} automatic · ${fmtInt(m.ev.manual)} manual`
                           : 'not recorded by this Claude Code version'],
    ['Refused tool calls', m.denialsRecorded ? fmtInt(Object.values(m.denials).reduce((a, b) => a + b, 0)) : '—',
     m.denialsRecorded ? 'by you, auto mode or a rule' : 'not recorded by this Claude Code version'],
  ]);

  renderContextChart(w);
  renderMissChart(m);
  renderCheckTiles(m);
  renderBarChart('habitDenials', 'habitDenialChart', m.denials, DENIAL_LABELS, fmtInt);
  renderBarChart('habitModes', 'habitModeChart', m.modes, MODE_LABELS, fmtInt);
  renderRecentSessions();
}

function bandLabels() {
  const edges = PAYLOAD.health.thresholds.context_bands.map(e => Math.round(e / 1000) + 'k');
  return [`under ${edges[0]}`, ...edges.slice(1).map((e, i) => `${edges[i]}–${e}`), `${edges[edges.length - 1]}+`];
}

function renderContextChart(w) {
  // Health rows are hourly, so a finer grain would pile each hour into its
  // first bar. Below an hour this chart stays at an hour, and says so.
  const finest = PAYLOAD.health.bucket_minutes;
  const clamped = typeof state.grain === 'number' && state.grain < finest;
  const grain = clamped ? finest : state.grain;
  document.getElementById('grainNoteHabits').textContent = 'per ' + grainInfo(grain).noun +
    (clamped ? ' (the finest this chart goes)' : '');
  const labels = bandLabels();
  const buckets = {};
  healthRows('context', w).forEach(r => {
    const k = rollup(r[0], grain);
    buckets[k] = buckets[k] || new Array(labels.length).fill(0);
    buckets[k][r[3]] += r[5];
  });
  const axis = axisFor(grain);
  // Ordered magnitude: one hue, faint (small context) to full (large). The
  // ramp starts a quarter in so the smallest band stays visible on either surface.
  const colors = labels.map((_, i) => seqColor(labels.length > 1 ? 0.25 + 0.75 * i / (labels.length - 1) : 1));
  const datasets = labels.map((label, i) => ({
    label, backgroundColor: colors[i], stack: 'ctx', borderColor: cssVar('--surface-1'), borderWidth: 1,
    data: axis.keys.map(k => (buckets[k] || [])[i] || 0),
  }));
  upsertChart('habitContext', 'habitContextChart', {
    type: 'bar',
    data: { labels: axis.labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { stacked: true, grid: { color: cssVar('--gridline') }, ticks: { color: cssVar('--text-muted') } },
        y: { stacked: true, grid: { color: cssVar('--gridline') }, ticks: { color: cssVar('--text-muted'), callback: v => fmtCost(v) } },
      },
      plugins: { legend: { display: false },
        tooltip: { callbacks: {
          title: ctx => axis.titles[ctx[0].dataIndex],
          label: ctx => ctx.raw ? `${ctx.dataset.label} tokens: ${fmtCost(ctx.raw)}` : null,
        } } },
    },
  });
  document.getElementById('habitContextLegend').innerHTML = labels.map((l, i) =>
    `<span><span class="swatch" style="background:${colors[i]}"></span>${esc(l)} tokens</span>`).join('');
}

// One series, so one colour and no legend box: the title names it.
function renderBarChart(key, canvasId, totals, labelMap, fmt) {
  const entries = Object.entries(totals).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  upsertChart(key, canvasId, {
    type: 'bar',
    data: {
      labels: entries.map(([k]) => labelMap[k] || k),
      datasets: [{ data: entries.map(([, v]) => v), backgroundColor: cssVar('--series-solo'),
                   borderRadius: 4, maxBarThickness: 22 }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      scales: {
        // Counts are whole numbers: without precision 0 a small axis reads 0, 1, 1, 2, 2.
        x: { grid: { color: cssVar('--gridline') },
             ticks: { color: cssVar('--text-muted'), callback: v => fmt(v), ...(fmt === fmtInt ? { precision: 0 } : {}) } },
        y: { grid: { display: false }, ticks: { color: cssVar('--text-secondary') } },
      },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => fmt(ctx.raw) } } },
    },
  });
}

function renderMissChart(m) {
  renderBarChart('habitMisses', 'habitMissChart', m.missByReason, MISS_LABELS, fmtCost);
  document.getElementById('habitMissNote').textContent = m.spend > 0
    ? `${fmtCost(m.missCost)} in total, ${fmtPct(m.missCost / m.spend)} of spend in this range.`
    : '';
}

function renderCheckTiles(m) {
  const c = m.checks;
  renderTiles('habitCheckTiles', [
    ['Checked after last edit', m.edited ? `${fmtInt(m.checked)} of ${fmtInt(m.edited)}` : '—',
     'sessions that edited files'],
    ['Test runs', fmtInt(c.test || 0)],
    ['Lint & type checks', fmtInt(c.lint || 0)],
    ['Builds', fmtInt(c.build || 0)],
    ['Commits', fmtInt(c.commit || 0)],
    ['PRs opened', fmtInt(c.pr || 0)],
  ]);
}

function renderRecentSessions() {
  const el = document.getElementById('habitRecent');
  const rows = sessionRows().filter(s => !s.derived && s.health)
    .sort((a, b) => String(b.end || b.start).localeCompare(String(a.end || a.start)))
    .slice(0, 6);
  if (!rows.length) {
    el.innerHTML = `<div class="empty-state"><div class="es-title">No sessions</div>` +
      `<div class="es-body">No sessions match the current filters.</div></div>`;
    return;
  }
  el.innerHTML = rows.map(s => {
    const tags = sessionHabitTags(s);
    const cost = s.est_cost_main + s.est_cost_sub;
    return `<div class="recent-session">` +
      `<div class="rs-title" title="${esc(s.title)}">${esc(s.title)}</div>` +
      `<div class="rs-meta">${esc(shortProjectLabel(s.project))} · ${esc(s.end ? new Date(s.end).toLocaleString() : '—')} · ${fmtCost(cost)}</div>` +
      `<div class="rs-tags">${tags.length ? tagsHtml(tags) : '<span class="badge ok">&#10003; nothing flagged</span>'}</div>` +
      `</div>`;
  }).join('');
}
