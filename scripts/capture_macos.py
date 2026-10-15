#!/usr/bin/env python3
"""Record a macOS runner's toolchain and the C compile commands of a build.

Subcommands:
  env      runner, OS, Xcode, SDK and compiler versions
  build    parse a `make -n` listing into per-file compile commands, then run
           the compiler once per distinct flag set for its predefined macros
           (-dM -E) and include search directories (-E -v)
  step     record a command line with its working directory and environment
  combine  merge the parts into one JSON file

Nothing here copies SDK headers or preprocessed output: only versions, paths,
command lines, macro definitions from -dM and include directory lists.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from capture_common import (  # noqa: E402
    add_common_subcommands,
    file_facts,
    filtered_env,
    run,
    write_json,
)

SOURCE_EXT = (".c", ".m", ".cc", ".cpp", ".S", ".s")
SEPARATORS = {";", ";;", "&&", "||", "|", "&", "(", ")"}
REDIRECTS = {"<", ">", ">>", ">&", "<&", "&>", "&>>", ">|"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SHELL_WORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "exec", "time", "env"}

# Options whose argument is a separate token.
ARG_STRIP = {
    "-o", "-I", "-D", "-U", "-MF", "-MT", "-MQ", "-iquote", "-isystem",
    "-idirafter", "-include", "-imacros", "-iprefix", "-iwithprefix",
}
ARG_KEEP = {"-arch", "-isysroot", "-target", "--target", "-x", "-Xclang", "-mllvm", "--sysroot"}
PREFIX_STRIP = (
    "-I", "-D", "-U", "-W", "-MF", "-MT", "-MQ", "-iquote", "-isystem",
    "-idirafter", "-fdiagnostics", "-fcolor-diagnostics",
    "-fno-color-diagnostics", "-fmessage-length",
)
EXACT_STRIP = {"-c", "-MMD", "-MD", "-MP", "-M", "-MM"}

MAKEFILE_VARS = [
    "CC", "CXX", "MAINCC", "CPP", "LINKCC", "AR", "OPT", "BASECFLAGS",
    "CONFIGURE_CFLAGS", "CONFIGURE_CFLAGS_NODIST", "CONFIGURE_CPPFLAGS",
    "CONFIGURE_LDFLAGS", "CFLAGS", "CFLAGS_NODIST", "CPPFLAGS", "LDFLAGS",
    "PY_CFLAGS", "PY_CFLAGS_NODIST", "PY_CPPFLAGS", "PY_CORE_CFLAGS",
    "PY_STDMODULE_CFLAGS", "PY_BUILTIN_MODULE_CFLAGS", "CCSHARED", "LDSHARED",
    "BLDSHARED", "MACHDEP", "MULTIARCH", "PLATFORM_TRIPLET", "HOST_GNU_TYPE",
    "BUILD_GNU_TYPE", "MACOSX_DEPLOYMENT_TARGET", "IPHONEOS_DEPLOYMENT_TARGET",
    "UNIVERSALSDK", "UNIVERSAL_ARCH_FLAGS", "ABIFLAGS", "SOABI", "CONFIG_ARGS",
    "PY_ENABLE_SHARED", "PYTHON_FOR_REGEN", "PYTHON_FOR_BUILD",
    "REGEN_JIT_COMMAND", "JIT_STENCILS_H", "CFLAGS_JIT", "CFLAGS_CEVAL",
    "LIBHACL_SIMD128_FLAGS", "LIBHACL_SIMD256_FLAGS", "LIBHACL_CFLAGS",
    "LIBMPDEC_CFLAGS", "LIBEXPAT_CFLAGS",
]


# ---------------------------------------------------------------- env


def first_line(result: dict) -> str | None:
    text = (result.get("stdout") or "") + (result.get("stderr") or "")
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def env_command(args) -> None:
    cmds = {
        "sw_vers": ["sw_vers"],
        "uname": ["uname", "-a"],
        "sysctl": ["sysctl", "hw.model", "hw.ncpu", "hw.memsize", "machdep.cpu.brand_string"],
        "xcode_select_path": ["xcode-select", "-p"],
        "xcodebuild_version": ["xcodebuild", "-version"],
        "xcodebuild_showsdks": ["xcodebuild", "-showsdks"],
        "xcrun_version": ["xcrun", "--version"],
        "xcrun_find_clang": ["xcrun", "--find", "clang"],
        "xcrun_clang_version": ["xcrun", "clang", "--version"],
        "which_compilers": ["which", "-a", "cc", "clang", "gcc"],
        "cc_version": ["cc", "--version"],
        "clang_version": ["clang", "--version"],
        "gcc_version": ["gcc", "--version"],
        "cc_dash_v": ["cc", "-E", "-v", "-x", "c", "/dev/null"],
        "ld_version": ["ld", "-v"],
        "clt_package": ["pkgutil", "--pkg-info=com.apple.pkg.CLTools_Executables"],
        "make_version": ["make", "--version"],
        "gmake_version": ["gmake", "--version"],
        "which_python3": ["which", "-a", "python3"],
        "python3_version": ["python3", "-VV"],
        "brew_version": ["brew", "--version"],
        "brew_list_versions": ["brew", "list", "--versions"],
        "pkg_config_version": ["pkg-config", "--version"],
    }
    results = {name: run(argv) for name, argv in cmds.items()}
    sdks = {}
    for sdk in args.sdk or []:
        sdks[sdk] = {
            "path": run(["xcrun", "--sdk", sdk, "--show-sdk-path"]),
            "version": run(["xcrun", "--sdk", sdk, "--show-sdk-version"]),
            "build_version": run(["xcrun", "--sdk", sdk, "--show-sdk-build-version"]),
            "platform_path": run(["xcrun", "--sdk", sdk, "--show-sdk-platform-path"]),
            "platform_version": run(["xcrun", "--sdk", sdk, "--show-sdk-platform-version"]),
            "find_clang": run(["xcrun", "--sdk", sdk, "--find", "clang"]),
        }
    extra = {}
    for cc in args.cc or []:
        extra[cc] = {
            "which": run(["which", "-a", cc]),
            "version": run([cc, "--version"]),
            "dash_v": run([cc, "-E", "-v", "-x", "c", "/dev/null"]),
        }
    developer = first_line(results["xcode_select_path"]) or ""
    libclang = {
        "xcode_toolchain": file_facts(
            os.path.join(developer, "Toolchains/XcodeDefault.xctoolchain/usr/lib/libclang.dylib")
        ),
        "command_line_tools": file_facts("/Library/Developer/CommandLineTools/usr/lib/libclang.dylib"),
    }
    for cc in args.cc or []:
        found = first_line(run(["which", cc]))
        if found:
            prefix = os.path.dirname(os.path.dirname(os.path.realpath(found)))
            libclang[cc] = file_facts(os.path.join(prefix, "lib", "libclang.dylib"))
    try:
        xcodes = sorted(n for n in os.listdir("/Applications") if n.startswith("Xcode"))
    except OSError:
        xcodes = []

    sw = {}
    for line in (results["sw_vers"].get("stdout") or "").splitlines():
        key, _, value = line.partition(":")
        sw[key.strip()] = value.strip()
    xcb = (results["xcodebuild_version"].get("stdout") or "").splitlines()
    summary = {
        "ImageOS": os.environ.get("ImageOS"),
        "ImageVersion": os.environ.get("ImageVersion"),
        "macos_product_version": sw.get("ProductVersion"),
        "macos_build_version": sw.get("BuildVersion"),
        "machine": first_line(run(["uname", "-m"])),
        "xcode_select_path": first_line(results["xcode_select_path"]),
        "xcode_version": xcb[0].strip() if xcb else None,
        "xcode_build": xcb[1].strip() if len(xcb) > 1 else None,
        "default_sdk_version": first_line(run(["xcrun", "--show-sdk-version"])),
        "default_sdk_path": first_line(run(["xcrun", "--show-sdk-path"])),
        "cc_version": first_line(results["cc_version"]),
        "sdk_versions": {k: first_line(v["version"]) for k, v in sdks.items()},
        "extra_compilers": {k: first_line(v["version"]) for k, v in extra.items()},
    }
    write_json(
        args.out,
        {
            "summary": summary,
            "applications_xcode": xcodes,
            "libclang": libclang,
            "commands": results,
            "sdks": sdks,
            "extra_compilers": extra,
            "environment": filtered_env(),
        },
    )
    print(summary)


# ---------------------------------------------------------------- build


def logical_lines(text: str):
    buf = ""
    for line in text.splitlines():
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        yield buf + line
        buf = ""
    if buf:
        yield buf


def shell_commands(line: str):
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return []
    cmds, cur, skip = [], [], False
    for tok in tokens:
        if skip:
            skip = False
            continue
        if tok in SEPARATORS:
            if cur:
                cmds.append(cur)
            cur = []
        elif tok in REDIRECTS:
            skip = True
        else:
            cur.append(tok)
    if cur:
        cmds.append(cur)
    out = []
    for argv in cmds:
        while argv and (ASSIGNMENT.match(argv[0]) or argv[0] in SHELL_WORDS):
            argv = argv[1:]
        if argv:
            out.append(argv)
    return out


def makefile_vars(path: str):
    """Return the raw values of MAKEFILE_VARS, and the variables the Makefile
    exports to its recipes that have a literal value."""
    values, everything, exported = {}, {}, []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return values, {}
    joined = re.sub(r"\\\n", " ", text)
    for line in joined.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:?+]?=\s*(.*?)\s*$", line)
        if m and m.group(1) not in everything:
            everything[m.group(1)] = m.group(2)
        m = re.match(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
        if m:
            exported.append(m.group(1))
    for name in MAKEFILE_VARS:
        if name in everything:
            values[name] = everything[name]
    exports = {
        name: everything[name]
        for name in exported
        if name in everything and "$" not in everything[name]
    }
    return values, exports


def find_compiles(text: str, cc_tokens, directory: str, root: str):
    records = []
    n = len(cc_tokens)
    for line in logical_lines(text):
        for argv in shell_commands(line):
            if len(argv) < n + 1:
                continue
            head = argv[:n]
            if head != cc_tokens and not (
                os.path.basename(head[0]) == os.path.basename(cc_tokens[0])
                and head[1:] == cc_tokens[1:]
            ):
                continue
            if "-c" not in argv:
                continue
            sources, output = [], None
            for i, tok in enumerate(argv[n:], start=n):
                prev = argv[i - 1]
                if prev == "-o":
                    output = tok
                    continue
                if prev in ARG_STRIP or prev in ARG_KEEP:
                    continue
                if not tok.startswith("-") and tok.endswith(SOURCE_EXT):
                    sources.append(tok)
            for src in sources:
                full = os.path.normpath(os.path.join(directory, src))
                records.append(
                    {
                        "directory": directory,
                        "file": src,
                        "source": os.path.relpath(full, root),
                        "output": output,
                        "arguments": argv,
                    }
                )
    return records


def macro_flags(args):
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in ARG_STRIP:
            i += 2
            continue
        if a in ARG_KEEP:
            out.extend(args[i : i + 2])
            i += 2
            continue
        if a in EXACT_STRIP or a.startswith(PREFIX_STRIP):
            i += 1
            continue
        if not a.startswith("-") and a.endswith(SOURCE_EXT):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def split_arches(flags):
    arches, rest, i = [], [], 0
    while i < len(flags):
        if flags[i] == "-arch" and i + 1 < len(flags):
            arches.append(flags[i + 1])
            i += 2
        else:
            rest.append(flags[i])
            i += 1
    if len(arches) <= 1:
        return [(arches[0] if arches else None, flags)]
    return [(arch, rest + ["-arch", arch]) for arch in arches]


def parse_dash_v(stderr: str) -> dict:
    quote, angle, mode = [], [], None
    info = {"version": None, "target": None, "thread_model": None, "installed_dir": None, "cc1": None}
    for line in stderr.splitlines():
        if line.startswith('#include "..." search starts here'):
            mode = quote
        elif line.startswith("#include <...> search starts here"):
            mode = angle
        elif line.startswith("End of search list"):
            mode = None
        elif mode is not None and line.startswith(" "):
            mode.append(line.strip())
        elif "clang version" in line and info["version"] is None:
            info["version"] = line.strip()
        elif line.startswith("Target:"):
            info["target"] = line.split(":", 1)[1].strip()
        elif line.startswith("Thread model:"):
            info["thread_model"] = line.split(":", 1)[1].strip()
        elif line.startswith("InstalledDir:"):
            info["installed_dir"] = line.split(":", 1)[1].strip()
        elif " -cc1 " in line and info["cc1"] is None:
            try:
                info["cc1"] = shlex.split(line)
            except ValueError:
                info["cc1"] = line.strip()
    info["quote_include_dirs"] = quote
    info["include_dirs"] = angle
    return info


def probe(cc_tokens, flags, env, cwd) -> dict:
    macros_run = run(cc_tokens + flags + ["-dM", "-E", "-x", "c", "/dev/null"], env=env, cwd=cwd)
    macros = {}
    for line in (macros_run.get("stdout") or "").splitlines():
        if line.startswith("#define "):
            body = line[len("#define ") :]
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?)\s?(.*)$", body)
            if m:
                macros[m.group(1)] = m.group(2)
    v_run = run(cc_tokens + flags + ["-E", "-v", "-x", "c", "/dev/null"], env=env, cwd=cwd)
    info = parse_dash_v(v_run.get("stderr") or "")
    result = {
        "macros_returncode": macros_run.get("returncode"),
        "macro_count": len(macros),
        "macros": dict(sorted(macros.items())),
        "dash_v_returncode": v_run.get("returncode"),
    }
    result.update(info)
    if macros_run.get("returncode") != 0:
        result["macros_stderr"] = macros_run.get("stderr") or macros_run.get("error")
    return result


def build_command(args) -> None:
    root = os.path.abspath(args.root)
    directory = os.path.abspath(args.dir)
    env = dict(os.environ)
    if args.path:
        env["PATH"] = args.path
    mk, exports = makefile_vars(os.path.join(directory, "Makefile"))
    # make passes these to every recipe, so the compiler saw them; for
    # example MACOSX_DEPLOYMENT_TARGET sets the deployment-target macros.
    env.update(exports)
    cc_tokens = shlex.split(mk.get("CC", "cc"))
    with open(args.dry_run, encoding="utf-8", errors="replace") as f:
        dry = f.read()
    records = find_compiles(dry, cc_tokens, directory, root)

    flag_sets, keys = [], {}
    for rec in records:
        flags = macro_flags(rec["arguments"][len(cc_tokens) :])
        key = tuple(flags)
        if key not in keys:
            keys[key] = len(flag_sets)
            flag_sets.append({"flags": flags, "file_count": 0, "example": rec["source"]})
        rec["flag_set"] = keys[key]
        flag_sets[keys[key]]["file_count"] += 1
    for fs in flag_sets:
        fs["probes"] = []
        for arch, flags in split_arches(fs["flags"]):
            p = probe(cc_tokens, flags, env, directory)
            p["arch"] = arch
            p["probe_flags"] = flags
            fs["probes"].append(p)

    compare = None
    if args.build_log and os.path.exists(args.build_log):
        with open(args.build_log, encoding="utf-8", errors="replace") as f:
            built = find_compiles(f.read(), cc_tokens, directory, root)
        planned = {(r["source"], r["output"]) for r in records}
        done = {(r["source"], r["output"]) for r in built}
        same_argv = {tuple(r["arguments"]) for r in records} >= {tuple(r["arguments"]) for r in built}
        compare = {
            "build_log_compiles": len(built),
            "dry_run_compiles": len(records),
            "only_in_dry_run": sorted("%s -> %s" % x for x in planned - done),
            "only_in_build_log": sorted("%s -> %s" % x for x in done - planned),
            "build_log_argv_all_in_dry_run": same_argv,
        }

    config_status = run(["sh", "config.status", "--config"], cwd=directory)
    config_log_head = []
    try:
        with open(os.path.join(directory, "config.log"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if "## Core tests. ##" in line:
                    break
                config_log_head.append(line.rstrip("\n"))
    except OSError:
        pass

    files_dir = os.path.dirname(os.path.abspath(args.out))
    copied = []
    for name in ["pyconfig.h", "config.cache"]:
        src = os.path.join(directory, name)
        if os.path.exists(src):
            dst = os.path.join(files_dir, "%s-%s" % (args.name, name))
            shutil.copyfile(src, dst)
            copied.append(os.path.basename(dst))

    write_json(
        args.out,
        {
            "name": args.name,
            "directory": directory,
            "root": root,
            "compiler": cc_tokens,
            "compiler_path_env": env.get("PATH"),
            "makefile_exports_applied_to_probes": exports,
            "probe_env": {k: env.get(k) for k in ["SDKROOT", "MACOSX_DEPLOYMENT_TARGET", "IPHONEOS_DEPLOYMENT_TARGET", "DEVELOPER_DIR"]},
            "makefile_vars": mk,
            "configure_args": (config_status.get("stdout") or "").strip(),
            "config_log_head": config_log_head,
            "compile_count": len(records),
            "distinct_sources": len({r["source"] for r in records}),
            "flag_sets": flag_sets,
            "compile_commands": records,
            "dry_run_vs_build_log": compare,
            "files": copied,
        },
    )
    print("%s: %d compiles, %d flag sets" % (args.name, len(records), len(flag_sets)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("env")
    p.add_argument("--out", required=True)
    p.add_argument("--sdk", action="append", help="xcrun SDK name, e.g. macosx")
    p.add_argument("--cc", action="append", help="another compiler to record")
    p.set_defaults(func=env_command)

    p = sub.add_parser("build")
    p.add_argument("--root", required=True, help="the CPython source tree")
    p.add_argument("--dir", required=True, help="the configured build directory")
    p.add_argument("--name", required=True)
    p.add_argument("--dry-run", required=True, help="`make -n` output")
    p.add_argument("--build-log", help="the real build's output, to compare")
    p.add_argument("--path", help="PATH the build ran the compiler with")
    p.add_argument("--out", required=True)
    p.set_defaults(func=build_command)

    add_common_subcommands(sub)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
