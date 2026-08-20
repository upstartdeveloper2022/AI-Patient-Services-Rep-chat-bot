import importlib, app
importlib.reload(app)
msg = "I'm calling for my husband, William Vance. I need to get his recent lab results."
resp = app.determine_pre_chart_response(msg, msg.lower())
print('response1:', resp)
resp2 = app.determine_pre_chart_response('This is Jane Vance', 'this is jane vance')
print('response2:', resp2)
resp3 = app.determine_pre_chart_response('5/7/1950', '5/7/1950')
print('response3:', resp3)
