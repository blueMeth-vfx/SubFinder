"""
SubFinder - plugin per DaVinci Resolve (Workspace > Scripts > Utility > SubFinder)

Cerca una parola nei sottotitoli della timeline aperta e salta al sottotitolo che
la contiene. Si apre in una finestra a parte (Chrome o Edge in modalita' app, col
logo Plutonic nella barra): la ricerca e' istantanea mentre scrivi, ◀ ▶ (o le
frecce) scorrono i risultati, un clic su una riga ci salta.

Self-contained: gira con il Python interno di Resolve, zero dipendenze esterne.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:                      # Python < 3.7
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

# --------------------------------------------------------------------------
# Connessione a Resolve
# --------------------------------------------------------------------------
try:
    resolve  # iniettata quando si lancia da Workspace > Scripts
except NameError:
    import DaVinciResolveScript as _dvr
    resolve = _dvr.scriptapp("Resolve")

APP = "SubFinder"

# --------------------------------------------------------------------------
# Finestra a parte (Chrome/Edge in modalita' "app", oppure il browser predefinito)
# servita da un piccolo server DENTRO il plugin, SOLO su questo computer.
# Gli script Utility girano in un processo a parte (fuscript), quindi il plugin
# puo' tenere il suo ciclo: la finestra manda i comandi, il plugin li esegue.
# --------------------------------------------------------------------------
_LOGO = os.path.join(os.environ.get("APPDATA", ""), "Blackmagic Design",
                     "DaVinci Resolve", "Support", "Fusion", "Scripts", "Utility",
                     "plutonic_logo.png")
_NOWIN = 0x08000000 if os.name == "nt" else 0   # niente console che lampeggia
_PL = os.path.join(os.environ.get("APPDATA", ""), "Plutonic")
_THEME_FILE = os.path.join(_PL, "theme.txt")     # condiviso con BeaTonic
_SET_FILE = os.path.join(_PL, APP.lower() + ".json")
WEB = os.path.join(tempfile.gettempdir(), "Plutonic", APP)
LOOKS = ("studio", "labs", "notte")

STATE = {"log": "", "status": "Pronto.", "busy": "", "quit": False, "pinged": 0.0}
CMDQ = queue.Queue()      # comandi dalla finestra, eseguiti dal plugin uno alla volta
SERVER = {"srv": None, "port": None}


def load_theme():
    try:
        with open(_THEME_FILE, "r") as f:
            t = f.read().strip()
        return t if t in ("studio", "labs") else "studio"
    except Exception:
        return "studio"


def save_theme(name):
    try:
        os.makedirs(_PL, exist_ok=True)
        with open(_THEME_FILE, "w") as f:
            f.write(name)
    except Exception:
        pass


def _save_settings():
    try:
        os.makedirs(_PL, exist_ok=True)
        with open(_SET_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(SET, f)
        os.replace(_SET_FILE + ".tmp", _SET_FILE)
    except Exception:
        pass


def _app_windows(title):
    """Finestre di Chrome/Edge visibili con quel titolo (solo Windows)."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    found = []

    def cb(h, _l):
        n = u.GetWindowTextLengthW(h)
        if n and u.IsWindowVisible(h):
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            if b.value == title:
                c = ctypes.create_unicode_buffer(64)
                u.GetClassNameW(h, c, 64)
                if c.value.startswith("Chrome_WidgetWin"):
                    found.append(h)
        return True
    u.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return found


def _pin_windows(titles, on, front=False):
    """Tiene le finestre del plugin SEMPRE DAVANTI (anche quando clicchi in Resolve).
    Tocca solo le finestre che non lo sono gia' (cosi' non rimescola l'ordine);
    front=True le porta comunque in cima, nell'ordine di 'titles' (l'ultima sopra)."""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        flags = 0x0001 | 0x0002 | 0x0010          # niente spostamenti, non ruba il fuoco
        n = 0
        for title in titles:
            for h in _app_windows(title):
                top = bool(u.GetWindowLongW(h, -20) & 0x8)   # WS_EX_TOPMOST
                if on and (front or not top):
                    u.SetWindowPos(h, wintypes.HWND(-1), 0, 0, 0, 0, flags)
                elif not on and front:             # in cima una volta sola, poi normale
                    u.SetWindowPos(h, wintypes.HWND(-1), 0, 0, 0, 0, flags)
                    u.SetWindowPos(h, wintypes.HWND(-2), 0, 0, 0, 0, flags)
                elif not on and top:
                    u.SetWindowPos(h, wintypes.HWND(-2), 0, 0, 0, 0, flags)
                n += 1
        return n
    except Exception:
        return 0


def _bring_front(title, on, wait=10.0):
    """Appena la finestra appare, la porta sopra le altre del plugin (in un thread)."""
    def run():
        end = time.time() + wait
        while time.time() < end:
            if _pin_windows((title,), on, front=True):
                return
            time.sleep(0.2)
    threading.Thread(target=run, daemon=True).start()


def _apply_look(d):
    """Impostazioni comuni: aspetto e "sempre davanti"."""
    if d.get("look") in LOOKS and d["look"] != SET["look"]:
        SET["look"] = d["look"]
        save_theme("labs" if SET["look"] == "labs" else "studio")
    if "ontop" in d and bool(d["ontop"]) != SET.get("ontop", True):
        SET["ontop"] = bool(d["ontop"])
        _pin_windows(TITLES, SET["ontop"])


def _load_settings():
    try:
        with open(_SET_FILE, "r", encoding="utf-8") as f:
            _apply_settings(json.load(f), save=False)
    except Exception:
        pass


def set_status(m):
    STATE["status"] = m


def set_log(m):
    STATE["log"] = m


class _Handler(BaseHTTPRequestHandler):
    """Server SOLO su 127.0.0.1: la pagina (/), lo stato (/api/state) e i comandi."""

    def log_message(self, *a):
        pass

    def _send(self, data, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        STATE["pinged"] = time.time()
        p = self.path.split("?")[0]
        try:
            if p == "/":
                self._send(PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif p == "/api/state":
                self._send(json.dumps(_state()).encode("utf-8"))
            elif p in GETS:
                self._send(json.dumps(GETS[p]()).encode("utf-8"))
            elif p in ("/logo", "/favicon.ico"):
                with open(_LOGO, "rb") as f:
                    self._send(f.read(), "image/png")
            else:
                self._send(b"not found", "text/plain", 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._send(str(e).encode("utf-8"), "text/plain", 500)
            except Exception:
                pass

    def do_POST(self):
        STATE["pinged"] = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        try:
            d = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8", "replace") or "{}")
        except Exception:
            d = {}
        p = self.path.split("?")[0]
        if p == "/api/set":
            _apply_settings(d)
        elif p == "/api/cmd":
            _apply_settings(d.get("set") or {})
            CMDQ.put((d.get("cmd"), d.get("arg")))
        else:
            return self._send(b"not found", "text/plain", 404)
        self._send(b"ok", "text/plain")


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    SERVER.update(srv=srv, port=srv.server_address[1])
    return SERVER["port"]


def _url(path=""):
    return "http://127.0.0.1:%d/%s" % (SERVER["port"], path)


def _open_window(url, size):
    """Finestra pulita (senza schede ne' barra) con Chrome o Edge; se mancano, il browser
    predefinito. Icona della finestra e della barra = logo Plutonic (favicon)."""
    flags = ["--app=" + url, "--new-window", "--no-first-run", "--no-default-browser-check",
             "--window-size=%d,%d" % size, "--user-data-dir=" + os.path.join(WEB, "profilo_browser")]
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                          os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        cands += [shutil.which(n) for n in ("google-chrome", "chromium", "microsoft-edge")]
    for c in cands:
        if c and os.path.exists(c):
            try:
                subprocess.Popen([c] + flags, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=_NOWIN)
                return True
            except Exception:
                pass
    import webbrowser
    return webbrowser.open(url)


def on_close(ev=None):
    STATE["quit"] = True


def run_app(size, idle=None, quiet=()):
    """Avvia il server, apre la finestra ed esegue i comandi finche' la finestra e'
    aperta: poi si chiude da solo. 'idle' gira ogni 2 s quando non c'e' niente da fare;
    i comandi in 'quiet' (istantanei) non accendono l'indicatore di lavoro."""
    os.makedirs(WEB, exist_ok=True)
    port = start_server()
    print("%s: finestra su http://127.0.0.1:%d/" % (APP, port))
    _open_window(_url(), size)
    _bring_front(TITLES[0], SET.get("ontop", True))
    started = last_idle = time.time()
    while not STATE["quit"]:
        try:
            cmd, arg = CMDQ.get(timeout=0.25)
        except queue.Empty:
            cmd, arg = None, None
        if cmd in CMDS:
            STATE["busy"] = "" if cmd in quiet else cmd
            try:
                CMDS[cmd](arg)
            except Exception as e:      # mai finestre bloccate: l'errore va nel log
                import traceback
                set_log("ERRORE durante '%s':\n%s" % (cmd, traceback.format_exc()[-1800:]))
                set_status("Errore: %s" % e)
            finally:
                STATE["busy"] = ""
        elif time.time() - last_idle > 2.0:
            last_idle = time.time()
            if SET.get("ontop", True):
                _pin_windows(TITLES, True)   # anche le finestre appena aperte
            try:
                if idle:
                    idle()
            except Exception:
                pass
        last = STATE["pinged"] or started
        if time.time() - last > (25 if STATE["pinged"] else 180):
            break                        # nessuna finestra aperta da un po': fine
    time.sleep(1.0)                      # la finestra fa in tempo a vedere "chiuso"
    try:
        SERVER["srv"].shutdown()
    except Exception:
        pass

# --------------------------------------------------------------------------
# Lettura sottotitoli + timecode
# --------------------------------------------------------------------------
def _fps(tl):
    try:
        f = float(tl.GetSetting("timelineFrameRate"))
    except Exception:
        f = 24.0
    for a, e in ((23.976, 24000 / 1001), (29.97, 30000 / 1001), (59.94, 60000 / 1001)):
        if abs(f - a) < 0.01:
            return e
    return f


def frames_to_tc(frame, fps):
    fps_i = max(1, int(round(fps)))
    frame = int(frame)
    ff = frame % fps_i
    total = frame // fps_i
    s = total % 60
    total //= 60
    m = total % 60
    h = total // 60
    return "%02d:%02d:%02d:%02d" % (h, m, s, ff)


def current_timeline():
    proj = resolve.GetProjectManager().GetCurrentProject()
    return proj.GetCurrentTimeline() if proj else None


def read_subs(tl):
    """Lista di dict {start, tc, text, track} ordinata per tempo, da tutte le tracce sub."""
    fps = _fps(tl)
    out = []
    try:
        n = tl.GetTrackCount("subtitle") or 0
    except Exception:
        n = 0
    for ti in range(1, n + 1):
        for it in (tl.GetItemListInTrack("subtitle", ti) or []):
            txt = (it.GetName() or "").strip()
            if not txt:
                continue
            st = it.GetStart()
            out.append({"start": st, "tc": frames_to_tc(st, fps), "text": txt, "track": ti})
    out.sort(key=lambda x: x["start"])
    return out, fps


# --------------------------------------------------------------------------
# Stato e comandi
# --------------------------------------------------------------------------
SET = {"look": load_theme(), "ontop": True, "whole": False, "accents": True}
TITLES = ("SubFinder",)
SUBS = {"tl": "", "key": "", "list": [], "fps": 24.0, "ver": 0, "tracks": 0}
CUR = {"tc": "", "text": ""}


def _apply_settings(d, save=True):
    """Impostazioni dalla finestra (validate) e ricordate per la prossima volta."""
    _apply_look(d)
    for k in ("whole", "accents"):
        if k in d:
            SET[k] = bool(d[k])
    if save:
        _save_settings()


def _tl_key(tl):
    try:
        return "%s|%s" % (tl.GetUniqueId(), tl.GetName())
    except Exception:
        return tl.GetName()


def on_refresh(ev=None):
    tl = current_timeline()
    if not tl:
        SUBS.update(tl="", key="", list=[], ver=SUBS["ver"] + 1, tracks=0)
        set_status("Nessuna timeline aperta in Resolve.")
        return
    subs, fps = read_subs(tl)
    SUBS.update(tl=tl.GetName(), key=_tl_key(tl), list=subs, fps=fps, ver=SUBS["ver"] + 1,
                tracks=tl.GetTrackCount("subtitle") or 0)
    if subs:
        set_status("Timeline «%s»: %d sottotitoli." % (SUBS["tl"], len(subs)))
    else:
        set_status("La timeline «%s» non ha sottotitoli con testo." % SUBS["tl"])


def on_goto(tc):
    """Sposta la playhead sul sottotitolo (pagina Edit)."""
    tl = current_timeline()
    if not tl or not tc:
        return
    try:
        if resolve.GetCurrentPage() not in ("edit", "cut"):
            resolve.OpenPage("edit")
    except Exception:
        pass
    ok = tl.SetCurrentTimecode(str(tc))
    for s in SUBS["list"]:
        if s["tc"] == tc:
            CUR.update(tc=tc, text=s["text"])
            break
    if not ok:
        set_status("Resolve non ha spostato la playhead su %s." % tc)


def _idle():
    """Se in Resolve apri un'altra timeline, SubFinder la rilegge da solo."""
    tl = current_timeline()
    key = _tl_key(tl) if tl else ""
    if key != SUBS["key"]:
        on_refresh()


def _state():
    return {"set": SET, "log": STATE["log"], "status": STATE["status"], "busy": STATE["busy"],
            "quit": STATE["quit"], "tl": SUBS["tl"], "n": len(SUBS["list"]), "ver": SUBS["ver"],
            "tracks": SUBS["tracks"], "cur": CUR}


GETS = {"/api/subs": lambda: {"ver": SUBS["ver"], "fps": SUBS["fps"],
                              "subs": [[s["tc"], s["text"], s["track"]] for s in SUBS["list"]]}}
CMDS = {"refresh": on_refresh, "autorefresh": on_refresh, "goto": on_goto, "quit": on_close}

PAGE_HTML = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>SubFinder</title>
<link rel="icon" id="fav" href="/logo">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Rubik+Mono+One&family=Space+Grotesk:wght@500;600;700&display=swap">
<style>
:root { --r: 16px; --gap: 16px; }
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body { font: 14px/1.45 var(--font); color: var(--fg); background: var(--bg); }
.bgfx { position: fixed; inset: 0; z-index: -1; pointer-events: none; background: var(--bgfx); overflow: hidden; }

/* ---------- aspetto STUDIO: scuro, vetro, bagliori viola ---------- */
body.look-studio {
  --font: "Inter", "Segoe UI", system-ui, sans-serif; --hfont: 700 12px "Inter", "Segoe UI", sans-serif;
  --titlefont: 800 30px "Inter", "Segoe UI", sans-serif; --titlels: -.02em; --ctafont: "Inter", "Segoe UI", sans-serif;
  --bg: #0a0a12;
  --bgfx: radial-gradient(900px 500px at 0% -10%, rgba(124,58,237,.38), transparent 60%),
          radial-gradient(800px 500px at 110% 0%, rgba(192,38,211,.22), transparent 60%),
          radial-gradient(900px 600px at 50% 120%, rgba(79,70,229,.25), transparent 60%);
  --fg: #ece9fb; --muted: #a39fc0; --head: #c4b5fd; --logfg: #d6d1f0;
  --card: rgba(255,255,255,.045); --tile: rgba(255,255,255,.04); --seg: rgba(255,255,255,.05);
  --seg-h: rgba(255,255,255,.09); --btn: rgba(255,255,255,.07); --btn-h: rgba(255,255,255,.12);
  --bd: 1px solid rgba(255,255,255,.09); --bd2: 1px solid rgba(255,255,255,.08);
  --shadow: 0 20px 50px rgba(0,0,0,.35); --blur: blur(14px);
  --accent: #8b5cf6; --accent-bg: linear-gradient(135deg, #7c3aed, #c026d3); --onaccent: #fff;
  --onshadow: 0 8px 24px rgba(124,58,237,.45);
  --titlebg: linear-gradient(90deg, #fff, #c4b5fd 60%, #f0abfc); --logofx: drop-shadow(0 6px 18px rgba(192,38,211,.45));
}
/* ---------- aspetto LABS: carta, bordi pieni, ombre nette, tramonto ---------- */
body.look-labs {
  --r: 10px;
  --font: "Inter", "Public Sans", "Segoe UI", sans-serif; --hfont: 400 12px "Rubik Mono One", "Impact", sans-serif;
  --titlefont: 400 30px "Rubik Mono One", "Impact", sans-serif; --titlels: 0; --ctafont: "Rubik Mono One", "Impact", sans-serif;
  --bg: #F3F0F7; --bgfx: radial-gradient(#d8c8ea 1.2px, transparent 1.4px) 0 0 / 18px 18px;
  --fg: #0E0418; --muted: #4B4458; --head: #B0126E; --logfg: #24103F;
  --card: #FBF4FF; --tile: #F3F0F7; --seg: #fff; --seg-h: #F1E8FA; --btn: #fff; --btn-h: #F4F07C;
  --bd: 2.5px solid #0E0418; --bd2: 2px solid #0E0418; --shadow: 6px 6px 0 #0E0418; --blur: none;
  --accent: #EE3A5B; --accent-bg: linear-gradient(90deg, #F4F07C, #F7A541 45%, #EE3A5B); --onaccent: #0E0418;
  --onshadow: 3px 3px 0 #0E0418;
  --titlebg: linear-gradient(180deg, #F7A541, #EE3A5B 55%, #B0126E); --logofx: none;
}
body.look-labs .btn { box-shadow: 3px 3px 0 #0E0418; }
body.look-labs .btn:active, body.look-labs .seg button:active { transform: translate(3px, 3px); box-shadow: none; }
/* ---------- aspetto NOTTE: notte viola, tramonto all'orizzonte, griglia synth ---------- */
body.look-notte {
  --font: "Space Grotesk", "Inter", "Segoe UI", sans-serif; --hfont: 400 12px "Rubik Mono One", "Impact", sans-serif;
  --titlefont: 400 32px "Rubik Mono One", "Impact", sans-serif; --titlels: .02em; --ctafont: "Rubik Mono One", "Impact", sans-serif;
  --bg: #0c0418;
  --bgfx: radial-gradient(760px 380px at 50% 108%, rgba(247,165,65,.55), rgba(238,58,91,.35) 35%, rgba(176,18,110,.18) 60%, transparent 75%),
          linear-gradient(180deg, #12071F 0%, #1d0b36 55%, #2A0B4A 100%);
  --fg: #fbefff; --muted: #c9b3df; --head: #F7A541; --logfg: #f2dcff;
  --card: rgba(20,8,38,.72); --tile: rgba(255,255,255,.04); --seg: rgba(255,255,255,.05);
  --seg-h: rgba(247,165,65,.12); --btn: rgba(255,255,255,.06); --btn-h: rgba(238,58,91,.18);
  --bd: 1px solid rgba(247,165,65,.28); --bd2: 1px solid rgba(255,255,255,.1);
  --shadow: 0 24px 60px rgba(0,0,0,.5), 0 0 40px rgba(176,18,110,.18); --blur: blur(10px);
  --accent: #EE3A5B; --accent-bg: linear-gradient(90deg, #F4F07C, #F7A541 30%, #EE3A5B 65%, #B0126E);
  --onaccent: #12071F; --onshadow: 0 0 24px rgba(238,58,91,.5);
  --titlebg: linear-gradient(180deg, #F4F07C, #F7A541 35%, #EE3A5B 65%, #B0126E);
  --logofx: drop-shadow(0 0 16px rgba(247,165,65,.55));
}
body.look-notte .bgfx::after {
  content: ""; position: absolute; left: -50%; right: -50%; bottom: 0; height: 42%;
  background-image: linear-gradient(rgba(238,58,91,.28) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(238,58,91,.28) 1px, transparent 1px);
  background-size: 64px 42px; transform: perspective(520px) rotateX(62deg); transform-origin: bottom;
  -webkit-mask-image: linear-gradient(transparent, #000 75%); mask-image: linear-gradient(transparent, #000 75%);
}

/* ---------- struttura ---------- */
.top { display: flex; align-items: center; gap: 14px; padding: 18px 24px 6px; max-width: 1280px; margin: 0 auto; }
#logo { height: 48px; width: auto; filter: var(--logofx); }
.brand .name { font: var(--titlefont); letter-spacing: var(--titlels); background: var(--titlebg);
  -webkit-background-clip: text; background-clip: text; color: transparent; line-height: 1.05; }
.brand .tag { color: var(--muted); font-size: 12.5px; letter-spacing: .04em; }
.spacer { flex: 1; }
.looks { display: flex; gap: 4px; padding: 4px; border-radius: 999px; background: var(--seg); border: var(--bd2); }
.looks span { align-self: center; padding: 0 6px 0 8px; font-size: 11px; color: var(--muted); }
.looks button { all: unset; cursor: pointer; padding: 6px 12px; border-radius: 999px; font-weight: 700; font-size: 12px; color: var(--muted); }
.looks button.on { background: var(--accent-bg); color: var(--onaccent); }
.pin { all: unset; cursor: pointer; padding: 7px 13px; border-radius: 999px; font-weight: 700; font-size: 12px;
  color: var(--muted); background: var(--seg); border: var(--bd2); white-space: nowrap; }
.pin.on { background: var(--accent-bg); color: var(--onaccent); }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap); padding: 14px 24px 24px; max-width: 1280px; margin: 0 auto; }
.card { background: var(--card); border: var(--bd); border-radius: var(--r); padding: 16px 18px; box-shadow: var(--shadow);
  backdrop-filter: var(--blur); -webkit-backdrop-filter: var(--blur); display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.wide { grid-column: 1 / -1; }
h2 { margin: 0 0 2px; font: var(--hfont); letter-spacing: .12em; text-transform: uppercase; color: var(--head);
  display: flex; align-items: center; gap: 10px; }
h2 .n { display: inline-grid; place-items: center; width: 24px; height: 24px; border-radius: 50%;
  background: var(--accent-bg); color: var(--onaccent); font-size: 12px; letter-spacing: 0; }
label { font-size: 12px; color: var(--muted); font-weight: 600; letter-spacing: .03em; }
.tiles { display: grid; grid-template-columns: 1fr 1.7fr; gap: 10px; }
.tile { background: var(--tile); border: var(--bd2); border-radius: calc(var(--r) - 4px); padding: 10px 12px; min-width: 0; }
.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); }
.tile .v { font-size: 30px; font-weight: 800; line-height: 1.15; }
.tile .v.song { font-size: 15px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 6px 0 2px; }
.tile .sub { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.seg { display: flex; flex-wrap: wrap; gap: 6px; }
.seg button { all: unset; cursor: pointer; flex: 1 1 0; min-width: 84px; text-align: center; padding: 8px 10px;
  border-radius: calc(var(--r) - 6px); background: var(--seg); border: var(--bd2); transition: background .15s, transform .1s; }
.seg button b { display: block; font-weight: 700; font-size: 13px; }
.seg button small { display: block; font-size: 11px; color: var(--muted); }
.seg button:hover { background: var(--seg-h); }
.seg button.on { background: var(--accent-bg); color: var(--onaccent); box-shadow: var(--onshadow); }
.seg button.on small { color: inherit; opacity: .8; }
.switch { all: unset; cursor: pointer; width: 54px; height: 32px; border-radius: 999px; background: var(--seg); border: var(--bd2); position: relative; display: block; }
.switch i { position: absolute; top: 4px; left: 4px; width: 22px; height: 22px; border-radius: 50%; background: var(--muted); transition: .2s; }
.switch.on { background: var(--accent-bg); }
.switch.on i { left: 26px; background: var(--onaccent); }
.btn { all: unset; cursor: pointer; text-align: center; padding: 11px 16px; border-radius: calc(var(--r) - 4px); font-weight: 700;
  background: var(--btn); border: var(--bd2); transition: background .15s, transform .1s; }
.btn:hover { background: var(--btn-h); }
.btn.primary, .btn.cta { background: var(--accent-bg); color: var(--onaccent); box-shadow: var(--onshadow); }
.btn.cta { font-family: var(--ctafont); font-size: 24px; padding: 24px; letter-spacing: .04em; }
.btn.ghost { padding: 6px 12px; font-size: 12px; }
.btn[disabled] { opacity: .45; pointer-events: none; }
.btn.working { position: relative; overflow: hidden; opacity: 1 !important; }
.btn.working::after { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, rgba(255,255,255,.4), transparent); animation: sh 1.1s infinite; }
@keyframes sh { from { transform: translateX(-100%); } to { transform: translateX(100%); } }
.hint { margin: -4px 0 4px; font-size: 12px; color: var(--muted); }
.link { all: unset; cursor: pointer; margin-top: 4px; font-size: 12px; font-weight: 700; color: var(--head); }
.link:hover { text-decoration: underline; }
.link[hidden] { display: none; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; min-height: 28px; }
.chip { padding: 4px 10px; border-radius: 999px; background: var(--tile); border: var(--bd2); font-size: 12px; color: var(--muted); }
.chip b { font-size: 14px; color: var(--fg); }
.go { justify-content: center; }
.status { display: flex; align-items: center; gap: 10px; font-weight: 600; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: #22c55e; box-shadow: 0 0 0 4px rgba(34,197,94,.18); flex: none; }
body.busy .dot { background: var(--accent); box-shadow: 0 0 0 4px rgba(238,58,91,.2); animation: pulse 1s infinite; }
@keyframes pulse { 50% { opacity: .35; } }
pre.log { margin: 0; max-height: 240px; overflow: auto; white-space: pre-wrap; font: 12.5px/1.55 ui-monospace, Consolas, monospace;
  color: var(--logfg); background: var(--tile); border: var(--bd2); border-radius: calc(var(--r) - 6px); padding: 12px 14px; }
.overlay { position: fixed; inset: 0; display: grid; place-items: center; background: rgba(0,0,0,.6); z-index: 9; }
.overlay[hidden] { display: none; }
.overlay .card { max-width: 520px; }
@media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }

.grid { grid-template-columns: 1fr; max-width: 980px; }
.top { max-width: 980px; }
.src { display: flex; align-items: center; gap: 14px; }
.src .tile { flex: 1; }
.src .v.song { font-size: 17px; }
.search { position: relative; }
.search input { width: 100%; font: inherit; font-size: 22px; font-weight: 700; color: var(--fg); background: var(--seg);
  border: var(--bd2); border-radius: calc(var(--r) - 4px); padding: 14px 16px 14px 50px; outline: none; }
.search input:focus { border-color: var(--accent); box-shadow: var(--onshadow); }
.search .ico { position: absolute; left: 17px; top: 50%; transform: translateY(-50%); font-size: 20px; opacity: .7; pointer-events: none; }
.opts { display: flex; flex-wrap: wrap; gap: 18px; align-items: center; }
.opt { display: flex; align-items: center; gap: 9px; font-size: 13px; font-weight: 600; cursor: pointer; user-select: none; }
.opt .switch { width: 44px; height: 26px; }
.opt .switch i { width: 16px; height: 16px; }
.opt .switch.on i { left: 22px; }
.keys { margin-left: auto; font-size: 11.5px; color: var(--muted); }
kbd { font: 600 11px ui-monospace, Consolas, monospace; padding: 1px 6px; border-radius: 5px; border: var(--bd2); background: var(--tile); }
.nav { display: grid; grid-template-columns: auto auto auto 1fr; gap: 12px; align-items: center; }
.nav .btn { font-size: 20px; padding: 10px 20px; }
.count { font: var(--titlefont); font-size: 26px; min-width: 110px; text-align: center; background: var(--titlebg);
  -webkit-background-clip: text; background-clip: text; color: transparent; }
.curbox { min-width: 0; background: var(--tile); border: var(--bd2); border-radius: calc(var(--r) - 4px); padding: 10px 14px; }
.curbox .tc { font: 700 12px ui-monospace, Consolas, monospace; color: var(--head); letter-spacing: .04em; }
.curbox .tx { font-size: 16px; font-weight: 600; white-space: pre-wrap; overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 3; -webkit-box-orient: vertical; }
mark { background: var(--accent-bg); color: var(--onaccent); border-radius: 4px; padding: 0 2px; }
.listhead { display: flex; align-items: baseline; gap: 10px; }
.listhead .spacer { flex: 1; }
.list { max-height: 46vh; min-height: 120px; overflow: auto; border: var(--bd2); border-radius: calc(var(--r) - 6px); background: var(--tile); }
.it { display: grid; grid-template-columns: 46px 104px 1fr; gap: 10px; padding: 8px 12px; cursor: pointer; align-items: baseline;
  border-bottom: 1px solid rgba(128,128,128,.14); }
.it:hover { background: var(--seg-h); }
.it.on { background: var(--accent-bg); color: var(--onaccent); }
.it.on mark { background: var(--bg); color: var(--fg); }
.it .n { font-size: 12px; color: var(--muted); text-align: right; }
.it.on .n, .it.on .t { color: inherit; opacity: .85; }
.it .t { font: 600 12px ui-monospace, Consolas, monospace; color: var(--head); }
.it .x { white-space: pre-wrap; min-width: 0; }
.empty { padding: 26px; text-align: center; color: var(--muted); }
pre.log:empty { display: none; }
@media (max-width: 640px) { .nav { grid-template-columns: auto auto auto; } .curbox { grid-column: 1 / -1; } .keys { display: none; } }
</style>
</head>
<body class="look-studio" data-look="studio">
<div class="bgfx"></div>
<header class="top">
  <img id="logo" src="/logo" alt="">
  <div class="brand"><div class="name">SubFinder</div><div class="tag">cerca nei sottotitoli e saltaci · Plutonic</div></div>
  <div class="spacer"></div>
  <button class="pin" id="pin" title="Tiene la finestra davanti a Resolve anche quando clicchi nella timeline">📌 Sempre davanti</button>
  <div class="looks" title="Aspetto della finestra">
    <span>Aspetto</span>
    <button data-look="studio">Studio</button><button data-look="labs">Labs</button><button data-look="notte">Notte</button>
  </div>
</header>
<main class="grid">
  <section class="card">
    <div class="src">
      <div class="tile"><div class="k">Timeline aperta in Resolve</div><div class="v song" id="tlName">—</div>
        <div class="sub" id="tlInfo">—</div></div>
      <button class="btn" id="bRefresh" title="Rilegge i sottotitoli (se li hai cambiati in Resolve)">↻ Aggiorna</button>
    </div>
  </section>
  <section class="card">
    <div class="search"><span class="ico">🔎</span>
      <input id="q" type="search" placeholder="Scrivi una parola o una frase…" autocomplete="off" spellcheck="false"></div>
    <div class="opts">
      <label class="opt"><button class="switch" id="whole" role="switch"><i></i></button>parola intera</label>
      <label class="opt" title="«perche» trova anche «perché»"><button class="switch" id="accents" role="switch"><i></i></button>ignora gli accenti</label>
      <span class="keys"><kbd>Invio</kbd>/<kbd>↓</kbd> successivo · <kbd>↑</kbd> precedente · <kbd>Esc</kbd> pulisci</span>
    </div>
    <div class="nav">
      <button class="btn primary" id="bPrev" title="Risultato precedente (↑)">◀</button>
      <div class="count" id="count">0 / 0</div>
      <button class="btn primary" id="bNext" title="Risultato successivo (↓ o Invio)">▶</button>
      <div class="curbox"><div class="tc" id="curTc">—</div><div class="tx" id="curTx">Scrivi cosa cercare: la playhead salta sul sottotitolo.</div></div>
    </div>
  </section>
  <section class="card">
    <div class="listhead"><h2 id="listTitle">Sottotitoli</h2><span class="spacer"></span><span class="hint" style="margin:0">clic su una riga = la playhead ci salta</span></div>
    <div class="list" id="list"></div>
  </section>
  <section class="card">
    <div class="status"><span class="dot"></span><span id="status">Pronto.</span><span class="spacer"></span>
      <button class="btn ghost" id="bQuit" title="Chiude SubFinder">Chiudi SubFinder</button></div>
    <pre class="log" id="log"></pre>
  </section>
</main>
<div id="off" class="overlay" hidden><div class="card"><h2>SubFinder è chiuso</h2>
  <p>Per riaprirlo, in DaVinci Resolve: <b>Workspace ▸ Scripts ▸ Utility ▸ SubFinder</b>.</p></div></div>
<script>
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
let ST = null, inited = false, lastLog = null, fails = 0, lastLocal = 0;
const post = (p, d) => fetch(p, {method: "POST", body: JSON.stringify(d)}).catch(() => {});
const full = () => Object.assign({look: document.body.dataset.look, ontop: $("pin").classList.contains("on")}, collect());
function push(extra) { lastLocal = Date.now(); post("/api/set", Object.assign(full(), extra || {})); }
function cmd(c, arg, quiet) {
  if (ST && ST.busy && !quiet) return;
  lastLocal = Date.now();
  post("/api/cmd", {cmd: c, arg: arg, set: full()});
  if (ST && !quiet) { ST.busy = c; renderCommon(); render(); }
}
function setLook(l) {
  document.body.className = "look-" + l;
  document.body.dataset.look = l;
  for (const b of document.querySelectorAll(".looks button")) b.classList.toggle("on", b.dataset.look === l);
}
function favicon() {                          // logo Plutonic quadrato: icona della finestra e della barra
  const im = new Image();
  im.onload = () => {
    const c = document.createElement("canvas"), g = c.getContext("2d");
    c.width = c.height = 128;
    const s = Math.min(128 / im.width, 128 / im.height);
    g.drawImage(im, (128 - im.width * s) / 2, (128 - im.height * s) / 2, im.width * s, im.height * s);
    $("fav").href = c.toDataURL("image/png");
  };
  im.src = "/logo";
}
function renderCommon() {
  if (ST.log !== lastLog) { const L = $("log"); L.textContent = ST.log; L.scrollTop = 0; lastLog = ST.log; }
  $("status").textContent = ST.busy ? (BUSY[ST.busy] || "Lavoro…") : ST.status;
  document.body.classList.toggle("busy", !!ST.busy);
  for (const [c, id] of Object.entries(BTN)) {
    $(id).disabled = !!ST.busy && c !== ST.busy;
    $(id).classList.toggle("working", ST.busy === c);
  }
}
async function poll() {
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    ST = await r.json();
    fails = 0; $("off").hidden = true;
    if (!inited) { inited = true; setLook(ST.set.look || "studio"); $("pin").classList.toggle("on", ST.set.ontop !== false); init(); }
    renderCommon();
    render();
    if (ST.quit) { $("off").hidden = false; setTimeout(() => window.close(), 400); return; }
  } catch (e) { if (++fails > 3) $("off").hidden = false; }
  setTimeout(poll, ST && ST.busy ? 300 : 600);
}
for (const b of document.querySelectorAll(".looks button")) b.onclick = () => { setLook(b.dataset.look); push(); };
$("pin").onclick = () => { $("pin").classList.toggle("on"); push(); };
function start() {
  for (const [c, id] of Object.entries(BTN)) if (!$(id).onclick) $(id).onclick = () => cmd(c);
  favicon();
  poll();
}

const BUSY = {refresh: "Leggo i sottotitoli della timeline…", quit: "Chiudo…"};
const BTN = {refresh: "bRefresh", quit: "bQuit"};
let SUBS = [], subsVer = -1, RES = [], pos = -1, lastSig = "";
const collect = () => ({whole: $("whole").classList.contains("on"), accents: $("accents").classList.contains("on")});

// testo "normalizzato" lettera per lettera (stessa lunghezza: le posizioni restano valide)
function norm(s, acc) {
  let o = "";
  for (const c of s.toLowerCase()) o += acc ? (c.normalize("NFD")[0] || c) : c;
  return o;
}
const WORD = /[\p{L}\p{N}]/u;
function hits(text, q, whole, acc) {
  const t = norm(text, acc), out = [];
  let i = t.indexOf(q);
  while (i >= 0) {
    const ok = !whole || ((i === 0 || !WORD.test(t[i - 1])) && (i + q.length >= t.length || !WORD.test(t[i + q.length])));
    if (ok) out.push([i, i + q.length]);
    i = t.indexOf(q, i + Math.max(1, q.length));
  }
  return out;
}
function marked(text, hs) {
  if (!hs || !hs.length) return esc(text);
  let o = "", p = 0;
  for (const [a, b] of hs) { o += esc(text.slice(p, a)) + "<mark>" + esc(text.slice(a, b)) + "</mark>"; p = b; }
  return o + esc(text.slice(p));
}
function search(keepTc) {
  const raw = $("q").value.trim(), c = collect(), q = norm(raw, c.accents);
  RES = [];
  SUBS.forEach((s, i) => {
    if (!q) { RES.push({i: i, h: []}); return; }
    const h = hits(s[1], q, c.whole, c.accents);
    if (h.length) RES.push({i: i, h: h});
  });
  pos = -1;
  if (keepTc) pos = RES.findIndex(r => SUBS[r.i][0] === keepTc);
  if (pos < 0 && q && RES.length) pos = 0;
  drawList(!!q);
  showCur(q && pos >= 0);
}
function drawList(isQuery) {
  $("listTitle").textContent = isQuery ? RES.length + " risultat" + (RES.length === 1 ? "o" : "i")
                                       : "Tutti i sottotitoli (" + SUBS.length + ")";
  const L = $("list");
  if (!RES.length) {
    L.innerHTML = "<div class='empty'>" + (SUBS.length ? "Nessun sottotitolo contiene «" + esc($("q").value.trim()) + "»."
      : "Questa timeline non ha sottotitoli con testo.<br>Aggiungi una traccia sottotitoli (o importa un .srt) e premi Aggiorna.") + "</div>";
    return;
  }
  L.innerHTML = RES.map((r, k) => "<div class='it' data-k='" + k + "'><span class='n'>" + (k + 1) + "</span><span class='t'>" +
    SUBS[r.i][0] + "</span><span class='x'>" + marked(SUBS[r.i][1], r.h) + "</span></div>").join("");
}
function showCur(jump) {
  const L = $("list");
  for (const el of L.querySelectorAll(".it.on")) el.classList.remove("on");
  const isQ = !!$("q").value.trim();
  $("count").textContent = pos >= 0 ? (pos + 1) + " / " + RES.length : (isQ ? "0 / 0" : "—");
  if (pos < 0) {
    $("curTc").textContent = "—";
    $("curTx").textContent = isQ ? "Nessun risultato." : "Scrivi cosa cercare: la playhead salta sul sottotitolo.";
    return;
  }
  const r = RES[pos], s = SUBS[r.i];
  $("curTc").textContent = s[0] + (ST && ST.tracks > 1 ? "  ·  traccia ST" + s[2] : "");
  $("curTx").innerHTML = marked(s[1], r.h);
  const el = L.querySelector("[data-k='" + pos + "']");
  if (el) { el.classList.add("on"); el.scrollIntoView({block: "nearest"}); }
  if (jump) cmd("goto", s[0], true);
}
function step(d) {
  if (!RES.length) return;
  pos = pos < 0 ? (d > 0 ? 0 : RES.length - 1) : (pos + d + RES.length) % RES.length;
  showCur(true);
}
async function loadSubs() {
  try {
    const d = await (await fetch("/api/subs", {cache: "no-store"})).json();
    const keep = pos >= 0 && RES[pos] ? SUBS[RES[pos].i][0] : null;
    SUBS = d.subs; subsVer = d.ver;
    search(keep);
  } catch (e) {}
}
function init() {
  $("whole").classList.toggle("on", !!ST.set.whole);
  $("accents").classList.toggle("on", ST.set.accents !== false);
  const q0 = new URLSearchParams(location.search).get("q");   // ?q=parola: ricerca gia' scritta
  if (q0) $("q").value = q0;
  $("q").focus();
}
function render() {
  $("tlName").textContent = ST.tl || "nessuna timeline aperta";
  $("tlInfo").textContent = ST.tl ? ST.n + " sottotitoli" + (ST.tracks > 1 ? " su " + ST.tracks + " tracce" : "") : "apri una timeline in Resolve";
  if (ST.ver !== subsVer) { subsVer = ST.ver; loadSubs(); }
  $("bPrev").disabled = $("bNext").disabled = !RES.length;
}
let typing = null;
$("q").addEventListener("input", () => {        // mentre scrivi: dopo una pausa salta al primo risultato
  search(null);
  clearTimeout(typing);
  typing = setTimeout(() => { typing = null; if (pos >= 0) showCur(true); }, 450);
});
$("q").addEventListener("keydown", e => {
  if (e.key === "Enter" && typing) { e.preventDefault(); clearTimeout(typing); typing = null; if (pos >= 0) showCur(true); }
  else if (e.key === "Enter" || e.key === "ArrowDown" || e.key === "F3" && !e.shiftKey) { e.preventDefault(); clearTimeout(typing); typing = null; step(1); }
  else if (e.key === "ArrowUp" || e.key === "F3" && e.shiftKey) { e.preventDefault(); clearTimeout(typing); typing = null; step(-1); }
  else if (e.key === "Escape") { $("q").value = ""; search(null); }
});
document.addEventListener("keydown", e => {
  if (e.target === $("q")) return;
  if (e.key === "ArrowDown" || e.key === "F3" && !e.shiftKey) { e.preventDefault(); step(1); }
  else if (e.key === "ArrowUp" || e.key === "F3" && e.shiftKey) { e.preventDefault(); step(-1); }
  else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) $("q").focus();
});
$("list").addEventListener("click", e => {
  const el = e.target.closest(".it");
  if (!el) return;
  pos = +el.dataset.k;
  showCur(true);
});
for (const id of ["whole", "accents"])
  $(id).onclick = e => { e.preventDefault(); $(id).classList.toggle("on"); push(); search(pos >= 0 && RES[pos] ? SUBS[RES[pos].i][0] : null); };
$("bPrev").onclick = () => step(-1);
$("bNext").onclick = () => step(1);
window.addEventListener("focus", () => { if (ST && !ST.busy) cmd("autorefresh", null, true); });   // torni dalla timeline: rilegge
start();
</script>
</body>
</html>
"""


def main():
    _load_settings()
    try:
        on_refresh()
    except Exception:
        pass
    run_app((900, 820), idle=_idle, quiet=("goto", "autorefresh"))


main()
