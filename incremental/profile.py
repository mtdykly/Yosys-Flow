"""Opt-in, nested wall-time attribution for an existing experiment worker job.

Instrumentation stays outside the archive's semantic fingerprint. No evidence,
selection policy or proof is changed. Exclusive spans partition measured time;
inclusive spans describe nested calls and must not be summed together.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import functools
import importlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import time


class Recorder:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.stream = (self.directory/'events.jsonl').open('w', buffering=1)
        self.start = time.perf_counter()
        self.stack = []
        self.events = []
        self.samples = Counter()
        self.restores = []
        self.next_id = 0

    def emit(self, row):
        self.stream.write(json.dumps(row, ensure_ascii=False)+'\n')

    @contextmanager
    def span(self, name, detail=None):
        row = {'id':self.next_id, 'parent':self.stack[-1]['id'] if self.stack else None,
               'name':name, 'start':time.perf_counter()-self.start, 'children_s':0.0,
               'detail':detail or {}}
        self.next_id += 1
        self.stack.append(row)
        self.emit({k:v for k,v in row.items() if k != 'children_s'} | {'event':'begin'})
        try:
            yield row
        except BaseException as exc:
            row['error'] = type(exc).__name__+': '+str(exc)[:240]
            raise
        finally:
            row['end'] = time.perf_counter()-self.start
            row['wall_s'] = row['end']-row['start']
            row['exclusive_s'] = row['wall_s']-row.pop('children_s')
            assert self.stack.pop() is row
            if self.stack:
                self.stack[-1]['children_s'] += row['wall_s']
            self.events.append(row)
            self.emit(row | {'event':'end'})

    def wrap(self, owner, attribute, name=None, metadata=None, result_metadata=None):
        original = getattr(owner, attribute)
        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            detail = metadata(args, kwargs) if metadata else {}
            label = name(args, kwargs) if callable(name) else name or attribute
            with self.span(label, detail) as row:
                result = original(*args, **kwargs)
                if result_metadata:
                    row['result'] = result_metadata(result)
                return result
        # Update already-imported aliases too (e.g. contracts.verify_outputs).
        for module_name, module in list(sys.modules.items()):
            if module is None or not module_name.startswith(('incremental','yosys_flow')):
                continue
            for key,value in list(vars(module).items()):
                if value is original:
                    self.restores.append((module,key,value));setattr(module,key,wrapped)
        self.restores.append((owner,attribute,original))
        setattr(owner,attribute,wrapped)

    def install(self):
        names = ['incremental.automatic','incremental.statements','incremental.replace',
                 'incremental.rtlil','incremental.equivalence','yosys_flow.contracts',
                 'yosys_flow.source_regions','yosys_flow.source_endpoints','yosys_flow.native',
                 'yosys_flow.persistent_index','yosys_flow.abc_internal','yosys_flow.abc_joint',
                 'yosys_flow.abc_lineage','yosys_flow.abc_provenance','yosys_flow.hierarchy',
                 'yosys_flow.diagnostics','yosys_flow.region_functions','yosys_flow.extract',
                 'yosys_flow.common','yosys_flow.runner']
        m = {name:importlib.import_module(name) for name in names}
        for module, functions in {
            'incremental.automatic':['run','prepare','contract_for','patch_for','prove_unselected_outputs'],
            'incremental.rtlil':['parse','compare'],
            'incremental.equivalence':['precheck'],
            'incremental.statements':['plan','merge','source_selectors','close_missing_outputs'],
            'incremental.replace':['_run','splice','_recheck_context_archive'],
            'yosys_flow.native':['run_native','load_native'],
            'yosys_flow.persistent_index':['artifact_inventory','write_index','load_index'],
            'yosys_flow.abc_internal':['read_evidence','analyze'],
            'yosys_flow.abc_joint':['joint_cuts','equivalence_cuts'],
            'yosys_flow.abc_provenance':['validate_artifacts','refine'],
            'yosys_flow.hierarchy':['instance_view','electrical_design'],
            'yosys_flow.diagnostics':['group_closure','audit_cut'],
            'yosys_flow.region_functions':['verify_outputs'],
            'yosys_flow.extract':['slice_module','extend_inputs'],
            'yosys_flow.contracts':['outside_paths','output_associations'],
        }.items():
            for function in functions:
                if hasattr(m[module],function):
                    self.wrap(m[module],function,module.rsplit('.',1)[1]+'.'+function)
        context = m['yosys_flow.contracts'].ContractContext
        for function in ('__init__','query','validate','cell'):
            self.wrap(context,function,'contract.'+function)
        self.wrap(context,'build','contract.build',
                  lambda a,k:{'kind':a[1].get('kind')},
                  lambda r:{'cells':len(r['region']['cells']),'inputs':sum(b['direction']=='input' for b in r['boundary']),
                            'outputs':sum(b['direction']=='output' for b in r['boundary'])})
        slicer = m['incremental.statements'].Slice
        self.wrap(slicer,'__init__','slice.initialize')
        self.wrap(slicer,'build','slice.build',lambda a,k:{'selected_cells':len(a[1]['region']['cells'])},
                  lambda r:{'ready':bool(r[0]),'missing_outputs':len(r[1]),
                            'copied_coarse_cells':r[0][3]['local_coarse_cell_count'] if r[0] else None})
        source = m['yosys_flow.source_regions']
        self.wrap(source,'archived','source.archived',lambda a,k:{'cache_hit':hasattr(a[0],'_source_coarse')})
        self.wrap(source,'make_cut','source.make_cut')
        self.wrap(source,'prove','source.prove',result_metadata=lambda r:{k:r[k] for k in ('status','missing_input_bits','reason') if k in r})
        self.wrap(source,'prove_design','source.prove_design')
        def command_name(a,k):
            script = a[2]
            return 'source.sat' if 'sat -enable_undef' in script else 'source.lower_old' if 'read_rtlil' in script else 'source.miter'
        self.wrap(source,'command',command_name)
        self.wrap(m['incremental.replace'],'run_yosys',lambda a,k:'yosys.'+a[2])
        trace = m['yosys_flow.native'].NativeTrace
        for function in ('__init__','compare_netlist','compare_abc_artifacts'):
            self.wrap(trace,function,'trace.'+function)
        self.wrap(m['yosys_flow.common'],'digest','file.hash',lambda a,k:{'file':str(a[0])})
        self.wrap(m['yosys_flow.source_endpoints'].Endpoints,'__init__','endpoints.initialize')
        self.wrap(m['yosys_flow.abc_internal'].Network,'truth','abc.network_truth')
        # Native run's execute() is a closure: observe its subprocess boundary.
        def process_name(a,k):
            command = a[0] if a else k.get('args', [])
            if isinstance(command,(list,tuple)) and '-s' in command:
                return 'process.'+Path(command[command.index('-s')+1]).stem
            return 'process.other'
        self.wrap(subprocess,'run',process_name)

    def sample(self, signum, frame):
        # Statistical Python CPU attribution, not child-tool CPU or wall time.
        # Keep bounded stacks; do not retain frame/local objects.
        stack=[]
        while frame and len(stack)<14:
            filename=frame.f_code.co_filename
            if '/Yosys-Flow/' in filename:
                stack.append(filename.split('/Yosys-Flow/',1)[1]+':'+str(frame.f_lineno)+':'+frame.f_code.co_name)
            frame=frame.f_back
        self.samples[tuple(stack)]+=1

    def finish(self, status):
        signal.setitimer(signal.ITIMER_PROF,0)
        self.stream.close()
        aggregate={}
        for row in self.events:
            item=aggregate.setdefault(row['name'],{'calls':0,'wall_s':0.0,'exclusive_s':0.0})
            item['calls']+=1;item['wall_s']+=row['wall_s'];item['exclusive_s']+=row['exclusive_s']
        report={'status':status,'elapsed_s':time.perf_counter()-self.start,
                'exclusive_sum_s':sum(r['exclusive_s'] for r in self.events),
                'scopes':aggregate,'spans':self.events,
                'python_cpu_samples':[{'stack':list(k),'samples':n} for k,n in self.samples.most_common()],
                'notes':['Inclusive nested wall times overlap; only exclusive times partition the root span.',
                         'Instrumentation overhead is included; final before/after performance must be measured without these wrappers.',
                         'CPU samples describe Python locations only, not total CPU fractions or child Yosys/ABC costs.']}
        (self.directory/'profile.json').write_text(json.dumps(report,indent=2)+'\n')
        for owner,attribute,value in reversed(self.restores):setattr(owner,attribute,value)
        return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job',type=Path,help='existing experiment_worker job JSON, in a new output directory')
    parser.add_argument('--profile-out',type=Path,required=True)
    parser.add_argument('--timeout',type=float,default=360)
    args=parser.parse_args()
    job=json.loads(args.job.read_text())
    if args.timeout<=0:parser.error('timeout must be positive')
    # Import before profiling; keep startup/import costs outside worker timing.
    from .experiment_worker import execute
    rec=Recorder(args.profile_out);rec.install()
    old_alarm=signal.getsignal(signal.SIGALRM);old_prof=signal.getsignal(signal.SIGPROF)
    def deadline(signum,frame):raise TimeoutError('profile job wall deadline exceeded')
    signal.signal(signal.SIGALRM,deadline);signal.signal(signal.SIGPROF,rec.sample)
    signal.setitimer(signal.ITIMER_REAL,args.timeout)
    signal.setitimer(signal.ITIMER_PROF,0.02,0.02)
    status='failed'
    try:
        with rec.span('worker'):
            result=execute(job,args.job.resolve().parent)
            status=result['status']
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        rec.finish(status)
        signal.signal(signal.SIGALRM,old_alarm);signal.signal(signal.SIGPROF,old_prof)
    print(json.dumps({'status':status,'profile':str(args.profile_out/'profile.json')}))
    return 0 if status=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
