"""Apply explicit hooks to the pinned upstream pass; fail on source drift."""
from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / "vendor/opt_merge.cc"
text = source.read_text()
expected = (ROOT / "vendor/opt_merge.sha256").read_text().split()[0]
if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
    raise SystemExit("Pinned opt_merge source hash mismatch")

def replace(old, new):
    global text
    if text.count(old) != 1:
        raise SystemExit(f"Instrumentation anchor changed: {old!r}")
    text = text.replace(old, new)

replace('#include "kernel/register.h"', '#include "kernel/register.h"\n#include "../plugin/recorder.h"')
replace('Pass("opt_merge",', 'Pass("trace_opt_merge",')
replace('opt_merge [options] [selection]', 'trace_opt_merge [options] [selection]')
replace('\t\textra_args(args, argidx, design);', '\t\textra_args(args, argidx, design);\n\t\tif (!provenance::recorder) log_cmd_error("Call trace_begin first.\\n");\n\t\tprovenance::recorder->begin_step(design, args);')
replace('\t\t\tfor (auto [remove_cell, keep_cell] : cell_ptrs)\n\t\t\t{', '\t\t\tfor (auto [remove_cell, keep_cell] : cell_ptrs)\n\t\t\t{\n\t\t\t\tprovenance::recorder->before_merge(module, remove_cell, keep_cell);')
replace('\t\t\t\tmodule->remove(remove_cell);', '\t\t\t\tprovenance::recorder->after_merge(module, remove_cell, keep_cell);\n\t\t\t\tmodule->remove(remove_cell);')
replace('\t\tlog("Removed a total of %d cells.\\n", total_count);', '\t\tlog("Removed a total of %d cells.\\n", total_count);\n\t\tprovenance::recorder->end_step(design);')
(ROOT / "build").mkdir(exist_ok=True)
(ROOT / "build/trace_opt_merge.cc").write_text(text)
