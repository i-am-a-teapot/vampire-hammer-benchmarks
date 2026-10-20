# syntax=docker/dockerfile:1
FROM ubuntu:24.04

ARG BENCHEXEC_VERSION=3.35
ARG EXPERIMENT=configurations/normal_small.xml
ARG PROVERS=configurations/provers_sample.xml
ARG SAMPLE_SIZE=100
ARG JOBS=1

ENV BENCHMARK_SOURCE=/opt/benchmark-src \
    BENCHMARK_STATE=/var/lib/benchmark \
    BENCHMARK_DEFINITION=${EXPERIMENT} \
    BENCHMARK_PROVERS=${PROVERS} \
    BENCHMARK_COUNT=${SAMPLE_SIZE} \
    BENCHMARK_JOBS=${JOBS} \
    BENCHMARK_BUILD_JOBS=2 \
    ELAN_HOME=/var/lib/benchmark/elan \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
ENV PATH="/opt/benchexec/bin:${PATH}"

# Only the small toolchain manager is installed here. The pinned Lean toolchain,
# repositories and all compilation outputs belong in the persistent volume.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git elan curl zstd python3 python3-venv \
      build-essential cmake flex bison libgmp-dev libgomp1 libatomic1 nodejs npm \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/benchexec \
    && /opt/benchexec/bin/pip install --no-cache-dir "benchexec==${BENCHEXEC_VERSION}"

# .dockerignore excludes host toolchains, .lake, prover builds and results.
COPY . /opt/benchmark-src/
RUN mkdir -p /var/lib/benchmark /results
WORKDIR /opt/benchmark-src
VOLUME ["/var/lib/benchmark"]
ENTRYPOINT ["python3", "/opt/benchmark-src/docker/bootstrap.py"]
