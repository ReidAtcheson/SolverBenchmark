"""Collect baseline measurements from benchmark artifacts for offline HTML reports."""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path

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


def _measurements(result, metric):
    """Return independent phase minima and the minimum complete trial core sum."""
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
    minima = dict.fromkeys((*PHASES, 'critical_path'))
    for times in trials.values():
        for phase, value in times.items():
            if minima[phase] is None or value < minima[phase]:
                minima[phase] = value
        if all(phase in times for phase in CORE_PHASES):
            core = sum(times[phase] for phase in CORE_PHASES)
            if not math.isfinite(core):
                raise ValueError('critical path sum must be finite')
            if minima['critical_path'] is None or core < minima['critical_path']:
                minima['critical_path'] = core
    return minima, len(accepted)


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


def collect_report(inputs, metric='wall'):
    """Pool comparable runs without mixing inputs, solver settings, or platforms.

    Matrix identity is the delivered matrix/RHS input hash. Rows retain all solver
    options (including tolerances), binary/dependency identity, thread environment,
    and platform metadata. Only baseline passes contribute timings and errors.
    """
    if metric not in METRICS:
        raise ValueError('metric must be wall or cpu')
    timing = METRICS[metric]
    report = {'metric': timing, 'sources': [], 'matrices': [], 'excluded_trace_results': 0}
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
            report['sources'].append({'path': str(path), 'run_id': run_id})
            count = 0
            for result in _results(path):
                count += 1
                kind = _object(result.get('instrumentation'), 'instrumentation').get('kind')
                if kind == 'trace':
                    report['excluded_trace_results'] += 1
                    continue
                if kind != 'baseline':
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
                platform_id = canonical_hash(platform)
                configuration = {'options': options, 'worker': workers[solver],
                                 'arguments': deployment.get(solver, {}).get('arguments', []),
                                 'process_environment': process.get('environment', {}),
                                 'effective_thread_environment': result.get('effective_thread_environment', {})}
                configuration_id = canonical_hash(configuration)
                row_key = (platform_id, solver, configuration_id)
                if row_key not in matrix['rows']:
                    label = ' / '.join(str(v) for v in (
                        host.get('cpu') or 'Unknown CPU', host.get('machine'),
                        process.get('host')) if v)
                    matrix['rows'][row_key] = {
                        'platform': label, 'platform_id': platform_id, 'host': platform,
                        'solver': solver, 'configuration': configuration,
                        'configuration_id': configuration_id, 'sources': set(),
                        'outcomes': Counter(), 'samples': 0,
                        'worst_componentwise_backward_error': None,
                        'times': dict.fromkeys((*PHASES, 'critical_path'))}
                row = matrix['rows'][row_key]
                row['sources'].add(source_number)
                row['outcomes'][_text(result.get('outcome'), 'outcome')] += 1
                error = _worst_componentwise_backward_error(result)
                if error is not None and (row['worst_componentwise_backward_error'] is None
                                         or error > row['worst_componentwise_backward_error']):
                    row['worst_componentwise_backward_error'] = error
                times, samples = _measurements(result, timing)
                row['samples'] += samples
                for phase, value in times.items():
                    if value is not None and (row['times'][phase] is None or value < row['times'][phase]):
                        row['times'][phase] = value
            if count == 0:
                raise ValueError('no benchmark results found')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f'{path}: {exc}') from exc
    for matrix in matrices.values():
        matrix['case_ids'] = sorted(matrix['case_ids'])
        matrix['rows'] = sorted(matrix['rows'].values(), key=lambda row: (
            row['platform'], row['platform_id'], row['solver'], row['configuration_id']))
        for row in matrix['rows']:
            row['sources'] = sorted(row['sources'])
            row['outcomes'] = dict(sorted(row['outcomes'].items()))
    report['matrices'] = sorted(matrices.values(), key=lambda matrix: (
        matrix['case_ids'], matrix['input_hash']))
    return report


def write_report(inputs, output, metric='wall', title='Solver benchmark report'):
    """Write a standalone HTML file without changing any benchmark artifact."""
    paths = _source_paths(inputs)
    output = Path(output)
    protected = {path for path in paths}
    protected.update(path.parent / name for path in paths for name in METADATA_FILES)
    if output.resolve() in protected:
        raise ValueError('report output must not overwrite benchmark input artifacts')
    html = render_report(collect_report(paths, metric), title=title)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding='utf-8')
