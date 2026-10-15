"""Configure one CPython build where it runs, and record what the parse and
the gates need from there.

    python3 capture.py OUT

Run inside the build's own userland (a container of its image, under QEMU
user mode for another architecture; or in a chroot of the same files in a
VM; or on a GitHub runner), in the build directory OUT, which holds
capture-spec.json: the build's name, its compiler (CC), configure options,
directories put first on PATH, make targets to run after configure, and the
CPython source directory. It writes into OUT:

- configure's outputs (pyconfig.h, Makefile, config.log, ...) and
  configure.log; prepare.log for the extra make targets;
- make-n.txt, `make -n` for libpython's archive and the shared modules the
  census parses (as tools/coverage/cpython_build.py's compile_commands asks
  it), and compile-commands.json, each C file's compile command;
- predefined/: for each distinct set of flags those commands pass (the
  flags that can change what the compiler defines before any source, as
  cpython_build.macro_flags keeps them), the compiler's `-dM -E` (KEY.h),
  `-E -v` (KEY.E-v.txt) and, for clang, `-###` (KEY.frontend.txt), with
  flag-sets.json saying which flags and files each KEY is; KEY is
  cpython_build.compiler_view's (the same flags give the same name);
- kernel-view.txt: what the kernel tells a program here (uname, sysconf,
  the auxiliary vector, /proc/cpuinfo), to compare QEMU user mode with a
  real kernel;
- packages.txt, every installed package; environment.part.json, the rest
  of the record (the OS, the compiler, libclang beside a clang, the tools,
  the steps' commands and environments), which the caller completes into
  environment.json.

Only the standard library is used, so it runs on any Python 3.10 or later
the userland has.
"""

import datetime
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

OUT = Path(sys.argv[1]).resolve()
SPEC = json.loads((OUT / "capture-spec.json").read_text())
SOURCE = Path(SPEC["source"])
CC = shlex.split(SPEC["compiler"])
ENV = dict(os.environ)
ENV.pop("HOSTNAME", None)
if SPEC.get("path"):
    ENV["PATH"] = os.pathsep.join([*SPEC["path"], ENV.get("PATH", "")])
# cpython_build.LIBRARY and SHARED_MODULES
LIBRARY = ("Objects/", "Python/", "Modules/")
SHARED_MODULES = (
    "math", "cmath", "_struct", "array", "_pickle", "_json", "_csv",
    "unicodedata", "binascii", "_random", "_heapq", "_bisect", "_queue",
    "_statistics", "_zoneinfo", "_asyncio",
)  # fmt: skip
# names of variables that may hold credentials, never written out (a run
# on a GitHub runner uploads the record)
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_KEY$|^KEY", re.I)
# cpython_build.NOT_MACRO_FLAGS and NOT_MACRO_PAIRS
NOT_MACRO_FLAGS = ("-I", "-D", "-U", "-W", "-M", "-o", "-c", "-iquote")
NOT_MACRO_PAIRS = ("-o", "-MF", "-MT", "-MQ", "-I", "-D", "-U", "-iquote", "-include")


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    command: list[str], log: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command in OUT, its output to a log file there or captured."""
    env = env or ENV
    if log is None:
        return subprocess.run(
            command, cwd=OUT, env=env, capture_output=True, text=True, check=False
        )
    with (OUT / log).open("w") as f:
        return subprocess.run(
            command, cwd=OUT, env=env, stdout=f, stderr=subprocess.STDOUT,
            text=True, check=False,
        )  # fmt: skip


def output_of(command: list[str]) -> str:
    try:
        done = run(command)
    except OSError:
        return ""
    return (done.stdout.strip() or done.stderr.strip()) if done.returncode == 0 else ""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def step(command: list[str], log: str, status: int, env: dict[str, str]) -> dict[str, Any]:
    return {
        "command": command,
        "cwd": str(OUT),
        "environment": {k: v for k, v in sorted(env.items()) if not SECRET_NAME.search(k)},
        "log": log,
        "exit": status,
    }


def makefile_var(makefile: str, name: str) -> str:
    m = re.search(rf"^{name}=[ \t]*(.*?)[ \t]*$", makefile, re.MULTILINE)
    return m.group(1) if m else ""


def targets() -> list[str]:
    """cpython_build.library and shared_objects: libpython's archive, named
    with the ABI flags, and the shared modules' object files."""
    makefile = (OUT / "Makefile").read_text()
    out = [f"libpython{makefile_var(makefile, 'VERSION')}{makefile_var(makefile, 'ABIFLAGS')}.a"]
    for name in SHARED_MODULES:
        m = re.search(
            rf"^Modules/{re.escape(name)}\$\(EXT_SUFFIX\):([^;\n]*)", makefile, re.MULTILINE
        )
        if m:
            out += [t for t in m.group(1).split() if t.endswith(".o")]
    return out


def commands_from(text: str) -> dict[str, list[str]]:
    """cpython_build.commands_from: each compile command, by source."""
    commands: dict[str, list[str]] = {}
    for line in text.replace("\\\n", " ").splitlines():
        if not line.startswith(CC[0]) or " -c " not in line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        sources = [Path(os.path.normpath(OUT / t)) for t in tokens if t.endswith(".c")]
        if len(sources) != 1:
            continue
        try:
            rel = sources[0].relative_to(SOURCE).as_posix()
        except ValueError:
            continue
        if rel.startswith(LIBRARY):
            commands[rel] = tokens
    return commands


def macro_flags(tokens: list[str]) -> list[str]:
    """cpython_build.macro_flags."""
    out: list[str] = []
    skip = False
    for t in tokens[1:]:
        if skip:
            skip = False
        elif t in NOT_MACRO_PAIRS:
            skip = True
        elif t.startswith(NOT_MACRO_FLAGS) or t.endswith(".c"):
            continue
        else:
            out.append(t)
    return out


def flag_sets(commands: dict[str, list[str]]) -> dict[str, Any]:
    """The compiler's macros, include directories and front-end options for
    each distinct set of flags."""
    folder = OUT / "predefined"
    folder.mkdir(exist_ok=True)
    sets: dict[str, dict[str, Any]] = {}
    for rel, tokens in sorted(commands.items()):
        flags = macro_flags(tokens)
        key = hashlib.sha1(json.dumps([CC, flags]).encode()).hexdigest()[:12]
        if key in sets:
            sets[key]["files"].append(rel)
            continue
        empty = ["-x", "c", "/dev/null"]
        macros = run([*CC, *flags, "-dM", "-E", *empty])
        (folder / f"{key}.h").write_text(macros.stdout)
        verbose = run([*CC, *flags, "-E", "-v", *empty])
        (folder / f"{key}.E-v.txt").write_text(verbose.stderr)
        search = re.search(
            r"#include <\.\.\.> search starts here:\n(.*?)\nEnd of search list",
            verbose.stderr,
            re.DOTALL,
        )
        entry: dict[str, Any] = {
            "flags": flags,
            "macros": f"predefined/{key}.h",
            "macros_exit": macros.returncode,
            "verbose": f"predefined/{key}.E-v.txt",
            "include_dirs": [d.strip() for d in search.group(1).splitlines()] if search else [],
        }
        if "#define __clang__" in macros.stdout:
            frontend = run([*CC, *flags, "-###", "-c", *empty, "-o", "/dev/null"])
            (folder / f"{key}.frontend.txt").write_text(frontend.stderr)
            entry["frontend"] = f"predefined/{key}.frontend.txt"
        entry["files"] = [rel]
        sets[key] = entry
    (folder / "flag-sets.json").write_text(json.dumps(sets, indent=1) + "\n")
    return sets


def kernel_view() -> None:
    """What the kernel tells a program here."""
    parts = []
    for title, command in (
        ("uname -a", ["uname", "-a"]),
        ("uname -m", ["uname", "-m"]),
        ("nproc", ["nproc"]),
        ("getconf -a", ["getconf", "-a"]),
        ("auxv (LD_SHOW_AUXV=1 /bin/true)", ["env", "LD_SHOW_AUXV=1", "/bin/true"]),
        ("ulimit -a", ["sh", "-c", "ulimit -a"]),
        ("/proc/cpuinfo", ["cat", "/proc/cpuinfo"]),
        ("/proc/sys/kernel/osrelease", ["cat", "/proc/sys/kernel/osrelease"]),
        ("mounts", ["cat", "/proc/self/mounts"]),
    ):
        done = run(command)
        parts.append(f"==== {title} (exit {done.returncode})\n{done.stdout}{done.stderr}")
    (OUT / "kernel-view.txt").write_text("\n".join(parts))


def packages() -> dict[str, Any]:
    if shutil.which("dpkg-query"):
        text = output_of(["dpkg-query", "-W", "-f", "${Package}:${Architecture}=${Version}\n"])
    else:
        text = output_of(["apk", "info", "-v"])
    listed = sorted(text.splitlines())
    (OUT / "packages.txt").write_text("\n".join(listed) + "\n")
    return {
        "file": "packages.txt",
        "count": len(listed),
        "sha256": sha256_of(OUT / "packages.txt"),
    }


def package_of(path: Path) -> dict[str, str]:
    if shutil.which("dpkg"):
        owner = output_of(["dpkg", "-S", str(path)]).split(":", 1)[0]
        if not owner:
            return {}
        version = output_of(["dpkg-query", "-W", "-f", "${Version}", owner])
        return {"package": owner, "package_version": version}
    owner = output_of(["apk", "info", "--who-owns", str(path)])
    m = re.search(r"is owned by (\S+)", owner)
    return {"package": m.group(1)} if m else {}


def compiler() -> dict[str, Any]:
    found = shutil.which(CC[0], path=ENV.get("PATH"))
    path = Path(found).resolve() if found else None
    return {
        "command": SPEC["compiler"],
        "path": str(path) if path else "",
        "version": output_of([*CC, "--version"]),
        "target": output_of([*CC, "-dumpmachine"]),
        **(package_of(path) if path else {}),
    }


def libclang(version_text: str) -> dict[str, Any]:
    """The libclang beside a clang: the library of the same LLVM release,
    packaged with it, and its Python bindings."""
    m = re.search(r"clang version (\d+)\.(\d+)\.(\d+)", version_text)
    if not m:
        return {}
    major = m.group(1)
    found = []
    for pattern in (
        f"/usr/lib/llvm-{major}/lib/libclang-{major}.so*",
        f"/usr/lib/llvm-{major}/lib/libclang.so.{major}*",
        f"/usr/lib/llvm{major}/lib/libclang.so.{major}*",
    ):
        found += sorted(Path("/").glob(pattern.lstrip("/")))
    if not found:
        return {}
    library = found[0].resolve()
    bindings = ""
    for candidate in (
        "/usr/lib/python3/dist-packages/clang/cindex.py",
        *[str(p) for p in Path("/usr/lib").glob("python3*/site-packages/clang/cindex.py")],
    ):
        if Path(candidate).is_file():
            bindings = str(Path(candidate).resolve().parent)
            break
    return {
        "version": ".".join(m.groups()),
        "path": str(library),
        "sha256": sha256_of(library),
        **package_of(library),
        "bindings": {"path": bindings, **(package_of(Path(bindings)) if bindings else {})},
    }


def os_release() -> dict[str, str]:
    text = Path("/etc/os-release").read_text()
    release = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', text, re.MULTILINE))
    return {
        "pretty_name": release.get("PRETTY_NAME", ""),
        "id": release.get("ID", ""),
        "version_id": release.get("VERSION_ID", ""),
        "machine": platform.machine(),
        "kernel": platform.release(),
    }


def tools() -> dict[str, str]:
    first = lambda text: text.splitlines()[0] if text else ""  # noqa: E731
    return {
        "make": first(output_of(["make", "--version"])),
        "python3": output_of(["python3", "--version"]),
        "capture_python": f"{platform.python_version()} ({sys.executable})",
        "sh": str(Path("/bin/sh").resolve()),
        "binutils": first(output_of(["ld", "--version"])),
        "libc": first(output_of(["ldd", "--version"])) or first(run(["ldd", "--version"]).stderr),
        "pkg-config": output_of(["pkg-config", "--version"]),
        **(
            {"path clang": first(output_of(["clang", "--version"]))}
            if SPEC.get("path")
            else {}
        ),
    }


def main() -> None:
    record: dict[str, Any] = {"started": now()}
    kernel_view()
    record["os"] = os_release()
    record["compiler"] = compiler()
    record["libclang"] = libclang(record["compiler"]["version"])
    record["tools"] = tools()
    record["packages"] = packages()
    env = dict(ENV, CC=SPEC["compiler"], PYTHONDONTWRITEBYTECODE="1")
    command = ["sh", str(SOURCE / "configure"), *SPEC["configure"]]
    done = run(command, "configure.log", env)
    record["configure"] = step(command, "configure.log", done.returncode, env)
    record["status"] = "configured" if done.returncode == 0 else f"failed: configure exit {done.returncode}"
    if done.returncode == 0 and SPEC.get("prepare"):
        # the JIT's stencils: as many clang processes as Python counts CPUs
        prepare_env = dict(env, PYTHON_CPU_COUNT=os.environ.get("PYTHON_CPU_COUNT", "4"))
        command = ["make", *SPEC["prepare"]]
        done = run(command, "prepare.log", prepare_env)
        record["prepare"] = step(command, "prepare.log", done.returncode, prepare_env)
        if done.returncode:
            record["status"] = f"failed: make {' '.join(SPEC['prepare'])} exit {done.returncode}"
    if record["status"] == "configured":
        command = ["make", "-n", *targets()]
        done = run(command, env=env)
        (OUT / "make-n.txt").write_text(done.stdout)
        commands = commands_from(done.stdout)
        (OUT / "compile-commands.json").write_text(
            json.dumps(commands, indent=1, sort_keys=True) + "\n"
        )
        record["make_n"] = {
            "command": command,
            "exit": done.returncode,
            "output": "make-n.txt",
            "compile_commands": "compile-commands.json",
            "files": len(commands),
        }
        sets = flag_sets(commands)
        record["predefined"] = {"folder": "predefined", "index": "predefined/flag-sets.json", "sets": len(sets)}
        front_ends = [s["frontend"] for s in sets.values() if "frontend" in s]
        if front_ends:
            record["frontend"] = front_ends
        if done.returncode:
            record["status"] = f"failed: make -n exit {done.returncode}"
    patchlevel = (SOURCE / "Include/patchlevel.h").read_text()
    version = re.search(r'#define PY_VERSION\s+"([^"]+)"', patchlevel)
    record["cpython"] = {"version": version.group(1) if version else "", "source": str(SOURCE)}
    record["finished"] = now()
    (OUT / "environment.part.json").write_text(json.dumps(record, indent=1) + "\n")
    print(record["status"])


if __name__ == "__main__":
    main()
