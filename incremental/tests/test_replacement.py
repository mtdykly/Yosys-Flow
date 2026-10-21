"""Actual local synthesis, immutable outside gates, and rejecting unsafe edits."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from incremental import demo, replace
from incremental.__main__ import parser
from yosys_flow.cli import parser as flow_parser
from yosys_flow.common import ROOT, TraceError, digest, write_json
from yosys_flow.contracts import ContractContext, SELECTOR_KEYS
from yosys_flow.extract import slice_module
from yosys_flow.runner import run as baseline_run

NATIVE = Path(os.environ.get('YOSYS_FLOW_NATIVE',ROOT/'build/native/install/bin/yosys'))


def gate(kind, inputs, output):
    return {'type':kind,'parameters':{},'attributes':{},'hide_name':0,
            'port_directions':{**{p:'input' for p in inputs},'Y':'output'},
            'connections':{**{p:[b] for p,b in inputs.items()},'Y':[output]}}


def example():
    module = {'ports':{'a':{'direction':'input','bits':[2]},'b':{'direction':'input','bits':[3]},
                       'y':{'direction':'output','bits':[4]},'z':{'direction':'output','bits':[5]}},
              'cells':{'and':gate('$_AND_',{'A':2,'B':3},4),'or':gate('$_OR_',{'A':2,'B':3},5)},
              'netnames':{},'attributes':{}}
    _, boundary = slice_module(module,['and','or'],'region')
    contract = {'contract_id':'unit','verification':{'status':'verified_old_region'},
                'region':{'cells':[{'name':'and'},{'name':'or'}]},'boundary':boundary}
    return module,contract,{'schema_version':1,'contract_id':'unit',
        'inputs':{'a':['in_2'],'b':['in_3']},'outputs':{'y':['out_4'],'z':['out_5']}}


class SpliceTests(unittest.TestCase):
    def test_connection_only_old_region_cannot_introduce_new_gates(self):
        module,contract,bindings=example()
        contract['region']['cells']=[]
        with self.assertRaisesRegex(TraceError,'requires selected final gates'):
            replace.splice(module,contract,module,bindings)

    def test_observer_alias_is_preserved_and_cannot_silently_change(self):
        module,contract,bindings=example()
        observer={'port':'observe_a','direction':'output','original_bit':2,
                  'reason':'selected_source_connection','external_users':[]}
        contract['boundary'].append(observer)
        bindings['outputs']['observe_a']=['observe_a']
        patch=deepcopy(module);patch['ports']['observe_a']={'direction':'output','bits':[2]}
        candidate,stats=replace.splice(module,contract,patch,bindings)
        self.assertEqual(stats['output_binding_buffers'],2)
        self.assertEqual(stats['preserved_observer_outputs'],['observe_a'])
        self.assertTrue(replace.prove_local_outputs(candidate,contract,patch,bindings,stats['added_names'])['all_outputs_verified'])
        patch['ports']['observe_a']['bits']=[3]
        candidate,stats=replace.splice(module,contract,patch,bindings)
        with self.assertRaisesRegex(TraceError,'differs from local specification'):
            replace.prove_local_outputs(candidate,contract,patch,bindings,stats['added_names'])
        # A real external consumer converts this into an unsupported driver
        # rebinding problem, even if it shares the same original input alias.
        observer['external_users']=[{'kind':'module_port','name':'z','offset':0}]
        with self.assertRaisesRegex(TraceError,'consumer-rebinding'):
            replace.splice(module,contract,patch,bindings)

    def test_constants_passthrough_and_merged_local_outputs_keep_distinct_old_wires(self):
        module,contract,bindings = example()
        for pair in ([2,'0'],[2,2],['1','1']):
            patch = deepcopy(module); patch['cells'] = {}
            patch['ports']['y']['bits'] = [pair[0]]; patch['ports']['z']['bits'] = [pair[1]]
            candidate,stats = replace.splice(module,contract,patch,bindings)
            self.assertEqual(candidate['ports'],module['ports'])
            self.assertEqual(stats['output_binding_buffers'],2)
            proof = replace.prove_local_outputs(candidate,contract,patch,bindings,stats['added_names'])
            self.assertTrue(proof['all_outputs_verified'])

    def test_missing_output_new_dependency_and_duplicate_bindings_fail(self):
        module,contract,bindings = example()
        for key in ('inputs','outputs'):
            bad = deepcopy(bindings); bad[key].pop(next(iter(bad[key])))
            with self.assertRaisesRegex(TraceError,'ports do not match'):
                replace.splice(module,contract,module,bad)
        bad = deepcopy(bindings); bad['outputs']['z'] = ['out_4']
        with self.assertRaisesRegex(TraceError,'exactly once'):
            replace.splice(module,contract,module,bad)
        patch = deepcopy(module); patch['ports']['new_dependency'] = {'direction':'input','bits':[6]}
        with self.assertRaisesRegex(TraceError,'ports do not match'):
            replace.splice(module,contract,patch,bindings)

    def test_return_path_cannot_become_a_combinational_cycle(self):
        module,_contract,_bindings = example()
        module['ports']['z']['bits'] = [6]
        module['cells']['outside'] = gate('$_NOT_',{'A':4},5)
        module['cells']['or'] = gate('$_OR_',{'A':2,'B':5},6)
        _,boundary = slice_module(module,['and','or'],'region')
        contract = {'contract_id':'unit','verification':{'status':'verified_old_region'},
                    'region':{'cells':[{'name':'and'},{'name':'or'}]},'boundary':boundary}
        patch = {'ports':{'a':{'direction':'input','bits':[2]},'b':{'direction':'input','bits':[3]},
                           'returning':{'direction':'input','bits':[5]},
                           'y':{'direction':'output','bits':[5]},'z':{'direction':'output','bits':[2]}},
                 'cells':{},'netnames':{}}
        bindings = {'schema_version':1,'contract_id':'unit','inputs':{'a':['in_2'],'b':['in_3'],'returning':['in_5']},
                    'outputs':{'y':['out_4'],'z':['out_6']}}
        with self.assertRaisesRegex(TraceError,'cycle'):
            replace.splice(module,contract,patch,bindings)

    def test_candidate_provenance_is_not_upgraded_by_splicing(self):
        module,contract,bindings = example()
        contract['verification']['status'] = 'partial_old_region'
        with self.assertRaisesRegex(TraceError,'verified old region'):
            replace.splice(module,contract,module,bindings)

    def test_explicit_inverted_input_gets_a_checked_adapter(self):
        module,contract,bindings = example()
        bindings['inputs']['a'] = [{'port':'in_2','inverted':True}]
        result,stats = replace.splice(module,contract,module,bindings)
        self.assertEqual(stats['input_polarity_inverters'],1)
        self.assertTrue(replace.prove_local_outputs(result,contract,module,bindings,stats['added_names'])['all_outputs_verified'])


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built; run make native')
class ReplacementIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='local-replacement-')
        cls.root = Path(cls.temp.name); cls.out = cls.root/'demo'
        cls.summary = demo.run(parser().parse_args(['demo','--out',str(cls.out),'--native-yosys',str(NATIVE)]))

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def args(self,mode,name,**overrides):
        directory = self.out/mode
        values = dict(base_run=str(directory/'base-run'),contract=str(directory/'region.contract.json'),
                      bindings=str(directory/'bindings.json'),patch_rtl=str(demo.EXAMPLE/'patch.v'),patch_top='patch',
                      new_source=[str(demo.EXAMPLE/'new.v')],yosys='yosys',out=str(self.root/name))
        return SimpleNamespace(**(values|overrides))

    def test_both_modes_replace_only_target_and_match_every_new_top_output(self):
        for mode in ('retained','flatten'):
            directory = self.out/mode/'replacement'
            report = json.loads((directory/'report.json').read_text())
            self.assertEqual(report['status'],'passed'); self.assertTrue(all(report['checks'].values()))
            self.assertEqual(report['splice']['base_cells'],152)
            self.assertEqual(report['splice']['removed_cells'],16)
            self.assertEqual(report['splice']['reused_cells'],136)
            base = json.loads((directory/'work/base-view.json').read_text())['modules']['chip']
            result = json.loads((directory/'incremental.json').read_text())['modules']['chip']
            for name in report['splice']['reused_names']:
                self.assertEqual(base['cells'][name],result['cells'][name])
            self.assertEqual(base['ports'],result['ports'])
            local_script = (directory/'work/local-synth.ys').read_text()
            self.assertIn('patch.v',local_script); self.assertNotIn('new.v',local_script)
            self.assertIn('sat -verify -prove trigger 0',(directory/'work/whole-equivalence.ys').read_text())
            self.assertFalse(report['safe_for_arbitrary_future_edits'])

    def test_flatten_polarity_is_proven_and_does_not_depend_on_original_wire_names(self):
        evidence = json.loads((self.out/'flatten/boundary-proof.json').read_text())
        self.assertEqual(len(evidence['bindings']),24)
        self.assertTrue(all(b['status']=='proven_against_old_final_circuit' for b in evidence['bindings']))
        report = json.loads((self.out/'flatten/replacement/report.json').read_text())
        self.assertEqual(report['splice']['input_polarity_inverters'],sum(b['inverted'] for b in evidence['bindings']))

    def test_boundary_obligations_include_downstream_summary_and_top_outputs(self):
        for mode in ('retained','flatten'):
            doc = json.loads((self.out/mode/'region.contract.json').read_text())
            self.assertEqual(len(doc['outputs']),8)
            for output in doc['outputs']:
                users = output['external_users']
                self.assertTrue(any(u['kind']=='cell' for u in users))
                self.assertTrue(any(u['kind']=='module_port' and u['name']=='result' for u in users))

    def test_wrong_bit_binding_passes_local_copy_proof_but_fails_whole_design_proof(self):
        bindings = json.loads((self.out/'retained/bindings.json').read_text())
        bindings['inputs']['a'][0],bindings['inputs']['a'][1] = bindings['inputs']['a'][1],bindings['inputs']['a'][0]
        path = self.root/'wrong-bindings.json'; write_json(path,bindings)
        args = self.args('retained','wrong-bit',bindings=str(path))
        with self.assertRaisesRegex(TraceError,'whole-equivalence failed'): replace.run(args)
        report = json.loads((Path(args.out)/'report.json').read_text())
        self.assertEqual(report['status'],'failed')
        self.assertTrue(report['local_output_proof']['all_outputs_verified'])
        self.assertFalse((Path(args.out)/'incremental.v').exists())

    def test_wrong_flattened_input_polarity_is_rejected(self):
        bindings = json.loads((self.out/'flatten/bindings.json').read_text())
        bindings['inputs']['a'][0]['inverted'] = not bindings['inputs']['a'][0]['inverted']
        path = self.root/'wrong-polarity.json'; write_json(path,bindings)
        with self.assertRaisesRegex(TraceError,'whole-equivalence failed'):
            replace.run(self.args('flatten','wrong-polarity',bindings=str(path)))

    def test_shared_old_output_cannot_change_only_one_consumer_without_splitting(self):
        source = self.root/'shared.v'
        source.write_text('module chip(input a,b,output y,z);\nassign y=a&b;\nassign z=a&b;\nendmodule\n')
        baseline = self.root/'shared-base'
        baseline_run(flow_parser().parse_args(['run','--source',str(source),'--top','chip','--out',str(baseline),
                                              '--native-yosys',str(NATIVE)]))
        context = ContractContext(baseline); selector = dict.fromkeys(SELECTOR_KEYS)
        selector.update(source=str(source),line=2)
        contract = context.build({'kind':'selection','selector':selector})
        path = self.root/'shared.contract.json'; write_json(path,contract)
        boundary = {b['original_bit']:b['port'] for b in contract['boundary']}
        bindings = {'schema_version':1,'contract_id':contract['contract_id'],
            'inputs':{n:[boundary[context.module['ports'][n]['bits'][0]]] for n in ('a','b')},
            'outputs':{'q':[contract['outputs'][0]['port']]}}
        bp = self.root/'shared.bindings.json'; write_json(bp,bindings)
        patch = self.root/'shared.patch.v'; patch.write_text('module patch(input a,b,output q); assign q=a|b; endmodule\n')
        new = self.root/'shared.new.v'; new.write_text('module chip(input a,b,output y,z); assign y=a|b; assign z=a&b; endmodule\n')
        args = SimpleNamespace(base_run=str(baseline),contract=str(path),bindings=str(bp),patch_rtl=str(patch),
                               patch_top='patch',new_source=[str(new)],out=str(self.root/'shared-replacement'),yosys='yosys')
        with self.assertRaisesRegex(TraceError,'whole-equivalence failed'): replace.run(args)
        self.assertFalse((Path(args.out)/'incremental.json').exists())

    def test_repeated_output_path_is_not_overwritten(self):
        args = self.args('retained','unused',out=str(self.out/'retained/replacement'))
        path = Path(args.out)/'report.json'; before = digest(path)
        with self.assertRaisesRegex(TraceError,'overwrite'): replace.run(args)
        self.assertEqual(digest(path),before)
