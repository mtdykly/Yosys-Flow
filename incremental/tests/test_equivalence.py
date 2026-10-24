"""ANUBIS functional/no-functional edits and fail-closed reuse proofs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from incremental import anubis, automatic, equivalence, rtlil, replace
from incremental.__main__ import parser
from incremental.tests.test_replacement import NATIVE
from yosys_flow.cli import parser as flow_parser
from yosys_flow.common import digest, write_json
from yosys_flow.runner import run as baseline_run


class ProofOutcomeTests(unittest.TestCase):
    def test_binary_leaf_premise_cannot_ignore_undefined_or_partial_context(self):
        context={'top':{'ports':{},'cells':{'leaf':{'type':'leaf','connections':{'a':['x']}}}},
                 'leaf':{'ports':{},'cells':{}}}
        self.assertFalse(equivalence.binary_context(context,{'leaf'})['verified'])
        context['top']['cells']['leaf']['connections']['a']=[2]
        context['top']['cells']['partial']={'type':'$div','connections':{'A':[2],'B':[3],'Y':[4]}}
        self.assertFalse(equivalence.binary_context(context,{'leaf'})['verified'])
        context['top']['cells']['partial']['type']='$add'
        self.assertTrue(equivalence.binary_context(context,{'leaf'})['verified'])

    def test_timeout_failure_and_missing_marker_never_mean_equivalent(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);counterexample=directory/'counterexample.json'
            for log in ('',equivalence.TIMEOUT,equivalence.SUCCESS+'\n'+equivalence.TIMEOUT,
                        equivalence.DIFFERENT):
                self.assertEqual(equivalence.verdict(log,counterexample)[0],'unknown')
            failed=subprocess.CompletedProcess([],1,stdout=equivalence.SUCCESS,stderr='error')
            with patch.object(equivalence.subprocess,'run',return_value=failed):
                self.assertEqual(equivalence.run_phase('yosys',directory,'error','',time.perf_counter()+1),(None,'tool_error'))
            with patch.object(equivalence.subprocess,'run',side_effect=subprocess.TimeoutExpired('yosys',1,output=b'partial')):
                self.assertEqual(equivalence.run_phase('yosys',directory,'timeout','',time.perf_counter()+1),(None,'wall_timeout'))
            self.assertIn('WALL BUDGET',(directory/'timeout.log').read_text())

    def test_unknown_outputs_cannot_be_certified_by_matching_x_values(self):
        # RTLIL writer-format unit fixture, not a second external RTL corpus.
        if not shutil.which('yosys'):self.skipTest('pinned Yosys is required')
        text="module \\demo\n  wire output 1 \\y\n  connect \\y 1'x\nend\n"
        module=rtlil.parse(text)['demo']
        with tempfile.TemporaryDirectory() as temp:
            result=equivalence.prove(module,module,'yosys',Path(temp)/'proof',time.perf_counter()+5)
            self.assertEqual(result['status'],'unknown',result)
            self.assertEqual(result['reason'],'undefined_output_or_unrecognized_counterexample')
            self.assertTrue(Path(result['counterexample']).is_file())


@unittest.skipUnless(NATIVE.is_file(),'native Yosys not built; run make native')
class EquivalentReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='equivalence-anubis-')
        cls.root=Path(os.environ.get('YOSYS_FLOW_EQUIVALENCE_RESULTS',cls.temp.name)).resolve()
        cls.root.mkdir(parents=True,exist_ok=True);cls.cases={};cls.results={};cls.timings=[]
        for name in ('bypass_noc1','compare_noc2','compare_local9'):
            fixture=cls.root/name/'fixture';anubis.prepare(name,fixture)
            base=cls.root/name/'base-run'
            baseline_run(flow_parser().parse_args(['run','--source',str(fixture/'base.v'),'--top','benchmark',
                '--out',str(base),'--native-yosys',str(NATIVE)]))
            cls.cases[name]=(fixture,base)
            # Keep both modes on identical archived/new inputs. Single samples
            # reveal cost, not a machine-independent speedup benchmark.
            for enabled in (False,True) if name!='compare_local9' else (True,):
                label=name+('-on' if enabled else '-off')
                result,out=cls.execute(name,label,extra=[] if enabled else ['--no-equivalence-precheck'])
                cls.results[name,enabled]=(result,out)
                cls.timings.append({'case':name,'enabled':enabled,'status':result['status'],
                    'timings':result['timings'],'report':str(out/'report.json')})
        write_json(cls.root/'comparison.json',{'sampling':'one sample per mode; off before on; no general speedup claim',
            'scope':'pinned ANUBIS macro variants with existing declared adapters','runs':cls.timings})

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    @classmethod
    def execute(cls,case,name,source=None,base=None,extra=()):
        fixture,original=cls.cases[case];out=cls.root/name
        args=parser().parse_args(['auto','--base-run',str(base or original),
            '--new-source',str(source or fixture/'new.v'),'--out',str(out),'--native-yosys',str(NATIVE),*extra])
        return automatic.run(args),out

    def test_anubis_no_change_macros_reuse_exact_archive_without_synthesis(self):
        for name in ('bypass_noc1','compare_noc2'):
            with self.subTest(case=name):
                result,out=self.results[name,True];_,base=self.cases[name]
                self.assertEqual(result['status'],'reused_equivalent',result.get('failure'))
                self.assertTrue(all(result['checks'].values()))
                self.assertEqual(result['equivalence_precheck']['counts'],{'equivalent':1,'different':0,'unknown':0})
                self.assertFalse(result['remaining_changes']);self.assertFalse(result['selected_scopes'])
                self.assertFalse(result['local_synthesis_executed']);self.assertFalse(result['new_full_synthesis_executed'])
                self.assertFalse((out/'patch.il').exists());self.assertFalse((out/'replacement').exists())
                for suffix in ('json','v'):self.assertEqual(digest(base/('after.'+suffix)),digest(out/('incremental.'+suffix)))
                for script in out.rglob('*.ys'):
                    commands=[s.split()[0] for s in script.read_text().splitlines() if s.strip()]
                    self.assertTrue(set(commands).isdisjoint({'synth','abc','techmap'}),str(script))
                # Independent TEST oracle: full new synthesis is outside auto.
                oracle=self.root/(name+'-independent-oracle');oracle.mkdir()
                replace.run_yosys('yosys',oracle,'reference',
                    'read_rtlil '+str(out/'work/new.frontend.il')+'\nsynth -top benchmark\nwrite_json new.json')
                replace.run_yosys('yosys',oracle,'all-outputs',
                    replace.equivalence_script(str(out/'incremental.json'),'new.json','benchmark'))

    def test_disable_switch_keeps_original_incremental_path_and_reports_cost(self):
        for name in ('bypass_noc1','compare_noc2'):
            result,out=self.results[name,False]
            self.assertEqual(result['status'],'passed',result.get('failure'))
            self.assertFalse(result['equivalence_precheck']['enabled'])
            self.assertTrue(result['local_synthesis_executed']);self.assertTrue((out/'patch.il').is_file())
        comparison=json.loads((self.root/'comparison.json').read_text())
        self.assertEqual(len(comparison['runs']),5)
        self.assertTrue(all(r['timings']['total_seconds']>0 for r in comparison['runs']))

    def test_functional_edit_retains_counterexample_and_uses_local_patch(self):
        result,out=self.results['compare_local9',True]
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['equivalence_precheck']['counts'],{'equivalent':0,'different':1,'unknown':0})
        self.assertEqual(result['selected_scopes'],['benchmark/u_target'])
        self.assertTrue((out/'work/equivalence/proof-0/counterexample.json').is_file())
        self.assertTrue((out/'patch.il').is_file());self.assertTrue(all(result['checks'].values()))

    def test_timeout_and_exhausted_total_budget_continue_normal_flow(self):
        for flag in ('--equivalence-timeout','--equivalence-budget'):
            with self.subTest(flag=flag):
                result,out=self.execute('bypass_noc1',flag.removeprefix('--'),extra=[flag,'0.000000001'])
                self.assertEqual(result['status'],'passed',result.get('failure'))
                row=result['equivalence_precheck']['checks'][0]
                self.assertEqual(row['status'],'unknown')
                self.assertIn(row['reason'],{'wall_timeout','total_budget_exhausted'})
                self.assertTrue((out/'patch.il').is_file());self.assertTrue(all(result['checks'].values()))

    def test_shared_definition_proves_once_and_applies_to_both_instances(self):
        fixture,_=self.cases['bypass_noc1']
        leaf=(fixture/'edited-leaf.v').read_text().replace('module anubis_edited','module anubis_base')
        leaf=leaf.replace('`include "dlx_defs.v"',(fixture/'dlx_defs.v').read_text())
        source=self.root/'shared.v';source.write_text(leaf+'\n'+(fixture/'base-top.v').read_text())
        result,_=self.execute('bypass_noc1','shared',source)
        self.assertEqual(result['status'],'reused_equivalent',result.get('failure'))
        report=result['equivalence_precheck'];self.assertEqual(report['unique_proof_count'],1)
        self.assertEqual(report['counts']['equivalent'],2)
        self.assertEqual(sum(r['cache_hit'] for r in report['checks']),1)

    def test_only_equivalent_leaf_is_filtered_in_mixed_functional_edit(self):
        fixture,_=self.cases['compare_local9']
        original=(anubis.CORPUS/'upstream/dlx/quick_compare.v').read_text()
        eq,_=anubis.materialize(original,{'ANUBIS_NOC_2'},'quick_compare_bug','anubis_same_function')
        contents=(eq+'\n'+(fixture/'edited-leaf.v').read_text()).replace('`include "dlx_defs.v"',
                    (fixture/'dlx_defs.v').read_text())
        top=(fixture/'new-top.v').read_text().replace('anubis_base u_other','anubis_same_function u_other')
        source=self.root/'mixed.v';source.write_text(contents+'\n'+top)
        result,out=self.execute('compare_local9','mixed',source)
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['equivalence_precheck']['counts'],{'equivalent':1,'different':1,'unknown':0})
        self.assertEqual(result['selected_scopes'],['benchmark/u_target'])
        self.assertTrue(all('/u_target/' in n for n in result['splice']['removed_names']))
        self.assertTrue(all(result['checks'].values()))

    def test_flattened_candidate_provenance_does_not_block_whole_netlist_reuse(self):
        fixture,_=self.cases['bypass_noc1'];base=self.root/'flat-baseline'
        baseline_run(flow_parser().parse_args(['run','--source',str(fixture/'base.v'),'--top','benchmark',
            '--out',str(base),'--native-yosys',str(NATIVE),'--flatten']))
        with patch('yosys_flow.contracts.ContractContext.query',side_effect=AssertionError('reuse must not select gates')):
            result,out=self.execute('bypass_noc1','flat-reuse',base=base)
        self.assertEqual(result['status'],'reused_equivalent',result.get('failure'))
        self.assertEqual(digest(base/'after.json'),digest(out/'incremental.json'))

    def test_parent_wiring_change_is_not_a_leaf_proof(self):
        fixture,_=self.cases['bypass_noc1']
        top=(fixture/'new-top.v').read_text().replace('.IRex(target_IRex)',
                                                    '.IRex({target_IRmem[0], target_IRex[30:0]})')
        (self.root/'rewired-top.v').write_text(top)
        source=self.root/'rewired.v';source.write_text((fixture/'new.v').read_text().replace(
            str(fixture/'new-top.v'),str(self.root/'rewired-top.v')))
        result,_=self.execute('bypass_noc1','rewired',source)
        self.assertEqual(result['status'],'passed',result.get('failure'))
        self.assertEqual(result['equivalence_precheck']['checks'][0]['reason'],'not_a_leaf_pair')
        self.assertEqual(result['selected_scopes'],['benchmark'])

    def test_proven_reuse_still_rejects_changed_frontend_or_proof_artifact(self):
        original=equivalence.precheck
        for target in ('frontend','proof'):
            def corrupt(*args,**kwargs):
                result=original(*args,**kwargs);work=args[5]
                path=work/('new.frontend.il' if target=='frontend' else 'equivalence/proof-0/checked-miter.json')
                with path.open('a') as f:f.write('\n')
                return result
            with patch.object(equivalence,'precheck',side_effect=corrupt):
                result,out=self.execute('bypass_noc1','corrupt-'+target)
            self.assertEqual(result['status'],'failed',result)
            self.assertFalse((out/'incremental.v').exists());self.assertFalse((out/'incremental.json').exists())
