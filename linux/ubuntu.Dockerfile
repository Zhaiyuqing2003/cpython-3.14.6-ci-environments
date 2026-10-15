# The Ubuntu 24.04 userland the arm64 Linux builds are configured in: CPython
# CI's posix-deps-apt.sh, gcc 13 from build-essential as on the
# ubuntu-24.04-arm runner, and Ubuntu's clang-20 (20.1.2, noble-updates) with
# its libclang and Python bindings. The same image is run on x86-64 under
# QEMU user mode and in an arm64 VM under QEMU system mode; linux-arm64.yml
# runs it on a real ubuntu-24.04-arm runner, to compare the three.
#
# Pinned: the base image by its per-architecture digest, Ubuntu's archive
# at one snapshot (snapshot.ubuntu.com, which serves the ports
# architectures under /ubuntu too), and every package named at its exact
# version. The pins are build arguments, kept in linux/ubuntu24.04-arm64.json.
ARG BASE
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/sh", "-eux", "-c"]

# ca-certificates from the release pocket, which never changes, to reach
# the snapshot service over https; then the archive as it was at SNAPSHOT.
# apt's `Snapshot:` option needs the archive to name its snapshot server,
# which ports.ubuntu.com does not, so the snapshot's URI is written out.
ARG SNAPSHOT
ARG BOOTSTRAP
ARG APT
RUN src=/etc/apt/sources.list.d/ubuntu.sources; \
    uri="$(awk '/^URIs:/ {print $2; exit}' "${src}")"; \
    mv "${src}" /tmp/ubuntu.sources; \
    printf '%s\n' 'Types: deb' "URIs: ${uri}" \
        'Suites: noble' 'Components: main' \
        'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
        > /etc/apt/sources.list.d/release.sources; \
    apt-get update; \
    apt-get -yq --no-install-recommends install ${BOOTSTRAP}; \
    rm /etc/apt/sources.list.d/release.sources; \
    sed "s|^URIs:.*|URIs: https://snapshot.ubuntu.com/ubuntu/${SNAPSHOT}/|" \
        /tmp/ubuntu.sources > "${src}"; \
    rm /tmp/ubuntu.sources; \
    apt-get update; \
    apt-get -yq --no-install-recommends install ${APT}; \
    rm -rf /var/lib/apt/lists/*

# apt.llvm.org, which keeps one build of each version (no snapshots), when
# LLVM_SUITES names its suites: its key checked by checksum, and its
# packages at their exact versions, so a rebuild after it moves on fails
# rather than drifting. llvm.sh installs with recommends; the parser's
# packages go without.
ADD https://apt.llvm.org/llvm-snapshot.gpg.key /tmp/apt.llvm.org.asc
ARG LLVM_KEY_SHA256=""
ARG LLVM_SUITES=""
ARG LLVM_APT=""
ARG PARSER_APT=""
RUN if [ -n "${LLVM_SUITES}" ]; then \
        echo "${LLVM_KEY_SHA256}  /tmp/apt.llvm.org.asc" | sha256sum -c -; \
        install -m 0644 /tmp/apt.llvm.org.asc /etc/apt/trusted.gpg.d/; \
        rm /tmp/apt.llvm.org.asc; \
        for suite in ${LLVM_SUITES}; do \
            echo "deb http://apt.llvm.org/noble/ ${suite} main" \
                >> /etc/apt/sources.list.d/apt.llvm.org.list; \
        done; \
        apt-get update; \
        apt-get -yq install ${LLVM_APT}; \
        apt-get -yq --no-install-recommends install ${PARSER_APT}; \
        rm -rf /var/lib/apt/lists/*; \
    else \
        rm /tmp/apt.llvm.org.asc; \
    fi
