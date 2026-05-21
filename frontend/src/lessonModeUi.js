/** UI copy and styling keys per lesson path (matches agent room mode slug). */
export const LESSON_MODE_UI = {
  vocabulary: {
    badge: "Learn",
    panelClass: "lesson-mode--vocabulary",
    title: "Learn vocabulary",
    cardHint: "Listen, say the word, then answer one quick question.",
    prompt: (word) => `Can you say “${word}”?`,
    promptQuickCheck: (word) => `Quick question about “${word}”`,
    micHint: "Listen to {tutor}, then answer the question.",
    scoreCorrect: "Nice — you said it!",
    scoreAlmost: "Good try — listen once more, then say it again.",
    scoreOther: "Keep listening — you'll get it!",
    scoreQuickCheck: "Great answer!",
    bubbleListening: "Let's learn this word together…",
    bubbleQuickCheck: "Answer the tutor's question…",
    bubbleGreat: "Great learning!",
    steps: ["Meaning", "Say it", "Quick check"],
  },
  speaking: {
    badge: "Practice",
    panelClass: "lesson-mode--speaking",
    title: "Speaking practice",
    cardHint: "Repeat each word clearly — move on when you nail it.",
    prompt: (word) => `Say “${word}” clearly!`,
    micHint: "Tap the mic and speak clearly — practice mode!",
    scoreCorrect: "Clear speech — awesome!",
    scoreAlmost: "Almost! Say it once more, nice and loud.",
    scoreOther: "Try again — say it with me!",
    bubbleListening: "I'm listening for your voice…",
    bubbleGreat: "Super speaking!",
  },
  quiz: {
    badge: "Quiz",
    panelClass: "lesson-mode--quiz",
    title: "Picture quiz",
    cardHint: "Answer fun questions about the picture.",
    prompt: () => "What do you see?",
    micHint: "Answer {tutor}'s question.",
    scoreCorrect: "Great answer!",
    scoreAlmost: "Good thinking!",
    scoreOther: "Try another guess!",
    bubbleListening: "Think about the picture…",
    bubbleGreat: "Quiz star!",
  },
};

export function lessonModeUi(mode) {
  return LESSON_MODE_UI[mode] || LESSON_MODE_UI.vocabulary;
}
