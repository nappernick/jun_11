"""
The sampling frame -- now TWO-TIER, because identity and situation live at
different grain and must not be flattened together.

  PERSONA  (stable, drawn once per batch): who the person IS.
      origin       -- country/region + native language + how that L1 colours English
      proficiency  -- overall English command
      disposition  -- stable communication trait (terse, formal, suspicious, ...)

  CONTEXT  (situational, drawn PER query / per conversation): how this ONE
           interaction happens. The same persona spans many of these.
      channel        -- the medium they typed/spoke through this time
      entry_route    -- slack vs quicksuite for this interaction
      momentary_state-- transient emotional overlay (neutral most of the time)

Why the split: route/channel/state are properties of an INTERACTION, not of a
person -- the same Lagos employee Slacks on mobile while frustrated one day and
uses QuickSuite on desktop calmly the next. Freezing those into the persona was a
grain error (and assumed an independence that may not hold). Persona breadth is
made structural by a per-axis coprime-stride walk on the batch number; context
breadth by a coprime-stride walk on (batch, query index). Pure arithmetic on
counters: no RNG, no model judgement, so neither layer can groove.

intent shape and quality vary WITHIN a batch via a quota in the Generator brief,
not as axes here.

Run:  python -m synth.frame            # persona for the NEXT batch + a sample context schedule
      python -m synth.frame --batch 7
"""
import argparse
import json
import math
import os

LEDGER_PATH = "data/synthetic/perspectives_ledger.jsonl"

# --- PERSONA tier ------------------------------------------------------------
# origin: the primary breadth axis. Worldwide on purpose, not a top-5.
ORIGINS = [
    {"label": "Nigeria (Lagos)", "native_language": "Yoruba / Nigerian English", "interference": "Nigerian-English idiom, 'kindly', serial verbs, 'I am wanting to'"},
    {"label": "Kenya (Nairobi)", "native_language": "Swahili", "interference": "measured phrasing, 'the same' as filler, dropped plurals"},
    {"label": "Ghana (Accra)", "native_language": "Akan / Twi", "interference": "'please' front-loaded, 'I want to find out', formal register"},
    {"label": "Ethiopia (Addis Ababa)", "native_language": "Amharic", "interference": "verb-final leakage, articles dropped, careful spelling"},
    {"label": "South Africa (Johannesburg)", "native_language": "Zulu / SA English", "interference": "'is it?', 'just now', 'shame', 'hey' tags"},
    {"label": "Morocco (Casablanca)", "native_language": "Darija Arabic / French", "interference": "French calques, 'normally', gendered slips, 'no?' tags"},
    {"label": "Egypt (Cairo)", "native_language": "Arabic", "interference": "no capital letters for proper nouns, 'kindly', 'i need to know'"},
    {"label": "Bosnia (Sarajevo)", "native_language": "Bosnian", "interference": "dropped articles, aspect confusion, blunt directness"},
    {"label": "Serbia (Belgrade)", "native_language": "Serbian", "interference": "no 'the/a', 'how I can', literal translation, terse"},
    {"label": "Poland (Krakow)", "native_language": "Polish", "interference": "dropped articles, 'since', 'make a reservation' over 'book', formal"},
    {"label": "Russia (Moscow)", "native_language": "Russian", "interference": "no articles, perfective/imperfective slips, blunt, 'I have question'"},
    {"label": "Ukraine (Kyiv)", "native_language": "Ukrainian / Russian", "interference": "dropped articles, 'tell me please', direct"},
    {"label": "Romania (Bucharest)", "native_language": "Romanian", "interference": "Romance calques, 'I would want', polite conditional overuse"},
    {"label": "Germany (Munich)", "native_language": "German", "interference": "V2 word order leak, compound nouns, 'since' for 'for', precise, 'Best regards'"},
    {"label": "Switzerland (Zurich)", "native_language": "Swiss German", "interference": "very formal, precise, 'I would like to kindly ask', exact amounts"},
    {"label": "France (Paris)", "native_language": "French", "interference": "'normally', 'actually'=currently, 'I am agree', accents in odd places"},
    {"label": "Italy (Milan)", "native_language": "Italian", "interference": "double consonants, 'I have 30 years', 'no?' tags, expressive"},
    {"label": "Spain (Madrid)", "native_language": "Spanish", "interference": "'actually'=currently, 'I have a doubt', gendered, warm"},
    {"label": "Portugal (Lisbon)", "native_language": "Portuguese", "interference": "'pretend'=intend, 'I am needing', soft hedging"},
    {"label": "Netherlands (Amsterdam)", "native_language": "Dutch", "interference": "very direct, near-native, 'how late'=what time, blunt brevity"},
    {"label": "Sweden (Stockholm)", "native_language": "Swedish", "interference": "near-native, understated, 'I wonder if', lowercase, minimal"},
    {"label": "Turkey (Istanbul)", "native_language": "Turkish", "interference": "agglutinative leak, articles dropped, verb-final tendency"},
    {"label": "Saudi Arabia (Riyadh)", "native_language": "Arabic", "interference": "'kindly do the needful', formal, 'i kindly request'"},
    {"label": "UAE (Dubai expat)", "native_language": "Arabic / mixed", "interference": "Gulf business English, 'revert back', 'do the needful'"},
    {"label": "India (Bengaluru)", "native_language": "Kannada / Hindi", "interference": "'do the needful', 'revert', 'prepone', 'kindly', 'itself/only' emphasis"},
    {"label": "India (Delhi)", "native_language": "Hindi", "interference": "'please do the needful', present continuous overuse, 'na' tags"},
    {"label": "Pakistan (Karachi)", "native_language": "Urdu", "interference": "formal, 'kindly guide me', Urdu-English register"},
    {"label": "Bangladesh (Dhaka)", "native_language": "Bengali", "interference": "'kindly', dropped articles, earnest over-explanation"},
    {"label": "China (Shanghai)", "native_language": "Mandarin", "interference": "no articles/plurals, dropped tense, topic-comment, measure words, terse"},
    {"label": "China (Shenzhen)", "native_language": "Mandarin / Cantonese", "interference": "very terse, no articles, 'can or not', literal"},
    {"label": "Japan (Tokyo)", "native_language": "Japanese", "interference": "topic-comment, dropped subjects, over-polite, 'I think maybe', apologetic"},
    {"label": "South Korea (Seoul)", "native_language": "Korean", "interference": "dropped articles, honorific carryover, 'I am curious about'"},
    {"label": "Vietnam (Ho Chi Minh)", "native_language": "Vietnamese", "interference": "dropped tense markers, classifiers, terse, 'how about'"},
    {"label": "Indonesia (Jakarta)", "native_language": "Bahasa Indonesia", "interference": "reduplication leak, 'already'=completed, friendly"},
    {"label": "Philippines (Manila)", "native_language": "Tagalog", "interference": "near-fluent, 'po' politeness echo, code-switch, 'for a while'=hold on"},
    {"label": "Thailand (Bangkok)", "native_language": "Thai", "interference": "dropped tense/plural, 'na', soft, final particles"},
    {"label": "Brazil (Sao Paulo)", "native_language": "Portuguese", "interference": "'I am with a doubt', 'make' for 'do', warm, 'no?' tags"},
    {"label": "Argentina (Buenos Aires)", "native_language": "Rioplatense Spanish", "interference": "'I have a doubt', 'actually'=currently, expressive, gendered slips"},
    {"label": "Mexico (Mexico City)", "native_language": "Spanish", "interference": "'I have a doubt', polite diminutives, 'actually'=currently"},
    {"label": "Colombia (Bogota)", "native_language": "Spanish", "interference": "very polite, 'I would like to know', formal usted-register warmth"},
]

PROFICIENCIES = [
    "broken -- frequent grammar errors, dropped words, phonetic spelling",
    "functional -- understandable but visibly non-native, article/tense slips",
    "fluent-but-accented -- smooth with occasional idiom/preposition tells",
    "near-native -- only subtle tells, the odd calque or unusual word choice",
    "uneven -- mostly fine then suddenly garbled on a hard word or concept",
]

# STABLE communication traits only. Transient feelings moved to CONTEXT below.
DISPOSITIONS = [
    "terse -- minimal words, no pleasantries, just the ask",
    "over-polite -- excessive thanks, apologies, deference",
    "formal -- writes like a memo, full sentences, sign-off",
    "rambling -- buries the question in backstory and context",
    "suspicious -- distrustful of the policy, pushing for loopholes",
    "chatty -- friendly, conversational, tangents",
    "deadpan-literal -- asks exactly and only the literal question",
]

# --- CONTEXT tier (drawn per interaction, not per person) ---------------------
CHANNELS = [
    "mobile-thumb -- short, autocorrect artifacts, missing punctuation, lowercase",
    "voice-transcribed -- run-on, filler words, homophone errors, no punctuation",
    "desktop-careful -- full punctuation, paragraphs, considered",
    "copy-paste-jargon -- pastes policy/system terms they half-understand",
    "chat-fragments -- sends the question in broken pieces, trailing '...'",
    "search-box-keywords -- types keywords not a sentence, like a search query",
]

ENTRY_ROUTES = [
    "slack -- conversational Slack bot; informal, can be chatty/multi-line",
    "quicksuite -- typed into QuickSuite, which tool-calls the RAG system; treated as RAW user text (assume NOT pre-cleaned)",
]

# Transient emotional overlay for ONE interaction. 'neutral' is weighted (listed
# multiple times) because most real interactions carry no strong affect.
MOMENTARY_STATES = [
    "neutral -- no strong emotional overlay",
    "neutral -- no strong emotional overlay",
    "neutral -- no strong emotional overlay",
    "frustrated -- something already went wrong, short-tempered",
    "rushed -- in a hurry, 'asap', half-finished thoughts",
    "anxious -- worried about doing something wrong, seeks reassurance",
    "confused -- doesn't understand the prior step or the policy",
]

# Sub-national placement. A country/city alone under-specifies a person: "north of
# Spain" carries a different accent, dialect lexicon, and cadence than the capital,
# and may borrow from a neighboring language. This positions the persona WITHIN their
# country; the generator (which knows the country) renders the concrete locale and its
# speech texture from this generic position. Selection stays mechanical (no model whim).
REGION_POSITIONS = [
    "capital / metro core -- cosmopolitan, more standardized English, faster cadence",
    "northern region -- its own regional accent and dialect words, distinct from the capital",
    "southern region -- warmer / more idiomatic dialect, often slower or more ornate",
    "major secondary city (not the capital) -- urban but with a strong separate regional identity",
    "coastal / port area -- trade-influenced, mixed or borrowed lexicon",
    "provincial small-town / rural -- traditional, local dialect, less exposure to cosmopolitan English",
    "border region -- a neighboring country's language bleeds into the lexicon and cadence",
]

PERSONA_AXES = {
    "origin": ORIGINS,
    "region": REGION_POSITIONS,
    "proficiency": PROFICIENCIES,
    "disposition": DISPOSITIONS,
}

CONTEXT_AXES = {
    "channel": CHANNELS,
    "entry_route": ENTRY_ROUTES,
    "momentary_state": MOMENTARY_STATES,
}


def _coprime_stride(length, avoid=None):
    """A stride coprime to `length` (optionally != `avoid`) so it visits every value.

    Candidates include 5 explicitly so composite lengths like 6 still get real mixing,
    not the degenerate stride of 1.
    """
    for candidate in (7, 11, 13, 17, 19, 23, 5, 29, 31, 37, 41, 43, 47, 3, 2):
        if candidate < length and math.gcd(candidate, length) == 1 and candidate != avoid:
            return candidate
    return 1  # length <= 2: stride of 1 still alternates/covers


def coordinate_for_batch(batch_number):
    """Map a batch number to one stable PERSONA (origin/proficiency/disposition)."""
    picked = {"batch": batch_number}
    for axis_name, values in PERSONA_AXES.items():
        stride = _coprime_stride(len(values))
        picked[axis_name] = values[(batch_number * stride) % len(values)]
    return picked


def context_for(batch_number, query_index):
    """One interaction CONTEXT, varying across queries within a batch and across batches.

    Each axis advances on its OWN pair of coprime strides -- one on the batch, a
    distinct one on the query index -- so context fans out across a persona's queries
    AND decorrelates from the persona walk, with no resonance against any axis length.
    """
    picked = {}
    for axis_name, values in CONTEXT_AXES.items():
        length = len(values)
        batch_stride = _coprime_stride(length)
        query_stride = _coprime_stride(length, avoid=batch_stride)
        index = (batch_number * batch_stride + query_index * query_stride) % length
        picked[axis_name] = values[index]
    return picked


def context_schedule(batch_number, count):
    """The per-query context list a Generator uses for a batch of `count` queries."""
    return [context_for(batch_number, query_index) for query_index in range(count)]


def _next_batch_number(ledger_path):
    if not os.path.exists(ledger_path):
        return 0
    with open(ledger_path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=None,
                        help="Batch number to sample; default = next unused batch.")
    parser.add_argument("--ledger", default=LEDGER_PATH)
    parser.add_argument("--schedule", type=int, default=6,
                        help="How many sample context rows to print alongside the persona.")
    args = parser.parse_args()

    batch_number = args.batch
    if batch_number is None:
        batch_number = _next_batch_number(args.ledger)

    out = {
        "persona": coordinate_for_batch(batch_number),
        "sample_context_schedule": context_schedule(batch_number, args.schedule),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
