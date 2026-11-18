"""Lossless run-local array references in native JSONL evidence.

Definitions precede uses. Returned lists are immutable by convention; captured
records remain unmodified. This encoding does not add provenance relationships.
"""
from .common import require


class Checkpoints:
    """Independent audit state; never derive a checkpoint from lifecycle events."""
    def __init__(self, features):
        self.enabled = 'native_checkpoint_deltas_v1' in features
        self.sequence = None
        self.cells, self.connections = set(), {}

    def check(self, row, live, assignments):
        encoding = row.get('checkpoint_encoding')
        require(encoding is None or (self.enabled and encoding in {'full', 'delta'}),
                'invalid native checkpoint encoding')
        require(not self.enabled or encoding is not None, 'missing native checkpoint encoding')
        require(row['record_type'] != 'run_end' or encoding != 'delta',
                'native run_end must be a full checkpoint')
        def refs(field):
            values = row.get(field)
            require(isinstance(values, list) and all(isinstance(v, str) for v in values),
                    'invalid native checkpoint references')
            result = set(values)
            require(len(values) == len(result), 'duplicate native checkpoint references')
            return result
        connections = row.get('connections', {})
        require(isinstance(connections, dict) and all(isinstance(k, str) and type(v) is int and v >= 0
                for k, v in connections.items()), 'invalid native connection checkpoint')
        if encoding == 'delta':
            require(type(row.get('base_checkpoint')) is int and self.sequence is not None and
                    row['base_checkpoint'] == self.sequence and 'cells' not in row,
                    'native checkpoint has missing or stale base')
            added, removed = refs('cell_additions'), refs('cell_removals')
            require(removed <= self.cells and not added.intersection(self.cells) and not added.intersection(removed),
                    'invalid native checkpoint delta disposition')
            self.cells.difference_update(removed)
            self.cells.update(added)
            self.connections.update(connections)
        else:
            require(not {'base_checkpoint', 'cell_additions', 'cell_removals'}.intersection(row),
                    'invalid full native checkpoint')
            self.cells = refs('cells')
            self.connections = dict(connections)
        if self.enabled:
            require(type(row.get('cell_count')) is int and row['cell_count'] == len(self.cells) and
                    type(row.get('connection_count')) is int and row['connection_count'] == len(self.connections),
                    'native checkpoint counts differ')
        require(self.cells == live, 'native snapshot does not match lifecycle replay')
        if self.enabled or 'connections' in row:
            require(self.connections == {r: obj['revision'] for r, obj in assignments.items()},
                    'native connection snapshot differs from replay')
        self.sequence = row['seq']


def same_array(left, right):
    """Shared immutable arrays need no repeated element-wise equality scan."""
    return left is right or left == right


class Arrays:
    def __init__(self, features):
        self.enabled = 'shared_native_arrays_v1' in features
        self.values = {}

    def define(self, row, sources):
        ident, kind, values = row['id'], row['element_kind'], row['values']
        require(self.enabled and type(ident) is int and ident == len(self.values)+1,
                'invalid native shared array definition')
        require(isinstance(values, list), 'native shared array must contain a list')
        if kind == 'origins':
            require(all(type(v) is int and v > 0 for v in values) and
                    values == sorted(set(values)) and set(values) <= sources.keys(),
                    'invalid native shared origins')
        else:
            require(kind == 'inputs' and all(isinstance(v, str) for v in values) and
                    len(set(values)) == len(values), 'invalid native shared inputs')
        self.values[ident] = (kind, values)

    def resolve(self, value, kind):
        if not isinstance(value, dict):
            require(isinstance(value, list), 'native array must be a list or reference')
            return value
        ident = value.get('$native_array')
        require(self.enabled and set(value) == {'$native_array'} and type(ident) is int and
                ident in self.values, 'unknown or malformed native shared array reference')
        stored_kind, values = self.values[ident]
        require(stored_kind == kind, 'native shared array element kind mismatch')
        return values

    def decode(self, row):
        result = dict(row)
        for field in ('origins', 'inputs', 'enclosing_controls'):
            if field in row:
                result[field] = self.resolve(row[field], 'inputs' if field == 'inputs' else 'origins')
        if 'roles' in row:
            result['roles'] = {k: self.resolve(v, 'origins') for k, v in row['roles'].items()}
        return result
