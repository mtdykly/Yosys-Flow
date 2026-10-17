"""Action/branch selection expectations are independent of recorded origin sets."""
import copy
import json
from pathlib import Path
import tempfile
import subprocess
import unittest

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import TraceError, token
from yosys_flow.native import NativeTrace
from yosys_flow.runner import run, query, load_run
from yosys_flow.extract import extract


@unittest.skipUnless(NATIVE.is_file(), "native Yosys not built; run make test")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yosys-process-tests-")
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def experiment(self, rtl, flow="m1", name="case"):
        source, out = self.base / (name + ".v"), self.base / name
        source.write_text(rtl)
        run(parser().parse_args(["run", "--source", str(source), "--top", "demo", "--out", str(out),
                                 "--native-yosys", str(NATIVE), "--flow", flow]))
        return source, out

    def select(self, fixture, line):
        source, out = fixture
        return query(parser().parse_args(["query", str(out), "--source", str(source), "--line", str(line)]))

    def export(self, fixture, line, name):
        source, out = fixture
        target = self.base / "exports" / (name + ".v")
        result = extract(parser().parse_args(["extract", str(out), "--source", str(source), "--line", str(line), "--out", str(target)]))
        return result, json.loads(target.with_suffix(".provenance.json").read_text())

    def test_nested_branch_excludes_sibling_mux_and_separates_control(self):
        fixture = self.experiment("""module demo(input s,t,a,b,c, output reg y);
always @* begin
  if (s) begin
    if (t)
      y = a;
    else
      y = b;
  end else
    y = c;
end
endmodule
""")
        a, b, c = [self.select(fixture, line) for line in (5, 7, 9)]
        module = json.loads((fixture[1] / "after.json").read_text())["modules"]["demo"]
        # proc -noopt retains the intermediate s ? inner : x mux as well.
        expected = set(module["cells"])
        self.assertEqual(len(expected), 3)
        self.assertTrue(all(cell["type"] == "$mux" for cell in module["cells"].values()))
        for result in (a, b):
            self.assertEqual({r["name"] for r in result["final_cells"]}, expected)
            self.assertEqual({r["precision"] for r in result["final_cells"]}, {"process_action"})
        self.assertEqual([r["type"] for r in c["final_cells"]], ["$mux"])
        self.assertTrue(set(r["ref"] for r in c["final_cells"]) < set(r["ref"] for r in a["final_cells"]))
        outer = self.select(fixture, 3)
        self.assertEqual({r["name"] for r in outer["final_cells"]},
                         {name for name, cell in module["cells"].items() if cell["connections"]["S"] == module["ports"]["s"]["bits"]})
        outer_mux = c["final_cells"][0]["name"]
        self.assertEqual(module["cells"][outer_mux]["connections"]["Y"], module["ports"]["y"]["bits"])
        self.assertTrue(any(e.get("role") == "control" for e in outer["edges"]))
        self.assertTrue(any(e.get("role") == "data" for e in a["edges"]))
        exported, report = self.export(fixture, 9, "outer")
        self.assertEqual(exported["selected_cell_count"], 1)
        self.assertTrue(report["process_proof_nodes"])
        self.assertFalse(report["end_to_end_exact"])

    def test_overwritten_action_is_not_rebound_to_later_value(self):
        rtl = """module demo(input a,b,c,d, output reg y);
always @* begin
  y = a & b;
  y = c ^ d;
end
endmodule
"""
        for flow in ("m1-opt", "synth"):
            with self.subTest(flow=flow):
                fixture = self.experiment(rtl, flow, flow)
                dead, live = self.select(fixture, 3), self.select(fixture, 4)
                self.assertFalse(dead["final_cells"])
                self.assertFalse(dead["final_connections"])
                self.assertTrue(any(r["reason"] == "frontend_overwritten" for r in dead["process_discards"]))
                self.assertTrue(live["final_cells"])
                self.assertTrue(all(3 not in {s["line"] for s in c["all_origins"]} for c in live["final_cells"]))
                self.assertTrue(self.export(fixture, 4, flow)[0]["validation"]["exported_verilog_equivalent_to_cell_slice"])

    def test_partial_overwrite_preserves_only_surviving_target_bits(self):
        fixture = self.experiment("""module demo(input [3:0] a, input [1:0] b, output reg [3:0] y);
always @* begin
  y = a;
  y[1:0] = b;
end
endmodule
""")
        whole, low = self.select(fixture, 3), self.select(fixture, 4)
        mod = json.loads((fixture[1] / "after.json").read_text())["modules"]["demo"]
        self.assertFalse(whole["final_cells"])
        self.assertEqual({b for r in whole["final_connections"] for b in r["lhs_bits"]}, set(mod["ports"]["y"]["bits"][2:]))
        self.assertEqual({b for r in low["final_connections"] for b in r["lhs_bits"]}, set(mod["ports"]["y"]["bits"][:2]))
        self.assertEqual(self.export(fixture, 3, "high")[0]["output_bits"], 2)
        self.assertEqual(self.export(fixture, 4, "low")[0]["output_bits"], 2)

    def test_default_assignment_remains_only_in_unassigned_branch(self):
        fixture = self.experiment("""module demo(input s,a,b, output reg y);
always @* begin
  y = a;
  if (s)
    y = b;
end
endmodule
""")
        a, b = self.select(fixture, 3), self.select(fixture, 5)
        self.assertEqual([c["type"] for c in a["final_cells"]], ["$mux"])
        self.assertEqual([c["ref"] for c in a["final_cells"]], [c["ref"] for c in b["final_cells"]])
        self.assertTrue(self.export(fixture, 3, "default")[0]["validation"]["exported_verilog_equivalent_to_cell_slice"])


    def test_independent_bit_conditions_do_not_cross_select(self):
        fixture = self.experiment("""module demo(input s,t,a,b,c,d, output reg [1:0] y);
always @* begin
  if (s) y[0] = a; else y[0] = b;
  if (t) y[1] = c; else y[1] = d;
end
endmodule
""")
        results = [self.select(fixture, line) for line in (3, 4)]
        self.assertTrue(all(len(r["final_cells"]) == 1 for r in results))
        self.assertNotEqual(results[0]["final_cells"][0]["ref"], results[1]["final_cells"][0]["ref"])
        for line in (3, 4):
            self.assertEqual(self.export(fixture, line, str(line))[0]["selected_cell_count"], 1)

    def test_case_priority_discards_unreachable_assignment(self):
        fixture = self.experiment("""module demo(input s,a,b,c, output reg y);
always @* case (s)
  1'b0: y = a;
  1'b0: y = b;
  default: y = c;
endcase
endmodule
""")
        dead = self.select(fixture, 4)
        self.assertFalse(dead["final_cells"])
        self.assertFalse(dead["final_connections"])
        self.assertTrue(any(r["reason"] == "proc_rmdead_unreachable" for r in dead["process_discards"]))
        self.assertTrue(self.select(fixture, 3)["final_cells"])
        self.assertTrue(self.export(fixture, 5, "case")[0]["validation"]["exported_verilog_equivalent_to_cell_slice"])

    def test_parallel_cases_share_data_mux_but_not_comparators(self):
        fixture = self.experiment("""module demo(input [1:0] s, input a,b,c, output reg y);
always @* case (s)
  2'b00: y = a;
  2'b01: y = b;
  default: y = c;
endcase
endmodule
""")
        mod = json.loads((fixture[1] / "after.json").read_text())["modules"]["demo"]
        result = self.select(fixture, 5)
        expected = {n for n, c in mod["cells"].items() if c["type"] == "$pmux"}
        self.assertEqual(len(expected), 1)
        self.assertEqual({c["name"] for c in result["final_cells"]}, expected)
        exported, report = self.export(fixture, 5, "parallel")
        self.assertEqual(exported["selected_cell_count"], 1)
        self.assertEqual(report["pmux_semantics"]["multiple_active_selects"], "undefined")
        self.assertTrue(report["pmux_semantics"]["undefined_values_modelled"])

    def test_default_synth_keeps_process_evidence_and_abc_candidate_precision(self):
        fixture = self.experiment("""module demo(input s,t,a,b,c, output reg y);
always @* if (s) begin
  if (t) y=a; else y=b;
end else y=c;
endmodule
""", "synth")
        result = self.select(fixture, 4)
        self.assertTrue(result["final_cells"])
        self.assertEqual({c["precision"] for c in result["final_cells"]}, {"abc_output_cone_candidate", "abc_internal_cut"})
        self.assertFalse(result["end_to_end_exact"])
        self.assertTrue(any(e["capture"] == "proc_mux" for e in result["edges"]))
        self.assertTrue(result["process_actions"])
        exported, report = self.export(fixture, 4, "abc")
        self.assertTrue(report["process_proof_nodes"])
        self.assertTrue(report["abc_regions"])
        self.assertTrue(exported["validation"]["exported_verilog_equivalent_to_cell_slice"])

    def test_auxiliary_design_does_not_register_user_process_gaps(self):
        source, auxiliary = self.base / "main.v", self.base / "auxiliary.v"
        source.write_text("module demo(input a,b, output y); assign y=a&b; endmodule\n")
        # techmap runs proc on its separate mapping design. Unlike design -push,
        # this does not replace the tracked user design or clone its objects.
        auxiliary.write_text("""(* techmap_celltype = "$and" *)
module helper #(parameter A_SIGNED=0, B_SIGNED=0, A_WIDTH=1, B_WIDTH=1, Y_WIDTH=1)
(input [A_WIDTH-1:0] A, input [B_WIDTH-1:0] B, output reg [Y_WIDTH-1:0] Y);
always @* if (A) Y=B; else Y=0;
endmodule
""")
        tracefile, netlist = self.base / "auxiliary.jsonl", self.base / "after.json"
        commands = (f"read_verilog {token(source)}\nhierarchy -top demo\n"
                    f"techmap -autoproc -map {token(auxiliary)}\nopt_merge\n")
        script = self.base / "auxiliary.ys"
        script.write_text(f"flow_start {token(tracefile)} aux\n" + commands + f"write_json {token(netlist)}\nflow_stop\n")
        subprocess.run([str(NATIVE), "-Q", "-T", "-s", str(script)], check=True, capture_output=True, text=True)
        trace = NativeTrace(tracefile)
        trace.compare_netlist(netlist)
        self.assertFalse(trace.process_nodes)
        self.assertNotIn("untracked_process_action", trace.gaps)
        self.assertNotIn(str(auxiliary), {r["file"] for r in trace.sources.values()})
        baseline = self.base / "baseline.json"
        script.write_text(commands + f"write_json {token(baseline)}\n")
        subprocess.run(["yosys", "-Q", "-T", "-s", str(script)], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(netlist.read_text())["modules"], json.loads(baseline.read_text())["modules"])


    def test_tampered_process_proof_is_rejected(self):
        fixture = self.experiment("""module demo(input s,a,b, output reg y);
always @* if (s) y=a; else y=b;
endmodule
""")
        _, trace, _ = load_run(fixture[1])
        mutations = []
        rows = copy.deepcopy(trace.records)
        next(r for r in rows if r["record_type"] == "process_binding")["inputs"] = ["native:process_999999"]
        mutations.append(rows)
        rows = copy.deepcopy(trace.records)
        next(r for r in rows if r["record_type"] == "process_node" and r["origins"])["origins"] = []
        mutations.append(rows)
        rows = copy.deepcopy(trace.records)
        next(r for r in rows if r["record_type"] == "process_action_detail")["bits"].pop()
        mutations.append(rows)
        for i, rows in enumerate(mutations):
            path = self.base / f"corrupt-{i}.jsonl"
            path.write_text("".join(json.dumps(row)+"\n" for row in rows))
            with self.subTest(i=i), self.assertRaises(TraceError):
                NativeTrace(path)
