"""Prepare immutable ABC witnesses once, independently of an ECO target."""
import gzip
import json
import hmac

from .abc_partition import Graph
from .common import digest, require


class Checkpoints:
    def __init__(self, context, region, directory, progress, *, rewrites=True,
                 cec_timeout=60, cec_attempts=8, resume=False):
        from .abc_checkpoints import capture
        from .proof_store import implementation, signature
        from .proof_checkpoint import atomic_json
        self.directory = directory
        binding={'baseline':context.baseline['manifest_sha256'],'invocation':region['invocation_id'],
                 'implementation':implementation(),'rewrites':rewrites,'cec_timeout':cec_timeout,'cec_attempts':cec_attempts}
        seal=directory/'witness-auth.json'
        if resume and seal.is_file():
            document=json.loads(seal.read_text());auth=document.pop('authentication','')
            require(hmac.compare_digest(signature(document),auth) and document['binding']==binding,
                    'checkpoint witness authentication mismatch')
            for name,sha in document['files'].items():
                require((directory/name).parent==directory and digest(directory/name)==sha,'checkpoint witness changed')
        else:
            # An interrupted, unsigned capture is not a proof cache. Preserve it
            # for diagnosis; rebuilding uses a fresh subdirectory.
            if directory.exists():
                import uuid
                directory=directory.with_name(directory.name+'-'+uuid.uuid4().hex)
                self.directory=directory;seal=directory/'witness-auth.json'
            from .evidence_relations import confirmed
            capture(context.directory, region, context.manifest, directory, progress,
                    include_rewrites=rewrites, mapping_cec_timeout=cec_timeout,mapping_cec_attempts=cec_attempts,
                    confirmed=confirmed(context,region))
            from .fraig_evidence import capture as capture_fraig
            capture_fraig(context.directory,region,context.manifest,directory,progress=progress)
            from .rewrite_recovery import capture as recover_rewrites
            recover_rewrites(context.directory,region,context.manifest,directory,progress=progress)
            document={'binding':binding,'files':{p.name:digest(p) for p in directory.iterdir() if p.is_file()}}
            atomic_json(seal,{**document,'authentication':signature(document)})
        self.metadata = json.loads((directory/'checkpoints.json').read_text())
        self.stages = self.metadata['stages']
        self.artifacts = {'checkpoints.json': (directory/'checkpoints.json', digest(directory/'checkpoints.json'))}
        for row in self.stages:
            self.file(row['file'], row['sha256'])
        self.lemmas = self.pairs('lemmas')
        self.fraig = self.pairs('fraig')
        self.mappings = self.pairs('mappings')
        self.copies = []
        for row in self.metadata.get('copies', []):
            paths = [self.file(row[k]['artifact'], row[k]['sha256']) for k in ('before', 'after', 'proof')]
            self.copies.append((Graph(paths[0].read_text()), Graph(paths[1].read_text()),
                                json.loads(paths[2].read_text()), row))
        self.recovery = None
        recovered = self.metadata.get('rewrite_recovery')
        if recovered:
            with gzip.open(self.file(recovered['artifact'],recovered['sha256']),'rt') as stream:
                self.recovery=(json.load(stream),recovered)
        self.rewrites = None
        self.replayed=[]
        replayed=self.metadata.get('replayed')
        if replayed:self.replayed=json.loads(self.file(replayed['artifact'],replayed['sha256']).read_text())
        row = self.metadata.get('rewrites')
        if row:
            with gzip.open(self.file(row['artifact'], row['sha256']), 'rt') as stream:
                self.rewrites = (json.load(stream), row)

    def file(self, name, sha):
        path = self.directory/name
        require(digest(path) == sha, 'checkpoint evidence changed: '+name)
        self.artifacts[name] = (path, sha)
        return path

    def pairs(self, key):
        return [(Graph(self.file(r['graph'], r['sha256']).read_text()),
                 json.loads(self.file(r['events'], r['events_sha256']).read_text()), r)
                for r in self.metadata.get(key, [])]

    def install(self, engine, progress, *, done=None, checkpoint=None, endpoint=False):
        from .abc_rewrites import install as rewrites
        from .abc_lemmas import install as lemmas, associative_congruence
        from .abc_copy import install as copies
        from .abc_mapping import install as mappings
        done=set(done or ())
        def apply(key,phase,callback):
            if key in done:return
            progress(phase);callback();done.add(key)
            if checkpoint:checkpoint(sorted(done))
        if endpoint and self.replayed:
            from .evidence_relations import install
            apply('replayed','reuse_verified_endpoint_equations',lambda:install(engine,self.replayed))
        from .fraig_evidence import install as fraig
        for i,args in enumerate(self.fraig):apply('fraig:'+str(i),'reuse_recorded_fraig_results',lambda:fraig(engine,*args))
        if self.rewrites:apply('rewrites','install_local_rewrite_functions',lambda:rewrites(engine,*self.rewrites))
        if self.recovery:
            from .rewrite_recovery import install as recover
            apply('rewrite_recovery','install_recovered_old_rewrites',lambda:recover(engine,*self.recovery))
        for i,args in enumerate(self.lemmas):apply('lemma:'+str(i),'reuse_recorded_solver_results',lambda:lemmas(engine,*args))
        for i,args in enumerate(self.copies):apply('copy:'+str(i),'install_verified_copy_functions',lambda:copies(engine,*args))
        for i,args in enumerate(self.mappings):apply('mapping:'+str(i),'install_verified_mapping_functions',lambda:mappings(engine,*args))
        apply('congruence','normalize_proved_function_classes',lambda:engine.proofs.append(
            {'proof':'and_associativity',**associative_congruence(engine.algebra,engine.classes,progress=progress)}))
        return sorted(done)

    def graphs(self, final, final_sha):
        for row in self.stages:
            yield Graph(self.file(row['file'], row['sha256']).read_text()), row
        yield final, {'endpoint': 'captured_output', 'sha256': final_sha}
