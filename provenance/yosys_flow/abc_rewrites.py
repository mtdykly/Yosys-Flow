"""Replay local, independently checked rewrite lemmas from actual snapshots.

Object identity is scoped to an epoch. Changed node functions are propagated
iteratively, stopping only at proved equality; a repeated integer ID is not an
identity across managers. Successful DCH solver facts remain separately labelled.
"""
from collections import Counter, defaultdict, deque
import random

from .abc_internal import evaluate, variables
from .abc_lemmas import REASONS, congruence
from .abc_partition import Classes
from .boolean import BDD, ProofBudgetExceeded
from .common import require
from .structural import Expressions, Expression


class Replay:
    def __init__(self, input_names):
        self.names=list(input_names);self.algebra=Expressions();self.classes=Classes({},{})
        self.bindings={n:self.algebra.variable(n).literal for n in self.names}
        rng=random.Random(921);self.mask=(1<<256)-1
        self.samples={n:rng.getrandbits(256) for n in sorted(self.names)}
        self.nodes={};self.values={};self.raw_values={};self.sim={};self.arguments={};self.users=defaultdict(set)
        self.seq=None;self.epoch=None;self.pis={};self.equations=[];self.stats=Counter();self.domains={};self.unknown=[]

    def expression(self,node,args):
        values=[Expression(self.algebra,self.classes.find(v)) for v in args]
        return evaluate(node,values,self.algebra.one).literal

    @staticmethod
    def operator(node):return node['op'],node.get('sop')

    def signature(self,node,args):
        values=tuple(self.classes.find(v) for v in args)
        return tuple(sorted(values)) if node['op'] in {'and','xor'} else values

    def ensure(self,root):
        stack=[(root,False)];active=set()
        while stack:
            ident,done=stack.pop()
            if ident in self.values:continue
            node=self.nodes[ident]
            if done:
                active.remove(ident);self.compute(ident);continue
            require(ident not in active,'cycle in rewrite snapshot')
            active.add(ident);stack.append((ident,True))
            stack.extend((f//2,False) for f in reversed(node['fanins']) if f//2 not in self.values)

    def compute(self,ident):
        node=self.nodes[ident];before=self.values.get(ident);old_sample=self.sim.get(ident)
        if node['op']=='pi':
            name=self.pis[ident];value=self.bindings[name];sample=self.samples[name];args=[]
        else:
            args=[self.values[f//2]^(f&1) for f in node['fanins']]
            value=self.expression(node,args)
            sample=evaluate(node,[self.sim[f//2]^(self.mask if f&1 else 0) for f in node['fanins']],self.mask)
        raw_args=[self.raw_values[f//2]^(f&1) for f in node['fanins']]
        require(all(self.classes.find(a)==self.classes.find(b) for a,b in zip(raw_args,args)),
                'raw and normalized rewrite operands are not proved equal')
        raw_value=(value if node['op']=='pi' else evaluate(node,
            [Expression(self.algebra,a) for a in raw_args],self.algebra.one).literal)
        if self.classes.find(raw_value)!=self.classes.find(value):
            # Keep the actual expression as well as its normalized form. This
            # explicit congruence witness makes later snapshot functions usable
            # without reconstructing an omitted intermediate expression tree.
            self.equations.append(dict(rule='structural_function_congruence',
                                       left=raw_value,right=value,node=ident))
            self.classes.join(raw_value,value)
            self.stats['context_equations']+=1
        self.raw_values[ident]=raw_value
        previous=self.arguments.get(ident)
        if before is not None and previous and previous[0]==self.operator(node) and \
                self.signature(node,previous[1])==self.signature(node,args):
            self.classes.join(before,value)
        self.values[ident]=value;self.sim[ident]=sample;self.arguments[ident]=(self.operator(node),tuple(args))
        if before is not None and self.classes.find(before)==self.classes.find(value):
            require(old_sample==sample,'rewrite function cache contradicts a proved equality')
        self.stats['node_computations']+=1
        return before is None or self.classes.find(before)!=self.classes.find(value)

    def observe(self,net):
        row=net.record
        require(len(net.pis)==len(self.names),'rewrite snapshot PI arity differs')
        names=net.names if set(net.names)==set(self.names) else self.names
        require(len(names)==len(set(names)),'rewrite PI substitution is not bijective')
        new_pis=dict(zip(net.pis,names))
        sequential=(row.get('encoding')=='delta' and row.get('base_snapshot')==self.seq and
                    row.get('network_epoch')==self.epoch and new_pis==self.pis)
        if not sequential:
            self.nodes=dict(net.nodes);self.values={};self.raw_values={};self.sim={};self.arguments={};self.users=defaultdict(set)
            self.pis=new_pis
            for ident,node in self.nodes.items():
                for f in node['fanins']:self.users[f//2].add(ident)
            for ident in self.nodes:self.ensure(ident)
            self.stats['full_snapshots']+=1
        else:
            changes={n['id']:n for n in row['nodes']};deleted=set(row.get('deleted_nodes',[]))
            for ident in changes.keys()|deleted:
                old=self.nodes.get(ident)
                if old:
                    for f in old['fanins']:self.users[f//2].discard(ident)
            for ident in deleted:
                self.nodes.pop(ident);self.values.pop(ident,None);self.sim.pop(ident,None)
                self.raw_values.pop(ident,None)
                self.arguments.pop(ident,None);self.users.pop(ident,None)
            self.nodes.update(changes)
            for ident,node in changes.items():
                for f in node['fanins']:self.users[f//2].add(ident)
            # Worklist convergence is over the actual acyclic current snapshot.
            # Equal functions avoid propagating a rewrite through its whole TFO.
            queue=deque(changes);pending=set(changes)
            updates=0
            while queue:
                updates+=1
                if updates>max(1024,8*len(changes)):
                    # Reconvergent dependencies can amplify an unordered
                    # worklist. Recompute this snapshot once in topological
                    # order, including cycle detection, instead of oscillating
                    # through millions of temporary intermediate functions.
                    self.values={};self.raw_values={};self.sim={};self.arguments={}
                    for ident in self.nodes:self.ensure(ident)
                    self.stats['topological_rebuilds']+=1
                    break
                ident=queue.popleft();pending.remove(ident)
                for f in self.nodes[ident]['fanins']:self.ensure(f//2)
                if self.compute(ident):
                    for user in self.users[ident]:
                        if user not in pending:queue.append(user);pending.add(user)
            self.stats['delta_snapshots']+=1
        self.seq=row['seq'];self.epoch=row.get('network_epoch')

    def ancestors(self,root,depth):
        seen=set();stack=[(root,0)]
        while stack:
            ident,level=stack.pop()
            if ident in seen:continue
            seen.add(ident)
            if len(seen)>4096:raise ProofBudgetExceeded('rewrite_anchor_search_limit')
            if level<depth:stack.extend((f//2,level+1) for f in self.nodes[ident]['fanins'])
        return seen

    def window(self,roots,stops):
        order=[];boundary=set();seen=set();active=set();stack=[(r,False) for r in roots]
        while stack:
            ident,done=stack.pop()
            if ident in seen:continue
            node=self.nodes[ident]
            if node['op']=='pi' or (ident in stops and node['op'] not in {'one','zero'}):
                boundary.add(ident);seen.add(ident);continue
            if done:active.remove(ident);seen.add(ident);order.append(ident);continue
            require(ident not in active,'cycle in rewrite cut')
            if len(active)+len(order)>=256:raise ProofBudgetExceeded('rewrite_window_limit')
            active.add(ident);stack.append((ident,True));stack.extend((f//2,False) for f in node['fanins'])
        return order,boundary

    def local(self,a,b,phase):
        # Searching for common anchors is distinct from expanding the proof.
        # A deep rewrite can still have a tiny independent cut once its anchors
        # are found. Keep the actual proof at 256 nodes/64 inputs below.
        for depth in (2,4,8,12,16,24,32,48,64):
            try:
                common=(self.ancestors(a,depth)&self.ancestors(b,depth))-{a,b}
                for expansion in range(9):
                    order,leaves=self.window([a,b],common)
                    if len(leaves)>64:raise ProofBudgetExceeded('rewrite cut input limit')
                    ids=sorted(leaves)
                    backend='truth_table' if len(ids)<=10 else 'bdd'
                    try:
                        if len(ids)<=10:
                            if len(ids) not in self.domains:self.domains[len(ids)]=variables(len(ids))
                            values=dict(zip(ids,self.domains[len(ids)]));one=(1<<(1<<len(ids)))-1
                        else:
                            manager=BDD(node_limit=4096,work_limit=32768);values=dict(zip(ids,manager.variables(len(ids))));one=manager.one
                        for ident in order:
                            node=self.nodes[ident]
                            args=[values[f//2]^(one if f&1 else 0) for f in node['fanins']]
                            values[ident]=evaluate(node,args,one)
                        equal=values[a]==(values[b]^(one if phase else 0))
                    except ProofBudgetExceeded:
                        equal,backend=self.sat_cut(a,b,phase,order,ids)
                    if equal:
                        return dict(rule='same_snapshot_independent_cut',leaves=ids,nodes=len(order),depth=depth,expansion=expansion,backend=backend)
                    # A shared cut may contain both t and a function of t.
                    # Treating them independently is sound but too strong.
                    # Expand the boundary, rather than assuming its correlation.
                    expandable={i for i in leaves if self.nodes[i]['fanins']}
                    if not expandable:break
                    common=(common|{f//2 for i in expandable for f in self.nodes[i]['fanins']})-expandable-{a,b}
            except ProofBudgetExceeded:continue
        return None

    def sat_cut(self,a,b,phase,order,leaves):
        """Use the same independent cut and the existing bounded SAT protocol."""
        from types import SimpleNamespace
        from .abc_checkpoints import network_blif
        from .abc_partition import Graph, Partitioner
        nodes={i:self.nodes[i] for i in order}
        nodes.update((i,{'id':i,'op':'pi','fanins':[]}) for i in leaves)
        net=SimpleNamespace(nodes=nodes,pis=leaves,names=['leaf_'+str(i) for i in leaves],
                            pos=[a,b],record={'po_names':['old_root','new_root']})
        graph=Graph(network_blif(net));engine=Partitioner(graph,graph,sat_timeout_ms=1000)
        equal,_,backend=engine.equivalent(graph.order,set(graph.pis),graph.order,set(graph.pis),
                                          [('old_root','new_root',phase)])
        return equal,backend

    def event(self,row):
        a=row['old'];b=row['new_literal']//2;phase=row['new_literal']&1
        require(row['snapshot']==self.seq,'rewrite event not attached to current immutable snapshot')
        require(a in self.nodes and b in self.nodes,'rewrite event root missing')
        left=self.values[a];right=self.values[b]^phase
        self.stats[row['kind']]+=1
        if self.classes.find(left)==self.classes.find(right):self.stats['already_equal']+=1;return
        if row['kind']=='proved_equivalence':
            require(row['reason'] in REASONS,'unsupported recorded solver rule')
            require(self.sim[a]==(self.sim[b]^(self.mask if phase else 0)), 'recorded solver result contradicts snapshot')
            proof={'rule':'recorded_dch_solver_result','reason':row['reason']}
        else:
            if self.sim[a]!=(self.sim[b]^(self.mask if phase else 0)):
                self.stats['rewrite_not_globally_equal']+=1;return
            proof=self.local(a,b,phase)
            if proof is None:
                self.stats['rewrite_unknown']+=1
                if len(self.unknown)<128:
                    self.unknown.append({k:row[k] for k in ('seq','snapshot','old','new_literal')})
                return
        self.equations.append(dict(event=row['seq'],snapshot=row['snapshot'],left=left,right=right,**proof))
        self.classes.join(left,right);self.stats['accepted']+=1


def build(rows,nets,input_names,progress=None):
    replay=Replay(input_names)
    for ordinal,row in enumerate(rows):
        if row['seq'] in nets:
            net=nets[row['seq']]
            if net.record['representation']=='aig':replay.observe(net)
        elif row['kind']=='aig_replace' or (row['kind']=='proved_equivalence' and row.get('reason') in REASONS):
            replay.event(row)
        if progress and ordinal%5000==0:
            progress('replay_local_rewrite_functions',records=ordinal,total=len(rows),statistics=dict(replay.stats))
    # Context equalities explicitly bind actual expressions to proved normalized
    # functions; they are ordinary congruence, not extra semantic assumptions.
    return dict(rule='captured_local_rewrite_function_lemmas_v1',
                terms=[{'literal':lit,'term':list(key)} for key,lit in replay.algebra.unique.items()],
                equations=replay.equations,statistics=dict(replay.stats),unknown_rewrite_examples=replay.unknown)


def install(engine,document,descriptor=None):
    require(document['rule']=='captured_local_rewrite_function_lemmas_v1','unknown rewrite lemma rule')
    mapping={0:0,1:1}
    def mapped(lit):return mapping[lit//2*2]^(lit&1)
    for row in document['terms']:
        literal=row['literal'];term=row['term']
        require(literal%2==0 and literal not in mapping,'duplicate rewrite term')
        if term[0]=='input':
            require(term[1] in engine.first.inputs,'rewrite variable is not a captured input')
            value=engine.algebra.variable(term[1]).literal
        else:
            require(term[0]=='and' and len(term)==3,'unsupported rewrite term')
            value=engine.algebra.conjunction(mapped(term[1]),mapped(term[2]))
        mapping[literal]=value
    equations=[(mapped(r['left']),mapped(r['right'])) for r in document['equations']]
    require(all(r['rule'] in {'same_snapshot_independent_cut','recorded_dch_solver_result','structural_function_congruence'}
                for r in document['equations']),'unproved rewrite equation')
    result=congruence(engine.algebra,engine.classes,equations)
    engine.proofs.append(dict(proof='captured_local_rewrite_functions',witness=descriptor,
                             statistics=document['statistics'],congruence_unions=result))
    return result
