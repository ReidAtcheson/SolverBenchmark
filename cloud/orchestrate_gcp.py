#!/usr/bin/env python3
"""Run a bounded GCP benchmark, retry confirmed capacity failures, and report.

Each invocation uses explicit identity, zones, and a new output directory. The
underlying launcher owns resource creation and deletion; this layer never
deletes resources and retries only after cleanup has been verified.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
CLOUD = ROOT / 'cloud'
sys.path.insert(0, str(CLOUD))
import run_gcp  # noqa: E402


def source_state(benchmark, cloud_profile=None):
    """Refuse source files omitted by make_source, excluding local outputs."""
    files = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--others', '--exclude-standard', '-z'],
                           check=True, capture_output=True, text=True).stdout.split('\0')
    included = {str(benchmark), 'cloud/gcp-worker.sh', 'cloud/single_core.py'}
    # Orchestration/verifier execute locally and are not needed on the VM.
    local_only = {'cloud/orchestrate_gcp.py', 'cloud/verify_gcp.py'}
    if cloud_profile is not None:
        try:
            local_only.add(str(Path(cloud_profile).resolve().relative_to(ROOT)))
        except ValueError:
            pass
    # An unknown untracked path may be a build input. Exclude known
    # local-only output, documentation and tests; fail closed on the rest.
    local_dirs = ('reports/', 'results/', 'tests/', 'docs/')
    omitted = sorted(f for f in files if f and f not in included and f not in local_only
                     and not f.startswith(local_dirs))
    status = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=no'],
                            check=True, capture_output=True, text=True).stdout
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                            check=True, capture_output=True, text=True).stdout.strip()
    tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'],
                             check=True, capture_output=True, text=True).stdout.split('\0')
    # Match make_source's path set and symlink exclusion. Include path, mode,
    # length and bytes so changes between attempts cannot silently change code.
    digest = hashlib.sha256()
    for name in sorted(set(filter(None, tracked + list(included)))):
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        stat = path.stat()
        digest.update(name.encode() + b'\0' + str(stat.st_mode & 0o7777).encode() + b'\0'
                      + str(stat.st_size).encode() + b'\0')
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return {'commit': commit, 'tracked_dirty': bool(status), 'tracked_status': status,
            'omitted_untracked': omitted, 'fingerprint': 'sha256:' + digest.hexdigest()}


def stockout(project, zone, uid):
    """Require a completed insert error for this exact Batch UID and zone."""
    if not isinstance(uid, str) or not re.fullmatch(r'[a-z0-9-]+', uid):
        return False
    try:
        result = subprocess.run(['gcloud', 'compute', 'operations', 'list', '--project', project,
                                 '--filter', 'targetLink~' + uid, '--format=json'],
                                capture_output=True, text=True, timeout=60, check=True)
        operations = json.loads(result.stdout)
        if not isinstance(operations, list):
            return False
        for op in operations:
            link = op.get('targetLink', '')
            if (op.get('operationType') != 'insert' or op.get('status') != 'DONE'
                    or not isinstance(link, str)
                    or f'/zones/{zone}/instances/{uid}-' not in link):
                continue
            errors = op.get('error', {}).get('errors', [])
            if any(e.get('code') in ('ZONE_RESOURCE_POOL_EXHAUSTED',
                                     'ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS') for e in errors):
                return True
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        pass
    return False


def verify_attempt(project, region, zone, manifest_path):
    """Call the ownership-aware verifier; errors remain unverified."""
    try:
        import verify_gcp
        return verify_gcp.verify_attempt(project, region, zone, manifest_path)
    except Exception as exc:
        return {'verified': False, 'error': str(exc)}


def write_summary(path, summary):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(summary, indent=2) + '\n')
    os.replace(tmp, path)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return 'sha256:' + digest.hexdigest()


def cleanup_wait_seconds(target_count):
    """Budget for bounded launcher cleanup after an interrupt.

    Each job deletion can re-check ownership before every delete retry and once
    more afterwards. Include two ambiguous-create ownership checks, the final
    bucket ownership check, bucket deletion, retry delays, and a small allowance
    for logging and local I/O.
    """
    tries = run_gcp.RETRIES
    command = run_gcp.COMMAND_TIMEOUT_SECONDS
    read = tries * command + (tries - 1)
    per_job = tries * (read + command) + (tries - 1) + read
    return target_count * per_job + 3 * read + command + 2 * command


def execute_attempt(command, log, timeout_seconds, cleanup_timeout_seconds):
    """Forward cancellation to the launcher's process group and await cleanup."""
    with log.open('w') as stream:
        proc = None
        interrupted = None
        forwarded = False
        watchdog = False
        forced_kill = False
        cleanup_deadline = None
        old = {}
        def deliver(signum):
            nonlocal forwarded
            if proc is not None and not forwarded:
                forwarded = True
                try:
                    os.killpg(proc.pid, signum)
                except ProcessLookupError:
                    pass
        def forward(signum, _frame):
            nonlocal interrupted, cleanup_deadline
            if interrupted is None:
                interrupted = signum
                cleanup_deadline = time.monotonic() + cleanup_timeout_seconds
                deliver(signum)
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                old[signum] = signal.signal(signum, forward)
            proc = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            if interrupted is not None:
                deliver(interrupted)
            needs_cleanup_wait = False
            deadline = time.monotonic() + timeout_seconds
            while True:
                if interrupted is not None:
                    needs_cleanup_wait = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    watchdog = True
                    cleanup_deadline = time.monotonic() + cleanup_timeout_seconds
                    deliver(signal.SIGTERM)
                    needs_cleanup_wait = True
                    break
                try:
                    # A short wait makes a user signal take effect promptly
                    # without raising asynchronous exceptions inside Popen.
                    code = proc.wait(timeout=min(remaining, 1))
                    break
                except subprocess.TimeoutExpired:
                    pass
            if needs_cleanup_wait:
                try:
                    code = proc.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    forced_kill = True
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        code = proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        code = None
        finally:
            for signum, handler in old.items():
                signal.signal(signum, handler)
        return code, interrupted, watchdog, forced_kill


def report_preflight():
    """Fail before launch if the invoking Python cannot produce reports."""
    from solver_benchmark.report import write_report
    return write_report


def benchmark_preflight(path):
    from solver_benchmark.profiles import load_json, validate_profile
    try:
        return validate_profile(load_json(path))
    except Exception as exc:
        raise ValueError(f'invalid benchmark profile: {exc}') from exc


def read_snapshot_profile(path, benchmark):
    from solver_benchmark.profiles import validate_profile
    try:
        with tarfile.open(path, 'r:gz') as archive:
            return validate_profile(json.load(archive.extractfile(benchmark)))
    except Exception as exc:
        raise ValueError(f'invalid benchmark profile in source snapshot: {exc}') from exc


def create_report(inputs, destination):
    from solver_benchmark.report import write_report
    write_report(inputs, destination)


def result_summary(outputs, profile):
    """Summarize recorded outcomes, absent executions, and input differences."""
    requested = {s['id'] for s in profile['solvers']}
    expected = [(c['id'], s['id'], p['id']) for c in profile['cases']
                for s in profile['solvers'] for p in profile['passes']]
    targets, hashes = {}, {}
    for target, path in outputs.items():
        rows = [json.loads(line) for line in (path / 'results.jsonl').read_text().splitlines() if line.strip()]
        observed = set()
        outcomes = Counter()
        solver_outcomes = {}
        for row in rows:
            observed.add((row['case_id'], row['solver_id'], row['pass_id']))
            outcomes[row['outcome']] += 1
            solver_outcomes.setdefault(row['solver_id'], Counter())[row['outcome']] += 1
            value = row.get('provenance', {}).get('input_hash')
            if value:
                hashes.setdefault(row['case_id'], {}).setdefault(target, set()).add(value)
        missing = [{'case_id': c, 'solver_id': s, 'pass_id': p}
                   for c, s, p in expected if (c, s, p) not in observed]
        available = None
        try:
            deployment = json.loads((path / 'execution.json').read_text())['deployment']['solvers']
            if isinstance(deployment, dict) and all(isinstance(k, str) and k for k in deployment):
                available = sorted(deployment)
        except (OSError, ValueError, TypeError, KeyError):
            pass
        targets[target] = {'outcomes': dict(outcomes), 'expected': len(expected),
                           'recorded': len(expected) - len(missing), 'missing': missing,
                           'observed_solvers': sorted(solver_outcomes),
                           'solver_outcomes': {solver: dict(counts) for solver, counts in solver_outcomes.items()},
                           'deployment_solvers': available,
                           'omitted_deployment_solvers': (sorted(set(available) - requested)
                                                          if available is not None else None)}
    differences = {case: {target: sorted(values) for target, values in by_target.items()}
                   for case, by_target in hashes.items()
                   if len(set().union(*by_target.values())) > 1}
    return {'requested_solvers': sorted(requested), 'targets': targets, 'input_hash_differences': differences}


def orchestrate(args):
    profile = run_gcp.load_profile(Path(args.profile).resolve())
    report_preflight()
    benchmark_preflight(ROOT / profile['benchmark_profile'])
    if not args.project:
        result = subprocess.run(['gcloud', 'config', 'get-value', 'project'],
                                capture_output=True, text=True, timeout=30, check=True)
        args.project = result.stdout.strip()
    args.service_account = args.service_account or os.environ.get('SB_GCP_SERVICE_ACCOUNT')
    if not args.project or not re.fullmatch(r'[a-z0-9-]+', args.project):
        raise ValueError('set --project or configure an active gcloud project')
    if not args.service_account or not re.fullmatch(r'[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+', args.service_account):
        raise ValueError('set --service-account or SB_GCP_SERVICE_ACCOUNT')
    if not 1 <= len(args.zones) <= 2 or len(set(args.zones)) != len(args.zones):
        raise ValueError('specify one or two distinct zones')
    if any(not re.fullmatch(r'[a-z0-9-]+', z) or not z.startswith(args.region + '-') for z in args.zones):
        raise ValueError('zones must be in the selected region')
    if not 1 <= args.queue_timeout_seconds <= 3600:
        raise ValueError('queue timeout must be between 1 and 3600 seconds')
    source = source_state(profile['benchmark_profile'], args.profile)
    if source['omitted_untracked']:
        raise ValueError('untracked source files would be omitted: ' + ', '.join(source['omitted_untracked']))
    out = Path(args.output).resolve()
    if out.exists():
        raise ValueError('output directory already exists')
    if args.dry_run:
        print(json.dumps({'project': args.project, 'service_account': args.service_account,
                          'region': args.region, 'zones': args.zones, 'profile': profile,
                          'queue_timeout_seconds': args.queue_timeout_seconds, 'source': source}, indent=2))
        return 0
    out.mkdir(parents=True)
    attempts = out / 'attempts'
    attempts.mkdir()
    snapshot = out / 'source.tar.gz'
    run_gcp.make_source(snapshot, profile['benchmark_profile'])
    archive_hash = file_hash(snapshot)
    benchmark_profile = read_snapshot_profile(snapshot, profile['benchmark_profile'])
    current = source_state(profile['benchmark_profile'], args.profile)
    if current['omitted_untracked'] or current['fingerprint'] != source['fingerprint']:
        raise ValueError('source changed while the archive was being prepared')
    summary = {'schema_version': '1.0', 'project': args.project, 'region': args.region,
               'zones': args.zones, 'source': {**source, 'archive': str(snapshot), 'archive_hash': archive_hash},
               'attempts': [], 'targets': {},
               'report': None, 'status': 'running'}
    summary_path = out / 'summary.json'
    write_summary(summary_path, summary)
    outputs = {}
    interrupted = False

    def attempt(name, target_ids, zone):
        nonlocal interrupted
        selected = dict(profile)
        selected['targets'] = [t for t in profile['targets'] if t['id'] in target_ids]
        profile_path = out / (name + '-profile.json')
        profile_path.write_text(json.dumps(selected, indent=2) + '\n')
        dest = attempts / name
        log = out / (name + '.log')
        record = {'name': name, 'zone': zone, 'targets': target_ids, 'output': str(dest),
                  'log': str(log), 'state': 'launching'}
        summary['attempts'].append(record)
        write_summary(summary_path, summary)
        try:
            if file_hash(snapshot) != archive_hash:
                raise ValueError('source archive changed since snapshot')
        except (OSError, ValueError) as exc:
            record.update(state='snapshot_error', error=str(exc), cleanup={'verified': True, 'not_created': True})
            for key in target_ids:
                summary['targets'][key] = {'attempt': name, 'state': 'NOT_SUBMITTED', 'result': None}
            write_summary(summary_path, summary)
            return {}, record
        command = [sys.executable, str(CLOUD / 'run_gcp.py'), '--profile', str(profile_path),
                   '--project', args.project, '--service-account', args.service_account,
                   '--region', args.region, '--zone', zone, '--output', str(dest),
                   '--queue-timeout-seconds', str(args.queue_timeout_seconds), '--source-archive', str(snapshot),
                   '--source-sha256', archive_hash.removeprefix('sha256:')]
        if args.single_core:
            command.append('--single-core')
        print(f'Starting {name} in {zone}; log: {log}', flush=True)
        try:
            code, signum, watchdog, forced_kill = execute_attempt(
                command, log, profile['max_run_seconds'] + args.queue_timeout_seconds + 1800,
                cleanup_wait_seconds(len(target_ids)))
            interrupted |= signum is not None
            record.update(exit_code=code, signal=signum, watchdog=watchdog, forced_kill=forced_kill, state='finished')
        except OSError as exc:
            record.update(state='launch_error', error=str(exc))
        manifest_path = dest / 'cloud-run.json'
        try:
            manifest = json.loads(manifest_path.read_text())
            record['manifest'] = str(manifest_path)
        except (OSError, ValueError):
            manifest = {}
        record['states'] = manifest.get('states', {})
        record['cleanup'] = verify_attempt(args.project, args.region, zone, manifest_path)
        if record.get('forced_kill'):
            record['cleanup']['verified'] = False
            record['cleanup']['recovery'] = ('Launcher required a forced kill. Inspect the manifest resource names, '
                                               'verify cleanup, and recover retained objects before deletion.')
        for key in target_ids:
            result = dest / key
            if (result / 'results.jsonl').is_file():
                outputs[key] = result
            summary['targets'][key] = {'attempt': name, 'state': manifest.get('states', {}).get(key, 'UNKNOWN'),
                                       'result': str(outputs[key]) if key in outputs else None}
        write_summary(summary_path, summary)
        return manifest, record

    ids = [t['id'] for t in profile['targets']]
    first, first_record = attempt('initial', ids, args.zones[0])
    if (len(args.zones) == 2 and not interrupted and first_record['state'] == 'finished'
            and not first_record.get('watchdog')
            and first_record['cleanup'].get('verified')):
        for key in ids:
            if interrupted:
                break
            if (first.get('states', {}).get(key) == 'QUEUE_TIMEOUT'
                    and first.get('ever_running', {}).get(key) is False
                    and key not in outputs
                    and stockout(args.project, args.zones[0], first.get('job_uids', {}).get(key))):
                try:
                    current = source_state(profile['benchmark_profile'], args.profile)
                    if current['omitted_untracked'] or current['fingerprint'] != source['fingerprint']:
                        summary.setdefault('retry_skipped', {})[key] = 'source changed since the initial attempt'
                        write_summary(summary_path, summary)
                        continue
                except (OSError, subprocess.SubprocessError) as exc:
                    summary.setdefault('retry_skipped', {})[key] = 'source verification failed: ' + str(exc)
                    write_summary(summary_path, summary)
                    continue
                attempt('retry-' + key, [key], args.zones[1])
    for key in ids:
        if (first.get('states', {}).get(key) == 'QUEUE_TIMEOUT'
                and summary['targets'].get(key, {}).get('attempt') == 'initial'
                and key not in summary.get('retry_skipped', {})):
            if len(args.zones) < 2:
                reason = 'no alternate zone configured'
            elif interrupted:
                reason = 'run was interrupted'
            elif not first_record['cleanup'].get('verified'):
                reason = 'initial cleanup was not verified'
            else:
                reason = 'required safety conditions or capacity evidence were not verified'
            summary.setdefault('retry_skipped', {})[key] = reason
    if outputs:
        try:
            summary['results'] = result_summary(outputs, benchmark_profile)
            destination = out / 'report.html'
            create_report([str(outputs[k]) for k in ids if k in outputs], str(destination))
            summary['report'] = {'html': str(destination), 'json': str(destination.with_suffix('.json'))}
        except Exception as exc:
            summary['report_error'] = str(exc)
    initial_states = first_record.get('states', {})
    recovered_initial = (first_record.get('exit_code') == 1
                         and all(initial_states.get(key) == 'SUCCEEDED' or
                             (initial_states.get(key) == 'QUEUE_TIMEOUT'
                              and summary['targets'].get(key, {}).get('attempt') == 'retry-' + key
                              and summary['targets'][key]['state'] == 'SUCCEEDED') for key in ids))
    exits_ok = all(a.get('exit_code') == 0 or (a['name'] == 'initial' and recovered_initial)
                   for a in summary['attempts'])
    complete = (not interrupted and len(outputs) == len(ids) and bool(summary['report']) and exits_ok
                and all(v['state'] == 'SUCCEEDED' for v in summary['targets'].values())
                and all(not row['missing'] and not (set(row['outcomes']) -
                    {'success', 'unsupported', 'unsupported_instrumentation'})
                    for row in summary.get('results', {}).get('targets', {}).values())
                and not any(a.get('watchdog') for a in summary['attempts'])
                and all(a['cleanup'].get('verified') for a in summary['attempts']))
    summary['status'] = 'complete' if complete else 'interrupted' if interrupted else 'partial'
    write_summary(summary_path, summary)
    print(f"Run status: {summary['status']}", flush=True)
    print(f'Summary: {summary_path}', flush=True)
    if summary['report']:
        print(f"HTML report: {summary['report']['html']}", flush=True)
        print(f"JSON report: {summary['report']['json']}", flush=True)
    if summary.get('report_error'):
        print('Report generation failed: ' + summary['report_error'], file=sys.stderr, flush=True)
    for record in summary['attempts']:
        if not record.get('cleanup', {}).get('verified'):
            print(f"Cleanup not verified for {record['name']}; inspect {record.get('manifest', record['output'])} "
                  'and the summary before considering the run finished.', file=sys.stderr, flush=True)
    for key, reason in summary.get('retry_skipped', {}).items():
        print(f'Retry skipped for {key}: {reason}', file=sys.stderr, flush=True)
    return 0 if complete else 130 if interrupted else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--project', help='GCP project (defaults to the active gcloud project)')
    parser.add_argument('--service-account', help='VM account (or set SB_GCP_SERVICE_ACCOUNT)')
    parser.add_argument('--region', required=True)
    parser.add_argument('--zones', nargs='+', required=True, metavar='ZONE')
    parser.add_argument('--queue-timeout-seconds', type=int, default=300)
    parser.add_argument('--output', required=True)
    parser.add_argument('--single-core', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        return orchestrate(args)
    except (ImportError, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
