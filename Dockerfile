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
ENV PYTHONPATH=/opt/kleva/src
ENV PATH="/home/klee/klee_build/bin:/tmp/llvm-130-install_O_D_A/bin:${PATH}"

COPY --from=klee_toolchain /home/klee/klee_src /home/klee/klee_src
COPY --from=klee_toolchain /home/klee/klee_build /home/klee/klee_build
COPY --from=klee_toolchain /tmp/llvm-130-install_O_D_A /tmp/llvm-130-install_O_D_A

WORKDIR /opt/kleva

COPY pyproject.toml README.md LICENSE ./
COPY docs ./docs
COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/kleva-docker-entrypoint

RUN python3 --version \
    && python3 -m ensurepip --upgrade \
    && python3 -m pip install --no-cache-dir --break-system-packages pyyaml \
    && python3 -c "import yaml" \
    && frama-c -version \
    && klee --version \
    && chmod +x /usr/local/bin/kleva-docker-entrypoint \
    && mkdir -p /work

WORKDIR /work

ENTRYPOINT ["kleva-docker-entrypoint"]
CMD ["--help"]
