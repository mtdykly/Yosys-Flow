// Native synthesis recorder. Semantic bindings and structural observations are
// deliberately separate; a checkpoint is never evidence of a source relation.
#include "kernel/flow_provenance.h"
#include "kernel/register.h"
#include "libs/json11/json11.hpp"
#include <fstream>

YOSYS_NAMESPACE_BEGIN
namespace FlowProvenance {
using J = json11::Json;
static std::ofstream output;
static std::string trace_path, active_region;
static int abc_id = 0;
static std::string abc_node;
static int abc_boundary = -1;
static RTLIL::SigSpec captured_outputs;
static RTLIL::Design *root_design = nullptr;
static pool<RTLIL::Module*> pending_modules;
static Origins active;
static std::vector<std::string> active_inputs;
static std::string operation;
static pool<RTLIL::SigBit> active_outputs;
static Region *capturing_region = nullptr;
static std::string precision = "cell";
static CellScope *cell_scope = nullptr;
static std::string merged_precision(const std::string &, const std::string &);
static int sequence = 0, source_id = 0, object_id = 0, step_id = 0;
static std::vector<int> steps;
static std::vector<std::string> passes;
struct CellInfo { int id, version = 0; Origins origins; std::string precision, state; };
static dict<RTLIL::Cell*, CellInfo> cells;
struct ConnectionInfo {
    RTLIL::Module *module;
    RTLIL::SigSpec lhs, rhs;
    std::string ref;
    std::vector<int> offsets;
    int revision = 0;
    Origins origins;
    std::string precision;
};
static std::vector<ConnectionInfo> connections;
static dict<RTLIL::Process*, dict<RTLIL::SigBit, Origins>> actions;
static dict<RTLIL::Process*, dict<RTLIL::SigBit, RTLIL::SigBit>> temporaries;
static std::set<std::string> gaps;
static std::map<int, J::object> source_rows;
static std::string cloning_source, flatten_context;
static std::vector<int> cloning_offsets;
static std::map<int, int> flatten_sources;
static std::string flatten_parent;
static J::array flatten_path;
static void process_created(RTLIL::Cell *cell);
static void process_retired(RTLIL::Cell *cell);
static void process_release(RTLIL::Process *proc);
static void process_temporary(RTLIL::Process *proc, const RTLIL::SigSpec &from, const RTLIL::SigSpec &to);
static J ids(const Origins &value) { return J::array(value.begin(), value.end()); }
bool enabled() { return output.is_open(); }
bool tracks(RTLIL::Design *design) { return enabled() && design == root_design; }
static bool tracks(RTLIL::Module *module) { return enabled() && (module->design == root_design || pending_modules.count(module)); }
ModuleScope::ModuleScope(RTLIL::Design *design, RTLIL::Module *m) : module(m) {
    if (tracks(design)) pending_modules.insert(m);
}
ModuleScope::~ModuleScope() { pending_modules.erase(module); }
static void emit(J::object row) {
    if (!enabled()) return;
    row["seq"] = ++sequence;
    row["step_id"] = steps.empty() ? 0 : steps.back();
    output << J(row).dump() << '\n';
    if (!output) log_error("Cannot write native provenance log.\n");
}
static J signal(const RTLIL::SigSpec &sig) {
    J::array result;
    for (auto bit : sig) {
        if (bit.wire) result.push_back(J::object{{"wire", bit.wire->name.str()}, {"offset", bit.offset}});
        else result.push_back(RTLIL::Const(bit.data).as_string());
    }
    return result;
}
static std::string ref(const CellInfo &info) {
    return "native:cell_" + std::to_string(info.id) + "@" + std::to_string(info.version);
}
static J::object state(RTLIL::Cell *cell) {
    J::object ports, params, attrs, directions;
    for (auto &it : cell->connections()) {
        ports[it.first.str()] = signal(it.second);
        auto child = root_design ? root_design->module(cell->type) : nullptr;
        auto wire = child ? child->wire(it.first) : nullptr;
        directions[it.first.str()] = wire ? (wire->port_input && wire->port_output ? "inout" : wire->port_input ? "input" : wire->port_output ? "output" : "unknown") :
            cell->input(it.first) ? "input" : cell->output(it.first) ? "output" : "unknown";
    }
    for (auto &it : cell->parameters) {
        std::string value = it.second.as_string();
        if ((it.second.flags & RTLIL::ConstFlags::CONST_FLAG_STRING) != 0) {
            value = it.second.decode_string();
            int mode = 0;
            for (char c : value) {
                if (mode == 0) { if (c == ' ') mode = 1; else if (c != '0' && c != '1' && c != 'x' && c != 'z') mode = 2; }
                else if (mode == 1 && c != ' ') mode = 2;
            }
            if (mode < 2) value += " ";
        }
        params[it.first.str()] = value;
    }
    // Source text is already represented by source records. Parameters are
    // compared bit-for-bit; full attributes are checked by the baseline JSON.
    return {{"module", cell->module->name.str()}, {"name", cell->name.str()}, {"type", cell->type.str()},
            {"connections", ports}, {"parameters", params}, {"port_directions", directions}};
}
int source(const std::string &module, const std::string &file, int line, int column,
           int end_line, int end_column, const std::string &kind) {
    if (!enabled()) return 0;
    int id = ++source_id;
    source_rows[id] = {{"record_type", "source"}, {"id", id}, {"module", module}, {"file", file},
          {"line", line}, {"column", column}, {"end_line", end_line}, {"end_column", end_column}, {"kind", kind}};
    emit(source_rows[id]);
    return id;
}
void gap(const std::string &reason) {
    if (enabled() && gaps.insert(reason).second)
        emit({{"record_type", "coverage_gap"}, {"reason", reason}});
}
Scope::Scope(const Origins &origins, const std::string &p, bool inherit) : previous(active), previous_precision(precision) {
    if (!inherit) active.clear();
    active.insert(origins.begin(), origins.end());
    precision = p;
}
Scope::~Scope() { active = previous; precision = previous_precision; }
static void observe(RTLIL::Cell *cell, const std::string &capture) {
    if (!cells.count(cell)) return;
    if (cell->is_builtin_ff()) log_error("Combinational-only flow rejects state cell %s (%s).\n", log_id(cell), log_id(cell->type));
    auto &info = cells.at(cell);
    auto data = state(cell);
    auto serial = J(data).dump();
    if (serial == info.state) return;
    auto old = ref(info);
    ++info.version;
    emit({{"record_type", "cell_state"}, {"input", old}, {"ref", ref(info)},
          {"state", data}, {"origins", ids(info.origins)}, {"precision", info.precision},
          {"capture", capture}, {"operation", operation}});
    info.state = serial;
}
static Origins collect_origins(const std::vector<RTLIL::Cell*> &targets) {
    Origins result;
    for (auto c : targets) if (cells.count(c)) result.insert(cells.at(c).origins.begin(), cells.at(c).origins.end());
    return result;
}
static std::string collect_precision(const std::vector<RTLIL::Cell*> &targets) {
    if (targets.size() == 1 && cells.count(targets[0])) return cells.at(targets[0]).precision;
    std::string result = "cell_group_candidate";
    for (auto c : targets) if (cells.count(c)) result = merged_precision(result, cells.at(c).precision);
    return result;
}
CellScope::CellScope(RTLIL::Cell *cell, const std::string &op) : CellScope(std::vector<RTLIL::Cell*>{cell}, op) {}
CellScope::CellScope(const std::vector<RTLIL::Cell*> &cs, const std::string &op) :
    scope(collect_origins(cs), collect_precision(cs), false),
    previous_inputs(active_inputs), previous_operation(operation), previous_outputs(active_outputs) {
    previous_scope = cell_scope; cell_scope = this;
    active_inputs.clear(); active_outputs.clear(); operation = op;
    for (auto c : cs) if (cells.count(c)) {
        observe(c, "before_instrumented_rewrite");
        targets.emplace_back(c, cells.at(c).id);
        active_inputs.push_back(ref(cells.at(c)));
        if (op == "techmap" && cs.size() == 1) {
            mapping_input = ref(cells.at(c));
            SigMap map(c->module);
            for (auto &p : c->connections()) if (c->output(p.first)) {
                original_outputs[p.first.str()] = p.second;
                for (int i = 0; i < p.second.size(); ++i) output_positions[map(p.second[i])].push_back({p.first.str(), i});
            }
        }
        for (auto &port : c->connections()) if (c->output(port.first))
            for (auto bit : port.second) active_outputs.insert(bit);
    }
}
CellScope::~CellScope() {
    if (!mapping_input.empty()) for (auto [c, id] : generated) if (cells.count(c) && cells.at(c).id == id) {
        observe(c, "techmap_output");
        SigMap map(c->module);
        J::array links;
        for (auto &p : c->connections()) if (c->output(p.first)) for (int i = 0; i < p.second.size(); ++i) {
            auto key = map(p.second[i]);
            if (!output_positions.count(key)) continue;
            for (const auto &source : output_positions.at(key)) links.push_back(J::object{{"port", p.first.str()}, {"offset", i},
                {"source_port", source.first}, {"source_offset", source.second}, {"signal", signal(key)},
                {"source_signal", signal(original_outputs.at(source.first)[source.second])}, {"target_signal", signal(p.second[i])}});
        }
        emit({{"record_type", "techmap_bit_map"}, {"input", mapping_input}, {"ref", ref(cells.at(c))},
            {"links", links}, {"meaning", "actual_output_boundary_correspondence"}});
    }
    cell_scope = previous_scope;
    for (auto [cell, id] : targets)
        if (cells.count(cell) && cells.at(cell).id == id) observe(cell, "instrumented_rewrite");
    active_inputs = previous_inputs; operation = previous_operation; active_outputs = previous_outputs;
}
RegionCapture::RegionCapture(Region &region, const RTLIL::SigSpec &outputs) : previous(capturing_region), previous_outputs(captured_outputs) { capturing_region = &region; captured_outputs = outputs; }
RegionCapture::~RegionCapture() { capturing_region = previous; captured_outputs = previous_outputs; }
RegionScope::RegionScope(const Region &region, RTLIL::Module *module, const RTLIL::SigSpec &signals,
    const std::vector<int> &input_ports, const std::vector<int> &output_ports,
    const std::string &tempdir, const std::string &executable, const std::string &script,
    const std::vector<std::vector<int>> &fanins) :
    scope(region.origins, "abc_region_candidate", false), previous_inputs(active_inputs), previous_operation(operation), previous_outputs(active_outputs), previous_region(active_region) {
    active_inputs = region.inputs; operation = "abc_region";
    active_outputs.clear();
    for (auto bit : region.outputs) active_outputs.insert(bit);
    if (!tracks(module)) return;
    active_region = region.id;
    log_assert(!active_region.empty());
    J::object artifacts;
    for (auto name : {"input.blif", "output.blif", "script", "input.blif.flow.jsonl"}) {
        auto path = trace_path + "." + active_region.substr(7) + "." + name;
        std::ifstream existing(path);
        if (existing.good()) log_error("Refusing to overwrite ABC provenance artifact.\n");
        std::ofstream target(path, std::ios::binary);
        if (std::string(name) == "script") target << script;
        else {
            std::ifstream input(tempdir + "/" + name, std::ios::binary);
            if (!input) log_error("Cannot read ABC provenance artifact.\n");
            target << input.rdbuf();
        }
        target.close();
        if (!target) log_error("Cannot write ABC provenance artifact.\n");
        artifacts[name] = path.substr(path.find_last_of('/') + 1);
    }
    J::array nodes;
    for (int i = 0; i < signals.size(); ++i) {
        J::array inputs; for (int n : fanins.at(i)) if (n >= 0) inputs.push_back(n);
        nodes.push_back(J::object{{"id", i}, {"fanins", inputs}, {"cell", region.node_cells.at(signals[i], "")}});
    }
    emit({{"input_nodes", nodes}, {"record_type", "abc_region"}, {"internal_trace_schema", 4}, {"module", module->name.str()}, {"invocation_id", active_region},
        {"inputs", region.inputs}, {"origins", ids(region.origins)}, {"signals", signal(signals)},
        {"input_ports", input_ports}, {"output_ports", output_ports}, {"artifacts", artifacts}, {"executable", executable},
        {"precision", "abc_region_candidate"}});
}
RegionScope::~RegionScope() { active_inputs = previous_inputs; operation = previous_operation; active_outputs = previous_outputs; active_region = previous_region; }
AbcImportScope::AbcImportScope(RTLIL::Cell *mapped, const dict<RTLIL::IdString, RTLIL::SigSpec> &target_ports) : previous(abc_node), previous_boundary(abc_boundary) {
    abc_node = mapped->name.str(); abc_boundary = -1;
    J::object ports, targets;
    for (auto &port : target_ports) targets[port.first.str()] = signal(port.second);
    for (auto &port : mapped->connections()) ports[port.first.str()] = signal(port.second);
    emit({{"record_type", "abc_import_node"}, {"invocation_id", active_region},
        {"node", abc_node}, {"type", mapped->type.str()}, {"ports", ports}, {"target_ports", targets}});
}
AbcImportScope::AbcImportScope(int boundary) : previous(abc_node), previous_boundary(abc_boundary) {
    abc_node.clear(); abc_boundary = boundary;
}
AbcImportScope::~AbcImportScope() { abc_node = previous; abc_boundary = previous_boundary; }
void created(RTLIL::Cell *cell) {
    if (!tracks(cell->module)) return;
    if (cell->is_builtin_ff()) log_error("Combinational-only flow rejects state cell %s (%s).\n", log_id(cell), log_id(cell->type));
    cells[cell] = {++object_id, 0, active, precision, ""};
    emit({{"record_type", "cell_create"}, {"ref", ref(cells.at(cell))}, {"origins", ids(active)},
          {"cloned_from", cloning_source}, {"flatten_event", cloning_source.empty() ? "" : flatten_context},
          {"precision", precision}, {"module", cell->module->name.str()}, {"name", cell->name.str()},
          {"inputs", active_inputs}, {"operation", operation}, {"abc_invocation", active_region}, {"abc_node", abc_node}, {"abc_boundary", abc_boundary}});
    if (cell_scope) cell_scope->generated.emplace_back(cell, cells.at(cell).id);
    process_created(cell);
    if (active.empty() && precision != "process_pending") gap("cell_created_without_semantic_source_binding");
}
void removed(RTLIL::Cell *cell) {
    if (!tracks(cell->module)) return;
    if (!cells.count(cell)) { gap("untracked_cell_removal"); return; }
    observe(cell, "before_removal");
    if (capturing_region) {
        if (capturing_region->id.empty()) capturing_region->id = "native:abc_" + std::to_string(++abc_id);
        const auto &info = cells.at(cell);
        capturing_region->inputs.push_back(ref(info));
        for (auto bit : captured_outputs) capturing_region->node_cells[bit] = ref(info);
        capturing_region->origins.insert(info.origins.begin(), info.origins.end());
        for (auto &port : cell->connections()) if (cell->output(port.first)) capturing_region->outputs.append(port.second);
    }
    emit({{"record_type", "cell_remove"}, {"ref", ref(cells.at(cell))},
          {"abc_invocation", capturing_region ? capturing_region->id : ""},
          {"abc_outputs", capturing_region ? signal(captured_outputs) : J::array{}},
          {"reason", operation.empty() ? (passes.empty() ? "outside_pass" : passes.back()) : operation}});
    process_retired(cell);
    cells.erase(cell); // Explicit retirement prevents allocator address reuse.
}
static std::string merged_precision(const std::string &a, const std::string &b) {
    static const std::map<std::string, int> rank = {{"cell", 0}, {"process_action", 1},
        {"cell_group_candidate", 2}, {"process_signal_candidate", 3}, {"abc_region_candidate", 4}, {"process_pending", 5}};
    return rank.at(a) >= rank.at(b) ? a : b;
}
void merge(RTLIL::Cell *keep, RTLIL::Cell *remove) {
    if (!tracks(keep->module)) return;
    if (!cells.count(keep) || !cells.count(remove)) { gap("untracked_merge_input"); return; }
    auto &k = cells.at(keep); auto &r = cells.at(remove);
    auto old = ref(k);
    k.origins.insert(r.origins.begin(), r.origins.end());
    k.precision = merged_precision(k.precision, r.precision);
    ++k.version;
    emit({{"record_type", "merge"}, {"inputs", J::array{old, ref(r)}}, {"output", ref(k)},
          {"origins", ids(k.origins)}, {"precision", k.precision}});
}
void contribute(RTLIL::Cell *target, RTLIL::Cell *donor, const std::string &op) {
    if (target == donor || !cells.count(target) || !cells.count(donor) || !tracks(target->module)) return;
    observe(target, "before_contribution"); observe(donor, "before_contribution");
    auto &t = cells.at(target); auto &d = cells.at(donor);
    auto old = ref(t), input = ref(d);
    t.origins.insert(d.origins.begin(), d.origins.end());
    t.precision = merged_precision(t.precision, d.precision); ++t.version;
    emit({{"record_type", "cell_contribution"}, {"inputs", J::array{old, input}},
          {"output", ref(t)}, {"origins", ids(t.origins)}, {"precision", t.precision}, {"operation", op}});
    for (auto &r : active_inputs) if (r == old) {
        r = ref(t); active.insert(d.origins.begin(), d.origins.end()); precision = merged_precision(precision, d.precision);
    }
}
void assignment(RTLIL::Module *module, const RTLIL::SigSpec &lhs, const RTLIL::SigSpec &rhs, const Origins &origins, const std::string &p, const std::string &cloned_from) {
    if (!tracks(module)) return;
    ConnectionInfo info{module, lhs, rhs, "native:connection_" + std::to_string(++object_id), {}};
    for (int i = 0; i < lhs.size(); ++i) info.offsets.push_back(i);
    info.origins = origins; info.precision = p;
    connections.push_back(info);
    emit({{"record_type", "assignment"}, {"ref", info.ref},
          {"module", module->name.str()}, {"lhs", signal(lhs)}, {"rhs", signal(rhs)},
          {"flatten_event", cloning_source.empty() ? "" : flatten_context},
          {"source_offsets", cloning_offsets},
          {"cloned_from", cloned_from}, {"origins", ids(origins)}, {"precision", p}, {"inputs", active_inputs}, {"operation", operation}, {"abc_invocation", active_region}, {"abc_node", abc_node}, {"abc_boundary", abc_boundary}});
}
void connected(RTLIL::Module *module, const RTLIL::SigSig &conn) {
    if (!tracks(module) || operation.empty() || active_inputs.empty()) return;
    RTLIL::SigSpec lhs, rhs;
    for (int i = 0; i < conn.first.size(); ++i) if (active_outputs.count(conn.first[i])) {
        lhs.append(conn.first[i]); rhs.append(conn.second[i]);
    }
    if (!lhs.empty()) assignment(module, lhs, rhs, active, precision);
}
static void connection_state(ConnectionInfo &info, const std::string &reason) {
    emit({{"record_type", "connection_state"}, {"ref", info.ref}, {"revision", ++info.revision},
          {"lhs", signal(info.lhs)}, {"rhs", signal(info.rhs)}, {"offsets", info.offsets}, {"reason", reason},
          {"module", info.module->name.str()}});
}
void remap_connections(RTLIL::Module *module, const SigMap &map) {
    if (!tracks(module)) return;
    for (auto &info : connections) if (info.module == module && !info.lhs.empty()) {
        auto lhs = map(info.lhs), rhs = map(info.rhs);
        if (lhs != info.lhs || rhs != info.rhs) {
            info.lhs = lhs; info.rhs = rhs;
            connection_state(info, "opt_clean_alias_representative");
        }
    }
}
void wires_removed(RTLIL::Module *module, const pool<RTLIL::Wire*> &wires) {
    if (!tracks(module) || wires.empty()) return;
    for (auto &info : connections) if (info.module == module) {
        RTLIL::SigSpec lhs, rhs;
        std::vector<int> offsets;
        for (int i = 0; i < info.lhs.size(); ++i) {
            if ((info.lhs[i].wire && wires.count(info.lhs[i].wire)) ||
                (info.rhs[i].wire && wires.count(info.rhs[i].wire))) continue;
            lhs.append(info.lhs[i]); rhs.append(info.rhs[i]); offsets.push_back(info.offsets[i]);
        }
        if (lhs.size() != info.lhs.size()) {
            info.lhs = lhs; info.rhs = rhs; info.offsets = offsets;
            connection_state(info, "signal_removed");
        }
    }
}
void parameter_module_cloned(RTLIL::Module *original, RTLIL::Module *copy) {
    if (!tracks(original) || !tracks(copy)) return;
    // Called only after hierarchy adds the actual top-level parameter clone.
    // Names are the correspondence used by RTLIL::Module::cloneInto itself.
    auto copy_bit = [copy](RTLIL::SigBit bit) {
        if (bit.wire) {
            bit.wire = copy->wire(bit.wire->name);
            log_assert(bit.wire != nullptr);
        }
        return bit;
    };
    auto copy_signal = [&copy_bit](const RTLIL::SigSpec &sig) {
        RTLIL::SigSpec result;
        for (auto bit : sig) result.append(copy_bit(bit));
        return result;
    };
    for (auto cell : original->cells()) {
        CellScope scope(cell, "hierarchy_parameter_clone");
        auto target = copy->cell(cell->name);
        log_assert(target != nullptr);
        created(target);
    }
    auto previous_connections = connections;
    for (const auto &info : previous_connections) if (info.module == original && !info.lhs.empty())
        assignment(copy, copy_signal(info.lhs), copy_signal(info.rhs), info.origins, info.precision, info.ref);
    for (auto &it : original->processes) {
        auto source = it.second;
        auto target = copy->processes.at(it.first);
        process_clone(source, target);
        // Inserting the clone can reallocate the outer dict: copy its source
        // metadata before populating the new process entry.
        if (actions.count(source)) {
            auto values = actions.at(source);
            for (const auto &a : values) actions[target][copy_bit(a.first)] = a.second;
        }
        if (temporaries.count(source)) {
            auto values = temporaries.at(source);
            for (const auto &t : values) temporaries[target][copy_bit(t.first)] = copy_bit(t.second);
        }
    }
}
void module_removed(RTLIL::Module *module) {
    if (!tracks(module)) return;
    for (auto cell : module->cells()) removed(cell);
    for (auto &it : module->processes) process_removed(it.second);
    pool<RTLIL::Wire*> wires;
    for (auto wire : module->wires()) wires.insert(wire);
    wires_removed(module, wires);
    for (auto &info : connections) if (info.module == module) {
        if (!info.lhs.empty()) {
            info.lhs = RTLIL::SigSpec(); info.rhs = RTLIL::SigSpec(); info.offsets.clear();
            connection_state(info, "module_removed");
        }
        info.module = nullptr; // Do not inherit metadata if the address is reused.
    }
}
void module_renamed(RTLIL::Module *module) {
    if (!tracks(module)) return;
    for (auto &info : connections) if (info.module == module)
        connection_state(info, "module_renamed");
}
void process_removed(RTLIL::Process *proc) {
    if (tracks(proc->module)) { process_release(proc); actions.erase(proc); temporaries.erase(proc); }
}
static RTLIL::SigBit original(RTLIL::Process *proc, RTLIL::SigBit bit) {
    if (!temporaries.count(proc)) return bit;
    const auto &map = temporaries.at(proc);
    pool<RTLIL::SigBit> seen;
    while (map.count(bit) && seen.insert(bit).second) bit = map.at(bit);
    return bit;
}
void temporary(RTLIL::Process *proc, const RTLIL::SigSpec &from, const RTLIL::SigSpec &to) {
    if (!tracks(proc->module)) return;
    log_assert(from.size() == to.size());
    process_temporary(proc, from, to);
    for (int i = 0; i < from.size(); ++i) temporaries[proc][to[i]] = original(proc, from[i]);
}
void action(RTLIL::Process *proc, const RTLIL::SigSpec &lhs, const Origins &origins) {
    if (!tracks(proc->module)) return;
    for (auto bit : lhs) {
        auto &s = actions[proc][original(proc, bit)];
        s.insert(origins.begin(), origins.end());
    }
    emit({{"record_type", "process_action"}, {"process", proc->name.str()},
          {"module", proc->module->name.str()}, {"lhs", signal(lhs)}, {"origins", ids(origins)}});
}
Origins process_sources(RTLIL::Process *proc, const RTLIL::SigSpec &sig) {
    Origins result;
    if (!tracks(proc->module) || !actions.count(proc)) return result;
    for (auto bit : sig) {
        auto key = original(proc, bit);
        if (actions.at(proc).count(key)) {
            const auto &s = actions.at(proc).at(key);
            result.insert(s.begin(), s.end());
        }
    }
    return result;
}
#include "process_provenance.inc"
#include "hierarchy_provenance.inc"

static void checkpoint(RTLIL::Design *design, bool final) {
    J::array snapshot;
    for (auto module : design->modules()) for (auto cell : module->cells()) {
        if (!cells.count(cell)) { gap("cell_missing_from_lifecycle"); continue; }
        observe(cell, "structural_observation");
        snapshot.push_back(ref(cells.at(cell)));
    }
    J::object record{{"record_type", final ? "run_end" : "checkpoint"}, {"cells", snapshot}};
    J::object connection_versions;
    for (auto &info : connections) connection_versions[info.ref] = info.revision;
    record["connections"] = connection_versions;
    if (final) record["input_files"] = J::array(yosys_input_files.begin(), yosys_input_files.end());
    emit(record);
}
void step_begin(const std::vector<std::string> &args, RTLIL::Design *design) {
    if (!tracks(design) || args[0] == "flow_start" || args[0] == "flow_stop") return;
    int parent = steps.empty() ? 0 : steps.back();
    steps.push_back(++step_id); passes.push_back(args[0]);
    emit({{"record_type", "step_begin"}, {"parent_step_id", parent}, {"pass", args[0]}, {"args", args}});
    static const std::set<std::string> supported = {"read_verilog", "hierarchy", "flatten", "flow_hierarchy", "proc", "proc_clean", "proc_rmdead",
        "proc_prune", "proc_init", "proc_arst", "proc_rom", "proc_mux", "proc_dlatch", "proc_dff", "proc_memwr",
        "opt_merge", "opt_clean", "opt_dff", "techmap", "abc", "write_json", "write_rtlil", "write_verilog", "check", "stat", "synth", "opt"};
    static const std::set<std::string> partial = {"opt_expr", "opt_muxtree", "opt_reduce", "alumacc"};
    if (partial.count(args[0])) gap("partial_pass:" + args[0]);
    else if (!supported.count(args[0])) gap("uncovered_pass:" + args[0]);
}
void step_end(RTLIL::Design *design) {
    if (!tracks(design) || steps.empty()) return;
    process_flush();
    checkpoint(design, false);
    emit({{"record_type", "step_end"}});
    steps.pop_back(); passes.pop_back();
}
struct Start : Pass {
    Start() : Pass("flow_start", "start native synthesis provenance") {}
    void execute(std::vector<std::string> args, RTLIL::Design *design) override {
        if (args.size() != 3 || enabled() || GetSize(design->modules()) != 0)
            log_cmd_error("flow_start FILE RUN_ID requires an empty design and inactive recorder.\n");
        sequence = source_id = object_id = step_id = 0;
        cells.clear(); actions.clear(); temporaries.clear(); gaps.clear(); active.clear(); steps.clear(); passes.clear();
        source_rows.clear(); cloning_source.clear(); flatten_context.clear(); flatten_sources.clear();
        process_reset();
        root_design = design; pending_modules.clear(); connections.clear(); active_inputs.clear(); active_outputs.clear(); operation.clear(); capturing_region = nullptr;
        abc_id = 0; active_region.clear(); abc_node.clear(); abc_boundary = -1; captured_outputs = RTLIL::SigSpec(); cell_scope = nullptr;
        rewrite_filename(args[1]);
        trace_path = args[1];
        std::ifstream existing(args[1]);
        if (existing.good()) log_cmd_error("Refusing to overwrite native provenance output.\n");
        output.open(args[1]);
        if (!output) log_cmd_error("Cannot open provenance output.\n");
        emit({{"record_type", "run_begin"}, {"schema_version", 2}, {"run_id", args[2]}, {"profile", "native-combinational"}, {"features", J::array{"process_action_proofs", "conservative_precision_merge", "abc_node_correspondence", "conservative_derivation", "techmap_output_bits", "combinational_only", "abc_internal_v4"}}});
    }
} Start;
struct Stop : Pass {
    Stop() : Pass("flow_stop", "finish native synthesis provenance") {}
    void execute(std::vector<std::string> args, RTLIL::Design *design) override {
        if (args.size() != 1 || !enabled()) log_cmd_error("flow_stop requires an active recorder.\n");
        process_flush(); checkpoint(design, true); output.close();
        if (!output) log_error("Cannot flush native provenance log.\n");
    }
} Stop;
}
YOSYS_NAMESPACE_END
