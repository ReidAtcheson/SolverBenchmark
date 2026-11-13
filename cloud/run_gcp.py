#!/usr/bin/env python3
"""Run a cloud profile on GCP Batch using temporary, private Cloud Storage."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
REPORT_FILES = frozenset(('results.jsonl', 'execution.json', 'profile.json', 'version-manifest.json'))


def call(*args, capture=False, check=True):
    return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE if capture else None)


def load_profile(path):
    profile = json.loads(path.read_text())
    if profile.get('schema_version') != '1.0' or not profile.get('targets'):
        raise ValueError('unsupported cloud profile or no targets')
    if not isinstance(profile.get('max_result_bytes'), int) or not 0 < profile['max_result_bytes'] <= 100_000_000:
        raise ValueError('invalid max_result_bytes')
    if not isinstance(profile.get('max_run_seconds'), int) or not 0 < profile['max_run_seconds'] <= 86400:
        raise ValueError('invalid max_run_seconds')
    benchmark = Path(profile['benchmark_profile'])
    if benchmark.is_absolute() or '..' in benchmark.parts or not (ROOT / benchmark).is_file():
        raise ValueError('benchmark_profile must be a file inside the repository')
    ids = set()
    for target in profile['targets']:
        key = target.get('id', '')
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,24}', key) or key in ids:
            raise ValueError('target IDs must be unique lowercase names')
        ids.add(key)
        if target.get('provider') != 'gcp' or not re.fullmatch(r'[a-z][a-z0-9-]+', target.get('machine_type', '')):
            raise ValueError('invalid GCP machine type')
        for field in ('cpu_milli', 'memory_mib'):
            if type(target.get(field)) is not int or target[field] < 1:
                raise ValueError(f'invalid {field}')
    return profile


def make_source(destination, benchmark):
    # Tracked working-tree files only: never copy .git, untracked secrets or builds.
    files = call('git', '-C', str(ROOT), 'ls-files', '-z', capture=True).stdout.split('\0')
    files += [benchmark, 'cloud/gcp-worker.sh']
    with tarfile.open(destination, 'w:gz') as archive:
        for name in sorted(set(filter(None, files))):
            path = ROOT / name
            if path.is_file() and not path.is_symlink():
                archive.add(path, arcname=name, recursive=False)
    if destination.stat().st_size > 20_000_000:
        raise ValueError('source archive exceeds 20 MB')


def job_config(target, profile, bucket, service_account, zone):
    script = (ROOT / 'cloud/gcp-worker.sh').read_text()
    values = {'RUN_BUCKET': bucket, 'RUN_KEY': target['id'],
              'BENCHMARK_PROFILE': profile['benchmark_profile'], 'MAX_RESULT_BYTES': str(profile['max_result_bytes'])}
    prefix = ''.join('export ' + key + '=' + shlex.quote(value) + '\n' for key, value in values.items())
    policy = {'machineType': target['machine_type'], 'provisioningModel': 'STANDARD',
              'bootDisk': {'sizeGb': 30}}
    if target.get('boot_disk_type'):
        policy['bootDisk']['type'] = target['boot_disk_type']
    if target.get('min_cpu_platform'):
        policy['minCpuPlatform'] = target['min_cpu_platform']
    if target.get('image'):
        policy['bootDisk']['image'] = target['image']
    return {'taskGroups': [{'taskCount': 1, 'parallelism': 1, 'taskSpec': {
        'runnables': [{'script': {'text': '#!/bin/bash\n' + prefix + script}}],
        'computeResource': {'cpuMilli': target['cpu_milli'], 'memoryMib': target['memory_mib']},
        'maxRetryCount': 0, 'maxRunDuration': str(profile['max_run_seconds']) + 's'}}],
        'allocationPolicy': {'instances': [{'policy': policy}],
                             'location': {'allowedLocations': ['zones/' + zone]},
                             'serviceAccount': {'email': service_account, 'scopes': ['https://www.googleapis.com/auth/cloud-platform']}}}


def extract_result(archive_path, destination, max_bytes):
    if archive_path.stat().st_size > max_bytes:
        raise ValueError('downloaded result exceeds transfer cap')
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        if {m.name for m in members} != REPORT_FILES or len(members) != len(REPORT_FILES):
            raise ValueError('unexpected files in result archive')
        if any(not m.isfile() for m in members) or sum(m.size for m in members) > 100_000_000:
            raise ValueError('invalid or oversized extracted result')
        destination.mkdir(parents=True, exist_ok=False)
        for member in members:
            (destination / member.name).write_bytes(archive.extractfile(member).read())


def collect_result(key, state, bucket, output, tmp, common, max_bytes):
    """Collect one finished job while the scratch bucket is still available."""
    dest = tmp / ('result-' + key + '.tar.gz')
    result = call('gcloud', 'storage', 'cp', 'gs://' + bucket + '/result-' + key + '.tar.gz', str(dest), *common, check=False)
    if result.returncode == 0:
        extract_result(dest, output / key, max_bytes)
    elif state == 'SUCCEEDED':
        state = 'MISSING_RESULT'
    if state != 'SUCCEEDED':
        # An ordinary worker failure uploads at most 64 KiB. A hard timeout or
        # provisioning failure may stop it before the upload.
        call('gcloud', 'storage', 'cp', 'gs://' + bucket + '/failure-' + key + '.log',
             str(output / ('failure-' + key + '.log')), *common, check=False)
    return state


def run(args):
    profile_path = Path(args.profile).resolve()
    profile = load_profile(profile_path)
    if not re.fullmatch(r'[a-z0-9-]+', args.project) or not re.fullmatch(r'[a-z0-9-]+', args.zone):
        raise ValueError('invalid project or zone')
    if not args.zone.startswith(args.region + '-'):
        raise ValueError('zone must be in region')
    if not re.fullmatch(r'[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+', args.service_account):
        raise ValueError('invalid service account email')
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError('output directory already exists')
    suffix = uuid.uuid4().hex[:16]
    bucket = 'sb-temp-' + suffix
    prefix = 'sb-' + suffix[:12]
    common = ['--project', args.project, '--quiet']
    jobs = []
    bucket_created = False
    with tempfile.TemporaryDirectory(prefix='sb-cloud-') as temp:
        tmp = Path(temp)
        make_source(tmp / 'source.tar.gz', profile['benchmark_profile'])
        configs = {}
        for target in profile['targets']:
            configs[target['id']] = job_config(target, profile, bucket, args.service_account, args.zone)
        if args.dry_run:
            print(json.dumps({'bucket': bucket, 'source_bytes': (tmp / 'source.tar.gz').stat().st_size, 'jobs': configs}, indent=2))
            return 0
        output.mkdir(parents=True)
        print(f'Cloud resource names: jobs {prefix}-*, bucket gs://{bucket}', file=sys.stderr, flush=True)
        # Age lifecycle protects against orphaned objects after a hard interruption.
        (tmp / 'lifecycle.json').write_text(json.dumps({'rule': [{'action': {'type': 'Delete'}, 'condition': {'age': 1}}]}))
        old_handler = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        states = {target['id']: 'NOT_SUBMITTED' for target in profile['targets']}

        def save_state():
            (output / 'cloud-run.json').write_text(json.dumps(
                {'profile': profile, 'region': args.region, 'zone': args.zone, 'states': states}, indent=2) + '\n')

        try:
            call('gcloud', 'storage', 'buckets', 'create', 'gs://' + bucket, '--location', args.region, '--soft-delete-duration=0', *common)
            bucket_created = True
            call('gcloud', 'storage', 'buckets', 'update', 'gs://' + bucket, '--lifecycle-file', str(tmp / 'lifecycle.json'), *common)
            call('gcloud', 'storage', 'buckets', 'add-iam-policy-binding', 'gs://' + bucket,
                 '--member', 'serviceAccount:' + args.service_account, '--role', 'roles/storage.objectUser', *common)
            call('gcloud', 'storage', 'cp', str(tmp / 'source.tar.gz'), 'gs://' + bucket + '/source.tar.gz', *common)
            for target in profile['targets']:
                key = target['id']
                name = prefix + '-' + key
                config = tmp / (key + '.json')
                config.write_text(json.dumps(configs[key]))
                call('gcloud', 'batch', 'jobs', 'submit', name, '--location', args.region, '--config', str(config), *common)
                jobs.append((key, name, time.monotonic()))
                states[key] = 'QUEUED'
            collected = set()
            started = set()
            deadline = time.monotonic() + profile['max_run_seconds'] + 3600
            while len(collected) < len(jobs):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Batch jobs exceeded the runtime plus one-hour queue deadline')
                for key, name, submitted in jobs:
                    if key in collected:
                        continue
                    result = call('gcloud', 'batch', 'jobs', 'describe', name, '--location', args.region, '--format=json', *common, capture=True)
                    state = json.loads(result.stdout).get('status', {}).get('state', 'UNKNOWN')
                    print(f'{key}: {state}', flush=True)
                    states[key] = state
                    if state == 'RUNNING':
                        started.add(key)
                    if state in ('SUCCEEDED', 'FAILED', 'CANCELLED', 'DELETION_IN_PROGRESS'):
                        states[key] = collect_result(key, state, bucket, output, tmp, common, profile['max_result_bytes'])
                        collected.add(key)
                        save_state()
                    elif key not in started and time.monotonic() - submitted >= 3600:
                        call('gcloud', 'batch', 'jobs', 'cancel', name, '--location', args.region, *common)
                        states[key] = 'QUEUE_TIMEOUT'
                        collected.add(key)
                        save_state()
                if len(collected) < len(jobs):
                    time.sleep(15)
            save_state()
            return 0 if all(state == 'SUCCEEDED' for state in states.values()) else 1
        finally:
            signal.signal(signal.SIGTERM, old_handler)
            # Keep already downloaded results and the most recent states even if
            # polling or submission fails, or the caller interrupts the run.
            state_error = None
            try:
                save_state()
            except OSError as exc:
                state_error = exc
            # Only delete resources whose creation returned success. A failed CLI
            # request can have an ambiguous remote outcome; print names for audit.
            cleanup_errors = []
            for _, name, _ in jobs:
                result = call('gcloud', 'batch', 'jobs', 'delete', name, '--location', args.region, *common, check=False)
                if result.returncode:
                    cleanup_errors.append('Batch job ' + name)
            if bucket_created:
                result = call('gcloud', 'storage', 'rm', '--recursive', 'gs://' + bucket, *common, check=False)
                if result.returncode:
                    cleanup_errors.append('bucket gs://' + bucket)
            print(f'Cloud resource names: jobs {prefix}-*, bucket gs://{bucket}. Verify them after an interrupted or failed API request.', file=sys.stderr)
            if cleanup_errors:
                raise RuntimeError('Cleanup failed for ' + ', '.join(cleanup_errors) + '; delete these resources manually')
            if state_error:
                raise RuntimeError(f'Could not save cloud-run.json: {state_error}')



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default=str(ROOT / 'cloud/profiles/light.json'))
    parser.add_argument('--project', required=True)
    parser.add_argument('--service-account', required=True, help='Batch VM service account email')
    parser.add_argument('--region', default='us-central1')
    parser.add_argument('--zone', default='us-central1-c')
    parser.add_argument('--output', required=True)
    parser.add_argument('--dry-run', action='store_true', help='show jobs and source size without creating cloud resources')
    args = parser.parse_args()
    try:
        return run(args)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
