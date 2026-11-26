"""Authenticated, indexed old-RTL lowering for local all-output proofs.

This caches a representation, not an equivalence verdict. The existing source
cut construction and definedness SAT still decide every previously unseen cut.
"""
from collections import defaultdict
from collections.abc import Mapping
from functools import lru_cache
import hmac
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import uuid

from .common import PIN, digest, require, token, unescape
from .proof_checkpoint import atomic_json
from .proof_store import implementation, signature

LOWERING='proc; opt_expr; techmap; opt_expr; opt_clean; check -assert'


def prepare(context, directory, timeout=600):
    start=time.perf_counter();directory=Path(directory)
    fingerprint=implementation()
    require(0<timeout<=86400,'invalid source preparation timeout')
    if (directory/'manifest.json').is_file():
        source=StoredSource(context,directory);source.close()
        return {'status':'ready','reused':True,'seconds':time.perf_counter()-start}
    directory.mkdir(parents=True,exist_ok=True)
    frontend=context.directory/'frontend.il';binary=context.manifest['tool']['stock_executable']
    require(digest(frontend)==context.baseline['artifacts']['frontend.il']['sha256'],'archived source frontend changed')
    tool=digest(binary)
    require(tool==context.manifest['tool']['stock_sha256'],'source lowering tool changed')
    version=subprocess.check_output([binary,'-V'],text=True,timeout=10).strip()
    require(PIN in version,'source lowering requires pinned Yosys')
    from .source_regions import flatten
    with tempfile.TemporaryDirectory(prefix='source-lowering-',dir=directory) as tmp:
        result=subprocess.run([binary,'-Q','-T','-p',
            'read_rtlil '+token(frontend)+'; '+LOWERING+'; write_json coarse.json'],
            cwd=tmp,capture_output=True,text=True,timeout=timeout)
        require(result.returncode==0,'source lowering failed: '+result.stderr[-1200:])
        modules=json.loads((Path(tmp)/'coarse.json').read_text())['modules']
        cells,aliases,primary=flatten(modules,context.baseline['module'])
        del modules
    lowering_seconds=time.perf_counter()-start
    path=directory/(uuid.uuid4().hex+'.sqlite3');db=sqlite3.connect(path)
    try:
        db.executescript('''
          CREATE TABLE cells(name TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE drivers(bit INTEGER PRIMARY KEY,cell TEXT NOT NULL);
          CREATE TABLE aliases(path TEXT,wire TEXT,offset INTEGER,value TEXT,
                               PRIMARY KEY(path,wire,offset));
          CREATE TABLE anchors(final INTEGER,path TEXT,wire TEXT,offset INTEGER,
                               PRIMARY KEY(final,path,wire,offset));
          CREATE TABLE inputs(bit INTEGER PRIMARY KEY);
        ''')
        db.executemany('INSERT INTO cells VALUES(?,?)',((n,json.dumps(c,separators=(',',':'))) for n,c in cells.items()))
        db.executemany('INSERT INTO drivers VALUES(?,?)',((b,n) for n,c in cells.items()
            for p,bs in c['connections'].items() if c['port_directions'][p]=='output' for b in bs if isinstance(b,int)))
        db.executemany('INSERT INTO aliases VALUES(?,?,?,?)',((*key,json.dumps(value)) for key,value in aliases.items()))
        db.executemany('INSERT INTO inputs VALUES(?)',((b,) for b in primary if isinstance(b,int)))
        from .source_endpoints import Endpoints
        resolver=Endpoints(context)
        for key in aliases:
            for final,phase in resolver.signals(*key):
                if isinstance(final,int):db.execute('INSERT OR IGNORE INTO anchors VALUES(?,?,?,?)',(final,*key))
            # The archive already authenticates the binding; avoid retaining a
            # redundant human-readable evidence row for every alias offline.
            resolver.evidence.clear()
        db.execute('ANALYZE');db.commit()
        require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','source cache integrity failure')
    finally:db.close()
    require(digest(frontend)==context.baseline['artifacts']['frontend.il']['sha256'] and digest(binary)==tool,
            'source inputs changed during lowering')
    require(implementation()==fingerprint and digest(context.directory/'manifest.json')==context.baseline['manifest_sha256'],
            'source preparation implementation or baseline changed')
    document={'schema':1,'baseline':context.baseline['manifest_sha256'],'implementation':fingerprint,
        'frontend':digest(frontend),'tool':tool,'lowering':LOWERING,'database':path.name,'sha256':digest(path),
        'cells':len(cells),'aliases':len(aliases),'lowering_seconds':lowering_seconds,
        'seconds':time.perf_counter()-start,'new_rtl_used':False,'edit_target_used':False}
    atomic_json(directory/'manifest.json',{**document,'authentication':signature(document)})
    return {'status':'ready','reused':False,**document}


class Lookup(Mapping):
    def __init__(self,source,table,key,value):self.source,self.table,self.key,self.value=source,table,key,value
    def __getitem__(self,key):return self.source.row(self.table,self.key,self.value,key)
    def __iter__(self):
        return (r[0] for r in self.source.db.execute('SELECT '+self.key+' FROM '+self.table))
    def __len__(self):return self.source.db.execute('SELECT count(*) FROM '+self.table).fetchone()[0]


class StoredSource:
    def __init__(self,context,directory):
        start=time.perf_counter();self.directory=Path(directory)
        document=json.loads((self.directory/'manifest.json').read_text());auth=document.pop('authentication','')
        require(hmac.compare_digest(signature(document),auth),'source representation authentication mismatch')
        require(document['schema']==1 and document['implementation']==implementation(),'source representation implementation differs')
        require(document['baseline']==context.baseline['manifest_sha256'] and
                document['frontend']==context.baseline['artifacts']['frontend.il']['sha256'] and
                document['tool']==context.manifest['tool']['stock_sha256'],'source representation baseline differs')
        name=document['database'];require(Path(name).name==name,'invalid source representation path')
        path=self.directory/name
        require(path.resolve().is_relative_to(self.directory.resolve()) and digest(path)==document['sha256'],
                'source representation hash mismatch')
        self.db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);self.db.execute('PRAGMA query_only=ON')
        self.cells=Lookup(self,'cells','name','payload');self.drivers=Lookup(self,'drivers','bit','cell')
        self.primary={r[0] for r in self.db.execute('SELECT bit FROM inputs')}
        self.metrics={'authentication_seconds':time.perf_counter()-start,'cells_read':0,'driver_rows_read':0,
                      'alias_rows_read':0,'boundary_lookup_seconds':0.0,'lowering_reused':0}
        self.row=lru_cache(maxsize=8192)(self.row)

    def row(self,table,key,value,ident):
        row=self.db.execute('SELECT '+value+' FROM '+table+' WHERE '+key+'=?',(ident,)).fetchone()
        if row is None:raise KeyError(ident)
        self.metrics['cells_read' if table=='cells' else 'driver_rows_read']+=1
        return json.loads(row[0]) if table=='cells' else row[0]

    def boundary(self,context,bits):
        """Baseline bindings plus current certified anchors, respecting overrides."""
        start=time.perf_counter();keys=set();bits=set(bits)
        for bit in bits:
            keys.update(self.db.execute('SELECT path,wire,offset FROM anchors WHERE final=?',(bit,)))
        from .partition_store import certificates
        for cert in certificates(context):
            for row in cert['anchors']:
                if row['final_bit'] in bits:keys.add((row['instance'],unescape(row['wire']),row['offset']))
        from .source_endpoints import Endpoints
        resolver=Endpoints(context);by_final=defaultdict(set);anchors=defaultdict(list)
        for key in sorted(keys):
            row=self.db.execute('SELECT value FROM aliases WHERE path=? AND wire=? AND offset=?',key).fetchone()
            if row is None:continue
            self.metrics['alias_rows_read']+=1;value=json.loads(row[0])
            for final,phase in resolver.signals(*key):
                if isinstance(final,int) and final in bits:
                    by_final[final].add((value,phase))
                    anchors[final].append(dict(instance=key[0],wire=key[1],offset=key[2],source_bit=value,inverted=phase))
        self.metrics['boundary_lookup_seconds']+=time.perf_counter()-start
        return by_final,anchors

    def circuit(self,manager):
        from .region_functions import Circuit
        circuit=Circuit.__new__(Circuit);circuit.cells=self.cells;circuit.manager=manager
        primaries=sorted(self.primary);circuit.values=dict(zip(primaries,manager.variables(len(primaries))))
        circuit.drivers=self.drivers
        return circuit

    def close(self):self.row.cache_clear();self.db.close()
