"""Data-only, versioned snapshots of a fully verified native archive.

The archive manifest is the integrity root, as for the original JSONL artifacts.
An index is an acceleration artifact, never independent provenance evidence:
every raw artifact is still hashed on each load. Stale indexes trigger ordinary
replay; corrupted indexed data is rejected. No Python objects are unpickled.
"""
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile

from .common import ROOT, TraceError, digest, require, unescape

INDEX_NAME = "verified-index.sqlite3"
LEGACY_NAME = "verified-index.json.gz"
INDEX_NAMES = {INDEX_NAME, LEGACY_NAME}
SCHEMA = 1
_TAG = "$verified_index_type"
# These modules create, refine, validate or project the stored trace. Changes to
# online contract presentation do not invalidate otherwise unchanged evidence.
_SEMANTIC_FILES = ("native.py", "native_storage.py", "common.py", "hierarchy.py", "persistent_index.py",
                   "abc_provenance.py", "abc_internal.py", "abc_joint.py",
                   "abc_lineage.py", "boolean.py", "sqlite_index.py", "graph_index.py", "diagnostics.py")
_FIELDS = {
    "records", "sources", "objects", "assignments", "events", "edges", "regions",
    "removals", "process_nodes", "process_actions", "process_discards",
    "process_connection_proofs", "abc_imports", "techmap_bits", "hierarchy",
    "flatten_events", "gaps", "final", "abc_associations", "abc_fallbacks",
    "abc_internal", "design", "instance_catalog", "view_rule", "lookup_index",
}


def _key_path():
    # The key is local to this checkout, ignored by Git, and NEVER archived.
    # Moving an archive to another checkout therefore requires raw replay.
    return ROOT / "build" / ".verified-index-auth-key"


def _authentication_key(*, create=False):
    path = _key_path()
    if not path.is_file() and create:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Publish complete bytes atomically so simultaneous baselines cannot
        # observe a half-written key or overwrite one another's trust root.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".verified-index-key-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(secrets.token_bytes(32))
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        return None
    require(len(key) == 32, "invalid local verified-index authentication key")
    return key


def _signature(key, envelope):
    return hmac.new(key, b"yosys-flow-verified-index-v1\x00" + _json_bytes(envelope), hashlib.sha256).hexdigest()


def implementation_fingerprint():
    root = Path(__file__).parent
    return {name: digest(root / name) for name in _SEMANTIC_FILES}


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _encode(value):
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        if all(type(key) is str for key in value) and _TAG not in value:
            return {key: _encode(item) for key, item in value.items()}
        return {_TAG: "map", "items": [[_encode(k), _encode(v)] for k, v in value.items()]}
    if isinstance(value, (set, tuple)):
        items = sorted(value, key=repr) if isinstance(value, set) else value
        return {_TAG: "set" if isinstance(value, set) else "tuple", "items": [_encode(v) for v in items]}
    raise TraceError(f"unsupported verified-index value: {type(value).__name__}")


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if _TAG not in value:
        return {key: _decode(item) for key, item in value.items()}
    require(set(value) == {_TAG, "items"} and isinstance(value["items"], list), "invalid verified-index type encoding")
    kind, items = value[_TAG], value["items"]
    if kind == "map":
        result = {}
        for pair in items:
            require(isinstance(pair, list) and len(pair) == 2, "invalid verified-index map entry")
            key, item = map(_decode, pair)
            require(key not in result, "duplicate verified-index map key")
            result[key] = item
        return result
    require(kind in {"set", "tuple"}, "unknown verified-index type")
    decoded = [_decode(item) for item in items]
    if kind == "tuple":
        return tuple(decoded)
    result = set(decoded)
    require(len(result) == len(decoded), "duplicate verified-index set member")
    return result


def build_lookup(trace):
    """Materialize source and electrical indexes once, in original pin order."""
    sources_to_final, sources_to_assignments = {}, {}
    for ref in sorted(trace.final):
        for source in trace.objects[ref]["origins"]:
            sources_to_final.setdefault(source, []).append(ref)
    for ref, row in trace.assignments.items():
        for source in row["origins"]:
            sources_to_assignments.setdefault(source, []).append(ref)
    consumers, consumer_rows, drivers, aliases = {}, [], {}, {}
    for name, cell in trace.design["cells"].items():
        for port, bits in cell["connections"].items():
            for offset, bit in enumerate(bits):
                if not isinstance(bit, int):
                    continue
                row = {"kind": "cell", "name": name, "port": port, "offset": offset}
                if cell["port_directions"][port] == "input":
                    consumers.setdefault(bit, []).append(len(consumer_rows))
                    consumer_rows.append(row)
                else:
                    drivers.setdefault(bit, []).append(row)
    for name, port in trace.design["ports"].items():
        for offset, bit in enumerate(port["bits"]):
            if not isinstance(bit, int):
                continue
            row = {"kind": "module_port", "name": name, "offset": offset}
            if port["direction"] == "output":
                consumers.setdefault(bit, []).append(len(consumer_rows))
                consumer_rows.append(row)
            else:
                drivers.setdefault(bit, []).append(row)
    for name, net in trace.design["netnames"].items():
        for offset, bit in enumerate(net["bits"]):
            aliases.setdefault(bit, []).append({"name": name, "offset": offset})
    from .graph_index import EdgeList
    trace.edges = EdgeList(trace.edges)
    trace.lookup_index = {"sources_to_final": sources_to_final,
                          "sources_to_assignments": sources_to_assignments,
                          "consumers": consumers, "consumer_rows": consumer_rows,
                          "drivers": drivers, "aliases": aliases}
    return trace.lookup_index


def _binding(manifest, artifacts):
    # Bind flow, endpoint, input/tool identities and coverage declarations too.
    # Only publication state, accounting and the index's own metadata are
    # excluded; the latter would otherwise make the artifact hash recursive.
    excluded = {"artifacts", "verified_index", "timings_seconds", "status"}
    return {"manifest": {key: value for key, value in manifest.items() if key not in excluded},
            "artifacts": {name: meta for name, meta in artifacts.items() if name not in INDEX_NAMES}}


def artifact_inventory(directory):
    return {path.name: {"sha256": digest(path), "size_bytes": path.stat().st_size}
            for path in sorted(Path(directory).iterdir())
            if path.is_file() and path.name not in {"manifest.json", *INDEX_NAMES, *(n+".tmp" for n in INDEX_NAMES)}}


def require_evidence_unchanged(expected, current):
    for name, metadata in expected.items():
        require(current.get(name) == metadata, f"validated evidence changed before index signing: {name}")


def write_legacy_index(directory, manifest, trace, *, verified_artifacts):
    """Called only after baseline replay, ABC checks and stock proofs pass."""
    directory = Path(directory)
    build_lookup(trace)
    require(set(vars(trace)) == _FIELDS, "native trace fields changed; update verified-index schema")
    artifacts = artifact_inventory(directory)
    require_evidence_unchanged(verified_artifacts, artifacts)
    require(artifacts == verified_artifacts, "unverified artifact appeared before index signing")
    payload = _encode(vars(trace))
    envelope = {"schema_version": SCHEMA, "kind": "verified_native_trace",
                "implementation": implementation_fingerprint(), "binding": _binding(manifest, artifacts),
                "payload_sha256": hashlib.sha256(_json_bytes(payload)).hexdigest(), "payload": payload}
    key = _authentication_key(create=True)
    envelope["authentication"] = {"algorithm": "hmac-sha256", "key_id": hashlib.sha256(key).hexdigest(),
                                  "signature": _signature(key, envelope)}
    target = directory / LEGACY_NAME
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(gzip.compress(_json_bytes(envelope), compresslevel=1, mtime=0))
    require_evidence_unchanged(verified_artifacts, artifact_inventory(directory))
    os.replace(temporary, target)
    return {"artifact": LEGACY_NAME, "schema_version": SCHEMA, "implementation": envelope["implementation"]}


def read_legacy_index(directory, manifest, trace_class):
    """Return (trace, status); original artifacts must already be hash-checked."""
    path = Path(directory) / LEGACY_NAME
    if LEGACY_NAME not in manifest["artifacts"]:
        return None, "not_archived"
    if not path.is_file():
        return None, "missing"
    try:
        envelope = json.loads(gzip.decompress(path.read_bytes()))
        require(isinstance(envelope, dict), "invalid verified-index envelope")
        if envelope.get("schema_version") != SCHEMA:
            return None, "schema_changed"
        require(envelope.get("kind") == "verified_native_trace", "invalid verified-index kind")
        if envelope.get("implementation") != implementation_fingerprint():
            return None, "implementation_changed"
        if envelope.get("binding") != _binding(manifest, manifest["artifacts"]):
            return None, "archive_binding_changed"
        payload = envelope["payload"]
        require(hashlib.sha256(_json_bytes(payload)).hexdigest() == envelope["payload_sha256"],
                "verified-index payload checksum mismatch")
        key = _authentication_key()
        if key is None:
            return None, "local_authentication_unavailable"
        authentication = envelope.pop("authentication", None)
        require(isinstance(authentication, dict) and authentication.get("algorithm") == "hmac-sha256",
                "verified-index authentication is missing or unsupported")
        if authentication.get("key_id") != hashlib.sha256(key).hexdigest():
            return None, "local_authentication_key_changed"
        require(isinstance(authentication.get("signature"), str) and
                hmac.compare_digest(authentication["signature"], _signature(key, envelope)),
                "verified-index authentication failed; replay from trusted raw artifacts is required")
        state = _decode(payload)
        require(isinstance(state, dict) and set(state) == _FIELDS, "invalid verified-index trace fields")
        require(state["records"][0]["run_id"] == manifest["run_id"], "verified-index run ID mismatch")
        require(isinstance(state["final"], set) and state["final"] <= state["objects"].keys(),
                "invalid verified-index final object set")
        require(set(state["design"]["cells"]) == {unescape(state["objects"][r]["state"]["name"]) for r in state["final"]},
                "verified-index design and objects differ")
        trace = trace_class.__new__(trace_class)
        trace.__dict__.update(state)
        return trace, "hit"
    except TraceError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, OSError, EOFError, RecursionError) as exc:
        raise TraceError(f"invalid verified-index artifact: {exc}") from exc


def write_index(directory, manifest, trace, *, verified_artifacts):
    from .sqlite_index import write
    return write(directory, manifest, trace, verified_artifacts)


def read_index(directory, manifest, trace_class):
    if INDEX_NAME in manifest['artifacts']:
        from .sqlite_index import read
        return read(directory, manifest, trace_class)
    return read_legacy_index(directory, manifest, trace_class)
