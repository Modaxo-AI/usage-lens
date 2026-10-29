// ===================== Habits (usage health) =====================
// Reads PAYLOAD.health (claude_usage/health.py); the research behind each
// measure is in docs/usage-health.md.
//
// Loaded before app.js and uses its helpers (state, filters, formatters,
// upsertChart) only when called, so nothing here runs at load time.
//
// Rules the panel keeps:
//  - no composite score: each habit is priced or counted on its own;
//  - a status only where a sourced line exists -- Anthropic's /usage 10% line,
//    which this tool applies to its own measures of long context and cache
//    expiry (so it is the tool's reading, not Anthropic's verdict), and
//    Anthropic's $13 average / $30 90th-percentile spend per active day, shown
//    as where you sit against them, never as a target. Everything else is a
//    profile;
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

// Claude Code's built-in tools, by name (code.claude.com/docs/en/tools-reference,
// checked 2026-10-29), plus the older names it has renamed.
const BUILTIN_TOOLS = new Set([
  'Agent', 'Artifact', 'AskUserQuestion', 'Bash', 'CronCreate', 'CronDelete', 'CronList', 'Edit',
  'EndConversation', 'EnterPlanMode', 'EnterWorktree', 'ExitPlanMode', 'ExitWorktree', 'Glob', 'Grep',
  'LSP', 'ListAgents', 'ListMcpResourcesTool', 'Monitor', 'NotebookEdit', 'PowerShell', 'PushNotification',
  'Read', 'ReadMcpResourceTool', 'RemoteTrigger', 'ReportFindings', 'ScheduleWakeup', 'SendFeedback',
  'SendMessage', 'SendUserFile', 'ShareOnboardingGuide', 'Skill', 'SubagentHandback', 'TaskCreate',
  'TaskGet', 'TaskList', 'TaskOutput', 'TaskStop', 'TaskUpdate', 'TodoWrite', 'ToolSearch',
  'WaitForMcpServers', 'WebFetch', 'WebSearch', 'Workflow', 'Write',
  // renamed or retired
  'Task', 'MultiEdit', 'LS', 'NotebookRead', 'TodoRead', 'KillShell', 'BashOutput', 'exit_plan_mode',
]);
function isMcpTool(name) { return String(name).startsWith('mcp__'); }
function mcpServerRaw(name) { return String(name).split('__')[1] || '(mcp)'; }

// A tool name that isn't a Claude Code built-in and never succeeded: made up by
// the model (a lowercase `bash`), or not available in that session. Nothing there
// can be fixed. Decided by name, so a real tool that fails every time (WebSearch
// when it isn't permitted) stays in the ranking, and so does any MCP tool.
// Used wherever tools are ranked by their errors (here and app.js).
function inventedTool(name, t) {
  return !isMcpTool(name) && !BUILTIN_TOOLS.has(name) && t.calls > 0 && t.errors >= t.calls;
}
// An MCP tool name that never succeeded while another tool on the same server
// did: a misspelt or retired tool name, not a failing server. Listed apart and
// kept out of the server's error rate. byTool: {name: {calls, errors}}.
function strayMcpTools(byTool) {
  const worked = new Set();
  Object.entries(byTool).forEach(([n, t]) => { if (isMcpTool(n) && t.calls > t.errors) worked.add(mcpServerRaw(n)); });
  return new Set(Object.entries(byTool)
    .filter(([n, t]) => isMcpTool(n) && t.calls > 0 && t.errors >= t.calls && worked.has(mcpServerRaw(n)))
    .map(([n]) => n));
}

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
// A transcript that made no requests (opened and closed, or a stub) is not a
// session: the rest of the tool leaves it out of the session count. A payload
// row without the field is taken as a session.
function didWork(s) { return s.requests == null || s.requests > 0; }
// Sessions that ended inside the window, originals only (a fork copy is a
// prefix of a session already listed).
function habitSessions(w) {
  return PAYLOAD.sessions.filter(s => !s.derived && s.health && didWork(s) &&
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

  let toolCalls = 0, toolErrors = 0, mainToolCalls = 0;
  const byTool = {};
  // Tool rows follow the project filter and, where the row says which model made
  // the call, the model filter too (toolModelOk, app.js).
  PAYLOAD.tools.filter(r => inWindow(r[0], w) && projectOk(projectOf(r[1])) && toolModelOk(r)).forEach(r => {
    toolCalls += r[3]; toolErrors += r[4];
    if (!r[5]) mainToolCalls += r[3];
    const t = byTool[r[2]] = byTool[r[2]] || { calls: 0, errors: 0 };
    t.calls += r[3]; t.errors += r[4];
  });
  // The tool behind the most errors. A name that isn't a built-in and never once
  // succeeded is one the model made up (a lowercase `bash`), not a tool to fix,
  // and an MCP tool name that never worked on a server whose other tools did is
  // a stray name rather than a failing integration: neither is a candidate. A
  // whole MCP server that never works is, and is. The rank is by errors, not by
  // rate, so a rarely-called tool can't outrank the one behind most failures.
  const stray = strayMcpTools(byTool);
  const worstTool = Object.entries(byTool)
    .filter(([name, t]) => t.calls >= 10 && t.errors > 0 && !inventedTool(name, t) && !stray.has(name))
    .map(([name, t]) => ({ name, errors: t.errors, rate: t.errors / t.calls, calls: t.calls,
                           shareOfErrors: toolErrors ? t.errors / toolErrors : null }))
    .sort((a, b) => b.errors - a.errors || a.name.localeCompare(b.name))[0] || null;

  const sessions = habitSessions(w);
  // Whether this Claude Code version records a measure is read from the sessions
  // that did work in the window, not only those that ended in it: a range where
  // everything is still open has none of the second kind, and "not recorded"
  // would be wrong.
  const activeIds = new Set(usage.map(r => r[1]));
  const endedIds = new Set(sessions.map(s => s.id));
  const worked = PAYLOAD.sessions.filter(s => !s.derived && s.health && didWork(s) &&
    projectOk(s.project) && (activeIds.has(s.id) || endedIds.has(s.id)));
  const edited = sessions.filter(s => s.health.edits > 0);
  const withInstr = sessions.filter(s => s.health.instructions !== null);
  return {
    w, spend, requests, activeDays, mainCost, longCost, veryLongCost, coldCost, missCost, missByReason,
    ev, denials, modes, checks, streaks, toolCalls, toolErrors, mainToolCalls, worstTool, stray,
    sessions, worked: worked.length,
    edited: edited.length,
    checked: edited.filter(s => s.health.checked_after_last_edit === true).length,
    withSubagents: sessions.filter(s => s.n_subagents > 0).length,
    withPlan: sessions.filter(s => s.health.plan_mode).length,
    instrRecorded: withInstr.length,
    withProjectMd: withInstr.filter(s => (s.health.instructions || []).includes('Project')).length,
    denialsRecorded: worked.some(s => s.health.recorded && s.health.recorded.denials),
    compactionsRecorded: worked.some(s => s.health.recorded && s.health.recorded.compactions),
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
// more of recent usage. This tool applies that line to its own measures (a
// 500k-token cutoff; the cost of re-caching after the cache expired), so the
// result is the tool's reading, not Anthropic's verdict.
function shareStatus(share) {
  if (share == null) return null;
  return share >= PAYLOAD.health.thresholds.flag_share ? 'look' : 'ok';
}
// Anthropic: about $13 per developer per active day on average, under $30 for
// 90% of users. Above the 90th percentile is flagged; at or under the average
// is not a target met, only where the figure sits.
function spendStatus(perDay) {
  if (perDay == null) return null;
  const b = PAYLOAD.health.benchmarks.cost_per_active_day;
  return perDay > b.p90 ? 'look' : (perDay <= b.average ? 'ok' : 'typical');
}
const SHARE_BADGE_TIP = "This tool applies Anthropic's /usage 10% line to its own measure of the habit. " +
  "It is the tool's reading, not Anthropic's verdict.";
// `kind` is 'share' (the 10% line) or 'spend' (Anthropic's $13 average / $30 p90).
function statusBadge(status, kind) {
  const spend = kind === 'spend';
  const tip = spend ? "Anthropic's published figures for spend per active day: about $13 on average, under $30 for 90% of users. They describe a population; they are not a target."
                    : SHARE_BADGE_TIP;
  if (status === 'look') {
    return `<span class="badge warn habit-status" title="${esc(tip)}">${spend ? '! Above the $30 p90' : '! 10% or more'}</span>`;
  }
  if (status === 'ok') {
    return spend ? `<span class="badge habit-status" title="${esc(tip)}">At or under the $13 average</span>`
                 : `<span class="badge ok habit-status" title="${esc(tip)}">&#10003; Under 10%</span>`;
  }
  if (status === 'typical') return `<span class="badge habit-status" title="${esc(tip)}">Between the $13 and $30 lines</span>`;
  return '';
}

// ---------- the Overview strip -------------------------------------------- //

function habitCards(r, b) {
  const h = PAYLOAD.health;
  const src = h.sources;
  const bench = h.benchmarks.cost_per_active_day;
  const pctVs = (now, base) => base == null ? 'no data before this range' : `baseline ${fmtPct(base)}`;
  const n = r.sessions.length;
  const longK = Math.round(h.thresholds.long_context_tokens / 1000);
  const flagK = Math.round((h.thresholds.flag_context_tokens || h.thresholds.long_context_tokens) / 1000);
  const cards = [];

  cards.push({
    label: 'Context tax',
    kind: 'share',
    status: shareStatus(r.veryLongShare),
    value: r.mainCost > 0 ? `${fmtPct(r.veryLongShare)} <span class="unit">of main spend</span>` : '—',
    sub: `${fmtCost(r.veryLongCost)} of ${fmtCost(r.mainCost)} on requests over ${flagK}k tokens ` +
         `(${fmtPct(r.longShare)} over ${longK}k) · ${pctVs(r.veryLongShare, b.veryLongShare)}` +
         (r.ev.auto ? ` · ${fmtInt(r.ev.auto)} auto-compaction${r.ev.auto === 1 ? '' : 's'}` : ''),
    fix: '<code>/clear</code> between unrelated tasks; for big work, write a spec and start a fresh session.',
    link: src.context_engineering || src.best_practices,
    tip: `Share of main-conversation spend on requests carrying more than ${flagK}k tokens of context ` +
         `(subagents excluded: each starts with a fresh, smaller context). Anthropic's /usage ` +
         `flags long context once it is 10% or more of recent usage; the badge is this tool applying ` +
         `that 10% line to its own ${flagK}k measure, not Anthropic's verdict. The ${flagK}k flag and the ` +
         `${longK}k share are this tool's own lines: Anthropic's pricing has no tier there (Claude 4.6 and ` +
         `later include the 1M window at standard pricing). Why it matters: Anthropic's context-engineering ` +
         `post says recall decreases as tokens grow, a performance gradient rather than a hard cliff. ` +
         `/clear between unrelated tasks is still worth doing.`,
  });
  cards.push({
    label: 'Cold-cache tax',
    kind: 'share',
    status: shareStatus(r.coldShare),
    value: r.spend > 0 ? `${fmtPct(r.coldShare)} <span class="unit">of spend</span>` : '—',
    sub: `${fmtCost(r.coldCost)} re-caching after the cache expired · ${pctVs(r.coldShare, b.coldShare)}`,
    fix: 'After a long break you could start fresh (or resume from a summary) rather than reopen a big session; not every break is avoidable.',
    link: src.costs,
    tip: "Extra paid to rebuild the prompt cache when a session was picked up after its cache expired, " +
         "by Claude Code's own cache-miss label, over reading the same tokens from a warm cache. " +
         "Anthropic's /usage flags cache misses at 10% or more of recent usage; the badge is this tool " +
         "applying that 10% line to its own measure, the priced cost of expiry misses " +
         "(previous_message_not_found) only, not misses with another reason. It is not Anthropic's verdict.",
  });
  cards.push({
    label: 'Spend per active day',
    kind: 'spend',
    status: spendStatus(r.costPerDay),
    value: r.costPerDay == null ? '—' : fmtCost(r.costPerDay),
    sub: `Anthropic: ~${fmtCost0(bench.average)} average, under ${fmtCost0(bench.p90)} for 90% · ` +
         (b.costPerDay == null ? 'no data before this range' : `baseline ${fmtCost(b.costPerDay)}`),
    fix: 'High spend usually traces to long uncleared sessions or the top model left on for everything.',
    link: src.costs,
    tip: 'List-price equivalent per day with any usage. On a seat plan this is not your bill; it shows ' +
         "how hard you draw on the allowance. Anthropic's $13 and $30 are averages across its own " +
         'deployments, not targets: under $13 does not mean healthy, and over $30 only puts you in the top tenth.',
  });
  cards.push({
    label: 'Checking work',
    status: null,
    value: r.edited ? `${fmtInt(r.checked)} <span class="unit">of ${fmtInt(r.edited)} sessions</span>` : '—',
    sub: (r.edited ? 'ran a shell test, lint or build after their last code edit' : 'no sessions edited code') +
         ` · ${pctVs(null, ratio(b.checked, b.edited))}`,
    fix: 'Give Claude a check it can run: tests, a build, a linter.',
    link: src.best_practices,
    tip: "Sessions that edited files and then ran a test, lint or build command in the shell. Browser " +
         "checks, Stop hooks, /verify and /code-review aren't counted: this tool can only see shell " +
         "commands. Anthropic's research finds verified success (passing tests, commits) is about " +
         'twice as common for experienced users.',
  });
  cards.push({
    label: 'Friction',
    status: null,
    value: r.toolCalls ? `${fmtPct(ratio(r.toolErrors, r.toolCalls))} <span class="unit">tool errors</span>` : '—',
    sub: (r.streaks ? `${fmtInt(r.streaks)} run${r.streaks === 1 ? '' : 's'} of ${PAYLOAD.health.thresholds.error_streak}+ errors in a row` : 'no long error runs') +
         (r.worstTool ? ` · most errors: ${esc(r.worstTool.name)}, ${fmtPct(r.worstTool.shareOfErrors)} of the errors ` +
                        `(fails ${fmtPct(r.worstTool.rate)} of its calls)` : '') +
         ` · ${pctVs(null, ratio(b.toolErrors, b.toolCalls))}`,
    fix: "Our suggestion, not Anthropic's: a tool that fails often may be an integration or permission worth checking.",
    link: null,
    tip: 'Tool calls that returned an error, runs of consecutive failures, and the tool behind the most ' +
         'errors (at least 10 calls). A tool name that is not a Claude Code built-in and never succeeded ' +
         '(made up by the model, or not available) is left out, and so is an MCP tool name that never ' +
         'worked while the server\'s other tools did; a built-in that fails every time, or an MCP server ' +
         'that never works, counts. Refused calls (you or a rule said no) are not errors.',
  });
  cards.push({
    label: 'Workflow',
    status: null,
    value: n ? `${fmtInt(r.withSubagents)} <span class="unit">of ${fmtInt(n)} used subagents</span>` : '—',
    sub: (n ? `plan mode ${fmtInt(r.withPlan)} · project CLAUDE.md ` +
              (r.instrRecorded ? `${fmtInt(r.withProjectMd)} of ${fmtInt(r.instrRecorded)}` : 'not recorded') : 'no sessions') +
         ` · subagents ${pctVs(null, ratio(b.withSubagents, b.sessions.length))}`,
    fix: 'Plan mode for complex tasks; subagents for parallel work, noisy output or an independent review; a short CLAUDE.md.',
    link: src.costs,
    tip: 'Sessions that ended in the window and made at least one request. Anthropic\'s costs page: ' +
         '"Use plan mode for complex tasks". Its best-practices page suggests subagents for parallel ' +
         'work, noisy output and independent review. Whether a CLAUDE.md was loaded is only recorded by ' +
         'newer Claude Code versions.',
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
    (r.spend ? (flagged ? ` · ${flagged} flagged` : ' · nothing flagged') : '');
  const strip = document.getElementById('habitsStrip');
  if (!r.spend && !r.sessions.length) {
    strip.innerHTML = `<div class="empty-state"><div class="es-title">No activity in these dates</div>` +
      `<div class="es-body">Widen the date range, or move it, to see a period with activity.</div></div>`;
    return;
  }
  strip.innerHTML = cards.map(c =>
    `<div class="habit${c.status === 'look' ? ' flagged' : ''}" title="${esc(c.tip)}">` +
      `<div class="habit-top"><span class="label">${esc(c.label)}</span>${statusBadge(c.status, c.kind)}</div>` +
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
      'Extra paid to rebuild the cache after it expired between requests. Tagged from $0.50: that line is ' +
      "this tool's own, not Anthropic's, and a break long enough to expire the cache isn't always avoidable.", 'warn']);
  }
  if (h.max_error_streak >= t.error_streak) {
    tags.push([`${h.max_error_streak} errors in a row`, 'The longest run of consecutive failed tool calls.', '']);
  }
  if (h.checked_after_last_edit === false) {
    tags.push(['unchecked edits', 'Edited code without running a shell test, lint or build afterwards ' +
      "(browser checks, hooks and /verify aren't counted).", '']);
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

  const perSession = m.sessions.map(s => s.health.prompts).filter(n => n > 0).sort((a, b) => a - b);
  const promptsPerSession = median(perSession);
  // A median of whole numbers can land halfway (4.5): showing 5 would overstate it.
  const medianText = promptsPerSession == null ? '—'
    : Number.isInteger(promptsPerSession) ? fmtInt(promptsPerSession) : promptsPerSession.toFixed(1);
  // Prompts and tool calls aren't recorded per model, so a model filter can't
  // split their ratio: it would divide a filtered numerator by an unfiltered one.
  const modelFiltered = state.models.length > 0;
  const noSession = 'no session ended in these dates';
  // The version line is where these logs first show the record, not a
  // documented Claude Code release (HB-9), so the caption says only that.
  const NOT_SEEN_YET = 'not in these logs before a later Claude Code version';
  const compSub = !m.worked ? 'no session worked in these dates'
    : m.compactionsRecorded ? `${fmtInt(m.ev.auto)} automatic · ${fmtInt(m.ev.manual)} manual`
    : NOT_SEEN_YET;
  const denialSub = !m.worked ? 'no session worked in these dates'
    : m.denialsRecorded ? 'by you, auto mode or a rule' : NOT_SEEN_YET;
  renderTiles('habitTiles', [
    ['Prompts', fmtInt(prompts),
     [`${fmtInt(m.ev.queued)} typed while Claude was busy`,
      m.ev.away ? `${fmtInt(m.ev.away)} away recap${m.ev.away === 1 ? '' : 's'}` : null].filter(Boolean).join(' · '),
     'Away recaps are the summaries Claude Code writes when you come back to a session after a break.'],
    ['Prompts per session', medianText, m.sessions.length ? 'median · sessions that ended in these dates' : noSession],
    ['Tool calls per prompt',
     !modelFiltered && prompts ? (m.mainToolCalls / prompts).toFixed(1) : '—',
     modelFiltered ? 'not split by model: clear the model filter' : 'Anthropic: ~5 actions for novices, ~12 for experts',
     'Tool calls Claude made in the main conversation (subagents\' calls left out) per prompt you sent. ' +
     'Anthropic counts Claude\'s actions per prompt; a tool call is the closest thing these logs hold, so ' +
     'read it as a profile, not a target. Bigger delegation goes with expertise.'],
    ['Interrupts', prompts ? `${fmtInt(m.ev.interrupt)}` : '—',
     prompts ? `${(100 * m.ev.interrupt / prompts).toFixed(1)} per 100 prompts` : null,
     'Experienced users interrupt more, not less (Anthropic, 2026): this is a profile, not a score.'],
    ['Compactions', m.compactionsRecorded ? fmtInt(m.ev.auto + m.ev.manual) : '—', compSub],
    ['Refused tool calls', m.denialsRecorded ? fmtInt(Object.values(m.denials).reduce((a, b) => a + b, 0)) : '—', denialSub],
  ]);
  // Four different groups sit on this tab; say which each figure counts.
  const note = document.getElementById('habitPopulations');
  if (note) note.textContent =
    `What each figure counts: prompts, interrupts, compactions and refusals are what happened on the selected ` +
    `days; prompts per session and "checked after last edit" count the ${fmtInt(m.sessions.length)} session` +
    `${m.sessions.length === 1 ? '' : 's'} that ended on them (originals that made at least one request); the ` +
    `charts below count requests on the selected days; tool calls per prompt counts main-conversation tool calls.`;

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
    ['Shell check after last edit', m.edited ? `${fmtInt(m.checked)} of ${fmtInt(m.edited)}` : '—',
     m.edited ? 'sessions that edited code' : (m.sessions.length ? 'no session edited code' : 'no session ended in these dates')],
    ['Shell test runs', fmtInt(c.test || 0)],
    ['Shell lint & type checks', fmtInt(c.lint || 0)],
    ['Shell builds', fmtInt(c.build || 0)],
    ['Commits', fmtInt(c.commit || 0)],
    ['PRs opened', fmtInt(c.pr || 0)],
  ]);
}

function renderRecentSessions() {
  const el = document.getElementById('habitRecent');
  const rows = sessionRows().filter(s => !s.derived && s.health && didWork(s))
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
