// Included by ast.cc, inside the Yosys namespace.
static FlowProvenance::Origins flow_subtree(AST::AstNode *node)
{
    auto result = node->flow_origins;
    for (auto &child : node->children) {
        auto origins = flow_subtree(child.get());
        result.insert(origins.begin(), origins.end());
    }
    return result;
}

static void flow_register_ast(AST::AstNode *node, std::string module = "",
                              FlowProvenance::Origins controls = {})
{
    using namespace AST;
    if (!FlowProvenance::enabled()) return;
    if (node->type == AST_MODULE) module = node->str;
    // Reject source-level state even if later optimization would remove it.
    if (node->type == AST_POSEDGE || node->type == AST_NEGEDGE || node->type == AST_INITIAL || node->attributes.count(ID::init))
        log_error("Combinational-only flow rejects clocked processes and initialization (%s).\n", type2str(node->type));
    // These forms still need elaboration/instance hooks.
    switch (node->type) {
    case AST_CELL: case AST_CELLARRAY: case AST_PRIMITIVE: case AST_MEMORY:
    case AST_FUNCTION: case AST_TASK: case AST_FCALL: case AST_TCALL:
    case AST_FOR: case AST_WHILE: case AST_REPEAT: case AST_GENFOR:
    case AST_GENIF: case AST_GENCASE: case AST_GENBLOCK:
        log_error("Combinational provenance does not support %s.\n", type2str(node->type));
    default: break;
    }
    bool statement = node->type == AST_ASSIGN || node->type == AST_ASSIGN_EQ || node->type == AST_ASSIGN_LE;
    bool expression = (node->type >= AST_TO_BITS && node->type <= AST_TERNARY) ||
                      node->type == AST_IDENTIFIER || node->type == AST_CONSTANT;
    if ((statement || expression) && node->location.begin.line > 0 && node->flow_origins.empty()) {
        int id = FlowProvenance::source(module, *node->location.begin.filename,
            node->location.begin.line, node->location.begin.column,
            node->location.end.line, node->location.end.column, type2str(node->type));
        node->flow_origins.insert(id);
    }
    node->flow_controls = controls;
    for (size_t i = 0; i < node->children.size(); ++i) {
        flow_register_ast(node->children[i].get(), module, controls);
        if (node->type == AST_CASE && i == 0) {
            auto condition = flow_subtree(node->children[i].get());
            controls.insert(condition.begin(), condition.end());
        }
    }
}
