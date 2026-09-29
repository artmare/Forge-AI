"""Runs under Landlock inside a network-disabled container, with one project snapshot."""

import json
import mimetypes
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright

root = Path.cwd()
request = json.loads((root / "request.json").read_text())
content = root / "site"
errors = []


def route_request(route):
    url = urlparse(route.request.url)
    relative = Path(unquote(url.path).lstrip("/"))
    if url.netloc != "forge.local" or ".." in relative.parts:
        return route.abort()
    target = content / relative
    if any(part.startswith(".") for part in relative.parts) or target.is_symlink():
        return route.abort()
    try:
        target.resolve(strict=True).relative_to(content)
        if not target.is_file() or target.stat().st_size > 5_000_000:
            return route.abort()
        route.fulfill(
            body=target.read_bytes(),
            content_type=mimetypes.guess_type(target)[0] or "application/octet-stream",
        )
    except (ValueError, OSError):
        route.abort()


with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path="/usr/bin/chromium",
        headless=True,
        args=["--disable-dev-shm-usage"],
    )
    context = browser.new_context(
        viewport={"width": request["width"], "height": request["height"]},
        service_workers="block",
    )
    context.route("**/*", route_request)
    page = context.new_page()
    page.on(
        "pageerror",
        lambda error: errors.append(str(error)[:500]) if len(errors) < 20 else None,
    )
    page.goto("http://forge.local/" + request["path"], wait_until="load", timeout=15000)
    page.screenshot(path=str(root / "screenshot.png"), timeout=10000)
    (root / "result.json").write_text(
        json.dumps(
            {"status": "success", "title": page.title(), "console_errors": errors}
        )
    )
    browser.close()
sys.exit(0)
