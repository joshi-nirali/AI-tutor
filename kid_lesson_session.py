"""Per-room lesson index, retry counts, and instruction suffix for the kid tutor agent."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    #: ``vocabulary`` | ``speaking`` | ``quiz`` — shapes live instruction suffix.
    session_mode: str = ""
    #: After a ``correct`` score, picture stays on ``word_index`` until the child speaks again;
    #: then we sync to this index (see agent ``KID_TUTOR_DEFER_PICTURE_UNTIL_RESPONSE``).
    pending_advance_to_index: int | None = None
    #: Vocabulary-only: pronunciation passed; waiting for a short comprehension answer before advancing.
    vocab_awaiting_comprehension: bool = False
    #: Quiz-only: how many questions the host has asked about the current picture.
    #: Resets to 0 whenever the picture changes (see ``_apply_word_index``). The
    #: agent's instruction suffix tells the LLM to ask AT MOST
    #: ``quiz_max_questions_per_picture`` questions; once reached, the picture
    #: must move on regardless.
    quiz_questions_asked: int = 0
    quiz_max_questions_per_picture: int = 2
    #: Quiz-only: curated Q&A from ``data/prompts/lesson_quiz_questions.json``.
    #: ``quiz_questions_asked`` counts child answers; the next question to ask
    #: is at index ``quiz_questions_asked`` (0 = first, 1 = second).
    quiz_word_questions: list[dict[str, str]] = field(default_factory=list)
    quiz_last_child_answer: str = ""

    #: Speaking-only: ordered practice sentences for the current word (3 by
    #: default). The agent fills this from ``data/prompts/lesson_sentences.json``
    #: each time the picture advances. ``speaking_sentence_index`` tracks which
    #: one is being modelled now; ``speaking_sentences_passed`` counts how many
    #: have hit the pass threshold for this word; ``speaking_sentence_attempts``
    #: counts retries on the current sentence. All four reset on picture change.
    speaking_word_sentences: list[str] = field(default_factory=list)
    speaking_sentence_index: int = 0
    speaking_sentences_passed: int = 0
    speaking_sentence_attempts: int = 0
    speaking_target_sentences: int = 3
    speaking_sentence_max_attempts: int = 3
    speaking_last_sentence_score: int | None = None
    speaking_last_sentence_band: str | None = None
    speaking_last_missing_words: list[str] = field(default_factory=list)

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
            # Quiz: every picture starts a fresh question round.
            self.quiz_questions_asked = 0
            self.quiz_word_questions = []
            self.quiz_last_child_answer = ""
            # Speaking: reset the per-word sentence drill (the agent re-fills
            # ``speaking_word_sentences`` from the JSON bank on word change).
            self.speaking_word_sentences = []
            self.speaking_sentence_index = 0
            self.speaking_sentences_passed = 0
            self.speaking_sentence_attempts = 0
            self.speaking_last_sentence_score = None
            self.speaking_last_sentence_band = None
            self.speaking_last_missing_words = []

    def set_quiz_questions(self, questions: list[dict[str, str]]) -> None:
        """Replace the cached quiz-question list for the current word."""
        cleaned: list[dict[str, str]] = []
        for item in questions or []:
            if not isinstance(item, dict):
                continue
            q = str(item.get("question") or "").strip()
            if not q:
                continue
            cleaned.append(
                {
                    "question": q,
                    "type": str(item.get("type") or "either_or").strip(),
                    "answer": str(item.get("answer") or "").strip(),
                }
            )
        self.quiz_word_questions = cleaned
        if cleaned:
            self.quiz_max_questions_per_picture = min(2, len(cleaned))

    def next_quiz_question(self) -> dict[str, str] | None:
        """The question the host should ask next (index == ``quiz_questions_asked``)."""
        if not self.quiz_word_questions:
            return None
        idx = max(0, int(self.quiz_questions_asked))
        if idx < len(self.quiz_word_questions):
            return self.quiz_word_questions[idx]
        return None

    def last_quiz_question(self) -> dict[str, str] | None:
        """The question the child most recently answered."""
        if not self.quiz_word_questions or self.quiz_questions_asked <= 0:
            return None
        idx = min(self.quiz_questions_asked - 1, len(self.quiz_word_questions) - 1)
        return self.quiz_word_questions[idx]

    def set_speaking_sentences(self, sentences: list[str]) -> None:
        """Replace the cached sentence list for the current word and reset progress."""
        self.speaking_word_sentences = list(sentences or [])
        self.speaking_sentence_index = 0
        self.speaking_sentences_passed = 0
        self.speaking_sentence_attempts = 0
        self.speaking_last_sentence_score = None
        self.speaking_last_sentence_band = None
        self.speaking_last_missing_words = []
        if self.speaking_word_sentences:
            self.speaking_target_sentences = min(
                self.speaking_target_sentences or 3,
                len(self.speaking_word_sentences),
            )

    def current_speaking_sentence(self) -> str | None:
        if not self.speaking_word_sentences:
            return None
        idx = max(0, min(self.speaking_sentence_index, len(self.speaking_word_sentences) - 1))
        return self.speaking_word_sentences[idx]

    def record_sentence_score(self, score: int, band: str, missing: list[str] | None) -> dict[str, Any]:
        """Bookkeeping for a sentence attempt; returns advance hints for the agent."""
        self.speaking_last_sentence_score = int(score)
        self.speaking_last_sentence_band = band
        self.speaking_last_missing_words = list(missing or [])
        self.speaking_sentence_attempts += 1

        # ``almost`` (>= 70) and ``correct`` both count as a pass — that matches
        # the user's chosen "balanced" threshold (>=70).
        passed = band in ("correct", "almost")
        attempts_maxed = self.speaking_sentence_attempts >= self.speaking_sentence_max_attempts

        advance_sentence = False
        advance_word = False
        if passed:
            self.speaking_sentences_passed += 1
            if self.speaking_sentences_passed >= self.speaking_target_sentences:
                advance_word = True
            else:
                advance_sentence = True
        elif attempts_maxed:
            # Out of retries: count it, move on so the child isn't stuck.
            if self.speaking_sentences_passed + 1 >= self.speaking_target_sentences:
                advance_word = True
            else:
                advance_sentence = True

        if advance_sentence and not advance_word:
            self.speaking_sentence_index += 1
            self.speaking_sentence_attempts = 0

        return {
            "passed": passed,
            "attempts_maxed": attempts_maxed,
            "advance_sentence": advance_sentence,
            "advance_word": advance_word,
            "sentences_passed": self.speaking_sentences_passed,
            "sentences_target": self.speaking_target_sentences,
            "sentence_index": self.speaking_sentence_index,
        }

    def set_word_index(self, index: int) -> None:
        """Jump to an index (UI or tool): cancels any deferred picture advance."""
        self.pending_advance_to_index = None
        self.vocab_awaiting_comprehension = False
        self._apply_word_index(index)

    def apply_pending_advance(self) -> None:
        """Move ``word_index`` to ``pending_advance_to_index`` and clear the flag."""
        if self.pending_advance_to_index is None:
            return
        idx = self.pending_advance_to_index
        self.pending_advance_to_index = None
        self.vocab_awaiting_comprehension = False
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
            f"- Session mode: {self.session_mode or 'lesson'}.",
            f"- Lesson word list index (0-based): {self.word_index} of {max(len(self.words) - 1, 0)}.",
        ]
        if self.session_mode == "vocabulary":
            lines.append(
                "- Active role: AI TEACHER (Teaching Mode). Tone: warm, curious, explains in simple "
                "English for ages 5–8."
            )
            if self.vocab_awaiting_comprehension:
                exp = self.expected_word()
                lines.append(
                    "- Teaching step NOW: comprehension question. The child pronounced "
                    f'"{exp or "this word"}" well — ask ONE simple question about its meaning '
                    "(yes/no, A/B, or \"where does it live?\"-style). Wait for their answer. "
                    "Do NOT announce the next word yet."
                )
            else:
                lines.append(
                    "- Teaching flow per word: (1) announce word, (2) simple meaning, "
                    "(3) point at the picture, (4) one example sentence, (5) ask them to repeat, "
                    "(6) ONE comprehension question. Never skip to the next word after pronunciation alone."
                )
        elif self.session_mode == "speaking":
            cur_sentence = self.current_speaking_sentence()
            target_word = exp or "this word"
            target_count = max(1, int(self.speaking_target_sentences or 3))
            passed_count = max(0, int(self.speaking_sentences_passed))
            sent_pos = min(passed_count + 1, target_count)
            attempts = max(0, int(self.speaking_sentence_attempts))
            max_att = max(1, int(self.speaking_sentence_max_attempts))
            last_score = self.speaking_last_sentence_score
            last_band = self.speaking_last_sentence_band
            last_missing = list(self.speaking_last_missing_words or [])

            lines.append(
                "- Active role: SENTENCE-SPEAKING COACH. Tone: punchy, fast, encouraging — "
                "you teach the child to speak whole sentences (not just words)."
            )
            if cur_sentence:
                lines.append(
                    f'- EXACT sentence to model right now: "{cur_sentence}". '
                    "Speak this sentence verbatim (kid-friendly cadence). Do NOT invent your own."
                )
            else:
                lines.append(
                    f'- No curated sentence loaded yet — say one short kid sentence using "{target_word}".'
                )
            lines.append(
                f"- Sentence progress on this word: {passed_count} of {target_count} passed; "
                f"now working on sentence #{sent_pos}. (Each word needs {target_count} sentences.)"
            )

            if last_score is None or last_band is None:
                lines.append(
                    f"- Turn plan: SAY the sentence ONCE, then 'Say it with me!' Wait for the child."
                )
            else:
                miss_clip = ", ".join(f'"{m}"' for m in last_missing[:3]) if last_missing else ""
                if last_band in ("correct", "almost"):
                    if attempts == 0:
                        lines.append(
                            f"- Last attempt scored {last_score}/100 ({last_band}) — PASSED. "
                            "Cheer briefly and move to the NEXT sentence (it is shown above)."
                        )
                    else:
                        lines.append(
                            f"- Last attempt scored {last_score}/100 ({last_band}) — PASSED. "
                            "Cheer briefly. Either move to the next sentence OR wrap this word "
                            "if all sentences are passed."
                        )
                else:
                    if miss_clip:
                        lines.append(
                            f"- Last attempt scored {last_score}/100 ({last_band}). "
                            f"They missed/garbled: {miss_clip}. Give ONE short tip on the trickiest "
                            "word (syllable-break is great, e.g. EL-E-PHANT) then re-model the "
                            "FULL sentence and ask them to try again."
                        )
                    else:
                        lines.append(
                            f"- Last attempt scored {last_score}/100 ({last_band}). "
                            "Say 'Good try! Slower and louder this time!' then re-model the FULL "
                            "sentence and ask them to repeat."
                        )
            lines.append(
                f"- Attempts on the current sentence: {attempts} / {max_att}. "
                f"After {max_att} attempts, the app auto-moves on regardless — never drill a 4th time."
            )
            lines.append(
                "- FORBIDDEN in this mode: inventing your own sentences (use the EXACT one above), "
                "teaching meaning, fun facts, 'what colour is it?', spelling letter-by-letter, "
                "skipping to the next word before sentences are passed, asking yes/no questions "
                "instead of repetition."
            )
        elif self.session_mode == "quiz":
            asked = max(0, int(self.quiz_questions_asked))
            cap = max(1, int(self.quiz_max_questions_per_picture))
            target = exp or "this picture"
            next_q = self.next_quiz_question()
            last_q = self.last_quiz_question()
            lines.append(
                "- Active role: GAME-SHOW HOST (Picture Quiz). Tone: dramatic pauses, big "
                "OOOHs, silly 'final answer?' energy. This is a GAME, not a lesson."
            )
            if next_q and asked < cap:
                q_text = next_q.get("question", "")
                q_type = next_q.get("type", "")
                ref = next_q.get("answer", "")
                q_num = asked + 1
                lines.append(
                    f'- EXACT quiz question to ask NOW (question {q_num} of {cap} on "{target}"): '
                    f'"{q_text}". Ask this question VERBATIM — do NOT invent your own. '
                    f"(Type: {q_type or 'either_or'}.)"
                )
                if ref:
                    lines.append(
                        f"- Reference answer (for your reaction only — never say 'wrong'; "
                        f'any child answer is fine): "{ref}".'
                    )
            elif asked >= cap:
                lines.append(
                    f"- Quiz cap reached ({cap} questions on this picture). Cheer their "
                    "answer with showmanship and announce the NEXT picture now (the app "
                    "syncs the picture when you say the next word aloud)."
                )
            else:
                lines.append(
                    f'- Quiz step NOW: cue "{target}" and ask the first curated question '
                    "from your live state. Wait for their answer."
                )
            if last_q and self.quiz_last_child_answer:
                lines.append(
                    f'- Last question asked: "{last_q.get("question", "")}". '
                    f'Child answered: "{self.quiz_last_child_answer}". React playfully in '
                    "ONE short line (never say 'wrong')."
                )
            if asked == 1 and cap >= 2 and next_q:
                prev_type = (last_q or {}).get("type", "")
                next_type = next_q.get("type", "")
                lines.append(
                    f"- After first answer: EITHER ask question 2 (type {next_type!r}, "
                    f"different from {prev_type!r}) ONLY if their answer was fast and "
                    "confident, OR transition to the next picture by saying the next word "
                    "aloud. Pick whichever keeps the game rhythm up."
                )
            lines.append(
                "- FORBIDDEN in this mode: inventing your own questions (use EXACT ones "
                "from live state), asking them to PRONOUNCE the word, teaching meanings, "
                "long fun facts, asking the same question type twice in a row, more than "
                f"{cap} questions per picture."
            )
        if exp:
            lines.append(f"- Current practice target word: \"{exp}\".")
        else:
            lines.append("- No fixed target word in the list (empty or out of range).")

        nw = self.next_word_while_deferring_picture()
        if nw is not None and exp:
            lines.append(
                f"- The child just succeeded on \"{exp}\" (index {self.word_index}). "
                f'Invite them to say the NEXT word "{nw}"; the picture updates when you speak '
                f'"{nw}" aloud (complete word, not spelled). '
                f'If they say nothing, cheerfully ask again for "{nw}" — do NOT skip ahead; stay on '
                "this prompt until they respond (never mention tools or code in speech)."
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
                "- The on-screen picture syncs when you speak each lesson word aloud — do not mention "
                "tools, function names, or code in speech."
            )
        return "\n".join(lines)
