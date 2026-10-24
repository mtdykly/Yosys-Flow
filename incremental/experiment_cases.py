"""Frozen comparison fixtures; the algorithm receives only complete RTL inputs.

ANUBIS-derived cases retain the original adapter's declared modifications.
Synthetic scale/sharing controls are reported separately from ANUBIS cases.
There are no source selectors, pre-extracted patches or connection hints here.
"""
from copy import deepcopy
import json
from pathlib import Path

from yosys_flow.common import ROOT, digest, require, write_json
from . import anubis


MANIFEST = ROOT / 'benchmarks/anubis/comparison-cases.json'
CATALOG = json.loads(MANIFEST.read_text())
CASES = {row['name']: row for row in CATALOG['cases']}
require(len(CASES) == len(CATALOG['cases']), 'duplicate comparison case names')
PRIMARY_CASES = tuple(CATALOG['suites']['anubis-functional']['cases'])
require(all(CASES[name]['family'] == 'anubis_adapted' and
            CASES[name]['category'] == 'functional' and
            CASES[name]['expected_change'] == 'functional_change'
            for name in PRIMARY_CASES), 'primary suite must contain ANUBIS combinational functional edits')


def select_cases(suite='anubis-functional', requested=None):
    """Keep performance cases separate from explicitly requested diagnostics.

    Eligibility is frozen before measurement. A source-proof failure or other
    unsuccessful result must never remove an eligible case from this selection.
    """
    require(suite in CATALOG['suites'], 'unknown comparison suite: ' + str(suite))
    allowed = CATALOG['suites'][suite]['cases']
    names = list(allowed if requested is None else requested)
    require(bool(names), 'comparison requires at least one case')
    require(len(names) == len(set(names)) and set(names) <= CASES.keys(),
            'unknown or repeated comparison cases')
    require(set(names) <= set(allowed),
            'cases outside ' + suite + '; use --suite diagnostic for NOC, synthetic or state-negative controls')
    return names


def _leaf(name, width, changed=False):
    operation = '|' if changed else '&'
    return '\n'.join([
        f'module {name}(input [{width-1}:0] a, b, c, d,',
        f'                   output [{width-1}:0] y, side);',
        f'  assign y = a {operation} b;',
        '  assign side = c ^ d;',
        'endmodule',
        '',
    ])


def _top(case, changed):
    width = case['width']
    neighbors = case['unchanged_neighbors']
    shared = case['variant'] == 'shared_inputs_and_dependent_output'
    ports = [f'input [{width-1}:0] target_a, target_b, target_c, target_d',
             f'output [{width-1}:0] target_y, target_side']
    if shared:
        ports.append(f'output [{width-1}:0] other_y, other_side, dependent')
    else:
        packed = width * neighbors
        ports += [f'input [{packed-1}:0] neighbor_a, neighbor_b, neighbor_c, neighbor_d',
                  f'output [{packed-1}:0] neighbor_y, neighbor_side']
    lines = ['module benchmark(\n  ' + ',\n  '.join(ports) + '\n);']
    target = 'comparison_edited' if changed else 'comparison_leaf'
    lines += [f'  {target} u_target(.a(target_a), .b(target_b), .c(target_c), .d(target_d),',
              '      .y(target_y), .side(target_side));']
    if shared:
        lines += ['  comparison_leaf u_other(.a(target_a), .b(target_b), .c(target_c), .d(target_d),',
                  '      .y(other_y), .side(other_side));',
                  '  assign dependent = target_y ^ other_y;']
    else:
        # Generate source text, not RTL generate/loop syntax outside our scope.
        for index in range(neighbors):
            high, low = width * (index + 1) - 1, width * index
            bindings = ', '.join(f'.{port}(neighbor_{port}[{high}:{low}])'
                                 for port in ('a', 'b', 'c', 'd', 'y', 'side'))
            lines.append(f'  comparison_leaf u_neighbor_{index}({bindings});')
    return '\n'.join([*lines, 'endmodule', ''])


def _synthetic(case, directory):
    directory.mkdir(parents=True, exist_ok=False)
    width = case['width']
    base_leaf = _leaf('comparison_leaf', width)
    (directory / 'base.v').write_text(base_leaf + _top(case, False))
    (directory / 'new.v').write_text(base_leaf + _leaf('comparison_edited', width, True) + _top(case, True))
    source_motifs = ['provenance/examples/multilevel.v',
                     'provenance/examples/factoring.v']
    adaptations = [
        'Synthetic 4-bit AND/OR and XOR motifs; not an ANUBIS macro or upstream design.',
        'Only target y changes from bitwise AND to bitwise OR; target side remains independent XOR.',
        'All target and neighbor outputs are top outputs so unchanged neighbors remain observable.',
        'Instances are generated as explicit Verilog source lines; no RTL generate or loop constructs.',
    ]
    if case['variant'] == 'shared_inputs_and_dependent_output':
        adaptations.append('Two instances share all inputs; both output sets and their y XOR are observable. Flattening can merge old gates and collapse the dependent output.')
    else:
        adaptations.append('Neighbor operands are independent top inputs; unchanged_neighbors excludes the edited target instance.')
    return {
        'schema_version': 1,
        'scope': 'synthetic_combinational_scaling_or_sharing_control',
        'original_ANUBIS_case_claimed': False,
        'original_historical_commit_pair_claimed': False,
        'motif_references_sha256': {name: digest(ROOT / name) for name in source_motifs},
        'adaptations': adaptations,
        'fixture_inputs': {p.name: digest(p) for p in sorted(directory.iterdir()) if p.is_file()},
    }


def prepare(name, directory):
    """Create a frozen fixture and return full source paths plus its manifest.

    The returned manifest describes experimental strata. It is not an input to
    the automatic selection algorithm, and expected_change is not a verdict.
    """
    require(name in CASES, 'unknown comparison case: ' + str(name))
    case = deepcopy(CASES[name])
    directory = Path(directory).resolve()
    require(not directory.exists(), 'refusing to overwrite fixture: ' + str(directory))
    if case['generator'] == 'anubis':
        source_case = anubis.CASES[case['source_case']]
        require(case['expected_change'] == source_case['expect'] and case['macro'] == source_case['macro'],
                'comparison catalog changes the ANUBIS adapter classification')
        anubis.prepare(case['source_case'], directory)
        fixture = json.loads((directory / 'fixture.json').read_text())
    else:
        require(case['generator'] == 'synthetic', 'unsupported comparison generator')
        require(case['variant'] in {'independent_neighbors', 'shared_inputs_and_dependent_output'},
                'unsupported synthetic fixture variant')
        require(case['width'] == 4 and case['unchanged_neighbors'] in {1, 2, 8, 32},
                'unsupported synthetic fixture size')
        fixture = _synthetic(case, directory)
    fixture.update(comparison_suite=CATALOG['suite'], comparison_case=case,
                   comparison_catalog_sha256=digest(MANIFEST),
                   comparison_generator_sha256=digest(__file__),
                   planner_inputs='archived_old_run_and_complete_new_RTL_only',
                   expected_change_is_not_measured_verdict=True)
    write_json(directory / 'fixture.json', fixture)
    return {'top': 'benchmark', 'base_source': str(directory / 'base.v'),
            'new_source': str(directory / 'new.v'),
            'fixture_manifest': str(directory / 'fixture.json')}
