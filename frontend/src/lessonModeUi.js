/** UI copy and styling keys per lesson path (matches agent room mode slug). */
export const LESSON_MODE_UI = {
  vocabulary: {
    badge: "Teaching",
    panelClass: "lesson-mode--vocabulary",
    title: "Learn vocabulary · Teaching mode",
    cardHint:
      "Discover what each word means 2014 your AI teacher introduces the word, explains its meaning, then asks you to repeat it.",
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
    steps: ["Word", "Meaning", "Example", "Say it"],
  },
  speaking: {
    badge: "Sentence coach",
    panelClass: "lesson-mode--speaking",
    title: "Speaking practice · Sentence coach",
    cardHint:
      "Speak whole sentences clearly! Your coach says one sentence at a time — repeat the whole sentence after them. Pass 3 sentences to move to the next word.",
    prompt: (word) =>
      word ? `Repeat the sentence about “${word}”` : "Repeat the whole sentence",
    micHint:
      "Repeat the WHOLE sentence after your coach — the app scores every word, not just one!",
    scoreCorrect: "Awesome speaking! Whole sentence clear!",
    scoreAlmost: "Almost! Try the trickiest word slowly, then the whole sentence again.",
    scoreOther: "Listen and repeat the WHOLE sentence with the coach!",
    bubbleListening: "{tutor} is modelling a sentence — repeat the whole thing…",
    bubbleGreat: "Super speaking!",
    steps: ["Coach says", "You repeat", "Score sentence", "Next sentence"],
  },
  quiz: {
    badge: "Quiz show",
    panelClass: "lesson-mode--quiz",
    title: "Picture quiz · Game-show mode",
    cardHint:
      "Show what you KNOW! The host points at the picture and asks fun questions — colour, sound, where it lives — guess quick to win the round.",
    prompt: () => "Answer the host's question!",
    micHint: "Tap the mic and shout out your answer — guess fast!",
    scoreCorrect: "Bingo!",
    scoreAlmost: "Good guess!",
    scoreOther: "Have a guess — there's no wrong answer!",
    bubbleListening: "Your quiz host is asking about the picture…",
    bubbleGreat: "Quiz champion!",
    steps: ["Picture", "Question", "Your guess", "Next round!"],
  },
};

export function lessonModeUi(mode) {
  return LESSON_MODE_UI[mode] || LESSON_MODE_UI.vocabulary;
}
