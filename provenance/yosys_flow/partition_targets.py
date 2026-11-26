"""Resolve captured ABC inputs by actual signal binding, never historical IDs."""
from .common import require, unescape


def resolve(trace, wires, module, cell_type=None, invocation=None):
    result = []
    for wire in wires:
        candidates = set()
        for region in trace.regions:
            if unescape(region['module']) != unescape(module): continue
            if invocation and region['invocation_id'] != invocation: continue
            positions = {i for i,b in enumerate(region['signals']) if isinstance(b,dict) and
                         unescape(b['wire']) == unescape(wire)}
            for node in region['input_nodes']:
                if node['id'] not in positions or not node['cell']: continue
                ref = node['cell']; state = trace.objects[ref]['state']
                require(unescape(state['module']) == unescape(module), 'captured target module mismatch')
                if cell_type:
                    require(unescape(state['type']) == unescape(cell_type), 'captured target type changed')
                require(any(isinstance(b,dict) and unescape(b['wire']) == unescape(wire)
                            for p,bits in state['connections'].items() if state['port_directions'][p]=='output'
                            for b in bits), 'captured target output no longer names requested wire')
                candidates.add(ref)
        require(len(candidates)==1, f'wire {module}.{wire} must identify exactly one captured ABC input cell')
        result.append(next(iter(candidates)))
    return list(dict.fromkeys(result))
