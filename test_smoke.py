import app
import importlib

def scenario1_adult_commercial():
    importlib.reload(app)
    app.new_patient_flow_active = True
    app.new_patient_requested_provider = 'Dr. Chen'
    app.new_patient_is_minor = False
    app.new_patient_offered_provider = 'Dr. Chen'
    resp = app.handle_new_patient_flow('United Healthcare through my employer', 'united healthcare through my employer')
    return ('adult_commercial', resp, app.new_patient_demographics_stage)

def scenario2_minor_commercial():
    importlib.reload(app)
    app.new_patient_flow_active = True
    app.new_patient_requested_provider = 'Dr. Stroman'
    app.new_patient_is_minor = True
    app.new_patient_offered_provider = 'Dr. Sarah Mitchell'
    resp = app.handle_new_patient_flow('United Healthcare through my employer', 'united healthcare through my employer')
    return ('minor_commercial', resp, app.new_patient_demographics_stage)

def scenario3_self_pay_email():
    importlib.reload(app)
    app.new_patient_flow_active = True
    app.new_patient_requested_provider = 'Dr. Sarah Mitchell'
    app.new_patient_is_minor = False
    app.new_patient_offered_provider = 'Dr. Sarah Mitchell'
    app.new_patient_insurance_type = 'self_pay_quoted'
    app.new_patient_insurance_collected = True
    app.new_patient_insurance_accepted = True
    app.new_patient_self_pay_intent = True
    app.new_patient_demographics_stage = 'email'
    resp = app.handle_new_patient_flow('thechapster@aol.com', 'thechapster@aol.com')
    return ('self_pay_email', resp, app.new_patient_demographics_stage)

def scenario4_third_party_lab():
    importlib.reload(app)
    msg = "I'm calling for my husband, William Vance. I need to get his recent lab results."
    resp = app.determine_pre_chart_response(msg, msg.lower())
    return ('third_party_lab', resp, app.patient_first_name, app.patient_last_name)

def scenario5_minor_dob_then_caller():
    importlib.reload(app)
    app.new_patient_flow_active = True
    app.new_patient_requested_provider = 'Dr. Stroman'
    app.new_patient_is_minor = True
    app.new_patient_offered_provider = 'Dr. Sarah Mitchell'
    # Start demographics at name
    app.new_patient_insurance_collected = True
    app.new_patient_insurance_accepted = True
    app.new_patient_demographics_stage = 'name'
    r1 = app.handle_new_patient_flow('Charlie Chaplin', 'charlie chaplin')
    r2 = app.handle_new_patient_flow('4/5/2015', '4/5/2015')
    return ('minor_dob_then_caller', r1, r2, app.new_patient_demographics_stage)

if __name__ == '__main__':
    tests = [
        scenario1_adult_commercial(),
        scenario2_minor_commercial(),
        scenario3_self_pay_email(),
        scenario4_third_party_lab(),
        scenario5_minor_dob_then_caller(),
    ]
    for t in tests:
        print(t)
