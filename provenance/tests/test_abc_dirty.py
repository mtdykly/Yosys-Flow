"""Real dc2/dch dirty capture versus the independently scanning recorder."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.abc_internal import read_evidence
from yosys_flow.cli import parser
from yosys_flow.runner import load_run, run


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built')
class DirtyCaptureTests(unittest.TestCase):
    def test_audited_changes_replay_identically_to_scanning_periodic_checkpoints(self):
        cases = {
            'adder': "module demo(input [23:0] a,b, output [24:0] y); assign y={1'b0,a}+{1'b0,b}; endmodule",
            'mixed': 'module demo(input [5:0] a,b,c, input s, output [11:0] y); assign y=s ? (a*b) : ((a^c)+(b|c)); endmodule',
        }
        with tempfile.TemporaryDirectory(prefix='abc-dirty-') as temp:
            root = Path(temp)
            for name, rtl in cases.items():
                with self.subTest(case=name):
                    source=root/(name+'.v');source.write_text(rtl+'\n')
                    snapshots=[];designs=[];visits=[];sizes=[]
                    for scan in (True, False):
                        out=root/(name+('-scan' if scan else '-dirty'))
                        env={'YOSYS_FLOW_ABC_FULL_SNAPSHOTS':'0',
                             'YOSYS_FLOW_ABC_SCAN_SNAPSHOTS':'1' if scan else '0',
                             'YOSYS_FLOW_ABC_VERIFY_DIRTY':'1',
                             'YOSYS_FLOW_ABC_CHECKPOINT_INTERVAL':'128' if scan else '0'}
                        with patch.dict(os.environ, env):
                            run(parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out)]))
                        _,trace,_=load_run(out)
                        artifacts=trace.regions[0]['artifacts']
                        path=out/artifacts['input.blif.flow.jsonl']
                        rows,nets,_,_=read_evidence(path,(out/artifacts['script']).read_text())
                        snapshots.append({i:(n.pis,n.pos,dict(n.nodes)) for i,n in nets.items()})
                        designs.append(json.loads((out/'after.json').read_text())['modules'])
                        stats=rows[-1]['capture_stats']
                        visits.append(stats['aig_full_node_visits']+stats['aig_dirty_node_visits'])
                        sizes.append(path.stat().st_size)
                        if not scan:
                            self.assertGreater(stats['aig_dirty_node_visits'],0)
                            self.assertEqual(stats['checkpoint_interval'],0)
                    self.assertEqual(snapshots[0],snapshots[1])
                    self.assertEqual(designs[0],designs[1])
                    self.assertLess(visits[1],visits[0])
                    self.assertLess(sizes[1],sizes[0])
