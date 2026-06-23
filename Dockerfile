# syntax=docker/dockerfile:1

FROM klee/klee:3.0 AS klee_toolchain

FROM framac/frama-c:dev-stripped.debian

USER root

ENV DEBIAN_FRONTEND=noninteractive
ENV KLEE_HOME=/home/klee/klee_src
ENV KLEE_BUILD=/home/klee/klee_build
ENV LLVM_HOME=/tmp/llvm-130-install_O_D_A
ENV KLEE_INCLUDE=/home/klee/klee_src/include
ENV KLEE_CLANG=/tmp/llvm-130-install_O_D_A/bin/clang
ENV LLVM_LINK=/tmp/llvm-130-install_O_D_A/bin/llvm-link
ENV PATH="/opt/kleva/.venv/bin:/home/klee/klee_build/bin:/tmp/llvm-130-install_O_D_A/bin:${PATH}"

COPY --from=klee_toolchain /home/klee/klee_src /home/klee/klee_src
COPY --from=klee_toolchain /home/klee/klee_build /home/klee/klee_build
COPY --from=klee_toolchain /tmp/llvm-130-install_O_D_A /tmp/llvm-130-install_O_D_A

RUN apt-get update \
    && apt-get -y \
        -o Dpkg::Options::="--force-confdef" \
        -o Dpkg::Options::="--force-confold" \
        install --no-install-recommends \
        build-essential \
        ca-certificates \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/kleva

COPY pyproject.toml README.md LICENSE ./
COPY docs ./docs
COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/kleva-docker-entrypoint

RUN python3 -m venv /opt/kleva/.venv \
    && /opt/kleva/.venv/bin/python -m pip install --upgrade pip \
    && /opt/kleva/.venv/bin/python -m pip install --no-cache-dir . \
    && chmod +x /usr/local/bin/kleva-docker-entrypoint \
    && if ! id -u klee >/dev/null 2>&1; then useradd -m -s /bin/bash klee; fi \
    && mkdir -p /work \
    && chown -R klee:klee /opt/kleva /work

USER klee
WORKDIR /work

ENTRYPOINT ["kleva-docker-entrypoint"]
CMD ["--help"]
