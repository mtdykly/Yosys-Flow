"""Both storage backends preserve evidence, selection, export and replacement."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import ROOT, TraceError, digest, write_json
from yosys_flow.contracts import ContractContext
from yosys_flow.diagnostics import group_closure
from yosys_flow.extract import extract
from yosys_flow import gzip_index, persistent_index as pi, sqlite_index
from yosys_flow.index_store import store_for, TABLE_WIDTHS
from yosys_flow.native import NativeTrace
from yosys_flow.runner import load_run, query_loaded, run


@unittest.skipUnless(NATIVE.is_file(), 'native Yosys not built')
class GzipIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='dual-index-')
        cls.root = Path(cls.temp.name)
        cls.source = cls.root/'hierarchy.v'
        cls.source.write_text('''module leaf(input a,b,c,output y,z,direct,zero);
assign y=a&b;
assign z=y^c;
assign direct=a;
assign zero=1'b0;
endmodule
module demo(input a,b,c,d,e,f,output y0,z0,d0,k0,y1,z1,d1,k1);
leaf first(a,b,c,y0,z0,d0,k0);
leaf second(d,e,f,y1,z1,d1,k1);
endmodule
''')
        cls.runs = {}
        for mode,extra in [('retained',[]),('flatten',['--flatten']),('m1',['--flow','m1'])]:
            out=cls.root/mode
            run(parser().parse_args(['run','--source',str(cls.source),'--top','demo','--out',str(out),
                                    '--index-backend','gzip',*extra]))
            cls.runs[mode]=out

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def clone(self,mode='retained'):
        out=self.root/self._testMethodName
        shutil.copytree(self.runs[mode],out)
        return out

    def edit(self,out,callback,*,resign=False):
        path=out/gzip_index.NAME
        data=json.loads(gzip.decompress(path.read_bytes()))
        callback(data)
        path.write_bytes(gzip.compress(pi._json_bytes(data),compresslevel=1,mtime=0))
        manifest=json.loads((out/'manifest.json').read_text())
        sha=digest(path)
        manifest['artifacts'][gzip_index.NAME]={'sha256':sha,'size_bytes':path.stat().st_size}
        manifest['verified_index']['gzip_sha256']=sha
        if resign:
            desc=manifest['verified_index']
            desc['authentication']['signature']=gzip_index.authenticate(pi._authentication_key(),
                {k:v for k,v in desc.items() if k!='authentication'})
        write_json(out/'manifest.json',manifest)

    def selector(self,line=2):
        return dict(source=str(self.source),line=line,end_line=None,column=None,end_column=None,
                    object=None,instance='demo/first')

    def test_default_cli_and_explicit_gzip_are_archived(self):
        self.assertEqual(parser().parse_args(['run','--source','a.v','--top','demo','--out','run']).index_backend,'sqlite')
        for out in self.runs.values():
            manifest=json.loads((out/'manifest.json').read_text())
            self.assertEqual(manifest['configuration']['index_backend'],'gzip')
            self.assertEqual(manifest['verified_index']['schema_version'],2)
            self.assertFalse((out/sqlite_index.NAME).exists())
            self.assertEqual(json.loads(gzip.decompress((out/gzip_index.NAME).read_bytes()))['kind'],gzip_index.KIND)

    def test_queries_and_all_fields_equal_replay_in_all_modes(self):
        for mode,out in self.runs.items():
            with self.subTest(mode=mode):
                timing={};m,cached,_=load_run(out,timing);_,raw,_=load_run(out,use_index=False)
                self.assertEqual(timing['verified_index_status'],'hit')
                self.assertEqual(store_for(cached).backend,'gzip')
                self.assertEqual(vars(cached),vars(raw))
                for line in range(2,6):
                    args=parser().parse_args(['query',str(out),'--source',str(self.source),'--line',str(line),'--instance','demo/first'])
                    self.assertEqual(query_loaded(args,m,cached),query_loaded(args,m,raw))
                    self.assertEqual(query_loaded(args,m,cached,compact=True),query_loaded(args,m,raw,compact=True))
                for ref in raw.final:
                    args=parser().parse_args(['query',str(out),'--object',ref])
                    self.assertEqual(query_loaded(args,m,cached),query_loaded(args,m,raw))

    def test_same_validated_trace_has_identical_rows_in_both_sinks(self):
        import zlib
        out=self.runs['retained'];_,raw,_=load_run(out,use_index=False)
        memory=gzip_index.Sink();from yosys_flow.index_store import populate
        populate(raw,memory)
        con=sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.executescript(sqlite_index.SCHEMA_SQL)
        populate(raw,sqlite_index.Sink(con))
        for table in TABLE_WIDTHS:
            rows=con.execute('SELECT * FROM '+table).fetchall()
            if table=='documents':rows=[(i,h.hex(),zlib.decompress(v).decode()) for i,h,v in rows]
            self.assertEqual(sorted(rows),sorted(memory.tables[table]),table)

    def test_hit_uses_no_sqlite_and_repeats_queries_without_replay(self):
        out=self.runs['retained']
        with patch('sqlite3.connect',side_effect=AssertionError('gzip must not use SQL')), \
             patch.object(NativeTrace,'__init__',side_effect=AssertionError('no raw replay')):
            m,t,_=load_run(out)
            args=parser().parse_args(['query',str(out),'--source',str(self.source),'--line','2'])
            a=query_loaded(args,m,t,compact=True)
            self.assertEqual(query_loaded(args,m,t,compact=True),a)

    def test_builder_writes_gzip_directly_and_shares_large_fields(self):
        source=self.root/'wide.v'
        source.write_text('module demo(input [39:0] a,b,output [39:0] y);\n'+
                          '\n'.join(f'assign y[{i}]=a[{i}]&b[{i}];' for i in range(40))+'\nendmodule\n')
        out=self.root/'wide'
        with patch('sqlite3.connect',side_effect=AssertionError('no temporary database')):
            run(parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out),'--index-backend','gzip']))
            _,trace,_=load_run(out)
        data=json.loads(gzip.decompress((out/gzip_index.NAME).read_bytes()))['tables']
        self.assertTrue(data['document_fields'])
        counts={}
        for _,_,child in data['document_fields']:counts[child]=counts.get(child,0)+1
        self.assertGreater(max(counts.values()),1)
        self.assertEqual(len(trace.final),40)

    def test_contract_group_closure_and_export_match_raw_evidence(self):
        out=self.root/'joint'
        run(parser().parse_args(['run','--source',str(ROOT/'provenance/examples/multilevel.v'),
                                '--top','demo','--out',str(out),'--index-backend','gzip']))
        context=ContractContext(out,compact=True);_,raw,_=load_run(out,use_index=False)
        saw_group=False
        for ref in raw.final:
            actual=group_closure(context.trace,{ref});saw_group|=bool(actual['groups'])
            self.assertEqual(actual,group_closure(raw,{ref}))
        self.assertTrue(saw_group)
        selector=dict.fromkeys(('source','line','end_line','column','end_column','object','instance'))
        selector.update(source=str(ROOT/'provenance/examples/multilevel.v'),line=4)
        plan=context.build({'kind':'source_cells','selector':selector})
        context.trace=raw;context.queries={};context.plans={};context.validated={};context._users=None
        self.assertEqual(context.build({'kind':'source_cells','selector':selector}),plan)
        result=extract(parser().parse_args(['extract',str(self.runs['retained']),'--source',str(self.source),
                       '--line','2','--instance','demo/first','--out',str(self.root/'selected.v')]))
        self.assertTrue((self.root/'selected.v').is_file())

    def test_selected_backend_is_used_when_another_index_file_exists(self):
        out=self.clone();m,raw,_=load_run(out,use_index=False)
        desc=sqlite_index.write(out,m,raw,pi.artifact_inventory(out))
        m['artifacts'][sqlite_index.NAME]={'sha256':digest(out/sqlite_index.NAME),'size_bytes':(out/sqlite_index.NAME).stat().st_size}
        write_json(out/'manifest.json',m)
        self.assertEqual(store_for(load_run(out)[1]).backend,'gzip')
        m['verified_index']=desc;write_json(out/'manifest.json',m)
        self.assertEqual(store_for(load_run(out)[1]).backend,'sqlite')

    def test_raw_and_gzip_corruption_are_rejected(self):
        out=self.clone();p=out/gzip_index.NAME;p.write_bytes(p.read_bytes()+b'broken')
        with self.assertRaisesRegex(TraceError,'artifact hash mismatch'):load_run(out)
        shutil.copyfile(self.runs['retained']/gzip_index.NAME,p)
        p=out/'yosys.jsonl';p.write_bytes(p.read_bytes()+b'\n')
        with self.assertRaisesRegex(TraceError,'artifact hash mismatch'):load_run(out)

    def test_rehashing_modified_gzip_does_not_bypass_authentication(self):
        out=self.clone()
        self.edit(out,lambda data:data['tables']['edges'].clear())
        with self.assertRaisesRegex(TraceError,'authentication failed'):load_run(out)
        load_run(out,use_index=False)

    def test_signed_but_malformed_tables_are_rejected(self):
        out=self.clone()
        self.edit(out,lambda data:data['tables']['documents'].append(data['tables']['documents'][0]),resign=True)
        with self.assertRaisesRegex(TraceError,'document identity'):load_run(out)

    def test_missing_index_and_missing_key_replay_without_writing(self):
        out=self.clone();(out/gzip_index.NAME).unlink();timings={}
        load_run(out,timings);self.assertEqual(timings['verified_index_status'],'missing')
        self.assertFalse((out/gzip_index.NAME).exists())
        with patch.object(pi,'_key_path',return_value=self.root/'absent-key'):
            timings={};load_run(self.runs['retained'],timings)
            self.assertEqual(timings['verified_index_status'],'local_authentication_unavailable')

    def test_schema_implementation_and_binding_changes_replay(self):
        out=self.clone();original=json.loads((out/'manifest.json').read_text())
        for kind,status in [('schema','schema_changed'),('implementation','implementation_changed'),('binding','archive_binding_changed')]:
            m=deepcopy(original)
            if kind=='schema':m['verified_index']['schema_version']=999
            elif kind=='implementation':m['verified_index']['implementation']={}
            else:m['endpoint']='changed endpoint'
            write_json(out/'manifest.json',m);timings={};load_run(out,timings)
            self.assertEqual(timings['verified_index_status'],status)

    def test_write_failure_does_not_publish_partial_gzip(self):
        out=self.clone();m,raw,_=load_run(out,use_index=False)
        (out/gzip_index.NAME).unlink()
        with patch('yosys_flow.gzip_index.gzip.GzipFile.write',side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                gzip_index.write(out,m,raw,pi.artifact_inventory(out))
        self.assertFalse((out/gzip_index.NAME).exists())
        self.assertFalse((out/(gzip_index.NAME+'.tmp')).exists())

    def test_signed_shared_field_cycle_is_rejected(self):
        out=self.clone()
        def corrupt(data):
            doc=data['tables']['documents'][-1][0]
            data['tables']['document_fields'].append([doc,'origins',doc])
        self.edit(out,corrupt,resign=True)
        with self.assertRaisesRegex(TraceError,'shared field'):load_run(out)

    def test_modified_raw_evidence_cannot_be_signed_into_new_index(self):
        out=self.clone();m,raw,_=load_run(out,use_index=False)
        (out/gzip_index.NAME).unlink()
        verified=pi.artifact_inventory(out)
        p=out/'yosys.jsonl';p.write_bytes(p.read_bytes()+b'\n')
        with self.assertRaisesRegex(TraceError,'validated evidence changed'):
            gzip_index.write(out,m,raw,verified)
        self.assertFalse((out/gzip_index.NAME).exists())

    def test_automatic_incremental_consumes_gzip_archive(self):
        from incremental import automatic
        from incremental.__main__ import parser as incremental_parser
        source=self.root/'base.v';new=self.root/'new.v'
        text='module demo(input a,b,c,d,output y,z);\nassign y=a&b;\nassign z=c^d;\nendmodule\n'
        source.write_text(text);new.write_text(text.replace('a&b','a|b'))
        base=self.root/'auto-base'
        run(parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(base),'--index-backend','gzip']))
        result=automatic.run(incremental_parser().parse_args(['auto','--base-run',str(base),
            '--new-source',str(new),'--out',str(self.root/'auto'),'--no-equivalence-precheck']))
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['baseline_load']['verified_index_status'],'hit')
        self.assertTrue(result['checks']['all_top_outputs_match_new_full_synthesis'])
        self.assertGreater(result['splice']['reused_cells'],0)
