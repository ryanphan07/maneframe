import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger("maneframe.llm")

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - optional dependency in lightweight CI
    AsyncOpenAI = None

llm_client = AsyncOpenAI(
    base_url=settings.LLM_BASE_URL,
    api_key=settings.LLM_API_KEY,
    timeout=settings.LLM_TIMEOUT,
    max_retries=0,  # the SDK default of 2 would turn one 300 s timeout into 15 minutes
) if AsyncOpenAI is not None else None

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_PATH = PROMPTS_DIR / "commentary_system.txt"
GLOSSARY_PATH = PROMPTS_DIR / "lion_dance_glossary.txt"
EXAMPLES_PATH = PROMPTS_DIR / "examples.txt"
STYLES_DIR = PROMPTS_DIR / "styles"
DEFAULT_STYLE = "hype"
JSON_NUDGE = "Your previous reply was not valid JSON. Return only the JSON object, no prose and no code fences."


class LLMOutputError(Exception):
    """The model replied, but not with usable JSON."""


@dataclass
class WindowResult:
    quips: List[Dict[str, Any]]
    seen: Optional[str] = None  # the model's one-sentence description of the window, used as context for the next


@lru_cache(maxsize=1)
def _read_prompt_file() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _body(text: str, drop_prefixes: tuple) -> str:
    """The text without lines that start with one of drop_prefixes (owner notes and format comments)."""
    return "\n".join(line for line in text.splitlines() if not line.startswith(drop_prefixes)).strip()


@lru_cache(maxsize=1)
def _read_glossary() -> str:
    return _body(GLOSSARY_PATH.read_text(encoding="utf-8"), ("DRAFT",))


@lru_cache(maxsize=1)
def _read_examples() -> str:
    """Empty when the file holds only comments: the prompt gets an examples section only if there are real ones."""
    return _body(EXAMPLES_PATH.read_text(encoding="utf-8"), ("#",))


def list_styles() -> List[str]:
    return sorted(p.stem for p in STYLES_DIR.glob("*.txt"))


@lru_cache(maxsize=None)
def _read_style(style: str) -> str:
    return (STYLES_DIR / f"{style}.txt").read_text(encoding="utf-8").strip()


def clear_prompt_cache() -> None:
    for reader in (_read_prompt_file, _read_glossary, _read_examples, _read_style):
        reader.cache_clear()


def load_system_prompt(style: str = DEFAULT_STYLE) -> str:
    """Base prompt (literal MAX_QUIPS replaced by MAX_QUIPS_PER_WINDOW), then the glossary, the examples if any, and the style."""
    if style not in list_styles():
        raise ValueError(f"Unknown style {style!r}")
    parts = [re.sub(r"MAX_QUIPS(?!_)", str(settings.MAX_QUIPS_PER_WINDOW), _read_prompt_file())]
    glossary = _read_glossary()
    if glossary:
        parts.append("LION DANCE GLOSSARY (background knowledge; use a term only when you can see it happen)\n" + glossary)
    examples = _read_examples()
    if examples:
        parts.append("MORE EXAMPLES OF THE OUTPUT FORMAT (do not reuse these lines)\n" + examples)
    parts.append("COMMENTARY STYLE FOR THIS RUN\n" + _read_style(style))
    return "\n\n".join(parts)


def prompt_hash(style: str = DEFAULT_STYLE) -> str:
    """Short hash of the full system prompt, to tell prompt versions apart in eval results."""
    return hashlib.sha256(load_system_prompt(style).encode("utf-8")).hexdigest()[:8]


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return re.sub(r"^.*?</think>", "", text, flags=re.DOTALL).strip()  # opening tag was in the template


def parse_model_objects(text: str) -> List[Dict[str, Any]]:
    """Every top-level JSON object in the reply, in order. Strips <think> blocks and code fences, skips prose."""
    text = re.sub(r"```(?:json)?", "", strip_think(text or ""), flags=re.IGNORECASE)
    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    pos = text.find("{")
    while pos != -1:
        try:
            obj, length = decoder.raw_decode(text[pos:])
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            objects.append(obj)
            pos = text.find("{", pos + length)
        else:
            pos = text.find("{", pos + 1)
    return objects


def parse_model_json(text: str) -> Dict[str, Any]:
    """The first JSON object in the reply."""
    objects = parse_model_objects(text)
    if not objects:
        raise LLMOutputError("No JSON object found in model reply")
    return objects[0]


def extract_quips(objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Quips from the first object with a 'quips' list; else from objects that are themselves single quips."""
    for obj in objects:
        if isinstance(obj.get("quips"), list):
            return clean_quips(obj)
    singles = [o for o in objects if "t" in o and "text" in o]
    if singles:
        quips = clean_quips({"quips": singles})
        if quips:
            return quips
    raise LLMOutputError("Model JSON has no 'quips' list")


def response_format() -> Dict[str, Any]:
    """JSON schema for the prompt's output: {"seen": str, "quips": [{"t": number, "text": str}]}."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "commentary",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "seen": {"type": "string"},
                    "quips": {
                        "type": "array",
                        "maxItems": settings.MAX_QUIPS_PER_WINDOW,
                        "items": {
                            "type": "object",
                            "properties": {"t": {"type": "number"}, "text": {"type": "string"}},
                            "required": ["t", "text"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["seen", "quips"],
                "additionalProperties": False,
            },
        },
    }


def clean_quips(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return [{"timestamp", "text"}] sorted by time, dropping malformed or empty entries."""
    raw = parsed.get("quips")
    if not isinstance(raw, list):
        raise LLMOutputError("Model JSON has no 'quips' list")
    quips = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            t = float(item["t"])
        except (KeyError, TypeError, ValueError):
            continue
        text = " ".join(str(item.get("text") or "").split())
        if text and t == t:  # t == t rejects NaN
            quips.append({"timestamp": t, "text": text})
    return sorted(quips, key=lambda q: q["timestamp"])


def build_messages(
    frames: List[Dict[str, Any]],
    start: float,
    end: float,
    previous_quips: List[str],
    nudge: bool = False,
    earlier_seen: Optional[List[str]] = None,
    style: str = DEFAULT_STYLE,
) -> List[Dict[str, Any]]:
    """System prompt first, the images last, so the server can reuse its prefix cache."""
    intro = f"These frames cover {start:.1f}s to {end:.1f}s of the video."
    if earlier_seen:
        intro += (
            " Earlier context, from the windows just before this one (oldest first; it is not in these frames, "
            "so do not describe it again): " + json.dumps(earlier_seen)
        )
    if previous_quips:
        intro += (
            " Your most recent previous quips (do not repeat them or reuse their jokes, comparisons or sentence shapes): "
            + json.dumps(previous_quips)
        )
    if nudge:
        intro += " " + JSON_NUDGE
    content: List[Dict[str, Any]] = [{"type": "text", "text": intro}]
    for frame in frames:
        content.append({"type": "text", "text": f"Frame at t={frame['timestamp']}s:"})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{frame['image_data']}"},
        })
    return [
        {"role": "system", "content": load_system_prompt(style)},
        {"role": "user", "content": content},
    ]


async def _call(messages: List[Dict[str, Any]], frame_count: int, label: str) -> str:
    started = time.monotonic()
    kwargs: Dict[str, Any] = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": settings.LLM_TEMPERATURE,
        "max_tokens": settings.LLM_MAX_TOKENS,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": settings.LLM_ENABLE_THINKING}},
    }
    if settings.LLM_JSON_SCHEMA:
        kwargs["response_format"] = response_format()
    try:
        response = await llm_client.chat.completions.create(**kwargs)
    except Exception as exc:
        if "response_format" not in kwargs or getattr(exc, "status_code", None) != 400:
            raise
        logger.warning("%s: server rejected response_format with HTTP 400; retrying once without it", label)
        del kwargs["response_format"]
        response = await llm_client.chat.completions.create(**kwargs)
    usage = getattr(response, "usage", None)
    logger.info(
        "LLM %s: frames=%d prompt_tokens=%s completion_tokens=%s seconds=%.1f",
        label,
        frame_count,
        getattr(usage, "prompt_tokens", "?"),
        getattr(usage, "completion_tokens", "?"),
        time.monotonic() - started,
    )
    return response.choices[0].message.content or ""


async def analyze_window(
    frames: List[Dict[str, Any]],
    start: float,
    end: float,
    previous_quips: Optional[List[str]] = None,
    label: str = "window",
    earlier_seen: Optional[List[str]] = None,
    style: str = DEFAULT_STYLE,
) -> WindowResult:
    """One LLM call for one window. Retries once with a JSON nudge if the reply is unusable.

    Raises on transport errors/timeouts (no retry: a slow model would double the wait) and on a second bad reply.
    """
    if llm_client is None:
        raise RuntimeError("openai package is not installed")
    previous_quips = previous_quips or []
    last_error: Optional[Exception] = None
    for attempt in range(2):
        messages = build_messages(frames, start, end, previous_quips, nudge=attempt > 0, earlier_seen=earlier_seen, style=style)
        reply = await _call(messages, len(frames), label)
        try:
            objects = parse_model_objects(reply)
            quips = extract_quips(objects)
            seen = next((o["seen"] for o in objects if "seen" in o), None)
            logger.debug("%s seen: %s", label, str(seen)[:300])
            seen = " ".join(seen.split()) if isinstance(seen, str) else None
            return WindowResult(quips=quips, seen=seen or None)
        except LLMOutputError as exc:
            last_error = exc
            logger.warning("%s: unusable reply on attempt %d (%s); full reply: %r", label, attempt + 1, exc, reply[:2000])
    raise LLMOutputError(f"{label}: model returned no usable JSON after retry") from last_error
