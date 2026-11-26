"""Reuse the verified replay in an opt-in archive + evidence preparation run."""
import errno
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace

from .common import digest, require
from .proof_checkpoint import atomic_json
from .proof_store import implementation, signature


def share_file(source, destination):
    try:os.link(source,destination)
    except OSError as exc:
        if exc.errno!=errno.EXDEV:raise
        shutil.copy2(source,destination)


class EvidencePipeline:
    def __init__(self, args, archive):
        self.archive=Path(archive)
        self.destination=Path(getattr(args,'evidence_out',None) or str(archive)+'.proofs').resolve()
        require(not self.destination.exists(), 'evidence output already exists')
        require(not self.destination.is_relative_to(self.archive), 'evidence output must be outside the archive')
        self.directory=Path(tempfile.mkdtemp(prefix=self.archive.name+'.proof-stage-',dir=self.archive.parent))
        self.entries={};self.seconds=0.0;self.fingerprint=implementation()
        self.cec_timeout=getattr(args,'evidence_cec_timeout',60)
        self.source_timeout=getattr(args,'source_timeout',600)

    def capture(self, directory, region, manifest, decoded, progress, confirmed):
        from .abc_checkpoints import capture
        start=time.perf_counter()
        path=self.directory/('checkpoints-'+str(len(self.entries)))
        capture(directory,region,manifest,path,progress,include_rewrites=True,
                mapping_cec_timeout=self.cec_timeout,decoded=decoded,confirmed=confirmed)
        from .fraig_evidence import capture as capture_fraig
        capture_fraig(Path(directory),region,manifest,path,decoded=decoded,progress=progress)
        from .rewrite_recovery import capture as recover_rewrites
        recover_rewrites(Path(directory),region,manifest,path,decoded=decoded,progress=progress)
        self.entries[region['invocation_id']]={
            'directory':path,'files':{p.name:digest(p) for p in path.iterdir() if p.is_file()}}
        self.seconds+=time.perf_counter()-start

    def adopt(self, context, destination, store):
        require(implementation()==self.fingerprint,'proof implementation changed during replay handoff')
        checked=set()
        for ordinal,region in enumerate(context.trace.regions):
            inv=region['invocation_id'];definition=region.get('definition_invocation_id',inv)
            require(definition in self.entries,'missing replay witness for invocation')
            row=self.entries[definition]
            path=row['directory']
            if definition not in checked:
                require({p.name:digest(p) for p in path.iterdir() if p.is_file()}==row['files'],
                        'staged witness changed before publication')
                checked.add(definition)
            final=destination/('checkpoints-'+str(ordinal))
            # Instances share immutable raw witnesses, but get independently
            # authenticated instance bindings. No links point into the archive.
            shutil.copytree(path,final,copy_function=share_file)
            binding={'baseline':context.baseline['manifest_sha256'],'invocation':inv,
                'implementation':self.fingerprint,'rewrites':True,'cec_timeout':self.cec_timeout,'cec_attempts':8}
            document={'binding':binding,'files':row['files']}
            atomic_json(final/'witness-auth.json',{**document,'authentication':signature(document)})
            store.completed['endpoint:'+inv+':witnesses']=final.name
        store.commit()
        shutil.rmtree(self.directory)

    def finish(self, manifest, trace):
        if not self.entries:
            self.directory.rmdir()
            return {'status':'not_applicable','reason':'no ABC invocation','decoded_abc_reused':0}
        from .contracts import ContractContext
        from .prepare_evidence import prepare
        context=ContractContext.from_validated(self.archive,manifest,trace)
        args=SimpleNamespace(directory=str(self.archive),out=str(self.destination),resume=False,
            structural_only=False,refine_outputs=False,cec_timeout=self.cec_timeout,
            enhance_budget=3600,batch_outputs=8,cache_engines=1,source_timeout=self.source_timeout)
        report=prepare(args,context=context,staged=self)
        return {**report,'replay_witness_capture_seconds':self.seconds}
