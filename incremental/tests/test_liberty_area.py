"""Area must cover every mapped instance and describe a verified implementation."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from incremental import liberty_area
from yosys_flow.common import TraceError, digest


# Small parser/mapper fixture, not an additional benchmark or physical library.
LIBERTY = '''library(test) {
  delay_model : table_lookup;
  cell(INV) { area:1; pin(A) {direction:input;}
    pin(Y) {direction:output; function:"!A";} }
  cell(BUF) { area:2; pin(A) {direction:input;}
    pin(Y) {direction:output; function:"A";} }
  cell(NAND2) { area:3; pin(A) {direction:input;} pin(B) {direction:input;}
    pin(Y) {direction:output; function:"!(A & B)";} }
  cell(NOR2) { area:3; pin(A) {direction:input;} pin(B) {direction:input;}
    pin(Y) {direction:output; function:"!(A | B)";} }
}'''

HIERARCHY = '''module leaf(input a,b,output y); assign y = a & b; endmodule
module top(input a,b,c,d,output y,z); leaf u1(a,b,y),u2(c,d,z); endmodule
'''


class AreaAccountingTests(unittest.TestCase):
    def fixture(self):
        ports = {'A': {'direction': 'input', 'width': 1},
                 'Y': {'direction': 'output', 'width': 1}}
        catalog = {'INV': {'area': 1.5, 'ports': ports}}
        module = {'ports': {'a': {'direction': 'input', 'bits': [2]},
                            'y': {'direction': 'output', 'bits': [3]}},
                  'cells': {'inv': {'type': 'INV', 'parameters': {},
                                   'connections': {'A': [2], 'Y': [3]},
                                   'port_directions': {'A': 'input', 'Y': 'output'}}}}
        mapped = {'modules': {'top': module}}
        stat = {'modules': {'\\top': {'num_cells': 1, 'num_cells_by_type': {'INV': 1}, 'area': 1.5}}}
        return mapped, catalog, stat

    def test_unknown_cells_cannot_yield_a_partial_area_sum(self):
        mapped, catalog, stat = self.fixture()
        mapped['modules']['top']['cells']['unknown'] = {'type': 'UNMAPPED'}
        with self.assertRaisesRegex(TraceError, 'unmapped or unknown'):
            liberty_area._account(mapped, 'top', catalog, stat)

    def test_missing_area_or_incomplete_pin_coverage_is_rejected(self):
        for bad in ('area', 'pins', 'directions'):
            mapped, catalog, stat = self.fixture()
            if bad == 'area':
                catalog['INV']['area'] = None
            elif bad == 'pins':
                del mapped['modules']['top']['cells']['inv']['connections']['A']
            else:
                mapped['modules']['top']['cells']['inv']['port_directions']['A'] = 'output'
            with self.subTest(bad=bad), self.assertRaises(TraceError):
                liberty_area._account(mapped, 'top', catalog, stat)

    def test_stat_must_match_independent_weighted_instance_census(self):
        mapped, catalog, stat = self.fixture()
        self.assertEqual(liberty_area._account(mapped, 'top', catalog, stat), (1.5, {'INV': 1}))
        for field, value in [('area', 2), ('num_cells', 2), ('num_cells_by_type', {'INV': 2})]:
            altered = copy.deepcopy(stat)
            altered['modules']['\\top'][field] = value
            with self.subTest(field=field), self.assertRaises(TraceError):
                liberty_area._account(mapped, 'top', catalog, altered)

    def test_physical_fillers_are_outside_functional_mapping_area(self):
        mapped, catalog, stat = self.fixture()
        mapped['modules']['top']['cells']['filler'] = {'type': 'FILL', 'connections': {}}
        catalog['FILL'] = {'area': 0.5, 'ports': {}}
        with self.assertRaisesRegex(TraceError, 'physical-only'):
            liberty_area._account(mapped, 'top', catalog, stat)


@unittest.skipUnless(shutil.which('yosys'), 'Yosys/ABC required for Liberty mapping')
class LibertyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='liberty-area-test-')
        self.root = Path(self.temp.name)
        (self.root/'tiny.lib').write_text(LIBERTY)
        self.library = liberty_area.prepare_library(self.root/'tiny.lib', self.root/'library', 'yosys')

    def tearDown(self):
        self.temp.cleanup()

    def build(self, text=HIERARCHY):
        (self.root/'input.v').write_text(text)
        result = subprocess.run(['yosys', '-Q', '-T', '-p',
            'read_verilog input.v; synth -top top; write_json input.json'],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        return self.root/'input.json'

    def test_hierarchical_instances_counted_and_both_exports_proven(self):
        netlist = self.build()
        before = digest(netlist)
        result = liberty_area.measure(netlist, 'top', self.library, self.root/'area', 'yosys')
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['cell_area'], 8)
        self.assertEqual(result['cell_counts'], {'INV': 2, 'NAND2': 2})
        self.assertEqual(result['cell_count'], 4)
        self.assertEqual(result['unit'], 'library_area_units')
        self.assertEqual(result['library_sha256'], self.library['sha256'])
        self.assertFalse(result['incremental_standard_cell_reuse'])
        self.assertEqual(set(result['proofs']), {'mapped', 'verilog'})
        self.assertTrue(all(p['status'] == 'passed' for p in result['proofs'].values()))
        self.assertEqual(before, digest(netlist))
        self.assertGreater(result['mapping_seconds'], 0)
        self.assertGreater(result['validation_seconds'], 0)
        self.assertNotIn('stat -', (self.root/'area/mapping.ys').read_text())
        self.assertNotIn('sat ', (self.root/'area/mapping.ys').read_text())
        self.assertTrue(all(digest(p) == h for p, h in self.library['frozen_files'].items()))

    def test_defined_connections_and_constants_have_zero_logic_area(self):
        netlist = self.build("module top(input a,output y,z); assign y=a; assign z=1'b0; endmodule")
        result = liberty_area.measure(netlist, 'top', self.library, self.root/'area', 'yosys')
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['cell_count'], 0)
        self.assertEqual(result['cell_area'], 0)
        self.assertIn('no tie-cell insertion', result['constants'])

    def test_matching_undefined_outputs_are_not_valid_area_evidence(self):
        netlist = self.build("module top(input a,output y); assign y=1'bx; endmodule")
        result = liberty_area.measure(netlist, 'top', self.library, self.root/'area', 'yosys')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure']['stage'], 'validation')
        self.assertIsNone(result['cell_area'])
        self.assertIsNone(result['cell_count'])

    def test_library_and_catalog_tampering_rejected_before_mapping(self):
        netlist = self.build()
        for field in ('path', 'cells_path'):
            path = Path(self.library[field])
            content = path.read_bytes()
            path.write_bytes(content+b'\n')
            result = liberty_area.measure(netlist, 'top', self.library, self.root/field, 'yosys')
            self.assertEqual(result['status'], 'failed', result)
            self.assertIn('hash mismatch', result['failure']['detail'])
            self.assertIsNone(result['cell_area'])
            path.write_bytes(content)

    def test_user_keep_hierarchy_is_preserved_and_residual_hierarchy_declined(self):
        netlist = self.build(HIERARCHY.replace('module leaf', '(* keep_hierarchy *) module leaf'))
        result = liberty_area.measure(netlist, 'top', self.library, self.root/'area', 'yosys')
        self.assertEqual(result['status'], 'failed', result)
        self.assertIsNone(result['cell_area'])
        script = (self.root/'area/mapping.ys').read_text()
        self.assertNotIn('-unset keep', script)

    def test_broken_verilog_readback_never_publishes_precomputed_area(self):
        netlist = self.build()
        verify = liberty_area._verify
        def alter_readback(directory, top, library, yosys, **kwargs):
            if kwargs.get('verilog'):
                (directory/'mapped.v').write_text('module top(input a,b,c,d,output y,z); '
                                                  "assign y=1'b0; assign z=1'b0; endmodule")
            return verify(directory, top, library, yosys, **kwargs)
        with patch.object(liberty_area, '_verify', side_effect=alter_readback):
            result = liberty_area.measure(netlist, 'top', self.library, self.root/'area', 'yosys')
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('verilog-prove', result['failure']['detail'])
        self.assertIsNone(result['cell_area'])
        self.assertIsNone(result['cell_count'])
        report = json.loads((self.root/'area/report.json').read_text())
        self.assertIsNone(report['cell_area'])


if __name__ == '__main__':
    unittest.main()
