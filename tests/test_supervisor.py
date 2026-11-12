import json
import os
from pathlib import Path
import struct
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from solver_benchmark.supervisor import run_worker


WORKER = r'''
import ctypes, json, mmap, os, resource, signal, socket, struct, sys, time
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
# RLIMIT_CORE is ignored by piped core handlers; avoid host crash-reporting delays.
libc=ctypes.CDLL(None,use_errno=True)
libc.prctl.argtypes=[ctypes.c_int]+[ctypes.c_ulong]*4
if libc.prctl(4,0,0,0,0) != 0: raise OSError(ctypes.get_errno(), 'PR_SET_DUMPABLE')
if libc.prctl(3,0,0,0,0) != 0: raise RuntimeError('fixture is still dumpable')
args = dict(arg[2:].split('=',1) for arg in sys.argv[1:])
s = socket.socket(fileno=int(args['sb-control-fd']))
mode = os.environ.get('MODE','good')
def send(v):
    b=json.dumps(v).encode(); s.sendall(struct.pack('<I',len(b))+b)
def exact(n):
    b=b''
    while len(b)<n:
        x=s.recv(n-len(b))
        if not x: sys.exit(2)
        b+=x
    return b
def recv(): return json.loads(exact(struct.unpack('<I',exact(4))[0]))
if mode=='crash': os.kill(os.getpid(), signal.SIGABRT)
if mode=='hang': time.sleep(60)
if mode=='truncated': s.sendall(struct.pack('<I',99)+b'{'); sys.exit(0)
send({'type':'hello','protocols':[{'major':1,'max_minor':0}], 'data_formats':['csr-f64-i32-i64-le'],
      'instrumentation_modes':['baseline','trace'],'worker':{'name':'fixture','version':'1','build_id':'test'}})
r=recv()
identity={k:r[k] for k in ('case_id','pass_id','provenance')}
def emit(kind, **fields): send({'type':kind,**identity,**fields})
if mode=='unavailable': emit('instrumentation_unavailable',message='not installed');sys.exit(4)
if mode=='mutate':
    try: os.pwrite(int(args['sb-input-fd']), b'x', 0);sys.exit(12)
    except PermissionError: pass
emit('ready',protocol=r['protocol'],data_format=r['data_format'],instrumentation=r['instrumentation'])
assert recv()['type']=='start'
out=mmap.mmap(int(args['sb-output-fd']),int(r['buffers']['output_bytes']))
out[:]=struct.pack('<'+'d'*(len(out)//8), *([1.]*(len(out)//8)))
if mode=='logs':
    os.write(2,b'x'*1200000)
for kind,count in [('warmup',r['plan']['warmup_runs']),('measured',r['plan']['measured_runs'])]:
    for index in range(count):
        trial={'kind':kind,'index': index+1 if mode=='wrong_trial' else index}
        emit('trial_begin',trial=trial)
        result='success'
        for phase in ['setup','pre_analyze','analysis','numeric','solve']:
            emit('phase_begin',trial=trial,phase=phase)
            if mode=='late_crash' and phase=='numeric': os.kill(os.getpid(),signal.SIGSEGV)
            result='unsupported' if mode=='unsupported' else 'failed' if mode=='failed' else 'success'
            if mode=='invalid_solve' and phase=='solve': result='not_applicable'
            extra={}
            if r['instrumentation']['kind']=='trace':
                extra={'trace':{'begin_monotonic_ns':'1','end_monotonic_ns':'2','pid':os.getpid(),'tid':os.getpid(),'clock':'CLOCK_MONOTONIC'},
                       'observations':{'source':'fixture','minor_page_faults':0}}
            emit('phase_end',trial=trial,phase=phase,status=result,message='',iterations=None,
                 times=None if kind=='warmup' else {'wall_seconds':0.01,'cpu_seconds':0.01},**extra)
            if result in ('unsupported','failed'): break
        emit('teardown_begin',trial=trial)
        emit('teardown_end',trial=trial,times=None if kind=='warmup' else {'wall_seconds':0.001,'cpu_seconds':0.001},**extra)
        emit('trial_complete',trial=trial,outcome=result,solution_ready=result=='success')
        assert recv()['type']=='trial_ack'
        if result in ('unsupported','failed'):
            emit('complete',outcome=result);sys.exit(3 if result=='unsupported' else 1)
if mode!='missing_complete': emit('complete',outcome='success')
if mode=='wrong_exit': sys.exit(3)
'''


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.script = self.path / 'worker.py'
        self.script.write_text(WORKER)
        self.worker = {'executable': sys.executable, 'arguments': [str(self.script)], 'environment': {}}
        self.request = {'case_id':'small','pass_id':'baseline',
                        'provenance':{'profile_hash':'sha256:'+'0'*64,'version_manifest_hash':'sha256:'+'1'*64,'input_hash':'sha256:'+'2'*64},
                        'data_format':'csr-f64-i32-i64-le',
                        'matrix':{'order':2,'nonzeros':'2','encoding':'real','properties':{}},
                        'dense':{'rows':2,'columns':1,'row_stride':'1','column_stride':'2'},
                        'plan':{'warmup_runs':1,'measured_runs':2,'case_timeout_seconds':3},
                        'options':{'thread_limit':1,'relative_tolerance':1e-10,'absolute_tolerance':0,'iteration_limit':'100'},
                        'instrumentation':{'kind':'baseline','collectors':[]}}
        self.arrays = {'row_offsets':struct.pack('<3q',0,1,2), 'column_indices':struct.pack('<2i',0,1),
                       'values':struct.pack('<2d',1,1),'rhs':struct.pack('<2d',1,1)}

    def run_case(self, mode='good', accepted=True):
        self.worker['environment']['MODE']=mode
        return run_worker(self.worker,self.request,self.arrays,
                          lambda values: {'accepted':accepted and values==[1.,1.]},self.path/'results')

    def test_success_warmups_and_samples(self):
        report=self.run_case()
        self.assertEqual(report['outcome'],'success',report)
        self.assertEqual(len(report['accuracy']),3)
        self.assertEqual(len(report['solution_artifacts']),3)
        self.assertEqual(len(report['phases']),15)
        self.assertEqual(sum(p['times'] is not None for p in report['phases']),10)
        self.assertGreater(report['process']['max_rss_bytes'],0)
        self.assertIsNone(report['process']['terminating_signal'])

    def test_identity_must_match_manifest(self):
        self.worker['identity']={'name':'different','version':'1','build_id':'test'}
        report=self.run_case()
        self.assertEqual(report['outcome'],'protocol_error')
        self.assertIn('manifest',report['diagnostics'][0])

    def test_sealed_input(self):
        self.assertEqual(self.run_case('mutate')['outcome'],'success')

    def test_accuracy_failed_preserves_all_samples(self):
        report=self.run_case(accepted=False)
        self.assertEqual(report['outcome'],'accuracy_failed')
        self.assertEqual(len(report['accuracy']),3)

    def test_crash_keeps_partial_progress(self):
        report=self.run_case('late_crash')
        self.assertEqual(report['outcome'],'worker_crashed',report)
        self.assertEqual(report['process']['terminating_signal'],signal.SIGSEGV)
        self.assertEqual(report['phases'][-1]['phase'],'numeric')
        self.assertFalse(report['phases'][-1]['completed'])
        self.assertGreater(report['process']['max_rss_bytes'],0)

    def test_early_crash(self):
        report=self.run_case('crash')
        self.assertEqual(report['outcome'],'worker_crashed',report)
        self.assertEqual(report['process']['terminating_signal'],signal.SIGABRT)

    def test_timeout(self):
        self.request['plan']['case_timeout_seconds']=0.15
        report=self.run_case('hang')
        self.assertEqual(report['outcome'],'timed_out',report)
        self.assertIsNotNone(report['process']['terminating_signal'])

    def test_protocol_failures(self):
        for mode in ['wrong_trial','missing_complete','wrong_exit','invalid_solve','truncated']:
            with self.subTest(mode=mode):
                self.assertEqual(self.run_case(mode)['outcome'],'protocol_error')

    def test_decline_and_backend_failure(self):
        self.assertEqual(self.run_case('unsupported')['outcome'],'unsupported')
        self.assertEqual(self.run_case('failed')['outcome'],'solver_failed')

    def test_missing_executable(self):
        self.worker['executable']='/does/not/exist'
        report=self.run_case()
        self.assertEqual(report['outcome'],'launch_failed')
        self.assertIsNone(report['process']['max_rss_bytes'])

    def test_logs_drained_and_capped(self):
        report=self.run_case('logs')
        self.assertEqual(report['outcome'],'success',report)
        self.assertTrue(report['logs_truncated'])
        self.assertEqual((self.path/'results/stderr.log').stat().st_size,1048576)

    def test_trace_and_unavailable(self):
        self.request['instrumentation']={'kind':'trace','collectors':['linux.phase_resources']}
        report=self.run_case()
        self.assertEqual(report['outcome'],'success',report)
        self.assertTrue(report['trace_complete'])
        self.assertTrue(Path(report['trace_artifacts'][0]).exists())
        self.assertEqual(self.run_case('unavailable')['outcome'],'unsupported_instrumentation')

    def test_perf_unavailable_does_not_fallback(self):
        self.request['instrumentation']={'kind':'trace','collectors':['linux.vm_events']}
        with patch('solver_benchmark.supervisor.shutil.which',return_value=None):
            report=self.run_case()
        self.assertEqual(report['outcome'],'unsupported_instrumentation')
        self.assertFalse(report['process']['launched'])


@unittest.skipUnless(os.environ.get('SB_WORKER_FIXTURE'), 'compiled worker fixture not configured')
class CppWorkerIntegrationTests(unittest.TestCase):
    def test_real_runtime_lifecycle_and_failures(self):
        fixture = SupervisorTests()
        fixture.setUp()
        self.addCleanup(fixture.temp.cleanup)
        expected = {'good':'success', 'wrong':'accuracy_failed', 'unsupported':'unsupported',
                    'exception':'solver_failed', 'crash':'worker_crashed', 'hang':'timed_out'}
        for mode, outcome in expected.items():
            with self.subTest(mode=mode):
                fixture.request['plan']['case_timeout_seconds'] = 0.2 if mode=='hang' else 5
                report = run_worker({'executable':os.environ['SB_WORKER_FIXTURE'],
                                     'arguments':['--fixture='+mode]},
                                    fixture.request, fixture.arrays,
                                    lambda values: {'accepted':values==[1.,1.]}, fixture.path/mode)
                self.assertEqual(report['outcome'],outcome,report)
        fixture.request['plan']['case_timeout_seconds'] = 5
        fixture.request['instrumentation'] = {'kind':'trace','collectors':['linux.phase_resources']}
        report = run_worker({'executable':os.environ['SB_WORKER_FIXTURE'],'arguments':['--fixture=good']},
                            fixture.request,fixture.arrays,lambda values:{'accepted':values==[1.,1.]},fixture.path/'trace')
        self.assertEqual(report['outcome'],'success',report)
        self.assertTrue(report['trace_complete'])
        self.assertEqual(len(report['solution_artifacts']),3)
        self.assertTrue(report['trace_artifact_hashes'])


if __name__=='__main__':
    unittest.main()
