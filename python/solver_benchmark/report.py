"""Collect baseline measurements for matching HTML and machine-readable reports."""
from __future__ import annotations

from collections import Counter
import json
import math
import statistics
from pathlib import Path
import tempfile

from .profiles import canonical_hash, load_json
from .report_html import render_report


PHASES = ('setup', 'pre_analyze', 'analysis', 'numeric', 'solve', 'teardown')
CORE_PHASES = ('analysis', 'numeric', 'solve')
METRICS = {'wall': 'wall_seconds', 'cpu': 'cpu_seconds'}
METADATA_FILES = ('execution.json', 'profile.json', 'version-manifest.json')


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f'{name} must be a JSON object')
    return value


def _text(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f'{name} must be a nonempty string')
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def _results(path):
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                result = _object(json.loads(line, object_pairs_hook=_unique_pairs), 'result')
                # Match the artifact writer's finite, canonicalizable JSON contract.
                canonical_hash(result)
                yield result
            except (ValueError, TypeError) as exc:
                raise ValueError(f'{path}:{number}: {exc}') from exc


def _trial_index(record):
    _object(record, 'trial record')
    trial = _object(record.get('trial', {}), 'trial')
    if trial.get('kind') != 'measured':
        return None
    index = trial.get('index')
    if type(index) is not int or index < 0:
        raise ValueError('measured trial index must be a nonnegative integer')
    return index


def _time(record, metric):
    times = record.get('times')
    if times is None:
        return None
    value = _object(times, 'times').get(metric)
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f'{metric} must be finite and nonnegative')
    return value


def _measurement_samples(result, metric):
    """Return eligible phase observations and complete same-trial core sums."""
    accepted = set()
    for record in result.get('accuracy', []):
        index = _trial_index(record)
        if index is not None and _object(record.get('accuracy', {}), 'accuracy').get('accepted') is True:
            accepted.add(index)
    trials = {}
    seen = set()
    for phase_name, records in ((None, result.get('phases', [])),
                                ('teardown', result.get('teardowns', []))):
        for record in records:
            index = _trial_index(record)
            if index not in accepted:
                continue
            phase = phase_name or record.get('phase')
            if phase not in PHASES:
                continue
            key = (index, phase)
            if key in seen:
                raise ValueError(f'duplicate measured phase: trial {index}, {phase}')
            seen.add(key)
            if record.get('completed') is not True:
                continue
            if phase != 'teardown':
                statuses = ('success',) if phase == 'solve' else ('success', 'not_applicable')
                if _object(record.get('result', {}), 'phase result').get('status') not in statuses:
                    continue
            value = _time(record, metric)
            if value is not None:
                trials.setdefault(index, {})[phase] = value
    samples = {phase: [] for phase in (*PHASES, 'critical_path')}
    for times in trials.values():
        for phase, value in times.items():
            samples[phase].append(value)
        if all(phase in times for phase in CORE_PHASES):
            core = sum(times[phase] for phase in CORE_PHASES)
            if not math.isfinite(core):
                raise ValueError('critical path sum must be finite')
            samples['critical_path'].append(core)
    return samples, len(accepted)


def _measurements(result, metric):
    samples, accepted = _measurement_samples(result, metric)
    return {phase: min(values, default=None) for phase, values in samples.items()}, accepted


def _median(values):
    if not values:
        return None
    low, high = statistics.median_low(values), statistics.median_high(values)
    return low + (high - low) / 2


def _utilization_samples(result):
    """Pair CPU, system CPU and wall from the same accepted trial."""
    accepted = {_trial_index(r) for r in result.get('accuracy', [])
                if _trial_index(r) is not None and r.get('accuracy', {}).get('accepted') is True}
    trials = {}
    for phase_name, records in ((None, result.get('phases', [])), ('teardown', result.get('teardowns', []))):
        for record in records:
            index = _trial_index(record)
            phase = phase_name or record.get('phase')
            if index not in accepted or phase not in PHASES or record.get('completed') is not True:
                continue
            if phase != 'teardown':
                statuses = ('success',) if phase == 'solve' else ('success', 'not_applicable')
                if _object(record.get('result', {}), 'phase result').get('status') not in statuses:
                    continue
            wall, cpu = _time(record, 'wall_seconds'), _time(record, 'cpu_seconds')
            system = _time(record, 'system_cpu_seconds')
            if wall is not None and cpu is not None:
                trials.setdefault(index, {})[phase] = (wall, cpu, system)
    pairs = dict.fromkeys((*PHASES, 'critical_path'))
    for phases in trials.values():
        if all(phase in phases for phase in CORE_PHASES):
            phases['critical_path'] = tuple(
                sum(phases[p][i] for p in CORE_PHASES)
                if all(phases[p][i] is not None for p in CORE_PHASES) else None
                for i in (0, 1, 2))
        for phase, pair in phases.items():
            if pair[0] > 0 and (pairs[phase] is None or pair[0] < pairs[phase][0]):
                pairs[phase] = pair
    return pairs


def _worst_componentwise_backward_error(result):
    """Include rejected measured solutions so bad accuracy cannot be hidden."""
    worst = None
    for record in result.get('accuracy', []):
        if _trial_index(record) is None:
            continue
        accuracy = _object(record.get('accuracy', {}), 'accuracy')
        for column in accuracy.get('columns', []):
            value = _object(column, 'accuracy column').get('componentwise_backward_error')
            if value is None:
                continue
            if value == {'nonfinite': '+inf'}:
                # The validator emits this JSON-safe diagnostic for failed verification.
                value = math.inf
            elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('componentwise backward error must be nonnegative '
                                 'and finite or a tagged +inf diagnostic')
            worst = value if worst is None else max(worst, value)
    return worst


def _source_paths(inputs):
    paths = []
    for value in inputs:
        path = Path(value).resolve()
        if path.is_dir():
            path = path / 'results.jsonl'
        if path.name != 'results.jsonl':
            raise ValueError(f'{value}: expected a benchmark directory or results.jsonl')
        if path not in paths:
            paths.append(path)
    if not paths:
        raise ValueError('at least one benchmark output is required')
    return paths


def _single_core_original_affinity(execution, host, process, options):
    """Return the pre-pin mask only when the recorded pin is internally consistent."""
    control = execution.get('affinity_control')
    if not isinstance(control, dict) or control.get('mode') != 'single_core':
        return None
    original, selected = control.get('original_affinity'), control.get('selected_cpu')
    if (not isinstance(original, list) or not original
            or any(type(cpu) is not int or cpu < 0 for cpu in original)
            or original != sorted(set(original)) or type(selected) is not int
            or selected != original[0] or host.get('affinity') != [selected]
            or process.get('affinity') != [selected] or options.get('thread_limit') != 1):
        return None
    return original


def collect_report(inputs, metric='wall', *, compare_affinity=False):
    """Pool comparable runs without mixing inputs, solver settings, or platforms.

    Matrix identity is the delivered matrix/RHS input hash. Rows retain all solver
    options (including tolerances), binary/dependency identity, thread environment,
    and platform metadata. Only baseline passes contribute timings and errors.
    """
    if metric not in METRICS:
        raise ValueError('metric must be wall or cpu')
    timing = METRICS[metric]
    report = {
        'schema_version': '1.1', 'report_type': 'solver-benchmark-comparison',
        'metric': timing, 'sources': [], 'matrices': [], 'excluded_trace_results': 0,
        'aggregation': {
            'pass_kind': 'baseline', 'trial_kind': 'measured',
            'timings': {'accuracy_accepted_only': True, 'phase_reduction': 'minimum',
                        'additional_reduction': 'median_of_pooled_trial_observations',
                        'critical_path': {'phases': list(CORE_PHASES),
                                          'reduction': 'minimum_of_same_trial_sum'}},
            'relative_times': {
                'baseline': 'minimum_per_matrix_platform_column',
                'affinity': ('compare_across_affinities' if compare_affinity
                             else 'separate_affinities_except_recorded_single_core'),
                'zero_best': {'zero_value': 1, 'positive_value': {'nonfinite': '+inf'}}},
            'cpu_utilization': {
                'definition': 'cpu_seconds_divided_by_wall_seconds_from_same_observation',
                'phase_selection': 'accepted_baseline_trial_with_minimum_positive_wall_seconds',
                'critical_path': 'same_trial_core_cpu_sum_divided_by_core_wall_sum',
                'process_selection': 'successful_baseline_process_with_accepted_trials_and_minimum_positive_wall_seconds'},
            'system_cpu': {
                'definition': 'system_cpu_seconds_and_percentage_of_total_cpu_seconds',
                'selection': 'same_observation_as_cpu_utilization'},
            'worst_componentwise_backward_error': {
                'reduction': 'maximum_over_rhs_trials_and_sources',
                'includes_rejected_trials': True},
        },
    }
    matrices = {}
    for path in _source_paths(inputs):
        try:
            execution, profile, manifest = (
                _object(load_json(path.parent / name), name) for name in METADATA_FILES)
            host = _object(execution.get('host'), 'execution host')
            run_id = _text(execution.get('run_id'), 'execution run_id')
            cases = {case['id']: case for case in profile['cases']}
            workers = _object(manifest.get('workers'), 'manifest workers')
            deployment = _object(execution.get('deployment', {}), 'deployment').get('solvers', {})
            _object(deployment, 'deployment solvers')
            source_number = len(report['sources']) + 1
            report['sources'].append({'id': source_number, 'path': str(path), 'run_id': run_id})
            count = 0
            for result in _results(path):
                count += 1
                kind = _object(result.get('instrumentation'), 'instrumentation').get('kind')
                if kind == 'trace':
                    report['excluded_trace_results'] += 1
                if kind not in ('baseline', 'trace'):
                    raise ValueError(f'unknown instrumentation kind: {kind!r}')
                case_id = _text(result.get('case_id'), 'case_id')
                solver = _text(result.get('solver_id'), 'solver_id')
                case = cases[case_id]
                provenance = _object(result.get('provenance'), 'provenance')
                input_hash = _text(provenance.get('input_hash'), 'input_hash')
                options = _object(result.get('options'), 'options')
                accuracy = {key: options[key] for key in ('relative_tolerance', 'absolute_tolerance')}
                for value in accuracy.values():
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError('accuracy tolerances must be finite and nonnegative')
                matrix_key = input_hash
                if matrix_key not in matrices:
                    matrices[matrix_key] = {'case_ids': set(), 'input_hash': input_hash,
                                            'case': case, 'rows': {}}
                matrix = matrices[matrix_key]
                matrix['case_ids'].add(case_id)
                process = _object(result.get('process', {}), 'process')
                platform = {**host, 'hostname': process.get('host'),
                            'worker_affinity': process.get('affinity')}
                if 'affinity_control' in execution:
                    platform['affinity_control'] = execution['affinity_control']
                platform_id = canonical_hash(platform)
                comparison_platform = {**host, 'worker_affinity': process.get('affinity')}
                if compare_affinity:
                    comparison_platform.pop('affinity', None)
                    comparison_platform.pop('worker_affinity', None)
                else:
                    original_affinity = _single_core_original_affinity(execution, host, process, options)
                    if original_affinity is not None:
                        comparison_platform['affinity'] = original_affinity
                        comparison_platform['worker_affinity'] = original_affinity
                if isinstance(comparison_platform.get('cloud'), dict):
                    comparison_platform['cloud'] = {k: v for k, v in comparison_platform['cloud'].items() if k != 'zone'}
                comparison_platform_id = canonical_hash(comparison_platform)
                configuration = {'options': options, 'worker': workers[solver],
                                 'arguments': deployment.get(solver, {}).get('arguments', []),
                                 'process_environment': process.get('environment', {}),
                                 'effective_thread_environment': result.get('effective_thread_environment', {})}
                configuration_id = canonical_hash(configuration)
                row_key = (platform_id, solver, configuration_id)
                if row_key not in matrix['rows']:
                    cloud = host.get('cloud')
                    cloud = cloud if isinstance(cloud, dict) else {}
                    cpu = host.get('cpu') or cloud.get('cpu_platform') or 'Unknown CPU'
                    cpu_platform = cloud.get('cpu_platform')
                    if cpu_platform == cpu:
                        cpu_platform = None
                    cloud_type = ' '.join(str(v) for v in
                                          (cloud.get('provider'), cloud.get('machine_type')) if v)
                    label = ' / '.join(str(v) for v in (
                        cpu, cpu_platform, cloud_type, host.get('machine'), process.get('host')) if v)
                    matrix['rows'][row_key] = {
                        'platform': label, 'platform_id': platform_id, 'host': platform,
                        'comparison_platform_id': comparison_platform_id,
                        'comparison_platform': ' / '.join(str(v) for v in
                            (cpu, cpu_platform, cloud_type, host.get('machine')) if v),
                        'solver': solver, 'configuration': configuration,
                        'configuration_id': configuration_id, 'sources': set(),
                        'outcomes': Counter(), 'trace_outcomes': Counter(), 'samples': 0,
                        '_baseline': False,
                        'worst_componentwise_backward_error': None,
                        'times': dict.fromkeys((*PHASES, 'critical_path')),
                        '_timing_samples': {phase: [] for phase in (*PHASES, 'critical_path')},
                        'cpu_times': dict.fromkeys((*PHASES, 'critical_path')),
                        '_utilization_pairs': dict.fromkeys((*PHASES, 'critical_path')),
                        '_process_utilization_pair': None,
                        'phase_rss_after_bytes': dict.fromkeys(PHASES),
                        'process_resources': dict.fromkeys(('cpu_seconds', 'max_rss_bytes'))}
                row = matrix['rows'][row_key]
                row['sources'].add(source_number)
                if kind == 'trace':
                    row['trace_outcomes'][_text(result.get('outcome'), 'outcome')] += 1
                    accepted = {_trial_index(a) for a in result.get('accuracy', [])
                                if _trial_index(a) is not None and a.get('accuracy', {}).get('accepted') is True}
                    for phase_name, records in ((None, result.get('phases', [])),
                                                ('teardown', result.get('teardowns', []))):
                        for record in records:
                            phase = phase_name or record.get('phase')
                            if (_trial_index(record) not in accepted or phase not in PHASES
                                    or record.get('completed') is not True):
                                continue
                            if phase != 'teardown' and record.get('result', {}).get('status') not in ('success', 'not_applicable'):
                                continue
                            value = (record.get('observations') or {}).get('rss_after_bytes')
                            if value is not None:
                                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                                    raise ValueError('rss_after_bytes must be finite and nonnegative')
                                old = row['phase_rss_after_bytes'][phase]
                                row['phase_rss_after_bytes'][phase] = value if old is None else min(old, value)
                    continue
                row['_baseline'] = True
                row['outcomes'][_text(result.get('outcome'), 'outcome')] += 1
                error = _worst_componentwise_backward_error(result)
                if error is not None and (row['worst_componentwise_backward_error'] is None
                                         or error > row['worst_componentwise_backward_error']):
                    row['worst_componentwise_backward_error'] = error
                timing_samples, samples = _measurement_samples(result, timing)
                for phase, values in timing_samples.items():
                    row['_timing_samples'][phase].extend(values)
                row['samples'] += samples
                cpu_times, _ = _measurements(result, 'cpu_seconds')
                for phase, pair in _utilization_samples(result).items():
                    old = row['_utilization_pairs'][phase]
                    if pair is not None and (old is None or pair[0] < old[0]):
                        row['_utilization_pairs'][phase] = pair
                for phase, value in cpu_times.items():
                    old = row['cpu_times'][phase]
                    if value is not None and (old is None or value < old):
                        row['cpu_times'][phase] = value
                if samples and result.get('outcome') == 'success':
                    user, system = process.get('user_cpu_seconds'), process.get('system_cpu_seconds')
                    for name, value in (('user_cpu_seconds', user), ('system_cpu_seconds', system)):
                        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                            raise ValueError(f'{name} must be finite and nonnegative')
                    values = {'cpu_seconds': user + system if user is not None and system is not None else None,
                              'max_rss_bytes': process.get('max_rss_bytes')}
                    wall = process.get('wall_seconds')
                    if wall is not None and (type(wall) not in (int, float) or not math.isfinite(wall) or wall < 0):
                        raise ValueError('process wall_seconds must be finite and nonnegative')
                    old = row['_process_utilization_pair']
                    if wall and values['cpu_seconds'] is not None and (old is None or wall < old[0]):
                        row['_process_utilization_pair'] = (wall, values['cpu_seconds'], system)
                    for key, value in values.items():
                        if value is not None:
                            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                                raise ValueError(f'{key} must be finite and nonnegative')
                            old = row['process_resources'][key]
                            row['process_resources'][key] = value if old is None else min(old, value)
            if count == 0:
                raise ValueError('no benchmark results found')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f'{path}: {exc}') from exc
    for matrix in matrices.values():
        matrix['case_ids'] = sorted(matrix['case_ids'])
        matrix['rows'] = sorted((row for row in matrix['rows'].values() if row.pop('_baseline')), key=lambda row: (
            row['platform'], row['platform_id'], row['solver'], row['configuration_id']))
        for row in matrix['rows']:
            samples = row.pop('_timing_samples')
            row['times'] = {phase: min(values, default=None) for phase, values in samples.items()}
            row['median_times'] = {phase: _median(values) for phase, values in samples.items()}
        matrix['best_times'] = {
            phase: min((row['times'][phase] for row in matrix['rows']
                        if row['times'][phase] is not None), default=None)
            for phase in (*PHASES, 'critical_path')}
        matrix['platform_best_times'] = {
            platform_id: {phase: min((r['times'][phase] for r in matrix['rows']
                                      if r['comparison_platform_id'] == platform_id and r['times'][phase] is not None), default=None)
                          for phase in (*PHASES, 'critical_path')}
            for platform_id in sorted({r['comparison_platform_id'] for r in matrix['rows']})}
        matrix['platform_best_median_times'] = {
            platform_id: {phase: min((r['median_times'][phase] for r in matrix['rows']
                                      if r['comparison_platform_id'] == platform_id and r['median_times'][phase] is not None), default=None)
                          for phase in (*PHASES, 'critical_path')}
            for platform_id in sorted({r['comparison_platform_id'] for r in matrix['rows']})}
        for row in matrix['rows']:
            row['cpu_utilization'] = {phase: None if pair is None else pair[1] / pair[0]
                                      for phase, pair in row['_utilization_pairs'].items()}
            row['system_cpu_seconds'] = {
                phase: None if pair is None else pair[2]
                for phase, pair in row['_utilization_pairs'].items()}
            row['system_cpu_percent'] = {
                phase: None if pair is None or pair[2] is None or pair[1] == 0 else 100 * (pair[2] / pair[1])
                for phase, pair in row.pop('_utilization_pairs').items()}
            pair = row.pop('_process_utilization_pair')
            row['process_resources']['cpu_utilization'] = None if pair is None else pair[1] / pair[0]
            row['process_resources']['system_cpu_seconds'] = None if pair is None else pair[2]
            row['process_resources']['system_cpu_percent'] = (None if pair is None or pair[2] is None or pair[1] == 0
                                                               else 100 * (pair[2] / pair[1]))
            row['sources'] = sorted(row['sources'])
            row['outcomes'] = dict(sorted(row['outcomes'].items()))
            row['trace_outcomes'] = dict(sorted(row['trace_outcomes'].items()))
            row['relative_times'] = {}
            row['relative_median_times'] = {}
            for phase, value in row['times'].items():
                best = matrix['platform_best_times'][row['comparison_platform_id']][phase]
                row['relative_times'][phase] = (
                    None if value is None else
                    1.0 if value == best else
                    math.inf if best == 0 else value / best)
                value = row['median_times'][phase]
                best = matrix['platform_best_median_times'][row['comparison_platform_id']][phase]
                row['relative_median_times'][phase] = (
                    None if value is None else 1.0 if value == best else
                    math.inf if best == 0 else value / best)
    report['matrices'] = sorted((m for m in matrices.values() if m['rows']), key=lambda matrix: (
        matrix['case_ids'], matrix['input_hash']))
    return report


def _json_safe(value):
    """Preserve missing values and encode nonfinite diagnostics as strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return {'nonfinite': 'nan' if math.isnan(value) else '+inf' if value > 0 else '-inf'}
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def write_report(inputs, output, metric='wall', title='Solver benchmark report', *, compare_affinity=False):
    """Write HTML and sibling JSON from one summary; return both output paths."""
    paths = _source_paths(inputs)
    output = Path(output)
    json_output = output.with_suffix('.json')
    destinations = (output, json_output)
    if output.resolve() == json_output.resolve():
        raise ValueError('HTML and JSON report paths must be different; use --output report.html')
    protected = {path.resolve() for path in paths}
    protected.update((path.parent / name).resolve() for path in paths for name in METADATA_FILES)
    if any(path.resolve() in protected for path in destinations):
        raise ValueError('report output must not overwrite benchmark input artifacts')
    for path in destinations:
        if path.exists() and not path.is_file():
            raise ValueError(f'report output must be a file: {path}')
    if json_output.exists():
        try:
            previous = load_json(json_output)
        except (ValueError, OSError) as exc:
            raise ValueError(f'JSON destination is not an existing comparison report: {json_output}') from exc
        if not isinstance(previous, dict) or previous.get('report_type') != 'solver-benchmark-comparison' \
                or previous.get('schema_version') not in ('1.0', '1.1'):
            raise ValueError(f'JSON destination is not an existing comparison report: {json_output}')
    report = collect_report(paths, metric, compare_affinity=compare_affinity)
    report['title'] = title
    contents = (render_report(report, title=title),
                json.dumps(_json_safe(report), indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Prepare both files before replacing existing reports; a serialization/write
    # failure must not leave a newly written HTML file paired with old JSON.
    staged = []
    try:
        for content in contents:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                                             prefix='.solver-report-', suffix='.tmp', delete=False) as stream:
                staged.append(Path(stream.name))
                stream.write(content)
        for temporary, destination in zip(staged, destinations):
            temporary.replace(destination)
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
    return destinations
