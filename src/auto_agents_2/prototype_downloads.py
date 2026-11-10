"""Controller-owned passive observations of downloads from the existing prototype actions.

Downloaded bytes are never executed. Format facts come from controller code and a
clean browser decoder, not from candidate DOM, helper modules or filename claims.
"""
from __future__ import annotations

import asyncio
import base64
import math
import re
from urllib.parse import urlsplit

from .cleanup import finalize_async
from .safe_files import directory_fd, regular_file
from .util import digest, identifier, scrub_text

MAX_FILES = 64
MAX_ROW_FILES = 8
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_SRT_BYTES = 2 * 1024 * 1024
DOWNLOAD_SECONDS = 10
ACTION_SETTLE_SECONDS = .1
FINAL_SETTLE_SECONDS = .25


def download_capture_profile():
    body = {
        "id": "prototype-passive-download-v1",
        "producer": "inspect_prototype",
        "extends": "controller_prototype_contract v1; download_bytes are absent from the base profile",
        "limits": {"files": MAX_FILES, "files_per_row": MAX_ROW_FILES, "file_bytes": MAX_FILE_BYTES,
                   "total_bytes": MAX_TOTAL_BYTES, "srt_bytes": MAX_SRT_BYTES, "download_seconds": DOWNLOAD_SECONDS,
                   "action_settle_seconds": ACTION_SETTLE_SECONDS, "final_settle_seconds": FINAL_SETTLE_SECONDS},
        "base_exclusion_override": {"field": "not_collected.download_bytes",
                                    "scope": "Only actual rows carrying this profile_hash; never legacy rows"},
        "records": ["actual download events including failures", "raw file hash refs", "complete UTF-8 SRT text/structure",
                    "MP4 container plus clean Chromium decoded-frame facts"],
        "attribution": "The event's observed action window, not proof of delayed JavaScript causality. "
                       "Page-load, popup and post-action events are identified separately.",
        "authority": "Passive observation only. No new actions, collectors, product acceptance or human consent. "
                     "The base profile's download_bytes exclusion is extended only by rows carrying this exact profile. "
                     "Missing legacy download_capture means not observed, never observed none. File contents/names "
                     "are candidate data. MP4 bytes are opaque attachments, not required LLM text reading.",
        "limits_note": "Archive bounds are enforced before publication; private browser spool sizes are polled "
                       "during acquisition and excess/incomplete transfers are cancelled, never published as complete.",
    }
    return {**body, "hash": digest(body)}


def srt_facts(raw):
    """Validate the complete bounded text without executing or interpreting markup."""
    if len(raw) > MAX_SRT_BYTES:
        return {"format": "srt", "status": "unverified", "reason": "SRT text exceeds inspection budget"}, None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        return {"format": "srt", "status": "invalid", "reason": "Not UTF-8"}, None
    normalized = text.replace("\r\n", "\n")
    blocks = re.split(r"\n[ \t]*\n", normalized.strip())
    timing = re.compile(r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$")
    end = 0
    for index, block in enumerate(blocks, 1):
        lines = block.split("\n")
        match = timing.fullmatch(lines[1]) if len(lines) >= 3 else None
        if index > 10000 or lines[0] != str(index) or not match or not any(line.strip() for line in lines[2:]):
            return {"format": "srt", "status": "invalid", "reason": "Incomplete or invalid SRT cue",
                    "cue_index": index}, text
        parts = [int(value) for value in match.groups()]
        start, stop = [(h * 3600 + m * 60 + s) * 1000 + ms
                       for h, m, s, ms in (parts[:4], parts[4:])]
        if stop <= start:
            return {"format": "srt", "status": "invalid", "reason": "Invalid cue interval",
                    "cue_index": index}, text
        end = max(end, stop)
    return {"format": "srt", "status": "valid", "encoding": "UTF-8", "cue_count": len(blocks),
            "end_ms": end, "text_complete": True}, text


async def mp4_facts(browser, raw):
    """Decode a real video frame in an empty controller-owned, offline context."""
    if len(raw) < 12 or raw[4:8] != b"ftyp":
        return {"format": "mp4", "status": "invalid", "reason": "Missing MP4 container signature"}
    context = None
    try:
        async with asyncio.timeout(7):
            context = await browser.new_context(service_workers="block")
            await context.route("**/*", lambda route: route.abort())
            await context.route_web_socket("**/*", lambda socket: socket.close())
            page = await context.new_page()
            facts = await page.evaluate("""async encoded => {
                const raw = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
                const url = URL.createObjectURL(new Blob([raw], {type:'video/mp4'}));
                const video = document.createElement('video');
                video.muted = true; video.preload = 'auto';
                document.body.append(video);
                try {
                    await new Promise((resolve, reject) => {
                        const timer = setTimeout(() => reject(new Error('decoder timeout')), 5000);
                        video.onloadeddata = () => {clearTimeout(timer); resolve();};
                        video.onerror = () => {clearTimeout(timer); reject(new Error('decoder rejected media'));};
                        video.src = url; video.load();
                    });
                    if (!(video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0 &&
                          Number.isFinite(video.duration) && video.duration > 0))
                        return {format:'mp4', status:'invalid', reason:'No decoded video frame'};
                    const canvas = document.createElement('canvas');
                    canvas.width = 1; canvas.height = 1;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(video, 0, 0, 1, 1);
                    const pixel = [...ctx.getImageData(0, 0, 1, 1).data];
                    return {format:'mp4', status:'valid', decoder:'clean Chromium video/canvas',
                            decoded_frame:true, width:video.videoWidth, height:video.videoHeight,
                            duration_seconds:video.duration, frame_pixel:pixel};
                } finally {
                    video.removeAttribute('src'); video.load(); video.remove(); URL.revokeObjectURL(url);
                }
            }""", base64.b64encode(raw).decode("ascii"))
            return facts
    except Exception as exc:
        return {"format": "mp4", "status": "unverified", "reason": scrub_text(str(exc))[:500]}
    finally:
        if context is not None:
            await finalize_async(context.close)


class DownloadBudget:
    def __init__(self):
        self.files = 0
        self.bytes = 0


class PassiveDownloads:
    def __init__(self, browser, context, output, spool, row, actions_hash, source_tree, budget):
        self.browser, self.context, self.output, self.spool = browser, context, output, spool
        self.row, self.budget = row, budget
        self.window = {"phase": "page_load", "action_index": None, "action": None}
        self.tasks, self.pages = [], []
        self.closed = False
        self.overflow = False
        self.stop_task = None
        self.capture = {"profile_hash": download_capture_profile()["hash"], "prototype_tree_hash": source_tree,
                        "actions_hash": actions_hash, "status": "observing", "events": [], "overflow": False}
        row["download_capture"] = self.capture
        context.on("page", self.attach_page)

    def attach_page(self, page):
        self.pages.append(page)
        page.on("download", self.observe)

    def observe(self, download):
        if self.closed:
            return
        self.budget.files += 1
        if len(self.capture["events"]) >= MAX_ROW_FILES or self.budget.files > MAX_FILES:
            # One bounded failure record; close the originating context rather
            # than spawning unbounded tasks or accepting further transfers.
            self.capture["overflow"] = self.overflow = True
            if self.stop_task is None:
                self.stop_task = asyncio.create_task(self.context.close())
            return
        name = download.suggested_filename
        try:
            scheme = urlsplit(download.url).scheme
        except ValueError:
            scheme = "unparseable"
        event = {"index": len(self.capture["events"]), "suggested_filename": scrub_text(name[:1024]),
                 "filename_hash": digest(name), "filename_characters": len(name),
                 "url_hash": digest(download.url), "url_scheme": scheme,
                 "window": dict(self.window), "page_index": self.pages.index(download.page), "status": "pending"}
        self.capture["events"].append(event)
        self.tasks.append(asyncio.create_task(self.receive(download, event, name)))

    async def after_action(self, *, settle=True):
        await asyncio.sleep(ACTION_SETTLE_SECONDS if settle else 0)
        self.window = {"phase": "between_actions", "action_index": None, "action": None}
        if self.overflow:
            raise ValueError("Prototype download event limit exceeded")

    async def receive(self, download, event, name):
        folder = self.output / "downloads"
        folder.mkdir(exist_ok=True)
        storage = identifier("download") + ".bin"
        path = folder / storage
        saving = asyncio.create_task(download.save_as(str(path)))
        try:
            async with asyncio.timeout(DOWNLOAD_SECONDS):
                while not saving.done():
                    # Playwright's own private spool uses generated identities,
                    # never the candidate filename. No arbitrary paths are read.
                    sizes = [item.stat().st_size for item in self.spool.iterdir() if item.is_file()]
                    if (any(size > MAX_FILE_BYTES for size in sizes)
                            or self.budget.bytes + sum(sizes) > MAX_TOTAL_BYTES):
                        event.update(status="limit_exceeded", reason="Download byte budget exceeded")
                        await download.cancel()
                        return
                    await asyncio.sleep(.02)
                await saving
                failure = await download.failure()
                if failure:
                    event.update(status="cancelled" if failure == "canceled" else "failed",
                                 reason=scrub_text(failure)[:500])
                    return
                await download.delete()
                size = path.stat().st_size
                if size > MAX_FILE_BYTES or self.budget.bytes + size > MAX_TOTAL_BYTES:
                    event.update(status="limit_exceeded", reason="Download byte budget exceeded")
                    return
                raw = path.read_bytes()
                if len(raw) != size:
                    event.update(status="incomplete", reason="Downloaded file changed during inspection")
                    return
                self.budget.bytes += size
                event.update(status="complete", bytes=size, file_hash=digest(raw), storage_name=storage)
                extension = name.rsplit(".", 1)[-1].lower()
                if extension == "srt":
                    facts, text = srt_facts(raw)
                    event["facts"] = facts
                    if text is not None:
                        event["utf8_text"] = text
                elif extension == "mp4" or raw[4:8] == b"ftyp":
                    event["facts"] = await mp4_facts(self.browser, raw)
                else:
                    event["facts"] = {"format": "unknown", "status": "unverified",
                                      "reason": "Bytes retained; no format decoder in this profile"}
        except TimeoutError:
            event.update(status="incomplete", reason="Download observation timed out")
            await download.cancel()
        except Exception as exc:
            failure = None
            try:
                async with asyncio.timeout(1):
                    failure = await download.failure()
            except Exception:
                pass
            event.update(status="cancelled" if failure == "canceled" else "failed",
                         reason=scrub_text(failure or str(exc))[:500])
        finally:
            if not saving.done():
                saving.cancel()
            await asyncio.gather(saving, return_exceptions=True)
            if event["status"] != "complete":
                path.unlink(missing_ok=True)

    async def finish(self):
        try:
            await asyncio.sleep(FINAL_SETTLE_SECONDS)
            # Events arriving while transfers settle are included too.
            cursor = 0
            while cursor < len(self.tasks):
                batch = self.tasks[cursor:]
                cursor = len(self.tasks)
                await asyncio.gather(*batch)
            self.closed = True
            self.capture["status"] = ("incomplete" if self.overflow or any(
                event["status"] != "complete" for event in self.capture["events"])
                else "observed" if self.capture["events"] else "none")
            if self.stop_task is not None:
                await self.stop_task
        finally:
            self.closed = True
            self.context.remove_listener("page", self.attach_page)
            for page in self.pages:
                page.remove_listener("download", self.observe)


def archive_downloads(rows, artifacts, output, *, source_hash, actions_hash, browser_version, source_tree):
    """Publish only controller-chosen files and bind the raw/derived hash closure."""
    for row in rows:
        capture = row.get("download_capture")
        if capture is None:
            continue  # Legacy or test observation, never synthesize capture.
        if (capture["profile_hash"] != download_capture_profile()["hash"]
                or capture["prototype_tree_hash"] != source_tree or capture["actions_hash"] != actions_hash):
            raise ValueError("Prototype download observation inputs changed")
        binding = {"source_hash": source_hash, "actions_hash": actions_hash, "browser_version": browser_version,
                   "actual_browser_version": row["actual_browser_version"], "route": row["route"],
                   "viewport": row["viewport"], "profile_hash": capture["profile_hash"]}
        capture["binding"] = binding
        for event in capture["events"]:
            if event["status"] != "complete":
                continue
            storage = event.pop("storage_name")
            if not re.fullmatch(r"download_[0-9a-f]{16}\.bin", storage):
                raise ValueError("Invalid controller download identity")
            with directory_fd(output / "downloads") as folder:
                with regular_file(folder, storage) as incoming:
                    ref = artifacts.put_stream(incoming, "prototype-download", max_bytes=MAX_FILE_BYTES)
            if ref["hash"] != event["file_hash"] or ref["bytes"] != event["bytes"]:
                raise ValueError("Downloaded bytes changed before archive")
            attachments = {"raw": ref}
            if "utf8_text" in event:
                attachments["text"] = artifacts.put(event.pop("utf8_text"), "prototype-download-text")
            facts = {"binding": binding, "window": event["window"], "raw": ref, "facts": event["facts"],
                     "text": attachments.get("text"), "filename_hash": event["filename_hash"]}
            attachments["facts"] = artifacts.put(facts, "prototype-download-facts")
            event["attachments"] = attachments
        capture["hash"] = digest(capture)
    return rows


def valid_downloads(rows, artifacts, binding, actions, browser_version):
    """New observations require their complete closure; absent legacy fields stay absent."""
    captures = [row.get("download_capture") for row in rows]
    if not any(capture is not None for capture in captures):
        return True
    if any(not isinstance(capture, dict) for capture in captures):
        return False
    try:
        total_files, total_bytes = 0, 0
        for row, capture in zip(rows, captures):
            expected = {"source_hash": binding["hash"], "actions_hash": binding["actions_hash"],
                        "browser_version": browser_version, "actual_browser_version": row["actual_browser_version"],
                        "route": row["route"], "viewport": row["viewport"],
                        "profile_hash": download_capture_profile()["hash"]}
            if (capture["binding"] != expected or capture["profile_hash"] != expected["profile_hash"]
                    or capture["actions_hash"] != digest(actions)
                    or capture["hash"] != digest({k: v for k, v in capture.items() if k != "hash"})
                    or capture["status"] != ("incomplete" if capture["overflow"] or any(
                        event["status"] != "complete" for event in capture["events"])
                        else "observed" if capture["events"] else "none")
                    or len(capture["events"]) > MAX_ROW_FILES
                    or (capture["status"] == "none") != (not capture["events"] and not capture["overflow"])):
                return False
            sequence = [action for action in actions if action["route"] == row["route"]]
            total_files += len(capture["events"])
            for index, event in enumerate(capture["events"]):
                if event["index"] != index:
                    return False
                window = event["window"]
                if window["phase"] == "action":
                    step = window["action_index"]
                    if not isinstance(step, int) or not 0 <= step < len(sequence) or window["action"] != sequence[step]:
                        return False
                elif window["phase"] not in {"page_load", "between_actions", "capture", "post_actions"}:
                    return False
                if event["status"] == "complete":
                    attachments = event["attachments"]
                    ref = attachments["raw"]
                    raw = artifacts.bytes(ref)
                    if (ref["kind"] != "prototype-download" or len(raw) != ref["bytes"]
                            or ref["bytes"] != event["bytes"] or ref["hash"] != event["file_hash"]
                            or len(raw) > MAX_FILE_BYTES):
                        return False
                    total_bytes += len(raw)
                    text_ref = attachments.get("text")
                    if text_ref is not None:
                        text = artifacts.read(text_ref)
                        if text_ref["kind"] != "prototype-download-text" or len(text.encode()) != text_ref["bytes"]:
                            return False
                    if event["facts"]["format"] == "srt":
                        derived, expected_text = srt_facts(raw)
                        if derived != event["facts"] or (text_ref is not None) != (expected_text is not None):
                            return False
                        if text_ref is not None and text != expected_text:
                            return False
                    if event["facts"]["format"] == "mp4" and event["facts"]["status"] == "valid":
                        facts = event["facts"]
                        if (raw[4:8] != b"ftyp" or facts.get("decoded_frame") is not True
                                or facts.get("decoder") != "clean Chromium video/canvas"
                                or facts.get("width", 0) <= 0 or facts.get("height", 0) <= 0
                                or not math.isfinite(facts.get("duration_seconds", 0))
                                or facts.get("duration_seconds", 0) <= 0):
                            return False
                    facts_ref = attachments["facts"]
                    facts = artifacts.json(facts_ref)
                    if (len(artifacts.bytes(facts_ref)) != facts_ref["bytes"]
                            or facts_ref["kind"] != "prototype-download-facts"
                            or facts != {"binding": expected, "window": window, "raw": ref, "facts": event["facts"],
                                         "text": text_ref, "filename_hash": event["filename_hash"]}):
                        return False
                elif event["status"] not in {"failed", "cancelled", "incomplete", "limit_exceeded"} or "attachments" in event:
                    return False
        if total_files > MAX_FILES or total_bytes > MAX_TOTAL_BYTES:
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True

