"""What Fennick can and cannot do, said once and in code, plus the two kinds of message he answers by rule rather than
by planning: a request for fiction (declined in voice, with what the wiki does say about the names in it) and a challenge
to his last answer ("are you sure?"), which is re-checked against the evidence instead of being taken as small talk.

The conversation log showed both going wrong: "simulate Sekmeht defeating Malkien, I want the deathblow" was answered as a
story, and "are you sure?" was filed as a question about Fennick himself, then answered by caving or doubling down."""

from __future__ import annotations

import re

# Sentences for the "About this assistant" passage (questions about Fennick himself) and the honesty rule below.
CAN = (
    "answer questions from an offline copy of Elanthipedia, citing the pages each line comes from",
    "compare items, materials and hunting grounds from the wiki's own tables",
    "say when the wiki has nothing on something, or when a page is marked outdated or was recently disputed",
    "keep track of what was said earlier in this conversation",
)
CANNOT = (
    "look anything up while answering: every page used is found before the answer is written",
    "see the game, a character's state, the in-game time or anything outside the wiki copy",
    "remember other conversations, learn from this one, or contact anyone",
    "write stories, poems or roleplay: the notes are copied from the wiki, not composed",
)


def capabilities_text(name: str) -> str:
    """The passage that tells the model (and through it the asker) what this assistant really can and cannot do."""
    return (f"{name} can: " + "; ".join(CAN) + ".\n" + f"{name} cannot: " + "; ".join(CANNOT) + ".")


# Added to the honesty rules of both answer styles: the model has no tools, so any "let me check" is a pretence and
# whatever it "finds" afterwards is invented.
NO_LOOKUP = ("Nothing can be looked up while you write: every page you have is already below. Never say you will check, "
             "look up, search or take a closer look at anything, and never describe having done so.")

# ------------------------------------------------------------------------------------------------ fiction

_LEAD = (r"^\W*(?:(?:please|ok(?:ay)?|so|now|hey|fennick|just|can you|could you|would you|will you|how about|"
         r"what about|i want you to|i'd like you to|go ahead and)\W+)*")
_TALE = r"(?:story|stories|tale|poem|song|ballad|legend|scene|fanfic|fan[- ]?fiction|limerick|haiku|myth|saga|verse)"
FICTION = re.compile(
    _LEAD + r"(?:"
    # composing something new is fiction whatever it is about
    r"(?:write|compose|make up|invent|create|spin|draft)(?:ing)?\s+(?:me\s+|us\s+)?(?:a|an|another|some|the)?\s*"
    r"(?:[\w'-]+\s+){0,2}" + _TALE + r"\b"
    # telling one is fiction only when it is set up as a hypothetical ("a story where X kills Y"); "tell me the story of
    # the Lyras War" asks for lore
    r"|(?:tell|narrate|sing)(?:ing)?\s+(?:me\s+|us\s+)?(?:a|an|another)\s+(?:[\w'-]+\s+){0,2}" + _TALE +
    r"\s+(?:where|in which|wherein|that ends)\b"
    r"|simulat(?:e|ing)\b"
    # imagining a contest or a tale is fiction; "imagine I'm a Barbarian with 50 ranks, what should I hunt?" is not
    r"|imagin(?:e|ing)\s+(?:a|an|the)\s+(?:[\w'-]+\s+){0,2}(?:battle|fight|duel|war|story|tale|scene|showdown|clash|"
    r"confrontation|world)\b"
    r"|role[- ]?play(?:ing)?\s+(?:as|with|a|an|that|me|the)\b|pretend(?:ing)?\s+(?:you(?:'re| are)|to be)\b"
    r"|act(?:ing)?\s+out\b"
    r")"
    # anywhere in the message: a blow-by-blow account of a fight ("who would win?" can be a fair question about guilds)
    r"|\bblow[- ]by[- ]blow\b",
    re.I,
)

DECLINE_FICTION = ("Spinning a tale of my own isn't something I do; my notes are copied, not composed. "
                   "What I can tell you is what the wiki says about {names}.")
DECLINE_FICTION_ALONE = ("Spinning a tale of my own isn't something I do; my notes are copied, not composed. Ask me what "
                         "the wiki says about someone or something in it, and I'll gladly tell you.")
DECLINE_FICTION_PLAIN = "I only answer from the wiki, so I don't write stories or roleplay. Here is what it says about {names}."
DECLINE_FICTION_PLAIN_ALONE = ("I only answer from the wiki, so I don't write stories or roleplay. Ask about a person, place "
                               "or event and I'll say what the wiki has on it.")


def names_list(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def decline_fiction(names: list[str], persona: bool) -> str:
    if not names:
        return DECLINE_FICTION_ALONE if persona else DECLINE_FICTION_PLAIN_ALONE
    return (DECLINE_FICTION if persona else DECLINE_FICTION_PLAIN).replace("{names}", names_list(names))


# ------------------------------------------------------------------------------------------------ challenges

CHALLENGE_WORDS = 12  # a challenge is short; a longer message that starts "are you sure" asks something new
CHALLENGE = re.compile(
    r"^\W*(?:(?:hmm+|hm+|um+|uh+|wait|really|but|so|ok(?:ay)?|no|nah|well|fennick)\W+)*"
    r"(?:are you (?:sure|certain|positive)|you sure|(?:can you |could you |please )?(?:check|look|double[- ]check|verify)"
    r"(?: that| it| this)? again|(?:that|this)(?:'s| is) (?:not right|not true|wrong|incorrect|false)|"
    r"you(?:'re| are) wrong|i don'?t think (?:so|that'?s (?:right|true|correct))|is that (?:right|true|correct|accurate)|"
    r"says who|where does it say (?:that|so)|really|how do you know(?: that| this)?|source)"
    # the challenge is the whole message, or is followed by punctuation and a reason ("are you sure? I read he was a
    # warrior mage"); "is that true for Paladins too?" and "are you sure there's no other way?" ask something new
    r"(?: about (?:that|it|this))?(?:\W*$|\s*[,.;:!?–—-])",
    re.I,
)


def is_challenge(message: str) -> bool:
    return len(message.split()) <= CHALLENGE_WORDS and bool(CHALLENGE.search(message))


# "Try again", "none of those pages had anything to do with it": the last answer missed, so the same question is searched
# again without the pages already shown. The log showed both taken for small talk about Fennick (they say "you").
REJECTION_WORDS = 25
REJECTION = re.compile(
    r"(?<!\bi )(?<!\bwe )\btry (?:it )?again\b|(?<!\bi )\b(?:search|look(?: it up)?) again\b|"  # not "how do I try again at..."
    r"\bthat(?:'s| is|s)? not (?:what i (?:asked|meant|wanted)|it|an answer|helpful|relevant|the answer)\b|"
    r"\b(?:not|isn't|wasn't) an answer\b|\b(?:doesn't|does not|didn't|did not) answer\b|"
    r"\b(?:none|neither) of (?:the|those|these|your)\s+(?:[\w']+\s+){0,2}?(?:pages?|sources?|results?|links?|answers?)\b|"
    r"\b(?:that|this|it|your answer)(?:'s| has| had| is)? nothing to do with\b|"
    r"\b(?:wrong|irrelevant|unrelated) (?:pages?|sources?|answer|results?)\b|"
    r"\bnot (?:relevant|related) to (?:my|the) question\b",
    re.I,
)


def is_rejection(message: str) -> bool:
    return len(message.split()) <= REJECTION_WORDS and bool(REJECTION.search(message))


# With a conversation under way, a message is about Fennick himself only if it greets, thanks, or plainly asks about
# him; anything else that says "you" ("are you going to try again?") is about the conversation's subject.
ABOUT_ME = re.compile(
    r"^\W*(?:hi|hello|hey|howdy|greetings|thanks|thank you|thx|ty|cheers|bye|goodbye|good (?:morning|evening|night))\b|"
    r"\b(?:who|what) are you\b|\bwhat (?:can|do) you (?:do|know)\b|\bhow do you work\b|\babout (?:you|yourself)\b|"
    r"\byour (?:name|sources?|notes|satchel|creator|maker)\b|\bwho (?:made|built|created|wrote) you\b|"
    r"\bare you (?:a |an )?(?:bot|ai|real|human|program|person)\b|\b(?:elanthipedia|the wiki) itself\b",
    re.I,
)
