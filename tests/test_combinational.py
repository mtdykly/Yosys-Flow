"""The supported input contract is combinational, including procedural RTL."""
import json
from pathlib import Path
import tempfile
import unittest

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import TraceError, require_combinational
from yosys_flow.runner import run, load_run
from yosys_flow.extract import slice_module


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built; run make test')
class CombinationalInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='comb-contract-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def experiment(self, rtl, name='case', flow='synth'):
        source, out = self.base/(name+'.v'), self.base/name
        source.write_text(rtl)
        args = parser().parse_args(['run', '--source', str(source), '--out', str(out),
                                    '--top', 'demo', '--native-yosys', str(NATIVE), '--flow', flow])
        return args, out

    def test_clocked_initial_and_inferred_latch_are_rejected_in_all_flows(self):
        cases = {
            'clock': 'module demo(input clk,d,output reg q);always @(posedge clk) q<=d;endmodule',
            'constant_clocked': 'module demo(input clk,output reg q);always @(posedge clk) q<=0;endmodule',
            'initial': "module demo(output reg q);initial q=1'b0;endmodule",
            'decl_init': "module demo(input a,output y);reg q=1'b0;assign y=a;endmodule",
            'latch': 'module demo(input en,d,output reg q);always @* if(en) q=d;endmodule',
            'always_latch': 'module demo(input en,d,output logic q);always_latch if(en) q=d;endmodule',
        }
        for flow in ('synth', 'm1', 'm1-opt'):
            for name, rtl in cases.items():
                with self.subTest(flow=flow, name=name):
                    args, out = self.experiment(rtl, flow+'-'+name, flow)
                    with self.assertRaisesRegex(TraceError, 'Combinational-only flow rejects'):
                        run(args)
                    self.assertEqual(json.loads((out/'manifest.json').read_text())['status'], 'failed')
                    with self.assertRaises(TraceError):
                        load_run(out)

    def test_complete_always_comb_and_nonblocking_comb_assignment_are_accepted(self):
        for kind, body in (
            ('always_comb', 'always_comb begin if(s) y=a; else y=b; end'),
            ('nonblocking', 'always @* begin if(s) y<=a; else y<=b; end'),
        ):
            with self.subTest(kind=kind):
                args, out = self.experiment('module demo(input s,a,b,output logic y);'+body+' endmodule', kind)
                result = run(args)
                self.assertTrue(all(result['checks'].values()))
                self.assertEqual(json.loads((out/'manifest.json').read_text())['input_scope'], 'combinational')
                module = json.loads((out/'after.json').read_text())['modules']['demo']
                require_combinational(module)
                self.assertTrue(module['cells'])

    def test_combinational_loop_is_not_published(self):
        args, out = self.experiment('module demo(input a,output y);wire t;assign t=y^a;assign y=~t;endmodule')
        with self.assertRaises(TraceError):
            run(args)
        self.assertEqual(json.loads((out/'manifest.json').read_text())['status'], 'failed')


class CombinationalExportTests(unittest.TestCase):
    def test_standalone_export_rejects_state_even_when_unselected(self):
        module = {'ports': {}, 'netnames': {}, 'cells': {'state': {'type': '$_DFF_P_'}}}
        with self.assertRaisesRegex(TraceError, 'rejects state cell'):
            slice_module(module, [], 'selected')

    def test_standalone_export_rejects_initialization(self):
        module = {'ports': {}, 'cells': {}, 'netnames': {'q': {'bits': [2], 'attributes': {'init': '1'}}}}
        with self.assertRaisesRegex(TraceError, 'initialization'):
            slice_module(module, [], 'selected')
