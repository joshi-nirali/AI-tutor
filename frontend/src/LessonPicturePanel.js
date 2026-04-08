import { useEffect, useState, useCallback } from "react";

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
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState(null);
  const [index, setIndex] = useState(0);

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
    </div>
  );
}
