"""JSONL loading, chronological replay, and separate lineage/connectivity indexes."""
from __future__ import annotations

from collections import defaultdict, deque
import json
from pathlib import Path
import re


class TraceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise TraceError(message)


def unescape(name):
    return name[1:] if name.startswith("\\") else name


class Trace:
    def __init__(self, path):
        self.path = Path(path)
        self.records = []
        with self.path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    record = json.loads(line)
                    require(isinstance(record, dict), "record must be an object")
                except (ValueError, TypeError) as exc:
                    raise TraceError(f"{self.path}:{number}: {exc}") from exc
                self.records.append(record)
        self.objects = {}
        self.events = {}
        self.forward = defaultdict(list)
        self.backward = defaultdict(list)
        self.final = set()
        self.snapshots = []
        self._validate()
        for ref, obj in self.objects.items():
            # src attributes are observations, not instrumented AST lineage.
            for source in obj["source_ids"]:
                edge = {"from": source, "to": ref, "capture": "src_attribute", "event_id": None}
                self.forward[source].append(edge)
                self.backward[ref].append(edge)
        for event in self.events.values():
            for src in event["inputs"]:
                for dst in event["outputs"]:
                    edge = {"from": src, "to": dst, "capture": event["capture"],
                            "event_id": event["event_id"], "step_id": event["step_id"]}
                    self.forward[src].append(edge)
                    self.backward[dst].append(edge)

    def _validate(self):
        require(bool(self.records), "empty trace")
        require(self.records[0].get("record_type") == "run_begin", "missing run_begin")
        require(self.records[-1].get("record_type") == "run_end", "missing run_end (interrupted trace)")
        run = self.records[0].get("run_id")
        active, declared, used, versions, steps = set(), set(), set(), {}, set()
        current_step = None
        initialized = False
        for sequence, record in enumerate(self.records, 1):
            require(record.get("schema_version") == 1, "unsupported schema_version")
            require(record.get("sequence") == sequence, f"broken sequence at record {sequence}")
            require(record.get("run_id") == run and bool(run), "inconsistent run_id")
            kind = record.get("record_type")
            if kind == "run_begin":
                require(sequence == 1, "duplicate run_begin")
            elif kind == "object":
                ref, oid, version = record["ref"], record["object_id"], record["version"]
                require(ref == f"{oid}@{version}" and ref not in self.objects, "duplicate or invalid object ref")
                require(version == versions.get(oid, -1) + 1, f"noncontiguous version: {ref}")
                versions[oid] = version
                for src in record["source_ids"]:
                    require(src in self.objects and self.objects[src]["kind"] == "source_span", "invalid source ref")
                state = record["state"]
                signals = list(state.get("ports", {}).values())
                signals += [state[k] for k in ("lhs", "rhs") if k in state]
                for signal in signals:
                    for bit in signal:
                        if isinstance(bit, str):
                            require(bit in self.objects and self.objects[bit]["kind"] == "signal_bit", f"invalid bit ref {bit}")
                        else:
                            require(isinstance(bit, dict) and bit.get("constant") in "01xz-" and len(bit["constant"]) == 1,
                                    "invalid constant bit")
                self.objects[ref] = record
                if record["kind"] != "source_span":
                    declared.add(ref)
            elif kind == "step_begin":
                require(initialized and current_step is None, "nested step or missing initial snapshot")
                current_step = record["step_id"]
                require(current_step not in steps, "duplicate step ID")
                require(record.get("coverage") == "instrumented" and record.get("pass") == "opt_merge",
                        "unsupported step coverage")
                steps.add(current_step)
            elif kind == "transform":
                require(current_step is not None and record["step_id"] == current_step, "event outside its step")
                eid = record["event_id"]
                require(eid not in self.events, "duplicate event ID")
                inputs, outputs, retired = map(set, (record["inputs"], record["outputs"], record["retired"]))
                require(len(inputs) == len(record["inputs"]) and len(outputs) == len(record["outputs"]), "duplicate event member")
                require(inputs <= active, "input refers to inactive or missing object")
                require(outputs <= declared and not outputs & used, "output missing or already produced")
                require(retired <= inputs, "retired object is not an input")
                for ref in outputs:
                    obj = self.objects[ref]
                    if obj["version"]:
                        require(f'{obj["object_id"]}@{obj["version"] - 1}' in retired, "revision did not retire predecessor")
                self._check_merge(record)
                active -= retired
                active |= outputs
                used |= outputs
                self.events[eid] = record
            elif kind == "snapshot":
                refs = [o["ref"] for o in record["objects"]]
                require(len(refs) == len(set(refs)), "duplicate snapshot object")
                require(set(refs) <= declared, "snapshot references undeclared objects")
                if record["phase"] == "initial":
                    require(not initialized and current_step is None, "duplicate initial snapshot")
                    require(set(refs) == declared, "initial inventory omits objects")
                    initialized, active, used = True, set(refs), set(refs)
                require(set(refs) == active, f"replay differs from {record['phase']} snapshot")
                for obj in record["objects"]:
                    require(obj["state"] == self.objects[obj["ref"]]["state"], "snapshot state mismatch")
                self.snapshots.append(record)
            elif kind == "step_end":
                require(current_step is not None and current_step == record["step_id"], "unmatched step_end")
                require(self.snapshots[-1]["phase"] == "after" and self.snapshots[-1]["step_id"] == current_step,
                        "missing step after snapshot")
                current_step = None
            elif kind == "run_end":
                require(sequence == len(self.records) and current_step is None and initialized, "unfinished run")
                require(record.get("status") == "complete", "run is incomplete")
                require(self.snapshots[-1]["phase"] == "final", "missing final snapshot")
                require(declared == used, "unexplained object declarations")
            else:
                raise TraceError(f"unknown record_type: {kind}")
        self.final = active

    def _check_merge(self, event):
        require(event.get("operation") == "merge" and event.get("capture") == "instrumented", "unsupported event")
        inputs = [self.objects[r] for r in event["inputs"]]
        outputs = [self.objects[r] for r in event["outputs"]]
        require(len(inputs) == 2 and all(o["kind"] == "cell" for o in inputs), "merge requires two cells")
        require(set(event["retired"]) == set(event["inputs"]), "merge must retire both prior versions")
        cells = [o for o in outputs if o["kind"] == "cell"]
        conns = [o for o in outputs if o["kind"] == "connection"]
        require(len(cells) == 1 and len(cells) + len(conns) == len(outputs), "invalid merge outputs")
        removed, kept = inputs
        require(cells[0]["object_id"] == kept["object_id"], "kept cell identity changed")
        require(cells[0]["state"] == kept["state"], "unexplained kept-cell mutation")
        require(removed["state"]["type"] == kept["state"]["type"] and
                removed["state"]["parameters"] == kept["state"]["parameters"], "incompatible merged cells")
        expected = [{"lhs": removed["state"]["ports"][port], "rhs": kept["state"]["ports"][port]}
                    for port, direction in removed["state"]["port_directions"].items() if direction == "output"]
        require(sorted(map(canonical, expected)) == sorted(canonical(c["state"]) for c in conns), "missing or incorrect output redirection")
        changes = event["connection_changes"]
        require({c["connection"] for c in changes} == {c["ref"] for c in conns}, "connection change references disagree")
        for change in changes:
            require(change["before"]["driver"] == removed["ref"] and change["after"]["driver"] == cells[0]["ref"],
                    "invalid redirection driver")

    def compare_netlist(self, filename, phase="final"):
        """Compare replay to an independent Yosys write_json snapshot, including aliases."""
        netlist = json.loads(Path(filename).read_text())["modules"]
        snapshot = next(s for s in self.snapshots if s["phase"] == phase)
        objects = [self.objects[item["ref"]] for item in snapshot["objects"]]
        modules = {unescape(o["module"]) for o in objects}
        require(modules == set(netlist), "netlist module set mismatch")
        bit_values = {}
        for obj in objects:
            if obj["kind"] == "signal_bit":
                state = obj["state"]
                bit_values[obj["ref"]] = netlist[unescape(obj["module"])]["netnames"][unescape(state["wire"])]["bits"][state["offset"]]
        def signal(bits):
            return [bit_values[b] if isinstance(b, str) else b["constant"] for b in bits]
        cells, wires = defaultdict(set), defaultdict(set)
        for obj in objects:
            module, name, state = unescape(obj["module"]), unescape(obj["name"]), obj["state"]
            if obj["kind"] == "cell":
                cells[module].add(name)
                actual = netlist[module]["cells"][name]
                require(unescape(state["type"]) == actual["type"], "netlist cell type mismatch")
                require({unescape(k): signal(v) for k, v in state["ports"].items()} == actual["connections"], "netlist cell connectivity mismatch")
                require({unescape(k): v for k, v in state["parameters"].items()} == actual["parameters"], "netlist parameters mismatch")
            elif obj["kind"] == "connection":
                require(signal(state["lhs"]) == signal(state["rhs"]), "netlist alias mismatch")
            elif obj["kind"] == "wire":
                wires[module].add(name)
                require(len(netlist[module]["netnames"][name]["bits"]) == state["width"], "netlist wire width mismatch")
        for module in modules:
            require(cells[module] == set(netlist[module]["cells"]), "netlist cell set mismatch")
            require(wires[module] == set(netlist[module]["netnames"]), "netlist wire set mismatch")

    def walk(self, seeds, reverse=False):
        index = self.backward if reverse else self.forward
        seen, queue, edges = set(seeds), deque(seeds), []
        while queue:
            node = queue.popleft()
            for edge in index[node]:
                edges.append(edge)
                nxt = edge["from"] if reverse else edge["to"]
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return seen, edges

    def source_seeds(self, filename, first, last=None, instance=None):
        target = Path(filename).resolve()
        seeds = []
        for ref, obj in self.objects.items():
            if obj["kind"] != "source_span":
                continue
            match = re.fullmatch(r"(.+):(\d+)\.(\d+)-(\d+)\.(\d+)", obj["state"]["src"])
            if not match:
                continue
            file, start, _, end, _ = match.groups()
            if Path(file).resolve() == target and int(start) <= (last or first) and int(end) >= first:
                if instance is None or unescape(obj["module"]) == unescape(instance):
                    seeds.append(ref)
        return seeds

    def connectivity(self):
        """Union only electrical aliases; never traverse them as provenance edges."""
        parent = {}
        def root(bit):
            bit = canonical(bit) if not isinstance(bit, str) else bit
            parent.setdefault(bit, bit)
            trail = []
            while parent[bit] != bit:
                trail.append(bit)
                bit = parent[bit]
            for old in trail:
                parent[old] = bit
            return bit
        for ref in self.final:
            obj = self.objects[ref]
            if obj["kind"] == "connection":
                for left, right in zip(obj["state"]["lhs"], obj["state"]["rhs"]):
                    parent[root(left)] = root(right)
        users = defaultdict(list)
        for ref in sorted(self.final):
            obj = self.objects[ref]
            if obj["kind"] == "cell":
                for port, bits in obj["state"]["ports"].items():
                    if obj["state"]["port_directions"][port] in ("input", "inout"):
                        for i, bit in enumerate(bits):
                            users[root(bit)].append({"kind": "cell_input", "ref": ref, "port": unescape(port), "bit": i})
            elif obj["kind"] == "wire" and obj["state"]["port_output"]:
                for bitref in sorted(self.final):
                    bit = self.objects[bitref]
                    if bit["kind"] == "signal_bit" and bit["module"] == obj["module"] and bit["state"]["wire"] == obj["name"]:
                        users[root(bitref)].append({"kind": "module_output", "name": unescape(obj["name"]), "bit": bit["state"]["offset"]})
        result = {}
        for ref in sorted(self.final):
            obj = self.objects[ref]
            if obj["kind"] == "cell":
                consumers = []
                for port, bits in obj["state"]["ports"].items():
                    if obj["state"]["port_directions"][port] == "output":
                        for bit in bits:
                            consumers.extend(users[root(bit)])
                result[ref] = list({canonical(c): c for c in consumers}.values())
        return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
