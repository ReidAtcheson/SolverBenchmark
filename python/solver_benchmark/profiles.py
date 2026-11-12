"""Profile validation, resolution, and independent recipe/software/input identities."""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys

import jsonschema
import rfc8785

PROPERTIES = ('symmetric', 'structurally_symmetric', 'positive_definite')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def load_json(path):
    def invalid(value):
        raise ValueError(f'invalid JSON number: {value}')
    with open(path, encoding='utf-8') as stream:
        obj = json.load(stream, object_pairs_hook=_pairs, parse_constant=invalid)
    # JCS rejects nonfinite/unsafe integers and invalid Unicode throughout.
    rfc8785.dumps(obj)
    return obj


def digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def canonical_hash(value) -> str:
    return digest(rfc8785.dumps(value))


def file_hash(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return 'sha256:' + h.hexdigest()


def validate_profile(profile):
    schema = json.loads(Path(__file__).with_name('benchmark-profile.schema.json').read_text())
    jsonschema.Draft202012Validator(schema).validate(profile)
    rfc8785.dumps(profile)
    for field in ('cases', 'solvers', 'passes'):
        ids = [entry['id'] for entry in profile[field]]
        if len(ids) != len(set(ids)):
            raise ValueError(f'duplicate {field} IDs')
    for case in profile['cases']:
        for recipe in (case['generator'], case['rhs']['generator']):
            if int(recipe['seed']) > 2**64 - 1:
                raise ValueError('seed exceeds uint64')
    for solver in profile['solvers']:
        if int(solver['options']['iteration_limit']) > 2**64 - 1:
            raise ValueError('iteration_limit exceeds uint64')
    return profile


def input_hash(problem):
    matrix = dict(problem['matrix'])
    props = dict(matrix.get('properties', {}))
    for name in PROPERTIES:
        props.setdefault(name, 'unknown')
    matrix['properties'] = props
    meta = rfc8785.dumps({'data_format': 'csr-f64-i32-i64-le', 'matrix': matrix,
                         'dense': problem['dense']})
    h = hashlib.sha256(b'SolverBenchmark.input.v1\0')
    for block in [meta] + [problem['arrays'][k] for k in ('row_offsets', 'column_indices', 'values', 'rhs')]:
        h.update(struct.pack('<Q', len(block)))
        h.update(block)
    return 'sha256:' + h.hexdigest()


def resolve_workers(deployment, path, profile):
    if set(deployment) != {'schema_version', 'solvers'} or deployment['schema_version'] != '1.0':
        raise ValueError('deployment requires schema_version 1.0 and solvers mapping')
    if not isinstance(deployment['solvers'], dict):
        raise ValueError('deployment solvers must be an object')
    result = {}
    for solver in profile['solvers']:
        name = solver['id']
        entry = deployment['solvers'].get(name)
        if not isinstance(entry, dict) or not {'executable', 'arguments', 'environment'} <= set(entry) or set(entry) - {'executable', 'arguments', 'environment', 'runtime_dependencies'}:
            raise ValueError(f'{name}: deployment requires executable, arguments, environment')
        if not isinstance(entry['executable'], str) or not entry['executable']:
            raise ValueError(f'{name}: executable must be a nonempty path')
        exe = (Path(path).resolve().parent / entry['executable']).resolve()
        if not exe.is_file() or not os.access(exe, os.X_OK):
            raise ValueError(f'{name}: executable is not runnable: {exe}')
        if not isinstance(entry['arguments'], list) or not all(isinstance(x, str) and '\0' not in x for x in entry['arguments']):
            raise ValueError('arguments must be strings without NUL')
        env = entry['environment']
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) and k and '=' not in k and '\0' not in k+v for k,v in env.items()):
            raise ValueError('environment must map variable names to string values')
        dependency_paths = entry.get('runtime_dependencies', {})
        if not isinstance(dependency_paths, dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in dependency_paths.items()):
            raise ValueError('runtime_dependencies must map labels to file paths')
        dependency_hashes = {key: file_hash((Path(path).resolve().parent / value).resolve())
                             for key,value in dependency_paths.items()}
        effective_env = {**os.environ, **env}
        description = subprocess.run([str(exe), *entry['arguments'], '--describe'],
                                     env=effective_env, capture_output=True, timeout=10, check=True)
        identity = json.loads(description.stdout, object_pairs_hook=_pairs)
        result[name] = {**entry, 'executable': str(exe), 'identity': identity,
                        'executable_hash': file_hash(exe), 'runtime_dependency_hashes':dependency_hashes}
    return result


def version_manifest(workers):
    directory = Path(__file__).parent
    return {
        'schema_version': '1.0', 'protocol_version': '1.0',
        'orchestrator': {'version': '0.1.0', 'source_hashes': {
            p.name: file_hash(p) for p in sorted(directory.glob('*.py'))}},
        'generator_contract': 'generated-matrices-v5',
        'profile_schema_hash': file_hash(directory / 'benchmark-profile.schema.json'),
        'python': sys.version,
        'dependencies': {name: importlib.metadata.version(name) for name in ('jsonschema', 'rfc8785')},
        'workers': {key: {'identity': value['identity'], 'executable_hash': value['executable_hash'],
                         'runtime_dependency_hashes':value.get('runtime_dependency_hashes',{})}
                    for key, value in workers.items()},
    }


def host_record():
    cpu = ''
    if Path('/proc/cpuinfo').exists():
        for line in Path('/proc/cpuinfo').read_text().splitlines():
            if line.startswith(('model name', 'Hardware', 'Processor')):
                cpu = line.split(':', 1)[-1].strip()
                break
    return {'system': platform.system(), 'kernel': platform.release(),
            'machine': platform.machine(), 'cpu': cpu,
            'parent_libc': platform.libc_ver(), 'cpu_count': os.cpu_count(),
            'affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None}
