"""Real synth in both modes: instance selection, binding, contracts and export."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_native import NATIVE
from yosys_flow.cli import parser
from yosys_flow.common import ROOT, TraceError
from yosys_flow.contracts import ContractContext, seal
from yosys_flow.extract import extract
from yosys_flow.native import NativeTrace
from yosys_flow.runner import run, load_run, query_loaded


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built; run make native')
class HierarchyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='hierarchy-provenance-')
        cls.base=Path(cls.temp.name); cls.fixtures={}
        examples={
            'nested':(ROOT/'examples/hierarchy.v').read_text(),
            'joint':(ROOT/'examples/multilevel.v').read_text().replace('module demo','module leaf')+'''module demo(input a,b,c,d,e,f,g,h,i,j,k,l,output y0,z0,y1,z1);
leaf u0(a,b,c,d,e,f,y0,z0);
leaf u1(g,h,i,j,k,l,y1,z1);
endmodule
''',
            'parameters':'''module leaf #(parameter W=1)(input [W-1:0] a,b, output [W-1:0] y);
assign y=a^b;
endmodule
module demo(input [2:0] a,b, output [2:0] y);
leaf #(.W(1)) u0(a[0],b[0],y[0]);
leaf #(.W(2)) u1(a[2:1],b[2:1],y[2:1]);
endmodule
''',
            'shared':'''module leaf(input a,b, output y);
assign y=a&b;
endmodule
module demo(input a,b, output y,z);
leaf u0(a,b,y);
leaf u1(a,b,z);
endmodule
''',
            'connections':'''module leaf(input a, output y,z);
assign y=a;
assign z=1'b0;
endmodule
module demo(input a,b, output y0,z0,y1,z1);
leaf u0(a,y0,z0);
leaf u1(b,y1,z1);
endmodule
''',
            'process':'''module leaf(input s,a,b, output reg y);
always @* begin
if(s) y=a;
else y=b;
end
endmodule
module demo(input s,a,b,c,d, output y,z);
leaf u0(s,a,b,y);
leaf u1(s,c,d,z);
endmodule
''',
            'kept':'''(* keep_hierarchy=1 *) module leaf(input a,b, output y);
assign y=a&b;
endmodule
module branch(input a,b, output y);
leaf inner(a,b,y);
endmodule
module demo(input a,b,c,d, output y,z);
branch u0(a,b,y);
branch u1(c,d,z);
endmodule
'''}
        for name,rtl in examples.items():
            source=cls.base/(name+'.v'); source.write_text(rtl)
            for flat in (False,True):
                out=cls.base/(name+('-flat' if flat else '-hier'))
                args=parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out),
                                         '--native-yosys',str(NATIVE),*(['--flatten'] if flat else [])])
                result=run(args)
                if not all(result['checks'].values()): raise AssertionError(result)
                cls.fixtures[name,flat]=(source,out,load_run(out))

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def select(self,name,flat,line,instance=None,ref=None):
        source,out,(manifest,trace,_)=self.fixtures[name,flat]
        options=['--object',ref] if ref else ['--source',str(source),'--line',str(line)]
        if instance: options+=['--instance',instance]
        return query_loaded(parser().parse_args(['query',str(out),*options]),manifest,trace)

    def contract(self,name,flat,line,instance):
        source,out,_=self.fixtures[name,flat]
        ctx=ContractContext(out)
        selector=dict(source=str(source),line=line,end_line=None,column=None,end_column=None,object=None,instance=instance)
        return ctx,ctx.build({'kind':'selection','selector':selector})

    def test_default_preserves_hierarchy_and_flatten_is_explicit(self):
        for flat in (False,True):
            _,out,(manifest,_,validation)=self.fixtures['nested',flat]
            modules=json.loads((out/'after.json').read_text())['modules']
            self.assertEqual(set(modules),{'demo'} if flat else {'demo','branch','leaf'})
            self.assertEqual(manifest['endpoint'],'synth -top demo'+(' -flatten' if flat else ''))
            self.assertTrue(all(validation['checks'].values()))
            steps=[r['pass'] for r in NativeTrace(out/'yosys.jsonl').records if r['record_type']=='step_begin']
            self.assertEqual('flatten' in steps,flat)
            self.assertIn('abc',steps)

    def test_nested_instance_selects_one_gate_and_excludes_other_instance(self):
        for flat in (False,True):
            a=self.select('nested',flat,4,'demo/u0/inner')
            b=self.select('nested',flat,4,'demo/u1/inner')
            self.assertEqual(len(a['final_cells']),1); self.assertEqual(len(b['final_cells']),1)
            self.assertEqual(a['final_cells'][0]['precision'],'abc_internal_cut')
            self.assertNotEqual(a['final_cells'][0]['ref'],b['final_cells'][0]['ref'])
            self.assertEqual({s['instance'] for s in a['final_cells'][0]['all_origins']},{'demo/u0/inner'})
            both=self.select('nested',flat,4)
            self.assertEqual(len(both['final_cells']),2)

    def test_instance_contract_only_drives_its_own_top_output(self):
        for flat in (False,True):
            for instance,expected in [('demo/u0/inner','y'),('demo/u1/inner','z')]:
                ctx,doc=self.contract('nested',flat,4,instance)
                self.assertEqual(ctx.validate(doc),doc)
                self.assertEqual(doc['verification']['status'],'verified_old_region')
                users=[u for o in doc['outputs'] for u in o['external_users'] if u['kind']=='module_port']
                self.assertEqual({u['name'] for u in users},{expected})
                self.assertEqual(len(doc['region']['cells']),1)

    def test_contract_union_across_instances_and_roundtrip_export(self):
        for flat in (False,True):
            ctx,a=self.contract('nested',flat,4,'demo/u0/inner')
            _,b=self.contract('nested',flat,4,'demo/u1/inner')
            merged=ctx.build({'kind':'union','parents':[a,b]})
            ctx.validate(merged)
            self.assertEqual(len(merged['region']['cells']),2)
            self.assertEqual(len(merged['outputs']),2)
            contract=self.base/f'union-{flat}.contract.json'; contract.write_text(json.dumps(merged))
            target=self.base/f'union-{flat}.v'
            out=self.fixtures['nested',flat][1]
            result=extract(parser().parse_args(['extract',str(out),'--contract',str(contract),'--out',str(target)]))
            self.assertTrue(result['validation']['exported_verilog_equivalent_to_cell_slice'])
            report=json.loads(target.with_suffix('.provenance.json').read_text())
            self.assertEqual(report['instance_catalog'],merged['hierarchy']['instances'])

    def test_parameter_instances_have_separate_widths_and_contexts(self):
        for flat in (False,True):
            a=self.select('parameters',flat,2,'demo/u0'); b=self.select('parameters',flat,2,'demo/u1')
            self.assertEqual(len(a['final_cells']),1); self.assertEqual(len(b['final_cells']),2)
            params=a['instance_catalog']
            self.assertEqual(int(params['demo/u0']['parameters']['\\W'],2),1)
            self.assertEqual(int(params['demo/u1']['parameters']['\\W'],2),2)
            self.assertTrue({c['ref'] for c in a['final_cells']}.isdisjoint(c['ref'] for c in b['final_cells']))

    def test_shared_gate_reports_both_output_obligations(self):
        for flat in (False,True):
            ctx,a=self.contract('shared',flat,2,'demo/u0')
            _,b=self.contract('shared',flat,2,'demo/u1')
            left={c['ref'] for c in a['region']['cells']}; right={c['ref'] for c in b['region']['cells']}
            if flat: self.assertEqual(left,right)
            else: self.assertTrue(left.isdisjoint(right))
            users={u['name'] for p in a['outputs'] for u in p['external_users'] if u['kind']=='module_port'}
            self.assertEqual(users,{'y','z'} if flat else {'y'})
            self.assertFalse(a['safe_for_incremental_reuse'])

    def test_connections_and_constants_keep_instance_endpoint_identity(self):
        for flat in (False,True):
            for instance,port in [('demo/u0','z0'),('demo/u1','z1')]:
                ctx,doc=self.contract('connections',flat,3,instance)
                self.assertFalse(doc['region']['cells'])
                self.assertEqual(doc['verification']['status'],'verified_old_region')
                users={u['name'] for p in doc['outputs'] for u in p['external_users'] if u['kind']=='module_port'}
                self.assertEqual(users,{port})
                ctx.validate(doc)
            ctx,doc=self.contract('connections',flat,2,'demo/u0')
            self.assertFalse(doc['region']['cells']); self.assertEqual(len(doc['outputs']),1)

    def test_combinational_process_proofs_do_not_mix_instances(self):
        for flat in (False,True):
            a=self.select('process',flat,3,'demo/u0'); b=self.select('process',flat,3,'demo/u1')
            self.assertTrue(a['final_cells']); self.assertTrue(b['final_cells'])
            self.assertTrue({c['ref'] for c in a['final_cells']}.isdisjoint(c['ref'] for c in b['final_cells']))
            self.assertTrue(all({s['instance'] for s in c['all_origins']}=={'demo/u0'} for c in a['final_cells']))
            if not flat:
                self.assertTrue(a['process_actions'])
                self.assertTrue(all(r['instance']=='demo/u0' for r in a['process_actions']))

    def test_offline_query_and_contract_validation_need_no_toolchain(self):
        ctx,doc=self.contract('nested',False,4,'demo/u0/inner')
        with patch.dict('os.environ',{'PATH':''}),patch('subprocess.run',side_effect=AssertionError('tool invoked')):
            self.assertEqual(ctx.validate(doc),doc)
            self.assertEqual(len(self.select('nested',False,4,'demo/u0/inner')['final_cells']),1)

    def test_reverse_refs_distinguish_definition_and_instance(self):
        a=self.select('nested',False,4,'demo/u0/inner')['final_cells'][0]
        reverse=self.select('nested',False,None,ref=a['ref'])
        self.assertEqual(reverse['final_cells'][0]['ref'],a['ref'])
        with self.assertRaisesRegex(TraceError,'ambiguous'):
            self.select('nested',False,None,ref=a['definition_ref'])
        selected=self.select('nested',False,None,'demo/u0/inner',a['definition_ref'])
        self.assertEqual(selected['final_cells'][0]['ref'],a['ref'])

    def test_unknown_instance_and_forged_contract_context_are_rejected(self):
        with self.assertRaisesRegex(TraceError,'unknown instance'):
            self.select('nested',False,4,'demo/not_here')
        ctx,doc=self.contract('nested',False,4,'demo/u0/inner')
        bad=copy.deepcopy(doc); bad['hierarchy']['instances']['demo/u0/inner']['module']='other'
        with self.assertRaises(TraceError): ctx.validate(seal(bad))

    def test_actual_flatten_copy_source_and_pin_tampering_is_rejected(self):
        out=self.fixtures['nested',True][1]
        original=[json.loads(s) for s in (out/'yosys.jsonl').read_text().splitlines()]
        for mode in ('source','pin','instance'):
            rows=copy.deepcopy(original)
            if mode=='source':
                row=next(r for r in rows if r['record_type']=='source' and r.get('flatten_event'))
                row['instance_path']=['wrong']
            elif mode=='pin':
                row=next(r for r in rows if r['record_type']=='cell_state' and r['capture']=='flatten_copy' and r['state']['connections'])
                row['state']['connections'][next(iter(row['state']['connections']))]=['0']
            else:
                row=next(r for r in rows if r['record_type']=='flatten_instance'); row['name']='\\wrong'
            path=self.base/f'corrupt-{mode}.jsonl'; path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with self.assertRaises(TraceError): NativeTrace(path)

    def test_expanded_instance_view_has_independent_whole_design_proof(self):
        for (name,flat),(_,out,(_,trace,validation)) in self.fixtures.items():
            if trace.view_rule=='electrical_instance_expansion_v1':
                self.assertTrue(validation['checks']['instance_view_equivalence'])
                self.assertTrue((out/'instance_equivalence.log').is_file())

    def test_user_keep_hierarchy_retains_recorded_original_paths(self):
        a=self.select('kept',True,2,'demo/u0/inner'); b=self.select('kept',True,2,'demo/u1/inner')
        self.assertEqual(len(a['final_cells']),1); self.assertEqual(len(b['final_cells']),1)
        self.assertTrue({c['ref'] for c in a['final_cells']}.isdisjoint(c['ref'] for c in b['final_cells']))
        ctx,doc=self.contract('kept',True,2,'demo/u0/inner'); ctx.validate(doc)
        self.assertEqual({u['name'] for o in doc['outputs'] for u in o['external_users'] if u['kind']=='module_port'},{'y'})

    def test_joint_group_closure_is_local_to_the_selected_instance(self):
        for flat in (False,True):
            ctx,doc=self.contract('joint',flat,4,'demo/u0')
            self.assertEqual(len(doc['region']['cells']),2)
            self.assertTrue(doc['recorded_group_closure']['complete_for_recorded_groups'])
            self.assertEqual({s['instance'] for c in doc['region']['cells'] for s in c['all_origins']},{'demo/u0'})
            self.assertEqual({u['name'] for p in doc['outputs'] for u in p['external_users'] if u['kind']=='module_port'},{'y0'})
            ctx.validate(doc)

    def test_unsupported_state_blackbox_and_inout_fail_closed(self):
        cases={
            'state':'module leaf(input c,d,output reg q);always @(posedge c) q<=d;endmodule\nmodule demo(input a,b,output y);leaf u(a,b,y);endmodule',
            'opaque':'(* blackbox *) module leaf(input a,output y);endmodule\nmodule demo(input a,output y);leaf u(a,y);endmodule',
            'inout':'module leaf(inout a,output y);assign y=a;endmodule\nmodule demo(inout a,output y);leaf u(a,y);endmodule',
            'cycle':'module leaf(input a,b,output y);assign y=a^b;endmodule\nmodule demo(input a,output y);wire t;leaf u0(a,t,y);leaf u1(a,y,t);endmodule'}
        for name,rtl in cases.items():
            for flat in (False,True):
                with self.subTest(name=name,flat=flat):
                    source=self.base/(name+'.v'); source.write_text(rtl); out=self.base/f'bad-{name}-{flat}'
                    args=parser().parse_args(['run','--source',str(source),'--top','demo','--out',str(out),
                        '--native-yosys',str(NATIVE),*(['--flatten'] if flat else [])])
                    with self.assertRaises(TraceError): run(args)
                    self.assertEqual(json.loads((out/'manifest.json').read_text())['status'],'failed')
