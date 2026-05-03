FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git jq nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash sandbox

RUN mkdir -p /workspace && chown sandbox:sandbox /workspace

USER sandbox
WORKDIR /workspace

ENV HOME=/home/sandbox
ENV PATH="/home/sandbox/.local/bin:${PATH}"
