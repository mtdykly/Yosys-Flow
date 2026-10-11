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
from yosys_flow.runner import ROOT, load_run, query, run
from yosys_flow.trace import TraceError

NATIVE = Path(os.environ.get("YOSYS_FLOW_NATIVE", ROOT / "build/native/install/bin/yosys"))


@unittest.skipUnless(NATIVE.is_file(), "native Yosys not built; run make test-native")
class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="yosys-native-tests-")
        cls.base = Path(cls.temp.name)
        cls.source = cls.base / "process with spaces.v"
        cls.source.write_text((ROOT / "examples/proc.v").read_text())
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
        for line, operator, exclude in ((5, "$and", "$or"), (7, "$or", "$and")):
            result = self.select(line)
            self.assertEqual({r["type"] for r in result["final_cells"]}, {operator, "$mux"})
            self.assertEqual(len(result["final_cells"]), 2)
            mux = next(r for r in result["final_cells"] if r["type"] == "$mux")
            self.assertEqual(self.module["cells"][mux["name"]]["connections"]["Y"], self.module["ports"]["y"]["bits"])
            self.assertEqual(mux["precision"], "process_signal_candidate")
            self.assertFalse(result["end_to_end_exact"])
            self.assertTrue(result["edges"])
            _, _, report, module = self.export("branch" + str(line), line)
            self.assertNotIn(exclude, {c["type"] for c in module["cells"].values()})
            self.assertEqual(sum(p["direction"] == "output" for p in report["boundary"]), 1)

    def test_simple_ff_reset_enable_init_and_state_export(self):
        result = self.select(13)
        self.assertEqual({r["type"] for r in result["final_cells"]}, {"$adff", "$mux", "$reduce_or"})
        self.assertNotIn("$and", {r["type"] for r in result["final_cells"]})
        target, _, report, module = self.export("state", 13)
        ff = next(c for c in module["cells"].values() if c["type"] == "$adff")
        original = next(c for c in self.module["cells"].values() if c["type"] == "$adff")
        self.assertEqual(ff, original)
        self.assertTrue(report["contains_state"])
        self.assertTrue(report["validation"]["selected_state_init_preserved"])
        init = {n["bits"][0]: n["attributes"]["init"] for n in module["netnames"].values() if "init" in n["attributes"]}
        self.assertEqual(init[ff["connections"]["Q"][0]], "1")
        self.assertIn("posedge", target.read_text())

    def test_initial_statement_and_clock_reach_state(self):
        for line in (2, 9):
            result = self.select(line)
            self.assertIn("$adff", {r["type"] for r in result["final_cells"]})
            self.assertNotIn("$and", {r["type"] for r in result["final_cells"]})

    def test_connection_only_and_constant_export(self):
        for line, count in ((15, 1), (16, 0)):
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

    def test_two_registers_in_one_process_do_not_mix(self):
        source, out = self.experiment("registers", """module demo(input clk,a,b, output reg q,r);
always @(negedge clk) begin
  q <= a;
  r <= b;
end
endmodule
""")
        result = self.select(3, source, out)
        self.assertEqual([r["type"] for r in result["final_cells"]], ["$dff"])
        module = json.loads((out / "after.json").read_text())["modules"]["demo"]
        cell = module["cells"][result["final_cells"][0]["name"]]
        self.assertEqual(cell["connections"]["Q"], module["ports"]["q"]["bits"])
        self.assertEqual(int(cell["parameters"]["CLK_POLARITY"], 2), 0)
        _, _, _, exported = self.export("negative_clock", 3, source, out)
        self.assertFalse(any("init" in n["attributes"] for n in exported["netnames"].values()))

    def test_unselected_ff_is_external_state_input(self):
        source, out = self.experiment("external_state", """module demo(input clk,d,a, output reg q, output y);
always @(posedge clk) q <= d;
assign y = q & a;
endmodule
""")
        self.assertEqual([r["type"] for r in self.select(3, source, out)["final_cells"]], ["$and"])
        _, _, report, module = self.export("external_state", 3, source, out)
        self.assertFalse(report["contains_state"])
        self.assertTrue(any(p.get("external_state_drivers") for p in report["boundary"]))
        self.assertEqual({c["type"] for c in module["cells"].values()}, {"$and"})

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
        source, out = self.experiment("merge", (ROOT / "examples/merge.v").read_text())
        first, second = self.select(2, source, out), self.select(3, source, out)
        self.assertEqual(first["final_cells"][0]["ref"], second["final_cells"][0]["ref"])
        ref = first["final_cells"][0]["ref"]
        reverse = query(parser().parse_args(["query", str(out), "--object", ref]))
        self.assertEqual({r["line"] for r in reverse["sources"]}, {2, 3})
        self.assertEqual({r["name"] for r in first["final_cells"][0]["consumers"]}, {"y1", "y2"})
        self.assertTrue(any(r["record_type"] == "merge" for r in first["events"]))

    def test_init_bus_bit_order(self):
        source, out = self.experiment("init_bits", """module demo(input clk, input [2:0] d, output reg [0:2] q);
initial q = 3'b110;
always @(posedge clk) q <= d;
endmodule
""")
        _, _, _, module = self.export("init_bits", 3, source, out)
        ff = next(c for c in module["cells"].values() if c["type"] == "$dff")
        values = {n["bits"][0]: n["attributes"]["init"] for n in module["netnames"].values() if "init" in n["attributes"]}
        self.assertEqual([values[b] for b in ff["connections"]["Q"]], list("011"))

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

    def test_unsupported_hierarchy_latch_memory_fail_closed(self):
        for name, rtl in (
            ("hierarchy", "module child(input a,output y);assign y=a;endmodule\nmodule demo(input a,output y);child c(a,y);endmodule"),
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


if __name__ == "__main__":
    unittest.main()
