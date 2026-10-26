"""Authenticated SQLite backend for the shared deduplicated index."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import zlib
from .common import TraceError, digest, require
from .index_store import (DocumentStore, Objects, Rows, Edges, SHARED_FIELDS, EAGER,
                          TABLE_WIDTHS, key, populate, restore, store_for, selected)
NAME = 'verified-index.sqlite3'
SCHEMA = 2

SCHEMA_SQL = '''
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
        '''


class Sink:
    def __init__(self, connection):
        self.connection = connection
        self.inserts = {table:'INSERT INTO '+table+' VALUES ('+','.join('?' for _ in range(width))+')'
                        for table,width in TABLE_WIDTHS.items()}
    def add(self, table, row):self.connection.execute(self.inserts[table], row)
    def document(self, doc, sha, payload):self.add('documents', (doc,sha,zlib.compress(payload,1)))


def authenticate(secret, descriptor):
    return hmac.new(secret, b'yosys-flow-sqlite-index-v1\0'+key(descriptor).encode(), hashlib.sha256).hexdigest()


def write(directory, manifest, trace, verified_artifacts):
    from . import persistent_index as pi
    directory = Path(directory)
    artifacts = pi.artifact_inventory(directory)
    pi.require_evidence_unchanged(verified_artifacts, artifacts)
    require(artifacts == verified_artifacts, 'unverified artifact appeared before index signing')
    target, temporary = directory/NAME, directory/(NAME+'.tmp')
    temporary.unlink(missing_ok=True)
    try:
        con = sqlite3.connect(temporary)
        try:
            con.executescript(SCHEMA_SQL)
            con.execute('BEGIN')
            populate(trace, Sink(con))
            con.commit()
            require(con.execute('PRAGMA integrity_check').fetchone()[0]=='ok','invalid SQLite index build')
        finally:
            con.close()
        pi.require_evidence_unchanged(verified_artifacts, pi.artifact_inventory(directory))
        os.replace(temporary,target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    descriptor={'artifact':NAME,'schema_version':SCHEMA,'implementation':pi.implementation_fingerprint(),
                'binding':pi._binding(manifest,artifacts),'database_sha256':digest(target)}
    secret=pi._authentication_key(create=True)
    descriptor['authentication']={'algorithm':'hmac-sha256','key_id':hashlib.sha256(secret).hexdigest(),
                                  'signature':authenticate(secret,descriptor)}
    return descriptor


class Store(DocumentStore):
    backend = 'sqlite'
    EDGE_SQL = '''SELECT e.ordinal,s.value,t.value,e.event,e.doc FROM edges e
                  JOIN refs s ON s.id=e.src JOIN refs t ON t.id=e.dst'''
    def __init__(self,path):
        self.connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
        self.connection.execute('PRAGMA query_only=ON')
        self.init_cache()
        self.fields={name:(kind,count,doc) for name,kind,count,doc in self.connection.execute('SELECT * FROM fields')}
    def __del__(self):
        if hasattr(self,'connection'):self.connection.close()
    def payload(self,doc):
        row=self.connection.execute('SELECT data FROM documents WHERE id=?',(doc,)).fetchone()
        require(row is not None,'SQLite index references missing document')
        return zlib.decompress(row[0])
    def links(self,doc):
        return self.connection.execute('SELECT field,value FROM document_fields WHERE doc=?',(doc,)).fetchall()
    def iter_items(self,field):
        return self.connection.execute('SELECT key,doc FROM items WHERE field=? ORDER BY ordinal',(field,))
    def item_document(self,field,encoded):
        row=self.connection.execute('SELECT doc FROM items WHERE field=? AND key=?',(field,encoded)).fetchone()
        return row[0] if row is not None else None
    def sequence_document(self,field,ordinal):
        row=self.connection.execute('SELECT doc FROM items WHERE field=? AND ordinal=?',(field,ordinal)).fetchone()
        return row[0] if row is not None else None
    def edge_at(self,i):
        row=self.connection.execute(self.EDGE_SQL+' WHERE e.ordinal=?',(i,)).fetchone()
        require(row is not None,'SQLite edge sequence gap')
        return row
    def iter_edges(self):return self.connection.execute(self.EDGE_SQL+' ORDER BY e.ordinal')
    def adjacent_rows(self,encoded,reverse):
        column='dst' if reverse else 'src'
        return self.connection.execute(self.EDGE_SQL+' WHERE e.'+column+
            '=(SELECT id FROM refs WHERE value=?) ORDER BY e.ordinal',(encoded,))
    def source_ids(self,file,start,end):
        return [json.loads(row[0]) for row in self.connection.execute(
            'SELECT sid FROM positions WHERE file=? AND first_line<=? AND last_line>=?',(str(file),end,start))]
    def tag_indices(self,field,tag,encoded):
        return (row[0] for row in self.connection.execute(
            'SELECT ordinal FROM tags WHERE field=? AND tag=? AND value=?',(field,tag,encoded)))
    def group_documents(self,encoded):
        return (row[0] for row in self.connection.execute('''SELECT g.doc FROM joint_members m
                    JOIN joint_groups g USING (invocation,group_id) WHERE m.ref=?''',(encoded,)))

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
        return restore(Store(path),manifest,trace_class),'hit'
    except (sqlite3.Error,ValueError,KeyError,IndexError,TypeError,zlib.error) as exc:
        raise TraceError(f'invalid SQLite index: {exc}') from exc
