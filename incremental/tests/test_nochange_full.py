import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from incremental import nochange, nochange_full
from incremental.__main__ import parser


class FullInventoryTests(unittest.TestCase):
    def test_all_thirty_macros_and_feature_profiles_without_running_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = parser().parse_args(['anubis-nochange','--with-sequential','--inventory-only',
                                        '--yosys','absent','--eqy','absent','--out',tmp+'/inventory'])
            result = nochange.run(args)
            self.assertEqual(result['status'], 'inventory_only')
            self.assertEqual(result['cases_accounted_for'], 30)
            self.assertEqual(len(result['profiles']), 35)
            self.assertIsNone(result['shared_proof_budget_seconds'])
            self.assertEqual(result['per_proof_budget_seconds'], 600)
            power = [r for r in result['profiles'] if r['case'] == 'or1200_noc5']
            self.assertEqual([r['defines'] for r in power], [[], ['OR1200_PM_IMPLEMENTED']])
            self.assertFalse(result['budget_observations']['default_values_changed'])

    def test_design_dependencies_exclude_testbenches_and_header_only_units(self):
        catalog = nochange.inventory()
        for design in ('alpha','dlx','fpu','mor1kx','or1200'):
            case = next(c for c in catalog['cases'] if c['design'] == design)
            sources = nochange_full.source_files(case, catalog)
            self.assertTrue(sources)
            self.assertFalse(any('/bench/' in n or '/tests/' in n or 'defines.v' in n for n in sources))
            self.assertTrue(all(n in catalog['input_files'] for n in sources))
        contexts, _ = nochange_full.contexts(catalog)
        self.assertEqual(contexts['contexts']['or1200_noc2']['top'], 'or1200_dc_top')

    def test_censored_and_inactive_results_do_not_calibrate_success_budget(self):
        rows = []
        for name,backend,status,effect,seconds in (
            ('comb','SAT','equivalent','changed_frontend_IR',0.2),
            ('seq','EQY','equivalent','changed_frontend_IR',70),
            ('slow_negative','EQY','different','changed_frontend_IR',500),
            ('timeout','EQY','unknown','changed_frontend_IR',600),
            ('comment','EQY','equivalent','preprocessor_tokens_same',300)):
            rows.append(dict(case=name,profile=0,backend=backend,status=status,edit_effect=effect,
                             proof_seconds={'max':seconds}))
        report = nochange_full.budget_observations(rows)
        self.assertEqual(report['backends']['SAT']['eligible_profiles'], ['comb/profile-0'])
        self.assertEqual(report['backends']['EQY']['eligible_profiles'], ['seq/profile-0'])
        self.assertEqual(report['backends']['EQY']['observed_max_seconds'], 70)
        self.assertFalse(report['shared_total_budget_calibrated'])


@unittest.skipUnless(all(shutil.which(t) for t in ('yosys','eqy','sby','yices-smt2')), 'formal tools required')
class FullSurveyTests(unittest.TestCase):
    def test_async_reset_is_measured_as_scope_rejection_and_disabled_feature_is_not_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = nochange.run(parser().parse_args(['anubis-nochange','--with-sequential',
                '--cases','or1200_noc0','or1200_noc5','--repeats','1','--out',tmp+'/survey']))
            self.assertEqual(result['status'], 'completed_with_findings')
            self.assertEqual(result['counts'], {'out_of_scope':2, 'structural_same':1})
            self.assertTrue(result['inputs_unchanged'])
            for row in result['profiles']:
                self.assertEqual(row['samples'], [])
                self.assertFalse((Path(row['directory'])/'baseline').exists())
                if row['status'] == 'out_of_scope':
                    self.assertIn('$adff', row['detail'])
                    self.assertEqual(set(row['scope_checks']), {'old','new'})
            self.assertEqual(result['budget_observations']['backends']['EQY']['eligible_count'], 0)

    def test_comb_proofs_still_repeat_and_input_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ['anubis-nochange','--with-sequential','--cases','dlx_noc1','--repeats','2']
            result = nochange.run(parser().parse_args(args+['--out',tmp+'/good']))
            row = result['profiles'][0]
            self.assertEqual(row['status'], 'equivalent')
            self.assertEqual(row['proof_seconds']['samples'], 2)
            self.assertTrue(all(not s['full_synthesis_used'] for s in row['samples']))
            original = nochange.measure
            def tamper(*args, **kwargs):
                row = original(*args, **kwargs)
                source = args[5]/'dlx/bypass_ex.v'
                source.write_text(source.read_text()+'\n')
                return row
            with patch.object(nochange, 'measure', side_effect=tamper):
                bad = nochange.run(parser().parse_args(args+['--out',tmp+'/bad']))
            self.assertEqual(bad['status'], 'failed')
            self.assertFalse(bad['inputs_unchanged'])

    def test_unverified_baseline_does_not_run_modified_design_proof_or_claim_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(nochange_full.sequential, 'baseline', return_value={
                'status':'blocked', 'elapsed_seconds':1.0,
                'proof':{'status':'unknown','reason':'new_outputs_not_proven_defined','elapsed_seconds':0.9}}), \
                patch.object(nochange_full.sequential, 'reuse') as reuse:
                result = nochange.run(parser().parse_args(['anubis-nochange','--with-sequential',
                    '--cases','dlx_noc3','--repeats','1','--out',tmp+'/survey']))
            reuse.assert_not_called()
            self.assertEqual(result['profiles'][0]['reason'], 'old_baseline_not_proven')
            self.assertEqual(result['profiles'][0]['samples'], [])
            self.assertEqual(result['counts'], {'unknown':1})
            self.assertEqual(result['budget_observations']['backends']['EQY']['eligible_count'], 0)


if __name__ == '__main__':
    unittest.main()
