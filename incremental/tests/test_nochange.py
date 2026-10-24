"""Raw ANUBIS NOC inventory, proof outcomes and honest timing populations."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from incremental import equivalence, nochange
from incremental.__main__ import parser
from yosys_flow.common import PIN, TraceError


class InventoryTests(unittest.TestCase):
    def test_all_macros_and_missing_script_targets_are_accounted_for(self):
        report = nochange.inventory()
        self.assertEqual(report['unique_macro_count'], 30)
        self.assertEqual(report['counts_by_design'], {'alpha':6,'dlx':4,'fpu':7,'mor1kx':7,'or1200':6})
        self.assertEqual(report['upstream_script_targets_without_macro'],
                         ['alpha_noc6','dlx_noc4','fpu_noc7','or1200_noc6','or1200_noc7'])
        self.assertEqual(report['macros_not_enumerated_by_script'], [])
        self.assertEqual(sum(c['execution'] == 'leaf' for c in report['cases']), 11)
        self.assertEqual(sum(len(c['parameters']) for c in report['cases'] if c['execution'] == 'leaf'), 15)

    def test_corrupted_source_and_incomplete_catalog_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'corpus'
            shutil.copytree(nochange.CORPUS, root)
            path = root/'upstream/fpu/rtl/fp_shift0.v'
            original = path.read_bytes()
            path.write_bytes(original+b'\n')
            with self.assertRaisesRegex(TraceError, 'checksum'):
                nochange.inventory(root)
            path.write_bytes(original)
            path = root/'nochange-cases.json'
            catalog = json.loads(path.read_text())
            catalog['cases'].pop()
            path.write_text(json.dumps(catalog))
            with self.assertRaisesRegex(TraceError, 'every macro'):
                nochange.inventory(root)

    def test_inventory_only_needs_no_tool_and_keeps_production_budgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = parser().parse_args(['anubis-nochange','--out',str(Path(tmp)/'out'),
                                        '--inventory-only','--yosys','nonexistent-yosys'])
            result = nochange.run(args)
            self.assertEqual(result['status'], 'inventory_only')
            self.assertEqual(result['per_proof_budget_seconds'], 600)
            self.assertIsNone(result['shared_proof_budget_seconds'])
            args = parser().parse_args(['auto','--base-run','old','--new-source','new.v','--out','out'])
            self.assertEqual((args.equivalence_timeout,args.equivalence_budget), (5,15))

    def test_preprocessor_comparison_ignores_comments_but_not_literals(self):
        self.assertEqual(nochange.verilog_tokens('assign x = 1; // text\n'),
                         nochange.verilog_tokens('assign x=1; /* other */'))
        self.assertNotEqual(nochange.verilog_tokens('assign x="//";'),
                            nochange.verilog_tokens('assign x="/* */";'))


@unittest.skipUnless(shutil.which('yosys'), 'pinned Yosys required')
class NochangeProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='anubis-nochange-')
        cls.root = Path(os.environ.get('YOSYS_FLOW_NOCHANGE_RESULTS', cls.temp.name)).resolve()
        cls.out = cls.root/'survey'
        cls.report = nochange.run(parser().parse_args(['anubis-nochange','--out',str(cls.out),'--repeats','3']))
        cls.rows = {(r['case'],r.get('profile')):r for r in cls.report['profiles']}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_every_macro_is_reported_without_claiming_thirty_successes(self):
        self.assertEqual(self.report['cases_accounted_for'], 30)
        self.assertEqual(self.report['counts'], {'equivalent':4,'different':5,'structural_same':3,
                                               'unknown':1,'out_of_scope':21})
        self.assertEqual(self.report['status'], 'completed_with_findings')
        self.assertEqual(len(self.report['profiles']), 34)
        self.assertTrue(self.report['inputs_unchanged'])
        self.assertEqual(self.report['adaptations'], [])
        self.assertIn(PIN, self.report['yosys']['version'])

    def test_four_raw_equivalent_edits_repeat_without_synthesis(self):
        for name in ('alpha_noc3','dlx_noc0','dlx_noc1','mor1kx_noc3'):
            row = self.rows[name,0]
            self.assertEqual(row['status'], 'equivalent')
            self.assertFalse(row['frontend_ir_equal'])
            self.assertEqual(len(row['samples']), 3)
            for sample in row['samples']:
                self.assertFalse(sample['full_synthesis_used'])
                self.assertGreater(sample['timings']['prepare_miter_seconds'], 0)
                self.assertGreater(sample['timings']['sat_command_seconds'], 0)
                self.assertIn('sat -enable_undef', (Path(sample['directory'])/'prove.ys').read_text())
        for script in self.out.rglob('*.ys'):
            if script.is_relative_to(self.out/'inputs'):
                # Locked upstream reference scripts are archived, not executed
                # by this isolated-module proof experiment.
                continue
            self.assertFalse(any(line.split()[0] in {'synth','abc','techmap','flatten'}
                                 for line in script.read_text().splitlines() if line.strip()), script)

    def test_fpu_noc_label_is_not_assumed_equivalent(self):
        for name,index in (('fpu_noc0',0),('fpu_noc1',0),('fpu_noc2',0),('fpu_noc2',1),('fpu_noc6',0)):
            row = self.rows[name,index]
            self.assertEqual(row['status'], 'different')
            sample = row['samples'][0]
            signals = {s['name']:s for s in json.loads(Path(sample['counterexample']).read_text())['signal']}
            self.assertEqual(signals['trigger']['wave'][0], '1')
            self.assertEqual(signals[equivalence.UNDEFINED]['wave'][0], '0')
        # The edited normalized output is the complement, for this concrete
        # solver-selected input. Do not require a particular SAT model.
        row = self.rows['fpu_noc0',0]
        signals = {s['name']:s for s in json.loads(Path(row['samples'][0]['counterexample']).read_text())['signal']}
        gold = signals['gold_normalized']['data'][0]
        gate = signals['gate_normalized']['data'][0]
        self.assertEqual(len(gold), 64)
        self.assertEqual(gate, ''.join('1' if bit == '0' else '0' for bit in gold))

    def test_inactive_parameters_and_inferred_latches_do_not_inflate_SAT_coverage(self):
        for name,index in (('fpu_noc0',1),('fpu_noc3',0),('fpu_noc4',0)):
            row = self.rows[name,index]
            self.assertEqual(row['status'], 'structural_same')
            self.assertFalse(row['preprocessed_tokens_equal'])
            self.assertTrue(row['frontend_ir_equal'])
            self.assertEqual(row['samples'], [])
        for name in ('fpu_noc3','fpu_noc4'):
            row = self.rows[name,1]
            self.assertEqual(row['status'], 'out_of_scope')
            self.assertIn('$dlatch', row['detail'])

    def test_original_X_default_is_not_silently_adapted_or_called_timeout(self):
        row = self.rows['dlx_noc2',0]
        self.assertEqual(row['status'], 'unknown')
        self.assertEqual(row['reason'], 'undefined_output_or_unrecognized_counterexample')
        signals = {s['name']:s for s in json.loads(Path(row['samples'][0]['counterexample']).read_text())['signal']}
        self.assertEqual(signals[equivalence.UNDEFINED]['wave'][0], '1')
        self.assertEqual(signals['gold_Result']['wave'][0], 'x')
        self.assertEqual(signals['gate_Result']['wave'][0], 'x')

    def test_budget_population_excludes_structural_same_unknown_and_out_of_scope(self):
        result = self.report['budget_observations']
        self.assertEqual(result['eligible_count'], 4)
        self.assertEqual(set(result['eligible_profiles']),
                         {n+'/profile-0' for n in ('alpha_noc3','dlx_noc0','dlx_noc1','mor1kx_noc3')})
        self.assertFalse(result['default_values_changed'])
        self.assertEqual(result['coverage'][-1]['covered'], 4)

    def test_exhausted_experiment_budget_is_unknown_and_other_cases_still_report(self):
        result = nochange.run(parser().parse_args(['anubis-nochange','--out',str(self.root/'timeout'),
            '--cases','dlx_noc1','dlx_noc3','--case-timeout','0.000000001','--repeats','1']))
        self.assertEqual(result['cases_accounted_for'], 2)
        self.assertEqual(result['counts'], {'unknown':1,'out_of_scope':1})
        self.assertEqual(result['profiles'][0]['reason'], 'wall_timeout')

    def test_input_tampering_during_proof_prevents_completed_report(self):
        original = nochange.measure
        def corrupt(*args, **kwargs):
            row = original(*args, **kwargs)
            path = args[5]/'dlx/bypass_ex.v'
            path.write_text(path.read_text()+'\n')
            return row
        out = self.root/'tampered'
        with patch.object(nochange, 'measure', side_effect=corrupt):
            result = nochange.run(parser().parse_args(['anubis-nochange','--out',str(out),
                                                       '--cases','dlx_noc1','--repeats','1']))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('inputs changed', result['failure'])
        self.assertEqual(json.loads((out/'report.json').read_text())['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
