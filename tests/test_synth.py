"""Default synth endpoint, source-selection contracts and evidence validation.

ABC tests deliberately assert the conservative region contract, not exact
statement-to-gate attribution. Expected endpoints come from the RTL ports.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.extract import extract, state_cell
from yosys_flow.native import NativeTrace
from yosys_flow.runner import ROOT, load_run, query, run
from yosys_flow.trace import TraceError, unescape


@unittest.skipUnless(NATIVE.is_file(), "native Yosys not built; run make native")
class SynthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="yosys-synth-tests-")
        cls.base = Path(cls.temp.name)
        cls.source = cls.base / "process.v"
        cls.source.write_text((ROOT / "examples/proc.v").read_text())
        cls.out = cls.base / "full"
        cls.execute(cls.source, cls.out)
        cls.manifest, cls.trace, cls.validation = load_run(cls.out)
        cls.module = json.loads((cls.out / "after.json").read_text())["modules"]["demo"]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def execute(source, out, flow="synth"):
        return run(parser().parse_args(["run", "--flow", flow, "--native-yosys", str(NATIVE),
                                       "--source", str(source), "--top", "demo", "--out", str(out)]))

    def experiment(self, name, rtl, flow="synth"):
        source, out = self.base / (name + ".v"), self.base / name
        source.write_text(rtl)
        self.execute(source, out, flow)
        return source, out

    def select(self, line, source=None, out=None, *options):
        return query(parser().parse_args(["query", str(out or self.out), "--source", str(source or self.source),
                                         "--line", str(line), *options]))

    def export(self, name, line, source=None, out=None):
        target = self.base / "exports" / (name + ".v")
        result = extract(parser().parse_args(["extract", str(out or self.out), "--source", str(source or self.source),
                                             "--line", str(line), "--out", str(target)]))
        report = json.loads(target.with_suffix(".provenance.json").read_text())
        module = json.loads(target.with_suffix(".json").read_text())["modules"]["selected"]
        return result, report, module

    def test_actual_default_synth_and_post_abc_optimization(self):
        self.assertEqual(self.manifest["endpoint"], "synth -top demo")
        self.assertEqual(self.manifest["configuration"]["passes"], ["synth -top demo"])
        steps = [r for r in self.trace.records if r["record_type"] == "step_begin"]
        self.assertIn("techmap", [r["pass"] for r in steps])
        abc = next(r for r in steps if r["pass"] == "abc")
        self.assertTrue(any(r["pass"] == "opt" and r["seq"] > abc["seq"] for r in steps))
        self.assertTrue(all(c["type"].startswith("$_") for c in self.module["cells"].values()))
        self.assertTrue(all(self.validation["checks"].values()))
        self.assertFalse(self.validation["full_transformation_coverage"])
        self.assertTrue(self.manifest["tool"]["abc"])

    def test_abc_candidates_exclude_register_and_report_real_members(self):
        result = self.select(5)
        combinational = {n for n, c in self.module["cells"].items() if not state_cell(c)}
        self.assertEqual({unescape(c["name"]) for c in result["final_cells"]}, combinational)
        self.assertEqual({c["precision"] for c in result["final_cells"]}, {"abc_region_candidate"})
        self.assertFalse(result["cell_level_refs"])
        self.assertFalse(result["end_to_end_exact"])
        region = result["abc_regions"][0]
        self.assertTrue(region["input_ports"] and region["output_ports"])
        self.assertTrue(all(not state_cell({"type": self.trace.objects[r]["state"]["type"]}) for r in region["inputs"]))
        self.assertTrue(any(e["capture"] == "techmap" for e in result["edges"]))
        exported, report, mod = self.export("abc", 5)
        self.assertEqual(set(mod["cells"]), combinational)
        self.assertEqual(report["abc_regions"], result["abc_regions"])
        self.assertTrue(report["abc_evidence_artifacts"])
        self.assertFalse(report["end_to_end_exact"])
        self.assertTrue(exported["validation"]["exported_verilog_equivalent_to_cell_slice"])

    def test_final_ff_preserves_init_and_cuts_combinational_input(self):
        result = self.select(13)
        ff = [c for c in result["final_cells"] if c["type"].startswith("$_DFFE_")]
        self.assertEqual(len(ff), 1)
        self.assertEqual(len(result["final_cells"]), 1)
        self.assertEqual(self.module["cells"][unescape(ff[0]["name"])]["connections"]["Q"], self.module["ports"]["q"]["bits"])
        exported, _, mod = self.export("state", 13)
        self.assertEqual(len(mod["cells"]), 1)
        self.assertTrue(exported["validation"]["selected_state_init_preserved"])
        self.assertTrue(exported["validation"]["sequential_equivalence"])
        self.assertIn("1", [n["attributes"].get("init") for n in mod["netnames"].values()])

    def test_direct_and_constant_survive_clean_and_export_without_cells(self):
        for line, name in ((15, "direct"), (16, "zero")):
            result = self.select(line)
            self.assertEqual(result["resolution"], "connections_found")
            self.assertFalse(result["final_cells"])
            self.assertTrue(result["final_connections"][0]["revision"] > 0)
            self.assertEqual(result["final_connections"][0]["original_lhs"][0]["wire"], "\\" + name)
            exported, _, mod = self.export(name, line)
            self.assertFalse(mod["cells"])
            self.assertTrue(exported["validation"]["exported_verilog_equivalent_to_cell_slice"])

    def test_multibit_negative_edge_init_survives_default_synth(self):
        source, out = self.experiment("vector-state", """module demo(input clk, input [0:2] d, output reg [0:2] q);
initial q = 3'b110;
always @(negedge clk) q <= d;
endmodule
""")
        result = self.select(3, source, out)
        self.assertEqual(len(result["final_cells"]), 3)
        self.assertEqual({c["type"] for c in result["final_cells"]}, {"$_DFF_N_"})
        exported, _, mod = self.export("vector-state", 3, source, out)
        self.assertTrue(exported["validation"]["selected_state_init_preserved"])
        init = {bit: n["attributes"]["init"] for n in mod["netnames"].values()
                if "init" in n["attributes"] for bit in n["bits"]}
        self.assertEqual(sorted(init.values()), ["0", "1", "1"])

    def test_basic_optimization_constant_replacement_has_evidence(self):
        source, out = self.experiment("constant", """module demo(output reg y);
wire s = 1'b1;
always @* if (s) y = 1'b1; else y = 1'b0;
endmodule
""", "m1-opt")
        result = self.select(3, source, out)
        self.assertFalse(result["final_cells"])
        self.assertTrue(result["final_connections"])
        self.assertTrue(all(c["rhs_bits"] == ["1"] for c in result["final_connections"]))
        self.assertTrue(any(c["reason"] == "opt_expr" for c in result["retired_cells"]))
        exported, _, mod = self.export("constant", 3, source, out)
        self.assertFalse(mod["cells"])
        self.assertTrue(exported["validation"]["exported_verilog_equivalent_to_cell_slice"])

    def test_unused_expression_retirement_is_distinct_from_missing_source(self):
        source, out = self.experiment("dead", """module demo(input a, b, output y);
wire unused;
assign unused = a ^ b;
assign y = a & b;
endmodule
""", "m1-opt")
        result = self.select(3, source, out)
        self.assertFalse(result["final_cells"])
        self.assertFalse(result["final_connections"])
        self.assertTrue(any(c["reason"] == "opt_clean" for c in result["retired_cells"]))
        self.assertTrue(result["retired_connections"])
        self.assertFalse(result["empty_result_means_no_effect"])
        self.assertEqual({c["type"] for c in self.select(4, source, out)["final_cells"]}, {"$and"})

    def test_wide_arithmetic_has_alumacc_techmap_abc_path(self):
        source, out = self.experiment("arithmetic", """module demo(input [3:0] a,b,c, output [4:0] y);
wire [4:0] t;
assign t = a + b;
assign y = t + c;
endmodule
""")
        for line in (3, 4):
            result = self.select(line, source, out)
            self.assertTrue(result["final_cells"])
            self.assertEqual({c["precision"] for c in result["final_cells"]}, {"abc_region_candidate"})
            self.assertTrue({"alumacc", "techmap", "abc_region"} <= {e["capture"] for e in result["edges"]})
        exported, _, mod = self.export("arithmetic", 3, source, out)
        self.assertGreater(len(mod["cells"]), 1)
        self.assertTrue(exported["validation"]["exported_verilog_equivalent_to_cell_slice"])

    def test_independent_outputs_share_only_the_reported_abc_region(self):
        source, out = self.experiment("independent", """module demo(input a,b,c,d, output y,z);
assign y = a & b;
assign z = c ^ d;
endmodule
""")
        result = self.select(2, source, out)
        # Default ABC may process disconnected logic in the same invocation.
        # Its conservative region includes both outputs and must say so.
        self.assertEqual(len(result["final_cells"]), 2)
        self.assertEqual({c["precision"] for c in result["final_cells"]}, {"abc_region_candidate"})
        self.assertFalse(result["end_to_end_exact"])
        self.assertTrue(result["abc_regions"])

    def test_abc_reverse_query_keeps_all_region_sources(self):
        forward = self.select(5)
        ref = forward["final_cells"][0]["ref"]
        reverse = query(parser().parse_args(["query", str(self.out), "--object", ref]))
        self.assertEqual([r["ref"] for r in reverse["final_cells"]], [ref])
        self.assertTrue({5, 7} <= {s["line"] for s in reverse["sources"]})
        self.assertEqual(reverse["association_precision"], ["abc_region_candidate"])

    def test_forged_abc_members_precision_and_connection_revision_rejected(self):
        mutations = []
        rows = copy.deepcopy(self.trace.records)
        next(r for r in rows if r["record_type"] == "abc_region")["inputs"] = []
        mutations.append(rows)
        rows = copy.deepcopy(self.trace.records)
        next(r for r in rows if r["record_type"] == "cell_create" and r.get("abc_invocation"))["precision"] = "cell"
        mutations.append(rows)
        rows = copy.deepcopy(self.trace.records)
        next(r for r in rows if r["record_type"] == "cell_state" and r["precision"] == "abc_region_candidate")["precision"] = "cell"
        mutations.append(rows)
        rows = copy.deepcopy(self.trace.records)
        next(r for r in rows if r["record_type"] == "connection_state")["revision"] += 1
        mutations.append(rows)
        for i, rows in enumerate(mutations):
            path = self.base / f"forged-{i}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            with self.assertRaises(TraceError):
                NativeTrace(path)

    def test_abc_artifact_boundary_and_manifest_hash_checked(self):
        import shutil
        target = self.base / "tampered-run"
        shutil.copytree(self.out, target)
        region = self.trace.regions[0]
        path = target / region["artifacts"]["input.blif"]
        path.write_text(path.read_text().replace(".inputs ", ".inputs bogus ", 1))
        with self.assertRaisesRegex(TraceError, "boundary differs"):
            self.trace.compare_abc_artifacts(target)
        with self.assertRaisesRegex(TraceError, "artifact hash mismatch"):
            load_run(target)

    def test_internal_mapping_ast_is_not_registered_as_user_source(self):
        self.assertEqual({Path(s["file"]).resolve() for s in self.trace.sources.values()}, {self.source.resolve()})
        self.assertFalse({"untracked_cell_removal", "cell_missing_from_lifecycle", "cell_created_without_semantic_source_binding"}.intersection(self.trace.gaps))


if __name__ == "__main__":
    unittest.main()
