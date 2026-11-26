import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import TraceError
from yosys_flow.runner import run, load_run
from yosys_flow.contracts import ContractContext


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built')
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        cls.source=cls.root/'old.v'
        cls.source.write_text('module top(input a,b,c,d,e,output y,z);\nwire t;\nassign t=a&b;\nassign y=t^c;\nassign z=d|e;\nendmodule\n')
        cls.old=cls.root/'old'
        from yosys_flow.abc_internal import read_evidence
        with patch('yosys_flow.abc_internal.read_evidence',wraps=read_evidence) as reads, \
             patch('yosys_flow.abc_checkpoints.read_evidence',side_effect=AssertionError('raw ABC parsed twice')), \
             patch('yosys_flow.contracts.load_run',side_effect=AssertionError('validated archive reloaded')):
            cls.report=run(parser().parse_args(['run','--source',str(cls.source),'--top','top',
                '--out',str(cls.old),'--native-yosys',str(NATIVE),'--prepare-evidence']))
        cls.reads=reads.call_count

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def test_archive_and_proof_share_one_replay_and_validated_context(self):
        self.assertEqual(self.report['status'],'complete')
        proof=self.report['proof_evidence']
        self.assertEqual(proof['status'],'ready')
        self.assertTrue(proof['validated_context_reused'])
        self.assertEqual(proof['decoded_abc_reused'],self.reads)
        self.assertEqual(self.reads,1)
        self.assertFalse(proof['source_representation']['new_rtl_used'])
        manifest=json.loads((self.old/'manifest.json').read_text())
        times=manifest['timings_seconds']
        self.assertAlmostEqual(times['record_validation'],times['proof_witness_capture']+
                               times['record_validation_excluding_proof_witnesses'])
        self.assertGreater(times['verified_index_build'],0)
        self.assertEqual(proof['typed_evidence']['per_yosys_object_sat_calls'],0)
        self.assertGreater(proof['typed_evidence']['counts']['source_association'],0)
        self.assertGreater(proof['typed_evidence']['counts']['function_equivalence'],0)

    def test_integrated_and_standalone_preparation_have_same_function_cuts(self):
        from yosys_flow.prepare_evidence import prepare
        from yosys_flow.proof_store import Reader,StoredPartitioner
        out=self.root/'standalone'
        prepare(parser().parse_args(['prepare-evidence',str(self.old),'--out',str(out)]))
        context=ContractContext(self.old,proof_evidence=False)
        a=Reader(context,Path(str(self.old)+'.proofs'));b=Reader(context,out)
        try:
            for inv in a.document['invocations']:
                x,y=StoredPartitioner(a,inv),StoredPartitioner(b,inv)
                for output in x.first.pos:self.assertEqual(x.region([output]),y.region([output]))
                proofs=json.loads(a.db.execute('SELECT evidence FROM invocations WHERE id=?',(inv,)).fetchone()[0])['proofs']
                self.assertTrue(any(p['proof']=='verified_replay_function_equations' and p['count']>0 for p in proofs))
        finally:a.close();b.close()

    def test_reindex_reuses_completed_capture_without_executing_synthesis(self):
        from yosys_flow.reindex import reindex
        from yosys_flow.common import digest
        before=digest(self.old/'manifest.json');out=self.root/'reindexed'
        with patch('yosys_flow.native.run_native',side_effect=AssertionError('synthesis rerun')), \
             patch('yosys_flow.abc_checkpoints.read_evidence',side_effect=AssertionError('second ABC parse')), \
             patch('yosys_flow.contracts.load_run',side_effect=AssertionError('second archive load')):
            result=reindex(parser().parse_args(['reindex',str(self.old),'--out',str(out),'--prepare-evidence']))
        self.assertEqual(result['status'],'complete')
        self.assertFalse(result['instrumented_synthesis_executed'])
        self.assertFalse(result['stock_synthesis_executed'])
        self.assertTrue(result['saved_stock_reference_rechecked'])
        self.assertTrue(result['proof_evidence']['validated_context_reused'])
        self.assertEqual(digest(self.old/'manifest.json'),before)
        _,_,validation=load_run(out)
        self.assertTrue(validation['valid'])

    def test_reindex_rejects_damaged_raw_evidence(self):
        from yosys_flow.reindex import reindex
        damaged=self.root/'raw-damaged';shutil.copytree(self.old,damaged)
        (damaged/'after.json').write_text('{}')
        with self.assertRaisesRegex(TraceError,'artifact mismatch'):
            reindex(parser().parse_args(['reindex',str(damaged),'--out',str(self.root/'reindex-refused')]))

    def test_bounded_online_proof_reports_gap_without_whole_design_solver(self):
        from incremental.__main__ import parser as auto_parser
        from incremental.automatic import run as automatic
        from yosys_flow.abc_partition import Partitioner
        library=self.root/'bounded-library';shutil.copytree(str(self.old)+'.proofs',library)
        for path in library.glob('regions*.sqlite3'):path.unlink()
        new=self.root/'bounded.v';new.write_text(self.source.read_text().replace('a&b','a|b'))
        with patch.object(Partitioner,'global_cec',side_effect=AssertionError('online global CEC')), \
             patch('yosys_flow.source_regions.prove_design',side_effect=AssertionError('oversized source SAT')):
            result=automatic(auto_parser().parse_args(['auto','--base-run',str(self.old),'--proof-evidence',str(library),
                '--new-source',str(new),'--native-yosys',str(NATIVE),'--no-equivalence-precheck',
                '--region-node-limit','1','--out',str(self.root/'bounded')]))
        self.assertEqual(result['status'],'blocked',result.get('failure'))
        self.assertEqual(result['failure']['code'],'old_region_node_limit')
        self.assertEqual(result['failure']['detail']['missing_evidence_stage'],'abc_independent_output_boundary')
        self.assertFalse(result['published_incremental_netlist'])

    def test_unknown_edit_uses_offline_source_but_still_proves_local_outputs(self):
        from incremental.__main__ import parser as auto_parser
        from incremental.automatic import run as automatic
        from yosys_flow.source_regions import command
        new=self.root/'new.v';new.write_text(self.source.read_text().replace('a&b','a|b'))
        def no_lowering(binary,directory,script):
            self.assertNotIn('; proc;',script)
            return command(binary,directory,script)
        with patch('yosys_flow.source_regions.command',side_effect=no_lowering) as calls:
            result=automatic(auto_parser().parse_args(['auto','--base-run',str(self.old),'--new-source',str(new),
                '--native-yosys',str(NATIVE),'--no-equivalence-precheck','--out',str(self.root/'edit')]))
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertGreater(calls.call_count,0)
        metrics=result['proof_evidence']['metrics']
        self.assertEqual(metrics['initial_region_cache_entries'],0)
        self.assertEqual(metrics['source_representation']['lowering_reused'],1)
        self.assertGreater(result['proof_timings']['old_source_miter_and_sat_seconds'],0)
        self.assertGreater(result['splice']['reused_cells'],0)
        self.assertAlmostEqual(sum(metrics['phase_seconds'].values()),metrics['region_preparation_seconds'],delta=0.02)
        with patch('yosys_flow.source_regions.prove_design',side_effect=AssertionError('cached source proof repeated')):
            again=automatic(auto_parser().parse_args(['auto','--base-run',str(self.old),'--new-source',str(new),
                '--native-yosys',str(NATIVE),'--no-equivalence-precheck','--out',str(self.root/'edit-repeat')]))
        self.assertEqual(again['status'],'passed',again.get('failure'))
        self.assertGreater(again['proof_evidence']['metrics']['initial_region_cache_entries'],0)
        self.assertGreater(again['proof_evidence']['metrics']['cache_hits'],0)
        self.assertEqual(again['timings']['required_old_region_proof_seconds'],0)

    def test_source_database_tampering_is_rejected(self):
        from yosys_flow.proof_store import Reader
        target=self.root/'damaged';shutil.copytree(str(self.old)+'.proofs',target)
        manifest=json.loads((target/'source/manifest.json').read_text())
        with sqlite3.connect(target/'source'/manifest['database']) as db:
            db.execute("UPDATE cells SET payload='{}'")
        context=ContractContext(self.old,compact=True,proof_evidence=False)
        with self.assertRaisesRegex(TraceError,'source representation hash mismatch'):Reader(context,target)

    def test_final_revalidation_keeps_explicit_library_when_default_is_stale(self):
        from incremental.__main__ import parser as auto_parser
        from incremental.automatic import run as automatic
        from yosys_flow.proof_store import signature
        base=self.root/'explicit-base';shutil.copytree(self.old,base)
        library=self.root/'explicit-library';shutil.copytree(str(self.old)+'.proofs',library)
        for path in library.glob('regions*.sqlite3'):path.unlink()
        default=Path(str(base)+'.proofs');shutil.copytree(library,default)
        path=default/'manifest.json';document=json.loads(path.read_text());document.pop('authentication')
        document['implementation']={}
        path.write_text(json.dumps({**document,'authentication':signature(document)}))
        new=self.root/'explicit-new.v';new.write_text(self.source.read_text().replace('a&b','a|b'))
        with self.assertRaisesRegex(TraceError,'implementation changed'):ContractContext(base)
        result=automatic(auto_parser().parse_args(['auto','--base-run',str(base),'--proof-evidence',str(library),
            '--new-source',str(new),'--native-yosys',str(NATIVE),'--no-equivalence-precheck',
            '--out',str(self.root/'explicit-edit')]))
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertTrue(result['checks']['baseline_unchanged'])
        self.assertTrue((self.root/'explicit-edit/incremental.v').is_file())

    def test_final_revalidation_still_rejects_changed_selected_library(self):
        from incremental.__main__ import parser as auto_parser
        from incremental.automatic import run as automatic
        from incremental import replace
        library=self.root/'changed-library';shutil.copytree(str(self.old)+'.proofs',library)
        for path in library.glob('regions*.sqlite3'):path.unlink()
        new=self.root/'changed-new.v';new.write_text(self.source.read_text().replace('a&b','a|b'))
        audit=replace._revalidate_baseline
        def changed(context,contract):
            selected=context.proof_evidence
            with (selected.directory/selected.document['database']).open('ab') as stream:
                stream.write(b'changed after candidate verification')
            return audit(context,contract)
        out=self.root/'changed-edit'
        with patch('incremental.replace._revalidate_baseline',side_effect=changed):
            result=automatic(auto_parser().parse_args(['auto','--base-run',str(self.old),
                '--proof-evidence',str(library),'--new-source',str(new),'--native-yosys',str(NATIVE),
                '--no-equivalence-precheck','--out',str(out)]))
        self.assertEqual(result['status'],'failed')
        self.assertIn('proof database hash mismatch',result['failure']['detail'])
        self.assertFalse((out/'incremental.v').exists())
        self.assertFalse((out/'replacement/incremental.v').exists())

    def test_replay_capture_resume_uses_same_handoff_without_traced_synthesis(self):
        from yosys_flow.capture import resume
        out=self.root/'resumed'
        result=resume(parser().parse_args(['resume',str(self.old),'--out',str(out),'--prepare-evidence']))
        self.assertEqual(result['status'],'complete')
        self.assertTrue(json.loads((out/'manifest.json').read_text())['resume']['reused_capture'])
        self.assertTrue(result['proof_evidence']['validated_context_reused'])
        self.assertEqual(result['proof_evidence']['decoded_abc_reused'],1)

    def test_staged_witness_tamper_does_not_invalidate_completed_archive(self):
        from yosys_flow.proof_pipeline import EvidencePipeline
        original=EvidencePipeline.adopt
        def damaged(builder,context,destination,store):
            first=next(iter(builder.entries.values()))
            (first['directory']/'checkpoints.json').write_text('{}')
            return original(builder,context,destination,store)
        out=self.root/'handoff-tamper'
        with patch.object(EvidencePipeline,'adopt',damaged):
            with self.assertRaisesRegex(TraceError,'staged witness changed'):
                run(parser().parse_args(['run','--source',str(self.source),'--top','top','--out',str(out),
                    '--native-yosys',str(NATIVE),'--prepare-evidence']))
        self.assertEqual(json.loads((out/'manifest.json').read_text())['status'],'complete')
        self.assertEqual(load_run(out)[0]['status'],'complete')

    def test_no_abc_archive_remains_complete(self):
        source=self.root/'direct.v';source.write_text('module top(input a,output y); assign y=a; endmodule\n')
        result=run(parser().parse_args(['run','--source',str(source),'--top','top','--out',str(self.root/'direct'),
            '--native-yosys',str(NATIVE),'--prepare-evidence']))
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['proof_evidence']['status'],'not_applicable')

    def test_indexed_source_cut_matches_retained_and_flattened_instances(self):
        from yosys_flow.extract import slice_module
        from yosys_flow.source_regions import archived,make_cut,prove_design
        source=self.root/'hier.v'
        source.write_text('module leaf(input a,b,output y); assign y=a&b; endmodule\n'
                          'module top(input a,b,c,d,output y,z); leaf u0(a,b,y); leaf u1(c,d,z); endmodule\n')
        for flatten in (False,True):
            with self.subTest(flatten=flatten):
                out=self.root/('hier-flat' if flatten else 'hier-retained')
                run(parser().parse_args(['run','--source',str(source),'--top','top','--out',str(out),
                    '--native-yosys',str(NATIVE),'--prepare-evidence',*(['--flatten'] if flatten else [])]))
                context=ContractContext(out,compact=True);self.addCleanup(context.proof_evidence.close)
                plain=ContractContext(out,compact=True,proof_evidence=False)
                bit=context.module['ports']['y']['bits'][0]
                names=[n for n,c in context.module['cells'].items() if bit in c['connections']['Y']]
                _,boundary=slice_module(context.module,names,'local',[])
                document={'boundary':boundary,'region':{'connections':[],
                    'recorded_cell_functions':{n:context.module['cells'][n] for n in names}}}
                with tempfile.TemporaryDirectory() as tmp:
                    binary=context.manifest['tool']['stock_executable'];directory=Path(tmp)
                    reference=make_cut(plain,document,archived(plain,directory,binary))
                    actual=make_cut(context,document,archived(context,directory,binary))
                    self.assertEqual(actual,reference)
                    prove_design(binary,directory,actual[0])


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built')
class IndexedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        source=cls.root/'wide.v';source.write_text('module top(input [255:0] a,b,output [255:0] y); assign y=a&b; endmodule\n')
        cls.old=cls.root/'old'
        run(parser().parse_args(['run','--source',str(source),'--top','top','--out',str(cls.old),
            '--native-yosys',str(NATIVE),'--prepare-evidence']))

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def test_local_indexed_cut_matches_full_source_without_scanning_all_cells(self):
        from yosys_flow.extract import slice_module
        from yosys_flow.source_regions import archived,make_cut,prove_design
        indexed=ContractContext(self.old,compact=True)
        ordinary=ContractContext(self.old,compact=True,proof_evidence=False)
        self.addCleanup(indexed.proof_evidence.close)
        bit=indexed.module['ports']['y']['bits'][17]
        names=[n for n,c in indexed.module['cells'].items() if bit in c['connections']['Y']]
        self.assertEqual(len(names),1)
        _,boundary=slice_module(indexed.module,names,'local',[])
        document={'boundary':boundary,'region':{'connections':[],
            'recorded_cell_functions':{n:indexed.module['cells'][n] for n in names}}}
        binary=indexed.manifest['tool']['stock_executable']
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            reference=make_cut(ordinary,document,archived(ordinary,directory,binary))
            actual=make_cut(indexed,document,archived(indexed,directory,binary))
            self.assertEqual(actual,reference)
            prove_design(binary,directory,actual[0])
            # Same boundary and shape, wrong function: representation reuse
            # cannot authorize the cut without its independent SAT check.
            for cell in actual[0]['modules']['gate']['cells'].values():cell['type']='$_OR_'
            with self.assertRaisesRegex(TraceError,'did not prove'):prove_design(binary,directory,actual[0])
        metrics=indexed.proof_evidence.source.metrics
        self.assertLess(metrics['cells_read'],10)
        self.assertLess(metrics['driver_rows_read'],20)
        self.assertLess(metrics['alias_rows_read'],20)
