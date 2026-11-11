import copy
import json

import pytest

from auto_agents_2.context import ContextBuilder
from auto_agents_2.prompts import PromptCompiler


def compiled_context(prompt):
    encoded = prompt.split("<context>\n", 1)[1].split("\n</context>", 1)[0]
    return json.loads(encoded)


@pytest.mark.parametrize(("model", "family"), [
    ("gpt-fixture", "openai"),
    ("gemini-fixture", "gemini"),
    ("claude-fixture", "claude"),
])
def test_workflow_use_request_preserves_full_product_input(model, family):
    raw = ("Use aa2's full delivery workflow to produce the target prototype and stop for human review.\n"
           "TARGET_START\nKeep every product requirement and acceptance obligation.\nTARGET_END")
    context = {
        "raw_requirements": raw,
        "raw_requirements_truncated": False,
        "project": {
            "files": [{"path": "requirements.md", "content": raw}],
            "read_on_demand": [],
            "discovery": "Complete snapshot: do not list directories, run commands.",
        },
        "controller_authority_scope": {"owner": "clarify", "meaning": "Only this stage's proposal."},
    }
    before = copy.deepcopy(context)
    prompt, actual_family = PromptCompiler().compile("clarify", context, model)
    delivered = compiled_context(prompt)

    assert actual_family == family
    assert delivered["raw_requirements"] == raw
    assert delivered["project"] == before["project"]
    assert delivered["controller_authority_scope"] == before["controller_authority_scope"]
    assert context == before
    assert "aa2 is the controller of this workflow" in prompt
    assert "not by locating, importing, invoking, or inspecting an installed aa2 package" in prompt
    assert 'stage="clarify"' in prompt
    assert "Respect required human prototype approval" in prompt


@pytest.mark.parametrize("stage", ["clarify", "plan", "implement"])
def test_worker_returns_current_stage_and_keeps_controller_execution(stage):
    context = {"mandatory": ["Keep the entire delivery workflow and human approval obligation."]}
    prompt, _ = PromptCompiler().compile(stage, context, "gemini-fixture")

    assert compiled_context(prompt)["mandatory"] == context["mandatory"]
    assert f'<task stage="{stage}">' in prompt
    assert "Return only the current stage's structured proposal" in prompt
    assert "Commands requested in the schema are proposals, not native-tool instructions" in prompt
    assert "aa2 runs\nthe required checks independently" in prompt


def test_aa2_as_target_keeps_supplied_source_in_scope():
    context = {
        "raw_requirements": "Improve the aa2 target source supplied in this snapshot.",
        "project": {"files": [{"path": "src/auto_agents_2/example.py",
                              "content": "TARGET_SOURCE = 'fully supplied'\n"}]},
    }
    prompt, _ = PromptCompiler().compile("clarify", context, "gpt-fixture")

    assert compiled_context(prompt)["project"] == context["project"]
    assert "If the target project itself is aa2, its supplied source remains valid target material" in prompt
    assert "supplied project snapshot" in prompt


def test_incomplete_snapshot_keeps_bounded_native_material_inspection(tmp_path):
    (tmp_path / "large.py").write_text("x" * 100001)
    project = ContextBuilder().select(tmp_path, paths=["large.py"])
    assert project["read_on_demand"] and project["discovery"].startswith("Search the mounted public workspace")

    prompt, _ = PromptCompiler().compile("clarify", {"project": project}, "gpt-fixture")

    assert compiled_context(prompt)["project"] == project
    assert "read-only shell and image tools may be used when needed for that inspection" in prompt
    assert "Project discovery follows project.discovery" in prompt


@pytest.mark.parametrize("purpose", ["material_probe", "provider_probe"])
def test_worker_scope_does_not_replace_diagnostic_read_contract(purpose):
    context = {"input_contract": {"purpose": purpose}, "context_files": ["/fixture/material.txt"]}
    stage = "material_probe" if purpose == "material_probe" else "review"
    prompt, _ = PromptCompiler().compile(stage, context, "gpt-fixture")

    assert "Controller/target boundary" not in prompt
    if purpose == "material_probe":
        assert "Read only the complete text files explicitly listed in context_files" in prompt
        assert "image_token must be an empty string" in prompt
    else:
        assert "Validate only the native structured-response contract" in prompt
        assert "Do not execute tools" in prompt

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["codex", "antigravity"])
async def test_prompt_version_uses_new_session_without_resetting_logical_input_or_budget(tmp_path, monkeypatch, kind):
    import sys

    from test_complete_snapshot_discovery import fixture_router

    from auto_agents_2.evidence import invocation_identity
    from auto_agents_2.models import IntakeResult
    from auto_agents_2.providers import VIEW_VERSION
    from auto_agents_2.transports import CodexTransport
    from auto_agents_2.util import digest

    config, store, rid, router, context = fixture_router(tmp_path)
    monkeypatch.setattr(router, "execution_identity", lambda stage: "fixture-execution")
    effective = {"kind": kind, "provider": "fixture", "model": "fixture-model",
                 "fingerprint": "fixture-settings", "binary": sys.executable,
                 "prompt_profile": "auto", "level": "deep"}
    identity = invocation_identity("clarify", context, IntakeResult, tmp_path, config.load(),
                                   execution="fixture-execution")
    from auto_agents_2.context_plan import CONTEXT_VERSION

    old_key = "/".join(["session", "clarify", "fixture", VIEW_VERSION, "fixture-old-prompt",
                        CONTEXT_VERSION, effective["fingerprint"], digest(config.load().sensitive_paths)])
    store.checkpoint(rid, old_key, "fixture-retained-conversation")
    budget = {"failures": 2, "grant": 1, "pending": False, "error_kind": "unavailable"}
    store.checkpoint(rid, "infra/clarify/fixture", budget)
    monkeypatch.setattr("auto_agents_2.prompts.PROMPT_VERSION", "fixture-new-prompt")
    monkeypatch.setattr("auto_agents_2.providers.PROMPT_VERSION", "fixture-new-prompt")
    seen = []

    class CapturedNativeArguments(Exception):
        pass

    class Capture(CodexTransport):
        def argv(self, effective, workspace, schema, output, prompt, previous, images, context_files=()):
            seen.append(previous)
            assert "aa2 is the controller of this workflow" in prompt
            raise CapturedNativeArguments

    router.transports[kind] = Capture()
    with pytest.raises(CapturedNativeArguments):
        await router._call(effective, "clarify", copy.deepcopy(context), IntakeResult,
                           tmp_path, rid, "clarify", fresh=False)

    assert seen == [None]
    assert store.get(rid, old_key) == "fixture-retained-conversation"
    assert store.get(rid, "infra/clarify/fixture") == budget
    assert invocation_identity("clarify", context, IntakeResult, tmp_path, config.load(),
                               execution="fixture-execution") == identity
    assert store.get(rid, "provider-result/clarify") is None


@pytest.mark.parametrize("model", ["gpt-fixture", "gemini-fixture", "claude-fixture"])
def test_static_prototype_respects_native_permissions_without_weakening_acceptance(model):
    raw = "Fixture: simulated processing permitted; real MP4 decoding and human approval still required."
    context = {"raw_requirements": raw, "project": {"read_on_demand": ["public.txt"]}}
    prompt, _ = PromptCompiler().compile("prototype", context, model)
    assert compiled_context(prompt) == context
    assert "read-only shell guidance does not grant command access or promise headless approval" in prompt
    assert "Do not probe the host toolchain or search host directories" in prompt
    assert "After a concrete native denial, stop that operation and target" in prompt
    assert "Preserve explicit requirements for real files, decoding, checks and approval" in prompt
    assert "do not fake media bytes or waive missing evidence" in prompt
    assert "The controller runs these checks after applying your proposal" in prompt
    assert "Respect required human prototype approval" in prompt
    other, _ = PromptCompiler().compile("clarify", context, model)
    assert "read-only shell and image tools may be used when needed for that inspection" in other
    assert "Do not probe the host toolchain or search host directories" not in other


@pytest.mark.parametrize("model", ["gpt-fixture", "gemini-fixture", "claude-fixture"])
def test_prototype_inspection_uses_admitted_files_not_native_tool_discovery(model):
    context = {
        "raw_requirements": "Fixture: preserve real download, decoding, review and human approval.",
        "project": {"read_on_demand": ["design/existing.html"]},
        "context_files": ["/fixture/admitted-design.md"],
    }
    before = copy.deepcopy(context)
    prompt, _ = PromptCompiler().compile("prototype", context, model)

    assert compiled_context(prompt) == before and context == before
    assert "bounded native file/image reads" in prompt
    assert "Do not use native command execution for this authoring stage" in prompt
    assert "Do not discover tools or inspect host/provider application data" in prompt
    assert "configuration, caches, conversations, credentials or bundled tooling" in prompt
    assert "Paths mentioned by native tool documentation are not additional project inputs" in prompt
    assert "A missing permitted read capability is an evidence gap to report" in prompt
    assert "read-only shell and image tools may be used" not in prompt
    assert "Preserve explicit requirements for real files, decoding, checks and approval" in prompt
    assert "After a concrete native denial, stop that operation and target" in prompt

    # Review and other author stages retain their existing bounded inspection contract.
    for stage in ("clarify", "review", "implement"):
        other, _ = PromptCompiler().compile(stage, context, model)
        assert "read-only shell and image tools may be used when needed for that inspection" in other
        assert "Static prototype inspection:" not in other
