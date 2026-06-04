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
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import deque

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
