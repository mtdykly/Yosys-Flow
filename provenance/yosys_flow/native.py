"""Native synthesis provenance and development checkpoints.

Semantic bindings are recorded at AST generation and proc/merge sites. Structural
checkpoints audit cell lifecycle; they do not establish transformation causality.
Process-signal and ABC-region candidates are not promoted to exact cell results.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from .common import PIN, TraceError, digest, identifier, require, require_combinational, token, unescape, write_json

PASSES = ["proc -noopt", "opt_merge"]
OPT_PASSES = ["proc", "opt_expr", "opt_clean", "opt_merge", "opt_clean"]
GAPS = [
    {"stage": "frontend", "coverage": "partial", "reason":
     "语句/表达式 ID 在 AST 简化前分配；已接入 clone 和生成位置，尚未完整记录 AST 简化、宏及参数依赖。"},
    {"stage": "proc", "coverage": "partial", "reason":
     "组合 mux 和连接使用动作/分支证据；完整前端简化与组合过程变换尚未覆盖。"},
    {"stage": "opt_expr / opt_clean / remaining synth / ABC", "coverage": "not_executed", "reason":
     "本次终点是 proc -noopt; opt_merge。尚未执行完整基础优化或默认 synth。"},
]


def resolve_bit(bit, module):
    if isinstance(bit, str):
        require(bit in {"0", "1", "x", "z"}, f"invalid constant: {bit}")
        return bit
    name = unescape(bit["wire"])
    require(name in module["netnames"], f"recorded signal no longer exists: {name}")
    bits = module["netnames"][name]["bits"]
    require(isinstance(bit["offset"], int) and 0 <= bit["offset"] < len(bits), "invalid recorded signal offset")
    return bits[bit["offset"]]


class NativeTrace:
    def __init__(self, path):
        self.records = [json.loads(line) for line in Path(path).read_text().splitlines()]
        require(bool(self.records) and self.records[0].get("record_type") == "run_begin" and
                self.records[0].get("schema_version") == 2, "invalid native trace header")
        require(self.records[-1].get("record_type") == "run_end", "incomplete native trace")
        self.sources, self.objects, self.assignments, self.events, self.edges = {}, {}, {}, [], []
        self.regions, self.removals = [], []
        self.process_nodes, self.process_actions, self.process_discards = {}, {}, []
        self.process_connection_proofs = set()
        invocations = {}
        self.abc_imports = {}
        self.techmap_bits = []
        self.hierarchy = None
        self.flatten_events = {}
        live, stack, steps, retired = set(), [], set(), set()
        self.gaps = []
        precision_rank = {"cell": 0, "process_action": 1, "cell_group_candidate": 2,
                          "process_signal_candidate": 3, "abc_region_candidate": 4, "process_pending": 5}
        conservative_merge = "conservative_precision_merge" in self.records[0].get("features", [])
        for sequence, row in enumerate(self.records, 1):
            require(row.get("seq") == sequence, "native sequence discontinuity")
            kind = row["record_type"]
            if kind == "run_begin":
                require(sequence == 1, "duplicate native run header")
            if kind == "step_begin":
                sid = row["step_id"]
                require(sid > 0 and sid not in steps and row["parent_step_id"] == (stack[-1] if stack else 0), "invalid native step nesting")
                steps.add(sid)
                stack.append(sid)
            else:
                require(row["step_id"] == (stack[-1] if stack else 0), "native record has wrong step ID")
            if "enclosing_controls" in row:
                require(set(row["enclosing_controls"]) <= self.sources.keys(), "unknown enclosing process control")
            if "origins" in row:
                require(set(row["origins"]) <= self.sources.keys(), "unknown native source reference")
            if kind == "source":
                require(row["id"] not in self.sources and row["id"] > 0, "duplicate native source ID")
                if row.get('flatten_event'):
                    require(row['flatten_event'] in self.flatten_events and row.get('cloned_from') in self.sources,
                            'unknown flatten source input')
                    original = self.sources[row['cloned_from']]
                    event = self.flatten_events[row['flatten_event']]
                    require(all(row[k]==original[k] for k in ('module','file','line','column','end_line','end_column','kind')) and
                            row['context_module']==event['module'] and
                            row['instance_path']==[unescape(event['name']), *original.get('instance_path',[])],
                            'flatten source context differs from actual instance')
                self.sources[row["id"]] = row
            elif kind == 'hierarchy_snapshot':
                require(self.hierarchy is None and row['top'] in row['modules'], 'invalid hierarchy snapshot')
                for name,module in row['modules'].items():
                    for port,data in module['ports'].items():
                        require(data['direction'] in {'input','output'} and data['bits']==[
                            {'wire':port,'offset':i} for i in range(len(data['bits']))], 'invalid declared hierarchy port')
                    for instance in module['instances'].values():
                        require(instance['ref'] in live and self.objects[instance['ref']]['state']==instance['state'] and
                                instance['state']['module']==name and instance['state']['type'] in row['modules'],
                                'hierarchy binding differs from live cell')
                        ports=row['modules'][instance['state']['type']]['ports']
                        require(all(p in ports and ports[p]['direction']==d for p,d in instance['state']['port_directions'].items()),
                                'hierarchy declared port direction differs from instance')
                self.hierarchy = row
                self.events.append(row)
            elif kind == 'flatten_instance':
                require(row['ref'] not in self.flatten_events and row['instance'] in live, 'invalid flatten instance')
                state = self.objects[row['instance']]['state']
                require(state['module']==row['module'] and state['name']==row['name'] and state['type']==row['child_module'],
                        'flatten instance differs from actual cell')
                require(len(set(row['wire_map'].values()))==len(row['wire_map']), 'non-injective flatten wire map')
                self.flatten_events[row['ref']] = row
                self.events.append(row)
            elif kind == "cell_create":
                ref = row["ref"]
                require(re.fullmatch(r"native:cell_[1-9][0-9]*@0", ref) and ref not in self.objects and ref not in retired,
                        "duplicate or invalid native cell creation")
                self.objects[ref] = row
                live.add(ref)
                self.events.append(row)
                if row.get('flatten_event'):
                    self.check_flatten_clone(row, self.objects)
                inputs = row.get("inputs", [])
                if inputs:
                    require(set(inputs) <= self.objects.keys() and ref not in inputs, "unknown or circular native derivation input")
                    expected = set().union(*(set(self.objects[r]["origins"]) for r in inputs))
                    require(set(row["origins"]) == expected, "native creation origins differ from derivation inputs")
                if row.get("abc_invocation"):
                    region = invocations.get(row["abc_invocation"])
                    require(region is not None and inputs == region["inputs"] and row["precision"] == "abc_region_candidate",
                            "native ABC output does not match its region")
                elif inputs:
                    expected_precision = self.objects[inputs[0]]["precision"] if len(inputs) == 1 else "cell_group_candidate"
                    if len(inputs) > 1 and "conservative_derivation" in self.records[0].get("features", []):
                        expected_precision = max([expected_precision, *(self.objects[r]["precision"] for r in inputs)], key=precision_rank.__getitem__)
                    require(row["precision"] == expected_precision, "native derivation changed association precision")
                self.edges += [{"from": source, "to": ref, "event_id": sequence,
                                "capture": 'flatten_cell_clone' if row.get('flatten_event') else row.get("operation") or ("ast_generation_scope" if row["precision"] == "cell" else "process_target_signal"),
                                "precision": row["precision"]} for source in (inputs or row["origins"])]
            elif kind == "process_node":
                ref, inputs = row["ref"], row["inputs"]
                require(re.fullmatch(r"native:process_[1-9][0-9]*", ref) and ref not in self.process_nodes,
                        "invalid or duplicate process proof node")
                require(len(inputs) == len(set(inputs)) and set(inputs) <= self.process_nodes.keys(),
                        "unknown process proof input")
                require(row["captured_step_id"] == 0 or row["captured_step_id"] in steps,
                        "unknown process capture step")
                roles = row.get("roles", {})
                allowed_roles = {
                    "action_bit": {"statement", "data", "target"},
                    "branch_control": {"control"}, "temporary_route": set(),
                    "untracked_action": set(),
                }
                require(row["operation"] in allowed_roles and set(roles) <= allowed_roles[row["operation"]],
                        "process proof operation and roles differ")
                require(set(roles) <= {"statement", "data", "control", "target"},
                        "unknown process source role")
                expected = set().union(*(set(v) for v in roles.values()),
                                       *(set(self.process_nodes[r]["origins"]) for r in inputs))
                require(set(row["origins"]) == expected, "process proof origins differ from inputs and roles")
                require(isinstance(row["complete"], bool) and
                        (not row["complete"] or all(self.process_nodes[r]["complete"] for r in inputs)),
                        "process proof incorrectly upgrades incomplete inputs")
                precise = row["complete"] and "candidate" not in roles and all(
                    self.process_nodes[r]["precision"] == "process_action" for r in inputs)
                proof_precision = "process_action" if precise else "process_signal_candidate"
                require(row.get("precision", proof_precision) == proof_precision, "process node incorrectly claims precision")
                self.process_nodes[ref] = {**row, "precision": proof_precision}
                self.events.append(row)
                for role, sources in roles.items():
                    self.edges += [{"from": source, "to": ref, "event_id": sequence,
                                    "capture": row["operation"], "role": role, "precision": proof_precision}
                                   for source in sources]
                self.edges += [{"from": r, "to": ref, "event_id": sequence,
                                "capture": row["operation"], "precision": proof_precision} for r in inputs]
            elif kind == "process_action_detail":
                require(row["ref"] not in self.process_actions and
                        re.fullmatch(r"native:action_[1-9][0-9]*", row["ref"]), "invalid process action identity")
                require(len(row["lhs"]) == len(row["rhs"]) == len(row["bits"]) and
                        len(set(row["bits"])) == len(row["bits"]), "process action bit widths differ")
                for i, ref in enumerate(row["bits"]):
                    require(ref in self.process_nodes and self.process_nodes[ref].get("action") == row["ref"] and
                            self.process_nodes[ref].get("offset") == i, "process action bit identity differs")
                require(set(row["origins"]) == set().union(*(set(self.process_nodes[r]["origins"]) for r in row["bits"])),
                        "process action sources differ from bit proofs")
                self.process_actions[row["ref"]] = row
                self.events.append(row)
            elif kind == "process_discard":
                require(row["inputs"] and set(row["inputs"]) <= self.process_nodes.keys(), "unknown discarded process action")
                require(row["reason"] in {"frontend_overwritten", "proc_prune_overwritten", "proc_rmdead_unreachable", "proc_clean_unreachable"},
                        "unknown process discard reason")
                require(all(self.process_nodes[r]["operation"] == "action_bit" for r in row["inputs"]) and
                        set(row["origins"]) == set().union(*(set(self.process_nodes[r]["origins"]) for r in row["inputs"])),
                        "process discard sources differ from action bits")
                self.process_discards.append(row)
                self.events.append(row)
            elif kind == "process_binding":
                old, out, inputs = row["input"], row["ref"], row["inputs"]
                require(old in live and self.objects[old]["precision"] == "process_pending", "process binding requires a pending live cell")
                stem, version = old.rsplit("@", 1)
                require(out == stem + "@" + str(int(version) + 1) and out not in self.objects, "invalid process binding version")
                require(set(inputs) <= self.process_nodes.keys(), "unknown process cell proof input")
                require(set(row["origins"]) == set().union(*(set(self.process_nodes[r]["origins"]) for r in inputs)),
                        "process cell origins differ from proof")
                require(row["precision"] in {"process_action", "process_signal_candidate"} and
                        (row["precision"] != "process_action" or all(self.process_nodes[r]["precision"] == "process_action" for r in inputs)),
                        "process binding incorrectly claims action precision")
                obj = deepcopy(self.objects[old])
                obj.update(row)
                self.objects[out] = obj
                live.remove(old); retired.add(old); live.add(out)
                self.events.append(row)
                self.edges += [{"from": r, "to": out, "event_id": sequence,
                                "capture": row["operation"], "precision": row["precision"]} for r in [old, *inputs]]
            elif kind == "process_connection_binding":
                ref, inputs = row["ref"], row["inputs"]
                require(ref in self.assignments and ref not in self.process_connection_proofs and
                        set(inputs) <= self.process_nodes.keys(), "invalid process connection proof")
                require(set(row["origins"]) == set(self.assignments[ref]["origins"]) ==
                        set().union(*(set(self.process_nodes[r]["origins"]) for r in inputs)),
                        "process connection sources differ from proof")
                require(self.assignments[ref]["precision"] != "process_action" or
                        all(self.process_nodes[r]["precision"] == "process_action" for r in inputs),
                        "process connection incorrectly claims action precision")
                self.process_connection_proofs.add(ref)
                self.events.append(row)
                self.edges = [e for e in self.edges if e["to"] != ref]
                self.edges += [{"from": r, "to": ref, "event_id": sequence,
                                "capture": "process_connection", "precision": self.assignments[ref]["precision"]} for r in inputs]
            elif kind in {"cell_state", "merge", "cell_contribution"}:
                inputs = [row["input"]] if kind == "cell_state" else row["inputs"]
                require(len(inputs) == (1 if kind == "cell_state" else 2) and len(set(inputs)) == len(inputs),
                        "invalid native transformation arity")
                require(set(inputs) <= live, "native transformation input is not live")
                out = row["ref"] if kind == "cell_state" else row["output"]
                stem, version = inputs[0].rsplit("@", 1)
                require(out == stem + "@" + str(int(version) + 1), "invalid native cell version")
                require(out not in self.objects, "duplicate native output reference")
                expected = set().union(*(set(self.objects[r]["origins"]) for r in inputs))
                require(set(row["origins"]) == expected, "native source propagation does not match event inputs")
                expected_precision = self.objects[inputs[0]]["precision"]
                if kind in {"merge", "cell_contribution"}:
                    other = self.objects[inputs[1]]["precision"]
                    if conservative_merge:
                        require(expected_precision in precision_rank and other in precision_rank, "unknown native merge precision")
                        expected_precision = max((expected_precision, other), key=precision_rank.__getitem__)
                    elif other != "cell":
                        expected_precision = other
                require(row["precision"] == expected_precision, "native transformation changed association precision")
                if kind == "cell_state" and self.objects[inputs[0]].get("abc_node") and "state" not in self.objects[inputs[0]]:
                    prior = self.objects[inputs[0]]
                    node = self.abc_imports[prior["abc_invocation"]].get(prior["abc_node"])
                    require(node is not None and row["state"]["connections"] == node["target_ports"], "ABC cell import pin mapping differs")
                if kind == 'cell_state' and row['capture']=='flatten_copy':
                    prior = self.objects[inputs[0]]
                    event = self.flatten_events[prior['flatten_event']]
                    original = self.objects[prior['cloned_from']]['state']
                    expected = {p:self.flatten_signal(bs,event) for p,bs in original['connections'].items()}
                    require(row['state']['module']==event['module'] and row['state']['type']==original['type'] and
                            row['state']['parameters']==original['parameters'] and row['state']['connections']==expected,
                            'flatten cell pins differ from actual wire mapping')
                obj = deepcopy(self.objects[inputs[0]])
                obj.update(row)
                obj["ref"] = out
                self.objects[out] = obj
                live.remove(inputs[0])
                retired.add(inputs[0])
                live.add(out)
                self.events.append(row)
                self.edges += [{"from": source, "to": out, "event_id": sequence,
                                "capture": row["operation"] if kind == "cell_contribution" else "instrumented_merge" if kind == "merge" else row["capture"],
                                "precision": row["precision"]} for source in inputs]
            elif kind == "cell_remove":
                require(row["ref"] in live, "native removal of non-live object")
                live.remove(row["ref"])
                retired.add(row["ref"])
                self.events.append(row)
                self.removals.append(row)
            elif kind == "assignment":
                require(row["ref"] not in self.assignments, "duplicate native connection")
                require(len(row["lhs"]) == len(row["rhs"]), "native assignment width mismatch")
                cloned_from = row.get("cloned_from")
                if cloned_from:
                    require(cloned_from in self.assignments, "unknown cloned connection")
                    original = self.assignments[cloned_from]
                    if row.get('flatten_event'):
                        self.check_flatten_clone(row,self.assignments)
                        event = self.flatten_events[row['flatten_event']]
                        require(row.get('source_offsets',original['offsets'])==original['offsets'], 'flatten source offsets differ')
                        require(row['lhs']==self.flatten_signal(original['lhs'],event) and
                                row['rhs']==self.flatten_signal(original['rhs'],event), 'flatten connection pin mapping differs')
                    require((bool(row.get('flatten_event')) or row["origins"] == original["origins"]) and row["precision"] == original["precision"] and
                            len(row["lhs"]) == len(original["lhs"]), "cloned connection differs from input")
                self.assignments[row["ref"]] = {**row, "original_lhs": deepcopy(row["lhs"]),
                                                "offsets": list(range(len(row["lhs"]))), "revision": 0}
                self.events.append(row)
                inputs = row.get("inputs", [])
                if inputs:
                    require(set(inputs) <= self.objects.keys(), "unknown native connection derivation input")
                    require(set(row["origins"]) == set().union(*(set(self.objects[r]["origins"]) for r in inputs)),
                            "native connection origins differ from derivation inputs")
                if row.get("abc_invocation"):
                    region = invocations.get(row["abc_invocation"])
                    require(region is not None and inputs == region["inputs"] and row["precision"] == "abc_region_candidate",
                            "native ABC connection does not match its region")
                self.edges += [{"from": source, "to": row["ref"], "event_id": sequence,
                                "capture": 'flatten_connection_clone' if row.get('flatten_event') else "hierarchy_parameter_clone" if cloned_from else row.get("operation") or "assignment_generation", "precision": row["precision"]}
                               for source in (row['origins'] if row.get('flatten_event') else [cloned_from] if cloned_from else inputs or row["origins"])]
            elif kind == "connection_state":
                require(row["ref"] in self.assignments, "unknown connection migration")
                obj = self.assignments[row["ref"]]
                require(row["revision"] == obj["revision"] + 1, "invalid connection revision")
                require(len(row["lhs"]) == len(row["rhs"]) == len(row["offsets"]), "connection migration width mismatch")
                require(row["offsets"] == sorted(set(row["offsets"])) and set(row["offsets"]) <= set(obj["offsets"]),
                        "connection migration introduced unknown offsets")
                require(row["reason"] in {"opt_clean_alias_representative", "signal_removed", "module_removed", "module_renamed"}, "unknown connection migration reason")
                obj.update(row)
                self.events.append(row)
            elif kind == "techmap_bit_map":
                require(row["input"] in self.objects and row["ref"] in live, "unknown techmap bit map endpoint")
                source, target = self.objects[row["input"]], self.objects[row["ref"]]
                require(target.get("operation") == "techmap" and row["input"] in target.get("inputs", []), "techmap bit map lacks derivation")
                for link in row["links"]:
                    for obj, port, offset in ((source, link["source_port"], link["source_offset"]), (target, link["port"], link["offset"])):
                        require(obj["state"]["port_directions"].get(port) == "output" and 0 <= offset < len(obj["state"]["connections"][port]), "invalid techmap output bit")
                    require(link["source_signal"] == [source["state"]["connections"][link["source_port"]][link["source_offset"]]] and
                            link["target_signal"] == [target["state"]["connections"][link["port"]][link["offset"]]], "techmap bit map pin correspondence differs")
                self.techmap_bits.append(row)
                self.events.append(row)
            elif kind == "abc_import_node":
                iid = row["invocation_id"]
                require(iid in invocations, "ABC import has unknown invocation")
                nodes = self.abc_imports.setdefault(iid, {})
                require(row["node"] not in nodes, "duplicate ABC imported node")
                nodes[row["node"]] = row
                self.events.append(row)
            elif kind == "abc_region":
                require(row.get("internal_trace_schema") in {None, 1, 2, 3, 4}, "unsupported ABC internal trace schema")
                if "abc_internal_v4" in self.records[0].get("features", []):
                    require(row.get("internal_trace_schema") == 4, "missing declared ABC internal v4 evidence")
                elif "abc_internal_v3" in self.records[0].get("features", []):
                    require(row.get("internal_trace_schema") == 3, "missing declared ABC internal v3 evidence")
                elif "abc_internal_v2" in self.records[0].get("features", []):
                    require(row.get("internal_trace_schema") == 2, "missing declared ABC internal v2 evidence")
                elif "abc_internal_v1" in self.records[0].get("features", []):
                    require(row.get("internal_trace_schema") == 1, "missing declared ABC internal evidence")
                require(set(row["inputs"]) <= retired, "ABC region includes non-retired input")
                require(set(row["origins"]) == set().union(*(set(self.objects[r]["origins"]) for r in row["inputs"])),
                        "ABC region origins differ from extracted inputs")
                require(row["precision"] == "abc_region_candidate", "ABC region cannot claim cell precision")
                if "invocation_id" in row:
                    iid = row["invocation_id"]
                    require(iid not in invocations, "duplicate ABC invocation")
                    extracted = {r["ref"] for r in self.removals if r.get("abc_invocation") == iid and r["step_id"] == row["step_id"]}
                    require(set(row["inputs"]) == extracted and len(row["inputs"]) == len(extracted),
                            "ABC region differs from actual extracted members")
                    require(not set(row["input_ports"]).intersection(row["output_ports"]), "ABC boundary directions overlap")
                    for key in ("input_ports", "output_ports"):
                        require(len(row[key]) == len(set(row[key])) and
                                all(isinstance(i, int) and 0 <= i < len(row["signals"]) for i in row[key]), "invalid ABC boundary index")
                    expected_artifacts = {"input.blif", "output.blif", "script"}
                    if row.get("internal_trace_schema") in {1, 2, 3, 4}: expected_artifacts.add("input.blif.flow.jsonl")
                    require(set(row["artifacts"]) == expected_artifacts and
                            all(Path(p).name == p for p in row["artifacts"].values()), "invalid ABC artifact paths")
                    invocations[iid] = row
                self.regions.append(row)
                self.events.append(row)
            elif kind in {"checkpoint", "run_end"}:
                require(len(row["cells"]) == len(set(row["cells"])) and set(row["cells"]) == live,
                        "native snapshot does not match lifecycle replay")
                if "connections" in row:
                    require(row["connections"] == {r: obj["revision"] for r, obj in self.assignments.items()},
                            "native connection snapshot differs from replay")
                if kind == "run_end":
                    require(not stack and sequence == len(self.records), "unclosed native steps or premature end")
            elif kind == "coverage_gap":
                self.gaps.append(row["reason"])
            elif kind == "step_end":
                require(bool(stack), "unmatched native step end")
                stack.pop()
            elif kind == "process_action":
                self.events.append(row)
            elif kind not in {"run_begin", "step_begin"}:
                raise TraceError(f"unknown native record type: {kind}")
        self.final = live
        require(all("state" in self.objects[r] for r in live), "final native cell has no state")
        require(all(self.objects[r]["precision"] != "process_pending" for r in live), "unbound process cell at endpoint")
        require(all(r in self.process_connection_proofs for r, obj in self.assignments.items()
                    if obj["precision"] == "process_action" and not obj.get("inputs") and not obj.get("cloned_from")), "missing process connection proof")

        # Effective query associations must never mutate the captured log.
        self.objects = deepcopy(self.objects)
        from .abc_provenance import refine
        refine(self)

    @staticmethod
    def flatten_signal(bits, event):
        return [{'wire':event['wire_map'][b['wire']], 'offset':b['offset']} if isinstance(b,dict) else b for b in bits]

    def check_flatten_clone(self,row,objects):
        require(row['flatten_event'] in self.flatten_events and row.get('cloned_from') in objects,
                'flatten clone input is unknown')
        original = objects[row['cloned_from']]
        sources = [self.sources[s] for s in row['origins']]
        require(all(s.get('flatten_event')==row['flatten_event'] for s in sources) and
                {s.get('cloned_from') for s in sources}==set(original['origins']) and
                row['precision']==original['precision'], 'flatten clone sources differ from original')

    def compare_abc_artifacts(self, directory):
        internal = {}
        self.abc_internal = []
        for region in self.regions:
            if "artifacts" not in region:
                continue  # Older v2 pilot logs did not archive ABC files.
            files = {k: Path(directory) / v for k, v in region["artifacts"].items()}
            require(all(p.is_file() and p.stat().st_size > 0 for p in files.values()), "missing ABC evidence artifact")
            from .abc_provenance import validate_artifacts
            validate_artifacts(self, region, files)
            if region.get("internal_trace_schema") in {1, 2, 3, 4}:
                from .abc_internal import analyze
                proof, summary = analyze(files["input.blif.flow.jsonl"], files["script"].read_text(), region,
                                         self.abc_imports.get(region["invocation_id"], {}), files["input.blif"].read_text())
                require(summary["schema"] == region["internal_trace_schema"], "ABC internal trace schema differs from declaration")
                internal[region["invocation_id"]] = proof
                self.abc_internal.append({"invocation_id": region["invocation_id"], **summary})
            lines = files["input.blif"].read_text().splitlines()
            for directive, field in ((".inputs", "input_ports"), (".outputs", "output_ports")):
                ports = next((line.split()[1:] for line in lines if line.startswith(directive)), None)
                expected = [f"ys__n{i}" for i in region[field]]
                if directive == ".inputs" and not expected:
                    expected = ["dummy_input"]
                require(ports == expected, "ABC boundary differs from exported network")
        if internal:
            from .abc_provenance import refine
            refine(self, internal)

    def compare_netlist(self, path):
        modules = json.loads(Path(path).read_text())["modules"]
        for module in modules.values():
            require_combinational(module, modules)
        expected = {(unescape(self.objects[r]["state"]["module"]), self.objects[r]["state"]["name"]): r for r in self.final}
        actual = {(m, n) for m, module in modules.items() for n in module["cells"]}
        require({(m, unescape(n)) for m, n in expected} == actual, "native final cell inventory differs from netlist")
        for (name, cell_name), ref in expected.items():
            module = modules[name]
            cell = module["cells"][unescape(cell_name)]
            state = self.objects[ref]["state"]
            require(cell["type"] == unescape(state["type"]), "native cell type differs from netlist")
            require(cell["parameters"] == {unescape(k): v for k, v in state["parameters"].items()}, "native cell parameters differ")
            connections = {unescape(p): [resolve_bit(b, module) for b in bits] for p, bits in state["connections"].items()}
            require(connections == cell["connections"], "native cell connections differ")
            require(cell["port_directions"] == {unescape(k): v for k, v in state["port_directions"].items()}, "native cell directions differ")
        for row in self.assignments.values():
            if not row["lhs"]:
                continue
            module = modules[unescape(row["module"])]
            require([resolve_bit(b, module) for b in row["lhs"]] == [resolve_bit(b, module) for b in row["rhs"]],
                    "recorded frontend connection is no longer valid at endpoint")


def run_native(args):
    require(args.flow in {"synth", "m1", "m1-opt"}, "unsupported native flow")
    out = Path(args.out).resolve()
    require(not out.exists(), f"output directory already exists: {out}")
    native, stock = shutil.which(args.native_yosys), shutil.which(args.yosys)
    require(native is not None, "native Yosys is missing; run make native first")
    require(stock is not None, f"stock Yosys is missing: {args.yosys}")
    versions = {key: subprocess.check_output([binary, "-V"], text=True).strip()
                for key, binary in (("native", native), ("stock", stock))}
    require(all(PIN in v for v in versions.values()), f"native flows are pinned to {PIN}: {versions}")
    require(Path(native).resolve() != Path(stock).resolve(), "--yosys must be an independent stock executable")
    sources = [Path(p).resolve(strict=True) for p in args.source]
    includes = [Path(p).resolve(strict=True) for p in args.include]
    for p in [out, *sources, *includes]:
        token(p)
    require(all(p.is_dir() and not any(c.isspace() for c in str(p)) for p in includes), "unsupported include directory")
    top = identifier(args.top)
    defines = []
    for definition in args.define:
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(=[A-Za-z0-9_'()+*/.<>=!~&|^?:+-]+)?", definition) is not None,
                f"unsupported macro definition: {definition}")
        defines.append("-D" + definition)
    parameters = []
    for parameter in args.param:
        name, sep, value = parameter.partition("=")
        require(sep and re.fullmatch(r"[0-9][A-Za-z0-9_'?+-]*", value) is not None, "--param expects NAME=numeric_value")
        parameters.extend(["-chparam", identifier(name), value])
    read = "read_verilog -sv -defer " + " ".join(defines + ["-I" + str(p) for p in includes] + [token(p) for p in sources])
    frontend = read + "\nhierarchy -check -top " + top + " " + " ".join(parameters)
    flatten = getattr(args,'flatten',False)
    require(not flatten or args.flow=='synth', '--flatten requires the synth flow')
    passes = [f"synth -top {top}" + (' -flatten' if flatten else '')] if args.flow == "synth" else OPT_PASSES if args.flow == "m1-opt" else PASSES
    gaps = deepcopy(GAPS[:2])
    if args.flow == "m1":
        gaps += deepcopy(GAPS[2:])
    else:
        gaps.append({"stage": "optimizations", "coverage": "partial", "reason":
                     "已接入 opt_expr 替代、opt_reduce 子归约合并和 opt_muxtree 裁剪；跨对象常量、控制依赖与复杂组合优化仍有缺口。"})
        if args.flow == "synth":
            gaps.append({"stage": "ABC", "coverage": "internal_partial", "reason":
                         "固定 ABC 已记录内部网络、实际 AIG 替换、Nf 选中切点及导出对象转换；大区域及宽独立边界使用有资源预算的符号功能验证。未覆盖全部内部重写，未证明对象继续返回输出锥或区域候选。"})
        else:
            gaps.append({"stage": "remaining synth / ABC", "coverage": "not_executed", "reason": "本次只执行基础优化检查点。"})
    pipeline = "\n".join(passes)
    run_id = uuid.uuid4().hex
    out.mkdir(parents=True)
    manifest = {"schema_version": 2, "run_id": run_id, "status": "running", "flow": args.flow, "input_scope": "combinational",
                "endpoint": "; ".join(passes), "coverage": "partial", "coverage_gaps": gaps,
                "safe_for_incremental_reuse": False, "inputs": [], "artifacts": {}, "timings_seconds": {},
                "configuration": {"top": top, "sources": list(map(str, sources)), "include_dirs": list(map(str, includes)),
                                  "defines": args.define, "parameters": args.param, "passes": passes, "flatten":flatten},
                "tool": {"versions": versions, "native_executable": str(Path(native).resolve()),
                         "native_sha256": digest(native), "stock_executable": str(Path(stock).resolve())}}
    write_json(out / "manifest.json", manifest)

    def path(name):
        return token(out / name)

    from .persistent_index import artifact_inventory, require_evidence_unchanged, write_index
    verified_artifacts = {}

    def freeze_evidence():
        nonlocal verified_artifacts
        current = artifact_inventory(out)
        require_evidence_unchanged(verified_artifacts, current)
        verified_artifacts = current

    def execute(name, binary, script, dependencies=False):
        target = out / (name + ".ys")
        target.write_text(script + "\n")
        command = [binary, "-Q", "-T"]
        if dependencies:
            command += ["-E", str(out / "frontend.d")]
        start = time.perf_counter()
        with (out / (name + ".log")).open("w") as log:
            env = os.environ.copy()
            if name == "traced": env["YOSYS_FLOW_ABC_TRACE"] = "1"
            else: env.pop("YOSYS_FLOW_ABC_TRACE", None)
            result = subprocess.run(command + ["-s", str(target)], stdout=log, stderr=subprocess.STDOUT, env=env)
        manifest["timings_seconds"][name] = time.perf_counter() - start
        require(result.returncode == 0, f"{name} failed:\n" + (out / (name + ".log")).read_text()[-4000:])
        # Freeze each generated artifact before any subsequent replay or proof
        # consumes it; later stages may add files, never alter earlier evidence.
        freeze_evidence()

    try:
        start_ns = time.time_ns()
        execute("traced", native, f"flow_start {path('yosys.jsonl')} {run_id}\n{frontend}\nflow_hierarchy\n"
                f"write_rtlil {path('frontend.il')}\n{pipeline}\ncheck -assert\n"
                f"write_json {path('after.json')}\nwrite_rtlil {path('after.il')}\nflow_stop\nwrite_verilog {path('after.v')}", True)
        after = json.loads((out / "after.json").read_text())["modules"]
        for module in after.values(): require_combinational(module,after)
        validation_start = time.perf_counter()
        trace = NativeTrace(out / "yosys.jsonl")
        trace.compare_netlist(out / "after.json")
        trace.compare_abc_artifacts(out)
        from .hierarchy import instance_view, electrical_design
        view = instance_view(trace,after,top)
        freeze_evidence()
        manifest["timings_seconds"]["record_validation"] = time.perf_counter() - validation_start
        manifest["tool"]["abc"] = []
        for executable in sorted({r["executable"] for r in trace.regions if "executable" in r}):
            binary = shutil.which(executable)
            require(binary is not None, f"recorded ABC executable missing: {executable}")
            require(Path(binary).stat().st_mtime_ns <= start_ns, "ABC executable changed during run")
            manifest["tool"]["abc"].append({"executable": str(Path(binary).resolve()), "sha256": digest(binary),
                "version_output": subprocess.check_output([binary, "-c", "version"], text=True).strip()})
        # Yosys lists every file opened by its frontend, including includes.
        # Use its native input_files set via a file list emitted by the recorder.
        input_record = trace.records[-1]
        dependencies = set(sources) | {Path(p).resolve(strict=True) for p in input_record.get("input_files", [])}
        dependencies.discard(out / "traced.ys")
        for file in sorted(dependencies):
            require(file.stat().st_mtime_ns <= start_ns, f"input changed during frontend: {file}")
            manifest["inputs"].append({"path": str(file), "sha256": digest(file), "size_bytes": file.stat().st_size})
        execute("baseline", stock, f"{frontend}\n{pipeline}\ncheck -assert\nwrite_json {path('baseline.json')}\nwrite_rtlil {path('baseline.il')}")
        require(after == json.loads((out / "baseline.json").read_text())["modules"], "native result differs from stock flow")
        # Only the disposable proof designs ignore keep_hierarchy. Synthesis
        # artifacts and stock comparison continue to respect the user's flags.
        proof_flatten = f'hierarchy -top {top}\nsetattr -mod -unset keep_hierarchy\nsetattr -unset keep_hierarchy\nflatten\ncheck -assert\n'
        proof_prepare = proof_flatten if len(after)>1 else ''
        execute("equivalence", stock, f"read_rtlil {path('baseline.il')}\n{proof_prepare}rename {top} gold\ndesign -stash gold_design\n"
                f"read_rtlil {path('after.il')}\n{proof_prepare}rename {top} gate\ndesign -stash gate_design\n"
                "design -copy-from gold_design -as gold gold\ndesign -copy-from gate_design -as gate gate\n"
                "equiv_make gold gate equiv\nhierarchy -top equiv\n"
                "equiv_simple\nequiv_status -assert")
        if view.view_rule=='electrical_instance_expansion_v1':
            write_json(out/'instance-view.json',electrical_design(view,top))
            execute('instance_equivalence',stock,
                f"read_rtlil {path('after.il')}\n{proof_flatten}rename {top} gold\ndesign -stash gold_design\n"
                f"read_json {path('instance-view.json')}\nhierarchy -top {top}\ncheck -assert\nrename {top} gate\ndesign -stash gate_design\n"
                "design -copy-from gold_design -as gold gold\ndesign -copy-from gate_design -as gate gate\n"
                "equiv_make gold gate equiv\nhierarchy -top equiv\nequiv_simple\nequiv_status -assert")
        for row in manifest["inputs"]:
            require(digest(row["path"]) == row["sha256"], f"input changed during run: {row['path']}")
        for tool in manifest["tool"]["abc"]:
            require(digest(tool["executable"]) == tool["sha256"], "ABC executable changed during validation")
        manifest["coverage_gaps"] += [{"stage": "native hooks", "coverage": "gap", "reason": g} for g in trace.gaps]
        validation = {"valid": True, "run_id": run_id, "schema_version": 2, "coverage": "partial",
                      "endpoint": manifest["endpoint"], "safe_for_incremental_reuse": False,
                      "checks": {"cell_lifecycle_replay": True, "source_references": True, "final_cell_netlist_match": True,
                                 "recorded_connections_match": True, "stock_flow_exact_match": True,
                                 "abc_boundary_matches_exported_network": True,
                                 "abc_internal_evidence_validated": len(trace.abc_internal) == len(trace.regions),
                                 "stock_to_traced_equivalence": True, "inputs_unchanged": True},
                      "counts": {"sources": len(trace.sources), "final_cells": len(trace.final), "assignments": len(trace.assignments),
                                 "abc_invocations": len(trace.regions)},
                      "full_transformation_coverage": False, "exact_proc_cell_attribution": False}
        if view.view_rule=='electrical_instance_expansion_v1': validation['checks']['instance_view_equivalence']=True
        write_json(out / "validation.json", validation)
        freeze_evidence()
        index_started = time.perf_counter()
        manifest["verified_index"] = write_index(out, manifest, view, verified_artifacts=verified_artifacts)
        manifest["timings_seconds"]["verified_index_build"] = time.perf_counter() - index_started
        manifest["artifacts"] = {p.name: {"sha256": digest(p), "size_bytes": p.stat().st_size}
                                 for p in sorted(out.iterdir()) if p.is_file() and p.name != "manifest.json"}
        manifest["status"] = "complete"
        write_json(out / "manifest.json", manifest)
        return {"run_directory": str(out), "status": "complete", **validation}
    except BaseException as exc:
        manifest.update(status="failed", error=str(exc))
        write_json(out / "manifest.json", manifest)
        raise


def load_native(directory, manifest, timings=None, *, use_index=True):
    from .persistent_index import INDEX_NAME, read_index, build_lookup
    timings = timings if timings is not None else {}
    started = time.perf_counter()
    require(manifest.get("status") == "complete", "native run is incomplete or failed")
    required = {"yosys.jsonl", "after.json", "baseline.json", "validation.json", "equivalence.log"}
    require(required <= manifest["artifacts"].keys(), "native manifest is missing required artifacts")
    for name, meta in manifest["artifacts"].items():
        require(Path(name).name == name, "invalid native artifact path")
        # A missing optional index can be rebuilt from the complete raw archive.
        # Existing but corrupted indexes fail closed, just like raw artifacts.
        if name == INDEX_NAME and not (directory / name).is_file():
            continue
        require((directory / name).is_file(), f"missing native artifact: {name}")
        require(digest(directory / name) == meta["sha256"], f"artifact hash mismatch: {name}")
    timings["artifact_hash_check"] = time.perf_counter() - started
    started = time.perf_counter()
    trace, index_status = read_index(directory, manifest, NativeTrace) if use_index else (None, "disabled")
    timings["verified_index_load"] = time.perf_counter() - started
    timings["verified_index_status"] = index_status
    if trace is not None:
        # Retain cheap archive and electrical-view checks. Expensive lifecycle
        # replay and ABC proof have already established this exact indexed state.
        validation = _check_saved_reference(directory, manifest)
        _check_instance_artifact(directory, manifest, trace, validation)
        timings["record_replay_and_netlist_check"] = 0.0
        timings["abc_artifact_check_and_proof"] = 0.0
        timings["instance_view_reconstruction"] = 0.0
        timings["saved_reference_check"] = time.perf_counter() - started - timings["verified_index_load"]
        return manifest, trace, validation
    started = time.perf_counter()
    trace = NativeTrace(directory / "yosys.jsonl")
    require(trace.records[0]["run_id"] == manifest["run_id"], "native run ID mismatch")
    trace.compare_netlist(directory / "after.json")
    for region in trace.regions:
        require(set(region.get("artifacts", {}).values()) <= manifest["artifacts"].keys(), "ABC evidence missing from artifact manifest")
    timings["record_replay_and_netlist_check"] = time.perf_counter() - started
    started = time.perf_counter()
    trace.compare_abc_artifacts(directory)
    timings["abc_artifact_check_and_proof"] = time.perf_counter() - started
    started = time.perf_counter()
    validation = _check_saved_reference(directory, manifest)
    timings["saved_reference_check"] = time.perf_counter() - started
    from .hierarchy import instance_view
    started=time.perf_counter()
    trace = instance_view(trace,json.loads((directory/'after.json').read_text())['modules'],manifest['configuration']['top'])
    _check_instance_artifact(directory, manifest, trace, validation)
    build_lookup(trace)
    timings['instance_view_reconstruction']=time.perf_counter()-started
    return manifest, trace, validation


def _check_saved_reference(directory, manifest):
    require(json.loads((directory / "after.json").read_text())["modules"] ==
            json.loads((directory / "baseline.json").read_text())["modules"], "native stock comparison mismatch")
    validation = json.loads((directory / "validation.json").read_text())
    require(validation.get("valid") is True and validation.get("run_id") == manifest["run_id"], "native validation failed")
    return validation


def _check_instance_artifact(directory, manifest, trace, validation):
    from .hierarchy import electrical_design
    if validation.get('checks',{}).get('instance_view_equivalence'):
        require('instance-view.json' in manifest['artifacts'] and 'instance_equivalence.log' in manifest['artifacts'],
                'instance view proof artifacts missing')
        require(electrical_design(trace,manifest['configuration']['top'])==json.loads((directory/'instance-view.json').read_text()),
                'reconstructed instance view differs from the verified artifact')
    else:
        actual = json.loads((directory / 'after.json').read_text())['modules'][manifest['configuration']['top']]
        actual['cells'] = {n:c for n,c in actual['cells'].items() if c['type'] != '$scopeinfo'}
        require(electrical_design(trace, manifest['configuration']['top']) == {'modules': {manifest['configuration']['top']: actual}},
                'indexed electrical view differs from the recorded final netlist')


def query_native(args, manifest, trace):
    reverse = args.object is not None
    expression_selection = False
    module_name = manifest["configuration"]["top"]
    instance = unescape(args.instance) if args.instance else None
    if instance:
        require(instance in trace.instance_catalog, f"unknown instance: {instance}; available: {', '.join(trace.instance_catalog)}")
    object_ref = args.object
    if reverse and object_ref not in trace.objects and object_ref not in trace.assignments:
        matches = [r for objects in (trace.objects,trace.assignments) for r,o in objects.items()
                   if o.get('definition_ref')==object_ref and (not instance or instance in o.get('instance_aliases',[o.get('instance')]))]
        require(len(matches)==1, 'definition object is ambiguous or absent; use an instance-qualified object reference or --instance')
        object_ref = matches[0]
    if reverse:
        require(object_ref in trace.objects or object_ref in trace.assignments, f"unknown object: {object_ref}")
        obj = trace.objects.get(object_ref, trace.assignments.get(object_ref))
        seeds = set(obj["origins"])
    else:
        require(args.line is not None and args.line > 0, "source query requires positive --line")
        end_line = args.end_line or args.line
        column, end_column = getattr(args, "column", None), getattr(args, "end_column", None)
        require(end_line >= args.line and (column is None or column > 0), "invalid source range")
        require(end_column is None or (column is not None and end_column > 0), "--end-column requires a positive --column")
        require(column is None or (end_line, end_column or column) >= (args.line, column), "reversed source range")
        source = Path(args.source).resolve()
        start, end = (args.line, column or 0), (end_line, (end_column or column) if column else 10**9)
        seeds = {sid for sid, row in trace.sources.items() if Path(row["file"]).resolve() == source and
                 (row["line"], row["column"]) <= end and (row["end_line"], row["end_column"]) > start}
        if column is not None:
            # A point inside a nested expression also overlaps the containing
            # assignment. Prefer the smallest operator span, otherwise a column
            # query would silently expand to the entire statement.
            operators = [sid for sid in seeds if trace.sources[sid]["kind"] not in
                         {"AST_IDENTIFIER", "AST_CONSTANT", "AST_ASSIGN", "AST_ASSIGN_EQ", "AST_ASSIGN_LE"} and
                         (trace.sources[sid]["line"], trace.sources[sid]["column"]) <= start and
                         (trace.sources[sid]["end_line"], trace.sources[sid]["end_column"]) > end]
            if operators:
                def extent(sid):
                    row = trace.sources[sid]
                    return row["end_line"] - row["line"], row["end_column"] - row["column"]
                smallest = min(map(extent, operators))
                seeds = {sid for sid in operators if extent(sid) == smallest}
                expression_selection = True
    if instance:
        seeds = {s for s in seeds if instance in trace.sources[s].get('instance_aliases',
                 [trace.sources[s].get('instance',module_name if len(trace.instance_catalog)==1 else None)])}
        if reverse: require(bool(seeds), 'object has no recorded sources in the selected instance')
    design = trace.design
    final = []
    lookup = getattr(trace, 'lookup_index', None)
    final_refs = trace.final
    if lookup is not None and not reverse:
        final_refs = set().union(*(lookup['sources_to_final'].get(s, []) for s in seeds))
    for ref in sorted(final_refs):
        row = trace.objects[ref]
        if (reverse and object_ref != ref) or (not reverse and not seeds.intersection(row["origins"])):
            continue
        state = row["state"]
        final.append({"ref": ref, "module": state["module"], "name": state["name"], "type": state["type"],
                      "precision": row["precision"], "origin_ids": row["origins"],
                      "all_origins": [trace.sources[s] for s in row["origins"]], "consumers": []})
        final[-1].update({k:row[k] for k in ('definition_ref','definition_module','definition_name','instance','instance_aliases') if k in row})
        selected = design["cells"][unescape(state["name"])]
        output_bits = {b for port, bits in selected["connections"].items()
                       if selected["port_directions"][port] == "output" for b in bits if isinstance(b, int)}
        consumers = final[-1]["consumers"]
        if lookup is not None:
            consumers.extend(lookup['consumer_rows'][i] for i in sorted({i for bit in output_bits
                for i in lookup['consumers'].get(bit, [])}))
            continue
        for name, cell in design["cells"].items():
            for port, bits in cell["connections"].items():
                if cell["port_directions"][port] == "input":
                    consumers.extend({"kind": "cell", "name": name, "port": port, "offset": i}
                                     for i, bit in enumerate(bits) if bit in output_bits)
        for name, port in design["ports"].items():
            if port["direction"] == "output":
                consumers.extend({"kind": "module_port", "name": name, "offset": i}
                                 for i, bit in enumerate(port["bits"]) if bit in output_bits)
    connections = []
    selected_output_bits = {bit for cell in final for port, bits in design["cells"][unescape(cell["name"])]["connections"].items()
                            if design["cells"][unescape(cell["name"])]["port_directions"][port] == "output" for bit in bits}
    for ref, row in trace.assignments.items():
        if (reverse and object_ref != ref) or (not reverse and not seeds.intersection(row["origins"])):
            continue
        lhs_bits = [resolve_bit(b, design) for b in row["lhs"]]
        if expression_selection and final and not all(b in selected_output_bits for b in lhs_bits):
            continue
        if lhs_bits:
            connections.append({**row, "lhs_bits": lhs_bits, "rhs_bits": [resolve_bit(b, design) for b in row["rhs"]]})
    changed = [row["path"] for row in manifest["inputs"]
               if not Path(row["path"]).is_file() or digest(row["path"]) != row["sha256"]]
    reachable, selected_edges = set(seeds), []
    for edge in trace.edges:
        if edge["from"] in reachable:
            reachable.add(edge["to"])
            selected_edges.append(edge)
    require(all(r["ref"] in reachable for r in final + connections), "selected object has no continuous source evidence path")
    # Include all supporting process inputs of selected objects, including
    # shared sources not selected by this source position.
    supporting = {r["ref"] for r in final + connections}
    for edge in reversed(trace.edges):
        if edge["to"] in supporting:
            supporting.add(edge["from"])
    proof_nodes = [r for ref, r in trace.process_nodes.items() if ref in supporting or ref in reachable]
    proof_actions = {r["action"] for r in proof_nodes if "action" in r}
    evidence_ids = {e["event_id"] for e in selected_edges}
    eliminated = [{**row, "origins": trace.objects[row["ref"]]["origins"]} for row in trace.removals
                  if seeds.intersection(trace.objects[row["ref"]]["origins"])]
    return {"run_id": manifest["run_id"], "direction": "reverse" if reverse else "forward", "matched": bool(seeds),
            "instance":instance, "instance_catalog":trace.instance_catalog, "netlist_view":trace.view_rule,
            "recorded_flatten_events":list(trace.flatten_events.values()),
            "seeds": sorted(seeds), "sources": [trace.sources[s] for s in sorted(seeds)],
            "final_cells": final, "final_connections": connections, "final_non_cell_objects": connections,
            "retired_cells": eliminated,
            "process_actions": [r for r in trace.process_actions.values() if r["ref"] in proof_actions or seeds.intersection(r["origins"])],
            "process_proof_nodes": proof_nodes,
            "process_discards": [r for r in trace.process_discards if seeds.intersection(r["origins"])],
            "action_level_refs": [r["ref"] for r in final if r["precision"] == "process_action"],
            "retired_connections": [r for r in trace.assignments.values() if not r["lhs"] and seeds.intersection(r["origins"])],
            "techmap_bit_maps": [r for r in trace.techmap_bits if r["ref"] in supporting],
            "abc_refinement_fallbacks": trace.abc_fallbacks,
            "abc_internal": getattr(trace, "abc_internal", []),
            "joint_cut_proven_refs": [r["ref"] for r in final if r["precision"] == "abc_internal_joint_cut"],
            "cut_proven_refs": [r["ref"] for r in final if r["precision"] == "abc_internal_cut"],
            "abc_associations": [r for r in trace.abc_associations if r["ref"] in supporting],
            "abc_regions": [r for r in trace.regions if seeds.intersection(r["origins"])],
            "resolution": "cells_found" if final else "connections_found" if connections else "source_not_mapped" if not seeds else "no_final_object_with_recorded_binding",
            "empty_result_means_no_effect": False,
            "association_precision": sorted({r["precision"] for r in final + connections}),
            "cell_level_refs": [r["ref"] for r in final if r["precision"] == "cell"],
            "candidate_cell_refs": [r["ref"] for r in final if r["precision"] not in {"cell", "process_action", "abc_internal_cut", "abc_internal_joint_cut"}],
            "end_to_end_exact": False,
            "edges": selected_edges, "events": [r for r in trace.events if r["seq"] in evidence_ids or
                (r["record_type"] == "connection_state" and r["ref"] in reachable) or
                (r["record_type"] in {"process_action", "process_action_detail", "process_discard"} and seeds.intersection(r["origins"]))],
            "coverage": manifest["coverage"], "coverage_gaps": manifest["coverage_gaps"], "endpoint": manifest["endpoint"],
            "safe_for_incremental_reuse": False, "changed_or_missing_base_inputs": changed,
            "note": "按转换记录及已验证的局部功能切点关联来源；abc_internal_cut 为单门切点证明；abc_internal_joint_cut 为多门整体对应，组内保留共享来源，不能逐门独占归属；二者不等于完整内部重写历史。process_signal_candidate、abc_output_cone_candidate 和 abc_region_candidate 均为候选，覆盖仍不完整。"}
