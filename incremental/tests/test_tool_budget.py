"""Large-case tool budgets must propagate without leaking across runs."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from incremental import automatic, replace
from incremental.__main__ import parser


class ToolBudgetTests(unittest.TestCase):
    def test_cli_positive_budget_and_default(self):
        commands = [
            ['auto', '--base-run', 'old', '--new-source', 'new.v', '--out', 'result'],
            ['run', '--base-run', 'old', '--new-source', 'new.v', '--out', 'result',
             '--contract', 'contract.json', '--bindings', 'bindings.json', '--patch-rtl', 'patch.v'],
        ]
        for command in commands:
            self.assertEqual(parser().parse_args(command).yosys_timeout, 120)
            self.assertEqual(parser().parse_args(command + ['--yosys-timeout', '3600']).yosys_timeout, 3600)
            for invalid in ('0', '-1', 'nan', 'inf'):
                with self.subTest(command=command[0], invalid=invalid), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parser().parse_args(command + ['--yosys-timeout', invalid])

    def test_both_entrypoints_propagate_and_restore_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            response = subprocess.CompletedProcess([], 0, 'ok\n', '')
            def stage(args):
                replace.run_yosys(sys.executable, out, 'probe', 'help')
                raise RuntimeError('stage failed')
            with patch('incremental.replace.subprocess.run', return_value=response) as child:
                for entry in (automatic, replace):
                    with patch.object(entry, '_run', side_effect=stage), self.assertRaisesRegex(RuntimeError, 'stage failed'):
                        entry.run(SimpleNamespace(yosys_timeout=3600))
                    self.assertEqual(child.call_args.kwargs['timeout'], 3600)
                    replace.run_yosys(sys.executable, out, 'next', 'help')
                    self.assertEqual(child.call_args.kwargs['timeout'], 120)

    def test_timeout_still_rejects_and_preserves_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            failure = subprocess.TimeoutExpired(['yosys'], 3600, output=b'partial proof output')
            with replace.yosys_timeout(3600), patch('incremental.replace.subprocess.run', side_effect=failure):
                with self.assertRaises(subprocess.TimeoutExpired):
                    replace.run_yosys(sys.executable, out, 'proof', 'check -assert')
            self.assertIn('partial proof output', (out / 'proof.log').read_text())

    def test_tool_paths_resolved_before_entering_job_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / 'tools'
            tools.mkdir()
            executable = tools / 'fake-yosys'
            executable.write_text('#!'+sys.executable+'\n'
                                  'import json, os, pathlib, sys\n'
                                  'print(json.dumps({"cwd":os.getcwd(), '
                                  '"script":pathlib.Path(sys.argv[-1]).read_text()}))\n')
            executable.chmod(0o700)
            work = root / 'job'
            work.mkdir()
            # The relative PATH entry must also be interpreted before chdir.
            with patch.dict(os.environ, {'PATH': os.path.relpath(tools)}):
                for binary in (str(executable), os.path.relpath(executable), 'fake-yosys'):
                    with self.subTest(binary=binary):
                        replace.run_yosys(binary, work, 'probe', 'check -assert')
                        result = json.loads((work / 'probe.log').read_text())
                        self.assertEqual(result['cwd'], str(work.resolve()))
                        self.assertEqual(result['script'], 'check -assert\n')
