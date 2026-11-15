"""Independent cut budgets, polarity, deep DAGs, and negative evidence."""
import unittest

from yosys_flow.abc_internal import Network, evaluate, variables, prove_cut
from yosys_flow.abc_local import cone, check_mapping, output_groups
from yosys_flow.boolean import ProofBudgetExceeded
from yosys_flow.common import TraceError
from yosys_flow.structural import Expressions


def network(nodes, pis, pos=()):
    return Network({'seq':1,'kind':'network','nodes':nodes,'pis':pis,'pos':list(pos)})


class IndependentLocalTests(unittest.TestCase):
    def test_structural_equal_is_exact_but_not_complete(self):
        m=Expressions();a,b,c=[m.variable(i) for i in range(3)]
        self.assertEqual(a&b,b&a)
        self.assertEqual(m.one^(a&b),(m.one^a)|(m.one^b))
        self.assertEqual(a^(m.one^a),m.one)
        self.assertNotEqual((a&b)|(a&c),a&(b|c))
        with self.assertRaises(ValueError): a & Expressions().variable(0)

    def test_deep_functions_and_local_cut_keep_cycle_detection(self):
        nodes=[{'id':0,'op':'pi','fanins':[]}]
        nodes.extend({'id':i,'op':'buf','fanins':[2*(i-1)]} for i in range(1,2500))
        net=network(nodes,[0])
        result,mask=net.truth()
        self.assertEqual(result[2499],result[0])
        match=prove_cut(net,2499,result,[result[0]],{'op':'buf'},False,mask,node_limit=4000)
        self.assertIsNotNone(match)
        with self.assertRaises(ProofBudgetExceeded): cone(net,2499,{0},limit=20)
        nodes[-1]['fanins']=[2*2499]
        cyclic=network(nodes,[0])
        with self.assertRaisesRegex(TraceError,'cycle'): cyclic.truth()
        with self.assertRaisesRegex(TraceError,'cycle'): cone(cyclic,2499,{0},limit=4000)

    def test_mapping_requires_independent_truth_and_recorded_polarity(self):
        aig=network([{'id':0,'op':'pi','fanins':[]},{'id':1,'op':'pi','fanins':[]},
                     {'id':2,'op':'and','fanins':[0,2]}],[0,1])
        final=network([{'id':0,'op':'pi','fanins':[]},{'id':1,'op':'pi','fanins':[]},
                       {'id':2,'op':'sop','sop':'11 1\n','fanins':[0,2]}],[0,1])
        cut={'snapshot':1,'root_literal':4,'leaves':[0,2]}
        check_mapping(2,final,{0:0,1:2,2:4},cut,{1:aig},[])
        with self.assertRaisesRegex(TraceError,'independent function'):
            check_mapping(2,final,{0:0,1:2,2:5},{**cut,'root_literal':5},{1:aig},[])

    def test_output_group_proves_rewrite_and_excludes_shared_upstream(self):
        # t=a&b is a shared boundary. The local output is factored by ABC;
        # no root structure match exists, so it needs an independent joint cut.
        pis=[{'id':i,'op':'pi','fanins':[],'name':f'p{i}'} for i in range(4)]
        first=network(pis+[
            {'id':4,'op':'and','fanins':[0,2]},
            {'id':5,'op':'and','fanins':[8,4]},
            {'id':6,'op':'and','fanins':[8,6]},
            {'id':7,'op':'sop','sop':'1- 1\n-1 1\n','fanins':[10,12]},
            {'id':8,'op':'buf','fanins':[14],'name':'ys__n9'}],list(range(4)),[8])
        final=network(pis+[
            {'id':4,'op':'and','fanins':[0,2],'gate':'AND'},
            {'id':5,'op':'sop','sop':'1- 1\n-1 1\n','fanins':[4,6],'gate':'OR'},
            {'id':6,'op':'and','fanins':[8,10],'gate':'AND'},
            {'id':7,'op':'buf','fanins':[12],'name':'ys__n9'}],list(range(4)),[7])
        m=Expressions();args=[m.variable(i) for i in range(4)]
        a=first.functions(args,m.one);b=final.functions(args,m.one)
        self.assertNotEqual(a[8],b[7])
        names={'shared':4,'or':5,'and':6};events={n:[i] for n,i in names.items()}
        attempts=[]
        groups=output_groups(first,final,{i:f'cell{i}' for i in range(4,8)},a,b,m,names,events,
                             {'shared':{'inputs':['cell4']}},{'output_ports':[9]},attempts,[])
        self.assertEqual(set(groups),{'or','and'})
        self.assertEqual(groups['or']['inputs'],['cell5','cell6','cell7'])
        self.assertTrue(any(a['status']=='verified' for a in attempts))
        final.nodes=final.nodes.updated({6:{**final.nodes[6],'op':'xor'}})
        b=final.functions(args,m.one)
        self.assertFalse(output_groups(first,final,{i:f'cell{i}' for i in range(4,8)},a,b,m,names,
            events,{}, {'output_ports':[9]},[],[]))

    def test_output_group_must_not_assume_distinct_inputs_are_correlated(self):
        nodes=[{'id':0,'op':'pi','fanins':[]},{'id':1,'op':'pi','fanins':[]},
               {'id':2,'op':'and','fanins':[0,2],'gate':'AND'},
               {'id':3,'op':'sop','sop':'0 1\n','fanins':[4],'gate':'NOT'},
               {'id':4,'op':'sop','sop':'1- 1\n-1 1\n','fanins':[4,6],'gate':'OR'},
               {'id':5,'op':'buf','fanins':[8],'name':'ys__n9'}]
        first=network(nodes,[0,1],[5])
        final=network([dict(n) if n['id']!=4 else {**n,'op':'xor','gate':'XOR'} for n in nodes],[0,1],[5])
        m=Expressions();args=[m.variable(i) for i in range(2)]
        a=first.functions(args,m.one);b=final.functions(args,m.one)
        self.assertEqual(a[5],b[5])  # both are 1 under reachable x, ~x
        attempts=[]
        result=output_groups(first,final,{4:'old_or'},a,b,m,{'out':4},{'out':[1]}, {},
                             {'output_ports':[9]},attempts,[])
        self.assertFalse(result)
        self.assertEqual(attempts[0]['reason'],'correlated_distinct_boundary_inputs')
