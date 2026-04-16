import { useMemo, useState, useCallback } from "react";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  VideoConference,
  StartAudio,
  useLocalParticipant,
} from "@livekit/components-react";
import "@livekit/components-styles";
import TutorLiveStatus from "./TutorLiveStatus";
import LessonPicturePanel, { curriculumApiBase } from "./LessonPicturePanel";

function sessionSuffix() {
  const a = new Uint8Array(4);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Must render under LiveKitRoomProvider (uses participant hooks). */
function TutorAvatarBlock({ tutorLabel }) {
  const { isMicrophoneEnabled } = useLocalParticipant();
  return (
    <div className="tutor-avatar-video-wrap">
      <div className="tutor-avatar-video-ring">
        <div className="tutor-avatar-video-shell">
          <VideoConference />
        </div>
        <span
          className={`tutor-avatar-video-live-dot${isMicrophoneEnabled ? " is-on" : ""}`}
          title={isMicrophoneEnabled ? "Microphone on" : "Microphone off"}
          aria-label={isMicrophoneEnabled ? "Microphone on" : "Microphone off"}
        />
      </div>
      <p className="tutor-avatar-video-tip">
        Say hello to {tutorLabel}! Short answers work best.
      </p>
      <div className="tutor-start-audio-wrap tutor-start-audio-wrap--avatar">
        <StartAudio label="Tap to turn on sound 🔊" />
      </div>
    </div>
  );
}

/**
 * LiveKit session: child sees/hears the bitHuman avatar published by agent.py.
 * Room name must match agent.py + token_server:
 *   kidtutor-{mode}-{topic}-{tutorSlug}-{sessionId}
 */
export default function TutorRoom({
  livekitUrl,
  tokenBaseUrl,
  mode,
  topicSlug,
  tutorSlug,
  childName,
  tutorLabel,
  onLeave,
}) {
  const [connect, setConnect] = useState(false);
  const [error, setError] = useState(null);

  const slug = (tutorSlug || "leo").toLowerCase().replace(/[^a-z0-9_]/g, "") || "leo";

  const roomName = useMemo(
    () => `kidtutor-${mode}-${topicSlug}-${slug}-${sessionSuffix()}`,
    [mode, topicSlug, slug]
  );

  const identity = useMemo(() => {
    const safe = (childName || "friend").replace(/\W/g, "").slice(0, 12) || "friend";
    return `child-${safe}-${sessionSuffix()}`;
  }, [childName]);

  const [token, setToken] = useState(null);
  const [serverUrl, setServerUrl] = useState(livekitUrl || null);

  const curriculumBase = useMemo(() => curriculumApiBase(tokenBaseUrl), [tokenBaseUrl]);

  const fetchToken = useCallback(async () => {
    setError(null);
    const params = new URLSearchParams({
      room: roomName,
      identity,
      name: childName || "Friend",
    });
    const res = await fetch(`${tokenBaseUrl}?${params.toString()}`);
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `Token error ${res.status}`);
    }
    const data = await res.json();
    if (!data.token) throw new Error("No token in response");
    setToken(data.token);
    if (data.url) setServerUrl(data.url);
  }, [tokenBaseUrl, roomName, identity, childName]);

  const handleStart = async () => {
    try {
      await fetchToken();
      setConnect(true);
    } catch (e) {
      setError(e.message || "Could not connect");
    }
  };

  if (!connect || !token || !serverUrl) {
    return (
      <div className="tutor-connect-panel">
        <p className="tutor-connect-lead">
          When you are ready, tap the big button. Allow the microphone when asked so{" "}
          {tutorLabel} can hear you.
        </p>
        <p className="tutor-room-hint">
          Room: <code>{roomName}</code>
        </p>
        <p className="tutor-grownup-note" role="note">
          Grown-ups: this session uses the microphone and AI voice services — stay nearby and supervise young
          children online.
        </p>
        {error && <p className="tutor-error">{error}</p>}
        <button type="button" className="kid-btn kid-btn-primary kid-btn-xl" onClick={handleStart}>
          Start with {tutorLabel} 🎤
        </button>
        <button type="button" className="kid-btn kid-btn-ghost" onClick={onLeave}>
          Back
        </button>
      </div>
    );
  }

  return (
    <LiveKitRoom
      serverUrl={serverUrl}
      token={token}
      connect
      audio
      video={false}
      onError={(e) => setError(e.message)}
      className="tutor-livekit-root"
    >
      <div className="tutor-livekit-inner tutor-livekit-inner--session">
        <header className="tutor-session-header">
          <button type="button" className="tutor-session-back" onClick={onLeave} aria-label="Leave lesson">
            <span aria-hidden>‹</span>
          </button>
          <div className="tutor-session-header-title">
            <span className="tutor-session-header-star" aria-hidden>
              ★
            </span>
            <span>Leo&apos;s Learning</span>
          </div>
          <div className="tutor-session-header-meta" aria-hidden>
            <span className="tutor-session-level">Level 1</span>
            <span className="tutor-session-level-stars">★☆</span>
          </div>
        </header>
        <TutorLiveStatus tutorLabel={tutorLabel} />
        <LessonPicturePanel
          apiBase={curriculumBase}
          topicSlug={topicSlug}
          tutorLabel={tutorLabel}
          childName={childName}
          avatarSlot={<TutorAvatarBlock tutorLabel={tutorLabel} />}
        />
        <RoomAudioRenderer />
        {error && <p className="tutor-error">{error}</p>}
      </div>
    </LiveKitRoom>
  );
}
