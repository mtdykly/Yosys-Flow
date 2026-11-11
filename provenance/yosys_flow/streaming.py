"""Maintain source/object/transition lookup state alongside native synthesis.

The live state is provisional. Only the ordinary completed capture validation
and ABC refinement may publish a signed final query index. No polling queue or
second parsed copy of the JSONL is kept in memory.
"""
from threading import Event, Thread
import time

from .common import require
from .jsonl_records import LiveJsonlRecords
from .progress import ArchiveProgress


class CaptureReplay:
    def __init__(self, directory, trace_class):
        self.finished, self.cancelled = Event(), Event()
        self.records = LiveJsonlRecords(directory/'yosys.jsonl', self.finished, self.cancelled)
        self.trace, self.error, self.seconds = None, None, 0.0
        self.progress = ArchiveProgress(directory.with_name(directory.name+'.replay.progress.json'))
        def replay():
            start = time.perf_counter()
            try:
                self.trace = trace_class(self.records.path, records=self.records,
                                         defer_refinement=True, progress=self.progress)
                self.progress.finish('complete', records=len(self.records), provisional=True)
            except BaseException as exc:
                self.error = exc
                self.progress.finish('failed', error=str(exc))
            finally:
                self.seconds = time.perf_counter()-start
        self.thread = Thread(target=replay, name='capture-provenance-replay', daemon=True)
        self.thread.start()

    def result(self, expected_sha256):
        self.thread.join()
        if self.error: raise self.error
        require(self.records.stream_sha256 == expected_sha256,
                'streamed native evidence differs from frozen capture')
        return self.trace

    def close(self):
        self.cancelled.set()
        self.finished.set()
        self.thread.join()
