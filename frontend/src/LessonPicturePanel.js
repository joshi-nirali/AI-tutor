import { useEffect, useState, useCallback, useRef } from "react";
import { useMaybeRoomContext } from "@livekit/components-react";
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

/**
 * Shows the current lesson word + image (from token_server /curriculum + /curriculum-media).
 */
export default function LessonPicturePanel({ apiBase, topicSlug, tutorLabel }) {
  const room = useMaybeRoomContext();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState(null);
  const [index, setIndex] = useState(0);
  const [pronunciationHint, setPronunciationHint] = useState(null);
  const encRef = useRef(typeof TextEncoder !== "undefined" ? new TextEncoder() : null);

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
      {pronunciationHint && pronunciationHint.wordIndex === index ? (
        <p className="lesson-visual-score" role="status">
          {pronunciationHint.band === "correct"
            ? `Great job — ${pronunciationHint.score}/100`
            : `${pronunciationHint.score}/100 · keep practicing`}
          {pronunciationHint.maxedOut ? " · take a tiny break or try the next word when you’re ready" : null}
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
      ) : null}
    </div>
  );
}
