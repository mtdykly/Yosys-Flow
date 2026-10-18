// Included inside FlowProvenance. Metadata does not affect RTLIL or pass choices.
struct ProcessNode {
    ProcessRefs inputs;
    Origins origins;
    J::object detail;
    std::string operation;
    int captured_step;
    bool bound = true, emitted = false, complete = true;
};
static std::map<std::string, ProcessNode> process_nodes;
static dict<const void*, dict<RTLIL::SigBit, ProcessRefs>> case_values;
static dict<RTLIL::CaseRule*, ProcessRefs> branch_values;
static dict<RTLIL::Process*, dict<RTLIL::SigBit, std::string>> process_slots;
static dict<RTLIL::Process*, dict<RTLIL::SigBit, RTLIL::SigBit>> process_roots;
struct ProcessBinding { ProcessRefs inputs; std::string operation, precision; };
static dict<RTLIL::Cell*, ProcessBinding> process_bindings;
static ProcessRefs process_active;
static std::string process_operation, process_precision;
static int process_id = 0;

ProcessRefs process_union(ProcessRefs a, const ProcessRefs &b) { a.insert(b.begin(), b.end()); return a; }
ProcessRefs process_union(const ProcessValue &value) {
    ProcessRefs refs;
    for (const auto &v : value) refs.insert(v.begin(), v.end());
    return refs;
}
static std::string process_node(const ProcessRefs &inputs, const Origins &origins,
                                const std::string &op, J::object detail = {}, bool bound = true) {
    auto ref = "native:process_" + std::to_string(++process_id);
    process_nodes.emplace(ref, ProcessNode{inputs, origins, detail, op, steps.empty() ? 0 : steps.back(), bound});
    return ref;
}
static Origins process_emit(const std::string &ref, std::set<std::string> &visiting) {
    auto &node = process_nodes.at(ref);
    if (node.emitted) return node.origins;
    if (!visiting.insert(ref).second) {
        gap("process_proof_cycle"); node.complete = false; return {};
    }
    if (!node.bound) { gap("process_unbound_temporary"); node.complete = false; }
    for (const auto &input : node.inputs) {
        auto s = process_emit(input, visiting);
        node.origins.insert(s.begin(), s.end());
        node.complete &= process_nodes.at(input).complete;
    }
    visiting.erase(ref);
    auto row = node.detail;
    row["record_type"] = "process_node"; row["ref"] = ref;
    row["inputs"] = J::array(node.inputs.begin(), node.inputs.end());
    row["origins"] = ids(node.origins); row["operation"] = node.operation;
    row["captured_step_id"] = node.captured_step;
    row["complete"] = node.complete;
    emit(row); node.emitted = true;
    return node.origins;
}
static Origins process_emit(const ProcessRefs &refs) {
    Origins result;
    std::set<std::string> visiting;
    for (const auto &ref : refs) {
        auto s = process_emit(ref, visiting); result.insert(s.begin(), s.end());
    }
    return result;
}
static std::string process_slot(RTLIL::Process *proc, RTLIL::SigBit bit) {
    if (!process_slots[proc].count(bit))
        process_slots[proc][bit] = process_node({}, {}, "temporary_route", {{"signal", signal(bit)}}, false);
    return process_slots.at(proc).at(bit);
}
ProcessValue process_values(RTLIL::Process *proc, const RTLIL::SigSpec &sig) {
    ProcessValue value(sig.size());
    if (!tracks(proc->module)) return value;
    for (int i = 0; i < sig.size(); ++i)
        if (temporaries.count(proc) && temporaries.at(proc).count(sig[i]))
            value[i].insert(process_slot(proc, sig[i]));
    return value;
}
ProcessValue process_action(RTLIL::Process *proc, RTLIL::CaseRule *cs,
    const RTLIL::SigSpec &lhs, const RTLIL::SigSpec &rhs, const RTLIL::SigSpec &target,
    const Origins &statement, const Origins &data, const Origins &control, const Origins &target_sources) {
    ProcessValue value(lhs.size());
    if (!tracks(proc->module)) return value;
    Origins origins = statement;
    for (const auto &role : {data, target_sources}) origins.insert(role.begin(), role.end());
    auto action_ref = "native:action_" + std::to_string(++process_id);
    J::array bits;
    for (int i = 0; i < lhs.size(); ++i) {
        auto r = process_node({}, origins, "action_bit", {{"action", action_ref}, {"offset", i},
            {"roles", J::object{{"statement", ids(statement)}, {"data", ids(data)},
                              {"target", ids(target_sources)}}}, {"enclosing_controls", ids(control)}});
        value[i].insert(r); bits.push_back(r);
        case_values[&cs->actions][lhs[i]] = value[i];
        process_emit(value[i]);
    }
    auto fallback = origins; fallback.insert(control.begin(), control.end());
    action(proc, target, fallback); // Original action context; precise bindings use the action-bit proof.
    emit({{"record_type", "process_action_detail"}, {"ref", action_ref}, {"process", proc->name.str()},
        {"module", proc->module->name.str()}, {"lhs", signal(lhs)}, {"rhs", signal(rhs)},
        {"target", signal(target)}, {"bits", bits}, {"origins", ids(origins)}, {"enclosing_controls", ids(control)}});
    return value;
}
void process_route(RTLIL::Process *proc, const void *actions, const RTLIL::SigSpec &lhs,
                   const RTLIL::SigSpec &rhs, const ProcessValue &provided) {
    if (!tracks(proc->module)) return;
    auto value = provided.empty() ? process_values(proc, rhs) : provided;
    log_assert(GetSize(value) == lhs.size());
    for (int i = 0; i < lhs.size(); ++i) case_values[actions][lhs[i]] = value[i];
}
void process_branch(RTLIL::CaseRule *cs, const Origins &control) {
    if (!enabled() || control.empty()) return;
    auto ref = process_node({}, control, "branch_control", {{"roles", J::object{{"control", ids(control)}}}});
    branch_values[cs] = {ref}; process_emit(ProcessRefs{ref});
}
ProcessRefs process_branch(RTLIL::CaseRule *cs) { return branch_values.at(cs, {}); }
void process_consume(RTLIL::Module *module, RTLIL::CaseRule *cs, const RTLIL::SigSig &action,
                     const RTLIL::SigSpec &sig, ProcessValue &value) {
    if (!tracks(module)) return;
    auto bits = action.first.to_sigbit_pool();
    auto recorded = case_values.at(&cs->actions, {});
    for (int i = 0; i < sig.size(); ++i) if (bits.count(sig[i])) {
        if (recorded.count(sig[i])) value[i] = recorded.at(sig[i]);
        else {
            gap("untracked_process_action");
            value[i] = {process_node({}, {}, "untracked_action", {}, false)};
        }
    }
}
void process_discard(RTLIL::CaseRule *cs, const RTLIL::SigSpec &lhs,
                     const pool<RTLIL::SigBit> &bits, const std::string &reason) {
    if (!enabled() || !case_values.count(&cs->actions)) return;
    ProcessRefs refs;
    RTLIL::SigSpec removed;
    for (auto bit : lhs) if (bits.count(bit)) {
        auto values = case_values.at(&cs->actions).at(bit, {});
        for (const auto &ref : values)
            if (process_nodes.at(ref).operation == "action_bit") refs.insert(ref);
        case_values.at(&cs->actions).erase(bit); removed.append(bit);
    }
    if (refs.empty()) return;
    auto origins = process_emit(refs);
    emit({{"record_type", "process_discard"}, {"inputs", J::array(refs.begin(), refs.end())},
          {"lhs", signal(removed)}, {"reason", reason}, {"origins", ids(origins)},
          {"meaning", "assignment_to_target_removed; value_may_have_other_uses"}});
}
void process_output(RTLIL::Process *proc, const RTLIL::SigSpec &sig, const ProcessValue &value) {
    if (!tracks(proc->module)) return;
    log_assert(sig.size() == GetSize(value));
    for (int i = 0; i < sig.size(); ++i) {
        // Only compiler-created process temporaries are traversed. User nets are boundaries.
        if (!temporaries.count(proc) || !temporaries.at(proc).count(sig[i])) continue;
        auto &node = process_nodes.at(process_slot(proc, sig[i]));
        log_assert(!node.emitted);
        node.inputs = value[i]; node.bound = true;
        node.captured_step = steps.empty() ? 0 : steps.back();
    }
}
void process_prune(RTLIL::Module *module, RTLIL::CaseRule *cs, const RTLIL::SigSig &action,
                   const SigMap &map, const RTLIL::SigSpec &kept, const RTLIL::SigSpec &promoted) {
    if (!tracks(module)) return;
    pool<RTLIL::SigBit> discarded;
    // The pass supplies its actual kept/promoted bits and its actual alias map.
    for (auto bit : action.first) {
        auto mapped = map(bit);
        auto refs = case_values.at(&cs->actions, {}).at(bit, {});
        if (promoted.to_sigbit_pool().count(mapped)) {
            for (auto &pr : module->processes) if (temporaries.count(pr.second) && temporaries.at(pr.second).count(bit))
                process_output(pr.second, bit, ProcessValue{refs});
        } else if (!kept.to_sigbit_pool().count(mapped)) discarded.insert(bit);
        if (kept.to_sigbit_pool().count(mapped)) case_values[&cs->actions][mapped] = refs;
    }
    process_discard(cs, action.first, discarded, "proc_prune_overwritten");
}
void process_case_move(RTLIL::CaseRule *from, RTLIL::CaseRule *to) {
    if (!case_values.count(&from->actions)) return;
    auto values = case_values.at(&from->actions);
    for (const auto &a : values) case_values[&to->actions][a.first] = a.second;
}
void process_case_removed(RTLIL::CaseRule *cs) { case_values.erase(&cs->actions); branch_values.erase(cs); }
void process_dead_case(RTLIL::CaseRule *cs, const std::string &reason) {
    for (auto &a : cs->actions) {
        pool<RTLIL::SigBit> bits; for (auto b : a.first) bits.insert(b);
        process_discard(cs, a.first, bits, reason);
    }
    for (auto sw : cs->switches) for (auto sub : sw->cases) process_dead_case(sub, reason);
}
ProcessValue process_join(const ProcessValue &a, const ProcessValue &b, const ProcessRefs &control) {
    log_assert(a.size() == b.size());
    ProcessValue result(a.size());
    for (size_t i = 0; i < a.size(); ++i) result[i] = process_union(process_union(a[i], b[i]), control);
    return result;
}
ProcessScope::ProcessScope(const ProcessRefs &refs, const std::string &op, const std::string &p) :
    scope({}, "process_pending", false), previous_refs(process_active),
    previous_operation(process_operation), previous_precision(process_precision) {
    process_active = refs; process_operation = op; process_precision = p;
}
ProcessScope::~ProcessScope() {
    process_active = previous_refs; process_operation = previous_operation; process_precision = previous_precision;
}
static void process_created(RTLIL::Cell *cell) {
    if (!process_operation.empty()) process_bindings[cell] = {process_active, process_operation, process_precision};
}
static void process_retired(RTLIL::Cell *cell) {
    if (process_bindings.count(cell)) {
        gap("process_cell_retired_before_binding"); process_bindings.erase(cell);
    }
}
void process_augment(RTLIL::Cell *cell, const ProcessRefs &refs) {
    if (process_bindings.count(cell)) process_bindings.at(cell).inputs = process_union(process_bindings.at(cell).inputs, refs);
}
void process_connection(RTLIL::Process *proc, const RTLIL::SigSpec &lhs, const RTLIL::SigSpec &rhs) {
    if (!tracks(proc->module)) return;
    auto values = process_values(proc, rhs);
    for (int i = 0; i < lhs.size(); ++i) {
        auto refs = values[i];
        // proc_dlatch may already have resolved aliases in rhs. Original target
        // temporaries retain the semantic route, independent of those aliases.
        auto target = original(proc, lhs[i]);
        if (refs.empty() && process_roots.at(proc, {}).count(target))
            refs.insert(process_slot(proc, process_roots.at(proc).at(target)));
        auto origins = process_emit(refs);
        bool complete = true;
        for (const auto &r : refs) complete &= process_nodes.at(r).complete;
        assignment(proc->module, lhs[i], rhs[i], origins, complete ? "process_action" : "process_signal_candidate");
        // The assignment event is followed by its independently checked proof.
        emit({{"record_type", "process_connection_binding"}, {"ref", connections.back().ref},
              {"inputs", J::array(refs.begin(), refs.end())}, {"origins", ids(origins)}});
    }
}
static void process_clone_case(RTLIL::CaseRule *from, RTLIL::CaseRule *to, RTLIL::Module *module) {
    auto values = case_values.at(&from->actions, {});
    for (const auto &v : values) {
        auto bit = v.first; if (bit.wire) bit.wire = module->wire(bit.wire->name);
        case_values[&to->actions][bit] = v.second;
    }
    if (branch_values.count(from)) { auto refs = branch_values.at(from); branch_values[to] = refs; }
    for (size_t i = 0; i < from->switches.size(); ++i)
        for (size_t j = 0; j < from->switches[i]->cases.size(); ++j)
            process_clone_case(from->switches[i]->cases[j], to->switches[i]->cases[j], module);
}
void process_clone(RTLIL::Process *from, RTLIL::Process *to) {
    process_clone_case(&from->root_case, &to->root_case, to->module);
    auto slots = process_slots.at(from, {});
    for (const auto &s : slots) { auto bit = s.first; bit.wire = to->module->wire(bit.wire->name); process_slots[to][bit] = s.second; }
    auto roots = process_roots.at(from, {});
    for (const auto &s : roots) { auto a = s.first, b = s.second; a.wire = to->module->wire(a.wire->name); b.wire = to->module->wire(b.wire->name); process_roots[to][a] = b; }
}
void process_flush() {
    for (auto &item : process_bindings) {
        auto cell = item.first;
        if (!cells.count(cell)) continue;
        auto &binding = item.second;
        auto origins = process_emit(binding.inputs);
        observe(cell, "before_process_binding");
        auto &info = cells.at(cell);
        auto old = ref(info);
        ++info.version; info.origins = origins; info.precision = binding.precision;
        for (const auto &r : binding.inputs) if (!process_nodes.at(r).complete) info.precision = "process_signal_candidate";
        emit({{"record_type", "process_binding"}, {"input", old}, {"ref", ref(info)},
            {"inputs", J::array(binding.inputs.begin(), binding.inputs.end())},
            {"origins", ids(info.origins)}, {"precision", info.precision}, {"operation", binding.operation}});
    }
    process_bindings.clear();
}
static void process_temporary(RTLIL::Process *proc, const RTLIL::SigSpec &from, const RTLIL::SigSpec &to) {
    for (int i = 0; i < from.size(); ++i) if (from[i].wire && to[i].wire) {
        auto key = original(proc, from[i]);
        if (!process_roots[proc].count(key)) process_roots[proc][key] = to[i];
    }
}
static void process_release(RTLIL::Process *proc) {
    process_slots.erase(proc); process_roots.erase(proc);
}
void process_reset() {
    process_nodes.clear(); case_values.clear(); branch_values.clear(); process_slots.clear();
    process_roots.clear(); process_bindings.clear(); process_active.clear(); process_operation.clear(); process_precision.clear(); process_id = 0;
}
