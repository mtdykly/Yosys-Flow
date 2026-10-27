"""All-output, independent-boundary functions for recorded combinational regions.

The reference is the replayed FINAL object state, not an invented RTL function.
Source correspondence is reported separately by contracts.py.
"""
from collections import Counter
from .boolean import BDD, Function, ProofBudgetExceeded, signal_order
from .common import require


class UnsupportedFunction(Exception):
    pass


class LogicDAG:
    """Collision-free structural Boolean terms, independent boundary variables.

    Equal roots prove equality; unequal roots say nothing about equivalence.
    This keeps copying/splicing proofs linear without expanding a ROBDD.
    """
    def __init__(self):
        self.nodes = [None, None]
        self.unique = {}

    @property
    def one(self): return Function(self, 1)

    def node(self, term):
        if term not in self.unique:
            self.unique[term] = len(self.nodes)
            self.nodes.append(term)
        return self.unique[term]

    def variables(self, count):
        return [Function(self, self.node(('input', i))) for i in range(count)]

    def apply(self, op, a, b):
        a,b = sorted((a,b))
        if op=='and':
            if a==0:return 0
            if a==1 or a==b:return b
        elif op=='or':
            if a==0 or a==b:return b
            if a==1:return 1
        elif op=='xor':
            if a==0:return b
            if a==b:return 0
            if a==1:
                if self.nodes[b][0]=='not':return self.nodes[b][1]
                return self.node(('not',b))
        else:raise UnsupportedFunction('unsupported Boolean term '+op)
        return self.node((op,a,b))

    def statistics(self):return {'backend':'structural_boolean_dag','nodes':len(self.unique)}


def encode_dag(function, input_ports):
    manager,root = function.manager,function.node
    remap,nodes={0:0,1:1},[]
    stack=[(root,False)]
    while stack:
        node,done=stack.pop()
        if node in remap:continue
        term=manager.nodes[node]
        children=() if term[0]=='input' else term[1:]
        if done:
            remap[node]=len(nodes)+2
            row={'id':remap[node],'op':term[0]}
            if term[0]=='input':row['input_port']=input_ports[term[1]]
            else:row['args']=[remap[c] for c in children]
            nodes.append(row)
        else:stack.extend([(node,True),*((c,False) for c in reversed(children))])
    return {'encoding':'boolean_dag_v1','root':remap[root],'nodes':nodes,
            'support':sorted({n['input_port'] for n in nodes if n['op']=='input'})}


def gate_function(kind, ports, one):
    """Pinned Yosys simcells.v two-state generic gate semantics."""
    inv = lambda x: one ^ x
    if kind == '$_BUF_': return ports['A']
    if kind == '$_NOT_': return inv(ports['A'])
    if kind in {'$_AND_', '$_NAND_', '$_OR_', '$_NOR_', '$_XOR_', '$_XNOR_', '$_ANDNOT_', '$_ORNOT_'}:
        a,b = ports['A'],ports['B']
        if kind in {'$_ANDNOT_', '$_ORNOT_'}: b = inv(b)
        if kind in {'$_AND_', '$_NAND_', '$_ANDNOT_'}: value = a & b
        elif kind in {'$_OR_', '$_NOR_', '$_ORNOT_'}: value = a | b
        else: value = a ^ b
        return inv(value) if kind in {'$_NAND_', '$_NOR_', '$_XNOR_'} else value
    if kind in {'$_AOI3_', '$_OAI3_', '$_AOI4_', '$_OAI4_'}:
        a,b,c = (ports[p] for p in 'ABC')
        if kind == '$_AOI3_': return inv((a & b) | c)
        if kind == '$_OAI3_': return inv((a | b) & c)
        d = ports['D']
        return inv((a & b) | (c & d)) if kind == '$_AOI4_' else inv((a | b) & (c | d))
    if kind in {'$_MUX_', '$_NMUX_', '$_MUX4_', '$_MUX8_', '$_MUX16_'}:
        width = {'$_MUX_':2, '$_NMUX_':2, '$_MUX4_':4, '$_MUX8_':8, '$_MUX16_':16}[kind]
        data = [ports[p] for p in 'ABCDEFGHIJKLMNOP'[:width]]
        for select in 'STUV'[:width.bit_length()-1]:
            s = ports[select]
            data = [(inv(s) & a) | (s & b) for a,b in zip(data[::2],data[1::2])]
        return inv(data[0]) if kind == '$_NMUX_' else data[0]
    raise UnsupportedFunction('unsupported_gate:'+kind)


class Circuit:
    def __init__(self, cells, inputs, manager):
        self.cells, self.manager = cells, manager
        self.values, self.drivers = dict(inputs), {}
        for name,cell in cells.items():
            for port,direction in cell['port_directions'].items():
                if direction != 'output': continue
                for bit in cell['connections'][port]:
                    require(isinstance(bit,int) and bit not in self.drivers, 'invalid region output driver')
                    self.drivers[bit] = name

    def cone(self, bit):
        names, seen, stack = set(), set(), [bit]
        while stack:
            bit = stack.pop()
            if not isinstance(bit,int) or bit in seen: continue
            seen.add(bit)
            name = self.drivers.get(bit)
            if name is None: continue
            if name in names: continue
            names.add(name)
            cell = self.cells[name]
            stack.extend(b for p,bs in cell['connections'].items() if cell['port_directions'][p]=='input' for b in bs)
        return sorted(names)

    def value(self, bit):
        active = set()
        def visit(b):
            if isinstance(b,str):
                if b not in {'0','1'}: raise UnsupportedFunction('non_binary_constant:'+b)
                return self.manager.one if b=='1' else self.manager.one ^ self.manager.one
            if b in self.values: return self.values[b]
            require(b in self.drivers, 'region function has unbound input')
            require(b not in active, 'cycle in region function')
            active.add(b)
            cell = self.cells[self.drivers[b]]
            if cell['parameters'] or any(len(bs)!=1 for bs in cell['connections'].values()) or \
                    {p for p,d in cell['port_directions'].items() if d=='output'} != {'Y'}:
                raise UnsupportedFunction('unsupported_gate_shape:'+cell['type'])
            ports = {p:visit(bs[0]) for p,bs in cell['connections'].items() if cell['port_directions'][p]=='input'}
            self.values[b] = gate_function(cell['type'], ports, self.manager.one)
            active.remove(b)
            return self.values[b]
        return visit(bit)


def encode_function(function, input_ports):
    """Self-contained reduced decision DAG; terminals 0/1, ports label variables."""
    manager, root = function.manager, function.node
    remap, nodes = {0:0, 1:1}, []
    stack = [(root,False)]
    while stack:
        node,done = stack.pop()
        if node in remap: continue
        variable,low,high = manager.nodes[node]
        if done:
            remap[node] = len(nodes)+2
            nodes.append({'id':remap[node], 'input_port':input_ports[variable],
                          'low':remap[low], 'high':remap[high]})
        else: stack.extend(((node,True),(high,False),(low,False)))
    return {'encoding':'robdd_v1', 'root':remap[root], 'nodes':nodes,
            'support':sorted({n['input_port'] for n in nodes})}


def ordered_inputs(boundary, cells=None):
    """Prioritize high-fanout controls, then interleave high operand bits."""
    fanout = Counter(b for cell in (cells or {}).values() for p, bs in cell['connections'].items()
                     if cell['port_directions'][p] == 'input' for b in bs)
    def key(row):
        aliases=[a for a in row.get('aliases',[]) if not a.get('hide_name',True)]
        if aliases:
            alias=min(aliases,key=lambda a:signal_order(a['name'],a.get('source_index',a['offset'])))
            return (fanout[row['original_bit']] < 16, *signal_order(alias['name'],alias.get('source_index',alias['offset'])),row['port'])
        return (fanout[row['original_bit']] < 16, *signal_order(row['port']),row['port'])
    return sorted((b for b in boundary if b['direction']=='input'),key=key)


def verify_outputs(recorded_cells, final_cells, boundary, *, structural=False):
    """Check EVERY cut output against final netlist at independent cut inputs."""
    structural = structural or len(recorded_cells)>256
    manager = LogicDAG() if structural else BDD()
    inputs = ordered_inputs(boundary, recorded_cells)
    variables = {}
    setup_error = None
    try: variables = dict(zip([b['original_bit'] for b in inputs],manager.variables(len(inputs))))
    except ProofBudgetExceeded as exc: setup_error = str(exc)
    reference, final = Circuit(recorded_cells,variables,manager), Circuit(final_cells,variables,manager)
    rows = []
    for output in (b for b in boundary if b['direction']=='output'):
        row = {'port':output['port'], 'original_bit':output['original_bit'],
               'cone_cells':reference.cone(output['original_bit']), 'status':'not_proven'}
        try:
            if setup_error: raise ProofBudgetExceeded(setup_error)
            expected = reference.value(output['original_bit'])
            actual = final.value(output['original_bit'])
            if structural and expected != actual:
                raise UnsupportedFunction('structural_function_mismatch')
            require(expected == actual, 'region output function differs from recorded objects: '+output['port'])
            encoder=encode_dag if structural else encode_function
            row.update(status='verified', function=encoder(expected,[b['port'] for b in inputs]),
                       proof_method='structural_boolean_identity' if structural else 'robdd_equality')
        except (UnsupportedFunction, ProofBudgetExceeded, RecursionError) as exc:
            if not structural and isinstance(exc,ProofBudgetExceeded):
                return verify_outputs(recorded_cells,final_cells,boundary,structural=True)
            row['reason'] = 'function_depth_limit' if isinstance(exc,RecursionError) else str(exc)
        rows.append(row)
    return {'scope':'replayed_final_objects_to_final_netlist_at_independent_boundary',
            'semantic_domain':'two_state_combinational', 'input_order':[b['port'] for b in inputs],
            'all_outputs_verified':bool(rows) and all(r['status']=='verified' for r in rows),
            'outputs':rows, 'resources':manager.statistics(), 'proves_rtl_source_completeness':False}
