"""Read-only, bounded verification of a single cloud-run's cleanup.

This checks only resources associated with the run identity in cloud-run.json.
It does not delete anything. API or permission failures are reported as
unknown, never as proof that a resource is absent.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import time


def _manifest(path, project, region, zone):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError('run manifest must be an object')
    for key, value in (('project', project), ('region', region), ('zone', zone)):
        if data.get(key) != value:
            raise ValueError(f'manifest {key} does not match requested {key}')
    owner = data.get('ownership_label')
    if not isinstance(owner, dict) or set(owner) != {'sb-run'}:
        raise ValueError('missing ownership label')
    owner = owner['sb-run']
    if not isinstance(owner, str) or not re.fullmatch(r'[0-9a-f]{32}', owner):
        raise ValueError('invalid ownership label')
    prefix = data.get('job_prefix')
    if prefix != 'sb-' + owner[:12]:
        raise ValueError('job prefix does not match ownership label')
    bucket = data.get('scratch_bucket')
    if bucket != 'sb-temp-' + owner[:16]:
        raise ValueError('bucket does not match ownership label')
    jobs = data.get('jobs')
    if not isinstance(jobs, dict):
        raise ValueError('missing job identities')
    if any(not isinstance(target, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,24}', target)
           or job != prefix + '-' + target for target, job in jobs.items()):
        raise ValueError('invalid job identity')
    return prefix, bucket, set(jobs.values())


def _run_json(command, runner, timeout):
    try:
        env = {**os.environ, 'CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR': 'false'}
        result = runner(command, capture_output=True, text=True, check=False, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f'{type(exc).__name__}: {exc}'
    if result.returncode:
        return None, (result.stderr or result.stdout or f'exit {result.returncode}').strip()
    # gcloud may report partial list failures on stderr yet exit zero when a
    # user's SDK configuration permits partial errors. Even when the query
    # succeeded, diagnostics mean we cannot prove the returned list is complete.
    if result.stderr and result.stderr.strip():
        return None, 'cloud command emitted diagnostics: ' + result.stderr.strip()
    try:
        return json.loads(result.stdout), None
    except (TypeError, ValueError) as exc:
        return None, f'invalid JSON response: {exc}'


def _list(command, runner, timeout):
    value, error = _run_json(command, runner, timeout)
    if error:
        return None, error
    if (not isinstance(value, list)
            or any(not isinstance(item, dict) or not isinstance(item.get('name'), str)
                   for item in value)):
        return None, 'invalid resource list response'
    return value, None


def _bucket(bucket, project, runner, timeout):
    command = ['gcloud', 'storage', 'buckets', 'describe', 'gs://' + bucket,
               '--project', project, '--format=json']
    try:
        env = {**os.environ, 'CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR': 'false'}
        result = runner(command, capture_output=True, text=True, check=False, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f'{type(exc).__name__}: {exc}'
    if result.returncode:
        message = (result.stderr or result.stdout or f'exit {result.returncode}').strip()
        # gcloud storage emits this explicit status for a missing bucket. A
        # permission error or a generic error containing "not found" is unknown.
        missing = (r'ERROR:\s*\(gcloud\.storage\.buckets\.describe\)\s+'
                   + re.escape('gs://' + bucket) + r'\s+not found:\s*404\.?')
        if re.fullmatch(missing, message, re.IGNORECASE):
            return [], None
        return None, message
    if result.stderr and result.stderr.strip():
        return None, 'cloud command emitted diagnostics: ' + result.stderr.strip()
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        return None, f'invalid bucket response: {exc}'
    if not isinstance(value, dict):
        return None, 'invalid bucket response'
    return [bucket], None


def verify_attempt(project, region, zone, manifest_path, *, timeout_seconds=180,
                   interval_seconds=5, stable_observations=2, runner=None,
                   monotonic=None, sleep=None):
    """Check Batch jobs, managed instance groups, VMs, disks, and bucket.

    Two clean observations are required by default to account for asynchronous
    deletion. Managed groups must disappear because they can recreate VMs
    after a transiently clean VM listing. All queries are bounded; the returned details include the last
    observation and errors. The global VM/disk lists are checked locally for
    Batch job labels and the run's unique resource-name prefix, including disks
    without labels. This makes a collision conservative: it can block a success,
    but this read-only verifier never acts on an unrelated resource.
    """
    runner = runner or subprocess.run
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    try:
        if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
                or not isinstance(interval_seconds, (int, float))
                or isinstance(interval_seconds, bool) or not math.isfinite(interval_seconds)
                or interval_seconds <= 0
                or type(stable_observations) is not int or stable_observations < 2):
            raise ValueError('invalid verification timing')
        prefix, bucket, job_names = _manifest(manifest_path, project, region, zone)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {'verified': False, 'details': {'errors': {'manifest': str(exc)}}}

    deadline = monotonic() + timeout_seconds
    clean = 0
    observations = 0
    details = {}
    known_disks = set()
    unresolved_vm_disks = set()
    queries = {
        'jobs': ['gcloud', 'batch', 'jobs', 'list', '--project', project,
                 '--location', region, '--format=json'],
        'groups': ['gcloud', 'compute', 'instance-groups', 'managed', 'list',
                   '--project', project, '--format=json'],
        'vms': ['gcloud', 'compute', 'instances', 'list', '--project', project, '--format=json'],
        'disks': ['gcloud', 'compute', 'disks', 'list', '--project', project, '--format=json'],
    }
    while True:
        resources, errors = {}, {}
        for kind, command in queries.items():
            remaining = deadline - monotonic()
            if remaining <= 0:
                errors[kind] = 'verification deadline exceeded'
                continue
            values, error = _list(command, runner, min(15, remaining))
            if error:
                errors[kind] = error
                continue
            found = []
            for item in values:
                name = item['name'].rsplit('/', 1)[-1]
                labels = item.get('labels') or {}
                if not isinstance(labels, dict):
                    errors[kind] = 'invalid resource labels'
                    break
                batch_label = labels.get('batch-job-id')
                if batch_label is not None and not isinstance(batch_label, str):
                    errors[kind] = 'invalid Batch job label'
                    break
                if kind in ('jobs', 'groups'):
                    belongs = name in job_names or name.startswith(prefix + '-')
                elif kind == 'vms':
                    belongs = batch_label in job_names or name.startswith(prefix + '-')
                    if belongs:
                        attached = item.get('disks')
                        if not isinstance(attached, list):
                            unresolved_vm_disks.add(name)
                        else:
                            boot = [disk for disk in attached if isinstance(disk, dict) and disk.get('boot') is True]
                            if (len(boot) != 1 or not isinstance(boot[0].get('source'), str)
                                    or not boot[0]['source']):
                                unresolved_vm_disks.add(name)
                            else:
                                known_disks.add(boot[0]['source'].rsplit('/', 1)[-1])
                                unresolved_vm_disks.discard(name)
                else:
                    # Auto-deleted boot disks are normally named after the VM,
                    # but do not necessarily inherit its labels.
                    belongs = (batch_label in job_names or name.startswith(prefix + '-')
                               or name in known_disks)
                if belongs:
                    found.append(name)
            if kind not in errors:
                resources[kind] = sorted(found)
        remaining = deadline - monotonic()
        if remaining <= 0:
            errors['bucket'] = 'verification deadline exceeded'
        else:
            resources['bucket'], error = _bucket(bucket, project, runner, min(15, remaining))
            if error:
                del resources['bucket']
                errors['bucket'] = error
        if unresolved_vm_disks:
            errors['identity'] = 'could not identify boot disk for ' + ', '.join(sorted(unresolved_vm_disks))
        observations += 1
        details = {'resources': resources, 'errors': errors, 'observations': observations}
        if not errors and all(not resources[kind] for kind in ('jobs', 'groups', 'vms', 'disks', 'bucket')):
            clean += 1
            if clean >= stable_observations:
                return {'verified': True, 'details': details}
        else:
            clean = 0
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {'verified': False, 'details': details}
        sleep(min(interval_seconds, remaining))
        if monotonic() >= deadline:
            return {'verified': False, 'details': details}
