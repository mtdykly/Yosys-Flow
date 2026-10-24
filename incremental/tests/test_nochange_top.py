"""Official hierarchy boundaries and counterexample attribution."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from incremental import nochange, nochange_top, rtlil, sequential
from incremental.__main__ import parser
from yosys_flow.common import TraceError


class TopInventoryTests(unittest.TestCase):
    def test_official_tops_and_file_lists_do_not_add_leaf_parameter_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = parser().parse_args(['anubis-nochange','--upstream-top','--inventory-only',
                '--yosys','absent','--eqy','absent','--out',tmp+'/inventory'])
            report = nochange.run(args)
            self.assertEqual(report['cases_accounted_for'], 30)
            self.assertEqual({r['top'] for r in report['profiles']},
                             {'pipeline','cpu_bug','fpu','mor1kx','or1200_top'})
            configs = json.loads((Path(args.out)/'configurations.json').read_text())
            self.assertEqual(configs['fpu']['parameters'], {})
            self.assertNotIn('fpu/rtl/fp_lead0_p1.v', configs['fpu']['sources'])
            self.assertEqual(configs['mor1kx']['defines'], [])
            self.assertFalse(report['reuse_authorized'])

    def test_child_type_parameter_and_initialization_changes_are_not_erased(self):
        template = 'module \\top\n  cell \\child_a \\u\n    parameter \\P 1\n  end\nend\n'
        old = rtlil.parse(template)
        for text in (template.replace('child_a','child_b'), template.replace('\\P 1','\\P 2')):
            self.assertEqual(nochange_top.differences(old, rtlil.parse(text)), ['top'])
        memory = ('module \\top\n  cell $mem_v2 $generated1\n'
                  '    parameter \\MEMID "$generated1"\n    parameter \\INIT 4\'0000\n  end\nend\n')
        renamed = memory.replace('generated1','generated2')
        self.assertEqual(nochange_top.differences(rtlil.parse(memory), rtlil.parse(renamed)), [])
        self.assertEqual(nochange_top.differences(rtlil.parse(memory),
            rtlil.parse(renamed.replace("4'0000", "4'0001"))), ['top'])

    def test_conflicting_boundary_options_are_rejected(self):
        args = parser().parse_args(['anubis-nochange','--upstream-top','--with-sequential','--out','unused'])
        with self.assertRaisesRegex(TraceError, 'different benchmark boundaries'):
            nochange.run(args)


@unittest.skipUnless(shutil.which('yosys') and shutil.which('eqy'), 'formal tools required')
class TopProofTests(unittest.TestCase):
    def test_all_previous_fpu_counterexamples_are_inactive_at_real_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = nochange.run(parser().parse_args(['anubis-nochange','--upstream-top',
                '--cases','fpu_noc0','fpu_noc1','fpu_noc2','fpu_noc6','--out',tmp+'/survey']))
            self.assertEqual(report['counts'], {'structural_same':4})
            self.assertEqual(report['top_counterexamples'], [])
            self.assertTrue(report['inputs_unchanged'])
            for row in report['profiles']:
                self.assertEqual(row['changed_modules'], [])
                self.assertFalse(row['output_definedness_proven'])
                self.assertEqual(row['local_checks'], [])
            sequential.verify_files(Path(tmp)/'survey', report['evidence_files'])

    def synthetic(self, tmp, body):
        root = Path(tmp)
        source = root/'source.v'
        source.write_text(body)
        config = {'top':'top','clock':'clk','parameters':{},'defines':[],
                  'reader_commands':['read_verilog -sv '+str(source)]}
        args = parser().parse_args(['anubis-nochange','--upstream-top','--repeats','1','--out',tmp+'/unused'])
        tools = sequential.toolchain(args)
        old = nochange_top.elaborate(config, None, tools['yosys']['path'], root/'old', 10)
        new = nochange_top.elaborate(config, 'EDIT', tools['yosys']['path'], root/'new', 10)
        return nochange_top.evaluate({'id':'control','macro':'EDIT'}, config, old, new,
            root/'old', root/'new', tools, args)

    def test_unreachable_local_counterexample_is_not_a_top_difference(self):
        body = '''module leaf(input a, output y);
`ifdef EDIT
assign y = a;
`else
assign y = 1'b0;
`endif
endmodule
module top(output y); leaf u(.a(1'b0), .y(y)); endmodule
'''
        with tempfile.TemporaryDirectory() as tmp:
            row = self.synthetic(tmp, body)
            self.assertEqual(row['local_checks'][0]['status'], 'different')
            self.assertEqual(row['status'], 'structural_same')
            self.assertEqual(row['reason'], 'same_normalized_complete_top_context')
            self.assertFalse(row['reuse_authorized'])

    def test_observable_top_change_has_a_real_counterexample(self):
        body = '''module leaf(input a, output y);
`ifdef EDIT
assign y = ~a;
`else
assign y = a;
`endif
endmodule
module top(input a, output y); leaf u(.a(a), .y(y)); endmodule
'''
        with tempfile.TemporaryDirectory() as tmp:
            row = self.synthetic(tmp, body)
            self.assertEqual(row['status'], 'different')
            self.assertEqual(row['top_proof']['counterexample_scope'], 'complete_upstream_top')
            self.assertTrue(Path(row['top_proof']['samples'][0]['counterexample']).is_file())

    def test_unknown_top_proof_does_not_promote_local_counterexample(self):
        body = '''module top(input a, output y);
`ifdef EDIT
assign y = ~a;
`else
assign y = a;
`endif
endmodule
'''
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(nochange_top, 'top_check', return_value={'status':'unknown','reason':'timeout'}):
                row = self.synthetic(tmp, body)
            self.assertEqual(row['local_checks'][0]['status'], 'different')
            self.assertEqual(row['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
