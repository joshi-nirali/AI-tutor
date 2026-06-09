"""Load kid-tutor prompt JSON (ai_prompts, response_templates, pronunciation_rules) and build agent instructions."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROMPTS_DIR: Path | None = None
_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()  # Fix: thread-safe cache access


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def prompts_dir() -> Path:
    global _PROMPTS_DIR
    if _PROMPTS_DIR is not None:
        return _PROMPTS_DIR
    override = os.getenv("KID_PROMPTS_DIR", "").strip()
    if override:
        _PROMPTS_DIR = Path(override).expanduser().resolve()
    else:
        _PROMPTS_DIR = Path(__file__).resolve().parent / "data" / "prompts"
    return _PROMPTS_DIR


# ---------------------------------------------------------------------------
# JSON loading
# ---------------------------------------------------------------------------

def _load_json(filename: str) -> dict[str, Any]:
    with _CACHE_LOCK:
        if filename in _CACHE:
            return _CACHE[filename]

    path = prompts_dir() / filename

    if not path.is_file():
        logger.warning("Prompt file not found: %s", path)
        with _CACHE_LOCK:
            _CACHE[filename] = {}
        return {}

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    # Fix: warn explicitly if file is not a dict instead of silently returning {}
    if not isinstance(raw, dict):
        logger.warning(
            "Expected a JSON object in %s but got %s — ignoring file.",
            path,
            type(raw).__name__,
        )
        result: dict[str, Any] = {}
    else:
        result = raw

    with _CACHE_LOCK:
        _CACHE[filename] = result

    return result


def load_ai_prompts() -> dict[str, Any]:
    return _load_json("ai_prompts.json")


def load_response_templates() -> dict[str, Any]:
    return _load_json("response_templates.json")


def load_pronunciation_rules() -> dict[str, Any]:
    return _load_json("pronunciation_rules.json")


def load_lesson_sentences() -> dict[str, Any]:
    """Load the per-category, per-word practice sentences for SPEAKING mode."""
    return _load_json("lesson_sentences.json")


def load_lesson_quiz_questions() -> dict[str, Any]:
    """Load the per-category, per-word quiz Q&A bank for QUIZ mode."""
    return _load_json("lesson_quiz_questions.json")


def get_quiz_questions(topic_slug: str, word: str) -> list[dict[str, str]]:
    """Return the quiz-question list for a given (topic, word).

    Each item is ``{"question": str, "type": str, "answer": str}``.
    Falls back to two generic either-or questions if the JSON has no entry.
    """
    if not word:
        return []
    data = load_lesson_quiz_questions() or {}
    if not isinstance(data, dict):
        return []
    topic_block = data.get((topic_slug or "").lower())
    questions: list[dict[str, str]] = []
    if isinstance(topic_block, dict):
        raw = topic_block.get(word.lower())
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    q = str(item.get("question") or "").strip()
                    if q:
                        questions.append(
                            {
                                "question": q,
                                "type": str(item.get("type") or "either_or").strip(),
                                "answer": str(item.get("answer") or "").strip(),
                            }
                        )
                elif isinstance(item, str) and item.strip():
                    questions.append(
                        {
                            "question": item.strip(),
                            "type": "either_or",
                            "answer": "",
                        }
                    )
    if questions:
        return questions
    w = word.strip()
    if not w:
        return []
    article = "an" if w[:1].lower() in "aeiou" else "a"
    return [
        {
            "question": f"Do you see {article} {w} on the screen — yes or no?",
            "type": "yes_no",
            "answer": "yes",
        },
        {
            "question": f"Is {article} {w} big or small?",
            "type": "size",
            "answer": "big",
        },
    ]


def get_speaking_sentences(topic_slug: str, word: str) -> list[str]:
    """Return the practice-sentences list for a given (topic, word).

    Falls back to a tiny generic set if the JSON has no entry — speaking
    mode must never silently leave the LLM without something to model.
    """
    if not word:
        return []
    data = load_lesson_sentences() or {}
    if not isinstance(data, dict):
        return []
    topic_block = data.get((topic_slug or "").lower())
    sentences: list[str] = []
    if isinstance(topic_block, dict):
        raw = topic_block.get(word.lower())
        if isinstance(raw, list):
            sentences = [str(s).strip() for s in raw if isinstance(s, str) and s.strip()]
    if sentences:
        return sentences
    # Fallback: generic three-step ramp using just the word, so the speaking
    # flow keeps working even before someone curates sentences for a topic.
    w = word.strip()
    if not w:
        return []
    article = "an" if w[:1].lower() in "aeiou" else "a"
    return [
        f"I see {article} {w}.",
        f"The {w} is here.",
        f"I really like the {w} a lot.",
    ]


def reload_prompt_configs() -> None:
    """Clear the in-memory cache so files are re-read on next access."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Block builders (private helpers)
# ---------------------------------------------------------------------------

def _lesson_picture_sync_block(
    prompts: dict[str, Any], fixed_words: list[str], mode: str = ""
) -> str:
    if not fixed_words:
        return ""
    lps = prompts.get("lessonPictureSync")
    if not isinstance(lps, dict):
        return ""
    p = lps.get("prompt")
    if not isinstance(p, str) or not p.strip():
        return ""
    ex = lps.get("example")
    parts = [
        "## Lesson picture sync (required when using the fixed list)",
        p.strip(),
    ]
    if isinstance(ex, str) and ex.strip():
        parts.append(f"Example: {ex.strip()}")
    if mode == "quiz":
        parts.append(
            "Quiz mode: ask the EXACT curated questions from your live state (from "
            "lesson_quiz_questions.json) — do NOT invent questions. Each picture gets up to 2 "
            "questions. To switch to the next picture, SAY the next word from the fixed list aloud "
            "as a complete spoken word — the app syncs the picture automatically. NEVER ask about a "
            "lion while the child still sees a banana, and never mention tools or function names in speech."
        )
    elif mode == "vocabulary":
        parts.append(
            "Vocabulary mode: sync the picture when you speak each new word; take time to teach meaning "
            "before asking them to repeat — do not rush to the next word after one good pronunciation alone."
        )
    elif mode == "speaking":
        parts.append(
            "Speaking mode: short say-and-repeat rounds only; picture updates when you speak the next word. "
            "No long definitions — focus on clear pronunciation."
        )
    return "\n".join(parts) + "\n"


def _fixed_word_list_block(words: list[str], mode: str = "") -> str:
    if not words:
        return ""
    numbered = "\n".join(f"{i + 1}. {w}" for i, w in enumerate(words))
    quiz_extra = ""
    if mode == "quiz":
        quiz_extra = """
- **Quiz mode only:** The big picture on the child's screen always matches the **current** list word \
(same order as above). Every quiz question must be about **that** picture — colors, sounds, where it \
lives, what it does, silly either/or, size, or "what letter does it start with?". Do not ask about \
another word from the list until you have moved the on-screen picture to that word (use the lesson \
picture tools when you change words).
"""
    vocab_flow = ""
    speak_flow = ""
    if mode == "vocabulary":
        vocab_flow = """
- **Vocabulary mode only:** For EACH word, complete ALL steps before moving on: (1) say word with excitement,
  (2) simple meaning for a 4-year-old, (3) one example sentence, (4) ask them to say the word,
  (5) ONE quick comprehension check (yes/no or either/or about meaning — NOT pronunciation yet).
  Stay on this word until step 5 is done or they need one gentle repeat. Do NOT rush to the next word
  after a single good pronunciation.
"""
    elif mode == "speaking":
        speak_flow = """
- **Speaking practice only:** Do NOT give long lessons or definitions. Per word: point at the picture,
  say the word once, ask them to repeat 2–3 times with quick praise. One short sentence max per turn.
  Move on after a clear **correct** pronunciation (the app scores them). No comprehension quizzes.
"""
    return f"""
FIXED WORD LIST for this session (only use these as lesson vocabulary / quiz targets / speaking \
practice words; in this order):
{numbered}

Rules for this list:
- Do not introduce other English words as new teaching targets; stay on this list.
- Teach one word at a time in order.{vocab_flow}{speak_flow}
- In quiz mode, only ask about words from this list.
- If the child asks about something off-list, answer in one short sentence if helpful, then gently \
return to the current list word.
- After the last word, celebrate, then offer to revisit a favourite or end the lesson.
- The child's screen shows a large picture for the word they are on (when the lesson has images). \
Ask them to look at the picture, then connect it to the word.{quiz_extra}"""


def _format_pair_block(title: str, obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""
    p = obj.get("prompt")
    ex = obj.get("example")
    if not p and not ex:
        return ""
    lines = [f"### {title}"]
    if p:
        lines.append(f"Template: {p}")
    if ex:
        lines.append(f"Example: {ex}")
    return "\n".join(lines) + "\n"


def _build_scenario_playbook(prompts: dict[str, Any]) -> str:
    keys = [
        "lessonStartPrompt",
        "pronunciationEvaluationPrompt",
        "correctionPrompt",
        "retryPrompt",
        "correctResponsePrompt",
        "almostCorrectPrompt",
        "incorrectPrompt",
        "teachingBreakdownPrompt",
        "exampleSentencePrompt",
        "listeningModePrompt",
        "processingPrompt",
        "lessonTransitionPrompt",
        "lessonCompletionPrompt",
        "fallbackPrompt",
    ]
    parts = ["## Scenario templates (use when it fits the moment; paraphrase naturally)\n"]
    for k in keys:
        parts.append(_format_pair_block(k, prompts.get(k)))
    mls = prompts.get("multiLanguageSupport")
    if isinstance(mls, dict):
        parts.append(_format_pair_block("multiLanguageSupport", mls))
    av = prompts.get("avatarInstructionPrompts")
    if isinstance(av, dict):
        parts.append("## Delivery energy (match your voice to how the child did)\n")
        for mood, hint in av.items():
            parts.append(f"- {mood}: {hint}\n")
    return "\n".join(parts)


def _build_response_style_examples(templates: dict[str, Any]) -> str:
    if not templates:
        return ""

    def lines_for(key: str, label: str, max_items: int = 4) -> str:
        rows = templates.get(key)
        if not isinstance(rows, list):
            return ""
        out = [f"## Example lines — {label} (paraphrase; keep short)\n"]
        for row in rows[:max_items]:
            if isinstance(row, dict) and row.get("text"):
                em = row.get("emotion", "")
                out.append(f'- "{row["text"]}" ({em})\n')
        return "".join(out)

    parts = [
        lines_for("correctResponses", "when pronunciation is strong / correct"),
        lines_for("almostCorrectResponses", "when close but needs a nudge"),
        lines_for("incorrectResponses", "when they need gentle redo"),
        lines_for("teachingPrompts", "intro / modeling"),
        lines_for("retryPrompts", "retry encouragement"),
        lines_for("lessonTransitions", "moving to next word"),
        lines_for("lessonCompletion", "end of lesson"),
    ]
    sys_rows = templates.get("systemPrompts")
    if isinstance(sys_rows, list):
        parts.append("## Short system phrases (optional)\n")
        for row in sys_rows[:5]:
            if isinstance(row, dict) and row.get("text"):
                parts.append(f'- [{row.get("state", "state")}] "{row["text"]}"\n')
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Mode block builder — unified across vocabulary / speaking / quiz so each mode
# is loud about its identity, rules, and the things it will REFUSE to do. The
# personas and flows live in ``ai_prompts.json`` (vocabularyTeachingMode /
# speakingCoachMode / quizMode); the fallbacks here only kick in if the JSON
# is missing keys, so the agent never goes silent on a bad config.
# ---------------------------------------------------------------------------

_MODE_FALLBACKS: dict[str, dict[str, Any]] = {
    "vocabulary": {
        "json_key": "vocabularyTeachingMode",
        "headline": "LEARN VOCABULARY — TEACHING MODE (curious explorer)",
        "persona": (
            "You are an AI TEACHER for ages 5–8 — a curious, gentle storyteller. "
            "Your ONE job is to make the child UNDERSTAND each word."
        ),
        "flow": [
            "Reveal the word with excitement.",
            "Give a simple meaning in one short sentence.",
            "Add ONE tiny vivid detail or fun fact.",
            "Ask the child to repeat the word once.",
            "Ask ONE comprehension question and WAIT for their answer.",
        ],
        "forbidden": [
            "Do NOT advance to the next word until they answer the comprehension question.",
            "Do NOT score pronunciation strictly — meaning matters more than perfect sound here.",
            "Do NOT skip the meaning, fun fact, or example just to save time.",
        ],
    },
    "speaking": {
        "json_key": "speakingCoachMode",
        "headline": "SPEAKING PRACTICE — COACH MODE (energetic pronunciation drill)",
        "persona": (
            "You are a SPEAKING COACH — energetic, encouraging, and FAST. "
            "Your ONE job is to make the child speak each word loud, clear, and confident."
        ),
        "flow": [
            "Model a short kid-friendly sentence using the word.",
            "Ask the child to repeat — the app scores their attempt.",
            "If clear (≥90): big cheer, move to the next word.",
            "If close (70–89): one syllable-break tip, then 'say it again!'.",
            "If quiet/garbled (<70): 'good try! louder this time!' and re-model.",
            "After 3 attempts on one word, gently move on (no shame).",
        ],
        "forbidden": [
            "Do NOT teach what words mean — no definitions, no fun facts.",
            "Do NOT ask quiz-style questions like 'where does it live?'.",
            "Do NOT spell words letter-by-letter — only whole words or syllables.",
        ],
    },
    "quiz": {
        "json_key": "quizMode",
        "headline": "PICTURE QUIZ — GAME-SHOW MODE (curated Q&A from live state)",
        "persona": (
            "You are a SILLY GAME-SHOW HOST. Ask the EXACT curated questions from live state — "
            "never invent your own. Test what the child KNOWS about each picture."
        ),
        "flow": [
            "Cue the picture with showmanship.",
            "Ask the EXACT question from live state VERBATIM.",
            "React playfully — never call it 'wrong'; use the reference answer to hint.",
            "Optional second curated question (different type) if the first answer was fast.",
            "After at most 2 questions, say the next word aloud and ask the new picture's first question.",
        ],
        "forbidden": [
            "Do NOT invent your own questions — use EXACT ones from live state.",
            "Do NOT ask the child to PRONOUNCE the word — quiz mode does NOT score speech.",
            "Do NOT teach meanings or give long fun facts.",
            "Do NOT exceed 2 questions per picture.",
            "Do NOT ask the same question type twice in a row about the same picture.",
        ],
    },
}


def _build_mode_block(prompts: dict[str, Any], mode: str) -> str:
    """Return the mode-specific instructions block (vocabulary / speaking / quiz).

    Pulls ``persona`` / ``flow`` / ``forbidden`` / ``example`` from the matching
    ``ai_prompts.json`` block and falls back to baked-in defaults if anything is
    missing. Each block is shaped IDENTICALLY across modes so the LLM always
    sees: headline → persona → flow → hard rules → guiding example. Identical
    structure makes it easy for the model to swap modes without bleeding
    behaviours across them.
    """
    fb = _MODE_FALLBACKS.get(mode)
    if not fb:
        return ""
    cfg = prompts.get(fb["json_key"]) or {}
    if not isinstance(cfg, dict):
        cfg = {}

    headline = (cfg.get("headline") or "").strip() or fb["headline"]
    persona = (cfg.get("persona") or "").strip() or fb["persona"]
    flow = cfg.get("flow") if isinstance(cfg.get("flow"), list) else []
    if not flow:
        flow = fb["flow"]
    forbidden = cfg.get("forbidden") if isinstance(cfg.get("forbidden"), list) else []
    if not forbidden:
        forbidden = fb["forbidden"]
    example = (cfg.get("example") or "").strip()

    flow_lines = "\n".join(f"{i + 1}) {step}" for i, step in enumerate(flow))
    forbidden_lines = "\n".join(f"- {rule}" for rule in forbidden)

    parts = [
        f"\nMode: {headline}",
        f"Role for this mode: {persona}",
        "",
        "Per-turn flow (follow in order — short turns, child speaks between steps):",
        flow_lines,
    ]
    if forbidden_lines:
        parts += [
            "",
            "HARD RULES — never break these in this mode:",
            forbidden_lines,
        ]
    if example:
        parts += [
            "",
            f"Guiding example: {example}",
        ]
    return "\n".join(parts) + "\n"


def _build_pronunciation_policy(rules: dict[str, Any]) -> str:
    if not rules:
        return ""

    th = rules.get("scoreThresholds") or {}
    correct: int = th.get("correct", 90)
    almost: int = th.get("almostCorrect", 70)

    retry = rules.get("retryPolicy") or {}
    max_r: int = retry.get("maxRetries", 3)
    strategies = retry.get("retryStrategy") or []
    strat = ", ".join(str(s) for s in strategies) if strategies else "(see config)"

    corr = rules.get("aiCorrectionStrategy") or []
    corr_lines: list[str] = []
    for c in corr:
        if not isinstance(c, dict):
            continue
        # Fix: safe unpack — handles missing, None, empty, or short scoreRange lists
        score_range = c.get("scoreRange") or []
        lo: int = score_range[0] if len(score_range) > 0 else 0
        hi: int = score_range[1] if len(score_range) > 1 else 0
        if lo == 0 and hi == 0:
            logger.warning("aiCorrectionStrategy entry has invalid/missing scoreRange: %s", c)
        act = c.get("action", "")
        corr_lines.append(f"- Score {lo}–{hi}: {act}")

    abm = rules.get("avatarBehaviorMapping") or {}
    ab_lines: list[str] = []
    for band, spec in abm.items():
        if isinstance(spec, dict):
            ab_lines.append(
                f"- {band}: emotion={spec.get('emotion')}, animation intent={spec.get('animation')}"
            )

    return f"""
## Pronunciation feedback policy (from curriculum config)
- Treat score >= {correct} as strong success (praise, then continue when ready).
- Treat score {almost}–{correct - 1} as almost: one clear tip, then retry.
- Below {almost}: teach slowly, break into chunks, then retry.
- Prefer at most {max_r} focused retries per word before you simplify or move on; \
strategies to try in order: {strat}.
{chr(10).join(corr_lines)}
Avatar tone mapping (voice should match):
{chr(10).join(ab_lines)}
When judging pronunciation without a numeric score, use the same spirit: celebrate clear success, \
gentle correction when close, patient teaching when not.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_kid_tutor_instructions(
    mode: str,
    topic: str,
    tutor_name: str,
    tutor_hint: str,
    fixed_words: list[str],
) -> str:
    """
    Build a complete system-prompt string for the kid tutor agent.

    Parameters
    ----------
    mode        : "vocabulary" | "speaking" | "quiz" | ""
    topic       : Lesson theme shown to the tutor (e.g. "Animals & Fruits")
    tutor_name  : Display name of the AI tutor character (e.g. "Luna")
    tutor_hint  : One-line character description injected after the name
    fixed_words : Ordered vocab list for this session; empty list = no fixed list
    """
    prompts = load_ai_prompts()
    templates = load_response_templates()
    pron_rules = load_pronunciation_rules()

    sp = prompts.get("systemPersonality") or {}
    role: str = sp.get("role", "You are a friendly English tutor for young children.")
    tone = sp.get("tone") or []
    tone_s = ", ".join(tone) if isinstance(tone, list) else str(tone)
    pers_rules = sp.get("rules") or []
    conv_rules = prompts.get("conversationRules") or []

    personality_block = (
        f"## Who you are\n"
        f"Your name is {tutor_name}. {tutor_hint}\n"
        f"Ground role (adapt fully to your name and character above): {role}\n"
        f"Tone to keep: {tone_s}.\n\n"
        f"Personality rules:\n"
        + "\n".join(f"- {r}" for r in pers_rules if isinstance(r, str))
        + f"\n\nConversation rules (strict):\n"
        + "\n".join(f"- {r}" for r in conv_rules if isinstance(r, str))
        + "\n"
    )

    # Tight voice-call rules — every line earns its tokens. Anything that
    # repeats personality_block / mode_block / fixed_block has been removed
    # so the prompt fits in <1500 tokens (was ~3400). This keeps GPT's TTFT
    # under ~1.5s instead of 6-7s on every turn.
    voice_block = (
        "## Voice rules (live call with a 3–7-year-old)\n"
        "- Replies are 1–2 short sentences in simple words. Never shame; "
        "never say \"wrong\".\n"
        "- Always encourage effort (\"Good try!\", \"Nice listening!\"). "
        f"Refer to yourself ONLY as {tutor_name}.\n"
        f"- Lesson theme: {topic}.\n"
        "- For pronunciation tips, break into syllable chunks like "
        "\"EL-E-PHANT\" — NEVER letter-by-letter (no R-O-A-R or C-A-T).\n"
        "\n"
        "## Session opening\n"
        "Wait for the child's first reply before starting the lesson. "
        "Acknowledge what they said in one warm line, then move into the "
        "first practice word. Never ask \"Are you ready?\".\n"
        "\n"
        "## Pronunciation vs chat\n"
        "Only judge pronunciation when YOU just asked for a specific word "
        "AND the audio sounds like it. Greetings/small talk/off-topic → "
        "reply conversationally in one short line, then steer back. If "
        "unsure, ask: \"Did you mean to say {word}?\".\n"
    )

    # NOTE: ``_build_scenario_playbook`` (~5 KB) and
    # ``_build_response_style_examples`` (~1.5 KB) used to be appended here.
    # They were dropped to cut LLM TTFT from ~7 s to <2 s — the templates
    # were redundant with personality_block / mode_block / voice_block.
    # Re-enable by setting KID_TUTOR_PROMPT_VERBOSE=1 (debug only).
    verbose = (os.getenv("KID_TUTOR_PROMPT_VERBOSE", "0") or "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
    playbook = _build_scenario_playbook(prompts) if (verbose and prompts) else ""
    examples = _build_response_style_examples(templates) if (verbose and templates) else ""
    policy = _build_pronunciation_policy(pron_rules) if pron_rules else ""
    fixed_block = _fixed_word_list_block(fixed_words, mode)
    picture_sync_block = _lesson_picture_sync_block(prompts, fixed_words, mode)

    mode_block = _build_mode_block(prompts, mode)

    sections = [
        personality_block,
        voice_block,
        policy,
        playbook,
        examples,
        fixed_block,
        picture_sync_block,
        mode_block,
    ]
    return "\n".join(s for s in sections if s.strip())
