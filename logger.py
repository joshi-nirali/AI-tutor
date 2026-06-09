"""Human-friendly latency / usage monitor for the kid tutor agent.

Subscribes to the LiveKit ``AgentSession``'s ``metrics_collected`` events and
emits compact, easy-to-read INFO lines so an operator can see at a glance
how long each turn of the conversation actually took.

Per turn you get four detail lines + one rollup line, all aligned for quick
scanning:

    [Latency] STT   audio= 5.20s  duration=  312ms   model=Deepgram/nova-3   req=8a1c…
    [Latency] EOU   end =  580ms  transcript= 120ms  on_user_turn=    5ms   speech=cac22935
    [Latency] LLM   ttft=  612ms  total = 1820ms     in= 412 out= 58 tps= 31.9   speech=cac22935
    [Latency] TTS   ttfb=  285ms  total =  940ms     audio= 3.10s chars= 142     speech=cac22935
    [Latency] TURN  user→tutor=  1480ms  (eou= 580ms + llm_ttft= 612ms + tts_ttfb= 288ms)   speech=cac22935

Plus a rolling summary every ``KID_TUTOR_PERF_SUMMARY_S`` seconds (default 30 s):

    [Latency] ROLLING (last≤20):  stt_avg=  280ms (n=12)  |  llm_ttft_avg=  605ms (n= 9)  |  tts_ttfb_avg=  295ms (n= 9)  |  eou_avg=  540ms (n=12)

And one final session-totals line on shutdown:

    [Latency] SESSION TOTALS: stt_audio=128.4s  llm_in_tokens=18421  llm_out_tokens=2104  tts_chars=8732  tts_audio=72.3s

To wire it into the agent (already done in ``agent.py``)::

    from logger import LatencyLogger
    perf = LatencyLogger()
    perf.attach(session)
    ctx.add_shutdown_callback(perf.shutdown)

Toggles (env vars):
    KID_TUTOR_LOG_PERF=0|1           default 1; 0 silences every [Latency] line.
    KID_TUTOR_PERF_SUMMARY_S=<sec>   default 30; min 5; cadence of the ROLLING line.
    KID_TUTOR_LOG_CYCLE=0|1          default follows LOG_PERF; one [Cycle] block per round-trip.

``CycleTracer`` (enabled via ``KID_TUTOR_LOG_CYCLE``) prints one consolidated block
per full conversation round-trip::

    [Cycle] #7 mode=speaking word=prince idx=2
    [Cycle]   ① prev_tutor     ttfb= 285ms  audio= 3.10s  total= 940ms
    [Cycle]   ② wait→user      gap= 2.15s  (tutor_done → mic_active)
    [Cycle]   ③ user_stt       audio= 1.80s  proc= 312ms  said="The prince is brave"
    [Cycle]   ④ eou            silence= 580ms  transcript= 120ms  turn_cb=   5ms
    [Cycle]   ⑤ turn_handler   42ms  (scoring + publish + reply setup)
    [Cycle]   ⑥ llm            ttft= 612ms  total=1820ms  in=412 out=58
    [Cycle]   ⑦ response_tts   ttfb= 288ms  audio= 2.05s  total=2300ms
    [Cycle]   ══ round_trip    10.24s  (prev_tutor_done → response_tts_done)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from livekit.agents import metrics


# Share the agent's logger name so [Latency] lines sit next to the rest of the
# agent's INFO output and a single ``grep "bithuman-agent"`` shows everything.
logger = logging.getLogger("bithuman-agent")


def _ms(seconds: float | None) -> str:
    """Format seconds as right-aligned milliseconds, e.g. ``  312ms`` / ``    —``."""
    if seconds is None:
        return "    —"
    return f"{seconds * 1000:5.0f}ms"


def _short_speech_id(sid: str | None) -> str:
    """Take ``speech_cac22935d3895555`` and return ``cac22935`` for log compactness."""
    if not sid:
        return "—"
    tail = sid.split("_", 1)[-1]
    return tail[:8] or "—"


def _sec(seconds: float | None) -> str:
    if seconds is None:
        return "   —"
    return f"{seconds:5.2f}s"


@dataclass
class _TurnMetrics:
    """Provider metrics for one response ``speech_id`` (EOU + LLM + TTS)."""

    speech_id: str = ""
    eou: float | None = None
    transcription_delay: float | None = None
    on_user_turn_delay: float | None = None
    llm_ttft: float | None = None
    llm_total: float | None = None
    llm_in: int = 0
    llm_out: int = 0
    tts_ttfb: float | None = None
    tts_total: float | None = None
    tts_audio: float | None = None

    def ready_to_emit(self) -> bool:
        return (
            self.eou is not None
            and self.llm_ttft is not None
            and self.tts_ttfb is not None
            and self.tts_total is not None
        )


@dataclass
class _ActiveUserTurn:
    """Wall-clock anchors for one child utterance → tutor response cycle."""

    stt_final_at: float = 0.0
    stt_text: str = ""
    user_speech_at: float | None = None
    turn_start_at: float | None = None
    turn_end_at: float | None = None
    stt_audio: float | None = None
    stt_proc: float | None = None
    metrics: _TurnMetrics = field(default_factory=_TurnMetrics)


class CycleTracer:
    """End-to-end round-trip tracer: tutor spoke → user answered → LLM → TTS.

    Emits one multi-line ``[Cycle]`` block when the response TTS finishes so you
    can grep a single tag and see every stage in one place. Complements the
    per-stage ``[Latency]`` lines from ``LatencyLogger``.
    """

    def __init__(self, *, context_fn: Callable[[], dict[str, Any]] | None = None) -> None:
        self._context_fn = context_fn
        self._enabled = self._read_enabled()
        self._cycle_num = 0

        # Previous tutor response (what the child just heard before speaking).
        self._prev_tutor_done_at: float | None = None
        self._prev_tts_ttfb: float | None = None
        self._prev_tts_audio: float | None = None
        self._prev_tts_total: float | None = None

        self._active: _ActiveUserTurn | None = None
        self._latest_stt_audio: float | None = None
        self._latest_stt_proc: float | None = None

    @staticmethod
    def _read_enabled() -> bool:
        raw = (os.getenv("KID_TUTOR_LOG_CYCLE") or "").strip().lower()
        if raw in ("0", "false", "no", "off"):
            return False
        if raw in ("1", "true", "yes", "on"):
            return True
        # Default: on when per-turn latency logging is on.
        return (os.getenv("KID_TUTOR_LOG_PERF", "1") or "1").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    def attach(self, session) -> None:
        if not self._enabled:
            logger.info(
                "[Cycle] disabled — set KID_TUTOR_LOG_CYCLE=1 to enable full round-trip blocks"
            )
            return
        logger.info(
            "[Cycle] enabled — one consolidated block per tutor→user→LLM→TTS round-trip"
        )

        @session.on("metrics_collected")
        def _on_metrics(ev) -> None:  # noqa: ANN001
            self._on_metrics(ev)

        @session.on("speech_created")
        def _on_speech_created(ev) -> None:  # noqa: ANN001
            src = getattr(ev, "source", None)
            if self._active is not None:
                logger.debug("[Cycle] speech_created source=%s (response pending)", src)

    # ── Hooks called from agent.py ─────────────────────────────────────────

    def mark_tutor_response_done(
        self,
        *,
        ttfb: float | None = None,
        audio: float | None = None,
        total: float | None = None,
    ) -> None:
        """Save tutor playback stats; used as ① for the *next* user turn."""
        self._prev_tutor_done_at = time.monotonic()
        if ttfb is not None:
            self._prev_tts_ttfb = ttfb
        if audio is not None:
            self._prev_tts_audio = audio
        if total is not None:
            self._prev_tts_total = total

    def mark_user_speech_started(self) -> None:
        if not self._enabled:
            return
        now = time.monotonic()
        if self._active is None:
            self._active = _ActiveUserTurn()
        if self._active.user_speech_at is None:
            self._active.user_speech_at = now

    def mark_stt_final(self, text: str) -> None:
        if not self._enabled:
            return
        now = time.monotonic()
        if self._active is None:
            self._active = _ActiveUserTurn()
        self._active.stt_final_at = now
        self._active.stt_text = (text or "").strip()[:120]
        if self._latest_stt_audio is not None:
            self._active.stt_audio = self._latest_stt_audio
        if self._latest_stt_proc is not None:
            self._active.stt_proc = self._latest_stt_proc

    def mark_turn_handler_start(self) -> None:
        if not self._enabled or self._active is None:
            return
        self._active.turn_start_at = time.monotonic()

    def mark_turn_handler_end(self) -> None:
        if not self._enabled or self._active is None:
            return
        self._active.turn_end_at = time.monotonic()

    # ── Metrics bridge ─────────────────────────────────────────────────────

    def _on_metrics(self, ev) -> None:  # noqa: ANN001
        m = getattr(ev, "metrics", None)
        if m is None:
            return
        kind = getattr(m, "type", "")
        if kind == "stt_metrics":
            audio = getattr(m, "audio_duration", 0.0) or 0.0
            duration = getattr(m, "duration", 0.0) or 0.0
            if audio > 0.001 or duration > 0.001:
                self._latest_stt_audio = audio
                self._latest_stt_proc = duration
                if self._active is not None and self._active.stt_final_at:
                    self._active.stt_audio = audio
                    self._active.stt_proc = duration
            return

        if kind == "tts_metrics" and self._active is None:
            # Greeting / proactive tutor speech before any user turn.
            ttfb = getattr(m, "ttfb", None)
            audio = getattr(m, "audio_duration", None)
            total = getattr(m, "duration", None)
            if ttfb is not None or audio is not None:
                self.mark_tutor_response_done(ttfb=ttfb, audio=audio, total=total)
                logger.info(
                    "[Cycle]   ★ greeting_tts   ttfb=%s  audio=%s  total=%s  (awaiting first user speech)",
                    _ms(ttfb),
                    _sec(audio),
                    _ms(total),
                )
            return

        sid = getattr(m, "speech_id", None) or ""
        if not sid:
            return
        if self._active is None and kind == "eou_metrics":
            self._active = _ActiveUserTurn(stt_final_at=time.monotonic())
        if self._active is None:
            return
        tm = self._active.metrics
        if tm.speech_id and tm.speech_id != sid:
            return
        tm.speech_id = sid

        if kind == "eou_metrics":
            tm.eou = getattr(m, "end_of_utterance_delay", None)
            tm.transcription_delay = getattr(m, "transcription_delay", None)
            tm.on_user_turn_delay = getattr(m, "on_user_turn_completed_delay", None)
        elif kind == "llm_metrics":
            tm.llm_ttft = getattr(m, "ttft", None)
            tm.llm_total = getattr(m, "duration", None)
            tm.llm_in = getattr(m, "prompt_tokens", 0) or 0
            tm.llm_out = getattr(m, "completion_tokens", 0) or 0
        elif kind == "tts_metrics":
            tm.tts_ttfb = getattr(m, "ttfb", None)
            tm.tts_total = getattr(m, "duration", None)
            tm.tts_audio = getattr(m, "audio_duration", None)
            if tm.ready_to_emit():
                self._emit_cycle()

    def _emit_cycle(self) -> None:
        if self._active is None:
            return
        turn = self._active
        tm = turn.metrics
        now = time.monotonic()
        self._cycle_num += 1

        ctx = self._context_fn() if self._context_fn else {}
        mode = ctx.get("mode") or "?"
        word = ctx.get("word") or "?"
        idx = ctx.get("word_index")

        wait_gap = None
        if self._prev_tutor_done_at is not None and turn.user_speech_at is not None:
            wait_gap = turn.user_speech_at - self._prev_tutor_done_at

        turn_handler_ms = None
        if turn.turn_start_at is not None and turn.turn_end_at is not None:
            turn_handler_ms = (turn.turn_end_at - turn.turn_start_at) * 1000.0

        round_trip = None
        if self._prev_tutor_done_at is not None:
            round_trip = now - self._prev_tutor_done_at

        said = turn.stt_text or "—"
        if len(said) > 80:
            said = said[:77] + "…"

        logger.info(
            "[Cycle] #%d mode=%s word=%s idx=%s",
            self._cycle_num,
            mode,
            word,
            idx if idx is not None else "—",
        )
        logger.info(
            "[Cycle]   ① prev_tutor     ttfb=%s  audio=%s  total=%s",
            _ms(self._prev_tts_ttfb),
            _sec(self._prev_tts_audio),
            _ms(self._prev_tts_total),
        )
        logger.info(
            "[Cycle]   ② wait→user      gap=%s  (tutor_done → mic_active)",
            _sec(wait_gap),
        )
        logger.info(
            '[Cycle]   ③ user_stt       audio=%s  proc=%s  said="%s"',
            _sec(turn.stt_audio),
            _ms(turn.stt_proc),
            said,
        )
        logger.info(
            "[Cycle]   ④ eou            silence=%s  transcript=%s  turn_cb=%s  speech=%s",
            _ms(tm.eou),
            _ms(tm.transcription_delay),
            _ms(tm.on_user_turn_delay),
            _short_speech_id(tm.speech_id),
        )
        logger.info(
            "[Cycle]   ⑤ turn_handler   %s  (scoring + publish + reply setup)",
            _ms(turn_handler_ms / 1000.0 if turn_handler_ms is not None else None),
        )
        logger.info(
            "[Cycle]   ⑥ llm            ttft=%s  total=%s  in=%d out=%d",
            _ms(tm.llm_ttft),
            _ms(tm.llm_total),
            tm.llm_in,
            tm.llm_out,
        )
        logger.info(
            "[Cycle]   ⑦ response_tts   ttfb=%s  audio=%s  total=%s",
            _ms(tm.tts_ttfb),
            _sec(tm.tts_audio),
            _ms(tm.tts_total),
        )
        logger.info(
            "[Cycle]   ══ round_trip    %s  (prev_tutor_done → response_tts_done)",
            _sec(round_trip),
        )

        # This response becomes ① for the next user turn.
        self._prev_tts_ttfb = tm.tts_ttfb
        self._prev_tts_audio = tm.tts_audio
        self._prev_tts_total = tm.tts_total
        self._prev_tutor_done_at = now
        self._active = None
        self._latest_stt_audio = None
        self._latest_stt_proc = None


class LatencyLogger:
    """Collect + pretty-print STT / LLM / TTS / EOU latencies for each turn.

    Why this class over the framework's built-in ``metrics.log_metrics``:
      * Aligned, single-line output per stage (easy to read while a lesson runs)
      * Per-turn ``TURN user→tutor=Xms`` rollup that adds the three perceived
        latencies a child actually experiences (silence-detect → first audio).
      * Rolling 20-event averages so you can answer "is STT fast right now?"
        without grepping back through the stream.
      * Per-session totals at shutdown for cost / usage sanity checks.
    """

    _WINDOW = 20  # rolling-average window size (last N events of each kind)

    def __init__(self) -> None:
        self.usage_collector = metrics.UsageCollector()

        # Per-speech_id memo so we can print one TURN rollup once all three
        # parts (EOU silence-delay, LLM TTFT, TTS TTFB) are known.
        self._eou_by_speech: dict[str, float] = {}
        self._llm_ttft_by_speech: dict[str, float] = {}
        self._tts_ttfb_by_speech: dict[str, float] = {}

        # Rolling windows for the periodic summary line.
        self._stt_window: deque[float] = deque(maxlen=self._WINDOW)
        self._llm_window: deque[float] = deque(maxlen=self._WINDOW)
        self._tts_window: deque[float] = deque(maxlen=self._WINDOW)
        self._eou_window: deque[float] = deque(maxlen=self._WINDOW)

        self._enabled = (
            os.getenv("KID_TUTOR_LOG_PERF", "1") or "1"
        ).strip().lower() in ("1", "true", "yes", "on")

        try:
            self._summary_s = max(
                5.0, float(os.getenv("KID_TUTOR_PERF_SUMMARY_S", "30"))
            )
        except ValueError:
            self._summary_s = 30.0

        self._summary_task: asyncio.Task | None = None

    # ── Public API ─────────────────────────────────────────────────────────

    def attach(self, session) -> None:
        """Subscribe to the session's ``metrics_collected`` events.

        Safe to call once per session, from sync or async context.
        """
        if not self._enabled:
            logger.info(
                "[Latency] disabled — set KID_TUTOR_LOG_PERF=1 to enable"
            )
            return

        logger.info(
            "[Latency] enabled — per-turn STT/LLM/TTS/EOU + rolling summary every %.0fs "
            "(KID_TUTOR_LOG_PERF=0 to silence)",
            self._summary_s,
        )

        @session.on("metrics_collected")
        def _on_metrics_collected(ev) -> None:  # noqa: ANN001
            self._handle_event(ev)

        # Start the rolling-summary background loop.
        self._summary_task = asyncio.ensure_future(self._summary_loop())

    async def shutdown(self) -> None:
        """Cancel the rolling task and emit one final SESSION TOTALS line.

        Designed to be passed to ``JobContext.add_shutdown_callback`` (which
        ``await``s its callbacks) — hence ``async def`` returning ``None``.
        """
        if self._summary_task is not None and not self._summary_task.done():
            self._summary_task.cancel()
            try:
                await self._summary_task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            s = self.usage_collector.get_summary()
        except Exception as e:
            logger.debug("[Latency] get_summary failed: %s", e)
            return

        logger.info(
            "[Latency] SESSION TOTALS: "
            "stt_audio=%5.1fs  llm_in_tokens=%d  llm_out_tokens=%d  "
            "tts_chars=%d  tts_audio=%5.1fs",
            getattr(s, "stt_audio_duration", 0.0) or 0.0,
            getattr(s, "llm_prompt_tokens", 0) or 0,
            getattr(s, "llm_completion_tokens", 0) or 0,
            getattr(s, "tts_characters_count", 0) or 0,
            getattr(s, "tts_audio_duration", 0.0) or 0.0,
        )

    def summary(self):
        """Backwards-compatible accessor returning ``UsageCollector`` summary."""
        return self.usage_collector.get_summary()

    # ── Internals ──────────────────────────────────────────────────────────

    @staticmethod
    def _avg(window: deque[float]) -> float:
        return (sum(window) / len(window)) if window else 0.0

    def _handle_event(self, ev) -> None:  # noqa: ANN001
        m = getattr(ev, "metrics", None)
        if m is None:
            return

        # Always feed the official UsageCollector for accurate cost totals,
        # even if our toggle silenced individual lines.
        try:
            self.usage_collector.collect(m)
        except Exception as e:
            logger.debug("[Latency] usage_collector.collect: %s", e)

        kind = getattr(m, "type", "")
        sid = getattr(m, "speech_id", None)
        sid_short = _short_speech_id(sid)

        if kind == "stt_metrics":
            self._log_stt(m)
        elif kind == "eou_metrics":
            self._log_eou(m, sid, sid_short)
        elif kind == "llm_metrics":
            self._log_llm(m, sid, sid_short)
        elif kind == "tts_metrics":
            self._log_tts(m, sid, sid_short)

        # Once all three perceived latencies for this speech are in, log the
        # user-perceived TURN total once and forget the speech_id.
        if (
            sid
            and sid in self._eou_by_speech
            and sid in self._llm_ttft_by_speech
            and sid in self._tts_ttfb_by_speech
        ):
            eou = self._eou_by_speech.pop(sid)
            llm = self._llm_ttft_by_speech.pop(sid)
            tts = self._tts_ttfb_by_speech.pop(sid)
            logger.info(
                "[Latency] TURN  user→tutor=%s  "
                "(eou=%s + llm_ttft=%s + tts_ttfb=%s)   speech=%s",
                _ms(eou + llm + tts),
                _ms(eou),
                _ms(llm),
                _ms(tts),
                sid_short,
            )

    # ── Per-stage formatters ──────────────────────────────────────────────

    def _log_stt(self, m) -> None:  # noqa: ANN001
        audio = getattr(m, "audio_duration", 0.0) or 0.0
        duration = getattr(m, "duration", 0.0) or 0.0
        req = getattr(m, "request_id", "") or ""
        # Skip Deepgram's WS-handshake / heartbeat events: those fire an
        # STTMetrics with audio=0, duration=0 and no request_id, and only
        # add noise to the log.
        if audio <= 0.001 and duration <= 0.001 and not req:
            return
        self._stt_window.append(duration)
        logger.info(
            "[Latency] STT   audio=%5.2fs  duration=%s   model=%s   req=%s",
            audio,
            _ms(duration),
            self._friendly_label(getattr(m, "label", "?")),
            req[:8] or "—",
        )

    def _log_eou(self, m, sid: str | None, sid_short: str) -> None:  # noqa: ANN001
        eou = getattr(m, "end_of_utterance_delay", 0.0) or 0.0
        self._eou_window.append(eou)
        if sid:
            self._eou_by_speech[sid] = eou
        logger.info(
            "[Latency] EOU   end =%s  transcript=%s  on_user_turn=%s   speech=%s",
            _ms(eou),
            _ms(getattr(m, "transcription_delay", None)),
            _ms(getattr(m, "on_user_turn_completed_delay", None)),
            sid_short,
        )

    def _log_llm(self, m, sid: str | None, sid_short: str) -> None:  # noqa: ANN001
        ttft = getattr(m, "ttft", None) or 0.0
        self._llm_window.append(ttft)
        if sid:
            self._llm_ttft_by_speech[sid] = ttft
        logger.info(
            "[Latency] LLM   ttft=%s  total =%s   "
            "in=%4d out=%3d tps=%5.1f   speech=%s",
            _ms(ttft),
            _ms(getattr(m, "duration", None)),
            getattr(m, "prompt_tokens", 0) or 0,
            getattr(m, "completion_tokens", 0) or 0,
            getattr(m, "tokens_per_second", 0.0) or 0.0,
            sid_short,
        )

    def _log_tts(self, m, sid: str | None, sid_short: str) -> None:  # noqa: ANN001
        ttfb = getattr(m, "ttfb", None) or 0.0
        self._tts_window.append(ttfb)
        if sid:
            self._tts_ttfb_by_speech[sid] = ttfb
        logger.info(
            "[Latency] TTS   ttfb=%s  total =%s   "
            "audio=%5.2fs chars=%4d   speech=%s",
            _ms(ttfb),
            _ms(getattr(m, "duration", None)),
            getattr(m, "audio_duration", 0.0) or 0.0,
            getattr(m, "characters_count", 0) or 0,
            sid_short,
        )

    @staticmethod
    def _friendly_label(label: str) -> str:
        """Turn ``livekit.plugins.deepgram.stt.STT`` → ``Deepgram/nova-3``-ish."""
        if not label:
            return "?"
        # Best-effort: strip the package prefix, keep last 1-2 components.
        parts = [p for p in label.split(".") if p]
        if len(parts) >= 3 and parts[0:2] == ["livekit", "plugins"]:
            # ``livekit.plugins.deepgram.stt.STT`` → ``deepgram.STT``
            return f"{parts[2]}.{parts[-1]}"
        return label

    async def _summary_loop(self) -> None:
        """Print rolling averages every ``self._summary_s`` seconds."""
        try:
            while True:
                await asyncio.sleep(self._summary_s)
                if not (
                    self._stt_window
                    or self._llm_window
                    or self._tts_window
                    or self._eou_window
                ):
                    continue
                logger.info(
                    "[Latency] ROLLING (last≤%d):  "
                    "stt_avg=%s (n=%2d)  |  "
                    "llm_ttft_avg=%s (n=%2d)  |  "
                    "tts_ttfb_avg=%s (n=%2d)  |  "
                    "eou_avg=%s (n=%2d)",
                    self._WINDOW,
                    _ms(self._avg(self._stt_window)),
                    len(self._stt_window),
                    _ms(self._avg(self._llm_window)),
                    len(self._llm_window),
                    _ms(self._avg(self._tts_window)),
                    len(self._tts_window),
                    _ms(self._avg(self._eou_window)),
                    len(self._eou_window),
                )
        except asyncio.CancelledError:
            return
