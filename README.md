# CPython 3.14.6 CI environments

This repository records the toolchains and build commands CPython 3.14.6's CI
uses on macOS, iOS and Windows, and configures two arm64 Linux builds on a
real `ubuntu-24.04-arm` runner.

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
- the path, size and sha256 of the toolchain's `libclang`;
- whether the build succeeded.

The test suites are not run. No SDK headers or preprocessed output are
uploaded: only versions, paths, command lines and macro definitions.

The workflows and the CPython `v3.14.6` jobs they mirror:

- `ios.yml`: `build.yml` `build-ios`, on `macos-14` with Xcode 15.4.
- `macos.yml`: `build.yml` `build-macos`, and the macOS jobs of `jit.yml` and
  `tail-call.yml`, on `macos-26`, `macos-15-intel` and `macos-15`.
- `windows.yml`: `build.yml` `build-windows`, on `windows-2022` and
  `windows-11-arm`.
- `linux-arm64.yml`: two arm64 Linux builds on `ubuntu-24.04-arm`, configured
  but not built: clang 20 (Ubuntu's 20.1.2) and gcc 13.3.0, the latter as
  `build.yml` `build-ubuntu` on that runner, without its extra options. Each
  is configured in a container of `linux/ubuntu.Dockerfile`, whose packages
  are pinned in `linux/ubuntu24.04-arm64.json`, and on the runner itself with
  the same packages. The outputs are compared with the same image run under
  QEMU, to check that QEMU user mode configures as a real kernel does. Only
  `workflow_dispatch` runs it.

`windows.yml` also builds arm64 on `windows-2022`, cross-compiled with Visual
Studio 2022's ARM64 cross compiler. That is not a CPython CI job; it is
there for comparison, because the image `windows-11-arm` now serves ships
Visual Studio 2026, which v3.14.6's `PCbuild` cannot build with (MSB8020).

The scripts in `scripts/` use only Python's standard library and run on the
runner's own Python (`capture_linux.py` also on the container's).

## Encrypted parse of the Apple builds

`apple-parse.yml` runs the Apple builds listed in `scripts/apple_builds.json`.
For each one it:

- configures and builds CPython `v3.14.6` as the matching CI job does, on
  the same runner label with the same Xcode (`build-ios` on `macos-14` with
  Xcode 15.4; `build-macos`, `jit.yml` and `tail-call.yml` on `macos-26`,
  `macos-15` and `macos-15-intel`);
- uses each build's own configure options, without CI's extra ones such as
  `--with-pydebug`;
- runs a private analysis bundle that parses the build with the
  toolchain's own `libclang`.

The bundle, `bundle/extractor.tar.age`, is encrypted with
[age](https://age-encryption.org) v1.3.2 using post-quantum hybrid keys. The
runner decrypts it with the repository secret `EXTRACTOR_IDENTITY`; its
steps print one status line each. Everything a job produces, the build
records included, is encrypted to `bundle/outputs.recipient` before upload,
so the artifacts hold only ciphertext and no SDK header leaves the runner in
clear form. Only `workflow_dispatch` runs it, for example
`gh workflow run apple-parse.yml -f builds=ios-arm64-simulator`.
