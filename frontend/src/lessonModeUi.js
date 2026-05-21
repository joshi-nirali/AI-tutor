/** UI copy and styling keys per lesson path (matches agent room mode slug). */
export const LESSON_MODE_UI = {
  vocabulary: {
    badge: "Teaching",
    panelClass: "lesson-mode--vocabulary",
    title: "Learn vocabulary · Teaching mode",
    cardHint:
      "Your AI teacher introduces each word: meaning, example, then a fun question.",
    prompt: (word) => `Listen to your teacher about “${word}”`,
    promptQuickCheck: (word) => `Answer the question about “${word}”`,
    micHint: "Listen to {tutor}, repeat the word, then answer the question.",
    scoreCorrect: "Nice — you said it!",
    scoreAlmost: "Good try — listen once more, then say it again.",
    scoreOther: "Keep listening — you'll get it!",
    scoreQuickCheck: "Great answer!",
    bubbleListening: "{tutor} is teaching you a new word…",
    bubbleQuickCheck: "Answer your teacher's question…",
    bubbleGreat: "Great learning!",
    steps: ["Word", "Meaning", "Example", "Say it", "Quick question"],
  },
  speaking: {
    badge: "Speaking coach",
    panelClass: "lesson-mode--speaking",
    title: "Speaking practice · Coach mode",
    cardHint:
      "Your AI speaking coach models a sentence — repeat clearly and build confidence.",
    prompt: (word) => `Repeat after coach: “${word}”`,
    micHint: "Tap the mic and repeat what your coach just said.",
    scoreCorrect: "Clear speech — awesome!",
    scoreAlmost: "Almost! Say it slowly with the coach: try the syllables.",
    scoreOther: "Try again — repeat after your coach!",
    bubbleListening: "{tutor} is your speaking coach — repeat after them…",
    bubbleGreat: "Super speaking!",
    steps: ["Coach models", "You repeat", "Gentle tip", "Move on"],
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
