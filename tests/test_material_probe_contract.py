import json
from pathlib import Path

import pytest
from test_browser import browser_cache  # noqa: F401

from auto_agents_2.artifacts import ArtifactStore
from auto_agents_2.capabilities import ReadChallengeResult, challenge_files, confirm_read
from auto_agents_2.context_plan import ContextPlanner
from auto_agents_2.errors import ProviderError
from auto_agents_2.prompts import PromptCompiler
from auto_agents_2.transports import AntigravityTransport

MODELS = ("gpt-6.1-sol", "gemini-3.8-flash-high", "claude-fixture", "custom-fixture")


def compile_probe(context, artifacts, workspace, model):
    prepared = ContextPlanner(artifacts).prepare("material_probe", context, workspace)
    prompt, _ = PromptCompiler().compile("material_probe", prepared.payload, model)
    wire = AntigravityTransport().encode_prompt(prompt)
    delivered = json.loads(wire)["message"]["content"]
    assert delivered == prompt
    data = json.loads(delivered.split("<context>\n", 1)[1].split("\n</context>", 1)[0])
    task = delivered.split('<task stage="material_probe">\n', 1)[1].split("\n</task>", 1)[0]
    files = [Path(path) for path in data["context_files"]]
    images = [Path(data["screenshot"])] if data.get("screenshot") else []
    argv = AntigravityTransport().argv(
        {"binary": "fixture-provider-no-execution", "model": model, "effort": "", "args": []},
        workspace, workspace / "schema.json", workspace / "result.json", prompt, None, images, files,
    )
    directories = [argv[index + 1] for index, value in enumerate(argv) if value == "--add-dir"]
    assert directories == sorted({str(path.parent) for path in [*images, *files]})
    return data, task


@pytest.mark.parametrize("model", MODELS)
async def test_text_only_probe_names_only_delivered_text_and_requires_empty_image(tmp_path, model):
    artifacts = ArtifactStore(tmp_path / "state")
    context, expected = await challenge_files(artifacts, tmp_path, image_required=False)
    assert set(context) == {"context_files"} and expected[1] == ""
    data, task = compile_probe(context, artifacts, tmp_path, model)
    assert data["context_files"] == context["context_files"] and "screenshot" not in data
    assert "text files explicitly listed in context_files" in task
    assert 'image_token must be an empty string ("")' in task
    assert "No screenshot is supplied" in task
    assert "Do not search for images, list parent directories, or read unlisted paths" in task
    assert "and the screenshot through your native tools" not in task
    response = ReadChallengeResult(text_token=Path(data["context_files"][0]).read_text(), image_token="")
    assert confirm_read(response, expected)["status"] == "observed_read"
    with pytest.raises(ProviderError, match="read challenge failed"):
        confirm_read(response.model_copy(update={"image_token": "invented-image"}), expected)


@pytest.mark.usefixtures("browser_cache")
async def test_image_probe_keeps_real_png_path_and_requires_both_tokens(tmp_path):
    artifacts = ArtifactStore(tmp_path / "state")
    context, expected = await challenge_files(artifacts, tmp_path, image_required=True)
    image = Path(context["screenshot"])
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert expected[0] and expected[1]
    for model in MODELS:
        data, task = compile_probe(context, artifacts, tmp_path, model)
        assert data["screenshot"] == str(image)
        assert "Read the complete context_files text attachment and the screenshot through your native tools" in task
        assert "Return the exact token in each as text_token and image_token" in task
        assert "No screenshot is supplied" not in task
    response = ReadChallengeResult(text_token=Path(context["context_files"][0]).read_text(), image_token=expected[1])
    assert confirm_read(response, expected)["status"] == "observed_read"
    with pytest.raises(ProviderError, match="read challenge failed"):
        confirm_read(response.model_copy(update={"image_token": ""}), expected)


@pytest.mark.parametrize(("text", "image"), [
    pytest.param("expected-text", "", id="missing-image"),
    pytest.param("expected-text", "wrong-image", id="wrong-image"),
    pytest.param("wrong-text", "expected-image", id="wrong-text"),
])
def test_image_required_gate_rejects_missing_or_wrong_tokens(text, image):
    with pytest.raises(ProviderError, match="read challenge failed"):
        confirm_read(ReadChallengeResult(text_token=text, image_token=image), ("expected-text", "expected-image"))
