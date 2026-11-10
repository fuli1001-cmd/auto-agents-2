import asyncio
import base64
import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_browser import browser_cache  # noqa: F401
from test_prototype_approvals import approval_fixture

from auto_agents_2.design import inspect_prototype
from auto_agents_2.models import BrowserAction
from auto_agents_2.prototype_downloads import (
    DownloadBudget,
    PassiveDownloads,
    archive_downloads,
    download_capture_profile,
    mp4_facts,
    srt_facts,
    valid_downloads,
)
from auto_agents_2.util import digest
from auto_agents_2.workspace import tree_hash

SRT = "1\r\n00:00:00,000 --> 00:00:00,200\r\nHello, 演示。\r\n\r\n"


@pytest.fixture
def demo_mp4(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("A preinstalled trusted ffmpeg is required to generate this local fixture")
    path = tmp_path / "fixture.mp4"
    subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=blue:s=32x24:r=5", "-t", "0.4", "-an", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)], check=True)
    return path.read_bytes()


def link(raw, kind, filename, element):
    encoded = base64.b64encode(raw).decode()
    return f'<a id="{element}" download="{filename}" href="data:{kind};base64,{encoded}">Download</a>'


def archive(rows, root, output, actions):
    from auto_agents_2.artifacts import ArtifactStore
    artifacts = ArtifactStore(root)
    identity = {"hash": "synthetic-source", "actions_hash": digest([a.model_dump() for a in actions])}
    archive_downloads(rows, artifacts, output, source_hash=identity["hash"], actions_hash=identity["actions_hash"],
                      browser_version=13, source_tree=tree_hash(root / "pages"))
    return artifacts, identity


async def test_real_local_srt_and_mp4_downloads_have_bound_owned_facts(browser_cache, demo_mp4, tmp_path):  # noqa: F811
    pages, output = tmp_path / "pages", tmp_path / "images"
    pages.mkdir()
    (pages / "index.html").write_text(
        '<button id="confirm" onclick="document.querySelectorAll(\'a\').forEach(a=>a.hidden=false)">Confirm</button>' +
        link(SRT.encode(), "application/x-subrip", "MUYI_EP01_fr_v2_DEMO.srt", "sub") +
        link(demo_mp4, "video/mp4", "MUYI_EP01_fr_v2_DEMO.mp4", "media"),
        encoding="utf-8")
    actions = [BrowserAction(route="/index.html", action="click", selector=selector)
               for selector in ("#confirm", "#sub", "#media")]
    rows = await inspect_prototype(pages, output, actions)
    assert len(rows) == 2 and all(row["actions"] == [a.model_dump() for a in actions] for row in rows)
    for row in rows:
        capture = row["download_capture"]
        assert capture["status"] == "observed"
        assert [event["window"]["action_index"] for event in capture["events"]] == [1, 2]
        assert capture["events"][0]["facts"]["cue_count"] == 1
        assert capture["events"][1]["facts"]["decoded_frame"] is True
        assert capture["events"][1]["facts"]["width"] == 32
    artifacts, identity = archive(rows, tmp_path, output, actions)
    assert valid_downloads(rows, artifacts, identity, [a.model_dump() for a in actions], 13)
    for row in rows:
        sub, video = row["download_capture"]["events"]
        assert artifacts.bytes(sub["attachments"]["raw"]) == SRT.encode()
        assert artifacts.read(sub["attachments"]["text"]) == SRT
        assert artifacts.bytes(video["attachments"]["raw"]) == demo_mp4
        assert "text" not in video["attachments"]  # Opaque bytes are not mandatory model text.
        assert "storage_name" not in sub
        assert artifacts.json(video["attachments"]["facts"])["binding"]["source_hash"] == identity["hash"]
    report = artifacts.put({"results": rows}, "prototype-browser")
    with artifacts.store.connect() as db:
        edges = {row[0] for row in db.execute("SELECT child FROM artifact_edges WHERE parent=?", (report["hash"],))}
    assert rows[0]["download_capture"]["events"][0]["attachments"]["raw"]["hash"] in edges
    # The actual normal compiled payload exposes the owned closure, not a
    # private temporary log or an obligation to read opaque MP4 as text.
    from auto_agents_2.context_plan import ContextPlanner
    from auto_agents_2.diagnostics import references
    from auto_agents_2.prompts import PromptCompiler
    from auto_agents_2.prototype_contract import prototype_contract
    prepared = ContextPlanner(artifacts).prepare("review", {
        "review_kind": "prototype", "browser_evidence": rows,
        "controller_prototype_contract": prototype_contract(),
        "controller_prototype_download_capture": download_capture_profile()}, tmp_path)
    prompt, _ = PromptCompiler().compile("review", prepared.payload)
    visible = {ref["hash"] for ref in references(prepared.payload)}
    expected = {ref["hash"] for row in rows for event in row["download_capture"]["events"]
                for ref in event["attachments"].values()}
    assert expected <= visible
    assert "opaque media bytes are not required LLM text reads" in prompt


async def test_no_event_is_explicit_and_legacy_is_not_relabelled(browser_cache, tmp_path):  # noqa: F811
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "index.html").write_text('<button id="x">No download</button>')
    actions = [BrowserAction(route="/index.html", action="click", selector="#x")]
    rows = await inspect_prototype(pages, tmp_path / "images", actions)
    assert all(row["download_capture"]["status"] == "none" and
               row["download_capture"]["events"] == [] for row in rows)
    artifacts, identity = archive(rows, tmp_path, tmp_path / "images", actions)
    assert valid_downloads(rows, artifacts, identity, [a.model_dump() for a in actions], 13)
    legacy = [{"route": "/index.html", "actions": []}]
    archive_downloads(legacy, artifacts, tmp_path / "absent", source_hash="old", actions_hash=digest([]),
                      browser_version=13, source_tree="old")
    assert "download_capture" not in legacy[0]


@pytest.mark.parametrize("raw,valid", [
    (SRT.encode(), True),
    (b"\xef\xbb\xbf" + SRT.encode(), True),
    (b"1\n00:00:00,000 --> 00:00:01,000\n", False),
    (b"1\n00:00:00,000 --> 00:00:00,000\nEmpty interval\n", False),
    (b"1\n00:99:00,000 --> 00:00:01,000\nWrong time\n", False),
    (b"1\n00:00:00,000 --> 00:00:01,000\n\xff\n", False),
])
def test_complete_utf8_srt_structure(raw, valid):
    facts, text = srt_facts(raw)
    assert (facts["status"] == "valid") is valid
    if valid:
        assert text == SRT


async def test_mp4_header_alone_is_not_decoding_and_candidate_cannot_forge_it(browser_cache, demo_mp4):  # noqa: F811
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            candidate = await browser.new_page()
            await candidate.evaluate("""() => {
                window.HTMLMediaElement = function(){};
                window.Uint8Array = {from:() => {throw Error('candidate API used')}};
                window.atob = () => {throw Error('candidate API used')};
            }""")
            assert (await mp4_facts(browser, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64))["status"] != "valid"
            facts = await mp4_facts(browser, demo_mp4)
            assert facts["status"] == "valid" and facts["decoded_frame"]
        finally:
            await browser.close()


class FakeDownload:
    def __init__(self, page, raw=b"data", name="../../outside.srt", mode="complete"):
        self.page, self.raw, self.suggested_filename, self.mode = page, raw, name, mode
        self.url = "blob:http://127.0.0.1/fixture"
        self.cancelled = False
        self.destination = None

    async def save_as(self, path):
        self.destination = Path(path)
        if self.mode == "hang":
            await asyncio.Event().wait()
        if self.mode in {"failed", "cancelled"}:
            raise RuntimeError("cancelled fixture transfer")
        self.destination.write_bytes(self.raw)

    async def failure(self):
        return "canceled" if self.mode == "cancelled" else None

    async def cancel(self):
        self.cancelled = True

    async def delete(self):
        pass


class FakeContext:
    def __init__(self):
        self.closed = False

    def on(self, *args):
        pass

    def remove_listener(self, *args):
        pass

    async def close(self):
        self.closed = True


def observer(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    row = {"route": "/", "viewport": 390, "actual_browser_version": "fixture"}
    context = FakeContext()
    capture = PassiveDownloads(None, context, tmp_path, spool, row, digest([]), "tree", DownloadBudget())
    page = SimpleNamespace(remove_listener=lambda *args: None)
    capture.pages.append(page)
    return capture, page, row


async def test_dangerous_filename_never_becomes_a_path_and_failed_transfers_stay_failed(tmp_path):
    capture, page, row = observer(tmp_path)
    good = FakeDownload(page, SRT.encode(), "../../../../outside.srt")
    failed = FakeDownload(page, mode="failed")
    capture.observe(good)
    capture.observe(failed)
    capture.observe(FakeDownload(page, mode="cancelled"))
    await capture.finish()
    assert good.destination.parent == tmp_path / "downloads"
    assert good.destination.name.startswith("download_") and good.destination.suffix == ".bin"
    assert row["download_capture"]["events"][0]["suggested_filename"] == "../../../../outside.srt"
    assert row["download_capture"]["events"][1]["status"] == "failed"
    assert row["download_capture"]["events"][2]["status"] == "cancelled"
    assert row["download_capture"]["status"] == "incomplete"
    assert not (tmp_path / "outside.srt").exists()


async def test_timeout_byte_and_event_limits_retain_failure_not_fake_files(tmp_path, monkeypatch):
    import auto_agents_2.prototype_downloads as module
    capture, page, row = observer(tmp_path)
    monkeypatch.setattr(module, "DOWNLOAD_SECONDS", .05)
    hanging = FakeDownload(page, mode="hang")
    capture.observe(hanging)
    await capture.finish()
    assert hanging.cancelled
    assert row["download_capture"]["events"][0]["status"] == "incomplete"
    assert "file_hash" not in row["download_capture"]["events"][0]
    other = tmp_path / "other"
    other.mkdir()
    capture, page, row = observer(other)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 3)
    capture.observe(FakeDownload(page, raw=b"four", name="big.bin"))
    await capture.finish()
    assert row["download_capture"]["events"][0]["status"] == "limit_exceeded"
    assert "file_hash" not in row["download_capture"]["events"][0]
    last = tmp_path / "last"
    last.mkdir()
    capture, page, row = observer(last)
    monkeypatch.setattr(module, "MAX_ROW_FILES", 1)
    capture.observe(FakeDownload(page, raw=b"x", name="small.bin"))
    capture.observe(FakeDownload(page, raw=b"x", name="extra.bin"))
    await capture.finish()
    assert capture.context.closed and row["download_capture"]["overflow"]
    assert len(row["download_capture"]["events"]) == 1


@pytest.mark.parametrize("mutation", ["source", "action", "profile", "raw", "text", "facts", "absent", "partial"])
async def test_missing_or_changed_download_closure_never_passes(tmp_path, monkeypatch, mutation):
    workflow, rid, document, _, _, _ = approval_fixture(tmp_path, monkeypatch, legacy=False)
    actions = document.model_dump()["browser_actions"]
    visuals = workflow.store.get(rid, "prototype_approval")["visuals"]
    output = tmp_path / "capture"
    (output / "downloads").mkdir(parents=True)
    from auto_agents_2.util import identifier
    name = identifier("download") + ".bin"
    (output / "downloads" / name).write_bytes(SRT.encode())
    facts, text = srt_facts(SRT.encode())
    for row in visuals:
        row["actual_browser_version"] = "fixture"
        row["download_capture"] = {
            "profile_hash": download_capture_profile()["hash"], "prototype_tree_hash": "tree",
            "actions_hash": digest(actions), "status": "observed", "overflow": False,
            "events": [{"index": 0, "window": {"phase": "action", "action_index": 0, "action": actions[0]},
                        "suggested_filename": "demo.srt", "filename_hash": digest("demo.srt"),
                        "status": "complete", "storage_name": name, "bytes": len(SRT.encode()),
                        "file_hash": digest(SRT.encode()), "facts": facts, "utf8_text": text}]}
    from auto_agents_2.approvals import prototype_identity
    binding = prototype_identity(tmp_path, actions)
    archive_downloads(visuals, workflow.artifacts, output, source_hash=binding["hash"],
                      actions_hash=binding["actions_hash"], browser_version=13, source_tree="tree")
    report = {"version": 13, "source_hash": binding["hash"], "actions_hash": binding["actions_hash"],
              "error": "", "results": visuals}
    assert workflow.approvals.browser_report_current(report, binding, actions, 13,
                                                      expected_routes=["/index.html"])
    if mutation in {"source", "action", "profile"}:
        key = {"source": "source_hash", "action": "actions_hash", "profile": "profile_hash"}[mutation]
        visuals[0]["download_capture"]["binding"][key] = "wrong"
    elif mutation in {"raw", "text", "facts", "absent"}:
        attachments = visuals[0]["download_capture"]["events"][0]["attachments"]
        if mutation == "absent":
            del attachments["raw"]
        else:
            ref = attachments[mutation]
            (workflow.artifacts.root / ref["hash"][:2] / ref["hash"]).unlink()
    elif mutation == "partial":
        del visuals[1]["download_capture"]
    for row in visuals:
        if "download_capture" in row:
            capture = row["download_capture"]
            capture["hash"] = digest({key: value for key, value in capture.items() if key != "hash"})
    assert not workflow.approvals.browser_report_current(report, binding, actions, 13,
                                                          expected_routes=["/index.html"])
    # A legacy record has no download assertion. It is not magically upgraded.
    legacy = copy.deepcopy(report)
    for row in legacy["results"]:
        row.pop("download_capture", None)
    assert workflow.approvals.browser_report_current(legacy, binding, actions, 13,
                                                      expected_routes=["/index.html"])


async def test_engine_archives_fresh_profile_only_and_keeps_review_binding(tmp_path, monkeypatch):
    from auto_agents_2.prototype_contract import source_context
    workflow, rid, document, _, _, _ = approval_fixture(tmp_path, monkeypatch, legacy=False)
    original = workflow.store.get(rid, "prototype_approval")
    before = copy.deepcopy(original)
    report = await workflow._prototype_browser_report(rid, tmp_path, document)
    assert all("download_capture" not in row for row in report["results"])  # Stub/old evidence is not relabelled.
    assert workflow.store.get(rid, "prototype_approval") == before
    assert workflow.approvals.current(rid, tmp_path, document.model_dump()["browser_actions"], 13)
    assert source_context(workflow.store, workflow.artifacts, rid)["controller_prototype_download_capture"] == download_capture_profile()


@pytest.mark.parametrize("interrupt", [None, "after_answer", "after_report"])
async def test_normal_retry_of_old_controller_gap_reobserves_once_and_keeps_rejection(
        tmp_path, monkeypatch, interrupt):
    from prototype_review_fixtures import prototype_verdict
    from test_prototype_recovery import prototype_fixture

    from auto_agents_2.engine import Replan
    from auto_agents_2.faults import HumanIntervention
    from auto_agents_2.models import IntakeResult
    from auto_agents_2.prototype_review import PrototypeFinding

    workflow, rid, _, _, calls = prototype_fixture(tmp_path, monkeypatch)
    spec = IntakeResult(summary="Demo files", document="Download MP4 and UTF-8 SRT before approval", frontend=True)
    workflow.store.checkpoint(rid, "clarify", spec.model_dump())
    workflow.store.checkpoint(rid, "capture", {"artifact": workflow.artifacts.put(spec.document, "requirements")})
    workflow.store.checkpoint(rid, "rounds/T1", {"round": 9, "stalled": 4})
    invoke = workflow.agent.invoke
    reviews = []
    modern = False

    async def reviewer(stage, context, *args, **kwargs):
        if stage != "review":
            return await invoke(stage, context, *args, **kwargs)
        reviews.append(copy.deepcopy(context))
        if not modern:
            finding = PrototypeFinding(severity="major", location="download button", description="Missing file collector",
                evidence="The old report has no bytes", resolution="Supply controller-owned file evidence",
                criterion_id="specification:document", kind="evidence_gap", observation="Download bytes are uncollected",
                evidence_refs=["browser", "requirements"], owner="controller", requested_capability="unavailable",
                source_quote=spec.document)
        else:
            assert context["controller_prototype_download_reobservation"]["previous"]["required_read"] is True
            assert all(row["download_capture"]["profile_hash"] == download_capture_profile()["hash"]
                       for row in context["browser_evidence"])
            # New evidence enables review; it must never manufacture acceptance.
            finding = PrototypeFinding(severity="major", location="download button", description="No demo file produced",
                evidence="Controller observed no download event", resolution="Fix the prototype download",
                criterion_id="specification:document", kind="defect", observation="No MP4/SRT download was observed",
                evidence_refs=["browser"], owner="prototype", requested_capability="none")
        return prototype_verdict(context, approved=False, findings=[finding])

    workflow.agent.invoke = reviewer

    async def paused(question):
        raise asyncio.CancelledError()

    workflow.question_handler = paused
    with pytest.raises(HumanIntervention) as rejected:
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, spec.document)
    with pytest.raises(asyncio.CancelledError):
        await workflow._wait_prerequisite(rid, rejected.value)
    question = workflow.store.pending(rid)[0]
    assert question["key"].startswith("prerequisite/")
    old_pending = workflow.store.get(rid, "prototype/pending")
    old_receipt = workflow.store.get(rid, "invocation/prototype-review")
    before_source = tree_hash(tmp_path)
    workflow.store.answer(question["id"], "y", question["artifact_hash"])
    modern = True
    old_browser = __import__("auto_agents_2.engine", fromlist=["inspect_prototype"]).inspect_prototype

    async def browser(*args):
        rows = await old_browser(*args)
        for row in rows:
            row["actual_browser_version"] = "synthetic-browser"
            row["download_capture"] = {
                "profile_hash": download_capture_profile()["hash"], "prototype_tree_hash": tree_hash(args[0]),
                "actions_hash": digest([a.model_dump() for a in args[2]]),
                "status": "none", "overflow": False, "events": []}
        return rows

    monkeypatch.setattr("auto_agents_2.engine.inspect_prototype", browser)
    checkpoints, checkpoint = workflow.store.checkpoints, workflow.store.checkpoint
    stopped = []

    def after_answer(run, data, *args, **kwargs):
        checkpoints(run, data, *args, **kwargs)
        if interrupt == "after_answer" and not stopped and question["key"].rsplit("/", 1)[0] in data:
            stopped.append(interrupt)
            raise asyncio.CancelledError()

    def after_report(run, key, data, *args, **kwargs):
        checkpoint(run, key, data, *args, **kwargs)
        if interrupt == "after_report" and not stopped and key == "prototype_browser_report":
            stopped.append(interrupt)
            raise asyncio.CancelledError()

    monkeypatch.setattr(workflow.store, "checkpoints", after_answer)
    monkeypatch.setattr(workflow.store, "checkpoint", after_report)
    if interrupt:
        with pytest.raises(asyncio.CancelledError):
            await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, spec.document)
    with pytest.raises(Replan, match="No MP4/SRT"):
        await workflow._prepare_prototype(rid, tmp_path, spec, spec.model_dump(), {}, spec.document)
    assert len(reviews) == 2 and calls.count("browser") == 2  # One legacy and one actual new capture.
    assert reviews[0]["prototype_review_basis"]["hash"] != reviews[1]["prototype_review_basis"]["hash"]
    assert reviews[0]["prototype_review_basis"]["protocol_hash"] == reviews[1]["prototype_review_basis"]["protocol_hash"]
    from auto_agents_2.context_plan import ContextPlanner
    from auto_agents_2.prompts import PromptCompiler
    for index, context in enumerate(reviews):
        prepared = ContextPlanner(workflow.artifacts).prepare("review", context, tmp_path, run_id=rid)
        prompt, _ = PromptCompiler().compile("review", prepared.payload)
        payload = json.loads(prompt.partition("<context>\n")[2].rpartition("\n</context>")[0])
        extension = payload["controller_prototype_download_capture"]
        assert extension["hash"] == download_capture_profile()["hash"]
        assert extension["base_exclusion_override"]["scope"] == "Only actual rows carrying this profile_hash; never legacy rows"
        assert "not_collected.download_bytes; for missing rows it does not" in prompt
        assert "download_bytes" in payload["controller_prototype_contract"]["not_collected"]
        observed = payload["browser_evidence"]
        if isinstance(observed, dict) and "artifact" in observed:
            observed = workflow.artifacts.json(observed["artifact"])
        assert all(("download_capture" in row) is bool(index) for row in observed)
    fresh = workflow.store.get(rid, "prototype/pending")
    prior = workflow.artifacts.json(fresh["download_capture_refresh"]["previous"])
    assert prior["pending"] == old_pending
    assert prior["invocation"] == old_receipt and not prior["invocation"]["result"]["approved"]
    assert workflow.store.question(question["id"])["answer"] == "y"
    assert workflow.store.get(rid, question["key"].rsplit("/", 1)[0]) == 1
    assert workflow.store.get(rid, "rounds/T1") == {"round": 9, "stalled": 4}
    assert workflow.store.get(rid, "prototype_approval") is None
    assert tree_hash(tmp_path) == before_source
