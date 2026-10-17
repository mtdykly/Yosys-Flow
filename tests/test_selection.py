"""Default synth selection acceptance: expected objects follow RTL outputs,
not the recorder's reported origin union. Precision remains explicit.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import TraceError, unescape
from yosys_flow.runner import run, query, load_run
from yosys_flow.extract import extract
from yosys_flow.native import NativeTrace


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built')
class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='selection-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def experiment(self, rtl):
        self.source, self.out = self.base/'source.v', self.base/'run'
        self.source.write_text(rtl)
        run(parser().parse_args(['run', '--source', str(self.source), '--top', 'demo', '--out', str(self.out), '--native-yosys', str(NATIVE)]))
        self.manifest, self.trace, self.validation = load_run(self.out)
        self.module = json.loads((self.out/'after.json').read_text())['modules']['demo']
        self.assertTrue(all(self.validation['checks'].values()))

    def select(self, line):
        return query(parser().parse_args(['query', str(self.out), '--source', str(self.source), '--line', str(line)]))

    def export(self, line):
        path = self.base/f'slice-{line}.v'
        result = extract(parser().parse_args(['extract', str(self.out), '--source', str(self.source), '--line', str(line), '--out', str(path)]))
        self.assertTrue(result['validation']['exported_verilog_equivalent_to_cell_slice'])
        return json.loads(path.with_suffix('.provenance.json').read_text())

    def drivers(self, port):
        bits = set(self.module['ports'][port]['bits'])
        return {n for n,c in self.module['cells'].items() if any(bits.intersection(v) for p,v in c['connections'].items() if c['port_directions'][p]=='output')}

    def names(self, result):
        return {unescape(c['name']) for c in result['final_cells']}

    def test_independent_outputs_are_separated_after_default_abc(self):
        self.experiment('''module demo(input a,b,c,d, output y,z);
assign y=a&b;
assign z=c^d;
endmodule
''')
        for line,port,other in ((2,'y',3),(3,'z',2)):
            result=self.select(line)
            self.assertEqual(self.names(result), self.drivers(port))
            self.assertEqual(len(result['final_cells']),1)
            self.assertEqual({c['precision'] for c in result['final_cells']},{'abc_internal_cut'})
            self.assertFalse(result['end_to_end_exact'])
            self.assertNotIn(other, {s['line'] for c in result['final_cells'] for s in c['all_origins']})
            self.assertTrue(self.export(line)['abc_associations'])

    def test_same_inputs_do_not_collapse_distinct_output_candidates(self):
        self.experiment('''module demo(input a,b, output y,z);
assign y=a&b;
assign z=a^b;
endmodule
''')
        self.assertEqual(self.names(self.select(2)),self.drivers('y'))
        self.assertEqual(self.names(self.select(3)),self.drivers('z'))
        self.assertTrue(self.names(self.select(2)).isdisjoint(self.names(self.select(3))))

    def test_shared_expression_still_reports_both_sources(self):
        self.experiment('''module demo(input a,b, output y,z);
assign y=a&b;
assign z=a&b;
endmodule
''')
        a,b=self.select(2),self.select(3)
        self.assertEqual(self.names(a),self.names(b))
        self.assertEqual(len(a['final_cells']),1)
        self.assertTrue({2,3}<={s['line'] for s in a['final_cells'][0]['all_origins']})
        self.export(2)


    def test_reduce_import_has_actual_donor_evidence(self):
        self.experiment('''module demo(input [2:0] a,b, output y);
wire t;
assign t=|a;
assign y=|{t,b};
endmodule
''')
        self.assertTrue(any(r.get('operation')=='opt_reduce_import' for r in self.trace.records))
        self.assertTrue(self.select(3)['final_cells'])
        self.export(3)


    def output_cone(self, port):
        # Independent expectation for an entire, isolated combinational expression.
        drivers={bit: n for n,c in self.module['cells'].items() for p,bits in c['connections'].items()
                 if c['port_directions'][p]=='output' for bit in bits if isinstance(bit,int)}
        result,stack=set(),list(self.module['ports'][port]['bits'])
        while stack:
            name=drivers.get(stack.pop())
            if name is None or name in result: continue
            result.add(name)
            cell=self.module['cells'][name]
            stack.extend(b for p,bits in cell['connections'].items() if cell['port_directions'][p]=='input' for b in bits)
        return result

    def test_independent_wide_arithmetic_excludes_other_adder(self):
        self.experiment("""module demo(input [3:0] a,b,c,d, output [4:0] y,z);
assign y=a+b;
assign z=c+d;
endmodule
""")
        for line,port in ((2,'y'),(3,'z')):
            self.assertEqual(self.names(self.select(line)),self.output_cone(port))
            self.export(line)
        self.assertTrue(self.names(self.select(2)).isdisjoint(self.names(self.select(3))))

    def test_abc_rewrite_preserves_expression_boundary(self):
        self.experiment("""module demo(input a,b,c,d,e, output y,z);
assign y=(a&b)|(a&c);
assign z=d^e;
endmodule
""")
        self.assertEqual(self.names(self.select(2)),self.output_cone('y'))
        self.assertEqual(self.names(self.select(3)),self.drivers('z'))
        self.assertFalse(self.select(2)['end_to_end_exact'])
        self.export(2)


    def test_muxtree_pruning_has_replacement_evidence(self):
        self.experiment("""module demo(input s,a,b,c, output y);
wire t;
assign t=s?a:b;
assign y=s?t:c;
endmodule
""")
        self.assertTrue(any(r.get('operation')=='opt_muxtree_prune' for r in self.trace.records))
        result=self.select(3)
        self.assertFalse(result['final_cells'])
        self.assertEqual(result['resolution'],'connections_found')
        self.assertEqual({b for r in result['final_connections'] for b in r['rhs_bits']},set(self.module['ports']['a']['bits']))
        self.assertTrue(any(r['reason']=='opt_muxtree_prune' for r in result['retired_cells']))
        self.export(3)

    def test_capture_records_remain_immutable_after_refinement(self):
        self.experiment("""module demo(input a,b,c,d, output y,z);
assign y=a&b;
assign z=c^d;
endmodule
""")
        self.assertEqual(self.trace.records, [json.loads(line) for line in (self.out/'yosys.jsonl').read_text().splitlines()])
        # Removing optional new correspondence simulates older v2 records.
        rows=copy.deepcopy(self.trace.records)
        for row in rows:
            if row['record_type']=='abc_region': row.pop('input_nodes')
        path=self.base/'old-region.jsonl'
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        old=NativeTrace(path)
        self.assertFalse(old.abc_associations)
        self.assertEqual({old.objects[r]['precision'] for r in old.final},{'abc_region_candidate'})


    def test_abc_input_and_import_correspondence_tampering_rejected(self):
        self.experiment('''module demo(input a,b,c,d, output y,z);
assign y=a&b;
assign z=c^d;
endmodule
''')
        for kind in ('input','output'):
            rows=copy.deepcopy(self.trace.records)
            if kind=='input':
                region=next(r for r in rows if r['record_type']=='abc_region')
                next(n for n in region['input_nodes'] if n['cell'])['fanins']=[]
            else:
                node=next(r for r in rows if r['record_type']=='abc_import_node')
                node['ports']['\\A'][0]['wire']='\\forged'
            path=self.base/f'forged-{kind}.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with self.assertRaises(TraceError):
                trace=NativeTrace(path)
                trace.compare_abc_artifacts(self.out)
