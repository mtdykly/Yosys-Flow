"""Whole-design proofs must not turn names, cut assumptions or timeouts into success."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from incremental import netlist_equivalence as eq
from incremental.__main__ import parser
from incremental.failures import classify
from yosys_flow.common import write_json

YOSYS = shutil.which('yosys') or '/opt/oss-cad-suite/bin/yosys'


def module(cells, outputs, inputs=('a', 'b', 'c')):
    return {'attributes': {}, 'netnames': {}, 'cells': cells,
            'ports': {**{n: {'direction': 'input', 'bits': [i + 2]} for i, n in enumerate(inputs)},
                      **{n: {'direction': 'output', 'bits': [b]} for n, b in outputs.items()}}}


def associativity():
    gold = module({'same_name': eq.gate('$_AND_', {'A': 2, 'B': 3}, 5),
                   'out': eq.gate('$_AND_', {'A': 5, 'B': 4}, 6)}, {'y': 6})
    candidate = module({'same_name': eq.gate('$_AND_', {'A': 3, 'B': 4}, 5),
                        'out': eq.gate('$_AND_', {'A': 2, 'B': 5}, 6)}, {'y': 6})
    return gold, candidate


@unittest.skipUnless(Path(YOSYS).is_file(), 'stock Yosys required')
class NetlistEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='netlist-equivalence-')
        self.root = Path(self.temp.name)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def verify(self, a, b, **options):
        self.counter += 1
        base = self.root / str(self.counter)
        base.mkdir()
        for name, m in (('gold', a), ('gate', b)):
            write_json(base / (name + '.json'), {'modules': {'top': m}})
        options = {'fast_sat': False, **options}
        result = eq.verify(base / 'gold.json', base / 'gate.json', 'top', base / 'proof',
                           yosys=YOSYS, options=eq.Options(timeout=20, **options))
        self.last = base
        return result

    def test_renamed_cells_and_bits_and_commuted_inputs_are_structural(self):
        a, _ = associativity()
        b = deepcopy(a)
        b['cells'] = {'renamed_' + n: c for n, c in b['cells'].items()}
        for c in b['cells'].values():
            c['connections']['A'], c['connections']['B'] = c['connections']['B'], c['connections']['A']
            c['connections'] = {p: [v + 100 for v in bs] for p, bs in c['connections'].items()}
        for p in b['ports'].values():
            p['bits'] = [v + 100 for v in p['bits']]
        r = self.verify(a, b)
        self.assertEqual(r['status'], 'equivalent')
        self.assertEqual(r['structural']['matched_outputs'], 1)
        self.assertFalse(r['partitions'])
        self.assertFalse(r['internal_names_assumed_equal'])

    def test_reassociated_logic_uses_sat_with_all_outputs_covered(self):
        a, b = associativity()
        r = self.verify(a, b, abc='none', functional_matching=False)
        self.assertEqual(r['status'], 'equivalent')
        self.assertEqual(r['structural']['matched_outputs'], 0)
        self.assertEqual(r['outputs'][0]['method'], 'independent_cut_sat')
        self.assertTrue(r['all_top_outputs_verified'])

    def test_functional_correspondences_are_proved_before_substitution(self):
        a, b = associativity()
        r = self.verify(a, b)
        self.assertEqual(r['status'], 'equivalent')
        self.assertGreater(r['functional_matching']['proved_classes'], 0)
        self.assertFalse(r['functional_matching']['simulation_is_proof'])
        self.assertFalse(r['partitions'])

    def test_small_design_fast_path_avoids_matching_and_many_tool_startups(self):
        a, b = associativity()
        r = self.verify(a, b, fast_sat=True)
        self.assertEqual(r['status'], 'equivalent')
        self.assertEqual(r['full_design_method'], 'sat_full_design_fast_path')
        self.assertNotIn('functional_matching', r)
        self.assertEqual(len(r['jobs']), 1)

    def test_cyclic_proposed_correspondences_are_proved_as_one_component(self):
        a = module({'f': eq.gate('$_AND_', {'A': 2, 'B': 3}, 5),
                    'g': eq.gate('$_OR_', {'A': 2, 'B': 5}, 6)}, {'f': 5, 'g': 6})
        b = module({'t': eq.gate('$_OR_', {'A': 2, 'B': 4}, 7),
                    'g': eq.gate('$_AND_', {'A': 2, 'B': 7}, 6),
                    'f': eq.gate('$_AND_', {'A': 6, 'B': 3}, 5)}, {'f': 5, 'g': 6})
        r = self.verify(a, b)
        self.assertEqual(r['status'], 'equivalent')
        self.assertGreater(r['functional_matching']['cyclic_components'], 0)
        self.assertTrue(r['all_top_outputs_verified'])

    def test_forced_simulation_collisions_never_prove_wrong_logic(self):
        a, b = associativity()
        b['cells']['out']['type'] = '$_OR_'
        with patch.object(eq.AIG, 'sample', return_value=12345):
            r = self.verify(a, b)
        self.assertEqual(r['status'], 'different')
        self.assertFalse(r['all_top_outputs_verified'])

    def test_abstract_counterexample_expands_instead_of_rejecting_design(self):
        common = {'and': eq.gate('$_AND_', {'A': 2, 'B': 3}, 5),
                  'xor': eq.gate('$_XOR_', {'A': 2, 'B': 3}, 6)}
        a = module({**common, 'sum': eq.gate('$_OR_', {'A': 5, 'B': 6}, 7)}, {'y': 7, 't': 5, 'u': 6})
        b = module({**deepcopy(common), 'sum': eq.gate('$_OR_', {'A': 2, 'B': 3}, 7)}, {'y': 7, 't': 5, 'u': 6})
        r = self.verify(a, b, abc='none', functional_matching=False)
        self.assertEqual(r['status'], 'equivalent')
        self.assertTrue(any(p['abstract_cuts'] and p['status'] == 'different' for p in r['partitions']))
        self.assertTrue(r['all_top_outputs_verified'])

    def test_same_names_wrong_gate_and_secondary_output_are_rejected(self):
        a, _ = associativity()
        a['ports']['secondary'] = {'direction': 'output', 'bits': [5]}
        b = deepcopy(a)
        b['ports']['secondary']['bits'] = [2]
        r = self.verify(a, b)
        self.assertEqual(r['status'], 'different')
        self.assertFalse(r['all_top_outputs_verified'])
        b = deepcopy(a)
        b['cells']['out']['type'] = '$_OR_'
        r = self.verify(a, b)
        self.assertEqual(r['status'], 'different')

    def test_port_order_and_internal_names_are_not_input_binding(self):
        a = module({'mux': eq.gate('$_MUX_', {'A': 2, 'B': 3, 'S': 4}, 5)}, {'y': 5})
        b = deepcopy(a)
        b['ports'] = dict(reversed(list(b['ports'].items())))
        self.assertEqual(self.verify(a, b)['status'], 'equivalent')
        b['ports']['a']['bits'], b['ports']['b']['bits'] = b['ports']['b']['bits'], b['ports']['a']['bits']
        self.assertEqual(self.verify(a, b)['status'], 'different')

    def test_constants_aliases_and_unused_debug_x(self):
        a = module({}, {'zero': '0', 'one': '1', 'alias': 2})
        a['netnames']['unused'] = {'bits': ['x'], 'attributes': {}}
        self.assertEqual(self.verify(a, deepcopy(a))['status'], 'equivalent')
        b = deepcopy(a)
        b['ports']['zero']['bits'] = ['1']
        self.assertEqual(self.verify(a, b)['status'], 'different')

    def test_conflicting_port_and_netname_cannot_change_external_tool_model(self):
        a, b = associativity()
        b['netnames']['y'] = {'bits': [5], 'attributes': {}}
        r = self.verify(a, b)
        self.assertEqual(r['status'], 'invalid')
        self.assertIn('port/netname', r['detail'])
        self.assertFalse(r['all_top_outputs_verified'])

    def test_unsupported_unknown_undriven_duplicate_cycle_and_state_fail_closed(self):
        original, _ = associativity()
        variants = []
        b = deepcopy(original); b['cells']['out']['connections']['A'] = ['x']; variants.append(b)
        b = deepcopy(original); b['cells']['out']['connections']['A'] = [999]; variants.append(b)
        b = deepcopy(original); b['cells']['out']['connections']['Y'] = [2]; variants.append(b)
        b = deepcopy(original); b['cells']['out']['connections']['A'] = [6]; variants.append(b)
        b = deepcopy(original); b['cells']['out']['type'] = '$_DFF_P_'; variants.append(b)
        b = deepcopy(original); b['cells']['out']['connections']['EXTRA'] = [2]; variants.append(b)
        b = deepcopy(original); b['ports']['y']['offset'] = 1; variants.append(b)
        b = deepcopy(original); b['ports'].pop('y'); variants.append(b)
        b = deepcopy(original); b['cells']['loop'] = eq.gate('$_NOT_', {'A': 100}, 100); variants.append(b)
        b = deepcopy(original); b['netnames']['state'] = {'bits': [5], 'attributes': {'init': '0'}}; variants.append(b)
        for b in variants:
            with self.subTest(b=b):
                r = self.verify(original, b)
                self.assertEqual(r['status'], 'invalid')
                self.assertFalse(r['all_top_outputs_verified'])

    def test_no_abc_falls_back_to_residual_sat_on_large_partitions(self):
        a, b = associativity()
        r = self.verify(a, b, abc='none', max_partition_nodes=1, functional_matching=False)
        self.assertEqual(r['status'], 'equivalent')
        self.assertEqual(r['full_design_method'], 'sat_proved_residual')
        self.assertTrue((self.last / 'proof/residual-concrete-sat.ys').exists())

    def test_abc_proves_residual_networks_when_partition_is_limited(self):
        a, b = associativity()
        r = self.verify(a, b, max_partition_nodes=1, functional_matching=False)
        self.assertEqual(r['status'], 'equivalent')
        self.assertEqual(r['full_design_method'], 'abc_cec_proved_residual')

    def test_residual_backends_reuse_proved_functional_matches_and_all_outputs(self):
        # The long AND almost always simulates as zero. Output simulation cannot
        # prove it, but the reassociated abc subfunction has a real local proof.
        names = ('a', 'b', 'c', 'd', *(f'x{i}' for i in range(24)))
        common, previous = {}, 6
        for i in range(23):
            common[f'rare{i}'] = eq.gate('$_AND_', {'A': previous, 'B': 7+i}, 100+i)
            previous = 100+i
        a = module({**common,
            't1': eq.gate('$_AND_', {'A': 2, 'B': 3}, 200),
            't': eq.gate('$_AND_', {'A': 200, 'B': 4}, 201),
            'partial': eq.gate('$_AND_', {'A': 201, 'B': 5}, 202),
            'out': eq.gate('$_AND_', {'A': 202, 'B': previous}, 203),
            'inv': eq.gate('$_NOT_', {'A': 203}, 204)},
            {'y': 203, 'neg': 204, 'same': 2}, inputs=names)
        b = deepcopy(a)
        b['cells'].update({
            't1': eq.gate('$_AND_', {'A': 3, 'B': 4}, 200),
            't': eq.gate('$_AND_', {'A': 2, 'B': 200}, 201),
            'partial': eq.gate('$_AND_', {'A': 5, 'B': previous}, 202),
            'out': eq.gate('$_AND_', {'A': 201, 'B': 202}, 203)})
        for backend in ('none', None):
            with self.subTest(backend=backend):
                r = self.verify(a, b, abc=backend, max_partition_nodes=1)
                self.assertEqual(r['status'], 'equivalent')
                self.assertGreater(r['functional_matching']['proved_classes'], 0)
                self.assertTrue(r['all_top_outputs_verified'])
                self.assertGreater(r['residuals'][0]['abstract_cut_nodes'], 0)
                self.assertLess(r['residuals'][0]['gold_and_nodes'], 10)
                self.assertTrue(r['residuals'][0]['proved_substitutions_reused'])
                binding = json.loads((self.last/'proof'/r['residuals'][0]['bindings']).read_text())
                self.assertEqual(binding['matching_evidence'], r['functional_matching']['sha256'])
                self.assertEqual({tuple(o) for p in r['residual_output_map'] for o in p['outputs']},
                                 {('y', 0), ('neg', 0)})
                # A rare, real functional change must still be rejected.
                wrong = deepcopy(b)
                wrong['cells']['out']['connections']['A'] = ['1']
                failure = self.verify(a, wrong, abc=backend, max_partition_nodes=1)
                self.assertEqual(failure['status'], 'different')
                self.assertFalse(failure['all_top_outputs_verified'])

    def test_residual_abstract_counterexample_restores_correlated_cut_definitions(self):
        common = {'t': eq.gate('$_AND_', {'A': 2, 'B': 3}, 5)}
        a = module({**common, 'u': eq.gate('$_XOR_', {'A': 2, 'B': 3}, 6),
                    'y': eq.gate('$_OR_', {'A': 5, 'B': 6}, 7)}, {'y': 7})
        b = deepcopy(a)
        b['cells']['u']['type'] = '$_OR_'
        for backend in ('none', None):
            with self.subTest(backend=backend):
                r = self.verify(a, b, abc=backend, functional_matching=False, max_partition_nodes=1)
                self.assertEqual(r['status'], 'equivalent')
                abstract, concrete = r['residuals']
                self.assertEqual(abstract['status'], 'different')
                self.assertFalse(abstract['accepted_counterexample'])
                self.assertEqual(concrete['abstract_cut_nodes'], 0)
                self.assertEqual(concrete['status'], 'equivalent')

    def test_residual_timeout_is_unknown_and_keeps_evidence(self):
        a, b = associativity()
        original = eq.yosys_job
        def timeout_job(binary, out, name, script, seconds):
            if name.startswith('residual-'):
                (out/(name+'.log')).write_text('SAT proof finished - no model found: SUCCESS!\n')
                return {'returncode': -15, 'timed_out': True, 'seconds': 0, 'log': name+'.log'}
            return original(binary, out, name, script, seconds)
        with patch.object(eq, 'yosys_job', side_effect=timeout_job):
            r = self.verify(a, b, abc='none', functional_matching=False, max_partition_nodes=1)
        self.assertEqual(r['status'], 'unknown')
        self.assertFalse(r['all_top_outputs_verified'])
        self.assertTrue((self.last/'proof'/r['residuals'][0]['bindings']).is_file())

    def test_hierarchy_flattening_keeps_both_instance_contexts(self):
        leaf = module({'x': eq.gate('$_AND_', {'A': 2, 'B': 3}, 5)}, {'y': 5}, inputs=('a', 'b'))
        inst = lambda a, b, y: {'type': 'leaf', 'parameters': {}, 'attributes': {},
            'port_directions': {'a': 'input', 'b': 'input', 'y': 'output'},
            'connections': {'a': [a], 'b': [b], 'y': [y]}}
        top = module({'u0': inst(2, 3, 5), 'u1': inst(3, 4, 6)}, {'y': 5, 'z': 6})
        flat = module({'g0': eq.gate('$_AND_', {'A': 2, 'B': 3}, 5),
                       'g1': eq.gate('$_AND_', {'A': 3, 'B': 4}, 6)}, {'y': 5, 'z': 6})
        write_json(self.root/'a.json', {'modules': {'top': top, 'leaf': leaf}})
        write_json(self.root/'b.json', {'modules': {'top': flat}})
        r = eq.verify(self.root/'a.json', self.root/'b.json', 'top', self.root/'hier', yosys=YOSYS)
        self.assertEqual(r['status'], 'equivalent')
        self.assertEqual(len(r['outputs']), 2)
        self.assertTrue(any(j['log'] == 'gold-flatten.log' for j in r['jobs']))

    def test_deep_network_has_no_python_recursion_limit(self):
        count = 2500
        cells = {}
        previous = 2
        for i in range(count):
            output = count + 10 + i
            cells[str(i)] = eq.gate('$_AND_', {'A': previous, 'B': i + 3}, output)
            previous = output
        a = module(cells, {'y': previous}, inputs=tuple('p'+str(i) for i in range(count+1)))
        r = self.verify(a, deepcopy(a))
        self.assertEqual(r['status'], 'equivalent')

    def test_shared_total_budget_and_timeout_are_unknown(self):
        a, b = associativity()
        self.verify(a, b)
        r = eq.verify(self.last/'gold.json', self.last/'gate.json', 'top', self.root/'timeout',
                      yosys=YOSYS, options=eq.Options(timeout=0.000001))
        self.assertEqual(r['status'], 'unknown')
        self.assertFalse(r['all_top_outputs_verified'])
        with self.assertRaises(eq.EquivalenceError) as failure:
            eq.require_equivalent('whole-equivalence', r, self.root/'timeout/report.json')
        self.assertEqual(classify('whole_design_equivalence', failure.exception)['code'], 'equivalence_unknown')

    def test_changed_input_file_cannot_receive_valid_result(self):
        a, b = associativity()
        original = eq.partition_proofs
        def mutate(*args, **kwargs):
            state = original(*args, **kwargs)
            path = Path(args[5]).parent/'gate.json'
            path.write_text(path.read_text() + ' ')
            return state
        with patch.object(eq, 'partition_proofs', side_effect=mutate):
            r = self.verify(a, b)
        self.assertEqual(r['status'], 'invalid')
        self.assertFalse(r['all_top_outputs_verified'])

    def test_all_gate_lowerings_against_independent_yosys_sat_model(self):
        # This does not use the verifier to validate its own gate semantics.
        # Original generic gates and our AIG encoding are compared by Yosys SAT.
        for kind, pins in eq.GATE_INPUTS.items():
            with self.subTest(kind=kind):
                path = self.root/kind.strip('$_')
                path.mkdir()
                a = module({'original': eq.gate(kind, {p: i+2 for i,p in enumerate(pins)}, len(pins)+2)},
                           {'trigger': len(pins)+2}, inputs=tuple(pins))
                manager = eq.AIG(); deadline = eq.Deadline(20)
                root = eq.encode(a, manager, deadline)['trigger', 0]
                cone = manager.partition(root, set(), set(), 1000, deadline)
                b = manager.miter_json(root, cone)['modules']['miter']
                # AIG input names identify exact primary-input bindings.
                a['ports'] = {('v_'+str(manager.variable((n,0)).literal>>1) if p['direction']=='input' else n): p
                              for n,p in a['ports'].items()}
                write_json(path/'a.json', {'modules': {'top': a}})
                write_json(path/'b.json', {'modules': {'top': b}})
                script = '\n'.join(eq.source_commands(path/'a.json', path/'b.json', 'top') + [
                    'miter -equiv -flatten gold gate miter', 'hierarchy -top miter',
                    'techmap -map +/simcells.v', 'opt_expr -keepdc',
                    'sat -verify -prove trigger 0 -set-def-inputs'])
                job = eq.yosys_job(YOSYS, path, 'independent', script, 10)
                self.assertEqual(eq.proof_result(path, job, 'sat'), 'equivalent', (path/'independent.log').read_text()[-1500:])

    def test_residual_blif_against_independent_yosys_gate_models(self):
        from incremental.equivalence_residual import write_blif
        for kind, pins in eq.GATE_INPUTS.items():
            with self.subTest(kind=kind):
                path = self.root/kind.strip('$_')
                path.mkdir()
                a = module({'original': eq.gate(kind, {p: i+2 for i,p in enumerate(pins)}, len(pins)+2)},
                           {'y': len(pins)+2}, inputs=tuple(pins))
                manager = eq.AIG(); deadline = eq.Deadline(20)
                root = eq.encode(a, manager, deadline)['y', 0]
                roots = [root, root ^ 1, 0, 1]
                cells, inputs = manager.cone(roots, set(), None, deadline)
                write_blif(path/'gate.blif', manager, roots, cells, inputs, deadline)
                a['cells']['inv'] = eq.gate('$_NOT_', {'A': len(pins)+2}, len(pins)+3)
                a['ports'] = {**{f'n{manager.variable((p,0)).literal>>1}':
                                {'direction':'input','bits':[i+2]} for i,p in enumerate(pins)},
                             **{f'o{i}': {'direction':'output','bits':[b]}
                                for i,b in enumerate((len(pins)+2, len(pins)+3, '0', '1'))}}
                write_json(path/'gold.json', {'modules': {'gold': a}})
                script = ('read_json gold.json\nread_blif gate.blif\nrename residual gate\n'
                          'miter -equiv -flatten gold gate miter\nhierarchy -top miter\n'
                          'techmap -map +/simcells.v\nopt_expr -keepdc\n'
                          'sat -verify -prove trigger 0 -set-def-inputs')
                job = eq.yosys_job(YOSYS, path, 'blif-oracle', script, 10)
                self.assertEqual(eq.proof_result(path, job, 'sat'), 'equivalent',
                                 (path/'blif-oracle.log').read_text()[-1500:])


class ProofResultTests(unittest.TestCase):
    def test_no_success_from_exit_zero_missing_output_or_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            job = {'returncode': 0, 'timed_out': False, 'log': 'tool.log'}
            for text in ('', 'Reading network from file has failed.\n',
                         'Networks are equivalent.\nERROR: invalid file\n',
                         'Networks are equivalent.\nNetworks are NOT EQUIVALENT.\n'):
                (out/'tool.log').write_text(text)
                self.assertEqual(eq.proof_result(out, job, 'cec'), 'unknown')
            (out/'tool.log').write_text('Networks are equivalent. Time = 0.01 sec\n')
            self.assertEqual(eq.proof_result(out, job, 'cec'), 'equivalent')
            job['timed_out'] = True
            self.assertEqual(eq.proof_result(out, job, 'cec'), 'unknown')

    def test_process_group_is_stopped_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            payload = 'import time,pathlib;time.sleep(0.8);pathlib.Path("escaped").write_text("bad")'
            code = 'import subprocess,time;subprocess.Popen('+repr([sys.executable, '-c', payload])+');time.sleep(10)'
            r = eq.run_tool([sys.executable, '-c', code], out, 'kill', 0.2)
            self.assertTrue(r['timed_out'])
            time.sleep(1)
            self.assertFalse((out/'escaped').exists())

    def test_cli_standalone_verify_does_not_require_old_archive(self):
        args = parser().parse_args(['verify-netlists', '--gold', 'a.json', '--gate', 'b.json',
                                    '--top', 'top', '--out', 'proof', '--abc', 'none'])
        self.assertEqual(args.timeout, 1200)
        self.assertEqual(args.abc, 'none')
        auto = parser().parse_args(['auto', '--base-run', 'old', '--new-source', 'new.v', '--out', 'out'])
        self.assertEqual(auto.whole_equivalence_timeout, 1200)
        self.assertEqual(auto.yosys_timeout, 120)


if __name__ == '__main__':
    unittest.main()
