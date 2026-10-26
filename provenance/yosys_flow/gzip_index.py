"""Authenticated, deduplicated gzip index with in-memory relation lookups.

This is schema 2, not the legacy expanded trace JSON. It uses the same row
builder and query views as SQLite and never creates an intermediate database.
"""
import bisect
from collections import defaultdict
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import zlib

from .common import TraceError, digest, require
from .index_store import DocumentStore, TABLE_WIDTHS, SHARED_FIELDS, key, populate, restore

NAME = 'verified-index.json.gz'
SCHEMA = 2
KIND = 'deduplicated_native_index'


class Sink:
    def __init__(self):self.tables = {name:[] for name in TABLE_WIDTHS}
    def add(self, table, row):self.tables[table].append(row)
    def document(self, doc, sha, payload):self.add('documents', (doc,sha.hex(),payload.decode()))


def authenticate(secret, descriptor):
    return hmac.new(secret,b'yosys-flow-gzip-index-v2\0'+key(descriptor).encode(),hashlib.sha256).hexdigest()


def write(directory, manifest, trace, verified_artifacts):
    from . import persistent_index as pi
    directory = Path(directory)
    artifacts = pi.artifact_inventory(directory)
    pi.require_evidence_unchanged(verified_artifacts,artifacts)
    require(artifacts == verified_artifacts, 'unverified artifact appeared before index signing')
    target,temporary = directory/NAME,directory/(NAME+'.tmp')
    temporary.unlink(missing_ok=True)
    try:
        sink = Sink()
        populate(trace,sink)
        envelope = {'schema_version':SCHEMA,'kind':KIND,'tables':sink.tables}
        with temporary.open('wb') as stream:
            with gzip.GzipFile(fileobj=stream,mode='wb',filename='',compresslevel=1,mtime=0) as compressed:
                compressed.write(pi._json_bytes(envelope))
            stream.flush()
            os.fsync(stream.fileno())
        pi.require_evidence_unchanged(verified_artifacts,pi.artifact_inventory(directory))
        os.replace(temporary,target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    descriptor = {'artifact':NAME,'backend':'gzip','schema_version':SCHEMA,
                  'implementation':pi.implementation_fingerprint(),
                  'binding':pi._binding(manifest,artifacts),'gzip_sha256':digest(target)}
    secret = pi._authentication_key(create=True)
    descriptor['authentication'] = {'algorithm':'hmac-sha256','key_id':hashlib.sha256(secret).hexdigest(),
                                    'signature':authenticate(secret,descriptor)}
    return descriptor


class Store(DocumentStore):
    backend = 'gzip'

    def __init__(self,path):
        envelope = json.loads(gzip.decompress(Path(path).read_bytes()))
        require(isinstance(envelope,dict) and set(envelope)=={'schema_version','kind','tables'} and
                envelope['schema_version']==SCHEMA and envelope['kind']==KIND, 'invalid gzip index envelope')
        tables = envelope['tables']
        require(isinstance(tables,dict) and set(tables)==set(TABLE_WIDTHS), 'invalid gzip index tables')
        for name,width in TABLE_WIDTHS.items():
            require(isinstance(tables[name],list) and all(isinstance(r,list) and len(r)==width for r in tables[name]),
                    f'invalid gzip index table: {name}')
        self.init_cache()
        self.documents = {}
        for doc,sha,payload in tables.pop('documents'):
            require(type(doc) is int and doc==len(self.documents)+1 and isinstance(sha,str) and
                    re.fullmatch('[0-9a-f]{64}',sha) and isinstance(payload,str), 'invalid gzip document identity')
            self.documents[doc] = payload
        self.shared = defaultdict(dict)
        for doc,field,child in tables.pop('document_fields'):
            require(doc in self.documents and type(child) is int and child in self.documents and 0<child<doc
                    and field in SHARED_FIELDS and field not in self.shared[doc], 'invalid gzip shared field')
            self.shared[doc][field] = child
        self.fields = {}
        for name,kind,count,doc in tables.pop('fields'):
            require(isinstance(name,str) and name not in self.fields and kind in {'mapping','sequence','value'}
                    and type(count) is int and count>=0 and
                    ((kind=='value' and count==0 and doc in self.documents) or (kind!='value' and doc is None)),
                    'invalid gzip index field')
            self.fields[name] = kind,count,doc
        self.items = defaultdict(dict)
        ordinals = defaultdict(dict)
        for field,encoded,ordinal,doc in tables.pop('items'):
            require(field in self.fields and self.fields[field][0]!='value' and field!='edges' and
                    isinstance(encoded,str) and encoded not in self.items[field] and
                    type(ordinal) is int and 0<=ordinal<self.fields[field][1] and
                    ordinal not in ordinals[field] and doc in self.documents, 'invalid gzip item identity')
            self.items[field][encoded] = doc
            ordinals[field][ordinal] = encoded,doc
        self.ordered = {field:[row for _,row in sorted(rows.items())] for field,rows in ordinals.items()}
        for field,(kind,count,_) in self.fields.items():
            if kind!='value' and field!='edges':
                require(len(self.ordered.get(field,[]))==count, 'gzip index sequence gap')
        self.refs = {}
        self.ref_ids = {}
        for rid,value in tables.pop('refs'):
            require(type(rid) is int and rid==len(self.refs)+1 and isinstance(value,str) and value not in self.ref_ids,
                    'invalid gzip relation identity')
            self.refs[rid] = value
            self.ref_ids[value] = rid
        self.edge_rows = tables.pop('edges')
        require(len(self.edge_rows)==self.fields.get('edges',('',-1))[1], 'gzip edge count mismatch')
        self.forward,self.reverse = defaultdict(list),defaultdict(list)
        for expected,(i,source,target,event,doc) in enumerate(self.edge_rows):
            require(type(i) is int and i==expected and source in self.refs and target in self.refs and
                    type(event) is int and event>=0 and doc in self.documents, 'invalid gzip edge')
            self.forward[source].append(i);self.reverse[target].append(i)
        self.positions = defaultdict(list)
        seen = set()
        for sid,file,start,end in tables.pop('positions'):
            require(isinstance(sid,str) and sid not in seen and isinstance(file,str) and
                    type(start) is int and type(end) is int and 0<=start<=end, 'invalid gzip source position')
            seen.add(sid);self.positions[file].append((start,end,sid))
        for rows in self.positions.values():rows.sort()
        self.tags = defaultdict(list)
        seen = set()
        for field,tag,value,ordinal in tables.pop('tags'):
            entry = field,tag,value,ordinal
            require(field in self.fields and self.fields[field][0]=='sequence' and field!='edges' and
                    isinstance(tag,str) and isinstance(value,str) and type(ordinal) is int and
                    0<=ordinal<self.fields[field][1] and entry not in seen, 'invalid gzip tag')
            seen.add(entry);self.tags[field,tag,value].append(ordinal)
        groups = {}
        for invocation,group,doc in tables.pop('joint_groups'):
            require(isinstance(invocation,str) and isinstance(group,str) and
                    (invocation,group) not in groups and doc in self.documents, 'invalid gzip group')
            groups[invocation,group] = doc
        self.members = defaultdict(list)
        seen = set()
        for ref,invocation,group in tables.pop('joint_members'):
            entry = ref,invocation,group
            require(isinstance(ref,str) and (invocation,group) in groups and entry not in seen,
                    'invalid gzip group member')
            seen.add(entry);self.members[ref].append(groups[invocation,group])

    def payload(self,doc):
        require(doc in self.documents,'gzip index references missing document')
        return self.documents[doc].encode()

    def links(self,doc):return self.shared.get(doc,{}).items()
    def iter_items(self,field):return iter(self.ordered.get(field,[]))
    def item_document(self,field,encoded):return self.items.get(field,{}).get(encoded)
    def sequence_document(self,field,ordinal):
        rows = self.ordered.get(field,[])
        return rows[ordinal][1] if 0<=ordinal<len(rows) else None
    def edge_at(self,i):
        ordinal,source,target,event,doc = self.edge_rows[i]
        return ordinal,self.refs[source],self.refs[target],event,doc
    def iter_edges(self):return (self.edge_at(i) for i in range(len(self.edge_rows)))
    def adjacent_rows(self,encoded,reverse):
        lookup = self.reverse if reverse else self.forward
        return (self.edge_at(i) for i in lookup.get(self.ref_ids.get(encoded),[]))
    def source_ids(self,file,start,end):
        rows = self.positions.get(str(file),[])
        limit = bisect.bisect_right(rows,(end,float('inf'),'\uffff'))
        return [json.loads(sid) for _,last,sid in rows[:limit] if last>=start]
    def tag_indices(self,field,tag,encoded):return self.tags.get((field,tag,encoded),[])
    def group_documents(self,encoded):return self.members.get(encoded,[])


def read(directory,manifest,trace_class):
    from . import persistent_index as pi
    path = Path(directory)/NAME
    if not path.is_file():return None,'missing'
    descriptor = manifest.get('verified_index',{})
    if descriptor.get('schema_version')!=SCHEMA:return None,'schema_changed'
    if descriptor.get('implementation')!=pi.implementation_fingerprint():return None,'implementation_changed'
    if descriptor.get('binding')!=pi._binding(manifest,manifest['artifacts']):return None,'archive_binding_changed'
    require(descriptor.get('artifact')==NAME and descriptor.get('backend')=='gzip' and
            descriptor.get('gzip_sha256')==manifest['artifacts'][NAME]['sha256'], 'gzip index payload checksum mismatch')
    secret = pi._authentication_key()
    if secret is None:return None,'local_authentication_unavailable'
    authentication = descriptor.get('authentication',{})
    if authentication.get('key_id')!=hashlib.sha256(secret).hexdigest():return None,'local_authentication_key_changed'
    require(authentication.get('algorithm')=='hmac-sha256' and isinstance(authentication.get('signature'),str) and
            hmac.compare_digest(authentication['signature'],authenticate(secret,{k:v for k,v in descriptor.items() if k!='authentication'})),
            'gzip index authentication failed')
    try:
        return restore(Store(path),manifest,trace_class),'hit'
    except (ValueError,KeyError,IndexError,TypeError,OSError,EOFError,RecursionError,zlib.error) as exc:
        raise TraceError(f'invalid gzip index: {exc}') from exc
