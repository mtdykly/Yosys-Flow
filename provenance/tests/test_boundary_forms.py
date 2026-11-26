import unittest

from yosys_flow.abc_partition import Graph, Partitioner
from yosys_flow.boundary_forms import install


class GateFormTests(unittest.TestCase):
    def test_sop_and_mapped_gates_compose_with_polarity_and_aliases(self):
        first=Graph('.model t\n.inputs a b c\n.outputs y\n'
            '.names a b p\n00 1\n10 1\n01 1\n'
            '.names p c y\n00 1\n11 1\n.end\n')
        final=Graph('.model t\n.inputs a b c\n.outputs y\n'
            '.gate NAND A=a B=b Y=n\n.names n buf\n1 1\n'
            '.gate XOR A=buf B=c Y=z\n.gate NOT A=z Y=y\n.end\n')
        engine=Partitioner(first,final)
        summary=install(engine)
        self.assertGreater(summary['unions'],0)
        self.assertEqual(engine.classes.find(engine.a['y']),engine.classes.find(engine.b['y']))
        self.assertEqual(engine.classes.find(engine.a['y']),engine.classes.find(engine.b['z'])^1)

    def test_independent_truth_not_names_proves_constants_and_rejects_changes(self):
        first=Graph('.model t\n.inputs a b\n.outputs y\n.names a a y\n10 1\n.end\n')
        final=Graph('.model t\n.inputs a b\n.outputs y\n.gate AND A=a B=b Y=y\n.end\n')
        engine=Partitioner(first,final);install(engine)
        self.assertEqual(engine.classes.find(engine.a['y']),engine.classes.find(0))
        self.assertNotEqual(engine.classes.find(engine.a['y']),engine.classes.find(engine.b['y']))

    def test_wide_unknown_forms_remain_scoped(self):
        text='.model t\n.inputs a b c d e f g\n.outputs y\n.names a b c d e f g y\n1111111 1\n.end\n'
        # The independent gate-form mechanism cannot trust opaque gates by name.
        from yosys_flow.boundary_forms import Forms
        forms=Forms();first=forms.graph(Graph(text),'first');last=forms.graph(Graph(text),'last')
        self.assertNotEqual(first['y'],last['y'])
        self.assertEqual(forms.skipped,2)
