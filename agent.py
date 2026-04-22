"""bitHuman Essence avatar agent -- cloud-hosted (no local models needed).

Kid tutor flow: the browser joins a room named like
  kidtutor-{mode}-{topic}-{tutor}-{sessionId}
e.g. kidtutor-vocabulary-animals-leo-a1b2c3d4
The {tutor} slug (e.g. leo, luna) sets the AI's name and short character note.

Data channel topic ``kidtutor`` (JSON):
  - Client → agent: {"type":"lesson_index","index":<int>,"topicSlug":<str>}
  - Agent → client: lesson_index_ack, pronunciation_result (may include ``avatarCue``), lesson_set_index

Optional env:
  OPENAI_REALTIME_MODEL — default ``gpt-realtime`` (GA Realtime name). Keys often lack ``gpt-4o-realtime-preview``
  or legacy preview IDs; wrong value → ``model_not_found`` and no speech (BitHuman may never attach reliably).
  LIVEKIT_AGENT_ICE_TRANSPORT — optional ``relay`` or ``nohost`` for the Python worker WebRTC stack; try ``relay``
  if logs show ``Publisher pc state failed`` / ``publisher connection: timeout`` on strict firewalls.
  KID_TUTOR_PRE_CONNECT_AUDIO — default ``1``. Set ``0`` to start the voice session without waiting for buffered
  pre-connect mic audio (slightly snappier join; may clip the child's first syllable).
  KID_TUTOR_AGENT_NOISE_FILTER — set ``1`` with ``pip install livekit-plugins-noise-cancellation`` to run LiveKit
  BVC on the agent-side mic stream (stronger than browser-only suppression; do not stack with heavy client DSP).
  KID_TUTOR_USE_AVATAR — default ``1``. Set ``0`` to run voice-only (no bitHuman ``AvatarSession``); then
  ``BITHUMAN_AGENT_ID`` is not required.
  USE_BITUMAN_AVATAR — alias for ``KID_TUTOR_USE_AVATAR`` if the latter is unset.
  KID_TUTOR_SCORING_REPLY — default ``1``. If set to ``0``, skip interrupt+generate_reply after scoring
  (only data + instruction refresh).
  KID_TUTOR_INTERRUPT_TIMEOUT — seconds to wait for interrupt during scoring reply (default ``0.85``, max ``8``).
  KID_TUTOR_AUTO_ADVANCE_ON_CORRECT — default ``1``. After a ``correct`` pronunciation band the lesson
  index advances one step, the UI picture is synced, and the tutor's celebration reply already introduces
  the next word in the same turn. Set ``0`` to require manual UI/data-channel advancement.
  KID_TUTOR_MIN_ATTEMPT_SCORE — default ``40``. Transcripts whose best similarity to the target word is
  below this score are treated as conversation (not a pronunciation attempt) and pass through to the LLM
  without scripted scoring feedback.

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
    RunContext,
    UserInputTranscribedEvent,
    WorkerOptions,
    WorkerType,
    cli,
    function_tool,
)
from livekit.agents.voice.room_io import AudioInputOptions, RoomOptions
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


def _livekit_agent_rtc_configuration() -> rtc.RtcConfiguration | None:
    """Optional WebRTC tuning for the worker (see LIVEKIT_AGENT_ICE_TRANSPORT in .env.example)."""
    raw = (os.getenv("LIVEKIT_AGENT_ICE_TRANSPORT") or "").strip().lower()
    if not raw or raw in ("all", "default", "auto"):
        return None
    if raw == "relay":
        logger.info("LiveKit worker ICE transport: relay (TURN-friendly)")
        return rtc.RtcConfiguration(ice_transport_type=rtc.IceTransportType.TRANSPORT_RELAY)
    if raw in ("nohost", "no-host", "nonhost"):
        logger.info("LiveKit worker ICE transport: nohost")
        return rtc.RtcConfiguration(ice_transport_type=rtc.IceTransportType.TRANSPORT_NOHOST)
    logger.warning(
        "Ignoring unknown LIVEKIT_AGENT_ICE_TRANSPORT=%r (use relay, nohost, or all)",
        os.getenv("LIVEKIT_AGENT_ICE_TRANSPORT"),
    )
    return None


def _kid_room_audio_input_options() -> AudioInputOptions:
    """Mic path into OpenAI Realtime: optional BVC + pre-connect buffering."""
    raw_pre = (os.getenv("KID_TUTOR_PRE_CONNECT_AUDIO", "1") or "1").strip().lower()
    pre_connect = raw_pre not in ("0", "false", "no", "off")
    nc = None
    if (os.getenv("KID_TUTOR_AGENT_NOISE_FILTER", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        try:
            from livekit.plugins import noise_cancellation as _lk_nc  # type: ignore import-not-found

            nc = _lk_nc.BVC()
            logger.info("Agent input noise filter: livekit.plugins.noise_cancellation.BVC")
        except ImportError:
            logger.warning(
                "KID_TUTOR_AGENT_NOISE_FILTER is enabled but noise_cancellation plugin is missing — "
                "pip install livekit-plugins-noise-cancellation"
            )
    return AudioInputOptions(pre_connect_audio=pre_connect, noise_cancellation=nc)


async def _ensure_room_connected(room: rtc.Room, *, timeout_s: float = 45.0) -> None:
    """Wait until the room is CONN_CONNECTED (avoids early stream_bytes / publish on flaky ICE)."""
    if room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
        return
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()

    def on_cs(state: int) -> None:
        if state == rtc.ConnectionState.CONN_CONNECTED and not fut.done():
            fut.set_result(None)
        elif state == rtc.ConnectionState.CONN_DISCONNECTED and not fut.done():
            fut.set_exception(RuntimeError("room disconnected before connection stabilized"))

    room.on("connection_state_changed", on_cs)
    try:
        if room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            return
        try:
            await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "room not CONN_CONNECTED after %.0fs (state=%s); continuing — "
                "if you see publisher timeouts, set LIVEKIT_AGENT_ICE_TRANSPORT=relay",
                timeout_s,
                room.connection_state,
            )
    finally:
        room.off("connection_state_changed", on_cs)


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
    await ctx.connect(rtc_config=_livekit_agent_rtc_configuration())
    await ctx.wait_for_participant()
    await _ensure_room_connected(ctx.room)

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

    realtime_model = (os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime") or "gpt-realtime").strip()
    logger.info("OpenAI Realtime model: %s", realtime_model)

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            voice=os.getenv("OPENAI_VOICE", "coral"),
            model=realtime_model,
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
        "KID_TUTOR_AUTO_ADVANCE_ON_CORRECT", "1"
    ).strip().lower() in ("1", "true", "yes", "on")
    if auto_advance_on_correct:
        logger.info("Auto-advance lesson picture on correct pronunciation: enabled")
    else:
        logger.info("Auto-advance lesson picture on correct pronunciation: disabled")

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

    min_attempt_score = max(
        0,
        min(
            100,
            int(os.getenv("KID_TUTOR_MIN_ATTEMPT_SCORE", "40").strip() or "40"),
        ),
    )

    async def handle_final_transcript(text: str) -> None:
        if mode not in ("vocabulary", "speaking"):
            return
        if pronunciation_score.should_skip_scoring(text):
            return
        expected = lesson.expected_word()
        if not expected:
            return
        # Only score if it actually looks like an attempt at the lesson word.
        # Conversational chat / questions are passed straight to the LLM untouched.
        if pronunciation_score.looks_like_chat(text):
            logger.debug("transcript looks conversational, skipping pronunciation scoring: %r", text)
            return
        result = pronunciation_score.score_utterance(expected, text, score_thresholds)
        if result["score"] < min_attempt_score:
            logger.debug(
                "low-similarity transcript (%s vs expected=%s, score=%s) — treating as chat",
                result["best_token"],
                expected,
                result["score"],
            )
            return
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

        # Decide whether to advance BEFORE we craft the spoken reply, so the
        # celebration sentence can flow straight into introducing the next word
        # and the agent's instruction context already reflects the new target.
        advanced = False
        next_word: str | None = None
        is_last_word = False
        if auto_advance_on_correct and result["band"] == "correct" and lesson.words:
            last_idx = len(lesson.words) - 1
            if lesson.word_index < last_idx:
                lesson.set_word_index(lesson.word_index + 1)
                next_word = lesson.expected_word()
                await publish_tutor_json(
                    {
                        "type": "lesson_set_index",
                        "topicSlug": topic_slug,
                        "index": lesson.word_index,
                        "reason": "auto_advance_on_correct",
                    }
                )
                advanced = True
                logger.info(
                    "auto-advanced lesson to index %s (next word: %s) after correct pronunciation",
                    lesson.word_index,
                    next_word,
                )
            else:
                is_last_word = True

        await refresh_agent_instructions()

        if scoring_reply_enabled:
            try:
                cue_hint = ""
                if cue:
                    cue_hint = (
                        f" Voice energy hint for this turn: {cue.get('emotion', '')} tone, "
                        f"{cue.get('animation', '')} body language (express in voice; UI may show cues)."
                    )
                if advanced and next_word:
                    transition = (
                        f" Then in the SAME short turn, smoothly move on to the next word "
                        f"\"{next_word}\": say it once clearly and ask the child to try it. "
                        "Do not pause for confirmation between the praise and the new word — "
                        "keep it as one upbeat 1–2 sentence reply."
                    )
                elif is_last_word and result["band"] == "correct":
                    transition = (
                        " This was the LAST word in the list — celebrate the whole lesson "
                        "warmly in 1–2 sentences and ask if they want to play again."
                    )
                else:
                    transition = ""
                session.generate_reply(
                    instructions=(
                        f"Pronunciation check just ran. Target word: \"{expected}\". "
                        f"Child transcript: \"{text}\". "
                        f"Score {result['score']}/100, band: {result['band']}. "
                        f"Failed attempts since last success on this word: {meta['retries']} "
                        f"(max {meta['max_retries']}). "
                        "Give ONE short spoken reply (1–2 sentences) for a 3–7 year old; "
                        "follow your tutor personality; do not lecture."
                        f"{transition}"
                        f"{cue_hint}"
                    ),
                )
            except Exception as e:
                logger.warning("generate_reply after pronunciation: %s", e)

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
        logger.info("BitHuman avatar pipeline started (expect remote identity bithuman-avatar-agent)")
    else:
        logger.info("Starting session without bitHuman avatar pipeline")

    await session.start(
        agent=kid_agent,
        room=ctx.room,
        room_options=RoomOptions(
            audio_output=False,
            audio_input=_kid_room_audio_input_options(),
        ),
    )

    greeting_sent = False
    greeting_lock = asyncio.Lock()

    def _is_avatar_identity(identity: str) -> bool:
        ident = (identity or "").lower()
        return ident.startswith("bithuman") or "avatar" in ident

    async def _send_greeting(reason: str) -> None:
        nonlocal greeting_sent
        async with greeting_lock:
            if greeting_sent:
                return
            greeting_sent = True
        try:
            session.generate_reply(
                instructions=(
                    f"Open the session as {tutor_name}. Speak ONE warm sentence introducing "
                    "yourself by name and welcoming the child to learning time. Then ONE short "
                    "opener question (their name, how they feel, or favorite colour). Do NOT "
                    "mention any vocabulary word and do NOT ask them to repeat anything yet. "
                    "Wait for them to reply."
                ),
            )
            logger.info("Sent proactive greeting prompt to kid tutor session (trigger=%s)", reason)
        except Exception as e:
            logger.warning("initial greeting generate_reply failed: %s", e)

    def _on_track_published(
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if greeting_sent:
            return
        if _is_avatar_identity(participant.identity):
            return
        if publication.kind != rtc.TrackKind.KIND_AUDIO:
            return
        asyncio.create_task(_send_greeting("kid_mic_published"))

    ctx.room.on("track_published", _on_track_published)

    # If the kid already published a mic track before we attached the listener, greet now.
    for participant in ctx.room.remote_participants.values():
        if _is_avatar_identity(participant.identity):
            continue
        for pub in participant.track_publications.values():
            if pub.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.create_task(_send_greeting("kid_mic_already_present"))
                break
        if greeting_sent:
            break

    async def _greeting_fallback() -> None:
        await asyncio.sleep(8.0)
        if not greeting_sent:
            await _send_greeting("fallback_timer")

    asyncio.create_task(_greeting_fallback())


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            worker_type=WorkerType.ROOM,
            job_memory_warn_mb=1500,
            num_idle_processes=1,
        )
    )
