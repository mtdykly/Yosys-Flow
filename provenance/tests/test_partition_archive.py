import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import ROOT, digest, TraceError
from yosys_flow.contracts import ContractContext
from yosys_flow.runner import run
from yosys_flow.partition_store import certificates


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built')
class PartitionArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='partition-archive-');cls.base=Path(cls.temp.name)
        cls.source=cls.base/'base.v'
        cls.source.write_text('module top(input a,b,c,d,e,output y,z);\nwire t;\nassign t=a&b;\nassign y=t^c;\nassign z=d|e;\nendmodule\n')
        cls.old=cls.base/'old'
        run(parser().parse_args(['run','--source',str(cls.source),'--top','top','--out',str(cls.old),
                                '--native-yosys',str(NATIVE)]))
        context=ContractContext(cls.old,compact=True);region=context.trace.regions[0]
        target=next(n['cell'] for n in region['input_nodes'] if region['signals'][n['id']]=={'wire':'\\t','offset':0})
        cls.before=digest(cls.old/'manifest.json');cls.derived=cls.base/'derived';cls.log=cls.base/'refine'
        result=subprocess.run([sys.executable,str(ROOT/'provenance/tools/refine_partitions.py'),str(cls.old),
            '--out',str(cls.log),'--input-cell',target,'--publish',str(cls.derived)],capture_output=True,text=True)
        if result.returncode:raise AssertionError(result.stdout+'\n'+result.stderr+'\n'+(cls.log/'traceback.txt').read_text())

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_new_archive_is_authenticated_and_original_unchanged(self):
        from yosys_flow.native import NativeTrace
        with patch.object(NativeTrace,'__init__',side_effect=AssertionError('must not replay')):
            c=ContractContext(self.derived,compact=True)
        self.assertEqual(c.load_timings['verified_index_status'],'hit')
        self.assertEqual(digest(self.old/'manifest.json'),self.before)
        row=certificates(c)[0]
        self.assertTrue(row['proof']['all_external_outputs'])
        self.assertTrue(row['source_proof']['all_outputs'])
        self.assertLess(len(row['final_cell_refs']),len(c.trace.final))

    def test_captured_checkpoint_graphs_preserve_actual_named_outputs(self):
        from yosys_flow.abc_checkpoints import capture
        from yosys_flow.abc_partition import Graph, Partitioner
        c=ContractContext(self.old,compact=True);region=c.trace.regions[0]
        out=self.base/'checkpoints'
        rows=capture(c.directory,region,c.manifest,out,include_rewrites=True)
        original=Graph((c.directory/region['artifacts']['input.blif']).read_text())
        self.assertTrue(rows)
        import gzip
        from yosys_flow.abc_rewrites import install
        metadata=json.loads((out/'checkpoints.json').read_text())['rewrites']
        self.assertEqual(digest(out/metadata['artifact']),metadata['sha256'])
        with gzip.open(out/metadata['artifact'],'rt') as stream:rewrites=json.load(stream)
        for row in rows:
            graph=Graph((out/row['file']).read_text());p=Partitioner(original,graph)
            install(p,rewrites,metadata)
            self.assertTrue(all(p.prove(n,n,0) for n in original.pos),row)

    def test_complete_incremental_uses_partition_and_reuses_neighbor(self):
        from incremental.__main__ import parser as incremental_parser
        from incremental.automatic import run as automatic
        new=self.base/'new.v';new.write_text(self.source.read_text().replace('a&b','a|b'))
        out=self.base/'incremental'
        args=incremental_parser().parse_args(['auto','--base-run',str(self.derived),'--new-source',str(new),
              '--native-yosys',str(NATIVE),'--selection-mode','fine','--no-equivalence-precheck','--out',str(out)])
        result=automatic(args)
        self.assertEqual(result['status'],'passed',result)
        self.assertGreater(result['splice']['reused_cells'],0)

    def test_changed_sqlite_certificate_is_rejected(self):
        import sqlite3
        altered=self.base/'altered';shutil.copytree(self.derived,altered)
        with sqlite3.connect(altered/'verified-index.sqlite3') as db:
            db.execute("UPDATE functional_partitions SET payload='{}'")
        with self.assertRaises(TraceError):ContractContext(altered,compact=True)

    def test_unproved_source_region_cannot_publish_even_with_a_partition_flag(self):
        from copy import deepcopy
        from yosys_flow.partition_archive import publish
        c=ContractContext(self.old,compact=True)
        certificate=deepcopy(certificates(ContractContext(self.derived,compact=True))[0])
        certificate['source_proof']['all_outputs']=False
        out=self.base/'unproved'
        with self.assertRaisesRegex(TraceError,'bound source proof'):
            publish(c,out,[certificate],{})
        self.assertFalse(out.exists())
