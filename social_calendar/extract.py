"""Gate + vision extraction. Ported from the Phase 0 spike, which validated this
call path on 160 real posts (SPEC section 3 RESULT).

Runs on OpenAI's Responses API. Both stages take the same shape -- a system
prompt, one user turn, and a strict JSON schema -- so `_respond` owns the
request and everything else describes what to put in it.

`gpt-5.4-mini` replaced `claude-haiku-4-5` for cost: $0.75/$4.50 per million
tokens against Haiku's $1.00/$5.00. The gate is a text-only classification and
runs fine a rung lower on `gpt-5.4-nano` ($0.20/$1.25) -- set GATE_MODEL to move
it, but re-run the replay eval first, the same way SPEC section 3 qualified
rung 1 in the first place.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path

import openai

from . import prompts, spend
from .paths import MEDIA_DIR

# SPEC section 3 escalation ladder, re-pointed at OpenAI. Rung 1 is production.
RUNGS = {1: "gpt-5.4-mini", 2: "gpt-5.4", 3: "gpt-5.5"}
DEFAULT_RUNG = 1

# Per-stage override. None means "whatever rung the extractor is on", which is
# the safe default: a cheaper gate is a measured decision, not a free win.
GATE_MODEL: str | None = None


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
        return [{"type": "input_text", "text": f"Caption:\n{caption}"}]

    blocks: list[dict] = []
    for name in post.get("local_images") or []:
        path = media_dir / name
        if not path.exists():
            continue
        data = path.read_bytes()
        # Inline data URI rather than an upload: these images are already on
        # disk, used once, and never referenced again, so the Files API would
        # add a round trip and something to clean up for no benefit.
        blocks.append({
            "type": "input_image",
            "image_url": (f"data:{sniff_media_type(data)};base64,"
                          f"{base64.standard_b64encode(data).decode()}"),
            "detail": "auto",
        })
    # posted_at goes in as its own salient line -- flyers say "THIS SATURDAY"
    # with no year, and this is the only anchor that resolves them.
    blocks.append({
        "type": "input_text",
        "text": prompts.extract_user_text(
            post.get("caption", ""), human_date(post.get("posted_at"))
        ),
    })
    return blocks


def _refusal(resp) -> str | None:
    """The refusal text, if the model declined. Refusals arrive as a content
    part inside an ordinary message rather than as a status, so a caller that
    only checks `status` reads them as an empty answer."""
    for item in getattr(resp, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return getattr(part, "refusal", "refused")
    return None


class Extractor:
    def __init__(self, client: openai.OpenAI | None = None, rung: int = DEFAULT_RUNG,
                 media_dir: Path = MEDIA_DIR, meter: spend.Meter | None = None):
        self.client = client or openai.OpenAI()
        self.rung = rung
        self.model = RUNGS[rung]
        self.media_dir = media_dir
        # Cost accrues here and is drained by the pipeline, which is the layer
        # that holds a database connection. `discovery.propose` borrows this
        # extractor's client, so it reports into the same meter.
        self.meter = meter if meter is not None else spend.Meter()

    def model_for(self, stage: str) -> str:
        return GATE_MODEL if (stage == "gate" and GATE_MODEL) else self.model

    def _respond(self, model: str, system: str, content: list[dict],
                 schema: dict, name: str) -> dict:
        """One structured call. Returns parsed JSON or {"_error": ...}, never
        raises, and meters before every early return -- a refusal and a garbled
        answer both cost exactly what a useful one does."""
        try:
            resp = self.client.responses.create(
                model=model,
                instructions=system,
                input=[{"role": "user", "content": content}],
                max_output_tokens=2048,
                text={"format": {"type": "json_schema", "name": name,
                                 "schema": schema, "strict": True}},
            )
        except Exception as exc:
            # No response means no usage object and nothing billed -- a request
            # rejected before the model ran costs nothing.
            return {"_error": f"{type(exc).__name__}: {exc}"}

        self.meter.add_openai(model, getattr(resp, "usage", None))

        if (refusal := _refusal(resp)) is not None:
            return {"_error": "refusal", "_stop_details": refusal}
        if resp.status == "incomplete":
            # Almost always the output cap. Distinguished from a refusal
            # because the fix is different: raise max_output_tokens.
            return {"_error": f"incomplete: {resp.incomplete_details}"}

        text = resp.output_text
        if not text:
            return {"_error": f"no text output (status={resp.status})"}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            return {"_error": f"unparseable JSON: {exc}", "_raw": text}

    def run(self, post: dict, stage: str) -> dict:
        """Return parsed model output, or {"_error": ...}. Never raises."""
        return self._respond(
            self.model_for(stage),
            prompts.system_for(stage),
            _content(post, stage, self.media_dir),
            prompts.GATE_SCHEMA if stage == "gate" else prompts.EXTRACT_SCHEMA,
            stage,
        )

    def gate(self, post: dict) -> dict:
        return self.run(post, "gate")

    def extract(self, post: dict) -> dict:
        return self.run(post, "extract")
