"""Independent selection expectations and internal ABC evidence regressions."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import ROOT, PROVENANCE_ROOT, TraceError, unescape
from yosys_flow.runner import run, query, load_run
from yosys_flow.extract import extract
from yosys_flow.abc_internal import DEFAULT_COMMANDS


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built; run make test')
class AbcInternalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='abc-internal-')
        cls.base = Path(cls.temp.name)
        cls.fixtures = {}
        sources = {
            'chain': (PROVENANCE_ROOT/'examples/chain.v').read_text(),
            'shared': '''module demo(input a,b,c,d,output y,z);
wire t;
assign t=a&b;
assign y=t^c;
assign z=t^d;
endmodule
''',
            'multilevel': '''module demo(input a,b,c,d,output y);
wire t,u;
assign t=a&b;
assign u=t^c;
assign y=u^d;
endmodule
''',
            'independent': (PROVENANCE_ROOT/'examples/multilevel.v').read_text(),
            'equivalent': '''module demo(input a,b,c,output y,z);
assign y=(a&b)|(a&c);
assign z=a&(b|c);
endmodule
''',
            'sharedjoint': '''module demo(input a,b,c,d,e,output y,z);
wire t,u;
assign t=a&b;
assign u=t^c;
assign y=u^d;
assign z=t^e;
endmodule
''',
            'nested': '''module demo(input a,b,c,d,s,r,output y,z);
wire t,u;
assign t=a&b;
assign u=t^c;
assign y=s ? (r ? u^d : t) : c;
assign z=a|d;
endmodule
''',
            'large': '''module demo(input [10:0] a,output y);
assign y=^a;
endmodule
''',
        }
        for name, rtl in sources.items():
            source, out = cls.base/(name+'.v'), cls.base/name
            source.write_text(rtl)
            result=run(parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out),'--native-yosys',str(NATIVE)]))
            if not all(result['checks'].values()): raise AssertionError(result)
            cls.fixtures[name]=(source,out)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def select(self, case, line):
        source,out=self.fixtures[case]
        return query(parser().parse_args(['query',str(out),'--source',str(source),'--line',str(line)]))

    def module(self, case):
        return json.loads((self.fixtures[case][1]/'after.json').read_text())['modules']['demo']

    def driver(self, module, port):
        bits=set(module['ports'][port]['bits'])
        return {name for name,c in module['cells'].items() if any(bits.intersection(v) for p,v in c['connections'].items() if c['port_directions'][p]=='output')}

    def names(self, result):
        return {unescape(c['name']) for c in result['final_cells']}

    def test_same_output_chain_selects_only_its_local_gate_and_exports_cut(self):
        module=self.module('chain')
        a,b=self.select('chain',3),self.select('chain',4)
        expected_b=self.driver(module,'y')
        self.assertEqual(len(module['cells']),2)
        self.assertEqual(self.names(b),expected_b)
        self.assertEqual(self.names(a),set(module['cells'])-expected_b)
        for result,own,other in ((a,3,4),(b,4,3)):
            self.assertEqual({c['precision'] for c in result['final_cells']},{'abc_internal_cut'})
            self.assertEqual({s['line'] for c in result['final_cells'] for s in c['all_origins']},{own})
            self.assertTrue(result['cut_proven_refs'])
            self.assertFalse(result['end_to_end_exact'])
            self.assertTrue(result['abc_associations'])
            for evidence in result['abc_associations']:
                self.assertTrue(evidence['matches'])
                self.assertTrue(evidence['conversion_events'])
                self.assertFalse(evidence['complete_rewrite_history'])
        source,out=self.fixtures['chain']
        target=self.base/'y-selected.v'
        result=extract(parser().parse_args(['extract',str(out),'--source',str(source),'--line','4','--out',str(target)]))
        self.assertEqual(result['selected_cell_count'],1)
        self.assertTrue(all(result['validation'].values()))
        report=json.loads(target.with_suffix('.provenance.json').read_text())
        cut_inputs={r['original_bit'] for r in report['boundary'] if r['direction']=='input'}
        upstream=module['cells'][next(iter(self.names(a)))]
        upstream_output=next(b for p,d in upstream['port_directions'].items() if d=='output' for b in upstream['connections'][p])
        self.assertEqual(cut_inputs,{upstream_output,*module['ports']['c']['bits']})
        self.assertTrue(report['abc_internal'])

    def test_shared_intermediate_is_not_attributed_to_its_consumers(self):
        module=self.module('shared')
        y,z=self.driver(module,'y'),self.driver(module,'z')
        self.assertEqual(self.names(self.select('shared',4)),y)
        self.assertEqual(self.names(self.select('shared',5)),z)
        self.assertEqual(self.names(self.select('shared',3)),set(module['cells'])-y-z)
        self.assertTrue(y.isdisjoint(z))

    def test_internal_actual_replacements_mapping_and_default_commands(self):
        _,out=self.fixtures['shared']
        manifest,trace,validation=load_run(out)
        self.assertEqual(manifest['configuration']['passes'],['synth -top demo'])
        summary=trace.abc_internal[0]
        self.assertEqual(summary['commands'],DEFAULT_COMMANDS)
        self.assertGreater(summary['aig_replacements'],0)
        self.assertEqual(summary['verified_aig_replacements'],summary['aig_replacements'])
        self.assertEqual(summary['proved_import_nodes'],len(self.module('shared')['cells']))
        self.assertEqual(summary['mapped_cuts'],3)
        self.assertFalse(summary['complete_rewrite_history'])
        self.assertTrue(validation['checks']['stock_flow_exact_match'])
        self.assertEqual(trace.records,[json.loads(l) for l in (out/'yosys.jsonl').read_text().splitlines()])

    def test_reassociated_multilevel_has_joint_proof_and_excludes_upstream(self):
        for case in ('multilevel', 'independent', 'sharedjoint'):
            with self.subTest(case=case):
                module = self.module(case)
                # Independently recognize the retained a&b gate by its PRIMARY
                # inputs. The remaining y-cone gates form the rewritten XOR pair.
                ab = set(module['ports']['a']['bits'] + module['ports']['b']['bits'])
                upstream = {n for n,c in module['cells'].items()
                            if {b for p,bs in c['connections'].items() if c['port_directions'][p]=='input' for b in bs} == ab}
                self.assertEqual(len(upstream), 1)
                unrelated = self.driver(module, 'z') if 'z' in module['ports'] else set()
                expected = set(module['cells']) - upstream - unrelated
                self.assertEqual(len(expected), 2)
                self.assertEqual(self.names(self.select(case, 3)), upstream)
                for line in (4, 5):
                    result = self.select(case, line)
                    self.assertEqual(self.names(result), expected)
                    self.assertEqual({c['precision'] for c in result['final_cells']}, {'abc_internal_joint_cut'})
                    self.assertEqual({s['line'] for c in result['final_cells'] for s in c['all_origins']}, {4, 5})
                    self.assertEqual(set(result['joint_cut_proven_refs']), {c['ref'] for c in result['final_cells']})
                    self.assertFalse(result['cut_proven_refs'])
                    self.assertFalse(result['candidate_cell_refs'])
                    self.assertFalse(result['end_to_end_exact'])
                    for evidence in result['abc_associations']:
                        self.assertFalse(evidence['per_gate_source_exact'])
                        self.assertEqual(evidence['attribution_scope'], 'joint_group')
                        self.assertTrue(evidence['conversion_events'])
                        for group in evidence['joint_groups']:
                            self.assertEqual(len(group['mapped_nodes']), 2)
                            self.assertTrue(group['boundary_functions_independent'])
                            self.assertEqual(len(group['mapping_events']), 2)
                if unrelated:
                    self.assertEqual(self.names(self.select(case, 6)), unrelated)

    def test_joint_export_preserves_retained_boundary_and_shared_origins(self):
        source, out = self.fixtures['independent']
        target = self.base/'joint-selected.v'
        result = extract(parser().parse_args(['extract',str(out),'--source',str(source),'--line','4','--out',str(target)]))
        self.assertEqual(result['selected_cell_count'], 2)
        self.assertTrue(all(result['validation'].values()))
        report = json.loads(target.with_suffix('.provenance.json').read_text())
        self.assertEqual(len(report['joint_cut_proven_refs']), 2)
        module = self.module('independent')
        upstream = module['cells'][next(iter(self.names(self.select('independent',3))))]
        upstream_bit = upstream['connections']['Y'][0]
        self.assertEqual({r['original_bit'] for r in report['boundary'] if r['direction']=='input'},
                         {upstream_bit, *module['ports']['c']['bits'], *module['ports']['d']['bits']})
        self.assertEqual({r['original_bit'] for r in report['boundary'] if r['direction']=='output'}, set(module['ports']['y']['bits']))
        reverse = query(parser().parse_args(['query',str(out),'--object',report['joint_cut_proven_refs'][0]]))
        self.assertEqual({s['line'] for s in reverse['sources']}, {4,5})

    def test_nested_condition_excludes_upstream_and_independent_output(self):
        module = self.module('nested')
        unrelated = self.driver(module, 'z')
        outer_mux = self.driver(module, 'y')
        self.assertEqual({module['cells'][n]['type'] for n in outer_mux}, {'$_MUX_'})
        ab = set(module['ports']['a']['bits'] + module['ports']['b']['bits'])
        upstream = {n for n,c in module['cells'].items() if
                    {b for p,bs in c['connections'].items() if c['port_directions'][p]=='input' for b in bs} == ab}
        inner = set(module['cells']) - upstream - unrelated - outer_mux
        self.assertEqual(len(inner), 3)
        self.assertEqual(self.names(self.select('nested',3)), upstream)
        self.assertEqual(self.names(self.select('nested',4)), inner)
        self.assertEqual(self.names(self.select('nested',5)), inner | outer_mux)
        self.assertEqual(self.names(self.select('nested',6)), unrelated)

    def test_actual_fraig_sat_and_dch_choices_are_validated(self):
        fraig = self.select('equivalent',2)['abc_internal'][0]
        self.assertGreater(fraig['equivalence_events']['fraig_sat_merge'], 0)
        # Entry lineage and both independent-boundary proofs are required;
        # recording an isolated SAT merge is insufficient.
        self.assertFalse(fraig['unproved_import_nodes'])
        self.assertFalse(self.select('equivalent',2)['candidate_cell_refs'])
        self.assertGreater(fraig['equivalence_groups'], 0)
        self.assertEqual(fraig['verified_entry_copies'], 4)
        dch = self.select('independent',4)['abc_internal'][0]
        for reason in ('dch_sat_merge','dch_structural_merge','dch_representative','dch_choice_link'):
            self.assertGreater(dch['equivalence_events'][reason], 0)
        for report in (fraig, dch):
            self.assertEqual(report['schema'], 3)
            self.assertEqual(report['verified_equivalences'], sum(report['equivalence_events'].values()))
            self.assertEqual(report['verified_history_copies'], report['history_copies'])
            self.assertEqual(report['history_copies'], 2)
            self.assertFalse(report['complete_rewrite_history'])

    def test_corrupt_equivalence_choice_copy_and_history_count_are_rejected(self):
        for case in ('equivalent','independent'):
            _,out = self.fixtures[case]
            _,trace,_ = load_run(out)
            filename = trace.regions[0]['artifacts']['input.blif.flow.jsonl']
            original = [json.loads(l) for l in (out/filename).read_text().splitlines()]
            changes = []
            rows = copy.deepcopy(original)
            next(r for r in rows if r['kind']=='proved_equivalence')['new_literal'] ^= 1
            changes.append(rows)
            rows = copy.deepcopy(original)
            next(r for r in rows if r['kind']=='history_copy')['links'][0][1] ^= 1
            changes.append(rows)
            rows = copy.deepcopy(original)
            next(r for r in rows if r['kind']=='history_copy')['links'].pop()
            changes.append(rows)
            rows = copy.deepcopy(original)
            next(r for r in rows if r['kind']=='command_end' and r['equivalences'])['equivalences'] += 1
            changes.append(rows)
            if case=='independent':
                rows = copy.deepcopy(original)
                next(r for r in rows if r.get('reason')=='dch_choice_link')['new_literal'] ^= 1
                changes.append(rows)
            for i,rows in enumerate(changes):
                with self.subTest(case=case,mutation=i):
                    target = self.base/f'history-corrupt-{case}-{i}'
                    shutil.copytree(out,target)
                    (target/filename).write_text(''.join(json.dumps(r)+'\n' for r in rows))
                    with self.assertRaises(TraceError): trace.compare_abc_artifacts(target)

    def test_large_input_region_checks_functions_without_claiming_every_cut(self):
        result=self.select('large',2)
        self.assertTrue(result['final_cells'])
        self.assertIsNone(result['abc_internal'][0]['fallback_reason'])
        self.assertEqual(result['abc_internal'][0]['function_proof']['backend'],'robdd')
        self.assertEqual(result['abc_internal'][0]['function_proof']['status'],'verified')
        self.assertFalse(result['end_to_end_exact'])

    def test_internal_trace_corruption_is_rejected(self):
        _,out=self.fixtures['chain']
        _,trace,_=load_run(out)
        filename=trace.regions[0]['artifacts']['input.blif.flow.jsonl']
        original=[json.loads(l) for l in (out/filename).read_text().splitlines()]
        mutations=[]
        mutations.append(original[:-1])
        rows=copy.deepcopy(original);next(r for r in rows if r['kind']=='mapped_cut')['root_literal']=999999;mutations.append(rows)
        rows=copy.deepcopy(original);next(r for r in rows if r['kind']=='gia_to_ntk')['links'][0][1]=999999;mutations.append(rows)
        rows=copy.deepcopy(original)
        first=next(r for r in rows if r['kind']=='network')
        next(n for n in first['nodes'] if n['op']=='sop')['sop']='00 1\n'
        mutations.append(rows)
        rows=copy.deepcopy(original)
        index=next(i for i,r in enumerate(rows) if r['kind']=='network' and r['command_id']>3)
        rows.pop(index)
        for i,r in enumerate(rows,1):r['seq']=i
        mutations.append(rows)
        for i,rows in enumerate(mutations):
            with self.subTest(case=i):
                target=self.base/f'corrupt-{i}'
                shutil.copytree(out,target)
                (target/filename).write_text(''.join(json.dumps(r)+'\n' for r in rows))
                with self.assertRaises(TraceError): trace.compare_abc_artifacts(target)
                with self.assertRaisesRegex(TraceError,'artifact hash mismatch'): load_run(target)

    def test_reverse_query_preserves_local_source(self):
        result=self.select('chain',4)
        _,out=self.fixtures['chain']
        reverse=query(parser().parse_args(['query',str(out),'--object',result['final_cells'][0]['ref']]))
        self.assertEqual({s['line'] for s in reverse['sources']},{4})
        self.assertTrue(reverse['cut_proven_refs'])

    def test_internal_proofs_do_not_mutate_or_accumulate_on_reload(self):
        for case in ('shared', 'independent'):
            _,out=self.fixtures[case]
            _,trace,_=load_run(out)
            original=copy.deepcopy(trace.records)
            associations=copy.deepcopy(trace.abc_associations)
            trace.compare_abc_artifacts(out)
            self.assertEqual(trace.records,original)
            self.assertEqual(trace.abc_associations,associations)


class CutSoundnessTests(unittest.TestCase):
    def test_joint_proof_requires_a_retained_internal_boundary(self):
        # (a^b)^c -> a^(b^c) is globally equivalent, but with no retained
        # internal boundary the joint prover must not upgrade the whole cone.
        from yosys_flow.abc_internal import Network
        from yosys_flow.abc_joint import joint_cuts
        pis = [{'id': i, 'op':'pi', 'fanins':[]} for i in range(3)]
        first = Network({'seq':1, 'nodes': [*pis,
            {'id':3, 'op':'xor', 'fanins':[0,2]},
            {'id':4, 'op':'xor', 'fanins':[6,4]}]})
        final = Network({'seq':2, 'nodes': [*pis,
            {'id':3, 'op':'xor', 'fanins':[2,4], 'gate':'XOR'},
            {'id':4, 'op':'xor', 'fanins':[0,6], 'gate':'XOR'}]})
        iv,mask = first.truth(); fv,_ = final.truth()
        self.assertEqual(iv[4],fv[4])
        result = joint_cuts(first, final, {3:'t',4:'y'}, iv, fv, mask,
                            {'new0':3,'new1':4}, {}, {'new0':[10],'new1':[11]}, lambda root: [1])
        self.assertEqual(result,{})

    def test_global_equivalence_does_not_prove_independent_boundary_equivalence(self):
        # x=a&b and z=a&~b can never both be true. XOR(x,z) and OR(x,z)
        # agree globally but disagree when x,z are independent exported inputs.
        from yosys_flow.abc_internal import Network, prove_cut
        net=Network({'pis':[0,1], 'nodes':[
            {'id':0,'op':'pi','fanins':[]}, {'id':1,'op':'pi','fanins':[]},
            {'id':2,'op':'and','fanins':[0,2]}, {'id':3,'op':'and','fanins':[0,3]},
            {'id':4,'op':'xor','fanins':[4,6]},
        ]})
        values,mask=net.truth()
        self.assertEqual(values[4],values[2]|values[3])
        gate={'op':'sop','sop':'1- 1\n-1 1\n'}
        self.assertIsNone(prove_cut(net,4,values,[values[2],values[3]],gate,0,mask))
        self.assertIsNone(prove_cut(net,4,values,[values[2],values[3]],None,0,mask,local_truth=0b1110))
