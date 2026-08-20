"""
Sprint 13: Post Hospital Follow-Up Workflow
"""

import random
import re
import json
import os
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
# State variables
# ─────────────────────────────────────────────

phf_flow_active = False
phf_stage = None
phf_caller_type = None  # "patient", "hospital_team"
phf_patient_first_name = None
phf_patient_last_name = None
phf_patient_dob = None
phf_patient_pcp = None
phf_hospital_name = None
phf_admission_date = None
phf_discharge_date = None
phf_reason_for_admission = None
phf_follow_up_deadline = None
phf_pcp_availability = None
phf_covering_availability = None
phf_appointment_day = None
phf_appointment_time = None
phf_callback_number = None
phf_existing_appt_day = None
phf_existing_appt_time = None
phf_reschedule_pending = False
phf_cancel_pending = False
phf_inquiry_pending = False
phf_work_in_requested = False
phf_intent_detected = False
phf_inquiry_intent_detected = False

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

COVERING_PROVIDER_NAME = "Elizabeth Horowitz, APRN"
COVERING_PROVIDER_BARE_NAME = "Elizabeth Horowitz"
COVERING_PROVIDER_CREDENTIAL = "APRN"
COVERING_PROVIDER_MA_NAME = "Catherine"

AVAILABLE_TIMES = [
    "9:00 AM", "9:30 AM", "10:00 AM", "10:30 AM",
    "11:00 AM", "11:30 AM", "1:00 PM", "1:30 PM",
    "2:00 PM", "2:30 PM", "3:00 PM", "3:30 PM",
    "4:00 PM", "4:40 PM"
]

DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

POST_HOSPITAL_FOLLOWUP_TRIGGERS = [
    "schedule a post hospital follow up", "schedule a post-hospital follow up",
    "schedule a post hospital follow-up", "schedule a post-hospital follow-up",
    "need a post discharge appointment", "need a post-discharge appointment",
    "follow up after hospital discharge", "follow-up after hospital discharge",
    "appointment after hospital discharge", "post hospital appointment",
    "post-hospital appointment", "discharged from hospital need appointment",
    "just discharged need follow up", "just discharged need follow-up",
    "hospital discharge follow up appointment", "hospital discharge follow-up appointment",
    "post hospitalization follow up", "post-hospitalization follow up",
    "post hospitalization follow-up", "post-hospitalization follow-up",
    "follow up after being discharged", "follow-up after being discharged",
    "schedule follow up after hospital", "schedule follow-up after hospital",
]

PHF_RESCHEDULE_TRIGGERS = [
    "reschedule my follow up", "reschedule my follow-up",
    "reschedule post hospital", "reschedule post-hospital",
    "reschedule my post hospital", "reschedule my post-hospital",
    "move my follow up", "change my follow up appointment",
    "change my post hospital appointment", "change my post-hospital appointment",
]

# Callers very commonly just say "reschedule my appointment" without
# qualifying it as a post-hospital follow-up. PHF_RESCHEDULE_TRIGGERS
# above is deliberately narrow (requires "follow up"/"post hospital"
# phrasing) since this generic phrasing could equally mean a different,
# non-PHF appointment. The caller must be checked at the call site for
# whether they have an actual PHF appointment on file before treating
# this as PHF reschedule intent - see detect_generic_reschedule_intent().
PHF_GENERIC_RESCHEDULE_TRIGGERS = [
    "reschedule my appointment", "reschedule the appointment",
    "reschedule appointment", "change my appointment",
    "move my appointment", "reschedule it",
]

PHF_CANCEL_TRIGGERS = [
    "cancel my follow up", "cancel my follow-up",
    "cancel post hospital appointment", "cancel my post-hospital appointment",
    "cancel my post hospital follow up", "cancel my post-hospital follow-up",
]

# A caller asking ABOUT an existing PHF appointment (when is it, what
# time, do I have one) is a lookup request - distinct from PHF_
# followup_TRIGGERS/POST_HOSPITAL_FOLLOWUP_TRIGGERS, which signal a
# request to SCHEDULE a new one.
PHF_INQUIRY_TRIGGERS = [
    "when is my post hospital follow up", "when is my post-hospital follow up",
    "when is my post hospital follow-up", "when is my post-hospital follow-up",
    "what time is my post hospital follow up", "what time is my post-hospital follow up",
    "what time is my post hospital follow-up", "what time is my post-hospital follow-up",
    "do i have a post hospital follow up", "do i have a post-hospital follow up",
    "do i have a post hospital appointment", "do i have a post-hospital appointment",
    "check on my post hospital follow up", "check on my post-hospital follow up",
    "check my post hospital follow up", "check my post-hospital follow up",
    "when is my post hospital appointment", "when is my post-hospital appointment",
]


# ─────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────


def reset_state():
    global phf_flow_active, phf_stage, phf_caller_type
    global phf_patient_first_name, phf_patient_last_name
    global phf_patient_dob, phf_patient_pcp
    global phf_hospital_name, phf_admission_date, phf_discharge_date, phf_reason_for_admission
    global phf_follow_up_deadline, phf_pcp_availability, phf_covering_availability
    global phf_appointment_day, phf_appointment_time, phf_callback_number
    global phf_existing_appt_day, phf_existing_appt_time
    global phf_reschedule_pending, phf_cancel_pending, phf_work_in_requested
    global phf_intent_detected, phf_inquiry_pending, phf_inquiry_intent_detected

    phf_flow_active = False
    phf_stage = None
    phf_caller_type = None
    phf_patient_first_name = None
    phf_patient_last_name = None
    phf_patient_dob = None
    phf_patient_pcp = None
    phf_hospital_name = None
    phf_admission_date = None
    phf_discharge_date = None
    phf_reason_for_admission = None
    phf_follow_up_deadline = None
    phf_pcp_availability = None
    phf_covering_availability = None
    phf_appointment_day = None
    phf_appointment_time = None
    phf_callback_number = None
    phf_existing_appt_day = None
    phf_existing_appt_time = None
    phf_reschedule_pending = False
    phf_cancel_pending = False
    phf_inquiry_pending = False
    phf_work_in_requested = False
    phf_intent_detected = False
    phf_inquiry_intent_detected = False


def detect_post_hospital_intent(message_lower):
    return any(trigger in message_lower for trigger in POST_HOSPITAL_FOLLOWUP_TRIGGERS)


def detect_phf_reschedule_intent(message_lower):
    return any(trigger in message_lower for trigger in PHF_RESCHEDULE_TRIGGERS)


def detect_generic_reschedule_intent(message_lower):
    return any(trigger in message_lower for trigger in PHF_GENERIC_RESCHEDULE_TRIGGERS)


def detect_phf_cancel_intent(message_lower):
    return any(trigger in message_lower for trigger in PHF_CANCEL_TRIGGERS)


def detect_phf_inquiry_intent(message_lower):
    return any(trigger in message_lower for trigger in PHF_INQUIRY_TRIGGERS)


def _resolve_relative_date(message):
    """Resolve common relative date phrases to an absolute MM/DD/YYYY string."""
    msg_lower = message.lower()
    today = datetime.now()

    day_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6
    }

    if "yesterday" in msg_lower:
        return (today - timedelta(days=1)).strftime("%m/%d/%Y")

    if "tomorrow" in msg_lower:
        return (today + timedelta(days=1)).strftime("%m/%d/%Y")

    if "today" in msg_lower:
        return today.strftime("%m/%d/%Y")

    if "last week" in msg_lower:
        return (today - timedelta(days=7)).strftime("%m/%d/%Y")

    if "earlier this week" in msg_lower:
        return (today - timedelta(days=3)).strftime("%m/%d/%Y")

    for day_name, day_num in day_map.items():
        if f"this past {day_name}" in msg_lower:
            days_diff = (today.weekday() - day_num) % 7
            if days_diff == 0:
                days_diff = 7
            return (today - timedelta(days=days_diff)).strftime("%m/%d/%Y")

    for day_name, day_num in day_map.items():
        if f"last {day_name}" in msg_lower:
            days_diff = (today.weekday() - day_num) % 7
            if days_diff == 0:
                days_diff = 7
            days_diff += 7
            return (today - timedelta(days=days_diff)).strftime("%m/%d/%Y")

    return None


def detect_discharge_date_in_message(message):
    relative = _resolve_relative_date(message)
    if relative:
        return relative

    patterns = [
        r'\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b',
        r'\b(january|february|march|april|may|june|july|august|'
        r'september|october|november|december)\s+\d{1,2},?\s+\d{4}\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def _offered_slot_day_matches(message, message_lower, date_key):
    """Return True if the caller's message refers to the calendar day an
    offered appointment slot falls on. date_key looks like "Wednesday,
    August 12" (no year). Two ways this can match:
      1. An explicit day name appears in the message ("Wednesday",
         "next Monday", "this Thursday" all contain their day name as a
         substring, so this already covers those cases).
      2. A relative reference with no day name at all ("tomorrow",
         "today") - resolved via the existing _resolve_relative_date()
         to an absolute date, then compared against the offered slot's
         actual month/day.
    """
    day_name = date_key.split(',')[0]
    if day_name.lower() in message_lower:
        return True

    resolved = _resolve_relative_date(message)
    if resolved:
        try:
            resolved_dt = datetime.strptime(resolved, "%m/%d/%Y")
            month_day = date_key.split(',', 1)[1].strip()
            key_dt = datetime.strptime(f"{month_day} {resolved_dt.year}", "%B %d %Y")
            if (resolved_dt.month, resolved_dt.day) == (key_dt.month, key_dt.day):
                return True
        except (ValueError, IndexError):
            pass

    return False


def parse_discharge_date(date_str):
    formats = [
        "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
        "%B %d, %Y", "%B %d %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def calculate_follow_up_deadline(discharge_date_str):
    discharge_dt = parse_discharge_date(discharge_date_str)
    if discharge_dt:
        deadline = discharge_dt + timedelta(days=7)
        return deadline.strftime("%A, %B %d, %Y")
    return None


# ─────────────────────────────────────────────
# Sprint 13 Test Harness
# ─────────────────────────────────────────────
# Lets Scenario 7 (PCP available), Scenario 8 (covering provider
# available), and Scenario 9 (neither available / work-in triggered) be
# tested deterministically, without depending on random.choice()
# outcomes. Both PHF scheduling and PHF rescheduling call
# generate_pcp_availability_within_7_days() and
# generate_covering_provider_availability_within_7_days() for their
# availability, so forcing the outcome here covers both flows with no
# separate scheduling/rescheduling harness code needed.
#
# PHF_TEST_MODE must be explicitly set to True to activate this at all.
# With PHF_TEST_MODE = False (the default), both generator functions
# below execute exactly as before this change - the harness gate is a
# single early-return checked before anything else runs, so none of the
# existing random-generation logic is touched, reordered, or altered.
PHF_TEST_MODE = False

# One of: "PCP_AVAILABLE", "COVERING_AVAILABLE", "NO_AVAILABILITY"
TEST_SCENARIO = "PCP_AVAILABLE"


def _test_fixed_availability():
    """Deterministic, weekday-safe near-term slot set used only when
    PHF_TEST_MODE is True. Same {date_str: [times]} shape the real
    generators return, so every downstream consumer (slot matching,
    format_availability, follow-up deadline calculation, appointment
    persistence) works completely unmodified."""
    today = datetime.now()
    day_offset = 1
    date = today + timedelta(days=day_offset)
    while date.weekday() >= 5:
        day_offset += 1
        date = today + timedelta(days=day_offset)
    date_str = date.strftime("%A, %B %d")
    return {date_str: ["10:00 AM", "2:00 PM"]}


def generate_pcp_availability_within_7_days():
    if PHF_TEST_MODE:
        if TEST_SCENARIO == "PCP_AVAILABLE":
            return _test_fixed_availability()
        return {}
    availability = {}
    if not random.choice([True, False]):
        return availability
    today = datetime.now()
    for day_offset in range(1, 8):
        date = today + timedelta(days=day_offset)
        if date.weekday() >= 5:
            continue
        if random.choice([True, False]):
            num_slots = random.randint(1, 3)
            slots = random.sample(AVAILABLE_TIMES, num_slots)
            slots.sort(key=lambda x: datetime.strptime(x, "%I:%M %p"))
            date_str = date.strftime("%A, %B %d")
            availability[date_str] = slots
    return availability


def generate_covering_provider_availability_within_7_days():
    if PHF_TEST_MODE:
        if TEST_SCENARIO == "COVERING_AVAILABLE":
            return _test_fixed_availability()
        return {}
    availability = {}
    if not random.choice([True, False]):
        return availability
    today = datetime.now()
    for day_offset in range(1, 8):
        date = today + timedelta(days=day_offset)
        if date.weekday() >= 5:
            continue
        if random.choice([True, False]):
            num_slots = random.randint(1, 3)
            slots = random.sample(AVAILABLE_TIMES, num_slots)
            slots.sort(key=lambda x: datetime.strptime(x, "%I:%M %p"))
            date_str = date.strftime("%A, %B %d")
            availability[date_str] = slots
    return availability


def format_availability(availability_dict):
    if not availability_dict:
        return "No availability within the next 7 days."
    lines = []
    for date_str, slots in availability_dict.items():
        lines.append(f"- {date_str}: {', '.join(slots)}")
    return "\n".join(lines)


def time_matches(user_input_text, available_time_slot):
    """Check if user's time mention matches an available time slot flexibly."""
    user_lower = user_input_text.lower()
    slot_lower = available_time_slot.lower()

    if slot_lower in user_lower:
        return True

    try:
        slot_dt = datetime.strptime(available_time_slot, "%I:%M %p")
        slot_hour = slot_dt.hour
        slot_minute = slot_dt.minute

        import re
        time_patterns = [
            r'(\d{1,2}):(\d{2})\s*(am|pm)',
            r'(\d{1,2})(\d{2})\s*(am|pm)',
            r'(\d{1,2})\s*(am|pm)',
        ]

        for pattern in time_patterns:
            matches = re.findall(pattern, user_lower)
            for match in matches:
                if len(match) == 3:
                    hour, minute, meridiem = match
                    hour = int(hour)
                    minute = int(minute)
                    if meridiem == 'pm' and hour != 12:
                        hour += 12
                    elif meridiem == 'am' and hour == 12:
                        hour = 0
                    if hour == slot_hour and minute == slot_minute:
                        return True
                elif len(match) == 2:
                    hour, meridiem = match
                    hour = int(hour)
                    if meridiem == 'pm' and hour != 12:
                        hour += 12
                    elif meridiem == 'am' and hour == 12:
                        hour = 0
                    if hour == slot_hour and slot_minute == 0:
                        return True
    except (ValueError, AttributeError):
        pass

    return False


def generate_existing_phf_appointment():
    today = datetime.now()
    days_ahead = random.randint(3, 14)
    appt_date = today + timedelta(days=days_ahead)
    while appt_date.weekday() >= 5:
        days_ahead += 1
        appt_date = today + timedelta(days=days_ahead)
    day_name = appt_date.strftime("%A, %B %d")
    time = random.choice(AVAILABLE_TIMES)
    return day_name, time


# ─────────────────────────────────────────────
# Persistent appointment record store
# ─────────────────────────────────────────────
# Simulates a durable record of scheduled PHF appointments, keyed by
# patient identity. Deliberately NOT touched by reset_state(): reset_state()
# only clears the in-progress conversational state for the CURRENT call,
# the same way a real EHR/scheduling record persists independently of any
# single phone call's session. This lets a discharge date captured during
# one call remain available for lookup during a LATER call when the same
# patient asks to reschedule.
#
# Backed by a small JSON file on disk (not just an in-memory dict): an
# in-memory-only dict lives solely in the current Python process's
# memory, so a record written while handling one request would be
# invisible to a later request served by a different worker process (or
# after a server restart) - even though both requests belong to the same
# logical patient encounter. The file is the source of truth; the dict
# is a fast in-process cache of it.
_PHF_RECORDS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "phf_appointment_records.json"
)


def _load_phf_appointment_records():
    """Read the persisted appointment records from disk. Returns an empty
    dict if the file doesn't exist yet or can't be parsed."""
    if os.path.exists(_PHF_RECORDS_FILE):
        try:
            with open(_PHF_RECORDS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_phf_appointment_records():
    """Write the current in-memory records dict to disk so it survives
    across separate processes/requests, not just within this one."""
    try:
        with open(_PHF_RECORDS_FILE, "w") as f:
            json.dump(phf_appointment_records, f)
    except OSError:
        pass


phf_appointment_records = _load_phf_appointment_records()


def _patient_record_key(first_name, last_name):
    """Build a lookup key for the persistent appointment record store."""
    if not first_name or not last_name:
        return None
    return f"{first_name.strip().lower()}_{last_name.strip().lower()}"


def store_phf_appointment_record():
    """Persist the current PHF encounter's discharge date and scheduled
    appointment under the patient's identity, so a future reschedule
    request for the same patient can look them up instead of the
    workflow fabricating a brand-new, unrelated discharge date."""
    key = _patient_record_key(phf_patient_first_name, phf_patient_last_name)
    if not key:
        return
    phf_appointment_records[key] = {
        "discharge_date": phf_discharge_date,
        "hospital_name": phf_hospital_name,
        "appointment_day": phf_appointment_day,
        "follow_up_deadline": phf_follow_up_deadline,
    }
    _save_phf_appointment_records()


def get_stored_appointment_record(first_name, last_name):
    """Look up a previously-persisted PHF appointment record for this
    patient, if one exists. Re-reads from disk first so a record written
    by a different process (a different worker, or an earlier run of the
    app) is found, not only records written earlier in this same
    process's lifetime."""
    key = _patient_record_key(first_name, last_name)
    if not key:
        return None
    if key not in phf_appointment_records:
        phf_appointment_records.update(_load_phf_appointment_records())
    return phf_appointment_records.get(key)


# ─────────────────────────────────────────────
# Main workflow handler
# ─────────────────────────────────────────────

def handle_post_hospital_flow(message, message_lower):
    global phf_flow_active, phf_stage, phf_caller_type
    global phf_patient_first_name, phf_patient_last_name
    global phf_patient_dob, phf_patient_pcp
    global phf_hospital_name, phf_admission_date, phf_discharge_date, phf_reason_for_admission
    global phf_follow_up_deadline, phf_pcp_availability, phf_covering_availability
    global phf_appointment_day, phf_appointment_time, phf_callback_number
    global phf_existing_appt_day, phf_existing_appt_time
    global phf_reschedule_pending, phf_cancel_pending, phf_work_in_requested
    global phf_inquiry_pending

    # Capture patient name from app.py globals if not already set in PHF context
    import app
    if not phf_patient_first_name and hasattr(app, 'patient_first_name') and app.patient_first_name:
        phf_patient_first_name = app.patient_first_name
    if not phf_patient_last_name and hasattr(app, 'patient_last_name') and app.patient_last_name:
        phf_patient_last_name = app.patient_last_name
    if not phf_patient_dob and hasattr(app, 'dob_collected') and app.dob_collected:
        # Note: app.py doesn't store the actual DOB, just the flag; PHF will detect it from context
        pass
    if not phf_patient_pcp and hasattr(app, 'pcp_collected') and app.pcp_collected:
        # Note: app.py doesn't store the actual PCP, just the flag; PHF will detect it from context
        pass

    patient_full = None
    if phf_patient_first_name and phf_patient_last_name:
        patient_full = f"{phf_patient_first_name} {phf_patient_last_name}"

    # ── RESCHEDULE WORKFLOW ──
    if phf_reschedule_pending:
        # Enhancement: look up a previously-persisted discharge date and
        # scheduled appointment for this patient (stored when their PHF
        # appointment was originally created, possibly in an earlier
        # call) instead of fabricating a brand-new, unrelated discharge
        # date and appointment every time a reschedule is requested.
        if not phf_discharge_date or not phf_existing_appt_day:
            stored_record = get_stored_appointment_record(
                phf_patient_first_name, phf_patient_last_name
            )
            if stored_record:
                if not phf_discharge_date and stored_record.get("discharge_date"):
                    phf_discharge_date = stored_record["discharge_date"]
                    phf_follow_up_deadline = calculate_follow_up_deadline(phf_discharge_date)
                if not phf_hospital_name and stored_record.get("hospital_name"):
                    phf_hospital_name = stored_record["hospital_name"]
                if not phf_existing_appt_day and stored_record.get("appointment_day"):
                    stored_appt = stored_record["appointment_day"]
                    if " @ " in stored_appt:
                        appt_day_part, appt_time_part = stored_appt.split(" @ ", 1)
                        phf_existing_appt_day = appt_day_part
                        phf_existing_appt_time = appt_time_part
                    else:
                        phf_existing_appt_day = stored_appt
        if not phf_existing_appt_day:
            phf_existing_appt_day, phf_existing_appt_time = generate_existing_phf_appointment()
        if not phf_pcp_availability:
            phf_pcp_availability = generate_pcp_availability_within_7_days()
        if phf_pcp_availability:
            phf_reschedule_pending = False
            # Bug fix: route the next turn (the caller's day/time pick)
            # to the check_pcp_availability handler instead of leaving
            # phf_stage unset, which previously caused the who_is_calling
            # question to loop back.
            phf_stage = "check_pcp_availability"
            avail_text = format_availability(phf_pcp_availability)
            return (
                f"I can help you reschedule your post-hospital follow-up "
                f"appointment (currently {phf_existing_appt_day} at "
                f"{phf_existing_appt_time}). Here is your provider's "
                f"availability:\n{avail_text}\n"
                f"Which day and time works best for you?"
            )
        else:
            if not phf_covering_availability:
                phf_covering_availability = generate_covering_provider_availability_within_7_days()
            if phf_covering_availability:
                phf_reschedule_pending = False
                # Bug fix: route the next turn (accept/decline or a
                # direct day/time pick) to the existing
                # check_covering_availability handler instead of leaving
                # phf_stage unset, which previously caused the
                # who_is_calling question to loop back.
                phf_stage = "check_covering_availability"
                avail_text = format_availability(phf_covering_availability)
                return (
                    f"I'm sorry, your provider has no availability to "
                    f"reschedule your follow-up. However, "
                    f"{COVERING_PROVIDER_CREDENTIAL} {COVERING_PROVIDER_BARE_NAME} "
                    f"has these openings:\n{avail_text}\n"
                    f"Would you like to schedule with "
                    f"{COVERING_PROVIDER_BARE_NAME} instead?"
                )
            else:
                phf_reschedule_pending = False
                phf_work_in_requested = True
                # Bug fix: without an explicit stage here, phf_stage stays
                # None/unset and the NEXT message falls into the
                # who_is_calling dispatch below instead of the work-in
                # callback-collection block, causing the "are you the
                # patient or hospital transition team" question to loop.
                phf_stage = "awaiting_work_in_callback"
                return (
                    f"I'm sorry, but neither your provider nor "
                    f"{COVERING_PROVIDER_BARE_NAME} has any availability "
                    f"within the next 7 days. I will create a high priority "
                    f"work-in request for your provider. May I get a good "
                    f"callback number for you?"
                )

    # ── CANCELLATION WORKFLOW ──
    if phf_cancel_pending:
        if not phf_existing_appt_day:
            phf_existing_appt_day, phf_existing_appt_time = generate_existing_phf_appointment()
        phf_cancel_pending = False
        return (
            f"Your post-hospital follow-up appointment on "
            f"{phf_existing_appt_day} at {phf_existing_appt_time} has been "
            f"cancelled. Please keep in mind that follow-up care after a "
            f"hospital stay is very important for your recovery. If you "
            f"change your mind, please call us back to reschedule. "
            f"Is there anything else I can help you with today?"
        )

    # ── APPOINTMENT INQUIRY WORKFLOW ──
    # A caller asking "when is my post hospital follow up appointment?"
    # is asking to look up an EXISTING appointment, not requesting a new
    # one. Check the persisted appointment record store; regardless of
    # whether a record is found, this branch must never fall into the
    # discharge_info intake flow or ask admission/discharge questions -
    # an inquiry and a new-scheduling request are deliberately distinct
    # intents (detect_phf_inquiry_intent vs detect_post_hospital_intent).
    if phf_inquiry_pending:
        phf_inquiry_pending = False
        inquiry_record = get_stored_appointment_record(
            phf_patient_first_name, phf_patient_last_name
        )
        phf_flow_active = False
        if inquiry_record and inquiry_record.get("appointment_day"):
            appt_str = inquiry_record["appointment_day"].replace(" @ ", " at ")
            pcp_str = f" with {phf_patient_pcp}" if phf_patient_pcp else ""
            if phf_caller_type == "hospital_team" and patient_full:
                return (
                    f"I see that {patient_full}'s post-hospital follow-up "
                    f"appointment is scheduled for {appt_str}{pcp_str}."
                )
            return (
                f"I see that your post-hospital follow-up appointment is "
                f"scheduled for {appt_str}{pcp_str}."
            )
        # No appointment on file - say so plainly. Do NOT enter
        # discharge_info or ask admission questions; the caller must
        # separately request scheduling if they want one.
        if phf_caller_type == "hospital_team" and patient_full:
            return (
                f"I don't see a post-hospital follow-up appointment on "
                f"file for {patient_full}. Would you like me to schedule one?"
            )
        return (
            f"I don't see a post-hospital follow-up appointment on file "
            f"for you right now. Would you like me to schedule one?"
        )

    # ── MAIN WORKFLOW ──

    # Stage: Determine caller type
    if phf_stage is None or phf_stage == "who_is_calling":
        if "hospital" in message_lower and (
                "transition" in message_lower or "team" in message_lower or "calling from" in message_lower):
            phf_caller_type = "hospital_team"
            phf_stage = "discharge_info"
        elif "i am the patient" in message_lower or "i'm the patient" in message_lower:
            phf_caller_type = "patient"
            phf_stage = "discharge_info"
        elif "patient" in message_lower and ("calling for" in message_lower or "on behalf of" in message_lower):
            phf_caller_type = "patient"
            phf_stage = "discharge_info"
        else:
            phf_stage = "who_is_calling"
            return (
                "Are you the patient, or are you calling from the "
                "hospital transition team?"
            )

    # Stage: Collect discharge info
    if phf_stage == "discharge_info":
        if not phf_hospital_name:
            hospital_match = re.search(
                r"(?:at|from|at the|from the)\s+([A-Z][A-Za-z\s\-'.]+(?:Hospital|Medical Center|Health Center|Clinic))",
                message, re.IGNORECASE
            )
            if hospital_match:
                phf_hospital_name = hospital_match.group(1).strip()
            else:
                # Fallback for bare hospital name without prefix
                hospital_match = re.search(
                    r"\b([A-Z][A-Za-z\s\-'.]+(?:Hospital|Medical Center|Health Center|Clinic))\b",
                    message, re.IGNORECASE
                )
                if hospital_match:
                    extracted = hospital_match.group(1).strip()
                    extracted_lower = extracted.lower()
                    if "post" not in extracted_lower and "hospitalized" not in extracted_lower:
                        phf_hospital_name = extracted
                elif phf_admission_date and phf_discharge_date:
                    # Neither regex matched, but by this point in the
                    # sequential discharge_info Q&A, admission and
                    # discharge dates are already known - meaning this
                    # message is the caller's direct answer to "Which
                    # hospital were you discharged from?" Not every real
                    # hospital name ends in Hospital/Medical Center/
                    # Health Center/Clinic (e.g. "Orlando Health"), so
                    # accept a short direct reply as-is rather than
                    # requiring that suffix. Bounded to a short reply so
                    # an unrelated longer sentence doesn't get misread as
                    # a hospital name.
                    stripped_msg = message.strip().rstrip(".")
                    if stripped_msg and len(stripped_msg.split()) <= 6:
                        phf_hospital_name = stripped_msg

        admission_date_just_set = False
        if not phf_admission_date:
            date_str = detect_discharge_date_in_message(message)
            if date_str:
                phf_admission_date = date_str
                admission_date_just_set = True
        # Bug fix: only attempt to parse the discharge date from THIS
        # message if it wasn't the message that just supplied the
        # admission date. Previously both blocks ran the same date
        # detector against the identical message text in the same turn,
        # so a single relative-date answer (e.g. "This past Sunday")
        # given in response to "what date were you admitted?" would also
        # silently populate phf_discharge_date, skipping the
        # "What was your discharge date?" question entirely.
        if not phf_discharge_date and not admission_date_just_set:
            date_str = detect_discharge_date_in_message(message)
            if date_str:
                phf_discharge_date = date_str
                phf_follow_up_deadline = calculate_follow_up_deadline(date_str)

        if not phf_reason_for_admission:
            reason_match = re.search(
                r"(?:for|because of|due to|with)\s+(.+?)(?:\.|,|and|but|$)",
                message_lower
            )
            if reason_match and len(reason_match.group(1)) > 3:
                phf_reason_for_admission = reason_match.group(1).strip()

        if not phf_admission_date:
            if phf_caller_type == "hospital_team" and patient_full:
                return f"Thank you. What date was {patient_full} admitted?"
            return "Thank you. What date were you admitted?"
        if not phf_discharge_date:
            if phf_caller_type == "hospital_team" and patient_full:
                return f"What date was {patient_full} discharged?"
            return "What was your discharge date?"
        if not phf_hospital_name:
            if phf_caller_type == "hospital_team" and patient_full:
                return f"What hospital was {patient_full} discharged from?"
            return "Which hospital were you discharged from?"

        phf_stage = "check_pcp_availability"
        phf_pcp_availability = generate_pcp_availability_within_7_days()

        if phf_pcp_availability:
            avail_text = format_availability(phf_pcp_availability)
            if phf_caller_type == "hospital_team":
                return (
                    f"Thank you. The follow-up deadline is "
                    f"{phf_follow_up_deadline}. Here is the provider's "
                    f"availability:\n{avail_text}\n"
                    f"Which day and time would you like to schedule?"
                )
            else:
                return (
                    f"Thank you. Based on your discharge date, your "
                    f"follow-up should be completed by "
                    f"{phf_follow_up_deadline}. Here is your provider's "
                    f"availability:\n{avail_text}\n"
                    f"Which day and time works best for you?"
                )
        else:
            phf_stage = "check_covering_availability"
            phf_covering_availability = generate_covering_provider_availability_within_7_days()
            if phf_covering_availability:
                avail_text = format_availability(phf_covering_availability)
                if phf_caller_type == "hospital_team":
                    return (
                        f"Your provider has no availability within 7 days. "
                        f"However, {COVERING_PROVIDER_CREDENTIAL} "
                        f"{COVERING_PROVIDER_BARE_NAME} has these openings:\n"
                        f"{avail_text}\n"
                        f"Which day and time would you like to schedule?"
                    )
                else:
                    return (
                        f"I'm sorry, your provider has no availability "
                        f"within the next 7 days. However, "
                        f"{COVERING_PROVIDER_CREDENTIAL} "
                        f"{COVERING_PROVIDER_BARE_NAME} can see you. "
                        f"Here are the available times:\n{avail_text}\n"
                        f"Would you like to schedule with "
                        f"{COVERING_PROVIDER_BARE_NAME}?"
                    )
            else:
                phf_work_in_requested = True
                if phf_caller_type == "hospital_team":
                    response = (
                        f"I'm sorry, but neither the provider nor "
                        f"{COVERING_PROVIDER_BARE_NAME} has any availability "
                        f"within the next 7 days. I will create a high "
                        f"priority work-in request."
                    )
                    # Hospital transition team has no callback branch per
                    # the workflow diagram, so this message is terminal.
                    # Without ending the flow here, the next hospital
                    # message would fall into the check_covering_availability
                    # yes/no handler (stage was left set to that value
                    # above) and get incorrectly asked for a callback
                    # number, looping the hospital workflow.
                    reset_state()
                    return response
                else:
                    # Bug fix: give this branch its own stage so the next
                    # message is routed straight to the work-in
                    # callback-collection block instead of falling through
                    # to check_covering_availability's yes/no handler.
                    phf_stage = "awaiting_work_in_callback"
                    return (
                        f"I'm sorry, but neither your provider nor "
                        f"{COVERING_PROVIDER_BARE_NAME} has any availability "
                        f"within the next 7 days. I will create a high "
                        f"priority work-in request for your provider. "
                        f"May I get a good callback number for you?"
                    )

    # Stage: Work-in callback collection (take precedence while waiting for callback)
    if phf_work_in_requested and not phf_callback_number and phf_caller_type != "hospital_team":
        phone_match = re.search(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b', message)
        if phone_match:
            phf_callback_number = phone_match.group(0)
            phf_stage = "complete"
            return (
                f"Thank you. I have created a high priority work-in "
                f"request and noted your callback number as "
                f"{phf_callback_number}. Someone from our office will be "
                f"in touch within 24 business hours. Is there anything "
                f"else I can help you with today?"
            )
        else:
            return "May I get a good callback number for you?"

    # Stage: Check PCP availability (patient/hospital team picks a slot
    # from the PCP's own availability presented in discharge_info above).
    # This handler was previously missing - discharge_info sets
    # phf_stage = "check_pcp_availability" and presents slots, but with
    # no matching block here the caller's day/time selection fell all
    # the way through to `return None`, handing the turn to the LLM with
    # no Python state advancement (stage stuck, appointment never
    # recorded) - the root cause of the reported context-loss/looping.
    if phf_stage == "check_pcp_availability":
        matched_slot = None
        if phf_pcp_availability:
            for date_key, times in phf_pcp_availability.items():
                if _offered_slot_day_matches(message, message_lower, date_key):
                    for time_slot in times:
                        if time_matches(message, time_slot):
                            matched_slot = f"{date_key} @ {time_slot}"
                            break
                    if matched_slot:
                        break

        if matched_slot:
            phf_appointment_day = matched_slot
            phf_stage = "complete"
            store_phf_appointment_record()
            if phf_caller_type == "hospital_team":
                return (
                    f"Perfect. I have scheduled the post-hospital follow-up "
                    f"for {phf_appointment_day}. Is there anything else I can "
                    f"help you with today?"
                )
            else:
                return (
                    f"Perfect. I have you scheduled for a post-hospital "
                    f"follow-up on {phf_appointment_day}. Please arrive a few "
                    f"minutes early to complete any necessary paperwork. "
                    f"Is there anything else I can help you with today?"
                )
        elif phf_pcp_availability:
            avail_text = format_availability(phf_pcp_availability)
            return (
                f"Which day and time works best for you?\n{avail_text}"
            )
        else:
            # Defensive fallback only - discharge_info already routes to
            # check_covering_availability whenever phf_pcp_availability
            # is empty, so this should not normally be reached.
            phf_stage = "check_covering_availability"

    # Stage: Check covering availability
    if phf_stage == "check_covering_availability":
        if "yes" in message_lower or "sure" in message_lower or "okay" in message_lower or "ok" in message_lower:
            if phf_covering_availability:
                avail_text = format_availability(phf_covering_availability)
                phf_stage = "schedule_confirm"
                return (
                    f"Great. Here are the available times with "
                    f"{COVERING_PROVIDER_BARE_NAME}:\n{avail_text}\n"
                    f"Which day and time works best for you?"
                )
            else:
                phf_work_in_requested = True
                return (
                    f"I'm sorry, {COVERING_PROVIDER_BARE_NAME} is no longer "
                    f"available. I will create a high priority work-in "
                    f"request. May I get a good callback number for you?"
                )
        elif (
                "no" in message_lower or "don't" in message_lower or "not" in message_lower
                or re.search(
            r"\b(i(?:'|’)d rather|i would rather|would rather|prefer (?:to )?(?:see )?(?:my )?(?:own )?provider|rather see (?:my )?(?:own )?provider|see my own provider)\b",
            message_lower)
        ):
            phf_work_in_requested = True
            return (
                f"I understand. I will create a high priority work-in "
                f"request for your provider. May I get a good callback "
                f"number for you?"
            )
        elif phf_covering_availability:
            matched_slot = None
            for date_key, times in phf_covering_availability.items():
                if _offered_slot_day_matches(message, message_lower, date_key):
                    for time_slot in times:
                        if time_matches(message, time_slot):
                            matched_slot = f"{date_key} @ {time_slot}"
                            break
                    if matched_slot:
                        break

            if matched_slot:
                phf_appointment_day = matched_slot
                phf_stage = "complete"
                store_phf_appointment_record()
                if phf_caller_type == "hospital_team":
                    return (
                        f"Perfect. I have scheduled the post-hospital follow-up "
                        f"for {phf_appointment_day}. Is there anything else I can "
                        f"help you with today?"
                    )
                else:
                    return (
                        f"Perfect. I have you scheduled for a post-hospital "
                        f"follow-up on {phf_appointment_day}. Please arrive a few "
                        f"minutes early to complete any necessary paperwork. "
                        f"Is there anything else I can help you with today?"
                    )
            else:
                avail_text = format_availability(phf_covering_availability)
                return (
                    f"Would you like to schedule with "
                    f"{COVERING_PROVIDER_BARE_NAME}? "
                    f"Here are the available times:\n{avail_text}"
                )
        else:
            phf_work_in_requested = True
            return (
                f"I'm sorry, no covering provider is available. "
                f"May I get a good callback number for a work-in request?"
            )

    # Stage: Schedule confirmation
    if phf_stage == "schedule_confirm":
        phf_appointment_day = message.strip()
        phf_stage = "complete"
        store_phf_appointment_record()
        if phf_caller_type == "hospital_team":
            return (
                f"Perfect. I have scheduled the post-hospital follow-up "
                f"for {phf_appointment_day}. Is there anything else I can "
                f"help you with today?"
            )
        else:
            return (
                f"Perfect. I have you scheduled for a post-hospital "
                f"follow-up on {phf_appointment_day}. Please arrive a few "
                f"minutes early to complete any necessary paperwork. "
                f"Is there anything else I can help you with today?"
            )

    # Stage: Work-in callback collection
    if phf_work_in_requested and not phf_callback_number:
        phone_match = re.search(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b', message)
        if phone_match:
            phf_callback_number = phone_match.group(0)
            phf_stage = "complete"
            return (
                f"Thank you. I have created a high priority work-in "
                f"request and noted your callback number as "
                f"{phf_callback_number}. Someone from our office will be "
                f"in touch within 24 business hours. Is there anything "
                f"else I can help you with today?"
            )
        else:
            return "May I get a good callback number for you?"

    # Stage: Complete
    if phf_stage == "complete":
        # Treat explicit 'no' responses (any punctuation/trailing words,
        # e.g. "no.", "no,", "no you..."), plus common goodbye phrasing,
        # as the caller indicating the call is finished. The previous
        # startswith() checks missed variants like "no," or "no!" and a
        # narrow phrase list missed plain goodbyes, so the flow kept
        # repeating "anything else?" instead of ending.
        stripped = message_lower.strip()
        goodbye_detected = (
                re.match(r'^no\b', stripped) is not None
                or any(phrase in message_lower for phrase in [
            "that will be all", "that's all", "that's it", "nothing else",
            "no i'm good", "no i'm all set", "i'm good", "i'm all set",
            "all set", "that will do", "no thank you", "no thanks",
            "nothing further", "i'm done", "that's everything",
            "goodbye", "good bye", "bye", "have a good day",
            "have a great day",
        ])
        )
        if goodbye_detected:
            response = "Thank you for calling. Have a wonderful day."
            # Reset PHF state so a later post-hospital trigger within the
            # same conversation starts clean instead of resuming from a
            # stale "complete" stage.
            reset_state()
            return response
        return "Is there anything else I can help you with today?"

    return None


# ─────────────────────────────────────────────
# Context builder for LLM injection
# ─────────────────────────────────────────────

def build_context():
    if not phf_flow_active and not phf_intent_detected:
        return ""

    context = "POST_HOSPITAL_FOLLOWUP INJECTED BY SYSTEM:\n"
    context += f"Stage: {phf_stage or 'initial'}\n"
    context += f"Caller type: {phf_caller_type or 'unknown'}\n"

    if phf_patient_first_name and phf_patient_last_name and phf_stage in ["get_patient_name", "discharge_info"]:
        context += f"Patient: {phf_patient_first_name} {phf_patient_last_name}\n"
    if phf_patient_pcp and phf_stage in ["get_patient_name", "discharge_info"]:
        context += f"PCP: {phf_patient_pcp}\n"
    if phf_hospital_name:
        context += f"Hospital: {phf_hospital_name}\n"
    if phf_discharge_date:
        context += f"Discharge date: {phf_discharge_date}\n"
    if phf_follow_up_deadline:
        context += f"Follow-up deadline: {phf_follow_up_deadline}\n"
    if phf_reason_for_admission:
        context += f"Reason for admission: {phf_reason_for_admission}\n"

    if phf_pcp_availability:
        context += "PCP_AVAILABILITY:\n"
        context += format_availability(phf_pcp_availability) + "\n"
    elif phf_stage in ["check_pcp_availability", "check_covering_availability"]:
        context += "PCP_AVAILABILITY: None within 7 days\n"

    if phf_covering_availability:
        context += "COVERING_PROVIDER_AVAILABILITY:\n"
        context += format_availability(phf_covering_availability) + "\n"
    elif phf_stage == "check_covering_availability":
        context += "COVERING_PROVIDER_AVAILABILITY: None within 7 days\n"

    if phf_work_in_requested:
        context += "WORK_IN_REQUESTED: True\n"
        context += "Do NOT offer scheduling. Collect callback number only.\n"

    if phf_reschedule_pending:
        context += "RESCHEDULE_PENDING: True\n"

    if phf_cancel_pending:
        context += "CANCEL_PENDING: True\n"

    if phf_caller_type == "hospital_team":
        context += (
            "HOSPITAL_TRANSITION_TEAM: True\n"
            "Do NOT ask for callback numbers. Schedule directly.\n"
        )

    if phf_existing_appt_day and phf_existing_appt_time:
        context += f"Existing appointment: {phf_existing_appt_day} at {phf_existing_appt_time}\n"

    context += (
        "CRITICAL: For post-hospital follow-ups, the follow-up must be "
        "scheduled within 7 days of discharge.\n"
        f"Covering provider: {COVERING_PROVIDER_CREDENTIAL} {COVERING_PROVIDER_BARE_NAME}\n"
        "If PCP is unavailable within 7 days, offer covering provider.\n"
        "If covering provider is also unavailable, create a high priority "
        "work-in request and collect a callback number (patient callers only).\n"
    )

    return context