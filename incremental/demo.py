"""Four-lane experiment with an explicit edit and proven boundary bindings."""
from pathlib import Path
from types import SimpleNamespace

from yosys_flow.boolean import BDD
from yosys_flow.cli import parser as flow_parser
from yosys_flow.common import require, write_json
from yosys_flow.contracts import ContractContext, SELECTOR_KEYS
from yosys_flow.region_functions import Circuit, encode_function
from yosys_flow.runner import run as baseline_run
from .replace import run as replace_run

EXAMPLE = Path(__file__).resolve().parent/'examples/four_lanes'


def resolve_bindings(context, contract):
    """Fixture-specific boundary spec, checked against the OLD actual circuit.

    ABC can remove `pre` and retain its complement. Never infer a signal's
    identity or polarity from the spelling/number of an optimized wire.
    """
    module = context.module
    manager = BDD()
    pins = [(n,i,b) for n,p in module['ports'].items() if p['direction']=='input' for i,b in enumerate(p['bits'])]
    variables = manager.variables(len(pins))
    by_pin = {(n,i):v for (n,i,_),v in zip(pins,variables)}
    circuit = Circuit(module['cells'],{b:v for (_,_,b),v in zip(pins,variables)},manager)
    expected = {
        'a':[(by_pin['data',i]^by_pin['key',i],f'data[{i}] ^ key[{i}]') for i in range(8,16)],
        'b':[(by_pin['mask',i],f'mask[{i}]') for i in range(8,16)],
        'c':[(by_pin['salt',i],f'salt[{i}]') for i in range(8,16)]}
    boundary = [b for b in contract['boundary'] if b['direction']=='input']
    actual = [(b,circuit.value(b['original_bit'])) for b in boundary]
    bindings = {'schema_version':1,'contract_id':contract['contract_id'],'inputs':{},'outputs':{'y':[]}}
    evidence = []
    for port,functions in expected.items():
        bindings['inputs'][port] = []
        for i,(function,specification) in enumerate(functions):
            matches = [(b,inverted) for b,value in actual for inverted in (False,True)
                       if function == value ^ int(inverted)]
            require(len(matches)==1, f'local boundary not uniquely retained: {port}[{i}]; expand the region or add missing evidence')
            row,inverted = matches[0]
            bindings['inputs'][port].append({'port':row['port'],'inverted':inverted})
            evidence.append({'local_port':port,'offset':i,'specification':specification,
                'actual_boundary_port':row['port'],'actual_bit':row['original_bit'],'inverted':inverted,
                'status':'proven_against_old_final_circuit',
                'function':encode_function(function,[f'{n}[{j}]' for n,j,_ in pins])})
    for bit in module['ports']['result']['bits'][8:16]:
        outputs = [b for b in contract['boundary'] if b['direction']=='output' and b['original_bit']==bit]
        require(len(outputs)==1, 'result output is not present exactly once at the contract boundary')
        bindings['outputs']['y'].append(outputs[0]['port'])
    return bindings, {'rule':'explicit_fixture_functions_matched_to_recorded_boundary',
        'automatic_RTL_dependency_discovery':False, 'bindings':evidence,'resources':manager.statistics()}


def run(args):
    out = Path(args.out).resolve()
    require(not out.exists(), 'refusing to overwrite demo directory: '+str(out))
    out.mkdir(parents=True)
    rows = []
    modes = ['retained','flatten'] if args.mode=='both' else [args.mode]
    for mode in modes:
        directory = out/mode; directory.mkdir()
        source = EXAMPLE/'base.v'; base = directory/'base-run'
        baseline_run(flow_parser().parse_args(['run','--source',str(source),'--top','chip','--out',str(base),
            '--native-yosys',args.native_yosys,'--yosys',args.yosys,*(['--flatten'] if mode=='flatten' else [])]))
        context = ContractContext(base)
        parents = []
        for line in (3,4):
            selector = dict.fromkeys(SELECTOR_KEYS)
            selector.update(source=str(source),line=line,instance='chip/u1/core')
            doc = context.build({'kind':'selection','selector':selector})
            write_json(directory/f'line-{line}.contract.json',doc); parents.append(doc)
        contract = context.build({'kind':'union','parents':parents})
        context.validate(contract)
        write_json(directory/'region.contract.json',contract)
        bindings,evidence = resolve_bindings(context,contract)
        write_json(directory/'bindings.json',bindings)
        write_json(directory/'boundary-proof.json',evidence)
        result = replace_run(SimpleNamespace(base_run=str(base),contract=str(directory/'region.contract.json'),
            bindings=str(directory/'bindings.json'),patch_rtl=str(EXAMPLE/'patch.v'),patch_top='patch',
            new_source=[str(EXAMPLE/'new.v')],out=str(directory/'replacement'),yosys=args.yosys))
        rows.append({'mode':mode,'status':result['status'],
            **{k:result['splice'][k] for k in ('base_cells','removed_cells','reused_cells','local_synthesized_cells',
                                             'input_polarity_inverters','output_binding_buffers','result_cells')},
            'output_count':len(contract['outputs']), 'checks':result['checks'],
            'report':str(directory/'replacement/report.json')})
    report = {'status':'passed','example':'four_lanes','modes':rows,
        'local_edit':'only chip/u1/core: (a & b) ^ c -> (a | b) ^ c',
        'new_local_spec_is_explicit':True,'new_full_synthesis_is_validation_only':True,
        'generic_automatic_incremental_synthesis':False}
    write_json(out/'report.json',report)
    return report
