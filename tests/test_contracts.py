"""Contracts keep record evidence primary; all outputs and union cuts are checked."""
import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.abc_cases import cases, expectation
from yosys_flow.boolean import BDD
from yosys_flow.cli import parser, main
from yosys_flow.common import ROOT, TraceError, digest, unescape
from yosys_flow.contracts import ContractContext, generate, validate, merge, seal
from yosys_flow.extract import extract
from yosys_flow.region_functions import gate_function, verify_outputs
from yosys_flow.runner import run


def value(function, inputs):
    nodes = {n['id']:n for n in function['nodes']}
    result = function['root']
    while result>1:
        node = nodes[result]
        result = node['high'] if inputs[node['input_port']] else node['low']
    return result


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built; run make test')
class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='region-contracts-')
        cls.base = Path(cls.temp.name)
        cls.fixtures = {}
        fixtures = [c for c in cases([32]) if c['name'] in {'chain','shared','multilevel','factoring','scale_32'}]
        fixtures += [{'name':'connections','rtl':'''module demo(input a, output y,z,w);
assign y=a;
assign z=1'b0;
assign w=1'b0;
endmodule
'''}, {'name':'arithmetic','rtl':'''module demo(input [3:0] a,b,c, output [4:0] y);
wire [4:0] t;
assign t=a+b;
assign y=t+c;
endmodule
'''}, {'name':'return_path','rtl':'''module demo(input a,b,c,d, output t,u,y);
assign t=a&b;
assign u=t^c;
assign y=u|d;
endmodule
'''}, {'name':'alias','rtl':'''module demo(input a,b, output y,z);
wire t;
assign t=a&b;
assign y=t;
assign z=t;
endmodule
'''}]
        for fixture in fixtures:
            name = fixture['name']
            source = cls.base/(name+'.v'); source.write_text(fixture['rtl'])
            out = cls.base/name
            result = run(parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out),
                                              '--native-yosys',str(NATIVE)]))
            if not all(result['checks'].values()): raise AssertionError(result)
            cls.fixtures[name] = (source,out)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def context(self,name): return ContractContext(self.fixtures[name][1])

    def selected(self,context,name,line,end=None,ref=None):
        selector = dict(source=str(self.fixtures[name][0]),line=line,end_line=end,column=None,end_column=None,object=None,instance=None)
        if ref: selector.update(source=None,line=None,end_line=None,object=ref)
        return context.build({'kind':'selection','selector':selector})

    def combine(self,context,*documents):
        document = context.build({'kind':'union','parents':list(documents)})
        self.assertEqual(context.validate(document),document)
        return document

    def test_chain_contract_certifies_boundary_function_with_polarity(self):
        context = self.context('chain')
        a = self.selected(context,'chain',3); b = self.selected(context,'chain',4)
        self.assertEqual(context.validate(b),b)
        self.assertEqual(len(b['region']['cells']),1)
        self.assertEqual(b['verification']['status'],'verified_old_region')
        self.assertFalse(b['provenance']['source_completeness_proven'])
        self.assertFalse(b['provenance']['complete_causal_history'])
        self.assertTrue(b['provenance']['supporting_edges'])
        self.assertTrue(b['provenance']['abc_associations'])
        self.assertEqual({s['line'] for s in b['provenance']['sources']},{4})
        upper = next(r['original_bit'] for r in a['boundary'] if r['direction']=='output')
        self.assertIn(upper,{r['original_bit'] for r in b['boundary'] if r['direction']=='input'})
        union = self.combine(context,a,b)
        output = union['outputs'][0]
        ports = {b['original_bit']:b['port'] for b in union['boundary'] if b['direction']=='input'}
        for a_bit,b_bit,c_bit in itertools.product((0,1),repeat=3):
            assignment = {ports[context.module['ports'][n]['bits'][0]]:bit for n,bit in zip('abc',(a_bit,b_bit,c_bit))}
            self.assertEqual(value(output['function'],assignment),(a_bit & b_bit)^c_bit)
        self.assertFalse(union['safe_for_incremental_reuse'])

    def test_shared_output_obligations_include_all_consumers_and_sources(self):
        context = self.context('factoring'); document = self.selected(context,'factoring',2)
        self.assertEqual(len(document['region']['cells']),2)
        self.assertEqual({u['name'] for u in document['outputs'][0]['external_users']},{'y','z'})
        self.assertEqual({s['line'] for s in document['other_recorded_sources']},{3})
        self.assertFalse(document['conditions']['functional_edit_assessed'])
        self.assertEqual(document['verification']['status'],'verified_old_region')

    def test_object_selection_explicitly_closes_recorded_joint_group(self):
        context = self.context('multilevel'); whole = self.selected(context,'multilevel',4)
        ref = whole['region']['cells'][0]['ref']
        one = self.selected(context,'multilevel',None,ref=ref)
        self.assertEqual(one['request']['cell_refs'],[ref])
        self.assertEqual(len(one['region']['cells']),2)
        self.assertEqual(len(one['expansion']['added_cell_refs']),1)
        self.assertFalse(one['expansion']['upstream_cone_added'])
        self.assertTrue(one['recorded_group_closure']['complete_for_recorded_groups'])
        self.assertEqual({s['line'] for s in one['provenance']['sources']},{4,5})

    def test_all_outputs_include_side_exit_to_unselected_logic(self):
        context = self.context('shared')
        upstream = self.selected(context,'shared',3); y = self.selected(context,'shared',4)
        union = self.combine(context,upstream,y)
        # t still drives z's unselected XOR, even though t->y became internal.
        self.assertEqual(len(union['outputs']),2)
        self.assertTrue(all(o['status']=='verified' for o in union['outputs']))
        users = [u for o in union['outputs'] for u in o['external_users']]
        self.assertTrue(any(u['kind']=='cell' for u in users))
        self.assertTrue(any(u['kind']=='module_port' and u['name']=='y' for u in users))
        self.assertTrue(union['verification']['all_output_functions']['all_outputs_verified'])

    def test_overlap_union_deduplicates_and_recomputes_obligations(self):
        context = self.context('shared')
        upstream = self.selected(context,'shared',3)
        left = self.combine(context,upstream,self.selected(context,'shared',4))
        right = self.combine(context,upstream,self.selected(context,'shared',5))
        union = self.combine(context,left,right)
        reverse = self.combine(context,right,left)
        self.assertEqual(union['contract_id'],reverse['contract_id'])
        expected = {c['ref'] for p in (left,right) for c in p['region']['cells']}
        self.assertEqual({c['ref'] for c in union['region']['cells']},expected)
        self.assertEqual(len(union['region']['cells']),3)
        self.assertEqual(len(union['union']['overlapping_cell_refs']),1)
        self.assertEqual({u['name'] for o in union['outputs'] for u in o['external_users']},{'y','z'})
        self.assertEqual(len(union['outputs']),2)
        self.assertTrue(any(r['disposition']=='internalized' for r in union['union']['parent_boundary_mapping']))
        for p in (left,right):
            self.assertTrue(set(p['provenance']['record_event_ids'])<=set(union['provenance']['record_event_ids']))

    def test_32_input_region_stays_a_single_target_gate(self):
        context = self.context('scale_32'); document = self.selected(context,'scale_32',3)
        case = next(c for c in cases([32]) if c['name']=='scale_32')
        self.assertEqual({unescape(c['name']) for c in document['region']['cells']},expectation(case,case['queries'][1],context.module))
        self.assertEqual(len(document['region']['cells']),1)
        self.assertEqual(document['verification']['status'],'verified_old_region')
        self.assertFalse(document['verification']['candidate_refs'])

    def test_connection_contracts_keep_constant_endpoint_identity_on_union(self):
        context = self.context('connections')
        a = self.selected(context,'connections',2); zero = self.selected(context,'connections',3)
        other = self.selected(context,'connections',4)
        self.assertFalse(zero['region']['cells'])
        self.assertEqual({u['name'] for o in zero['outputs'] for u in o['external_users']},{'z'})
        self.assertTrue(all(o['function']['root']==0 for o in zero['outputs']))
        union = self.combine(context,a,zero,other)
        self.assertEqual({u['name'] for o in union['outputs'] for u in o['external_users']},{'y','z','w'})
        self.assertTrue(union['verification']['all_output_functions']['all_outputs_verified'])
        self.assertTrue(all(r['disposition']=='retained_boundary' for r in union['union']['parent_boundary_mapping']))

    def test_candidate_source_is_not_upgraded_by_old_function_proof(self):
        context = self.context('arithmetic'); document = self.selected(context,'arithmetic',3)
        self.assertTrue(document['verification']['all_output_functions']['all_outputs_verified'])
        self.assertTrue(document['verification']['candidate_refs'])
        self.assertEqual(document['verification']['status'],'partial_old_region')
        self.assertFalse(document['end_to_end_exact'])
        self.assertFalse(document['safe_for_incremental_reuse'])

    def test_external_return_path_is_recomputed_when_intermediate_region_is_added(self):
        context = self.context('return_path')
        upstream = self.selected(context,'return_path',2)
        middle = self.selected(context,'return_path',3)
        downstream = self.selected(context,'return_path',4)
        separated = self.combine(context,upstream,downstream)
        self.assertTrue(separated['conditions']['outside_return_paths'])
        complete = self.combine(context,separated,middle)
        self.assertFalse(complete['conditions']['outside_return_paths'])
        self.assertEqual(len(complete['outputs']),3)
        self.assertTrue(complete['verification']['all_output_functions']['all_outputs_verified'])

    def test_connection_output_remains_bound_when_its_driver_is_added(self):
        context = self.context('alias')
        driver = self.selected(context,'alias',3); connection = self.selected(context,'alias',4)
        self.assertFalse(connection['region']['cells'])
        union = self.combine(context,driver,connection)
        mappings = [m for m in union['union']['parent_boundary_mapping']
                    if m['parent_id']==connection['contract_id'] and m['direction']=='output']
        self.assertTrue(mappings)
        self.assertTrue(all(m['disposition']=='retained_boundary' for m in mappings))
        self.assertEqual({u['name'] for o in union['outputs'] for u in o['external_users']},{'y','z'})

    def test_output_user_function_evidence_and_expansion_tampering_rejected_even_resealed(self):
        context = self.context('shared'); document = self.selected(context,'shared',3,4)
        changes = []
        bad = copy.deepcopy(document); bad['outputs'].pop(); changes.append(bad)
        bad = copy.deepcopy(document); bad['outputs'][0]['external_users'].pop(); changes.append(bad)
        bad = copy.deepcopy(document); bad['outputs'][0]['function']['root'] ^= 1; changes.append(bad)
        bad = copy.deepcopy(document); bad['boundary'].pop(0); changes.append(bad)
        bad = copy.deepcopy(document); bad['conditions']['input_binding']='inverted'; changes.append(bad)
        bad = copy.deepcopy(document); bad['provenance']['supporting_edges'].pop(); changes.append(bad)
        bad = copy.deepcopy(document); bad['expansion']['added_cell_refs']=['invented']; changes.append(bad)
        bad = copy.deepcopy(document); bad['safe_for_incremental_reuse']=True; changes.append(bad)
        for index,bad in enumerate(changes):
            with self.subTest(index=index), self.assertRaises(TraceError): context.validate(seal(bad))

    def test_union_rejects_other_baseline_invalid_parent_and_forged_boundary(self):
        context = self.context('chain'); a = self.selected(context,'chain',3); b = self.selected(context,'chain',4)
        other_context = self.context('shared'); other = self.selected(other_context,'shared',3)
        with self.assertRaisesRegex(TraceError,'baseline'): self.combine(context,a,other)
        bad = copy.deepcopy(b); bad['outputs'][0]['external_users']=[]; bad = seal(bad)
        with self.assertRaises(TraceError): self.combine(context,a,bad)
        union = self.combine(context,a,b)
        bad = copy.deepcopy(union); bad['boundary'].append(a['boundary'][-1]); bad = seal(bad)
        with self.assertRaises(TraceError): context.validate(bad)
        bad = copy.deepcopy(union); bad['union']['overlapping_cell_refs']=['invented']; bad = seal(bad)
        with self.assertRaises(TraceError): context.validate(bad)

    def test_offline_cli_generation_validation_union_and_no_overwrite(self):
        source,out = self.fixtures['chain']
        target = self.base/'cli'; target.mkdir()
        before = {p.name:digest(p) for p in out.iterdir() if p.is_file()}
        a,b,c = (target/name for name in ('a.json','b.json','union.json'))
        commands = [
            ['contract',str(out),'--source',str(source),'--line','3','--out',str(a)],
            ['contract',str(out),'--source',str(source),'--line','4','--out',str(b)],
            ['contract-merge',str(out),str(a),str(b),'--out',str(c)],
            ['contract-validate',str(out),str(c)]]
        with patch.dict('os.environ',{'PATH':''}), \
             patch('subprocess.run',side_effect=AssertionError('no synthesis')), \
             patch('subprocess.Popen',side_effect=AssertionError('no tools')):
            for command in commands:
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(command),0)
                self.assertFalse(json.loads(output.getvalue())['safe_for_incremental_reuse'])
            prior = c.read_bytes()
            with redirect_stderr(io.StringIO()): self.assertEqual(main(commands[2]),1)
            self.assertEqual(c.read_bytes(),prior)
        self.assertEqual(before,{p.name:digest(p) for p in out.iterdir() if p.is_file()})

    def test_merged_contract_export_preserves_union_and_checks_verilog(self):
        context = self.context('chain'); a = self.selected(context,'chain',3); b = self.selected(context,'chain',4)
        union = self.combine(context,a,b)
        path = self.base/'export-union.json'; path.write_text(json.dumps(union))
        out = self.base/'union-export.v'
        result = extract(parser().parse_args(['extract',str(context.directory),'--contract',str(path),'--out',str(out)]))
        self.assertEqual(result['selected_cell_count'],2)
        self.assertTrue(all(result['validation'].values()))
        report = json.loads(out.with_suffix('.provenance.json').read_text())
        self.assertEqual(report['contract_id'],union['contract_id'])
        self.assertEqual(report['boundary'],union['boundary'])

    def test_changed_input_does_not_change_historical_contract_but_flags_applicability(self):
        context = self.context('chain'); document = self.selected(context,'chain',3)
        path = self.base/'changed-input.json'; path.write_text(json.dumps(document))
        source,out = self.fixtures['chain']; original = source.read_text()
        try:
            source.write_text(original+'// changed\n')
            result = validate(parser().parse_args(['contract-validate',str(out),str(path)]))
            self.assertTrue(result['valid'])
            self.assertFalse(result['current_input_files_match_baseline'])
            self.assertIn(str(source),result['changed_or_missing_base_inputs'])
        finally: source.write_text(original)


class ContractFunctionTests(unittest.TestCase):
    def test_generic_gate_semantics_against_boolean_formulas(self):
        formulas = {
            'BUF':('A',lambda a:a), 'NOT':('A',lambda a:1-a),
            'AND':('AB',lambda a,b:a*b), 'NAND':('AB',lambda a,b:1-a*b),
            'OR':('AB',lambda a,b:int(bool(a or b))), 'NOR':('AB',lambda a,b:int(not(a or b))),
            'XOR':('AB',lambda a,b:int(a!=b)), 'XNOR':('AB',lambda a,b:int(a==b)),
            'ANDNOT':('AB',lambda a,b:a*(1-b)), 'ORNOT':('AB',lambda a,b:int(a or not b)),
            'MUX':('ABS',lambda a,b,s:b if s else a), 'NMUX':('ABS',lambda a,b,s:1-(b if s else a)),
            'AOI3':('ABC',lambda a,b,c:int(not((a and b) or c))),
            'OAI3':('ABC',lambda a,b,c:int(not((a or b) and c))),
            'AOI4':('ABCD',lambda a,b,c,d:int(not((a and b) or (c and d)))),
            'OAI4':('ABCD',lambda a,b,c,d:int(not((a or b) and (c or d))))}
        for name,(ports,formula) in formulas.items():
            for bits in itertools.product((0,1),repeat=len(ports)):
                self.assertEqual(gate_function('$_'+name+'_',dict(zip(ports,bits)),1),formula(*bits))
        for width in (4,8,16):
            for chosen in range(width):
                for hot in range(width):
                    ports = {p:int(i==hot) for i,p in enumerate('ABCDEFGHIJKLMNOP'[:width])}
                    ports.update({p:(chosen>>i)&1 for i,p in enumerate('STUV'[:width.bit_length()-1])})
                    self.assertEqual(gate_function(f'$_MUX{width}_',ports,1),int(chosen==hot))

    def test_side_output_mismatch_is_rejected_even_if_main_output_unchanged(self):
        cells = {'one':{'type':'$_AND_','parameters':{},'port_directions':{'A':'input','B':'input','Y':'output'},
                        'connections':{'A':[2],'B':[3],'Y':[4]}},
                 'two':{'type':'$_XOR_','parameters':{},'port_directions':{'A':'input','B':'input','Y':'output'},
                        'connections':{'A':[2],'B':[3],'Y':[5]}}}
        boundary = [{'port':str(i),'direction':'input' if i<4 else 'output','original_bit':i} for i in range(2,6)]
        actual = copy.deepcopy(cells); actual['two']['type']='$_OR_'
        with self.assertRaisesRegex(TraceError,'output function differs'): verify_outputs(cells,actual,boundary)

    def test_non_binary_and_budget_limits_are_not_proofs(self):
        result = verify_outputs({}, {}, [{'port':'z','direction':'output','original_bit':'x'}])
        self.assertFalse(result['all_outputs_verified'])
        self.assertEqual(result['outputs'][0]['reason'],'non_binary_constant:x')
        boundary = [{'port':str(i),'direction':'input','original_bit':i} for i in (2,3)]
        boundary += [{'port':'out','direction':'output','original_bit':2}]
        with patch('yosys_flow.region_functions.BDD',side_effect=lambda:BDD(node_limit=1)):
            result = verify_outputs({}, {}, boundary)
        self.assertFalse(result['all_outputs_verified'])
        self.assertEqual(result['outputs'][0]['reason'],'bdd_node_limit_exceeded')
