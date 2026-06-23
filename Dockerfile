# syntax=docker/dockerfile:1

FROM klee/klee:3.0 AS klee

FROM framac/frama-c:32.1

USER root

ENV DEBIAN_FRONTEND=noninteractive
ENV KLEE_HOME=/home/klee/klee_src
ENV KLEE_BUILD=/tmp/klee_build130stp_z3
ENV LLVM_HOME=/tmp/llvm-130-install_O_D_A
ENV KLEE_INCLUDE=/home/klee/klee_src/include
ENV KLEE_CLANG=/tmp/llvm-130-install_O_D_A/bin/clang
ENV LLVM_LINK=/tmp/llvm-130-install_O_D_A/bin/llvm-link
ENV PATH="/opt/kleva/.venv/bin:/tmp/klee_build130stp_z3/bin:/tmp/llvm-130-install_O_D_A/bin:${PATH}"

COPY --from=klee /home/klee/klee_src /home/klee/klee_src
COPY --from=klee /tmp/klee_build130stp_z3 /tmp/klee_build130stp_z3
COPY --from=klee /tmp/llvm-130-install_O_D_A /tmp/llvm-130-install_O_D_A

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
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
    && mkdir -p /work \
    && chown -R opam:opam /opt/kleva /work /home/klee /tmp/klee_build130stp_z3 /tmp/llvm-130-install_O_D_A

USER opam
WORKDIR /work

ENTRYPOINT ["kleva-docker-entrypoint"]
CMD ["--help"]
