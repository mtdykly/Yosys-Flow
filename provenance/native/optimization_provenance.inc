// Actual optimization decisions; included inside FlowProvenance. No network
// snapshots, optimization constraints, or global fan-in source unions.
static int rewrite_depth = 0;
static std::map<std::string, std::pair<SharedOrigins, std::string>> rewrite_inputs;
std::string rewrite_input(RTLIL::Cell *cell)
{
    if (!cells.count(cell)) return "";
    work("inversion_witness");
    observe(cell, "before_instrumented_rewrite");
    auto &info = cells.at(cell);
    auto reference = ref(info);
    rewrite_inputs.emplace(reference, std::make_pair(info.origins, info.precision));
    return reference;
}

std::vector<std::string> constant_premises(RTLIL::Cell *cell, const SigMap &map)
{
    ProfileTimer timer(PremiseQuery);
    work("premise_queries");
    std::set<std::string> result;
    if (!tracks(cell->module) || !connection_evidence.count(cell->module)) return {};
    auto &evidence = connection_evidence.at(cell->module);
    for (const auto &port : cell->connections()) if (cell->input(port.first))
        for (auto bit : port.second) if (bit.wire && !map(bit).wire) {
            ConnectionEvidence::Key key{bit.wire, bit.offset};
            if (audit_scans) evidence.constant_cache.clear();
            auto cached = evidence.constant_cache.find(key);
            if (cached == evidence.constant_cache.end()) {
                work("premise_cache_miss");
                std::set<ConnectionEvidence::Key> seen;
                std::vector<int> chain;
                auto current = bit;
                while (current.wire && evidence.count(current) && seen.insert({current.wire, current.offset}).second) {
                    work("premise_chain_link");
                    auto link = evidence.at(current);
                    chain.push_back(link.first); current = link.second;
                }
                if (current.wire) continue; // Missing edge or cycle, never a proof.
                if (evidence.constant_cache.size() >= 4096) evidence.constant_cache.clear();
                cached = evidence.constant_cache.emplace(key, std::make_pair(current, std::move(chain))).first;
            } else work("premise_cache_hit");
            if (cached->second.first == map(bit))
                for (int index : cached->second.second) result.insert(connections.at(index).ref);
        }
    return {result.begin(), result.end()};
}

struct RewriteScope::Impl {
    std::unique_ptr<CellScope> scope;
    std::vector<std::string> inputs;
    std::map<int, std::pair<std::string, J>> before;
    std::vector<std::pair<RTLIL::Cell*, int>> targets;
    SharedOrigins origins;
    std::string rule, precision;
    int connection_start, start_seq;
    J::object premises;
};

RewriteScope::RewriteScope(const std::vector<RTLIL::Cell*> &inputs, const std::string &rule,
                          const std::vector<std::string> &premises)
{
    if (!enabled() || rewrite_depth) return; // Outer decision owns recursive work.
    std::vector<RTLIL::Cell*> tracked;
    pool<RTLIL::Cell*> seen;
    for (auto cell : inputs) if (cell && cells.count(cell) && seen.insert(cell).second) tracked.push_back(cell);
    if (tracked.empty()) return;
    ++rewrite_depth;
    work("rewrite_begin/" + rule);
    impl = std::make_unique<Impl>();
    impl->rule = rule;
    // Reuse only a live outer scope whose typed RTLIL state still matches.
    // A recorder version alone is NOT a mutation clock for arbitrary passes.
    bool reuse = cell_scope && cell_scope->targets.size() == tracked.size();
    for (size_t i = 0; reuse && i < tracked.size(); ++i)
        reuse = cell_scope->targets[i].first == tracked[i] &&
                cell_scope->targets[i].second == cells.at(tracked[i]).id &&
                unchanged(tracked[i], cells.at(tracked[i]));
    impl->scope = std::make_unique<CellScope>(tracked, rule, reuse);
    impl->targets = impl->scope->targets;
    for (auto [cell, id] : impl->targets) {
        auto &info = cells.at(cell);
        impl->before[id] = {ref(info), info.state};
    }
    for (const auto &r : premises) if (rewrite_inputs.count(r)) {
        auto &info = rewrite_inputs.at(r);
        active.unite(info.first);
        if (std::find(active_inputs.begin(), active_inputs.end(), r) == active_inputs.end()) active_inputs.push_back(r);
        precision = merged_precision(precision, info.second);
        impl->premises[r] = -1; // immutable cell version, possibly retired
    } else if (connection_slots.count(r)) {
        auto &info = connections.at(connection_slots.at(r));
        active.unite(info.origins);
        if (std::find(active_inputs.begin(), active_inputs.end(), r) == active_inputs.end()) active_inputs.push_back(r);
        impl->premises[r] = info.revision;
        // Connections with incomplete process evidence remain conservative.
        if (info.precision != "bit_range") precision = merged_precision(precision, info.precision);
    }
    active_input_cached = false;
    impl->inputs = active_inputs;
    impl->origins = active;
    impl->precision = precision;
    impl->connection_start = GetSize(connections);
    impl->start_seq = sequence;
}

RewriteScope::~RewriteScope()
{
    if (!impl) return;
    auto generated = std::move(impl->scope->generated);
    impl->scope.reset(); // Flush real post-rewrite states, then restore context.
    J::array bindings, outputs, preserved, removed, ports, assignments;
    for (auto [cell, id] : impl->targets) {
        auto &before = impl->before.at(id);
        if (!cells.count(cell) || cells.at(cell).id != id) { removed.push_back(before.first); continue; }
        auto &info = cells.at(cell);
        if (info.state == before.second) { preserved.push_back(before.first); continue; }
        auto old = ref(info);
        ++info.version;
        info.origins = impl->origins; info.precision = impl->precision;
        bindings.push_back(J::object{{"input", old}, {"ref", ref(info)}});
        outputs.push_back(ref(info));
        // Raw port values preserve widths, offsets, constants and polarity;
        // replacement output connections below explain removed/padded bits.
        ports.push_back(J::object{{"before", before.first}, {"after", ref(info)},
            {"old_ports", before.second["connections"]}, {"new_ports", info.state["connections"]},
            {"old_parameters", before.second["parameters"]}, {"new_parameters", info.state["parameters"]}});
    }
    for (auto [cell, id] : generated) if (cells.count(cell) && cells.at(cell).id == id) {
        observe(cell, "instrumented_rewrite");
        outputs.push_back(ref(cells.at(cell)));
    }
    for (int i = impl->connection_start; i < GetSize(connections); ++i)
        assignments.push_back(connections[i].ref);
    if (!outputs.empty() || !removed.empty() || !assignments.empty()) {
        work("rewrite_emitted/" + impl->rule);
        emit({{"record_type", "optimization_rewrite"}, {"rule", impl->rule}, {"start_seq", impl->start_seq},
              {"inputs", impl->inputs}, {"premises", impl->premises}, {"bindings", bindings},
              {"outputs", outputs}, {"preserved", preserved}, {"removed", removed},
              {"connections", assignments}, {"port_changes", ports},
              {"origins", ids(impl->origins)}, {"precision", impl->precision}});
    } else work("rewrite_noop/" + impl->rule);
    --rewrite_depth;
}

static DeferredRewrite *pending_rewrite = nullptr;
DeferredRewrite::DeferredRewrite(RTLIL::Cell *c, const char *r, const SigMap *m) :
    cell(c), rule(r), map(m), previous(pending_rewrite)
{
    pending_rewrite = this;
    if (profile_capture) work(std::string("deferred_visit/") + rule);
    if (audit_scans) rewrite_touch(cell); // Diagnostic eager reference path.
}
DeferredRewrite::~DeferredRewrite()
{
    scope.reset(); // Commit before restoring the parent, even on early return.
    pending_rewrite = previous;
}
void rewrite_touch(RTLIL::Cell *cell)
{
    auto pending = pending_rewrite;
    if (!pending || (cell && cell != pending->cell) || pending->scope || rewrite_depth || !enabled()) return;
    pending->scope = std::make_unique<RewriteScope>(std::vector<RTLIL::Cell*>{pending->cell}, pending->rule,
        pending->map ? constant_premises(pending->cell, *pending->map) : std::vector<std::string>{});
}
