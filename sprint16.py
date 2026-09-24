"""
Sprint 16: Fax Inquiry from Third-Party Representative Workflow

Business purpose: a representative involved in the patient's care (nurse
case manager, patient advocate, insurance rep, specialist office, referral
office, imaging facility, pharmacist, durable-medical-equipment
representative, or any healthcare-related office involved in treatment
coordination) contacts the office regarding a fax they previously sent,
requesting additional information/records/documentation/orders/notes/
diagnosis codes/approvals/referrals/imaging/DME paperwork/insurance
documentation, or similar materials.

ARCHITECTURE (per the Sprint 13/Sprint 14 convention already in this
codebase):
  - This file is SELF-CONTAINED and ISOLATED from Sprint13.py's PHF and
    Sprint14.py's wellness workflows. app.py owns routing and session
    control: it decides WHEN to call into this module (intent detection +
    dispatch, mirroring the exact pattern app.py already uses for
    Sprint13/Sprint14) and calls reset_state() at the start of each new
    call.
  - This module owns the workflow LOGIC and STATE. Where app.py already
    tracks identity that is safe to reuse (patient/caller name already
    collected), this module reads it directly from `app`'s globals the
    same way Sprint14.py does - no new dependency pattern.

WORKFLOW (per Sprint 16 requirements):
  1. Steve identifies the caller as a third-party representative.
  2. Steve obtains: patient first/last name and DOB.
  3. Steve asks what information/documentation was requested (or lets the
     representative describe the fax request).
  4. Steve randomly determines whether the fax was received:
        75% received / 25% not received.

  FAX RECEIVED PATH:
    - Steve confirms the fax request was received.
    - Steve gathers the fax number to send the requested items over to.
    - Steve confirms he has faxed over the requested information and
      closes the call.
    - No provider message is created.

  FAX NOT RECEIVED PATH:
    - Steve advises "I do not see a record of that fax being received."
    - If the representative requests office fax information, Steve
      provides the office fax number.
    - Steve asks the representative to resend the fax.
    - Steve collects a callback number and closes the call appropriately.
    - No provider message is created.

  NEVER: provide medical advice, provide diagnosis codes him/herself, read
  clinical documentation to the caller, confirm protected medical
  information beyond what the workflow needs, or invent records that do
  not exist.
"""

import os
import random
import re


# ─────────────────────────────────────────────
# State variables
# ─────────────────────────────────────────────

fax_intent_detected = False
fax_flow_active = False
fax_stage = None  # None | "collect_patient" | "collect_dob" |
                  # "collect_request" | "received_callback" |
                  # "not_received" |
                  # "not_received_callback" | "closed"

fax_patient_first = None
fax_patient_last = None
fax_patient_dob = None
fax_request_details = None

# Set to True/False ONCE (random 75/25) when the request details turn
# completes; used by every subsequent stage.
fax_received = None

fax_callback_number = None
fax_deadline = None
fax_priority = None  # "high" | "normal"
fax_fax_number_provided = False
fax_office_number = None  # Randomized per call (see _office_fax_number).

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

OFFICE_FAX_NUMBER = "321-555-0199"

# Third-party fax-inquiry detection. Two complementary routes cover the
# natural phrasings representatives use:
#   1. An explicit fax mention ("fax"/"faxed"/"facsimile"/"fax request")
#      combined with follow-up/inquiry framing ("checking on", "did you
#      receive", "regarding the fax", ...).
#   2. A records/documentation request ("records request", "medical
#      records", "documentation", "office note", "diagnosis code",
#      "imaging", "insurance documentation", "referral documentation",
#      "dme paperwork") combined with a send/request frame ("request",
#      "sent", "follow up on", "checking on", ...).
# Patient-facing lab phrasing ("did my results come in", "fax my lab
# results to Quest") matches neither route: it has no inquiry frame and no
# docs-request frame, so those existing workflows are untouched.
_FAX_WORDS = (
    "fax", "faxed", "facsimile", "fax request", "fax inquiry",
)

_FAX_INQUIRY_FRAMING = [
    "checking on a fax", "checking on the fax", "check on a fax",
    "check on the fax", "check an", "following up on a fax",
    "follow up on a fax", "follow up on the fax", "followed up on the fax",
    "did you receive", "have you received", "did you guys receive",
    "did you get", "received the fax", "receive the fax",
    "received our fax", "receive our fax", "regarding the fax",
    "about the fax", "the fax we sent", "fax we sent",
    "sent a fax", "sent you a fax", "sent over a fax",
    "sent us a fax", "we sent over a fax", "sent the fax",
    "resend", "resending", "resend the fax", "our fax",
    "for the fax", "in reference to the fax", "status of a fax",
    "status of the fax", "checking on our fax", "fax we faxed",
    "did the office receive", "did your office receive",
    "did the doctor's office receive", "did the team receive",
    "did your practice receive", "did you folks receive",
    "receive that request", "receive the request",
    "receive this request", "did you get that request",
]

_DOC_REQUEST_WORDS = [
    "records request", "records request we", "records we",
    "medical records", "request for records", "request for documentation",
    "requesting records", "requesting documentation",
    "records", "documentation", "documents", "office note", "office notes",
    "visit note", "visit notes", "diagnosis code", "diagnosis codes",
    "imaging", "imaging study", "imaging studies",
    "insurance documentation", "insurance documents",
    "referral documentation", "referral paperwork", "dme paperwork",
    "dme documentation", "approval", "approvals", "prior authorization",
    "appointment note", "appointment notes", "latest labs", "latest lab",
    "lab results", "lab report", "latest ekg", "ekg", "ekgs",
    "calcium scoring", "scoring test", "scoring tests",
    "you have on file", "you may have on file", "have on file", "on file",
]

_DOC_REQUEST_FRAMING = [
    "request", "requesting", "requested", "sent", "sent over", "sent in",
    "did you receive", "have you received", "follow up on", "followed up on",
    "checking on", "check on", "we need", "we are requesting",
    "we're requesting", "we were requesting", "we faxed", "we sent",
    "looking for",
]

_PATIENT_NAME_PATTERN = re.compile(
    r"(?:the patient(?:'s)? name is|the patient is|patient(?:'s)? name is|"
    r"patient is)\s+([A-Z][a-z]+)\s+([A-Z][a-z]+)",
    re.IGNORECASE,
)

# Dense intros ("...for patient Alice Johnson DOB 1/2/1960...") don't use
# the "the patient's name is" scaffolding. Only safe when the message also
# contains a DOB - otherwise "patient medical records" would be captured
# as a name.
_PATIENT_FOR_NAME_PATTERN = re.compile(
    r"(?:for\s+|about\s+|on\s+)patient\s+([A-Z][a-z]+)\s+([A-Z][a-z]+)",
    re.IGNORECASE,
)

# Tokens that are structurally "the name is Bob Barker" scaffolding or
# common stop words - never a real first/last name (mirrors app.py's own
# _MED_PRO_NON_NAME_WORDS idea).
_NON_NAME_WORDS = {
    "name", "is", "are", "of", "and", "for", "from", "about",
    "regarding", "with", "my", "our", "your", "his", "her", "their",
    "the", "to", "a", "an", "patient", "caller", "am", "i",
    "did", "you", "receive", "received", "fax", "request",
}

_DOB_PATTERN = re.compile(
    r'\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b|'
    r'\b(january|february|march|april|may|june|july|august|'
    r'september|october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?'
    r'(?:,\s*|\s+)\d{4}\b',
    re.IGNORECASE,
)

_PHONE_PATTERN = re.compile(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b')

# Deadline phrasing that marks a request as HIGH priority (needed within
# 48 hours / 2 days or less).
_URGENT_DEADLINE_PHRASES = [
    "48 hours", "48hrs", "48 hrs", "within 48", "48-hour",
    "2 days", "two days", "2 day", "within 2", "within two",
    "within the next 48", "by tomorrow", "tomorrow", "asap",
    "as soon as possible", "immediately", "right away", "urgent",
    "this week", "by friday", "today", "quickly", "needed by tomorrow",
]

# Phrasing that explicitly says there is NO deadline (NORMAL priority).
_NO_DEADLINE_PHRASES = [
    "no deadline", "no particular deadline", "no specific deadline",
    "no rush", "whenever", "not urgent", "no hurry", "take your time",
    "no timeframe", "no time frame", "no date",
]

_ASK_FAX_NUMBER_PHRASES = [
    "fax number", "fax info", "fax information", "resend to",
    "what number do i fax", "what number do i send", "send it to",
    "where do i fax", "number to fax", "fax it to",
]

_GOODBYE_PHRASES = [
    "goodbye", "good bye", "bye", "have a great day", "thank you",
    "thanks", "no", "nothing else", "that's all", "thats all",
    "all set", "not right now", "no thank you", "no thanks",
]


# ─────────────────────────────────────────────
# State reset
# ─────────────────────────────────────────────

def reset_state():
    global fax_intent_detected, fax_flow_active, fax_stage
    global fax_patient_first, fax_patient_last, fax_patient_dob
    global fax_request_details, fax_received
    global fax_callback_number, fax_deadline, fax_priority
    global fax_fax_number_provided, fax_office_number

    fax_intent_detected = False
    fax_flow_active = False
    fax_stage = None
    fax_patient_first = None
    fax_patient_last = None
    fax_patient_dob = None
    fax_request_details = None
    fax_received = None
    fax_callback_number = None
    fax_deadline = None
    fax_priority = None
    fax_fax_number_provided = False
    fax_office_number = None


# ─────────────────────────────────────────────
# Intent detection (called by app.py for routing)
# ─────────────────────────────────────────────

def detect_fax_inquiry_intent(message_lower):
    if any(w in message_lower for w in _FAX_WORDS) and (
        any(f in message_lower for f in _FAX_INQUIRY_FRAMING)
    ):
        return True
    if any(d in message_lower for d in _DOC_REQUEST_WORDS) and (
        any(f in message_lower for f in _DOC_REQUEST_FRAMING)
    ):
        return True
    return False


# ─────────────────────────────────────────────
# UAT / harness override
# ─────────────────────────────────────────────

def _determine_fax_received():
    """Random 75/25 for whether the fax was received, with an environment-
    variable override (STEVE_FORCE_FAX_RECEIVED=true/false) so UAT can
    test both branches deterministically. Mirrors app.py's
    STEVE_FORCE_REFERRAL_FOUND harness pattern."""
    forced = os.environ.get("STEVE_FORCE_FAX_RECEIVED")
    if forced is not None and forced.lower() in ("true", "false", "1", "0", "yes", "no"):
        return forced.lower() in ("true", "1", "yes")
    return random.random() < 0.75


# ─────────────────────────────────────────────
# Small identity / text helpers (local copies - see architecture note)
# ─────────────────────────────────────────────

def detect_dob_in_message(message):
    return bool(_DOB_PATTERN.search(message))


def extract_dob_from_message(message):
    match = _DOB_PATTERN.search(message)
    return match.group(0) if match else None


def _extract_patient_name(message):
    """Returns (first, last) or (None, None). Mirrors app.py's med-pro
    name extraction: named patterns first, then a "for patient X Y" intro
    (guarded by a DOB also being present), then a bare "Firstname
    Lastname" reply."""
    m = _PATIENT_NAME_PATTERN.search(message)
    if m:
        return m.group(1), m.group(2)
    if detect_dob_in_message(message):
        fm = _PATIENT_FOR_NAME_PATTERN.search(message)
        if fm and fm.group(1).lower() not in _NON_NAME_WORDS and (
                fm.group(2).lower() not in _NON_NAME_WORDS
        ):
            return fm.group(1), fm.group(2)
    parts = message.strip().rstrip(".!?,").split()
    if (
        len(parts) == 2
        and parts[0][0].isupper()
        and parts[1][0].isupper()
        and parts[0].lower() not in _NON_NAME_WORDS
        and parts[1].lower() not in _NON_NAME_WORDS
    ):
        return parts[0], parts[1]
    return None, None


def _extract_phone(message):
    match = _PHONE_PATTERN.search(message)
    return match.group(0) if match else None


def _pull_identity_from_app():
    """Best-effort reuse of identity app.py already collected (pre-chart
    patient, or an earlier handle_med_pro_collection run)."""
    first = last = dob = None
    try:
        import app as _caller_app
        first = getattr(_caller_app, "patient_first_name", None) or getattr(
            _caller_app, "caller_first_name", None)
        last = getattr(_caller_app, "patient_last_name", None) or getattr(
            _caller_app, "caller_last_name", None)
        if getattr(_caller_app, "med_pro_patient_first", None):
            first = _caller_app.med_pro_patient_first
        if getattr(_caller_app, "med_pro_patient_last", None):
            last = _caller_app.med_pro_patient_last
        if getattr(_caller_app, "med_pro_patient_dob", None):
            dob = _caller_app.med_pro_patient_dob
    except Exception:
        pass
    return first, last, dob


_REQUEST_VERB_PHRASES = [
    "request", "requesting", "requested", "looking for", "need",
    "sent", "sent over", "sent us", "sent you", "faxed over",
]

# Leading connective words to strip off a freshly-extracted request
# description ("for the patient's", "the patient's", "his", "her"...).
_DESCRIPTION_LEAD_STRIPS = (
    " for the patient's", " for the patient", " the patient's",
    " the patient", " for his", " for her", " for the", " regarding",
    " about", " the", " a", " an", " his", " her", " their", " some",
)

# Trailing subclauses to cut off a freshly-extracted request description
# so follow-up phrasing ("did you receive it?", "did the office receive
# that request?") doesn't leak into the stored provider-message details.
_DESCRIPTION_TAIL_CUTS = (
    " did the office receive", " did your office receive",
    " did the doctor's office receive", " did you all receive",
    " did the team receive", " did you folks receive",
    " did you receive", " have you received", " did you get",
    " receive that request", " receive the request",
    " receive this request", " date of birth", " for patient", " dob",
    " did you", " do you", " when", " if you", " please", " thank you",
    " ok thanks", " okay thanks", " thanks",
    " you may have on file", " may have on file", " have on file",
    " for him", " for her", " on file",
)


def _clean_request_details(message, message_lower):
    """Extract a readable request noun-phrase out of the caller's words
    instead of storing the whole message (the intro message also contains
    the caller's name, their employer, and the "did you receive it"
    framing - none of which belongs in the provider message). Returns the
    cleaned description, or the raw trimmed message if nothing can be
    pulled out."""
    low = message_lower
    # First cut off any trailing follow-up subclause in the WHOLE message
    # ("We faxed over a request for the latest EKG ... did the office
    # receive that request?") so a closing "request?" can't become the
    # verb anchor below.
    for cut in _DESCRIPTION_TAIL_CUTS:
        idx = low.find(cut)
        if idx != -1:
            message = message[:idx]
            low = low[:idx]
            break
    pos = -1
    for v in _REQUEST_VERB_PHRASES:
        idx = low.rfind(v)
        if idx > pos:
            pos = idx
    if pos != -1:
        tail = message[pos:]
        for v in sorted(_REQUEST_VERB_PHRASES, key=len, reverse=True):
            if tail.lower().startswith(v):
                tail = tail[len(v):]
                break
    else:
        positions = [
            low.find(d) for d in _DOC_REQUEST_WORDS if d in low
        ]
        if positions:
            tail = message[min(positions):]
        else:
            tail = message
    tail = tail.strip()
    for lead in _DESCRIPTION_LEAD_STRIPS:
        if tail.lower().startswith(lead):
            tail = tail[len(lead):].lstrip()
    for cut in _DESCRIPTION_TAIL_CUTS:
        idx = tail.lower().find(cut)
        if idx != -1:
            tail = tail[:idx].rstrip()
    return tail.strip().strip(".,!?")


def _try_capture_request_details(message, message_lower, require_identity=False):
    """If the caller already described the request inside the current
    message, hold onto it so Steve does not redundantly re-ask on the
    next turn. When require_identity is True (intro/getting-identity
    turns), the description is only captured if the caller ALSO stated
    the patient DOB in the same message OR the patient identity is
    already known (e.g. Sprint16 reused it from a completed
    medical-professional referral/collection flow) - otherwise a bare
    introduction ("We sent a fax requesting the patient's medical
    records...") would be stored verbatim as the request details.
    Returns the stored details (or None)."""
    global fax_request_details
    if fax_request_details:
        return fax_request_details
    if require_identity and not (
        fax_patient_dob
        or detect_dob_in_message(message)
    ):
        return None
    if not (any(d in message_lower for d in _DOC_REQUEST_WORDS) or "request" in message_lower):
        return None
    fax_request_details = _clean_request_details(message, message_lower)
    return fax_request_details


def _is_urgent_deadline(message_lower):
    if any(p in message_lower for p in _NO_DEADLINE_PHRASES):
        return False
    if any(p in message_lower for p in _URGENT_DEADLINE_PHRASES):
        return True
    # Absolute date given ("by 5/14", "May 14th") - urgent if within 2 days.
    date_match = re.search(
        r'\b(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})\b', message_lower
    )
    if date_match:
        try:
            from datetime import datetime
            month, day, year = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
            if year < 100:
                year += 2000
            due = datetime(year, month, day).date()
            return (due - datetime.now().date()).days <= 2
        except ValueError:
            return False
    return False


def _asks_for_fax_number(message_lower):
    return any(p in message_lower for p in _ASK_FAX_NUMBER_PHRASES)


def _office_fax_number():
    """Office fax number Steve quotes when a fax was NOT received. Per the
    Sprint 16 refinement, this is generated randomly once per call instead
    of always being the same constant (with a STEVE_FORCE_FAX_NUMBER
    override for UAT/tests)."""
    global fax_office_number
    if fax_office_number is not None:
        return fax_office_number
    forced = os.environ.get("STEVE_FORCE_FAX_NUMBER")
    if forced:
        fax_office_number = forced
    else:
        fax_office_number = "321-555-0" + str(random.randint(100, 999))
    return fax_office_number


# "any [name of diagnostic-test] you have on file" phrasing that the
# Sprint 16 refinement treats specially (see _extract_on_file_items).
_ON_FILE_ITEM_PATTERN = re.compile(
    r'any\s+([A-Za-z0-9][A-Za-z0-9 \-]{1,40}?)\s+'
    r'(?:that\s+)?(?:you\s+)?(?:may\s+|might\s+|could\s+)?'
    r'(?:have\s+)?on\s+file',
    re.IGNORECASE,
)


def _extract_on_file_items(message, message_lower):
    """Find "any [diagnostic test] you have on file" items in the caller's
    words. Returns a list of the items (e.g. "calcium scoring test"),
    deduplicated in first-seen order. Used by the RECEIVED path: for every
    such item Steve rolls 50/50 whether it is on the electronic chart."""
    text = message_lower
    if fax_request_details:
        text += " " + fax_request_details.lower()
    items = []
    for m in _ON_FILE_ITEM_PATTERN.finditer(text):
        item = m.group(1).strip()
        if item and item.lower() not in (i.lower() for i in items):
            items.append(item)
    return items


def _determine_item_on_chart():
    """Random 50/50 for whether a specific "any [test] you have on file"
    item is on the electronic chart, with a STEVE_FORCE_TEST_ON_CHART
    override (true/false) for UAT/tests. Mirrors the fax-received harness
    pattern."""
    forced = os.environ.get("STEVE_FORCE_TEST_ON_CHART")
    if forced is not None and forced.lower() in ("true", "false", "1", "0", "yes", "no"):
        return forced.lower() in ("true", "1", "yes")
    return random.random() < 0.5


def _compose_received_confirmation(message, message_lower):
    """RECEIVED-path confirmation. Confirms the fax was received and that
    Steve will fax the requested items over, asking for a good fax
    number to send them to. For any "any [test] you have on file" item,
    Steve rolls 50/50 whether that item is on the electronic chart and
    states the result for each."""
    parts = [
        "Yes we did receive it. I can go ahead and fax these over "
        "to you."
    ]
    for item in _extract_on_file_items(message, message_lower):
        if _determine_item_on_chart():
            parts.append(
                f"Also, the {item} you asked about is on the electronic "
                f"chart."
            )
        else:
            parts.append(
                f"One note on the {item} you asked about: that is not "
                f"currently showing on the electronic chart."
            )
    parts.append("May I have a good fax number for you?")
    return " ".join(parts)


def _is_goodbye(message_lower):
    return any(p in message_lower for p in _GOODBYE_PHRASES)


# ─────────────────────────────────────────────
# Workflow handler (called by app.py while fax_flow_active)
# ─────────────────────────────────────────────

def handle_fax_flow(message, message_lower):
    """Deterministic state machine. Returns the Steve response string, or
    None to let app.py fall through (never happens while the flow is
    active - every stage is answered here so the caller never leaks into
    the LLM mid-flow)."""
    global fax_stage
    global fax_patient_first, fax_patient_last, fax_patient_dob
    global fax_request_details, fax_received
    global fax_callback_number, fax_deadline, fax_priority
    global fax_fax_number_provided

    patient_full = None
    if fax_patient_first and fax_patient_last:
        patient_full = f"{fax_patient_first} {fax_patient_last}"

    if fax_stage is None or fax_stage == "collect_patient":
        if not fax_patient_first or not fax_patient_last:
            first, last, dob = _pull_identity_from_app()
            if first and last:
                fax_patient_first, fax_patient_last = first, last
            if dob and not fax_patient_dob:
                fax_patient_dob = dob
        if not fax_patient_first or not fax_patient_last:
            first, last = _extract_patient_name(message)
            if first and last:
                fax_patient_first, fax_patient_last = first, last
        if not fax_patient_dob and detect_dob_in_message(message):
            fax_patient_dob = extract_dob_from_message(message)
        _try_capture_request_details(message, message_lower, require_identity=True)

        if not fax_patient_first or not fax_patient_last:
            return (
                "Can I get the patient's first and last name and "
                "date of birth for this request?"
            )
        if not fax_patient_dob:
            fax_stage = "collect_dob"
            return f"Thank you. Could I get {fax_patient_first}'s date of birth?"
        fax_stage = "collect_request"
        if not fax_request_details:
            return (
                "Thank you. And what information or documentation are "
                "you requesting for this patient?"
            )
        return _advance_from_request(message, message_lower)

    if fax_stage == "collect_dob":
        if detect_dob_in_message(message):
            fax_patient_dob = extract_dob_from_message(message)
            fax_stage = "collect_request"
            _try_capture_request_details(message, message_lower)
            if not fax_request_details:
                return (
                    "Thank you. And what information or documentation "
                    "are you requesting for this patient?"
                )
            return _advance_from_request(message, message_lower)
        return (
            f"Could I get {fax_patient_first if fax_patient_first else 'the patient'}'s "
            f"date of birth?"
        )

    if fax_stage == "collect_request":
        if not fax_request_details:
            _try_capture_request_details(message, message_lower)
        if not fax_request_details:
            return "What information or documentation are you requesting?"
        return _advance_from_request(message, message_lower)

    if fax_stage == "received_callback":
        phone = _extract_phone(message)
        if not phone:
            return "May I have a good fax number for you?"
        fax_callback_number = phone
        fax_stage = "closed"
        details = fax_request_details or "the requested information"
        if details.lower().startswith("for "):
            details = details[4:]
        return (
            f"I have faxed over {details}. Is there anything else I can "
            f"help you with today?"
        )

    if fax_stage == "not_received":
        phone = _extract_phone(message)
        asks_fax_number = _asks_for_fax_number(message_lower)
        if phone:
            fax_callback_number = phone
            fax_stage = "closed"
            base = (
                f"Our office fax number is {_office_fax_number()}. "
                if asks_fax_number else ""
            )
            return (
                base +
                "Thank you. We'll be on the lookout for the resend. "
                "Is there anything else I can help you with today?"
            )
        if asks_fax_number:
            fax_fax_number_provided = True
            return (
                f"Our office fax number is {_office_fax_number()}. Please "
                f"resend the fax when you get a chance. May I get a "
                f"good callback number for our team to follow up?"
            )
        fax_stage = "not_received_callback"
        return (
            "Thank you. May I get a good callback number for our team "
            "to follow up if needed?"
        )

    if fax_stage == "not_received_callback":
        phone = _extract_phone(message)
        asks_fax_number = _asks_for_fax_number(message_lower)
        if phone:
            fax_callback_number = phone
            fax_stage = "closed"
            base = (
                f"Our office fax number is {_office_fax_number()}. "
                if asks_fax_number and not fax_fax_number_provided else ""
            )
            return (
                base +
                "Thank you. We'll be on the lookout for the resend. "
                "Is there anything else I can help you with today?"
            )
        if asks_fax_number and not fax_fax_number_provided:
            fax_fax_number_provided = True
            return (
                f"Our office fax number is {_office_fax_number()}. Please "
                f"resend the fax when you get a chance. May I get a "
                f"good callback number for our team to follow up?"
            )
        return "May I get a good callback number for our team to follow up?"

    if fax_stage == "closed":
        if _is_goodbye(message_lower):
            return "Thank you for calling. Have a great day!"
        if fax_received:
            return (
                "Is there anything else I can help you with today? "
                "The message has been created and our team is on it."
            )
        return "Is there anything else I can help you with today?"

    return None


def _advance_from_request(message, message_lower):
    """Shared completion of the identity+request stages: randomly
    determine whether the fax was received (75/25, once) and begin the
    appropriate branch."""
    global fax_received, fax_stage
    if fax_received is None:
        fax_received = _determine_fax_received()
    if fax_received:
        fax_stage = "received_callback"
        return _compose_received_confirmation(message, message_lower)
    fax_stage = "not_received"
    return (
        "I do not see a record of that fax being received. Could you "
        "please resend the fax to our office? I can provide our office "
        "fax number if you need it. May I get a good callback number "
        "for our team to follow up?"
    )


# ─────────────────────────────────────────────
# Context builder for LLM injection
# ─────────────────────────────────────────────

def build_context():
    if not fax_flow_active:
        return ""

    context = "FAX_INQUIRY_WORKFLOW INJECTED BY SYSTEM:\n"
    context += f"Stage: {fax_stage or 'initial'}\n"
    if fax_received is not None:
        context += f"Fax received: {fax_received}\n"
    if fax_priority:
        context += f"Message priority: {fax_priority}\n"
    if fax_request_details:
        context += f"Request details: {fax_request_details}\n"
    context += (
        "NEVER provide medical advice, provide diagnosis codes, read "
        "clinical documentation to the caller, or confirm protected "
        "medical information beyond what this workflow needs. Take "
        "messages, route requests, collect callback numbers, and provide "
        "fax information when appropriate.\n"
    )
    return context
