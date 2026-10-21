"""Pinned ANUBIS edits: successes, explicit blockers, and rejected state edits."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from incremental import anubis
from incremental.__main__ import parser
from incremental.failures import classify
from incremental.tests.test_replacement import NATIVE
from yosys_flow.abc_internal import read_evidence
from yosys_flow.common import TraceError, unescape
from yosys_flow.contracts import ContractContext
from yosys_flow.region_functions import Circuit, verify_outputs
from yosys_flow.boolean import BDD


class AnubisAdapterTests(unittest.TestCase):
    def test_fixed_inputs_macro_lines_and_only_default_output_adaptation(self):
        lock=anubis.verify_corpus()
        self.assertEqual(lock['commit'],'93088bd3c05407ccd871e8d5067d024f812aeeaa')
        with tempfile.TemporaryDirectory() as tmp:
            fixture=Path(tmp)/'fixture';anubis.prepare('compare_local9',fixture)
            original=(anubis.CORPUS/'upstream/dlx/quick_compare.v').read_text()
            base=(fixture/'base-leaf.v').read_text();new=(fixture/'edited-leaf.v').read_text()
            self.assertEqual(len(base.splitlines()),len(original.splitlines()))
            self.assertEqual(len(new.splitlines()),len(original.splitlines()))
            self.assertRegex(base,r"default\s*:\s*Result\s*=\s*1'b0")
            self.assertEqual(base.count('`dc6'),original.count('`dc6'))
            self.assertEqual((fixture/'dlx_defs.v').read_bytes(),(anubis.CORPUS/'upstream/dlx/dlx_defs.v').read_bytes())
            self.assertNotEqual(base,new)
            manifest=json.loads((fixture/'fixture.json').read_text())
            self.assertFalse(manifest['original_historical_commit_pair_claimed'])
            control=(anubis.CORPUS/'upstream/dlx/alu_control.v').read_text()
            adapted,_=anubis.materialize(control,set(),'alu_control_bug','leaf')
            self.assertIn('casex',adapted)
            self.assertIn('`dc6',adapted)
            edited,_=anubis.materialize(control,{'ANUBIS_LOCAL_2'},'alu_control_bug','leaf')
            self.assertTrue(all(line in adapted or line in edited for line in control.splitlines() if '`dc6' in line))

    def test_corpus_tampering_is_rejected_before_running_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'upstream').mkdir()
            (root/'lock.json').write_text(json.dumps({'files':{'input.v':'wrong_hash'}}))
            (root/'upstream/input.v').write_text('changed')
            with patch.object(anubis,'CORPUS',root), self.assertRaisesRegex(TraceError,'checksum'):
                anubis.verify_corpus()

    def test_failures_do_not_claim_to_diagnose_shared_consumer_splits_from_sat_alone(self):
        report=classify('whole_design_equivalence','whole-equivalence failed: proof failed')
        self.assertEqual(report['code'],'new_design_equivalence_failed')
        self.assertIn('shared_consumers_require_split',report['possible_causes'])
        self.assertFalse(report['proven_safe'])


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built; run make native')
class AnubisIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='anubis-regression-')
        cls.root=Path(os.environ.get('YOSYS_FLOW_ANUBIS_RESULTS',str(Path(cls.temp.name)/'experiment')))
        cls.report=anubis.run(parser().parse_args(['anubis','--out',str(cls.root),'--native-yosys',str(NATIVE)]))
        cls.rows={(r['case'],r['mode']):r for r in cls.report['cases']}

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_retained_equivalent_and_functional_edits_replace_only_target(self):
        for name in ('bypass_noc1','compare_noc2','compare_local9','bypass_with_adder'):
            with self.subTest(case=name):
                row=self.rows[name,'retained']
                self.assertEqual(row['status'],'passed',row.get('failure'))
                self.assertTrue(all(row['checks'].values()))
                expected='different_with_sat_witness' if name=='compare_local9' else 'equivalent_all_outputs'
                self.assertEqual(row['base_new_relation'],expected)
                self.assertLess(row['selection']['contract_cells'],row['selection']['whole_design_cells'])
                self.assertFalse(row['selection']['minimal_region_claimed'])
                run=self.root/name/'retained/replacement'
                old=json.loads((run/'work/base-view.json').read_text())['modules']['benchmark']
                new=json.loads((run/'incremental.json').read_text())['modules']['benchmark']
                self.assertEqual(old['ports'],new['ports'])
                for cell in row['splice']['reused_names']:
                    self.assertEqual(old['cells'][cell],new['cells'][cell])
                self.assertTrue(all('u_target/' in n for n in row['splice']['removed_names']))
                timing=row['timings'];replacement=timing['replacement']
                self.assertGreater(timing['incremental_candidate_ready_seconds'],replacement['local_synth_seconds'])
                self.assertGreater(timing['online_checked_total_seconds'],timing['incremental_candidate_ready_seconds'])
                self.assertTrue((run/'work/whole-equivalence.log').is_file())

    def test_flatten_and_control_blockers_are_not_counted_as_success(self):
        self.assertEqual(self.report['counts'],{'passed':4,'expected_rejection':2,'blocked':6,'failed':0})
        self.assertEqual(self.report['status'],'completed_with_limitations')
        for (name,mode),row in self.rows.items():
            if row['status']!='blocked': continue
            self.assertEqual(row['failure']['code'],'old_region_not_proven')
            self.assertTrue(row['failure']['blockers'])
            self.assertFalse((self.root/name/mode/'replacement/incremental.v').exists())
        for name in ('bypass_noc1','compare_noc2','compare_local9','bypass_with_adder'):
            details=self.rows[name,'flatten']['failure']['abc']
            missing=[r for a in details for r in a['stage_witness_failures']]
            self.assertTrue(missing)
            self.assertTrue(all(r['command'] and r['snapshot'] for r in missing))
        self.assertIn('connection_consumers_not_fully_bound',self.rows['control_local2','retained']['failure']['blockers'])

    def test_missing_default_edit_is_rejected_as_state_in_both_modes(self):
        for mode in ('retained','flatten'):
            row=self.rows['writeback_local10',mode]
            self.assertEqual(row['status'],'expected_rejection',row)
            self.assertEqual(row['failure']['code'],'state_outside_combinational_scope')

    def test_independent_anubis_32_bit_add_does_not_exhaust_target_proof(self):
        for mode in ('retained','flatten'):
            row=self.rows['bypass_with_adder',mode]
            self.assertTrue(all(a['fallback_reason'] is None for a in row['abc_resources']))
            self.assertTrue(any(a['region_input_count']>=64 for a in row['abc_resources']))
            run=self.root/'bypass_with_adder'/mode
            context=ContractContext(run/'base-run')
            selected=json.loads((run/'edit-query.json').read_text())
            cells={unescape(c['name']) for c in selected['final_cells']}
            circuit=Circuit(context.module['cells'],{},BDD())
            neighbor=set().union(*(set(circuit.cone(b)) for b in context.module['ports']['neighbor_sum']['bits']))
            self.assertTrue(cells)
            self.assertTrue(cells.isdisjoint(neighbor))

    def test_real_compare_contract_proof_is_sensitive_to_operand_order(self):
        run=self.root/'compare_local9/retained'
        context=ContractContext(run/'base-run')
        contract=json.loads((run/'region.contract.json').read_text())
        selected={unescape(c['name']):context.module['cells'][unescape(c['name'])] for c in contract['region']['cells']}
        ordered=verify_outputs(selected,selected,contract['boundary'])
        self.assertTrue(ordered['all_outputs_verified'])
        # Reproduce the former grouped bit order using the REAL synthesized
        # ANUBIS comparator, not a BDD-only arithmetic model.
        with patch('yosys_flow.region_functions.ordered_inputs',side_effect=lambda bs:
                   sorted((b for b in bs if b['direction']=='input'),key=lambda b:b['original_bit'])):
            grouped=verify_outputs(selected,selected,contract['boundary'])
        self.assertFalse(grouped['all_outputs_verified'])

    def test_cleanup_actual_deletions_mapping_polarity_and_chain_corruption(self):
        run=self.root/'compare_local9/retained/base-run'
        paths=list(run.glob('*input.blif.flow.jsonl'))
        filename=next(p for p in paths if any(r.get('deleted') for r in map(json.loads,p.read_text().splitlines())))
        original=[json.loads(s) for s in filename.read_text().splitlines()]
        script=Path(str(filename).replace('.input.blif.flow.jsonl','.script')).read_text()
        _,_,_,summary=read_evidence(filename,script)
        self.assertEqual(summary['schema'],4)
        self.assertGreater(summary['cleanup_deleted_objects'],0)
        self.assertFalse(summary['complete_rewrite_history'])
        for change in ('polarity','mapping','disposition','constant','chain'):
            rows=deepcopy(original)
            cleanup=next(r for r in rows if r.get('reason')=='fraig_cleanup_copy')
            if change=='polarity': cleanup['links'][-1][1]^=1
            elif change=='mapping': cleanup['links'][1][1],cleanup['links'][2][1]=cleanup['links'][2][1],cleanup['links'][1][1]
            elif change=='disposition': cleanup['deleted'].pop()
            elif change=='constant': cleanup['links'][0][1]^=1
            else: cleanup['before']=next(r['before'] for r in rows if r.get('reason')=='fraig_sweep_copy')
            target=Path(self.temp.name)/('corrupt-'+change+'.jsonl')
            target.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with self.subTest(change=change),self.assertRaises(TraceError): read_evidence(target,script)
