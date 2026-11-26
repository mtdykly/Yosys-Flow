"""Bound missing old-region evidence; never silently launch a whole-design proof."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_active=ContextVar('old_region_evidence_budget',default=None)


class EvidenceBlocked(Exception):
    def __init__(self,code,detail):self.code,self.detail=code,detail;super().__init__(str(detail))


def current():return _active.get()


def bounded_walk(context,roots):
    from .graph_index import walk,EdgeList
    policy=context.evidence_budget
    edges=context.trace.edges
    if not hasattr(edges,'adjacent'):edges=EdgeList(edges)
    class Edges:
        count=0
        def __len__(self):return len(edges)
        def adjacent(self,node,reverse=False):
            for item in edges.adjacent(node,reverse):
                self.count+=1
                if self.count>16*policy.node_limit:
                    policy.block('old_region_provenance_traversal_limit',visited_edges=self.count,
                        missing_relations=getattr(getattr(context,'proof_evidence',None),'metrics',{}).get('misses',[])[-3:])
                yield item
    # Keep the existing chronological traversal; the wrapper only limits work.
    return walk(Edges(),roots)[0]


class Budget:
    def __init__(self,seconds=60,node_limit=4096):
        if seconds<=0 or node_limit<=0:raise ValueError('old-region budgets must be positive')
        self.seconds,self.node_limit=seconds,node_limit
        self.used=0.0;self.started=None;self.phase=None;self.stages=[]

    def block(self,code,**detail):
        raise EvidenceBlocked(code,{'missing_evidence_stage':self.phase or 'region_selection',
            'budget_seconds':self.seconds,'node_limit':self.node_limit,'proven_safe':False,**detail})

    def remaining(self):
        remaining=self.seconds-self.used-(time.perf_counter()-self.started if self.started is not None else 0)
        if remaining<=0:self.block('old_region_proof_budget_exhausted')
        return remaining

    def check_nodes(self,count,phase=None):
        if count>self.node_limit:self.block('old_region_node_limit',nodes=count,relation=phase)
        self.remaining()

    @contextmanager
    def scope(self,phase):
        if current() is self:
            yield;return
        self.remaining();self.phase=phase;self.started=time.perf_counter();token=_active.set(self)
        try:
            yield
            self.remaining()
        finally:
            elapsed=time.perf_counter()-self.started;self.used+=elapsed;self.started=None
            self.stages.append({'phase':phase,'seconds':elapsed});_active.reset(token)


@contextmanager
def scope(context,phase,document=None):
    policy=getattr(context,'evidence_budget',None)
    if policy is None:
        yield;return
    if document:policy.check_nodes(len(document['region'].get('cells',[])),'selected_final_cells')
    with policy.scope(phase):yield
