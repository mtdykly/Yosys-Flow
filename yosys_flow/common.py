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


def require_combinational(module, module_types=()):
    """Fail closed at run/load/export boundaries, including inferred state."""
    for name, cell in module['cells'].items():
        if cell['type'] in module_types: continue
        kind = cell['type'].lower().lstrip('$_')
        require(not kind.startswith(('dff', 'adff', 'aldff', 'sdff', 'dlatch', 'adlatch', 'sr', 'ff', 'mem')),
                f'Combinational-only flow rejects state cell {name} ({cell["type"]})')
    require(all('init' not in n.get('attributes', {}) for n in module.get('netnames', {}).values()),
            'Combinational-only flow rejects initialization attributes')
