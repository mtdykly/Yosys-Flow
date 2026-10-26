"""Shared native evidence is lossless; factoring preserves source reachability."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import TraceError
from yosys_flow.native import NativeTrace
from yosys_flow.native_storage import Arrays
from yosys_flow.graph_index import has_path
from yosys_flow.runner import run, load_run
from yosys_flow.sqlite_index import store_for


class ArrayTests(unittest.TestCase):
    def test_references_are_typed_preceded_and_do_not_mutate_raw_records(self):
        arrays = Arrays(['shared_native_arrays_v1'])
        source = {'id': 1, 'element_kind': 'origins', 'values': [1, 2]}
        arrays.define(source, {1: {}, 2: {}})
        raw = {'origins': {'$native_array': 1}, 'roles': {'data': {'$native_array': 1}}}
        before = deepcopy(raw)
        row = arrays.decode(raw)
        self.assertIs(row['origins'], row['roles']['data'])
        self.assertEqual(raw, before)
        for value, kind in [({'$native_array': 2}, 'origins'),
                            ({'$native_array': 1}, 'inputs'),
                            ({'$native_array': True}, 'origins'),
                            ({'$native_array': 1, 'extra': 0}, 'origins')]:
            with self.assertRaises(TraceError): arrays.resolve(value, kind)
        with self.assertRaises(TraceError): arrays.define(source, {1: {}, 2: {}})
        with self.assertRaises(TraceError): Arrays([]).define(source, {1: {}, 2: {}})
        with self.assertRaises(TraceError): Arrays(['shared_native_arrays_v1']).define(source, {1: {}})


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built')
class NativeStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='native-shared-')
        cls.root = Path(cls.temp.name)
        source = cls.root/'wide.v'
        source.write_text('module demo(input [39:0] a,b, output [39:0] y);\n' +
                          '\n'.join(f'assign y[{i}] = a[{i}] & b[{i}];' for i in range(40)) + '\nendmodule\n')
        cls.traces = {}
        for mode in ('shared', 'full'):
            out = cls.root/mode
            with patch.dict(os.environ, {'YOSYS_FLOW_NATIVE_FULL_ARRAYS': '1'} if mode == 'full' else {}):
                run(parser().parse_args(['run', '--source', str(source), '--top', 'demo', '--out', str(out)]))
            _, trace, _ = load_run(out, use_index=False)
            cls.traces[mode] = trace

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def test_encoding_replay_selection_and_database_are_lossless(self):
        shared, full = self.traces['shared'], self.traces['full']
        self.assertTrue(any(r['record_type'] == 'shared_array' for r in shared.records))
        self.assertFalse(any(r['record_type'] == 'shared_array' for r in full.records))
        self.assertEqual(shared.final, full.final)
        for ref in shared.final:
            for field in ('state', 'origins', 'precision', 'recorded_origins'):
                self.assertEqual(shared.objects[ref][field], full.objects[ref][field])
        self.assertLess((self.root/'shared/yosys.jsonl').stat().st_size,
                        (self.root/'full/yosys.jsonl').stat().st_size * .9)
        _, cached, _ = load_run(self.root/'shared')
        self.assertEqual(vars(cached), vars(shared))
        self.assertGreater(store_for(cached).connection.execute('SELECT count(*) FROM document_fields').fetchone()[0], 0)
        # Full regional candidates are N-to-M but the stored graph is N+M.
        captured = NativeTrace(self.root/'shared/yosys.jsonl')
        region = captured.regions[0]
        self.assertEqual(sum(e['to'] == region['invocation_id'] for e in captured.edges), len(region['inputs']))
        for ref in shared.final:
            seeds = set(shared.objects[ref]['origins'])
            self.assertTrue(has_path(shared.edges, seeds, ref))
            self.assertEqual({s for s in shared.sources if has_path(shared.edges, {s}, ref)},
                             {s for s in full.sources if has_path(full.edges, {s}, ref)})

    def test_changed_region_members_or_shared_origin_references_are_rejected(self):
        original = self.traces['shared'].records
        for mutation in ('origins', 'inputs', 'forward'):
            rows = deepcopy(original)
            output = next(r for r in rows if r['record_type'] == 'cell_create' and r.get('abc_invocation'))
            if mutation == 'origins': output['origins'] = []
            elif mutation == 'inputs': output['inputs'] = []
            else: output['origins'] = {'$native_array': 999999}
            path = self.root/(mutation+'.jsonl')
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with self.assertRaises(TraceError): NativeTrace(path)
