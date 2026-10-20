from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import shutil
import socket
import stat
import sys
import threading
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from .cleanup import finalize_async
from .errors import AA2Error, PrototypeBrowserError
from .evidence import NODE_RUNTIME_CACHES
from .network import OWNERSHIP_FAILURE, PreviewProxy, preview_owner
from .process_ownership import PreviewOwnershipError
from .progress import capture_output, current_activity
from .safe_files import directory_fd, regular_file
from .sandbox import SandboxRunner
from .util import atomic_write, digest, scrub_text

PROTOTYPE_BROWSER_VERSION = 6


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
        directory = directory.absolute()
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_head(self):
                try:
                    path = Path(self.translate_path(self.path))
                    path.relative_to(directory.absolute())
                    # Every parent and the final component are opened without following links.
                    with directory_fd(path.parent) as parent:
                        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            with directory_fd(path) as folder:
                                parsed = urlsplit(self.path)
                                if not parsed.path.endswith("/"):
                                    self.send_response(301)
                                    self.send_header("Location", urlunsplit(parsed._replace(path=parsed.path + "/")))
                                    self.send_header("Content-Length", "0")
                                    self.end_headers()
                                    return None
                                stream = None
                                for name in ("index.html", "index.htm"):
                                    try:
                                        stream = regular_file(folder, name)
                                        path = path / name
                                        break
                                    except FileNotFoundError:
                                        continue
                                if stream is None:
                                    raise FileNotFoundError("Directory index unavailable")
                        else:
                            stream = regular_file(parent, path.name)
                    try:
                        self.send_response(200)
                        self.send_header("Content-type", self.guess_type(str(path)))
                        self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
                        self.end_headers()
                        return stream
                    except BaseException:
                        stream.close()
                        raise
                except (OSError, ValueError):
                    self.send_error(404, "Preview file unavailable")
                    return None
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


async def capture_page(page, image, row, actions=()):
    """Capture intact viewport images; Chromium's beyond-viewport capture can resize to 1x1.

    Preserve the immediate final-state image and record additional scroll positions
    for content below/outside it. Never resize, navigate, crop or stitch the evidence.
    """
    async def geometry():
        return await page.evaluate("""() => ({x: scrollX, y: scrollY, width: innerWidth, height: innerHeight,
            content_width: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
            content_height: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)})""")

    initial = await geometry()
    await page.screenshot(path=str(image), full_page=False)
    row.update(screenshot=str(image), viewport_height=initial["height"], capture_mode="viewport_tiles",
               capture_origin=initial, tiles=[])
    if row["errors"]:
        return
    terminal = []
    for action in reversed([a for a in actions if a.route == row["route"]]):
        if not action.action.startswith("expect_"):
            break
        terminal.insert(0, action)
    width, height = initial["width"], initial["height"]
    if width <= 0 or height <= 0:
        raise AA2Error("Screenshot viewport has no area")
    xs = sorted({min(x, max(0, initial["content_width"] - width))
                 for x in range(0, initial["content_width"], width)})
    ys = sorted({min(y, max(0, initial["content_height"] - height))
                 for y in range(0, initial["content_height"], height)})
    if len(xs) * len(ys) > 64:
        raise AA2Error("Page exceeds the 64 viewport screenshot capture limit")
    try:
        await exercise(page, terminal, row["route"])
        for y in ys:
            for x in xs:
                if (x, y) == (initial["x"], initial["y"]):
                    continue
                await page.evaluate("p => scrollTo({left: p.x, top: p.y, behavior: 'instant'})", {"x": x, "y": y})
                current = await geometry()
                if (any(current[k] != initial[k] for k in ("width", "height", "content_width", "content_height"))
                        or abs(current["x"] - x) > 1 or abs(current["y"] - y) > 1):
                    raise AA2Error("Page geometry changed during screenshot capture")
                await exercise(page, terminal, row["route"])
                tile = image.with_name(image.stem + f"-tile-{len(row['tiles'])}.png")
                await page.screenshot(path=str(tile), full_page=False)
                row["tiles"].append({"screenshot": str(tile), **current})
                await exercise(page, terminal, row["route"])
    finally:
        await finalize_async(lambda: page.evaluate(
            "p => scrollTo({left: p.x, top: p.y, behavior: 'instant'})", initial), original=sys.exception())
    await exercise(page, terminal, row["route"])


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


class PreviewContext:
    def __init__(self, context, proxy):
        self.context, self.proxy = context, proxy

    def __getattr__(self, name):
        return getattr(self.context, name)

    async def close(self):
        try:
            await self.context.close()
        except BaseException as exc:
            try:
                await self.proxy.close()
            except BaseException as cleanup:
                exc.add_note("Preview proxy cleanup failed: " + type(cleanup).__name__)
            raise
        else:
            await self.proxy.close()


async def preview_context(browser, origin, errors, width, height):
    """Apply the same network boundary to every page, popup and socket."""
    expected = urlsplit(origin)
    def local(url):
        parsed = urlsplit(url)
        scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
        return scheme == expected.scheme and parsed.hostname == expected.hostname and parsed.port == expected.port

    proxy = await PreviewProxy(origin, errors).start()
    context = None
    try:
        context = await browser.new_context(viewport={"width": width, "height": height}, service_workers="block",
                                            proxy={"server": proxy.url, "bypass": "<-loopback>"})
        context.on("close", proxy.stop)
        async def route(request):
            if local(request.request.url):
                await request.continue_()
            else:
                errors.append("Preview attempted external request")
                await request.abort()
        async def websocket(socket):
            if local(socket.url):
                socket.connect_to_server()
            else:
                errors.append("Preview attempted external WebSocket request")
                await socket.close()
        await context.route("**/*", route)
        await context.route_web_socket("**/*", websocket)
        return PreviewContext(context, proxy)
    except BaseException as exc:
        try:
            if context is not None:
                await context.close()
        except BaseException as cleanup:
            exc.add_note("Browser context cleanup failed: " + type(cleanup).__name__)
        finally:
            try:
                await proxy.close()
            except BaseException as cleanup:
                exc.add_note("Preview proxy cleanup failed: " + type(cleanup).__name__)
        raise


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
                        errors = []
                        completed = []
                        row = {"page": unquote(urlsplit(resolved).path).lstrip("/"), "route": route,
                               "resolved_route": resolved, "viewport": width, "status": 0,
                               "errors": errors, "actions": completed}
                        results.append(row)
                        context, page = None, None
                        try:
                            try:
                                context = await preview_context(browser, server.url, errors, width, height)
                                page = await context.new_page()
                                page.on("pageerror", lambda error: errors.append(scrub_text(str(error))))
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
                                if page is not None:
                                    await capture_page(page, image, row, actions)
                                else:
                                    raise AA2Error("Prototype page creation failed")
                                row["screenshot"] = str(image)
                            except Exception as exc:
                                errors.append(scrub_text(str(exc)))
                            if activity:
                                activity.output()
                                activity.progress(len(results), len(routes) * 2)
                        finally:
                            if context is not None:
                                try:
                                    await finalize_async(context.close, original=sys.exception())
                                except Exception as exc:
                                    errors.append("Browser context cleanup failed: " + scrub_text(str(exc)))
                        if errors or not 200 <= row["status"] < 400:
                            raise PrototypeBrowserError("Prototype browser checks failed: " + json.dumps(row), results)
            except PrototypeBrowserError:
                raise
            except Exception as exc:
                raise PrototypeBrowserError("Prototype browser check failed: " + scrub_text(str(exc)), results) from exc
            finally:
                original = sys.exception()
                try:
                    await browser.close()
                except Exception as exc:
                    if original is not None:
                        original.add_note("Browser cleanup failed: " + type(exc).__name__)
                    else:
                        raise PrototypeBrowserError("Prototype browser cleanup failed", results) from exc
    return results


async def inspect_application(spec, workspace, output, environment, sandbox, root):
    from .config import ConfigStore
    from .resources import ResourceManager
    from .store import Store
    resources = ResourceManager(Store(root.parent), ConfigStore(root.parent).load().cleanup)
    with resources.temporary("preview") as scratch:
        for attempt in range(3):
            try:
                return await _inspect_application(spec, workspace, output, environment, sandbox, root, scratch)
            except PreviewOwnershipError:
                if attempt == 2:
                    raise
                scratch.mkdir(exist_ok=True)


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
                                  [scratch, cwd / ".next", cwd / "dist",
                                   *(cwd / "node_modules" / name for name in sorted(NODE_RUNTIME_CACHES))], [workspace, environment.prefix])
    # Drain pipes to avoid deadlocking a long-running server; retain a bounded diagnostic tail.
    tail = []

    async def drain(stream):
        while line := await stream.read(8192):
            if current_activity.get():
                current_activity.get().output()
            tail.append(line.decode(errors="replace")[-2000:])
            del tail[:-30]

    drains, results = [], []
    owner_token = preview_owner.set(process)
    readiness = None
    try:
        drains = [asyncio.create_task(drain(process.stdout)), asyncio.create_task(drain(process.stderr))]
        activity = current_activity.get()
        if activity:
            activity.progress(0, len(spec.routes) * 2)
        output.mkdir(parents=True, exist_ok=True)
        readiness = await PreviewProxy(f"http://127.0.0.1:{port}", []).start()
        async with httpx.AsyncClient(timeout=2, trust_env=False, proxy=readiness.url) as client:
            for _ in range(60):
                if process.returncode is not None:
                    if any(marker in "".join(tail).lower() for marker in ("address already in use", "eaddrinuse")):
                        raise PreviewOwnershipError("Preview launch collided with another listener")
                    raise AA2Error("Preview process exited: " + "".join(tail))
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/")
                    if response.status_code < 500:
                        break
                except httpx.HTTPError:
                    if readiness.ownership_failed:
                        raise PreviewOwnershipError(OWNERSHIP_FAILURE) from None
                await asyncio.sleep(1)
            else:
                raise AA2Error("Preview server readiness timeout: " + "".join(tail))
        await readiness.close()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                for index, route in enumerate(spec.routes):
                    if not route.startswith("/") or route.startswith("//"):
                        raise AA2Error("Preview routes must be local paths")
                    for width, height in ((1440, 900), (390, 844)):
                        errors, completed = [], []
                        row = {"route": route, "viewport": width, "errors": errors, "status": 0, "actions": completed}
                        results.append(row)
                        context = await preview_context(browser, f"http://127.0.0.1:{port}", errors, width, height)
                        try:
                            page = await context.new_page()
                            page.on("pageerror", lambda error: errors.append(scrub_text(str(error))))
                            try:
                                response = await page.goto(f"http://127.0.0.1:{port}{route}", wait_until="networkidle")
                                row["status"] = response.status if response else 0
                                await exercise(page, spec.actions, route, completed)
                            except Exception as exc:
                                errors.append(scrub_text(str(exc)))
                                sequence = [a for a in spec.actions if a.route == route]
                                if len(completed) < len(sequence):
                                    row["failed_action"] = sequence[len(completed)].model_dump()
                            try:
                                screenshot = output / f"route-{index}-{width}.png"
                                await capture_page(page, screenshot, row, spec.actions)
                                row["screenshot"] = str(screenshot)
                            except Exception as exc:
                                errors.append(scrub_text(str(exc)))
                            if activity:
                                activity.output()
                                activity.progress(len(results), len(spec.routes) * 2)
                        finally:
                            original = sys.exception()
                            try:
                                await context.close()
                            except Exception as exc:
                                if original is not None:
                                    original.add_note("Browser context cleanup failed: " + type(exc).__name__)
                                else:
                                    errors.append("Browser context cleanup failed: " + scrub_text(str(exc)))
                        if OWNERSHIP_FAILURE in errors:
                            raise PreviewOwnershipError(OWNERSHIP_FAILURE)
                        if errors or not 200 <= row["status"] < 400:
                            raise PrototypeBrowserError("Implemented UI browser checks failed", results)
            finally:
                original = sys.exception()
                try:
                    await browser.close()
                except Exception as exc:
                    if original is None:
                        raise
                    original.add_note("Browser cleanup failed: " + type(exc).__name__)
        if not results:
            raise PrototypeBrowserError("Implemented UI browser checks produced no evidence", results)
        return results
    except (PrototypeBrowserError, PreviewOwnershipError):
        raise
    except Exception as exc:
        raise PrototypeBrowserError("Application browser check failed: " + scrub_text(str(exc)), results) from exc
    finally:
        original = sys.exception()
        preview_owner.reset(owner_token)
        cleanup_errors = []
        if readiness is not None:
            try:
                await readiness.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        try:
            await sandbox.stop(process)
        except BaseException as exc:
            cleanup_errors.append(exc)
        finally:
            try:
                for reader in drains:
                    reader.cancel()
                await asyncio.gather(*drains, return_exceptions=True)
            finally:
                if process.cleanup_confirmed:
                    shutil.rmtree(scratch, ignore_errors=True)
        if cleanup_errors:
            if original is None:
                raise cleanup_errors[0]
            for error in cleanup_errors:
                original.add_note("Preview cleanup failed: " + type(error).__name__)


def browser_environment(cache: Path):
    """Configure the browser driver of this single-project CLI process, never the user's shell."""
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
