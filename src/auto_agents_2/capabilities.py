"""Challenge-based material readability, separate from task correctness or model quality."""
from __future__ import annotations

import secrets
from pathlib import Path

from .errors import ProviderError
from .models import Record
from .util import digest


class ReadChallengeResult(Record):
    text_token: str
    image_token: str


async def challenge_files(artifacts, directory: Path, image_required=True):
    text_token, image_token = secrets.token_hex(8), secrets.token_hex(8) if image_required else ""
    text = artifacts.put(text_token, "read_challenge")
    context = {"context_files": [str(artifacts.root / text["hash"][:2] / text["hash"])]}
    if not image_required:
        return context, (text_token, image_token)
    from playwright.async_api import async_playwright
    image = directory / "challenge.png"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 900, "height": 160})
            await page.set_content(f'<body style="background:white;color:black;font:48px monospace;padding:20px">{image_token}</body>')
            await page.screenshot(path=str(image))
        finally:
            await browser.close()
    image_ref = artifacts.put_file(image, "read_challenge_image")
    return {"context_files": [str(artifacts.root / text["hash"][:2] / text["hash"])],
            "screenshot": str(artifacts.root / image_ref["hash"][:2] / image_ref["hash"])}, (text_token, image_token)


def confirm_read(response, expected):
    if (response.text_token.strip(), response.image_token.strip()) != expected:
        raise ProviderError("Native material read challenge failed; image review cannot proceed")
    return {"status": "observed_read", "challenge_identity": digest(expected)}
