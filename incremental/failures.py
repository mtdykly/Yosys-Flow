"""Actionable experiment failure classes, not correctness claims."""
import subprocess


def classify(stage, error):
    text = str(error); lower = text.lower()
    if isinstance(error, subprocess.TimeoutExpired): code = 'tool_timeout'
    elif any(s in lower for s in ('latch','state cell','clocked','clock edge','initialization')): code = 'state_outside_combinational_scope'
    elif 'budget' in lower or 'bdd_' in lower: code = 'proof_budget_exhausted'
    elif 'connection-only' in lower or 'requires selected final gates' in lower: code = 'connection_only_region_unsupported'
    elif 'verified old region' in lower: code = 'source_correspondence_not_proven'
    elif 'ports do not match' in lower or 'binding width' in lower: code = 'new_interface_exceeds_old_boundary'
    elif 'exactly once' in lower: code = 'incomplete_or_duplicate_boundary_binding'
    elif 'cycle' in lower: code = 'combinational_cycle'
    elif 'whole-equivalence failed' in lower: code = 'new_design_equivalence_failed'
    elif any(s in lower for s in ('checksum','hash mismatch','tamper','invalid object','changes function')): code = 'invalid_recorded_evidence'
    else: code = 'stage_failed'
    result = {'code':code,'stage':stage,'detail':text,'proven_safe':False}
    if code == 'new_design_equivalence_failed':
        result['possible_causes'] = ['incorrect_local_specification_or_binding','new_dependency_outside_boundary',
                                    'shared_consumers_require_split']
        result['note'] = 'A failed miter does not by itself identify which cause applies.'
    return result
