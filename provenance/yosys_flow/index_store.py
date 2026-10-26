"""Shared deduplicated representation and query views for index backends.

Backends only encode rows and implement lookups. Object identity, field sharing,
edge order, cache policy and provenance queries are independent of storage.
"""
from collections import OrderedDict
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path

from .common import require

SHARED_FIELDS = {'origins', 'recorded_origins', 'inputs', 'values', 'enclosing_controls'}
EAGER = {'design', 'instance_catalog', 'view_rule', 'final', 'gaps',
         'abc_fallbacks', 'process_connection_proofs', 'hierarchy'}
TABLE_WIDTHS = {'documents':3, 'document_fields':3, 'fields':4, 'items':4, 'refs':2,
                'edges':5, 'positions':4, 'tags':4, 'joint_groups':3, 'joint_members':3}


def key(value):
    from .persistent_index import _encode, _json_bytes
    return _json_bytes(_encode(value)).decode()


def populate(trace, sink):
    """Emit identical normalized rows to a streaming SQL or in-memory gzip sink."""
    from . import persistent_index as pi
    pi.build_lookup(trace)
    require(set(vars(trace)) == pi._FIELDS, 'native trace fields changed; update index schema')
    seen, refs, arrays = {}, {}, {}

    def document(value):
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
            doc = len(seen) + 1
            seen[sha] = doc
            sink.document(doc, sha, payload)
            for field, child in links.items():sink.add('document_fields', (doc, field, child))
        doc = seen[sha]
        if cacheable: arrays[id(value)] = (value, doc)
        return doc

    def reference(value):
        encoded = key(value)
        if encoded not in refs:
            refs[encoded] = len(refs) + 1
            sink.add('refs', (refs[encoded], encoded))
        return refs[encoded]

    def put(field, value):
        if field in EAGER or not isinstance(value, (dict, list)):
            sink.add('fields', (field, 'value', 0, document(value)))
            return
        kind = 'mapping' if isinstance(value, dict) else 'sequence'
        sink.add('fields', (field, kind, len(value), None))
        for ordinal, (k, row) in enumerate(value.items() if isinstance(value, dict) else enumerate(value)):
            if field == 'edges':
                doc = document({k:v for k,v in row.items() if k not in {'from','to','event_id'}})
                sink.add('edges', (ordinal, reference(row['from']), reference(row['to']), row['event_id'], doc))
            else:
                sink.add('items', (field, key(k), ordinal, document(row)))
            if field == 'sources':
                sink.add('positions', (key(k), str(Path(row['file']).resolve()), row['line'], row['end_line']))
            if isinstance(row, dict) and kind == 'sequence' and field not in {'records','edges'}:
                tags = [(tag,row[tag]) for tag in ('ref','seq','invocation_id','action') if tag in row]
                origins = row.get('origins',[]) if field in {'regions','process_discards'} or (
                    field == 'events' and row.get('record_type') in {'process_action','process_action_detail','process_discard'}) else []
                if field == 'removals':origins = trace.objects[row['ref']]['origins']
                tags.extend(('origin',sid) for sid in origins)
                for entry in dict.fromkeys((field,tag,key(v),ordinal) for tag,v in tags):
                    sink.add('tags', entry)

    for field, value in vars(trace).items():
        if field == 'lookup_index':
            for name, lookup in value.items():put('lookup.'+name, lookup)
        else:put(field, value)
    from .diagnostics import recorded_groups
    for group in recorded_groups(trace):
        iid,gid = key(group['invocation_id']),key(group['group_id'])
        sink.add('joint_groups', (iid,gid,document(group)))
        for ref in group['final_cell_refs']:sink.add('joint_members', (key(ref),iid,gid))


class DocumentStore:
    def init_cache(self):
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.decoded_records = 0

    def value(self, doc):
        if doc in self.cache:
            self.cache.move_to_end(doc)
            return self.cache[doc][0]
        raw = self.payload(doc)
        self.decoded_records += 1
        from .persistent_index import _decode
        value = _decode(json.loads(raw))
        for field, child in self.links(doc):
            require(isinstance(value,dict) and field in SHARED_FIELDS and field not in value and
                    type(child) is int and 0 < child < doc, 'invalid index shared field')
            value[field] = self.value(child)
            require(isinstance(value[field],list), 'index shared field is not an array')
        if len(raw) < 1048576:
            self.cache[doc] = (value,len(raw));self.cache_bytes += len(raw)
            while len(self.cache) > 512 or self.cache_bytes > 8*1024*1024:
                _,(_,size) = self.cache.popitem(last=False);self.cache_bytes -= size
        return value

    def field(self, name):
        kind,count,doc = self.fields[name]
        if kind == 'value':return self.value(doc)
        if name == 'edges':return Edges(self,name,count)
        return Rows(self,name,count) if kind == 'sequence' else Objects(self,name,count)

    def select(self, field, tag, values):
        indices = {i for value in values for i in self.tag_indices(field,tag,key(value))}
        rows = self.field(field)
        return [(i,rows[i]) for i in sorted(indices)]

    def groups(self, refs):
        seen = set()
        for ref in refs:
            for doc in self.group_documents(key(ref)):
                if doc not in seen:
                    seen.add(doc)
                    yield self.value(doc)


class Objects(Mapping):
    def __init__(self,store,field,count):self.store,self.field,self.count=store,field,count
    def __len__(self):return self.count
    def __iter__(self):
        from .persistent_index import _decode
        for k,_ in self.store.iter_items(self.field):yield _decode(json.loads(k))
    def __getitem__(self,k):
        doc = self.store.item_document(self.field,key(k))
        if doc is None:raise KeyError(k)
        return self.store.value(doc)
    def __contains__(self,k):return self.store.item_document(self.field,key(k)) is not None
    def items(self):
        from .persistent_index import _decode
        for k,doc in self.store.iter_items(self.field):yield _decode(json.loads(k)),self.store.value(doc)
    def values(self):return (v for _,v in self.items())
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
        doc = self.store.sequence_document(self.field,i)
        require(doc is not None, 'index sequence gap')
        return self.store.value(doc)
    def __iter__(self):
        for _,doc in self.store.iter_items(self.field):yield self.store.value(doc)
    def __eq__(self,other):return len(self)==len(other) and all(a==b for a,b in zip(self,other))
    def __deepcopy__(self,memo):
        from copy import deepcopy
        return deepcopy(list(self),memo)


class Edges(Rows):
    def decode(self,row):
        _,source,target,event,doc=row
        from .persistent_index import _decode
        return {'from':_decode(json.loads(source)), 'to':_decode(json.loads(target)),
                'event_id':event, **self.store.value(doc)}
    def __getitem__(self,i):
        if isinstance(i,slice):return [self[j] for j in range(*i.indices(self.count))]
        if i<0:i+=self.count
        if i<0 or i>=self.count:raise IndexError(i)
        return self.decode(self.store.edge_at(i))
    def __iter__(self):
        for row in self.store.iter_edges():yield self.decode(row)
    def adjacent(self,node,reverse=False):
        return [(row[0],self.decode(row)) for row in self.store.adjacent_rows(key(node),reverse)]


def restore(store, manifest, trace_class):
    from .persistent_index import _FIELDS
    require(set(store.fields) == _FIELDS-{'lookup_index'} | {'lookup.'+k for k in
            ('sources_to_final','sources_to_assignments','consumers','consumer_rows','drivers','aliases')},
            'invalid index trace fields')
    trace = trace_class.__new__(trace_class)
    for field in _FIELDS-{'lookup_index'}:setattr(trace,field,store.field(field))
    trace.lookup_index = {k.removeprefix('lookup.'):store.field(k) for k in store.fields if k.startswith('lookup.')}
    require(trace.records[0]['run_id']==manifest['run_id'], 'index run ID mismatch')
    return trace


def store_for(trace):return getattr(trace.edges,'store',None)


def selected(trace,field,tag,values):
    store = store_for(trace)
    if store is not None:return store.select(field,tag,values)
    values=set(values)
    return [(i,row) for i,row in enumerate(getattr(trace,field)) if
            (bool(values.intersection(row.get('origins',[]))) if tag=='origin' else row.get(tag) in values)]
