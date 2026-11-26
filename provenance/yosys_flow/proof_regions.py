"""Compose a requested old region from a signed function library.

The library is target-independent. Target-specific closure and missing source
proofs are online costs. A successful cut is cached separately with its own
authentication; no raw archive or complete SQLite index is copied.
"""
import hmac
import json
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager

from .common import TraceError, require, unescape
from .proof_store import Reader, StoredPartitioner, signature
from .boolean import ProofBudgetExceeded
from .online_evidence import EvidenceBlocked


def attach(context, directory=None):
    if directory is False: return
    path = Path(directory) if directory else context.directory.with_name(context.directory.name+'.proofs')
    if directory or path.exists():
        context.proof_evidence = Reader(context, path)


def prove_source(context, document):
    from .source_regions import prove
    start = time.perf_counter()
    proof = None
    try:
        from .online_evidence import scope
        with scope(context,'old_rtl_to_independent_region',document):proof = prove(context, document)
        return proof
    finally:
        key = ('old_source_certificate_lookup_seconds' if proof and proof.get('reused_from_authenticated_partition')
               else 'old_source_cut_proof_seconds')
        context.proof_timings[key] = context.proof_timings.get(key, 0.0)+time.perf_counter()-start


@contextmanager
def cache_connection(store):
    db = sqlite3.connect(cache_path(store))
    db.executescript('''CREATE TABLE IF NOT EXISTS certificates(id TEXT PRIMARY KEY, payload TEXT, auth TEXT);
                       CREATE TABLE IF NOT EXISTS roots(root TEXT, id TEXT, PRIMARY KEY(root,id));''')
    try:
        with db: yield db
    finally:
        db.close()


def cache_path(store):
    # Immutable generations may be queried while enhancement continues. Region
    # certificates stay bound to the exact generation that authorized them.
    return store.directory/('regions-'+store.document['sha256']+'.sqlite3' if
                            store.document['database']!='functions.sqlite3' else 'regions.sqlite3')


def envelope(store, certificate):
    return {'library': store.document['sha256'],
            'baseline': store.document['baseline_manifest_sha256'], 'certificate': certificate}


def remembered(store, roots):
    if not cache_path(store).is_file(): return None
    with cache_connection(store) as db:
        # Index rows only propose candidates. The signed payload and root set
        # must independently confirm the requested operation coverage.
        rows = db.execute('SELECT c.payload,c.auth FROM certificates c JOIN roots r ON c.id=r.id WHERE r.root=?',
                          (roots[0],)).fetchall()
    matches = []
    for payload, auth in rows:
        row = json.loads(payload)
        require(hmac.compare_digest(signature(envelope(store, row)), auth), 'region evidence authentication mismatch')
        if set(roots) <= set(row['operation_roots']): matches.append(row)
    return min(matches, key=lambda r: len(r['final_cell_refs'])) if matches else None


def remember(store, row):
    require(row['proof']['status'] == 'verified' and row['proof']['all_external_outputs'], 'unproved partition')
    require(row['source_proof']['status'] == 'verified' and row['source_proof']['all_outputs'], 'unproved source cut')
    with cache_connection(store) as db:
        db.execute('INSERT OR REPLACE INTO certificates VALUES(?,?,?)',
                   (row['id'], json.dumps(row), signature(envelope(store, row))))
        db.executemany('INSERT OR IGNORE INTO roots VALUES(?,?)', ((r, row['id']) for r in row['operation_roots']))


def install(context, row):
    from .partition_store import certificates
    rows = certificates(context)
    if not any(c['id'] == row['id'] for c in rows): rows.append(row)
    context.__dict__.setdefault('_proof_library_certificate_ids', set()).add(row['id'])
    context.__dict__.pop('_partition_endpoints', None)
    context._operation_descendants.clear()


def derive(context, roots):
    """No full graph normalization, BLIF parse, or raw witness replay here."""
    store = getattr(context, 'proof_evidence', None)
    if store is None or not roots: return None
    key = tuple(sorted(roots))
    attempted = context.__dict__.setdefault('_proof_region_attempts', {})
    if key in attempted:
        store.metrics['request_cache_hits']=store.metrics.get('request_cache_hits',0)+1
        return attempted[key]
    from .partition_store import certificates, operation_certificate
    existing = operation_certificate(context, roots)
    if existing: return existing
    start = time.perf_counter()
    original = list(certificates(context))
    original_ids = set(getattr(context, '_proof_library_certificate_ids', set()))
    phase = 'authenticated_region_cache'
    phase_started=time.perf_counter()
    def enter(next_phase):
        nonlocal phase,phase_started
        now=time.perf_counter();times=store.metrics.setdefault('phase_seconds',{})
        times[phase]=times.get(phase,0.0)+now-phase_started
        phase,phase_started=next_phase,now
    try:
        row = remembered(store, key)
        if row:
            install(context, row)
            store.metrics['cache_hits'] += 1
            attempted[key] = row
            return row
        enter('frontend_operation_to_abc')
        reached = {}; seen = set(); stack = list(roots)
        while stack:
            ref = stack.pop()
            if ref in seen: continue
            seen.add(ref)
            endpoints = store.cell_nodes(ref)
            if endpoints:
                for inv, name in endpoints: reached.setdefault(inv, set()).add(name)
                continue
            require(ref not in context.trace.final, 'operator escapes the stored ABC boundary')
            stack.extend(e['to'] for _, e in context.trace.edges.adjacent(ref))
        require(len(reached) == 1, 'operation needs one covered ABC invocation')
        inv, seeds = next(iter(reached.items()))
        engine = StoredPartitioner(store, inv)
        enter('abc_independent_output_boundary')
        context.notify('compose_stored_region', invocation=inv, seed_nodes=len(seeds))
        from .online_evidence import scope
        with scope(context,'abc_independent_output_boundary'):
            proof = engine.region(sorted(seeds),limit=getattr(getattr(context,'evidence_budget',None),'node_limit',4096))
        from .partition_archive import bind
        enter('abc_to_final_netlist_binding')
        region = next(r for r in context.trace.regions if r['invocation_id'] == inv)
        row = bind(context, region, proof)
        require(set(roots) <= set(row['operation_roots']), 'region does not close every requested operation')
        # Use proved functional anchors to construct the independent old-RTL cut;
        # the provisional row does NOT yet carry a successful source proof.
        install(context, row)
        from .extract import slice_module
        from .diagnostics import audit_cut
        from .source_regions import cut_signature
        names = [unescape(context.trace.objects[r]['state']['name']) for r in row['final_cell_refs']]
        _, boundary = slice_module(context.module, names, 'region', [])
        require(audit_cut(context.module, names, boundary)['selected_cell_cut_complete'], 'incomplete final boundary')
        document = {'boundary': boundary, 'region': {'cells': [{'ref': r} for r in row['final_cell_refs']],
                    'connections': [], 'recorded_cell_functions': {n: context.module['cells'][n] for n in names}}}
        context.notify('prove_missing_old_source_cut', gates=len(names), ports=len(boundary))
        enter('old_rtl_to_independent_region')
        source_proof = prove_source(context, document)
        require(source_proof['status'] == 'verified' and source_proof['all_outputs'],
                'stored functions do not certify the old source cut: '+source_proof.get('reason', 'unknown'))
        row.update(source_proof=source_proof, source_cut_sha256=cut_signature(document), boundary=boundary)
        enter('publish_region_certificate')
        remember(store, row)
        store.metrics['derived_regions'] += 1
        attempted[key] = row
        return row
    except (TraceError, ProofBudgetExceeded, EvidenceBlocked) as exc:
        # Integrity failures must fail closed; callers must not silently fall back
        # after a corrupted cache. Functional coverage misses can use the existing
        # conservative selection/proof path, with an explicit diagnostic.
        if 'authentication' in str(exc): raise
        context._functional_partitions = original
        context._proof_library_certificate_ids = original_ids
        context.__dict__.pop('_partition_endpoints', None)
        context._operation_descendants.clear()
        detail = {'roots': list(key), 'reason': str(exc), 'missing_evidence_stage': phase, 'proven_safe': False}
        if hasattr(exc, 'detail'): detail['window'] = exc.detail
        store.metrics['misses'].append(detail)
        context.notify('stored_region_coverage_miss', **detail)
        attempted[key] = None
        if isinstance(exc,EvidenceBlocked):raise
        return None
    finally:
        enter('finished')
        store.metrics['region_preparation_seconds'] += time.perf_counter()-start
