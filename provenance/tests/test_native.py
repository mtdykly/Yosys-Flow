"""Real native/frontend/proc tests. Expected source selections are independent
of the recorder: assert cell operators and the original RTL output bits.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from yosys_flow.cli import parser
from yosys_flow.extract import extract
from yosys_flow.native import NativeTrace
from yosys_flow.common import ROOT, PROVENANCE_ROOT, TraceError, token
from yosys_flow.runner import load_run, query, run

NATIVE = Path(os.environ.get("YOSYS_FLOW_NATIVE", ROOT / "build/native/install/bin/yosys"))


@unittest.skipUnless(NATIVE.is_file(), "native Yosys not built; run make test-native")
class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="yosys-native-tests-")
        cls.base = Path(cls.temp.name)
        cls.source = cls.base / "process with spaces.v"
        cls.source.write_text((PROVENANCE_ROOT / "examples/comb.v").read_text())
        cls.out = cls.base / "base"
        cls.execute(cls.source, cls.out)
        cls.manifest, cls.trace, cls.validation = load_run(cls.out)
        cls.module = json.loads((cls.out / "after.json").read_text())["modules"]["demo"]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def execute(source, out, *extra):
        return run(parser().parse_args(["run", "--flow", "m1", "--native-yosys", str(NATIVE),
                                       "--source", str(source), "--top", "demo", "--out", str(out), *extra]))

    def experiment(self, name, rtl, *extra):
        source, out = self.base / (name + ".v"), self.base / name
        source.write_text(rtl)
        self.execute(source, out, *extra)
        return source, out

    def select(self, line, source=None, out=None, *extra):
        return query(parser().parse_args(["query", str(out or self.out), "--source", str(source or self.source),
                                         "--line", str(line), *extra]))

    def export(self, name, line, source=None, out=None):
        target = self.base / "exports" / (name + ".v")
        result = extract(parser().parse_args(["extract", str(out or self.out), "--source", str(source or self.source),
                                             "--line", str(line), "--out", str(target)]))
        report = json.loads(target.with_suffix(".provenance.json").read_text())
        module = json.loads(target.with_suffix(".json").read_text())["modules"]["selected"]
        return target, result, report, module

    def test_stock_match_nested_steps_and_real_endpoint(self):
        self.assertTrue(all(self.validation["checks"].values()))
        self.assertFalse(self.validation["full_transformation_coverage"])
        self.assertFalse(self.validation["exact_proc_cell_attribution"])
        self.assertEqual(self.manifest["endpoint"], "proc -noopt; opt_merge")
        steps = [r for r in self.trace.records if r["record_type"] == "step_begin"]
        proc = next(r["step_id"] for r in steps if r["pass"] == "proc")
        self.assertTrue(any(r["parent_step_id"] == proc and r["pass"] == "proc_mux" for r in steps))
        self.assertEqual(self.trace.gaps, [])

    def test_branch_operator_and_mux_excludes_other_branch_operator(self):
        for line, operator, exclude in ((4, "$and", "$or"), (6, "$or", "$and")):
            result = self.select(line)
            self.assertEqual({r["type"] for r in result["final_cells"]}, {operator, "$mux"})
            self.assertEqual(len(result["final_cells"]), 2)
            mux = next(r for r in result["final_cells"] if r["type"] == "$mux")
            self.assertEqual(self.module["cells"][mux["name"]]["connections"]["Y"], self.module["ports"]["y"]["bits"])
            self.assertEqual(mux["precision"], "process_action")
            self.assertFalse(result["end_to_end_exact"])
            self.assertTrue(result["edges"])
            _, _, report, module = self.export("branch" + str(line), line)
            self.assertNotIn(exclude, {c["type"] for c in module["cells"].values()})
            self.assertEqual(sum(p["direction"] == "output" for p in report["boundary"]), 1)


    def test_connection_only_and_constant_export(self):
        for line, count in ((8, 1), (9, 0)):
            result = self.select(line)
            self.assertEqual(result["final_cells"], [])
            self.assertEqual(result["resolution"], "connections_found")
            self.assertEqual(len(result["final_connections"]), 1)
            _, output, report, module = self.export("connection" + str(line), line)
            self.assertEqual(output["input_bits"], count)
            self.assertEqual(output["output_bits"], 1)
            self.assertEqual(module["cells"], {})
            self.assertTrue(report["selected_connections"])

    def test_combinational_always_passthrough_has_connection(self):
        source, out = self.experiment("always_alias", "module demo(input a, output reg y);\nalways @* y = a;\nendmodule\n")
        result = self.select(2, source, out)
        self.assertEqual(result["resolution"], "connections_found")
        self.assertEqual(result["final_cells"], [])
        _, output, _, _ = self.export("always_alias", 2, source, out)
        self.assertEqual((output["input_bits"], output["output_bits"]), (1, 1))

    def test_same_process_other_target_excluded(self):
        source, out = self.experiment("targets", """module demo(input s,a,b, output reg y,z);
always @* begin
  if (s) begin
    y = a & b;
    z = a ^ b;
  end else begin
    y = 0;
    z = 1;
  end
end
endmodule
""")
        module = json.loads((out / "after.json").read_text())["modules"]["demo"]
        result = self.select(4, source, out)
        self.assertEqual({r["type"] for r in result["final_cells"]}, {"$and", "$mux"})
        self.assertEqual(len(result["final_cells"]), 2)
        mux = next(r for r in result["final_cells"] if r["type"] == "$mux")
        self.assertEqual(module["cells"][mux["name"]]["connections"]["Y"], module["ports"]["y"]["bits"])


    def test_columns_and_two_stage_cut(self):
        source, out = self.experiment("columns", """module demo(input a,b,c, output t,y);
assign t = a & b; assign y = t ^ c;
endmodule
""")
        result = self.select(2, source, out, "--column", "30")
        self.assertEqual([r["type"] for r in result["final_cells"]], ["$xor"])
        args = parser().parse_args(["extract", str(out), "--source", str(source), "--line", "2", "--column", "30",
                                   "--out", str(self.base / "exports/column.v")])
        report = extract(args)
        self.assertEqual(report["selected_cell_count"], 1)

    def test_merge_shared_sources_reverse_and_consumers(self):
        source, out = self.experiment("merge", (PROVENANCE_ROOT / "examples/merge.v").read_text())
        first, second = self.select(2, source, out), self.select(3, source, out)
        self.assertEqual(first["final_cells"][0]["ref"], second["final_cells"][0]["ref"])
        ref = first["final_cells"][0]["ref"]
        reverse = query(parser().parse_args(["query", str(out), "--object", ref]))
        self.assertEqual({r["line"] for r in reverse["sources"]}, {2, 3})
        self.assertEqual({r["name"] for r in first["final_cells"][0]["consumers"]}, {"y1", "y2"})
        self.assertTrue(any(r["record_type"] == "merge" for r in first["events"]))


    def test_column_inside_nested_expression_excludes_enclosing_operator(self):
        source, out = self.experiment("nested_expression", """module demo(input a,b,c, output y);
assign y = (a & b) ^ c;
endmodule
""")
        result = self.select(2, source, out, "--column", "14")
        self.assertEqual([r["type"] for r in result["final_cells"]], ["$and"])
        self.assertEqual(result["final_connections"], [])
        report = extract(parser().parse_args(["extract", str(out), "--source", str(source), "--line", "2",
                                             "--column", "14", "--out", str(self.base / "exports/nested.v")]))
        self.assertEqual(report["input_bits"], 2)
        self.assertEqual(report["output_bits"], 1)

    def test_include_inputs_and_changed_base_coordinates(self):
        header = self.base / "config.vh"
        header.write_text("`define VALUE 1'b1\n")
        source, out = self.experiment("included", '`include "config.vh"\nmodule demo(output y);\nassign y = `VALUE;\nendmodule\n', "-I", str(self.base))
        manifest, _, _ = load_run(out)
        self.assertIn(str(header), {r["path"] for r in manifest["inputs"]})
        header.write_text("`define VALUE 1'b0\n")
        self.assertIn(str(header), self.select(3, source, out)["changed_or_missing_base_inputs"])

    def test_unsupported_latch_memory_fail_closed(self):
        for name, rtl in (
            ("latch", "module demo(input en,d,output reg q);always @* if(en) q=d;endmodule"),
            ("memory", "module demo(input clk,a,d,output y);reg mem[0:1];always @(posedge clk) mem[a]<=d;assign y=mem[a];endmodule"),
        ):
            with self.subTest(name=name), self.assertRaises(TraceError):
                self.experiment(name, rtl)
            manifest = json.loads((self.base / name / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            with self.assertRaises(TraceError):
                load_run(self.base / name)

    def test_tampered_source_propagation_and_truncated_trace_rejected(self):
        rows = copy.deepcopy(self.trace.records)
        row = next(r for r in rows if r["record_type"] == "cell_state")
        row["origins"] = []
        target = self.base / "bad.jsonl"
        target.write_text("\n".join(map(json.dumps, rows)) + "\n")
        with self.assertRaisesRegex(TraceError, "source propagation"):
            NativeTrace(target)
        target.write_text("\n".join(map(json.dumps, self.trace.records[:-1])) + "\n")
        with self.assertRaisesRegex(TraceError, "incomplete"):
            NativeTrace(target)

    def test_partially_instrumented_native_pass_is_reported(self):
        target = self.base / "unsupported.jsonl"
        script = self.base / "unsupported.ys"
        script.write_text(f'flow_start "{target}" manual\nread_verilog "{self.source}"\n'
                          'hierarchy -top demo\nproc -noopt\nopt_expr\nflow_stop\n')
        result = subprocess.run([str(NATIVE), "-Q", "-T", "-s", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = NativeTrace(target)
        self.assertIn("partial_pass:opt_expr", trace.gaps)


    def test_commutative_multibit_cascade_keeps_all_merge_sources(self):
        source, out = self.experiment("cascade", """module demo(input [3:0] a,b,c, output [3:0] y1,y2,y3);
wire [3:0] x1 = a & b;
wire [3:0] x2 = b & a;
wire [3:0] x3 = a & b;
assign y1 = x1 ^ c;
assign y2 = x2 ^ c;
assign y3 = x3 ^ c;
endmodule
""")
        _, trace, _ = load_run(out)
        self.assertEqual(len(trace.final), 2)
        self.assertEqual(sum(r["record_type"] == "merge" for r in trace.records), 4)
        for lines in ((2, 3, 4), (5, 6, 7)):
            refs = [self.select(line, source, out)["final_cells"][0]["ref"] for line in lines]
            self.assertEqual(len(set(refs)), 1)
            reverse = query(parser().parse_args(["query", str(out), "--object", refs[0]]))
            self.assertEqual({r["line"] for r in reverse["sources"]}, set(lines))

    def test_user_keep_repeated_merge_and_module_rename(self):
        source = self.base / "kept.v"
        source.write_text((PROVENANCE_ROOT / "examples/merge.v").read_text())
        tracefile, netlist = self.base / "kept.jsonl", self.base / "kept.json"
        commands = (f"read_verilog {token(source)}\nhierarchy -top demo\nproc -noopt\n"
                    "setattr -set keep 1 t:$and\nopt_merge\nopt_merge\nrename demo renamed\n")
        script = self.base / "kept.ys"
        script.write_text(f"flow_start {token(tracefile)} kept\n{commands}write_json {token(netlist)}\nflow_stop\n")
        subprocess.run([str(NATIVE), "-Q", "-T", "-s", str(script)], check=True, capture_output=True, text=True)
        trace = NativeTrace(tracefile)
        trace.compare_netlist(netlist)
        self.assertEqual(len(trace.final), 2)
        self.assertFalse(any(r["record_type"] == "merge" for r in trace.records))
        steps = [r["step_id"] for r in trace.records if r["record_type"] == "step_begin" and r["pass"] == "opt_merge"]
        self.assertEqual(len(steps), 2)
        self.assertEqual(len(set(steps)), 2)
        baseline = self.base / "kept-baseline.json"
        script.write_text(commands + f"write_json {token(baseline)}\n")
        subprocess.run(["yosys", "-Q", "-T", "-s", str(script)], check=True, capture_output=True, text=True)
        actual = json.loads(netlist.read_text())["modules"]
        self.assertEqual(actual, json.loads(baseline.read_text())["modules"])
        self.assertTrue(all(int(c["attributes"]["keep"], 2) == 1 for c in actual["renamed"]["cells"].values()))
        self.assertTrue(any(r["record_type"] == "connection_state" and r["reason"] == "module_renamed" for r in trace.records))

    def test_corrupt_native_lifecycle_versions_and_step_parent_rejected(self):
        cases = []
        for field, value in (("input", "missing@0"), ("ref", "native:cell_1@9000")):
            rows = copy.deepcopy(self.trace.records)
            next(r for r in rows if r["record_type"] == "cell_state")[field] = value
            cases.append(rows)
        rows = copy.deepcopy(self.trace.records)
        rows[-1]["cells"].pop()
        cases.append(rows)
        rows = copy.deepcopy(self.trace.records)
        next(r for r in rows if r["record_type"] == "step_begin")["parent_step_id"] = 9000
        cases.append(rows)
        for index, rows in enumerate(cases):
            path = self.base / f"bad-lifecycle-{index}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            with self.subTest(index=index), self.assertRaises(TraceError):
                NativeTrace(path)


if __name__ == "__main__":
    unittest.main()
