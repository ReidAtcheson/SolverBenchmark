#!/usr/bin/env python3
"""Run a command on one available CPU with a single-thread solver profile."""
import json
import os
import sys
import tempfile
from pathlib import Path


def write_json_atomic(path, value):
    """Never leave a truncated execution record if writing metadata fails."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.affinity-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def attach_affinity_control(metadata_path, execution_path):
    metadata = json.loads(Path(metadata_path).read_text(encoding='utf-8'))
    original = metadata.get('original_affinity')
    selected = metadata.get('selected_cpu')
    if (metadata.get('mode') != 'single_core' or not isinstance(original, list) or not original
            or any(type(cpu) is not int or cpu < 0 for cpu in original)
            or original != sorted(set(original)) or type(selected) is not int or selected not in original):
        raise ValueError('invalid single-core affinity provenance')
    execution_path = Path(execution_path)
    execution = json.loads(execution_path.read_text(encoding='utf-8'))
    if execution.get('host', {}).get('affinity') != [selected] or 'affinity_control' in execution:
        raise ValueError('execution affinity does not match single-core provenance')
    execution['affinity_control'] = metadata
    write_json_atomic(execution_path, execution)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--attach':
        if len(sys.argv) != 4:
            raise ValueError('usage: single_core.py --attach AFFINITY_JSON EXECUTION_JSON')
        attach_affinity_control(sys.argv[2], sys.argv[3])
        return
    source_path, derived_path, *command = sys.argv[1:]
    with open(source_path) as source:
        profile = json.load(source)
    for solver in profile['solvers']:
        solver['options']['thread_limit'] = 1
    with open(derived_path, 'w') as output:
        json.dump(profile, output)
    original_affinity = sorted(os.sched_getaffinity(0))
    selected_cpu = min(original_affinity)
    os.sched_setaffinity(0, {selected_cpu})
    metadata_path = os.environ.get('SB_SINGLE_CORE_AFFINITY_PATH')
    if metadata_path:
        write_json_atomic(metadata_path, {'mode': 'single_core',
                                         'original_affinity': original_affinity,
                                         'selected_cpu': selected_cpu})
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
