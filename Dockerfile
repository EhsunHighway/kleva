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
COPY --from=klee_toolchain /usr/local/lib /usr/local/lib
COPY --from=klee_toolchain /tmp/stp-2.3.3-install/lib/libstp.so* /usr/local/lib/
COPY --from=klee_toolchain /tmp/z3-*-install/lib/libz3.so* /usr/local/lib/

WORKDIR /opt/kleva

COPY pyproject.toml README.md LICENSE ./
COPY docs ./docs
COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/kleva-docker-entrypoint
COPY docker/kleva /usr/local/bin/kleva

RUN python3 --version \
    && python3 -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', '/tmp/get-pip.py')" \
    && python3 /tmp/get-pip.py --break-system-packages \
    && python3 -m pip install --no-cache-dir --break-system-packages pyyaml \
    && rm -f /tmp/get-pip.py \
    && python3 -c "import yaml" \
    && ldconfig \
    && frama-c -version \
    && klee --version \
    && chmod +x /usr/local/bin/kleva-docker-entrypoint /usr/local/bin/kleva \
    && mkdir -p /work

WORKDIR /work

ENTRYPOINT ["kleva-docker-entrypoint"]
CMD ["--help"]
