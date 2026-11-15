"""Publish proved endpoint partitions beside an unchanged authenticated capture."""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import uuid

from .common import digest, require, unescape, write_json, TraceError
from .contracts import fingerprint
from .native import resolve_bit
from . import persistent_index as pi
from .sqlite_index import NAME, SCHEMA, authenticate


def bind(context, region, proof):
    trace=context.trace;module=context.module
    require(len(trace.instance_catalog)==1,'partition publication currently requires a single combinational module')
    require(proof['status']=='verified' and proof['all_external_outputs'],'cannot publish an unproved cut')
    imports=trace.abc_imports[region['invocation_id']]
    wires={};gate_by_wire={}
    for name,row in imports.items():
        for port,bits in row['ports'].items():
            require(len(bits)==1,'partition import must be one bit')
            original=bits[0]
            if isinstance(original,str):continue
            wire=unescape(original['wire']);target=row['target_ports'][port][0]
            try:bit=resolve_bit(target,module)
            except TraceError:
                require(wire.startswith('ys__n') and int(wire[5:]) in set(region['output_ports'])|set(region['input_ports']),
                        'exported internal signal disappeared after ABC: '+wire)
                bit=resolve_bit(region['signals'][int(wire[5:])],module)
            require(wire not in wires or wires[wire]==bit,'inconsistent exported signal binding')
            wires[wire]=bit
        y=unescape(row['ports']['\\Y'][0]['wire']);gate_by_wire[y]=(name,row)
    for i in region['input_ports']:
        wires['ys__n'+str(i)]=resolve_bit(region['signals'][i],module)
    by_driver={}
    for name,cell in module['cells'].items():
        for port,direction in cell['port_directions'].items():
            if direction=='output':
                for bit in cell['connections'][port]:
                    if isinstance(bit,int):
                        require(bit not in by_driver,'multiple final drivers');by_driver[bit]=(name,cell)
    refs_by_name={unescape(trace.objects[r]['state']['name']):r for r in trace.final}
    refs=[]
    for wire in proof['export_nodes']:
        if wire not in gate_by_wire:continue
        name,imp=gate_by_wire[wire]
        actual_name,actual=by_driver[wires[wire]]
        require(actual['type']=='$_'+unescape(imp['type'])+'_','final gate changed after ABC')
        for port,bits in imp['ports'].items():
            value=bits[0];expected=value if isinstance(value,str) else wires[unescape(value['wire'])]
            require(actual['connections'][unescape(port)]==[expected],'final gate binding changed after ABC')
        refs.append(refs_by_name[actual_name])
    require(refs,'partition contains no live mapped gates')
    anchors=[]
    for target,originals,phase in [(r['export_node'],r['entry_nodes'],r['inverted']) for r in proof['boundary']]+[
            (r['export'],[r['entry']],False) for r in proof['outputs']]:
        for original in originals:
            require(original.startswith('ys__n'),'unbound captured entry node')
            bit=region['signals'][int(original[5:])]
            require(isinstance(bit,dict) and bit['wire'].startswith('\\'),'partition source signal has no public anchor')
            anchors.append(dict(instance=context.baseline['module'],wire=bit['wire'],offset=bit['offset'],
                                final_bit=wires[target],inverted=phase,entry_node=original,export_node=target))
    entry_refs={region['input_nodes'][int(n[5:])]['cell'] for n in proof['entry_nodes'] if n.startswith('ys__n')}
    entry_refs.discard(None);entry_refs.discard('')
    seed_refs={region['input_nodes'][int(n[5:])]['cell'] for n in proof['seeds']}
    # Find actual frontend creations, then ensure ALL their transformed ABC
    # descendants are covered. A shared operand with other descendants is not
    # allowed to inherit this narrow edit-impact certificate.
    roots=set();seen=set();stack=list(seed_refs)
    while stack:
        ref=stack.pop()
        if ref in seen:continue
        seen.add(ref)
        for _,edge in trace.edges.adjacent(ref,reverse=True):
            if edge.get('capture')=='ast_generation_scope':roots.add(ref)
            elif edge['from'] in trace.objects:stack.append(edge['from'])
    abc_inputs={r for invocation in trace.regions for r in invocation['inputs']}
    accepted=[]
    for root in sorted(roots):
        reached=set();seen=set();stack=[root];outside=False
        while stack:
            ref=stack.pop()
            if ref in seen:continue
            seen.add(ref)
            if ref in abc_inputs:reached.add(ref);continue
            if ref in trace.final:outside=True;break
            stack.extend(e['to'] for _,e in trace.edges.adjacent(ref))
        if reached and reached<=entry_refs and not outside:accepted.append(root)
    require(accepted,'partition has no closed frontend operation binding')
    result=dict(invocation_id=region['invocation_id'],proof=proof,final_cell_refs=sorted(refs),
                entry_cell_refs=sorted(entry_refs),operation_roots=accepted,anchors=anchors)
    result['id']='partition:'+fingerprint(result)
    return result


def publish(context, out, certificates, evidence, *, progress=None, supplementary_artifacts=None):
    """Copy the capture; supplement the SQLite index and sign only after checks.

    The original data tables remain byte-for-byte semantically unchanged.
    Additional tables carry independent certificates; no candidate is promoted.
    """
    require(certificates,'no verified regions to publish')
    for certificate in certificates:
        require(certificate['proof']['status']=='verified' and certificate['proof']['all_external_outputs'],
                'partition publication requires every external output')
        source_proof=certificate.get('source_proof',{})
        require(source_proof.get('status')=='verified' and source_proof.get('all_outputs') and
                source_proof.get('frontend_sha256')==context.baseline['artifacts']['frontend.il']['sha256'] and
                certificate.get('source_cut_sha256'), 'partition publication requires a bound source proof')
    require(not context.manifest.get('functional_partitions'),'extend from the original capture; nested partition publications are not supported')
    require(context.manifest['verified_index']['artifact']==NAME,'partition publication needs a SQLite baseline')
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    source=context.directory;original_hash=digest(source/'manifest.json')
    require(original_hash==context.baseline['manifest_sha256'],'baseline changed before publication')
    for name,meta in context.manifest['artifacts'].items():
        require(digest(source/name)==meta['sha256'],'baseline artifact changed before publication: '+name)
        if progress:progress('copy_artifact',artifact=name)
        shutil.copy2(source/name,out/name)
        require(digest(out/name)==meta['sha256'],'archive copy differs: '+name)
    for name,(path,sha) in (supplementary_artifacts or {}).items():
        require(Path(name).name==name and not (out/name).exists() and name not in {'manifest.json','functional-partitions.json'},
                'invalid supplementary proof artifact name')
        require(digest(path)==sha,'supplementary proof changed before publication')
        shutil.copy2(path,out/name)
        require(digest(out/name)==sha,'supplementary proof copy differs')
    artifact='functional-partitions.json'
    document={'schema_version':1,'parent_manifest_sha256':original_hash,
              'certificates':certificates,'evidence':evidence}
    write_json(out/artifact,document)
    with sqlite3.connect(out/NAME) as db:
        db.execute('CREATE TABLE functional_partitions(id TEXT PRIMARY KEY,payload TEXT NOT NULL) WITHOUT ROWID')
        db.executemany('INSERT INTO functional_partitions VALUES (?,?)',
                       [(c['id'],json.dumps(c,sort_keys=True,separators=(',',':'))) for c in certificates])
        require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','derived SQLite integrity check failed')
    manifest=deepcopy(context.manifest)
    manifest['functional_partitions']={'artifact':artifact,'count':len(certificates),
        'parent_manifest_sha256':original_hash,'derivation_id':uuid.uuid4().hex,
        'rule':'supplementary_functional_partitions_v1','original_candidates_unchanged':True}
    artifacts=pi.artifact_inventory(out)
    descriptor={'artifact':NAME,'schema_version':SCHEMA,'implementation':pi.implementation_fingerprint(),
                'binding':pi._binding(manifest,artifacts),'database_sha256':digest(out/NAME)}
    secret=pi._authentication_key()
    require(secret is not None,'local archive authentication key unavailable')
    descriptor['authentication']={'algorithm':'hmac-sha256','key_id':hashlib.sha256(secret).hexdigest(),
                                  'signature':authenticate(secret,descriptor)}
    manifest['verified_index']=descriptor
    manifest['artifacts']={**artifacts,NAME:{'sha256':descriptor['database_sha256'],'size_bytes':(out/NAME).stat().st_size}}
    require(digest(source/'manifest.json')==original_hash,'baseline changed during publication')
    write_json(out/'manifest.json',manifest)
    return manifest
