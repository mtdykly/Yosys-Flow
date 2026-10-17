"""Build the immutable ABC revision with passive internal provenance hooks."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def instrument(source):
    def edit(name, old, new):
        p = source / name
        text = p.read_text()
        if text.count(old) != 1:
            raise ValueError(f'ABC hook anchor changed: {name}: {old[:100]}')
        p.write_text(text.replace(old, new))
    for name in ('src/base/cmd/cmdUtils.c', 'src/base/io/ioWriteBlif.c',
                 'src/aig/aig/aigObj.c', 'src/aig/gia/giaNf.c',
                 'src/base/abc/abcNetlist.c', 'src/proof/cec/cecSatG2.c',
                 'src/proof/dch/dchSweep.c', 'src/proof/dch/dchChoice.c',
                 'src/base/abci/abcStrash.c', 'src/base/abci/abc.c', 'src/base/abc/abcUtil.c'):
        edit(name, 'ABC_NAMESPACE_IMPL_START', '#include "base/main/flow.h"\n\nABC_NAMESPACE_IMPL_START')
    edit('src/base/abci/abcDar.c', '#include "misc/vec/vecMem.h"', '#include "misc/vec/vecMem.h"\n#include "base/main/flow.h"')
    edit('src/base/cmd/cmdUtils.c', '    fError = (*pFunc)( pAbc, argc, argv );',
         '    Flow_CommandBefore(pAbc, argc, argv);\n    fError = (*pFunc)( pAbc, argc, argv );\n    Flow_CommandAfter(pAbc, argc, argv, fError);')
    edit('src/base/io/ioWriteBlif.c', '    Io_NtkWrite( pFile, pNtk, fWriteLatches, fBb2Wb, fSeq );',
         '    Io_NtkWrite( pFile, pNtk, fWriteLatches, fBb2Wb, fSeq );\n    Flow_WriteBlif(pNtk, FileName);')
    edit('src/aig/aig/aigObj.c', '    // recursively delete the old node - but leave the object there',
         '    Flow_AigReplace(p, pObjOld, pObjNew);\n    // recursively delete the old node - but leave the object there')
    edit('src/aig/gia/giaNf.c', '    p->pGia->vCellMapping = vMapping;',
         '    p->pGia->vCellMapping = vMapping;\n    Flow_NfMapping(p->pGia);')
    edit('src/base/abci/abcDar.c', '    Vec_IntFree( vCopyLits );',
         '    Flow_FromMappedGia(p, pNtkNew, vCopyLits);\n    Vec_IntFree( vCopyLits );')
    # ToNetlist's logic conversion has one specific tail, shared by the writer.
    edit('src/base/abc/abcNetlist.c', '    if ( pNtk->pExdc )\n        pNtkNew->pExdc = Abc_NtkToNetlist( pNtk->pExdc );',
         '    Flow_ToNetlist(pNtk, pNtkNew);\n    if ( pNtk->pExdc )\n        pNtkNew->pExdc = Abc_NtkToNetlist( pNtk->pExdc );')
    edit('src/base/abci/abcStrash.c',
         '    Abc_NtkStrashPerform( pNtk, pNtkAig, fAllNodes, fRecord );\n    Abc_NtkFinalize( pNtk, pNtkAig );',
         '    Abc_NtkStrashPerform( pNtk, pNtkAig, fAllNodes, fRecord );\n    Abc_NtkFinalize( pNtk, pNtkAig );\n    Flow_StrashCopy(pNtk, pNtkAig);')
    edit('src/base/abci/abc.c',
         '            pAig = Abc_NtkToDar( pAbc->pNtkCur, 0, 1 );\n        pGia = Gia_ManFromAig( pAig );\n        Aig_ManStop( pAig );',
         '            pAig = Abc_NtkToDar( pAbc->pNtkCur, 0, 1 );\n        pGia = Gia_ManFromAig( pAig );\n        Flow_GetCopy(pAbc->pNtkCur, pAig, pGia);\n        Aig_ManStop( pAig );')
    edit('src/base/abc/abcUtil.c',
         '                    Abc_ObjAddFanin( pDriverNew, pFanin );\n                nDupGates++;',
         '                    Abc_ObjAddFanin( pDriverNew, pFanin );\n                Flow_OutputClone(pNtk, pDriver, pDriverNew);\n                nDupGates++;')
    # Only instrument the default Cec4 path, not similarly named experimental functions.
    p = source/'src/proof/cec/cecSatG2.c'
    text = p.read_text()
    begin = text.index('    execute_sat:')
    end = text.index('finalize:', begin)
    body = text[begin:end]
    for old, new in (
        ('            Gia_ObjSetProved( p, i );',
         '            Gia_ObjSetProved( p, i );\n            Flow_GiaEquivalence(p, pObj, pRepr, "fraig_structural_merge");'),
        ('            pObj->Value = Abc_LitNotCond( pRepr->Value, pObj->fPhase ^ pRepr->fPhase );',
         '            pObj->Value = Abc_LitNotCond( pRepr->Value, pObj->fPhase ^ pRepr->fPhase );\n            Flow_GiaEquivalence(p, pObj, pRepr, "fraig_sat_merge");'),
        ('        *ppNew = Gia_ManCleanup( pMan->pNew );',
         '        Flow_GiaSweepCopy(p, pMan->pNew);\n        *ppNew = Gia_ManCleanup( pMan->pNew );')):
        if body.count(old) != 1: raise ValueError('Cec4 hook anchor changed: '+old)
        body = body.replace(old, new)
    p.write_text(text[:begin]+body+text[end:])
    edit('src/proof/dch/dchSweep.c',
         '    if ( Aig_Regular(pObjFraig) == Aig_Regular(pObjReprFraig) )\n    {',
         '    if ( Aig_Regular(pObjFraig) == Aig_Regular(pObjReprFraig) )\n    {\n        Flow_AigEquivalence(p->pAigTotal, pObj, pObjRepr, "dch_structural_merge");')
    edit('src/proof/dch/dchSweep.c',
         '        Dch_ObjSetFraig( pObj, pObjFraig2 );',
         '        Dch_ObjSetFraig( pObj, pObjFraig2 );\n        Flow_AigEquivalence(p->pAigTotal, pObj, pObjRepr, "dch_sat_merge");')
    edit('src/proof/dch/dchChoice.c',
         '    Aig_ObjSetRepr( pAigNew, pObjNew, pReprNew );',
         '    Aig_ObjSetRepr( pAigNew, pObjNew, pReprNew );\n    Flow_AigEquivalence(pAigNew, pObjNew, pReprNew, "dch_representative");')
    edit('src/proof/dch/dchChoice.c',
         '    pAigNew->pEquivs[pReprNew->Id] = pObjNew;',
         '    pAigNew->pEquivs[pReprNew->Id] = pObjNew;\n    Flow_AigEquivalence(pAigNew, pObjNew, pReprNew, "dch_choice_link");')
    edit('src/proof/dch/dchChoice.c',
         '    Aig_ManSetRegNum( pChoices, Aig_ManRegNum(pAig) );',
         '    Aig_ManSetRegNum( pChoices, Aig_ManRegNum(pAig) );\n    Flow_DchChoiceCopy(pAig, pChoices);')
    for name in ('flow.c', 'flow.h'):
        shutil.copyfile(ROOT/'native/abc'/name, source/'src/base/main'/name)
    with (source/'src/base/main/module.make').open('a') as f:
        f.write('\nSRC += src/base/main/flow.c\n')


def build(jobs=4):
    base = ROOT/'build/abc'
    base.mkdir(parents=True, exist_ok=True)
    lock = json.loads((ROOT/'native/abc/lock.json').read_text())
    fingerprint = hashlib.sha256(''.join(sha(p) for p in [Path(__file__), *sorted((ROOT/'native/abc').iterdir())]).encode()).hexdigest()
    archive, source, stamp = base/'source.tar.gz', base/'upstream', base/'prepared.json'
    previous = json.loads(stamp.read_text()) if stamp.exists() else {}
    if not archive.exists():
        target = archive.with_suffix('.download')
        with urllib.request.urlopen(f"https://codeload.github.com/{lock['repository']}/tar.gz/{lock['commit']}", timeout=120) as response, target.open('wb') as f:
            shutil.copyfileobj(response, f)
        if sha(target) != lock['sha256']: raise ValueError('ABC source archive checksum mismatch')
        target.replace(archive)
    if sha(archive) != lock['sha256']: raise ValueError('ABC source archive checksum mismatch')
    if previous.get('fingerprint') != fingerprint:
        source.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='prepare-', dir=base) as temp:
            prepared = Path(temp)
            subprocess.run(['tar','-xzf',str(archive),'--strip-components=1','-C',str(prepared)],check=True)
            instrument(prepared)
            for p in prepared.rglob('*'):
                dest = source/p.relative_to(prepared)
                if p.is_dir(): dest.mkdir(parents=True, exist_ok=True)
                elif not dest.is_file() or sha(p) != sha(dest): shutil.copy2(p,dest)
        files = ['src/base/main/flow.c','src/base/main/flow.h','src/base/main/module.make',
                 'src/base/cmd/cmdUtils.c','src/base/io/ioWriteBlif.c','src/aig/aig/aigObj.c','src/aig/gia/giaNf.c','src/base/abci/abcDar.c','src/base/abc/abcNetlist.c',
                 'src/proof/cec/cecSatG2.c', 'src/proof/dch/dchSweep.c', 'src/proof/dch/dchChoice.c',
                 'src/base/abci/abcStrash.c', 'src/base/abci/abc.c', 'src/base/abc/abcUtil.c']
        previous = {'fingerprint':fingerprint,'commit':lock['commit'],'files':{n:sha(source/n) for n in files}}
        stamp.write_text(json.dumps(previous,indent=2)+'\n')
    for name, digest in previous['files'].items():
        if sha(source/name) != digest: raise ValueError(f'ABC prepared source changed: {name}')
    subprocess.run(['make','-C',str(source),f'-j{jobs}','ABC_USE_NO_READLINE=1','abc'],check=True)
    output = base/'install/bin/yosys-abc'
    output.parent.mkdir(parents=True,exist_ok=True)
    if not output.is_file() or sha(output)!=sha(source/'abc'): shutil.copy2(source/'abc',output)
    return output


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs',type=int,default=4)
    args=parser.parse_args()
    if args.jobs<1: parser.error('--jobs must be positive')
    print(build(args.jobs))
