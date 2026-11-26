import unittest
from unittest.mock import patch

from yosys_flow.abc_partition import Graph, Partitioner
from yosys_flow.boundary_scan import hop_engine, sweep, install
from yosys_flow.proof_checkpoint import StopPreparation
from yosys_flow.common import TraceError

H='.model t\n.inputs a b c d\n.outputs y z\n'
OLD=H+'.names a b p\n11 1\n.names a c q\n11 1\n.names p q u\n1- 1\n-1 1\n.names u d y\n11 1\n.names b c z\n11 1\n.end\n'
MID=H+'.names b c r\n1- 1\n-1 1\n.names a r m\n11 1\n.names m d y\n11 1\n.names b c z\n11 1\n.end\n'
FINAL=H+'.names b c n\n00 1\n.names a n s\n10 1\n.names s d y\n11 1\n.names b c z\n11 1\n.end\n'


class BoundaryScanTests(unittest.TestCase):
    def test_all_nodes_compose_across_checkpoints_and_preserve_polarity(self):
        endpoint=Partitioner(Graph(OLD),Graph(FINAL))
        previous=endpoint.first
        with patch.object(Partitioner,'global_cec',side_effect=AssertionError('unbounded proof')):
            for graph in (Graph(MID),endpoint.final):
                hop=hop_engine(endpoint,previous,graph)
                result=sweep(hop)
                self.assertGreater(result['matched'],0)
                install(endpoint,hop,{'test_checkpoint':len(endpoint.proofs)})
                previous=graph
        cls=endpoint.classes
        self.assertEqual(cls.find(endpoint.a['u']),cls.find(endpoint.b['s']))
        self.assertNotEqual(cls.find(endpoint.a['u']),cls.find(endpoint.b['n']))
        # Two later selections use this same preparation, no preparation seeds.
        self.assertTrue(endpoint.region(['u'])['export_nodes'])
        self.assertTrue(endpoint.region(['z'])['export_nodes'])
        self.assertTrue(all(row['target_independent'] for row in endpoint.proofs))

    def test_simulation_collision_never_becomes_equality(self):
        old=Graph('.model t\n.inputs a b\n.outputs y\n.names a b y\n11 1\n.end\n')
        new=Graph('.model t\n.inputs a b\n.outputs y\n.names a b y\n1- 1\n-1 1\n.end\n')
        endpoint=Partitioner(old,new);hop=hop_engine(endpoint,old,new)
        hop.sa['y']=hop.sb['y']=0;hop.candidates[0]=['y']
        result=sweep(hop);install(endpoint,hop,{})
        self.assertEqual(result['new_proofs'],0)
        self.assertNotEqual(endpoint.classes.find(endpoint.a['y']),endpoint.classes.find(endpoint.b['y']))

    def test_interruption_does_not_install_unproved_candidates(self):
        endpoint=Partitioner(Graph(OLD),Graph(FINAL));hop=hop_engine(endpoint,endpoint.first,Graph(MID))
        def stop():raise StopPreparation('budget')
        with self.assertRaises(StopPreparation):sweep(hop,check=stop)
        install(endpoint,hop,{})
        self.assertFalse(hop.proofs)
        self.assertNotEqual(endpoint.classes.find(endpoint.a['u']),endpoint.classes.find(endpoint.b['s']))

    def test_locally_proved_inversion_is_preserved_when_published(self):
        negated=FINAL.replace('.names a n s\n10 1', '.names a n s\n0- 1\n-1 1')
        endpoint=Partitioner(Graph(OLD),Graph(negated))
        hop=hop_engine(endpoint,endpoint.first,endpoint.final);sweep(hop);install(endpoint,hop,{})
        self.assertEqual(endpoint.classes.find(endpoint.a['u']),endpoint.classes.find(endpoint.b['s'])^1)
        self.assertTrue(any(p['inverted'] for p in hop.proofs))

    def test_input_binding_mismatch_rejected(self):
        endpoint=Partitioner(Graph(OLD),Graph(FINAL))
        with self.assertRaisesRegex(TraceError,'input interface'):
            hop_engine(endpoint,endpoint.first,Graph(MID.replace('.inputs a b c d','.inputs a b c d e')))


class BoundaryJournalTests(unittest.TestCase):
    def test_interrupted_scan_reuses_signed_hop_and_rejects_tampering(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from yosys_flow.common import digest
        from yosys_flow.proof_checkpoint import CheckpointStore
        from yosys_flow.boundary_scan import prepare
        with tempfile.TemporaryDirectory() as temporary, \
             patch('yosys_flow.proof_store._authentication_key',return_value=b't'*32):
            root=Path(temporary);path=root/'middle.blif';path.write_text(MID)
            witness=SimpleNamespace(stages=[{'file':path.name,'sha256':digest(path)}],file=lambda n,s:root/n)
            store=CheckpointStore(root/'work','old-baseline',{})
            endpoint=Partitioner(Graph(OLD),Graph(FINAL));store.save('endpoint',endpoint)
            metadata={};calls=[]
            from yosys_flow.boundary_scan import sweep as original
            def interrupted(hop,**kwargs):
                calls.append(1)
                if len(calls)>1:raise StopPreparation('interrupted')
                return original(hop,**kwargs)
            with patch('yosys_flow.boundary_scan.sweep',interrupted):
                with self.assertRaises(StopPreparation):prepare(endpoint,witness,store,'endpoint',metadata,lambda *a,**k:None,lambda:None)
            # Simulate a crash before the expensive full-engine save. Restore
            # the base, replay the committed small proof journal, resume next hop.
            restored=CheckpointStore(root/'work','old-baseline',{},resume=True)
            fresh=restored.load('endpoint');retries=[]
            def tracked(hop,**kwargs):retries.append(hop.final);return original(hop,**kwargs)
            with patch('yosys_flow.boundary_scan.sweep',tracked):
                result=prepare(fresh,witness,restored,'endpoint',{},lambda *a,**k:None,lambda:None)
            self.assertTrue(result['complete']);self.assertEqual(len(retries),1)
            self.assertEqual(fresh.classes.find(fresh.a['u']),fresh.classes.find(fresh.b['s']))
            row=restored.completed['endpoint:boundary:0'];(root/'work'/row['artifact']).write_text('{}')
            with self.assertRaisesRegex(TraceError,'journal hash'):
                prepare(restored.load('endpoint'),witness,restored,'endpoint',{},lambda *a,**k:None,lambda:None)

    def test_lost_internal_cut_uses_a_group_without_inventing_single_gate_ownership(self):
        endpoint=Partitioner(Graph(OLD),Graph(FINAL));hop=hop_engine(endpoint,endpoint.first,endpoint.final)
        sweep(hop);install(endpoint,hop,{})
        self.assertNotEqual(endpoint.classes.sides[endpoint.classes.find(endpoint.a['p'])//2],3)
        region=endpoint.region(['p'])
        self.assertIn('s',region['export_nodes'])
        self.assertTrue(region['boundary'])


class ExhaustiveCorrespondenceTests(unittest.TestCase):
    def test_composed_classes_preserve_truth_tables_even_for_nonequivalent_checkpoints(self):
        import random
        from yosys_flow.abc_internal import variables
        rng=random.Random(7429)
        def graph():
            names=['a','b','c','d','e','f'];lines=['.model t','.inputs '+' '.join(names),'.outputs y']
            for i in range(60):
                a,b=rng.choices(names,k=2);name='n'+str(i)
                sop=rng.choice(['11 1','1- 1\n-1 1','10 1\n01 1','00 1','0- 1'])
                lines.extend(['.names '+a+' '+b+' '+name,sop]);names.append(name)
            lines.extend(['.names '+names[-1]+' y','1 1','.end'])
            return Graph('\n'.join(lines)+'\n')
        for _ in range(25):
            first,middle,last=graph(),graph(),graph()
            endpoint=Partitioner(first,last)
            previous=first
            for g in (middle,last):
                hop=hop_engine(endpoint,previous,g);sweep(hop);install(endpoint,hop,{})
                previous=g
            one=(1<<64)-1;bindings=dict(zip(first.pis,variables(6)));known={}
            for g,values in ((first,endpoint.a),(last,endpoint.b)):
                truth=g.values(g.order,bindings,one);truth[None]=0
                for name,lit in values.items():
                    representative=endpoint.classes.find(lit)
                    normalized=truth[name]^(one if representative&1 else 0)
                    self.assertEqual(known.setdefault(representative//2,normalized),normalized)
