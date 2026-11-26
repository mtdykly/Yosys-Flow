from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_abc_copy import fixture
from yosys_flow.abc_partition import Graph,Partitioner
from yosys_flow.common import TraceError
from yosys_flow.fraig_evidence import export,install
from yosys_flow.abc_snapshots import SnapshotMap


def data():
    before,after,copy,facts=fixture()
    before.record={'kind':'history_network','command_id':1,'seq':8,'representation':'gia'}
    after.record={'kind':'history_network','command_id':1,'seq':9,'representation':'gia'}
    # The copy fixture's old node 5 is !a; recorded and adopted polarity is 1.
    copy={**copy,'kind':'history_copy','command_id':1,'reason':'fraig_sweep_copy'}
    fact={**facts[0],'seq':7,'snapshot':8,'command_id':1,'reason':'fraig_sat_merge'}
    return [{'kind':'command_begin','command_id':1,'argv':['&fraig']},fact,copy],{8:before,9:after}


class FraigEvidenceTests(unittest.TestCase):
    def test_many_shared_snapshots_reuse_structure_but_check_every_fact(self):
        rows,nets=data();source=nets[8];source.nodes=SnapshotMap(source.nodes)
        copy=rows[-1];copy['seq']=100000
        facts=[]
        for i in range(3000):
            snapshot=100+i
            nets[snapshot]=SimpleNamespace(nodes=source.nodes,pis=source.pis,pos=source.pos,
                record={**source.record,'seq':snapshot})
            facts.append({**rows[1],'seq':i+1,'snapshot':snapshot})
        rows=[rows[0],*facts,copy];stats={};progress=[]
        with tempfile.TemporaryDirectory() as temporary, \
             patch('yosys_flow.fraig_evidence.shape',side_effect=AssertionError('shared graph rebuilt')):
            descriptor=export(rows,nets,['a','b'],Path(temporary),
                lambda phase,**details:progress.append(phase),statistics=stats)[0]
            self.assertEqual(descriptor['count'],3000)
            self.assertEqual(stats['facts_checked'],3000)
            self.assertEqual(stats['copy_maps_built'],1)
            self.assertEqual(stats['shape_builds'],0)
            self.assertEqual(stats['shared_node_storage_checks'],3000)
            self.assertIn('fraig_evidence_validate',progress)
            self.assertEqual(progress[-1],'fraig_evidence_complete')
            self.assertEqual(set(stats['phase_seconds']),{'index','validate','write'})
            facts[-1]['new_literal']^=1
            with self.assertRaisesRegex(TraceError,'polarity'):
                export(rows,nets,['a','b'],Path(temporary))

    def test_shared_nodes_do_not_hide_changed_metadata_scope_or_absent_nodes(self):
        for change in ('representation','ports','command','kind','sequence','missing_node'):
            with self.subTest(change=change):
                rows,nets=data();source=nets[8];source.nodes=SnapshotMap(source.nodes)
                snapshot=SimpleNamespace(nodes=source.nodes,pis=list(source.pis),pos=list(source.pos),
                                         record={**source.record,'seq':12})
                nets[12]=snapshot
                bad={**rows[1],'snapshot':12}
                if change=='representation':snapshot.record['representation']='aig'
                elif change=='ports':snapshot.pis.reverse()
                elif change=='command':snapshot.record['command_id']=2
                elif change=='kind':snapshot.record['kind']='network'
                elif change=='sequence':bad['seq']=rows[-1]['seq']+1
                else:bad['old']=9999
                rows.insert(2,bad)
                with tempfile.TemporaryDirectory() as temporary,self.assertRaises(TraceError):
                    export(rows,nets,['a','b'],Path(temporary))

    def test_distinct_immutable_versions_are_compared_once_and_changes_still_fail(self):
        rows,nets=data();source=nets[8];source.nodes=SnapshotMap(source.nodes)
        separate=SnapshotMap(deepcopy(dict(source.nodes)))
        for i in (12,13):
            nets[i]=SimpleNamespace(nodes=separate,pis=list(source.pis),pos=list(source.pos),
                                    record={**source.record,'seq':i})
        rows[1:2]=[{**rows[1],'snapshot':i} for i in (12,13)]
        stats={}
        with tempfile.TemporaryDirectory() as temporary:
            export(rows,nets,['a','b'],Path(temporary),statistics=stats)
            self.assertEqual(stats['shape_builds'],2)
            self.assertEqual(stats['shape_cache_hits'],1)
            changed=separate.updated({5:{**separate[5],'fanins':[6,8]}})
            nets[14]=SimpleNamespace(nodes=changed,pis=list(source.pis),pos=list(source.pos),
                                    record={**source.record,'seq':14})
            rows.insert(-1,{**rows[1],'snapshot':14})
            with self.assertRaisesRegex(TraceError,'wrong source snapshot'):
                export(rows,nets,['a','b'],Path(temporary))

    def test_missing_and_ambiguous_copy_evidence_still_fail(self):
        for change in ('missing','duplicate','missing_link','duplicate_cleanup'):
            rows,nets=data()
            if change=='missing':rows.pop()
            elif change=='duplicate':rows.append(deepcopy(rows[-1]))
            elif change=='missing_link':rows[-1]['links']=[r for r in rows[-1]['links'] if r[0]!=5]
            else:
                cleanup={'reason':'fraig_cleanup_copy','kind':'history_copy','before':9,'after':11}
                rows.extend([cleanup,dict(cleanup)])
            with tempfile.TemporaryDirectory() as temporary,self.assertRaises(TraceError):
                export(rows,nets,['a','b'],Path(temporary))

    def test_adopted_solver_fact_becomes_shared_function_without_new_sat(self):
        rows,nets=data()
        with tempfile.TemporaryDirectory() as temporary:
            out=Path(temporary);descriptor=export(rows,nets,['a','b'],out)[0]
            graph=Graph((out/descriptor['graph']).read_text());engine=Partitioner(graph,graph)
            a=engine.a['__checkpoint_5'];b=engine.a['a']^1
            self.assertNotEqual(engine.classes.find(a),engine.classes.find(b))
            with patch.object(Partitioner,'prove',side_effect=AssertionError('redundant SAT')):
                install(engine,graph,json.loads((out/descriptor['events']).read_text()),descriptor)
            self.assertEqual(engine.classes.find(a),engine.classes.find(b))
            self.assertEqual(engine.proofs[-1]['events'],1)
            self.assertFalse(engine.proofs[-1]['independently_resolved_again'])

    def test_unadopted_polarity_and_wrong_snapshot_scope_are_rejected(self):
        for change in ('polarity','command','snapshot'):
            rows,nets=data()
            if change=='polarity':rows[1]['new_literal']^=1
            elif change=='command':rows[0]['argv']=['&dch']
            else:nets[8].record['command_id']=2
            with tempfile.TemporaryDirectory() as temporary,self.assertRaises(TraceError):
                export(rows,nets,['a','b'],Path(temporary))

    def test_corrupted_fact_function_cannot_pass_only_by_matching_copy_ids(self):
        rows,nets=data();nets[8].nodes[5]['fanins']=[6,8]
        with tempfile.TemporaryDirectory() as temporary:
            out=Path(temporary);d=export(rows,nets,['a','b'],out)[0]
            graph=Graph((out/d['graph']).read_text());engine=Partitioner(graph,graph)
            with self.assertRaisesRegex(TraceError,'contradicts'):
                install(engine,graph,json.loads((out/d['events']).read_text()),d)
