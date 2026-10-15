# CPython 3.14.6 CI environments

This repository records the toolchains and build commands CPython 3.14.6's CI
uses on macOS, iOS and Windows.

Each workflow runs on the GitHub-hosted runner label that the matching job in
[CPython's workflows at `v3.14.6`](https://github.com/python/cpython/tree/v3.14.6/.github/workflows)
uses, checks out `python/cpython` at tag `v3.14.6`, and builds it the way that
job does. It then uploads one artifact per job with an `environment.json`
that holds:

- the runner image (`ImageOS`, `ImageVersion`) and the OS version;
- Xcode and the SDK versions, or Visual Studio, the MSVC toolset, the Windows
  SDKs and `cl`'s banner;
- the configure or `build.bat` command lines and their environment;
- the compile command of every C file in the build, from `make -n` or from
  MSBuild's binary log;
- the compiler's predefined macros and include search directories for each
  distinct set of flags;
- whether the build succeeded.

The test suites are not run. No SDK headers or preprocessed output are
uploaded: only versions, paths, command lines and macro definitions.

| Workflow | Mirrors (CPython `v3.14.6`) | Runner |
| --- | --- | --- |
| `ios.yml` | `build.yml` `build-ios` | `macos-14`, Xcode 15.4 |
| `macos.yml` | `build.yml` `build-macos`; `jit.yml` and `tail-call.yml` macOS jobs | `macos-26`, `macos-15-intel`, `macos-15` |
| `windows.yml` | `build.yml` `build-windows` | `windows-2022`, `windows-11-arm` |

The scripts in `scripts/` use only Python's standard library and run on the
runner's own Python.
