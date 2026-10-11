"""Extract provenance-associated cells with an explicit electrical boundary."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .runner import PIN, digest, identifier, load_run, query_loaded, token, write_json
from .trace import TraceError, require, unescape


def state_cell(cell):
    kind = cell["type"].lower().lstrip("$_")
    return kind.startswith(("dff", "adff", "aldff", "sdff", "dlatch", "adlatch", "sr", "ff", "mem"))


def slice_module(module, selected_names, module_name, selected_connections=()):
    """Yosys JSON bit IDs already represent electrical aliases.

    Traverse connectivity only to find the cut. Never include upstream cells
    merely because they drive a selected cell.
    """
    selected = set(selected_names)
    require((bool(selected) or bool(selected_connections)) and selected <= module["cells"].keys(), "invalid or empty selection")
    drivers, users, aliases = defaultdict(list), defaultdict(list), defaultdict(list)
    used, driven = set(), set()
    ambiguous, state_outputs = set(), set()
    connection_outputs = []
    for row in selected_connections:
        require(row["lhs_bits"] == row["rhs_bits"], "selected connection differs from final aliases")
        for offset, bit in enumerate(row["lhs_bits"]):
            connection_outputs.append((row, offset, bit))
            if isinstance(bit, int):
                used.add(bit)

    for name, cell in module["cells"].items():
        require(set(cell["connections"]) == set(cell["port_directions"]), f"unknown port directions: {name}")
        for port, bits in cell["connections"].items():
            direction = cell["port_directions"][port]
            require(direction in {"input", "output", "inout"}, f"unknown direction: {name}.{port}")
            if name in selected:
                require(direction != "inout", "inout cell boundaries are not supported")
            for offset, bit in enumerate(bits):
                endpoint = {"kind": "cell", "name": name, "port": port, "offset": offset}
                if name in selected:
                    if isinstance(bit, int):
                        used.add(bit)
                        if direction == "output":
                            driven.add(bit)
                            if state_cell(cell):
                                state_outputs.add(bit)
                    else:
                        require(direction == "input" and bit in {"0", "1", "x", "z"},
                                f"unsupported constant on {name}.{port}: {bit}")
                if not isinstance(bit, int):
                    continue
                if direction == "inout":
                    ambiguous.add(bit)
                if direction in {"output", "inout"}:
                    drivers[bit].append(endpoint)
                if direction in {"input", "inout"}:
                    users[bit].append(endpoint)

    for name, port in module["ports"].items():
        direction = port["direction"]
        require(direction in {"input", "output", "inout"}, f"unknown module port direction: {name}")
        for offset, bit in enumerate(port["bits"]):
            if not isinstance(bit, int):
                continue
            endpoint = {"kind": "module_port", "name": name, "offset": offset}
            if direction == "inout":
                ambiguous.add(bit)
            if direction in {"input", "inout"}:
                drivers[bit].append(endpoint)
            if direction in {"output", "inout"}:
                users[bit].append(endpoint)

    initial = {}
    for name, net in module["netnames"].items():
        for offset, bit in enumerate(net["bits"]):
            if bit not in used:
                continue
            source_index = net.get("offset", 0) + (len(net["bits"]) - 1 - offset if net.get("upto") else offset)
            aliases[bit].append({"name": name, "offset": offset, "source_index": source_index,
                                 "hide_name": bool(net.get("hide_name", 0)), "attributes": deepcopy(net.get("attributes", {}))})
            init = net.get("attributes", {}).get("init")
            if init is not None and bit in state_outputs:
                require(isinstance(init, str) and len(init) == len(net["bits"]), f"unsupported init encoding on {name}")
                value = init[-1 - offset]
                require(value in "01xz", f"invalid init bit on {name}")
                if bit in initial and value != "x" and initial[bit] != "x":
                    require(initial[bit] == value, f"conflicting init values at net bit {bit}")
                if bit not in initial or value != "x":
                    initial[bit] = value

    for bit in used:
        require(bit not in ambiguous, f"inout boundary at net bit {bit} is not supported")
        require(len(drivers[bit]) == 1, f"net bit {bit} has {len(drivers[bit])} drivers; cannot extract an unambiguous boundary")

    def is_external(endpoint):
        return endpoint["kind"] == "module_port" or endpoint["name"] not in selected

    inputs = used - driven
    outputs = {bit for bit in driven if any(is_external(e) for e in users[bit]) or not users[bit]}
    ports, boundary, nets = {}, [], {}
    for direction, bits in (("input", inputs), ("output", outputs)):
        for bit in sorted(bits):
            name = ("in_" if direction == "input" else "out_") + str(bit)
            ports[name] = {"direction": direction, "bits": [bit]}
            nets[name] = {"hide_name": 0, "bits": [bit], "attributes": {}}
            boundary.append({"port": name, "direction": direction, "original_bit": bit,
                             "reason": "unconsumed_selected_output" if direction == "output" and not users[bit]
                                       else "external_driver" if direction == "input" else "external_consumer",
                             "aliases": aliases[bit], "drivers": drivers[bit], "users": users[bit],
                             "external_drivers": [e for e in drivers[bit] if is_external(e)],
                             "external_users": [e for e in users[bit] if is_external(e)]})
            boundary[-1]["external_state_drivers"] = [e for e in drivers[bit] if e["kind"] == "cell" and
                e["name"] not in selected and state_cell(module["cells"][e["name"]])]
    for bit in sorted(used - inputs - outputs):
        nets[f"net_{bit}"] = {"hide_name": 0, "bits": [bit], "attributes": {}}
    for row, offset, bit in connection_outputs:
        original_offset = row["offsets"][offset] if "offsets" in row else offset
        original_lhs = row.get("original_lhs", row["lhs"])[original_offset]
        if bit in outputs:
            port = next(p for p in boundary if p["direction"] == "output" and p["original_bit"] == bit)
            port.setdefault("source_connections", []).append({"ref": row["ref"], "offset": original_offset,
                                                              "original_lhs": original_lhs})
            continue
        name = f"connection_{len(ports)}_{offset}"
        ports[name] = {"direction": "output", "bits": [bit]}
        nets[name] = {"hide_name": 0, "bits": [bit], "attributes": {}}
        boundary.append({"port": name, "direction": "output", "original_bit": bit,
                         "reason": "selected_source_connection", "connection_ref": row["ref"],
                         "connection_offset": original_offset, "original_lhs": original_lhs,
                         "aliases": aliases[bit], "drivers": drivers[bit], "users": users[bit]})
    for net in nets.values():
        bit = net["bits"][0]
        if bit in initial:
            net["attributes"]["init"] = initial[bit]
    # No optimization here: keep original names, types, parameters, directions,
    # attributes, connection ordering, and literal constants byte-for-value.
    cells = {name: deepcopy(module["cells"][name]) for name in sorted(selected)}
    result = {"attributes": {"top": "00000000000000000000000000000001"},
              "ports": ports, "cells": cells, "netnames": nets}
    return {"modules": {module_name: result}}, boundary


def extract(args):
    out = Path(args.out).absolute()
    require(out.suffix == ".v", "--out must name a new .v file")
    module_name = identifier(args.module_name)
    targets = {suffix: out.with_suffix(suffix) for suffix in
               (".v", ".il", ".json", ".provenance.json", ".ys", ".log")}
    for path in targets.values():
        require(not path.exists() and not path.is_symlink(), f"refusing to overwrite {path}")
        token(path.name)

    manifest, trace, _ = load_run(args.directory)
    last_pass = (manifest["endpoint"] if manifest.get("schema_version") == 2 else
                 next((r["pass"] for r in reversed(trace.records) if r["record_type"] == "step_begin"), None))
    selection = query_loaded(args, manifest, trace)
    connections = selection.get("final_connections", [])
    require(selection["final_cells"] or connections,
            f"no mapped final cells or connections to extract (resolution={selection['resolution']}); "
            "this does not mean the statement has no effect. Inspect the recorded coverage gaps.")
    modules = {unescape(cell["module"]) for cell in selection["final_cells"] + connections}
    require(len(modules) == 1, "extraction currently requires cells from a single module")
    original_module = next(iter(modules))
    after = Path(args.directory) / "after.json"
    design = json.loads(after.read_text())
    require(digest(after) == manifest["artifacts"]["after.json"]["sha256"], "after.json changed during selection")
    chosen_names = [unescape(cell["name"]) for cell in selection["final_cells"]]
    netlist, boundary = slice_module(design["modules"][original_module], chosen_names, module_name, connections)
    has_state = any(state_cell(c) for c in netlist["modules"][module_name]["cells"].values())
    if has_state:
        targets[".roundtrip.json"] = out.with_suffix(".roundtrip.json")
        require(not targets[".roundtrip.json"].exists() and not targets[".roundtrip.json"].is_symlink(),
                f"refusing to overwrite {targets['.roundtrip.json']}")

    yosys = shutil.which(args.yosys)
    require(yosys is not None, f"Yosys executable not found: {args.yosys}")
    version = subprocess.check_output([yosys, "-V"], text=True).strip()
    require(PIN in version, f"extraction is tested with Yosys {PIN}; found {version}")

    def filename(suffix):
        return token(targets[suffix].name)

    # Verify both the standalone cut and the emitted Verilog. The gold design is
    # the exact cell slice, not a fresh synthesis of just the source statement.
    commands = [f"read_json {filename('.json')}", f"hierarchy -top {module_name}", "check -assert",
                f"write_rtlil {filename('.il')}", f"write_verilog -noattr {filename('.v')}"]
    if module_name != "gold":
        commands.append(f"rename {module_name} gold")
    commands += ["design -stash gold_design", f"read_verilog {filename('.v')}",
                 f"hierarchy -top {module_name}", "proc"]
    if module_name != "gate":
        commands.append(f"rename {module_name} gate")
    if has_state:
        commands.append(f"write_json {filename('.roundtrip.json')}")
    commands += ["design -stash gate_design", "design -copy-from gold_design -as gold gold",
                 "design -copy-from gate_design -as gate gate", "equiv_make gold gate equiv",
                 "hierarchy -top equiv"]
    if has_state:
        commands.append("clk2fflogic")
    commands.append("equiv_simple -seq 5" if has_state else "equiv_simple")
    if has_state:
        commands.append("equiv_induct -seq 5")
    commands.append("equiv_status -assert")
    script = "\n".join(commands) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".yosys-flow-extract-", dir=out.parent) as temporary:
        staging = Path(temporary)
        staged = {suffix: staging / dest.name for suffix, dest in targets.items()}
        write_json(staged[".json"], netlist)
        staged[".ys"].write_text(script)
        with staged[".log"].open("w") as log:
            process = subprocess.run([yosys, "-Q", "-T", "-s", staged[".ys"].name], cwd=staging,
                                     stdout=log, stderr=subprocess.STDOUT)
        if process.returncode:
            raise TraceError("extracted netlist validation failed; no export published:\n" +
                             staged[".log"].read_text()[-3000:])
        if has_state:
            # Induction alone can overlook initial-state restrictions. Check the
            # named state-bit aliases after Verilog reimport as an independent
            # assertion that the serialized init information survived.
            roundtrip = json.loads(staged[".roundtrip.json"].read_text())["modules"]["gate"]
            init_by_bit = {}
            for net in roundtrip["netnames"].values():
                if "init" in net.get("attributes", {}):
                    for i, bit in enumerate(net["bits"]):
                        init_by_bit[bit] = net["attributes"]["init"][-1 - i]
            gold_module = netlist["modules"][module_name]
            state_bits = {bit for cell in gold_module["cells"].values() if state_cell(cell)
                          for port, bits in cell["connections"].items() if cell["port_directions"][port] == "output" for bit in bits}
            for name, net in gold_module["netnames"].items():
                if net["bits"][0] in state_bits:
                    require(name in roundtrip["netnames"], f"state alias missing after reimport: {name}")
                    bit = roundtrip["netnames"][name]["bits"][0]
                    require(init_by_bit.get(bit, "x") == net["attributes"].get("init", "x"),
                            f"state init changed after reimport: {name}")
        report = {
            "schema_version": manifest["schema_version"], "status": "complete", "run_id": manifest["run_id"],
            "mode": "source_association", "upstream_cone_included": False,
            "original_module": original_module, "module": module_name,
            "netlist_stage": "recorded_flow_end", "last_recorded_pass": last_pass,
            "after_json_sha256": manifest["artifacts"]["after.json"]["sha256"],
            "selector": {key: getattr(args, key, None) for key in
                         ("source", "line", "end_line", "column", "end_column", "instance", "object")},
            "selected_cells": selection["final_cells"], "boundary": boundary,
            "selected_connections": connections, "contains_state": has_state,
            "association_precision": selection.get("association_precision", ["src_attribute"]),
            "provenance": {key: selection[key] for key in ("seeds", "sources", "edges", "events")},
            "coverage": selection["coverage"], "coverage_gaps": selection["coverage_gaps"],
            "changed_or_missing_base_inputs": selection["changed_or_missing_base_inputs"],
            "safe_for_incremental_reuse": False,
            "validation": {"exact_cell_copy": True, "boundary_check": True, "yosys_check": True,
                           "exported_verilog_equivalent_to_cell_slice": True},
            "tool_version": version,
            "note": "端口按电气信号位切割，映射见 boundary；共享门保留全部已记录来源。等价检查验证导出格式，未证明此集合可安全替换。",
            "artifacts": {dest.name: {"sha256": digest(staged[suffix]), "size_bytes": staged[suffix].stat().st_size}
                          for suffix, dest in targets.items() if suffix != ".provenance.json"},
        }
        if manifest["schema_version"] == 2:
            report["end_to_end_exact"] = selection["end_to_end_exact"]
            report["abc_regions"] = selection.get("abc_regions", [])
            report["abc_evidence_artifacts"] = {name: manifest["artifacts"][name]
                for region in report["abc_regions"] for name in region.get("artifacts", {}).values()}
        if has_state:
            report["validation"]["selected_state_init_preserved"] = True
            report["validation"]["sequential_equivalence"] = True
            report["sequential_verification"] = {
                "method": "clk2fflogic; equiv_simple -seq 5; equiv_induct -seq 5; equiv_status -assert",
                "init_check": "per-bit alias comparison after Verilog reimport",
                "limitations": "clk2fflogic uses sampled clock events and negative hold time for async inputs; "
                               "equiv_induct is a non-divergence proof after equal observable states. "
                               "Exact cell copying and the separate init roundtrip check preserve the selected state definition.",
            }
        write_json(staged[".provenance.json"], report)
        # Publish with exclusive creation and the completion report last. Preserve
        # all pre-existing outputs, including a file created during validation.
        published = []
        try:
            order = [".json", ".il", ".v", ".ys", ".log"]
            if has_state:
                order.append(".roundtrip.json")
            for suffix in [*order, ".provenance.json"]:
                os.link(staged[suffix], targets[suffix])
                published.append(targets[suffix])
        except OSError:
            for path in published:
                path.unlink()
            raise
    return {"status": "complete", "out": str(out), "module": module_name,
            "selected_cell_count": len(chosen_names), "input_bits": sum(p["direction"] == "input" for p in boundary),
            "output_bits": sum(p["direction"] == "output" for p in boundary),
            "provenance": str(targets[".provenance.json"]), "validation": report["validation"],
            "coverage": report["coverage"], "last_recorded_pass": report["last_recorded_pass"],
            "association_precision": report["association_precision"],
            "safe_for_incremental_reuse": False}
