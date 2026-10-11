"""Reproducible, fail-closed orchestration of the supported experiment."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from .trace import Trace, TraceError, require

ROOT = Path(__file__).resolve().parents[1]
PIN = "0e82bbefe"
GAPS = [
    {"stage": "read_verilog / hierarchy", "capture": "src_attribute", "coverage": "partial",
     "reason": "源码位置仅来自 RTLIL src 属性；宏、AST 简化、generate、参数依赖未插桩。"},
    {"stage": "other passes / ABC", "capture": "none", "coverage": "not_executed",
     "reason": "本次流程只执行 opt_merge；尚未实现完整综合与 ABC 跨工具追踪。"},
]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def token(value):
    value = str(value)
    # Yosys scripts are not a shell, but separators must still be excluded.
    if any(c in value for c in '\n\r\x00;"'):
        raise TraceError(f"unsupported character in Yosys argument: {value!r}")
    return '"' + value.replace("\\", "\\\\") + '"'


def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value):
        raise TraceError(f"expected a simple Verilog identifier: {value!r}")
    return value


def run(args):
    if getattr(args, "flow", "opt-merge") in {"m1", "m1-opt", "synth"}:
        from .native import run_native
        return run_native(args)
    out = Path(args.out).resolve()
    require(not out.exists(), f"output directory already exists: {out}; choose a new --out")
    plugin = Path(args.plugin).resolve()
    require(plugin.is_file(), f"plugin is missing: {plugin}; run make first")
    yosys = shutil.which(args.yosys)
    require(yosys is not None, f"Yosys executable not found: {args.yosys}")
    version = subprocess.check_output([yosys, "-V"], text=True).strip()
    require(PIN in version, f"plugin is pinned to Yosys {PIN}; found {version}")
    sources = [Path(p).resolve(strict=True) for p in args.source]
    includes = [Path(p).resolve(strict=True) for p in args.include]
    for directory in includes:
        require(directory.is_dir() and not any(c.isspace() for c in str(directory)),
                "this Yosys frontend requires include directories without whitespace")
    top = identifier(args.top)
    require(args.steps >= 1, "--steps must be at least 1")
    defines = []
    for define in args.define:
        identifier(define.split("=", 1)[0])
        # Embedded whitespace / script syntax in macros needs a richer frontend API.
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(=[A-Za-z0-9_'()+*/.<>=!~&|^?:+-]+)?", define) is not None,
                f"unsupported macro definition: {define}")
        defines.append("-D" + define)
    params = []
    for parameter in args.param:
        name, separator, value = parameter.partition("=")
        require(bool(separator) and re.fullmatch(r"[0-9][A-Za-z0-9_'?+-]*", value) is not None,
                "--param expects NAME=numeric_value")
        params.extend(["-chparam", identifier(name), value])
    read = "read_verilog -sv -defer " + " ".join(defines + ["-I" + str(p) for p in includes] + [token(p) for p in sources])
    hierarchy = "hierarchy -check -top " + top + " " + " ".join(params)
    merge_options = " -nomux" if args.nomux else ""
    # Validate paths before creating any output directory.
    for path in [out, plugin, *sources, *includes]:
        token(path)
    out.mkdir(parents=True)
    run_id = uuid.uuid4().hex
    manifest = {
        "schema_version": 1, "run_id": run_id, "status": "running", "coverage": "partial",
        "safe_for_incremental_reuse": False, "coverage_gaps": GAPS,
        "tool": {"version": version, "executable": str(Path(yosys).resolve()), "source_commit": PIN,
                 "plugin_sha256": digest(plugin)},
        "configuration": {"top": top, "sources": list(map(str, sources)), "include_dirs": list(map(str, includes)),
                          "defines": args.define, "parameters": args.param, "steps": args.steps, "nomux": args.nomux,
                          "frontend": "builtin read_verilog -sv -defer", "libraries": [], "constraints": []},
        "started_at_ns": time.time_ns(), "inputs": [], "timings_seconds": {}, "peak_rss_kib": {}, "artifacts": {},
    }
    write_json(out / "manifest.json", manifest)

    def execute(label, script, load_plugin=False, dependencies=False):
        script_path = out / f"{label}.ys"
        script_path.write_text(script + "\n")
        command = [yosys, "-Q", "-T"]
        if load_plugin:
            command += ["-m", str(plugin)]
        if dependencies:
            command += ["-E", str(out / "frontend.d")]
        command += ["-s", str(script_path)]
        timer = Path("/usr/bin/time")
        if timer.is_file():
            command = [str(timer), "-f", "%M", "-o", str(out / f"{label}.rss"), "--", *command]
        start = time.perf_counter()
        with (out / f"{label}.log").open("w") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        manifest["timings_seconds"][label] = time.perf_counter() - start
        if timer.is_file() and result.returncode == 0:
            manifest["peak_rss_kib"][label] = int((out / f"{label}.rss").read_text().strip())
        if result.returncode:
            tail = (out / f"{label}.log").read_text()[-3000:]
            raise TraceError(f"{label} failed (exit {result.returncode}); see {out / (label + '.log')}\n{tail}")

    def path(name):
        return token(out / name)

    try:
        started = time.time_ns()
        execute("frontend", f"{read}\n{hierarchy}\nwrite_rtlil {path('frontend.il')}\nwrite_json {path('before.json')}\n"
                f"trace_inputs {path('inputs.json')}", load_plugin=True, dependencies=True)
        dependencies = sorted((set(sources) | {Path(p).resolve(strict=True) for p in json.loads((out / "inputs.json").read_text())})
                              - {out / "frontend.ys"})
        for file in dependencies:
            require(file.stat().st_mtime_ns <= started, f"input changed during frontend execution: {file}")
            manifest["inputs"].append({"path": str(file), "sha256": digest(file), "size_bytes": file.stat().st_size})
        manifest["frontend_snapshot_sha256"] = digest(out / "frontend.il")
        write_json(out / "manifest.json", manifest)
        prefix = f"read_rtlil {path('frontend.il')}\n"
        steps = "\n".join("trace_opt_merge" + merge_options for _ in range(args.steps))
        execute("traced", prefix + f"trace_begin {path('yosys.jsonl')} {run_id}\n{steps}\ntrace_end\n"
                f"check -assert\nwrite_rtlil {path('after.il')}\nwrite_json {path('after.json')}\nwrite_verilog {path('after.v')}", load_plugin=True)
        trace = Trace(out / "yosys.jsonl")
        trace.compare_netlist(out / "before.json", "initial")
        trace.compare_netlist(out / "after.json")
        execute("baseline", prefix + "\n".join("opt_merge" + merge_options for _ in range(args.steps)) +
                f"\ncheck -assert\nwrite_json {path('baseline.json')}")
        traced = json.loads((out / "after.json").read_text())["modules"]
        baseline = json.loads((out / "baseline.json").read_text())["modules"]
        require(traced == baseline, "instrumented result differs from stock opt_merge")
        execute("equivalence", f"read_rtlil {path('frontend.il')}\nrename {top} gold\ndesign -stash gold_design\n"
                f"read_rtlil {path('after.il')}\nrename {top} gate\ndesign -stash gate_design\n"
                "design -copy-from gold_design -as gold gold\ndesign -copy-from gate_design -as gate gate\n"
                "equiv_make gold gate equiv\nhierarchy -top equiv\nequiv_simple\nequiv_status -assert")
        for file in manifest["inputs"]:
            require(digest(file["path"]) == file["sha256"], f"input changed during run: {file['path']}")
        validation = {"valid": True, "run_id": run_id, "schema_version": 1,
                      "checks": {"references_and_versions": True, "chronological_replay": True,
                                 "before_and_after_netlists": True, "stock_opt_merge_exact_match": True,
                                 "frontend_to_after_equivalence": True, "inputs_unchanged": True},
                      "counts": {"objects": len(trace.objects), "events": len(trace.events),
                                 "final_cells": sum(trace.objects[r]["kind"] == "cell" for r in trace.final)},
                      "coverage": "partial", "safe_for_incremental_reuse": False}
        write_json(out / "validation.json", validation)
        for file in sorted(out.iterdir()):
            if file.is_file() and file.name != "manifest.json":
                manifest["artifacts"][file.name] = {"sha256": digest(file), "size_bytes": file.stat().st_size}
        timings = manifest["timings_seconds"]
        manifest["trace_to_baseline_wall_time_ratio"] = timings["traced"] / timings["baseline"]
        manifest["timing_note"] = "Includes process startup, checks and extra trace/output I/O; not a pass-only benchmark. Peak RSS is measured with /usr/bin/time when available."
        manifest["status"] = "complete"
        manifest["finished_at_ns"] = time.time_ns()
        write_json(out / "manifest.json", manifest)
        return {"run_directory": str(out), "status": "complete", **validation}
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        write_json(out / "manifest.json", manifest)
        write_json(out / "validation.json", {"valid": False, "error": str(exc)})
        raise


def load_run(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema_version") == 2:
        from .native import load_native
        return load_native(directory, manifest)
    require(manifest.get("schema_version") == 1 and manifest.get("status") == "complete", "run is incomplete or failed")
    require(manifest.get("artifacts"), "manifest has no artifact hashes")
    required = {"yosys.jsonl", "before.json", "after.json", "baseline.json", "validation.json", "equivalence.log"}
    require(required <= manifest["artifacts"].keys(), "missing required artifact hashes")
    for name, meta in manifest["artifacts"].items():
        require(Path(name).name == name, "invalid artifact path in manifest")
        require(digest(directory / name) == meta["sha256"], f"artifact hash mismatch: {name}")
    trace = Trace(directory / "yosys.jsonl")
    require(trace.records[0]["run_id"] == manifest["run_id"], "manifest and trace run IDs differ")
    trace.compare_netlist(directory / "before.json", "initial")
    trace.compare_netlist(directory / "after.json")
    validation = json.loads((directory / "validation.json").read_text())
    require(validation.get("valid") is True, "validation did not pass")
    return manifest, trace, validation


def query(args):
    manifest, trace, _ = load_run(args.directory)
    return query_loaded(args, manifest, trace)


def query_loaded(args, manifest, trace):
    """Reuse an already validated run for export; build its indexes only once."""
    if manifest.get("schema_version") == 2:
        from .native import query_native
        return query_native(args, manifest, trace)
    reverse = args.object is not None
    if reverse:
        require(args.object in trace.objects, f"unknown object: {args.object}")
        seeds = [args.object]
    else:
        require(args.line is not None and args.line > 0, "source query requires positive --line")
        require(args.end_line is None or args.end_line >= args.line, "--end-line must be >= --line")
        column, end_column = getattr(args, "column", None), getattr(args, "end_column", None)
        require(column is None or column > 0, "--column must be positive")
        require(end_column is None or (column is not None and end_column > 0), "--end-column requires --column and must be positive")
        require(column is None or (args.end_line or args.line, end_column or column) >= (args.line, column),
                "end location must not precede start location")
        seeds = trace.source_seeds(args.source, args.line, args.end_line, args.instance, column, end_column)
    seen, edges = trace.walk(seeds, reverse=reverse)
    finals = [r for r in sorted(seen & trace.final) if trace.objects[r]["kind"] == "cell"]
    source_refs = [r for r in sorted(seen) if trace.objects[r]["kind"] == "source_span"]
    sharing = trace.connectivity()
    # Show ALL origins of a shared final gate, even when only one statement was queried.
    other_origins = {}
    for ref in finals:
        ancestors, _ = trace.walk([ref], reverse=True)
        other_origins[ref] = [trace.objects[r]["state"]["src"] for r in sorted(ancestors)
                              if trace.objects[r]["kind"] == "source_span"]
    changed_inputs = [i["path"] for i in manifest["inputs"]
                      if not Path(i["path"]).is_file() or digest(i["path"]) != i["sha256"]]
    non_cells = [{"ref": r, "kind": trace.objects[r]["kind"], "name": trace.objects[r]["name"],
                  "state": trace.objects[r]["state"]} for r in sorted(seen & trace.final)
                 if trace.objects[r]["kind"] != "cell"]
    resolution = ("cells_found" if finals else "source_not_mapped" if not seeds else
                  "non_cell_objects_only" if non_cells else "no_final_objects")
    return {"run_id": manifest["run_id"], "direction": "reverse" if reverse else "forward", "seeds": seeds,
            "matched": bool(seeds), "resolution": resolution, "final_non_cell_objects": non_cells,
            "empty_result_means_no_effect": False,
            "final_cells": [{"ref": r, "name": trace.objects[r]["name"], "module": trace.objects[r]["module"],
                "type": trace.objects[r]["state"]["type"], "all_origins": other_origins[r],
                "consumers": sharing[r]} for r in finals],
            "sources": [{"ref": r, **trace.objects[r]["state"]} for r in source_refs],
            "edges": edges, "events": [trace.events[e] for e in sorted({e["event_id"] for e in edges if e["event_id"]})],
            "coverage": manifest["coverage"], "coverage_gaps": manifest["coverage_gaps"],
            "safe_for_incremental_reuse": False, "changed_or_missing_base_inputs": changed_inputs,
            "note": "查询基于 Base 的源码坐标；返回候选来源和共享关系，不提供可安全替换的增量边界。"}
