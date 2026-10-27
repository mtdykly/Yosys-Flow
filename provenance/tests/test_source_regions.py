"""Source witnesses must cover all outputs and fail closed on missing proof."""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yosys_flow import source_regions
from yosys_flow.common import TraceError
from yosys_flow.region_functions import verify_outputs
from incremental.replace import check_network


def gate(kind,y):
    return {'type':kind,'parameters':{},'port_directions':{'A':'input','B':'input','Y':'output'},
            'connections':{'A':[2],'B':[3],'Y':[y]},'attributes':{}}


@unittest.skipUnless(shutil.which('yosys'),'pinned Yosys required')
class SourceRegionTests(unittest.TestCase):
    def setUp(self):
        self.cells={'a':gate('$_AND_',4),'b':gate('$_XOR_',5)}
        self.aliases={('top',n,0):b for n,b in [('a',2),('b',3),('y',4),('z',5)]}
        self.context=SimpleNamespace(directory=Path('/unused'),manifest={'tool':{'stock_executable':shutil.which('yosys')}},
            baseline={'artifacts':{'frontend.il':{'sha256':'fixture'}}})
        self.document={'boundary':[{'port':str(b),'direction':'input' if b<4 else 'output','original_bit':b}
                                   for b in range(2,6)],
                       'region':{'connections':[],'recorded_cell_functions':deepcopy(self.cells)}}

    def prove(self,document=None):
        with patch.object(source_regions,'archived',return_value=(self.cells,self.aliases,{2,3})), \
             patch.object(source_regions.Endpoints,'bit',side_effect=lambda p,w,o:self.aliases.get((p,w,o))):
            return source_regions.prove(self.context,document or self.document)

    def test_both_outputs_proven_and_side_output_counterexample_rejected(self):
        result=self.prove()
        self.assertEqual(result['status'],'verified',result)
        self.assertEqual(result['output_count'],2)
        self.document['region']['recorded_cell_functions']['b']['type']='$_OR_'
        result=self.prove()
        self.assertEqual(result['status'],'not_proven',result)
        self.assertFalse(result['all_outputs'])

    def test_missing_output_anchor_cannot_be_silently_dropped(self):
        del self.aliases['top','z',0]
        result=self.prove()
        self.assertEqual(result['status'],'not_proven')
        self.assertIn('output identity unavailable',result['reason'])

    def test_missing_input_identity_requests_expansion(self):
        del self.aliases['top','a',0]
        result=self.prove()
        self.assertEqual(result['status'],'not_proven')
        self.assertEqual(result['missing_input_bits'],[2])

    def test_solver_timeout_is_not_a_certificate(self):
        with patch.object(source_regions,'command',side_effect=subprocess.TimeoutExpired('yosys',60)):
            result=self.prove()
        self.assertEqual(result['status'],'not_proven')
        self.assertFalse(result['all_outputs'])

    def test_matching_x_outputs_are_not_binary_equivalence(self):
        self.cells['b']['connections']['A']=['x']
        self.document['region']['recorded_cell_functions']=deepcopy(self.cells)
        result=self.prove()
        self.assertEqual(result['status'],'not_proven')

    def test_parent_fallback_proves_unselected_outputs_and_rejects_a_changed_one(self):
        from incremental import automatic, rtlil
        text='''module \\child
  wire input 1 \\a
  wire output 2 \\y
  cell $not \\inv
    parameter \\A_SIGNED 0
    parameter \\A_WIDTH 1
    parameter \\Y_WIDTH 1
    connect \\A \\a
    connect \\Y \\y
  end
end
module \\top
  wire input 1 \\__observed_0
  wire input 2 \\b
  wire output 3 \\y
  wire output 4 \\z
  cell \\child \\u
    connect \\a \\__observed_0
    connect \\y \\y
  end
  connect \\z \\__observed_0
end
'''
        old=rtlil.parse(text)
        new=rtlil.parse(text.replace('cell $not','cell $pos'))
        proof=automatic.prove_unselected_outputs(self.context,old,new,old['top'],new['top'],[('z',0)])
        self.assertEqual(proof['outputs'],[['z',0]])
        bad=rtlil.parse(text.replace('connect \\z \\__observed_0','connect \\z \\b'))
        with self.assertRaisesRegex(TraceError,'did not prove'):
            automatic.prove_unselected_outputs(self.context,old,bad,old['top'],bad['top'],[('z',0)])


class StructuralCutTests(unittest.TestCase):
    def test_large_structural_certificate_rejects_changed_side_output(self):
        cells={str(i):gate('$_AND_',i+4) for i in range(300)}
        boundary=[{'port':'a','direction':'input','original_bit':2},
                  {'port':'b','direction':'input','original_bit':3},
                  {'port':'y','direction':'output','original_bit':4},
                  {'port':'z','direction':'output','original_bit':303}]
        good=verify_outputs(cells,deepcopy(cells),boundary)
        self.assertTrue(good['all_outputs_verified'])
        self.assertEqual(good['outputs'][1]['function']['encoding'],'boolean_dag_v1')
        changed=deepcopy(cells);changed['299']['type']='$_OR_'
        bad=verify_outputs(cells,changed,boundary)
        self.assertFalse(bad['all_outputs_verified'])

    def test_unused_x_alias_allowed_but_active_pin_and_port_rejected(self):
        module={'ports':{'a':{'direction':'input','bits':[2]},'b':{'direction':'input','bits':[3]},
                         'y':{'direction':'output','bits':[4]}},
                'netnames':{'unused_padding':{'bits':['x'],'attributes':{}}},'cells':{'c':gate('$_AND_',4)}}
        check_network(module)
        for changed in ('pin','port'):
            bad=deepcopy(module)
            if changed=='pin':bad['cells']['c']['connections']['A']=['x']
            else:bad['ports']['y']['bits']=['x']
            with self.assertRaisesRegex(TraceError,'fully defined'):check_network(bad)
