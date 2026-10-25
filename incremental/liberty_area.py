"""Common, post-generation Liberty mapping for area comparison.

This does not implement incremental standard-cell mapping: every delivered
generic design is flattened and mapped using the same ABC command.  Area is
published only after every mapped instance is accounted for and both the JSON
and exported Verilog implementations pass all-output, definedness-aware SAT.
"""
from collections import Counter
import json
import math
from pathlib import Path
import shutil
import subprocess
import time

from yosys_flow.common import (TraceError, digest, identifier, require,
                              require_combinational, token, write_json)
from .equivalence import SUCCESS, UNDEFINED, definedness_miter


RULE = 'common_flattened_liberty_mapping_v1'


def _run(yosys, directory, name, script, timeout):
    (directory/(name+'.ys')).write_text(script+'\n')
    with (directory/(name+'.log')).open('w') as log:
        try:
            result = subprocess.run([yosys, '-Q', '-T', '-s', name+'.ys'],
                                    cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TraceError(f'{name}: Yosys wall timeout ({timeout} seconds)') from exc
    require(result.returncode == 0, f'{name}: Yosys failed; see {name}.log')
    return (directory/(name+'.log')).read_text()


def _ports(module):
    return {name: {'direction': port['direction'], 'width': len(port['bits']),
                   'offset': port.get('offset', 0), 'upto': bool(port.get('upto', 0)),
                   'signed': bool(port.get('signed', 0))}
            for name, port in module['ports'].items()}


def prepare_library(path, directory, yosys, *, unit='library_area_units', provenance=None):
    """Freeze an external Liberty and obtain areas/ports with Yosys's parser.

    Units are not inferred from a technology name.  The caller may supply a unit
    established by pinned library documentation (and record that provenance).
    """
    path = Path(path).resolve()
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    frozen = directory/'library.lib'
    shutil.copyfile(path, frozen)
    original = digest(path)
    require(digest(frozen) == original, 'Liberty changed while freezing input')
    _run(yosys, directory, 'library',
         'read_liberty -lib -ignore_miss_func library.lib\nwrite_json library.json', 300)
    modules = json.loads((directory/'library.json').read_text())['modules']
    cells = {}
    for name, module in modules.items():
        value = module.get('attributes', {}).get('area')
        area = float(value) if value is not None else None
        require(area is None or (math.isfinite(area) and area >= 0),
                f'invalid Liberty area for {name}')
        cells[name] = {'area': area, 'ports': _ports(module)}
    require(cells, 'Liberty contains no imported cells')
    write_json(directory/'cells.json', cells)
    result = {'path': str(frozen), 'sha256': original,
              'cells_path': str(directory/'cells.json'),
              'cells_sha256': digest(directory/'cells.json'),
              'unit': unit, 'cell_types': len(cells), 'source_path': str(path),
              'provenance': provenance or {},
              'area_definition': 'sum of Liberty cell area over instantiated mapped cells'}
    result['frozen_files'] = {str(p): digest(p) for p in directory.iterdir() if p.is_file()}
    write_json(directory/'library-metadata.json', result)
    return result


def _library(library):
    require(digest(library['path']) == library['sha256'], 'frozen Liberty hash mismatch')
    require(digest(library['cells_path']) == library['cells_sha256'],
            'frozen Liberty cell catalog hash mismatch')
    return json.loads(Path(library['cells_path']).read_text())


def _guard(module):
    require_combinational(module)
    require(not module.get('memories') and not module.get('processes'),
            'unlowered memory/process in area proof')
    require(all(p['direction'] in {'input', 'output'} for p in module['ports'].values()),
            'inout interface is outside combinational area proof')
    require(any(p['direction'] == 'output' for p in module['ports'].values()),
            'area proof requires observable outputs')
    require(all(c['type'] not in {'$anyconst', '$anyseq', '$allconst', '$allseq',
                                  '$anyinit', '$assert', '$assume', '$cover', '$live', '$fair'}
                for c in module['cells'].values()), 'formal cells in area proof')


def _account(mapped, top, catalog, statistics):
    """Reject partial mapping and missing area, rather than summing a subset."""
    require(set(mapped['modules']) == {top}, 'mapped export must contain exactly the flattened top')
    module = mapped['modules'][top]
    _guard(module)
    counts = Counter()
    for name, cell in module['cells'].items():
        kind = cell['type']
        require(kind in catalog, f'unmapped or unknown cell {name}: {kind}')
        info = catalog[kind]
        require(info['area'] is not None and math.isfinite(info['area']) and info['area'] >= 0,
                f'missing or invalid area for mapped cell {kind}')
        expected = info['ports']
        require(any(p['direction'] == 'output' for p in expected.values()),
                f'physical-only or outputless cell {name}')
        require(set(cell['connections']) == set(expected), f'incomplete mapped pins on {name}')
        require(not cell.get('parameters'), f'parameterized Liberty cell {name}')
        for pin, port in expected.items():
            require(port['direction'] in {'input', 'output'} and
                    len(cell['connections'][pin]) == port['width'] and
                    cell.get('port_directions', {}).get(pin) == port['direction'],
                    f'invalid mapped pin {name}.{pin}')
        counts[kind] += 1
    total = math.fsum(catalog[kind]['area']*count for kind, count in counts.items())
    stat = statistics['modules'].get('\\'+top, statistics['modules'].get(top))
    require(stat is not None, 'stat did not report the mapped top')
    require(stat.get('num_cells_by_type', {}) == dict(counts) and
            stat['num_cells'] == sum(counts.values()), 'stat and mapped cell census disagree')
    stated_area = stat.get('area', 0 if not counts else None)
    require(stated_area is not None and math.isclose(total, stated_area, abs_tol=1e-6, rel_tol=1e-8),
            'stat area differs from sum of mapped Liberty cell areas')
    require(stat.get('sequential_area', 0) == 0, 'sequential cells in combinational area estimate')
    return total, dict(sorted(counts.items()))


def _verify(directory, top, library, yosys, *, verilog=False, timeout=300):
    """Compare the full mapped implementation with the delivered generic top."""
    name = 'verilog' if verilog else 'mapped'
    lib = token(library['path'])
    reader = 'read_verilog mapped.v' if verilog else 'read_json mapped.json'
    script = (f'read_json generic.json\nrename {top} gold\ndesign -stash gold\n'
              f'read_liberty -ignore_miss_func {lib}\n{reader}\n'
              f'hierarchy -check -top {top}\nflatten -noscopeinfo\ndelete t:$scopeinfo\n'
              f'clean\ncheck -assert\nscc -expect 0\nrename {top} gate\n'
              f'write_json {name}-functional.json\ndesign -stash gate\n'
              'design -copy-from gold gold\ndesign -copy-from gate gate\n'
              'miter -equiv -make_outputs -flatten gold gate miter\n'
              f'hierarchy -check -top miter\ncheck -assert\nwrite_json {name}-miter.json')
    _run(yosys, directory, name+'-prepare', script, timeout)
    gold = json.loads((directory/'generic.json').read_text())['modules'][top]
    gate = json.loads((directory/(name+'-functional.json')).read_text())['modules']['gate']
    _guard(gold)
    _guard(gate)
    ports = _ports(gold)
    require(_ports(gate) == ports, 'mapped interface differs from generic interface')
    miter = definedness_miter(json.loads((directory/(name+'-miter.json')).read_text()), ports)
    write_json(directory/(name+'-checked-miter.json'), miter)
    log = _run(yosys, directory, name+'-prove',
               f'read_json {name}-checked-miter.json\nhierarchy -check -top miter\ncheck -assert\n'
               f'sat -verify -enable_undef -prove trigger 0 -prove {UNDEFINED} 0 '
               f'-set-def-inputs -show-inputs -show-outputs -dump_json {name}-counterexample.json', timeout)
    require(log.splitlines().count(SUCCESS) == 1, 'missing all-output SAT success marker')
    return {'status': 'passed', 'proof_rule': 'all_outputs_equal_and_defined',
            'assumptions': 'all primary inputs binary; no internal signal assumptions',
            'log': str(directory/(name+'-prove.log'))}


def measure(netlist, top, library, directory, yosys, *, timeout=300):
    """Map one successful generic result; failed proofs never yield valid area."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    source = Path(netlist).resolve()
    started = time.perf_counter()
    row = {'status': 'failed', 'rule': RULE, 'cell_area': None, 'cell_count': None,
           'cell_counts': None, 'unit': library.get('unit', 'library_area_units'),
           'mapping_seconds': None, 'validation_seconds': None,
           'source_netlist': str(source), 'library_sha256': library.get('sha256'),
           'mapping_scope': 'whole delivered design flattened for common area assessment',
           'attribute_policy': 'preserve keep and keep_hierarchy; reject residual hierarchy',
           'incremental_standard_cell_reuse': False,
           'constraints': {'delay_target_ps': None, 'output_load_ff': None, 'driving_cell': None},
           'constants': 'literal constants retained; no tie-cell insertion',
           'physical_design_included': False,
           'paths': {'mapped_json': str(directory/'mapped.json'),
                     'mapped_rtlil': str(directory/'mapped.il'),
                     'mapped_verilog': str(directory/'mapped.v'),
                     'stat': str(directory/'stat.json')}}
    phase = 'input'
    phase_started = started
    try:
        identifier(top)
        catalog = _library(library)
        row['source_sha256'] = digest(source)
        shutil.copyfile(source, directory/'input.json')
        require(digest(directory/'input.json') == row['source_sha256'], 'netlist changed while freezing input')
        lib = token(library['path'])
        script = (f'read_json input.json\nhierarchy -check -top {top}\n'
                  'flatten -noscopeinfo\ndelete t:$scopeinfo\nclean\n'
                  f'write_json generic.json\nabc -liberty {lib}\nclean\n'
                  f'read_liberty -lib -ignore_miss_func {lib}\nhierarchy -check -top {top}\n'
                  f'select {top}\nwrite_json -selected mapped.json\n'
                  'write_rtlil -selected mapped.il\nwrite_verilog -selected -noattr mapped.v')
        phase = 'mapping'
        phase_started = started
        _run(yosys, directory, 'mapping', script, timeout)
        row['mapping_seconds'] = time.perf_counter()-phase_started
        phase = 'validation'
        phase_started = time.perf_counter()
        _run(yosys, directory, 'statistics',
             f'read_liberty -lib -ignore_miss_func {lib}\nread_json mapped.json\n'
             f'hierarchy -check -top {top}\ncheck -assert\nselect {top}\n'
             f'tee -o stat.json stat -json -top {top} -liberty {lib}', timeout)
        mapped = json.loads((directory/'mapped.json').read_text())
        area, counts = _account(mapped, top, catalog, json.loads((directory/'stat.json').read_text()))
        frozen = {name: digest(directory/name) for name in
                  ('input.json', 'generic.json', 'mapped.json', 'mapped.il', 'mapped.v', 'stat.json')}
        proofs = {name: _verify(directory, top, library, yosys, verilog=(name == 'verilog'), timeout=timeout)
                  for name in ('mapped', 'verilog')}
        require(all(digest(directory/name) == value for name, value in frozen.items()),
                'area inputs or outputs changed during validation')
        require(digest(source) == row['source_sha256'], 'original generic netlist changed during area assessment')
        _library(library)
        row.update(status='passed', cell_area=area, cell_count=sum(counts.values()),
                   cell_counts=counts, proofs=proofs)
    except (TraceError, OSError, ValueError, KeyError, TypeError) as exc:
        row['failure'] = {'stage': phase, 'code': 'liberty_area_not_verified', 'detail': str(exc)}
    finally:
        if phase in {'mapping', 'validation'} and row[phase+'_seconds'] is None:
            row[phase+'_seconds'] = time.perf_counter()-phase_started
        row['elapsed_seconds'] = time.perf_counter()-started
        row['artifacts'] = {p.name: digest(p) for p in directory.iterdir()
                            if p.is_file() and p.name != 'report.json'}
        write_json(directory/'report.json', row)
    return row
