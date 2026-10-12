"""Native hooks for exactly the source revision in native/lock.json.

Called on pristine, verified source by build_native.py. Each anchor count is
checked; no regex guessing, keep attributes, or pass renaming is involved.
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

    for name in ("flow_provenance.h", "flow_provenance.cc", "process_provenance.inc"):
        shutil.copyfile(ROOT / "native" / name, root / "kernel" / name)
    shutil.copyfile(ROOT / "native/ast_sources.inc", root / "frontends/ast/flow_sources.inc")
    edit("kernel/CMakeLists.txt", "\tbinding.cc", "\tflow_provenance.cc\n\tflow_provenance.h\n\tbinding.cc")
    for file in ("kernel/rtlil.cc", "kernel/register.cc", "frontends/ast/ast.cc",
                 "frontends/ast/genrtlil.cc", "passes/proc/proc_mux.cc",
                 "passes/proc/proc_dff.cc", "passes/proc/proc_dlatch.cc", "passes/proc/proc_init.cc", "passes/opt/opt_merge.cc",
                 "passes/opt/opt_expr.cc", "passes/opt/opt_dff.cc", "passes/opt/opt_muxtree.cc", "passes/opt/opt_reduce.cc", "passes/opt/opt_clean/wires.cc",
                 "passes/techmap/techmap.cc", "passes/techmap/alumacc.cc", "passes/techmap/abc.cc", "passes/hierarchy/hierarchy.cc", "passes/proc/proc_arst.cc", "passes/proc/proc_prune.cc", "passes/proc/proc_clean.cc", "passes/proc/proc_rmdead.cc"):
        path = root / file
        originals.setdefault(file, path.read_text())
        path.write_text('#include "kernel/flow_provenance.h"\n' + path.read_text())
    edit("frontends/ast/ast.h", "\t\tAstSrcLocType location;",
         "\t\tAstSrcLocType location;\n\t\tstd::set<int> flow_origins, flow_controls;\n"
         "\t\tstd::set<int> flow_sources() const {\n"
         "\t\t\tauto ids = flow_origins; ids.insert(flow_controls.begin(), flow_controls.end());\n"
         "\t\t\tfor (auto &child : children) { auto c = child->flow_sources(); ids.insert(c.begin(), c.end()); }\n"
         "\t\t\treturn ids;\n\t\t}\n"
         "\t\tstd::set<int> flow_data() const { auto ids = flow_origins; for (auto &c : children) { auto s = c->flow_data(); ids.insert(s.begin(), s.end()); } return ids; }")
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
         "\t\t\t\tauto flow_value = FlowProvenance::process_action(proc, current_case, lvalue, rvalue, unmapped_lvalue,\n"
         "\t\t\t\t\tast->flow_origins, ast->children[1]->flow_data(), ast->flow_controls, ast->children[0]->flow_data());\n"
         "\t\t\t\tif (ast->type == AST_ASSIGN_EQ) for (int i = 0; i < GetSize(unmapped_lvalue); ++i) flow_subst.set(unmapped_lvalue[i], flow_value[i]);\n"
         "\t\t\t\tcurrent_case->actions.push_back(RTLIL::SigSig(lvalue, rvalue));")
    edit(file, "\tstackmap<RTLIL::SigBit, RTLIL::SigBit> subst_rvalue_map;",
         "\tstackmap<RTLIL::SigBit, RTLIL::SigBit> subst_rvalue_map;\n\tstackmap<RTLIL::SigBit, FlowProvenance::ProcessRefs> flow_subst;")
    edit(file, "bool inSyncRule = false)\n\t{", "bool inSyncRule = false, const FlowProvenance::ProcessValue &flow_value = {})\n\t{")
    edit(file, "\t\t\tactions.push_back(RTLIL::SigSig(lhs, rhs));",
         "\t\t\tFlowProvenance::ProcessValue flow_chunk;\n"
         "\t\t\tif (!flow_value.empty()) flow_chunk.assign(flow_value.begin()+offset, flow_value.begin()+offset+lhs.size());\n"
         "\t\t\tFlowProvenance::process_route(proc, &actions, lhs, rhs, flow_chunk);\n"
         "\t\t\tactions.push_back(RTLIL::SigSig(lhs, rhs));")
    edit(file, "\t\t\t\tit->first.remove2(pattern_bits, &it->second);",
         "\t\t\t\tFlowProvenance::process_discard(cs, it->first, pattern_bits, \"frontend_overwritten\");\n\t\t\t\tit->first.remove2(pattern_bits, &it->second);")
    edit(file, "\t\t\t\tRTLIL::CaseRule *default_case = nullptr;",
         "\t\t\t\tFlowProvenance::ProcessValue flow_default;\n"
         "\t\t\t\tfor (auto bit : this_case_eq_lvalue) flow_default.push_back(flow_subst.stdmap().count(bit) ? flow_subst.stdmap().at(bit) : FlowProvenance::ProcessRefs{});\n"
         "\t\t\t\tRTLIL::CaseRule *default_case = nullptr;")
    edit(file, "\t\t\t\t\tsubst_rvalue_map.save();", "\t\t\t\t\tsubst_rvalue_map.save();\n\t\t\t\t\tflow_subst.save();")
    edit(file, "\t\t\t\t\tsubst_rvalue_map.restore();", "\t\t\t\t\tsubst_rvalue_map.restore();\n\t\t\t\t\tflow_subst.restore();")
    edit(file, "addChunkActions(current_case->actions, this_case_eq_ltemp, this_case_eq_rvalue);",
         "addChunkActions(current_case->actions, this_case_eq_ltemp, this_case_eq_rvalue, false, flow_default);\n"
         "\t\t\t\t\tauto flow_control = ast->children[0]->flow_data();\n"
         "\t\t\t\t\tfor (auto &part : child->children) if (part->type != AST_BLOCK) { auto ids = part->flow_data(); flow_control.insert(ids.begin(), ids.end()); }\n"
         "\t\t\t\t\tFlowProvenance::process_branch(current_case, flow_control);")
    edit(file, "addChunkActions(default_case->actions, this_case_eq_ltemp, this_case_eq_rvalue);",
         "addChunkActions(default_case->actions, this_case_eq_ltemp, this_case_eq_rvalue, false, flow_default);")
    edit(file, "\t\t\t\tthis_case_eq_lvalue.replace(subst_lvalue_map.stdmap());",
         "\t\t\t\tauto flow_temps = FlowProvenance::process_values(proc, this_case_eq_ltemp);\n"
         "\t\t\t\tfor (int i = 0; i < GetSize(this_case_eq_lvalue); ++i) flow_subst.set(this_case_eq_lvalue[i], flow_temps[i]);\n"
         "\t\t\t\tthis_case_eq_lvalue.replace(subst_lvalue_map.stdmap());")
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
    edit("kernel/rtlil.cc", "RTLIL::Module::~Module()\n{",
         "RTLIL::Module::~Module()\n{\n\tFlowProvenance::module_removed(this);")
    edit("kernel/rtlil.cc", "\tmodule->name = new_name;\n\tadd(module);",
         "\tmodule->name = new_name;\n\tadd(module);\n\tFlowProvenance::module_renamed(module);")
    edit("kernel/register.cc", "\tpass->execute(args, design);\n\tpass->post_execute(state);",
         "\tFlowProvenance::step_begin(args, design);\n\tpass->execute(args, design);\n"
         "\tif (args[0] != \"flow_start\" && args[0] != \"flow_stop\") FlowProvenance::step_end(design);\n"
         "\tpass->post_execute(state);")
    # Process mux values follow the actual recursive lowering, including its defaults.
    f = "passes/proc/proc_mux.cc"
    edit(f, "const RTLIL::SigSpec &defval, bool ifxmode)\n{",
         "const RTLIL::SigSpec &defval, bool ifxmode, FlowProvenance::ProcessValue &flow_result, const FlowProvenance::ProcessValue &flow_default)\n{")
    edit(f, "\tRTLIL::SigSpec result = defval;", "\tRTLIL::SigSpec result = defval;\n\tflow_result = flow_default;")
    edit(f, "\t\tsig.replace(action.first, action.second, &result);",
         "\t\tFlowProvenance::process_consume(mod, cs, action, sig, flow_result);\n\t\tsig.replace(action.first, action.second, &result);")
    edit(f, "\t\t\t\tresult[i] = State::Sx;", "\t\t\t{ result[i] = State::Sx; flow_result[i].clear(); }")
    edit(f, "\t\tRTLIL::SigSpec initial_val = result;", "\t\tRTLIL::SigSpec initial_val = result;\n\t\tauto flow_initial = flow_result;")
    edit(f, "signal_to_mux_tree(mod, swcache, swpara, cs2, sig, initial_val, ifxmode);",
         "signal_to_mux_tree(mod, swcache, swpara, cs2, sig, initial_val, ifxmode, flow_value, flow_initial);")
    edit(f, "\t\t\tRTLIL::SigSpec value = signal_to_mux_tree", "\t\t\tFlowProvenance::ProcessValue flow_value;\n\t\t\tRTLIL::SigSpec value = signal_to_mux_tree")
    edit(f, "append_pmux(mod, sw->signal, cs2->compare, value, last_mux_cell, sw, cs2, ifxmode);",
         "append_pmux(mod, sw->signal, cs2->compare, value, last_mux_cell, sw, cs2, ifxmode, flow_value, flow_result);")
    edit(f, "gen_mux(mod, sw->signal, cs2->compare, value, result, last_mux_cell, sw, cs2, ifxmode);",
         "gen_mux(mod, sw->signal, cs2->compare, value, result, last_mux_cell, sw, cs2, ifxmode, flow_value, flow_result);")
    edit(f, "\t\tRTLIL::SigSpec value = signal_to_mux_tree(mod, swcache, swpara, &proc->root_case, sig, RTLIL::SigSpec(RTLIL::State::Sx, sig.size()), ifxmode);",
         "\t\tFlowProvenance::ProcessValue flow_result;\n"
         "\t\tRTLIL::SigSpec value = signal_to_mux_tree(mod, swcache, swpara, &proc->root_case, sig, RTLIL::SigSpec(RTLIL::State::Sx, sig.size()), ifxmode, flow_result, FlowProvenance::ProcessValue(sig.size()));\n"
         "\t\tFlowProvenance::process_output(proc, sig, flow_result);")
    # Unique signatures distinguish gen_mux / append_pmux from gen_cmp.
    edit(f, "RTLIL::Cell *&last_mux_cell, RTLIL::SwitchRule *sw, RTLIL::CaseRule *cs, bool ifxmode)",
         "RTLIL::Cell *&last_mux_cell, RTLIL::SwitchRule *sw, RTLIL::CaseRule *cs, bool ifxmode, const FlowProvenance::ProcessValue &flow_when, FlowProvenance::ProcessValue &flow_result)")
    edit(f, "RTLIL::Cell *last_mux_cell, RTLIL::SwitchRule *sw, RTLIL::CaseRule *cs, bool ifxmode)",
         "RTLIL::Cell *last_mux_cell, RTLIL::SwitchRule *sw, RTLIL::CaseRule *cs, bool ifxmode, const FlowProvenance::ProcessValue &flow_when, FlowProvenance::ProcessValue &flow_result)")
    edit(f, "\tif (compare.size() == 0 || when_signal == else_signal)\n\t\treturn when_signal;",
         "\tif (compare.size() == 0 || when_signal == else_signal) {\n"
         "\t\tflow_result = compare.empty() ? flow_when : FlowProvenance::process_join(flow_when, flow_result);\n\t\treturn when_signal;\n\t}")
    edit(f, "\tif (ctrl_sig.size() == 0)\n\t\treturn when_signal;",
         "\tif (ctrl_sig.size() == 0) { flow_result = flow_when; return when_signal; }")
    edit(f, "\t// prepare multiplexer output signal",
         "\tflow_result = FlowProvenance::process_join(flow_when, flow_result, FlowProvenance::process_branch(cs));\n"
         "\tFlowProvenance::ProcessScope flow_scope(FlowProvenance::process_union(flow_result), \"proc_mux\");\n\t// prepare multiplexer output signal")
    edit(f, "\tif (when_signal == last_mux_cell->getPort(ID::A))\n\t\treturn;",
         "\tif (when_signal == last_mux_cell->getPort(ID::A)) {\n"
         "\t\tflow_result = FlowProvenance::process_join(flow_when, flow_result);\n"
         "\t\tFlowProvenance::process_augment(last_mux_cell, FlowProvenance::process_union(flow_result)); return;\n\t}")
    edit(f, "\tlast_mux_cell->type = ID($pmux);",
         "\tflow_result = FlowProvenance::process_join(flow_when, flow_result, FlowProvenance::process_branch(cs));\n"
         "\tFlowProvenance::process_augment(last_mux_cell, FlowProvenance::process_union(flow_result));\n\tlast_mux_cell->type = ID($pmux);")
    # All comparator cells derive only from the controlling branch, not its data actions.
    edit(f, "\tRTLIL::Wire *cmp_wire = mod->addWire", "\tFlowProvenance::ProcessScope flow_compare(FlowProvenance::process_branch(cs), \"proc_compare\");\n\tRTLIL::Wire *cmp_wire = mod->addWire")
    edit("passes/proc/proc_dff.cc", "\t\tRTLIL::SigSpec insig = RTLIL::SigSpec(RTLIL::State::Sz, sig.size());",
         "\t\tFlowProvenance::ProcessScope flow_scope(FlowProvenance::process_state(proc, sig), \"proc_dff\", FlowProvenance::process_state_precision(proc));\n"
         "\t\tRTLIL::SigSpec insig = RTLIL::SigSpec(RTLIL::State::Sz, sig.size());")
    edit("passes/proc/proc_dff.cc", "\t\t\tconst auto rstval = async_rules.front().first;",
         "\t\t\tFlowProvenance::ProcessScope flow_trigger(FlowProvenance::process_reset_controls(proc), \"proc_reset_trigger\");\n\t\t\tconst auto rstval = async_rules.front().first;")
    edit("passes/proc/proc_dff.cc", "\t\t\tmod->connect(RTLIL::SigSig(sig, insig));",
         "\t\t\tFlowProvenance::process_connection(proc, sig, insig);\n\t\t\tmod->connect(RTLIL::SigSig(sig, insig));")
    edit("passes/proc/proc_dlatch.cc", "\t\tdb.module->connect(lhs, rhs);",
         "\t\tFlowProvenance::process_connection(proc, lhs, rhs);\n\t\tdb.module->connect(lhs, rhs);")
    edit("passes/proc/proc_prune.cc", "\t\t\tif (GetSize(conn_lhs)) {",
         "\t\t\tFlowProvenance::process_prune(module, cs, *it, sigmap, new_lhs, conn_lhs);\n\t\t\tif (GetSize(conn_lhs)) {")
    edit("passes/proc/proc_clean.cc", "\t\tfor (auto &action : sw->cases[0]->actions)",
         "\t\tFlowProvenance::process_case_move(sw->cases[0], parent);\n\t\tfor (auto &action : sw->cases[0]->actions)")
    edit("passes/proc/proc_clean.cc", "\t\t\t\tsw->cases.erase(sw->cases.begin()+(i--));\n\t\t\t\tdelete cs;",
         "\t\t\t\tFlowProvenance::process_dead_case(cs, \"proc_clean_unreachable\");\n\t\t\t\tsw->cases.erase(sw->cases.begin()+(i--));\n\t\t\t\tdelete cs;")
    edit("passes/proc/proc_clean.cc", "\t\t\tdelete sw->cases.back();\n\t\t\tsw->cases.pop_back();",
         "\t\t\tFlowProvenance::process_dead_case(sw->cases.back(), \"proc_clean_unreachable\");\n\t\t\tdelete sw->cases.back();\n\t\t\tsw->cases.pop_back();")
    edit("passes/proc/proc_rmdead.cc", "\t\t\t\tdelete sw->cases[i];",
         "\t\t\t\tFlowProvenance::process_dead_case(sw->cases[i]);\n\t\t\t\tdelete sw->cases[i];")
    edit("kernel/rtlil.cc", "RTLIL::CaseRule::~CaseRule()\n{",
         "RTLIL::CaseRule::~CaseRule()\n{\n\tFlowProvenance::process_case_removed(this);")
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
    # actual target and Q/D correspondence; successful mux donors are hooked below.
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
         "\tRTLIL::SigSpec flow_outputs;\n"
         "\tfor (auto &p : cell->connections()) if (cell->output(p.first)) flow_outputs.append(assign_map(p.second));\n"
         "\tFlowProvenance::RegionCapture flow_capture(flow_region, flow_outputs);")
    edit("passes/techmap/abc.cc", '\tlog_header(design, "Re-integrating ABC results.\\n");',
         '\tlog_header(design, "Re-integrating ABC results.\\n");\n'
         '\tstd::vector<int> flow_inputs, flow_outputs;\n'
         '\tfor (auto &si : run_abc.signal_list) if (si.is_port)\n'
         '\t\t(si.type == G(NONE) ? flow_inputs : flow_outputs).push_back(si.id);\n'
         '\tstd::vector<std::vector<int>> flow_fanins;\n'
         '\tfor (auto &n : run_abc.signal_list) flow_fanins.push_back({n.in1, n.in2, n.in3, n.in4});\n'
         '\tFlowProvenance::RegionScope flow_scope(flow_region, module, RTLIL::SigSpec(signal_bits),\n'
         '\t\tflow_inputs, flow_outputs, run_abc.per_run_tempdir_name, run_abc.config.exe_file, run_abc.abc_script, flow_fanins);')
    edit("passes/techmap/abc.cc", "\tfor (auto c : mapped_mod->cells())\n\t{",
         "\tfor (auto c : mapped_mod->cells())\n\t{\n"
         "\t\tdict<RTLIL::IdString, RTLIL::SigSpec> flow_ports;\n"
         "\t\tfor (auto &p : c->connections()) for (auto b : p.second)\n"
         "\t\t\tflow_ports[p.first].append(b.wire ? RTLIL::SigBit(module->wire(remap_name(b.wire->name)), b.offset) : b);\n"
         "\t\tFlowProvenance::AbcImportScope flow_node(c, flow_ports);")
    edit("passes/techmap/abc.cc", "\t\t\tsnprintf(buffer, 100, \"\\\\ys__n%d\", si.id);",
         "\t\t\tFlowProvenance::AbcImportScope flow_boundary(si.id);\n\t\t\tsnprintf(buffer, 100, \"\\\\ys__n%d\", si.id);")

    for f, anchor in (("passes/opt/opt_expr.cc", "\t\tcell->setPort(port, (invert_map.at(sig)));"),
                      ("passes/opt/opt_expr.cc", "\t\t\tcell->setPort(port, (invert_map.at(sig)));")):
        edit(f, "\n" + anchor, "\n" + anchor[:len(anchor)-len(anchor.lstrip())] + "FlowProvenance::polarity_contribution(cell, sig, assign_map);\n" + anchor)
    for value in ("a", "b"):
        # Exact successful reset/enable absorption branches (one each per function).
        f = root / "passes/opt/opt_dff.cc"
        text = f.read_text()
        old = "\t\t\t\t\tff.sig_d[i] = " + value + ";"
        if text.count(old) != 2: raise ValueError("opt_dff mux absorption anchors changed")
        for mode in ("srst", "ce"):
            hook = f'\t\t\t\t\tFlowProvenance::contribute_bit(cell, mbit.first, ff.sig_q[i], ff.sig_d[i], {value}, mbit.second, "{mode}");\n'
            # The srst function precedes ce in the pinned source.
            index = text.index(old, 0 if mode == "srst" else text.index("bool try_merge_ce("))
            text = text[:index] + hook + text[index:]
        f.write_text(text)
    # Recursive feedback: transfer a mux only if this search found feedback.
    edit("passes/opt/opt_dff.cc", "\t\t\t\treturn ret;\n\t\t\t}\n\t\t}\n\n\t\t// Specific path",
         "\t\t\t\tif (!ret.empty()) FlowProvenance::contribute_active(mbit.first, \"opt_dff_feedback\");\n\t\t\t\treturn ret;\n\t\t\t}\n\t\t}\n\n\t\t// Specific path")
    edit("passes/opt/opt_dff.cc", "\t\treturn ret;\n\t}\n\n\tctrl_t make_patterns_logic",
         "\t\tif (!ret.empty()) FlowProvenance::contribute_active(mbit.first, \"opt_dff_feedback\");\n\t\treturn ret;\n\t}\n\n\tctrl_t make_patterns_logic")
    edit("passes/opt/opt_muxtree.cc", "\t\t\tif (live_ports.empty()) {",
         "\t\t\tFlowProvenance::CellScope flow_scope(mi.cell, \"opt_muxtree_prune\");\n\t\t\tif (live_ports.empty()) {")
    edit("passes/opt/opt_reduce.cc", "\t\tcells.erase(cell);",
         "\t\tcells.erase(cell);\n\t\tFlowProvenance::CellScope flow_scope(cell, \"opt_reduce\");")
    edit("passes/opt/opt_reduce.cc", "\t\t\t\t\timported_children = true;",
         "\t\t\t\t\tFlowProvenance::contribute(cell, child_cell, \"opt_reduce_import\");\n\t\t\t\t\timported_children = true;")
    for name in ("opt_pmux", "opt_bmux"):
        edit("passes/opt/opt_reduce.cc", "\tvoid " + name + "(RTLIL::Cell *cell)\n\t{",
             "\tvoid " + name + "(RTLIL::Cell *cell)\n\t{\n\t\tFlowProvenance::CellScope flow_scope(cell, \"opt_reduce\");")


    f = "passes/proc/proc_arst.cc"
    edit(f, "bool polarity, bool unknown)\n{", "bool polarity, bool unknown, FlowProvenance::ProcessValue &flow_value)\n{")
    edit(f, "\tfor (auto &action : cs->actions) {",
         "\tfor (auto &action : cs->actions) {\n\t\tFlowProvenance::process_reset_consume(mod, cs, action, rspec, flow_value, unknown);")
    for mode in ("unknown", "false", "true"):
        edit(f, "apply_const(mod, rspec, rval, cs2, const_sig, polarity, " + mode + ");",
             "apply_const(mod, rspec, rval, cs2, const_sig, polarity, " + mode + ", flow_value);")
    edit(f, "\tRTLIL::SigSpec last_rval;", "\tFlowProvenance::ProcessValue flow_value(rspec.size());\n\tRTLIL::SigSpec last_rval;")
    edit(f, "apply_const(mod, rspec, rval, &proc->root_case, root_sig, polarity, false);",
         "apply_const(mod, rspec, rval, &proc->root_case, root_sig, polarity, false, flow_value);")
    edit(f, "\treturn rval;", "\tFlowProvenance::process_reset_bind(proc, sync, log_sig, flow_value);\n\treturn rval;")

    file = root / "passes/hierarchy/hierarchy.cc"
    text = file.read_text()
    anchor = "\t\t\t\tdesign->add(m);\n\t\t\t\ttop_mod = m;"
    if text.count(anchor) != 2:
        raise ValueError("Native top-parameter clone anchors changed")
    file.write_text(text.replace(anchor, "\t\t\t\tdesign->add(m);\n"
        "\t\t\t\tFlowProvenance::parameter_module_cloned(top_mod, m);\n\t\t\t\ttop_mod = m;"))
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
