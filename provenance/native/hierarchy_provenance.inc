// Actual hierarchy bindings and flatten copies. Never modifies synthesis IR.
static Origins flatten_origins(const Origins &origins) {
    Origins result;
    for (int old : origins) {
        if (!flatten_sources.count(old)) {
            auto row = source_rows.at(old);
            auto path = flatten_path;
            auto prior = row.find("instance_path");
            if (prior != row.end()) for (const auto &part : prior->second.array_items()) path.push_back(part);
            int id = ++source_id;
            row["id"] = id; row["cloned_from"] = old;
            row["instance_path"] = path; row["context_module"] = flatten_parent;
            row["flatten_event"] = flatten_context;
            source_rows[id] = row; flatten_sources[old] = id; emit(row);
        }
        result.insert(flatten_sources.at(old));
    }
    return result;
}
void flatten_begin(RTLIL::Cell *instance, RTLIL::Module *child, const dict<RTLIL::Wire*, RTLIL::Wire*> &wires) {
    if (!tracks(instance->module)) return;
    log_assert(child->processes.empty()); // Default synth lowers proc before flatten.
    observe(instance, "before_flatten");
    flatten_context = "native:flatten_" + std::to_string(++object_id);
    flatten_parent = instance->module->name.str();
    flatten_path = J::array{instance->name.unescape()}; flatten_sources.clear();
    J::object mapping;
    for (auto &it : wires) mapping[it.first->name.str()] = it.second->name.str();
    emit({{"record_type", "flatten_instance"}, {"ref", flatten_context},
        {"instance", ref(cells.at(instance))}, {"module", flatten_parent},
        {"name", instance->name.str()}, {"child_module", child->name.str()}, {"wire_map", mapping}});
}
FlattenScope::FlattenScope(RTLIL::Cell *original) :
    scope(enabled() && cells.count(original) ? flatten_origins(cells.at(original).origins) : Origins{},
          enabled() && cells.count(original) ? cells.at(original).precision : "cell", false), previous(cloning_source) {
    if (enabled() && cells.count(original)) {
        observe(original, "before_flatten_copy"); cloning_source = ref(cells.at(original));
    }
}
FlattenScope::~FlattenScope() { cloning_source = previous; }
void flatten_copied(RTLIL::Cell *cell) { if (tracks(cell->module)) observe(cell, "flatten_copy"); }
void flatten_connections(RTLIL::Module *child, RTLIL::Module *parent, const dict<RTLIL::Wire*, RTLIL::Wire*> &wires) {
    if (!tracks(parent)) return;
    auto remap = [&wires](RTLIL::SigSpec sig) {
        for (auto &bit : sig) if (bit.wire) bit.wire = wires.at(bit.wire);
        return sig;
    };
    auto previous = connections;
    for (auto &info : previous) if (info.module == child && !info.lhs.empty()) {
        cloning_source = info.ref;
        cloning_offsets = info.offsets;
        assignment(parent, remap(info.lhs), remap(info.rhs), flatten_origins(info.origins), info.precision, info.ref);
        cloning_source.clear(); cloning_offsets.clear();
    }
    flatten_context.clear(); flatten_sources.clear();
}
struct HierarchySnapshot : Pass {
    HierarchySnapshot() : Pass("flow_hierarchy", "record elaborated instance bindings") {}
    void execute(std::vector<std::string> args, RTLIL::Design *design) override {
        if (args.size()!=1 || !tracks(design)) log_cmd_error("flow_hierarchy requires active provenance.\n");
        J::object modules;
        pool<RTLIL::Module*> active_modules, visited;
        std::function<void(RTLIL::Module*)> visit = [&](RTLIL::Module *m) {
            if (active_modules.count(m)) log_error("Recursive hierarchy is not supported.\n");
            if (visited.count(m)) return;
            if (m->get_blackbox_attribute() || m->get_bool_attribute(ID::whitebox)) log_error("Opaque modules are not supported.\n");
            active_modules.insert(m);
            J::object ports, instances, params;
            for (auto wire : m->wires()) if (wire->port_id) {
                if (wire->port_input && wire->port_output) log_error("Hierarchical inout ports are not supported.\n");
                ports[wire->name.str()] = J::object{{"direction", wire->port_input ? "input" : "output"}, {"bits", signal(wire)}};
            }
            for (auto &it : m->parameter_default_values) params[it.first.str()] = it.second.as_string();
            for (auto cell : m->cells()) if (auto child = design->module(cell->type)) {
                observe(cell, "hierarchy_binding");
                instances[cell->name.str()] = J::object{{"ref", ref(cells.at(cell))}, {"state", state(cell)}};
                visit(child);
            } else if (cell->type.isPublic()) log_error("Unknown module in recorded hierarchy.\n");
            modules[m->name.str()] = J::object{{"ports", ports}, {"instances", instances}, {"parameters", params}};
            active_modules.erase(m); visited.insert(m);
        };
        auto top = design->top_module();
        if (!top) log_error("Recorded hierarchy requires a top module.\n");
        visit(top);
        emit({{"record_type", "hierarchy_snapshot"}, {"top", top->name.str()}, {"modules", modules}});
    }
} HierarchySnapshot;
