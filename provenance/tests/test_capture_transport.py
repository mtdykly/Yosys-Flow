"""Compact capture keeps independent audit and causal evidence intact."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_native import NATIVE
from yosys_flow.abc_internal import decode_network, read_evidence
from yosys_flow.capture import traced_script
from yosys_flow.common import TraceError, token
from yosys_flow.native import NativeTrace
from yosys_flow.native_storage import Checkpoints


class CheckpointTransportTests(unittest.TestCase):
    def full(self):
        return dict(record_type='checkpoint', seq=2, checkpoint_encoding='full',
                    cells=['c@1'], connections={'wire': 0}, cell_count=1, connection_count=1)

    def delta(self):
        return dict(record_type='checkpoint', seq=4, checkpoint_encoding='delta', base_checkpoint=2,
                    cell_removals=['c@1'], cell_additions=['c@2'], connections={'wire': 1},
                    cell_count=1, connection_count=1)

    def started(self):
        replay = Checkpoints(['native_checkpoint_deltas_v1'])
        replay.check(self.full(), {'c@1'}, {'wire': {'revision': 0}})
        return replay

    def test_versions_connections_empty_deltas_and_final_full_audit(self):
        replay = self.started()
        replay.check(self.delta(), {'c@2'}, {'wire': {'revision': 1}})
        replay.check(dict(self.delta(), seq=6, base_checkpoint=4, cell_additions=[],
                          cell_removals=[], connections={}), {'c@2'}, {'wire': {'revision': 1}})
        replay.check(dict(self.full(), seq=7, record_type='run_end', cells=['c@2'],
                          connections={'wire': 1}), {'c@2'}, {'wire': {'revision': 1}})

    def test_corrupt_or_unannounced_deltas_are_rejected(self):
        for change in ({'base_checkpoint': 1}, {'base_checkpoint': True}, {'cells': []},
                       {'cell_removals': ['absent']}, {'cell_removals': []},
                       {'cell_additions': ['c@1']}, {'cell_additions': ['c@2', 'c@2']},
                       {'cell_additions': []}, {'connections': {}}, {'connections': {'wire': True}},
                       {'cell_count': 2}, {'connection_count': False}, {'record_type': 'run_end'}):
            with self.subTest(change=change), self.assertRaises(TraceError):
                self.started().check(dict(self.delta(), **change), {'c@2'}, {'wire': {'revision': 1}})
        with self.assertRaises(TraceError):
            Checkpoints([]).check(self.delta(), {'c@2'}, {'wire': {'revision': 1}})
        with self.assertRaises(TraceError):
            self.started().check(dict(self.full(), seq=5, record_type='run_end'),
                                 {'c@2'}, {'wire': {'revision': 1}})

    def test_legacy_full_checkpoints_remain_supported(self):
        Checkpoints([]).check({'record_type': 'run_end', 'seq': 1, 'cells': []}, set(), {})


class InterfaceTransportTests(unittest.TestCase):
    features = ['shared_snapshot_interfaces_v1']

    def initial(self):
        row = dict(kind='history_network', seq=2, command_id=1, representation='aig',
                   encoding='checkpoint', network_epoch=1, deleted_nodes=[], pis=[1], pos=[2],
                   nodes=[{'id': 1, 'op': 'pi', 'fanins': []}, {'id': 2, 'op': 'buf', 'fanins': [2]}])
        nets, epochs = {}, {}
        nets[2] = decode_network(row, nets, epochs, 5, self.features)
        return nets, epochs

    def delta(self):
        return dict(kind='history_network', seq=4, command_id=1, representation='aig',
                    encoding='delta', network_epoch=1, base_snapshot=2, interface_base=2,
                    deleted_nodes=[], nodes=[])

    def test_reference_reuses_interface_without_modifying_raw_evidence(self):
        nets, epochs = self.initial()
        raw = self.delta(); original = deepcopy(raw)
        nets[4] = decode_network(raw, nets, epochs, 5, self.features)
        self.assertEqual(raw, original)
        self.assertIs(nets[4].pis, nets[2].pis)
        self.assertIs(nets[4].pos, nets[2].pos)
        changed = dict(self.delta(), seq=6, base_snapshot=4, interface_base=4,
                       nodes=[{'id': 2, 'op': 'buf', 'fanins': [3]}])
        later = decode_network(changed, nets, epochs, 5, self.features)
        self.assertEqual(later.nodes[2]['fanins'], [3])
        self.assertEqual(nets[2].nodes[2]['fanins'], [2])

    def test_explicit_interface_change_becomes_next_reference(self):
        nets, epochs = self.initial()
        row = self.delta(); row.pop('interface_base'); row.update(pis=[1], pos=[1])
        nets[4] = decode_network(row, nets, epochs, 5, self.features)
        net = decode_network(dict(self.delta(), seq=6, base_snapshot=4, interface_base=4),
                             nets, epochs, 5, self.features)
        self.assertEqual(net.pos, [1])

    def test_corrupt_scope_metadata_missing_feature_and_deleted_output_rejected(self):
        for change in ({'interface_base': 9}, {'interface_base': True}, {'command_id': 2},
                       {'network_epoch': 2}, {'representation': 'gia'}, {'encoding': 'checkpoint'},
                       {'pis': [1]}, {'pos': []}, {'pi_names': ['fabricated']},
                       {'deleted_nodes': [2]}, {'nodes': [{'id': 1, 'op': 'one', 'fanins': []}]}):
            with self.subTest(change=change), self.assertRaises(TraceError):
                nets, epochs = self.initial()
                decode_network(dict(self.delta(), **change), nets, epochs, 5, self.features)
        with self.assertRaises(TraceError):
            nets, epochs = self.initial()
            decode_network(self.delta(), nets, epochs, 5)
        with self.assertRaises(TraceError):
            nets, epochs = self.initial()
            row = self.delta(); row.pop('interface_base')
            decode_network(row, nets, epochs, 5, self.features)


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built')
class ActualCaptureTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='capture-transport-')
        cls.root = Path(cls.tmp.name)
        source = cls.root/'design.v'
        source.write_text('''module left(input [63:0] a,b, output [63:0] y);
assign y=a&b;
endmodule
module right(input [63:0] a,b, output [63:0] y);
assign y=a^b;
endmodule
module top(input [63:0] a,b, output [63:0] y,z, output [16:0] sum);
left first(a,b,y);
right second(a,b,z);
assign sum={1'b0,a[15:0]}+{1'b0,b[15:0]};
endmodule
''')
        cls.runs = {}
        for mode in ('compact', 'expanded', 'profiled'):
            out = cls.root/mode; out.mkdir()
            script = traced_script(out, 'test', f'read_verilog -sv -defer {token(source)}\nhierarchy -check -top top', 'synth -top top')
            (out/'traced.ys').write_text(script)
            env = {k: v for k, v in os.environ.items() if not k.startswith('YOSYS_FLOW_')}
            env['YOSYS_FLOW_ABC_TRACE'] = '1'
            if mode == 'expanded':
                env.update(YOSYS_FLOW_NATIVE_FULL_CHECKPOINTS='1', YOSYS_FLOW_ABC_FULL_INTERFACES='1')
            if mode == 'profiled': env['YOSYS_FLOW_CAPTURE_PROFILE'] = '1'
            result = subprocess.run([str(NATIVE), '-Q', '-T', '-s', str(out/'traced.ys')],
                                    env=env, capture_output=True, text=True, timeout=60)
            if result.returncode: raise AssertionError(result.stdout[-5000:]+result.stderr[-5000:])
            trace = NativeTrace(out/'yosys.jsonl', defer_refinement=True)
            cls.runs[mode] = (out, trace)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def test_shared_inputs_lookup_once_per_region_and_do_not_leak_between_modules(self):
        _, trace = self.runs['compact']
        self.assertGreaterEqual(len(trace.regions), 3)
        stats = trace.records[-1]['recorder_stats']
        self.assertLessEqual(stats['input_content_lookups'], len(trace.regions)*2)
        self.assertGreater(stats['input_cache_hits'], 100)
        inputs = [set(r['inputs']) for r in trace.regions]
        self.assertTrue(all(not a.intersection(b) for i,a in enumerate(inputs) for b in inputs[i+1:]))

    def test_expanded_and_compact_transport_have_identical_replay_and_networks(self):
        a, ta = self.runs['compact']; b, tb = self.runs['expanded']
        self.assertEqual(ta.final, tb.final)
        self.assertEqual(ta.objects, tb.objects)
        self.assertEqual(ta.assignments, tb.assignments)
        self.assertEqual(ta.edges, tb.edges)
        self.assertEqual(json.loads((a/'after.json').read_text()), json.loads((b/'after.json').read_text()))
        self.assertTrue(any(r.get('checkpoint_encoding') == 'delta' for r in ta.records))
        self.assertTrue(all(r.get('checkpoint_encoding') != 'delta' for r in tb.records))
        compact_bytes = expanded_bytes = 0; inherited = 0
        for ra, rb in zip(ta.regions, tb.regions):
            pa, pb = a/ra['artifacts']['input.blif.flow.jsonl'], b/rb['artifacts']['input.blif.flow.jsonl']
            rows, na, _, _ = read_evidence(pa, (a/ra['artifacts']['script']).read_text())
            _, nb, _, _ = read_evidence(pb, (b/rb['artifacts']['script']).read_text())
            self.assertEqual({k:(v.pis,v.pos,dict(v.nodes)) for k,v in na.items()},
                             {k:(v.pis,v.pos,dict(v.nodes)) for k,v in nb.items()})
            inherited += sum('interface_base' in r for r in rows)
            compact_bytes += pa.stat().st_size; expanded_bytes += pb.stat().st_size
        self.assertGreater(inherited, 0)
        self.assertLess(compact_bytes, expanded_bytes)
        self.assertLess((a/'yosys.jsonl').stat().st_size, (b/'yosys.jsonl').stat().st_size)

    def test_opt_in_timings_do_not_change_design_or_provenance(self):
        a, ta = self.runs['compact']; b, tb = self.runs['profiled']
        self.assertNotIn('capture_profile', ta.records[-1])
        profile = tb.records[-1]['capture_profile']
        self.assertGreater(profile['timings']['json_encode']['seconds'], 0)
        self.assertGreater(profile['timings']['abc_import_inclusive']['calls'], 0)
        self.assertEqual(ta.objects, tb.objects)
        self.assertEqual(ta.edges, tb.edges)
        self.assertEqual(json.loads((a/'after.json').read_text()), json.loads((b/'after.json').read_text()))
        for region in tb.regions:
            with (b/region['artifacts']['input.blif.flow.jsonl']).open() as stream:
                ends = [r for line in stream if (r := json.loads(line))['kind'] == 'command_end']
            self.assertTrue(all(r['cpu_seconds_inclusive'] >= 0 for r in ends))
