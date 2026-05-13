"""bitHuman Essence avatar agent -- cloud-hosted (no local models needed).

Kid tutor flow: the browser joins a room named like
  kidtutor-{mode}-{topic}-{tutor}-{sessionId}
e.g. kidtutor-vocabulary-animals-leo-a1b2c3d4
The {tutor} slug (e.g. leo, luna) sets the AI's name and short character note.

Data channel topic ``kidtutor`` (JSON):
  - Client → agent: {"type":"lesson_index","index":<int>,"topicSlug":<str>}
  - Client → agent: {"type":"child_profile","childName":<str>,"topicSlug":<str>} — name from home screen (sent when the lesson connects; multiple kids / sessions)
  - Agent → client: lesson_index_ack, pronunciation_result (may include ``avatarCue``), lesson_set_index,
    input_speech_started (first child speech detected on STT path — UI may hide setup loader)

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
  BitHuman agent IDs are not required.
  BITHUMAN_AGENT_ID — default BitHuman agent for all tutors (see ``_bithuman_agent_id_for_tutor``).
  BITHUMAN_AGENT_ID_<SLUG> — optional per-tutor override (e.g. ``BITHUMAN_AGENT_ID_LEO``) so each
  character in the UI can use a different bithuman.ai agent ID.
  USE_BITUMAN_AVATAR — alias for ``KID_TUTOR_USE_AVATAR`` if the latter is unset.
  KID_TUTOR_SCORING_REPLY — default ``1``. If set to ``0``, skip interrupt+generate_reply after scoring
  (only data + instruction refresh).
  KID_TUTOR_INTERRUPT_TIMEOUT — seconds to wait for interrupt during scoring reply (default ``0.85``, max ``8``).
  KID_TUTOR_AUTO_ADVANCE_ON_CORRECT — default ``1``. After a ``correct`` pronunciation band the lesson
  index advances one step, the UI picture is synced, and the tutor's celebration reply already introduces
  the next word in the same turn. Set ``0`` to require manual UI/data-channel advancement.
  KID_TUTOR_DEFER_PICTURE_UNTIL_RESPONSE — default ``1``. With auto-advance on, the picture index moves only
  after the child's next utterance (silence alone never advances). Set ``0`` for immediate picture sync on
  each correct score (legacy behavior).
  KID_TUTOR_MIN_ATTEMPT_SCORE — default ``40``. Transcripts whose best similarity to the target word is
  below this score are treated as conversation (not a pronunciation attempt) and pass through to the LLM
  without scripted scoring feedback.
  KID_TUTOR_POST_INTRO_SCORING_DELAY_S — after the greeting→first-word handoff, ignore pronunciation
  scoring for this many seconds (default ``5``) so spurious STT does not fake a ``correct`` and advance.
  KID_TUTOR_POST_ADVANCE_SCORING_DELAY_S — same idea after each auto-advance celebration (default ``3.5``).

Usage:
    python agent.py dev        # local dev; pair with token_server + npm start
    python agent.py start      # production worker
"""

import asyncio
import json
import logging
import os
import re
import time

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

# Matches LiveKit identity from TutorRoom: child-{alphanumericSlug}-{sessionSuffix}
_CHILD_IDENTITY_PREFIX_RE = re.compile(r"^child-([a-zA-Z0-9]{1,24})-", re.I)


def _child_display_name_from_room(room: rtc.Room) -> str:
    """Child name from token ``name`` (see token_server) or ``identity`` ``child-…``."""
    try:
        participants = list(room.remote_participants.values())
    except Exception:
        return ""
    for p in participants:
        ident = str(getattr(p, "identity", "") or "")
        il = ident.lower()
        if "bithuman" in il or "avatar" in il or il.startswith("agent-"):
            continue
        display = str(getattr(p, "name", "") or "").strip()
        if display and display.lower() not in ("friend", "participant"):
            return display[:80]
        m = _CHILD_IDENTITY_PREFIX_RE.match(ident)
        if m:
            slug = m.group(1)
            if slug and slug.lower() != "friend":
                return slug[:80]
    return ""


# Slug from room name -> per-tutor config (display name, OpenAI voice, character hint).
# Add a new tutor by appending an entry here — agent.py will pick up its voice + persona.
# Voice precedence at runtime (see _voice_for_tutor): OPENAI_VOICE_<SLUG> env override
# → this "voice" field → global OPENAI_VOICE env → "coral".
TUTOR_FROM_SLUG: dict[str, dict[str, str]] = {
    "leo": {
        "name": "Leo",
        "voice": "ballad",
        "hint": (
            "You are Leo, a bouncy, enthusiastic lion cub who gets SUPER excited about every new word! "
            "You roar with joy when kids get things right and gasp dramatically when introducing new words. "
            "You speak with big theatrical energy like a cartoon show host. "
            "You love to make silly sounds, celebrate with 'ROAAAR! That was AMAZING!', and use funny "
            "comparisons kids love ('Elephant is HUGE — bigger than a hundred pizzas!'). "
            "You have a silly side — you sometimes pretend to forget things so the child can correct you. "
            "You love giving high-fives and saying things like 'You and me are the BEST team!'. "
            "When a child struggles, you get softer and say 'Hey, no worries buddy, let's figure this out together.' "
            "You occasionally share mini fun-facts about animals since you are a lion."
        ),
    },
    # "luna": {
    #     "name": "Luna",
    #     "voice": "shimmer",
    #     "hint": (
    #         "You are Luna, a wise but playful owl who speaks with gentle wonder and curiosity. "
    #         "You make soft 'ooo' and 'aaa' sounds when amazed and whisper excitedly for suspense. "
    #         "You celebrate with a cheerful 'Hoo-hoo-hooray!'. "
    #         "You love starlight, bedtime stories, and magical things. You weave tiny stories around words "
    #         "('Did you know butterflies taste with their FEET? How silly is that!'). "
    #         "You are patient and never rush — if a child struggles you say 'Take your time little one, "
    #         "Luna is right here with you.' You love to count stars together as a reward after each word. "
    #         "You sometimes act surprised in a funny way: 'Wait… did YOU just say that perfectly?! "
    #         "I think my feathers just ruffled from excitement!'"
    #     ),
    # },
    "cub": {
        "name": "Cub",
        "voice": "coral",
        "hint": (
            "You are Cub, a sweet young lion cub who is curious and encouraging. "
            "You speak in a warm, clear voice — excited but not overwhelming for little kids. "
            "You celebrate wins with short happy sounds ('Yes! You got it!') and gentle fist-pumps in your tone. "
            "When they stumble you stay patient: 'Let's try that sound together — you've got this.' "
            "You love silly comparisons and tiny pretend games around each word. "
            "Keep sentences short and friendly for ages 3–7."
        ),
    },
}

# Fallback voice when neither the tutor config nor any env var sets one.
_DEFAULT_TUTOR_VOICE = "coral"


def _voice_for_tutor(tutor_slug: str) -> str:
    """Resolve the OpenAI Realtime voice for a tutor.

    Precedence (highest first):
      1. Per-tutor env override ``OPENAI_VOICE_<SLUG>`` (e.g. ``OPENAI_VOICE_LEO``)
         — lets ops swap voices without redeploy / code edits.
      2. ``TUTOR_FROM_SLUG[slug]["voice"]`` — the tutor's curated default.
      3. Global ``OPENAI_VOICE`` env — backwards compatible with the old single-voice setup.
      4. Hardcoded ``coral``.
    """
    slug = (tutor_slug or "").strip().lower()
    if slug:
        per_tutor_env = os.getenv(f"OPENAI_VOICE_{slug.upper()}", "").strip()
        if per_tutor_env:
            return per_tutor_env
        cfg = TUTOR_FROM_SLUG.get(slug)
        if cfg and cfg.get("voice"):
            return cfg["voice"]
    return (os.getenv("OPENAI_VOICE", "") or "").strip() or _DEFAULT_TUTOR_VOICE


def _bithuman_agent_id_for_tutor(tutor_slug: str) -> str:
    """Resolve the BitHuman cloud agent ID for this tutor session.

    Precedence (highest first):
      1. ``BITHUMAN_AGENT_ID_<SLUG>`` (e.g. ``BITHUMAN_AGENT_ID_LEO``) — bind each
         picker avatar (lion cub, owl, …) to its own agent created at bithuman.ai.
      2. ``BITHUMAN_AGENT_ID`` — single shared avatar for all tutors (legacy).
    """
    slug = (tutor_slug or "").strip().lower()
    if slug:
        per = os.getenv(f"BITHUMAN_AGENT_ID_{slug.upper()}", "").strip()
        if per:
            return per
    return (os.getenv("BITHUMAN_AGENT_ID", "") or "").strip()


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


def parse_room(room_name: str) -> tuple[str, str, str, str, str, str]:
    """Return (mode, topic_phrase, topic_slug, tutor_slug, tutor_name, tutor_hint).

    ``topic_slug`` keys ``word_lists.json``; ``tutor_slug`` keys ``TUTOR_FROM_SLUG``
    (and is what ``_voice_for_tutor`` uses to pick the OpenAI voice).
    """
    m = _ROOM_RE.match((room_name or "").strip().lower())
    if not m:
        fb_name = os.getenv("TUTOR_NAME", "Leo")
        fb_slug = fb_name.strip().lower().replace(" ", "_")
        return (
            "vocabulary",
            "fun everyday things",
            "",
            fb_slug,
            fb_name,
            "You are a warm, playful animal tutor.",
        )
    mode = m.group("mode")
    topic_slug = m.group("topic")
    topic = topic_slug.replace("_", " ")
    slug = m.group("tutor")
    cfg = TUTOR_FROM_SLUG.get(slug)
    if cfg:
        tutor_name = cfg["name"]
        tutor_hint = cfg["hint"]
    else:
        tutor_name = slug.replace("_", " ").title()
        tutor_hint = "You are a warm, playful animal tutor for young children."
    return mode, topic, topic_slug, slug, tutor_name, tutor_hint


async def entrypoint(ctx: JobContext):
    await ctx.connect(rtc_config=_livekit_agent_rtc_configuration())
    await ctx.wait_for_participant()
    await _ensure_room_connected(ctx.room)

    use_avatar = use_bithuman_avatar()

    room_name = getattr(ctx.room, "name", "") or ""
    mode, topic, topic_slug, tutor_slug, tutor_name, tutor_hint = parse_room(room_name)

    avatar_id = _bithuman_agent_id_for_tutor(tutor_slug)
    if use_avatar and not avatar_id:
        slug_hint = tutor_slug or "leo"
        raise ValueError(
            "Set BITHUMAN_AGENT_ID in your .env file, or set a per-tutor ID such as "
            f"BITHUMAN_AGENT_ID_{slug_hint.upper()}=… for this tutor (slug from the LiveKit room name). "
            "Create agents at https://www.bithuman.ai. "
            "Alternatively set KID_TUTOR_USE_AVATAR=0 for voice-only."
        )
    fixed_words = words_for_topic(topic_slug)
    base_instructions = build_kid_tutor_instructions(
        mode, topic, tutor_name, tutor_hint, fixed_words
    )
    # Participant ``name`` from the token sometimes attaches shortly after join — brief pause helps.
    await asyncio.sleep(0.35)
    child_display_name = _child_display_name_from_room(ctx.room)
    # Authoritative default when the React app publishes ``child_profile`` on connect (multi-kid same device).
    child_name_from_app = ""

    pron_rules = load_pronunciation_rules()
    score_thresholds = pron_rules.get("scoreThresholds") or {}
    retry_policy = pron_rules.get("retryPolicy") or {}

    lesson = KidLessonSession(
        words=fixed_words,
        max_retries=int(retry_policy.get("maxRetries", 3)),
    )
    lesson.set_topic_slug(topic_slug)

    # Until the agent has explicitly handed off greeting → lesson, never score the
    # child's speech. Otherwise the kid's casual reply to "What's your favourite
    # colour?" gets matched against word_index 0 (e.g. "blue") and auto-advances
    # the picture before they ever practiced the first word. See transition_into_lesson.
    lesson_started = False

    # Ignore pronunciation scoring until ``time.monotonic()`` passes this value.
    # Realtime STT often emits bogus finals right after the tutor speaks (echo from
    # speakers, noise, or duplicate segments). Those can fuzzy-match the lesson
    # word → false "correct" → auto-advance while the child never spoke.
    scoring_mute_until: float | None = None

    def _post_intro_scoring_mute_s() -> float:
        raw = (os.getenv("KID_TUTOR_POST_INTRO_SCORING_DELAY_S", "5.0") or "5.0").strip()
        try:
            v = float(raw)
        except ValueError:
            v = 5.0
        return max(0.0, min(v, 30.0))

    def _post_advance_scoring_mute_s() -> float:
        raw = (os.getenv("KID_TUTOR_POST_ADVANCE_SCORING_DELAY_S", "3.5") or "3.5").strip()
        try:
            v = float(raw)
        except ValueError:
            v = 3.5
        return max(0.0, min(v, 20.0))

    def _extend_scoring_mute(seconds: float) -> None:
        nonlocal scoring_mute_until
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        if scoring_mute_until is None or deadline > scoring_mute_until:
            scoring_mute_until = deadline
            logger.debug("pronunciation scoring muted for %.1fs (anti-spurious-STT window)", seconds)

    def child_identity_instruction_suffix() -> str:
        """Teach the model how to address the learner; supports changing kids on one device."""
        default = (child_name_from_app.strip() or (child_display_name or "").strip()).strip()
        lines = [
            "",
            "## Learner name (same device may be used by different children)",
            "**Trust order:** (1) If the child clearly states their name or nickname (e.g. \"I'm Leo\", \"call me Jo\"), "
            "**always** use exactly what they said from then on — repeat it back correctly once so they know you heard them. "
            "(2) The name sent from the learning app for this session (child_profile). "
            "(3) Room join metadata, if present.",
            "Never invent, rhyme, or substitute a different name (e.g. do not turn \"Tom\" into \"Zen\" or similar). "
            "If speech-to-text might be wrong, still try to mirror the sounds they used; you may ask once gently to confirm.",
        ]
        if default:
            lines.append(
                f'Default name from the app for this session: "{default}". '
                "Override immediately if they introduce themselves differently."
            )
        return "\n".join(lines) + "\n"

    def full_instructions() -> str:
        return (
            base_instructions
            + child_identity_instruction_suffix()
            + lesson.instruction_suffix()
        )

    instruction_lock = asyncio.Lock()

    ap_ver = load_ai_prompts().get("version", "?")
    pr_ver = pron_rules.get("version", "?")

    logger.info(
        "Prompt packs ai_prompts=%s pronunciation_rules=%s",
        ap_ver,
        pr_ver,
    )
    tutor_voice = _voice_for_tutor(tutor_slug)
    logger.info(
        "Cloud Essence mode -- use_avatar=%s avatar_id=%s room=%s mode=%s topic=%s tutor=%s voice=%s words=%d child_name=%r",
        use_avatar,
        avatar_id or "(none)",
        room_name,
        mode,
        topic,
        tutor_name,
        tutor_voice,
        len(fixed_words),
        child_display_name or "",
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
            voice=tutor_voice,
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

    input_speech_started_sent = False

    async def publish_input_speech_started_once() -> None:
        """Tell the browser once STT has seen real speech — aligns with LiveKit input pipeline warmup."""
        nonlocal input_speech_started_sent
        if input_speech_started_sent:
            return
        input_speech_started_sent = True
        await publish_tutor_json(
            {"type": "input_speech_started", "topicSlug": topic_slug}
        )
        logger.info("Published input_speech_started (child speech reached agent/STT)")

    scoring_reply_enabled = os.getenv("KID_TUTOR_SCORING_REPLY", "1").lower() in (
        "1",
        "true",
        "yes",
    )

    def _interrupt_timeout_s() -> float:
        # Default bumped from 0.85s → 2.0s: with LIVEKIT_AGENT_ICE_TRANSPORT=relay
        # the bitHuman avatar's clear-buffer RPC routinely needs >1s to flush over
        # TURN. A timeout that's too tight means we proceed to generate_reply
        # while the previous reply is still draining, causing audible cut-outs,
        # "speech not done in time after interruption" errors, and the avatar
        # audio queue overflowing.
        raw = os.getenv("KID_TUTOR_INTERRUPT_TIMEOUT", "2.0").strip()
        try:
            v = float(raw)
        except ValueError:
            v = 2.0
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

    defer_picture_until_response = os.getenv(
        "KID_TUTOR_DEFER_PICTURE_UNTIL_RESPONSE", "1"
    ).strip().lower() in ("1", "true", "yes", "on")
    if auto_advance_on_correct and defer_picture_until_response:
        logger.info(
            "Deferred picture sync: image advances only after the child speaks (not on silence); "
            "set KID_TUTOR_DEFER_PICTURE_UNTIL_RESPONSE=0 for instant advance after each correct"
        )

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
                "when you jump to a specific word or go back. Safe to call: it is a no-op if the "
                "picture is already at this index, so it will not double-advance after an "
                "auto-advance from the scoring pipeline."
            )
        )
        async def sync_lesson_picture_index(_ctx: RunContext, word_index: int) -> str:
            requested = max(0, min(int(word_index), max(len(lesson.words) - 1, 0)))
            # Don't re-publish or rebuild instructions when the LLM asks us to set
            # the index to where we already are. The previous behavior caused an
            # extra lesson_set_index round-trip on every "correct" turn (LLM
            # echoing the auto-advance) which thrashed the bitHuman avatar
            # pipeline and produced audible cut-outs.
            if requested == lesson.word_index:
                w = lesson.expected_word() or "n/a"
                return f"Already at index {lesson.word_index} (word: {w}); no change."
            lesson.set_word_index(requested)
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

    async def transition_into_lesson(reason: str) -> None:
        """Hand off from the greeting to the actual lesson, introducing word 0.

        Called the first time the child speaks after the greeting. Flips
        ``lesson_started`` so subsequent transcripts are eligible for scoring.
        Without this, the child's greeting reply (e.g. "blue" in answer to
        "What's your favourite colour?") would be scored against word 0,
        often hit ``correct``, and silently advance the picture.
        """
        nonlocal lesson_started
        if lesson_started:
            return
        lesson_started = True
        if mode not in ("vocabulary", "speaking"):
            return
        if not lesson.words:
            return
        expected = lesson.expected_word()
        if not expected:
            return
        try:
            session.generate_reply(
                instructions=(
                    "Acknowledge what the child just said in ONE short, warm sentence in "
                    "your tutor voice (no name-asking, no new opener question). "
                    "If they just told you their name or how to address them, repeat that name back **exactly** "
                    "in that sentence (match what they said, not a different name). "
                    f"Then introduce the FIRST lesson word \"{expected}\": say it once, "
                    "clearly and slowly, and invite them to try saying it. "
                    "Keep the whole reply to 2 short sentences. "
                    "Do NOT skip past this word — wait for them to attempt it before moving on."
                ),
            )
            # Block scoring for a few seconds: STT often emits a junk "final" right
            # after the model speaks, which can match the target word and auto-advance.
            _extend_scoring_mute(_post_intro_scoring_mute_s())
            logger.info(
                "Transitioned greeting → first lesson word (trigger=%s, word=%s)",
                reason,
                expected,
            )
        except Exception as e:
            logger.warning("transition_into_lesson generate_reply failed: %s", e)

    async def handle_final_transcript(text: str) -> None:
        if mode not in ("vocabulary", "speaking"):
            return
        if pronunciation_score.should_skip_scoring(text):
            # Tiny ack words ("yes", "ok", ...) shouldn't trigger the lesson handoff
            # either — wait for a real utterance from the child.
            return
        if not lesson_started:
            # First real utterance after the greeting. Treat it as the greeting reply,
            # never as a pronunciation attempt, and use it to launch the lesson.
            # BUT: if the child is just saying "Yep I'm ready" / "Yes" / "I'm ready"
            # in response to a (forbidden but sometimes generated) readiness question,
            # do NOT treat that as the trigger — wait for a real conversational reply.
            expected_word_0 = lesson.expected_word() if lesson.words else ""
            if pronunciation_score.looks_like_readiness_acknowledgment(text, expected_word_0):
                logger.debug(
                    "greeting readiness reply — not triggering lesson transition: %r", text
                )
                return
            await transition_into_lesson("first_child_utterance")
            return
        if scoring_mute_until is not None:
            _now = time.monotonic()
            if _now < scoring_mute_until:
                logger.debug(
                    "pronunciation scoring suppressed (%.2fs left in anti-spurious STT window): %r",
                    scoring_mute_until - _now,
                    text,
                )
                return

        # Waiting for any real utterance before syncing the picture to the next word after
        # a correct score (see deferred advance below).
        if lesson.pending_advance_to_index is not None:
            pidx = lesson.pending_advance_to_index
            if lesson.words and 0 <= pidx < len(lesson.words):
                next_w = lesson.words[pidx]
                if pronunciation_score.looks_like_readiness_acknowledgment(text, next_w):
                    lesson.apply_pending_advance()
                    await publish_tutor_json(
                        {
                            "type": "lesson_set_index",
                            "topicSlug": topic_slug,
                            "index": lesson.word_index,
                            "reason": "deferred_advance_readiness",
                        }
                    )
                    await refresh_agent_instructions()
                    logger.info(
                        "deferred picture advance applied (readiness) → index %s",
                        lesson.word_index,
                    )
                    return
                if pronunciation_score.should_skip_scoring(text):
                    return
                if pronunciation_score.looks_like_chat(text):
                    # Still counts as "they answered" — sync picture so the tutor can react without
                    # mis-scoring chat as the vocabulary token.
                    lesson.apply_pending_advance()
                    await publish_tutor_json(
                        {
                            "type": "lesson_set_index",
                            "topicSlug": topic_slug,
                            "index": lesson.word_index,
                            "reason": "deferred_advance_chat",
                        }
                    )
                    await refresh_agent_instructions()
                    logger.debug(
                        "deferred picture advance applied (conversational reply): %r",
                        text,
                    )
                    return
                lesson.apply_pending_advance()
                await publish_tutor_json(
                    {
                        "type": "lesson_set_index",
                        "topicSlug": topic_slug,
                        "index": lesson.word_index,
                        "reason": "deferred_advance_attempt",
                    }
                )
                await refresh_agent_instructions()
                logger.info(
                    "deferred picture advance applied (attempt) → index %s; scoring same utterance",
                    lesson.word_index,
                )
            else:
                lesson.pending_advance_to_index = None

        if pronunciation_score.should_skip_scoring(text):
            return
        expected = lesson.expected_word()
        if not expected:
            return
        # Don't score as pronunciation when the child is answering meta prompts such as
        # "Are you ready?" — plain "yes"/"ok" are handled by should_skip_scoring; phrases
        # like "yes I'm ready" used to reach score_utterance and could mis-trigger advances.
        if pronunciation_score.looks_like_readiness_acknowledgment(text, expected):
            logger.debug(
                "readiness/meta reply — skipping pronunciation scoring: %r",
                text,
            )
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

        # Decide whether to advance BEFORE we craft the spoken reply, so the
        # celebration sentence can flow straight into introducing the next word
        # and the agent's instruction context already reflects the new target.
        advanced = False
        deferred_next_intro = False
        next_word: str | None = None
        is_last_word = False
        if auto_advance_on_correct and result["band"] == "correct" and lesson.words:
            last_idx = len(lesson.words) - 1
            if lesson.word_index < last_idx:
                if defer_picture_until_response:
                    lesson.pending_advance_to_index = lesson.word_index + 1
                    next_word = lesson.words[lesson.pending_advance_to_index]
                    deferred_next_intro = True
                    _extend_scoring_mute(_post_advance_scoring_mute_s())
                    logger.info(
                        "deferring picture to index %s until child speaks (next word: %s)",
                        lesson.pending_advance_to_index,
                        next_word,
                    )
                else:
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
                    _extend_scoring_mute(_post_advance_scoring_mute_s())
                    logger.info(
                        "auto-advanced lesson to index %s (next word: %s) after correct pronunciation",
                        lesson.word_index,
                        next_word,
                    )
            else:
                is_last_word = True

        await refresh_agent_instructions()

        # Only force a hard interrupt + scripted reply when we MUST redirect the
        # agent's speech — i.e. we just auto-advanced to a new word, finished
        # the last word, or the child has maxed out retries and we want to
        # gracefully skip on. Otherwise let the OpenAI Realtime model's natural
        # turn-taking respond using the freshly refreshed instructions.
        #
        # Why: the previous "interrupt + generate_reply on every transcript"
        # flow caused audible cut-outs and avatar-pipeline thrash. Each manual
        # interrupt fires a clear-buffer RPC to the bitHuman avatar; over the
        # TURN-relay path that RPC frequently times out (>5s), and meanwhile we
        # were already queuing the next reply on top of the half-flushed audio.
        # Net effect: the user hears the agent stutter / drop syllables and
        # sometimes lose audio entirely after a few rounds.
        needs_scripted_reply = (
            scoring_reply_enabled
            and (advanced or deferred_next_intro or is_last_word or meta["maxed_out"])
        )
        if needs_scripted_reply:
            try:
                await asyncio.wait_for(
                    session.interrupt(force=False),
                    timeout=interrupt_timeout_s,
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug("pronunciation interrupt: %s", e)
            # Tiny breath so the OpenAI Realtime stream is fully cancelled
            # before we queue the new reply. Without this, the realtime API can
            # process the new reply before the cancellation lands, and the
            # avatar gets two overlapping audio segments.
            await asyncio.sleep(0.05)

            try:
                cue_hint = ""
                if cue:
                    cue_hint = (
                        f" Voice energy hint for this turn: {cue.get('emotion', '')} tone, "
                        f"{cue.get('animation', '')} body language (express in voice; UI may show cues)."
                    )
                if deferred_next_intro and next_word:
                    transition = (
                        f" They pronounced \"{expected}\" correctly. The next word is \"{next_word}\". "
                        "IMPORTANT: The child's picture still shows the previous word until they speak — "
                        f"celebrate briefly, then invite them to try \"{next_word}\". "
                        "If they have not answered yet, do NOT skip ahead — ask again in a fun, "
                        f'encouraging way for "{next_word}". '
                        "Do NOT call go_to_next_lesson_word or sync_lesson_picture_index; the app moves "
                        "the picture when the child responds."
                    )
                elif advanced and next_word:
                    transition = (
                        f" Then in the SAME short turn, smoothly move on to the next word "
                        f"\"{next_word}\": say it once clearly and ask the child to try it. "
                        "Do not pause for confirmation between the praise and the new word — "
                        "keep it as one upbeat 1–2 sentence reply."
                    )
                elif is_last_word and result["band"] == "correct":
                    # Definitive goodbye — no "want to play again?" question, because
                    # the frontend will auto-redirect back to the categories grid
                    # after this reply plays out (see lesson_complete signal below).
                    transition = (
                        " This was the LAST word in the lesson — celebrate the whole "
                        "lesson warmly in 1–2 sentences, name 1 thing they did really well, "
                        "and end with a clear, cheerful goodbye like \"See you next time, "
                        "bye-bye!\". Do NOT ask any follow-up question."
                    )
                elif meta["maxed_out"]:
                    transition = (
                        " They've tried this word several times. Be EXTRA gentle — say "
                        "something like 'this one's tricky even for grown-ups, let's come back "
                        "to it later!' and invite them to move on. Keep it to 2 short sentences."
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
                        "follow your tutor personality; do not lecture. "
                        "React DIRECTLY to what they just said about this exact word — "
                        "do NOT introduce yourself, do NOT ask their name, do NOT change topic."
                        f"{transition}"
                        f"{cue_hint}"
                    ),
                )
            except Exception as e:
                logger.warning("generate_reply after pronunciation: %s", e)

            # If we just closed out the last word with a "correct", tell the
            # frontend the lesson is done so it can play the goodbye, then
            # auto-redirect the child back to the categories grid. The actual
            # disconnect is driven from the browser (which unmounts LiveKitRoom
            # → cleanly closes the agent session); we just provide the signal
            # and a suggested grace period so the wrap-up audio plays out.
            if is_last_word and result["band"] == "correct":
                redirect_ms = max(
                    2000,
                    int(os.getenv("KID_TUTOR_LESSON_COMPLETE_DELAY_MS", "11000") or "11000"),
                )
                await publish_tutor_json(
                    {
                        "type": "lesson_complete",
                        "topicSlug": topic_slug,
                        "totalWords": len(lesson.words),
                        "redirectAfterMs": redirect_ms,
                    }
                )
                logger.info(
                    "lesson_complete signalled (topic=%s, words=%d, redirectAfterMs=%d)",
                    topic_slug,
                    len(lesson.words),
                    redirect_ms,
                )
        elif scoring_reply_enabled:
            logger.debug(
                "skipping scripted reply for band=%s (no advance/deferred-intro, no max-retry); "
                "letting realtime model respond naturally",
                result["band"],
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
        if msg.get("topicSlug") and str(msg["topicSlug"]).lower() != topic_slug.lower():
            return
        mtype = msg.get("type")
        if mtype == "child_profile":
            nonlocal child_name_from_app
            raw = str(msg.get("childName") or msg.get("name") or "").strip()
            child_name_from_app = raw[:160]
            await refresh_agent_instructions()
            logger.info("child_profile from browser: name=%r (topic=%s)", child_name_from_app, topic_slug)
            return
        if mtype != "lesson_index":
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
        t = (ev.transcript or "").strip()
        if t:
            asyncio.create_task(publish_input_speech_started_once())
        if not ev.is_final:
            return
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

    # With bitHuman, tutor audio is published by the avatar pipeline — turn off duplicate agent track.
    # Voice-only (KID_TUTOR_USE_AVATAR=0): we must publish Realtime audio to the room or the child hears nothing.
    await session.start(
        agent=kid_agent,
        room=ctx.room,
        room_options=RoomOptions(
            audio_output=not use_avatar,
            audio_input=_kid_room_audio_input_options(),
        ),
    )
    logger.info(
        "AgentSession started: audio published to LiveKit room=%s (use_avatar=%s; set KID_TUTOR_USE_AVATAR=0 for voice-only if you hear no tutor)",
        not use_avatar,
        use_avatar,
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
            cn = (child_name_from_app.strip() or _child_display_name_from_room(ctx.room) or child_display_name)
            child_hint = ""
            if cn:
                child_hint = (
                    f' The app shows this learner\'s name as "{cn}" for this session — use it in your greeting if natural. '
                    "If they say a different name, use theirs. Do not invent or substitute a different name."
                )
            session.generate_reply(
                instructions=(
                    f"Open the session as {tutor_name}. Speak ONE warm sentence introducing "
                    "yourself by name and welcoming the child to learning time. Then ONE short "
                    "opener question — pick from: how they're feeling today, or "
                    "what they had for breakfast. "
                    f"If you do not yet know their preferred name from context, you may ask their name gently.{child_hint} "
                    f"Do NOT ask anything related to the lesson topic ({topic}) — for example, "
                    "do NOT ask their favourite colour / animal / number / fruit / shape, and "
                    "do NOT name anything from the picture on their screen. "
                    "Do NOT mention any vocabulary word and do NOT ask them to repeat anything yet. "
                    "CRITICAL: Do NOT ask 'Are you ready?', 'Ready to learn?', 'Shall we start?', "
                    "'Ready for fun?', or ANY yes/no question about starting — these cause the lesson "
                    "to skip the first word. Ask ONLY about feelings or breakfast. "
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
