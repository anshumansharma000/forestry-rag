"""Conservative, model-free replies for messages containing only pleasantries."""

import random
import re
import unicodedata

from pipeline_trace import event

# Every word must belong to an approved phrase. Never match a greeting prefix
# and discard a substantive request, even when it follows punctuation/newlines.
PHRASES = {
    "greeting": ("hi", "hii", "hello", "hey", "hey there", "hello there", "hi there", "good morning", "good afternoon",
                 "good evening", "greetings", "namaste"),
    "wellbeing": ("how are you", "how are you doing", "how is it going", "hows it going", "whats up", "what is up", "sup",
                  "how have you been", "hope you are well", "hope youre doing well", "how is your day", "hows your day"),
    "thanks": ("thanks", "thank you", "thanks a lot", "thank you so much", "many thanks", "much appreciated", "thank u",
               "thanks for your help", "thank you for your help", "thanks for helping", "thanks again"),
    "goodbye": ("bye", "goodbye", "see you", "see you later", "take care", "have a good day", "have a nice day",
                "bye bye", "talk to you later", "good night", "thats all", "that is all", "nothing else"),
    "capabilities": ("help", "help me", "can you help", "can you help me", "what can you do", "what can you help with",
                     "what can you help me with", "can u help me", "what do you do", "what topics do you cover",
                     "what can i ask you", "what should i ask", "what is your scope", "can i ask you something",
                     "i have a question", "i need help", "could you help me", "please help", "please help me"),
    "identity": ("who are you", "what are you", "what is your name", "whats your name", "are you a bot",
                 "are you human", "are you an ai", "are you a real person"),
    "availability": ("are you there", "hello are you there", "anyone there", "are you available", "are you online",
                     "can we talk", "can i talk to you", "can you hear me", "is this working"),
    "introduction": ("nice to meet you", "pleased to meet you", "im new here", "i am new here", "this is my first time here"),
    "getting_started": ("how do i use this", "how do i use this assistant", "how do i get started", "where do i start",
                        "how does this work", "how should i ask my question"),
    "examples": ("give me examples", "show me examples", "give me some examples", "show me sample questions",
                 "give me sample questions", "what are some questions i can ask"),
    "compliment": ("youre helpful", "you are helpful", "youre very helpful", "you are very helpful", "great job",
                   "well done", "youre great", "you are great", "youre amazing", "you are amazing",
                   "that was helpful", "thats helpful", "that helps", "this is helpful", "good job"),
    "apology": ("sorry", "im sorry", "i am sorry", "my apologies", "sorry about that", "sorry to bother you"),
    "wellbeing_reply": ("im fine", "i am fine", "im good", "i am good", "im doing well", "i am doing well", "all good"),
    "frustration": ("im frustrated", "i am frustrated", "this is frustrating", "im having a bad day", "i am having a bad day"),
    "confusion": ("im confused", "i am confused", "i dont understand", "i do not understand", "this is confusing"),
    "negative_feedback": ("thats wrong", "that is wrong", "youre wrong", "you are wrong", "thats not helpful",
                          "that is not helpful", "this isnt helpful", "thanks for nothing"),
    "humor": ("tell me a joke", "tell me a forest joke", "tell me a forestry joke", "make me laugh"),
}
RESPONSES = {
    "greeting": (
        "Hi! What's on your mind? I can help with forestry rules, permissions, and related questions.",
        "Hello! How can I help with your forestry or forest-department questions today?",
    ),
    "wellbeing": (
        "I'm here and ready to help. What's on your mind?",
        "Ready to help! What would you like to know about forestry rules or procedures?",
    ),
    "thanks": ("You're welcome!", "You're welcome! Let me know if you have another forestry question."),
    "goodbye": ("Goodbye! Have a good day.", "Take care! I'm here whenever you need help with a forestry question."),
    "capabilities": (
        "I can help with forestry, wildlife, conservation, related laws and policies, and forest-department administration. "
        "What would you like help with?",
        "Tell me what you're working on. I can help with forestry rules, permits, wildlife and conservation, "
        "or forest-department procedures.",
    ),
    "identity": (
        "I'm an AI assistant for forestry and forest-department questions. "
        "I can help you navigate relevant rules, permissions, and procedures. What's on your mind?",
        "I'm an AI assistant for forestry information. Tell me what you'd like help with, "
        "and I'll help you find your way through the relevant rules and procedures.",
    ),
    "availability": (
        "Yes, I'm here. What would you like help with?",
        "I'm here and ready to help. You can type your question whenever you're ready.",
    ),
    "introduction": (
        "Welcome! You can ask me about forestry rules, permits, conservation, or forest-department procedures. "
        "What would you like to start with?",
        "Nice to meet you! Tell me what forestry or forest-department matter you're working on, and we can start there.",
    ),
    "getting_started": (
        "Ask your question in your own words. Include the location and any relevant rule, permit, or document "
        "if you know it. For example: 'What permissions are needed to fell a tree on private land in Assam?'",
        "Start with what you need to find out, and include the state or jurisdiction if relevant. "
        "If you're unsure where to begin, just describe the forestry matter you're dealing with.",
    ),
    "examples": (
        "You could ask:\n\n- What permissions are needed to fell a tree on private land in Assam?\n"
        "- What documents are needed for a timber transit permit in my state?\n"
        "- Can you explain this forestry rule in simpler terms?\n\nInclude your location or the relevant document when you can.",
    ),
    "compliment": ("Thank you! I'm glad that helped.", "Glad that was useful! Let me know if you have another question."),
    "apology": ("No problem at all. What would you like help with?", "No need to apologize. Take your time; I'm here to help."),
    "wellbeing_reply": ("Good to hear! What would you like help with today?", "Glad to hear it. What's on your mind?"),
    "frustration": (
        "That sounds frustrating. If there's a forestry question or procedure you're stuck on, "
        "tell me which part and we can work through it step by step.",
        "I'm sorry it's been difficult. Tell me what you're trying to work out, and I'll help you take it one step at a time.",
    ),
    "confusion": (
        "Which part would you like help understanding? Share the forestry question or passage, and we can work through it.",
        "Let's take it one step at a time. Tell me which forestry rule or procedure you'd like explained.",
    ),
    "negative_feedback": (
        "Thanks for flagging that. Which answer or detail should I take another look at?",
        "I'd like to help put that right. Share the question or point you're concerned about so I can check it.",
    ),
    "humor": (
        "Why did the tree bring a notebook? To keep a log.\n\nWhat forestry question can I help with?",
        "What do trees do when they get online? They log in.\n\nWhat forestry question can I help with?",
    ),
}
_INTENTS = {phrase: intent for intent, phrases in PHRASES.items() for phrase in phrases}
_PATTERN = re.compile("|".join(re.escape(phrase) for phrase in sorted(_INTENTS, key=len, reverse=True)))
_CONTEXTUAL_INTENTS = {"confusion", "negative_feedback", "examples", "getting_started"}


def conversational_response(message: str, history: list[dict] | None = None) -> dict | None:
    # Bound matching work; longer and unfamiliar messages use the scope gate.
    if len(message) > 300:
        return None
    normalized = unicodedata.normalize("NFKC", message).casefold().replace("’", "'").replace("'", "")
    # Only harmless phrase separators are removed. URLs, emoji and other
    # unfamiliar symbols stay unmatched rather than silently losing meaning.
    normalized = re.sub(r"[\s,.!?;:]+", " ", normalized).strip()
    if not normalized:
        return None
    intents = []
    position = 0
    while position < len(normalized):
        match = _PATTERN.match(normalized, position)
        if match is None or (match.end() < len(normalized) and normalized[match.end()] != " "):
            return None
        intents.append(_INTENTS[match.group()])
        position = match.end() + 1
    # A greeting may introduce another pleasantry; use the last meaningful intent.
    intent = next((value for value in reversed(intents) if value != "greeting"), "greeting")
    # Feedback and requests for examples may refer to a substantive answer. Let
    # the existing context resolver address it instead of sending a generic reply.
    if _CONTEXTUAL_INTENTS.intersection(intents) and history:
        answers = [item for item in history if item.get("role") == "assistant"]
        if not answers:
            return None
        # Intervening greetings/thanks must not erase an earlier forestry answer.
        for answer in answers:
            metadata = answer.get("metadata") or {}
            outcome = metadata.get("outcome", answer.get("outcome"))
            origin = metadata.get("answer_origin", answer.get("answer_origin"))
            if outcome != "conversational" and origin != "conversational":
                return None
    event("conversational_response", intent=intent)
    return conversational_result(random.choice(RESPONSES[intent]))


def conversational_result(answer: str) -> dict:
    """Shared API shape for approved templates and lightweight social replies."""
    return {
        "answer": answer,
        "outcome": "conversational",
        "answer_origin": "conversational",
        "abstained": False,
        "sources": [],
        "cited_sources": [],
        "confidence": None,
    }
