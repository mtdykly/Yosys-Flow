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
    std::string id;
    Origins origins;
    std::vector<std::string> inputs;
    RTLIL::SigSpec outputs;
};
struct RegionCapture {
    Region *previous;
    RegionCapture(Region &region);
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
                const std::string &tempdir, const std::string &executable, const std::string &script);
    ~RegionScope();
};
void created(RTLIL::Cell *cell);
void removed(RTLIL::Cell *cell);
void merge(RTLIL::Cell *keep, RTLIL::Cell *remove);
void assignment(RTLIL::Module *module, const RTLIL::SigSpec &lhs, const RTLIL::SigSpec &rhs, const Origins &ids,
                const std::string &precision = "bit_range");
void connected(RTLIL::Module *module, const RTLIL::SigSig &conn);
void remap_connections(RTLIL::Module *module, const SigMap &map);
void wires_removed(RTLIL::Module *module, const pool<RTLIL::Wire*> &wires);
void process_removed(RTLIL::Process *proc);
void clock_sources(RTLIL::Process *proc, const Origins &ids);
void initialization(RTLIL::Process *proc, const RTLIL::SigSpec &lhs, const RTLIL::SigSpec &value);
void action(RTLIL::Process *proc, const RTLIL::SigSpec &lhs, const Origins &ids);
void temporary(RTLIL::Process *proc, const RTLIL::SigSpec &original, const RTLIL::SigSpec &generated);
Origins process_sources(RTLIL::Process *proc, const RTLIL::SigSpec &signal);
Origins state_sources(RTLIL::Process *proc, const RTLIL::SigSpec &signal);
void step_begin(const std::vector<std::string> &args, RTLIL::Design *design);
void step_end(RTLIL::Design *design);
}
YOSYS_NAMESPACE_END
