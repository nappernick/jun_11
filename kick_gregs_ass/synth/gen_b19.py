import json, os

path = "data/synthetic/staging/conversations_b19.jsonl"
os.makedirs(os.path.dirname(path), exist_ok=True)

# Persona (batch 19): German (Munich), fluent-but-accented (V2 word-order leak,
# compound nouns, "since" for "for", precise, "Best regards"), suspicious --
# distrustful of policy, pushing for loopholes.
PERSONA = "de-munich-suspicious-loophole"

sets = []

# -------------------------------------------------------------------
# SET 01 -- ctx60: search-box-keywords, quicksuite, neutral
# edge_profile: drill_down -- keeps narrowing on one rule looking for the gap
# -------------------------------------------------------------------
sets.append({
    "set_id": "c19-s01",
    "batch": 19,
    "persona_tag": PERSONA,
    "session_channel": "search-box-keywords",
    "entry_route": "quicksuite",
    "turn_count": 3,
    "edge_profile": ["drill_down"],
    "turns": [
        {
            "turn": 1, "relationship": "opening",
            "momentary_state": "neutral",
            "response_dependent": False, "depends_on_turn": None,
            "user_utterance": "flight booking outside Concur reimbursable exceptions",
            "wants": "Whether a flight booked outside the mandated tool (Concur) can still be reimbursed, and what exceptions exist.",
        },
        {
            "turn": 2, "relationship": "narrows-on-the-exception-from-turn1",
            "momentary_state": "neutral",
            "response_dependent": True, "depends_on_turn": 1,
            "user_utterance": "ok but the exception you said -- what counts as 'no Concur availability' exactly. who decides",
            "wants": "The precise definition of the exception named in the prior answer and who has authority to approve it.",
        },
        {
            "turn": 3, "relationship": "drills-into-the-edge-of-turn2-definition",
            "momentary_state": "neutral",
            "response_dependent": True, "depends_on_turn": 2,
            "user_utterance": "so if i book myself first then claim Concur was down -- proof required? screenshot enough",
            "wants": "Whether self-booking first and claiming the tool was unavailable is allowed, and what evidence is required to substantiate it.",
        },
    ],
})

# -------------------------------------------------------------------
# SET 02 -- ctx61: mobile-thumb, slack, anxious
# edge_profile: callback -- references an earlier worry later
# -------------------------------------------------------------------
sets.append({
    "set_id": "c19-s02",
    "batch": 19,
    "persona_tag": PERSONA,
    "session_channel": "mobile-thumb",
    "entry_route": "slack",
    "turn_count": 3,
    "edge_profile": ["callback"],
    "turns": [
        {
            "turn": 1, "relationship": "opening",
            "momentary_state": "anxious",
            "response_dependent": False, "depends_on_turn": None,
            "user_utterance": "hey quick q i already paid the hotel myself before approval came thru is that gonna be a problem",
            "wants": "Reassurance and the rule on whether paying for a hotel before getting approval causes a reimbursement problem.",
        },
        {
            "turn": 2, "relationship": "follows-on-same-worry",
            "momentary_state": "anxious",
            "response_dependent": True, "depends_on_turn": 1,
            "user_utterance": "ok so if its over the nightly cap like you said do i lose the whole thing or just the difference",
            "wants": "Whether exceeding the nightly hotel rate cap forfeits the entire claim or only the amount over the cap.",
        },
        {
            "turn": 3, "relationship": "callback-to-the-pre-approval-worry-from-turn1",
            "momentary_state": "anxious",
            "response_dependent": True, "depends_on_turn": 2,
            "user_utterance": "coming back to the no approval thing -- since 3 days i wait for my manager. can i just submit anyway and add approval after",
            "wants": "Whether the expense can be submitted now and the missing manager approval attached retroactively, tying back to the pre-approval issue raised in turn 1.",
        },
    ],
})

# -------------------------------------------------------------------
# SET 03 -- ctx62: voice-transcribed, quicksuite, neutral
# edge_profile: tangent -- drifts to a related-but-separate topic
# -------------------------------------------------------------------
sets.append({
    "set_id": "c19-s03",
    "batch": 19,
    "persona_tag": PERSONA,
    "session_channel": "voice-transcribed",
    "entry_route": "quicksuite",
    "turn_count": 3,
    "edge_profile": ["tangent"],
    "turns": [
        {
            "turn": 1, "relationship": "opening",
            "momentary_state": "neutral",
            "response_dependent": False, "depends_on_turn": None,
            "user_utterance": "yeah so i wanted to no um what is the per diem for munich versus when i travel to like the states because i go to seattle next month for the offsite",
            "wants": "The per diem meal allowance for Munich versus US (Seattle) travel for an upcoming offsite.",
        },
        {
            "turn": 2, "relationship": "tangent-off-the-offsite-mention",
            "momentary_state": "neutral",
            "response_dependent": True, "depends_on_turn": 1,
            "user_utterance": "actually wait the offsite is a team event so does the dinner there count against my per diem or is that separate since the company pays the event",
            "wants": "Whether a company-paid team event dinner reduces the personal per diem or is accounted separately.",
        },
        {
            "turn": 3, "relationship": "extends-the-event-tangent",
            "momentary_state": "neutral",
            "response_dependent": True, "depends_on_turn": 2,
            "user_utterance": "and if i skip the team dinner and eat alone can i still claim the full per diem for that day or do they subtract it anyway",
            "wants": "Whether opting out of a provided team dinner lets the traveler claim the full per diem or whether the provided-meal deduction still applies.",
        },
    ],
})

# -------------------------------------------------------------------
# SET 04 -- ctx63: desktop-careful, slack, rushed
# edge_profile: correction -- user corrects own earlier statement
# -------------------------------------------------------------------
sets.append({
    "set_id": "c19-s04",
    "batch": 19,
    "persona_tag": PERSONA,
    "session_channel": "desktop-careful",
    "entry_route": "slack",
    "turn_count": 3,
    "edge_profile": ["correction"],
    "turns": [
        {
            "turn": 1, "relationship": "opening",
            "momentary_state": "rushed",
            "response_dependent": False, "depends_on_turn": None,
            "user_utterance": "Need this asap: I am booking a rental car for a 2-day client trip in Berlin. What class of car is allowed under policy?",
            "wants": "The rental car class permitted by policy for a short domestic client trip.",
        },
        {
            "turn": 2, "relationship": "corrects-a-fact-from-turn1",
            "momentary_state": "rushed",
            "response_dependent": True, "depends_on_turn": 1,
            "user_utterance": "Correction, sorry -- it is not 2 days, it is 6 days, and 3 colleagues drive with me. Does that change the allowed class? Quickly please.",
            "wants": "Whether the longer duration and extra passengers change the permitted rental car class.",
        },
        {
            "turn": 3, "relationship": "pushes-loophole-on-corrected-facts",
            "momentary_state": "rushed",
            "response_dependent": True, "depends_on_turn": 2,
            "user_utterance": "If 4 people justify a bigger car, can I take the SUV and just keep it the weekend in between for myself? It is already rented anyway.",
            "wants": "Whether a policy-justified larger vehicle can be retained for personal use over an intervening weekend at no extra cost.",
        },
    ],
})

# -------------------------------------------------------------------
# SET 05 -- ctx64: copy-paste-jargon, quicksuite, neutral
# edge_profile: contradiction -- two turns assert mutually inconsistent premises
# -------------------------------------------------------------------
sets.append({
    "set_id": "c19-s05",
    "batch": 19,
    "persona_tag": PERSONA,
    "session_channel": "copy-paste-jargon",
    "entry_route": "quicksuite",
    "turn_count": 3,
    "edge_profile": ["contradiction"],
    "turns": [
        {
            "turn": 1, "relationship": "opening",
            "momentary_state": "neutral",
            "response_dependent": False, "depends_on_turn": None,
            "user_utterance": "Per the Global T&E Policy section on 'Premium Economy eligibility for transcontinental segments >6h' -- I have a 9h leg, so I am eligible, correct?",
            "wants": "Confirmation that a 9-hour transcontinental leg qualifies for premium economy under the cited policy clause.",
        },
        {
            "turn": 2, "relationship": "builds-on-turn1-premise",
            "momentary_state": "neutral",
            "response_dependent": True, "depends_on_turn": 1,
            "user_utterance": "Good. Now the 'combined itinerary aggregation rule' -- my two 4h legs add to 8h, so they also aggregate to Premium Economy, yes?",
            "wants": "Whether two separate 4-hour legs can be summed to clear the duration threshold for premium economy.",
        },
        {
            "turn": 3, "relationship": "contradicts-own-turn1-claim",
            "momentary_state": "neutral",
            "response_dependent": True, "depends_on_turn": 2,
            "user_utterance": "Wait, earlier I said my long leg is 9h but actually the booked fare shows 5h50m -- still Premium Economy under the >6h rule, or does the aggregation cover me instead?",
            "wants": "Resolution of the contradiction: the real leg is under 6h, so whether eligibility now rests on aggregation instead, or fails entirely.",
        },
    ],
})

# -------------------------------------------------------------------
# SET 06 -- ctx65: chat-fragments, slack, frustrated  (5-turn ENDURANCE)
# edge_profile: escalation + topic_switch + winding
# -------------------------------------------------------------------
sets.append({
    "set_id": "c19-s06",
    "batch": 19,
    "persona_tag": PERSONA,
    "session_channel": "chat-fragments",
    "entry_route": "slack",
    "turn_count": 5,
    "edge_profile": ["escalation", "topic_switch", "winding"],
    "turns": [
        {
            "turn": 1, "relationship": "opening",
            "momentary_state": "frustrated",
            "response_dependent": False, "depends_on_turn": None,
            "user_utterance": "my expense report got rejected again...",
            "wants": "To understand why an expense report was rejected and what to do next.",
        },
        {
            "turn": 2, "relationship": "supplies-detail-after-turn1",
            "momentary_state": "frustrated",
            "response_dependent": True, "depends_on_turn": 1,
            "user_utterance": "...it says receipt missing but i have it... just not itemized, only the total...",
            "wants": "Whether a non-itemized total-only receipt satisfies the receipt requirement, since that was the stated rejection reason.",
        },
        {
            "turn": 3, "relationship": "escalates-grievance-from-turn2",
            "momentary_state": "frustrated",
            "response_dependent": True, "depends_on_turn": 2,
            "user_utterance": "this is ridiculous since weeks i fight this... so what, the restaurant must reprint? nobody does that...",
            "wants": "A realistic path to satisfy the itemization rule when an itemized receipt is impractical to obtain.",
        },
        {
            "turn": 4, "relationship": "topic-switch-while-still-frustrated",
            "momentary_state": "frustrated",
            "response_dependent": True, "depends_on_turn": 3,
            "user_utterance": "forget it. different thing... my lounge access fee at the airport... that one is reimbursable or no?",
            "wants": "Whether airport lounge access fees are a reimbursable travel expense (new topic).",
        },
        {
            "turn": 5, "relationship": "winds-back-tying-lounge-to-original-rejection",
            "momentary_state": "frustrated",
            "response_dependent": True, "depends_on_turn": 4,
            "user_utterance": "ok and the lounge receipt is also only total no items... so same problem again?? is there ANY category where total-only is accepted...",
            "wants": "Whether the lounge expense hits the same itemization wall, and whether any expense category accepts total-only receipts -- winding back to the turn-2 itemization issue.",
        },
    ],
})

with open(path, "w") as f:
    for s in sets:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")

print("WROTE", path, "sets=", len(sets))
for s in sets:
    print(s["set_id"], s["turn_count"], s["edge_profile"])
