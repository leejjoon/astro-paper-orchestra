# BizRouter (https://bizrouter.ai) backend.
#
# BizRouter exposes an OpenAI-compatible API in front of Gemini, OpenAI,
# Anthropic and other models. This module lets the existing Gemini-style call
# sites (google.genai `types.Part` contents + `generation_configs` dicts) run
# unchanged against BizRouter:
#
#   * text / image parts      -> chat.completions content parts
#   * application/pdf parts   -> page images (+ extracted text), since BizRouter
#                                does not accept PDF input for Gemini models
#   * system_instruction      -> system message
#   * tools=[GoogleSearch]    -> BizRouter Search (model suffix ":search") or a
#                                search model given by BIZROUTER_SEARCH_MODEL
#   * image generation        -> POST /images/generations

import base64
import os
import threading
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv  # type: ignore
from google.genai import types  # type: ignore
from openai import OpenAI  # type: ignore

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

BIZROUTER_API_KEY = os.environ.get("BIZROUTER_API_KEY")
BIZROUTER_BASE_URL = os.environ.get(
    "BIZROUTER_BASE_URL", "https://api.bizrouter.ai/v1"
).rstrip("/")
# Model used for calls that request Google Search grounding (the literature
# review's candidate discovery). Gemini's native grounding tool is not
# available through BizRouter; BizRouter Search (":search" suffix) is used
# instead. In tests on an astronomy topic, gemini-3.1-pro with search returned
# far more ADS-verifiable titles (12/14) than gemini-3-flash (1/13) or
# perplexity/sonar-pro (6/10). Set to "" to use the calling model + ":search".
BIZROUTER_SEARCH_MODEL = os.environ.get(
    "BIZROUTER_SEARCH_MODEL", "google/gemini-3.1-pro-preview:search"
).strip()
# PDF pages are rendered at this DPI and at most this many pages are sent.
PDF_RENDER_DPI = int(os.environ.get("BIZROUTER_PDF_DPI", "100"))
PDF_MAX_PAGES = int(os.environ.get("BIZROUTER_PDF_MAX_PAGES", "40"))

# LLM_BACKEND selects where non-OpenAI-SDK calls go: "bizrouter" or "google".
LLM_BACKEND = os.environ.get(
    "LLM_BACKEND", "bizrouter" if BIZROUTER_API_KEY else "google"
).lower()
USE_BIZROUTER = LLM_BACKEND == "bizrouter"
if USE_BIZROUTER and not BIZROUTER_API_KEY:
    raise ValueError("LLM_BACKEND=bizrouter requires BIZROUTER_API_KEY to be set.")

# Aliases for Google model names that BizRouter lists under a different id.
_MODEL_ALIASES = {
    "gemini-3-pro-image-preview": "google/gemini-3-pro-image",
}

_client: Optional[OpenAI] = None
_client_lock = threading.Lock()
_model_ids: Optional[set] = None


def get_bizrouter_client() -> OpenAI:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = OpenAI(base_url=BIZROUTER_BASE_URL, api_key=BIZROUTER_API_KEY)
    return _client


def _available_models() -> set:
    global _model_ids
    if _model_ids is None:
        try:
            _model_ids = {m.id for m in get_bizrouter_client().models.list().data}
        except Exception as e:
            print(f"Warning: could not list BizRouter models ({e}); skipping validation.")
            _model_ids = set()
    return _model_ids


def resolve_model_name(model_name: str) -> str:
    """Map pipeline model names (e.g. 'gemini-3.1-pro-preview') to BizRouter ids."""
    if model_name in _MODEL_ALIASES:
        return _MODEL_ALIASES[model_name]
    if "/" in model_name:
        return model_name
    lower = model_name.lower()
    if lower.startswith("gemini"):
        candidate = f"google/{model_name}"
    elif lower.startswith(("gpt", "o1", "o3", "o4")):
        candidate = f"openai/{model_name}"
    elif lower.startswith("claude"):
        candidate = f"anthropic/{model_name}"
    else:
        candidate = model_name

    available = _available_models()
    if available and candidate not in available:
        stripped = candidate.removesuffix("-preview")
        if stripped in available:
            return stripped
        raise ValueError(
            f"Model '{model_name}' (resolved to '{candidate}') is not offered by BizRouter. "
            f"Use a full id such as 'google/gemini-3.1-pro-preview'."
        )
    return candidate


def _sniff_image_mime(data: bytes, declared: Optional[str]) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return declared or "image/png"


def _image_part(data: bytes, mime: Optional[str] = None) -> Dict[str, Any]:
    mime = _sniff_image_mime(data, mime)
    b64 = base64.b64encode(data).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _pdf_parts(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """Render a PDF as page images plus its extracted text."""
    import pymupdf  # type: ignore

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    n_pages = doc.page_count
    n_send = min(n_pages, PDF_MAX_PAGES)
    text = "\n\n".join(
        f"--- page {i + 1} ---\n{doc[i].get_text()}" for i in range(n_pages)
    )
    parts: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"[Attached PDF document: {n_pages} pages. Extracted text follows, then "
                f"images of {'all' if n_send == n_pages else f'the first {n_send}'} pages.]\n"
                f"{text}"
            ),
        }
    ]
    for i in range(n_send):
        pix = doc[i].get_pixmap(dpi=PDF_RENDER_DPI)
        parts.append(_image_part(pix.tobytes("jpeg", jpg_quality=85), "image/jpeg"))
    doc.close()
    return parts


def _part_to_openai(part: Any) -> List[Dict[str, Any]]:
    if part is None:
        return []
    if isinstance(part, str):
        return [{"type": "text", "text": part}]
    if isinstance(part, types.Content):
        out: List[Dict[str, Any]] = []
        for p in part.parts or []:
            out.extend(_part_to_openai(p))
        return out
    if isinstance(part, types.Part):
        if part.text is not None:
            return [{"type": "text", "text": part.text}]
        if part.inline_data is not None:
            data = part.inline_data.data
            mime = (part.inline_data.mime_type or "").lower()
            if mime == "application/pdf":
                return _pdf_parts(data)
            if mime.startswith("image/") or not mime:
                return [_image_part(data, mime or None)]
            raise ValueError(f"Unsupported inline data type for BizRouter: {mime}")
        raise ValueError(f"Unsupported google.genai Part for BizRouter: {part!r}"[:300])
    if isinstance(part, dict) and "type" in part:
        return [part]  # already OpenAI-format
    raise ValueError(f"Unsupported content item for BizRouter: {type(part)}")


def contents_to_messages(
    contents: Any, system_instruction: Optional[Any] = None
) -> List[Dict[str, Any]]:
    items = contents if isinstance(contents, (list, tuple)) else [contents]
    content: List[Dict[str, Any]] = []
    for item in items:
        content.extend(_part_to_openai(item))

    messages: List[Dict[str, Any]] = []
    if system_instruction:
        sys_parts = _part_to_openai(system_instruction)
        sys_text = "\n\n".join(p["text"] for p in sys_parts if p.get("type") == "text")
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
    messages.append({"role": "user", "content": content})
    return messages


def _wants_search(tools: Optional[List[Any]]) -> bool:
    for t in tools or []:
        if isinstance(t, types.Tool) and t.google_search is not None:
            return True
        if isinstance(t, dict) and ("google_search" in t or t.get("type") == "google_search"):
            return True
    return False


def generate_text(contents: Any, model_name: str, generation_configs: Optional[Dict] = None) -> str:
    """Single BizRouter chat completion; returns the response text."""
    cfg = dict(generation_configs or {})
    system_instruction = cfg.pop("system_instruction", None)
    tools = cfg.pop("tools", None)

    model = resolve_model_name(model_name)
    if _wants_search(tools):
        model = BIZROUTER_SEARCH_MODEL or f"{model}:search"

    kwargs: Dict[str, Any] = {}
    # OpenAI reasoning models only accept the default temperature.
    if "temperature" in cfg and not model.startswith("openai/"):
        kwargs["temperature"] = cfg.pop("temperature")
    cfg.pop("temperature", None)
    if "max_output_tokens" in cfg:
        kwargs["max_tokens"] = cfg.pop("max_output_tokens")
    if "top_p" in cfg:
        kwargs["top_p"] = cfg.pop("top_p")
    if cfg:
        print(f"Warning: ignoring generation configs not supported via BizRouter: {sorted(cfg)}")

    response = get_bizrouter_client().chat.completions.create(
        model=model,
        messages=contents_to_messages(contents, system_instruction),
        **kwargs,
    )
    text = response.choices[0].message.content
    if isinstance(text, list):  # multimodal content array
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if not text:
        raise ValueError(f"Empty response from BizRouter (model={model})")
    return text


def generate_image(
    model_name: str,
    prompt: str,
    aspect_ratio: str = "16:9",
    system_instruction: Optional[str] = None,
) -> Optional[bytes]:
    """Generate one image via BizRouter's /images/generations. Returns raw bytes."""
    full_prompt = f"{system_instruction}\n\n{prompt}" if system_instruction else prompt
    rsp = requests.post(
        f"{BIZROUTER_BASE_URL}/images/generations",
        headers={"Authorization": f"Bearer {BIZROUTER_API_KEY}"},
        json={
            "model": resolve_model_name(model_name),
            "prompt": full_prompt,
            "n": 1,
            "aspect_ratio": aspect_ratio,
        },
        timeout=300,
    )
    if rsp.status_code != 200:
        raise RuntimeError(f"BizRouter image generation HTTP {rsp.status_code}: {rsp.text[:300]}")
    data = rsp.json()["data"][0]
    if data.get("b64_json"):
        return base64.b64decode(data["b64_json"])
    if data.get("url"):
        return requests.get(data["url"], timeout=60).content
    return None
