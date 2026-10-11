// Observation-only recorder. Never adds attributes or changes optimization decisions.
#pragma once
#include "kernel/json.h"
#include <fstream>
#include <map>
#include <memory>
#include <set>

namespace provenance {
using namespace Yosys;
using J = Json;

struct Recorder {
    struct Entry { std::string id; int version; J state; std::string kind; };
    std::ofstream stream;
    std::string run_id, step_id;
    int sequence = 0, object_seq = 0, event_seq = 0, step_seq = 0;
    std::map<std::string, Entry> active;
    std::map<std::string, std::string> sources;
    std::vector<std::string> merge_inputs;
    size_t old_connections = 0;

    Recorder(const std::string &path, const std::string &run) : run_id(run) {
        std::ifstream existing(path);
        if (existing.good()) log_cmd_error("Refusing to overwrite trace file %s.\n", path);
        stream.open(path);
        if (!stream) log_cmd_error("Cannot open trace file %s.\n", path);
        emit({{"record_type", "run_begin"}, {"capture", "instrumented"}, {"step_snapshot_ids", "current"}});
    }
    void emit(J::object record) {
        record["schema_version"] = 1;
        record["run_id"] = run_id;
        record["sequence"] = ++sequence;
        stream << J(record).dump() << '\n';
        if (!stream) log_cmd_error("Trace write failed.\n");
    }
    static std::string key(const std::string &kind, Module *m, const std::string &name) {
        return J(J::array{kind, m->name.str(), name}).dump();
    }
    static std::string ref(const Entry &e) { return e.id + "@" + std::to_string(e.version); }
    static J attrs(const dict<IdString, Const> &items) {
        J::object result;
        for (const auto &item : items) result[item.first.str()] = item.second.as_string();
        return result;
    }
    J::array source_refs(const std::string &src, Module *m) {
        J::array refs;
        size_t start = 0;
        while (start < src.size()) {
            size_t end = src.find('|', start);
            std::string span = src.substr(start, end == std::string::npos ? end : end - start);
            std::string k = key("source", m, span);
            if (!sources.count(k)) {
                std::string id = "yosys:source_" + std::to_string(++object_seq);
                sources[k] = id + "@0";
                emit({{"record_type", "object"}, {"object_id", id}, {"version", 0},
                      {"ref", sources[k]}, {"kind", "source_span"}, {"module", m->name.str()},
                      {"name", span}, {"source_ids", J::array{}},
                      {"state", J::object{{"src", span}, {"capture", "src_attribute"},
                                         {"instance", m->name.str()}}}});
            }
            refs.push_back(sources.at(k));
            if (end == std::string::npos) break;
            start = end + 1;
        }
        return refs;
    }
    std::string register_object(const std::string &kind, Module *m, const std::string &name,
                                const J &state, const std::string &src = "", bool revise = false) {
        std::string k = key(kind, m, name);
        auto it = active.find(k);
        if (it != active.end() && !revise) {
            if (it->second.state != state) log_cmd_error("Unrecorded change to %s.\n", name);
            return ref(it->second);
        }
        Entry e = it == active.end() ? Entry{"yosys:object_" + std::to_string(++object_seq), 0, state, kind}
                                     : Entry{it->second.id, it->second.version + 1, state, kind};
        active[k] = e;
        J::array srcs = source_refs(src, m);
        emit({{"record_type", "object"}, {"object_id", e.id}, {"version", e.version},
              {"ref", ref(e)}, {"kind", kind}, {"module", m->name.str()}, {"name", name},
              {"state", state}, {"source_ids", srcs}});
        return ref(e);
    }
    J signal(Module *m, const SigSpec &sig) const {
        J::array bits;
        for (SigBit b : sig) {
            if (b.wire) {
                std::string n = J(J::array{b.wire->name.str(), b.offset}).dump();
                auto it = active.find(key("signal_bit", m, n));
                if (it == active.end()) log_cmd_error("Unrecorded signal bit %s.\n", n);
                bits.push_back(ref(it->second));
            } else bits.push_back(J::object{{"constant", Const(b.data).as_string()}});
        }
        return bits;
    }
    J cell_state(Module *m, Cell *c) const {
        J::object ports, directions;
        for (const auto &p : c->connections()) {
            ports[p.first.str()] = signal(m, p.second);
            directions[p.first.str()] = c->input(p.first) && c->output(p.first) ? "inout" :
                                        c->output(p.first) ? "output" : "input";
        }
        return J::object{{"type", c->type.str()}, {"parameters", attrs(c->parameters)},
                         {"attributes", attrs(c->attributes)}, {"ports", ports},
                         {"port_directions", directions}};
    }
    static J wire_state(Wire *w) {
        return J::object{{"width", w->width}, {"start_offset", w->start_offset}, {"upto", w->upto},
                         {"port_id", w->port_id}, {"port_input", w->port_input},
                         {"port_output", w->port_output}, {"attributes", attrs(w->attributes)}};
    }
    J connection_state(Module *m, const SigSig &conn) const {
        return J::object{{"lhs", signal(m, conn.first)}, {"rhs", signal(m, conn.second)}};
    }
    void initialize(Design *design) {
        for (Module *m : design->modules()) {
            register_object("module", m, m->name.str(), J::object{{"attributes", attrs(m->attributes)}});
            for (Wire *w : m->wires()) {
                register_object("wire", m, w->name.str(), wire_state(w), w->get_src_attribute());
                for (int i = 0; i < w->width; ++i)
                    register_object("signal_bit", m, J(J::array{w->name.str(), i}).dump(),
                                    J::object{{"wire", w->name.str()}, {"offset", i}});
            }
            for (Cell *c : m->cells())
                register_object("cell", m, c->name.str(), cell_state(m, c), c->get_src_attribute());
            for (size_t i = 0; i < m->connections().size(); ++i)
                register_object("connection", m, std::to_string(i), connection_state(m, m->connections()[i]));
        }
        snapshot(design, "initial");
    }
    void snapshot(Design *design, const std::string &phase) {
        // Re-read actual RTLIL rather than trusting recorded events.
        std::map<std::string, J> actual;
        for (Module *m : design->modules()) {
            if (!m->processes.empty()) log_cmd_error("Processes are outside trace coverage.\n");
            actual[key("module", m, m->name.str())] = J::object{{"attributes", attrs(m->attributes)}};
            for (Wire *w : m->wires()) {
                actual[key("wire", m, w->name.str())] = wire_state(w);
                for (int i = 0; i < w->width; ++i)
                    actual[key("signal_bit", m, J(J::array{w->name.str(), i}).dump())] =
                        J::object{{"wire", w->name.str()}, {"offset", i}};
            }
            for (Cell *c : m->cells()) actual[key("cell", m, c->name.str())] = cell_state(m, c);
            for (size_t i = 0; i < m->connections().size(); ++i)
                actual[key("connection", m, std::to_string(i))] = connection_state(m, m->connections()[i]);
        }
        if (actual.size() != active.size()) log_cmd_error("Unrecorded object creation or deletion.\n");
        J::array objects;
        for (auto &p : actual) {
            if (!active.count(p.first) || active.at(p.first).state != p.second)
                log_cmd_error("Unrecorded RTLIL change at %s.\n", p.first);
            objects.push_back(J::object{{"ref", ref(active.at(p.first))}, {"state", p.second}});
        }
        emit({{"record_type", "snapshot"}, {"step_id", step_id}, {"phase", phase}, {"objects", objects}});
    }
    void begin_step(Design *design, const std::vector<std::string> &args) {
        step_id = "step_" + std::to_string(++step_seq);
        snapshot(design, "before");
        J::array command;
        for (auto &a : args) command.push_back(a);
        emit({{"record_type", "step_begin"}, {"step_id", step_id}, {"command", command},
              {"pass", "opt_merge"}, {"coverage", "instrumented"}});
    }
    void before_merge(Module *m, Cell *remove, Cell *keep) {
        merge_inputs = {ref(active.at(key("cell", m, remove->name.str()))),
                        ref(active.at(key("cell", m, keep->name.str())))};
        old_connections = m->connections().size();
    }
    void after_merge(Module *m, Cell *remove, Cell *keep) {
        std::string kept = register_object("cell", m, keep->name.str(), cell_state(m, keep), "", true);
        J::array outputs{kept}, changes;
        for (size_t i = old_connections; i < m->connections().size(); ++i) {
            std::string conn = register_object("connection", m, std::to_string(i), connection_state(m, m->connections()[i]));
            outputs.push_back(conn);
            changes.push_back(J::object{{"connection", conn}, {"operation", "add_alias"},
                {"before", J::object{{"driver", merge_inputs[0]}}},
                {"after", J::object{{"driver", kept}}}});
        }
        active.erase(key("cell", m, remove->name.str()));
        emit({{"record_type", "transform"}, {"event_id", "event_" + std::to_string(++event_seq)},
              {"step_id", step_id}, {"operation", "merge"}, {"capture", "instrumented"},
              {"granularity", "cell"}, {"inputs", J::array{merge_inputs[0], merge_inputs[1]}},
              {"retired", J::array{merge_inputs[0], merge_inputs[1]}}, {"outputs", outputs},
              {"connection_changes", changes}, {"dependencies", J::array{}},
              {"reason", "identical cell type, parameters and canonicalized inputs"}});
    }
    void end_step(Design *design) {
        snapshot(design, "after");
        emit({{"record_type", "step_end"}, {"step_id", step_id}, {"coverage", "instrumented"}});
    }
    void finish(Design *design) {
        snapshot(design, "final");
        emit({{"record_type", "run_end"}, {"status", "complete"}, {"coverage", "partial"}});
        stream.flush();
        if (!stream) log_cmd_error("Cannot flush trace.\n");
    }
};
static std::unique_ptr<Recorder> recorder;

struct InputsPass : Pass {
    InputsPass() : Pass("trace_inputs", "export actual frontend input dependencies as JSON") {}
    void help() override { log("trace_inputs <json-path>\n"); }
    void execute(std::vector<std::string> args, Design *) override {
        if (args.size() != 2) log_cmd_error("Usage: trace_inputs <json-path>\n");
        rewrite_filename(args[1]);
        std::ifstream existing(args[1]);
        if (existing.good()) log_cmd_error("Refusing to overwrite %s.\n", args[1]);
        std::ofstream file(args[1]);
        J::array inputs;
        for (const auto &path : yosys_input_files) inputs.push_back(path);
        file << J(inputs).dump() << '\n';
        file.flush();
        if (!file) log_cmd_error("Cannot write input dependencies.\n");
    }
} inputs_pass;

struct BeginPass : Pass {
    BeginPass() : Pass("trace_begin", "start a JSONL provenance recording") {}
    void help() override { log("trace_begin <jsonl-path> <run-id>\nSupports one elaborated combinational module.\n"); }
    void execute(std::vector<std::string> args, Design *design) override {
        if (args.size() != 3 || recorder) log_cmd_error("Usage: trace_begin <jsonl-path> <run-id>; only one active recorder.\n");
        if (GetSize(design->modules()) != 1) log_cmd_error("Phase 1 requires exactly one elaborated module; hierarchy is not instrumented.\n");
        for (Module *m : design->modules()) {
            if (!m->processes.empty() || !m->memories.empty()) log_cmd_error("Processes and memories are not covered in phase 1.\n");
            for (Cell *c : m->cells())
                if (!c->known() || c->is_builtin_ff() || c->type.str().find("$mem") == 0)
                    log_cmd_error("Cell %s (%s) is outside combinational trace coverage.\n", c->name, c->type);
        }
        rewrite_filename(args[1]);
        recorder = std::make_unique<Recorder>(args[1], args[2]);
        recorder->initialize(design);
    }
} begin_pass;
struct EndPass : Pass {
    EndPass() : Pass("trace_end", "finish provenance recording and check actual RTLIL") {}
    void help() override { log("trace_end\n"); }
    void execute(std::vector<std::string> args, Design *design) override {
        if (args.size() != 1 || !recorder) log_cmd_error("trace_end requires an active recorder and no arguments.\n");
        recorder->finish(design);
        recorder.reset();
    }
} end_pass;
} // namespace provenance
