"""Gate + vision extraction. Ported from the Phase 0 spike, which validated this
call path on 160 real posts (SPEC section 3 RESULT).

Both stages run on claude-haiku-4-5 -- the rung that passed. Escalating is a
config change here, not a code change, but per SPEC section 3 it needs explicit
approval first.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path

import anthropic

from . import prompts

# SPEC section 3 escalation ladder. Rung 1 passed Phase 0 and is production.
RUNGS = {1: "claude-haiku-4-5", 2: "claude-sonnet-5", 3: "claude-opus-5"}
DEFAULT_RUNG = 1

MEDIA_DIR = Path(__file__).parent.parent / "data" / "media"


def sniff_media_type(data: bytes) -> str:
    """Instagram serves WebP behind .jpg URLs; a wrong media_type is a 400."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def human_date(iso: str | None) -> str:
    if not iso:
        return "an unknown date"
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return f"{d:%Y-%m-%d} ({d:%A})"
    except ValueError:
        return str(iso)


def _content(post: dict, stage: str, media_dir: Path) -> list[dict]:
    if stage == "gate":
        caption = (post.get("caption") or "").strip() or "(no caption)"
        return [{"type": "text", "text": f"Caption:\n{caption}"}]

    blocks: list[dict] = []
    for name in post.get("local_images") or []:
        path = media_dir / name
        if not path.exists():
            continue
        data = path.read_bytes()
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": sniff_media_type(data),
                "data": base64.standard_b64encode(data).decode(),
            },
        })
    # posted_at goes in as its own salient line -- flyers say "THIS SATURDAY"
    # with no year, and this is the only anchor that resolves them.
    blocks.append({
        "type": "text",
        "text": prompts.extract_user_text(
            post.get("caption", ""), human_date(post.get("posted_at"))
        ),
    })
    return blocks


class Extractor:
    def __init__(self, client: anthropic.Anthropic | None = None, rung: int = DEFAULT_RUNG,
                 media_dir: Path = MEDIA_DIR):
        self.client = client or anthropic.Anthropic()
        self.rung = rung
        self.model = RUNGS[rung]
        self.media_dir = media_dir

    def run(self, post: dict, stage: str) -> dict:
        """Return parsed model output, or {"_error": ...}. Never raises."""
        schema = prompts.GATE_SCHEMA if stage == "gate" else prompts.EXTRACT_SCHEMA
        system = prompts.system_for(stage)
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": _content(post, stage, self.media_dir)}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except Exception as exc:
            return {"_error": f"{type(exc).__name__}: {exc}"}

        if resp.stop_reason == "refusal":
            return {"_error": "refusal", "_stop_details": str(resp.stop_details)}

        # A thinking block can precede the text on some models -- find the text
        # block rather than indexing content[0].
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            return {"_error": f"no text block (stop_reason={resp.stop_reason})"}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            return {"_error": f"unparseable JSON: {exc}", "_raw": text}

    def gate(self, post: dict) -> dict:
        return self.run(post, "gate")

    def extract(self, post: dict) -> dict:
        return self.run(post, "extract")
