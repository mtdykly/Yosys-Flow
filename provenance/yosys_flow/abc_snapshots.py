"""Immutable views of a linear ABC snapshot history, without full dict copies.

Each integer key keeps its value changes. A view's version and size never change;
later writes cannot alter earlier views. Branching is deliberately rejected:
decode_network separately requires the immediately preceding snapshot/epoch.
"""
from bisect import bisect_left
from collections.abc import Mapping


_DELETED = object()


class SnapshotMap(Mapping):
    __slots__ = ('_history', '_clock', '_version', '_size')

    def __init__(self, values):
        self._history = {key: [(0, value)] for key, value in values.items()}
        self._clock = [0]
        self._version = 0
        self._size = len(values)

    def __getitem__(self, key):
        history = self._history[key]
        index = bisect_left(history, (self._version + 1,)) - 1
        if index < 0 or history[index][1] is _DELETED:
            raise KeyError(key)
        return history[index][1]

    def __iter__(self):
        for key in self._history:
            if key in self:
                yield key

    def __len__(self):
        return self._size

    def updated(self, changes, deleted=()):
        if not changes and not deleted:
            return self
        if self._version != self._clock[0]:
            raise ValueError('ABC snapshot history cannot branch')
        # Validate before publishing anything to the shared histories.
        removed = set(deleted)
        if len(removed) != len(deleted) or removed.intersection(changes):
            raise ValueError('invalid snapshot disposition')
        if any(key not in self for key in removed):
            raise KeyError('deleting absent snapshot key')
        added = sum(key not in self for key in changes)
        version = self._version + 1
        for key, value in changes.items():
            self._history.setdefault(key, []).append((version, value))
        for key in removed:
            self._history[key].append((version, _DELETED))
        self._clock[0] = version
        view = object.__new__(type(self))
        view._history, view._clock = self._history, self._clock
        view._version, view._size = version, self._size + added - len(removed)
        return view
