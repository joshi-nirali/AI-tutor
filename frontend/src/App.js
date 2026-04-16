import { useCallback, useEffect, useState } from "react";
import TutorRoom from "./TutorRoom";
import "./kid-tutor.css";

const STORAGE = { name: "kidTutorName", avatar: "kidTutorAvatar" };

const AVATARS = [
  {
    id: "leo",
    emoji: "🦁",
    name: "Leo",
    blurb: "Loves stories & big words",
  },
  {
    id: "luna",
    emoji: "🦉",
    name: "Luna",
    blurb: "Patient with sounds",
  },
  {
    id: "soon",
    emoji: "✨",
    name: "More friends",
    blurb: "Coming soon",
    locked: true,
  },
];

const LESSONS = [
  { slug: "animals", title: "Animals", hint: "Cats, elephants, and more" },
  { slug: "foods", title: "Foods", hint: "Banana, apple, and more" },
  { slug: "colors", title: "Colors", hint: "Rainbows & everyday things" },
  { slug: "shapes", title: "Shapes", hint: "Circles, stars, squares" },
  { slug: "fairytales", title: "Fairy tales", hint: "Kings, castles, magic" },
];

const MODES = [
  {
    id: "vocabulary",
    title: "Learn vocabulary",
    hint: "Meaning, examples, then say the word",
  },
  {
    id: "speaking",
    title: "Speaking practice",
    hint: "Listen and repeat together",
  },
  {
    id: "quiz",
    title: "Quiz mode",
    hint: "Fun questions & gentle scoring",
  },
];

const TOKEN_URL =
  process.env.REACT_APP_TOKEN_SERVER_URL || "http://127.0.0.1:5000/token";
const LIVEKIT_URL_ENV = process.env.REACT_APP_LIVEKIT_URL || undefined;

function loadStored(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

function initialScreen() {
  const n = loadStored(STORAGE.name);
  const a = loadStored(STORAGE.avatar);
  if (n && a) return "home";
  if (n) return "avatar";
  return "name";
}

export default function App() {
  const [screen, setScreen] = useState(initialScreen);
  const [childName, setChildName] = useState(() => loadStored(STORAGE.name));
  const [avatarId, setAvatarId] = useState(
    () => loadStored(STORAGE.avatar) || "leo"
  );
  const [mode, setMode] = useState(null);
  const [topicSlug, setTopicSlug] = useState(null);
  const [draftName, setDraftName] = useState("");

  useEffect(() => {
    if (screen === "name" && childName) setDraftName(childName);
  }, [screen, childName]);

  const persistName = useCallback(() => {
    const trimmed = draftName.trim();
    if (trimmed.length < 1) return;
    setChildName(trimmed);
    try {
      localStorage.setItem(STORAGE.name, trimmed);
    } catch {
      /* ignore */
    }
    setScreen("avatar");
  }, [draftName]);

  const selectAvatar = (a) => {
    if (a.locked) return;
    setAvatarId(a.id);
    try {
      localStorage.setItem(STORAGE.avatar, a.id);
    } catch {
      /* ignore */
    }
    setScreen("home");
  };

  const tutorLabel =
    AVATARS.find((a) => a.id === avatarId && !a.locked)?.name || "Leo";

  const tutorActive = screen === "tutor" && mode && topicSlug;

  return (
    <div className="kid-app">
      <div className={`kid-shell${tutorActive ? " kid-shell--tutor" : ""}`}>
        <header className="kid-brand">
          <h1>Leo&apos;s learning corner</h1>
          <p>Talk, listen, and learn — with a friendly tutor</p>
        </header>

        {screen === "name" && (
          <section className="kid-card">
            <h2>Hi! What should we call you?</h2>
            <label className="kid-label" htmlFor="kid-name">
              Your name
            </label>
            <input
              id="kid-name"
              className="kid-input"
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              placeholder="e.g. John"
              maxLength={24}
              autoComplete="nickname"
            />
            <div className="kid-footer-actions">
              <button
                type="button"
                className="kid-btn kid-btn-primary kid-btn-xl"
                onClick={persistName}
              >
                Continue
              </button>
            </div>
          </section>
        )}

        {screen === "avatar" && (
          <section className="kid-card">
            <h2>Pick your tutor friend</h2>
            <div className="kid-avatar-grid">
              {AVATARS.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className={`kid-avatar-tile ${avatarId === a.id ? "selected" : ""} ${a.locked ? "locked" : ""}`}
                  onClick={() => selectAvatar(a)}
                  disabled={a.locked}
                >
                  <div className="kid-avatar-emoji" aria-hidden>
                    {a.emoji}
                  </div>
                  <div className="kid-avatar-name">{a.name}</div>
                  <div className="kid-avatar-note">{a.blurb}</div>
                </button>
              ))}
            </div>
            <div className="kid-footer-actions">
              <button
                type="button"
                className="kid-btn kid-btn-ghost"
                onClick={() => setScreen("name")}
              >
                Back
              </button>
            </div>
          </section>
        )}

        {screen === "home" && (
          <section className="kid-card">
            <div className="kid-welcome-banner">
              <p>
                Welcome <strong>{childName || "friend"}</strong>
              </p>
              <p>
                Meet your AI tutor <strong>{tutorLabel}</strong>
              </p>
            </div>
            <h2>Choose learning mode</h2>
            <div className="kid-mode-grid">
              {MODES.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className="kid-mode-btn"
                  onClick={() => {
                    setMode(m.id);
                    setScreen("lesson");
                  }}
                >
                  {m.title}
                  <span>{m.hint}</span>
                </button>
              ))}
            </div>
            <div className="kid-progress">
              <div className="kid-progress-label">Progress: Beginner level</div>
              <div className="kid-progress-bar" aria-hidden>
                <div className="kid-progress-fill" />
              </div>
            </div>
            <div className="kid-footer-actions">
              <button
                type="button"
                className="kid-btn kid-btn-ghost"
                onClick={() => setScreen("avatar")}
              >
                Change tutor
              </button>
            </div>
          </section>
        )}

        {screen === "lesson" && mode && (
          <section className="kid-card">
            <h2>Pick a lesson theme</h2>
            <p style={{ color: "var(--kt-muted)", marginTop: 0 }}>
              {MODES.find((x) => x.id === mode)?.title}
            </p>
            <div className="kid-lesson-grid">
              {LESSONS.map((lesson) => (
                <button
                  key={lesson.slug}
                  type="button"
                  className="kid-lesson-btn"
                  onClick={() => {
                    setTopicSlug(lesson.slug);
                    setScreen("tutor");
                  }}
                >
                  {lesson.title}
                  <small>{lesson.hint}</small>
                </button>
              ))}
            </div>
            <div className="kid-footer-actions">
              <button
                type="button"
                className="kid-btn kid-btn-ghost"
                onClick={() => setScreen("home")}
              >
                Back
              </button>
            </div>
          </section>
        )}

        {screen === "tutor" && mode && topicSlug && (
          <section className="kid-card">
            <TutorRoom
              livekitUrl={LIVEKIT_URL_ENV}
              tokenBaseUrl={TOKEN_URL}
              mode={mode}
              topicSlug={topicSlug}
              tutorSlug={avatarId === "soon" ? "leo" : avatarId}
              childName={childName}
              tutorLabel={tutorLabel}
              onLeave={() => {
                setScreen("home");
                setTopicSlug(null);
                setMode(null);
              }}
            />
          </section>
        )}

        {process.env.NODE_ENV === "development" && (
          <p
            style={{
              textAlign: "center",
              fontSize: "0.8rem",
              color: "var(--kt-muted)",
              marginTop: "1.5rem",
            }}
          >
            Grown-ups: run <code>python token_server.py</code> and{" "}
            <code>python agent.py dev</code>, then open this app. Without bitHuman yet, use{" "}
            <code>KID_TUTOR_USE_AVATAR=0</code> in <code>.env</code> (voice-only).
          </p>
        )}
      </div>
    </div>
  );
}
