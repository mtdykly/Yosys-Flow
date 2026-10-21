"""Automatic edits use only archived records and complete new ANUBIS RTL."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from incremental import anubis, automatic, rtlil
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

    def execute(self,case,name,source=None,base=None):
        fixture,original=self.cases[case];out=self.root/name
        args=parser().parse_args(['auto','--base-run',str(base or original),
            '--new-source',str(source or fixture/'new.v'),'--out',str(out),'--native-yosys',str(NATIVE)])
        result=automatic.run(args)
        return result,out

    def test_automatic_actual_function_edit_and_instance_isolation(self):
        result,out=self.execute('compare_local9','function-edit')
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['selected_scopes'],['benchmark/u_target'])
        self.assertEqual(result['splice']['removed_cells'],115)
        self.assertEqual(result['splice']['reused_cells'],116)
        self.assertTrue(all(result['checks'].values()))
        self.assertEqual(result['changes'][0]['ir_changes'][0]['old'],[['connect','\\B','0']])
        self.assertEqual(result['changes'][0]['ir_changes'][0]['new'],[['connect','\\B','1']])
        self.assertTrue((out/'patch.il').is_file());self.assertTrue((out/'patch.v').is_file())
        self.assertTrue((out/'bindings.json').is_file())
        self.assertIn('read_rtlil',(out/'replacement/work/local-synth.ys').read_text())
        self.assertNotIn('new.frontend.il',(out/'replacement/work/local-synth.ys').read_text())
        self.assertIn('new.frontend.il',(out/'replacement/work/new-reference.ys').read_text())
        self.assertNotIn('synth',(out/'work/new-frontend.ys').read_text())
        self.assertNotIn('abc',(out/'work/new-combinational-check.ys').read_text())

    def test_equivalent_macro_rewrite_is_also_derived(self):
        result,_=self.execute('bypass_noc1','equivalent-edit')
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['selected_scopes'],['benchmark/u_target'])
        self.assertEqual(result['splice']['removed_cells'],31)
        self.assertEqual(result['splice']['reused_cells'],32)

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
        self.assertEqual(result['splice']['reused_cells'],1)  # summary only

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
        contract=json.loads((out/'region.contract.json').read_text())
        self.assertEqual(contract['derivation']['kind'],'extend_inputs')
        self.assertTrue(all(result['checks'].values()))
        exported=extract(flow_parser().parse_args(['extract',str(self.cases['bypass_noc1'][1]),
            '--contract',str(out/'region.contract.json'),'--out',str(out/'old-region.v')]))
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
        self.assertIn('cell $paramod',(out/'patch.il').read_text())
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

    def test_modified_archived_frontend_is_rejected(self):
        _,base=self.cases['bypass_noc1'];target=self.root/'tampered-baseline'
        shutil.copytree(base,target)
        with (target/'frontend.il').open('a') as f:f.write('# modified\n')
        result,out=self.execute('bypass_noc1','tampered-rejected',base=target)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['failure']['code'],'invalid_recorded_evidence')
        self.assertFalse((out/'work/new.frontend.il').exists())

    def test_new_frontend_change_between_planning_and_synthesis_is_rejected(self):
        original=automatic.patch_for
        def corrupt_after_planning(*args,**kwargs):
            result=original(*args,**kwargs)
            with (self.root/'new-frontend-tampered/work/new.frontend.il').open('a') as f:
                f.write('# changed after local extraction\n')
            return result
        with patch.object(automatic,'patch_for',side_effect=corrupt_after_planning):
            result,out=self.execute('bypass_noc1','new-frontend-tampered')
        self.assertEqual(result['status'],'failed')
        self.assertIn('new input changed since frontend elaboration',result['failure']['detail'])
        self.assertFalse((out/'incremental.v').exists())

    def test_extra_contract_input_cannot_hide_an_internal_driver_or_corrupt_identity(self):
        # Reuse the automatic result, or build a separate one if unittest orders change.
        result,out=self.execute('bypass_noc1','contract-extension-check')
        self.assertEqual(result['status'],'passed',result.get('failure'))
        context=ContractContext(self.cases['bypass_noc1'][1])
        doc=json.loads((out/'region.contract.json').read_text())
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
