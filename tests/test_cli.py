"""Public CLI contracts, including explicit retirement of the old format."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

from yosys_flow.cli import main, parser
from yosys_flow.common import TraceError
from yosys_flow.runner import load_run


class CliTests(unittest.TestCase):
    def test_default_flow_and_development_checkpoints(self):
        base = ["run", "--source", "input.v", "--top", "top", "--out", "run"]
        self.assertEqual(parser().parse_args(base).flow, "synth")
        for flow in ("synth", "m1", "m1-opt"):
            self.assertEqual(parser().parse_args(base + ["--flow", flow]).flow, flow)
        args = vars(parser().parse_args(base))
        self.assertTrue({"yosys", "native_yosys"} <= args.keys())
        self.assertTrue({"plugin", "steps", "nomux"}.isdisjoint(args))

    def test_retired_options_are_rejected(self):
        base = ["run", "--source", "input.v", "--top", "top", "--out", "run"]
        for options in (["--flow", "opt-merge"], ["--plugin", "old.so"], ["--steps", "2"], ["--nomux"]):
            with self.subTest(options=options), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                parser().parse_args(base + options)
            self.assertEqual(error.exception.code, 2)

    def test_v1_rejected_by_all_readers_without_touching_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "manifest.json").write_text(json.dumps({"schema_version": 1, "status": "complete"}))
            (directory / "yosys.jsonl").write_text("historical trace\n")
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            with self.assertRaisesRegex(TraceError, "v1.*retired"):
                load_run(directory)
            for command in (
                ["validate", str(directory)],
                ["query", str(directory), "--source", "old.v", "--line", "2"],
                ["extract", str(directory), "--source", "old.v", "--line", "2", "--out", str(directory / "selected.v")],
            ):
                with self.subTest(command=command), redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(main(command), 1)
                    self.assertIn("v1 support has been retired", output.getvalue())
                self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_unknown_schema_rejected_before_reading_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for version in (None, 0, 3):
                (directory / "manifest.json").write_text(json.dumps({"schema_version": version}))
                with self.subTest(version=version), self.assertRaisesRegex(TraceError, "unsupported run schema"):
                    load_run(directory)
