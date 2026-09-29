FROM python:3.13-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev linux-libc-dev
COPY dev-runner/sandbox_exec.c /build/sandbox_exec.c
RUN gcc -DFORGE_BROWSER_SANDBOX -O2 -Wall -Wextra -Werror -o /build/forge-sandbox-exec /build/sandbox_exec.c
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends chromium tini \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir playwright==1.55.0 \
    && useradd --uid 10001 --create-home forge \
    && mkdir /sandboxes && chown forge /sandboxes
COPY --from=build /build/forge-sandbox-exec /usr/local/bin/forge-sandbox-exec
COPY browser-runner/capture.py /usr/local/bin/capture.py
COPY browser-runner/worker.py /usr/local/bin/browser-worker.py
USER forge
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/usr/local/bin/browser-worker.py"]
