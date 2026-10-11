"""Native hooks for exactly the source revision in native/lock.json.

Called on pristine, verified source by build_native.py. Each replacement is
unique; no regex guessing, keep attributes, or pass renaming is involved.
"""
from pathlib import Path
import difflib
import shutil

ROOT = Path(__file__).resolve().parents[1]


def instrument(root):
    root = Path(root)
    originals = {}

    def edit(name, old, new):
        path = root / name
        text = path.read_text()
        originals.setdefault(name, text)
        if text.count(old) != 1:
            raise ValueError(f"Native instrumentation anchor changed: {name}: {old!r}")
        path.write_text(text.replace(old, new))

    for name in ("flow_provenance.h", "flow_provenance.cc"):
        shutil.copyfile(ROOT / "native" / name, root / "kernel" / name)
    shutil.copyfile(ROOT / "native/ast_sources.inc", root / "frontends/ast/flow_sources.inc")
    edit("kernel/CMakeLists.txt", "\tbinding.cc", "\tflow_provenance.cc\n\tflow_provenance.h\n\tbinding.cc")
    for file in ("kernel/rtlil.cc", "kernel/register.cc", "frontends/ast/ast.cc",
                 "frontends/ast/genrtlil.cc", "passes/proc/proc_mux.cc",
                 "passes/proc/proc_dff.cc", "passes/proc/proc_dlatch.cc", "passes/proc/proc_init.cc", "passes/opt/opt_merge.cc",
                 "passes/opt/opt_expr.cc", "passes/opt/opt_dff.cc", "passes/opt/opt_clean/wires.cc",
                 "passes/techmap/techmap.cc", "passes/techmap/alumacc.cc", "passes/techmap/abc.cc"):
        path = root / file
        originals.setdefault(file, path.read_text())
        path.write_text('#include "kernel/flow_provenance.h"\n' + path.read_text())
    edit("frontends/ast/ast.h", "\t\tAstSrcLocType location;",
         "\t\tAstSrcLocType location;\n\t\tstd::set<int> flow_origins, flow_controls;\n"
         "\t\tstd::set<int> flow_sources() const {\n"
         "\t\t\tauto ids = flow_origins; ids.insert(flow_controls.begin(), flow_controls.end());\n"
         "\t\t\tfor (auto &child : children) { auto c = child->flow_sources(); ids.insert(c.begin(), c.end()); }\n"
         "\t\t\treturn ids;\n\t\t}")
    edit("frontends/ast/ast.cc", "using namespace AST_INTERNAL;",
         'using namespace AST_INTERNAL;\n#include "flow_sources.inc"')
    edit("frontends/ast/ast.cc", "\tother.type = type;",
         "\tother.flow_origins.insert(flow_origins.begin(), flow_origins.end());\n"
         "\tother.flow_controls.insert(flow_controls.begin(), flow_controls.end());\n\tother.type = type;")
    edit("frontends/ast/ast.cc", "\tcurrent_ast = ast;\n\tcurrent_ast_mod = nullptr;",
         "\tif (FlowProvenance::tracks(design)) flow_register_ast(ast);\n\tcurrent_ast = ast;\n\tcurrent_ast_mod = nullptr;")
    edit("frontends/ast/ast.cc", "\tcurrent_module = module;",
         "\tcurrent_module = module;\n\tFlowProvenance::ModuleScope flow_module_scope(design, module);")
    file = "frontends/ast/genrtlil.cc"
    edit(file, "RTLIL::SigSpec AstNode::genRTLIL(int width_hint, bool sign_hint)\n{",
         "RTLIL::SigSpec AstNode::genRTLIL(int width_hint, bool sign_hint)\n{\n"
         "\tFlowProvenance::Scope flow_scope(flow_origins);")
    edit(file, "\tvoid processAst(AstNode *ast)\n\t{",
         "\tvoid processAst(AstNode *ast)\n\t{\n\t\tFlowProvenance::Scope flow_scope(ast->flow_origins);")
    edit(file, "\t\tset_src_attr(proc, always.get());",
         "\t\tset_src_attr(proc, always.get());\n"
         "\t\tfor (auto &child : always->children)\n"
         "\t\t\tif (child->type == AST_POSEDGE || child->type == AST_NEGEDGE)\n"
         "\t\t\t\tFlowProvenance::clock_sources(proc, child->flow_sources());")
    edit(file, "\t\t\tcurrent_module->connect(RTLIL::SigSig(left, right));",
         "\t\t\tFlowProvenance::assignment(current_module, left, right, flow_sources());\n"
         "\t\t\tcurrent_module->connect(RTLIL::SigSig(left, right));")
    edit(file, "\t\t\t\tcurrent_case->actions.push_back(RTLIL::SigSig(lvalue, rvalue));",
         "\t\t\t\tFlowProvenance::action(proc, unmapped_lvalue, ast->flow_sources());\n"
         "\t\t\t\tcurrent_case->actions.push_back(RTLIL::SigSig(lvalue, rvalue));")
    edit(file, "\t\treturn chunks;\n\t}",
         "\t\tFlowProvenance::temporary(proc, sig, RTLIL::SigSpec(chunks));\n\t\treturn chunks;\n\t}")
    edit("kernel/rtlil.cc", "\tcell->type = type;\n\tadd(cell);\n\treturn cell;",
         "\tcell->type = type;\n\tadd(cell);\n\tFlowProvenance::created(cell);\n\treturn cell;")
    edit("kernel/rtlil.cc", "void RTLIL::Module::remove(RTLIL::Cell *cell)\n{",
         "void RTLIL::Module::remove(RTLIL::Cell *cell)\n{\n\tFlowProvenance::removed(cell);")
    edit("kernel/rtlil.cc", "void RTLIL::Module::remove(RTLIL::Process *process)\n{",
         "void RTLIL::Module::remove(RTLIL::Process *process)\n{\n\tFlowProvenance::process_removed(process);")
    edit("kernel/rtlil.cc", "void RTLIL::Module::remove(const pool<RTLIL::Wire*> &wires)\n{",
         "void RTLIL::Module::remove(const pool<RTLIL::Wire*> &wires)\n{\n\tFlowProvenance::wires_removed(this, wires);")
    edit("kernel/rtlil.cc", "\tconnections_.push_back(conn);",
         "\tconnections_.push_back(conn);\n\tFlowProvenance::connected(this, conn);")
    edit("kernel/register.cc", "\tpass->execute(args, design);\n\tpass->post_execute(state);",
         "\tFlowProvenance::step_begin(args, design);\n\tpass->execute(args, design);\n"
         "\tif (args[0] != \"flow_start\" && args[0] != \"flow_stop\") FlowProvenance::step_end(design);\n"
         "\tpass->post_execute(state);")
    edit("passes/proc/proc_mux.cc", "\t\tRTLIL::SigSpec sig = sigsnip.sigidx[idx];",
         "\t\tRTLIL::SigSpec sig = sigsnip.sigidx[idx];\n"
         "\t\tFlowProvenance::Scope flow_scope(FlowProvenance::process_sources(proc, sig), \"process_signal_candidate\", false);")
    edit("passes/proc/proc_dff.cc", "\t\tRTLIL::SigSpec insig = RTLIL::SigSpec(RTLIL::State::Sz, sig.size());",
         "\t\tFlowProvenance::Scope flow_scope(FlowProvenance::state_sources(proc, sig), \"process_signal_candidate\", false);\n"
         "\t\tRTLIL::SigSpec insig = RTLIL::SigSpec(RTLIL::State::Sz, sig.size());")
    edit("passes/proc/proc_dff.cc", "\t\t\tmod->connect(RTLIL::SigSig(sig, insig));",
         "\t\t\tFlowProvenance::assignment(mod, sig, insig, FlowProvenance::process_sources(proc, sig), \"process_signal_candidate\");\n"
         "\t\t\tmod->connect(RTLIL::SigSig(sig, insig));")
    edit("passes/proc/proc_dlatch.cc", "\t\tdb.module->connect(lhs, rhs);",
         "\t\tFlowProvenance::assignment(db.module, lhs, rhs, FlowProvenance::process_sources(proc, lhs), \"process_signal_candidate\");\n"
         "\t\tdb.module->connect(lhs, rhs);")
    edit("passes/proc/proc_init.cc", "\t\t\t\tint offset = 0;",
         "\t\t\t\tFlowProvenance::initialization(proc, lhs, rhs);\n\t\t\t\tint offset = 0;")
    edit("passes/opt/opt_merge.cc", "\t\t\tfor (auto [remove_cell, keep_cell] : cell_ptrs)\n\t\t\t{",
         "\t\t\tfor (auto [remove_cell, keep_cell] : cell_ptrs)\n\t\t\t{\n"
         "\t\t\t\tFlowProvenance::merge(keep_cell, remove_cell);")
    edit("passes/opt/opt_clean/wires.cc",
         "\tupdate_assign_map(actx.assign_map, new_sigmap_rep_candidates, cell_wires, conn_kinds);",
         "\tupdate_assign_map(actx.assign_map, new_sigmap_rep_candidates, cell_wires, conn_kinds);\n"
         "\tFlowProvenance::remap_connections(module, actx.assign_map);")
    edit("passes/opt/opt_expr.cc", "\tfor (auto cell : cells.sorted)\n\t{",
         "\tfor (auto cell : cells.sorted)\n\t{\n\t\tFlowProvenance::CellScope flow_scope(cell, \"opt_expr\");")
    # opt_dff can replace a register with several registers. This records the
    # actual target register; absorbed mux/control donors still require hooks.
    edit("passes/opt/opt_dff.cc", "\t\t\tdff_cells.pop_back();",
         "\t\t\tdff_cells.pop_back();\n\t\t\tFlowProvenance::CellScope flow_scope(cell, \"opt_dff\");")
    edit("passes/techmap/techmap.cc", "\t\tfor (auto cell : cells.sorted)\n\t\t{",
         "\t\tfor (auto cell : cells.sorted)\n\t\t{\n\t\t\tFlowProvenance::CellScope flow_scope(cell, \"techmap\");")
    edit("passes/techmap/alumacc.cc", "\t\t\t\t\tbool do_subtract = port.do_subtract;",
         "\t\t\t\t\tFlowProvenance::merge(n->cell, other_n->cell);\n\t\t\t\t\tbool do_subtract = port.do_subtract;")
    edit("passes/techmap/alumacc.cc", "\t\t\tauto cell = module->addCell(NEW_ID, ID($macc));",
         "\t\t\tFlowProvenance::CellScope flow_scope(n->cell, \"alumacc\");\n\t\t\tauto cell = module->addCell(NEW_ID, ID($macc));")
    edit("passes/techmap/alumacc.cc", "\t\tfor (auto n : it1.second)\n\t\t{",
         "\t\tfor (auto n : it1.second)\n\t\t{\n\t\t\tFlowProvenance::CellScope flow_scope(n->cells, \"alumacc\");")
    edit("passes/techmap/abc.cc", "struct AbcModuleState {",
         "struct AbcModuleState {\n\tFlowProvenance::Region flow_region;")
    edit("passes/techmap/abc.cc",
         "bool AbcModuleState::extract_cell(const AbcSigMap &assign_map, RTLIL::Module *module, RTLIL::Cell *cell, bool keepff)\n{",
         "bool AbcModuleState::extract_cell(const AbcSigMap &assign_map, RTLIL::Module *module, RTLIL::Cell *cell, bool keepff)\n{\n"
         "\tFlowProvenance::RegionCapture flow_capture(flow_region);")
    edit("passes/techmap/abc.cc", '\tlog_header(design, "Re-integrating ABC results.\\n");',
         '\tlog_header(design, "Re-integrating ABC results.\\n");\n'
         '\tstd::vector<int> flow_inputs, flow_outputs;\n'
         '\tfor (auto &si : run_abc.signal_list) if (si.is_port)\n'
         '\t\t(si.type == G(NONE) ? flow_inputs : flow_outputs).push_back(si.id);\n'
         '\tFlowProvenance::RegionScope flow_scope(flow_region, module, RTLIL::SigSpec(signal_bits),\n'
         '\t\tflow_inputs, flow_outputs, run_abc.per_run_tempdir_name, run_abc.config.exe_file, run_abc.abc_script);')
    # NEW_ID embeds __LINE__. Preserve original locations so instrumentation
    # changes neither generated identifiers nor ordering tied to those names.
    for name, original in originals.items():
        if not name.endswith((".cc", ".h")):
            continue
        before = original.splitlines(keepends=True)
        after = (root / name).read_text().splitlines(keepends=True)
        restored = []
        for tag, i, _end, j, end in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
            if j != end:
                restored.append(f'#line {i + 1} "{name}"\n')
                restored.extend(after[j:end])
        (root / name).write_text("".join(restored))
