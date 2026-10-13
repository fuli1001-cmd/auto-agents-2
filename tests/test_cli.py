import asyncio
from io import StringIO

import pytest
from rich.console import Console

from auto_agents_2 import cli
from auto_agents_2.store import Store


@pytest.mark.parametrize("tty", [False, True])
@pytest.mark.parametrize("settings, expected", [
    ({"model": "custom-model", "effort": "high"}, "模型：custom-model｜推理：high"),
    ({"model": "", "effort": ""}, "模型：CLI 默认（未知）｜推理：CLI 默认（未知）"),
    ({"model": "old-model"}, "模型：old-model｜推理：CLI 默认（未知）"),
])
async def test_runtime_displays_call_settings_instead_of_level(tmp_path, monkeypatch, tty, settings, expected):
    store = Store(tmp_path)
    store.event(None, "agent.started", {
        "stage": "implement", "provider": "custom-provider", "level": "custom-level", **settings,
    })
    output = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, width=200))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: tty)
    stop = asyncio.Event()

    async def stop_after_render(_):
        stop.set()

    monkeypatch.setattr(cli.asyncio, "sleep", stop_after_render)
    await cli.render(store, stop, asyncio.Event(), 0)
    rendered = output.getvalue()
    assert f"开发实现｜custom-provider｜{expected}" in rendered
    assert "custom-level" not in rendered
