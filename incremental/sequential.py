"""Independent, fail-closed sequential EQY precheck and whole-netlist reuse.

This does not enable sequential provenance or local replacement. The new RTL is
the specification (gold), the archived implementation is gate. Only an unbounded
proof of every output, plus output definedness, authorizes delivery.
"""
import copy
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

from yosys_flow.common import PIN, TraceError, digest, identifier, require, token, write_json

SCHEMA = 'sequential-baseline-v1'
UNDEFINED = '__flow_seq_undefined'
WRAPPER = '__flow_seq_wrapper'
FORMAL = {'$anyconst', '$anyseq', '$allconst', '$allseq', '$anyinit', '$assert',
          '$assume', '$cover', '$live', '$fair', '$check', '$initstate'}


def terminate_session(process):
    """EQY/SBY create separate process groups, but retain our Linux session.

    Freeze groups before killing them so a solver launcher cannot leave a new
    child behind. Never signal the caller's session or an unrelated tool run.
    """
    session = process.pid  # execute() uses start_new_session=True
    groups = {session}
    while True:
        for group in groups:
            try:
                os.killpg(group, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        discovered = set()
        for path in Path('/proc').glob('[0-9]*'):
            try:
                pid = int(path.name)
                if os.getsid(pid) == session:
                    discovered.add(os.getpgid(pid))
            except (ProcessLookupError, PermissionError):
                pass
        if discovered <= groups:
            break
        groups |= discovered
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def execute(argv, directory, name, deadline):
    """Wall budget includes children; kill the complete EQY/SBY/solver group."""
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        return 'timeout'
    with (directory/(name+'.log')).open('w') as log:
        process = subprocess.Popen(argv, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            terminate_session(process)
            log.write('\nWALL TIMEOUT: terminated all tool process groups in the session\n')
            return 'timeout'
        except BaseException:
            terminate_session(process)
            raise
    return 'ok' if code == 0 else 'tool_error'


def yosys(binary, directory, name, script, deadline):
    (directory/(name+'.ys')).write_text(script+'\n')
    result = execute([binary, '-Q', '-T', '-s', name+'.ys'], directory, name, deadline)
    require(result == 'ok', f'{name}: {result}; see {directory/(name+".log")}')


def toolchain(args):
    require(Path('/proc/self/stat').is_file(), 'sequential process supervision currently requires Linux /proc')
    result = {}
    for key, name in [('yosys', args.yosys), ('eqy', args.eqy), ('sby', 'sby'),
                      ('solver', 'yices-smt2'), ('smtbmc', 'yosys-smtbmc'), ('abc', 'yosys-abc')]:
        path = shutil.which(name)
        require(path, 'missing sequential proof tool: '+name)
        path = str(Path(path).resolve())
        require(re.fullmatch(r'[A-Za-z0-9_./+\-]+', path), 'tool path is unsafe for EQY shell scripts')
        result[key] = {'path':path, 'sha256':digest(path)}
    for key in ('yosys', 'eqy', 'sby'):
        flag = '-V' if key == 'yosys' else '--version'
        proc = subprocess.run([result[key]['path'], flag], capture_output=True, text=True, timeout=10)
        require(proc.returncode == 0, 'cannot identify '+key)
        result[key]['version'] = proc.stdout.strip()
    require(PIN in result['yosys']['version'], 'sequential baseline requires pinned Yosys '+PIN)
    return result


def fingerprint(root):
    return {str(p.relative_to(root)):digest(p) for p in sorted(root.rglob('*')) if p.is_file()}


def verify_files(root, files):
    require(files, 'empty sequential archive evidence')
    for name, expected in files.items():
        path = root/name
        require(path.resolve().is_relative_to(root.resolve()), 'invalid sequential archive path')
        require(path.is_file() and digest(path) == expected, 'sequential evidence checksum mismatch: '+name)


def interface(module):
    return {name:{'direction':p['direction'], 'width':len(p['bits']),
                  'offset':p.get('offset', 0), 'upto':p.get('upto', 0), 'signed':p.get('signed', 0)}
            for name,p in module['ports'].items()}


def guard(module, config):
    """Validate clocks before EQY's formalff can abstract them away."""
    ports = module['ports']
    for name in ports:
        identifier(name)
        require(not name.startswith('__flow_seq_'), 'reserved sequential proof port')
    require(all(p['direction'] in {'input', 'output'} for p in ports.values()), 'inout is unsupported')
    require(any(p['direction'] == 'output' for p in ports.values()), 'no outputs to compare')
    clock = ports.get(config['clock'], {})
    require(clock.get('direction') == 'input' and len(clock['bits']) == 1, 'clock must be a scalar input')
    clk = clock['bits'][0]
    require(type(clk) is int, 'constant clock is unsupported')
    reset = config.get('reset')
    if reset:
        p = ports.get(reset, {})
        require(p.get('direction') == 'input' and len(p['bits']) == 1 and p['bits'] != [clk],
                'startup reset must be a separate scalar input')
    initial = {}
    for wire in module['netnames'].values():
        init = wire.get('attributes', {}).get('init', '')
        for bit,value in zip(wire['bits'], reversed(init)):
            if value in '01':
                require(bit not in initial or initial[bit] == value, 'conflicting initialization')
                initial[bit] = value
    states = set()
    for name,cell in module['cells'].items():
        kind = cell['type']
        require(kind not in FORMAL, 'formal or nondeterministic RTL is unsupported')
        require(kind.startswith('$'), 'unresolved or black-box cell: '+kind)
        require(kind == '$dff' or not kind.lower().lstrip('$_').startswith(
            ('dff', 'adff', 'aldff', 'sdff', 'dlatch', 'adlatch', 'sr', 'ff', 'mem')),
            'only synchronous positive-edge FFs are supported: '+kind)
        if kind == '$dff':
            require(cell['connections']['CLK'] == [clk] and int(cell['parameters']['CLK_POLARITY'], 2) == 1,
                    'multiple, derived, or negative-edge clocks are unsupported')
            states.update(cell['connections']['Q'])
        for port,bits in cell['connections'].items():
            require(clk not in bits or (kind == '$dff' and port == 'CLK'),
                    'clock used as data or combinational logic is unsupported')
    require(states, 'no sequential state; use the combinational precheck')
    require(all(clk not in p['bits'] for p in ports.values() if p['direction'] == 'output'),
            'clock as an output is unsupported')
    require(reset or states <= initial.keys(),
            'state lacks explicit initialization; specify a synchronous startup reset')
    return {'state_bits':len(states), 'initialized_state_bits':len(states & initial.keys()),
            'ports':interface(module)}


def frontend(args, config, directory, binary, deadline):
    """Freeze preprocessed text, then elaborate that exact snapshot (no synth)."""
    started = time.monotonic()
    directory.mkdir()
    sources = [Path(p).resolve() for p in args.source]
    before = {str(p):digest(p) for p in sources}
    defines = getattr(args, 'define', []) or []
    includes = [str(Path(p).resolve()) for p in (getattr(args, 'include', []) or [])]
    for define in defines:
        require(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:=[A-Za-z0-9_\x27]+)?', define), 'invalid -D definition')
    for path in includes:
        require(not re.search(r'[\s;"\\]', path), 'include directory must not contain script metacharacters')
    flags = ''.join(' -D'+d for d in defines)+''.join(' -I'+p for p in includes)
    yosys(binary, directory, 'preprocess', 'read_verilog -sv -defer -ppdump'+flags+' '+
          ' '.join(token(p) for p in sources), deadline)
    dumps = re.findall(r'-- Verilog code after preprocessor --\n(.*?)-- END OF DUMP --',
                       (directory/'preprocess.log').read_text(), re.S)
    require(dumps, 'missing preprocessed RTL snapshot')
    (directory/'snapshot.v').write_text('\n'.join(dumps))
    require(all(digest(p) == h for p,h in before.items()), 'source changed while preprocessing')
    params = config['parameters']
    options = ''.join(f' -chparam {identifier(k)} {v}' for k,v in params.items())
    yosys(binary, directory, 'frontend', 'read_verilog -sv -defer snapshot.v\nhierarchy -check -top '+
          config['top']+options+'\nwrite_rtlil frontend.il\nproc\nflatten\nmemory_map\nopt_clean\n'+
          'check -assert\nwrite_rtlil coarse.il\nwrite_json coarse.json', deadline)
    design = json.loads((directory/'coarse.json').read_text())
    require(set(design['modules']) == {config['top']}, 'proof requires a resolved flattened design')
    scope = guard(design['modules'][config['top']], config)
    return {'original_sources':before, 'defines':defines, 'includes':includes,
            'elapsed_seconds':time.monotonic()-started, **scope}


def reset_wrapper(ports, config):
    """Proof-only harness: force startup reset, compare only after its release."""
    reset, cycles = config['reset'], config['reset_cycles']
    lines = ['module '+WRAPPER+'('+','.join(ports)+');']
    connections = []
    for name,p in ports.items():
        width = p['width']
        lines.append(f'{p["direction"]} [{width-1}:0] {name};')
        signal_name = name
        if p['direction'] == 'output':
            signal_name = '__observed_'+name
            lines += [f'wire [{width-1}:0] {signal_name};',
                      f'assign {name} = __ready ? {signal_name} : {width}\'b0;']
        elif name == reset:
            signal_name = '__reset'
        connections.append('.'+name+'('+signal_name+')')
    width = max(1, cycles.bit_length())
    lines += [f'reg [{width-1}:0] __count = 0;', f'wire __ready = (__count == {cycles});',
              f'wire __reset = __ready ? {reset} : 1\'b{config["reset_active"]};',
              f'always @(posedge {config["clock"]}) if (!__ready) __count <= __count + 1\'b1;',
              config['top']+' __dut('+','.join(connections)+');', 'endmodule']
    return '\n'.join(lines)+'\n'


def proof_design(source, config, ports, binary, directory, side, deadline):
    script = 'read_rtlil '+token(source)+'\n'
    top = config['top']
    if config.get('reset'):
        (directory/'startup.v').write_text(reset_wrapper(ports, config))
        script += 'read_verilog startup.v\nhierarchy -top '+WRAPPER+'\nproc\nflatten\n'
        top = WRAPPER
    script += 'hierarchy -top '+top+'\nrename '+top+' proof_top\nopt_clean\nwrite_json '+side+'.json'
    yosys(binary, directory, side, script, deadline)
    return json.loads((directory/(side+'.json')).read_text())


def definedness(design, gold, startup_reset=False):
    """Expose X on any observed gold output as a *defined* failed obligation."""
    design = copy.deepcopy(design)
    module = design['modules']['proof_top']
    require(UNDEFINED not in module['netnames'] and UNDEFINED not in module['ports'] and
            not any(n.startswith(UNDEFINED+'$') for n in module['cells']),
            'reserved output-definedness helper name in design')
    outputs = [b for p in module['ports'].values() if p['direction'] == 'output' for b in p['bits']]
    require(outputs, 'empty output-definedness obligation')
    module['ports'] = {n:p for n,p in module['ports'].items() if p['direction'] == 'input'}
    bits = [b for n in module['netnames'].values() for b in n['bits'] if type(b) is int]
    state = [b for c in module['cells'].values() if c['type'] == '$dff' for b in c['connections']['Q']]
    next_bit = max(bits, default=1)+1
    if gold and state:
        ready = module['netnames'].get('__ready') if startup_reset else None
        if ready:
            masked = list(range(next_bit, next_bit+len(state)))
            next_bit += len(state)
            module['cells'][UNDEFINED+'$state_mask'] = {'type':'$mux', 'hide_name':1, 'attributes':{},
                'parameters':{'WIDTH':format(len(state), '032b')},
                'port_directions':{'A':'input','B':'input','S':'input','Y':'output'},
                'connections':{'A':['0']*len(state), 'B':state, 'S':ready['bits'], 'Y':masked}}
            state = masked
        # A stronger, inductive invariant also rules out latent unknown state
        # that a different read address could expose in a later cycle.
        outputs += state
    reduced,flag = next_bit, next_bit+1
    module['ports'][UNDEFINED] = {'direction':'output', 'bits':[flag] if gold else ['0']}
    module['netnames'][UNDEFINED] = {'hide_name':0, 'bits':[flag] if gold else ['0'], 'attributes':{}}
    if gold:
        module['cells'][UNDEFINED+'$reduce'] = {'type':'$reduce_xor', 'hide_name':1, 'attributes':{},
            'parameters':{'A_SIGNED':'0', 'A_WIDTH':format(len(outputs), '032b'), 'Y_WIDTH':format(1, '032b')},
            'port_directions':{'A':'input', 'Y':'output'}, 'connections':{'A':outputs, 'Y':[reduced]}}
        module['cells'][UNDEFINED+'$check'] = {'type':'$eqx', 'hide_name':1, 'attributes':{},
            'parameters':{'A_SIGNED':'0', 'B_SIGNED':'0', 'A_WIDTH':format(1, '032b'),
                          'B_WIDTH':format(1, '032b'), 'Y_WIDTH':format(1, '032b')},
            'port_directions':{'A':'input', 'B':'input', 'Y':'output'},
            'connections':{'A':[reduced], 'B':['x'], 'Y':[flag]}}
    return design


def eqy(gold, gate, directory, tools, deadline, strengthen=False, engine='smtbmc yices'):
    directory.mkdir()
    write_json(directory/'gold.json', gold)
    write_json(directory/'gate.json', gate)
    g, t = gold['modules']['proof_top'], gate['modules']['proof_top']
    require(interface(g) == interface(t), 'proof interface mismatch')
    outputs = {n:set(range(len(p['bits']))) for n,p in g['ports'].items() if p['direction'] == 'output'}
    names = list(g['ports'])
    auxiliary = []
    if strengthen:
        # EQY proves these state matches in their own partitions. Acceptance
        # requires every partition and every dependency, not just output cones.
        state_bits = {b for c in g['cells'].values() if c['type'] == '$dff' for b in c['connections']['Q']}
        auxiliary = [n for n,w in g['netnames'].items() if not w.get('hide_name', 0)
                     and w['bits'] and set(w['bits']) <= state_bits
                     and n in t['netnames'] and n not in names
                     and len(w['bits']) == len(t['netnames'][n]['bits'])]
        names += auxiliary
    config = '[gold]\nread_json gold.json\nhierarchy -top proof_top\n'+\
             '[gate]\nread_json gate.json\nhierarchy -top proof_top\n'+\
             '[match proof_top]\nnodefault\n'+\
             '\n'.join('gold-match /^'+re.escape(n)+'$/' for n in names)+\
             ('\n' if auxiliary else '\n[collect proof_top]\ngroup *\n')+\
             '[strategy induction]\nuse sby\n'+\
             'engine '+engine+'\ndepth 3\ntimeout '+str(max(1, math.ceil(deadline-time.monotonic())))+'\n'
    (directory/'proof.eqy').write_text(config)
    started = time.monotonic()
    outcome = execute([tools['eqy']['path'], '--yosys', tools['yosys']['path'],
                       '-j', '1', '-d', 'work', 'proof.eqy'], directory, 'eqy', deadline)
    row = {'status':'unknown', 'reason':outcome, 'elapsed_seconds':time.monotonic()-started,
           'engine':engine, 'auxiliary_invariants':auxiliary, 'all_outputs_covered':False, 'internal_cut_inputs':False}
    work = directory/'work'
    partitions = sorted((work/'partitions').glob('*.json'))
    if outcome == 'timeout' or not partitions or (not auxiliary and len(partitions) != 1):
        row['reason'] = 'timeout' if outcome == 'timeout' else 'missing_or_non_whole_partition'
        return row
    partitions = [json.loads(p.read_text())['partition'] for p in partitions]
    listed = work/'partition.list'
    expected = {line.split()[1] for line in listed.read_text().splitlines() if line.strip()} if listed.is_file() else set()
    if not expected or expected != {p['name'] for p in partitions}:
        row['reason'] = 'partition_inventory_incomplete'
        return row
    actual = {}
    for partition in partitions:
        for n,bits in partition['outbits'].items():
            actual.setdefault(n, set()).update(bits)
    row['all_outputs_covered'] = all(actual.get(n) == bits for n,bits in outputs.items())
    inputs = {n:set(range(len(p['bits']))) for n,p in g['ports'].items() if p['direction'] == 'input'}
    dependencies = [(n,bits) for p in partitions for kind in ('inbits','crossbits')
                    for n,bits in p.get(kind, {}).items()]
    row['internal_cut_inputs'] = any(n not in inputs for n,bits in dependencies)
    row['cut_dependencies_covered'] = all(set(bits) <= inputs.get(n, actual.get(n, set()))
                                        for n,bits in dependencies)
    if not row['all_outputs_covered'] or not row['cut_dependencies_covered']:
        row['reason'] = 'incomplete_outputs_or_unproved_cut_dependency'
        return row
    row['partitions'] = []
    for partition in partitions:
        name = partition['name']
        require(Path(name).name == name and name not in {'.','..'}, 'invalid EQY partition name')
        status_path = work/'strategies'/name/'induction'/'status'
        status = status_path.read_text().strip() if status_path.is_file() else ''
        task = status_path.parent/name
        sby_status = (task/'status').read_text().split()[0] if (task/'status').is_file() else ''
        row['partitions'].append({'name':name, 'eqy_strategy_status':status, 'sby_status':sby_status})
    if outcome == 'ok' and all(p['eqy_strategy_status'] == p['sby_status'] == 'PASS' for p in row['partitions']):
        row.update(status='equivalent', reason='unbounded_pdr_proof' if engine == 'abc pdr' else 'unbounded_induction_proof')
    elif len(partitions) == 1 and status == sby_status == 'FAIL' and not auxiliary:
        witness = task/'engine_0/trace.vcd'
        log = (task/'logfile.txt').read_text()
        if witness.is_file() and 'status returned by engine for basecase: fail' in log.lower():
            row.update(status='different', reason='reachable_output_counterexample',
                       counterexample=str(witness.relative_to(directory)))
        else:
            row['reason'] = 'failure_without_reachable_counterexample'
    else:
        row['reason'] = 'auxiliary_or_induction_not_proven' if auxiliary else 'proof_not_completed'
    return row


def prove(spec, implementation, config, ports, tools, directory, timeout):
    directory.mkdir()
    started = time.monotonic()
    deadline = started+timeout
    row = {'status':'unknown', 'gold':'new_or_baseline_RTL', 'gate':'archived_old_netlist',
           'new_full_synthesis_used':False, 'proof_kind':'unbounded_sequential', 'obligations':{}}
    try:
        original = {Path(spec):digest(spec), Path(implementation):digest(implementation)}
        gold = proof_design(spec, config, ports, tools['yosys']['path'], directory, 'gold', deadline)
        gate = proof_design(implementation, config, ports, tools['yosys']['path'], directory, 'gate', deadline)
        total = eqy(definedness(gold, True, bool(config.get('reset'))), definedness(gold, False),
                    directory/'definedness', tools, deadline,
                    engine='abc pdr')
        row['obligations']['definedness'] = total
        if total['status'] != 'equivalent':
            row['reason'] = 'new_outputs_not_proven_defined'
            return row
        match = eqy(gold, gate, directory/'equivalence', tools, deadline, strengthen=True)
        row['obligations']['equivalence'] = match
        if match['status'] != 'equivalent' and match['auxiliary_invariants'] and time.monotonic() < deadline:
            match = eqy(gold, gate, directory/'outputs-only', tools, deadline)
            row['obligations']['outputs_only_retry'] = match
        row.update(status=match['status'], reason=match['reason'])
        require(all(digest(p) == h for p,h in original.items()), 'proof input changed while proving')
    except (ValueError, KeyError, TypeError, IndexError, OSError, subprocess.SubprocessError) as exc:
        row.update(status='unknown', reason='tool_or_scope_error', detail=str(exc))
    finally:
        row['elapsed_seconds'] = time.monotonic()-started
        row['files'] = fingerprint(directory)
        write_json(directory/'report.json', row)
    return row


def configuration(args):
    params = {}
    for assignment in args.param or []:
        name,value = assignment.split('=', 1)
        identifier(name)
        params[name] = int(value, 0)
    return {'top':identifier(args.top), 'clock':identifier(args.clock), 'parameters':params,
            'reset':identifier(args.reset) if args.reset else None, 'reset_active':args.reset_active,
            'reset_cycles':args.reset_cycles, 'clock_semantics':'single_positive_edge_cycle_model',
            'initial_state_policy':'startup_synchronous_reset' if args.reset else 'declared_initialization',
            'input_semantics':'all input bits defined, arbitrary each cycle',
            'observations':'all outputs after startup reset' if args.reset else 'all outputs from initial state'}


def baseline(args):
    started = time.monotonic()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    config, tools = configuration(args), toolchain(args)
    row = {'schema':SCHEMA, 'status':'blocked', 'config':config, 'tools':tools,
           'sequential_local_replacement_supported':False}
    try:
        row['frontend'] = frontend(args, config, out/'rtl', tools['yosys']['path'],
                                   time.monotonic()+args.frontend_timeout)
        # Exactly the stock default synth; flattening is confined to proof IR.
        yosys(tools['yosys']['path'], out, 'synth', 'read_rtlil rtl/frontend.il\nsynth -top '+
              config['top']+'\ncheck -assert\nwrite_rtlil netlist.il\nwrite_json netlist.json\n'+
              'write_verilog -noattr netlist.v', time.monotonic()+args.synthesis_timeout)
        # The independently delivered Verilog is the implementation checked by
        # EQY. Re-read it so serialization errors cannot hide behind RTLIL proof.
        yosys(tools['yosys']['path'], out, 'roundtrip', 'read_verilog netlist.v\nhierarchy -check -top '+
              config['top']+'\nproc\nflatten\nwrite_rtlil implementation.il',
              time.monotonic()+args.frontend_timeout)
        row['proof'] = prove(out/'rtl/coarse.il', out/'implementation.il', config,
                             row['frontend']['ports'], tools, out/'proof', args.proof_timeout)
        if row['proof']['status'] == 'equivalent':
            row['status'] = 'ready'
    except (TraceError, OSError, subprocess.SubprocessError) as exc:
        row['failure'] = str(exc)
    row['files'] = fingerprint(out)
    row['elapsed_seconds'] = time.monotonic()-started
    write_json(out/'baseline.json', row)
    return row


def reuse(args):
    started = time.monotonic()
    out, base = Path(args.out).resolve(), Path(args.base_run).resolve()
    out.mkdir(parents=True, exist_ok=False)
    row = {'status':'blocked', 'reused':False, 'new_full_synthesis_used':False,
           'sequential_local_replacement_supported':False, 'next_action':'full_synthesis_required'}
    try:
        manifest = base/'baseline.json'
        manifest_hash = digest(manifest)
        old = json.loads(manifest.read_text())
        require(old['schema'] == SCHEMA and old['status'] == 'ready' and
                old['proof']['status'] == 'equivalent', 'baseline is not verified for sequential reuse')
        verify_files(base, old['files'])
        tools = toolchain(args)
        require(tools['yosys'] == old['tools']['yosys'], 'Yosys tool changed since baseline')
        config = old['config']
        row.update(config=config, tools=tools, baseline_manifest_sha256=manifest_hash)
        row['frontend'] = frontend(args, config, out/'rtl', tools['yosys']['path'],
                                   time.monotonic()+args.frontend_timeout)
        require(row['frontend']['ports'] == old['frontend']['ports'], 'changed top-level interface')
        row['proof'] = prove(out/'rtl/coarse.il', base/'implementation.il', config,
                             row['frontend']['ports'], tools, out/'proof', args.proof_timeout)
        row['status'] = row['proof']['status']
        verify_files(base, old['files'])
        require(digest(manifest) == manifest_hash, 'baseline manifest changed during proof')
        verify_files(out/'proof', row['proof']['files'])
        require(all(digest(p) == h for p,h in row['frontend']['original_sources'].items()),
                'new RTL changed during proof')
        if row['status'] == 'equivalent':
            # Hash checks above bind proof to the delivered netlist, not just a
            # filename. No synthesis of the new design and no stitching occurs.
            for suffix in ('v', 'il', 'json'):
                shutil.copyfile(base/('netlist.'+suffix), out/('reused.'+suffix))
                require(digest(out/('reused.'+suffix)) == old['files']['netlist.'+suffix], 'reuse copy mismatch')
            row.update(reused=True, next_action='reuse_old_netlist_under_recorded_conditions')
    except (TraceError, OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        row.update(status='blocked', failure=str(exc), reused=False)
    if not row['reused']:
        for suffix in ('v', 'il', 'json'):
            (out/('reused.'+suffix)).unlink(missing_ok=True)
    row['files'] = fingerprint(out)
    row['elapsed_seconds'] = time.monotonic()-started
    write_json(out/'report.json', row)
    return row
