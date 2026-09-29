"""Bounded queue worker. Only the child browser sees the copied project snapshot."""

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from uuid import UUID

QUEUE = Path("/runner-queue/browser")
WORKSPACES = Path("/workspaces")
for name in ("requests", "responses", "artifacts"):
    (QUEUE / name).mkdir(parents=True, exist_ok=True)


def capture(request):
    identifier = str(UUID(request["id"]))
    company, project = request["workspace"].split("/")
    relative = Path(str(UUID(company))) / str(UUID(project))
    source = WORKSPACES / relative
    if any(p.is_symlink() for p in (source, source.parent)):
        raise ValueError("symlink workspace")
    source.resolve(strict=True).relative_to(WORKSPACES)
    target = Path(request["path"])
    if (
        target.is_absolute()
        or ".." in target.parts
        or target.suffix not in {".html", ".htm"}
    ):
        raise ValueError("invalid page")
    if not 320 <= request["width"] <= 1920 or not 240 <= request["height"] <= 1440:
        raise ValueError("invalid viewport")
    sandbox = Path("/sandboxes") / identifier
    sandbox.mkdir()
    site = sandbox / "site"
    site.mkdir()
    total = count = 0
    try:
        for directory, names, files in os.walk(source, followlinks=False):
            names[:] = [
                n
                for n in names
                if not n.startswith(".")
                and n != "node_modules"
                and not (Path(directory) / n).is_symlink()
            ]
            for name in files:
                path = Path(directory) / name
                if name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                if path.suffix in {".pem", ".key"} or "credentials" in name.lower():
                    continue
                total += path.stat().st_size
                count += 1
                if total > 30_000_000 or count > 2000:
                    raise ValueError("snapshot limit exceeded")
                destination = site / path.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        (sandbox / "request.json").write_text(json.dumps(request))
        process = subprocess.Popen(
            [
                "/usr/local/bin/forge-sandbox-exec",
                str(sandbox),
                "python3",
                "/usr/local/bin/capture.py",
            ],
            cwd=sandbox,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(sandbox)},
        )
        try:
            if process.wait(timeout=40) != 0:
                raise ValueError("browser process failed")
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        screenshot = sandbox / "screenshot.png"
        if screenshot.stat().st_size > 10_000_000:
            raise ValueError("artifact too large")
        shutil.copyfile(screenshot, QUEUE / "artifacts" / f"{identifier}.png")
        return json.loads((sandbox / "result.json").read_text())
    finally:
        shutil.rmtree(sandbox)


while True:
    for path in (QUEUE / "requests").glob("*.json"):
        try:
            identifier = str(UUID(path.stem))
            request = json.loads(path.read_text())
            if request["id"] != identifier:
                raise ValueError("identity mismatch")
            response = capture(request)
        except Exception:
            response = {"status": "error"}
        output = QUEUE / "responses" / path.name
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(response))
        os.replace(temporary, output)
        path.unlink()
    time.sleep(0.2)
