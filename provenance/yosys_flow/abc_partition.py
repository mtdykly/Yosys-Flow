"""Bounded functional partitions of authenticated ABC endpoint networks.

Simulation proposes pairs only. Every accepted pair is proved over independent
previously proved frontier functions. The proof DAG is strictly chronological;
neither a proposed pair nor the root being proved may serve as an assumption.
These are functional correspondences, not reconstructed ABC rewrite history.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import random
import shutil
import subprocess
import tempfile
from pathlib import Path
import math
import time
import hashlib
import re


def cec_verified(output):
    return bool(re.search(r'^Networks are equivalent(?:\.| after structural hashing\.)', output, re.M))

from .abc_internal import evaluate
from .boolean import BDD, ProofBudgetExceeded
from .common import require
from .region_functions import gate_function
from .structural import Expressions


class WindowLimit(ProofBudgetExceeded):
    def __init__(self, side, root, limit, order, boundary, active, pending):
        super().__init__('partition_window_node_limit')
        self.detail = dict(side=side, root=root, limit=limit, visited=len(order),
                           boundary_count=len(boundary), active_count=len(active),
                           pending_count=len(pending), pending_preview=pending[-16:])


class Graph:
    def __init__(self, text):
        self.nodes = {}; self.pis = []; self.pos = []; pending = None
        for raw in text.replace('\\\n', ' ').splitlines():
            line = raw.split('#', 1)[0].strip()
            if not line: continue
            words = line.split()
            if line.startswith('.'):
                pending = None
                if words[0] == '.inputs': self.pis.extend(words[1:])
                elif words[0] == '.outputs': self.pos.extend(words[1:])
                elif words[0] == '.names':
                    pending = dict(op='sop', sop='', fanins=words[1:-1])
                    require(words[-1] not in self.nodes, 'duplicate BLIF driver')
                    self.nodes[words[-1]] = pending
                elif words[0] == '.gate':
                    ports = dict(w.split('=') for w in words[2:]); name = ports.pop('Y')
                    require(name not in self.nodes, 'duplicate BLIF driver')
                    self.nodes[name] = dict(op=words[1], ports=list(ports), fanins=list(ports.values()))
                else: require(words[0] in {'.model', '.end'}, 'unsupported partition BLIF directive')
            else:
                require(pending is not None, 'BLIF table without node')
                pending['sop'] += line+'\n'
        require(len(set(self.pis)) == len(self.pis) and not (set(self.pis) & self.nodes.keys()),
                'invalid BLIF primary inputs')
        self.inputs=set(self.pis)
        self.order, _ = self.window(list(self.nodes), set(self.pis), limit=None)
        self.users = defaultdict(set)
        for name, node in self.nodes.items():
            for fanin in node['fanins']: self.users[fanin].add(name)

    def window(self, roots, stops, *, limit=4096, side='network'):
        order = []; boundary = set(); seen = set(); active = set()
        roots = list(roots); root_set = set(roots)
        stack = [(r, False) for r in reversed(roots)]
        while stack:
            name, done = stack.pop()
            if name in seen: continue
            if name not in root_set and name in stops:
                boundary.add(name); seen.add(name); continue
            if done:
                active.remove(name); seen.add(name); order.append(name); continue
            require(name not in active, 'cycle in partition network')
            if name not in self.nodes:
                require(name in self.inputs and name in stops, 'unbound partition input')
                boundary.add(name); seen.add(name); continue
            if limit is not None and len(order)+len(active) >= limit:
                raise WindowLimit(side, roots, limit, order, boundary, active, [n for n,d in stack if not d])
            active.add(name); stack.append((name, True))
            stack.extend((f, False) for f in reversed(self.nodes[name]['fanins']))
        return order, boundary

    def values(self, order, bindings, one):
        values = dict(bindings)
        for name in order:
            node = self.nodes[name]; args = [values[n] for n in node['fanins']]
            values[name] = (evaluate(node, args, one) if node['op'] == 'sop' else
                            gate_function('$_'+node['op']+'_', dict(zip(node['ports'], args)), one))
        return values

    def blif(self, root, inverted=False):
        """Cone at the captured PIs for independent fallback CEC; no cut assumptions."""
        roots=[root] if isinstance(root,str) else list(root)
        order,_=self.window(roots,self.inputs,limit=None)
        outputs=['__partition_output'+str(i) for i in range(len(roots))]
        lines=['.model partition','.inputs '+' '.join(sorted(self.pis)),'.outputs '+' '.join(outputs)]
        for name in order:
            node=self.nodes[name]
            lines.append('.names '+' '.join([*node['fanins'],name]))
            if node['op']=='sop':lines.extend(node['sop'].strip().splitlines());continue
            require(len(node['fanins'])<=6,'mapped gate is too wide for partition CEC encoding')
            for bits in range(1<<len(node['fanins'])):
                args=[(bits>>i)&1 for i in range(len(node['fanins']))]
                if gate_function('$_'+node['op']+'_',dict(zip(node['ports'],args)),1):
                    lines.append(''.join(map(str,args))+' 1')
        for root,out in zip(roots,outputs):lines.extend(['.names '+root+' '+out,('0' if inverted else '1')+' 1'])
        lines.append('.end')
        return '\n'.join(lines)+'\n',len(order)


class Classes:
    """Parity union-find of proved structural functions; unions need a proof."""
    def __init__(self, first, final):
        self.parents = {}; self.sides = defaultdict(int)
        for side, values in ((1, first), (2, final)):
            for literal in values.values(): self.sides[literal//2] |= side

    def find(self, literal):
        path = []; current = literal
        while current//2 in self.parents:
            path.append(current)
            current = self.parents[current//2] ^ (current&1)
        for old in path: self.parents[old//2] = current ^ (old&1)
        return current

    def find_no_compress(self, literal):
        while literal//2 in self.parents: literal = self.parents[literal//2] ^ (literal&1)
        return literal

    def join(self, a, b):
        a, b = self.find(a), self.find(b)
        if a//2 == b//2:
            require(a == b, 'contradictory partition proof'); return
        # Deterministic union; no recursive expression substitution.
        if a//2 < b//2: a, b = b, a
        self.parents[a//2] = b ^ (a&1)
        self.sides[b//2] |= self.sides[a//2]


class Partitioner:
    def __init__(self, first, final, *, sample_bits=256, sat_timeout_ms=1000, sat_backend='abc',
                 cec_timeout_seconds=60, cec_attempt_limit=32):
        require(set(first.pis) == set(final.pis), 'partition captured input interface mismatch')
        require(set(first.pos) == set(final.pos), 'partition captured output interface mismatch')
        self.first, self.final = first, final
        require(1<=sat_timeout_ms<=3600000,'invalid local SAT budget')
        self.sat_timeout_ms=sat_timeout_ms
        require(sat_backend in {'abc','abc_cec','z3'},'unsupported partition SAT backend')
        self.sat_backend=sat_backend
        self.cec_timeout_seconds=cec_timeout_seconds;self.cec_attempt_limit=cec_attempt_limit
        require(0<cec_timeout_seconds<=3600 and 0<=cec_attempt_limit<=10000,'invalid CEC budget')
        self.cec_attempts={}
        algebra = self.algebra = Expressions(); bindings = {n:algebra.variable(n) for n in first.pis}
        self.a = {n:v.literal for n,v in first.values(first.order, bindings, algebra.one).items()}
        self.b = {n:v.literal for n,v in final.values(final.order, bindings, algebra.one).items()}
        # A virtual literal is a proof target only. It is never exported as a
        # captured object, selected gate, or electrical boundary.
        final.nodes[None]=dict(op='sop',sop='',fanins=[])
        self.b[None]=0
        self.classes = Classes(self.a, self.b)
        rng = random.Random(73129); self.mask = (1 << sample_bits)-1
        samples = {n:rng.getrandbits(sample_bits) for n in sorted(first.pis)}
        self.sa = first.values(first.order, samples, self.mask)
        self.sb = final.values(final.order, samples, self.mask)
        self.sb[None]=0
        self.proofs = []; self.attempts = Counter(); self.diagnostics = []
        self.candidates = defaultdict(list)
        for n in final.order:
            v = self.sb[n]
            self.candidates[min(v, v^self.mask)].append(n)
        self.candidates[0].insert(0,None)

    def window(self, side, roots, excluded=(), limit=4096, allowed=None):
        graph, vals = (self.first,self.a) if side=='entry' else (self.final,self.b)
        classes = self.classes
        excluded = {classes.find(l)//2 for l in excluded}
        class Frontier:
            def __contains__(_, name):
                ident = classes.find(vals[name])//2
                return name in graph.inputs or (ident not in excluded and classes.sides[ident] == 3
                                             and (allowed is None or name in allowed))
        return graph.window(roots, Frontier(), limit=limit, side=side)

    def aligned(self, a, b, excluded, limit, allowed_a=None, allowed_b=None):
        cls = self.classes
        pi_classes = {cls.find(self.a[n])//2 for n in self.first.pis}
        for _ in range(64):
            ao, ab = self.window('entry', a, excluded, limit, allowed_a)
            bo, bb = self.window('export', b, excluded, limit, allowed_b)
            ac = {cls.find(self.a[n])//2 for n in ab}
            bc = {cls.find(self.b[n])//2 for n in bb}
            unmatched = (ac ^ bc) - pi_classes
            if not unmatched: return ao,ab,bo,bb
            excluded.update(2*i for i in unmatched)
        raise ProofBudgetExceeded('partition_frontier_alignment_limit')

    def independent(self, ao, ab, bo, bb, manager=None):
        cls = self.classes
        identities = sorted({cls.find(self.a[n])//2 for n in ab} |
                            {cls.find(self.b[n])//2 for n in bb})
        identities=[i for i in identities if i!=0]
        if manager is None:
            if len(identities)>64: raise ProofBudgetExceeded('partition_input_limit')
            manager = BDD(node_limit=10000, work_limit=100000)
        variables = dict(zip(identities, [manager.variable(i) for i in identities] if isinstance(manager,Expressions)
                              else manager.variables(len(identities))))
        variables[0]=manager.one^1
        def bindings(names, values):
            return {n:variables[cls.find(values[n])//2] ^ (cls.find(values[n])&1) for n in names}
        return (self.first.values(ao,bindings(ab,self.a),manager.one),
                self.final.values(bo,bindings(bb,self.b),manager.one),len(identities))

    def equivalent(self, ao, ab, bo, bb, pairs):
        try:
            av,bv,count=self.independent(ao,ab,bo,bb)
            return all(av[a]==(bv[b]^phase) for a,b,phase in pairs),count,'bdd'
        except ProofBudgetExceeded:
            # BDD exhaustion says nothing about the function. Prove the same
            # independently quantified cut using bounded SAT, without changing
            # inputs, polarity, outputs, or any assumptions.
            binary=shutil.which('z3' if self.sat_backend=='z3' else 'yosys-abc')
            if not binary:raise ProofBudgetExceeded('partition_sat_unavailable')
            manager=Expressions();av,bv,count=self.independent(ao,ab,bo,bb,manager)
            mismatch=manager.one^1
            for a,b,phase in pairs:mismatch=mismatch | (av[a]^bv[b]^phase)
            if mismatch.literal==0:return True,count,'structural'
            if mismatch.literal==1:return False,count,'structural'
            if self.sat_backend in {'abc','abc_cec'}:
                # Optimize only the local Boolean miter. There is no design
                # synthesis, library mapping, or assumed boundary correlation.
                pis=['n'+str(lit//2) for key,lit in manager.unique.items() if key[0]=='input']
                lines=['.model local_miter','.inputs '+' '.join(pis),'.outputs mismatch',
                       '.names constant_zero','.names constant_one','1']
                def wire(lit):return ('constant_one' if lit else 'constant_zero') if lit<2 else 'n'+str(lit//2)
                for key,lit in manager.unique.items():
                    if key[0]=='input':continue
                    x,y=key[1:]
                    lines.extend(['.names '+wire(x)+' '+wire(y)+' '+wire(lit),
                                  str(1-(x&1) if x>=2 else 1)+str(1-(y&1) if y>=2 else 1)+' 1'])
                lines.extend(['.names '+wire(mismatch.literal)+' mismatch',
                              ('0' if mismatch.literal&1 else '1')+' 1','.end'])
                with tempfile.TemporaryDirectory(prefix='abc-partition-') as tmp:
                    Path(tmp,'miter.blif').write_text('\n'.join(lines)+'\n')
                    Path(tmp,'zero.blif').write_text('.model zero\n.inputs '+' '.join(pis)+'\n.outputs mismatch\n.names mismatch\n.end\n')
                    script=('cec -T '+str(math.ceil(self.sat_timeout_ms/1000))+' miter.blif zero.blif'
                            if self.sat_backend=='abc_cec' else 'read_blif miter.blif; strash; dc2; sat')
                    try:
                        result=subprocess.run([binary,'-c',script],cwd=tmp,
                            text=True,capture_output=True,timeout=self.sat_timeout_ms/1000)
                    except subprocess.TimeoutExpired:raise ProofBudgetExceeded('partition_sat_process_timeout')
                require(result.returncode==0,'partition ABC SAT tool failed')
                if self.sat_backend=='abc_cec':
                    if cec_verified(result.stdout):return True,count,'abc_local_cec'
                    if 'Networks are NOT EQUIVALENT' in result.stdout:return False,count,'abc_local_cec'
                    raise ProofBudgetExceeded('partition_local_cec_unknown')
                words=result.stdout.split()
                if 'UNSATISFIABLE' in words:return True,count,'abc_sat'
                if 'SATISFIABLE' in words:return False,count,'abc_sat'
                raise ProofBudgetExceeded('partition_sat_unknown')
            def term(lit):
                if lit<2:return 'true' if lit else 'false'
                return '(not n'+str(lit//2)+')' if lit&1 else 'n'+str(lit//2)
            lines=['(set-option :timeout '+str(self.sat_timeout_ms)+')','(set-logic QF_UF)']
            for key,lit in manager.unique.items():
                name='n'+str(lit//2)
                lines.append('(declare-fun '+name+' () Bool)' if key[0]=='input' else
                             '(define-fun '+name+' () Bool (and '+term(key[1])+' '+term(key[2])+'))')
            lines.extend(['(assert '+term(mismatch.literal)+')','(check-sat)'])
            try:
                result=subprocess.run([binary,'-in','-smt2'],input='\n'.join(lines),text=True,
                                      capture_output=True,timeout=self.sat_timeout_ms/1000+5)
            except subprocess.TimeoutExpired:raise ProofBudgetExceeded('partition_sat_process_timeout')
            require(result.returncode==0 and result.stdout.strip() in {'sat','unsat','unknown'},
                    'partition SAT solver protocol failure')
            if result.stdout.strip()=='unknown':raise ProofBudgetExceeded('partition_sat_unknown')
            return result.stdout.strip()=='unsat',count,'sat'

    def prove(self, a, b, polarity, *, limit=256):
        cls = self.classes
        aa, bb = cls.find(self.a[a]), cls.find(self.b[b]) ^ polarity
        if aa == bb: return True
        # A root can already be a certified boundary through another object.
        # Cutting it is sound: only verified classes are shared here, and two
        # distinct roots remain independent variables. Excluding both roots
        # would unnecessarily unfold absorption/copy-to-existing-signal cases.
        excluded = set()
        for _ in range(8):
            ao,ab,bo,bbnd = self.aligned([a],[b],excluded,limit)
            equal,count,backend = self.equivalent(ao,ab,bo,bbnd,[(a,b,polarity)])
            if equal: break
            expand = {cls.find(self.a[n]) for n in ab if n not in self.first.inputs}
            expand.update(cls.find(self.b[n]) for n in bbnd if n not in self.final.inputs)
            if not expand: return False
            excluded.update(expand)
        else: raise ProofBudgetExceeded('partition_function_expansion_limit')
        self.proofs.append(dict(entry_root=a, export_root=b, inverted=bool(polarity),
            proof='independent_frontier_function', earlier_proofs=len(self.proofs),
            entry_nodes=len(ao), export_nodes=len(bo), inputs=count, node_limit=limit, backend=backend))
        cls.join(self.a[a], self.b[b]^polarity)
        return True

    def refine(self, outputs, *, limits=(64,256,4096), progress=None, checkpoint=None):
        require(set(outputs)<=self.first.nodes.keys()|self.first.inputs, 'unknown captured function target')
        needed, _ = self.first.window(outputs, set(self.first.pis), limit=None)
        # Only source dependencies of requested captured outputs are attempted.
        # Signature calculation is shared linear work, not a full proof pass.
        for limit in limits:
            start = len(self.proofs); tried = set()
            for ordinal, a in enumerate(needed):
                sig = self.sa[a]; candidates = self.candidates.get(min(sig,sig^self.mask), ())
                for b in candidates[:32]:
                    phase = int(sig != self.sb[b])
                    if self.classes.find(self.a[a]) == (self.classes.find(self.b[b])^phase): break
                    key = (self.classes.find(self.a[a]),self.classes.find(self.b[b]),phase)
                    if key in tried: continue
                    tried.add(key)
                    try:
                        matched = self.prove(a,b,phase,limit=limit)
                        self.attempts['verified' if matched else 'independent_functions_differ'] += 1
                        if matched: break
                    except ProofBudgetExceeded as exc:
                        self.attempts[str(exc)] += 1
                if progress and ordinal % 5000 == 0:
                    progress('partition_refinement', limit=limit, visited=ordinal, total=len(needed),
                             proved_pairs=len(self.proofs), attempts=dict(self.attempts))
            if progress: progress('partition_round_complete', limit=limit, added=len(self.proofs)-start)
            if checkpoint: checkpoint(self.proofs)
            if all(n in self.b and self.classes.find(self.a[n]) == self.classes.find(self.b[n]) for n in outputs): break

    def replay(self, proofs):
        """An untrusted saved pair is re-proved, never installed as an assumption."""
        for row in proofs:
            if row['proof']=='captured_outputs_cec':
                require(row['outputs']==sorted(self.first.pos),'cached output CEC interface differs')
                require(self.outputs_cec(timeout=row['timeout_seconds']),'saved output interface is not equivalent')
                continue
            if row['proof']=='captured_pi_cec':
                require(self.global_cec(row['entry_root'],row['export_root'],int(row['inverted'])),
                        'saved global boundary pair is not equivalent')
                continue
            require(row['proof'] in {'independent_frontier_bdd','independent_frontier_function'},'unknown partition proof rule')
            require(self.prove(row['entry_root'],row['export_root'],int(row['inverted']),
                               limit=row.get('node_limit',4096)), 'saved partition pair is not equivalent')

    def global_cec(self, a, b, phase, progress=None):
        cls=self.classes;aa,bb=cls.find(self.a[a]),cls.find(self.b[b])^phase
        if aa==bb:return True
        key=(aa//2,bb//2,(aa^bb)&1)
        if key in self.cec_attempts:return False
        if len(self.cec_attempts)>=self.cec_attempt_limit:raise ProofBudgetExceeded('partition_cec_attempt_limit')
        self.cec_attempts[key]={'status':'unknown'}
        binary=shutil.which('yosys-abc')
        if not binary:raise ProofBudgetExceeded('partition_cec_unavailable')
        old,ac=self.first.blif(a);new,bc=self.final.blif(b,bool(phase))
        if progress:progress('captured_pi_cec',entry=a,export=b,entry_nodes=ac,export_nodes=bc,
                             budget_seconds=self.cec_timeout_seconds)
        start=time.perf_counter()
        with tempfile.TemporaryDirectory(prefix='abc-boundary-cec-') as tmp:
            Path(tmp,'old.blif').write_text(old);Path(tmp,'new.blif').write_text(new)
            try:
                result=subprocess.run([binary,'-c','cec -T '+str(math.ceil(self.cec_timeout_seconds))+' old.blif new.blif'],
                    cwd=tmp,text=True,capture_output=True,timeout=self.cec_timeout_seconds+5)
            except subprocess.TimeoutExpired:raise ProofBudgetExceeded('partition_cec_timeout')
        require(result.returncode==0,'partition CEC tool failed')
        record=dict(entry_root=a,export_root=b,inverted=bool(phase),proof='captured_pi_cec',
            input_domain='all_captured_pis_independent',entry_nodes=ac,export_nodes=bc,
            old_blif_sha256=hashlib.sha256(old.encode()).hexdigest(),new_blif_sha256=hashlib.sha256(new.encode()).hexdigest(),
            elapsed_seconds=time.perf_counter()-start,log=result.stdout,
            timeout_seconds=self.cec_timeout_seconds,
            tool={'path':str(Path(binary).resolve()),'sha256':hashlib.sha256(Path(binary).read_bytes()).hexdigest()})
        if cec_verified(result.stdout):
            self.cec_attempts[key]={'status':'verified',**record}
            self.proofs.append(record);cls.join(self.a[a],self.b[b]^phase)
            return True
        self.cec_attempts[key]={'status':'different' if 'NOT EQUIVALENT' in result.stdout else 'unknown',**record}
        return False

    def outputs_cec(self, *, timeout=360, progress=None):
        """Prove reusable captured PO anchors together, sharing the CEC network."""
        outputs=sorted(self.first.pos)
        binary=shutil.which('yosys-abc')
        if not binary:raise ProofBudgetExceeded('partition_cec_unavailable')
        old,ac=self.first.blif(outputs);new,bc=self.final.blif(outputs)
        if progress:progress('captured_output_anchors_cec',outputs=len(outputs),entry_nodes=ac,export_nodes=bc)
        start=time.perf_counter()
        with tempfile.TemporaryDirectory(prefix='abc-output-cec-') as tmp:
            Path(tmp,'old.blif').write_text(old);Path(tmp,'new.blif').write_text(new)
            try:
                result=subprocess.run([binary,'-c','cec -T '+str(math.ceil(timeout))+' old.blif new.blif'],cwd=tmp,
                                      text=True,capture_output=True,timeout=timeout+5)
            except subprocess.TimeoutExpired:raise ProofBudgetExceeded('partition_output_cec_timeout')
        require(result.returncode==0,'captured output CEC tool failed')
        if not cec_verified(result.stdout):return False
        self.proofs.append(dict(proof='captured_outputs_cec',outputs=outputs,timeout_seconds=timeout,
            input_domain='all_captured_pis_independent',entry_nodes=ac,export_nodes=bc,
            old_blif_sha256=hashlib.sha256(old.encode()).hexdigest(),new_blif_sha256=hashlib.sha256(new.encode()).hexdigest(),
            elapsed_seconds=time.perf_counter()-start,log=result.stdout,
            tool={'path':str(Path(binary).resolve()),'sha256':hashlib.sha256(Path(binary).read_bytes()).hexdigest()}))
        for name in outputs:self.classes.join(self.a[name],self.b[name])
        return True

    def targeted(self, outputs, *, limit=4096, progress=None, frontier_prover=None, pairs=None):
        """Resolve a DAG of conditional lemmas; assumptions alone never publish.

        Sampled pairs merely propose cut points. Each conditional lemma
        is proved under independent variables, then every used pair must be
        proved first. Cyclic dependencies or failed children remain unknown.
        The final acceptance repeats the proof with VERIFIED classes only.
        """
        verified=self.classes; speculative=deepcopy(verified)
        left=defaultdict(dict);right=defaultdict(dict)
        for vals,samples,groups in ((self.a,self.sa,left),(self.b,self.sb,right)):
            for n,lit in vals.items():
                v=samples[n]
                if v in (0,self.mask):continue
                groups[min(v,v^self.mask)][verified.find(lit)//2]=n
        for sig,group in left.items():
            other=right.get(sig,{})
            if group and other and len(group)<=32 and len(other)<=32:
                # Several different nodes may implement the same function.
                # Every used entry/export pair becomes an explicit dependency
                # below; equality is not granted by joining this trial class.
                a=next(iter(group.values()));b=next(iter(other.values()))
                for n in group.values():
                    speculative.join(self.a[n],self.b[b]^int(self.sa[n]!=self.sb[b]))
                for n in other.values():
                    speculative.join(self.a[a],self.b[n]^int(self.sa[a]!=self.sb[n]))
        failed={};active=set();completed=set();pending={};details={}
        def known(key):
            a,b,p=key
            return verified.find(self.a[a])==(verified.find(self.b[b])^p)
        def conditional(key):
            a,b,phase=key;self.classes=speculative
            try:
                excluded={self.a[a],self.b[b]}
                for _ in range(8):
                    ao,ab,bo,bb=self.aligned([a],[b],excluded,limit)
                    equal,_,_=self.equivalent(ao,ab,bo,bb,[(a,b,phase)])
                    if equal:break
                    excluded.update(speculative.find(self.a[n]) for n in ab if n not in self.first.inputs)
                    excluded.update(speculative.find(self.b[n]) for n in bb if n not in self.final.inputs)
                else:raise ProofBudgetExceeded('conditional_function_not_proven')
                by_class=defaultdict(list)
                for n in bb:by_class[speculative.find(self.b[n])//2].append(n)
                deps=set()
                for n in ab:
                    matches=by_class[speculative.find(self.a[n])//2]
                    for m in matches:
                        p=(speculative.find(self.a[n])^speculative.find(self.b[m]))&1
                        child=(n,m,p)
                        if not known(child):deps.add(child)
                return sorted(deps)
            finally:self.classes=verified
        targets=list(pairs) if pairs is not None else [(n,n,0) for n in outputs]
        for root_key in targets:
            stack=[(root_key,False)]
            while stack:
                key,done=stack.pop()
                if known(key) or key in completed or key in failed:continue
                if done:
                    active.discard(key)
                    if any(child in failed for child in pending[key]):
                        failed[key]='unproved_dependency';continue
                    try:
                        require(all(known(child) for child in pending[key]),'conditional proof has an unresolved dependency')
                        if self.prove(*key,limit=limit):completed.add(key)
                        else:failed[key]='independent_functions_differ'
                    except ProofBudgetExceeded as exc:failed[key]=str(exc)
                    continue
                if key in active:
                    failed[key]='cyclic_conditional_dependency';continue
                active.add(key)
                try:deps=conditional(key)
                except ProofBudgetExceeded as exc:
                    try:
                        if self.global_cec(*key,progress=progress):
                            active.remove(key);completed.add(key);continue
                    except ProofBudgetExceeded:pass
                    if isinstance(exc,WindowLimit):details[key]=exc.detail
                    if progress and len(details)<=12:
                        progress('partition_dependency_blocked',entry=key[0],export=key[1],reason=str(exc),
                                 **({'window':exc.detail} if isinstance(exc,WindowLimit) else {}))
                    active.remove(key);failed[key]=str(exc);continue
                if key==root_key:
                    # Prefer a few boundaries nearest the requested output to
                    # proving thousands of their internal ancestors. CEC uses
                    # the captured PIs, so this creates no unproved assumption.
                    if progress:progress('target_frontier',output=key[0],pairs=len(deps),preview=deps[:8])
                    if frontier_prover and deps:
                        frontier_prover([child for child in deps if not known(child)])
                    for child in deps[:8]:
                        if known(child):continue
                        try:self.global_cec(*child,progress=progress)
                        except ProofBudgetExceeded:pass
                pending[key]=deps
                stack.append((key,True));stack.extend((child,False) for child in reversed(deps))
                if progress and len(pending)%100==0:
                    progress('targeted_dependencies',lemmas=len(pending),proved=len(completed),failed=len(failed))
        return {'conditional_lemmas':len(pending),'proved':len(completed),'failed':len(failed),
                'global_cec_attempts':list(self.cec_attempts.values()),
                'failures':[dict(entry=a,export=b,inverted=bool(p),reason=r,
                                 **({'window':details[a,b,p]} if (a,b,p) in details else {}))
                            for (a,b,p),r in failed.items()],
                'outputs':{a:known((a,b,p)) for a,b,p in targets}}

    def region(self, seeds, *, source_nodes=None, limit=4096):
        """Close the forward edit impact, then prove EVERY external cut output."""
        require(seeds and set(seeds)<=self.first.nodes.keys(), 'invalid partition source seeds')
        affected=set();stack=list(seeds)
        while stack:
            n=stack.pop()
            if n in affected: continue
            affected.add(n);stack.extend(self.first.users[n])
        outputs=sorted(affected & set(self.first.pos))
        require(outputs, 'source target has no captured observable output')
        cls=self.classes
        allowed_a=set(self.a) if source_nodes is None else set(source_nodes)|set(self.first.pis)
        signed={cls.find(self.a[n])//2 for n in allowed_a}
        allowed_b={n for n,v in self.b.items() if cls.find(v)//2 in signed}
        export_rank={n:i for i,n in enumerate(self.final.pis+self.final.order)}
        excluded={cls.find(self.a[n]) for n in affected}
        excluded.update(cls.find(self.b[n]) for n in outputs)
        for _ in range(32):
            ao,ab,bo,bb=self.aligned(outputs,outputs,excluded,limit,allowed_a,allowed_b)
            # Electrical independence is stricter than functional equivalence:
            # two separate drivers of equal/complement functions are NOT one input.
            groups=defaultdict(list)
            for n in bb: groups[cls.find(self.b[n])//2].append(n)
            duplicates=[ns for ns in groups.values() if len(ns)>1]
            if duplicates:
                # Keep the earliest actual driver and absorb the downstream
                # duplicate/inverter. Excluding its entire functional class
                # would unfold the upstream cone on BOTH sides unnecessarily.
                for ns in duplicates:
                    keep=min(ns,key=lambda n:export_rank[n])
                    allowed_b.difference_update(set(ns)-{keep})
                continue
            old_members=set(ao); new_members=set(bo)
            # Unchanged upstream helpers may be copied into the source cut.
            # Only its affected external outputs are edit obligations; every
            # selected FINAL output is checked independently below, including
            # an absorbed inverter whose users are otherwise unchanged.
            old_outputs=sorted(n for n in ao if n in affected and
                               (n in self.first.pos or self.first.users[n]-old_members))
            new_outputs=sorted(n for n in bo if n in self.final.pos or self.final.users[n]-new_members)
            old_by_class=defaultdict(list)
            for n in sorted(set(old_outputs)|set(ab)):old_by_class[cls.find(self.a[n])].append(n)
            pairs=[]
            for n in new_outputs:
                matches=old_by_class.get(cls.find(self.b[n]),[])
                if n in outputs: matches=[n]
                require(matches, 'unmatched external partition output: '+n)
                pairs.extend((a,n) for a in matches)
            require(set(old_outputs)<=set(a for a,b in pairs), 'old external output omitted from partition')
            equal,count,backend=self.equivalent(ao,ab,bo,bb,[(a,b,0) for a,b in pairs])
            if equal:
                require(set(seeds)<=old_members,'partition omitted a changed source target')
                boundary=[]
                for n in sorted(bb):
                    matches=[a for a in sorted(ab) if cls.find(self.a[a])==cls.find(self.b[n])]
                    inverted=False
                    if not matches:
                        matches=[a for a in sorted(ab) if cls.find(self.a[a])==(cls.find(self.b[n])^1)]
                        inverted=True
                    require(matches, 'partition has a missing source boundary')
                    boundary.append(dict(export_node=n,entry_nodes=matches,inverted=inverted))
                return dict(status='verified',rule='all_external_outputs_independent_partition_v1',
                    complete_causal_history=False,inputs_are_independent=True,
                    seeds=sorted(seeds),captured_outputs=outputs,entry_nodes=ao,export_nodes=bo,
                    boundary=boundary,outputs=[dict(entry=a,export=b) for a,b in pairs],
                    all_external_outputs=True,independent_input_count=count,backend=backend)
            excluded.update(cls.find(self.a[n]) for n in ab if n not in self.first.inputs)
            excluded.update(cls.find(self.b[n]) for n in bb if n not in self.final.inputs)
        raise ProofBudgetExceeded('partition_region_expansion_limit')

    def report_window(self, output, *, limit=4096):
        result = {'output':output, 'sides':{}}
        excluded = (self.a[output],self.b[output])
        for side in ('entry','export'):
            try:
                order,boundary = self.window(side,[output],excluded,limit)
                result['sides'][side] = dict(status='bounded',nodes=len(order),boundary_count=len(boundary),
                                             boundary=sorted(boundary),members=order)
            except WindowLimit as exc: result['sides'][side] = dict(status='unknown',**exc.detail)
        return result
