"""Structural metrics for Yosys JSON, without technology-library claims.

``measure_netlist(json_path_or_mapping, top)`` counts electrically instantiated
leaf cells (a module instantiated twice is counted twice).  ``logic_depth`` is
the longest unit-gate path to a top output; ``maximum_cell_depth`` also includes
unused cells.  Hierarchy boundaries cost zero stages.  Neither value estimates
area or delay.  Only Yosys generic single-bit combinational gates are supported
for depth.  Unsupported logic, unknown values, multiple/absent drivers or cycles
produce ``depth_status='unsupported'`` and null depth values, while preserving
cell counts.  Malformed JSON/hierarchy raises ``NetlistMetricError``.
"""

from collections import Counter, defaultdict, deque
from collections.abc import Mapping
import json
from pathlib import Path


class NetlistMetricError(ValueError):
    """The input cannot be interpreted as a finite instantiated netlist."""


_GATE_INPUTS = {
    '$_BUF_': {'A'}, '$_NOT_': {'A'},
    **{f'$_{name}_': {'A', 'B'} for name in
       ('AND', 'NAND', 'OR', 'NOR', 'XOR', 'XNOR', 'ANDNOT', 'ORNOT')},
    '$_MUX_': {'A', 'B', 'S'}, '$_NMUX_': {'A', 'B', 'S'},
    '$_AOI3_': {'A', 'B', 'C'}, '$_OAI3_': {'A', 'B', 'C'},
    '$_AOI4_': {'A', 'B', 'C', 'D'}, '$_OAI4_': {'A', 'B', 'C', 'D'},
}


def _require(condition, message):
    if not condition:
        raise NetlistMetricError(message)


def _enabled(value):
    if isinstance(value, str) and value and set(value) <= {'0', '1'}:
        return int(value, 2) != 0
    return bool(value)


class _Aliases:
    def __init__(self):
        self.parent = {}

    def find(self, value):
        self.parent.setdefault(value, value)
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            previous = value
            value = self.parent[value]
            self.parent[previous] = root
        return root

    def union(self, first, second):
        a, b = self.find(first), self.find(second)
        if a == b:
            return
        if isinstance(a, str) and isinstance(b, str):
            _require(False, 'hierarchy connects incompatible constants')
        # Constants remain canonical so they are recognized as depth-zero roots.
        if isinstance(a, str):
            self.parent[b] = a
        else:
            self.parent[a] = b


def measure_netlist(netlist, top):
    """Return counts plus qualified depth metrics for an explicit top module.

    ``netlist`` is a Yosys ``write_json`` mapping or a path to such a file.
    A file input additionally records ``netlist_bytes``.  The function neither
    modifies the input nor invokes synthesis.  Black boxes count as leaf cells
    and cause depth to be unsupported.  Uninstantiated module definitions are
    ignored, as are pinless ``$scopeinfo`` metadata records left by flattening.
    A ``$scopeinfo`` record declaring pins is malformed and rejected, rather
    than silently hiding electrical logic.  ``depth_issues`` supplies the
    reasons for a null depth result.
    """
    file_size = None
    if isinstance(netlist, (str, Path)):
        path = Path(netlist)
        file_size = path.stat().st_size
        netlist = json.loads(path.read_text())
    _require(isinstance(netlist, Mapping), 'expected a Yosys JSON mapping')
    modules = netlist.get('modules', {})
    _require(top in modules, f'missing top module: {top}')
    aliases = _Aliases()
    leaves = []
    issues = set()
    top_inputs, top_outputs = [], []
    module_instances = 0

    def bits(path, values):
        _require(isinstance(values, list), 'signal bits must be an array')
        result = []
        for bit in values:
            _require((isinstance(bit, int) and not isinstance(bit, bool) and bit >= 0)
                     or (isinstance(bit, str) and bit in {'0', '1', 'x', 'z'}),
                     f'invalid signal bit: {bit!r}')
            if bit in ('x', 'z'):
                issues.add('unknown or high-impedance signal value')
            result.append(aliases.find(bit if isinstance(bit, str) else (path, bit)))
        return result

    def expand(module_name, path, ancestors):
        nonlocal module_instances
        _require(module_name not in ancestors, 'recursive module hierarchy')
        module_instances += 1
        module = modules[module_name]
        if module.get('processes') or module.get('memories'):
            issues.add('unlowered process or memory')
        ports = module.get('ports', {})
        port_bits = {}
        for name, port in ports.items():
            direction = port.get('direction')
            if direction not in {'input', 'output'}:
                issues.add('unsupported module port direction')
            port_bits[name] = bits(path, port.get('bits'))
            if not path:
                if direction == 'input':
                    top_inputs.extend(port_bits[name])
                elif direction == 'output':
                    top_outputs.extend(port_bits[name])
        for name, cell in module.get('cells', {}).items():
            cell_path = path + (name,)
            kind = cell.get('type')
            _require(isinstance(kind, str), 'cell type must be a string')
            if kind == '$scopeinfo':
                _require(not cell.get('connections') and not cell.get('port_directions'),
                         '$scopeinfo metadata must not have electrical pins')
                continue
            connections = {port: bits(path, signal)
                           for port, signal in cell.get('connections', {}).items()}
            directions = cell.get('port_directions', {})
            if set(directions) != set(connections):
                issues.add('missing or inconsistent cell port directions')
            if any(direction not in {'input', 'output'} for direction in directions.values()):
                issues.add('unsupported cell port direction')
            child = modules.get(kind)
            blackbox = child is not None and any(
                _enabled(child.get('attributes', {}).get(attribute, 0))
                for attribute in ('blackbox', 'whitebox'))
            if child is None or blackbox:
                leaves.append((cell_path, kind, connections, directions))
                continue
            child_ports = expand(kind, cell_path, ancestors + (module_name,))
            _require(set(child_ports) == set(connections), 'hierarchy port set mismatch')
            for port, inner in child_ports.items():
                outer = connections[port]
                _require(len(inner) == len(outer), 'hierarchy port width mismatch')
                if directions.get(port) != child['ports'][port].get('direction'):
                    issues.add('hierarchy port direction mismatch')
                for a, b in zip(inner, outer):
                    aliases.union(a, b)
        return port_bits

    expand(top, (), ())
    counts = Counter(kind for _, kind, _, _ in leaves)
    result = {
        'top': top, 'cell_count': len(leaves),
        'cell_types': dict(sorted(counts.items())),
        'module_instance_count': module_instances,
        'logic_depth': None, 'maximum_cell_depth': None,
        'depth_status': 'unsupported', 'depth_issues': [],
        'depth_unit': 'generic_gate_stages',
    }
    if file_size is not None:
        result['netlist_bytes'] = file_size

    inputs = {aliases.find(bit) for bit in top_inputs}
    outputs = [aliases.find(bit) for bit in top_outputs]
    drivers = {}
    cell_inputs = []
    for index, (_, kind, connections, directions) in enumerate(leaves):
        expected = _GATE_INPUTS.get(kind)
        if expected is None:
            issues.add(f'unsupported leaf cell type: {kind}')
        elif (set(connections) != expected | {'Y'}
              or directions != {**{name: 'input' for name in expected}, 'Y': 'output'}
              or any(len(signal) != 1 for signal in connections.values())):
            issues.add(f'unsupported generic gate signature: {kind}')
        dependencies = set()
        for port, signal in connections.items():
            for bit in signal:
                root = aliases.find(bit)
                if directions.get(port) == 'input':
                    dependencies.add(root)
                elif directions.get(port) == 'output':
                    if isinstance(root, str) or root in inputs or root in drivers:
                        issues.add('multiple drivers or a driven constant/input')
                    drivers[root] = index
        cell_inputs.append(dependencies)

    prerequisites = []
    users = defaultdict(set)
    for index, signal in enumerate(cell_inputs):
        dependencies = set()
        for bit in signal:
            if bit in drivers:
                dependencies.add(drivers[bit])
            elif bit not in inputs and bit not in {'0', '1'}:
                issues.add('undriven cell input')
        prerequisites.append(dependencies)
        for dependency in dependencies:
            users[dependency].add(index)
    for bit in outputs:
        if bit not in drivers and bit not in inputs and bit not in {'0', '1'}:
            issues.add('undriven top output')

    pending = [len(dependencies) for dependencies in prerequisites]
    ready = deque(index for index, count in enumerate(pending) if count == 0)
    depths = {}
    while ready:
        index = ready.popleft()
        depths[index] = 1 + max((depths[d] for d in prerequisites[index]), default=0)
        for user in users[index]:
            pending[user] -= 1
            if pending[user] == 0:
                ready.append(user)
    if len(depths) != len(leaves):
        issues.add('combinational dependency cycle')
    result['depth_issues'] = sorted(issues)
    if not issues:
        result.update(depth_status='ok',
                      logic_depth=max((depths[drivers[b]] if b in drivers else 0
                                       for b in outputs), default=0),
                      maximum_cell_depth=max(depths.values(), default=0))
    return result
