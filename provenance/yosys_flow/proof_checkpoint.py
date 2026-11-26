"""Atomic, authenticated, data-only prover checkpoints. Never pickle engines."""
from collections import Counter, defaultdict
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .abc_partition import Graph, Partitioner, Classes
from .structural import Expressions
from .common import digest, require
from .persistent_index import _encode, _decode
from .proof_store import implementation, signature


def atomic_json(path, data):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('w') as stream:
            json.dump(data,stream,separators=(',',':'),allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally: temporary.unlink(missing_ok=True)


class StopPreparation(Exception): pass


class BoundedPartitioner(Partitioner):
    def prove(self,*args,**kwargs):
        self.check_budget();return super().prove(*args,**kwargs)
    def global_cec(self,*args,**kwargs):
        self.check_budget();return super().global_cec(*args,**kwargs)


MAPS=('a','b','sa','sb','candidates','cec_attempts','attempts')
SCALARS=('mask','sat_timeout_ms','sat_backend','cec_timeout_seconds','cec_attempt_limit','proofs','diagnostics')


class CheckpointStore:
    def __init__(self, directory, baseline, policy, *, resume=False):
        self.directory=Path(directory);self.path=self.directory/'state.json'
        self.binding={'baseline':baseline,'implementation':implementation(),'policy':policy}
        self.stats={'engine_builds':0,'engine_restores':0,'state_save_seconds':0.0,'state_restore_seconds':0.0}
        if resume:
            require(self.path.is_file(),'no authenticated preparation checkpoint; legacy unsigned files require rebuilding')
            document=json.loads(self.path.read_text());auth=document.pop('authentication','')
            require(hmac.compare_digest(signature(document),auth),'preparation checkpoint authentication mismatch')
            require(document['binding']==self.binding,'preparation baseline, implementation or policy differs')
            self.entries=document['entries'];self.completed=document['completed']
        else:
            self.directory.mkdir(parents=True,exist_ok=False)
            self.entries={};self.completed={};self.commit()

    def commit(self):
        document={'binding':self.binding,'entries':self.entries,'completed':self.completed}
        atomic_json(self.path,{**document,'authentication':signature(document)})

    def save(self,key,engine,metadata=None):
        require(implementation()==self.binding['implementation'],'proof implementation changed during preparation')
        start=time.perf_counter();path=self.directory/(uuid.uuid4().hex+'.sqlite3')
        db=sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE state(field TEXT,key TEXT,value TEXT,PRIMARY KEY(field,key)) WITHOUT ROWID')
            def put(field,items):
                db.executemany('INSERT INTO state VALUES(?,?,?)',((field,json.dumps(_encode(k),separators=(',',':')),
                    json.dumps(_encode(v),separators=(',',':'))) for k,v in items))
            for field in MAPS:put(field,getattr(engine,field).items())
            put('unique',engine.algebra.unique.items());put('parents',engine.classes.parents.items());put('sides',engine.classes.sides.items())
            put('scalars',((f,getattr(engine,f)) for f in SCALARS))
            for label,graph in (('first',engine.first),('final',engine.final)):
                put(label+'.nodes',graph.nodes.items())
                put(label+'.interface',((f,getattr(graph,f)) for f in ('pis','pos','order')))
            db.commit()
        finally:db.close()
        previous=self.entries.get(key)
        self.entries[key]={'artifact':path.name,'sha256':digest(path),'metadata':metadata or {}}
        self.commit() # Descriptor changes only after the entire state is durable.
        if previous and previous['artifact']!=path.name:
            (self.directory/previous['artifact']).unlink(missing_ok=True)
        self.stats['state_save_seconds']+=time.perf_counter()-start

    def load(self,key,check_budget=lambda:None):
        require(implementation()==self.binding['implementation'],'proof implementation changed during preparation')
        row=self.entries.get(key)
        if row is None:return None
        start=time.perf_counter();path=self.directory/row['artifact']
        require(path.parent==self.directory and digest(path)==row['sha256'],'prover checkpoint hash mismatch')
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        def read(field):
            return {_decode(json.loads(k)):_decode(json.loads(v)) for k,v in db.execute('SELECT key,value FROM state WHERE field=?',(field,))}
        try:
            engine=BoundedPartitioner.__new__(BoundedPartitioner)
            for field in MAPS:setattr(engine,field,read(field))
            engine.attempts=Counter(engine.attempts);engine.candidates=defaultdict(list,engine.candidates)
            engine.algebra=Expressions();engine.algebra.unique=read('unique')
            engine.classes=Classes({},{});engine.classes.parents=read('parents');engine.classes.sides=defaultdict(int,read('sides'))
            for field,value in read('scalars').items():setattr(engine,field,value)
            for label in ('first','final'):
                graph=Graph.__new__(Graph);graph.nodes=read(label+'.nodes')
                for field,value in read(label+'.interface').items():setattr(graph,field,value)
                graph.inputs=set(graph.pis);graph.users=defaultdict(set)
                for n,node in graph.nodes.items():
                    for f in node['fanins']:graph.users[f].add(n)
                setattr(engine,label,graph)
        finally:db.close()
        engine.check_budget=check_budget
        self.stats['engine_restores']+=1
        self.stats['state_restore_seconds']+=time.perf_counter()-start
        return engine
