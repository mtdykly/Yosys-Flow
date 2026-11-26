from types import SimpleNamespace
import unittest
from yosys_flow.common import TraceError
from yosys_flow.partition_targets import resolve


class PartitionTargetTests(unittest.TestCase):
    def trace(self):
        return SimpleNamespace(regions=[{'module':'\\top','invocation_id':'abc1',
            'signals':[{'wire':'\\n214713','offset':0}], 'input_nodes':[{'id':0,'cell':'new_id@3'}]}],
            objects={'new_id@3':{'state':{'module':'\\top','type':'$_AND_',
                'connections':{'\\Y':[{'wire':'\\n214713','offset':0}]},'port_directions':{'\\Y':'output'}}}})

    def test_changed_ids_are_resolved_from_actual_binding(self):
        self.assertEqual(resolve(self.trace(),['n214713'],'top','$_AND_'),['new_id@3'])

    def test_missing_ambiguous_and_changed_types_are_rejected(self):
        with self.assertRaises(TraceError): resolve(self.trace(),['absent'],'top')
        with self.assertRaises(TraceError): resolve(self.trace(),['n214713'],'top','$_OR_')
        trace=self.trace();trace.regions[0]['input_nodes'].append({'id':0,'cell':'second@1'})
        trace.objects['second@1']=trace.objects['new_id@3']
        with self.assertRaises(TraceError): resolve(trace,['n214713'],'top')
