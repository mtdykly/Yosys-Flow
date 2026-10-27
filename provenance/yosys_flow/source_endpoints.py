"""Resolve source signal identities from validated instance/copy records."""
from urllib.parse import quote
from .common import unescape


class Endpoints:
    """Resolve original interface bits using actual recorded port/copy links."""
    def __init__(self,context): self.context=context;self.cache={};self.evidence=[]

    def bit(self,path,wire,offset,seen=frozenset()):
        key=(path,wire,offset)
        if key in self.cache:return self.cache[key]
        if key in seen:return None
        c=self.context;wire=unescape(wire);net=None;rule=None
        if c.trace.view_rule=='electrical_instance_expansion_v1':
            candidate=c.module['netnames'].get(path+'/'+quote(wire,safe=''))
            if candidate and candidate.get('definition',{}).get('instance')==path:
                net=candidate;rule='validated_instance_port_or_wire'
        elif path==quote(c.baseline['module'],safe=''):
            net=c.module['netnames'].get(wire);rule='recorded_top_wire'
        if net and offset<len(net['bits']): result=net['bits'][offset]
        else:
            info=c.trace.instance_catalog.get(path);result=None
            if not info:return None
            binding=info.get('bindings',{})
            ports=binding.get('ports',{})
            if '\\'+wire in ports and offset<len(ports['\\'+wire]):
                bit=ports['\\'+wire][offset]
                if isinstance(bit,str):result=bit
                else:result=self.bit(binding['parent'],bit['wire'],bit['offset'],seen|{key})
                rule='recorded_hierarchy_port_binding'
            if result is None and binding:
                events=[e for e in c.trace.flatten_events.values() if e['instance']==binding['instance_ref']]
                if len(events)==1 and '\\'+wire in events[0]['wire_map']:
                    result=self.bit(binding['parent'],events[0]['wire_map']['\\'+wire],offset,seen|{key})
                    rule='recorded_flatten_wire_map:'+events[0]['ref']
        if result is not None:
            self.cache[key]=result
            self.evidence.append({'instance':path,'wire':wire,'offset':offset,'final_bit':result,'rule':rule})
        return result

