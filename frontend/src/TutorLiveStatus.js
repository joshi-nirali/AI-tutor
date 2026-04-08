import { useEffect, useState } from "react";
import {
  useConnectionState,
  useRemoteParticipants,
  useLocalParticipant,
} from "@livekit/components-react";
import { ConnectionState } from "livekit-client";

/**
 * Explains common reasons the child hears no reply (agent not running, mic off, audio blocked).
 */
export default function TutorLiveStatus({ tutorLabel }) {
  const connectionState = useConnectionState();
  const remotes = useRemoteParticipants();
  const { isMicrophoneEnabled, lastMicrophoneError } = useLocalParticipant();
  const [showAgentHint, setShowAgentHint] = useState(false);

  useEffect(() => {
    if (connectionState !== ConnectionState.Connected || remotes.length > 0) {
      setShowAgentHint(false);
      return;
    }
    const id = setTimeout(() => setShowAgentHint(true), 2500);
    return () => clearTimeout(id);
  }, [connectionState, remotes.length]);

  if (connectionState === ConnectionState.Connecting || connectionState === ConnectionState.Reconnecting) {
    return (
      <div className="tutor-status tutor-status-info" role="status">
        Connecting to the lesson room…
      </div>
    );
  }

  if (connectionState !== ConnectionState.Connected) {
    return null;
  }

  if (remotes.length === 0 && showAgentHint) {
    return (
      <div className="tutor-status tutor-status-warn" role="alert">
        <strong>{tutorLabel} hasn&apos;t joined yet.</strong>
        <p className="tutor-status-detail">
          The talking tutor is a separate program. On this machine, in the{" "}
          <code>essence-cloud</code> folder, an adult should run:
        </p>
        <pre className="tutor-status-code">python agent.py dev</pre>
        <p className="tutor-status-detail">
          Use the same LiveKit project as <code>token_server.py</code>. Running{" "}
          <code>quickstart.py</code> does not connect to this room.
        </p>
      </div>
    );
  }

  if (remotes.length === 0) {
    return (
      <div className="tutor-status tutor-status-info" role="status">
        Waiting for {tutorLabel}…
      </div>
    );
  }

  if (!isMicrophoneEnabled) {
    return (
      <div className="tutor-status tutor-status-warn" role="status">
        <strong>Microphone is off.</strong> Tap the microphone in the bar above (or unmute) so{" "}
        {tutorLabel} can hear you.
      </div>
    );
  }

  if (lastMicrophoneError) {
    return (
      <div className="tutor-status tutor-status-warn" role="alert">
        Mic problem: {lastMicrophoneError.message}. Check browser permissions for this site.
      </div>
    );
  }

  return (
    <div className="tutor-status tutor-status-ok" role="status">
      {tutorLabel} is here — say hello! If you can&apos;t hear them, tap{" "}
      <strong>Turn on sound</strong> below.
    </div>
  );
}
