import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yosys_flow.abc_partition import Graph, Partitioner
from yosys_flow.common import TraceError, digest
from yosys_flow.proof_checkpoint import CheckpointStore, BoundedPartitioner, StopPreparation
from yosys_flow.proof_bridge import BridgeSession


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.key=patch('yosys_flow.proof_store._authentication_key',return_value=b't'*32)
        self.key.start();self.addCleanup(self.key.stop)

    def test_partial_write_keeps_previous_authenticated_state_and_rejects_corruption(self):
        g=Graph('.model t\n.inputs a b\n.outputs y\n.names a b y\n11 1\n.end\n')
        p=BoundedPartitioner(g,g);p.check_budget=lambda:None
        store=CheckpointStore(self.root/'work','baseline',{})
        store.save('engine',p,{'completed':['first']})
        old=store.path.read_bytes()
        with patch('yosys_flow.proof_checkpoint.atomic_json',side_effect=StopPreparation('power loss')):
            with self.assertRaises(StopPreparation):store.save('engine',p,{'completed':['second']})
        self.assertEqual(store.path.read_bytes(),old)
        restored=CheckpointStore(self.root/'work','baseline',{},resume=True)
        self.assertEqual(restored.entries['engine']['metadata']['completed'],['first'])
        q=restored.load('engine')
        self.assertEqual(q.region(['y']),p.region(['y']))
        with sqlite3.connect(restored.directory/restored.entries['engine']['artifact']) as db:
            db.execute("UPDATE state SET value='99' WHERE field='a'")
        with self.assertRaisesRegex(TraceError,'hash mismatch'):restored.load('engine')
        with self.assertRaisesRegex(TraceError,'baseline'):CheckpointStore(self.root/'work','wrong',{},resume=True)

    def test_bridge_eviction_and_resume_never_rebuild_or_reinstall_completed_engines(self):
        header='.model t\n.inputs a b c d\n.outputs y z\n'
        old=Graph(header+'.names a b p\n11 1\n.names a c q\n11 1\n.names p q y\n1- 1\n-1 1\n.names y d z\n11 1\n.end\n')
        middle=header+'.names b c r\n1- 1\n-1 1\n.names a r y\n11 1\n.names y d z\n11 1\n.end\n'
        final=Graph(header+'.names b c n\n00 1\n.names a n y\n10 1\n.names y d z\n11 1\n.end\n')
        path=self.root/'middle.blif';path.write_text(middle)
        witness=SimpleNamespace(stages=[{'file':path.name,'sha256':digest(path),'command':'&dch'}],
            file=lambda name,sha:self.root/name,install=lambda engine,progress:None)
        endpoint=BoundedPartitioner(old,final);endpoint.check_budget=lambda:None
        store=CheckpointStore(self.root/'work','base',{});store.save('endpoint',endpoint,{'outputs':[]})
        session=BridgeSession(endpoint,witness,store,'endpoint',lambda *a,**k:None,lambda:None,cache_engines=1)
        result=session.prove([('y','y',0)])
        self.assertEqual(result['accepted'],[['y','y',0]])
        self.assertEqual(session.stats['engine_builds'],2)
        resumed=CheckpointStore(self.root/'work','base',{},resume=True)
        q=resumed.load('endpoint')
        self.assertEqual(q.classes.find(q.a['y']),q.classes.find(q.b['y']))
        second=BridgeSession(q,witness,resumed,'endpoint',lambda *a,**k:None,lambda:None,cache_engines=1)
        with patch.object(BoundedPartitioner,'__init__',side_effect=AssertionError('reconstructed')):
            result=second.prove([('z','z',0)])
        self.assertEqual(result['accepted'],[['z','z',0]])
        self.assertEqual(second.stats['engine_builds'],0)
        self.assertEqual(second.stats['engine_restores'],2)
        # Compare against the original uncached bridge for polarity and acceptance.
        from yosys_flow.abc_checkpoints import bridge
        reference=Partitioner(old,final)
        expected=bridge(reference,[('z','z',0)],[(Graph(middle),{'command':'&dch'}),(final,{'endpoint':'final'})])
        self.assertEqual(result['accepted'],expected['accepted'])


from test_native import NATIVE


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built')
class ResumeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from yosys_flow.cli import parser
        from yosys_flow.runner import run
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        source=cls.root/'old.v';source.write_text('module top(input a,b,c,d,e,output y,z);\nassign y=(a&b)^c;\nassign z=d|e;\nendmodule\n')
        cls.old=cls.root/'old'
        run(parser().parse_args(['run','--source',str(source),'--top','top','--out',str(cls.old),'--native-yosys',str(NATIVE)]))

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def args(self,name,*extra):
        from yosys_flow.cli import parser
        return parser().parse_args(['prepare-evidence',str(self.old),'--out',str(self.root/name),*extra])

    def test_interrupt_after_one_batch_preserves_online_library_and_resume_skips_completed_output(self):
        from yosys_flow.prepare_evidence import prepare
        from yosys_flow.contracts import ContractContext
        from yosys_flow.proof_store import Reader,StoredPartitioner
        original=BoundedPartitioner.targeted;calls=[]
        def interrupted(engine,outputs,**kwargs):
            calls.extend(outputs)
            if len(calls)>1:raise StopPreparation('injected interruption')
            return original(engine,outputs,**kwargs)
        with patch.object(BoundedPartitioner,'targeted',interrupted):
            first=prepare(self.args('interrupted','--refine-outputs','--batch-outputs','1'))
        self.assertEqual(first['status'],'base_ready_partial')
        c=ContractContext(self.old,compact=True)
        reader=Reader(c,self.root/'interrupted')
        inv=reader.document['invocations'][0]
        before=StoredPartitioner(reader,inv)
        self.assertTrue(before.first.pos)
        completed=calls[0];retried=[]
        def tracked(engine,outputs,**kwargs):
            retried.extend(outputs);return original(engine,outputs,**kwargs)
        with patch.object(BoundedPartitioner,'__init__',side_effect=AssertionError('endpoint rebuild')), \
             patch('yosys_flow.abc_checkpoints.capture',side_effect=AssertionError('witness capture repeated')), \
             patch.object(BoundedPartitioner,'targeted',tracked):
            result=prepare(self.args('interrupted','--resume','--refine-outputs','--batch-outputs','1'))
        self.assertEqual(result['status'],'ready')
        self.assertNotIn(completed,retried)
        # An already-open reader still uses its immutable generation after publication.
        self.assertEqual(before.a[before.first.pos[0]],before.a[before.first.pos[0]])
        reader.close()
        prepare(self.args('continuous','--refine-outputs','--batch-outputs','1'))
        with_a=Reader(c,self.root/'interrupted');with_b=Reader(c,self.root/'continuous')
        a=StoredPartitioner(with_a,inv);b=StoredPartitioner(with_b,inv)
        for output in a.first.pos:self.assertEqual(a.region([output]),b.region([output]))
        with_a.close();with_b.close()

    def test_base_survives_failed_enhancement_and_legacy_unsigned_resume_is_rejected(self):
        from yosys_flow.prepare_evidence import prepare
        from yosys_flow.proof_store import Reader
        from yosys_flow.contracts import ContractContext
        with patch.object(BoundedPartitioner,'targeted',side_effect=RuntimeError('solver crash')):
            with self.assertRaisesRegex(RuntimeError,'solver crash'):
                prepare(self.args('failed','--refine-outputs'))
        r=Reader(ContractContext(self.old,compact=True),self.root/'failed');r.close()
        report=json.loads((self.root/'failed/report.json').read_text())
        self.assertTrue(report['base_available']);self.assertEqual(report['status'],'failed')
        (self.root/'legacy').mkdir()
        with self.assertRaisesRegex(TraceError,'no authenticated'):
            prepare(self.args('legacy','--resume'))

    def test_enhancement_budget_retains_base_and_can_continue(self):
        from yosys_flow.prepare_evidence import prepare
        from yosys_flow.contracts import ContractContext
        from yosys_flow.proof_store import Reader
        first=prepare(self.args('budget','--refine-outputs','--enhance-budget','0.000000001'))
        self.assertEqual(first['status'],'base_ready_partial')
        self.assertIn('budget',first['reason'])
        reader=Reader(ContractContext(self.old,compact=True),self.root/'budget');reader.close()
        second=prepare(self.args('budget','--resume','--refine-outputs','--enhance-budget','30'))
        self.assertEqual(second['status'],'ready')
        self.assertTrue(second['resumed'])
        self.assertEqual(len(list((self.root/'budget').glob('report-*.json'))),2)

    def test_base_interruption_retains_elapsed_time_and_resumes_without_endpoint_rebuild(self):
        from yosys_flow.prepare_evidence import prepare
        from yosys_flow.contracts import ContractContext
        from yosys_flow.proof_store import Reader
        with patch('yosys_flow.checkpoint_evidence.Checkpoints.install',side_effect=StopPreparation('base interrupted')):
            first=prepare(self.args('base-interrupted'))
        self.assertEqual(first['status'],'base_ready_partial')
        self.assertGreater(first['timings']['base_evidence_seconds'],0)
        self.assertLessEqual(first['timings']['archive_load_seconds']+first['timings']['base_evidence_seconds'],
                             first['timings']['total_seconds'])
        reader=Reader(ContractContext(self.old,compact=True),self.root/'base-interrupted');reader.close()
        with patch.object(BoundedPartitioner,'__init__',side_effect=AssertionError('endpoint rebuild')):
            second=prepare(self.args('base-interrupted','--resume'))
        self.assertEqual(second['status'],'ready')
