# syntax=docker/dockerfile:1
FROM ubuntu:24.04

ARG TARGETARCH
RUN test "$TARGETARCH" = amd64

ENV BENCHMARK_ROOT=/opt/benchmark \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
ENV PATH="/opt/lean/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git python3 libgmp10 libstdc++6 libgomp1 libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# The separate artifacts context supplies both the pinned toolchain and the
# complete compiled checkout. Relative dependency paths survive relocation.
COPY --from=precompiled /toolchain/ /opt/lean/
WORKDIR /opt/benchmark
COPY --from=precompiled /checkout/ ./

COPY docker/entrypoint.sh /usr/local/share/benchmark/entrypoint.sh
COPY docker/Catalog.lean /usr/local/share/benchmark/Catalog.lean
COPY lean-toolchain /usr/local/share/benchmark/lean-toolchain

# --no-build rejects missing compiled artifacts. Only catalog extraction and
# suite preparation execute here; no repositories are downloaded or compiled.
RUN --network=none mkdir -p /opt/benchmark-data /results \
    && cmp lean-toolchain /usr/local/share/benchmark/lean-toolchain \
    && lake --no-build lean /usr/local/share/benchmark/Catalog.lean \
    && /bin/sh /usr/local/share/benchmark/entrypoint.sh \
      --count 1 --extract-only --output /tmp/offline-check \
    && rm -rf /tmp/offline-check

ENTRYPOINT ["/bin/sh", "/usr/local/share/benchmark/entrypoint.sh"]
