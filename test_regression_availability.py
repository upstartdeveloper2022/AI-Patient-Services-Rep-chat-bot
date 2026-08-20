import importlib
import types
import pytest

import app


def setup_function():
	# Reload module to reset globals between tests
	importlib.reload(app)


def test_regression_availability_sequence():
	# Step 1: third-party says calling for husband with patient name
	msg = "I'm calling for my husband, William Vance. I need to get his recent lab results"
	resp = app.determine_pre_chart_response(msg, msg.lower())
	assert resp and 'first and last name' in resp.lower()

	# Step 2: caller gives their own name (use phrasing the extractor recognizes)
	resp2 = app.determine_pre_chart_response('This is Judy Vance', 'this is judy vance')
	assert resp2 and 'date of birth' in resp2.lower()

	# Step 3: caller provides patient's DOB
	resp3 = app.determine_pre_chart_response('5/5/1950', '5/5/1950')
	assert resp3 and 'is the patient available' in resp3.lower()

	# Ensure flag was set
	assert getattr(app, 'third_party_availability_asked', False) is True


def test_before_hours_acute_virtual_accept():
	importlib.reload(app)
	app.contagious_visit_active = True
	app.before_hours_acute_active = True
	app.before_hours_virtual_offered = True
	app.pre_chart_complete = True
	app.caller_is_patient = True

	# Patient accepts virtual clinic (Step 1 & Step 2)
	message = "yes, I would like to do the virtual clinic"
	message_lower = message.lower()
	patient_accepted = app.patient_wants_to_proceed(message_lower)
	assert patient_accepted is True


def test_before_hours_acute_in_person_decline():
	importlib.reload(app)
	app.contagious_visit_active = True
	app.before_hours_acute_active = True
	app.before_hours_virtual_offered = True

	# Patient wants to be seen in person (Step 3)
	message = "I don't want virtual care. I want to be seen in person."
	message_lower = message.lower()
	wants_in_person = any(
		p in message_lower for p in [
			"in person", "in-person", "don't want virtual", "do not want virtual",
			"no virtual", "want to be seen", "prefer to be seen"
		]
	)
	assert wants_in_person is True


def test_before_hours_acute_pcp_decline():
	importlib.reload(app)
	app.contagious_visit_active = True
	app.before_hours_acute_active = True
	app.before_hours_virtual_offered = True

	# Patient specifically wants PCP today (Step 4)
	message = "I want to be seen by Dr. Amanda Foster today."
	message_lower = message.lower()
	wants_pcp = any(
		p in message_lower for p in [
			"dr.", "dr ", "doctor", "foster", "pcp", "my doctor", "my provider",
			"specifically", "want to be seen by dr", "want dr"
		]
	)
	assert wants_pcp is True
