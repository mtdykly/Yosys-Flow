"""Factored reverse provenance: sources -> shared groups -> ordered objects.

No source/object Cartesian product is materialized while building or loading.
Only a requested source (or union of sources) expands its groups. Origin lists
are immutable in the verified trace; equal lists share groups across versions.
"""
from collections.abc import Mapping
from heapq import merge


class SourceRelation(Mapping):
    def __init__(self, sources, members):
        self.sources, self.members = sources, members

    @classmethod
    def build(cls, rows, progress=None):
        sources, members, sets, identities = {}, {}, {}, {}
        for ordinal, (ref, origins) in enumerate(rows):
            cached = identities.get(id(origins))
            if cached is None:
                canonical = tuple(sorted(set(origins)))
                group = sets.get(canonical)
                if group is None:
                    group = len(members)
                    sets[canonical] = group
                    members[group] = []
                    for source in canonical:
                        sources.setdefault(source, []).append(group)
                # Retain the list so id reuse cannot alias a different group.
                identities[id(origins)] = (origins, group)
            else:
                group = cached[1]
            members[group].append([ordinal, ref])
            if progress and ordinal % 10000 == 0:
                progress('index_source_groups', objects=ordinal, groups=len(members))
        return cls(sources, members)

    def __len__(self): return len(self.sources)
    def __iter__(self): return iter(self.sources)
    def __contains__(self, source): return source in self.sources
    def __getitem__(self, source):
        return self._expand(self.sources[source])

    def _expand(self, groups):
        return [ref for _, ref in merge(*(self.members[g] for g in groups))]

    def select(self, sources):
        groups = {g for source in sources for g in self.sources.get(source, ())}
        return self._expand(groups)


def source_objects(relation, sources):
    if isinstance(relation, SourceRelation):
        return set(relation.select(sources))
    return {ref for source in sources for ref in relation.get(source, ())}
