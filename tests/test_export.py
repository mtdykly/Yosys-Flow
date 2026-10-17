"""Common export and run contracts migrated from the retired plugin suite.

The base run uses default synth. Coarse cell/parameter assertions explicitly
use the native proc checkpoint; they do not assert coarse types after synth.
"""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import ROOT, TraceError
from yosys_flow.extract import extract, slice_module
from yosys_flow.runner import load_run, query, run


@unittest.skipUnless(NATIVE.is_file(), "native Yosys not built; run make test")
class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="yosys-export-tests-")
        cls.base = Path(cls.temp.name)
        cls.source = cls.base / "merge with spaces.v"
        cls.source.write_text((ROOT / "examples/merge.v").read_text())
        cls.out = cls.base / "base with spaces"
        run(parser().parse_args(["run", "--native-yosys", str(NATIVE), "--source", str(cls.source),
                                 "--top", "demo", "--out", str(cls.out)]))
        cls.manifest, cls.trace, cls.report = load_run(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def experiment(self, name, rtl, *extra):
        source = self.base / (name + ".v")
        source.write_text(rtl)
        out = self.base / name
        args = parser().parse_args(["run", "--native-yosys", str(NATIVE), "--source", str(source), "--top", "demo", "--out", str(out), *extra])
        run(args)
        return load_run(out)

    def test_include_define_parameter_manifest(self):
        inc = self.base / "includes"
        inc.mkdir()
        header = inc / "width.vh"
        header.write_text("`define WIDTH 2\n")
        manifest, trace, _ = self.experiment("configured", """`include "width.vh"
module demo #(parameter N=`WIDTH)(input [N-1:0] a,b, output [N-1:0] y1,y2);
assign y1 = a `OP b;
assign y2 = a `OP b;
endmodule
""", "-I", str(inc), "-D", "OP=&", "--param", "N=4")
        self.assertIn(str(header), {i["path"] for i in manifest["inputs"]})
        self.assertEqual(manifest["configuration"]["parameters"], ["N=4"])
        module = json.loads((self.base / "configured/after.json").read_text())["modules"]["demo"]
        self.assertTrue(all(len(p["bits"]) == 4 for p in module["ports"].values()))
        self.assertTrue(any(r["record_type"] == "cell_remove" for r in trace.records))
        result, _, report = self.export("configured", self.base / "configured", self.base / "configured.v", "--line", "3")
        self.assertEqual(result["selected_cell_count"], 4)
        self.assertTrue(all(c["all_origins"] for c in report["selected_cells"]))


    def test_parameter_clone_preserves_combinational_process_and_connections(self):
        _, trace, _ = self.experiment("parameter-comb", """module demo #(parameter N=1)(input sel, input [N-1:0] a,b,
output reg [N-1:0] y, output [N-1:0] direct, output zero);
always @* if (sel) y=a; else y=b;
assign direct = a;
assign zero = 1'b0;
endmodule
""", "--param", "N=2")
        source, directory = self.base / "parameter-comb.v", self.base / "parameter-comb"
        result, _, _ = self.export("parameter-process", directory, source, "--line", "3")
        self.assertTrue(result["validation"]["exported_verilog_equivalent_to_cell_slice"])
        for line, name in ((4, "direct"), (5, "zero")):
            selected = query(parser().parse_args(["query", str(directory), "--source", str(source), "--line", str(line)]))
            self.assertEqual(selected["resolution"], "connections_found")
            self.assertFalse(selected["final_cells"])
            self.assertTrue(any(e["capture"] == "hierarchy_parameter_clone" for e in selected["edges"]))
            exported, _, _ = self.export("parameter-" + name, directory, source, "--line", str(line))
            self.assertEqual(exported["selected_cell_count"], 0)
        self.assertTrue(any(r["record_type"] == "assignment" and r.get("cloned_from") for r in trace.records))

    def test_source_and_output_paths_with_spaces(self):
        self.experiment("paths with spaces", (ROOT / "examples/merge.v").read_text())

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
        self.assertEqual({s["line"] for s in report["selected_cells"][0]["all_origins"]}, {2, 3})
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
        self.experiment("cut", rtl, "--flow", "m1")
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
""", "--flow", "m1")
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

    def test_export_preserves_preexisting_sidecar(self):
        reserved = self.base / "reserved.json"
        reserved.write_text("existing result")
        args = parser().parse_args(["extract", str(self.out), "--source", str(self.source), "--line", "2", "--out", str(reserved.with_suffix(".v"))])
        with self.assertRaisesRegex(TraceError, "overwrite"):
            extract(args)
        self.assertEqual(reserved.read_text(), "existing result")
        self.assertFalse(reserved.with_suffix(".v").exists())

    def test_export_by_object_with_custom_module_and_spaced_path(self):
        cell = next(iter(self.trace.final))
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

    def test_signed_comparison_export_preserves_parameter_semantics(self):
        self.experiment("signed", """module demo(input signed [3:0] a,b, output y);
assign y = a < b;
endmodule
""", "--flow", "m1")
        _, netlist, report = self.export("signed", self.base / "signed", self.base / "signed.v")
        cell = next(iter(netlist["modules"]["selected"]["cells"].values()))
        self.assertEqual(int(cell["parameters"]["A_SIGNED"], 2), 1)
        self.assertTrue(report["validation"]["exported_verilog_equivalent_to_cell_slice"])
