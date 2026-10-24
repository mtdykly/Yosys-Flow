"""Sequential reuse, including explicit ANUBIS-derived scope/negative controls.

The fast fixtures reduce ALPHA regfile data width from 64 to 2; the independent
anubis-sequential command runs the unchanged full upstream module/macros.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from incremental import nochange, sequential
from incremental.__main__ import parser
from yosys_flow.common import TraceError, digest


class SequentialUnitTests(unittest.TestCase):
    def test_cli_separates_sequential_and_combinational_budgets(self):
        args = parser().parse_args(['seq-baseline','--source','a.v','--top','top','--clock','clk','--out','out'])
        self.assertEqual(args.proof_timeout, 120)
        self.assertIsNone(args.reset)
        args = parser().parse_args(['auto','--base-run','old','--new-source','new.v','--out','out'])
        self.assertEqual((args.equivalence_timeout,args.equivalence_budget), (5,15))

    def test_process_timeout_terminates_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = "import subprocess,sys,time,os; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], preexec_fn=os.setpgrp); open('pid','w').write(str(p.pid)); time.sleep(30)"
            status = sequential.execute([sys.executable,'-c',script], root, 'timeout', time.monotonic()+0.5)
            self.assertEqual(status, 'timeout')
            pid = int((root/'pid').read_text())
            # A killed child may remain a zombie until its reaper schedules.
            state = Path(f'/proc/{pid}/stat')
            deadline = time.monotonic()+1
            while state.exists() and state.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(not state.exists() or state.read_text().split()[2] == 'Z')

    def test_archive_rejects_path_escape_and_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(TraceError, 'invalid'):
                sequential.verify_files(root, {'../outside':'bad'})
            with self.assertRaisesRegex(TraceError, 'empty'):
                sequential.verify_files(root, {})


@unittest.skipUnless(all(shutil.which(t) for t in ('yosys','eqy','sby','yices-smt2','yosys-abc')),
                     'pinned sequential EQY toolchain required')
class SequentialProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nochange.inventory()
        cls.temp = tempfile.TemporaryDirectory(prefix='seq-anubis-')
        cls.root = Path(os.environ.get('YOSYS_FLOW_SEQUENTIAL_RESULTS', cls.temp.name)).resolve()
        cls.root.mkdir(parents=True, exist_ok=True)
        cls.inputs = cls.root/'inputs'
        cls.inputs.mkdir()
        raw = nochange.CORPUS/'upstream/alpha/regfile.v'
        cls.text = raw.read_text().replace('[63:0]', '[1:0]').replace("64'b0", "2'b0")
        cls.source = cls.inputs/'base.v'
        cls.source.write_text(cls.text)
        shutil.copyfile(raw.with_name('sys_defs.vh'), cls.inputs/'sys_defs.vh')
        (cls.inputs/'adaptation.json').write_text(json.dumps({'source':'alpha/regfile.v',
            'sha256':digest(raw), 'changes':['data width 64 -> 2; original 32 addresses and macros retained'],
            'raw_upstream_case':False}))
        cls.base = cls.root/'baseline'
        cls.baseline = sequential.baseline(parser().parse_args(['seq-baseline','--source',str(cls.source),
            '--top','regfile','--clock','wr_clk','--out',str(cls.base)]))
        if cls.baseline['status'] != 'ready':
            raise AssertionError(json.dumps(cls.baseline, indent=2)[-4000:])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_reuse(self, name, source=None, extra=(), base=None):
        args = parser().parse_args(['seq-reuse','--base-run',str(base or self.base),
            '--new-source',str(source or self.source),'--out',str(self.root/name),*extra])
        return sequential.reuse(args)

    def variant(self, name, text):
        source = self.inputs/(name+'.v')
        source.write_text(text)
        return source

    def assert_no_delivery(self, name, result):
        self.assertFalse(result['reused'])
        self.assertEqual(result['next_action'], 'full_synthesis_required')
        self.assertEqual(list((self.root/name).glob('reused.*')), [])

    def test_noc5_unbounded_proof_reuses_exact_old_netlist(self):
        result = self.run_reuse('noc5', extra=['--define','ANUBIS_NOC_5'])
        self.assertEqual(result['status'], 'equivalent', result.get('proof'))
        self.assertTrue(result['reused'])
        self.assertFalse(result['new_full_synthesis_used'])
        for suffix in ('v','il','json'):
            self.assertEqual((self.base/('netlist.'+suffix)).read_bytes(),
                             (self.root/'noc5'/('reused.'+suffix)).read_bytes())
        proof = result['proof']
        for obligation in proof['obligations'].values():
            self.assertTrue(obligation['all_outputs_covered'])
            self.assertTrue(all(p['sby_status'] == 'PASS' for p in obligation['partitions']))
        for script in (self.root/'noc5/rtl').glob('*.ys'):
            self.assertNotIn('\nsynth ', script.read_text())
        config = (self.root/'noc5/proof/equivalence/proof.eqy').read_text()
        self.assertIn('nodefault', config)
        self.assertIn('use sby', config)

    def test_real_state_write_change_is_rejected(self):
        result = self.run_reuse('local0', extra=['--define','ANUBIS_LOCAL_0'])
        self.assertEqual(result['status'], 'different', result.get('proof'))
        obligation = result['proof']['obligations']['outputs_only_retry']
        self.assertEqual(obligation['reason'], 'reachable_output_counterexample')
        self.assertFalse(obligation['internal_cut_inputs'])
        self.assert_no_delivery('local0', result)

    def test_forwarding_change_is_rejected(self):
        result = self.run_reuse('local2', extra=['--define','ANUBIS_LOCAL_2'])
        self.assertEqual(result['status'], 'different', result.get('proof'))
        self.assert_no_delivery('local2', result)

    def test_secondary_output_is_not_omitted(self):
        source = self.variant('secondary', self.text.replace('rdb_out = wr_data;', 'rdb_out = ~wr_data;'))
        result = self.run_reuse('secondary', source)
        self.assertEqual(result['status'], 'different', result.get('proof'))
        self.assert_no_delivery('secondary', result)

    def test_x_outputs_cannot_vacuously_pass(self):
        source = self.variant('undefined', self.text.replace('rda_out = wr_data;', "rda_out = 2'bxx;"))
        result = self.run_reuse('undefined', source)
        self.assertEqual(result['status'], 'unknown', result.get('proof'))
        self.assertEqual(result['proof']['reason'], 'new_outputs_not_proven_defined')
        self.assert_no_delivery('undefined', result)

    def test_uninitialized_state_is_rejected_before_proof(self):
        source = self.variant('uninitialized', self.text[:self.text.index('initial begin')]+'endmodule\n')
        result = self.run_reuse('uninitialized', source)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('initialization', result['failure'])
        self.assert_no_delivery('uninitialized', result)

    def test_negedge_and_derived_clock_are_rejected(self):
        for name, edge in [('negedge','negedge wr_clk'), ('derived','posedge wr_en')]:
            source = self.variant(name, self.text.replace('posedge wr_clk', edge))
            result = self.run_reuse(name, source)
            self.assertEqual(result['status'], 'blocked')
            self.assertIn('clocks', result['failure'])
            self.assert_no_delivery(name, result)

    def test_changed_interface_is_rejected(self):
        source = self.variant('width', self.text.replace('[1:0]', '[2:0]').replace("2'b0", "3'b0"))
        result = self.run_reuse('width', source)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('interface', result['failure'])
        self.assert_no_delivery('width', result)

    def test_budget_exhaustion_never_delivers(self):
        result = self.run_reuse('timeout', extra=['--proof-timeout','0.000001'])
        self.assertEqual(result['status'], 'unknown')
        self.assert_no_delivery('timeout', result)

    def test_netlist_tampering_is_rejected_before_eqy(self):
        base = self.root/'tampered-baseline'
        shutil.copytree(self.base, base)
        (base/'netlist.v').write_text((base/'netlist.v').read_text()+'\n// changed\n')
        result = self.run_reuse('tampered', base=base)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('checksum', result['failure'])
        self.assertFalse((self.root/'tampered/proof').exists())
        self.assert_no_delivery('tampered', result)

    def test_partial_partition_pass_does_not_authorize_reuse(self):
        gold = json.loads((self.base/'proof/gold.json').read_text())
        gate = json.loads((self.base/'proof/gate.json').read_text())
        actual_execute = sequential.execute
        def damaged(argv, directory, name, deadline):
            code = actual_execute(argv, directory, name, deadline)
            statuses = sorted((directory/'work/strategies').glob('*/induction/status'))
            self.assertGreater(len(statuses), 1)
            statuses[0].write_text('UNKNOWN\n')
            return code
        with patch.object(sequential, 'execute', side_effect=damaged):
            result = sequential.eqy(gold, gate, self.root/'partial-partitions', self.baseline['tools'],
                                    time.monotonic()+120, strengthen=True)
        self.assertEqual(result['status'], 'unknown')

    def test_synchronous_startup_reset_and_reset_value_change(self):
        # Explicit test adaptation: replace ALPHA's initial block with a
        # synchronous reset of the same register array. Not an upstream NOC.
        text = self.text[:self.text.index('initial begin')]+'endmodule\n'
        text = text.replace('wr_en, wr_clk);', 'wr_en, wr_clk, rst);').replace('integer i;', 'input rst;\n  integer i;')
        text = text.replace('always @(posedge wr_clk)',
            "always @(posedge wr_clk)\n    if (rst) begin\n      for (i=0;i<32;i=i+1) registers[i] <= 2'b0;\n    end else")
        source = self.variant('reset', text)
        base = self.root/'reset-baseline'
        result = sequential.baseline(parser().parse_args(['seq-baseline','--source',str(source),
            '--top','regfile','--clock','wr_clk','--reset','rst','--reset-cycles','2','--out',str(base)]))
        self.assertEqual(result['status'], 'ready', result.get('proof', result))
        result = self.run_reuse('reset-same', source, base=base)
        self.assertEqual(result['status'], 'equivalent', result.get('proof'))
        self.assertEqual(result['config']['observations'], 'all outputs after startup reset')
        source = self.variant('bad-reset', text.replace("registers[i] <= 2'b0", "registers[i] <= 2'b11"))
        result = self.run_reuse('bad-reset', source, base=base)
        self.assertEqual(result['status'], 'different', result.get('proof'))
        self.assert_no_delivery('bad-reset', result)
