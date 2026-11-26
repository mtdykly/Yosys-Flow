"""Large-case experiment must not smuggle an edit into offline preparation."""
from pathlib import Path
import tempfile
import unittest

from experiments import div
from yosys_flow.common import write_json
from yosys_flow.cli import parser as archive_parser
from incremental.__main__ import parser as incremental_parser


class DivProtocolTests(unittest.TestCase):
    def test_resume_uses_capture_cli_and_never_supplies_new_source_to_archive(self):
        plan = div.commands(Path('/tmp/job'),Path('/tmp/fixture'),'/opt/yosys','/opt/native',Path('/tmp/old/base'))
        args = archive_parser().parse_args(plan['archive'][4:])
        self.assertEqual(args.command,'resume')
        self.assertEqual(args.directory,'/tmp/old/base')
        self.assertTrue(args.prepare_evidence)
        self.assertNotIn('--source',plan['archive'])
        self.assertNotIn('/tmp/fixture/new.v',plan['archive'])

    def test_resumed_capture_time_is_not_counted_as_new_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'base').mkdir()
            write_json(root/'base/manifest.json',{'resume':{'reused_capture':True,
                'source_timings_seconds':{'traced':133.36}},'timings_seconds':{'record_validation':50}})
            write_json(root/'archive.stage.json',{'status':'passed','seconds':200})
            report=div.summarize(root)
            self.assertTrue(report['instrumented_synthesis_reused'])
            t=report['seconds']
            self.assertEqual(t['instrumented_synthesis_and_writes'],133.36)
            self.assertEqual(t['instrumented_synthesis_in_this_run'],0)
            self.assertEqual(t['archive_after_reused_capture'],200)
            self.assertIsNone(t['archive_including_capture_and_base_evidence'])

    def test_commands_use_existing_clis_without_target_hints(self):
        plan = div.commands(Path('/tmp/job'), Path('/tmp/fixture'), '/opt/yosys', '/opt/native')
        for stage in ('archive', 'evidence'):
            args = archive_parser().parse_args(plan[stage][4:])
            self.assertNotIn('/tmp/fixture/new.v', plan[stage])
            self.assertNotIn('n28918', ' '.join(plan[stage]))
            self.assertFalse(getattr(args, 'refine_outputs', False))
        args = incremental_parser().parse_args(plan['incremental'][4:])
        self.assertEqual(args.new_source, ['/tmp/fixture/new.v'])
        self.assertEqual(args.whole_equivalence_timeout, 1200)
        self.assertNotIn('/tmp/job/full', ' '.join(plan['incremental']))
        self.assertNotIn('--input-wire', plan['incremental'])

    def test_first_run_rejects_partial_preparation_and_both_cache_formats(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'proofs').mkdir()
            evidence = dict(status='base_ready_partial', new_rtl_used=False, edit_target_used=False)
            write_json(root / 'proofs/report.json', evidence)
            with self.assertRaisesRegex(ValueError, 'not ready'):
                div.require_first_incremental(root)
            evidence['status'] = 'ready'
            write_json(root / 'proofs/report.json', evidence)
            div.require_first_incremental(root)
            for name in ('regions.sqlite3', 'regions-abcdef.sqlite3'):
                (root / 'proofs' / name).touch()
                with self.assertRaisesRegex(ValueError, 'cache already exists'):
                    div.require_first_incremental(root)
                (root / 'proofs' / name).unlink()
            evidence['edit_target_used'] = True
            write_json(root / 'proofs/report.json', evidence)
            with self.assertRaisesRegex(ValueError, 'not ready'):
                div.require_first_incremental(root)

    def test_no_speedup_for_unvalidated_or_cached_generation_and_nested_times_not_added(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'incremental').mkdir()
            write_json(root / 'full.stage.json', dict(status='passed', seconds=100))
            write_json(root / 'incremental.stage.json', dict(status='passed', seconds=95))
            report = {'status': 'failed', 'timings': {'netlist_generation_seconds': 60,
                'required_old_region_proof_seconds': 10, 'post_generation_validation_seconds': 30},
                'proof_evidence': {'metrics': {'initial_region_cache_entries': 0, 'cache_hits': 0}}}
            write_json(root / 'incremental/report.json', report)
            self.assertIsNone(div.summarize(root)['speedup_F_over_P'])
            report['status'] = 'passed'
            write_json(root / 'incremental/report.json', report)
            summary = div.summarize(root)
            self.assertEqual(summary['seconds']['P_complete_new_RTL_to_candidate'], 60)
            self.assertAlmostEqual(summary['speedup_F_over_P'], 100 / 60)
            report['proof_evidence']['metrics']['cache_hits'] = 1
            write_json(root / 'incremental/report.json', report)
            self.assertIsNone(div.summarize(root)['speedup_F_over_P'])
            write_json(root / 'incremental.stage.json', dict(status='timeout', seconds=7200))
            self.assertEqual(div.summarize(root)['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
