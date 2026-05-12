"""Heuristic pronunciation scoring: expected word vs child transcript (edit distance + token pick)."""

from __future__ import annotations

import re
from typing import Any


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            ins, delete, sub = cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def _normalize_word(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z']+", text or "")


# Short replies that are not pronunciation attempts of the target word
_SKIP_TRANSCRIPTS: frozenset[str] = frozenset(
    {
        "yes",
        "no",
        "ok",
        "okay",
        "yeah",
        "yep",
        "yup",
        "nah",
        "hi",
        "hello",
        "hey",
        "thanks",
        "thank you",
        "bye",
        "uh",
        "um",
        "hmm",
        "good",
        "fine",
        "great",
        "cool",
        "nice",
        "what",
        "who",
        "why",
        "where",
        "when",
        "how",
        "which",
    }
)

_QUESTION_STARTERS: frozenset[str] = frozenset(
    {
        "who",
        "what",
        "where",
        "when",
        "why",
        "how",
        "which",
        "whose",
        "are",
        "is",
        "am",
        "do",
        "does",
        "did",
        "can",
        "could",
        "will",
        "would",
        "should",
        "may",
        "might",
        "must",
        "tell",
        "say",
        "show",
        "give",
        "let",
        "please",
        "i",  # "i don't know", "i want", "i am"
        "my",
        "your",
        "the",
    }
)


def looks_like_chat(transcript: str) -> bool:
    """Heuristic: looks like conversation/question, not a pronunciation attempt."""
    t = (transcript or "").strip().lower()
    if not t:
        return True
    if "?" in t:
        return True
    tokens = t.split()
    if not tokens:
        return True
    if tokens[0].strip(".,!?'\"") in _QUESTION_STARTERS:
        return True
    if len(tokens) >= 4:
        return True
    return False


def should_skip_scoring(transcript: str) -> bool:
    t = (transcript or "").strip().lower().rstrip(".,!?")
    if len(t) < 2:
        return True
    if t in _SKIP_TRANSCRIPTS:
        return True
    return False


# Tokens common in "Are you ready?" / "Let's start!"–style replies — not lesson vocabulary.
_READINESS_FILLER_TOKENS: frozenset[str] = frozenset(
    {
        "yes",
        "no",
        "ok",
        "okay",
        "yeah",
        "yep",
        "yup",
        "nah",
        "sure",
        "alright",
        "right",
        "ready",
        "im",
        "ill",
        "mhm",
        "uh",
        "um",
        "hi",
        "hello",
        "hey",
        "thanks",
        "bye",
        "good",
        "fine",
        "great",
        "cool",
        "nice",
        "am",
        "are",
        "we",
        "i",
        "you",
        "it",
        "is",
        "do",
        "did",
        "can",
        "will",
        "here",
        "go",
        "come",
        "on",
        "let",
        "lets",
        "start",
        "starting",
        "please",
    }
)


def _phrase_key(transcript: str) -> str:
    """Lowercase letters/digits only, single spaces — for comparing fixed readiness phrases."""
    t = re.sub(r"[^a-z0-9\s]+", " ", (transcript or "").lower())
    return re.sub(r"\s+", " ", t).strip()


# Normalized multi-word replies that are never a pronunciation attempt at the lesson word.
_READINESS_PHRASE_KEYS: frozenset[str] = frozenset(
    {
        _phrase_key(p)
        for p in (
            "yes i'm ready",
            "yes im ready",
            "yeah i'm ready",
            "yeah im ready",
            "ok i'm ready",
            "ok im ready",
            "i'm ready",
            "im ready",
            "we're ready",
            "were ready",
            "i am ready",
            "we are ready",
            "yes let's go",
            "yeah let's go",
            "ok let's go",
            "lets go",
            "let's go",
            "ok lets go",
            "yes lets go",
            "ready",
            "i'm here",
            "im here",
            "here i am",
            "we can start",
            "i can do it",
            "sure thing",
        )
    }
)

# Flexible patterns: affirmation + optional "I'm" + optional "ready/here", etc.
_READINESS_ACK_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|ok|okay|sure|alright|right|mhm|uh[\s\-]?huh)" r"(\s+(i['\s]?m|i\s+am|we['\s]?re))?" r"\s*(ready|here)?\s*[.!?…]*\s*$",
    re.IGNORECASE | re.VERBOSE,
)
_STANDALONE_READY_RE = re.compile(r"^\s*ready\s*[.!?…]*\s*$", re.IGNORECASE)
_LETS_GO_RE = re.compile(
    r"^\s*(ok|yes|yeah)?\s*(let['\s]s)\s+go\s*[.!?…]*\s*$",
    re.IGNORECASE | re.VERBOSE,
)


def looks_like_readiness_acknowledgment(transcript: str, expected: str) -> bool:
    """True if this sounds like answering \"ready?\" / \"shall we start?\" — not practicing the target word.

    If the child clearly includes the lesson ``expected`` word (normalized token match), returns False
    so normal scoring can run.
    """
    exp = _normalize_word(expected or "")
    if not (transcript or "").strip():
        return True

    raw_tokens = _tokens(transcript)
    norm_tokens = [_normalize_word(t) for t in raw_tokens if _normalize_word(t)]

    # Child said the target word — treat as an attempt even among fillers ("yes banana").
    if exp and exp in norm_tokens:
        return False

    key = _phrase_key(transcript)
    if key in _READINESS_PHRASE_KEYS:
        return True

    if _READINESS_ACK_RE.match((transcript or "").strip()):
        return True
    if _LETS_GO_RE.match((transcript or "").strip()):
        return True

    # Standalone "ready" is usually answering the tutor — unless the vocabulary word is literally "ready".
    if exp != "ready" and _STANDALONE_READY_RE.match((transcript or "").strip()):
        return True

    # Only short replies: every token is a filler/ack — not a attempt at a content word.
    if norm_tokens and len(norm_tokens) <= 6 and all(t in _READINESS_FILLER_TOKENS for t in norm_tokens):
        return True

    return False


def score_utterance(expected: str, transcript: str, thresholds: dict[str, Any]) -> dict[str, Any]:
    """
    Return score 0-100, band correct|almost|incorrect, and best matching token from transcript.
    """
    exp = _normalize_word(expected)
    if not exp:
        return {
            "score": 0,
            "band": "incorrect",
            "best_token": "",
            "expected_normalized": exp,
        }

    correct = int(thresholds.get("correct", 90))
    almost = int(thresholds.get("almostCorrect", 70))

    raw_tokens = _tokens(transcript)
    if not raw_tokens:
        return {
            "score": 0,
            "band": "incorrect",
            "best_token": "",
            "expected_normalized": exp,
        }

    best_score = 0
    best_tok = ""
    for tok in raw_tokens:
        w = _normalize_word(tok)
        if not w:
            continue
        dist = _levenshtein(exp, w)
        mlen = max(len(exp), len(w), 1)
        s = max(0, min(100, round(100 * (1 - dist / mlen))))
        if w == exp:
            s = 100
        if s > best_score:
            best_score = s
            best_tok = w

    if best_score >= correct:
        band = "correct"
    elif best_score >= almost:
        band = "almost"
    else:
        band = "incorrect"

    return {
        "score": best_score,
        "band": band,
        "best_token": best_tok,
        "expected_normalized": exp,
    }
