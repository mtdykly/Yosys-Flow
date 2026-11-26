"""Capture cost invariants, with an eager/audited evidence reference."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_native import NATIVE
from yosys_flow.common import token
from yosys_flow.native import NativeTrace


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys required')
class CaptureCostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='capture-cost-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def capture(self, name, rtl, passes, audit=False):
        source = self.root/'source.v'
        source.write_text(rtl)
        out = self.root/name
        out.mkdir()
        script = (f'flow_start {token(out/"yosys.jsonl")} cost\n'
                  f'read_verilog -sv -defer {token(source)}\nhierarchy -top demo\n'
                  f'flow_hierarchy\nproc -noopt\n{passes}\n'
                  f'write_json {token(out/"after.json")}\nflow_stop\n')
        env = {k:v for k,v in os.environ.items() if not k.startswith('YOSYS_FLOW_')}
        env['YOSYS_FLOW_CAPTURE_PROFILE'] = '1'
        if audit:
            env['YOSYS_FLOW_NATIVE_AUDIT'] = '1'
        result = subprocess.run([str(NATIVE), '-Q', '-T', '-p', script], env=env,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:]+result.stderr[-4000:])
        trace = NativeTrace(out/'yosys.jsonl', defer_refinement=True)
        trace.compare_netlist(out/'after.json')
        return trace, json.loads((out/'after.json').read_text())

    @staticmethod
    def signature(trace):
        # Version IDs and capture intervals can differ after delaying a scope.
        return sorted(json.dumps({k:trace.objects[r][k] for k in
                      ('state', 'origins', 'precision')}, sort_keys=True) for r in trace.final)

    def pair(self, rtl, passes):
        a, an = self.capture('lazy', rtl, passes)
        b, bn = self.capture('audit', rtl, passes, audit=True)
        self.assertEqual(an, bn)
        self.assertEqual(self.signature(a), self.signature(b))
        self.assertEqual(a.gaps, b.gaps)
        return a, b

    def test_unchanged_cells_do_not_prepare_rewrites_or_query_premises(self):
        rtl = 'module demo(input [255:0] a,b,output [255:0] y);\n'
        rtl += '\n'.join(f'assign y[{i}]=a[{i}]&b[{i}];' for i in range(256))+'\nendmodule\n'
        a, b = self.pair(rtl, 'wreduce; wreduce')
        work = a.records[-1]['capture_work']
        self.assertGreaterEqual(work['wreduce/deferred_visit/wreduce_special'], 512)
        self.assertFalse(any('/rewrite_begin/' in k for k in work))
        self.assertNotIn('wreduce/premise_queries', work)
        self.assertGreater(b.records[-1]['capture_work']['wreduce/rewrite_begin/wreduce_cell'], 0)

    def test_first_output_connection_and_recursive_width_changes_keep_evidence(self):
        rtl = '''module demo(input [3:0] a,b, output [11:0] y, output [7:0] z);
assign y=a+b;
assign z=a*b;
endmodule
'''
        a,b = self.pair(rtl, 'wreduce; opt_clean; wreduce')
        def events(t):
            return [(r['rule'],list(r['origins']),r['port_changes']) for r in t.events
                    if r['record_type']=='optimization_rewrite']
        self.assertEqual(len(events(a)), len(events(b)))
        changes=[r for r in a.events if r['record_type']=='optimization_rewrite']
        self.assertTrue(changes)
        self.assertTrue(any(r['connections'] for r in changes))
        self.assertTrue(all(r['start_seq'] < r['seq'] for r in changes))

    def test_shared_constant_chain_cache_and_pass_invalidation(self):
        rtl = '''module demo(input a,b, output [1:0] y,z);
wire s,t;
assign s=1'b0;
assign t=s;
assign y=t?{t,a}:{t,b};
assign z=s?{s,b}:{s,a};
endmodule
'''
        a,b = self.pair(rtl, 'opt_expr; opt_clean; opt_expr')
        work=a.records[-1]['capture_work']
        self.assertGreater(work.get('opt_expr/premise_cache_hit',0),0)
        self.assertNotIn('opt_expr/premise_cache_hit',b.records[-1]['capture_work'])
        self.assertTrue(any(r.get('premises') for r in a.events if r['record_type']=='optimization_rewrite'))

    def test_retired_inversion_witness_and_scope_restoration(self):
        rtl = '''module demo(input s,a,b,c,d,output y,z,w);
wire n,m;
assign n=~s;
assign m=~n;
assign y=n?a:b;
assign z=m?c:d;
assign w=s&d;
endmodule
'''
        a,b = self.pair(rtl, 'opt_expr; opt_clean; opt_expr')
        events=[r for r in a.events if r['record_type']=='optimization_rewrite']
        self.assertTrue(events)
        self.assertTrue(any(r['premises'] for r in events))
        self.assertGreater(a.records[-1]['capture_work'].get('opt_expr/scope_reused_observation',0),0)


if __name__ == '__main__':
    unittest.main()
