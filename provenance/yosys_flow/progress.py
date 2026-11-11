"""Live archival diagnostics, deliberately outside the evidence directory."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time


class ArchiveProgress:
    def __init__(self, path):
        self.path = Path(path)
        self.started = self.stage_started = time.perf_counter()
        self.stage = None
        self.completed = []
        self.updated = 0.0

    def __call__(self, stage, **details):
        now = time.perf_counter()
        changed = stage != self.stage
        if changed:
            if self.stage is not None:
                self.completed.append({'stage': self.stage, 'seconds': now-self.stage_started})
            self.stage, self.stage_started = stage, now
        if changed or now-self.updated >= 5:
            self._write('running', now, details)

    def finish(self, status='complete', **details):
        now = time.perf_counter()
        if self.stage is not None:
            self.completed.append({'stage': self.stage, 'seconds': now-self.stage_started,
                                   'finished': status == 'complete'})
        self._write(status, now, details)

    def _write(self, status, now, details):
        row = {'status': status, 'pid': os.getpid(), 'stage': self.stage,
               'updated_utc': datetime.now(timezone.utc).isoformat(),
               'elapsed_seconds': now-self.started, 'stage_elapsed_seconds': now-self.stage_started,
               'completed_stages': self.completed, 'details': details}
        temporary = self.path.with_name(self.path.name+'.tmp')
        temporary.write_text(json.dumps(row, ensure_ascii=False, indent=2)+'\n')
        os.replace(temporary, self.path)
        print(f'[yosys-flow] {self.stage}: {status}; elapsed {now-self.started:.1f}s'
              + (f'; {details}' if details else ''), file=sys.stderr, flush=True)
        self.updated = now
