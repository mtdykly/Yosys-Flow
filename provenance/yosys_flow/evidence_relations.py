"""Typed evidence descriptors and reuse of already verified ABC cut equations."""
import hmac
import json
from pathlib import Path
import sqlite3
import uuid
from collections import Counter

from .common import digest,require
from .proof_checkpoint import atomic_json
from .proof_store import implementation,signature


def confirmed(context,region):
    inv=region['invocation_id']
    return {'proofs':{r['mapped_node']:r for r in context.trace.abc_associations
                     if r['invocation_id']==inv and r.get('matches')},
            'imports':context.trace.abc_imports.get(inv,{})}


def equations(proofs,imports,nets):
    first=next(n for n in nets.values() if n.record['kind']=='network')
    result=[]
    for name,proof in proofs.items():
        if not proof.get('matches'):continue
        require(proof.get('precision') in {'abc_internal_cut','abc_internal_joint_cut'},'unproved replay relation')
        ports=imports[name]['ports'].get('\\Y',[])
        require(len(ports)==1 and isinstance(ports[0],dict) and ports[0]['offset']==0,'unsupported verified export binding')
        export=ports[0]['wire'].lstrip('\\')
        for match in proof['matches']:
            entry=first.nodes[match['entry_root']].get('name')
            require(entry and type(match['output_inverted']) is bool,'invalid verified function equation')
            result.append({'category':'function_equivalence','entry':entry,'export':export,
                'inverted':match['output_inverted'],'method':proof['method'],
                'scope':'captured_input_functions','independent_cut_verified':True})
    return result


def install(engine,rows):
    for row in rows:
        require(row['category']=='function_equivalence' and row['independent_cut_verified'] is True and
                row['scope']=='captured_input_functions','non-functional evidence cannot merge function classes')
        require(row['entry'] in engine.a and row['export'] in engine.b and type(row['inverted']) is bool,
                'verified replay equation endpoint mismatch')
        engine.classes.join(engine.a[row['entry']],engine.b[row['export']]^int(row['inverted']))
    engine.proofs.append({'proof':'verified_replay_function_equations','count':len(rows),
                          'new_sat_calls':0,'complete_causal_history':False})


def prepare(context,directory):
    directory=Path(directory);seal=directory/'manifest.json'
    if seal.is_file():return validate(context,directory)
    directory.mkdir(parents=True,exist_ok=True);fingerprint=implementation()
    path=directory/(uuid.uuid4().hex+'.sqlite3');db=sqlite3.connect(path);counts=Counter()
    try:
        db.execute('CREATE TABLE relations(id TEXT PRIMARY KEY,domain TEXT,category TEXT,rule TEXT,locator TEXT,scope TEXT)')
        db.execute('CREATE INDEX categories ON relations(domain,category)')
        for ordinal,event in enumerate(context.trace.events):
            kind=event['record_type']
            if kind not in {'cell_create','cell_state','assignment','cell_contribution','merge','optimization_rewrite'}:continue
            checked=kind in {'merge','optimization_rewrite'}
            category='rule_checked_transform' if checked else 'source_association'
            scope='incidence_versions_and_recorded_ports' if checked else 'recorded_origin_not_function_equivalence'
            db.execute('INSERT INTO relations VALUES(?,?,?,?,?,?)',('event:'+str(ordinal),'yosys',category,
                event.get('rule',kind),json.dumps({'field':'events','index':ordinal,'seq':event['seq']}),scope));counts[category]+=1
        for i,row in enumerate(context.trace.abc_associations):
            functional=row.get('precision') in {'abc_internal_cut','abc_internal_joint_cut'}
            category='function_equivalence' if functional else 'source_association'
            scope='joint_group' if row.get('precision')=='abc_internal_joint_cut' else (
                'independent_gate_cut' if functional else 'candidate_region')
            db.execute('INSERT INTO relations VALUES(?,?,?,?,?,?)',('abc:'+str(i),'abc',category,row['method'],
                json.dumps({'field':'abc_associations','index':i,'ref':row['ref']}),scope));counts[category]+=1
        db.commit()
    finally:db.close()
    require(fingerprint==implementation(),'evidence descriptor implementation changed')
    document={'schema':1,'baseline':context.baseline['manifest_sha256'],'implementation':fingerprint,
              'database':path.name,'sha256':digest(path),'counts':dict(counts),'per_yosys_object_sat_calls':0}
    atomic_json(seal,{**document,'authentication':signature(document)})
    return document


def validate(context,directory):
    directory=Path(directory);document=json.loads((directory/'manifest.json').read_text());auth=document.pop('authentication','')
    require(hmac.compare_digest(signature(document),auth),'typed evidence authentication mismatch')
    require(document['baseline']==context.baseline['manifest_sha256'] and document['implementation']==implementation(),
            'typed evidence baseline or implementation differs')
    path=directory/document['database']
    require(path.parent==directory and path.resolve().is_relative_to(directory.resolve()) and digest(path)==document['sha256'],
            'typed evidence hash mismatch')
    return document
