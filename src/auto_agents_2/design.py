from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import shutil
import socket
import threading
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from .errors import AA2Error, PrototypeBrowserError
from .progress import capture_output, current_activity
from .sandbox import SandboxRunner
from .util import atomic_write, digest, scrub_text

PROTOTYPE_BROWSER_VERSION = 2


class DesignCatalog:
    def __init__(self, cache: Path, sandbox: SandboxRunner | None = None):
        self.cache = cache
        self.sandbox = sandbox or SandboxRunner(cache)

    async def candidates(self, requirement_text: str) -> list[dict]:
        index = self.cache / "index.json"
        data = json.loads(index.read_text()) if index.exists() else {}
        if data.get("source") != "git-commit":
            data = await self._git_catalog()
            atomic_write(index, json.dumps(data))
        paths = [n["path"] for n in data.get("tree", []) if n["path"].endswith("DESIGN.md")]
        if not paths:
            raise AA2Error("Design catalog contains no DESIGN.md files")
        words = requirement_text.lower().split()
        paths.sort(key=lambda p: (-sum(w in p.lower() for w in words if len(w) > 2), p))
        sha = data["sha"]
        return [{"path": p, "url": f"https://raw.githubusercontent.com/VoltAgent/awesome-design-md/{sha}/{quote(p)}",
                 "revision": sha} for p in paths]

    async def _git_catalog(self):
        """Resolve an actual commit through public Git, without relying on API quota."""
        self.cache.mkdir(parents=True, exist_ok=True)
        repo = self.cache / "repository"
        variables = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

        async def command(args):
            process = await self.sandbox.spawn(["git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", *args],
                                               self.cache, variables, [repo])
            try:
                async with asyncio.timeout(120):
                    stdout, stderr = await capture_output(process)
                if process.returncode:
                    raise AA2Error("Design catalog Git fetch failed: " + stderr.decode(errors="replace")[-1500:])
                return stdout.decode()
            finally:
                await self.sandbox.stop(process)

        if not (repo / ".git").exists():
            if repo.exists():
                shutil.rmtree(repo)
            await command(["clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "--single-branch", "--branch", "main",
                           "https://github.com/VoltAgent/awesome-design-md.git", str(repo)])
        sha = (await command(["-C", str(repo), "rev-parse", "HEAD"])).strip()
        paths = (await command(["-C", str(repo), "ls-tree", "-r", "--name-only", "-z", "HEAD"])).split("\0")
        return {"source": "git-commit", "sha": sha, "tree": [{"path": path} for path in paths if path]}


class PreviewServer:
    def __init__(self, directory: Path):
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        handler = functools.partial(Handler, directory=str(directory))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()


async def exercise(page, actions, route, completed=None):
    if completed is None:
        completed = []
    for step in actions:
        if step.route != route:
            continue
        locator = page.locator(step.selector)
        if step.action == "expect_visible":
            await locator.wait_for(state="visible", timeout=10000)
        elif step.action == "expect_text":
            from playwright.async_api import expect
            await expect(locator).to_have_text(step.value, timeout=10000)
        elif step.action in {"fill", "press"}:
            await getattr(locator, step.action)(step.value, timeout=10000)
        else:
            await getattr(locator, step.action)(timeout=10000)
        completed.append(step.model_dump())
    return completed


def prototype_routes(directory: Path, actions) -> list[tuple[str, str]]:
    """Pair declared routes with local URLs; retain declarations as evidence identities."""
    pages = {"/" + p.relative_to(directory).as_posix(): p for p in sorted(directory.rglob("*.html"))}
    routes = {}
    for action in actions:
        route = action.route
        parsed = urlsplit(route)
        path = unquote(parsed.path)
        if (parsed.scheme or parsed.netloc or not path.startswith("/") or path.startswith("//")
                or "\\" in path or any(p in {".", ".."} for p in path.split("/"))):
            raise AA2Error(f"Prototype route must be a local path: {route}")
        if path.endswith("/"):
            path += "index.html"
        target = path
        if target not in pages:
            # Support old proposals rooted at a single nested prototype directory.
            matches = [p for p in pages if p.endswith(path)] if path.count("/") == 1 else []
            if len(matches) != 1:
                raise AA2Error(f"Prototype route is missing or ambiguous: {route}; use a path relative to design/prototypes")
            target = matches[0]
        if not pages[target].resolve().is_relative_to(directory.resolve()):
            raise AA2Error(f"Prototype route escapes its directory: {route}")
        routes[route] = urlunsplit(("", "", quote(target, safe="/"), parsed.query, parsed.fragment))
    for path in pages:
        if not pages[path].resolve().is_relative_to(directory.resolve()):
            raise AA2Error(f"Prototype page escapes its directory: {path}")
        resolved = quote(path, safe="/")
        if resolved not in routes.values():
            routes.setdefault(path, resolved)
    if not routes:
        raise AA2Error("Prototype has no HTML pages")
    return list(routes.items())


async def inspect_prototype(directory: Path, output: Path, actions=()) -> list[dict]:
    """Render static pages in a real browser; save screenshots before human approval."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise AA2Error("Install aa2[browser] in the aa2 environment for prototype verification") from exc
    try:
        routes = prototype_routes(directory, actions)
    except (AA2Error, ValueError) as exc:
        raise PrototypeBrowserError(str(exc), []) from exc
    output.mkdir(parents=True, exist_ok=True)
    results = []
    activity = current_activity.get()
    if activity:
        activity.progress(0, len(routes) * 2)
    with PreviewServer(directory) as server:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch()
            except Exception as exc:
                raise PrototypeBrowserError("Prototype browser startup failed: " + scrub_text(str(exc)), []) from exc
            try:
                for route, resolved in routes:
                    for width, height in ((1440, 900), (390, 844)):
                        page = await browser.new_page(viewport={"width": width, "height": height})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(scrub_text(str(error))))
                        async def local_only(request):
                            if urlsplit(request.request.url).netloc == urlsplit(server.url).netloc:
                                await request.continue_()
                            else:
                                errors.append("Prototype attempted external request: " + request.request.url)
                                await request.abort()
                        await page.route("**/*", local_only)
                        completed = []
                        row = {"page": unquote(urlsplit(resolved).path).lstrip("/"), "route": route,
                               "resolved_route": resolved, "viewport": width, "status": 0,
                               "errors": errors, "actions": completed}
                        results.append(row)
                        try:
                            response = await page.goto(server.url.rstrip("/") + resolved, wait_until="networkidle")
                            row["status"] = response.status if response else 0
                            await exercise(page, actions, route, completed)
                        except Exception as exc:
                            errors.append(scrub_text(str(exc)))
                            sequence = [a for a in actions if a.route == route]
                            if row["status"] and len(completed) < len(sequence):
                                row["failed_action"] = sequence[len(completed)].model_dump()
                        image = output / f"route-{digest(route)[:16]}-{width}.png"
                        try:
                            await page.screenshot(path=str(image), full_page=True)
                            row["screenshot"] = str(image)
                        except Exception as exc:
                            errors.append(scrub_text(str(exc)))
                        if activity:
                            activity.output()
                            activity.progress(len(results), len(routes) * 2)
                        await page.close()
                        if errors or not 200 <= row["status"] < 400:
                            raise PrototypeBrowserError("Prototype browser checks failed: " + json.dumps(row), results)
            finally:
                await browser.close()
    return results


async def inspect_application(spec, workspace, output, environment, sandbox, root):
    from .config import ConfigStore
    from .resources import ResourceManager
    from .store import Store
    resources = ResourceManager(Store(root.parent), ConfigStore(root.parent).load().cleanup)
    with resources.temporary("preview") as scratch:
        return await _inspect_application(spec, workspace, output, environment, sandbox, root, scratch)


async def _inspect_application(spec, workspace, output, environment, sandbox, root, scratch):
    import asyncio

    from playwright.async_api import async_playwright

    from .util import contained

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    argv = [a.replace("{port}", str(port)) for a in spec.command.argv]
    if not any("{port}" in a for a in spec.command.argv):
        raise AA2Error("Preview command must contain a {port} placeholder")
    cwd = workspace if spec.command.cwd == "." else contained(workspace, spec.command.cwd)
    process = await sandbox.spawn(argv, cwd, environment.variables(scratch),
                                  [scratch, cwd / ".next", cwd / "dist", cwd / "node_modules/.vite"], [environment.prefix])
    # Drain pipes to avoid deadlocking a long-running server; retain a bounded diagnostic tail.
    tail = []

    async def drain(stream):
        while line := await stream.read(8192):
            if current_activity.get():
                current_activity.get().output()
            tail.append(line.decode(errors="replace")[-2000:])
            del tail[:-30]

    drains = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr))]
    results = []
    activity = current_activity.get()
    if activity:
        activity.progress(0, len(spec.routes) * 2)
    output.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(60):
                if process.returncode is not None:
                    raise AA2Error("Preview process exited: " + "".join(tail))
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/")
                    if response.status_code < 500:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            else:
                raise AA2Error("Preview server readiness timeout: " + "".join(tail))
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                for index, route in enumerate(spec.routes):
                    if not route.startswith("/") or route.startswith("//"):
                        raise AA2Error("Preview routes must be local paths")
                    for width, height in ((1440, 900), (390, 844)):
                        page = await browser.new_page(viewport={"width": width, "height": height})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        response = await page.goto(f"http://127.0.0.1:{port}{route}", wait_until="networkidle")
                        completed = await exercise(page, spec.actions, route)
                        screenshot = output / f"route-{index}-{width}.png"
                        await page.screenshot(path=str(screenshot), full_page=True)
                        results.append({"route": route, "viewport": width, "errors": errors,
                                        "status": response.status if response else 0, "screenshot": str(screenshot), "actions": completed})
                        if activity:
                            activity.output()
                            activity.progress(len(results), len(spec.routes) * 2)
                        await page.close()
            finally:
                await browser.close()
        if any(r["errors"] or r["status"] >= 400 for r in results):
            raise AA2Error("Implemented UI browser checks failed: " + json.dumps(results))
        return results
    finally:
        await sandbox.stop(process)
        await asyncio.gather(*drains, return_exceptions=True)
        shutil.rmtree(scratch, ignore_errors=True)


def browser_environment(cache: Path):
    """Configure the browser driver of this single-project CLI process, never the user's shell."""
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
