#!/usr/bin/env python3
"""vschwank.py v1.0 - Ursachenanalyse der Spannungsschwankung bei Wiedergabe (Diretta-Host, Raspberry Pi 5).

Benoetigt hostmess.py im selben Verzeichnis (gemeinsame, getestete Mess-Bausteine). Nur Standardbibliothek,
kein sudo, aendert keine Konfiguration.

  python3 vschwank.py run [--dur S] [--ohne-musik]   Versuchsreihe starten (laeuft abgekoppelt weiter)
  python3 vschwank.py status | stop
  python3 vschwank.py analyse ORDNER                 Bericht neu erzeugen (schreibt nichts)
  python3 vschwank.py vergleich ORDNER_A ORDNER_B    Vorher/Nachher-Vergleich zweier Reihen
  python3 vschwank.py selftest

Versuchsplan (je Bedingung DUR s, Standard 180 s):
  Musik an : P50 Wiedergabe @50 Hz | P47 Wiedergabe @47 Hz (Alias-Test)
  Musik aus: I50 Ruhe @50 | I47 Ruhe @47 | E50/E47 nachgebildeter Diretta-Verkehr auf end0 (Rate/Groesse aus P50)
             U50 nachgebildeter Verkehr ueber den USB-Netzwerkadapter (enu1, Rate/Groesse aus P50)
             C50 CPU-Weckrhythmus wie Diretta (Rate aus P50) auf CPU0 | I50b Ruhe-Wiederholung (Stabilitaet)
Auswertung: Statistik, Spektrum (Welch), Allan-Abweichung, Alias-Vorhersage, Varianzanteile, Aktivitaet.
"""
import argparse, cmath, fcntl, glob, hashlib, math, os, shutil, signal, socket, subprocess, sys, threading, time, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import hostmess as H
except ImportError:
    sys.exit("hostmess.py fehlt im selben Verzeichnis wie vschwank.py")

VERSION = "1.0"
SHM = os.environ.get("VS_SHM", "/dev/shm/vschwank")
RESULTS = os.environ.get("VS_RESULTS", os.path.join(H.EXT5V, "results"))
END0 = os.environ.get("VS_END0", "end0")
LAN = os.environ.get("VS_LAN", "enu1")
END0_DST = os.environ.get("VS_END0_DST", "172.20.0.2")       # Target (Diretta-Gegenstelle)
LAN_DST = os.environ.get("VS_LAN_DST", "192.168.178.1")      # Router
SETTLE_CHANGE = float(os.environ.get("VS_SETTLE", "30"))     # nach Musik an/aus
SETTLE_SAME = float(os.environ.get("VS_SETTLE_SAME", "15"))  # zwischen Bedingungen gleichen Zustands
WAIT_MAX = float(os.environ.get("VS_WAIT", "1800"))
NSEG = 512                                                   # Welch-Segment (Potenz von 2)
BANDS = ((0.02, 0.2), (0.2, 2.0), (2.0, 10.0), (10.0, 1e9))
log = H.log


# ------------------------------------------------------------------ Mathematik
def fft(x):
    """iterative Radix-2-FFT (len(x) = 2^k)."""
    n = len(x); a = list(x); j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit; bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    size = 2
    while size <= n:
        w = cmath.exp(-2j * math.pi / size); half = size // 2
        for st in range(0, n, size):
            wk = 1.0
            for k in range(half):
                u = a[st + k]; v = a[st + k + half] * wk
                a[st + k] = u + v; a[st + k + half] = u - v
                wk *= w
        size *= 2
    return a

def welch(v, fs, n=NSEG):
    """einseitige PSD [Einheit^2/Hz], Hann-Fenster, 50 % Ueberlappung, Mittelwert je Segment entfernt."""
    if len(v) < n:
        return [], []
    w = [0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]
    u = sum(x * x for x in w)
    acc = [0.0] * (n // 2 + 1); cnt = 0
    for st in range(0, len(v) - n + 1, n // 2):
        seg = v[st:st + n]; m = math.fsum(seg) / n
        X = fft([(seg[i] - m) * w[i] for i in range(n)])
        for k in range(n // 2 + 1):
            acc[k] += abs(X[k]) ** 2
        cnt += 1
    psd = [acc[k] / cnt / (fs * u) * (1 if k in (0, n // 2) else 2) for k in range(n // 2 + 1)]
    return [k * fs / n for k in range(n // 2 + 1)], psd

def band_rms(f, p, lo, hi):
    if len(f) < 2:
        return float("nan")
    df = f[1] - f[0]
    return math.sqrt(math.fsum(pk * df for fk, pk in zip(f, p) if lo <= fk < hi and fk > 0))

def peaks(f, p, top=3, ratio=4.0):
    """lokale Maxima mit PSD > ratio x Median der Umgebung (+-1 Hz); -> [(f, Faktor, rms_mV)]"""
    out = []
    if len(f) < 8:
        return out
    df = f[1] - f[0]; r = max(2, int(round(1.0 / df)))
    for k in range(2, len(p) - 1):
        if not (p[k] >= p[k - 1] and p[k] >= p[k + 1]):
            continue
        nb = sorted(p[max(1, k - r):k - 1] + p[k + 2:k + r + 1])
        if not nb:
            continue
        med = nb[len(nb) // 2]
        if med > 0 and p[k] / med >= ratio:
            rms = math.sqrt(math.fsum(p[max(1, k - 1):k + 2]) * df)
            out.append((f[k], p[k] / med, rms))
    return sorted(out, key=lambda t: -t[2])[:top]

def excess(mc, mr, fa, win=0.2):
    """Zusatzleistung bei fa: max ueber +-win Hz von PSD_Bedingung/PSD_Ruhe (3-Bin-Mittel) und Faktor ueber Umgebung."""
    f, pc = mc.get("f") or [], mc.get("psd") or []
    pr = mr.get("psd") if mr else None
    if len(f) < 8 or not pr or len(pr) != len(pc):
        return float("nan"), float("nan")
    df = f[1] - f[0]; r = max(2, int(round(1.0 / df))); best = (float("nan"), float("nan"))
    for k in range(1, len(f) - 1):
        if abs(f[k] - fa) > win:
            continue
        c3 = (pc[k - 1] + pc[k] + pc[k + 1]) / 3; r3 = (pr[k - 1] + pr[k] + pr[k + 1]) / 3
        nb = sorted(pc[max(1, k - r):k - 1] + pc[k + 2:k + r + 1])
        loc = pc[k] / nb[len(nb) // 2] if nb and nb[len(nb) // 2] > 0 else float("nan")
        ratio = c3 / r3 if r3 > 0 else float("inf")
        if math.isnan(best[0]) or ratio > best[0]:
            best = (ratio, loc)
    return best

def alias(fsrc, fs):
    return abs(fsrc - round(fsrc / fs) * fs)

def pct(s, q):
    if not s:
        return float("nan")
    r = (len(s) - 1) * q / 100.0; lo = int(math.floor(r)); hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (r - lo)

def adev(v, fs, taus=(0.04, 0.16, 0.64, 2.56, 10.24)):
    out = {}
    for tau in taus:
        k = int(round(tau * fs))
        m = len(v) // k if k >= 1 else 0
        if m < 8:
            continue
        y = [math.fsum(v[i * k:(i + 1) * k]) / k for i in range(m)]
        out[tau] = math.sqrt(math.fsum((y[i + 1] - y[i]) ** 2 for i in range(m - 1)) / (2 * (m - 1)))
    return out


# ------------------------------------------------------------------ Lastgeneratoren (Thread, CPU0)
class Pacer(threading.Thread):
    """ruft fn() mit fester Rate auf; protokolliert Anzahl, Fehler und Verspaetung."""
    def __init__(self, rate, fn, name):
        super().__init__(daemon=True)
        self.rate, self.fn, self.label = rate, fn, name
        self.stop_ev = threading.Event(); self.n = 0; self.err = 0; self.late = []; self.t0 = self.t1 = None

    def run(self):
        per = 1.0 / self.rate; nxt = time.monotonic(); self.t0 = H.now_boot()
        while not self.stop_ev.is_set():
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)
            self.late.append(time.monotonic() - nxt)
            try:
                self.fn()
            except OSError:
                self.err += 1
            self.n += 1
            nxt += per
            if time.monotonic() - nxt > 0.5:      # Ueberlast: Takt neu aufsetzen statt nachzuholen
                nxt = time.monotonic()
        self.t1 = H.now_boot()

    def stats(self):
        dt = (self.t1 or H.now_boot()) - (self.t0 or H.now_boot())
        s = sorted(self.late)
        return {"label": self.label, "soll_hz": self.rate, "ist_hz": self.n / dt if dt > 0 else 0.0, "n": self.n,
                "fehler": self.err, "verzug_p50_ms": 1000 * pct(s, 50), "verzug_p99_ms": 1000 * pct(s, 99),
                "verzug_max_ms": 1000 * (s[-1] if s else float("nan"))}

def udp_sender(dst, size):
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = os.urandom(size)
    return lambda: sk.sendto(payload, (dst, 9))

def cpu_burst(us=50):
    def f():
        t = time.perf_counter()
        while time.perf_counter() - t < us * 1e-6:
            pass
    return f


# ------------------------------------------------------------------ Zaehler
def counters():
    return {"t": H.now_boot(), "stat": H.rd("/proc/stat"), "interrupts": H.rd("/proc/interrupts"),
            "softirqs": H.rd("/proc/softirqs"), "netdev": H.rd("/proc/net/dev"),
            "throttled": H.sh(["vcgencmd", "get_throttled"])}

def net_rates(a, b):
    dt = b["t"] - a["t"]; na, nb = H.parse_netdev(a["netdev"]), H.parse_netdev(b["netdev"]); out = {}
    for k in nb:
        if k in na and k != "lo":
            out[k] = {x: (nb[k][x] - na[k][x]) / dt for x in ("rxp", "rxb", "txp", "txb")}
    return out


# ------------------------------------------------------------------ Versuchsreihe
class Series:
    def __init__(self, cdir, dur, with_music):
        self.dir, self.dur, self.music = cdir, dur, with_music
        self.raw = os.path.join(cdir, "raw"); os.makedirs(self.raw, exist_ok=True)
        self.children, self.gen, self.params, self.state = [], None, {}, None

    def wait_state(self, want):
        txt = "Musik STARTEN (96 kHz, Wiederholung an)" if want else "Musik STOPPEN"
        t_end = time.monotonic() + WAIT_MAX; ann = 0
        while True:
            if H.alsa_state()[0] == want:
                settle = SETTLE_SAME if self.state == want else SETTLE_CHANGE
                log("Zustand %s erkannt - %.0f s Beruhigung" % ("Wiedergabe" if want else "Ruhe", settle))
                t_s = time.monotonic() + settle; ok = True
                while time.monotonic() < t_s:
                    if H.alsa_state()[0] != want:
                        ok = False; break
                    time.sleep(0.5)
                if ok:
                    self.state = want; return
            if not ann or time.monotonic() - ann >= 120:
                log(">>> BITTE JETZT: %s  (Messung startet automatisch; Abbruch in %d min)" % (
                    txt, max(0, round((t_end - time.monotonic()) / 60))))
                ann = time.monotonic()
            if time.monotonic() > t_end:
                raise SystemExit("Zeitueberschreitung beim Warten auf: " + txt)
            time.sleep(1.0)

    def condition(self, tag, hz, want_play, gen=None):
        log("== %s: %s, %d Hz, %d s%s" % (tag, "Wiedergabe" if want_play else "Ruhe", hz, self.dur,
                                         (", Last: " + gen[0]) if gen else ""))
        self.wait_state(want_play)
        cd = os.path.join(self.dir, tag); os.makedirs(cd, exist_ok=True)
        info = {"tag": tag, "hz": hz, "dur": self.dur, "want_play": want_play, "alsa_start": H.alsa_state()[1]}
        if gen:
            self.gen = Pacer(gen[1], gen[2], gen[0]); self.gen.start()
            time.sleep(3.0)                                   # Last laeuft vor Messbeginn eingeschwungen
        c0 = counters()
        smp = H.Sampler(os.path.join(cd, "sampler.json")); smp.start()
        outp = os.path.join(cd, "run_out.txt")
        env = dict(os.environ, EXT5V_CPU=H.LOGGER_CPU, EXT5V_OUT=self.raw)
        with open(outp, "w") as of:
            p = subprocess.Popen([os.path.join(H.EXT5V, "ext5v_run.sh"), str(hz), str(self.dur), "vs-" + tag], env=env,
                                 stdin=subprocess.DEVNULL, stdout=of, stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append(p)
        lpid, lstart = H.find_logger_start(p.pid)
        info.update({"logger_pid": lpid, "logger_start": lstart})
        if lstart is None:
            log("FEHLER ext5v_log-Start nicht erkannt")
        try:
            p.wait(timeout=self.dur + 120)
        except subprocess.TimeoutExpired:
            info["timeout"] = True; H.kill_group(p)
        self.children.remove(p)
        c1 = counters()
        smp.stop_ev.set(); smp.join(10)
        if self.gen:
            self.gen.stop_ev.set(); self.gen.join(10)
            info["gen"] = self.gen.stats(); self.gen = None
        out = H.rd(outp)
        runs = [l for l in out.splitlines() if l.startswith(self.raw)]
        info.update({"rc": p.returncode, "run_dir": os.path.relpath(runs[-1], self.dir) if runs else None,
                     "alsa_end": H.alsa_state()[1], "c0": c0, "c1": c1, "sampler_err": smp.err})
        H.wjson(os.path.join(cd, "cond.json"), info)
        log("%s beendet: logger_rc=%s%s" % (tag, p.returncode, ("  Last %.1f/%.0f Hz" % (
            info["gen"]["ist_hz"], info["gen"]["soll_hz"])) if "gen" in info else ""))
        return info

    def derive(self, play_info):
        """Nachbildungs-Parameter aus der gemessenen Wiedergabe (P50)."""
        nr = net_rates(play_info["c0"], play_info["c1"]) if play_info else {}
        def mtu(i):
            v = H.rd("/sys/class/net/%s/mtu" % i).strip()
            return int(v) if v.isdigit() else 1500
        e, l = nr.get(END0, {}), nr.get(LAN, {})
        p = {"quelle": "gemessen (P50)" if e.get("txp", 0) > 50 else "Standardwerte (keine Wiedergabe gemessen)"}
        p["end0_hz"] = e["txp"] if e.get("txp", 0) > 50 else 500.0
        eb = e["txb"] / e["txp"] if e.get("txp", 0) > 50 else 1514.0
        p["lan_hz"] = l["rxp"] if l.get("rxp", 0) > 50 else 360.0
        lb = l["rxb"] / l["rxp"] if l.get("rxp", 0) > 50 else 1514.0
        # /proc/net/dev zaehlt Ethernet-Rahmen ohne FCS: Nutzlast = Rahmen - 14 (Eth) - 20 (IPv4) - 8 (UDP)
        p["end0_payload"] = int(max(18, min(eb - 42, mtu(END0) - 28)))
        p["lan_payload"] = int(max(18, min(lb - 42, mtu(LAN) - 28)))
        p["cpu_hz"] = p["end0_hz"]; p["lan_if"] = LAN
        self.params = p
        H.wjson(os.path.join(self.dir, "params.json"), p)
        log("Nachbildung: end0 %.1f Pak/s x %d B, %s %.1f Pak/s x %d B, CPU-Wecker %.1f Hz (%s)" % (
            p["end0_hz"], p["end0_payload"], LAN, p["lan_hz"], p["lan_payload"], p["cpu_hz"], p["quelle"]))

    def run(self):
        P = None
        if self.music:
            P = self.condition("P50", 50, True)
            self.condition("P47", 47, True)
        self.derive(P)
        p = self.params
        self.condition("I50", 50, False)
        self.condition("I47", 47, False)
        self.condition("E50", 50, False, ("end0-Verkehr", p["end0_hz"], udp_sender(END0_DST, p["end0_payload"])))
        self.condition("E47", 47, False, ("end0-Verkehr", p["end0_hz"], udp_sender(END0_DST, p["end0_payload"])))
        self.condition("U50", 50, False, (LAN + "-Verkehr", p["lan_hz"], udp_sender(LAN_DST, p["lan_payload"])))
        self.condition("C50", 50, False, ("CPU0-Wecker 50us", p["cpu_hz"], cpu_burst(50)))
        self.condition("I50b", 50, False)

    def cleanup(self):
        if self.gen:
            self.gen.stop_ev.set(); self.gen.join(5)
        for p in list(self.children):
            H.kill_group(p)


def child_main(cdir, dur, music, lockfd):
    lock = os.fdopen(lockfd, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("== ABBRUCH: eine andere Messung laeuft bereits"); return 1
    try:
        os.sched_setaffinity(0, {H.CTL_CPU})
    except OSError as e:
        log("WARN  CPU-Bindung nicht moeglich: %s" % e)
    s = Series(cdir, dur, music)
    def on_term(*_):
        raise SystemExit("durch stop beendet")
    signal.signal(signal.SIGTERM, on_term)
    rc, done = 0, False
    try:
        pre = preflight()
        H.wjson(os.path.join(cdir, "series.json"), {"version": VERSION, "hostmess": H.VERSION, "dur": dur,
                "music": music, "start": time.strftime("%Y-%m-%dT%H:%M:%S"), "host": os.uname().nodename,
                "kernel": os.uname().release, "pid": os.getpid(), "pre": pre,
                "env": {k: v for k, v in os.environ.items() if k.startswith(("VS_", "HOSTMESS_"))},
                "diretta_setting": H.rd("/opt/diretta-alsa/setting.inf", None), "test_mode": H.TEST})
        s.run(); done = True
    except SystemExit as e:
        log("== ABBRUCH: %s" % e); rc = 1
    except Exception:
        log("== ABBRUCH: unerwarteter Fehler\n" + traceback.format_exc()); rc = 2
    finally:
        s.cleanup()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    conds = [d for d in os.listdir(cdir) if os.path.isfile(os.path.join(cdir, d, "cond.json"))]
    if not conds:
        log("Keine Bedingung abgeschlossen - nichts gespeichert"); return rc or 1
    try:
        log("== Messungen %s - Musik darf wieder laufen. Auswertung ..." % ("fertig" if done else "TEILWEISE"))
        dst = os.path.join(RESULTS, "vschwank_" + os.path.basename(cdir) + ("" if done else "_ABGEBROCHEN"))
        shutil.copytree(cdir, dst, ignore=shutil.ignore_patterns("run.log"))
        full, short = analyse(dst)
        for fn, txt in (("report.txt", full), ("kurz.txt", short)):
            with open(os.path.join(dst, fn), "w") as f:
                f.write(txt)
        print(short, flush=True)
        log("== %s  Ergebnis: %s" % ("FERTIG" if done else "TEILERGEBNIS GESPEICHERT", dst))
        log("Kurzbericht: cat %s/kurz.txt" % dst)
        write_sums(cdir, dst)
    except Exception:
        log("== FEHLER bei der Auswertung (Rohdaten in %s)\n%s" % (cdir, traceback.format_exc())); rc = rc or 2
    return rc

def write_sums(cdir, dst):
    shutil.copy2(os.path.join(cdir, "run.log"), os.path.join(dst, "run.log"))
    sums = []
    for root, _, files in os.walk(dst):
        for fn in files:
            if fn != "SHA256SUMS":
                fp = os.path.join(root, fn)
                with open(fp, "rb") as fh:
                    sums.append((os.path.relpath(fp, dst), hashlib.sha256(fh.read()).hexdigest()))
    with open(os.path.join(dst, "SHA256SUMS"), "w") as f:
        f.write("".join("%s  %s\n" % (h, n) for n, h in sorted(sums)))

def preflight():
    log("== Vorpruefung")
    errs = []
    model = H.rd("/proc/device-tree/model").replace("\0", "").strip()
    if not model.startswith("Raspberry Pi 5") and not H.TEST:
        errs.append("kein Raspberry Pi 5")
    for f in ("ext5v_run.sh", "ext5v_log"):
        if not os.access(os.path.join(H.EXT5V, f), os.X_OK):
            errs.append("fehlt: " + os.path.join(H.EXT5V, f))
    if H.other_logger_running():
        errs.append("ext5v_log laeuft bereits")
    if not glob.glob(H.ALSA_GLOB):
        errs.append("kein ALSA-Wiedergabegeraet")
    nd = H.parse_netdev(H.rd("/proc/net/dev"))
    for i in (END0, LAN):
        if i not in nd and not H.TEST:
            errs.append("Netzwerkschnittstelle fehlt: " + i)
    for dst in (END0_DST, LAN_DST):
        try:
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"vs", (dst, 9))
        except OSError as e:
            errs.append("Ziel %s nicht erreichbar: %s" % (dst, e))
    if errs:
        for e in errs:
            log("FEHLER " + e)
        raise SystemExit("Vorpruefung nicht bestanden - nichts gemessen")
    log("OK    %s, Schnittstellen %s/%s, Ziele %s/%s" % (model or "?", END0, LAN, END0_DST, LAN_DST))
    return {"model": model}


# ------------------------------------------------------------------ Auswertung
def load_cond(cdir, tag):
    pth = os.path.join(cdir, tag, "cond.json")
    if not os.path.isfile(pth):
        return None
    info = H.rjson(pth)
    rdir = os.path.join(cdir, info["run_dir"]) if info.get("run_dir") else None
    volt = H.load_voltage(rdir, 0.0) if rdir else []
    t = [a for a, _ in volt]; v = [b * 1000.0 for _, b in volt]          # mV
    fs = (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else float("nan")
    smp = H.rjson(os.path.join(cdir, tag, "sampler.json"))["rows"]
    al = [r["alsa"] for r in smp]
    info.update({"v": v, "fs": fs, "alsa_frac": 100.0 * sum(al) / len(al) if al else float("nan"),
                 "rates": sorted({r["rate"] for r in smp if r.get("rate")})})
    return info

def metrics(c):
    v = c["v"]; s = sorted(v)
    m = {"n": len(v), "fs": c["fs"]}
    if len(v) < 2:
        return m
    m.update({"mean": H.mean(v), "sd": H.sdev(v), "p01": pct(s, 0.1), "p999": pct(s, 99.9), "min": s[0], "max": s[-1]})
    f, p = welch(v, c["fs"])
    m["f"], m["psd"] = f, p
    m["bands"] = [band_rms(f, p, lo, hi) for lo, hi in BANDS]
    m["peaks"] = peaks(f, p)
    m["adev"] = adev(v, c["fs"])
    a, b = c["c0"], c["c1"]; dt = b["t"] - a["t"]
    sa, sb = H.parse_stat(a["stat"]), H.parse_stat(b["stat"])
    m["busy"] = {k: 100.0 * (H.cpu_busy(sb["cpus"][k]) - H.cpu_busy(sa["cpus"][k])) /
                 max(1, H.cpu_total(sb["cpus"][k]) - H.cpu_total(sa["cpus"][k])) for k in sorted(sb["cpus"]) if k in sa["cpus"]}
    m["ctxt"] = (sb.get("ctxt", 0) - sa.get("ctxt", 0)) / dt; m["intr"] = (sb.get("intr", 0) - sa.get("intr", 0)) / dt
    m["net"] = net_rates(a, b)
    n, ia = H.parse_table(a["interrupts"]); _, ib = H.parse_table(b["interrupts"]); rows = []
    for k, (cb, desc) in ib.items():
        d = [y - x for x, y in zip(ia.get(k, ([0] * n, ""))[0], cb)]
        if sum(d) > 0:
            rows.append((sum(d) / dt, k, desc.split()[-1] if desc else "", [x / dt for x in d]))
    m["irq"] = sorted(rows, reverse=True)[:5]
    ta, tb = H.thr_val(a.get("throttled")), H.thr_val(b.get("throttled"))
    m["thr_ok"] = ta is not None and tb is not None and not (ta & 0xF) and not (tb & 0xF) and not (tb & ~ta & 0xF0000)
    return m

def checks_for(tag, c, m):
    out = [("%s: logger_rc=0" % tag, c.get("rc") == 0 and not c.get("timeout") and c.get("logger_start") is not None),
           ("%s: Daten vollstaendig (N=%d, Soll %d)" % (tag, m["n"], c["dur"] * c["hz"]), m["n"] >= 0.98 * c["dur"] * c["hz"]),
           ("%s: Abtastrate %.3f Hz (Soll %d)" % (tag, m["fs"], c["hz"]), abs(m["fs"] - c["hz"]) < 0.01 * c["hz"]),
           ("%s: Wiedergabe %s (ist %.0f %%)" % (tag, "an" if c["want_play"] else "aus", c["alsa_frac"]),
            abs(c["alsa_frac"] - (100.0 if c["want_play"] else 0.0)) < 1e-9),
           ("%s: keine Unterspannung/Drosselung" % tag, m.get("thr_ok", False)),
           ("%s: Sampler ohne Fehler" % tag, c.get("sampler_err") is None)]
    if c["want_play"]:
        out.append(("%s: Abtastrate Musik konstant %s" % (tag, c["rates"]), len(c["rates"]) == 1))
    g = c.get("gen")
    if g:
        out.append(("%s: Last %.1f von %.1f Hz, Verzug p99 %.2f ms, Fehler %d" % (tag, g["ist_hz"], g["soll_hz"],
                    g["verzug_p99_ms"], g["fehler"]), abs(g["ist_hz"] - g["soll_hz"]) <= 0.02 * g["soll_hz"] and g["fehler"] == 0
                    and g["verzug_p99_ms"] < 1.0))
    return out

def fmt(x, nd=2):
    return "n/a" if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else "%.*f" % (nd, x)

def analyse(cdir):
    L, S, checks = [], [], []
    try:
        meta = H.rjson(os.path.join(cdir, "series.json"))
    except OSError:
        t = "keine Versuchsreihe in %s\n" % cdir; return t, t
    hd = "vschwank %s  host=%s  start=%s  Dauer je Bedingung %d s%s" % (meta["version"], meta["host"], meta["start"],
         meta["dur"], "  TESTMODUS" if meta.get("test_mode") else "")
    L.append(hd); S.append(hd)
    params = H.rjson(os.path.join(cdir, "params.json")) if os.path.isfile(os.path.join(cdir, "params.json")) else {}
    tags = ["P50", "P47", "I50", "I47", "E50", "E47", "U50", "C50", "I50b"]
    C, M = {}, {}
    for t in tags:
        c = load_cond(cdir, t)
        if c:
            C[t] = c; M[t] = metrics(c); checks += checks_for(t, c, M[t])
    lan = params.get("lan_if", LAN)
    if params:
        S.append("Nachbildung: end0 %.1f Pak/s x %d B | %s %.1f Pak/s x %d B | CPU %.1f Hz (%s)" % (
            params["end0_hz"], params["end0_payload"], lan, params["lan_hz"], params["lan_payload"], params["cpu_hz"], params["quelle"]))
    # Tabelle
    hdr = "Bed.  fs[Hz]  mean[V]   sd   P0,1-Ruhe  | rms 0,02-0,2 0,2-2 2-10 >10 Hz [mV] | ADEV 0,04/0,64/2,56 s"
    L.append(""); L.append(hdr); S.append(""); S.append(hdr)
    base = M.get("I50", {}).get("mean")
    for t in tags:
        m = M.get(t)
        if not m or "sd" not in m:
            continue
        ad = m["adev"]
        line = "%-5s %6.2f %8.4f %5.2f %8s   | %s | %s" % (
            t, m["fs"], m["mean"] / 1000, m["sd"], fmt(m["p01"] - base if base else None),
            " ".join("%5s" % fmt(b) for b in m["bands"]), "/".join(fmt(ad.get(x)) for x in (0.04, 0.64, 2.56)))
        L.append(line); S.append(line)
    # Spektrale Spitzen
    L.append(""); L.append("Spektrale Spitzen (Frequenz Hz / Faktor ueber Umgebung / rms mV / R=Faktor ggue. Ruhe gleicher Rate):")
    S.append(""); S.append("Spitzen:")
    for t in tags:
        m = M.get(t)
        if m and m.get("peaks") is not None and "sd" in m:
            ref = M.get("I47" if t.endswith("47") else "I50")
            txt = "; ".join("%.2f/%.0fx/%.2f/R%s" % (pk + (fmt(excess(m, ref, pk[0], 0.05)[0], 0),)) for pk in m["peaks"]) or "keine"
            L.append("  %-5s %s" % (t, txt)); S.append("  %-5s %s" % (t, txt))
    # Alias-Test
    cands = []
    if params:
        cands = [("Diretta-Zyklus", params["end0_hz"]), ("2x Zyklus", 2 * params["end0_hz"]), (lan + "-Pakete", params["lan_hz"])]
    L.append(""); L.append("Alias-Test (Vorhersage aus gemessener Abtastrate; Faktor = PSD ggue. Ruhe gleicher Rate / ggue. Umgebung;"
             " SPITZE wenn >= 4 ggue. Ruhe UND >= 10 ggue. Umgebung):")
    S.append(""); S.append("Alias-Test:")
    for name, fsrc in cands:
        res = []
        for t in ("P50", "P47", "E50", "E47"):
            m = M.get(t)
            if not m or not m.get("f"):
                continue
            fa = alias(fsrc, m["fs"])
            ref = M.get("I47" if t.endswith("47") else "I50")
            rr, loc = excess(m, ref, fa)
            hit = rr >= 4 and loc >= 10
            res.append("%s %.2f Hz %s/%s%s" % (t, fa, fmt(rr, 0), fmt(loc, 0), " SPITZE" if hit else ""))
        line = "  %-15s %7.1f Hz -> %s" % (name, fsrc, " | ".join(res) or "keine Daten")
        L.append(line); S.append(line)
    # Varianzanteile
    if "I50" in M and "P50" in M and "sd" in M["I50"] and "sd" in M["P50"]:
        vi, vp = M["I50"]["sd"] ** 2, M["P50"]["sd"] ** 2
        L.append(""); S.append("")
        head = "Zusatzvarianz Wiedergabe (P50-I50): %.2f mV^2 (sd %.2f -> %.2f mV)" % (vp - vi, M["I50"]["sd"], M["P50"]["sd"])
        L.append(head); S.append(head)
        for t, lab in (("E50", "end0-Verkehr"), ("U50", lan + "-Verkehr"), ("C50", "CPU-Wecker")):
            if t in M and "sd" in M[t] and vp > vi:
                ex = M[t]["sd"] ** 2 - vi
                line = "  %-13s Zusatzvarianz %6.2f mV^2 = %5.0f %% der Wiedergabe-Zusatzvarianz (Naeherung)" % (lab, ex, 100 * ex / (vp - vi))
                L.append(line); S.append(line)
        if "I50b" in M and "sd" in M["I50b"]:
            line = "  Stabilitaet Ruhe: I50 sd %.2f / I50b sd %.2f mV, Mittel %+.2f mV" % (
                M["I50"]["sd"], M["I50b"]["sd"], M["I50b"]["mean"] - M["I50"]["mean"])
            L.append(line); S.append(line)
    # Aktivitaet
    L.append(""); L.append("Aktivitaet je Bedingung:")
    for t in tags:
        m = M.get(t)
        if not m or "busy" not in m:
            continue
        L.append("  %-5s busy %s | ctxt %.0f/s irq %.0f/s" % (t, " ".join("%s=%.2f" % (k[3:], v) for k, v in m["busy"].items()),
                 m["ctxt"], m["intr"]))
        L.append("        Netz: " + "; ".join("%s rx %.0f/%.0f tx %.0f/%.0f (Pak/s / kB/s)" % (
            k, v["rxp"], v["rxb"] / 1000, v["txp"], v["txb"] / 1000) for k, v in sorted(m["net"].items()) if v["rxp"] + v["txp"] > 0.5))
        L.append("        IRQ: " + "; ".join("%s %s %.0f/s [%s]" % (k, d, r, " ".join("%.0f" % x for x in per)) for r, k, d, per in m["irq"]))
        if C[t].get("gen"):
            g = C[t]["gen"]
            L.append("        Last %s: %.2f/%.2f Hz, Verzug p50 %.3f p99 %.3f max %.3f ms, Fehler %d" % (
                g["label"], g["ist_hz"], g["soll_hz"], g["verzug_p50_ms"], g["verzug_p99_ms"], g["verzug_max_ms"], g["fehler"]))
    nf = [x for x, ok in checks if not ok]
    L.append(""); L.append("Pruefungen: %d von %d bestanden" % (len(checks) - len(nf), len(checks)))
    L.extend("  [%s] %s" % ("PASS" if ok else "FAIL", x) for x, ok in checks)
    S.append(""); S.append("Pruefungen: %d von %d bestanden%s" % (len(checks) - len(nf), len(checks), "" if nf else " (alle)"))
    S.extend("  [FAIL] " + x for x in nf)
    return "\n".join(L) + "\n", "\n".join(S) + "\n"

def compare(a, b):
    out = ["Vergleich A=%s  B=%s" % (os.path.basename(a), os.path.basename(b)),
           "Bed.   sd_A   sd_B   Diff  | mean_A    mean_B   | rms 2-10 Hz A/B"]
    for t in ("P50", "P47", "I50", "I47", "E50", "E47", "U50", "C50", "I50b"):
        ca, cb = load_cond(a, t), load_cond(b, t)
        if not ca or not cb:
            continue
        ma, mb = metrics(ca), metrics(cb)
        if "sd" not in ma or "sd" not in mb:
            continue
        out.append("%-5s %6.2f %6.2f %+6.2f | %8.4f %8.4f | %s/%s" % (t, ma["sd"], mb["sd"], mb["sd"] - ma["sd"],
                   ma["mean"] / 1000, mb["mean"] / 1000, fmt(ma["bands"][2]), fmt(mb["bands"][2])))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ Selbsttest
def selftest():
    ok = True
    def chk(c, msg):
        nonlocal ok
        print("[%s] %s" % ("PASS" if c else "FAIL", msg)); ok &= bool(c)
    import random
    random.seed(1)
    x = [complex(random.random(), 0) for _ in range(64)]
    X = fft(x)
    dft = [sum(x[n] * cmath.exp(-2j * math.pi * k * n / 64) for n in range(64)) for k in range(64)]
    chk(max(abs(a - b) for a, b in zip(X, dft)) < 1e-9, "FFT = direkte DFT")
    fs = 50.0; n = 9000
    w = [random.gauss(0, 2.0) for _ in range(n)]
    f, p = welch(w, fs)
    tot = math.sqrt(math.fsum(p) * (f[1] - f[0]))
    chk(abs(tot - 2.0) < 0.1, "Parseval: weisses Rauschen sd 2,0 -> PSD-Integral %.3f" % tot)
    s = [3.0 * math.sin(2 * math.pi * 5.14 * i / fs) + random.gauss(0, 0.5) for i in range(n)]
    f, p = welch(s, fs); pk = peaks(f, p)
    chk(pk and abs(pk[0][0] - 5.14) < 0.1 and abs(pk[0][2] - 3 / math.sqrt(2)) < 0.25,
        "Sinus 5,14 Hz Amplitude 3: Spitze %.2f Hz rms %.2f (Soll 2,12)" % (pk[0][0], pk[0][2]) if pk else "Sinus: keine Spitze")
    chk(abs(alias(505.1, 49.996) - 5.14) < 0.01 and abs(alias(505.1, 47.0) - 11.9) < 0.01 and alias(500.0, 50.0) == 0.0,
        "Alias-Formel (505,1 Hz -> 5,14 / 11,90 Hz)")
    # Abtastung eines 505,1-Hz-Signals mit 47 Hz muss Spitze bei 11,9 Hz ergeben
    s = [2.0 * math.sin(2 * math.pi * 505.1 * i / 47.0) for i in range(8460)]
    f, p = welch(s, 47.0); pk = peaks(f, p)
    chk(pk and abs(pk[0][0] - 11.9) < 0.1, "Abtastung 505,1 Hz @47 Hz -> Spitze %.2f Hz" % (pk[0][0] if pk else -1))
    chk(abs(pct([1, 2, 3, 4], 50) - 2.5) < 1e-12 and pct([5], 99) == 5, "Perzentil")
    a = adev([float(i % 2) for i in range(1000)], 50.0)
    chk(abs(a[0.04] - 0.0) < 1e-12, "ADEV (Periode 2 bei tau=2 Samples = 0)")
    cnt = []
    pc = Pacer(500.0, lambda: cnt.append(1), "t"); pc.start(); time.sleep(1.0); pc.stop_ev.set(); pc.join()
    st = pc.stats()
    chk(abs(st["ist_hz"] - 500) < 15, "Pacer 500 Hz -> %.1f Hz, p99 Verzug %.3f ms" % (st["ist_hz"], st["verzug_p99_ms"]))
    print("SELBSTTEST %s" % ("BESTANDEN" if ok else "NICHT BESTANDEN"))
    return 0 if ok else 1


# ------------------------------------------------------------------ Kommandozeile
def main():
    ap = argparse.ArgumentParser(prog="vschwank.py")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run"); r.add_argument("--dur", type=int, default=180); r.add_argument("--ohne-musik", action="store_true")
    sp.add_parser("status"); sp.add_parser("stop"); sp.add_parser("selftest")
    a = sp.add_parser("analyse"); a.add_argument("ordner")
    v = sp.add_parser("vergleich"); v.add_argument("a"); v.add_argument("b")
    k = sp.add_parser("_child"); k.add_argument("dir"); k.add_argument("dur", type=int); k.add_argument("music", type=int)
    k.add_argument("lockfd", type=int)
    args = ap.parse_args()
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "analyse":
        print(analyse(os.path.abspath(args.ordner))[0], end=""); return 0
    if args.cmd == "vergleich":
        print(compare(os.path.abspath(args.a), os.path.abspath(args.b)), end=""); return 0
    if args.cmd == "_child":
        return child_main(args.dir, args.dur, bool(args.music), args.lockfd)
    curp = os.path.join(SHM, "current.json")
    cur = H.rjson(curp) if os.path.isfile(curp) else None
    if args.cmd == "status":
        if not cur:
            print("keine Messung bekannt"); return 0
        print("Messung %s - %s" % (cur["dir"], "LAEUFT" if H.alive(cur["pid"]) else "beendet"))
        print("".join(H.rd(os.path.join(cur["dir"], "run.log")).splitlines(True)[-15:]), end=""); return 0
    if args.cmd == "stop":
        if cur and H.alive(cur["pid"]):
            os.kill(cur["pid"], signal.SIGTERM); print("Abbruch gesendet."); return 0
        print("keine laufende Messung"); return 0
    if args.dur < 60:
        print("--dur mindestens 60 s (Spektralaufloesung)"); return 2
    os.makedirs(SHM, exist_ok=True)
    lock = open(os.path.join(SHM, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Es laeuft bereits eine Messung (python3 vschwank.py status)"); return 1
    try:
        os.sched_setaffinity(0, {H.CTL_CPU})
    except OSError:
        pass
    cdir = os.path.join(SHM, time.strftime("%Y%m%d_%H%M%S")); os.makedirs(cdir)
    logp = os.path.join(cdir, "run.log")
    with open(logp, "w") as lf:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_child", cdir, str(args.dur),
                              str(0 if args.ohne_musik else 1), str(lock.fileno())], stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock.fileno(),))
    lock.close()
    H.wjson(curp, {"dir": cdir, "pid": p.pid})
    n = 7 + (0 if args.ohne_musik else 2)
    print("Versuchsreihe gestartet: %d Bedingungen x %d s, gesamt ca. %d min. Laeuft weiter, auch wenn die Verbindung abreisst." % (
        n, args.dur, round((n * (args.dur + 25) + 60) / 60)))
    print("Fortschritt:  python3 vschwank.py status      Abbruch:  python3 vschwank.py stop\n")
    H.follow(logp, p)
    return 0

if __name__ == "__main__":
    sys.exit(main())
