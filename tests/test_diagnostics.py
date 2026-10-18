"""Diagnose existing records without synthesizing; never label the cut safe."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.cli import main, parser
from yosys_flow.common import ROOT, TraceError, digest
from yosys_flow.diagnostics import diagnose, audit_cut
from yosys_flow.runner import run, load_run
from tools.benchmark_abc import sample, summarize
from yosys_flow.abc_cases import cases


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built; run make test')
class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='abc-diagnostics-')
        cls.base=Path(cls.temp.name)
        cls.source=cls.base/'factoring.v'
        cls.source.write_text((ROOT/'examples/factoring.v').read_text())
        cls.out=cls.base/'run'
        run(parser().parse_args(['run','--source',str(cls.source),'--top','demo','--out',str(cls.out),
                                 '--native-yosys',str(NATIVE)]))

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def args(self):
        return parser().parse_args(['diagnose',str(self.out),'--source',str(self.source),'--line','2'])

    def test_reads_records_without_external_tools_or_mutating_run(self):
        before={p.name:digest(p) for p in self.out.iterdir() if p.is_file()}
        with patch('subprocess.run',side_effect=AssertionError('diagnosis must not execute tools')), \
             patch('subprocess.Popen',side_effect=AssertionError('diagnosis must not execute tools')):
            report=diagnose(self.args())
        self.assertEqual(before,{p.name:digest(p) for p in self.out.iterdir() if p.is_file()})
        self.assertEqual(report['synthesis_invocations'],0)
        self.assertEqual(len(report['selected_cells']),2)
        self.assertTrue(report['electrical_cut']['selected_cell_cut_complete'])
        self.assertTrue(report['recorded_group_closure']['complete_for_recorded_groups'])
        users=report['electrical_cut']['output_obligations'][0]['external_users']
        self.assertEqual({e['name'] for e in users},{'y','z'})
        self.assertEqual({s['line'] for s in report['other_recorded_sources']},{3})
        self.assertFalse(report['safe_for_incremental_reuse'])
        self.assertFalse(report['end_to_end_exact'])
        self.assertFalse(report['electrical_cut']['source_completeness_proven'])
        self.assertEqual(report['replacement_assessment']['status'],'not_proven')
        self.assertEqual(report['inventory']['abc_regions'][0]['input_count'],5)
        self.assertGreater(report['inventory']['abc_trace_bytes'],0)
        for key in ('artifact_hash_check','record_replay_and_netlist_check','abc_artifact_check_and_proof','query_from_loaded_records'):
            self.assertGreaterEqual(report['timings_seconds'][key],0)

    def test_object_query_detects_missing_group_member_without_expansion(self):
        all_report=diagnose(self.args())
        ref=all_report['selected_cells'][0]['ref']
        args=parser().parse_args(['diagnose',str(self.out),'--object',ref])
        report=diagnose(args)
        self.assertEqual(len(report['selected_cells']),1)
        closure=report['recorded_group_closure']
        self.assertFalse(closure['selection_expanded'])
        self.assertFalse(closure['complete_for_recorded_groups'])
        self.assertEqual(set(closure['required_final_cell_refs']),{c['ref'] for c in all_report['selected_cells']})
        self.assertEqual(len(closure['additional_required_cell_refs']),1)
        self.assertIn('recorded_group_not_closed',report['replacement_assessment']['blockers'])

    def test_boundary_audit_rejects_missing_shared_consumer_and_port(self):
        report=diagnose(self.args())
        module=json.loads((self.out/'after.json').read_text())['modules']['demo']
        names=list(module['cells'])
        names=[n for n in names if module['cells'][n]['type']!='$_XOR_']
        boundary=copy.deepcopy(report['boundary'])
        row=next(r for r in boundary if r['direction']=='output')
        row['external_users']=[e for e in row['external_users'] if e['name']!='z']
        result=audit_cut(module,names,boundary)
        self.assertFalse(result['selected_cell_cut_complete'])
        self.assertEqual(result['violations'][0]['reason'],'missing_external_user')
        boundary=[r for r in boundary if r['direction']!='output']
        self.assertIn('missing_output',{v['reason'] for v in audit_cut(module,names,boundary)['violations']})

    def test_corrupt_record_is_rejected_and_changed_source_is_flagged(self):
        import shutil
        target=self.base/'corrupt';shutil.copytree(self.out,target)
        (target/'yosys.jsonl').write_text((target/'yosys.jsonl').read_text()+'\n')
        args=self.args();args.directory=str(target)
        with self.assertRaisesRegex(TraceError,'artifact hash mismatch'): diagnose(args)
        original=self.source.read_text()
        try:
            self.source.write_text(original+'// new version\n')
            report=diagnose(self.args())
            self.assertIn(str(self.source),report['changed_or_missing_base_inputs'])
            self.assertIn('base_source_changed_or_missing',report['replacement_assessment']['blockers'])
        finally: self.source.write_text(original)

    def test_cli_reports_partial_and_does_not_need_yosys(self):
        with patch.dict('os.environ',{'PATH':''}), redirect_stdout(io.StringIO()) as output:
            result=main(['diagnose',str(self.out),'--source',str(self.source),'--line','2'])
        self.assertEqual(result,0)
        report=json.loads(output.getvalue())
        self.assertEqual(report['coverage'],'partial')
        self.assertEqual(report['report_type'],'region_diagnostics')

    def test_scale_sample_requires_exact_selection_and_reports_symbolic_proof(self):
        case=next(c for c in cases([12]) if c['name']=='scale_12')
        report=sample(case,self.base/'scale',str(NATIVE),'yosys')
        self.assertTrue(report['contract_passed'])
        self.assertEqual(report['traced_synthesis_invocations'],1)
        self.assertEqual(report['inventory']['abc_regions'][0]['input_count'],12)
        internal=report['inventory']['abc_regions'][0]['internal']
        self.assertIsNone(internal['fallback_reason'])
        self.assertEqual(internal['function_proof']['backend'],'robdd')
        first=report['queries'][0]
        self.assertTrue(first['exact_cell_selection'])
        self.assertEqual(len(first['expected_cells']),1)
        self.assertFalse(first['extra_cells'])
        self.assertFalse(first['missing_cells'])
        self.assertEqual(first['cut_proven_count'],1)
        aggregate=summarize([report,report])[0]
        self.assertTrue(aggregate['queries'][0]['exact_in_all_samples'])
        self.assertIsNone(aggregate['sample_max_rss_kib_median'])
        self.assertEqual(aggregate['samples'],2)
        manifest=json.loads((self.base/'scale/run/manifest.json').read_text())
        self.assertGreaterEqual(manifest['timings_seconds']['record_validation'],0)


class BoundaryConstantTests(unittest.TestCase):
    def test_equal_constant_values_do_not_imply_shared_output_identity(self):
        module={'cells':{},'ports':{'y':{'direction':'output','bits':['0']},'z':{'direction':'output','bits':['0']}}}
        result=audit_cut(module,[],[{'direction':'output','original_bit':'0','original_lhs':{'wire':'\\y','offset':0}}])
        self.assertFalse(result['output_obligations'])
        self.assertFalse(result['source_completeness_proven'])
