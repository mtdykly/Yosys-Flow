#!/usr/bin/env bash
# Capture only: no Python replay, archive, baseline synthesis or index build.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
hyp_out="${HYP_OUT:-$PWD/runs/hyp-capture-lazy-$(date +%Y%m%d-%H%M%S)}"
hyp_native="${HYP_NATIVE:-$PWD/build/native/install/bin/yosys}"
hyp_cpu="${HYP_CPU:-0}"
[[ "$hyp_out" = /* ]] || hyp_out="$PWD/$hyp_out"
[[ "$hyp_native" = /* ]] || hyp_native="$PWD/$hyp_native"
[[ ! -e "$hyp_out" ]] || { echo "Output already exists: $hyp_out" >&2; exit 2; }
[[ -x "$hyp_native" ]] || { echo "Native Yosys missing: $hyp_native" >&2; exit 2; }
unset YOSYS_FLOW_NATIVE_AUDIT YOSYS_FLOW_NATIVE_FULL_CHECKPOINTS
unset YOSYS_FLOW_NATIVE_FULL_ARRAYS YOSYS_FLOW_ABC_FULL_INTERFACES
unset YOSYS_FLOW_ABC_FULL_SNAPSHOTS YOSYS_FLOW_ABC_SCAN_SNAPSHOTS
unset YOSYS_FLOW_ABC_VERIFY_DIRTY YOSYS_FLOW_ABC_CHECKPOINT_INTERVAL
export YOSYS_FLOW_CAPTURE_PROFILE=1 YOSYS_FLOW_ABC_TRACE=1
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export ABC="$PWD/build/abc/install/bin/yosys-abc"
python3 - "$hyp_out" "$hyp_native" "$hyp_cpu" <<'PY'
import hashlib,json,os,sys,uuid
from pathlib import Path
from yosys_flow.capture import traced_script
from yosys_flow.common import token
out,native,cpu=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
source=Path('build/epfl-large-screen/sources/hyp.v').resolve()
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(source)=='ca578a27ce121d7f4ae9f56b02f071661118871534d314e187635b1b2309e653', 'hyp input changed'
out.mkdir(parents=True,exist_ok=False)
(out/'traced.ys').write_text(traced_script(out,uuid.uuid4().hex,
    f'read_verilog -sv -defer {token(source)}\nhierarchy -check -top top', 'synth -top top'))
(out/'measurement.json').write_text(json.dumps({'mode':'standalone_capture',
    'native':str(native),'native_sha256':sha(native),'abc_sha256':sha(Path(os.environ['ABC'])),
    'source_sha256':sha(source),'cpu':cpu,'profile':True,'background_replay':False,
    'script_sha256':sha(out/'traced.ys')},indent=2)+'\n')
PY
echo "hyp standalone capture starting; CPU: $hyp_cpu; output: $hyp_out"
echo "Includes RTL parsing, synth, provenance and netlist writes; no replay or index."
/usr/bin/time -v -o "$hyp_out/time.txt" \
  timeout --kill-after=15s "${HYP_TIMEOUT:-7200}s" \
  taskset -c "$hyp_cpu" "$hyp_native" -Q -T -t \
  -E "$hyp_out/frontend.d" -s "$hyp_out/traced.ys" 2>&1 | tee "$hyp_out/traced.log"
echo "Capture completed. Timing: $hyp_out/time.txt"
