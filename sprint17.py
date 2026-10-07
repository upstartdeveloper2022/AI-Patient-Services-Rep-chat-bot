"""
requests.py — Miscellaneous Patient Services Request Workflows

Covers 12 UAT-checklist request types that don't belong in the PHF
(Sprint13) or Wellness/MAW/CHA (Sprint14) workflows:

  1.  Practice Manager complaint/compliment (Monica Caldwell)
  2.  Transfer to Total Care for a car/automobile accident
  3.  Prior authorization request from an insurance company rep
  4.  Life insurance rep following up on a disability claim
  5.  In-house home health: cannot accept patient (referred elsewhere)
  6.  Home health: will the PCP follow the patient for home health?
  7.  Home health nurse requesting written treatment instructions
  8.  Medication samples inquiry
  9.  Imaging order / imaging request
  10. Lab order request
  11. Clarity on an existing imaging order (what body part it concerns)
  12. Faxing an imaging order to an outside facility
  13. Critical lab result reported by a lab representative
  14. Outside office asking for OUR fax number (one-shot)

ARCHITECTURE (mirrors Sprint13.py / Sprint14.py exactly):
  - Self-contained. Does not import Sprint13 or Sprint14 and never reads
    or writes their state.
  - app.py owns routing/dispatch and must call reset_state() on each new
    call (see home()/reset pattern in app.py).
  - Reads app.py's already-collected patient identity the same way
    Sprint13/Sprint14 already do (import app locally inside the handler
    that needs it, read-only).
  - Every branch either returns a response string (which app.py should
    return to the caller directly, bypassing the LLM, exactly like the
    Sprint13/Sprint14 dispatch blocks already do) or returns None to
    signal "not handled this turn" / "hand off to app.py's own flow"
    (used only by the imaging/lab "offer appointment" branches, which
    intentionally hand off to app.py's existing FUTURE appointment
    scheduling rather than duplicating availability generation a third
    time in a third file).

SPRINT 17 INTEGRATION STATUS (integrated into app.py via Sprint17 alias)
  Enabled (11): practice_manager, total_care, samples, imaging_request,
    imaging_clarity, imaging_fax (patient-type; activated after
    pre-chart) and prior_auth, hh_decline, hh_follow, hh_instructions,
    office_fax_number (external callers; activated before the
    medical-professional intercept and before pre-chart).
  Deliberately NOT enabled (see ENABLED_WORKFLOWS): critical_lab (already
    handled in app.py/Sprint16 med-pro flow), lab_order_request (already
    handled by app.py lab-work / Sprint14 lab-order flows), life_insurance
    (staged; set aside to stay within the 10-workflow Sprint 17 limit).
  Detection helpers use word-boundary matching so short triggers such as
  "mri" or "ave" cannot match inside unrelated words.
"""

import os
import random
import re
from datetime import datetime

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

PRACTICE_MANAGER_NAME = "Monica Caldwell"
PRACTICE_MANAGER_FIRST_NAME = "Monica"
TOTAL_CARE_NAME = "Total Care"

PHONE_PATTERN = re.compile(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b')

# ── 1. Practice Manager ──
PRACTICE_MANAGER_TRIGGERS = [
    "practice manager", "speak to Monica", "talk to Monica",
    "speak with Monica", "talk with Monica", "Monica Caldwell",
    "ms. Caldwell", "ms Caldwell", "mrs. Caldwell", "mrs Caldwell",
    "speak to the manager", "talk to the manager",
    "speak with the manager", "talk with the manager",
    "speak to management", "file a complaint", "file a compliment",
    "make a complaint", "lodge a complaint", "give a compliment",
    "complaint about the office", "complaint about my visit",
    "compliment about my visit", "compliment for",
]

# ── 2. Total Care / automobile accident ──
CAR_ACCIDENT_TRIGGERS = [
    "car wreck", "car accident", "automobile wreck",
    "automobile accident", "car collision", "automobile collision",
    "car crash", "automobile crash", "auto accident", "auto wreck",
    "auto collision", "auto crash", "motor vehicle accident",
    "motor vehicle collision", "mva",
]

# ── 3. Prior authorization (insurance rep) ──
PRIOR_AUTH_TRIGGERS = [
    "prior authorization", "prior auth", "requires a prior authorization",
    "need a prior auth", "prior authorization is required",
    "pa is required for", "authorization is required for this medication",
    "authorization required to cover", "need authorization to cover",
]

# ── 4. Life insurance disability claim follow-up ──
LIFE_INSURANCE_DISABILITY_TRIGGERS = [
    "life insurance", "disability claim", "following up on a disability",
    "calling about a disability claim", "disability claim follow up",
    "disability claim follow-up", "checking on a disability claim",
    "status of a disability claim", "disability claim status",
]

# ── 5/6/7. Home health (all require the base context word) ──
HOME_HEALTH_CONTEXT_WORDS = ["home health"]

HOME_HEALTH_DECLINE_TRIGGERS = [
    "can't accept the patient", "cannot accept the patient",
    "unable to accept the patient", "referred to another home health",
    "referred out to another home health",
    "referred to a different home health",
    "patient was referred to another agency", "already been referred to another agency",
    "the patient was already referred to another agency",
    "not able to accept this patient", "won't be able to accept",
    "will not be able to accept",
    "can't accept this patient", "cannot accept this patient",
    "unable to accept this patient", "can't accept your patient",
    "cannot accept your patient", "unable to accept your patient",
]

# The same decline is often phrased without the word "accept" ("we are
# unable to take Mr. Howard on as a patient"), so the substring list above
# misses it and the turn fell through to the LLM, which improvised a
# finished-message + 72-hour reply. This pattern covers the "on as a
# patient" family; the base "home health" context word is still required
# by detect_home_health_decline().
_HH_DECLINE_ONBOARDING_PATTERN = re.compile(
    r"\b(?:not\s+able\s+to|unable\s+to|cannot|can't|won'?t\s+be\s+able\s+to|"
    r"will\s+not\s+be\s+able\s+to)\s+"
    r"(?:take|taking|accept|accepting|see|seeing|manage|managing|keep)\b"
    r"(?:\s+on\s+as\s+(?:a|an|our|your|the)\s+|\s+as\s+(?:a|an|our|your|the)\s+)?"
    r"(?:\s+[\w'\.]+){0,6}?"
    r"\s*(?:new\s+|this\s+|the\s+)*patients?\b"
)

# The same decline is just as often phrased about a PRONOUN rather than
# the word "patient" - "we are not able to take him on because he has
# already been referred to Loving Hands Home Health", "the patient has
# already been referred to Loving Hands Home Health, so we cannot take
# him on", "unfortunately we are not able to take him as he has already
# been referred elsewhere for home health". The onboarding pattern above
# cannot match any of those (it requires a literal "patient(s)" downstream
# of the verb) and the fixed trigger list cannot either (it requires
# "accept the patient" or "referred to another home health", never
# "referred to <named agency> Home Health" or "referred elsewhere"), so
# detect_home_health_decline() returned False. The turn then fell through
# to the LLM, which improvised a message it claimed was already
# documented plus a 72-business-hour follow-up, and never asked for a
# callback number at all.
#
# _HH_DECLINE_REFUSED_PATTERN is the refusal half (the caller rejecting
# the patient) and _HH_ALREADY_REFERRED_PATTERN is the evidence half (a
# referral already exists somewhere else). BOTH are required by
# detect_home_health_decline(): either alone is far too broad - "we
# cannot see the results" or "she was referred to us" must keep their
# existing owners - but together they are the exact definition of this
# workflow. The base "home health" context word is still required.
_HH_DECLINE_REFUSED_PATTERN = re.compile(
    r"\b(?:cannot|can't|can\s+not|could\s+not|couldn't|won't|will\s+not|"
    r"would\s+not|wouldn't|do\s+not|don't|does\s+not|never|"
    r"(?:un)?able\s+to|"
    r"not(?:\s+going\s+to\s+be\s+able\s+to|\s+be\s+able\s+to|\s+able\s+to)?)"
    # Up to three filler words between the refusal and the verb, so
    # "won't be able to take" and "not going to be able to accept" are
    # read the same as the bare "cannot take".
    r"(?:\s+\w+){0,3}?\s+"
    r"(?:accept|admit|admitt|handle|keep|see|serve|take|treat)"
    r"(?:s|ing)?\b",
    re.IGNORECASE,
)

_HH_ALREADY_REFERRED_PATTERN = re.compile(
    r"\balready\s+(?:been\s+)?referred\b"
    r"|\b(?:has|have|had)\s+been\s+referred\b"
    r"|\b(?:was|were)\s+(?:already\s+)?referred\b"
    r"|\breferred\s+(?:out\s+|over\s+|already\s+|elsewhere"
    r"|to\s+(?:an?\s+)?(?:other|another|different|existing|second)\s+)",
    re.IGNORECASE,
)

# "the patient has already been referred to loving hands home health, ..."
_HH_REFERRAL_AGENCY_PATTERN = re.compile(
    r"referred\s+(?:them|him|her|the\s+patient|this\s+patient|"
    r"the\s+person)?\s*(?:out\s+|over\s+|else\s+)?to\s+"
    r"(?P<agency>[^,.;?!]+)",
    re.IGNORECASE,
)

HOME_HEALTH_FOLLOW_TRIGGERS = [
    "will the provider follow", "will the doctor follow",
    "follow the patient for home health", "follow for home health",
    "continue to follow this patient", "will follow the patient",
    "provider willing to follow", "pcp follow the patient",
    "physician follow the patient",
    "follow this patient", "will follow this patient",
    "follow him for home health", "follow her for home health",
]

# The same follow-up request is often phrased with a demonstrative or
# pronoun object, which the substring list above misses: "will Dr.
# Rodriguez follow THIS patient for home health" contains neither "follow
# for home health" nor "follow the patient for home health", so
# detect_home_health_follow() returned False and the turn fell through to
# the LLM, which improvised its own callback-and-close wording. This
# pattern covers the "follow <object> for home health" family; the base
# "home health" context word is still required by
# detect_home_health_follow().
_HH_FOLLOW_FOR_PATTERN = re.compile(
    r"\b(?:follow|following|continue\s+to\s+follow|keep\s+following|"
    r"see|seeing)\s+"
    r"(?:him|her|them|this\s+patient|that\s+patient|the\s+patient|"
    r"(?:mr|mrs|ms|miss|dr)\.?\s+[\w'\.]+)?\s*"
    r"for\s+home\s+health\b"
)

HOME_HEALTH_INSTRUCTIONS_TRIGGERS = [
    "written instructions", "need instructions to treat",
    "instructions to treat the patient", "treatment instructions",
    "need orders to treat", "instructions on how to treat",
    "orders to treat the patient", "care instructions for the patient",
]

# The same written-instructions request is often qualified by the KIND of
# care between "written" and "instructions" ("written WOUND-CARE
# instructions", "written medication instructions"), so none of the fixed
# substrings above match and detect_home_health_instructions() returned
# False. The whole turn then fell to the LLM, which improvised its own
# request/priority/deadline wording and, once Groq rate-limited, answered
# the caller's deadline with the connection-failure fallback. This pattern
# allows up to two qualifier words between the modifier and
# "instructions"/"orders"; the base "home health" context word is still
# required by detect_home_health_instructions().
_HH_INSTRUCTIONS_PATTERN = re.compile(
    r"\b(?:written|verbal|updated|revised|formal|home\s+health)\s+"
    r"(?:\w+[\s\-]\s*){0,2}"
    r"(?:instructions?|orders?)\b"
)

# ── 8. Medication samples inquiry ──
MEDICATION_SAMPLES_TRIGGERS = [
    "medication samples", "medicine samples", "drug samples",
    "sample medication", "sample of my medication",
    "samples of this medication", "have any samples",
    "any samples available", "provide samples",
]

# Priority escalation of the samples message Steve ALREADY created ("can
# you make that message high priority?"). Only ever consulted on the
# completed samples stage - it never opens a message-intake turn, so it
# cannot restart the workflow or ask for the callback number again. Needs
# BOTH an urgency word and a reference to the existing message, so
# ordinary closing remarks ("no that's all, thank you") are not treated
# as an escalation.
_PRIORITY_ESCALATION_TRIGGERS = [
    "high priority", "higher priority", "top priority", "priority",
    "urgent", "urgency", "expedite", "expedited", "rush", "asap",
    "as soon as possible",
]
_PRIORITY_ESCALATION_TARGETS = [
    "message", "note", "memo", "request", "ticket", "it", "this", "that",
]

# ── 9. Imaging order / imaging request ──
# NOTE: none of these currently collide with any app.py trigger list -
# x-ray/imaging vocabulary is unclaimed elsewhere in the codebase.
IMAGING_ORDER_TRIGGERS = [
    "xray", "x-ray", "x ray", "mri", "ct scan", "cat scan", "pet scan",
    "calcium score", "ultrasound", "mammogram", "mammography",
    "imaging order", "imaging request",
    "order for an mri", "order for a ct", "order for an x-ray",
    "order for an ultrasound", "order for a mammogram",
]

# ── 10. Lab order request ──
# *** SEE "LAB TRIGGER OVERLAP" NOTE AT TOP OF FILE ***
LAB_ORDER_NAME_TRIGGERS = [
    "complete blood count", "cbc",
    "white blood cell differential", "wbc differential",
    "reticulocyte count", "peripheral blood smear",
    "basic metabolic panel", "bmp", "renal function panel",
    "hepatic function panel", "liver function panel",
    "lipid panel", "apolipoprotein b", "apob",
    "high-sensitivity c-reactive protein", "hs-crp", "hscrp",
    "troponin", "bnp", "hemoglobin a1c", "hba1c",
    "thyroid panel", "vitamin d", "vitamin b12",
    "iron panel", "blood culture", "urine culture",
    "antinuclear antibody", "ana panel", "sti panel", "std panel",
    "urinalysis", "fecal occult blood test", "fobt",
    "lab order", "order my labs", "order for my labs",
]

# ── 11. Clarity on an imaging order ──
IMAGING_CLARITY_TRIGGERS = [
    "clarity on my imaging order", "clarity on my imaging",
    "what is my imaging order for", "what does my imaging order say",
    "clarify my imaging order", "what part of my body is the imaging",
    "not sure what my imaging order is for",
    "what is this imaging order for", "explain my imaging order",
    "what was my imaging order for", "what body part is the imaging for",
    "what was ordered for imaging", "what imaging was ordered",
    "what imaging did the doctor order", "what imaging did dr",
    "what imaging did my doctor order", "what kind of imaging was ordered",
    "what type of imaging was ordered", "what test was ordered",
    "what's the imaging order for", "what's my imaging order for",
    "what's my imaging order",
    "can't remember what the imaging order was for",
    "don't remember what the imaging order was for",
    "don't know what the imaging order is for",
    "don't recall what my imaging order is for",
]

# Natural phrasings for the same question that name no "imaging order"
# wording at all ("Dr. Patel ordered some imaging for me, but I can't
# remember exactly what it was for or what part of my body they're
# supposed to image"). Requires an imaging word AND a "I don't know what
# the order says" cue so a request for a new order ("I need an imaging
# order for my knee") and an imaging-results question ("what did my
# MRI show") keep their own owners.
_IMAGING_CONTEXT_WORDS = [
    "imaging", "xray", "x-ray", "x ray", "mri", "ct scan", "cat scan",
    "pet scan", "ultrasound", "radiology", "calcium score", "scan",
]
_IMAGING_UNCERTAINTY_CUES = [
    "i can't remember", "i cannot remember", "i don't remember",
    "i do not remember", "i can't recall", "i cannot recall",
    "i don't recall", "i do not recall", "not sure what", "unsure what",
    "don't know what", "do not know what", "don't remember what",
    "do not remember what", "don't know which", "don't know what part",
    "what part of my body", "which part of my body", "what body part",
    "supposed to image", "supposed to scan", "what it was for",
    "what the order says", "what the order is for", "what was it for",
    "check my order", "look at my order", "look into my order",
]

# ── 12. Fax imaging order to outside facility ──
IMAGING_FAX_TRIGGERS = [
    "fax my imaging order", "fax the imaging order",
    "fax my x-ray order", "fax my mri order", "fax my mammogram order",
    "send my imaging order to", "fax imaging order to",
    "fax my imaging order to", "fax the imaging order to",
    "fax the x-ray order", "fax the mri order", "fax the mammogram order",
    "fax my ct order", "fax the ct order", "fax my ultrasound order",
]

# Sending an imaging order to an outside facility WITHOUT saying "fax"
# ("...the order for the CT scan of my heart to be sent to Beachside
# Imaging"). Only the destination verbs - a bare "send" with no
# destination is an imaging ORDER request, not a send-to-facility one.
_IMAGING_ORDER_SEND_CUES = [
    "sent to", "send to", "send it to", "send them to", "sent it to",
    "send over to", "sent over to", "forward to", "forwarded to",
    "route to", "routed to", "mail to", "mailed to", "email to",
    "email it to", "deliver to", "delivered to",
]
# Facility extraction also needs to look for fax-related destination cues
# ("...faxed to Crossroads Diagnostics").
_IMAGING_FAX_SEND_CUES = [
    "fax to", "faxed to", "fax it to", "fax them to",
]

# ── 13. Critical lab result (lab representative call) ──
CRITICAL_LAB_TRIGGERS = [
    "critical lab", "critical value", "critical result",
    "critical lab result", "critical lab value", "panic value",
    "panic lab", "panic result", "a critical", "abnormal critical",
    "critical finding",
]

# Address heuristic (mirrors app.py's ADDRESS_INDICATORS shape - kept
# local per the self-contained-module rule rather than importing app.py's).
_ADDRESS_INDICATORS = [
    "blvd", "boulevard", "street", "avenue", "ave", "road", "rd",
    "lane", "ln", "court", "ct", "circle", "terrace", "suite", "ste",
    "floor", "building", "apt", "unit", "po box",
]

# Sprint 17 UAT additions (callers who are NOT verified patients - they are
# activated before the medical-professional intercept and before pre-chart):
#   life_insurance (previously staged), supervisor, death_cert,
#   wrong_office, fax_notice, office_fax_number (a bare "what is your
#   fax number?" needs no patient identity, so it is external-type).
EXTERNAL_WORKFLOWS = frozenset({
    "prior_auth", "hh_decline", "hh_follow", "hh_instructions",
    "life_insurance", "supervisor", "death_cert", "wrong_office",
    "fax_notice", "office_fax_number",
})
# Sprint 17 UAT additions (activated after pre-chart): transfer_lab,
# transfer_records, insurance_update, lab_order_request (previously staged),
# and fax_notice (a VERIFIED patient announcing that another office will
# fax - the same tag is also external for unverified callers).
PATIENT_WORKFLOWS = frozenset({
    "practice_manager", "total_care", "samples",
    "imaging_request", "imaging_clarity", "imaging_fax",
    "patient_prior_auth",
    "transfer_lab", "transfer_records", "insurance_update",
    "lab_order_request", "fax_notice", "wrong_office",
})
ENABLED_WORKFLOWS = EXTERNAL_WORKFLOWS | PATIENT_WORKFLOWS

_IMAGING_TYPE_CHOICES = [
    "X-ray", "MRI", "CT scan", "Ultrasound", "PET scan",
]
_IMAGING_BODY_PART_CHOICES = [
    "left knee", "right knee", "lumbar spine", "cervical spine",
    "right shoulder", "left shoulder", "chest", "abdomen", "pelvis",
    "right hip", "left hip", "right wrist", "left ankle", "brain",
]

# ─────────────────────────────────────────────
# State variables
# ─────────────────────────────────────────────

# Law-enforcement caller identity ("Sgt. Jefferson"), kept SEPARATE from the
# patient they are calling about. Set by app.py's pre-chart recognition; used
# only so the closing never addresses the caller by the patient's name.
law_enforcement_caller_name = None
requests_flow_active = False
requests_active_workflow = None  # one of the tags returned by detect_any_request_intent()
requests_pending_workflow = None  # patient-type intent captured before pre-chart completes
requests_appointment_handoff_reason = None  # set when a flow hands off to app.py scheduling
tc_stage = None  # Total Care: None -> "ask_transfer" -> "complete"

# 1. Practice Manager
pm_stage = None  # None -> "ask_reason" -> "await_callback" | "complete"
pm_reason = None
pm_availability_determined = False
pm_available = None
pm_callback_number = None

# 2. Total Care (car accident) — stateless response, phone cached per call
total_care_phone_number = None

# 3. Prior authorization
pa_stage = None  # "ask_fax" -> "ask_confirmation" -> "complete"
pa_fax_number = None
pa_portal_accepted = False
pa_confirmation_number = None
# 3b. Patient-reported prior authorization requirement ("Tricare requires a
# prior authorization through their portal for my tirzepatide"). The
# medication and the payer are both stated by the patient, so the message
# to the MA can name them; no fax/confirmation machine exists because the
# patient is not the party transmitting anything.
pa_patient_stage = None  # "ask_contact" -> "complete"
pa_patient_insurance = None
pa_patient_medication = None
pa_patient_ma = None
pa_patient_contact_number = None

# 4. Life insurance disability claim
li_stage = None  # "ask_contact" -> "ask_fax" -> "ask_confirmation" -> "complete"
li_contact_number = None
li_fax_number = None
li_confirmation_number = None

# 5. Home health - decline (referred elsewhere)
hh_decline_stage = None  # "ask_contact" -> "complete"
hh_decline_contact_number = None

# 6. Home health - will PCP follow
hh_follow_stage = None  # "ask_contact" -> "complete"
hh_follow_contact_number = None

# 7. Home health nurse - written instructions
# "check_ma" -> "ask_contact" (if unavailable) -> "ask_deadline" -> "ask_contact" -> "complete"
hh_instructions_stage = None
hh_instructions_ma_available = None
hh_instructions_contact_number = None
hh_instructions_deadline = None
hh_instructions_high_priority = False

# 8. Medication samples
samples_stage = None  # "ask_contact" -> "complete" (escalation handled on complete)
samples_contact_number = None

# 9. Imaging request
# "ask_reason" -> "ask_provider_aware" -> "offer_appt" -> "complete"
# "verify_phone" is the reason-already-stated path: the caller named the
# physician and the reason in the first turn, so the request is notated and
# the callback number verified instead of asking for either.
imaging_stage = None
imaging_reason = None
imaging_provider_aware = None
imaging_provider_name = None
imaging_contact_number = None

# 10. Lab order request (independent of imaging, same shape)
lab_order_req_stage = None
lab_order_req_reason = None
lab_order_req_provider_aware = None

# 11. Imaging order clarity (cached per call so a repeat question gets
# the same answer instead of a new random result)
imaging_clarity_result = None  # (imaging_type, body_part)

# 12. Fax imaging order to outside facility
imaging_fax_stage = None  # "ask_facility" -> "ask_fax_or_address" -> "complete"
imaging_fax_facility_name = None
imaging_fax_number = None

# 13. Critical lab result (lab rep call) - stateless one-shot; MA is
# always available per spec, so there is no failure branch/stage ladder.
critical_lab_reported = False

# ── Sprint 17 UAT additions ──
# Identity captured from an EXTERNAL caller about the patient they are
# calling on (life insurance rep, coroner/funeral home, another office).
ext_patient_first = None
ext_patient_last = None
ext_patient_dob = None
requests_appointment_handoff_label = "an imaging order"
dc_stage = None  # death certificate: "ask_decedent" -> "ask_contact"
dc_callback_number = None
dc_provider = None
wo_stage = None  # wrong office: "ask_office"
fn_stage = None  # fax notice: "ask_patient" -> "ask_contact"
fn_provider = None
fn_callback_number = None
fn_followup_requested = False
xfer_stage = None  # department transfer: "ask_transfer"
xfer_numbers = {}  # department -> number, cached per call
ins_stage = None  # insurance update: "ask_company" -> "ask_member"
ins_company = None
ins_member_number = None
ins_chart_record = None  # simulated chart: {"company": ..., "member_number": ...}
_ext_identity_owner = None  # workflow that currently owns ext_patient_*


def reset_state():
    global requests_flow_active, requests_active_workflow
    global pm_stage, pm_reason, pm_availability_determined, pm_available, pm_callback_number
    global total_care_phone_number
    global pa_stage, pa_fax_number, pa_portal_accepted, pa_confirmation_number
    global pa_patient_stage, pa_patient_insurance, pa_patient_medication
    global pa_patient_ma, pa_patient_contact_number
    global li_stage, li_contact_number, li_fax_number, li_confirmation_number
    global hh_decline_stage, hh_decline_contact_number
    global hh_follow_stage, hh_follow_contact_number
    global hh_instructions_stage, hh_instructions_ma_available, hh_instructions_contact_number
    global hh_instructions_deadline, hh_instructions_high_priority
    global samples_stage, samples_contact_number
    global imaging_stage, imaging_reason, imaging_provider_aware
    global imaging_provider_name, imaging_contact_number
    global lab_order_req_stage, lab_order_req_reason, lab_order_req_provider_aware
    global imaging_clarity_result
    global imaging_fax_stage, imaging_fax_facility_name, imaging_fax_number
    global critical_lab_reported
    global requests_pending_workflow, requests_appointment_handoff_reason, tc_stage
    global ext_patient_first, ext_patient_last, ext_patient_dob
    global requests_appointment_handoff_label
    global dc_stage, dc_callback_number, dc_provider, wo_stage
    global fn_stage, fn_provider, fn_callback_number, fn_followup_requested
    global xfer_stage, xfer_numbers
    global ins_stage, ins_company, ins_member_number, _ext_identity_owner
    global ins_chart_record
    global script_verify_sent, law_enforcement_caller_name

    law_enforcement_caller_name = None
    script_verify_sent = None
    _ext_identity_owner = None
    ext_patient_first = None
    ext_patient_last = None
    ext_patient_dob = None
    requests_appointment_handoff_label = "an imaging order"
    dc_stage = None
    dc_callback_number = None
    dc_provider = None
    wo_stage = None
    fn_stage = None
    fn_provider = None
    fn_callback_number = None
    fn_followup_requested = False
    xfer_stage = None
    xfer_numbers = {}
    ins_stage = None
    ins_company = None
    ins_member_number = None
    ins_chart_record = None
    requests_flow_active = False
    requests_pending_workflow = None
    requests_appointment_handoff_reason = None
    tc_stage = None
    requests_active_workflow = None
    pm_stage = None
    pm_reason = None
    pm_availability_determined = False
    pm_available = None
    pm_callback_number = None
    total_care_phone_number = None
    pa_stage = None
    pa_fax_number = None
    pa_portal_accepted = False
    pa_confirmation_number = None
    pa_patient_stage = None
    pa_patient_insurance = None
    pa_patient_medication = None
    pa_patient_ma = None
    pa_patient_contact_number = None
    li_stage = None
    li_contact_number = None
    li_fax_number = None
    li_confirmation_number = None
    hh_decline_stage = None
    hh_decline_contact_number = None
    hh_follow_stage = None
    hh_follow_contact_number = None
    hh_instructions_stage = None
    hh_instructions_ma_available = None
    hh_instructions_contact_number = None
    hh_instructions_deadline = None
    hh_instructions_high_priority = False
    samples_stage = None
    samples_contact_number = None
    imaging_stage = None
    imaging_reason = None
    imaging_provider_aware = None
    imaging_provider_name = None
    imaging_contact_number = None
    lab_order_req_stage = None
    lab_order_req_reason = None
    lab_order_req_provider_aware = None
    imaging_clarity_result = None
    imaging_fax_stage = None
    imaging_fax_facility_name = None
    imaging_fax_number = None
    critical_lab_reported = False


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _extract_phone(message):
    match = PHONE_PATTERN.search(message)
    return match.group(0) if match else None


def _pm_previous_callback_number():
    """Return the latest patient number given in response to a callback ask."""
    import sys

    app_module = sys.modules.get("app")
    history = getattr(app_module, "conversation_history", [])
    for index in range(len(history) - 1, 0, -1):
        assistant_turn = history[index - 1]
        user_turn = history[index]
        if (
                assistant_turn.get("role") != "assistant"
                or user_turn.get("role") != "user"
                or not re.search(
                    r"\b(?:callback|contact)\s+number\b",
                    assistant_turn.get("content", ""),
                    re.IGNORECASE,
                )
        ):
            continue
        phone = _extract_phone(user_turn.get("content", ""))
        if phone:
            return phone
    return None


def _looks_like_fax_number(message):
    return bool(_extract_phone(message))


_ADDRESS_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(i) for i in _ADDRESS_INDICATORS) + r")\.?(?!\w)"
)


def _looks_like_address(message_lower):
    # Word-boundary match plus a street number: the old substring test
    # matched "ave" inside "have" and any digit, so "I have 5 minutes"
    # counted as an address.
    return bool(_ADDRESS_RE.search(message_lower)) and bool(
        re.search(r"\b\d{1,6}\s+[a-z0-9]", message_lower)
    )


def _resolve_availability(env_key):
    """Staff availability for transfer flows. Env override for UAT
    (true/false); otherwise never 'available' outside office hours
    (Steve must not claim to reach staff while the office is closed),
    and a 50/50 roll during office hours."""
    forced = os.environ.get(env_key)
    if forced is not None and forced.lower() in ("true", "false", "1", "0", "yes", "no"):
        return forced.lower() in ("true", "1", "yes")
    try:
        import app as _app
        if not _app.is_within_office_hours():
            return False
    except Exception:
        pass
    return random.choice([True, False])


def pop_appointment_handoff_reason():
    """Returns (and clears) the reason a flow handed off to app.py's
    FUTURE appointment scheduling, or None."""
    global requests_appointment_handoff_reason
    reason = requests_appointment_handoff_reason
    requests_appointment_handoff_reason = None
    return reason


def _generate_phone_number():
    return f"321-{random.randint(100, 999)}-{random.randint(1000, 9999)}"


def _contains_any(message_lower, triggers):
    # Word-boundary match (plural "s" allowed). Plain substring checks
    # matched short triggers inside unrelated words.
    return any(
        re.search(r"(?<!\w)" + re.escape(t) + r"s?(?!\w)", message_lower)
        for t in triggers
    )


# ─────────────────────────────────────────────
# Intent detection
# ─────────────────────────────────────────────

def detect_practice_manager_request(message_lower):
    return _contains_any(message_lower, PRACTICE_MANAGER_TRIGGERS)


def detect_car_accident(message_lower):
    return _contains_any(message_lower, CAR_ACCIDENT_TRIGGERS)


_EXTERNAL_CALLER_CUE = re.compile(
    r"\b(?:calling|call(?:ing)?\s+you)\s+(?:from|with|on behalf of)\b"
    r"|\b(?:i'?m|i am|we'?re|we are)\s+(?:with|from)\b"
    r"|\bthis is\s+[\w .'-]{1,40}?\s+(?:from|with|at)\b"
    r"|\bon behalf of\b"
)


def detect_prior_auth_request(message_lower):
    """Prior authorization call from a payer/pharmacy representative.
    Requires a representative-style self-introduction so a PATIENT who
    merely mentions a prior authorization is not treated as a rep."""
    return _contains_any(message_lower, PRIOR_AUTH_TRIGGERS) and bool(
        _EXTERNAL_CALLER_CUE.search(message_lower)
    )


# A PATIENT reporting what their own insurer requires ("I checked with
# Tricare and they are requiring a prior authorization through their
# portal in order for them to cover the tirzepatide that was prescribed
# to me") is not a payer representative, so _EXTERNAL_CALLER_CUE above is
# deliberately False and this turn matched NO workflow at all - it fell to
# the LLM, which improvised an apology plus a callback ask instead of
# noting the requirement. These cues are all COVERAGE CONSEQUENCES ("they
# will not cover it unless...") rather than a bare mention of the words,
# so a patient merely asking whether something needs authorization ("do
# I need a prior authorization for this?") - a benefits question Steve
# cannot answer - is still not captured.
_PATIENT_PRIOR_AUTH_CUES = [
    "in order for them to cover", "in order for it to cover",
    "in order to cover", "so they will cover", "so they can cover",
    "for them to cover", "for it to cover", "so that they cover",
    "require a prior authorization", "requires a prior authorization",
    "require a prior auth", "requires a prior auth",
    "requiring a prior authorization", "requiring a prior auth",
    "requiring prior authorization", "requires prior authorization",
    "need a prior authorization to cover", "needs a prior authorization to cover",
    "need prior authorization to cover", "needs prior authorization to cover",
    "won't cover", "will not cover", "not covered without",
    "not covered unless", "won't pay for", "will not pay for",
    "won't approve", "will not approve", "not approved without",
    "told me they require", "told me they requires", "told me it requires",
]


def detect_patient_prior_auth_request(message_lower):
    """Prior authorization requirement reported BY the patient about their
    own coverage. Deliberately excludes the representative cue so every
    payer/pharmacy caller keeps the external prior_auth workflow."""
    return (
            _contains_any(message_lower, PRIOR_AUTH_TRIGGERS)
            and _contains_any(message_lower, _PATIENT_PRIOR_AUTH_CUES)
            and not _EXTERNAL_CALLER_CUE.search(message_lower)
    )


def detect_life_insurance_disability(message_lower):
    # A representative-style introduction is required so a PATIENT who
    # merely mentions their life insurance or a disability claim is not
    # treated as a company representative.
    return _contains_any(
        message_lower, LIFE_INSURANCE_DISABILITY_TRIGGERS
    ) and bool(_EXTERNAL_CALLER_CUE.search(message_lower))


def _home_health_caller_in_context():
    """True when the medical-professional caller already identified
    themselves as home health in an EARLIER turn of this call. Detectors
    here are stateless per message, so a decline worded without the phrase
    "home health" ("...referred elsewhere, so we will not be admitting
    them") lost the context the caller established in their introduction
    and fell through to the LLM. Read-only, same pattern as the other
    app.py reads in this file. Scoped to medical-professional callers whose
    earlier turn was an external-caller self-introduction, so a patient who
    merely mentioned home health cannot trigger it."""
    try:
        import app as _app
        if not getattr(_app, "is_medical_professional_caller", False):
            return False
        return any(
            turn.get("role") == "user"
            and "home health" in turn.get("content", "").lower()
            and _EXTERNAL_CALLER_CUE.search(turn.get("content", "").lower())
            for turn in getattr(_app, "conversation_history", [])
        )
    except Exception:
        return False


def detect_home_health_decline(message_lower):
    if not (
            _contains_any(message_lower, HOME_HEALTH_CONTEXT_WORDS)
            or _home_health_caller_in_context()
    ):
        return False
    if (
            _contains_any(message_lower, HOME_HEALTH_DECLINE_TRIGGERS)
            or _HH_DECLINE_ONBOARDING_PATTERN.search(message_lower)
    ):
        return True
    # Pronoun/referral-worded declines ("...not able to take him on because
    # he has already been referred to <Agency> Home Health"). Both halves
    # are required. No sibling-workflow veto is added here: hh_decline is
    # already checked ahead of hh_follow/hh_instructions in
    # detect_any_request_intent(), so a turn carrying a genuine decline
    # keeps resolving to the decline exactly as it does for the fixed
    # triggers above.
    if (
            _HH_DECLINE_REFUSED_PATTERN.search(message_lower)
            and _HH_ALREADY_REFERRED_PATTERN.search(message_lower)
    ):
        return True
    return False


def detect_home_health_follow(message_lower):
    return bool(
        _contains_any(message_lower, HOME_HEALTH_CONTEXT_WORDS)
        and (
                _contains_any(message_lower, HOME_HEALTH_FOLLOW_TRIGGERS)
                or _HH_FOLLOW_FOR_PATTERN.search(message_lower)
        )
    )


def detect_home_health_instructions(message_lower):
    return bool(
        (
            _contains_any(message_lower, HOME_HEALTH_CONTEXT_WORDS)
            or _home_health_caller_in_context()
        )
        and (
                _contains_any(message_lower, HOME_HEALTH_INSTRUCTIONS_TRIGGERS)
                or _HH_INSTRUCTIONS_PATTERN.search(message_lower)
        )
    )


def detect_medication_samples(message_lower):
    return _contains_any(message_lower, MEDICATION_SAMPLES_TRIGGERS)


def detect_priority_escalation(message_lower):
    """Patient asking to raise the priority of a message that has already
    been created (medication samples, etc.) rather than to start a new
    one. Requires an urgency word plus a reference to that message."""
    return (
            _contains_any(message_lower, _PRIORITY_ESCALATION_TRIGGERS)
            and _contains_any(message_lower, _PRIORITY_ESCALATION_TARGETS)
    )


_IMAGING_EXPLICIT = [
    "imaging order", "imaging request", "order for an mri",
    "order for a ct", "order for an x-ray", "order for an ultrasound",
    "order for a mammogram",
]
_IMAGING_REQUEST_CUE = re.compile(
    r"\b(order|orders|ordered|request|need|needs|want|get|schedule)\b"
)
_IMAGING_EXCLUDE = re.compile(r"\b(appointment|results?|report|findings)\b")


def detect_imaging_request(message_lower):
    """An imaging ORDER/REQUEST. Talking about an imaging appointment or
    imaging results ("schedule an appointment to go over my MRI
    results") is not an order request and is left to normal handling."""
    if _contains_any(message_lower, _IMAGING_EXPLICIT):
        return True
    if not _contains_any(message_lower, IMAGING_ORDER_TRIGGERS):
        return False
    if _IMAGING_EXCLUDE.search(message_lower):
        return False
    return bool(_IMAGING_REQUEST_CUE.search(message_lower))


_LAB_GENERIC_WORDS = [
    "lab work", "labs", "blood work", "blood test", "lab test",
    "lab order", "lab request", "blood draw",
]
_LAB_REQUEST_CUE = re.compile(
    r"\b(?:need|needs|want|wants|order|ordered|request|requesting|"
    r"put\s+in|get|can\s+i|could\s+i|can\s+you|could\s+you|like)\b"
)
# Everything that already has an owner elsewhere: lab RESULTS inquiries,
# lab-order pickup/print/fax/send (app.py + Sprint14), "can't find my lab
# order" (Sprint14), scheduling/transfer to the lab (transfer_lab below),
# fasting questions, and critical-lab calls.
_LAB_EXCLUDE = re.compile(
    r"\b(?:results?|pick(?:ing)?\s*up|print\w*|fax\w*|send\w*|sent|"
    r"find|locate|where|appointment|schedul\w+|fast\w*|critical|quest|"
    r"labcorp|lab\s+corp|front\s+desk|going\s+to|have\s+my|has\s+my|"
    r"have\s+the|transfer\w*|cancel\w*|bill\w*)\b"
)


# A sentence that only states an existing wellness/physical appointment
# ("I already have my physical scheduled next month.") is context, not
# evidence about what is being requested. Words such as "have my" or
# "scheduled" inside it must not exclude the lab request that follows.
_WELLNESS_NOUN = (
    r"(?:wellness|physical|check-?\s?up|annual|maw|comprehensive\s+health)"
)
_WELLNESS_NOUN_RE = re.compile(r"\b" + _WELLNESS_NOUN + r"\b")

# The caller is ACTING on a wellness appointment (schedule/book/reschedule/
# cancel/move/change it), as opposed to merely mentioning one. Past-tense
# "scheduled"/"booked" describe state and deliberately do not match.
_WELLNESS_ACTION_RE = re.compile(
    r"\b(?:schedul(?:e|ing)|book(?:ing)?|reschedul(?:e|ing)|rebook(?:ing)?|"
    r"cancel(?:l?ing)?|move|moving|change|changing|set(?:ting)?\s+up)\b"
    r"(?:\W+\w+){0,5}?\W+" + _WELLNESS_NOUN + r"\b"
    r"|\b" + _WELLNESS_NOUN + r"\b(?:\W+\w+){0,6}?\W+"
    r"(?:reschedul(?:e|ing)|rebook|cancel|move|change|push)\b"
)


def wellness_mention_is_contextual(message_lower):
    """True when a wellness/physical visit is only MENTIONED (e.g. as the
    reason the caller wants labs, or inside a complaint) and the caller is
    not asking to schedule/reschedule/cancel/change it. app.py uses this so
    a contextual mention does not let Sprint14 pre-empt another recognised
    patient workflow."""
    return not _WELLNESS_ACTION_RE.search(message_lower)


def _lab_exclusion_text(message_lower):
    """Message text the lab-order exclusions are evaluated against:
    sentences that only state a wellness appointment (wellness noun, no lab
    wording) are dropped. Single-sentence messages are never altered."""
    sentences = re.split(r"(?<=[.!?;])\s+", message_lower)
    if len(sentences) < 2:
        return message_lower
    kept = [
        sent for sent in sentences
        if not (
            _WELLNESS_NOUN_RE.search(sent)
            and not _contains_any(sent, LAB_ORDER_NAME_TRIGGERS)
            and not _contains_any(sent, _LAB_GENERIC_WORDS)
        )
    ]
    return " ".join(kept) if kept else message_lower


def detect_lab_order_request(message_lower):
    """A request for NEW lab work / a lab order. Only fires when no
    pickup/fax/results/scheduling/fasting wording is present, so the
    existing lab-order pickup, fax, EHR-guidance and lab-results flows in
    app.py/Sprint14 keep ownership of their own phrasing."""
    if _LAB_EXCLUDE.search(_lab_exclusion_text(message_lower)):
        return False
    has_lab = (
            _contains_any(message_lower, LAB_ORDER_NAME_TRIGGERS)
            or _contains_any(message_lower, _LAB_GENERIC_WORDS)
    )
    return has_lab and bool(_LAB_REQUEST_CUE.search(message_lower))


def detect_imaging_clarity(message_lower):
    if _contains_any(message_lower, IMAGING_CLARITY_TRIGGERS):
        return True
    # A question about an imaging order the patient cannot recall, phrased
    # without any "imaging order" wording. Both halves are required: the
    # imaging word keeps non-imaging questions out, the uncertainty cue
    # keeps a request for a NEW order (imaging_request) and an
    # imaging-results question out. Results/report/findings phrasing is
    # excluded for the same reason _IMAGING_EXCLUDE guards the request
    # trigger - "what did my MRI show" is not an order-clarity question.
    return (
            _contains_any(message_lower, _IMAGING_CONTEXT_WORDS)
            and _contains_any(message_lower, _IMAGING_UNCERTAINTY_CUES)
            and not _IMAGING_EXCLUDE.search(message_lower)
    )


def detect_imaging_fax(message_lower):
    if _contains_any(message_lower, IMAGING_FAX_TRIGGERS):
        return True
    # imaging modality + fax wording ("get my MRI order faxed to X").
    # Requires an imaging word so lab-order faxing stays with app.py.
    if (
            _contains_any(message_lower, IMAGING_ORDER_TRIGGERS)
            and bool(re.search(r"\b(fax|faxed|faxing|faxes)\b", message_lower))
            and not _IMAGING_EXCLUDE.search(message_lower)
    ):
        return True
    # The same request phrased without the word "fax" ("I need the order
    # for the CT scan of my heart to be sent to Beachside Imaging"). An
    # imaging word plus a send-to-destination cue is required, so a request
    # for a NEW order ("I need an MRI of my left knee") and an
    # imaging-results question keep their existing owners.
    return (
            _contains_any(message_lower, IMAGING_ORDER_TRIGGERS)
            and _contains_any(message_lower, _IMAGING_ORDER_SEND_CUES)
            and not _IMAGING_EXCLUDE.search(message_lower)
    )


# An imaging order request whose reason the caller stated in the SAME turn
# ("I need Dr. Chen to order an MRI of my left knee. When they did an x
# ray of the knee, they found nothing."). Asking "what is the reason?"
# there is the UAT failure: the patient already gave it. Kept narrow - a
# bare order ("I need an MRI order for my knee") names no indication, so it
# still gets the reason question.
_IMAGING_REASON_STATED = re.compile(
    r"\b(?:"
    r"found\s+nothing|showed\s+nothing|shows\s+nothing|showing\s+nothing"
    r"|nothing\s+(?:showed|shows|showing|was\s+found|came\s+up|on\s+it)"
    r"|(?:is|was|are|were|isn't|wasn't|aren't|weren't|seem(?:s|ed)?|look(?:s|ed)?)\s+"
    r"(?:normal|negative|clear|unremarkable|fine|good)"
    r"|(?:not|never|didn't|doesn't|isn't|wasn't|weren't)\s+(?:show|find|reveal|see)\w*"
    r"|already\s+(?:had|done|gotten|got|went|been)"
    r"|been\s+(?:hurting|aching|in\s+pain)"
    r"|hurt|hurts|hurting|ache|aches|aching|pain|painful|stiff|stiffness"
    r"|swelling|swollen|numb|numbness|tingling|injury|injured|fracture"
    r"|sprain|sprained|tear|torn|rash|bruise|bruised"
    r")\b"
)

# The study the caller says the provider will review. An existing study is
# named apart from the one being requested ("MRI of my knee ... they did an
# x ray"), so x-ray/CT/ultrasound are matched BEFORE any fallback.
_IMAGING_PRIOR_STUDY = (
    (re.compile(r"\bx[\s-]?rays?\b"), "x-ray"),
    (re.compile(r"\b(?:ct|cat)[\s-]?scans?\b|\bcts?\b"), "CT scan"),
    (re.compile(r"\bultrasounds?\b"), "ultrasound"),
)

# Case-sensitive on purpose: "my doctor said ..." must not yield "Dr. Said",
# and a caller who names the physician capitalizes the surname.
_PHYSICIAN_IN_MESSAGE = re.compile(
    r"\b(?:Dr|Doctor)\.?\s+((?:[A-Z][a-zA-Z'’-]+)(?:\s+[A-Z][a-zA-Z'’-]+)*)"
)


def imaging_reason_already_stated(message_lower):
    """True when the caller's own turn already carried the clinical reason
    for the imaging order (a prior study with negative findings, or the
    symptoms/injury driving it)."""
    return bool(_IMAGING_REASON_STATED.search(message_lower))


def _prior_study_named(message_lower):
    """The existing study the provider is to review, or None."""
    for pattern, label in _IMAGING_PRIOR_STUDY:
        if pattern.search(message_lower):
            return label
    return None


_FACILITY_SUFFIXES = (
    "imaging", "radiology", "center", "centre", "centre", "clinic",
    "medical", "diagnostics", "diagnostic", "associates", "group",
    "hospital", "lab", "labs", "laboratory", "health", "partners",
)


def _facility_named_in_message(message):
    """The outside facility the caller already named as the destination,
    or None. Reads the text AFTER the send cue ("...sent to Beachside
    Imaging"), so a body part mentioned earlier in the sentence ("...of
    my heart to be sent to Beachside Imaging") is never mistaken for the
    facility."""
    stripped = message.strip().rstrip("?.!,")
    lowered = stripped.lower()
    best = None
    # Check both send cues and fax cues
    all_cues = _IMAGING_ORDER_SEND_CUES + _IMAGING_FAX_SEND_CUES
    for cue in all_cues:
        idx = lowered.rfind(cue)
        if idx == -1:
            continue
        tail = stripped[idx + len(cue):].strip()
        tail = re.sub(r"^(?:it|them|the order|the form)?\s*to\s+", "", tail,
                      flags=re.IGNORECASE).strip()
        tail = re.sub(r"^(?:the|a|an)\s+", "", tail, flags=re.IGNORECASE).strip()
        if not tail or PHONE_PATTERN.search(tail):
            continue
        best = tail
    if not best:
        return None
    # Keep it to a facility-like name: at most four words, and it must
    # not be a body part or a clinical phrase.
    words = best.split()
    if len(words) > 4:
        return None
    if any(len(w) > 24 for w in words):
        return None
    if re.search(r"\b(?:of|for|my|heart|knee|chest|head|brain|back|"
                 r"abdomen|spine|shoulder|hip|ankle|wrist|lung|lungs)\b",
                 best, re.IGNORECASE):
        return None
    if _contains_any(best.lower(), _FACILITY_SUFFIXES):
        return best
    # No facility keyword, but a short capitalized proper name at the end
    # of the sentence is still a name ("...sent to Beachside").
    return best if best[:1].isupper() or len(words) <= 2 else None


def _resolve_imaging_physician(message):
    """The physician the caller named, in the caller's own form ("Dr.
    Chen"). Falls back to the patient's PCP on the chart, then to neutral
    wording - never invents a name."""
    match = _PHYSICIAN_IN_MESSAGE.search(message)
    if match:
        return "Dr. " + match.group(1).split()[-1]
    try:
        import app as _app
        pcp = _app.get_patient_pcp_from_history()
    except Exception:
        pcp = None
    if pcp:
        return "Dr. " + pcp.split()[-1]
    return "your provider"


def detect_critical_lab_report(message_lower):
    return _contains_any(message_lower, CRITICAL_LAB_TRIGGERS)


def _detect_fax_notice_unless_hh_instructions(message_lower):
    """A heads-up that a request will be faxed is NOT the caller's intent
    when it is only the DELIVERY METHOD of a Home Health written-
    instructions request ("we need written instruction and we're also
    going to fax the request over"). fax_notice is checked ahead of
    hh_instructions, so it used to take ownership of that turn and turn
    the request into a generic incoming-fax/provider-order note (72
    business hours, no Home Health priority handling). Yield to
    hh_instructions when that detector also matches. Fax notices without
    written-instructions wording are unchanged."""
    return (
        detect_incoming_fax_notice(message_lower)
        and not detect_home_health_instructions(message_lower)
    )


def detect_any_request_intent(message_lower):
    """Returns a workflow tag for app.py's early-capture pattern
    (mirrors Sprint13.phf_intent_detected / Sprint14.wellness_intent_detected),
    or None. Checked in a fixed priority order so a message that could
    plausibly match more than one (rare, given the trigger specificity)
    resolves deterministically. Critical lab is checked FIRST, ahead of
    every other workflow in this file (and, per the integration note at
    the top of this file, must also be checked ahead of app.py's
    medical-professional intercept) - a delayed or misrouted critical
    lab report is a patient-safety issue, not just a misrouted call.
    Imaging clarity and imaging fax are checked before the general
    imaging-request trigger since both are more specific subsets of
    "imaging" phrasing."""
    ordered = (
        ("critical_lab", detect_critical_lab_report),
        ("supervisor", detect_supervisor_request),
        ("death_cert", detect_death_certificate),
        ("wrong_office", detect_wrong_office),
        ("fax_notice", _detect_fax_notice_unless_hh_instructions),
        # Checked AFTER fax_notice: a message that both announces a
        # future fax AND asks for our number keeps the full notice
        # handling (which already quotes the office fax number).
        ("office_fax_number", detect_office_fax_number_request),
        ("practice_manager", detect_practice_manager_request),
        ("total_care", detect_car_accident),
        ("patient_prior_auth", detect_patient_prior_auth_request),
        ("prior_auth", detect_prior_auth_request),
        ("insurance_update", detect_insurance_update),
        ("transfer_lab", detect_lab_scheduling_transfer),
        ("transfer_records", detect_records_transfer),
        ("life_insurance", detect_life_insurance_disability),
        ("hh_decline", detect_home_health_decline),
        ("hh_follow", detect_home_health_follow),
        ("hh_instructions", detect_home_health_instructions),
        ("samples", detect_medication_samples),
        ("imaging_clarity", detect_imaging_clarity),
        ("imaging_fax", detect_imaging_fax),
        ("imaging_request", detect_imaging_request),
        ("lab_order_request", detect_lab_order_request),
    )
    for tag, detector in ordered:
        if tag in ENABLED_WORKFLOWS and detector(message_lower):
            return tag
    return None


# ─────────────────────────────────────────────
# 1. Practice Manager complaint/compliment
# ─────────────────────────────────────────────

# Words that carry no information about WHY the caller wants the Practice
# Manager - politeness, generic request wording, and the topic noun itself.
# They are stripped only for reason detection, never echoed back.
_PM_BOILERPLATE_PATTERN = re.compile(
    r"\b(?:i'?d|i'?m|i|me|my|we|our|us|would|like|to|want|wish|need|"
    r"please|could|can|may|might|will|shall|file|make|making|lodge|give|"
    r"leave|send|talk|speak|with|about|a|an|the|and|for|of|there|is|it|"
    r"that|this|get|in|have|has|had|do|does|did|you|your|are|am|was|were|"
    r"be|been|so|if|or|at|up|out|now|also|just|really|still|when|"
    r"actually|yes|how|what|question|questions|reason|issue|issues|"
    r"concern|concerns|matter|matters|regarding|today|"
    r"someone|somebody|complain|complaint|complaints|compliment|"
    r"compliments|practice|manager|management|monica|caldwell|ms|mrs|mr|office|"
    r"front|desk|staff|call|calling|connect|put|let|any|one|"
    r"something|anything|going|hope|feel|think|know|see)\b"
)
_PM_NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")


def _pm_caller_stated_reason(message_lower):
    """True when a Practice Manager request carries an actual reason.

    Strip request scaffolding and generic phrases so a request to speak
    with the manager "about a question" is still treated as missing its
    reason, while a caller who says what the message is about is not asked
    to repeat it.

    Content words such as "helpful" in a compliment about the service
    remain, so Steve records that reason rather than asking again."""
    stripped = _PM_BOILERPLATE_PATTERN.sub(" ", message_lower)
    return bool([word for word in _PM_NON_WORD_PATTERN.split(stripped) if word])


def _handle_practice_manager(message, message_lower):
    global pm_stage, pm_reason, pm_availability_determined, pm_available
    global pm_callback_number, requests_flow_active

    if pm_stage is None:
        pm_callback_number = (
            _extract_phone(message) or _pm_previous_callback_number()
        )

    if pm_stage is None and _pm_caller_stated_reason(message_lower):
        # The caller already stated the reason - do not ask again.
        pm_reason = message.strip()
        pm_availability_determined = True
        pm_available = _resolve_availability("STEVE_FORCE_PM_AVAILABLE")
        if pm_available:
            pm_stage = "complete"
            requests_flow_active = False
            return (
                f"I'd be happy to connect you with our Practice Manager, "
                f"{PRACTICE_MANAGER_NAME}. Let me transfer you now. "
                f"Please hold."
            )
        if pm_callback_number:
            pm_stage = "complete"
            requests_flow_active = False
            return (
                f"Thank you. I've noted your callback number as "
                f"{pm_callback_number} and left a message for "
                f"{PRACTICE_MANAGER_FIRST_NAME}. Is there anything else "
                f"I can help you with today?"
            )
        pm_stage = "await_callback"
        return (
            f"I'd be happy to help with that. I'm sorry, "
            f"{PRACTICE_MANAGER_FIRST_NAME} is not available right now. "
            f"I'll take a message and have her follow up with you. May "
            f"I get a good contact number for you?"
        )

    if pm_stage is None:
        pm_stage = "ask_reason"
        # The caller named the topic ("file a complaint with the manager")
        # without saying what it is about. Ask for the substance BEFORE
        # the callback number, so the note Monica receives actually
        # describes the issue - otherwise she gets a message she cannot
        # act on.
        if (
                "complaint" in message_lower
                or "compliment" in message_lower
        ):
            return (
                f"I'd be happy to help with that. {PRACTICE_MANAGER_FIRST_NAME} "
                f"is going to want to know what the nature of the "
                f"{'complaint' if 'complaint' in message_lower else 'compliment'} "
                f"is. Could you give me details about the "
                f"{'complaint' if 'complaint' in message_lower else 'compliment'} "
                f"please?"
            )
        return (
            f"I'd be happy to connect you with our Practice Manager, "
            f"{PRACTICE_MANAGER_NAME}. May I ask the reason for your call?"
        )

    if pm_stage == "ask_reason":
        pm_reason = message.strip()
        if not pm_availability_determined:
            pm_available = _resolve_availability("STEVE_FORCE_PM_AVAILABLE")
            pm_availability_determined = True
        if pm_available:
            pm_stage = "complete"
            requests_flow_active = False
            return (
                f"Thank you. Let me transfer you to {PRACTICE_MANAGER_FIRST_NAME} "
                f"now. Please hold."
            )
        if pm_callback_number:
            pm_stage = "complete"
            requests_flow_active = False
            return (
                f"Thank you. I've noted your callback number as "
                f"{pm_callback_number} and left a message for "
                f"{PRACTICE_MANAGER_FIRST_NAME}. Is there anything else "
                f"I can help you with today?"
            )
        pm_stage = "await_callback"
        return (
            f"I'm sorry, {PRACTICE_MANAGER_FIRST_NAME} is not available "
            f"right now. I'll take a message and have her follow up with "
            f"you. May I get a good contact number for you?"
        )

    if pm_stage == "await_callback":
        phone = _extract_phone(message)
        if phone:
            pm_callback_number = phone
            pm_stage = "complete"
            requests_flow_active = False
            return (
                f"Thank you. I've noted your callback number as {phone} "
                f"and left a message for {PRACTICE_MANAGER_FIRST_NAME}. "
                f"Is there anything else I can help you with today?"
            )
        return "May I get a good contact number for you?"

    return None


# ─────────────────────────────────────────────
# 2. Total Care transfer (car/automobile accident)
# ─────────────────────────────────────────────

def _handle_total_care(message, message_lower):
    global total_care_phone_number, requests_flow_active, tc_stage
    if not total_care_phone_number:
        total_care_phone_number = _generate_phone_number()
    if tc_stage is None:
        tc_stage = "ask_transfer"
        return (
            f"I'm sorry to hear about your accident. Unfortunately, your "
            f"provider does not treat injuries from automobile accidents. "
            f"I can transfer you to {TOTAL_CARE_NAME}, who specializes in "
            f"this kind of care - their number is {total_care_phone_number}. "
            f"Would you like me to transfer you now?"
        )
    # The flow used to end right after asking the question, so the
    # patient's yes/no went to the LLM. Answer it deterministically.
    declines = _contains_any(message_lower, ["no", "nope", "not right now", "no thank you", "no thanks"])
    accepts = _contains_any(message_lower, ["yes", "yeah", "yep", "sure", "ok", "okay", "please", "go ahead"])
    if declines and not accepts:
        tc_stage = "complete"
        requests_flow_active = False
        return (
            f"No problem. If you change your mind, {TOTAL_CARE_NAME} can be "
            f"reached at {total_care_phone_number}. Is there anything else "
            f"I can help you with today?"
        )
    if accepts:
        tc_stage = "complete"
        requests_flow_active = False
        return (
            f"I'm transferring you to {TOTAL_CARE_NAME} now. Should the call "
            f"disconnect, their number is {total_care_phone_number}. Thank "
            f"you for calling Sykes Creek Primary Care. Have a great day!"
        )
    return "Would you like me to transfer you to Total Care now?"


# ─────────────────────────────────────────────
# 3. Prior authorization (insurance rep)
# ─────────────────────────────────────────────

def _handle_prior_auth(message, message_lower):
    global pa_stage, pa_fax_number, pa_portal_accepted
    global pa_confirmation_number, requests_flow_active

    if pa_stage is None:
        pa_stage = "ask_fax"
        return (
            "Thank you for letting us know. Can I get a good fax number "
            "so we can send over what's needed for the prior "
            "authorization? If it's easier to handle through your "
            "portal, that works too."
        )

    if pa_stage == "ask_fax":
        if "portal" in message_lower:
            pa_portal_accepted = True
            pa_stage = "ask_confirmation"
            return (
                "That works. Could I get a confirmation number for this "
                "request?"
            )
        fax = _extract_phone(message)
        if fax:
            pa_fax_number = fax
            pa_stage = "ask_confirmation"
            return "Thank you. Could I get a confirmation number for this request?"
        return (
            "Can I get a good fax number, or let me know if this can be "
            "handled through your portal instead?"
        )

    if pa_stage == "ask_confirmation":
        pa_confirmation_number = message.strip()
        pa_stage = "complete"
        requests_flow_active = False
        return (
            "Thank you, I've put in a message with that information for "
            "the provider to complete the prior authorization. Is there "
            "anything else I can help you with today?"
        )

    return None


# ─────────────────────────────────────────────
# 3b. Patient-reported prior authorization requirement
# ─────────────────────────────────────────────
# Distinct from #3 above: that workflow models a payer representative who
# has paperwork to transmit (fax number -> confirmation number). This one
# models a patient relaying what the insurer told them, which is a message
# to the MA - "Tricare requires a prior auth through their portal for my
# tirzepatide" - exactly the notate-and-call-back shape every other
# patient-type workflow uses.

_PA_MEDICATION_PATTERNS = (
    r"(?:cover|covering|covered|pay for|paying for)\s+(?:the|my)\s+"
    r"([A-Za-z][\w'\-]{2,30})",
    r"(?:prior\s+auth(?:orization)?|pa)\s+(?:for|on)\s+(?:the|my)\s+"
    r"([A-Za-z][\w'\-]{2,30})",
    r"(?:medication|medicine|drug|script|prescription)\s+(?:called\s+|named\s+)?"
    r"([A-Za-z][\w'\-]{2,30})",
    r"(?:to\s+cover|for)\s+(?:my\s+)?([A-Za-z][\w'\-]{2,30})(?!\s+(?:authorization|auth))",
)
_PA_MEDICATION_BLOCKLIST = {
    "authorization", "auth", "prescription", "prescribed", "medication",
    "medicine", "coverage", "portal", "it", "that", "this", "them",
    "they", "one", "anything", "something", "prior",
}
_PA_INSURER_PATTERN = re.compile(
    r"(?:checked|called|spoke|talked|confirmed|verified|heard)\s+"
    r"(?:with|to|on)\s+((?:[A-Z][\w&'\.\-]*)(?:\s+[A-Z][\w&'\.\-]*){0,2})"
)


def _patient_prior_auth_medication(message):
    for pattern in _PA_MEDICATION_PATTERNS:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            name = match.group(1).lower()
            if name not in _PA_MEDICATION_BLOCKLIST:
                return name
    return None


def _patient_prior_auth_insurance(message):
    match = _PA_INSURER_PATTERN.search(message)
    if not match:
        return None
    name = match.group(1).strip()
    # Trim a trailing clause that followed the payer's name.
    name = re.split(r"\b(?:and|but|they|who|to|for|about)\b", name)[0].strip()
    return name or None


def _patient_prior_auth_ma():
    """The MA name + provider the message goes to, from the PCP the patient
    already named. Returns (ma_label, None) or ("the medical assistant for
    your provider", None) when no PCP is on the call."""
    try:
        import app as _app
        ma_name, provider = _app.get_ma_for_patient()
    except Exception:
        ma_name, provider = None, None
    if ma_name and provider:
        return f"{ma_name}, the medical assistant for {provider}"
    return "the medical assistant for your provider"


def _handle_patient_prior_auth(message, message_lower):
    global pa_patient_stage, pa_patient_insurance, pa_patient_medication
    global pa_patient_ma, pa_patient_contact_number, requests_flow_active

    if pa_patient_stage is None:
        pa_patient_stage = "ask_contact"
        pa_patient_insurance = _patient_prior_auth_insurance(message)
        pa_patient_medication = _patient_prior_auth_medication(message)
        pa_patient_ma = _patient_prior_auth_ma()

        # With no payer named, "noting that requires..." would read as a
        # broken sentence - the subject is missing. Use the neutral
        # "that your insurance requires" instead.
        payer = (
            f"{pa_patient_insurance} " if pa_patient_insurance
            else "your insurance "
        )
        via = " through their portal" if "portal" in message_lower else ""
        drug = (
            f" for your {pa_patient_medication}"
            if pa_patient_medication else " for your medication"
        )
        return (
            f"Thank you for letting us know. I'll send a message to "
            f"{pa_patient_ma}, noting that {payer}requires a prior "
            f"authorization{via}{drug}. May I get a good callback number "
            f"for you?"
        )

    if pa_patient_stage == "ask_contact":
        pa_patient_contact_number = (
                _extract_phone(message) or message.strip()
        )
        pa_patient_stage = "complete"
        requests_flow_active = False
        # Do NOT re-read the payer/drug back here. The message was already
        # stated in full on the previous turn, so repeating it added
        # nothing; what the patient still needs is the processing window
        # and an open-ended close (same shape as app.py's
        # handle_medication_order_destination), not a hard "have a great
        # day" sign-off that ended the call before Steve asked whether
        # anything else was needed.
        return (
            "Thank you. I have put in a message. Please allow up to 72 "
            "business hours for processing. Is there anything else that I "
            "can help you with?"
        )

    return None


# ─────────────────────────────────────────────
# 4. Life insurance disability claim follow-up
# ─────────────────────────────────────────────

def _handle_life_insurance(message, message_lower):
    global li_stage, li_contact_number, li_fax_number
    global li_confirmation_number, requests_flow_active

    if li_stage in (None, "ask_patient"):
        # Sprint 17 UAT: the representative is not a verified patient and
        # no identity has been collected yet, so the patient's name and
        # date of birth come first (the chart cannot be checked without
        # them). Existing downstream stages are unchanged.
        _ext_capture_identity(message, bare_reply=(li_stage == "ask_patient"))
        if not (ext_patient_first and ext_patient_last):
            li_stage = "ask_patient"
            return (
                "I can help with that. Could I get the patient's first and "
                "last name and date of birth?"
            )
        if not ext_patient_dob:
            li_stage = "ask_patient"
            return f"Thank you. Could I get {ext_patient_first}'s date of birth?"
        li_stage = "ask_contact"
        return (
            "One moment while I check the chart. (pause) I do see the "
            "disability claim documentation on file. Could I get a good "
            "contact number for your office?"
        )

    if li_stage == "ask_contact":
        phone = _extract_phone(message)
        li_contact_number = phone or message.strip()
        li_stage = "ask_fax"
        return "Thank you. Could I also get a good fax number?"

    if li_stage == "ask_fax":
        fax = _extract_phone(message)
        li_fax_number = fax or message.strip()
        li_stage = "ask_confirmation"
        return "And could I get a confirmation number for this request?"

    if li_stage == "ask_confirmation":
        li_confirmation_number = message.strip()
        li_stage = "complete"
        requests_flow_active = False
        return (
            "Thank you, I've put in a message with that information for "
            "the provider. Is there anything else I can help you with "
            "today?"
        )

    return None


# ─────────────────────────────────────────────
# 5. Home health - cannot accept patient (referred elsewhere)
# ─────────────────────────────────────────────

def _hh_decline_patient_label():
    """The patient's full name for the decline reply, from the identity the
    med-pro caller already gave. Falls back to neutral wording rather than
    inventing or mis-naming a patient."""
    try:
        import app as _app
        first = getattr(_app, "med_pro_patient_first", None)
        last = getattr(_app, "med_pro_patient_last", None)
        if not first:
            first = getattr(_app, "patient_first_name", None)
        if not last:
            last = getattr(_app, "patient_last_name", None)
    except Exception:
        first = last = None
    full = f"{first} {last}".strip() if first else None
    return full or "the patient"


def _hh_decline_pronoun(message):
    """The caller's own pronoun for the patient. Checks this turn's wording
    (including the "Mr."/"Mrs."/"Ms." honorific) first, then the last few
    caller turns, and never guesses when nothing is known."""

    def _from_text(text):
        if not text:
            return None
        low = text.lower()
        if re.search(r"\b(?:mrs\.?|ms\.)\b", low):
            return "she"
        if re.search(r"\bmr\.?\b", low):
            return "he"
        if re.search(r"\b(?:he|his|him|himself)\b", low):
            return "he"
        if re.search(r"\b(?:she|her|hers|herself)\b", low):
            return "she"
        return None

    found = _from_text(message)
    if found:
        return found
    try:
        import app as _app
        prior_user_turns = [
            turn.get("content", "")
            for turn in getattr(_app, "conversation_history", [])
            if turn.get("role") == "user"
        ]
    except Exception:
        prior_user_turns = []
    for prior in reversed(prior_user_turns[-5:]):
        found = _from_text(prior)
        if found:
            return found
    return "they"


def _hh_decline_agency(message):
    """The home-health agency the patient was already referred to, in the
    caller's own words. Returns neutral wording when the caller only said
    "another home health" with no agency name."""
    match = _HH_REFERRAL_AGENCY_PATTERN.search(message)
    if not match:
        return None
    agency = match.group("agency").strip()
    agency = re.split(
        r"\b(?:and|but|so|because|which|who|that|they|we|since|as)\b",
        agency,
    )[0].strip()
    agency = re.sub(r"\s+", " ", agency)
    if not agency:
        return None
    lowered = agency.lower()
    if re.match(
            r"^(?:an?|the|other|some)?\s*(?:different|other|another)?\s*"
            r"(?:home\s+health|home\s+health\s+agency|home\s+health\s+provider"
            r"|agency|provider|company)\b",
            lowered,
    ) or not re.search(r"[a-z]", lowered):
        return "another home health agency"
    # Title-case only fully-lowercase words so an agency the caller already
    # capitalized partially ("St. Mary's") is preserved.
    words = []
    for word in agency.split(" "):
        core = re.sub(r"[^A-Za-z]", "", word)
        if core and any(ch.isupper() for ch in word[1:]):
            words.append(word)
        elif core:
            words.append(word[0].upper() + word[1:].lower())
        else:
            words.append(word)
    return " ".join(words)


def _handle_hh_decline(message, message_lower):
    global hh_decline_stage, hh_decline_contact_number, requests_flow_active

    if hh_decline_stage is None:
        hh_decline_stage = "ask_contact"
        patient = _hh_decline_patient_label()
        pronoun = _hh_decline_pronoun(message)
        agency = _hh_decline_agency(message) or "another home health agency"
        verb = "has" if pronoun in ("he", "she") else "have"
        return (
            f"I am putting in a message that the agency cannot accept {patient} "
            f"because {pronoun} {verb} already been referred to {agency}. "
            f"What is a good number to reach you just in case the office has questions regarding this?"
        )

    if hh_decline_stage == "ask_contact":
        phone = _extract_phone(message)
        hh_decline_contact_number = phone or message.strip()
        hh_decline_stage = "complete"
        requests_flow_active = False
        # Open-ended close, NOT a "have a great day" sign-off: the workflow
        # is done but the caller may have another reason for calling, and
        # hard-closing here ended the call before Steve asked.
        return (
            "Thank you, I've sent a message with that information. Is "
            "there anything else I can help you with today?"
        )

    return None


# ─────────────────────────────────────────────
# 6. Home health - will PCP follow the patient?
# ─────────────────────────────────────────────

def _handle_hh_follow(message, message_lower):
    global hh_follow_stage, hh_follow_contact_number, requests_flow_active

    if hh_follow_stage is None:
        hh_follow_stage = "ask_contact"
        return (
            "I'll need to check with the provider on that. May I get a "
            "good contact number so we can follow up with you?"
        )

    if hh_follow_stage == "ask_contact":
        phone = _extract_phone(message)
        hh_follow_contact_number = phone or message.strip()
        hh_follow_stage = "complete"
        requests_flow_active = False
        return (
            "Thank you, I've put in a message for the provider. Is there "
            "anything else I can help you with today?"
        )

    return None


# ─────────────────────────────────────────────
# 7. Home health nurse - written treatment instructions
# ─────────────────────────────────────────────

def _handle_hh_instructions(message, message_lower):
    global hh_instructions_stage, hh_instructions_ma_available
    global hh_instructions_contact_number, requests_flow_active
    global hh_instructions_deadline, hh_instructions_high_priority

    if hh_instructions_stage is None:
        hh_instructions_ma_available = _resolve_availability("STEVE_FORCE_HH_MA_AVAILABLE")
        if hh_instructions_ma_available:
            hh_instructions_stage = "complete"
            requests_flow_active = False
            return (
                "Let me get the medical assistant on the line for you "
                "now. Please hold while I transfer you."
            )
        # The request is a provider-facing clinical message, which this
        # workflow has always notated at high priority; an explicit
        # escalation turn below confirms it rather than changing it.
        hh_instructions_high_priority = True
        # The patient's DOB is required before the callback number. Take
        # whatever the caller already volunteered (the first message
        # usually carries the name, sometimes the DOB) and only ask for
        # what is still missing.
        _ext_capture_identity(message)
        if not ext_patient_dob:
            hh_instructions_stage = "ask_dob"
            return (
                "I'm sorry, the medical assistant is not available right "
                "now. " + _hh_instructions_dob_question()
            )
        hh_instructions_stage = "ask_contact"
        return (
            "I'm sorry, the medical assistant is not available right "
            "now. May I get a good contact number for you?"
        )

    if hh_instructions_stage == "ask_dob":
        _ext_capture_identity(message, bare_reply=True)
        if ext_patient_dob:
            hh_instructions_stage = "ask_contact"
            return "Thank you. May I get a good contact number for you?"
        # An escalation request here keeps the flow's ownership and its
        # urgency; the DOB is still owed before anything else.
        if detect_priority_escalation(message_lower):
            hh_instructions_high_priority = True
            return (
                "I can mark the message as high priority for the "
                "provider. " + _hh_instructions_dob_question()
            )
        return _hh_instructions_dob_question()

    if hh_instructions_stage == "ask_contact":
        # The caller asked for this message to be made HIGH PRIORITY rather
        # than giving the number we asked for. That is an escalation of the
        # message that already exists, so the flow must retain the request
        # and its urgency and ask for the deadline instead of filing the
        # sentence as the contact number and closing. Previously this turn
        # matched nothing, the flow lost ownership, and the caller's
        # deadline answer reached the LLM.
        if detect_priority_escalation(message_lower):
            hh_instructions_high_priority = True
            hh_instructions_stage = "ask_deadline"
            return (
                "I can mark the message as high priority for the provider. "
                "When will the wound-care instructions be needed?"
            )
        phone = _extract_phone(message)
        hh_instructions_contact_number = phone or message.strip()
        hh_instructions_stage = "complete"
        requests_flow_active = False
        return _hh_instructions_close()

    if hh_instructions_stage == "ask_deadline":
        # The caller answered "by tomorrow morning". Capture it verbatim as
        # the urgency deadline and go back for the callback number, keeping
        # request + priority + deadline + number on one message.
        hh_instructions_deadline = message.strip().rstrip(".?!, ")
        hh_instructions_stage = "ask_contact"
        return (
            f"Thank you, I've noted the deadline. What is the best contact "
            f"number for the office to reach you?"
        )

    return None


def _hh_instructions_dob_question():
    """Ask for the patient's missing DOB, by first name when the caller
    already gave the patient's name."""
    if ext_patient_first and ext_patient_last:
        return f"Could I get {ext_patient_first}'s date of birth?"
    return "Could I get the patient's first and last name and date of birth?"


def _hh_instructions_close():
    """Confirmation for the completed written-instructions request, keeping
    only the details this call actually captured."""
    parts = ["Thank you. I've put in a message"]
    if hh_instructions_high_priority:
        parts.append(" and marked it high priority")
    reply = "".join(parts) + " for the provider."
    if hh_instructions_deadline:
        reply += f" I've noted the deadline you gave: {hh_instructions_deadline}."
    reply += (
        " Please allow up to 24 business hours for this to be processed. Is "
        "there anything else I can help you with today?"
    )
    return reply


# ─────────────────────────────────────────────
# 8. Medication samples inquiry
# ─────────────────────────────────────────────

def _handle_medication_samples(message, message_lower):
    global samples_stage, samples_contact_number, requests_flow_active

    if samples_stage is None:
        samples_stage = "ask_contact"
        return (
            "I'm not able to confirm sample availability myself, but I "
            "can pass that along to the provider. May I get your best "
            "contact number?"
        )

    if samples_stage == "ask_contact":
        phone = _extract_phone(message)
        samples_contact_number = phone or message.strip()
        samples_stage = "complete"
        # requests_flow_active deliberately STAYS True here. The message
        # now exists, and the patient's next turn is very often a change
        # to that same message ("can you make that message high
        # priority?"). Clearing the flag ended the flow at the callback
        # number, so the escalation turn fell through to the LLM, which
        # restarted intake - claiming it had to create a new urgent
        # message and re-asking for the callback number the patient had
        # just given. Keeping the flow dispatchable lets the completed
        # stage below answer that turn deterministically. Any turn that is
        # NOT an escalation returns None, which releases the flow (see
        # handle_requests_flow) so nothing else is held back.
        return (
            "Thank you, I've put in a message with that information. Is "
            "there anything else I can help you with today?"
        )

    if samples_stage == "complete":
        if not detect_priority_escalation(message_lower):
            return None
        captured = samples_contact_number or ""
        # Echo the number already on file so the patient can see it was
        # retained (no second intake); fall back to neutral wording if the
        # caller gave a non-number answer.
        reference = (
            captured if PHONE_PATTERN.search(captured)
            else "the contact number you gave me"
        )
        return (
            f"Of course. I've updated the message I already sent to the "
            f"provider to high priority - there's no need to start a new "
            f"one. The callback number I have on file is {reference}. "
            f"Please allow up to 24 business hours for the provider to "
            f"process it. Is there anything else I can help you with today?"
        )

    return None


# ─────────────────────────────────────────────
# 9. Imaging request  /  10. Lab order request
# ─────────────────────────────────────────────
# Identical shape per the spec, kept as independent state so the two
# workflows never cross-contaminate, but sharing one implementation.

def _handle_reason_awareness_flow(message, message_lower, kind):
    """kind: 'imaging' or 'lab'. Returns a response string, or None to
    hand off to app.py's existing FUTURE appointment scheduling (only
    on the 'offer appointment, patient accepts' branch)."""
    global imaging_stage, imaging_reason, imaging_provider_aware
    global imaging_provider_name, imaging_contact_number
    global lab_order_req_stage, lab_order_req_reason, lab_order_req_provider_aware
    global requests_flow_active, requests_appointment_handoff_reason

    label = "imaging" if kind == "imaging" else "lab"

    if kind == "imaging":
        stage, reason, aware = imaging_stage, imaging_reason, imaging_provider_aware
    else:
        stage, reason, aware = (
            lab_order_req_stage, lab_order_req_reason, lab_order_req_provider_aware
        )

    if stage is None and kind == "imaging":
        # Check if the caller stated the reason in the same turn as the order
        # ("...order an MRI of my left knee. When they did an x ray of the
        # knee, they found nothing."), and named the provider who will
        # review it. Re-asking the reason made the patient repeat
        # themselves, so the request is notated and the callback number
        # verified instead.
        if imaging_reason_already_stated(message_lower) or _prior_study_named(message_lower):
            imaging_reason = message.strip()
            imaging_provider_name = _resolve_imaging_physician(message)
            study = _prior_study_named(message_lower)
            review = (
                f"will review the {study} and get back in touch with you"
                if study
                else "will review this and get back in touch with you"
            )
            imaging_stage = "verify_phone"
            return (
                f"{imaging_provider_name} {review}, however I will notate our "
                f"conversation. Can you verify your phone number so that when "
                f"{imaging_provider_name} calls, they are able to reach you?"
            )

    if stage is None:
        if kind == "imaging":
            imaging_stage = "ask_reason"
        else:
            lab_order_req_stage = "ask_reason"
        return f"Sure, what is the reason for the {label} request?"

    if stage == "verify_phone":
        # The patient confirms the number already on file or gives a new
        # one; either way the notated request is closed here so the answer
        # never reaches the LLM.
        phone = _extract_phone(message)
        if phone:
            imaging_contact_number = phone
        provider = imaging_provider_name or "your provider"
        reach = f" at {imaging_contact_number}" if imaging_contact_number else ""
        imaging_stage = "complete"
        requests_flow_active = False
        return (
            f"Thank you. I have notated your request and our conversation, "
            f"and {provider} will be able to reach you{reach} to review it. "
            f"Please allow up to 24 business hours for that to be processed. "
            f"Is there anything else I can help you with today?"
        )

    if stage == "ask_reason":
        if kind == "imaging":
            imaging_reason = message.strip()
            imaging_stage = "ask_provider_aware"
        else:
            lab_order_req_reason = message.strip()
            lab_order_req_stage = "ask_provider_aware"
        return f"Thank you. Is your provider already aware of this {label} request?"

    if stage == "ask_provider_aware":
        # Negation is checked first: "not aware" contains "aware" and
        # bare "no" matched inside "know"/"not" under substring tests.
        is_yes = _contains_any(message_lower, ["yes", "yeah", "yep"])
        is_not_aware = _contains_any(message_lower, [
            "not aware", "isn't aware", "is not aware", "unaware",
            "don't think", "do not think", "doesn't know", "does not know",
            "not sure",
        ]) or (_contains_any(message_lower, ["no", "nope"]) and not is_yes)
        if is_not_aware:
            if kind == "imaging":
                imaging_provider_aware = False
                imaging_stage = "offer_appt"
            else:
                lab_order_req_provider_aware = False
                lab_order_req_stage = "offer_appt"
            return (
                f"Since your provider isn't aware yet, I can offer to "
                f"schedule an appointment so this can be discussed. "
                f"Would you like me to set that up?"
            )
        # Default to aware=True on an ambiguous/affirmative reply.
        if kind == "imaging":
            imaging_provider_aware = True
            imaging_stage = "complete"
        else:
            lab_order_req_provider_aware = True
            lab_order_req_stage = "complete"
        requests_flow_active = False
        if kind == "lab":
            return (
                "Thank you, I've put in a message with that information "
                "for the provider. Please allow up to 72 business hours "
                "for this to be processed. Is there anything else I can "
                "help you with today?"
            )
        return (
            f"Thank you, I've put in a message with that information "
            f"for the provider. Is there anything else I can help you "
            f"with today?"
        )

    if stage == "offer_appt":
        wants_appt = _contains_any(
            message_lower, ["yes", "yeah", "sure", "okay", "ok", "please", "sounds good"]
        )
        declines_appt = _contains_any(
            message_lower, ["no", "nope", "not right now", "no thank you"]
        )
        if declines_appt and not wants_appt:
            if kind == "imaging":
                imaging_stage = "complete"
            else:
                lab_order_req_stage = "complete"
            requests_flow_active = False
            return (
                "No problem, I've put in a message with that "
                "information for the provider. Is there anything else "
                "I can help you with today?"
            )
        if wants_appt:
            if kind == "imaging":
                imaging_stage = "complete"
            else:
                lab_order_req_stage = "complete"
            requests_flow_active = False
            requests_appointment_handoff_reason = (
                                                      imaging_reason if kind == "imaging" else lab_order_req_reason
                                                  ) or "this request"
            requests_appointment_handoff_label = (
                "an imaging order" if kind == "imaging" else "a lab order"
            )
            # Hand off to app.py's existing FUTURE appointment
            # scheduling rather than duplicating availability
            # generation here - the patient's stated reason
            # (imaging_reason/lab_order_req_reason) is available for
            # app.py to use as the already-known appointment reason.
            return None
        return "Would you like me to schedule an appointment for this?"

    return None


def _handle_imaging_request(message, message_lower):
    return _handle_reason_awareness_flow(message, message_lower, "imaging")


def _handle_lab_order_request(message, message_lower):
    return _handle_reason_awareness_flow(message, message_lower, "lab")


# ─────────────────────────────────────────────
# 11. Clarity on an existing imaging order
# ─────────────────────────────────────────────

def _handle_imaging_clarity(message, message_lower):
    global imaging_clarity_result, requests_flow_active

    if not imaging_clarity_result:
        imaging_clarity_result = (
            random.choice(_IMAGING_TYPE_CHOICES),
            random.choice(_IMAGING_BODY_PART_CHOICES),
        )
    imaging_type, body_part = imaging_clarity_result
    requests_flow_active = False
    return (
        f"One moment while I pull that up. (pause) I see your imaging "
        f"order is for a {imaging_type} of the {body_part}. Is there "
        f"anything else I can help you with today?"
    )


# ─────────────────────────────────────────────
# 12. Fax an imaging order to an outside facility
# ─────────────────────────────────────────────

def _handle_imaging_fax(message, message_lower):
    global imaging_fax_stage, imaging_fax_facility_name
    global imaging_fax_number, requests_flow_active

    if imaging_fax_stage is None:
        # The caller may already have named the facility in this very turn
        # ("...sent to Beachside Imaging"). Asking for the name again was
        # the UAT failure, so capture it and go straight to the fax
        # number/address question.
        facility = _facility_named_in_message(message)
        if facility:
            imaging_fax_facility_name = facility
            imaging_fax_stage = "ask_fax_or_address"
            return (
                "I can get that faxed over for you. Do you have the "
                "facility's fax number? If not, do you have the address "
                "of the facility so I can look up the fax number?"
            )
        imaging_fax_stage = "ask_facility"
        return "Sure, what is the name of the facility this should be faxed to?"

    if imaging_fax_stage == "ask_facility":
        imaging_fax_facility_name = message.strip()
        imaging_fax_stage = "ask_fax_or_address"
        return (
            f"Thank you. Can I get a good fax number for "
            f"{imaging_fax_facility_name}? If you don't know the fax "
            f"number, you can give me the address instead and I can "
            f"look it up."
        )

    if imaging_fax_stage == "ask_fax_or_address":
        fax = _extract_phone(message)
        if fax:
            imaging_fax_number = fax
            imaging_fax_stage = "complete"
            requests_flow_active = False
            return (
                f"Perfect, I'll fax the imaging order to "
                f"{imaging_fax_facility_name} at {fax}. Is there "
                f"anything else I can help you with today?"
            )
        if _looks_like_address(message_lower):
            imaging_fax_stage = "complete"
            requests_flow_active = False
            return (
                f"Thank you, I've located the fax number for that "
                f"address and will fax the imaging order to "
                f"{imaging_fax_facility_name}. Is there anything else I "
                f"can help you with today?"
            )
        return (
            "Can you provide a fax number, or the address so I can "
            "look up the fax number?"
        )

    return None


# ─────────────────────────────────────────────
# 13. Critical lab result (lab representative call)
# ─────────────────────────────────────────────
# Per spec: the medical assistant is available 100% of the time for
# this - there is no "MA unavailable" branch to build. One-shot,
# immediate connection.

def _handle_critical_lab(message, message_lower):
    global critical_lab_reported, requests_flow_active
    critical_lab_reported = True
    requests_flow_active = False
    return (
        "Thank you for letting us know right away. I'm connecting you "
        "with our medical assistant now - please hold."
    )


# ─────────────────────────────────────────────
# Sprint 17 UAT additions
# ─────────────────────────────────────────────
# Source: updated one-page UAT checklist. Every handler below is a
# deterministic Python state machine in the same shape as the handlers
# above; nothing here duplicates app.py/Sprint14/Sprint16 behavior.

_DOB_RE = re.compile(
    r"\b(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})\b|"
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*|\s+)\d{4}\b",
    re.IGNORECASE,
)
_NAME_TOKEN = r"[A-Z][a-zA-Z'\-]+"
_NAME_STOP = {
    "The", "This", "That", "His", "Her", "Our", "Your", "My", "Dr", "Mr",
    "Mrs", "Ms", "Insurance", "Life", "Home", "Health", "Funeral", "Office",
    "Clinic", "Hospital", "Pharmacy", "Police", "Department", "Medical",
    "Center", "Group", "Services", "Company", "Sheriff", "Coroner",
}
_NAME_DENSE_RE = re.compile(
    r"(" + _NAME_TOKEN + r")\s+(" + _NAME_TOKEN + r")\s*,?\s*"
                                                  r"(?i:(?:his\s+|her\s+|their\s+)?(?:dob|d\.o\.b\.?|date\s+of\s+birth|born))"
)
_NAME_KEYWORD_RE = re.compile(
    r"(?i:\b(?:patient|claimant|insured|decedent|deceased|name\s+is|"
    r"named|for|regarding|about))\s*,?\s+(" + _NAME_TOKEN + r")\s+("
    + _NAME_TOKEN + r")"
)
_NAME_BARE_RE = re.compile(
    r"^\s*(" + _NAME_TOKEN + r")\s+(" + _NAME_TOKEN + r")\b[\s,.:;!?\d/\-]*"
                                                      r"(?i:(?:dob|date\s+of\s+birth)?)[\s,.:;\d/\-a-z]*$"
)


def _name_ok(first, last):
    return (
            first not in _NAME_STOP and last not in _NAME_STOP
            and first.lower() != last.lower()
    )


def _ext_capture_identity(message, bare_reply=False):
    """Capture the patient/decedent name and DOB an EXTERNAL caller gives.
    Anchored patterns only (a name next to a DOB, or after patient/
    claimant/decedent/for/regarding); a bare two-word reply is accepted
    only when Steve has just asked for the name (bare_reply=True)."""
    global ext_patient_first, ext_patient_last, ext_patient_dob
    if not (ext_patient_first and ext_patient_last):
        for pattern in (_NAME_DENSE_RE, _NAME_KEYWORD_RE):
            for match in pattern.finditer(message):
                first, last = match.group(1), match.group(2)
                if _name_ok(first, last):
                    ext_patient_first, ext_patient_last = first, last
                    break
            if ext_patient_first:
                break
        if not ext_patient_first and bare_reply:
            match = _NAME_BARE_RE.match(message.strip())
            if match and _name_ok(match.group(1), match.group(2)):
                ext_patient_first, ext_patient_last = match.group(1), match.group(2)
    if not ext_patient_dob:
        dob = _DOB_RE.search(message)
        if dob:
            ext_patient_dob = dob.group(0)


def _verified_patient_identity():
    """(first, last) of a patient already identified by app.py pre-chart,
    or (None, None). Read-only, same pattern as Sprint13/14/16."""
    try:
        import app as _app
        if _app.pre_chart_complete and _app.caller_is_patient:
            return (
                getattr(_app, "patient_first_name", None),
                getattr(_app, "patient_last_name", None),
            )
    except Exception:
        pass
    return None, None


def _our_provider_named(message_lower):
    try:
        import app as _app
        return _app.detect_provider_in_message(message_lower)
    except Exception:
        return None


def _office_is_open():
    try:
        import app as _app
        return _app.is_within_office_hours()
    except Exception:
        return True


def _office_fax_number():
    """Same per-call office fax number Sprint16 quotes (one number per
    call, STEVE_FORCE_FAX_NUMBER override honored there)."""
    try:
        import Sprint16
        return Sprint16._office_fax_number()
    except Exception:
        return "321-555-0199"


_ASK_OFFICE_FAX_RE = re.compile(
    r"\b(?:what(?:'s| is)?\s+(?:your|the)\s+fax|your\s+fax\s+number|"
    r"fax\s+number\s+for|where\s+(?:do|should)\s+(?:i|we)\s+fax)\b"
)

# ── detectors ──

_SUPERVISOR_RE = re.compile(
    r"\b(?:speak|talk|connect|transfer|put\s+me|get|reach|need|want|like|"
    r"can\s+i|could\s+i|ask\s+for)\b[^.?!]{0,40}\bsupervisor\b"
)


def detect_supervisor_request(message_lower):
    """A caller asking for the supervisor ("my supervisor needs a note" is
    deliberately not a request: a cue verb must precede the word)."""
    return bool(_SUPERVISOR_RE.search(message_lower)) and not re.search(
        r"\bmy\s+supervisor\b", message_lower
    )


_DEATH_CERT_RE = re.compile(
    r"\bdeath\s+certificates?\b|\bcertificates?\s+of\s+death\b|\bdeath\s+cert\b"
)
_DEATH_CALLER_RE = re.compile(
    r"\b(?:coroner|medical\s+examiner|police|sheriff|officer|detective|"
    r"sgt|sergeant|"
    r"funeral|mortuary|mortician|crematory|cremation|morgue|deputy|trooper)\b"
)


def detect_death_certificate(message_lower):
    return bool(_DEATH_CERT_RE.search(message_lower)) and bool(
        _DEATH_CALLER_RE.search(message_lower)
        or _EXTERNAL_CALLER_CUE.search(message_lower)
        or law_enforcement_caller_name
    )


# ── Law-enforcement caller recognition (UAT) ──
# "This is Sgt. Jefferson..." / "Officer Smith calling about..." A caller
# who opens with a law-enforcement title + name is neither the patient nor a
# family member, so pre-chart must not ask for a relationship or re-run
# third-party authorization. Anchored to a SELF-introduction (start of the
# message, or after "this is / I am / I'm / my name is") so a title that only
# appears inside a sentence ("my son is an officer") never matches.
_LE_TITLES = {"sgt": "Sgt.", "sergeant": "Sergeant", "officer": "Officer"}
_LE_NOT_A_NAME = frozenset({
    "calling", "from", "with", "here", "speaking", "at", "of", "and",
    "about", "regarding", "for", "on", "in", "the", "a", "an", "i", "we",
    "to", "is",
})
_LE_INTRO_RE = re.compile(
    r"(?:^\s*|\b(?:this\s+is|i\s+am|i['\u2019]m|my\s+name\s+is)\s+)"
    r"(?:(?:hi|hello|hey)\W+)?(?:the\s+)?"
    r"(sgt|sergeant|officer)\b\.?\s+"
    r"([A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*)?)",
    re.IGNORECASE,
)


def detect_law_enforcement_caller(message):
    """Returns (title, name) for a law-enforcement self-introduction
    ("Sgt. Jefferson", "Officer Smith"), else None. Takes the ORIGINAL-case
    message (names are title-cased on output)."""
    for match in _LE_INTRO_RE.finditer(message):
        # Stop at the first filler word so "Jefferson calling" -> Jefferson.
        kept = []
        for t in match.group(2).split():
            if t.lower() in _LE_NOT_A_NAME:
                break
            kept.append(t)
        if kept:
            return _LE_TITLES[match.group(1).lower()], " ".join(
                t.title() for t in kept
            )
    return None


_LE_PATIENT_NAME_RE = re.compile(
    r"\b(?:calling|call(?:ing)?)\s+(?:about|for|regarding|on)\s+"
    r"(?:the\s+|a\s+)?patient\s+([A-Za-z][A-Za-z'\-]+)\s+([A-Za-z][A-Za-z'\-]+)",
    re.IGNORECASE,
)


def law_enforcement_patient_name(message):
    """(first, last) for "...calling about patient Danny Bezos". The generic
    third-party name patterns in app.py have no "patient" keyword handling
    and would read this as first="patient", last="Danny". Returns
    (None, None) when the message does not use that phrasing."""
    match = _LE_PATIENT_NAME_RE.search(message)
    if match and match.group(1).lower() not in _LE_NOT_A_NAME:
        return match.group(1).title(), match.group(2).title()
    return None, None


_WRONG_OFFICE_RE = re.compile(
    r"\bwrong\s+(?:office|number|practice|clinic|place|medical\s+office|"
    r"doctor'?s?\s+office|doctors?\s+office)\b|"
    r"\b(?:called|dialed|reached|calling)\s+the\s+wrong\b|"
    r"\bmeant\s+to\s+call\s+(?:another|a\s+different|someone)\b|"
    r"\bdid(?:n'?t|\s+not)\s+mean\s+to\s+call\b"
)


# "Is this the office of Dr. Woods?" / "Did I reach Dr. Smith's office?" -
# a caller verifying WHICH office they reached. Names an outside (or one of
# our own) provider/office; generic "is this the doctor's office" and our
# own practice name never match. _handle_wrong_office decides whether the
# named provider is one of ours (right office) or not (wrong office).
_OFFICE_ID_LEAD = (
    r"(?:is\s+this|did\s+i\s+(?:reach|get|call|dial)|"
    r"have\s+i\s+(?:reached|gotten|called|dialed)|"
    r"am\s+i\s+(?:speaking\s+(?:with|to)|talking\s+to|calling|at|"
    r"through\s+to|reaching)|are\s+you)"
)
_OFFICE_ID_NAMED = (
    r"(?:(?:the\s+)?(?:office|practice|clinic)\s+of\s+(?:dr|doctor)\b\.?\s+[a-z]"
    r"|(?:dr|doctor)\b\.?\s+[a-z][\w'\-]*(?:\s+[a-z][\w'\-]*)?'?s?\s+"
    r"(?:office|practice|clinic)"
    r"|\b(?!(?:doctor|doctors|dentist|physician|provider|medical|primary|"
    r"family|my|your|our|the)'s\b)[a-z][\w\-]+'s\s+(?:office|practice|clinic))"
)
_OFFICE_IDENTITY_RE = re.compile(
    r"\b" + _OFFICE_ID_LEAD + r"\b[^.?!]{0,25}?" + _OFFICE_ID_NAMED
)


def detect_office_identity_question(message_lower):
    return (
        bool(_OFFICE_IDENTITY_RE.search(message_lower))
        and "sykes creek" not in message_lower
    )


def detect_wrong_office(message_lower):
    return bool(_WRONG_OFFICE_RE.search(message_lower)) or (
        detect_office_identity_question(message_lower)
    )


_FAX_NOTICE_FUTURE_RE = re.compile(
    r"\b(?:will|going\s+to|gonna|about\s+to|'ll)\s+(?:be\s+)?"
    r"(?:faxing|fax|sending\s+(?:over\s+)?(?:you\s+)?a\s+fax)\b|"
    r"\b(?:will|going\s+to|gonna|about\s+to)\s+be\s+sending\b[^.?!]{0,30}\bfax\b"
)
_FAX_NOTICE_RECEIPT_RE = re.compile(
    r"\breceiv\w*\b|\bdid\s+you\b|\bhave\s+you\b|\barriv\w*\b|"
    r"\bcame\s+through\b|\bresend\w*\b"
)
_FAX_NOTICE_FOLLOWUP_RE = re.compile(
    r"\b(?:please|kindly|could\s+you|can\s+you|would\s+you)\b"
    r"[^.?!]{0,30}\b(?:call|contact|follow\s+up|let\s+(?:me|us)\s+know)\b|"
    r"\b(?:callback|call\s+back)\s+number\b"
)


def detect_incoming_fax_notice(message_lower):
    """Heads-up that another office WILL fax a request for an order from
    the PCP. Future tense only, so a past-tense receipt question ("did you
    receive the fax we sent") stays with Sprint16."""
    return (
            bool(_FAX_NOTICE_FUTURE_RE.search(message_lower))
            and bool(re.search(r"\b(?:request|order)s?\b", message_lower))
            and not _FAX_NOTICE_RECEIPT_RE.search(message_lower)
    )


def detect_office_fax_number_request(message_lower):
    """An outside office asking for OUR fax number so they can send
    something ("This is Tim from Dr. Talbott's office. What is your fax
    number?", "Where should I fax this?"). UAT root cause: this phrasing
    matched no workflow, so the turn fell through to Sprint16's
    fax-status handling / the LLM, which treats every fax mention as a
    status check and answered "Yes we did receive it." Receipt/status
    questions ("Did you receive my fax?", "I faxed something yesterday.
    Did you get it?") and future-tense send notices keep their existing
    owners via the same exclusions detect_incoming_fax_notice uses."""
    return (
            bool(_ASK_OFFICE_FAX_RE.search(message_lower))
            and not _FAX_NOTICE_RECEIPT_RE.search(message_lower)
            and not _FAX_NOTICE_FUTURE_RE.search(message_lower)
    )


_XFER_VERB = (
    r"(?:transfer(?:red)?|connect(?:ed)?|put\s+me\s+through|route\s+me|"
    r"speak\s+(?:to|with)|talk\s+(?:to|with)|reach|get\s+me\s+to|send\s+me\s+to)"
)
_LAB_XFER_RE = re.compile(
    r"\blab(?:oratory)?\s+scheduling\b|\b" + _XFER_VERB
    + r"\b[^.?!]{0,30}\b(?:the\s+)?lab(?:oratory)?\b"
)
_LAB_SCHED_RE = re.compile(
    r"\b(?:schedule|book|make|set\s+up)\b[^.?!]{0,40}\b(?:labs?|lab\s+work|"
    r"blood\s*work|blood\s+draw|lab\s+draw|lab\s+appointment)\b"
)
_LAB_XFER_EXCLUDE = re.compile(
    r"\b(?:results?|go\s+over|review|discuss|order|orders|pick(?:ing)?\s*up|"
    r"fax\w*|send|sent|critical|quest|labcorp|cancel\w*|fast\w*)\b"
)


def detect_lab_scheduling_transfer(message_lower):
    if _LAB_XFER_EXCLUDE.search(message_lower):
        return False
    if _LAB_XFER_RE.search(message_lower):
        return True
    if _LAB_SCHED_RE.search(message_lower):
        # "schedule an appointment to discuss my labs" is not lab scheduling.
        return (
                not re.search(r"\bappointment\b", message_lower)
                or bool(re.search(r"\blabs?\s+appointment\b", message_lower))
        )
    return False


_RECORDS_XFER_RE = re.compile(
    r"\b" + _XFER_VERB + r"\b[^.?!]{0,30}\b(?:the\s+)?(?:medical\s+)?records\b|"
                         r"\b(?:medical\s+)?records\s+(?:department|dept|team|office)\b"
)


def detect_records_transfer(message_lower):
    """Transfer to the medical records department. Fax wording is excluded
    so Sprint16 keeps every records FAX inquiry."""
    return bool(_RECORDS_XFER_RE.search(message_lower)) and not re.search(
        r"\bfax\w*\b", message_lower
    )


_INS_GIVE_RE = re.compile(
    r"\b(?:update|updating|add|adding|change|changing|changed|switch(?:ed|ing)?|"
    r"give|provide|put|new)\b[^.?!]{0,40}\b(?:insurance|coverage)\b|"
    r"\b(?:insurance|coverage)\b[^.?!]{0,40}\b(?:changed|is\s+new|"
    r"information|info|card|update|updated)\b"
)
_INS_EXCLUDE_RE = re.compile(
    r"\b(?:accept\w*|take\s+(?:my|our|this)|in[- ]network|out[- ]of[- ]network|"
    r"prior\s+auth\w*|authorization|life\s+insurance|disability|claim|bill\w*|"
    r"copay|deductible|referral|new\s+patient|not\s+a\s+patient|estimate|"
    r"cover(?:ed|s)?\b)"
)


def detect_insurance_update(message_lower):
    return bool(_INS_GIVE_RE.search(message_lower)) and not _INS_EXCLUDE_RE.search(
        message_lower
    )


_SCRIPT_VERIFY_RE = re.compile(r"\bverif(?:y|ying|ication)\b|\bconfirm(?:ing)?\b")
_SCRIPT_WORD_RE = re.compile(
    r"\b(?:script|scripts|prescription|prescriptions|rx|e-?prescri\w+)\b"
)


def detect_script_verification(message_lower):
    """A pharmacy verifying a script/prescription that was sent in. Used by
    app.py's EXISTING pharmacy high-priority-message flow (same flow as the
    not-in-stock substitution call) - no second flow is built here. A
    caller who asks to speak with the provider keeps the speak-with-provider
    flow, and fax wording keeps Sprint16."""
    return (
            bool(_SCRIPT_VERIFY_RE.search(message_lower))
            and bool(_SCRIPT_WORD_RE.search(message_lower))
            and not re.search(r"\bfax\w*\b", message_lower)
    )


# Pharmacy "verify this script came from you" - simulated EMR lookup.
# One outcome is rolled per call (75% on file / 25% not on file) and cached,
# so a repeated question on the same call cannot contradict the first answer.
# STEVE_FORCE_SCRIPT_SENT=true/false forces the outcome for UAT, mirroring
# STEVE_FORCE_FAX_RECEIVED in Sprint16.
script_verify_sent = None

_SCRIPT_MED_STOPWORDS = frozenset({
    "a", "an", "the", "my", "our", "your", "his", "her", "their", "this",
    "that", "these", "those", "it", "patient", "patients", "medication",
    "medicine", "prescription", "script", "refill", "one", "some", "him",
    "them", "you", "us", "me",
})
_SCRIPT_MED_RE = re.compile(
    r"\b(?:prescription|script|rx|e-?prescription)s?\s+(?:for|of)\s+"
    r"(?:the\s+|a\s+|an\s+)?([a-z][a-z\-]{2,30})"
    r"(?:\s+(\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units?)))?",
    re.IGNORECASE,
)


def _script_verified_by_office():
    """75% EMR shows the script WAS sent by our office / 25% it was NOT."""
    global script_verify_sent
    if script_verify_sent is None:
        forced = os.environ.get("STEVE_FORCE_SCRIPT_SENT")
        if forced is not None and forced.lower() in (
                "true", "false", "1", "0", "yes", "no"):
            script_verify_sent = forced.lower() in ("true", "1", "yes")
        else:
            script_verify_sent = random.random() < 0.75
    return script_verify_sent


def _script_description(message):
    """Medication (and dose) in the caller's own words, or a neutral
    phrase. Nothing is hard-coded and nothing is assumed."""
    match = _SCRIPT_MED_RE.search(message)
    if match and match.group(1).lower() not in _SCRIPT_MED_STOPWORDS:
        med = match.group(1).capitalize()
        dose = re.sub(r"\s+", " ", match.group(2).strip()) if match.group(2) else ""
        return f"the prescription for {med}{(' ' + dose) if dose else ''}"
    return "that prescription"


def handle_script_verification(message, message_lower):
    """Pharmacy asks Steve to verify a script was actually sent by this
    office. Returns (response, needs_callback). Only the prescription
    RECORD is verified - no clinical authorization is claimed.

    needs_callback=True (not on file) tells app.py to arm its existing
    pharmacy high-priority-message callback capture, so the escalation
    and its 24-business-hour close are the existing ones."""
    described = _script_description(message)
    if _script_verified_by_office():
        return (
            f"One moment while I check the prescription record. (pause) "
            f"I can confirm that our records show {described} was sent by "
            f"our office. I'm only able to verify the prescription record. "
            f"Is there anything else I can help you with today?",
            False,
        )
    return (
        f"One moment while I check the prescription record. (pause) I'm "
        f"sorry, but I am not able to verify {described} as originating "
        f"from our office, because I do not see a record of it being sent. "
        f"I will put in a high priority phone message so our team can "
        f"follow up. May I have a good callback number for the pharmacy?",
        True,
    )


# ── handlers ──

def _handle_death_certificate(message, message_lower):
    global dc_stage, dc_callback_number, dc_provider, requests_flow_active
    provider = _our_provider_named(message_lower)
    if provider:
        dc_provider = provider
    if law_enforcement_caller_name:
        import app as _app
        if _app.patient_first_name and _app.patient_last_name:
            globals()["ext_patient_first"] = _app.patient_first_name
            globals()["ext_patient_last"] = _app.patient_last_name
            if _app.established_patient_dob:
                globals()["ext_patient_dob"] = _app.established_patient_dob
    if dc_stage in (None, "ask_decedent"):
        _ext_capture_identity(message, bare_reply=(dc_stage == "ask_decedent"))
        if not (ext_patient_first and ext_patient_last):
            dc_stage = "ask_decedent"
            return (
                "I'm very sorry for the loss. I can put in a high priority "
                "message for the provider about the death certificate. Could "
                "I get the decedent's first and last name and date of birth?"
            )
        if not ext_patient_dob:
            dc_stage = "ask_decedent"
            return f"Thank you. Could I get {ext_patient_first}'s date of birth?"
        dc_stage = "ask_contact"
        return "Thank you. May I get a good callback number for you?"
    if dc_stage == "ask_contact":
        phone = _extract_phone(message)
        if not phone:
            return "May I get a good callback number for you?"
        dc_callback_number = phone
        dc_stage = "complete"
        requests_flow_active = False
        provider_team = "the provider and their medical assistant"
        if dc_provider:
            import app as _app
            ma_name = _app.PROVIDER_MA_MAP.get(dc_provider)
            provider_team = (
                f"{dc_provider} and {ma_name}, {dc_provider}'s medical assistant"
                if ma_name else f"{dc_provider}'s medical assistant"
            )
        return (
            f"Thank you. I've put in a high priority message for "
            f"{provider_team} regarding {ext_patient_first} "
            f"{ext_patient_last}'s death certificate and noted your "
            f"callback number. Please allow up to 24 business hours for "
            "this to be processed. Is there anything else I can help you "
            "with today?"
        )
    return None


def _outside_office_label(message_lower):
    """Readable label for the provider/office the caller asked about."""
    m = re.search(r"(?:dr|doctor)\b\.?\s+([a-z][\w\-]*)", message_lower)
    if m:
        return "Dr. " + re.sub(r"'s?$", "", m.group(1)).title()
    m = re.search(r"([a-z][\w\-]+)'s\s+(?:office|practice|clinic)", message_lower)
    if m:
        return m.group(1).title() + "'s office"
    return "that office"


def _our_provider_exact(message_lower):
    """Whole-word match against our provider last names (the substring
    match in app.detect_provider_in_message would read Dr. Parker as Dr.
    Park)."""
    try:
        import app as _app
        for last, full in _app.PROVIDER_LAST_NAMES.items():
            if re.search(r"\b" + re.escape(last) + r"\b", message_lower):
                return full
    except Exception:
        pass
    return None


def _handle_wrong_office(message, message_lower):
    global wo_stage, requests_flow_active
    # Once resolved, a later turn (e.g. the PCP answer "Dr. Mitchell")
    # must not be re-read as a wrong-office question.
    if wo_stage == "complete":
        return None
    if wo_stage is None and detect_office_identity_question(message_lower):
        ours = _our_provider_exact(message_lower)
        wo_stage = "complete"
        requests_flow_active = False
        if ours:
            return (
                f"Yes, {ours} is one of our providers here at Sykes Creek "
                f"Primary Care, so you have reached the right office. How "
                f"can I help you today?"
            )
        return (
            f"This is Sykes Creek Primary Care, and I don't have "
            f"{_outside_office_label(message_lower)} here, so you may have "
            f"reached the wrong office. I'm not able to transfer you to "
            f"another practice or look up their number, but I recommend "
            f"checking your appointment card, patient portal, or insurance "
            f"provider directory for the correct number. Is there anything "
            f"else I can help you with today?"
        )
    ours = _our_provider_named(message_lower)
    if ours:
        wo_stage = "complete"
        requests_flow_active = False
        return (
            f"{ours} is one of our providers here at Sykes Creek Primary "
            f"Care, so you have reached the right office. How can I help "
            f"you today?"
        )
    if wo_stage is None:
        wo_stage = "ask_office"
        return (
            "No problem at all. You have reached Sykes Creek Primary Care. "
            "Which office or provider were you trying to reach?"
        )
    if wo_stage == "ask_office":
        wo_stage = "complete"
        requests_flow_active = False
        return (
            "Thank you. I'm not able to transfer you to another practice "
            "or look up their number, but I recommend checking your "
            "appointment card, patient portal, or insurance provider "
            "directory for the correct number. Is there anything else I "
            "can help you with today?"
        )
    return None


def _handle_fax_notice(message, message_lower):
    global fn_stage, fn_provider, fn_callback_number
    global fn_followup_requested, requests_flow_active
    if _FAX_NOTICE_FOLLOWUP_RE.search(message_lower):
        fn_followup_requested = True
    first, last = _verified_patient_identity()
    if first and last and not ext_patient_first:
        globals()["ext_patient_first"], globals()["ext_patient_last"] = first, last
    _ext_capture_identity(message, bare_reply=(fn_stage == "ask_patient"))
    if fn_provider is None:
        fn_provider = _our_provider_named(message_lower)
    fax_line = ""
    if _ASK_OFFICE_FAX_RE.search(message_lower):
        fax_line = f"Our office fax number is {_office_fax_number()}. "
    if fn_stage in (None, "ask_patient"):
        if not (ext_patient_first and ext_patient_last):
            fn_stage = "ask_patient"
            return (
                f"{fax_line}Thank you for letting us know. Could I get the "
                f"patient's first and last name and date of birth for the "
                f"request?"
            )
        if not ext_patient_dob and not (first and last):
            fn_stage = "ask_patient"
            return (
                f"{fax_line}Thank you. Could I get {ext_patient_first}'s "
                f"date of birth?"
            )
        if not fn_followup_requested:
            fn_stage = "complete"
            requests_flow_active = False
            return (
                f"{fax_line}Thank you. I've notated this in the patient's "
                f"chart so the appropriate medical assistant knows to "
                f"expect the fax. Is there anything else I can help you "
                f"with today?"
            )
        fn_stage = "ask_contact"
        return (
            f"{fax_line}Thank you. May I get a good callback number in case "
            f"the team has questions?"
        )
    if fn_stage == "ask_contact":
        phone = _extract_phone(message)
        if not phone:
            return f"{fax_line}May I get a good callback number?".strip()
        fn_callback_number = phone
        fn_stage = "complete"
        requests_flow_active = False
        provider = fn_provider or "the provider"
        return (
            f"{fax_line}Thank you. I've put in a note for {provider}'s team "
            f"that a request for an order for {ext_patient_first} "
            f"{ext_patient_last} will be coming in by fax. Please allow up "
            f"to 72 business hours for it to be processed once it is "
            f"received. Is there anything else I can help you with today?"
        )
    return None


def _handle_office_fax_number(message, message_lower):
    """One-shot: give the caller our office fax number. Stateless (like
    critical_lab) - nothing to collect, so the flow closes immediately.
    Uses Sprint16's per-call number (STEVE_FORCE_FAX_NUMBER honored there)
    rather than duplicating a constant."""
    global requests_flow_active
    requests_flow_active = False
    return (
        f"Our office fax number is {_office_fax_number()}. Is there "
        f"anything else I can help you with today?"
    )


_DEPARTMENTS = {
    "transfer_lab": "Lab Scheduling",
    "transfer_records": "Medical Records",
}


def _dept_number(dept):
    if dept not in xfer_numbers:
        xfer_numbers[dept] = _generate_phone_number()
    return xfer_numbers[dept]


def _handle_department_transfer(message, message_lower):
    """Transfer to an internal department. Same offer/accept/decline shape
    as Total Care, plus the existing office-hours rule: no warm transfer
    while the office is closed (staff are not reachable)."""
    global xfer_stage, requests_flow_active
    dept = _DEPARTMENTS.get(requests_active_workflow, "that department")
    number = _dept_number(dept)
    if xfer_stage is None:
        if not _office_is_open():
            xfer_stage = "complete"
            requests_flow_active = False
            return (
                f"I'm sorry, our {dept} team is only available during "
                f"office hours, Monday through Friday, 9:00 AM to 5:00 PM, "
                f"and our office is currently closed. You can reach them "
                f"at {number} when we reopen. Is there anything else I can "
                f"help you with today?"
            )
        xfer_stage = "ask_transfer"
        return (
            f"I can transfer you to {dept}. Their number is {number} in "
            f"case we get disconnected. Would you like me to transfer you "
            f"now?"
        )
    declines = _contains_any(
        message_lower, ["no", "nope", "not right now", "no thank you", "no thanks"]
    )
    accepts = _contains_any(
        message_lower, ["yes", "yeah", "yep", "sure", "ok", "okay", "please", "go ahead"]
    )
    if declines and not accepts:
        xfer_stage = "complete"
        requests_flow_active = False
        return (
            f"No problem. If you change your mind, {dept} can be reached "
            f"at {number}. Is there anything else I can help you with today?"
        )
    if accepts:
        xfer_stage = "complete"
        requests_flow_active = False
        return (
            f"I'm transferring you to {dept} now. Should the call "
            f"disconnect, their number is {number}. Thank you for calling "
            f"Sykes Creek Primary Care. Have a great day!"
        )
    return f"Would you like me to transfer you to {dept} now?"


def _handle_insurance_update(message, message_lower):
    global ins_stage, ins_company, ins_member_number, requests_flow_active
    global ins_chart_record
    if ins_stage is None:
        ins_stage = "ask_company"
        return (
            "I can take that down for you. What is the name of your "
            "insurance company?"
        )
    if ins_stage == "ask_company":
        ins_company = message.strip().rstrip(".")
        ins_stage = "ask_member"
        return "Thank you. Could I get the member number on your card?"
    if ins_stage == "ask_member":
        ins_member_number = message.strip()
        # Simulated chart update: Steve updates the insurance record
        # himself - no provider/office message and no 72-hour turnaround.
        ins_chart_record = {
            "company": ins_company,
            "member_number": ins_member_number,
        }
        ins_stage = "complete"
        requests_flow_active = False
        return (
            "I have updated this in your chart. Is there anything else "
            "I can help you with today?"
        )
    return None


def _looks_like_close(message_lower):
    """A short farewell/no-thanks after an external workflow finished."""
    try:
        import app as _app
        if _app.is_conversation_closing_reply(message_lower):
            return True
    except Exception:
        pass
    words = message_lower.split()
    if "?" in message_lower or len(words) > 10:
        return False
    if re.search(
            r"\b(?:need|want|also|but|actually|another|question|how|what|when|"
            r"where|why|can|could|would|please|wait|hold|one\s+more)\b",
            message_lower,
    ):
        return False
    return bool(re.search(
        r"\b(?:no|nope|nothing|that'?s\s+all|thats\s+all|thanks?|thank\s+you|"
        r"bye|goodbye|all\s+set|have\s+a\s+good|have\s+a\s+great)\b",
        message_lower,
    ))


def _handle_external_close(message, message_lower):
    """After an EXTERNAL-caller workflow finishes, Steve asks "anything
    else?". That caller was never run through pre-chart, so app.py's
    general closing block (pre_chart_complete) never sees their "no,
    thanks" - close deterministically here. Any other reply releases the
    flow (returns None) so new-intent capture and the normal pipeline own
    it, exactly as before."""
    global requests_active_workflow, requests_flow_active
    # The Home Health written-instructions message already exists (the
    # flow completed and handed over to this closing stage) and the caller
    # now asks to make it high priority. That is an escalation of the
    # existing message - the same high-priority handling the workflow
    # applies mid-intake - not a new request and not a generic provider
    # order, so confirm it here instead of letting the LLM reject it with
    # the 72-business-hour order turnaround. The closing stage stays
    # active so a following "no thanks" still closes normally.
    if (
            hh_instructions_stage == "complete"
            and not hh_instructions_ma_available
            and detect_priority_escalation(message_lower)
    ):
        captured = hh_instructions_contact_number or ""
        reference = (
            captured if PHONE_PATTERN.search(captured)
            else "the contact number you gave me"
        )
        return (
            f"Of course. The message I put in for the provider is marked "
            f"high priority, and there's no need to start a new one. The "
            f"contact number I have on file is {reference}. Please allow "
            f"up to 24 business hours for it to be processed. Is there "
            f"anything else I can help you with today?"
        )
    if _looks_like_close(message_lower):
        requests_active_workflow = None
        requests_flow_active = False
        return "Thank you for calling Sykes Creek Primary Care. Have a great day!"
    return None


# ─────────────────────────────────────────────
# Main dispatcher
# ─────────────────────────────────────────────

_WORKFLOW_HANDLERS = {
    "critical_lab": _handle_critical_lab,
    "practice_manager": _handle_practice_manager,
    "total_care": _handle_total_care,
    "patient_prior_auth": _handle_patient_prior_auth,
    "prior_auth": _handle_prior_auth,
    "life_insurance": _handle_life_insurance,
    "hh_decline": _handle_hh_decline,
    "hh_follow": _handle_hh_follow,
    "hh_instructions": _handle_hh_instructions,
    "samples": _handle_medication_samples,
    "imaging_request": _handle_imaging_request,
    "lab_order_request": _handle_lab_order_request,
    "imaging_clarity": _handle_imaging_clarity,
    "imaging_fax": _handle_imaging_fax,
    "supervisor": _handle_practice_manager,  # supervisor == Practice Manager
    "death_cert": _handle_death_certificate,
    "wrong_office": _handle_wrong_office,
    "fax_notice": _handle_fax_notice,
    "office_fax_number": _handle_office_fax_number,
    "transfer_lab": _handle_department_transfer,
    "transfer_records": _handle_department_transfer,
    "insurance_update": _handle_insurance_update,
    "external_close": _handle_external_close,
}


def handle_requests_flow(message, message_lower):
    """Main entry point for app.py. Call only when requests_flow_active
    is True (set by app.py after detect_any_request_intent() fires) or
    when a fresh intent is detected on this turn. Returns a response
    string, or None if the current stage intentionally hands off to
    app.py's own logic (imaging/lab 'offer appointment' acceptance)."""
    global requests_flow_active, requests_active_workflow

    if not requests_active_workflow:
        return None
    handler = _WORKFLOW_HANDLERS.get(requests_active_workflow)
    if handler is None:
        requests_flow_active = False
        requests_active_workflow = None
        return None
    global _ext_identity_owner, ext_patient_first, ext_patient_last, ext_patient_dob
    if requests_active_workflow in (
            "life_insurance", "death_cert", "fax_notice", "hh_instructions"):
        # A second external workflow in the same call must never inherit the
        # previous workflow's patient/decedent identity.
        if _ext_identity_owner != requests_active_workflow:
            ext_patient_first = ext_patient_last = ext_patient_dob = None
            _ext_identity_owner = requests_active_workflow
    response = handler(message, message_lower)
    if response is None:
        # Handler intentionally handed off (e.g. imaging/lab
        # appointment acceptance) - clear our own state so app.py's
        # own flow owns the rest of the conversation cleanly.
        requests_flow_active = False
        requests_active_workflow = None
    elif (
            not requests_flow_active
            and requests_active_workflow in EXTERNAL_WORKFLOWS
            and not any(_verified_patient_identity())
    ):
        # An EXTERNAL caller's workflow just finished and Steve asked
        # "anything else?". Stay dispatchable for exactly one more turn so
        # their "no thanks" is closed here (see _handle_external_close).
        requests_active_workflow = "external_close"
        requests_flow_active = True
    return response


# ─────────────────────────────────────────────
# Context builder for LLM injection (consistency with Sprint13/Sprint14;
# unused today since every branch above returns a response directly)
# ─────────────────────────────────────────────

def build_context():
    if not requests_flow_active or not requests_active_workflow:
        return ""
    context = "MISC_REQUEST_WORKFLOW INJECTED BY SYSTEM:\n"
    context += f"Active workflow: {requests_active_workflow}\n"
    context += (
        "This workflow is handled deterministically by Python. Do NOT "
        "improvise - follow the Python-generated response for this "
        "turn.\n"
    )
    return context
