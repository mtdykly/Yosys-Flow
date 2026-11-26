#!/usr/bin/env bash
# Reproduce this fixed hyp AND->OR edit; does not claim a cold arbitrary ECO.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."

fresh=0
dry=0
for arg in "$@"; do
  case "$arg" in
    --fresh-archive) fresh=1 ;;
    --dry-run) dry=1 ;;
    *) echo "Usage: bash experiments/tools/rerun_hyp.sh [--fresh-archive] [--dry-run]" >&2; exit 2 ;;
  esac
done
hyp_suite="${HYP_OUT:-$PWD/runs/hyp-rerun-$(date +%Y%m%d-%H%M%S)}"
hyp_cpu="${HYP_CPU:-0}"
hyp_stock="/opt/oss-cad-suite/bin/yosys"
hyp_native="$PWD/build/native/install/bin/yosys"
hyp_old="$PWD/build/epfl-large-screen/sources/hyp.v"
hyp_new="${HYP_NEW_RTL:-$PWD/runs/hyp-rerun-20261115-060717/replay-inputs/new.v}"
hyp_pairs="${HYP_PAIRS:-$PWD/runs/hyp-rerun-20261115-060717/replay-inputs/partition-checkpoint-constants.json}"
hyp_base="${HYP_BASE_RUN:-$PWD/runs/hyp-rerun-20261115-060717/base-functional}"
[[ "$hyp_suite" = /* ]] || hyp_suite="$PWD/$hyp_suite"
[[ ! -e "$hyp_suite" ]] || { echo "Output already exists: $hyp_suite" >&2; exit 2; }
for file in "$hyp_stock" "$hyp_native" "$hyp_old" "$hyp_new"; do
  [[ -f "$file" ]] || { echo "Missing prerequisite: $file" >&2; exit 2; }
done
if (( fresh )); then
  [[ -f "$hyp_pairs" ]] || { echo "Missing candidate pairs to re-prove: $hyp_pairs" >&2; exit 2; }
  hyp_base="$hyp_suite/base-functional"
else
  [[ -f "$hyp_base/manifest.json" ]] || { echo "Missing verified archive: $hyp_base" >&2; exit 2; }
fi

# Fix the corpus and the exact edit before spending hours on synthesis.
python3 - "$hyp_old" "$hyp_new" <<'PY'
import hashlib,sys
from pathlib import Path
old,new=(Path(p).read_bytes() for p in sys.argv[1:])
assert hashlib.sha256(old).hexdigest()=='ca578a27ce121d7f4ae9f56b02f071661118871534d314e187635b1b2309e653', 'hyp input differs from pinned EPFL file'
a,b=old.decode().splitlines(),new.decode().splitlines()
assert len(a)==len(b)==239751
changed=[i for i,(x,y) in enumerate(zip(a,b)) if x!=y]
assert changed==[239741] and a[239741].replace(' & ',' | ')==b[239741], 'expected fixed n214713 AND->OR edit'
PY
echo "hyp output: $hyp_suite; CPU: $hyp_cpu; fresh archive: $fresh"
unset YOSYS_FLOW_ABC_FULL_SNAPSHOTS YOSYS_FLOW_ABC_SCAN_SNAPSHOTS
unset YOSYS_FLOW_ABC_VERIFY_DIRTY YOSYS_FLOW_ABC_CHECKPOINT_INTERVAL
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
if (( ! dry )); then mkdir -p "$hyp_suite"; fi

stage=setup
trap 'echo "Failed at $stage; inspect $hyp_suite/$stage.log. No later stage was started." >&2' ERR
run_stage() {
  stage="$1"; local limit="$2"; shift 2
  echo "[$stage] starting (limit ${limit}s)"
  if (( dry )); then printf '  %q ' "$@"; printf '\n'; return; fi
  /usr/bin/time -f 'wall_seconds=%e\npeak_rss_kib=%M\nexit_status=%x' \
    -o "$hyp_suite/$stage.time.txt" \
    timeout --kill-after=15s "${limit}s" taskset -c "$hyp_cpu" "$@" \
    > "$hyp_suite/$stage.log" 2>&1
  echo "[$stage] completed"
}

if (( fresh )); then
  run_stage archive 21600 python3 -u -m yosys_flow run --flow synth \
    --source "$hyp_old" --top top --yosys "$hyp_stock" --native-yosys "$hyp_native" \
    --index-backend sqlite --out "$hyp_suite/base-raw"
  # The authenticated reader resolves this capture's actual signal binding.
  # Added provenance events can change object IDs without changing the circuit.
  run_stage region-proof 21600 python3 -u provenance/tools/refine_partitions.py \
    "$hyp_suite/base-raw" --input-wire n214713 --input-module top --input-type '$_AND_' \
    --out "$hyp_suite/region-proof" --publish "$hyp_base" \
    --reuse-pairs "$hyp_pairs" --node-limits 64 256 4096 \
    --sat-timeout 60 --cec-timeout 360 --cec-attempts 256 \
    --checkpoint-bridges --recorded-rewrites
fi

if (( ! dry )); then
  mkdir "$hyp_suite/full-new"
  python3 - "$hyp_new" "$hyp_suite" <<'PY'
import json,sys
from pathlib import Path
source,root=sys.argv[1],Path(sys.argv[2])
commands=['read_verilog -sv -defer '+json.dumps(source),'hierarchy -check -top top','synth -top top']
for command,extension in [('write_json','json'),('write_rtlil','il'),('write_verilog -noattr','v')]:
    commands.append(command+' '+json.dumps(str(root/'full-new'/('netlist.'+extension))))
(root/'full-new.ys').write_text('\n'.join(commands)+'\n')
PY
fi
run_stage full-new 3600 "$hyp_stock" -Q -T -s "$hyp_suite/full-new.ys"
# auto builds its own reference after candidate generation. The separate F
# above starts from complete RTL and is the comparable full-synthesis timing.
run_stage incremental 7200 python3 -u -m incremental auto \
  --base-run "$hyp_base" --new-source "$hyp_new" \
  --yosys "$hyp_stock" --native-yosys "$hyp_native" \
  --selection-mode fine --no-equivalence-precheck \
  --yosys-timeout 3600 --whole-equivalence-timeout 1200 --out "$hyp_suite/incremental"

if (( ! dry )); then
  python3 - "$hyp_suite" "$fresh" "$hyp_base" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);stages={}
for path in root.glob('*.time.txt'):
    stages[path.name.removesuffix('.time.txt')]={k:float(v) for line in path.read_text().splitlines() if '=' in line for k,v in [line.split('=',1)]}
auto=json.loads((root/'incremental/report.json').read_text())
assert auto['status']=='passed', 'incremental run did not publish a validated deliverable'
replacement=json.loads((root/'incremental/replacement/report.json').read_text())
t=auto['timings'];r=replacement['timings']
report={'status':'passed','fresh_archive':bool(int(sys.argv[2])),'base_run':sys.argv[3],
        'scope':'fixed known hyp ECO; archived candidate pairs are re-proved when building fresh',
        'stages':stages,'F_new_RTL_to_netlist_seconds':stages['full-new']['wall_seconds'],
        'P_fine_new_RTL_to_candidate_seconds':t['netlist_generation_seconds'],
        'new_reference_from_frontend_seconds':r['new_full_synth_seconds'],
        'whole_equivalence_seconds':r['whole_equivalence_seconds'],
        'post_generation_validation_seconds':t['post_generation_validation_seconds'],
        'automatic_total_seconds':t['total_seconds'],
        'whole_equivalence':replacement['whole_equivalence']['status'],
        'verilog_equivalence':replacement['verilog_equivalence']['status'],
        'note':'F command wall time includes process startup/exit; P generation is the application timer; reference/proof are included in post-generation time, do not sum twice'}
(root/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
PY
fi
if (( dry )); then
  echo "Command preview only; no synthesis or archive work was started."
else
  echo "hyp finished: $hyp_suite"
fi
