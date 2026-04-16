"""Load kid-tutor prompt JSON (ai_prompts, response_templates, pronunciation_rules) and build agent instructions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_PROMPTS_DIR: Path | None = None
_CACHE: dict[str, Any] = {}


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


def _load_json(filename: str) -> dict[str, Any]:
    if filename in _CACHE:
        return _CACHE[filename]
    path = prompts_dir() / filename
    if not path.is_file():
        _CACHE[filename] = {}
        return _CACHE[filename]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    _CACHE[filename] = data if isinstance(data, dict) else {}
    return _CACHE[filename]


def load_ai_prompts() -> dict[str, Any]:
    return _load_json("ai_prompts.json")


def load_response_templates() -> dict[str, Any]:
    return _load_json("response_templates.json")


def load_pronunciation_rules() -> dict[str, Any]:
    return _load_json("pronunciation_rules.json")


def reload_prompt_configs() -> None:
    _CACHE.clear()


def _lesson_picture_sync_block(prompts: dict[str, Any], fixed_words: list[str]) -> str:
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
    return "\n".join(parts) + "\n"


def _fixed_word_list_block(words: list[str]) -> str:
    if not words:
        return ""
    numbered = "\n".join(f"{i + 1}. {w}" for i, w in enumerate(words))
    return f"""

FIXED WORD LIST for this session (only use these as lesson vocabulary / quiz targets / speaking practice words; in this order):
{numbered}

Rules for this list:
- Do not introduce other English words as new teaching targets; stay on this list.
- Teach one word at a time in order: introduce → simple meaning → short example → ask them to say it → one quick check. Then move on unless they need a repeat.
- In quiz and speaking modes, only ask about words from this list.
- If the child asks about something off-list, answer in one short sentence if helpful, then gently return to the current list word.
- After the last word, celebrate, then offer to revisit a favorite or end the lesson.
- The child's screen shows a large picture for the word they are on (when the lesson has images). Ask them to look at the picture, then connect it to the word (e.g. "This is a banana — can you say banana?")."""


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

    def lines_for(
        key: str,
        label: str,
        max_items: int = 4,
    ) -> str:
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
    correct = th.get("correct", 90)
    almost = th.get("almostCorrect", 70)
    retry = rules.get("retryPolicy") or {}
    max_r = retry.get("maxRetries", 3)
    strategies = retry.get("retryStrategy") or []
    strat = ", ".join(str(s) for s in strategies) if strategies else "(see config)"
    corr = rules.get("aiCorrectionStrategy") or []
    corr_lines = []
    for c in corr:
        if isinstance(c, dict):
            lo, hi = (c.get("scoreRange") or [0, 0])[:2]
            act = c.get("action", "")
            corr_lines.append(f"- Score {lo}-{hi}: {act}")
    abm = rules.get("avatarBehaviorMapping") or {}
    ab_lines = []
    for band, spec in abm.items():
        if isinstance(spec, dict):
            ab_lines.append(
                f"- {band}: emotion={spec.get('emotion')}, animation intent={spec.get('animation')}"
            )
    return f"""
## Pronunciation feedback policy (from curriculum config)
- Treat score >= {correct} as strong success (praise, then continue when ready).
- Treat score {almost}-{correct - 1} as almost: one clear tip, then retry.
- Below {almost}: teach slowly, break into chunks, then retry.
- Prefer at most {max_r} focused retries per word before you simplify or move on; strategies to try in order: {strat}.
{chr(10).join(corr_lines)}
Avatar tone mapping (voice should match):
{chr(10).join(ab_lines)}
When judging pronunciation without a numeric score, use the same spirit: celebrate clear success, gentle correction when close, patient teaching when not.
"""


def build_kid_tutor_instructions(
    mode: str,
    topic: str,
    tutor_name: str,
    tutor_hint: str,
    fixed_words: list[str],
) -> str:
    prompts = load_ai_prompts()
    templates = load_response_templates()
    pron_rules = load_pronunciation_rules()

    sp = prompts.get("systemPersonality") or {}
    role = sp.get("role", "You are a friendly English tutor for young children.")
    tone = sp.get("tone") or []
    tone_s = ", ".join(tone) if isinstance(tone, list) else str(tone)
    pers_rules = sp.get("rules") or []
    conv_rules = prompts.get("conversationRules") or []

    personality_block = f"""## Who you are
Your name is {tutor_name}. {tutor_hint}
Ground role (adapt fully to your name and character above): {role}
Tone to keep: {tone_s}.

Personality rules:
{chr(10).join(f"- {r}" for r in pers_rules if isinstance(r, str))}

Conversation rules (strict):
{chr(10).join(f"- {r}" for r in conv_rules if isinstance(r, str))}
"""

    voice_block = """## Voice session behavior
You tutor children about 3–7 years old on a live voice call.
Keep replies SHORT (one or two sentences) unless you are slowly modeling syllables.
As soon as you can, say one cheerful greeting (one sentence) so the child knows you are there; then listen for them.
Use simple words, a gentle tone, and enthusiasm. Never shame the child.
Refer to yourself only as {name} (not any other name).
Always encourage effort ("Good try!", "Nice listening!"). Never say the word "wrong".
If an answer is incorrect, gently teach the right idea like: "Good try! Banana is usually yellow."
If you give a pronunciation tip, break the word into clear chunks like "EL… E… PHANT" when helpful.
Lesson theme to lean on: {topic}.
""".format(name=tutor_name, topic=topic)

    playbook = _build_scenario_playbook(prompts) if prompts else ""
    examples = _build_response_style_examples(templates) if templates else ""
    policy = _build_pronunciation_policy(pron_rules) if pron_rules else ""

    fixed_block = _fixed_word_list_block(fixed_words)
    picture_sync_block = _lesson_picture_sync_block(prompts, fixed_words)

    if mode == "vocabulary":
        mode_block = """

Mode: LEARN VOCABULARY
Follow this flow in order when starting or when the child seems ready for a new word:
1) Say the new word clearly with excitement.
2) Explain what it means in very simple language.
3) Give one short example sentence.
4) Ask the child to say the word; listen and give gentle pronunciation help if needed.
5) Ask one easy yes/no or choice question to check understanding.

Stay on kid-friendly words related to the lesson theme."""
    elif mode == "speaking":
        mode_block = """

Mode: SPEAKING PRACTICE
Focus on pronunciation and repeating.
- Say a word or short phrase; ask the child to repeat.
- If it is close, celebrate; if not, model slowly in chunks and ask them to try again.
- Keep turns quick and game-like."""
    elif mode == "quiz":
        mode_block = """

Mode: QUIZ
- Ask short questions (choices are easier than open-ended).
- After an answer, say if it is correct in a fun way, or encourage and teach.
- Mix easy wins with one slightly harder question."""
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
