"""Compact plans preserve old-region obligations without copying evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import TraceError
from yosys_flow.contracts import ContractContext, SELECTOR_KEYS, seal
from yosys_flow.extract import extract
from yosys_flow.runner import run


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built; run make test')
class ReplacementPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='replacement-plans-')
        cls.base = Path(cls.temp.name)
        cls.fixtures = {}
        sources = {
            'shared':'''module demo(input a,b,c,d,e, output y,z);
wire t;
assign t=a&b;
assign y=t^c;
assign z=t^d;
endmodule
''',
            'candidate':'''module demo(input [3:0] a,b,c, output [4:0] y);
wire [4:0] t;
assign t=a+b;
assign y=t+c;
endmodule
''',
            'connections':'''module demo(input a, output y,z,w);
assign y=a;
assign z=1'b0;
assign w=1'b0;
endmodule
''',
            'hierarchy':'''module leaf(input a,b,c, output y);
wire t;
assign t=a&b;
assign y=t^c;
endmodule
module demo(input a,b,c,d,e,f, output y,z);
leaf left(a,b,c,y);
leaf right(d,e,f,z);
endmodule
'''}
        for name,rtl in sources.items():
            source = cls.base/(name+'.v'); source.write_text(rtl)
            out = cls.base/name
            result = run(parser().parse_args(['run','--source',str(source),'--top','demo',
                                              '--out',str(out),'--native-yosys',str(NATIVE)]))
            if not all(result['checks'].values()): raise AssertionError(result)
            cls.fixtures[name] = source,out

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def context(self, name, compact=True):
        return ContractContext(self.fixtures[name][1],compact=compact)

    def selected(self, context, name, line, instance=None):
        selector = dict.fromkeys(SELECTOR_KEYS)
        selector.update(source=str(self.fixtures[name][0]),line=line,instance=instance)
        return context.build({'kind':'selection','selector':selector})

    def test_compact_union_and_new_input_preserve_old_functions_and_every_consumer(self):
        full = self.context('shared',False); compact = self.context('shared')
        def make(context):
            parents = [self.selected(context,'shared',n) for n in (3,4)]
            union = context.build({'kind':'union','parents':parents})
            extra = context.module['ports']['e']['bits'][0]
            return context.build({'kind':'extend_inputs','parent':union,'input_bits':[extra]})
        legacy,plan = make(full),make(compact)
        for field in ('request','boundary','outputs','electrical_cut','recorded_group_closure','conditions'):
            self.assertEqual(plan[field],legacy[field],field)
        self.assertEqual(compact.expanded_provenance(plan),legacy['provenance'])
        self.assertEqual(plan['verification']['status'],'verified_old_region')
        self.assertTrue(any(u['kind']=='cell' for out in plan['outputs'] for u in out['external_users']))
        self.assertEqual({u['name'] for out in plan['outputs'] for u in out['external_users']
                          if u['kind']=='module_port'},{'y'})
        self.assertLess(len(json.dumps(plan)),len(json.dumps(legacy))//2)
        self.assertNotIn('supporting_edges',plan['provenance'])
        self.assertTrue(all('all_origins' not in c for c in plan['region']['cells']))
        self.assertEqual(set(plan['derivation']['parent']),{'contract_id','derivation'})
        self.assertNotIn('region',plan['derivation']['parent'])
        # A new process/default audit context reconstructs compact recipes with
        # no access to the in-memory parent cache and without invoking Yosys.
        with patch.dict('os.environ',{'PATH':''}), patch('subprocess.Popen',side_effect=AssertionError('no tools')):
            audit = self.context('shared',False)
            self.assertEqual(audit.validate(json.loads(json.dumps(plan))),plan)
            self.assertEqual(audit.expanded_provenance(plan),legacy['provenance'])

    def test_resealed_proof_boundary_reference_and_recipe_tampering_is_rejected(self):
        context = self.context('shared')
        parents = [self.selected(context,'shared',n) for n in (3,4)]
        plan = context.build({'kind':'union','parents':parents})
        changed = []
        def variant():
            value = copy.deepcopy(plan); changed.append(value); return value
        variant()['outputs'].pop()
        variant()['outputs'][0]['external_users'].clear()
        variant()['outputs'][0]['function']['root'] ^= 1
        variant()['boundary'].pop(0)
        variant()['provenance']['supporting_edge_indices'].pop()
        variant()['provenance']['abc_associations'][0]['evidence_index'] = 999999
        variant()['provenance']['source_ids'].append(999999)
        variant()['provenance']['complete_causal_history'] = True
        variant()['conditions']['functional_edit_assessed'] = True
        variant()['region']['cells'][0]['precision'] = 'abc_region_candidate'
        variant()['safe_for_incremental_reuse'] = True
        variant()['derivation']['parents'][0]['derivation']['selector']['line'] = 5
        for index,bad in enumerate(changed):
            with self.subTest(index=index), self.assertRaises(TraceError):
                context.validate(seal(bad))

    def test_compact_candidate_proof_does_not_upgrade_source_precision(self):
        context = self.context('candidate')
        plan = self.selected(context,'candidate',3)
        legacy = self.selected(self.context('candidate',False),'candidate',3)
        self.assertEqual(plan['verification']['candidate_refs'],legacy['verification']['candidate_refs'])
        self.assertTrue(plan['verification']['all_output_functions']['all_outputs_verified'])
        self.assertEqual(plan['verification']['status'],'partial_old_region')
        self.assertIn('candidate_source_associations',plan['verification']['blockers'])
        self.assertFalse(plan['safe_for_incremental_reuse'])

    def test_constant_connections_keep_separate_consumers(self):
        context = self.context('connections')
        parents = [self.selected(context,'connections',n) for n in (3,4)]
        plan = context.build({'kind':'union','parents':parents})
        self.assertFalse(plan['region']['cells'])
        self.assertEqual(len(plan['outputs']),2)
        self.assertEqual([{u['name'] for u in out['external_users']} for out in plan['outputs']],[{'z'},{'w'}])
        self.assertTrue(all(out['function']['root']==0 for out in plan['outputs']))
        self.assertEqual(self.context('connections',False).validate(plan),plan)

    def test_instance_scope_and_full_contract_compatibility(self):
        compact = self.context('hierarchy'); legacy = self.context('hierarchy',False)
        old = self.selected(legacy,'hierarchy',4,'demo/left')
        plan = compact.build(old['derivation'])
        self.assertEqual(plan['outputs'],old['outputs'])
        self.assertTrue(plan['region']['cells'])
        self.assertTrue(all(c['instance']=='demo/left' for c in plan['region']['cells']))
        self.assertEqual(compact.validate(old),old)
        self.assertEqual(legacy.validate(plan),plan)
        wrong = self.context('shared')
        with self.assertRaisesRegex(TraceError,'baseline'): wrong.validate(plan)

    def test_compact_plan_explicit_export_still_preserves_selected_circuit(self):
        context = self.context('shared')
        plan = context.build({'kind':'union','parents':[self.selected(context,'shared',n) for n in (3,4)]})
        path = self.base/'export-plan.json'; path.write_text(json.dumps(plan))
        result = extract(parser().parse_args(['extract',str(context.directory),'--contract',str(path),
                                              '--out',str(self.base/'selected.v')]))
        self.assertEqual(result['selected_cell_count'],len(plan['region']['cells']))
        self.assertTrue(all(result['validation'].values()))
        exported = json.loads((self.base/'selected.provenance.json').read_text())
        self.assertEqual(exported['boundary'],plan['boundary'])
        self.assertEqual(exported['contract_id'],plan['contract_id'])
        self.assertTrue(exported['abc_associations'])


if __name__ == '__main__': unittest.main()
