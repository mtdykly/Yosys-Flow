"""Persistent validated state must equal replay, and never hide stale evidence."""
import gzip
import hashlib
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import TraceError, digest, write_json
from yosys_flow.native import NativeTrace
from yosys_flow import native, persistent_index
from yosys_flow.persistent_index import INDEX_NAME, _decode, _encode, _json_bytes
from yosys_flow.runner import load_run, query_loaded, run


class IndexCodecTests(unittest.TestCase):
    def test_data_roundtrip_preserves_integer_and_tuple_keys_and_sets(self):
        value = {1: {("signal", 2): {"x", 3}}, "tuple": (True, None, 2),
                 "reserved": {"$verified_index_type": "ordinary user data"}}
        self.assertEqual(_decode(json.loads(json.dumps(_encode(value)))), value)

    def test_non_data_and_unknown_types_are_rejected(self):
        with self.assertRaises(TraceError):
            _encode(object())
        with self.assertRaises(TraceError):
            _decode({"$verified_index_type": "python_object", "items": []})
        with self.assertRaises(TraceError):
            _decode({"$verified_index_type": "map", "items": [[1, 2], [1, 3]]})


@unittest.skipUnless(NATIVE.is_file(), "native Yosys not built; run make native")
class PersistentIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="persistent-index-")
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "source.v"
        cls.source.write_text("""module leaf(input a,b,c,output y,z,direct,zero);
assign y=a&b;
assign z=y^c;
assign direct=a;
assign zero=1'b0;
endmodule
module demo(input a,b,c,d,e,f,output y0,z0,d0,k0,y1,z1,d1,k1);
leaf first(a,b,c,y0,z0,d0,k0);
leaf second(d,e,f,y1,z1,d1,k1);
endmodule
""")
        cls.runs = {}
        for mode in ("retained", "flatten", "m1"):
            out = cls.root / mode
            extra = ["--flatten"] if mode == "flatten" else ["--flow", "m1"] if mode == "m1" else []
            run(parser().parse_args(["run", "--source", str(cls.source), "--top", "demo",
                                    "--out", str(out), "--native-yosys", str(NATIVE), *extra]))
            cls.runs[mode] = out

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def copy_archive(self, mode="retained"):
        destination = self.root / self._testMethodName
        shutil.copytree(self.runs[mode], destination)
        return destination

    def reseal(self, directory, name):
        path = directory / "manifest.json"
        manifest = json.loads(path.read_text())
        artifact = directory / name
        manifest["artifacts"][name] = {"sha256": digest(artifact), "size_bytes": artifact.stat().st_size}
        write_json(path, manifest)

    def edit_index(self, directory, callback):
        path = directory / INDEX_NAME
        data = json.loads(gzip.decompress(path.read_bytes()))
        callback(data)
        path.write_bytes(gzip.compress(json.dumps(data).encode(), mtime=0))
        self.reseal(directory, INDEX_NAME)

    def test_new_run_archives_index_and_restores_identical_queries_in_all_modes(self):
        for mode, directory in self.runs.items():
            with self.subTest(mode=mode):
                timings = {}
                manifest, cached, validation = load_run(directory, timings)
                _, replayed, _ = load_run(directory, use_index=False)
                self.assertEqual(timings["verified_index_status"], "hit")
                self.assertIn(INDEX_NAME, manifest["artifacts"])
                self.assertGreaterEqual(manifest["timings_seconds"]["verified_index_build"], 0)
                self.assertEqual(vars(cached), vars(replayed))
                for line in (2, 3, 4, 5):
                    args = parser().parse_args(["query", str(directory), "--source", str(self.source),
                                                "--line", str(line), "--instance", "demo/first"])
                    expected = query_loaded(args, manifest, replayed)
                    self.assertEqual(query_loaded(args, manifest, cached), expected)
                    # The consumer lookup must preserve the public query ordering.
                    lookup = replayed.lookup_index
                    del replayed.lookup_index
                    self.assertEqual(query_loaded(args, manifest, replayed), expected)
                    replayed.lookup_index = lookup

    def test_cache_hit_does_not_replay_or_reprove_abc(self):
        with patch.object(NativeTrace, "__init__", side_effect=AssertionError("unexpected replay")), \
             patch.object(NativeTrace, "compare_abc_artifacts", side_effect=AssertionError("unexpected ABC proof")), \
             patch("yosys_flow.hierarchy.instance_view", side_effect=AssertionError("unexpected instance expansion")):
            timings = {}
            load_run(self.runs["retained"], timings)
            self.assertEqual(timings["verified_index_status"], "hit")
            self.assertEqual(timings["abc_artifact_check_and_proof"], 0)

    def test_legacy_archive_without_index_replays(self):
        directory = self.copy_archive()
        path = directory / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["artifacts"].pop(INDEX_NAME)
        manifest.pop("verified_index")
        write_json(path, manifest)
        (directory / INDEX_NAME).unlink()
        timings = {}
        load_run(directory, timings)
        self.assertEqual(timings["verified_index_status"], "not_archived")
        self.assertGreater(timings["record_replay_and_netlist_check"], 0)

    def test_missing_optional_index_replays_complete_raw_archive(self):
        directory = self.copy_archive()
        (directory / INDEX_NAME).unlink()
        timings = {}
        load_run(directory, timings)
        self.assertEqual(timings["verified_index_status"], "missing")

    def test_changed_implementation_and_schema_replay(self):
        directory = self.copy_archive()
        with patch("yosys_flow.persistent_index.implementation_fingerprint", return_value={"new": "revision"}):
            timings = {}
            load_run(directory, timings)
            self.assertEqual(timings["verified_index_status"], "implementation_changed")
        self.edit_index(directory, lambda data: data.update(schema_version=999))
        timings = {}
        load_run(directory, timings)
        self.assertEqual(timings["verified_index_status"], "schema_changed")

    def test_index_corruption_is_rejected_even_with_resealed_artifact_hash(self):
        directory = self.copy_archive()
        path = directory / INDEX_NAME
        original = path.read_bytes()
        path.write_bytes(original + b"corruption")
        with self.assertRaisesRegex(TraceError, "artifact hash mismatch"):
            load_run(directory)
        self.reseal(directory, INDEX_NAME)
        with self.assertRaisesRegex(TraceError, "invalid verified-index artifact"):
            load_run(directory)

    def test_payload_checksum_rejects_changes_to_restored_origins(self):
        directory = self.copy_archive()
        self.edit_index(directory, lambda data: data["payload"].update(gaps=["invented source evidence"]))
        with self.assertRaisesRegex(TraceError, "payload checksum mismatch"):
            load_run(directory)

    def test_resealed_payload_and_manifest_cannot_forge_verified_state(self):
        directory = self.copy_archive()
        def forge(data):
            data["payload"]["gaps"] = []
            data["payload_sha256"] = hashlib.sha256(_json_bytes(data["payload"])).hexdigest()
        self.edit_index(directory, forge)
        with self.assertRaisesRegex(TraceError, "authentication failed"):
            load_run(directory)
        # Explicit raw replay validates original evidence without trusting the
        # forged index. A caller cannot make it pass by resealing JSON hashes.
        _, replayed, _ = load_run(directory, use_index=False)
        self.assertTrue(replayed.gaps)

    def test_archive_without_local_trust_key_falls_back_without_modifying_it(self):
        directory = self.copy_archive()
        before = digest(directory / INDEX_NAME)
        missing = self.root / "other-checkout" / "key"
        with patch("yosys_flow.persistent_index._key_path", return_value=missing):
            timings = {}
            load_run(directory, timings)
            self.assertEqual(timings["verified_index_status"], "local_authentication_unavailable")
        self.assertFalse(missing.exists())
        self.assertEqual(digest(directory / INDEX_NAME), before)

    def test_resealed_bad_raw_log_invalidates_cache_then_fails_replay(self):
        directory = self.copy_archive()
        path = directory / "yosys.jsonl"
        rows = path.read_text().splitlines()
        row = json.loads(rows[1]); row["seq"] += 100
        rows[1] = json.dumps(row)
        path.write_text("\n".join(rows) + "\n")
        self.reseal(directory, "yosys.jsonl")
        timings = {}
        with self.assertRaisesRegex(TraceError, "sequence discontinuity"):
            load_run(directory, timings)
        self.assertEqual(timings["verified_index_status"], "archive_binding_changed")

    def test_semantic_manifest_metadata_cannot_reuse_cached_validation(self):
        directory = self.copy_archive()
        path = directory / "manifest.json"
        original = json.loads(path.read_text())
        for field, value in (("flow", "m1"), ("endpoint", "invented endpoint"),
                             ("coverage", "invented exact coverage"), ("tool", {}), ("inputs", [])):
            with self.subTest(field=field):
                write_json(path, {**original, field: value})
                timings = {}
                load_run(directory, timings)
                self.assertEqual(timings["verified_index_status"], "archive_binding_changed")

    def test_raw_evidence_is_hashed_on_every_cache_hit(self):
        directory = self.copy_archive()
        manifest = json.loads((directory / "manifest.json").read_text())
        name = next(n for n in manifest["artifacts"] if n.endswith(".flow.jsonl"))
        path = directory / name
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(TraceError, "artifact hash mismatch"):
            load_run(directory)

    def test_reading_index_never_mutates_archived_state(self):
        directory = self.runs["retained"]
        _, first, _ = load_run(directory)
        first.sources.clear()
        first.design["cells"].clear()
        _, second, _ = load_run(directory)
        self.assertTrue(second.sources)
        self.assertTrue(second.design["cells"])

    def test_later_stock_stage_cannot_change_evidence_already_replayed(self):
        directory = self.root / self._testMethodName
        execute = native.subprocess.run
        def changed_during_baseline(command, *args, **kwargs):
            result = execute(command, *args, **kwargs)
            if str(command[-1]).endswith("baseline.ys"):
                evidence = directory / "yosys.jsonl"
                evidence.write_bytes(evidence.read_bytes() + b"\n")
            return result
        with patch("yosys_flow.native.subprocess.run", side_effect=changed_during_baseline):
            with self.assertRaisesRegex(TraceError, "validated evidence changed before index signing: yosys.jsonl"):
                run(parser().parse_args(["run", "--source", str(self.source), "--top", "demo",
                                        "--out", str(directory), "--native-yosys", str(NATIVE)]))
        self.assertFalse((directory / INDEX_NAME).exists())
        self.assertEqual(json.loads((directory / "manifest.json").read_text())["status"], "failed")

    def test_signing_rejects_changed_log_abc_or_netlist_after_validation(self):
        write_verified_index = persistent_index.write_index
        for kind in ("log", "abc", "netlist"):
            with self.subTest(kind=kind):
                directory = self.root / (self._testMethodName + "-" + kind)
                def change_before_signature(out, manifest, trace, **kwargs):
                    name = "yosys.jsonl" if kind == "log" else "after.json" if kind == "netlist" else next(
                        p.name for p in Path(out).iterdir() if p.name.endswith(".flow.jsonl"))
                    evidence = Path(out) / name
                    evidence.write_bytes(evidence.read_bytes() + b"\n")
                    return write_verified_index(out, manifest, trace, **kwargs)
                with patch("yosys_flow.persistent_index.write_index", side_effect=change_before_signature):
                    with self.assertRaisesRegex(TraceError, "validated evidence changed before index signing"):
                        run(parser().parse_args(["run", "--source", str(self.source), "--top", "demo",
                                                "--out", str(directory), "--native-yosys", str(NATIVE)]))
                self.assertFalse((directory / INDEX_NAME).exists())
                self.assertEqual(json.loads((directory / "manifest.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
