#!/usr/bin/env python3
"""hostmess.py v1.0 - Messkampagne 5-V-Schiene + Systemaktivitaet (Raspberry Pi 5, AudioLinux/Diretta).

Nur Python-Standardbibliothek. Laeuft als normaler Benutzer (kein sudo). Aendert keine Konfiguration.

  python3 hostmess.py run [--idle S] [--play S] [--step S]   Kampagne starten (laeuft abgekoppelt weiter)
  python3 hostmess.py status                                  Fortschritt anzeigen
  python3 hostmess.py stop                                    laufende Kampagne abbrechen
  python3 hostmess.py analyse ORDNER                          Bericht neu erzeugen
  python3 hostmess.py selftest                                Selbsttest der Auswertung

Phasen (Reihenfolge fest): idle (Musik aus) -> play (Musik an) -> step (Musik aus, Laststufen auf CPU0).
Phasenwechsel erkennt das Programm selbst am ALSA-Status; es gibt nur Anweisungen im Log aus.
Waehrend der Messung wird nur nach /dev/shm geschrieben (keine SD-Karten-Zugriffe); Kopie ins
Ergebnisverzeichnis, Pruefsummen und Bericht erst nach der letzten Messung.
"""
import argparse, datetime, fcntl, glob, hashlib, json, math, os, re, shutil, signal
import subprocess, sys, threading, time, traceback

VERSION = "1.0"
HOME = os.path.expanduser("~")
EXT5V = os.environ.get("HOSTMESS_EXT5V", os.path.join(HOME, "ext5v"))
SHM = os.environ.get("HOSTMESS_SHM", "/dev/shm/hostmess")
RESULTS = os.environ.get("HOSTMESS_RESULTS", os.path.join(EXT5V, "results"))
ALSA_GLOB = os.environ.get("HOSTMESS_ALSA_GLOB", "/proc/asound/card*/pcm*p/sub*/status")
SETTLE = float(os.environ.get("HOSTMESS_SETTLE", "30"))      # Beruhigungszeit vor jeder Phase [s]
WAIT_MAX = float(os.environ.get("HOSTMESS_WAIT", "900"))     # max. Wartezeit auf Musik an/aus [s]
TEST = os.environ.get("HOSTMESS_TEST") == "1"                # nur fuer Selbsttests ausserhalb eines Pi
HZ = 50                                                      # wie alle bisherigen ext5v-Messungen
LOGGER_CPU = "1"                                             # wie bisher
CTL_CPU = 0                                                  # Steuerung, Sampler, Laststufen
STEP_ON = STEP_OFF = 10.0                                    # Laststufe 10 s an / 10 s aus
STEP_LEAD = 10.0                                             # Ruhe vor erster Stufe
TEMP_STOP = 75.0                                             # Laststufen-Abbruch ab dieser SoC-Temperatur [C]
TICK = os.sysconf("SC_CLK_TCK")
DISK_RE = re.compile(r"^(mmcblk\d+|nvme\d+n\d+|sd[a-z]+)$")
LOGF = None


# ------------------------------------------------------------------ Hilfen
def now_boot():
    return time.clock_gettime(time.CLOCK_BOOTTIME)

def log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)

def rd(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return default

def sh(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:  # Werkzeug fehlt/haengt: als Text festhalten, nicht abbrechen
        return "FEHLER: %s" % e

def wjson(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)

def rjson(path):
    with open(path) as f:
        return json.load(f)

def temp_c():
    v = rd("/sys/class/thermal/thermal_zone0/temp").strip()
    return int(v) / 1000.0 if v.isdigit() else float("nan")


# ------------------------------------------------------------------ ALSA-Status
def alsa_state():
    """(laeuft, [Details]) ueber alle Wiedergabe-Substreams."""
    running, info = False, []
    for st in sorted(glob.glob(ALSA_GLOB)):
        txt = rd(st)
        m = re.search(r"state:\s*(\S+)", txt)
        state = m.group(1) if m else ("closed" if "closed" in txt else "?")
        hw = rd(os.path.join(os.path.dirname(st), "hw_params"))
        rate = re.search(r"rate:\s*(\d+)", hw)
        fmt = re.search(r"format:\s*(\S+)", hw)
        info.append({"sub": st, "state": state, "rate": int(rate.group(1)) if rate else None,
                     "format": fmt.group(1) if fmt else None})
        running |= (state == "RUNNING")
    return running, info


# ------------------------------------------------------------------ Parser (/proc)
def parse_stat(txt):
    cpus, d = {}, {}
    for ln in txt.splitlines():
        p = ln.split()
        if not p:
            continue
        if re.fullmatch(r"cpu\d+", p[0]):
            v = [int(x) for x in p[1:]] + [0] * 10
            cpus[p[0]] = {"user": v[0], "nice": v[1], "system": v[2], "idle": v[3], "iowait": v[4],
                          "irq": v[5], "softirq": v[6], "steal": v[7]}
        elif p[0] in ("ctxt", "processes", "procs_running", "procs_blocked"):
            d[p[0]] = int(p[1])
        elif p[0] == "intr":
            d["intr"] = int(p[1])
    d["cpus"] = cpus
    return d

def cpu_total(c):
    return sum(c[k] for k in ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"))

def cpu_busy(c):
    return cpu_total(c) - c["idle"] - c["iowait"]

def parse_table(txt):
    """/proc/interrupts und /proc/softirqs -> (Anzahl CPUs, {Name: ([Zaehler je CPU], Beschreibung)})."""
    lines = txt.splitlines()
    if not lines:
        return 0, {}
    ncpu = len(lines[0].split())
    out = {}
    for ln in lines[1:]:
        if ":" not in ln:
            continue
        lab, rest = ln.split(":", 1)
        toks = rest.split()
        cnt = []
        for t in toks[:ncpu]:
            if not t.isdigit():
                break
            cnt.append(int(t))
        if not cnt:
            continue
        cnt += [0] * (ncpu - len(cnt))
        desc = " ".join(toks[ncpu:]) if len(toks) > ncpu else ""
        out[lab.strip()] = (cnt, desc)
    return ncpu, out

def parse_diskstats(txt):
    out = {}
    for ln in txt.splitlines():
        p = ln.split()
        if len(p) >= 10 and DISK_RE.match(p[2]):
            out[p[2]] = {"r": int(p[3]), "rs": int(p[5]), "w": int(p[7]), "ws": int(p[9])}
    return out

def parse_netdev(txt):
    out = {}
    for ln in txt.splitlines()[2:]:
        if ":" not in ln:
            continue
        name, rest = ln.split(":", 1)
        v = [int(x) for x in rest.split()]
        if len(v) >= 16:
            out[name.strip()] = {"rxb": v[0], "rxp": v[1], "txb": v[8], "txp": v[9]}
    return out

def parse_task_stat(txt):
    """Felder aus /proc/PID/task/TID/stat; comm kann Leerzeichen/Klammern enthalten."""
    r = txt.rfind(")")
    if r < 0:
        return None
    comm = txt[txt.find("(") + 1:r]
    f = txt[r + 2:].split()          # f[0] = Feld 3 (state)
    if len(f) < 39:
        return None
    g = lambda n: f[n - 3]
    return {"comm": comm, "state": g(3), "ppid": int(g(4)), "utime": int(g(14)), "stime": int(g(15)),
            "nice": int(g(19)), "start": int(g(22)), "cpu": int(g(39)), "rtprio": int(g(40)),
            "policy": int(g(41))}

def read_tasks():
    tasks = {}
    for tdir in glob.glob("/proc/[0-9]*/task/[0-9]*"):
        st = parse_task_stat(rd(tdir + "/stat"))
        if not st:
            continue
        stt = rd(tdir + "/status")
        m = lambda k: re.search(r"^%s:\s*(.+)$" % k, stt, re.M)
        a, v, n = m("Cpus_allowed_list"), m("voluntary_ctxt_switches"), m("nonvoluntary_ctxt_switches")
        pid, tid = tdir.split("/")[2], tdir.split("/")[4]
        st.update({"pid": int(pid), "tid": int(tid), "allowed": a.group(1).strip() if a else "?",
                   "vcs": int(v.group(1)) if v else 0, "nvcs": int(n.group(1)) if n else 0})
        tasks["%s/%s" % (tid, st["start"])] = st   # Schluessel mit Startzeit: TID-Wiederverwendung erkennbar
    return tasks


# ------------------------------------------------------------------ Schnappschuss
def snapshot(path, light=False):
    s = {"t_boot": now_boot(), "wall": datetime.datetime.now().isoformat(timespec="seconds"),
         "stat": rd("/proc/stat"), "interrupts": rd("/proc/interrupts"), "softirqs": rd("/proc/softirqs"),
         "diskstats": rd("/proc/diskstats"), "netdev": rd("/proc/net/dev"), "tasks": read_tasks(),
         "temp_c": temp_c(), "alsa": alsa_state()[1]}
    s["t_boot_end"] = now_boot()
    if not light:   # Werkzeugaufrufe nur ausserhalb der Messfenster
        s["throttled"] = sh(["vcgencmd", "get_throttled"])
        s["ext5v_now"] = sh(["vcgencmd", "pmic_read_adc", "EXT5V_V"])
        s["timers"] = sh(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"])
    wjson(path, s)
    return s


# ------------------------------------------------------------------ Sampler (1 Hz, nur /proc-Lesen)
class Sampler(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.stop_ev, self.rows, self.err = path, threading.Event(), [], None

    def run(self):
        try:
            nxt = time.monotonic()
            while not self.stop_ev.is_set():
                t = now_boot()
                st = parse_stat(rd("/proc/stat"))
                dk = parse_diskstats(rd("/proc/diskstats"))
                nd = parse_netdev(rd("/proc/net/dev"))
                run, ai = alsa_state()
                rate = next((i["rate"] for i in ai if i["state"] == "RUNNING"), None)
                self.rows.append({"t": t, "cpus": {k: [cpu_busy(v), cpu_total(v)] for k, v in st["cpus"].items()},
                                  "ctxt": st.get("ctxt", 0), "intr": st.get("intr", 0),
                                  "procs": st.get("processes", 0), "run": st.get("procs_running", 0),
                                  "dw": sum(v["w"] for v in dk.values()),
                                  "np": {k: v["rxp"] + v["txp"] for k, v in nd.items()},
                                  "alsa": 1 if run else 0, "rate": rate, "temp": temp_c()})
                nxt += 1.0
                self.stop_ev.wait(max(0.0, nxt - time.monotonic()))
        except Exception:
            self.err = traceback.format_exc()
        finally:
            wjson(self.path, {"rows": self.rows, "err": self.err})


# ------------------------------------------------------------------ Laststufen (CPU0)
class Stepper(threading.Thread):
    def __init__(self, t0, dur):
        super().__init__(daemon=True)
        self.t0, self.dur, self.stop_ev, self.edges, self.proc, self.note = t0, dur, threading.Event(), [], None, []
        self.lock = threading.Lock()

    def _off(self):
        with self.lock:
            p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()
            self.edges.append(["off", now_boot()])

    def run(self):
        t_end = self.t0 + self.dur - STEP_OFF - 5.0
        t = self.t0 + STEP_LEAD
        try:
            while not self.stop_ev.is_set() and t + STEP_ON <= t_end:
                if self.stop_ev.wait(max(0.0, t - now_boot())):
                    break
                if temp_c() >= TEMP_STOP:
                    self.note.append("Temperatur %.1f C >= %.0f C: Laststufen beendet" % (temp_c(), TEMP_STOP))
                    break
                with self.lock:
                    if self.stop_ev.is_set():
                        break
                    self.proc = subprocess.Popen(["taskset", "-c", str(CTL_CPU), "sha256sum", "/dev/zero"],
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.edges.append(["on", now_boot()])
                self.stop_ev.wait(max(0.0, t + STEP_ON - now_boot()))
                self._off()
                t += STEP_ON + STEP_OFF
        finally:
            self._off()


# ------------------------------------------------------------------ Logger-Lauf (ext5v_run.sh)
def is_logger(d):
    """ext5v_log-Prozess? Prueft comm und die ersten beiden argv-Eintraege (Interpreter-Aufruf)."""
    if rd(d + "/comm").strip() == "ext5v_log":
        return True
    argv = rd(d + "/cmdline").split("\0")[:2]
    return any(os.path.basename(a) == "ext5v_log" for a in argv if a)

def find_logger_start(pgid, timeout=10.0):
    """Startzeit (CLOCK_BOOTTIME, 1/CLK_TCK-Raster = Fork-Zeitpunkt) des ext5v_log-Prozesses in der Prozessgruppe."""
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        for d in glob.glob("/proc/[0-9]*"):
            if not is_logger(d):
                continue
            pid = int(d.split("/")[2])
            try:
                if os.getpgid(pid) != pgid:
                    continue
            except OSError:
                continue
            st = parse_task_stat(rd(d + "/stat"))
            if st:
                return pid, st["start"] / TICK
        time.sleep(0.05)
    return None, None

def other_logger_running():
    return [d for d in glob.glob("/proc/[0-9]*") if is_logger(d)]


def kill_group(p):
    """Prozessgruppe beenden: SIGTERM, nach 10 s SIGKILL."""
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            break
        try:
            p.wait(wait); break
        except subprocess.TimeoutExpired:
            continue


# ------------------------------------------------------------------ Kampagne
class Campaign:
    def __init__(self, cdir, args):
        self.dir, self.args = cdir, args
        self.raw = os.path.join(cdir, "raw"); os.makedirs(self.raw, exist_ok=True)
        self.children = []
        self.stepper = None

    def preflight(self):
        log("== Vorpruefung")
        errs = []
        model = rd("/proc/device-tree/model").replace("\0", "").strip()
        if not model.startswith("Raspberry Pi 5") and not TEST:
            errs.append("kein Raspberry Pi 5: '%s'" % model)
        for f in ("ext5v_run.sh", "ext5v_log"):
            if not os.access(os.path.join(EXT5V, f), os.X_OK):
                errs.append("fehlt/nicht ausfuehrbar: %s" % os.path.join(EXT5V, f))
        if not os.path.isfile(os.path.join(EXT5V, "ext5v_stats.py")):
            errs.append("fehlt: %s" % os.path.join(EXT5V, "ext5v_stats.py"))
        for c in ("vcgencmd", "taskset", "sha256sum", "systemctl"):
            if not shutil.which(c):
                errs.append("Befehl fehlt: %s" % c)
        v = sh(["vcgencmd", "pmic_read_adc", "EXT5V_V"])
        if not re.search(r"=\s*[\d.]+\s*V", v):
            errs.append("EXT5V nicht lesbar: %s" % v)
        if not glob.glob(ALSA_GLOB):
            errs.append("kein ALSA-Wiedergabegeraet gefunden (%s)" % ALSA_GLOB)
        if not rd("/proc/1/stat"):
            errs.append("/proc fremder Prozesse nicht lesbar (hidepid?) - Thread-Auswertung waere unvollstaendig")
        if other_logger_running():
            errs.append("ext5v_log laeuft bereits - erst beenden")
        if os.cpu_count() != 4 and not TEST:
            errs.append("erwartet 4 CPUs, gefunden %s" % os.cpu_count())
        free = shutil.disk_usage(os.path.dirname(SHM) or "/").free
        if free < 200 * 2 ** 20:
            errs.append("zu wenig Platz in %s" % SHM)
        if errs:
            for e in errs:
                log("FEHLER " + e)
            raise SystemExit("Vorpruefung nicht bestanden - nichts gemessen")
        log("OK    %s, EXT5V %s, ALSA %s" % (model or "?", v.split("=")[-1], ",".join(i["state"] for i in alsa_state()[1])))

    def meta(self):
        m = {"version": VERSION, "host": os.uname().nodename, "kernel": os.uname().release,
             "start": datetime.datetime.now().isoformat(timespec="seconds"), "hz": HZ, "logger_cpu": LOGGER_CPU,
             "ctl_cpu": CTL_CPU, "settle_s": SETTLE, "durations": {"idle": self.args.idle, "play": self.args.play,
             "step": self.args.step}, "step": {"on": STEP_ON, "off": STEP_OFF, "lead": STEP_LEAD, "cpu": CTL_CPU},
             "clk_tck": TICK, "pid": os.getpid(), "test_mode": TEST,
             "env": {k: v for k, v in os.environ.items() if k.startswith("HOSTMESS_")},
             "files": {}}
        for name, path in (("cmdline", "/proc/cmdline"), ("config_txt", "/boot/config.txt"),
                           ("diretta_setting", "/opt/diretta-alsa/setting.inf"),
                           ("isolated", "/sys/devices/system/cpu/isolated"),
                           ("nohz_full", "/sys/devices/system/cpu/nohz_full"),
                           ("journald_conf", "/etc/systemd/journald.conf")):
            m["files"][name] = rd(path, None)
        m["services"] = sh(["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain"])
        m["irq_affinity"] = {os.path.basename(d): rd(d + "/smp_affinity_list").strip()
                             for d in glob.glob("/proc/irq/[0-9]*")}
        m["governors"] = {os.path.basename(c): rd(c + "/cpufreq/scaling_governor").strip()
                          for c in glob.glob("/sys/devices/system/cpu/cpu[0-9]*")}
        m["self_hash"] = hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()
        m["ext5v_hashes"] = {f: hashlib.sha256(open(os.path.join(EXT5V, f), "rb").read()).hexdigest()
                             for f in ("ext5v_run.sh", "ext5v_log", "ext5v_stats.py")
                             if os.path.isfile(os.path.join(EXT5V, f))}
        wjson(os.path.join(self.dir, "campaign.json"), m)

    def wait_state(self, want_play):
        txt = "Musik STARTEN (wie gewohnt, 48 oder 96 kHz)" if want_play else "Musik STOPPEN"
        t_end = time.monotonic() + WAIT_MAX
        announced = False
        while True:
            if alsa_state()[0] == want_play:
                # Zustand muss die ganze Beruhigungszeit stabil bleiben
                log("Zustand erkannt (%s) - %.0f s Beruhigung" % ("Wiedergabe" if want_play else "keine Wiedergabe", SETTLE))
                t_s = time.monotonic() + SETTLE
                stable = True
                while time.monotonic() < t_s:
                    if alsa_state()[0] != want_play:
                        stable = False; break
                    time.sleep(0.5)
                if stable:
                    return
                log("Zustand hat gewechselt - warte erneut")
            if not announced:
                log(">>> BITTE JETZT: %s  (Messung startet automatisch)" % txt)
                announced = True
            if time.monotonic() > t_end:
                raise SystemExit("Zeitueberschreitung beim Warten auf: %s" % txt)
            time.sleep(1.0)

    def phase(self, name, dur, want_play, step=False):
        log("== Phase %s (%d s)" % (name, dur))
        self.wait_state(want_play)
        pdir = os.path.join(self.dir, name); os.makedirs(pdir, exist_ok=True)
        snapshot(os.path.join(pdir, "snap_a.json"))
        smp = Sampler(os.path.join(pdir, "sampler.json")); smp.start()
        env = dict(os.environ, EXT5V_CPU=LOGGER_CPU, EXT5V_OUT=self.raw)
        tag = "hm-" + name
        outp = os.path.join(pdir, "run_out.txt")
        with open(outp, "w") as of:
            p = subprocess.Popen([os.path.join(EXT5V, "ext5v_run.sh"), str(HZ), str(dur), tag], env=env,
                                 stdin=subprocess.DEVNULL, stdout=of, stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append(p)
        lpid, lstart = find_logger_start(p.pid)   # p.pid = Prozessgruppe (start_new_session)
        info = {"name": name, "dur": dur, "want_play": want_play, "logger_pid": lpid, "logger_start": lstart,
                "alsa_start": alsa_state()[1]}
        if lstart is None:
            log("FEHLER ext5v_log-Start nicht erkannt")
        elif step:
            self.stepper = Stepper(lstart, dur); self.stepper.start()
        log("Messung laeuft ... (Ende ca. %s)" % time.strftime("%H:%M:%S", time.localtime(time.time() + dur)))
        if lstart is not None:   # Aktivitaet nur innerhalb des Logger-Fensters zaehlen (ohne Start/Ende des Werkzeugs)
            for tgt, fn in ((lstart + 1.0, "in_a"), (lstart + dur - 1.0, "in_b")):
                time.sleep(max(0.0, tgt - now_boot()))
                if p.poll() is None:
                    snapshot(os.path.join(pdir, "snap_%s.json" % fn), light=True)
        try:
            p.wait(timeout=max(1.0, (lstart or now_boot()) + dur + 120 - now_boot()))
        except subprocess.TimeoutExpired:
            info["timeout"] = True
            kill_group(p)
        self.children.remove(p)
        out = rd(outp)
        if self.stepper:
            self.stepper.stop_ev.set(); self.stepper.join(15)
            info["step_edges"], info["step_notes"] = self.stepper.edges, self.stepper.note
            self.stepper = None
        smp.stop_ev.set(); smp.join(10)
        info.update({"rc": p.returncode, "run_out": out.strip()[-2000:], "alsa_end": alsa_state()[1]})
        runs = [l for l in out.splitlines() if l.startswith(self.raw)]
        info["run_dir"] = os.path.relpath(runs[-1], self.dir) if runs else None
        snapshot(os.path.join(pdir, "snap_b.json"))
        wjson(os.path.join(pdir, "phase.json"), info)
        log("Phase %s beendet: logger_rc=%s%s" % (name, p.returncode, "" if smp.err is None else " (Sampler-Fehler!)"))

    def cleanup(self):
        if self.stepper:
            self.stepper.stop_ev.set(); self.stepper.join(10)
            self.stepper._off()
        for p in list(self.children):
            kill_group(p)

    def finalize(self, aborted=False):
        """Kopie nach RESULTS, Bericht, Kurzbericht. Pruefsummen erst ganz am Ende (write_sums)."""
        stamp = os.path.basename(self.dir)
        dst = os.path.join(RESULTS, "hostmess_" + stamp + ("_ABGEBROCHEN" if aborted else ""))
        shutil.copytree(self.dir, dst, ignore=shutil.ignore_patterns("run.log"))
        full, short = analyse(dst, write_files=True)
        with open(os.path.join(dst, "report.txt"), "w") as f:
            f.write(full)
        with open(os.path.join(dst, "kurz.txt"), "w") as f:
            f.write(short)
        return dst, short

    def write_sums(self, dst):
        shutil.copy2(os.path.join(self.dir, "run.log"), os.path.join(dst, "run.log"))
        sums = []
        for root, _, files in os.walk(dst):
            for fn in files:
                if fn != "SHA256SUMS":
                    fp = os.path.join(root, fn)
                    with open(fp, "rb") as fh:
                        sums.append((os.path.relpath(fp, dst), hashlib.sha256(fh.read()).hexdigest()))
        with open(os.path.join(dst, "SHA256SUMS"), "w") as f:
            f.write("".join("%s  %s\n" % (h, n) for n, h in sorted(sums)))


def campaign_main(cdir, args):
    lock = os.fdopen(args.lockfd, "w")   # vom Elternprozess gesperrt geerbt (keine Luecke zwischen Pruefen und Sperren)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("== ABBRUCH: eine andere Kampagne laeuft bereits"); return 1
    try:
        os.sched_setaffinity(0, {CTL_CPU})
    except OSError as e:
        log("WARN  CPU-Bindung an CPU%d nicht moeglich: %s" % (CTL_CPU, e))
    c = Campaign(cdir, args)
    def on_term(*_):
        raise SystemExit("durch stop beendet")
    signal.signal(signal.SIGTERM, on_term)
    rc, done = 0, False
    try:
        c.preflight(); c.meta()
        if args.idle: c.phase("idle", args.idle, False)
        if args.play: c.phase("play", args.play, True)
        if args.step: c.phase("step", args.step, False, step=True)
        done = True
    except SystemExit as e:
        log("== ABBRUCH: %s" % e); rc = 1
    except Exception:
        log("== ABBRUCH: unerwarteter Fehler\n" + traceback.format_exc()); rc = 2
    finally:
        c.cleanup()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    if not any(os.path.isfile(os.path.join(cdir, n, "phase.json")) for n in ("idle", "play", "step")):
        log("Keine Phase abgeschlossen - nichts gespeichert (Rohdaten: %s)" % cdir)
        return rc or 1
    try:
        log("== Messungen %s - Musik darf wieder laufen. Auswertung ..." % ("fertig" if done else "TEILWEISE"))
        dst, short = c.finalize(aborted=not done)
        print(short, flush=True)
        log("== %s  Ergebnis: %s" % ("FERTIG" if done else "TEILERGEBNIS GESPEICHERT", dst))
        log("Kurzbericht: cat %s/kurz.txt    Vollbericht: cat %s/report.txt" % (dst, dst))
        c.write_sums(dst)
    except Exception:
        log("== FEHLER bei der Auswertung (Rohdaten bleiben in %s)\n%s" % (cdir, traceback.format_exc())); rc = rc or 2
    return rc


# ------------------------------------------------------------------ Auswertung
def mean(a): return math.fsum(a) / len(a) if a else float("nan")
def sdev(a):
    if len(a) < 2: return float("nan")
    m = mean(a); return math.sqrt(math.fsum((x - m) ** 2 for x in a) / (len(a) - 1))
def median(a):
    s = sorted(a); n = len(s)
    return float("nan") if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)
def pearson(x, y):
    if len(x) < 3: return float("nan")
    mx, my = mean(x), mean(y)
    sxy = math.fsum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = math.fsum((a - mx) ** 2 for a in x); syy = math.fsum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else float("nan")

def load_voltage(run_dir, t0):
    """(t_abs, V) aus ext5v.csv; t_abs = Logger-Start (CLOCK_BOOTTIME) + t_s."""
    out = []
    p = os.path.join(run_dir, "ext5v.csv")
    if not os.path.isfile(p):
        return out
    with open(p) as f:
        for ln in f:
            if ln.startswith("#") or ln.startswith("t_s"):
                continue
            a = ln.split(",")
            try:
                t, v = float(a[0]), float(a[1])
            except (ValueError, IndexError):
                continue
            if math.isfinite(t) and math.isfinite(v):
                out.append((t0 + t, v))
    return out

def window(v, a, b):
    return [x for t, x in v if a <= t < b]

def f1(x, nd=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else ("%.*f" % (nd, x))

def analyse_activity(sa, sb, dt, lines, mine=()):
    res = {"busy": {}, "disk": {}, "net": {}}
    a, b = parse_stat(sa["stat"]), parse_stat(sb["stat"])
    lines.append("  CPU-Auslastung [%%] (Zeitraum %.1f s):" % dt)
    lines.append("    cpu   busy   user    sys    irq   sirq iowait")
    for k in sorted(a["cpus"]):
        ca, cb = a["cpus"][k], b["cpus"].get(k)
        if not cb: continue
        tot = cpu_total(cb) - cpu_total(ca)
        if tot <= 0: continue
        pc = lambda f: 100.0 * (cb[f] - ca[f]) / tot
        res["busy"][k] = 100.0 * (cpu_busy(cb) - cpu_busy(ca)) / tot
        lines.append("    %-4s %6.2f %6.2f %6.2f %6.2f %6.2f %6.2f" % (k, 100.0 * (cpu_busy(cb) - cpu_busy(ca)) / tot,
                     pc("user") + pc("nice"), pc("system"), pc("irq"), pc("softirq"), pc("iowait")))
    res["ctxt"] = (b.get("ctxt", 0) - a.get("ctxt", 0)) / dt; res["intr"] = (b.get("intr", 0) - a.get("intr", 0)) / dt
    res["forks"] = b.get("processes", 0) - a.get("processes", 0)
    lines.append("  Kontextwechsel %.0f/s  Interrupts %.0f/s  neue Prozesse %d (%.3f/s)" % (
        (b.get("ctxt", 0) - a.get("ctxt", 0)) / dt, (b.get("intr", 0) - a.get("intr", 0)) / dt,
        b.get("processes", 0) - a.get("processes", 0), (b.get("processes", 0) - a.get("processes", 0)) / dt))
    n, ia = parse_table(sa["interrupts"]); _, ib = parse_table(sb["interrupts"])
    rows = []
    for k, (cb, desc) in ib.items():
        ca = ia.get(k, ([0] * n, ""))[0]
        d = [y - x for x, y in zip(ca, cb)]
        if sum(d) > 0: rows.append((sum(d), k, d, desc))
    rows.sort(reverse=True)
    lines.append("  Interrupts je Quelle [/s] (Top 10)   " + "  ".join("CPU%d" % i for i in range(n)))
    for s, k, d, desc in rows[:10]:
        lines.append("    %-6s %-26s %s" % (k, desc[-26:], " ".join("%7.1f" % (x / dt) for x in d)))
    n, qa = parse_table(sa["softirqs"]); _, qb = parse_table(sb["softirqs"])
    lines.append("  Softirqs [/s]          " + "  ".join("CPU%d" % i for i in range(n)))
    for k, (cb, _) in qb.items():
        d = [y - x for x, y in zip(qa.get(k, ([0] * n, ""))[0], cb)]
        if sum(d) > 0:
            lines.append("    %-10s %s" % (k, " ".join("%7.1f" % (x / dt) for x in d)))
    ta, tb = sa["tasks"], sb["tasks"]
    trs = []
    for key, t in tb.items():
        o = ta.get(key)
        if not o: continue
        cpu_ms = (t["utime"] + t["stime"] - o["utime"] - o["stime"]) * 1000.0 / TICK
        wk = (t["vcs"] - o["vcs"]) / dt; inv = (t["nvcs"] - o["nvcs"]) / dt
        if cpu_ms > 0 or wk > 0:
            trs.append((cpu_ms, wk, inv, t))
    new = len(set(tb) - set(ta)); gone = len(set(ta) - set(tb))
    lines.append("  Threads: %d aktiv, %d neu, %d beendet im Zeitraum" % (len(trs), new, gone))
    lines.append("    CPU[ms/s] Weck/s Verdr/s cpu erlaubt  pid/tid      Name")
    own = lambda t: t["pid"] in mine or t["comm"] in ("ext5v_log", "sha256sum")
    hdr = lambda r: "    %8.2f %7.1f %7.1f %3d %-8s %6d/%-6d %s%s" % (r[0] / dt, r[1], r[2], r[3]["cpu"],
                    r[3]["allowed"][:8], r[3]["pid"], r[3]["tid"], r[3]["comm"], " [Messung]" if own(r[3]) else "")
    for r in sorted(trs, key=lambda r: -r[0])[:10]:
        lines.append(hdr(r))
    lines.append("    -- meiste Aufwachvorgaenge (Top 8):")
    for r in sorted(trs, key=lambda r: -r[1])[:8]:
        lines.append(hdr(r))
    res["wake"] = [r for r in sorted(trs, key=lambda r: -r[1]) if not own(r[3])][:5]
    iso = [r for r in trs if r[3]["cpu"] in (2, 3)]
    res["iso"] = sorted(iso, key=lambda r: -r[1])[:5]
    lines.append("    -- zuletzt auf CPU2/3 gelaufen (%d Threads, Top 10 nach Aufwachvorgaengen):" % len(iso))
    for r in sorted(iso, key=lambda r: -r[1])[:10]:
        lines.append(hdr(r))
    da, db = parse_diskstats(sa["diskstats"]), parse_diskstats(sb["diskstats"])
    for k in sorted(db):
        if k in da:
            w = db[k]["w"] - da[k]["w"]; r = db[k]["r"] - da[k]["r"]
            res["disk"][k] = w / dt
            lines.append("  Datentraeger %-10s Schreibvorgaenge %d (%.3f/s, %d KiB)  Lesevorgaenge %d" % (
                k, w, w / dt, (db[k]["ws"] - da[k]["ws"]) // 2, r))
    na, nb = parse_netdev(sa["netdev"]), parse_netdev(sb["netdev"])
    for k in sorted(nb):
        if k in na and k != "lo":
            rp = nb[k]["rxp"] - na[k]["rxp"]; tp = nb[k]["txp"] - na[k]["txp"]
            if rp or tp:
                res["net"][k] = (rp + tp) / dt
                lines.append("  Netz %-6s rx %8.1f Pak/s %9.0f B/s   tx %8.1f Pak/s %9.0f B/s" % (
                    k, rp / dt, (nb[k]["rxb"] - na[k]["rxb"]) / dt, tp / dt, (nb[k]["txb"] - na[k]["txb"]) / dt))
    if sa.get("timers") and sb.get("timers"):
        fired = [l for l in sb["timers"].splitlines() if l not in sa["timers"].splitlines()]
        lines.append("  Timer mit geaenderter Zeile (ausgeloest/neu geplant): %d" % len(fired))
        for l in fired[:6]:
            lines.append("    " + " ".join(l.split()[-2:]))
    return res

def analyse_correlation(rows, volt, lines, excl=()):
    bins, skipped = [], 0
    for r0, r1 in zip(rows, rows[1:]):
        if any(r0["t"] < e1 and r1["t"] > e0 for e0, e1 in excl):
            skipped += 1; continue   # Sekunde mit eigenem Task-Scan (Werkzeuglast) nicht verwenden
        v = window(volt, r0["t"], r1["t"])
        if len(v) < 10: continue
        dt = r1["t"] - r0["t"]
        busy = {}
        for k in r1["cpus"]:
            if k in r0["cpus"]:
                tot = r1["cpus"][k][1] - r0["cpus"][k][1]
                busy[k] = 100.0 * (r1["cpus"][k][0] - r0["cpus"][k][0]) / tot if tot > 0 else 0.0
        bins.append({"vm": mean(v) * 1000, "vmin": min(v) * 1000, "busy": busy,
                     "ctxt": (r1["ctxt"] - r0["ctxt"]) / dt, "intr": (r1["intr"] - r0["intr"]) / dt,
                     "dw": (r1["dw"] - r0["dw"]) / dt, "alsa": r1["alsa"]})
    lines.append("  Sekundenwerte: %d, %d ausgeschlossen (Korrelation = Zusammenhang, kein Ursachennachweis)" % (len(bins), skipped))
    if len(bins) < 30:
        lines.append("    zu wenige Sekundenwerte fuer Korrelation"); return []
    vm = [b["vm"] for b in bins]; vmin = [b["vmin"] for b in bins]
    lines.append("    Groesse              r(V_mittel) r(V_min)  Mittel   Max")
    series = [("busy " + k, [b["busy"].get(k, 0.0) for b in bins]) for k in sorted(bins[0]["busy"])]
    series += [("Kontextwechsel/s", [b["ctxt"] for b in bins]), ("Interrupts/s", [b["intr"] for b in bins]),
               ("Schreibvorg./s", [b["dw"] for b in bins])]
    out = []
    for name, s in series:
        r1_, r2_ = pearson(s, vm), pearson(s, vmin)
        out.append((name, r1_, r2_))
        lines.append("    %-20s %10s %8s %7.1f %7.1f" % (name, f1(r1_, 2), f1(r2_, 2), mean(s), max(s)))
    w = [b for b in bins if b["dw"] > 0]; nw = [b for b in bins if b["dw"] == 0]
    if w and nw:
        lines.append("    Sekunden mit SD-Schreiben: %d, V_min %.2f mV vs. ohne %.2f mV (Differenz %.2f mV)" % (
            len(w), mean([b["vmin"] for b in w]), mean([b["vmin"] for b in nw]),
            mean([b["vmin"] for b in w]) - mean([b["vmin"] for b in nw])))
    return out

def analyse_steps(info, volt, rows, lines):
    edges = info.get("step_edges") or []
    ons = [t for k, t in edges if k == "on"]; offs = [t for k, t in edges if k == "off"]
    cyc = []
    for i, ton in enumerate(ons):
        toff = next((t for t in offs if t > ton), None)
        if toff is None: continue
        nxt = ons[i + 1] if i + 1 < len(ons) else toff + STEP_OFF
        prev_off = window(volt, ton - STEP_OFF + 2.0, ton)
        on_s = window(volt, ton + 2.0, toff); off_s = window(volt, toff + 2.0, min(nxt, toff + STEP_OFF))
        on_t = window(volt, ton, ton + 1.0); off_t = window(volt, toff, toff + 1.0)
        if min(len(on_s), len(off_s), len(on_t), len(off_t), len(prev_off)) < 10: continue
        m_on, m_off, m_prev = mean(on_s), mean(off_s), mean(prev_off)
        dv = (m_prev + m_off) / 2 - m_on
        # Flankenverzug: erstes 5-Werte-Mittel (100 ms) nach ton-0.5 s unter halber Absenkung
        seg = [(t, x) for t, x in volt if ton - 0.5 <= t < ton + 2.0]
        lag = None
        for k in range(len(seg) - 4):
            if mean([x for _, x in seg[k:k + 5]]) < m_prev - dv / 2:
                lag = seg[k + 2][0] - ton; break
        cyc.append({"dv": dv, "under": min(on_t) - m_on, "over": max(off_t) - m_off, "lag": lag})
    lines.append("  Laststufen: %d gestartet, %d auswertbar (CPU%d, je %.0f s an/aus, Uebergaenge 2 s ausgeblendet)" % (
        len(ons), len(cyc), CTL_CPU, STEP_ON))
    for n in info.get("step_notes") or []:
        lines.append("    HINWEIS " + n)
    res = {"n_on": len(ons), "n_cyc": len(cyc), "on_b": float("nan"), "off_b": float("nan")}
    if not cyc: return res
    f = lambda k: [c[k] * 1000 for c in cyc]
    for k, label in (("dv", "Absenkung unter Last  "), ("under", "Unterschwinger (1 s)  "), ("over", "Ueberschwinger (1 s)  ")):
        s = f(k)
        res[k] = median(s)
        lines.append("    %s Median %7.2f mV  min %7.2f  max %7.2f" % (label, median(s), min(s), max(s)))
    lags = [c["lag"] for c in cyc if c["lag"] is not None]
    if lags and res["dv"] >= 3 * 1.34:
        res["lag"] = median(lags)
        lines.append("    Flankenverzug Last->Spannung Median %.0f ms (n=%d; Pruefung der Zeitausrichtung, erwartet 0..+200 ms)" % (
            1000 * median(lags), len(lags)))
    else:
        lines.append("    Flankenverzug nicht bestimmbar (Absenkung < 3 LSB)")
    # Nachweis, dass die Last wirklich anlag: CPU0-Auslastung in an- vs. aus-Sekunden
    on_b, off_b = [], []
    key = "cpu%d" % CTL_CPU
    for r0, r1 in zip(rows, rows[1:]):
        if key not in r1["cpus"]: continue
        tot = r1["cpus"][key][1] - r0["cpus"][key][1]
        if tot <= 0: continue
        b = 100.0 * (r1["cpus"][key][0] - r0["cpus"][key][0]) / tot
        mid = (r0["t"] + r1["t"]) / 2
        state = any(t_on + 1 <= mid < (next((t for t in offs if t > t_on), t_on) - 1) for t_on in ons)
        quiet = any(t_off + 1 <= mid < t_off + STEP_OFF - 1 for t_off in offs)
        (on_b if state else off_b if quiet else []).append(b)
    res["on_b"], res["off_b"] = mean(on_b), mean(off_b)
    lines.append("    Kontrolle CPU%d-Auslastung: unter Last %s %%, ohne Last %s %%" % (CTL_CPU, f1(mean(on_b)), f1(mean(off_b))))
    return res

def thr_val(txt):
    m = re.search(r"0x[0-9a-fA-F]+", txt or "")
    return int(m.group(0), 16) if m else None

def analyse(cdir, write_files=False):
    """-> (Vollbericht, Kurzbericht). Schreibt nur mit write_files=True (ext5v_stats.txt)."""
    L, S = [], []
    try:
        m = rjson(os.path.join(cdir, "campaign.json"))
    except OSError:
        t = "keine Kampagne in %s\n" % cdir
        return t, t
    head = "hostmess %s  host=%s  kernel=%s  start=%s%s" % (m["version"], m["host"], m["kernel"], m["start"],
            "  TESTMODUS" if m.get("test_mode") else "")
    L.append(head); S.append(head)
    L.append("Logger %d Hz auf CPU%s; Steuerung, Sampler, Laststufen auf CPU%d. Zeitbasis der Spannungswerte:"
             " Fork-Zeit von ext5v_log (10-ms-Raster) + t_s; Pruefung ueber Flankenverzug in Phase step." % (
             m["hz"], m["logger_cpu"], m["ctl_cpu"]))
    checks, runs = [], []
    for name in ("idle", "play", "step"):
        pdir = os.path.join(cdir, name)
        if not os.path.isfile(os.path.join(pdir, "phase.json")):
            continue
        info = rjson(os.path.join(pdir, "phase.json"))
        sa, sb = rjson(os.path.join(pdir, "snap_a.json")), rjson(os.path.join(pdir, "snap_b.json"))
        smp = rjson(os.path.join(pdir, "sampler.json")); rows = smp["rows"]
        rdir = os.path.join(cdir, info["run_dir"]) if info.get("run_dir") else None
        if rdir: runs.append(rdir)
        volt = load_voltage(rdir, info["logger_start"]) if rdir and info.get("logger_start") is not None else []
        L.append(""); L.append("== Phase %s  (Soll %d s, Messordner %s)" % (name, info["dur"], info.get("run_dir")))
        vs = [v for _, v in volt]
        vline = "N=%d mean=%.5f V sd=%.2f mV min=%.5f max=%.5f" % (len(vs), mean(vs), sdev(vs) * 1000, min(vs), max(vs)) if vs else "keine Spannungsdaten"
        L.append("  Spannung (eigene Rechnung, Gegenprobe zu ext5v_stats): " + vline)
        checks.append(("%s: ext5v_log-Start erkannt" % name, info.get("logger_start") is not None))
        al = [r["alsa"] for r in rows]
        frac = 100.0 * sum(al) / len(al) if al else float("nan")
        want = 100.0 if info["want_play"] else 0.0
        checks.append(("%s: Wiedergabe %s waehrend Messung (ist %s %%)" % (name, "an" if want else "aus", f1(frac)),
                       bool(al) and abs(frac - want) < 1e-9))
        rates = sorted({r["rate"] for r in rows if r.get("rate")} |
                       {i["rate"] for i in info.get("alsa_start", []) + info.get("alsa_end", []) if i["state"] == "RUNNING" and i["rate"]})
        rtxt = ", ".join("%d Hz" % r for r in rates) or "unbekannt"
        if info["want_play"]:
            L.append("  ALSA-Rate: " + rtxt)
            checks.append(("%s: Abtastrate konstant (%s)" % (name, rtxt), len(rates) == 1))
        checks.append(("%s: logger_rc=0" % name, info.get("rc") == 0 and not info.get("timeout")))
        gaps = [b["t"] - a["t"] for a, b in zip(rows, rows[1:])]
        checks.append(("%s: Sampler lueckenlos (max. Abstand %s s)" % (name, f1(max(gaps) if gaps else float("nan"), 2)),
                       bool(gaps) and max(gaps) < 1.5 and smp.get("err") is None))
        checks.append(("%s: Spannungsdaten vollstaendig (N=%d, Soll %d)" % (name, len(vs), info["dur"] * HZ),
                       len(vs) >= 0.98 * info["dur"] * HZ))
        ta, tb = thr_val(sa.get("throttled")), thr_val(sb.get("throttled"))
        ok = ta is not None and tb is not None and not (ta & 0xF) and not (tb & 0xF) and not (tb & ~ta & 0xF0000)
        checks.append(("%s: keine Unterspannung/Drosselung waehrend Phase (vorher %s, nachher %s)" % (
            name, hex(ta) if ta is not None else "?", hex(tb) if tb is not None else "?"), ok))
        temps = [r["temp"] for r in rows if not math.isnan(r["temp"])]
        L.append("  SoC-Temperatur %s .. %s C" % (f1(min(temps) if temps else float("nan")), f1(max(temps) if temps else float("nan"))))
        ia, ib = os.path.join(pdir, "snap_in_a.json"), os.path.join(pdir, "snap_in_b.json")
        excl = []
        if os.path.isfile(ia) and os.path.isfile(ib):
            xa, xb, src = rjson(ia), rjson(ib), "im Messfenster"
            excl = [(x["t_boot"], x.get("t_boot_end", x["t_boot"]) + 0.05) for x in (xa, xb)]
        else:
            xa, xb, src = sa, sb, "inkl. Werkzeug-Start/Ende"
        dt = xb["t_boot"] - xa["t_boot"]
        L.append("  Aktivitaet %s:" % src)
        act = analyse_activity(xa, xb, dt, L, mine={m.get("pid"), info.get("logger_pid")})
        cor = analyse_correlation(rows, volt, L, excl)
        # Kurzbericht
        S.append(""); S.append("[%s] V: %s" % (name, vline))
        S.append("  CPU-busy %%: %s | Kontextw. %.0f/s  IRQ %.0f/s  neue Prozesse %d (%s)" % (
            " ".join("%s=%.2f" % (k[3:], v) for k, v in sorted(act["busy"].items())), act["ctxt"], act["intr"],
            act["forks"], src))
        S.append("  Schreiben/s: %s | Netz Pak/s: %s%s" % (
            " ".join("%s=%.3f" % kv for kv in sorted(act["disk"].items())) or "-",
            " ".join("%s=%.1f" % kv for kv in sorted(act["net"].items())) or "-",
            ("  | Rate " + rtxt) if info["want_play"] else ""))
        S.append("  Weckt am haeufigsten: " + "; ".join("%s %.1f/s@cpu%d" % (r[3]["comm"], r[1], r[3]["cpu"]) for r in act["wake"]))
        S.append("  Auf CPU2/3: " + ("; ".join("%s %.1f/s@cpu%d" % (r[3]["comm"], r[1], r[3]["cpu"]) for r in act["iso"]) or "-"))
        strong = [(n, a_, b_) for n, a_, b_ in cor if (not math.isnan(a_) and abs(a_) >= 0.3) or (not math.isnan(b_) and abs(b_) >= 0.3)]
        if strong:
            S.append("  Korrelation |r|>=0,3: " + "; ".join("%s r=%s/%s" % (n, f1(a_, 2), f1(b_, 2)) for n, a_, b_ in strong))
        if name == "step":
            st = analyse_steps(info, volt, rows, L)
            okst = st["n_on"] > 0 and st["n_cyc"] >= 0.8 * st["n_on"] and st["on_b"] > 80 and st["off_b"] < 20
            checks.append(("step: Laststufen wirksam und auswertbar (%d/%d, CPU0 %s %% / %s %%)" % (
                st["n_cyc"], st["n_on"], f1(st["on_b"]), f1(st["off_b"])), okst))
            if "dv" in st:
                S.append("  Laststufe CPU0: Absenkung %.2f mV, Unterschwinger %.2f mV, Ueberschwinger %.2f mV, Verzug %s ms" % (
                    st["dv"], st["under"], st["over"], f1(1000 * st["lag"], 0) if st.get("lag") is not None else "n/a"))
    L.append(""); L.append("== Pruefungen")
    for txt, ok in checks:
        L.append("  [%s] %s" % ("PASS" if ok else "FAIL", txt))
    nf = [t for t, ok in checks if not ok]
    S.append(""); S.append("Pruefungen: %d von %d bestanden%s" % (len(checks) - len(nf), len(checks), "" if nf else " (alle)"))
    S.extend("  [FAIL] " + t for t in nf)
    st_ = os.path.join(EXT5V, "ext5v_stats.py")
    if runs and os.path.isfile(st_):
        out = sh([sys.executable, st_] + runs, timeout=300)
        if write_files:
            with open(os.path.join(cdir, "ext5v_stats.txt"), "w") as f:
                f.write(out + "\n")
        L.append(""); L.append("== ext5v_stats.py" + (" (vollstaendig in ext5v_stats.txt)" if write_files else ""))
        fails = [l for l in out.splitlines() if "[FAIL]" in l]
        L.extend("  " + l for l in fails)
        tail = out.split("== Vergleich")
        if len(tail) > 1:
            L.append("== Vergleich" + tail[-1].rstrip()); S.append("== Vergleich" + tail[-1].rstrip())
    return "\n".join(L) + "\n", "\n".join(S) + "\n"


# ------------------------------------------------------------------ Selbsttest
def selftest():
    ok = True
    def chk(c, msg):
        nonlocal ok
        print("[%s] %s" % ("PASS" if c else "FAIL", msg)); ok &= bool(c)
    st = parse_task_stat("123 (a b) c)) S 1 1 1 0 -1 4194560 10 0 0 0 7 3 0 0 20 0 1 0 555 " + "0 " * 16 + "2 0 0")
    chk(st and st["comm"] == "a b) c)" and st["utime"] == 7 and st["stime"] == 3 and st["cpu"] == 2 and st["start"] == 555,
        "parse_task_stat mit Klammern/Leerzeichen im Namen")
    n, t = parse_table("           CPU0       CPU1       CPU2       CPU3\n 104:          0          0    2741682          0 rp1_irq_chip   6 Level     end0\nIPI0:  5 6 7 8   Rescheduling interrupts\nErr:          0\n")
    chk(n == 4 and t["104"][0] == [0, 0, 2741682, 0] and t["104"][1].endswith("end0") and t["IPI0"][0] == [5, 6, 7, 8]
        and t["Err"][0] == [0, 0, 0, 0], "parse_table /proc/interrupts")
    s = parse_stat("cpu  1 2 3 4 5 6 7 8 0 0\ncpu0 10 0 5 80 5 0 0 0 0 0\nctxt 99\nprocesses 7\nintr 1234 1 2\n")
    chk(s["cpus"]["cpu0"]["idle"] == 80 and cpu_busy(s["cpus"]["cpu0"]) == 15 and s["ctxt"] == 99 and s["intr"] == 1234,
        "parse_stat")
    chk(parse_diskstats(" 179 0 mmcblk0 10 0 20 0 30 0 40 0 0 0 0\n 179 1 mmcblk0p1 1 0 2 0 3 0 4 0 0 0 0\n") ==
        {"mmcblk0": {"r": 10, "rs": 20, "w": 30, "ws": 40}}, "parse_diskstats (nur ganze Geraete)")
    nd = parse_netdev("h1\nh2\n  end0: 100 2 0 0 0 0 0 0 300 4 0 0 0 0 0 0\n")
    chk(nd["end0"] == {"rxb": 100, "rxp": 2, "txb": 300, "txp": 4}, "parse_netdev")
    # synthetische Laststufe: 10 mV Absenkung, 5 mV Unterschwinger
    t0, volt, edges = 1000.0, [], []
    for i in range(int(120 * HZ)):
        t = t0 + i / HZ
        cyc = (t - t0 - STEP_LEAD) % (STEP_ON + STEP_OFF)
        on = t - t0 >= STEP_LEAD and t - t0 < STEP_LEAD + 5 * (STEP_ON + STEP_OFF) and cyc < STEP_ON
        v = 5.000 - (0.010 if on else 0.0) - (0.005 if on and cyc < 0.1 else 0.0)
        volt.append((t, v))
    for k in range(5):
        edges += [["on", t0 + STEP_LEAD + k * 20], ["off", t0 + STEP_LEAD + k * 20 + STEP_ON]]
    L = []
    res = analyse_steps({"step_edges": edges}, volt, [], L)
    txt = "\n".join(L)
    chk(res.get("lag") is not None and abs(res["lag"]) < 0.05, "Flankenverzug synthetisch ~0 ms (ist %s)" % res.get("lag"))
    chk(thr_val("throttled=0x50000") == 0x50000 and thr_val("x") is None, "throttled-Wert lesen")
    chk("5 auswertbar" in txt and re.search(r"Absenkung unter Last\s+Median\s+10\.00 mV", txt) and
        re.search(r"Unterschwinger \(1 s\)\s+Median\s+-5\.00 mV", txt), "Laststufen-Auswertung (10 mV / -5 mV)")
    chk(abs(pearson([1, 2, 3, 4], [2, 4, 6, 8]) - 1) < 1e-12 and abs(pearson([1, 2, 3], [3, 2, 1]) + 1) < 1e-12, "pearson")
    print("SELBSTTEST %s" % ("BESTANDEN" if ok else "NICHT BESTANDEN"))
    return 0 if ok else 1


# ------------------------------------------------------------------ Kommandozeile
def current():
    p = os.path.join(SHM, "current.json")
    return rjson(p) if os.path.isfile(p) else None

def alive(pid):
    """lebt der Prozess (Zombie zaehlt als beendet)?"""
    st = rd("/proc/%d/stat" % pid)
    if not st:
        return False
    t = parse_task_stat(st)
    return bool(t) and t["state"] not in ("Z", "X")

def follow(logp, proc):
    pos = 0
    try:
        while True:
            with open(logp) as f:
                f.seek(pos); chunk = f.read(); pos = f.tell()
            if chunk:
                sys.stdout.write(chunk); sys.stdout.flush()
            if proc.poll() is not None and not chunk:
                return
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n(Anzeige beendet - die Messung laeuft weiter. Fortschritt: python3 hostmess.py status)")

def main():
    ap = argparse.ArgumentParser(prog="hostmess.py")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--idle", type=int, default=600); r.add_argument("--play", type=int, default=600)
    r.add_argument("--step", type=int, default=300)
    sp.add_parser("status"); sp.add_parser("stop"); sp.add_parser("selftest")
    a = sp.add_parser("analyse"); a.add_argument("ordner")
    k = sp.add_parser("_child"); k.add_argument("dir"); k.add_argument("idle", type=int)
    k.add_argument("play", type=int); k.add_argument("step", type=int); k.add_argument("lockfd", type=int)
    args = ap.parse_args()

    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "analyse":   # schreibt nichts in den Ordner (Pruefsummen bleiben gueltig)
        print(analyse(os.path.abspath(args.ordner))[0], end=""); return 0
    if args.cmd == "_child":
        return campaign_main(args.dir, args)
    cur = current()
    if args.cmd == "status":
        if not cur:
            print("keine Kampagne bekannt"); return 0
        print("Kampagne %s - %s" % (cur["dir"], "LAEUFT" if alive(cur["pid"]) else "beendet"))
        print("".join(rd(os.path.join(cur["dir"], "run.log")).splitlines(True)[-15:]), end=""); return 0
    if args.cmd == "stop":
        if cur and alive(cur["pid"]):
            os.kill(cur["pid"], signal.SIGTERM); print("Abbruch gesendet."); return 0
        print("keine laufende Kampagne"); return 0
    # run
    if not (args.idle or args.play or args.step):
        print("mindestens eine Phase > 0 angeben"); return 2
    for d in (args.idle, args.play, args.step):
        if d and d < 60:
            print("Dauer je Phase mindestens 60 s"); return 2
    if args.step and args.step < STEP_LEAD + STEP_ON + STEP_OFF + 5 + 20:
        print("step zu kurz"); return 2
    os.makedirs(SHM, exist_ok=True)
    lock = open(os.path.join(SHM, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Es laeuft bereits eine Kampagne (python3 hostmess.py status)"); return 1
    try:
        os.sched_setaffinity(0, {CTL_CPU})   # auch Anzeige und Python-Start des Kindes auf CPU0
    except OSError:
        pass
    cdir = os.path.join(SHM, time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(cdir)
    logp = os.path.join(cdir, "run.log")
    with open(logp, "w") as lf:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_child", cdir, str(args.idle),
                              str(args.play), str(args.step), str(lock.fileno())], stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock.fileno(),))
    lock.close()   # Sperre lebt im Kind weiter (gleiche offene Datei)
    wjson(os.path.join(SHM, "current.json"), {"dir": cdir, "pid": p.pid})
    total = args.idle + args.play + args.step + 3 * SETTLE
    print("Kampagne gestartet (reine Messzeit ca. %d min). Sie laeuft auch weiter, wenn die Verbindung abreisst." % round(total / 60))
    print("Fortschritt jederzeit:  python3 hostmess.py status      Abbruch:  python3 hostmess.py stop\n")
    follow(logp, p)
    return 0

if __name__ == "__main__":
    sys.exit(main())
