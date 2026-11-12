import contextlib
import copy
from html.parser import HTMLParser
import io
import json
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


def add_trial(row, values, *, index=0, kind='measured', accepted=True):
    trial = {'kind': kind, 'index': index}
    row['accuracy'].append({'trial': dict(trial), 'accuracy': {'accepted': accepted}})
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

    def test_input_identity_and_accuracy_tolerances_separate_matrices(self):
        first = self.output('first', [add_trial(result(), [1] * 6)])
        second = self.output('second', [add_trial(result(input_id='c'), [2] * 6)])
        loose = add_trial(result(), [3] * 6)
        loose['options']['relative_tolerance'] = 1e-3
        third = self.output('third', [loose])
        report = collect_report([first, second, third])
        self.assertEqual(len(report['matrices']), 3)
        self.assertTrue(all(matrix['case_ids'] == ['matrix'] for matrix in report['matrices']))

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

    def test_report_cannot_overwrite_source_artifacts(self):
        directory = self.output('run', [add_trial(result(), [1] * 6)])
        for filename in ('results.jsonl', 'profile.json', 'execution.json', 'version-manifest.json'):
            with self.subTest(filename=filename):
                path = directory / filename
                original = path.read_bytes()
                with self.assertRaises(ValueError):
                    write_report([directory], path)
                self.assertEqual(path.read_bytes(), original)

    def test_cli_writes_cpu_report_from_multiple_inputs(self):
        first = self.output('first', [add_trial(result(), [1] * 6)])
        second = self.output('second', [add_trial(result('solver-b'), [2] * 6)])
        output = self.base / 'cli.html'
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(['report', str(first), str(second / 'results.jsonl'),
                         '--output', str(output), '--metric', 'cpu', '--title', 'CPU comparison'])
        self.assertEqual(code, 0)
        html = output.read_text(encoding='utf-8')
        self.assertIn('CPU comparison', html)
        self.assertIn('solver-a', html)
        self.assertIn('solver-b', html)


if __name__ == '__main__':
    unittest.main()
