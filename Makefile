PYTHON ?= python3
NATIVE_JOBS ?= 4
DEMO_OUT ?= runs/synth-demo

.PHONY: all native demo test test-native clean
all: native

native:
	$(PYTHON) tools/build_native.py --jobs $(NATIVE_JOBS)

demo: all
	$(PYTHON) -m yosys_flow run --source examples/comb.v --top demo --out "$(DEMO_OUT)"
	$(PYTHON) -m yosys_flow query "$(DEMO_OUT)" --source examples/comb.v --line 4
	$(PYTHON) -m yosys_flow extract "$(DEMO_OUT)" --source examples/comb.v --line 4 --out "$(DEMO_OUT)/selected.v"

test: all
	$(PYTHON) tools/run_regression.py

test-native: test

clean:
	rm -rf build
