import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useMaybeRoomContext, useLocalParticipant } from "@livekit/components-react";
import { ConnectionState, RoomEvent } from "livekit-client";
import { lessonModeUi } from "./lessonModeUi";

const KID_TUTOR_DATA_TOPIC = "kidtutor";

/** Derive API origin from token URL, e.g. http://127.0.0.1:5000/token → http://127.0.0.1:5000 */
export function curriculumApiBase(tokenUrl) {
  try {
    const u = new URL(tokenUrl);
    let p = u.pathname.replace(/\/?token\/?$/i, "") || "/";
    if (p !== "/" && p.endsWith("/")) p = p.slice(0, -1);
    return `${u.origin}${p === "/" ? "" : p}`;
  } catch {
    return "";
  }
}

function speakText(text) {
  if (typeof window === "undefined" || !window.speechSynthesis || !text) return;
  try {
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 0.95;
    window.speechSynthesis.speak(u);
  } catch {
    /* ignore */
  }
}

/**
 * Shows the current lesson word + image (from token_server /curriculum + /curriculum-media).
 * When `avatarSlot` is set, uses the split layout: tutor video (left), lesson picture card (right).
 */
export default function LessonPicturePanel({
  apiBase,
  topicSlug,
  tutorLabel,
  childName,
  lessonMode = "vocabulary",
  avatarSlot,
  onLessonComplete,
}) {
  const room = useMaybeRoomContext();
  const { isMicrophoneEnabled } = useLocalParticipant();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState(null);
  const [index, setIndex] = useState(0);
  const [pronunciationHint, setPronunciationHint] = useState(null);
  const [pictureFocus, setPictureFocus] = useState(false);
  const [dockActive, setDockActive] = useState("picture");
  const [lessonDone, setLessonDone] = useState(null);
  const [redirectIn, setRedirectIn] = useState(null);
  const encRef = useRef(typeof TextEncoder !== "undefined" ? new TextEncoder() : null);
  /** Skip echoing agent-driven carousel updates back as lesson_index. */
  const suppressLessonIndexPublishRef = useRef(false);
  const completeTimerRef = useRef(null);
  const tickTimerRef = useRef(null);

  useEffect(() => {
    setDockActive("picture");
  }, [index]);

  useEffect(() => {
    if (!apiBase || !topicSlug) {
      setItems([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setFetchError(null);
    fetch(`${apiBase}/curriculum/${encodeURIComponent(topicSlug)}`)
      .then((res) => {
        if (!res.ok) throw new Error("Could not load lesson pictures");
        return res.json();
      })
      .then((data) => {
        if (cancelled) return;
        setItems(Array.isArray(data.items) ? data.items : []);
        setIndex(0);
      })
      .catch((e) => {
        if (!cancelled) setFetchError(e.message || "Network error");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [apiBase, topicSlug]);

  const n = items.length;
  const current = n ? items[Math.min(index, n - 1)] : null;

  const applyAgentPictureIndex = useCallback(
    (rawIndex) => {
      if (!Number.isFinite(Number(rawIndex)) || n < 1) return;
      const maxI = Math.max(0, n - 1);
      const requested = Math.max(0, Math.min(Math.floor(Number(rawIndex)), maxI));
      setIndex((prev) => {
        let target = requested;
        if (target > prev + 1) {
          target = prev + 1;
        }
        if (prev === target) {
          suppressLessonIndexPublishRef.current = false;
          return prev;
        }
        suppressLessonIndexPublishRef.current = true;
        return target;
      });
    },
    [n]
  );

  const prev = useCallback(() => setIndex((i) => Math.max(0, i - 1)), []);
  const next = useCallback(() => setIndex((i) => Math.min(n - 1, i + 1)), [n]);

  /** Push word index to the Python agent so prompts + scoring match the picture card. */
  useEffect(() => {
    if (!room || n < 1 || room.state !== ConnectionState.Connected) return;
    if (suppressLessonIndexPublishRef.current) {
      suppressLessonIndexPublishRef.current = false;
      return;
    }
    const enc = encRef.current;
    if (!enc) return;
    const payload = enc.encode(
      JSON.stringify({
        type: "lesson_index",
        index,
        topicSlug,
      })
    );
    room.localParticipant
      .publishData(payload, { reliable: true, topic: KID_TUTOR_DATA_TOPIC })
      .catch(() => {});
  }, [room, n, index, topicSlug]);

  useEffect(() => {
    if (!room) return undefined;
    const onData = (payload, participant, _kind, topic) => {
      if (topic !== KID_TUTOR_DATA_TOPIC) return;
      if (!participant || participant.isLocal) return;
      try {
        const text = new TextDecoder().decode(payload);
        const msg = JSON.parse(text);
        if (msg.topicSlug !== topicSlug) return;
        if (msg.type === "pronunciation_result") {
          setPronunciationHint(msg);
          if (Number.isFinite(Number(msg.pictureIndex))) {
            applyAgentPictureIndex(msg.pictureIndex);
          }
        }
        if (msg.type === "lesson_set_index") {
          applyAgentPictureIndex(msg.index);
        }
        if (msg.type === "lesson_complete") {
          // Already handled (e.g., duplicate signal)? skip.
          if (completeTimerRef.current) return;
          const requestedDelay = Number.isFinite(Number(msg.redirectAfterMs))
            ? Math.max(2000, Math.min(30000, Math.floor(Number(msg.redirectAfterMs))))
            : 11000;
          setLessonDone({
            totalWords: Number(msg.totalWords) || n,
          });
          setRedirectIn(Math.ceil(requestedDelay / 1000));
          // Live countdown for the overlay copy.
          tickTimerRef.current = window.setInterval(() => {
            setRedirectIn((prev) => {
              if (prev === null) return null;
              return prev > 1 ? prev - 1 : 0;
            });
          }, 1000);
          completeTimerRef.current = window.setTimeout(() => {
            completeTimerRef.current = null;
            if (tickTimerRef.current) {
              window.clearInterval(tickTimerRef.current);
              tickTimerRef.current = null;
            }
            if (typeof onLessonComplete === "function") {
              onLessonComplete({
                topicSlug,
                totalWords: Number(msg.totalWords) || n,
              });
            }
          }, requestedDelay);
        }
      } catch {
        /* ignore */
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, topicSlug, n, onLessonComplete, applyAgentPictureIndex]);

  useEffect(() => {
    return () => {
      if (completeTimerRef.current) {
        window.clearTimeout(completeTimerRef.current);
        completeTimerRef.current = null;
      }
      if (tickTimerRef.current) {
        window.clearInterval(tickTimerRef.current);
        tickTimerRef.current = null;
      }
    };
  }, []);

  const displayName = (childName || "friend").trim() || "friend";
  const modeUi = useMemo(() => lessonModeUi(lessonMode), [lessonMode]);
  const flowPhase = pronunciationHint?.flowPhase || "";
  const awaitingCheck =
    lessonMode === "vocabulary" &&
    (pronunciationHint?.awaitingComprehension || flowPhase === "quick_check");
  const promptPhrase = current
    ? awaitingCheck && modeUi.promptQuickCheck
      ? modeUi.promptQuickCheck(current.word)
      : modeUi.prompt(current.word)
    : "";
  const bannerHint =
    awaitingCheck && lessonMode === "vocabulary"
      ? "Answer the tutor's quick question about this word."
      : modeUi.cardHint;
  const listeningBubble =
    awaitingCheck && modeUi.bubbleQuickCheck
      ? modeUi.bubbleQuickCheck
      : modeUi.bubbleListening.replace("{tutor}", tutorLabel);

  const onDockRepeat = () => {
    setDockActive("repeat");
    speakText(promptPhrase);
  };
  const onDockListen = () => {
    setDockActive("listen");
    if (current?.word) speakText(current.word);
  };
  const onDockPicture = () => {
    setDockActive("picture");
    setPictureFocus(true);
    window.setTimeout(() => setPictureFocus(false), 1200);
  };
  const onDockNext = () => {
    setDockActive("next");
    next();
  };

  if (!apiBase || !topicSlug) return null;
  if (loading) {
    return (
      <div className="lesson-visual lesson-visual-loading" role="status">
        Loading pictures…
      </div>
    );
  }
  if (fetchError) {
    return (
      <div className="lesson-visual lesson-visual-error" role="alert">
        {fetchError}
      </div>
    );
  }
  if (!n) return null;

  const showGreatJob =
    pronunciationHint &&
    pronunciationHint.wordIndex === index &&
    pronunciationHint.band === "correct";

  const scoreMessage = (() => {
    if (!pronunciationHint || pronunciationHint.wordIndex !== index) return null;
    if (awaitingCheck && modeUi.scoreQuickCheck) return modeUi.scoreQuickCheck;
    if (pronunciationHint.band === "correct") return modeUi.scoreCorrect;
    if (pronunciationHint.band === "almost") return modeUi.scoreAlmost;
    return modeUi.scoreOther;
  })();

  const scoreBlock =
    pronunciationHint && pronunciationHint.wordIndex === index ? (
      <p
        className={`lesson-visual-score lesson-visual-score--card lesson-visual-score--${pronunciationHint.band || "other"}`}
        role="status"
      >
        {pronunciationHint.score}/100 — {scoreMessage}
        {pronunciationHint.maxedOut ? " · try the next word when you’re ready" : null}
        {pronunciationHint.avatarCue ? (
          <span
            className="lesson-visual-avatar-cue"
            title={`Tutor cue: ${pronunciationHint.avatarCue.emotion || ""} · ${pronunciationHint.avatarCue.animation || ""}`}
          >
            {" "}
            · {pronunciationHint.avatarCue.emotion}
            {pronunciationHint.avatarCue.animation
              ? ` (${pronunciationHint.avatarCue.animation})`
              : null}
          </span>
        ) : null}
      </p>
    ) : null;

  if (!avatarSlot) {
    return (
      <div className="lesson-visual">
        <p className={`lesson-visual-hint ${modeUi.panelClass}`}>
          <span className="lesson-mode-badge">{modeUi.badge}</span>
          {modeUi.cardHint}
        </p>
        <div className="lesson-visual-card">
          {current.imageUrl ? (
            <img
              src={current.imageUrl}
              alt={current.word}
              className="lesson-visual-img"
              loading="lazy"
            />
          ) : (
            <div className="lesson-visual-placeholder">
              <span className="lesson-visual-placeholder-emoji" aria-hidden>
                🖼️
              </span>
              <span className="lesson-visual-placeholder-text">Picture coming soon</span>
            </div>
          )}
          <p className="lesson-visual-word">{current.word}</p>
          {current.caption ? <p className="lesson-visual-caption">{current.caption}</p> : null}
        </div>
        <div className="lesson-visual-controls">
          <button type="button" className="kid-btn kid-btn-secondary" onClick={prev} disabled={index <= 0}>
            ← Back
          </button>
          <span className="lesson-visual-step">
            {index + 1} / {n}
          </span>
          <button
            type="button"
            className="kid-btn kid-btn-secondary"
            onClick={next}
            disabled={index >= n - 1}
          >
            Next →
          </button>
        </div>
        {scoreBlock}
      </div>
    );
  }

  const micListening = Boolean(room && isMicrophoneEnabled);

  return (
    <div className={`tutor-session ${modeUi.panelClass}`}>
      <p className="lesson-mode-banner" role="status">
        <span className="lesson-mode-badge lesson-mode-badge--large">{modeUi.badge}</span>
        <span className="lesson-mode-banner-title">{modeUi.title}</span>
        <span className="lesson-mode-banner-hint">{bannerHint}</span>
      </p>
      {lessonMode === "vocabulary" && modeUi.steps ? (
        <ol
          className={`lesson-vocab-steps${awaitingCheck ? " lesson-vocab-steps--check" : ""}`}
          aria-label="Teaching mode steps"
        >
          {modeUi.steps.map((label, i) => {
            const lastIdx = modeUi.steps.length - 1;
            const active = awaitingCheck ? i === lastIdx : i === 0;
            return (
              <li key={label} className={active ? "is-active" : ""}>
                {label}
              </li>
            );
          })}
        </ol>
      ) : null}
      {lessonMode === "speaking" ? (
        <>
          {modeUi.steps ? (
            <ol
              className="lesson-vocab-steps lesson-vocab-steps--speaking"
              aria-label="Speaking coach steps"
            >
              {modeUi.steps.map((label, i) => (
                <li key={label} className={i === 0 ? "is-active" : ""}>
                  {label}
                </li>
              ))}
            </ol>
          ) : null}
          <p className="lesson-speaking-tag" role="status">
            Coach models a sentence — you repeat clearly, then move on
          </p>
        </>
      ) : null}
      <div className="tutor-session-grid">
        <aside className="tutor-session-avatar-col" aria-label="Your tutor">
          {avatarSlot}
          {showGreatJob ? (
            <div className="tutor-bubble tutor-bubble--feedback" role="status">
              <span className="tutor-bubble-sparkle" aria-hidden>
                ✦
              </span>
              {modeUi.bubbleGreat} {displayName}!
            </div>
          ) : (
            <div className="tutor-bubble tutor-bubble--feedback tutor-bubble--muted">
              <span className="tutor-bubble-sparkle" aria-hidden>
                ✦
              </span>
              {listeningBubble}
            </div>
          )}
          <div
            className={`tutor-bubble tutor-bubble--prompt${!micListening ? " tutor-bubble--warn" : ""}`}
            role="region"
            aria-label="Say this word"
          >
            {promptPhrase}
            {!micListening ? (
              <span className="tutor-bubble-mic-note"> Turn on your mic so {tutorLabel} can hear you.</span>
            ) : null}
          </div>
        </aside>
        <section className="tutor-session-lesson-card" aria-label="Lesson picture">
          <div
            className={`tutor-session-lesson-media${pictureFocus ? " tutor-session-lesson-media--focus" : ""}`}
          >
            {current.imageUrl ? (
              <img
                src={current.imageUrl}
                alt={current.word}
                className="tutor-session-lesson-img"
                loading="lazy"
              />
            ) : (
              <div className="tutor-session-lesson-placeholder">
                <span className="lesson-visual-placeholder-emoji" aria-hidden>
                  🖼️
                </span>
                <span className="lesson-visual-placeholder-text">Picture coming soon</span>
              </div>
            )}
            <div className="tutor-session-lesson-toolbar">
              <span className="tutor-session-word-pill">{current.word}</span>
              <button
                type="button"
                className="tutor-session-listen-chip"
                onClick={onDockListen}
                aria-label={`Listen: ${current.word}`}
                title="Listen to the word"
              >
                <span aria-hidden>🔊</span>
              </button>
            </div>
          </div>
          {lessonMode === "vocabulary" && current.caption ? (
            <p className="tutor-session-caption">{current.caption}</p>
          ) : null}
          {scoreBlock}
          <p className={`tutor-session-mic-hint ${lessonMode === "speaking" ? "tutor-session-mic-hint--speak" : ""}`}>
            <span className="tutor-session-mic-hint-icon" aria-hidden>
              {lessonMode === "speaking" ? "🎤" : "▶"}
            </span>
            {modeUi.micHint.replace("{tutor}", tutorLabel)}
          </p>
        </section>
      </div>
      <nav className="tutor-session-dock" aria-label="Lesson controls">
        <button
          type="button"
          className={`tutor-session-dock-btn${dockActive === "repeat" ? " is-active" : ""}`}
          onClick={onDockRepeat}
        >
          <span className="tutor-session-dock-icon" aria-hidden>
            ↻
          </span>
          <span className="tutor-session-dock-label">Repeat</span>
        </button>
        <button
          type="button"
          className={`tutor-session-dock-btn${dockActive === "listen" ? " is-active" : ""}`}
          onClick={onDockListen}
        >
          <span className="tutor-session-dock-icon" aria-hidden>
            🔊
          </span>
          <span className="tutor-session-dock-label">Listen</span>
        </button>
        <button
          type="button"
          className={`tutor-session-dock-btn${dockActive === "picture" ? " is-active" : ""}`}
          onClick={onDockPicture}
        >
          <span className="tutor-session-dock-icon" aria-hidden>
            🖼
          </span>
          <span className="tutor-session-dock-label">Picture</span>
        </button>
        <button
          type="button"
          className={`tutor-session-dock-btn${dockActive === "next" ? " is-active" : ""}`}
          onClick={onDockNext}
          disabled={index >= n - 1}
        >
          <span className="tutor-session-dock-icon" aria-hidden>
            ›
          </span>
          <span className="tutor-session-dock-label">Next</span>
        </button>
      </nav>
      <p className="tutor-session-step-pill" aria-live="polite">
        Word {index + 1} of {n}
        {index > 0 ? (
          <button type="button" className="tutor-session-step-back" onClick={prev}>
            Previous word
          </button>
        ) : null}
      </p>
      {lessonDone ? (
        <div className="tutor-lesson-complete-overlay" role="dialog" aria-live="assertive">
          <div className="tutor-lesson-complete-card">
            <span className="tutor-lesson-complete-burst" aria-hidden>
              ✦
            </span>
            <h2 className="tutor-lesson-complete-title">
              You did it, {displayName}!
            </h2>
            <p className="tutor-lesson-complete-sub">
              All {lessonDone.totalWords} words finished. {tutorLabel} is saying goodbye…
            </p>
            <p className="tutor-lesson-complete-countdown" aria-live="polite">
              {redirectIn !== null && redirectIn > 0
                ? `Back to lessons in ${redirectIn}s…`
                : "Heading back to lessons…"}
            </p>
            <button
              type="button"
              className="kid-btn kid-btn-primary"
              onClick={() => {
                if (completeTimerRef.current) {
                  window.clearTimeout(completeTimerRef.current);
                  completeTimerRef.current = null;
                }
                if (tickTimerRef.current) {
                  window.clearInterval(tickTimerRef.current);
                  tickTimerRef.current = null;
                }
                if (typeof onLessonComplete === "function") {
                  onLessonComplete({
                    topicSlug,
                    totalWords: lessonDone.totalWords,
                  });
                }
              }}
            >
              Pick a new lesson now
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
