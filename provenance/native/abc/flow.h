#ifndef YOSYS_FLOW_ABC_H
#define YOSYS_FLOW_ABC_H
#include "base/abc/abc.h"
#include "base/main/main.h"
#include "aig/aig/aig.h"
#include "aig/gia/gia.h"
ABC_NAMESPACE_HEADER_START
void Flow_CommandBefore(Abc_Frame_t *, int, char **);
void Flow_CommandAfter(Abc_Frame_t *, int, char **, int);
void Flow_WriteBlif(Abc_Ntk_t *, const char *);
void Flow_AigReplace(Aig_Man_t *, Aig_Obj_t *, Aig_Obj_t *);
void Flow_GiaEquivalence(Gia_Man_t *, Gia_Obj_t *, Gia_Obj_t *, const char *);
void Flow_AigEquivalence(Aig_Man_t *, Aig_Obj_t *, Aig_Obj_t *, const char *);
void Flow_GiaSweepCopy(Gia_Man_t *, Gia_Man_t *);
void Flow_GiaCleanupCopy(Gia_Man_t *, Gia_Man_t *);
void Flow_DchChoiceCopy(Aig_Man_t *, Aig_Man_t *);
void Flow_StrashCopy(Abc_Ntk_t *, Abc_Ntk_t *);
void Flow_GetCopy(Abc_Ntk_t *, Aig_Man_t *, Gia_Man_t *);
void Flow_OutputClone(Abc_Ntk_t *, Abc_Obj_t *, Abc_Obj_t *);
void Flow_NfMapping(Gia_Man_t *);
void Flow_FromMappedGia(Gia_Man_t *, Abc_Ntk_t *, Vec_Int_t *);
void Flow_ToNetlist(Abc_Ntk_t *, Abc_Ntk_t *);
ABC_NAMESPACE_HEADER_END
#endif
