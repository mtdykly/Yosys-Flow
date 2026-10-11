YOSYS_CONFIG ?= yosys-config
CXX = g++
PYTHON ?= python3

.PHONY: all demo test clean native test-native
all: build/provenance.so

build/trace_opt_merge.cc: vendor/opt_merge.cc vendor/opt_merge.sha256 tools/instrument.py plugin/recorder.h
	$(PYTHON) tools/instrument.py

build/provenance.so: build/trace_opt_merge.cc plugin/recorder.h
	$(YOSYS_CONFIG) --exec $(CXX) --cxxflags --ldflags -O2 -shared -o $@ $< --ldlibs

demo: all
	$(PYTHON) -m yosys_flow run --source examples/merge.v --top demo --out runs/merge
	$(PYTHON) -m yosys_flow query runs/merge --source examples/merge.v --line 2

test: all
	$(PYTHON) -m unittest discover -s tests -v

NATIVE_JOBS ?= 4
native:
	$(PYTHON) tools/build_native.py --jobs $(NATIVE_JOBS)

test-native: native
	$(PYTHON) -m unittest discover -s tests -p 'test_native.py' -v
	$(PYTHON) -m unittest discover -s tests -p 'test_synth.py' -v

clean:
	rm -rf build
