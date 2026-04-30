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
    quote: "Roar! I'm Leo. Let's learn and play together!",
    ring: "lavender",
  },
  {
    id: "luna",
    emoji: "🦉",
    name: "Luna",
    blurb: "Patient with sounds",
    quote: "Hoot! I'll help you with every sound.",
    ring: "mint",
  },
  {
    id: "cub",
    emoji: "🐆",
    name: "Cub",
    blurb: "Playful lion cub friend",
    quote: "Hi! I'm Cub — let's learn together!",
    ring: "sun",
  },
];

const LESSONS = [
  { slug: "animals", title: "Animals", hint: "Cats, elephants, and more", emoji: "🐾", theme: "amber" },
  { slug: "foods", title: "Foods", hint: "Banana, apple, and more", emoji: "🍎", theme: "peach" },
  { slug: "colors", title: "Colors", hint: "Rainbows & everyday things", emoji: "🌈", theme: "sky" },
  { slug: "shapes", title: "Shapes", hint: "Circles, stars, squares", emoji: "⭐", theme: "lilac" },
  { slug: "fairytales", title: "Fairy tales", hint: "Kings, castles, magic", emoji: "🏰", theme: "pink" },
  { slug: "numbers", title: "Numbers", hint: "Count from one to ten", emoji: "🔢", theme: "amber" },
  { slug: "body_parts", title: "Body Parts", hint: "Head, hands, and toes", emoji: "🖐️", theme: "peach" },
  { slug: "weather", title: "Weather", hint: "Sun, rain, and rainbows", emoji: "☀️", theme: "sky" },
  { slug: "vehicles", title: "Vehicles", hint: "Cars, trains, and planes", emoji: "🚗", theme: "lilac" },
  { slug: "fruits", title: "Fruits", hint: "Yummy fruits to learn", emoji: "🍓", theme: "pink" },
];

const MODES = [
  {
    id: "vocabulary",
    title: "Learn vocabulary",
    hint: "Discover new words with fun pictures!",
    emoji: "📖",
    theme: "vocab",
  },
  {
    id: "speaking",
    title: "Speaking practice",
    hint: "Say it out loud with your tutor friend!",
    emoji: "🎤",
    theme: "speak",
  },
  {
    id: "quiz",
    title: "Quiz mode",
    hint: "Show off what you've learned!",
    emoji: "🎯",
    theme: "quiz",
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
  const [avatarId, setAvatarId] = useState(() => {
    const raw = loadStored(STORAGE.avatar) || "leo";
    // Legacy picker id before Cub existed — map to cub so room slug stays valid.
    if (raw === "soon") return "cub";
    return raw;
  });
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
  const tutorEmoji =
    AVATARS.find((a) => a.id === avatarId && !a.locked)?.emoji || "🦁";
  const tutorPick = AVATARS.find((a) => a.id === avatarId) || AVATARS[0];

  const tutorActive = screen === "tutor" && mode && topicSlug;
  const flowWide =
    screen === "name" ||
    screen === "avatar" ||
    screen === "home" ||
    screen === "lesson";

  const scrollToParentNote = () => {
    document.getElementById("kid-parent-note")?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <div className="kid-app">
      <div
        className={`kid-shell${tutorActive ? " kid-shell--tutor" : ""}${flowWide ? " kid-shell--flow" : ""
          }`}
      >
        <nav className="kid-appbar" aria-label="App">
          <div className="kid-appbar-brand">
            <span className="kid-appbar-star" aria-hidden>
              ★
            </span>
            <span className="kid-appbar-title">Leo&apos;s Learning</span>
          </div>
          <button type="button" className="kid-appbar-parents" onClick={scrollToParentNote}>
            <span className="kid-appbar-gear" aria-hidden>
              ⚙
            </span>
            Parents
          </button>
        </nav>

        {screen === "name" && (
          <section className="kid-card kid-card-hero">
            <div className="kid-sparkles" aria-hidden>
              <span>✦</span>
              <span>✦</span>
              <span>✦</span>
            </div>
            <h1 className="kid-hero-heading">Hi! What should we call you?</h1>
            <p className="kid-hero-sub">We&apos;ll cheer you on by name.</p>
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
                className="kid-btn kid-btn-primary kid-btn-xl kid-btn-cta"
                onClick={persistName}
              >
                Continue
              </button>
            </div>
          </section>
        )}

        {screen === "avatar" && (
          <section className="kid-card kid-card-tutor-page">
            <h2 className="kid-page-title">Choose your Tutor</h2>
            <p className="kid-page-sub">Pick a friend to help you learn today!</p>
            <div className="kid-tutor-picker">
              <div className="kid-tutor-picker-grid">
                {AVATARS.map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    className={`kid-avatar-tile kid-avatar-tile--${a.ring} ${avatarId === a.id ? "selected" : ""
                      } ${a.locked ? "locked" : ""}`}
                    onClick={() => selectAvatar(a)}
                    disabled={a.locked}
                  >
                    {!a.locked ? (
                      <span className="kid-avatar-try" aria-hidden>
                        Try me!
                      </span>
                    ) : null}
                    <div className={`kid-avatar-ring kid-avatar-ring--${a.ring}`}>
                      <span className="kid-avatar-emoji" aria-hidden>
                        {a.emoji}
                      </span>
                    </div>
                    <div className="kid-avatar-name">{a.name}</div>
                    <div className="kid-avatar-note">{a.blurb}</div>
                  </button>
                ))}
              </div>
              <aside className="kid-tutor-preview" aria-live="polite">
                <div className={`kid-tutor-preview-glow kid-tutor-preview-glow--${tutorPick.ring}`}>
                  <span className="kid-tutor-preview-emoji" aria-hidden>
                    {tutorPick.emoji}
                  </span>
                  <span className="kid-tutor-preview-sparkle" aria-hidden>
                    ✦
                  </span>
                </div>
                <blockquote className="kid-tutor-preview-quote">&ldquo;{tutorPick.quote}&rdquo;</blockquote>
              </aside>
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
          <section className="kid-card kid-card-flow">
            <div className="kid-hero-banner">
              <p className="kid-hero-line1">
                Welcome,{" "}
                <span className="kid-name-highlight">{childName || "friend"}</span>
                <span className="kid-hero-wave" aria-hidden>
                  {" "}
                  👋
                </span>
              </p>
              <p className="kid-hero-line2">
                Ready for a fun adventure? Meet your AI Tutor{" "}
                <span className="kid-tutor-highlight">
                  {tutorLabel} {tutorEmoji}
                </span>
              </p>
            </div>
            <h2 className="kid-path-heading">
              <span className="kid-path-heading-icon" aria-hidden>
                ▶
              </span>
              Choose a Path
            </h2>
            <div className="kid-path-grid">
              {MODES.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className={`kid-path-card kid-path-card--${m.theme}`}
                  onClick={() => {
                    setMode(m.id);
                    setScreen("lesson");
                  }}
                >
                  <span className="kid-path-card-icon" aria-hidden>
                    {m.emoji}
                  </span>
                  <span className="kid-path-card-title">{m.title}</span>
                  <span className="kid-path-card-hint">{m.hint}</span>
                </button>
              ))}
            </div>
            <div className="kid-level-pill" role="status">
              <span className="kid-level-stars" aria-hidden>
                ☆☆
              </span>
              Level 1 — keep going!
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
          <section className="kid-card kid-card-flow">
            <h2 className="kid-page-title">Pick a lesson theme</h2>
            <p className="kid-page-sub">{MODES.find((x) => x.id === mode)?.title}</p>
            <div className="kid-lesson-path-grid">
              {LESSONS.map((lesson) => (
                <button
                  key={lesson.slug}
                  type="button"
                  className={`kid-lesson-path-card kid-lesson-path-card--${lesson.theme}`}
                  onClick={() => {
                    setTopicSlug(lesson.slug);
                    setScreen("tutor");
                  }}
                >
                  <span className="kid-lesson-path-icon" aria-hidden>
                    {lesson.emoji}
                  </span>
                  <span className="kid-lesson-path-title">{lesson.title}</span>
                  <small className="kid-lesson-path-hint">{lesson.hint}</small>
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
          <section className="kid-card kid-card-tutor-live">
            <TutorRoom
              livekitUrl={LIVEKIT_URL_ENV}
              tokenBaseUrl={TOKEN_URL}
              mode={mode}
              topicSlug={topicSlug}
              tutorSlug={avatarId}
              childName={childName}
              tutorLabel={tutorLabel}
              onLeave={() => {
                setScreen("home");
                setTopicSlug(null);
                setMode(null);
              }}
              onLessonComplete={() => {
                // All words finished — drop the child back on the categories
                // grid for the same path (vocabulary / speaking / quiz) so they
                // can immediately pick another lesson without re-choosing mode.
                // Unmounting this section cleanly disconnects the LiveKit room.
                setScreen("lesson");
                setTopicSlug(null);
              }}
            />
          </section>
        )}

        <p id="kid-parent-note" className="kid-parent-note" role="note">
          <span className="kid-parent-lock" aria-hidden>
            🔒
          </span>
          Grown-ups: stay nearby while your child uses the microphone and AI tutor. For setup tips, see the
          README in this project.
        </p>

        {process.env.NODE_ENV === "development" && (
          <p className="kid-dev-hint">
            Dev: run <code>python token_server.py</code> and <code>python agent.py dev</code>. Voice-only:{" "}
            <code>KID_TUTOR_USE_AVATAR=0</code> in <code>.env</code>.
          </p>
        )}
      </div>
    </div>
  );
}
