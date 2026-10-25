"""Disk query equivalence, bounded loading and authenticated corruption checks."""
import json
import random
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import TraceError, digest, write_json
from yosys_flow.graph_index import EdgeList, walk, has_path
from yosys_flow.diagnostics import group_closure
from yosys_flow.contracts import ContractContext
from yosys_flow.runner import load_run, query_loaded, run
from yosys_flow.sqlite_index import NAME, store_for


class GraphTests(unittest.TestCase):
    def test_indexed_traversal_preserves_ordered_replay_including_cycles(self):
        rng=random.Random(107)
        for _ in range(100):
            edges=EdgeList({'from':rng.randrange(20),'to':rng.randrange(20),'event_id':i} for i in range(80))
            seeds=set(rng.sample(range(20),3))
            for reverse in (False,True):
                expected=set(seeds);chosen=[]
                rows=list(enumerate(edges))
                for i,e in reversed(rows) if reverse else rows:
                    if e['to' if reverse else 'from'] in expected:
                        expected.add(e['from' if reverse else 'to']);chosen.append((i,e))
                self.assertEqual(walk(edges,seeds,reverse=reverse),(expected,sorted(chosen)))
                if not reverse:
                    for target in range(20):self.assertEqual(has_path(edges,seeds,target),target in expected)


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built')
class SqliteIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='sqlite-provenance-');cls.root=Path(cls.temp.name)
        cls.source=cls.root/'source.v'
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
        cls.runs={}
        for mode in ('retained','flatten','m1'):
            out=cls.root/mode
            extra=['--flatten'] if mode=='flatten' else ['--flow','m1'] if mode=='m1' else []
            run(parser().parse_args(['run','--source',str(cls.source),'--top','demo','--out',str(out),*extra]))
            cls.runs[mode]=out

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def clone(self):
        out=self.root/self._testMethodName;shutil.copytree(self.runs['retained'],out);return out

    def test_queries_and_decoded_fields_equal_raw_replay_all_modes(self):
        for mode,out in self.runs.items():
            with self.subTest(mode=mode):
                timings={};m,cached,_=load_run(out,timings)
                self.assertEqual(timings['verified_index_status'],'hit')
                store=store_for(cached)
                self.assertIsNotNone(store)
                self.assertLess(store.decoded_records,20)
                _,raw,_=load_run(out,use_index=False)
                for line in range(2,6):
                    for instance in ('demo/first','demo/second'):
                        args=parser().parse_args(['query',str(out),'--source',str(self.source),'--line',str(line),'--instance',instance])
                        self.assertEqual(query_loaded(args,m,cached),query_loaded(args,m,raw))
                self.assertEqual(vars(cached),vars(raw))

    def test_group_closure_reads_only_indexed_members(self):
        from yosys_flow.sqlite_index import Rows, Edges
        from yosys_flow.common import ROOT
        out=self.root/'joint-groups'
        run(parser().parse_args(['run','--source',str(ROOT/'provenance/examples/multilevel.v'),
                                 '--top','demo','--out',str(out)]))
        _,trace,_=load_run(out);_,raw,_=load_run(out,use_index=False)
        expected={ref:group_closure(raw,{ref}) for ref in raw.final}
        self.assertTrue(any(row['groups'] for row in expected.values()))
        with patch.object(Rows,'__iter__',side_effect=AssertionError('full evidence scan')), \
             patch.object(Edges,'__iter__',side_effect=AssertionError('full history scan')):
            for ref,row in expected.items():self.assertEqual(group_closure(trace,{ref}),row)

    def test_contract_cell_matches_reverse_query_without_building_evidence_report(self):
        for mode in ('retained','flatten'):
            out=self.runs[mode];context=ContractContext(out)
            for ref in context.trace.final:
                args=parser().parse_args(['query',str(out),'--object',ref])
                expected=query_loaded(args,context.manifest,context.trace)['final_cells'][0]
                with patch.object(context,'query',side_effect=AssertionError('redundant reverse report')):
                    self.assertEqual(context.cell(ref),expected)

    def test_sql_uses_edge_and_source_indexes_and_is_readonly(self):
        _,trace,_=load_run(self.runs['retained']);con=store_for(trace).connection
        for sql,params,index in [
            ('SELECT ordinal FROM edges WHERE src=?',('1',),'edge_from'),
            ('SELECT ordinal FROM edges WHERE dst=?',('1',),'edge_to'),
            ('SELECT sid FROM positions WHERE file=? AND first_line<=?',(str(self.source),3),'source_location')]:
            self.assertIn(index,str(con.execute('EXPLAIN QUERY PLAN '+sql,params).fetchall()))
        with self.assertRaises(sqlite3.OperationalError):con.execute('DELETE FROM edges')

    def test_edges_share_templates_and_use_integer_references(self):
        _,trace,_=load_run(self.runs['retained']);con=store_for(trace).connection
        total,templates=con.execute('SELECT count(*),count(DISTINCT doc) FROM edges').fetchone()
        self.assertLess(templates,total)
        self.assertEqual(con.execute("SELECT count(*) FROM edges WHERE typeof(src)!='integer' OR typeof(dst)!='integer' OR typeof(doc)!='integer'").fetchone()[0],0)
        # Source IDs and object-version strings remain distinct on decoding.
        self.assertTrue(any(type(e['from']) is int for e in trace.edges))
        self.assertTrue(any(isinstance(e['from'],str) for e in trace.edges))

    def test_missing_optional_database_replays_without_rewriting(self):
        out=self.clone();(out/NAME).unlink();timings={};load_run(out,timings)
        self.assertEqual(timings['verified_index_status'],'missing');self.assertFalse((out/NAME).exists())

    def test_changed_raw_evidence_is_not_hidden(self):
        out=self.clone();p=out/'yosys.jsonl';p.write_bytes(p.read_bytes()+b'\n')
        with self.assertRaisesRegex(TraceError,'artifact hash mismatch'):load_run(out)

    def test_database_corruption_rejected_even_when_manifest_hash_is_resealed(self):
        out=self.clone()
        with sqlite3.connect(out/NAME) as con:con.execute('DELETE FROM edges WHERE ordinal=0')
        with self.assertRaisesRegex(TraceError,'artifact hash mismatch'):load_run(out)
        manifest=json.loads((out/'manifest.json').read_text())
        sha=digest(out/NAME);manifest['artifacts'][NAME]['sha256']=sha
        manifest['verified_index']['database_sha256']=sha
        write_json(out/'manifest.json',manifest)
        with self.assertRaisesRegex(TraceError,'authentication failed'):load_run(out)
        load_run(out,use_index=False)

    def test_schema_implementation_binding_and_missing_key_use_replay(self):
        out=self.clone();original=json.loads((out/'manifest.json').read_text())
        for change,status in [('schema','schema_changed'),('implementation','implementation_changed'),('binding','archive_binding_changed')]:
            data=json.loads(json.dumps(original))
            if change=='schema':data['verified_index']['schema_version']=999
            elif change=='implementation':data['verified_index']['implementation']={}
            else:data['endpoint']='different endpoint'
            write_json(out/'manifest.json',data);timings={};load_run(out,timings)
            self.assertEqual(timings['verified_index_status'],status)
        write_json(out/'manifest.json',original)
        with patch('yosys_flow.persistent_index._key_path',return_value=self.root/'missing-key'):
            timings={};load_run(out,timings);self.assertEqual(timings['verified_index_status'],'local_authentication_unavailable')

    def test_point_query_does_not_iterate_full_history(self):
        out=self.runs['retained'];m,trace,_=load_run(out)
        from yosys_flow.sqlite_index import Edges
        args=parser().parse_args(['query',str(out),'--source',str(self.source),'--line','2','--instance','demo/first'])
        with patch.object(Edges,'__iter__',side_effect=AssertionError('full history scan')):
            self.assertTrue(query_loaded(args,m,trace)['final_cells'])
