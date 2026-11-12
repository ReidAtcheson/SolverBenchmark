import contextlib
import copy
from html.parser import HTMLParser
import io
import json
import math
from pathlib import Path
import tempfile
import unittest

from solver_benchmark.cli import main
from solver_benchmark.profiles import canonical_hash
from solver_benchmark.report import collect_report, write_report


PHASES = ('setup', 'pre_analyze', 'analysis', 'numeric', 'solve', 'teardown')
TIMINGS = (*PHASES, 'critical_path')
OPTIONS = {'thread_limit': 1, 'relative_tolerance': 1e-10,
           'absolute_tolerance': 1e-12, 'iteration_limit': '10000'}
CASE = {
    'id': 'matrix',
    'generator': {'id': 'laplacian_1d', 'parameters': {'mx': 3, 'dx': 0.5}, 'seed': '0'},
    'rhs': {'generator': {'id': 'manufactured_constant_columns',
                          'parameters': {'first_value': 1, 'column_increment': 1},
                          'seed': '0'}, 'count': 1},
    'data_format': 'csr-f64-i32-i64-le', 'encoding': {'kind': 'real'},
}
HOST = {'system': 'Linux', 'kernel': 'test-kernel', 'machine': 'x86_64',
        'cpu': 'Test CPU', 'parent_libc': ['glibc', '2.39'],
        'cpu_count': 2, 'affinity': [0, 1]}


def result(solver='solver-a', *, case='matrix', input_id='a', outcome='success'):
    return {
        'case_id': case, 'solver_id': solver, 'pass_id': 'timing', 'outcome': outcome,
        'provenance': {'input_hash': 'sha256:' + input_id * 64},
        'instrumentation': {'kind': 'baseline', 'collectors': []},
        'options': dict(OPTIONS), 'effective_thread_environment': {'OMP_NUM_THREADS': '1'},
        'process': {'host': 'test-host', 'affinity': [0, 1]},
        'worker': {'name': solver, 'version': '1.0', 'build_id': 'test-build',
                   'adapter_api_version': 1},
        'phases': [], 'teardowns': [], 'accuracy': [],
    }


def add_trial(row, values, *, index=0, kind='measured', accepted=True, errors=None):
    trial = {'kind': kind, 'index': index}
    accuracy = {'accepted': accepted}
    if errors is not None:
        accuracy['columns'] = [{'componentwise_backward_error': value} for value in errors]
    row['accuracy'].append({'trial': dict(trial), 'accuracy': accuracy})
    for phase, value in zip(PHASES, values):
        record = {'trial': dict(trial), 'completed': True,
                  'times': {'wall_seconds': value, 'cpu_seconds': value * 2}}
        if phase == 'teardown':
            row['teardowns'].append(record)
        else:
            record.update(phase=phase, result={'status': 'success'})
            row['phases'].append(record)
    return row


class Tables(HTMLParser):
    """Read actual rendered table cells without depending on CSS or whitespace."""

    def __init__(self, html):
        super().__init__()
        self.tables = []
        self.cells = []
        self.table = self.row = self.cell = None
        self.caption = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.table = {'caption': '', 'rows': []}
            self.tables.append(self.table)
        elif tag == 'caption' and self.table is not None:
            self.caption = True
        elif tag == 'tr' and self.table is not None:
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = ''
            self.cell_attrs = dict(attrs)
        elif tag == 'br' and self.cell is not None:
            self.cell += ' '

    def handle_data(self, data):
        if self.caption:
            self.table['caption'] += data
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag == 'caption':
            self.caption = False
        elif tag in ('td', 'th') and self.cell is not None:
            self.row.append(self.cell.strip())
            self.cells.append({'text': self.cell.strip(), 'attrs': self.cell_attrs})
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.table['rows'].append(self.row)
            self.row = None
        elif tag == 'table':
            self.table = None


class Reports(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def read_report_json(self, path):
        def reject_constant(value):
            self.fail(f'report contains nonstandard JSON constant: {value}')
        return json.loads(path.read_text(encoding='utf-8'), parse_constant=reject_constant)

    def output(self, name, rows, *, host=None, orchestrator='one', worker_build=None,
               arguments=None):
        directory = self.base / name
        directory.mkdir()
        rows = copy.deepcopy(rows)
        cases = {row['case_id']: {**copy.deepcopy(CASE), 'id': row['case_id']} for row in rows}
        solvers = {row['solver_id']: {'id': row['solver_id'], 'options': row['options']}
                   for row in rows}
        profile = {'schema_version': '1.0', 'name': name, 'cases': list(cases.values()),
                   'solvers': list(solvers.values()), 'passes': [
                       {'id': 'timing', 'warmup_runs': 1, 'measured_runs': 3,
                        'case_timeout_seconds': 30,
                        'instrumentation': {'kind': 'baseline', 'collectors': []}}]}
        workers = {row['solver_id']: {
            'identity': row['worker'], 'executable_hash': 'sha256:' + 'b' * 64,
            'runtime_dependency_hashes': {},
        } for row in rows}
        if worker_build is not None:
            for worker in workers.values():
                worker['executable_hash'] = 'sha256:' + worker_build * 64
        manifest = {'schema_version': '1.0', 'protocol_version': '1.0',
                    'orchestrator': {'version': orchestrator}, 'workers': workers}
        execution = {'run_id': name, 'host': copy.deepcopy(HOST if host is None else host),
                     'profile_hash': canonical_hash(profile),
                     'version_manifest_hash': canonical_hash(manifest),
                     'deployment': {'schema_version': '1.0', 'solvers': {
                         solver: {'executable': '/test/worker',
                                  'arguments': [] if arguments is None else arguments,
                                  'environment': {}}
                         for solver in solvers}}}
        for row in rows:
            row['provenance'].update(profile_hash=execution['profile_hash'],
                                     version_manifest_hash=execution['version_manifest_hash'])
        for filename, value in [('execution.json', execution), ('profile.json', profile),
                                ('version-manifest.json', manifest)]:
            (directory / filename).write_text(json.dumps(value), encoding='utf-8')
        (directory / 'results.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        return directory

    def test_fastest_phases_and_same_trial_critical_path(self):
        row = result(outcome='accuracy_failed')
        add_trial(row, [8, 7, 1, 8, 5, 3])
        add_trial(row, [4, 5, 8, 1, 6, 2], index=1)
        add_trial(row, [0.001] * 6, kind='warmup')
        add_trial(row, [0.01] * 6, index=2, accepted=False)
        trace = add_trial(result(), [0.0001] * 6)
        trace['instrumentation'] = {'kind': 'trace', 'collectors': ['linux.phase_resources']}
        trace['pass_id'] = 'trace'
        directory = self.output('run', [row, trace])

        report = collect_report([directory])
        self.assertEqual(report['metric'], 'wall_seconds')
        self.assertEqual(report['excluded_trace_results'], 1)
        measured = report['matrices'][0]['rows'][0]
        self.assertEqual(measured['samples'], 2)
        self.assertEqual(measured['times'], dict(zip(TIMINGS, [4, 5, 1, 1, 5, 2, 14])))
        self.assertEqual(measured['outcomes'], {'accuracy_failed': 1})

        cpu = collect_report([directory], metric='cpu')
        self.assertEqual(cpu['metric'], 'cpu_seconds')
        self.assertEqual(cpu['matrices'][0]['rows'][0]['times'],
                         dict(zip(TIMINGS, [8, 10, 2, 2, 10, 4, 28])))

    def test_multiple_outputs_platforms_and_duplicate_paths(self):
        first = self.output('first', [add_trial(result(), [10] * 6),
                                      add_trial(result('solver-b'), [12] * 6)])
        second = self.output('second', [add_trial(result(), [8] * 6)],
                             host={**HOST, 'cpu': 'Another CPU', 'machine': 'aarch64'})
        third = self.output('third', [add_trial(result(), [9] * 6)], orchestrator='two')
        report = collect_report([first, second / 'results.jsonl', third, first / 'results.jsonl'])
        self.assertEqual(len(report['sources']), 3)
        self.assertEqual(len(report['matrices']), 1)
        rows = report['matrices'][0]['rows']
        self.assertEqual(len(rows), 3)
        first_a = next(row for row in rows if row['solver'] == 'solver-a'
                       and row['host']['cpu'] == HOST['cpu'])
        self.assertEqual(first_a['times']['analysis'], 9)
        self.assertEqual(first_a['samples'], 2)
        self.assertEqual(first_a['sources'], [1, 3])
        self.assertEqual(len({row['platform_id'] for row in rows}), 2)

    def test_inputs_separate_matrices_and_tolerances_separate_solver_rows(self):
        first = self.output('first', [add_trial(result(), [1] * 6)])
        second = self.output('second', [add_trial(result(input_id='c'), [2] * 6)])
        loose = add_trial(result(), [3] * 6)
        loose['options']['relative_tolerance'] = 1e-3
        third = self.output('third', [loose])
        report = collect_report([first, second, third])
        self.assertEqual(len(report['matrices']), 2)
        self.assertTrue(all(matrix['case_ids'] == ['matrix'] for matrix in report['matrices']))
        original = next(matrix for matrix in report['matrices']
                        if matrix['input_hash'] == 'sha256:' + 'a' * 64)
        self.assertEqual(len(original['rows']), 2)
        self.assertEqual({row['configuration']['options']['relative_tolerance']
                          for row in original['rows']}, {1e-10, 1e-3})

    def test_worst_error_includes_rejections_rhs_trials_and_outputs(self):
        baseline = add_trial(result(outcome='accuracy_failed'), [1] * 6,
                             errors=[1e-8, 3e-8])
        add_trial(baseline, [2] * 6, index=1, errors=[5e-8, 2e-8])
        add_trial(baseline, [0.1] * 6, index=2, accepted=False, errors=[7e-8, 4e-8])
        add_trial(baseline, [0.01] * 6, kind='warmup', errors=[1000])
        trace = add_trial(result(), [0.001] * 6, errors=[10000])
        trace['instrumentation'] = {'kind': 'trace', 'collectors': ['linux.phase_resources']}
        trace['pass_id'] = 'trace'
        first = self.output('first', [baseline, trace])
        second = self.output('second', [add_trial(result(), [3] * 6, errors=[6e-8, 9e-8])])

        first_row = collect_report([first])['matrices'][0]['rows'][0]
        self.assertEqual(first_row['worst_componentwise_backward_error'], 7e-8)
        self.assertEqual(first_row['samples'], 2)
        for metric in ('wall', 'cpu'):
            with self.subTest(metric=metric):
                rows = collect_report([first, second], metric=metric)['matrices'][0]['rows']
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['worst_componentwise_backward_error'], 9e-8)
                self.assertEqual(rows[0]['samples'], 3)

    def test_error_column_is_raw_dimensionless_for_each_table_and_metric(self):
        missing = add_trial(result('missing'), [1] * 6, errors=[None])
        missing['accuracy'][0]['accuracy']['columns'].append({})
        directory = self.output('errors', [
            missing,
            add_trial(result('zero'), [1] * 6, errors=[0]),
            add_trial(result('finite'), [1] * 6, errors=[1.25e-8]),
            add_trial(result('infinite', outcome='accuracy_failed'), [1] * 6,
                      accepted=False, errors=[{'nonfinite': '+inf'}]),
        ])
        report_rows = {row['solver']: row for row in collect_report([directory])['matrices'][0]['rows']}
        self.assertIsNone(report_rows['missing']['worst_componentwise_backward_error'])
        self.assertEqual(report_rows['zero']['worst_componentwise_backward_error'], 0)
        self.assertEqual(report_rows['finite']['worst_componentwise_backward_error'], 1.25e-8)
        self.assertEqual(report_rows['infinite']['worst_componentwise_backward_error'], math.inf)
        expected = {'missing': '—', 'zero': '0', 'finite': '1.25e-08', 'infinite': '∞'}
        for metric in ('wall', 'cpu'):
            with self.subTest(metric=metric):
                output = self.base / f'{metric}-errors.html'
                write_report([directory], output, metric=metric)
                html = output.read_text(encoding='utf-8')
                self.assertNotIn('Relative tolerance:', html)
                self.assertNotIn('Absolute tolerance:', html)
                parsed = Tables(html)
                self.assertEqual(len(parsed.tables), 2)
                for table in parsed.tables:
                    self.assertEqual(' '.join(table['rows'][0][-8].split()),
                                     'Worst componentwise backward error')
                    for solver, value in expected.items():
                        row = next(row for row in table['rows'] if row[1].startswith(solver))
                        self.assertEqual(row[-8], value)
                for cell in parsed.cells:
                    if cell['text'] in expected.values():
                        self.assertNotIn('seconds', cell['attrs'].get('title', ''))

    def test_invalid_backward_errors_are_rejected(self):
        for index, value in enumerate((-1, 'invalid', True, {'nonfinite': 'invalid'})):
            with self.subTest(value=value):
                row = add_trial(result(), [1] * 6, errors=[value])
                directory = self.output(f'invalid-error-{index}', [row])
                with self.assertRaises(ValueError):
                    collect_report([directory])

    def test_solver_configuration_and_worker_builds_remain_distinct(self):
        first = self.output('first', [add_trial(result(), [1] * 6)])
        threaded = add_trial(result(), [2] * 6)
        threaded['options']['thread_limit'] = 2
        threaded['effective_thread_environment']['OMP_NUM_THREADS'] = '2'
        second = self.output('second', [threaded])
        third = self.output('third', [add_trial(result(), [3] * 6)], worker_build='c')
        rows = collect_report([first, second, third])['matrices'][0]['rows']
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row['configuration_id'] for row in rows}), 3)

    def test_worker_arguments_and_recorded_process_environment_remain_distinct(self):
        first = self.output('first', [add_trial(result(), [1] * 6)])
        second = self.output('second', [add_trial(result(), [2] * 6)],
                             arguments=['--ordering', 'natural'])
        bound = add_trial(result(), [3] * 6)
        bound['process']['environment'] = {'OMP_PROC_BIND': 'spread', 'OMP_PLACES': 'cores'}
        third = self.output('third', [bound])
        rows = collect_report([first, second, third])['matrices'][0]['rows']
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row['configuration_id'] for row in rows}), 3)
        arguments = next(row for row in rows if row['sources'] == [2])
        self.assertEqual(arguments['configuration']['arguments'], ['--ordering', 'natural'])
        environment = next(row for row in rows if row['sources'] == [3])
        self.assertEqual(environment['configuration']['process_environment'],
                         {'OMP_PROC_BIND': 'spread', 'OMP_PLACES': 'cores'})

    def test_trace_only_output_has_no_baseline_comparisons(self):
        trace = add_trial(result(), [1] * 6)
        trace['instrumentation'] = {'kind': 'trace', 'collectors': ['linux.phase_resources']}
        trace['pass_id'] = 'trace'
        directory = self.output('trace', [trace])
        report = collect_report([directory])
        self.assertEqual(report['matrices'], [])
        self.assertEqual(report['excluded_trace_results'], 1)
        self.assertEqual(len(report['sources']), 1)
        output = self.base / 'trace.html'
        write_report([directory], output)
        self.assertIn('No baseline results', output.read_text(encoding='utf-8'))
        exported = self.read_report_json(output.with_suffix('.json'))
        self.assertEqual(exported['matrices'], [])
        self.assertEqual(exported['excluded_trace_results'], 1)
        self.assertEqual(exported['sources'][0]['id'], 1)

    def test_json_preserves_precision_and_matches_html_for_each_metric(self):
        precise_time = 0.12345678901234567
        precise_error = 2.345678901234567e-12
        directory = self.output('precision', [
            add_trial(result('best'), [0.1] * 6, errors=[0.5e-12]),
            add_trial(result('precise'), [precise_time] * 6, errors=[precise_error]),
        ])
        for metric, scale in (('wall', 1), ('cpu', 2)):
            with self.subTest(metric=metric):
                output = self.base / f'{metric}-precision.html'
                paths = write_report([directory], output, metric=metric, title='Precision report')
                self.assertEqual(paths, (output, output.with_suffix('.json')))
                exported = self.read_report_json(paths[1])
                self.assertEqual(exported['schema_version'], '1.0')
                self.assertEqual(exported['report_type'], 'solver-benchmark-comparison')
                self.assertTrue(exported['aggregation'])
                matrix = exported['matrices'][0]
                precise = next(row for row in matrix['rows'] if row['solver'] == 'precise')
                self.assertEqual(precise['times']['analysis'], precise_time * scale)
                self.assertNotEqual(precise['times']['analysis'], float(f'{precise_time * scale:.6g}'))
                self.assertEqual(precise['worst_componentwise_backward_error'], precise_error)
                self.assertEqual(matrix['best_times']['analysis'], 0.1 * scale)
                self.assertEqual(precise['relative_times']['analysis'],
                                 (precise_time * scale) / (0.1 * scale))
                self.assertEqual(set(matrix['best_times']), set(TIMINGS))
                self.assertEqual(set(precise['relative_times']), set(TIMINGS))
                tables = Tables(paths[0].read_text(encoding='utf-8')).tables
                self.assertEqual(len(tables), 2)
                for table, key in zip(tables, ('times', 'relative_times')):
                    for json_row in matrix['rows']:
                        html_row = next(row for row in table['rows']
                                        if row[1].startswith(json_row['solver']))
                        self.assertEqual(html_row[-7:], [f'{json_row[key][phase]:.6g}'
                                                         for phase in TIMINGS])
                original = paths[1].read_bytes()
                write_report([directory], output, metric=metric, title='Precision report')
                self.assertEqual(paths[1].read_bytes(), original)

    def test_json_uses_null_for_missing_and_tags_for_infinities(self):
        directory = self.output('nonfinite', [
            add_trial(result('zero'), [0] * 6, errors=[0]),
            add_trial(result('positive'), [1] * 6, errors=[1e-12]),
            add_trial(result('failed', outcome='accuracy_failed'), [1] * 6,
                      accepted=False, errors=[{'nonfinite': '+inf'}]),
            result('unavailable', outcome='unsupported'),
        ])
        output = self.base / 'nonfinite.html'
        write_report([directory], output)
        exported = self.read_report_json(output.with_suffix('.json'))
        matrix = exported['matrices'][0]
        rows = {row['solver']: row for row in matrix['rows']}
        self.assertEqual(matrix['best_times'], dict.fromkeys(TIMINGS, 0))
        self.assertEqual(rows['zero']['relative_times'], dict.fromkeys(TIMINGS, 1))
        self.assertEqual(rows['positive']['relative_times'],
                         dict.fromkeys(TIMINGS, {'nonfinite': '+inf'}))
        self.assertEqual(rows['failed']['worst_componentwise_backward_error'], {'nonfinite': '+inf'})
        self.assertEqual(rows['failed']['outcomes'], {'accuracy_failed': 1})
        self.assertEqual(rows['failed']['times'], dict.fromkeys(TIMINGS))
        self.assertEqual(rows['failed']['relative_times'], dict.fromkeys(TIMINGS))
        self.assertIsNone(rows['unavailable']['worst_componentwise_backward_error'])
        relative = next(table for table in Tables(output.read_text(encoding='utf-8')).tables
                        if table['caption'].startswith('Relative'))
        positive = next(row for row in relative['rows'] if row[1].startswith('positive'))
        failed = next(row for row in relative['rows'] if row[1].startswith('failed'))
        self.assertEqual(positive[-7:], ['∞'] * 7)
        self.assertEqual(failed[-8:], ['∞', *(['—'] * 7)])

    def test_duplicate_measured_phases_are_rejected(self):
        row = add_trial(result(), [1] * 6)
        row['phases'].append(copy.deepcopy(row['phases'][2]))
        directory = self.output('duplicate', [row])
        with self.assertRaisesRegex(ValueError, 'duplicate measured phase'):
            collect_report([directory])

    def test_incomplete_phases_and_failed_solvers_are_visible(self):
        partial = add_trial(result(), [1] * 6)
        partial['phases'][2]['completed'] = False
        partial['phases'][3]['result']['status'] = 'not_applicable'
        partial['teardowns'][0]['times'] = None
        failed = result('failed-solver', outcome='worker_crashed')
        directory = self.output('run', [partial, failed])
        rows = {row['solver']: row for row in collect_report([directory])['matrices'][0]['rows']}
        self.assertIsNone(rows['solver-a']['times']['analysis'])
        self.assertIsNone(rows['solver-a']['times']['critical_path'])
        self.assertIsNone(rows['solver-a']['times']['teardown'])
        self.assertEqual(rows['solver-a']['times']['numeric'], 1)
        self.assertEqual(rows['failed-solver']['outcomes'], {'worker_crashed': 1})
        self.assertTrue(all(value is None for value in rows['failed-solver']['times'].values()))

    def test_absolute_and_column_best_relative_tables(self):
        directory = self.output('run', [
            add_trial(result('solver-a'), [10, 20, 30, 40, 50, 60]),
            add_trial(result('solver-b'), [12, 10, 60, 20, 75, 30]),
            add_trial(result('solver-c', input_id='c', case='second-matrix'), [2] * 6),
        ])
        output = self.base / 'report.html'
        write_report([directory], output)
        tables = Tables(output.read_text(encoding='utf-8')).tables
        timing_tables = [table for table in tables
                         if any('solver-a' in cell for row in table['rows'] for cell in row)]
        self.assertEqual(len(timing_tables), 2)
        absolute, relative = timing_tables

        def values(table, solver):
            row = next(row for row in table['rows'] if row[1].startswith(solver))
            return [float(value.rstrip('×x').strip()) for value in row[-7:]]

        self.assertEqual(values(absolute, 'solver-a'), [10, 20, 30, 40, 50, 60, 120])
        for actual, expected in zip(values(relative, 'solver-a'), [1, 2, 1, 2, 1, 2, 1]):
            self.assertAlmostEqual(actual, expected, places=4)
        for actual, expected in zip(values(relative, 'solver-b'), [1.2, 1, 2, 1, 1.5, 1, 155 / 120]):
            self.assertAlmostEqual(actual, expected, places=3)
        self.assertEqual(len([table for table in tables
                              if any('solver-c' in cell for row in table['rows'] for cell in row)]), 2)

    def test_relative_best_can_be_on_another_platform(self):
        first = self.output('first', [add_trial(result(), [12] * 6)])
        second = self.output('second', [add_trial(result(), [10] * 6)],
                             host={**HOST, 'cpu': 'Faster CPU'})
        output = self.base / 'platforms.html'
        write_report([first, second], output)
        table = next(table for table in Tables(output.read_text(encoding='utf-8')).tables
                     if table['caption'].startswith('Relative'))
        slow = next(row for row in table['rows'] if row[0].startswith('Test CPU'))
        fast = next(row for row in table['rows'] if row[0].startswith('Faster CPU'))
        self.assertEqual([float(value) for value in slow[-7:]], [1.2] * 7)
        self.assertEqual([float(value) for value in fast[-7:]], [1] * 7)

    def test_zero_timings_missing_results_and_escaped_html(self):
        malicious = 'solver<script>alert("bad")</script>&'
        directory = self.output('run', [add_trial(result(malicious), [0] * 6),
                                       add_trial(result('positive'), [1] * 6),
                                       result('unavailable', outcome='unsupported')])
        output = self.base / 'escaped.html'
        write_report([directory], output, title='<Report & comparisons>')
        html = output.read_text(encoding='utf-8')
        self.assertIn('&lt;Report &amp; comparisons&gt;', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>alert', html)
        self.assertIn('unsupported', html)
        relative = next(table for table in Tables(html).tables
                        if table['caption'].startswith('Relative'))
        zero = next(row for row in relative['rows'] if row[1].startswith(malicious))
        positive = next(row for row in relative['rows'] if row[1].startswith('positive'))
        unavailable = next(row for row in relative['rows'] if row[1].startswith('unavailable'))
        self.assertEqual(zero[-7:], ['1'] * 7)
        self.assertEqual(positive[-7:], ['∞'] * 7)
        self.assertEqual(unavailable[-7:], ['—'] * 7)

    def test_invalid_timing_is_rejected(self):
        for index, value in enumerate((-1, float('nan'), float('inf'))):
            with self.subTest(value=value):
                row = add_trial(result(), [1] * 6)
                row['phases'][2]['times']['wall_seconds'] = value
                directory = self.output(f'invalid-{index}', [row])
                with self.assertRaises(ValueError):
                    collect_report([directory])

    def test_missing_metadata_and_malformed_jsonl_fail_without_output(self):
        directory = self.output('missing', [add_trial(result(), [1] * 6)])
        (directory / 'execution.json').unlink()
        with self.assertRaises((ValueError, OSError)):
            collect_report([directory])
        directory = self.output('malformed', [add_trial(result(), [1] * 6)])
        (directory / 'results.jsonl').write_text('{not JSON}\n', encoding='utf-8')
        output = self.base / 'not-written.html'
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = main(['report', str(directory), '--output', str(output)])
        self.assertEqual(code, 2)
        self.assertIn('error:', errors.getvalue())
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix('.json').exists())

    def test_report_cannot_overwrite_source_artifacts(self):
        directory = self.output('run', [add_trial(result(), [1] * 6)])
        for filename in ('results.jsonl', 'profile.json', 'execution.json', 'version-manifest.json'):
            with self.subTest(filename=filename):
                path = directory / filename
                original = path.read_bytes()
                with self.assertRaises(ValueError):
                    write_report([directory], path)
                self.assertEqual(path.read_bytes(), original)

    def test_sidecar_cannot_overwrite_metadata_or_alias_html(self):
        directory = self.output('run', [add_trial(result(), [1] * 6)])
        for filename in ('profile.json', 'execution.json', 'version-manifest.json'):
            with self.subTest(filename=filename):
                metadata = directory / filename
                original = metadata.read_bytes()
                output = metadata.with_suffix('.html')
                with self.assertRaises(ValueError):
                    write_report([directory], output)
                self.assertEqual(metadata.read_bytes(), original)
                self.assertFalse(output.exists())

        same_path = self.base / 'report.json'
        with self.assertRaises(ValueError):
            write_report([directory], same_path)
        self.assertFalse(same_path.exists())

        output = self.base / 'aliased.html'
        output.write_text('existing report', encoding='utf-8')
        sidecar = output.with_suffix('.json')
        sidecar.symlink_to(output)
        with self.assertRaises(ValueError):
            write_report([directory], output)
        self.assertEqual(output.read_text(encoding='utf-8'), 'existing report')
        self.assertTrue(sidecar.is_symlink())

    def test_sidecar_preserves_benchmark_summary_and_unrelated_json(self):
        directory = self.output('run', [add_trial(result(), [1] * 6)])
        cases = [
            (directory / 'summary.json', {'outcomes': {'success': 1}, 'execution_count': 1}),
            (self.base / 'unrelated.json', {'measurements': [1, 2, 3]}),
        ]
        for sidecar, contents in cases:
            with self.subTest(sidecar=sidecar.name):
                original_json = json.dumps(contents, indent=2).encode('utf-8') + b'\n'
                sidecar.write_bytes(original_json)
                output = sidecar.with_suffix('.html')
                original_html = b'<html>Existing analysis</html>\n'
                output.write_bytes(original_html)
                with self.assertRaises(ValueError):
                    write_report([directory], output)
                self.assertEqual(sidecar.read_bytes(), original_json)
                self.assertEqual(output.read_bytes(), original_html)

    def test_cli_writes_cpu_report_from_multiple_inputs(self):
        first = self.output('first', [add_trial(result(), [1] * 6)])
        second = self.output('second', [add_trial(result('solver-b'), [2] * 6)])
        output = self.base / 'cli.html'
        with contextlib.redirect_stdout(io.StringIO()) as messages:
            code = main(['report', str(first), str(second / 'results.jsonl'),
                         '--output', str(output), '--metric', 'cpu', '--title', 'CPU comparison'])
        self.assertEqual(code, 0)
        html = output.read_text(encoding='utf-8')
        self.assertIn('CPU comparison', html)
        self.assertIn('solver-a', html)
        self.assertIn('solver-b', html)
        sidecar = output.with_suffix('.json')
        exported = self.read_report_json(sidecar)
        self.assertEqual(exported['title'], 'CPU comparison')
        self.assertEqual(exported['metric'], 'cpu_seconds')
        self.assertEqual([source['id'] for source in exported['sources']], [1, 2])
        self.assertEqual([source['run_id'] for source in exported['sources']], ['first', 'second'])
        rows = {row['solver']: row for row in exported['matrices'][0]['rows']}
        self.assertEqual(rows['solver-a']['sources'], [1])
        self.assertEqual(rows['solver-b']['sources'], [2])
        self.assertEqual(rows['solver-b']['times']['analysis'], 4)
        self.assertEqual(rows['solver-b']['relative_times']['analysis'], 2)
        self.assertIn(str(output), messages.getvalue())
        self.assertIn(str(sidecar), messages.getvalue())


if __name__ == '__main__':
    unittest.main()
