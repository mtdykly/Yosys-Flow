"""Capture checkpoints and continuation into a separate verified archive.

A checkpoint binds bytes, not completed proofs. Resume always replays evidence,
runs the stock reference/proofs, and publishes an index only after validation.
"""
from copy import deepcopy
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from types import SimpleNamespace

from .common import digest, require, token, write_json

CORE = {'yosys.jsonl', 'frontend.il', 'after.il', 'after.json', 'after.v',
        'traced.ys', 'traced.log', 'frontend.d'}


def stamp(path):
    path = Path(path)
    before = path.stat()
    sha = digest(path)
    after = path.stat()
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            f'capture file changed while hashing: {path}')
    return {'sha256': sha, 'size_bytes': after.st_size}


def traced_script(directory, run_id, frontend, pipeline):
    def path(name): return token(Path(directory)/name)
    return (f"flow_start {path('yosys.jsonl')} {run_id}\n{frontend}\nflow_hierarchy\n"
            f"write_rtlil {path('frontend.il')}\n{pipeline}\ncheck -assert\n"
            f"write_json {path('after.json')}\nwrite_rtlil {path('after.il')}\n"
            f"flow_stop\nwrite_verilog {path('after.v')}\n")


def metadata(directory):
    """Bounded-memory scan; full event validation remains NativeTrace's job."""
    directory = Path(directory)
    require(all((directory/name).is_file() and (directory/name).stat().st_size for name in CORE),
            'capture is incomplete: missing native log, netlist, frontend or script')
    regions = []
    with (directory/'yosys.jsonl').open('rb') as stream:
        first = stream.readline()
        header = json.loads(first)
        last = first
        for line in stream:
            last = line
            if b'"abc_region"' in line:
                row = json.loads(line)
                if row.get('record_type') == 'abc_region':
                    regions.append({key:row[key] for key in ('invocation_id','executable','artifacts')})
        footer = json.loads(last)
    require(header.get('record_type') == 'run_begin' and header.get('schema_version') == 2,
            'resume requires native JSONL v2')
    require(footer.get('record_type') == 'run_end', 'capture has no complete run_end')
    first_command = shlex.split((directory/'traced.ys').read_text().splitlines()[0])
    require(len(first_command) == 3 and first_command[0] == 'flow_start' and
            first_command[2] == header['run_id'], 'capture script/run ID mismatch')
    log = Path(first_command[1])
    require(log.is_absolute() and log.name == 'yosys.jsonl', 'unsupported capture script path')
    names = set(CORE)
    for region in regions:
        for name in region['artifacts'].values():
            require(Path(name).name == name and name.startswith('yosys.jsonl.') and
                    (directory/name).is_file() and (directory/name).stat().st_size,
                    'missing or invalid ABC capture artifact')
            names.add(name)
    return {'header':header, 'footer':footer, 'regions':regions,
            'script_directory':str(log.parent), 'files':sorted(names)}


def configuration(value):
    # Index storage does not affect the captured synthesis.
    return {key:val for key,val in value.items() if key != 'index_backend'}


def inputs(meta, config):
    paths = {Path(p).resolve() for p in meta['footer'].get('input_files', [])}
    paths.update(Path(p).resolve(strict=True) for p in config['sources'])
    paths.discard((Path(meta['script_directory'])/'traced.ys').resolve())
    return paths


def bind(directory, manifest, meta, *, previous=None):
    """Bind current bytes, checking saved hashes or the legacy timestamp guard."""
    start = manifest['capture_started_ns']
    expected = {row['path']: row for row in (previous['inputs'] if previous else
                                           manifest.get('inputs', []))}
    paths = inputs(meta, manifest['configuration'])
    if expected:
        require(set(expected) == {str(p) for p in paths}, 'capture input inventory changed')
    roots = {row['path']:row for row in manifest.get('source_inputs_before_capture', [])}
    bound = []
    for path in sorted(paths):
        value = {'path':str(path), **stamp(path)}
        old = expected.get(str(path), roots.get(str(path)))
        if old:
            require(value['sha256'] == old['sha256'] and value['size_bytes'] == old['size_bytes'],
                    f'capture input hash changed: {path}')
        if not expected:
            require(path.stat().st_mtime_ns <= start, f'input changed after capture started: {path}')
        bound.append(value)
    abc = []
    old_tools = {row.get('recorded_executable',row['executable']):row for row in
                 (previous['abc'] if previous else manifest['tool'].get('abc', []))}
    for name in sorted({r['executable'] for r in meta['regions']}):
        binary = shutil.which(name)
        require(binary is not None, f'recorded ABC executable missing: {name}')
        binary = Path(binary).resolve()
        value = {'executable':str(binary), 'recorded_executable':name, **stamp(binary)}
        old = old_tools.get(name)
        if old:
            require(value['sha256'] == old['sha256'], f'ABC executable changed since capture: {name}')
        else:
            require(binary.stat().st_mtime_ns <= start, f'ABC executable changed after capture started: {name}')
        value['version_output'] = subprocess.check_output([str(binary), '-c', 'version'], text=True).strip()
        abc.append(value)
    basis = previous['input_binding_basis'] if previous else (
        'saved_input_hashes' if expected else 'capture_time_guard' if roots else 'legacy_timestamp_guard')
    return {'schema_version':1, 'run_id':manifest['run_id'], 'capture_started_ns':start,
            'configuration':configuration(manifest['configuration']), 'inputs':bound, 'abc':abc,
            'native_sha256':manifest['tool']['native_sha256'], 'script_directory':meta['script_directory'],
            'input_binding_basis':basis,
            'artifacts':{name:stamp(Path(directory)/name) for name in meta['files']}}


def prepare(directory, manifest, frontend, pipeline, progress):
    """Validate a capture checkpoint (or legacy capture) before copying it."""
    directory = Path(directory)
    progress('capture_inventory')
    meta = metadata(directory)
    require(meta['header']['run_id'] == manifest['run_id'], 'capture run ID differs from manifest')
    require((directory/'traced.ys').read_text() == traced_script(
        meta['script_directory'], manifest['run_id'], frontend, pipeline),
        'capture script differs from recorded configuration')
    previous = None
    descriptor = manifest.get('capture')
    if descriptor:
        require(descriptor['artifact'] == 'capture.json' and digest(directory/'capture.json') == descriptor['sha256'],
                'capture checkpoint hash mismatch')
        previous = json.loads((directory/'capture.json').read_text())
        require(previous['schema_version'] == 1 and previous['run_id'] == manifest['run_id'] and
                previous['configuration'] == configuration(manifest['configuration']) and
                previous['native_sha256'] == manifest['tool']['native_sha256'] and
                previous['capture_started_ns'] == manifest['capture_started_ns'] and
                previous['script_directory'] == meta['script_directory'], 'capture checkpoint binding mismatch')
        require(set(previous['artifacts']) == set(meta['files']), 'capture checkpoint artifact inventory differs')
    progress('capture_binding')
    checkpoint = bind(directory, manifest, meta, previous=previous)
    for name, value in checkpoint['artifacts'].items():
        expected = previous['artifacts'][name] if previous else manifest.get('artifacts', {}).get(name)
        if expected:
            require(value == expected, f'capture artifact hash changed: {name}')
    return meta, checkpoint


def publish_checkpoint(directory, manifest, checkpoint):
    directory = Path(directory)
    write_json(directory/'capture.json', checkpoint)
    manifest['inputs'] = checkpoint['inputs']
    manifest['tool']['abc'] = checkpoint['abc']
    manifest['capture'] = {'artifact':'capture.json', 'sha256':digest(directory/'capture.json')}
    write_json(directory/'manifest.json', manifest)


def copy_capture(source, destination, checkpoint, progress):
    """Independent copies: writes in a resumed run cannot change the original."""
    progress('capture_copy')
    for name, expected in checkpoint['artifacts'].items():
        original, target = Path(source)/name, Path(destination)/name
        shutil.copy2(original, target)
        require(stamp(target) == expected and stamp(original) == expected,
                f'capture changed during copy: {name}')
        progress('capture_copy', artifact=name)


def resume(args):
    directory = Path(args.directory).resolve(strict=True)
    out = Path(args.out).resolve()
    require(not out.is_relative_to(directory), 'resume output must be outside the original capture directory')
    require(not out.exists(), f'output directory already exists: {out}')
    manifest = json.loads((directory/'manifest.json').read_text())
    require(manifest.get('schema_version') == 2 and manifest.get('input_scope') == 'combinational',
            'resume requires a native combinational capture')
    require(manifest.get('status') in {'running','failed','complete'}, 'unsupported capture status')
    require(not manifest.get('resume') or manifest.get('capture'),
            'previous resume did not finish copying its capture; resume from the original directory')
    config = manifest['configuration']
    # Legacy manifests did not save a start time. The script was written before
    # invoking the recorder; copy2 preserves this conservative cutoff on retries.
    manifest = deepcopy(manifest)
    manifest.setdefault('capture_started_ns', (directory/'traced.ys').stat().st_mtime_ns)
    native = args.native_yosys or manifest['tool']['native_executable']
    binary = shutil.which(native)
    require(binary is not None and digest(binary) == manifest['tool']['native_sha256'],
            'native executable differs from capture; use the original binary')
    options = SimpleNamespace(flow=manifest['flow'], index_backend=args.index_backend or config.get('index_backend','sqlite'),
        out=str(out), native_yosys=native, yosys=args.yosys or manifest['tool']['stock_executable'],
        source=config['sources'], include=config['include_dirs'], define=config['defines'],
        param=config['parameters'], top=config['top'], flatten=config.get('flatten',False),
        skip_stock_equivalence=getattr(args,'skip_stock_equivalence',False),
        prepare_evidence=getattr(args,'prepare_evidence',False),evidence_out=getattr(args,'evidence_out',None),
        evidence_cec_timeout=getattr(args,'evidence_cec_timeout',60),source_timeout=getattr(args,'source_timeout',600))
    from .native import run_native
    return run_native(options, resume_capture=(directory, manifest))
