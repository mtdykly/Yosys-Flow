PYTHON ?= python3
NATIVE_JOBS ?= 4
DEMO_OUT ?= runs/synth-demo
INCREMENTAL_OUT ?= runs/incremental-demo

.PHONY: all native demo incremental-demo test test-native clean
all: native

native:
	$(PYTHON) provenance/tools/build_native.py --jobs $(NATIVE_JOBS)

demo: all
	$(PYTHON) -m yosys_flow run --source provenance/examples/comb.v --top demo --out "$(DEMO_OUT)"
	$(PYTHON) -m yosys_flow query "$(DEMO_OUT)" --source provenance/examples/comb.v --line 4
	$(PYTHON) -m yosys_flow extract "$(DEMO_OUT)" --source provenance/examples/comb.v --line 4 --out "$(DEMO_OUT)/selected.v"

test: all
	$(PYTHON) provenance/tools/run_regression.py

test-native: test

incremental-demo: all
	$(PYTHON) -m incremental demo --out "$(INCREMENTAL_OUT)"

clean:
	rm -rf build
