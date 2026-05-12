import { useMemo, useState, useCallback, useEffect, useRef } from "react";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  StartAudio,
  useLocalParticipant,
  useMaybeRoomContext,
  useParticipantTracks,
  useParticipants,
  useRoomContext,
  ParticipantTile,
  ControlBar,
  LayoutContextProvider,
} from "@livekit/components-react";
import { ConnectionState, RoomEvent, Track } from "livekit-client";
import "@livekit/components-styles";
import TutorLiveStatus from "./TutorLiveStatus";
import LessonPicturePanel, { curriculumApiBase } from "./LessonPicturePanel";

const KID_TUTOR_DATA_TOPIC = "kidtutor";

/** Hard cap if nothing dismisses the loader (keep reasonable — was 45s and felt stuck). */
const AVATAR_SETUP_LOADER_MAX_MS = 16000;

/** After LiveKit is connected and at least one remote participant joins (agent / avatar), hide loader. */
const AVATAR_SETUP_DISMISS_AFTER_AGENT_MS = 4000;

/** Browser mic processing: cuts fan/room noise; echo cancellation helps laptop speakers. */
const KID_MIC_CAPTURE_OPTIONS = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  /** Stronger voice isolation when the browser supports it (Chrome). */
  voiceIsolation: true,
};

/**
 * Worker identity is `agent-…` (OpenAI session). bitHuman joins as `bithuman-avatar-agent`.
 * Never treat the worker as the tutor video tile (was falling back when only `agent-*` had a placeholder).
 */
function pickTutorParticipant(remotes) {
  if (!remotes.length) return null;
  const bithuman = remotes.find(
    (p) => /bithuman/i.test(p.identity || "") || /bithuman/i.test(p.name || "")
  );
  if (bithuman) return bithuman;
  return remotes.find((p) => !/^agent-/i.test(p.identity || "")) || null;
}

function onlyLessonAgentRemotes(remotes) {
  return remotes.length > 0 && remotes.every((p) => /^agent-/i.test(p.identity || ""));
}

/** Non-local participants; `useParticipants` subscribes to join/leave so this list stays fresh. */
function useRemoteParticipantsFromRoom() {
  const all = useParticipants();
  return useMemo(() => all.filter((p) => !p.isLocal), [all]);
}

/**
 * Sends the name from the home screen to the agent so one device can serve many kids reliably.
 * Retries once so the message arrives after the agent subscribes to data.
 */
function PublishChildProfile({ topicSlug, childName }) {
  const room = useMaybeRoomContext();
  const encRef = useRef(typeof TextEncoder !== "undefined" ? new TextEncoder() : null);

  useEffect(() => {
    if (!room || room.state !== ConnectionState.Connected) return undefined;
    const enc = encRef.current;
    if (!enc) return undefined;
    const raw = (childName || "").trim();
    if (!raw) return undefined;

    const publish = () => {
      const payload = enc.encode(
        JSON.stringify({
          type: "child_profile",
          topicSlug,
          childName: raw,
        })
      );
      room.localParticipant
        .publishData(payload, { reliable: true, topic: KID_TUTOR_DATA_TOPIC })
        .catch(() => {});
    };

    publish();
    const retry = window.setTimeout(publish, 900);
    return () => window.clearTimeout(retry);
  }, [room, room.state, topicSlug, childName]);

  return null;
}

/** Kid flow: browser may allow the mic OS-wide but LiveKit still starts muted — publish on connect. */
function TutorEnableMicOnConnect() {
  const room = useRoomContext();
  useEffect(() => {
    const enable = () => {
      if (room.state !== ConnectionState.Connected) return;
      room.localParticipant.setMicrophoneEnabled(true).catch(() => {});
    };
    room.on(RoomEvent.Connected, enable);
    room.on(RoomEvent.ConnectionStateChanged, enable);
    enable();
    return () => {
      room.off(RoomEvent.Connected, enable);
      room.off(RoomEvent.ConnectionStateChanged, enable);
    };
  }, [room]);
  return null;
}

/**
 * Full-screen overlay until any of:
 *   - Agent sends input_speech_started (first STT from child), or
 *   - Room is connected + a remote participant is present for AVATAR_SETUP_DISMISS_AFTER_AGENT_MS
 *     (agent joined — no need to wait for speech), or
 *   - AVATAR_SETUP_LOADER_MAX_MS safety cap.
 * Content stays mounted; overlay uses pointer-events: none so taps reach StartAudio under it.
 */
function TutorAvatarSetupGate({ tutorLabel, topicSlug, show, onDismiss }) {
  const room = useMaybeRoomContext();
  const participants = useParticipants();
  const remoteParticipantCount = useMemo(
    () => participants.filter((p) => !p.isLocal).length,
    [participants]
  );

  useEffect(() => {
    if (!show || !room) return undefined;

    const maxId = window.setTimeout(onDismiss, AVATAR_SETUP_LOADER_MAX_MS);

    const onData = (payload, participant, _kind, topic) => {
      if (topic !== KID_TUTOR_DATA_TOPIC) return;
      if (!participant || participant.isLocal) return;
      try {
        const msg = JSON.parse(new TextDecoder().decode(payload));
        if (msg.type !== "input_speech_started") return;
        if (msg.topicSlug && topicSlug && msg.topicSlug !== topicSlug) return;
        onDismiss();
      } catch {
        /* ignore */
      }
    };

    room.on(RoomEvent.DataReceived, onData);
    return () => {
      window.clearTimeout(maxId);
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [show, room, topicSlug, onDismiss]);

  useEffect(() => {
    if (!show || !room) return undefined;
    if (room.state !== ConnectionState.Connected) return undefined;
    if (remoteParticipantCount < 1) return undefined;
    const id = window.setTimeout(onDismiss, AVATAR_SETUP_DISMISS_AFTER_AGENT_MS);
    return () => window.clearTimeout(id);
  }, [show, room, remoteParticipantCount, onDismiss]);

  if (!show) return null;

  return (
    <div
      className="tutor-avatar-setup-overlay"
      role="status"
      aria-live="polite"
      aria-busy="true"
      aria-labelledby="tutor-avatar-setup-heading"
    >
      <div className="tutor-avatar-setup-card">
        <div className="tutor-avatar-setup-spinner" aria-hidden />
        <p id="tutor-avatar-setup-heading" className="tutor-avatar-setup-title">
          Getting {tutorLabel} ready…
        </p>
        <p className="tutor-avatar-setup-sub">
          This clears when your tutor joins or when you say hi — allow the mic if asked.
        </p>
      </div>
    </div>
  );
}

/** One participant + camera/screen tile; needs a stable `participant` (rules of hooks). */
function TutorAvatarTile({ participant }) {
  const tracks = useParticipantTracks(
    [Track.Source.Camera, Track.Source.ScreenShare],
    { participantIdentity: participant.identity }
  );
  const trackRef = useMemo(() => {
    if (!tracks.length) return null;
    const live = tracks.find((t) => t.publication?.track);
    return live ?? tracks[0];
  }, [tracks]);

  if (!trackRef) {
    return (
      <div className="tutor-avatar-waiting" role="status">
        Video connecting…
      </div>
    );
  }
  return <ParticipantTile trackRef={trackRef} />;
}

function sessionSuffix() {
  const a = new Uint8Array(4);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Must render under LiveKitRoomProvider (uses participant hooks). */
function TutorAvatarBlock({ tutorLabel }) {
  const { isMicrophoneEnabled } = useLocalParticipant();
  const remotes = useRemoteParticipantsFromRoom();
  const tutorParticipant = useMemo(() => pickTutorParticipant(remotes), [remotes]);
  const agentOnly = onlyLessonAgentRemotes(remotes);

  return (
    <div className="tutor-avatar-video-wrap">
      <div className="tutor-avatar-video-ring">
        <div className="tutor-avatar-video-shell" data-lk-theme="default">
          <LayoutContextProvider>
            <div className="lk-video-conference tutor-avatar-lk-conference">
              <div className="lk-video-conference-inner">
                <div className="lk-grid-layout-wrapper tutor-avatar-grid-stage">
                  {tutorParticipant ? (
                    <TutorAvatarTile key={tutorParticipant.sid} participant={tutorParticipant} />
                  ) : (
                    <div className="tutor-avatar-waiting" role="status">
                      {remotes.length === 0 ? (
                        <>Waiting for {tutorLabel}… Start <code>python agent.py dev</code> with the same LiveKit project.</>
                      ) : agentOnly ? (
                        <>
                          The lesson helper is here, but Leo&apos;s <strong>video face</strong> (BitHuman) has not
                          joined. Check <code>BITHUMAN_AGENT_ID</code>, <code>BITHUMAN_API_SECRET</code>, and errors in
                          the terminal running <code>agent.py</code>.
                        </>
                      ) : (
                        <>Waiting for {tutorLabel}…</>
                      )}
                    </div>
                  )}
                </div>
              </div>
              <ControlBar
                controls={{ microphone: true, camera: true, screenShare: true, chat: true, leave: true }}
              />
            </div>
          </LayoutContextProvider>
        </div>
        <span
          className={`tutor-avatar-video-live-dot${isMicrophoneEnabled ? " is-on" : ""}`}
          title={isMicrophoneEnabled ? "Microphone on" : "Microphone off"}
          aria-label={isMicrophoneEnabled ? "Microphone on" : "Microphone off"}
        />
      </div>
      <p className="tutor-avatar-video-tip">
        Say hello to {tutorLabel}! If the mic has a slash, tap <strong>Microphone</strong> so they can hear you. Tap{" "}
        <strong>Turn on sound</strong> if you cannot hear them.
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
  onLessonComplete,
}) {
  const [connect, setConnect] = useState(false);
  const [error, setError] = useState(null);
  const [showSetupLoader, setShowSetupLoader] = useState(true);

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

  const dismissSetupLoader = useCallback(() => {
    setShowSetupLoader(false);
  }, []);

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

  /** Warm token while the start screen is visible so tap-to-room is faster. */
  useEffect(() => {
    if (connect || !tokenBaseUrl || !roomName) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const params = new URLSearchParams({
          room: roomName,
          identity,
          name: childName || "Friend",
        });
        const res = await fetch(`${tokenBaseUrl}?${params.toString()}`);
        if (cancelled || !res.ok) return;
        const data = await res.json();
        if (cancelled || !data?.token) return;
        setToken(data.token);
        if (data.url) setServerUrl(data.url);
      } catch {
        /* ignore — user will retry via Start or see error on explicit connect */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [connect, tokenBaseUrl, roomName, identity, childName]);

  const handleStart = async () => {
    try {
      if (!token || !serverUrl) {
        await fetchToken();
      }
      setConnect(true);
    } catch (e) {
      const msg = e?.message || "Could not connect";
      const hint =
        msg === "Failed to fetch" || msg === "Load failed" || msg === "NetworkError when attempting to fetch resource."
          ? ` Could not reach ${tokenBaseUrl} — start the token API from the project folder: python token_server.py (leave it running), then try again. If you opened this site from another device, replace 127.0.0.1 in REACT_APP_TOKEN_SERVER_URL with this computer's LAN IP.`
          : "";
      setError(`${msg}${hint}`);
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
      audio={KID_MIC_CAPTURE_OPTIONS}
      video={false}
      options={{ audioCaptureDefaults: { ...KID_MIC_CAPTURE_OPTIONS } }}
      connectOptions={{ autoSubscribe: true }}
      onError={(e) => setError(e.message)}
      className="tutor-livekit-root"
    >
      <TutorEnableMicOnConnect />
      <PublishChildProfile topicSlug={topicSlug} childName={childName} />
      <TutorAvatarSetupGate
        tutorLabel={tutorLabel}
        topicSlug={topicSlug}
        show={showSetupLoader}
        onDismiss={dismissSetupLoader}
      />
      <div
        className={`tutor-livekit-inner tutor-livekit-inner--session${
          showSetupLoader ? " tutor-livekit-inner--setup-pending" : ""
        }`}
        aria-hidden={showSetupLoader}
      >
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
          onLessonComplete={onLessonComplete}
        />
        <RoomAudioRenderer />
        {error && <p className="tutor-error">{error}</p>}
      </div>
    </LiveKitRoom>
  );
}
