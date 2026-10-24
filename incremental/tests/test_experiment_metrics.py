"""Metrics count actual instances and never label unmodeled paths as depth."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from incremental.experiment_metrics import measure_netlist, NetlistMetricError


def gate(kind, **connections):
    return {'type': kind, 'connections': {p: [b] for p, b in connections.items()},
            'port_directions': {p: 'output' if p == 'Y' else 'input' for p in connections}}


def instance(kind, **connections):
    return {'type': kind, 'connections': {p: [b] for p, b in connections.items()},
            'port_directions': {p: 'output' if p == 'y' else 'input' for p in connections}}


def module(inputs, outputs, cells):
    return {'ports': {**{p: {'direction': 'input', 'bits': [b]} for p, b in inputs.items()},
                      **{p: {'direction': 'output', 'bits': [b]} for p, b in outputs.items()}},
            'cells': cells}


class NetlistMetricsTests(unittest.TestCase):
    def test_repeated_hierarchy_counts_instances_and_crosses_boundaries(self):
        child = module({'a': 2, 'b': 3}, {'y': 4}, {'g': gate('$_AND_', A=2, B=3, Y=4)})
        # Same child numeric bit IDs are scoped to the instance.  A third
        # unused module definition contributes neither cells nor depth.
        top = module({'a': 2, 'b': 3, 'c': 4}, {'y': 6}, {
            'first': instance('child', a=2, b=3, y=5),
            'second': instance('child', a=5, b=4, y=6)})
        result = measure_netlist({'modules': {'top': top, 'child': child, 'unused': child}}, 'top')
        self.assertEqual(result['cell_count'], 2)
        self.assertEqual(result['cell_types'], {'$_AND_': 2})
        self.assertEqual(result['module_instance_count'], 3)
        self.assertEqual(result['logic_depth'], 2)
        self.assertEqual(result['depth_status'], 'ok')

    def test_shared_fanin_and_multiple_outputs_use_longest_path(self):
        top = module({'a': 2, 'b': 3}, {'y': 7, 'short': 4}, {
            'shared': gate('$_AND_', A=2, B=3, Y=4),
            'left': gate('$_NOT_', A=4, Y=5),
            'right': gate('$_OR_', A=4, B='0', Y=6),
            'join': gate('$_XOR_', A=5, B=6, Y=7)})
        result = measure_netlist({'modules': {'top': top}}, 'top')
        self.assertEqual(result['cell_count'], 4)
        self.assertEqual(result['logic_depth'], 3)
        self.assertEqual(result['maximum_cell_depth'], 3)

    def test_hierarchical_aliases_constants_and_zero_gate_connections(self):
        passthrough = module({'a': 2}, {'y': 2}, {})
        tied = module({}, {'y': '1'}, {})
        top = module({'a': 2}, {'y': 4, 'one': 5}, {
            'pass': instance('pass', a=2, y=3),
            'out': instance('pass', a=3, y=4),
            'tie': instance('tie', y=5)})
        result = measure_netlist({'modules': {'top': top, 'pass': passthrough, 'tie': tied}}, 'top')
        self.assertEqual(result['cell_count'], 0)
        self.assertEqual(result['logic_depth'], 0)
        self.assertEqual(result['depth_issues'], [])

    def test_constant_input_to_child_and_unobserved_gate_depth(self):
        child = module({'a': 2, 'b': 3}, {'y': 4}, {'g': gate('$_AND_', A=2, B=3, Y=4)})
        top = module({'a': 2}, {'y': 2}, {
            'first': instance('child', a=2, b='1', y=3),
            'second': gate('$_NOT_', A=3, Y=4)})
        result = measure_netlist({'modules': {'top': top, 'child': child}}, 'top')
        self.assertEqual(result['cell_count'], 2)
        self.assertEqual(result['logic_depth'], 0)
        self.assertEqual(result['maximum_cell_depth'], 2)

    def test_cycle_and_multiple_driver_fail_closed(self):
        cycle = module({}, {'y': 3}, {'a': gate('$_NOT_', A=3, Y=2),
                                     'b': gate('$_NOT_', A=2, Y=3)})
        conflict = module({'a': 2}, {'y': 3}, {'a': gate('$_NOT_', A=2, Y=3),
                                             'b': gate('$_BUF_', A=2, Y=3)})
        for top, reason in [(cycle, 'combinational dependency cycle'),
                            (conflict, 'multiple drivers or a driven constant/input')]:
            with self.subTest(reason=reason):
                result = measure_netlist({'modules': {'top': top}}, 'top')
                self.assertEqual(result['cell_count'], 2)
                self.assertIsNone(result['logic_depth'])
                self.assertIsNone(result['maximum_cell_depth'])
                self.assertIn(reason, result['depth_issues'])

    def test_unknown_cells_values_inout_and_undriven_are_not_depth_zero(self):
        top = module({'a': 2}, {'y': 3}, {'g': gate('$_NOT_', A=2, Y=3)})
        variations = []
        unknown = deepcopy(top); unknown['cells']['g']['type'] = '$_DFF_P_'
        variations.append(unknown)
        x = deepcopy(top); x['cells']['g']['connections']['A'] = ['x']; variations.append(x)
        inout = deepcopy(top); inout['ports']['a']['direction'] = 'inout'; variations.append(inout)
        undriven = deepcopy(top); undriven['cells']['g']['connections']['A'] = [999]
        variations.append(undriven)
        missing = deepcopy(top); del missing['cells']['g']['port_directions']['A']; variations.append(missing)
        for variation in variations:
            with self.subTest(module=variation):
                result = measure_netlist({'modules': {'top': variation}}, 'top')
                self.assertEqual(result['cell_count'], 1)
                self.assertEqual(result['depth_status'], 'unsupported')
                self.assertIsNone(result['logic_depth'])
                self.assertTrue(result['depth_issues'])

    def test_blackboxes_count_as_leaves_and_recursive_hierarchy_rejected(self):
        blackbox = module({'a': 2}, {'y': 3}, {})
        blackbox['attributes'] = {'blackbox': '0001'}
        top = module({'a': 2}, {'y': 3}, {'u': instance('external', a=2, y=3)})
        result = measure_netlist({'modules': {'top': top, 'external': blackbox}}, 'top')
        self.assertEqual(result['cell_types'], {'external': 1})
        self.assertIsNone(result['logic_depth'])
        top['cells']['u']['type'] = 'top'
        with self.assertRaisesRegex(NetlistMetricError, 'recursive'):
            measure_netlist({'modules': {'top': top}}, 'top')

    def test_hierarchy_port_width_mismatch_rejected(self):
        child = module({'a': 2}, {'y': 2}, {})
        top = module({'a': 2}, {'y': 3}, {'u': instance('child', a=2, y=3)})
        top['cells']['u']['connections']['a'].append('0')
        with self.assertRaisesRegex(NetlistMetricError, 'width mismatch'):
            measure_netlist({'modules': {'top': top, 'child': child}}, 'top')

    def test_only_pinless_scopeinfo_is_ignored_and_electrical_pins_are_rejected(self):
        metadata = {'type': '$scopeinfo', 'parameters': {'TYPE': 'module'},
                    'attributes': {'module': 'child'}, 'connections': {}, 'port_directions': {}}
        top = module({'a': 2}, {'y': 3}, {'g': gate('$_NOT_', A=2, Y=3), 'scope': metadata})
        result = measure_netlist({'modules': {'top': top}}, 'top')
        self.assertEqual(result['cell_types'], {'$_NOT_': 1})
        self.assertEqual(result['cell_count'], 1)
        self.assertEqual(result['logic_depth'], 1)
        for changes in ({'connections': {'A': [2]}, 'port_directions': {'A': 'input'}},
                        {'connections': {'A': []}}, {'port_directions': {'A': 'input'}}):
            with self.subTest(changes=changes):
                invalid = deepcopy(top)
                invalid['cells']['scope'].update(changes)
                with self.assertRaisesRegex(NetlistMetricError, 'scopeinfo.*electrical pins'):
                    measure_netlist({'modules': {'top': invalid}}, 'top')
        # Do not accidentally ignore every pinless unknown internal cell.
        metadata['type'] = '$unknown_metadata'
        result = measure_netlist({'modules': {'top': top}}, 'top')
        self.assertEqual(result['cell_count'], 2)
        self.assertEqual(result['depth_status'], 'unsupported')

    @unittest.skipUnless(shutil.which('yosys'), 'stock Yosys not installed')
    def test_stock_flatten_output_counts_gates_but_not_scope_metadata(self):
        source = '''module child(input a, b, output y);
assign y = a & b;
endmodule
module top(input a,b,c,d, output y,z);
child first(a,b,y);
child second(c,d,z);
endmodule
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'design.v').write_text(source)
            command = ['yosys', '-Q', '-T', '-p',
                       'read_verilog design.v; synth -top top -flatten; write_json netlist.json']
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            netlist = json.loads((root / 'netlist.json').read_text())
            self.assertEqual(sum(c['type'] == '$scopeinfo' for c in netlist['modules']['top']['cells'].values()), 2)
            metrics = measure_netlist(root / 'netlist.json', 'top')
            self.assertEqual(metrics['cell_count'], 2)
            self.assertEqual(metrics['cell_types'], {'$_AND_': 2})
            self.assertEqual(metrics['logic_depth'], 1)
            self.assertEqual(metrics['depth_status'], 'ok')

    def test_file_input_records_bytes_without_mutating_mapping(self):
        data = {'modules': {'top': module({'a': 2}, {'y': 2}, {})}}
        original = deepcopy(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'netlist.json'
            path.write_text(json.dumps(data))
            result = measure_netlist(path, 'top')
            self.assertEqual(result['netlist_bytes'], path.stat().st_size)
            self.assertEqual(result['logic_depth'], 0)
        measure_netlist(data, 'top')
        self.assertEqual(data, original)


if __name__ == '__main__':
    unittest.main()
