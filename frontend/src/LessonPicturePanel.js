import { useEffect, useState, useCallback, useRef } from "react";
import { useMaybeRoomContext, useLocalParticipant } from "@livekit/components-react";
import { ConnectionState, RoomEvent } from "livekit-client";

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
 * When `avatarSlot` is set, uses the split “avatar left / picture right” session layout.
 */
export default function LessonPicturePanel({
  apiBase,
  topicSlug,
  tutorLabel,
  childName,
  avatarSlot,
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
  const encRef = useRef(typeof TextEncoder !== "undefined" ? new TextEncoder() : null);

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

  const prev = useCallback(() => setIndex((i) => Math.max(0, i - 1)), []);
  const next = useCallback(() => setIndex((i) => Math.min(n - 1, i + 1)), [n]);

  /** Push word index to the Python agent so prompts + scoring match the picture card. */
  useEffect(() => {
    if (!room || n < 1 || room.state !== ConnectionState.Connected) return;
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
        }
        if (msg.type === "lesson_set_index" && Number.isFinite(Number(msg.index))) {
          const maxI = Math.max(0, n - 1);
          const idx = Math.max(0, Math.min(Math.floor(Number(msg.index)), maxI));
          setIndex(idx);
        }
      } catch {
        /* ignore */
      }
    };
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, topicSlug, n]);

  const displayName = (childName || "friend").trim() || "friend";
  const promptPhrase = current
    ? `Can you say “${current.word}”?`
    : "";

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

  const scoreBlock =
    pronunciationHint && pronunciationHint.wordIndex === index ? (
      <p className="lesson-visual-score lesson-visual-score--card" role="status">
        {pronunciationHint.band === "correct"
          ? `${pronunciationHint.score}/100 — nice!`
          : `${pronunciationHint.score}/100 · keep practicing`}
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
        <p className="lesson-visual-hint">
          Look at the picture! Tap <strong>Next</strong> when you and {tutorLabel} are ready for the next
          word.
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
    <div className="tutor-session">
      <div className="tutor-session-grid">
        <aside className="tutor-session-avatar-col" aria-label="Your tutor">
          {showGreatJob ? (
            <div className="tutor-bubble tutor-bubble--feedback" role="status">
              <span className="tutor-bubble-sparkle" aria-hidden>
                ✦
              </span>
              Great job, {displayName}!
            </div>
          ) : (
            <div className="tutor-bubble tutor-bubble--feedback tutor-bubble--muted" aria-hidden>
              <span className="tutor-bubble-sparkle">✦</span>
              {tutorLabel} is listening…
            </div>
          )}
          {avatarSlot}
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
          {current.caption ? <p className="tutor-session-caption">{current.caption}</p> : null}
          {scoreBlock}
          <p className="tutor-session-mic-hint">
            <span className="tutor-session-mic-hint-icon" aria-hidden>
              ▶
            </span>
            Tap the microphone and say the word clearly!
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
    </div>
  );
}
