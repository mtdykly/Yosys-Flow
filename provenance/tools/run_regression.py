"""Run the complete suite and retain a report; skipped integration tests fail."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import uuid

PROVENANCE_ROOT = Path(__file__).resolve().parents[1]
ROOT = PROVENANCE_ROOT.parent
sys.path.insert(0, str(ROOT))


def command_output(args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return {'returncode': result.returncode, 'output': result.stdout.strip(), 'stderr': result.stderr.strip()}


def test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, help='new report directory (default: unique directory under runs/)')
    args = parser.parse_args()
    out = args.out or ROOT/'runs'/('regression-'+uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    suite = unittest.defaultTestLoader.discover(str(PROVENANCE_ROOT/'tests'))
    suite.addTests(unittest.TestLoader().discover(str(ROOT/'incremental/tests'),top_level_dir=str(ROOT)))
    ids = list(test_ids(suite))
    started = time.perf_counter()
    # Keep external experiment artifacts alongside the regression report.
    # unittest on its own still uses temporary directories.
    destinations={'YOSYS_FLOW_ANUBIS_RESULTS':out/'anubis','YOSYS_FLOW_AUTO_RESULTS':out/'automatic',
                  'YOSYS_FLOW_STATEMENT_RESULTS':out/'statements',
                  'YOSYS_FLOW_EQUIVALENCE_RESULTS':out/'equivalence',
                  'YOSYS_FLOW_NOCHANGE_RESULTS':out/'nochange',
                  'YOSYS_FLOW_SEQUENTIAL_RESULTS':out/'sequential'}
    previous={key:os.environ.get(key) for key in destinations}
    for key,path in destinations.items():os.environ[key]=str(path.resolve())
    try:
        with (out/'tests.log').open('w') as log:
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    finally:
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
    complete = bool(ids) and result.wasSuccessful() and not result.skipped
    binaries = {'native': os.environ.get('YOSYS_FLOW_NATIVE', str(ROOT/'build/native/install/bin/yosys')),
                'stock': shutil.which('yosys'), 'abc': shutil.which('yosys-abc'),
                'abc_internal': str(ROOT/'build/abc/install/bin/yosys-abc')}
    versions = {name: command_output([binary, '-c', 'version'] if name.startswith('abc') else [binary, '-V'])
                for name, binary in binaries.items() if binary and Path(binary).is_file()}
    report = {'status': 'passed' if complete else 'failed',
              'input_scope': 'combinational provenance/incremental; independent single-clock sequential precheck',
              'timestamp_utc': datetime.now(timezone.utc).isoformat(),
              'elapsed_seconds': time.perf_counter()-started, 'command': [sys.executable, *sys.argv],
              'commit': command_output(['git', 'rev-parse', 'HEAD'])['output'],
              'working_tree': command_output(['git', 'status', '--short'])['output'],
              'tools': versions, 'discovered_tests': ids,
              'counts': {'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
                         'skipped': len(result.skipped), 'expected_failures': len(result.expectedFailures),
                         'unexpected_successes': len(result.unexpectedSuccesses)},
              'failures': [{'test': test.id(), 'traceback': tb} for test, tb in result.failures],
              'errors': [{'test': test.id(), 'traceback': tb} for test, tb in result.errors],
              'skipped': [{'test': test.id(), 'reason': why} for test, why in result.skipped],
              'anubis_experiment_report': str((out/'anubis/report.json').resolve()),
              'automatic_experiment_directory': str((out/'automatic').resolve()),
              'statement_experiment_directory': str((out/'statements').resolve()),
              'equivalence_experiment_directory': str((out/'equivalence').resolve()),
              'nochange_experiment_directory': str((out/'nochange').resolve()),
              'sequential_experiment_directory': str((out/'sequential').resolve()),
              'acceptance_layers': {
                  'record_consistency': ['test_native', 'test_synth', 'test_cli', 'test_hierarchy',
                                         'test_persistent_index', 'test_sqlite_index', 'test_abc_delta'],
                  'source_selection': ['test_process', 'test_selection', 'test_abc_internal', 'test_abc_factoring',
                                       'test_boolean', 'test_abc_scale', 'test_diagnostics', 'test_contracts'],
                  'region_contracts': ['test_contracts', 'test_plans', 'test_hierarchy'],
                  'hierarchical_instances': ['test_hierarchy'],
                  'local_replacement_experiment': ['incremental.tests.test_replacement'],
                  'anubis_external_edits': ['incremental.tests.test_anubis'],
                  'automatic_difference_extraction_and_binding': ['incremental.tests.test_automatic',
                                                                 'incremental.tests.test_statements'],
                  'controlled_comparison_accounting': ['incremental.tests.test_experiment',
                                                      'incremental.tests.test_experiment_metrics'],
                  'equivalence_precheck_and_whole_netlist_reuse': ['incremental.tests.test_equivalence'],
                  'anubis_nochange_inventory_and_proof_measurements': ['incremental.tests.test_nochange',
                                                                    'incremental.tests.test_nochange_full',
                                                                    'incremental.tests.test_nochange_top'],
                  'sequential_precheck_and_whole_netlist_reuse': ['incremental.tests.test_sequential'],
                  'export_correctness': ['test_export'],
                  'input_contract': ['test_combinational']},
              'note': 'Suite-to-layer mapping; passing tests does not prove complete provenance coverage.'}
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(f"{report['status']}: {result.testsRun} tests, {len(result.skipped)} skipped; report: {out/'report.json'}")
    if not complete:
        print((out/'tests.log').read_text()[-10000:])
    return 0 if complete else 1


if __name__ == '__main__':
    raise SystemExit(main())
