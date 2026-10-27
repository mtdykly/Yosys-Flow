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
    def test_means_exclude_failures_and_differ_from_medians(self):
        samples = [row('F', value, value, 10) for value in (1, 2, 9)]
        samples.append(row('F', 999, 999, 999, outcome='timeout'))
        summary = experiment.summarize(samples, {})[0]
        self.assertEqual(summary['successful_means']['netlist_generation_seconds'], 4)
        self.assertEqual(summary['successful_medians']['netlist_generation_seconds'], 2)
        self.assertFalse(summary['complete_successful_series'])

    def test_library_mapping_cost_is_separate_and_never_includes_proof(self):
        measurement = {'library_area': {'status': 'passed', 'library_sha256': 'same-library',
                       'unit': 'um^2', 'cell_area': 12.5, 'cell_count': 3,
                       'mapping_seconds': 2, 'validation_seconds': 100}}
        columns = experiment.area_columns(measurement, 5)
        self.assertEqual(columns['mapped_netlist_generation_seconds'], 7)
        self.assertEqual(columns['mapping_validation_seconds'], 100)
        measurement['library_area']['status'] = 'failed'
        failed = experiment.area_columns(measurement, 5)
        self.assertIsNone(failed['mapped_cell_area'])
        self.assertIsNone(failed['mapped_cell_count'])
        self.assertIsNone(failed['mapped_netlist_generation_seconds'])
        self.assertEqual(experiment.area_columns({}, None)['area_status'], 'not_attempted')

    def test_area_ratios_require_matching_library_and_complete_proofs(self):
        full, fine = row('F', 1, 100, 10), row('P-fine', 2, 200, 12)
        def attach(value, area):
            value.update(experiment.area_columns({'library_area': {
                'status': 'passed', 'library_sha256': 'same-library', 'unit': 'um^2',
                'cell_area': area, 'cell_count': 10, 'mapping_seconds': 1,
                'validation_seconds': 100}}, value['netlist_generation_seconds']))
        attach(full, 20)
        attach(fine, 25)
        def summary(rows):
            return next(r for r in experiment.summarize(rows, {}) if r['method'] == 'P-fine')
        result = summary([full, fine])
        self.assertEqual(result['mapped_cell_area_overhead_percent'], 25)
        self.assertEqual(result['mapped_generation_speedup'], 2/3)
        self.assertEqual(result['generation_speedup'], 0.5)
        for field, changed in [('library_sha256', 'another-library'), ('area_unit', 'different-unit'),
                               ('area_status', 'failed'), ('outcome', 'blocked')]:
            altered = dict(fine, **{field: changed})
            result = summary([full, fine, altered])
            self.assertNotIn('mapped_cell_area_overhead_percent', result)
            self.assertNotIn('mapped_generation_speedup', result)
            # With different units/libraries even successful medians are meaningless.
            if field in {'library_sha256', 'area_unit'}:
                self.assertIsNone(result['library_area']['successful_medians']['mapped_cell_area'])
        self.assertNotIn('mapped_cell_area_overhead_percent', summary([fine]))

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
                    ({'status': 'resource_limit', 'failure': {'code': 'measurement_output_byte_limit'}}, 'resource_limit'),
                    ({'status': 'blocked', 'failure': {'code': 'source_correspondence_not_proven'}}, 'blocked'),
                    ({'status': 'failed', 'failure': {'code': 'new_design_equivalence_failed'}}, 'validation_failed'),
                    ({'status': 'failed', 'failure': {'code': 'state_outside_combinational_scope'}}, 'out_of_scope'),
                    ({'status': 'reused_equivalent'}, 'success')]
        for value, expected in controls:
            with self.subTest(value=value):
                self.assertEqual(experiment.outcome(value), expected)


class ExperimentMeasurementTests(unittest.TestCase):
    def test_size_limit_retains_partial_files_and_is_not_a_proof_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'measurement'
            process = MagicMock(pid=1001, returncode=-9)
            process.poll.return_value = None
            def launch(*args, **kwargs):
                (output/'partial.jsonl').write_text('0123456789')
                return process
            with patch.object(experiment.subprocess, 'Popen', side_effect=launch), \
                    patch.object(experiment, 'process_tree', return_value=(128, {1001, 1002})), \
                    patch.object(experiment.time, 'perf_counter', side_effect=[0, 2, 3]), \
                    patch.object(experiment.os, 'kill') as kill, patch.object(experiment.os, 'killpg'):
                result = experiment.measured({'kind': 'baseline', 'archive_byte_limit': 5}, output, 100)
            self.assertEqual(result['status'], 'resource_limit')
            self.assertEqual(experiment.outcome(result), 'resource_limit')
            self.assertEqual(result['failure']['limit_bytes'], 5)
            self.assertGreaterEqual(result['failure']['observed_bytes'], 10)
            self.assertEqual((output/'partial.jsonl').read_text(), '0123456789')
            self.assertEqual({call.args[0] for call in kill.call_args_list}, {1001, 1002})

    def test_directory_size_excludes_symlink_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root/'job'; job.mkdir()
            (job/'actual').write_bytes(b'123')
            (root/'outside').write_bytes(b'1234567890')
            (job/'linked-file').symlink_to(root/'outside')
            (job/'linked-directory').symlink_to(root, target_is_directory=True)
            self.assertEqual(experiment.directory_bytes(job), 3)

    def test_shared_baseline_uses_content_identity_and_does_not_rerun_or_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root/'first.v', root/'second.v'
            first.write_text('same RTL'); second.write_text('same RTL')
            a, b = ({'base_source': str(p), 'top': 'top'} for p in (first, second))
            cache = {}
            with patch.object(experiment, 'measured', return_value={'status': 'resource_limit'}) as measure:
                one = experiment.prepare_baseline('epfl-functional', a, 'retained', root/'original', {}, 1, cache, {})
                two = experiment.prepare_baseline('epfl-functional', b, 'retained', root/'must-not-create', {}, 1, cache, {})
            self.assertIs(one, two)
            self.assertEqual(measure.call_count, 2)  # stock old + native old, once
            self.assertEqual(two['baseline_reference'], str(root/'original'))
            identity = experiment.baseline_identity('epfl-functional', a, 'retained')
            self.assertNotEqual(identity, experiment.baseline_identity('anubis-functional', b, 'retained'))
            self.assertNotEqual(identity, experiment.baseline_identity('epfl-functional', b, 'flatten'))
            self.assertNotEqual(identity, experiment.baseline_identity('epfl-functional', dict(b, top='other'), 'retained'))
            second.write_text('different RTL')
            self.assertNotEqual(identity, experiment.baseline_identity('epfl-functional', b, 'retained'))

    def test_reuse_fractions_use_old_cells_and_missing_values_stay_unknown(self):
        self.assertEqual(experiment.reuse_columns(100, {'removed_cells': 10, 'reused_cells': 90}),
                         {'base_cells': 100, 'removed_fraction': .1, 'reused_fraction': .9})
        for denominator in (None, 0):
            values = experiment.reuse_columns(denominator, {'removed_cells': 10, 'reused_cells': 90})
            self.assertIsNone(values['removed_fraction'])
            self.assertIsNone(values['reused_fraction'])
        self.assertIsNone(experiment.reuse_columns(100, {})['removed_fraction'])

    def test_epfl_schedule_shares_three_baselines_and_keeps_failed_qualification_rows(self):
        from incremental.__main__ import parser
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = [c+'_'+edit for c in ('adder', 'bar', 'arbiter') for edit in ('local', 'shared')]
            cases = {name: {'category': 'functional', 'family': 'epfl_mutated'} for name in names}
            catalog = root/'catalog.json'; catalog.write_text(json.dumps(cases))
            for filename in ('yosys', 'native', 'yosys-abc'):
                (root/filename).write_text(filename)
            args = parser().parse_args(['compare', '--suite', 'epfl-functional', '--repeats', '2',
                                        '--baseline-repeats', '2', '--index-backend', 'gzip',
                                        '--out', str(root/'run')])
            def prepare(name, dest):
                dest.mkdir(parents=True)
                circuit = name.split('_')[0]
                (dest/'base.v').write_text(circuit)
                (dest/'new.v').write_text(name)
                (dest/'fixture.json').write_text('{}')
                return {'top': 'top', 'case': name, 'base_source': str(dest/'base.v'),
                        'new_source': str(dest/'new.v'), 'fixture_manifest': str(dest/'fixture.json'),
                        'baseline_key': circuit}
            calls = []
            def measured(job, dest, timeout):
                calls.append(job)
                dest.mkdir(parents=True)
                result = {'status': 'passed', 'measured_wall_seconds': 1}
                if job['kind'] == 'qualification':
                    if job['fixture']['case'] == 'adder_local':
                        result['status'] = 'failed'
                elif job['kind'] == 'baseline':
                    base = dest/'base-run'; base.mkdir()
                    (base/'manifest.json').write_text(json.dumps({'tool': {'abc': []}}))
                    result.update(base_run=str(base), baseline_stages_seconds={'traced': .5})
                elif job['kind'] == 'incremental':
                    result.update(status='blocked', failure={'code': 'source_correspondence_not_proven'})
                else:
                    result.update(netlist=str(dest/'netlist.json'), rtl_to_netlist_seconds=.5,
                                  netlist_generation_seconds=.5, post_generation_validation_seconds=.1)
                (dest/'measurement.json').write_text(json.dumps(result))
                return result
            with patch.object(experiment, 'suite_definition', return_value=(cases, names, prepare, catalog)), \
                    patch.object(experiment.shutil, 'which', side_effect=lambda value: str(root/('native' if 'native' in value else 'yosys'))), \
                    patch.object(experiment.subprocess, 'check_output', return_value=experiment.PIN), \
                    patch.object(experiment, 'measured', side_effect=measured), \
                    patch('incremental.experiment_metrics.measure_netlist', return_value={'cell_count': 100, 'logic_depth': 8}):
                report = experiment.run(args)
            self.assertEqual(report['methods'], ['F', 'P-fine'])
            self.assertEqual(len(report['measurements']), 24)
            self.assertEqual(report['unique_baseline_count'], 6)
            self.assertEqual(sum(j['kind'] == 'baseline' for j in calls), 6)
            self.assertEqual(sum(j['kind'] == 'stock_old' for j in calls), 6)
            self.assertTrue(all(j['index_backend'] == 'gzip' for j in calls))
            for job in (j for j in calls if j['kind'] == 'incremental'):
                self.assertTrue('trial-1' in job['base_run'] or 'trial-2' in job['base_run'])
            for measurement in report['measurements']:
                if measurement.get('baseline_reference'):
                    self.assertIn('trial-'+str(measurement['repeat']), measurement['baseline_reference'])
            self.assertEqual(sum(j['kind'] == 'qualification' for j in calls), 6)
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(report['baselines']['bar_local/retained']['baseline_reference'],
                             report['baselines']['bar_shared/retained']['baseline_reference'])
            refused = [r for r in report['measurements'] if r['case'] == 'adder_local']
            self.assertEqual(len(refused), 4)
            self.assertTrue(all(r['outcome'] == 'blocked' and
                                r['failure']['code'] == 'edit_qualification_not_proven' for r in refused))
            self.assertEqual(report['counts'], {'success': 10, 'blocked': 14})
            for item in report['summary']:
                if item['case'] == 'adder_local' or item['method'] != 'F':
                    self.assertNotIn('generation_speedup', item)

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
            sizes = experiment_worker.archive_sizes(root)
            self.assertEqual(sizes['archive_bytes'], 12)
            self.assertEqual(sizes['jsonl_bytes'], 5)
            self.assertEqual(sizes['abc_evidence_bytes'], 7)
            self.assertEqual(sizes['raw_record_bytes'], 10)
            self.assertEqual(sizes['index_bytes'], 0)
            (root/'verified-index.sqlite3').write_bytes(b'12345')
            sizes = experiment_worker.archive_sizes(root)
            self.assertEqual(sizes['archive_bytes'], 17)
            self.assertEqual(sizes['index_bytes'], 5)
            self.assertEqual(sizes['index_files'], {'verified-index.sqlite3': 5})

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

    def test_optional_mapping_failure_preserves_valid_generic_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            generic = {'status': 'passed', 'netlist': '/tmp/generic.json',
                       'netlist_generation_seconds': 2, 'post_generation_validation_seconds': 10}
            area = {'status': 'failed', 'cell_area': None, 'mapping_seconds': 1,
                    'validation_seconds': 3}
            with patch.object(experiment_worker, 'full', return_value=generic), \
                    patch('incremental.liberty_area.measure', return_value=area) as mapper:
                result = experiment_worker.execute({'kind': 'full', 'library': {'sha256': 'lib'},
                    'top': 'top', 'yosys': 'yosys'}, Path(directory))
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(result['netlist_generation_seconds'], 2)
            self.assertEqual(result['post_generation_validation_seconds'], 10)
            self.assertEqual(result['library_area'], area)
            mapper.assert_called_once()
            with patch.object(experiment_worker, 'full', return_value=generic), \
                    patch('incremental.liberty_area.measure', side_effect=OSError('area directory unavailable')):
                result = experiment_worker.execute({'kind': 'full', 'library': {'sha256': 'lib'},
                    'top': 'top', 'yosys': 'yosys'}, Path(directory))
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(result['library_area']['failure']['code'], 'area_measurement_error')

    def test_failed_generic_result_is_not_mapped_even_if_file_path_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(experiment_worker, 'full', return_value={
                    'status': 'failed', 'netlist': '/tmp/partial.json'}), \
                    patch('incremental.liberty_area.measure') as mapper:
                experiment_worker.execute({'kind': 'full', 'library': {'sha256': 'lib'}}, Path(directory))
            mapper.assert_not_called()


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
    def test_epfl_defaults_are_single_mode_and_two_methods_with_mandatory_reference(self):
        from incremental.__main__ import parser
        from yosys_flow.common import TraceError
        args = parser().parse_args(['compare', '--suite', 'epfl-functional', '--out', '/tmp/epfl'])
        self.assertEqual(args.mode, 'retained')
        self.assertEqual(experiment.selected_methods(args.suite, args.methods), ['F', 'P-fine'])
        self.assertEqual(experiment.selected_methods('anubis-functional'), ['F', 'P-module', 'P-fine'])
        self.assertEqual(experiment.selected_methods('epfl-functional', ['F', 'P-module']), ['F', 'P-module'])
        self.assertIsNone(args.archive_byte_limit)
        with self.assertRaisesRegex(TraceError, 'include F'):
            experiment.selected_methods('epfl-functional', ['P-fine'])
        with self.assertRaisesRegex(TraceError, 'unique'):
            experiment.selected_methods('epfl-functional', ['F', 'F'])

    def test_suite_catalogs_do_not_allow_cross_suite_cases(self):
        from yosys_flow.common import TraceError
        with self.assertRaises(TraceError):
            experiment.suite_definition('anubis-functional', ['epfl_adder_local'])
        with self.assertRaises(TraceError):
            experiment.suite_definition('epfl-functional', ['compare_local9'])

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
        self.assertIsNone(compare.liberty)
        mapped = parser().parse_args(['compare', '--liberty', '/tmp/cells.lib', '--out', '/tmp/mapped'])
        self.assertEqual(mapped.liberty, '/tmp/cells.lib')
        diagnostic = parser().parse_args(['compare', '--suite', 'diagnostic', '--precheck', 'both',
                                         '--cases', 'bypass_noc1', '--out', '/tmp/diagnostic'])
        self.assertEqual(experiment_cases.select_cases(diagnostic.suite, diagnostic.cases), ['bypass_noc1'])
        auto = parser().parse_args(['auto', '--base-run', '/tmp/base', '--new-source', '/tmp/new.v',
                                   '--out', '/tmp/auto'])
        self.assertFalse(auto.no_equivalence_precheck)


if __name__ == '__main__':
    unittest.main()
