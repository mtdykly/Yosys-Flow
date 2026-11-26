"""Authenticated old-design function classes, with bounded lazy SQLite access.

This is supplementary evidence, not a replacement for the provenance archive.
Only a live prover may publish canonical classes. JSON pair files are not trusted.
The database stores polarity explicitly and never promotes a candidate relation.
"""
from collections.abc import Mapping
from collections import defaultdict
from functools import lru_cache
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time

from .abc_partition import Graph, Partitioner
from .boolean import ProofBudgetExceeded
from .common import digest, require, write_json
from .persistent_index import _authentication_key, implementation_fingerprint

SCHEMA = 1
FILES = ('proof_store.py', 'proof_regions.py', 'checkpoint_evidence.py',
         'proof_checkpoint.py', 'proof_bridge.py', 'prepare_evidence.py','proof_pipeline.py','source_cache.py','contracts.py','reindex.py',
         'evidence_relations.py','online_evidence.py','boundary_scan.py','boundary_forms.py','fraig_evidence.py','rewrite_recovery.py',
         'abc_partition.py', 'abc_checkpoints.py', 'abc_lemmas.py', 'abc_copy.py',
         'abc_mapping.py', 'abc_rewrites.py', 'boolean.py', 'structural.py',
         'partition_archive.py', 'source_regions.py', 'region_functions.py')


def implementation():
    return {**implementation_fingerprint(),
            **{n: digest(Path(__file__).with_name(n)) for n in FILES}}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def signature(value):
    key = _authentication_key()
    require(key is not None, 'proof evidence requires the local archive authentication key')
    return hmac.new(key, b'yosys-flow-proof-store-v1\0'+encoded(value), hashlib.sha256).hexdigest()


def name_key(name):
    return '' if name is None else name


class Writer:
    """One transactional publication, completed by a signed descriptor last."""
    def __init__(self, context, directory):
        self.context = context
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.path = self.directory/'functions.sqlite3'
        self.implementation = implementation()
        self.db = sqlite3.connect(self.path)
        self.db.executescript('''
          CREATE TABLE invocations(id TEXT PRIMARY KEY, interface TEXT NOT NULL, evidence TEXT NOT NULL);
          CREATE TABLE nodes(inv TEXT, side INTEGER, name TEXT, literal INTEGER, class INTEGER,
                             public INTEGER, rank INTEGER, payload TEXT,
                             PRIMARY KEY(inv,side,name));
          CREATE INDEX classes_by_node ON nodes(inv,side,class,public);
          CREATE TABLE classes(inv TEXT, id INTEGER, sides INTEGER, PRIMARY KEY(inv,id));
          CREATE TABLE users(inv TEXT, side INTEGER, input TEXT, output TEXT,
                             PRIMARY KEY(inv,side,input,output));
          CREATE TABLE cell_nodes(cell TEXT, inv TEXT, node TEXT, PRIMARY KEY(cell,inv,node));
        ''')
        self.invocations = []
        self.preparation_scope = []

    def add(self, region, engine, evidence=None):
        inv = region['invocation_id']
        self.invocations.append(inv)
        self.preparation_scope.append({'invocation':inv, 'target_independent':(evidence or {}).get('target_independent',False)})
        public = {'ys__n'+str(i) for i, b in enumerate(region['signals'])
                  if isinstance(b, dict) and b['wire'].startswith('\\')}
        interface = {'entry_inputs': engine.first.pis, 'entry_outputs': engine.first.pos,
                     'export_inputs': engine.final.pis, 'export_outputs': engine.final.pos}
        # Proof dependencies are shared once per invocation, not duplicated per node.
        evidence = dict(evidence or {}, proofs=engine.proofs,
                        endpoints={k: self.context.manifest['artifacts'][region['artifacts'][k]]['sha256']
                                   for k in ('input.blif', 'output.blif')})
        self.db.execute('INSERT INTO invocations VALUES(?,?,?)', (inv, json.dumps(interface), json.dumps(evidence)))
        sides = defaultdict(int)
        for side, graph, values in ((1, engine.first, engine.a), (2, engine.final, engine.b)):
            ranks = {n: i for i, n in enumerate(graph.pis+graph.order)}
            def rows():
                for name, literal in values.items():
                    canonical = engine.classes.find(literal)
                    sides[canonical//2] |= side
                    node = graph.nodes.get(name)
                    yield (inv, side, name_key(name), canonical, canonical//2,
                           int(side == 1 and (name in public or name in graph.inputs)),
                           ranks.get(name, -1), json.dumps(node) if node is not None else None)
            self.db.executemany('INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)', rows())
            self.db.executemany('INSERT INTO users VALUES(?,?,?,?)',
                ((inv, side, name_key(a), name_key(b)) for a, bs in graph.users.items() for b in bs))
        self.db.executemany('INSERT INTO classes VALUES(?,?,?)', ((inv, c, s) for c, s in sides.items()))
        self.db.executemany('INSERT INTO cell_nodes VALUES(?,?,?)',
            ((r['cell'], inv, 'ys__n'+str(r['id'])) for r in region['input_nodes'] if r.get('cell')))
        self.db.commit()

    def finish(self, timings=None):
        require(self.invocations, 'no invocations in proof evidence')
        require(implementation() == self.implementation, 'proof implementation changed during publication')
        require(digest(self.context.directory/'manifest.json') == self.context.baseline['manifest_sha256'],
                'baseline changed during proof publication')
        self.db.execute('ANALYZE')
        self.db.commit()
        require(self.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'proof database integrity failure')
        self.db.close()
        document = {'schema': SCHEMA, 'baseline_manifest_sha256': self.context.baseline['manifest_sha256'],
                    'implementation': self.implementation, 'database': 'functions.sqlite3',
                    'sha256': digest(self.path), 'invocations': self.invocations,
                    'preparation_scope':self.preparation_scope,
                    'timings': timings or {}, 'complete_causal_history': False}
        write_json(self.directory/'manifest.json', {**document, 'authentication': signature(document)})
        return document


class Rows(Mapping):
    """Bounded node cache; never restore the entire graph/expression algebra."""
    def __init__(self, store, inv, side, field):
        self.store, self.inv, self.side, self.field = store, inv, side, field

    def __getitem__(self, name):
        row = self.store.node(self.inv, self.side, name_key(name))
        if row is None or (self.field == 'payload' and row[4] is None): raise KeyError(name)
        return row[{'literal': 0, 'public': 1, 'rank': 2, 'class': 3, 'payload': 4}[self.field]]

    def __iter__(self):
        clause = ' AND payload IS NOT NULL' if self.field == 'payload' else ''
        for (name,) in self.store.db.execute('SELECT name FROM nodes WHERE inv=? AND side=?'+clause,
                                             (self.inv, self.side)):
            yield None if name == '' else name

    def __len__(self):
        clause = ' AND payload IS NOT NULL' if self.field == 'payload' else ''
        return self.store.db.execute('SELECT count(*) FROM nodes WHERE inv=? AND side=?'+clause,
                                    (self.inv, self.side)).fetchone()[0]


class Users:
    def __init__(self, store, inv, side): self.store, self.inv, self.side = store, inv, side
    def __getitem__(self, name):
        return set(self.store.users(self.inv, self.side, name_key(name)))


class DiskGraph(Graph):
    def __init__(self, store, inv, side, inputs, outputs):
        self.nodes = Rows(store, inv, side, 'payload')
        self.pis, self.pos = inputs, outputs
        self.inputs = set(inputs)
        self.users = Users(store, inv, side)


class IdentityClasses:
    """All representatives have already been closed and polarity-normalized."""
    def __init__(self, store, inv):
        class Sides:
            def __getitem__(_, ident): return store.sides(inv, ident)
        self.sides = Sides()
    def find(self, value): return value
    find_no_compress = find


class Eligible:
    def __init__(self, store, inv, side):
        self.store, self.inv, self.side, self.removed = store, inv, side, set()
    def __contains__(self, name):
        if name in self.removed: return False
        row = self.store.node(self.inv, self.side, name_key(name))
        return bool(row and (row[1] if self.side == 1 else self.store.public_class(self.inv, row[3])))
    def difference_update(self, names): self.removed.update(names)


class StoredPartitioner(Partitioner):
    def __init__(self, store, inv, *, sat_timeout_ms=1000):
        data = json.loads(store.db.execute('SELECT interface FROM invocations WHERE id=?', (inv,)).fetchone()[0])
        self.store, self.inv = store, inv
        self.first = DiskGraph(store, inv, 1, data['entry_inputs'], data['entry_outputs'])
        self.final = DiskGraph(store, inv, 2, data['export_inputs'], data['export_outputs'])
        self.a, self.b = Rows(store, inv, 1, 'literal'), Rows(store, inv, 2, 'literal')
        self.classes = IdentityClasses(store, inv)
        self.sat_timeout_ms, self.sat_backend = sat_timeout_ms, 'abc'

    def region_frontiers(self, source_nodes):
        require(source_nodes is None, 'stored partitions use archived public source anchors')
        return Eligible(self.store, self.inv, 1), Eligible(self.store, self.inv, 2), Rows(self.store, self.inv, 2, 'rank')

    def region(self, seeds, *, source_nodes=None, limit=4096):
        """Same cut rule as Partitioner.region, using indexed frontier membership.

        Keep the authenticated in-memory verifier unchanged for old archives;
        shared window/alignment/equivalence methods still perform every proof.
        Differential tests cover this disk traversal adapter.
        """
        require(seeds and set(seeds)<=self.first.nodes.keys(), 'invalid partition source seeds')
        affected=set();stack=list(seeds)
        while stack:
            n=stack.pop()
            if n in affected: continue
            affected.add(n);stack.extend(self.first.users[n])
            from .online_evidence import current
            policy=current()
            if policy:policy.check_nodes(len(affected),'affected_abc_nodes')
        outputs=sorted(affected & set(self.first.pos))
        require(outputs, 'source target has no captured observable output')
        cls=self.classes
        allowed_a,allowed_b,export_rank=self.region_frontiers(source_nodes)
        excluded={cls.find(self.a[n]) for n in affected}
        excluded.update(cls.find(self.b[n]) for n in outputs)
        for _ in range(32):
            ao,ab,bo,bb=self.aligned(outputs,outputs,excluded,limit,allowed_a,allowed_b)
            # Electrical independence is stricter than functional equivalence:
            # two separate drivers of equal/complement functions are NOT one input.
            groups=defaultdict(list)
            for n in bb: groups[cls.find(self.b[n])//2].append(n)
            duplicates=[ns for ns in groups.values() if len(ns)>1]
            if duplicates:
                # Keep the earliest actual driver and absorb the downstream
                # duplicate/inverter. Excluding its entire functional class
                # would unfold the upstream cone on BOTH sides unnecessarily.
                for ns in duplicates:
                    keep=min(ns,key=lambda n:export_rank[n])
                    allowed_b.difference_update(set(ns)-{keep})
                continue
            old_members=set(ao); new_members=set(bo)
            # Unchanged upstream helpers may be copied into the source cut.
            # Only its affected external outputs are edit obligations; every
            # selected FINAL output is checked independently below, including
            # an absorbed inverter whose users are otherwise unchanged.
            old_outputs=sorted(n for n in ao if n in affected and
                               (n in self.first.pos or self.first.users[n]-old_members))
            new_outputs=sorted(n for n in bo if n in self.final.pos or self.final.users[n]-new_members)
            old_by_class=defaultdict(list)
            for n in sorted(set(old_outputs)|set(ab)):old_by_class[cls.find(self.a[n])].append(n)
            pairs=[]
            for n in new_outputs:
                matches=old_by_class.get(cls.find(self.b[n]),[])
                if n in outputs: matches=[n]
                require(matches, 'unmatched external partition output: '+n)
                pairs.extend((a,n) for a in matches)
            require(set(old_outputs)<=set(a for a,b in pairs), 'old external output omitted from partition')
            equal,count,backend=self.equivalent(ao,ab,bo,bb,[(a,b,0) for a,b in pairs])
            if equal:
                require(set(seeds)<=old_members,'partition omitted a changed source target')
                boundary=[]
                for n in sorted(bb):
                    matches=[a for a in sorted(ab) if cls.find(self.a[a])==cls.find(self.b[n])]
                    inverted=False
                    if not matches:
                        matches=[a for a in sorted(ab) if cls.find(self.a[a])==(cls.find(self.b[n])^1)]
                        inverted=True
                    require(matches, 'partition has a missing source boundary')
                    boundary.append(dict(export_node=n,entry_nodes=matches,inverted=inverted))
                return dict(status='verified',rule='all_external_outputs_independent_partition_v1',
                    complete_causal_history=False,inputs_are_independent=True,
                    seeds=sorted(seeds),captured_outputs=outputs,entry_nodes=ao,export_nodes=bo,
                    boundary=boundary,outputs=[dict(entry=a,export=b) for a,b in pairs],
                    all_external_outputs=True,independent_input_count=count,backend=backend)
            excluded.update(cls.find(self.a[n]) for n in ab if n not in self.first.inputs)
            excluded.update(cls.find(self.b[n]) for n in bb if n not in self.final.inputs)
        raise ProofBudgetExceeded('partition_region_expansion_limit')

    def equivalent(self, *args):
        start = time.perf_counter()
        from .online_evidence import current
        policy=current();previous=self.sat_timeout_ms
        if policy:self.sat_timeout_ms=max(1,min(previous,int(policy.remaining()*1000)))
        try: return super().equivalent(*args)
        finally:
            self.sat_timeout_ms=previous
            self.store.metrics['boundary_function_proof_seconds'] += time.perf_counter()-start


class Reader:
    def __init__(self, context, directory):
        start = time.perf_counter()
        self.directory = Path(directory).resolve()
        document = json.loads((self.directory/'manifest.json').read_text())
        authentication = document.pop('authentication', '')
        require(hmac.compare_digest(authentication, signature(document)), 'proof evidence authentication mismatch')
        require(document['schema'] == SCHEMA, 'unsupported proof evidence schema')
        require(document['implementation'] == implementation(), 'proof evidence implementation changed; rebuild evidence')
        require(document['baseline_manifest_sha256'] == context.baseline['manifest_sha256'], 'proof evidence belongs to another baseline')
        import re
        require(document['database'] == 'functions.sqlite3' or
                re.fullmatch(r'generations/[0-9a-f]{32}/functions\.sqlite3',document['database']), 'invalid proof database path')
        path = self.directory/document['database']
        require(path.resolve().is_relative_to(self.directory),'proof database escapes evidence directory')
        require(digest(path) == document['sha256'], 'proof database hash mismatch')
        self.document = document
        self.db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        self.db.execute('PRAGMA query_only=ON')
        # Instance-owned caches: closing one library releases all its cached nodes.
        for method in ('node', 'users', 'sides', 'public_class'):
            setattr(self, method, lru_cache(maxsize=8192)(getattr(self, method)))
        self.metrics = {'authentication_seconds': time.perf_counter()-start,
                        'boundary_function_proof_seconds': 0.0,
                        'region_preparation_seconds': 0.0, 'cache_hits': 0, 'derived_regions': 0,
                        'misses': [], 'node_rows_read': 0}
        self.source=None
        if (self.directory/'relations/manifest.json').is_file():
            from .evidence_relations import validate
            try:self.metrics['typed_evidence']=validate(context,self.directory/'relations')['counts']
            except BaseException:self.close();raise
        if (self.directory/'source/manifest.json').is_file():
            from .source_cache import StoredSource
            try:self.source=StoredSource(context,self.directory/'source')
            except BaseException:self.close();raise
            self.metrics['source_representation']=self.source.metrics
        self.metrics['authentication_seconds']=time.perf_counter()-start
        from .proof_regions import cache_path
        path=cache_path(self)
        self.metrics['initial_region_cache_entries']=0
        if path.is_file():
            connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            try:self.metrics['initial_region_cache_entries']=connection.execute('SELECT count(*) FROM certificates').fetchone()[0]
            finally:connection.close()

    def node(self, inv, side, name):
        row = self.db.execute('SELECT literal,public,rank,class,payload FROM nodes WHERE inv=? AND side=? AND name=?',
                              (inv, side, name)).fetchone()
        self.metrics['node_rows_read'] += int(row is not None)
        return (*row[:4], json.loads(row[4]) if row[4] is not None else None) if row else None

    def users(self, inv, side, name):
        return tuple(r[0] for r in self.db.execute('SELECT output FROM users WHERE inv=? AND side=? AND input=?', (inv, side, name)))

    def sides(self, inv, ident):
        row = self.db.execute('SELECT sides FROM classes WHERE inv=? AND id=?', (inv, ident)).fetchone()
        return row[0] if row else 0

    def public_class(self, inv, ident):
        return bool(self.db.execute('SELECT 1 FROM nodes WHERE inv=? AND side=1 AND class=? AND public=1 LIMIT 1', (inv, ident)).fetchone())

    def cell_nodes(self, cell):
        return self.db.execute('SELECT inv,node FROM cell_nodes WHERE cell=?', (cell,)).fetchall()

    def close(self):
        if self.source is not None:self.source.close();self.source=None
        for method in ('node', 'users', 'sides', 'public_class'): getattr(self, method).cache_clear()
        self.db.close()
