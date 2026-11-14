#!/usr/bin/env python3
"""Run a cloud profile on GCP Batch using temporary, private Cloud Storage."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
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
COMMAND_TIMEOUT_SECONDS = 120
TERMINAL_COLLECTION_GRACE_SECONDS = 180
MAX_SOURCE_BYTES = 20_000_000
MAX_EXPANDED_SOURCE_BYTES = 200_000_000
MAX_SOURCE_FILES = 100_000


def call(*args, capture=False, check=True):
    try:
        return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.PIPE if capture else None,
                              timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Do not include command arguments or output, which may be sensitive.
        raise OSError(f'cloud subprocess timed out after {COMMAND_TIMEOUT_SECONDS} seconds') from None


def write_state_atomic(path, value):
    """Make each recovery manifest visible as a complete JSON document."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.cloud-run-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def running_observation(state, status, prior):
    """Only prove never-running from a complete, contiguous Batch state history."""
    if prior is True or state == 'RUNNING':
        return True
    events = status.get('statusEvents')
    if not isinstance(events, list):
        return None
    transitions = []
    for event in events:
        if not isinstance(event, dict):
            return None
        if event.get('type') != 'STATUS_CHANGED':
            continue
        description = event.get('description')
        if not isinstance(description, str):
            return None
        match = re.search(r'Job state is set from ([A-Z_]+) to ([A-Z_]+)\b', description)
        if not match:
            return None
        before, after = match.groups()
        if 'RUNNING' in (before, after):
            return True
        transitions.append((before, after))
    if state not in ('FAILED', 'CANCELLED') or not transitions:
        return None
    current = 'QUEUED'
    for before, after in transitions:
        if before != current:
            return None
        current = after
    return False if current == state else None


def load_profile(path):
    profile = json.loads(path.read_text())
    if profile.get('schema_version') != '1.0' or not profile.get('targets'):
        raise ValueError('unsupported cloud profile or no targets')
    if not isinstance(profile.get('max_result_bytes'), int) or not 0 < profile['max_result_bytes'] <= 100_000_000:
        raise ValueError('invalid max_result_bytes')
    if not isinstance(profile.get('max_run_seconds'), int) or not 0 < profile['max_run_seconds'] <= 86400:
        raise ValueError('invalid max_run_seconds')
    grace = profile.get('upload_grace_seconds', 0)
    if type(grace) is not int or not (grace == 0 or 30 <= grace < profile['max_run_seconds']):
        raise ValueError('invalid upload_grace_seconds')
    if type(profile.get('max_entries', 5_000_000)) is not int or not 0 < profile.get('max_entries', 5_000_000) <= (1 << 63) - 1:
        raise ValueError('invalid max_entries')
    if type(profile.get('boot_disk_gb', 30)) is not int or not 10 <= profile.get('boot_disk_gb', 30) <= 65536:
        raise ValueError('invalid boot_disk_gb')
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
    if destination.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('source archive exceeds 20 MB')


def validate_source_archive(path, benchmark):
    """Validate a source snapshot before a worker can extract it on a VM."""
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('source archive exceeds 20 MB')
    required = {benchmark, 'cloud/gcp-worker.sh', 'cloud/single_core.py'}
    seen = set()
    expanded = 0
    script = None
    try:
        with tarfile.open(path, 'r:gz') as archive:
            for member in archive:
                name = member.name
                relative = PurePosixPath(name)
                if (not member.isfile() or not name or not relative.parts or '\\' in name or relative.is_absolute()
                        or '..' in relative.parts or name != relative.as_posix() or name in seen):
                    raise ValueError('source archive contains unsafe or duplicate members')
                seen.add(name)
                expanded += member.size
                if len(seen) > MAX_SOURCE_FILES or expanded > MAX_EXPANDED_SOURCE_BYTES:
                    raise ValueError('source archive exceeds extraction limits')
                if name == 'cloud/gcp-worker.sh':
                    if member.size > 1_000_000:
                        raise ValueError('source archive worker script exceeds size limit')
                    script = archive.extractfile(member).read().decode('utf-8')
    except (tarfile.TarError, UnicodeDecodeError, EOFError) as exc:
        raise ValueError('invalid source archive') from exc
    if not required <= seen:
        raise ValueError('source archive is missing required benchmark or worker files')
    return script


def job_config(target, profile, bucket, service_account, zone, owner=None, single_core=False, worker_script=None):
    script = worker_script if worker_script is not None else (ROOT / 'cloud/gcp-worker.sh').read_text()
    values = {'RUN_BUCKET': bucket, 'RUN_KEY': target['id'],
              'BENCHMARK_PROFILE': profile['benchmark_profile'], 'MAX_RESULT_BYTES': str(profile['max_result_bytes']),
              'SINGLE_CORE': '1' if single_core else '0',
              'MAX_ENTRIES': str(profile.get('max_entries', 5_000_000)),
              'MAX_RUN_SECONDS': str(profile['max_run_seconds']),
              'UPLOAD_GRACE_SECONDS': str(profile.get('upload_grace_seconds', 0))}
    prefix = ''.join('export ' + key + '=' + shlex.quote(value) + '\n' for key, value in values.items())
    policy = {'machineType': target['machine_type'], 'provisioningModel': 'STANDARD',
              'bootDisk': {'sizeGb': profile.get('boot_disk_gb', 30)}}
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


def retry_call(*args, capture=False, attempts=RETRIES, reject_stderr=False):
    """Retry failed reads, including ordinary nonzero CLI exits."""
    last = None
    for attempt in range(attempts):
        try:
            last = call(*args, capture=capture, check=False)
            if (last.returncode == 0
                    and not (reject_stderr and getattr(last, 'stderr', None))):
                return last
        except (OSError, subprocess.CalledProcessError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(1)
    return last


def owned_resource(*args, owner):
    """Return True/False only with a readable label; None means unverified."""
    result = retry_call(*args, capture=True, reject_stderr=True)
    if (isinstance(result, BaseException) or result.returncode
            or getattr(result, 'stderr', None)):
        return None
    try:
        labels = json.loads(result.stdout).get('labels', {})
        return labels.get(RUN_LABEL) == owner
    except (TypeError, ValueError, AttributeError):
        return None


def owned_job_state(name, region, common, owner, uid):
    """Return absent/deleting/owned only from a successful readable job list."""
    result = retry_call('gcloud', 'batch', 'jobs', 'list', '--location', region,
                        '--format=json', *common, capture=True, reject_stderr=True)
    if (isinstance(result, BaseException) or result.returncode
            or getattr(result, 'stderr', None)):
        return 'unknown'
    try:
        jobs = json.loads(result.stdout)
        if not isinstance(jobs, list) or any(not isinstance(job, dict) or not isinstance(job.get('name'), str)
                                             for job in jobs):
            return 'unknown'
        matching = [job for job in jobs if job['name'].rsplit('/', 1)[-1] == name]
        if not matching:
            return 'absent'
        if len(matching) != 1:
            return 'unknown'
        job = matching[0]
        if (job.get('labels', {}).get(RUN_LABEL) != owner
                or (uid is not None and job.get('uid') != uid)):
            return 'unknown'
        return 'deleting' if job.get('status', {}).get('state') == 'DELETION_IN_PROGRESS' else 'owned'
    except (TypeError, ValueError, AttributeError):
        return 'unknown'


def delete_owned_job(name, region, common, owner, uid):
    """Request deletion only while the exact job is verified as owned."""
    for attempt in range(RETRIES):
        state = owned_job_state(name, region, common, owner, uid)
        if state in ('absent', 'deleting'):
            return True
        if state != 'owned':
            return False
        try:
            result = call('gcloud', 'batch', 'jobs', 'delete', name, '--location', region, *common, check=False)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.CalledProcessError):
            pass
        if attempt + 1 < RETRIES:
            time.sleep(1)
    return owned_job_state(name, region, common, owner, uid) in ('absent', 'deleting')


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
    snapshot = getattr(args, 'source_archive', None)
    expected_source_sha = getattr(args, 'source_sha256', None)
    if ((snapshot is None) != (expected_source_sha is None)
            or (expected_source_sha is not None
                and (not isinstance(expected_source_sha, str)
                     or not re.fullmatch(r'[0-9a-f]{64}', expected_source_sha)))):
        raise ValueError('source_archive and a lowercase SHA-256 source_sha256 must be provided together')
    queue_timeout = getattr(args, 'queue_timeout_seconds', 3600)
    if type(queue_timeout) is not int or not 1 <= queue_timeout <= 86400:
        raise ValueError('queue_timeout_seconds must be an integer between 1 and 86400')
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
    planned_jobs = {target['id']: prefix + '-' + target['id'] for target in profile['targets']}
    common = ['--project', args.project, '--quiet']
    jobs = []
    bucket_created = False
    bucket_pending = False
    pending_job = None
    collected = set()
    retain_bucket = False
    with tempfile.TemporaryDirectory(prefix='sb-cloud-') as temp:
        tmp = Path(temp)
        source = tmp / 'source.tar.gz'
        worker_script = None
        if snapshot is None:
            make_source(source, profile['benchmark_profile'])
        else:
            snapshot = Path(snapshot)
            if not snapshot.is_file() or snapshot.is_symlink() or snapshot.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError('source_archive must be a regular file of at most 20 MB')
            shutil.copyfile(snapshot, source)
        with source.open('rb') as stream:
            source_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        if expected_source_sha is not None:
            if source_sha != expected_source_sha:
                raise ValueError('source archive SHA-256 does not match the expected digest')
            worker_script = validate_source_archive(source, profile['benchmark_profile'])
        configs = {}
        for target in profile['targets']:
            configs[target['id']] = job_config(target, profile, bucket, args.service_account, args.zone, owner,
                                                single_core=getattr(args, 'single_core', False),
                                                worker_script=worker_script)
        if args.dry_run:
            print(json.dumps({'bucket': bucket, 'source_bytes': source.stat().st_size,
                              'source_sha256': source_sha, 'jobs': configs}, indent=2))
            return 0
        output.mkdir(parents=True)
        print(f'Cloud resource names: jobs {prefix}-*, bucket gs://{bucket}', file=sys.stderr, flush=True)
        states = {target['id']: 'NOT_SUBMITTED' for target in profile['targets']}
        job_uids = {target['id']: None for target in profile['targets']}
        ever_running = {target['id']: None for target in profile['targets']}
        cancellations = {}

        def save_state():
            write_state_atomic(output / 'cloud-run.json',
                {'profile': profile, 'project': args.project, 'region': args.region, 'zone': args.zone,
                 'job_prefix': prefix, 'jobs': planned_jobs, 'job_uids': job_uids,
                 'ever_running': ever_running, 'states': states, 'queue_timeout_seconds': queue_timeout,
                 'cancellations': cancellations,
                 'source_sha256': source_sha,
                 'scratch_bucket': bucket, 'ownership_label': {RUN_LABEL: owner},
                 'bucket_retained': retain_bucket})

        # Record all planned resource names before even the bucket create. A
        # hard kill during an ambiguous create must still leave recovery IDs.
        save_state()
        old_handler = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

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
                name = planned_jobs[key]
                config = tmp / (key + '.json')
                config.write_text(json.dumps(configs[key]))
                pending_job = (key, name, time.monotonic())
                submitted = call('gcloud', 'batch', 'jobs', 'submit', name, '--location', args.region,
                                 '--config', str(config), '--format=json', *common, capture=True)
                jobs.append((key, name, time.monotonic()))
                pending_job = None
                states[key] = 'QUEUED'
                if submitted.stdout.strip():
                    submitted_job = json.loads(submitted.stdout)
                    if not isinstance(submitted_job, dict):
                        raise ValueError('invalid Batch job submission response')
                    uid = submitted_job.get('uid')
                    if isinstance(uid, str) and uid:
                        job_uids[key] = uid
                # Record each confirmed submission before starting the next.
                save_state()
            started = set()
            cancellation_requested = {}

            def request_cancellation(key, name):
                record = cancellations[key]
                record['attempts'] += 1
                states[key] = 'CANCELLATION_REQUESTED'
                # Persist before the request: timeout or termination can leave
                # its remote outcome ambiguous.
                save_state()
                try:
                    call('gcloud', 'batch', 'jobs', 'cancel', name, '--location', args.region, *common)
                except (OSError, subprocess.CalledProcessError) as exc:
                    record['ambiguous'] = True
                    record['last_error'] = type(exc).__name__
                    states[key] = 'CANCELLATION_UNCONFIRMED'
                    print(f'{key}: cancellation request failed or timed out; continuing other targets', file=sys.stderr)
                else:
                    record['accepted'] = True
                    states[key] = 'CANCELLATION_IN_PROGRESS'
                save_state()

            # A task that starts near the queue deadline can finish close to
            # this boundary. Leave bounded time for the next status poll and
            # result collection without extending the Batch task runtime.
            deadline = (time.monotonic() + profile['max_run_seconds'] + queue_timeout
                        + TERMINAL_COLLECTION_GRACE_SECONDS)
            while len(collected) < len(jobs):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Batch jobs exceeded the runtime, queue deadline, and 180-second collection grace')
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
                    description = json.loads(result.stdout)
                    status = description.get('status', {})
                    state = status.get('state', 'UNKNOWN')
                    uid = description.get('uid')
                    if isinstance(uid, str) and uid:
                        if job_uids[key] is not None and job_uids[key] != uid:
                            raise RuntimeError(f'{key}: Batch job UID changed unexpectedly')
                        job_uids[key] = uid
                    ever_running[key] = running_observation(state, status, ever_running[key])
                    print(f'{key}: {state}', flush=True)
                    states[key] = state
                    if ever_running[key] is True:
                        started.add(key)
                        if key in cancellation_requested and state not in ('SUCCEEDED', 'FAILED', 'CANCELLED', 'DELETION_IN_PROGRESS'):
                            # A queued job can start while a cancel request is
                            # in flight. Preserve its work rather than sending
                            # repeated cancels or applying the queue deadline.
                            cancellations[key]['observed_running'] = True
                            del cancellation_requested[key]
                    # Persist the observed UID and running evidence before
                    # any cancellation, retry, or result-collection work.
                    save_state()
                    if state in ('SUCCEEDED', 'FAILED', 'CANCELLED', 'DELETION_IN_PROGRESS'):
                        states[key], safe = collect_result(key, state, bucket, output, tmp, common, profile['max_result_bytes'])
                        # Deletion is asynchronous: a VM may still upload after
                        # an object-absence check, so keep its scratch bucket.
                        if state == 'DELETION_IN_PROGRESS':
                            safe = False
                        if key in cancellation_requested and ever_running[key] is False and state in ('FAILED', 'CANCELLED'):
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
                    elif (key in cancellation_requested and not cancellations[key]['accepted']
                          and cancellations[key]['attempts'] < RETRIES):
                        request_cancellation(key, name)
                    elif key not in started and key not in cancellation_requested and time.monotonic() - submitted >= queue_timeout:
                        cancellation_requested[key] = time.monotonic()
                        cancellations[key] = {'attempts': 0, 'accepted': False, 'ambiguous': False}
                        request_cancellation(key, name)
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
            for key, name, _ in jobs:
                if not delete_owned_job(name, args.region, common, owner, job_uids[key]):
                    cleanup_errors.append('Batch job ' + name)
            if bucket_created:
                if retain_bucket:
                    print(f'Skipped deletion of gs://{bucket} to allow result recovery. Confirm ownership first; '
                          f'then inspect with: gcloud storage ls gs://{bucket}/; '
                          f'download with: gcloud storage cp "gs://{bucket}/result-*.tar.gz" DESTINATION/; '
                          'remove the bucket only after confirming it belongs to this run.', file=sys.stderr)
                else:
                    owned = owned_resource('gcloud', 'storage', 'buckets', 'describe', 'gs://' + bucket,
                                           '--format=json', *common, owner=owner)
                    if owned is not True:
                        # A bucket can be removed and replaced while this run is
                        # active. Never delete a mismatched or unknown resource.
                        retain_bucket = True
                        cleanup_errors.append('unverified bucket gs://' + bucket)
                        print(f'Skipped deletion of gs://{bucket}: ownership did not match or could not be verified. '
                              'Inspect the resource before taking further action.', file=sys.stderr)
                        try:
                            save_state()
                        except OSError as exc:
                            state_error = exc
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
                raise RuntimeError('Cleanup failed for ' + ', '.join(cleanup_errors) +
                                   '; inspect these resources and clean up only resources owned by this run')
            if state_error:
                raise RuntimeError(f'Could not save cloud-run.json: {state_error}')



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default=str(ROOT / 'cloud/profiles/light.json'))
    parser.add_argument('--project', required=True)
    parser.add_argument('--service-account', required=True, help='Batch VM service account email')
    parser.add_argument('--region', default='us-central1')
    parser.add_argument('--zone', default='us-central1-c')
    parser.add_argument('--queue-timeout-seconds', type=int, default=3600,
                        help='cancel jobs that have not started within this many seconds (1-86400)')
    parser.add_argument('--source-archive', help='use a validated source snapshot instead of packaging the working tree')
    parser.add_argument('--source-sha256', help='expected lowercase SHA-256 for --source-archive')
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
