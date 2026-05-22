"""bitHuman Essence avatar agent -- cloud-hosted (no local models needed).

Kid tutor flow: the browser joins a room named like
  kidtutor-{mode}-{topic}-{tutor}-{sessionId}
e.g. kidtutor-vocabulary-animals-leo-a1b2c3d4
The {tutor} slug (e.g. leo, luna) sets the AI's name and short character note.

Data channel topic ``kidtutor`` (JSON):
  - Client → agent: {"type":"lesson_index","index":<int>,"topicSlug":<str>}
  - Client → agent: {"type":"child_profile","childName":<str>,"topicSlug":<str>,"tutorSlug":<str>,"cartesiaVoiceId":<uuid>} — name + optional Cartesia Sonic UUID from the app (when non-empty, overrides server CARTESIA_VOICE_* for TTS)
  - Agent → client: lesson_index_ack, pronunciation_result (may include ``avatarCue``), lesson_set_index,
    input_speech_started (first child speech detected on STT path — UI may hide setup loader)

Optional env:
  Speech is **always** Cartesia Ink STT + Sonic TTS (``CARTESIA_API_KEY`` required). Tutor reasoning uses OpenAI
  **chat** only (``OPENAI_API_KEY`` + ``OPENAI_LLM_MODEL``) — not for STT or TTS.
  With BitHuman avatar on, Cartesia audio is published on the agent track; the child's speakers should use that
  (the UI mutes ``bithuman-avatar-agent`` audio, which otherwise uses the voice set on bithuman.ai).
  CARTESIA_STT_MODEL — default ``ink-whisper`` (child speech → text only). CARTESIA_TTS_MODEL — default ``sonic-3``
  (tutor speaking timbre). Changing STT does **not** change how the tutor sounds; use TTS model + ``CARTESIA_VOICE_*``.
  CARTESIA_VOICE / CARTESIA_VOICE_<SLUG> — Sonic voice UUIDs from https://play.cartesia.ai
  CARTESIA_TTS_SPEED / CARTESIA_TTS_SPEED_<SLUG> — Sonic-3 speech speed as a float 0.6–2.0 (1.0 = normal).
  Leo and Cub default to 0.88 when unset (slower for kids 3–7); override per tutor with e.g.
  ``CARTESIA_TTS_SPEED_LEO=0.90`` / ``CARTESIA_TTS_SPEED_CUB=0.90`` in ``.env``.
  KID_TUTOR_LOG_TRANSCRIPTS — set ``1`` to log each Cartesia STT final transcript (verbose; for STT debugging).
  LIVEKIT_LOG_LEVEL / LOG_LEVEL — worker and job-process verbosity (default INFO in prod). Set ``ERROR`` and you will
  not see ``bithuman-agent`` INFO lines in the console.
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
  KID_TUTOR_INTERRUPT_TIMEOUT — floor (seconds) for waiting on ``session.interrupt()`` during scoring replies (default ``2.0``).
  KID_TUTOR_LIVEKIT_SPEECH_INTERRUPT_TIMEOUT_S — override LiveKit ``SpeechHandle.INTERRUPTION_TIMEOUT`` (5–120s). If unset and BitHuman is on, defaults to **15s** so ``speech not done in time after interruption`` is less likely.
  KID_TUTOR_AVATAR_CLEAR_BUFFER_RPC_TIMEOUT_S — seconds for ``perform_rpc(lk.clear_buffer)`` to the avatar worker (default **25** when unset and patch applies). Set ``0`` to skip this monkeypatch.
  KID_TUTOR_PREEMPTIVE_GENERATION — ``0``/``1`` forces off/on; if unset, **off when avatar is on** (reduces ``preemptive generation … chat context or tools have changed`` when lesson tools run).
  KID_TUTOR_AUTO_ADVANCE_ON_CORRECT — default ``1``. After a ``correct`` pronunciation band the lesson
  index advances one step, the UI picture is synced, and the tutor's celebration reply already introduces
  the next word in the same turn. Set ``0`` to require manual UI/data-channel advancement.
  KID_TUTOR_DEFER_PICTURE_UNTIL_RESPONSE — default ``0``. When ``1``, lesson index still advances on
  correct but the on-screen picture may wait for the child's next utterance. Set ``0`` for immediate
  picture sync on each correct score (recommended).
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
import sys
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
from livekit.agents.voice import io as agent_io
from livekit.agents.voice.room_io import AudioInputOptions, RoomOptions
from livekit.plugins import bithuman, cartesia, deepgram, openai, silero

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

# Match "R-O-A-R", "R O A R", "r-o-a-r" — single letters separated by hyphens or spaces.
# Requires at least 3 letters to avoid clobbering "A-Z", "x-y" axis labels, etc.
_LETTER_SPELL_RE = re.compile(r"\b([A-Za-z])(?:[-\s]+[A-Za-z]){2,}\b")

# ALL-CAPS words (2+ letters): Cartesia reads them as acronyms ("R-O-A-R").
# Convert to lowercase so TTS says the word aloud.
_ALL_CAPS_RE = re.compile(r"\b[A-Z]{2,}\b")

# LLM sometimes leaks tool-call text into spoken replies (e.g. functions.go_to_next_lesson_word).
_TOOL_NAME_LEAK_RE = re.compile(
    r"(?:functions\.)?(?:go_to_next_lesson_word|sync_lesson_picture_index)\b",
    re.I,
)
_FUNCTIONS_NAMESPACE_LEAK_RE = re.compile(r"functions\.[a-z_][a-z0-9_]*\s*", re.I)


def _tts_has_speakable_content(text: str) -> bool:
    """False for punctuation-only fragments (e.g. a lone '.') that TTS reads aloud as 'dot'."""
    if not (text or "").strip():
        return False
    core = re.sub(r"[\s.!?…,;:—\-'\"()]+", "", text)
    return bool(core)


def _fix_tts_text(text: str) -> str:
    """Fix TTS problems: letter spelling, ALL-CAPS acronyms, leaked tool names."""
    text = _TOOL_NAME_LEAK_RE.sub("", text)
    text = _FUNCTIONS_NAMESPACE_LEAK_RE.sub("", text)
    text = _LETTER_SPELL_RE.sub(lambda m: re.sub(r"[-\s]+", "", m.group(0)).lower(), text)
    text = _ALL_CAPS_RE.sub(lambda m: m.group(0).lower(), text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if text and not _tts_has_speakable_content(text):
        return ""
    return text


def _lesson_word_mentioned_in_speech(spoken_text: str, word: str) -> bool:
    """True if *word* appears in tutor TTS (handles hyphens / ALL-CAPS fixes)."""
    if not (spoken_text or "").strip() or not (word or "").strip():
        return False
    spoken_lower = spoken_text.lower()
    wl = word.lower().strip()
    if re.search(rf"\b{re.escape(wl)}\b", spoken_lower):
        return True
    compact = re.sub(r"[\s\-]+", "", spoken_lower)
    wl_compact = re.sub(r"[\s\-]+", "", wl)
    return bool(wl_compact) and len(wl_compact) >= 3 and wl_compact in compact


# "Can you say cat?", "try elephant", etc.
_TTS_SAY_WORD_RE = re.compile(
    r"\b(?:say|try|practice|repeat)(?:\s+the\s+word)?\s+([a-z][a-z\-']{1,24})\b",
    re.I,
)
_TTS_FIRST_WORD_PHRASE_RE = re.compile(
    r"\bfirst\s+(?:lesson\s+)?word\s+is\s+([a-z][a-z\-']{1,24})\b",
    re.I,
)
_TTS_NEXT_WORD_PHRASE_RE = re.compile(
    r"\bnext\s+word\s+is\s+([a-z][a-z\-']{1,24})\b",
    re.I,
)


def _lesson_word_compact_key(word: str) -> str:
    wk = pronunciation_score.lesson_word_key(word)
    return re.sub(r"[\s\-']+", "", (wk or word).lower())


def _vocab_index_for_spoken_token(token: str, words: list[str]) -> int | None:
    key = _lesson_word_compact_key(token)
    if len(key) < 2:
        return None
    for i, w in enumerate(words):
        if _lesson_word_compact_key(w) == key:
            return i
    return None


def _tts_last_say_prompt_word_index(spoken_text: str, words: list[str]) -> int | None:
    """Last say/try prompt in the buffer (what the tutor is asking for right now)."""
    if not words:
        return None
    last: int | None = None
    for m in _TTS_SAY_WORD_RE.finditer(spoken_text or ""):
        idx = _vocab_index_for_spoken_token(m.group(1), words)
        if idx is not None:
            last = idx
    return last


def _pick_tts_lesson_word_index(
    spoken_text: str,
    words: list[str],
    *,
    current_index: int,
    pending_index: int | None,
) -> int | None:
    """Pick at most one list index from explicit tutor phrasing — never skip ahead in the list."""
    if not words or not (spoken_text or "").strip():
        return None
    cur = max(0, min(current_index, len(words) - 1))
    max_allowed = cur + 1
    if pending_index is not None:
        max_allowed = min(max_allowed, pending_index)

    for m in _TTS_FIRST_WORD_PHRASE_RE.finditer(spoken_text):
        idx = _vocab_index_for_spoken_token(m.group(1), words)
        if idx is not None and idx <= max_allowed:
            return idx

    for m in _TTS_NEXT_WORD_PHRASE_RE.finditer(spoken_text):
        idx = _vocab_index_for_spoken_token(m.group(1), words)
        if idx is not None and idx <= max_allowed and idx in (cur, cur + 1):
            return idx

    say_idx = _tts_last_say_prompt_word_index(spoken_text, words)
    if say_idx is not None and say_idx <= max_allowed:
        return say_idx

    if pending_index is not None and pending_index < len(words):
        if _lesson_word_mentioned_in_speech(spoken_text, words[pending_index]):
            return pending_index

    if _lesson_word_mentioned_in_speech(spoken_text, words[cur]):
        return cur

    return None


class _KidTutorAgent(Agent):
    """Agent subclass that strips letter-by-letter spellings from TTS text."""

    def __init__(self, *args, picture_sync_from_tts=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._picture_sync_from_tts = picture_sync_from_tts

    def tts_node(self, text, model_settings):
        sync_fn = self._picture_sync_from_tts

        async def _filtered():
            buffer = ""
            async for chunk in text:
                logger.info("tts_chunk raw: %r", chunk)
                buffer += chunk
                # Flush on sentence boundary or when buffer grows large
                if re.search(r"[.!?]\s*$", buffer) or len(buffer) > 100:
                    fixed = _fix_tts_text(buffer)
                    if fixed != buffer and fixed:
                        logger.info("tts fix applied: %r → %r", buffer.strip(), fixed.strip())
                    elif fixed != buffer and not fixed:
                        logger.debug("tts skip punctuation-only chunk: %r", buffer.strip())
                    if fixed:
                        if sync_fn:
                            try:
                                await sync_fn(fixed)
                            except Exception as e:
                                logger.debug("tts picture sync: %s", e)
                        yield fixed
                    buffer = ""
            # Flush any remaining text
            if buffer:
                fixed = _fix_tts_text(buffer)
                if fixed != buffer and fixed:
                    logger.info("tts fix applied: %r → %r", buffer.strip(), fixed.strip())
                elif fixed != buffer and not fixed:
                    logger.debug("tts skip punctuation-only chunk: %r", buffer.strip())
                if fixed:
                    if sync_fn:
                        try:
                            await sync_fn(fixed)
                        except Exception as e:
                            logger.debug("tts picture sync: %s", e)
                    yield fixed

        return Agent.default.tts_node(self, _filtered(), model_settings)


def _parse_log_level_from_env() -> int:
    """Match LiveKit worker: LIVEKIT_LOG_LEVEL or LOG_LEVEL (default INFO)."""
    raw = (os.getenv("LIVEKIT_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    if raw == "TRACE":
        return logging.DEBUG
    if raw in ("WARN", "WARNING"):
        return logging.WARNING
    return getattr(logging, raw, logging.INFO)


def _ensure_job_process_logging() -> None:
    """Job processes may have no logging handlers (root stays WARNING); force stderr output.

    LiveKit's CLI configures logging in the main worker process; forked/spawned job workers
    sometimes run ``entrypoint`` with an empty root logger, so ``logger.info`` would be dropped.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    level = _parse_log_level_from_env()
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s"))
    root.addHandler(h)
    root.setLevel(level)
    logger.setLevel(level)


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


# Slug from room name -> per-tutor config (display name, Cartesia Sonic voice UUID, character hint).
# Add a new tutor by appending an entry here. Voice precedence: CARTESIA_VOICE_<SLUG> env
# → optional "cartesia_voice" below → CARTESIA_VOICE env → default UUID.
# Voice UUID precedence: env CARTESIA_VOICE_<SLUG> → CARTESIA_VOICE → this default (Cartesia "Tessa" Sonic voice).
_DEFAULT_CARTESIA_VOICE = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b"

TUTOR_FROM_SLUG: dict[str, dict[str, str]] = {
    "leo": {
        "name": "Leo",
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


def _hydrate_tutor_cartesia_voices() -> None:
    """Load CARTESIA_VOICE_<SLUG> from .env into TUTOR_FROM_SLUG and log the map."""
    for slug, cfg in TUTOR_FROM_SLUG.items():
        env_voice = (os.getenv(f"CARTESIA_VOICE_{slug.upper()}", "") or "").strip()
        if env_voice:
            cfg["cartesia_voice"] = env_voice
    leo_v = (TUTOR_FROM_SLUG.get("leo") or {}).get("cartesia_voice", "")
    cub_v = (TUTOR_FROM_SLUG.get("cub") or {}).get("cartesia_voice", "")
    if leo_v and cub_v and leo_v == cub_v:
        logger.error(
            "CARTESIA_VOICE_LEO and CARTESIA_VOICE_CUB are the same UUID (%s) — "
            "Leo and Cub will sound identical; use two different voices from play.cartesia.ai",
            leo_v,
        )
    for slug, cfg in TUTOR_FROM_SLUG.items():
        vid = (cfg.get("cartesia_voice") or "").strip()
        logger.info(
            "Cartesia voice for tutor %s → %s (set CARTESIA_VOICE_%s in .env)",
            slug,
            vid or "(missing — will use CARTESIA_VOICE fallback)",
            slug.upper(),
        )


def _cartesia_voice_for_tutor(tutor_slug: str) -> str:
    """Cartesia Sonic voice UUID (https://play.cartesia.ai)."""
    slug = (tutor_slug or "").strip().lower()
    if slug:
        per = os.getenv(f"CARTESIA_VOICE_{slug.upper()}", "").strip()
        if per:
            return per
        cfg = TUTOR_FROM_SLUG.get(slug)
        if cfg and cfg.get("cartesia_voice"):
            return cfg["cartesia_voice"].strip()
    return (os.getenv("CARTESIA_VOICE", "") or "").strip() or _DEFAULT_CARTESIA_VOICE


def _min_attempt_score_for_mode(mode: str) -> int:
    """Minimum similarity score before treating speech as a pronunciation attempt."""
    if mode == "speaking":
        raw = (
            os.getenv("KID_TUTOR_SPEAKING_MIN_ATTEMPT_SCORE")
            or os.getenv("KID_TUTOR_MIN_ATTEMPT_SCORE")
            or "55"
        )
    else:
        raw = os.getenv("KID_TUTOR_MIN_ATTEMPT_SCORE", "40")
    try:
        return max(0, min(100, int(str(raw).strip())))
    except ValueError:
        return 55 if mode == "speaking" else 40


def _advance_bands_for_mode(mode: str) -> frozenset[str]:
    """Pronunciation bands that may auto-advance the lesson picture."""
    if mode == "speaking":
        return frozenset({"correct"})
    return frozenset({"correct", "almost"})


def _cartesia_tts_speed_for_tutor(tutor_slug: str) -> float:
    """Cartesia Sonic-3 speed (0.6–2.0; 1.0 ≈ normal).

    Kid tutors (Leo and Cub) default to a slightly slower 0.88 so young learners
    (ages 3–7) can follow the words; override per tutor with
    ``CARTESIA_TTS_SPEED_<SLUG>`` in ``.env``.
    """
    slug = (tutor_slug or "").strip().lower()
    if slug:
        per = (os.getenv(f"CARTESIA_TTS_SPEED_{slug.upper()}", "") or "").strip()
        if per:
            try:
                return max(0.6, min(2.0, float(per)))
            except ValueError:
                logger.warning(
                    "Invalid CARTESIA_TTS_SPEED_%s=%r — ignored",
                    slug.upper(),
                    per,
                )
    raw = (os.getenv("CARTESIA_TTS_SPEED", "") or "").strip()
    if raw:
        try:
            return max(0.6, min(2.0, float(raw)))
        except ValueError:
            logger.warning("Invalid CARTESIA_TTS_SPEED=%r — ignored", raw)
    if slug in ("cub", "leo"):
        return 0.88
    return 1.0


_hydrate_tutor_cartesia_voices()


class _DualCartesiaAudioOutput(agent_io.AudioOutput):
    """Play Cartesia TTS in the room (Sonic voice) and mirror to BitHuman for lip-sync.

    BitHuman cloud re-voices audio with the voice baked into each bithuman.ai agent;
    the child must hear the agent worker track (room sink), not the avatar participant.
    """

    def __init__(
        self,
        *,
        room_sink: agent_io.AudioOutput,
        bithuman_sink: agent_io.AudioOutput,
    ) -> None:
        super().__init__(
            label="CartesiaDual",
            next_in_chain=None,
            sample_rate=room_sink.sample_rate or bithuman_sink.sample_rate,
            capabilities=agent_io.AudioOutputCapabilities(
                pause=room_sink.can_pause and bithuman_sink.can_pause
            ),
        )
        self._room_sink = room_sink
        self._bithuman_sink = bithuman_sink

        # Forward playback_finished from the room sink up to this object so that
        # AgentSession.wait_for_playout() resolves and the INTERRUPTION_TIMEOUT is
        # cancelled. Without this, _DualCartesiaAudioOutput.on_playback_finished()
        # is never called and "speech not done in time" fires after 15 s every time.
        room_sink.on(
            "playback_finished",
            lambda ev: self.on_playback_finished(
                playback_position=ev.playback_position,
                interrupted=ev.interrupted,
            ),
        )

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        await asyncio.gather(
            self._room_sink.capture_frame(frame),
            self._bithuman_sink.capture_frame(frame),
        )

    def flush(self) -> None:
        super().flush()
        self._room_sink.flush()
        self._bithuman_sink.flush()

    def clear_buffer(self) -> None:
        super().clear_buffer()
        self._room_sink.clear_buffer()
        self._bithuman_sink.clear_buffer()

    def on_attached(self) -> None:
        super().on_attached()
        self._room_sink.on_attached()
        self._bithuman_sink.on_attached()

    def on_detached(self) -> None:
        super().on_detached()
        self._room_sink.on_detached()
        self._bithuman_sink.on_detached()

    def pause(self) -> None:
        super().pause()
        self._room_sink.pause()
        self._bithuman_sink.pause()

    def resume(self) -> None:
        super().resume()
        self._room_sink.resume()
        self._bithuman_sink.resume()


def _deepgram_keyterms(fixed_words: list[str] | None) -> list[str]:
    """Words to boost in Deepgram STT for this session.

    Combines: env override (``DEEPGRAM_KEYTERMS``, comma- or pipe-separated) with the
    current lesson word list. Boosting the lesson vocabulary dramatically improves
    recognition for kids — e.g. "elephant" / "banana" stop being heard as "elephant"
    / "panama". Deepgram nova-3 ``keyterm`` is unlimited; nova-2 uses ``keywords``.
    """
    out: list[str] = []
    raw = (os.getenv("DEEPGRAM_KEYTERMS", "") or "").strip()
    if raw:
        parts = re.split(r"[|,]", raw)
        out.extend(p.strip() for p in parts if p.strip())
    if fixed_words:
        out.extend(w.strip() for w in fixed_words if isinstance(w, str) and w.strip())
    seen: set[str] = set()
    deduped: list[str] = []
    for w in out:
        k = w.lower()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(w)
    return deduped


def _deepgram_endpointing_ms() -> int:
    """Kid speech often has long pauses — bigger endpointing helps STT wait for full words."""
    raw = (os.getenv("DEEPGRAM_ENDPOINTING_MS", "") or "").strip()
    if not raw:
        return 25
    try:
        return max(10, min(2000, int(raw)))
    except ValueError:
        logger.warning("Invalid DEEPGRAM_ENDPOINTING_MS=%r — ignored", raw)
        return 25


def _build_agent_session(
    *,
    tutor_slug: str,
    use_avatar: bool,
    fixed_words: list[str] | None = None,
) -> AgentSession:
    """Deepgram Nova-3 STT + Cartesia Sonic TTS; OpenAI chat LLM for reasoning and tools."""
    if not (os.getenv("CARTESIA_API_KEY") or "").strip():
        raise ValueError(
            "CARTESIA_API_KEY is required — tutor voice uses Cartesia Sonic TTS. "
            "Create a key at https://play.cartesia.ai"
        )
    deepgram_api_key = (os.getenv("DEEPGRAM_API_KEY") or "").strip()
    if not deepgram_api_key:
        raise ValueError(
            "DEEPGRAM_API_KEY is required — child speech uses Deepgram Nova-3 STT. "
            "Create a key at https://console.deepgram.com"
        )
    tts_model = (os.getenv("CARTESIA_TTS_MODEL", "sonic-3") or "sonic-3").strip()
    deepgram_model = (os.getenv("DEEPGRAM_STT_MODEL", "nova-3") or "nova-3").strip()
    # Indian English by default ("en-IN") — supported on both nova-3 and nova-2. Override
    # via DEEPGRAM_STT_LANGUAGE=en, en-US, en-GB, en-AU, multi (nova-3 multilingual), …
    deepgram_language = (
        os.getenv("DEEPGRAM_STT_LANGUAGE", "en-IN") or "en-IN"
    ).strip()
    cartesia_voice = _cartesia_voice_for_tutor(tutor_slug)
    cartesia_speed = _cartesia_tts_speed_for_tutor(tutor_slug)
    llm_model = (os.getenv("OPENAI_LLM_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini").strip()
    if "realtime" in llm_model.lower():
        raise ValueError(
            f"OPENAI_LLM_MODEL={llm_model!r} is a Realtime speech model, not a chat completions model. "
            "Set OPENAI_LLM_MODEL to a chat model (e.g. gpt-4.1-mini, gpt-4o-mini)."
        )

    keyterms = _deepgram_keyterms(fixed_words)
    endpointing_ms = _deepgram_endpointing_ms()
    smart_format = (os.getenv("DEEPGRAM_SMART_FORMAT", "0") or "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    filler_words_enabled = (
        os.getenv("DEEPGRAM_FILLER_WORDS", "1") or "1"
    ).strip().lower() in ("1", "true", "yes", "on")

    logger.info(
        "Voice pipeline: Deepgram STT model=%s language=%s endpointing=%dms keyterms=%d "
        "smart_format=%s filler_words=%s | Cartesia TTS model=%s voice=%s speed=%s | OpenAI LLM=%s",
        deepgram_model,
        deepgram_language,
        endpointing_ms,
        len(keyterms),
        smart_format,
        filler_words_enabled,
        tts_model,
        cartesia_voice,
        cartesia_speed,
        llm_model,
    )
    if keyterms:
        logger.info("Deepgram keyterm boost (%d words): %s", len(keyterms), keyterms)

    stt_kwargs: dict = {
        "model": deepgram_model,
        "language": deepgram_language,
        "endpointing_ms": endpointing_ms,
        "smart_format": smart_format,
        "filler_words": filler_words_enabled,
    }
    if keyterms:
        # nova-3 family takes ``keyterm`` (no weight); nova-2 family takes ``keywords``
        # as a list of (word, weight) tuples. Fall back to no-boost on unknown models.
        model_lc = deepgram_model.lower()
        if model_lc.startswith("nova-3"):
            stt_kwargs["keyterm"] = keyterms
        elif model_lc.startswith("nova-2"):
            stt_kwargs["keywords"] = [(w, 1.5) for w in keyterms]
        else:
            logger.info(
                "Skipping Deepgram keyterm boost — model %r does not advertise keyterm/keywords support",
                deepgram_model,
            )

    th = _kid_tutor_turn_handling(use_avatar=use_avatar)
    if th:
        logger.info("AgentSession turn_handling override: %s", th)
    kwargs: dict = {
        "stt": deepgram.STT(**stt_kwargs),
        "llm": openai.LLM(model=llm_model),
        "tts": cartesia.TTS(model=tts_model, voice=cartesia_voice, speed=cartesia_speed),
        "vad": silero.VAD.load(),
    }
    # Always use VAD-based turn detection — avoids AdaptiveInterruptionDetector
    # which requires LiveKit Cloud credentials and fails on self-hosted LiveKit.
    kwargs["turn_handling"] = {"turn_detection": "vad", **(th or {})}
    return AgentSession(**kwargs)


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


_LK_DATASTREAM_CLEAR_BUFFER_PATCHED = False


def _apply_livekit_speech_interrupt_tuning(*, use_avatar: bool) -> None:
    """Raise ``SpeechHandle.INTERRUPTION_TIMEOUT`` when BitHuman needs longer post-interrupt drain.

    Library default is often 5s — too tight with Cartesia + avatar clear-buffer over TURN.
    Explicit ``KID_TUTOR_LIVEKIT_SPEECH_INTERRUPT_TIMEOUT_S`` always wins; if unset and
    ``use_avatar``, defaults to 15s.
    """
    raw = (os.getenv("KID_TUTOR_LIVEKIT_SPEECH_INTERRUPT_TIMEOUT_S") or "").strip()
    if raw:
        try:
            v = float(raw)
        except ValueError:
            logger.warning(
                "Invalid KID_TUTOR_LIVEKIT_SPEECH_INTERRUPT_TIMEOUT_S=%r — ignored",
                raw,
            )
            return
        v = max(5.0, min(v, 120.0))
    elif use_avatar:
        v = 15.0
    else:
        return
    try:
        from livekit.agents.voice import speech_handle as speech_handle_mod
    except Exception as e:
        logger.warning("Cannot patch LiveKit SpeechHandle interrupt timeout: %s", e)
        return
    prev = getattr(speech_handle_mod, "INTERRUPTION_TIMEOUT", None)
    speech_handle_mod.INTERRUPTION_TIMEOUT = v  # type: ignore[misc]
    logger.info(
        "LiveKit SpeechHandle INTERRUPTION_TIMEOUT → %.1fs (was %s) "
        "(KID_TUTOR_LIVEKIT_SPEECH_INTERRUPT_TIMEOUT_S or avatar default)",
        v,
        prev,
    )


def _kid_tutor_turn_handling(*, use_avatar: bool) -> dict | None:
    """Turn handling tweaks: preemptive LLM hurts when tools + interrupts churn (BitHuman)."""
    raw = (os.getenv("KID_TUTOR_PREEMPTIVE_GENERATION") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return None
    if raw in ("0", "false", "no", "off"):
        return {"preemptive_generation": {"enabled": False}}
    if use_avatar:
        return {"preemptive_generation": {"enabled": False}}
    return None


def _ensure_livekit_avatar_datastream_clear_buffer_rpc_patch() -> None:
    """BitHuman uses ``DataStreamAudioOutput``; clear-buffer RPC used to omit ``response_timeout`` → timeouts on relay.

    Patches ``perform_rpc(..., response_timeout=…)`` only. Set ``KID_TUTOR_AVATAR_CLEAR_BUFFER_RPC_TIMEOUT_S=0``
    to disable. Default 25s when unset.
    """
    global _LK_DATASTREAM_CLEAR_BUFFER_PATCHED
    if _LK_DATASTREAM_CLEAR_BUFFER_PATCHED:
        return
    raw = (os.getenv("KID_TUTOR_AVATAR_CLEAR_BUFFER_RPC_TIMEOUT_S") or "25").strip()
    if raw in ("0", "false", "no", "off"):
        _LK_DATASTREAM_CLEAR_BUFFER_PATCHED = True
        logger.info("KID_TUTOR_AVATAR_CLEAR_BUFFER_RPC_TIMEOUT_S disabled — skipping clear-buffer RPC timeout patch")
        return
    try:
        rpc_to = float(raw)
    except ValueError:
        logger.warning("Invalid KID_TUTOR_AVATAR_CLEAR_BUFFER_RPC_TIMEOUT_S=%r — using 25", raw)
        rpc_to = 25.0
    rpc_to = max(5.0, min(rpc_to, 120.0))
    try:
        from livekit.agents.voice.avatar import _datastream_io as _ds
    except Exception as e:
        logger.warning("Cannot patch LiveKit DataStreamAudioOutput clear-buffer RPC: %s", e)
        _LK_DATASTREAM_CLEAR_BUFFER_PATCHED = True
        return

    async def _clear_buffer_task_with_rpc_timeout(self, pushed_duration: float) -> None:
        timeout = self._clear_buffer_timeout
        try:
            await self._room.local_participant.perform_rpc(
                destination_identity=self._destination_identity,
                method=_ds.RPC_CLEAR_BUFFER,
                payload="",
                response_timeout=rpc_to,
            )
        except Exception as e:
            logger.error("failed to perform clear buffer rpc", exc_info=True)
            timeout = 0

        def _on_timeout() -> None:
            logger.warning(
                "didn't receive playback finished event after clear buffer, marking playout as done arbitrarily"
            )
            self.on_playback_finished(playback_position=pushed_duration, interrupted=True)
            self._reset_playback_count()

        if self._clear_buffer_timeout_handler:
            self._clear_buffer_timeout_handler.cancel()

        if timeout is not None:
            self._clear_buffer_timeout_handler = asyncio.get_event_loop().call_later(
                timeout, _on_timeout
            )

    _ds.DataStreamAudioOutput._clear_buffer_task = (  # type: ignore[method-assign]
        _clear_buffer_task_with_rpc_timeout
    )
    _LK_DATASTREAM_CLEAR_BUFFER_PATCHED = True
    logger.info(
        "Patched DataStreamAudioOutput clear-buffer perform_rpc response_timeout=%.1fs "
        "(KID_TUTOR_AVATAR_CLEAR_BUFFER_RPC_TIMEOUT_S)",
        rpc_to,
    )


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
    """Mic path into Cartesia STT: optional BVC + pre-connect buffering."""
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


def _register_voice_debug_handlers(room: rtc.Room, session: AgentSession) -> None:
    """Log Cartesia STT/TTS/OpenAI LLM errors and LiveKit track subscription failures."""

    def _on_agent_error(ev: object) -> None:
        err = getattr(ev, "error", None)
        src = getattr(ev, "source", None)
        src_name = type(src).__name__ if src is not None else "unknown"
        msg = str(err) if err is not None else repr(ev)
        if "STT" in src_name:
            logger.error("Cartesia STT error [%s]: %s", src_name, msg)
        elif "TTS" in src_name:
            logger.error("Cartesia TTS error [%s]: %s", src_name, msg)
            status = getattr(err, "status_code", None)
            if status == 402 or "402" in msg or "quota" in msg.lower() or "credits" in msg.lower():
                logger.error(
                    "Cartesia TTS blocked: model credits / quota limit reached for this API key. "
                    "The tutor cannot speak until you add credits or upgrade at https://play.cartesia.ai "
                    "(Billing / Usage). This is not a code bug."
                )
        elif "LLM" in src_name:
            logger.error("OpenAI chat LLM error [%s]: %s", src_name, msg)
            if "model.request" in msg or "missing_scope" in msg:
                logger.error(
                    "OpenAI key cannot call Chat Completions (needs scope model.request). "
                    "Cartesia handles all speech; this key is only for tutor text — create or fix the key at "
                    "https://platform.openai.com/api-keys"
                )
        else:
            logger.error("AgentSession error source=%s: %s", src_name, msg)

    def _on_track_subscription_failed(
        participant: rtc.RemoteParticipant,
        track_sid: str,
        error: str,
    ) -> None:
        logger.error(
            "LiveKit track_subscription_failed identity=%s track_sid=%s error=%s",
            getattr(participant, "identity", ""),
            track_sid,
            error,
        )

    def _on_room_disconnected(reason: object) -> None:
        logger.info("LiveKit room disconnected reason=%s", reason)

    def _on_speech_created(ev: object) -> None:
        src = getattr(ev, "source", None)
        ui = getattr(ev, "user_initiated", None)
        logger.info("TTS pipeline: speech_created source=%s user_initiated=%s", src, ui)

    session.on("error", _on_agent_error)
    session.on("speech_created", _on_speech_created)
    room.on("track_subscription_failed", _on_track_subscription_failed)
    room.on("disconnected", _on_room_disconnected)


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
    (and is what ``_cartesia_voice_for_tutor`` uses for Sonic TTS voice UUIDs).
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
    _ensure_job_process_logging()
    room_name = getattr(ctx.room, "name", "") or ""
    logger.info("Agent job started — joining room=%s", room_name)
    await ctx.connect(rtc_config=_livekit_agent_rtc_configuration())
    await ctx.wait_for_participant()
    await _ensure_room_connected(ctx.room)

    use_avatar = use_bithuman_avatar()
    _apply_livekit_speech_interrupt_tuning(use_avatar=use_avatar)
    if use_avatar:
        _ensure_livekit_avatar_datastream_clear_buffer_rpc_patch()

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

    _default_max_retries = int(retry_policy.get("maxRetries", 3))
    if mode == "speaking":
        max_retries = max(1, min(_default_max_retries, 2))
    elif mode == "vocabulary":
        max_retries = max(_default_max_retries, 5)
    else:
        max_retries = _default_max_retries
    lesson = KidLessonSession(
        words=fixed_words,
        max_retries=max_retries,
        session_mode=mode,
    )
    lesson.set_topic_slug(topic_slug)

    # Until the agent has explicitly handed off greeting → lesson, never score the
    # child's speech. Otherwise the kid's casual reply to "What's your favourite
    # colour?" gets matched against word_index 0 (e.g. "blue") and auto-advances
    # the picture before they ever practiced the first word. See transition_into_lesson.
    lesson_started = False

    # Ignore pronunciation scoring until ``time.monotonic()`` passes this value.
    # STT often emits bogus finals right after the tutor speaks (echo from
    # speakers, noise, or duplicate segments). Those can fuzzy-match the lesson
    # word → false "correct" → auto-advance while the child never spoke.
    scoring_mute_until: float | None = None
    transcript_lock = asyncio.Lock()
    pipeline_index_lock_until: float | None = None
    pending_advance_trigger_norm: str | None = None
    last_stt_dedupe_key: str | None = None
    last_stt_dedupe_at: float = 0.0
    last_pronunciation_burst_key: str | None = None
    last_pronunciation_burst_at: float = 0.0
    last_published_picture_index: int | None = None

    def _stt_final_dedupe_s() -> float:
        raw = (os.getenv("KID_TUTOR_STT_FINAL_DEDUPE_S", "2.5") or "2.5").strip()
        try:
            v = float(raw)
        except ValueError:
            v = 2.5
        return max(0.5, min(v, 10.0))

    def _pipeline_index_lock_s() -> float:
        raw = (os.getenv("KID_TUTOR_PIPELINE_INDEX_LOCK_S", "8.0") or "8.0").strip()
        try:
            v = float(raw)
        except ValueError:
            v = 8.0
        return max(2.0, min(v, 30.0))

    def _mark_pipeline_index_lock() -> None:
        nonlocal pipeline_index_lock_until
        pipeline_index_lock_until = time.monotonic() + _pipeline_index_lock_s()

    def _pipeline_controls_index() -> bool:
        return (
            pipeline_index_lock_until is not None
            and time.monotonic() < pipeline_index_lock_until
        )

    def _clear_pipeline_index_lock() -> None:
        nonlocal pipeline_index_lock_until, pending_advance_trigger_norm
        pipeline_index_lock_until = None
        pending_advance_trigger_norm = None

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
    cartesia_voice = _cartesia_voice_for_tutor(tutor_slug)
    logger.info(
        "Cloud Essence mode -- use_avatar=%s avatar_id=%s room=%s mode=%s topic=%s tutor=%s cartesia_voice=%s "
        "words=%d child_name=%r",
        use_avatar,
        avatar_id or "(none)",
        room_name,
        mode,
        topic,
        tutor_name,
        cartesia_voice,
        len(fixed_words),
        child_display_name or "",
    )
    logger.info(
        "Cartesia TTS voice UUID above is from the server env chain: CARTESIA_VOICE_%s → CARTESIA_VOICE → "
        "code default. If it is not the voice you picked in play.cartesia.ai, unset or update those lines in "
        ".env and restart the worker. The React app can override with REACT_APP_CARTESIA_VOICE_%s (sent in "
        "child_profile) when that value is non-empty.",
        (tutor_slug or "leo").upper(),
        (tutor_slug or "leo").upper(),
    )
    if topic_slug and not fixed_words:
        logger.warning("No fixed word list for topic_slug=%s (check data/word_lists.json)", topic_slug)

    avatar = None
    if use_avatar:
        avatar = bithuman.AvatarSession(
            avatar_id=avatar_id,
            api_secret=os.getenv("BITHUMAN_API_SECRET"),
        )

    session = _build_agent_session(
        tutor_slug=tutor_slug,
        use_avatar=use_avatar,
        fixed_words=fixed_words,
    )
    _register_voice_debug_handlers(ctx.room, session)

    # Wait for browser child_profile before greeting so REACT_APP_CARTESIA_VOICE_* applies to first speech.
    child_profile_received = asyncio.Event()

    async def apply_cartesia_voice(voice_id: str, *, source: str) -> None:
        voice_id = (voice_id or "").strip()
        if not voice_id:
            return
        tts = session.tts
        if tts is None:
            logger.warning("Cannot apply Cartesia voice from %s — session has no TTS", source)
            return
        speed = _cartesia_tts_speed_for_tutor(tutor_slug)
        tts.update_options(voice=voice_id, speed=speed)
        tts_m = getattr(getattr(tts, "_opts", None), "model", None)
        logger.info(
            "Cartesia TTS voice applied from %s: voice=%s speed=%s (tts_model=%s)",
            source,
            voice_id,
            speed,
            tts_m or (os.getenv("CARTESIA_TTS_MODEL", "sonic-3") or "sonic-3").strip(),
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

    async def publish_lesson_picture_index(index: int, reason: str) -> int:
        """Publish at most one forward step per advance; skip duplicate indices."""
        nonlocal last_published_picture_index
        if not lesson.words:
            return 0
        clamped = max(0, min(int(index), len(lesson.words) - 1))
        if last_published_picture_index is not None:
            if clamped == last_published_picture_index:
                logger.debug(
                    "skip duplicate lesson_set_index %s (%s)",
                    clamped,
                    reason,
                )
                return clamped
            if clamped > last_published_picture_index + 1:
                logger.warning(
                    "picture jump %s→%s capped to %s (%s)",
                    last_published_picture_index,
                    clamped,
                    last_published_picture_index + 1,
                    reason,
                )
                clamped = last_published_picture_index + 1
        last_published_picture_index = clamped
        await publish_tutor_json(
            {
                "type": "lesson_set_index",
                "topicSlug": topic_slug,
                "index": clamped,
                "reason": reason,
            }
        )
        return clamped

    async def _maybe_sync_picture_from_tts(spoken_text: str) -> None:
        """Sync lesson index + carousel to the word the tutor is teaching (current or +1 only)."""
        if mode not in ("vocabulary", "speaking") or not lesson.words:
            return
        if not (spoken_text or "").strip():
            return
        words = lesson.words
        pending = lesson.pending_advance_to_index
        target_idx = _pick_tts_lesson_word_index(
            spoken_text,
            words,
            current_index=lesson.word_index,
            pending_index=pending,
        )
        if target_idx is None:
            return

        max_step = lesson.word_index + 1
        if pending is not None:
            max_step = pending
        if target_idx > max_step:
            logger.debug(
                "tts picture sync ignored %s (%r) — beyond allowed step %s",
                target_idx,
                words[target_idx],
                max_step,
            )
            return
        if last_published_picture_index == target_idx and lesson.word_index == target_idx:
            return

        if pending is not None:
            if target_idx != pending:
                return
            await publish_lesson_picture_index(target_idx, "tts_spoken_lesson_word")
            return

        if lesson.word_index != target_idx:
            lesson.set_word_index(target_idx)
            _mark_pipeline_index_lock()
            await refresh_agent_instructions()
            logger.info(
                "tts lesson index synced to %s (%r)",
                target_idx,
                words[target_idx],
            )

        if last_published_picture_index != target_idx:
            await publish_lesson_picture_index(target_idx, "tts_spoken_lesson_word")
            logger.info(
                "tts picture synced to index %s (%r)",
                target_idx,
                words[target_idx],
            )

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
        # Floor for how long we wait on ``session.interrupt()`` before giving up.
        # The actual ``asyncio.wait_for`` cap is ``max(this, SpeechHandle.INTERRUPTION_TIMEOUT + 2.5)``
        # so we never return early while LiveKit is still draining interrupted speech
        # (library default is often 5s — shorter floors caused overlap + errors).
        raw = os.getenv("KID_TUTOR_INTERRUPT_TIMEOUT", "2.0").strip()
        try:
            v = float(raw)
        except ValueError:
            v = 2.0
        return max(0.15, min(v, 60.0))

    interrupt_timeout_s = _interrupt_timeout_s()

    def _scoring_interrupt_wait_cap_s() -> float:
        try:
            from livekit.agents.voice import speech_handle as _sh

            drain = float(getattr(_sh, "INTERRUPTION_TIMEOUT", 5.0))
        except Exception:
            drain = 5.0
        return max(interrupt_timeout_s, drain + 2.5)

    if scoring_reply_enabled:
        logger.info(
            "Pronunciation reply: interrupt wait floor=%.2fs cap=%.2fs (LiveKit drain + 2.5s)",
            interrupt_timeout_s,
            _scoring_interrupt_wait_cap_s(),
        )

    auto_advance_on_correct = os.getenv(
        "KID_TUTOR_AUTO_ADVANCE_ON_CORRECT", "1"
    ).strip().lower() in ("1", "true", "yes", "on")
    min_attempt_score = _min_attempt_score_for_mode(mode)
    advance_bands = _advance_bands_for_mode(mode)
    if auto_advance_on_correct:
        logger.info(
            "Auto-advance lesson picture: enabled (mode=%s, bands=%s, min_attempt_score=%s)",
            mode,
            ",".join(sorted(advance_bands)),
            min_attempt_score,
        )
    else:
        logger.info("Auto-advance lesson picture on correct pronunciation: disabled")
    if mode == "vocabulary":
        logger.info(
            "Vocabulary flow: teach → say word → one comprehension check → next word (picture stays until check)"
        )
    elif mode == "speaking":
        logger.info(
            "Speaking flow: quick say-and-repeat; advance on clear pronunciation only (bands=%s)",
            ",".join(sorted(advance_bands)),
        )

    defer_picture_until_response = os.getenv(
        "KID_TUTOR_DEFER_PICTURE_UNTIL_RESPONSE", "0"
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
                "Internal only: advance one step in the lesson word list and sync the picture. "
                "Do not mention this tool in speech. In vocabulary mode the app usually advances "
                "automatically after a correct pronunciation — call only if the picture is clearly wrong."
            )
        )
        async def go_to_next_lesson_word(_ctx: RunContext) -> str:
            if not lesson.words:
                return "No vocabulary list in this lesson."
            if lesson.pending_advance_to_index is not None:
                return (
                    "Picture advance is waiting for the child to speak after their last correct "
                    "word — do not call this tool until they respond."
                )
            if _pipeline_controls_index():
                w = lesson.expected_word() or "n/a"
                return (
                    f"Picture is already at index {lesson.word_index} (word: {w}) from pronunciation "
                    "scoring — do not advance again."
                )
            last = len(lesson.words) - 1
            if lesson.word_index >= last:
                return "Already on the last word — celebrate or wrap up."
            lesson.set_word_index(lesson.word_index + 1)
            await publish_lesson_picture_index(lesson.word_index, "go_to_next_lesson_word")
            await refresh_agent_instructions()
            w = lesson.expected_word() or "n/a"
            return f"Advanced to index {lesson.word_index} (word: {w})."

        @function_tool(
            description=(
                "Internal only: set the picture carousel to a 0-based lesson index. "
                "Never mention this tool in speech. Call only when the picture is clearly out of sync."
            )
        )
        async def sync_lesson_picture_index(_ctx: RunContext, word_index: int) -> str:
            requested = max(0, min(int(word_index), max(len(lesson.words) - 1, 0)))
            if lesson.pending_advance_to_index is not None and requested != lesson.word_index:
                return (
                    "Picture will advance when the child speaks after their correct word — "
                    "do not jump the carousel index yet."
                )
            if _pipeline_controls_index() and requested != lesson.word_index:
                w = lesson.expected_word() or "n/a"
                return (
                    f"Picture already synced to index {lesson.word_index} (word: {w}) from "
                    "pronunciation scoring — no change."
                )
            # Don't re-publish or rebuild instructions when the LLM asks us to set
            # the index to where we already are. The previous behavior caused an
            # extra lesson_set_index round-trip on every "correct" turn (LLM
            # echoing the auto-advance) which thrashed the bitHuman avatar
            # pipeline and produced audible cut-outs.
            if requested == lesson.word_index:
                w = lesson.expected_word() or "n/a"
                return f"Already at index {lesson.word_index} (word: {w}); no change."
            lesson.set_word_index(requested)
            await publish_lesson_picture_index(
                lesson.word_index, "sync_lesson_picture_index"
            )
            await refresh_agent_instructions()
            w = lesson.expected_word() or "n/a"
            return f"Picture synced to index {lesson.word_index} (word: {w})."

        lesson_tools.append(go_to_next_lesson_word)
        lesson_tools.append(sync_lesson_picture_index)

    kid_agent = _KidTutorAgent(
        instructions=full_instructions(),
        tools=lesson_tools,
        picture_sync_from_tts=_maybe_sync_picture_from_tts,
    )

    async def refresh_agent_instructions() -> None:
        async with instruction_lock:
            await kid_agent.update_instructions(full_instructions())

    async def transition_into_lesson(reason: str, *, child_utterance: str = "") -> None:
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
        if lesson.word_index != 0 or lesson.pending_advance_to_index is not None:
            lesson.set_word_index(0)
        await publish_lesson_picture_index(0, "lesson_handoff_first_word")
        expected = lesson.expected_word()
        if not expected:
            return
        cu = (child_utterance or "").strip()
        stt_hint = ""
        if cu:
            stt_hint = (
                f"The learner's last spoken reply (speech-to-text) was: {cu!r}. "
                "Acknowledge that content specifically (if the text looks garbled, guess kindly what a child likely said). "
            )
        if mode == "speaking":
            mode_intro = (
                f'Then start SPEAKING COACH mode on the first word "{expected}". '
                f'Model a short kid-friendly sentence using "{expected}" '
                f'(e.g. "Say: I like {expected}!" or "Say: I see a {expected}!") and ask them '
                "to repeat after you. No definitions. 1–2 short sentences."
            )
        else:
            mode_intro = (
                f'Then start TEACHING MODE on the first word "{expected}". '
                f'Announce the word with excitement, give a simple meaning a 5-year-old can '
                f'picture, point at the picture on screen, and one short example sentence using '
                f'"{expected}". Then ask them to say "{expected}". You will ask ONE comprehension '
                "question about the word after they repeat it. 2–3 short sentences."
            )
        try:
            session.generate_reply(
                instructions=(
                    stt_hint
                    + "Acknowledge what the child just said in ONE short, warm sentence in "
                    "your tutor voice (no name-asking, no new opener question). "
                    "If they just told you their name or how to address them, repeat that name back **exactly** "
                    "in that sentence (match what they said, not a different name). "
                    + mode_intro
                    + " NEVER spell letter-by-letter. "
                    "Do NOT mention any other vocabulary words from the list. "
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
        async with transcript_lock:
            await _handle_final_transcript_locked(text)

    async def _handle_final_transcript_locked(text: str) -> None:
        nonlocal last_stt_dedupe_key, last_stt_dedupe_at, last_pronunciation_burst_key, last_pronunciation_burst_at, pending_advance_trigger_norm
        if not lesson_started:
            # First utterance after the greeting: hand off to the lesson. Run this
            # branch *before* ``should_skip_scoring`` — one-word feeling answers
            # ("fine", "good", "great") are valid replies to the opener but were in
            # the skip list / readiness filler heuristic and never called
            # ``transition_into_lesson``, so the tutor stayed stuck in small-talk.
            t0 = (text or "").strip()
            if not t0 or len(t0) < 2:
                return
            # Only block explicit "I'm ready / let's go" style lines (phrase list +
            # let's-go regex), not generic filler-token readiness vs word 0.
            if pronunciation_score.looks_like_explicit_readiness_reply(t0):
                logger.debug(
                    "greeting explicit readiness reply — not triggering lesson transition: %r",
                    t0,
                )
                return
            await transition_into_lesson("first_child_utterance", child_utterance=t0)
            return
        if pronunciation_score.should_skip_scoring(text):
            logger.info(
                "STT skipped (filler/skip-list, will be handled as chat by LLM): %r",
                text,
            )
            return
        if scoring_mute_until is not None:
            _now = time.monotonic()
            if _now < scoring_mute_until:
                logger.info(
                    "STT skipped (scoring muted %.2fs to avoid echo): %r",
                    scoring_mute_until - _now,
                    text,
                )
                return

        norm_key = pronunciation_score.transcript_dedupe_key(text)
        if norm_key:
            _now = time.monotonic()
            if (
                norm_key == last_stt_dedupe_key
                and (_now - last_stt_dedupe_at) < _stt_final_dedupe_s()
            ):
                logger.info(
                    "STT skipped (duplicate within %.1fs): %r",
                    _now - last_stt_dedupe_at,
                    text,
                )
                return
            last_stt_dedupe_key = norm_key
            last_stt_dedupe_at = _now

        # Vocabulary: pronunciation passed — next child reply is a comprehension answer (not scored as chat).
        if mode == "vocabulary" and lesson.vocab_awaiting_comprehension:
            if pending_advance_trigger_norm and norm_key == pending_advance_trigger_norm:
                logger.debug(
                    "duplicate STT after vocabulary pronunciation — ignore: %r",
                    text,
                )
                return
            if pronunciation_score.should_skip_scoring(text):
                return
            exp_now = lesson.expected_word()
            if exp_now and pronunciation_score.looks_like_readiness_acknowledgment(
                text, exp_now
            ):
                logger.debug(
                    "readiness during vocabulary comprehension wait — ignore: %r",
                    text,
                )
                return
            pidx = lesson.pending_advance_to_index
            next_w: str | None = None
            if pidx is not None and lesson.words and 0 <= pidx < len(lesson.words):
                next_w = lesson.words[pidx]
            lesson.vocab_awaiting_comprehension = False
            if pidx is not None:
                lesson.apply_pending_advance()
                pending_advance_trigger_norm = None
                _mark_pipeline_index_lock()
                await publish_lesson_picture_index(
                    lesson.word_index, "vocab_comprehension_answer"
                )
                await refresh_agent_instructions()
                logger.info(
                    "vocabulary comprehension answered — advanced to index %s (%s)",
                    lesson.word_index,
                    next_w,
                )
            if scoring_reply_enabled:
                try:
                    await asyncio.wait_for(
                        session.interrupt(force=False),
                        timeout=_scoring_interrupt_wait_cap_s(),
                    )
                except (asyncio.TimeoutError, Exception) as e:
                    logger.debug("vocabulary check interrupt: %s", e)
                await asyncio.sleep(0.05)
                try:
                    if next_w:
                        session.generate_reply(
                            instructions=(
                                f'They answered your quick question about "{exp_now or "the word"}" '
                                f'(child said: "{text}"). Celebrate briefly, then teach the next word '
                                f'"{next_w}": say the word, tiny meaning, one example, ask them to say it. '
                                "Speak the next word as a whole word so the picture stays in sync. "
                                "2–3 short sentences. Do not mention tools or code."
                            ),
                        )
                    elif exp_now:
                        session.generate_reply(
                            instructions=(
                                f'They answered about "{exp_now}". One warm sentence of praise — '
                                "all lesson words are done."
                            ),
                        )
                except Exception as e:
                    logger.warning("generate_reply after vocabulary check: %s", e)
            return

        # Waiting for any real utterance before syncing the picture to the next word after
        # a correct score (speaking defer / KID_TUTOR_DEFER_PICTURE_UNTIL_RESPONSE).
        if lesson.pending_advance_to_index is not None:
            if pending_advance_trigger_norm and norm_key == pending_advance_trigger_norm:
                logger.debug(
                    "duplicate STT final for deferred advance (same as correct utterance) — ignore: %r",
                    text,
                )
                return
            pidx = lesson.pending_advance_to_index
            if lesson.words and 0 <= pidx < len(lesson.words):
                next_w = lesson.words[pidx]

                async def _apply_deferred_picture_advance(reason: str) -> None:
                    nonlocal pending_advance_trigger_norm
                    lesson.apply_pending_advance()
                    pending_advance_trigger_norm = None
                    _mark_pipeline_index_lock()
                    await publish_lesson_picture_index(
                        lesson.word_index, reason
                    )
                    await refresh_agent_instructions()

                if pronunciation_score.should_skip_scoring(text):
                    return
                if pronunciation_score.looks_like_readiness_acknowledgment(text, next_w):
                    logger.debug(
                        "readiness reply during deferred handoff — not advancing: %r",
                        text,
                    )
                    return
                if pronunciation_score.looks_like_chat(text):
                    logger.debug(
                        "chat during deferred handoff — not advancing: %r",
                        text,
                    )
                    return
                await _apply_deferred_picture_advance("deferred_advance_attempt")
                logger.info(
                    "deferred picture advance applied (attempt) → index %s; scoring same utterance",
                    lesson.word_index,
                )
            else:
                lesson.pending_advance_to_index = None
                pending_advance_trigger_norm = None

        if pronunciation_score.should_skip_scoring(text):
            return
        expected = lesson.expected_word()
        if not expected:
            return
        scored_at_index = lesson.word_index
        exp_key = pronunciation_score.lesson_word_key(expected)
        if exp_key:
            burst_key = f"{scored_at_index}:{exp_key}"
            _burst_now = time.monotonic()
            if (
                burst_key == last_pronunciation_burst_key
                and (_burst_now - last_pronunciation_burst_at)
                < _stt_final_dedupe_s()
            ):
                logger.debug(
                    "repeated STT for same lesson word %r (index %s) within %.1fs — ignore",
                    expected,
                    scored_at_index,
                    _burst_now - last_pronunciation_burst_at,
                )
                return
            last_pronunciation_burst_key = burst_key
            last_pronunciation_burst_at = _burst_now
        # Don't score as pronunciation when the child is answering meta prompts such as
        # "Are you ready?" — plain "yes"/"ok" are handled by should_skip_scoring; phrases
        # like "yes I'm ready" used to reach score_utterance and could mis-trigger advances.
        if pronunciation_score.looks_like_readiness_acknowledgment(text, expected):
            logger.info(
                "STT routed to LLM (readiness/meta reply, expected=%r): %r",
                expected,
                text,
            )
            return
        # Only score if it actually looks like an attempt at the lesson word.
        # Conversational chat / questions are passed straight to the LLM untouched.
        if pronunciation_score.looks_like_chat(text):
            logger.info(
                "STT routed to LLM (looks conversational, expected=%r): %r",
                expected,
                text,
            )
            return
        result = pronunciation_score.score_utterance(expected, text, score_thresholds)
        if result["score"] < min_attempt_score:
            logger.info(
                "STT routed to LLM (low-similarity vs expected=%r: best_token=%r score=%s < min=%s): %r",
                expected,
                result["best_token"],
                result["score"],
                min_attempt_score,
                text,
            )
            return
        logger.info(
            "STT scored: expected=%r heard=%r best_token=%r score=%s band=%s (mode=%s, word_index=%s)",
            expected,
            text,
            result["best_token"],
            result["score"],
            result["band"],
            mode,
            scored_at_index,
        )
        meta = lesson.record_score(result["score"], result["band"], result["best_token"])
        cue = avatar_cue_for_band(pron_rules, result["band"])

        # Decide picture + lesson index before pronunciation_result so the UI gets one index.
        advanced = False
        deferred_next_intro = False
        vocab_check_queued = False
        next_word: str | None = None
        is_last_word = False
        picture_index = scored_at_index
        if (
            auto_advance_on_correct
            and result["band"] in advance_bands
            and lesson.words
            and not pronunciation_score.looks_like_short_ready_affirmation(text)
        ):
            _extend_scoring_mute(_post_advance_scoring_mute_s())
            last_idx = len(lesson.words) - 1
            if scored_at_index < last_idx and lesson.word_index == scored_at_index:
                next_idx = scored_at_index + 1
                next_word = lesson.words[next_idx]
                if mode == "vocabulary":
                    if not lesson.vocab_awaiting_comprehension:
                        lesson.vocab_awaiting_comprehension = True
                        lesson.pending_advance_to_index = next_idx
                        pending_advance_trigger_norm = norm_key or None
                        vocab_check_queued = True
                        logger.info(
                            "vocabulary: %r ok — comprehension check before %r (picture stays on %r)",
                            expected,
                            next_word,
                            expected,
                        )
                elif defer_picture_until_response:
                    if lesson.pending_advance_to_index is None:
                        lesson.pending_advance_to_index = next_idx
                        pending_advance_trigger_norm = norm_key or None
                        deferred_next_intro = True
                        logger.info(
                            "correct — picture will sync when tutor speaks %r; scoring handoff on next speech",
                            next_word,
                        )
                    else:
                        logger.debug(
                            "correct score while deferred handoff already queued — no extra advance"
                        )
                else:
                    lesson.set_word_index(next_idx)
                    _mark_pipeline_index_lock()
                    advanced = True
                    logger.info(
                        "auto-advanced lesson to index %s (next word: %s); picture sync on tutor TTS",
                        lesson.word_index,
                        next_word,
                    )
            elif scored_at_index >= last_idx:
                is_last_word = True
                picture_index = scored_at_index
                if mode == "vocabulary" and not lesson.vocab_awaiting_comprehension:
                    lesson.vocab_awaiting_comprehension = True
                    vocab_check_queued = True
                    logger.info(
                        "vocabulary: last word %r ok — final comprehension check before wrap-up",
                        expected,
                    )
            else:
                logger.debug(
                    "correct at index %s but lesson.word_index=%s — skip extra picture advance",
                    scored_at_index,
                    lesson.word_index,
                )
                picture_index = lesson.word_index

        pr_payload: dict = {
            "type": "pronunciation_result",
            "topicSlug": topic_slug,
            "lessonMode": mode,
            "wordIndex": scored_at_index,
            "pictureIndex": picture_index,
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
        if mode == "vocabulary":
            pr_payload["flowPhase"] = (
                "quick_check" if lesson.vocab_awaiting_comprehension else "learn"
            )
            pr_payload["awaitingComprehension"] = lesson.vocab_awaiting_comprehension
        elif mode == "speaking":
            pr_payload["flowPhase"] = "speak_repeat"
        await publish_tutor_json(pr_payload)

        await refresh_agent_instructions()

        # Only force a hard interrupt + scripted reply when we MUST redirect the
        # agent's speech — i.e. we just auto-advanced to a new word, finished
        # the last word, or the child has maxed out retries and we want to
        # gracefully skip on. Otherwise let the agent's natural turn-taking respond
        # using the freshly refreshed instructions.
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
            and (
                advanced
                or deferred_next_intro
                or vocab_check_queued
                or is_last_word
                or meta["maxed_out"]
            )
        )
        if needs_scripted_reply:
            try:
                await asyncio.wait_for(
                    session.interrupt(force=False),
                    timeout=_scoring_interrupt_wait_cap_s(),
                )
            except (asyncio.TimeoutError, Exception) as e:
                logger.debug("pronunciation interrupt: %s", e)
            # Tiny breath so in-flight TTS is cancelled before the next reply;
            # avoids overlapping audio on the BitHuman avatar pipeline.
            await asyncio.sleep(0.05)

            try:
                cue_hint = ""
                if cue:
                    cue_hint = (
                        f" Voice energy hint for this turn: {cue.get('emotion', '')} tone, "
                        f"{cue.get('animation', '')} body language (express in voice; UI may show cues)."
                    )
                if vocab_check_queued:
                    transition = (
                        f' They pronounced "{expected}" well ({result["band"]}). '
                        f'Ask ONE quick comprehension question about "{expected}" only — '
                        "yes/no or simple A/B (e.g. big or small?). "
                        f'Do NOT say "{next_word}" yet and do NOT move on. 1–2 sentences.'
                    )
                elif deferred_next_intro and next_word:
                    transition = (
                        f" They pronounced \"{expected}\" correctly. The next word is \"{next_word}\". "
                        f"Celebrate briefly, then invite them to try \"{next_word}\" — say that word once "
                        "as a complete spoken word (never letter-by-letter) so the picture can update. "
                        "If they have not answered yet, do NOT skip ahead — ask again in a fun, "
                        f'encouraging way for "{next_word}". '
                        "The app syncs the picture when you speak the next word — only use natural "
                        "child-friendly sentences; never say tool names, function names, or code."
                    )
                elif advanced and next_word:
                    if mode == "speaking":
                        transition = (
                            f' Speaking coach mode. Celebrate their attempt at "{expected}" in one '
                            f'short cheer, then model a short kid-friendly sentence using "{next_word}" '
                            f'(e.g. "Say: I see a {next_word}!" or "Say: I like {next_word}!"). '
                            "Ask them to repeat after you. No definitions. Total 1–2 sentences."
                        )
                    else:
                        transition = (
                            f' Teaching mode. They did well on "{expected}". If you have not already '
                            f'asked the comprehension question for "{expected}", ask ONE now and wait. '
                            f'Otherwise begin teaching "{next_word}": announce the word, give a simple '
                            "meaning, point at the picture, and one short example sentence (2–3 short "
                            "sentences). Ask them to repeat after that. Picture updates when you speak "
                            "the next word."
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
            if (
                is_last_word
                and result["band"] == "correct"
                and not vocab_check_queued
            ):
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
                "letting agent respond naturally",
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
        # Do not require dp.participant: some SDK paths deliver child data with participant unset;
        # dropping those packets prevented child_profile (name + cartesiaVoiceId) from ever reaching us.
        if isinstance(dp.participant, rtc.LocalParticipant):
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
            profile_slug = str(msg.get("tutorSlug") or "").strip().lower()
            profile_voice = str(msg.get("cartesiaVoiceId") or "").strip()
            server_voice = _cartesia_voice_for_tutor(tutor_slug)
            if profile_slug and profile_slug != tutor_slug:
                logger.warning(
                    "child_profile tutorSlug=%r does not match room tutor_slug=%r — "
                    "voice/avatar come from the LiveKit room name",
                    profile_slug,
                    tutor_slug,
                )
            # Room slug + CARTESIA_VOICE_<SLUG> in .env are authoritative. Only accept the
            # browser UUID when it matches this session's tutor; otherwise both tutors can end
            # up on the same fallback voice from a stale or empty React env build.
            voice_to_apply = server_voice
            voice_source = "room_tutor_slug"
            if profile_voice and profile_slug == tutor_slug:
                voice_to_apply = profile_voice
                voice_source = "child_profile"
            elif profile_voice and profile_voice != server_voice:
                logger.warning(
                    "child_profile cartesiaVoiceId=%s ignored for tutor %s — using server %s "
                    "(set REACT_APP_CARTESIA_VOICE_%s in frontend/.env.local to match .env)",
                    profile_voice,
                    tutor_slug,
                    server_voice,
                    tutor_slug.upper(),
                )
            await apply_cartesia_voice(voice_to_apply, source=voice_source)
            await refresh_agent_instructions()
            logger.info(
                "child_profile from browser: name=%r tutorSlug=%r voice_applied=%r (topic=%s)",
                child_name_from_app,
                profile_slug or tutor_slug,
                voice_to_apply,
                topic_slug,
            )
            child_profile_received.set()
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
        _clear_pipeline_index_lock()
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
        log_transcripts = (
            os.getenv("KID_TUTOR_LOG_TRANSCRIPTS", "1") or "1"
        ).strip().lower() in ("1", "true", "yes", "on")
        if not ev.is_final:
            if (
                t
                and log_transcripts
                and (os.getenv("KID_TUTOR_LOG_INTERIM_TRANSCRIPTS", "0") or "0")
                .strip()
                .lower()
                in ("1", "true", "yes", "on")
            ):
                logger.info(
                    "STT interim (mode=%s, word_index=%s, expected=%r): %r",
                    mode,
                    lesson.word_index if lesson.words else None,
                    lesson.expected_word(),
                    t[:300] + ("…" if len(t) > 300 else ""),
                )
            return
        if not t:
            return
        if log_transcripts:
            logger.info(
                "STT final (mode=%s, word_index=%s, expected=%r, len=%d): %r",
                mode,
                lesson.word_index if lesson.words else None,
                lesson.expected_word(),
                len(t),
                t[:500] + ("…" if len(t) > 500 else ""),
            )
        asyncio.create_task(handle_final_transcript(t))

    def _on_data_received(dp: rtc.DataPacket) -> None:
        asyncio.create_task(handle_room_data(dp))

    session.on("user_input_transcribed", _on_user_input_transcribed)
    ctx.room.on("data_received", _on_data_received)

    # Publish Cartesia TTS on the agent track; tee a copy to BitHuman for lip-sync when avatar is on.
    await session.start(
        agent=kid_agent,
        room=ctx.room,
        room_options=RoomOptions(
            audio_output=True,
            audio_input=_kid_room_audio_input_options(),
        ),
    )

    # Full chain head that publishes the agent audio track (Recorder → … → room).
    # Do NOT use room_io.audio_output here — that can be an inner node; teeing there drops speech.
    pre_avatar_room_audio_head = session.output.audio

    rio = session.room_io
    if rio is not None:
        sub_fut = rio.subscribed_fut
        if sub_fut is not None:
            try:
                await asyncio.wait_for(asyncio.shield(sub_fut), timeout=8.0)
                logger.info("Cartesia room audio track subscribed (agent participant)")
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out waiting for Cartesia audio track subscription — "
                    "child may need to tap Turn on sound"
                )

    if avatar is not None:
        await avatar.start(session, room=ctx.room)
        bithuman_sink = session.output.audio
        if (
            pre_avatar_room_audio_head is not None
            and bithuman_sink is not None
            and pre_avatar_room_audio_head is not bithuman_sink
        ):
            session.output.audio = _DualCartesiaAudioOutput(
                room_sink=pre_avatar_room_audio_head,
                bithuman_sink=bithuman_sink,
            )
            session.output.audio.on_attached()
        elif pre_avatar_room_audio_head is None:
            logger.error(
                "BitHuman avatar on but session.output.audio is missing before avatar.start — "
                "room may have no tutor audio"
            )
        logger.info(
            "BitHuman lip-sync on; Cartesia plays on agent audio track (not bithuman-avatar-agent)"
        )
    else:
        logger.info("Starting session without bitHuman avatar pipeline")

    logger.info(
        "AgentSession started: cartesia_room_sink=%s use_avatar=%s cartesia_voice=%s",
        pre_avatar_room_audio_head is not None,
        use_avatar,
        cartesia_voice,
    )
    await publish_tutor_json(
        {
            "type": "tutor_session_info",
            "tutorSlug": tutor_slug,
            "tutorName": tutor_name,
            "cartesiaVoiceId": cartesia_voice,
            "topicSlug": topic_slug,
        }
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
            try:
                await asyncio.wait_for(child_profile_received.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.info(
                    "Greeting: no child_profile within 3s (trigger=%s) — first speech uses server voice %s; "
                    "set REACT_APP_CARTESIA_VOICE_%s in frontend/.env.local or CARTESIA_VOICE_%s in .env.",
                    reason,
                    cartesia_voice,
                    (tutor_slug or "leo").upper(),
                    (tutor_slug or "leo").upper(),
                )
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
            logger.error("initial greeting generate_reply failed: %s", e, exc_info=True)

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
