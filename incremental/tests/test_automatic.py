"""Automatic edits use only archived records and complete new ANUBIS RTL."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from incremental import anubis, automatic, replace, rtlil, statements
from incremental.__main__ import parser
from incremental.tests.test_replacement import NATIVE
from yosys_flow.cli import parser as flow_parser
from yosys_flow.common import TraceError
from yosys_flow.contracts import ContractContext, seal
from yosys_flow.extract import extract
from yosys_flow.runner import run as baseline_run


class IRComparisonTests(unittest.TestCase):
    def test_generated_names_and_source_moves_do_not_become_changes(self):
        # Writer-format unit data; end-to-end RTL cases below all use ANUBIS.
        a='''attribute \\src "old.v:1.1-4.1"
module \\top
  wire input 1 \\a
  wire output 2 \\y
  wire $and$old.v:2$1_Y
  cell $not $not$old.v:2$1
    connect \\A \\a
    connect \\Y $and$old.v:2$1_Y
  end
  connect \\y $and$old.v:2$1_Y
end
'''
        b=a.replace('old.v:1.1-4.1','moved.v:20.1-24.1').replace('old.v:2$1','moved.v:21$99')
        self.assertFalse(rtlil.compare(rtlil.parse(a),rtlil.parse(b),'top')[0]['changes'])
        b=b.replace('cell $not','cell $pos')
        self.assertEqual(len(rtlil.compare(rtlil.parse(a),rtlil.parse(b),'top')[0]['changes']),1)
        self.assertEqual(automatic.expanded(['chip%24top/u0'],'chip$top'),['chip%24top'])
        self.assertEqual(automatic.expanded(['chip%24top'],'chip$top'),['chip%24top'])


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built; run make native')
class AutomaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='automatic-anubis-')
        cls.root=Path(os.environ.get('YOSYS_FLOW_AUTO_RESULTS',cls.temp.name)).resolve()
        cls.root.mkdir(parents=True,exist_ok=True)
        cls.cases={}
        for name in ('bypass_noc1','compare_local9','writeback_local10'):
            fixture=cls.root/name/'fixture';anubis.prepare(name,fixture)
            base=cls.root/name/'base-run'
            baseline_run(flow_parser().parse_args(['run','--source',str(fixture/'base.v'),'--top','benchmark',
                '--out',str(base),'--native-yosys',str(NATIVE)]))
            cls.cases[name]=(fixture,base)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def execute(self,case,name,source=None,base=None,selection_mode=None):
        fixture,original=self.cases[case];out=self.root/name
        args=parser().parse_args(['auto','--base-run',str(base or original),
            '--new-source',str(source or fixture/'new.v'),'--out',str(out),'--native-yosys',str(NATIVE),
            '--no-equivalence-precheck'])
        if selection_mode is not None:args.selection_mode=selection_mode
        result=automatic.run(args)
        return result,out

    def test_automatic_actual_function_edit_and_instance_isolation(self):
        result,out=self.execute('compare_local9','function-edit')
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['selected_scopes'],['benchmark/u_target'])
        self.assertEqual(result['selection_strategy'],'statement_dependency_slice')
        self.assertLess(result['splice']['removed_cells'],115)  # previous whole-leaf replacement
        self.assertGreater(result['splice']['reused_cells'],116)
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(result['changes'][0]['ir_changes'][0]['old'],[['connect','\\B','0']])
        self.assertEqual(result['changes'][0]['ir_changes'][0]['new'],[['connect','\\B','1']])
        self.assertTrue((out/'patch.il').is_file());self.assertTrue((out/'patch.v').is_file())
        self.assertTrue((out/'bindings.json').is_file())
        plan_path=out/'replacement-plan.json'
        plan=json.loads(plan_path.read_text())
        self.assertEqual(plan['schema_version'],2)
        self.assertEqual(plan['report_type'],'replacement_plan')
        self.assertLess(plan_path.stat().st_size,2_000_000)
        self.assertFalse((out/'region.contract.json').exists())
        self.assertFalse((out/'replacement/contract.json').exists())
        self.assertFalse((out/'replacement/replacement-plan.json').exists())
        replacement=json.loads((out/'replacement/report.json').read_text())
        self.assertEqual(Path(replacement['selection_document']['path']),plan_path)
        self.assertEqual(replacement['selection_document']['sha256'],result['replacement_plan']['sha256'])
        self.assertIn('read_rtlil',(out/'replacement/work/local-synth.ys').read_text())
        self.assertNotIn('new.frontend.il',(out/'replacement/work/local-synth.ys').read_text())
        self.assertIn('new.frontend.il',(out/'replacement/work/new-reference.ys').read_text())
        self.assertNotIn('synth',(out/'work/new-frontend.ys').read_text())
        self.assertNotIn('abc',(out/'work/new-combinational-check.ys').read_text())

    def test_generation_cutoff_includes_full_export_and_excludes_later_proof(self):
        original_proof=replace.prove_local_outputs
        real_clock=replace.time.perf_counter
        offset=[0.0]
        destination=self.root/'generation-cutoff'
        def proof_after_generation(*args,**kwargs):
            for suffix in ('json','il','v'):
                self.assertTrue((destination/'replacement/work'/('candidate.'+suffix)).is_file())
            # Charge 100 simulated seconds to a post-export proof. This must
            # affect validation/checked timings, never netlist generation.
            offset[0]+=100
            return original_proof(*args,**kwargs)
        with patch.object(replace,'prove_local_outputs',side_effect=proof_after_generation), \
                patch.object(replace.time,'perf_counter',side_effect=lambda:real_clock()+offset[0]):
            result,out=self.execute('bypass_noc1','generation-cutoff')
        self.assertEqual(result['status'],'passed',result.get('failure'))
        timings=result['timings']
        self.assertGreater(timings['post_generation_validation_seconds'],100)
        self.assertGreater(timings['incremental_candidate_ready_seconds']-timings['netlist_generation_seconds'],100)
        self.assertEqual(timings['netlist_generation_seconds'],timings['automatic_preparation_seconds']+
                         timings['replacement']['netlist_generation_seconds'])
        for suffix in ('json','il','v'):
            self.assertTrue((out/('incremental.'+suffix)).is_file())

    def test_post_generation_proof_failure_keeps_candidate_unpublished(self):
        with patch.object(replace,'prove_local_outputs',side_effect=TraceError('test post-generation proof failure')):
            result,out=self.execute('bypass_noc1','generation-rejected')
        self.assertEqual(result['status'],'failed')
        replacement=json.loads((out/'replacement/report.json').read_text())
        self.assertGreater(replacement['timings']['netlist_generation_seconds'],0)
        self.assertTrue((out/'replacement/work/candidate.v').is_file())
        for suffix in ('json','il','v'):
            self.assertFalse((out/('incremental.'+suffix)).exists())
            self.assertFalse((out/'replacement'/('incremental.'+suffix)).exists())

    def test_module_selection_ablation_replaces_more_cells_with_the_same_safety_checks(self):
        fine,_=self.execute('compare_local9','ablation-fine')
        with patch.object(statements,'plan',side_effect=AssertionError('module mode must skip statement planning')):
            module,out=self.execute('compare_local9','ablation-module',selection_mode='module')
        for result in (fine,module):
            self.assertEqual(result['status'],'passed',result.get('failure'))
            self.assertEqual(result['selected_scopes'],['benchmark/u_target'])
            self.assertTrue(all(result['checks'].values()))
            times=result['timings']
            self.assertGreater(times['selection_contract_binding_seconds'],0)
            self.assertGreater(times['patch_export_seconds'],0)
            self.assertGreaterEqual(times['region_contract_patch_binding_seconds'],
                times['selection_contract_binding_seconds']+times['patch_export_seconds'])
        self.assertEqual(fine['selection_mode'],'fine')
        self.assertEqual(module['selection_mode'],'module')
        self.assertEqual(module['selection_strategy'],'module_scope_ablation')
        self.assertEqual(module['checks'].keys(),fine['checks'].keys())
        self.assertGreater(module['splice']['removed_cells'],fine['splice']['removed_cells'])
        self.assertLess(module['splice']['reused_cells'],fine['splice']['reused_cells'])
        self.assertTrue((out/'replacement-plan.json').is_file())
        self.assertTrue((out/'replacement/work/whole-equivalence.log').is_file())

    def test_same_call_context_handoff_preserves_proofs_and_standalone_replay(self):
        created=[]
        original=ContractContext.__init__
        def record(context,*args,**kwargs):
            original(context,*args,**kwargs)
            created.append(context)
        for mode in ('fine','module'):
            created.clear()
            with patch.object(ContractContext,'__init__',record):
                result,out=self.execute('bypass_noc1','handoff-'+mode,selection_mode=mode)
            self.assertEqual(result['status'],'passed',result.get('failure'))
            self.assertTrue(all(result['checks'].values()))
            self.assertEqual(len(created),2)  # independent initial and final archive loads
            self.assertIsNot(created[0],created[1])
            replacement=json.loads((out/'replacement/report.json').read_text())
            self.assertEqual(replacement['baseline_context_source'],'same_automatic_call')
            self.assertEqual(replacement['timings']['baseline_load_and_evidence_replay_seconds'],0)
            self.assertGreater(replacement['timings']['baseline_artifact_recheck_seconds'],0)
            self.assertGreater(replacement['timings']['final_baseline_revalidation_seconds'],0)
        # Independent callers load and validate the archive at entry as well,
        # restoring its verified index when available.
        fixture,base=self.cases['bypass_noc1']
        args=SimpleNamespace(base_run=str(base),contract=str(out/'replacement-plan.json'),
            bindings=str(out/'bindings.json'),patch_rtl=str(out/'patch.il'),patch_top='__auto_patch',
            patch_is_rtlil=True,new_frontend=str(out/'work/new.frontend.il'),baseline_from_archive=True,
            new_source=[str(fixture/'new.v')],out=str(self.root/'handoff-standalone'),yosys='yosys')
        created.clear()
        with patch.object(ContractContext,'__init__',record):direct=replace.run(args)
        self.assertEqual(direct['status'],'passed')
        self.assertTrue(all(direct['checks'].values()))
        self.assertEqual(len(created),2)
        self.assertEqual(direct['baseline_context_source'],'fresh_archive_load')
        self.assertGreater(direct['timings']['baseline_load_and_evidence_replay_seconds'],0)
        self.assertNotIn('baseline_artifact_recheck_seconds',direct['timings'])

    def test_missing_optional_index_replays_through_automatic_handoff(self):
        _,original=self.cases['bypass_noc1']
        base=self.root/'without-index';shutil.copytree(original,base)
        (base/'verified-index.json.gz').unlink()
        result,_=self.execute('bypass_noc1','replay-without-index',base=base)
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['baseline_load']['verified_index_status'],'missing')
        self.assertTrue(all(result['checks'].values()))

    def test_plan_reference_is_bound_to_the_bytes_actually_validated(self):
        original=ContractContext.validate
        plan=self.root/'plan-changed/replacement-plan.json'
        changed=[]
        def mutate_after_read(context,document,*args,**kwargs):
            value=original(context,document,*args,**kwargs)
            if plan.is_file() and not changed:
                changed.append(True)
                with plan.open('a') as stream:stream.write(' ')
            return value
        with patch.object(ContractContext,'validate',mutate_after_read):
            result,out=self.execute('bypass_noc1','plan-changed')
        self.assertTrue(changed)
        self.assertEqual(result['status'],'failed')
        replacement=json.loads((out/'replacement/report.json').read_text())
        self.assertIn('replacement plan changed',replacement['failure']['detail'])
        self.assertEqual(replacement['selection_document']['sha256'],result['replacement_plan']['sha256'])
        self.assertFalse((out/'incremental.v').exists())

    def test_context_handoff_rechecks_all_archive_bytes_before_local_synthesis(self):
        _,base=self.cases['bypass_noc1'];original=statements.plan
        for artifact in ('manifest.json','after.json','yosys.jsonl'):
            target=self.root/('handoff-tampered-'+artifact);shutil.copytree(base,target)
            def corrupt(*args,**kwargs):
                planned=original(*args,**kwargs)
                with (target/artifact).open('a') as file:file.write(' ')
                return planned
            with patch.object(statements,'plan',side_effect=corrupt):
                result,out=self.execute('bypass_noc1','handoff-reject-'+artifact,base=target)
            self.assertEqual(result['status'],'failed',result)
            self.assertEqual(result['failure']['code'],'invalid_recorded_evidence')
            self.assertFalse((out/'replacement/work/local-synth.ys').exists())
            self.assertFalse((out/'incremental.json').exists())

    def test_context_handoff_keeps_fresh_final_audit_after_local_synthesis(self):
        _,base=self.cases['bypass_noc1'];target=self.root/'handoff-late-tampered'
        shutil.copytree(base,target);original=replace.run_yosys
        def corrupt_after_synthesis(binary,work,name,script):
            elapsed=original(binary,work,name,script)
            if name=='local-synth':
                with (target/'after.json').open('a') as file:file.write(' ')
            return elapsed
        with patch.object(replace,'run_yosys',side_effect=corrupt_after_synthesis):
            result,out=self.execute('bypass_noc1','handoff-late-rejected',base=target)
        self.assertEqual(result['status'],'failed',result)
        replacement=json.loads((out/'replacement/report.json').read_text())
        self.assertEqual(replacement['failure']['stage'],'baseline_revalidation')
        self.assertEqual(replacement['failure']['code'],'invalid_recorded_evidence')
        self.assertTrue((out/'replacement/work/candidate.json').exists())
        self.assertFalse((out/'replacement/incremental.json').exists())
        self.assertFalse((out/'incremental.json').exists())

    def test_context_handoff_does_not_trust_a_resealed_contract(self):
        original=replace._run
        def corrupt(args,**kwargs):
            path=Path(args.contract);document=json.loads(path.read_text())
            document['verification']['status']='partial_old_region'
            path.write_text(json.dumps(seal(document)))
            return original(args,**kwargs)
        with patch.object(replace,'_run',side_effect=corrupt):
            result,out=self.execute('bypass_noc1','handoff-resealed-contract')
        self.assertEqual(result['status'],'failed',result)
        # The frozen automatic-plan hash now rejects the replacement before
        # semantic replay, even when the attacker reseals the document itself.
        self.assertIn('replacement plan changed since automatic selection',result['failure']['detail'])
        self.assertFalse((out/'replacement/work/local-synth.ys').exists())
        self.assertFalse((out/'incremental.json').exists())

    def test_context_handoff_rejects_another_archive_directory(self):
        _,base=self.cases['bypass_noc1'];context=ContractContext(base)
        copied=self.root/'handoff-identical-copy';shutil.copytree(base,copied)
        with self.assertRaisesRegex(TraceError,'different baseline directory'):
            replace._recheck_context_archive(context,copied)

    def test_equivalent_macro_rewrite_is_also_derived(self):
        result,_=self.execute('bypass_noc1','equivalent-edit')
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['selected_scopes'],['benchmark/u_target'])
        self.assertEqual(result['selection_strategy'],'statement_dependency_slice')
        self.assertLess(result['splice']['removed_cells'],31)
        self.assertGreater(result['splice']['reused_cells'],32)

    def test_only_comments_locations_and_module_rename_reuse_every_gate(self):
        fixture,_=self.cases['bypass_noc1']
        leaf=(fixture/'base-leaf.v').read_text().replace('module anubis_base','module renamed')
        # Inline the existing macro definitions so no new original path is needed.
        leaf=leaf.replace('`include "dlx_defs.v"',(fixture/'dlx_defs.v').read_text())
        top=(fixture/'base-top.v').read_text().replace('anubis_base','renamed')
        source=self.root/'format-only.v';source.write_text('// moved file\n\n'+leaf+'\n'+top)
        result,out=self.execute('bypass_noc1','format-only',source)
        self.assertEqual(result['status'],'unchanged',result.get('failure'))
        self.assertFalse(result['changes']);self.assertTrue(all(result['checks'].values()))
        self.assertFalse((out/'patch.il').exists())

    def test_shared_definition_edit_discovers_both_instances(self):
        fixture,_=self.cases['compare_local9']
        leaf=(fixture/'edited-leaf.v').read_text().replace('module anubis_edited','module anubis_base')
        leaf=leaf.replace('`include "dlx_defs.v"',(fixture/'dlx_defs.v').read_text())
        source=self.root/'shared-definition.v';source.write_text(leaf+'\n'+(fixture/'base-top.v').read_text())
        result,_=self.execute('compare_local9','both-instances',source)
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['selected_scopes'],['benchmark/u_other','benchmark/u_target'])
        self.assertGreaterEqual(result['splice']['reused_cells'],1)  # summary, plus any reusable cuts

    def test_new_dependency_is_added_and_bound_from_old_top_driver(self):
        fixture,_=self.cases['bypass_noc1']
        # A wiring mutation of the ANUBIS wrapper, not another claimed upstream macro.
        text=(fixture/'new.v').read_text().replace(str(fixture/'new-top.v'),str(self.root/'rewired-top.v'))
        top=(fixture/'new-top.v').read_text().replace('.IRex(target_IRex)',
                                                   '.IRex({target_IRmem[0], target_IRex[30:0]})')
        (self.root/'rewired-top.v').write_text(top)
        source=self.root/'rewired.v';source.write_text(text)
        result,out=self.execute('bypass_noc1','new-dependency',source)
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['selected_scopes'],['benchmark'])
        specification=json.loads((out/'patch-specification.json').read_text())
        self.assertTrue(specification['additional_input_bits'])
        contract=json.loads((out/'replacement-plan.json').read_text())
        self.assertEqual(contract['derivation']['kind'],'extend_inputs')
        self.assertTrue(all(result['checks'].values()))
        exported=extract(flow_parser().parse_args(['extract',str(self.cases['bypass_noc1'][1]),
            '--contract',str(out/'replacement-plan.json'),'--out',str(out/'old-region.v')]))
        self.assertTrue(all(exported['validation'].values()))
        boundary=json.loads(Path(exported['provenance']).read_text())['boundary']
        self.assertEqual({b['original_bit'] for b in boundary if b['reason']=='new_edit_dependency'},
                         set(specification['additional_input_bits']))

    def test_parameter_derived_module_identity_is_preserved(self):
        # Add a parameter to the ANUBIS wrapper/leaf without changing the
        # upstream baseline function. The actual edit is still LOCAL_9.
        fixture=self.root/'parameter-fixture';anubis.prepare('compare_local9',fixture)
        for name in ('base-leaf.v','edited-leaf.v'):
            path=fixture/name
            path.write_text(path.read_text().replace('(RSbus, RTbus, QCsel, Result);',
                '#(parameter SIGN=31) (RSbus, RTbus, QCsel, Result);').replace('RSbus[31]','RSbus[SIGN]'))
        for name in ('base-top.v','new-top.v'):
            path=fixture/name
            path.write_text(path.read_text().replace(' u_target(', ' #(.SIGN(31)) u_target('))
        baseline=self.root/'parameter-baseline'
        baseline_run(flow_parser().parse_args(['run','--source',str(fixture/'base.v'),'--top','benchmark',
            '--out',str(baseline),'--native-yosys',str(NATIVE)]))
        result,out=self.execute('compare_local9','parameter-edit',fixture/'new.v',baseline)
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['selected_scopes'],['benchmark/u_target'])
        self.assertTrue(result['changes'][0]['new_module'].startswith('$paramod'))
        self.assertEqual(result['selection_strategy'],'statement_dependency_slice')
        self.assertTrue(all(result['checks'].values()))

    def test_old_files_can_be_deleted_or_edited_after_archiving(self):
        fixture=self.root/'archived-fixture';anubis.prepare('bypass_noc1',fixture)
        baseline=self.root/'archived-baseline'
        baseline_run(flow_parser().parse_args(['run','--source',str(fixture/'base.v'),'--top','benchmark',
            '--out',str(baseline),'--native-yosys',str(NATIVE)]))
        shutil.rmtree(fixture)
        result,out=self.execute('bypass_noc1','without-old-files',base=baseline)
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertTrue(result['changed_or_missing_old_files'])
        report=json.loads((out/'replacement/report.json').read_text())
        self.assertEqual(report['baseline_input_policy'],'archived_hashed_frontend_and_netlist')

    def test_latch_is_rejected_before_local_extraction(self):
        result,out=self.execute('writeback_local10','state-rejected')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['failure']['code'],'state_outside_combinational_scope')
        self.assertFalse((out/'incremental.v').exists());self.assertFalse((out/'patch.il').exists())

    def test_candidate_abc_provenance_cannot_be_promoted_by_auto_mode(self):
        fixture,_=self.cases['bypass_noc1'];base=self.root/'flat-base'
        baseline_run(flow_parser().parse_args(['run','--source',str(fixture/'base.v'),'--top','benchmark',
            '--out',str(base),'--flatten','--native-yosys',str(NATIVE)]))
        result,out=self.execute('bypass_noc1','flat-blocked',base=base)
        self.assertEqual(result['status'],'blocked',result.get('failure'))
        self.assertEqual(result['failure']['code'],'old_region_not_proven')
        self.assertEqual(len(result['attempts']),2)
        self.assertFalse((out/'incremental.v').exists())
        module,module_out=self.execute('bypass_noc1','flat-module-blocked',base=base,selection_mode='module')
        self.assertEqual(module['status'],'blocked',module.get('failure'))
        self.assertEqual(module['failure']['code'],'old_region_not_proven')
        self.assertFalse((module_out/'incremental.v').exists())

    def test_modified_archived_frontend_is_rejected(self):
        _,base=self.cases['bypass_noc1'];target=self.root/'tampered-baseline'
        shutil.copytree(base,target)
        with (target/'frontend.il').open('a') as f:f.write('# modified\n')
        for mode in ('fine','module'):
            result,out=self.execute('bypass_noc1','tampered-rejected-'+mode,base=target,selection_mode=mode)
            self.assertEqual(result['status'],'failed')
            self.assertEqual(result['failure']['code'],'invalid_recorded_evidence')
            self.assertFalse((out/'work/new.frontend.il').exists())

    def test_new_frontend_change_between_planning_and_synthesis_is_rejected(self):
        original=statements.plan
        def corrupt_after_planning(*args,**kwargs):
            result=original(*args,**kwargs)
            with (self.root/'new-frontend-tampered/work/new.frontend.il').open('a') as f:
                f.write('# changed after local extraction\n')
            return result
        with patch.object(statements,'plan',side_effect=corrupt_after_planning):
            result,out=self.execute('bypass_noc1','new-frontend-tampered')
        self.assertEqual(result['status'],'failed')
        self.assertIn('new input changed since frontend elaboration',result['failure']['detail'])
        self.assertFalse((out/'incremental.v').exists())

    def test_extra_contract_input_cannot_hide_an_internal_driver_or_corrupt_identity(self):
        # Reuse the automatic result, or build a separate one if unittest orders change.
        result,out=self.execute('bypass_noc1','contract-extension-check')
        self.assertEqual(result['status'],'passed',result.get('failure'))
        context=ContractContext(self.cases['bypass_noc1'][1])
        doc=json.loads((out/'replacement-plan.json').read_text())
        internal=next(b['original_bit'] for b in doc['boundary'] if b['direction']=='output' and b['reason']!='selected_source_connection')
        with self.assertRaisesRegex(TraceError,'driven inside'):
            context.build({'kind':'extend_inputs','parent':doc,'input_bits':[internal]})
        old=rtlil.parse((context.directory/'frontend.il').read_text())
        other=context.build({'kind':'selection',
            'selector':rtlil.source_selector(old['anubis_base'],'benchmark/u_other')})
        external=next(b['original_bit'] for b in other['boundary'] if b['direction']=='output' and b['reason']!='selected_source_connection')
        extension=context.build({'kind':'extend_inputs','parent':doc,'input_bits':[external]})
        self.assertTrue(any(b['reason']=='new_edit_dependency' for b in extension['boundary']))
        tampered=deepcopy(extension)
        next(b for b in tampered['boundary'] if b['reason']=='new_edit_dependency')['drivers']=[]
        with self.assertRaisesRegex(TraceError,'differs from replayed'):
            context.validate(seal(tampered))
        merged=context.build({'kind':'union','parents':[extension,other]})
        self.assertEqual(context.validate(merged),merged)
        self.assertFalse(any(b['direction']=='input' and b['original_bit']==external for b in merged['boundary']))
