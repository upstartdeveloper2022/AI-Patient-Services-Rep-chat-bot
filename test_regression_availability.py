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
