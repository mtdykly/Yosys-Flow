"""Public entry points for the native synthesis and checkpoint flows."""
import json
from pathlib import Path

from .common import require
from .native import load_native, query_native, run_native


def run(args):
    return run_native(args)


def load_run(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    require(manifest.get("schema_version") != 1,
            "M0 / JSONL v1 support has been retired. Historical results are unchanged; "
            "use a historical Git revision to read them, or create a new native synth run.")
    require(manifest.get("schema_version") == 2, "unsupported run schema; expected native JSONL v2")
    return load_native(directory, manifest)


def query(args):
    manifest, trace, _ = load_run(args.directory)
    return query_loaded(args, manifest, trace)


def query_loaded(args, manifest, trace):
    """Reuse an already validated native run for export."""
    return query_native(args, manifest, trace)
