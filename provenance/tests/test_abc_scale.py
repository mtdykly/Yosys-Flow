"""Large real ABC regions must not pollute retained small statement cuts."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.abc_cases import cases, expectation, expected_boundary
from yosys_flow.abc_internal import cut_domain
from yosys_flow.boolean import ProofBudgetExceeded
from yosys_flow.cli import parser
from yosys_flow.common import TraceError, unescape
from yosys_flow.extract import extract
from yosys_flow.runner import run, load_run, query, query_loaded


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built; run make test')
class AbcScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='abc-scale-')
        cls.base = Path(cls.temp.name)
        cls.fixtures = {}
        for case in cases([12,16,32]):
            if not case['name'].startswith('scale_'): continue
            source = cls.base/(case['name']+'.v')
            source.write_text(case['rtl'])
            out = cls.base/case['name']
            result = run(parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out),
                                              '--native-yosys',str(NATIVE)]))
            if not all(result['checks'].values()): raise AssertionError(result)
            cls.fixtures[case['declared_input_count']] = (case,source,out)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def args(self, size, line=2):
        _,source,out = self.fixtures[size]
        return parser().parse_args(['query',str(out),'--source',str(source),'--line',str(line)])

    def test_exact_selection_origins_and_export_for_real_large_regions(self):
        for size,(case,source,out) in self.fixtures.items():
            with self.subTest(inputs=size):
                manifest,trace,validation = load_run(out)
                self.assertEqual(manifest['configuration']['passes'], ['synth -top demo'])
                self.assertTrue(validation['checks']['stock_flow_exact_match'])
                internal = trace.abc_internal[0]
                self.assertEqual(internal['region_input_count'],size)
                self.assertEqual(internal['function_proof']['backend'],'robdd')
                self.assertIsNone(internal['fallback_reason'])
                self.assertEqual(internal['verified_equivalences'],sum(internal['equivalence_events'].values()))
                self.assertEqual(internal['verified_aig_replacements'],internal['aig_replacements'])
                self.assertEqual(internal['verified_history_copies'],internal['history_copies'])
                self.assertEqual(internal['verified_entry_copies'],4)
                self.assertFalse(internal['local_proof_failures'])
                if size==32: self.assertTrue(internal['local_function_resources'])
                module = json.loads((out/'after.json').read_text())['modules']['demo']
                for spec in case['queries']:
                    selected = query_loaded(self.args(size,spec['line']), manifest, trace)
                    names = {unescape(c['name']) for c in selected['final_cells']}
                    self.assertEqual(names, expectation(case,spec,module))
                    self.assertFalse(selected['candidate_cell_refs'])
                    self.assertEqual({s['line'] for c in selected['final_cells'] for s in c['all_origins']},set(spec['origin_lines']))
                target = self.base/f'small-{size}.v'
                exported = extract(parser().parse_args(['extract',str(out),'--source',str(source),'--line','3','--out',str(target)]))
                self.assertTrue(all(exported['validation'].values()))
                self.assertEqual(exported['selected_cell_count'],1)
                provenance = json.loads(target.with_suffix('.provenance.json').read_text())
                expected = expectation(case,case['queries'][1],module)
                inputs,outputs = expected_boundary(module,expected,case['queries'][1])
                self.assertEqual({b['original_bit'] for b in provenance['boundary'] if b['direction']=='input'},inputs)
                self.assertEqual({b['original_bit'] for b in provenance['boundary'] if b['direction']=='output'},outputs)

    def test_large_run_can_be_requeried_offline_without_synthesis(self):
        with patch.dict('os.environ',{'PATH':''}), \
             patch('subprocess.run',side_effect=AssertionError('query must read records only')), \
             patch('subprocess.Popen',side_effect=AssertionError('query must read records only')):
            selected = query(self.args(32))
        self.assertEqual(len(selected['final_cells']),1)
        self.assertEqual(len(selected['cut_proven_refs']),1)
        self.assertFalse(selected['safe_for_incremental_reuse'])

    def test_region_resource_exhaustion_is_explicit_unknown(self):
        for setting,limit,reason in [('MAX_BDD_NODES',5,'bdd_node_limit_exceeded'),
                                      ('MAX_BDD_WORK',0,'bdd_work_limit_exceeded')]:
            with self.subTest(setting=setting), patch('yosys_flow.boolean.'+setting,limit):
                selected = query(self.args(32))
                internal = selected['abc_internal'][0]
                self.assertEqual(internal['fallback_reason'],reason)
                self.assertEqual(internal['function_proof']['status'],'incomplete')
                self.assertFalse(selected['cut_proven_refs'])
                self.assertFalse(selected['joint_cut_proven_refs'])
                self.assertTrue(selected['candidate_cell_refs'])

    def test_local_budget_failure_keeps_other_proofs_and_reports_candidate(self):
        def limited(count, **kwargs):
            if count>10: raise ProofBudgetExceeded('bdd_work_limit_exceeded')
            return cut_domain(count, **kwargs)
        with patch('yosys_flow.abc_joint.cut_domain',side_effect=limited):
            selected = query(self.args(32))
        internal = selected['abc_internal'][0]
        self.assertEqual(internal['function_proof']['status'],'verified')
        self.assertIsNone(internal['fallback_reason'])
        self.assertTrue(internal['local_proof_failures'])
        self.assertGreater(internal['proved_import_nodes'],0)
        self.assertTrue(selected['cut_proven_refs'])
        self.assertTrue(selected['candidate_cell_refs'])

    def test_corrupted_large_stage_polarity_copy_and_pi_binding_are_rejected(self):
        _,_,out = self.fixtures[32]
        _,trace,_ = load_run(out)
        filename = trace.regions[0]['artifacts']['input.blif.flow.jsonl']
        original = [json.loads(line) for line in (out/filename).read_text().splitlines()]
        mutations = []
        rows = copy.deepcopy(original)
        stage = next(r for r in rows if r['kind']=='network' and r['command_id']>3)
        next(n for n in stage['nodes'] if n['id']==stage['pos'][0])['fanins'][0] ^= 1
        mutations.append(rows)
        rows = copy.deepcopy(original)
        next(r for r in rows if r['kind']=='proved_equivalence')['new_literal'] ^= 1
        mutations.append(rows)
        rows = copy.deepcopy(original)
        copying = next(r for r in rows if r['kind']=='history_copy')
        # Two real source PIs copied to the wrong destinations: cardinalities
        # and object existence remain valid, function checks must still reject.
        before = next(r for r in rows if r['seq']==copying['before'])
        links = [link for link in copying['links'] if link[0] in before['pis']]
        links[0][1],links[-1][1] = links[-1][1],links[0][1]
        mutations.append(rows)
        rows = copy.deepcopy(original)
        stage = next(r for r in rows if r['kind']=='network' and r['command_id']>3)
        stage['pi_names'][0],stage['pi_names'][-1] = stage['pi_names'][-1],stage['pi_names'][0]
        mutations.append(rows)
        for i,rows in enumerate(mutations):
            with self.subTest(mutation=i):
                target = self.base/f'corrupt-{i}'
                shutil.copytree(out,target)
                (target/filename).write_text(''.join(json.dumps(r)+'\n' for r in rows))
                with self.assertRaises(TraceError): trace.compare_abc_artifacts(target)
