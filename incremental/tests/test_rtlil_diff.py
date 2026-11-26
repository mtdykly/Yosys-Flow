"""Structural matching must not hide rewiring, attributes or process priority."""
import random
import unittest
from unittest.mock import patch

from incremental import rtlil
from incremental.rtlil_diff import MAX_POSITIONAL_EDITS, Work, _positional_edits, opcodes


def compare(a, b):
    return rtlil.compare(rtlil.parse(a), rtlil.parse(b), 'top')[0]


def network(count, extra=False, prefix='old'):
    wires=[]; cells=[]; connects=[]
    indices=([-1] if extra else [])+list(range(count))
    for i in indices:
        temporary=f'${prefix}$tmp{i}'
        wires.append(f'  wire width 8 {temporary}')
        cells.append(f'''  cell $xor ${prefix}$cell{i}
    parameter \\A_WIDTH 8
    parameter \\B_WIDTH 8
    parameter \\Y_WIDTH 8
    connect \\A \\input{i}
    connect \\B \\common
    connect \\Y {temporary}
  end''')
        connects.append(f'  connect \\output{i} {temporary}')
    return 'module \\top\n'+'\n'.join(wires+cells+connects)+'\nend\n'


class StructuredDifferenceTests(unittest.TestCase):
    def test_lexical_filters_keep_port_semantics_and_retained_attributes(self):
        text='''module \\top
  attribute \\src "old.v:1.1-1.2"
  wire width 4 signed input 1 \\a
  wire output 2 \\y
  wire inout 3 \\io
  wire \\input_hidden
  attribute \\src_extra 1
  connect \\y \\a
end
'''
        modules=rtlil.parse(text);ports=modules['top'].ports()
        self.assertEqual(set(ports),{'a','y','io'})
        self.assertEqual(ports['a']['width'],4)
        self.assertTrue(ports['a']['signed'])
        self.assertEqual(ports['io']['direction'],'inout')
        self.assertFalse(compare(text,text.replace('old.v:1.1-1.2','new.v:3.4-5.6'))['changes'])
        self.assertTrue(compare(text,text.replace('src_extra 1','src_extra 0'))['changes'])

    def test_sparse_name_stable_edits_skip_global_alignment(self):
        old=network(5000)
        new=old.replace('cell $xor $old$cell1\n','cell $or $old$cell1\n').replace(
            'connect \\A \\input4999','connect \\A \\common')
        with patch('incremental.rtlil_diff._alignment',side_effect=AssertionError('unneeded alignment')):
            report=compare(old,new)
        edits=report['changes'][0]['ir_changes']
        self.assertEqual(sum(len(r['old']) for r in edits),2)
        self.assertTrue(any(r['old'][0][:2]==['cell','$xor'] and r['new'][0][:2]==['cell','$or']
                            for r in edits))
        self.assertIn({'operation':'replace','old':[['connect','\\A','\\input4999']],
                       'new':[['connect','\\A','\\common']]},edits)
        stats=report['diff_statistics'][0]
        self.assertEqual(stats['matching_path'],'exact_identity')
        self.assertEqual(stats['alignment_passes'],0)

    def test_dense_name_stable_edits_keep_bounded_fallback(self):
        old=network(MAX_POSITIONAL_EDITS+1)
        new=old.replace('connect \\B \\common','connect \\B 1')
        report=compare(old,new)
        stats=report['diff_statistics'][0]
        self.assertEqual(stats['matching_path'],'bounded_alignment')
        self.assertTrue(report['changes'])
        self.assertLessEqual(stats['exact_gap_work_cells'],stats['exact_gap_work_limit'])

    def test_early_expression_insertion_does_not_rewrite_unchanged_body(self):
        old=network(1500)
        new=network(1500,extra=True,prefix='moved')
        report=compare(old,new)
        self.assertEqual(len(report['changes']),1)
        change=report['changes'][0]; stats=change['diff_statistics']
        self.assertGreaterEqual(stats['aligned_generated_identifiers'],3000)
        # Exactly the inserted wire, cell (8 rows), and assignment are edits.
        self.assertEqual(sum(len(r['old']) for r in change['ir_changes']),0)
        self.assertEqual(sum(len(r['new']) for r in change['ir_changes']),10)
        self.assertEqual(stats['conservative_gap_fallbacks'],0)

    def test_generated_rename_and_source_move_are_not_function_edits(self):
        old=network(40)
        new=old.replace('old','another-file')
        self.assertFalse(compare(old,new)['changes'])

    def test_numbering_shift_cannot_hide_later_real_change(self):
        old=network(600)
        new=network(600,extra=True,prefix='new').replace('connect \\A \\input599','connect \\A \\common')
        report=compare(old,new)
        edits=report['changes'][0]['ir_changes']
        self.assertTrue(any(['connect','\\A','\\input599'] in row['old'] for row in edits))
        self.assertTrue(any(['connect','\\A','\\common'] in row['new'] for row in edits))
        self.assertLess(sum(len(r['old'])+len(r['new']) for r in edits),30)

    def test_changed_constant_keeps_cell_identity_but_not_its_value(self):
        old=network(1).replace('connect \\B \\common','connect \\B 0')
        new=old.replace('connect \\B 0','connect \\B 1')
        edits=compare(old,new)['changes'][0]['ir_changes']
        self.assertEqual(edits,[{'operation':'replace','old':[['connect','\\B','0']],
                                'new':[['connect','\\B','1']]}])

    def test_width_and_retained_attributes_are_changes(self):
        old=network(2).replace('  wire width 8 $old$tmp0',
                              '  attribute \\keep 1\n  wire width 8 $old$tmp0')
        for new in (old.replace('attribute \\keep 1','attribute \\keep 0'),
                    old.replace('wire width 8 $old$tmp0','wire width 7 $old$tmp0')):
            self.assertTrue(compare(old,new)['changes'])

    def test_random_wiring_and_parameter_changes_never_become_equal(self):
        rng=random.Random(716)
        old=network(15)
        for _ in range(45):
            i=rng.randrange(15)
            new=network(15,extra=bool(rng.randrange(2)),prefix='new')
            if rng.randrange(2):
                new=new.replace(f'connect \\A \\input{i}',f'connect \\A \\input{(i+1)%15}')
            else:
                new=new.replace(f'cell $xor $new$cell{i}\n    parameter \\A_WIDTH 8',
                                f'cell $xor $new$cell{i}\n    parameter \\A_WIDTH 7')
            self.assertTrue(compare(old,new)['changes'])

    def test_local_shapes_cannot_erase_cross_object_connectivity(self):
        # NOT(a), NOT(b) are independent. Renaming outputs independently in
        # each cell would incorrectly accept tying BOTH outputs to NOT(a).
        old='''module \\top
  wire $x
  wire $z
  cell $not $c0
    connect \\A \\a
    connect \\Y $x
  end
  cell $not $c1
    connect \\A \\b
    connect \\Y $z
  end
  connect \\y $x
  connect \\z $z
end
'''
        new=old.replace('connect \\z $z','connect \\z $x')
        report=compare(old,new)
        self.assertTrue(report['changes'])
        self.assertEqual(report['diff_statistics'][0]['matching_path'],'bounded_alignment')
        self.assertGreater(report['diff_statistics'][0]['alignment_conflicts'],0)

    def test_process_is_one_block_and_keeps_order_and_attributes(self):
        old='''module \\top
  wire \\a
  wire \\y
  attribute \\full_case 1
  process $proc0
    assign \\y 1'0
    attribute \\full_case 1
    switch \\a
      case 1'1
        assign \\y 1'1
      case
        assign \\y 1'0
    end
    sync always
      update \\y \\a
  end
end
'''
        modules=rtlil.parse(old)
        rows,spans=modules['top'].canonical_objects(modules)
        block=next(rows[i:j] for i,j in spans if any(r[0]=='process' for r in rows[i:j]))
        self.assertEqual(block[0],['attribute','\\full_case','1'])
        self.assertIn(['switch','\\a'],block)
        self.assertIn(['sync','always'],block)
        for new in [old.replace('case 1\'1','case 1\'0'),
                    old.replace('update \\y \\a','update \\y 1\'0'),
                    old.replace('attribute \\full_case 1','attribute \\full_case 0'),
                    old.replace("assign \\y 1'0\n    attribute", "assign \\y 1'1\n    attribute")]:
            self.assertTrue(compare(old,new)['changes'])
        ordered=old.replace("    assign \\y 1'0", "    assign \\y 1'0\n    assign \\y 1'1",1)
        swapped=ordered.replace("    assign \\y 1'0\n    assign \\y 1'1", "    assign \\y 1'1\n    assign \\y 1'0",1)
        self.assertTrue(compare(ordered,swapped)['changes'])

    def test_changed_child_is_still_visited_in_unchanged_parent(self):
        child=network(20).replace('module \\top','module \\child')
        parent='''module \\top
  cell \\child \\u0
  end
end
'''
        old=parent+child
        new=parent+child.replace('connect \\A \\input19','connect \\A \\input18')
        report=compare(old,new)
        self.assertEqual([r['instance'] for r in report['changes']],['top/u0'])
        self.assertIn('top',report['unchanged_local_bodies'])


class MatchingBudgetTests(unittest.TestCase):
    def test_identity_path_preserves_every_literal_change_and_rejects_reference_changes(self):
        rng=random.Random(1091)
        for _ in range(100):
            old=[['connect','\\A',f'@generated_{i}',str(rng.randrange(4))] for i in range(40)]
            new=[list(r) for r in old]
            for i in rng.sample(range(40),8):new[i][-1]=str(4+rng.randrange(4))
            edits=_positional_edits(old,new)
            self.assertIsNotNone(edits)
            rebuilt=[list(r) for r in old]
            for tag,i,j,k,l in edits:
                self.assertEqual(tag,'replace');rebuilt[i:j]=new[k:l]
            self.assertEqual(rebuilt,new)
            new[17][2]='@generated_0'
            self.assertIsNone(_positional_edits(old,new))
        self.assertIsNone(_positional_edits([['wire','@generated_0']],[]))

    def assert_exact_edits(self,old,new,work):
        rebuilt=[]; cursor=0
        for tag,i,j,k,l in opcodes(old,new,work):
            if tag=='equal':
                self.assertEqual(old[i:j],new[k:l]); continue
            rebuilt.extend(old[cursor:i]);rebuilt.extend(new[k:l]);cursor=j
        rebuilt.extend(old[cursor:])
        self.assertEqual(rebuilt,new)

    def test_large_repeated_unanchored_middle_has_bounded_conservative_edit(self):
        old=['left']+['parameter']*20000+['old']
        new=['right']+['parameter']*20000+['new']
        work=Work()
        self.assert_exact_edits(old,new,work)
        self.assertEqual(work.cells,0)
        self.assertEqual(work.fallbacks,1)

    def test_one_budget_is_shared_across_gaps_and_exhaustion_keeps_changes(self):
        old=[];new=[]
        for i in range(80):
            old+=['old']*8+[f'anchor{i}']
            new+=['new']*8+[f'anchor{i}']
        work=Work(remaining=100)
        self.assert_exact_edits(old,new,work)
        self.assertLessEqual(work.cells,100)
        self.assertGreater(work.fallbacks,1)

    def test_reordered_unique_anchors_and_repeated_regions_remain_exact(self):
        rng=random.Random(8107)
        for _ in range(100):
            old=[str(rng.randrange(10)) for _ in range(rng.randrange(150))]
            new=[str(rng.randrange(10)) for _ in range(rng.randrange(150))]
            self.assert_exact_edits(old,new,Work(remaining=1000))
