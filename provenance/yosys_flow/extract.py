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

from .common import PIN, TraceError, digest, identifier, require, require_combinational, token, unescape, write_json
from .runner import load_run, query_loaded


def slice_module(module, selected_names, module_name, selected_connections=()):
    """Yosys JSON bit IDs already represent electrical aliases.

    Traverse connectivity only to find the cut. Never include upstream cells
    merely because they drive a selected cell.
    """
    require_combinational(module)
    selected = set(selected_names)
    require((bool(selected) or bool(selected_connections)) and selected <= module["cells"].keys(), "invalid or empty selection")
    drivers, users, aliases = defaultdict(list), defaultdict(list), defaultdict(list)
    used, driven = set(), set()
    ambiguous = set()
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

    for name, net in module["netnames"].items():
        for offset, bit in enumerate(net["bits"]):
            if bit not in used:
                continue
            source_index = net.get("offset", 0) + (len(net["bits"]) - 1 - offset if net.get("upto") else offset)
            aliases[bit].append({"name": name, "offset": offset, "source_index": source_index,
                                 "hide_name": bool(net.get("hide_name", 0)), "attributes": deepcopy(net.get("attributes", {}))})
            if 'definition' in net: aliases[bit][-1]['definition']=deepcopy(net['definition'])
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
    for bit in sorted(used - inputs - outputs):
        nets[f"net_{bit}"] = {"hide_name": 0, "bits": [bit], "attributes": {}}
    for row, offset, bit in connection_outputs:
        # Selected drivers and consumers already retain this internal net.
        if bit in driven and bit not in outputs:
            continue
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
        binding = module.get('_connection_users',{}).get((row['ref'],original_offset))
        if binding is not None:
            boundary[-1]['external_users'] = binding
            boundary[-1]['consumer_binding'] = 'recorded_instance_output_port'
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

    contract_document = None
    if getattr(args,'contract',None):
        require(all(getattr(args,k,None) is None for k in ('line','end_line','column','end_column','instance')),
                'source/instance selectors cannot be combined with --contract')
        from .contracts import ContractContext, export_selection
        context = ContractContext(args.directory)
        contract_document = context.validate(json.loads(Path(args.contract).read_text()))
        manifest,trace = context.manifest,context.trace
        selection = export_selection(context,contract_document)
    else:
        manifest, trace, _ = load_run(args.directory)
        selection = query_loaded(args, manifest, trace)
    last_pass = manifest["endpoint"]
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
    netlist, boundary = slice_module(trace.design, chosen_names, module_name, connections)
    has_pmux = any(c["type"] == "$pmux" for c in netlist["modules"][module_name]["cells"].values())

    yosys = shutil.which(args.yosys)
    require(yosys is not None, f"Yosys executable not found: {args.yosys}")
    version = subprocess.check_output([yosys, "-V"], text=True).strip()
    require(PIN in version, f"extraction is tested with Yosys {PIN}; found {version}")

    def filename(suffix):
        return token(targets[suffix].name)

    # Verify both the standalone cut and the emitted Verilog. The gold design is
    # the exact cell slice, not a fresh synthesis of just the source statement.
    verilog_options = "-noattr -noparallelcase" if has_pmux else "-noattr"
    undef_option = " -undef" if has_pmux else ""
    commands = [f"read_json {filename('.json')}", f"hierarchy -top {module_name}", "check -assert",
                f"write_rtlil {filename('.il')}", f"write_verilog {verilog_options} {filename('.v')}"]
    if module_name != "gold":
        commands.append(f"rename {module_name} gold")
    commands += ["design -stash gold_design", f"read_verilog {filename('.v')}",
                 f"hierarchy -top {module_name}", "proc"]
    if module_name != "gate":
        commands.append(f"rename {module_name} gate")
    commands += ["design -stash gate_design", "design -copy-from gold_design -as gold gold",
                 "design -copy-from gate_design -as gate gate", "equiv_make gold gate equiv",
                 "hierarchy -top equiv"]
    commands.append("equiv_simple" + undef_option + "")
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
        report = {
            "schema_version": manifest["schema_version"], "status": "complete", "run_id": manifest["run_id"],
            "mode": "source_association", "upstream_cone_included": False,
            "original_module": original_module, "module": module_name,
            "netlist_view":trace.view_rule, "instance_catalog":trace.instance_catalog,
            "recorded_flatten_events":list(trace.flatten_events.values()),
            "netlist_stage": "recorded_flow_end", "last_recorded_pass": last_pass,
            "after_json_sha256": manifest["artifacts"]["after.json"]["sha256"],
            "selector": {key: getattr(args, key, None) for key in
                         ("source", "line", "end_line", "column", "end_column", "instance", "object")},
            "selected_cells": selection["final_cells"], "boundary": boundary,
            "selected_connections": connections, "input_scope": "combinational",
            "association_precision": selection["association_precision"],
            "provenance": {key: selection[key] for key in ("seeds", "sources", "edges", "events")},
            "process_actions": selection["process_actions"], "process_proof_nodes": selection["process_proof_nodes"],
            "process_discards": selection["process_discards"],
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
        report["end_to_end_exact"] = selection["end_to_end_exact"]
        report["abc_regions"] = selection.get("abc_regions", [])
        report["abc_refinement_fallbacks"] = selection.get("abc_refinement_fallbacks", [])
        report["abc_associations"] = selection.get("abc_associations", [])
        report["abc_internal"] = selection.get("abc_internal", [])
        report["joint_cut_proven_refs"] = selection.get("joint_cut_proven_refs", [])
        report["cut_proven_refs"] = selection.get("cut_proven_refs", [])
        report["techmap_bit_maps"] = selection.get("techmap_bit_maps", [])
        report["abc_evidence_artifacts"] = {name: manifest["artifacts"][name]
            for region in report["abc_regions"] for name in region.get("artifacts", {}).values()}
        if contract_document:
            report['mode'] = 'region_contract'
            report['contract_id'] = contract_document['contract_id']
            report['contract_verification'] = contract_document['verification']
        if has_pmux:
            report["pmux_semantics"] = {"multiple_active_selects": "undefined", "undefined_values_modelled": True, "verilog_option": "-noparallelcase"}
        write_json(staged[".provenance.json"], report)
        # Publish with exclusive creation and the completion report last. Preserve
        # all pre-existing outputs, including a file created during validation.
        published = []
        try:
            order = [".json", ".il", ".v", ".ys", ".log"]
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
