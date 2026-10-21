"""Structural assignment fingerprints of archived pre-proc RTLIL.

This does not evaluate Verilog or replace proc. It follows writer-generated
temporaries, ordered process actions and their enclosing priority conditions.
Extraction uses Yosys' actual lowered network, and delivery requires full SAT.
"""
from collections import defaultdict
import json
import re

from yosys_flow.contracts import fingerprint
from .rtlil import tokens


class Unsupported(ValueError):
    """A construct for which the automatic planner must use a coarser scope."""


def check(condition, message):
    if not condition: raise Unsupported(message)


def elements(module):
    pending=[]; lines=module.lines; i=1
    while i<len(lines)-1:
        ts=tokens(lines[i]); kind=ts[0]
        if kind=='attribute': pending.append(lines[i]);i+=1;continue
        body=[lines[i]];i+=1
        if kind in {'cell','process'}:
            while i<len(lines) and lines[i]!='  end':body.append(lines[i]);i+=1
            check(i<len(lines),'unterminated RTLIL object');body.append(lines[i]);i+=1
        yield {'kind':kind,'tokens':ts,'attributes':pending,'lines':body}
        pending=[]


def source(element):
    for line in element['attributes']:
        ts=tokens(line)
        if ts[:2]==['attribute','\\src']:return json.loads(ts[2])
    return None


class Graph:
    def __init__(self,module,modules):
        self.module=module;self.wires={};self.cells={};self.processes=[]
        self.aliases={};self.drivers={};self.inputs=set();self.memo={};self.active=set()
        objects=list(elements(module))
        for item in objects:
            ts=item['tokens']
            if item['kind']!='wire':continue
            width=int(ts[ts.index('width')+1]) if 'width' in ts else 1
            check(width>0,'zero width wire')
            self.wires[ts[-1]]={'width':width,'source':source(item),'public':ts[-1].startswith('\\')}
            if 'input' in ts:self.inputs.update((ts[-1],i) for i in range(width))
        for item in objects:
            ts=item['tokens'];kind=item['kind']
            if kind=='cell':
                check(ts[1].lstrip('\\') not in modules,'hierarchical cell requires module fallback')
                ports={};params=[]
                for line in item['lines'][1:-1]:
                    row=tokens(line)
                    if row[0]=='connect':ports[row[1]]=self.one(row[2:])
                    elif row[0]=='parameter':params.append(row[1:])
                    else:check(False,'unsupported cell record')
                check('\\Y' in ports and ts[1].startswith('$'),'unsupported frontend cell output convention')
                cell={**item,'name':ts[2],'type':ts[1],'ports':ports,'params':params}
                self.cells[ts[2]]=cell
                for offset,bit in enumerate(ports['\\Y']):self.drive(bit,('cell',cell,offset))
            elif kind=='process':
                process=self.process(item);self.processes.append(process)
                for target,value in process['updates'].items():self.drive(target,('process',process,value))
            elif kind=='connect':
                lhs,rhs=self.pair(ts[1:])
                for a,b in zip(lhs,rhs):
                    check(a not in self.aliases or self.aliases[a]==b,'ambiguous frontend connection')
                    self.aliases[a]=b
            else:check(kind in {'wire','parameter'},'unsupported module record '+kind)

    def drive(self,bit,driver):
        check(isinstance(bit,tuple) and bit not in self.drivers,'multiple frontend drivers')
        self.drivers[bit]=driver

    def signal(self,ts,pos=0):
        check(pos<len(ts),'missing RTLIL signal');token=ts[pos];pos+=1
        if token=='{':
            pieces=[]
            while pos<len(ts) and ts[pos]!='}':
                piece,pos=self.signal(ts,pos);pieces.append(piece)
            check(pos<len(ts),'unclosed signal concatenation')
            return [b for part in reversed(pieces) for b in part],pos+1
        if token in self.wires:
            indices=list(range(self.wires[token]['width']))
            if pos<len(ts) and ts[pos].startswith('['):
                match=re.fullmatch(r'\[(\d+)(?::(\d+))?\]',ts[pos]);pos+=1
                check(match is not None,'unsupported signal selection')
                high=int(match[1]);low=int(match[2]) if match[2] else high
                check(high>=low and high<len(indices),'invalid signal range');indices=list(range(low,high+1))
            return [(token,i) for i in indices],pos
        match=re.fullmatch(r"(\d+)'([01xz-]+)",token)
        if match:
            check(int(match[1])==len(match[2]),'unsupported abbreviated constant')
            return list(reversed(match[2])),pos
        if re.fullmatch(r'-?\d+',token):return list(reversed(format(int(token)&0xffffffff,'032b'))),pos
        raise Unsupported('unsupported RTLIL signal '+token)

    def one(self,ts):
        bits,pos=self.signal(ts);check(pos==len(ts),'extra signal tokens');return bits

    def pair(self,ts):
        a,pos=self.signal(ts);b,end=self.signal(ts,pos)
        check(end==len(ts) and len(a)==len(b),'signal pair width mismatch');return a,b

    def process(self,item):
        actions=defaultdict(list);updates={};stack=[];sync=False
        for line in item['lines'][1:-1]:
            ts=tokens(line);kind=ts[0]
            if kind=='attribute':continue
            if kind=='switch':
                stack.append({'signal':self.one(ts[1:]),'cases':[]});continue
            if kind=='case':
                check(stack,'case outside switch');stack[-1]['cases'].append(ts[1:]);continue
            if kind=='end':
                check(stack,'unexpected process end');stack.pop();continue
            if kind=='sync':
                check(not stack and ts==['sync','always'],'non-combinational process sync');sync=True;continue
            check(kind in {'assign','update'},'unsupported process action '+kind)
            lhs,rhs=self.pair(ts[1:])
            if kind=='update':
                check(sync,'update outside sync')
                for a,b in zip(lhs,rhs):check(a not in updates,'duplicate process update');updates[a]=b
            else:
                check(not sync,'assignment after sync')
                # Include preceding cases: a later/default branch is guarded by
                # the failures of earlier branches, including wildcard matches.
                controls=[(s['signal'],[list(c) for c in s['cases']]) for s in stack]
                for a,b in zip(lhs,rhs):actions[a].append((b,controls))
        check(not stack,'unclosed process switch')
        return {**item,'name':item['tokens'][1],'actions':dict(actions),'updates':updates}

    def value(self,bit,local=False,root=False):
        if isinstance(bit,str):return fingerprint(['constant',bit])
        if bit in self.inputs or (local and not root and self.wires[bit[0]]['public']):
            return fingerprint(['signal',*bit])
        key=(bit,local,root)
        if key in self.memo:return self.memo[key]
        check(key not in self.active,'cyclic/unsupported pre-proc dependency');self.active.add(key)
        if bit in self.aliases:
            result=self.value(self.aliases[bit],local)
        elif bit in self.drivers:
            kind,obj,offset=self.drivers[bit]
            if kind=='cell':
                result=fingerprint(['cell',obj['type'],obj['params'],offset,
                    [(p,[self.value(b,local) for b in bits]) for p,bits in sorted(obj['ports'].items()) if p!='\\Y']])
            else:result=self.process_value(obj,offset,local,{},set())
        else:raise Unsupported('undriven frontend signal '+str(bit))
        self.active.remove(key);self.memo[key]=result;return result

    def process_value(self,process,bit,local,memo,active):
        if bit not in process['actions']:return self.value(bit,local)
        if bit in memo:return memo[bit]
        check(bit not in active,'cyclic process temporary');active.add(bit)
        def val(b):return self.process_value(process,b,local,memo,active)
        result=fingerprint(['ordered_process_actions',[
            [val(rhs),[([val(b) for b in signal],cases) for signal,cases in controls]]
            for rhs,controls in process['actions'][bit]]])
        active.remove(bit);memo[bit]=result;return result

    def definition(self,bit):
        """Find the implementation of a named assignment without crossing it."""
        seen=set()
        while bit in self.aliases:
            check(bit not in seen,'cyclic alias');seen.add(bit);rhs=self.aliases[bit]
            if isinstance(rhs,str) or self.wires[rhs[0]]['public']:return ('connection',None,bit)
            bit=rhs
        return self.drivers.get(bit,('connection',None,bit))

    def public_bits(self):
        return [(name,i) for name,wire in self.wires.items() if wire['public']
                for i in range(wire['width']) if (name,i) not in self.inputs]


def differences(old,new):
    """Named assignment roots are anchors; other named signals are cut leaves.

    Thus changing t=a&b does not by itself mark y=t^c as an edited statement.
    Full-cone fingerprints (local=False) separately protect reused input cuts.
    """
    common=set(old.public_bits())&set(new.public_bits());changes=[]
    for bit in sorted(common):
        a,b=old.value(bit,local=True,root=True),new.value(bit,local=True,root=True)
        if a!=b:changes.append({'wire':bit[0],'offset':bit[1],
            'old_fingerprint':a,'new_fingerprint':b,'definition_kind':old.definition(bit)[0]})
    check(changes,'no comparable changed assignment roots; module fallback required')
    return changes
