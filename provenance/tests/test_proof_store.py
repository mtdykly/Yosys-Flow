import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yosys_flow.abc_partition import Graph, Partitioner, WindowLimit
from yosys_flow.common import digest, TraceError
from yosys_flow.boolean import ProofBudgetExceeded
from yosys_flow.proof_store import Writer, Reader, StoredPartitioner


class ProofStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='proof-store-')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def store(self, first, final, public, engine=None):
        root = self.directory
        (root/'manifest.json').write_text('{}')
        (root/'input.blif').write_text(first)
        (root/'output.blif').write_text(final)
        context = SimpleNamespace(directory=root, baseline={'manifest_sha256': digest(root/'manifest.json')},
             manifest={'artifacts': {n: {'sha256': digest(root/n)} for n in ('input.blif', 'output.blif')}})
        engine = engine or Partitioner(Graph(first), Graph(final))
        count = max(int(n[5:]) for n in engine.a if n.startswith('ys__n'))+1
        region = {'invocation_id': 'abc1', 'signals': [{'wire': ('\\' if i in public else '$')+str(i), 'offset': 0}
                                                    for i in range(count)],
                  'input_nodes': [], 'artifacts': {n: n for n in ('input.blif', 'output.blif')}}
        writer = Writer(context, root/'proofs')
        writer.add(region, engine)
        writer.finish()
        reader = Reader(context, root/'proofs')
        self.addCleanup(reader.close)
        return context, engine, reader

    def test_lazy_small_target_in_large_network_and_repeated_queries(self):
        gates = ['.names a b ys__n0\n11 1', '.names ys__n0 c ys__n1\n10 1\n01 1']
        gates += [f'.names a d ys__n{i}\n11 1' for i in range(2, 3002)]
        text = '.model large\n.inputs a b c d\n.outputs ys__n1\n'+'\n'.join(gates)+'\n.end\n'
        _, original, reader = self.store(text, text, range(3002))
        with patch.object(Partitioner, '__init__', side_effect=AssertionError('global normalization repeated')):
            disk = StoredPartitioner(reader, 'abc1')
            result = disk.region(['ys__n1'])
        self.assertEqual(result, original.region(['ys__n1'], source_nodes=set(original.a)))
        self.assertEqual(result['export_nodes'], ['ys__n1'])
        count = reader.metrics['node_rows_read']
        self.assertLess(count, 30)
        self.assertEqual(disk.region(['ys__n1']), result)
        self.assertEqual(reader.metrics['node_rows_read'], count)

    def test_complementary_boundary_absorbs_inverter_and_all_side_outputs(self):
        header = '.model t\n.inputs a b c d\n.outputs ys__n5 ys__n0\n'
        a = header+('.names a b ys__n0\n1- 1\n-1 1\n.names ys__n0 ys__n1\n0 1\n'
                    '.names ys__n1 c ys__n2\n11 1\n.names ys__n0 d ys__n3\n11 1\n'
                    '.names ys__n2 ys__n3 ys__n5\n1- 1\n-1 1\n.end\n')
        b = header+('.names a b k\n00 1\n.names k ys__n0\n0 1\n.names k c t\n11 1\n'
                    '.names ys__n0 d w\n11 1\n.names t w ys__n5\n1- 1\n-1 1\n.end\n')
        _, engine, reader = self.store(a, b, [0])
        result = StoredPartitioner(reader, 'abc1').region(['ys__n2'])
        self.assertEqual(result, engine.region(['ys__n2'], source_nodes={'ys__n0'}))
        self.assertEqual({p['export'] for p in result['outputs']}, {'ys__n0', 'ys__n5'})
        self.assertTrue(any(p['inverted'] for p in result['boundary']))

    def test_proved_nonstructural_relation_survives_without_resolving(self):
        h = '.model t\n.inputs a b c d\n.outputs ys__n3\n'
        a = h+('.names a b ys__n0\n11 1\n.names a c ys__n1\n11 1\n'
               '.names ys__n0 ys__n1 ys__n2\n1- 1\n-1 1\n.names ys__n2 d ys__n3\n11 1\n.end\n')
        b = h+'.names b c t\n1- 1\n-1 1\n.names a t u\n11 1\n.names u d ys__n3\n11 1\n.end\n'
        engine = Partitioner(Graph(a), Graph(b))
        self.assertTrue(engine.prove('ys__n2', 'u', 0))
        _, _, reader = self.store(a, b, [2, 3], engine)
        disk = StoredPartitioner(reader, 'abc1')
        self.assertEqual(disk.a['ys__n2'], disk.b['u'])
        result = disk.region(['ys__n3'])
        self.assertEqual(result['export_nodes'], ['ys__n3'])
        self.assertEqual(result, engine.region(['ys__n3'], source_nodes={'ys__n2', 'ys__n3'}))

    def test_unknown_relation_and_limit_do_not_become_proofs(self):
        h = '.model t\n.inputs a b c d\n.outputs ys__n1\n'
        a = h+'.names a b ys__n0\n11 1\n.names ys__n0 c ys__n1\n11 1\n.end\n'
        b = h+'.names a b ys__n0\n11 1\n.names ys__n0 c ys__n1\n1- 1\n-1 1\n.end\n'
        _, _, reader = self.store(a, b, [0, 1])
        with self.assertRaises((TraceError, ProofBudgetExceeded)):
            StoredPartitioner(reader, 'abc1').region(['ys__n1'], limit=1)
        with self.assertRaises((TraceError, ProofBudgetExceeded)):
            StoredPartitioner(reader, 'abc1').region(['ys__n1'])

    def test_database_tamper_wrong_baseline_and_wrong_implementation_rejected(self):
        text = '.model t\n.inputs a b\n.outputs ys__n0\n.names a b ys__n0\n11 1\n.end\n'
        context, _, reader = self.store(text, text, [0])
        wrong = SimpleNamespace(baseline={'manifest_sha256': 'wrong'})
        with self.assertRaisesRegex(TraceError, 'another baseline'): Reader(wrong, reader.directory)
        with patch('yosys_flow.proof_store.implementation', return_value={}):
            with self.assertRaisesRegex(TraceError, 'implementation changed'): Reader(context, reader.directory)
        with sqlite3.connect(reader.directory/'functions.sqlite3') as db:
            db.execute('UPDATE nodes SET literal=99')
        with self.assertRaisesRegex(TraceError, 'hash mismatch'): Reader(context, reader.directory)


from test_native import NATIVE


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built')
class ProofLibraryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from yosys_flow.cli import parser
        from yosys_flow.runner import run
        from yosys_flow.prepare_evidence import prepare
        cls.tmp = tempfile.TemporaryDirectory(prefix='proof-library-')
        cls.directory = Path(cls.tmp.name)
        cls.source = cls.directory/'old.v'
        cls.source.write_text('module top(input a,b,c,d,e,output y,z);\nwire t;\nassign t=a&b;\nassign y=t^c;\nassign z=d|e;\nendmodule\n')
        cls.old = cls.directory/'old'
        run(parser().parse_args(['run','--source',str(cls.source),'--top','top','--out',str(cls.old),'--native-yosys',str(NATIVE)]))
        cls.before = digest(cls.old/'manifest.json')
        # No edit or new RTL exists at preparation time. Exercise real checkpoint
        # evidence as well as the structural classes in unit tests above.
        cls.preparation = prepare(parser().parse_args(['prepare-evidence',str(cls.old)]))

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def run_change(self, old_text, new_text, suffix):
        from incremental.__main__ import parser
        from incremental.automatic import run
        new = self.directory/(suffix+'.v')
        new.write_text(self.source.read_text().replace(old_text, new_text))
        return run(parser().parse_args(['auto','--base-run',str(self.old),'--new-source',str(new),
            '--native-yosys',str(NATIVE),'--no-equivalence-precheck','--out',str(self.directory/suffix)]))

    def test_two_unknown_edits_use_same_library_and_repeat_reuses_cut_proof(self):
        with patch.object(Partitioner, '__init__', side_effect=AssertionError('online global normalization')):
            a = self.run_change('a&b', 'a|b', 'edit1')
            b = self.run_change('d|e', 'd&e', 'edit2')
        for report in (a,b):
            self.assertEqual(report['status'], 'passed', report.get('failure'))
            self.assertEqual(report['proof_evidence']['metrics']['derived_regions'], 1, report['proof_evidence'])
            self.assertGreater(report['splice']['reused_cells'], 0)
            self.assertGreater(report['timings']['required_old_region_proof_seconds'], 0)
            self.assertGreater(report['timings']['post_generation_validation_seconds'], 0)
        with patch('yosys_flow.source_regions.prove_design', side_effect=AssertionError('old source SAT repeated')):
            again = self.run_change('a&b', 'a|b', 'repeat1')
        self.assertEqual(again['status'], 'passed', again.get('failure'))
        self.assertEqual(again['proof_evidence']['metrics']['cache_hits'], 1)
        self.assertEqual(again['timings']['required_old_region_proof_seconds'], 0)
        self.assertEqual(digest(self.old/'manifest.json'), self.before)
        self.assertFalse(self.preparation['edit_target_used'])

    def test_tampered_cached_certificate_is_rejected(self):
        from yosys_flow.contracts import ContractContext
        from yosys_flow.proof_regions import derive, cache_path
        # Independent copy so test ordering cannot affect the clean library.
        library = self.directory/'tamper-proof'
        shutil.copytree(self.directory/'old.proofs', library)
        context = ContractContext(self.old, compact=True, proof_evidence=library)
        root = next(ref for ref,row in context.trace.objects.items()
                    if row.get('record_type')=='cell_create' and row.get('name','').startswith('$and$'))
        certificate = derive(context, [root])
        self.assertIsNotNone(certificate)
        with sqlite3.connect(cache_path(context.proof_evidence)) as db:
            db.execute("UPDATE certificates SET payload='{}'")
        other = ContractContext(self.old, compact=True, proof_evidence=library)
        with self.assertRaisesRegex(TraceError, 'authentication mismatch'): derive(other,[root])

    def test_refinement_persists_relations_and_marks_target_guided_preparation(self):
        import subprocess
        import sys
        from yosys_flow.common import ROOT
        from yosys_flow.contracts import ContractContext
        from yosys_flow.proof_regions import derive
        out = self.directory/'refinement'
        library = self.directory/'refinement-library'
        result = subprocess.run([sys.executable, str(ROOT/'provenance/tools/refine_partitions.py'), str(self.old),
            '--out', str(out), '--input-wire', 't', '--evidence-out', str(library)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        c = ContractContext(self.old, compact=True, proof_evidence=library)
        row = json.loads((out/'certificate.json').read_text())
        restored = derive(c, row['operation_roots'])
        self.assertEqual(restored, row)
        self.assertEqual(c.proof_evidence.metrics['cache_hits'], 1)
        self.assertFalse(c.proof_evidence.document['preparation_scope'][0]['target_independent'])
        self.assertFalse((library/'after.json').exists())

    def test_target_independent_output_refinement_entry(self):
        from yosys_flow.cli import parser
        from yosys_flow.prepare_evidence import prepare
        result = prepare(parser().parse_args(['prepare-evidence', str(self.old), '--out', str(self.directory/'all-outputs'),
                                              '--refine-outputs']))
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(result['new_rtl_used'])
        self.assertFalse(result['edit_target_used'])

    def test_missing_boundary_reports_stage_without_publishing_a_certificate(self):
        from yosys_flow.contracts import ContractContext
        from yosys_flow.proof_regions import derive
        library = self.directory/'missing-frontier'
        shutil.copytree(self.directory/'old.proofs', library)
        for path in library.glob('regions*.sqlite3'):path.unlink()
        context = ContractContext(self.old, compact=True, proof_evidence=library)
        root = next(ref for ref,row in context.trace.objects.items()
                    if row.get('record_type')=='cell_create' and row.get('name','').startswith('$and$'))
        error = WindowLimit('export', ['ys__n1'], 1, [], set(), set(), ['ys__n0'])
        with patch.object(StoredPartitioner, 'region', side_effect=error):
            self.assertIsNone(derive(context, [root]))
        miss = context.proof_evidence.metrics['misses'][0]
        self.assertEqual(miss['missing_evidence_stage'], 'abc_independent_output_boundary')
        self.assertFalse(miss['proven_safe'])
        self.assertEqual(context._functional_partitions, [])
        self.assertFalse((library/'regions.sqlite3').exists())
