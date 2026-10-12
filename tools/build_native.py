"""Build the pinned native provenance recorder for synth and checkpoints.

Requires CMake >= 3.28, Ninja, C++20, Bison, Flex, m4, and zlib headers.
Downloads only immutable, SHA256-verified source archives into build/native.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

from instrument_native import instrument

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--cmake", help="CMake >= 3.28；可使用仓库本地安装")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--abc", default=shutil.which("yosys-abc"))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    base = ROOT / "build/native"
    base.mkdir(parents=True, exist_ok=True)
    source = base / "upstream"
    lock = json.loads((ROOT / "native/lock.json").read_text())
    inputs = sorted((ROOT / "native").glob("*")) + [ROOT / "tools/instrument_native.py"]
    fingerprint = hashlib.sha256("".join(sha(p) for p in inputs if p.is_file()).encode()).hexdigest()
    stamp = base / "prepared.json"
    previous = json.loads(stamp.read_text()) if stamp.exists() else {}
    if previous.get("fingerprint") != fingerprint:
        with tempfile.TemporaryDirectory(prefix="prepare-", dir=base) as temporary:
            prepared = Path(temporary)
            for name, meta in lock.items():
                archive = base / (name + ".tar.gz")
                if not archive.exists():
                    url = f"https://codeload.github.com/{meta['repository']}/tar.gz/{meta['commit']}"
                    temporary = archive.with_suffix(".download")
                    urllib.request.urlretrieve(url, temporary)
                    if sha(temporary) != meta["sha256"]:
                        raise ValueError(f"Archive hash mismatch: {name}")
                    os.replace(temporary, archive)
                if sha(archive) != meta["sha256"]:
                    raise ValueError(f"Archive hash mismatch: {name}")
                destination = prepared / meta["directory"]
                destination.mkdir(parents=True, exist_ok=True)
                subprocess.run(["tar", "-xzf", str(archive), "--strip-components=1", "-C", str(destination)], check=True)
            instrument(prepared)
            # Preserve mtimes of unchanged files so a recorder edit does not rebuild
            # every upstream translation unit. Never modify the installed tool.
            for path in prepared.rglob("*"):
                destination = source / path.relative_to(prepared)
                if path.is_symlink():
                    if destination.is_symlink() and os.readlink(destination) == os.readlink(path):
                        continue
                    if destination.exists() or destination.is_symlink():
                        destination.unlink()
                    destination.symlink_to(os.readlink(path))
                elif path.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif not destination.is_file() or sha(path) != sha(destination):
                    shutil.copy2(path, destination)
            changed = ["kernel/flow_provenance.cc", "kernel/flow_provenance.h", "kernel/process_provenance.inc", "kernel/CMakeLists.txt",
                       "kernel/register.cc", "kernel/rtlil.cc", "frontends/ast/ast.cc", "frontends/ast/ast.h",
                       "frontends/ast/flow_sources.inc", "frontends/ast/genrtlil.cc", "passes/proc/proc_mux.cc",
                       "passes/proc/proc_dff.cc", "passes/proc/proc_dlatch.cc", "passes/proc/proc_init.cc", "passes/opt/opt_merge.cc",
                       "passes/opt/opt_expr.cc", "passes/opt/opt_dff.cc", "passes/opt/opt_muxtree.cc", "passes/opt/opt_reduce.cc", "passes/opt/opt_clean/wires.cc",
                       "passes/techmap/techmap.cc", "passes/techmap/alumacc.cc", "passes/techmap/abc.cc", "passes/hierarchy/hierarchy.cc", "passes/proc/proc_arst.cc", "passes/proc/proc_prune.cc", "passes/proc/proc_clean.cc", "passes/proc/proc_rmdead.cc"]
            previous = {"fingerprint": fingerprint, "files": {p: sha(source / p) for p in changed}}
            stamp.write_text(json.dumps(previous, indent=2) + "\n")
    for name, expected in previous["files"].items():
        if sha(source / name) != expected:
            raise ValueError(f"Prepared source changed outside instrumentation: {name}")
    if args.prepare_only:
        return
    if not args.abc:
        parser.error("provide --abc /path/to/yosys-abc")
    local_cmake = base / "python/cmake/data/bin/cmake"
    cmake = args.cmake or (str(local_cmake) if local_cmake.is_file() else "cmake")
    version = subprocess.check_output([cmake, "--version"], text=True)
    match = re.search(r"cmake version (\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (3, 28):
        parser.error("CMake >= 3.28 is required; see docs/native-m1.md for local installation")
    build = base / "build"
    options = ["-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_DISABLE_FIND_PACKAGE_Git=TRUE",
               "-DYOSYS_CHECKOUT_INFO=0e82bbefe-provenance", "-DYOSYS_WITHOUT_SLANG=ON",
               "-DYOSYS_WITHOUT_READLINE=ON", "-DYOSYS_WITHOUT_EDITLINE=ON",
               "-DYOSYS_WITHOUT_TCL=ON", "-DYOSYS_WITHOUT_LIBFFI=ON",
               f"-DYOSYS_ABC_EXECUTABLE={Path(args.abc).resolve()}", f"-DCMAKE_INSTALL_PREFIX={base / 'install'}"]
    env = os.environ.copy()
    # Also accept dependencies unpacked locally, without sudo/system installs.
    local_tools = base / "toolchain/usr"
    for name, variable in (("bison", "BISON_EXECUTABLE"), ("flex", "FLEX_EXECUTABLE"), ("m4", "BISON_M4_EXECUTABLE")):
        executable = local_tools / "bin" / name
        if executable.is_file():
            options.append(f"-D{variable}={executable}")
    if (local_tools / "share/bison").is_dir():
        env["BISON_PKGDATADIR"] = str(local_tools / "share/bison")
    if (local_tools / "bin/m4").is_file():
        env["M4"] = str(local_tools / "bin/m4")
    library = local_tools / "lib/x86_64-linux-gnu"
    if library.is_dir():
        env["LD_LIBRARY_PATH"] = str(library) + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    subprocess.run([cmake, "-S", str(source), "-B", str(build), "-G", "Ninja", *options], env=env, check=True)
    subprocess.run([cmake, "--build", str(build), "-j", str(args.jobs)], env=env, check=True)
    subprocess.run([cmake, "--install", str(build)], env=env, check=True)
    print(base / "install/bin/yosys")


if __name__ == "__main__":
    main()
