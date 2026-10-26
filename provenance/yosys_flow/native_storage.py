"""Lossless run-local array references in native JSONL evidence.

Definitions precede uses. Returned lists are immutable by convention; captured
records remain unmodified. This encoding does not add provenance relationships.
"""
from .common import require


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
