# Engineered by uncoalesced
"""Instruction-like text: patterns that address an AI agent rather than a human reader.

Two strengths, so pages that merely discuss prompt injection don't raise alerts:
- STRONG rules (name agent-wire tools, tell the model to hide things from the user, smuggle data
  out through a URL) alert wherever they appear, visible text included.
- WEAK rules ("ignore previous instructions", "you are now", chat tokens) alert only when the text
  was hidden from human readers. In visible text they are counted, not raised: a security blog
  quoting them is not an attack on you.
"""

import re

_I = re.IGNORECASE

STRONG = {
    "tool-steering": re.compile(
        r"\b(?:call|invoke|run|use|execute)\s+(?:the\s+)?(?:wire_[a-z]+|wire\.(?:read|search|feed|"
        r"research|cite|me))\b",
        _I,
    ),
    "conceal-from-user": re.compile(
        r"\b(?:do\s+not|don'?t|never)\s+(?:tell|inform|mention|reveal|show|disclose)\b"
        r"[^.\n]{0,30}\b(?:the\s+)?user\b",
        _I,
    ),
    "exfil-instruction": re.compile(
        r"\b(?:send|email|post|upload|forward|exfiltrate|transmit)\b[^.\n]{0,60}"
        r"\b(?:api\s*keys?|tokens?|passwords?|credentials?|cookies?|secrets?|conversation|"
        r"chat\s+history|system\s+prompt)\b",
        _I,
    ),
    "markdown-beacon": re.compile(r"!\[[^\]\n]{0,80}\]\(https?://[^)\s]+\?[^)\s]*=[^)\s]+\)"),
}

WEAK = {
    "override-instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|"
        r"above|earlier|preceding|your)\s+(?:instructions?|prompts?|messages?|rules|directions)",
        _I,
    ),
    "role-reassignment": re.compile(
        r"\byou\s+are\s+now\s+(?:a|an|the|in)\b|\bnew\s+(?:system\s+)?instructions?\s*:"
        r"|\bfrom\s+now\s+on,?\s+you\s+(?:will|must)\b",
        _I,
    ),
    "addresses-ai": re.compile(
        r"\b(?:ai|llm|language\s+model|assistant|agent|chatbot)s?\s*[,:]\s*(?:please\s+)?"
        r"(?:ignore|you\s+must|you\s+should|do\s+not|always|never)\b"
        r"|\bsystem\s+prompt\b|\bdeveloper\s+message\b",
        _I,
    ),
    "chat-tokens": re.compile(
        r"<\|(?:im_start|im_end|system|endoftext|eot_id|start_header_id)\|>|\[/?INST\]"
        r"|^\s*(?:system|assistant)\s*:",
        _I | re.MULTILINE,
    ),
}

# Cheap prefilter: a page with none of these words can't match any rule.
_HINT = re.compile(
    r"ignore|disregard|forget|override|you are now|instruction|system|assistant|\[inst|<\||"
    r"wire_|wire\.|tell|reveal|mention|disclose|send|email|post|upload|forward|exfil|transmit|"
    r"!\[|from now on|llm|agent|chatbot|developer message",
    _I,
)


def find(text: str) -> list[tuple[str, bool, str]]:
    """[(rule_id, strong, matched text with context)] for instruction-like text."""
    if not text or not _HINT.search(text):
        return []
    hits = []
    for rule_id, rx in STRONG.items():
        if m := rx.search(text):
            hits.append((rule_id, True, _around(text, m)))
    for rule_id, rx in WEAK.items():
        if m := rx.search(text):
            hits.append((rule_id, False, _around(text, m)))
    return hits


def _around(text: str, m: re.Match, pad: int = 30) -> str:
    return text[max(0, m.start() - pad) : m.end() + pad]
