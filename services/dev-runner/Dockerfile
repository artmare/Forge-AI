FROM node:22-bookworm-slim AS sandbox-builder

RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev linux-libc-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY sandbox_exec.c ./sandbox_exec.c
RUN gcc -O2 -Wall -Wextra -Werror -o forge-sandbox-exec sandbox_exec.c

FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git python3 python3-pip python3-venv ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 forge-runner \
    && useradd --system --uid 10001 --gid forge-runner --home-dir /tmp/forge-runner-home forge-runner

WORKDIR /runner
COPY --from=sandbox-builder /build/forge-sandbox-exec /usr/local/bin/forge-sandbox-exec
COPY --chown=forge-runner:forge-runner runner.py ./runner.py
USER forge-runner
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/runner/runner.py"]
