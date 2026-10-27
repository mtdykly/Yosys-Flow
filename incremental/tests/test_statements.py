"""Within-module edits use ANUBIS expressions with documented adaptations."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from incremental import anubis, automatic, ir_graph, rtlil
from incremental.__main__ import parser
from incremental.tests.test_replacement import NATIVE
from yosys_flow.cli import parser as flow_parser
from yosys_flow.common import TraceError
from yosys_flow.contracts import ContractContext, seal
from yosys_flow.runner import run as baseline_run


def fixtures(directory):
    anubis.verify_corpus();directory.mkdir(parents=True)
    original=(anubis.CORPUS/'upstream/dlx/bypass_id.v').read_text()
    base,_=anubis.materialize(original,set(),'bypass_id_bug','unused')
    edited,_=anubis.materialize(original,{'ANUBIS_LOCAL_3'},'bypass_id_bug','unused')
    def assignment(text,name):
        return next(s.strip() for s in text.splitlines() if s.strip().startswith('assign '+name+' ='))
    raw_rs=assignment(base,'rsr31');raw_rt=assignment(base,'rtr31').replace('IRid','IRother')
    raw_modified=assignment(edited,'rsr31')
    # The pinned corpus defines r31 as SIX ones, while rs/rt are five-bit
    # fields. Keep that original constant-only edit as a rejection test. For
    # the live-comparator test, explicitly widen the compared fields to six;
    # do not silently fix the upstream macro or claim unadapted benchmark RTL.
    rs=raw_rs.replace('[`rs]','[26:21]');rt=raw_rt.replace('[`rt]','[25:20]')
    modified=raw_modified.replace('[`rs]','[26:21]')
    header='module demo(input [31:0] IRid, IRother, output rsr31,rtr31);\n'
    direct=header+rs+'\n'+rt+'\nendmodule\n'
    direct_new=header+modified+'\n'+rt+'\nendmodule\n'
    constant=header+raw_rs+'\n'+raw_rt+'\nendmodule\n'
    constant_new=header+raw_modified+'\n'+raw_rt+'\nendmodule\n'
    # Same ANUBIS comparisons, expressed as one combinational process with
    # nested control, ordered overrides and an independent second target.
    process=header.replace('output rsr31,rtr31','output reg rsr31,rtr31')+'''
always @* begin
  rsr31 = 0;
  if (IRid[26:21] == `r31) begin
    if (IRid[`rt] != `r0) rsr31 = 1;
  end
  case (IRid[`op])
    `JAL: rsr31 = 1;
    `JR: rsr31 = 0;
    default: begin end
  endcase
'''+rt.removeprefix('assign ')+'\nend\nendmodule\n'
    process_new=process.replace('IRid[26:21] == `r31','IRid[26:21] == `r0')
    # Compose the original ALU AND/XOR/OR branch expressions into a 4-bit
    # dependency chain. This is a declared width/connection adaptation, not a
    # claim to run the complete ALU or another upstream macro edit.
    alu=(anubis.CORPUS/'upstream/dlx/alu.v').read_text()
    def expression(op):
        row=next(s for s in alu.splitlines() if '`select_alu_'+op+':' in s)
        return row.split('ALUout = ',1)[1].split(';',1)[0]
    chain='''module demo(input [3:0] RSbus,Y,RTbus,other_a,other_b, output [3:0] t,result,side);
assign t = '''+expression('and')+''';
assign result = '''+expression('xor').replace('RSbus','t').replace('Y','RTbus')+''';
assign side = '''+expression('or').replace('RSbus','other_a').replace('Y','other_b')+''';
endmodule
'''
    chain_new=chain.replace('t ^ RTbus','t | RTbus')
    missing=chain.replace('output [3:0] t,result,side','output [3:0] result,side').replace('assign t =','wire [3:0] t;\nassign t =')
    missing_new=missing.replace('RSbus & Y','RSbus | Y')
    # Project ALU's operand selection to one condition and four-bit operands,
    # express it using if/case, and keep an independent ALU OR in the SAME
    # process. The edit switches the conditional data operand, not its guard.
    process_header='''module demo(input UseImm, input [3:0] RSbus,RTbus,Imm,other_a,other_b,
output reg [3:0] result,side);
always @* begin
  result = RSbus;
'''
    process_tail='\n  side = other_a | other_b;\nend\nendmodule\n'
    process_if=process_header+'  if (UseImm) result = RTbus;'+process_tail
    process_case=process_header+'''  case (UseImm)
    1'b1: result = RTbus;
    default: begin end
  endcase'''+process_tail
    result={}
    notice=original[:original.index('`include')]
    definitions=(anubis.CORPUS/'upstream/dlx/dlx_defs.v').read_text()
    for name,a,b in [('direct',direct,direct_new),('process',process,process_new),('constant_edit',constant,constant_new),
                     ('chain',chain,chain_new),('missing_cut',missing,missing_new),
                     ('new_dependency',chain,chain.replace('t ^ RTbus','t | side')),
                     ('process_if',process_if,process_if.replace('result = RTbus','result = Imm')),
                     ('process_case',process_case,process_case.replace('result = RTbus','result = Imm'))]:
        root=directory/name;root.mkdir()
        for version,text in [('base',a),('new',b)]:
            (root/(version+'.v')).write_text(notice+'\n'+definitions+'\n'+text)
        (root/'adaptation.json').write_text(json.dumps({'upstream_commit':anubis.verify_corpus()['commit'],
            'source_files':['dlx/bypass_id.v'] if name in {'direct','process','constant_edit'} else ['dlx/alu.v'],
            'scope':name,'adaptation':'selected expressions; independent neighbor; explicit process/width/connection wrapper',
            'r31_comparison_field_bits':6 if name in {'direct','process'} else 5 if name=='constant_edit' else None,
            'actual_upstream_macro':'ANUBIS_LOCAL_3' if name=='direct' else None},indent=2)+'\n')
        result[name]=root
    return result


class AssignmentFingerprintTests(unittest.TestCase):
    def graph(self,text):
        modules=rtlil.parse(text)
        return ir_graph.Graph(modules['demo'],modules)

    def test_upstream_edit_is_a_seed_but_downstream_is_not_a_reusable_cut(self):
        # Writer-format unit data, independent of any external RTL corpus.
        text=r'''module \demo
  wire input 1 \a
  wire input 2 \b
  wire input 3 \c
  wire \t
  wire output 4 \y
  cell $and $op1
    connect \A \a
    connect \B \b
    connect \Y \t
  end
  cell $xor $op2
    connect \A \t
    connect \B \c
    connect \Y \y
  end
end
'''
        old=self.graph(text);new=self.graph(text.replace('cell $and','cell $or'))
        self.assertEqual({r['wire'] for r in ir_graph.differences(old,new)},{'\\t'})
        self.assertNotEqual(old.value(('\\y',0),root=True),new.value(('\\y',0),root=True))
        renamed=self.graph(text.replace('$op1','$moved1').replace('$op2','$moved2'))
        self.assertEqual(old.value(('\\y',0),root=True),renamed.value(('\\y',0),root=True))

    def test_process_case_priority_and_assignment_order_affect_only_target(self):
        text=r'''module \demo
  wire input 1 \s
  wire input 2 \a
  wire input 3 \b
  wire output 4 \y
  wire output 5 \z
  wire $ytmp
  wire $ztmp
  process $proc
    assign $ytmp \a
    assign $ztmp \b
    switch \s
      case 1'1
        assign $ytmp \b
      case
        assign $ytmp \a
    end
    sync always
      update \y $ytmp
      update \z $ztmp
  end
end
'''
        old=self.graph(text)
        for changed in (text.replace("case 1'1","case 1'0"),
                        text.replace('    assign $ytmp \\a','    assign $ytmp \\b\n    assign $ytmp \\a')):
            new=self.graph(changed)
            self.assertEqual({r['wire'] for r in ir_graph.differences(old,new)},{'\\y'})
            self.assertEqual(old.value(('\\z',0),root=True),new.value(('\\z',0),root=True))


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built; run make native')
class StatementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='statement-anubis-')
        cls.root=Path(os.environ.get('YOSYS_FLOW_STATEMENT_RESULTS',cls.temp.name)).resolve()
        cls.root.mkdir(parents=True,exist_ok=True)
        cls.fixtures=fixtures(cls.root/'fixtures');cls.baselines={};cls.results={}
        for name,fixture in cls.fixtures.items():
            base=cls.root/(name+'-baseline')
            baseline_run(flow_parser().parse_args(['run','--source',str(fixture/'base.v'),'--top','demo',
                '--out',str(base),'--native-yosys',str(NATIVE)]))
            out=cls.root/name
            args=parser().parse_args(['auto','--base-run',str(base),
                '--new-source',str(fixture/'new.v'),'--out',str(out),'--native-yosys',str(NATIVE)])
            if name=='missing_cut':
                # Fault injection: withhold the internal t anchor. No archive
                # is edited, and the expanded cut still needs full proof/SAT.
                original=automatic.Endpoints.bit
                def unavailable(self,path,wire,offset,seen=frozenset()):
                    return None if wire in {'t','\\t'} else original(self,path,wire,offset,seen)
                with patch.object(automatic.Endpoints,'bit',unavailable):result=automatic.run(args)
            else:result=automatic.run(args)
            cls.baselines[name]=base;cls.results[name]=(result,out)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def successful(self,name):
        result,out=self.results[name]
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['selection_strategy'],'statement_dependency_slice',result.get('statement_plan'))
        self.assertTrue(all(result['checks'].values()))
        self.assertLess(result['splice']['removed_cells'],result['splice']['base_cells'])
        return result,out

    def test_candidates_need_independent_source_cut_proof_and_keep_precision(self):
        for name in ('direct','process'):
            with self.subTest(name=name):
                result,out=self.results[name]
                self.assertEqual({r['wire'] for r in result['statement_plan']['seeds']},{'\\rsr31'})
                self.assertEqual(result['status'],'passed',result.get('failure'))
                doc=json.loads((out/'replacement-plan.json').read_text())
                proof=doc['verification']['source_region_function_proof']
                self.assertEqual(proof['status'],'verified')
                self.assertTrue(proof['all_outputs'])
                self.assertTrue(doc['verification']['candidate_refs'])
                self.assertFalse(doc['end_to_end_exact'])
                self.assertFalse(doc['provenance']['complete_causal_history'])
                context=ContractContext(self.baselines[name])
                self.assertEqual(context.validate(doc),doc)
                bad=deepcopy(doc);bad['verification']['source_region_function_proof']['anchors']=[]
                with self.assertRaises(TraceError):context.validate(seal(bad))

    def test_one_process_preserves_if_case_semantics_and_other_target(self):
        for name in ('process_if','process_case'):
            with self.subTest(name=name):
                result,out=self.successful(name)
                self.assertEqual({r['wire'] for r in result['statement_plan']['seeds']},{'\\result'})
                self.assertTrue(all(r['definition_kind']=='process' for r in result['statement_plan']['seeds']))
                original=json.loads((self.baselines[name]/'after.json').read_text())['modules']['demo']
                independent=set(original['ports']['side']['bits'])
                drivers={n for n,c in original['cells'].items() if any(b in independent for p,bs in c['connections'].items()
                         if c['port_directions'][p]=='output' for b in bs)}
                self.assertTrue(drivers<=set(result['splice']['reused_names']))
                self.assertEqual(result['splice']['removed_cells'],4)
                self.assertEqual(result['splice']['reused_cells'],4)
                spec=json.loads((out/'patch-specification.json').read_text())
                self.assertLess(spec['local_coarse_cell_count'],spec['whole_changed_modules_coarse_cell_count'])
                self.assertTrue(spec['additional_input_bits'])

    def test_unchanged_internal_function_is_an_input_cut(self):
        result,out=self.successful('chain')
        self.assertEqual((result['splice']['removed_cells'],result['splice']['reused_cells']),(4,8))
        self.assertEqual({r['wire'] for r in result['statement_plan']['seeds']},{'\\result'})
        original=json.loads((self.baselines['chain']/'after.json').read_text())['modules']['demo']
        t=set(original['ports']['t']['bits'])
        contract=json.loads((out/'replacement-plan.json').read_text())
        self.assertTrue(t<={r['original_bit'] for r in contract['boundary'] if r['direction']=='input'})
        spec=json.loads((out/'patch-specification.json').read_text())
        self.assertFalse(any(c['type']=='$and' for c in json.loads((out/'work/statement-slice.json').read_text())['modules']['__auto_patch']['cells'].values()))
        self.assertLess(spec['local_coarse_cell_count'],spec['whole_changed_modules_coarse_cell_count'])

    def test_missing_endpoint_expands_to_actual_downstream_users(self):
        result,out=self.successful('missing_cut')
        self.assertEqual({r['wire'] for r in result['statement_plan']['seeds']},{'\\t'})
        self.assertTrue(result['statement_plan']['expansions'])
        self.assertEqual({r['reason'] for r in result['statement_plan']['expansions']},{'old_output_cut_disappeared'})
        self.assertEqual((result['splice']['removed_cells'],result['splice']['reused_cells']),(8,4))

    def test_new_dependency_uses_old_unchanged_internal_signal(self):
        result,out=self.successful('new_dependency')
        original=json.loads((self.baselines['new_dependency']/'after.json').read_text())['modules']['demo']
        spec=json.loads((out/'patch-specification.json').read_text())
        self.assertEqual(set(spec['additional_input_bits']),set(original['ports']['side']['bits']))
        self.assertEqual((result['splice']['removed_cells'],result['splice']['reused_cells']),(4,8))

    def test_source_gate_derivation_replays_all_outputs_and_rejects_forgery(self):
        _,out=self.successful('chain')
        contract=json.loads((out/'replacement-plan.json').read_text())
        self.assertEqual(contract['derivation']['kind'],'source_cells')
        context=ContractContext(self.baselines['chain'])
        self.assertEqual(context.validate(contract),contract)
        bad=deepcopy(contract);bad['outputs'][0]['external_users']=[]
        with self.assertRaises(TraceError):context.validate(seal(bad))
        bad=deepcopy(contract);bad['region']['cells'].pop()
        with self.assertRaises(TraceError):context.validate(seal(bad))

    def test_original_constant_only_macro_stays_blocked(self):
        result,out=self.results['constant_edit']
        self.assertEqual(result['status'],'blocked',result.get('failure'))
        self.assertIn('consumer rebinding',result['statement_plan']['reason'])
        self.assertFalse((out/'incremental.v').exists())
