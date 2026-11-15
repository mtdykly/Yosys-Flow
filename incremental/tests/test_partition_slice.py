"""Certified complementary anchors must become actual patch connections."""
from types import SimpleNamespace
import unittest

from incremental.statements import Slice
from yosys_flow.boolean import BDD
from yosys_flow.region_functions import Circuit


class PartitionSliceTests(unittest.TestCase):
    def test_complementary_input_and_output_are_both_applied(self):
        cell={'type':'$_XOR_','parameters':{},'attributes':{},'port_directions':{'A':'input','B':'input','Y':'output'},
              'connections':{'A':[2],'B':[3],'Y':[4]}}
        old={**cell,'connections':{'A':[10],'B':[11],'Y':[12]}}
        slicer=Slice(SimpleNamespace(module={'cells':{'old':old}}),{},{},SimpleNamespace(evidence=[]))
        slicer.units={'top':(None,SimpleNamespace(module=SimpleNamespace(name='top')))}
        slicer.coarse={'top':{'cells':{'xor':cell}}};slicer.drivers={('top',4):('top','xor')}
        for new,original,phase in [(2,10,True),(3,11,False)]:
            slicer.by_new['top',new].append(dict(old_bit=original,new_bit=new,old_inverted=phase,
                                               unchanged_full_definition=True))
        slicer.aliases[12].append(dict(instance='top',new_bit=4,old_inverted=True,
                                      new_function_fingerprint='changed',unchanged_full_definition=False))
        document={'contract_id':'test','region':{'cells':[{'name':'old'}]},'boundary':[
            {'port':'a','direction':'input','original_bit':10},
            {'port':'b','direction':'input','original_bit':11},
            {'port':'y','direction':'output','original_bit':12,'reason':'cell_output'}]}
        result,missing=slicer.build(document)
        self.assertEqual(missing,[])
        module=result[1]['modules']['__auto_patch']
        manager=BDD();a,b=manager.variables(2)
        circuit=Circuit(module['cells'],{module['ports']['a']['bits'][0]:a,module['ports']['b']['bits'][0]:b},manager)
        self.assertEqual(circuit.value(module['ports']['y']['bits'][0]),a^b)
        self.assertEqual(result[3]['reused_input_cut_count'],2)
