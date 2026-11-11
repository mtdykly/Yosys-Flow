"""Deep dependency cones keep the old hashes, cuts, cache and cycle checks."""
from copy import deepcopy
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from incremental import automatic, ir_graph, rtlil, statements
from yosys_flow.boolean import BDD
from yosys_flow.common import TraceError
from yosys_flow.contracts import fingerprint
from yosys_flow.region_functions import Circuit, LogicDAG, UnsupportedFunction, verify_outputs
from yosys_flow.source_regions import make_cut


DEPTH = 3500


def graph(text, cls=ir_graph.Graph):
    modules = rtlil.parse(text)
    return cls(modules['demo'], modules)


class RecursiveReference(ir_graph.Graph):
    """Frozen pre-fix evaluator, used only on small fixtures as a hash oracle."""
    def value(self, bit, local=False, root=False):
        if isinstance(bit, str):return fingerprint(['constant', bit])
        if bit in self.inputs or (local and not root and self.wires[bit[0]]['public']):
            return fingerprint(['signal', *bit])
        key = bit, local, root
        if key in self.memo:return self.memo[key]
        if bit in self.aliases:result = self.value(self.aliases[bit], local)
        elif bit in self.drivers:
            kind, obj, offset = self.drivers[bit]
            if kind == 'cell':
                result = fingerprint(['cell', obj['type'], obj['params'], offset,
                    [(p, [self.value(b, local) for b in bits])
                     for p, bits in sorted(obj['ports'].items()) if p != '\\Y']])
            else:result = self.process_value(obj, offset, local, {}, set())
        else:raise AssertionError(bit)
        self.memo[key] = result
        return result

    def process_value(self, process, bit, local, memo, active):
        if bit not in process['actions']:return self.value(bit, local)
        if bit in memo:return memo[bit]
        def val(b):return self.process_value(process, b, local, memo, active)
        result = fingerprint(['ordered_process_actions', [
            [val(rhs), [([val(b) for b in signal], cases) for signal, cases in controls]]
            for rhs, controls in process['actions'][bit]]])
        memo[bit] = result
        return result


def cell(kind, inputs, output):
    return {'type': kind, 'parameters': {}, 'attributes': {},
            'port_directions': {**{p: 'input' for p in inputs}, 'Y': 'output'},
            'connections': {**{p: [b] for p, b in inputs.items()}, 'Y': [output]}}


def chain(depth=DEPTH):
    return {'c'+str(i): cell('$_NOT_', {'A': i+2}, i+3) for i in range(depth)}


class DeepFingerprintTests(unittest.TestCase):
    def test_deep_shared_cells_aliases_cache_and_unchanged_recursion_limit(self):
        limit = sys.getrecursionlimit()
        rows = ['module \\demo', '  wire input 1 \\a']
        previous = '\\a'
        expected = fingerprint(['signal', '\\a', 0])
        for i in range(DEPTH):
            name = '$w'+str(i)
            rows += ['  wire '+name, '  cell $and $c'+str(i),
                     '    connect \\A '+previous, '    connect \\B '+previous,
                     '    connect \\Y '+name, '  end']
            expected = fingerprint(['cell', '$and', [], 0, [('\\A', [expected]), ('\\B', [expected])]])
            previous = name
        for i in range(DEPTH):
            name = '$alias'+str(i)
            rows += ['  wire '+name, '  connect '+name+' '+previous]
            previous = name
        rows += ['  wire output 2 \\y', '  connect \\y '+previous, 'end']
        g = graph('\n'.join(rows))
        with patch.object(ir_graph, 'fingerprint', wraps=fingerprint) as hashes:
            self.assertEqual(g.value(('\\y', 0), root=True), expected)
            self.assertLessEqual(hashes.call_count, DEPTH+2)
            count = hashes.call_count
            self.assertEqual(g.value(('\\y', 0), root=True), expected)
            self.assertEqual(hashes.call_count, count)
        self.assertFalse(g.active)
        self.assertEqual(sys.getrecursionlimit(), limit)

    def test_deep_process_actions_and_graph_transitions(self):
        rows = ['module \\demo', '  wire input 1 \\a', '  wire output 2 \\y']
        rows += ['  wire $t'+str(i) for i in range(DEPTH)]
        rows += ['  process $p']
        previous = '\\a'
        expected = fingerprint(['signal', '\\a', 0])
        for i in range(DEPTH):
            name = '$t'+str(i)
            rows.append('    assign '+name+' '+previous)
            previous = name
            expected = fingerprint(['ordered_process_actions', [[expected, []]]])
        rows += ['    sync always', '      update \\y '+previous, '  end', 'end']
        self.assertEqual(graph('\n'.join(rows)).value(('\\y', 0), root=True), expected)
        # Mutual graph -> process -> graph traversal must also use the stack.
        rows = ['module \\demo', '  wire input 1 \\a']
        previous = '\\a'
        for i in range(DEPTH):
            name = '$w'+str(i)
            rows.append('  wire '+name)
            if i % 2:
                rows += ['  process $p'+str(i), '    sync always',
                         '      update '+name+' '+previous, '  end']
            else:
                rows += ['  cell $not $c'+str(i), '    connect \\A '+previous,
                         '    connect \\Y '+name, '  end']
            previous = name
        rows += ['  wire output 2 \\y', '  connect \\y '+previous, 'end']
        g = graph('\n'.join(rows))
        self.assertEqual(len(g.value(('\\y', 0), root=True)), 64)
        self.assertFalse(g.active)

    def test_small_random_dags_match_old_fingerprints_in_every_mode(self):
        rng = random.Random(8223)
        for _ in range(30):
            rows = ['module \\demo', '  wire input 1 \\a', '  wire input 2 \\b']
            wires = ['\\a', '\\b', "1'0", "1'x"]
            for i in range(25):
                name = ('\\' if i % 3 else '$')+'w'+str(i)
                a,b = rng.choices(wires, k=2)
                rows += ['  wire '+name, '  cell '+rng.choice(['$and', '$or', '$xor'])+' $c'+str(i),
                         '    parameter \\Y_WIDTH 1', '    connect \\B '+b,
                         '    connect \\A '+a, '    connect \\Y '+name, '  end']
                wires.append(name)
            rows.append('end')
            text = '\n'.join(rows)
            actual, reference = graph(text), graph(text, RecursiveReference)
            for bit in actual.wires:
                for local in (False, True):
                    for root in (False, True):
                        self.assertEqual(actual.value((bit, 0), local, root), reference.value((bit, 0), local, root))
            self.assertEqual(actual.memo, reference.memo)

    def test_process_priority_controls_and_alias_root_semantics_match_old(self):
        text = r'''module \demo
  wire input 1 \a
  wire input 2 \b
  wire \t
  wire output 3 \y
  wire output 4 \alias
  wire $tmp
  wire $control
  cell $not $c
    connect \A \a
    connect \Y \t
  end
  process $p
    assign $tmp \a
    assign $control \t
    switch $control
      case 1'1
        assign $tmp \b
      case
        switch \b
          case 1'0
            assign $tmp \t
        end
    end
    sync always
      update \y $tmp
  end
  connect \alias \y
end
'''
        for text in (text, text.replace("case 1'1", "case 1'0")):
            a,b = graph(text), graph(text, RecursiveReference)
            for name in ('\\y', '\\alias'):
                for local in (False, True):
                    for root in (False, True):
                        self.assertEqual(a.value((name, 0), local, root), b.value((name, 0), local, root))

    def test_cycles_and_failures_unwind_graph_and_process_active_sets(self):
        cases = [
            ('  connect \\y $tmp\n  connect $tmp \\y', 'pre-proc dependency'),
            ('  cell $not $c\n    connect \\A \\y\n    connect \\Y \\y\n  end', 'pre-proc dependency'),
            ('  process $p\n    assign $tmp $tmp\n    sync always\n      update \\y $tmp\n  end', 'process temporary'),
            ('  process $p\n    assign $tmp \\y\n    sync always\n      update \\y $tmp\n  end', 'pre-proc dependency'),
            ('  connect \\y $tmp', 'undriven'),
        ]
        for body,reason in cases:
            g = graph('module \\demo\n  wire input 1 \\a\n  wire output 2 \\y\n  wire $tmp\n'+body+'\nend')
            for _ in range(2):
                with self.assertRaisesRegex(ir_graph.Unsupported, reason):g.value(('\\y', 0), root=True)
                self.assertFalse(g.active)
                self.assertNotIn((('\\y', 0), False, True), g.memo)
            self.assertEqual(g.value(('\\a', 0)), fingerprint(['signal', '\\a', 0]))
        process = {'actions': {('t', 0): [(('t', 0), [])]}}
        active,memo = set(),{}
        with self.assertRaisesRegex(ir_graph.Unsupported, 'process temporary'):
            g.process_value(process, ('t', 0), False, memo, active)
        self.assertFalse(active)
        self.assertFalse(memo)


class DeepRegionTests(unittest.TestCase):
    def test_deep_gate_functions_preserve_cache_and_all_output_verification(self):
        cells = chain()
        for manager in (LogicDAG(), BDD()):
            a = manager.variables(1)[0]
            circuit = Circuit(cells, {2: a}, manager)
            self.assertEqual(circuit.value(DEPTH+2), a)
            with patch('yosys_flow.region_functions.gate_function', side_effect=AssertionError('cached gates recomputed')):
                self.assertEqual(circuit.value(DEPTH+1), manager.one ^ a)
            self.assertEqual(len(circuit.values), DEPTH+1)
        boundary = [{'direction': 'input', 'original_bit': 2, 'port': 'a'},
                    {'direction': 'output', 'original_bit': DEPTH+2, 'port': 'y'},
                    {'direction': 'output', 'original_bit': DEPTH+1, 'port': 'z'}]
        result = verify_outputs(cells, deepcopy(cells), boundary)
        self.assertTrue(result['all_outputs_verified'])
        self.assertEqual(len(result['outputs']), 2)
        bad = deepcopy(cells);bad['c0']['type'] = '$_BUF_'
        result = verify_outputs(cells, bad, boundary)
        self.assertFalse(result['all_outputs_verified'])

    def test_region_function_still_rejects_cycles_undriven_x_and_bad_shape(self):
        for cells,reason in (({'c':cell('$_NOT_', {'A':3}, 3)}, 'cycle'), ({}, 'unbound input')):
            c = Circuit(cells, {}, LogicDAG())
            for _ in range(2):
                with self.assertRaisesRegex(TraceError, reason):c.value(3)
        c = Circuit({'c':cell('$_NOT_', {'A':'x'}, 3)}, {}, LogicDAG())
        with self.assertRaisesRegex(UnsupportedFunction, 'non_binary_constant'):c.value(3)
        c.cells['c']['connections']['A'] = ['0', '1']
        with self.assertRaisesRegex(UnsupportedFunction, 'unsupported_gate_shape'):c.value(3)

    def test_deep_source_cut_keeps_all_cells_and_rejects_cycle(self):
        cells = chain()
        aliases = {('top', 'a', 0):2, ('top', 'y', 0):DEPTH+2}
        doc = {'region': {'connections': [], 'recorded_cell_functions':cells},
               'boundary':[{'direction':'input','original_bit':2,'port':'a'},
                           {'direction':'output','original_bit':DEPTH+2,'port':'y'}]}
        with patch('yosys_flow.source_regions.Endpoints') as endpoints:
            endpoints.return_value.bit.side_effect = lambda *key:aliases.get(key)
            design,evidence = make_cut(None, doc, (cells, aliases, {2}))
            self.assertEqual(len(design['modules']['gold']['cells']), DEPTH)
            self.assertEqual(len(evidence), 2)
            manager = LogicDAG();a = manager.variables(1)[0];outputs = []
            for module in design['modules'].values():
                circuit = Circuit(module['cells'], {module['ports']['a']['bits'][0]:a}, manager)
                outputs.append(circuit.value(module['ports']['y__anchor_0']['bits'][0]))
            self.assertEqual(outputs, [a,a])
            cells['c0']['connections']['A'] = [DEPTH+2]
            with self.assertRaisesRegex(TraceError, 'cycle in source cut'):
                make_cut(None, doc, (cells, aliases, {2}))

    def test_deep_new_slice_preserves_cut_and_detects_cycle(self):
        cells = chain()
        context = SimpleNamespace(module={'cells':cells})
        slicer = statements.Slice(context, {}, {}, SimpleNamespace(evidence=[]))
        slicer.units = {'top':(None, SimpleNamespace(module=SimpleNamespace(name='top')))}
        slicer.coarse = {'top':{'cells':cells}}
        slicer.drivers = {('top',i+3):('top','c'+str(i)) for i in range(DEPTH)}
        input_row = {'old_bit':2,'new_bit':2,'unchanged_full_definition':True}
        output_row = {'new_bit':DEPTH+2,'instance':'top','new_function_fingerprint':'same'}
        slicer.by_new['top',2].append(input_row)
        slicer.aliases[DEPTH+2].append(output_row)
        doc = {'contract_id':'test','region':{'cells':[{'name':n} for n in cells]},
               'boundary':[{'direction':'input','original_bit':2,'port':'a'},
                           {'direction':'output','original_bit':DEPTH+2,'port':'y','reason':'cell_output'}]}
        result,missing = slicer.build(doc)
        self.assertEqual(missing, [])
        _,design,_,spec = result
        self.assertEqual(len(design['modules']['__auto_patch']['cells']), DEPTH)
        self.assertEqual(spec['reused_input_cut_count'], 1)
        cells['c0']['connections']['A'] = [DEPTH+2]
        with self.assertRaisesRegex(ir_graph.Unsupported, 'cycle in new local dependency cone'):slicer.build(doc)
        cells['c0']['connections']['A'] = [DEPTH+20]
        with self.assertRaisesRegex(ir_graph.Unsupported, 'no unchanged recorded input cut'):slicer.build(doc)

    def test_support_for_deep_cones_and_hierarchy_keeps_port_bindings(self):
        module = {'cells':chain(), 'ports':{'a':{'direction':'input','bits':[2]},
                                          'alias':{'direction':'input','bits':[2]},
                                          'y':{'direction':'output','bits':[DEPTH+2]}}}
        self.assertEqual(automatic.input_support({'leaf':module}, 'leaf'), {('y',0):{('a',0),('alias',0)}})
        # Also test a module dependency stack, with unused input b and a
        # constant-bound child. Unused inputs must not become dependencies.
        modules = {'m0':{'cells':{}, 'ports':{'A':{'direction':'input','bits':[2]},
                                           'B':{'direction':'input','bits':[3]},
                                           'Y':{'direction':'output','bits':[2]}}}}
        for i in range(1, DEPTH):
            modules['m'+str(i)] = {'cells':{'u':cell('m'+str(i-1), {'A':2, 'B':3}, 4)},
                                  'ports':{'A':{'direction':'input','bits':[2]},
                                           'B':{'direction':'input','bits':[3]},
                                           'Y':{'direction':'output','bits':[4]}}}
        self.assertEqual(automatic.input_support(modules, 'm'+str(DEPTH-1)), {('Y',0):{('A',0)}})
        modules['m1']['cells']['u']['connections']['A'] = ['0']
        self.assertEqual(automatic.input_support(modules, 'm'+str(DEPTH-1)), {('Y',0):set()})
        module['cells']['c0']['connections']['A'] = [DEPTH+2]
        with self.assertRaisesRegex(TraceError, 'cycle in new coarse IR'):
            automatic.input_support({'leaf':module}, 'leaf')
        modules['m1']['cells']['u']['type'] = 'm1'
        with self.assertRaisesRegex(TraceError, 'recursive coarse IR hierarchy'):
            automatic.input_support(modules, 'm1')

    def test_unexpected_failure_saves_traceback_without_publishing_netlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'job'
            args = SimpleNamespace(out=str(out), base_run='unused')
            with patch.object(automatic, 'ContractContext', side_effect=ValueError('diagnostic example')):
                report = automatic.run(args)
            self.assertEqual(report['status'], 'failed')
            self.assertFalse(report['published_incremental_netlist'])
            diagnostic = (out/report['failure']['traceback']).read_text()
            self.assertIn('ValueError: diagnostic example', diagnostic)
            self.assertIn('_run', diagnostic)
