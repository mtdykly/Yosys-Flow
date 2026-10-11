"""Real Yosys experiments and adversarial provenance validation; no mock optimizer."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from yosys_flow.cli import parser
from yosys_flow.runner import ROOT, load_run, query, run
from yosys_flow.trace import Trace, TraceError


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("yosys") or not (ROOT / "build/provenance.so").exists():
            raise RuntimeError("Real Yosys and built provenance.so are required; run make test")
        cls.temp = tempfile.TemporaryDirectory(prefix="yosys-flow-tests-")
        cls.base = Path(cls.temp.name)
        cls.source = cls.base / "merge.v"
        cls.source.write_text((ROOT / "examples/merge.v").read_text())
        cls.out = cls.base / "base"
        run(parser().parse_args(["run", "--source", str(cls.source), "--top", "demo", "--out", str(cls.out), "--steps", "2"]))
        cls.manifest, cls.trace, cls.report = load_run(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def experiment(self, name, rtl, *extra):
        source = self.base / (name + ".v")
        source.write_text(rtl)
        out = self.base / name
        args = parser().parse_args(["run", "--source", str(source), "--top", "demo", "--out", str(out), *extra])
        run(args)
        return load_run(out)

    def test_two_sources_one_gate_and_reverse(self):
        results = []
        for line in (2, 3):
            args = parser().parse_args(["query", str(self.out), "--source", str(self.source), "--line", str(line)])
            result = query(args)
            self.assertEqual(len(result["final_cells"]), 1)
            cell = result["final_cells"][0]
            self.assertEqual({c["name"] for c in cell["consumers"]}, {"y1", "y2"})
            self.assertEqual(len(cell["all_origins"]), 2)
            self.assertFalse(result["safe_for_incremental_reuse"])
            results.append(cell["ref"])
        self.assertEqual(*results)
        reverse = query(parser().parse_args(["query", str(self.out), "--object", results[0]]))
        self.assertEqual(len(reverse["sources"]), 2)
        self.assertTrue(all(self.report["checks"].values()))

    def test_repeated_pass_has_distinct_steps(self):
        steps = [r["step_id"] for r in self.trace.records if r["record_type"] == "step_begin"]
        self.assertEqual(steps, ["step_1", "step_2"])
        self.assertEqual(len(self.trace.events), 1)

    def test_commutative_multibit_cascade(self):
        _, trace, report = self.experiment("cascade", """module demo(input [3:0] a,b,c, output [3:0] y1,y2,y3);
wire [3:0] x1 = a & b;
wire [3:0] x2 = b & a;
wire [3:0] x3 = a & b;
assign y1 = x1 ^ c;
assign y2 = x2 ^ c;
assign y3 = x3 ^ c;
endmodule
""")
        self.assertEqual(report["counts"]["final_cells"], 2)
        self.assertEqual(len(trace.events), 4)
        self.assertTrue(any(o["version"] >= 2 for o in trace.objects.values()))

    def test_no_merge_alias_and_constant_survive(self):
        _, trace, report = self.experiment("aliases", """module demo(input a, output y,z);
assign y = a;
assign z = 1'b0;
endmodule
""")
        self.assertEqual(report["counts"]["final_cells"], 0)
        self.assertEqual(len(trace.events), 0)
        self.assertGreater(sum(trace.objects[r]["kind"] == "connection" for r in trace.final), 0)

    def test_changed_statement_does_not_replace_shared_gate(self):
        _, trace, report = self.experiment("changed", (ROOT / "examples/merge_new.v").read_text())
        self.assertEqual(report["counts"]["final_cells"], 2)
        self.assertEqual({trace.objects[r]["state"]["type"] for r in trace.final if trace.objects[r]["kind"] == "cell"}, {"$and", "$or"})

    def test_include_define_parameter_manifest(self):
        inc = self.base / "includes"
        inc.mkdir()
        header = inc / "width.vh"
        header.write_text("`define WIDTH 2\n")
        manifest, _, _ = self.experiment("configured", """`include "width.vh"
module demo #(parameter N=`WIDTH)(input [N-1:0] a,b, output [N-1:0] y1,y2);
assign y1 = a `OP b;
assign y2 = a `OP b;
endmodule
""", "-I", str(inc), "-D", "OP=&", "--param", "N=4")
        self.assertIn(str(header), {i["path"] for i in manifest["inputs"]})
        self.assertEqual(manifest["configuration"]["parameters"], ["N=4"])

    def test_source_and_output_paths_with_spaces(self):
        self.experiment("paths with spaces", (ROOT / "examples/merge.v").read_text())

    def test_nomux_option(self):
        _, trace, report = self.experiment("nomux", """module demo(input a,b,s, output y1,y2);
assign y1 = s ? a : b;
assign y2 = s ? a : b;
endmodule
""", "--nomux")
        self.assertEqual(report["counts"]["final_cells"], 2)
        self.assertEqual(len(trace.events), 0)

    def test_keep_attributes_preserved(self):
        tracefile = self.base / "kept.jsonl"
        script = self.base / "kept.ys"
        script.write_text(f'read_rtlil "{self.out / "frontend.il"}"\nsetattr -set keep 1 t:$and\n'
                          f'trace_begin "{tracefile}" kept\ntrace_opt_merge\ntrace_end\n')
        subprocess.run(["yosys", "-Q", "-T", "-m", str(ROOT / "build/provenance.so"), "-s", str(script)],
                       check=True, capture_output=True, text=True)
        trace = Trace(tracefile)
        self.assertEqual(len(trace.events), 0)
        self.assertEqual(sum(trace.objects[r]["kind"] == "cell" for r in trace.final), 2)

    def test_processes_fail_and_manifest_not_complete(self):
        with self.assertRaises(TraceError):
            self.experiment("sequential", "module demo(input clk,d,output reg q); always @(posedge clk) q <= d; endmodule")
        manifest = json.loads((self.base / "sequential/manifest.json").read_text())
        self.assertEqual(manifest["status"], "failed")
        with self.assertRaises(TraceError):
            load_run(self.base / "sequential")

    def test_hierarchy_is_rejected_explicitly(self):
        with self.assertRaises(TraceError):
            self.experiment("hierarchy", "module child(input a,output y); assign y=~a; endmodule\nmodule demo(input a,output y); child u(a,y); endmodule")

    def test_artifact_tampering_is_detected(self):
        dest = self.base / "tampered"
        shutil.copytree(self.out, dest)
        with (dest / "after.json").open("a") as stream:
            stream.write(" ")
        with self.assertRaisesRegex(TraceError, "hash mismatch"):
            load_run(dest)

    def test_existing_output_is_preserved(self):
        before = (self.out / "manifest.json").read_bytes()
        with self.assertRaisesRegex(TraceError, "already exists"):
            run(parser().parse_args(["run", "--source", str(self.source), "--top", "demo", "--out", str(self.out)]))
        self.assertEqual(before, (self.out / "manifest.json").read_bytes())

    def test_missing_events_versions_and_truncated_logs(self):
        records = self.trace.records
        cases = []
        cases.append(records[:-1])
        cases.append([r for r in records if r["record_type"] != "transform"])
        bad_input = copy.deepcopy(records)
        next(r for r in bad_input if r["record_type"] == "transform")["inputs"][0] = "missing@0"
        cases.append(bad_input)
        bad_version = copy.deepcopy(records)
        next(r for r in bad_version if r["record_type"] == "object" and r["version"] == 1)["version"] = 3
        cases.append(bad_version)
        bad_snapshot = copy.deepcopy(records)
        next(r for r in bad_snapshot if r["record_type"] == "snapshot" and r["phase"] == "final")["objects"].pop()
        cases.append(bad_snapshot)
        for i, case in enumerate(cases):
            with self.subTest(case=i):
                path = self.base / f"bad-{i}.jsonl"
                case = copy.deepcopy(case)
                for sequence, record in enumerate(case, 1):
                    record["sequence"] = sequence
                path.write_text("".join(json.dumps(r) + "\n" for r in case))
                with self.assertRaises(TraceError):
                    Trace(path)

    def test_uninstrumented_change_is_detected(self):
        tracefile = self.base / "unobserved.jsonl"
        script = self.base / "unobserved.ys"
        script.write_text(f'read_rtlil "{self.out / "frontend.il"}"\ntrace_begin "{tracefile}" unobserved\nopt_merge\ntrace_end\n')
        result = subprocess.run(["yosys", "-Q", "-T", "-m", str(ROOT / "build/provenance.so"), "-s", str(script)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unrecorded", result.stderr + result.stdout)
        with self.assertRaises(TraceError):
            Trace(tracefile)


if __name__ == "__main__":
    unittest.main()
