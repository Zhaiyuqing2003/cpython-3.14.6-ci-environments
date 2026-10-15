#!/usr/bin/env python3
"""Record a Windows runner's toolchain and the cl.exe commands of a build.

Subcommands:
  env      runner, OS, Visual Studio (vswhere), MSVC toolsets, Windows SDKs
  build    replay MSBuild's binary log to text, split each CL task command
           into one command per source file, then run cl.exe once per
           distinct flag set to read its predefined macros
  step     record a command line with its working directory and environment
  combine  merge the parts into one JSON file

The binary log itself is not uploaded (it embeds Visual Studio's MSBuild
files), and nothing here copies SDK headers or preprocessed output.
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import platform
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from capture_common import (  # noqa: E402
    add_common_subcommands,
    file_facts,
    filtered_env,
    run,
    write_json,
)

VSWHERE = os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe")
KITS = os.path.expandvars(r"%ProgramFiles(x86)%\Windows Kits\10")
SOURCE_EXT = (".c", ".cpp", ".cxx", ".cc")

# MSVC options whose argument may be a separate token.
ARG_OPTS = {"/D", "-D", "/U", "-U", "/I", "-I", "/FI", "-FI", "/external:I", "-external:I", "/AI", "/FU"}
# Options that do not change the predefined macros (paths, diagnostics, output).
STRIP_PREFIX = (
    "/I", "/D", "/U", "/Fo", "/Fd", "/Fa", "/Fp", "/FR", "/Fr", "/Fe", "/Fi",
    "/Yu", "/Yc", "/FI", "/W", "/w", "/external:", "/analyze", "/errorReport",
    "/diagnostics", "/MP", "/showIncludes", "/sourceDependencies", "/doc",
)
STRIP_EXACT = {"/c", "/nologo", "/FS", "/Zi", "/ZI", "/Z7", "/Gm-", "/Gm", "/bigobj", "/FC"}

PROPERTIES = [
    "VisualStudioVersion", "VSInstallDir", "MSBuildVersion", "MSBuildToolsVersion",
    "PlatformToolset", "PlatformToolsetVersion", "VCToolsVersion", "VCToolsInstallDir",
    "VCInstallDir", "DefaultWindowsSDKVersion", "WindowsTargetPlatformVersion",
    "TargetPlatformVersion", "WindowsSdkDir", "WindowsSDKVersion", "UCRTContentRoot",
    "UniversalCRTSdkDir", "PreferredToolArchitecture", "IncludePath",
    "ExternalIncludePath", "LibraryPath", "ExecutablePath", "PyStdlibDir",
    "PySourcePath", "ArchName", "PlatformTarget", "IntDir", "OutDir",
]

MACROS = """
__STDC__ __STDC_HOSTED__ __STDC_VERSION__ __STDC_NO_ATOMICS__ __STDC_NO_COMPLEX__
__STDC_NO_THREADS__ __STDC_NO_VLA__ __STDC_IEC_559__ __STDC_UTF_16__ __STDC_UTF_32__
__cplusplus _MSVC_LANG _MSC_VER _MSC_FULL_VER _MSC_BUILD _MSC_EXTENSIONS
_MSVC_TRADITIONAL _MSVC_WARNING_LEVEL _MSVC_EXECUTION_CHARACTER_SET
_MSVC_CONSTEXPR_ATTRIBUTE _WIN32 _WIN64 _M_IX86 _M_IX86_FP _M_X64 _M_AMD64 _M_ARM
_M_ARM64 _M_ARM64EC _M_ARMT _M_ARM_ARMV7VE _M_ARM_FP _M_THUMB _M_HYBRID
_M_HYBRID_X86_ARM64 _M_CEE _M_CEE_PURE _M_CEE_SAFE _M_FP_CONTRACT _M_FP_EXCEPT
_M_FP_FAST _M_FP_PRECISE _M_FP_STRICT __AVX__ __AVX2__ __AVX512F__ __AVX512CD__
__AVX512BW__ __AVX512DQ__ __AVX512VL__ __AVX10_VER__ __SSE__ __SSE2__ __SSE3__
__SSSE3__ __SSE4_1__ __SSE4_2__ __ARM_ARCH __ARM_NEON __ARM_FP __ARM_FEATURE_CRC32
_CHAR_UNSIGNED _CPPRTTI _CPPUNWIND _DEBUG _DLL _MT _NATIVE_WCHAR_T_DEFINED
_WCHAR_T_DEFINED _INTEGRAL_MAX_BITS _ISO_VOLATILE _KERNEL_MODE _MANAGED _OPENMP
_PREFAST_ _CONTROL_FLOW_GUARD __SANITIZE_ADDRESS__ __CLR_VER _VC_NODEFAULTLIB
_WINRT_DLL __MSVC_RUNTIME_CHECKS __STDCPP_DEFAULT_NEW_ALIGNMENT__ __STDCPP_THREADS__
__BOOL_DEFINED __INTELLISENSE__ __clang__ __clang_major__ __GNUC__ __llvm__
_WIN32_WINNT NTDDI_VERSION WINVER
""".split()


# ---------------------------------------------------------------- env


def first_line(result: dict) -> str | None:
    text = (result.get("stdout") or "") + (result.get("stderr") or "")
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def reg_values(root, path) -> dict:
    try:
        import winreg
    except ImportError:
        return {}
    out = {}
    for view in (0, getattr(winreg, "KEY_WOW64_32KEY", 0)):
        try:
            key = winreg.OpenKey(root, path, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            out[name] = value if isinstance(value, (str, int)) else repr(value)
            i += 1
        j, subkeys = 0, []
        while True:
            try:
                subkeys.append(winreg.EnumKey(key, j))
            except OSError:
                break
            j += 1
        if subkeys:
            out["(subkeys)"] = subkeys
        if out:
            break
    return out


def vswhere_json() -> list:
    res = run([VSWHERE, "-all", "-prerelease", "-products", "*", "-format", "json", "-utf8"])
    try:
        return json.loads(res.get("stdout") or "[]")
    except ValueError:
        return [{"error": res}]


def msbuild_path() -> str | None:
    res = run([VSWHERE, "-latest", "-prerelease", "-products", "*", "-requires",
               "Microsoft.Component.MSBuild", "-find", r"MSBuild\**\Bin\MSBuild.exe"])
    lines = [l.strip() for l in (res.get("stdout") or "").splitlines() if l.strip()]
    return lines[0] if lines else None


def env_command(args) -> None:
    import winreg

    installs = vswhere_json()
    toolsets = {}
    for inst in installs:
        path = inst.get("installationPath")
        if not path:
            continue
        aux = os.path.join(path, "VC", "Auxiliary", "Build")
        defaults = {}
        for f in glob.glob(os.path.join(aux, "Microsoft.VCToolsVersion*.txt")):
            with open(f, encoding="utf-8", errors="replace") as fh:
                defaults[os.path.basename(f)] = fh.read().strip()
        toolsets[path] = {
            "msvc_dirs": sorted(os.listdir(os.path.join(path, "VC", "Tools", "MSVC")))
            if os.path.isdir(os.path.join(path, "VC", "Tools", "MSVC")) else [],
            "default_version_files": defaults,
            "llvm": sorted(os.listdir(os.path.join(path, "VC", "Tools", "Llvm")))
            if os.path.isdir(os.path.join(path, "VC", "Tools", "Llvm")) else [],
        }
    llvm = {}
    program_llvm = os.path.expandvars(r"%ProgramFiles%\LLVM\bin")
    candidates = [("program_files", program_llvm)]
    for path in toolsets:
        for sub in ["x64", "ARM64", ""]:
            candidates.append(("vs:" + (sub or "default"), os.path.join(path, "VC", "Tools", "Llvm", sub, "bin")))
    for label, bindir in candidates:
        if not os.path.isdir(bindir):
            continue
        llvm[label] = {
            "bin": bindir,
            "libclang": file_facts(os.path.join(bindir, "libclang.dll")),
            "clang_version": first_line(run([os.path.join(bindir, "clang.exe"), "--version"])),
            "clang_cl_version": first_line(run([os.path.join(bindir, "clang-cl.exe"), "--version"])),
        }
    sdk_include = sorted(os.listdir(os.path.join(KITS, "Include"))) if os.path.isdir(os.path.join(KITS, "Include")) else []
    sdk_lib = sorted(os.listdir(os.path.join(KITS, "Lib"))) if os.path.isdir(os.path.join(KITS, "Lib")) else []
    msbuild = msbuild_path()
    osreg = reg_values(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
    keep = ["ProductName", "DisplayVersion", "ReleaseId", "CurrentBuild", "CurrentBuildNumber",
            "UBR", "EditionID", "InstallationType", "BuildLabEx", "CurrentMajorVersionNumber",
            "CurrentMinorVersionNumber"]
    osreg = {k: osreg[k] for k in keep if k in osreg}
    sysinfo = run(["systeminfo"], timeout=300)
    sysinfo_lines = [
        l for l in (sysinfo.get("stdout") or "").splitlines()
        if re.match(r"^(OS |System Type|System Manufacturer|System Model|Processor|Total Physical Memory|\s+\[\d+\]: .*(Family|GenuineIntel|AuthenticAMD|ARM))", l)
    ]
    results = {
        "ver": run(["cmd", "/c", "ver"]),
        "msbuild_version": run([msbuild, "-version", "-nologo"]) if msbuild else None,
        "git_version": run(["git", "--version"]),
        "py_launcher": run(["py", "-0p"]),
        "where_python": run(["where", "python"]),
        "where_cl": run(["where", "cl"]),
        "where_clang": run(["where", "clang"]),
    }
    summary = {
        "ImageOS": os.environ.get("ImageOS"),
        "ImageVersion": os.environ.get("ImageVersion"),
        "windows": osreg,
        "ver": first_line(results["ver"]),
        "machine": platform.machine(),
        "processor_architecture": os.environ.get("PROCESSOR_ARCHITECTURE"),
        "visual_studio": [
            {
                "displayName": i.get("displayName"),
                "installationVersion": i.get("installationVersion"),
                "catalog_productDisplayVersion": (i.get("catalog") or {}).get("productDisplayVersion"),
                "installationPath": i.get("installationPath"),
                "isPrerelease": i.get("isPrerelease"),
            }
            for i in installs
        ],
        "msvc_toolsets": {p: t["msvc_dirs"] for p, t in toolsets.items()},
        "windows_sdk_include_versions": sdk_include,
        "msbuild": msbuild,
        "msbuild_version": first_line(results["msbuild_version"]) if results["msbuild_version"] else None,
    }
    write_json(
        args.out,
        {
            "summary": summary,
            "vswhere": installs,
            "toolsets": toolsets,
            "llvm": llvm,
            "windows_kits": {
                "root": KITS,
                "include_versions": sdk_include,
                "lib_versions": sdk_lib,
                "installed_roots": reg_values(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows Kits\Installed Roots"),
                "sdk_v10": reg_values(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Microsoft SDKs\Windows\v10.0"),
            },
            "systeminfo": sysinfo_lines,
            "platform": {"platform": platform.platform(), "win32_ver": platform.win32_ver(), "version": platform.version()},
            "commands": results,
            "environment": filtered_env(),
        },
    )
    print(json.dumps(summary, indent=1))


# ---------------------------------------------------------------- build


def split_command_line(text: str) -> list:
    """Split like the C runtime does (CommandLineToArgvW)."""
    fn = ctypes.windll.shell32.CommandLineToArgvW
    fn.restype = ctypes.POINTER(ctypes.c_wchar_p)
    fn.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    argc = ctypes.c_int(0)
    argv = fn("x " + text, ctypes.byref(argc))
    try:
        return [argv[i] for i in range(1, argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


CL_LINE = re.compile(r'^\s*(?:\d+>)?\s*"?(?P<exe>(?:[A-Za-z]:)?[^"]*?\\cl\.exe)"?\s+(?P<args>.*\S)\s*$', re.I)


def parse_cl_lines(log_path: str):
    invocations = []
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "cl.exe" not in line.lower():
                continue
            m = CL_LINE.match(line)
            if not m or ".exe" in m.group("exe").lower()[:-len("\\cl.exe")]:
                continue
            # The diagnostic logger appends the task's id to each message.
            args = re.sub(r"\s*\(TaskId:\d+\)$", "", m.group("args"))
            if "/c" not in args.split() and "-c" not in args.split():
                continue
            invocations.append((m.group("exe"), args))
    return invocations


def split_sources(args):
    options, sources, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a in ARG_OPTS and i + 1 < len(args):
            options.append(a + args[i + 1])
            i += 2
            continue
        if not a.startswith(("/", "-")) and a.lower().endswith(SOURCE_EXT):
            sources.append(a)
        else:
            options.append(a)
        i += 1
    return options, sources


def macro_flags(options):
    out = []
    for a in options:
        norm = "/" + a[1:] if a.startswith("-") else a
        if norm in STRIP_EXACT or norm.startswith(STRIP_PREFIX):
            continue
        out.append(a)
    return out


def cl_env(exe: str) -> dict:
    env = dict(os.environ)
    exe_dir = os.path.dirname(exe)
    host_dir = os.path.dirname(exe_dir)
    host = os.path.basename(host_dir).lower()
    native = {"hostx64": "x64", "hostx86": "x86", "hostarm64": "arm64"}.get(host)
    extra = [exe_dir]
    if native:
        extra.append(os.path.join(host_dir, native))
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def probe_source(tmp: str) -> tuple:
    lines = ["#define CIENV_STR(x) #x", "#define CIENV_XSTR(x) CIENV_STR(x)"]
    for name in MACROS:
        lines += [
            "#ifdef " + name,
            'CIENV_MACRO "%s" = CIENV_XSTR(%s)' % (name, name),
            "#else",
            'CIENV_UNDEF "%s"' % name,
            "#endif",
        ]
    probe = os.path.join(tmp, "probe.c")
    empty = os.path.join(tmp, "empty.c")
    with open(probe, "w", encoding="ascii") as f:
        f.write("\n".join(lines) + "\n")
    with open(empty, "w", encoding="ascii") as f:
        f.write("\n")
    return probe, empty


def probe(exe, flags, probe_c, empty_c, tmp) -> dict:
    env = cl_env(exe)
    res = run([exe] + flags + ["/nologo", "/EP", "/TC", probe_c], env=env, cwd=tmp)
    defined, undefined = {}, []
    for line in (res.get("stdout") or "").splitlines():
        m = re.match(r'^\s*CIENV_MACRO "(\w+)" = (.*)$', line)
        if m:
            defined[m.group(1)] = m.group(2).strip()
            continue
        m = re.match(r'^\s*CIENV_UNDEF "(\w+)"', line)
        if m:
            undefined.append(m.group(1))
    out = {
        "returncode": res.get("returncode"),
        "known_macros_defined": defined,
        "known_macros_undefined": undefined,
    }
    if res.get("returncode") != 0:
        out["stderr"] = res.get("stderr") or res.get("error")
    # /PD prints every macro definition, but only with the conforming
    # preprocessor (/Zc:preprocessor), which also sets _MSVC_TRADITIONAL to 0.
    pd_flags = [] if "/Zc:preprocessor" in flags else ["/Zc:preprocessor"]
    pd = run([exe] + flags + pd_flags + ["/nologo", "/EP", "/PD", "/TC", empty_c], env=env, cwd=tmp)
    out["pd_added_flags"] = pd_flags
    pd_macros = {}
    for line in (pd.get("stdout") or "").splitlines():
        m = re.match(r"^\s*#define\s+(\S+)\s?(.*)$", line)
        if m:
            pd_macros[m.group(1)] = m.group(2)
    out["pd_supported"] = bool(pd_macros)
    out["pd_macros"] = dict(sorted(pd_macros.items()))
    if not pd_macros:
        out["pd_stderr"] = (pd.get("stderr") or "")[:2000]
    return out


def msbuild_properties(msbuild, project, configuration, platform_name, props) -> dict:
    argv = [msbuild, project, "-nologo", "-p:Configuration=" + configuration, "-p:Platform=" + platform_name]
    argv += ["-p:" + p for p in props]
    argv += ["-getProperty:" + p for p in PROPERTIES]
    res = run(argv, timeout=600)
    try:
        return json.loads(res.get("stdout") or "{}").get("Properties", {})
    except ValueError:
        return {"error": res}


def build_command(args) -> None:
    root = os.path.abspath(args.root)
    pcbuild = os.path.join(root, "PCbuild")
    msbuild = args.msbuild or msbuild_path()
    replay = os.path.join(tempfile.gettempdir(), "msbuild-replay.log")
    rep = run([msbuild, args.binlog, "-noconlog", "-flp:logfile=%s;verbosity=diagnostic" % replay], timeout=1800)

    header = {}
    if os.path.exists(replay):
        with open(replay, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                for key in ("Command line arguments", "MSBuild executable path", "MSBuild version"):
                    if key in line and key not in header:
                        header[key] = line.strip()
                if n > 20000 or len(header) == 3:
                    break
    invocations = parse_cl_lines(replay) if os.path.exists(replay) else []

    records, flag_sets, keys, banners = [], [], {}, {}
    for exe, text in invocations:
        argv = split_command_line(text)
        options, sources = split_sources(argv)
        fo = next((o[3:] for o in options if o.lower().startswith(("/fo", "-fo"))), None)
        project = os.path.basename(os.path.normpath(fo)) if fo and fo.endswith(("\\", "/")) else None
        flags = macro_flags(options)
        key = (exe.lower(), tuple(flags))
        if key not in keys:
            keys[key] = len(flag_sets)
            flag_sets.append({"cl": exe, "flags": flags, "file_count": 0, "example": None})
        fs = flag_sets[keys[key]]
        for src in sources:
            full = os.path.normpath(os.path.join(pcbuild, src))
            rel = os.path.relpath(full, root) if full.lower().startswith(root.lower()) else full
            records.append({
                "directory": pcbuild,
                "file": src,
                "source": rel,
                "exists": os.path.exists(full),
                "project": project,
                "cl": exe,
                "arguments": options + [src],
                "flag_set": keys[key],
            })
            fs["file_count"] += 1
            fs["example"] = fs["example"] or rel
        if exe.lower() not in banners:
            env = cl_env(exe)
            banners[exe.lower()] = {"path": exe, "banner": run([exe], env=env)}

    tmp = tempfile.mkdtemp(prefix="clprobe")
    probe_c, empty_c = probe_source(tmp)
    for fs in flag_sets:
        fs["probe"] = probe(fs["cl"], fs["flags"], probe_c, empty_c, tmp)
    for b in banners.values():
        bv = run([b["path"], "/Bv", "/nologo", "/EP", "/TC", empty_c], env=cl_env(b["path"]), cwd=tmp)
        b["Bv"] = bv.get("stderr")
        b["banner_line"] = first_line({"stderr": b["banner"].get("stderr")})

    props = msbuild_properties(msbuild, os.path.join(pcbuild, "pythoncore.vcxproj"),
                               args.configuration, args.platform, args.prop or [])

    write_json(
        args.out,
        {
            "name": args.name,
            "root": root,
            "msbuild": msbuild,
            "replay_returncode": rep.get("returncode"),
            "binlog_header": header,
            "cl_invocations": len(invocations),
            "compile_count": len(records),
            "distinct_sources": len({r["source"].lower() for r in records}),
            "compilers": list(banners.values()),
            "pythoncore_properties": props,
            "flag_sets": flag_sets,
            "compile_commands": records,
        },
    )
    print("%s: %d cl invocations, %d compiles, %d flag sets" % (args.name, len(invocations), len(records), len(flag_sets)))
    for b in banners.values():
        print(b["banner_line"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("env")
    p.add_argument("--out", required=True)
    p.set_defaults(func=env_command)

    p = sub.add_parser("build")
    p.add_argument("--root", required=True)
    p.add_argument("--binlog", required=True)
    p.add_argument("--msbuild")
    p.add_argument("--platform", required=True)
    p.add_argument("--configuration", required=True)
    p.add_argument("--prop", action="append", help="NAME=VALUE passed when reading properties")
    p.add_argument("--name", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=build_command)

    add_common_subcommands(sub)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
