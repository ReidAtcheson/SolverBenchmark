"""A local executable profile runner; artifacts can be collected by a batch job."""
from __future__ import annotations
import argparse
import datetime
import json
from pathlib import Path
import sys
import uuid

import rfc8785
from .profiles import (load_json, validate_profile, resolve_workers, canonical_hash,
                       version_manifest, host_record, input_hash, digest, available_physical_cores)
from .problems import generate_case
from .accuracy import validate_solution
from .supervisor import run_worker


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def summarize(result):
    """Keep all raw data; aggregate only accepted measured trials of this pass."""
    accepted = {(a['trial']['kind'], a['trial']['index']) for a in result.get('accuracy', [])
                if a['accuracy'].get('accepted')}
    times = {}
    for phase in result.get('phases', []):
        trial = phase.get('trial', {})
        timing = phase.get('times')
        if trial.get('kind') != 'measured' or (trial.get('kind'), trial.get('index')) not in accepted:
            continue
        if phase.get('result', {}).get('status') not in ('success', 'not_applicable') or not timing:
            continue
        row = times.setdefault(str(trial['index']), {})
        row[phase['phase']] = timing
    samples = []
    for index, row in times.items():
        if all(p in row for p in ('setup', 'pre_analyze', 'analysis', 'numeric', 'solve')):
            samples.append({'trial_index': int(index), **{
                unit: {'core': sum(row[p][unit] for p in ('analysis','numeric','solve')),
                       'total_solver': sum(t[unit] for t in row.values())}
                for unit in ('wall_seconds','cpu_seconds')}})
    return {'accepted_measured_samples': samples,
            'accuracy_failures': sum(not a['accuracy'].get('accepted',False) for a in result.get('accuracy', []))}


def execute(profile_path, deployment_path, output, max_entries=5_000_000):
    profile = validate_profile(load_json(profile_path))
    core_limit = (available_physical_cores() if any(s['options']['thread_limit'] == 'available_cores'
                                                    for s in profile['solvers']) else None)
    deployment = load_json(deployment_path)
    workers = resolve_workers(deployment, deployment_path, profile)
    # Validate/materialize all recipes before launching any benchmark process.
    problems = {case['id']: generate_case(case, max_entries=max_entries) for case in profile['cases']}
    manifest = version_manifest(workers)
    profile_digest = canonical_hash(profile)
    versions_digest = canonical_hash(manifest)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'profile.json', profile)
    (output/'profile.canonical.json').write_bytes(rfc8785.dumps(profile))
    write_json(output/'version-manifest.json', manifest)
    write_json(output/'execution.json', {'run_id': str(uuid.uuid4()),
        'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'host': host_record(), 'deployment': deployment,
        'resolved_executables': {key: value['executable'] for key,value in workers.items()},
        'max_entries': max_entries, 'profile_hash': profile_digest,
        'version_manifest_hash': versions_digest})
    results=[]
    with (output/'results.jsonl').open('w') as stream:
        for case in profile['cases']:
            problem = problems[case['id']]
            case_dir = output/'inputs'/case['id']; case_dir.mkdir(parents=True)
            ihash = input_hash(problem)
            for name,data in problem['arrays'].items():
                (case_dir/f'{name}.bin').write_bytes(data)
            write_json(case_dir/'metadata.json', {k: problem[k] for k in ('matrix','dense')})
            write_json(case_dir/'reference.json', problem['reference'])
            write_json(case_dir/'generation.json', problem.get('generation', {}))
            write_json(case_dir/'identity.json', {'input_hash':ihash, 'reference_hash':canonical_hash(problem['reference'])})
            for solver in profile['solvers']:
                options = dict(solver['options'])
                if options['thread_limit'] == 'available_cores':
                    options['thread_limit'] = core_limit
                for measurement in profile['passes']:
                    run_dir=output/'runs'/case['id']/solver['id']/measurement['id']
                    run_dir.mkdir(parents=True)
                    request = {'type':'run','protocol':{'major':1,'minor':0},
                        'case_id':case['id']+'/'+solver['id'], 'pass_id':measurement['id'],
                        'data_format':case['data_format'],
                        'provenance': {'profile_hash':profile_digest,'version_manifest_hash':versions_digest,'input_hash':ihash},
                        'matrix':problem['matrix'],'dense':problem['dense'],
                        'instrumentation':measurement['instrumentation'],
                        'plan':{k:measurement[k] for k in ('warmup_runs','measured_runs','case_timeout_seconds')},
                        'options':options}
                    worker = dict(workers[solver['id']])
                    # Apply native thread runtime limits before executable startup.
                    env = dict(worker['environment'])
                    for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','BLIS_NUM_THREADS'):
                        env[name]=str(options['thread_limit'])
                    worker['environment']=env
                    result=run_worker(worker,request,problem['arrays'],
                        lambda x: validate_solution(problem,x,options),run_dir)
                    result.update({'case_id':case['id'],'solver_id':solver['id'],'pass_id':measurement['id'],
                        'provenance':request['provenance'],'instrumentation':measurement['instrumentation'],
                        'options':options,'effective_thread_environment':env})
                    result['summary']=summarize(result)
                    write_json(run_dir/'result.json',result)
                    stream.write(json.dumps(result,allow_nan=False)+'\n'); stream.flush()
                    results.append(result)
                    print(f"{case['id']} / {solver['id']} / {measurement['id']}: {result['outcome']}",flush=True)
    outcomes={}
    for result in results:
        outcomes[result['outcome']]=outcomes.get(result['outcome'],0)+1
    write_json(output/'summary.json', {'profile_hash':profile_digest,'version_manifest_hash':versions_digest,
        'outcomes':outcomes, 'execution_count':len(results)})
    return 0 if all(r['outcome'] in ('success','unsupported','unsupported_instrumentation') for r in results) else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run'); run.add_argument('profile'); run.add_argument('--deployment',required=True)
    run.add_argument('--output',required=True); run.add_argument('--max-entries',type=int,default=5_000_000)
    check=sub.add_parser('validate'); check.add_argument('profile')
    report=sub.add_parser('report', help='compare benchmark outputs in matching HTML and JSON reports')
    report.add_argument('inputs', nargs='+', help='benchmark directories or results.jsonl files')
    report.add_argument('--output', required=True, help='destination HTML file; also writes a sibling .json file')
    report.add_argument('--metric', choices=('wall', 'cpu'), default='wall')
    report.add_argument('--title', default='Solver benchmark report')
    args=parser.parse_args(argv)
    try:
        if args.command=='validate':
            profile=validate_profile(load_json(args.profile))
            for case in profile['cases']: generate_case(case)
            print(canonical_hash(profile)); return 0
        if args.command=='report':
            from .report import write_report
            outputs = write_report(args.inputs, args.output, metric=args.metric, title=args.title)
            for output in outputs:
                print(f'Report written to {output}')
            return 0
        if args.max_entries < 1: raise ValueError('--max-entries must be positive')
        return execute(args.profile,args.deployment,args.output,args.max_entries)
    except Exception as exc:
        print(f'error: {exc}',file=sys.stderr); return 2
