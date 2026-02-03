import logging
import random
import re
from typing import Any, Dict, List, Optional, Sequence

# pinterest_scheduler/services/hook_generator.py

logger = logging.getLogger(__name__)

_BAD_END_WORDS = {
    "a", "an", "the", "and", "or", "but",
    "to", "of", "in", "on", "at", "for", "from", "by",
    "with", "without", "into", "onto", "over", "under",
    "your", "you", "its", "it's", "their", "this", "that", "these", "those",
    "hidden",
}

_BAD_END_RE = re.compile(r"[^a-zA-Z']+")


def _looks_incomplete(hook: str) -> bool:
    """Heuristic to detect 'half-written' hooks that read unfinished."""
    s = _one_line(hook)
    if not s:
        return True

    if len(s) < 12:
        return True

    # Ends with punctuation fragments often indicate truncation
    if s.endswith((",", ":", ";", "-", "—", "–")):
        return True

    # Ends with dangling connector/stopword
    last = s.split()[-1]
    last = _BAD_END_RE.sub("", last).lower()
    if last in _BAD_END_WORDS:
        return True

    return False


def _is_good_hook(hook: str, max_chars: int, recent: Optional[set] = None) -> bool:
    s = _one_line(hook)
    if not s:
        return False
    if "\n" in (hook or "") or "\r" in (hook or ""):
        return False
    if len(s) > max_chars:
        return False
    if _looks_incomplete(s):
        return False

    if recent:
        if s.lower().strip() in recent:
            return False

    return True

def _one_line(s: str) -> str:
    """Normalise to a single line, stripping quotes and excess whitespace."""
    if not s:
        return ""
    s = str(s).strip()
    s = s.replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    # Remove wrapping quotes that models sometimes add
    s = s.strip('"\'')
    return s

def _clamp_chars(s: str, max_chars: int = 50) -> str:
    """Clamp to a hard character limit (including spaces).

    - Forces single-line output.
    - Tries to cut on a word boundary.
    - Strips trailing punctuation.
    """
    s = _one_line(s)
    if not s:
        return ""

    if len(s) <= max_chars:
        return s

    cut = s[:max_chars].rstrip()

    # Prefer cutting at a word boundary (avoid over-shortening)
    last_space = cut.rfind(" ")
    if last_space >= max(10, int(max_chars * 0.6)):
        cut = cut[:last_space].rstrip()

    # Clean trailing punctuation
    cut = cut.rstrip(" ,.;:!-–—")

    # If we still end on a dangling connector, drop the last word.
    try:
        last_word = _BAD_END_RE.sub("", cut.split()[-1]).lower()
        if last_word in _BAD_END_WORDS and len(cut.split()) > 1:
            cut = " ".join(cut.split()[:-1]).rstrip()
    except Exception:
        pass

    return cut

def build_context(pin) -> Dict[str, Any]:
    """Build a resilient context dict for hook generation.

    This function should never raise due to missing relations/fields.
    """
    headline = getattr(pin, "headline", None)
    pillar_obj = getattr(headline, "pillar", None) if headline else None
    campaign_obj = getattr(pillar_obj, "campaign", None) if pillar_obj else None

    # Keywords can be missing if relation isn't available.
    keywords: List[str] = []
    try:
        kw_qs = getattr(pin, "keywords", None)
        if kw_qs is not None:
            keywords = list(kw_qs.values_list("phrase", flat=True)[:12])
    except Exception:
        keywords = []

    question = _one_line(getattr(pin, "title", None) or getattr(headline, "text", None) or "")

    return {
        "campaign": _one_line(getattr(campaign_obj, "name", "") if campaign_obj else ""),
        "pillar": _one_line(getattr(pillar_obj, "name", "") if pillar_obj else ""),
        "tagline": _one_line(getattr(pillar_obj, "tagline", "") if pillar_obj else ""),
        "question": question,
        "description": _one_line(getattr(pin, "description", "") or ""),
        "keywords": [k for k in (_one_line(x) for x in keywords) if k],
    }

def generate_hook_openai(
    context: Dict[str, Any],
    client,
    recent_hooks: Optional[Sequence[str]] = None,
    max_chars: int = 50,
    model: str = "gpt-4.1-mini",
    temperature: float = 0.9,
) -> str:
    recent_hooks_list = [
        _one_line(h) for h in (recent_hooks or [])
        if _one_line(h)
    ]

    pillar = _one_line(context.get("pillar", ""))
    tagline = _one_line(context.get("tagline", ""))
    question = _one_line(context.get("question", ""))
    description = _one_line(context.get("description", ""))
    keywords = context.get("keywords") or []

    if not isinstance(keywords, list):
        keywords = [str(keywords)]
    keywords = [_one_line(k) for k in keywords if _one_line(k)]

    pillar_line = (f"{pillar} — {tagline}" if tagline else pillar).strip()
    recent_block = list(recent_hooks_list[-12:])

    prompt = f"""
Write ONE scroll-stopping hook for a short-form culinary trivia video (Ruoth).

Audience: chefs, bakers, culinary pros + serious home bakers.
Goal: touch a nerve (status, competence, waste), without cringe.

ABSOLUTE RULES:
- EXACTLY one line
- MAX {max_chars} characters total (including spaces)
- No emojis, no hashtags
- Do NOT repeat the trivia question verbatim
- Do NOT reveal the answer
- Avoid repeating phrases used in these recent hooks: {recent_block}

Use ONE of these proven patterns (keep it short):
- Still [undesirable habit]?
- Ever [bad consequence]?
- Would you [X] after seeing [Y]?
- Pro-level [X] doesn't look like [low standard]
- Stop [undesirable]. Start [ideal].

Context:
Pillar: {pillar_line}
Trivia question: {question}
Description: {description}
Keywords: {", ".join(keywords)}

Return ONLY the hook text. No quotes. No extra lines.
""".strip()

    def _fallback_hook() -> str:
        # Safe fallback that doesn't reveal the answer.
        if keywords:
            token = keywords[0]
            options = [
                f"Still guessing {token} basics?",
                f"Ever messed up {token} on a bake?",
                f"Pro bakers don’t guess {token}.",
            ]
            return _clamp_chars(random.choice(options), max_chars=max_chars)

        # If the pillar suggests money/pricing, push that angle.
        pillar_l = (pillar or "").lower()
        if any(w in pillar_l for w in ["profit", "cost", "pricing", "business", "margin"]):
            return _clamp_chars("Still guessing profits by eye?", max_chars=max_chars)

        return _clamp_chars("Still guessing this ingredient?", max_chars=max_chars)

    def _call(prompt_text: str, temp: float) -> str:
        resp = client.responses.create(
            model=model,
            input=prompt_text,
            temperature=float(temp),
        )
        return _clamp_chars(getattr(resp, "output_text", "") or "", max_chars=max_chars)

    try:
        recent_set = {h.lower().strip() for h in recent_hooks_list if h}

        attempts = [
            (prompt, temperature),
            (prompt + f"\n\nRewrite: complete thought, no dangling ending, <= {max_chars} chars.", temperature * 0.85),
            (prompt + f"\n\nRewrite: sharp, complete, question OR statement, <= {max_chars} chars.", temperature * 0.7),
        ]

        for idx, (p, t) in enumerate(attempts, start=1):
            hook = _call(p, t)
            if _is_good_hook(hook, max_chars=max_chars, recent=recent_set):
                logger.info("hook_generator: accepted hook attempt=%s len=%s", idx, len(hook))
                return hook
            logger.info(
                "hook_generator: rejected hook attempt=%s len=%s hook=%r",
                idx,
                len(hook or ""),
                hook,
            )

        # If model keeps failing, use a deterministic safe fallback.
        return _fallback_hook()

    except Exception as e:
        logger.exception("Hook generation failed: %s", e)
        return _fallback_hook()