# SPDX-FileCopyrightText: Copyright 2026 Arm Limited and its affiliates <open-source-office@arm.com>
# SPDX-License-Identifier: Apache-2.0
#
# Multi-architecture PoC environment for the host tool replay example.
#
# Build from the repository root:
#   docker build -f examples/13_SWEBench_Host_Tool_Replay_Example/Dockerfile \
#     --build-arg USER_ID=$(id -u) --build-arg GROUP_ID=$(id -g) \
#     -t host-replay-poc examples/13_SWEBench_Host_Tool_Replay_Example
#
# Run from the repository root:
#   docker run --rm -it -v "$(pwd)":/workspace -w /workspace host-replay-poc bash

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG USER_ID=1000
ARG GROUP_ID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      autoconf \
      automake \
      bash \
      build-essential \
      ca-certificates \
      cmake \
      curl \
      file \
      findutils \
      g++ \
      gcc \
      git \
      golang-go \
      grep \
      gzip \
      libtool \
      libtool-bin \
      make \
      numactl \
      patch \
      pkg-config \
      procps \
      protobuf-compiler \
      python3 \
      python3-pip \
      python3-venv \
      sed \
      tar \
      util-linux \
      xz-utils && \
    rm -rf /var/lib/apt/lists/*

RUN if ! getent group "${GROUP_ID}" >/dev/null; then \
      groupadd -g "${GROUP_ID}" appuser; \
    fi && \
    useradd -u "${USER_ID}" -g "${GROUP_ID}" --create-home --shell /bin/bash appuser --no-log-init && \
    mkdir -p /workspace && \
    chown "${USER_ID}:${GROUP_ID}" /workspace

USER appuser
WORKDIR /workspace

CMD ["bash"]
