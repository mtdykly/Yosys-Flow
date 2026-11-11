"""Large live arrays, batched provenance checks and compact plan encoding."""
from copy import deepcopy
import gc
import gzip
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from yosys_flow import gzip_index, sqlite_index
from yosys_flow.common import TraceError
from yosys_flow.contracts import ContractContext, seal
from yosys_flow.graph_index import EdgeList, has_paths, supporting_nodes, walk
from yosys_flow.index_store import key
from yosys_flow.origin_sets import OriginMatcher, OriginSets, pack_plan_sources, cell_origins
from yosys_flow.index_compatibility import compatible, fingerprint_digest


class SharedQueryTests(unittest.TestCase):
    def test_large_array_stays_shared_past_lru_eviction_and_can_be_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for backend in (sqlite_index, gzip_index):
                with self.subTest(backend=backend.NAME):
                    path = Path(tmp)/backend.NAME
                    con = sqlite3.connect(path) if backend is sqlite_index else None
                    if con: con.executescript(sqlite_index.SCHEMA_SQL)
                    sink = backend.Sink(con) if con else backend.Sink()
                    payload = key(list(range(300000))).encode()
                    self.assertGreater(len(payload),1048576)
                    sink.document(1,hashlib.sha256(payload).digest(),payload)
                    sink.add('fields',('objects','mapping',600,None))
                    sink.add('fields',('edges','sequence',0,None))
                    for i in range(600):
                        data=key({'ref':str(i)}).encode()
                        sink.document(i+2,hashlib.sha256(data).digest(),data)
                        sink.add('document_fields',(i+2,'origins',1))
                        sink.add('items',('objects',key(str(i)),i,i+2))
                    if con: con.commit(); con.close()
                    else:
                        path.write_bytes(gzip.compress(json.dumps({'schema_version':backend.SCHEMA,
                            'kind':backend.KIND,'tables':sink.tables}).encode()))
                    store=backend.Store(path)
                    rows=store.field('objects')
                    first=rows['0']['origins']; ref=weakref.ref(first)
                    for i in range(600): self.assertIs(rows[str(i)]['origins'],first)
                    self.assertEqual(store.decoded_records,601)
                    self.assertNotIn(1,store.cache)  # larger than the ordinary LRU
                    self.assertIs(rows['0']['origins'],first)  # parent was evicted
                    with self.assertRaises(TypeError):first.append(-1)
                    copied=deepcopy([first,first])
                    self.assertIs(copied[0],copied[1])
                    copied[0].append(-1)
                    self.assertEqual(len(first),300000)
                    del copied, first
                    store.cache.clear();gc.collect()
                    self.assertIsNone(ref())
                    self.assertEqual(len(store.shared_arrays),0)

    def test_matching_and_unions_only_visit_each_live_array_once(self):
        class Counted(list):
            def __iter__(self):
                self.visits += 1
                return super().__iter__()
        origins=Counted(range(20000));origins.visits=0
        matcher=OriginMatcher({30000})
        for _ in range(2000):self.assertFalse(matcher.intersects(origins))
        self.assertEqual(origins.visits,1)
        origins.visits=0;pool=OriginSets()
        first=pool.ordered_union([origins]*2000)
        for _ in range(10):self.assertIs(pool.ordered_union([origins]*20),first)
        self.assertEqual(origins.visits,1)
        self.assertEqual(first,list(range(20000)))

    def test_grouped_cells_preserve_each_cells_own_source_obligation(self):
        context=ContractContext.__new__(ContractContext)
        context.compact=True;context._users=None
        objects={};netlist={};edges=EdgeList()
        for i in range(24):
            ref='gate'+str(i)
            origins=[0] if i<12 else [1]  # equal copies must also batch
            objects[ref]={'origins':origins,'precision':'cell',
                          'state':{'name':ref,'module':'top','type':'$_BUF_'}}
            netlist[ref]={'connections':{'Y':[i+10]},'port_directions':{'Y':'output'}}
            edges.append({'from':origins[0],'to':ref,'event_id':i})
        context.module={'cells':netlist,'ports':{}}
        context.trace=SimpleNamespace(objects=objects,final=set(objects),edges=edges)
        expected=[context.cell(ref) for ref in sorted(objects)]
        with patch('yosys_flow.contracts.has_paths', wraps=has_paths) as checked:
            self.assertEqual(context.cells(objects),expected)
            self.assertEqual(checked.call_count,2)
        # The target is reachable from another group's source, but not its own.
        objects['gate0']['origins']=[2]
        with self.assertRaisesRegex(TraceError,'continuous source evidence path'):
            context.cells(objects)

    def test_streaming_checks_keep_order_and_skip_evidence_payloads(self):
        rng=random.Random(11)
        for _ in range(30):
            edges=EdgeList({'from':rng.randrange(30),'to':rng.randrange(30),'event_id':i}
                           for i in range(100))
            seeds={0,1};expected,_=walk(edges,seeds)
            ancestors,_=walk(edges,seeds,reverse=True)
            class Pairs:
                def endpoint_pairs(self,reverse=False):
                    for row in reversed(edges) if reverse else edges:
                        yield row['from'],row['to']
                def __iter__(self):raise AssertionError('decoded full evidence')
            self.assertEqual(has_paths(Pairs(),seeds,set(range(20))),set(range(20))<=expected)
            self.assertEqual(supporting_nodes(Pairs(),seeds),ancestors)

    def test_plan_json_factors_origins_and_seals_group_contents(self):
        sources=list(range(1000))
        doc={'region':{'cells':[{'ref':str(i),'origin_ids':sources} for i in range(200)]},
             'outputs':[{'recorded_source_ids':sources} for _ in range(8)]}
        original=json.dumps(doc)
        pack_plan_sources(doc)
        self.assertEqual(len(doc['source_sets']),1)
        self.assertLess(len(json.dumps(doc)),len(original)//10)
        for row in doc['region']['cells']:self.assertEqual(cell_origins(doc,row),sources)
        sealed=seal(doc)
        doc['source_sets']['0'].pop()
        self.assertNotEqual(seal(doc)['contract_id'],sealed['contract_id'])

    def test_compatibility_is_exact_reviewed_pair_not_schema_bypass(self):
        old={'reader':'old','recorder':'same'};new={'reader':'new','recorder':'same'}
        pair=(fingerprint_digest(old),fingerprint_digest(new))
        self.assertFalse(compatible(old,new))
        with patch('yosys_flow.index_compatibility.REVIEWED_READER_UPGRADES',{pair}):
            self.assertTrue(compatible(old,new))
            self.assertFalse(compatible(old,{**new,'reader':'future'}))
            self.assertFalse(compatible({**old,'recorder':'tampered'},new))
