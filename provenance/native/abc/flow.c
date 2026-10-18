/* Passive internal ABC evidence. No pCopy/Value, traversal IDs or names changed.
 * Snapshot IDs scope object IDs: IDs from different snapshots are never identities.
 * Actual rewrite and selected mapping events are separate from command snapshots.
 */
#include "base/main/flow.h"
#include "map/mio/mio.h"
#include "misc/nm/nm.h"
ABC_NAMESPACE_IMPL_START
static FILE * flow_file;
static unsigned flow_seq, flow_command;
static unsigned flow_relations, flow_copies, flow_rewrites, flow_entry_copies;
/* This entry path is scoped to the first read/strash/get/fraig sequence. It is
 * cleared after fraig, before any manager address can be reused by later work. */
static Abc_Ntk_t *flow_entry_ntk, *flow_strash_ntk;
static Gia_Man_t *flow_entry_gia;
static unsigned flow_entry_snapshot, flow_strash_snapshot, flow_gia_snapshot;
static int flow_is_strash, flow_is_get, flow_is_put;
static unsigned flow_clone_count;
static Abc_Ntk_t *flow_clone_ntk;
static Vec_Int_t *flow_clone_links;
static char flow_path[4096];
static void flow_string(const char *s) {
    const unsigned char *p = (const unsigned char *)(s ? s : "");
    fputc('"', flow_file);
    for (; *p; p++) {
        if (*p == '"' || *p == '\\') fprintf(flow_file, "\\%c", *p);
        else if (*p < 32) fprintf(flow_file, "\\u%04x", *p);
        else fputc(*p, flow_file);
    }
    fputc('"', flow_file);
}
static unsigned flow_start(const char *kind) {
    if (!strcmp(kind,"proved_equivalence")) ++flow_relations;
    if (!strcmp(kind,"history_copy")) ++flow_copies;
    if (!strcmp(kind,"entry_copy")) ++flow_entry_copies;
    if (!strcmp(kind,"mapped_clone")) ++flow_clone_count;
    if (!strcmp(kind,"aig_replace")) ++flow_rewrites;
    fprintf(flow_file, "{\"seq\":%u,\"command_id\":%u,\"kind\":", ++flow_seq, flow_command);
    flow_string(kind); return flow_seq;
}
static void flow_end(void) {
    fputs("}\n", flow_file);
    if (fflush(flow_file) || ferror(flow_file)) { fprintf(stderr, "ABC provenance write failed\n"); exit(2); }
}
static void flow_node(int id, const char *op, int *first) {
    fprintf(flow_file, "%s{\"id\":%d,\"op\":", *first ? "" : ",", id); *first = 0; flow_string(op);
}
static void flow_ntk(Abc_Ntk_t *p, const char *kind) {
    Abc_Obj_t *o, *f; int i, j, first = 1;
    if (!flow_file || !p) return;
    flow_start(kind); fputs(",\"representation\":\"ntk\",\"pis\":[", flow_file);
    Abc_NtkForEachCi(p, o, i) { if(i) fputc(',',flow_file); fprintf(flow_file,"%d",o->Id); }
    fputs("],\"pos\":[",flow_file);
    Abc_NtkForEachCo(p, o, i) { if(i) fputc(',',flow_file); fprintf(flow_file,"%d",o->Id); }
    fputs("],\"nodes\":[",flow_file);
    Abc_NtkForEachObj(p, o, i) {
        const char *op = Abc_ObjIsCi(o) ? "pi" : o->Type == ABC_OBJ_CONST1 ? "one" :
            Abc_ObjIsCo(o) || Abc_ObjIsNet(o) ? "buf" :
            Abc_ObjIsNode(o) && Abc_NtkIsStrash(p) ? "and" :
            Abc_ObjIsNode(o) && (Abc_NtkHasSop(p) || Abc_NtkHasMapping(p)) ? "sop" : "unsupported";
        const char *name = Nm_ManFindNameById(p->pManName, o->Id);
        flow_node(o->Id,op,&first);
        fputs(",\"name\":",flow_file); flow_string(name);
        fputs(",\"fanins\":[",flow_file);
        Abc_ObjForEachFanin(o,f,j) {
            int inv = Abc_NtkIsStrash(p) ? Abc_ObjFaninC(o,j) : 0;
            if(j) fputc(',',flow_file);
            fprintf(flow_file,"%d",2*f->Id+inv);
        }
        fputc(']',flow_file);
        if (!strcmp(op,"sop")) {
            const char *sop = Abc_NtkHasMapping(p) ? (o->pData ? Mio_GateReadSop((Mio_Gate_t *)o->pData) : "1 1\n") : (const char *)o->pData;
            fputs(",\"sop\":",flow_file); flow_string(sop);
            if (Abc_NtkHasMapping(p) && o->pData) { fputs(",\"gate\":",flow_file); flow_string(Mio_GateReadName((Mio_Gate_t *)o->pData)); }
        }
        fputc('}',flow_file);
    }
    fputc(']',flow_file); flow_end();
}
static unsigned flow_gia(Gia_Man_t *p, const char *kind) {
    Gia_Obj_t *o; int i, first=1; unsigned seq;
    if (!flow_file || !p) return 0;
    seq=flow_start(kind); fputs(",\"representation\":\"gia\",\"pis\":[",flow_file);
    Gia_ManForEachCi(p,o,i) { if(i) fputc(',',flow_file); fprintf(flow_file,"%d",Gia_ObjId(p,o)); }
    fputs("],\"pos\":[",flow_file);
    Gia_ManForEachCo(p,o,i) { if(i) fputc(',',flow_file); fprintf(flow_file,"%d",Gia_ObjId(p,o)); }
    fputs("],\"po_names\":[",flow_file);
    Gia_ManForEachCo(p,o,i) { if(i) fputc(',',flow_file); flow_string(Gia_ObjCoName(p,i)); }
    fputs("],\"pi_names\":[",flow_file);
    Gia_ManForEachCi(p,o,i) { if(i) fputc(',',flow_file); flow_string(Gia_ObjCiName(p,i)); }
    fputs("],\"nodes\":[",flow_file);
    Gia_ManForEachObj(p,o,i) {
        const char *op = i==0 ? "zero" : Gia_ObjIsCi(o) ? "pi" : Gia_ObjIsCo(o) || Gia_ObjIsBuf(o) ? "buf" :
            Gia_ObjIsMux(p,o) ? "mux" : Gia_ObjIsXor(o) ? "xor" : "and";
        flow_node(i,op,&first); fputs(",\"fanins\":[",flow_file);
        if (Gia_ObjIsCo(o) || Gia_ObjIsBuf(o)) fprintf(flow_file,"%d",Gia_ObjFaninLit0(o,i));
        else if (Gia_ObjIsAnd(o)) {
            fprintf(flow_file,"%d,%d",Gia_ObjFaninLit0(o,i),Gia_ObjFaninLit1(o,i));
            if (Gia_ObjIsMux(p,o)) fprintf(flow_file,",%d",Gia_ObjFaninLit2(p,i));
        }
        fputs("]}",flow_file);
    }
    fputc(']',flow_file); flow_end(); return seq;
}
void Flow_CommandBefore(Abc_Frame_t *p, int argc, char **argv) {
    if (argc && !strcmp(argv[0],"version")) printf("Yosys-Flow ABC internal trace v3 (e026ed5380f3bdc3beea2ff9ffc23236fc549d5b)\n");
    if (!flow_file && getenv("YOSYS_FLOW_ABC_TRACE") && argc == 2 && !strcmp(argv[0],"read_blif")) {
        if (snprintf(flow_path,sizeof(flow_path),"%s.flow.jsonl",argv[1]) >= (int)sizeof(flow_path)) exit(2);
        flow_file=fopen(flow_path,"wx");
        if (!flow_file) { perror("ABC provenance open"); exit(2); }
        flow_seq=flow_command=0;
        flow_entry_ntk=flow_strash_ntk=NULL; flow_entry_gia=NULL;
        flow_entry_snapshot=flow_strash_snapshot=flow_gia_snapshot=0;
        flow_start("begin"); fputs(",\"schema\":3,\"abc_commit\":\"e026ed5380f3bdc3beea2ff9ffc23236fc549d5b\"",flow_file); flow_end();
    }
    if (!flow_file || !strcmp(argv[0],"echo")) return;
    flow_clone_count=0; flow_is_put=!strcmp(argv[0],"&put");
    flow_is_strash=!strcmp(argv[0],"strash"); flow_is_get=!strcmp(argv[0],"&get");
    flow_relations=flow_copies=flow_rewrites=flow_entry_copies=0;
    ++flow_command; flow_start("command_begin"); fputs(",\"argv\":[",flow_file);
    { int i; for(i=0;i<argc;i++){ if(i) fputc(',',flow_file); flow_string(argv[i]); } }
    fputc(']',flow_file); flow_end();
}
void Flow_CommandAfter(Abc_Frame_t *p, int argc, char **argv, int status) {
    if (!flow_file || !argc || !strcmp(argv[0],"echo")) return;
    if (!status && strcmp(argv[0],"read_library") && strcmp(argv[0],"write_blif")) {
        if (argv[0][0]=='&' && strcmp(argv[0],"&put")) flow_gia(Abc_FrameReadGia(p),"network");
        else flow_ntk(Abc_FrameReadNtk(p),"network");
    }
    if (!status && !strcmp(argv[0],"read_blif")) {
        flow_entry_ntk=Abc_FrameReadNtk(p); flow_entry_snapshot=flow_seq;
    }
    if (!strcmp(argv[0],"&fraig")) {
        flow_entry_ntk=flow_strash_ntk=NULL; flow_entry_gia=NULL;
    }
    flow_start("command_end"); fprintf(flow_file,",\"status\":%d,\"equivalences\":%u,\"history_copies\":%u,\"replacements\":%u,\"entry_copies\":%u,\"mapped_clones\":%u",status,flow_relations,flow_copies,flow_rewrites,flow_entry_copies,flow_clone_count); flow_end();
    if (!strcmp(argv[0],"write_blif") || status) {
        flow_start("end"); fprintf(flow_file,",\"status\":%d",status); flow_end(); fclose(flow_file); flow_file=NULL;
    }
}
void Flow_WriteBlif(Abc_Ntk_t *p, const char *file) {
    if (flow_file) { flow_ntk(p,"export_network"); }
}
static unsigned flow_aig(Aig_Man_t *p, const char *kind) {
    Aig_Obj_t *o; int i, first=1; unsigned seq;
    seq=flow_start(kind); fputs(",\"representation\":\"aig\",\"pis\":[",flow_file);
    Aig_ManForEachCi(p,o,i) { if(i) fputc(',',flow_file); fprintf(flow_file,"%d",o->Id); }
    fputs("],\"pos\":[",flow_file);
    Aig_ManForEachCo(p,o,i) { if(i) fputc(',',flow_file); fprintf(flow_file,"%d",o->Id); }
    fputs("],\"nodes\":[",flow_file);
    Aig_ManForEachObj(p,o,i) {
        const char *op = Aig_ObjIsConst1(o)?"one":Aig_ObjIsCi(o)?"pi":Aig_ObjIsCo(o)||Aig_ObjIsBuf(o)?"buf":Aig_ObjIsExor(o)?"xor":"and";
        flow_node(o->Id,op,&first); fputs(",\"fanins\":[",flow_file);
        if (o->pFanin0) fprintf(flow_file,"%d",2*Aig_ObjFanin0(o)->Id+Aig_ObjFaninC0(o));
        if (o->pFanin1) fprintf(flow_file,",%d",2*Aig_ObjFanin1(o)->Id+Aig_ObjFaninC1(o));
        fputs("]}",flow_file);
    }
    fputc(']',flow_file); flow_end(); return seq;
}
void Flow_AigReplace(Aig_Man_t *p, Aig_Obj_t *old, Aig_Obj_t *next) {
    unsigned snap;
    if (!flow_file) return;
    snap=flow_aig(p,"history_network");
    flow_start("aig_replace"); fprintf(flow_file,",\"snapshot\":%u,\"old\":%d,\"new_literal\":%d",snap,old->Id,2*Aig_Regular(next)->Id+Aig_IsComplement(next)); flow_end();
}
void Flow_GiaEquivalence(Gia_Man_t *p, Gia_Obj_t *old, Gia_Obj_t *repr, const char *reason) {
    unsigned snap;
    if (!flow_file) return;
    snap=flow_gia(p,"history_network");
    flow_start("proved_equivalence"); fprintf(flow_file,",\"snapshot\":%u,\"old\":%d,\"new_literal\":%d,\"reason\":",snap,Gia_ObjId(p,old),2*Gia_ObjId(p,repr)+(old->fPhase^repr->fPhase));
    flow_string(reason);
    if (p==flow_entry_gia) fprintf(flow_file,",\"entry_snapshot\":%u",flow_gia_snapshot);
    flow_end();
}
void Flow_AigEquivalence(Aig_Man_t *p, Aig_Obj_t *old, Aig_Obj_t *repr, const char *reason) {
    unsigned snap;
    if (!flow_file) return;
    snap=flow_aig(p,"history_network");
    flow_start("proved_equivalence"); fprintf(flow_file,",\"snapshot\":%u,\"old\":%d,\"new_literal\":%d,\"reason\":",snap,old->Id,2*repr->Id+(old->fPhase^repr->fPhase));
    flow_string(reason); flow_end();
}
void Flow_GiaSweepCopy(Gia_Man_t *from, Gia_Man_t *to) {
    unsigned before,after; Gia_Obj_t *o; int i,first=1;
    if (!flow_file) return;
    before=flow_gia(from,"history_network"); after=flow_gia(to,"history_network");
    flow_start("history_copy"); fprintf(flow_file,",\"reason\":\"fraig_sweep_copy\",\"before\":%u,\"after\":%u,\"links\":[",before,after);
    Gia_ManForEachObj(from,o,i) if(o->Value != ~0u) {
        fprintf(flow_file,"%s[%d,%u]",first?"":",",i,o->Value);first=0;
    }
    fputc(']',flow_file);
    if (from==flow_entry_gia) fprintf(flow_file,",\"entry_snapshot\":%u",flow_gia_snapshot);
    flow_end();
}
void Flow_DchChoiceCopy(Aig_Man_t *from, Aig_Man_t *to) {
    unsigned before,after; Aig_Obj_t *o; int i,first=1;
    if (!flow_file) return;
    before=flow_aig(from,"history_network"); after=flow_aig(to,"history_network");
    flow_start("history_copy"); fprintf(flow_file,",\"reason\":\"dch_choice_copy\",\"before\":%u,\"after\":%u,\"links\":[",before,after);
    Aig_ManForEachObj(from,o,i) if(!Aig_ObjIsCo(o) && o->pData) {
        Aig_Obj_t *next=(Aig_Obj_t *)o->pData;
        fprintf(flow_file,"%s[%d,%d]",first?"":",",o->Id,2*Aig_Regular(next)->Id+Aig_IsComplement(next));first=0;
    }
    fputc(']',flow_file); flow_end();
}
static void flow_copy_begin(const char *reason, unsigned before, unsigned after) {
    flow_start("entry_copy"); fprintf(flow_file,",\"reason\":"); flow_string(reason);
    fprintf(flow_file,",\"before\":%u,\"after\":%u,\"links\":[",before,after);
}
void Flow_StrashCopy(Abc_Ntk_t *from, Abc_Ntk_t *to) {
    Abc_Obj_t *o; int i,first=1;
    if (!flow_file || !flow_is_strash || from!=flow_entry_ntk || flow_strash_ntk) return;
    flow_ntk(to,"history_network"); flow_strash_snapshot=flow_seq; flow_strash_ntk=to;
    flow_copy_begin("entry_strash",flow_entry_snapshot,flow_strash_snapshot);
    Abc_NtkForEachObj(from,o,i) if(!Abc_ObjIsCo(o) && o->pCopy) {
        fprintf(flow_file,"%s[%d,%d]",first?"":",",o->Id,2*Abc_ObjRegular(o->pCopy)->Id+Abc_ObjIsComplement(o->pCopy)); first=0;
    }
    fputc(']',flow_file); flow_end();
}
void Flow_GetCopy(Abc_Ntk_t *from, Aig_Man_t *aig, Gia_Man_t *gia) {
    Abc_Obj_t *o; Aig_Obj_t *a; int i,j,first=1; unsigned ntk_snap,aig_snap;
    if (!flow_file || !flow_is_get || from!=flow_strash_ntk || flow_entry_gia) return;
    flow_ntk(from,"history_network"); ntk_snap=flow_seq;
    /* Between this specific strash hook and &get, only cleanup can delete
     * nodes; it does not create or renumber nodes. Record surviving objects. */
    flow_copy_begin("entry_strash_survivors",flow_strash_snapshot,ntk_snap);
    Abc_NtkForEachObj(from,o,i) {
        fprintf(flow_file,"%s[%d,%d]",first?"":",",o->Id,2*o->Id);first=0;
    }
    fputc(']',flow_file); flow_end();
    aig_snap=flow_aig(aig,"history_network");
    flow_copy_begin("entry_ntk_to_aig",ntk_snap,aig_snap); first=1;
    Abc_NtkForEachObj(from,o,i) if(!Abc_ObjIsCo(o) && o->pCopy) {
        /* Do not dereference a possibly retired pCopy. Only live AIG objects
         * from the returned manager can establish a conversion. */
        Aig_ManForEachObj(aig,a,j) if(a==Aig_Regular((Aig_Obj_t *)o->pCopy)) {
            fprintf(flow_file,"%s[%d,%d]",first?"":",",o->Id,2*a->Id+Aig_IsComplement((Aig_Obj_t *)o->pCopy));first=0;break;
        }
    }
    fputc(']',flow_file); flow_end();
    flow_gia_snapshot=flow_gia(gia,"history_network"); flow_entry_gia=gia;
    flow_copy_begin("entry_aig_to_gia",aig_snap,flow_gia_snapshot);first=1;
    Aig_ManForEachObj(aig,a,i) if(!Aig_ObjIsCo(a) && a->iData) {
        fprintf(flow_file,"%s[%d,%d]",first?"":",",a->Id,a->iData);first=0;
    }
    fputc(']',flow_file); flow_end();
}
void Flow_OutputClone(Abc_Ntk_t *ntk, Abc_Obj_t *from, Abc_Obj_t *to) {
    unsigned snapshot, event;
    if (!flow_file || !flow_is_put || !Abc_NtkHasMapping(ntk)) return;
    flow_ntk(ntk,"history_network"); snapshot=flow_seq;
    event=flow_start("mapped_clone");
    fprintf(flow_file,",\"snapshot\":%u,\"old\":%d,\"new\":%d",snapshot,from->Id,to->Id);flow_end();
    if (!flow_clone_links) flow_clone_links=Vec_IntAlloc(16);
    if (flow_clone_ntk!=ntk) Vec_IntClear(flow_clone_links);
    flow_clone_ntk=ntk;
    Vec_IntPush(flow_clone_links,from->Id);Vec_IntPush(flow_clone_links,to->Id);Vec_IntPush(flow_clone_links,event);
}
void Flow_NfMapping(Gia_Man_t *p) {
    int i,j; unsigned snap;
    if (!flow_file) return;
    snap=flow_gia(p,"mapping_network");
    Gia_ManForEachCell(p,i) {
        flow_start("mapped_cut"); fprintf(flow_file,",\"snapshot\":%u,\"root_literal\":%d,\"leaves\":[",snap,i);
        if (Gia_ObjIsCellInv(p,i)) fprintf(flow_file,"%d",i^1);
        else if (Gia_ObjIsCellBuf(p,i)) fprintf(flow_file,"%d",Gia_ObjFaninLit0(Gia_ManObj(p,i/2),i/2));
        else for(j=0;j<Gia_ObjCellSize(p,i);j++){ if(j)fputc(',',flow_file);fprintf(flow_file,"%d",Gia_ObjCellFanin(p,i,j)); }
        fprintf(flow_file,"],\"gate_id\":%d",Gia_ObjIsCellInv(p,i)?-1:Gia_ObjIsCellBuf(p,i)?-2:Gia_ObjCellId(p,i)); flow_end();
    }
}
void Flow_FromMappedGia(Gia_Man_t *p, Abc_Ntk_t *ntk, Vec_Int_t *copies) {
    unsigned before, after; int lit, id, first=1;
    if (!flow_file) return;
    before=flow_gia(p,"mapped_gia"); flow_ntk(ntk,"mapped_ntk"); after=flow_seq;
    flow_start("gia_to_ntk"); fprintf(flow_file,",\"before\":%u,\"after\":%u,\"links\":[",before,after);
    Vec_IntForEachEntry(copies,id,lit) if(id >= 0) {
        /* MakeSimpleCos recreates output constants and can delete the original
         * constant object without updating vCopyLits. Such a stale entry is not
         * a surviving copy; do not infer the new constant's identity from it.
         * Keep unexpected missing nonconstant targets for the reader to reject. */
        if (lit < 2 && !Abc_NtkObj(ntk,id)) continue;
        fprintf(flow_file,"%s[%d,%d]",first?"":",",lit,id); first=0;
    }
    fputs("],\"clones\":[",flow_file);
    if (flow_clone_ntk==ntk && flow_clone_links) {
        for (id=0;id<Vec_IntSize(flow_clone_links);id+=3) {
            fprintf(flow_file,"%s[%d,%d,%d]",id?",":"",Vec_IntEntry(flow_clone_links,id),Vec_IntEntry(flow_clone_links,id+1),Vec_IntEntry(flow_clone_links,id+2));
        }
    }
    fputc(']',flow_file); flow_end();
    if (flow_clone_links) Vec_IntFree(flow_clone_links);
    flow_clone_links=NULL;flow_clone_ntk=NULL;
}
void Flow_ToNetlist(Abc_Ntk_t *from, Abc_Ntk_t *to) {
    unsigned before, after; Abc_Obj_t *o; int i,first=1;
    if (!flow_file) return;
    flow_ntk(from,"netlist_source"); before=flow_seq; flow_ntk(to,"netlist_target"); after=flow_seq;
    flow_start("ntk_to_netlist"); fprintf(flow_file,",\"before\":%u,\"after\":%u,\"links\":[",before,after);
    Abc_NtkForEachObj(from,o,i) if(o->pCopy) {
        fprintf(flow_file,"%s[%d,%d]",first?"":",",o->Id,Abc_ObjRegular(o->pCopy)->Id); first=0;
    }
    fputc(']',flow_file); flow_end();
}
ABC_NAMESPACE_IMPL_END
