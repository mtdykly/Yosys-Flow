"""Independent selection tests for real sharing, absorption and bit reduction."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import TraceError
from yosys_flow.extract import extract
from yosys_flow.native import NativeTrace
from yosys_flow.optimization import propagate_gaps
from yosys_flow.runner import run, query, load_run

SHARE = '''module demo(input [3:0] a,b,c, input s,x,w, output [3:0] y, output z);
wire [3:0] p,q;
assign p = a+b;
assign q = a+c;
assign y = s ? p : q;
assign z = x & w;
endmodule
'''
INVERT = '''module demo(input s,a,b,c,d, output y,z);
wire n;
assign n = ~s;
assign y = n ? a : b;
assign z = c & d;
endmodule
'''


class GapPropagationTests(unittest.TestCase):
    def test_large_group_shares_gap_set_and_keeps_every_parent(self):
        count = 4096
        objects = {f'p{i}': {'direct_source_gaps': [i]} for i in range(count)}
        objects.update({f'c{i}': {} for i in range(count)})
        trace = SimpleNamespace(objects=objects, assignments={}, process_nodes={},
            object_gaps={i: {'ref': f'p{i}'} for i in range(count)},
            edges=[{'from': f'p{i}', 'to': 'group'} for i in range(count)]+
                  [{'from': 'group', 'to': f'c{i}'} for i in range(count)])
        for _ in range(2):  # Recompute after a proof refinement, without stale results.
            propagate_gaps(trace)
            shared = objects['c0']['source_gaps']
            self.assertEqual(shared, list(range(count)))
            self.assertTrue(all(objects[f'c{i}']['source_gaps'] is shared for i in range(count)))


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys required')
class OptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='optimization-evidence-')
        cls.root = Path(cls.temp.name)
        cls.archives = {}
        for name, rtl, backend in [('share', SHARE, 'sqlite'), ('invert', INVERT, 'gzip')]:
            source = cls.root/(name+'.v'); source.write_text(rtl)
            out = cls.root/name
            run(parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out),
                                     '--native-yosys',str(NATIVE),'--index-backend',backend]))
            cls.archives[name] = (source, out)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def select(self, name, line):
        source,out = self.archives[name]
        return query(parser().parse_args(['query',str(out),'--source',str(source),'--line',str(line)]))

    def raw(self, name, rtl, passes):
        p=self.root/name; p.mkdir(); source=p/'source.v'; source.write_text(rtl)
        script=f'flow_start {p}/yosys.jsonl test\nread_verilog -sv -defer {source}\nhierarchy -top demo\nflow_hierarchy\nproc -noopt\n{passes}\nwrite_json {p}/after.json\nflow_stop\n'
        result=subprocess.run([str(NATIVE),'-Q','-T','-p',script],env={**os.environ,'YOSYS_FLOW_ABC_TRACE':'1'},
                              stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout[-3000:])
        tr=NativeTrace(p/'yosys.jsonl',defer_refinement=True); tr.compare_netlist(p/'after.json')
        return p,tr

    def test_shared_arithmetic_retains_both_statements_and_control(self):
        selections=[self.select('share',i) for i in (3,4,5)]
        self.assertTrue(all(q['final_cells'] for q in selections))
        sets=[{c['ref'] for c in q['final_cells']} for q in selections]
        self.assertEqual(sets[0],sets[1]); self.assertEqual(sets[1],sets[2])
        unrelated={c['ref'] for c in self.select('share',6)['final_cells']}
        self.assertTrue(unrelated); self.assertFalse(sets[0]&unrelated)
        _,out=self.archives['share']; _,tr,v=load_run(out)
        self.assertTrue(v['checks']['stock_flow_exact_match'])
        self.assertFalse(any(not tr.objects[r]['origins'] for r in tr.final))
        self.assertTrue(any(e['record_type']=='optimization_rewrite' and e['rule']=='opt_share_operators' for e in selections[0]['events']))

    def test_select_inversion_follows_absorbed_operator(self):
        a,b=self.select('invert',3),self.select('invert',4)
        self.assertEqual(len(a['final_cells']),1)
        self.assertEqual({c['ref'] for c in a['final_cells']},{c['ref'] for c in b['final_cells']})
        self.assertFalse({c['ref'] for c in a['final_cells']} & {c['ref'] for c in self.select('invert',5)['final_cells']})
        self.assertFalse(a['end_to_end_exact'])

    def test_repaired_selection_exports_with_actual_boundary(self):
        source,out=self.archives['share']
        result=extract(parser().parse_args(['extract',str(out),'--source',str(source),'--line','4',
                                           '--out',str(self.root/'shared.v')]))
        self.assertTrue(result['validation']['exported_verilog_equivalent_to_cell_slice'])

    def test_double_inversion_and_complement_keep_consumed_premise(self):
        for name,expression,rule in [('double','~n','opt_expr_double_invert'),('complement','n & s','opt_expr_const_logic')]:
            with self.subTest(name=name):
                _,tr=self.raw(name,f'module demo(input s, output y);\nwire n;\nassign n=~s;\nassign y={expression};\nendmodule\n','opt_expr; opt_clean')
                events=[e for e in tr.events if e['record_type']=='optimization_rewrite' and e['rule']==rule]
                self.assertTrue(events)
                event=events[0]
                self.assertTrue(event['premises'])
                self.assertEqual({tr.sources[s]['line'] for s in event['origins']},{3,4})
                self.assertTrue(event['connections'])
                self.assertTrue(all({tr.sources[s]['line'] for s in tr.assignments[r]['origins']}=={3,4} for r in event['connections']))

    def test_constant_premise_uses_assignment_identity_not_constant_value(self):
        _,tr=self.raw('constant',"module demo(input a,b,output y,z);\nwire s;\nassign s=1'b0;\nassign y=s?a:b;\nassign z=1'b0;\nendmodule\n",'opt_expr; opt_clean')
        events=[e for e in tr.events if e['record_type']=='optimization_rewrite']
        self.assertTrue(any(e['premises'] for e in events))
        for e in events:
            lines={tr.sources[s]['line'] for s in e['origins']}
            self.assertEqual(lines,{3,4})
            self.assertNotIn(5,lines)

    def test_wreduce_records_output_padding_and_port_widths(self):
        for signed in (False,True):
            with self.subTest(signed=signed):
                _,tr=self.raw('width'+str(signed),f'module demo(input {"signed" if signed else ""} [3:0] a,b, output [7:0] y);\nassign y=a+b;\nendmodule\n','wreduce; opt_clean')
                e=next(e for e in tr.events if e['record_type']=='optimization_rewrite' and e['rule']=='wreduce_cell')
                self.assertEqual(len(e['port_changes'][0]['old_ports']['\\Y']),8)
                self.assertEqual(len(e['port_changes'][0]['new_ports']['\\Y']),5)
                self.assertTrue(e['connections'])
                padding=[tr.assignments[r] for r in e['connections']]
                self.assertEqual(sum(len(a['rhs']) for a in padding),3)
                if not signed: self.assertTrue(all(b=='0' for a in padding for b in a['rhs']))
                else: self.assertTrue(all(isinstance(b,dict) and b['offset']==4 for a in padding for b in a['rhs']))

    def test_wreduce_mux_removed_bits_have_replacement_connections(self):
        _,tr=self.raw('muxwidth',"module demo(input a,b,s,output [2:0] y);\nassign y=s?{2'b10,a}:{2'b10,b};\nendmodule\n",'wreduce; opt_clean')
        e=next(e for e in tr.events if e['record_type']=='optimization_rewrite')
        self.assertEqual(len(e['port_changes'][0]['new_ports']['\\Y']),1)
        self.assertTrue(e['connections'])

    def test_damaged_rewrite_metadata_is_rejected(self):
        _,out=self.archives['share']; rows=[json.loads(l) for l in (out/'yosys.jsonl').read_text().splitlines()]
        ordinal=next(i for i,r in enumerate(rows) if r['record_type']=='optimization_rewrite' and r['rule']=='opt_share_operators')
        mutations=[lambda r:r.update(outputs=[]),lambda r:r.update(removed=[]),
                   lambda r:r['port_changes'][0].update(new_ports={}),lambda r:r.update(rule='not_a_rule'),
                   lambda r:r.update(start_seq=r['seq']),lambda r:r['inputs'].pop()]
        for i,mutate in enumerate(mutations):
            with self.subTest(mutation=i):
                damaged=copy.deepcopy(rows); mutate(damaged[ordinal])
                with self.assertRaises(TraceError): NativeTrace(out/'yosys.jsonl',records=damaged,defer_refinement=True)

    def test_unknown_mutation_has_object_specific_gap(self):
        _,tr=self.raw('gap','module demo(input [3:0] a, output y);\nassign y=&(~a);\nendmodule\n','simplemap t:$not; opt_demorgan')
        self.assertIn('uncovered_pass:opt_demorgan',tr.gaps)
        self.assertTrue(tr.object_gaps)
        self.assertTrue(any(g['reason']=='object_change_without_rewrite_binding' for g in tr.object_gaps.values()))
        self.assertTrue(any(tr.objects[r].get('source_gaps') for r in tr.final))

    def test_old_logs_without_rewrite_feature_remain_readable(self):
        _,tr=self.raw('old','module demo(input a,b, output y);\nassign y=a&b;\nendmodule\n','opt_merge')
        rows=[dict(r) for r in tr.records]
        rows[0]['features']=[f for f in rows[0]['features'] if f!='optimization_rewrites_v1']
        reread=NativeTrace('/unused',records=rows,defer_refinement=True)
        self.assertEqual(tr.final,reread.final)

    def test_abc_function_proof_does_not_clear_upstream_gap(self):
        p,tr=self.raw('gapabc','module demo(input [3:0] a, output y);\nassign y=&(~a);\nendmodule\n',
                      'simplemap t:$not; opt_demorgan; synth -top demo')
        tr.compare_abc_artifacts(p)
        self.assertTrue(any(tr.objects[r]['precision']=='abc_internal_cut' for r in tr.final))
        self.assertTrue(any(tr.objects[r].get('source_gaps') for r in tr.final))

    def test_shared_sources_keep_instance_context_in_both_hierarchy_modes(self):
        rtl=SHARE.replace('module demo','module leaf')+'''module demo(input [3:0] a,b,c, input s,x,w, output [3:0] y0,y1, output z0,z1);
leaf first(a,b,c,s,x,w,y0,z0);
leaf second(c,b,a,s,x,w,y1,z1);
endmodule
'''
        source=self.root/'hierarchy.v'; source.write_text(rtl)
        for flatten in (False,True):
            with self.subTest(flatten=flatten):
                out=self.root/('hierarchy-'+str(flatten))
                run(parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out),
                    '--native-yosys',str(NATIVE),*(['--flatten'] if flatten else [])]))
                q=query(parser().parse_args(['query',str(out),'--source',str(source),'--line','4','--instance','demo/first']))
                self.assertTrue(q['final_cells'])
                self.assertTrue(all(any(s.get('instance')=='demo/first' for s in c['all_origins']) for c in q['final_cells']))
