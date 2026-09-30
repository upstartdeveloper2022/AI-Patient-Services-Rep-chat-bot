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

  FAX RECEIVED PATH (directional - see is_fax_receipt_question):
    A fax inquiry has two opposite directions and the received path
    answers each one in its own direction:
    - RECEIPT QUESTION ("did you receive the fax we sent?"): Steve
      confirms the fax was received, names the requested items so the
      caller can tell WHICH fax arrived, and closes. The outbound send
      offer and the fax-number ask belong to the other direction and are
      never attached automatically to a receipt question. A further
      business need the caller states is served by the workflow that owns
      that need, not by an assumed fax-back; if the caller then supplies a
      destination or asks for the documents, the outbound path below
      resumes unchanged.
    - OUTBOUND REQUEST ("fax X to us"): Steve offers to fax the
      requested items over, gathers the fax number to send them to, and
      confirms he has faxed over the requested information and closes the
      call.
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
                  # "collect_request" | "receipt_answered" |
                  # "receipt_answered_callback" | "received_callback" |
                  # "not_received" | "not_received_callback" | "closed"

fax_patient_first = None
fax_patient_last = None
fax_patient_dob = None
fax_request_details = None

# Set to True/False ONCE (random 75/25) when the request details turn
# completes; used by every subsequent stage.
fax_received = None

# RETIRED. This used to be set for a fax-inquiry message from a
# medical-professional caller whose identity collection already completed,
# and it forced receipt to True instead of rolling the 75/25 - so the roll
# never actually ran for exactly the pharmacy/professional callers this
# workflow exists for. Nothing reads it any more; _determine_fax_received
# always rolls. Kept only so stale references fail loudly rather than
# silently reintroducing the bypass.
fax_verified_receipt = False

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
    # Receipt-confirmation phrasings. A pharmacy asking the office to
    # confirm a fax it already sent ("...Can you confirm you received that
    # fax?", "...Just checking on that fax.", "...Has it arrived?") states
    # the SAME immediate question as "did you receive it" and must be owned
    # by this workflow, not fall through to the LLM (which was answering
    # "I cannot verify whether that fax was received"). Deliberately
    # receipt-only: no "can you fax ..." sending phrasing, so patient-facing
    # lab phrasing ("can you fax my lab results to Quest") still misses.
    "can you confirm you received", "confirm you received",
    "confirm receipt", "confirm that you received",
    "checking on that fax", "checking on this fax",
    "did it come through", "come through",
    "has it arrived", "did it arrive", "have you seen it",
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

# ── Direction: receipt question vs. outbound send request ──
# The two framing lists above are deliberately broad, so they match BOTH
# kinds of fax inquiry:
#
#   * outbound  "we need the recent x ray faxed over"  -> Steve must offer
#     to fax something, which requires patient identity + the item first.
#   * inbound   "we faxed you yesterday, did you receive it?" -> Steve is
#     only being asked whether something already arrived. Identity and the
#     requested item are NOT needed to answer that, and asking for them
#     first is what let this turn escape the workflow (see
#     handle_fax_flow / is_fax_receipt_question below).
#
# So the inbound direction is detected explicitly here, from the
# receipt-question forms only. This is a direction rule over a closed set of
# question shapes - not a per-phrasing patch - so a pharmacy/prescription/
# clarification sentence still resolves through the normal 75/25 roll.
_RECEIPT_QUESTION_FRAMING = (
    "did you receive", "did you guys receive", "did you folks receive",
    "have you received", "did you get", "did you get that request",
    "did the office receive", "did your office receive",
    "did the doctor's office receive", "did the team receive",
    "did your practice receive",
    "can you confirm you received", "confirm you received",
    "confirm receipt", "confirm that you received",
    "did it come through", "come through",
    "has it arrived", "did it arrive", "have you seen it",
    "received the fax", "receive the fax", "received our fax",
    "receive our fax", "receive that request", "receive the request",
    "receive this request",
    "the fax we sent", "fax we sent", "fax we faxed", "sent a fax",
    "sent you a fax", "sent over a fax", "sent us a fax",
    "we sent over a fax", "sent the fax", "our fax",
    "regarding the fax", "about the fax", "in reference to the fax",
    "status of a fax", "status of the fax",
    "checking on a fax", "checking on the fax", "check on a fax",
    "check on the fax", "following up on a fax", "follow up on a fax",
    "follow up on the fax", "followed up on the fax",
    "checking on that fax", "checking on this fax",
    "resend", "resending",
)

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

# The FAREWELL half of _GOODBYE_PHRASES. _is_goodbye also matches
# gratitude ("thank you", "thanks") and bare yes/no ("no"), which answer a
# question without ending the call - "thanks" after a receipt confirmation
# means "yes, we got it", and the caller's follow-on business need usually
# comes on the NEXT turn. Used by the receipt_answered stage, where closing
# on an acknowledgement would swallow that need.
_CALL_ENDING_PHRASES = (
    "goodbye", "good bye", "bye", "have a great day", "nothing else",
    "that's all", "thats all", "all set",
)


# ─────────────────────────────────────────────
# State reset
# ─────────────────────────────────────────────

def reset_state():
    global fax_intent_detected, fax_flow_active, fax_stage
    global fax_patient_first, fax_patient_last, fax_patient_dob
    global fax_request_details, fax_received
    global fax_callback_number, fax_deadline, fax_priority
    global fax_fax_number_provided, fax_office_number
    global fax_verified_receipt

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
    fax_verified_receipt = False


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


_RECEIPT_STEM_PATTERN = re.compile(
    r'\breceiv\w*\b|\breceipt\b|\barriv\w*\b|\b(?:come|came)\s+through\b|'
    r'\bgot\s+(?:it|that|this|the\s+fax)\b',
    re.IGNORECASE,
)


def is_fax_receipt_question(message_lower):
    """True when this turn asks whether a fax Steve was already sent has
    arrived ("we faxed X, did you receive it?"). Directional sibling of
    detect_fax_inquiry_intent: true for every inbound fax inquiry, false for
    the outbound "fax this to me" asks that still need identity + items.

    Used by handle_fax_flow to resolve the question with the existing 75/25
    roll on the turn it is asked, instead of first prompting for a patient
    name and DOB the answer does not depend on.

    RT16-11 root-cause fix: the literal list below matches exact substrings
    only, so an ordinary variation such as "...I'm calling to see if you
    received that fax" ("received that", not "received the"/"did you
    receive") missed every entry and the flow fell into collect_request,
    asking for documentation the caller had already described. The RECEIVE/
    ARRIVE word stem is the real signal, so it is matched first and covers
    every tense, inflection, and word order at once. The literal list is
    kept unchanged for shapes with no such stem (e.g. "resend", "our fax")."""
    if _RECEIPT_STEM_PATTERN.search(message_lower):
        return True
    return any(f in message_lower for f in _RECEIPT_QUESTION_FRAMING)


# ─────────────────────────────────────────────
# UAT / harness override
# ─────────────────────────────────────────────

def _determine_fax_received():
    """Random 75/25 for whether the fax was received, with an environment-
    variable override (STEVE_FORCE_FAX_RECEIVED=true/false) so UAT can
    test both branches deterministically. Mirrors app.py's
    STEVE_FORCE_REFERRAL_FOUND harness pattern.

    There is deliberately NO "already verified" bypass here. A completed
    medical-professional call used to force receipt to True, which meant
    this 75/25 never actually ran for exactly the pharmacy/professional
    callers this workflow exists for. The requirement is that this roll
    decides the outcome for every fax inquiry; only the harness override
    may pre-empt it."""
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

# A request verb is matched as a WHOLE WORD with its inflection included, so
# "needs"/"needed"/"needing" anchor and strip as themselves. The anchor used
# to be a bare substring search (rfind) and the verb was then removed with
# tail[len(v):], so "needs" matched "need" and the slice left the orphan "s",
# which became the first word of the stored description ("s the CPAP and I
# was really just trying to fugure out"). When two verb phrases start at the
# same index ("requesting" is both "request" and "requesting") the longest
# span wins, so the inflection is removed rather than truncated.
_REQUEST_VERB_PATTERNS = tuple(
    (verb, re.compile(r"\b" + re.escape(verb) + r"(?:s|ed|ing)?\b",
                      re.IGNORECASE))
    for verb in _REQUEST_VERB_PHRASES
)

# A request anchor introduces the requested documents with "for": "a request
# for the labs", "requesting the office note". A verb inside the caller's own
# prose does not ("he needs the CPAP and I was just trying to fugure out when
# the machine ships"), which is why the last anchor that qualifies this way is
# preferred over a later plain verb match. Falls back to the last match of any
# kind when nothing qualifies, so a request stated without "for" still anchors
# ("we need her office notes").
_REQUEST_ANCHOR_OBJECT = re.compile(r"^for\b", re.IGNORECASE)

# End of the list of requested documents, when the caller keeps talking after
# naming them: "..., and the CPAP office note, he needs the CPAP and I was
# really just trying to fugure out when the machine can be shipped."
#
# Deliberately requires a comma followed by a SUBJECT PRONOUN and never a bare
# "and": the items are a list ("a diagnosis code, and the CPAP office note"),
# so cutting at "and" would truncate the very documents the caller listed.
# Applied to the ANCHORED description only, not to the whole message - a
# request stated after such a clause ("I faxed yesterday, we sent a request
# for the labs") would otherwise be dropped with the clause.
_CLAUSE_BOUNDARY_PATTERN = re.compile(
    r",\s*(?:i|we|he|she|they)\b", re.IGNORECASE
)

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
# The list is scanned in order and the first hit wins, so a cut listed
# before the receipt-framing cuts is applied to the WHOLE message: that
# matters when a reason clause and a receipt question sit in the same
# sentence.
_DESCRIPTION_TAIL_CUTS = (
    # Reason clauses stating WHY the documents are needed, in the shape
    # "...and the CPAP office note addressing why the patient needs a CPAP
    # machine". These must be cut before the receipt-framing cuts below,
    # because otherwise the framing cut removes only the trailing question
    # and the cleaner then anchors on the "need" inside "needs a CPAP
    # machine", storing "s a CPAP machine" as the list of received items.
    " addressing why", " stating why", " explaining why", " to explain why",
    " saying why", " noting why",
    " did the office receive", " did your office receive",
    " did the doctor's office receive", " did you all receive",
    " did the team receive", " did you folks receive",
    " did you receive", " have you received", " did you get",
    " receive that request", " receive the request",
    " receive this request", " date of birth", " for patient", " dob",
    # Same receipt-confirmation tails as the framing list above. Without
    # these, Steve's confirmation parroted the question back as one of the
    # "requested items" ("...fax these over to you: clarification on the
    # prescription directions. Can you confirm you received that fax.").
    # Kept specific-before-generic so the existing " did you receive" cut
    # still wins over the bare " did you" fallback below.
    " can you confirm", " confirm you received",
    " confirm that you received", " confirm receipt",
    " just checking on that fax", " checking on that fax",
    " checking on this fax", " did it come through",
    " has it arrived", " did it arrive", " have you seen it",
    " did you", " do you", " when", " if you", " please", " thank you",
    " ok thanks", " okay thanks", " thanks",
    " you may have on file", " may have on file", " have on file",
    " for him", " for her", " on file",
    # Documentation-request trailers that state WHY the docs are needed
    # ("...discussing the neck pain as per required by the patient's
    # insurance?") - not part of what Steve will fax over, so they must not
    # leak into the stored request details that get echoed back.
    " as per required by the patient's insurance",
    " as required by the patient's insurance",
    " per required by the patient's insurance",
    " as required by their insurance", " per their insurance",
    " per the patient's insurance", " as per the insurance",
    " as required by insurance",
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
    # Anchor the requested documents on a request verb. RT16-12: the anchor
    # used to be the LAST bare-substring match anywhere in the message, so in
    # a run-on caller turn ("...the CPAP office note, he needs the CPAP and I
    # was really just trying to fugure out ...") the verb inside the caller's
    # prose won over the real request verb and the caller's trailing sentence
    # was stored as the list of received documents. Matches are whole words
    # including the inflection, a request anchor is preferred over a plain
    # verb, and the anchored verb is removed as the whole matched span.
    verb_spans = {}
    for _verb, _pattern in _REQUEST_VERB_PATTERNS:
        for _match in _pattern.finditer(low):
            start = _match.start()
            if start not in verb_spans or _match.end() > verb_spans[start]:
                verb_spans[start] = _match.end()
    spans = sorted(verb_spans.items())
    if spans:
        anchored = [
            span for span in spans
            if _REQUEST_ANCHOR_OBJECT.match(message[span[1]:].lstrip())
        ]
        _start, end = (anchored or spans)[-1]
        tail = message[end:]
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
    boundary = _CLAUSE_BOUNDARY_PATTERN.search(tail)
    if boundary:
        tail = tail[:boundary.start()].rstrip()
    for cut in _DESCRIPTION_TAIL_CUTS:
        idx = tail.lower().find(cut)
        if idx != -1:
            tail = tail[:idx].rstrip()
    return tail.strip().strip(".,!?")


def _try_capture_request_details(message, message_lower, require_identity=False,
                                 prefer_most_specific=False):
    """If the caller already described the request inside the current
    message, hold onto it so Steve does not redundantly re-ask on the
    next turn. When require_identity is True (intro/getting-identity
    turns), the description is only captured if the caller ALSO stated
    the patient DOB in the same message OR the patient identity is
    already known (e.g. Sprint16 reused it from a completed
    medical-professional referral/collection flow) - otherwise a bare
    introduction ("We sent a fax requesting the patient's medical
    records...") would be stored verbatim as the request details.
    Returns the stored details (or None).

    prefer_most_specific is for the turn that resolves a receipt question.
    A receipt confirmation is only useful if it names the right items, and
    a vaguer description captured on an EARLIER turn ("calling about an
    order for a CPAP machine") would otherwise be echoed instead of the
    items the caller is listing right now. So when the caller re-describes
    the fax in this turn, the fuller of the two descriptions wins; a shorter
    re-description ("that request") never displaces a more specific one.
    """
    global fax_request_details
    if fax_request_details and not prefer_most_specific:
        return fax_request_details
    if require_identity and not (
        fax_patient_dob
        or detect_dob_in_message(message)
    ):
        return fax_request_details
    if not (any(d in message_lower for d in _DOC_REQUEST_WORDS) or "request" in message_lower):
        return fax_request_details
    described = _clean_request_details(message, message_lower)
    if fax_request_details and len(described) <= len(fax_request_details):
        return fax_request_details
    fax_request_details = described
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


def _echoed_request_details():
    """The stored request description as it should read when Steve echoes it
    back, with a leading "for " dropped. The description keeps the caller's
    own preposition, so echoing it verbatim after "It came in for" produced
    "It came in for for the latest EKG"."""
    details = (fax_request_details or "").strip()
    if details.lower().startswith("for "):
        details = details[4:]
    return details


def _compose_received_confirmation(message, message_lower,
                                   receipt_question=False):
    """RECEIVED-path confirmation, answered in the direction the caller
    actually asked about. Both directions name the items the caller listed
    (the cleaned request details, e.g. "the recent X ray of the neck and the
    appointment note discussing the neck pain") so the acknowledgment works
    for every fax inquiry, not just the prototype phrasing, and both roll
    50/50 on any "any [test] you have on file" item. The difference is the
    OUTBOUND half:

      receipt_question=True - the caller asked whether the fax THEY sent has
        arrived, so Steve confirms receipt and closes. RT16-12: the send
        offer and the "good fax number" ask used to be attached to this turn
        as well, so a caller who only wanted a receipt answer was told Steve
        could fax the documents back and was then asked for a number to send
        them to - asking for a service that was never requested. The
        requested items are still named so the caller can tell WHICH fax
        arrived, then Steve asks what else he can help with.
      receipt_question=False - the caller asked us to SEND documentation, so
        Steve offers to fax it over and asks for a good fax number to send
        it to (unchanged).
    """
    parts = []
    stated = (fax_request_details or "").strip()
    if receipt_question:
        echoed = _echoed_request_details()
        if echoed:
            parts.append(f"Yes we did receive it. It came in for {echoed}.")
        else:
            parts.append("Yes we did receive it.")
    elif stated:
        parts.append(
            "Yes we did receive it. I can go ahead and fax these over "
            f"to you: {stated}."
        )
    else:
        parts.append(
            "Yes we did receive it. I can go ahead and fax these over "
            "to you."
        )
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
    if receipt_question:
        parts.append("Is there anything else I can help you with today?")
    else:
        parts.append("What is a good fax number to send these to?")
    return " ".join(parts)


def _is_goodbye(message_lower):
    return any(p in message_lower for p in _GOODBYE_PHRASES)


def _is_call_ending(message_lower):
    return any(p in message_lower for p in _CALL_ENDING_PHRASES)


# ─────────────────────────────────────────────
# Workflow handler (called by app.py while fax_flow_active)
# ─────────────────────────────────────────────

def handle_fax_flow(message, message_lower):
    """Deterministic state machine. Returns the Steve response string, or
    None to let app.py fall through to the workflow that owns the caller's
    turn. Most stages are always answered here; the one exception is
    "receipt_answered", which hands back any turn that is a separate
    business need rather than a reply about the fax (see that stage)."""
    global fax_stage
    global fax_patient_first, fax_patient_last, fax_patient_dob
    global fax_request_details, fax_received
    global fax_callback_number, fax_deadline, fax_priority
    global fax_fax_number_provided

    patient_full = None
    if fax_patient_first and fax_patient_last:
        patient_full = f"{fax_patient_first} {fax_patient_last}"

    # Direction of THIS turn, computed once and threaded down to the received
    # path. A fax inquiry has two opposite directions: "did the fax WE sent
    # arrive?" and "please fax these documents TO us". Both run the same
    # 75/25 receipt roll, but only the second one may offer to fax anything
    # back or ask for a fax number. Deciding this once, here, keeps the
    # direction out of the individual stage branches.
    receipt_question = is_fax_receipt_question(message_lower)

    # A turn that asks whether an already-sent fax arrived is answered by the
    # 75/25 roll NOW. It used to fall into the identity stages below, which
    # replied "Can I get the patient's first and last name and date of birth
    # for this request?" to a question that needs no identity - so the
    # question was never answered by this workflow and was free to leak to
    # the LLM, which invented fax details ("a general office fax number") and
    # claimed it could not confirm receipt. Identity present in the message is
    # still captured (opportunistically, for later stages), it is just no
    # longer a precondition for answering the receipt question.
    if (
        fax_received is None
        and fax_stage in (None, "collect_patient", "collect_dob", "collect_request")
        and receipt_question
    ):
        if not fax_patient_first or not fax_patient_last:
            pulled_first, pulled_last, pulled_dob = _pull_identity_from_app()
            if pulled_first and pulled_last:
                fax_patient_first, fax_patient_last = pulled_first, pulled_last
            if pulled_dob and not fax_patient_dob:
                fax_patient_dob = pulled_dob
        if not fax_patient_first or not fax_patient_last:
            name_first, name_last = _extract_patient_name(message)
            if name_first and name_last:
                fax_patient_first, fax_patient_last = name_first, name_last
        if not fax_patient_dob and detect_dob_in_message(message):
            fax_patient_dob = extract_dob_from_message(message)
        _try_capture_request_details(
            message, message_lower, require_identity=False,
            prefer_most_specific=True,
        )
        return _advance_from_request(message, message_lower, receipt_question)

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
        return _advance_from_request(message, message_lower, receipt_question)

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
            return _advance_from_request(message, message_lower, receipt_question)
        return (
            f"Could I get {fax_patient_first if fax_patient_first else 'the patient'}'s "
            f"date of birth?"
        )

    if fax_stage == "collect_request":
        if not fax_request_details:
            _try_capture_request_details(message, message_lower)
        if not fax_request_details:
            return "What information or documentation are you requesting?"
        return _advance_from_request(message, message_lower, receipt_question)

    if fax_stage == "receipt_answered":
        # The caller's receipt question was answered in the inbound direction:
        # nothing is being faxed to them and they were never asked for a fax
        # number, so the outbound path must not be assumed. Three honest
        # continuations, in this order:
        #   * a destination (a number, or an ask for where to send it) - the
        #     caller is taking the send offer after all, so the outbound path
        #     resumes UNCHANGED by falling through to received_callback below.
        #     The number is tested before the farewell check because a
        #     farewell and a number can share a turn ("no, bye, it's
        #     321-555-7777"), and a supplied number is the stronger signal.
        #   * a real farewell - end the call deterministically. Gratitude is
        #     deliberately not treated as a farewell here: "thanks" answers
        #     the receipt question, and closing on it would take the caller's
        #     next turn - very often the actual reason they called - down the
        #     canned closed-stage reply instead of the workflow that owns it.
        #   * anything else is a separate business need (a diagnosis code for
        #     an equipment order, a prescription refill, a records request)
        #     that belongs to the workflow that owns it, not to a fax
        #     exchange nobody asked for. Returning None hands the turn back to
        #     app.py so that workflow can serve it; fax_request_details is
        #     still captured and still published by build_context(), so the
        #     stated need is not lost.
        if _extract_phone(message) or _asks_for_fax_number(message_lower):
            fax_stage = "received_callback"
        elif _is_call_ending(message_lower):
            fax_stage = "closed"
            return "Thank you for calling. Have a great day!"
        else:
            # RT16-11 fix: the receipt question is already answered, and
            # this turn is the caller stating their actual business need
            # (e.g. "we just need clarification on the prescription
            # directions by tomorrow") - not a fax destination, not a
            # goodbye, and not something any OTHER app.py workflow owns.
            # Returning None here silently handed the turn to the LLM,
            # which had no deterministic grounding for it and either
            # invented an answer, claimed it couldn't verify anything, or
            # asked about faxing again - the exact "third outcome" this
            # workflow must not produce. Preserve the stated need, apply
            # the existing (already built, previously unused) urgency
            # rule, and take a callback number - never answer the
            # clinical content itself.
            _try_capture_request_details(
                message, message_lower, require_identity=False,
                prefer_most_specific=True,
            )
            urgent = _is_urgent_deadline(message_lower)
            if urgent:
                fax_priority = "high"
            fax_stage = "receipt_answered_callback"
            priority_phrase = " as a high priority message" if urgent else ""
            return (
                "I'm not able to provide medication or prescription "
                "information directly. I will pass this along to the "
                f"provider's team{priority_phrase}. May I get a good "
                "callback number so someone can follow up with you?"
            )

    if fax_stage == "receipt_answered_callback":
        phone = _extract_phone(message)
        if not phone:
            return "May I get a good callback number for you?"
        fax_callback_number = phone
        fax_stage = "closed"
        return (
            "Thank you. I have noted that and someone from our office "
            "will follow up with you. Is there anything else I can help "
            "you with today?"
        )

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
        # A representative who asks for the office fax number must get it at
        # ANY point in the call, including after the flow has already closed.
        # On the NOT-RECEIVED path this is the whole point - Steve told them
        # to resend, so the number to resend to may be asked for on a later
        # turn. Previously the closed stage ignored the request and just
        # asked "Is there anything else...", leaving the caller with no way
        # to resend. Deliberately says nothing about a message: this
        # workflow never creates a provider message (see the module
        # docstring), so a fax Steve just said never arrived can never be
        # described as something the provider was notified to review.
        if _asks_for_fax_number(message_lower):
            return (
                f"Our office fax number is {_office_fax_number()}. "
                "Is there anything else I can help you with today?"
            )
        # No provider message is created on either branch of this workflow
        # (see the module docstring), so Steve must not claim one here. The
        # received branch used to add "The message has been created and our
        # team is on it.", which the not-received branch had just been fixed
        # to stop saying - so a caller whose fax DID arrive was told a message
        # was created, which this workflow never does.
        return "Is there anything else I can help you with today?"

    return None


def _advance_from_request(message, message_lower, receipt_question=False):
    """Shared completion of the identity+request stages: randomly
    determine whether the fax was received (75/25, once) and begin the
    appropriate branch. receipt_question is the direction of the turn that
    completed the request - it decides whether the RECEIVED branch closes
    after confirming receipt or opens the outbound fax-back."""
    global fax_received, fax_stage
    if fax_received is None:
        fax_received = _determine_fax_received()
    if fax_received:
        fax_stage = "receipt_answered" if receipt_question else "received_callback"
        return _compose_received_confirmation(
            message, message_lower, receipt_question
        )
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
