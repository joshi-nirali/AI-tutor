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
