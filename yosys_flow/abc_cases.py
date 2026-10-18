"""Independent structural expectations for the pinned default-synth fixtures.

Only RTL intent and the final netlist define expectations, never trace origins.
The electrical cone below is a TEST ORACLE, not the product selection algorithm.
"""
from collections import Counter
from .common import ROOT, require


def cases(sizes=(8,10,12,16,32)):
    result = []
    for name, filename, queries in (
        ('chain','chain.v',[(3,'upstream',{3}),(4,'y',{4})]),
        ('shared','shared_chain.v',[(3,'upstream',{3}),(4,'y',{4}),(5,'z',{5})]),
        ('multilevel','multilevel.v',[(3,'upstream',{3}),(4,'joint',{4,5}),(5,'joint',{4,5}),(6,'z',{6})]),
        ('factoring','factoring.v',[(2,'factor',{2,3}),(3,'factor',{2,3}),(4,'independent',{4})]),
    ):
        result.append({'name':name,'rtl':(ROOT/'examples'/filename).read_text(),
                       'queries':[{'line':line,'rule':rule,'origin_lines':sorted(origins)} for line,rule,origins in queries]})
    result.append({'name':'retained_boundary','rtl':'''module demo(input s,t,b,c,d,e,output a,y,z,independent);
assign a=s&t;
assign y=(a&b)|(a&c);
assign z=a&(b|c);
assign independent=d^e;
endmodule
''', 'queries':[{'line':2,'rule':'a','origin_lines':[2]},
                {'line':3,'rule':'bounded_factor','origin_lines':[3,4]},
                {'line':4,'rule':'bounded_factor','origin_lines':[3,4]},
                {'line':5,'rule':'independent','origin_lines':[5]}]})
    for name,expr,value in [('zero','(a|b)&(~a|b)&~b','0'),('one','~((a|b)&(~a|b)&~b)','1')]:
        result.append({'name':name,'rtl':f'module demo(input a,b,output y);\nassign y={expr};\nendmodule\n',
                       'queries':[{'line':2,'rule':'constant','value':value,'origin_lines':[2]}]})
    for size in sizes:
        require(size>=4,'scale cases need at least four inputs')
        result.append({'name':f'scale_{size}','declared_input_count':size,
                       'rtl':f'''module demo(input [{size-1}:0] a,output t,small,y);
assign t=a[0]&a[1];
assign small=t^a[2];
assign y=small^(^a[{size-1}:3]);
endmodule
''', 'queries':[{'line':2,'rule':'t','origin_lines':[2]},
                {'line':3,'rule':'small','origin_lines':[3]},
                {'line':4,'rule':'scale_tail','origin_lines':[4]}]})
    return result


def outputs(cell):
    return {b for p,bs in cell['connections'].items() if cell['port_directions'][p]=='output' for b in bs if isinstance(b,int)}


def driver(module, port):
    bits=set(module['ports'][port]['bits'])
    return {n for n,c in module['cells'].items() if outputs(c)&bits}


def cone(module, port, stops=()):
    by_bit={b:n for n,c in module['cells'].items() for b in outputs(c)}
    found=set(); stack=list(module['ports'][port]['bits']); stops=set(stops)
    while stack:
        bit=stack.pop()
        if bit in stops or bit not in by_bit: continue
        name=by_bit[bit]
        if name in found: continue
        found.add(name)
        stack.extend(b for p,bs in module['cells'][name]['connections'].items()
                     if module['cells'][name]['port_directions'][p]=='input' for b in bs)
    return found


def expectation(case, spec, module):
    rule=spec['rule']; cells=module['cells']
    if rule=='constant':
        require(not cells and module['ports']['y']['bits']==[spec['value']], 'constant fixture changed')
        return set()
    if rule=='factor':
        result=cone(module,'y')
        require(Counter(cells[n]['type'] for n in result)==Counter(['$_AND_','$_OR_']), 'factoring fixture changed')
        return result
    if rule=='bounded_factor':
        result=cone(module,'y',module['ports']['a']['bits'])
        require(len(result)==2,'bounded factoring fixture changed')
        return result
    if rule in {'upstream','joint'}:
        ab=set(module['ports']['a']['bits']+module['ports']['b']['bits'])
        upstream={n for n,c in cells.items() if {b for p,bs in c['connections'].items()
                  if c['port_directions'][p]=='input' for b in bs}==ab}
        require(len(upstream)==1,'retained upstream fixture changed')
        if rule=='upstream': return upstream
        stop={b for n in upstream for b in outputs(cells[n])}
        result=cone(module,'y',stop)
        require(len(result)==2,'multilevel joint fixture changed')
        return result
    if rule=='scale_tail': return cone(module,'y',module['ports']['small']['bits'])
    result=driver(module,rule)
    require(len(result)==1,f'expected single driver for {rule}')
    return result


def expected_boundary(module, selected, spec):
    """Independent expected electrical bits, including constant/connection cases."""
    if spec['rule']=='constant': return set(), {spec['value']}
    driven={b for n in selected for b in outputs(module['cells'][n])}
    used_inputs={b for n in selected for p,bs in module['cells'][n]['connections'].items()
                 if module['cells'][n]['port_directions'][p]=='input' for b in bs if isinstance(b,int)}
    outside={b for n,c in module['cells'].items() if n not in selected for p,bs in c['connections'].items()
             if c['port_directions'][p]=='input' for b in bs}
    outside.update(b for p in module['ports'].values() if p['direction']=='output' for b in p['bits'])
    return used_inputs-driven, driven & outside
