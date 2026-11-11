"""Factored selection, streaming integrity, and recorder audit equivalence."""
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from yosys_flow.common import TraceError, digest, token
from yosys_flow.jsonl_records import LiveJsonlRecords
from yosys_flow.native import NativeTrace
from yosys_flow.source_index import SourceRelation
from yosys_flow.cli import parser
from yosys_flow.runner import run, load_run, query_loaded
from test_native import NATIVE


class SharedSourceTests(unittest.TestCase):
    def test_ordered_selection_matches_expanded_reference_with_overlap_and_copies(self):
        rng = random.Random(10)
        sets = [rng.sample(range(30), rng.randrange(15)) for _ in range(40)]
        rows = [(f'cell{i}', list(sets[rng.randrange(len(sets))])) for i in range(200)]
        expected = {}
        for ref, sources in rows:
            for source in sources: expected.setdefault(source, []).append(ref)
        actual = SourceRelation.build(rows)
        self.assertEqual(dict(actual), expected)
        for _ in range(30):
            seeds = set(rng.sample(range(40), 10))
            self.assertEqual(actual.select(seeds), [r for r,s in rows if seeds.intersection(s)])

    def test_large_shared_region_never_expands_cartesian_product(self):
        class Counted(list):
            count = 0
            def __iter__(self):
                self.count += 1
                return super().__iter__()
        origins = Counted(range(20000))
        relation = SourceRelation.build((f'gate{i}', origins) for i in range(20000))
        # Represents 400 million pairs with 40 thousand memberships.
        self.assertEqual(origins.count, 1)
        self.assertEqual(len(relation.members), 1)
        self.assertEqual(sum(map(len, relation.sources.values())), 20000)
        self.assertEqual(sum(map(len, relation.members.values())), 20000)
        with patch.object(relation, '_expand', wraps=relation._expand) as expand:
            self.assertEqual(len(relation.select(range(1000))), 20000)
            self.assertEqual(len(expand.call_args.args[0]), 1)


class StreamTests(unittest.TestCase):
    def test_partial_batches_are_consumed_before_producer_finishes_and_bind_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'rows.jsonl'
            done, cancelled, consumed = Event(), Event(), Event()
            rows = LiveJsonlRecords(path, done, cancelled)
            result, errors = [], []
            def read():
                try:
                    for row in rows:
                        result.append(row)
                        consumed.set()
                except BaseException as exc: errors.append(exc)
            thread = Thread(target=read); thread.start()
            try:
                with path.open('wb') as stream:
                    stream.write(b'{"seq":1}\n'); stream.flush()
                    self.assertTrue(consumed.wait(5))
                    self.assertFalse(done.is_set())
                    stream.write(b'{"seq":'); stream.flush()
                    stream.write(b'2}\n'); stream.flush()
            finally: done.set(); thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(result, [{'seq':1},{'seq':2}])
            self.assertEqual(rows.stream_sha256, digest(path))
            self.assertEqual(list(rows), result)

    def test_truncated_stream_and_records_after_end_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'rows.jsonl'; done = Event(); done.set()
            path.write_text('{"seq":')
            with self.assertRaisesRegex(TraceError, 'truncated'):
                list(LiveJsonlRecords(path, done, Event()))
            path.write_text('\n'.join(json.dumps(r) for r in [
                {'record_type':'run_begin','seq':1,'step_id':0,'schema_version':2},
                {'record_type':'run_end','seq':2,'step_id':0,'cells':[]},
                {'record_type':'coverage_gap','seq':3,'step_id':0,'reason':'after end'}])+'\n')
            with self.assertRaisesRegex(TraceError, 'after native run_end'):
                NativeTrace(path, records=LiveJsonlRecords(path,done,Event()))


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built')
class ArchiveAuditTests(unittest.TestCase):
    def test_optimized_and_full_scan_capture_select_identical_logic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'source.v'
            source.write_text('''module leaf(input [3:0] a,b, input sel, output reg [3:0] y);
always @* if(sel) y = a + b; else y = a ^ b;
endmodule
module demo(input [3:0] a,b,c, input sel, output [3:0] y,z);
leaf first(a,b,sel,y);
leaf second(b,c,sel,z);
endmodule
''')
            for flatten in (False, True):
                outputs = []
                for audit in (False, True):
                    out = root/f'run-{flatten}-{audit}'
                    env = {'YOSYS_FLOW_STREAM_REPLAY':'0' if audit else '1'}
                    if audit: env['YOSYS_FLOW_NATIVE_AUDIT']='1'
                    with patch.dict(os.environ, env):
                        run(parser().parse_args(['run','--source',str(source),'--top','demo',
                            '--out',str(out),*(['--flatten'] if flatten else [])]))
                    m,t,v = load_run(out)
                    self.assertEqual(v['stock_equivalence_method'], 'internal_name_structural_matching_v1')
                    self.assertNotIn('equiv_simple', (out/'equivalence.ys').read_text())
                    self.assertIn('equiv_make -inames', (out/'equivalence.ys').read_text())
                    self.assertEqual('capture_replay_overlapped' in m['timings_seconds'], not audit)
                    q = query_loaded(parser().parse_args(['query',str(out),'--source',str(source),
                        '--line','2','--instance','demo/first']),m,t)
                    outputs.append((json.loads((out/'after.json').read_text()),
                                    {ref:{k:t.objects[ref][k] for k in ('state','origins','precision')} for ref in t.final},
                                    sorted((c['name'],c['type'],c['precision']) for c in q['final_cells'])))
                    stats = t.records[-1]['recorder_stats']
                    if not audit:
                        self.assertGreater(stats['skipped_checkpoints'], 0)
                        self.assertLess(stats['state_builds'], stats['observe_calls'])
                    else: self.assertEqual(stats['state_builds'], stats['observe_calls'])
                self.assertEqual(outputs[0], outputs[1])

    def test_internal_matching_rejects_changed_gate_with_identical_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root/'check.ys'
            design = root/'pair.il'
            pieces = []
            for name, gate in [('gold','$_AND_'),('gate','$_NAND_')]:
                pieces.append(f"""module \\{name}
  wire input 1 \\a
  wire input 2 \\b
  wire output 3 \\y
  cell {gate} \\same_name
    connect \\A \\a
    connect \\B \\b
    connect \\Y \\y
  end
end
""")
            design.write_text(''.join(pieces))
            script.write_text(f'read_rtlil {token(design)}\n'
                              'equiv_make -inames gold gate equiv\nhierarchy -top equiv\n'
                              'opt_clean\nequiv_status -assert\n')
            result = subprocess.run(['/opt/oss-cad-suite/bin/yosys','-Q','-T','-s',str(script)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unproven', result.stdout+result.stderr)
