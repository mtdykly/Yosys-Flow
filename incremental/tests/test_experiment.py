"""Comparison accounting uses actual successes, explicit strata and raw RTL."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from incremental import experiment, experiment_worker, experiment_cases
from yosys_flow.common import digest


def row(method, candidate, end_to_end, cells, *, case='changed', mode='retained',
        precheck=True, outcome='success', group='functional_change'):
    return {'case': case, 'mode': mode, 'method': method,
            'precheck': None if method == 'F' else precheck, 'outcome': outcome,
            'group': group, 'candidate_seconds': candidate, 'netlist_generation_seconds': candidate,
            'post_generation_validation_seconds': None,
            'end_to_end_seconds': end_to_end, 'cell_count': cells, 'logic_depth': 3}


class ExperimentSummaryTests(unittest.TestCase):
    def test_primary_speedup_uses_actual_generation_cutoff_not_checked_total(self):
        full = row('F', 10, 14, 100)
        fine = row('P-fine', 8, 200, 110)
        fine.update(netlist_generation_seconds=2, post_generation_validation_seconds=180)
        entry = next(r for r in experiment.summarize([full, fine], {'changed/retained': {
            'validated_archive_overhead_seconds': 31}}) if r['method'] == 'P-fine')
        self.assertEqual(entry['generation_speedup'], 5)
        self.assertEqual(entry['generation_seconds_saved'], 8)
        self.assertEqual(entry['candidate_speedup'], 1.25)
        self.assertEqual(entry['break_even_edits'], 4)
        self.assertEqual(entry['successful_medians']['post_generation_validation_seconds'], 180)

    def test_diagnostic_reuse_missing_generation_timing_is_not_inferred(self):
        reuse = row('P-fine', 1, 2, 100)
        reuse['netlist_generation_seconds'] = None
        entry = next(r for r in experiment.summarize([row('F', 10, 12, 100), reuse], {})
                     if r['method'] == 'P-fine')
        self.assertNotIn('generation_speedup', entry)
        self.assertNotIn('break_even_edits', entry)

    def test_zero_duration_and_incomplete_series_have_no_speedup_claim(self):
        rows = [row('F', 1, 2, 10), row('P-fine', 0, 1, 10),
                row('P-module', 0.5, 1, 10), row('P-module', None, 3, None, outcome='blocked')]
        for result in experiment.summarize(rows, {}):
            self.assertNotIn('candidate_speedup', result)

    def test_ratios_use_matching_full_result_and_archive_cost_is_amortized(self):
        rows = [row('F', 10, 12, 100), row('P-module', 4, 9, 120),
                row('P-fine', 2, 7, 110),
                row('F', 20, 24, 200, mode='flatten'),
                row('P-fine', 5, 15, 250, mode='flatten')]
        result = experiment.summarize(rows, {'changed/retained': {
            'validated_archive_overhead_seconds': 31}})
        lookup = {(r['mode'], r['method']): r for r in result}
        coarse = lookup['retained', 'P-module']
        self.assertEqual(coarse['candidate_speedup'], 2.5)
        self.assertEqual(coarse['end_to_end_speedup'], 12 / 9)
        self.assertEqual(coarse['candidate_seconds_saved'], 6)
        self.assertEqual(coarse['break_even_edits'], 6)
        self.assertAlmostEqual(coarse['generic_cell_overhead_percent'], 20)
        flat = lookup['flatten', 'P-fine']
        self.assertEqual(flat['candidate_speedup'], 4)
        self.assertAlmostEqual(flat['generic_cell_overhead_percent'], 25)
        self.assertIsNone(flat['break_even_edits'])
        self.assertNotIn('candidate_speedup', lookup['retained', 'F'])

    def test_failed_and_refused_samples_never_enter_successful_medians(self):
        rows = [row('F', 10, 12, 100), row('F', 9999, 9999, 9999, outcome='error'),
                row('P-fine', 2, 6, 110), row('P-fine', 4, 8, 130),
                row('P-fine', 1, 1, 1, outcome='blocked'),
                row('P-module', 0.1, 0.1, 1, outcome='timeout')]
        summaries = {r['method']: r for r in experiment.summarize(rows, {})}
        fine = summaries['P-fine']
        self.assertEqual(fine['successful_medians']['candidate_seconds'], 3)
        self.assertEqual(fine['successful_medians']['end_to_end_seconds'], 7)
        self.assertEqual(fine['successful_medians']['cell_count'], 120)
        self.assertEqual(fine['outcomes'], {'success': 2, 'blocked': 1})
        self.assertFalse(fine['complete_successful_series'])
        self.assertEqual(summaries['F']['successful_medians']['candidate_seconds'], 10)
        self.assertIsNone(summaries['P-module']['successful_medians']['candidate_seconds'])
        self.assertNotIn('candidate_speedup', summaries['P-module'])
        self.assertNotIn('generic_cell_overhead_percent', summaries['P-module'])

    def test_noc_precheck_on_off_and_functional_changes_stay_separate(self):
        rows = [row('F', 10, 12, 100, case='noc', group='noc'),
                row('P-fine', 1, 2, 100, case='noc', group='noc', precheck=True),
                row('P-fine', 4, 8, 100, case='noc', group='noc', precheck=False),
                row('F', 20, 22, 200), row('P-fine', 5, 15, 210)]
        result = experiment.summarize(rows, {})
        lookup = {(r['case'], r['method'], r['precheck']): r for r in result}
        self.assertEqual(len(result), 5)
        self.assertEqual(lookup['noc', 'P-fine', True]['candidate_speedup'], 10)
        self.assertEqual(lookup['noc', 'P-fine', False]['candidate_speedup'], 2.5)
        self.assertEqual(lookup['changed', 'P-fine', True]['candidate_speedup'], 4)

    def test_no_savings_has_no_break_even_and_zero_cell_denominator_is_not_a_ratio(self):
        rows = [row('F', 2, 3, 0), row('P-fine', 4, 5, 0)]
        entry = next(r for r in experiment.summarize(rows, {'changed/retained': {
            'validated_archive_overhead_seconds': 10}}) if r['method'] == 'P-fine')
        self.assertEqual(entry['candidate_speedup'], 0.5)
        self.assertIsNone(entry['break_even_edits'])
        self.assertNotIn('generic_cell_overhead_percent', entry)

    def test_outcome_keeps_timeouts_validation_failures_and_scope_separate(self):
        controls = [({'status': 'timeout'}, 'timeout'),
                    ({'status': 'blocked', 'failure': {'code': 'source_correspondence_not_proven'}}, 'blocked'),
                    ({'status': 'failed', 'failure': {'code': 'new_design_equivalence_failed'}}, 'validation_failed'),
                    ({'status': 'failed', 'failure': {'code': 'state_outside_combinational_scope'}}, 'out_of_scope'),
                    ({'status': 'reused_equivalent'}, 'success')]
        for value, expected in controls:
            with self.subTest(value=value):
                self.assertEqual(experiment.outcome(value), expected)


class ExperimentMeasurementTests(unittest.TestCase):
    def test_missing_or_modified_frozen_input_invalidates_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input.v'
            path.write_text('old')
            frozen = {path: digest(path)}
            self.assertTrue(experiment.hashes_match(frozen))
            path.write_text('new')
            self.assertFalse(experiment.hashes_match(frozen))
            path.unlink()
            self.assertFalse(experiment.hashes_match(frozen))

    def test_native_abc_evidence_size_counts_actual_artifact_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in [('yosys.jsonl', '123'), ('yosys.jsonl.abc_1.input.blif', '12345'),
                               ('yosys.jsonl.abc_1.input.blif.flow.jsonl', '12'), ('after.json', '{}')]:
                (root/name).write_text(text)
            self.assertEqual(experiment_worker.archive_sizes(root),
                             {'archive_bytes': 12, 'jsonl_bytes': 5, 'abc_evidence_bytes': 7})

    def test_process_tree_counts_each_live_pid_once_across_threads(self):
        real_path = Path
        with tempfile.TemporaryDirectory() as directory:
            proc = real_path(directory)
            for pid, rss, tasks in [(101, 10, {'101': '102 103', '104': '102'}),
                                    (102, 20, {'102': '103'}), (103, 30, {'103': '999'})]:
                root = proc / str(pid)
                root.mkdir()
                (root / 'status').write_text(f'Name:\tworker\nVmRSS:\t{rss} kB\n')
                for tid, children in tasks.items():
                    task = root / 'task' / tid
                    task.mkdir(parents=True)
                    (task / 'children').write_text(children)

            def proc_path(value):
                return proc / str(value).removeprefix('/proc/')

            with patch.object(experiment, 'Path', side_effect=proc_path):
                rss, pids = experiment.process_tree(101)
            self.assertEqual(rss, 60)
            self.assertEqual(pids, {101, 102, 103, 999})

    def test_timeout_overrides_partial_success_and_kills_observed_tree_and_group(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'measurement'
            process = MagicMock(pid=1001, returncode=-9)
            process.poll.return_value = None

            def launch(*args, **kwargs):
                (output / 'worker-result.json').write_text(json.dumps({'status': 'passed', 'netlist': 'partial.json'}))
                return process

            with patch.object(experiment.subprocess, 'Popen', side_effect=launch) as popen, \
                    patch.object(experiment, 'process_tree', return_value=(128, {1001, 1002})), \
                    patch.object(experiment.time, 'perf_counter', side_effect=[0, 2, 3]), \
                    patch.object(experiment.os, 'kill') as kill, \
                    patch.object(experiment.os, 'killpg') as killpg, \
                    patch.dict(os.environ, {'YOSYS_FLOW_ABC_TRACE': 'must-not-leak'}):
                result = experiment.measured({'kind': 'full'}, output, timeout=1)
            self.assertEqual(result['status'], 'timeout')
            self.assertEqual(result['failure']['code'], 'measurement_wall_timeout')
            self.assertIsNone(result['netlist'])
            self.assertEqual(result['sampled_process_tree_rss_kib'], 128)
            self.assertEqual(result['measured_wall_seconds'], 3)
            self.assertEqual({call.args[0] for call in kill.call_args_list}, {1001, 1002})
            killpg.assert_called_once_with(1001, experiment.signal.SIGKILL)
            process.wait.assert_called_once()
            environment = popen.call_args.kwargs['env']
            self.assertNotIn('YOSYS_FLOW_ABC_TRACE', environment)
            self.assertEqual(environment['OMP_NUM_THREADS'], '1')
            self.assertTrue(popen.call_args.kwargs['start_new_session'])

    def test_full_method_starts_from_rtl_and_uses_selected_hierarchy_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode in ('retained', 'flatten'):
                out = Path(directory) / mode
                job = {'kind': 'full', 'top': 'benchmark', 'source': '/tmp/complete-new.v',
                       'mode': mode, 'yosys': '/pinned/stock-yosys'}
                with patch.object(experiment_worker.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                        patch.object(experiment_worker.time, 'perf_counter', side_effect=[10, 12, 13, 17]):
                    result = experiment_worker.full(job, out)
                script = (out / 'full.ys').read_text()
                self.assertIn('read_verilog -sv -defer', script)
                self.assertIn('/tmp/complete-new.v', script)
                self.assertNotIn('read_rtlil', script)
                self.assertNotIn('frontend.il', script)
                self.assertIn('synth -top benchmark' + (' -flatten' if mode == 'flatten' else '') + '\n', script)
                self.assertNotIn('check -assert\n', script)
                self.assertIn('check -assert\n', (out / 'post-check.ys').read_text())
                for extension in ('json', 'il', 'v'):
                    self.assertIn('netlist.'+extension, script)
                self.assertEqual(run.call_args.args[0][0], '/pinned/stock-yosys')
                self.assertEqual(result['rtl_to_netlist_seconds'], 2)
                self.assertEqual(result['netlist_generation_seconds'], 2)
                self.assertEqual(result['post_generation_validation_seconds'], 4)
                self.assertEqual(result['input_representation'], 'complete_new_RTL')

    def test_full_post_generation_failure_does_not_become_a_successful_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            job = {'kind': 'full', 'top': 'benchmark', 'source': '/tmp/new.v',
                   'mode': 'retained', 'yosys': '/pinned/stock-yosys'}
            with patch.object(experiment_worker.subprocess, 'run', side_effect=[
                    SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]):
                result = experiment_worker.full(job, Path(directory)/'full')
            self.assertEqual(result['status'], 'failed')
            self.assertIsNone(result['netlist'])
            self.assertGreater(result['netlist_generation_seconds'], 0)

    def test_worker_high_water_is_maximum_individual_process_not_sum(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(experiment_worker, 'full', return_value={'status': 'passed'}), \
                    patch.object(experiment_worker.time, 'perf_counter', side_effect=[0, 2]), \
                    patch.object(experiment_worker.resource, 'getrusage', side_effect=[
                        SimpleNamespace(ru_maxrss=100), SimpleNamespace(ru_maxrss=900)]):
                result = experiment_worker.execute({'kind': 'full'}, Path(directory))
            self.assertEqual(result['max_process_peak_rss_kib'], 900)
            self.assertNotIn('sampled_process_tree_rss_kib', result)
            self.assertEqual(result['worker_seconds'], 2)


class FrozenFixtureTests(unittest.TestCase):
    def test_catalog_retains_negative_cases_and_separates_synthetic_controls(self):
        cases = experiment_cases.CASES
        self.assertEqual(len(cases), 10)
        self.assertEqual(sum(c['family']=='anubis_adapted' for c in cases.values()), 6)
        self.assertEqual(cases['control_local2']['category'], 'functional')
        self.assertEqual(cases['writeback_local10']['expected_change'], 'reject_state')
        self.assertEqual([cases[f'scale_{n}']['unchanged_neighbors'] for n in (2, 8, 32)], [2, 8, 32])
        with tempfile.TemporaryDirectory() as directory:
            for name in cases:
                fixture = experiment_cases.prepare(name, Path(directory)/name)
                manifest = json.loads(Path(fixture['fixture_manifest']).read_text())
                self.assertEqual(manifest['planner_inputs'], 'archived_old_run_and_complete_new_RTL_only')
                root = Path(fixture['fixture_manifest']).parent
                self.assertTrue(experiment.hashes_match({root/p: sha for p, sha in manifest['fixture_inputs'].items()}))
                self.assertTrue(Path(fixture['base_source']).is_file())
                self.assertTrue(Path(fixture['new_source']).is_file())


class PrimaryComparisonSelectionTests(unittest.TestCase):
    def test_primary_default_keeps_both_anubis_functional_cases_including_blocker(self):
        self.assertEqual(experiment_cases.PRIMARY_CASES, ('compare_local9', 'control_local2'))
        self.assertEqual(experiment_cases.select_cases(), ['compare_local9', 'control_local2'])
        for name in experiment_cases.select_cases():
            case = experiment_cases.CASES[name]
            self.assertEqual(case['family'], 'anubis_adapted')
            self.assertEqual(case['category'], 'functional')
            self.assertEqual(case['expected_change'], 'functional_change')

    def test_controls_require_explicit_diagnostic_suite(self):
        from yosys_flow.common import TraceError
        for name in set(experiment_cases.CASES) - set(experiment_cases.PRIMARY_CASES):
            with self.subTest(name=name):
                with self.assertRaisesRegex(TraceError, 'use --suite diagnostic'):
                    experiment_cases.select_cases(requested=[name])
                self.assertEqual(experiment_cases.select_cases('diagnostic', [name]), [name])
        self.assertEqual(experiment_cases.select_cases('diagnostic'), list(experiment_cases.CASES))

    def test_explicit_primary_subset_and_bad_selections(self):
        from yosys_flow.common import TraceError
        self.assertEqual(experiment_cases.select_cases(requested=['control_local2']), ['control_local2'])
        for values in ([], ['unknown'], ['compare_local9', 'compare_local9']):
            with self.subTest(values=values), self.assertRaises(TraceError):
                experiment_cases.select_cases(requested=values)
        with self.assertRaisesRegex(TraceError, 'unknown comparison suite'):
            experiment_cases.select_cases('missing')

    def test_cli_default_scope_disables_precheck_without_changing_auto(self):
        from incremental.__main__ import parser
        compare = parser().parse_args(['compare', '--out', '/tmp/comparison'])
        self.assertEqual(compare.suite, 'anubis-functional')
        self.assertEqual(compare.precheck, 'off')
        self.assertIsNone(compare.cases)
        diagnostic = parser().parse_args(['compare', '--suite', 'diagnostic', '--precheck', 'both',
                                         '--cases', 'bypass_noc1', '--out', '/tmp/diagnostic'])
        self.assertEqual(experiment_cases.select_cases(diagnostic.suite, diagnostic.cases), ['bypass_noc1'])
        auto = parser().parse_args(['auto', '--base-run', '/tmp/base', '--new-source', '/tmp/new.v',
                                   '--out', '/tmp/auto'])
        self.assertFalse(auto.no_equivalence_precheck)


if __name__ == '__main__':
    unittest.main()
