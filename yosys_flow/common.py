"""Shared validation, file and Yosys argument helpers."""
import hashlib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PIN = "0e82bbefe"


class TraceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise TraceError(message)


def unescape(name):
    return name[1:] if name.startswith("\\") else name


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
