"""Attach Compute Engine hardware metadata to a cloud benchmark execution."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request


METADATA_ROOT = 'http://metadata.google.internal/computeMetadata/v1/instance/'


def read_metadata(key):
    """Read public instance identity metadata, bypassing any HTTP proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(METADATA_ROOT + key, headers={'Metadata-Flavor': 'Google'})
    for attempt in range(3):
        try:
            with opener.open(request, timeout=5) as response:
                return response.read().decode('utf-8').strip()
        except (OSError, urllib.error.URLError):
            if attempt == 2:
                raise
            time.sleep(1)


def capture_metadata():
    machine = read_metadata('machine-type')
    cpu = read_metadata('cpu-platform')
    zone = read_metadata('zone')
    # Require the documented project-qualified metadata format, but do not
    # persist the project number in the portable benchmark result.
    match_machine = re.fullmatch(r'projects/[0-9]+/machineTypes/([a-z0-9-]+)', machine)
    match_zone = re.fullmatch(r'projects/[0-9]+/zones/([a-z0-9-]+)', zone)
    if not match_machine or not match_zone or not cpu:
        raise ValueError('invalid or incomplete Compute Engine hardware metadata')
    return {'provider': 'gcp', 'machine_type': match_machine.group(1),
            'cpu_platform': cpu, 'zone': match_zone.group(1)}


def attach_metadata(execution_path):
    path = Path(execution_path)
    execution = json.loads(path.read_text(encoding='utf-8'))
    host = execution.get('host')
    if not isinstance(host, dict):
        raise ValueError('execution record has no host object')
    if 'cloud' in host:
        raise ValueError('execution record already has cloud metadata')
    host['cloud'] = capture_metadata()
    # A failed metadata write must not truncate the benchmark execution record.
    fd, temporary = tempfile.mkstemp(prefix='.execution-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(execution, stream, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if len(sys.argv) != 2:
        print('usage: python -m solver_benchmark.gcp_metadata EXECUTION_JSON', file=sys.stderr)
        return 2
    try:
        attach_metadata(sys.argv[1])
    except (OSError, ValueError, UnicodeError, urllib.error.URLError) as exc:
        print(f'failed to record GCP hardware metadata: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
