"""EPFL cases must be frozen, observable edits without planner hints."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from incremental import epfl
from yosys_flow.common import digest


SOURCE = '''module top(a, b, c, y, z);
input a, b, c;
output y, z;
wire n1, n2;
assign n1 = a & b;
assign n2 = n1 & c;
assign y = n2;
assign z = n1;
endmodule
'''


def fixture(directory, new=SOURCE.replace('n1 & c', 'n1 | c')):
    directory.mkdir()
    (directory / 'base.v').write_text(SOURCE)
    (directory / 'new.v').write_text(new)
    return {'top': 'top', 'base_source': str(directory / 'base.v'),
            'new_source': str(directory / 'new.v'),
            'source_hashes': {'base': digest(directory / 'base.v'), 'new': digest(directory / 'new.v')}}


def wave(inputs=None, **values):
    bits = {'in_a': 0, 'in_b': 0, 'in_c': 1, 'gold_y': 0, 'gold_z': 0,
            'gate_y': 1, 'gate_z': 0, 'trigger': 1}
    bits.update(inputs or {})
    bits.update(values)
    return {'signal': [{'name': name, 'wave': str(value) + '4'} for name, value in bits.items()]}


class EPFLFixtureTests(unittest.TestCase):
    def test_catalog_freezes_two_functional_mutations_per_original_circuit(self):
        self.assertEqual(len(epfl.select_cases()), 6)
        for circuit in ('adder', 'bar', 'arbiter'):
            cases = [epfl.CASES[f'epfl_{circuit}_{kind}'] for kind in ('local', 'shared')]
            self.assertEqual(cases[0]['upstream_file'], cases[1]['upstream_file'])
            self.assertEqual(cases[0]['mutation']['reachable_output_count'], 1)
            self.assertGreater(cases[1]['mutation']['reachable_output_count'], 1)
            for case in cases:
                self.assertEqual(case['family'], 'epfl_mutated')
                self.assertEqual(case['expected_change'], 'functional_change')
                mutation = case['mutation']
                self.assertEqual(mutation['original'].replace('&', '|', 1), mutation['replacement'])
        for requested in ([], ['unknown'], ['epfl_bar_local', 'epfl_bar_local']):
            with self.assertRaises(ValueError):
                epfl.select_cases(requested)

    def test_topology_selection_and_scalar_evaluation(self):
        network = epfl._network(SOURCE)
        targets = epfl._ranked_targets(network)
        self.assertEqual(targets['local']['target'], 'n2')
        self.assertEqual(targets['local']['reachable_output_count'], 1)
        self.assertEqual(targets['shared']['target'], 'n1')
        self.assertEqual(targets['shared']['reachable_output_count'], 2)
        self.assertEqual(targets['shared']['direct_users'], 2)
        self.assertEqual(epfl._evaluate(network, {'a': 1, 'b': 1, 'c': 0}), {'y': 0, 'z': 1})
        escaped = SOURCE.replace(' a', ' \\a[0] ').replace('(a,', '(\\a[0] ,')
        network = epfl._network(escaped)
        self.assertEqual(epfl._evaluate(network, {'a[0]': 1, 'b': 1, 'c': 0}), {'y': 0, 'z': 1})

    def test_parser_rejects_cycles_unknowns_multiple_drivers_and_non_binary_witness(self):
        for source in (SOURCE.replace('a & b', 'n2 & b'), SOURCE.replace('a & b', 'missing & b'),
                       SOURCE.replace('assign y = n2;', 'assign n1 = n2;'),
                       SOURCE.replace('n1 & c', 'n1 ? a : c')):
            with self.assertRaises(ValueError):
                epfl._network(source)
        for inputs in ({'a': 1, 'b': 0}, {'a': 1, 'b': 'x', 'c': 0}):
            with self.assertRaises(ValueError):
                epfl._evaluate(epfl._network(SOURCE), inputs)

    def test_prepare_preserves_base_bytes_and_changes_only_one_operator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / 'cache'
            cache.mkdir()
            (cache / 'source.v').write_text(SOURCE)
            (cache / 'LICENSE').write_text('Test fixture only\n')
            target = epfl._ranked_targets(epfl._network(SOURCE))['local']
            changed = SOURCE.replace('n1 & c', 'n1 | c')
            case = {'circuit': 'tiny', 'variant': 'local', 'upstream_file': 'source.v',
                    'mutation': target, 'new_source_sha256': hashlib.sha256(changed.encode()).hexdigest(),
                    'family': 'epfl_mutated', 'category': 'functional', 'expected_change': 'functional_change'}
            with patch.object(epfl, 'CACHE', cache), patch.object(epfl, 'CASES', {'tiny': case}), \
                    patch.object(epfl, 'verify_corpus', return_value={'files': {'source.v': digest(cache / 'source.v')}}):
                result = epfl.prepare('tiny', root / 'fixture')
                self.assertEqual(Path(result['base_source']).read_text(), SOURCE)
                self.assertEqual(Path(result['new_source']).read_text(), changed)
                differences = [i for i, (old, new) in enumerate(zip(SOURCE, changed)) if old != new]
                self.assertEqual(len(differences), 1)
                self.assertEqual(result['top'], 'top')
                self.assertEqual(result['baseline_key'], 'epfl_tiny')
                self.assertNotIn('mutation', result)
                self.assertNotIn('selector', result)
                self.assertNotIn('patch', result)
                with self.assertRaises(FileExistsError):
                    epfl.prepare('tiny', root / 'fixture')
                altered = deepcopy(case)
                altered['mutation']['line'] += 1
                with patch.object(epfl, 'CASES', {'tiny': altered}), self.assertRaises(ValueError):
                    epfl.prepare('tiny', root / 'altered')

    def test_corpus_must_match_locked_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / 'lock.json'
            lock.write_text(json.dumps({'files': {'source.v': hashlib.sha256(SOURCE.encode()).hexdigest()}}))
            with patch.object(epfl, 'LOCK', lock):
                with self.assertRaisesRegex(ValueError, 'source missing'):
                    epfl.verify_corpus(root)
                (root / 'source.v').write_text(SOURCE)
                epfl.verify_corpus(root)
                (root / 'source.v').write_text(SOURCE + '// corrupted\n')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    epfl.verify_corpus(root)


class EPFLQualificationTests(unittest.TestCase):
    def run_qualification(self, *, model=None, returncode=0, new=None, changed_hash=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture(root / 'fixture', new) if new is not None else fixture(root / 'fixture')
            if changed_hash:
                source['source_hashes']['new'] = 'incorrect'
            def tool(arguments, **kwargs):
                if model is not None:
                    (root / 'qualification/sat-witness.json').write_text(json.dumps(model))
                    kwargs['stdout'].write('SAT solving finished - model found:\n')
                return subprocess.CompletedProcess(arguments, returncode)
            with patch.object(epfl.subprocess, 'run', side_effect=tool) as call:
                result = epfl.qualify(source, root / 'qualification', 'yosys', 5)
            script = (root / 'qualification/qualify.ys')
            return result, script.read_text() if script.exists() else '', call.call_count

    def test_only_full_output_difference_with_independent_replay_qualifies(self):
        result, script, count = self.run_qualification(model=wave())
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['differing_output_count'], 1)
        self.assertEqual(result['source_replay'], 'passed')
        self.assertIn('miter -equiv -make_outputs -flatten', script)
        self.assertIn('-set-def-inputs -show-inputs -show-outputs', script)
        self.assertNotIn('synth ', script)
        self.assertNotIn('abc ', script)
        self.assertEqual(count, 1)

    def test_missing_model_unknowns_or_wrong_output_never_qualify(self):
        for model in (None, wave(trigger=0), wave(in_a='x'), wave(gate_z=1), wave(gold_y=1)):
            result, _, _ = self.run_qualification(model=model)
            self.assertNotEqual(result['status'], 'passed')
        result, _, _ = self.run_qualification(model=wave(), returncode=1)
        self.assertNotEqual(result['status'], 'passed')

    def test_identical_sources_cannot_be_qualified_by_a_fabricated_sat_model(self):
        result, _, _ = self.run_qualification(model=wave(), new=SOURCE)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('no difference', result['reason'])

    def test_source_tampering_is_rejected_before_solver(self):
        result, _, count = self.run_qualification(model=wave(), changed_hash=True)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(count, 0)

    def test_timeout_is_unknown_not_eligible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixture(root / 'fixture')
            with patch.object(epfl.subprocess, 'run', side_effect=subprocess.TimeoutExpired('yosys', 1)):
                result = epfl.qualify(source, root / 'qualification', 'yosys', 1)
            self.assertEqual(result['status'], 'timeout')


if __name__ == '__main__':
    unittest.main()
