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
from urllib import error, parse, request
import uuid

ROOT = Path(__file__).resolve().parents[1]
REPORT_FILES = frozenset(('results.jsonl', 'execution.json', 'profile.json', 'version-manifest.json'))
RUN_LABEL = 'sb-run'
RETRIES = 3


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
    files += [benchmark, 'cloud/gcp-worker.sh', 'cloud/single_core.py']
    with tarfile.open(destination, 'w:gz') as archive:
        for name in sorted(set(filter(None, files))):
            path = ROOT / name
            if path.is_file() and not path.is_symlink():
                archive.add(path, arcname=name, recursive=False)
    if destination.stat().st_size > 20_000_000:
        raise ValueError('source archive exceeds 20 MB')


def job_config(target, profile, bucket, service_account, zone, owner=None, single_core=False):
    script = (ROOT / 'cloud/gcp-worker.sh').read_text()
    values = {'RUN_BUCKET': bucket, 'RUN_KEY': target['id'],
              'BENCHMARK_PROFILE': profile['benchmark_profile'], 'MAX_RESULT_BYTES': str(profile['max_result_bytes']),
              'SINGLE_CORE': '1' if single_core else '0'}
    prefix = ''.join('export ' + key + '=' + shlex.quote(value) + '\n' for key, value in values.items())
    policy = {'machineType': target['machine_type'], 'provisioningModel': 'STANDARD',
              'bootDisk': {'sizeGb': 30}}
    if target.get('boot_disk_type'):
        policy['bootDisk']['type'] = target['boot_disk_type']
    if target.get('min_cpu_platform'):
        policy['minCpuPlatform'] = target['min_cpu_platform']
    if target.get('image'):
        policy['bootDisk']['image'] = target['image']
    config = {'taskGroups': [{'taskCount': 1, 'parallelism': 1, 'taskSpec': {
        'runnables': [{'script': {'text': '#!/bin/bash\n' + prefix + script}}],
        'computeResource': {'cpuMilli': target['cpu_milli'], 'memoryMib': target['memory_mib']},
        'maxRetryCount': 0, 'maxRunDuration': str(profile['max_run_seconds']) + 's'}}],
        'allocationPolicy': {'instances': [{'policy': policy}],
                             'location': {'allowedLocations': ['zones/' + zone]},
                             'serviceAccount': {'email': service_account, 'scopes': ['https://www.googleapis.com/auth/cloud-platform']}}}
    if owner is not None:
        config['labels'] = {RUN_LABEL: owner}
    return config


def create_bucket(bucket, project, region, owner):
    """Atomically attach the ownership marker and cleanup policy at creation."""
    token = call('gcloud', 'auth', 'print-access-token', capture=True).stdout.strip()
    if not token:
        raise RuntimeError('gcloud did not provide an access token')
    payload = {'name': bucket, 'location': region, 'storageClass': 'STANDARD',
               'labels': {RUN_LABEL: owner},
               'softDeletePolicy': {'retentionDurationSeconds': '0'},
               'lifecycle': {'rule': [{'action': {'type': 'Delete'}, 'condition': {'age': 1}}]}}
    url = 'https://storage.googleapis.com/storage/v1/b?' + parse.urlencode({'project': project})
    req = request.Request(url, data=json.dumps(payload).encode(), method='POST',
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with request.urlopen(req, timeout=30) as response:
            response.read()
    except error.HTTPError as exc:
        # Do not include request headers or access tokens in diagnostics.
        raise RuntimeError(f'bucket create failed with HTTP {exc.code}') from None


def retry_call(*args, capture=False, attempts=RETRIES):
    """Retry failed reads, including ordinary nonzero CLI exits."""
    last = None
    for attempt in range(attempts):
        try:
            last = call(*args, capture=capture, check=False)
            if last.returncode == 0:
                return last
        except (OSError, subprocess.CalledProcessError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(1)
    return last


def owned_resource(*args, owner):
    """Return True/False only with a readable label; None means unverified."""
    result = retry_call(*args, capture=True)
    if isinstance(result, BaseException) or result.returncode:
        return None
    try:
        labels = json.loads(result.stdout).get('labels', {})
        return labels.get(RUN_LABEL) == owner
    except (TypeError, ValueError, AttributeError):
        return None


def object_exists(bucket, name):
    """A definitive 404 is absent; auth and transport errors are unknown."""
    url = ('https://storage.googleapis.com/storage/v1/b/' + parse.quote(bucket, safe='') +
           '/o/' + parse.quote(name, safe=''))
    for attempt in range(RETRIES):
        try:
            token = call('gcloud', 'auth', 'print-access-token', capture=True).stdout.strip()
            if not token:
                raise RuntimeError('gcloud did not provide an access token')
            req = request.Request(url, headers={'Authorization': 'Bearer ' + token})
            with request.urlopen(req, timeout=30) as response:
                response.read()
            return True
        except error.HTTPError as exc:
            if exc.code == 404:
                return False
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            pass
        if attempt + 1 < RETRIES:
            time.sleep(1)
    return None


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
    """Return the effective state and whether remote artifacts are safe to delete."""
    dest = tmp / ('result-' + key + '.tar.gz')
    result = retry_call('gcloud', 'storage', 'cp', 'gs://' + bucket + '/result-' + key + '.tar.gz', str(dest), *common)
    safe = False
    if not isinstance(result, BaseException) and result.returncode == 0:
        try:
            with tempfile.TemporaryDirectory(prefix='.extract-', dir=output) as work:
                staging = Path(work) / 'result'
                extract_result(dest, staging, max_bytes)
                staging.rename(output / key)
            safe = True
        except (OSError, ValueError, tarfile.TarError) as exc:
            print(f'{key}: could not extract result: {exc}', file=sys.stderr)
    if not safe:
        absent = object_exists(bucket, 'result-' + key + '.tar.gz') is False
        safe = absent
        if state == 'SUCCEEDED':
            state = 'MISSING_RESULT' if absent else 'DOWNLOAD_FAILED'
    if state != 'SUCCEEDED':
        # An ordinary worker failure uploads at most 64 KiB. A hard timeout or
        # provisioning failure may stop it before the upload.
        logs = retry_call('gcloud', 'storage', 'cp', 'gs://' + bucket + '/failure-' + key + '.log',
                          str(output / ('failure-' + key + '.log')), *common)
        if isinstance(logs, BaseException) or logs.returncode:
            safe &= object_exists(bucket, 'failure-' + key + '.log') is False
    return state, safe


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
    owner = uuid.uuid4().hex
    suffix = owner[:16]
    bucket = 'sb-temp-' + suffix
    prefix = 'sb-' + suffix[:12]
    common = ['--project', args.project, '--quiet']
    jobs = []
    bucket_created = False
    bucket_pending = False
    pending_job = None
    collected = set()
    retain_bucket = False
    with tempfile.TemporaryDirectory(prefix='sb-cloud-') as temp:
        tmp = Path(temp)
        make_source(tmp / 'source.tar.gz', profile['benchmark_profile'])
        configs = {}
        for target in profile['targets']:
            configs[target['id']] = job_config(target, profile, bucket, args.service_account, args.zone, owner,
                                                single_core=getattr(args, 'single_core', False))
        if args.dry_run:
            print(json.dumps({'bucket': bucket, 'source_bytes': (tmp / 'source.tar.gz').stat().st_size, 'jobs': configs}, indent=2))
            return 0
        output.mkdir(parents=True)
        print(f'Cloud resource names: jobs {prefix}-*, bucket gs://{bucket}', file=sys.stderr, flush=True)
        old_handler = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        states = {target['id']: 'NOT_SUBMITTED' for target in profile['targets']}

        def save_state():
            (output / 'cloud-run.json').write_text(json.dumps(
                {'profile': profile, 'region': args.region, 'zone': args.zone, 'states': states,
                 'scratch_bucket': bucket, 'ownership_label': {RUN_LABEL: owner},
                 'bucket_retained': retain_bucket}, indent=2) + '\n')

        try:
            bucket_pending = True
            create_bucket(bucket, args.project, args.region, owner)
            bucket_created = True
            bucket_pending = False
            call('gcloud', 'storage', 'buckets', 'add-iam-policy-binding', 'gs://' + bucket,
                 '--member', 'serviceAccount:' + args.service_account, '--role', 'roles/storage.objectUser', *common)
            call('gcloud', 'storage', 'cp', str(tmp / 'source.tar.gz'), 'gs://' + bucket + '/source.tar.gz', *common)
            for target in profile['targets']:
                key = target['id']
                name = prefix + '-' + key
                config = tmp / (key + '.json')
                config.write_text(json.dumps(configs[key]))
                pending_job = (key, name, time.monotonic())
                call('gcloud', 'batch', 'jobs', 'submit', name, '--location', args.region, '--config', str(config), *common)
                jobs.append((key, name, time.monotonic()))
                pending_job = None
                states[key] = 'QUEUED'
            started = set()
            cancellation_requested = {}
            deadline = time.monotonic() + profile['max_run_seconds'] + 3600
            while len(collected) < len(jobs):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Batch jobs exceeded the runtime plus one-hour queue deadline')
                for key, name, submitted in jobs:
                    if key in collected:
                        continue
                    result = retry_call('gcloud', 'batch', 'jobs', 'describe', name, '--location', args.region, '--format=json', *common, capture=True)
                    if isinstance(result, BaseException) or result.returncode:
                        print(f'{key}: could not query status; preserving scratch bucket', file=sys.stderr)
                        states[key] = 'POLL_ERROR'
                        retain_bucket = True
                        collected.add(key)
                        save_state()
                        continue
                    state = json.loads(result.stdout).get('status', {}).get('state', 'UNKNOWN')
                    print(f'{key}: {state}', flush=True)
                    states[key] = state
                    if state == 'RUNNING':
                        started.add(key)
                    if state in ('SUCCEEDED', 'FAILED', 'CANCELLED', 'DELETION_IN_PROGRESS'):
                        states[key], safe = collect_result(key, state, bucket, output, tmp, common, profile['max_result_bytes'])
                        # Deletion is asynchronous: a VM may still upload after
                        # an object-absence check, so keep its scratch bucket.
                        if state == 'DELETION_IN_PROGRESS':
                            safe = False
                        if key in cancellation_requested and state in ('FAILED', 'CANCELLED'):
                            states[key] = 'QUEUE_TIMEOUT'
                        retain_bucket |= not safe
                        collected.add(key)
                        save_state()
                    elif key in cancellation_requested and time.monotonic() - cancellation_requested[key] >= 120:
                        print(f'{key}: cancellation did not finish; preserving scratch bucket', file=sys.stderr)
                        states[key] = 'CANCEL_TIMEOUT'
                        retain_bucket = True
                        collected.add(key)
                        save_state()
                    elif key not in started and key not in cancellation_requested and time.monotonic() - submitted >= 3600:
                        call('gcloud', 'batch', 'jobs', 'cancel', name, '--location', args.region, *common)
                        cancellation_requested[key] = time.monotonic()
                        states[key] = 'CANCELLATION_IN_PROGRESS'
                        save_state()
                if len(collected) < len(jobs):
                    time.sleep(15)
            save_state()
            return 0 if all(state == 'SUCCEEDED' for state in states.values()) else 1
        finally:
            signal.signal(signal.SIGTERM, old_handler)
            # An interrupted/failed create may have succeeded remotely. The
            # random ownership label is attached in the create request, so a
            # name collision alone never authorizes deletion.
            ambiguous = []
            if pending_job is not None and not any(name == pending_job[1] for _, name, _ in jobs):
                key, name, submitted = pending_job
                owned = owned_resource('gcloud', 'batch', 'jobs', 'describe', name, '--location', args.region,
                                       '--format=json', *common, owner=owner)
                if owned is True:
                    jobs.append((key, name, submitted))
                    states[key] = 'AMBIGUOUS_SUBMIT'
                elif owned is None:
                    ambiguous.append('Batch job ' + name)
                    retain_bucket = True
                else:
                    print(f'Skipping unrelated Batch job {name}', file=sys.stderr)
            if bucket_pending:
                owned = owned_resource('gcloud', 'storage', 'buckets', 'describe', 'gs://' + bucket,
                                       '--format=json', *common, owner=owner)
                if owned is True:
                    bucket_created = True
                elif owned is None:
                    ambiguous.append('bucket gs://' + bucket)
                else:
                    print(f'Skipping unrelated bucket gs://{bucket}', file=sys.stderr)
            retain_bucket |= any(key not in collected for key, _, _ in jobs)
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
                try:
                    result = call('gcloud', 'batch', 'jobs', 'delete', name, '--location', args.region, *common, check=False)
                except OSError:
                    cleanup_errors.append('Batch job ' + name)
                else:
                    if result.returncode:
                        cleanup_errors.append('Batch job ' + name)
            if bucket_created:
                if retain_bucket:
                    print(f'Retained gs://{bucket} for result recovery. Objects expire after one day; '
                          f'inspect with: gcloud storage ls gs://{bucket}/; '
                          f'download with: gcloud storage cp "gs://{bucket}/result-*.tar.gz" DESTINATION/; '
                          f'when finished remove it with: gcloud storage rm --recursive gs://{bucket}', file=sys.stderr)
                else:
                    try:
                        result = call('gcloud', 'storage', 'rm', '--recursive', 'gs://' + bucket, *common, check=False)
                    except OSError:
                        cleanup_errors.append('bucket gs://' + bucket)
                    else:
                        if result.returncode:
                            cleanup_errors.append('bucket gs://' + bucket)
            print(f'Cloud resource names: jobs {prefix}-*, bucket gs://{bucket}. Verify them after an interrupted or failed API request.', file=sys.stderr)
            if ambiguous:
                print('Could not verify ownership or existence of ' + ', '.join(ambiguous) +
                      '; inspect and clean up owned resources manually.', file=sys.stderr)
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
    parser.add_argument('--single-core', action='store_true',
                        help='pin the benchmark to one CPU and set every solver thread limit to 1')
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
