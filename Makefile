PYTHON ?= python3
NATIVE_JOBS ?= 4
DEMO_OUT ?= runs/synth-demo

.PHONY: all native demo test test-native clean
all: native

native:
	$(PYTHON) tools/build_native.py --jobs $(NATIVE_JOBS)

demo: all
	$(PYTHON) -m yosys_flow run --source examples/proc.v --top demo --out "$(DEMO_OUT)"
	$(PYTHON) -m yosys_flow query "$(DEMO_OUT)" --source examples/proc.v --line 13
	$(PYTHON) -m yosys_flow extract "$(DEMO_OUT)" --source examples/proc.v --line 13 --out "$(DEMO_OUT)/selected.v"

test: all
	$(PYTHON) -m unittest discover -s tests -v

test-native: test

clean:
	rm -rf build
