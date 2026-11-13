"""Render collected benchmark timings as a self-contained, offline HTML report."""
from __future__ import annotations

from html import escape
import json
import math


COLUMNS = (
    ('setup', 'Setup'),
    ('pre_analyze', 'Pre-analysis'),
    ('analysis', 'Analysis'),
    ('numeric', 'Numeric'),
    ('solve', 'Solve'),
    ('teardown', 'Teardown'),
    ('critical_path', 'Critical path'),
)

_STYLE = """
:root { color-scheme: light; --ink: #182737; --muted: #526575;
  --line: #dce4eb; --accent: #075f56; --best: #e5f5ef; }
* { box-sizing: border-box; }
body { margin: 0; background: #f3f6f8; color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width: 1720px; margin: auto; padding: 40px 28px 64px; }
h1, h2, h3, p { margin-top: 0; }
h1 { font-size: clamp(28px, 4vw, 42px); line-height: 1.2; margin-bottom: 12px; }
h2 { font-size: 23px; margin-bottom: 8px; overflow-wrap: anywhere; }
h3 { font-size: 17px; margin-bottom: 10px; }
.eyebrow { color: var(--accent); text-transform: uppercase; font-size: 12px;
  font-weight: 750; letter-spacing: .12em; margin-bottom: 10px; }
.lead { color: var(--muted); max-width: 1000px; }
.stats { display: flex; flex-wrap: wrap; gap: 8px; margin: 20px 0 28px; }
.stat { background: #fff; border: 1px solid var(--line); border-radius: 6px; padding: 7px 12px; }
.method, .matrix, .sources { background: #fff; border: 1px solid var(--line);
  border-radius: 10px; margin: 22px 0; padding: 24px; }
.method p:last-child, .sources p:last-child { margin-bottom: 0; }
.muted, small { color: var(--muted); }
.matrix-meta { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; margin-bottom: 12px; }
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--accent); font-size: 13px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 10px 0 0;
  padding: 12px; background: #f6f8fa; border-radius: 5px; font-size: 12px; line-height: 1.45; }
code { font-size: .9em; overflow-wrap: anywhere; }
.table-scroll { overflow-x: auto; margin-top: 22px; }
table { border-collapse: collapse; width: 100%; min-width: 1120px; font-size: 13px; }
caption { text-align: left; font-size: 17px; font-weight: 650; color: var(--ink); padding-bottom: 10px; }
th, td { padding: 12px 10px; border-bottom: 1px solid var(--line); text-align: right; vertical-align: top; }
thead th { background: #edf2f5; font-weight: 650; white-space: nowrap; }
th:first-child, td:first-child, .identity { text-align: left; }
tbody th { font-weight: 500; }
.platform { min-width: 180px; max-width: 280px; overflow-wrap: anywhere; }
.identity { min-width: 200px; max-width: 320px; overflow-wrap: anywhere; }
.identity strong, .identity small, .platform small { display: block; }
.identity details, .platform details { font-weight: 400; }
.identity pre, .platform pre { min-width: 200px; max-width: 320px; }
.timing { font-variant-numeric: tabular-nums; white-space: nowrap; }
.best { background: var(--best); color: #075449; font-weight: 700; }
.missing { color: #778795; }
.critical { border-left: 2px solid var(--line); }
.legend { margin: 10px 0 0; color: var(--muted); font-size: 12px; }
.swatch { display: inline-block; width: 10px; height: 10px; margin-right: 5px;
  background: var(--best); border: 1px solid #b9dfd1; }
a { color: var(--accent); }
.sources ol { padding-left: 24px; margin-bottom: 0; }
.sources li { margin-bottom: 12px; overflow-wrap: anywhere; }
.sources li:last-child { margin-bottom: 0; }
@media (max-width: 700px) { main { padding: 24px 12px; }
  .method, .matrix, .sources { padding: 16px; } }
@media print { body { background: #fff; } main { max-width: none; padding: 0; }
  .method, .matrix, .sources { border-radius: 0; padding: 12px; }
  .table-scroll { overflow: visible; } table { min-width: 0; font-size: 9px; }
  th, td { padding: 6px 4px; } .platform, .identity { min-width: 0; }
  thead { display: table-header-group; } tr { break-inside: avoid; }
  .best { print-color-adjust: exact; } }
"""


def _text(value):
    return escape(str(value), quote=True)


def _json_details(label, value):
    return (f'<details><summary>{_text(label)}</summary>'
            f'<pre>{_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))}</pre>'
            '</details>')


def _short_id(value):
    return str(value).removeprefix('sha256:')[:10]


def _row_identity(row):
    sources = ', '.join(
        f'<a href="#source-{_text(index)}">{_text(index)}</a>'
        for index in row['sources'])
    outcomes = ', '.join(f'{name}: {count}' for name, count in sorted(row['outcomes'].items()))
    return (
        '<td class="platform">'
        f'{_text(row["platform"])}'
        f'<small>Platform {_text(_short_id(row["platform_id"]))}</small>'
        f'{_json_details("Platform details", row["host"])}'
        '</td><th scope="row" class="identity">'
        f'<strong>{_text(row["solver"])}</strong>'
        f'<small>Configuration {_text(_short_id(row["configuration_id"]))}</small>'
        f'<small>Sources: {sources}</small>'
        f'<small>{_text(outcomes)}</small>'
        f'{_json_details("Solver configuration", row["configuration"])}'
        f'</th><td class="timing">{_text(row["samples"])}</td>'
    )


def _timing_cell(value, best, ratio, median, median_best, median_ratio, *, critical):
    classes = ['timing']
    if critical:
        classes.append('critical')
    if value is None: classes.append('missing')
    elif value == best: classes.append('best')
    spans = []
    for stat, number, baseline, normalized in (('min', value, best, ratio),
                                               ('median', median, median_best, median_ratio)):
        tooltip = ('No accepted measured timing' if number is None else
                   f'{number!r} seconds / column best {baseline!r} seconds')
        for mode in ('absolute', 'relative'):
            item = number if mode == 'absolute' else normalized
            label = '—' if item is None else '∞' if math.isinf(item) else f'{item:.6g}'
            hidden = '' if stat == 'min' and mode == 'absolute' else ' hidden'
            spans.append(f'<span class="{mode}" data-stat="{stat}" data-best="{str(number is not None and number == baseline).lower()}" '
                         f'data-missing="{str(number is None).lower()}" data-tooltip="{_text(tooltip)}"{hidden}>{label}</span>')
    label = ''.join(spans)
    tooltip = 'No accepted measured timing' if value is None else f'{value!r} seconds / column best {best!r} seconds'
    return f'<td class="{" ".join(classes)}" title="{_text(tooltip)}">{label}</td>'


def _error_cell(value):
    if value is None:
        return '<td class="timing missing" title="No measured backward error available">—</td>'
    label = '∞' if math.isinf(value) else f'{value:.6g}'
    return (f'<td class="timing" title="Worst measured componentwise backward error '
            f'(dimensionless): {_text(value)}">{label}</td>')


def _table(matrix, metric_name):
    rows, best = matrix['rows'], matrix['platform_best_times'][matrix['rows'][0]['platform_id']]
    median_best = matrix['platform_best_median_times'][matrix['rows'][0]['platform_id']]
    caption = f'Timings — {metric_name} (seconds or ratio to column best)'
    parts = ['<div class="table-scroll"><table>', f'<caption>{caption}</caption>',
             '<thead><tr><th scope="col">Platform</th>',
             '<th scope="col" class="identity">Solver / configuration</th>',
             '<th scope="col">Accepted runs</th>',
             '<th scope="col">Worst componentwise <br>backward error</th>']
    for phase, label in COLUMNS:
        classes = ' class="critical"' if phase == 'critical_path' else ''
        parts.append(f'<th scope="col"{classes}>{label}</th>')
    parts.append('</tr></thead><tbody>')
    for row in rows:
        parts.extend(('<tr>', _row_identity(row),
                      _error_cell(row['worst_componentwise_backward_error'])))
        for phase, _ in COLUMNS:
            parts.append(_timing_cell(row['times'][phase], best[phase], row['relative_times'][phase],
                                      row['median_times'][phase], median_best[phase], row['relative_median_times'][phase],
                                      critical=phase == 'critical_path'))
        parts.append('</tr>')
    parts.append('</tbody></table></div>')
    return '\n'.join(parts)


def _resource_table(matrix):
    columns = [('cpu_utilization', phase, label) for phase, label in COLUMNS]
    columns += [('phase_rss_after_bytes', phase, label) for phase, label in COLUMNS[:-1]]
    columns += [('process_resources', 'cpu_utilization', 'Process CPU / wall'),
                ('process_resources', 'max_rss_bytes', 'Process peak RSS')]
    parts = ['<div class="table-scroll"><table><caption>Resource utilization</caption>',
             '<thead><tr><th>Platform</th><th>Solver / configuration</th><th>Accepted runs</th>']
    for group, key, label in columns:
        prefix = 'CPU / wall: ' if group == 'cpu_utilization' else 'RSS after: ' if group == 'phase_rss_after_bytes' else ''
        parts.append(f'<th scope="col">{prefix}{label}</th>')
    parts.append('</tr></thead><tbody>')
    for row in matrix['rows']:
        parts.extend(('<tr>', _row_identity(row)))
        for group, key, _ in columns:
            value = row[group][key]
            label = '—' if value is None else f'{value / (1048576 if "rss" in group or key == "max_rss_bytes" else 1):.6g}'
            parts.append(f'<td class="timing">{label}</td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div>')
    return '\n'.join(parts)


def render_report(report, title='Solver benchmark report'):
    """Return an HTML document; every benchmark-supplied string is escaped."""
    metric_name = 'wall time' if report['metric'] == 'wall_seconds' else 'CPU time'
    matrices = report['matrices']
    sources = report['sources']
    parts = [
        '<!doctype html>', '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>{_text(title)}</title>', f'<style>{_STYLE}</style></head><body><main>',
        '<header><p class="eyebrow">SolverBenchmark</p>', f'<h1>{_text(title)}</h1>',
        '<p class="lead">Compare solver performance within each platform, with '
        'timing and resource tables for each matrix input.</p>',
        '<button type="button" id="toggle-relative" aria-pressed="false">Show relative timings</button>',
        '<button type="button" id="toggle-median" aria-pressed="false">Show median timings</button>',
        '<div class="stats">',
        f'<span class="stat"><strong>{len(sources)}</strong> benchmark outputs</span>',
        f'<span class="stat"><strong>{len(matrices)}</strong> matrix comparisons</span>',
        f'<span class="stat">Metric: <strong>{metric_name}</strong></span>',
        '</div></header>',
        '<section class="method" aria-labelledby="method-title">',
        '<h2 id="method-title">How to read this report</h2>',
        '<p>Only accepted measured trials from baseline passes contribute timings. '
        'Warmup runs and trace passes are excluded. Each phase shows either the minimum or '
        'median of eligible repetitions pooled across the listed sources; different phases may use '
        'different repetitions. Even-sized medians average the middle two values. Earlier accepted trials remain eligible if '
        'a later trial fails; execution outcomes are shown beside each solver.</p>',
        '<p><strong>Critical path</strong> is the minimum or median of same-trial sums of '
        '<strong>analysis + numeric + solve</strong>. It excludes setup, pre-analysis, '
        'teardown, IPC, and parent validation. It is not a sum of independently reduced phase values.</p>',
        '<p>Relative timings divide each value by the best value in that column across '
        'solver rows on the same platform and matrix input, using the selected statistic: '
        '<strong>1</strong> is best and <strong>1.2</strong> is 20% slower. '
        'A dash means no eligible timing and does not affect the best value. '
        'If the best value is zero, zero ties are 1 and positive values are ∞.</p>',
        '<p><strong>Worst componentwise backward error</strong> is the maximum over '
        'all RHS columns and measured baseline repetitions across the listed sources, '
        'including rejected solutions. Warmups and trace passes are excluded. '
        'This dimensionless error is shown as an absolute value; '
        'it is not normalized. A dash means no error was recorded; ∞ retains an '
        'infinite error or failed-verification diagnostic.</p>',
        '<p>Input hashes include matrix and RHS data. Different input hashes '
        'have separate comparisons. Solver tolerance settings remain in each '
        'configuration; achieved errors are shown alongside timings. Platform and solver '
        'configuration details identify the measurements being pooled. '
        f'Trace results excluded from timings: <strong>{_text(report["excluded_trace_results"])}</strong>.</p>',
        '<p>CPU / wall is process CPU seconds divided by wall seconds for the same observation: 1 means one CPU core on average, and values above 1 mean more than one core. Each phase uses the eligible accepted baseline trial with the shortest positive wall time; critical path uses the same-trial sums of analysis, numeric and solve. Process CPU / wall uses the successful baseline process with accepted trials and shortest positive lifetime wall time. Process values include startup, warmup and work outside measured phases. RSS values are MiB. RSS after each phase is the minimum boundary snapshot from accepted trace trials, not a phase peak. Process peak RSS is the minimum lifetime peak across successful baseline processes with accepted trials. A dash means no eligible measurement.</p>',
        '</section>',
    ]
    if not matrices:
        parts.append('<section class="matrix"><h2>No baseline results</h2>'
                     '<p>The supplied outputs contain no baseline result rows to compare.</p></section>')
    for index, matrix in enumerate(matrices, 1):
        names = ', '.join(matrix['case_ids'])
        parts.extend((
            f'<section class="matrix" aria-labelledby="matrix-{index}">',
            f'<h2 id="matrix-{index}">{_text(names)}</h2>',
            f'<p class="matrix-meta">Input <code>{_text(matrix["input_hash"])}</code></p>',
            _json_details('Matrix and RHS recipe', matrix['case']),
        ))
        platforms = dict.fromkeys(row['platform_id'] for row in matrix['rows'])
        for platform_id in platforms:
            rows = [row for row in matrix['rows'] if row['platform_id'] == platform_id]
            group = {**matrix, 'rows': rows}
            parts.extend((f'<h3>{_text(rows[0]["platform"])}</h3>',
                          _table(group, metric_name), _resource_table(group),
                          '<p class="legend"><span class="swatch" aria-hidden="true"></span>'
                          'Green cells mark the best timing on this platform, including ties. '
                          'Values show six significant digits; hover over a timing for its stored value.</p>'))
        parts.append('</section>')
    parts.extend(('<section class="sources" aria-labelledby="sources-title">',
                  '<h2 id="sources-title">Benchmark sources</h2>',
                  '<p class="muted">Source numbers in each row refer to these collected outputs.</p><ol>'))
    for index, source in enumerate(sources, 1):
        parts.append(f'<li id="source-{index}"><code>{_text(source["path"])}</code><br>'
                     f'<small>Run ID: {_text(source["run_id"])}</small></li>')
    parts.extend(('</ol></section>', '</main><script>\n'
                  'const relativeButton = document.getElementById("toggle-relative");'
                  'const medianButton = document.getElementById("toggle-median");'
                  'function refreshTimings() {'
                  'const relative = relativeButton.getAttribute("aria-pressed") === "true";'
                  'const median = medianButton.getAttribute("aria-pressed") === "true";'
                  'relativeButton.textContent = relative ? "Show absolute timings" : "Show relative timings";'
                  'medianButton.textContent = median ? "Show minimum timings" : "Show median timings";'
                  'document.querySelectorAll("td.timing span[data-stat]").forEach(e => {'
                  'e.hidden = (e.dataset.stat === "median") !== median || e.classList.contains("relative") !== relative;'
                  'if (!e.hidden) { const cell = e.parentElement; cell.classList.toggle("best", e.dataset.best === "true");'
                  'cell.classList.toggle("missing", e.dataset.missing === "true"); cell.title = e.dataset.tooltip; }'
                  '}); }'
                  '[relativeButton, medianButton].forEach(button => button.addEventListener("click", function () {'
                  'this.setAttribute("aria-pressed", String(this.getAttribute("aria-pressed") !== "true")); refreshTimings();'
                  '}));</script></body></html>'))
    return '\n'.join(parts) + '\n'
