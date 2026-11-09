import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_shared_cache import fake_installer

from auto_agents_2.browser_cache import BrowserCache, driver_identity
from auto_agents_2.browser_sources import (
    HOST_KEYS,
    OFFICIAL_BASES,
    download_configuration,
    equivalent_plans,
    official_plan,
)
from auto_agents_2.shared_cache import read_json
from auto_agents_2.util import digest, write_json


def client(root, shared, variables=None, *, version="1.55.1", value="original", urls=True):
    manager, environment, work, calls, command = fake_installer(root, shared, version=version)
    package = work / "node_modules/playwright-core"
    node, cli = environment.prefix / "bin/node", work / "node_modules/playwright/cli.js"
    browser = BrowserCache(manager)
    consumer = manager.root / "browser-bindings/controller.json"
    variables = dict(variables or {})

    async def with_sources(argv, workspace, env, *args, **kwargs):
        output = await command(argv, workspace, env, *args, **kwargs)
        if "--dry-run" in argv and urls:
            def setting(name):
                return next((env[key] for key in (name, "npm_config_" + name.lower(),
                             "npm_package_config_" + name.lower()) if key in env), None)
            host = setting("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST") or setting("PLAYWRIGHT_DOWNLOAD_HOST")
            sources = [host] if host else [OFFICIAL_BASES[0], OFFICIAL_BASES[2]]
            labels = ["url", *(f"fallback {i}" for i in range(1, len(sources)))]
            output += "".join(f"  Download {label}: {base}/builds/chromium/123/chromium-linux.zip\n"
                              for label, base in zip(labels, sources))
        elif "install" in argv and "--dry-run" not in argv and value != "original":
            (Path(env["PLAYWRIGHT_BROWSERS_PATH"]) / "chromium-123/chrome").write_text(value)
        return output

    manager._command = with_sources

    async def prepare():
        return await browser.acquire(work, node, package, cli, ["chromium"], variables, consumer,
                                     runtime=environment.prefix)
    return browser, prepare, calls, consumer, package


def installs(calls):
    return sum("install" in argv and "--dry-run" not in argv for argv, *_ in calls)


def legacy_metadata(browser, record):
    manifest = Path(record["path"]).parent / "manifest.json"
    body = read_json(manifest)
    body.pop("provenance", None)
    manifest.chmod(0o644)
    manifest.write_text(json.dumps(body))
    manifest.chmod(0o444)
    write_json(browser.cache.root / "index" / f"browser-{record['spec_key']}.json",
               {"object_id": record["object_id"], "touched": body["created"]})
    return manifest.read_bytes()


@pytest.mark.parametrize("legacy", [False, True])
async def test_official_default_and_exact_mirror_reuse_without_download_or_copy(tmp_path, monkeypatch, legacy):
    first = client(tmp_path / "one", tmp_path / "shared")
    old = await first[1]()
    before = legacy_metadata(first[0], old) if legacy else Path(old["path"]).parent.joinpath("manifest.json").read_bytes()
    second = client(tmp_path / "two", tmp_path / "shared", {HOST_KEYS[0]: OFFICIAL_BASES[2]})
    from auto_agents_2 import shared_cache
    monkeypatch.setattr(shared_cache.shutil, "copytree", lambda *a, **k: pytest.fail("reused payload must not be copied"))
    current = await second[1]()
    assert installs(first[2]) == 1 and installs(second[2]) == int(legacy)
    assert current["object_id"] == old["object_id"] and current["path"] == old["path"]
    assert current["reuse"] == ("verified-content-v1" if legacy else "official-download-plan-v1")
    assert current["spec"] == old["spec"]
    assert current["requested_spec_key"] != old["spec_key"]
    assert second[0].cache.matches({**old["spec"], "download_configuration_hash": digest({HOST_KEYS[0]: OFFICIAL_BASES[2]})}, current)
    assert Path(old["path"]).parent.joinpath("manifest.json").read_bytes() == before
    assert len(list((tmp_path / "shared/objects").iterdir())) == 1
    assert not list((tmp_path / "shared/staging").iterdir())
    count = len(second[2])
    assert (await second[1]())["object_id"] == old["object_id"]
    assert len(second[2]) == count  # The proved source alias is a normal exact hit thereafter.


async def test_unchanged_legacy_spec_remains_an_exact_hit_without_installer(tmp_path):
    one = client(tmp_path / "one", tmp_path / "shared")
    old = await one[1]()
    original = legacy_metadata(one[0], old)
    two = client(tmp_path / "two", tmp_path / "shared")
    current = await two[1]()
    assert current["object_id"] == old["object_id"] and two[2] == []
    assert Path(old["path"]).parent.joinpath("manifest.json").read_bytes() == original
    assert current["reuse"] is None


async def test_legacy_explicit_source_requires_independent_installation_before_storage_reuse(tmp_path):
    one = client(tmp_path / "one", tmp_path / "shared", {HOST_KEYS[0]: OFFICIAL_BASES[0]})
    old = await one[1]()
    legacy_metadata(one[0], old)
    two = client(tmp_path / "two", tmp_path / "shared")
    assert (await two[1]())["object_id"] == old["object_id"]
    assert installs(two[2]) == 1


async def test_custom_source_requires_install_then_equal_full_content_reuses_storage(tmp_path, monkeypatch):
    first = client(tmp_path / "one", tmp_path / "shared")
    old = await first[1]()
    second = client(tmp_path / "two", tmp_path / "shared", {HOST_KEYS[0]: "https://mirror.invalid"})
    from auto_agents_2 import shared_cache
    monkeypatch.setattr(shared_cache.shutil, "copytree", lambda *a, **k: pytest.fail("equal content must not be copied"))
    current = await second[1]()
    assert installs(second[2]) == 1 and current["object_id"] == old["object_id"]
    assert current["reuse"] == "verified-content-v1" and current["downloaded"]
    alias = read_json(second[0].cache.root / "index" / f"browser-{current['requested_spec_key']}.json")
    assert alias["spec"]["download_configuration_hash"] == digest({HOST_KEYS[0]: "https://mirror.invalid"})
    assert alias["content_hash"] == old["content_hash"] and alias["provenance"]["official_plan"] is None
    assert len(list((tmp_path / "shared/objects").iterdir())) == 1
    count = len(second[2])
    assert (await second[1]())["object_id"] == old["object_id"] and len(second[2]) == count


async def test_independent_official_installation_witness_can_reuse_legacy_storage_for_another_source(tmp_path):
    one = client(tmp_path / "one", tmp_path / "shared", {HOST_KEYS[0]: "https://mirror.invalid"})
    old = await one[1]()
    legacy_metadata(one[0], old)
    two = client(tmp_path / "two", tmp_path / "shared")
    observed = await two[1]()
    assert observed["reuse"] == "verified-content-v1" and installs(two[2]) == 1
    three = client(tmp_path / "three", tmp_path / "shared", {HOST_KEYS[0]: OFFICIAL_BASES[2]})
    current = await three[1]()
    assert current["object_id"] == old["object_id"] and installs(three[2]) == 0
    assert current["reuse"] == "official-download-plan-v1"
    assert len(list((tmp_path / "shared/objects").iterdir())) == 1


async def test_legacy_immutable_source_gap_cannot_be_repaired_by_a_mutable_index_plan(tmp_path):
    one = client(tmp_path / "one", tmp_path / "shared")
    old = await one[1]()
    index = one[0].cache.root / "index" / f"browser-{old['spec_key']}.json"
    copied = read_json(index)
    legacy_metadata(one[0], old)
    write_json(index, copied)  # An otherwise valid copied plan cannot upgrade legacy provenance.
    two = client(tmp_path / "two", tmp_path / "shared", {HOST_KEYS[0]: OFFICIAL_BASES[2]})
    current = await two[1]()
    assert installs(two[2]) == 1 and current["reuse"] == "verified-content-v1"
    assert current["object_id"] == old["object_id"]


async def test_custom_source_with_different_bytes_is_not_merged(tmp_path):
    one = client(tmp_path / "one", tmp_path / "shared")
    old = await one[1]()
    two = client(tmp_path / "two", tmp_path / "shared", {HOST_KEYS[0]: "https://mirror.invalid"}, value="different")
    current = await two[1]()
    assert installs(two[2]) == 1 and current["object_id"] != old["object_id"]
    assert len(list((tmp_path / "shared/objects").iterdir())) == 2
    assert Path(old["path"], "chromium-123/chrome").read_text() == "original"


@pytest.mark.parametrize("field", ["driver_version", "driver_hash", "browsers", "families", "platform", "architecture",
                                   "libc", "os_release", "host_platform_override"])
async def test_equal_bytes_do_not_merge_different_payload_identity(tmp_path, field):
    fixture = client(tmp_path / "project", tmp_path / "shared")
    record = await fixture[1]()
    spec = {**record["spec"], field: "different", "download_configuration_hash": "d" * 64}
    source = Path(record["path"])
    different = fixture[0].cache.publish(spec, source, fixture[3], None)
    assert different["object_id"] != record["object_id"]
    assert len(list((tmp_path / "shared/objects").iterdir())) == 2


async def test_concurrent_official_source_variants_download_once(tmp_path):
    one = client(tmp_path / "one", tmp_path / "shared")
    two = client(tmp_path / "two", tmp_path / "shared", {HOST_KEYS[0]: OFFICIAL_BASES[2]})
    a, b = await asyncio.gather(one[1](), two[1]())
    assert a["object_id"] == b["object_id"] and installs(one[2]) + installs(two[2]) == 1
    assert len(list((tmp_path / "shared/objects").iterdir())) == 1


async def test_corrupt_equivalent_object_is_retained_and_not_reused(tmp_path):
    one = client(tmp_path / "one", tmp_path / "shared")
    old = await one[1]()
    binary = Path(old["path"], "chromium-123/chrome")
    binary.chmod(0o755)
    binary.write_text("corrupt")
    two = client(tmp_path / "two", tmp_path / "shared", {HOST_KEYS[0]: OFFICIAL_BASES[2]})
    current = await two[1]()
    assert installs(two[2]) == 1 and current["object_id"] != old["object_id"]
    assert binary.read_text() == "corrupt" and len(list((tmp_path / "shared/objects").iterdir())) == 2


async def test_unknown_dry_run_format_does_not_prove_pre_download_equivalence(tmp_path):
    one = client(tmp_path / "one", tmp_path / "shared", urls=False)
    old = await one[1]()
    two = client(tmp_path / "two", tmp_path / "shared", {HOST_KEYS[0]: OFFICIAL_BASES[2]}, urls=False)
    current = await two[1]()
    assert installs(two[2]) == 1 and current["reuse"] == "verified-content-v1"
    assert current["object_id"] == old["object_id"]


async def test_alias_needs_source_content_and_identity_proof_and_keeps_controller_binding(tmp_path, monkeypatch):
    one = client(tmp_path / "one", tmp_path / "shared")
    old = await one[1]()
    two = client(tmp_path / "two", tmp_path / "shared", {HOST_KEYS[0]: OFFICIAL_BASES[2]})
    record = await two[1]()
    spec = {**old["spec"], "download_configuration_hash": digest({HOST_KEYS[0]: OFFICIAL_BASES[2]})}
    write_json(two[3], {key: record[key] for key in ("object_id", "path", "content_hash", "spec_key", "spec")})
    from auto_agents_2 import browser_cache
    monkeypatch.setattr(browser_cache, "controller_driver", lambda: (two[4].parent / "node", two[4], two[4] / "cli.js"))
    monkeypatch.setattr(browser_cache, "driver_identity", lambda *a: spec)
    alias = two[0].cache.root / "index" / f"browser-{digest(spec)}.json"
    before = alias.read_bytes()
    assert two[0].current_identity()["available"]
    assert alias.read_bytes() == before  # Read-only identity never creates a reference or changes the proof.
    proof = read_json(alias)
    cases = [{**proof, field: value} for field, value in (
        ("content_hash", "0" * 64), ("reuse", None), ("spec", old["spec"]),
        ("provenance", None), ("provenance", ["malformed"]), ("proof", ["malformed"]))]
    cases += [[], {"object_id": old["object_id"]},
              {**proof, "provenance": {**proof["provenance"], "download_configuration_hash": "f" * 64}},
              {**proof, "proof": {**proof["proof"], "installation": {
                  **proof["proof"]["installation"], "files_hash": "f" * 64}}}]
    for invalid in cases:
        write_json(alias, invalid)
        assert not two[0].current_identity()["available"]
        assert two[0].cache.lookup(spec, two[3], None) is None
    # Copying an existing proof and changing only the requested source/spec does
    # not turn a never-installed custom source into a cache hit.
    custom = {**spec, "download_configuration_hash": digest({HOST_KEYS[0]: "https://unobserved.invalid"})}
    write_json(two[0].cache.root / "index" / f"browser-{digest(custom)}.json", {**proof, "spec": custom})
    assert not two[0].cache.matches(custom, record)
    assert two[0].cache.lookup(custom, two[3], None) is None
    # A corrupt alias is skipped safely during actual source comparison.
    write_json(alias, {**proof, "provenance": ["malformed"]})
    assert (await two[1]())["object_id"] == old["object_id"]
    assert installs(two[2]) == 0


def test_source_configuration_preserves_legacy_hash_and_accounts_for_npm_precedence(tmp_path):
    fixture = client(tmp_path / "project", tmp_path / "shared")
    package = fixture[4]
    primary, npm, packaged = HOST_KEYS[0], "npm_config_playwright_download_host", "npm_package_config_playwright_download_host"
    assert download_configuration({primary: "https://mirror.invalid"}) == {primary: "https://mirror.invalid"}
    assert driver_identity(package, ["chromium"], {})["download_configuration_hash"] == digest({})
    a = driver_identity(package, ["chromium"], {npm: "https://one.invalid", packaged: "https://two.invalid"})
    b = driver_identity(package, ["chromium"], {packaged: "https://two.invalid"})
    assert a["download_configuration_hash"] != b["download_configuration_hash"] != digest({})
    assert download_configuration({primary: "", npm: "https://one.invalid"}) == {primary: ""}
    assert driver_identity(package, ["chromium"], {"PLAYWRIGHT_HOST_PLATFORM_OVERRIDE": "fixture"}) != driver_identity(package, ["chromium"], {})


def test_official_equivalence_requires_every_exact_artifact_url_not_just_a_domain(tmp_path):
    def output(components):
        return "".join(f"Install location: {tmp_path}/{name}\n" + "".join(
            f" Download {'url' if i == 0 else 'fallback ' + str(i)}: {url}\n" for i, url in enumerate(urls))
            for name, urls in components)
    a = OFFICIAL_BASES[2] + "/builds/cft/153/chrome.zip"
    b = OFFICIAL_BASES[0] + "/builds/cft/153/chrome.zip"
    base = official_plan(output([("chromium-123", [a])]), tmp_path)
    changed = official_plan(output([("chromium-123", [b])]), tmp_path)
    assert not equivalent_plans(base, changed)  # An official hostname alone proves no mirror route.
    assert official_plan(output([("chromium-123", [a + "?token=private"])]), tmp_path) is None
    assert official_plan(output([("chromium-123", ["https://cdn.playwright.dev.evil.invalid/builds/cft/153/chrome.zip"])]), tmp_path) is None
    assert official_plan(output([("chromium-123", [a]), ("ffmpeg-1", ["https://mirror.invalid/file"])]), tmp_path) is None
    assert not equivalent_plans(base, [base[0], ["ffmpeg-1", "builds/ffmpeg/1.zip", [OFFICIAL_BASES[2] + "/builds/ffmpeg/1.zip"]]])


def test_real_process_source_variants_serialize_publication(tmp_path):
    script = """import asyncio,json,sys
from pathlib import Path
from test_browser_cache_sources import client,installs
from auto_agents_2.browser_sources import HOST_KEYS,OFFICIAL_BASES
b,prepare,calls,_,_=client(Path(sys.argv[1]),Path(sys.argv[2]),{HOST_KEYS[0]:OFFICIAL_BASES[2]} if sys.argv[3]=='mirror' else {})
r=asyncio.run(prepare())
print(json.dumps({'calls':installs(calls),'object':r['object_id']}))
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[1] / "src"),
                                           str(Path(__file__).parent)]), "PYTHONDONTWRITEBYTECODE": "1"}
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path / name), str(tmp_path / "shared"), name],
                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ("default", "mirror")]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert sorted(result["calls"] for result in results) == [0, 1]
    assert results[0]["object"] == results[1]["object"]
