"""Replay storage and candidate sharing must preserve selection and evidence."""
import builtins
from copy import deepcopy
import json
import random
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yosys_flow.abc_provenance import ConeMembers, cones, refine
from yosys_flow.common import TraceError
from yosys_flow.graph_index import has_path
from yosys_flow.jsonl_records import JsonlRecords
from yosys_flow.progress import ArchiveProgress
from yosys_flow.native_storage import same_array


class RecordSequenceTests(unittest.TestCase):
    def test_shared_array_identity_skips_scan_but_distinct_arrays_compare(self):
        class NoEquality(list):
            def __eq__(self, other):
                raise AssertionError('shared array compared element by element')
        values = NoEquality(range(1000))
        self.assertTrue(same_array(values, values))
        self.assertTrue(same_array([1,2], [1,2]))
        self.assertFalse(same_array([1,2], [1,3]))

    def test_random_stream_and_copy_access_preserve_all_raw_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rows.jsonl'
            expected = [{'seq': i, 'text': '来源', 'values': [i, i+1]} for i in range(1000)]
            path.write_text('\n'.join(map(json.dumps, expected)))  # also no final newline
            rows = JsonlRecords(path)
            self.assertEqual(rows, expected)
            self.assertEqual(rows[0], expected[0])
            self.assertEqual(rows[-1], expected[-1])
            self.assertEqual(rows[5:10], expected[5:10])
            self.assertEqual(rows[::-7], expected[::-7])
            self.assertEqual(deepcopy(rows), expected)
            self.assertEqual(rows.offsets.itemsize*len(rows.offsets), 8*len(expected))
            rows[0]['values'].append(-1)
            self.assertEqual(rows[0], expected[0])
            with self.assertRaises(IndexError): rows[len(rows)]
            # A file changing between validation and consumption is rejected.
            path.write_text(path.read_text()+'\n{}\n')
            with self.assertRaises(TraceError): list(rows)
            with self.assertRaises(TraceError): rows[0]

    def test_bad_middle_record_is_not_hidden_by_valid_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rows.jsonl'
            path.write_text('{}\nBROKEN\n{}\n')
            with self.assertRaises(json.JSONDecodeError): list(JsonlRecords(path))

    def test_progress_records_phase_transitions_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'progress.json'
            progress = ArchiveProgress(path)
            with patch('sys.stderr'):
                progress('native_replay', records=100)
                progress('abc_candidates')
                progress.finish('failed', error='interrupted')
            saved = json.loads(path.read_text())
            self.assertEqual(saved['status'], 'failed')
            self.assertEqual(saved['stage'], 'abc_candidates')
            self.assertEqual(saved['completed_stages'][0]['stage'], 'native_replay')
            self.assertFalse(saved['completed_stages'][-1]['finished'])
            self.assertFalse(path.with_name(path.name+'.tmp').exists())


class ConeScaleTests(unittest.TestCase):
    def test_packed_unions_equal_independent_set_unions(self):
        rng = random.Random(91)
        names = [f'cell{i}' for i in range(521)]
        rng.shuffle(names)
        before = {i:set(rng.sample(names, rng.randrange(len(names)))) for i in range(20)}
        packed = ConeMembers(before, names)
        for _ in range(100):
            outputs = rng.sample(list(before), rng.randrange(21))
            expected = tuple(sorted(set().union(*(before[i] for i in outputs))))
            self.assertEqual(packed.union(outputs), expected)

    def test_overlapping_member_blocks_preserve_only_the_expected_source_paths(self):
        n, count = 4096, 16
        inputs = [f'in{i}' for i in range(n)]
        before = {i:set(inputs[32*i:]) for i in range(count)}
        region = {'invocation_id':'abc', 'seq':n+1, 'input_nodes':[], 'inputs':inputs}
        events = [{'record_type':'cell_create', 'abc_invocation':'abc', 'abc_node':f'g{i}',
                   'ref':f'out{i}', 'seq':n+2+i, 'precision':'abc_region_candidate', 'origins':list(range(n))}
                  for i in range(count)]
        trace = SimpleNamespace(regions=[region], abc_imports={}, events=events,
            sources={i:{} for i in range(n)}, assignments={}, process_nodes={},
            objects={**{ref:{'origins':[i], 'precision':'cell', 'record_type':'cell_create'}
                        for i,ref in enumerate(inputs)}, **{r['ref']:dict(r) for r in events}},
            edges=[{'from':i, 'to':ref, 'event_id':i+1, 'precision':'cell'} for i,ref in enumerate(inputs)] +
                  [{'from':ref, 'to':'abc', 'event_id':n+1, 'precision':'abc_region_candidate'} for ref in inputs])
        with patch('yosys_flow.abc_provenance.cones', return_value=(before,{f'g{i}':{i} for i in range(count)},None)):
            refine(trace)
            for i in range(count):
                self.assertEqual(trace.objects[f'out{i}']['origins'], list(range(32*i,n)))
                self.assertEqual(set(trace.abc_associations[i]['inputs']), before[i])
            self.assertLess(sum(e.get('abc_member_group', False) for e in trace.edges), n*count//3)
            self.assertTrue(has_path(trace.edges, {0}, 'out0'))
            self.assertFalse(has_path(trace.edges, {0}, 'out1'))
            self.assertTrue(has_path(trace.edges, {n-1}, 'out15'))
            refine(trace, {'abc': {'g0': {'inputs':[inputs[10]], 'precision':'abc_internal_cut'}}})
            self.assertEqual(trace.objects['out0']['origins'], [10])
            self.assertFalse(has_path(trace.edges, {0}, 'out0'))
            self.assertTrue(has_path(trace.edges, {n-1}, 'out15'))

    def test_input_membership_does_not_scan_the_member_list_per_node(self):
        class NoContains(list):
            def __contains__(self, key):
                raise AssertionError('linear member lookup')
        n = 300
        region = {'invocation_id': 'abc', 'inputs': NoContains(f'c{i}' for i in range(1,n)),
                  'input_ports': NoContains([0]), 'output_ports': [n-1],
                  'signals': list(range(n)),
                  'input_nodes': [{'id': i, 'cell': f'c{i}' if i else '', 'fanins': [i-1] if i else []}
                                  for i in range(n)]}
        trace = SimpleNamespace(removals=[{'abc_invocation': 'abc', 'ref': f'c{i}', 'abc_outputs': [i]}
                                         for i in range(1,n)], abc_imports={})
        before, _, reason = cones(trace, region)
        self.assertEqual(before[n-1], set(region['inputs']))
        self.assertEqual(reason, 'unresolved_abc_output')
        region['input_nodes'][-1]['cell'] = 'forged'
        with self.assertRaises(TraceError): cones(trace, region)

    def test_shared_candidates_keep_paths_and_refinement_without_per_gate_sort(self):
        inputs = [f'in{i}' for i in range(64)]
        region = {'invocation_id': 'abc', 'seq': 100, 'input_nodes': [], 'inputs': inputs}
        events = [{'record_type': 'cell_create', 'abc_invocation': 'abc', 'abc_node': f'g{i}',
                   'ref': f'out{i}', 'seq': 101+i, 'precision': 'abc_region_candidate', 'origins': list(range(64))}
                  for i in range(200)]
        edges = [{'from': i, 'to': ref, 'event_id': i+1, 'precision': 'cell'} for i,ref in enumerate(inputs)]
        edges += [{'from': ref, 'to': 'abc', 'event_id': 100, 'precision': 'abc_region_candidate'} for ref in inputs]
        edges += [{'from': 'abc', 'to': e['ref'], 'event_id': e['seq'], 'precision': 'abc_region_candidate'} for e in events]
        trace = SimpleNamespace(regions=[region], abc_imports={}, events=events, edges=edges,
                                sources={i:{} for i in range(64)}, assignments={}, process_nodes={},
                                objects={**{ref:{'origins':[i], 'precision':'cell', 'record_type':'cell_create'}
                                            for i,ref in enumerate(inputs)},
                                         **{e['ref']: dict(e) for e in events}})
        before = deepcopy(events)
        with patch('yosys_flow.abc_provenance.cones', return_value=(
                {7:set(inputs)}, {f'g{i}':{7} for i in range(200)}, None)), \
                patch('yosys_flow.abc_provenance.sorted', wraps=builtins.sorted, create=True) as sorting:
            refine(trace)
            large_sorts = sum(len(c.args[0]) >= 32 for c in sorting.call_args_list)
            self.assertLess(large_sorts, 10)  # independent of 200 equal gate candidate sets
            for e in events:
                self.assertEqual(trace.objects[e['ref']]['origins'], list(range(64)))
                self.assertEqual(trace.objects[e['ref']]['precision'], 'abc_output_cone_candidate')
            self.assertEqual(len({id(a['inputs']) for a in trace.abc_associations}), 1)
            self.assertEqual(sum(e.get('abc_member_group',False) for e in trace.edges), 64)
            self.assertTrue(has_path(trace.edges, {63}, 'out199'))
            # A proven cut replaces only its target; other candidates keep their
            # original coverage, and re-running refinement removes obsolete groups.
            refine(trace, {'abc': {'g0': {'inputs': [inputs[0]], 'precision':'abc_internal_cut'}}})
            self.assertEqual(trace.objects['out0']['origins'], [0])
            self.assertEqual(trace.objects['out1']['origins'], list(range(64)))
            self.assertFalse(has_path(trace.edges, {63}, 'out0'))
            self.assertTrue(has_path(trace.edges, {0}, 'out0'))
        self.assertEqual(events, before)
