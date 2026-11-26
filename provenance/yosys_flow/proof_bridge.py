"""Reuse bounded, authenticated checkpoint engines across output requests."""
from collections import OrderedDict
from pathlib import Path

from .abc_partition import Graph
from .boolean import ProofBudgetExceeded
from .common import digest, require
from .proof_checkpoint import BoundedPartitioner


class BridgeSession:
    def __init__(self, endpoint, witnesses, store, prefix, progress, check_budget, *, cache_engines=1):
        require(cache_engines>=1,'bridge cache must retain at least one engine')
        self.endpoint,self.witnesses,self.store=endpoint,witnesses,store
        self.prefix,self.progress,self.check_budget=prefix,progress,check_budget
        self.cache_engines=cache_engines;self.cache=OrderedDict();self.saved={}
        self.descriptors=[*witnesses.stages,{'endpoint':'captured_output'}]
        self.stats={'engine_builds':0,'engine_restores':0,'memory_hits':0,'graph_parses':0,'witness_installs':0,'bridge_requests':0}

    def engine(self,index):
        if index in self.cache:
            self.stats['memory_hits']+=1
            engine=self.cache.pop(index);self.cache[index]=engine
        else:
            while len(self.cache)>=self.cache_engines:self.cache.popitem(last=False)
            key=self.prefix+':hop:'+str(index)
            engine=self.store.load(key,self.check_budget)
            if engine is not None:self.stats['engine_restores']+=1
            else:
                def graph(i):
                    if i<0:return self.endpoint.first
                    if i==len(self.descriptors)-1:return self.endpoint.final
                    row=self.descriptors[i];path=self.witnesses.file(row['file'],row['sha256'])
                    self.stats['graph_parses']+=1;return Graph(path.read_text())
                engine=BoundedPartitioner(graph(index-1),graph(index),sat_timeout_ms=self.endpoint.sat_timeout_ms,
                    sat_backend=self.endpoint.sat_backend,cec_timeout_seconds=self.endpoint.cec_timeout_seconds,
                    cec_attempt_limit=self.endpoint.cec_attempt_limit)
                engine.check_budget=self.check_budget;self.stats['engine_builds']+=1
                history_started=any(r.get('command')=='&dch' or 'endpoint' in r for r in self.descriptors[:index+1])
                if history_started:
                    self.witnesses.install(engine,self.progress);self.stats['witness_installs']+=1
                self.store.save(key,engine)
            self.cache[index]=engine
            self.saved[index]=self.marker(engine)
        return engine,engine.final,index==len(self.descriptors)-1

    @staticmethod
    def marker(engine):return (len(engine.proofs),len(engine.algebra.unique),len(engine.cec_attempts))

    def persist(self,index,engine):
        marker=self.marker(engine)
        if marker!=self.saved.get(index):
            self.store.save(self.prefix+':hop:'+str(index),engine)
            self.saved[index]=marker

    def prove(self,requests,limit=4096):
        if not requests:return {'accepted':[]}
        self.stats['bridge_requests']+=1
        endpoint=self.endpoint;progress=self.progress
        active = {tuple(key): (key[0], 0) for key in requests}
        first = endpoint.first
        history = [];history_started=False
        for index, descriptor in enumerate(self.descriptors):
            self.check_budget()
            engine, final, is_endpoint = self.engine(index)
            following = {}; results = []; refined = False
            if progress:
                progress('checkpoint_bridge', checkpoint=descriptor, requested=len(active))
            for key, (a, accumulated) in active.items():
                sig = engine.sa[a]
                candidates = ([key[1]] if is_endpoint else
                              [n for n in engine.candidates.get(min(sig, sig ^ engine.mask), ()) if n is not None][:3])
                unique = {}
                for name in candidates:
                    unique.setdefault(engine.classes.find(engine.b[name]) // 2, name)
                candidates = list(unique.values())
                row = dict(endpoint=list(key), entry=a, status='unknown')
                for b in candidates:
                    phase = int(sig != engine.sb[b])
                    if sig not in (engine.sb[b], engine.sb[b] ^ engine.mask):
                        continue
                    known = engine.classes.find(engine.a[a]) == (engine.classes.find(engine.b[b]) ^ phase)
                    try:
                        try:
                            ok = known or engine.prove(a, b, phase, limit=limit)
                        except ProofBudgetExceeded:
                            ok = False
                        if not ok:
                            ok = engine.global_cec(a, b, phase, progress)
                    except ProofBudgetExceeded as exc:
                        ok = False; row['reason'] = str(exc)
                    if not ok and not refined:
                        # Repair nearby boundaries bottom-up once for this hop,
                        # rather than retrying the same large PI miter per root.
                        refined = True
                        budget=engine.sat_timeout_ms
                        engine.sat_timeout_ms=min(budget,1000)
                        try:
                            engine.refine(sorted({n for n, phase in active.values()}), limits=(64,), progress=progress)
                        finally:
                            engine.sat_timeout_ms=budget
                    if not ok and refined:
                        try:
                            ok = engine.prove(a, b, phase, limit=limit)
                        except ProofBudgetExceeded as exc:
                            row['reason'] = str(exc)
                    if not ok:
                        engine.targeted([], pairs=[(a,b,phase)], limit=limit, progress=progress)
                        ok=engine.classes.find(engine.a[a])==(engine.classes.find(engine.b[b])^phase)
                    if ok:
                        following[key] = (b, accumulated ^ phase)
                        row.update(status='verified', export=b, inverted=bool(phase), structural=known)
                        break
                results.append(row)
                if progress:
                    progress('checkpoint_pair', **row)
            self.persist(index,engine)
            history.append(dict(checkpoint=descriptor, pairs=results, proofs=list(engine.proofs)))
            first = final; active = following
            if not active:
                break
        require(is_endpoint or not active, 'checkpoint bridge did not reach final endpoint')
        accepted = []
        for key, (b, phase) in active.items():
            a, target, expected = key
            require(b == target and phase == expected, 'checkpoint chain polarity mismatch')
            endpoint.classes.join(endpoint.a[a], endpoint.b[b] ^ phase)
            accepted.append(list(key))
        evidence = dict(proof='captured_checkpoint_function_chain', requested=[list(k) for k in requests],
                        accepted=accepted, stages=history, complete_causal_history=False)
        endpoint.proofs.append(evidence)
        self.store.save(self.prefix,endpoint,self.store.entries[self.prefix]['metadata'])
        return evidence
