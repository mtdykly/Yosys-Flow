"""Authenticated, rebuildable on-disk provenance indexes; payloads load on demand.

The database is never independent evidence. Raw files are still hash checked;
the authenticated descriptor binds the DB bytes, implementation and manifest.
Only a completely validated archive may publish this read-only acceleration.
"""
from collections import OrderedDict
from collections.abc import Mapping, Sequence
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import zlib

from .common import TraceError, digest, require

NAME = 'verified-index.sqlite3'
SCHEMA = 2
SHARED_FIELDS = {'origins', 'recorded_origins', 'inputs', 'values', 'enclosing_controls'}
EAGER = {'design', 'instance_catalog', 'view_rule', 'final', 'gaps',
         'abc_fallbacks', 'process_connection_proofs', 'hierarchy'}


def key(value):
    from .persistent_index import _encode, _json_bytes
    return _json_bytes(_encode(value)).decode()


def unpack(value):
    from .persistent_index import _decode
    return _decode(json.loads(zlib.decompress(value)))


def authenticate(secret, descriptor):
    return hmac.new(secret, b'yosys-flow-sqlite-index-v1\0'+key(descriptor).encode(), hashlib.sha256).hexdigest()


def write(directory, manifest, trace, verified_artifacts):
    from . import persistent_index as pi
    directory = Path(directory)
    pi.build_lookup(trace)
    require(set(vars(trace)) == pi._FIELDS, 'native trace fields changed; update SQLite schema')
    artifacts = pi.artifact_inventory(directory)
    pi.require_evidence_unchanged(verified_artifacts, artifacts)
    require(artifacts == verified_artifacts, 'unverified artifact appeared before index signing')
    target = directory/NAME
    temporary = directory/(NAME+'.tmp')
    temporary.unlink(missing_ok=True)
    con = sqlite3.connect(temporary)
    try:
        con.executescript('''
            PRAGMA journal_mode=DELETE;
            CREATE TABLE documents(id INTEGER PRIMARY KEY, hash BLOB UNIQUE NOT NULL, data BLOB NOT NULL);
            CREATE TABLE document_fields(doc INTEGER, field TEXT, value INTEGER,
                                         PRIMARY KEY(doc,field)) WITHOUT ROWID;
            CREATE TABLE fields(name TEXT PRIMARY KEY, kind TEXT, count INTEGER, doc INTEGER) WITHOUT ROWID;
            CREATE TABLE items(field TEXT, key TEXT, ordinal INTEGER, doc INTEGER,
                               PRIMARY KEY(field,key)) WITHOUT ROWID;
            CREATE INDEX item_order ON items(field,ordinal);
            CREATE TABLE refs(id INTEGER PRIMARY KEY, value TEXT UNIQUE NOT NULL);
            CREATE TABLE edges(ordinal INTEGER PRIMARY KEY, src INTEGER, dst INTEGER, event INTEGER, doc INTEGER);
            CREATE INDEX edge_from ON edges(src,ordinal);
            CREATE INDEX edge_to ON edges(dst,ordinal);
            CREATE TABLE positions(sid TEXT PRIMARY KEY, file TEXT, first_line INTEGER, last_line INTEGER) WITHOUT ROWID;
            CREATE INDEX source_location ON positions(file,first_line,last_line);
            CREATE TABLE tags(field TEXT, tag TEXT, value TEXT, ordinal INTEGER,
                              PRIMARY KEY(field,tag,value,ordinal)) WITHOUT ROWID;
            CREATE TABLE joint_groups(invocation TEXT, group_id TEXT, doc INTEGER,
                                      PRIMARY KEY(invocation,group_id)) WITHOUT ROWID;
            CREATE TABLE joint_members(ref TEXT, invocation TEXT, group_id TEXT,
                                       PRIMARY KEY(ref,invocation,group_id)) WITHOUT ROWID;
        ''')
        # One offline transaction, no per-event commits. Repeated payloads are
        # stored once; SQL keys remain explicit and searchable outside the blobs.
        con.execute('BEGIN')
        seen, refs, arrays = {}, {}, {}
        def document(value):
            # Source/member arrays recur across objects, versions and evidence
            # rows. Resolve a shared document lazily, never serialize them again
            # for every row. IDs are only an on-disk encoding, not new evidence.
            cacheable = isinstance(value, list) and len(value) >= 32
            if cacheable and id(value) in arrays:
                return arrays[id(value)][1]
            links = {}
            payload_value = value
            if isinstance(value, dict):
                links = {k: document(v) for k, v in value.items()
                         if k in SHARED_FIELDS and isinstance(v, list) and len(v) >= 32}
                if links: payload_value = {k: v for k, v in value.items() if k not in links}
            payload = key(payload_value).encode()
            sha = hashlib.sha256(payload + b'\0fields\0' + key(links).encode()).digest()
            if sha not in seen:
                doc = con.execute('INSERT INTO documents(hash,data) VALUES (?,?)',
                                  (sha,zlib.compress(payload,1))).lastrowid
                seen[sha] = doc
                con.executemany('INSERT INTO document_fields VALUES (?,?,?)',
                                ((doc, k, v) for k, v in links.items()))
            doc = seen[sha]
            if cacheable: arrays[id(value)] = (value, doc)
            return doc
        def reference(value):
            encoded=key(value)
            if encoded not in refs:
                refs[encoded]=con.execute('INSERT INTO refs(value) VALUES (?)',(encoded,)).lastrowid
            return refs[encoded]
        def put(field, value):
            if field in EAGER or not isinstance(value,(dict,list)):
                con.execute('INSERT INTO fields VALUES (?,?,?,?)',(field,'value',0,document(value)))
                return
            kind = 'mapping' if isinstance(value,dict) else 'sequence'
            con.execute('INSERT INTO fields VALUES (?,?,?,NULL)',(field,kind,len(value)))
            entries = value.items() if isinstance(value,dict) else enumerate(value)
            for ordinal,(k,row) in enumerate(entries):
                if field == 'edges':
                    # Do not duplicate each edge as a compressed JSON document.
                    # Endpoints and event live in searchable columns; capture,
                    # role and other common fields share a small template.
                    doc=document({k:v for k,v in row.items() if k not in {'from','to','event_id'}})
                    con.execute('INSERT INTO edges VALUES (?,?,?,?,?)',
                                (ordinal,reference(row['from']),reference(row['to']),row['event_id'],doc))
                else:
                    doc = document(row)
                    con.execute('INSERT INTO items VALUES (?,?,?,?)',(field,key(k),ordinal,doc))
                if field == 'sources':
                    con.execute('INSERT INTO positions VALUES (?,?,?,?)',
                                (key(k),str(Path(row['file']).resolve()),row['line'],row['end_line']))
                if isinstance(row,dict) and kind=='sequence' and field not in {'records','edges'}:
                    tags=[(tag,row[tag]) for tag in ('ref','seq','invocation_id','action') if tag in row]
                    origins=row.get('origins',[]) if field in {'regions','process_discards'} or (
                        field=='events' and row.get('record_type') in {'process_action','process_action_detail','process_discard'}) else []
                    if field=='removals': origins=trace.objects[row['ref']]['origins']
                    tags.extend(('origin',sid) for sid in origins)
                    con.executemany('INSERT OR IGNORE INTO tags VALUES (?,?,?,?)',
                                    ((field,tag,key(v),ordinal) for tag,v in tags))
        for field,value in vars(trace).items():
            if field == 'lookup_index':
                for name,lookup in value.items(): put('lookup.'+name,lookup)
            else: put(field,value)
        from .diagnostics import recorded_groups
        for group in recorded_groups(trace):
            iid,gid=key(group['invocation_id']),key(group['group_id'])
            con.execute('INSERT INTO joint_groups VALUES (?,?,?)',(iid,gid,document(group)))
            con.executemany('INSERT INTO joint_members VALUES (?,?,?)',
                            ((key(ref),iid,gid) for ref in group['final_cell_refs']))
        con.commit()
        require(con.execute('PRAGMA integrity_check').fetchone()[0]=='ok','invalid SQLite index build')
    finally:
        con.close()
    pi.require_evidence_unchanged(verified_artifacts, pi.artifact_inventory(directory))
    os.replace(temporary,target)
    descriptor={'artifact':NAME,'schema_version':SCHEMA,'implementation':pi.implementation_fingerprint(),
                'binding':pi._binding(manifest,artifacts),'database_sha256':digest(target)}
    secret=pi._authentication_key(create=True)
    descriptor['authentication']={'algorithm':'hmac-sha256','key_id':hashlib.sha256(secret).hexdigest(),
                                  'signature':authenticate(secret,descriptor)}
    return descriptor


class Store:
    def __init__(self,path):
        self.connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
        self.connection.execute('PRAGMA query_only=ON')
        self.cache=OrderedDict()
        self.cache_bytes=0
        self.decoded_records=0
        self.fields={name:(kind,count,doc) for name,kind,count,doc in self.connection.execute('SELECT * FROM fields')}

    def __del__(self):
        if hasattr(self,'connection'): self.connection.close()

    def value(self,doc):
        if doc in self.cache:
            self.cache.move_to_end(doc)
            return self.cache[doc][0]
        found=self.connection.execute('SELECT data FROM documents WHERE id=?',(doc,)).fetchone()
        require(found is not None,'SQLite index references missing document')
        raw=zlib.decompress(found[0]);self.decoded_records+=1
        from .persistent_index import _decode
        value=_decode(json.loads(raw))
        links = self.connection.execute('SELECT field,value FROM document_fields WHERE doc=?',(doc,)).fetchall()
        for field, child in links:
            require(isinstance(value,dict) and field in SHARED_FIELDS and field not in value and
                    type(child) is int and 0 < child < doc, 'invalid SQLite shared field')
            child_value = self.value(child)
            require(isinstance(child_value,list), 'SQLite shared field is not an array')
            value[field] = child_value
        if len(raw)<1048576:
            self.cache[doc]=(value,len(raw));self.cache_bytes+=len(raw)
            while len(self.cache)>512 or self.cache_bytes>8*1024*1024:
                _,(_,size)=self.cache.popitem(last=False);self.cache_bytes-=size
        return value

    def field(self,name):
        kind,count,doc=self.fields[name]
        if kind=='value':return self.value(doc)
        if name=='edges':return Edges(self,name,count)
        return Rows(self,name,count) if kind=='sequence' else Objects(self,name,count)

    def source_ids(self,file,start,end):
        return [json.loads(row[0]) for row in self.connection.execute(
            'SELECT sid FROM positions WHERE file=? AND first_line<=? AND last_line>=?',(str(file),end,start))]

    def select(self,field,tag,values):
        indices=set()
        for value in values:
            indices.update(row[0] for row in self.connection.execute(
                'SELECT ordinal FROM tags WHERE field=? AND tag=? AND value=?',(field,tag,key(value))))
        rows=self.field(field)
        return [(i,rows[i]) for i in sorted(indices)]

    def groups(self,refs):
        seen=set()
        for ref in refs:
            for (doc,) in self.connection.execute('''SELECT g.doc FROM joint_members m
                    JOIN joint_groups g USING (invocation,group_id) WHERE m.ref=?''',(key(ref),)):
                if doc not in seen:
                    seen.add(doc)
                    yield self.value(doc)


class Objects(Mapping):
    def __init__(self,store,field,count):self.store,self.field,self.count=store,field,count
    def __len__(self):return self.count
    def __iter__(self):
        from .persistent_index import _decode
        for (k,) in self.store.connection.execute('SELECT key FROM items WHERE field=? ORDER BY ordinal',(self.field,)):
            yield _decode(json.loads(k))
    def __getitem__(self,k):
        row=self.store.connection.execute('SELECT doc FROM items WHERE field=? AND key=?',(self.field,key(k))).fetchone()
        if row is None:raise KeyError(k)
        return self.store.value(row[0])
    def __contains__(self,k):
        return self.store.connection.execute('SELECT 1 FROM items WHERE field=? AND key=?',(self.field,key(k))).fetchone() is not None
    def items(self):
        from .persistent_index import _decode
        for k,doc in self.store.connection.execute('SELECT key,doc FROM items WHERE field=? ORDER BY ordinal',(self.field,)):
            yield _decode(json.loads(k)),self.store.value(doc)
    def values(self):
        return (v for _,v in self.items())
    def __deepcopy__(self,memo):
        from copy import deepcopy
        return deepcopy(dict(self.items()),memo)


class Rows(Sequence):
    def __init__(self,store,field,count):self.store,self.field,self.count=store,field,count
    def __len__(self):return self.count
    def __getitem__(self,i):
        if isinstance(i,slice):return [self[j] for j in range(*i.indices(self.count))]
        if i<0:i+=self.count
        if i<0 or i>=self.count:raise IndexError(i)
        row=self.store.connection.execute('SELECT doc FROM items WHERE field=? AND ordinal=?',(self.field,i)).fetchone()
        require(row is not None,'SQLite index sequence gap')
        return self.store.value(row[0])
    def __iter__(self):
        for (doc,) in self.store.connection.execute('SELECT doc FROM items WHERE field=? ORDER BY ordinal',(self.field,)):
            yield self.store.value(doc)
    def __eq__(self,other):return len(self)==len(other) and all(a==b for a,b in zip(self,other))
    def __deepcopy__(self,memo):
        from copy import deepcopy
        return deepcopy(list(self),memo)


class Edges(Rows):
    SQL='''SELECT e.ordinal,s.value,t.value,e.event,e.doc FROM edges e
           JOIN refs s ON s.id=e.src JOIN refs t ON t.id=e.dst'''
    def decode(self,row):
        _,source,target,event,doc=row
        from .persistent_index import _decode
        return {'from':_decode(json.loads(source)),'to':_decode(json.loads(target)),
                'event_id':event,**self.store.value(doc)}
    def __getitem__(self,i):
        if isinstance(i,slice):return [self[j] for j in range(*i.indices(self.count))]
        if i<0:i+=self.count
        if i<0 or i>=self.count:raise IndexError(i)
        row=self.store.connection.execute(self.SQL+' WHERE e.ordinal=?',(i,)).fetchone()
        require(row is not None,'SQLite edge sequence gap')
        return self.decode(row)
    def __iter__(self):
        for row in self.store.connection.execute(self.SQL+' ORDER BY e.ordinal'):
            yield self.decode(row)
    def adjacent(self,node,reverse=False):
        column='dst' if reverse else 'src'
        return [(row[0],self.decode(row)) for row in self.store.connection.execute(
            self.SQL+' WHERE e.'+column+'=(SELECT id FROM refs WHERE value=?) ORDER BY e.ordinal',(key(node),))]


def read(directory,manifest,trace_class):
    from . import persistent_index as pi
    path=Path(directory)/NAME
    if not path.is_file():return None,'missing'
    descriptor=manifest.get('verified_index',{})
    if descriptor.get('schema_version')!=SCHEMA:return None,'schema_changed'
    if descriptor.get('implementation')!=pi.implementation_fingerprint():return None,'implementation_changed'
    if descriptor.get('binding')!=pi._binding(manifest,manifest['artifacts']):return None,'archive_binding_changed'
    require(descriptor.get('database_sha256')==manifest['artifacts'][NAME]['sha256'],'SQLite index payload checksum mismatch')
    secret=pi._authentication_key()
    if secret is None:return None,'local_authentication_unavailable'
    authentication=descriptor.get('authentication',{})
    if authentication.get('key_id')!=hashlib.sha256(secret).hexdigest():return None,'local_authentication_key_changed'
    require(authentication.get('algorithm')=='hmac-sha256' and isinstance(authentication.get('signature'),str) and
            hmac.compare_digest(authentication['signature'],authenticate(secret,{k:v for k,v in descriptor.items() if k!='authentication'})),
            'SQLite index authentication failed')
    try:
        store=Store(path)
        require(set(store.fields)==pi._FIELDS-{'lookup_index'}|{'lookup.'+k for k in
                ('sources_to_final','sources_to_assignments','consumers','consumer_rows','drivers','aliases')},'invalid SQLite trace fields')
        trace=trace_class.__new__(trace_class)
        for field in pi._FIELDS-{'lookup_index'}:setattr(trace,field,store.field(field))
        trace.lookup_index={k.removeprefix('lookup.'):store.field(k) for k in store.fields if k.startswith('lookup.')}
        require(trace.records[0]['run_id']==manifest['run_id'],'SQLite index run ID mismatch')
        return trace,'hit'
    except (sqlite3.Error,ValueError,KeyError,TypeError,zlib.error) as exc:
        raise TraceError(f'invalid SQLite index: {exc}') from exc


def store_for(trace):
    return getattr(trace.edges,'store',None)


def selected(trace,field,tag,values):
    store=store_for(trace)
    if store is not None:return store.select(field,tag,values)
    values=set(values)
    return [(i,row) for i,row in enumerate(getattr(trace,field)) if
            (bool(values.intersection(row.get('origins',[]))) if tag=='origin' else row.get(tag) in values)]
