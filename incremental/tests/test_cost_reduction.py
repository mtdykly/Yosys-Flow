"""Performance reductions must preserve edit content and evidence obligations."""
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from incremental import rtlil, statements
from incremental.ir_graph import Unsupported
from incremental.profile import Recorder
from yosys_flow.contracts import output_associations
from yosys_flow.extract import slice_module


class DifferenceTests(unittest.TestCase):
    def test_edits_reconstruct_exact_new_rows_with_repetition_and_insert_delete(self):
        rng=random.Random(4102)
        examples=[([],[]),([],['x']),(['x'],[]),(['x']*100,['x']*101),
                  (['a','b','c'],['z','b','w'])]
        examples += [([rng.randrange(5) for _ in range(rng.randrange(80))],
                      [rng.randrange(5) for _ in range(rng.randrange(80))]) for _ in range(150)]
        for old,new in examples:
            cursor=0;rebuilt=[]
            for tag,i,j,k,l in rtlil.changed_ranges(old,new):
                self.assertGreaterEqual(i,cursor)
                rebuilt.extend(old[cursor:i]);rebuilt.extend(new[k:l]);cursor=j
            rebuilt.extend(old[cursor:])
            self.assertEqual(rebuilt,new)

    def test_large_repeated_body_only_searches_changed_middle(self):
        old=[['connect','\\A','0']]*12000+[['cell','$and']]+[['end']]*12000
        new=old[:12000]+[['cell','$or']]+old[12001:]
        original=rtlil.difflib.SequenceMatcher
        with patch.object(rtlil.difflib,'SequenceMatcher',wraps=original) as matcher:
            edits=list(rtlil.changed_ranges(old,new))
        self.assertEqual(edits,[('replace',12000,12001,12000,12001)])
        self.assertEqual(len(matcher.call_args.kwargs['a']),1)
        self.assertEqual(len(matcher.call_args.kwargs['b']),1)


class EvidenceScanTests(unittest.TestCase):
    def test_batched_outputs_match_independent_ordered_scans_including_duplicates(self):
        rng=random.Random(971)
        for _ in range(80):
            edges=[{'from':str(rng.randrange(30)), 'to':str(rng.randrange(30))} for _ in range(100)]
            associations=[{'ref':str(rng.randrange(30))} for _ in range(40)]
            seeds=[{str(rng.randrange(30)) for _ in range(4)} for _ in range(20)]
            expected=[]
            for initial in seeds:
                reached=set(initial)
                for edge in reversed(edges):
                    if edge['to'] in reached:reached.add(edge['from'])
                expected.append([a['ref'] for a in associations if a['ref'] in reached])
            self.assertEqual(output_associations(edges,associations,seeds),expected)
        self.assertEqual(output_associations([],[],[]),[])


class ExpansionPreviewTests(unittest.TestCase):
    def test_unbindable_chain_reaches_same_budget_without_intermediate_proofs(self):
        cells={'c'+str(i):{'type':'$_NOT_','parameters':{},'attributes':{},
                          'port_directions':{'A':'input','Y':'output'},
                          'connections':{'A':[i+2],'Y':[i+3]}} for i in range(40)}
        module={'ports':{'a':{'direction':'input','bits':[2]},'y':{'direction':'output','bits':[42]}},
                'cells':cells,'netnames':{}}
        context=SimpleNamespace(module=module,trace=object(),build=Mock())
        contract={'region':{'cells':[{'ref':'r0','name':'c0'}],'connections':[]}}
        _,boundary=slice_module(module,['c0'],'preview')
        missing=[r for r in boundary if r['direction']=='output']
        slicer=SimpleNamespace(output=lambda row: ('top',42) if row['original_bit']==42 else None)
        refs={'c'+str(i):'r'+str(i) for i in range(40)};report={'expansions':[]}
        with patch.object(statements,'group_closure',side_effect=lambda trace,refs:{'required_final_cell_refs':sorted(refs)}):
            with self.assertRaisesRegex(Unsupported,'expansion budget exceeded'):
                statements.close_missing_outputs(context,slicer,contract,missing,refs,report)
        self.assertEqual(len(report['expansions']),32)
        self.assertEqual(report['expansions'][-1]['selected_cell_count_after'],33)
        self.assertEqual(report['expansions'][0]['added_cell_refs'],['r1'])
        context.build.assert_not_called()


class AttributionTests(unittest.TestCase):
    def test_cpu_samples_separate_generation_from_later_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder=Recorder(Path(directory)/'profile')
            with recorder.span('worker'):
                with self.assertRaises(ValueError):
                    with recorder.span('yosys.candidate-export'):
                        raise ValueError('output was not generated')
                self.assertFalse(recorder.generation_finished)
                recorder.sample(None,sys._getframe())
                with recorder.span('yosys.candidate-export'):
                    pass
                recorder.sample(None,sys._getframe())
            report=recorder.finish('test')
            counts={row['phase']:row['samples'] for row in report['python_cpu_samples']}
            self.assertEqual(counts,{'generation_or_archive':1,'post_generation':1})

    def test_exclusive_times_partition_root_and_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder=Recorder(Path(directory)/'profile')
            with recorder.span('root'):
                with recorder.span('child'):
                    sum(range(100))
                try:
                    with recorder.span('failure'):raise ValueError('example')
                except ValueError:pass
            report=recorder.finish('test')
            root=next(row for row in report['spans'] if row['name']=='root')
            self.assertAlmostEqual(report['exclusive_sum_s'],root['wall_s'],places=9)
            self.assertIn('ValueError',next(row for row in report['spans'] if row['name']=='failure')['error'])
            self.assertTrue(all(r['exclusive_s']>=0 for r in report['spans']))
            self.assertEqual(len((Path(directory)/'profile/events.jsonl').read_text().splitlines()),6)
