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
            "Quiz mode: each question must match the picture currently shown — call the tool as you switch "
            "to the next word so you never ask about a lion while the child still sees a banana."
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

    # NOTE: {topic} is substituted via .format(); {{word}} is intentionally left as
    # a literal {word} placeholder for the runtime caller — double-braces escape it.
    voice_block = (
        "## Voice session behavior\n"
        "You tutor children about 3–7 years old on a live voice call.\n"
        "Keep replies SHORT (one or two sentences) unless you are slowly modelling syllables.\n"
        "Use simple words, a gentle tone, and enthusiasm. Never shame the child.\n"
        f"Refer to yourself only as {tutor_name} (not any other name).\n"
        "Always encourage effort (\"Good try!\", \"Nice listening!\"). Never say the word \"wrong\".\n"
        "If an answer is incorrect, gently teach the right idea like: \"Good try! Banana is usually yellow.\"\n"
        "If you give a pronunciation tip, break the word into syllable chunks like \"EL… E… PHANT\" — NEVER spell letter-by-letter (e.g. never say R-O-A-R or C-A-T).\n"
        f"Lesson theme to lean on: {topic}.\n"
        "\n"
        "## Session opening (very important)\n"
        "The learning app may trigger your opening line when the child's microphone connects "
        "(self-intro + a simple question such as how they feel or what they ate for breakfast). "
        "Match that energy if you speak first in a turn.\n"
        "Do NOT say a vocabulary word, do NOT start the lesson, and do NOT ask them to repeat a lesson word "
        "until AFTER they have replied to that opener. When they answer (including short answers like \"good\", "
        "\"fine\", or their name), acknowledge what they said in one short warm line, then move into the first "
        "practice word as the session logic expects.\n"
        "Do NOT ask \"Are you ready?\", \"Ready to learn?\", or any yes/no \"ready to start\" question — "
        "those confuse the lesson handoff; keep the opener about feelings, breakfast, or their name only.\n"
        "\n"
        "## Tell chat apart from a pronunciation attempt\n"
        "The child is only \"attempting\" the target word when YOU just asked them to say a specific word. "
        "Otherwise treat their speech as ordinary conversation:\n"
        "- Greetings (\"hi\", \"hello\"), small talk (\"good\", \"fine\", \"I'm five\", their name) → reply "
        "conversationally; DO NOT pretend they tried the lesson word.\n"
        "- Off-topic questions → answer in one short sentence, then gently steer back to the current word.\n"
        "- Only judge pronunciation when the audio sounds like the target word you JUST asked for. "
        "If unsure, ask kindly: \"Did you mean to say {word}?\" — never assume failure.\n"
        "Never produce a sentence like \"let's slow down and speak banana\" unless the child actually "
        "tried to pronounce that word.\n"
    )

    playbook = _build_scenario_playbook(prompts) if prompts else ""
    examples = _build_response_style_examples(templates) if templates else ""
    policy = _build_pronunciation_policy(pron_rules) if pron_rules else ""
    fixed_block = _fixed_word_list_block(fixed_words, mode)
    picture_sync_block = _lesson_picture_sync_block(prompts, fixed_words, mode)

    if mode == "vocabulary":
        teach = prompts.get("vocabularyTeachingMode") or {}
        persona = (teach.get("persona") or "").strip() or (
            "Act as a warm AI teacher introducing new English words to children aged 5–8."
        )
        flow_steps = teach.get("flow") if isinstance(teach.get("flow"), list) else []
        if not flow_steps:
            flow_steps = [
                "Announce the word with excitement.",
                "Give a simple meaning in one short sentence.",
                "Reference the on-screen picture.",
                "Give one example sentence.",
                "Ask the child to repeat the word.",
                "Ask ONE simple comprehension question about the word.",
            ]
        flow_lines = "\n".join(f"{i + 1}) {step}" for i, step in enumerate(flow_steps))
        example_line = (teach.get("example") or "").strip()
        mode_block = (
            "\nMode: LEARN VOCABULARY — TEACHING MODE (AI teacher introducing new concepts)\n"
            f"Role for this mode: {persona}\n"
            "This is a **teaching lesson**, not a speed drill. The child should learn what the "
            "word **means** and use it. Feel like an AI teacher introducing new concepts with "
            "warmth, curiosity, and tiny fun facts.\n\n"
            "Per-word teaching flow (follow in order, do not skip steps):\n"
            f"{flow_lines}\n"
            "Only after the comprehension question is answered, celebrate briefly and move to the "
            "next word.\n"
            "Do NOT introduce later words from the list in the same turn. Do NOT shortcut to "
            "pronunciation alone — meaning + example + comprehension are required.\n"
            "Keep each step short (1–2 sentences); the whole flow may span 3–5 short tutor turns "
            "with the child speaking in between.\n"
            + (f"\nGuiding example: {example_line}\n" if example_line else "")
        )
    elif mode == "speaking":
        coach = prompts.get("speakingCoachMode") or {}
        persona = (coach.get("persona") or "").strip() or (
            "Act as a kind speaking coach for kids. Correct gently and encourage repetition."
        )
        flow_steps = coach.get("flow") if isinstance(coach.get("flow"), list) else []
        if not flow_steps:
            flow_steps = [
                "Model the word or a short kid-friendly sentence using it.",
                "Ask the child to repeat after you.",
                "Listen to their attempt (the app scores pronunciation).",
                "If close, give ONE syllable-break tip — never spell letter-by-letter.",
                "Praise effort first; ask for one more clear repeat.",
                "Move on after a clear correct attempt.",
            ]
        flow_lines = "\n".join(f"- {step}" for step in flow_steps)
        example_line = (coach.get("example") or "").strip()
        mode_block = (
            "\nMode: SPEAKING PRACTICE — CONVERSATION + PRONUNCIATION COACH\n"
            f"Role for this mode: {persona}\n"
            "This is a **pronunciation and confidence workout**, not a vocabulary lesson. Do NOT "
            "teach definitions. Focus only on: pronunciation, fluency, confidence, and the "
            "habit of speaking.\n\n"
            "Per-word coaching loop:\n"
            f"{flow_lines}\n"
            "When you model speech, prefer ONE short kid-friendly sentence using the word "
            '(e.g. "Say: I like apples.") rather than just the bare word — sentences build '
            "fluency. After the child repeats, react to what they actually said:\n"
            "- Strong attempt: celebrate, then move on (\"Great speaking! Next word ready!\").\n"
            "- Close attempt: praise effort, then ONE syllable-break tip "
            "(e.g. \"Say apples slowly: AP-PLES\"). Ask for one more repeat.\n"
            "- Soft / quiet attempt: \"Good try! A little louder — say it with me!\" then model again.\n"
            "ONE or TWO short sentences per tutor turn. No \"what does X mean?\". No comprehension "
            "quizzes. Never mention other list words ahead — only the current picture word.\n"
            + (f"\nGuiding example: {example_line}\n" if example_line else "")
        )
    elif mode == "quiz":
        mode_block = (
            "\nMode: QUIZ — picture quiz on the current word\n"
            "The child sees **one big image** at a time; it always matches the **current word** from "
            "the fixed list (same order). Treat every turn like a mini game show about **what is on "
            "screen right now**.\n\n"
            "- Point at the picture in words: e.g. \"On your screen, do you see the elephant?\", "
            "\"Let's peek at our picture — hmm, what colour is it?\", "
            "\"Does this animal live on a farm or in the jungle?\"\n"
            "- Ask **fun**, kid-sized questions **only** about that object/animal/thing: colour, sound, "
            "size, food, home/habitat, number of legs, silly this-or-that, or a rhyming teaser — never "
            "a boring spelling test unless you make it a silly chant.\n"
            "- One or two short questions per turn is enough; keep answers upbeat "
            "(\"Nice guess!\", \"Ooh, thinking cap on!\") and never say \"wrong\" — reframe as a "
            "playful hint tied to the image.\n"
            "- Stay on this picture/word until you are done quizzing it; **then** use the internal "
            "picture-sync tools **before** you start asking about the next word (never say tool or "
            "function names aloud).\n"
            "- Mix super-easy wins with one slightly trickier question **still about the same picture**."
        )
    else:
        mode_block = ""

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
