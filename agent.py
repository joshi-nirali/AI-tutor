"""bitHuman Essence avatar agent -- cloud-hosted (no local models needed).

Kid tutor flow: the browser joins a room named like
  kidtutor-{mode}-{topic}-{tutor}-{sessionId}
e.g. kidtutor-vocabulary-animals-leo-a1b2c3d4
The {tutor} slug (e.g. leo, luna) sets the AI's name and short character note.

Data channel topic ``kidtutor`` (JSON):
  - Client → agent: {"type":"lesson_index","index":<int>,"topicSlug":<str>}
  - Agent → client: lesson_index_ack, pronunciation_result (may include ``avatarCue``), lesson_set_index

Optional env:
  KID_TUTOR_USE_AVATAR — default ``1``. Set ``0`` to run voice-only (no bitHuman ``AvatarSession``); then
  ``BITHUMAN_AGENT_ID`` is not required.
  USE_BITUMAN_AVATAR — alias for ``KID_TUTOR_USE_AVATAR`` if the latter is unset.
  KID_TUTOR_SCORING_REPLY — default ``1``. If set to ``0``, skip interrupt+generate_reply after scoring
  (only data + instruction refresh).
  KID_TUTOR_INTERRUPT_TIMEOUT — seconds to wait for interrupt during scoring reply (default ``0.85``, max ``8``).
  KID_TUTOR_AUTO_ADVANCE_ON_CORRECT — default ``0``. If ``1``, after a ``correct`` pronunciation band the
  lesson index advances one step and the UI is synced (after the scoring reply).

Usage:
    python agent.py dev        # local dev; pair with token_server + npm start
    python agent.py start      # production worker
"""

import asyncio
import json
import logging
import os
import re

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomOutputOptions,
    RunContext,
    UserInputTranscribedEvent,
    WorkerOptions,
    WorkerType,
    cli,
    function_tool,
)
from livekit.plugins import bithuman, openai, silero

from curriculum import words_for_topic
from kid_lesson_session import KidLessonSession
import pronunciation_score
from prompt_config import (
    build_kid_tutor_instructions,
    load_ai_prompts,
    load_pronunciation_rules,
)
from tutor_session_utils import avatar_cue_for_band, use_bithuman_avatar

KID_TUTOR_DATA_TOPIC = "kidtutor"

logger = logging.getLogger("bithuman-agent")
logger.setLevel(logging.INFO)

load_dotenv()

_ROOM_RE = re.compile(
    r"^kidtutor-(?P<mode>vocabulary|speaking|quiz)-(?P<topic>[a-z0-9_]+)-(?P<tutor>[a-z][a-z0-9_]{0,14})-(?P<sess>[a-zA-Z0-9]+)$"
)

# Slug from room name -> (display name, optional character hint for the LLM)
TUTOR_FROM_SLUG: dict[str, tuple[str, str]] = {
    "leo": ("Leo", "You are a friendly lion who loves stories and exciting words."),
    "luna": ("Luna", "You are a gentle, patient owl who loves helping with sounds."),
}


def parse_room(room_name: str) -> tuple[str, str, str, str, str]:
    """Return (mode, topic_phrase, topic_slug, tutor_name, tutor_hint). topic_slug keys word_lists.json."""
    m = _ROOM_RE.match((room_name or "").strip().lower())
    if not m:
        fb = os.getenv("TUTOR_NAME", "Leo")
        return "vocabulary", "fun everyday things", "", fb, "You are a warm, playful animal tutor."
    mode = m.group("mode")
    topic_slug = m.group("topic")
    topic = topic_slug.replace("_", " ")
    slug = m.group("tutor")
    if slug in TUTOR_FROM_SLUG:
        tutor_name, tutor_hint = TUTOR_FROM_SLUG[slug]
    else:
        tutor_name = slug.replace("_", " ").title()
        tutor_hint = "You are a warm, playful animal tutor for young children."
    return mode, topic, topic_slug, tutor_name, tutor_hint


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    await ctx.wait_for_participant()

    use_avatar = use_bithuman_avatar()
    avatar_id = (os.getenv("BITHUMAN_AGENT_ID") or "").strip()
    if use_avatar and not avatar_id:
        raise ValueError(
            "Set BITHUMAN_AGENT_ID in your .env file (or set KID_TUTOR_USE_AVATAR=0 for voice-only). "
            "Create an agent at https://www.bithuman.ai or via api/generation.py"
        )

    room_name = getattr(ctx.room, "name", "") or ""
    mode, topic, topic_slug, tutor_name, tutor_hint = parse_room(room_name)
    fixed_words = words_for_topic(topic_slug)
    base_instructions = build_kid_tutor_instructions(
        mode, topic, tutor_name, tutor_hint, fixed_words
    )
    pron_rules = load_pronunciation_rules()
    score_thresholds = pron_rules.get("scoreThresholds") or {}
    retry_policy = pron_rules.get("retryPolicy") or {}

    lesson = KidLessonSession(
        words=fixed_words,
        max_retries=int(retry_policy.get("maxRetries", 3)),
    )
    lesson.set_topic_slug(topic_slug)

    def full_instructions() -> str:
        return base_instructions + lesson.instruction_suffix()

    instruction_lock = asyncio.Lock()

    ap_ver = load_ai_prompts().get("version", "?")
    pr_ver = pron_rules.get("version", "?")

    logger.info(
        "Prompt packs ai_prompts=%s pronunciation_rules=%s",
        ap_ver,
        pr_ver,
    )
    logger.info(
        "Cloud Essence mode -- use_avatar=%s avatar_id=%s room=%s mode=%s topic=%s tutor=%s words=%d",
        use_avatar,
        avatar_id or "(none)",
        room_name,
        mode,
        topic,
        tutor_name,
        len(fixed_words),
    )
    if topic_slug and not fixed_words:
        logger.warning("No fixed word list for topic_slug=%s (check data/word_lists.json)", topic_slug)

    avatar = None
    if use_avatar:
        avatar = bithuman.AvatarSession(
            avatar_id=avatar_id,
            api_secret=os.getenv("BITHUMAN_API_SECRET"),
        )

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            voice=os.getenv("OPENAI_VOICE", "coral"),
            model="gpt-4o-mini-realtime-preview",
        ),
        vad=silero.VAD.load(),
    )

    async def publish_tutor_json(payload: dict) -> None:
        try:
            lp = ctx.room.local_participant
            await lp.publish_data(
                json.dumps(payload).encode("utf-8"),
                topic=KID_TUTOR_DATA_TOPIC,
                reliable=True,
            )
        except Exception as e:
            logger.warning("publish_data failed: %s", e)

    scoring_reply_enabled = os.getenv("KID_TUTOR_SCORING_REPLY", "1").lower() in (
        "1",
        "true",
        "yes",
    )

    def _interrupt_timeout_s() -> float:
        raw = os.getenv("KID_TUTOR_INTERRUPT_TIMEOUT", "0.85").strip()
        try:
            v = float(raw)
        except ValueError:
            v = 0.85
        return max(0.15, min(v, 8.0))

    interrupt_timeout_s = _interrupt_timeout_s()
    if scoring_reply_enabled:
        logger.info("Pronunciation reply: interrupt timeout=%.2fs", interrupt_timeout_s)

    auto_advance_on_correct = os.getenv(
        "KID_TUTOR_AUTO_ADVANCE_ON_CORRECT", "0"
    ).strip().lower() in ("1", "true", "yes", "on")
    if auto_advance_on_correct:
        logger.info("Auto-advance lesson picture on correct pronunciation: enabled")

    lesson_tools: list = []
    if fixed_words:

        @function_tool(
            description=(
                "Advance one step in the lesson word list and sync the child's picture. "
                "Call when you move to the next vocabulary word in order."
            )
        )
        async def go_to_next_lesson_word(_ctx: RunContext) -> str:
            if not lesson.words:
                return "No vocabulary list in this lesson."
            last = len(lesson.words) - 1
            if lesson.word_index >= last:
                return "Already on the last word — celebrate or wrap up."
            lesson.set_word_index(lesson.word_index + 1)
            await publish_tutor_json(
                {
                    "type": "lesson_set_index",
                    "topicSlug": topic_slug,
                    "index": lesson.word_index,
                }
            )
            await refresh_agent_instructions()
            w = lesson.expected_word() or "n/a"
            return f"Advanced to index {lesson.word_index} (word: {w})."

        @function_tool(
            description=(
                "Set the child's picture carousel to this 0-based index in the lesson word list "
                "when you jump to a specific word or go back."
            )
        )
        async def sync_lesson_picture_index(_ctx: RunContext, word_index: int) -> str:
            lesson.set_word_index(int(word_index))
            await publish_tutor_json(
                {
                    "type": "lesson_set_index",
                    "topicSlug": topic_slug,
                    "index": lesson.word_index,
                }
            )
            await refresh_agent_instructions()
            w = lesson.expected_word() or "n/a"
            return f"Picture synced to index {lesson.word_index} (word: {w})."

        lesson_tools.append(go_to_next_lesson_word)
        lesson_tools.append(sync_lesson_picture_index)

    kid_agent = Agent(instructions=full_instructions(), tools=lesson_tools)

    async def refresh_agent_instructions() -> None:
        async with instruction_lock:
            await kid_agent.update_instructions(full_instructions())

    async def handle_final_transcript(text: str) -> None:
        if mode not in ("vocabulary", "speaking"):
            return
        if pronunciation_score.should_skip_scoring(text):
            return
        expected = lesson.expected_word()
        if not expected:
            return
        result = pronunciation_score.score_utterance(expected, text, score_thresholds)
        meta = lesson.record_score(result["score"], result["band"], result["best_token"])
        cue = avatar_cue_for_band(pron_rules, result["band"])
        pr_payload: dict = {
            "type": "pronunciation_result",
            "topicSlug": topic_slug,
            "wordIndex": lesson.word_index,
            "expected": expected,
            "said": text,
            "bestToken": result["best_token"],
            "score": result["score"],
            "band": result["band"],
            "retries": meta["retries"],
            "maxRetries": meta["max_retries"],
            "maxedOut": meta["maxed_out"],
        }
        if cue:
            pr_payload["avatarCue"] = cue
        await publish_tutor_json(pr_payload)
        if scoring_reply_enabled:
            try:
                await asyncio.wait_for(
                    session.interrupt(force=False),
                    timeout=interrupt_timeout_s,
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug("pronunciation interrupt: %s", e)
        await refresh_agent_instructions()
        if scoring_reply_enabled:
            try:
                cue_hint = ""
                if cue:
                    cue_hint = (
                        f" Voice energy hint for this turn: {cue.get('emotion', '')} tone, "
                        f"{cue.get('animation', '')} body language (express in voice; UI may show cues)."
                    )
                session.generate_reply(
                    instructions=(
                        f"Pronunciation check just ran. Target word: \"{expected}\". "
                        f"Child transcript: \"{text}\". "
                        f"Score {result['score']}/100, band: {result['band']}. "
                        f"Failed attempts since last success on this word: {meta['retries']} "
                        f"(max {meta['max_retries']}). "
                        "Give ONE short spoken reply (1–2 sentences) for a 3–7 year old; "
                        "follow your tutor personality; do not lecture."
                        f"{cue_hint}"
                    ),
                )
            except Exception as e:
                logger.warning("generate_reply after pronunciation: %s", e)

        if auto_advance_on_correct and result["band"] == "correct" and lesson.words:
            last = len(lesson.words) - 1
            if lesson.word_index < last:
                lesson.set_word_index(lesson.word_index + 1)
                await publish_tutor_json(
                    {
                        "type": "lesson_set_index",
                        "topicSlug": topic_slug,
                        "index": lesson.word_index,
                        "reason": "auto_advance_on_correct",
                    }
                )
                await refresh_agent_instructions()
                logger.info(
                    "auto-advanced lesson to index %s after correct pronunciation",
                    lesson.word_index,
                )

        logger.info(
            "pronunciation score=%s band=%s expected=%s best_token=%s retries=%s",
            result["score"],
            result["band"],
            expected,
            result["best_token"],
            meta["retries"],
        )

    async def handle_room_data(dp: rtc.DataPacket) -> None:
        if (dp.topic or "") != KID_TUTOR_DATA_TOPIC:
            return
        if dp.participant is None:
            return
        try:
            msg = json.loads(dp.data.decode("utf-8"))
        except Exception:
            return
        if msg.get("type") != "lesson_index":
            return
        if msg.get("topicSlug") and str(msg["topicSlug"]).lower() != topic_slug.lower():
            return
        raw_idx = msg.get("index")
        if raw_idx is None:
            return
        try:
            idx_int = int(raw_idx)
        except (TypeError, ValueError):
            return
        lesson.set_word_index(idx_int)
        await publish_tutor_json(
            {
                "type": "lesson_index_ack",
                "topicSlug": topic_slug,
                "index": lesson.word_index,
            }
        )
        await refresh_agent_instructions()
        logger.info("lesson index from UI: %s (topic=%s)", lesson.word_index, topic_slug)

    def _on_user_input_transcribed(ev: UserInputTranscribedEvent) -> None:
        if not ev.is_final:
            return
        t = (ev.transcript or "").strip()
        if not t:
            return
        asyncio.create_task(handle_final_transcript(t))

    def _on_data_received(dp: rtc.DataPacket) -> None:
        asyncio.create_task(handle_room_data(dp))

    session.on("user_input_transcribed", _on_user_input_transcribed)
    ctx.room.on("data_received", _on_data_received)

    if avatar is not None:
        await avatar.start(session, room=ctx.room)
    else:
        logger.info("Starting session without bitHuman avatar pipeline")

    await session.start(
        agent=kid_agent,
        room=ctx.room,
        room_output_options=RoomOutputOptions(audio_enabled=False),
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            worker_type=WorkerType.ROOM,
            job_memory_warn_mb=1500,
            num_idle_processes=1,
        )
    )
