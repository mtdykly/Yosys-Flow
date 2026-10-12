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
            self.assertEqual({c['precision'] for c in result['final_cells']},{'abc_output_cone_candidate'})
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

    def test_enable_mux_absorption_preserves_separate_statement(self):
        self.experiment('''module demo(input clk,en,d, output reg q);
wire next;
assign next=en?d:q;
always @(posedge clk) q<=next;
endmodule
''')
        result=self.select(3)
        self.assertEqual(self.names(result),self.drivers('q'))
        self.assertTrue(any(r.get('operation')=='opt_dff_mux_absorb' for r in self.trace.records))
        self.assertTrue(self.export(3)['validation']['sequential_equivalence'])

    def assert_selected_q_bit(self, line, offset, other_line):
        result = self.select(line)
        bit = self.module['ports']['q']['bits'][offset]
        expected = {n for n, c in self.module['cells'].items() if c['connections'].get('Q') == [bit]}
        self.assertEqual(self.names(result), expected)
        self.assertEqual(len(result['final_cells']), 1)
        self.assertNotIn(other_line, {s['line'] for c in result['final_cells'] for s in c['all_origins']})
        self.assertTrue(result['ff_bit_maps'])
        self.assertTrue(all(r['applied'] for r in result['ff_bit_maps']))
        exported = self.export(line)
        self.assertTrue(exported['validation']['sequential_equivalence'])
        self.assertEqual(exported['ff_bit_maps'], result['ff_bit_maps'])

    def test_independent_enable_absorptions_keep_state_bits_separate(self):
        self.experiment("""module demo(input clk,e0,e1,a,b, output reg [1:0] q);
always @(posedge clk) begin
  if (e0) q[0]<=a;
  if (e1) q[1]<=b;
end
endmodule
""")
        self.assert_selected_q_bit(3, 0, 4)
        self.assert_selected_q_bit(4, 1, 3)

    def test_separate_mux_statements_follow_only_their_absorbing_bit(self):
        self.experiment("""module demo(input clk,e0,e1,a,b, output reg [1:0] q);
wire [1:0] next;
assign next[0]=e0?a:q[0];
assign next[1]=e1?b:q[1];
always @(posedge clk) q<=next;
endmodule
""")
        self.assert_selected_q_bit(3, 0, 4)
        self.assert_selected_q_bit(4, 1, 3)

    def test_independent_sync_reset_absorptions_keep_state_bits_separate(self):
        self.experiment("""module demo(input clk,r0,r1,a,b, output reg [1:0] q);
always @(posedge clk) begin
  if (r0) q[0]<=0; else q[0]<=a;
  if (r1) q[1]<=1; else q[1]<=b;
end
endmodule
""")
        self.assert_selected_q_bit(3, 0, 4)
        self.assert_selected_q_bit(4, 1, 3)

    def test_partial_enable_split_preserves_unabsorbed_bit(self):
        self.experiment("""module demo(input clk,e,a,b, output reg [1:0] q);
always @(posedge clk) begin
  if (e) q[0]<=a;
  q[1]<=b;
end
endmodule
""")
        self.assert_selected_q_bit(3, 0, 4)
        self.assert_selected_q_bit(4, 1, 3)

    def test_shared_enable_preserves_separate_data_actions(self):
        self.experiment("""module demo(input clk,e,a,b, output reg [1:0] q);
always @(posedge clk) if (e) begin
  q[0]<=a;
  q[1]<=b;
end
endmodule
""")
        self.assert_selected_q_bit(3, 0, 4)
        self.assert_selected_q_bit(4, 1, 3)
        self.assertEqual(self.names(self.select(2)), self.drivers('q'))

    def test_async_reset_and_negative_enable_keep_bits_separate(self):
        self.experiment("""module demo(input clk,rst,e0,e1,a,b, output reg [1:0] q);
always @(posedge clk or negedge rst)
  if (!rst) q<=2'b10;
  else begin
    if (!e0) q[0]<=a;
    if (!e1) q[1]<=b;
  end
endmodule
""")
        self.assert_selected_q_bit(5, 0, 6)
        self.assert_selected_q_bit(6, 1, 5)
        self.assertEqual(self.names(self.select(3)), self.drivers('q'))

    def test_reset_then_enable_rewrites_keep_per_bit_proofs(self):
        self.experiment("""module demo(input clk,r0,r1,e0,e1,a,b, output reg [1:0] q);
always @(posedge clk) begin
  if (r0) q[0]<=0; else if (e0) q[0]<=a;
  if (r1) q[1]<=1; else if (e1) q[1]<=b;
end
endmodule
""")
        self.assert_selected_q_bit(3, 0, 4)
        self.assert_selected_q_bit(4, 1, 3)

    def test_split_enable_ff_preserves_selected_initialization(self):
        self.experiment("""module demo(input clk,e0,e1,a,b, output reg [1:0] q);
initial q=2'b10;
always @(posedge clk) begin
  if (e0) q[0]<=a;
  if (e1) q[1]<=b;
end
endmodule
""")
        self.assert_selected_q_bit(4, 0, 5)
        self.assert_selected_q_bit(5, 1, 4)
        for offset, line in ((0,4),(1,5)):
            report = json.loads((self.base/f'slice-{line}.provenance.json').read_text())
            self.assertTrue(report['validation']['selected_state_init_preserved'])
            module = json.loads((self.base/f'slice-{line}.json').read_text())['modules']['selected']
            init = [n['attributes']['init'] for n in module['netnames'].values() if 'init' in n.get('attributes', {})]
            self.assertIn(str(offset), init)
        self.assertEqual(self.names(self.select(2)), self.drivers('q'))

    def test_compound_enable_keeps_explicit_control_fallback(self):
        self.experiment("""module demo(input clk,e0,e1,a,b, output reg [1:0] q);
wire next;
assign next=e0?(e1?a:q[0]):q[0];
always @(posedge clk) begin
  q[0]<=next;
  q[1]<=b;
end
endmodule
""")
        result = self.select(3)
        self.assertTrue(result['final_cells'])
        self.assertFalse(result['end_to_end_exact'])
        self.assertTrue(any(not r['applied'] and r['fallback_reason'] == 'unsupported_ff_control_rewrite'
                            for r in result['ff_bit_maps']))
        self.export(3)

    def test_ff_absorption_and_slice_evidence_tampering_is_rejected(self):
        self.experiment("""module demo(input clk,e0,e1,a,b, output reg [1:0] q);
always @(posedge clk) begin
  if (e0) q[0]<=a;
  if (e1) q[1]<=b;
end
endmodule
""")
        for kind, field, value, message in (
                ('ff_mux_absorb', 'source_offset', 1, 'FF absorption Q'),
                ('ff_mux_absorb', 'donor_offset', 9, 'FF absorption mux'),
                ('ff_bit_map', 'source_offset', 1, 'FF bit map Q'),
                ('ff_bit_map', 'absorptions', [], 'absorption evidence')):
            with self.subTest(kind=kind, field=field):
                rows = copy.deepcopy(self.trace.records)
                row = next(r for r in rows if r['record_type'] == kind and r.get('source_offset', r.get('links', [{}])[0].get('source_offset')) == 0)
                target = row['links'][0] if kind == 'ff_bit_map' else row
                target[field] = value
                path = self.base / 'tampered-ff.jsonl'
                path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
                with self.assertRaisesRegex(TraceError, message):
                    NativeTrace(path)
        self.assertEqual(self.trace.records, [json.loads(line) for line in (self.out/'yosys.jsonl').read_text().splitlines()])

    def test_clock_inverter_absorption_preserves_source(self):
        self.experiment('''module demo(input clk,d, output reg q);
wire nclk;
assign nclk=~clk;
always @(posedge nclk) q<=d;
endmodule
''')
        result=self.select(3)
        self.assertEqual(self.names(result),self.drivers('q'))
        self.assertEqual([c['type'] for c in result['final_cells']],['$_DFF_N_'])
        self.assertTrue(any(r.get('operation')=='opt_expr_polarity' for r in self.trace.records))
        self.export(3)

    def test_async_reset_tracks_surviving_action(self):
        self.experiment('''module demo(input clk,rst,d, output reg q);
always @(posedge clk or negedge rst) begin
  if (!rst) begin
    q<=0;
    q<=1;
  end else q<=d;
end
endmodule
''')
        self.assertFalse(self.select(4)['final_cells'])
        self.assertEqual(self.names(self.select(5)),self.drivers('q'))
        self.assertEqual(self.names(self.select(6)),self.drivers('q'))
        self.assertNotIn('partial_process_async_reset',self.trace.gaps)
        self.assertTrue(any(n['operation']=='reset_value' for n in self.trace.process_nodes.values()))
        self.assertTrue(self.export(5)['validation']['sequential_equivalence'])

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

    def test_wide_register_splits_preserve_per_bit_actions(self):
        self.experiment("""module demo(input clk,a,b, output reg [1:0] q);
always @(posedge clk) begin
  q[0]<=a;
  q[1]<=b;
end
endmodule
""")
        for line, offset in ((3,0),(4,1)):
            result=self.select(line)
            expected={n for n,c in self.module['cells'].items() if c['connections'].get('Q')==[self.module['ports']['q']['bits'][offset]]}
            self.assertEqual(self.names(result), expected)
            self.assertEqual(len(result['final_cells']),1)
            self.assertTrue(result['techmap_bit_maps'])
            self.export(line)

    def test_async_polarity_optimization_keeps_data_bits_separate(self):
        self.experiment("""module demo(input clk,rst_n,a,b, output reg [1:0] q);
always @(posedge clk or negedge rst_n)
  if (!rst_n) q<=0;
  else begin
    q[0]<=a;
    q[1]<=b;
  end
endmodule
""")
        for line,offset in ((5,0),(6,1)):
            result=self.select(line)
            expected={n for n,c in self.module['cells'].items() if c['connections'].get('Q')==[self.module['ports']['q']['bits'][offset]]}
            self.assertEqual(self.names(result),expected)
            self.assertEqual(len(result['final_cells']),1)
            self.export(line)
        self.assertEqual(self.names(self.select(3)),self.drivers('q'))

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

    def test_synchronous_reset_mux_absorption_keeps_source(self):
        self.experiment("""module demo(input clk,rst,d, output reg q);
wire next;
assign next=rst?1'b0:d;
always @(posedge clk) q<=next;
endmodule
""")
        self.assertEqual(self.names(self.select(3)),self.drivers('q'))
        self.assertTrue(any(c['type'].startswith('$_SDFF') for c in self.select(3)['final_cells']))
        self.assertTrue(any(r.get('operation')=='opt_dff_mux_absorb' for r in self.trace.records))
        self.export(3)

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

    def test_techmap_source_bit_swap_is_rejected(self):
        self.experiment("""module demo(input clk,a,b, output reg [1:0] q);
always @(posedge clk) begin
  q[0]<=a;
  q[1]<=b;
end
endmodule
""")
        rows=copy.deepcopy(self.trace.records)
        row=next(r for r in rows if r['record_type']=='techmap_bit_map' and r['links'])
        row['links'][0]['source_offset'] ^= 1
        path=self.base/'wrong-bit.jsonl'
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        with self.assertRaisesRegex(TraceError, 'techmap bit map pin correspondence'):
            NativeTrace(path)

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
