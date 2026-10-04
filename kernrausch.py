#!/usr/bin/env python3
"""kernrausch.py v1.0 - Unterbrechungen und Weck-Verzoegerungen auf den Audio-Kernen (Raspberry Pi 5, Host und Target).

Benoetigt hostmess.py im selben Verzeichnis. Nur Standardbibliothek, keine Zusatzpakete: nutzt die Kernel-Tracer
osnoise/timerlat direkt ueber tracefs. Aendert keine Konfiguration dauerhaft; alle Tracer-Einstellungen werden
vorher gesichert und danach (auch bei Abbruch) wiederhergestellt.

  sudo python3 kernrausch.py run [--dauer S]   Messreihe starten (laeuft abgekoppelt weiter, ~7 min)
  sudo python3 kernrausch.py status | stop
  sudo python3 kernrausch.py aufraeumen        Tracer zuruecksetzen, falls ein Lauf hart abgebrochen wurde
  python3 kernrausch.py analyse ORDNER         Bericht neu erzeugen
  python3 kernrausch.py vergleich ORDNER_A ORDNER_B
  python3 kernrausch.py selftest

Ablauf (je Phase DAUER s, Standard 60):
  A  Musik AUS, passiv   : Interrupts, Softirqs und Thread-Aktivierungen je CPU (/proc, nichts wird gestartet)
  B  Musik AUS, osnoise  : Betriebssystem-Rauschen je CPU (Rechenzeit, die dem Kern weggenommen wird) + Quellen
  C  Musik AUS, timerlat : Weck-Verzoegerung je CPU (Timer-IRQ und Echtzeit-Thread, Periode 1 ms)
  D  Musik AN,  passiv   : wie A, waehrend der Wiedergabe
B und C laufen nur bei gestoppter Musik (die Tracer belegen die Kerne bzw. wecken sie 1000x/s mit FIFO 95).
Musikzustand = end0-Paketrate (> 200 Pak/s), gilt auf Host und Target gleich.
"""
import argparse, errno, fcntl, glob, hashlib, json, math, os, pwd, re, select, shutil, signal, subprocess, sys, threading, time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import hostmess as H
except ImportError:
    sys.exit("hostmess.py fehlt im selben Verzeichnis wie kernrausch.py")

VERSION = "1.0"
TEST = os.environ.get("KR_TEST") == "1"                       # nur Selbsttest ausserhalb eines Pi
SHM = os.environ.get("KR_SHM", "/dev/shm/kernrausch")
END0 = os.environ.get("KR_END0", "end0")
TRACER_CPUS = os.environ.get("KR_CPUS", "1-3")                # CPU0 bleibt frei fuer Steuerung/Auswertung
AUDIO_CPUS = (2, 3)
PLAY_PPS = float(os.environ.get("KR_PLAY_PPS", "200"))
SETTLE_OFF = float(os.environ.get("KR_SETTLE_OFF", "30"))
SETTLE_ON = float(os.environ.get("KR_SETTLE_ON", "60"))
WAIT_MAX = float(os.environ.get("KR_WAIT", "1800"))
OSN_PERIOD_US = 1000000                                       # Kernel-Standard: 1 s Periode, 1 s Laufzeit
TL_PERIOD_US = 1000
SAVE_KEYS = ("current_tracer", "tracing_on", "osnoise/options", "osnoise/cpus", "osnoise/period_us", "osnoise/runtime_us",
             "osnoise/stop_tracing_us", "osnoise/stop_tracing_total_us", "osnoise/timerlat_period_us",
             "events/osnoise/enable", "events/osnoise/irq_noise/enable", "events/osnoise/softirq_noise/enable",
             "events/osnoise/thread_noise/enable", "events/osnoise/nmi_noise/enable")
EVENT_KEYS = tuple(k for k in SAVE_KEYS if k.startswith("events/"))
OPEN_PIPES = set()                       # nur Test: bildet die Kernel-Sperre nach (current_tracer bei offener trace_pipe)
VORHER = os.path.join(SHM, "vorher.json")
log = H.log


# ------------------------------------------------------------------ Hilfen
def tracefs():
    for p in ([os.environ["KR_TRACEFS"]] if os.environ.get("KR_TRACEFS") else []) + ["/sys/kernel/tracing",
                                                                                     "/sys/kernel/debug/tracing"]:
        if os.path.isfile(os.path.join(p, "current_tracer")):
            return p
    return None

def cpulist(txt):
    out = set()
    for part in txt.strip().split(","):
        if "-" in part:
            a, b = part.split("-"); out.update(range(int(a), int(b) + 1))
        elif part.strip():
            out.add(int(part))
    return sorted(out)

def pct(s, q):
    if not s:
        return float("nan")
    r = (len(s) - 1) * q / 100.0; lo = int(math.floor(r)); hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (r - lo)

def fmt(x, nd=1):
    return "n/a" if x is None or (isinstance(x, float) and not math.isfinite(x)) else "%.*f" % (nd, x)

def end0_pps(dt=1.0):
    a = H.parse_netdev(H.rd("/proc/net/dev")).get(END0, {}); t0 = time.monotonic(); time.sleep(dt)
    b = H.parse_netdev(H.rd("/proc/net/dev")).get(END0, {}); d = time.monotonic() - t0
    return sum(b.get(k, 0) - a.get(k, 0) for k in ("rxp", "txp")) / d

def results_dir():
    u = os.environ.get("SUDO_USER")
    if u and u != "root":
        try:
            return os.path.join(pwd.getpwnam(u).pw_dir, "ext5v", "results")
        except KeyError:
            pass
    return H.RESULTS


# ------------------------------------------------------------------ tracefs: sichern / setzen / wiederherstellen
class Tracefs:
    def __init__(self, root):
        self.root, self.saved = root, {}

    def rd(self, key):
        return H.rd(os.path.join(self.root, key), None)

    def wr(self, key, val):
        # Kernel: current_tracer laesst sich nicht wechseln, solange trace_pipe offen ist (EBUSY) - im Test nachgebildet
        if TEST and key == "current_tracer" and OPEN_PIPES and str(val) != (self.rd(key) or "").strip():
            raise OSError(errno.EBUSY, "Device or resource busy (trace_pipe offen)")
        with open(os.path.join(self.root, key), "w") as f:
            f.write(str(val))

    def save(self):
        for k in SAVE_KEYS:
            v = self.rd(k)
            if v is not None:
                self.saved[k] = v.strip()
        return dict(self.saved)

    def restore(self):
        """Tracer zuerst auf nop (beendet Tracer-Threads), dann Einstellungen zurueck. -> Liste der Fehler."""
        errs = []
        try:
            self.wr("current_tracer", "nop")
        except OSError as e:
            errs.append("current_tracer: %s" % e)
        for k, v in self.saved.items():
            if k in ("current_tracer", "osnoise/options") or k in EVENT_KEYS or v is None:
                continue
            try:
                self.wr(k, v)
            except OSError as e:
                errs.append("%s: %s" % (k, e))
        try:
            # Ereignisse: erst alle aus, dann die einzeln gesicherten Zustaende (Sammelschalter kann "X" = gemischt sein)
            if self.saved.get("events/osnoise/enable") is not None:
                self.wr("events/osnoise/enable", "1" if self.saved["events/osnoise/enable"] == "1" else "0")
            for k in EVENT_KEYS[1:]:
                if self.saved.get(k) in ("0", "1"):
                    self.wr(k, self.saved[k])
            if "NO_OSNOISE_WORKLOAD" in (self.saved.get("osnoise/options") or "").split():
                self.wr("osnoise/options", "NO_OSNOISE_WORKLOAD")    # nur die eine von uns geaenderte Option
            if self.saved.get("current_tracer", "nop") != "nop":
                self.wr("current_tracer", self.saved["current_tracer"])
        except OSError as e:
            errs.append("abschluss: %s" % e)
        return errs

    def overruns(self, cpus):
        out = {}
        for c in cpus:
            m = re.search(r"^overrun:\s*(\d+)", self.rd("per_cpu/cpu%d/stats" % c) or "", re.M)
            out[c] = int(m.group(1)) if m else None
        return out


# ------------------------------------------------------------------ Parser
OSN_RE = re.compile(r"\[(\d+)\]\s+(?:\S+\s+)?[\d.]+:\s+(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$")
NOISE_RE = re.compile(r"\[(\d+)\].*?\b(irq_noise|softirq_noise|thread_noise|nmi_noise):\s*(.*?)\s*start\s+[\d.]+\s+duration\s+(\d+)\s+ns")
TL_RE = re.compile(r"\[(\d+)\].*?context\s+(irq|thread)\s+timer_latency\s+(\d+)\s+ns")
LOST_RE = re.compile(r"LOST\s+(\d+)\s+EVENTS")

def src_name(kind, txt):
    if kind == "nmi_noise":
        return "nmi"
    name = txt.rsplit(":", 1)[0].strip() if ":" in txt else txt.strip()
    if kind == "thread_noise":
        name = re.sub(r"/\d+$", "/N", name) if name.startswith(("ksoftirqd/", "migration/", "cpuhp/", "rcuc/")) else name
    return name or "?"

class Collector:
    """sammelt geparste Zeilen je CPU; nur Zaehler/Listen, keine Rohdaten (Speicher)."""
    def __init__(self):
        self.osn = {}; self.src = {}; self.tl = {}; self.lost = 0; self.lines = 0; self.unparsed = 0; self.sample = []

    def feed(self, ln):
        if not ln.strip() or ln.lstrip().startswith("#"):
            return
        self.lines += 1
        m = LOST_RE.search(ln)
        if m:
            self.lost += int(m.group(1)); return
        m = TL_RE.search(ln)
        if m:
            self.tl.setdefault(int(m.group(1)), {"irq": [], "thread": []})[m.group(2)].append(int(m.group(3))); return
        m = NOISE_RE.search(ln)
        if m:
            cpu, kind = int(m.group(1)), m.group(2)
            key = (kind.replace("_noise", ""), src_name(kind, m.group(3)))
            d = self.src.setdefault(cpu, {}).setdefault(key, [0, 0, 0]); ns = int(m.group(4))
            d[0] += 1; d[1] += ns; d[2] = max(d[2], ns); return
        m = OSN_RE.search(ln)
        if m:
            self.osn.setdefault(int(m.group(1)), []).append([float(x) for x in m.groups()[1:]]); return
        self.unparsed += 1
        if len(self.sample) < 20:
            self.sample.append(ln[:200])

class PipeReader(threading.Thread):
    def __init__(self, path, coll):
        super().__init__(daemon=True)
        self.path, self.coll, self.stop_ev, self.err = path, coll, threading.Event(), None
        self.abort_ev, self.opened = threading.Event(), threading.Event()

    def run(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            self.err = str(e); self.opened.set(); return
        OPEN_PIPES.add(fd); self.opened.set()
        buf = b""
        try:
            while not self.abort_ev.is_set():
                r, _, _ = select.select([fd], [], [], 0.2)
                chunk = b""
                if r:
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        chunk = b""
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for ln in lines:
                        self.coll.feed(ln.decode("utf-8", "replace"))
                elif self.stop_ev.is_set():
                    break
                else:
                    time.sleep(0.05)             # regulaere Datei (Test) liefert sofort EOF
        finally:
            if buf:
                self.coll.feed(buf.decode("utf-8", "replace"))
            os.close(fd); OPEN_PIPES.discard(fd)


# ------------------------------------------------------------------ passive Momentaufnahme
def tasks():
    out = {}
    for tdir in glob.glob("/proc/[0-9]*/task/[0-9]*"):
        st = H.parse_task_stat(H.rd(tdir + "/stat"))
        if not st:
            continue
        ss = H.rd(tdir + "/schedstat").split()
        pid = tdir.split("/")[2]
        argv0 = H.rd("/proc/%s/cmdline" % pid).split("\0")[0]          # nur Programmpfad, nicht Argumente/Ordnernamen
        out["%s/%d" % (tdir.split("/")[4], st["start"])] = {
            "comm": st["comm"], "cpu": st["cpu"], "pid": int(pid), "policy": st["policy"], "rtprio": st["rtprio"],
            "run_ns": int(ss[0]) if len(ss) >= 3 and ss[0].isdigit() else None,
            "slices": int(ss[2]) if len(ss) >= 3 and ss[2].isdigit() else None,
            "ticks": st["utime"] + st["stime"], "diretta": argv0.startswith("/opt/diretta") or "diretta" in st["comm"].lower() or "syncalsa" in st["comm"].lower()}
    return out

def snap():
    return {"t": H.now_boot(), "interrupts": H.rd("/proc/interrupts"), "softirqs": H.rd("/proc/softirqs"),
            "stat": H.rd("/proc/stat"), "tasks": tasks(), "throttled": H.sh(["vcgencmd", "get_throttled"])}

def passive(a, b, cpus=AUDIO_CPUS):
    dt = b["t"] - a["t"]
    res = {"dt": dt, "cpu": {}}
    n, ia = H.parse_table(a["interrupts"]); _, ib = H.parse_table(b["interrupts"])
    _, sa = H.parse_table(a["softirqs"]); _, sb = H.parse_table(b["softirqs"])
    stA, stB = H.parse_stat(a["stat"]), H.parse_stat(b["stat"])
    allc = sorted(int(k[3:]) for k in stB["cpus"])
    for c in allc:
        irq = {}
        for k, (cb, desc) in ib.items():
            ca = ia.get(k, ([0] * len(cb), ""))[0]
            if c < len(cb) and c < len(ca) and cb[c] - ca[c] > 0:
                nm = (desc.split()[-1] if desc.split() else "") if k.isdigit() else desc.strip()
                irq["%s %s" % (k, nm)] = (cb[c] - ca[c]) / dt
        sirq = {}
        for k, (cb, _) in sb.items():
            ca = sa.get(k, ([0] * len(cb), ""))[0]
            if c < len(cb) and c < len(ca) and cb[c] - ca[c] > 0:
                sirq[k] = (cb[c] - ca[c]) / dt
        ck = "cpu%d" % c
        busy = 100.0 * (H.cpu_busy(stB["cpus"][ck]) - H.cpu_busy(stA["cpus"][ck])) / max(
            1, H.cpu_total(stB["cpus"][ck]) - H.cpu_total(stA["cpus"][ck])) if ck in stA["cpus"] else float("nan")
        res["cpu"][c] = {"irq": irq, "softirq": sirq, "busy": busy, "threads": []}
    for key, tb in b["tasks"].items():
        ta = a["tasks"].get(key)
        if not ta or tb["cpu"] not in res["cpu"]:
            continue
        if ta["slices"] is not None and tb["slices"] is not None:
            act, run = (tb["slices"] - ta["slices"]) / dt, (tb["run_ns"] - ta["run_ns"]) / dt / 1e6   # /s, ms/s
        else:
            act, run = float("nan"), (tb["ticks"] - ta["ticks"]) / H.TICK / dt * 1000
        if (act > 0 if math.isfinite(act) else run > 0):
            res["cpu"][tb["cpu"]]["threads"].append({"comm": tb["comm"], "pid": tb["pid"], "akt_s": act, "ms_s": run,
                                                     "rt": tb["rtprio"], "policy": tb["policy"], "diretta": tb["diretta"],
                                                     "cpu_vorher": ta["cpu"]})
    for c in res["cpu"]:
        res["cpu"][c]["threads"].sort(key=lambda x: -(x["akt_s"] if math.isfinite(x["akt_s"]) else x["ms_s"]))
    ta_, tb_ = H.thr_val(a.get("throttled")), H.thr_val(b.get("throttled"))
    res["thr_ok"] = ta_ is not None and tb_ is not None and not (ta_ & 0xF) and not (tb_ & 0xF)
    return res


# ------------------------------------------------------------------ Messreihe
class Run:
    def __init__(self, cdir, dauer):
        self.dir, self.dauer, self.state, self.data = cdir, dauer, None, {"phasen": {}}
        self.tf = Tracefs(tracefs()) if tracefs() else None

    def wait(self, want):
        txt = "Musik STARTEN (Wiederholung an)" if want else "Musik STOPPEN"
        t_end = time.monotonic() + WAIT_MAX; ann = 0
        while True:
            if (end0_pps(1.0) > PLAY_PPS) == want:
                settle = SETTLE_ON if want else SETTLE_OFF
                log("Zustand %s erkannt - %.0f s Beruhigung" % ("Wiedergabe" if want else "Ruhe", settle))
                t_s = time.monotonic() + settle; ok = True
                while time.monotonic() < t_s:
                    if (end0_pps(1.0) > PLAY_PPS) != want:
                        ok = False; break
                if ok:
                    return
            if not ann or time.monotonic() - ann >= 120:
                log(">>> BITTE JETZT: %s  (Messung startet automatisch; Abbruch in %d min)" % (
                    txt, max(0, round((t_end - time.monotonic()) / 60))))
                ann = time.monotonic()
            if time.monotonic() > t_end:
                raise SystemExit("Zeitueberschreitung beim Warten auf: " + txt)

    def watch_state(self, want, dur, stop_on_change=False):
        """Musikzustand je Sekunde mitschreiben; bei stop_on_change sofort abbrechen, wenn er wechselt."""
        st = []; t_end = time.monotonic() + dur
        while time.monotonic() < t_end:
            p = end0_pps(min(1.0, max(0.05, t_end - time.monotonic())))
            st.append(p)
            if stop_on_change and (p > PLAY_PPS) != want:
                log("WARN  Musikzustand hat gewechselt - Phase wird abgebrochen"); break
        return st

    def passive_phase(self, name, want):
        log("== %s: passiv (%s), %d s" % (name, "Musik an" if want else "Musik aus", self.dauer))
        a = snap(); st = self.watch_state(want, self.dauer); b = snap()
        r = passive(a, b); r["pps"] = st; r["soll_musik"] = want
        self.data["phasen"][name] = r
        log("%s beendet (%d s)" % (name, round(r["dt"])))

    def tracer_phase(self, name, tracer):
        log("== %s: %s auf CPU %s, %d s (Musik aus)" % (name, tracer, TRACER_CPUS, self.dauer))
        if end0_pps(1.0) > PLAY_PPS:                           # Tracer nie bei laufender Musik starten
            log("Musik laeuft - Tracer %s startet erst nach dem Stoppen" % tracer); self.wait(False)
        tf = self.tf; cpus = cpulist(TRACER_CPUS); coll = Collector()
        r = {"tracer": tracer, "cpus": cpus, "soll_musik": False, "fehler": "abgebrochen"}   # wird bei Erfolg geloescht
        if tracer not in (tf.rd("available_tracers") or "").split():
            r["fehler"] = "Tracer %s im Kernel nicht vorhanden" % tracer; self.data["phasen"][name] = r
            log("WARN  " + r["fehler"]); return
        try:
            tf.wr("current_tracer", "nop")
            tf.wr("osnoise/cpus", TRACER_CPUS)
            for k in ("osnoise/stop_tracing_us", "osnoise/stop_tracing_total_us"):
                if tf.rd(k) is not None:
                    tf.wr(k, "0")
            if tracer == "osnoise":
                tf.wr("osnoise/period_us", OSN_PERIOD_US); tf.wr("osnoise/runtime_us", OSN_PERIOD_US)
                ev = tf.rd("events/osnoise/enable") is not None
                if ev:
                    for e in ("irq_noise", "softirq_noise", "thread_noise", "nmi_noise"):
                        if os.path.exists(os.path.join(tf.root, "events/osnoise/%s/enable" % e)):
                            tf.wr("events/osnoise/%s/enable" % e, "1")
                r["quellen_events"] = ev
            else:
                tf.wr("osnoise/timerlat_period_us", TL_PERIOD_US)
                if tf.rd("events/osnoise/enable") is not None:
                    tf.wr("events/osnoise/enable", "0")
            if "NO_OSNOISE_WORKLOAD" in (tf.rd("osnoise/options") or "").split():
                tf.wr("osnoise/options", "OSNOISE_WORKLOAD")      # sonst startet timerlat/osnoise keine Mess-Threads
            tf.wr("trace", "")                                  # Puffer leeren
            ov0 = tf.overruns(cpus)
            tf.wr("tracing_on", "1")
            # Reihenfolge zwingend: erst Tracer setzen, DANN trace_pipe oeffnen (Kernel: EBUSY bei offener Pipe);
            # die Zeilen bis zum Oeffnen bleiben im Ringpuffer und werden mitgelesen
            t0 = H.now_boot(); tf.wr("current_tracer", tracer)
            self.reader = rd = PipeReader(os.path.join(tf.root, "trace_pipe"), coll); rd.start(); rd.opened.wait(5)
            time.sleep(1.0)
            r["threads"] = [{"comm": t["comm"], "cpu": t["cpu"], "policy": t["policy"], "rtprio": t["rtprio"]}
                            for t in tasks().values() if t["comm"].startswith(("osnoise/", "timerlat/"))]
            st = self.watch_state(False, max(0.0, self.dauer - 1.0), stop_on_change=True)
            tf.wr("tracing_on", "0"); t1 = H.now_boot()       # Aufzeichnung stoppen, Rest lesen, Pipe schliessen
            rd.stop_ev.set(); rd.join(10)
            r.update({"dt": t1 - t0, "pps": st, "overrun": {c: (b - ov0[c]) if (b is not None and ov0[c] is not None) else None
                                                            for c, b in tf.overruns(cpus).items()},
                      "reader_err": rd.err, "zeilen": coll.lines, "unparsed": coll.unparsed, "lost": coll.lost,
                      "beispiel_unparsed": coll.sample})
            if tracer == "osnoise":
                r["osn"] = {c: v for c, v in coll.osn.items()}
                r["src"] = {c: {"%s|%s" % k: v for k, v in d.items()} for c, d in coll.src.items()}
            else:
                r["tl"] = {c: {ctx: summarize(v) for ctx, v in d.items()} for c, d in coll.tl.items()}
            if len(st) < max(1, int(self.dauer - 1.0)) - 1:
                r["fehler"] = "vorzeitig beendet: Musikzustand wechselte"
            else:
                del r["fehler"]
        finally:
            old = [signal.signal(sg, signal.SIG_IGN) for sg in (signal.SIGTERM, signal.SIGHUP)]   # Ruecksetzen nicht unterbrechen
            self.close_reader()
            errs = tf.restore()
            if errs:
                log("FEHLER beim Zuruecksetzen: %s" % errs)
            r["restore_err"] = errs
            r["nach"] = {k: (tf.rd(k) or "").strip() for k in ("current_tracer", "tracing_on")}
            self.data["phasen"][name] = r                       # auch bei Abbruch: Nachweis des Zuruecksetzens
            for sg, h in zip((signal.SIGTERM, signal.SIGHUP), old):
                signal.signal(sg, h)
        log("%s beendet: %d Zeilen, %d nicht erkannt, %d verloren" % (name, coll.lines, coll.unparsed, coll.lost))

    def close_reader(self):
        """trace_pipe muss zu sein, bevor current_tracer geaendert werden kann."""
        rd = getattr(self, "reader", None)
        if rd and rd.is_alive():
            try:
                self.tf.wr("tracing_on", "0")
            except OSError:
                pass
            rd.stop_ev.set(); rd.join(3)
            if rd.is_alive():
                rd.abort_ev.set(); rd.join(3)
        self.reader = None

    def run(self):
        self.data["vorher"] = self.tf.save() if self.tf else {}
        if self.tf:                                             # dauerhaft sichern: aufraeumen auch nach kill -9
            H.wjson(VORHER, {"tracefs": self.tf.root, "werte": self.data["vorher"]})
        self.wait(False)
        self.passive_phase("A_ruhe", False)
        if self.tf:
            self.tracer_phase("B_osnoise", "osnoise")
            self.tracer_phase("C_timerlat", "timerlat")
        else:
            log("WARN  tracefs nicht gefunden - Phasen B/C entfallen")
        log(">>> Messungen ohne Musik fertig.")
        self.wait(True)
        self.passive_phase("D_musik", True)

def summarize(v):
    s = sorted(v)
    return {"n": len(s), "p50_us": pct(s, 50) / 1000, "p99_us": pct(s, 99) / 1000, "p999_us": pct(s, 99.9) / 1000,
            "max_us": (s[-1] / 1000) if s else float("nan"), "ueber_50us": sum(1 for x in s if x > 50000)}


# ------------------------------------------------------------------ Auswertung
def preflight():
    errs = []
    if os.geteuid() != 0 and not TEST:
        errs.append("bitte mit sudo starten (Tracer brauchen root)")
    model = H.rd("/proc/device-tree/model").replace("\0", "").strip()
    if not model.startswith("Raspberry Pi 5") and not TEST:
        errs.append("kein Raspberry Pi 5")
    tf = tracefs()
    if tf:
        cur = (H.rd(os.path.join(tf, "current_tracer")) or "").strip()
        if cur != "nop":
            errs.append("Tracer '%s' ist aktiv - %s" % (cur, "Reste eines Abbruchs: sudo python3 kernrausch.py aufraeumen"
                                                        if cur in ("osnoise", "timerlat") else "anderes Werkzeug benutzt ihn"))
        av = (H.rd(os.path.join(tf, "available_tracers")) or "").split()
        miss = [t for t in ("osnoise", "timerlat") if t not in av]
        if miss:
            log("WARN  Kernel ohne %s - diese Phase(n) entfallen" % "/".join(miss))
    else:
        log("WARN  tracefs nicht gefunden - nur passive Phasen A/D")
    if END0 not in H.parse_netdev(H.rd("/proc/net/dev")) and not TEST:
        errs.append("Schnittstelle %s fehlt" % END0)
    for lk in ("/dev/shm/vschwank/lock", "/dev/shm/hostmess/lock"):
        if os.path.exists(lk):
            try:
                with open(lk, "a") as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                errs.append("andere Messung laeuft (%s)" % lk)
    return errs, {"model": model, "tracefs": tf}

def checks(d):
    out = []; ph = d["phasen"]; dauer = d["dauer"]
    for n, want in (("A_ruhe", False), ("D_musik", True)):
        r = ph.get(n)
        if not r:
            out.append(("%s vorhanden" % n, False)); continue
        ok_state = all((p > PLAY_PPS) == want for p in r["pps"]) and len(r["pps"]) > 0
        out.append(("%s: Musik %s waehrend der ganzen Phase (end0 %s Pak/s)" % (n, "an" if want else "aus",
                    "/".join(fmt(x, 0) for x in (min(r["pps"] or [0]), max(r["pps"] or [0])))), ok_state))
        out.append(("%s: Dauer %.0f s (Soll %d)" % (n, r["dt"], dauer), abs(r["dt"] - dauer) < 0.1 * dauer + 3))
        out.append(("%s: keine Unterspannung/Drosselung" % n, r.get("thr_ok", False)))
    for n in ("B_osnoise", "C_timerlat"):
        r = ph.get(n)
        if not r:
            continue
        if "fehler" in r:
            out.append(("%s: %s" % (n, r["fehler"]), False))
            if "nach" in r:
                out.append(("%s: Einstellungen trotzdem zurueckgesetzt (current_tracer=%s, Fehler %d)" % (
                    n, r["nach"].get("current_tracer"), len(r["restore_err"])), not r["restore_err"] and r["nach"].get(
                        "current_tracer") == d["vorher"].get("current_tracer", "nop")))
            continue
        out.append(("%s: Einstellungen zurueckgesetzt (current_tracer=%s, Fehler %d)" % (n, r["nach"].get("current_tracer"),
                    len(r["restore_err"])), not r["restore_err"] and r["nach"].get("current_tracer") == d["vorher"].get(
                        "current_tracer", "nop")))
        out.append(("%s: Musik blieb aus" % n, bool(r.get("pps")) and all(p <= PLAY_PPS for p in r["pps"])))
        ov = r["overrun"]; unk = [c for c, v in ov.items() if v is None]
        out.append(("%s: Lesen ohne Fehler, keine verlorenen Ereignisse (verloren %d, Overrun %s%s)" % (
                    n, r["lost"], {c: v for c, v in ov.items() if v is not None}, (", CPU %s nicht pruefbar" % unk) if unk else ""),
                    not r["reader_err"] and r["lost"] == 0 and all(v == 0 for v in ov.values() if v is not None)))
        out.append(("%s: Zeilen erkannt (%d von %d, nicht erkannt <= 1 %%)" % (n, r["zeilen"] - r["unparsed"], r["zeilen"]),
                    r["zeilen"] > 0 and r["unparsed"] <= 0.01 * r["zeilen"]))
        if n == "B_osnoise":
            for c in r["cpus"]:
                k = len(r["osn"].get(str(c), r["osn"].get(c, [])))
                exp = r["dt"] / (OSN_PERIOD_US / 1e6)
                out.append(("%s: CPU%d %d Perioden (Soll ~%.0f)" % (n, c, k, exp), k >= 0.85 * exp - 1))
        else:
            for c in r["cpus"]:
                v = r["tl"].get(str(c), r["tl"].get(c, {}))
                k = min(v.get("irq", {}).get("n", 0), v.get("thread", {}).get("n", 0))
                exp = r["dt"] * 1e6 / TL_PERIOD_US
                out.append(("%s: CPU%d %d Weckvorgaenge (Soll ~%.0f)" % (n, c, k, exp), k >= 0.9 * exp))
    return out

POL = {0: "OTHER", 1: "FIFO", 2: "RR", 3: "BATCH", 5: "IDLE", 6: "DEADLINE"}

def g(dct, c):
    return dct.get(str(c), dct.get(c))

def report(d):
    L, S = [], []
    def both(x):
        L.append(x); S.append(x)
    both("kernrausch %s  host=%s  start=%s  Dauer je Phase %d s  Tracer-CPUs %s  Audio-CPUs %s%s" % (
        d["version"], d["host"], d["start"], d["dauer"], TRACER_CPUS, ",".join(map(str, AUDIO_CPUS)),
        "  TESTMODUS" if d.get("test") else ""))
    both("cmdline: " + d.get("cmdline", "").strip())
    ph = d["phasen"]
    for n, lab in (("A_ruhe", "A  Musik AUS, passiv"), ("D_musik", "D  Musik AN, passiv")):
        r = ph.get(n)
        if not r:
            continue
        both(""); both("%s (%.0f s): je CPU Interrupts/s, Softirqs/s, Thread-Aktivierungen/s" % (lab, r["dt"]))
        for c in sorted(r["cpu"], key=int):
            x = r["cpu"][c]
            irq = sorted(x["irq"].items(), key=lambda kv: -kv[1]); sq = sorted(x["softirq"].items(), key=lambda kv: -kv[1])
            both("  CPU%s  belegt %s %%  IRQ %s/s [%s]  Softirq %s/s [%s]" % (
                c, fmt(x["busy"], 2), fmt(sum(v for _, v in irq), 0), ", ".join("%s %s" % (k.strip(), fmt(v, 0)) for k, v in irq[:5]),
                fmt(sum(v for _, v in sq), 0), ", ".join("%s %s" % (k, fmt(v, 0)) for k, v in sq[:4])))
            if int(c) in AUDIO_CPUS:
                for t in x["threads"][:8]:
                    both("        %-18s %s Akt./s %s ms/s %s%s%s" % (t["comm"][:18], fmt(t["akt_s"], 1), fmt(t["ms_s"], 2),
                         POL.get(t["policy"], t["policy"]) + (str(t["rt"]) if t["rt"] else ""),
                         "  Diretta" if t["diretta"] else "", "" if t["cpu_vorher"] == int(c) else "  (gewandert)"))
    if "A_ruhe" in ph and "D_musik" in ph:
        both(""); both("Differenz Musik - Ruhe je Audio-CPU (Interrupts/s, Softirqs/s):")
        for c in AUDIO_CPUS:
            a, b = g(ph["A_ruhe"]["cpu"], c), g(ph["D_musik"]["cpu"], c)
            if a and b:
                both("  CPU%d  IRQ %+.0f/s  Softirq %+.0f/s  belegt %+.2f %%" % (c, sum(b["irq"].values()) - sum(a["irq"].values()),
                     sum(b["softirq"].values()) - sum(a["softirq"].values()), b["busy"] - a["busy"]))
    r = ph.get("B_osnoise")
    if r and "osn" in r:
        both(""); both("B  osnoise (Musik aus): Rechenzeit, die dem Kern durch das System entzogen wird")
        both("  CPU  verfuegbar%  Rauschen us/s  max.Einzelstoerung us | Ereignisse/s: HW NMI IRQ SIRQ THREAD")
        for c in r["cpus"]:
            v = g(r["osn"], c) or []
            if not v:
                both("  CPU%d keine Daten" % c); continue
            rt = sum(x[0] for x in v); nz = sum(x[1] for x in v); sec = rt / 1e6
            both("  CPU%d  %8s  %10s  %10s | %s" % (c, fmt(100.0 * (1 - nz / rt) if rt else float("nan"), 4), fmt(nz / sec, 1),
                 fmt(max(x[3] for x in v), 0), " ".join(fmt(sum(x[i] for x in v) / sec, 1) for i in range(4, 9))))
        if r.get("src"):
            both("  Haeufigste Quellen je CPU (Typ Name: Anzahl/s, Mittel us, max us):")
            for c in r["cpus"]:
                srcd = g(r["src"], c) or {}
                sec = r["dt"]
                top = sorted(srcd.items(), key=lambda kv: -kv[1][1])[:6]
                both("   CPU%d: %s" % (c, "; ".join("%s %s: %s/s, %s/%s us" % (k.split("|")[0], k.split("|")[1], fmt(v[0] / sec, 1),
                      fmt(v[1] / v[0] / 1000, 1), fmt(v[2] / 1000, 1)) for k, v in top) or "keine"))
        if r.get("threads"):
            both("  Tracer-Threads: " + ", ".join("%s %s%s" % (t["comm"], POL.get(t["policy"], t["policy"]),
                                                              t["rtprio"] or "") for t in r["threads"]))
    r = ph.get("C_timerlat")
    if r and "tl" in r:
        both(""); both("C  timerlat (Musik aus, Periode 1 ms): Weck-Verzoegerung in us")
        both("  CPU  IRQ p50/p99/p99,9/max            | Thread p50/p99/p99,9/max         | Thread > 50 us")
        for c in r["cpus"]:
            v = g(r["tl"], c) or {}
            i, t = v.get("irq", {}), v.get("thread", {})
            both("  CPU%d  %s | %s | %s" % (c, "/".join(fmt(i.get(k), 1) for k in ("p50_us", "p99_us", "p999_us", "max_us")),
                 "/".join(fmt(t.get(k), 1) for k in ("p50_us", "p99_us", "p999_us", "max_us")), t.get("ueber_50us", "n/a")))
    ck = checks(d); nf = [x for x, ok in ck if not ok]
    L.append(""); L.append("Pruefungen: %d von %d bestanden" % (len(ck) - len(nf), len(ck)))
    L.extend("  [%s] %s" % ("PASS" if ok else "FAIL", x) for x, ok in ck)
    S.append(""); S.append("Pruefungen: %d von %d bestanden%s" % (len(ck) - len(nf), len(ck), "" if nf else " (alle)"))
    S.extend("  [FAIL] " + x for x in nf)
    for n in ("B_osnoise", "C_timerlat"):
        if ph.get(n, {}).get("beispiel_unparsed"):
            L.append("  nicht erkannte Zeilen %s (Beispiele): %s" % (n, ph[n]["beispiel_unparsed"][:5]))
    return "\n".join(L) + "\n", "\n".join(S) + "\n"

def compare(a, b):
    da, db = H.rjson(os.path.join(a, "data.json")), H.rjson(os.path.join(b, "data.json"))
    out = ["Vergleich A=%s  B=%s" % (os.path.basename(a), os.path.basename(b))]
    for n in ("A_ruhe", "D_musik"):
        for c in AUDIO_CPUS:
            x, y = g(da["phasen"].get(n, {}).get("cpu", {}), c), g(db["phasen"].get(n, {}).get("cpu", {}), c)
            if x and y:
                out.append("%-8s CPU%d  IRQ %s -> %s /s   Softirq %s -> %s /s   belegt %s -> %s %%" % (
                    n, c, fmt(sum(x["irq"].values()), 0), fmt(sum(y["irq"].values()), 0), fmt(sum(x["softirq"].values()), 0),
                    fmt(sum(y["softirq"].values()), 0), fmt(x["busy"], 2), fmt(y["busy"], 2)))
    for c in cpulist(TRACER_CPUS):
        x, y = g(da["phasen"].get("C_timerlat", {}).get("tl", {}), c), g(db["phasen"].get("C_timerlat", {}).get("tl", {}), c)
        if x and y:
            out.append("timerlat CPU%d  Thread p99 %s -> %s us, max %s -> %s us" % (c, fmt(x["thread"]["p99_us"]),
                       fmt(y["thread"]["p99_us"]), fmt(x["thread"]["max_us"]), fmt(y["thread"]["max_us"])))
        x, y = g(da["phasen"].get("B_osnoise", {}).get("osn", {}), c), g(db["phasen"].get("B_osnoise", {}).get("osn", {}), c)
        if x and y:
            nx = sum(v[1] for v in x) / (sum(v[0] for v in x) / 1e6); ny = sum(v[1] for v in y) / (sum(v[0] for v in y) / 1e6)
            out.append("osnoise  CPU%d  Rauschen %s -> %s us/s, max %s -> %s us" % (c, fmt(nx), fmt(ny), fmt(max(v[3] for v in x), 0),
                       fmt(max(v[3] for v in y), 0)))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ Prozessrahmen
def chown_tree_one(p):
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid and os.geteuid() == 0:
        try:
            os.chown(p, int(uid), int(gid))
        except OSError:
            pass

def chown_tree(p):
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid and os.geteuid() == 0:
        for root, dirs, files in os.walk(p):
            for x in [root] + [os.path.join(root, f) for f in files]:
                try:
                    os.chown(x, int(uid), int(gid))
                except OSError:
                    pass

def child_main(cdir, dauer, lockfd):
    lock = os.fdopen(lockfd, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("== ABBRUCH: eine andere Messung laeuft bereits"); return 1
    try:
        os.sched_setaffinity(0, {0})
    except OSError as e:
        log("WARN  CPU-Bindung nicht moeglich: %s" % e)
    def on_term(*_):
        raise SystemExit("durch stop/Verbindungsabbruch beendet")
    signal.signal(signal.SIGTERM, on_term); signal.signal(signal.SIGHUP, on_term)
    errs, pre = preflight()
    if errs:
        for e in errs:
            log("FEHLER " + e)
        log("== ABBRUCH: Vorpruefung nicht bestanden - nichts gemessen"); return 1
    R = Run(cdir, dauer)
    R.data.update({"version": VERSION, "host": os.uname().nodename, "kernel": os.uname().release, "dauer": dauer,
                   "start": time.strftime("%Y-%m-%dT%H:%M:%S"), "cmdline": H.rd("/proc/cmdline"), "pre": pre, "test": TEST,
                   "isolated": H.rd("/sys/devices/system/cpu/isolated").strip(),
                   "nohz_full": H.rd("/sys/devices/system/cpu/nohz_full").strip()})
    rc, done = 0, False
    try:
        R.run(); done = True
    except SystemExit as e:
        log("== ABBRUCH: %s" % e); rc = 1
    except Exception:
        log("== ABBRUCH: unerwarteter Fehler\n" + traceback.format_exc()); rc = 2
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if R.tf:
            R.close_reader()
            cur = (R.tf.rd("current_tracer") or "").strip()
            if cur in ("osnoise", "timerlat"):          # Sicherheitsnetz, falls eine Phase mitten drin abbrach
                log("Sicherheitsnetz: Tracer %s wird zurueckgesetzt: %s" % (cur, R.tf.restore() or "ok"))
            if (R.tf.rd("current_tracer") or "").strip() == R.data.get("vorher", {}).get("current_tracer", "nop"):
                try:
                    os.unlink(VORHER)
                except OSError:
                    pass
    if not R.data["phasen"]:
        log("Keine Phase abgeschlossen - nichts gespeichert"); return rc or 1
    base = results_dir(); made = []
    p_ = base
    while p_ and not os.path.isdir(p_):
        made.append(p_); p_ = os.path.dirname(p_)
    dst = os.path.join(base, "kernrausch_" + os.path.basename(cdir) + ("" if done else "_ABGEBROCHEN"))
    os.makedirs(dst, exist_ok=True)
    for m_ in made:                                     # neu angelegte Elternordner gehoeren dem Benutzer, nicht root
        chown_tree_one(m_)
    H.wjson(os.path.join(dst, "data.json"), R.data)
    try:
        full, short = report(R.data)
        for fn, txt in (("report.txt", full), ("kurz.txt", short)):
            with open(os.path.join(dst, fn), "w") as f:
                f.write(txt)
        print(short, flush=True)
        log("== %s  Ergebnis: %s" % ("FERTIG" if done else "TEILERGEBNIS GESPEICHERT", dst))
        log("Kurzbericht: cat %s/kurz.txt" % dst)
    except Exception:
        log("== FEHLER bei der Auswertung\n" + traceback.format_exc()); rc = rc or 2
    shutil.copy2(os.path.join(cdir, "run.log"), os.path.join(dst, "run.log"))
    sums = []
    for fn in sorted(os.listdir(dst)):
        if fn != "SHA256SUMS":
            with open(os.path.join(dst, fn), "rb") as fh:
                sums.append("%s  %s\n" % (hashlib.sha256(fh.read()).hexdigest(), fn))
    with open(os.path.join(dst, "SHA256SUMS"), "w") as f:
        f.write("".join(sums))
    chown_tree(dst)
    return rc

def aufraeumen():
    tf = tracefs()
    if not tf:
        print("tracefs nicht gefunden"); return 1
    T = Tracefs(tf); cur = (T.rd("current_tracer") or "").strip()
    curp = os.path.join(SHM, "current.json")
    if os.path.isfile(curp) and H.alive(H.rjson(curp)["pid"]):
        print("Messung laeuft noch - zuerst: sudo python3 kernrausch.py stop"); return 1
    if os.path.isfile(VORHER):
        v = H.rjson(VORHER); T.saved = v["werte"]
        if T.saved.get("current_tracer") in ("osnoise", "timerlat"):
            T.saved["current_tracer"] = "nop"
        e = T.restore()
        print("Alle gesicherten Tracer-Einstellungen wiederhergestellt%s (current_tracer jetzt: %s)" % (
            (" - FEHLER: %s" % e) if e else "", (T.rd("current_tracer") or "").strip()))
        if not e:
            os.unlink(VORHER)
        return 1 if e else 0
    if cur in ("osnoise", "timerlat"):
        T.wr("current_tracer", "nop")
        if T.rd("events/osnoise/enable") is not None:
            T.wr("events/osnoise/enable", "0")
        print("Tracer %s zurueckgesetzt (jetzt: %s); keine Sicherung gefunden, uebrige Werte unveraendert" % (
            cur, (T.rd("current_tracer") or "").strip())); return 0
    print("nichts zu tun (current_tracer=%s)" % cur); return 0


# ------------------------------------------------------------------ Selbsttest
def selftest():
    ok = True
    def chk(c, msg):
        nonlocal ok
        print("[%s] %s" % ("PASS" if c else "FAIL", msg)); ok &= bool(c)
    C = Collector()
    for ln in ("           <...>-859     [000] ....    81.637220: 1000000        190  99.98100            9      18    0   1007     18       1",
               "     osnoise/8-961     [008] d.h.  5789.857532: irq_noise: local_timer:236 start 5789.857529929 duration 1845 ns",
               "     osnoise/2-100     [002] ....   100.000001: thread_noise:  syncAlsa:1234 start 100.000000 duration 5000 ns",
               "     osnoise/2-100     [002] ..s.   100.000002: softirq_noise:    TIMER:1 start 100.000000 duration 700 ns",
               "     osnoise/3-101     [003] d.h.   100.000003: nmi_noise: start 100.000000 duration 300 ns",
               "        <idle>-0       [000] d.h1    54.029328: #1     context    irq timer_latency       932 ns",
               "         <...>-867     [000] ....    54.029339: #1     context thread timer_latency     11700 ns",
               "CPU:2 [LOST 7 EVENTS]", "# tracer: osnoise", "", "voellig unbekannte zeile"):
        C.feed(ln)
    chk(C.osn.get(0) == [[1000000, 190, 99.981, 9, 18, 0, 1007, 18, 1]], "osnoise-Periodenzeile: %s" % C.osn.get(0))
    chk(C.src[8][("irq", "local_timer")][:3] == [1, 1845, 1845] and C.src[2][("thread", "syncAlsa")][0] == 1
        and C.src[2][("softirq", "TIMER")][0] == 1 and C.src[3][("nmi", "nmi")][0] == 1, "Stoerquellen-Zeilen: %s" % C.src)
    chk(C.tl[0]["irq"] == [932] and C.tl[0]["thread"] == [11700], "timerlat-Zeilen: %s" % C.tl)
    chk(C.lost == 7 and C.unparsed == 1 and C.lines == 9, "verlorene/unerkannte Zeilen: lost %d unparsed %d lines %d" % (
        C.lost, C.unparsed, C.lines))
    sm = summarize([1000 * i for i in range(1, 101)])
    chk(abs(sm["p50_us"] - 50.5) < 1e-9 and sm["max_us"] == 100 and sm["ueber_50us"] == 50, "Perzentile/Zaehlung: %s" % sm)
    chk(cpulist("1-3") == [1, 2, 3] and cpulist("0,2-3") == [0, 2, 3], "CPU-Listen")
    import tempfile
    d = tempfile.mkdtemp()
    for k, v in (("current_tracer", "nop"), ("tracing_on", "0"), ("osnoise/cpus", "0-3"), ("osnoise/period_us", "1000000"),
                 ("osnoise/runtime_us", "1000000"), ("events/osnoise/enable", "0"), ("available_tracers", "osnoise timerlat nop")):
        os.makedirs(os.path.dirname(os.path.join(d, k)), exist_ok=True)
        with open(os.path.join(d, k), "w") as f:
            f.write(v + "\n")
    T = Tracefs(d); sv = T.save()
    T.wr("current_tracer", "osnoise"); T.wr("osnoise/cpus", "1-3"); T.wr("tracing_on", "1"); T.wr("events/osnoise/enable", "1")
    e = T.restore()
    chk(not e and T.rd("current_tracer") == "nop" and T.rd("osnoise/cpus") == "0-3" and T.rd("tracing_on") == "0"
        and T.rd("events/osnoise/enable") == "0" and "osnoise/timerlat_period_us" not in sv,
        "tracefs sichern/wiederherstellen (fehlende Dateien uebersprungen)")
    p = os.path.join(d, "pipe")
    with open(p, "w") as f:
        f.write("         <...>-867     [002] ....    54.029339: #1     context thread timer_latency     2000 ns\n" * 3)
    C2 = Collector(); rd = PipeReader(p, C2); rd.start(); time.sleep(0.5); rd.stop_ev.set(); rd.join(5)
    chk(C2.tl.get(2, {}).get("thread") == [2000] * 3 and not rd.err, "Lesethread (Datei/EOF, Stopp)")
    a = {"t": 0.0, "interrupts": "           CPU0       CPU1       CPU2       CPU3\n104:   0   0   10   0  GICv2 104 Level  end0\n",
         "softirqs": "                    CPU0       CPU1       CPU2       CPU3\n          TIMER:   1  1  5  1\n",
         "stat": "cpu0 1 0 1 10 0 0 0 0\ncpu1 1 0 1 10 0 0 0 0\ncpu2 1 0 1 10 0 0 0 0\ncpu3 1 0 1 10 0 0 0 0\n",
         "tasks": {"9/1": {"comm": "syncAlsa", "cpu": 2, "pid": 9, "policy": 1, "rtprio": 70, "run_ns": 0, "slices": 0, "ticks": 0,
                           "diretta": True}}, "throttled": "throttled=0x0"}
    b = json.loads(json.dumps(a)); b["t"] = 10.0
    b["interrupts"] = a["interrupts"].replace("   10   0  GIC", "   1010   0  GIC")
    b["softirqs"] = a["softirqs"].replace("1  1  5  1", "1  1  105  1")
    b["stat"] = a["stat"].replace("cpu2 1 0 1 10", "cpu2 6 0 6 10")
    b["tasks"]["9/1"].update({"run_ns": 50000000, "slices": 5000})
    r = passive(a, b)
    c2 = r["cpu"][2]
    chk(abs(sum(c2["irq"].values()) - 100) < 1e-9 and abs(c2["softirq"]["TIMER"] - 10) < 1e-9
        and abs(c2["threads"][0]["akt_s"] - 500) < 1e-9 and abs(c2["threads"][0]["ms_s"] - 5) < 1e-9 and r["thr_ok"],
        "passive Raten (IRQ 100/s, Softirq 10/s, 500 Akt./s, 5 ms/s): %s" % {k: c2[k] for k in ("irq", "softirq")})
    shutil.rmtree(d, ignore_errors=True)
    print("SELBSTTEST %s" % ("BESTANDEN" if ok else "NICHT BESTANDEN"))
    return 0 if ok else 1


# ------------------------------------------------------------------ Kommandozeile
def main():
    ap = argparse.ArgumentParser(prog="kernrausch.py")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run"); r.add_argument("--dauer", type=int, default=60)
    for c in ("status", "stop", "aufraeumen", "selftest"):
        sp.add_parser(c)
    a = sp.add_parser("analyse"); a.add_argument("ordner")
    v = sp.add_parser("vergleich"); v.add_argument("a"); v.add_argument("b")
    k = sp.add_parser("_child"); k.add_argument("dir"); k.add_argument("dauer", type=int); k.add_argument("lockfd", type=int)
    args = ap.parse_args()
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "analyse":
        print(report(H.rjson(os.path.join(os.path.abspath(args.ordner), "data.json")))[0], end=""); return 0
    if args.cmd == "vergleich":
        print(compare(os.path.abspath(args.a), os.path.abspath(args.b)), end=""); return 0
    if args.cmd == "_child":
        return child_main(args.dir, args.dauer, args.lockfd)
    if args.cmd == "aufraeumen":
        return aufraeumen()
    curp = os.path.join(SHM, "current.json")
    cur = H.rjson(curp) if os.path.isfile(curp) else None
    if args.cmd == "status":
        if not cur:
            print("keine Messung bekannt"); return 0
        print("Messung %s - %s" % (cur["dir"], "LAEUFT" if H.alive(cur["pid"]) else "beendet"))
        print("".join(H.rd(os.path.join(cur["dir"], "run.log")).splitlines(True)[-15:]), end=""); return 0
    if args.cmd == "stop":
        if cur and H.alive(cur["pid"]):
            os.kill(cur["pid"], signal.SIGTERM); print("Abbruch gesendet (Tracer werden zurueckgesetzt)."); return 0
        print("keine laufende Messung"); return 0
    if args.dauer < 20:
        print("--dauer mindestens 20 s"); return 2
    if os.geteuid() != 0 and not TEST:
        print("Bitte mit sudo starten:  sudo python3 kernrausch.py run"); return 2
    os.makedirs(SHM, exist_ok=True)
    lock = open(os.path.join(SHM, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Es laeuft bereits eine Messung (sudo python3 kernrausch.py status)"); return 1
    cdir = os.path.join(SHM, time.strftime("%Y%m%d_%H%M%S")); os.makedirs(cdir)
    logp = os.path.join(cdir, "run.log")
    with open(logp, "w") as lf:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_child", cdir, str(args.dauer), str(lock.fileno())],
                             stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True,
                             pass_fds=(lock.fileno(),))
    lock.close()
    H.wjson(curp, {"dir": cdir, "pid": p.pid})
    print("kernrausch gestartet: 4 Phasen je %d s + Beruhigung, gesamt ca. %d min. Laeuft weiter, auch wenn die Verbindung abreisst."
          % (args.dauer, round((4 * args.dauer + SETTLE_OFF + SETTLE_ON + 30) / 60)))
    print("Fortschritt:  sudo python3 kernrausch.py status      Abbruch:  sudo python3 kernrausch.py stop\n")
    pos = 0
    try:
        while True:
            with open(logp) as f:
                f.seek(pos); chunk = f.read(); pos = f.tell()
            if chunk:
                sys.stdout.write(chunk); sys.stdout.flush()
            if p.poll() is not None and not chunk:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n(Anzeige beendet - Messung laeuft weiter. Fortschritt: sudo python3 kernrausch.py status)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
