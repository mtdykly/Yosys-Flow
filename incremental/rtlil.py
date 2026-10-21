"""Read the pinned Yosys RTLIL writer's module/instance structure.

Processes are kept verbatim. This is not a Verilog parser or semantic-equivalence
oracle. Canonical comparison only removes source metadata and alpha-renames
generated identifiers; every delivered result still requires whole-output SAT.
"""
from dataclasses import dataclass
import difflib
import json
import re
from urllib.parse import quote

from yosys_flow.common import require, unescape
from yosys_flow.contracts import fingerprint

TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[^\s]+')
IGNORED = {'\\src','\\hdlname','\\top','\\module_not_derived','\\cells_not_processed','\\dynports'}


def tokens(line): return TOKEN.findall(line)


@dataclass
class Module:
    name: str
    attributes: list
    lines: list

    @property
    def source(self):
        for line in self.attributes:
            ts=tokens(line)
            if ts[:2]==['attribute','\\src']: return json.loads(ts[2])
        return None

    def ports(self):
        result={}
        for line in self.lines:
            ts=tokens(line)
            if not line.startswith('  wire ') or not {'input','output','inout'}&set(ts): continue
            def value(key,default): return int(ts[ts.index(key)+1]) if key in ts else default
            direction=next(k for k in ('input','output','inout') if k in ts)
            result[unescape(ts[-1])]={'direction':direction,'width':value('width',1),
                'offset':value('offset',0),'upto':'upto' in ts,'signed':'signed' in ts}
        return result

    def children(self, modules):
        return {unescape(ts[2]):unescape(ts[1]) for line in self.lines
                if line.startswith('  cell ') and (ts:=tokens(line)) and unescape(ts[1]) in modules}

    def canonical(self, modules):
        generated={}
        for line in self.lines:
            ts=tokens(line)
            if ts and ts[0] in {'wire','cell','process'} and ts[-1].startswith('$'):
                generated.setdefault(ts[-1], '@generated_'+str(len(generated)))
        result=[]
        for line in [*self.attributes,*self.lines]:
            ts=tokens(line)
            if not ts or ts[0].startswith('#'): continue
            if ts[0]=='attribute' and ts[1] in IGNORED: continue
            if ts[0]=='module': ts=['module','@self']
            if ts[0]=='cell' and unescape(ts[1]) in modules: ts[1]='@child_module'
            result.append([generated.get(t,t) for t in ts])
        return result

    def render(self):
        attributes=[s for s in self.attributes if tokens(s)[:2]!=['attribute','\\top']]
        return '\n'.join([*attributes,*self.lines])+'\n'


def parse(text):
    modules={}; attributes=[]; current=None; lines=[]
    for line in text.splitlines():
        if current is None:
            if line.startswith('attribute '): attributes.append(line)
            elif line.startswith('module '):
                ts=tokens(line);require(len(ts)==2,'unsupported RTLIL module identifier')
                current=unescape(ts[1]);lines=[line]
            else: require(not line or line.startswith(('#','autoidx ')), 'unsupported RTLIL top-level record')
        else:
            lines.append(line)
            if line=='end':
                require(current not in modules,'duplicate RTLIL module')
                modules[current]=Module(current,attributes,lines)
                current=None;attributes=[]
    require(current is None and modules,'incomplete/empty RTLIL design')
    return modules


def catalog(modules, top):
    result={}
    def visit(name,path,ancestors):
        require(name in modules and name not in ancestors and len(result)<10000,'invalid or oversized RTLIL hierarchy')
        result[path]=name
        for child,kind in sorted(modules[name].children(modules).items()):
            visit(kind,path+'/'+quote(child,safe=''),ancestors|{name})
    visit(top,quote(top,safe=''),set())
    return result


def compare(old, new, top):
    """Match by actual instance paths, allowing renamed module definitions.

    Child type names alone are not parent behavior changes: compare the child
    definitions at that instance. Port bindings and instance additions/removals
    ARE part of the parent comparison and select that whole parent.
    """
    oc,nc=catalog(old,top),catalog(new,top)
    require(old[top].ports()==new[top].ports(),'automatic mode requires an unchanged top interface')
    changes=[];unchanged=[]
    def walk(path):
        a,b=old[oc[path]],new[nc[path]]
        ac,bc=a.canonical(old),b.canonical(new)
        if ac!=bc:
            delta=[]
            for tag,i,j,k,l in difflib.SequenceMatcher(a=list(map(str,ac)),b=list(map(str,bc)),autojunk=False).get_opcodes():
                if tag!='equal': delta.append({'operation':tag,'old':ac[i:j],'new':bc[k:l]})
            changes.append({'instance':path,'old_module':a.name,'new_module':b.name,
                'old_src':a.source,'new_src':b.source,'old_fingerprint':fingerprint(ac),
                'new_fingerprint':fingerprint(bc),'ir_changes':delta})
            return
        unchanged.append(path)
        for child in sorted(a.children(old)):
            target=path+'/'+quote(child,safe='')
            require(target in nc,'unchanged parent has different child instance')
            walk(target)
    walk(quote(top,safe=''))
    return {'rule':'instance_path_and_alpha_normalized_pre_proc_rtlil_v1',
            'scope':'smallest_changed_module_instances_not_minimal_statement_slices',
            'changes':changes,'unchanged_local_bodies':unchanged},oc,nc


def source_selector(module, instance):
    from yosys_flow.contracts import SELECTOR_KEYS
    match=re.fullmatch(r'(.*):(\d+)\.(\d+)-(\d+)\.(\d+)',module.source or '')
    require(match is not None,'recorded module source range unavailable: '+module.name)
    result=dict.fromkeys(SELECTOR_KEYS)
    result.update(source=match[1],line=int(match[2]),column=int(match[3]),
                  end_line=int(match[4]),end_column=int(match[5]),instance=instance)
    return result


def extract(modules, roots):
    needed=set()
    def visit(name):
        if name in needed:return
        needed.add(name)
        for child in modules[name].children(modules).values():visit(child)
    for name in roots:visit(name)
    return ''.join(modules[name].render() for name in sorted(needed))
