"""Selection and negative evidence tests for actual ABC equivalence groups."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import ROOT, TraceError, unescape
from yosys_flow.runner import run, query, load_run
from yosys_flow.extract import extract
from yosys_flow.abc_internal import Network


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built; run make test')
class FactoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='abc-factoring-')
        cls.base = Path(cls.temp.name)
        cls.fixtures = {}
        cases = {
            'base': (ROOT/'examples/factoring.v').read_text(),
            'boundary': '''module demo(input s,t,b,c,d,e,output a,y,z,independent);
assign a=s&t;
assign y=(a&b)|(a&c);
assign z=a&(b|c);
assign independent=d^e;
endmodule
''',
            'complement': '''module demo(input a,b,c,d,e,output y,z,independent);
assign y=(a&b)|(a&c);
assign z=~(a&(b|c));
assign independent=d^e;
endmodule
''',
            'zero': '''module demo(input a,b,output y);
assign y=(a|b)&(~a|b)&~b;
endmodule
''',
            'one': '''module demo(input a,b,output y);
assign y=~((a|b)&(~a|b)&~b);
endmodule
''',
        }
        for name,rtl in cases.items():
            source,out = cls.base/(name+'.v'), cls.base/name
            source.write_text(rtl)
            result=run(parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out),'--native-yosys',str(NATIVE)]))
            if not all(result['checks'].values()): raise AssertionError(result)
            cls.fixtures[name]=(source,out)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def select(self,case,line):
        source,out=self.fixtures[case]
        return query(parser().parse_args(['query',str(out),'--source',str(source),'--line',str(line)]))

    def module(self,case):
        return json.loads((self.fixtures[case][1]/'after.json').read_text())['modules']['demo']

    def driver(self,module,port):
        bits=set(module['ports'][port]['bits'])
        return {n for n,c in module['cells'].items() if bits.intersection(c['connections'].get('Y',[]))}

    def names(self,result): return {unescape(c['name']) for c in result['final_cells']}

    def test_both_spellings_select_shared_and_or_and_exclude_other_output(self):
        module=self.module('base')
        independent=self.driver(module,'independent')
        expected=set(module['cells'])-independent
        self.assertEqual({module['cells'][n]['type'] for n in expected},{'$_AND_','$_OR_'})
        self.assertEqual(len(expected),2)
        for line in (2,3):
            result=self.select('base',line)
            self.assertEqual(self.names(result),expected)
            self.assertEqual({c['precision'] for c in result['final_cells']},{'abc_internal_joint_cut'})
            self.assertEqual({s['line'] for c in result['final_cells'] for s in c['all_origins']},{2,3})
            self.assertFalse(result['candidate_cell_refs'])
            self.assertFalse(result['end_to_end_exact'])
            for evidence in result['abc_associations']:
                self.assertFalse(evidence['per_gate_source_exact'])
                for group in evidence['joint_groups']:
                    self.assertEqual(group['basis'],'recorded_fraig_equivalence')
                    lineage=group['entry_lineage']
                    self.assertEqual(len(lineage['paths']),2)
                    self.assertTrue(all(len(path)==4 for path in lineage['paths']))
                    self.assertTrue(lineage['equivalence_event'])
                    self.assertTrue(lineage['sweep_copy_event'])
        self.assertEqual(self.names(self.select('base',4)),independent)

    def test_recorded_output_clone_and_post_abc_merge_keep_evidence(self):
        _,out=self.fixtures['base']
        manifest,trace,validation=load_run(out)
        self.assertEqual(manifest['configuration']['passes'],['synth -top demo'])
        self.assertTrue(validation['checks']['stock_flow_exact_match'])
        summary=trace.abc_internal[0]
        self.assertEqual(summary['schema'],3)
        self.assertEqual(summary['verified_entry_copies'],4)
        self.assertEqual(summary['entry_copies'],4)
        self.assertGreater(summary['mapped_clones'],0)
        self.assertTrue(summary['entry_lineage']['available'])
        self.assertEqual(summary['entry_lineage']['scope'],'entry_to_fraig')
        self.assertGreater(summary['entry_lineage']['merged_root_pairs'],0)
        self.assertFalse(summary['unproved_import_nodes'])
        self.assertFalse(summary['complete_rewrite_history'])
        captured=copy.deepcopy(trace.records); associations=copy.deepcopy(trace.abc_associations)
        trace.compare_abc_artifacts(out)
        self.assertEqual(trace.records,captured)
        self.assertEqual(trace.abc_associations,associations)

    def test_common_retained_boundary_is_not_pulled_into_equivalence_group(self):
        module=self.module('boundary')
        upstream=self.driver(module,'a'); unrelated=self.driver(module,'independent')
        expected=set(module['cells'])-upstream-unrelated
        self.assertEqual(len(upstream),1)
        self.assertEqual(len(expected),2)
        self.assertEqual(self.names(self.select('boundary',2)),upstream)
        for line in (3,4):
            self.assertEqual(self.names(self.select('boundary',line)),expected)
            self.assertEqual({s['line'] for c in self.select('boundary',line)['final_cells'] for s in c['all_origins']},{3,4})
        self.assertEqual(self.names(self.select('boundary',5)),unrelated)

    def test_complemented_equivalence_retains_polarity_and_excludes_unrelated(self):
        module=self.module('complement')
        expected=set(module['cells'])-self.driver(module,'independent')
        self.assertEqual({module['cells'][n]['type'] for n in expected},{'$_AND_','$_NAND_','$_OR_'})
        result=self.select('complement',2)
        self.assertEqual(self.names(result),expected)
        self.assertEqual(self.names(self.select('complement',3)),expected)
        groups=[g for e in result['abc_associations'] for g in e['joint_groups']]
        self.assertTrue(any(g['entry_lineage']['inverted'] for g in groups))
        self.assertTrue(any(m['output_inverted'] for g in groups for m in g['matches']))

    def test_export_and_reverse_query_preserve_shared_sources_and_boundary(self):
        for case,line,sources in (('base',2,{2,3}),('boundary',3,{3,4})):
            with self.subTest(case=case):
                source,out=self.fixtures[case];target=self.base/(case+'-selected.v')
                result=extract(parser().parse_args(['extract',str(out),'--source',str(source),'--line',str(line),'--out',str(target)]))
                self.assertEqual(result['selected_cell_count'],2)
                self.assertTrue(all(result['validation'].values()))
                report=json.loads(target.with_suffix('.provenance.json').read_text())
                module=self.module(case)
                inputs={b for p in ('a','b','c') for b in module['ports'][p]['bits']}
                self.assertEqual({r['original_bit'] for r in report['boundary'] if r['direction']=='input'}, inputs)
                self.assertEqual({r['original_bit'] for r in report['boundary'] if r['direction']=='output'},set(module['ports']['y']['bits']))
                for ref in report['joint_cut_proven_refs']:
                    reverse=query(parser().parse_args(['query',str(out),'--object',ref]))
                    self.assertEqual({s['line'] for s in reverse['sources']},sources)

    def test_corrupt_entry_chain_clone_and_adoption_are_rejected(self):
        _,out=self.fixtures['base'];_,trace,_=load_run(out)
        filename=trace.regions[0]['artifacts']['input.blif.flow.jsonl']
        original=[json.loads(l) for l in (out/filename).read_text().splitlines()]
        changes=[]
        rows=copy.deepcopy(original);next(r for r in rows if r['kind']=='entry_copy')['links'][0][1]^=1;changes.append(rows)
        rows=copy.deepcopy(original);next(r for r in rows if r['kind']=='entry_copy')['links'].pop();changes.append(rows)
        rows=copy.deepcopy(original);r=next(r for r in rows if r.get('reason')=='entry_ntk_to_aig');r['before']=r['after'];changes.append(rows)
        rows=copy.deepcopy(original);next(r for r in rows if r.get('reason')=='fraig_sat_merge')['entry_snapshot']=1;changes.append(rows)
        rows=copy.deepcopy(original);r=next(r for r in rows if r['kind']=='mapped_clone');r['old']=r['new'];changes.append(rows)
        rows=copy.deepcopy(original);next(r for r in rows if r['kind']=='gia_to_ntk')['clones']=[];changes.append(rows)
        rows=copy.deepcopy(original);next(r for r in rows if r['kind']=='command_end' and r['entry_copies'])['entry_copies']+=1;changes.append(rows)
        # Choose a functionally equivalent but NOT adopted temporary literal.
        # Ordinary truth-table checking passes; replay of the actual merge must fail.
        rows=copy.deepcopy(original)
        event=next(r for r in rows if r.get('reason')=='fraig_sat_merge')
        sweep=next(r for r in rows if r.get('reason')=='fraig_sweep_copy')
        after=Network(next(r for r in rows if r['seq']==sweep['after']))
        values,mask=after.truth()
        link=next(link for link in sweep['links'] if link[0]==event['old'])
        expected=values[link[1]//2] ^ (mask if link[1]&1 else 0)
        choices=[2*i+phase for i in after.nodes if i not in after.pis+after.pos for phase in (0,1)
                 if (values[i] ^ (mask if phase else 0))==expected and 2*i+phase!=link[1]]
        self.assertTrue(choices)
        link[1]=choices[0];changes.append(rows)
        for i,rows in enumerate(changes):
            with self.subTest(mutation=i):
                target=self.base/f'corrupt-{i}';shutil.copytree(out,target)
                (target/filename).write_text(''.join(json.dumps(r)+'\n' for r in rows))
                with self.assertRaises(TraceError): trace.compare_abc_artifacts(target)
                with self.assertRaisesRegex(TraceError,'artifact hash mismatch'):load_run(target)

    def test_abc_recreated_constant_outputs_validate_and_export_conservatively(self):
        # MakeSimpleCos deletes the original vCopyLits constant target. Its stale
        # ID must not be emitted as a live copy or used to invent a proof.
        for case,value in (('zero','0'),('one','1')):
            with self.subTest(case=case):
                source,out=self.fixtures[case]
                module=self.module(case)
                self.assertFalse(module['cells'])
                self.assertEqual(module['ports']['y']['bits'],[value])
                result=self.select(case,2)
                self.assertFalse(result['final_cells'])
                self.assertFalse(result['cut_proven_refs'])
                self.assertFalse(result['joint_cut_proven_refs'])
                self.assertTrue(result['final_connections'])
                self.assertTrue(any(c['precision']=='abc_output_cone_candidate' for c in result['final_connections']))
                self.assertFalse(result['end_to_end_exact'])
                _,trace,_=load_run(out)
                self.assertTrue(trace.regions[0]['inputs'])  # It really passed through ABC.
                target=self.base/(case+'-selected.v')
                exported=extract(parser().parse_args(['extract',str(out),'--source',str(source),'--line','2','--out',str(target)]))
                self.assertEqual(exported['selected_cell_count'],0)
                self.assertEqual(exported['input_bits'],0)
                self.assertTrue(all(exported['validation'].values()))
                ports=json.loads(target.with_suffix('.json').read_text())['modules'][exported['module']]['ports']
                self.assertEqual({bit for port in ports.values() for bit in port['bits']},{value})


class EquivalenceAnchorTests(unittest.TestCase):
    def test_same_function_without_actual_merge_does_not_create_group(self):
        from yosys_flow.abc_joint import equivalence_cuts
        pis=[{'id':i,'op':'pi','fanins':[]} for i in range(3)]
        first=Network({'seq':1,'nodes':[*pis,
            {'id':3,'op':'and','fanins':[0,2]}, {'id':4,'op':'and','fanins':[0,4]},
            {'id':5,'op':'sop','fanins':[6,8],'sop':'1- 1\n-1 1\n'},
            {'id':6,'op':'sop','fanins':[2,4],'sop':'1- 1\n-1 1\n'},
            {'id':7,'op':'and','fanins':[0,12]}]})
        final=Network({'seq':2,'nodes':[*pis,
            {'id':3,'op':'sop','fanins':[2,4],'sop':'1- 1\n-1 1\n','gate':'OR'},
            {'id':4,'op':'and','fanins':[0,6],'gate':'AND'}]})
        iv,mask=first.truth();fv,_=final.truth()
        self.assertEqual(iv[5],iv[7]);self.assertEqual(iv[5],fv[4])
        proof=equivalence_cuts(first,final,{i:str(i) for i in range(3,8)},iv,fv,mask,
                              {'or':3,'and':4},{},{'or':[10],'and':[11]},lambda r:[1],{'pairs':[]})
        self.assertEqual(proof,{})
