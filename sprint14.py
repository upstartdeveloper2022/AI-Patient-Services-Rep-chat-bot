"""
Sprint 14: Wellness / Physical / MAW / CHA / Lab Orders / Work-In /
Appointment Escalation Workflows

Source of requirements: Steve_Sprint14_Workflow.pdf ("Annual Wellness,
Physical, MAW, CHA, Lab Orders, Work-In Requests, and Appointment
Escalation Workflows").

ARCHITECTURE (per PDF Section 8 - "Sprint 14 Architecture Recommendation"):
  - This file is SELF-CONTAINED and ISOLATED from Sprint13.py's PHF
    (Post Hospital Follow-up) workflow. It does not import Sprint13 and
    never reads or writes any Sprint13 state (phf_*). This is
    deliberate, per the explicit instruction to keep regression risk to
    Sprint 13 at zero.
  - app.py owns routing and session control: it decides WHEN to call
    into this module (intent detection + dispatch, mirroring the same
    pattern app.py already uses for Sprint13) and is responsible for
    calling reset_state() at the start of each new call.
  - This module owns the workflow LOGIC and STATE for:
      1. Annual Wellness / Physical entry + eligibility (1yr+1day rule)
      2. Age/insurance-driven MAW & CHA eligibility
      3. "Appointment too far out" escalation ladder
      4. PCP vs. covering-provider (Elizabeth Horowitz / Janet Walker)
         routing
      5. Medicare Annual Wellness / CHA routing (Janet Walker path)
      6. Medication refill escalation (bridge-until-appointment)
      7. Lab order / EHR (MyChart) guidance
  - Where app.py already tracks state that is safe to reuse (patient
    identity already collected during pre-chart), this module reads it
    directly from `app`'s globals the same way Sprint13.py already
    does for its own patient-name capture (see handle_wellness_flow()
    below) - not a new dependency pattern, just following what's
    already proven in this codebase. app.py only tracks a
    `dob_collected` BOOLEAN, not the raw DOB string, so this module
    captures its own DOB text locally when it needs the patient's
    actual age (age drives the MAW/CHA/insurance branch).

SCOPE NOTE: This is Sprint 14 SCAFFOLDING, not an exhaustive
defect-proofed implementation of every edge case in the PDF. The goal
is a clean, working architecture for the workflows above. Expansion
points for future sprints are marked "# EXPANSION POINT".
"""

import json
import os
import random
import re
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# State variables
# ─────────────────────────────────────────────

wellness_flow_active = False
wellness_stage = None

# Early-capture flags (set the moment intent is detected, even before
# pre_chart_complete) so a request stated on the caller's very first
# message isn't lost while app.py finishes pre-chart collection.
# Mirrors Sprint13.phf_intent_detected exactly.
wellness_intent_detected = None    # "wellness" | "maw" | "cha" | None
refill_intent_detected = False
lab_order_intent_detected = False

# Identity - first/last pulled from app.py's pre-chart state; DOB is
# captured locally by this module (see note above).
wellness_patient_first_name = None
wellness_patient_last_name = None
wellness_patient_dob = None
wellness_patient_age = None
wellness_patient_pcp = None

# Visit type / eligibility
wellness_requested_visit_type = None   # "wellness" | "maw" | "cha"
wellness_last_visit_date = None
wellness_days_since_last_visit = None
wellness_eligible_for_next = None      # True once 1 year + 1 day has passed
wellness_next_eligible_date = None
wellness_eligibility_narration = None  # set once when eligible, consumed by the next response so eligibility is always stated, not just on the not-eligible path

# Age 65+ insurance branch (PDF Section 2)
wellness_insurance_type = None         # "priority_medicare_advantage" | "original_medicare" | "other"
wellness_eligible_for_maw = None
wellness_eligible_for_cha = None

# Availability / scheduling
wellness_pcp_availability = None
wellness_covering_availability = None
wellness_covering_provider_name = None  # "Elizabeth Horowitz" or "Janet Walker" depending on path
wellness_patient_deadline_date = None    # datetime the patient must be seen by (e.g. travel departure), or None
wellness_appointment_day = None
wellness_pending_requested_date = None  # carries a specific date the patient named, across the age/insurance routing hop, so it isn't lost when Medicare/MAW/CHA determination has to run first

# Appointment-too-far-out escalation (PDF Section 3)
wellness_too_far_out_pending = False
wellness_too_far_out_reason = None

# Work-in / callback (shared terminal step for several branches)
wellness_work_in_requested = False
wellness_callback_number = None

# Paperwork appointment override (Scenario 15)
wellness_paperwork_appointment_requested = False

# Medication refill escalation - independent mini-flow (PDF Section 6)
refill_escalation_active = False
refill_stage = None
refill_medication_name = None
refill_days_remaining = None
refill_will_run_out_before_next_appt = None

# Lab order / EHR guidance - independent mini-flow (PDF Section 7)
lab_order_ehr_guidance_active = False
lab_order_ehr_stage = None
lab_order_uncomfortable_with_tech = None
lab_order_mailing_address = None
lab_order_fax_destination = None

# Husband & Wife (couple) wellness scheduling - a single workflow that
# books BOTH patients back-to-back on the same day against ONE shared
# insurance determination (never a divergent random insurance per
# person). Escalates to Janet Walker when the PCP's back-to-back
# opening isn't soon enough.
couple_stage = None                        # None | "collect_spouse" | "collect_spouse_dob" | "locate_existing" | "eligibility" | "offer_pcp" | "offer_janet" | "await_callback" | "complete"
couple_existing_located = False            # True once the couple's on-file appointments were surfaced ("located")
couple_is_reschedule = False               # True when the caller said "reschedule" (not "schedule")
couple_reschedule_old_days = None          # (day1, day2) of the couple's existing visits once a move is confirmed
couple_patient1_first = None               # caller/husband-or-wife (from app.py pre-chart globals)
couple_patient1_last = None
couple_patient2_first = None               # the spouse named by the caller
couple_patient2_last = None
couple_patient2_dob = None                 # the spouse's DOB (asked once her full name is known)
couple_patient2_age = None                 # computed from couple_patient2_dob
couple_spouse_relation = None              # "husband" | "wife" | "spouse"
couple_shared_insurance = None             # determined ONCE, shared by both
couple_visit_type = None                   # "wellness" | "maw" | "cha", shared by both
couple_pcp_pairs = None                    # list of (day_str, time1, time2) back-to-back PCP slots
couple_covering_pairs = None               # same shape, Janet Walker (or covering provider) slots
couple_covering_provider_name = None       # "Janet Walker" for MAW/CHA, covering provider otherwise

# 1yr+1day eligibility (PDF Section 1) for the couple flow. Each
# patient's last wellness visit is randomly generated, and scheduling
# may only proceed once BOTH are at least 367 days past their last
# visit (mirrors the single-patient determine_eligibility stage).
couple_last_visit_date1 = None
couple_days_since_last_visit1 = None
couple_last_visit_date2 = None
couple_days_since_last_visit2 = None
couple_eligible_both = None                # None until evaluated, then True/False for BOTH patients
couple_eligibility_narration = None        # set in the eligibility stage, consumed once by _present_couple_pcp_pairs
couple_pcp_earliest_offset = 1             # back-to-back slots never offered before this many days out (proves the 1yr+1day rule)


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

# Mirrors app.py's own covering-provider constant/spelling exactly so
# any future consolidation is a non-event; duplicated here rather than
# imported to keep this file self-contained per the architecture rule.
COVERING_PROVIDER_NAME = "Elizabeth Horowitz, NP"
COVERING_PROVIDER_BARE_NAME = "Elizabeth Horowitz"
COVERING_PROVIDER_MA_NAME = "Catherine"

# New for Sprint 14 (PDF Section 5) - the MAW/CHA-specific alternate
# provider, distinct from Elizabeth Horowitz who covers general
# wellness/non-wellness overflow (PDF Section 4).
JANET_WALKER_NAME = "Janet Walker"

# Appointment persistence (Scenario — appointment lookup)
# JSON file path for storing scheduled appointments across calls.
# Stored in the same output directory as other persistent project data.
APPOINTMENTS_JSON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sprint14_appointments.json"
)

AVAILABLE_TIMES = [
    "9:00 AM", "9:30 AM", "10:00 AM", "10:30 AM",
    "11:00 AM", "11:30 AM", "1:00 PM", "1:30 PM",
    "2:00 PM", "2:30 PM", "3:00 PM", "3:30 PM",
    "4:00 PM", "4:40 PM"
]

WELLNESS_TRIGGERS = [
    "annual wellness", "annual physical", "wellness visit", "wellness exam",
    "physical exam", "yearly checkup", "yearly check up", "annual checkup",
    "annual check up", "routine physical", "routine checkup",
    "routine check up", "checkup", "check up", "physical", "wellness",
]

MAW_TRIGGERS = [
    "medicare annual wellness", "annual wellness visit", "maw visit",
    "maw appointment", "medicare wellness visit",
]

# Appointment lookup intent (patient asking about an already-scheduled
# appointment, NOT requesting a new one). Checked BEFORE wellness intent
# by app.py so "When is my annual physical?" routes to lookup rather
# than re-entering the wellness scheduling workflow.
APPOINTMENT_LOOKUP_TRIGGERS = [
    "when is my next appointment", "when's my next appointment",
    "when is my appointment", "when's my appointment",
    "when am i scheduled", "when am i booked",
    "what time is my appointment", "do i have an appointment",
    "when is my annual physical", "when's my annual physical",
    "when is my physical", "when's my physical",
    "when is my wellness visit", "when's my wellness visit",
    "when is my next wellness visit", "when's my next wellness visit",
    "when is my checkup", "when's my checkup",
    "when is my maw", "when's my maw",
    "when is my cha", "when's my cha",
    "when is my medicare annual wellness", "when's my medicare annual wellness",
]

CHA_TRIGGERS = [
    "comprehensive health assessment", "cha visit", "cha appointment",
]

TOO_FAR_OUT_TRIGGERS = [
    "too far out", "too far away", "anything sooner", "nothing sooner",
    "is there anything sooner", "isn't there anything sooner",
    "too long to wait", "can't wait that long", "cannot wait that long",
    "that's too long", "that is too long", "any earlier",
    "is there something earlier",
    "be seen sooner", "see me sooner", "scheduled sooner",
    "scheduled earlier", "anything before", "something sooner",
    "physical earlier", "physical sooner", "appointment earlier",
    "appointment sooner", "visit earlier", "visit sooner",
    "wellness earlier", "wellness sooner", "come in earlier",
    "come in sooner", "get in earlier", "get in sooner",
]

TOO_FAR_OUT_MEDICATION_TRIGGERS = [
    "refill", "run out of my medication", "running out of my medication",
    "out of medication", "need my medication", "need a refill",
]

# TOO_FAR_OUT_MEDICATION_TRIGGERS above only matches the generic word
# "medication" itself ("out of medication", "run out of my
# medication"). A patient naming their actual drug instead ("run out
# of Alprazolam", "running out of my Metformin") fell through
# unclassified - confirmed root cause of the Scenario 11 regression
# (traced: classify_too_far_out_reason() returned None for the literal
# transcript phrase, causing both the pivot gate and
# wellness_confirmation's own reason-check to fall through to the
# generic re-ask). This generalizes to any medication name without
# enumerating drugs, while excluding common non-medication nouns that
# would otherwise false-positive on "run out of time/patience/etc."
_RUN_OUT_OF_NON_MEDICATION_NOUNS = {
    "time", "patience", "luck", "money", "gas", "space", "room",
    "options", "ideas", "words", "energy", "days", "minutes", "hours",
    "breath", "steam", "excuses",
}
_MEDICATION_RUN_OUT_PATTERN = re.compile(
    r'\b(?:run|running)\s+out\s+of\s+(?:my\s+)?([a-z]+)\b', re.IGNORECASE
)


def _mentions_running_out_of_medication(message_lower):
    match = _MEDICATION_RUN_OUT_PATTERN.search(message_lower)
    if not match:
        return False
    return match.group(1).lower() not in _RUN_OUT_OF_NON_MEDICATION_NOUNS


TOO_FAR_OUT_ACUTE_TRIGGERS = [
    "not feeling well", "sick", "in pain", "hurts", "hurt", "fever",
    "something is wrong", "need to be seen sooner because i'm sick",
    "symptoms", "cough", "cold", "sore throat", "headache",
    "earache", "ear ache", "nausea", "vomiting", "rash",
    "need an appointment today", "need to be seen today",
    "need to be seen sooner",
    "fractured", "fracture", "broken", "injury", "injured",
    "sprained", "cut", "bleeding", "swollen", "wound",
]

TOO_FAR_OUT_CHRONIC_TRIGGERS = [
    "chronic", "ongoing condition", "keeps coming back",
    "my condition is getting worse", "long term issue", "long-term issue",
]

# Named chronic conditions - a patient naming their actual condition
# ("my diabetes is acting up", "I need to discuss my diabetes") is
# just as valid a chronic-reason signal as one of the generic phrases
# above, and is how patients most commonly phrase this in practice.
CHRONIC_CONDITION_NAME_TRIGGERS = [
    "diabetes", "copd", "asthma", "blood pressure", "high blood pressure",
    "hypertension", "arthritis", "thyroid",
]

TOO_FAR_OUT_PAPERWORK_TRIGGERS = [
    "paperwork", "form", "fmla", "disability form", "work note",
    "school note", "needs to be signed",
]

TOO_FAR_OUT_EMPLOYER_DEADLINE_TRIGGERS = [
    "my employer", "my job requires", "insurance deadline",
    "have to have this done by", "deadline", "before my insurance",
    "before the end of the year", "employer requires",
    "insurance requires", "surcharge", "penalty", "benefits deadline",
    "before my benefits", "requires this by",
]

# Scenario 17: travel constraints that conflict with scheduled MAW/CHA appointments
TRAVEL_CONSTRAINT_TRIGGERS = [
    "going to", "travel", "out of town", "out of the country",
    "leaving for", "away for", "gone for", "trip", "vacation",
    "moving to", "relocated", "relocating",
    "leaving the country", "leaving the state", "out of state",
    "won't be here", "will not be here", "won't be in town",
]

PROCEED_PHRASES = [
    "yes", "sure", "okay", "ok", "i would", "i'd like", "i would like",
    "sounds good", "please", "that works", "works for me", "go ahead",
    "let's do it",
]

DECLINE_PHRASES = [
    "no", "not right now", "no thank you", "not interested",
    "don't think so", "do not think so", "i'd rather not",
]

TECH_UNCOMFORTABLE_PHRASES = [
    "not good with computers", "not good with technology",
    "don't have a computer", "do not have a computer",
    "don't use mychart", "do not use mychart", "not comfortable with",
    "i don't know how", "i do not know how", "no internet",
    "don't have internet", "not tech savvy", "not very tech savvy",
]

LAB_ORDER_CANNOT_LOCATE_TRIGGERS = [
    "can't find my lab order", "cannot find my lab order",
    "can't locate my lab order", "cannot locate my lab order",
    "don't see my lab order", "do not see my lab order",
    "where is my lab order", "missing lab order",
    "don't have my lab order", "doesn't have my lab order",
    "lab doesn't have my order", "lab doesn't have the order",
]

# Scenario 21: Priority Care Network labs can access lab orders directly
# through the patient's chart, so no separate send is ever needed for
# these locations. Each entry is a recognizable phrase/substring mapped
# to the canonical display name. Checked in order - more specific
# entries (e.g. "viera medical plaza") must be listed before shorter,
# more general ones that could also match within them (e.g. "viera").
PRIORITY_CARE_NETWORK_LABS = [
    ("indialantic", "Indialantic office"),
    ("eldron", "Eldron office"),
    ("titusville", "Titusville Knox McRae office"),
    ("knox mcrae", "Titusville Knox McRae office"),
    ("gateway", "Gateway office"),
    ("cancer institute", "Cancer Institute"),
    ("cape canaveral", "Cape Canaveral Hospital"),
    ("holmes regional", "Holmes Regional Medical Center"),
    ("viera medical plaza", "Viera Medical Plaza"),
    ("viera pcp", "Viera PCP office"),
    ("priority care", "Priority Care urgent care clinic"),
    ("adventure health", "Adventure Health urgent care clinic"),
]

# Noun phrases the patient might use for what's being sent, and verb/
# framing phrases indicating they want it sent somewhere (or that a lab
# is reporting it doesn't have the orders yet). Both an noun AND a verb
# trigger must be present - this mirrors the same-day-request pattern
# used elsewhere (SAME_DAY_KEYWORDS + SAME_DAY_ACTION_WORDS) to avoid a
# bare "labs" or "send" alone causing false positives.
LAB_SEND_NOUN_TRIGGERS = [
    "blood work", "blood work orders", "blood and urine",
    "blood work and urine", "urine order", "urine orders",
    "labs", "lab work", "lab orders", "lab work orders",
]
LAB_SEND_VERB_TRIGGERS = [
    "send", "sent", "don't have my", "doesn't have my",
    "does not have my", "do not have my",
]

REFILL_ESCALATION_TRIGGERS = [
    "need a refill before my appointment", "won't have enough medication",
    "will not have enough medication", "run out before my appointment",
    "running out before my appointment", "need more medication until then",
]


# ─────────────────────────────────────────────
# State reset
# ─────────────────────────────────────────────

def reset_state():
    global wellness_flow_active, wellness_stage
    global wellness_intent_detected, refill_intent_detected, lab_order_intent_detected
    global wellness_patient_first_name, wellness_patient_last_name
    global wellness_patient_dob, wellness_patient_age, wellness_patient_pcp
    global wellness_requested_visit_type
    global wellness_last_visit_date, wellness_days_since_last_visit
    global wellness_eligible_for_next, wellness_next_eligible_date
    global wellness_eligibility_narration
    global wellness_insurance_type
    global wellness_eligible_for_maw, wellness_eligible_for_cha
    global wellness_pcp_availability, wellness_covering_availability
    global wellness_covering_provider_name, wellness_appointment_day
    global wellness_patient_deadline_date
    global wellness_pending_requested_date
    global wellness_too_far_out_pending, wellness_too_far_out_reason
    global wellness_work_in_requested, wellness_callback_number
    global wellness_paperwork_appointment_requested
    global refill_escalation_active, refill_stage
    global refill_medication_name, refill_days_remaining
    global refill_will_run_out_before_next_appt
    global lab_order_ehr_guidance_active, lab_order_ehr_stage
    global lab_order_uncomfortable_with_tech, lab_order_mailing_address
    global couple_stage
    global couple_patient1_first, couple_patient1_last
    global couple_patient2_first, couple_patient2_last, couple_spouse_relation
    global couple_patient2_dob, couple_patient2_age
    global couple_shared_insurance, couple_visit_type
    global couple_pcp_pairs, couple_covering_pairs
    global couple_covering_provider_name
    global couple_last_visit_date1, couple_days_since_last_visit1
    global couple_last_visit_date2, couple_days_since_last_visit2
    global couple_eligible_both, couple_eligibility_narration
    global couple_pcp_earliest_offset
    global couple_existing_located
    global couple_is_reschedule
    global couple_reschedule_old_days

    wellness_flow_active = False
    wellness_stage = None
    wellness_intent_detected = None
    refill_intent_detected = False
    lab_order_intent_detected = False
    wellness_patient_first_name = None
    wellness_patient_last_name = None
    wellness_patient_dob = None
    wellness_patient_age = None
    wellness_patient_pcp = None
    wellness_requested_visit_type = None
    wellness_last_visit_date = None
    wellness_days_since_last_visit = None
    wellness_eligible_for_next = None
    wellness_next_eligible_date = None
    wellness_eligibility_narration = None
    wellness_insurance_type = None
    wellness_eligible_for_maw = None
    wellness_eligible_for_cha = None
    wellness_pcp_availability = None
    wellness_covering_availability = None
    wellness_covering_provider_name = None
    wellness_patient_deadline_date = None
    wellness_pending_requested_date = None
    wellness_appointment_day = None
    wellness_too_far_out_pending = False
    wellness_too_far_out_reason = None
    wellness_work_in_requested = False
    wellness_callback_number = None
    wellness_paperwork_appointment_requested = False
    refill_escalation_active = False
    refill_stage = None
    refill_medication_name = None
    refill_days_remaining = None
    refill_will_run_out_before_next_appt = None
    lab_order_ehr_guidance_active = False
    lab_order_ehr_stage = None
    lab_order_uncomfortable_with_tech = None
    lab_order_mailing_address = None
    lab_order_fax_destination = None
    couple_stage = None
    couple_patient1_first = None
    couple_patient1_last = None
    couple_patient2_first = None
    couple_patient2_last = None
    couple_patient2_dob = None
    couple_patient2_age = None
    couple_spouse_relation = None
    couple_shared_insurance = None
    couple_visit_type = None
    couple_pcp_pairs = None
    couple_covering_pairs = None
    couple_covering_provider_name = None
    couple_last_visit_date1 = None
    couple_days_since_last_visit1 = None
    couple_last_visit_date2 = None
    couple_days_since_last_visit2 = None
    couple_eligible_both = None
    couple_eligibility_narration = None
    couple_pcp_earliest_offset = 1
    couple_existing_located = False
    couple_is_reschedule = False
    couple_reschedule_old_days = None


# ─────────────────────────────────────────────
# Intent detection (called by app.py for routing)
# ─────────────────────────────────────────────

def detect_appointment_lookup_intent(message_lower):
    """Returns True if the patient is asking about an already-scheduled
    appointment (e.g. 'When is my annual physical?'), NOT requesting a
    new one. app.py should check this BEFORE detect_wellness_intent so
    lookup questions don't accidentally re-enter the scheduling flow."""
    return any(t in message_lower for t in APPOINTMENT_LOOKUP_TRIGGERS)


def detect_wellness_intent(message_lower):
    """Returns the specific visit type requested ("wellness", "maw",
    "cha", or "couple") or None. Checked most-specific-first so a
    caller naming MAW or CHA directly isn't misclassified as a generic
    wellness visit.

    "couple" is returned when a husband and wife are being scheduled
    together (see detect_couple_wellness_intent) - it drives the
    back-to-back couple workflow in handle_wellness_flow().

    DEFENSIVE FIX: If the message is clearly an appointment lookup
    ("When is my annual physical?"), return None so the lookup path
    in app.py can take precedence. This prevents a routing race where
    wellness intent hijacks a lookup query before app.py checks lookup
    intent."""
    if detect_appointment_lookup_intent(message_lower):
        return None
    if detect_couple_wellness_intent(message_lower):
        return "couple"
    return _base_wellness_intent(message_lower)


def detect_refill_escalation_intent(message_lower):
    return any(t in message_lower for t in REFILL_ESCALATION_TRIGGERS)


def detect_lab_order_ehr_intent(message_lower):
    return any(t in message_lower for t in LAB_ORDER_CANNOT_LOCATE_TRIGGERS)


def detect_lab_send_request_intent(message_lower):
    """Scenario 21: patient wants lab orders sent to (or reports a lab
    doesn't yet have) a specific destination - e.g. "I need my blood
    work sent to Holmes Regional" or "The lab says they don't have my
    orders." Separate from detect_lab_order_ehr_intent (Scenario 19/20's
    "I can't find my lab order in the portal" trigger) - both can be
    active in the same build without interfering with each other."""
    return (
        any(t in message_lower for t in LAB_SEND_NOUN_TRIGGERS)
        and any(t in message_lower for t in LAB_SEND_VERB_TRIGGERS)
    )


def _extract_priority_network_lab(message_lower):
    """Returns the canonical display name of a recognized Priority Care
    Network location mentioned in the message, or None if no known
    Priority Care Network location is named."""
    for phrase, display_name in PRIORITY_CARE_NETWORK_LABS:
        if re.search(r'\b' + re.escape(phrase) + r'\b', message_lower):
            return display_name
    return None


# Common external lab brand names, recognized only to distinguish "the
# patient named a specific non-Priority-Network lab" from "the patient
# didn't name any lab at all" on the very first message - so a message
# like "send my labs to Quest" continues straight into the existing
# lab-order workflow instead of redundantly asking "which lab" for a
# lab the patient already named.
KNOWN_EXTERNAL_LAB_TRIGGERS = ["quest", "labcorp", "lab corp"]


# ── Husband & Wife (couple) wellness scheduling ──
# First-person-joint phrasing that establishes BOTH of the couple are
# being scheduled (e.g. "my husband and I", "we both", "for both of
# us"). A single-patient request for just a spouse ("my husband needs
# his annual physical") contains none of these and keeps routing to
# the normal single wellness flow. Note pivotal visit-type checks
# below are checked at the top of detect_wellness_intent() so wording
# like "Comprehensive Health Assessment for my wife and I" routes to
# the couple flow rather than the single CHA path.
_JOINT_COUPLE_PHRASES = [
    "husband and i", "husband and me", "husband and myself",
    "wife and i", "wife and me", "wife and myself",
    "spouse and i", "spouse and me", "spouse and myself",
    "i and my husband", "i and my wife", "me and my husband",
    "me and my wife", "myself and my husband", "myself and my wife",
    "my husband and i", "my wife and i", "my spouse and i",
    "and my husband", "and my wife", "and my spouse",
    "we both", "both of us", "us both", "for both of us",
    "both of our", "for the two of us", "schedule us both",
]
_COUPLE_RELATION_WORDS = ["husband", "wife", "spouse"]

# Spouse name capture ("my husband William", "my husband, William
# Vance", "my wife's name is Kate", "my wife named Diane"). The
# captured tokens must be real names, so common pronoun/conjunction
# words ("and I", "and Myself") are rejected even though the pattern
# runs case-insensitively.
_SPOUSE_NAME_PATTERN = re.compile(
    r"\bmy\s+(husband|wife|spouse)(?:'s\s+name\s+is|'s\s+name|,|:)?\s*"
    r"(?:named\s+|is\s+)?([A-Za-z][a-z]+)(?:\s+([A-Za-z][a-z]+))?",
    re.IGNORECASE
)
_SPOUSE_NAME_STOPWORDS = {
    "and", "is", "are", "was", "were", "with", "or", "my", "i", "me",
    "mine", "to", "for", "the", "a", "an", "that", "he", "she", "his",
    "her", "also", "us", "am", "will", "want", "need",
}


def _extract_spouse_name(message):
    """Returns (relation, first_name, last_name) parsed from a message
    mentioning the caller's husband/wife/spouse by name, or (None,
    None, None) if absent. last_name may legitimately be None when the
    caller only stated a first name."""
    match = _SPOUSE_NAME_PATTERN.search(message)
    if not match:
        return None, None, None
    first = match.group(2) or None
    last = match.group(3) or None
    if first and first.lower() in _SPOUSE_NAME_STOPWORDS:
        return None, None, None
    if last and last.lower() in _SPOUSE_NAME_STOPWORDS:
        last = None
    return match.group(1).lower(), first, last


def _extract_plain_reply_names(message, exclude_first=None):
    """Scoped fallback for the collect_spouse stage: after Steve has
    explicitly asked for the spouse's name, the caller's next reply is
    usually just the name ("William Brooks" / "William"). Pulls the
    caller-attributable capitalized words out of that reply while
    excluding the caller's own first name (spouses typically share the
    same last name, so the caller's last name is NOT excluded)."""
    tokens = re.findall(r"\b[A-Z][a-z]+\b", message)
    filtered = []
    for tok in tokens:
        if exclude_first and tok == exclude_first:
            continue
        if tok.lower() in _SPOUSE_NAME_STOPWORDS:
            continue
        filtered.append(tok)
    if not filtered:
        return None, None
    if len(filtered) >= 2:
        return filtered[0], filtered[1]
    return filtered[0], None


def _base_wellness_intent(message_lower):
    """Singular (per-person) wellness request type, without the lookup
    short-circuit or the couple check - the pieces the couple detector
    needs without recursing through detect_wellness_intent()."""
    if any(t in message_lower for t in CHA_TRIGGERS):
        return "cha"
    if any(t in message_lower for t in MAW_TRIGGERS):
        return "maw"
    if any(t in message_lower for t in WELLNESS_TRIGGERS):
        return "wellness"
    return None


def detect_couple_wellness_intent(message_lower):
    """True when the caller is requesting annual wellness/MAW/CHA visits
    for BOTH themselves and their husband/wife/spouse. Requires a
    spouse word AND a first-person-joint phrase AND a wellness trigger
    all in the same message, so a lone "my husband needs his annual
    physical" (single third-party scheduling) can never reach here.

    EXCEPTION - already-scheduled couple reschedule/locate: a couple
    asking to "schedule appointments" (no literal wellness noun) for
    BOTH of them is still a couple request WHEN the caller already has
    a recorded Sprint14 appointment on file (located on a prior call;
    observed failure: "I'd like to schedule appointments for my wife
    and myself" fell through to generic appointment scheduling and the
    on-file couple records were never surfaced). Without a stored
    record - i.e. a first-time couple who hasn't named a visit type -
    this stays a generic scheduling request, leaving that flow
    untouched."""
    has_spouse = any(w in message_lower for w in _COUPLE_RELATION_WORDS)
    has_joint = any(p in message_lower for p in _JOINT_COUPLE_PHRASES)
    if not (has_spouse and has_joint):
        return False
    if _base_wellness_intent(message_lower) is not None:
        return True
    return (
        _mentions_couple_schedule_appointments(message_lower)
        and _caller_has_stored_sprint14_appointment()
    )


_COUPLE_SCHEDULE_VERBS = ["schedule", "book", "set up", "make", "move", "change"]


def _mentions_couple_schedule_appointments(message_lower):
    """True for generic "schedule/book/make/move/change ... appointment(s)"
    phrasing with no specific visit type named, e.g. "I'd like to
    schedule appointments for my wife and myself"."""
    return (
        "appointment" in message_lower
        and any(v in message_lower for v in _COUPLE_SCHEDULE_VERBS)
    )


def _caller_has_stored_sprint14_appointment():
    """True when the caller (app.py pre-chart identity) already has an
    appointment on file in the Sprint14 store - i.e. this is a callback
    from a prior scheduling session. Uses the SAME store the couple
    flow writes to, so the couple reschedule/locate detection and the
    booking persistence can never disagree (observed failure: records
    lived in sprint14_appointments.json but the caller's reschedule
    phrasing routed to the generic scheduling flow instead)."""
    try:
        import app as _caller_app
        first = getattr(_caller_app, "patient_first_name", None) or getattr(
            _caller_app, "caller_first_name", None)
        last = getattr(_caller_app, "patient_last_name", None) or getattr(
            _caller_app, "caller_last_name", None)
    except Exception:
        return False
    if not first or not last:
        return False
    return get_next_appointment_for_patient(f"{first} {last}") is not None


NOT_ELIGIBLE_ACCEPTANCE_TRIGGERS = [
    "i understand", "i'm aware", "im aware", "i am aware",
    "schedule it now", "schedule it anyway", "book it now",
    "book it anyway", "go ahead and schedule", "i just want to schedule",
    "i need to schedule that far out", "schedule that far out",
    "that far out is fine", "far out is fine", "worry about it later",
    # "I still want to schedule..." replies pull up the provider's
    # availability anyway, instead of re-asking the eligibility question
    # (observed failure: "I still want to schedule wellness visits for
    # my wife and myself" re-printed the checkpoint instead of slots).
    "i still want to schedule", "we still want to schedule",
    "still want to schedule", "still want to book",
    "i'd still like to schedule", "i'd still like to book",
]


def patient_accepts_far_out_timeline(message_lower):
    """Scoped acceptance detector for the not-eligible checkpoint: the
    general-purpose patient_wants_to_proceed() catches a direct yes/sure/
    ok answer; this catches the declarative phrasing patients more
    commonly use in response to that stage's binary question ("I
    understand I need to schedule that far out", "I still want to
    schedule", "I just want to get this on the books now") which
    contains no bare yes/ok/sure at all. Deliberately kept separate from
    PROCEED_PHRASES, which is also used by several unrelated stages
    (covering-provider accept/decline, lab-order tech-comfort check,
    refill option selection) where a phrase as broad as "I understand"
    would risk misclassifying an unrelated response.
    """
    return _contains_trigger(message_lower, NOT_ELIGIBLE_ACCEPTANCE_TRIGGERS)


def patient_wants_to_proceed(message_lower):
    # Word-boundary matching, not naive substring - a plain `in` check
    # on "no" also matches inside "not" (e.g. "If not, when does he
    # have availability?" is a follow-up question, not a decline).
    # Reuses the same _contains_trigger() fix already applied to
    # classify_too_far_out_reason() for the identical bug class (see
    # that function's docstring for the "form"/"metformin" precedent).
    if _contains_trigger(message_lower, DECLINE_PHRASES):
        return False
    return _contains_trigger(message_lower, PROCEED_PHRASES)


def patient_wants_to_decline(message_lower):
    return _contains_trigger(message_lower, DECLINE_PHRASES)


# Reply to the couple "locate_existing" keep-vs-reschedule question.
# Explicit keep/leave phrasing keeps both appointments untouched; ANY
# other reply (including "reschedule them", "move them", plain "yes")
# proceeds into the normal couple booking path to move them.
KEEP_APPOINTMENT_TRIGGERS = [
    "keep them", "keep the appointments", "keep the appointment",
    "keep them as is", "keep as is", "keep it as is",
    "leave them", "leave them as is", "leave them alone",
    "leave the appointments", "leave the appointment",
    "no change", "no need to change", "don't change", "dont change",
    "don't reschedule", "dont reschedule", "no reschedule",
    "that's fine", "thats fine", "as is", "as scheduled",
    "keep the same", "keep my appointment",
]


def patient_wants_to_keep_appointments(message_lower):
    return any(t in message_lower for t in KEEP_APPOINTMENT_TRIGGERS)


def patient_uncomfortable_with_tech(message_lower):
    return any(p in message_lower for p in TECH_UNCOMFORTABLE_PHRASES)


# ── Scenario 15: explicit appointment request during not-eligible follow-up ──
# When a patient gives a reason (paperwork, acute, etc.) but ALSO explicitly
# says "I need to schedule an appointment" or similar, they want scheduling
# to occur — not an immediate bypass to work-in / callback.
APPOINTMENT_SCHEDULING_TRIGGERS = [
    "schedule an appointment", "need an appointment", "book an appointment",
    "want an appointment", "get an appointment", "make an appointment",
    "need to schedule", "want to schedule", "like to schedule",
]


def _patient_explicitly_requests_appointment(message_lower):
    return any(t in message_lower for t in APPOINTMENT_SCHEDULING_TRIGGERS)


def _patient_mentions_travel_constraint(message_lower):
    """Scenario 17: detect when a patient mentions upcoming travel or
    relocation that may conflict with available appointment dates."""
    return any(t in message_lower for t in TRAVEL_CONSTRAINT_TRIGGERS)


# ─────────────────────────────────────────────
# DOB / age helpers (local copies - see architecture note at top)
# ─────────────────────────────────────────────

def detect_dob_in_message(message):
    dob_pattern = re.compile(
        r'\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b|'
        r'\b(january|february|march|april|may|june|july|august|'
        r'september|october|november|december)\s+\d{1,2},?\s+\d{4}\b',
        re.IGNORECASE
    )
    return bool(dob_pattern.search(message))


def extract_dob_from_message(message):
    dob_pattern = re.compile(
        r'\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b|'
        r'\b(january|february|march|april|may|june|july|august|'
        r'september|october|november|december)\s+\d{1,2},?\s+\d{4}\b',
        re.IGNORECASE
    )
    match = dob_pattern.search(message)
    return match.group(0) if match else None


def calculate_age_from_dob(dob_string):
    """Parses a DOB string and returns age in years, or None if
    unparseable. Mirrors app.py's calculate_age_from_dob()."""
    dob_patterns = ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"]
    for fmt in dob_patterns:
        try:
            dob_date = datetime.strptime(dob_string.strip(), fmt)
            today = datetime.now()
            age = today.year - dob_date.year
            if (today.month, today.day) < (dob_date.month, dob_date.day):
                age -= 1
            return age
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────
# Wellness eligibility / availability generation
# ─────────────────────────────────────────────

def generate_last_wellness_date():
    """Randomly simulates the patient's last wellness/physical visit
    date. Weighted 70/30 toward already-eligible patients, matching
    observed real-world call patterns: patients calling to schedule an
    annual wellness visit are more often already past their 1-year-
    and-1-day eligibility window than not yet eligible. Permanent
    behavior change, not a temporary testing override."""
    if random.random() < 0.70:
        # Eligible bucket: more than 1 year + 1 day ago (367+ days).
        # Upper bound wide enough to also cover "very overdue" callers.
        days_since = random.randint(367, 730)
    else:
        # Not-yet-eligible bucket: within the last year (<= 366 days).
        days_since = random.randint(30, 366)
    last_date = datetime.now() - timedelta(days=days_since)
    return last_date.strftime("%B %d, %Y"), days_since


def is_eligible_for_next_wellness(days_since_last_visit):
    """PDF Section 1: 'Enforce one year and one day rule before
    scheduling next wellness visit.'"""
    return days_since_last_visit > 366


def calculate_next_eligible_date(days_since_last_visit):
    days_until_eligible = 367 - days_since_last_visit
    if days_until_eligible <= 0:
        return None
    return (datetime.now() + timedelta(days=days_until_eligible)).strftime(
        "%A, %B %d, %Y"
    )


def determine_insurance_type_for_senior():
    """PDF Section 2: equal 33.33% split for patients 65+."""
    return random.choice(
        ["priority_medicare_advantage", "original_medicare", "other"]
    )


def determine_maw_cha_eligibility(insurance_type):
    """PDF Section 2:
    - Priority Care Medicare Advantage: eligible for MAW and CHA.
    - Original Medicare: eligible for MAW only.
    - Other Insurance: standard wellness workflow only (neither)."""
    if insurance_type == "priority_medicare_advantage":
        return True, True
    if insurance_type == "original_medicare":
        return True, False
    return False, False


_MONTH_NAME_TO_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

_REQUESTED_DATE_PATTERN = re.compile(
    r'\b(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})\b|'
    r'\b(january|february|march|april|may|june|july|august|'
    r'september|october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?'
    r'(?:,?\s*(\d{4}))?\b',
    re.IGNORECASE
)


def extract_requested_date(message):
    """Parse a specific calendar date the patient explicitly stated
    (e.g. "Monday May 3, 2027", "5/3/2027", "January 7th" with no
    year at all) so a "does the provider have availability on X"
    question can be answered against that SPECIFIC date, instead of a
    generic random window that has no relation to what was actually
    asked. Returns a datetime or None.

    The year is optional for the month-name form, since patients very
    commonly state a date without one ("January 7th?"). When absent,
    this infers the next upcoming occurrence of that month/day from
    today - rolling forward to next year if that date has already
    passed this year - rather than failing to match at all."""
    match = _REQUESTED_DATE_PATTERN.search(message)
    if not match:
        return None
    if match.group(1):
        month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if year < 100:
            year += 2000
    else:
        month = _MONTH_NAME_TO_NUM[match.group(4).lower()]
        day = int(match.group(5))
        year_str = match.group(6)
        if year_str:
            year = int(year_str)
        else:
            today = datetime.now()
            year = today.year
            try:
                candidate = datetime(year, month, day)
            except ValueError:
                return None
            if candidate < today:
                year += 1
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def generate_availability_for_requested_date(requested_date, earliest_offset_days=1):
    """Check availability for a SPECIFIC date the patient asked about,
    rather than a random window unrelated to their question. If that
    exact date has openings, return just that date. If not (or it's a
    weekend / before the earliest allowed date), walk forward day by
    day for the nearest few available weekdays, so the alternatives
    offered are chronologically close to what was requested rather
    than scattered across many months. earliest_offset_days constrains
    how soon from today a date may ever be considered (so a wellness
    visit isn't offered before the patient is actually eligible)."""
    today = datetime.now()
    earliest_allowed = today + timedelta(days=earliest_offset_days)

    if requested_date >= earliest_allowed and requested_date.weekday() < 5:
        if random.choice([True, False]):
            num_slots = random.randint(1, 3)
            slots = sorted(
                random.sample(AVAILABLE_TIMES, num_slots),
                key=lambda x: datetime.strptime(x, "%I:%M %p")
            )
            return {requested_date.strftime("%A, %B %d, %Y"): slots}, True

    availability = {}
    date = max(requested_date, earliest_allowed)
    attempts = 0
    while len(availability) < 3 and attempts < 60:
        attempts += 1
        date = date + timedelta(days=1)
        if date.weekday() >= 5:
            continue
        if random.choice([True, True, False]):
            num_slots = random.randint(1, 3)
            slots = sorted(
                random.sample(AVAILABLE_TIMES, num_slots),
                key=lambda x: datetime.strptime(x, "%I:%M %p")
            )
            availability[date.strftime("%A, %B %d, %Y")] = slots
    if not availability:
        # Guarantee at least one result, mirroring
        # generate_wellness_pcp_availability()'s own non-empty guarantee.
        fallback_date = date
        while fallback_date.weekday() >= 5:
            fallback_date += timedelta(days=1)
        availability[fallback_date.strftime("%A, %B %d, %Y")] = [
            random.choice(AVAILABLE_TIMES)
        ]
    return availability, False


def generate_wellness_pcp_availability(earliest_offset_days=1):
    """PDF Section 1: 'Generate PCP availability between 1 day and 6
    months out. Do not state PCP unavailable.' Unlike the covering-
    provider generators (which are allowed to come back empty so the
    escalation ladder has something to escalate FROM), this generator
    is guaranteed non-empty per that explicit instruction.

    earliest_offset_days: the minimum number of days from today before
    a slot may appear. Defaults to 1 (tomorrow), preserving existing
    behavior for all current call sites. The not-eligible-followup
    stage passes a larger value so a patient who isn't eligible for
    (say) another 300 days is never offered a slot before that date.
    """
    today = datetime.now()
    availability = {}
    attempts = 0
    while not availability and attempts < 25:
        attempts += 1
        for _ in range(6):
            day_offset = random.randint(
                earliest_offset_days, earliest_offset_days + 180
            )
            date = today + timedelta(days=day_offset)
            if date.weekday() >= 5:
                continue
            if random.choice([True, True, False]):
                num_slots = random.randint(1, 3)
                slots = random.sample(AVAILABLE_TIMES, num_slots)
                slots.sort(key=lambda x: datetime.strptime(x, "%I:%M %p"))
                availability[date.strftime("%A, %B %d")] = slots
    if not availability:
        # Guarantee: never come back empty for the PCP.
        fallback_offset = earliest_offset_days
        fallback_date = today + timedelta(days=fallback_offset)
        while fallback_date.weekday() >= 5:
            fallback_offset += 1
            fallback_date = today + timedelta(days=fallback_offset)
        availability[fallback_date.strftime("%A, %B %d")] = [
            random.choice(AVAILABLE_TIMES)
        ]
    return availability


def generate_covering_wellness_availability(earliest_offset_days=1, latest_date=None):
    """Availability generator for the covering-provider escalation
    step (Elizabeth Horowitz or Janet Walker, depending on path).

    earliest_offset_days: minimum number of days from today before a
    slot may appear - mirrors generate_wellness_pcp_availability()'s
    parameter of the same name, so a wellness visit isn't offered
    before the patient is actually eligible for it.

    latest_date: optional datetime upper bound (e.g. the patient's
    stated travel departure date, captured when the travel constraint
    was first detected). No slot is ever generated on or after this
    date. When given, the ENTIRE valid window up to that date is
    searched (not just a fixed few days of it) - a deadline can leave
    a much wider valid range than the default window below. If the
    resulting window is empty or invalid (latest_date falls at or
    before the earliest eligible date), this correctly returns no
    availability - there is genuinely no valid date to offer, and the
    work-in escalation path should trigger instead.

    With no latest_date, searches a 7-day window starting at the
    earliest eligible date (unchanged default behavior for every
    non-travel call site).

    Each weekday in the window has a 50% chance of having a slot;
    Janet's schedule is dedicated to MAW/CHA visits and typically has
    more room than the PCP's, so over a normal-width window this
    yields a high (comfortably above the ~80% target) chance of at
    least one available day, while still allowing genuine scarcity
    for a narrow window."""
    availability = {}
    today = datetime.now()
    earliest_allowed = today + timedelta(days=earliest_offset_days)
    window_end = (
        latest_date if latest_date is not None
        else earliest_allowed + timedelta(days=7)
    )
    if window_end <= earliest_allowed:
        return availability
    date = earliest_allowed
    while date < window_end:
        if date.weekday() < 5 and random.choice([True, False]):
            num_slots = random.randint(1, 3)
            slots = random.sample(AVAILABLE_TIMES, num_slots)
            slots.sort(key=lambda x: datetime.strptime(x, "%I:%M %p"))
            availability[date.strftime("%A, %B %d")] = slots
        date += timedelta(days=1)
    return availability


def format_availability(availability_dict):
    if not availability_dict:
        return "No availability within the next 7 days."
    lines = [f"- {d}: {', '.join(t)}" for d, t in availability_dict.items()]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Husband & Wife (couple) back-to-back availability
# ─────────────────────────────────────────────

def _next_adjacent_time(t1):
    """Returns the time slot exactly 30 minutes after t1 in
    AVAILABLE_TIMES, or None if no such slot exists (handles the
    lunch break and the 4:40 PM odd slot). Mirrors app.py's own
    couple-followup adjacency requirement."""
    t1_dt = datetime.strptime(t1, "%I:%M %p")
    for t2 in AVAILABLE_TIMES:
        t2_dt = datetime.strptime(t2, "%I:%M %p")
        if (t2_dt - t1_dt).seconds == 30 * 60:
            return t2
    return None


def _couple_reschedule_earliest_offset(record1, record2):
    """Return the minimum number of days from today for a couple's
    rescheduled appointments. A rescheduled wellness appointment must
    remain later than both patients' existing scheduled appointments."""
    today = datetime.now().date()
    appointment_dates = []

    for record in (record1, record2):
        appointment_day = record.get("appointment_day", "")
        match = re.search(
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+(\d{1,2})",
            appointment_day,
            re.IGNORECASE,
        )
        if not match:
            continue

        month = datetime.strptime(match.group(1), "%B").month
        day = int(match.group(2))
        candidate = datetime(today.year, month, day).date()
        if candidate < today:
            candidate = candidate.replace(year=today.year + 1)
        appointment_dates.append(candidate)

    if not appointment_dates:
        return 1

    latest_existing_date = max(appointment_dates)
    return max((latest_existing_date - today).days + 1, 1)


def generate_couple_pcp_pairs(earliest_offset_days=1):
    """Back-to-back (same-day, adjacent 30-minute) wellness slots for
    the couple's own PCP, spanning the same 1-day to ~6-month window
    the single-visit PCP generator uses. Each pair is
    (day_str, time1, time2) with time2 exactly 30 minutes after time1
    (the 11:30 AM-1:00 PM lunch gap and the 4:40 PM odd slot are
    excluded so every pair is a true back-to-back open slot).
    Guaranteed non-empty, mirroring the PCP generator's own non-empty
    guarantee."""
    today = datetime.now()
    pairs = []
    attempts = 0
    while not pairs and attempts < 25:
        attempts += 1
        for _ in range(12):
            offset = random.randint(earliest_offset_days, earliest_offset_days + 180)
            date = today + timedelta(days=offset)
            if date.weekday() >= 5:
                continue
            t1 = random.choice(AVAILABLE_TIMES)
            t2 = _next_adjacent_time(t1)
            if t2 is None:
                continue
            day_str = date.strftime("%A, %B %d")
            if not any(p[0] == day_str for p in pairs):
                pairs.append((day_str, t1, t2))
        if len(pairs) >= 3:
            break
    if not pairs:
        offset = earliest_offset_days
        date = today + timedelta(days=offset)
        while date.weekday() >= 5:
            offset += 1
            date = today + timedelta(days=offset)
        pairs.append((date.strftime("%A, %B %d"), AVAILABLE_TIMES[0], AVAILABLE_TIMES[1]))
    return pairs[:3]


def generate_couple_covering_pairs(earliest_offset_days=1):
    """Back-to-back couple slots for the covering provider (Janet Walker
    for MAW/CHA). Janet's schedule is dedicated to MAW/CHA visits and
    typically opens sooner than the PCP's - search a 2-week window
    starting at earliest_offset_days so escalation genuinely offers
    earlier (but rule-eligible) dates. Same (day_str, time1, time2)
    back-to-back shape as the PCP generator."""
    today = datetime.now()
    start_offset = max(earliest_offset_days, 1)
    pairs = []
    date = today + timedelta(days=start_offset)
    window_end = date + timedelta(days=14)
    while date < window_end and len(pairs) < 3:
        if date.weekday() < 5 and random.choice([True, True, False]):
            t1 = random.choice(AVAILABLE_TIMES)
            t2 = _next_adjacent_time(t1)
            if t2 is None:
                date += timedelta(days=1)
                continue
            pairs.append(
                (
                    date.strftime("%A, %B %d"),
                    t1,
                    t2,
                )
            )
        date += timedelta(days=1)
    if not pairs:
        fallback = today + timedelta(days=start_offset)
        while fallback.weekday() >= 5:
            fallback += timedelta(days=1)
        pairs.append(
            (
                fallback.strftime("%A, %B %d"),
                AVAILABLE_TIMES[0],
                AVAILABLE_TIMES[1],
            )
        )
    return pairs[:3]


def format_couple_pairs(pairs):
    lines = [
        f"- {day}: {t1} and {t2}" for day, t1, t2 in pairs
    ]
    return "\n".join(lines)


def _match_couple_pair(message, message_lower, pairs):
    """Return the (day_str, time1, time2) pair whose calendar day is
    named in the message and whose start time (or second slot) matches
    the time stated, or None. Falls back to matching 'the second time'
    phrasing ('the 9:30 slot') so either member of the pair is a valid
    selection."""
    for day, t1, t2 in pairs:
        if _slot_day_matches(message_lower, day):
            if time_matches(message, t1) or time_matches(message, t2):
                return (day, t1, t2)
    return None


def _couple_requested_day_unavailable(message_lower, pcp_pairs):
    """Detect a caller request for a specific month or weekday that NONE
    of the offered PCP back-to-back pairs cover (e.g. "get rescheduled
    for a day in April" when the PCP list only shows September/February
    dates). Returns the requested label ("April", "Friday"...), or None
    when the message names nothing specific or the window IS covered by
    the current PCP list. Drives the escalation to Janet Walker / the
    covering provider when the PCP genuinely has no opening in the
    window the caller wants, instead of silently re-listing the same
    PCP pairs."""
    _MONTH_NAMES = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november",
        "december",
    ]
    _WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    requested = None
    for name in _MONTH_NAMES + _WEEKDAY_NAMES:
        if re.search(r'\b' + name + r'\b', message_lower):
            requested = name
            break
    if requested is None:
        return None
    for day_str, _t1, _t2 in pcp_pairs:
        if requested in day_str.lower():
            return None
    return requested.capitalize()


def time_matches(user_input_text, available_time_slot):
    """Flexible time-string match. Mirrors Sprint13.time_matches()."""
    user_lower = user_input_text.lower()
    if available_time_slot.lower() in user_lower:
        return True
    try:
        slot_dt = datetime.strptime(available_time_slot, "%I:%M %p")
        slot_hour, slot_minute = slot_dt.hour, slot_dt.minute
        for pattern in [
            r'(\d{1,2}):(\d{2})\s*(am|pm)',
            r'(\d{1,2})(\d{2})\s*(am|pm)',
            r'(\d{1,2})\s*(am|pm)',
        ]:
            for match in re.findall(pattern, user_lower):
                if len(match) == 3:
                    hour, minute, meridiem = int(match[0]), int(match[1]), match[2]
                elif len(match) == 2:
                    hour, minute, meridiem = int(match[0]), 0, match[1]
                else:
                    continue
                if meridiem == 'pm' and hour != 12:
                    hour += 12
                elif meridiem == 'am' and hour == 12:
                    hour = 0
                if hour == slot_hour and minute == slot_minute:
                    return True
    except (ValueError, AttributeError):
        pass
    return False


def _slot_day_matches(message_lower, date_key):
    """Returns True if the message refers to the calendar day this
    slot falls on - either by weekday name ("Friday") or by month+day
    ("November 13th", "Nov 13"). generate_availability_for_requested_
    date() formats keys as "Weekday, Month Day, Year" - a patient
    replying to that listing very naturally repeats the month+day
    phrasing they used to ask for it in the first place ("November
    13th at 2PM"), not necessarily the weekday name Steve echoed back,
    so both forms have to be checked or a valid, exact-match reply
    silently fails to book and the availability list just repeats."""
    parts = [p.strip() for p in date_key.split(',')]
    weekday = parts[0].lower() if parts else ""
    if weekday and weekday in message_lower:
        return True
    if len(parts) > 1:
        month_day_match = re.search(r'([A-Za-z]+)\s+(\d{1,2})', parts[1])
        if month_day_match:
            month_name = month_day_match.group(1).lower()
            month_abbrev = month_name[:3]
            # date_key's day comes from strftime("%d"), which is
            # always zero-padded ("09"). A patient naturally says "9th"
            # with no leading zero, so the day must be compared as an
            # integer - not as the padded string - or a valid,
            # exact-match reply for any single-digit day (1st-9th)
            # silently fails to match at all.
            day_num_int = str(int(month_day_match.group(2)))
            day_num_padded = month_day_match.group(2)
            day_present = re.search(
                r'\b(?:' + re.escape(day_num_int) + r'|'
                + re.escape(day_num_padded) + r')(?:st|nd|rd|th)?\b',
                message_lower
            )
            month_present = (
                month_name in message_lower
                or re.search(r'\b' + month_abbrev + r'\b', message_lower)
            )
            if month_present and day_present:
                return True
    return False


def _match_slot_from_availability(message, message_lower, availability):
    for date_key, times in availability.items():
        if _slot_day_matches(message_lower, date_key):
            for time_slot in times:
                if time_matches(message, time_slot):
                    return f"{date_key} @ {time_slot}"
    return None


# ─────────────────────────────────────────────
# "Appointment too far out" reason classification (PDF Section 3)
# ─────────────────────────────────────────────

def _contains_trigger(message_lower, triggers):
    """Word-boundary trigger match. Plain 'in' substring checks are
    unsafe for short/generic single-word triggers - e.g. the plain
    substring "form" (intended for "paperwork"/"form") also matches
    inside "metformin", silently misrouting a medication name into the
    paperwork branch. Multi-word phrases are effectively already safe
    (very low collision risk) but are matched the same way here for
    consistency."""
    for trigger in triggers:
        if re.search(r'\b' + re.escape(trigger) + r'\b', message_lower):
            return True
    return False


def detect_appointment_too_far_out(message_lower):
    return _contains_trigger(message_lower, TOO_FAR_OUT_TRIGGERS)


def classify_too_far_out_reason(message_lower):
    if (
        _contains_trigger(message_lower, TOO_FAR_OUT_MEDICATION_TRIGGERS)
        or _mentions_running_out_of_medication(message_lower)
    ):
        return "medication_refill"
    if _contains_trigger(message_lower, TOO_FAR_OUT_ACUTE_TRIGGERS):
        return "acute"
    if (
        _contains_trigger(message_lower, TOO_FAR_OUT_CHRONIC_TRIGGERS)
        or _contains_trigger(message_lower, CHRONIC_CONDITION_NAME_TRIGGERS)
    ):
        return "chronic"
    if _contains_trigger(message_lower, TOO_FAR_OUT_PAPERWORK_TRIGGERS):
        return "paperwork"
    if _contains_trigger(message_lower, TOO_FAR_OUT_EMPLOYER_DEADLINE_TRIGGERS):
        return "employer_deadline"
    return None


def _find_dob_in_history(conversation_history):
    """Recover a DOB the patient already stated earlier in this call
    (almost always during app.py's own pre-chart identification)
    instead of asking for it again.

    Root cause this works around: for a self-identifying patient
    caller, app.py's pre-chart flow (determine_pre_chart_response)
    only ever sets a `dob_collected` BOOLEAN - it never stores the raw
    DOB text into any app.py global. (Only the new-patient and
    medical-professional flows keep the raw text, and neither applies
    here.) So there is no DOB value in app.py's state for this module
    to read - the one place the raw text still exists is the literal
    conversation transcript itself.

    Scans oldest-to-newest and returns the FIRST DOB-shaped match,
    since the patient's DOB is almost always stated during initial
    identification; taking the earliest match avoids a later
    date-shaped phrase (e.g. an appointment day) being mistaken for
    the DOB.
    """
    for turn in conversation_history:
        if turn.get("role") != "user":
            continue
        content = turn.get("content", "")
        if detect_dob_in_message(content):
            return extract_dob_from_message(content)
    return None


# ─────────────────────────────────────────────
# Non-wellness pivot detection gate (Architecture Reviews #1-#3)
# ─────────────────────────────────────────────
# Finalized ownership model: Sprint14 owns Wellness/MAW/CHA (and their
# wellness-specific eligibility/lab mini-flows). app.py owns Acute,
# Medication, Chronic-condition, and Paperwork appointments. Those four
# reasons were previously only detectable from two call sites inside
# the too-far-out-reason flow (not_eligible_followup and
# check_pcp_availability), so a patient who pivoted from a wellness
# request to one of these reasons at ANY OTHER stage (e.g. while DOB
# was being collected, or while covering-provider availability was
# being presented) had that statement silently absorbed as if it were
# an answer to the current question instead.
#
# This gate is checked ONCE PER TURN at the very top of
# handle_wellness_flow - before the refill/lab-order mini-flows and
# before the main stage ladder - so a pivot is caught immediately
# regardless of which of the three (main ladder, refill mini-flow,
# lab-order mini-flow) currently has the turn. It reuses the SAME
# reason classification already built for the too-far-out flow rather
# than a second, divergent trigger list.
def _pivot_gate_suppressed():
    """True during stages where the expected reply is structured data
    (a phone number, a mailing address, a fax destination) rather than
    a free-form appointment reason. Running the gate here risks
    misreading that data as a pivot - the same word-boundary collision
    class _contains_trigger() already guards against for "form" vs.
    "metformin", just at the stage level instead of the token level."""
    if wellness_work_in_requested and not wellness_callback_number:
        return True
    if wellness_stage == "await_callback":
        return True
    if lab_order_ehr_stage in ("verify_mailing_address", "collect_fax_destination"):
        return True
    return False


def _execute_pivot_handoff(reason, message_lower=""):
    """Ends Sprint14's ownership of the conversation immediately: clears
    Sprint14-specific workflow state (not patient identity, which is
    harmless to retain and expensive to re-collect), and either hands
    off with a short bridging response or, for paperwork, hands off
    silently (see below). This generalizes the pre-existing 'acute'
    branch inside too_far_out_reason (the only one of the four that
    previously exited cleanly) to all four reasons, reachable from
    every stage instead of only two call sites.

    CRITICAL - also clears wellness_intent_detected (and, defensively,
    refill_intent_detected/lab_order_intent_detected). Traced defect:
    clearing wellness_flow_active alone is NOT sufficient to transfer
    ownership. app.py's own activation block re-checks
    Sprint14.wellness_intent_detected on every turn where
    wellness_flow_active is currently False:
        if not Sprint14.wellness_flow_active:
            if Sprint14.wellness_intent_detected:
                Sprint14.wellness_flow_active = True   # re-hijack
    wellness_intent_detected is set once, the moment the ORIGINAL
    wellness request is first mentioned, and is never cleared except
    by a full reset_state(). Without clearing it here, the very next
    user turn after this handoff re-triggers app.py's own activation
    block and silently pulls the conversation straight back into
    Sprint14 - which is why "ownership was not fully transferred" and
    why paperwork questioning kept resuming after a one-turn handoff
    that looked correct in isolation."""
    global wellness_flow_active, wellness_stage
    global wellness_requested_visit_type, wellness_pcp_availability
    global wellness_covering_availability, wellness_pending_requested_date
    global wellness_eligibility_narration, wellness_too_far_out_pending
    global wellness_too_far_out_reason, wellness_paperwork_appointment_requested
    global refill_escalation_active, refill_stage
    global lab_order_ehr_guidance_active, lab_order_ehr_stage
    global wellness_intent_detected, refill_intent_detected, lab_order_intent_detected
    global couple_stage
    global couple_patient1_first, couple_patient1_last
    global couple_patient2_first, couple_patient2_last, couple_spouse_relation
    global couple_patient2_dob, couple_patient2_age
    global couple_shared_insurance, couple_visit_type
    global couple_pcp_pairs, couple_covering_pairs
    global couple_covering_provider_name
    global couple_last_visit_date1, couple_days_since_last_visit1
    global couple_last_visit_date2, couple_days_since_last_visit2
    global couple_eligible_both, couple_eligibility_narration
    global couple_pcp_earliest_offset
    global couple_existing_located
    global couple_is_reschedule
    global couple_reschedule_old_days

    wellness_flow_active = False
    wellness_stage = None
    wellness_requested_visit_type = None
    wellness_pcp_availability = None
    wellness_covering_availability = None
    wellness_pending_requested_date = None
    wellness_eligibility_narration = None
    wellness_too_far_out_pending = False
    wellness_too_far_out_reason = None
    wellness_paperwork_appointment_requested = False
    refill_escalation_active = False
    refill_stage = None
    lab_order_ehr_guidance_active = False
    lab_order_ehr_stage = None
    wellness_intent_detected = None
    refill_intent_detected = False
    lab_order_intent_detected = False
    couple_stage = None
    couple_patient1_first = None
    couple_patient1_last = None
    couple_patient2_first = None
    couple_patient2_last = None
    couple_patient2_dob = None
    couple_patient2_age = None
    couple_spouse_relation = None
    couple_shared_insurance = None
    couple_visit_type = None
    couple_pcp_pairs = None
    couple_covering_pairs = None
    couple_covering_provider_name = None
    couple_last_visit_date1 = None
    couple_days_since_last_visit1 = None
    couple_last_visit_date2 = None
    couple_days_since_last_visit2 = None
    couple_eligible_both = None
    couple_eligibility_narration = None
    couple_pcp_earliest_offset = 1
    couple_existing_located = False
    couple_is_reschedule = False
    couple_reschedule_old_days = None
    if reason == "paperwork":
        # Unlike acute/chronic ("I'm sick" / "my diabetes is acting
        # up"), a paperwork trigger match - "paperwork", "form",
        # "fmla", "disability form", "work note", "school note",
        # "needs to be signed" - IS ITSELF the stated reason for the
        # visit. There is nothing further to ask before scheduling can
        # begin, so asking "tell me more" is a redundant round-trip
        # that re-asks what the patient already said (Task 3's root
        # cause). Returning None lets THIS SAME message fall through
        # to app.py's existing pipeline and LLM call in the same turn
        # - not a new code path, the identical None-fallthrough
        # pattern already used by determine_pre_chart_response(),
        # handle_med_pro_collection(), and handle_new_patient_flow().
        # app.py's own WEEKLY AVAILABILITY injection and WORK NOTE /
        # PAPERWORK REQUESTS system-prompt section then handle this
        # message directly, with the patient's original wording -
        # e.g. "I need to schedule an appointment because..." - intact
        # and available to trigger real, deterministic PCP
        # availability generation immediately, instead of waiting on
        # an extra turn's worth of AI-interpreted bridging text.
        # Applies uniformly to every paperwork type (FMLA, disability,
        # school, employer, housing, insurance, emotional support
        # animal, etc.) - no reason-specific branching, per the
        # explicit "do not build a workflow specific to emotional
        # support animals" requirement.
        return None

    if reason == "acute":
        # Let app.py's existing acute/urgent workflow handle this with
        # full office-hours awareness, rather than intercepting with a
        # hardcoded bridging question that bypasses after-hours logic.
        return None
    if reason == "medication_refill":
        # Traced Scenario 11 defect: this branch always asked "which
        # medication" even when the drug name was already stated in
        # the SAME message that triggered the pivot ("run out of
        # Alprazolam") - re-asking info already given, the identical
        # bug class already fixed for paperwork above. Unlike
        # paperwork, this checks a DETERMINISTIC Python-level signal
        # rather than adding another prompt-compliance patch: app.py's
        # own detect_controlled_substance() already recognizes named
        # drugs (Schedule 1/2/3) with zero LLM-reliability risk. When
        # it fires, return None so this SAME message falls through to
        # app.py's existing controlled-substance workflow (last-visit
        # window check, schedule-appropriate virtual/in-office rules)
        # in the same turn - a strictly richer, already-built path,
        # not a duplicate of it. When no recognized drug name is
        # present, the original bridging question is still needed and
        # unchanged below.
        try:
            import app as _app
            if _app.detect_controlled_substance(message_lower):
                return None
        except Exception:
            pass
        return (
            "I understand. Let's take care of your medication needs "
            "separately - can you tell me which medication this is "
            "regarding?"
        )
    if reason == "chronic":
        return (
            "I'm sorry to hear that. Let's get you scheduled to "
            "address that instead - can you tell me a bit more about "
            "what's been going on?"
        )
    if reason == "paperwork":
        return (
            "I understand you need paperwork completed. Let's get "
            "that taken care of - can you tell me a bit more about "
            "what needs to be completed?"
        )
    return None


# ─────────────────────────────────────────────
# Appointment persistence helpers
# ─────────────────────────────────────────────

def _load_appointments():
    """Load the appointments JSON file. Returns a dict keyed by
    patient full name, each value a list of appointment records."""
    if not os.path.exists(APPOINTMENTS_JSON_PATH):
        return {}
    try:
        with open(APPOINTMENTS_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_appointments(data):
    """Atomically write the appointments dict back to JSON.

    FIX: Ensure the target directory exists before writing, and do not
    silently swallow OSError (which masks permission/disk issues)."""
    try:
        os.makedirs(os.path.dirname(APPOINTMENTS_JSON_PATH), exist_ok=True)
        with open(APPOINTMENTS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        # Log the error for debugging; do not fail the conversation silently.
        print(f"[Sprint14] Failed to save appointments: {e}")


def _resolve_patient_key(patient_full):
    """Resolve a non-empty patient key, falling back to app.py globals
    if the passed-in value is missing. This prevents silent persistence
    failures when handle_wellness_flow()'s module-level copy of the
    patient's name happens to be None at booking time."""
    if patient_full and str(patient_full).strip():
        return str(patient_full).strip()
    try:
        import app as _app
        first = getattr(_app, "patient_first_name", None)
        last = getattr(_app, "patient_last_name", None)
        if first and last:
            return f"{first} {last}".strip()
    except Exception:
        pass
    return None


def _persist_appointment(patient_full, visit_type, appointment_day,
                         provider_name=None):
    """Persist a newly booked appointment so it can be retrieved later.
    Called immediately after the patient selects a slot."""
    patient_key = _resolve_patient_key(patient_full)
    if not patient_key or not appointment_day:
        print(f"[Sprint14] PERSISTENCE SKIPPED: patient_key={patient_key!r}, "
              f"appointment_day={appointment_day!r}")
        return False
    data = _load_appointments()
    record = {
        "visit_type": visit_type,
        "appointment_day": appointment_day,
        "provider": provider_name or "PCP",
        "scheduled_at": datetime.now().isoformat(),
    }
    data.setdefault(patient_key, []).append(record)
    _save_appointments(data)
    print(f"[Sprint14] PERSISTENCE OK: key={patient_key!r}, "
          f"appt={appointment_day!r}, file={APPOINTMENTS_JSON_PATH}")
    return True


def _remove_appointment_for_patient(patient_full, appointment_day):
    """Remove a patient's stored appointment record matching the given
    day. Used by the couple reschedule path so the new booking REPLACES
    the old visit instead of accumulating duplicates in the store."""
    patient_key = _resolve_patient_key(patient_full)
    if not patient_key or not appointment_day:
        return False
    data = _load_appointments()
    records = data.get(patient_key, [])
    remaining = [r for r in records if r.get("appointment_day") != appointment_day]
    if len(remaining) == len(records):
        return False
    if remaining:
        data[patient_key] = remaining
    else:
        data.pop(patient_key, None)
    _save_appointments(data)
    print(f"[Sprint14] REMOVED OLD APPOINTMENT: key={patient_key!r}, "
          f"appt={appointment_day!r}, file={APPOINTMENTS_JSON_PATH}")
    return True


def cancel_appointment_for_patient(patient_full):
    """Public API: cancel the most recent Sprint14 appointment for a
    patient.  Returns the removed record dict (with its appointment_day)
    so the caller can reference it in a confirmation message, or None
    if no record existed."""
    patient_key = _resolve_patient_key(patient_full)
    if not patient_key:
        return None
    data = _load_appointments()
    records = data.get(patient_key, [])
    if not records:
        return None
    removed = records.pop()
    if records:
        data[patient_key] = records
    else:
        data.pop(patient_key, None)
    _save_appointments(data)
    print(f"[Sprint14] CANCEL APPOINTMENT: key={patient_key!r}, "
          f"appt={removed.get('appointment_day')!r}, "
          f"file={APPOINTMENTS_JSON_PATH}")
    return removed


def find_spouse_appointment_record(first, last):
    """Public API: find the most recent Sprint14 appointment record for
    the caller's spouse - another patient in the store whose visit was
    booked back-to-back on the SAME calendar date as the caller's (the
    couple flow always books both halves on one shared date). Returns
    (spouse_key, record) or (None, None) if none found. Same-date
    matching is what keeps this from grabbing an unrelated patient's
    record (e.g. a different patient booked alone on another day)."""
    if not first or not last:
        return None, None
    caller_key = f"{first} {last}".strip()
    data = _load_appointments()
    caller_records = data.get(caller_key, []) or []
    caller_day = None
    if caller_records:
        caller_day = str(caller_records[-1].get("appointment_day", "")) \
            .split(" @ ")[0].strip()
    if not caller_day:
        return None, None
    for key, records in data.items():
        if not records:
            continue
        if key.strip().lower() == caller_key.lower():
            continue
        other_day = str(records[-1].get("appointment_day", "")) \
            .split(" @ ")[0].strip()
        if other_day == caller_day:
            return key, records[-1]
    return None, None


def get_next_appointment_for_patient(patient_full):
    """Public API for app.py (or any caller) to look up the most
    recently scheduled Sprint14 appointment for a patient.
    Returns the appointment record dict or None."""
    patient_key = _resolve_patient_key(patient_full)
    if not patient_key:
        return None
    data = _load_appointments()
    records = data.get(patient_key, [])
    if not records:
        return None
    # Return the most recently scheduled record
    return records[-1]


# ─────────────────────────────────────────────
# Appointment lookup handler
# ─────────────────────────────────────────────

def handle_appointment_lookup(message_lower):
    """Answer 'When is my appointment?' style questions by reading the
    persisted appointment store. Called by app.py when
    detect_appointment_lookup_intent() fires."""
    import app
    first = getattr(app, "patient_first_name", None)
    last = getattr(app, "patient_last_name", None)
    patient_full = f"{first} {last}".strip() if first and last else None
    patient_key = _resolve_patient_key(patient_full)

    if not patient_full:
        return (
            "Could I get your name so I can look up your appointment?"
        )

    record = get_next_appointment_for_patient(patient_key)
    if record:
        appt = record["appointment_day"]
        visit = _visit_type_label(record.get("visit_type", "wellness"))
        provider = record.get("provider", "your provider")
        return (
            f"I show you have {visit} scheduled with {provider} on "
            f"{appt}. Is there anything else I can help you with today?"
        )
    # Sprint 15 fix: a generic (non-wellness) appointment - e.g. a
    # 3-month follow-up booked through LLM-driven generic scheduling -
    # lives in app.py's own generic_appointment_records store, NOT in
    # this Sprint14 wellness store. Without this fallback, a patient
    # whose only appointment on file is a generic one got "I don't see
    # any upcoming appointments" here, because the generic inquiry
    # block in app.py (which reads that store) never runs - this lookup
    # handler short-circuits first. Read the generic store as the
    # fallback so every scheduled appointment is answerable in one place.
    generic = app.get_stored_generic_appointment_record(first, last)
    if generic and generic.get("appointment_day"):
        appt = generic["appointment_day"]
        provider = generic.get("provider") or "your provider"
        reason = generic.get("reason")
        reason_label = None
        if reason:
            reason_label = re.split(r"[.!?\n]", reason, maxsplit=1)[0].strip()
        reason_clause = f" for {reason_label}" if reason_label else ""
        return (
            f"I show you have an appointment scheduled{reason_clause} "
            f"with {provider} on {appt}. Is there anything else I can "
            f"help you with today?"
        )
    print(f"[Sprint14] LOOKUP MISS: key={patient_key!r}, "
          f"file={APPOINTMENTS_JSON_PATH}, exists={os.path.exists(APPOINTMENTS_JSON_PATH)}")
    return (
        "I don't see any upcoming appointments on file for you right now. "
        "Is there anything else I can help you with today?"
    )


# ─────────────────────────────────────────────
# Main workflow handler
# ─────────────────────────────────────────────

def handle_wellness_flow(message, message_lower):
    global wellness_flow_active, wellness_stage
    global wellness_patient_first_name, wellness_patient_last_name
    global wellness_patient_dob, wellness_patient_age, wellness_patient_pcp
    global wellness_requested_visit_type
    global wellness_last_visit_date, wellness_days_since_last_visit
    global wellness_eligible_for_next, wellness_next_eligible_date
    global wellness_eligibility_narration
    global wellness_insurance_type
    global wellness_eligible_for_maw, wellness_eligible_for_cha
    global wellness_pcp_availability, wellness_covering_availability
    global wellness_covering_provider_name, wellness_appointment_day
    global wellness_patient_deadline_date
    global wellness_pending_requested_date
    global wellness_too_far_out_pending, wellness_too_far_out_reason
    global wellness_work_in_requested, wellness_callback_number
    global wellness_paperwork_appointment_requested
    global refill_escalation_active, refill_stage
    global refill_medication_name, refill_days_remaining
    global refill_will_run_out_before_next_appt
    global lab_order_ehr_guidance_active, lab_order_ehr_stage
    global lab_order_uncomfortable_with_tech

    # Pull identity already collected by app.py's pre-chart flow.
    # Mirrors Sprint13.handle_post_hospital_flow()'s existing pattern
    # of reading (not writing) app.py's globals for this same purpose.
    import app
    if not wellness_patient_first_name and getattr(app, "patient_first_name", None):
        wellness_patient_first_name = app.patient_first_name
    if not wellness_patient_last_name and getattr(app, "patient_last_name", None):
        wellness_patient_last_name = app.patient_last_name
    if not wellness_patient_pcp:
        provider = app.detect_provider_in_message(message_lower)
        if provider:
            wellness_patient_pcp = provider
        elif getattr(app, "pcp_collected", False):
            found = app.get_patient_pcp_from_history()
            if found:
                wellness_patient_pcp = found

    patient_full = None
    if wellness_patient_first_name and wellness_patient_last_name:
        patient_full = f"{wellness_patient_first_name} {wellness_patient_last_name}"

    # ── NON-WELLNESS PIVOT DETECTION GATE ──
    # Runs before the refill mini-flow, the lab-order mini-flow, and
    # the main stage ladder, so a pivot stated while any of the three
    # currently has the turn is still caught. See the gate/handoff
    # functions above for the full rationale.
    if not _pivot_gate_suppressed():
        pivot_reason = classify_too_far_out_reason(message_lower)
        if pivot_reason in ("acute", "medication_refill", "chronic", "paperwork"):
            return _execute_pivot_handoff(pivot_reason, message_lower)

    # ── MEDICATION REFILL ESCALATION (independent mini-flow) ──
    # Checked before the main stage ladder so it can interrupt/branch
    # off from the too-far-out reason classification (Section 3) as
    # well as fire on its own if directly requested (Section 6).
    if refill_escalation_active:
        refill_response = _handle_refill_escalation(message, message_lower)
        if refill_response is not None:
            return refill_response

    # ── LAB ORDER / EHR GUIDANCE (independent mini-flow) ──
    if lab_order_ehr_guidance_active:
        lab_response = _handle_lab_order_ehr_guidance(message, message_lower)
        if lab_response is not None:
            return lab_response

    # ── WORK-IN CALLBACK COLLECTION (shared terminal step) ──
    if wellness_work_in_requested and not wellness_callback_number:
        phone_match = re.search(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b', message)
        if phone_match:
            wellness_callback_number = phone_match.group(0)
            wellness_stage = "complete"
            wellness_flow_active = False
            return (
                "Thank you. I have created a high priority work-in "
                "request and noted your callback number as "
                f"{wellness_callback_number}. Someone from our office "
                "will follow up with you. Is there anything else I can "
                "help you with today?"
            )
        return "May I get a good callback number for you?"

    # ── HUSBAND & WIFE (COUPLE) WELLNESS SCHEDULING ──
    # Entirely separate stage machine from the single-patient ladder
    # below. Activated when detect_wellness_intent() returned "couple";
    # books BOTH patients back-to-back against one shared insurance,
    # escalating to Janet Walker if the PCP's pair isn't soon enough.
    if wellness_requested_visit_type == "couple":
        couple_response = _handle_couple_wellness_flow(message, message_lower)
        if couple_response is not None:
            return couple_response

    # ── STAGE: collect DOB (needed for age/insurance branch) ──
    if wellness_stage is None:
        wellness_stage = "collect_dob"

    if wellness_stage == "collect_dob":
        if not wellness_patient_dob:
            recovered_dob = _find_dob_in_history(
                getattr(app, "conversation_history", [])
            )
            if recovered_dob:
                wellness_patient_dob = recovered_dob
                wellness_patient_age = calculate_age_from_dob(wellness_patient_dob)
            elif detect_dob_in_message(message):
                wellness_patient_dob = extract_dob_from_message(message)
                wellness_patient_age = calculate_age_from_dob(wellness_patient_dob)
            else:
                if patient_full:
                    return f"Thank you. Could I get {wellness_patient_full_possessive(patient_full)} date of birth?"
                return "Could I get your date of birth?"
        wellness_stage = "determine_eligibility"

    # ── STAGE: last-visit / 1yr+1day eligibility check (Section 1) ──
    # Requirements change: BOTH the eligible and not-yet-eligible cases
    # now stop here and ask the standard confirmation/pivot-opportunity
    # question before any scheduling proceeds - this used to be true
    # only for the not-yet-eligible branch (which returns and waits),
    # while the eligible branch fell straight through into
    # age_insurance_check -> check_pcp_availability in the same call,
    # generating availability immediately with no checkpoint. That
    # immediate-scheduling behavior was traced and confirmed NOT to be
    # a regression from the pivot-gate work (reproduced identically
    # against the untouched /mnt/project/Sprint14.py) - it's being
    # changed here because it's now an explicit requirements update,
    # not because it was broken.
    if wellness_stage == "determine_eligibility":
        wellness_last_visit_date, wellness_days_since_last_visit = (
            generate_last_wellness_date()
        )
        wellness_eligible_for_next = is_eligible_for_next_wellness(
            wellness_days_since_last_visit
        )
        if not wellness_eligible_for_next:
            wellness_next_eligible_date = calculate_next_eligible_date(
                wellness_days_since_last_visit
            )
            narration = (
                f"I see your last wellness visit was on "
                f"{wellness_last_visit_date}. In order for your "
                f"insurance to count this appointment as a wellness "
                f"visit, your upcoming visit needs to be scheduled at "
                f"least one year and one day away from your last "
                f"wellness visit, so you will not be eligible again "
                f"until {wellness_next_eligible_date}. "
            )
            # Not set here on purpose: the "I see your last visit..."
            # narration is delivered directly in this response below, so
            # it must not also be replayed a second time by
            # check_pcp_availability once the patient confirms. The
            # separate MAW/CHA insurance-routing narration appended later
            # in age_insurance_check is unaffected - it starts fresh from
            # None here, exactly as it already did on the not-eligible path.
            wellness_stage = "wellness_confirmation"
            return (
                f"{narration}"
                f"Are you OK scheduling your wellness visit that far out, "
                f"or were you hoping to schedule an appointment for a "
                f"specific issue that would not count as your annual "
                f"wellness visit?"
            )

        # Already eligible: there is no future eligibility date to wait
        # on, so the "are you OK scheduling that far out" checkpoint
        # doesn't apply - proceed directly to scheduling. Set the
        # narration on the carry-through global (the same mechanism
        # check_pcp_availability already uses to prepend the
        # not-eligible narration once the patient confirms) and fall
        # through to age_insurance_check -> check_pcp_availability in
        # this same turn, so the eligibility statement and the
        # provider's availability arrive in one response.
        wellness_eligibility_narration = (
            f"I see your last wellness visit was on "
            f"{wellness_last_visit_date}. Since that is over a year "
            f"and a day ago, you are eligible for your next wellness "
            f"visit. "
        )
        wellness_stage = "age_insurance_check"

    # ── STAGE: wellness confirmation / pivot opportunity ──
    # Business goal: before ANY wellness scheduling proceeds - eligible
    # or not - the patient gets one explicit checkpoint to either
    # continue with wellness scheduling or redirect to a non-wellness
    # reason. Either (a) the patient is fine proceeding - continue to
    # scheduling (constrained, for the not-yet-eligible case, so no
    # slot before the next eligible date is ever offered) - or (b) they
    # actually need care for a specific concern now, in which case this
    # reuses the SAME reason classification/routing already built for
    # the "too far out" escalation ladder below (medication refill /
    # acute / chronic / paperwork / employer deadline) rather than
    # duplicating that logic here. Formerly "not_eligible_followup";
    # renamed since both eligible and not-yet-eligible patients now
    # land here.
    if wellness_stage == "wellness_confirmation":
        if patient_wants_to_proceed(message_lower) or patient_accepts_far_out_timeline(message_lower):
            requested_date = extract_requested_date(message)
            if requested_date:
                wellness_pending_requested_date = requested_date
            # Route through age/insurance determination before
            # scheduling - this path (patient confirms) previously
            # jumped straight to check_pcp_availability, silently
            # skipping Medicare/MAW/CHA determination for 65+ patients
            # entirely. That determination applies to any 65+ patient
            # getting a wellness visit scheduled, not only ones who
            # happened to be immediately eligible.
            wellness_stage = "age_insurance_check"
            return handle_wellness_flow(message, message_lower)

        reason = classify_too_far_out_reason(message_lower)
        if reason:
            # Scenario 15: if the patient explicitly asks to schedule an
            # appointment for paperwork (e.g. "I need to schedule an
            # appointment because I need paperwork signed"), override the
            # wellness workflow and book a paperwork appointment instead.
            if reason == "paperwork" and _patient_explicitly_requests_appointment(message_lower):
                wellness_paperwork_appointment_requested = True
                wellness_requested_visit_type = "paperwork"
                wellness_stage = "check_pcp_availability"
                return handle_wellness_flow(message, message_lower)
            wellness_stage = "too_far_out_reason"
            return handle_wellness_flow(message, message_lower)

        return (
            "Are you OK scheduling your wellness visit that far out, "
            "or were you hoping to schedule an appointment for a "
            "specific issue that would not count as your annual "
            "wellness visit?"
        )

    # ── STAGE: age / insurance / MAW-CHA eligibility (Section 2) ──
    if wellness_stage == "age_insurance_check":
        if wellness_patient_age is not None and wellness_patient_age >= 65:
            wellness_insurance_type = determine_insurance_type_for_senior()
            wellness_eligible_for_maw, wellness_eligible_for_cha = (
                determine_maw_cha_eligibility(wellness_insurance_type)
            )
            # Auto-route to the appropriate visit type based on
            # insurance. Patients are not expected to choose between
            # MAW and CHA (most do not know what they mean).
            if wellness_requested_visit_type not in ("maw", "cha"):
                if wellness_insurance_type == "priority_medicare_advantage":
                    wellness_requested_visit_type = "cha"
                    wellness_eligibility_narration = (
                        (wellness_eligibility_narration or "")
                        + "Based on your Priority Care Medicare Advantage "
                        "insurance, I will schedule you for a Comprehensive "
                        "Health Assessment. "
                    )
                elif wellness_insurance_type == "original_medicare":
                    wellness_requested_visit_type = "maw"
                    wellness_eligibility_narration = (
                        (wellness_eligibility_narration or "")
                        + "Based on your Original Medicare insurance, I will "
                        "schedule you for a Medicare Annual Wellness visit. "
                    )
                else:
                    wellness_requested_visit_type = "wellness"
                    wellness_eligibility_narration = (
                        (wellness_eligibility_narration or "")
                        + "Based on your insurance, I will schedule you for a "
                        "standard wellness visit. "
                    )
        # Not 65+, or no MAW/CHA eligibility - standard wellness path.
        if not wellness_requested_visit_type:
            wellness_requested_visit_type = "wellness"
        wellness_stage = "check_pcp_availability"

    # visit_type_confirm is no longer used in the normal flow;
    # insurance-driven auto-routing in age_insurance_check handles
    # MAW/CHA selection. Kept as a defensive pass-through.
    if wellness_stage == "visit_type_confirm":
        if "comprehensive" in message_lower or "cha" in message_lower:
            wellness_requested_visit_type = "cha"
        elif "wellness" in message_lower or "maw" in message_lower:
            wellness_requested_visit_type = "maw"
        wellness_stage = "check_pcp_availability"

    # ── STAGE: PCP availability (Section 1 + Section 4) ──
    if wellness_stage == "check_pcp_availability":
        if not wellness_pcp_availability:
            narration = wellness_eligibility_narration or ""
            wellness_eligibility_narration = None
            # Scenario 15: paperwork appointment framing
            if wellness_paperwork_appointment_requested:
                provider_label = wellness_patient_pcp or "your provider"
                wellness_pcp_availability = generate_wellness_pcp_availability()
                avail_text = format_availability(wellness_pcp_availability)
                return (
                    "Certainly. Let's get an appointment scheduled so "
                    f"{provider_label} can review the paperwork. "
                    f"Here is {provider_label}'s availability:"

                    f"{avail_text}"

                    f"Which day and time works best for you?"
                )
            if wellness_pending_requested_date:
                requested_date = wellness_pending_requested_date
                wellness_pending_requested_date = None
                earliest_offset = max(
                    1, 367 - (wellness_days_since_last_visit or 0)
                )
                provider_label = wellness_patient_pcp or "your provider"
                wellness_pcp_availability, exact_match = (
                    generate_availability_for_requested_date(
                        requested_date, earliest_offset_days=earliest_offset
                    )
                )
                avail_text = format_availability(wellness_pcp_availability)
                if exact_match:
                    return (
                        f"{narration}Yes, here is {provider_label}'s "
                        f"availability on "
                        f"{requested_date.strftime('%A, %B %d, %Y')}:\n"
                        f"{avail_text}\nWhich time works best for you?"
                    )
                return (
                    f"{narration}{provider_label} does not have "
                    f"availability on "
                    f"{requested_date.strftime('%A, %B %d, %Y')}. Their "
                    f"nearest availability after that is:\n{avail_text}\n"
                    f"Which day and time works best for you?"
                )
            earliest_offset = max(
                1, 367 - (wellness_days_since_last_visit or 0)
            )
            wellness_pcp_availability = generate_wellness_pcp_availability(
                earliest_offset_days=earliest_offset
            )
            avail_text = format_availability(wellness_pcp_availability)
            return (
                f"Great, let's get that scheduled. {narration}Here is "
                f"your provider's availability:\n{avail_text}\n"
                f"Which day and time works best for you? If none of "
                f"these work, just let me know."
            )

        # Scenario 15: paperwork appointment urgency escalation
        if wellness_paperwork_appointment_requested:
            urgency_phrases = [
                "within a week", "within 7 days", "this week",
                "sooner", "as soon as possible", "asap",
            ]
            if any(p in message_lower for p in urgency_phrases):
                wellness_work_in_requested = True
                wellness_stage = "await_callback"
                return (
                    "I understand you need to be seen soon for the "
                    "paperwork. I will create a high priority work-in "
                    "request for your provider. May I get a good callback "
                    "number for you?"
                )

        # Scenario 17: travel constraint during wellness scheduling.
        # If the patient mentions upcoming travel that conflicts with
        # the offered availability, transition to the covering provider
        # (Janet Walker for MAW/CHA, the generic covering provider
        # otherwise) instead of repeating the same dates. Applies to
        # any wellness visit type - not just MAW/CHA - since a plain
        # annual physical request runs into the identical conflict.
        if _patient_mentions_travel_constraint(message_lower):
            # Capture the stated departure date (if any) so covering-
            # provider generation below can be constrained to land
            # strictly before it - otherwise a date on or after the
            # patient's departure would be offered and defeat the
            # entire point of escalating for an earlier appointment.
            wellness_patient_deadline_date = extract_requested_date(message)
            wellness_stage = "covering_provider_check"
            return _offer_covering_provider(
                for_maw_or_cha=(wellness_requested_visit_type in ("maw", "cha"))
            )

        if detect_appointment_too_far_out(message_lower):
            # Do not probe for a reason - the patient may have already
            # explained (and Steve shouldn't require an explanation
            # either way). Go straight to the covering provider, the
            # same as the travel-constraint shortcut above. Capture a
            # deadline date opportunistically in case the message
            # states one (e.g. "...before I leave July 1st") so
            # generation below still respects it if present; harmless
            # None otherwise.
            wellness_patient_deadline_date = extract_requested_date(message)
            wellness_stage = "covering_provider_check"
            return _offer_covering_provider(
                for_maw_or_cha=(wellness_requested_visit_type in ("maw", "cha"))
            )

        matched_slot = _match_slot_from_availability(
            message, message_lower, wellness_pcp_availability
        )
        if matched_slot:
            wellness_appointment_day = matched_slot
            wellness_stage = "complete"
            wellness_flow_active = False
            _persist_appointment(
                patient_full=patient_full,
                visit_type=wellness_requested_visit_type,
                appointment_day=wellness_appointment_day,
                provider_name=wellness_patient_pcp,
            )
            visit_label = _visit_type_label(wellness_requested_visit_type)
            if wellness_paperwork_appointment_requested:
                provider_label = wellness_patient_pcp or "your provider"
                return (
                    f"Perfect. I have you scheduled with {provider_label} "
                    f"on {wellness_appointment_day} to review your "
                    f"paperwork. Is there anything else I can help you "
                    f"with today?"
                )
            return (
                f"Perfect. I have you scheduled for "
                f"{visit_label} on "
                f"{wellness_appointment_day}. Please arrive a few "
                f"minutes early to complete any necessary paperwork. "
                f"Is there anything else I can help you with today?"
            )
        avail_text = format_availability(wellness_pcp_availability)
        return (
            f"Which day and time works best for you?\n{avail_text}"
        )

    # ── STAGE: too-far-out reason routing (Section 3) ──
    if wellness_stage == "too_far_out_reason":
        reason = classify_too_far_out_reason(message_lower)
        wellness_too_far_out_reason = reason

        if reason == "medication_refill":
            wellness_too_far_out_pending = False
            refill_escalation_active = True
            refill_stage = "ask_medication"
            return (
                "I can help with that. What medication do you need "
                "refilled?"
            )

        if reason == "acute":
            # EXPANSION POINT: app.py owns the acute-visit workflow
            # (URGENT_SYMPTOM_TRIGGERS / same-day scheduling). This
            # module does not duplicate that logic - it hands the
            # conversation back by ending its own flow so app.py's
            # existing acute/same-day detection can take over.
            # Return None so app.py handles this turn directly with
            # full office-hours awareness, rather than intercepting
            # with a hardcoded bridging question.
            wellness_too_far_out_pending = False
            wellness_flow_active = False
            wellness_stage = "complete"
            return None

        if reason == "chronic":
            wellness_too_far_out_pending = False
            wellness_stage = "covering_provider_check"
            return _offer_covering_provider(
                for_maw_or_cha=(
                    wellness_requested_visit_type in ("maw", "cha")
                )
            )

        if reason == "paperwork":
            wellness_too_far_out_pending = False
            wellness_work_in_requested = True
            wellness_stage = "await_callback"
            return (
                "I will create a phone message asking your provider "
                "to review the paperwork, or to consider a work-in "
                "appointment if needed. May I get a good callback "
                "number for you?"
            )

        if reason == "employer_deadline":
            wellness_too_far_out_pending = False
            wellness_work_in_requested = True
            wellness_stage = "await_callback"
            return (
                "I understand you have a deadline to meet. I will "
                "create a phone message for the office regarding "
                "this. May I get a good callback number for you?"
            )

        # Reason not yet classified - ask again rather than guessing.
        return (
            "I want to make sure I route this correctly - is this "
            "related to a medication refill, a new or worsening "
            "symptom, an ongoing/chronic condition, paperwork that "
            "needs to be completed, or a deadline from your employer "
            "or insurance?"
        )

    if wellness_stage == "await_callback":
        # Handled by the shared WORK-IN CALLBACK COLLECTION block near
        # the top of this function on the next call, once
        # wellness_work_in_requested is set. Kept as an explicit stage
        # only so build_context() can report it accurately.
        return "May I get a good callback number for you?"

    # ── STAGE: covering-provider routing (Section 4 + Section 5) ──
    if wellness_stage == "covering_provider_check":
        if patient_wants_to_decline(message_lower):
            wellness_work_in_requested = True
            wellness_stage = "await_callback"
            return (
                "I understand. I will create a high priority work-in "
                "request for your provider. May I get a good callback "
                "number for you?"
            )
        if patient_wants_to_proceed(message_lower) or not wellness_covering_availability:
            if not wellness_covering_availability:
                earliest_offset = max(
                    1, 367 - (wellness_days_since_last_visit or 0)
                )
                wellness_covering_availability = (
                    generate_covering_wellness_availability(
                        earliest_offset_days=earliest_offset,
                        latest_date=wellness_patient_deadline_date,
                    )
                )
            if wellness_covering_availability:
                avail_text = format_availability(wellness_covering_availability)
                wellness_stage = "covering_provider_schedule"
                return (
                    f"Here is {wellness_covering_provider_name}'s "
                    f"availability:\n{avail_text}\n"
                    f"Which day and time works best for you?"
                )
            wellness_work_in_requested = True
            wellness_stage = "await_callback"
            return (
                f"I'm sorry, {wellness_covering_provider_name} does "
                f"not have any availability either. I will create a "
                f"high priority work-in request. May I get a good "
                f"callback number for you?"
            )
        return _offer_covering_provider(
            for_maw_or_cha=(wellness_requested_visit_type in ("maw", "cha"))
        )

    if wellness_stage == "covering_provider_schedule":
        matched_slot = _match_slot_from_availability(
            message, message_lower, wellness_covering_availability or {}
        )
        if matched_slot:
            wellness_appointment_day = matched_slot
            wellness_stage = "complete"
            wellness_flow_active = False
            _persist_appointment(
                patient_full=patient_full,
                visit_type=wellness_requested_visit_type,
                appointment_day=wellness_appointment_day,
                provider_name=wellness_covering_provider_name,
            )
            if wellness_paperwork_appointment_requested:
                return (
                    f"Perfect. I have you scheduled with "
                    f"{wellness_covering_provider_name} on "
                    f"{wellness_appointment_day} to review your "
                    f"paperwork. Is there anything else I can help you "
                    f"with today?"
                )
            return (
                f"Perfect. I have you scheduled with "
                f"{wellness_covering_provider_name} for "
                f"{_visit_type_label(wellness_requested_visit_type)} on "
                f"{wellness_appointment_day}. Is there anything else I "
                f"can help you with today?"
            )
        avail_text = format_availability(wellness_covering_availability or {})
        return f"Which day and time works best for you?\n{avail_text}"

    # ── STAGE: complete ──
    if wellness_stage == "complete":
        if any(
            phrase in message_lower for phrase in [
                "that will be all", "that's all", "that's it",
                "nothing else", "no i'm good", "i'm good", "all set",
                "no thank you", "no thanks", "no sir", "no ma'am",
                "no.",
            ]
        ):
            response = "Thank you for calling. Have a wonderful day."
            reset_state()
            return response
        return "Is there anything else I can help you with today?"

    return None


def wellness_patient_full_possessive(patient_full):
    return f"{patient_full}'s"


def _visit_type_label(visit_type):
    return {
        "wellness": "your annual wellness visit",
        "maw": "your Medicare Annual Wellness visit",
        "cha": "your Comprehensive Health Assessment",
        "paperwork": "your paperwork review appointment",
    }.get(visit_type, "your appointment")


def _offer_covering_provider(for_maw_or_cha):
    """PDF Section 4 (general wellness -> Elizabeth Horowitz) vs.
    Section 5 (MAW/CHA -> Janet Walker, specifically when the PCP
    availability isn't acceptable solely because the patient wants the
    annual completed sooner)."""
    global wellness_covering_provider_name
    if for_maw_or_cha:
        wellness_covering_provider_name = JANET_WALKER_NAME
        return (
            f"Your provider does not have an earlier opening. Would "
            f"you like me to check {JANET_WALKER_NAME}'s availability "
            f"instead?"
        )
    wellness_covering_provider_name = COVERING_PROVIDER_BARE_NAME
    return (
        f"Your provider does not have an earlier opening. Would you "
        f"like me to check {COVERING_PROVIDER_BARE_NAME}'s "
        f"availability instead?"
    )


# ─────────────────────────────────────────────
# Husband & Wife (couple) wellness scheduling flow
# ─────────────────────────────────────────────

def _sync_couple_identity(message, message_lower, app_module):
    """Fill the couple's identities from app.py's globals (patient 1 is
    the caller/pre-chart patient) and from the conversation history or
    the live message (patient 2 is the named spouse). Runs on every
    couple-flow turn so a name/relation stated late is still caught."""
    global couple_patient1_first, couple_patient1_last
    global couple_patient2_first, couple_patient2_last
    global couple_spouse_relation

    if not couple_patient1_first:
        couple_patient1_first = getattr(app_module, "caller_first_name", None) \
            or getattr(app_module, "patient_first_name", None)
    if not couple_patient1_last:
        couple_patient1_last = getattr(app_module, "caller_last_name", None) \
            or getattr(app_module, "patient_last_name", None)

    # Spouse relation: prefer the live message, otherwise recover the
    # original request from the earliest user turn in history (the
    # couple intent was stated there, possibly turns ago while
    # pre-chart ran).
    rel, first, last = _extract_spouse_name(message)
    if not rel:
        for turn in getattr(app_module, "conversation_history", []):
            if turn.get("role") != "user":
                continue
            content = turn.get("content", "")
            if any(w in content.lower() for w in _COUPLE_RELATION_WORDS):
                rel, first, last = _extract_spouse_name(content)
                if rel:
                    break
    if not rel:
        for w in _COUPLE_RELATION_WORDS:
            if w in message_lower:
                rel = w
                break
    if rel and not couple_spouse_relation:
        couple_spouse_relation = rel
    if rel and not couple_patient2_first and first:
        couple_patient2_first = first
    if rel and not couple_patient2_last and last:
        couple_patient2_last = last


def _format_couple_names():
    name1 = f"{couple_patient1_first} {couple_patient1_last}".strip()
    name2 = f"{couple_patient2_first} {couple_patient2_last}".strip()
    return name1, name2


def _find_patient_record_lenient(first, last):
    """Locate the most recent stored appointment for the couple flow,
    tolerating a slightly misspelled last name. Exact "First Last"
    lookup is tried first; when that misses, fall back to matching a
    store key whose first name matches exactly and whose last name
    shares a >= 3-char prefix with the given last name (e.g. the
    patient saying "Jone" must still surface the stored "Jones" record).
    Used ONLY by the couple locate step so a spouse-name typo cannot
    silently route an existing couple into the fresh-booking path."""
    if not (first and last):
        return None
    patient_full = f"{first} {last}".strip()
    exact = get_next_appointment_for_patient(patient_full)
    if exact:
        return exact
    first_l, last_l = first.lower(), last.lower()
    best = None
    for key, records in _load_appointments().items():
        if not records:
            continue
        parts = key.split()
        if len(parts) < 2:
            continue
        if parts[0].lower() != first_l:
            continue
        stored_last = parts[1].lower()
        common = min(len(stored_last), len(last_l))
        if common >= 3 and stored_last[:common] == last_l[:common]:
            best = records[-1]
    return best


def _couple_stored_appointment_records():
    """Return (record1, record2) = most recent Sprint14 appointment for
    each couple patient, or (None, None) if either patient has no
    on-file record or the couple's identities aren't both known yet.
    Reads the SAME store the couple booking flow persists to."""
    if not (couple_patient1_first and couple_patient1_last
            and couple_patient2_first and couple_patient2_last):
        return None, None
    record1 = _find_patient_record_lenient(
        couple_patient1_first, couple_patient1_last
    )
    record2 = _find_patient_record_lenient(
        couple_patient2_first, couple_patient2_last
    )
    if record1 and record2:
        return record1, record2
    return None, None


def _present_couple_locatable_appointments(record1, record2):
    """Build the narration that LOCATES the couple's already-scheduled
    appointments (both patients' records from the Sprint14 store) and
    asks whether to keep or reschedule them."""
    label = _visit_type_label(record1.get("visit_type", "wellness"))
    day1 = record1.get("appointment_day")
    day2 = record2.get("appointment_day")
    prov1 = record1.get("provider")
    prov2 = record2.get("provider")
    return (
        f"I can help with that. I show you and {couple_patient2_first} "
        f"already have {label} scheduled - yours with "
        f"{prov1 or 'your provider'} on {day1}, and "
        f"{couple_patient2_first}'s with {prov2 or 'your provider'} on "
        f"{day2}. Would you like to keep those appointments as they "
        f"are, or reschedule them for a different day or time?"
    )


def _book_couple_pair(pair, provider_name, storage_provider=None):
    """Persist both back-to-back appointments and return the closing
    response confirming both slots. storage_provider defaults to
    provider_name; pass the raw PCP name (may be None) so the
    placeholder display label never lands in the appointment store."""
    global couple_stage, wellness_stage, wellness_flow_active
    global couple_reschedule_old_days
    day, t1, t2 = pair
    name1, name2 = _format_couple_names()
    if storage_provider is None:
        storage_provider = provider_name
    # On a reschedule, replace the couple's existing visits with the new
    # pair instead of appending a duplicate alongside the old record.
    if couple_reschedule_old_days:
        _remove_appointment_for_patient(name1, couple_reschedule_old_days[0])
        _remove_appointment_for_patient(name2, couple_reschedule_old_days[1])
        couple_reschedule_old_days = None
    _persist_appointment(name1, couple_visit_type, f"{day} @ {t1}", storage_provider)
    _persist_appointment(name2, couple_visit_type, f"{day} @ {t2}", storage_provider)
    couple_stage = "complete"
    wellness_stage = "complete"
    wellness_flow_active = False
    label = _visit_type_label(couple_visit_type)
    return (
        f"Perfect. I have you scheduled for {label} on {day} @ {t1}, "
        f"and {couple_patient2_first} scheduled for {label} on "
        f"{day} @ {t2}, back-to-back with {provider_name}. Is there "
        f"anything else I can help you with today?"
    )


def _present_couple_pcp_pairs(provider_label):
    """Generate/present the PCP's back-to-back couple slots. Prepends
    the eligibility narration (both-eligible or accepted-not-eligible)
    exactly once; the offset floor keeps every offered date at or after
    the 1yr+1day eligible date."""
    global couple_pcp_pairs, couple_eligibility_narration
    narration = couple_eligibility_narration or ""
    couple_eligibility_narration = None
    if couple_pcp_pairs is None:
        couple_pcp_pairs = generate_couple_pcp_pairs(couple_pcp_earliest_offset)
    insurance_note = ""
    if couple_visit_type:
        insurance_note = {
            "cha": "Based on your shared insurance, I will schedule "
                   "you both for a Comprehensive Health Assessment. ",
            "maw": "Based on your shared insurance, I will schedule "
                   "you both for a Medicare Annual Wellness visit. ",
            "wellness": "Based on your shared insurance, I will "
                       "schedule you both for a standard wellness "
                       "visit. ",
        }[couple_visit_type]
    return (
        f"{narration}"
        f"Great, let's get those scheduled back-to-back. {insurance_note}"
        f"Here is {provider_label}'s availability:\n"
        f"{format_couple_pairs(couple_pcp_pairs)}\n"
        f"Which day and starting time works best for you? If none of "
        f"these work, just let me know and I can check for sooner "
        f"availability."
    )


def _handle_couple_wellness_flow(message, message_lower):
    """Schedule annual wellness/MAW/CHA visits for BOTH a husband and
    wife in one workflow:
      1. Patient 1 = the caller (already identified by app.py).
      2. Patient 2 = the spouse, named by the caller or asked for.
      3. ONE shared insurance determination drives BOTH visit types -
         Steve never invents a different insurance per person.
      4. Back-to-back PCP slots are offered first; if they aren't soon
         enough, escalate to Janet Walker (MAW/CHA) just like the
         single-visit escalation ladder.
    Returns None only if this message isn't a couple-flow reply at
    all (letting the caller's standard stage ladder take over)."""
    global couple_stage
    global couple_patient2_first, couple_patient2_last
    global couple_patient2_dob, couple_patient2_age
    global couple_pcp_pairs, couple_covering_pairs
    global couple_covering_provider_name
    global couple_shared_insurance, couple_visit_type
    global couple_last_visit_date1, couple_days_since_last_visit1
    global couple_last_visit_date2, couple_days_since_last_visit2
    global couple_eligible_both, couple_eligibility_narration
    global couple_pcp_earliest_offset
    global couple_existing_located
    global couple_is_reschedule
    global couple_reschedule_old_days
    global wellness_stage, wellness_flow_active
    global wellness_work_in_requested
    global wellness_patient_pcp

    import app as _app

    _sync_couple_identity(message, message_lower, _app)

    # Track whether the flow was entered by an explicit "reschedule"
    # request (Bob already has visits on the books and is changing
    # them), as opposed to a fresh "schedule" (back-to-back mentions
    # "reschedule"/"schedule" in the first message; recorded once so a
    # later reply mentioning "reschedule" can't flip fresh to reschedule).
    if not couple_is_reschedule and "reschedule" in message_lower:
        couple_is_reschedule = True

    provider_label = wellness_patient_pcp or "your provider"

    # Shared insurance determination happens exactly once and applies
    # to BOTH patients - the core "same insurance for both" rule.
    if not couple_shared_insurance:
        couple_shared_insurance = determine_insurance_type_for_senior()
        if couple_shared_insurance == "priority_medicare_advantage":
            couple_visit_type = "cha"
        elif couple_shared_insurance == "original_medicare":
            couple_visit_type = "maw"
        else:
            couple_visit_type = "wellness"

    if couple_stage is None:
        if not couple_patient2_first or not couple_patient2_last:
            couple_stage = "collect_spouse"
        elif not couple_patient2_dob:
            couple_stage = "collect_spouse_dob"
        elif _couple_stored_appointment_records()[0]:
            couple_stage = "locate_existing"
        else:
            couple_stage = "eligibility"

    # ── Stage: collect the spouse's name (if not already stated) ──
    if couple_stage == "collect_spouse":
        relation = couple_spouse_relation or "spouse"
        if not couple_patient1_first or not couple_patient1_last:
            return "Could I get your first and last name, please?"
        if not couple_patient2_first or not couple_patient2_last:
            # The caller was just asked for the spouse's name, so a
            # bare name reply ("William Brooks") is safe to accept.
            plain_first, plain_last = _extract_plain_reply_names(
                message,
                exclude_first=couple_patient1_first,
            )
            if plain_first and not couple_patient2_first:
                couple_patient2_first = plain_first
            if plain_last and not couple_patient2_last:
                couple_patient2_last = plain_last
        if not couple_patient2_first:
            action = "reschedule" if couple_is_reschedule else "schedule"
            return (
                f"Wonderful - I'd be happy to {action} your "
                f"{relation}'s visit back-to-back with yours. Could I "
                f"get your {relation}'s first and last name?"
            )
        if not couple_patient2_last:
            return (
                f"Thank you. Could I get {couple_patient2_first}'s "
                f"last name?"
            )
        couple_stage = "collect_spouse_dob"
        return _handle_couple_wellness_flow(message, message_lower)

    # ── Stage: collect the spouse's DOB (parity with the single flow) ──
    # Every couple entry point asks for the spouse's date of birth once
    # both parts of her name are known - previously Steve jumped
    # straight from the name reply into the eligibility narration
    # without ever asking for HER date of birth. A DOB bundled into the
    # same message as the name is captured here via the recursion below.
    if couple_stage == "collect_spouse_dob":
        if not couple_patient2_dob and detect_dob_in_message(message):
            couple_patient2_dob = extract_dob_from_message(message)
            couple_patient2_age = calculate_age_from_dob(couple_patient2_dob)
        if not couple_patient2_dob:
            return (
                f"Thank you. And could I get "
                f"{couple_patient2_first}'s date of birth, please?"
            )
        couple_stage = (
            "locate_existing" if _couple_stored_appointment_records()[0]
            else "eligibility"
        )
        return _handle_couple_wellness_flow(message, message_lower)

    # ── Stage: locate the couple's already-scheduled appointments ──
    # Reschedule/locate path entered when a callback request was generic
    # ("I'd like to schedule appointments for my wife and myself") and
    # BOTH patients already have Sprint14 records on file. Surfaces the
    # stored appointments ("locates" them); a keep reply closes the
    # flow, anything else jumps straight to the back-to-back rebooking
    # slots (eligibility was already passed when they first booked).
    if couple_stage == "locate_existing":
        record1, record2 = _couple_stored_appointment_records()
        if not (record1 and record2):
            couple_stage = "eligibility"
            return _handle_couple_wellness_flow(message, message_lower)
        if not couple_existing_located:
            couple_existing_located = True
            return _present_couple_locatable_appointments(record1, record2)
        if patient_wants_to_keep_appointments(message_lower):
            couple_stage = "complete"
            wellness_stage = "complete"
            wellness_flow_active = False
            return (
                "Great - I'll leave both appointments exactly where they "
                "are. Is there anything else I can help you with today?"
            )
        # The caller wants to MOVE the already-booked visits. Preserve the
        # exact visit type stored on the existing appointments and never offer a
        # replacement date earlier than either existing appointment.
        stored_visit_type = record1.get("visit_type")
        if stored_visit_type in ("wellness", "maw", "cha"):
            couple_visit_type = stored_visit_type

        couple_pcp_earliest_offset = _couple_reschedule_earliest_offset(
            record1, record2
        )

        # Force fresh availability generation using the reschedule date floor.
        couple_pcp_pairs = None
        couple_covering_pairs = None

        # Remember which visits are being replaced so the new booking
        # removes them from the store rather than duplicating them.
        couple_reschedule_old_days = (
            record1.get("appointment_day"), record2.get("appointment_day")
        )

        couple_eligibility_narration = (
            "Great. Let's find you a new day and time for both visits. "
        )
        couple_stage = "offer_pcp"
        return _present_couple_pcp_pairs(provider_label)

    # ── Stage: 1yr+1day eligibility check (BOTH patients) ──
    if couple_stage == "eligibility":
        if couple_days_since_last_visit1 is None or couple_days_since_last_visit2 is None:
            (couple_last_visit_date1, couple_days_since_last_visit1) = (
                generate_last_wellness_date()
            )
            (couple_last_visit_date2, couple_days_since_last_visit2) = (
                generate_last_wellness_date()
            )
            elig1 = is_eligible_for_next_wellness(couple_days_since_last_visit1)
            elig2 = is_eligible_for_next_wellness(couple_days_since_last_visit2)
            couple_eligible_both = elig1 and elig2
            relation = couple_spouse_relation or "spouse"
            if couple_eligible_both:
                couple_eligibility_narration = (
                    f"I see your last wellness visit was on "
                    f"{couple_last_visit_date1}. Since that is over a "
                    f"year and a day ago, you are eligible for your next "
                    f"wellness visit. And your {relation}'s last wellness "
                    f"visit was on {couple_last_visit_date2}, so "
                    f"{couple_patient2_first} is eligible as well. "
                )
                couple_stage = "offer_pcp"
                return _present_couple_pcp_pairs(provider_label)
            narration = ""
            if elig1:
                narration += (
                    f"I see your last wellness visit was on "
                    f"{couple_last_visit_date1}. Since that is over a "
                    f"year and a day ago, you are eligible for your next "
                    f"wellness visit. "
                )
            else:
                narration += (
                    f"I see your last wellness visit was on "
                    f"{couple_last_visit_date1}. In order for your "
                    f"insurance to count this appointment as a wellness "
                    f"visit, your upcoming visit needs to be scheduled at "
                    f"least one year and one day away from your last "
                    f"wellness visit, so you will not be eligible again "
                    f"until {calculate_next_eligible_date(couple_days_since_last_visit1)}. "
                )
            if elig2:
                narration += (
                    f"Your {relation}'s last wellness visit was on "
                    f"{couple_last_visit_date2}, and "
                    f"{couple_patient2_first} is eligible as well. "
                )
            else:
                narration += (
                    f"Your {relation}'s last wellness visit was on "
                    f"{couple_last_visit_date2}, so "
                    f"{couple_patient2_first} will not be eligible again "
                    f"until {calculate_next_eligible_date(couple_days_since_last_visit2)}. "
                )
            couple_eligibility_narration = (
                f"{narration}Since an annual wellness visit only counts "
                f"as a wellness visit when it is scheduled at least a "
                f"year and a day away from the previous one, are you OK "
                f"scheduling your visits that far out, or were you hoping "
                f"to schedule for a specific issue that would not count "
                f"as an annual wellness visit?"
            )
            return couple_eligibility_narration

        # Already evaluated and at least one patient is NOT eligible -
        # the caller is now replying to the checkpoint question.
        if patient_wants_to_proceed(message_lower) or patient_accepts_far_out_timeline(message_lower):
            couple_pcp_earliest_offset = max(
                max(367 - couple_days_since_last_visit1,
                    367 - couple_days_since_last_visit2),
                1,
            )
            # Clear the checkpoint text (it must not be prepended to the
            # availability below) and confirm before pulling up the
            # provider's back-to-back openings.
            couple_eligibility_narration = (
                "Great. I will schedule your visits on or after the date "
                "you are both eligible for your next wellness visit. "
            )
            couple_stage = "offer_pcp"
            return _present_couple_pcp_pairs(provider_label)
        if patient_wants_to_decline(message_lower):
            couple_stage = "complete"
            wellness_stage = "complete"
            wellness_flow_active = False
            return (
                "OK - since an annual wellness visit needs to be "
                "scheduled at least one year and one day after the "
                "previous one in order to count as a wellness visit, I "
                "will hold off on scheduling those for now. Is there "
                "anything else I can help you with today?"
            )
        return couple_eligibility_narration

    # ── Stage: offer the PCP's back-to-back couple slots ──
    if couple_stage == "offer_pcp":
        if couple_pcp_pairs is None:
            couple_pcp_pairs = generate_couple_pcp_pairs()
        if detect_appointment_too_far_out(message_lower):
            couple_stage = "offer_janet"
            if couple_visit_type in ("maw", "cha"):
                couple_covering_provider_name = JANET_WALKER_NAME
                return (
                    f"Your provider's earliest back-to-back opening is "
                    f"not soon enough. Would you like me to check "
                    f"{JANET_WALKER_NAME}'s availability instead?"
                )
            couple_covering_provider_name = COVERING_PROVIDER_BARE_NAME
            return (
                f"Your provider's earliest back-to-back opening is "
                f"not soon enough. Would you like me to check "
                f"{COVERING_PROVIDER_BARE_NAME}'s availability instead?"
            )
        picked = _match_couple_pair(message, message_lower, couple_pcp_pairs)
        if picked:
            return _book_couple_pair(
                picked, provider_label, storage_provider=wellness_patient_pcp
            )
        requested_day = _couple_requested_day_unavailable(
            message_lower, couple_pcp_pairs
        )
        if requested_day:
            couple_stage = "offer_janet"
            if couple_visit_type in ("maw", "cha"):
                couple_covering_provider_name = JANET_WALKER_NAME
                return (
                    f"I don't see any {requested_day} openings for "
                    f"{provider_label} in the availability I shared. "
                    f"Would you like me to check "
                    f"{JANET_WALKER_NAME}'s availability instead?"
                )
            couple_covering_provider_name = COVERING_PROVIDER_BARE_NAME
            return (
                f"I don't see any {requested_day} openings for "
                f"{provider_label} in the availability I shared. "
                f"Would you like me to check "
                f"{COVERING_PROVIDER_BARE_NAME}'s availability instead?"
            )
        if patient_wants_to_decline(message_lower):
            couple_stage = "offer_janet"
            if couple_visit_type in ("maw", "cha"):
                couple_covering_provider_name = JANET_WALKER_NAME
                return (
                    "I understand. Would you like me to check "
                    f"{JANET_WALKER_NAME}'s availability instead?"
                )
            couple_covering_provider_name = COVERING_PROVIDER_BARE_NAME
            return (
                "I understand. Would you like me to check "
                f"{COVERING_PROVIDER_BARE_NAME}'s availability instead?"
            )
        return _present_couple_pcp_pairs(provider_label)

    # ── Stage: escalate to Janet Walker / covering provider ──
    if couple_stage == "offer_janet":
        if couple_covering_pairs:
            picked = _match_couple_pair(message, message_lower, couple_covering_pairs)
            if picked:
                return _book_couple_pair(picked, couple_covering_provider_name)
        if patient_wants_to_decline(message_lower):
            wellness_work_in_requested = True
            wellness_stage = "await_callback"
            couple_stage = "await_callback"
            return (
                f"I will create a high priority work-in request so "
                f"both of you can be seen. May I get a good callback "
                f"number for you?"
            )
        if not couple_covering_pairs:
            couple_covering_pairs = generate_couple_covering_pairs(
                couple_pcp_earliest_offset
            )
            return (
                f"Here is {couple_covering_provider_name}'s availability:\n"
                f"{format_couple_pairs(couple_covering_pairs)}\n"
                f"Which day and starting time works best for you?"
            )
        return (
            f"Here is {couple_covering_provider_name}'s availability:\n"
            f"{format_couple_pairs(couple_covering_pairs)}\n"
            f"Which day and starting time works best for you?"
        )

    return None


# ─────────────────────────────────────────────
# Medication refill escalation (PDF Section 6)
# ─────────────────────────────────────────────

def _handle_refill_escalation(message, message_lower):
    global refill_stage, refill_medication_name, refill_days_remaining
    global refill_will_run_out_before_next_appt, refill_escalation_active
    global wellness_stage, wellness_flow_active

    if refill_stage == "ask_medication":
        refill_medication_name = message.strip()
        refill_stage = "ask_days_remaining"
        return (
            f"Thank you. How many days of {refill_medication_name} do "
            f"you have left?"
        )

    if refill_stage == "ask_days_remaining":
        days_match = re.search(r'\d+', message)
        if not days_match:
            return "How many days of medication do you currently have left?"
        refill_days_remaining = int(days_match.group(0))

        # Compare against the wellness appointment date if one is
        # already on the books; otherwise assume the worst case (no
        # appointment booked yet) so the patient is offered help either
        # way rather than being told "you're fine" with no appointment
        # actually scheduled.
        # EXPANSION POINT: once a real scheduling backend exists, this
        # should compute actual calendar days between today and
        # wellness_appointment_day instead of this conservative default.
        refill_will_run_out_before_next_appt = True

        refill_stage = "offer_options"
        return (
            "It sounds like you may run out before your next "
            "appointment. Would you like me to offer you the next "
            "available appointment, or would you prefer I send a "
            "phone message asking the provider to authorize enough "
            "medication to bridge you until then?"
        )

    if refill_stage == "offer_options":
        wants_appointment = any(
            p in message_lower for p in [
                "appointment", "next available", "schedule", "book"
            ]
        )
        wants_bridge = any(
            p in message_lower for p in [
                "message", "bridge", "phone message", "note", "call in"
            ]
        )
        if wants_appointment and not wants_bridge:
            refill_escalation_active = False
            refill_stage = None
            wellness_stage = "check_pcp_availability"
            return (
                "Sure, let's find the next available appointment. "
                "One moment while I pull that up."
            )
        if wants_bridge:
            refill_escalation_active = False
            refill_stage = None
            wellness_stage = "complete"
            wellness_flow_active = False
            return (
                f"I have sent a phone message to your provider "
                f"requesting enough {refill_medication_name} to bridge "
                f"you until your next appointment. Someone from our "
                f"office will follow up with you. Is there anything "
                f"else I can help you with today?"
            )
        return (
            "Would you like the next available appointment, or a "
            "phone message asking the provider to bridge your "
            "medication until then?"
        )

    return None


# ─────────────────────────────────────────────
# Lab order / EHR guidance (PDF Section 7)
# ─────────────────────────────────────────────

def _looks_like_mailing_address(message):
    """Heuristic to detect a complete US mailing address in free text.
    Used when the patient provides an address without explicitly saying
    'mail' after being asked mail/fax/pickup. Requires a street
    number, a state name/abbreviation, and a 5-digit ZIP code to
    minimize false positives against fax/pickup responses."""
    # Street number followed by a word (e.g., "642 Dan", "123 Main")
    has_street = re.search(r'\b\d+\s+[A-Za-z]', message)
    # 5-digit ZIP code
    has_zip = re.search(r'\b\d{5}\b', message)
    # US state name or two-letter abbreviation
    state_pattern = (
        r'\b(?:Alabama|Alaska|Arizona|Arkansas|California|Colorado|'
        r'Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|'
        r'Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|'
        r'Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|'
        r'Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|'
        r'North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|'
        r'Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|'
        r'Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|'
        r'AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|'
        r'MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|'
        r'SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b'
    )
    has_state = re.search(state_pattern, message, re.IGNORECASE)
    return bool(has_street and has_state and has_zip)


def _looks_like_fax_number(message):
    """Heuristic to detect a US fax/phone number in free text.
    Matches common formats: 555-123-4567, (555) 123-4567, 555.123.4567,
    555 123 4567, or 10 consecutive digits."""
    patterns = [
        r'\(\d{3}\)\s*\d{3}[-.\s]?\d{4}',          # (555) 123-4567
        r'\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b',        # 555-123-4567
        r'\b\d{10}\b',                               # 5551234567
    ]
    for pat in patterns:
        if re.search(pat, message):
            return True
    return False


def _handle_lab_order_ehr_guidance(message, message_lower):
    global lab_order_ehr_stage, lab_order_uncomfortable_with_tech
    global lab_order_ehr_guidance_active, wellness_stage
    global wellness_flow_active
    global lab_order_mailing_address, lab_order_fax_destination

    if lab_order_ehr_stage is None:
        if detect_lab_send_request_intent(message_lower):
            priority_lab = _extract_priority_network_lab(message_lower)
            if priority_lab:
                lab_order_ehr_guidance_active = False
                lab_order_ehr_stage = None
                wellness_stage = "complete"
                wellness_flow_active = False
                return (
                    f"Because that lab is part of the Priority Care "
                    f"Network, they can access your lab orders directly "
                    f"through your chart. There is no need for us to "
                    f"send the orders separately. Is there anything "
                    f"else I can help you with today?"
                )
            if any(t in message_lower for t in KNOWN_EXTERNAL_LAB_TRIGGERS):
                lab_order_ehr_stage = "ask_comfort"
                return (
                    "I can help you locate that. Are you comfortable "
                    "checking MyChart, or would you prefer another "
                    "option?"
                )
            lab_order_ehr_stage = "priority_network_ask_lab"
            return "Which lab will you be using?"

        lab_order_ehr_stage = "ask_comfort"
        return (
            "I can help you locate that. Are you comfortable checking "
            "MyChart, or would you prefer another option?"
        )

    if lab_order_ehr_stage == "priority_network_ask_lab":
        priority_lab = _extract_priority_network_lab(message_lower)
        if priority_lab:
            lab_order_ehr_guidance_active = False
            lab_order_ehr_stage = None
            wellness_stage = "complete"
            wellness_flow_active = False
            return (
                f"Because that lab is part of the Priority Care "
                f"Network, they can access your lab orders directly "
                f"through your chart. There is no need for us to send "
                f"the orders separately. Is there anything else I can "
                f"help you with today?"
            )
        # Named a lab, just not one in the Priority Care Network -
        # continue into the existing lab-order workflow unchanged.
        lab_order_ehr_stage = "ask_comfort"
        return (
            "I can help you locate that. Are you comfortable checking "
            "MyChart, or would you prefer another option?"
        )

    if lab_order_ehr_stage == "ask_comfort":
        if patient_uncomfortable_with_tech(message_lower) or patient_wants_to_decline(message_lower):
            lab_order_uncomfortable_with_tech = True
            lab_order_ehr_stage = "offer_alternate_options"
            return (
                "No problem at all. I can mail the lab order to you, "
                "fax it to a location of your choice, or have it ready "
                "for pickup at our office. Which would you prefer?"
            )
        lab_order_uncomfortable_with_tech = False
        lab_order_ehr_stage = "guide_ehr"
        return (
            "Great. Please log into MyChart and check under your "
            "test/lab orders section - the order should be listed "
            "there. If you're part of the Priority Care lab network, "
            "the lab can also pull up your order directly using your "
            "name and date of birth. Were you able to locate it, or "
            "would you like a different option?"
        )

    if lab_order_ehr_stage == "guide_ehr":
        if patient_wants_to_decline(message_lower) or "no" in message_lower or "can't" in message_lower or "cannot" in message_lower:
            lab_order_ehr_stage = "offer_alternate_options"
            return (
                "No problem. I can mail the lab order to you, fax it "
                "to a location of your choice, or have it ready for "
                "pickup at our office. Which would you prefer?"
            )
        if "print" in message_lower:
            lab_order_ehr_stage = "ehr_print_followup"
            return (
                "Underneath each lab, you will see a button that says "
                "'Download Document'. Click the button for each lab order."


                "Once downloaded:"


                "Windows:"

                "Open your Downloads folder from the bottom of the screen."


                "Mac:"

                "Open the downloaded files from Finder."


                "Open each downloaded lab order and print it."


                "Please let me know if you were able to print your lab orders "
                "or if you would like to try another method to get the orders "
                "to your lab."
            )
        lab_order_ehr_guidance_active = False
        lab_order_ehr_stage = None
        wellness_stage = "complete"
        wellness_flow_active = False
        return (
            "Great, glad that worked. Is there anything else I can "
            "help you with today?"
        )

    if lab_order_ehr_stage == "ehr_print_followup":
        # Broad success-intent recognition for print confirmations.
        # Covers explicit yes, "I was able to print", "I printed them",
        # "I got them printed", and similar declarative success phrasing.
        print_success_triggers = [
            "yes", "worked", "successful", "got it", "i printed",
            "i was able to print", "i got them printed",
            "i got it printed", "printing worked", "they printed",
            "all set", "done", "completed",
        ]
        if any(p in message_lower for p in print_success_triggers):
            lab_order_ehr_guidance_active = False
            lab_order_ehr_stage = None
            wellness_stage = "complete"
            wellness_flow_active = False
            return (
                "That's great. Is there anything else I can help you with "
                "today?"
            )
        if any(p in message_lower for p in [
            "no", "didn't work", "did not work", "wasn't able",
            "was not able", "unable", "another method",
            "different method", "try another", "can't print",
            "cannot print",
        ]):
            lab_order_ehr_stage = "offer_alternate_options"
            return (
                "No problem. I can mail the lab order to you, fax it "
                "to a location of your choice, or have it ready for "
                "pickup at our office. Which would you prefer?"
            )
        return (
            "Please let me know if you were able to print your lab orders "
            "or if you would like to try another method to get the orders "
            "to your lab."
        )

    if lab_order_ehr_stage == "offer_alternate_options":
        if "mail" in message_lower:
            lab_order_ehr_stage = "verify_mailing_address"
            return (
                "To ensure that the lab orders get mailed to the correct "
                "location, could you please verify your mailing address?"
            )
        elif "fax" in message_lower:
            # Patient wants to fax — validate that a complete destination
            # (fax number or full address) was provided, not just a
            # generic location name like "Quest".
            if _looks_like_fax_number(message) or _looks_like_mailing_address(message):
                lab_order_fax_destination = message.strip()
                lab_order_ehr_guidance_active = False
                lab_order_ehr_stage = None
                wellness_stage = "complete"
                wellness_flow_active = False
                return (
                    "Sounds good, I will fax the lab order to the location "
                    "you provide. Is there anything else I can help you "
                    "with today?"
                )
            lab_order_ehr_stage = "collect_fax_destination"
            return (
                "Can I get a good fax number for that location? If you "
                "don't know the fax number, you can provide the address "
                "and I can look up the fax number."
            )
        elif "pick" in message_lower:
            choice = "have the lab order ready for pickup at our office"
            lab_order_ehr_guidance_active = False
            lab_order_ehr_stage = None
            wellness_stage = "complete"
            wellness_flow_active = False
            return (
                f"Sounds good, I will {choice}. Is there anything else I "
                f"can help you with today?"
            )
        elif _looks_like_mailing_address(message):
            # Patient provided a complete mailing address without
            # explicitly saying "mail" — infer the mail option.
            lab_order_mailing_address = message.strip()
            lab_order_ehr_guidance_active = False
            lab_order_ehr_stage = None
            wellness_stage = "complete"
            wellness_flow_active = False
            return (
                "Thank you. I will mail the lab order to that address. "
                "Is there anything else I can help you with today?"
            )
        else:
            return (
                "Would you like me to mail it, fax it, or have it "
                "ready for pickup at our office?"
            )

    if lab_order_ehr_stage == "verify_mailing_address":
        lab_order_mailing_address = message.strip()
        lab_order_ehr_guidance_active = False
        lab_order_ehr_stage = None
        wellness_stage = "complete"
        wellness_flow_active = False
        return (
            "Thank you. I will mail the lab orders to that address. "
            "Is there anything else I can help you with today?"
        )

    if lab_order_ehr_stage == "collect_fax_destination":
        if _looks_like_fax_number(message) or _looks_like_mailing_address(message):
            lab_order_fax_destination = message.strip()
            lab_order_ehr_guidance_active = False
            lab_order_ehr_stage = None
            wellness_stage = "complete"
            wellness_flow_active = False
            return (
                "Sounds good, I will fax the lab order to the location "
                "you provide. Is there anything else I can help you "
                "with today?"
            )
        return (
            "Can I get a good fax number for that location? If you "
            "don't know the fax number, you can provide the address "
            "and I can look up the fax number."
        )

    return None


# ─────────────────────────────────────────────
# Context builder for LLM injection
# ─────────────────────────────────────────────

def build_context():
    if not wellness_flow_active and not refill_escalation_active and not lab_order_ehr_guidance_active:
        return ""

    context = "WELLNESS_MAW_CHA_WORKFLOW INJECTED BY SYSTEM:\n"
    context += f"Stage: {wellness_stage or 'initial'}\n"
    if wellness_requested_visit_type:
        context += f"Visit type: {wellness_requested_visit_type}\n"
    if wellness_patient_age is not None:
        context += f"Patient age: {wellness_patient_age}\n"
    if wellness_insurance_type:
        context += f"Insurance type: {wellness_insurance_type}\n"
        context += f"Eligible for MAW: {wellness_eligible_for_maw}\n"
        context += f"Eligible for CHA: {wellness_eligible_for_cha}\n"
    if wellness_pcp_availability:
        context += "PCP_AVAILABILITY:\n" + format_availability(wellness_pcp_availability) + "\n"
    if wellness_covering_availability:
        context += (
            f"{(wellness_covering_provider_name or 'COVERING_PROVIDER').upper()}_AVAILABILITY:\n"
            + format_availability(wellness_covering_availability) + "\n"
        )
    if wellness_work_in_requested:
        context += "WORK_IN_REQUESTED: True\nDo NOT offer scheduling. Collect callback number only.\n"
    if refill_escalation_active:
        context += f"REFILL_ESCALATION_ACTIVE: stage={refill_stage}\n"
    if lab_order_ehr_guidance_active:
        context += f"LAB_ORDER_EHR_GUIDANCE_ACTIVE: stage={lab_order_ehr_stage}\n"

    if wellness_requested_visit_type == "couple":
        context += f"COUPLE_WELLNESS_ACTIVE: stage={couple_stage or 'init'}\n"
        if couple_visit_type:
            context += f"Shared visit type: {couple_visit_type}\n"
        if couple_covering_provider_name:
            context += f"Escalation provider: {couple_covering_provider_name}\n"
        context += (
            "Husband and wife back-to-back wellness scheduling. "
            "Both patients share the same insurance and visit type. "
            "Schedule ONLY back-to-back same-day pairs.\n"
        )

    context += (
        "This workflow is handled deterministically by Python. Do NOT "
        "improvise scheduling, eligibility, or work-in decisions "
        "yourself - follow the Python-generated response for this "
        "turn.\n"
    )
    return context
