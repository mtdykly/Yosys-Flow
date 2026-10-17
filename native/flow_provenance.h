// Run-local metadata only: never adds RTLIL attributes or optimization constraints.
#pragma once
#include "kernel/rtlil.h"
#include "kernel/sigtools.h"
#include <set>

YOSYS_NAMESPACE_BEGIN
namespace FlowProvenance {
using Origins = std::set<int>;
bool enabled();
bool tracks(RTLIL::Design *design);
struct ModuleScope {
    RTLIL::Module *module;
    ModuleScope(RTLIL::Design *design, RTLIL::Module *module);
    ~ModuleScope();
};
int source(const std::string &module, const std::string &file, int line, int column,
           int end_line, int end_column, const std::string &kind);
void gap(const std::string &reason);
struct Scope {
    Origins previous;
    std::string previous_precision;
    Scope(const Origins &ids, const std::string &precision = "cell", bool inherit = true);
    ~Scope();
};
// Inputs are captured at the actual rewrite site, never inferred from a diff.
struct CellScope {
    CellScope *previous_scope;
    std::vector<std::pair<RTLIL::Cell*, int>> generated;
    std::map<RTLIL::SigBit, std::vector<std::pair<std::string, int>>> output_positions;
    dict<std::string, RTLIL::SigSpec> original_outputs;
    std::string mapping_input;
    std::vector<std::pair<RTLIL::Cell*, int>> targets;
    Scope scope;
    std::vector<std::string> previous_inputs;
    std::string previous_operation;
    pool<RTLIL::SigBit> previous_outputs;
    CellScope(RTLIL::Cell *cell, const std::string &operation);
    CellScope(const std::vector<RTLIL::Cell*> &cells, const std::string &operation);
    ~CellScope();
};
struct Region {
    dict<RTLIL::SigBit, std::string> node_cells;
    std::string id;
    Origins origins;
    std::vector<std::string> inputs;
    RTLIL::SigSpec outputs;
};
struct RegionCapture {
    Region *previous;
    RTLIL::SigSpec previous_outputs;
    RegionCapture(Region &region, const RTLIL::SigSpec &outputs);
    ~RegionCapture();
};
struct RegionScope {
    Scope scope;
    std::vector<std::string> previous_inputs;
    std::string previous_operation;
    pool<RTLIL::SigBit> previous_outputs;
    std::string previous_region;
    RegionScope(const Region &region, RTLIL::Module *module, const RTLIL::SigSpec &signals,
                const std::vector<int> &input_ports, const std::vector<int> &output_ports,
                const std::string &tempdir, const std::string &executable, const std::string &script,
                const std::vector<std::vector<int>> &fanins);
    ~RegionScope();
};
struct AbcImportScope {
    std::string previous;
    int previous_boundary;
    AbcImportScope(RTLIL::Cell *mapped, const dict<RTLIL::IdString, RTLIL::SigSpec> &target_ports);
    AbcImportScope(int boundary);
    ~AbcImportScope();
};
void created(RTLIL::Cell *cell);
void removed(RTLIL::Cell *cell);
void merge(RTLIL::Cell *keep, RTLIL::Cell *remove);
void contribute(RTLIL::Cell *target, RTLIL::Cell *donor, const std::string &operation);
void assignment(RTLIL::Module *module, const RTLIL::SigSpec &lhs, const RTLIL::SigSpec &rhs, const Origins &ids,
                const std::string &precision = "bit_range", const std::string &cloned_from = "");
void connected(RTLIL::Module *module, const RTLIL::SigSig &conn);
void remap_connections(RTLIL::Module *module, const SigMap &map);
void wires_removed(RTLIL::Module *module, const pool<RTLIL::Wire*> &wires);
void parameter_module_cloned(RTLIL::Module *original, RTLIL::Module *copy);
void module_removed(RTLIL::Module *module);
void module_renamed(RTLIL::Module *module);
void process_removed(RTLIL::Process *proc);
void action(RTLIL::Process *proc, const RTLIL::SigSpec &lhs, const Origins &ids);
void temporary(RTLIL::Process *proc, const RTLIL::SigSpec &original, const RTLIL::SigSpec &generated);
Origins process_sources(RTLIL::Process *proc, const RTLIL::SigSpec &signal);
// Semantic process values travel alongside the actual lowering algorithm.
using ProcessRefs = std::set<std::string>;
using ProcessValue = std::vector<ProcessRefs>;
struct ProcessScope {
    Scope scope;
    ProcessRefs previous_refs;
    std::string previous_operation, previous_precision;
    ProcessScope(const ProcessRefs &refs, const std::string &operation,
                 const std::string &precision = "process_action");
    ~ProcessScope();
};
ProcessRefs process_union(const ProcessValue &value);
ProcessRefs process_union(ProcessRefs a, const ProcessRefs &b);
ProcessValue process_values(RTLIL::Process *proc, const RTLIL::SigSpec &sig);
ProcessValue process_action(RTLIL::Process *proc, RTLIL::CaseRule *cs,
    const RTLIL::SigSpec &lhs, const RTLIL::SigSpec &rhs, const RTLIL::SigSpec &target,
    const Origins &statement, const Origins &data, const Origins &control, const Origins &target_sources);
void process_route(RTLIL::Process *proc, const void *actions, const RTLIL::SigSpec &lhs,
                   const RTLIL::SigSpec &rhs, const ProcessValue &value = {});
void process_branch(RTLIL::CaseRule *cs, const Origins &control);
ProcessRefs process_branch(RTLIL::CaseRule *cs);
void process_consume(RTLIL::Module *module, RTLIL::CaseRule *cs, const RTLIL::SigSig &action,
                     const RTLIL::SigSpec &sig, ProcessValue &value);
void process_discard(RTLIL::CaseRule *cs, const RTLIL::SigSpec &lhs,
                     const pool<RTLIL::SigBit> &bits, const std::string &reason);
void process_prune(RTLIL::Module *module, RTLIL::CaseRule *cs, const RTLIL::SigSig &action,
                   const SigMap &map, const RTLIL::SigSpec &kept, const RTLIL::SigSpec &promoted);
void process_case_move(RTLIL::CaseRule *from, RTLIL::CaseRule *to);
void process_case_removed(RTLIL::CaseRule *cs);
void process_dead_case(RTLIL::CaseRule *cs, const std::string &reason = "proc_rmdead_unreachable");
void process_output(RTLIL::Process *proc, const RTLIL::SigSpec &sig, const ProcessValue &value);
ProcessValue process_join(const ProcessValue &a, const ProcessValue &b, const ProcessRefs &control = {});
void process_augment(RTLIL::Cell *cell, const ProcessRefs &refs);
void process_connection(RTLIL::Process *proc, const RTLIL::SigSpec &lhs, const RTLIL::SigSpec &rhs);
void process_clone(RTLIL::Process *from, RTLIL::Process *to);
void process_flush();
void process_reset();
void step_begin(const std::vector<std::string> &args, RTLIL::Design *design);
void step_end(RTLIL::Design *design);
}
YOSYS_NAMESPACE_END
