"""Read-only JSONL sequence with compact offsets instead of decoded row copies.

Iteration is sequential IO. Random access is used for headers/trailers and
diagnostics; the verified index still stores the actual rows, never this path.
"""
from array import array
from collections.abc import Sequence
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path

from .common import require


class JsonlRecords(Sequence):
    def __init__(self, path, progress=None):
        self.path = Path(path).resolve()
        self.offsets = array('Q')
        with self.path.open('rb') as stream:
            self.identity = self._identity(os.fstat(stream.fileno()))
            offset = 0
            for count, line in enumerate(stream, 1):
                self.offsets.append(offset)
                offset += len(line)
                if progress and count % 100000 == 0:
                    progress('native_record_scan', records=count, bytes_read=offset)
            require(self._identity(os.fstat(stream.fileno())) == self.identity,
                    'JSONL evidence changed during scan')

    @staticmethod
    def _identity(stat):
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _check(self, stream):
        require(self._identity(os.fstat(stream.fileno())) == self.identity,
                'JSONL evidence changed during replay')

    def __len__(self):
        return len(self.offsets)

    def __iter__(self):
        with self.path.open('rb') as stream:
            self._check(stream)
            for line in stream:
                yield json.loads(line)
            self._check(stream)

    def __getitem__(self, index):
        if isinstance(index, slice):
            with self.path.open('rb') as stream:
                self._check(stream)
                result = []
                for i in range(*index.indices(len(self))):
                    stream.seek(self.offsets[i])
                    result.append(json.loads(stream.readline()))
                self._check(stream)
                return result
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        with self.path.open('rb') as stream:
            self._check(stream)
            stream.seek(self.offsets[index])
            result = json.loads(stream.readline())
            self._check(stream)
            return result

    def __eq__(self, other):
        if not isinstance(other, Sequence):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def __deepcopy__(self, memo):
        return deepcopy(list(self), memo)


class LiveJsonlRecords(JsonlRecords):
    """Consume complete records as the producer flushes batches; no row queue.

    After EOF and producer completion this becomes an ordinary immutable
    JsonlRecords. The rolling hash must match the frozen capture before use.
    Partial lines wait for more bytes; truncation/trailing data are errors.
    """
    def __init__(self, path, finished, cancelled):
        self.path = Path(path).resolve()
        self.offsets = array('Q')
        self.finished, self.cancelled = finished, cancelled
        self.identity = None
        self.stream_sha256 = None

    def __iter__(self):
        if self.identity is not None:
            yield from super().__iter__()
            return
        while not self.path.exists():
            require(not self.cancelled.is_set(), 'streaming replay cancelled')
            require(not self.finished.is_set(), 'capture ended without native records')
            self.finished.wait(0.05)
        sha, offset = hashlib.sha256(), 0
        with self.path.open('rb') as stream:
            initial = os.fstat(stream.fileno())
            pending = b''
            while True:
                require(not self.cancelled.is_set(), 'streaming replay cancelled')
                chunk = stream.readline()
                if chunk:
                    pending += chunk
                    if not pending.endswith(b'\n'):
                        continue
                    sha.update(pending)
                    self.offsets.append(offset)
                    offset += len(pending)
                    row = json.loads(pending)
                    pending = b''
                    yield row
                elif self.finished.is_set():
                    # Re-read after observing producer completion: data may
                    # have arrived between our previous read and the signal.
                    chunk = stream.readline()
                    if chunk:
                        pending += chunk
                        if pending.endswith(b'\n'):
                            sha.update(pending); self.offsets.append(offset)
                            offset += len(pending)
                            row = json.loads(pending); pending = b''
                            yield row
                        continue
                    require(not pending, 'truncated streaming native record')
                    stat = os.fstat(stream.fileno())
                    require((stat.st_dev,stat.st_ino) == (initial.st_dev,initial.st_ino) and
                            stat.st_size == offset, 'native capture truncated during streaming replay')
                    self.identity = self._identity(stat)
                    self.stream_sha256 = sha.hexdigest()
                    return
                else:
                    self.finished.wait(0.05)
