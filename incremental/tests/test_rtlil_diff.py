"""Structural matching must not hide rewiring, attributes or process priority."""
import random
import unittest

from incremental import rtlil
from incremental.rtlil_diff import Work, opcodes


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
