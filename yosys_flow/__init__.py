"""Checkout entry point; implementation lives in provenance/yosys_flow.

Installed packages use the implementation directly (see pyproject.toml).
"""
from pathlib import Path

__path__.append(str(Path(__file__).resolve().parents[1] / "provenance" / "yosys_flow"))
