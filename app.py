import os
import sys

# Root-cause fix: when this file is launched directly (`python app.py`),
# Python loads it as the module `__main__`, NOT as a module named
# `app`. Sprint13.py and Sprint14.py both do `import app` internally
# (to read this file's live conversation_history/patient_first_name/
# etc.) - without this registration, that `import app` doesn't find
# this already-running module, so Python loads app.py from disk AGAIN
# as a brand-new, second, disconnected module object with its own
# fresh globals (conversation_history=[], patient_first_name=None...)
# that no HTTP request ever touches. That phantom copy is what
# Sprint13/Sprint14 were reading from, which is why they'd see empty
# state despite the real, request-handling copy being fully populated.
# Registering this module under the name "app" up front means any
# later `import app` reuses this exact object instead of re-loading a
# duplicate. Must run before the Sprint13/Sprint14 imports below.
sys.modules.setdefault("app", sys.modules[__name__])

# os.environ["STEVE_FORCE_PCP_AVAILABLE"] = "false"
import re
import random
import secrets
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from groq import Groq
import Sprint13
import Sprint14

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# ─────────────────────────────────────────────
# Groq API key loading
# ─────────────────────────────────────────────
# Keys are read from the environment (optionally seeded from the local,
# gitignored .env file) instead of a hardcoded literal, so a key can be
# rotated without editing this file. Multiple keys are supported so that
# when one account hits its daily token cap (HTTP 429) the next key is
# tried automatically: set GROQ_API_KEYS to a comma-separated list,
# and/or GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3, ... Duplicates are
# dropped and order is preserved (primary first).
def _load_dotenv(path):
    """Minimal stdlib .env loader (no python-dotenv dependency). Only
    sets variables not already present in the environment, so a real
    environment variable always wins over the file."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                name = name.strip()
                value = value.strip().strip('"').strip("'")
                if name and name not in os.environ:
                    os.environ[name] = value
    except OSError:
        pass


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def _collect_groq_api_keys():
    keys = []
    for raw in (os.environ.get("GROQ_API_KEYS", ""),
                os.environ.get("GROQ_API_KEY", "")):
        for candidate in raw.split(","):
            candidate = candidate.strip()
            if candidate and candidate not in keys:
                keys.append(candidate)
    index = 2
    while True:
        candidate = os.environ.get(f"GROQ_API_KEY_{index}", "").strip()
        if not candidate:
            break
        if candidate not in keys:
            keys.append(candidate)
        index += 1
    return keys


GROQ_API_KEYS = _collect_groq_api_keys()
groq_clients = [Groq(api_key=key) for key in GROQ_API_KEYS]
client = groq_clients[0] if groq_clients else None


def groq_create_completion(messages, **kwargs):
    """Call the Groq API, rotating through all configured keys on any
    error.  The *last* error is re-raised so real failures still surface
    once every key has been tried."""
    if not groq_clients:
        raise RuntimeError(
            "No Groq API key configured. Set GROQ_API_KEY in the "
            "environment or in the project's .env file."
        )
    last_error = None
    for idx, groq_client in enumerate(groq_clients):
        try:
            return groq_client.chat.completions.create(
                messages=messages, **kwargs
            )
        except Exception as e:
            last_error = e
            print(
                f"[groq_create_completion] key#{idx + 1}/{len(groq_clients)} "
                f"failed: {e}"
            )
    raise last_error

conversation_history = []
profanity_count = 0
hipaa_status_determined = False
current_hipaa_status = None
third_party_availability_asked = False
third_party_detected = False
dob_collected = False
patient_self_dob_verified = False
# Sprint HIPAA fix: the patient taken on the line during a third-party
# call must verify BOTH their last name AND their date of birth before
# verbal consent is requested (patient_self_dob_verified exists for the
# DOB half; this flag covers the last-name half).
patient_self_last_name_verified = False
pcp_collected = False
verbal_consent_requested = False
third_party_consent_asked = False
third_party_consent_obtained = False
response_time_stated = False
office_hours_stated = False
lab_result_fax_active = False
lab_result_inquiry_active = False
ma_request_active = False
ma_request_name = None
ma_request_provider = None
ma_request_reason_collected = False
ma_request_reason_asked = False
referral_lookup_done = False
referral_lookup_result = None
referral_specialist_name = None
referral_specialist_phone = None
# Nurse-visit request state: once a patient requests a nurse visit
# (injection, B12/flu shot, vaccine, etc.) this stays True for the rest
# of the call so the injected NURSE_VISIT context keeps Steve on the
# correct in-office / MA-scheduled path across follow-up turns (e.g. the
# turn where the patient provides a contact number). Reset alongside the
# other per-call state in home().
nurse_visit_active = False

# New patient workflow state (Sprint 11)
new_patient_flow_active = False
new_patient_requested_provider = None
new_patient_is_minor = None
new_patient_minor_age = None
new_patient_parent_on_line_pending = None
# Sprint 15 (established patients): the under-18 DOB gate lives in the
# new-patient flow, but existing patients (e.g. "I'm a patient of Dr.
# Smith") never enter handle_new_patient_flow - determine_pre_chart_response
# digests their DOB and only sets dob_collected. These flags carry the
# parent/guardian gate for the established-patient path so a minor like
# "Alice Addison, DOB 5/2/2011" cannot book without a guardian on the line.
established_patient_minor_pending = None
established_patient_minor_age = None
established_patient_minor_guardian_confirmed = False
established_patient_dob = None
# Sprint 15 (established patients): parent FETCHED the minor away from
# pre-chart ("...going to put my mom on the phone right now. Please
# wait." was routed by the transfer branch) and Steve said "Go ahead,
# I'll wait." While True, the next message(s) are expected from the
# parent/guardian who must identify themselves by name before the gate
# is confirmed and scheduling can proceed.
established_patient_minor_waiting = False
# Sprint 15 (established patients): a parent/guardian who identifies with
# only a first name ("Hi this is Dawn") must not be re-asked for their
# first AND last name - Steve holds the half already given here until the
# missing half arrives, then confirms the guardian. Distinct from
# caller_first_name / caller_last_name because the minor's own name still
# occupies those variables until the guardian is fully identified.
established_patient_minor_parent_first = None
established_patient_minor_parent_last = None
new_patient_eligible_providers = None
new_patient_offered_provider = None
new_patient_accepting_checked = False
new_patient_no_provider_available = False
new_patient_insurance_collected = False
new_patient_insurance_type = None
new_patient_insurance_name = None
new_patient_insurance_accepted = None
new_patient_insurance_pending_oon = False
new_patient_self_pay_intent = False
new_patient_demographics_stage = None
new_patient_first_name = None
new_patient_last_name = None
new_patient_dob = None
new_patient_street_address = None
new_patient_email = None
new_patient_phone = None
new_patient_member_number = None
new_patient_text_consent = None
new_patient_weekly_schedule = None
new_patient_appointment_selection = None
new_patient_household_relation = None
new_patient_household_stage = "primary"
new_patient_household_second_time = None
new_patient_household_primary_first_name = None
new_patient_household_primary_address = None
new_patient_household_speaker_active = False

# Urgent symptoms state
urgent_symptoms_active = False
urgent_can_wait_asked = False
# Sticky same-day/acute urgency flag. Traced Scenario 13 defect: is_same_day
# is recomputed fresh from each individual message's own wording, so once a
# patient establishes same-day urgency ("I need an appointment today...")
# and then, in a LATER message, describes their symptoms without repeating
# "today" (e.g. "I have a very persistent cough..."), is_same_day silently
# reverts to False for that turn and the AI improvises without the
# deterministic same-day/office-hours context - asking "can this wait"
# again even though urgency was already established. Once True, this stays
# True for the rest of the call (reset only at Sprint14.reset_state() time,
# alongside the other per-call state below) instead of being recomputed
# per-message.
acute_same_day_established = False
ma_availability_determined = False
current_ma_availability = None

# Sprint 12: Acute visit nuances - contagious/virtual-refusal and UTI
contagious_visit_active = False
contagious_same_day_check_pending = False
virtual_visit_offered = False
provider_callin_offer_pending = False
symptoms_pharmacy_pending = False
provider_callin_decision_determined = False
current_provider_callin_decision = None
uti_antibiotic_demand_active = False
ma_same_day_approval_determined = False
current_ma_same_day_approval = None
covering_provider_offer_pending = False
covering_provider_visit_eligible = False
covering_provider_same_day_determined = False
current_covering_provider_same_day_available = None
same_day_virtual_clinic_offer_pending = False
next_available_options_pending = False
next_day_or_urgent_care_pending = False

# Scenario 16/17: Reschedule / Cancel Acute Visit
acute_existing_appt_day = None
acute_existing_appt_time = None
acute_reschedule_confirm_pending = False
acute_cancel_confirm_pending = False
acute_new_time_pending = False

# Generic (non-PHF) appointment reschedule - mirrors the acute-visit
# pending flags above, scoped to routine future appointments stored in
# generic_appointment_records instead of the simulated acute visit.
generic_reschedule_pending = False
generic_joint_cancel_pending = False
# Provider-cancelled appointment reschedule offer (the cancellation-why
# path): the reason-answer turn asks "Would you like to reschedule it?",
# a yes-turn presents availability, and the following turn captures the
# picked slot. Kept independent of generic_reschedule_pending above
# because a cancelled appointment may no longer be on file at all.
generic_cancelled_reschedule_pending = False
generic_cancelled_reschedule_offered = False
generic_cancelled_reschedule_provider = None
# Spouse leg of a generic reschedule: the caller asked to move a
# spouse's appointment too, but the office cannot pull up/change the
# spouse's record without first collecting their first and last name
# and date of birth. The caller's own slot is stored immediately; the
# spouse's slot is held here until that identity is collected on a
# later turn.
generic_spouse_reschedule_pending = False
generic_spouse_reschedule_slot = None
generic_spouse_reschedule_relation = None
# Dual NEW-PATIENT household reschedule ("my spouse and myself are
# scheduled to establish care with Dr. Mitchell and we need to reschedule
# those appointments"). The caller has never been seen at the office, so
# the spouse has never had a chance to add the caller to a HIPAA form -
# the office cannot pull up/transfer the spouse's chart on the caller's
# authority alone. Steve must collect the spouse's first/last name, date
# of birth, AND the originally-scheduled appointment day/time to locate
# it, reschedule the spouse first, then the caller.
generic_newpatient_household_reschedule_pending = False
generic_household_reschedule_stage = None
generic_household_spouse_first = None
generic_household_spouse_last = None
generic_household_spouse_dob = None
generic_household_spouse_old_slot = None
generic_household_spouse_relation = None

# Dual NEW-PATIENT household cancellation mirror: same HIPAA rationale
# as the reschedule flow - the caller's own records exist, but the
# office cannot cancel a spouse's separate appointment on the caller's
# authority alone, so the spouse's name/DOB/original slot are collected
# first, then BOTH stored appointments are removed.
generic_newpatient_household_cancel_pending = False
generic_household_cancel_stage = None
generic_household_cancel_spouse_first = None
generic_household_cancel_spouse_last = None
generic_household_cancel_spouse_dob = None
generic_household_cancel_spouse_old_slot = None
generic_household_cancel_spouse_relation = None

# Virtual-visit wait workflow: the patient is awaiting a scheduled
# virtual visit and reports the PCP hasn't joined (or shows any sign of
# annoyance with the wait). Steve explains the provider is running
# behind, offers to reschedule or wait a little longer, presents the
# provider's availability on a reschedule, and thanks the patient if
# they choose to wait.
virtual_wait_active = False
virtual_wait_choice_pending = False
virtual_wait_reschedule_pending = False
virtual_wait_reschedule_schedule = None
virtual_wait_reschedule_provider = None

# Patient-running-late workflow: the patient calls to say they will be
# late for their appointment. Steve asks how many minutes late they will
# be; under 15 minutes Steve just lets them know he will inform the
# medical assistant; 15 minutes or more means the slot cannot be held
# and Steve offers to reschedule during the call. Deterministic Python
# state machine like virtual_wait_* above - never delegated to the LLM.
late_arrival_active = False
late_arrival_minutes_asked = False
late_arrival_reschedule_pending = False
late_arrival_reschedule_offered = False
late_arrival_reschedule_schedule = None

# Established-couple routine six-month follow-up workflow.
couple_followup_flow_active = False
couple_followup_previous_visit_date = None
couple_followup_available_pairs = None
individual_six_month_followup_active = False
individual_six_month_followup_previous_visit_date = None
individual_six_month_followup_days_since = None
individual_six_month_followup_eligible = None
individual_six_month_followup_spouse_stage = None
individual_six_month_followup_spouse_slot = None
individual_six_month_followup_spouse_relation = None
individual_six_month_followup_spouse_first_name = None
individual_six_month_followup_spouse_last_name = None

# Established-couple routine three-month follow-up workflow. Mirrors the
# six-month flow's shape but is a deliberately separate set of variables
# so the two flows never cross-trigger.
individual_three_month_followup_active = False
individual_three_month_followup_previous_visit_date = None
individual_three_month_followup_days_since = None

# Controlled-substance appointment bridge follow-up (Scenario 11/12).
# Set when a patient explicitly requests an appointment with a named
# controlled substance as the reason (see
# patient_explicitly_requested_appointment_for_med below) - persists
# across turns until the LLM-driven booking is captured, at which
# point a deterministic Python-authored bridge question is appended
# and controlled_substance_bridge_awaiting_days takes over for the
# next turn. Kept fully deterministic (no reliance on the LLM
# correctly remembering to ask this) given the repeated prompt-
# compliance issues already found on this same appointment path.
controlled_substance_appt_pending = False
controlled_substance_appt_medication_word = None
controlled_substance_appt_schedule = None
controlled_substance_bridge_awaiting_days = False
controlled_substance_appt_day_name = None
controlled_substance_appt_date = None
controlled_substance_bridge_awaiting_dosage = False
controlled_substance_bridge_dosage = None
controlled_substance_bridge_awaiting_pharmacy = False
controlled_substance_bridge_awaiting_callback = False
controlled_substance_bridge_callback_number = None
controlled_substance_bridge_confirmed_insufficient = False

# Generic (non-PHF) appointment availability snapshot. The weekly
# schedule is randomly generated; before this cache it was regenerated
# on EVERY turn, so a slot Steve offered on one turn (e.g. "Friday,
# September 18 at 1:00 PM") could be absent from the freshly-generated
# schedule injected on the very next turn when the patient accepted it -
# producing "that time is not available" for a slot Steve had just
# listed. Generate once per call and reuse; reset alongside the other
# per-call state in home().
generic_weekly_schedule_snapshot = None

# Covering-provider counterpart to generic_weekly_schedule_snapshot. The
# covering schedule is also randomly generated and was regenerated on
# every turn, so a covering slot offered on one turn (when the PCP had no
# availability) could be absent when the patient accepted it. Cache it the
# same way; reset alongside the other per-call state in home().
generic_covering_weekly_schedule_snapshot = None

# Pre-chart state
caller_first_name = None
caller_last_name = None
patient_first_name = None
patient_last_name = None
caller_is_patient = None
pre_chart_complete = False
is_medical_professional_caller = False

# Medical professional state
med_pro_patient_first = None
med_pro_patient_last = None
med_pro_patient_dob = None
med_pro_patient_pcp = None
med_pro_collection_complete = False
med_pro_referral_looked_up = False
med_pro_referral_status = None
med_pro_referral_date = None
med_pro_referral_provider = None
med_pro_referral_reason = None

AVAILABLE_TIMES = [
    "9:00 AM", "9:30 AM", "10:00 AM", "10:30 AM",
    "11:00 AM", "11:30 AM", "1:00 PM", "1:30 PM",
    "2:00 PM", "2:30 PM", "3:00 PM", "3:30 PM",
    "4:00 PM", "4:40 PM"
]

DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

PROVIDERS = [
    "Dr. Sheldon Stroman", "Dr. Sarah Mitchell", "Dr. Robert Chen",
    "Dr. Maria Rodriguez", "Dr. David Thompson", "Dr. Jennifer Park",
    "Dr. Michael Brooks", "Dr. Lisa Anderson", "Dr. Kevin Patel",
    "Dr. Amanda Foster"
]

PROVIDER_LAST_NAMES = {
    "stroman": "Dr. Sheldon Stroman",
    "mitchell": "Dr. Sarah Mitchell",
    "chen": "Dr. Robert Chen",
    "rodriguez": "Dr. Maria Rodriguez",
    "thompson": "Dr. David Thompson",
    "park": "Dr. Jennifer Park",
    "brooks": "Dr. Michael Brooks",
    "anderson": "Dr. Lisa Anderson",
    "patel": "Dr. Kevin Patel",
    "foster": "Dr. Amanda Foster"
}

PROVIDER_MA_MAP = {
    "Dr. Sheldon Stroman": "Lisa",
    "Dr. Sarah Mitchell": "Karen",
    "Dr. Robert Chen": "Diana",
    "Dr. Maria Rodriguez": "Carmen",
    "Dr. David Thompson": "Brian",
    "Dr. Jennifer Park": "Susan",
    "Dr. Michael Brooks": "Tony",
    "Dr. Lisa Anderson": "Nicole",
    "Dr. Kevin Patel": "Priya",
    "Dr. Amanda Foster": "Rachel"
}

MA_NAME_TO_PROVIDER = {
    "lisa": "Dr. Sheldon Stroman",
    "karen": "Dr. Sarah Mitchell",
    "diana": "Dr. Robert Chen",
    "carmen": "Dr. Maria Rodriguez",
    "brian": "Dr. David Thompson",
    "susan": "Dr. Jennifer Park",
    "tony": "Dr. Michael Brooks",
    "nicole": "Dr. Lisa Anderson",
    "priya": "Dr. Kevin Patel",
    "rachel": "Dr. Amanda Foster"
}

SCHEDULE_1 = ["oxycodone", "percocet", "morphine", "fentanyl"]
SCHEDULE_2 = ["vyvanse", "adderall", "focalin", "sunosi"]
SCHEDULE_3 = [
    "ativan", "lorazepam",
    "xanax", "xanex", "zanax", "zanex", "zannex", "xannex",
    "alprazolam", "klonopin", "clonazepam",
    "valium", "diazepam",
    "ambien", "ambien cr", "zolpidem",
    "lunesta", "eszopiclone", "belsomra",
    "temazepam", "restoril", "promethazine",
    "cheratussin", "lyrica", "pregabalin",
    "soma", "carisoprodol", "viberzi",
    "zannie", "xanny", "xannie"
]

LAB_WORK_TRIGGERS = [
    "lab work", "lab test", "blood work", "blood test",
    "call in a script", "order a blood", "i need a cbc",
    "cbc", "call in an order", "lab order", "order labs",
    "order a lab", "need labs", "get labs", "draw blood",
    "lipid panel", "metabolic panel", "urinalysis", "urine test",
    "thyroid test", "a1c", "glucose test", "cholesterol test",
    "complete blood count", "comprehensive metabolic",
    "basic metabolic", "order a test", "run some labs",
    "run labs", "get some blood work", "get my blood drawn"
]

# Nurse-visit triggers: services that are always performed in the office
# and are always scheduled by the medical assistant, never by Steve and
# never virtually (injections/B12 shots, flu shots, vaccines, etc.).
# Deliberately EXCLUDES "urinalysis", "blood draw", and "x-ray"/"lab
# work" style service words: those already have their own routing via
# LAB_WORK_TRIGGERS / the generic appointment path, so triggering here
# would break those existing workflows. Detection is scoped to explicit
# nurse-visit requests and injection/shots that have no other dedicated
# handler.
NURSE_VISIT_TRIGGERS = [
    "nurse visit", "nurse appointment", "nurse's visit",
    "b12 shot", "b12", "vitamin b12", "vitamin b-12",
    "flu shot", "flu vaccine", "flu vaccination",
    "vaccine", "vaccination", "immunization", "immunisation",
    "injection", "cortisone shot", "depo shot", "testosterone shot",
]

SAME_DAY_KEYWORDS = [
    "today", "same day", "as soon as possible",
    "this morning", "this afternoon", "asap"
]
SAME_DAY_ACTION_WORDS = [
    "appointment", "come in", "be seen", "schedule", "slot",
    "get in", "anything", "available", "availability",
    "opening", "openings", "fit me in", "fit in", "see",
]

LAB_ORDER_PICKUP_TRIGGERS = [
    "pick up", "pick them up", "pick up my labs",
    "pick up the labs", "pick up lab orders",
    "pick up my orders", "pick up the orders",
    "come pick up", "coming to pick up",
    "stop by and pick up", "swing by and pick",
    "going to quest", "going to labcorp",
    "going to the lab", "getting my labs done",
    "print out", "print my labs", "print the labs",
    "print out my orders", "print the orders",
    "leave at the front", "leave them at the front",
    "front desk", "leave at front desk"
]

URGENT_SYMPTOM_TRIGGERS = [
    "chest pain", "heart attack", "can't breathe", "cannot breathe",
    "difficulty breathing", "shortness of breath", "stroke",
    "unconscious", "not responding", "passing out", "passed out",
    "seizure", "worst headache of my life",
    "coughing up blood", "vomiting blood", "severe bleeding",
    "throat closing", "can't swallow", "cannot swallow",
    "severe allergic reaction", "anaphylaxis",
    "severe dizziness", "severe nausea", "severe vomiting",
    "having a heart attack", "think i'm having",
    "think she's having", "think he's having"
]

PAST_TENSE_INJURY_PHRASES = [
    "last night", "yesterday", "this morning", "earlier today",
    "earlier", "a few days ago", "last week", "recently",
    "twisted", "sprained", "fell", "fallen", "hurt myself",
    "injured", "accident", "i think i might have",
    "i think i may have", "might be fractured", "may be fractured",
    "might be broken", "may be broken"
]

CANNOT_WAIT_PHRASES = [
    "can't wait", "cannot wait", "need help now", "needs help now",
    "immediately", "no it cannot", "no it can't",
    "no she can't", "no he can't", "no i can't", "no we can't",
    "need assistance right away", "needs assistance right away",
    "it cannot wait", "it can't wait", "help now", "help right now",
    "urgent", "emergency", "immediate assistance", "need immediate",
    "right away",
]

CAN_WAIT_PHRASES = [
    "it can wait", "can wait", "yes it can",
    "she can wait", "he can wait", "i can wait",
    "we can wait", "not urgent", "take your time",
    "no rush", "whenever", "yes", "sure", "okay", "ok"
]

PATIENT_NOT_PRESENT_PHRASES = [
    "she is not with me", "he is not with me",
    "she's not with me", "he's not with me",
    "she is not here", "he is not here",
    "she's not here", "he's not here",
    "not available", "running errands",
    "at work", "she is busy", "he is busy",
    "she's busy", "he's busy", "not home", "out right now",
    "i will let her know", "i will let him know",
    "i'll let her know", "i'll let him know",
    "she cannot come", "he cannot come",
    "she can't come", "he can't come"
]

PATIENT_WILL_CALL_BACK_PHRASES = [
    "she will call", "he will call",
    "she'll call", "he'll call",
    "will call you", "will call back",
    "she'll call later", "he'll call later",
    "she will call later", "he will call later",
    "she will call you later", "he will call you later",
    "i will have her call", "i will have him call",
    "i'll have her call", "i'll have him call",
    "she'll reach out", "he'll reach out",
    "she will reach out", "he will reach out"
]

PATIENT_PRESENT_PHRASES = [
    "she is here", "he is here", "she's here", "he's here",
    "right here", "right here with me",
    "i'll put her on", "i'll put him on",
    "putting her on", "putting him on",
    "here she is", "here he is",
    "she's right here", "he's right here",
    "i will put her on", "i will put him on",
    "hold on", "one moment",
    "she is with me", "he is with me",
    "she's with me", "he's with me"
]

THIRD_PARTY_PHRASES = [
    "calling on behalf", "calling for my", "calling for",
    "on behalf of", "i am calling for", "i'm calling for",
    "calling about my", "this is for my",
    "my mother", "my father", "my wife", "my husband",
    "my son", "my daughter", "my sister", "my brother",
    "my grandmother", "my grandfather", "my parent",
    "my spouse", "my partner", "my friend",
    "my relative", "caregiver", "power of attorney",
    "for my", "behalf of"
]

# Bug fix: THIRD_PARTY_PHRASES fires on a bare mention like "my
# husband" anywhere in the message, so a caller speaking in first-
# person-plural about a SHARED situation ("my husband and I both have
# new patient appointments", "we both have appointments with Dr.
# Brooks") was misread as a third-party call requiring the spouse's
# own verbal consent to discuss THEIR information - even though the
# caller never asked Steve to look up or disclose anything specific
# to the spouse. A genuine third-party call ("calling on behalf of my
# husband", "can you check my husband's appointment?") has no such
# joint "and I have/are" phrasing and is unaffected by this exclusion.
_JOINT_APPOINTMENT_REFERENCE_PATTERN = re.compile(
    r"\bmy\s+(?:husband|wife|spouse|son|daughter|partner)\b"
    r"(?:\s+\w+){0,3}\s+and\s+(?:i|myself)\s+(?:both\s+)?"
    r"(?:have|has|are|am|would\s+like\s+to|want\s+to|need|need\s+to)\b"
    r"|\bwe\s+both\s+have\b"
    r"|\bwe\s+are\s+(?:both\s+)?(?:already\s+)?scheduled\b"
    # Bug fix: "my spouse and myself would like to cancel our new
    # patient appointments" still fired the third-party consent
    # workflow, since the pattern above only recognized "have/has/
    # are/am" as the joint verb - not the "would like to"/"want to"/
    # "need to" + action-verb phrasing a caller naturally uses when
    # asking to cancel or reschedule on behalf of both of them.
    r"|\bwe\s+(?:would\s+like\s+to|want\s+to|need\s+to)\b",
    re.IGNORECASE
)

# Bug fix: the virtual-visit wait phrasing "have/had been waiting for
# my pcp/doctor/provider to join" contains "for my", which is a
# THIRD_PARTY_PHRASES entry - so the patient's own wait complaint was
# misrouted into the third-party HIPAA consent workflow ("Is the
# patient available right now so I can ask for their consent?") and the
# wait support flow never ran. Waiting "for my <provider>" (the
# caller's OWN provider, phrased in first person) is unambiguously the
# patient speaking for themself, never a third party. Same symmetric
# exclusion shape as _JOINT_APPOINTMENT_REFERENCE_PATTERN above: allows
# caller_is_patient=True inference and suppresses third-party detection.
_VIRTUAL_WAIT_SELF_PATTERN = re.compile(
    r"\b(?:been\s+|i(?:'m| am| have|'ve| have\s+been)\s+)?"
    r"wait(?:ing|ed)?\s+(?:for|on)\s+my\s+"
    r"(?:pcp|provider|physician|doctor|dr\.?)",
    re.IGNORECASE
)

MEDICAL_PROFESSIONAL_KEYWORDS = [
    "this is the pharmacy", "pharmacy calling", "pharmacist calling",
    "i'm a pharmacist", "i am a pharmacist", "this is the pharmacist",
    "this is the hospital", "hospital calling", "calling from the hospital",
    "this is the clinic", "clinic calling", "calling from the clinic",
    "medical center", "health center", "home health",
    "nursing home", "assisted living", "rehabilitation",
    "specialist", "orthopedic", "cardiology", "neurology",
    "dermatology", "urology", "oncology", "radiology",
    "gastroenterology", "endocrinology", "pulmonology",
    "rheumatology", "nephrology", "hematology",
    "ophthalmology", "otolaryngology", "psychiatry",
    "calling from", "i'm calling from", "we are calling from",
    "this is the office of", "calling on behalf of dr",
    "this is dr.", "calling from dr.",
    "doctor's office", "dr.'s office",
    "physician", "practitioner",
    "office manager", "referral coordinator",
]

NURSE_MA_REQUEST_PHRASES = [
    "speak to the nurse", "speak to my nurse",
    "talk to the nurse", "talk to my nurse",
    "nurse please", "the nurse please",
    "can i speak to the nurse", "can i talk to the nurse",
    "i'd like to speak to the nurse",
    "i would like to speak to the nurse",
    "speak to someone in the back",
    "talk to someone in the back",
    "speak with the nurse", "talk with the nurse",
    "speak to a nurse", "talk to a nurse",
    "get the nurse", "get my nurse",
    "speak to the ma", "speak to my ma",
    "talk to the ma", "talk to my ma",
    "speak to the medical assistant",
    "talk to the medical assistant",
    "i'd like to speak to the medical assistant",
    "i would like to speak to the medical assistant"
]

LAB_RESULT_INQUIRY_TRIGGERS = [
    "are the results in", "are my results in", "did my results come in",
    "did the results come in", "have my results come in",
    "results come back", "results came back", "results in my chart",
    "fax over the results", "faxed over the results",
    "sent over the results", "results been received",
    "results in yet", "results ready", "lab results in",
    "blood work results", "blood test results", "test results in",
    "lab results", "get his results", "get her results",
    "get my results", "get the results", "recent lab results",
    "recent results"
]

LAB_RESULT_FAX_TO_OUTSIDE_TRIGGERS = [
    "fax my lab results", "fax the lab results",
    "fax results to", "send results to", "send my results to",
    "fax my results to", "send lab results to",
    "forward my results", "forward lab results"
]

LAB_ORDER_FAX_TO_FACILITY_TRIGGERS = [
    "fax my lab orders to", "fax the lab orders to",
    "fax my orders to", "fax the orders to",
    "fax lab orders to", "fax orders to",
    "fax my labs to", "fax the labs to",
    "fax them to", "fax it to",
    "fax my lab order to", "fax the lab order to"
]

# Street address patterns - used to detect when patient is giving a location
ADDRESS_INDICATORS = [
    "blvd", "boulevard", "street", "avenue", "ave",
    "road", "lane", "court", "circle", "terrace",
    "highway", "hwy", "suite", "ste", "floor", "building",
    " st ", " rd ", " ln ", " ct ", " pl ",
    "apt ", "apt.", "unit ", "po box", "p.o. box"
]

RESPONSE_TIME_PHRASES = [
    "72 business hours", "24 business hours",
    "allow 72", "allow 24"
]

OFFICE_HOURS_PHRASES = [
    "monday through friday", "9:00 am to 5:00 pm",
    "office hours are", "our hours are",
    "we are open", "open monday"
]

OFFICE_HOURS = "Monday through Friday, 9:00 AM to 5:00 PM"


# ─────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────

def get_harness_override(env_key, mapping=None):
    """
    Checks for an environment variable override.
    Returns the mapped value or the raw value if no mapping is provided.
    Returns None if the environment variable is not set.
    """
    val = os.environ.get(env_key)
    if val is None:
        return None

    if mapping:
        return mapping.get(val.lower(), val)

    # Default boolean parsing if no mapping is provided
    if val.lower() in ["true", "1", "yes"]:
        return True
    if val.lower() in ["false", "0", "no"]:
        return False

    return val


def is_medical_professional_message(message, message_lower):
    if any(phrase in message_lower for phrase in NURSE_MA_REQUEST_PHRASES):
        return False
    if any(phrase in message_lower for phrase in MEDICAL_PROFESSIONAL_KEYWORDS):
        return True
    office_pattern = re.search(
        r"from\s+dr\.?\s+\w+'s\s+office|"
        r"from\s+\w+'s\s+office|"
        r"\w+\s+office\s+calling|"
        r"calling\s+from.*'s\s+office",
        message_lower
    )
    if office_pattern:
        return True
    from_pattern = re.search(
        r"(?:this is|i am|i'm)\s+\w+\s+from\s+(?:dr\.?\s+)?\w+",
        message_lower
    )
    if from_pattern:
        return True
    return False


def is_truly_urgent(message_lower):
    has_urgent = any(
        trigger in message_lower for trigger in URGENT_SYMPTOM_TRIGGERS
    )
    if not has_urgent:
        return False
    has_past_tense = any(
        phrase in message_lower for phrase in PAST_TENSE_INJURY_PHRASES
    )
    is_appointment_request = any(
        word in message_lower for word in
        ["appointment", "schedule", "come in", "be seen", "slot"]
    )
    if is_appointment_request:
        return False
    if has_past_tense:
        return False
    return True


# ─────────────────────────────────────────────
# Office holiday rules
# ─────────────────────────────────────────────
# Applies to appointment availability, office-hours determination,
# same-day evaluation, callback workflows, and "is the office open"
# questions. A recognized holiday closure is treated exactly like a
# weekend closure everywhere in this file - both are folded into the
# is_office_open_today()/is_within_office_hours() primitives that
# almost everything else already consults, rather than each caller
# separately checking for holidays.

_MONTH_LENGTHS = {
    1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 30, 9: 30, 10: 31, 11: 30, 12: 31,
}


def _is_leap_year(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_in_month(year, month):
    if month == 2 and _is_leap_year(year):
        return 29
    return _MONTH_LENGTHS[month]


def _nth_weekday_of_month(year, month, weekday, n):
    """weekday: Monday=0 ... Sunday=6. n: 1-based occurrence (e.g. 3rd
    Monday)."""
    first_weekday = datetime(year, month, 1).weekday()
    delta_days = (weekday - first_weekday) % 7
    day = 1 + delta_days + (n - 1) * 7
    return datetime(year, month, day).date()


def _last_weekday_of_month(year, month, weekday):
    last_day_num = _days_in_month(year, month)
    last_weekday = datetime(year, month, last_day_num).weekday()
    delta_days = (last_weekday - weekday) % 7
    day = last_day_num - delta_days
    return datetime(year, month, day).date()


def _observed_fixed_holiday(year, month, day):
    """Weekend-observance shift used for New Year's Day, Independence
    Day, and Christmas Day: falls on Saturday -> observed the day
    before; falls on Sunday -> observed the day after."""
    d = datetime(year, month, day).date()
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _office_holidays_for_year(year):
    """Returns {date: holiday_name} for the 7 recognized office
    holidays in a given year, with weekend-observance shifting
    already applied where it's part of that holiday's rule."""
    return {
        _observed_fixed_holiday(year, 1, 1): "New Year's Day",
        _nth_weekday_of_month(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _last_weekday_of_month(year, 5, 0): "Memorial Day",
        _observed_fixed_holiday(year, 7, 4): "Independence Day",
        _nth_weekday_of_month(year, 9, 0, 1): "Labor Day",
        _nth_weekday_of_month(year, 11, 3, 4): "Thanksgiving Day",
        _observed_fixed_holiday(year, 12, 25): "Christmas Day",
    }


def _office_holidays_near(year):
    """Merges the holiday maps for year-1, year, and year+1 - needed
    because New Year's Day's Saturday-shift can land the observed
    closure on December 31 of the PREVIOUS year (e.g. Jan 1, 2028 is
    a Saturday, so Dec 31, 2027 is the recognized closure date)."""
    merged = {}
    for y in (year - 1, year, year + 1):
        merged.update(_office_holidays_for_year(y))
    return merged


def is_recognized_office_holiday(check_date):
    """check_date: a datetime.date. True if it falls on one of the 7
    recognized office holidays, after weekend-observance shifting."""
    return check_date in _office_holidays_near(check_date.year)


def get_recognized_office_holiday_name(check_date):
    """Returns the holiday name if check_date is a recognized office
    holiday, else None."""
    return _office_holidays_near(check_date.year).get(check_date)


def is_office_open_today():
    today = datetime.now().date()
    if is_recognized_office_holiday(today):
        return False
    return datetime.now().weekday() < 5


def is_within_office_hours():
    """True only if it is currently within actual office hours: Monday
    through Friday, 9:00 AM to 5:00 PM, and not a recognized office
    holiday. Distinct from is_office_open_today(), which only checks
    the day (weekend/holiday) and ignores time of day entirely - that
    gap is why a 5:17 AM weekday call was treated as if the office
    were open."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    if is_recognized_office_holiday(now.date()):
        return False
    return 9 <= now.hour < 17


# ─────────────────────────────────────────────
# Generic (non-PHF) appointment persistence
# ─────────────────────────────────────────────
# Completely independent of Sprint13's PHF appointment persistence
# (phf_appointment_records / store_phf_appointment_record() /
# get_stored_appointment_record() / _patient_record_key() in
# Sprint13.py). None of that PHF code is read, written, or imported
# here - separate file, separate in-memory dict, separate key-builder,
# so a patient can have both a PHF appointment and a generic
# appointment on file at the same time with zero collision risk.
#
# Unlike PHF (a deterministic Python state machine with clear
# completion points), generic scheduling is driven entirely by the AI
# via system_prompt's "APPOINTMENT SCHEDULING - FUTURE" instructions -
# there is no Python-side confirmation step to hook into. Capture here
# is therefore necessarily best-effort: it scans the AI's own generated
# response text for a day name + time alongside confirmation language,
# after the response is generated, and persists what it finds. This is
# less reliable than PHF's deterministic capture, but it is the only
# capture point available given generic scheduling's architecture.
GENERIC_APPOINTMENT_RECORDS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "generic_appointment_records.json"
)


def _load_generic_appointment_records():
    if os.path.exists(GENERIC_APPOINTMENT_RECORDS_FILE):
        try:
            with open(GENERIC_APPOINTMENT_RECORDS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_generic_appointment_records():
    try:
        with open(GENERIC_APPOINTMENT_RECORDS_FILE, "w") as f:
            json.dump(generic_appointment_records, f)
    except OSError:
        pass


generic_appointment_records = _load_generic_appointment_records()


def _generic_patient_record_key(first_name, last_name):
    """Build a lookup key for the generic appointment record store.
    Deliberately the same firstname_lastname shape as Sprint13's
    _patient_record_key(), but this is an entirely separate function
    operating on an entirely separate dict/file - no shared state."""
    if not first_name or not last_name:
        return None
    return f"{first_name.strip().lower()}_{last_name.strip().lower()}"


def store_generic_appointment_record(first_name, last_name, day_time_text, provider=None, reason=None):
    """Persist a confirmed generic appointment's day/time (and provider,
    if known) under the patient's identity."""
    key = _generic_patient_record_key(first_name, last_name)
    if not key:
        return
    generic_appointment_records[key] = {
        "appointment_day": day_time_text,
        "provider": provider,
        "reason": reason,
    }
    _save_generic_appointment_records()


def get_stored_generic_appointment_record(first_name, last_name):
    """Look up a previously-persisted generic appointment for this
    patient, if one exists. Re-reads from disk first so a record
    written by a different process is found, matching the same pattern
    Sprint13's PHF store uses for its own reads."""
    key = _generic_patient_record_key(first_name, last_name)
    if not key:
        return None
    if key not in generic_appointment_records:
        generic_appointment_records.update(_load_generic_appointment_records())
    return generic_appointment_records.get(key)


def cancel_generic_appointment_record(first_name, last_name):
    """Remove a persisted generic appointment record for this patient,
    if one exists. Returns True if a record was found and removed,
    False otherwise. Mirrors get_stored_generic_appointment_record's
    key logic and disk re-read pattern exactly, so a record written by
    a different process is still found before concluding there is
    nothing to cancel."""
    key = _generic_patient_record_key(first_name, last_name)
    if not key:
        return False
    if key not in generic_appointment_records:
        generic_appointment_records.update(_load_generic_appointment_records())
    if key in generic_appointment_records:
        del generic_appointment_records[key]
        _save_generic_appointment_records()
        return True
    return False


# Prefixes to strip from a patient's raw request message when deriving
# the appointment reason, so the stored reason reads as a clean
# noun-phrase ("3 month follow up" rather than "I would like to
# schedule a 3 month follow up"). Ordered longest-first at use time via
# sorted(..., key=len, reverse=True) so "i need an" wins over "i need a"
# (which is a string prefix of "i need an..."), or the trailing "n"
# survives the strip.
_REASON_PREFIXES = (
    "i would like to schedule a",
    "i would like to schedule an",
    "i'd like to schedule a",
    "i'd like to schedule an",
    "i'd like to schedule an appointment for",
    "i'd like to book a",
    "i'd like to book an",
    "i'd like an appointment for",
    "i'd like an appointment",
    "i'd like to set up an appointment for",
    "i'd like to set up a",
    "i'd like to set up an",
    "i'd like to make an appointment for",
    "i'd like to make a",
    "i'd like to make an",
    "i'd like a",
    "i'd like an",
    "i'd like to",
    "i would like to book a",
    "i would like to book an",
    "i would like to make an appointment for",
    "i would like to make an appointment",
    "i would like to make a",
    "i would like to make an",
    "i would like to set up an appointment for",
    "i would like to set up an appointment",
    "i would like to set up a",
    "i would like to set up an",
    "i would like an appointment for",
    "i would like an appointment",
    "i would like a",
    "i would like an",
    "i want to schedule a",
    "i want to schedule an",
    "i want to book a",
    "i want to book an",
    "i need to schedule an appointment for a",
    "i need to schedule an appointment for",
    "i need to schedule a",
    "i need to schedule an",
    "i need an appointment for",
    "i need an appointment",
    "i need a",
    "i need an",
    "i want a",
    "i want an",
    "can i get an appointment for a",
    "can i get an appointment for",
    "can i get a",
    "can i get an",
)


def _trim_reason_prefix(text):
    """Best-effort cleanup of a patient's raw request message into a
    reason noun-phrase, so the stored reason reads naturally when Steve
    echoes it back ("3 month follow up" rather than "I would like to
    schedule a 3 month follow up")."""
    t = text.strip().strip(".,!?")
    t_lower = t.lower()
    for prefix in sorted(_REASON_PREFIXES, key=len, reverse=True):
        if t_lower.startswith(prefix):
            t = t[len(prefix):].strip().strip(".,!?")
            break
    if t.lower().startswith("appointment for "):
        t = t[len("appointment for "):].strip()
    return t


def _is_appointment_transaction_turn(text):
    """True for user turns that are NOT a reason-for-visit statement:
    appointment inquiry ("when is my next appointment"), reschedule,
    cancel, or a slot-selection confirmation. Such turns must never be
    stored as the appointment's reason."""
    text_lower = text.lower()
    if detect_generic_appointment_inquiry_intent(text_lower):
        return True
    if any(kw in text_lower for kw in (
            "reschedule", "cancel", "move my", "move it",
    )):
        return True
    if (
            any(day in text_lower for day in _GENERIC_APPT_DAY_NAMES)
            and _find_time_in_text(text)
    ):
        return True
    return False


def _derive_generic_appointment_reason():
    """Best-effort capture of the reason for a just-booked generic
    appointment. Prefers deterministic flow state (three/six-month
    follow-up, wellness visit types), then the patient's own stated
    reason: first the bare answer immediately following Steve's
    "What is the reason for this appointment?" prompt (these answers
    often carry no scheduling keyword at all, e.g. "I've been having
    knee pain"), then the earliest message that stated the visit
    intent. Inquiry / reschedule / cancel / slot-selection turns are
    always excluded so they can never be stored as the reason."""
    if new_patient_flow_active:
        # New-patient registrations are always booked as a new-patient
        # visit - never as a verbatim replay of the caller's opening
        # message ("I'm not a patient yet. This is Janet Jackson dob
        # 5/15/1965 I want to set up new patient appointments..." ).
        return "New patient appointment"
    if individual_three_month_followup_active:
        return "3-month follow-up"
    if individual_six_month_followup_active:
        return "6-month follow-up"
    if Sprint14.wellness_flow_active and Sprint14.wellness_requested_visit_type:
        label = {
            "wellness": "annual wellness visit",
            "maw": "Medicare Annual Wellness visit",
            "cha": "Comprehensive Health Assessment",
        }.get(
            Sprint14.wellness_requested_visit_type, "wellness visit"
        )
        return label
    # Pass 1: the patient's bare reason given right after Steve asked
    # for it ("What is the reason for this appointment?").
    for i, turn in enumerate(conversation_history):
        if turn.get("role") != "assistant":
            continue
        a_lower = turn.get("content", "").lower()
        if "reason" in a_lower and "appointment" in a_lower and "?" in a_lower:
            for nxt in conversation_history[i + 1:]:
                if nxt.get("role") != "user":
                    continue
                ut = nxt.get("content", "").strip()
                if _is_appointment_transaction_turn(ut):
                    break
                cleaned = _trim_reason_prefix(ut)
                if cleaned:
                    return _condense_appointment_reason(cleaned)
            break
    # Pass 2: the earliest message that stated the visit intent,
    # skipping inquiry / reschedule / cancel / booking-selection turns.
    for turn in conversation_history:
        if turn.get("role") != "user":
            continue
        content = turn.get("content", "").strip()
        text_lower = content.lower()
        if _is_appointment_transaction_turn(content):
            continue
        if any(kw in text_lower for kw in (
                "appointment", "schedule", "book",
                "checkup", "physical", "follow", "recheck",
                "visit",
        )):
            cleaned = _trim_reason_prefix(content)
            if cleaned:
                return _condense_appointment_reason(cleaned)
    return None


# Deterministic condensation of a captured appointment reason so the
# persisted record reads as a concise phrase instead of a verbatim
# transcription of what the caller typed (e.g. "I want to discuss
# possibly increasing how much adderall I should take" ->
# "Discuss increasing dosage of Adderall"). Applied by
# _derive_generic_appointment_reason() to every reason derived from
# free-form caller text. Deliberately conservative: only well-bounded
# phrasings are rewritten, everything else is left unchanged.
_DOSAGE_INCREASE_RULES = (
    # "... discuss ... increasing ... how much <med> ..."
    # "i want to discuss possibly increasing how much adderall i should
    # take" -> "Discuss increasing dosage of Adderall"
    (
        re.compile(
            r"\bdiscuss\b.*?\bincreas(?:e|es|ed|ing)\b.*?\bhow much\b\s+"
            r"([a-z][a-z'-]*)\b",
            re.IGNORECASE
        ),
        "Discuss increasing dosage of",
    ),
    # "... discuss ... increasing ... (my|the) (dosage|dose|amount) of <med>"
    # "i want to discuss increasing my dosage of Lorazepam" ->
    # "Discuss increasing dosage of Lorazepam"
    (
        re.compile(
            r"\bdiscuss\b.*?\bincreas(?:e|es|ed|ing)\b[^.!?]*?\b"
            r"(?:dosage|dose|amount)\s+of\s+([a-z][a-z'\s-]*?)[.,!?]?\s*$",
            re.IGNORECASE
        ),
        "Discuss increasing dosage of",
    ),
    # "I want to increase ... (my|the) dosage of <med>" (no "discuss")
    # - requires a first-person subject so it never re-matches the
    # imperative output above ("Discuss increasing dosage of X").
    (
        re.compile(
            r"\bi\b.*?\bincreas(?:e|es|ed|ing)\b.*?\b"
            r"(?:dosage|dose|amount)\s+of\s+([a-z][a-z'\s-]*?)[.,!?]?\s*$",
            re.IGNORECASE
        ),
        "Increase dosage of",
    ),
)


def _condense_appointment_reason(text):
    """Shorten a free-form appointment-reason fragment into a concise,
    reason-like phrase. Only well-bounded phrasings are rewritten;
    anything unrecognized is returned unchanged."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    # Late-arrival relay messages ("Can you tell <name> that I'm going
    # to be late for my appointment today") are not a visit reason.
    if "be late for my appointment" in low or "running late for my appointment" in low:
        return "Running late for appointment"
    for pattern, label in _DOSAGE_INCREASE_RULES:
        match = pattern.search(t)
        if match and match.lastindex:
            med = match.group(match.lastindex).strip().capitalize()
            return f"{label} {med}"
    return t


_GENERIC_APPT_DAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
]
_GENERIC_APPT_CONFIRM_KEYWORDS = [
    "scheduled", "confirmed", "booked", "got you down",
    "appointment is set", "you're all set", "all set for",
    "squared away", "all squared away", "you're set", "set you up",
    "set for you", "taken care of", "locked in", "have you down",
    "we have you down", "put you down for",
]

# Availability-offer markers: when the AI is presenting a menu of open
# slots (rather than confirming a single booking) the text is NOT a
# confirmation, even though it can contain words like "scheduled"
# alongside a weekday and a time. Capturing such a listing as a
# "confirmed" appointment would store a phantom record and prematurely
# close flows - e.g. the six-month follow-up eligibility message offers
# "scheduled" availability, and the false "Monday @ 9:00 AM" capture
# deactivated individual_six_month_followup_active before the
# deterministic spouse scheduler could engage.
_GENERIC_APPT_AVAILABILITY_OFFER_PATTERN = re.compile(
    r"which\s+of\s+these|"
    r"which\s+(?:day|time|one|date|option)\s+(?:works|work|would|is\s+best)|"
    r"no\s+availability\b|"
    r"here(?:'s| is)?\s+(?:your\s+|our\s+|the\s+)?availability|"
    r"available\s+(?:times|slots|days?)\b|"
    r"(?:times?|slots?|days?)\s+(?:are|is)\s+available",
    re.IGNORECASE,
)

_MONTH_NAME_TO_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

# Title abbreviations whose period does not end a sentence - used by
# _find_real_sentence_end() below so a name like "Dr. Michael Brooks"
# isn't mistaken for the end of the confirmation sentence.
_TITLE_ABBREVIATIONS = ("dr", "mr", "mrs", "ms", "st", "jr", "sr")


def _find_real_sentence_end(text, start):
    """Find the end of the sentence beginning at/after `start`,
    skipping a period that is actually a title abbreviation (e.g.
    "Dr.") rather than a genuine sentence end. Returns an index into
    `text`, or None if no sentence-ending punctuation is found."""
    pos = start
    while True:
        match = re.search(r"[.!\n]", text[pos:])
        if not match:
            return None
        end = pos + match.end()
        preceding_word = re.search(
            r"([A-Za-z]+)$", text[pos:pos + match.start()]
        )
        if (
                preceding_word
                and preceding_word.group(1).lower() in _TITLE_ABBREVIATIONS
        ):
            pos = end
            continue
        return end


def _trim_to_complete_sentence(text):
    """Trim a (possibly truncated) reply back to its last complete
    sentence so it never ends mid-fragment. Keeps any sentence that
    ends in . ! ? or a newline; skips title abbreviations like "Dr." so
    the last sentence boundary is a genuine one. Returns the trimmed
    text unchanged if no sentence boundary is found."""
    if not text:
        return text
    last_end = None
    pos = 0
    while True:
        match = re.search(r"[.!\n]", text[pos:])
        if not match:
            break
        end = pos + match.end()
        preceding_word = re.search(
            r"([A-Za-z]+)$", text[pos:pos + match.start()]
        )
        if (
                preceding_word
                and preceding_word.group(1).lower() in _TITLE_ABBREVIATIONS
        ):
            pos = end
            continue
        last_end = end
        pos = end
    if last_end is None:
        return text
    return text[:last_end].rstrip()


def _extract_generic_appointment_confirmation(assistant_text):
    """Best-effort extraction of a confirmed generic appointment's
    day/time from the AI's own free-form response text. Requires both
    a day name AND a time AND at least one confirmation-indicating
    phrase, to avoid false-positives on messages that merely mention a
    day/time in passing (e.g. listing availability options, not yet
    confirming one)."""
    text_lower = assistant_text.lower()
    if _GENERIC_APPT_AVAILABILITY_OFFER_PATTERN.search(text_lower):
        return None
    distinct_days = sum(
        1 for day in _GENERIC_APPT_DAY_NAMES if day in text_lower
    )
    if distinct_days >= 2:
        return None
    if not any(day in text_lower for day in _GENERIC_APPT_DAY_NAMES):
        return None
    if not any(kw in text_lower for kw in _GENERIC_APPT_CONFIRM_KEYWORDS):
        return None
    day_match = re.search(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        assistant_text, re.IGNORECASE
    )
    time_match = re.search(
        r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
        assistant_text, re.IGNORECASE
    )
    if day_match and time_match:
        return f"{day_match.group(1).capitalize()} @ {time_match.group(1).upper()}"
    return None


def _extract_day_time_from_reply(message):
    """Extract just a weekday name + time from a raw PATIENT reply
    (e.g. "Please reschedule it for wednesday at 930AM"), instead of
    persisting the entire sentence verbatim. Unlike
    _extract_generic_appointment_confirmation() above (which scans the
    AI's own reply and can rely on the system's consistent "H:MM AM/PM"
    time formatting), patient input is unpredictable - this uses a
    more permissive time regex that also handles a compact form with
    no colon (e.g. "930AM" as 9:30 AM, not "30 AM" with the leading
    digit dropped). Returns None if no day or no time is found, so the
    caller can fall back to the raw message rather than silently
    losing the reschedule on an unparseable reply."""
    day_match = re.search(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        message, re.IGNORECASE
    )
    time_match = re.search(
        r"\b(\d{1,2})(?::?(\d{2}))?\s*(am|pm)\b",
        message, re.IGNORECASE
    )
    if not day_match or not time_match:
        return None
    day_name = day_match.group(1).capitalize()
    hour = time_match.group(1)
    minutes = time_match.group(2) or "00"
    meridiem = time_match.group(3).upper()
    return f"{day_name} @ {hour}:{minutes} {meridiem}"


GENERIC_APPOINTMENT_INQUIRY_TRIGGERS = [
    "when is my next appointment", "when is my appointment",
    "do i have an appointment scheduled", "do i have an appointment",
    "when am i scheduled to see", "when am i scheduled",
    "what time is my appointment", "check my appointment",
]


def detect_generic_appointment_inquiry_intent(message_lower):
    return any(t in message_lower for t in GENERIC_APPOINTMENT_INQUIRY_TRIGGERS)


# ─────────────────────────────────────────
# Fasting-lab appointment inquiry
# ─────────────────────────────────────────
# "Are my labs fasting labs?" - whether an upcoming appointment's labs
# must be fasting depends on the appointment type:
#   RULE 1  wellness visits and diabetes-management appointments
#           (incl. diabetes-medication refills / dosage increases)
#           -> fasting. ALWAYS.
#   RULE 2  follow-up appointments (1/3/6-month, medication, chronic
#           condition) -> 75% fasting / 25% not.
#   RULE 3  every other appointment type (sports physical, Form 1823,
#           medication refill, virtual visit, misc) -> 50/50.
# The answer is generated fresh at inquiry time and never persisted.
# If the appointment type cannot be determined, the message falls
# through to the existing (LLM-driven) behavior unchanged.
FASTING_LAB_INQUIRY_TRIGGERS = [
    "fasting labs", "fasting lab",
    "fasting blood work", "fasting blood test", "fasting blood draw",
    "fast before my labs", "fast before the labs",
    "fast before my lab work", "fast before the lab work",
    "fast before the lab", "fast for the labs",
    "labs need to be fasting", "lab needs to be fasting",
    "labs need to be fasted", "do these labs require fasting",
    "does this lab require fasting",
]

_WORD_BOUNDARY_FAST_RE = re.compile(r"\bfast(?:ing)?\b", re.IGNORECASE)

# Diabetes-management markers (medications commonly used for diabetes).
DIABETES_MEDICATION_WORDS = [
    "glucophage", "metformin", "ozempic", "mounjaro", "januvia",
    "victoza", "trulicity", "lantus", "levemir", "tresiba", "toujeo",
    "humalog", "novolog", "glyburide", "glipizide", "pioglitazone",
    "actos", "farxiga", "jardiance", "invokana", "insulin",
]


def detect_fasting_lab_inquiry(message_lower):
    """True when the patient is asking whether their pre-appointment
    labs must be fasting labs."""
    if any(t in message_lower for t in FASTING_LAB_INQUIRY_TRIGGERS):
        return True
    if _WORD_BOUNDARY_FAST_RE.search(message_lower) and "lab" in message_lower:
        return True
    return False


def _fasting_lab_is_wellness_or_diabetes(text_lower):
    """RULE 1: wellness visits and diabetes-management appointments
    (including diabetes-medication refills / dosage increases) are
    always fasting."""
    if any(p in text_lower for p in (
            "annual wellness visit", "medicare annual wellness",
            "comprehensive health assessment", "wellness",
    )):
        return True
    if "diabetes" in text_lower:
        return True
    if any(d in text_lower for d in DIABETES_MEDICATION_WORDS):
        return True
    return False


def _fasting_lab_is_follow_up(text_lower):
    """RULE 2: follow-up appointments."""
    return "follow" in text_lower


def generate_fasting_lab_answer(appointment_reason):
    """Classify the appointment reason and pick the fasting-lab answer at
    inquiry time. RULE 1 -> always fasting; RULE 2 -> 75% fasting; every
    other determined appointment type -> 50% fasting."""
    text_lower = appointment_reason.lower()
    if _fasting_lab_is_wellness_or_diabetes(text_lower):
        return "Yes. These are fasting labs."
    if _fasting_lab_is_follow_up(text_lower):
        return (
            "Yes, these are fasting labs." if random.random() < 0.75
            else "No, fasting is not required."
        )
    return (
        "Yes, these are fasting labs." if random.random() < 0.50
        else "No, fasting is not required."
    )


# ─────────────────────────────────────────
# Provider-cancelled appointment: why did it happen?
# ─────────────────────────────────────────
# PCPs occasionally cancel scheduled appointments. When a patient calls
# asking why their appointment was cancelled, Steve must never guess -
# the reason is chosen deterministically from this weighted list and
# recited verbatim. Weights MUST sum to 100 (25% labs, 25% provider
# time off, 20% family emergency, 20% out sick, 10% termination for
# misconduct). The why-question must be answered ahead of any generic
# cancel-trigger block so "why did you cancel my appointment" is read
# as a question, not as a NEW request to cancel the appointment.
CANCELLATION_REASONS_WITH_WEIGHTS = [
    ("the lab work required prior to the appointment had not been completed", 25),
    ("the provider needed to take time off", 25),
    ("the provider had a family emergency", 20),
    ("the provider was out sick", 20),
    ("the provider had to terminate the patient-provider relationship due to patient misconduct", 10),
]

_CANCELLATION_REASON_WEIGHTS = tuple(
    w for _, w in CANCELLATION_REASONS_WITH_WEIGHTS
)

CANCELLATION_WHY_TRIGGERS = [
    "why was my appointment cancelled", "why was my appointment canceled",
    "why did my appointment get cancelled", "why did my appointment get canceled",
    "why did you cancel my appointment", "why did the office cancel my appointment",
    "why did the doctor cancel my appointment", "why did the provider cancel my appointment",
    "why was my appointment brought up as cancelled", "why is my appointment cancelled",
    "was my appointment cancelled", "was my appointment canceled",
    "my appointment was cancelled", "my appointment was canceled",
    "my appointment got cancelled", "my appointment got canceled",
    "who cancelled my appointment", "who canceled my appointment",
    "can you tell me why my appointment was cancelled",
    "can you tell me why my appointment was canceled",
]


def detect_cancellation_why_intent(message_lower):
    return any(t in message_lower for t in CANCELLATION_WHY_TRIGGERS)


def _pick_cancellation_reason(round_index=None):
    """Returns one of the five standard cancellation reasons per the
    configured weights (25/25/20/20/10). Pass round_index (0-4) to
    force a specific reason for tests; otherwise a weighted random pick,
    so over many calls each reason appears with its probability."""
    if round_index is not None:
        return CANCELLATION_REASONS_WITH_WEIGHTS[round_index][0]
    return random.choices(
        CANCELLATION_REASONS_WITH_WEIGHTS,
        weights=_CANCELLATION_REASON_WEIGHTS,
        k=1,
    )[0][0]


def get_next_business_day():
    """Returns the next day the office is actually open - skips
    Saturday, Sunday, AND any recognized office holiday (e.g. if
    Monday itself is Labor Day, this returns Tuesday)."""
    candidate = datetime.now() + timedelta(days=1)
    while (
            candidate.weekday() >= 5
            or is_recognized_office_holiday(candidate.date())
    ):
        candidate += timedelta(days=1)
    return candidate.strftime("%A, %B %d")


def detect_dob_in_message(message):
    dob_pattern = re.compile(
        r'\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b|'
        r'\b(january|february|march|april|may|june|july|august|'
        r'september|october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b',
        re.IGNORECASE
    )
    return bool(dob_pattern.search(message))


def _parent_yes_in_message(message):
    """Deterministic parent/guardian-on-line YES router (Sprint 15).

    After the under-18 DOB gate asked the minor's caller whether a
    parent or guardian can come on the phone, THIS is the child's yes
    reply. "Yes" here is a DIFFERENT axis from the appointment-proceed
    yes ("yes, I want to book") - a minor saying "yes, my dad is right
    here" must route the parent onto the line, NOT the adult booking
    ladder. These phrases are the exact "parent is physically present"
    confirmations we pass to _parent_yes_in_message from the dob stage.
    """
    # A negation ("my mom isnt home", "no she cannot come") is ALWAYS a
    # refusal to produce a guardian, never a confirmation - even though
    # the message contains "she"/"my mom". Without this guard the bare
    # relationship-token match below would misroute those as YES and
    # book a minor whose guardian explicitly declined to get on the line.
    if _parent_no_in_message(message):
        return False
    yes_pattern = re.compile(
        r'\b(yes|yeah|yep|yup|sure|absolutely|of course|okay|ok|fine)\b|'
        r'\b(she|he|my (mum|mom|mother|dad|daddy|father|parent|guardian|'
        r'grandma|grandfather|grandparent))\b',
        re.IGNORECASE
    )
    return bool(yes_pattern.search(message))


def _parent_no_in_message(message):
    """Deterministic parent/guardian-on-line NO router (Sprint 15).

    The minor's reply to "put the parent/guardian on the phone now"
    declined to have the parent come on. Steve must never book a minor
    without a guardian - so this routes to the warm no-parent exit (no
    appointment scheduled, parent asked to call back) instead of the
    adult booking ladder.
    """
    no_pattern = re.compile(
        # Bug fix: the trailing \b is attached to the WHOLE group, not
        # just the last alternative - otherwise a bare "no" has no right
        # word-boundary and matches the "no" inside unrelated words like
        # "nOW" ("put my mom on the phone right now" -> false NO, hanging
        # up on a minor whose parent is literally being fetched).
        r'\b(no|nope|nah|not (right )?now|not available|cant|can\'t|'
        r'cannot|guards?)\b',  # not available right now
        re.IGNORECASE
    )
    if no_pattern.search(message):
        return True
    # Explicit "parent is not here" / "they are not available" catches
    no_parent_pattern = re.compile(
        r'\b(parent|guardian|mom|dad|mother|father|she|he)\b[^.!?]{0,40}'
        r'\b(not (here|available|home|able)|isn\'t|isnt|cant|can\'t|cannot|'
        r'won\'t|wont)\b',
        re.IGNORECASE
    )
    return bool(no_parent_pattern.search(message))


def _parent_transfer_in_message(message):
    """Deterministic parent/guardian-on-line TRANSFER router (Sprint 15).

    A minor's reply to the under-18 gate question is not always a plain
    yes/no ("yes, my mom is right here" / "no, she cannot come"). The
    most common third shape is a TRANSFER announcement: the child is
    about to fetch the parent but the parent is NOT yet on the line -
    e.g. "I'm going to put my mom on the phone right now. Please wait."
    or "hold on, I'll get my dad". Such a message is neither a guardian
    confirmation (the guardian has not spoken) nor a refusal (they are
    coming) - Steve must acknowledge the transfer and WAIT for the
    parent to identify themselves, not prematurely declare the guardian
    confirmed. Detected phrases are the "go get the parent" /
    "putting them on" constructions WITHOUT the parent having spoken.
    """
    transfer_pattern = re.compile(
        r'\b(going to put|gonna put|i\'?ll put|i will put|let me put|'
        r'putting (?:my |her |him |the )?|get my|let me get|i\'?ll get|'
        r'i will get|going to get|go get|bring|sending|transferring|'
        r'hold on|one moment|please wait|just a (second|moment|minute)|'
        r'wait (?:a|one) (?:second|moment|minute|bit)|'
        r'put (?:my |the )?(?:mom|mum|mother|dad|daddy|father|parent|guardian)[^.!?]{0,30}'
        r'on (?:the )?(?:phone|line))|'
        r'\b(go get|fetch|retrieve)\b',
        re.IGNORECASE
    )
    return bool(transfer_pattern.search(message))


# Relationship/pronoun/low-content words that extract_names_from_message
# can mistake for a caller identity when a parent self-introduces without
# actually giving their name (e.g. "This is her mom", "I'm his father",
# "Hi, my name is her mom"). Used by _established_parent_name_from_message
# to reject such false captures so Steve keeps asking for a real name
# instead of "confirming" a guardian whose name he never received.
_ESTABLISHED_PARENT_NONNAME_TOKENS = frozenset({
    "her", "his", "my", "our", "their", "the", "a", "an",
    "mom", "mum", "mother", "dad", "daddy", "father",
    "parent", "guardian", "grandma", "grandma", "grandmother",
    "grandpa", "grandfather", "she", "he", "they", "wait", "waiting",
    "hi", "hello", "hey", "yes", "yeah", "okay", "ok", "sure", "just",
    "here", "there", "coming", "phone", "line", "right", "now",
    "please", "one", "moment", "second", "minute", "hold", "gonna",
})


def _established_parent_name_from_message(message):
    """Extract the parent/guardian's own first+last name from a message
    spoken once they are on (or announced on) the line. Reuses
    extract_names_from_message's caller patterns ("this is X Y", "my
    name is X Y", "I'm X Y", bare "X Y") but rejects the relationship /
    pronoun captures that would otherwise misidentify "This is her mom"
    as a guardian named "Her Mom". Returns (first, last) or (None, None).
    """
    extracted = extract_names_from_message(message)
    first = extracted.get("caller_first")
    last = extracted.get("caller_last")
    if not first or not last:
        return None, None
    if first.lower() in _ESTABLISHED_PARENT_NONNAME_TOKENS:
        return None, None
    if last.lower() in _ESTABLISHED_PARENT_NONNAME_TOKENS:
        return None, None
    if first.lower() == last.lower():
        return None, None
    # A transfer announcement carries no parent name yet ("I'm going to
    # put my mom on the phone right now") - the only plausible extract
    # there would be a false "my mom", which the token filter rejects.
    return first, last


def _established_parent_first_name_from_message(message):
    """Best-effort capture of a guardian's FIRST name when only one name
    token has been offered so far ("Hi this is Dawn", "my name is Dawn").
    _established_parent_name_from_message requires both names and yields
    (None, None) here, which made Steve re-ask "first and last name, "
    "please?" despite the first name already being on the line. Requires
    a capital-initial token, so lowercase filler ("I'm going to get her")
    can never be mistaken for a name, and rejects the known
    relationship/pronoun tokens."""
    m = re.search(
        r"(?i:this\s+is|my\s+name\s+is|i'?m|i\s+am)\s+([A-Z][a-z]+)",
        message
    )
    if not m:
        return None
    name = m.group(1)
    if name.lower() in _ESTABLISHED_PARENT_NONNAME_TOKENS:
        return None
    return name


def _established_parent_last_name_from_message(message, known_first=None):
    """Best-effort capture of a guardian's LAST name when Steve just
    asked for it and the parent replies with the surname alone ("Baldwin",
    "It's Baldwin."). Only fires on a short, name-like reply (at most 4
    tokens) containing exactly ONE capitalized candidate that is not the
    already-known first name and not a relationship/pronoun token."""
    tokens = re.findall(r"\b[A-Za-z'.-]+\b", message)
    if len(tokens) > 4:
        return None
    candidates = [
        c for c in re.findall(r"\b[A-Z][a-z'.-]+\b", message)
        if c.lower() not in _ESTABLISHED_PARENT_NONNAME_TOKENS
        and c.lower() != "this"   # the "this is" self-intro keyword
        and (not known_first or c.lower() != known_first.lower())
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def extract_dob_from_message(message):
    dob_pattern = re.compile(
        r'\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b|'
        r'\b(january|february|march|april|may|june|july|august|'
        r'september|october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b',
        re.IGNORECASE
    )
    match = dob_pattern.search(message)
    return match.group(0) if match else None


def detect_provider_in_message(message_lower):
    for last_name, full_name in PROVIDER_LAST_NAMES.items():
        if last_name in message_lower:
            return full_name
    return None


def detect_ma_name_in_message(message_lower):
    for ma_name, provider in MA_NAME_TO_PROVIDER.items():
        if ma_name in message_lower:
            return ma_name.capitalize(), provider
    return None, None


def detect_nurse_ma_request(message_lower):
    return any(phrase in message_lower for phrase in NURSE_MA_REQUEST_PHRASES)


def detect_nurse_visit(message_lower):
    """True when the patient is asking for a nurse visit (injection,
    B12/flu shot, vaccine, etc.) - a service that must be performed in
    the office and is scheduled by the medical assistant, never by Steve
    and never virtually."""
    return any(phrase in message_lower for phrase in NURSE_VISIT_TRIGGERS)


def detect_lab_order_pickup(message_lower):
    return any(
        trigger in message_lower for trigger in LAB_ORDER_PICKUP_TRIGGERS
    )


def detect_controlled_substance(message_lower):
    for med in SCHEDULE_1:
        if med in message_lower:
            return 1
    for med in SCHEDULE_2:
        if med in message_lower:
            return 2
    for med in SCHEDULE_3:
        if med in message_lower:
            return 3
    return None


def find_controlled_substance_word(message_lower):
    """Returns the exact matched drug word/phrase (e.g. 'alprazolam'),
    not just its schedule number. Additive sibling to
    detect_controlled_substance() above - does not modify that
    function or any of its existing callers, avoiding any risk to
    already-working code that depends on its current return signature.
    Used only for wording the Scenario 11/12 post-booking bridge
    question with the patient's actual medication name."""
    for med in SCHEDULE_1 + SCHEDULE_2 + SCHEDULE_3:
        if med in message_lower:
            return med
    return None


_DAY_COUNT_WORD_TO_NUM = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
}


def parse_days_remaining_from_reply(message_lower):
    """Parses a day-count out of a free-form reply to 'Do you have
    enough medication to hold you over until your appointment?'.
    Handles raw digits ('2 days'), spelled-out counts ('a week', 'one
    week', 'two weeks'), and a bare number with no unit at all ('10').
    Checked BEFORE any sufficiency-phrase check (see
    patient_confirms_sufficient_medication() below) so a reply that
    names an actual count - even inside an otherwise affirmative-
    sounding sentence - is measured against the real days-until-
    appointment rather than trusted at face value. Returns an int day
    count, or None if nothing recognizable is present."""
    match = re.search(r'\b(\d+)\s*weeks?\b', message_lower)
    if match:
        return int(match.group(1)) * 7
    match = re.search(
        r'\b(a|an|one|two|three|four)\s+weeks?\b', message_lower
    )
    if match:
        return _DAY_COUNT_WORD_TO_NUM[match.group(1)] * 7
    match = re.search(r'\b(\d+)\s*days?\b', message_lower)
    if match:
        return int(match.group(1))
    match = re.search(r'\b(a|an|one|two|three|four)\s+days?\b', message_lower)
    if match:
        return _DAY_COUNT_WORD_TO_NUM[match.group(1)]
    match = re.search(r'\b(\d+)\b', message_lower)
    if match:
        return int(match.group(1))
    return None


_SUFFICIENT_MEDICATION_PHRASES = [
    "enough to hold me over", "have enough", "should be enough",
    "should hold me over", "should last", "i'll be fine",
    "i will be fine", "i think i have enough", "plenty",
    "more than enough",
]

_NEGATION_BEFORE_ENOUGH_PATTERN = re.compile(
    r"\b(not|no|don't|do not|doesn't|does not|won't|will not|"
    r"can't|cannot|isn't|is not|wasn't|was not)\b[^.?!]{0,20}\benough\b",
    re.IGNORECASE
)

# Bug fix: a direct one-word reply to the yes/no question "Do you have
# enough X remaining to hold you over until your appointment?" (e.g.
# a bare "No") previously matched neither the phrase lists above nor
# below, since those only look for "enough"/insufficiency wording
# specifically. That silently fell through to a generic re-ask
# instead of registering as an answer at all. Anchored to the whole
# message (not a substring search) so it only fires on a genuinely
# bare answer, never on "no" or "yes" appearing inside a longer
# sentence (e.g. "I don't know" must not match).
_BARE_NEGATIVE_REPLY_PATTERN = re.compile(
    r"^\s*(no|nope|nah|not really)\s*[.!]?\s*$", re.IGNORECASE
)
_BARE_AFFIRMATIVE_REPLY_PATTERN = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure)\s*[.!]?\s*$", re.IGNORECASE
)

_INSUFFICIENT_MEDICATION_PHRASES = [
    "not enough", "won't be enough", "will not be enough",
    "wouldn't be enough", "would not be enough", "insufficient",
    "running low", "don't think i have enough",
    "do not think i have enough",
]


def patient_declines_sufficient_medication(message_lower):
    """Detects an explicit insufficiency statement, INCLUDING a
    negated form of the sufficiency phrase itself (e.g. 'I do NOT
    have enough medication to hold me over'). MUST be checked BEFORE
    patient_confirms_sufficient_medication() below - that function's
    naive substring check on 'have enough' also matches inside 'do
    not have enough', silently inverting the patient's actual answer.
    Traced root cause of the Scenario 11 regression: a patient stating
    insufficiency was misclassified as confirming sufficiency, and the
    bridge-request workflow was skipped entirely."""
    if _NEGATION_BEFORE_ENOUGH_PATTERN.search(message_lower):
        return True
    if _BARE_NEGATIVE_REPLY_PATTERN.match(message_lower):
        return True
    return any(p in message_lower for p in _INSUFFICIENT_MEDICATION_PHRASES)


def patient_confirms_sufficient_medication(message_lower):
    """A direct sufficiency affirmation with no day-count attached at
    all (e.g. 'I have enough alprazolam to hold me over') - only
    checked once parse_days_remaining_from_reply() above has already
    found nothing to measure, so this never overrides an actual number
    the patient gave. Callers MUST check
    patient_declines_sufficient_medication() first - this function's
    own phrase list is a positive-only substring match and does not
    itself detect negation (e.g. 'do not have enough')."""
    if _BARE_AFFIRMATIVE_REPLY_PATTERN.match(message_lower):
        return True
    return any(p in message_lower for p in _SUFFICIENT_MEDICATION_PHRASES)


def check_response_time_stated(text):
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in RESPONSE_TIME_PHRASES)


def check_office_hours_stated(text):
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in OFFICE_HOURS_PHRASES)


def get_patient_pcp_from_history():
    for msg in conversation_history:
        content = msg.get("content", "").lower()
        for last_name, full_name in PROVIDER_LAST_NAMES.items():
            if last_name in content:
                return full_name
    return None


def get_ma_for_patient():
    provider = get_patient_pcp_from_history()
    if provider and provider in PROVIDER_MA_MAP:
        return PROVIDER_MA_MAP[provider], provider
    return None, None


def detect_address_in_message(message_lower):
    """Returns True if message appears to contain a street address."""
    return any(indicator in message_lower for indicator in ADDRESS_INDICATORS)


# ─────────────────────────────────────────────
# Sprint 12: Covering Provider (Acute Visit Workflow)
# ─────────────────────────────────────────────
# Elizabeth Horowitz is a Nurse Practitioner, NOT a physician. She is
# intentionally kept separate from PROVIDERS/PROVIDER_MA_MAP/
# PROVIDER_LAST_NAMES above, since those model the practice's primary
# PCPs. She only ever serves as a COVERING provider for patients already
# established with one of the practice's PCPs, per the acute visit
# workflow (see Acute_Visit_Workflow_v4_1_Diagram.pdf).
COVERING_PROVIDER_NAME = "Elizabeth Horowitz, NP"
COVERING_PROVIDER_BARE_NAME = "Elizabeth Horowitz"
COVERING_PROVIDER_CREDENTIAL = "Nurse Practitioner"
COVERING_PROVIDER_MA_NAME = "Catherine"
SAME_DAY_VIRTUAL_CLINIC_PHONE_NUMBER = "321-555-0143"
# ASSUMPTION — confirm actual Same-Day Virtual Clinic hours before deploying.
SAME_DAY_VIRTUAL_CLINIC_HOURS = "7:00 AM to 7:00 PM, seven days a week"

# Scenario 16/17: Reschedule / Cancel Acute Visit
RESCHEDULE_ACUTE_VISIT_TRIGGERS = [
    "reschedule my appointment", "reschedule the appointment",
    "reschedule my visit", "need to reschedule", "want to reschedule",
    "change my appointment", "move my appointment",
    "reschedule it", "reschedule this", "like to reschedule",
]

CANCEL_ACUTE_VISIT_TRIGGERS = [
    "cancel my appointment", "cancel the appointment",
    "cancel my visit", "need to cancel my appointment",
    "want to cancel my appointment", "cancel my acute visit",
    "cancel it", "cancel this", "like to cancel",
]

# Bug fix: distinguishes explicit per-person ownership language
# ("my appointment is X" vs. "his/her/their appointment is Y") in a
# household cancellation request, so each stored appointment record
# can be cancelled under its correct owner instead of both being
# collapsed into whichever name patient_first_name happened to hold.
_MY_APPOINTMENT_OWNERSHIP_PATTERN = re.compile(
    r"\bmy\s+appointment\b", re.IGNORECASE
)
_SPOUSE_APPOINTMENT_OWNERSHIP_PATTERN = re.compile(
    r"\b(?:his|her|their)\s+appointment\b", re.IGNORECASE
)

QUERY_ACUTE_VISIT_TRIGGERS = [
    "when is my appointment", "when is my acute visit",
    "when my acute visit is", "what is my appointment",
    "what's my appointment", "see my appointment",
    "check my appointment", "do i have an appointment",
    "when is my visit", "what time is my appointment",
]

# Bug fix (household NEW-PATIENT cancellation): a couple establishing
# care together says "we need to cancel THOSE appointments" - a
# plural-family phrasing that matches none of the singular
# CANCEL_ACUTE_VISIT_TRIGGERS above. Kept OUT of
# CANCEL_ACUTE_VISIT_TRIGGERS on purpose so the acute-visit cancel
# block (which fabricates an acute visit on any of those phrases for
# patients without a generic record) is not broadened; this list gates
# ONLY the dual new-patient household cancel branch in the generic
# block plus the pre-chart same-message shortcut guard.
HOUSEHOLD_CANCEL_TRIGGERS = [
    "cancel those appointments", "cancel those",
    "cancel the appointments", "cancel our appointments",
    "cancel both appointments",
]


def generate_existing_acute_appointment():
    """
    Randomly simulates the patient's already-scheduled acute visit
    appointment (day + time), so Steve has something concrete to
    reference before a reschedule or cancel can proceed. Mirrors the
    same random-simulation approach already used elsewhere in this file
    (generate_weekly_availability, get_future_available_times).
    """
    day = random.choice(DAYS_OF_WEEK)
    time = random.choice(AVAILABLE_TIMES)
    return day, time


# Visit reasons for which the covering provider is NEVER eligible,
# regardless of availability. Plain "follow up" is excluded here on
# purpose - see COVERING_PROVIDER_POST_ACUTE_FOLLOWUP_TRIGGERS below for
# the narrower post-hospital/post-ER follow-up carve-out, which IS
# eligible even though it also contains the words "follow up".
COVERING_PROVIDER_INELIGIBLE_VISIT_TRIGGERS = [
    "new patient", "establish care", "establish with",
    "follow up", "follow-up", "followup",
    "comprehensive health assessment", "cha visit", "cha appointment",
    "medicare annual wellness", "annual wellness visit", "maw visit",
    "annual physical", "yearly physical", "physical exam",
    "wellness exam", "wellness visit",
]

# Visit reasons that ARE eligible for the covering provider even though
# they contain "follow up" wording - these are post-acute-event
# follow-ups, not routine PCP follow-ups, and take priority over the
# ineligible list above.
COVERING_PROVIDER_POST_ACUTE_FOLLOWUP_TRIGGERS = [
    "post hospital", "post-hospital", "post-hospitalization",
    "after being discharged", "discharged from the hospital",
    "released from the hospital", "hospital follow up",
    "hospital follow-up", "out of the hospital",
    "post er", "post-er", "post emergency room",
    "after the er", "after the emergency room", "after my er visit",
    "er follow up", "er follow-up", "emergency room follow up",
    "emergency room follow-up", "went to the er",
    "went to the emergency room",
]


def is_visit_eligible_for_covering_provider(message_lower):
    """
    Returns True if the stated reason for visit is a type the covering
    provider (Elizabeth Horowitz, NP) is eligible to see for an
    established patient, per practice policy:
      ELIGIBLE:    acute or ongoing issues; post-hospital follow-up;
                   post-ER follow-up
      INELIGIBLE:  new patient appointments; routine follow-ups; CHA;
                   Medicare Annual Wellness visits; annual physicals
    This only determines VISIT-TYPE eligibility. It does NOT check
    availability — see covering_provider_has_more_immediate_availability()
    below for the separate "more immediate availability than the PCP"
    rule that must ALSO be satisfied before offering the covering
    provider.
    """
    if any(
            phrase in message_lower
            for phrase in COVERING_PROVIDER_POST_ACUTE_FOLLOWUP_TRIGGERS
    ):
        return True
    if any(
            phrase in message_lower
            for phrase in COVERING_PROVIDER_INELIGIBLE_VISIT_TRIGGERS
    ):
        return False
    return True


def covering_provider_has_more_immediate_availability(
        pcp_soonest_days, covering_soonest_days
):
    """
    Returns True if the covering provider (Elizabeth Horowitz, NP) has a
    sooner available appointment than the patient's own PCP.
    NOTE: this function only encodes the COMPARISON rule itself. It
    takes two already-computed "days until soonest available slot"
    values as input. The actual per-provider availability generation
    (same-day and within-7-days, for both the PCP and the covering
    provider) is separate scheduling logic to be built out later this
    sprint per the Acute Visit Workflow diagram, so this is intentionally
    left as a small, ready-to-plug-in piece rather than guessing at a
    scheduling mechanism that may need to be designed together.
    """
    return covering_soonest_days < pcp_soonest_days


def generate_provider_soonest_availability(provider_name):
    """
    Randomly simulates the number of days from today until the given
    provider's soonest available appointment slot (0 = same-day).
    Mirrors the same random-simulation approach already used elsewhere
    in this file (generate_weekly_availability, get_future_available_times,
    generate_new_patient_provider_availability) rather than a real
    scheduling backend. Works for either a PCP name from PROVIDERS or
    COVERING_PROVIDER_NAME.
    """
    if not is_office_open_today():
        return random.randint(1, 3)
    return random.randint(0, 7)


# ─────────────────────────────────────────────
# Sprint 12: Acute visit nuances - contagious/virtual-refusal and UTI
# ─────────────────────────────────────────────
CONTAGIOUS_SYMPTOM_TRIGGERS = [
    "flu", "influenza", "strep throat", "strep", "covid", "coronavirus",
    "pink eye", "conjunctivitis", "hand foot and mouth", "rsv",
    "contagious", "fever and cough", "sore throat and fever",
    "stomach bug", "norovirus", "pneumonia", "bronchitis",
]

VIRTUAL_VISIT_REFUSAL_TRIGGERS = [
    "needs to see me", "needs to see him", "needs to see her",
    "needs to be seen in person", "not good with computers",
    "not good with technology", "don't do video calls",
    "do not do video calls", "prefer to be seen in person",
    "prefers to be seen in person", "in person visit",
    "won't do a virtual", "will not do a virtual",
    "no virtual", "not comfortable with video",
    "i really think he needs to see", "i really think she needs to see",
    "technically challenged", "in person",
]

# Virtual-visit wait / late-PCP workflow. Detects a patient who says
# they've been waiting for the PCP to join the virtual visit or gives
# any indication they are annoyed with the wait, so Steve can
# acknowledge the delay and offer to reschedule or wait.
VIRTUAL_WAIT_DIRECT_PHRASES = [
    "waiting for the doctor", "waiting for dr", "waiting for dr.",
    "waiting for my doctor", "waiting for the provider",
    "waiting for my pcp", "waiting for the physician",
    "waiting for the pcp", "waiting on dr", "waiting on dr.",
    "waiting on the doctor", "waiting on the provider",
    "waiting on my pcp", "waiting on the pcp",
    "doctor hasn't joined", "doctor has not joined",
    "provider hasn't joined", "provider has not joined",
    "pcp hasn't joined", "pcp has not joined",
    "doctor hasn't shown up", "provider hasn't shown up",
    "doctor hasn't started", "provider hasn't started",
    "doctor to join", "provider to join", "pcp to join",
    "join the virtual visit", "join my virtual visit",
    "join the video visit", "join the video call",
    "virtual visit hasn't started",
    "waiting for the doctor to join", "waiting for the pcp to join",
]

VIRTUAL_WAIT_WAITING_WORDS = [
    "waiting", "wait", "how much longer", "running late",
    "running behind",
]

VIRTUAL_WAIT_PROVIDER_WORDS = [
    "doctor", "provider", "pcp", "physician", "dr.",
]

VIRTUAL_WAIT_JOIN_WORDS = [
    "join", "virtual", "video", "telehealth",
]

VIRTUAL_WAIT_ANNOYANCE_WORDS = [
    "annoyed", "annoying", "frustrated", "impatient", "tired of waiting",
    "sick of waiting", "ridiculous", "taking too long", "taking forever",
    "is he even coming", "is she even coming", "what's taking so long",
]

VIRTUAL_WAIT_RESCHEDULE_PHRASES = [
    "reschedule", "rebook", "another day", "another time",
    "different day", "different time", "a different day",
    "a different time", "move it", "move my",
]

VIRTUAL_WAIT_STAY_PHRASES = [
    "i'll wait", "i will wait", "ill wait", "i can wait",
    "i'll hold", "i will hold", "i'll stay on", "i will stay on",
    "wait a little longer", "wait longer", "i'm fine to wait",
    "i am fine to wait", "ok i'll wait", "okay i'll wait",
    "no thank you", "thats fine", "that's fine",
]

VIRTUAL_WAIT_SCHEDULING_WORDS = [
    "appointment", "get in", "get to see", "get to see him",
    "get to see her", "book", "schedule", "waiting list",
    "to be seen", "to see", "next available",
    "first available",
]


def detect_virtual_wait_annoyance(message_lower):
    """Return True when the patient reports waiting for the PCP to join
    the virtual visit or shows any sign of annoyance with the wait."""
    if any(p in message_lower for p in VIRTUAL_WAIT_DIRECT_PHRASES):
        return True
    waiting = any(w in message_lower for w in VIRTUAL_WAIT_WAITING_WORDS)
    provider = any(w in message_lower for w in VIRTUAL_WAIT_PROVIDER_WORDS)
    joining = any(w in message_lower for w in VIRTUAL_WAIT_JOIN_WORDS)
    annoyed = any(a in message_lower for a in VIRTUAL_WAIT_ANNOYANCE_WORDS)
    if not (provider or joining) or not (waiting or annoyed):
        return False
    if any(n in message_lower for n in VIRTUAL_WAIT_SCHEDULING_WORDS):
        return False
    return True


# Virtual-visit paywall bypass. When a patient says they do not want to
# (or are not comfortable) providing credit card / payment information
# through the virtual visit portal - for any reason - or asks for a way
# around the virtual visit payment screen, Steve is allowed to bypass
# the virtual visit paywall. Detected deterministically and answered
# with a fixed business script (never delegated to the LLM), mirroring
# the virtual-visit wait workflow just above.
VIRTUAL_VISIT_PAYWALL_BYPASS_TRIGGERS = [
    "way around the payment",
    "around the payment screen",
    "bypass the payment",
    "bypass the payment screen",
    "bypass the virtual visit payment",
    "bypass that for me",
    "bypass the screen",
    "skip the payment",
    "is there a way around",
    "can someone bypass",
    "can you bypass",
]

# Payment / card refusal words used together with a reluctance phrase
# ("don't want", "not comfortable", etc.). "ccard" is included because
# patients actually type it ("don't want to give my ccard information").
_VIRTUAL_VISIT_PAYWALL_CARD_WORDS = [
    "credit card", "ccard", "card information", "payment info",
]
# "payment" alone needs a virtual-visit-ish context word so a general
# billing/copay statement ("I don't want to pay my bill online") can
# never fire the virtual-visit bypass on its own.
_VIRTUAL_VISIT_PAYWALL_PAYMENT_CONTEXT_WORDS = [
    "virtual visit", "virtual", "portal", "online",
    "over the internet", "check-in", "check in",
]
_VIRTUAL_VISIT_PAYWALL_RELUCTANCE_PHRASES = [
    "don't want", "do not want", "dont want",
    "not comfortable", "don't feel comfortable",
    "do not feel comfortable", "dont feel comfortable",
    "won't", "will not", "wont", "prefer not", "rather not",
]


def detect_virtual_visit_paywall_bypass(message_lower):
    """Return True when the patient does not want to provide credit card
    / payment information through the virtual visit portal (for any
    reason) or asks for a way around the virtual visit payment screen.
    Narrowly scoped to genuine paywall-bypass requests so unrelated
    billing / insurance questions never trigger it."""
    if any(p in message_lower for p in VIRTUAL_VISIT_PAYWALL_BYPASS_TRIGGERS):
        return True
    has_card_word = any(
        p in message_lower for p in _VIRTUAL_VISIT_PAYWALL_CARD_WORDS
    )
    has_payment_context = (
        "payment" in message_lower
        and any(
            p in message_lower
            for p in _VIRTUAL_VISIT_PAYWALL_PAYMENT_CONTEXT_WORDS
        )
    )
    if not (has_card_word or has_payment_context):
        return False
    reluctant = any(
        p in message_lower for p in _VIRTUAL_VISIT_PAYWALL_RELUCTANCE_PHRASES
    )
    if reluctant:
        return True
    return any(
        p in message_lower for p in ("is there a way", "any way around")
    )


# ─────────────────────────────────────────────
# Patient running-late workflow
# ─────────────────────────────────────────────
# The patient calls to say they will be late for their appointment.
# Steve asks how many minutes late they will be. Under 15 minutes Steve
# says he will inform the medical assistant. 15 minutes or more means
# the slot cannot be held, so Steve explains the visit must be
# rescheduled and offers to reschedule during the call. Entirely
# deterministic (mirrors virtual-wait state machine) so the LLM never
# improvises the 15-minute threshold.
LATE_ARRIVAL_TRIGGERS = [
    "going to be late", "gonna be late", "i will be late", "i'll be late",
    "ill be late", "im going to be late", "i'm going to be late",
    "i am going to be late", "running late", "run late",
    "going to run late", "gonna run late", "will be running late",
    "i will be running late", "i'll be running late", "be running late",
    "be a little late", "a little late", "be a few minutes late",
    "few minutes late", "be a bit late", "be a few mins late",
    "late for my appointment", "late for my visit",
    "late to my appointment", "late to my visit",
    "late for the appointment",
    "i am late", "i'm late", "stuck in traffic", "traffic is bad",
    "in traffic", "i am delayed", "i'm delayed", "going to be delayed",
    "gonna be delayed", "will be delayed", "held up", "running behind"
]


def detect_late_arrival_intent(message_lower):
    """True when the patient is reporting they will be late for their
    appointment (rather than asking about a third party or the provider
    running late)."""
    return any(phrase in message_lower for phrase in LATE_ARRIVAL_TRIGGERS)


def parse_late_minutes(message_lower):
    """Parse how many minutes late the patient says they will be from a
    free-form reply to Steve's 'how many minutes late will you be?'
    question. Handles '10 minutes', '10 mins', '15 min', 'half an hour',
    'an hour', '2 hours', and a bare number like '20'. Returns an int
    number of minutes, or None if nothing recognizable is present.
    Kwargs: none."""
    # 'an hour' style phrases must be matched after 'half an hour',
    # since 'half an hour' also contains the substring 'an hour'.
    if re.search(r"\b(?:half an hour|half hour|thirty minutes?)\b", message_lower):
        return 30
    if re.search(r"\b(?:an hour|one hour|a few hours?)\b", message_lower):
        return 60
    if re.search(r"\b(?:a couple(?: of)? hours?|two hours?)\b", message_lower):
        return 120
    match = re.search(r"\b(\d{1,3})\s*(?:hour|hr)s?\b", message_lower)
    if match:
        return int(match.group(1)) * 60
    match = re.search(r"\b(\d{1,3})\s*(?:min(?:ute)?s?|m)\b", message_lower)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d{1,3})\b", message_lower)
    if match:
        minutes = int(match.group(1))
        if 1 <= minutes <= 240:
            return minutes
    return None


def handle_late_arrival_flow(message, message_lower):
    """State machine for the patient-running-late workflow. Returns a
    deterministic response at each digestible step, or None if this
    turn does not belong to the workflow (so the caller can fall
    through to the normal dispatch unchanged)."""
    global late_arrival_active, late_arrival_minutes_asked
    global late_arrival_reschedule_pending, late_arrival_reschedule_offered
    global late_arrival_reschedule_schedule

    if late_arrival_reschedule_pending:
        if not late_arrival_reschedule_offered:
            if patient_wants_to_decline(message_lower):
                late_arrival_reschedule_pending = False
                late_arrival_reschedule_offered = False
                late_arrival_reschedule_schedule = None
                late_arrival_active = False
                return (
                    "No problem. Since you'll be more than 15 minutes "
                    "late, we won't be able to hold today's appointment "
                    "slot. Is there anything else I can help you with "
                    "today?"
                )
            if patient_wants_to_proceed(message_lower) or "reschedule" in message_lower:
                late_arrival_reschedule_schedule = generate_weekly_availability(
                    force_has_availability=True
                )
                avail_lines = [
                    f"{day}: {', '.join(slots)}"
                    for day, slots in late_arrival_reschedule_schedule.items()
                    if slots
                ]
                avail_text = (
                    "; ".join(avail_lines) if avail_lines
                    else "no availability this week"
                )
                late_arrival_reschedule_offered = True
                return (
                    "Of course. Here is what we have available - "
                    f"{avail_text}. Which day and time works best for you?"
                )
            return (
                "Since you'll be more than 15 minutes late, we will need "
                "to reschedule your appointment. Would you like me to "
                "reschedule it for you now?"
            )
        slot = _extract_calendar_date_slot_from_text(message) or (
            _extract_day_time_from_reply(message)
        )
        if not slot:
            if patient_wants_to_decline(message_lower):
                late_arrival_active = False
                late_arrival_reschedule_pending = False
                late_arrival_reschedule_offered = False
                late_arrival_reschedule_schedule = None
                return (
                    "No problem. Is there anything else I can help you "
                    "with today?"
                )
            return (
                "I'm sorry, could you tell me the day and time that "
                "works best for you from the options I listed?"
            )
        store_generic_appointment_record(
            patient_first_name or caller_first_name,
            patient_last_name or caller_last_name,
            slot,
            reason=_derive_generic_appointment_reason(),
        )
        late_arrival_active = False
        late_arrival_reschedule_pending = False
        late_arrival_reschedule_offered = False
        late_arrival_reschedule_schedule = None
        return (
            f"Perfect, I've rescheduled your appointment for {slot}. "
            "Is there anything else I can help you with today?"
        )

    if late_arrival_active and late_arrival_minutes_asked:
        minutes = parse_late_minutes(message_lower)
        if minutes is None:
            return "No problem. About how many minutes late will you be?"
        late_arrival_active = False
        late_arrival_minutes_asked = False
        if minutes < 15:
            return (
                f"Thank you for letting us know. I'll let the medical "
                f"assistant know you're running about {minutes} minutes "
                f"late. Have a great day!"
            )
        late_arrival_reschedule_pending = True
        late_arrival_reschedule_offered = False
        return (
            "Since you'll be 15 minutes or more late, the provider "
            "won't be able to see you today and we will need to "
            "reschedule your appointment. Would you like me to reschedule "
            "it for you while you're on the line?"
        )

    if not late_arrival_active and detect_late_arrival_intent(message_lower):
        late_arrival_active = True
        late_arrival_minutes_asked = True
        return (
            "I'm sorry to hear that. About how many minutes late will "
            "you be today?"
        )

    return None

UTI_TRIGGERS = [
    "uti", "urinary tract infection", "burning when i urinate",
    "burning when i pee", "painful urination", "urinating a lot",
    "frequent urination", "urgency to urinate",
]

ANTIBIOTIC_DEMAND_TRIGGERS = [
    "just call in an antibiotic", "call in antibiotics",
    "prescribe an antibiotic", "put in for an antibiotic",
    "i need an antibiotic", "just need an antibiotic",
    "call in a prescription", "just write a prescription",
    "can you just call something in", "just call something in",
    "call in an antibiotic", "call in some antibiotics",
    "antibiotic", "antibiotics",
]


def get_provider_callin_decision():
    """
    Randomly simulates whether the primary provider agrees to call in a
    medication without an office visit, for a contagious patient who has
    refused a virtual visit. Mirrors the get_hipaa_status()/
    get_ma_availability() pattern: determined once per call, and
    idempotent (safe to call again) on any later call within the same
    request or conversation.
    """
    global provider_callin_decision_determined, current_provider_callin_decision
    if not provider_callin_decision_determined:
        forced = get_harness_override(
            "STEVE_FORCE_VIRTUAL_DECISION",
            mapping={"accepted": "AGREES", "declined": "DECLINES"}
        )
        if forced is not None:
            current_provider_callin_decision = forced
        else:
            current_provider_callin_decision = (
                "AGREES" if random.randint(0, 1) == 0 else "DECLINES"
            )
        provider_callin_decision_determined = True
    return current_provider_callin_decision


def get_ma_same_day_approval():
    """
    Randomly simulates (50/50) whether the medical assistant approves
    fitting a same-day PCP office visit into the schedule, per the
    Acute Visit Workflow diagram's "MA Approval Required" step. Mirrors
    the get_hipaa_status()/get_provider_callin_decision() pattern:
    determined once per call, and idempotent (safe to call again) on any
    later call within the same request or conversation.
    """
    global ma_same_day_approval_determined, current_ma_same_day_approval
    if not ma_same_day_approval_determined:
        forced = get_harness_override("STEVE_FORCE_PCP_AVAILABLE")
        if forced is not None:
            current_ma_same_day_approval = forced
        else:
            current_ma_same_day_approval = random.choice([True, False])
        ma_same_day_approval_determined = True
    return current_ma_same_day_approval


def get_covering_provider_same_day_availability():
    """
    Randomly simulates (50/50) whether the covering provider (Elizabeth
    Horowitz, NP) has same-day availability, checked via her medical
    assistant Catherine after the patient's own PCP has already declined
    a same-day visit. Mirrors the get_ma_same_day_approval() pattern:
    determined once per call, and idempotent (safe to call again) on any
    later call within the same request or conversation.
    """
    global covering_provider_same_day_determined
    global current_covering_provider_same_day_available
    if not covering_provider_same_day_determined:
        forced = get_harness_override("STEVE_FORCE_COVERING_AVAILABLE")
        if forced is not None:
            current_covering_provider_same_day_available = forced
        else:
            current_covering_provider_same_day_available = random.choice([True, False])
        covering_provider_same_day_determined = True
    return current_covering_provider_same_day_available


MINOR_ELIGIBLE_PROVIDERS = [
    "Dr. Sarah Mitchell", "Dr. David Thompson", "Dr. Lisa Anderson"
]

ACCEPTED_COMMERCIAL_INSURANCE = [
    "united", "united healthcare", "unitedhealthcare",
    "health first", "health first health plans",
    "cigna", "blue cross", "bcbs",
    "florida blue", "anthem", "aetna", "humana",
]

NEW_PATIENT_TRIGGERS = [
    "not a patient", "i'm not a patient", "i am not a patient",
    "new patient", "become a patient", "establish care",
    "accepting new patients", "taking new patients",
    "is dr.", "is doctor", "does dr.", "does doctor",
    "schedule my child", "schedule my son", "schedule my daughter",
    "for my son", "for my daughter", "for my child",
    "establish my child", "establish my son", "establish my daughter"
]

# Bug fix: "new patient" is a bare substring in NEW_PATIENT_TRIGGERS,
# so a caller STATING they already have a new-patient appointment
# ("my spouse and I have new patient appointments with Dr. Brooks")
# was misread as a REQUEST for one, dropping Steve into new-patient
# intake instead of existing-appointment handling. Mirrors the
# pcp_statement_pattern exclusion below for the same class of problem
# (a statement about an existing fact, not a request).
_EXISTING_NEW_PATIENT_APPOINTMENT_PATTERN = re.compile(
    r"\b(?:have|has|had|got)\s+(?:a\s+)?new[\s-]patient\s+appointments?\b"
    r"|\bnew[\s-]patient\s+appointments?\s+(?:already\s+)?(?:scheduled|booked|set)\b"
    r"|\b(?:are|is|am)\s+(?:already\s+)?scheduled\s+as\s+new\s+patients?\b"
    r"|\balready\s+(?:have|has|had)\s+(?:a\s+)?new[\s-]patient\s+appointments?\b"
    # Bug fix: a caller asking to CANCEL/reschedule/move/change an
    # EXISTING new-patient appointment ("would like to cancel our new
    # patient appointments") still references that appointment using
    # the phrase "new patient appointments" - the alternatives above
    # only covered "have/has/had/got" and "already scheduled" phrasing,
    # so this cancellation-of-an-existing-appointment case fell
    # through and was misread as a fresh new-patient REQUEST instead.
    r"|\b(?:cancel|reschedule|change|move)\s+(?:our|my|the)\s+new[\s-]patient\s+appointments?\b"
    # Bug fix: a caller can also reference their ALREADY-scheduled
    # new-patient appointment using "establish care" phrasing while
    # saying they need to move it ("my spouse and myself are scheduled
    # to establish care with Dr. Mitchell and we need to reschedule
    # those appointments"). "are scheduled to establish care" states an
    # existing arranged fact - it must not be re-read as a REQUEST for
    # new-patient intake the way the bare "establish care" trigger is.
    r"|\b(?:are|is|am)\s+scheduled\s+to\s+establish\s+care\b",
    re.IGNORECASE
)

MEDICAID_TRIGGERS = [
    "medicaid", "medi-caid", "medi caid"
]

# Sprint 15 enhancement: recognizes when a caller is registering
# themselves AND a spouse as new patients in the same call, so the
# household's second appointment request isn't silently dropped once
# the caller's own registration completes.
_HOUSEHOLD_RELATION_PATTERN = re.compile(
    r"\b(?:myself|me)\s+and\s+my\s+(husband|wife|spouse)\b"
    r"|\bmy\s+(husband|wife|spouse)\s+and\s+(?:myself|me|i)\b",
    re.IGNORECASE
)


def detect_new_patient_household_relation(message_lower):
    """Returns 'husband', 'wife', or 'spouse' if the message indicates
    the caller is registering for themselves AND a spouse (e.g.
    "myself and my husband", "husband and wife", "both of us", "me
    and my husband"), else None."""
    match = _HOUSEHOLD_RELATION_PATTERN.search(message_lower)
    if match:
        return (match.group(1) or match.group(2)).lower()
    if "husband and wife" in message_lower or "wife and husband" in message_lower:
        return "spouse"
    if "both of us" in message_lower:
        return "spouse"
    return None


_DAY_NAME_PATTERN = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday)\b", re.IGNORECASE
)
# Bug fix: the previous single pattern (\d{1,2}(?::\d{2})?\s*(?:am|pm))
# only matched a time WITH a colon (e.g. "2:30 PM") or a bare hour
# (e.g. "3 PM") - a compact time with no colon, like "230PM" (a common
# way "2:30 PM" gets typed/transcribed), matched neither: \d{1,2} caps
# at 2 digits so it could only consume "23" or "2", and neither is
# immediately followed by "am"/"pm" in "230PM", so the whole match
# failed. That silently made the day/time extraction fail and fall
# back to storing the entire raw sentence. Checked in priority order
# so a colon time is never misread by the compact pattern, and a
# compact time is never truncated to just its leading hour digit(s)
# by the hour-only pattern.
_TIME_COLON_PATTERN = re.compile(
    r"\b(\d{1,2}):(\d{2})\s*(am|pm)\b", re.IGNORECASE
)
_TIME_COMPACT_PATTERN = re.compile(
    r"\b(\d{1,2})(\d{2})\s*(am|pm)\b", re.IGNORECASE
)
_TIME_HOUR_ONLY_PATTERN = re.compile(
    r"\b(\d{1,2})\s*(am|pm)\b", re.IGNORECASE
)
_HOUSEHOLD_SECOND_APPT_PATTERN = re.compile(
    r"\b(?:schedule|book)\s+my\s+(husband|wife|spouse)\b"
    r"|\band\s+my\s+(husband|wife|spouse)\b", re.IGNORECASE
)
# Bug fix: the household pattern above needs a word boundary before
# "schedule", so it never matches "Please REschedule my wife for..." -
# and the generic reschedule reply handler had no spouse handling at
# all, so a request to move a spouse's appointment in the same message
# was silently dropped. This pattern recognizes the reschedule
# phrasing and is used to split the caller's own pick from the
# spouse's pick.
_SPOUSE_RESCHEDULE_PATTERN = re.compile(
    r"\b(?:schedule|book|reschedule)\s+my\s+(husband|wife|spouse)\b"
    r"|\band\s+my\s+(husband|wife|spouse)\b", re.IGNORECASE
)


def _find_time_in_text(text):
    """Finds a time expression in free text and returns it normalized
    as 'H:MM AM/PM'. Handles "2:30 PM" (colon), "230PM" (compact, no
    colon or space), and "3 PM" (hour only, minutes default to :00)."""
    for pattern in (_TIME_COLON_PATTERN, _TIME_COMPACT_PATTERN):
        match = pattern.search(text)
        if match:
            hour, minute, meridiem = match.groups()
            return f"{hour}:{minute} {meridiem.upper()}"
    match = _TIME_HOUR_ONLY_PATTERN.search(text)
    if match:
        hour, meridiem = match.groups()
        return f"{hour}:00 {meridiem.upper()}"
    return None


def _extract_slot_from_text(text, fallback_day=None):
    """Pulls a 'Weekday H:MM AM/PM' slot out of free text. Falls back
    to fallback_day if no weekday name is found in this specific
    segment (needed for phrasing like "...for 2:00 PM that same
    day", which refers back to a day named earlier in the message)."""
    day_match = _DAY_NAME_PATTERN.search(text)
    day_name = day_match.group(1).capitalize() if day_match else fallback_day
    time_str = _find_time_in_text(text)
    if not (day_name and time_str):
        return None
    return f"{day_name} {time_str}"


def _parse_calendar_date_from_text(text):
    """Extracts a concrete calendar date from text (e.g. the stored
    slot string 'September 30, 2026 @ 9:00 AM' or a reply like
    'October 5 at 9AM') as a datetime.date, or None. Year defaults to
    the current year (rolled forward when that resolves clearly into
    the past). Shared by _extract_calendar_date_slot_from_text()
    (scheduling) and the generic reschedule flow, which must never
    offer rescheduled slots earlier than the stored calendar-dated
    appointment."""
    date_match = re.search(
        r"\b(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\s+(\d{1,2})"
        r"(?:st|nd|rd|th)?(?:,?\s+(\d{2,4}))?\b",
        text, re.IGNORECASE
    )
    if not date_match:
        return None
    month_num = _MONTH_NAME_TO_NUM[date_match.group(1).lower()]
    day_num = int(date_match.group(2))
    year_str = date_match.group(3)
    if year_str:
        year = int(year_str)
        if year < 100:
            year += 2000
    else:
        year = datetime.now().year
    try:
        candidate = datetime(year, month_num, day_num).date()
    except ValueError:
        return None
    if candidate < datetime.now().date() - timedelta(days=60):
        year += 1
        try:
            candidate = datetime(year, month_num, day_num).date()
        except ValueError:
            return None
    return candidate


def _next_monday_on_or_after(reference_date):
    """First Monday on or after a reference date. Monday has
    weekday() == 0, so (0 - weekday) % 7 is 0 for a reference date
    that is already a Monday (returned unchanged), 1 for a Sunday
    (next day), and so on."""
    return reference_date + timedelta(days=(0 - reference_date.weekday()) % 7)


def _calendar_slot_from_date(reference_date, time_str):
    """Joins a concrete date and a normalized time string into a
    calendar slot (e.g. date(2026, 10, 5) + '1:00 PM' ->
    'October 05, 2026 @ 1:00 PM'). Shared by the calendar-slot parser
    and the reschedule reply handler (which builds a spouse's slot as
    the caller's picked date + the spouse's time)."""
    return (
        f"{reference_date.strftime('%B')} "
        f"{reference_date.strftime('%d')}, {reference_date.year} @ {time_str}"
    )


def _extract_calendar_date_slot_from_text(text):
    """Parses a concrete calendar date + time (e.g. 'October 5th at 9AM')
    into a slot string. The individual six-month follow-up flow offers
    appointments as calendar dates ('Monday, October 05, 2026') - never
    weekday names - so a caller naturally picks by date and
    _extract_slot_from_text (weekday-based) finds nothing, silently
    dropping the caller's own booking in the household-scheduling
    machine. Handles 'Month D', 'Month Dth', 'Month D, YYYY', and
    'Month D YYYY', defaulting the year to the current year (rolled
    forward when that resolves clearly into the past). Returns None if
    no month/date or time is found, mirroring _extract_slot_from_text's
    contract."""
    time_str = _find_time_in_text(text)
    candidate = _parse_calendar_date_from_text(text)
    if not time_str or candidate is None:
        return None
    return _calendar_slot_from_date(candidate, time_str)


def extract_household_scheduling(message, message_lower):
    """Bug fix: the appointment_time stage previously stored the
    entire raw sentence as the appointment slot instead of parsing it
    (e.g. "I'd like to take this Thursday at 1:30 PM and please
    schedule my husband for 2:00 PM that same day" was saved
    verbatim). Parses out a primary slot and, if the message also
    asks to schedule a spouse, a second slot for them. Returns
    (primary_slot, second_relation, second_slot) - the latter two are
    None if no second scheduling request is found. Falls back to the
    raw message for primary_slot if no day/time pair can be found at
    all, so an unrecognized format still gets something reasonable."""
    second_match = _HOUSEHOLD_SECOND_APPT_PATTERN.search(message_lower)
    if second_match:
        primary_text = message[:second_match.start()]
        second_text = message[second_match.start():]
    else:
        primary_text = message
        second_text = ""

    primary_slot = _extract_slot_from_text(primary_text) or message.strip()

    second_relation = None
    second_slot = None
    if second_match:
        second_relation = (second_match.group(1) or second_match.group(2)).lower()
        primary_day_match = _DAY_NAME_PATTERN.search(primary_text)
        primary_day_name = (
            primary_day_match.group(1).capitalize() if primary_day_match else None
        )
        second_slot = _extract_slot_from_text(second_text, fallback_day=primary_day_name)

    return primary_slot, second_relation, second_slot


def _household_pronoun_possessive(relation):
    """Returns the possessive pronoun to use when asking about the
    second household member's own information during a household
    registration pass (e.g. "Could I get his date of birth?") -
    avoids the ambiguous "your", which can be misread as still
    referring to the caller who just finished their own registration."""
    if relation == "husband":
        return "his"
    if relation == "wife":
        return "her"
    return "their"


def _household_object_pronoun(relation):
    """Object-case counterpart to _household_pronoun_possessive - "him"
    for husband, "her" for wife, "them" for spouse/unspecified."""
    if relation == "husband":
        return "him"
    if relation == "wife":
        return "her"
    return "them"


MARKETPLACE_TRIGGERS = [
    "marketplace", "obamacare", "aca plan", "aca insurance",
    "affordable care act", "healthcare.gov", "exchange plan"
]

MEDICARE_ADVANTAGE_TRIGGERS = [
    "medicare advantage", "medicare hmo", "medicare ppo",
    "advantage plan"
]

MEDICARE_TRIGGERS = [
    "medicare", "on medicare"
]


def calculate_age_from_dob(dob_string):
    """Parses a DOB string and returns age in years, or None if unparseable."""
    # Day-ordinal normalization: a spelled-month DOB can arrive with an
    # ordinal suffix on the day ("May 15th, 1965"). strptime's %B %d
    # patterns reject the suffix, so strip it first ("May 15th, 1965" ->
    # "May 15, 1965"). Numeric formats are unaffected (no ordinals).
    normalized_dob = re.sub(
        r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", dob_string.strip()
    )
    dob_patterns = [
        "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
        "%B %d, %Y", "%B %d %Y",
    ]
    for fmt in dob_patterns:
        try:
            dob_date = datetime.strptime(normalized_dob, fmt)
            today = datetime.now()
            age = today.year - dob_date.year
            if (today.month, today.day) < (dob_date.month, dob_date.day):
                age -= 1
            return age
        except ValueError:
            continue
    return None


def generate_new_patient_provider_availability():
    """
    Randomly determines accepting-new-patients status (50/50) for
    EVERY provider independently. Returns a dict of provider name
    to boolean (True = accepting, False = panel closed).
    """
    return {provider: random.choice([True, False]) for provider in PROVIDERS}


def determine_eligible_minor_providers(provider_availability):
    """
    Returns the list of providers who BOTH see minors AND are
    currently accepting new patients, per the current random
    availability dict. This is the intersection Don specified -
    minor-eligibility and new-patient-acceptance are independent
    filters that must both pass.
    """
    return [
        provider for provider in MINOR_ELIGIBLE_PROVIDERS
        if provider_availability.get(provider, False)
    ]


def detect_insurance_name(message_lower):
    """Returns the matched insurance keyword if found, else None."""
    for ins in ACCEPTED_COMMERCIAL_INSURANCE:
        if ins in message_lower:
            return ins
    return None


# Bug fix: previously only matched "through my employer"/"through an
# employer"/"employer"/"through work"/"work insurance" - so a patient
# saying their insurance is through a SPOUSE's job/company (e.g.
# "through my husband's work", "through my wife's company", "through
# my spouse's job") wasn't recognized as employer-sponsored at all,
# and got asked a clarifying question the answer to which they'd
# already given. "employer" alone already covers "my husband's
# employer" / "my wife's employer" as a substring; the word-boundary
# check below adds "work", "job", and "company" the same way, without
# false-matching inside an unrelated word like "network".
_EMPLOYER_SPONSORED_WORD_PATTERN = re.compile(
    r"\b(work|job|company)\b", re.IGNORECASE
)


def is_employer_sponsored_insurance_phrasing(message_lower):
    """True if the message describes employer-sponsored insurance,
    including insurance obtained through a spouse's employer, job,
    work, or company."""
    if any(
            phrase in message_lower for phrase in [
                "through my employer", "through an employer", "employer",
                "through work", "work insurance",
            ]
    ):
        return True
    return bool(_EMPLOYER_SPONSORED_WORD_PATTERN.search(message_lower))


def generate_referral_lookup():
    """
    Randomly determines if a past referral can be found in the chart.
    Returns (found, specialist_name, phone_number) or (False, None, None).
    Uses truly random generation each call.
    """
    found = random.choice([True, False])
    if found:
        first_names = [
            "James", "Robert", "Michael", "David", "William",
            "Richard", "Thomas", "Charles", "Daniel", "Matthew",
            "Sarah", "Jennifer", "Amanda", "Jessica", "Emily",
            "Michelle", "Patricia", "Linda", "Barbara", "Susan"
        ]
        last_names = [
            "Anderson", "Thompson", "Martinez", "Robinson", "Clark",
            "Rodriguez", "Lewis", "Lee", "Walker", "Hall",
            "Allen", "Young", "Hernandez", "King", "Wright",
            "Lopez", "Hill", "Scott", "Green", "Adams"
        ]
        first = random.choice(first_names)
        last = random.choice(last_names)
        # Ensure name doesn't repeat by using a fresh random each time
        specialist_name = f"Dr. {first} {last}"
        # Area code always 321 per business rule
        phone = f"321-{random.randint(100, 999)}-{random.randint(1000, 9999)}"
        return True, specialist_name, phone
    return False, None, None


def generate_lab_result_status():
    """Randomly determines if outside lab results are in the chart."""
    return random.choice([True, False])


def generate_appointment_within_week():
    """Randomly determines if patient has an appointment within the next week."""
    has_appointment = random.choice([True, False])
    if has_appointment:
        days_away = random.randint(1, 7)
        appt_date = datetime.now() + timedelta(days=days_away)
        while appt_date.weekday() >= 5:
            days_away += 1
            appt_date = datetime.now() + timedelta(days=days_away)
        appt_date_str = appt_date.strftime("%A, %B %d")
        return True, appt_date_str
    return False, None


def get_hipaa_status():
    global hipaa_status_determined, current_hipaa_status
    if not hipaa_status_determined:
        current_hipaa_status = (
            "ON_HIPAA" if random.randint(0, 1) == 0 else "NOT_ON_HIPAA"
        )
        hipaa_status_determined = True
    return current_hipaa_status


def get_ma_availability():
    global ma_availability_determined, current_ma_availability
    if not ma_availability_determined:
        current_ma_availability = random.choice([True, False])
        ma_availability_determined = True
    return current_ma_availability


def generate_referral_details(patient_pcp=None):
    days_ago = random.randint(1, 30)
    referral_date = (
            datetime.now() - timedelta(days=days_ago)
    ).strftime("%B %d, %Y")
    sending_provider = patient_pcp if patient_pcp else random.choice(PROVIDERS)
    reasons = [
        "chronic back pain", "knee pain", "shoulder pain",
        "cardiac evaluation", "neurological evaluation",
        "dermatology follow-up", "urological evaluation",
        "endocrinology consultation", "pulmonology evaluation",
        "gastroenterology consultation", "frequent kidney stones",
        "diabetes management", "hypertension management",
        "sleep apnea evaluation", "thyroid evaluation",
        "elbow pain", "hip pain", "wrist pain", "osteoarthritis"
    ]
    return referral_date, sending_provider, random.choice(reasons)


def generate_last_visit_date(schedule_num):
    if schedule_num == 1:
        days_since = random.randint(1, 60)
        window = 30
    elif schedule_num == 2:
        days_since = random.randint(1, 150)
        window = 90
    else:
        days_since = random.randint(1, 250)
        window = 180
    last_visit = datetime.now() - timedelta(days=days_since)
    last_visit_str = last_visit.strftime("%B %d, %Y")
    over_12_months = days_since > 365
    needs_appointment = days_since > window or over_12_months
    return last_visit_str, needs_appointment, days_since, over_12_months, window


def is_six_month_followup_request(message_lower):
    return any(
        phrase in message_lower for phrase in [
            "6 month follow up", "6-month follow up",
            "six month follow up", "six-month follow up",
            "6 month follow-up", "6-month follow-up",
            "six month follow-up", "six-month follow-up",
        ]
    )


def is_three_month_followup_request(message_lower):
    """Recognise an individual 3-month routine follow-up request. Kept
    deliberately separate from is_six_month_followup_request() so the two
    flows never cross-trigger: a "3 month" phrase cannot match any of the
    six-month phrases above and vice versa."""
    return any(
        phrase in message_lower for phrase in [
            "3 month follow up", "3-month follow up",
            "three month follow up", "three-month follow up",
            "3 month follow-up", "3-month follow-up",
            "three month follow-up", "three-month follow-up",
        ]
    )


def is_couple_six_month_followup_request(message_lower):
    return (
        bool(_JOINT_APPOINTMENT_REFERENCE_PATTERN.search(message_lower))
        and is_six_month_followup_request(message_lower)
    )


def generate_couple_followup_previous_visit_date():
    """Generate one shared prior visit that makes both spouses eligible
    for their routine six-month follow-up."""
    return (datetime.now() - timedelta(days=random.randint(180, 365))).strftime(
        "%B %d, %Y"
    )


def generate_couple_followup_availability():
    """Return routine follow-up slots as adjacent spouse appointment pairs."""
    pairs = []
    for day in random.sample(DAYS_OF_WEEK, 4):
        for index, first_time in enumerate(AVAILABLE_TIMES[:-1]):
            first_dt = datetime.strptime(first_time, "%I:%M %p")
            second_time = AVAILABLE_TIMES[index + 1]
            second_dt = datetime.strptime(second_time, "%I:%M %p")
            if (second_dt - first_dt).seconds == 30 * 60:
                pairs.append((day, first_time, second_time))
                break
    return pairs


def handle_couple_six_month_followup_flow(message):
    global couple_followup_flow_active, couple_followup_previous_visit_date
    global couple_followup_available_pairs
    global individual_six_month_followup_active
    global individual_six_month_followup_previous_visit_date
    global individual_six_month_followup_days_since
    global individual_six_month_followup_eligible
    global individual_six_month_followup_spouse_stage
    global individual_six_month_followup_spouse_slot
    global individual_six_month_followup_spouse_relation
    global individual_six_month_followup_spouse_first_name
    global individual_six_month_followup_spouse_last_name

    if couple_followup_previous_visit_date is None:
        couple_followup_previous_visit_date = generate_couple_followup_previous_visit_date()
    if couple_followup_available_pairs is None:
        couple_followup_available_pairs = generate_couple_followup_availability()
        choices = "; ".join(
            f"{day}: {first_time} and {second_time}"
            for day, first_time, second_time in couple_followup_available_pairs
        )
        return (
            f"Your shared previous appointment was on "
            f"{couple_followup_previous_visit_date}, so you are both due "
            f"for a six-month follow-up. I can schedule you back-to-back: "
            f"{choices}. Which day and starting time works best?"
        )

    selected_slot = _extract_slot_from_text(message)
    if selected_slot:
        for day, first_time, second_time in couple_followup_available_pairs:
            if selected_slot == f"{day} {first_time}":
                store_generic_appointment_record(
                    caller_first_name, caller_last_name,
                    f"{day} {first_time}",
                    reason=_derive_generic_appointment_reason(),
                )
                store_generic_appointment_record(
                    patient_first_name, patient_last_name,
                    f"{day} {second_time}",
                    reason=_derive_generic_appointment_reason(),
                )
                couple_followup_flow_active = False
                return (
                    f"Based on your shared previous appointment on "
                    f"{couple_followup_previous_visit_date}, I have you "
                    f"scheduled for {day} {first_time} and "
                    f"{patient_first_name} scheduled for {day} {second_time}."
                )
    return "Please choose one of the offered days and starting times."


def generate_weekly_availability(force_has_availability=None):
    weekly_schedule = {}
    for day in DAYS_OF_WEEK:
        if force_has_availability is not None:
            has_availability = force_has_availability
        else:
            has_availability = random.choice([True, True, True, False])
        if has_availability:
            num_slots = random.randint(2, 5)
            slots = random.sample(AVAILABLE_TIMES, num_slots)
            slots.sort(key=lambda x: datetime.strptime(x, "%I:%M %p"))
            weekly_schedule[day] = slots
        else:
            weekly_schedule[day] = []
    return weekly_schedule


def get_future_available_times():
    if not is_office_open_today():
        return []
    now = datetime.now()
    all_future_slots = []
    for slot in AVAILABLE_TIMES:
        slot_time = datetime.strptime(slot, "%I:%M %p").replace(
            year=now.year, month=now.month, day=now.day
        )
        if slot_time > now:
            all_future_slots.append(slot)
    if not all_future_slots:
        return []
    max_available = min(5, len(all_future_slots))
    weights = [10, 20, 25, 20, 15, 10][:max_available + 1]
    num_available = random.choices(
        range(0, max_available + 1),
        weights=weights, k=1
    )[0]
    if num_available == 0:
        return []
    return sorted(
        random.sample(all_future_slots, num_available),
        key=lambda x: datetime.strptime(x, "%I:%M %p")
    )


def aggressive_name_extraction(message):
    p0 = re.search(
        r"(?:my|patient(?:'s)?|the patient's)\s+name\s+is\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
        message, re.IGNORECASE
    )
    if p0:
        parts = p0.group(1).split()
        if len(parts) >= 2:
            return parts[0], parts[-1]
    p1 = re.search(
        r"(?:for patient|patient(?:'s name is)?|patient name is)\s+"
        r"([A-Z][a-z]+)\s+([A-Z][a-z]+)",
        message, re.IGNORECASE
    )
    if p1:
        return p1.group(1), p1.group(2)
    p2 = re.search(
        r"referral for\s+([A-Z][a-z]+)\s+([A-Z][a-z]+)",
        message, re.IGNORECASE
    )
    if p2:
        return p2.group(1), p2.group(2)
    p3 = re.search(
        r"received a referral for\s+([A-Z][a-z]+)\s+([A-Z][a-z]+)",
        message, re.IGNORECASE
    )
    if p3:
        return p3.group(1), p3.group(2)
    p4 = re.search(
        r"(?:regarding|about)\s+([A-Z][a-z]+)\s+([A-Z][a-z]+)",
        message, re.IGNORECASE
    )
    if p4:
        return p4.group(1), p4.group(2)
    p5 = re.search(
        r"\bpatient\b.{0,30}?([A-Z][a-z]+)\s+([A-Z][a-z]+)",
        message, re.IGNORECASE
    )
    if p5:
        return p5.group(1), p5.group(2)
    if any(word in message.lower() for word in ["referral", "patient", "calling about"]):
        p6 = re.search(
            r"\b([A-Z][a-z]{1,15})\s+([A-Z][a-z]{1,15})\b",
            message
        )
        if p6:
            exclude = {
                "Dr", "This", "From", "Please", "Thank", "Hello",
                "We", "Our", "The", "His", "Her", "Your", "My",
                "For", "And", "Also", "Could", "Would", "Should",
                "Just", "Know", "Have", "Need", "Been", "With",
                "Sykes", "Creek", "Primary", "Care"
            }
            first, last = p6.group(1), p6.group(2)
            if first not in exclude and last not in exclude:
                return first, last
            return None, None
    return None, None


NEW_PATIENT_PROCEED_PHRASES = [
    "yes", "sure", "okay", "ok", "i would", "i'd like", "i would like",
    "let's do it", "sounds good", "please", "still like", "still want",
    "establish", "schedule me", "handle that", "i can handle", "go ahead",
    "that works", "works for me", "sign me up", "book me", "set me up",
]

NEW_PATIENT_DECLINE_PHRASES = [
    "no", "not right now", "no thank you", "not interested",
    "don't think so", "do not think so",
]

NON_HF_MEDICARE_ADVANTAGE_MARKERS = [
    "united", "humana", "aetna", "cigna", "anthem", "wellcare",
    "devoted", "sunshine", "simply", "molina", "careplus", "freedom",
]


def patient_wants_to_proceed(message_lower):
    if any(phrase in message_lower for phrase in NEW_PATIENT_DECLINE_PHRASES):
        return False
    return any(phrase in message_lower for phrase in NEW_PATIENT_PROCEED_PHRASES)


def patient_wants_to_decline(message_lower):
    return any(phrase in message_lower for phrase in NEW_PATIENT_DECLINE_PHRASES)


# RT13 fix: general conversation-closing phrase list, used when Steve's
# own last message asked "anything else I can help you with today?" -
# see is_conversation_closing_reply() below. Matched against the FULL
# message after normalization, not as a substring, so a continuation
# like "No, actually I have another question" is correctly NOT treated
# as closing intent (it normalizes to something other than one of
# these exact phrases).
CONVERSATION_CLOSING_PHRASES = [
    "no", "no thank you", "no thanks", "nope", "nope thank you",
    "nope thanks", "nothing else", "that's all", "thats all",
    "that's all thank you", "thats all thank you", "that will be all",
    "i'm good", "im good", "no i'm good", "no im good",
    "i'm all set", "im all set", "no i'm all set", "no im all set",
    "all set", "that's it", "thats it",
    "no sir", "no ma'am", "no i am all set",
    "no that's all", "no that is all",
    "no that's everything", "no that is everything",
    "no i am good", "no thank you that's all", "no thank you that is all",
    "no have a good day", "no that's it", "no thats it",
    "no we're good", "no we are good",
    "no i don't need anything else", "no i do not need anything else",
    "have a good day", "have a great day",
    "no you have been very helpful", "you have been very helpful",
    "no you've been very helpful", "you've been very helpful",
    "no you have been so helpful", "you have been so helpful",
    "no you were very helpful", "you were very helpful",
    "no you've been so helpful", "you've been so helpful",
    "no you've been really helpful", "you've been really helpful",
    "thank you for your help", "no thank you for your help",
    "thank you for all your help", "no thank you for all your help",
    "thanks for your help", "no thanks for your help",
    "thanks for all your help", "no thanks for all your help",
    "no that's all you've been very helpful",
    "no that is all you have been very helpful",
    "that's all you've been very helpful",
    "thats all you've been very helpful",
    "that's all you have been very helpful",
    "thats all you have been very helpful",
]


def is_conversation_closing_reply(message_lower):
    """Returns True only if the ENTIRE message, after stripping
    trailing punctuation (periods, commas, exclamation points, question
    marks) and collapsing whitespace, exactly matches a known closing
    phrase - not a substring match. This is what makes "No. Thank you."
    equivalent to "No thank you" (the period+space between them is the
    only difference) without needing every punctuated variant spelled
    out separately, while still refusing to match a longer reply that
    merely starts with "no" but continues into a new request."""
    normalized = re.sub(r"[.,!?]", "", message_lower).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized in CONVERSATION_CLOSING_PHRASES


def detect_self_pay_intent(message_lower):
    self_pay_phrases = [
        "self pay", "self-pay", "out of pocket", "out-of-pocket",
        "pay out of pocket", "cash pay", "paying out of pocket",
    ]
    knows_not_accepted = any(
        phrase in message_lower for phrase in [
            "you don't take", "you do not take", "not accepted",
            "don't accept", "do not accept", "isn't accepted",
            "is not accepted", "will not accept", "won't accept",
            "doesn't accept", "does not accept", "not accept",
        ]
    )
    return (
            any(phrase in message_lower for phrase in self_pay_phrases)
            or knows_not_accepted
    )


def self_pay_cost_quote_message():
    return (
        "A new patient appointment will cost $90 to $130. "
        "It might cost more but it might cost less. This is just "
        "an estimate. Would you still like to set up a new "
        "patient appointment?"
    )


def medicare_advantage_oon_message(provider):
    return (
        "The amount that you owe is contingent upon your out of "
        "network benefits. I recommend checking with your plan to "
        "see how much they will cover. Would you still like to "
        f"establish with {provider}?"
    )


def new_patient_goodbye_message():
    return (
        "No problem at all. Have a great day. Is there anything "
        "else I can help you with today?"
    )


def start_demographics_name_question(is_minor):
    if is_minor:
        return (
            "Could I get the first and last name of the patient "
            "we will be scheduling the appointment for?"
        )
    return "Could I get your first and last name?"


def handle_med_pro_collection(message, message_lower):
    global med_pro_patient_first, med_pro_patient_last
    global med_pro_patient_dob, med_pro_patient_pcp
    global med_pro_collection_complete
    global med_pro_referral_looked_up, med_pro_referral_status
    global med_pro_referral_date, med_pro_referral_provider
    global med_pro_referral_reason
    global pcp_collected

    if not med_pro_patient_first or not med_pro_patient_last:
        first, last = aggressive_name_extraction(message)
        if first and last:
            med_pro_patient_first = first
            med_pro_patient_last = last
        else:
            # Fallback for bare "Firstname Lastname" reply
            parts = message.strip().split()
            if len(parts) == 2 and parts[0][0].isupper() and parts[1][0].isupper():
                med_pro_patient_first = parts[0]
                med_pro_patient_last = parts[1]

    if not med_pro_patient_dob and detect_dob_in_message(message):
        med_pro_patient_dob = extract_dob_from_message(message)

    if not med_pro_patient_pcp:
        provider = detect_provider_in_message(message_lower)
        if provider:
            med_pro_patient_pcp = provider
            pcp_collected = True

    patient_full = None
    if med_pro_patient_first and med_pro_patient_last:
        patient_full = f"{med_pro_patient_first} {med_pro_patient_last}"

    if not patient_full:
        return (
            "I'd be happy to help with that. Could you please provide "
            "me with the patient's first and last name?"
        )
    if not med_pro_patient_dob:
        return f"Thank you. Could I get {patient_full}'s date of birth?"
    if not med_pro_patient_pcp:
        return (
            f"Just so you know we have multiple providers in our "
            f"practice. Could you tell me which provider is "
            f"{patient_full}'s primary care physician here at "
            f"Sykes Creek Primary Care?"
        )

    if not med_pro_referral_looked_up:
        forced = get_harness_override("STEVE_FORCE_REFERRAL_FOUND")
        if forced is not None:
            med_pro_referral_status = "FOUND" if forced else "NOT_FOUND"
        else:
            med_pro_referral_status = (
                "FOUND" if random.randint(0, 1) == 0 else "NOT_FOUND"
            )
        med_pro_referral_looked_up = True
        if med_pro_referral_status == "FOUND":
            med_pro_referral_date, med_pro_referral_provider, \
                med_pro_referral_reason = generate_referral_details(
                patient_pcp=med_pro_patient_pcp
            )
            med_pro_collection_complete = True

    if med_pro_referral_status == "FOUND":
        return (
            f"One moment while I pull up {patient_full}'s chart. "
            f"(pause) I do have a referral on file for {patient_full}. "
            f"According to our records a referral was sent on "
            f"{med_pro_referral_date} by {med_pro_referral_provider} "
            f"for {med_pro_referral_reason}. "
            f"Is that the referral you are calling about?"
        )
    else:
        med_pro_collection_complete = True  # Mark complete even if not found to allow follow up
        return (
            f"One moment while I pull up {patient_full}'s chart. "
            f"(pause) I'm sorry but I was not able to locate a "
            f"referral for {patient_full} in our system. Could you "
            f"tell me a little more about the referral you are "
            f"looking for, such as when it was sent and the reason? "
            f"I will flag this for the provider and have someone "
            f"follow up with you. May I get your fax number and "
            f"best callback number?"
        )


def _new_patient_after_consent_response():
    """Shared completion logic that runs once text-message consent has
    been determined - either answered directly, or recorded as
    declined because the second household member wasn't available to
    answer themselves (see the household_consent_availability stage).
    Extracted out of the text_consent stage so both paths share it
    instead of duplicating the appointment-availability logic."""
    global new_patient_demographics_stage, new_patient_household_stage
    global new_patient_appointment_selection, new_patient_weekly_schedule
    global caller_first_name, new_patient_first_name, new_patient_offered_provider
    global new_patient_household_second_time, new_patient_household_primary_first_name
    global new_patient_household_relation, new_patient_last_name
    global new_patient_household_speaker_active
    if (
            new_patient_household_stage == "secondary"
            and new_patient_household_second_time
    ):
        # Household fix: the time for this household member was
        # already captured from the original combined scheduling
        # request (e.g. "...and please schedule my husband for 2:00
        # PM that same day") - don't present availability and ask
        # again, just confirm it.
        new_patient_appointment_selection = new_patient_household_second_time
        new_patient_demographics_stage = "complete"
        new_patient_household_stage = "done"
        # Bug fix: this shortcut confirms the second household
        # member's appointment directly (skipping the appointment_time
        # stage entirely), so it needs its own
        # store_generic_appointment_record() call - the one in the
        # appointment_time stage never runs for this path.
        store_generic_appointment_record(
            new_patient_first_name, new_patient_last_name,
            new_patient_appointment_selection,
            provider=new_patient_offered_provider,
            reason=_derive_generic_appointment_reason(),
        )
        address_name = (
                caller_first_name or new_patient_household_primary_first_name
        )
        # Bug fix: if the household member themselves took the line for
        # consent (household_speaker_transfer -> household_speaker_verify),
        # they are the one Steve is speaking to now, so address them
        # directly rather than as "your <relation>" of the caller.
        target = (
            "you" if new_patient_household_speaker_active
            else f"your {new_patient_household_relation}"
        )
        return (
            f"Thank you. I have {target} "
            f"scheduled with {new_patient_offered_provider} for "
            f"{new_patient_appointment_selection}. You're both all "
            f"set. Is there anything else I can help you with today?"
        )
    new_patient_demographics_stage = "appointment_time"
    schedule = generate_weekly_availability()
    # Bug fix (holiday): this workflow calls generate_weekly_availability()
    # directly and builds its own availability text in Python,
    # entirely bypassing the availability_context/same_day_context
    # holiday filtering used by standard scheduling. That existing
    # filtering works by computing each weekday name's real
    # calendar date and checking it against the holiday calendar -
    # mirrored here rather than changed, since generate_weekly_
    # availability() itself only knows weekday names, not real
    # dates, and has no way to know which real date a name like
    # "Monday" resolves to this week.
    _new_patient_weekday_to_index = {
        "Monday": 0, "Tuesday": 1, "Wednesday": 2,
        "Thursday": 3, "Friday": 4,
    }
    _new_patient_today = datetime.now()
    _new_patient_today_index = _new_patient_today.weekday()
    for _np_day_name, _np_idx in _new_patient_weekday_to_index.items():
        if _np_day_name not in schedule:
            continue
        if _np_idx == _new_patient_today_index:
            # Bug fix (same-day): new-patient appointments should
            # never be offered same-day. The spoken response only
            # ever states a bare weekday name with no date attached
            # (unlike wellness scheduling, which states a real date),
            # so if today happens to be, say, Monday, presenting
            # "Monday: ..." reads as a same-day offer to the caller
            # even if it were silently resolved to next Monday
            # internally - exclude today's own weekday entirely
            # rather than resolve-and-relabel it.
            schedule[_np_day_name] = []
            continue
        _np_delta = (_np_idx - _new_patient_today_index) % 7
        _np_real_date = (
                _new_patient_today + timedelta(days=_np_delta)
        ).date()
        if is_recognized_office_holiday(_np_real_date):
            schedule[_np_day_name] = []
    available_days = [
        day for day, slots in schedule.items() if slots
    ]
    new_patient_weekly_schedule = schedule
    if available_days:
        lines = []
        for day in available_days:
            slots_str = ", ".join(schedule[day])
            lines.append(f"{day}: {slots_str}")
        availability_text = "; ".join(lines)
        return (
            f"Great, now let's get an appointment on the books. "
            f"Here is what we have available this week - "
            f"{availability_text}. Which day and time works "
            f"best for you?"
        )
    else:
        new_patient_demographics_stage = "complete"
        address_name = caller_first_name if caller_first_name else new_patient_first_name
        return (
            f"Thank you, {address_name}. I have everything I "
            f"need to get {new_patient_first_name} set up as a "
            f"new patient with {new_patient_offered_provider}. "
            f"We do not have any availability this week, so "
            f"someone from our office will follow up with you "
            f"directly to find the best time. Is there anything "
            f"else I can help you with today?"
        )


def _household_consent_availability_question(relation):
    """Bug fix: previously the consent question was always addressed
    to the caller ("do we have YOUR consent to text HIM"), even for a
    second adult household member - meaning Sarah would be giving SMS
    consent on Sam's behalf. Consent is now asked of the second person
    directly whenever they're available on the call."""
    reflexive = (
        "himself" if relation == "husband"
        else "herself" if relation == "wife"
        else "themselves"
    )
    return (
        f"For the last question, I would need your {relation} to "
        f"provide consent {reflexive}. Is your {relation} available?"
    )


def handle_new_patient_flow(message, message_lower):
    global new_patient_flow_active, new_patient_requested_provider
    global new_patient_is_minor, new_patient_minor_age
    global new_patient_parent_on_line_pending
    global caller_first_name, caller_last_name
    global new_patient_eligible_providers, new_patient_offered_provider
    global new_patient_accepting_checked, new_patient_no_provider_available
    global new_patient_insurance_collected, new_patient_insurance_type
    global new_patient_insurance_name, new_patient_insurance_accepted
    global new_patient_insurance_pending_oon, new_patient_self_pay_intent
    global new_patient_demographics_stage
    global new_patient_first_name, new_patient_last_name, new_patient_dob
    global new_patient_street_address, new_patient_email, new_patient_phone
    global new_patient_member_number, new_patient_text_consent
    global new_patient_weekly_schedule, new_patient_appointment_selection
    global new_patient_household_relation, new_patient_household_stage
    global new_patient_household_second_time, new_patient_household_primary_first_name
    global new_patient_household_primary_address
    global new_patient_household_speaker_active

    print(f"DEBUG STAGE_ENTRY: message={message_lower!r} "
          f"insurance_type={new_patient_insurance_type!r} "
          f"insurance_collected={new_patient_insurance_collected!r} "
          f"is_minor={new_patient_is_minor!r} "
          f"accepting_checked={new_patient_accepting_checked!r} "
          f"offered_provider={new_patient_offered_provider!r}")

    # Bug fix: a caller often states their own name in the very same
    # message that also identifies them as a new patient (e.g. "This
    # is Sarah Smith. I'm not a patient yet. I'm looking to schedule
    # new patient appointments for myself and my spouse..."). Unlike
    # determine_pre_chart_response() (which never runs for this flow -
    # new_patient_flow_active short-circuits it entirely), nothing in
    # handle_new_patient_flow() itself ever looked at a message for a
    # name until the "name" demographics stage is reached, many turns
    # later - so a name stated up front was silently discarded, and
    # the "name" stage re-asked for something already given. Captures
    # it opportunistically, once, the first time it appears in any
    # turn's message; the "name" stage skip logic just below uses it.
    if new_patient_first_name is None:
        _early_name = extract_names_from_message(message)
        if _early_name["caller_first"] and _early_name["caller_last"]:
            new_patient_first_name = _early_name["caller_first"]
            new_patient_last_name = _early_name["caller_last"]

    if detect_self_pay_intent(message_lower):
        new_patient_self_pay_intent = True

    if (
            new_patient_self_pay_intent
            and not new_patient_requested_provider
            and not new_patient_accepting_checked
            and new_patient_insurance_type is None
            and not new_patient_insurance_collected
    ):
        new_patient_insurance_type = "self_pay_quoted"
        new_patient_insurance_accepted = False
        return self_pay_cost_quote_message()

    proceed_phrases = NEW_PATIENT_PROCEED_PHRASES
    decline_phrases = NEW_PATIENT_DECLINE_PHRASES

    # ---- STAGE 0: Detect if this is for a minor ----
    if new_patient_is_minor is None:
        # Use a flexible regex instead of exact phrases, since parents
        # commonly insert an age description between "my" and the
        # relationship word (e.g. "my 16 year old son", "my 8 year
        # old daughter"). The regex allows up to a handful of words
        # in between rather than requiring exact adjacency.
        minor_relationship_pattern = re.search(
            r"\bmy\s+(?:[\w'-]+\s+){0,4}(son|daughter|child|kid|boy|girl)\b",
            message_lower
        )
        # Also catch explicit age statements under 18 anywhere nearby,
        # even if the relationship word is phrased unusually.
        age_under_18_pattern = re.search(
            r"\b(\d{1,2})\s*(?:year[\s-]old|yo|years\s+old)\b",
            message_lower
        )
        explicit_minor_age = False
        if age_under_18_pattern:
            try:
                stated_age = int(age_under_18_pattern.group(1))
                if stated_age < 18:
                    explicit_minor_age = True
            except ValueError:
                pass

        if minor_relationship_pattern or explicit_minor_age:
            new_patient_is_minor = True
        else:
            new_patient_is_minor = False

    # ---- EARLY INSURANCE CHECK: Medicaid stated before provider chosen ----
    # If the caller already told us they have Medicaid before we ever asked
    # about insurance, don't make them sit through the provider/
    # availability questions first only to be denied afterward. Deny it
    # and quote self-pay immediately, per Don's spec.
    if (
            new_patient_insurance_type is None
            and not new_patient_insurance_collected
            and any(t in message_lower for t in MEDICAID_TRIGGERS)
    ):
        new_patient_insurance_type = "medicaid_denied"
        new_patient_insurance_collected = False
        return (
                "I'm sorry but we do not accept Medicaid at this practice. "
                "I apologize for the inconvenience. " + self_pay_cost_quote_message()
        )

    # Resolve the self-pay follow-up here, before STAGE 1 below gets a
    # chance to misread a plain yes/no as a failed provider name. This
    # only applies when Medicaid was mentioned before a provider was ever
    # chosen; STAGE 3B further below still handles the case where
    # Medicaid comes up after a provider has already been selected.
    if (
            new_patient_insurance_type == "medicaid_denied"
            and not new_patient_insurance_collected
            and not new_patient_requested_provider
    ):
        if patient_wants_to_proceed(message_lower):
            new_patient_insurance_collected = True
            new_patient_insurance_accepted = True
            return (
                "Great, thank you. Which primary care provider are you "
                "interested in seeing at our practice?"
            )
        if patient_wants_to_decline(message_lower):
            new_patient_insurance_collected = True
            new_patient_insurance_accepted = False
            return new_patient_goodbye_message()
        return self_pay_cost_quote_message()

    # ---- STAGE 1: Determine which provider is being asked about ----
    if not new_patient_requested_provider:
        provider = detect_provider_in_message(message_lower)
        if provider:
            new_patient_requested_provider = provider
        else:
            return (
                "Which primary care provider are you interested in "
                "seeing at our practice?"
            )

    # ---- STAGE 2: Minor-eligibility + availability intersection ----
    if not new_patient_accepting_checked:
        new_patient_accepting_checked = True
        availability = generate_new_patient_provider_availability()

        if new_patient_is_minor:
            eligible = determine_eligible_minor_providers(availability)
            new_patient_eligible_providers = eligible

            if new_patient_requested_provider in MINOR_ELIGIBLE_PROVIDERS:
                # The requested provider DOES see minors - check if
                # that specific provider survived the random filter
                if new_patient_requested_provider in eligible:
                    new_patient_offered_provider = new_patient_requested_provider
                    return (
                        f"Good news, {new_patient_requested_provider} does "
                        f"see patients under 18 and is currently accepting "
                        f"new patients. Would you like to schedule a new "
                        f"patient appointment?"
                    )
                else:
                    # Requested provider sees minors but panel is full -
                    # offer another eligible provider from the intersection
                    if eligible:
                        new_patient_offered_provider = eligible[0]
                        return (
                            f"I'm sorry but {new_patient_requested_provider}'s "
                            f"panel is currently closed to new patients. "
                            f"However, {eligible[0]} does see patients "
                            f"under 18 and is currently accepting new "
                            f"patients. Would you like to schedule with "
                            f"{eligible[0]} instead?"
                        )
                    else:
                        new_patient_no_provider_available = True
                        return (
                            "I'm sorry but none of our providers who see "
                            "patients under 18 are currently accepting new "
                            "patients. I apologize for the inconvenience. "
                            "I wish you luck in finding a primary care "
                            "provider for your child. Have a nice day."
                        )
            else:
                # Requested provider does NOT see minors at all -
                # immediately offer from the eligible intersection,
                # regardless of the requested provider's own availability
                if eligible:
                    new_patient_offered_provider = eligible[0]
                    return (
                        f"I'm sorry but {new_patient_requested_provider} "
                        f"does not see patients under the age of 18. "
                        f"However, {eligible[0]} does see patients under "
                        f"18 and is currently accepting new patients. "
                        f"Would you like to schedule with {eligible[0]} "
                        f"instead?"
                    )
                else:
                    new_patient_no_provider_available = True
                    return (
                        "I'm sorry but none of our providers who see "
                        "patients under 18 are currently accepting new "
                        "patients. I apologize for the inconvenience. "
                        "I wish you luck in finding a primary care "
                        "provider for your child. Have a nice day."
                    )
        else:
            # Adult patient - simple random check on the requested
            # provider only, with up to two alternate suggestions if
            # the requested provider's panel is closed
            is_accepting = availability.get(new_patient_requested_provider, False)
            if is_accepting:
                new_patient_offered_provider = new_patient_requested_provider
                already_expressed_intent = any(
                    phrase in message_lower for phrase in [
                        "establish", "schedule", "set up an appointment",
                        "set up a new patient appointment",
                        "new patient appointment", "want to set up",
                        "make an appointment", "book an appointment",
                        "get an appointment"
                    ]
                )
                if already_expressed_intent:
                    if detect_self_pay_intent(message_lower):
                        new_patient_self_pay_intent = True
                        new_patient_insurance_type = "self_pay_quoted"
                        new_patient_insurance_accepted = False
                        return self_pay_cost_quote_message()
                    return (
                        f"I can set you up with a new patient "
                        f"appointment with {new_patient_requested_provider}. "
                        f"What insurance do you have?"
                    )
                return (
                    f"{new_patient_requested_provider} is currently "
                    f"accepting new patients. Would you like to schedule "
                    f"a new patient appointment?"
                )
            else:
                alternates = [
                    p for p, accepting in availability.items()
                    if accepting and p != new_patient_requested_provider
                ]
                if alternates:
                    suggestion = random.choice(alternates)
                    new_patient_offered_provider = suggestion
                    return (
                        f"I'm sorry but {new_patient_requested_provider}'s "
                        f"panel is currently closed to new patients. "
                        f"{suggestion} is currently accepting new patients. "
                        f"Would you like to schedule with {suggestion} "
                        f"instead?"
                    )
                else:
                    new_patient_no_provider_available = True
                    return (
                        "I'm sorry but none of our providers are currently "
                        "accepting new patients at this time. I apologize "
                        "for the inconvenience. Have a nice day."
                    )

    if new_patient_no_provider_available:
        return (
            "I'm sorry but none of our providers are currently accepting "
            "new patients at this time. I apologize for the inconvenience. "
            "Have a nice day."
        )

    # ---- STAGE 3: Confirm patient wants to proceed with scheduling ----
    if not new_patient_insurance_collected and new_patient_insurance_type is None:
        if patient_wants_to_decline(message_lower):
            return new_patient_goodbye_message()

        if new_patient_self_pay_intent or detect_self_pay_intent(message_lower):
            new_patient_self_pay_intent = True
            new_patient_insurance_type = "self_pay_quoted"
            new_patient_insurance_accepted = False
            return self_pay_cost_quote_message()

        # ---- STAGE 4: Determine insurance type ----
        if any(t in message_lower for t in MEDICAID_TRIGGERS):
            new_patient_insurance_type = "medicaid_denied"
            new_patient_insurance_collected = False
            if new_patient_is_minor:
                return (
                    f"I'm sorry but we do not accept Medicaid at this "
                    f"practice. I apologize for the inconvenience. Would "
                    f"you still like to have your son or daughter "
                    f"establish with {new_patient_offered_provider} as "
                    f"a self-pay patient?"
                )
            return (
                f"I'm sorry but we do not accept Medicaid at this "
                f"practice. I apologize for the inconvenience. Would "
                f"you still like to establish with "
                f"{new_patient_offered_provider} as a self-pay patient?"
            )

        if any(t in message_lower for t in MARKETPLACE_TRIGGERS):
            new_patient_insurance_type = "marketplace"
            return (
                "Does your marketplace plan happen to be through Health "
                "First Health Plans?"
            )

        if any(t in message_lower for t in MEDICARE_ADVANTAGE_TRIGGERS):
            new_patient_insurance_type = "medicare_advantage"
            hf_confirmed = (
                    "health first" in message_lower
                    and not any(
                p in message_lower for p in [
                    "not health first", "isn't health first",
                    "is not health first", "no it is not", "no it's not",
                ]
            )
            )
            non_hf_ma = any(
                marker in message_lower for marker in NON_HF_MEDICARE_ADVANTAGE_MARKERS
            ) and not hf_confirmed
            if hf_confirmed:
                new_patient_insurance_collected = True
                new_patient_insurance_accepted = True
                return (
                    "Great news, we do accept that insurance. Could I get "
                    "your first and last name to get started?"
                )
            if non_hf_ma or patient_wants_to_decline(message_lower):
                new_patient_insurance_collected = True
                new_patient_insurance_accepted = False
                new_patient_insurance_pending_oon = True
                return medicare_advantage_oon_message(new_patient_offered_provider)
            return (
                "Is your Medicare Advantage plan through Health First "
                "Health Plans?"
            )

        if any(t in message_lower for t in MEDICARE_TRIGGERS):
            new_patient_insurance_type = "medicare_unspecified"
            return (
                "Is that a Medicare Advantage plan, or original Medicare?"
            )

        commercial_match = detect_insurance_name(message_lower)
        if commercial_match and new_patient_insurance_type is None:
            new_patient_insurance_name = commercial_match
            if (
                    "commercial" in message_lower
                    or is_employer_sponsored_insurance_phrasing(message_lower)
            ):
                new_patient_insurance_type = "commercial"
                new_patient_insurance_collected = True
                new_patient_insurance_accepted = True
                if new_patient_is_minor:
                    return (
                        "Great news, we do accept that insurance. Could I "
                        "get the first and last name of the patient we "
                        "will be scheduling the appointment for?"
                    )
                if new_patient_first_name and new_patient_last_name:
                    new_patient_demographics_stage = "dob"
                    return "We do accept that insurance. Could I get your date of birth?"
                return (
                    "Great news, we do accept that insurance. Could I get "
                    "your first and last name to get started?"
                )

            # Do NOT assume commercial yet - ask the clarifying
            # question first, per Don's spec. The insurance NAME is
            # remembered so we don't have to ask for it again once
            # the type is confirmed.
            new_patient_insurance_type = "pending_clarification"
            return (
                "Is that commercial insurance through an employer, "
                "Medicaid, or a Marketplace plan?"
            )

        if patient_wants_to_proceed(message_lower):
            if new_patient_self_pay_intent:
                new_patient_insurance_type = "self_pay_quoted"
                new_patient_insurance_accepted = False
                return self_pay_cost_quote_message()
            return "What insurance do you have?"

        # Patient asked a clarifying question instead of answering
        # yes/no (e.g. "are any other providers accepting new
        # patients") - this is NOT an insurance name, do not treat it
        # as one. Re-confirm what was already offered and ask again.
        other_provider_question_phrases = [
            "other provider", "anyone else", "anybody else",
            "any other doctor", "different provider", "another provider",
            "any other providers", "who else", "what other"
        ]
        if any(phrase in message_lower for phrase in other_provider_question_phrases):
            if new_patient_offered_provider:
                return (
                    f"At this time {new_patient_offered_provider} is the "
                    f"only provider I have available for new patients "
                    f"matching your request. Would you like to schedule "
                    f"with {new_patient_offered_provider}?"
                )
            return (
                "Could you tell me which provider you would like to "
                "schedule with?"
            )

        # Unrecognized insurance name - ask clarifying question first
        new_patient_insurance_name = message.strip()
        new_patient_insurance_type = "pending_clarification"
        return (
            "Is that commercial insurance through an employer, "
            "Medicaid, or a Marketplace plan?"
        )
        new_patient_insurance_collected = True
        if new_patient_insurance_accepted:
            if new_patient_is_minor:
                return (
                    "Great news, we do accept that insurance. Could I "
                    "get the first and last name of the patient we will "
                    "be scheduling the appointment for?"
                )
            return (
                "Great news, we do accept that insurance. Could I get "
                "your first and last name to get started?"
            )
        else:
            return (
                "It can be $90 to $130 typically but it could be more "
                "or less depending on what's done. This is just an "
                "estimate."
            )

    # ---- STAGE 3B: Resolve Medicaid self-pay follow-up question ----
    if new_patient_insurance_type == "medicaid_denied" and not new_patient_insurance_collected:
        if patient_wants_to_proceed(message_lower):
            new_patient_insurance_collected = True
            new_patient_insurance_accepted = True
            return start_demographics_name_question(new_patient_is_minor)
        if patient_wants_to_decline(message_lower):
            new_patient_insurance_collected = True
            new_patient_insurance_accepted = False
            return new_patient_goodbye_message()
        return (
            "It can be $90 to $130 typically but it could be more "
            "or less depending on what's done. This is just an "
            "estimate."
        )

    # ---- STAGE 3C: Resolve marketplace/commercial denial self-pay follow-up ----
    if new_patient_insurance_type == "marketplace_denied" and not new_patient_insurance_collected:
        new_patient_insurance_collected = True
        new_patient_insurance_accepted = False
        if any(phrase in message_lower for phrase in proceed_phrases):
            return (
                "It can be $90 to $130 typically but it could be more "
                "or less depending on what's done. This is just an "
                "estimate."
            )
        return (
            "No problem at all. Have a great day, and please feel free "
            "to call back if you change your mind."
        )
    # ---- STAGE 3D: Resolve self-pay-quote confirmation ----
    if new_patient_insurance_type == "self_pay_quoted" and not new_patient_insurance_collected:
        if patient_wants_to_proceed(message_lower):
            new_patient_insurance_collected = True
            new_patient_insurance_accepted = True
            return start_demographics_name_question(new_patient_is_minor)
        if patient_wants_to_decline(message_lower):
            new_patient_insurance_collected = True
            new_patient_insurance_accepted = False
            return new_patient_goodbye_message()
        return self_pay_cost_quote_message()

    # ---- STAGE 4A: Resolve pending insurance type clarification ----
    print(f"DEBUG REACHED_STAGE_4A: insurance_type={new_patient_insurance_type!r}")
    if new_patient_insurance_type == "pending_clarification":
        print(f"DEBUG ENTERING_4A_BLOCK: message={message_lower!r}")
        if any(t in message_lower for t in MEDICAID_TRIGGERS):
            new_patient_insurance_type = "medicaid_denied"
            new_patient_insurance_collected = False
            if new_patient_is_minor:
                return (
                    f"I'm sorry but we do not accept Medicaid at this "
                    f"practice. I apologize for the inconvenience. Would "
                    f"you still like to have your son or daughter "
                    f"establish with {new_patient_offered_provider} as "
                    f"a self-pay patient?"
                )
            return (
                f"I'm sorry but we do not accept Medicaid at this "
                f"practice. I apologize for the inconvenience. Would "
                f"you still like to establish with "
                f"{new_patient_offered_provider} as a self-pay patient?"
            )
        if any(t in message_lower for t in MARKETPLACE_TRIGGERS) or "marketplace" in message_lower:
            new_patient_insurance_type = "marketplace"
            # Check the insurance NAME already given earlier in the
            # call, rather than asking a redundant question the
            # patient already answered indirectly.
            known_name_lower = (new_patient_insurance_name or "").lower()
            if "health first" in known_name_lower:
                new_patient_insurance_accepted = True
                new_patient_insurance_collected = True
                if new_patient_is_minor:
                    return (
                        "Great news, we do accept that insurance. Could "
                        "I get the first and last name of the patient we "
                        "will be scheduling the appointment for?"
                    )
                return (
                    "Great news, we do accept that insurance. Could I "
                    "get your first and last name to get started?"
                )
            elif known_name_lower and known_name_lower != "marketplace":
                new_patient_insurance_type = "marketplace_denied"
                new_patient_insurance_collected = False
                if new_patient_is_minor:
                    return (
                        f"I'm sorry but we do not accept that insurance. "
                        f"Would you still like me to get your son or "
                        f"daughter established with "
                        f"{new_patient_offered_provider}?"
                    )
                return (
                    f"I'm sorry but we do not accept that insurance. "
                    f"Would you still like to establish with "
                    f"{new_patient_offered_provider}?"
                )
            return (
                "Does your marketplace plan happen to be through Health "
                "First Health Plans?"
            )
        # Default: treat as commercial/employer insurance. Employer
        # plans should move forward immediately; known accepted-list
        # names also auto-accept. Unrecognized names keep the prior
        # random fallback behavior.
        new_patient_insurance_type = "commercial"
        new_patient_insurance_collected = True
        employer_plan = is_employer_sponsored_insurance_phrasing(message_lower)
        known_name_match = (
                new_patient_insurance_name and
                detect_insurance_name(new_patient_insurance_name.lower())
        )
        if employer_plan or known_name_match:
            new_patient_insurance_accepted = True
        else:
            new_patient_insurance_accepted = random.choice([True, False])

        if new_patient_insurance_accepted:
            if new_patient_is_minor:
                return (
                    "Great news, we do accept that insurance. Could I "
                    "get the first and last name of the patient we will "
                    "be scheduling the appointment for?"
                )
            return (
                "Great news, we do accept that insurance. Could I get "
                "your first and last name to get started?"
            )
        return (
            "It can be $90 to $130 typically but it could be more "
            "or less depending on what's done. This is just an "
            "estimate."
        )

    # ---- STAGE 4B: Resolve marketplace / medicare advantage follow-up ----
    if new_patient_insurance_type == "marketplace" and not new_patient_insurance_collected:
        if any(phrase in message_lower for phrase in proceed_phrases) or "health first" in message_lower:
            new_patient_insurance_accepted = True
        else:
            new_patient_insurance_accepted = False
        new_patient_insurance_collected = True
        if new_patient_insurance_accepted:
            if new_patient_is_minor:
                return (
                    "Great news, we do accept that insurance. Could I "
                    "get the first and last name of the patient we will "
                    "be scheduling the appointment for?"
                )
            return (
                "Great news, we do accept that insurance. Could I get "
                "your first and last name to get started?"
            )
        else:
            return (
                "It can be $90 to $130 typically but it could be more "
                "or less depending on what's done. This is just an "
                "estimate."
            )

    if new_patient_insurance_type == "medicare_advantage" and not new_patient_insurance_collected:
        hf_confirmed = (
                "health first" in message_lower
                and not any(
            p in message_lower for p in [
                "not health first", "isn't health first",
                "is not health first", "no it is not", "no it's not",
            ]
        )
        )
        non_hf_ma = any(
            marker in message_lower for marker in NON_HF_MEDICARE_ADVANTAGE_MARKERS
        ) and not hf_confirmed

        if hf_confirmed:
            new_patient_insurance_collected = True
            new_patient_insurance_accepted = True
            return (
                "Great news, we do accept that insurance. Could I get "
                "your first and last name to get started?"
            )
        if non_hf_ma or patient_wants_to_decline(message_lower):
            new_patient_insurance_collected = True
            new_patient_insurance_accepted = False
            new_patient_insurance_pending_oon = True
            return medicare_advantage_oon_message(new_patient_offered_provider)
        return (
            "Is your Medicare Advantage plan through Health First "
            "Health Plans?"
        )

    if new_patient_insurance_type == "medicare_unspecified":
        if "advantage" in message_lower:
            new_patient_insurance_type = "medicare_advantage"
            return (
                "Is your Medicare Advantage plan through Health First "
                "Health Plans?"
            )
        else:
            new_patient_insurance_type = "medicare_advantage"
            new_patient_insurance_collected = True
            new_patient_insurance_accepted = False
            new_patient_insurance_pending_oon = True
            return medicare_advantage_oon_message(new_patient_offered_provider)

    # ---- STAGE 3E: Medicare Advantage OON follow-up ----
    if new_patient_insurance_pending_oon:
        if patient_wants_to_proceed(message_lower):
            new_patient_insurance_pending_oon = False
            new_patient_insurance_accepted = True
            return start_demographics_name_question(new_patient_is_minor)
        if patient_wants_to_decline(message_lower):
            new_patient_insurance_pending_oon = False
            return new_patient_goodbye_message()
        return medicare_advantage_oon_message(new_patient_offered_provider)

    # ---- STAGE 3F: Proceed after cost estimate (non self-pay-quote path) ----
    if (new_patient_insurance_collected and not new_patient_insurance_accepted
            and new_patient_insurance_type != "self_pay_quoted"
            and not new_patient_insurance_pending_oon):
        if patient_wants_to_proceed(message_lower):
            new_patient_insurance_accepted = True
            return start_demographics_name_question(new_patient_is_minor)

    # ---- STAGE 5: Demographics collection (only if insurance accepted) ----
    if new_patient_insurance_accepted and new_patient_insurance_collected:
        if new_patient_demographics_stage is None:
            if (
                    not new_patient_is_minor
                    and new_patient_first_name and new_patient_last_name
            ):
                # Bug fix: the caller's name was already captured
                # earlier in this call (see the opportunistic capture
                # at the top of this function) - skip the redundant
                # name question entirely and continue straight to
                # asking for DOB.
                new_patient_demographics_stage = "dob"
            else:
                new_patient_demographics_stage = "name"

        if new_patient_demographics_stage == "name":
            extracted_first, extracted_last = aggressive_name_extraction(message)
            first, last = extracted_first, extracted_last
            if not first or not last:
                parts = message.strip().split()
                if len(parts) >= 2:
                    first, last = parts[0], parts[1]
            if first and last:
                new_patient_first_name = first
                new_patient_last_name = last
                new_patient_demographics_stage = "dob"
                if new_patient_is_minor:
                    return (
                        "Thank you. Could I get the patient's date of "
                        "birth?"
                    )
                if new_patient_household_stage == "secondary":
                    poss = _household_pronoun_possessive(new_patient_household_relation)
                    return f"Thank you. Could I get {poss} date of birth?"
                return "Thank you. Could I get your date of birth?"
            if new_patient_is_minor:
                return (
                    "Could I get the first and last name of the "
                    "patient we will be scheduling the appointment for?"
                )
            return "Could I get your first and last name?"

        if new_patient_demographics_stage == "dob":
            # ── Parent-on-line router (Sprint 15): after the under-18 gate
            # above asked the minor's caller whether a parent/guardian can
            # come on the line, THIS turn is the child's yes/no reply. It
            # must be routed BEFORE the DOB-digest if below, because that
            # digest already happened last turn - the demographics stage is
            # still "dob", so a reply saying "yes, my dad is right here"
            # contains NO date of birth and would otherwise fall straight
            # out of the ladder stages below and get silently swallowed by
            # the adult-path caller logic. Route in, deterministically.
            if new_patient_parent_on_line_pending:
                new_patient_parent_on_line_pending = False
                if _parent_yes_in_message(message):
                    # Parent is on the line: capture their name as the
                    # caller (guardian) - one opportunistic parse, matching
                    # the caller-stage behavior for adult callers - then
                    # route straight to address collection so the parent
                    # continues the registration with the patient beside
                    # them on the line.
                    pf, pl = aggressive_name_extraction(message)
                    if pf and pl:
                        caller_first_name = pf
                        caller_last_name = pl
                    new_patient_demographics_stage = "address"
                    return (
                        "Perfect - I have the parent or guardian on the line. "
                        "Could I get the street address where the patient lives?"
                    )
                if _parent_no_in_message(message):
                    # No parent available right now: Steve must never book
                    # a minor without a guardian - apologize, ask the parent
                    # to call back when they are available, and end warmly
                    # with no appointment scheduled.
                    return (
                        "I completely understand. As the patient is under 18, "
                        "I am not able to schedule the appointment without a "
                        "parent or guardian on the line. When your parent "
                        "or guardian is available, please give us a call "
                        "back and we will be glad to help. We look forward "
                        "to hearing from you. Take care!"
                    )
            if detect_dob_in_message(message):
                new_patient_dob = extract_dob_from_message(message)
                # ── Under-18 gate (Sprint 15): Steve must never schedule
                # a minor without a parent/guardian on the line. The DOB
                # was just digested above, so compute the age from it and
                # check the <18 bound deterministically BEFORE asking who
                # is on the call (Caller stage below) - for an adult the
                # caller is the patient, but for a minor the caller is the
                # parent and must be explicitly routed onto the line first.
                dob_age = calculate_age_from_dob(new_patient_dob)
                if dob_age is not None and dob_age < 18:
                    # Deterministic minor-from-DOB detection: DOB-derived
                    # age under 18. The existing phrase-based minor
                    # detection (new_patient_is_minor) fires on "my son /
                    # my 8 year old daughter" - it NEVER computes age from
                    # a raw DOB. So a minor who simply gave Steve their
                    # DOB without stating a relationship or age gets past
                    # that phrase gate today. Set the dedicated flag now -
                    # and persist the parent-on-line confirmation through
                    # the NEXT turn so a "yes, my parent is right here"
                    # reply is routed onto the parent confirmation prompt
                    # rather than silently swallowed by the caller stage.
                    new_patient_is_minor = True
                    new_patient_minor_age = dob_age
                    new_patient_parent_on_line_pending = True
                    return (
                        f"Thank you for that. Since the patient is "
                        f"{dob_age} and under 18, I need to confirm a "
                        f"parent or guardian is on the line with you. "
                        f"Would it be okay to put the patient's parent "
                        f"or guardian on the phone now?"
                    )
                # After collecting the patient's DOB, ask who is on the
                # call (caller) before continuing with address collection.
                # For adult patients, the caller is the patient �?" name already collected.
                if not new_patient_is_minor and new_patient_first_name and new_patient_last_name:
                    # Household fix: don't overwrite the caller's own
                    # identity with the second household member's name
                    # while registering them later in the same call.
                    if new_patient_household_stage != "secondary":
                        caller_first_name = new_patient_first_name
                        caller_last_name = new_patient_last_name
                    if (
                            new_patient_household_stage == "secondary"
                            and new_patient_household_primary_address
                    ):
                        # Enhancement: most husband/wife new-patient
                        # scheduling shares one household address -
                        # ask once instead of unconditionally making
                        # the caller repeat street/city/state/ZIP that
                        # was already collected earlier in the same
                        # call.
                        new_patient_demographics_stage = "household_same_address"
                        return (
                            f"Does your {new_patient_household_relation} live "
                            "at the same address as you?"
                        )
                    new_patient_demographics_stage = "address"
                    if new_patient_household_stage == "secondary":
                        poss = _household_pronoun_possessive(new_patient_household_relation)
                        return f"Thank you. Could I get {poss} street address?"
                    return "Thank you. Could I get your street address?"
                new_patient_demographics_stage = "caller"
                return "Thank you. Who am I speaking with today?"
            if new_patient_household_stage == "secondary":
                poss = _household_pronoun_possessive(new_patient_household_relation)
                return f"Could I get {poss} date of birth?"
            return "Could I get your date of birth?"

        if new_patient_demographics_stage == "caller":
            # Capture the caller's name (often the parent/guardian).
            cf, cl = aggressive_name_extraction(message)
            if not cf or not cl:
                parts = message.strip().split()
                if len(parts) >= 2:
                    cf, cl = parts[0], parts[1]
            if cf and cl:
                caller_first_name = cf
                caller_last_name = cl
            else:
                # If we couldn't parse two names, still store the raw
                # string as caller_first_name for later reference.
                caller_first_name = message.strip()
                caller_last_name = None
            new_patient_demographics_stage = "address"
            return "Thank you. Could I get your street address?"

        if new_patient_demographics_stage == "household_same_address":
            if patient_wants_to_decline(message_lower):
                new_patient_demographics_stage = "address"
                poss = _household_pronoun_possessive(new_patient_household_relation)
                return f"Thank you. Could I get {poss} street address?"
            if patient_wants_to_proceed(message_lower):
                new_patient_street_address = new_patient_household_primary_address
                new_patient_demographics_stage = "phone"
                obj = _household_object_pronoun(new_patient_household_relation)
                return f"Thank you. What is the best number to reach {obj} at?"
            return (
                f"Does your {new_patient_household_relation} live "
                "at the same address as you?"
            )

        if new_patient_demographics_stage == "address":
            new_patient_street_address = message.strip()
            new_patient_demographics_stage = "phone"
            if new_patient_household_stage == "secondary":
                obj = _household_object_pronoun(new_patient_household_relation)
                return f"Thank you. What is the best number to reach {obj} at?"
            return "Thank you. What is the best number to reach you?"

        if new_patient_demographics_stage == "phone":
            new_patient_phone = message.strip()
            new_patient_demographics_stage = "email"
            if new_patient_household_stage == "secondary":
                poss = _household_pronoun_possessive(new_patient_household_relation)
                return f"Thank you. Could I get {poss} email address?"
            return "Thank you. Could I get your email address?"

        if new_patient_demographics_stage == "email":
            new_patient_email = message.strip()
            if (
                    new_patient_insurance_type == "self_pay_quoted"
                    or new_patient_self_pay_intent
            ):
                if new_patient_household_stage == "secondary":
                    new_patient_demographics_stage = "household_consent_availability"
                    return _household_consent_availability_question(
                        new_patient_household_relation
                    )
                new_patient_demographics_stage = "text_consent"
                return (
                    "Last question - do we have your consent to text you "
                    "at the number you provided?"
                )
            new_patient_demographics_stage = "member_number"
            if new_patient_household_stage == "secondary":
                poss = _household_pronoun_possessive(new_patient_household_relation)
                return (
                    f"Thank you. Could I get the member number for "
                    f"{poss} insurance?"
                )
            return (
                "Thank you. Could I get the member number for your insurance?"
            )

        if new_patient_demographics_stage == "member_number":
            if (
                    new_patient_insurance_type == "self_pay_quoted"
                    or new_patient_self_pay_intent
            ):
                if new_patient_household_stage == "secondary":
                    new_patient_demographics_stage = "household_consent_availability"
                    return _household_consent_availability_question(
                        new_patient_household_relation
                    )
                new_patient_demographics_stage = "text_consent"
                return (
                    "Last question - do we have your consent to text you "
                    "at the number you provided?"
                )
            new_patient_member_number = message.strip()
            if new_patient_household_stage == "secondary":
                new_patient_demographics_stage = "household_consent_availability"
                return _household_consent_availability_question(
                    new_patient_household_relation
                )
            new_patient_demographics_stage = "text_consent"
            return (
                "Last question - do we have your consent to text you "
                "at the number you provided?"
            )

        if new_patient_demographics_stage == "household_consent_availability":
            # Bug fix: previously Sarah (the caller) was asked to give
            # SMS consent on Sam's behalf, even though he's a separate
            # adult patient. Ask whether he's available to consent
            # himself first, matching how consent works for the
            # primary caller.
            if patient_wants_to_decline(message_lower):
                # Not available - record consent as declined but
                # continue scheduling; consent can be updated later,
                # same as any other patient who hasn't given consent.
                new_patient_text_consent = False
                return _new_patient_after_consent_response()
            if patient_wants_to_proceed(message_lower):
                # Bug fix: "Yes" here only confirms the husband is
                # available in the room - it is Sarah speaking, not a
                # change of speaker. Explicitly request the transfer
                # and wait for identity confirmation before treating
                # any answer as Sam's own.
                new_patient_demographics_stage = "household_speaker_transfer"
                full_name = f"{new_patient_first_name} {new_patient_last_name}".strip()
                return f"Please put {full_name} on the line. I'll wait."
            return _household_consent_availability_question(
                new_patient_household_relation
            )

        if new_patient_demographics_stage == "household_speaker_transfer":
            # Bug fix: only once the new speaker's identity is
            # confirmed does Steve treat further answers as coming
            # from the second patient rather than the caller.
            identity_confirmed = bool(
                new_patient_first_name
                and new_patient_first_name.lower() in message_lower
            )
            if identity_confirmed:
                # The second household member has now taken the line,
                # so Steve's subsequent messages speak to them - not
                # the caller who set up the booking.
                new_patient_household_speaker_active = True
                # Bug fix: confirming the household member's FIRST name
                # is not identity verification. A spouse (or anyone) who
                # takes the line must verify their last name AND their
                # date of birth - mirroring the HIPAA patient self-
                # verification gate used when a patient is put on a
                # third-party call - before Steve may accept their
                # consent. Previously the flow jumped straight from
                # "Hi this is <first name>" to asking for text consent.
                new_patient_demographics_stage = "household_speaker_verify"
                return (
                    "For verification purposes, May I have your last "
                    "name and date of birth?"
                )
            full_name = f"{new_patient_first_name} {new_patient_last_name}".strip()
            return (
                f"I just want to make sure I'm speaking with "
                f"{full_name} before continuing. Could you confirm "
                f"that for me?"
            )

        if new_patient_demographics_stage == "household_speaker_verify":
            # The household member who took the line must offer their
            # last name AND a date of birth before consent is asked.
            last_name_present = (
                not new_patient_last_name
                or bool(
                    re.search(
                        r"\b" + re.escape(new_patient_last_name) + r"\b",
                        message_lower, re.IGNORECASE
                    )
                )
            )
            if last_name_present and detect_dob_in_message(message_lower):
                new_patient_demographics_stage = "text_consent"
                return (
                    "Do we have your consent to text you at the number "
                    "provided?"
                )
            return (
                "For verification purposes, May I have your last name "
                "and date of birth?"
            )

        if new_patient_demographics_stage == "text_consent":
            new_patient_text_consent = any(
                phrase in message_lower for phrase in proceed_phrases
            )
            return _new_patient_after_consent_response()

        if new_patient_demographics_stage == "appointment_time":
            primary_slot, second_relation, second_slot = extract_household_scheduling(
                message, message_lower
            )
            new_patient_appointment_selection = primary_slot
            new_patient_demographics_stage = "complete"
            # Bug fix: this workflow builds its own confirmation text
            # in Python and returns immediately, so it never reached
            # the Groq-response-driven store_generic_appointment_record()
            # call that ordinary (wellness/acute) bookings go through -
            # new-patient appointments were only ever spoken aloud,
            # never persisted, so a later "when is my appointment?"
            # lookup (which reads from that same store) found nothing.
            store_generic_appointment_record(
                new_patient_first_name, new_patient_last_name,
                new_patient_appointment_selection,
                provider=new_patient_offered_provider,
                reason=_derive_generic_appointment_reason(),
            )
            address_name = caller_first_name if caller_first_name else new_patient_first_name
            confirmation = (
                f"Thank you, {address_name}. I have you all set. "
                f"You are scheduled with "
                f"{new_patient_offered_provider} for "
                f"{new_patient_appointment_selection}."
            )
            if (
                    new_patient_household_relation
                    and second_relation
                    and new_patient_household_stage == "primary"
            ):
                # Bug fix: previously this workflow only ever tracked
                # a single patient, so a combined request like "...and
                # please schedule my husband for 2:00 PM" was silently
                # dropped once the caller's own appointment was
                # booked. Pivot into registering the second household
                # member using the exact same demographics sequence
                # (unchanged), instead of closing the call.
                new_patient_household_second_time = second_slot
                new_patient_household_primary_first_name = new_patient_first_name
                new_patient_household_primary_address = new_patient_street_address
                new_patient_household_stage = "secondary"
                new_patient_first_name = None
                new_patient_last_name = None
                new_patient_dob = None
                new_patient_is_minor = None
                new_patient_street_address = None
                new_patient_phone = None
                new_patient_email = None
                new_patient_member_number = None
                new_patient_text_consent = None
                new_patient_demographics_stage = "name"
                relation_label = new_patient_household_relation
                poss = _household_pronoun_possessive(relation_label)
                return (
                        confirmation + f" I can schedule your {relation_label} "
                                       f"for {new_patient_household_second_time} to "
                                       f"establish as a new patient as well, but I would "
                                       f"need to get {poss} information first. Can I get "
                                       f"your {relation_label}'s first and last name?"
                )
            return confirmation + " Is there anything else I can help you with today?"

    if new_patient_demographics_stage == "complete":
        if any(
                phrase in message_lower for phrase in [
                    "that will be all",
                    "that's all",
                    "that's it",
                    "nothing else",
                    "no i'm good",
                    "no i'm all set",
                    "i'm good",
                    "i'm all set",
                    "all set",
                    "that will do",
                ]
        ):
            new_patient_flow_active = False
            return (
                f"Perfect, thank you {new_patient_first_name or 'for your time'}. "
                f"Your appointment with {new_patient_offered_provider} is "
                f"confirmed. Please arrive a few minutes early to complete "
                f"any new patient paperwork at check-in. Have a wonderful day."
            )

    print(f"DEBUG FELL_THROUGH_TO_NONE: insurance_type={new_patient_insurance_type!r} "
          f"insurance_collected={new_patient_insurance_collected!r} "
          f"demographics_stage={new_patient_demographics_stage!r}")
    return None


def extract_names_from_message(message):
    result = {
        "caller_first": None, "caller_last": None,
        "patient_first": None, "patient_last": None
    }
    caller_patterns = [
        r"(?i:this\s+is)\s+([A-Za-z][a-z]+)\s+([A-Za-z][a-z]+)",
        r"(?i:my\s+name\s+is)\s+([A-Za-z][a-z]+)\s+([A-Za-z][a-z]+)",
        # Avoid matching verbs like 'calling' after "I'm" or "I am".
        # Use a negative lookahead to skip common phrases such as
        # "I'm calling for" or "I'm calling about".
        r"(?i:i(?:'|)m)\s+(?!(?i:calling\b|calling\s+for\b|calling\s+about\b|not\b))([A-Za-z][a-z]+)\s+([A-Za-z][a-z]+)",
        r"(?i:i\s+am)\s+(?!(?i:calling\b|calling\s+for\b|calling\s+about\b|not\b))([A-Za-z][a-z]+)\s+([A-Za-z][a-z]+)",
        # Fallback: a bare "Firstname Lastname" reply with no prefix at
        # all (e.g. answering "for whom do I have the pleasure of
        # speaking" with just "Nick Adams"). Anchored to the WHOLE
        # message so it doesn't misfire on a longer sentence that
        # happens to contain two adjacent capitalized words. This is
        # deliberately last in the list so any of the more specific
        # prefixed patterns above take priority if they also match.
        # Uses [A-Za-z]* (not [a-z]+) after the leading capital so a
        # mixed-case typo like "WIlliams" still matches - only the
        # first letter of each word needs to be capitalized.
        # Deliberately NOT relaxed to accept a lowercase leading letter
        # like patterns 1-4 above: this pattern has no contextual
        # anchor ("this is", "my name is", etc.), so accepting a bare
        # lowercase two-word message here would meaningfully raise the
        # risk of matching an unrelated two-word reply as if it were a
        # name.
        # Bug fix: a transcribed answer ("Helen Harper.") may carry a
        # trailing period/punctuation, which the old \s*$ anchor
        # rejected - so the caller's name was never captured, the
        # deterministic HIPAA gate never fired, and the LLM improvised
        # a reply ("Thank") instead of the prescribed verdict. Tolerate
        # trailing punctuation without weakening the whole-message,
        # two-capitalized-words anchor.
        r"^\s*([A-Z][A-Za-z]*)\s+([A-Z][A-Za-z]*)[.,;:!?\"'”\-]*\s*$",
    ]
    for pattern in caller_patterns:
        match = re.search(pattern, message)
        if match:
            # Root cause fix: patterns 1-4 above now accept a lowercase
            # leading letter (real callers, and phone transcription in
            # particular, don't reliably capitalize spoken names - e.g.
            # "this is dan wolowitz"). Title-casing here keeps every
            # downstream consumer's contract intact either way: Steve's
            # own replies (e.g. "Thank you {caller_first_name}") and the
            # generic-appointment-store key builder both expect a
            # properly-formatted name string. Title-casing an
            # already-capitalized match (the pre-existing, working case)
            # is a no-op, so this changes nothing for input that was
            # already being captured correctly.
            matched_first = match.group(1).title()
            matched_last = match.group(2).title()
            # If this phrase actually names the PATIENT (not the original
            # caller), the patient is announcing themselves mid-call, not
            # the caller restating their own identity. Do not let this
            # overwrite caller_first/caller_last in that case — the
            # dedicated patient self-announce check in the /chat route
            # is responsible for that scenario instead.
            is_patient_self_announce = (
                    patient_first_name and patient_last_name and
                    matched_first.lower() == patient_first_name.lower() and
                    matched_last.lower() == patient_last_name.lower()
            )
            if not is_patient_self_announce:
                result["caller_first"] = matched_first
                result["caller_last"] = matched_last
            break
    patient_patterns = [
        # Accept optional punctuation or the word 'named' after the
        # relationship so constructions like "calling for my husband,
        # William Vance" or "calling for my husband named William
        # Vance" are captured.
        r"(?:on behalf of|calling for|calling about)\s+(?:my\s+)?(?:wife|husband|mother|father|son|daughter|sister|brother|grandmother|grandfather|spouse|partner|relative|friend)[,:\s]*(?:named\s+)?([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?",
        # Bug fix: a caller can name the patient directly with NO
        # relationship word ("...calling on behalf of Thelma Louis"),
        # which the relationship-requiring pattern above silently
        # skips - leaving the patient's name uncaptured and forcing a
        # redundant "Could I get the patient's first and last name?"
        # later. Stopword lookahead blocks non-name fillers like "an
        # appointment" / "a refill" from being parsed as names.
        r"(?:on behalf of|calling for|calling about)\s+(?!(?:an?|the|my|our|your|their|his|her|doctor|office|appointment|refill|medication|labs?|results?|records?|help|information)\s)([A-Za-z][a-z]+)(?:\s+([A-Za-z][a-z]+))?",
        r"\bmy\s+(?:wife|husband|mother|father|son|daughter|sister|brother|grandmother|grandfather|spouse|partner)(?:'s name is| named)?\s+([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?",
        # A caller with no personal relationship to the patient (e.g. a
        # hospital transition team member) typically states the patient's
        # name directly rather than via a relationship word: "The
        # patient's name is John Johnson", "The patient is John Johnson",
        # "patient is named John Johnson".
        r"(?:the\s+)?patient(?:'s)?\s+(?:name\s+is|is\s+named|is)\s+([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?",
        # A pronoun-prefixed statement, common once the patient's gender/
        # relationship has already come up in conversation: "Her name is
        # Margaret Cooper", "His name is...", "Their name is...".
        r"(?:her|his|their)\s+name\s+is\s+([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?",
    ]
    for pattern in patient_patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            result["patient_first"] = match.group(1)
            if match.lastindex >= 2 and match.group(2):
                result["patient_last"] = match.group(2)
            break
    return result


def determine_pre_chart_response(message, message_lower):
    global caller_first_name, caller_last_name, patient_first_name
    global patient_last_name, caller_is_patient, pre_chart_complete
    global dob_collected, third_party_detected, pcp_collected
    global is_medical_professional_caller, third_party_availability_asked
    global established_patient_minor_pending, established_patient_minor_age
    global established_patient_minor_guardian_confirmed, established_patient_dob
    global established_patient_minor_waiting
    global established_patient_minor_parent_first, established_patient_minor_parent_last

    # Sprint 15 (established patients): parent/guardian-on-line router. The
    # under-18 DOB gate below armed established_patient_minor_pending last
    # turn by intercepting the minor's digested DOB right before pre-chart
    # would have completed; THIS turn is the minor's yes/no reply to
    # "can the parent or guardian come on the line?" Route it here, BEFORE
    # name extraction - a reply like "yes, my mom is here" contains no
    # patient name to extract and would otherwise fall straight through
    # the caller_is_patient checks and get silently swallowed.
    if established_patient_minor_pending:
        established_patient_minor_pending = False
        if _parent_yes_in_message(message):
            if _parent_transfer_in_message(message):
                # The minor did not say "yes, they are here" - they said
                # they are GOING TO FETCH the parent ("I'm going to put
                # my mom on the phone right now. Please wait." / "hold
                # on, I'll get my dad"). The parent has not spoken yet,
                # so this is NOT a guardian confirmation: acknowledge,
                # arm the waiting state, and let the parent identify
                # themselves by name on the next turn.
                established_patient_minor_waiting = True
                return "Go ahead, I'll wait."
            # Parent/guardian is on the line now (or their name is in
            # this reply): capture their name as the caller (the
            # patient's identity stays the minor), mark the guardian
            # confirmed so the gate never re-arms this call, and
            # complete pre-chart - the minor's name/DOB/PCP were
            # already collected before the gate fired.
            parent_first, parent_last = _established_parent_name_from_message(message)
            if not parent_first:
                parent_first = _established_parent_first_name_from_message(message)
            if parent_first:
                established_patient_minor_parent_first = parent_first
            if parent_last:
                established_patient_minor_parent_last = parent_last
            if parent_first and parent_last:
                caller_first_name = parent_first
                caller_last_name = parent_last
                established_patient_minor_guardian_confirmed = True
                pre_chart_complete = True
                return "How can I help you today?"
            # The parent is on the line but has not identified
            # themselves yet - collect the missing part of their name
            # before any scheduling begins.
            established_patient_minor_waiting = True
            if parent_first:
                return (
                    "Perfect - I have the parent or guardian on the line. "
                    "Could I get your last name please?"
                )
            return (
                "Perfect - I have the parent or guardian on the line. "
                "Could I get your first and last name, please?"
            )
        if _parent_no_in_message(message):
            # No parent/guardian available right now: Steve must never book
            # a minor without a guardian - apologize, ask the parent to call
            # back when available, and end warmly with no appointment booked.
            return (
                "I completely understand. As the patient is under 18, "
                "I am not able to schedule the appointment without a "
                "parent or guardian on the line. When your parent "
                "or guardian is available, please give us a call "
                "back and we will be glad to help. We look forward "
                "to hearing from you. Take care!"
            )
        # Ambiguous reply - not a clear yes or no. Re-arm the gate and ask
        # again so a minor is never booked without an explicit guardian.
        established_patient_minor_pending = True
        return (
            "I still need to confirm a parent or guardian is on the line "
            "before I can help with that. Would it be okay to put the "
            "patient's parent or guardian on the phone now?"
        )

    # Sprint 15 (established patients): the minor said they were going to
    # fetch the parent ("I'm going to put my mom on the phone right now.
    # Please wait.") or that the parent is already on the line, so Steve
    # replied "Go ahead, I'll wait." / asked for the guardian's name.
    # THIS turn is the parent/guardian speaking for the first time.
    # Capture their identity: if they give a name, confirm the guardian
    # and complete pre-chart (the minor's name/DOB/PCP were already
    # collected before the gate fired); if they only announce the
    # relationship ("this is her mom") without a name, keep waiting and
    # collect the name - a guardian who never identifies themselves is
    # never confirmed.
    if established_patient_minor_waiting:
        parent_first, parent_last = _established_parent_name_from_message(message)
        if not parent_first:
            parent_first = _established_parent_first_name_from_message(message)
        if parent_first:
            established_patient_minor_parent_first = parent_first
        if parent_last:
            established_patient_minor_parent_last = parent_last
        first = established_patient_minor_parent_first
        last = established_patient_minor_parent_last
        if not last:
            last = _established_parent_last_name_from_message(
                message, known_first=first
            )
        if first and last:
            caller_first_name = first
            caller_last_name = last
            established_patient_minor_waiting = False
            established_patient_minor_guardian_confirmed = True
            pre_chart_complete = True
            return "How can I help you today?"
        if first:
            return "Thank you. Could I get your last name please?"
        if last:
            return "Thank you. Could I get your first name please?"
        return "Thank you. Could I get your first and last name, please?"

    # Bug fix: mirror the Sprint13.phf_intent_detected guard used on the
    # equivalent medical-professional intercept in the /chat route. This
    # is a second, independent call to is_medical_professional_message()
    # for the SAME message - without this guard, a hospital transition
    # team caller's self-introduction ("This is Janet from the Orlando
    # Health Transitions team...schedule a post hospital follow up")
    # still matched the generic "this is X from Y" fallback pattern here
    # and set pre_chart_complete = True, causing the /chat route's
    # generic "Thank you for that. How can I help you today?" fallback
    # to fire instead of asking for the patient's first and last name.
    if not Sprint13.phf_intent_detected and is_medical_professional_message(message, message_lower):
        is_medical_professional_caller = True
        pre_chart_complete = True
        return None

    # Only extract names if pre-chart is not yet complete
    # Prevents city/state names in follow-up messages from overwriting
    # already collected caller name variables
    if not pre_chart_complete:
        extracted = extract_names_from_message(message)
        if extracted["caller_first"] and not caller_first_name:
            caller_first_name = extracted["caller_first"]
        if extracted["caller_last"] and not caller_last_name:
            caller_last_name = extracted["caller_last"]
        if extracted["patient_first"] and not patient_first_name:
            patient_first_name = extracted["patient_first"]
        if extracted["patient_last"] and not patient_last_name:
            patient_last_name = extracted["patient_last"]

        # A caller who introduces themselves by name (e.g. "This is Tim
        # Matthews") without using any third-party language is, by far,
        # most likely the patient calling for themselves - not someone
        # calling on behalf of another person. Without this default,
        # caller_is_patient stays ambiguous (None) indefinitely unless
        # the caller also happens to say an explicit phrase like "I am
        # the patient", which most callers never think to say. Staying
        # ambiguous causes determine_pre_chart_response to keep
        # returning None every turn, leaving the AI to improvise the
        # rest of pre-chart collection (DOB/PCP order, re-asking for a
        # name already given) instead of following a deterministic order.
        if (
                extracted["caller_first"]
                and caller_is_patient is None
                and (
                not any(phrase in message_lower for phrase in THIRD_PARTY_PHRASES)
                or _JOINT_APPOINTMENT_REFERENCE_PATTERN.search(message_lower)
                or _VIRTUAL_WAIT_SELF_PATTERN.search(message_lower)
        )
        ):
            caller_is_patient = True

    if (
            any(phrase in message_lower for phrase in THIRD_PARTY_PHRASES)
            and not _JOINT_APPOINTMENT_REFERENCE_PATTERN.search(message_lower)
            and not _VIRTUAL_WAIT_SELF_PATTERN.search(message_lower)
    ):
        caller_is_patient = False
        third_party_detected = True
        if detect_dob_in_message(message):
            dob_collected = True
        # If the caller is a third-party and we haven't captured their
        # name yet, ask for it first so we can check HIPAA authorization
        # against the patient's records before making any assumptions.
        if not caller_first_name or not caller_last_name:
            return "Thank you. Could I get your first and last name, please?"
        # dob_collected is NOT reset here. The third party's statement of
        # the patient's DOB is sufficient for HIPAA verification of the
        # THIRD PARTY's access. Separate re-verification of the PATIENT'S
        # OWN identity (when the patient later gets on the phone) is
        # handled by patient_self_dob_verified below, not by this flag.

    patient_confirm_phrases = [
        "i am the patient", "i'm the patient", "yes i am",
        "yes, i am", "that's me", "that is me", "speaking",
        "this is me", "yes it is",
        "i am a patient", "i'm a patient",
        "i am a patient of", "i'm a patient of",
        "i am the patient of", "i'm the patient of",
        "i am his patient", "i am her patient",
        "patient of dr", "a patient of dr"
    ]
    # Bug fix: the bare confirm phrases "patient of dr" / "a patient of
    # dr" also match a THIRD PARTY stating the patient's PCP ("She is a
    # patient of Dr. Patel", "my mother is a patient of Dr. Chen") - the
    # caller describes the PATIENT's provider, not themselves. Once a
    # caller has been positively identified as a third party (set just
    # above), no confirm phrase may re-flag them as the patient, or the
    # third-party flow (caller-name collection, then the deterministic
    # HIPAA status check) is silently bypassed and the AI improvises an
    # unauthorized HIPAA verdict. Genuine patients never set
    # third_party_detected, so their normal confirmation is unaffected.
    if any(phrase in message_lower for phrase in patient_confirm_phrases) and not third_party_detected:
        caller_is_patient = True

    if detect_dob_in_message(message):
        dob_collected = True
        if not established_patient_dob:
            established_patient_dob = extract_dob_from_message(message)

    provider = detect_provider_in_message(message_lower)
    if provider:
        pcp_collected = True

    if (not caller_first_name or not caller_last_name) and Sprint13.phf_caller_type != "hospital_team":
        last_name_only = re.search(
            r"(?:this is |i am |i'm )"
            r"(?:mr\.|mrs\.|ms\.|miss\.|dr\.)\s*([A-Z][a-z]+)",
            message, re.IGNORECASE
        )
        if last_name_only:
            caller_last_name = last_name_only.group(1)
            return "Thank you. Could I get your first name as well?"
        if len(conversation_history) == 0:
            return None
        if caller_first_name and not caller_last_name:
            return (
                f"Thank you {caller_first_name}. "
                f"Could I also get your last name?"
            )
        if not caller_first_name and not caller_last_name:
            return None

    if caller_is_patient is None and Sprint13.phf_caller_type != "hospital_team":
        return None

    if caller_is_patient:
        # When the caller IS the patient, patient_first_name/
        # patient_last_name must reflect the caller's own name.
        # extract_names_from_message's patient_patterns only match
        # third-party phrasing ("the patient's name is X", "my husband
        # X"), so a patient's own self-introduction ("This is Nick
        # Miller...") never sets patient_first_name/patient_last_name by
        # itself. Without this sync they stay None for the whole call,
        # which silently breaks PHF appointment storage/lookup (keyed on
        # patient_first_name/patient_last_name) for every patient calling
        # about themselves. Guarded so it never overwrites an
        # already-set value, and runs every turn so it applies whichever
        # of the two places sets caller_is_patient = True.
        if caller_first_name and not patient_first_name:
            patient_first_name = caller_first_name
        if caller_last_name and not patient_last_name:
            patient_last_name = caller_last_name
        if not dob_collected:
            if Sprint13.phf_flow_active:
                return None
            return (
                "May I have your date of birth so I can pull up your chart?"
            )
        if not pcp_collected:
            if Sprint13.phf_flow_active:
                return None
            return (
                "Which primary care provider do you see at our practice?"
            )
        # Sprint 15 (established patients): deterministic under-18 gate.
        # Existing patients never enter handle_new_patient_flow, so the
        # minor DOB they give here in pre-chart is the ONLY place we can
        # catch it - compute the age from the digested DOB right before
        # pre-chart would otherwise complete and hand control to the adult
        # scheduling flow. A minor (e.g. "Alice Addison, DOB 5/2/2011")
        # must confirm a parent/guardian is on the line before any booking.
        # Guarded to only fire in the plain established-patient booking
        # path - mirrors the surrounding Sprint13.phf_flow_active guards
        # that step aside for specialty workflows.
        if (
            not Sprint13.phf_flow_active
            and not Sprint13.phf_intent_detected
            and established_patient_dob
            and not established_patient_minor_guardian_confirmed
            and not established_patient_minor_pending
        ):
            established_patient_minor_age = calculate_age_from_dob(
                established_patient_dob
            )
            if (
                established_patient_minor_age is not None
                and established_patient_minor_age < 18
            ):
                established_patient_minor_pending = True
                return (
                    f"Thank you for that. Since you are under the age of "
                    f"18, I need to confirm a parent or guardian is with "
                    f"you. Would it be okay to put the patient's parent "
                    f"or guardian on the phone now?"
                )
        pre_chart_complete = True
        return None

    # If this is a third-party caller who has provided the patient's DOB,
    # check HIPAA authorization first, then ask for patient availability
    # only if the caller is NOT on HIPAA.
    if (third_party_detected and dob_collected and not is_medical_professional_caller
            and not hipaa_status_determined):
        hipaa_status = get_hipaa_status()
        if hipaa_status == "NOT_ON_HIPAA":
            third_party_availability_asked = True
            # Bug fix: previously this returned only the availability
            # question, never reporting the HIPAA verdict - the caller
            # was left with no idea why access was denied. State the
            # verdict explicitly (matching the system prompt's prescribed
            # NOT_ON_HIPAA script) and use the patient's name when it has
            # been captured.
            patient_label = patient_first_name or "the patient"
            return (
                f"You are not listed under {patient_label}'s HIPAA form. "
                f"Is {patient_label} there with you?"
            )
        # Bug fix: a third-party caller who IS on the HIPAA form was never
        # told so - the code fell through to post-pre-chart and the LLM's
        # only output was "One moment while I pull up the chart." with no
        # verdict, leaving the caller unaware their access was authorized.
        # Report the ON verdict deterministically, mirroring the
        # NOT_ON_HIPAA gate above. Only once the patient's name is
        # captured (otherwise keep the fall-through so the name-collection
        # asks below still run) and only when PHF's own handler isn't
        # active (its flow takes over unchanged, as it did before).
        if (
            hipaa_status == "ON_HIPAA"
            and patient_first_name and patient_last_name
            and not Sprint13.phf_flow_active
        ):
            pre_chart_complete = True
            return (
                f"Thank you. One moment while I pull up the chart. "
                f"You are listed under {patient_first_name}'s HIPAA "
                f"form. How can I help you today?"
            )
        # If ON_HIPAA without a captured patient name, fall through to
        # continue normal pre-chart flow below.

    # If the code hasn't yet captured the patient's name, try one
    # more tolerant extraction that looks for relationship + name
    # patterns in the raw message (handles commas or the word
    # 'named' after the relationship), OR a caller with no personal
    # relationship to the patient (e.g. a hospital transition team
    # member) stating the patient's name directly - "The patient's
    # name is John Johnson". This avoids re-asking for the patient's
    # name when the caller already provided it.
    if not patient_first_name:
        rel_name_match = re.search(
            r"(?:on behalf of|calling for|calling about|for my)\s+(?:my\s+)?(?:wife|husband|mother|father|son|daughter|sister|brother|spouse|partner)[,:\s]*(?:named\s+)?([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?"
            r"|(?:(?i:the\s+))?(?i:patient)(?:'s)?\s+(?:(?i:name\s+is)|(?i:is\s+named)|(?i:is))\s+([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?"
            r"|(?i:her|his|their)\s+(?i:name\s+is)\s+([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?",
            message
        )
        if rel_name_match:
            patient_first_name = rel_name_match.group(1) or rel_name_match.group(3) or rel_name_match.group(5)
            rel_last_name = rel_name_match.group(2) or rel_name_match.group(4) or rel_name_match.group(6)
            if rel_last_name:
                patient_last_name = rel_last_name
        else:
            if Sprint13.phf_flow_active:
                return None
            return "Could I get the patient's first and last name?"
    if not patient_last_name:
        if Sprint13.phf_flow_active:
            return None
        return (
            f"Thank you. Could I get {patient_first_name}'s last name "
            f"and date of birth?"
        )
    if (not caller_first_name or not caller_last_name) and Sprint13.phf_caller_type != "hospital_team":
        return "Could I also get your first and last name?"
    if not dob_collected:
        patient_full = f"{patient_first_name} {patient_last_name}"
        return (
            f"May I have {patient_full}'s date of birth "
            f"so I can pull up their chart?"
        )
    if not pcp_collected and Sprint13.phf_caller_type == "hospital_team":
        return (
            f"Thank you. Which primary care provider at our practice "
            f"does {patient_first_name} see?"
        )

    pre_chart_complete = True

    # If PHF workflow is active, let Sprint13 handle the response
    # but still allow this function to extract names/DOB into globals
    if Sprint13.phf_flow_active:
        return None

    return None


# ─────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────

system_prompt = """You are Steve, a professional patient services representative at the Sykes Creek Primary Care office.

ACTIVE OVERRIDES — READ THIS FIRST, HIGHEST PRIORITY:
Any block appended below that ends in "INJECTED BY SYSTEM" reflects a
decision already made this turn from the patient's own words or from
Python-computed facts (availability, dates, eligibility, what the
patient already asked for). When any such injected block conflicts
with a static instruction elsewhere in this prompt — even one that
looks more specific, more detailed, or comes with its own numbered
steps — the injected block wins COMPLETELY for this turn. Do not
average the two, do not partially apply the static instruction "just
to be safe," and do not re-derive your own version of a question or
choice the injected block already says is settled. Examples of what
this means in practice: if a signal says the patient already chose an
appointment over a refill, do not re-offer that choice in any words,
including softer phrasings like "alternatively, if you'd prefer...";
if a signal says a determination or collection step is being handled
by Python, do not attempt your own version of it, even briefly, even
combined with something else you're already saying this turn; if a
signal gives you an exact date or exact list of options, use exactly
those and do not adjust, recalculate, or hedge them based on other
instructions in this prompt.

YOUR NAME:
Your name is Steve. The opening greeting already says so.
Do NOT re-introduce yourself mid-conversation.
If directly asked your name respond: "My name is Steve."

GREETING:
The chat interface already displays the opening greeting.
Do NOT repeat it. Respond naturally to the caller.

RESPONSE TIME — ONE TIME ONLY:
State timeframe ONCE per conversation maximum.
Standard: 72 business hours.
Urgent patient medical only: 24 business hours.
Medical professional requests: always 72 business hours.
Lab order pickup: do NOT state any timeframe.
After stating once do NOT repeat.
If RESPONSE_TIME_ALREADY_STATED injected: do NOT state again.

OFFICE HOURS — ONE TIME ONLY:
State office hours at most ONCE per conversation.
Hours: Monday through Friday, 9:00 AM to 5:00 PM.
After stating once do NOT repeat.
If OFFICE_HOURS_ALREADY_STATED injected: say "during our office hours"
without restating the specific times.

PROVIDER LISTING — ABSOLUTE RULE:
NEVER list providers unless caller explicitly says they don't know.
Just ask: "Which primary care provider do you see at our practice?"

PCP COLLECTION:
Collect PCP before proceeding with ANY request.
If PCP_ALREADY_COLLECTED injected do NOT ask again.

NEVER RECOMMEND THE EMERGENCY ROOM:
NEVER mention ER, emergency room, or 911 unless patient brings it up.
Urgent care is the ONLY external option.

NO SPLIT RESPONSES:
Complete the full thought in one single response always.

PATIENT WILL CALL BACK:
Respond: "That sounds great. When [patient name] calls we will be happy
to assist further. Is there anything else I can help you with today?"

VERBAL CONSENT TRANSITION:
THIS SECTION APPLIES ONLY TO THIRD-PARTY CALLERS — someone calling on
behalf of the patient, not the patient calling for themselves. If
caller_is_patient is True, this section does NOT apply. Do NOT say
"I'll wait while you get [patient name] on the line" to a caller who
IS the patient.
SITUATION A — PATIENT IS PRESENT:
Say ONLY: "Could you please put [patient name] on the line?"

SITUATION B — PATIENT IS NOT PRESENT:
NEVER say I'll wait.
Say: "I would recommend having [patient name] call us directly.
I am happy to schedule a new appointment if needed."
HIPAA form: in person or mailed physical form ONLY.

PRE_CHART_STATE tells you what has been collected.
Do NOT ask for anything already marked as collected.

MEDICAL PROFESSIONAL CALLERS:
If MEDICAL_PROFESSIONAL_DETECTED injected:
Python handled ALL collection AND referral lookup.
NEVER ask about HIPAA. NEVER ask for patient info again.
All follow-up timeframes: 72 business hours.

CHART LOOKUP AND HIPAA — THIRD PARTY NON-MEDICAL ONLY:
NEVER run HIPAA for medical professional callers.
When DOB collected AND PCP collected and non-medical third party — ONE response:
"Thank you. One moment while I pull up the chart. (pause)"
Then IMMEDIATELY report HIPAA status from the system injection.
IF ON HIPAA: Verified, happy to assist.
IF NOT ON HIPAA: Say EXACTLY: "You are not listed under [patient]'s HIPAA
form." Then ask ONLY: "Is [patient] there with you?" This question is about
the PATIENT'S PRESENCE, not about the caller's own consent. Do NOT proceed to
the VERBAL CONSENT (STRICT) section below until the patient is confirmed to
be on the phone with you directly.
If patient IS present: follow VERBAL CONSENT TRANSITION SITUATION A above.
If patient IS NOT present: tell the caller the patient will need to call
back themselves, wish them a good day, and end the call. Do NOT offer to
schedule an appointment in this specific HIPAA-denial scenario.

CRITICAL — NEVER INVENT YOUR OWN HIPAA VERIFICATION:
NEVER ask caller if they have the patient's permission to discuss their care.
NEVER ask caller to confirm they are authorized.
NEVER say anything like "Can you confirm you have permission to discuss
his medical information with me."
The ONLY valid process is the system-injected HIPAA status.
If HIPAA_STATUS injection is not present do NOT attempt verification yourself.
If DOB has not yet been collected ask for the patient's date of birth first
and wait. If PCP has not yet been collected ask for PCP first and wait.
Do NOT make up any verification process of your own.

VERBAL CONSENT (STRICT):
THIS SECTION APPLIES ONLY WHEN SPEAKING DIRECTLY TO THE PATIENT, AFTER THE
PATIENT HAS GOTTEN ON THE PHONE. NEVER address Steps 1-3 below to the third
party caller themselves — a third party cannot give consent on the patient's
own behalf. If the patient is not yet on the phone, follow the NOT ON HIPAA
instructions above instead (ask if the patient is present).
Step 1: Ask for patient's date of birth.
Step 2: Ask for permission using EXACT phrasing:
"Would you be willing to give your consent for me to share information with (relationship to the patient), (name of third party)?"
DO NOT:
- Paraphrase this question
- Assume permission was already given
- Use phrases like "you've given permission"
- Reword or summarize the consent question
Step 3:
- If YES: say EXACTLY this and nothing else: "If you'd like you can put
  [third party name] back on the phone." Do NOT ask the consent
  question again. Do NOT add any summary of what was just documented.
  Do NOT ask the patient what they need help with. Do NOT offer to
  discuss medical information, schedule appointments, or assist the
  patient directly at this point - the consent step exists only to
  unlock sharing with the third party, not to start a new conversation
  with the patient. Keep this response to ONE short sentence only.
- If NO: inform caller that information cannot be shared.

MEDICAL ASSISTANT INFORMATION:
Each provider has a dedicated medical assistant:
- Dr. Sheldon Stroman → Lisa
- Dr. Sarah Mitchell → Karen
- Dr. Robert Chen → Diana
- Dr. Maria Rodriguez → Carmen
- Dr. David Thompson → Brian
- Dr. Jennifer Park → Susan
- Dr. Michael Brooks → Tony
- Dr. Lisa Anderson → Nicole
- Dr. Kevin Patel → Priya
- Dr. Amanda Foster → Rachel

NURSE AND MA REQUEST HANDLING — CRITICAL — SPRINT 8 BUG 5 FIX:
NEVER volunteer or mention the MA name proactively.
Do NOT announce the MA name after pulling up a chart or at any other
point unless the patient specifically asks to speak with the nurse or MA.

RULE 1 — Patient asks for nurse or MA generically
(e.g. "May I speak to the nurse", "Can I talk to the MA"):
ONLY at this point introduce the MA by name.
Say: "I will see if [MA name] is available."
Then ask the reason for the call.
Then check availability and route accordingly.

RULE 2 — Patient asks for specific MA by first name
(e.g. "I'd like to speak with Diana"):
The patient already knows who Diana is. Do NOT re-introduce her.
Simply say: "Let me check on Diana's availability."
Ask reason for call, then route based on availability.

RULE 3 — Do NOT mention MA name in any other context.
After chart pull do NOT say "Your MA is Diana" or
"Dr. Chen's dedicated medical assistant is Diana."
Only bring up the MA name if the patient asks for the nurse or MA.

MA WORKFLOW:
1. Ask reason for call (unless returning MA call)
2. Check MA_AVAILABILITY from injection
3. If AVAILABLE: connect patient
4. If NOT AVAILABLE: get callback number
   State 72 business hours (24 if urgent) — once only per call

LAB ORDER PICKUP — SPRINT 8 BUG 4 FIX — CRITICAL:
When patient calls about picking up EXISTING lab orders this is a
LAB ORDER PICKUP REQUEST not a new lab work request.

CORRECT WORKFLOW — follow this order exactly:
STEP 1: First ask if the patient would like the orders faxed to the lab.
Say: "Would you like me to fax the lab orders over to the facility
for you as well, or would you prefer to just pick them up?"

STEP 2A — If patient WANTS fax:
Ask for the fax number. If they don't know it ask for the facility
address so you can look up the fax number.
Then confirm you will print the orders and fax them.
State office hours ONCE for pickup.

STEP 2B — If patient does NOT want fax:
Confirm you will print the orders and have them ready for pickup.
State office hours ONCE.

Do NOT ask reason for the lab work.
Do NOT ask if PCP is aware.
Do NOT say 72 business hours — this is not a 72 hour request.
The orders already exist — this is fax and pickup coordination only.
If OFFICE_HOURS_ALREADY_STATED injected: do NOT repeat hours.

LAB WORK — NEW ORDERS ONLY:
When patient requests NEW lab work not yet ordered:
1. Ask reason. 2. Ask if PCP aware. 3. Note to MA.
4. State 72 business hours once if not already stated.
5. Document without name or DOB.

OFFICE HOURS:
Office open Monday through Friday. CLOSED Saturday and Sunday.
Same day on weekend: office closed, offer next Monday,
recommend urgent care if cannot wait. NEVER mention ER.

SCHEDULING VS URGENT SYMPTOMS:
Symptom + appointment request = SCHEDULING.
Past tense injury + appointment request = SCHEDULING.
Urgent only for present tense emergencies, no appointment request.

URGENT SYMPTOMS — TWO PHASE:
PHASE 1: Acknowledge calmly. Ask can it wait or need help now. STOP.
PHASE 2 — complete in ONE message:
MA AVAILABLE: Get MA on line, confirm connected, end call.
MA NOT AVAILABLE + CANNOT WAIT: Attempted to reach MA, unavailable,
recommend urgent care.
MA NOT AVAILABLE + CAN WAIT: Attempted to reach MA, unavailable,
get callback, 24 business hours.
NEVER mention ER or 911.

WORK NOTE / PAPERWORK REQUESTS:
When the stated reason for the visit is a work note, school note, FMLA
form, or other documentation to be signed or completed, this is a
documentation request — handle it separately from a routine
appointment request, UNLESS the patient has explicitly asked to
schedule an appointment (paperwork stated only as the reason for that
appointment, e.g. "I need to schedule an appointment for my provider
to fill out paperwork"). In that case, skip everything below for this
entire request — including after the appointment is booked — and
follow APPOINTMENT SCHEDULING — FUTURE instead, using paperwork as
the already-known reason: go straight to offering availability, book
it, and confirm normally once booked. No medical assistant routing,
no urgency framing, no promised follow-up timeframe, and no
"without an appointment" question — the patient already answered
that by asking for one.

Standalone paperwork request (patient did NOT ask for an appointment):
0. If the patient's message already makes an appointment likely
   necessary - a stated deadline plus a signature/fill-out
   requirement, or the patient directly asking whether an appointment
   is needed - skip the medical assistant determination in Step 1 and
   go straight to checking PROVIDER appointment availability: the
   patient's PCP first, then the covering provider if the PCP has
   nothing suitable. Checking generated calendar availability requires
   no live person, so it is NOT affected by current office hours and
   should always be attempted before falling back to a phone message.
   Only create a phone message and request a callback number (per
   Step 1's OFFICE_CURRENTLY_CLOSED branch) if NEITHER provider has
   availability that fits the stated deadline.
1. Otherwise - the request is genuinely ambiguous about whether an
   appointment is needed at all (e.g. "I just need a form filled
   out," no deadline or signature context given) - check the
   OFFICE_CURRENTLY_OPEN / OFFICE_CURRENTLY_CLOSED signal injected by
   the system for this turn BEFORE saying anything about reaching the
   medical assistant.
   - If OFFICE_CURRENTLY_CLOSED: do NOT claim to have reached, or to be
     connecting the patient with, the medical assistant, and do NOT
     attempt a warm transfer. Instead, create an urgent phone message
     and obtain a good callback number if needed. If the patient has
     stated a deadline (e.g. "before Monday morning", "I have to be at
     work Monday at 7AM"), document that deadline on the message
     itself. Do NOT frame the response around when the office reopens
     (e.g. "someone will contact you Monday") — that framing can
     conflict with a deadline that falls before the office opens.
     Instead confirm: the message has been created and marked urgent,
     the stated deadline (if any) has been documented, and someone
     will follow up within 24 business hours.
   - If OFFICE_CURRENTLY_OPEN: route through the patient's PCP's
     medical assistant FIRST (see MEDICAL ASSISTANT INFORMATION above
     for the correct MA name). Say: "Let me check with [MA name] on
     that."
2. Determine whether:
   - The provider can complete the note without an appointment, or
   - The provider wants to see the patient for an appointment, or
   - A work-in request is required.
3. Only if same-day accommodation is actually required for this
   documentation request (e.g. the patient states a same-day or
   next-business-day deadline) and same-day accommodation is
   appropriate here, follow the standard same-day scheduling workflow.
4. If same-day appointments are not appropriate for this request, or
   the office is closed, offer FUTURE availability only — do NOT offer
   or schedule a same-day appointment.
Do NOT bypass the medical assistant review step and immediately
schedule the patient same day.

SAME DAY APPOINTMENT:
State current time. Present ONLY injected slots. Wait for selection.
Weekend: closed, offer Monday, urgent care if cannot wait.

APPOINTMENT SCHEDULING — FUTURE:
If the patient has not already stated a reason for the visit (via a
symptom, refill, follow-up, or other context already given earlier in
this conversation), first ask: "What is the reason for the
appointment?" The reason determines appointment length, whether it is
a wellness/routine visit, and which workflow-specific scheduling
rules apply, so it must be collected before presenting availability.
Once the reason is known, offer future availability directly and
proceed to scheduling - do NOT route to a medical assistant callback
or state a follow-up timeframe (e.g. "72 business hours") for a
routine future appointment request. A patient stating a topic they
want to discuss with the provider - including a medication topic, a
new prescription, or being put on a medication - is giving a reason
for the visit, NOT asking a clinical question or asking for medical
advice; treat it exactly like any other appointment reason (a
symptom, a follow-up, a wellness visit) and continue straight to
offering availability. This applies even if the office is currently
closed - OFFICE_CURRENTLY_CLOSED only affects same-day/urgent
workflows and MA reachability right now, not the ability to offer and
book a FUTURE appointment. Only route to a medical assistant callback
if the patient explicitly asks a clinical question (what/why/how,
side effects, safety), explicitly requests same-day/urgent care, or
explicitly asks to speak with or leave a message for the medical
assistant. Use weekly availability. Present day by day.

VIRTUAL VISIT:
Same day: check MA, callback, HIGH PRIORITY.
Future: schedule directly.

NURSE VISIT:
Nurse visits (injections, B12/flu shots, vaccines, urinalysis, x-rays,
etc.) are ALWAYS performed IN OFFICE - never virtually, so never ask
about a virtual visit or a virtual/in-office preference for a nurse
visit. You do NOT schedule nurse visits; the medical assistant schedules
them. If a patient requests a nurse visit, inform them the medical
assistant will need to schedule it, put in a note for the medical
assistant, and ask for a good contact number. Do NOT offer appointment
times and do NOT book the nurse visit yourself.

SYMPTOM ROUTING:
Contagious: offer virtual. Declines: callback, 24 hours.
Pushes back: urgent care only.

MEDICATION REFILLS:
0. If a CONTROLLED_SUBSTANCE_APPOINTMENT_REQUESTED or
   MEDICATION_BRIDGE_FLOW_ACTIVE signal is injected for this turn,
   this collection is being handled deterministically by Python - do
   NOT ask for dosage, pharmacy, or bridge-request details yourself,
   do NOT ask "do you have enough medication" yourself, and do NOT
   state 72 business hours for this flow. Follow ONLY the exact
   Python-authored question or confirmation for this turn.
1. Otherwise, collect: name, dosage, days remaining, pharmacy.
2. State 72 business hours once only.

CONTROLLED SUBSTANCES:
0. FIRST check whether the patient has explicitly asked for an
   APPOINTMENT (e.g. "I need an appointment", "schedule an
   appointment") with this controlled substance as the stated REASON.
   If so, this is a normal FUTURE appointment request, already
   decided by the patient - do NOT ask whether they would prefer a
   refill request forwarded instead, do NOT re-present a choice
   between "refill request" or "appointment" (the patient already
   chose appointment, do not reopen that decision), and do NOT apply
   the last-visit-window logic in the steps below AT ALL, not even to
   explain why an appointment is or isn't required. Specifically, do
   NOT say anything resembling "Because it has been less than [X]
   days since your last visit, I can forward your refill request to
   [provider] for approval. Would you like me to do that?
   Alternatively, if you would prefer to schedule an appointment..." -
   that entire sentence pattern, in any wording or softened variant of
   it, is exactly the reopened choice this step forbids. Go straight
   to offering availability from the injected weekly availability and
   proceed to scheduling, exactly like APPOINTMENT SCHEDULING —
   FUTURE above. This override applies for the entire rest of the
   appointment request, including after booking - do not layer any
   of Steps 1+ below on top of the confirmation once booked. Steps 1+
   below apply ONLY when the patient asks for a refill/renewal
   WITHOUT explicitly requesting an appointment.
1. State last visit first always.
2. Within window: collect refill info, send to provider. NO appointment.
   Do NOT say "if necessary we may need to schedule."
3. Outside window: schedule appointment.
4. Schedule 1: in-office only. Schedule 2/3: virtual or in-office.
5. Then bridge refill info after appointment.
6. Provider makes ALL decisions.

SPECIALIST RECOMMENDATIONS:
If patient asks for PCP recommendations for a specialist:
NEVER claim to know what the PCP recommends.
NEVER provide a list of specialists.
NEVER say "Dr. X has a few recommendations."
Say: "I don't have that information available but I can put in a
phone note for Dr. [PCP] to have someone call you back with
recommendations. May I get a good callback number for you?"
Get callback number.
State 72 business hours ONLY if RESPONSE_TIME_NOT_YET_STATED is injected.
If RESPONSE_TIME_ALREADY_STATED is injected do NOT restate timeframe.
When confirming callback do NOT repeat 72 business hours again.
Say: "Someone from the office will be in touch. Is there anything
else I can help you with today?"
Do NOT mention any MA name. Someone from the office will call back.

REFERRALS — PATIENT INITIATED:
PCP aware, reason, specialist details, relay to PCP.
State 72 business hours once only.
NEVER say referral is on file or that you will send it.

MYCHART: Direct to MyChart for lab results first. MA only if troubleshooting fails.

MYCHART ACTIVATION LINK — NEW WORKFLOW:
When patient asks for a MyChart activation link sent to them:
STEP 1: Ask: "Would you like that sent by email or by text to your cell phone?"
STEP 2A: If patient says EMAIL — ask: "What email address should I send it to?"
After patient provides email say: "I've sent the activation link to that email address."
STEP 2B: If patient says TEXT or CELL PHONE — ask: "What is the best cell number to text it to?"
After patient provides number say: "I've sent the activation link to that number."
Do NOT assume email or text — always ask first.
Do NOT say "the email we have on file" — always ask for the address or number directly.
Do NOT offer to troubleshoot unless patient says they didn't receive it.

SPEAKING TO MA: Ask reason, check availability, callback if unavailable.

BILLING: Never collect payment. Portal or billing transfer.

RESPONSE TIME: 72 standard. 24 urgent only. Once per call.

POST HOSPITAL FOLLOW-UP WORKFLOW:
When a patient or hospital transition team calls to schedule a post-hospital follow-up appointment:
1. Collect/verify patient information (name, DOB, PCP)
2. Determine caller type: patient or hospital transition team
3. Collect discharge information: hospital name, discharge date, reason for admission (optional)
4. Calculate follow-up deadline: discharge date + 7 days
5. Check PCP availability within 7 days of discharge date
6. If PCP available: present exact calendar dates and times, schedule appointment
7. If PCP not available: check covering provider Elizabeth Horowitz, APRN availability within 7 days
8. If covering provider available: present exact calendar dates and times
   - Patient callers: ask if they accept covering provider
   - Hospital team: schedule directly
9. If covering provider not available: create high priority work-in request
   - Patient callers: collect callback number, inform 24 business hours
   - Hospital team: create work-in request without callback
10. For reschedule requests:
    - Check PCP vacancy first
    - If no PCP vacancy: check covering provider vacancy
    - If no vacancies: cancel existing appointment, create high priority PCP work-in message
11. For cancellation requests:
    - Confirm cancellation
    - Cancel appointment
    - Advise that follow-up care is important
    - Wish patient a good day"""


def _generic_reschedule_avail_text():
    """Build the 'Here is what we have available' text shared by the
    generic reschedule flows. Plain weekday listing, matching the
    standard established-patient reschedule offer. New-patient records
    store weekday slots, so the calendar-dated alternative used by the
    six-month follow-up reschedule is not needed here."""
    schedule = generate_weekly_availability()
    lines = [
        f"{day}: {', '.join(slots)}"
        for day, slots in schedule.items() if slots
    ]
    return "; ".join(lines) if lines else "no availability this week"


def _household_reschedule_possessive(relation):
    """Possessive pronoun for the dual new-patient household reschedule
    ask. The scenario speaks to the caller's husband, so a gender-
    neutral 'spouse' is addressed with 'his' (matching Steve's planned
    script: '...date and time of his appointment?'); an explicit 'wife'
    maps to 'her'."""
    if relation == "wife":
        return "her"
    return "his"


def _handle_household_reschedule_continuation(message, existing_generic_record):
    """Continuation state machine for the dual NEW-PATIENT household
    reschedule started in chat() (the generic reschedule block). Stages:
    collect the spouse's first/last name + DOB + ORIGINALLY-scheduled
    appointment day/time -> present spouse availability -> capture the
    spouse's new slot -> present the caller's availability -> capture
    the caller's new slot -> confirm. Returns the next response, or None
    when no household reschedule is in progress."""
    global generic_newpatient_household_reschedule_pending
    global generic_household_reschedule_stage
    global generic_household_spouse_first, generic_household_spouse_last
    global generic_household_spouse_dob, generic_household_spouse_old_slot
    global generic_household_spouse_relation
    if not generic_newpatient_household_reschedule_pending:
        return None
    relation = generic_household_spouse_relation or "spouse"
    possessive = _household_reschedule_possessive(relation)
    stage = generic_household_reschedule_stage or "spouse_info"
    if stage == "spouse_info":
        spouse_first, spouse_last = aggressive_name_extraction(message)
        if not (spouse_first and spouse_last):
            title_cased = re.findall(r"\b([A-Z][a-z]+)\b", message)
            title_cased = [
                p for p in title_cased if p.lower() not in (
                    "her", "his", "their", "name", "is", "my", "wife",
                    "husband", "spouse", "and", "date", "birth", "of",
                    "the", "first", "last", "appointment",
                )
            ]
            if len(title_cased) >= 2:
                spouse_first, spouse_last = title_cased[0], title_cased[1]
        spouse_dob = extract_dob_from_message(message)
        spouse_old_slot = (
            _extract_calendar_date_slot_from_text(message)
            or _extract_day_time_from_reply(message)
        )
        if not (spouse_first and spouse_last):
            return f"What is your {relation}'s first and last name?"
        if not spouse_dob:
            return f"What is your {relation}'s date of birth?"
        if not spouse_old_slot:
            return (
                f"What day and time is {possessive} appointment currently "
                f"scheduled for?"
            )
        generic_household_spouse_first = spouse_first
        generic_household_spouse_last = spouse_last
        generic_household_spouse_dob = spouse_dob
        generic_household_spouse_old_slot = spouse_old_slot
        generic_household_reschedule_stage = "spouse_slot"
        return (
            f"Thank you. I have located "
            f"{spouse_first.capitalize()} {spouse_last.capitalize()}'s new "
            f"patient appointment. Here is what we have available for the "
            f"reschedule - {_generic_reschedule_avail_text()}. Which day "
            f"and time would you like to move {possessive} appointment to?"
        )
    if stage == "spouse_slot":
        spouse_first = generic_household_spouse_first
        spouse_last = generic_household_spouse_last
        # Bug fix (single-turn combined rebook): the caller may answer
        # the spouse's availability question with BOTH new times in one
        # message ("Please reschedule me for friday at 9AM and him the
        # same day at 930AM"). Previously the whole message was treated
        # as the spouse's pick - the FIRST time found (9:00 AM) was
        # stored to the SPOUSE even when the caller meant it for
        # themselves, and the caller then had to answer a second
        # availability prompt to move their own appointment. Detect the
        # speaker split ("me/mine" = caller, "him/her/his/theirs" =
        # spouse), store BOTH records, and confirm both in one
        # response.
        spouse_marker = re.search(
            r"\b(?:him|her|his|their)\b", message, re.IGNORECASE
        )
        if spouse_marker:
            caller_part = message[:spouse_marker.start()]
            spouse_part = message[spouse_marker.start():]
        else:
            caller_part = message
            spouse_part = ""
        caller_slot = (
            _extract_calendar_date_slot_from_text(caller_part)
            or _extract_day_time_from_reply(caller_part)
        )
        spouse_slot = (
            _extract_calendar_date_slot_from_text(spouse_part)
            or _extract_day_time_from_reply(spouse_part)
        )
        if not spouse_slot:
            spouse_time = _find_time_in_text(spouse_part)
            if spouse_time:
                day_match = re.search(
                    r"\b(" + "|".join(_GENERIC_APPT_DAY_NAMES) + r")\b",
                    message, re.IGNORECASE
                )
                spouse_slot = (
                    _extract_day_time_from_reply(
                        f"{day_match.group(1)} at {spouse_time}"
                    )
                    if day_match else spouse_time
                )
        if caller_slot and spouse_slot:
            caller_first = patient_first_name or caller_first_name
            caller_last = patient_last_name or caller_last_name
            store_generic_appointment_record(
                caller_first, caller_last, caller_slot,
                provider=(
                    existing_generic_record.get("provider")
                    if existing_generic_record else None
                ),
                reason=(
                    (existing_generic_record.get("reason")
                     if existing_generic_record else None)
                    or _derive_generic_appointment_reason()
                ),
            )
            store_generic_appointment_record(
                spouse_first, spouse_last, spouse_slot,
                provider=(
                    existing_generic_record.get("provider")
                    if existing_generic_record else None
                ),
                reason="New patient appointment",
            )
            generic_newpatient_household_reschedule_pending = False
            generic_household_reschedule_stage = None
            generic_household_spouse_first = None
            generic_household_spouse_last = None
            generic_household_spouse_dob = None
            generic_household_spouse_old_slot = None
            generic_household_spouse_relation = None
            return (
                f"Perfect, I've moved your appointment to {caller_slot} and "
                f"I moved {spouse_first.capitalize()} "
                f"{spouse_last.capitalize()}'s appointment to {spouse_slot}. "
                f"Is there anything else I can help you with?"
            )
        spouse_pick = (
            spouse_slot
            or _extract_calendar_date_slot_from_text(message)
            or _extract_day_time_from_reply(message)
            or message.strip()
        )
        store_generic_appointment_record(
            spouse_first, spouse_last, spouse_pick,
            provider=(
                existing_generic_record.get("provider")
                if existing_generic_record else None
            ),
            reason="New patient appointment",
        )
        generic_household_reschedule_stage = "caller_slot"
        return (
            f"Perfect, I've moved {spouse_first.capitalize()} "
            f"{spouse_last.capitalize()}'s appointment to {spouse_pick}. "
            f"Now, to reschedule your own appointment currently set for "
            f"{existing_generic_record['appointment_day']} - here is what "
            f"we have available - {_generic_reschedule_avail_text()}. "
            f"Which day and time would you like to move yours to?"
        )
    if stage == "caller_slot":
        caller_pick = (
            _extract_calendar_date_slot_from_text(message)
            or _extract_day_time_from_reply(message)
            or message.strip()
        )
        caller_first = patient_first_name or caller_first_name
        caller_last = patient_last_name or caller_last_name
        store_generic_appointment_record(
            caller_first, caller_last, caller_pick,
            provider=(
                existing_generic_record.get("provider")
                if existing_generic_record else None
            ),
            reason=(
                (existing_generic_record.get("reason")
                 if existing_generic_record else None)
                or _derive_generic_appointment_reason()
            ),
        )
        generic_newpatient_household_reschedule_pending = False
        generic_household_reschedule_stage = None
        generic_household_spouse_first = None
        generic_household_spouse_last = None
        generic_household_spouse_dob = None
        generic_household_spouse_old_slot = None
        generic_household_spouse_relation = None
        return (
            f"Thank you. I've moved your appointment to {caller_pick}. "
            f"You're both all set. Is there anything else I can help you "
            f"with today?"
        )
    return None


def _handle_household_cancel_continuation(message):
    """Continuation state machine for the dual NEW-PATIENT household
    cancellation started in chat() (the generic reschedule/cancel
    block). Mirrors _handle_household_reschedule_continuation: collects
    the spouse's first/last name + DOB + originally-scheduled
    appointment day/time, then cancels BOTH the spouse's and the
    caller's stored appointments. Returns the next response, or None
    when no household cancellation is in progress."""
    global generic_newpatient_household_cancel_pending
    global generic_household_cancel_stage
    global generic_household_cancel_spouse_first
    global generic_household_cancel_spouse_last
    global generic_household_cancel_spouse_dob
    global generic_household_cancel_spouse_old_slot
    global generic_household_cancel_spouse_relation
    if not generic_newpatient_household_cancel_pending:
        return None
    relation = generic_household_cancel_spouse_relation or "spouse"
    stage = generic_household_cancel_stage or "spouse_info"
    if stage == "spouse_info":
        spouse_first, spouse_last = aggressive_name_extraction(message)
        if not (spouse_first and spouse_last):
            title_cased = re.findall(r"\b([A-Z][a-z]+)\b", message)
            title_cased = [
                p for p in title_cased if p.lower() not in (
                    "her", "his", "their", "name", "is", "my", "wife",
                    "husband", "spouse", "and", "date", "birth", "of",
                    "the", "first", "last", "appointment",
                )
            ]
            if len(title_cased) >= 2:
                spouse_first, spouse_last = title_cased[0], title_cased[1]
        spouse_dob = extract_dob_from_message(message)
        spouse_old_slot = (
            _extract_calendar_date_slot_from_text(message)
            or _extract_day_time_from_reply(message)
        )
        if not (spouse_first and spouse_last):
            return f"What is your {relation}'s first and last name?"
        if not spouse_dob:
            return f"What is your {relation}'s date of birth?"
        if not spouse_old_slot:
            return (
                f"What day and time is {relation}'s appointment currently "
                f"scheduled for?"
            )
        generic_household_cancel_spouse_first = spouse_first
        generic_household_cancel_spouse_last = spouse_last
        generic_household_cancel_spouse_dob = spouse_dob
        generic_household_cancel_spouse_old_slot = spouse_old_slot
        caller_first = patient_first_name or caller_first_name
        caller_last = patient_last_name or caller_last_name
        cancelled_parts = []
        spouse_record = get_stored_generic_appointment_record(
            spouse_first, spouse_last
        )
        if spouse_record:
            spouse_day = spouse_record.get("appointment_day")
            if cancel_generic_appointment_record(spouse_first, spouse_last):
                cancelled_parts.append(
                    f"{spouse_first.capitalize()} "
                    f"{spouse_last.capitalize()}'s appointment "
                    f"on {spouse_day}"
                )
        caller_record = get_stored_generic_appointment_record(
            caller_first, caller_last
        )
        if caller_record:
            caller_day = caller_record.get("appointment_day")
            if cancel_generic_appointment_record(caller_first, caller_last):
                cancelled_parts.append(f"your appointment on {caller_day}")
        generic_newpatient_household_cancel_pending = False
        generic_household_cancel_stage = None
        generic_household_cancel_spouse_first = None
        generic_household_cancel_spouse_last = None
        generic_household_cancel_spouse_dob = None
        generic_household_cancel_spouse_old_slot = None
        generic_household_cancel_spouse_relation = None
        if not cancelled_parts:
            return (
                f"I'm sorry, I couldn't find any appointments on file for "
                f"{spouse_first.capitalize()} {spouse_last.capitalize()} "
                f"or yourself to cancel. Is there anything else I can help "
                f"you with today?"
            )
        return (
            f"I've cancelled {' and '.join(cancelled_parts)}. Is there "
            f"anything else I can help you with today?"
        )
    return None


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route("/")
def home():
    global profanity_count, hipaa_status_determined, current_hipaa_status
    global third_party_detected, dob_collected, pcp_collected
    global verbal_consent_requested, response_time_stated, office_hours_stated
    global patient_self_dob_verified, patient_self_last_name_verified
    global third_party_consent_asked, third_party_consent_obtained
    global urgent_symptoms_active, urgent_can_wait_asked
    global acute_same_day_established
    global ma_availability_determined, current_ma_availability
    global caller_first_name, caller_last_name, patient_first_name
    global patient_last_name, caller_is_patient, pre_chart_complete
    global is_medical_professional_caller
    global med_pro_patient_first, med_pro_patient_last
    global med_pro_patient_dob, med_pro_patient_pcp
    global med_pro_collection_complete, med_pro_referral_looked_up
    global med_pro_referral_status, med_pro_referral_date
    global med_pro_referral_provider, med_pro_referral_reason
    global ma_request_reason_asked
    global new_patient_flow_active, new_patient_requested_provider
    global new_patient_is_minor, new_patient_minor_age
    global new_patient_parent_on_line_pending
    global established_patient_minor_pending, established_patient_minor_age
    global established_patient_minor_guardian_confirmed, established_patient_dob
    global established_patient_minor_waiting
    global established_patient_minor_parent_first, established_patient_minor_parent_last
    global new_patient_eligible_providers, new_patient_offered_provider
    global new_patient_accepting_checked, new_patient_no_provider_available
    global new_patient_insurance_collected, new_patient_insurance_type
    global new_patient_insurance_name, new_patient_insurance_accepted
    global new_patient_insurance_pending_oon, new_patient_self_pay_intent
    global new_patient_demographics_stage
    global new_patient_first_name, new_patient_last_name, new_patient_dob
    global new_patient_street_address, new_patient_email, new_patient_phone
    global new_patient_member_number, new_patient_text_consent
    global new_patient_weekly_schedule, new_patient_appointment_selection
    global new_patient_household_relation, new_patient_household_stage
    global new_patient_household_second_time, new_patient_household_primary_first_name
    global new_patient_household_primary_address
    global lab_result_fax_active
    global lab_result_inquiry_active
    global contagious_visit_active, contagious_same_day_check_pending
    global virtual_visit_offered
    global provider_callin_offer_pending
    global symptoms_pharmacy_pending
    global covering_provider_offer_pending, covering_provider_visit_eligible
    global covering_provider_same_day_determined
    global current_covering_provider_same_day_available
    global same_day_virtual_clinic_offer_pending
    global next_available_options_pending
    global next_day_or_urgent_care_pending
    global acute_existing_appt_day, acute_existing_appt_time
    global acute_reschedule_confirm_pending, acute_cancel_confirm_pending
    global acute_new_time_pending
    global generic_reschedule_pending, generic_joint_cancel_pending
    global generic_cancelled_reschedule_pending
    global generic_cancelled_reschedule_offered, generic_cancelled_reschedule_provider
    global generic_spouse_reschedule_pending
    global generic_spouse_reschedule_slot, generic_spouse_reschedule_relation
    global generic_newpatient_household_reschedule_pending
    global generic_household_reschedule_stage, generic_household_spouse_first
    global generic_household_spouse_last, generic_household_spouse_dob
    global generic_household_spouse_old_slot, generic_household_spouse_relation
    global virtual_wait_active, virtual_wait_choice_pending
    global virtual_wait_reschedule_pending, virtual_wait_reschedule_schedule
    global virtual_wait_reschedule_provider
    global late_arrival_active, late_arrival_minutes_asked
    global late_arrival_reschedule_pending, late_arrival_reschedule_offered
    global late_arrival_reschedule_schedule
    global generic_weekly_schedule_snapshot
    global generic_covering_weekly_schedule_snapshot
    global couple_followup_flow_active, couple_followup_previous_visit_date
    global couple_followup_available_pairs
    global controlled_substance_appt_pending, controlled_substance_appt_medication_word
    global controlled_substance_appt_schedule, controlled_substance_bridge_awaiting_days
    global controlled_substance_appt_day_name, controlled_substance_appt_date
    global controlled_substance_bridge_awaiting_dosage, controlled_substance_bridge_dosage
    global controlled_substance_bridge_awaiting_pharmacy
    global controlled_substance_bridge_awaiting_callback, controlled_substance_bridge_callback_number
    global controlled_substance_bridge_confirmed_insufficient
    global ma_request_reason_collected
    global referral_lookup_done, referral_lookup_result
    global referral_specialist_name, referral_specialist_phone
    global nurse_visit_active

    profanity_count = 0
    hipaa_status_determined = False
    current_hipaa_status = None
    third_party_detected = False
    dob_collected = False
    patient_self_dob_verified = False
    patient_self_last_name_verified = False
    pcp_collected = False
    verbal_consent_requested = False
    third_party_consent_asked = False
    third_party_consent_obtained = False
    response_time_stated = False
    office_hours_stated = False
    lab_result_fax_active = False
    lab_result_inquiry_active = False
    contagious_visit_active = False
    contagious_same_day_check_pending = False
    virtual_visit_offered = False
    provider_callin_offer_pending = False
    symptoms_pharmacy_pending = False
    provider_callin_decision_determined = False
    current_provider_callin_decision = None
    uti_antibiotic_demand_active = False
    ma_same_day_approval_determined = False
    current_ma_same_day_approval = None
    covering_provider_offer_pending = False
    covering_provider_visit_eligible = False
    covering_provider_same_day_determined = False
    current_covering_provider_same_day_available = None
    same_day_virtual_clinic_offer_pending = False
    next_available_options_pending = False
    next_day_or_urgent_care_pending = False
    acute_existing_appt_day = None
    acute_existing_appt_time = None
    acute_reschedule_confirm_pending = False
    acute_cancel_confirm_pending = False
    acute_new_time_pending = False
    generic_reschedule_pending = False
    generic_joint_cancel_pending = False
    generic_cancelled_reschedule_pending = False
    generic_cancelled_reschedule_offered = False
    generic_cancelled_reschedule_provider = None
    generic_spouse_reschedule_pending = False
    generic_spouse_reschedule_slot = None
    generic_spouse_reschedule_relation = None
    generic_newpatient_household_reschedule_pending = False
    generic_household_reschedule_stage = None
    generic_household_spouse_first = None
    generic_household_spouse_last = None
    generic_household_spouse_dob = None
    generic_household_spouse_old_slot = None
    generic_household_spouse_relation = None
    generic_newpatient_household_cancel_pending = False
    generic_household_cancel_stage = None
    generic_household_cancel_spouse_first = None
    generic_household_cancel_spouse_last = None
    generic_household_cancel_spouse_dob = None
    generic_household_cancel_spouse_old_slot = None
    generic_household_cancel_spouse_relation = None
    virtual_wait_active = False
    virtual_wait_choice_pending = False
    virtual_wait_reschedule_pending = False
    virtual_wait_reschedule_schedule = None
    virtual_wait_reschedule_provider = None
    late_arrival_active = False
    late_arrival_minutes_asked = False
    late_arrival_reschedule_pending = False
    late_arrival_reschedule_offered = False
    late_arrival_reschedule_schedule = None
    generic_weekly_schedule_snapshot = None
    generic_covering_weekly_schedule_snapshot = None
    couple_followup_flow_active = False
    couple_followup_previous_visit_date = None
    couple_followup_available_pairs = None
    individual_six_month_followup_active = False
    individual_six_month_followup_previous_visit_date = None
    individual_six_month_followup_days_since = None
    individual_six_month_followup_eligible = None
    individual_six_month_followup_spouse_stage = None
    individual_six_month_followup_spouse_slot = None
    individual_six_month_followup_spouse_relation = None
    individual_six_month_followup_spouse_first_name = None
    individual_six_month_followup_spouse_last_name = None
    individual_three_month_followup_active = False
    individual_three_month_followup_previous_visit_date = None
    individual_three_month_followup_days_since = None
    controlled_substance_appt_pending = False
    controlled_substance_appt_medication_word = None
    controlled_substance_appt_schedule = None
    controlled_substance_bridge_awaiting_days = False
    controlled_substance_appt_day_name = None
    controlled_substance_appt_date = None
    controlled_substance_bridge_awaiting_dosage = False
    controlled_substance_bridge_dosage = None
    controlled_substance_bridge_awaiting_pharmacy = False
    controlled_substance_bridge_awaiting_callback = False
    controlled_substance_bridge_callback_number = None
    controlled_substance_bridge_confirmed_insufficient = False
    ma_request_active = False
    ma_request_name = None
    ma_request_provider = None
    ma_request_reason_collected = False
    ma_request_reason_asked = False
    referral_lookup_done = False
    referral_lookup_result = None
    referral_specialist_name = None
    referral_specialist_phone = None
    nurse_visit_active = False
    new_patient_flow_active = False
    new_patient_requested_provider = None
    new_patient_is_minor = None
    new_patient_is_minor = None
    new_patient_minor_age = None
    new_patient_parent_on_line_pending = None
    established_patient_minor_pending = None
    established_patient_minor_age = None
    established_patient_minor_guardian_confirmed = False
    established_patient_dob = None
    established_patient_minor_waiting = False
    established_patient_minor_parent_first = None
    established_patient_minor_parent_last = None
    new_patient_eligible_providers = None
    new_patient_offered_provider = None
    new_patient_accepting_checked = False
    new_patient_no_provider_available = False
    new_patient_insurance_collected = False
    new_patient_insurance_type = None
    new_patient_insurance_name = None
    new_patient_insurance_accepted = None
    new_patient_insurance_pending_oon = False
    new_patient_self_pay_intent = False
    third_party_availability_asked = False
    new_patient_demographics_stage = None
    new_patient_first_name = None
    new_patient_last_name = None
    new_patient_dob = None
    new_patient_street_address = None
    new_patient_email = None
    new_patient_phone = None
    new_patient_member_number = None
    new_patient_text_consent = None
    new_patient_weekly_schedule = None
    new_patient_appointment_selection = None
    new_patient_household_relation = None
    new_patient_household_stage = "primary"
    new_patient_household_second_time = None
    new_patient_household_primary_first_name = None
    new_patient_household_primary_address = None
    new_patient_household_speaker_active = False
    urgent_symptoms_active = False
    urgent_can_wait_asked = False
    acute_same_day_established = False
    ma_availability_determined = False
    current_ma_availability = None
    caller_first_name = None
    caller_last_name = None
    patient_first_name = None
    patient_last_name = None
    caller_is_patient = None
    pre_chart_complete = False
    is_medical_professional_caller = False
    med_pro_patient_first = None
    med_pro_patient_last = None
    med_pro_patient_dob = None
    med_pro_patient_pcp = None
    med_pro_collection_complete = False
    med_pro_referral_looked_up = False
    med_pro_referral_status = None
    med_pro_referral_date = None
    med_pro_referral_provider = None
    med_pro_referral_reason = None
    Sprint13.reset_state()
    Sprint14.reset_state()
    conversation_history.clear()
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    global profanity_count, third_party_detected, dob_collected
    global patient_self_dob_verified, patient_self_last_name_verified
    global pcp_collected, verbal_consent_requested, third_party_consent_asked
    global third_party_consent_obtained, response_time_stated
    global office_hours_stated, lab_result_fax_active
    global lab_result_inquiry_active
    global contagious_visit_active, contagious_same_day_check_pending
    global virtual_visit_offered
    global provider_callin_offer_pending
    global symptoms_pharmacy_pending
    global provider_callin_decision_determined, current_provider_callin_decision
    global uti_antibiotic_demand_active
    global ma_same_day_approval_determined, current_ma_same_day_approval
    global covering_provider_offer_pending, covering_provider_visit_eligible
    global covering_provider_same_day_determined
    global current_covering_provider_same_day_available
    global same_day_virtual_clinic_offer_pending
    global next_available_options_pending
    global next_day_or_urgent_care_pending
    global acute_existing_appt_day, acute_existing_appt_time
    global acute_reschedule_confirm_pending, acute_cancel_confirm_pending
    global acute_new_time_pending
    global generic_reschedule_pending, generic_joint_cancel_pending
    global generic_cancelled_reschedule_pending
    global generic_cancelled_reschedule_offered, generic_cancelled_reschedule_provider
    global generic_spouse_reschedule_pending
    global generic_spouse_reschedule_slot, generic_spouse_reschedule_relation
    global generic_newpatient_household_reschedule_pending
    global generic_household_reschedule_stage, generic_household_spouse_first
    global generic_household_spouse_last, generic_household_spouse_dob
    global generic_household_spouse_old_slot, generic_household_spouse_relation
    global generic_newpatient_household_cancel_pending
    global generic_household_cancel_stage, generic_household_cancel_spouse_first
    global generic_household_cancel_spouse_last, generic_household_cancel_spouse_dob
    global generic_household_cancel_spouse_old_slot, generic_household_cancel_spouse_relation
    global virtual_wait_active, virtual_wait_choice_pending
    global virtual_wait_reschedule_pending, virtual_wait_reschedule_schedule
    global virtual_wait_reschedule_provider
    global late_arrival_active, late_arrival_minutes_asked
    global late_arrival_reschedule_pending, late_arrival_reschedule_offered
    global late_arrival_reschedule_schedule
    global generic_weekly_schedule_snapshot, generic_covering_weekly_schedule_snapshot
    global couple_followup_flow_active, couple_followup_previous_visit_date
    global couple_followup_available_pairs
    global individual_six_month_followup_active
    global individual_six_month_followup_previous_visit_date
    global individual_six_month_followup_days_since
    global individual_six_month_followup_eligible
    global individual_six_month_followup_spouse_stage
    global individual_six_month_followup_spouse_slot
    global individual_six_month_followup_spouse_relation
    global individual_six_month_followup_spouse_first_name
    global individual_six_month_followup_spouse_last_name
    global individual_three_month_followup_active
    global individual_three_month_followup_previous_visit_date
    global individual_three_month_followup_days_since
    global controlled_substance_appt_pending, controlled_substance_appt_medication_word
    global controlled_substance_appt_schedule, controlled_substance_bridge_awaiting_days
    global controlled_substance_appt_day_name, controlled_substance_appt_date
    global controlled_substance_bridge_awaiting_dosage, controlled_substance_bridge_dosage
    global controlled_substance_bridge_awaiting_pharmacy
    global controlled_substance_bridge_awaiting_callback, controlled_substance_bridge_callback_number
    global controlled_substance_bridge_confirmed_insufficient
    global referral_lookup_done, referral_lookup_result
    global referral_specialist_name, referral_specialist_phone
    global nurse_visit_active
    global ma_request_active, ma_request_name
    global ma_request_provider, ma_request_reason_collected
    global ma_request_reason_asked
    global new_patient_flow_active, new_patient_requested_provider
    global new_patient_is_minor, new_patient_minor_age
    global new_patient_parent_on_line_pending
    global established_patient_minor_pending, established_patient_minor_age
    global established_patient_minor_guardian_confirmed, established_patient_dob
    global established_patient_minor_waiting
    global established_patient_minor_parent_first, established_patient_minor_parent_last
    global new_patient_eligible_providers, new_patient_offered_provider
    global new_patient_accepting_checked, new_patient_no_provider_available
    global new_patient_insurance_collected, new_patient_insurance_type
    global new_patient_insurance_name, new_patient_insurance_accepted
    global new_patient_insurance_pending_oon, new_patient_self_pay_intent
    global new_patient_demographics_stage
    global new_patient_first_name, new_patient_last_name, new_patient_dob
    global new_patient_street_address, new_patient_email, new_patient_phone
    global new_patient_member_number, new_patient_text_consent
    global new_patient_weekly_schedule, new_patient_appointment_selection
    global new_patient_household_relation, new_patient_household_stage
    global new_patient_household_second_time, new_patient_household_primary_first_name
    global new_patient_household_primary_address
    global urgent_symptoms_active, urgent_can_wait_asked
    global acute_same_day_established
    global ma_availability_determined, current_ma_availability
    global caller_first_name, caller_last_name, patient_first_name
    global patient_last_name, caller_is_patient, pre_chart_complete
    global is_medical_professional_caller
    global med_pro_patient_first, med_pro_patient_last
    global med_pro_patient_dob, med_pro_patient_pcp
    global med_pro_collection_complete, med_pro_referral_looked_up
    global med_pro_referral_status, med_pro_referral_date
    global med_pro_referral_provider, med_pro_referral_reason

    user_message = request.json.get("message")
    message_lower = user_message.lower()

    if (
            "cancel" in message_lower
            and re.search(r"\bour\s+new[\s-]patient\s+appointments?\b", message_lower)
            and _JOINT_APPOINTMENT_REFERENCE_PATTERN.search(message_lower)
    ):
        generic_joint_cancel_pending = True

    if is_couple_six_month_followup_request(message_lower):
        couple_followup_flow_active = True
    elif is_six_month_followup_request(message_lower):
        individual_six_month_followup_active = True
        if individual_six_month_followup_previous_visit_date is None:
            individual_six_month_followup_eligible = random.random() >= 0.60
            individual_six_month_followup_days_since = random.randint(
                180, 365
            ) if individual_six_month_followup_eligible else random.randint(1, 179)
            individual_six_month_followup_previous_visit_date = (
                datetime.now() - timedelta(
                    days=individual_six_month_followup_days_since
                )
            ).strftime("%B %d, %Y")

    if is_three_month_followup_request(message_lower):
        individual_three_month_followup_active = True
        if individual_three_month_followup_previous_visit_date is None:
            individual_three_month_followup_days_since = random.randint(
                60, 120
            )
            individual_three_month_followup_previous_visit_date = (
                datetime.now() - timedelta(
                    days=individual_three_month_followup_days_since
                )
            ).strftime("%B %d, %Y")

    # Capture "wants lab results" intent the moment it is ever stated,
    # even if this turn's message gets intercepted by an earlier-return
    # path (e.g. still collecting caller name/DOB/PCP for a third party).
    # Without this, the intent stated in message 1 of a multi-turn
    # pre-chart collection is lost by the time DOB/PCP finish collecting,
    # and the AI has no signal to distinguish a lab RESULTS inquiry from
    # a lab ORDER pickup request.
    if any(trigger in message_lower for trigger in LAB_RESULT_INQUIRY_TRIGGERS):
        lab_result_inquiry_active = True

    # Same reasoning as above: capture "this is a contagious complaint"
    # the moment it's ever stated, even if this turn gets intercepted by
    # an earlier-return path. A third-party caller (e.g. a parent calling
    # about a child) often states the symptom before pre-chart/HIPAA
    # collection completes, and the virtual-visit refusal may come
    # several turns later.
    if any(trigger in message_lower for trigger in CONTAGIOUS_SYMPTOM_TRIGGERS):
        contagious_visit_active = True

    # Sprint 13: Capture post-hospital follow-up intent early so it
    # survives any earlier-return path (e.g. pre-chart collection).
    if Sprint13.detect_post_hospital_intent(message_lower):
        Sprint13.phf_intent_detected = True

    # Sprint 13: Capture appointment-inquiry intent early too, for the
    # same reason. A caller asking "when is my post hospital follow up?"
    # BEFORE identifying themselves (pre_chart_complete still False)
    # would otherwise have this intent silently lost - the inquiry check
    # only lives inside the elif chain gated behind pre_chart_complete,
    # so the message fell through to the AI and got improvised as if it
    # were the start of a brand-new scheduling conversation.
    if Sprint13.detect_phf_inquiry_intent(message_lower):
        Sprint13.phf_inquiry_intent_detected = True

    # Sprint 13: A hospital transition team caller typically identifies
    # their hospital in their very first message via "from the X
    # Transitions team" phrasing (e.g. "This is Janet from the Orlando
    # Health Transitions team"). That organization name does not carry
    # the "Hospital"/"Medical Center"/"Clinic" suffix Sprint13's own
    # discharge_info-stage extraction looks for, and that extraction
    # doesn't run yet anyway (Sprint13.phf_flow_active only turns on once
    # pre-chart collection finishes). Without capturing it here, the
    # hospital name is still unknown when phf_context is built below for
    # the AI, so the AI redundantly re-asks "what hospital was the
    # patient discharged from" even though the caller already stated it.
    if not Sprint13.phf_hospital_name:
        hospital_transitions_match = re.search(
            r"(?:from|at|with)\s+(?:the\s+)?([A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)*?)"
            r"\s+Transitions?\s+[Tt]eam",
            user_message,
            re.IGNORECASE
        )
        if hospital_transitions_match:
            Sprint13.phf_hospital_name = hospital_transitions_match.group(1).strip()

    if not Sprint13.phf_caller_type:
        if "hospital" in message_lower and (
                "transition" in message_lower or "team" in message_lower
                or "calling from" in message_lower
        ):
            Sprint13.phf_caller_type = "hospital_team"

    # Capture same-day/acute urgency the moment it's ever stated, even if
    # this turn gets intercepted by an earlier-return path (e.g. Sprint14's
    # acute pivot handoff, which returns before the later is_same_day
    # computation runs). Mirrors the Sprint13/Sprint14 intent-capture
    # pattern directly below. Once set, stays True for the rest of the
    # call (reset at Sprint14.reset_state() time) so a later message that
    # doesn't repeat "today" (e.g. a symptom description that follows up
    # on an already-stated same-day request) doesn't lose the urgency
    # already established.
    if (
            any(word in message_lower for word in SAME_DAY_KEYWORDS)
            and any(word in message_lower for word in SAME_DAY_ACTION_WORDS)
    ):
        acute_same_day_established = True

    # Sprint 14: Capture wellness/MAW/CHA, refill-escalation, and
    # lab-order-EHR intent the moment they're ever stated, even if this
    # turn gets intercepted by an earlier-return path (e.g. still
    # collecting caller name/DOB/PCP). Mirrors the Sprint13
    # phf_intent_detected early-capture pattern directly above.
    #
    # Gated on "not wellness_flow_active" (previously missing here, and
    # only partially present for wellness): without it, a message sent
    # WHILE a flow is already active can still contain trigger words
    # (e.g. "I would still like to schedule my annual physical" still
    # contains "physical") and silently repopulate the *_intent_detected
    # flag right after it was just cleared on consumption - reopening
    # the exact reactivation bug fixed at the activation block below,
    # since that flag isn't consumed again until the flow next goes
    # idle (i.e. after the call has already moved on to closing).
    if (
            not Sprint14.wellness_intent_detected
            and not Sprint14.wellness_flow_active
    ):
        _wellness_intent = Sprint14.detect_wellness_intent(message_lower)
        if _wellness_intent:
            Sprint14.wellness_intent_detected = _wellness_intent
    if (
            not Sprint14.wellness_flow_active
            and Sprint14.detect_refill_escalation_intent(message_lower)
    ):
        Sprint14.refill_intent_detected = True
    if (
            not Sprint14.wellness_flow_active
            and (
            Sprint14.detect_lab_order_ehr_intent(message_lower)
            or Sprint14.detect_lab_send_request_intent(message_lower)
    )
    ):
        Sprint14.lab_order_intent_detected = True

    # --- Profanity check ---
    profanity_words = [
        "shit", "fuck", "fucking", "cunt", "bitch",
        "bastard", "crap", "piss", "asshole"
    ]
    contains_profanity = any(
        re.search(r'\b' + word + r'\b', message_lower)
        for word in profanity_words
    )
    if contains_profanity:
        profanity_count += 1
        if profanity_count == 1:
            warning = ("I am here to help you today, however if the abusive "
                       "language continues I will need to terminate this call.")
            return jsonify({"response": warning})
        elif profanity_count >= 2:
            profanity_count = 0
            conversation_history.clear()
            termination = ("I am terminating this call due to the abusive "
                           "language. Please call back when you are ready to "
                           "speak respectfully. Goodbye.")
            return jsonify({"response": termination, "terminated": True})

        # --- Medical professional intercept ---
    # Bug fix: skip this intercept when the SAME message has already
    # signaled post-hospital-follow-up intent (Sprint13.phf_intent_detected,
    # set just above at the "Sprint 13" capture block). Without this
    # guard, a hospital transition team caller's normal self-introduction
    # ("This is Janet from the Orlando Health Transitions team...") matches
    # is_medical_professional_message()'s generic "this is X from Y"
    # fallback pattern and gets misrouted into handle_med_pro_collection(),
    # which has no real patient name to extract and fabricates one from
    # the hospital's name instead (e.g. asking for "Orlando Health's date
    # of birth") rather than letting Sprint13 correctly ask for the
    # patient's first and last name.
    if (not is_medical_professional_caller and not Sprint13.phf_flow_active
            and not Sprint13.phf_intent_detected
            and not Sprint14.wellness_flow_active):
        if is_medical_professional_message(user_message, message_lower):
            is_medical_professional_caller = True
            pre_chart_complete = True

    if is_medical_professional_caller:
        if not med_pro_collection_complete:
            python_response = handle_med_pro_collection(
                user_message, message_lower
            )
            if python_response is not None:
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": python_response}
                )
                if check_response_time_stated(python_response):
                    response_time_stated = True
                return jsonify({"response": python_response})

    # Intercept address follow-up in lab result fax workflow
    # When lab_result_fax_active is True and patient gives an address
    # return directly from Python to prevent AI from asking for patient name
    if lab_result_fax_active and detect_address_in_message(message_lower) and pre_chart_complete:
        address_response = (
            f"Thank you. I have located the fax number for that address "
            f"and have faxed your lab results over to the office. "
            f"Is there anything else I can help you with today?"
        )
        conversation_history.append({"role": "user", "content": user_message})
        conversation_history.append({"role": "assistant", "content": address_response})
        return jsonify({"response": address_response})

    # --- New patient workflow (Sprint 11) — before pre-chart ---
    if not is_medical_professional_caller:
        # "is dr."/"is doctor"/"does dr."/"does doctor" correctly catch
        # "Is Dr. Smith accepting new patients?" but also false-positive on
        # a caller simply STATING an EXISTING patient's PCP, e.g. "his
        # doctor is Dr. Brooks". Suppress the match in that statement
        # pattern so PCP identification for an existing patient does not
        # get misrouted into new-patient scheduling.
        pcp_statement_pattern = re.search(
            r"(?:doctor|physician|provider|pcp)\s+(?:is|does)\s+(?:dr\.?|doctor)\b",
            message_lower
        )
        ambiguous_new_patient_phrases = ["is dr.", "is doctor", "does dr.", "does doctor"]
        existing_new_patient_appt_statement = bool(
            _EXISTING_NEW_PATIENT_APPOINTMENT_PATTERN.search(message_lower)
        )
        # Bug fix: "for my daughter" / "schedule my son" / "establish my
        # child" are how a parent asks for NEW-patient intake for a child -
        # but the SAME phrase appears when the parent of an ESTABLISHED
        # minor books for the patient already identified in this call
        # ("I'd like to schedule an appointment for my daughter Alice"
        # after Alice's name/DOB/PCP were collected and the guardian was
        # confirmed). Once this call already has an established patient
        # with a DOB on file and pre-chart complete, a bare relationship
        # phrase must NOT reopen new-patient intake and re-ask for the
        # provider. An explicit "new patient"/"not a patient" in the same
        # message still starts new-patient intake, since it is a different
        # trigger and is unaffected by this exclusion.
        relationship_new_patient_phrases = (
            "schedule my child", "schedule my son", "schedule my daughter",
            "for my son", "for my daughter", "for my child",
            "establish my child", "establish my son", "establish my daughter",
        )
        established_patient_identified = bool(
            pre_chart_complete and established_patient_dob
        )
        unambiguous_match = (
                any(
                    trigger in message_lower for trigger in NEW_PATIENT_TRIGGERS
                    if trigger not in ambiguous_new_patient_phrases
                    and not (
                        established_patient_identified
                        and trigger in relationship_new_patient_phrases
                    )
                )
                and not existing_new_patient_appt_statement
        )
        ambiguous_match = (
                any(trigger in message_lower for trigger in ambiguous_new_patient_phrases)
                and not pcp_statement_pattern
                and not (pre_chart_complete and caller_is_patient)
                and not existing_new_patient_appt_statement
        )
        if unambiguous_match or ambiguous_match:
            new_patient_flow_active = True
            if new_patient_household_relation is None:
                new_patient_household_relation = detect_new_patient_household_relation(
                    message_lower
                )
            if any(phrase in message_lower for phrase in [
                "not a patient", "i'm not a patient", "i am not a patient",
                "new patient", "become a patient",
            ]):
                caller_is_patient = False
                pre_chart_complete = True

    if ( new_patient_flow_active
        and not any(
                trigger in message_lower
                for trigger in CANCEL_ACUTE_VISIT_TRIGGERS
            )
    ):
        new_patient_response = handle_new_patient_flow(
            user_message, message_lower
        )
        if new_patient_response is not None:
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": new_patient_response}
            )
            return jsonify({"response": new_patient_response})

    elif not pre_chart_complete:
        python_response = determine_pre_chart_response(
            user_message, message_lower
        )
        if python_response is not None:
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": python_response}
            )
            return jsonify({"response": python_response})
        # Patient gave everything at once — pre_chart_complete just
        # became True and no intermediate question was needed.
        # Return natural response directly without passing to AI.
        # Only return Python chart response for patients calling themselves.
        # Third party callers must fall through to AI so HIPAA can fire.
        # Also step aside for any caller with a pending PHF intent
        # (scheduling or inquiry) so the Sprint 13 dispatch block below
        # gets a chance to run, instead of this generic shortcut
        # preempting it when identification completes in the same
        # message that also expressed the PHF intent.
        if (
                pre_chart_complete and not third_party_detected
                and Sprint13.phf_caller_type != "hospital_team"
                and not Sprint13.phf_intent_detected
                and not Sprint13.phf_inquiry_intent_detected
                and not Sprint14.wellness_intent_detected
                and not Sprint14.refill_intent_detected
                and not Sprint14.lab_order_intent_detected
                and not couple_followup_flow_active
                # Bug fix: this shortcut fires the moment pre-chart
                # collection completes, without checking whether the
                # SAME message that completed it also expressed explicit
                # appointment-management intent (e.g. "...with Dr. Brooks
                # and we'd like to reschedule our appointments") - that
                # intent was silently discarded in favor of a generic
                # "how can I help you today?" handoff. Mirrors the
                # existing PHF/wellness/refill/lab-order guards just above.
                and not any(
            trigger in message_lower for trigger in (
                    RESCHEDULE_ACUTE_VISIT_TRIGGERS
                    + CANCEL_ACUTE_VISIT_TRIGGERS
                    + QUERY_ACUTE_VISIT_TRIGGERS
                    + HOUSEHOLD_CANCEL_TRIGGERS
            )
        )
                and not detect_virtual_wait_annoyance(message_lower)
        ):
            chart_response = "Thank you for that. How can I help you today?"
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": chart_response}
            )
            return jsonify({"response": chart_response})

    # ── Sprint 13: Post Hospital Follow-Up Workflow ──
    if (
            couple_followup_flow_active and pre_chart_complete
            and not is_medical_professional_caller
    ):
        couple_followup_response = handle_couple_six_month_followup_flow(
            user_message
        )
        conversation_history.append({"role": "user", "content": user_message})
        conversation_history.append(
            {"role": "assistant", "content": couple_followup_response}
        )
        return jsonify({"response": couple_followup_response})

    if (
            individual_six_month_followup_active
            and pre_chart_complete
            and not is_medical_professional_caller
    ):
        spouse_response = None
        if individual_six_month_followup_spouse_stage == "name":
            spouse_first, spouse_last = aggressive_name_extraction(user_message)
            if not spouse_first or not spouse_last:
                parts = user_message.strip().split()
                if len(parts) >= 2:
                    spouse_first, spouse_last = parts[0], parts[1]
            if spouse_first and spouse_last:
                individual_six_month_followup_spouse_first_name = spouse_first
                individual_six_month_followup_spouse_last_name = spouse_last
                individual_six_month_followup_spouse_stage = "dob"
                possessive = _household_pronoun_possessive(
                    individual_six_month_followup_spouse_relation
                )
                spouse_response = f"Thank you. Could I get {possessive} date of birth?"
            else:
                spouse_response = "What is her first and last name?"
        elif individual_six_month_followup_spouse_stage == "dob":
            if detect_dob_in_message(user_message):
                individual_six_month_followup_spouse_stage = "pcp"
                spouse_first = individual_six_month_followup_spouse_first_name
                spouse_pcp = get_patient_pcp_from_history()
                if spouse_pcp:
                    spouse_response = (
                        f"Thank you. Just to confirm, is {spouse_first} "
                        f"also seeing {spouse_pcp}?"
                    )
                else:
                    spouse_response = (
                        f"Thank you. Does {spouse_first} also see the "
                        f"same primary care provider as you?"
                    )
            else:
                possessive = _household_pronoun_possessive(
                    individual_six_month_followup_spouse_relation
                )
                spouse_response = f"Could I get {possessive} date of birth?"
        elif individual_six_month_followup_spouse_stage == "pcp":
            store_generic_appointment_record(
                individual_six_month_followup_spouse_first_name,
                individual_six_month_followup_spouse_last_name,
                individual_six_month_followup_spouse_slot,
                reason=_derive_generic_appointment_reason(),
            )
            individual_six_month_followup_active = False
            individual_six_month_followup_spouse_stage = None
            spouse_response = (
                f"Thank you. I have {individual_six_month_followup_spouse_first_name} "
                f"scheduled for {individual_six_month_followup_spouse_slot}. "
                f"Is there anything else I can help you with today?"
            )
        else:
            primary_slot, spouse_relation, spouse_slot = extract_household_scheduling(
                user_message, message_lower
            )
            # Bug fix (household follow-up question order): this
            # six-month follow-up flow offers appointments as calendar
            # dates ("March 09, 2027"), never weekday names, so the slot
            # extraction above cannot find a day and spouse_slot comes
            # back None even when the caller clearly added a spouse
            # request ("...please schedule my wife for 10:30 AM that
            # same day"). The turn then fell through to the AI, which
            # improvised the spouse collection in the wrong order
            # (asking for her date of birth before her first and last
            # name) and re-asked whether the spouse sees the same PCP.
            # Fall back to a time-only slot parsed from the spouse
            # portion of the message so the deterministic name-first ->
            # DOB -> schedule machine below still engages for this
            # household request, preserving the household context.
            if spouse_relation and not spouse_slot:
                _spouse_match = _HOUSEHOLD_SECOND_APPT_PATTERN.search(message_lower)
                if _spouse_match:
                    spouse_time = _find_time_in_text(
                        user_message[_spouse_match.start():]
                    )
                    if spouse_time:
                        spouse_slot = spouse_time
            # Bug fix (primary slot lost on calendar-date phrasing):
            # the caller's OWN slot suffers the same weekday-extraction
            # failure the spouse slot just fell back from - this flow
            # offers calendar dates ("October 05, 2026"), so a caller
            # picking "October 5 at 9AM" leaves primary_slot at its raw
            # message fallback, and the `!= user_message` guards below
            # skip persisting/confirming the caller's own appointment.
            # Recover it from the primary portion of the message so the
            # primary patient's record is stored like the spouse's.
            _primary_match = _HOUSEHOLD_SECOND_APPT_PATTERN.search(message_lower)
            if (
                    spouse_relation
                    and primary_slot.strip() == user_message.strip()
                    and _primary_match
            ):
                _primary_calendar_slot = _extract_calendar_date_slot_from_text(
                    user_message[:_primary_match.start()]
                )
                if _primary_calendar_slot:
                    primary_slot = _primary_calendar_slot
            if spouse_relation and spouse_slot:
                # Only persist the caller's own slot when the weekday
                # extraction succeeded - otherwise primary_slot is the
                # raw message (calendar-date phrasing) and storing it
                # would corrupt the appointment record.
                if primary_slot.strip() != user_message.strip():
                    store_generic_appointment_record(
                        caller_first_name, caller_last_name, primary_slot,
                        reason=_derive_generic_appointment_reason(),
                    )
                individual_six_month_followup_spouse_stage = "name"
                individual_six_month_followup_spouse_slot = spouse_slot
                individual_six_month_followup_spouse_relation = spouse_relation
                name_question = (
                    "What is her first and last name?"
                    if spouse_relation == "wife"
                    else f"What is your {spouse_relation}'s first and last name?"
                )
                if primary_slot.strip() != user_message.strip():
                    spouse_response = (
                        f"I have you scheduled for {primary_slot}. {name_question}"
                    )
                else:
                    spouse_response = name_question
        if spouse_response:
            conversation_history.append({"role": "user", "content": user_message})
            conversation_history.append({"role": "assistant", "content": spouse_response})
            return jsonify({"response": spouse_response})

    if pre_chart_complete and not Sprint13.phf_flow_active:
        if Sprint13.phf_intent_detected:
            Sprint13.phf_flow_active = True
            # Copy pre-chart patient data
            if patient_first_name:
                Sprint13.phf_patient_first_name = patient_first_name
            if patient_last_name:
                Sprint13.phf_patient_last_name = patient_last_name
            if dob_collected:
                Sprint13.phf_patient_dob = "collected"
            if pcp_collected:
                provider = detect_provider_in_message(message_lower)
                if provider:
                    Sprint13.phf_patient_pcp = provider
            # If pre-chart already identified caller as patient, skip caller-type question
            if caller_is_patient is True:
                Sprint13.phf_caller_type = "patient"
                Sprint13.phf_stage = "discharge_info"
            elif Sprint13.phf_caller_type == "hospital_team":
                Sprint13.phf_stage = "discharge_info"
            else:
                Sprint13.phf_stage = "who_is_calling"
        elif Sprint13.detect_phf_reschedule_intent(message_lower) or (
                Sprint13.detect_generic_reschedule_intent(message_lower)
                and Sprint13.get_stored_appointment_record(patient_first_name, patient_last_name)
        ):
            Sprint13.phf_flow_active = True
            Sprint13.phf_reschedule_pending = True
        elif Sprint13.detect_phf_cancel_intent(message_lower):
            Sprint13.phf_flow_active = True
            Sprint13.phf_cancel_pending = True
        elif Sprint13.detect_phf_inquiry_intent(message_lower) or Sprint13.phf_inquiry_intent_detected or (
                detect_generic_appointment_inquiry_intent(message_lower)
                and Sprint13.get_stored_appointment_record(patient_first_name, patient_last_name)
        ):
            Sprint13.phf_flow_active = True
            Sprint13.phf_inquiry_pending = True
            Sprint13.phf_inquiry_intent_detected = False
            if patient_first_name:
                Sprint13.phf_patient_first_name = patient_first_name
            if patient_last_name:
                Sprint13.phf_patient_last_name = patient_last_name
            if caller_is_patient is True:
                Sprint13.phf_caller_type = "patient"
            elif Sprint13.phf_caller_type == "hospital_team":
                pass
            else:
                Sprint13.phf_caller_type = "patient"

    if Sprint13.phf_flow_active:
        phf_response = Sprint13.handle_post_hospital_flow(
            user_message, message_lower
        )
        if phf_response is not None:
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": phf_response}
            )
            return jsonify({"response": phf_response})

    # ── Sprint 14: Appointment lookup ("When is my annual physical?") ──
    # Must run before wellness-intent activation below, per
    # Sprint14.detect_appointment_lookup_intent()'s own docstring: a
    # caller asking about an already-scheduled appointment should get
    # the persisted record, not be routed into the scheduling flow.
    # Gated the same way as the wellness activation block just below
    # (pre-chart complete, no PHF or active wellness conversation) so
    # it can't hijack an in-progress flow.
    if (
            pre_chart_complete and not Sprint13.phf_flow_active
            and not Sprint14.wellness_flow_active
            and Sprint14.detect_appointment_lookup_intent(message_lower)
    ):
        lookup_response = Sprint14.handle_appointment_lookup(message_lower)
        conversation_history.append(
            {"role": "user", "content": user_message}
        )
        conversation_history.append(
            {"role": "assistant", "content": lookup_response}
        )
        return jsonify({"response": lookup_response})

    # ── Fasting-lab inquiry ("Are my labs fasting labs?") ──
    # Whether an upcoming appointment's labs must be fasting depends on
    # the appointment type (fasting-lab RULES 1/2/3 described above).
    # Reuses the existing appointment-type sources: the active
    # wellness/refill flow (Sprint14), the routine follow-up flow
    # flags, the persisted generic appointment's reason, and the stated
    # scheduling reason from history. If NO appointment type can be
    # determined the message falls through unchanged to the existing
    # behavior, and no fasting-lab state is ever stored.
    if (
            pre_chart_complete and not Sprint13.phf_flow_active
            and not is_medical_professional_caller
            and detect_fasting_lab_inquiry(message_lower)
    ):
        lookup_first = patient_first_name or caller_first_name
        lookup_last = patient_last_name or caller_last_name
        fasting_reason = None
        if Sprint14.wellness_flow_active and Sprint14.refill_medication_name:
            fasting_reason = f"{Sprint14.refill_medication_name} refill"
        elif Sprint14.wellness_flow_active:
            fasting_reason = {
                "wellness": "annual wellness visit",
                "maw": "Medicare annual wellness visit",
                "cha": "comprehensive health assessment",
                "couple": "wellness visit",
                "paperwork": "paperwork visit",
            }.get(Sprint14.wellness_requested_visit_type, "wellness visit")
        elif individual_six_month_followup_active:
            fasting_reason = "6-month follow-up"
        elif individual_three_month_followup_active:
            fasting_reason = "3-month follow-up"
        elif couple_followup_flow_active:
            fasting_reason = "follow-up"
        else:
            generic_record = get_stored_generic_appointment_record(
                lookup_first, lookup_last
            )
            if generic_record and generic_record.get("reason"):
                fasting_reason = generic_record["reason"]
        if not fasting_reason:
            fasting_reason = _derive_generic_appointment_reason()
        if fasting_reason:
            fasting_response = generate_fasting_lab_answer(fasting_reason)
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": fasting_response}
            )
            return jsonify({"response": fasting_response})

    # ── Sprint 14: Wellness / MAW / CHA / Refill / Lab-Order Workflow ──
    # Activated only once pre-chart is complete and no PHF conversation
    # is already in progress - mirrors the Sprint13 activation guard
    # directly above. Kept fully isolated: this block never reads or
    # writes any Sprint13.phf_* state, and runs (and can short-circuit
    # via early return) before the generic appointment inquiry and
    # generic reschedule/cancel blocks below, so an active wellness
    # conversation can never be hijacked by those unrelated triggers.
    if (
            pre_chart_complete and not Sprint13.phf_flow_active
            and not Sprint14.wellness_flow_active
    ):
        if Sprint14.wellness_intent_detected:
            Sprint14.wellness_flow_active = True
            Sprint14.wellness_requested_visit_type = Sprint14.wellness_intent_detected
            Sprint14.wellness_intent_detected = None
        elif Sprint14.refill_intent_detected:
            Sprint14.wellness_flow_active = True
            Sprint14.refill_escalation_active = True
            Sprint14.refill_stage = "ask_medication"
            Sprint14.refill_intent_detected = False
        elif Sprint14.lab_order_intent_detected:
            Sprint14.wellness_flow_active = True
            Sprint14.lab_order_ehr_guidance_active = True
            Sprint14.lab_order_intent_detected = False

    if Sprint14.wellness_flow_active:
        wellness_response = Sprint14.handle_wellness_flow(
            user_message, message_lower
        )
        if wellness_response is not None:
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": wellness_response}
            )
            return jsonify({"response": wellness_response})

    # ─────────────────────────────────────────
    # Generic (non-PHF) appointment inquiry
    # ─────────────────────────────────────────
    # Independent of PHF's own inquiry mechanism above - checks the
    # separate generic_appointment_records store for ordinary
    # (non-post-hospital-follow-up) appointments. Mirrors PHF's inquiry
    # pattern: a deterministic direct return, bypassing the AI, for
    # reliability. Gated on pre_chart_complete (need patient identity
    # to look up the store) and not Sprint13.phf_flow_active (don't
    # interrupt an active PHF conversation).
    if (
            pre_chart_complete and not Sprint13.phf_flow_active
            and not Sprint14.wellness_flow_active
            and not is_medical_professional_caller
            and detect_generic_appointment_inquiry_intent(message_lower)
    ):
        lookup_first = patient_first_name or caller_first_name
        lookup_last = patient_last_name or caller_last_name
        generic_record = get_stored_generic_appointment_record(lookup_first, lookup_last)
        if generic_record and generic_record.get("appointment_day"):
            provider_suffix = (
                f" with {generic_record['provider']}"
                if generic_record.get("provider") else ""
            )
            generic_response = (
                f"I see that your appointment is scheduled for "
                f"{generic_record['appointment_day']}{provider_suffix}."
            )
        else:
            generic_response = (
                "I've checked your schedule, and I don't see any "
                "upcoming appointments on file for you right now."
            )
        conversation_history.append(
            {"role": "user", "content": user_message}
        )
        conversation_history.append(
            {"role": "assistant", "content": generic_response}
        )
        return jsonify({"response": generic_response})

    # ─────────────────────────────────────────
    # Generic (non-PHF) appointment reschedule / cancel
    # ─────────────────────────────────────────
    # RT13/RT14 root cause: RESCHEDULE_ACUTE_VISIT_TRIGGERS and
    # CANCEL_ACUTE_VISIT_TRIGGERS (e.g. "reschedule my appointment",
    # "cancel my appointment") are generic phrasing, not acute-specific,
    # but the acute-visit-management block further below has no gate on
    # whether the patient actually has an acute visit - it unconditionally
    # fabricates one via generate_existing_acute_appointment() the moment
    # any of those phrases appear. A patient whose only appointment on
    # file is a routine FUTURE one booked through generic scheduling
    # would therefore have that phrase hijacked into the unrelated acute
    # workflow, and their real generic_appointment_records entry would
    # never be touched.
    #
    # This block runs first and is gated on an existing_generic_record
    # actually being on file for this patient - mirroring the same
    # gating pattern Sprint13 already uses for its own PHF reschedule
    # detection (detect_generic_reschedule_intent() + a confirmed
    # stored record, before generic phrasing is treated as PHF-specific).
    # If no generic record exists, this block does nothing and the
    # message falls through unchanged to the existing acute-visit logic
    # - zero behavior change for every patient who doesn't have a
    # generic appointment on file, i.e. zero regression risk to the
    # already-passing acute-visit reschedule/cancel/query scenarios.
    if (
            pre_chart_complete and not Sprint13.phf_flow_active
            and not Sprint14.wellness_flow_active
            and not is_medical_professional_caller
    ):
        lookup_first = patient_first_name or caller_first_name
        lookup_last = patient_last_name or caller_last_name
        existing_generic_record = get_stored_generic_appointment_record(
            lookup_first, lookup_last
        )

        # Dual NEW-PATIENT household reschedule continuation (the caller
        # has never been seen at the office, so the spouse has never had
        # a chance to add them to a HIPAA form - collect the spouse's
        # name/DOB/original appointment first, reschedule the spouse,
        # then the caller). Must run ahead of late-arrival/other
        # handlers because the caller's slot-pick replies won't contain
        # a reschedule trigger phrase.
        if generic_newpatient_household_reschedule_pending:
            household_response = _handle_household_reschedule_continuation(
                user_message, existing_generic_record
            )
            if household_response is not None:
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": household_response}
                )
                return jsonify({"response": household_response})

        if generic_newpatient_household_cancel_pending:
            household_cancel_response = _handle_household_cancel_continuation(
                user_message
            )
            if household_cancel_response is not None:
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": household_cancel_response}
                )
                return jsonify({"response": household_cancel_response})

        # Patient-running-late workflow: the patient calls to say they
        # will be late for their appointment. Steve asks how many
        # minutes late; under 15 minutes he informs the MA, 15 or more
        # means the slot cannot be held and he offers to reschedule
        # during the call. Deterministic Python state machine - never
        # delegated to the LLM - running ahead of all other
        # reschedule/cancel handling so a late-arrival turn is never
        # misread as a generic reschedule/cancel request.
        late_arrival_response = handle_late_arrival_flow(
            user_message, message_lower
        )
        if late_arrival_response is not None:
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": late_arrival_response}
            )
            return jsonify({"response": late_arrival_response})

        # Virtual-visit wait / late-PCP workflow: the patient reports
        # waiting for the PCP to join the virtual visit (or shows any
        # sign of annoyance with the wait). Steve explains the provider
        # is running behind, offers to reschedule or wait a little
        # longer, presents the provider's availability on a reschedule,
        # and thanks the patient if they choose to wait. Deterministic
        # Python state machine - never delegated to the LLM - and
        # handled before the generic reschedule/cancel triggers below
        # so a reschedule reply during this flow is never misread as a
        # generic reschedule request.
        if virtual_wait_choice_pending:
            if any(p in message_lower for p in VIRTUAL_WAIT_RESCHEDULE_PHRASES):
                if virtual_wait_reschedule_schedule is None:
                    virtual_wait_schedule = generate_weekly_availability(
                        force_has_availability=True
                    )
                    virtual_wait_avail_lines = [
                        f"{day}: {', '.join(slots)}"
                        for day, slots in virtual_wait_schedule.items() if slots
                    ]
                    virtual_wait_reschedule_schedule = (
                        "; ".join(virtual_wait_avail_lines)
                        if virtual_wait_avail_lines
                        else "no availability this week"
                    )
                virtual_wait_choice_pending = False
                virtual_wait_reschedule_pending = True
                virtual_wait_reschedule_response = (
                    f"Of course. {virtual_wait_reschedule_provider or 'your provider'} "
                    f"has the following availability for a rescheduled visit: "
                    f"{virtual_wait_reschedule_schedule}. Which day and time works "
                    f"best for you?"
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": virtual_wait_reschedule_response}
                )
                return jsonify({"response": virtual_wait_reschedule_response})
            virtual_wait_choice_pending = False
            virtual_wait_active = False
            virtual_wait_reschedule_provider = None
            wait_patient_response = (
                f"Thank you for your patience. "
                f"{virtual_wait_reschedule_provider or 'Your provider'} will be with "
                f"you shortly."
            )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": wait_patient_response}
            )
            return jsonify({"response": wait_patient_response})

        if virtual_wait_reschedule_pending:
            virtual_wait_reschedule_pending = False
            virtual_wait_active = False
            chosen_virtual_wait_time = (
                _extract_day_time_from_reply(user_message)
                or user_message.strip()
            )
            virtual_wait_reschedule_schedule = None
            virtual_wait_reschedule_provider = None
            virtual_wait_reschedule_confirm = (
                f"Perfect, your virtual visit has been rescheduled for "
                f"{chosen_virtual_wait_time}. Is there anything else I can help "
                f"you with today?"
            )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": virtual_wait_reschedule_confirm}
            )
            return jsonify({"response": virtual_wait_reschedule_confirm})

        if detect_virtual_wait_annoyance(message_lower):
            if not virtual_wait_active:
                virtual_wait_active = True
                virtual_wait_choice_pending = True
                virtual_wait_reschedule_provider = (
                    get_patient_pcp_from_history()
                    or next(
                        (
                            full_name
                            for last_name, full_name in PROVIDER_LAST_NAMES.items()
                            if last_name in message_lower
                        ),
                        None,
                    )
                )
                wait_support_response = (
                    f"I'm sorry about the wait. "
                    f"{virtual_wait_reschedule_provider or 'Your provider'} is running a little bit "
                    f"behind today and will be with you shortly. Would you like to "
                    f"wait a little longer, or would you prefer to reschedule "
                    f"your virtual visit?"
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": wait_support_response}
                )
                return jsonify({"response": wait_support_response})

        # Virtual-visit paywall bypass: the patient does not want to
        # provide credit card / payment information through the virtual
        # visit portal (for any reason) or asks for a way around the
        # payment screen. Steve bypasses the virtual visit paywall with
        # a fixed business script. Deterministic - never delegated to
        # the LLM - mirroring the virtual-wait workflow above and
        # leaving all virtual-visit scheduling behavior untouched.
        if detect_virtual_visit_paywall_bypass(message_lower):
            virtual_visit_paywall_bypass_response = (
                "I can help with that. I have bypassed the payment "
                "screen for your virtual visit. Please refresh your "
                "virtual visit link. After refreshing the page, you "
                "should be able to continue completing your virtual "
                "visit check-in without providing payment "
                "information. Please let us know if you continue "
                "having any issues."
            )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant",
                 "content": virtual_visit_paywall_bypass_response}
            )
            return jsonify({"response": virtual_visit_paywall_bypass_response})

        # Provider-cancelled appointment: patient wants to know WHY it
        # was cancelled. Deterministic - never delegated to the LLM.
        # Runs before the reschedule/cancel trigger blocks below so a
        # message like "why did you cancel my appointment" is answered
        # as a question instead of being treated as a new cancel
        # request that erases the record. Answers regardless of whether
        # a record is still on file (the appointment may already have
        # been removed by the provider's office).
        if detect_cancellation_why_intent(message_lower):
            cancelled_day = (
                existing_generic_record.get("appointment_day")
                if existing_generic_record else None
            )
            why_provider = (
                (existing_generic_record.get("provider")
                 if existing_generic_record else None)
                or get_patient_pcp_from_history()
                or "your provider"
            )
            why_day_phrase = (
                f" on {cancelled_day}" if cancelled_day else ""
            )
            why_reason = _pick_cancellation_reason()
            if "misconduct" in why_reason:
                generic_response = (
                    f"{why_provider} dismissed you for misconduct. You "
                    f"are no longer a patient of {why_provider}. Is "
                    f"there anything else that I can help you with?"
                )
            else:
                generic_response = (
                    f"I apologize for the inconvenience. Your appointment"
                    f"{why_day_phrase} was cancelled by {why_provider} "
                    f"because {why_reason}. Would you like to reschedule "
                    f"it?"
                )
                generic_cancelled_reschedule_pending = True
                generic_cancelled_reschedule_offered = False
                generic_cancelled_reschedule_provider = why_provider
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": generic_response}
            )
            return jsonify({"response": generic_response})

        # Continue a reschedule offer made by the cancellation-why block
        # above: while pending, the yes-turn presents availability and
        # the following turn captures the patient's picked slot. Kept
        # deterministic and independent of generic_reschedule_pending
        # (which requires a record still on file) since a cancelled
        # appointment may no longer be on file at all.
        if generic_cancelled_reschedule_pending:
            if not generic_cancelled_reschedule_offered:
                if patient_wants_to_decline(message_lower):
                    generic_cancelled_reschedule_pending = False
                    generic_response = (
                        "No problem. Is there anything else I can help "
                        "you with today?"
                    )
                elif (
                        patient_wants_to_proceed(message_lower)
                        or "reschedule" in message_lower
                ):
                    schedule = generate_weekly_availability()
                    avail_lines = [
                        f"{day}: {', '.join(slots)}"
                        for day, slots in schedule.items() if slots
                    ]
                    avail_text = (
                        "; ".join(avail_lines) if avail_lines
                        else "no availability this week"
                    )
                    generic_cancelled_reschedule_offered = True
                    generic_response = (
                        "Great. Here is what we have available - "
                        f"{avail_text}. Which day and time works best "
                        "for you?"
                    )
                else:
                    generic_response = "Would you like to reschedule it?"
            else:
                extracted_new_time = _extract_calendar_date_slot_from_text(
                    user_message
                )
                if not extracted_new_time:
                    extracted_new_time = _extract_day_time_from_reply(
                        user_message
                    )
                chosen_new_time = extracted_new_time or user_message.strip()
                store_generic_appointment_record(
                    lookup_first, lookup_last, chosen_new_time,
                    provider=generic_cancelled_reschedule_provider,
                    reason=(
                        existing_generic_record.get("reason")
                        if existing_generic_record else None
                    ) or _derive_generic_appointment_reason(),
                )
                generic_cancelled_reschedule_pending = False
                generic_cancelled_reschedule_offered = False
                generic_cancelled_reschedule_provider = None
                generic_response = (
                    f"Perfect, I've scheduled your appointment for "
                    f"{chosen_new_time}. Is there anything else I can "
                    f"help you with today?"
                )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": generic_response}
            )
            return jsonify({"response": generic_response})

        # Resume a reschedule already in progress: the previous turn
        # presented availability and asked for a new day/time, this
        # turn is the patient's selection. Checked before re-detecting
        # trigger phrases since the patient's reply here (e.g. "Tuesday
        # at 2pm") won't itself contain "reschedule my appointment".
        if generic_reschedule_pending and existing_generic_record:
            # Patient picks a new time from the calendar-dated offer
            # (e.g. "October 7 at 11 AM") or, for plain weekday
            # records, the weekday offer (e.g. "Tuesday at 2PM"). The
            # same reply can also ask to move a spouse's appointment
            # ("...please reschedule my wife for 1:30PM that same
            # day") - split that off and handle both records.
            spouse_prompt = _SPOUSE_RESCHEDULE_PATTERN.search(user_message)
            if spouse_prompt:
                primary_text = user_message[:spouse_prompt.start()]
                second_text = user_message[spouse_prompt.start():]
            else:
                primary_text = user_message
                second_text = ""
            primary_date = _parse_calendar_date_from_text(primary_text)
            extracted_new_time = _extract_calendar_date_slot_from_text(
                primary_text
            )
            if not extracted_new_time:
                extracted_new_time = _extract_day_time_from_reply(primary_text)
            chosen_new_time = extracted_new_time or user_message.strip()
            store_generic_appointment_record(
                lookup_first, lookup_last, chosen_new_time,
                provider=existing_generic_record.get("provider"),
                reason=(
                    existing_generic_record.get("reason")
                    or _derive_generic_appointment_reason()
                ),
            )
            generic_reschedule_pending = False
            generic_response = (
                f"Perfect, I've moved your appointment to "
                f"{chosen_new_time}."
            )
            if spouse_prompt:
                spouse_slot = _extract_calendar_date_slot_from_text(
                    second_text
                )
                if not spouse_slot:
                    spouse_time = _find_time_in_text(second_text)
                    if spouse_time and primary_date is not None:
                        spouse_slot = _calendar_slot_from_date(
                            primary_date, spouse_time)
                    elif spouse_time:
                        spouse_slot = _extract_day_time_from_reply(
                            second_text)
                if spouse_prompt and spouse_slot:
                    # Bug fix: the office cannot pull up or change a
                    # spouse's record without first collecting their
                    # first and last name and date of birth. Previously
                    # this guessed the spouse's identity from a stored
                    # same-surname record and rescheduled her with no
                    # verification - Mark never supplied Helen's name
                    # or DOB. Hold the spouse's slot and ask for that
                    # identity instead; the caller's own slot above is
                    # already stored.
                    generic_spouse_reschedule_pending = True
                    generic_spouse_reschedule_slot = spouse_slot
                    generic_spouse_reschedule_relation = (
                        spouse_prompt.group(1) or spouse_prompt.group(2)
                    ).lower()
                    spouse_possessive = _household_pronoun_possessive(
                        generic_spouse_reschedule_relation
                    )
                    generic_response = (
                        f"Perfect, I've moved your appointment to "
                        f"{chosen_new_time}. To also reschedule "
                        f"your {generic_spouse_reschedule_relation}'s "
                        f"appointment, I'll need {spouse_possessive} "
                        f"first and last name and date of birth so I "
                        f"can pull up {spouse_possessive} chart. What "
                        f"is {spouse_possessive} first and last name "
                        f"and date of birth?"
                    )
            generic_response += (
                " Is there anything else I can help you with today?"
            )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": generic_response}
            )
            return jsonify({"response": generic_response})

        if generic_spouse_reschedule_pending:
            # The previous turn stored the caller's own rescheduled
            # slot and asked for the spouse's first/last name and DOB
            # before touching the spouse's record. Once both arrive,
            # apply the held spouse slot and confirm.
            spouse_first, spouse_last = aggressive_name_extraction(
                user_message
            )
            if not spouse_first or not spouse_last:
                parts = re.findall(r"\b([A-Z][a-z]+)\b", user_message)
                parts = [
                    p for p in parts
                    if p.lower() not in (
                        "her", "his", "their", "name", "is", "my",
                        "wife", "husband", "spouse", "and", "date",
                        "birth", "of",
                    )
                ]
                if len(parts) >= 2:
                    spouse_first, spouse_last = parts[0], parts[1]
            spouse_dob = extract_dob_from_message(user_message)
            spouse_possessive = _household_pronoun_possessive(
                generic_spouse_reschedule_relation or "spouse"
            )
            if not (spouse_first and spouse_last):
                generic_response = (
                    f"What is {spouse_possessive} first and last name?"
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": generic_response}
                )
                return jsonify({"response": generic_response})
            if not spouse_dob:
                generic_response = (
                    f"What is {spouse_possessive} date of birth?"
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": generic_response}
                )
                return jsonify({"response": generic_response})
            spouse_slot = generic_spouse_reschedule_slot
            spouse_record = get_stored_generic_appointment_record(
                spouse_first, spouse_last
            )
            provider = spouse_record.get("provider") if spouse_record else None
            if provider is None and existing_generic_record:
                provider = existing_generic_record.get("provider")
            reason = (
                (spouse_record.get("reason") if spouse_record else None)
                or (
                    existing_generic_record.get("reason")
                    if existing_generic_record else None
                )
                or _derive_generic_appointment_reason()
            )
            store_generic_appointment_record(
                spouse_first, spouse_last, spouse_slot,
                provider=provider, reason=reason,
            )
            generic_spouse_reschedule_pending = False
            generic_spouse_reschedule_slot = None
            generic_spouse_reschedule_relation = None
            generic_response = (
                f"Thank you. I've moved "
                f"{spouse_first.capitalize()} "
                f"{spouse_last.capitalize()}'s appointment to "
                f"{spouse_slot}. Is there anything else I can help "
                f"you with today?"
            )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": generic_response}
            )
            return jsonify({"response": generic_response})

        if existing_generic_record and any(
                trigger in message_lower for trigger in RESCHEDULE_ACUTE_VISIT_TRIGGERS
        ):
            # Dual NEW-PATIENT household reschedule (e.g. "Actually my
            # spouse and myself are scheduled to establish care with Dr.
            # Mitchell and we need to reschedule those appointments").
            # Because the caller has never been seen at the office, the
            # spouse has never had a chance to add the caller to a HIPAA
            # form - the office cannot pull up or change the spouse's
            # appointment on the caller's authority alone. Collect the
            # spouse's first/last name, date of birth, and ORIGINALLY
            # scheduled appointment day/time before any slot is offered.
            # Must run before the single-appointment availability offer
            # below, or the household request is silently reduced to
            # just the caller.
            household_match = _HOUSEHOLD_RELATION_PATTERN.search(message_lower)
            caller_new_patient = (
                (existing_generic_record.get("reason") or "").strip().lower()
                == "new patient appointment"
            )
            if household_match and caller_new_patient:
                relation = (
                    household_match.group(1) or household_match.group(2)
                ).lower()
                generic_newpatient_household_reschedule_pending = True
                generic_household_reschedule_stage = "spouse_info"
                generic_household_spouse_relation = relation
                possessive = _household_reschedule_possessive(relation)
                generic_response = (
                    f"I can take care of that for you. May I have your "
                    f"{relation}'s first and last name, date of birth, and "
                    f"date and time of {possessive} appointment?"
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": generic_response}
                )
                return jsonify({"response": generic_response})
            schedule = generate_weekly_availability()
            # Bug fix: the routine six-month follow-up flow is the only
            # flow that stores a concrete calendar date in the record
            # (e.g. "September 30, 2026 @ 9:00 AM"). Each such
            # appointment is always booked on/after the six-month due
            # date, so a reschedule must never move it earlier - but the
            # generic offer below used generate_weekly_availability()
            # (always the upcoming week), which on the days right
            # before the due date offered weekday slots that resolve to
            # dates BEFORE the follow-up is due. For calendar-dated
            # records, generate the offered week starting at the first
            # Monday on/after the existing appointment and label every
            # slot with its exact date, so no offered slot can fall
            # before the follow-up is due.
            existing_appt_date = _parse_calendar_date_from_text(
                existing_generic_record.get("appointment_day") or ""
            )
            if existing_appt_date is not None:
                week_start = _next_monday_on_or_after(existing_appt_date)
                avail_lines = []
                for day_idx, day in enumerate(DAYS_OF_WEEK):
                    day_slots = schedule.get(day, [])
                    if day_slots:
                        slot_date = week_start + timedelta(days=day_idx)
                        avail_lines.append(
                            f"{day}, {slot_date.strftime('%B %d, %Y')}: "
                            f"{', '.join(day_slots)}"
                        )
                avail_text = (
                    "; ".join(avail_lines) if avail_lines
                    else "no availability"
                )
            else:
                avail_lines = [
                    f"{day}: {', '.join(slots)}"
                    for day, slots in schedule.items() if slots
                ]
                avail_text = (
                    "; ".join(avail_lines) if avail_lines
                    else "no availability this week"
                )
            generic_reschedule_pending = True
            generic_response = (
                f"I can help you reschedule your appointment currently "
                f"set for {existing_generic_record['appointment_day']}. "
                f"Here is what we have available - {avail_text}. Which "
                f"day and time works best for you?"
            )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": generic_response}
            )
            return jsonify({"response": generic_response})

        if existing_generic_record and any(
                trigger in message_lower for trigger in (
                    CANCEL_ACUTE_VISIT_TRIGGERS + HOUSEHOLD_CANCEL_TRIGGERS
                )
        ):
            # Dual NEW-PATIENT household cancellation (e.g. "Actually my
            # spouse and myself are scheduled to establish care with Dr.
            # Mitchell and we need to cancel those appointments"). Same
            # HIPAA rationale as the household reschedule branch above -
            # the office cannot cancel the spouse's separate appointment
            # on the caller's authority alone, so collect the spouse's
            # name/DOB/original slot first, then remove both records.
            # Must run before the single-appointment generic cancel
            # below, or only the caller's own record is cancelled.
            household_match = _HOUSEHOLD_RELATION_PATTERN.search(message_lower)
            caller_new_patient = (
                (existing_generic_record.get("reason") or "").strip().lower()
                == "new patient appointment"
            )
            if household_match and caller_new_patient:
                relation = (
                    household_match.group(1) or household_match.group(2)
                ).lower()
                generic_newpatient_household_cancel_pending = True
                generic_household_cancel_stage = "spouse_info"
                generic_household_cancel_spouse_relation = relation
                generic_response = (
                    f"I can take care of that for you. Can I get your "
                    f"{relation}'s first and last name, date of birth, and "
                    f"date and time of their appointment?"
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": generic_response}
                )
                return jsonify({"response": generic_response})

        if generic_joint_cancel_pending:
            generic_joint_cancel_pending = False
            if (
                    caller_first_name and caller_last_name
                    and patient_first_name and patient_last_name
                    and (caller_first_name, caller_last_name)
                    != (patient_first_name, patient_last_name)
            ):
                caller_record = get_stored_generic_appointment_record(
                    caller_first_name, caller_last_name
                )
                spouse_record = get_stored_generic_appointment_record(
                    patient_first_name, patient_last_name
                )
                if caller_record and spouse_record:
                    caller_time = caller_record.get("appointment_day")
                    spouse_time = spouse_record.get("appointment_day")
                    cancel_generic_appointment_record(
                        caller_first_name, caller_last_name
                    )
                    cancel_generic_appointment_record(
                        patient_first_name, patient_last_name
                    )
                    generic_response = (
                        f"I've cancelled your appointment on {caller_time} "
                        f"and {patient_first_name}'s appointment on {spouse_time}. "
                        "Is there anything else I can help you with today?"
                    )
                    conversation_history.append(
                        {"role": "user", "content": user_message}
                    )
                    conversation_history.append(
                        {"role": "assistant", "content": generic_response}
                    )
                    return jsonify({"response": generic_response})

        if any(
                trigger in message_lower for trigger in CANCEL_ACUTE_VISIT_TRIGGERS
        ):
            # Bug fix: lookup_first/lookup_last default to
            # patient_first_name ahead of caller_first_name (see the
            # RT13/RT14 comment above), and patient_first_name can get
            # set to a SPOUSE's name mentioned earlier in this very
            # message (e.g. "Sam Smith ... who is my spouse"). That
            # silently resolved "cancel our appointments" to only the
            # spouse's own stored record, mislabeled in the response
            # as "your appointment." When the message explicitly
            # states ownership for BOTH ("my appointment is X" and
            # "his appointment is Y"), cancel each under its correctly
            # attributed owner instead of defaulting to whichever name
            # patient_first_name happened to hold.
            mentions_my_appointment = bool(
                _MY_APPOINTMENT_OWNERSHIP_PATTERN.search(message_lower)
            )
            spouse_appt_match = _SPOUSE_APPOINTMENT_OWNERSHIP_PATTERN.search(
                message_lower
            )
            if (
                    mentions_my_appointment and spouse_appt_match
                    and caller_first_name and caller_last_name
                    and patient_first_name and patient_last_name
                    and (caller_first_name, caller_last_name)
                    != (patient_first_name, patient_last_name)
            ):
                caller_record = get_stored_generic_appointment_record(
                    caller_first_name, caller_last_name
                )
                spouse_record = get_stored_generic_appointment_record(
                    patient_first_name, patient_last_name
                )
                caller_time = (
                    caller_record.get("appointment_day") if caller_record else None
                )
                spouse_time = (
                    spouse_record.get("appointment_day") if spouse_record else None
                )
                if not caller_time or not spouse_time:
                    # Bug fix: no separate store_generic_appointment_
                    # record() call happened for these appointments in
                    # this session (e.g. the caller is cancelling
                    # purely from what they just said), so fall back
                    # to the day/time stated in THIS message rather
                    # than asking the caller to repeat information
                    # they already gave.
                    my_part = message[:spouse_appt_match.start()]
                    spouse_part = message[spouse_appt_match.start():]
                    my_day_match = _DAY_NAME_PATTERN.search(my_part)
                    my_day_name = (
                        my_day_match.group(1).capitalize() if my_day_match else None
                    )
                    if not caller_time:
                        caller_time = _extract_slot_from_text(my_part)
                    if not spouse_time:
                        spouse_time = _extract_slot_from_text(
                            spouse_part, fallback_day=my_day_name
                        )
                cancelled_parts = []
                if caller_time:
                    cancel_generic_appointment_record(
                        caller_first_name, caller_last_name
                    )
                    cancelled_parts.append(f"your appointment on {caller_time}")
                if spouse_time:
                    cancel_generic_appointment_record(
                        patient_first_name, patient_last_name
                    )
                    cancelled_parts.append(
                        f"{patient_first_name}'s appointment on {spouse_time}"
                    )
                if cancelled_parts:
                    generic_response = (
                        f"I've cancelled {' and '.join(cancelled_parts)}. "
                        f"Is there anything else I can help you with "
                        f"today?"
                    )
                    conversation_history.append(
                        {"role": "user", "content": user_message}
                    )
                    conversation_history.append(
                        {"role": "assistant", "content": generic_response}
                    )
                    return jsonify({"response": generic_response})
            if existing_generic_record:
                cancelled_day = existing_generic_record.get("appointment_day")
                cancel_generic_appointment_record(lookup_first, lookup_last)
                generic_response = (
                    f"Your appointment on {cancelled_day} has been "
                    f"cancelled. Is there anything else I can help you with "
                    f"today?"
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": generic_response}
                )
                return jsonify({"response": generic_response})

    # ─────────────────────────────────────────
    # Controlled-substance appointment bridge follow-up (Scenario 11/12)
    # ─────────────────────────────────────────
    # Deterministic Python interceptor - intentionally NOT delegated to
    # the LLM. The bridge-need decision is a plain comparison (days of
    # medication remaining vs. days until the already-booked
    # appointment), not something requiring model judgment, and this
    # same appointment path has already needed several prompt-
    # compliance corrections this session. Placed ahead of the general
    # conversation-closing block below so a bare number in the
    # patient's reply is never mistaken for closing intent.
    if controlled_substance_bridge_awaiting_dosage:
        controlled_substance_bridge_dosage = user_message.strip()
        controlled_substance_bridge_awaiting_dosage = False
        controlled_substance_bridge_awaiting_pharmacy = True
        pharmacy_response = "What pharmacy do you use?"
        conversation_history.append({"role": "user", "content": user_message})
        conversation_history.append({"role": "assistant", "content": pharmacy_response})
        return jsonify({"response": pharmacy_response})

    if controlled_substance_bridge_awaiting_pharmacy:
        controlled_substance_bridge_awaiting_pharmacy = False
        # Bug #3 fix: previously this step went straight to the final
        # bridge message with no callback number collected, even though
        # the bridge request is sent to the provider as a message - the
        # same kind of high-priority phone message this system already
        # collects a callback number for elsewhere. Ask for it before
        # creating the message rather than after.
        controlled_substance_bridge_awaiting_callback = True
        callback_response = "What is the best callback number to reach you at?"
        conversation_history.append({"role": "user", "content": user_message})
        conversation_history.append({"role": "assistant", "content": callback_response})
        return jsonify({"response": callback_response})

    if controlled_substance_bridge_awaiting_callback:
        controlled_substance_bridge_awaiting_callback = False
        controlled_substance_bridge_callback_number = user_message.strip()
        controlled_substance_bridge_dosage = None
        controlled_substance_bridge_callback_number = None
        bridge_response = (
            "I understand. I will send a bridge-request message to "
            "your provider asking them to authorize enough medication "
            "to hold you over until your appointment. Please allow up "
            "to 24 business hours for this to be processed. Provider "
            "approval is required, and bridge medication is not "
            "guaranteed. Is there anything else I can help you with "
            "today?"
        )
        conversation_history.append({"role": "user", "content": user_message})
        conversation_history.append({"role": "assistant", "content": bridge_response})
        return jsonify({"response": bridge_response})

    if controlled_substance_bridge_awaiting_days:
        days_remaining = parse_days_remaining_from_reply(message_lower)
        if days_remaining is not None:
            controlled_substance_bridge_awaiting_days = False
            if controlled_substance_bridge_confirmed_insufficient:
                # Patient already explicitly declared insufficiency on
                # the previous turn (see the decline branch below) -
                # this day-count is purely informational for the
                # bridge-request message, not a fresh sufficiency
                # decision. Do NOT re-run the day-math comparison,
                # which could otherwise contradict what the patient
                # already told us directly.
                controlled_substance_bridge_confirmed_insufficient = False
                controlled_substance_appt_day_name = None
                controlled_substance_appt_date = None
                controlled_substance_bridge_awaiting_dosage = True
                bridge_response = "What dosage do you currently take?"
            else:
                days_until_appt = None
                if controlled_substance_appt_date:
                    # Bug fix: the appointment's actual confirmed date
                    # (captured at booking time) gives an exact day
                    # count. Preferred over the weekday-name guess
                    # below, which assumes the appointment is within
                    # the next 6 days and silently produces a wrong
                    # (too-small) count for anything booked further
                    # out - e.g. a 25-day-out appointment was
                    # previously computed as 3 days away, which
                    # incorrectly skipped the bridge request entirely.
                    days_until_appt = (
                            controlled_substance_appt_date - datetime.now().date()
                    ).days
                elif controlled_substance_appt_day_name:
                    weekday_index = {
                        "monday": 0, "tuesday": 1, "wednesday": 2,
                        "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
                    }.get(controlled_substance_appt_day_name.lower())
                    if weekday_index is not None:
                        today_index = datetime.now().weekday()
                        days_until_appt = (weekday_index - today_index) % 7
                        if days_until_appt == 0:
                            days_until_appt = 7
                controlled_substance_appt_day_name = None
                controlled_substance_appt_date = None
                if days_until_appt is not None and days_remaining < days_until_appt:
                    # Bug fix: bridge is needed - collect Dosage, then
                    # Pharmacy, BEFORE creating the bridge message
                    # (this collection step was previously skipped
                    # entirely).
                    controlled_substance_bridge_awaiting_dosage = True
                    bridge_response = "What dosage do you currently take?"
                else:
                    # Bug #2 fix: no preamble statement - go straight
                    # to the closing question, since the patient
                    # already knows they have enough medication.
                    bridge_response = "Is there anything else I can help you with today?"
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": bridge_response}
            )
            return jsonify({"response": bridge_response})
        elif patient_declines_sufficient_medication(message_lower):
            # Scenario 11 regression fix: an explicit insufficiency
            # statement - INCLUDING a negated sufficiency phrase like
            # "I do NOT have enough medication to hold me over" - was
            # previously misclassified by
            # patient_confirms_sufficient_medication()'s naive
            # substring match on "have enough", silently inverting the
            # patient's answer and skipping the bridge workflow
            # entirely. Checked BEFORE the sufficiency-confirmation
            # branch below. The patient has already told us directly
            # that supply is insufficient - no day-math comparison is
            # needed to decide that; "days remaining" is now asked
            # purely as an informational collection step for the
            # bridge-request message (see
            # controlled_substance_bridge_confirmed_insufficient
            # above), matching the expected Scenario 11 sequence:
            # days remaining -> dosage -> pharmacy -> bridge message.
            controlled_substance_bridge_confirmed_insufficient = True
            clarify_response = (
                "How many days of medication do you currently have "
                "remaining?"
            )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": clarify_response}
            )
            return jsonify({"response": clarify_response})
        elif patient_confirms_sufficient_medication(message_lower):
            # Bug #1 fix: a direct sufficiency affirmation ("I have
            # enough alprazolam to hold me over") already fully answers
            # the yes/no question Steve just asked - re-asking for an
            # exact day count here contradicts what the patient just
            # said. No bridge, no further collection.
            # Bug #2 fix: no "No bridge needed." preamble - the patient
            # already knows they have enough medication; go straight
            # to the closing question.
            controlled_substance_bridge_awaiting_days = False
            controlled_substance_appt_day_name = None
            controlled_substance_appt_date = None
            bridge_response = "Is there anything else I can help you with today?"
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": bridge_response}
            )
            return jsonify({"response": bridge_response})
        else:
            clarify_response = (
                "How many days of medication do you currently have "
                "remaining?"
            )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": clarify_response}
            )
            return jsonify({"response": clarify_response})

    # ─────────────────────────────────────────
    # General conversation-closing detection
    # ─────────────────────────────────────────
    # RT13 conversation-closing defect: after Steve's own "anything else
    # I can help you with today?" question, closing intent was being
    # decided entirely by the AI - no Python detection exists for this
    # general moment (unlike Sprint13's PHF "complete" stage and the
    # new-patient flow's "complete" stage, which each have their own
    # closing-phrase lists, but neither covers this general closing
    # question, which is asked at the end of many different flows -
    # generic scheduling, lab result inquiry, referral lookup, the
    # generic reschedule/cancel block above, and more). The AI reliably
    # closed on a bare "No" but was observed returning an EMPTY reply to
    # "No. Thank you" - this is a model generation-reliability issue,
    # not a Python string-matching bug, since no Python code was
    # evaluating this particular reply at all before now. Moving this
    # decision into deterministic Python matching (see
    # is_conversation_closing_reply() above) removes that inconsistency
    # from the picture entirely for this specific, narrow moment.
    #
    # Scoped narrowly to avoid interfering with flows that already have
    # their own closing handling (PHF, new-patient), and matched against
    # the FULL normalized message (not a substring) so a reply that
    # merely starts with "no" but continues into a new request (e.g.
    # "No, actually I have another question") correctly falls through
    # unchanged instead of prematurely ending the call.

    if (
            pre_chart_complete and not Sprint13.phf_flow_active
            and not is_medical_professional_caller
            and not new_patient_flow_active
    ):
        last_assistant_offered_closing = False
        for turn in reversed(conversation_history):
            if turn.get("role") == "assistant":
                last_assistant_lower = turn.get("content", "").lower()
                if ("anything else" in last_assistant_lower and (
                        "?" in last_assistant_lower
                        or "i can help" in last_assistant_lower
                        or "can i help" in last_assistant_lower
                )):
                    last_assistant_offered_closing = True
                break

        if last_assistant_offered_closing and is_conversation_closing_reply(message_lower):
            closing_name = patient_first_name or caller_first_name
            patient_expressed_thanks = "thank" in message_lower
            if patient_expressed_thanks:
                closing_response = (
                    f"You're welcome, {closing_name}. Thank you for "
                    f"calling Sykes Creek Primary Care. Have a great day!"
                    if closing_name else
                    "You're welcome. Thank you for calling Sykes Creek "
                    "Primary Care. Have a great day!"
                )
            else:
                closing_response = (
                    f"Thank you for calling Sykes Creek Primary Care, "
                    f"{closing_name}. Have a great day!"
                    if closing_name else
                    "Thank you for calling Sykes Creek Primary Care. "
                    "Have a great day!"
                )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": closing_response}
            )
            return jsonify({"response": closing_response})

    # Detect the patient announcing themselves mid-call after a third
    # party caller has already completed pre-chart.
    # A self-announcement may use the patient's FULL name ("this is
    # Thelma Louis") OR, once we have just asked for the patient to come
    # on the line (verbal_consent_requested is True, i.e. the DOB
    # self-verification gate told them we are transferring to them), a
    # bare FIRST NAME ("hi, this is Thelma"). The full-name form is the
    # general path; the first-name-only form only applies right after we
    # requested the patient, so a run-on "this is Thelma" greeting from
    # the patient does not slip past the deterministic HIPAA gates — the
    # LLM must not be allowed to improvise a response here.
    if third_party_detected and patient_first_name and patient_last_name:
        patient_self_announce_pattern = re.search(
            r"(?:this is|i am|i'm|it's|it is)\s+" +
            re.escape(patient_first_name) + r"\s+" +
            re.escape(patient_last_name),
            message_lower, re.IGNORECASE
        )
        # Bug fix: the "we just asked for the patient" signal lives in
        # verbal_consent_requested, but that flag is only set when the
        # caller's reply happens to match a PATIENT_PRESENT_PHRASE entry
        # ("she is here", etc.). A short acknowledgment like "Yes she is"
        # slips through, so the patient's first-name-only introduction
        # ("hi, this is Thelma") was never recognized as the patient
        # taking the line — caller_is_patient stayed False, the
        # deterministic verification gates were bypassed, and the LLM
        # improvised a HIPAA consent request with NO identity
        # verification. Also treat the patient as on the line whenever
        # Steve has already asked to put them on the phone ("...please put
        # Thelma on the line?"), regardless of the presence-phrase match.
        if not patient_self_announce_pattern and patient_first_name:
            last_assistant_put_on_line = False
            for _turn in reversed(conversation_history):
                if _turn.get("role") == "assistant":
                    _last_text = _turn.get("content", "") or ""
                    last_assistant_put_on_line = bool(re.search(
                        r"\bput(?:ting)?\s+[^.!?\n]{0,25}" +
                        re.escape(patient_first_name) +
                        r"[^.!?\n]{0,25}\bon the line\b",
                        _last_text, re.IGNORECASE
                    ))
                    break
            if (
                (verbal_consent_requested or last_assistant_put_on_line)
                and re.search(
                    r"(?:this is|i am|i'm|it's|it is)\s+" +
                    re.escape(patient_first_name) +
                    r"\s*(?:,|\.|!|\?)?$",
                    message_lower, re.IGNORECASE
                )
            ):
                patient_self_announce_pattern = True
        if patient_self_announce_pattern:
            caller_is_patient = True

    # HIPAA Patient Self-Verification Gate.
    # When a third party call later has the PATIENT take over speaking
    # (caller_is_patient becomes True), the patient must verify their
    # OWN identity — their last name AND their date of birth — before any
    # consent step can proceed, even though the third party already gave
    # a date of birth earlier. These use SEPARATE flags
    # (patient_self_last_name_verified / patient_self_dob_verified) so
    # they do not interfere with the third party's own DOB-based HIPAA
    # check.
    if third_party_detected and caller_is_patient:
        if (
            patient_last_name
            and not patient_self_last_name_verified
            and re.search(
                r"\b" + re.escape(patient_last_name) + r"\b",
                message_lower, re.IGNORECASE
            )
        ):
            patient_self_last_name_verified = True
        if not patient_self_dob_verified and detect_dob_in_message(message_lower):
            patient_self_dob_verified = True
        patient_self_verified = (
            patient_self_dob_verified
            and (patient_self_last_name_verified or not patient_last_name)
        )
        if not patient_self_verified:
            if patient_self_dob_verified:
                verification_response = (
                    f"Thank you, {patient_first_name}. For verification "
                    "purposes, could you also provide me with your last "
                    "name?"
                )
            elif patient_self_last_name_verified or not patient_last_name:
                verification_response = (
                    f"Thank you, {patient_first_name}. For verification "
                    "purposes, could you also provide me with your date "
                    "of birth?"
                )
            else:
                verification_response = (
                    f"Hello {patient_first_name}. For verification "
                    "purposes, please provide me with your last name "
                    "and date of birth."
                )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": verification_response}
            )
            return jsonify({"response": verification_response})

    # HIPAA Third-Party Consent Gate (deterministic).
    # Once the patient has verified their OWN identity (last name and
    # date of birth) after being put on the line
    # (patient_self_last_name_verified / patient_self_dob_verified),
    # Steve must obtain the patient's verbal consent before sharing any
    # further information with the third party caller. This is handled
    # deterministically instead of by the LLM, so a bare "Hi [name]" or
    # greeting does not cause the model to improvise and skip the
    # required consent step. Ask first; on the next turn interpret a
    # clear yes, a clear no, or an ambiguous reply.
    if (
        third_party_detected and caller_is_patient
        and patient_self_dob_verified
        and (patient_self_last_name_verified or not patient_last_name)
    ):
        if not third_party_consent_obtained:
            if not third_party_consent_asked:
                third_party_consent_asked = True
                consent_gate_response = (
                    f"Thank you, {patient_first_name}. Do I have your "
                    f"consent to share information about your care with "
                    f"{caller_first_name}?"
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": consent_gate_response}
                )
                return jsonify({"response": consent_gate_response})
            if patient_wants_to_proceed(message_lower):
                third_party_consent_obtained = True
                consent_gate_response = (
                    f"Thank you, {patient_first_name}. I have your "
                    f"consent, so I can discuss your care with "
                    f"{caller_first_name}."
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": consent_gate_response}
                )
                return jsonify({"response": consent_gate_response})
            if patient_wants_to_decline(message_lower):
                third_party_consent_obtained = True
                consent_gate_response = (
                    f"I understand, {patient_first_name}. Without your "
                    f"consent, I am not able to share your health "
                    f"information with {caller_first_name}. Is there "
                    "anything else I can help you with today?"
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": consent_gate_response}
                )
                return jsonify({"response": consent_gate_response})
            consent_gate_response = (
                f"Hi {patient_first_name}. Just to make sure I have your "
                f"permission — will you allow me to share information "
                f"about your care with {caller_first_name}?"
            )
            conversation_history.append(
                {"role": "user", "content": user_message}
            )
            conversation_history.append(
                {"role": "assistant", "content": consent_gate_response}
            )
            return jsonify({"response": consent_gate_response})

    if caller_is_patient is False:
        third_party_detected = True

    if not is_medical_professional_caller:
        provider = detect_provider_in_message(message_lower)
        if provider:
            pcp_collected = True

    # --- Check response time and office hours stated ---
    response_time_count = 0
    office_hours_count = 0
    for msg in conversation_history:
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if check_response_time_stated(content):
                response_time_count += 1
            if check_office_hours_stated(content):
                office_hours_count += 1
    if response_time_count >= 1:
        response_time_stated = True
    if office_hours_count >= 1:
        office_hours_stated = True

    # --- Patient presence detection ---
    patient_is_present = any(
        phrase in message_lower for phrase in PATIENT_PRESENT_PHRASES
    )
    patient_is_not_present = any(
        phrase in message_lower for phrase in PATIENT_NOT_PRESENT_PHRASES
    )
    patient_will_call_back = any(
        phrase in message_lower for phrase in PATIENT_WILL_CALL_BACK_PHRASES
    )
    if patient_is_present and third_party_detected:
        verbal_consent_requested = True

    # --- Detect nurse/MA request ---
    nurse_ma_requested = detect_nurse_ma_request(message_lower)
    ma_name_requested, ma_provider = detect_ma_name_in_message(message_lower)
    # Set persistent MA request state so follow-up messages retain context
    if ma_name_requested and not ma_request_active:
        ma_request_active = True
        ma_request_name = ma_name_requested
        ma_request_provider = ma_provider
        # Reset MA availability so each new request gets fresh determination
        ma_availability_determined = False
        current_ma_availability = None
    elif nurse_ma_requested and not ma_request_active:
        ma_request_active = True
        # Reset MA availability so each new request gets fresh determination
        ma_availability_determined = False
        current_ma_availability = None
    # Detect if patient just gave reason for MA call
    # Only mark reason collected if:
    # 1. MA request is active
    # 2. Current message does NOT contain an MA name (not the initial request)
    # 3. Current message does NOT contain nurse/MA request phrases
    # 4. The reason-asked flag confirms we already asked the question
    # This prevents marking reason collected on the SAME message Diana is mentioned
    if ma_request_active and not ma_request_reason_collected:
        if not nurse_ma_requested and not ma_name_requested:
            if ma_request_reason_asked:
                ma_request_reason_collected = True
            else:
                ma_request_reason_asked = True

    # --- New patient workflow: block LLM from improvising scheduling ---
    if new_patient_flow_active:
        if any(
                phrase in message_lower for phrase in [
                    "no", "not right now", "no thank you", "not interested",
                    "don't think so", "do not think so",
                ]
        ):
            new_patient_flow_active = False
            farewell = "Have a great day."
            conversation_history.append({"role": "user", "content": user_message})
            conversation_history.append({"role": "assistant", "content": farewell})
            return jsonify({"response": farewell})

        fallback_response = (
            "I want to make sure I have your information correct. "
            "Could you please repeat that for me?"
        )
        conversation_history.append({"role": "user", "content": user_message})
        conversation_history.append(
            {"role": "assistant", "content": fallback_response}
        )
        return jsonify({"response": fallback_response})

    # --- Detect lab order pickup ---
    lab_order_pickup = detect_lab_order_pickup(message_lower)

    # --- Urgent symptoms ---
    truly_urgent = False
    if not is_medical_professional_caller:
        truly_urgent = is_truly_urgent(message_lower)

    if truly_urgent and not urgent_symptoms_active:
        urgent_symptoms_active = True
        urgent_can_wait_asked = False

    caller_cannot_wait = any(
        phrase in message_lower for phrase in CANNOT_WAIT_PHRASES
    )
    caller_can_wait = any(
        phrase in message_lower for phrase in CAN_WAIT_PHRASES
    )

    if (
            not urgent_symptoms_active
            and acute_same_day_established
            and (caller_cannot_wait or caller_can_wait)
    ):
        # Root-cause fix: a chronic-condition (or any non-911-emergency)
        # escalation that already established same-day urgency earlier in
        # the call can still reach a "can this wait, or do you need help
        # right now?" framing conversationally - but without ever tripping
        # is_truly_urgent()'s emergency-keyword gate, urgent_symptoms_active
        # never turned on, so the caller's direct cannot-wait/can-wait
        # answer had no deterministic pipeline to land in and the AI
        # improvised a response with no callback collection or
        # high-priority message. This was already asked and just
        # answered, so mark it answered rather than re-asking Phase 1.
        urgent_symptoms_active = True
        urgent_can_wait_asked = True

    caller_answered_wait_question = (
            not is_medical_professional_caller and
            urgent_symptoms_active and
            urgent_can_wait_asked and
            (caller_cannot_wait or caller_can_wait)
    )

    lab_work_detected = (
            any(trigger in message_lower for trigger in LAB_WORK_TRIGGERS)
            and not lab_order_pickup
    )

    # Nurse-visit request detection. Sticky once True for the rest of the
    # call, so the injected NURSE_VISIT context below keeps Steve on the
    # in-office / MA-scheduled path even on follow-up turns (e.g. when the
    # patient answers the contact-number question on a later message).
    if detect_nurse_visit(message_lower) and not is_medical_professional_caller:
        nurse_visit_active = True

    hipaa_gate_conditions_met = (
            third_party_detected and dob_collected and pcp_collected
            and not is_medical_professional_caller and not caller_is_patient
    )
    early_hipaa_status = get_hipaa_status() if hipaa_gate_conditions_met else None
    hipaa_cleared_for_disclosure = (
            caller_is_patient or early_hipaa_status == "ON_HIPAA"
    )
    lab_result_inquiry_detected = (
            lab_result_inquiry_active and
            (not third_party_detected or caller_is_patient or hipaa_cleared_for_disclosure)
    )

    referral_lookup_triggers = [
        "don't remember the name", "dont remember the name",
        "can't remember the name", "cannot remember the name",
        "don't remember who", "dont remember who",
        "can you look up", "look up the referral",
        "look up the specialist", "find the referral",
        "what was the name of the", "forgot the name",
        "referred me to a", "referred to a",
        "what was the specialist", "who was the specialist",
        "what doctor was i referred to", "who did they refer me to",
        "name of the specialist", "name of the doctor i was referred"
    ]
    referral_lookup_detected = any(
        trigger in message_lower for trigger in referral_lookup_triggers
    )
    if referral_lookup_detected and not referral_lookup_done:
        referral_lookup_done = True
        referral_lookup_result, referral_specialist_name, \
            referral_specialist_phone = generate_referral_lookup()
        # Return directly from Python like med pro flow
        # This prevents stale globals from affecting the response
        if referral_lookup_result and referral_specialist_name and referral_specialist_phone:
            referral_python_response = (
                f"One moment while I check your chart. (pause) "
                f"I was able to locate that referral in your chart. "
                f"You were referred to {referral_specialist_name}. "
                f"Their phone number is {referral_specialist_phone}. "
                f"Is there anything else I can help you with today?"
            )
        else:
            referral_python_response = (
                "One moment while I check your chart. (pause) "
                "I'm sorry but I was not able to locate that referral "
                "in your chart. I can put in a phone note regarding this. "
                "May I get a good callback number for you so someone from "
                "our office can follow up?"
            )
        conversation_history.append({"role": "user", "content": user_message})
        conversation_history.append({"role": "assistant", "content": referral_python_response})
        return jsonify({"response": referral_python_response})

    # Intercept callback number given after a NOT FOUND referral lookup
    # Prevents the AI from improvising a confused response referencing
    # stale specialist names or asking redundant follow-up questions
    phone_number_pattern = re.search(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b', user_message)
    if (referral_lookup_done and referral_lookup_result is False and
            phone_number_pattern and not is_medical_professional_caller):
        callback_ack_response = (
            "Thank you for that. I have sent a message in regards to "
            "this phone call and someone will get back with you. "
            "Please allow up to 72 business hours for this message to "
            "process. Is there anything else I can help you with today?"
        )
        conversation_history.append({"role": "user", "content": user_message})
        conversation_history.append({"role": "assistant", "content": callback_ack_response})
        return jsonify({"response": callback_ack_response})

    lab_result_fax_to_outside_detected = any(
        trigger in message_lower for trigger in LAB_RESULT_FAX_TO_OUTSIDE_TRIGGERS
    )
    # Keep fax to outside active across follow-up messages so AI retains context
    if lab_result_fax_to_outside_detected:
        lab_result_fax_active = True

    lab_order_fax_to_facility_detected = any(
        trigger in message_lower for trigger in LAB_ORDER_FAX_TO_FACILITY_TRIGGERS
    )

    patient_explicitly_requested_fax = any(
        phrase in message_lower for phrase in [
            "fax", "fax it", "fax them", "fax the orders",
            "fax my orders", "fax it over", "fax them over",
            "fax to", "fax my lab", "fax the lab"
        ]
    )

    controlled_substance_schedule = detect_controlled_substance(message_lower)

    # Bug fix: after a contagious patient accepts a virtual visit, Steve
    # was skipping straight to presenting FUTURE virtual availability
    # without ever asking whether they need to be seen today. Mirrors
    # the deterministic yes/no pattern already used for the bridge-
    # medication questions above, so this decision doesn't depend on
    # the LLM remembering to ask it. Placed here (rather than at the
    # point the "virtual visit accepted" injection is built, further
    # below) so a "yes, today" answer can set acute_same_day_established
    # BEFORE is_same_day is computed just below, letting the existing,
    # unmodified same-day / office-hours / covering-provider logic
    # handle everything else exactly as it already does for any other
    # same-day request.
    contagious_present_future_availability = False
    contagious_same_day_check_just_answered = False
    if contagious_same_day_check_pending:
        contagious_same_day_check_pending = False
        contagious_same_day_check_just_answered = True
        if _BARE_NEGATIVE_REPLY_PATTERN.match(message_lower) or any(
                w in message_lower for w in ["wait", "future", "later", "can wait"]
        ):
            contagious_present_future_availability = True
        else:
            # Covers a bare "yes"/"today" reply and any other answer
            # that isn't clearly "I can wait" - defaults to treating
            # ambiguous replies as needing to be seen today, which is
            # the safer direction for a contagious/urgent complaint.
            acute_same_day_established = True

    is_same_day = (
                          any(word in message_lower for word in SAME_DAY_KEYWORDS)
                          and any(word in message_lower for word in SAME_DAY_ACTION_WORDS)
                  ) or acute_same_day_established

    # ── Context injections ──

    pre_chart_context = (
        f"PRE_CHART_STATE INJECTED BY SYSTEM:\n"
        f"Caller first name: {caller_first_name or 'NOT YET COLLECTED'}\n"
        f"Caller last name: {caller_last_name or 'NOT YET COLLECTED'}\n"
        f"Caller is patient: {caller_is_patient}\n"
        f"Patient first name: {patient_first_name or 'NOT YET COLLECTED'}\n"
        f"Patient last name: {patient_last_name or 'NOT YET COLLECTED'}\n"
        f"DOB collected: {dob_collected}\n"
        f"Pre-chart complete: {pre_chart_complete}\n"
        f"Do NOT ask for anything already marked as collected above.\n"
        f"Do NOT ask 'Are you the patient or calling on behalf of "
        f"someone else?' if caller_is_patient is already True or False.\n"
    )
    if caller_is_patient is True:
        pre_chart_context += (
            f"CRITICAL: caller_is_patient is True — this caller IS the "
            f"patient and is already speaking with you directly right "
            f"now. There is no third party. NEVER say 'I'll wait while "
            f"you get {patient_first_name or 'the patient'} on the "
            f"line' or any similar phrase asking them to put the "
            f"patient on the phone. The VERBAL CONSENT TRANSITION "
            f"section does NOT apply to this caller.\n"
        )

    response_time_context = ""
    if response_time_stated:
        response_time_context = (
            "RESPONSE_TIME_ALREADY_STATED INJECTED BY SYSTEM:\n"
            "Do NOT say '72 business hours' or '24 business hours' again.\n"
            "Say 'as previously mentioned' or omit entirely.\n"
        )
    else:
        response_time_context = (
            "RESPONSE_TIME_NOT_YET_STATED INJECTED BY SYSTEM:\n"
            "State timeframe ONCE when appropriate.\n"
            "Standard: 72 business hours.\n"
            "Urgent patient medical only: 24 business hours.\n"
            "Medical professional requests: 72 business hours.\n"
            "Lab order pickup: do NOT state any timeframe.\n"
        )

    office_hours_context = ""
    if office_hours_stated:
        office_hours_context = (
            "OFFICE_HOURS_ALREADY_STATED INJECTED BY SYSTEM:\n"
            "Do NOT repeat office hours again this call.\n"
            "Say 'during our office hours' without restating times.\n"
        )
    else:
        office_hours_context = (
            "OFFICE_HOURS_NOT_YET_STATED INJECTED BY SYSTEM:\n"
            f"If relevant state ONCE: {OFFICE_HOURS}\n"
            "After stating once do not repeat.\n"
        )
    if is_within_office_hours():
        office_hours_context += (
            "OFFICE_CURRENTLY_OPEN INJECTED BY SYSTEM.\n"
        )
    else:
        office_hours_context += (
            "OFFICE_CURRENTLY_CLOSED INJECTED BY SYSTEM:\n"
            "It is currently outside office hours "
            f"({OFFICE_HOURS}). Staff, including medical assistants, are "
            "NOT reachable right now. Do NOT say a staff member is "
            "available, reached, or being connected. Do NOT attempt a "
            "warm transfer. This blocks any REAL-TIME staff consultation "
            "right now - regardless of whether the underlying request is "
            "same-day or a future matter (e.g. paperwork due weeks from "
            "now still cannot be checked with staff live while the "
            "office is closed). It does NOT block offering or booking "
            "FUTURE appointment availability, since that requires no "
            "live staff contact and can proceed normally at any hour.\n"
        )

    # Bug fix: the LLM's only anchor for "what is today's real date"
    # was the TODAY'S DATE line buried inside availability_context,
    # which is gated on scheduling-keyword triggers. A message about
    # an EXISTING appointment (e.g. cancelling one, with no scheduling
    # keywords) got no date grounding at all, so the LLM invented one -
    # in one observed case, incorrectly stating a Wednesday was Labor
    # Day when Labor Day was actually the Monday the call took place
    # on. Computed unconditionally, like office_hours_context above,
    # so every turn has an accurate anchor regardless of topic.
    _todays_holiday_name = get_recognized_office_holiday_name(
        datetime.now().date()
    )
    todays_date_context = (
            f"TODAY'S ACTUAL DATE INJECTED BY SYSTEM: Today is "
            f"{datetime.now().strftime('%A, %B %d, %Y')}"
            + (f" ({_todays_holiday_name}).\n" if _todays_holiday_name else ".\n")
            + "Resolve any relative date the caller uses (\"today\", "
              "\"this Wednesday\", \"next Monday\", etc.) against this "
              "actual date. Never state or assume a different date is "
              "today, and never guess which date is a holiday - only the "
              "date above (if a holiday name is shown) is one.\n"
    )

    medical_professional_context = ""
    if is_medical_professional_caller:
        patient_full = ""
        if med_pro_patient_first and med_pro_patient_last:
            patient_full = f"{med_pro_patient_first} {med_pro_patient_last}"
        medical_professional_context = (
            "MEDICAL_PROFESSIONAL_DETECTED INJECTED BY SYSTEM:\n"
            "Python handled ALL collection AND referral lookup.\n"
            "NEVER ask about HIPAA. NEVER ask for patient info again.\n"
            f"Patient: {patient_full}\n"
            f"DOB: {med_pro_patient_dob or 'collected'}\n"
            f"PCP: {med_pro_patient_pcp or 'collected'}\n"
            f"Referral: {med_pro_referral_status or 'pending'}\n"
        )
        if med_pro_referral_status == "FOUND":
            medical_professional_context += "Referral has been located. Do NOT state 72 business hours for completed referrals.\n"
        else:
            medical_professional_context += "All follow-up timeframes: 72 business hours.\n"

    # Nurse/MA context — Bug 5 fixed
    nurse_ma_context = ""
    if nurse_ma_requested or ma_name_requested or (ma_request_active and not is_medical_professional_caller):
        nurse_ma_office_closed = not is_office_open_today()
        if nurse_ma_office_closed:
            # Holiday-closure fix: mirrors office-closed same-day
            # handling - do not proceed with the MA-availability
            # simulation/routing below (which would otherwise say
            # "let me see if [MA] is available" / "connect you now")
            # when no staff are actually present, whether that's
            # because of a weekend or a recognized holiday.
            holiday_name_today = get_recognized_office_holiday_name(
                datetime.now().date()
            )
            if holiday_name_today:
                nurse_ma_context = (
                    f"NURSE_MA_OFFICE_CLOSED_HOLIDAY INJECTED BY SYSTEM:\n"
                    f"Today is {holiday_name_today}, a recognized office "
                    f"holiday. No staff are reachable.\n"
                    f"Say EXACTLY: 'Our office is currently closed in "
                    f"observance of {holiday_name_today}.'\n"
                    f"Do NOT say you will check on or connect to a nurse "
                    f"or medical assistant. Do NOT offer a callback "
                    f"today.\n"
                )
            else:
                nurse_ma_context = (
                    f"NURSE_MA_OFFICE_CLOSED_WEEKEND INJECTED BY SYSTEM:\n"
                    f"Today is a weekend. No staff are reachable.\n"
                    f"Say: 'I'm sorry, our office is closed today. We "
                    f"are open {OFFICE_HOURS}.'\n"
                    f"Do NOT say you will check on or connect to a nurse "
                    f"or medical assistant. Do NOT offer a callback "
                    f"today.\n"
                )
            ma_avail = False
        else:
            ma_avail = get_ma_availability()
        # Use persistent state if available, otherwise use current detection
        effective_ma_name = ma_request_name if ma_request_name else ma_name_requested
        effective_ma_provider = ma_request_provider if ma_request_provider else ma_provider
        if nurse_ma_office_closed:
            pass
        elif effective_ma_name and effective_ma_provider:
            # Patient asked by name — they already know who it is
            if ma_request_reason_collected:
                # Reason already given - now complete the routing
                nurse_ma_context = (
                    f"NURSE_MA_ROUTING INJECTED BY SYSTEM:\n"
                    f"Patient asked for {effective_ma_name} and has given reason.\n"
                    f"MA_AVAILABILITY: "
                    f"{'AVAILABLE' if ma_avail else 'NOT AVAILABLE'}\n"
                    f"CRITICAL — COMPLETE FULL RESPONSE IN ONE MESSAGE NOW:\n"
                    f"Do NOT ask any more questions.\n"
                    f"If AVAILABLE: Say 'Let me go ahead and connect you with "
                    f"{effective_ma_name} now. Please hold for one moment.'\n"
                    f"If NOT AVAILABLE: Say 'I will put in a message for "
                    f"{effective_ma_name} that you called back. "
                    f"May I get a good callback number for you?'\n"
                    f"NEVER go silent. NEVER check lab results. NEVER check prior authorization status. NEVER run HIPAA.\n"
                    f"Do NOT ask for patient name — already collected.\n"
                )
            else:
                nurse_ma_context = (
                    f"NURSE_MA_REQUEST_BY_NAME INJECTED BY SYSTEM:\n"
                    f"Patient asked for {effective_ma_name} by first name.\n"
                    f"The patient already knows who {effective_ma_name} is.\n"
                    f"Do NOT re-introduce {effective_ma_name}.\n"
                    f"MA_AVAILABILITY: "
                    f"{'AVAILABLE' if ma_avail else 'NOT AVAILABLE'}\n"
                    f"If patient says they are RETURNING {effective_ma_name}'s call:\n"
                    f"Ask ONLY: 'Do you know what it's about?'\n"
                    f"Do NOT ask what they want to discuss.\n"
                    f"Do NOT check lab results. Do NOT run HIPAA.\n"
                    f"Do NOT check on prior authorization status.\n"
                    f"Do NOT ask for patient name — already collected.\n"
                )
        else:
            # Patient asked generically — introduce MA name here and only here
            ma_name, provider_for_ma = get_ma_for_patient()
            if ma_name:
                nurse_ma_context = (
                    f"NURSE_MA_REQUEST_GENERIC INJECTED BY SYSTEM:\n"
                    f"Patient asked to speak with the nurse or MA.\n"
                    f"Patient's MA is {ma_name}.\n"
                    f"Ask ONLY: 'Do you know what it's about?'\n"
                    f"MA_AVAILABILITY: "
                    f"{'AVAILABLE' if ma_avail else 'NOT AVAILABLE'}\n"
                    f"Say: 'I will see if {ma_name} is available.'\n"
                    f"Ask reason for the call.\n"
                    f"Then route based on availability.\n"
                    f"Do NOT ask for patient name — already collected.\n"
                )
            else:
                nurse_ma_context = (
                    f"NURSE_MA_REQUEST_GENERIC INJECTED BY SYSTEM:\n"
                    f"Patient asked to speak with the nurse or MA.\n"
                    f"PCP not yet confirmed — MA name unknown.\n"
                    f"MA_AVAILABILITY: "
                    f"{'AVAILABLE' if ma_avail else 'NOT AVAILABLE'}\n"
                    f"Say: 'Let me check on the medical assistant for you.'\n"
                    f"Ask reason for the call.\n"
                    f"Then route based on availability.\n"
                    f"Do NOT ask for patient name — already collected.\n"
                )

    # Lab order fax to facility — patient explicitly asked to fax to a location
    lab_order_fax_to_facility_context = ""
    if lab_order_fax_to_facility_detected:
        already_has_fax_info = bool(re.search(
            r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b', message_lower
        )) or detect_address_in_message(message_lower)
        lab_order_fax_to_facility_context = (
            "LAB_ORDER_FAX_TO_FACILITY INJECTED BY SYSTEM:\n"
            "Patient has explicitly requested lab orders be faxed to a specific facility.\n"
            "Do NOT ask if they want fax or pickup — they already said fax.\n"
            "Do NOT offer a choice. The patient wants it faxed.\n"
            "CRITICAL: You do NOT have the fax number or address yet just "
            "because a doctor or office name was mentioned. A doctor or "
            "specialist name is NOT a fax number and is NOT enough to "
            "send a fax. You MUST collect either an actual fax number or "
            "a physical address before confirming anything has been sent.\n"
            f"Fax number or address already provided this message: "
            f"{already_has_fax_info}\n"
            "If fax number or address NOT yet provided, say EXACTLY this "
            "and nothing else: 'Can you please provide me with a fax "
            "number or the address so that I can look up the fax "
            "number?'\n"
            "Do NOT say 'I've got the address' or 'I have the address' "
            "or anything implying you already possess contact "
            "information you have not actually been given.\n"
            "Do NOT say you will send anything until you have a real "
            "fax number or address. Do NOT assume there is only one "
            "office or one provider with that name.\n"
            "Once fax number or address IS provided: confirm you will "
            "fax the orders. Then ask: 'Would you also like to come by "
            "and pick up hard copies of the orders?'\n"
            "If YES state office hours ONCE.\n"
            "If NO confirm fax is all that is needed.\n"
            "Do NOT ask reason for lab work.\n"
            "Do NOT ask if PCP aware.\n"
            "Do NOT say 72 business hours.\n"
        )

    # Lab order pickup context — Bug 4 fixed
    lab_order_pickup_context = ""
    if lab_order_pickup:
        if patient_explicitly_requested_fax:
            lab_order_pickup_context = (
                "LAB_ORDER_PICKUP_WITH_FAX_REQUESTED INJECTED BY SYSTEM:\n"
                "Patient has EXPLICITLY requested the orders be faxed.\n"
                "Do NOT ask if they want fax or pickup — they already said fax.\n"
                "STEP 1: Ask for the fax number of the facility.\n"
                "If patient does not have fax number ask for the facility "
                "name and address so you can look it up.\n"
                "STEP 2: Confirm you will print and fax the orders.\n"
                "STEP 3: State office hours ONCE if not already stated.\n"
                "Do NOT ask reason for lab work.\n"
                "Do NOT ask if PCP aware.\n"
                "Do NOT say 72 business hours.\n"
                "If OFFICE_HOURS_ALREADY_STATED: do NOT repeat hours.\n"
            )
        else:
            lab_order_pickup_context = (
                "LAB_ORDER_PICKUP_DETECTED INJECTED BY SYSTEM:\n"
                "Patient wants to pick up EXISTING lab orders.\n"
                "This is NOT a new lab work request.\n"
                "FOLLOW THIS ORDER EXACTLY:\n"
                "STEP 1 — Ask if patient wants orders faxed first:\n"
                "Say: 'Would you like me to fax the lab orders over to the "
                "facility for you as well, or would you prefer to just "
                "pick them up?'\n"
                "STEP 2A — If patient WANTS fax:\n"
                "ONLY AFTER patient confirms they want fax — THEN collect "
                "contact info for the facility.\n"
                "Do NOT mention fax number or address until patient "
                "confirms they want it faxed.\n"
                "Confirm you will print and fax the orders.\n"
                "State office hours ONCE for pickup.\n"
                "STEP 2B — If patient does NOT want fax:\n"
                "Confirm orders will be printed and ready for pickup.\n"
                "State office hours ONCE.\n"
                "Do NOT ask reason for lab work.\n"
                "Do NOT ask if PCP aware.\n"
                "Do NOT say 72 business hours.\n"
                "If OFFICE_HOURS_ALREADY_STATED: do NOT repeat hours.\n"
            )

    urgent_context = ""
    if urgent_symptoms_active and not urgent_can_wait_asked:
        urgent_context = (
            "URGENT_SYMPTOMS_PHASE_1 INJECTED BY SYSTEM:\n"
            "Truly urgent present tense symptoms.\n"
            "PHASE 1: Acknowledge calmly. No alarming language.\n"
            "First say: 'I'm not able to provide medical advice.' Then "
            "ask ONLY: 'Is this something that can wait or do you "
            "need assistance right away?' STOP. Wait for answer.\n"
            "NEVER mention ER or 911.\n"
        )
        urgent_can_wait_asked = True
    elif caller_answered_wait_question:
        if caller_cannot_wait:
            ma_available = get_ma_availability()
            if ma_available:
                urgent_context = (
                    "URGENT_SYMPTOMS_PHASE_2 — CANNOT WAIT — MA "
                    "AVAILABLE:\n"
                    "Complete FULL response in ONE message.\n"
                    "Say: 'Let me get our medical assistant on the line "
                    "for you right away. (pause) I have our medical "
                    "assistant on the line for you now.'\n"
                    "NEVER mention ER or 911.\n"
                )
            else:
                urgent_context = (
                    "URGENT_SYMPTOMS_PHASE_2 — CANNOT WAIT — MA NOT "
                    "AVAILABLE:\n"
                    "Complete FULL response in ONE message.\n"
                    "Say: 'I attempted to reach our medical assistant "
                    "but they are not available. May I get a good "
                    "callback number? I will put in a high priority "
                    "message for the provider. If you need immediate "
                    "assistance, I would recommend visiting your "
                    "nearest urgent care center.'\n"
                    "NEVER mention ER or 911.\n"
                )
        else:
            urgent_context = (
                "URGENT_SYMPTOMS_PHASE_2 — CAN WAIT:\n"
                "Complete FULL response in ONE message.\n"
                "Do NOT check or mention medical assistant availability "
                "for this case — only check the MA if the patient says "
                "it cannot wait.\n"
                "Say: 'May I get a good callback number? I will put in "
                "a high priority message for the provider. If it turns "
                "out this cannot wait after all, please visit your "
                "nearest urgent care center.'\n"
                "NEVER mention ER or 911.\n"
            )

    patient_presence_context = ""
    if patient_will_call_back and third_party_detected:
        patient_presence_context = (
            "PATIENT_WILL_CALL_BACK: Respond naturally — "
            "'That sounds great. When [patient name] calls we will be "
            "happy to assist further. Anything else today?'\n"
        )
    elif patient_is_not_present and third_party_detected:
        patient_presence_context = (
            "PATIENT_NOT_PRESENT: NEVER say I'll wait.\n"
            "Recommend patient call directly. Offer new appointment.\n"
            "HIPAA form: in-person or mailed physical form only.\n"
        )
    elif patient_is_present and third_party_detected and verbal_consent_requested:
        patient_presence_context = (
            "PATIENT_PRESENT: Say ONLY: 'Could you please put "
            "[patient name] on the line?' Nothing else.\n"
        )

    # Sprint 12: patient just accepted a previously-offered virtual visit.
    # Computed here (before availability_context below) so both that
    # block and the contagious-visit context further down can use the
    # same snapshot value; the flag itself is only consumed (reset)
    # further down, after both uses have already read it.
    #
    # virtual_visit_offered is only ever set True by the contagious-
    # symptom code path further below - it's the sole deterministic
    # virtual-visit-offer trigger in the codebase. A virtual visit
    # offered for any other reason (e.g. an escalated paperwork/note
    # request) is pure AI improvisation with no Python state behind it,
    # so acceptance of THAT offer would otherwise go undetected here.
    # As a general fallback, also recognize acceptance when Steve's own
    # most recent message actually offered a virtual visit, regardless
    # of which workflow prompted it.
    virtual_visit_recently_offered = virtual_visit_offered
    if not virtual_visit_recently_offered:
        for turn in reversed(conversation_history):
            if turn.get("role") == "assistant":
                last_assistant_lower = turn.get("content", "").lower()
                if "virtual visit" in last_assistant_lower and (
                        "would you" in last_assistant_lower
                        or "willing" in last_assistant_lower
                        or "?" in last_assistant_lower
                ):
                    virtual_visit_recently_offered = True
                break

    virtual_visit_accepted_now = (
            virtual_visit_recently_offered
            and patient_wants_to_proceed(message_lower)
            and not is_medical_professional_caller
    )

    # Sprint 12: patient just accepted checking with Catherine about
    # Elizabeth Horowitz (the covering provider) after their own PCP's
    # MA declined a same-day visit.
    covering_provider_check_accepted_now = (
            covering_provider_offer_pending
            and patient_wants_to_proceed(message_lower)
            and not is_medical_professional_caller
    )

    covering_provider_check_declined_now = (
            covering_provider_offer_pending
            and patient_wants_to_decline(message_lower)
            and not is_medical_professional_caller
    )

    # Sprint 12: patient just answered whether the provider calling in a
    # medication (instead of an office visit) works for them.
    provider_callin_accepted_now = (
            provider_callin_offer_pending
            and patient_wants_to_proceed(message_lower)
            and not is_medical_professional_caller
    )
    provider_callin_declined_now = (
            provider_callin_offer_pending
            and patient_wants_to_decline(message_lower)
            and not is_medical_professional_caller
    )

    same_day_virtual_clinic_declined_now = (
            same_day_virtual_clinic_offer_pending
            and patient_wants_to_decline(message_lower)
            and not is_medical_professional_caller
    )

    # RT13 fix: after Steve asks "What is the reason for the
    # appointment?" (per APPOINTMENT SCHEDULING - FUTURE), the patient's
    # answer is very often *only* the reason itself (e.g. "I want to
    # discuss being put on weight loss medication") - none of the
    # keywords availability_context's outer gate below checks for
    # ("appointment", "schedule", "available", "availability", "next
    # week", "this week") typically appear in that reply. Without this,
    # availability_context stays empty on exactly the turn the AI is
    # instructed to present real availability, so it has nothing to
    # draw from and either fabricates times or stalls. Mirrors the
    # virtual_visit_recently_offered pattern above: scan Steve's own
    # last reply rather than requiring a Python state flag, since this
    # workflow (unlike PHF/new-patient scheduling) has no state machine
    # to hook a flag into.
    appointment_reason_just_requested = False
    for turn in reversed(conversation_history):
        if turn.get("role") == "assistant":
            last_assistant_lower = turn.get("content", "").lower()
            if (
                    "reason" in last_assistant_lower
                    and "appointment" in last_assistant_lower
                    and "?" in last_assistant_lower
            ):
                appointment_reason_just_requested = True
            break

    availability_context = ""
    if (
            any(word in message_lower for word in [
                "appointment", "schedule", "available", "availability",
                "next week", "this week"
            ])
            or virtual_visit_accepted_now
            or same_day_virtual_clinic_declined_now
            or next_available_options_pending
            or appointment_reason_just_requested
            or controlled_substance_schedule
            or controlled_substance_appt_pending
    ) and not is_medical_professional_caller and not (
            individual_six_month_followup_active
            and individual_six_month_followup_eligible is False
    ) and not individual_three_month_followup_active and not nurse_visit_active:
        if (
                not is_same_day or virtual_visit_accepted_now
                or same_day_virtual_clinic_declined_now
                or next_available_options_pending
                or appointment_reason_just_requested
        ):
            forced_pcp_weekly = get_harness_override("STEVE_FORCE_PCP_WEEKLY_AVAILABLE")
            if generic_weekly_schedule_snapshot is None:
                generic_weekly_schedule_snapshot = generate_weekly_availability(
                    force_has_availability=forced_pcp_weekly)
            schedule = generic_weekly_schedule_snapshot
            pcp_has_no_availability_this_week = not any(schedule.values())
            if pcp_has_no_availability_this_week and not virtual_visit_accepted_now:
                covering_eligible_week = is_visit_eligible_for_covering_provider(
                    message_lower
                )
                if covering_eligible_week:
                    forced_covering_weekly = get_harness_override("STEVE_FORCE_COVERING_WEEKLY_AVAILABLE")
                    if generic_covering_weekly_schedule_snapshot is None:
                        generic_covering_weekly_schedule_snapshot = (
                            generate_weekly_availability(
                                force_has_availability=forced_covering_weekly))
                    covering_weekly_schedule = (
                        generic_covering_weekly_schedule_snapshot)
                    covering_has_availability = any(
                        covering_weekly_schedule.values()
                    )
                    if covering_has_availability:
                        availability_text = (
                            "WEEKLY AVAILABILITY INJECTED BY SYSTEM:\n"
                            "Your provider has NO availability within the next "
                            "7 days.\n"
                            f"COVERING PROVIDER ({COVERING_PROVIDER_CREDENTIAL} "
                            f"{COVERING_PROVIDER_BARE_NAME}) availability this "
                            "week:\n"
                        )
                        for day, slots in covering_weekly_schedule.items():
                            if slots:
                                availability_text += f"- {day}: {', '.join(slots)}\n"
                            else:
                                availability_text += f"- {day}: No availability\n"
                        availability_text += (
                            "Tell the patient their own provider has no "
                            "availability this week, and offer to schedule "
                            f"with {COVERING_PROVIDER_CREDENTIAL} "
                            f"{COVERING_PROVIDER_BARE_NAME} instead using the "
                            "times above. Use this exact availability only.\n"
                        )
                    else:
                        availability_text = (
                            "WEEKLY AVAILABILITY INJECTED BY SYSTEM:\n"
                            "Neither your provider nor the covering provider "
                            f"({COVERING_PROVIDER_CREDENTIAL} "
                            f"{COVERING_PROVIDER_BARE_NAME}) has any "
                            "availability within the next 7 days.\n"
                        )
                else:
                    availability_text = (
                        "WEEKLY AVAILABILITY INJECTED BY SYSTEM:\n"
                        "Your provider has NO availability within the next 7 "
                        "days, and this visit type is not eligible for the "
                        "covering provider.\n"
                    )
                availability_context = availability_text
            else:
                availability_text = "WEEKLY AVAILABILITY INJECTED BY SYSTEM:\n"
                for day, slots in schedule.items():
                    if slots:
                        availability_text += f"- {day}: {', '.join(slots)}\n"
                    else:
                        availability_text += f"- {day}: No availability\n"
                availability_text += "Use this exact availability only.\n"
                if virtual_visit_accepted_now:
                    availability_text += (
                        "The patient just accepted a virtual visit. Present "
                        "these times as the available VIRTUAL visit slots now. "
                        "Do NOT say you will check with the medical assistant — "
                        "present the times directly.\n"
                    )
                availability_context = availability_text

    if availability_context:
        _today = datetime.now()
        _today_weekday_index = _today.weekday()  # 0=Monday ... 6=Sunday
        # Root-cause fix: the LLM was previously asked to CALCULATE
        # each weekday's actual calendar date itself from a natural-
        # language hint ("skip today and earlier days... treat the
        # rest as next week"). That is exactly what produced dates
        # that don't match their stated weekday (e.g. "Monday,
        # September 1" when September 1, 2026 is actually a Tuesday)
        # and, more seriously, dates that fall in the past. Python now
        # computes the exact next occurrence of every weekday name
        # directly and hands it to the model as a literal lookup table
        # - no arithmetic left for the model to get wrong. The formula
        # below (delta = (idx - today_idx) % 7, pushed to +7 when zero)
        # uniformly reproduces "skip today itself and any day already
        # passed this week, otherwise use next week's occurrence" for
        # every day of the week today could be, replacing the previous
        # two-branch (weekday vs. weekend) special-casing with one
        # formula that is correct for both.
        _weekday_name_to_index = {
            "Monday": 0, "Tuesday": 1, "Wednesday": 2,
            "Thursday": 3, "Friday": 4,
        }
        _date_lines = []
        _holiday_days = []
        for _day_name, _idx in _weekday_name_to_index.items():
            _delta = (_idx - _today_weekday_index) % 7
            if _delta == 0:
                _delta = 7
            _real_date = _today + timedelta(days=_delta)
            _holiday_name = get_recognized_office_holiday_name(_real_date.date())
            if _holiday_name:
                # Bug fix: a computed weekday date can land on a
                # recognized holiday (e.g. "Monday" resolving to Labor
                # Day) - that date must never be offered as an
                # available slot, regardless of what the simulated
                # weekly schedule above says for that day name.
                _date_lines.append(
                    f"{_day_name} = {_real_date.strftime('%B %d, %Y')} "
                    f"— OFFICE HOLIDAY ({_holiday_name}), NOT AVAILABLE"
                )
                _holiday_days.append(_day_name)
            else:
                _date_lines.append(
                    f"{_day_name} = {_real_date.strftime('%B %d, %Y')}"
                )
        _valid_days_note = (
                "REAL CALENDAR DATES INJECTED BY SYSTEM — use these EXACT "
                "dates whenever you state a date to the patient for any of "
                "the weekday names in the availability above. Do NOT "
                "calculate, guess, or adjust these dates yourself:\n"
                + "\n".join(_date_lines) + "\n"
                                           "Never state, offer, or confirm any date earlier than "
                                           f"today's actual date ({_today.strftime('%B %d, %Y')}).\n"
        )
        if _holiday_days:
            _valid_days_note += (
                    "The office is closed on any day marked OFFICE HOLIDAY "
                    "above, regardless of what slots the availability list "
                    "shows for that day name - do NOT offer, confirm, or "
                    "mention any time on " + ", ".join(_holiday_days) + " "
                                                                        "this week.\n"
            )
        availability_context += (
            f"\nTODAY'S DATE INJECTED BY SYSTEM: Today is "
            f"{_today.strftime('%A, %B %d, %Y')}. Resolve any relative "
            f"date the patient uses (\"today\", \"tomorrow\", \"this "
            f"Monday\", \"next Monday\", etc.) against this actual date "
            f"before matching it to the weekly availability above.\n"
            f"{_valid_days_note}"
        )

    same_day_context = ""
    if is_same_day and not is_medical_professional_caller:
        now = datetime.now()
        current_time_str = now.strftime("%I:%M %p")
        office_open_today = is_office_open_today()
        within_office_hours = is_within_office_hours()
        if not office_open_today:
            next_monday = get_next_business_day()
            holiday_name_today = get_recognized_office_holiday_name(
                datetime.now().date()
            )
            if holiday_name_today:
                # Holiday closures get a distinct, simpler message than
                # a plain weekend closure - no covering-provider or
                # virtual-clinic offer, matching the required examples
                # exactly ("Our office is currently closed in
                # observance of Labor Day. We will reopen on the next
                # business day."). NEVER imply staff, PCP, or covering
                # provider availability on a recognized holiday.
                same_day_context = (
                    f"OFFICE_CLOSED_HOLIDAY INJECTED BY SYSTEM:\n"
                    f"Today is {holiday_name_today}, a recognized office "
                    f"holiday. Office is CLOSED.\n"
                    f"Next business day: {next_monday}\n"
                    f"Say EXACTLY: 'Our office is currently closed in "
                    f"observance of {holiday_name_today}. We will reopen "
                    f"on the next business day.'\n"
                    f"NEVER offer slots today. NEVER mention the covering "
                    f"provider or medical assistant as available or "
                    f"checkable today. NEVER mention ER. NEVER imply "
                    f"staff are reachable today.\n"
                )
            elif contagious_visit_active:
                # Bug fix: the office is fully closed (weekend) - no
                # provider, PCP or covering, can actually be checked
                # for in-office same-day availability. The previous
                # wording ("I can look to see if the covering provider
                # has any same-day availability") implied in-office
                # coverage that cannot exist while the office itself is
                # closed. Mirrors the already-correct weekday-outside-
                # hours contagious branch below (OFFICE_CLOSED_
                # CONTAGIOUS_SAME_DAY), which redirects straight to the
                # Same-Day Virtual Clinic instead of implying any
                # provider could be checked.
                same_day_virtual_clinic_offer_pending = True
                same_day_context = (
                    f"OFFICE_CLOSED_CONTAGIOUS_SAME_DAY INJECTED BY SYSTEM:\n"
                    f"Today is a weekend. Office is CLOSED - no medical "
                    f"assistant, provider, or staff member is reachable "
                    f"right now.\n"
                    f"Say: 'I'm sorry, our office is closed today so "
                    f"I'm not able to check with a medical assistant or "
                    f"provider right now. Since your symptoms sound "
                    f"contagious, I can offer you our Same-Day Virtual "
                    f"Clinic instead. You can reach them directly at "
                    f"{SAME_DAY_VIRTUAL_CLINIC_PHONE_NUMBER}, available "
                    f"{SAME_DAY_VIRTUAL_CLINIC_HOURS}. Would you like me "
                    f"to transfer you there now?'\n"
                    f"CRITICAL — DO NOT:\n"
                    f"- Say a medical assistant was contacted or checked.\n"
                    f"- Say a provider (including the covering provider) "
                    f"was contacted or checked.\n"
                    f"- Say availability was checked.\n"
                    f"- Say office staff were contacted.\n"
                    f"The office is closed right now — there is no one "
                    f"to check with.\n"
                )
                covering_provider_visit_eligible = False
            else:
                covering_eligible = is_visit_eligible_for_covering_provider(
                    message_lower
                )
                covering_provider_visit_eligible = covering_eligible
                if covering_eligible:
                    covering_provider_offer_pending = True
                    same_day_context = (
                        f"OFFICE_CLOSED_TODAY INJECTED BY SYSTEM:\n"
                        f"Today is a weekend. Office is CLOSED for your "
                        f"provider.\n"
                        f"Next available day: {next_monday}\n"
                        f"Say: 'I'm sorry but we don't have any same-day "
                        f"appointments available today with your provider. "
                        f"I can look to see if the covering provider "
                        f"{COVERING_PROVIDER_CREDENTIAL} "
                        f"{COVERING_PROVIDER_BARE_NAME} has any same-day "
                        f"availability. Alternatively, if you're in need of "
                        f"immediate attention, I can recommend that you "
                        f"visit an urgent care center. Would you like me to "
                        f"see if {COVERING_PROVIDER_BARE_NAME} has any "
                        f"availability to see you today?'\n"
                        f"Do NOT mention {next_monday} yet unless the "
                        f"patient declines checking the covering provider.\n"
                        f"NEVER offer slots today for the patient's own PCP. "
                        f"NEVER mention ER.\n"
                    )
                else:
                    same_day_context = (
                        f"OFFICE_CLOSED_TODAY INJECTED BY SYSTEM:\n"
                        f"Today is a weekend. Office is CLOSED.\n"
                        f"Next available day: {next_monday}\n"
                        f"Say: 'I'm sorry but our office is closed today. "
                        f"We are open Monday through Friday. I would be "
                        f"happy to schedule you for {next_monday}. If this "
                        f"cannot wait until Monday I would recommend "
                        f"visiting your nearest urgent care center.'\n"
                        f"NEVER offer slots today. NEVER mention ER.\n"
                    )
        elif not within_office_hours:
            # RT12: Weekday, but before/after actual office hours (e.g.
            # 5:17 AM). is_office_open_today() alone can't catch this -
            # it only checks day of week, not time of day - which is why
            # this branch exists as a distinct elif rather than folding
            # into the "not office_open_today" branch above. No staff
            # are reachable right now, so Steve must never claim to have
            # checked with a medical assistant, provider, or availability.
            if contagious_visit_active:
                same_day_virtual_clinic_offer_pending = True
                same_day_context = (
                    f"OFFICE_CLOSED_CONTAGIOUS_SAME_DAY INJECTED BY SYSTEM:\n"
                    f"It is currently outside office hours ({OFFICE_HOURS}). "
                    f"No medical assistant, provider, or staff member is "
                    f"reachable right now.\n"
                    f"Say: 'I'm sorry, our office is currently closed so "
                    f"I'm not able to check with a medical assistant or "
                    f"provider right now. Since your symptoms sound "
                    f"contagious, I can offer you our Same-Day Virtual "
                    f"Clinic instead. You can reach them directly at "
                    f"{SAME_DAY_VIRTUAL_CLINIC_PHONE_NUMBER}, available "
                    f"{SAME_DAY_VIRTUAL_CLINIC_HOURS}. Would you like me "
                    f"to transfer you there now?'\n"
                    f"CRITICAL — DO NOT:\n"
                    f"- Say a medical assistant was contacted or checked.\n"
                    f"- Say a provider was contacted or checked.\n"
                    f"- Say availability was checked.\n"
                    f"- Say office staff were contacted.\n"
                    f"- Say you received any real-time response from staff.\n"
                    f"The office is closed right now — there is no one to "
                    f"check with.\n"
                )
            else:
                # Root-cause fix: this branch previously left "the next
                # available appointment" undefined, so the AI guessed
                # "tomorrow" even when it's a weekday call before 9 AM and
                # the office actually reopens LATER THAT SAME DAY. Compute
                # the real next-reopen moment instead of leaving it to be
                # guessed.
                if now.hour < 9:
                    _next_reopen_label = f"today at 9:00 AM ({now.strftime('%A, %B %d')})"
                else:
                    _next_open_day = now + timedelta(days=1)
                    while _next_open_day.weekday() >= 5:
                        _next_open_day += timedelta(days=1)
                    _next_reopen_label = (
                        f"{_next_open_day.strftime('%A, %B %d')} at 9:00 AM"
                    )
                same_day_context = (
                    f"OFFICE_CLOSED_TODAY INJECTED BY SYSTEM:\n"
                    f"It is currently outside office hours ({OFFICE_HOURS}). "
                    f"No staff are reachable right now. The office reopens "
                    f"{_next_reopen_label} — do NOT say \"tomorrow\" unless "
                    f"that is actually when the office reopens.\n"
                    f"Say: 'I'm sorry but our office is currently closed. "
                    f"We are open {OFFICE_HOURS}. Since you need to be seen "
                    f"today, I'll create a high priority message for the "
                    f"office so they can follow up with you as soon as "
                    f"we reopen ({_next_reopen_label}). If this cannot wait "
                    f"I would recommend visiting your nearest urgent care "
                    f"center.'\n"
                    f"Then obtain a good callback number and confirm a high "
                    f"priority phone message has been created for follow-up.\n"
                    f"NEVER offer slots today. NEVER mention ER. NEVER "
                    f"claim to have checked with staff — the office is "
                    f"closed.\n"
                )
        else:
            available_slots = get_future_available_times()
            if available_slots:
                ma_approves_same_day = get_ma_same_day_approval()
                slots_str = ", ".join(available_slots)
                if ma_approves_same_day:
                    same_day_context = (
                        f"SAME_DAY_AVAILABILITY INJECTED BY SYSTEM:\n"
                        f"Current time: {current_time_str}\n"
                        f"MA_SAME_DAY_APPROVAL: APPROVED\n"
                        f"Say: 'Let me check with the medical assistant on "
                        f"fitting you in today. (pause) Good news, the "
                        f"medical assistant can fit you in.' Then state "
                        f"current time and present ONLY these slots: "
                        f"{slots_str}\n"
                        f"Wait for selection before confirming.\n"
                    )
                else:
                    covering_eligible = is_visit_eligible_for_covering_provider(
                        message_lower
                    )
                    covering_provider_visit_eligible = covering_eligible
                    patient_pcp = get_patient_pcp_from_history()
                    pcp_name = patient_pcp if patient_pcp else "your provider"
                    if covering_eligible:
                        covering_provider_offer_pending = True
                        same_day_context = (
                            f"SAME_DAY_AVAILABILITY INJECTED BY SYSTEM:\n"
                            f"Current time: {current_time_str}\n"
                            f"MA_SAME_DAY_APPROVAL: DECLINED\n"
                            f"Say: 'Let me check with the medical assistant "
                            f"on fitting you in today. (pause) I'm sorry, "
                            f"they were not able to fit you in with "
                            f"{pcp_name} for today. Would you like me to "
                            f"check with {COVERING_PROVIDER_MA_NAME} to see "
                            f"if our covering provider "
                            f"{COVERING_PROVIDER_CREDENTIAL} "
                            f"{COVERING_PROVIDER_BARE_NAME} can see you "
                            f"today?'\n"
                            f"Do NOT offer any of today's time slots "
                            f"({slots_str}) — the MA did not approve them.\n"
                            f"Do NOT mention urgent care yet — urgent care "
                            f"only comes up after the covering provider is "
                            f"also checked and unavailable, or the patient "
                            f"declines the covering provider.\n"
                        )
                    else:
                        same_day_virtual_clinic_offer_pending = True
                        same_day_context = (
                            f"SAME_DAY_AVAILABILITY INJECTED BY SYSTEM:\n"
                            f"Current time: {current_time_str}\n"
                            f"MA_SAME_DAY_APPROVAL: DECLINED\n"
                            f"Say: 'Let me check with the medical assistant "
                            f"on fitting you in today. (pause) I'm sorry, "
                            f"they were not able to fit you in with "
                            f"{pcp_name} for today. However, although "
                            f"{pcp_name} is not available to see you "
                            "today, I can offer you a same-day virtual "
                            "clinic appointment if that would work for "
                            "you?'\n"
                            f"Do NOT offer any of today's time slots "
                            f"({slots_str}) — the MA did not approve them.\n"
                        )
            else:
                covering_eligible = is_visit_eligible_for_covering_provider(message_lower)
                if covering_eligible:
                    same_day_context = (
                        f"SAME_DAY_AVAILABILITY INJECTED BY SYSTEM:\n"
                        f"Current time: {current_time_str}\n"
                        "No slots available today with your provider. "
                        "Say: 'I'm sorry but I don't have any same-day "
                        "appointments available today with your provider. "
                        "Would you like me to check with Catherine to see "
                        "if our covering provider Nurse Practitioner "
                        "Elizabeth Horowitz can see you today?'\n"
                    )
                else:
                    same_day_virtual_clinic_offer_pending = True
                    same_day_context = (
                        f"SAME_DAY_AVAILABILITY INJECTED BY SYSTEM:\n"
                        f"Current time: {current_time_str}\n"
                        "No slots available today with your provider. "
                        "Say: 'I'm sorry but I don't have any same-day "
                        "appointments available today with your provider. "
                        "However, I can transfer you to the Same-Day Virtual "
                        "Clinic scheduling team if you'd like.'\n"
                    )

    same_day_virtual_clinic_offer_was_pending = same_day_virtual_clinic_offer_pending
    next_day_or_urgent_care_was_pending = next_day_or_urgent_care_pending

    covering_provider_context = ""
    if covering_provider_check_accepted_now:
        covering_provider_offer_pending = False
        elizabeth_available = get_covering_provider_same_day_availability()
        if elizabeth_available:
            elizabeth_slots = get_future_available_times()
            if elizabeth_slots:
                covering_slots_str = ", ".join(elizabeth_slots)
                covering_provider_context = (
                    "COVERING_PROVIDER_SAME_DAY INJECTED BY SYSTEM:\n"
                    "COVERING_PROVIDER_AVAILABILITY: AVAILABLE\n"
                    f"Say: '{COVERING_PROVIDER_MA_NAME} confirmed that "
                    f"{COVERING_PROVIDER_CREDENTIAL} "
                    f"{COVERING_PROVIDER_BARE_NAME} can see you today.' "
                    f"Then present ONLY these times: {covering_slots_str}\n"
                    "Wait for selection before confirming.\n"
                    "Do NOT mention urgent care.\n"
                )
            else:
                same_day_virtual_clinic_offer_pending = True
                covering_provider_context = (
                    "COVERING_PROVIDER_SAME_DAY INJECTED BY SYSTEM:\n"
                    "COVERING_PROVIDER_AVAILABILITY: AVAILABLE_BUT_NO_SLOTS\n"
                    f"Say: '{COVERING_PROVIDER_MA_NAME} confirmed that "
                    f"{COVERING_PROVIDER_CREDENTIAL} "
                    f"{COVERING_PROVIDER_BARE_NAME} can see you today, but "
                    "there are no open slots left right now. However, I "
                    "can transfer you to the Same-Day Virtual Clinic scheduling team "
                    "if that would work for you?'\n"
                    "Do NOT mention urgent care yet.\n"
                )
        else:
            same_day_virtual_clinic_offer_pending = True
            patient_pcp = get_patient_pcp_from_history()
            pcp_name = patient_pcp if patient_pcp else "your provider"
            covering_provider_context = (
                "COVERING_PROVIDER_SAME_DAY INJECTED BY SYSTEM:\n"
                "COVERING_PROVIDER_AVAILABILITY: NOT_AVAILABLE\n"
                f"Say: 'I'm sorry, {COVERING_PROVIDER_MA_NAME} let me know "
                f"that {COVERING_PROVIDER_BARE_NAME} is not able to see "
                f"you today either. However, although {pcp_name} and "
                f"{COVERING_PROVIDER_BARE_NAME} will not be available to "
                "see you today, I can transfer you to the Same-Day "
                "Virtual Clinic scheduling team if you'd like.'\n"
            )
    elif covering_provider_check_declined_now:
        covering_provider_offer_pending = False
        same_day_virtual_clinic_offer_pending = True
        patient_pcp = get_patient_pcp_from_history()
        pcp_name = patient_pcp if patient_pcp else "your provider"
        covering_provider_context = (
            "COVERING_PROVIDER_DECLINED INJECTED BY SYSTEM:\n"
            "Patient declined checking with "
            f"{COVERING_PROVIDER_MA_NAME} about the covering provider.\n"
            f"Say EXACTLY: 'I understand. Although {pcp_name} will not "
            "be available to see you today, would you like me to set "
            "you up with an appointment for their next available slot? "
            "If it can't wait, I recommend that you visit the urgent "
            "care center. Or you can call scheduling and set up an "
            "appointment with the same day virtual clinic.'\n"
            "Do NOT re-ask about the covering provider. Do NOT ask "
            "clarifying questions about symptoms — the triage step is "
            "already complete.\n"
            "If the patient indicates they want to be seen at the "
            "same-day virtual clinic, provide the phone number from the "
            "SAME_DAY_VIRTUAL_CLINIC injection (see below) if present.\n"
        )

    next_available_options_was_pending = next_available_options_pending

    same_day_virtual_clinic_context = ""
    if same_day_virtual_clinic_offer_was_pending and not is_medical_professional_caller:
        virtual_clinic_requested = any(
            phrase in message_lower for phrase in [
                "virtual clinic", "same day virtual", "same-day virtual",
                "virtual visit today", "virtual option",
            ]
        )
        if virtual_clinic_requested or patient_wants_to_proceed(message_lower):
            same_day_virtual_clinic_offer_pending = False
            same_day_virtual_clinic_context = (
                "SAME_DAY_VIRTUAL_CLINIC INJECTED BY SYSTEM:\n"
                "Patient wants to be seen at the same-day virtual clinic.\n"
                "1. Offer to transfer the patient now.\n"
                f"2. Provide their phone number ({SAME_DAY_VIRTUAL_CLINIC_PHONE_NUMBER}) in case you get disconnected.\n"
                "3. Thank the patient, wish them well, and end the conversation cleanly.\n"
                "4. Do NOT ask for any callback confirmation or additional information.\n"
            )
        elif patient_wants_to_decline(message_lower):
            same_day_virtual_clinic_offer_pending = False
            next_available_options_pending = True
            same_day_virtual_clinic_context = (
                "SAME_DAY_VIRTUAL_CLINIC_DECLINED INJECTED BY SYSTEM:\n"
                "The patient has declined the same-day virtual clinic.\n"
                "1. Advise the patient that because they want to be evaluated today, "
                "they should visit Urgent Care.\n"
                "2. If the WEEKLY AVAILABILITY injection shows available times, offer them. If it shows NO availability within 7 days, first explain to the patient that neither their primary provider nor the covering provider has availability within the next 7 days. Then recommend Urgent Care because they want to be evaluated today. Then ask whether they prefer Urgent Care or a work-in request. Do NOT offer a callback, phone follow-up, provider callback, or any alternative. Only ask the patient to choose between Urgent Care and a work-in request and then wait for their response.\n"
            )

    if next_available_options_was_pending and not is_medical_professional_caller:
        if patient_wants_to_decline(message_lower):
            next_available_options_pending = False
            same_day_virtual_clinic_context += (
                "\nNEXT_AVAILABLE_OPTIONS_DECLINED INJECTED BY SYSTEM:\n"
                "Patient declined all of the next-available scheduling "
                "options.\n"
                "Say: Ask the patient for their preferred callback number. Create a message for the PCP requesting a work-in "
                "review, and let the patient know the office will review possible work-in options and someone will be in touch "
                "within 24 business hours. Mention that urgent care is available if the condition cannot wait.\n"
            )
        elif patient_wants_to_proceed(message_lower):
            next_available_options_pending = False
            same_day_virtual_clinic_context += (
                "\nNEXT_AVAILABLE_OPTIONS_PROCEED INJECTED BY SYSTEM:\n"
                "Patient wants to proceed but there are no available appointments "
                "within the next 7 days. Ask whether they prefer urgent care or "
                "a work-in request.\n"
            )

    if next_day_or_urgent_care_was_pending and not is_medical_professional_caller:
        wants_urgent_care_instead = "urgent care" in message_lower
        if wants_urgent_care_instead:
            next_day_or_urgent_care_pending = False
            same_day_virtual_clinic_context += (
                "\nNEXT_DAY_OR_URGENT_CARE_RESOLVED INJECTED BY SYSTEM:\n"
                "Patient prefers to go to urgent care instead of waiting "
                "for the next available day. Acknowledge their choice.\n"
            )
        elif patient_wants_to_proceed(message_lower):
            next_day_or_urgent_care_pending = False
            next_day_schedule = generate_weekly_availability()
            chosen_day = None
            chosen_time = None
            for day in DAYS_OF_WEEK:
                if next_day_schedule.get(day):
                    chosen_day = day
                    chosen_time = next_day_schedule[day][0]
                    break
            if chosen_day and chosen_time:
                same_day_virtual_clinic_context += (
                    "\nNEXT_DAY_APPOINTMENT INJECTED BY SYSTEM:\n"
                    "Patient wants to be scheduled for the next available "
                    "day instead of urgent care.\n"
                    f"NEXT_AVAILABLE_DAY: {chosen_day}\n"
                    f"NEXT_AVAILABLE_TIME: {chosen_time}\n"
                    "Say: 'The next available appointment with your "
                    f"provider is on {chosen_day} at {chosen_time}. We "
                    "are open Monday through Friday, from 9:00 AM to "
                    "5:00 PM. If you need to be seen today, I would "
                    "advise going to urgent care. Would you like to "
                    f"schedule the appointment for {chosen_day} at "
                    f"{chosen_time}?'\n"
                    "Use this exact day and time only — do NOT say "
                    "'[insert day]' or any other placeholder.\n"
                )
            else:
                same_day_virtual_clinic_context += (
                    "\nNEXT_DAY_APPOINTMENT INJECTED BY SYSTEM:\n"
                    "No specific slot could be generated this week. Let "
                    "the patient know someone will follow up with exact "
                    "timing, and say: 'If you need to be seen today, I "
                    "would advise going to urgent care.'\n"
                )
        elif patient_wants_to_decline(message_lower):
            next_day_or_urgent_care_pending = False

    # Scenario 16/17: Reschedule / Cancel Acute Visit
    acute_reschedule_confirm_was_pending = acute_reschedule_confirm_pending
    acute_cancel_confirm_was_pending = acute_cancel_confirm_pending
    acute_new_time_was_pending = acute_new_time_pending

    acute_visit_management_context = ""
    if (
            pre_chart_complete
            and caller_is_patient
            and not is_medical_professional_caller
            and not acute_reschedule_confirm_was_pending
            and not acute_cancel_confirm_was_pending
            and not acute_new_time_was_pending
    ):
        reschedule_requested = any(
            trigger in message_lower for trigger in RESCHEDULE_ACUTE_VISIT_TRIGGERS
        )
        cancel_requested = any(
            trigger in message_lower for trigger in CANCEL_ACUTE_VISIT_TRIGGERS
        )
        query_requested = any(
            trigger in message_lower for trigger in QUERY_ACUTE_VISIT_TRIGGERS
        )
        if reschedule_requested or cancel_requested or query_requested:
            if acute_existing_appt_day is None:
                appt_day, appt_time = generate_existing_acute_appointment()
                acute_existing_appt_day = appt_day
                acute_existing_appt_time = appt_time
            else:
                appt_day = acute_existing_appt_day
                appt_time = acute_existing_appt_time
            if reschedule_requested:
                new_schedule = generate_weekly_availability()
                avail_lines = []
                for day, slots in new_schedule.items():
                    if slots:
                        avail_lines.append(f"{day}: {', '.join(slots)}")
                avail_text = (
                    "; ".join(avail_lines) if avail_lines
                    else "no availability this week"
                )
                acute_new_time_pending = True
                acute_visit_management_context = (
                    "ACUTE_VISIT_RESCHEDULE_CONFIRMED INJECTED BY SYSTEM:\n"
                    "Patient has explicitly asked to reschedule their "
                    f"appointment (currently {appt_day} at {appt_time}). "
                    "This is already clear intent - do NOT ask 'would "
                    "you like to reschedule it', go straight to offering "
                    "new times.\n"
                    f"NEW AVAILABILITY: {avail_text}\n"
                    "Present this new availability and ask the patient "
                    "to pick a day and time.\n"
                )
            elif cancel_requested:
                acute_visit_management_context = (
                    "ACUTE_VISIT_CANCEL_CONFIRMED INJECTED BY SYSTEM:\n"
                    "Patient has explicitly asked to cancel their "
                    f"appointment on {appt_day} at {appt_time}. This is "
                    "already clear intent - do NOT ask 'would you like "
                    "to cancel it', confirm the cancellation directly.\n"
                    f"Say the appointment on {appt_day} at {appt_time} "
                    "has been cancelled. Ask if there is anything else "
                    "you can help with.\n"
                )
                acute_existing_appt_day = None
                acute_existing_appt_time = None
            else:
                acute_visit_management_context = (
                    "ACUTE_VISIT_QUERY INJECTED BY SYSTEM:\n"
                    f"Patient's existing scheduled acute visit is on "
                    f"{appt_day} at {appt_time}.\n"
                    "Say: 'One moment while I pull up your chart. "
                    f"(pause) I've accessed your chart, and I can see "
                    f"your upcoming appointment is on {appt_day} at "
                    f"{appt_time}.' Do NOT say you don't see an "
                    "appointment, and do NOT ask the patient for more "
                    "details about it — you already have it.\n"
                )

    if acute_reschedule_confirm_was_pending and not is_medical_professional_caller:
        if patient_wants_to_proceed(message_lower):
            acute_reschedule_confirm_pending = False
            acute_new_time_pending = True
            new_schedule = generate_weekly_availability()
            avail_lines = []
            for day, slots in new_schedule.items():
                if slots:
                    avail_lines.append(f"{day}: {', '.join(slots)}")
            avail_text = (
                "; ".join(avail_lines) if avail_lines
                else "no availability this week"
            )
            acute_visit_management_context = (
                "ACUTE_VISIT_RESCHEDULE_CONFIRMED INJECTED BY SYSTEM:\n"
                "Patient wants to reschedule their appointment "
                f"(currently {acute_existing_appt_day} at "
                f"{acute_existing_appt_time}).\n"
                f"NEW AVAILABILITY: {avail_text}\n"
                "Present this new availability and ask the patient to "
                "pick a day and time.\n"
            )
        elif patient_wants_to_decline(message_lower):
            acute_reschedule_confirm_pending = False
            acute_visit_management_context = (
                "ACUTE_VISIT_RESCHEDULE_DECLINED INJECTED BY SYSTEM:\n"
                "Patient does not want to reschedule after all. "
                "Acknowledge and leave the existing appointment "
                "unchanged.\n"
            )

    if acute_new_time_was_pending and not is_medical_professional_caller:
        acute_new_time_pending = False
        chosen_new_time = user_message.strip()
        acute_visit_management_context = (
            "ACUTE_VISIT_RESCHEDULE_FINALIZED INJECTED BY SYSTEM:\n"
            f"Patient selected: {chosen_new_time}\n"
            "Confirm the appointment has been moved from "
            f"{acute_existing_appt_day} at {acute_existing_appt_time} to "
            "the newly selected time. Say this clearly and ask if "
            "there is anything else you can help with.\n"
        )
        acute_existing_appt_day = None
        acute_existing_appt_time = None

    if acute_cancel_confirm_was_pending and not is_medical_professional_caller:
        if patient_wants_to_proceed(message_lower):
            acute_cancel_confirm_pending = False
            acute_visit_management_context = (
                "ACUTE_VISIT_CANCEL_CONFIRMED INJECTED BY SYSTEM:\n"
                f"Confirm the appointment on {acute_existing_appt_day} at "
                f"{acute_existing_appt_time} has been cancelled. Ask if "
                "there is anything else you can help with.\n"
            )
            acute_existing_appt_day = None
            acute_existing_appt_time = None
        elif patient_wants_to_decline(message_lower):
            acute_cancel_confirm_pending = False
            acute_visit_management_context = (
                "ACUTE_VISIT_CANCEL_DECLINED INJECTED BY SYSTEM:\n"
                "Patient does not want to cancel after all. Acknowledge "
                "and leave the existing appointment unchanged.\n"
            )

    lab_work_context = ""
    if lab_work_detected and not is_medical_professional_caller:
        lab_work_context = (
            "LAB_WORK_DETECTED — NEW ORDER INJECTED BY SYSTEM:\n"
            "Patient requesting NEW lab work. Full 5 step protocol:\n"
            "1. Ask reason. 2. Ask if PCP aware. 3. Note to MA.\n"
            "4. State 72 business hours once if not already stated.\n"
            "5. Document without name or DOB.\n"
        )

    lab_result_inquiry_context = ""
    if lab_result_inquiry_detected and not is_medical_professional_caller:
        results_in_chart = generate_lab_result_status()
        if results_in_chart:
            has_appointment, appt_date = generate_appointment_within_week()
            if has_appointment:
                lab_result_inquiry_context = (
                    "LAB_RESULT_INQUIRY INJECTED BY SYSTEM:\n"
                    "Patient asking if outside lab results are in chart.\n"
                    "RESULTS STATUS: IN CHART — results have been received.\n"
                    f"APPOINTMENT STATUS: Patient has appointment on {appt_date}.\n"
                    f"Tell patient: 'I can see that your results have been "
                    f"received. You have an upcoming appointment on {appt_date} "
                    "and the doctor will go over the results with you at that time.'\n"
                    "Do NOT offer to discuss results with patient.\n"
                    "Do NOT share any details of the results.\n"
                    "Do NOT mention any staff name.\n"
                    "Steve is NOT a medical professional.\n"
                )
            else:
                lab_result_inquiry_context = (
                    "LAB_RESULT_INQUIRY INJECTED BY SYSTEM:\n"
                    "Patient asking if outside lab results are in chart.\n"
                    "RESULTS STATUS: IN CHART — results have been received.\n"
                    "APPOINTMENT STATUS: No upcoming appointment within a week.\n"
                    "Tell patient: 'I can see that your results have been "
                    "received. I will put in a message for someone from our "
                    "office to call you back to go over the results with you.'\n"
                    "Do NOT offer to discuss results yourself.\n"
                    "Do NOT share any details of the results.\n"
                    "Do NOT mention any staff member name.\n"
                    "Steve is NOT a medical professional.\n"
                )
        else:
            lab_result_inquiry_context = (
                "LAB_RESULT_INQUIRY INJECTED BY SYSTEM:\n"
                "Patient asking if outside lab results are in chart.\n"
                "RESULTS STATUS: NOT IN CHART — results not yet received.\n"
                "Tell patient: 'I was not able to locate your results in "
                "the chart at this time. It is possible they have not been "
                "received yet. I will make a note of this and someone from "
                "our office will follow up. Is there anything else I can "
                "help you with today?'\n"
                "Do NOT offer to discuss results.\n"
                "Do NOT mention any staff member name.\n"
                "Steve is NOT a medical professional.\n"
            )
        # Enforce non-clinical routing: never offer to discuss lab results
        # directly as Steve. Always direct to MyChart first; if patient
        # needs help or requests discussion, route to the Medical Assistant
        # (MA) and collect a callback number. This prevents the assistant
        # from improvising clinical assistance even when HIPAA checks pass.
        lab_result_inquiry_context += (
            "CRITICAL: Steve is NOT a medical professional. Do NOT offer to "
            "discuss, interpret, or read lab results directly. Always tell "
            "the caller to check MyChart first. If the caller asks for the "
            "results to be discussed or needs help accessing MyChart, say "
            "EXACTLY: 'I'm not able to discuss medical results. I can have "
            "a medical assistant from our clinical team call you back to "
            "discuss the results. May I have a good callback number for you?' "
            "Do NOT offer to check or discuss results yourself.\n"
        )
        # One-shot: consume the flag now that the inquiry has actually been
        # answered, so it doesn't re-fire and re-randomize the chart lookup
        # result on every subsequent turn of the same call.
        lab_result_inquiry_active = False
    # Detect medication questions
    medication_inquiry_detected = any(phrase in message_lower for phrase in [
        "medication", "medicine", "prescribed", "prescription",
        "what is it for", "what is metformin", "what are the side effects",
        "how to take", "how does it work", "what does it do",
        "why did", "why was he prescribed", "why was she prescribed",
        "why is he on", "why is she on", "what does this medication",
        "metformin", "lisinopril", "atorvastatin", "amlodipine",
        "omeprazole", "levothyroxine", "sertraline", "gabapentin"
    ])

    # A caller stating a NEW-medication topic as the REASON for wanting
    # an appointment ("I want to discuss being put on weight loss
    # medication", "I'd like to start on a new medication") is not
    # asking Steve a clinical question - it's a routine appointment
    # reason and must continue through the standard scheduling workflow,
    # not be deflected to the clinical-team callback. Narrowly scoped to
    # "starting/being put on" phrasing (not broad words like "discuss"/
    # "talk about" alone, which can also appear in genuine clinical
    # questions phrased conversationally). Any interrogative wording
    # always overrides this and keeps the deflection active.
    narrow_new_med_reason = any(p in message_lower for p in [
        "being put on", "put me on", "start me on", "start on",
        "started on", "starting a", "starting on",
        "appointment to discuss", "appointment to talk about",
        "appointment to review", "talk with", "talk to",
        "speak with", "speak to",
    ])
    interrogative_override = any(p in message_lower for p in [
        "what", "why", "how", "side effect", "safe", "does it", "is it", "?"
    ])
    if narrow_new_med_reason and not interrogative_override:
        medication_inquiry_detected = False

    medication_inquiry_context = ""
    if medication_inquiry_detected and not is_medical_professional_caller:
        medication_inquiry_context = (
            "MEDICATION_INQUIRY_DETECTED INJECTED BY SYSTEM:\n"
            "Patient or caller is asking about a medication.\n"
            "CRITICAL: Steve is NOT a medical professional.\n"
            "NEVER explain what a medication is for.\n"
            "NEVER explain how a medication works.\n"
            "NEVER explain side effects of any medication.\n"
            "NEVER say things like 'Metformin is used to treat diabetes'\n"
            "or any similar medical explanation.\n"
            "The ONLY correct response is:\n"
            "'I'm not able to provide medical information about medications. "
            "I can have someone from our clinical team call you back to "
            "answer those questions. May I get a good callback number for you?'\n"
            "Get callback number.\n"
            "Do NOT mention any staff member name.\n"
            "Steve is NOT a medical professional.\n"
        )

    # Sprint 12: contagious symptom + virtual visit refusal
    # (contagious_visit_active is captured earlier, at the very top of
    # this function, so it survives any earlier-return path)
    virtual_refusal_detected = any(
        trigger in message_lower for trigger in VIRTUAL_VISIT_REFUSAL_TRIGGERS
    )

    contagious_virtual_refusal_context = ""
    if (
            contagious_visit_active
            and virtual_refusal_detected
            and not is_medical_professional_caller
    ):
        callin_decision = get_provider_callin_decision()
        contagious_virtual_refusal_context = (
            "CONTAGIOUS_VIRTUAL_REFUSAL INJECTED BY SYSTEM:\n"
            "Patient has a contagious-type complaint and is pushing back "
            "on / refusing a virtual visit.\n"
            f"PROVIDER_CALLIN_DECISION: {callin_decision}\n"
            "Complete in ONE response — checking with the MA, then the "
            "outcome, then (if AGREES) the follow-up question, all in "
            "the SAME message. Do NOT stop after saying you'll check "
            "with the medical assistant — that is not a real hold, you "
            "already have PROVIDER_CALLIN_DECISION above, so continue "
            "immediately in this same response.\n"
            "Say you will check with the medical assistant to see if the "
            "provider is willing to call something in instead of an "
            "office visit. (pause) Then immediately continue in the "
            "SAME response with the outcome below.\n"
            "If PROVIDER_CALLIN_DECISION is AGREES: tell the patient the "
            "provider is willing to call something in, and ask if that "
            "works for them INSTEAD of an office visit. STOP and wait "
            "for their answer to THIS question — do NOT decide this "
            "yourself based on how strongly they earlier refused the "
            "virtual visit.\n"
            "If PROVIDER_CALLIN_DECISION is DECLINES: tell the patient "
            "the provider would like to see them for this, then say "
            "EXACTLY: 'For the protection of staff and patients we ask "
            "that you bring a face covering.'\n"
            "Do NOT offer a virtual visit again after this point in the "
            "call.\n"
        )
        virtual_visit_offered = False
        provider_callin_offer_pending = (callin_decision == "AGREES")
    elif virtual_visit_accepted_now and not contagious_same_day_check_just_answered:
        # Bug fix: previously jumped straight to presenting FUTURE
        # virtual availability the moment the patient accepted a
        # virtual visit, with no check for same-day urgency. Ask the
        # missing decision-point question instead; contagious_same_day_
        # check_pending (handled near is_same_day above) picks up the
        # patient's answer on the next turn. The "not
        # contagious_same_day_check_just_answered" guard prevents this
        # from re-firing on that very next turn - a patient answering
        # "Yes" to the new question can otherwise also satisfy
        # virtual_visit_accepted_now's own (separate) detection and
        # re-ask the same question instead of proceeding.
        contagious_same_day_check_pending = True
        contagious_virtual_refusal_context = (
            "VIRTUAL_VISIT_ACCEPTED INJECTED BY SYSTEM:\n"
            "The patient just agreed to a virtual visit for their "
            "contagious complaint. Before presenting any availability, "
            "ask EXACTLY: 'Do you need to be seen today, or would a "
            "future virtual visit work for you?' Do NOT present any "
            "times or say you will check with the medical assistant "
            "yet - wait for their answer to this question first.\n"
        )
        virtual_visit_offered = False
    elif contagious_present_future_availability:
        contagious_virtual_refusal_context = (
            "VIRTUAL_VISIT_ACCEPTED INJECTED BY SYSTEM:\n"
            "The patient just agreed to a virtual visit for their "
            "contagious complaint. Present the available appointment "
            "times now from the WEEKLY AVAILABILITY injection below. Do "
            "NOT say you will check with the medical assistant — present "
            "the times directly.\n"
        )
        virtual_visit_offered = False
    elif provider_callin_accepted_now:
        provider_callin_offer_pending = False
        symptoms_pharmacy_pending = True
        contagious_virtual_refusal_context = (
            "PROVIDER_CALLIN_ACCEPTED INJECTED BY SYSTEM:\n"
            "The patient just agreed to have the provider call in a "
            "medication instead of an office visit.\n"
            "Ask BOTH what symptoms they are experiencing AND what "
            "pharmacy they use. STOP and wait for their answer to these "
            "questions — do NOT skip straight to concluding the call or "
            "saying someone will follow up.\n"
        )
    elif symptoms_pharmacy_pending:
        symptoms_pharmacy_pending = False
        ma_name_for_callin, _ = get_ma_for_patient()
        ma_display_name = ma_name_for_callin or "the medical assistant"
        contagious_virtual_refusal_context = (
            "SYMPTOMS_PHARMACY_PROVIDED INJECTED BY SYSTEM:\n"
            "The patient just answered the symptoms/pharmacy questions.\n"
            f"Say EXACTLY: 'I've made a note of your symptoms and "
            f"pharmacy information in a high priority message sent to "
            f"{ma_display_name}, who will forward this to your doctor. "
            "Although your doctor will send in a script to the pharmacy "
            "as soon as possible, this message can take up to 24 "
            "business hours to process. Is there anything else I can "
            "help you with today?'\n"
            "Do NOT hedge or contradict the earlier PROVIDER_CALLIN_"
            "DECISION — you already told the patient the provider is "
            "willing to call something in, so do NOT say 'if the "
            "provider decides to' here. Do NOT say 72 business hours "
            "here — this specific message is 24 business hours.\n"
        )
    elif provider_callin_declined_now:
        provider_callin_offer_pending = False
        contagious_virtual_refusal_context = (
            "PROVIDER_CALLIN_DECLINED INJECTED BY SYSTEM:\n"
            "The patient does not want the provider to call something "
            "in, and still wants to be seen in the office.\n"
            "Say EXACTLY: 'For the protection of staff and patients we "
            "ask that you bring a face covering.'\n"
        )
    elif (
            contagious_visit_active
            and not virtual_visit_offered
            and not provider_callin_offer_pending
            and not is_medical_professional_caller
    ):
        contagious_virtual_refusal_context = (
            "CONTAGIOUS_OFFER_VIRTUAL INJECTED BY SYSTEM:\n"
            "Patient has a contagious-type complaint (e.g. pink eye, flu, "
            "strep, covid). Before discussing same-day or future "
            "in-office scheduling, offer a virtual visit FIRST.\n"
            "Ask: 'Would you be willing to do a virtual visit?'\n"
            "Only proceed with in-office scheduling if the patient "
            "declines the virtual visit.\n"
        )
        virtual_visit_offered = True

    # Sprint 12: UTI + demanding antibiotic without being seen
    if not is_medical_professional_caller and (
            any(trigger in message_lower for trigger in UTI_TRIGGERS)
            and any(trigger in message_lower for trigger in ANTIBIOTIC_DEMAND_TRIGGERS)
    ):
        uti_antibiotic_demand_active = True

    uti_antibiotic_demand_context = ""
    if uti_antibiotic_demand_active and not is_medical_professional_caller:
        uti_antibiotic_demand_context = (
            "UTI_ANTIBIOTIC_DEMAND INJECTED BY SYSTEM:\n"
            "Patient has UTI-type symptoms and is asking for an "
            "antibiotic to be called in without being seen.\n"
            "Tell the patient an appointment is needed first so the "
            "provider can assess them. The provider will order a "
            "urinalysis to determine the strain, and depending on those "
            "results will then decide on an antibiotic.\n"
            "Do NOT promise an antibiotic will be called in without an "
            "appointment.\n"
            "If the patient agrees to schedule, continue with normal "
            "appointment scheduling.\n"
            "If the patient refuses to schedule an appointment: ask "
            "what symptoms they are experiencing so this can be noted "
            "for the provider (attempt this even if the patient may "
            "decline to answer), ask what pharmacy they use in case the "
            "provider decides to call something in, get a good callback "
            "number, and tell them this will be noted as a HIGH "
            "PRIORITY message for the provider.\n"
        )

    # referral_lookup_context intentionally left as an empty string.
    # The referral lookup now returns its response directly from Python
    # at the moment of detection (see the early return above) and the
    # NOT FOUND + callback number follow-up is also handled directly
    # from Python via the phone_number_pattern intercept above.
    # This injection used to re-fire on EVERY subsequent message once
    # referral_lookup_done became True, leaking stale specialist names
    # into unrelated later turns of the same call. Removing it entirely
    # is the fix — no injection is needed here anymore.
    referral_lookup_context = ""

    # Nurse-visit context: deterministic injection that keeps Steve on
    # the correct in-office / MA-scheduled path for the entire call once
    # a nurse-visit request is detected, including the follow-up turn
    # where the patient provides a contact number.
    nurse_visit_context = ""
    if nurse_visit_active and not is_medical_professional_caller:
        nurse_visit_context = (
            "NURSE_VISIT_REQUEST INJECTED BY SYSTEM:\n"
            "The patient is requesting a NURSE VISIT (in-office services "
            "such as injections, B12/flu shots, vaccines, etc.).\n"
            "- NURSE VISITS ARE ALWAYS PERFORMED IN OFFICE. Never ask "
            "about a virtual visit or a virtual-versus-in-office preference "
            "for a nurse visit.\n"
            "- YOU DO NOT SCHEDULE NURSE VISITS. The medical assistant "
            "schedules all nurse visits.\n"
            "- Tell the patient the medical assistant will need to schedule "
            "the nurse visit, and that you will put in a note for the "
            "medical assistant.\n"
            "- Then ask: 'May I get a good contact number for you?'\n"
            "- Do NOT check or present appointment availability, do NOT "
            "offer any times, and do NOT book anything yourself.\n"
            "- Once a contact number has been provided: confirm the note "
            "has been put in for the medical assistant and say: 'Is there "
            "anything else I can help you with today?'\n"
        )

    lab_result_fax_outside_context = ""
    if (lab_result_fax_active or lab_result_fax_to_outside_detected) and not is_medical_professional_caller:
        already_has_fax_info_outside = bool(re.search(
            r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b', message_lower
        )) or detect_address_in_message(message_lower)
        lab_result_fax_outside_context = (
            "LAB_RESULT_FAX_TO_OUTSIDE INJECTED BY SYSTEM:\n"
            "Patient is requesting lab results be faxed to an outside provider.\n"
            "The patient has explicitly requested a fax.\n"
            "CRITICAL: Patient info is already collected — do NOT ask for name again.\n"
            "CRITICAL: A doctor name, specialist name, or 'hematologist' is "
            "NOT a fax number and is NOT an address. Mentioning a provider's "
            "name does NOT mean you have their contact information yet.\n"
            f"Fax number or address already provided this message: "
            f"{already_has_fax_info_outside}\n"
            "If fax number or address NOT yet provided, say EXACTLY this "
            "and nothing else: 'Can you please provide me with a fax "
            "number or the address so that I can look up the fax "
            "number?'\n"
            "Do NOT say 'using the address you've provided' or 'I've got "
            "the address' or anything implying you already possess "
            "contact information you have not actually been given.\n"
            "Do NOT ask if they want fax or pickup — they already said fax.\n"
            "Do NOT ask for patient name or date of birth — already collected.\n"
            "Once fax number or address IS actually provided: confirm you "
            "will send the request to have results faxed.\n"
            "STEP — State 72 business hours once if not already stated.\n"
            "Do NOT share any results with the patient directly.\n"
        )

    # Scenario 11/12 fix: the CONTROLLED_SUBSTANCE_INFO injection below
    # and the CONTROLLED SUBSTANCES system-prompt section were both
    # designed around a BARE refill request ("can I get a refill of my
    # Xanax") where no appointment was ever mentioned - the last-visit
    # window determines whether an appointment becomes necessary. When
    # the patient has ALREADY explicitly asked for an appointment (with
    # the controlled substance as the stated reason, e.g. after a
    # wellness-flow medication pivot), that window-check framing is
    # backwards: it produces "you are required to see [PCP] before we
    # can process any refill requests" for a patient who never asked
    # for a refill without an appointment in the first place. Same
    # principle already applied to paperwork above - an explicit
    # appointment request is never re-litigated through workflow-
    # specific escalation logic built for the case where no
    # appointment was requested.
    patient_explicitly_requested_appointment_for_med = any(
        phrase in message_lower for phrase in [
            "schedule an appointment", "need an appointment",
            "book an appointment", "want an appointment",
            "get an appointment", "make an appointment",
            "need to schedule", "want to schedule", "like to schedule",
        ]
    )

    controlled_substance_context = ""
    if controlled_substance_schedule and not is_medical_professional_caller:
        if patient_explicitly_requested_appointment_for_med:
            controlled_substance_appt_pending = True
            controlled_substance_appt_medication_word = (
                    find_controlled_substance_word(message_lower) or "your medication"
            )
            controlled_substance_appt_schedule = controlled_substance_schedule
            controlled_substance_context = (
                "CONTROLLED_SUBSTANCE_APPOINTMENT_REQUESTED INJECTED BY "
                "SYSTEM:\n"
                "The patient has already explicitly asked for an "
                "appointment, with this controlled substance as the "
                "stated reason. Do NOT say anything about being "
                "'required' to be seen before refill requests can be "
                "processed, and do NOT apply the CONTROLLED SUBSTANCES "
                "last-visit-window logic - that logic is for a bare "
                "refill request with no appointment mentioned, which is "
                "not what happened here. Treat this exactly like "
                "APPOINTMENT SCHEDULING — FUTURE: offer availability "
                "directly and proceed to scheduling.\n"
                "MEDICATION_BRIDGE_FLOW_ACTIVE - CRITICAL: Once the "
                "appointment is booked, confirm it and STOP - do not "
                "ask about dosage, pharmacy, days of medication "
                "remaining, or bridge-request details in this same "
                "response or any response of your own. That entire "
                "collection is handled deterministically by Python "
                "immediately after booking - it will ask its own "
                "follow-up question directly. Do NOT pre-empt it, do "
                "NOT combine it with your booking confirmation, and do "
                "NOT mention 72 business hours for this request.\n"
            )
        else:
            last_visit_str, needs_appointment, days_since, over_12_months, window = \
                generate_last_visit_date(controlled_substance_schedule)
            if controlled_substance_schedule == 1:
                schedule_name = "Schedule 1"
                meds_example = "Oxycodone, Percocet, Morphine, Fentanyl"
                virtual_option = "IN OFFICE ONLY — never offer virtual"
            elif controlled_substance_schedule == 2:
                schedule_name = "Schedule 2"
                meds_example = "Vyvanse, Adderall, Focalin, Sunosi"
                virtual_option = "Virtual OR in-office — patient's choice"
            else:
                schedule_name = "Schedule 3"
                meds_example = "Xanax, Ativan, Zolpidem, Klonopin, etc."
                virtual_option = "Virtual OR in-office — patient's choice"
            if over_12_months:
                appt_reason = "over 12 months — appointment required"
            elif needs_appointment:
                appt_reason = (
                    f"{days_since} days exceeds {window}-day window "
                    f"— appointment required"
                )
            else:
                appt_reason = (
                    f"{days_since} days within {window}-day window "
                    f"— NO appointment needed"
                )
            controlled_substance_context = (
                f"CONTROLLED_SUBSTANCE_INFO INJECTED BY SYSTEM:\n"
                f"Schedule: {schedule_name} ({meds_example})\n"
                f"Last visit: {last_visit_str} ({days_since} days ago)\n"
                f"Window: {window} days\n"
                f"Appointment needed: "
                f"{'YES' if needs_appointment else 'NO — DO NOT SUGGEST ONE'}\n"
                f"Status: {appt_reason}\n"
                f"Virtual if needed: {virtual_option}\n"
                f"ALWAYS state last visit date first.\n"
                f"IF NO appointment needed: collect refill info only. "
                f"Do NOT mention appointments at all.\n"
            )
            if needs_appointment:
                # Root cause fix (Bug #1/#2/#4): this branch determined
                # an appointment was required from the last-visit window
                # (patient never explicitly asked for one - e.g. a bare
                # "I need a refill" request) but never set
                # controlled_substance_appt_pending or told the LLM to
                # stop after booking. Only the explicit-request branch
                # above did that, so once the LLM booked this appointment
                # on its own initiative, the deterministic bridge
                # follow-up (day-count -> dosage -> pharmacy -> callback
                # -> bridge message, with its correct 24-business-hour
                # wording) never engaged, and the LLM freelanced its own
                # incomplete version instead. Mirrors the explicit-
                # request branch's pending flag and STOP instruction so
                # both paths hand off to the same deterministic flow.
                controlled_substance_appt_pending = True
                controlled_substance_appt_medication_word = (
                        find_controlled_substance_word(message_lower) or "your medication"
                )
                controlled_substance_appt_schedule = controlled_substance_schedule
                controlled_substance_context += (
                    "MEDICATION_BRIDGE_FLOW_ACTIVE - CRITICAL: Once the "
                    "appointment is booked, confirm it and STOP - do not "
                    "ask about dosage, pharmacy, days of medication "
                    "remaining, or bridge-request details in this same "
                    "response or any response of your own. That entire "
                    "collection is handled deterministically by Python "
                    "immediately after booking - it will ask its own "
                    "follow-up question directly. Do NOT pre-empt it, do "
                    "NOT combine it with your booking confirmation, and "
                    "do NOT mention 72 business hours for this "
                    "request.\n"
                )

    hipaa_context = ""
    if (third_party_detected and dob_collected and
            pcp_collected and not is_medical_professional_caller
            and not caller_is_patient):
        hipaa_status = get_hipaa_status()
        if hipaa_status == "ON_HIPAA":
            hipaa_instruction = (
                "Say: 'I was able to verify that you are listed on the "
                "patient's HIPAA authorization. I will be happy to "
                "assist you today.' Then ask for PCP if not collected.\n"
                # Bug fix: a caller verified ON the patient's HIPAA form is
                # authorized to act on the patient's behalf, but the static
                # SITUATION B text ("I would recommend having [patient name]
                # call us directly") is unconditional in the system prompt,
                # so the model kept applying that patient-must-call rule to
                # authorized callers and refused to schedule. Provide an
                # explicit deterministic override for the authorized path,
                # exactly like MINOR_GUARDIAN_CONFIRMED does for guardians -
                # it only ADDS scheduling authorization for ON-HIPAA callers
                # and never touches the not-on-HIPAA/SITUATION B flows.
                "THIRD PARTY AUTHORIZED (ON HIPAA) INJECTED BY SYSTEM:\n"
                "This caller is listed on the patient's HIPAA form and is "
                "authorized to act on the patient's behalf. If they ask to "
                "schedule or book an appointment for the patient, proceed "
                "with scheduling normally EXACTLY as for the patient "
                "themselves: ask for the reason for the visit only if none "
                "has been stated, then present the injected weekly "
                "availability and continue to booking. Do NOT tell them to "
                "have the patient call us directly, do NOT say the patient "
                "must call back themselves, and do NOT apply the SITUATION "
                "B patient-must-call rule to a caller who is verified on "
                "HIPAA. The HIPAA status is already verified for this call "
                "- do NOT re-run or re-ask HIPAA verification.\n"
            )
        else:
            hipaa_instruction = (
                "Say EXACTLY: 'I'm sorry but I was not able to verify "
                "that you are listed on the patient's HIPAA authorization.' "
                "Then ask EXACTLY: 'Is the patient available right now so "
                "I can ask for their consent?' "
                "CRITICAL: Do NOT ask the caller for the patient's consent. "
                "Do NOT say anything like 'Do I have your consent to give "
                "you information' or 'Do I have the patient's consent to "
                "give you, [caller name], information' — these are WRONG, "
                "the caller cannot consent on the patient's behalf. "
                "Only the patient themselves can give consent, and only "
                "once they are on the phone directly."
            )
        hipaa_context = (
            f"HIPAA: {hipaa_status}\n"
            f"Complete in ONE response — chart pull then status.\n"
            f"{hipaa_instruction}\n"
        )

    pcp_context = ""
    if pcp_collected:
        pcp_context = "PCP_ALREADY_COLLECTED: Do NOT ask for PCP again.\n"

    # Sprint 15 (established patients): once the parent/guardian of an
    # under-18 existing patient has come on the line and identified
    # themselves, the guardian is authorized to schedule on the patient's
    # behalf. Without this signal the AI can misapply the third-party /
    # under-18 rules to the parent's own scheduling request and refuse
    # ("have the patient call us directly") or re-ask settled questions.
    # The static prompt already tells it to ask the reason for a future
    # appointment when none is stated; this only removes the guardian
    # ambiguity so that instruction fires reliably.
    established_guardian_context = ""
    if established_patient_minor_guardian_confirmed and patient_first_name:
        established_guardian_context = (
            "MINOR_GUARDIAN_CONFIRMED INJECTED BY SYSTEM:\n"
            f"The patient, {patient_first_name}, is under 18 and a parent "
            "or guardian is on the line with them right now and has "
            "identified themselves. The guardian is authorized to schedule "
            "on the patient's behalf. Proceed with scheduling normally: do "
            "NOT ask the patient to call back, do NOT say the patient must "
            "call directly, do NOT run HIPAA verification, do NOT ask for "
            "the guardian again, and do NOT ask which provider (the "
            "patient's PCP is already on file). If no reason for the visit "
            "has been stated, ask exactly: \"What is the reason for the "
            "appointment?\"\n"
        )

    individual_six_month_followup_context = ""
    if individual_six_month_followup_active:
        due_date = (
            datetime.now() - timedelta(
                days=individual_six_month_followup_days_since
            ) + timedelta(days=180)
        ).strftime("%B %d, %Y")
        # Holiday-closure fix: this flow has no deterministic
        # availability generator (unlike the couple workflow) - the AI
        # invents its own calendar dates, which let it offer a
        # recognized office holiday (e.g. Friday, January 01, 2027 =
        # New Year's Day). Enumerate the recognized holiday closures
        # inside the scheduling window and forbid them explicitly,
        # reusing the same holiday calendar as the rest of the app.
        _followup_window_end = datetime.now().date() + timedelta(days=200)
        _holiday_closure_block = ""
        _closed_holiday_dates = sorted(
            (d, n) for d, n in
            _office_holidays_near(_followup_window_end.year).items()
            if datetime.now().date() < d <= _followup_window_end
        )
        if _closed_holiday_dates:
            _holiday_closure_block = (
                "RECOGNIZED OFFICE HOLIDAY CLOSURES (office CLOSED, do NOT "
                "offer, confirm, or mention an appointment on any of): "
                + "; ".join(
                    f"{d.strftime('%B %d, %Y')} {n}"
                    for d, n in _closed_holiday_dates
                ) + ".\n"
            )
        if individual_six_month_followup_eligible:
            individual_six_month_followup_context = (
                "INDIVIDUAL_SIX_MONTH_FOLLOWUP INJECTED BY SYSTEM:\n"
                f"Previous appointment date: "
                f"{individual_six_month_followup_previous_visit_date} "
                f"({individual_six_month_followup_days_since} days ago).\n"
                "Say concisely that the patient is due for a routine "
                "six-month follow-up, then offer availability. Use this "
                "same date consistently for the rest of the call.\n"
                "The patient has already stated the reason for this "
                "appointment (the routine six-month follow-up). Do NOT "
                "ask for the appointment reason.\n"
                f"{_holiday_closure_block}"
            )
        else:
            _due_dt = (
                datetime.now() - timedelta(
                    days=individual_six_month_followup_days_since
                ) + timedelta(days=180)
            ).date()
            _post_due_business_days = []
            _cursor = _due_dt
            while len(_post_due_business_days) < 5:
                if (
                        _cursor.weekday() < 5
                        and not is_recognized_office_holiday(_cursor)
                ):
                    _post_due_business_days.append(
                        _cursor.strftime("%A, %B %d, %Y")
                    )
                _cursor += timedelta(days=1)
            _post_due_dates_text = "; ".join(_post_due_business_days)
            individual_six_month_followup_context = (
                "INDIVIDUAL_SIX_MONTH_FOLLOWUP INJECTED BY SYSTEM:\n"
                f"Previous appointment date: "
                f"{individual_six_month_followup_previous_visit_date} "
                f"({individual_six_month_followup_days_since} days ago).\n"
                f"The patient is not due until {due_date}. Do NOT offer "
                "an appointment before that date; offer a routine "
                "follow-up on or after it. Offer ONLY the following "
                "exact dates (do NOT offer, mention, or invent any "
                f"date earlier than {due_date} or outside this list):\n"
                f"- {_post_due_dates_text}\n"
                "Use ONLY dates from this list, consistently, for the "
                "rest of the call.\n"
                "The patient has already stated the reason for this "
                "appointment (the routine six-month follow-up). Do NOT "
                "ask for the appointment reason.\n"
                f"{_holiday_closure_block}"
            )

    individual_three_month_followup_context = ""
    if individual_three_month_followup_active:
        _three_month_due = (
            datetime.now() - timedelta(
                days=individual_three_month_followup_days_since
            ) + timedelta(days=90)
        ).date()
        three_month_due_text = _three_month_due.strftime("%B %d, %Y")
        _holiday_closure_block = ""
        _closed_holiday_dates = sorted(
            (d, n) for d, n in
            _office_holidays_near(_three_month_due.year).items()
            if datetime.now().date() < d <= datetime.now().date() + timedelta(days=200)
        )
        if _closed_holiday_dates:
            _holiday_closure_block = (
                "RECOGNIZED OFFICE HOLIDAY CLOSURES (office CLOSED, do NOT "
                "offer, confirm, or mention an appointment on any of): "
                + "; ".join(
                    f"{d.strftime('%B %d, %Y')} {n}"
                    for d, n in _closed_holiday_dates
                ) + ".\n"
            )
        _earliest = max(
            _three_month_due, (datetime.now() + timedelta(days=1)).date()
        )
        _post_due_business_days = []
        _cursor = _earliest
        while len(_post_due_business_days) < 5:
            if (
                    _cursor.weekday() < 5
                    and not is_recognized_office_holiday(_cursor)
            ):
                _post_due_business_days.append(
                    _cursor.strftime("%A, %B %d, %Y")
                )
            _cursor += timedelta(days=1)
        _post_due_dates_text = "; ".join(_post_due_business_days)
        individual_three_month_followup_context = (
            "INDIVIDUAL_THREE_MONTH_FOLLOWUP INJECTED BY SYSTEM:\n"
            f"Previous appointment date: "
            f"{individual_three_month_followup_previous_visit_date} "
            f"({individual_three_month_followup_days_since} days ago).\n"
            f"The patient is due for a routine 3-month follow-up on or "
            f"after {three_month_due_text}. Offer ONLY the following "
            "exact dates (do NOT offer, mention, or invent any date "
            f"earlier than {three_month_due_text} or outside this list):\n"
            f"- {_post_due_dates_text}\n"
            "Use ONLY dates from this list, consistently, for the rest "
            "of the call.\n"
            "Also state the patient's previous appointment date "
            f"({individual_three_month_followup_previous_visit_date}) in "
            "your availability response and use it consistently.\n"
            "In that same availability response, explicitly list the "
            "offered dates above so the patient can choose.\n"
            "The patient has already stated the reason for this "
            "appointment (the routine 3-month follow-up). Do NOT ask "
            "for the appointment reason.\n"
            f"{_holiday_closure_block}"
        )

    conversation_history.append({"role": "user", "content": user_message})

    phf_context = Sprint13.build_context()
    wellness_context = Sprint14.build_context()

    system_with_context = (
            system_prompt + "\n" +
            pre_chart_context + "\n" +
            response_time_context + "\n" +
            office_hours_context + "\n" +
            todays_date_context + "\n" +
            medical_professional_context + "\n" +
            pcp_context + "\n" +
            established_guardian_context + "\n" +
            individual_six_month_followup_context + "\n" +
            individual_three_month_followup_context + "\n" +
            nurse_ma_context + "\n" +
            nurse_visit_context + "\n" +
            lab_order_fax_to_facility_context + "\n" +
            lab_order_pickup_context + "\n" +
            patient_presence_context + "\n" +
            lab_work_context + "\n" +
            lab_result_inquiry_context + "\n" +
            lab_result_fax_outside_context + "\n" +
            referral_lookup_context + "\n" +
            medication_inquiry_context + "\n" +
            contagious_virtual_refusal_context + "\n" +
            uti_antibiotic_demand_context + "\n" +
            same_day_context + "\n" +
            covering_provider_context + "\n" +
            same_day_virtual_clinic_context + "\n" +
            acute_visit_management_context + "\n" +
            availability_context + "\n" +
            controlled_substance_context + "\n" +
            urgent_context + "\n" +
            hipaa_context + "\n" +
            phf_context + "\n" +
            wellness_context
    )

    messages = [{"role": "system", "content": system_with_context}] + \
               conversation_history

    try:
        response = groq_create_completion(
            model="qwen/qwen3.8-27b",
            messages=messages,
            reasoning_effort="none",
            max_completion_tokens=900
        )
        first_choice = response.choices[0]
        assistant_message = first_choice.message.content or ""
        # Root cause fix: the completion above is hard-capped at
        # max_completion_tokens=900. When the model exhausts that budget
        # Groq returns finish_reason="length" with the reply cut off
        # mid-sentence, and it was previously served to the patient
        # verbatim (e.g. "...Is there anything else I" with no ending).
        # Never expose a dangling fragment - trim to the last complete
        # sentence so the reply always reads cleanly.
        if getattr(first_choice, "finish_reason", None) == "length":
            trimmed = _trim_to_complete_sentence(assistant_message)
            print(
                "[chat] completion truncated (finish_reason=length); "
                f"reply trimmed from {len(assistant_message)} to {len(trimmed)} chars"
            )
            assistant_message = trimmed
        conversation_history.append(
            {"role": "assistant", "content": assistant_message}
        )
        if check_response_time_stated(assistant_message):
            response_time_stated = True
        if check_office_hours_stated(assistant_message):
            office_hours_stated = True
        if (
                pre_chart_complete and not is_medical_professional_caller
                and not Sprint13.phf_flow_active
        ):
            confirmed_generic_appt = _extract_generic_appointment_confirmation(
                assistant_message
            )
            if confirmed_generic_appt:
                capture_first = patient_first_name or caller_first_name
                capture_last = patient_last_name or caller_last_name
                store_generic_appointment_record(
                    capture_first, capture_last, confirmed_generic_appt,
                    reason=_derive_generic_appointment_reason(),
                )
                individual_six_month_followup_active = False
                individual_three_month_followup_active = False
                # Scenario 11/12: if this booking was for a
                # controlled-substance-reason appointment, append a
                # deterministic Python-authored bridge question rather
                # than relying on the LLM to remember to ask it - the
                # bridge-need decision itself is a plain day-count
                # comparison, not something requiring model judgment,
                # and this same appointment path has already needed
                # multiple prompt-compliance corrections this session.
                if controlled_substance_appt_pending:
                    day_match = re.search(
                        r"\b(monday|tuesday|wednesday|thursday|friday|"
                        r"saturday|sunday)\b",
                        confirmed_generic_appt, re.IGNORECASE
                    )
                    controlled_substance_appt_day_name = (
                        day_match.group(1).capitalize() if day_match else None
                    )
                    # Bug fix: capture the actual confirmed calendar
                    # date too (not just the weekday name), so the
                    # bridge day-math below can use an exact day count
                    # instead of a weekday-name guess that silently
                    # breaks for anything booked more than 6 days out.
                    month_match = re.search(
                        r"\b(january|february|march|april|may|june|july|"
                        r"august|september|october|november|december)\s+"
                        r"(\d{1,2})\b",
                        assistant_message, re.IGNORECASE
                    )
                    controlled_substance_appt_date = None
                    if month_match:
                        _month_num = _MONTH_NAME_TO_NUM[month_match.group(1).lower()]
                        _day_num = int(month_match.group(2))
                        _today = datetime.now()
                        try:
                            _candidate = datetime(_today.year, _month_num, _day_num).date()
                            if _candidate < _today.date() - timedelta(days=60):
                                _candidate = datetime(_today.year + 1, _month_num, _day_num).date()
                            controlled_substance_appt_date = _candidate
                        except ValueError:
                            controlled_substance_appt_date = None
                    med_word = controlled_substance_appt_medication_word or "your medication"
                    # Bug fix: MEDICATION_BRIDGE_FLOW_ACTIVE tells the LLM
                    # to stop right after confirming the booking, but it
                    # does not always comply - it has been observed
                    # tacking on further freelanced sentences in the same
                    # response (a backwards "we'll call you" callback
                    # claim, a forbidden "72 business hours" mention, a
                    # premature "anything else?"). Simply appending the
                    # deterministic question after that leaves it stuck
                    # at the end, after the freelanced closing, producing
                    # a garbled close-then-reopen order. Instead, cut the
                    # response back to the end of the sentence that
                    # actually confirms the day/time, discarding
                    # anything the LLM added after it, then append the
                    # deterministic question directly after the
                    # confirmation.
                    confirm_time_match = re.search(
                        r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
                        assistant_message, re.IGNORECASE
                    )
                    if confirm_time_match:
                        cutoff = _find_real_sentence_end(
                            assistant_message, confirm_time_match.end()
                        )
                        if cutoff is not None:
                            assistant_message = assistant_message[:cutoff].rstrip()
                    assistant_message = (
                            assistant_message.rstrip()
                            + f"\n\nDo you have enough {med_word} remaining "
                            + "to hold you over until your appointment?"
                    )
                    conversation_history[-1]["content"] = assistant_message
                    controlled_substance_bridge_awaiting_days = True
                    controlled_substance_appt_pending = False
                    controlled_substance_appt_medication_word = None
                    controlled_substance_appt_schedule = None
        return jsonify({"response": assistant_message})
    except Exception as e:
        print(f"Groq API error: {e}")
        return jsonify({"response": f"Error: {str(e)}"})


if __name__ == "__main__":
    app.run(debug=True)
