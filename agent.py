"""bitHuman Essence avatar agent -- cloud-hosted (no local models needed).

Kid tutor flow: the browser joins a room named like
  kidtutor-{mode}-{topic}-{tutor}-{sessionId}
e.g. kidtutor-vocabulary-animals-leo-a1b2c3d4
The {tutor} slug (e.g. leo, luna) sets the AI's name and short character note.

Usage:
    python agent.py dev        # local dev; pair with token_server + npm start
    python agent.py start      # production worker
"""

import logging
import os
import re

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomOutputOptions,
    WorkerOptions,
    WorkerType,
    cli,
)
from livekit.plugins import bithuman, openai, silero

from curriculum import words_for_topic

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


def build_kid_tutor_instructions(
    mode: str,
    topic: str,
    tutor_name: str,
    tutor_hint: str,
    fixed_words: list[str],
) -> str:
    base = f"""Your name is {tutor_name}. {tutor_hint}
You tutor children about 3–7 years old.
You are on a voice call: keep replies SHORT (one or two sentences) unless practicing syllables.
As soon as you can, say one cheerful greeting (one sentence) so the child knows you are there; then listen for them.
Use simple words, a gentle tone, and enthusiasm. Never shame the child.
Refer to yourself only as {tutor_name} (not any other name).

Lesson theme to lean on: {topic}.

Always encourage effort ("Good try!", "Nice listening!"). Never say the word "wrong".
If an answer is incorrect, gently teach the right idea like: "Good try! Banana is usually yellow."

If you give a pronunciation tip, break the word into clear chunks like "EL… E… PHANT" when helpful."""
    fixed_block = _fixed_word_list_block(fixed_words)

    if mode == "vocabulary":
        return (
            base
            + fixed_block
            + """

Mode: LEARN VOCABULARY
Follow this flow in order when starting or when the child seems ready for a new word:
1) Say the new word clearly with excitement.
2) Explain what it means in very simple language.
3) Give one short example sentence.
4) Ask the child to say the word; listen and give gentle pronunciation help if needed.
5) Ask one easy yes/no or choice question to check understanding.

Stay on kid-friendly words related to the lesson theme."""
        )

    if mode == "speaking":
        return (
            base
            + fixed_block
            + """

Mode: SPEAKING PRACTICE
Focus on pronunciation and repeating.
- Say a word or short phrase; ask the child to repeat.
- If it is close, celebrate; if not, model slowly in chunks and ask them to try again.
- Keep turns quick and game-like."""
        )

    if mode == "quiz":
        return (
            base
            + fixed_block
            + """

Mode: QUIZ
- Ask short questions (choices are easier than open-ended).
- After an answer, say if it is correct in a fun way, or encourage and teach.
- Mix easy wins with one slightly harder question."""
        )

    return base + fixed_block


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    await ctx.wait_for_participant()

    avatar_id = os.getenv("BITHUMAN_AGENT_ID")
    if not avatar_id:
        raise ValueError(
            "Set BITHUMAN_AGENT_ID in your .env file. "
            "Create an agent at https://www.bithuman.ai or via api/generation.py"
        )

    room_name = getattr(ctx.room, "name", "") or ""
    mode, topic, topic_slug, tutor_name, tutor_hint = parse_room(room_name)
    fixed_words = words_for_topic(topic_slug)
    instructions = build_kid_tutor_instructions(
        mode, topic, tutor_name, tutor_hint, fixed_words
    )

    logger.info(
        "Cloud Essence mode -- avatar_id=%s room=%s mode=%s topic=%s tutor=%s words=%d",
        avatar_id,
        room_name,
        mode,
        topic,
        tutor_name,
        len(fixed_words),
    )
    if topic_slug and not fixed_words:
        logger.warning("No fixed word list for topic_slug=%s (check data/word_lists.json)", topic_slug)

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

    await avatar.start(session, room=ctx.room)

    await session.start(
        agent=Agent(instructions=instructions),
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
