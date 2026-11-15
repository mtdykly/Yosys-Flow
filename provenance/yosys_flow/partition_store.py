"""Read authenticated, supplementary functional regions from a derived archive.

Original candidate provenance is retained. These certificates authorize a bounded
edit-impact selection, not a claim of complete causal history or arbitrary ECO
safety. Ordinary source-cut and new-patch checks still apply.
"""
import json
from pathlib import Path

from .common import require, unescape
from .index_store import store_for


def certificates(context):
    if hasattr(context,'_functional_partitions'): return context._functional_partitions
    descriptor=getattr(context,'manifest',{}).get('functional_partitions')
    rows=[]
    if descriptor:
        require(descriptor['artifact'] in context.manifest['artifacts'],'partition certificate missing from archive')
        # load_run has authenticated the database and the manifest-bound raw
        # artifact inventory before this context becomes available.
        store=store_for(context.trace)
        require(store is not None and store.backend=='sqlite','partition archive requires its authenticated SQLite index')
        rows=[json.loads(payload) for payload, in store.connection.execute(
            'SELECT payload FROM functional_partitions ORDER BY id')]
        require(len(rows)==descriptor['count'],'partition index inventory mismatch')
        for row in rows:
            require(row['proof']['status']=='verified' and row['proof']['all_external_outputs'],
                    'unverified functional partition')
    context._functional_partitions=rows
    return rows


def source_seeds(context, selector):
    """Source-position lookup only; never enumerate broad candidate final gates."""
    require(selector.get('object') is None,'operator selection needs a source position')
    source=Path(selector['source']).resolve();line=selector['line'];end_line=selector.get('end_line') or line
    column=selector.get('column');end_column=selector.get('end_column') or column
    require(line>0 and end_line>=line,'invalid operator source range')
    require(column is None or column>0,'invalid operator source column')
    require(selector.get('end_column') is None or (column is not None and end_column>0),'invalid operator end column')
    start=(line,column or 0);end=(end_line,end_column if column else 10**9)
    require(start<=end,'reversed operator source range')
    store=store_for(context.trace)
    ids=(store.source_ids(source,line,end_line) if store else context.trace.sources)
    selected=set()
    for sid in ids:
        row=context.trace.sources[sid]
        if Path(row['file']).resolve()!=source:continue
        if (row['line'],row['column'])<=end and (row['end_line'],row['end_column'])>start:selected.add(sid)
    return selected


def operation_roots(context, selector, cell_name):
    if len(context.trace.instance_catalog)!=1:return []
    if selector.get('instance') not in (None,context.manifest['configuration']['top']):return []
    roots={r for c in certificates(context) for r in c['operation_roots']}
    if not roots:return []
    seeds=source_seeds(context,selector);found=[]
    for ref in roots:
        row=context.trace.objects[ref];state=row.get('state') or {}
        if unescape(row.get('name',state.get('name','')))==unescape(cell_name) and seeds.intersection(row['origins']):
            found.append(ref)
    return sorted(found)


def operation_certificate(context, roots):
    matches=[c for c in certificates(context) if set(roots)<=set(c['operation_roots'])]
    return min(matches,key=lambda c:len(c['final_cell_refs'])) if matches else None


def region_certificate(context, refs):
    return next((c for c in certificates(context) if set(c['final_cell_refs'])==set(refs)),None)


def signals(context, path, wire, offset):
    if not hasattr(context,'_partition_endpoints'):
        mapping={}
        for c in certificates(context):
            for row in c['anchors']:
                key=(row['instance'],unescape(row['wire']),row['offset'])
                value=(row['final_bit'],bool(row['inverted']))
                values=mapping.setdefault(key,[])
                require(not any(bit==value[0] and phase!=value[1] for bit,phase in values),
                        'conflicting certified signal anchor polarity')
                if value not in values:values.append(value)
        context._partition_endpoints=mapping
    return context._partition_endpoints.get((path,unescape(wire),offset),[])


def signal(context,path,wire,offset):
    values=signals(context,path,wire,offset)
    return min(values,key=lambda v:v[1]) if values else None


def endpoint(context,path,wire,offset):
    result=signal(context,path,wire,offset)
    return result[0] if result is not None and not result[1] else None
