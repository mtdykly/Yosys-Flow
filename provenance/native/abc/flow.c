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
static Gia_Man_t *flow_cleanup_from;
static unsigned flow_cleanup_snapshot;
static unsigned flow_entry_snapshot, flow_strash_snapshot, flow_gia_snapshot;
static int flow_is_strash, flow_is_get, flow_is_put;
static unsigned flow_clone_count;
static Abc_Ntk_t *flow_clone_ntk;
static Vec_Int_t *flow_clone_links;
static char flow_path[4096];
/* Encoding state only: never used as evidence that two ABC objects are copies.
 * Each decoded snapshot retains its original (sequence, object ID) identity.
 * Reset across commands/managers. Periodic full checkpoints are diagnostic,
 * not required to replay the lossless chain. Never infer identity from this. */
typedef struct Flow_NodeState_ {
    const char *op;
    int fanins[3], count;
    unsigned seen;
} Flow_NodeState;
static Flow_NodeState *flow_nodes;
static int flow_nodes_size;
static void *flow_manager;
static const char *flow_rep;
static unsigned flow_snapshot, flow_epoch, flow_observations;
static int flow_delta;
static unsigned flow_checkpoint_interval;
static int flow_force_full, flow_force_scan, flow_verify_dirty, flow_sparse_command;
static int flow_aig_valid, flow_dirty_size;
static unsigned char *flow_dirty_marks;
static Vec_Int_t *flow_dirty;
static unsigned long long flow_aig_full_visits, flow_aig_dirty_visits;
static void flow_reset_nodes(void) {
    ABC_FREE(flow_nodes); flow_nodes_size=0; flow_manager=NULL;
    ABC_FREE(flow_dirty_marks); flow_dirty_size=0;
    if (flow_dirty) Vec_IntFree(flow_dirty);
    flow_dirty=NULL; flow_aig_valid=0;
    flow_snapshot=0; flow_observations=0; flow_rep=NULL;
}
/* Observe mutations, never write ABC scratch fields or modify its graph.
 * Only the currently encoded manager needs a dirty set: switching managers
 * always takes a full checkpoint. Store IDs, not recycled object pointers. */
void Flow_AigTouch(Aig_Man_t *p, Aig_Obj_t *o) {
    int size;
    if (!flow_file || flow_manager!=p || !flow_rep || strcmp(flow_rep,"aig")) return;
    if (o->Id>=flow_dirty_size) {
        size=flow_dirty_size?flow_dirty_size:64;
        while (size<=o->Id) size*=2;
        flow_dirty_marks=ABC_REALLOC(unsigned char,flow_dirty_marks,size);
        memset(flow_dirty_marks+flow_dirty_size,0,size-flow_dirty_size); flow_dirty_size=size;
    }
    if (flow_dirty_marks[o->Id]) return;
    flow_dirty_marks[o->Id]=1;
    if (!flow_dirty) flow_dirty=Vec_IntAlloc(16);
    Vec_IntPush(flow_dirty,o->Id);
}
void Flow_AigTouchObject(Aig_Obj_t *o) {
    Aig_Man_t *p;
    if (!flow_file || !flow_manager || !flow_rep || strcmp(flow_rep,"aig")) return;
    p=(Aig_Man_t *)flow_manager;
    if (o->Id>=0 && o->Id<Aig_ManObjNumMax(p) && Aig_ManObj(p,o->Id)==o) Flow_AigTouch(p,o);
}
void Flow_AigInvalidate(Aig_Man_t *p) {
    if (flow_manager==p && flow_rep && !strcmp(flow_rep,"aig")) flow_aig_valid=0;
}
void Flow_AigStop(Aig_Man_t *p) {
    if (flow_manager==p && flow_rep && !strcmp(flow_rep,"aig")) flow_reset_nodes();
}
static void flow_encoding(void *manager, const char *rep, const char *kind, unsigned seq) {
    int same=flow_manager==manager && flow_rep && !strcmp(flow_rep,rep);
    if (!strcmp(rep,"aig") && !flow_aig_valid) same=0;
    flow_delta=same && !strcmp(kind,"history_network") && !flow_force_full &&
        (!flow_checkpoint_interval || flow_observations<flow_checkpoint_interval);
    if (!flow_delta) { flow_reset_nodes(); ++flow_epoch; }
    fprintf(flow_file,",\"encoding\":\"%s\",\"network_epoch\":%u",flow_delta?"delta":"checkpoint",flow_epoch);
    if (flow_delta) fprintf(flow_file,",\"base_snapshot\":%u",flow_snapshot);
    flow_manager=manager; flow_rep=rep; flow_snapshot=seq; ++flow_observations;
}
static int flow_changed(int id, const char *op, int count, int a, int b, int c) {
    Flow_NodeState *old;
    int changed, size;
    if (id>=flow_nodes_size) {
        size=flow_nodes_size?flow_nodes_size:64;
        while (size<=id) size*=2;
        flow_nodes=ABC_REALLOC(Flow_NodeState,flow_nodes,size);
        memset(flow_nodes+flow_nodes_size,0,(size-flow_nodes_size)*sizeof(Flow_NodeState));
        flow_nodes_size=size;
    }
    old=flow_nodes+id;
    changed=!flow_delta || !old->seen || strcmp(old->op,op) || old->count!=count ||
            old->fanins[0]!=a || old->fanins[1]!=b || old->fanins[2]!=c;
    old->op=op; old->count=count; old->fanins[0]=a; old->fanins[1]=b; old->fanins[2]=c; old->seen=flow_snapshot;
    return changed;
}
static void flow_deleted(void) {
    int i,first=1;
    fputs(",\"deleted_nodes\":[",flow_file);
    for(i=0;i<flow_nodes_size;i++) if(flow_nodes[i].seen && flow_nodes[i].seen!=flow_snapshot) {
        fprintf(flow_file,"%s%d",first?"":",",i); first=0; flow_nodes[i].seen=0;
    }
    fputc(']',flow_file);
}
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
    if (ferror(flow_file)) { fprintf(stderr, "ABC provenance write failed\n"); exit(2); }
}
static void flow_flush(void) {
    if (fflush(flow_file) || ferror(flow_file)) { fprintf(stderr, "ABC provenance flush failed\n"); exit(2); }
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
    seq=flow_start(kind); flow_encoding(p,"gia",kind,seq);
    fputs(",\"representation\":\"gia\",\"pis\":[",flow_file);
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
        int count=Gia_ObjIsCo(o)||Gia_ObjIsBuf(o)?1:Gia_ObjIsAnd(o)?(Gia_ObjIsMux(p,o)?3:2):0;
        int a=count?Gia_ObjFaninLit0(o,i):0, b=count>1?Gia_ObjFaninLit1(o,i):0, c=count>2?Gia_ObjFaninLit2(p,i):0;
        if (!flow_changed(i,op,count,a,b,c)) continue;
        flow_node(i,op,&first); fputs(",\"fanins\":[",flow_file);
        if (Gia_ObjIsCo(o) || Gia_ObjIsBuf(o)) fprintf(flow_file,"%d",Gia_ObjFaninLit0(o,i));
        else if (Gia_ObjIsAnd(o)) {
            fprintf(flow_file,"%d,%d",Gia_ObjFaninLit0(o,i),Gia_ObjFaninLit1(o,i));
            if (Gia_ObjIsMux(p,o)) fprintf(flow_file,",%d",Gia_ObjFaninLit2(p,i));
        }
        fputs("]}",flow_file);
    }
    fputc(']',flow_file); flow_deleted(); flow_end(); return seq;
}
void Flow_CommandBefore(Abc_Frame_t *p, int argc, char **argv) {
    if (argc && !strcmp(argv[0],"version")) printf("Yosys-Flow ABC internal trace v5 (e026ed5380f3bdc3beea2ff9ffc23236fc549d5b)\n");
    if (!flow_file && getenv("YOSYS_FLOW_ABC_TRACE") && argc == 2 && !strcmp(argv[0],"read_blif")) {
        if (snprintf(flow_path,sizeof(flow_path),"%s.flow.jsonl",argv[1]) >= (int)sizeof(flow_path)) exit(2);
        flow_file=fopen(flow_path,"wx");
        if (!flow_file) { perror("ABC provenance open"); exit(2); }
        setvbuf(flow_file,NULL,_IOFBF,1<<20);
        flow_force_full=getenv("YOSYS_FLOW_ABC_FULL_SNAPSHOTS") && !strcmp(getenv("YOSYS_FLOW_ABC_FULL_SNAPSHOTS"),"1");
        flow_force_scan=getenv("YOSYS_FLOW_ABC_SCAN_SNAPSHOTS") && !strcmp(getenv("YOSYS_FLOW_ABC_SCAN_SNAPSHOTS"),"1");
        flow_verify_dirty=getenv("YOSYS_FLOW_ABC_VERIFY_DIRTY") && !strcmp(getenv("YOSYS_FLOW_ABC_VERIFY_DIRTY"),"1");
        flow_checkpoint_interval=0;
        if (getenv("YOSYS_FLOW_ABC_CHECKPOINT_INTERVAL")) {
            char *end; const char *s=getenv("YOSYS_FLOW_ABC_CHECKPOINT_INTERVAL");
            unsigned long n=strtoul(s,&end,10);
            if (!*s || *end || *s=='-' || n>1000000000UL) { fprintf(stderr,"Invalid ABC checkpoint interval\n"); exit(2); }
            flow_checkpoint_interval=(unsigned)n;
        }
        flow_aig_full_visits=flow_aig_dirty_visits=0;
        flow_seq=flow_command=flow_epoch=0; flow_reset_nodes();
        flow_entry_ntk=flow_strash_ntk=NULL; flow_entry_gia=NULL;
        flow_entry_snapshot=flow_strash_snapshot=flow_gia_snapshot=0;
        flow_start("begin"); fputs(",\"schema\":5,\"abc_commit\":\"e026ed5380f3bdc3beea2ff9ffc23236fc549d5b\"",flow_file); flow_end();
    }
    if (!flow_file || !strcmp(argv[0],"echo")) return;
    flow_reset_nodes();
    flow_sparse_command=!strcmp(argv[0],"dc2") || !strcmp(argv[0],"&dch");
    flow_cleanup_from=NULL; flow_cleanup_snapshot=0;
    flow_clone_count=0; flow_is_put=!strcmp(argv[0],"&put");
    flow_is_strash=!strcmp(argv[0],"strash"); flow_is_get=!strcmp(argv[0],"&get");
    flow_relations=flow_copies=flow_rewrites=flow_entry_copies=0;
    ++flow_command; flow_start("command_begin"); fputs(",\"argv\":[",flow_file);
    { int i; for(i=0;i<argc;i++){ if(i) fputc(',',flow_file); flow_string(argv[i]); } }
    fputc(']',flow_file); flow_end(); flow_flush();
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
    flow_flush();
    if (!strcmp(argv[0],"write_blif") || status) {
        flow_start("end"); fprintf(flow_file,",\"status\":%d,\"capture_stats\":{\"aig_full_node_visits\":%llu,\"aig_dirty_node_visits\":%llu,\"checkpoint_interval\":%u,\"scan_snapshots\":%d}",status,flow_aig_full_visits,flow_aig_dirty_visits,flow_checkpoint_interval,flow_force_scan); flow_end(); flow_flush();
        if (fclose(flow_file)) { fprintf(stderr,"ABC provenance close failed\n"); exit(2); } flow_file=NULL;
    }
}
void Flow_WriteBlif(Abc_Ntk_t *p, const char *file) {
    if (flow_file) { flow_ntk(p,"export_network"); }
}
static int flow_aig_state(Aig_Obj_t *o, const char **op, int *a, int *b) {
    *op=Aig_ObjIsConst1(o)?"one":Aig_ObjIsCi(o)?"pi":Aig_ObjIsCo(o)||Aig_ObjIsBuf(o)?"buf":Aig_ObjIsExor(o)?"xor":"and";
    *a=o->pFanin0?2*Aig_ObjFanin0(o)->Id+Aig_ObjFaninC0(o):0;
    *b=o->pFanin1?2*Aig_ObjFanin1(o)->Id+Aig_ObjFaninC1(o):0;
    return (o->pFanin0!=NULL)+(o->pFanin1!=NULL);
}
static void flow_aig_node(Aig_Obj_t *o, int *first) {
    const char *op; int a,b,count=flow_aig_state(o,&op,&a,&b);
    if (!flow_changed(o->Id,op,count,a,b,0)) return;
    flow_node(o->Id,op,first); fputs(",\"fanins\":[",flow_file);
    if (count) fprintf(flow_file,"%d",a);
    if (count>1) fprintf(flow_file,",%d",b);
    fputs("]}",flow_file);
}
static void flow_aig_audit(Aig_Man_t *p) {
    int i;
    for (i=0;i<Aig_ManObjNumMax(p) || i<flow_nodes_size;i++) {
        Aig_Obj_t *o=i<Aig_ManObjNumMax(p)?Aig_ManObj(p,i):NULL;
        Flow_NodeState *n=i<flow_nodes_size?flow_nodes+i:NULL;
        int ok=(!o)==(!n || !n->seen);
        if (ok && o) {
            const char *op; int a,b,count=flow_aig_state(o,&op,&a,&b);
            ok=n && n->seen && !strcmp(n->op,op) && n->count==count && n->fanins[0]==a && n->fanins[1]==b;
        }
        if (!ok) { fprintf(stderr,"ABC dirty capture missed object %d\n",i); exit(2); }
    }
}
static unsigned flow_aig(Aig_Man_t *p, const char *kind) {
    Aig_Obj_t *o; int i,id,first=1,sparse; unsigned seq;
    seq=flow_start(kind); flow_encoding(p,"aig",kind,seq);
    sparse=flow_delta && flow_sparse_command && !flow_force_scan;
    fputs(",\"representation\":\"aig\",\"pis\":[",flow_file);
    Aig_ManForEachCi(p,o,i) { if(i) fputc(',',flow_file); fprintf(flow_file,"%d",o->Id); }
    fputs("],\"pos\":[",flow_file);
    Aig_ManForEachCo(p,o,i) { if(i) fputc(',',flow_file); fprintf(flow_file,"%d",o->Id); }
    fputs("],\"nodes\":[",flow_file);
    if (sparse) {
        if (flow_dirty) {
            Vec_IntSort(flow_dirty,0);
            Vec_IntForEachEntry(flow_dirty,id,i) {
                ++flow_aig_dirty_visits;
                o=Aig_ManObj(p,id);
                if (o) flow_aig_node(o,&first);
            }
        }
        fputs("],\"deleted_nodes\":[",flow_file); first=1;
        if (flow_dirty) Vec_IntForEachEntry(flow_dirty,id,i) {
            if (!Aig_ManObj(p,id) && id<flow_nodes_size && flow_nodes[id].seen) {
                fprintf(flow_file,"%s%d",first?"":",",id); first=0; flow_nodes[id].seen=0;
            }
        }
        fputc(']',flow_file);
        if (flow_verify_dirty) flow_aig_audit(p);
    } else {
        Aig_ManForEachObj(p,o,i) { ++flow_aig_full_visits; flow_aig_node(o,&first); }
        fputc(']',flow_file); flow_deleted();
    }
    if (flow_dirty) {
        Vec_IntForEachEntry(flow_dirty,id,i) flow_dirty_marks[id]=0;
        Vec_IntClear(flow_dirty);
    }
    flow_aig_valid=1;
    flow_end(); return seq;
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
    flow_cleanup_from=to; flow_cleanup_snapshot=after;
    flow_start("history_copy"); fprintf(flow_file,",\"reason\":\"fraig_sweep_copy\",\"before\":%u,\"after\":%u,\"links\":[",before,after);
    Gia_ManForEachObj(from,o,i) if(o->Value != ~0u) {
        fprintf(flow_file,"%s[%d,%u]",first?"":",",i,o->Value);first=0;
    }
    fputc(']',flow_file);
    if (from==flow_entry_gia) fprintf(flow_file,",\"entry_snapshot\":%u",flow_gia_snapshot);
    flow_end();
}
void Flow_GiaCleanupCopy(Gia_Man_t *from, Gia_Man_t *to) {
    unsigned after; Gia_Obj_t *o; int i,first=1;
    if (!flow_file) return;
    if (from!=flow_cleanup_from || !flow_cleanup_snapshot) {
        fprintf(stderr,"ABC cleanup provenance has no preceding sweep\n"); exit(2);
    }
    after=flow_gia(to,"history_network");
    flow_start("history_copy");
    fprintf(flow_file,",\"reason\":\"fraig_cleanup_copy\",\"before\":%u,\"after\":%u,\"links\":[",flow_cleanup_snapshot,after);
    /* Gia_ManDupMarked initializes Value to ~0u, then writes the actual
     * destination literal for every retained object, including constant/COs.
     * Read it before the caller destroys the old manager. */
    Gia_ManForEachObj(from,o,i) if(o->Value != ~0u) {
        fprintf(flow_file,"%s[%d,%u]",first?"":",",i,o->Value); first=0;
    }
    fputs("],\"deleted\":[",flow_file); first=1;
    Gia_ManForEachObj(from,o,i) if(o->Value == ~0u) {
        fprintf(flow_file,"%s%d",first?"":",",i); first=0;
    }
    fputc(']',flow_file); flow_end();
    flow_cleanup_from=NULL; flow_cleanup_snapshot=0;
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
typedef struct Flow_LiveAig_ { ABC_PTRUINT_T address; int id; } Flow_LiveAig;
static int flow_live_compare(const void *left, const void *right) {
    ABC_PTRUINT_T a=((const Flow_LiveAig *)left)->address, b=((const Flow_LiveAig *)right)->address;
    return (a>b)-(a<b);
}
void Flow_GetCopy(Abc_Ntk_t *from, Aig_Man_t *aig, Gia_Man_t *gia) {
    Abc_Obj_t *o; Aig_Obj_t *a; int i,live_count=0,first=1; unsigned ntk_snap,aig_snap;
    Flow_LiveAig *live;
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
    live=ABC_ALLOC(Flow_LiveAig,Aig_ManObjNumMax(aig));
    Aig_ManForEachObj(aig,a,i) {
        live[live_count].address=(ABC_PTRUINT_T)a; live[live_count++].id=a->Id;
    }
    qsort(live,live_count,sizeof(*live),flow_live_compare);
    Abc_NtkForEachObj(from,o,i) if(!Abc_ObjIsCo(o) && o->pCopy) {
        /* Do not dereference a possibly retired pCopy. Only live AIG objects
         * from the returned manager can establish a conversion. An address
         * index replaces the old all-live-objects scan for every source. */
        Flow_LiveAig key,*found; key.address=(ABC_PTRUINT_T)Aig_Regular((Aig_Obj_t *)o->pCopy);
        found=(Flow_LiveAig *)bsearch(&key,live,live_count,sizeof(*live),flow_live_compare);
        if(found) {
            fprintf(flow_file,"%s[%d,%d]",first?"":",",o->Id,2*found->id+Aig_IsComplement((Aig_Obj_t *)o->pCopy));first=0;
        }
    }
    ABC_FREE(live);
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
