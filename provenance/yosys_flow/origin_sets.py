"""Operation-scoped set views of immutable, shared provenance arrays."""

PLAN_INLINE_LIMIT = 65536


class OriginMatcher:
    def __init__(self, seeds):
        self.seeds = set(seeds)
        self.matches = {}

    def intersects(self, origins):
        key = id(origins)
        if key not in self.matches:
            # Retain the array: an object ID must never be reused in this query.
            self.matches[key] = (origins, not self.seeds.isdisjoint(origins))
        return self.matches[key][1]


class OriginSets:
    def __init__(self):
        self.identities, self.canonical = {}, {}
        self.groups, self.unions, self.ordered = [], {}, {}

    def group(self, values):
        key = id(values)
        if key not in self.identities:
            sources = frozenset(values)
            if sources not in self.canonical:
                self.canonical[sources] = len(self.groups)
                self.groups.append(sources)
            self.identities[key] = (values, self.canonical[sources])
        return self.identities[key][1]

    def ids(self, arrays):
        return tuple(sorted({self.group(values) for values in arrays}))

    def union_ids(self, groups):
        if groups not in self.unions:
            self.unions[groups] = (self.groups[groups[0]] if len(groups) == 1 else
                                   frozenset().union(*(self.groups[g] for g in groups)))
        return self.unions[groups]

    def union(self, arrays):
        return self.union_ids(self.ids(arrays))

    def ordered_union(self, arrays):
        groups = self.ids(arrays)
        if groups not in self.ordered:
            self.ordered[groups] = sorted(self.union_ids(groups))
        return self.ordered[groups]


def pack_plan_sources(document):
    """Keep large plans linear in unique sets, including after JSON encoding."""
    slots = [(cell, 'origin_ids', 'origin_set_id') for cell in document['region']['cells']]
    slots += [(output, 'recorded_source_ids', 'recorded_source_set_id') for output in document['outputs']]
    if sum(len(row[field]) for row, field, _ in slots) <= PLAN_INLINE_LIMIT:
        return
    pool = OriginSets()
    for row, field, ref_field in slots:
        row[ref_field] = str(pool.group(row.pop(field)))
    document['source_set_encoding'] = 'shared_source_sets_v1'
    document['source_sets'] = {str(i):sorted(group) for i,group in enumerate(pool.groups)}


def cell_origins(document, cell):
    return cell['origin_ids'] if 'origin_ids' in cell else document['source_sets'][cell['origin_set_id']]
