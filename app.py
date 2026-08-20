import os

#os.environ["STEVE_FORCE_PCP_AVAILABLE"] = "false"
import re
import random
import secrets
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from groq import Groq
import Sprint13

os.environ["GROQ_API_KEY"] = "Withheldforprotection"

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
client = Groq(api_key=os.environ["GROQ_API_KEY"])

conversation_history = []
profanity_count = 0
hipaa_status_determined = False
current_hipaa_status = None
third_party_availability_asked = False
third_party_detected = False
dob_collected = False
patient_self_dob_verified = False
pcp_collected = False
verbal_consent_requested = False
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

# New patient workflow state (Sprint 11)
new_patient_flow_active = False
new_patient_requested_provider = None
new_patient_is_minor = None
new_patient_minor_age = None
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

# Urgent symptoms state
urgent_symptoms_active = False
urgent_can_wait_asked = False
ma_availability_determined = False
current_ma_availability = None

# Sprint 12: Acute visit nuances - contagious/virtual-refusal and UTI
contagious_visit_active = False
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

# Before office hours acute contagious state
before_hours_acute_active = False
before_hours_virtual_offered = False
before_hours_pcp_message_pending = False

# Scenario 16/17: Reschedule / Cancel Acute Visit
acute_existing_appt_day = None
acute_existing_appt_time = None
acute_reschedule_confirm_pending = False
acute_cancel_confirm_pending = False
acute_new_time_pending = False

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
    "it cannot wait", "it can't wait", "help now",
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


def is_office_open_today():
    return datetime.now().weekday() < 5


def is_within_office_hours():
    """True only if it is currently within actual office hours: Monday
    through Friday, 9:00 AM to 5:00 PM. Distinct from
    is_office_open_today(), which only checks the day of week and
    ignores time of day entirely - that gap is why a 5:17 AM weekday
    call was treated as if the office were open."""
    now = datetime.now()
    if now.weekday() >= 5:
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


def store_generic_appointment_record(first_name, last_name, day_time_text, provider=None):
    """Persist a confirmed generic appointment's day/time (and provider,
    if known) under the patient's identity."""
    key = _generic_patient_record_key(first_name, last_name)
    if not key:
        return
    generic_appointment_records[key] = {
        "appointment_day": day_time_text,
        "provider": provider,
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


_GENERIC_APPT_DAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
]
_GENERIC_APPT_CONFIRM_KEYWORDS = [
    "scheduled", "confirmed", "booked", "got you down",
    "appointment is set", "you're all set", "all set for",
]


def _extract_generic_appointment_confirmation(assistant_text):
    """Best-effort extraction of a confirmed generic appointment's
    day/time from the AI's own free-form response text. Requires both
    a day name AND a time AND at least one confirmation-indicating
    phrase, to avoid false-positives on messages that merely mention a
    day/time in passing (e.g. listing availability options, not yet
    confirming one)."""
    text_lower = assistant_text.lower()
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


GENERIC_APPOINTMENT_INQUIRY_TRIGGERS = [
    "when is my next appointment", "when is my appointment",
    "do i have an appointment scheduled", "do i have an appointment",
    "when am i scheduled to see", "when am i scheduled",
    "what time is my appointment", "check my appointment",
]


def detect_generic_appointment_inquiry_intent(message_lower):
    return any(t in message_lower for t in GENERIC_APPOINTMENT_INQUIRY_TRIGGERS)


def get_next_monday():
    today = datetime.now()
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    next_monday = today + timedelta(days=days_until_monday)
    return next_monday.strftime("%A, %B %d")


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

# Scenario 16/17: Reschedule / Cancel Acute Visit
RESCHEDULE_ACUTE_VISIT_TRIGGERS = [
    "reschedule my appointment", "reschedule the appointment",
    "reschedule my visit", "need to reschedule", "want to reschedule",
    "change my appointment", "move my appointment",
]

CANCEL_ACUTE_VISIT_TRIGGERS = [
    "cancel my appointment", "cancel the appointment",
    "cancel my visit", "need to cancel my appointment",
    "want to cancel my appointment", "cancel my acute visit",
]

QUERY_ACUTE_VISIT_TRIGGERS = [
    "when is my appointment", "when is my acute visit",
    "when my acute visit is", "what is my appointment",
    "what's my appointment", "see my appointment",
    "check my appointment", "do i have an appointment",
    "when is my visit", "what time is my appointment",
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
    "flu-like", "fever", "cough", "congestion", "body aches", "sore throat",
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
    "florida blue", "anthem"
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

MEDICAID_TRIGGERS = [
    "medicaid", "medi-caid", "medi caid"
]

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
    dob_patterns = [
        "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"
    ]
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
        phone = f"321-{random.randint(100,999)}-{random.randint(1000,9999)}"
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
        med_pro_collection_complete = True # Mark complete even if not found to allow follow up
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

def handle_new_patient_flow(message, message_lower):
    global new_patient_flow_active, new_patient_requested_provider
    global new_patient_is_minor, new_patient_minor_age
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

    print(f"DEBUG STAGE_ENTRY: message={message_lower!r} "
          f"insurance_type={new_patient_insurance_type!r} "
          f"insurance_collected={new_patient_insurance_collected!r} "
          f"is_minor={new_patient_is_minor!r} "
          f"accepting_checked={new_patient_accepting_checked!r} "
          f"offered_provider={new_patient_offered_provider!r}")

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
                or any(
                    phrase in message_lower for phrase in [
                        "through my employer",
                        "through an employer",
                        "employer",
                        "through work",
                        "work insurance",
                    ]
                )
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
        employer_plan = any(
            phrase in message_lower for phrase in [
                "through my employer",
                "through an employer",
                "employer",
                "through work",
            ]
        )
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
                return "Thank you. Could I get your date of birth?"
            if new_patient_is_minor:
                return (
                    "Could I get the first and last name of the "
                    "patient we will be scheduling the appointment for?"
                )
            return "Could I get your first and last name?"

        if new_patient_demographics_stage == "dob":
            if detect_dob_in_message(message):
                new_patient_dob = extract_dob_from_message(message)
                # After collecting the patient's DOB, ask who is on the
                # call (caller) before continuing with address collection.
                # For adult patients, the caller is the patient — name already collected.
                if not new_patient_is_minor and new_patient_first_name and new_patient_last_name:
                    caller_first_name = new_patient_first_name
                    caller_last_name = new_patient_last_name
                    new_patient_demographics_stage = "address"
                    return "Thank you. Could I get your street address?"
                new_patient_demographics_stage = "caller"
                return "Thank you. Who am I speaking with today?"
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

        if new_patient_demographics_stage == "address":
            new_patient_street_address = message.strip()
            new_patient_demographics_stage = "phone"
            return "Thank you. What is the best number to reach you?"

        if new_patient_demographics_stage == "phone":
            new_patient_phone = message.strip()
            new_patient_demographics_stage = "email"
            return "Thank you. Could I get your email address?"

        if new_patient_demographics_stage == "email":
            new_patient_email = message.strip()
            if (
                new_patient_insurance_type == "self_pay_quoted"
                or new_patient_self_pay_intent
            ):
                new_patient_demographics_stage = "text_consent"
                return (
                    "Last question - do we have your consent to text you "
                    "at the number you provided?"
                )
            new_patient_demographics_stage = "member_number"
            return (
                "Thank you. Could I get the member number for your insurance?"
            )

        if new_patient_demographics_stage == "member_number":
            if (
                new_patient_insurance_type == "self_pay_quoted"
                or new_patient_self_pay_intent
            ):
                new_patient_demographics_stage = "text_consent"
                return (
                    "Last question - do we have your consent to text you "
                    "at the number you provided?"
                )
            new_patient_member_number = message.strip()
            new_patient_demographics_stage = "text_consent"
            return (
                "Last question - do we have your consent to text you "
                "at the number you provided?"
            )

        if new_patient_demographics_stage == "text_consent":
            new_patient_text_consent = any(
                phrase in message_lower for phrase in proceed_phrases
            )
            new_patient_demographics_stage = "appointment_time"
            schedule = generate_weekly_availability()
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

        if new_patient_demographics_stage == "appointment_time":
            new_patient_appointment_selection = message.strip()
            if new_patient_appointment_selection.lower().endswith(" works perfectly"):
                new_patient_appointment_selection = (
                    new_patient_appointment_selection[:-len(" works perfectly")]
                    .rstrip()
                )
            new_patient_demographics_stage = "complete"
            address_name = caller_first_name if caller_first_name else new_patient_first_name
            return (
                f"Thank you, {address_name}. I have you all set. "
                f"You are scheduled with "
                f"{new_patient_offered_provider} for "
                f"{new_patient_appointment_selection}. Is there "
                f"anything else I can help you with today?"
            )

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
        r"(?i:this\s+is)\s+([A-Z][a-z]+)\s+([A-Z][a-z]+)",
        r"(?i:my\s+name\s+is)\s+([A-Z][a-z]+)\s+([A-Z][a-z]+)",
        # Avoid matching verbs like 'calling' after "I'm" or "I am".
        # Use a negative lookahead to skip common phrases such as
        # "I'm calling for" or "I'm calling about".
        r"(?i:i(?:'|)m)\s+(?!(?i:calling\b|calling\s+for\b|calling\s+about\b))([A-Z][a-z]+)\s+([A-Z][a-z]+)",
        r"(?i:i\s+am)\s+(?!(?i:calling\b|calling\s+for\b|calling\s+about\b))([A-Z][a-z]+)\s+([A-Z][a-z]+)",
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
        r"^\s*([A-Z][A-Za-z]*)\s+([A-Z][A-Za-z]*)\s*$",
    ]
    for pattern in caller_patterns:
        match = re.search(pattern, message)
        if match:
            matched_first, matched_last = match.group(1), match.group(2)
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
            and not any(phrase in message_lower for phrase in THIRD_PARTY_PHRASES)
        ):
            caller_is_patient = True

    if any(phrase in message_lower for phrase in THIRD_PARTY_PHRASES):
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
    if any(phrase in message_lower for phrase in patient_confirm_phrases):
        caller_is_patient = True

    if detect_dob_in_message(message):
        dob_collected = True

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
            return "Is the patient available right now so I can ask for their consent?"
        # If ON_HIPAA, fall through to continue normal pre-chart flow

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
SITUATION A — PATIENT IS PRESENT:
Say ONLY: "Of course! I'll wait while you get [patient name] on the line."

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
appointment request.
1. Check the OFFICE_CURRENTLY_OPEN / OFFICE_CURRENTLY_CLOSED signal
   injected by the system for this turn BEFORE saying anything about
   reaching the medical assistant.
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

SYMPTOM ROUTING:
Contagious: offer virtual. Declines: callback, 24 hours.
Pushes back: urgent care only.

MEDICATION REFILLS:
Collect: name, dosage, days remaining, pharmacy.
State 72 business hours once only.

CONTROLLED SUBSTANCES:
State last visit first always.
Within window: collect refill info, send to provider. NO appointment.
Do NOT say "if necessary we may need to schedule."
Outside window: schedule appointment.
Schedule 1: in-office only. Schedule 2/3: virtual or in-office.
Then bridge refill info after appointment.
Provider makes ALL decisions.

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


# ─────────────────────────────────────────────
# Routes
                                                    # ─────────────────────────────────────────────

@app.route("/")
def home():
    global profanity_count, hipaa_status_determined, current_hipaa_status
    global third_party_detected, dob_collected, pcp_collected
    global verbal_consent_requested, response_time_stated, office_hours_stated
    global urgent_symptoms_active, urgent_can_wait_asked
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
    global lab_result_fax_active
    global lab_result_inquiry_active
    global contagious_visit_active
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
    global ma_request_reason_collected
    global referral_lookup_done, referral_lookup_result
    global referral_specialist_name, referral_specialist_phone
    global before_hours_acute_active, before_hours_virtual_offered, before_hours_pcp_message_pending

    profanity_count = 0
    hipaa_status_determined = False
    current_hipaa_status = None
    third_party_detected = False
    dob_collected = False
    patient_self_dob_verified = False
    pcp_collected = False
    verbal_consent_requested = False
    response_time_stated = False
    office_hours_stated = False
    lab_result_fax_active = False
    lab_result_inquiry_active = False
    contagious_visit_active = False
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
    before_hours_acute_active = False
    before_hours_virtual_offered = False
    before_hours_pcp_message_pending = False
    acute_existing_appt_day = None
    acute_existing_appt_time = None
    acute_reschedule_confirm_pending = False
    acute_cancel_confirm_pending = False
    acute_new_time_pending = False
    ma_request_active = False
    ma_request_name = None
    ma_request_provider = None
    ma_request_reason_collected = False
    ma_request_reason_asked = False
    referral_lookup_done = False
    referral_lookup_result = None
    referral_specialist_name = None
    referral_specialist_phone = None
    new_patient_flow_active = False
    new_patient_requested_provider = None
    new_patient_is_minor = None
    new_patient_minor_age = None
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
    urgent_symptoms_active = False
    urgent_can_wait_asked = False
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
    conversation_history.clear()
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    global profanity_count, third_party_detected, dob_collected
    global patient_self_dob_verified
    global pcp_collected, verbal_consent_requested, response_time_stated
    global office_hours_stated, lab_result_fax_active
    global lab_result_inquiry_active
    global contagious_visit_active
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
    global referral_lookup_done, referral_lookup_result
    global referral_specialist_name, referral_specialist_phone
    global ma_request_active, ma_request_name
    global ma_request_provider, ma_request_reason_collected
    global ma_request_reason_asked
    global new_patient_flow_active, new_patient_requested_provider
    global new_patient_is_minor, new_patient_minor_age
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
    global urgent_symptoms_active, urgent_can_wait_asked
    global ma_availability_determined, current_ma_availability
    global caller_first_name, caller_last_name, patient_first_name
    global patient_last_name, caller_is_patient, pre_chart_complete
    global is_medical_professional_caller
    global med_pro_patient_first, med_pro_patient_last
    global med_pro_patient_dob, med_pro_patient_pcp
    global med_pro_collection_complete, med_pro_referral_looked_up
    global med_pro_referral_status, med_pro_referral_date
    global med_pro_referral_provider, med_pro_referral_reason
    global before_hours_acute_active, before_hours_virtual_offered, before_hours_pcp_message_pending

    user_message = request.json.get("message")
    message_lower = user_message.lower()

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
            and not Sprint13.phf_intent_detected):
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
        unambiguous_match = any(
            trigger in message_lower for trigger in NEW_PATIENT_TRIGGERS
            if trigger not in ambiguous_new_patient_phrases
        )
        ambiguous_match = (
            any(trigger in message_lower for trigger in ambiguous_new_patient_phrases)
            and not pcp_statement_pattern
            and not (pre_chart_complete and caller_is_patient)
        )
        if unambiguous_match or ambiguous_match:
            new_patient_flow_active = True
            if any(phrase in message_lower for phrase in [
                "not a patient", "i'm not a patient", "i am not a patient",
                "new patient", "become a patient",
            ]):
                caller_is_patient = False
                pre_chart_complete = True

    if new_patient_flow_active:
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
        elif Sprint13.detect_phf_inquiry_intent(message_lower) or Sprint13.phf_inquiry_intent_detected:
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

    # Detect the patient announcing themselves mid-call after a third
    # party caller has already completed pre-chart. This is separate
    # from determine_pre_chart_response (which only runs pre-completion)
    # because pre_chart_complete is already True by this point in the
    # call — Margaret's pre-chart finished first. We need a lightweight
    # check here so Robert's "This is Robert Cooper" can flip
    # caller_is_patient to True even though full pre-chart already ran.
    if third_party_detected and patient_first_name and patient_last_name:
        patient_self_announce_pattern = re.search(
            r"(?:this is|i am|i'm|it's|it is)\s+" +
            re.escape(patient_first_name) + r"\s+" +
            re.escape(patient_last_name),
            message_lower, re.IGNORECASE
        )
        if patient_self_announce_pattern:
            caller_is_patient = True

    # HIPAA Patient Self-Verification Gate.
    # When a third party call later has the PATIENT take over speaking
    # (caller_is_patient becomes True), the patient must verify their
    # OWN date of birth before any consent step can proceed — even
    # though the third party already gave a date of birth earlier.
    # This uses a SEPARATE flag (patient_self_dob_verified) so it does
    # not interfere with the third party's own DOB-based HIPAA check.
    if third_party_detected and caller_is_patient:
        if not patient_self_dob_verified:
            if detect_dob_in_message(message_lower):
                patient_self_dob_verified = True
            else:
                dob_gate_response = (
                    "Thank you. Can I get your date of birth to verify "
                    "your identity?"
                )
                conversation_history.append(
                    {"role": "user", "content": user_message}
                )
                conversation_history.append(
                    {"role": "assistant", "content": dob_gate_response}
                )
                return jsonify({"response": dob_gate_response})

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

    same_day_keywords = [
        "today", "same day", "as soon as possible",
        "this morning", "this afternoon", "asap"
    ]
    is_same_day = any(
        word in message_lower for word in same_day_keywords
    ) and any(
        word in message_lower for word in
        ["appointment", "come in", "be seen", "schedule", "slot", "get in", "today", "seen today"]
    )

    is_before_hours = not is_within_office_hours()
    is_contagious_case = (
        contagious_visit_active
        or any(trigger in message_lower for trigger in CONTAGIOUS_SYMPTOM_TRIGGERS)
    )
    if is_before_hours and is_contagious_case and is_same_day:
        before_hours_acute_active = True

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
            "warm transfer. This applies to same-day/immediate needs "
            "and MA reachability ONLY - it does NOT prevent offering or "
            "booking a FUTURE appointment. A routine future appointment "
            "request should still be offered availability and scheduled "
            "normally regardless of current office hours.\n"
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
        ma_avail = get_ma_availability()
        # Use persistent state if available, otherwise use current detection
        effective_ma_name = ma_request_name if ma_request_name else ma_name_requested
        effective_ma_provider = ma_request_provider if ma_request_provider else ma_provider
        if effective_ma_name and effective_ma_provider:
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
            "PATIENT_PRESENT: Say ONLY: 'Of course! I'll wait while "
            "you get [patient name] on the line.' Nothing else.\n"
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

    availability_context = ""
    if (
        any(word in message_lower for word in [
            "appointment", "schedule", "available", "availability",
            "next week", "this week"
        ])
        or virtual_visit_accepted_now
        or same_day_virtual_clinic_declined_now
        or next_available_options_pending
    ) and not is_medical_professional_caller:
        if not is_same_day or virtual_visit_accepted_now or same_day_virtual_clinic_declined_now or next_available_options_pending:
            forced_pcp_weekly = get_harness_override("STEVE_FORCE_PCP_WEEKLY_AVAILABLE")
            schedule = generate_weekly_availability(force_has_availability=forced_pcp_weekly)
            pcp_has_no_availability_this_week = not any(schedule.values())
            if pcp_has_no_availability_this_week and not virtual_visit_accepted_now:
                covering_eligible_week = is_visit_eligible_for_covering_provider(
                    message_lower
                )
                if covering_eligible_week:
                    forced_covering_weekly = get_harness_override("STEVE_FORCE_COVERING_WEEKLY_AVAILABLE")
                    covering_weekly_schedule = generate_weekly_availability(force_has_availability=forced_covering_weekly)
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
        availability_context += (
            f"\nTODAY'S DATE INJECTED BY SYSTEM: Today is "
            f"{_today.strftime('%A, %B %d, %Y')}. Resolve any relative "
            f"date the patient uses (\"today\", \"tomorrow\", \"this "
            f"Monday\", \"next Monday\", etc.) against this actual date "
            f"before matching it to the weekly availability above.\n"
        )

    before_hours_acute_context = ""
    if before_hours_acute_active and not is_within_office_hours() and not is_medical_professional_caller:
        if not before_hours_virtual_offered:
            before_hours_virtual_offered = True
            before_hours_acute_context = (
                "BEFORE_HOURS_ACUTE_VIRTUAL_OFFER INJECTED BY SYSTEM:\n"
                "Conditions met: Acute contagious symptoms, before office hours, same-day appointment request.\n"
                "The office is closed and medical assistants/staff are NOT working.\n"
                "Do NOT claim to check with a medical assistant or staff.\n"
                "Do NOT claim the PCP is unavailable or perform real-time checks.\n"
                "Do NOT offer Elizabeth Horowitz or covering provider availability.\n"
                "STEP 1: Offer Same-Day Virtual Clinic.\n"
                f"Provide: Same-Day Virtual Clinic phone number ({SAME_DAY_VIRTUAL_CLINIC_PHONE_NUMBER}) and Same-Day Virtual Clinic hours (e.g. 8:00 AM to 8:00 PM).\n"
                "Ask if they would like to use the Same-Day Virtual Clinic.\n"
            )
        elif before_hours_pcp_message_pending:
            phone_num = re.search(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b', user_message)
            if phone_num:
                before_hours_acute_context = (
                    "BEFORE_HOURS_ACUTE_PCP_MESSAGE_CONFIRMED INJECTED BY SYSTEM:\n"
                    "Callback number collected.\n"
                    "Confirm that a HIGH PRIORITY phone message has been created for their provider when the office opens.\n"
                    "Do NOT claim to have checked with staff. Do NOT claim to have checked availability.\n"
                    "Wish the patient well and ask if there is anything else.\n"
                )
            else:
                before_hours_acute_context = (
                    "BEFORE_HOURS_ACUTE_PCP_MESSAGE_REQUEST INJECTED BY SYSTEM:\n"
                    "Patient specifically wants to be seen by their PCP today.\n"
                    "Collect callback number to create a HIGH PRIORITY phone message.\n"
                    "Do NOT claim to have checked with staff. Do NOT claim to have checked availability.\n"
                )
        else:
            wants_pcp_specifically = any(
                p in message_lower for p in [
                    "dr.", "dr ", "doctor", "foster", "pcp", "my doctor", "my provider",
                    "specifically", "want to be seen by dr", "want dr"
                ]
            )
            wants_in_person = any(
                p in message_lower for p in [
                    "in person", "in-person", "don't want virtual", "do not want virtual",
                    "no virtual", "want to be seen", "prefer to be seen"
                ]
            )
            patient_accepted_virtual = (
                patient_wants_to_proceed(message_lower)
                and not wants_pcp_specifically
                and not wants_in_person
            )

            if patient_accepted_virtual:
                before_hours_acute_context = (
                    "BEFORE_HOURS_ACUTE_ACCEPTED INJECTED BY SYSTEM:\n"
                    "STEP 2: Patient ACCEPTS Same-Day Virtual Clinic.\n"
                    f"Provide Same-Day Virtual Clinic information: phone number {SAME_DAY_VIRTUAL_CLINIC_PHONE_NUMBER}.\n"
                    "Wish the patient a nice day (e.g. 'I've provided the Same-Day Virtual Clinic information. I hope you feel better soon and have a nice day.').\n"
                    "End the workflow. STOP. Do NOT continue scheduling.\n"
                )
            elif wants_pcp_specifically:
                before_hours_pcp_message_pending = True
                before_hours_acute_context = (
                    "BEFORE_HOURS_ACUTE_DECLINED_PCP INJECTED BY SYSTEM:\n"
                    "STEP 4: Patient declines virtual clinic because they specifically want their PCP today.\n"
                    "Collect callback number to create a HIGH PRIORITY phone message.\n"
                    "Do NOT claim to have checked with staff. Do NOT claim to have checked availability.\n"
                )
            elif wants_in_person or patient_wants_to_decline(message_lower):
                before_hours_acute_context = (
                    "BEFORE_HOURS_ACUTE_DECLINED_IN_PERSON INJECTED BY SYSTEM:\n"
                    "STEP 3: Patient declines virtual clinic because they want to be seen IN PERSON.\n"
                    "Advise Urgent Care.\n"
                )
            else:
                before_hours_acute_context = (
                    "BEFORE_HOURS_ACUTE_VIRTUAL_OFFER INJECTED BY SYSTEM:\n"
                    f"Provide Same-Day Virtual Clinic information (phone: {SAME_DAY_VIRTUAL_CLINIC_PHONE_NUMBER}) and ask if they accept.\n"
                )

    same_day_context = ""
    if is_same_day and not is_medical_professional_caller and not (before_hours_acute_active and not is_within_office_hours()):
        now = datetime.now()
        current_time_str = now.strftime("%I:%M %p")
        office_open = is_office_open_today()
        if not office_open:
            next_monday = get_next_monday()
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
        if virtual_clinic_requested:
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
    elif virtual_visit_accepted_now:
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

    controlled_substance_context = ""
    if controlled_substance_schedule and not is_medical_professional_caller:
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

    hipaa_context = ""
    if (third_party_detected and dob_collected and
            pcp_collected and not is_medical_professional_caller
            and not caller_is_patient):
        hipaa_status = get_hipaa_status()
        if hipaa_status == "ON_HIPAA":
            hipaa_instruction = (
                "Say: 'I was able to verify that you are listed on the "
                "patient's HIPAA authorization. I will be happy to "
                "assist you today.' Then ask for PCP if not collected."
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

    conversation_history.append({"role": "user", "content": user_message})

    phf_context = Sprint13.build_context()

    system_with_context = (
        system_prompt + "\n" +
        pre_chart_context + "\n" +
        response_time_context + "\n" +
        office_hours_context + "\n" +
        medical_professional_context + "\n" +
        pcp_context + "\n" +
        nurse_ma_context + "\n" +
        lab_order_fax_to_facility_context + "\n" +
        lab_order_pickup_context + "\n" +
        patient_presence_context + "\n" +
        lab_work_context + "\n" +
        lab_result_inquiry_context + "\n" +
        lab_result_fax_outside_context + "\n" +
        referral_lookup_context + "\n" +
        medication_inquiry_context + "\n" +
        (contagious_virtual_refusal_context if not (before_hours_acute_active and not is_within_office_hours()) else "") + "\n" +
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
        before_hours_acute_context
    )

    messages = [{"role": "system", "content": system_with_context}] + \
        conversation_history

    try:
        response = client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=messages,
            reasoning_effort="none"
        )
        assistant_message = response.choices[0].message.content
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
                    capture_first, capture_last, confirmed_generic_appt
                )
        return jsonify({"response": assistant_message})
    except Exception as e:
        print(f"Groq API error: {e}")
        return jsonify({"response": f"Error: {str(e)}"})

if __name__ == "__main__":
    app.run(debug=True)