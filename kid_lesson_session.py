"""Per-room lesson index, retry counts, and instruction suffix for the kid tutor agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class KidLessonSession:
    words: list[str]
    max_retries: int = 3
    word_index: int = 0
    retries_after_fail: int = 0
    last_score: int | None = None
    last_band: str | None = None
    last_said: str | None = None
    last_expected: str | None = None
    topic_slug: str = ""
    #: After a ``correct`` score, picture stays on ``word_index`` until the child speaks again;
    #: then we sync to this index (see agent ``KID_TUTOR_DEFER_PICTURE_UNTIL_RESPONSE``).
    pending_advance_to_index: int | None = None

    def set_topic_slug(self, slug: str) -> None:
        self.topic_slug = slug or ""

    def _apply_word_index(self, index: int) -> None:
        """Set carousel index and reset per-word stats (does not touch ``pending_advance_to_index``)."""
        if not self.words:
            self.word_index = 0
            return
        i = max(0, min(int(index), len(self.words) - 1))
        if i != self.word_index:
            self.word_index = i
            self.retries_after_fail = 0
            self.last_score = None
            self.last_band = None
            self.last_said = None
            self.last_expected = None

    def set_word_index(self, index: int) -> None:
        """Jump to an index (UI or tool): cancels any deferred picture advance."""
        self.pending_advance_to_index = None
        self._apply_word_index(index)

    def apply_pending_advance(self) -> None:
        """Move ``word_index`` to ``pending_advance_to_index`` and clear the flag."""
        if self.pending_advance_to_index is None:
            return
        idx = self.pending_advance_to_index
        self.pending_advance_to_index = None
        self._apply_word_index(idx)

    def next_word_while_deferring_picture(self) -> str | None:
        """The word we are asking them to try next while the picture still shows the previous item."""
        if self.pending_advance_to_index is None or not self.words:
            return None
        j = self.pending_advance_to_index
        if 0 <= j < len(self.words):
            return self.words[j]
        return None

    def expected_word(self) -> str | None:
        if not self.words or self.word_index < 0 or self.word_index >= len(self.words):
            return None
        return self.words[self.word_index]

    def record_score(self, score: int, band: str, said_token: str) -> dict[str, Any]:
        self.last_score = score
        self.last_band = band
        self.last_said = said_token
        self.last_expected = self.expected_word()

        maxed = False
        if band == "correct":
            self.retries_after_fail = 0
        else:
            self.retries_after_fail += 1
            maxed = self.retries_after_fail > self.max_retries

        return {
            "retries": self.retries_after_fail,
            "max_retries": self.max_retries,
            "maxed_out": maxed,
        }

    def instruction_suffix(self) -> str:
        exp = self.expected_word()
        lines = [
            "",
            "## Live session state (updated automatically — follow this)",
            f"- Lesson word list index (0-based): {self.word_index} of {max(len(self.words) - 1, 0)}.",
        ]
        if exp:
            lines.append(f"- Current practice target word: \"{exp}\".")
        else:
            lines.append("- No fixed target word in the list (empty or out of range).")

        nw = self.next_word_while_deferring_picture()
        if nw is not None and exp:
            lines.append(
                f"- The on-screen picture still matches \"{exp}\" (index {self.word_index}), but the child "
                f'just succeeded — your job is to invite them to say the NEXT word "{nw}" next. '
                "The picture will update automatically when they speak (any real answer). "
                f'If they say nothing, cheerfully ask again for "{nw}" — do NOT skip ahead or call '
                "go_to_next_lesson_word to force the picture; stay on this prompt until they respond."
            )

        if self.last_score is not None and self.last_band and self.last_said is not None:
            lines.append(
                f"- Last heard attempt (best match token): \"{self.last_said}\" "
                f"→ score {self.last_score}/100 ({self.last_band})."
            )
            lines.append(f"- Failed attempts on this word (since last success): {self.retries_after_fail} (max {self.max_retries}).")
            if self.retries_after_fail > self.max_retries:
                lines.append(
                    "- This word is tricky for them! Be EXTRA gentle and supportive. "
                    "Say something like 'This is a tough one even for grown-ups!' "
                    "Offer to skip with excitement: 'Let's go on an adventure to the next word "
                    "and come back to this one later — it'll be easier then!' "
                    "Never make the child feel they failed. Keep it fun and light."
                )
        else:
            lines.append("- No scored pronunciation attempt on this word yet in this session.")

        lines.append(
            "- Stay aligned with the picture on the child's screen: they use Next/Back for the same index."
        )
        if self.words and self.pending_advance_to_index is None:
            lines.append(
                "- When YOU change which list word you are teaching, call go_to_next_lesson_word or "
                "sync_lesson_picture_index so the on-screen picture matches (see main instructions)."
            )
        return "\n".join(lines)
