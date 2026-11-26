from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch

from yosys_flow.common import TraceError
from yosys_flow.online_evidence import Budget,EvidenceBlocked,bounded_walk
from yosys_flow.graph_index import EdgeList,walk


class OnlineEvidenceTests(unittest.TestCase):
    def test_bounded_traversal_preserves_chronological_semantics(self):
        edges=EdgeList([{'from':'b','to':'c'},{'from':'a','to':'b'}])
        context=SimpleNamespace(trace=SimpleNamespace(edges=edges),evidence_budget=Budget())
        self.assertEqual(bounded_walk(context,['a']),walk(edges,['a'])[0])
        self.assertNotIn('c',bounded_walk(context,['a']))
        context.trace.edges=EdgeList({'from':str(i),'to':str(i+1)} for i in range(18))
        context.evidence_budget=Budget(node_limit=1)
        with self.assertRaises(EvidenceBlocked):bounded_walk(context,['0'])

    def test_external_source_solver_respects_remaining_shared_budget(self):
        from yosys_flow.source_regions import command
        policy=Budget(seconds=0.1)
        def timeout(*args,**kwargs):
            self.assertLessEqual(kwargs['timeout'],0.1)
            raise subprocess.TimeoutExpired(args[0],kwargs['timeout'])
        with patch('yosys_flow.source_regions.subprocess.run',side_effect=timeout):
            with self.assertRaises(EvidenceBlocked) as caught:
                with policy.scope('old_rtl_to_independent_region'):command('yosys','/tmp','sat')
        self.assertEqual(caught.exception.code,'old_region_tool_timeout')
        self.assertGreater(policy.used,0)

    def test_source_association_cannot_authorize_function_class_merge(self):
        from yosys_flow.evidence_relations import install
        for category in ('source_association','rule_checked_transform'):
            with self.assertRaisesRegex(TraceError,'non-functional'):
                install(SimpleNamespace(),[{'category':category,'independent_cut_verified':False}])
