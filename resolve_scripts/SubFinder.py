"""
SubFinder - GUI per DaVinci Resolve (Workspace > Scripts > Utility > SubFinder)

Cerca una parola nei sottotitoli della timeline aperta e salta al sottotitolo che
la contiene. Se la parola compare piu' volte, usa le freccette  ◀  ▶  per scorrere
tra i risultati. Doppio click su una riga per saltarci.

Self-contained: gira con il Python interno di Resolve, zero dipendenze esterne.
"""

import re

# --------------------------------------------------------------------------
# Connessione a Resolve / Fusion
# --------------------------------------------------------------------------
try:
    resolve  # iniettata quando si lancia da Workspace > Scripts
except NameError:
    import DaVinciResolveScript as _dvr
    resolve = _dvr.scriptapp("Resolve")

fusion = resolve.Fusion()
ui = fusion.UIManager
try:
    disp = bmd.UIDispatcher(ui)
except NameError:
    import fusionscript as bmd
    disp = bmd.UIDispatcher(ui)


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
    tl = proj.GetCurrentTimeline() if proj else None
    return tl


def read_subs(tl):
    """Lista di dict {start, tc, text} ordinata per tempo, da tutte le tracce sub."""
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
            out.append({"start": st, "tc": frames_to_tc(st, fps), "text": txt})
    out.sort(key=lambda x: x["start"])
    return out, fps


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
ACCENT = "#4cc38a"  # verde (MakeACut ambra, MakeBroll ciano)
STYLE = """
* { font-family: 'Segoe UI','Helvetica Neue',sans-serif; font-size: 13px;
    color: #e6e8ea; outline: none; }
QWidget { background-color: #1b1e23; }
QLabel#Header { font-size: 20px; font-weight: 800; color: %(a)s; }
QLabel#Src { color: #b8c0cc; padding: 4px 2px; }
QLabel#Count { color: %(a)s; font-weight: 800; }
QLabel#Cur { color: #e6e8ea; }
QLineEdit { background: #262a31; border: 1px solid #3a3f47; border-radius: 5px;
    padding: 6px 8px; }
QLineEdit:focus { border: 1px solid %(a)s; }
QPushButton { background: #333842; border: 1px solid #444b55; border-radius: 6px;
    padding: 6px 12px; }
QPushButton:hover { background: #3d434e; }
QPushButton#Find, QPushButton#Prev, QPushButton#Next {
    background: %(a)s; color: #10241b; font-weight: 800; border: 0; }
QPushButton#Find:hover, QPushButton#Prev:hover, QPushButton#Next:hover { background: #6ad7a4; }
QCheckBox::indicator { width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid #4a515b; background: #262a31; }
QCheckBox::indicator:checked { background: %(a)s; border: 1px solid %(a)s; }
QTreeWidget, QTreeView { background: #15181c; border: 1px solid #333842;
    border-radius: 6px; alternate-background-color: #191d22; }
QHeaderView::section { background: #262a31; color: #aab2bd; border: 0;
    border-bottom: 1px solid #333842; padding: 6px; font-weight: 700; }
QTreeWidget::item { padding: 3px; }
QTreeWidget::item:selected { background: %(a)s; color: #10241b; }
QLabel#Status { color: #9aa2ad; background: #15181c; border: 1px solid #333842;
    border-radius: 5px; padding: 6px 9px; }
""" % {"a": ACCENT}

COLS = ["#", "Timecode", "Sottotitolo"]

win = disp.AddWindow({
    "ID": "SubFinderWin",
    "WindowTitle": "SubFinder",
    "Geometry": [130, 100, 860, 560],
    "StyleSheet": STYLE,
}, [
    ui.VGroup({"Spacing": 7, "Margin": 14}, [
        ui.HGroup({"Weight": 0, "Spacing": 10}, [
            ui.Label({"ID": "Header", "Text": "🔎  SubFinder", "Weight": 0}),
            ui.Label({"Text": "cerca una parola nei sottotitoli e saltaci",
                      "Weight": 1, "Alignment": {"AlignBottom": True}}),
        ]),
        ui.Label({"ID": "Src", "Text": "Timeline: (aggiorna)", "Weight": 0}),

        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.LineEdit({"ID": "Query", "PlaceholderText": "parola da cercare…", "Weight": 6}),
            ui.CheckBox({"ID": "Whole", "Text": "parola intera", "Weight": 0}),
            ui.Button({"ID": "Find", "Text": "Cerca", "Weight": 2}),
            ui.Button({"ID": "Refresh", "Text": "Aggiorna", "Weight": 2}),
        ]),
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Button({"ID": "Prev", "Text": "◀", "Weight": 1}),
            ui.Label({"ID": "Count", "Text": "0 / 0", "Weight": 0,
                      "Alignment": {"AlignHCenter": True, "AlignVCenter": True}}),
            ui.Button({"ID": "Next", "Text": "▶", "Weight": 1}),
            ui.Label({"ID": "Cur", "Text": "—", "Weight": 8, "WordWrap": True}),
        ]),

        ui.Tree({"ID": "Tree", "Weight": 1, "AlternatingRowColors": True,
                 "RootIsDecorated": False, "ItemsExpandable": False}),
        ui.HGroup({"Weight": 0, "Spacing": 8}, [
            ui.Label({"ID": "Status", "Text": "Pronto.", "Weight": 1, "WordWrap": True}),
            ui.Button({"ID": "Close", "Text": "Chiudi", "Weight": 0}),
        ]),
    ]),
])

itm = win.GetItems()
try:
    itm["Tree"].ColumnCount = len(COLS)
    itm["Tree"].SetHeaderLabels(COLS)
except Exception:
    pass

SUBS = []       # tutti i sottotitoli
FPS = 24.0
RESULTS = []    # indici in SUBS che matchano
CUR = -1        # posizione corrente in RESULTS
ITEMS = []      # righe del Tree parallele a RESULTS


def set_status(msg):
    itm["Status"].Text = msg


def refresh(ev=None):
    global SUBS, FPS
    tl = current_timeline()
    if not tl:
        itm["Src"].Text = "Nessuna timeline aperta."
        SUBS = []
        return
    SUBS, FPS = read_subs(tl)
    itm["Src"].Text = "Timeline: %s   ·   %d sottotitoli" % (tl.GetName(), len(SUBS))
    if not SUBS:
        set_status("Questa timeline non ha una traccia sottotitoli con testo.")


def _matches(query, whole):
    q = query.lower()
    res = []
    for i, s in enumerate(SUBS):
        low = s["text"].lower()
        if whole:
            toks = re.split(r"[^0-9a-zàèéìòùáíóú]+", low)
            if q in toks:
                res.append(i)
        else:
            if q in low:
                res.append(i)
    return res


def render_results():
    global ITEMS
    tree = itm["Tree"]
    try:
        tree.Clear()
    except Exception:
        pass
    ITEMS = []
    for n, idx in enumerate(RESULTS, 1):
        s = SUBS[idx]
        it = tree.NewItem()
        it.Text[0] = str(n)
        it.Text[1] = s["tc"]
        it.Text[2] = s["text"]
        tree.AddTopLevelItem(it)
        ITEMS.append(it)


def _goto():
    """Sposta la playhead sul risultato corrente e aggiorna la UI."""
    if not RESULTS:
        itm["Count"].Text = "0 / 0"
        itm["Cur"].Text = "—"
        return
    s = SUBS[RESULTS[CUR]]
    itm["Count"].Text = "%d / %d" % (CUR + 1, len(RESULTS))
    itm["Cur"].Text = "%s   %s" % (s["tc"], s["text"])
    tl = current_timeline()
    if tl:
        try:
            resolve.OpenPage("edit")
        except Exception:
            pass
        try:
            tl.SetCurrentTimecode(s["tc"])
        except Exception:
            pass
    # evidenzia la riga corrispondente
    if 0 <= CUR < len(ITEMS):
        try:
            itm["Tree"].SetCurrentItem(ITEMS[CUR])
        except Exception:
            pass


def on_find(ev=None):
    global RESULTS, CUR
    if not SUBS:
        refresh()
    q = itm["Query"].Text.strip()
    if not q:
        set_status("Scrivi una parola da cercare.")
        return
    RESULTS = _matches(q, bool(itm["Whole"].Checked))
    render_results()
    if not RESULTS:
        CUR = -1
        itm["Count"].Text = "0 / 0"
        itm["Cur"].Text = "—"
        set_status("Nessun sottotitolo contiene «%s»." % q)
        return
    CUR = 0
    _goto()
    set_status("%d risultati per «%s». Usa ◀ ▶ per scorrere." % (len(RESULTS), q))


def on_next(ev=None):
    global CUR
    if not RESULTS:
        return
    CUR = (CUR + 1) % len(RESULTS)
    _goto()


def on_prev(ev=None):
    global CUR
    if not RESULTS:
        return
    CUR = (CUR - 1) % len(RESULTS)
    _goto()


def on_tree(ev=None):
    global CUR
    tree = itm["Tree"]
    try:
        cur = tree.CurrentItem()
    except Exception:
        cur = None
    if cur is None:
        return
    for i, it in enumerate(ITEMS):
        if it == cur:
            CUR = i
            _goto()
            return


def on_close(ev):
    disp.ExitLoop()


win.On.Find.Clicked = on_find
win.On.Refresh.Clicked = refresh
win.On.Next.Clicked = on_next
win.On.Prev.Clicked = on_prev
win.On.Tree.ItemDoubleClicked = on_tree
win.On.Close.Clicked = on_close
win.On.SubFinderWin.Close = on_close
try:
    win.On.Query.ReturnPressed = on_find   # Invio nel campo = Cerca
except Exception:
    pass

refresh()
win.Show()
disp.RunLoop()
win.Hide()
