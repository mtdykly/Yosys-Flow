"""Real Yosys experiments and adversarial provenance validation; no mock optimizer."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from yosys_flow.cli import parser
from yosys_flow.extract import extract, slice_module
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
        for phase in ("before", "after"):
            self.assertEqual([r["step_id"] for r in self.trace.snapshots if r["phase"] == phase], steps)

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

    def export(self, name, directory=None, source=None, *options):
        out = self.base / "exports" / (name + ".v")
        args = parser().parse_args(["extract", str(directory or self.out), "--source", str(source or self.source),
                                   "--line", "2", "--out", str(out), *options])
        result = extract(args)
        return result, json.loads(out.with_suffix(".json").read_text()), json.loads(out.with_suffix(".provenance.json").read_text())

    def test_export_shared_gate_keeps_origins_and_aliases(self):
        result, netlist, report = self.export("shared")
        self.assertEqual(result["selected_cell_count"], 1)
        self.assertEqual((result["input_bits"], result["output_bits"]), (2, 1))
        self.assertTrue(all(result["validation"].values()))
        self.assertEqual(len(report["selected_cells"][0]["all_origins"]), 2)
        output = next(p for p in report["boundary"] if p["direction"] == "output")
        self.assertEqual({p["name"] for p in output["external_users"]}, {"y1", "y2"})
        original = json.loads((self.out / "after.json").read_text())["modules"]["demo"]["cells"]
        self.assertEqual(netlist["modules"]["selected"]["cells"], original)

    def test_export_cuts_upstream_and_keeps_constants(self):
        rtl = """module demo(input [3:0] a,b, output [3:0] y);
wire [3:0] t = a & b;
assign y = t ^ 4'b0101;
endmodule
"""
        self.experiment("cut", rtl)
        result, netlist, report = self.export("cut", self.base / "cut", self.base / "cut.v", "--line", "3")
        self.assertEqual(result["selected_cell_count"], 1)
        self.assertEqual((result["input_bits"], result["output_bits"]), (4, 4))
        cell = next(iter(netlist["modules"]["selected"]["cells"].values()))
        self.assertEqual(cell["type"], "$xor")
        self.assertEqual(cell["connections"]["B"], ["1", "0", "1", "0"])
        self.assertTrue(all(p["external_drivers"][0]["kind"] == "cell" for p in report["boundary"] if p["direction"] == "input"))
        self.assertFalse(report["upstream_cone_included"])

    def test_export_range_preserves_internal_connections(self):
        self.experiment("internal", """module demo(input [3:0] a,b, output [3:0] y);
wire [3:0] t = a & b;
assign y = ~t;
endmodule
""")
        result, netlist, report = self.export("internal", self.base / "internal", self.base / "internal.v", "--end-line", "3")
        self.assertEqual(result["selected_cell_count"], 2)
        self.assertEqual((result["input_bits"], result["output_bits"]), (8, 4))
        cells = list(netlist["modules"]["selected"]["cells"].values())
        first, second = next(c for c in cells if c["type"] == "$and"), next(c for c in cells if c["type"] == "$not")
        self.assertEqual(first["connections"]["Y"], second["connections"]["A"])
        self.assertTrue(set(first["connections"]["Y"]).isdisjoint({p["original_bit"] for p in report["boundary"]}))

    def test_export_bus_offset_and_upto_alias_mapping(self):
        self.experiment("busindices", """module demo(input [0:3] a, input [7:4] b, output [4:1] y);
assign y = a ^ b;
endmodule
""")
        _, _, report = self.export("busindices", self.base / "busindices", self.base / "busindices.v")
        aliases = [a for p in report["boundary"] for a in p["aliases"]]
        a = next(a for a in aliases if a["name"] == "a" and a["offset"] == 0)
        b = next(a for a in aliases if a["name"] == "b" and a["offset"] == 0)
        self.assertEqual((a["source_index"], b["source_index"]), (3, 4))

    def test_column_selection_distinguishes_same_line_expressions(self):
        line = "assign y1 = a & b; assign y2 = a | b;"
        self.experiment("columns", "module demo(input a,b, output y1,y2);\n" + line + "\nendmodule\n")
        directory, source = self.base / "columns", self.base / "columns.v"
        all_cells = query(parser().parse_args(["query", str(directory), "--source", str(source), "--line", "2"]))
        self.assertEqual(len(all_cells["final_cells"]), 2)
        col = line.index("a | b") + 1
        _, netlist, _ = self.export("columns", directory, source, "--column", str(col))
        self.assertEqual([c["type"] for c in netlist["modules"]["selected"]["cells"].values()], ["$or"])
        at_end = query(parser().parse_args(["query", str(directory), "--source", str(source), "--line", "2", "--column", str(col + 5)]))
        self.assertEqual(at_end["resolution"], "source_not_mapped")

    def test_connection_only_selection_is_not_reported_as_no_effect(self):
        self.experiment("wireonly", "module demo(input a, output y,z);\nassign y = a;\nassign z = 1'b0;\nendmodule\n")
        directory, source = self.base / "wireonly", self.base / "wireonly.v"
        result = query(parser().parse_args(["query", str(directory), "--source", str(source), "--line", "2"]))
        self.assertEqual(result["resolution"], "source_not_mapped")
        self.assertFalse(result["empty_result_means_no_effect"])
        declaration = query(parser().parse_args(["query", str(directory), "--source", str(source), "--line", "1"]))
        self.assertEqual(declaration["resolution"], "non_cell_objects_only")
        with self.assertRaisesRegex(TraceError, "does not mean"):
            self.export("wireonly", directory, source)
        self.assertFalse((self.base / "exports/wireonly.v").exists())

    def test_export_preserves_preexisting_sidecar(self):
        reserved = self.base / "reserved.json"
        reserved.write_text("existing result")
        args = parser().parse_args(["extract", str(self.out), "--source", str(self.source), "--line", "2", "--out", str(reserved.with_suffix(".v"))])
        with self.assertRaisesRegex(TraceError, "overwrite"):
            extract(args)
        self.assertEqual(reserved.read_text(), "existing result")
        self.assertFalse(reserved.with_suffix(".v").exists())

    def test_export_by_object_with_custom_module_and_spaced_path(self):
        cell = next(r for r in self.trace.final if self.trace.objects[r]["kind"] == "cell")
        args = parser().parse_args(["extract", str(self.out), "--object", cell, "--module-name", "gold",
                                   "--out", str(self.base / "exports/object with spaces.v")])
        result = extract(args)
        self.assertEqual(result["module"], "gold")
        self.assertTrue(result["validation"]["exported_verilog_equivalent_to_cell_slice"])

    def test_ambiguous_drivers_and_inout_are_rejected(self):
        original = json.loads((self.out / "after.json").read_text())["modules"]["demo"]
        name = next(iter(original["cells"]))
        multi = copy.deepcopy(original)
        multi["cells"]["second_driver"] = copy.deepcopy(multi["cells"][name])
        with self.assertRaisesRegex(TraceError, "drivers"):
            slice_module(multi, [name], "selected")
        inout = copy.deepcopy(original)
        inout["ports"]["a"]["direction"] = "inout"
        with self.assertRaisesRegex(TraceError, "inout"):
            slice_module(inout, [name], "selected")

    def test_wrong_before_step_id_fails_but_legacy_logs_load(self):
        records = copy.deepcopy(self.trace.records)
        before = next(r for r in records if r["record_type"] == "snapshot" and r["phase"] == "before")
        before["step_id"] = "wrong_step"
        path = self.base / "wrong-step.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        with self.assertRaisesRegex(TraceError, "before snapshot"):
            Trace(path)
        # v1 logs before the fix lack the convention marker; keep them readable.
        records[0].pop("step_snapshot_ids")
        before["step_id"] = ""
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        self.assertEqual(len(Trace(path).events), 1)

    def test_signed_comparison_export_preserves_parameter_semantics(self):
        self.experiment("signed", """module demo(input signed [3:0] a,b, output y);
assign y = a < b;
endmodule
""")
        _, netlist, report = self.export("signed", self.base / "signed", self.base / "signed.v")
        cell = next(iter(netlist["modules"]["selected"]["cells"].values()))
        self.assertEqual(int(cell["parameters"]["A_SIGNED"], 2), 1)
        self.assertTrue(report["validation"]["exported_verilog_equivalent_to_cell_slice"])


if __name__ == "__main__":
    unittest.main()
