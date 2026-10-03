#!/usr/bin/env python3
"""vschwank.py v1.3 - Ursachenanalyse der Spannungsschwankung bei Wiedergabe (Diretta-Host, Raspberry Pi 5).

Benoetigt hostmess.py im selben Verzeichnis (gemeinsame, getestete Mess-Bausteine). Nur Standardbibliothek,
kein sudo, aendert keine Konfiguration.

  python3 vschwank.py run [--dur S] [--ohne-musik]   Versuchsreihe starten (laeuft abgekoppelt weiter)
  python3 vschwank.py widerstand [--dur S]          nur Zuleitungswiderstand messen (auch am Target, Musik egal)
  python3 vschwank.py target [--dur S]              Reihe fuer den Diretta-TARGET (Empfaenger): P50/P47/I50/I47, C47/C47x,
                                                    Kalibrierung K7/K113/K313/K213b, R50; Musikzustand = end0-Empfangsrate
  python3 vschwank.py status | stop
  python3 vschwank.py analyse ORDNER                 Bericht neu erzeugen (schreibt nichts)
  python3 vschwank.py vergleich ORDNER_A ORDNER_B    Vorher/Nachher-Vergleich zweier Reihen
  python3 vschwank.py selftest

Versuchsplan (je Bedingung DUR s, Standard 180 s):
  Musik an : P50 Wiedergabe @50 Hz | P47 Wiedergabe @47 Hz (Alias-Test)
  Musik aus: I50 Ruhe @50 | I47 Ruhe @47 | E50/E47 nachgebildeter Diretta-Verkehr auf end0 (Rate/Groesse aus P50)
             U50 nachgebildeter Verkehr ueber den USB-Netzwerkadapter (enu1, Rate/Groesse aus P50)
             C50 CPU-Weckrhythmus wie Diretta (Rate aus P50) auf CPU0
             C47/C47x CPU-Weckrhythmus @47 Hz: Rechenzeit je Zyklus wie Diretta (gemessen P50-I50) bzw. ~10 % Tastverh.
                      (Positivkontrolle) -> Anteil Prozessor an der 500-Hz-Linie
             R50      Laststufe 10 s an / 10 s aus + PMIC-Strommessung -> Widerstand Netzteil+Kabel+Stecker
             K7/K213/K213b Kalibrierung der Messkette (Rechtecklast CPU0 7,3 Hz und 213 Hz @50/@47)
             I50b Ruhe-Wiederholung (Stabilitaet)
Auswertung: Statistik, Spektrum (Welch), Allan-Abweichung, Alias-Vorhersage, Varianzanteile, Aktivitaet.
"""
import argparse, cmath, fcntl, glob, hashlib, math, os, re, shutil, signal, socket, subprocess, sys, threading, time, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import hostmess as H
except ImportError:
    sys.exit("hostmess.py fehlt im selben Verzeichnis wie vschwank.py")

VERSION = "1.3.1"
SHM = os.environ.get("VS_SHM", "/dev/shm/vschwank")
RESULTS = os.environ.get("VS_RESULTS", os.path.join(H.EXT5V, "results"))
END0 = os.environ.get("VS_END0", "end0")
LAN = os.environ.get("VS_LAN", "enu1")
END0_DST = os.environ.get("VS_END0_DST", "172.20.0.2")       # Target (Diretta-Gegenstelle)
LAN_DST = os.environ.get("VS_LAN_DST", "192.168.178.1")      # Router
SETTLE_CHANGE = float(os.environ.get("VS_SETTLE", "60"))     # nach Musik an/aus (auch thermisch)
SETTLE_SAME = float(os.environ.get("VS_SETTLE_SAME", "15"))  # zwischen Bedingungen gleichen Zustands
WAIT_MAX = float(os.environ.get("VS_WAIT", "1800"))
NSEG = 512                                                   # Welch-Segment (Potenz von 2)
BANDS = ((0.09, 0.5), (0.5, 2.0), (2.0, 10.0), (10.0, 1e9))   # unterstes Band ab 1. Bin (0,098 Hz)
KAL_LO, KAL_HI = 7.3, 213.0                                     # Kalibrierfrequenzen (Alias 213 Hz: 13 Hz @50, 22 Hz @47)
KAL_F = (3.1, 7.3, 31.0, 113.0, 213.0, 313.0)                   # Frequenzgang-Stuetzstellen (alle Aliasse @50 Hz pruefbar)
KAL_DUR = 120                                                   # Dauer je Kalibrierbedingung [s]
SQ_FUND = 2 * math.sqrt(2) / math.pi / 2                        # rms der Grundwelle eines Rechtecks je Volt Hub (0,4502)
VAR_FMIN = 0.09                                                 # Varianzanteile ohne Drift (< 0,1 Hz)
R_FREQ = 0.05                                                   # Laststufe R50: 10 s an / 10 s aus
R_SKIP = 2.0                                                    # je Halbperiode verworfen (Einschwingen) [s]
PMIC_DT = 0.5                                                   # PMIC-Abfrage R50 [s]
ETA = (0.80, 0.875, 0.95)                                       # Wirkungsgrad PMIC-Wandler (Annahme, Spanne)
MECH_DUTY = 0.10                                                # C47x: Tastverhaeltnis der Positivkontrolle
KAL_F_TARGET = (7.3, 113.0, 313.0)                              # Target: 3 Stuetzstellen + K213b (Fit braucht >= 3)
PLAY_PPS = float(os.environ.get("VS_PLAY_PPS", "200"))          # Target: Wiedergabe = end0-Empfang > 200 Pak/s
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

def seg_var(v, fs, n=NSEG):
    """Varianz > VAR_FMIN je Welch-Segment (fuer Standardfehler)."""
    out = []
    if len(v) < n:
        return out
    w = [0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]; u = sum(x * x for x in w)
    k0 = max(1, int(math.ceil(VAR_FMIN * n / fs)))
    for st in range(0, len(v) - n + 1, n // 2):
        seg = v[st:st + n]; m = math.fsum(seg) / n
        X = fft([(seg[i] - m) * w[i] for i in range(n)])
        out.append(math.fsum(abs(X[k]) ** 2 * (1 if k == n // 2 else 2) for k in range(k0, n // 2 + 1)) / (fs * u) * fs / n)
    return out

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

def peak_rms(m, fa, win=0.15):
    """rms [mV] einer Linie bei fa: Summe PSD*df ueber +-win um das Maximum, abzueglich lokalem Median-Untergrund."""
    f, p = m.get("f") or [], m.get("psd") or []
    if len(f) < 8:
        return float("nan")
    df = f[1] - f[0]
    ks = [k for k in range(1, len(f)) if abs(f[k] - fa) <= win]
    if not ks:
        return float("nan")
    k = max(ks, key=lambda i: p[i]); r = max(3, int(round(1.0 / df)))
    nb = sorted(p[max(1, k - r):k - 2] + p[k + 3:k + r + 1]); bg = nb[len(nb) // 2] if nb else 0.0
    return math.sqrt(max(0.0, math.fsum(p[i] - bg for i in range(max(1, k - 2), min(len(p), k + 3))) * df))

def sinc_h(f, T):
    x = math.pi * f * T
    return 1.0 if x == 0 else abs(math.sin(x) / x)

def fit_window(pts):
    """Mittelungsfenster T [s] eines Rechteck-(Boxcar-)Sensors aus (f, A)-Paaren; A = A0*|sinc(f T)|.
    Gitter 0..40 ms in 0,05 ms, A0 je T analytisch (kleinste Quadrate). -> (T, A0, rel. Restfehler)"""
    best = None
    for i in range(801):
        T = i * 5e-5
        hs = [sinc_h(f, T) for f, _ in pts]
        den = math.fsum(h * h for h in hs)
        if den <= 0:
            continue
        a0 = math.fsum(h * a for h, (_, a) in zip(hs, pts)) / den
        res = math.fsum((a - a0 * h) ** 2 for h, (_, a) in zip(hs, pts))
        if best is None or res < best[2]:
            best = (T, a0, res)
    if not best or best[1] <= 0:
        return None
    return best[0], best[1], math.sqrt(best[2] / len(pts)) / best[1]

def kal_fit(M, C=None):
    """Kalibrierpunkte (Tag, f, Alias, rms, xRuhe, signifikant) und Fensterfit."""
    krow, kpts = [], []
    for fk in KAL_F:
        t = ktag(fk)
        if t in M and "f" in M[t]:
            fa = alias(fk, M[t]["fs"]); a = peak_rms(M[t], fa)
            d = (C.get(t, {}).get("gen") or {}).get("tastverhaeltnis", float("nan")) if C else float("nan")
            if math.isfinite(d) and 0.1 < d < 0.9:
                a = a / math.sin(math.pi * d)      # Grundwelle eines Rechtecks ~ sin(pi*d); auf 50 % normiert
            rr, _ = excess(M[t], M.get("I50"), fa, 0.15)
            ok = rr >= 4 and math.isfinite(a)
            krow.append((t, fk, fa, a, rr, ok, d))
            if ok:
                kpts.append((fk, a))
    return krow, (fit_window(kpts) if len(kpts) >= 3 else None)

def baseline(M, C, t_mid):
    """Ruhe-Mittel zur Zeit t_mid, linear zwischen den Drift-Klammern I50/I50m/I50b (ausserhalb: naechster Wert)."""
    pts = sorted(((C[t]["c0"]["t"] + C[t]["c1"]["t"]) / 2, M[t]["mean"]) for t in IDLE_REF if t in M and "mean" in M[t])
    if not pts:
        return float("nan"), "keine Ruhe-Referenz"
    if t_mid <= pts[0][0]:
        return pts[0][1], "vor erster Referenz (nicht interpoliert)"
    if t_mid >= pts[-1][0]:
        return pts[-1][1], "nach letzter Referenz (nicht interpoliert)"
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        if ta <= t_mid <= tb:
            return va + (vb - va) * (t_mid - ta) / (tb - ta), "interpoliert"
    return pts[-1][1], "?"

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
        per = 1.0 / self.rate; nxt = time.monotonic(); self.t0 = H.now_boot(); c0 = time.thread_time()
        while not self.stop_ev.is_set():
            d = nxt - time.monotonic()
            if d > 0.05:                      # lange Wartezeit: abbrechbar
                if self.stop_ev.wait(d - 0.02):
                    break
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
            if time.monotonic() - nxt > 3 * per:  # mehr als 3 Perioden zurueck: Takt neu aufsetzen, nicht buendeln
                nxt = time.monotonic(); self.resync = getattr(self, "resync", 0) + 1
        self.t1 = H.now_boot(); self.cpu_s = time.thread_time() - c0   # Rechenzeit dieses Threads (Nutzer + Kern)

    def stats(self):
        dt = (self.t1 or H.now_boot()) - (self.t0 or H.now_boot())
        s = sorted(self.late)
        cpu = getattr(self, "cpu_s", float("nan"))
        return {"label": self.label, "soll_hz": self.rate, "ist_hz": self.n / dt if dt > 0 else 0.0, "n": self.n, "dauer_s": dt,
                "cpu_us_je_aufruf": 1e6 * cpu / self.n if self.n else float("nan"),
                "fehler": self.err, "verzug_p50_ms": 1000 * pct(s, 50), "verzug_p99_ms": 1000 * pct(s, 99),
                "verzug_max_ms": 1000 * (s[-1] if s else float("nan")), "neu_aufgesetzt": getattr(self, "resync", 0),
                "anteil_ueber_1ms": 100.0 * sum(1 for x in s if x > 1e-3) / len(s) if s else float("nan")}

class SquareGen(threading.Thread):
    """Kalibrierlast: sha256sum auf CPU0, per SIGSTOP/SIGCONT im Rechteck (50 %) mit freq Hz geschaltet."""
    def __init__(self, freq, label):
        super().__init__(daemon=True)
        self.freq, self.label, self.stop_ev, self.pacer, self.proc, self.on = freq, label, threading.Event(), None, None, True
        self.toggles = []

    def _toggle(self):
        os.kill(self.proc.pid, signal.SIGSTOP if self.on else signal.SIGCONT); self.on = not self.on
        self.toggles.append((H.now_boot(), self.on))      # (Zeit, Last an?) fuer R50

    def _cpu(self):
        st = H.parse_task_stat(H.rd("/proc/%d/stat" % self.proc.pid))
        return (st["utime"] + st["stime"]) / H.TICK if st else float("nan")

    def run(self):
        # SCHED_IDLE: der Taktgeber (normale Prioritaet, gleiche CPU) verdraengt die Last sofort -> sauberes 50-%-Rechteck
        self.proc = subprocess.Popen(["taskset", "-c", str(H.CTL_CPU), "chrt", "-i", "0", "sha256sum", "/dev/zero"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.2)
        c0, w0 = self._cpu(), time.monotonic()
        self.pacer = Pacer(2 * self.freq, self._toggle, self.label); self.pacer.start()
        self.stop_ev.wait()
        self.pacer.stop_ev.set(); self.pacer.join(5)
        self.duty = (self._cpu() - c0) / (time.monotonic() - w0)   # gemessener Anteil "Last an" (Rechenzeit / Wandzeit)
        try:
            os.kill(self.proc.pid, signal.SIGCONT); self.proc.terminate(); self.proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()

    def stats(self):
        st = self.pacer.stats() if self.pacer else {"label": self.label, "soll_hz": 2 * self.freq, "ist_hz": 0.0, "n": 0,
                                                    "fehler": 0, "verzug_p50_ms": float("nan"), "verzug_p99_ms": float("nan"),
                                                    "verzug_max_ms": float("nan")}
        st["rechteck_hz"] = self.freq
        st["tastverhaeltnis"] = getattr(self, "duty", float("nan"))
        st["toggles"] = list(self.toggles)
        return st

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

def tune_burst(target_us, rate, rounds=3, probe=1.0):
    """Wartezeit der Schleife so waehlen, dass die GEMESSENE Rechenzeit je Aufruf (inkl. Python/Kern-Aufwand) target_us
    trifft. -> (busy_us, gemessen_us); gemessen > Soll, wenn schon der Mindestaufwand groesser ist."""
    busy, got = 0.0, float("nan")
    for _ in range(rounds):
        pc = Pacer(rate, cpu_burst(busy), "abgleich"); pc.start(); time.sleep(probe); pc.stop_ev.set(); pc.join(5)
        got = pc.stats()["cpu_us_je_aufruf"]
        if not math.isfinite(got):
            break
        busy = max(0.0, busy + (target_us - got))
    return busy, got


# ------------------------------------------------------------------ PMIC (R50)
PMIC_RE = re.compile(r"^\s*(\S+)_([AV])\s+\w+\(\d+\)=([-+0-9.eE]+)[AV]\s*$")

def parse_pmic(txt):
    """vcgencmd pmic_read_adc -> (Summe V*I der Schienen [W], EXT5V [V], {Schiene: W})"""
    a, v = {}, {}
    for ln in txt.splitlines():
        m = PMIC_RE.match(ln)
        if m:
            (a if m.group(2) == "A" else v)[m.group(1)] = float(m.group(3))
    rails = {k: a[k] * v[k] for k in a if k in v}
    return (math.fsum(rails.values()) if rails else float("nan")), v.get("EXT5V", float("nan")), rails

class PmicSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_ev, self.rows, self.err = threading.Event(), [], 0

    def run(self):
        nxt = time.monotonic()
        while not self.stop_ev.is_set():
            ta = H.now_boot(); out = H.sh(["vcgencmd", "pmic_read_adc"], timeout=5); tb = H.now_boot()
            p, u, rails = parse_pmic(out)
            if math.isfinite(p):
                self.rows.append({"t": (ta + tb) / 2, "dauer": tb - ta, "p": p, "ext5v": u,
                                  "core": rails.get("VDD_CORE", float("nan"))})
            else:
                self.err += 1
            nxt += PMIC_DT
            self.stop_ev.wait(max(0.0, nxt - time.monotonic()))


# ------------------------------------------------------------------ Diretta-Rechenzeit
def diretta_rt():
    """Summe Laufzeit [ns] aller Diretta-Threads + end0-IRQ-Threads + ksoftirqd (schedstat, sonst stat-Ticks)."""
    irqs = set()
    for ln in H.rd("/proc/interrupts").splitlines():
        n = ln.split(":", 1)[0].strip()
        if n.isdigit() and re.search(r"\b(%s|eth\d*)\b" % re.escape(END0), ln):
            irqs.add(n)
    tot, src, names = 0, set(), set()
    for pdir in glob.glob("/proc/[0-9]*"):
        cmd = H.rd(pdir + "/cmdline").replace("\0", " ").lower(); comm = H.rd(pdir + "/comm").strip().lower()
        isd = "diretta" in cmd or "diretta" in comm or "syncalsa" in comm
        for tdir in glob.glob(pdir + "/task/[0-9]*"):
            tc = H.rd(tdir + "/comm").strip()
            m = re.match(r"irq/(\d+)-", tc)
            if not (isd or (m and m.group(1) in irqs) or tc.startswith("ksoftirqd/")):
                continue
            ss = H.rd(tdir + "/schedstat").split()
            if ss and ss[0].isdigit():
                tot += int(ss[0]); src.add("schedstat")
            else:
                st = H.parse_task_stat(H.rd(tdir + "/stat"))
                if not st:
                    continue
                tot += int((st["utime"] + st["stime"]) * 1e9 / H.TICK); src.add("stat-Ticks")
            names.add(re.sub(r"\d+$", "#", tc))
    return {"ns": tot, "quelle": "+".join(sorted(src)) or "keine", "threads": sorted(names)[:40]}

def diretta_us(p_info, i_info, hz):
    """zusaetzliche Rechenzeit je Diretta-Zyklus bei Wiedergabe [us] = (Rate P50 - Rate I50) / Zyklusrate."""
    def rate(c):
        a, b = c["c0"].get("diretta"), c["c1"].get("diretta")
        return (b["ns"] - a["ns"]) / (c["c1"]["t"] - c["c0"]["t"]) if a and b and c["c1"]["t"] > c["c0"]["t"] else float("nan")
    rp, ri = rate(p_info), rate(i_info)
    return (rp - ri) / hz / 1000.0, rp / hz / 1000.0


# ------------------------------------------------------------------ Zaehler
def end0_rx_pps(dt):
    a = H.parse_netdev(H.rd("/proc/net/dev")).get(END0, {}).get("rxp", 0); t0 = time.monotonic()
    time.sleep(dt)
    b = H.parse_netdev(H.rd("/proc/net/dev")).get(END0, {}).get("rxp", 0)
    return (b - a) / (time.monotonic() - t0)

def setting_file():
    f = sorted(glob.glob("/opt/diretta*/setting.inf"))
    return (f[0], H.rd(f[0], None)) if f else (None, None)

def counters():
    d = diretta_rt()          # vor dem Zeitstempel: Suche dauert einige 10 ms, Messfenster beginnt danach
    return {"t": H.now_boot(), "stat": H.rd("/proc/stat"), "interrupts": H.rd("/proc/interrupts"),
            "softirqs": H.rd("/proc/softirqs"), "netdev": H.rd("/proc/net/dev"),
            "throttled": H.sh(["vcgencmd", "get_throttled"]), "diretta": d}

def net_rates(a, b):
    dt = b["t"] - a["t"]; na, nb = H.parse_netdev(a["netdev"]), H.parse_netdev(b["netdev"]); out = {}
    for k in nb:
        if k in na and k != "lo":
            out[k] = {x: (nb[k][x] - na[k][x]) / dt for x in ("rxp", "rxb", "txp", "txb")}
    return out


# ------------------------------------------------------------------ Versuchsreihe
class Series:
    def __init__(self, cdir, dur, with_music, kal_kurz=False, mode="voll"):
        self.dir, self.dur, self.music, self.kal_kurz, self.mode = cdir, dur, with_music, kal_kurz, mode
        self.raw = os.path.join(cdir, "raw"); os.makedirs(self.raw, exist_ok=True)
        self.children, self.gen, self.params, self.state = [], None, {}, None

    def playing(self):
        """Host: ALSA-Zustand. Target: end0-Empfangsrate (Diretta sendet nur bei Wiedergabe ~500 Pak/s); unabhaengig davon,
        ob der Target-Dienst das ALSA-Geraet zwischen Titeln offen haelt."""
        if self.mode != "target":
            return H.alsa_state()[0]
        return end0_rx_pps(0.5) > PLAY_PPS

    def wait_state(self, want, settle=None):
        txt = "Musik STARTEN (96 kHz, Wiederholung an)" if want else "Musik STOPPEN"
        t_end = time.monotonic() + WAIT_MAX; ann = 0
        while True:
            if self.playing() == want:
                settle = settle if settle is not None else (SETTLE_SAME if self.state == want else SETTLE_CHANGE)
                log("Zustand %s erkannt - %.0f s Beruhigung" % ("Wiedergabe" if want else "Ruhe", settle))
                t_s = time.monotonic() + settle; ok = True
                while time.monotonic() < t_s:
                    if self.playing() != want:
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

    def condition(self, tag, hz, want_play, gen=None, dur=None, pmic=False, settle=None):
        only = os.environ.get("VS_ONLY")              # nur fuer Tests: Teilmenge der Bedingungen
        if only and tag not in only.split(","):
            return None
        dur = dur or self.dur
        log("== %s: %s, %d Hz, %d s%s" % (tag, {True: "Wiedergabe", False: "Ruhe", None: "Musik egal"}[want_play], hz, dur,
                                         (", Last: " + gen[0]) if gen else ""))
        if want_play is not None:
            self.wait_state(want_play, settle)
        cd = os.path.join(self.dir, tag); os.makedirs(cd, exist_ok=True)
        info = {"tag": tag, "hz": hz, "dur": dur, "want_play": want_play, "alsa_start": H.alsa_state()[1]}
        if gen:
            self.gen = SquareGen(gen[1], gen[0]) if gen[2] == "rechteck" else Pacer(gen[1], gen[2], gen[0])
            self.gen.start()
            time.sleep(3.0)                                   # Last laeuft vor Messbeginn eingeschwungen
        c0 = counters()
        smp = H.Sampler(os.path.join(cd, "sampler.json")); smp.start()
        pm = PmicSampler() if pmic else None
        if pm:
            pm.start()
        outp = os.path.join(cd, "run_out.txt")
        env = dict(os.environ, EXT5V_CPU=H.LOGGER_CPU, EXT5V_OUT=self.raw)
        with open(outp, "w") as of:
            p = subprocess.Popen([os.path.join(H.EXT5V, "ext5v_run.sh"), str(hz), str(dur), "vs-" + tag], env=env,
                                 stdin=subprocess.DEVNULL, stdout=of, stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append(p)
        lpid, lstart = H.find_logger_start(p.pid)
        info.update({"logger_pid": lpid, "logger_start": lstart})
        if lstart is None:
            log("FEHLER ext5v_log-Start nicht erkannt")
        try:
            p.wait(timeout=dur + 120)
        except subprocess.TimeoutExpired:
            info["timeout"] = True; H.kill_group(p)
        self.children.remove(p)
        if pm:
            pm.stop_ev.set(); pm.join(10)
            info["pmic"] = pm.rows; info["pmic_err"] = pm.err
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
        log("%s beendet: logger_rc=%s%s" % (tag, p.returncode, ("  Last %.4g/%.4g Hz" % (
            info["gen"]["ist_hz"], info["gen"]["soll_hz"])) if "gen" in info else ""))
        return info

    def derive_target(self, play_info):
        """Target: Diretta-Zyklus = end0-EMPFANGSrate in P50 (keine Netz-Nachbildung moeglich: Sender ist der Host)."""
        e = (net_rates(play_info["c0"], play_info["c1"]) if play_info else {}).get(END0, {})
        ok = e.get("rxp", 0) > 50
        p = {"end0_hz": e["rxp"] if ok else 500.0, "end0_rahmen_b": int(e["rxb"] / e["rxp"]) if ok else 0,
             "cpu_hz": e["rxp"] if ok else 500.0, "end0_if": END0, "lan_if": None, "richtung": "rx",
             "quelle": "gemessen end0-Empfang (P50)" if ok else "Standardwert 500 (P50 ohne end0-Empfang!)"}
        self.params = p; self.save_params()
        log("Target: Diretta-Empfang end0 %.1f Pak/s x %d B Rahmen (%s)" % (p["end0_hz"], p["end0_rahmen_b"], p["quelle"]))

    def derive(self, play_info):
        """Nachbildungs-Parameter aus der gemessenen Wiedergabe (P50)."""
        nr = net_rates(play_info["c0"], play_info["c1"]) if play_info else {}
        def mtu(i):
            v = H.rd("/sys/class/net/%s/mtu" % i).strip()
            return int(v) if v.isdigit() else 1500
        e, l = nr.get(END0, {}), nr.get(LAN, {})
        p = {"quelle_end0": "gemessen (P50)" if e.get("txp", 0) > 50 else "Standardwert 505 (Messprotokoll 02.10.)",
             "quelle_lan": "gemessen RX (P50), nachgebildet als TX" if l.get("rxp", 0) > 50 else "Standardwert 361, als TX"}
        p["quelle"] = p["quelle_end0"] + " / " + p["quelle_lan"]
        p["end0_hz"] = e["txp"] if e.get("txp", 0) > 50 else 505.0
        eb = e["txb"] / e["txp"] if e.get("txp", 0) > 50 else 1514.0
        p["lan_hz"] = l["rxp"] if l.get("rxp", 0) > 50 else 361.0
        lb = l["rxb"] / l["rxp"] if l.get("rxp", 0) > 50 else 1514.0
        # /proc/net/dev zaehlt Ethernet-Rahmen ohne FCS: Nutzlast = Rahmen - 14 (Eth) - 20 (IPv4) - 8 (UDP)
        p["end0_payload"] = int(max(18, min(eb - 42, mtu(END0) - 28)))
        p["lan_payload"] = int(max(18, min(lb - 42, mtu(LAN) - 28)))
        p["cpu_hz"] = p["end0_hz"]; p["lan_if"] = LAN; p["end0_if"] = END0
        self.params = p
        H.wjson(os.path.join(self.dir, "params.json"), p)
        log("Nachbildung: end0 %.1f Pak/s x %d B, %s %.1f Pak/s x %d B, CPU-Wecker %.1f Hz (%s)" % (
            p["end0_hz"], p["end0_payload"], LAN, p["lan_hz"], p["lan_payload"], p["cpu_hz"], p["quelle"]))

    def save_params(self):
        H.wjson(os.path.join(self.dir, "params.json"), self.params)

    def mech_params(self, P, I):
        """C47/C47x: Soll-Rechenzeit je Weckvorgang aus P50-I50, Schleifen-Abgleich vor Ort."""
        p = self.params; hz = p["cpu_hz"]
        if not P or not I:
            p["diretta_us"] = float("nan"); return False
        extra, tot = diretta_us(P, I, hz)
        if H.TEST and os.environ.get("VS_TEST_DIRETTA_US"):          # nur Selbsttest ausserhalb des Pi
            extra = tot = float(os.environ["VS_TEST_DIRETTA_US"])
        p.update({"diretta_us": extra, "diretta_us_gesamt": tot, "diretta_quelle": P["c1"]["diretta"]["quelle"],
                  "diretta_threads": P["c1"]["diretta"]["threads"]})
        if not (math.isfinite(extra) and extra > 0):
            log("WARN  Diretta-Rechenzeit je Zyklus nicht bestimmbar (%s us) - C47 entfaellt" % fmt(extra)); self.save_params()
            return False
        ux = min(max(4 * extra, MECH_DUTY * 1e6 / hz), 0.15 * 1e6 / hz)
        p["c47_busy_us"], p["c47_cpu_abgleich_us"] = tune_burst(extra, hz)
        p["c47x_busy_us"], p["c47x_cpu_abgleich_us"] = tune_burst(ux, hz)
        p["c47x_soll_us"] = ux
        self.save_params()
        log("Mechanismus: Diretta +%.1f us Rechenzeit je Zyklus (gesamt %.1f us, %s); C47 Soll %.1f us (Abgleich %.1f), "
            "C47x Soll %.1f us (Abgleich %.1f)" % (extra, tot, p["diretta_quelle"], extra, p["c47_cpu_abgleich_us"], ux,
                                                   p["c47x_cpu_abgleich_us"]))
        return True

    def run_target(self):
        P = self.condition("P50", 50, True)
        self.condition("P47", 47, True)
        self.derive_target(P)
        p = self.params
        I = self.condition("I50", 50, False)
        self.condition("I47", 47, False)
        if self.mech_params(P, I):
            self.condition("C47", 47, False, ("CPU0-Wecker wie Diretta %.0f us" % p["diretta_us"], p["cpu_hz"],
                                              cpu_burst(p["c47_busy_us"])))
            self.condition("C47x", 47, False, ("CPU0-Wecker Positivkontrolle %.0f us" % p["c47x_soll_us"], p["cpu_hz"],
                                               cpu_burst(p["c47x_busy_us"])))
        self.condition("I50m", 50, False)
        for fk in KAL_F_TARGET:
            self.condition(ktag(fk), 50, False, ("Rechteck CPU0 %g Hz" % fk, fk, "rechteck"), dur=min(self.dur, KAL_DUR))
        self.condition("K213b", 47, False, ("Rechteck CPU0 %.0f Hz" % KAL_HI, KAL_HI, "rechteck"), dur=min(self.dur, KAL_DUR))
        self.condition("R50", 50, False, ("Laststufe CPU0 %g Hz" % R_FREQ, R_FREQ, "rechteck"), pmic=True)
        self.condition("I50b", 50, False, settle=SETTLE_CHANGE)

    def run(self):
        if self.mode == "widerstand":
            self.condition("R50", 50, None, ("Laststufe CPU0 %g Hz" % R_FREQ, R_FREQ, "rechteck"), pmic=True)
            return
        if self.mode == "target":
            return self.run_target()
        P = None
        if self.music:
            P = self.condition("P50", 50, True)
            self.condition("P47", 47, True)
        self.derive(P)
        p = self.params
        I = self.condition("I50", 50, False)
        self.condition("I47", 47, False)
        mech = self.music and self.mech_params(P, I)
        self.condition("E50", 50, False, ("end0-Verkehr", p["end0_hz"], udp_sender(END0_DST, p["end0_payload"])))
        self.condition("E47", 47, False, ("end0-Verkehr", p["end0_hz"], udp_sender(END0_DST, p["end0_payload"])))
        self.condition("U50", 50, False, (LAN + "-Verkehr", p["lan_hz"], udp_sender(LAN_DST, p["lan_payload"])))
        self.condition("C50", 50, False, ("CPU0-Wecker 50us", p["cpu_hz"], cpu_burst(50)))
        if mech:
            self.condition("C47", 47, False, ("CPU0-Wecker wie Diretta %.0f us" % p["diretta_us"], p["cpu_hz"],
                                              cpu_burst(p["c47_busy_us"])))
            self.condition("C47x", 47, False, ("CPU0-Wecker Positivkontrolle %.0f us" % p["c47x_soll_us"], p["cpu_hz"],
                                               cpu_burst(p["c47x_busy_us"])))
        self.condition("I50m", 50, False)                       # Drift-Klammer Mitte
        # Kalibrierung der Messkette: Frequenzgang mit identischer Rechtecklast
        for fk in (KAL_F if not self.kal_kurz else (KAL_LO, KAL_HI)):
            self.condition(ktag(fk), 50, False, ("Rechteck CPU0 %g Hz" % fk, fk, "rechteck"), dur=min(self.dur, KAL_DUR))
        self.condition("K213b", 47, False, ("Rechteck CPU0 %.0f Hz" % KAL_HI, KAL_HI, "rechteck"), dur=min(self.dur, KAL_DUR))
        self.condition("R50", 50, False, ("Laststufe CPU0 %g Hz" % R_FREQ, R_FREQ, "rechteck"), pmic=True)
        # nach Laststufe laenger beruhigen (03.10.: I50b nach 15 s Varianz x1,44 gegenueber I50)
        self.condition("I50b", 50, False, settle=SETTLE_CHANGE)

    def cleanup(self):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # zweites stop darf das Aufraeumen nicht unterbrechen
        if self.gen:
            self.gen.stop_ev.set(); self.gen.join(5)
        for p in list(self.children):
            H.kill_group(p)


def child_main(cdir, dur, music, lockfd, kal_kurz=False, mode="voll"):
    lock = os.fdopen(lockfd, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("== ABBRUCH: eine andere Messung laeuft bereits"); return 1
    try:
        os.sched_setaffinity(0, {H.CTL_CPU})
    except OSError as e:
        log("WARN  CPU-Bindung nicht moeglich: %s" % e)
    s = Series(cdir, dur, music, kal_kurz, mode)
    def on_term(*_):
        raise SystemExit("durch stop beendet")
    signal.signal(signal.SIGTERM, on_term)
    rc, done = 0, False
    try:
        pre = preflight(mode)
        H.wjson(os.path.join(cdir, "series.json"), {"version": VERSION, "hostmess": H.VERSION, "dur": dur,
                "music": music, "kal_kurz": kal_kurz, "modus": mode, "start": time.strftime("%Y-%m-%dT%H:%M:%S"), "host": os.uname().nodename,
                "kernel": os.uname().release, "pid": os.getpid(), "pre": pre,
                "env": {k: v for k, v in os.environ.items() if k.startswith(("VS_", "HOSTMESS_"))},
                "diretta_setting": setting_file()[1], "diretta_setting_datei": setting_file()[0], "test_mode": H.TEST})
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
        try:
            full, short = analyse(dst)
            for fn, txt in (("report.txt", full), ("kurz.txt", short)):
                with open(os.path.join(dst, fn), "w") as f:
                    f.write(txt)
            print(short, flush=True)
            log("== %s  Ergebnis: %s" % ("FERTIG" if done else "TEILERGEBNIS GESPEICHERT", dst))
            log("Kurzbericht: cat %s/kurz.txt" % dst)
        finally:
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

def preflight(mode="voll"):
    log("== Vorpruefung (%s)" % mode)
    errs = []
    model = H.rd("/proc/device-tree/model").replace("\0", "").strip()
    if not model.startswith("Raspberry Pi 5") and not H.TEST:
        errs.append("kein Raspberry Pi 5")
    for f in ("ext5v_run.sh", "ext5v_log"):
        if not os.access(os.path.join(H.EXT5V, f), os.X_OK):
            errs.append("fehlt: " + os.path.join(H.EXT5V, f))
    if H.other_logger_running():
        errs.append("ext5v_log laeuft bereits")
    for c in ("taskset", "chrt", "sha256sum"):
        if not shutil.which(c):
            errs.append("Befehl fehlt: " + c)
    hl = os.path.join(H.SHM, "lock")
    if os.path.exists(hl):
        try:
            with open(hl, "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            errs.append("hostmess.py-Messung laeuft gerade")
    p_, u_, rails_ = parse_pmic(H.sh(["vcgencmd", "pmic_read_adc"], timeout=5))
    if not (math.isfinite(p_) and "VDD_CORE" in rails_ and math.isfinite(u_)):
        errs.append("vcgencmd pmic_read_adc liefert keine Schienenstroeme (VDD_CORE/EXT5V)")
    if mode == "target":
        if END0 not in H.parse_netdev(H.rd("/proc/net/dev")) and not H.TEST:
            errs.append("Netzwerkschnittstelle fehlt: " + END0)
    if mode == "voll":
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
    log("OK    %s, PMIC %.2f W / EXT5V %.3f V%s" % (model or "?", p_, u_, (", Schnittstellen %s/%s, Ziele %s/%s" % (
        END0, LAN, END0_DST, LAN_DST)) if mode == "voll" else ""))
    return {"model": model, "pmic_w": p_}


# ------------------------------------------------------------------ Auswertung
def ktag(f):
    return "K%d" % int(f)        # 3,1 -> K3, 7,3 -> K7, 31 -> K31, 213 -> K213

KTAGS = [ktag(f) for f in KAL_F]
TAGS = ["P50", "P47", "I50", "I47", "E50", "E47", "U50", "C50", "C47", "C47x", "I50m"] + KTAGS + ["K213b", "R50", "I50b"]
NEW13 = ("C47", "C47x", "R50")                     # erst ab v1.3
IDLE_REF = ("I50", "I50m", "I50b")

def temp_of(c):
    t = [r["temp"] for r in c.get("rows", []) if math.isfinite(r.get("temp", float("nan")))]
    return sum(t) / len(t) if t else float("nan")

def load_cond(cdir, tag):
    """Bedingung laden; None wenn nicht vorhanden. Defekte Dateien -> {'defekt': Grund}."""
    pth = os.path.join(cdir, tag, "cond.json")
    if not os.path.isfile(pth):
        return None
    try:
        info = H.rjson(pth)
        rdir = os.path.join(cdir, info["run_dir"]) if info.get("run_dir") else None
        volt = H.load_voltage(rdir, 0.0) if rdir else []
        smp = H.rjson(os.path.join(cdir, tag, "sampler.json"))["rows"]
    except (OSError, KeyError, ValueError) as e:
        return {"tag": tag, "defekt": str(e)}
    t = [a for a, _ in volt]; v = [b * 1000.0 for _, b in volt]          # mV
    fs = (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else float("nan")
    dts = [b - a for a, b in zip(t, t[1:])]
    al = [r["alsa"] for r in smp]
    info.update({"v": v, "t": t, "fs": fs, "dt_max": max(dts) if dts else float("nan"),
                 "dt_gaps": sum(1 for d in dts if d > 1.5 / info["hz"]),
                 "alsa_frac": 100.0 * sum(al) / len(al) if al else float("nan"),
                 "rates": sorted({r["rate"] for r in smp if r.get("rate")}), "rows": smp})
    return info

def metrics(c):
    v = c["v"]; s = sorted(v)
    m = {"n": len(v), "fs": c["fs"]}
    if len(v) < 2 or not math.isfinite(c["fs"]):
        return m
    m.update({"mean": H.mean(v), "sd": H.sdev(v), "p01": pct(s, 0.1), "p999": pct(s, 99.9), "min": s[0], "max": s[-1]})
    f, p = welch(v, c["fs"])
    m["f"], m["psd"], m["v"] = f, p, v
    m["df"] = f[1] - f[0] if len(f) > 1 else float("nan")
    m["bands"] = [band_rms(f, p, lo, hi) for lo, hi in BANDS]
    m["var_hf"] = band_rms(f, p, VAR_FMIN, 1e9) ** 2 if f else float("nan")   # Varianz ohne Drift < 0,1 Hz [mV^2]
    sv = seg_var(v, c["fs"])
    m["var_hf_se"] = H.sdev(sv) / math.sqrt(max(1, len(sv) / 2)) if len(sv) > 2 else float("nan")  # 50 % Ueberlappung
    nb = sorted(x for fk, x in zip(f, p) if 2.0 <= fk <= 20.0)
    m["det_rms"] = math.sqrt(15 * nb[len(nb) // 2] * m["df"]) if nb else float("nan")   # kleinste nachweisbare Spitze
    m["peaks"] = peaks(f, p)
    m["adev"] = adev(v, c["fs"])
    a, b = c["c0"], c["c1"]; dt = b["t"] - a["t"]
    sa, sb = H.parse_stat(a["stat"]), H.parse_stat(b["stat"])
    m["busy"] = {k: 100.0 * (H.cpu_busy(sb["cpus"][k]) - H.cpu_busy(sa["cpus"][k])) /
                 max(1, H.cpu_total(sb["cpus"][k]) - H.cpu_total(sa["cpus"][k])) for k in sorted(sb["cpus"]) if k in sa["cpus"]}
    m["ctxt"] = (sb.get("ctxt", 0) - sa.get("ctxt", 0)) / dt; m["intr"] = (sb.get("intr", 0) - sa.get("intr", 0)) / dt
    m["net"] = net_rates(a, b); m["cdt"] = dt
    n, ia = H.parse_table(a["interrupts"]); _, ib = H.parse_table(b["interrupts"]); rows = []
    for k, (cb, desc) in ib.items():
        d = [y - x for x, y in zip(ia.get(k, ([0] * n, ""))[0], cb)]
        if sum(d) > 0:
            rows.append((sum(d) / dt, k, desc.split()[-1] if desc else "", [x / dt for x in d]))
    m["irq"] = sorted(rows, reverse=True)[:5]
    ta, tb = H.thr_val(a.get("throttled")), H.thr_val(b.get("throttled"))
    m["thr_ok"] = ta is not None and tb is not None and not (ta & 0xF) and not (tb & 0xF) and not (tb & ~ta & 0xF0000)
    return m

def checks_for(tag, c, m, params):
    if "defekt" in c:
        return [("%s: Daten lesbar (%s)" % (tag, c["defekt"]), False)]
    out = [("%s: logger_rc=0" % tag, c.get("rc") == 0 and not c.get("timeout") and c.get("logger_start") is not None)]
    if "pmic" not in c:
        out += [("%s: Daten vollstaendig (N=%d, Soll %d)" % (tag, m["n"], c["dur"] * c["hz"]), m["n"] >= 0.98 * c["dur"] * c["hz"]),
                ("%s: Abtastrate %.3f Hz (Soll %d)" % (tag, m["fs"], c["hz"]), abs(m["fs"] - c["hz"]) < 0.01 * c["hz"]),
                ("%s: Abtastung gleichmaessig (max dt %s ms, Luecken %d)" % (tag, fmt(1000 * c["dt_max"], 1), c["dt_gaps"]),
                 c["dt_gaps"] == 0)]
    else:
        # R50: jede PMIC-Abfrage blockiert den Logger kurz (gemeinsame Firmware-Schnittstelle, gemessen 03.10.: 1 Luecke
        # je Abfrage, ~40 ms). Fuer Halbperioden-Mittel unschaedlich; geprueft wird, dass es nur daran liegt.
        npm = len(c.get("pmic") or [])
        out += [("%s: Daten ausreichend fuer Halbperioden-Mittel (N=%d, Soll %d, >= 90 %%)" % (tag, m["n"], c["dur"] * c["hz"]),
                 m["n"] >= 0.90 * c["dur"] * c["hz"]),
                ("%s: Luecken nur durch PMIC-Abfrage (%d Luecken bei %d Abfragen, max dt %s ms < 100)" % (
                    tag, c["dt_gaps"], npm, fmt(1000 * c["dt_max"], 1)), c["dt_gaps"] <= npm + 3 and c["dt_max"] < 0.1)]
    out += [
           (("%s: Wiedergabe %s (ist %.0f %%%s)" % (tag, "an" if c["want_play"] else "aus", c["alsa_frac"],
                                                   ", aus end0-Paketrate, Toleranz 3 %" if c.get("modus") == "target" else ""),
             abs(c["alsa_frac"] - (100.0 if c["want_play"] else 0.0)) <= (3.0 if c.get("modus") == "target" else 1e-9))
            if c["want_play"] is not None else
            ("%s: Musikzustand waehrend der Messung konstant (Wiedergabe %.0f %%)" % (tag, c["alsa_frac"]),
             c["alsa_frac"] in (0.0, 100.0))),
            ("%s: keine Unterspannung/Drosselung" % tag, m.get("thr_ok", False)),
            ("%s: Sampler ohne Fehler" % tag, c.get("sampler_err") is None)]
    if (c["want_play"] or (c["want_play"] is None and c["alsa_frac"] == 100.0)) and not (c.get("modus") == "target" and not c["rates"]):
        out.append(("%s: Abtastrate Musik konstant %s" % (tag, c["rates"]), len(c["rates"]) == 1))
    g = c.get("gen")
    if g:
        dt_ = g.get("dauer_s") or (g["n"] / g["ist_hz"] if g["ist_hz"] > 0 else 0.0)
        soll_n = g["soll_hz"] * dt_          # Anzahl statt Rate: bei 0,1 Hz zaehlt der erste Aufruf sonst 5 % mit
        out.append(("%s: Last %.1f von %.1f Hz (%d von %.1f Aufrufen), Verzug p99 %.2f ms, >1 ms %.1f %%, Fehler %d" % (
                    tag, g["ist_hz"], g["soll_hz"], g["n"], soll_n, g["verzug_p99_ms"], g.get("anteil_ueber_1ms", float("nan")),
                    g["fehler"]),
                    abs(g["n"] - soll_n) <= max(1.5, 0.02 * soll_n) and g["fehler"] == 0 and g["verzug_p99_ms"] < (
                        0.25 * 1000.0 / g["soll_hz"] if "rechteck_hz" in g else 2.0)))   # Rechteck: <25 % der Halbperiode
    if "pmic" in c:
        n_exp = c["dur"] / PMIC_DT
        out.append(("%s: PMIC-Abfragen %d (Soll ~%.0f), Fehler %d" % (tag, len(c["pmic"]), n_exp, c.get("pmic_err", 0)),
                    len(c["pmic"]) >= 0.6 * n_exp and c.get("pmic_err", 0) == 0))
        iface = {"E": params.get("end0_if", END0), "U": params.get("lan_if", LAN)}.get(tag[0])
        if iface and "net" in m:
            base = 0.0
            tx = m["net"].get(iface, {}).get("txp", 0.0)
            out.append(("%s: Verkehr verlaesst %s wirklich (tx %.1f Pak/s, erzeugt %.1f/s)" % (tag, iface, tx, g["ist_hz"]),
                        tx - base >= 0.95 * g["ist_hz"]))
    return out

def fmt(x, nd=2):
    return "n/a" if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else "%.*f" % (nd, x)

def testable(fa, m):
    return math.isfinite(m.get("df", float("nan"))) and 2 * m["df"] <= fa <= m["fs"] / 2 - 2 * m["df"]

def sig_peaks(m, ref):
    """signifikante Spitzen: >= 10x Umgebung und >= 4x Ruhe gleicher Rate."""
    out = []
    for fk, loc, rms in peaks(m.get("f") or [], m.get("psd") or [], top=30, ratio=10.0):
        rr, _ = excess(m, ref, fk, 0.05)
        if rr >= 4:
            out.append((fk, loc, rms, rr))
    return out

def source_search(M, f0, span=25.0, step=0.01):
    """Quellfrequenz nahe f0, deren Alias in P50 UND P47 je auf eine signifikante Spitze faellt."""
    p50, p47 = M.get("P50"), M.get("P47")
    if not p50 or not p47 or "f" not in p50 or "f" not in p47:
        return []
    s50, s47 = sig_peaks(p50, M.get("I50")), sig_peaks(p47, M.get("I47"))
    if not s47:
        return []
    hits = []
    n = int(2 * span / step)
    for i in range(n + 1):
        fq = f0 - span + i * step
        a50, a47 = alias(fq, p50["fs"]), alias(fq, p47["fs"])
        k50 = [pk for pk in s50 if abs(pk[0] - a50) <= p50["df"]]
        k47 = [pk for pk in s47 if abs(pk[0] - a47) <= p47["df"]]
        blind50 = not testable(a50, p50)        # Quelle ~Vielfaches von fs: bei 50 Hz unsichtbar
        if k47 and (k50 or blind50):
            hits.append((fq, a50, a47, k50[0][2] if k50 else float("nan"), k47[0][2]))
    # zusammenhaengende Treffer zu Intervallen buendeln
    out = []
    for h in hits:
        if out and h[0] - out[-1][-1][0] <= 2 * step:
            out[-1].append(h)
        else:
            out.append([h])
    return [(g[0][0], g[-1][0], g[len(g) // 2]) for g in out]

def r_analysis(c):
    """R50: Spannungs- und Leistungshub je Lastwechsel. Jede Last-an-Halbperiode gegen das Mittel ihrer beiden
    Nachbarn (Last aus) -> lineare Drift faellt heraus. Ergebnis None, wenn Grunddaten fehlen."""
    g = c.get("gen") or {}; tog = g.get("toggles") or []; rows = c.get("pmic") or []
    t0 = c.get("logger_start"); v = c.get("v") or []
    if len(tog) < 4 or not rows or not v or t0 is None or not math.isfinite(c.get("fs", float("nan"))):
        return None
    t = [t0 + x for x in c["t"]]                 # Logger-Zeit -> CLOCK_BOOTTIME (wie Umschalt- und PMIC-Zeiten)
    halves = []
    for (ta, on), (tb, _) in zip(tog, tog[1:]):
        a, b = ta + R_SKIP, tb - 0.2
        vs = [x for tt, x in zip(t, v) if a <= tt < b]
        rs = [r for r in rows if a <= r["t"] < b]
        if b <= a or len(vs) < 0.8 * (b - a) * c["fs"] or len(rs) < 3:
            halves.append(None); continue
        halves.append((on, H.mean(vs), H.mean([r["p"] for r in rs]), H.mean([r["ext5v"] for r in rs])))
    dv, dp = [], []
    for i in range(1, len(halves) - 1):
        h, a, b = halves[i], halves[i - 1], halves[i + 1]
        if h and a and b and h[0] and not a[0] and not b[0]:
            dv.append((a[1] + b[1]) / 2 - h[1]); dp.append(h[2] - (a[2] + b[2]) / 2)
    out = {"n": len(dv)}
    if len(dv) < 2:
        return out
    se = lambda x: H.sdev(x) / math.sqrt(len(x))
    vin = H.mean([h[3] for h in halves if h])
    dV, dP = H.mean(dv), H.mean(dp)
    out.update({"dv": dV, "dv_se": se(dv), "dp": dP, "dp_se": se(dp), "vin": vin,
                "r": {e: (dV / 1000.0) / (dP / (e * vin)) if dP > 0 and vin > 0 else float("nan") for e in ETA}})
    return out

def line_at(M, t, fsrc):
    """Linie einer Quelle fsrc im Spektrum der Bedingung t: Alias, rms, Faktor ggue. Ruhe gleicher Rate, signifikant?"""
    m = M.get(t)
    if not m or "f" not in m or not math.isfinite(fsrc):
        return None
    fa = alias(fsrc, m["fs"])
    if not testable(fa, m):
        return {"fa": fa, "a": float("nan"), "rr": float("nan"), "det": m.get("det_rms", float("nan")), "se": float("nan"),
                "sig": False}
    a = peak_rms(m, fa); rr, _ = excess(m, M.get("I47" if "47" in t else "I50"), fa, 0.15)
    det, se = m.get("det_rms", float("nan")), line_se(m, fa)
    # signifikant: >= 4x Ruhe UND >= 3 Standardfehler (Bloecke). Nachweisgrenze det nur ohne Blockfehler: bei
    # linienreichen Spektren (Pulsfolgen falten viele Oberwellen ein) liegt deren Median nicht mehr am Rauschen.
    strong = a >= 3 * se if math.isfinite(se) else (math.isfinite(det) and a >= det)
    return {"fa": fa, "a": a, "rr": rr, "det": det, "se": se, "sig": rr >= 4 and math.isfinite(a) and strong}

def line_se(m, fa, nblk=4):
    """Standardfehler einer Linien-rms aus nblk unabhaengigen Zeitbloecken (empirisch, kein Rauschmodell)."""
    v = m.get("v"); n = len(v) // nblk if v else 0
    if n < 2 * NSEG:
        return float("nan")
    vals = []
    for b in range(nblk):
        f, p = welch(v[b * n:(b + 1) * n], m["fs"]); vals.append(peak_rms({"f": f, "psd": p}, fa))
    return H.sdev(vals) / math.sqrt(nblk)

def spin(us, f):
    """Grundwellen-Gewicht einer Pulsfolge: Pulsdauer us bei Rate f -> sin(pi*d) (Amplitude ~ Hoehe * sin(pi*d))."""
    return math.sin(math.pi * min(0.5, max(0.0, us * 1e-6 * f)))

def mechanism(M, C, params):
    """Anteil des Prozessor-Aufwachens an der Diretta-500-Hz-Linie (P47), skaliert ueber die Positivkontrolle C47x."""
    ut, fz = params.get("diretta_us", float("nan")), params.get("end0_hz", float("nan"))
    if not (math.isfinite(ut) and ut > 0) or not all(t in C and C[t].get("gen") for t in ("C47", "C47x")):
        return None
    g1, gx = C["C47"]["gen"], C["C47x"]["gen"]
    r = {"ut": ut, "fz": fz, "u1": g1["cpu_us_je_aufruf"], "ux": gx["cpu_us_je_aufruf"], "f1": g1["ist_hz"], "fx": gx["ist_hz"],
         "P": line_at(M, "P47", fz), "P2": line_at(M, "P47", 2 * fz), "L1": line_at(M, "C47", g1["ist_hz"]),
         "LX": line_at(M, "C47x", gx["ist_hz"]), "LX2": line_at(M, "C47x", 2 * gx["ist_hz"])}
    if not (r["P"] and r["LX"] and r["L1"]) or not all(math.isfinite(r[k]) and r[k] > 0 for k in ("u1", "ux")):
        return r
    k = r["LX"]["a"] / spin(r["ux"], r["fx"])          # mV rms je Einheit sin(pi*d) bei voller Kernlast-Hoehe
    r["pred"] = k * spin(ut, fz)                        # erwartete Linie, wenn nur Diretta-Rechenzeit wirkte
    r["pred1"] = k * spin(r["u1"], r["f1"])             # erwartete Linie in C47 (Linearitaetsprobe)
    r["ratio"] = r["pred"] / r["P"]["a"] if r["P"]["sig"] and r["P"]["a"] > 0 else float("nan")
    rel = lambda x: x["se"] / x["a"] if x["a"] > 0 else float("nan")
    r["ratio_se"] = abs(r["ratio"]) * math.hypot(rel(r["LX"]), rel(r["P"]))
    if "E47" in C and C["E47"].get("gen"):
        ge = C["E47"]["gen"]; le = line_at(M, "E47", ge["ist_hz"])
        ue = ge.get("cpu_us_je_aufruf", float("nan"))
        if le and math.isfinite(ue):
            r.update({"LE": le, "ue": ue, "pred_e_cpu": k * spin(ue, ge["ist_hz"])})
    return r

def analyse(cdir):
    L, S, checks = [], [], []
    try:
        meta = H.rjson(os.path.join(cdir, "series.json"))
    except OSError:
        t = "keine Versuchsreihe in %s\n" % cdir; return t, t
    hd = "vschwank %s  host=%s  start=%s  Dauer je Bedingung %d s%s" % (meta["version"], meta["host"], meta["start"],
         meta["dur"], "  TESTMODUS" if meta.get("test_mode") else "")
    L.append(hd); S.append(hd)
    pp = os.path.join(cdir, "params.json")
    params = H.rjson(pp) if os.path.isfile(pp) else {}
    lan = params.get("lan_if", LAN)
    C, M = {}, {}
    mode = meta.get("modus", "voll")
    v13 = tuple(int(x) for x in str(meta.get("version", "0")).split(".")[:2]) >= (1, 3)
    if mode == "widerstand":
        soll = ["R50"]
    elif mode == "target":
        soll = ["P50", "P47", "I50", "I47", "C47", "C47x", "I50m"] + [ktag(f) for f in KAL_F_TARGET] + ["K213b", "R50", "I50b"]
    else:
        soll = [t for t in TAGS
                if (meta.get("music") or t not in ("P50", "P47", "C47", "C47x"))
                and (v13 or t not in NEW13)
                and not (meta.get("kal_kurz") and t in KTAGS and t not in (ktag(KAL_LO), ktag(KAL_HI)))]
    for t in soll:
        c = load_cond(cdir, t)
        if c is None:
            checks.append(("%s: Bedingung vorhanden" % t, False)); continue
        if mode == "target" and "defekt" not in c:
            # Wiedergabe-Anteil aus der end0-Paketrate (Sampler, 1 s), nicht aus ALSA
            r_ = c["rows"]; fl = []
            for a_, b_ in zip(r_, r_[1:]):
                dt_ = b_["t"] - a_["t"]
                if dt_ > 0 and END0 in a_.get("np", {}) and END0 in b_.get("np", {}):
                    fl.append((b_["np"][END0] - a_["np"][END0]) / dt_ > PLAY_PPS)
            c["alsa_frac"] = 100.0 * sum(fl) / len(fl) if fl else float("nan"); c["modus"] = "target"
        C[t] = c
        M[t] = metrics(c) if "defekt" not in c else {}
        checks += checks_for(t, c, M[t], params)
    if not meta.get("music") and mode != "widerstand":
        S.append("HINWEIS: ohne Musik gemessen - kein P50/P47, Alias-Nachweis und Varianzanteile nicht moeglich.")
        L.append(S[-1])
    if params and lan:
        S.append("Nachbildung: end0 %.1f Pak/s x %d B | %s %.1f Pak/s x %d B (TX-Ersatz fuer RX) | CPU %.1f Hz | %s" % (
            params["end0_hz"], params["end0_payload"], lan, params["lan_hz"], params["lan_payload"], params["cpu_hz"],
            params.get("quelle", "")))
        L.append(S[-1])
    elif params:
        S.append("Target: Diretta-Empfang end0 %.1f Pak/s x %d B Rahmen (%s); Musikzustand aus end0-Paketrate (> %.0f Pak/s)" % (
            params["end0_hz"], params.get("end0_rahmen_b", 0), params.get("quelle", ""), PLAY_PPS))
        L.append(S[-1])
    hdr = "Bed.  fs[Hz]  mean[V]  sd[mV] var>0,1Hz[mV2] mean-P0,1[mV] | rms 0,1-0,5 0,5-2 2-10 >10 Hz [mV] | ADEV 0,04/0,64/2,56 s [mV]"
    L.append(""); L.append(hdr); S.append(""); S.append(hdr)
    for t in TAGS:
        m = M.get(t)
        if not m or "sd" not in m:
            continue
        ad = m["adev"]
        line = "%-5s %6.2f %8.4f %6.2f %8.2f %12.2f    | %s | %s" % (
            t, m["fs"], m["mean"] / 1000, m["sd"], m["var_hf"], m["mean"] - m["p01"],
            " ".join("%5s" % fmt(b) for b in m["bands"]), "/".join(fmt(ad.get(x)) for x in (0.04, 0.64, 2.56)))
        L.append(line); S.append(line)
    L.append(""); L.append("Signifikante Spitzen (>=10x Umgebung, >=4x Ruhe gleicher Rate): Frequenz Hz / x Umgebung / rms mV / x Ruhe")
    S.append(""); S.append("Signifikante Spitzen (f Hz/xUmg/rms mV/xRuhe):")
    for t in TAGS:
        m = M.get(t)
        if not m or "f" not in m or t.startswith("I"):
            continue
        ref = M.get("I47" if t.endswith("47") or t == "K213b" else "I50")
        sp = sig_peaks(m, ref)[:4]
        txt = "; ".join("%.2f/%.0f/%.2f/%.0f" % pk for pk in sp) or "keine"
        L.append("  %-5s %s" % (t, txt)); S.append("  %-5s %s" % (t, txt))
    if mode != "widerstand":
        # Alias-Test
        L.append(""); S.append("")
        L.append("Alias-Test (Vorhersage aus gemessener Abtastrate; Faktor ggue. Ruhe/Umgebung; SPITZE wenn >=4 und >=10;"
                 " 'n.p.' = Alias liegt bei 0 Hz oder Nyquist, nicht pruefbar):"); S.append("Alias-Test:")
        cands = []
        if params:
            fz = params["end0_hz"]
            cands = [("Diretta-Zyklus", fz, ("P50", "P47")), ("2x Zyklus", 2 * fz, ("P50", "P47")),
                     ("1/2 Zyklus", fz / 2, ("P50", "P47"))] + ([(lan + "-Pakete", params["lan_hz"], ("P50", "P47"))] if lan else [])
        cands += [("Netz 50 Hz", 50.0, ("I47", "P47")), ("Netz 100 Hz", 100.0, ("I47", "P47")), ("Netz 150 Hz", 150.0, ("I47", "P47")),
                  ("60 Hz", 60.0, ("I50", "I47", "P50", "P47")), ("120 Hz", 120.0, ("I50", "I47", "P50", "P47"))]
        for t, g_ in (("E50", "E"), ("E47", "E"), ("U50", "U"), ("C50", "C")):
            if t in C and C[t].get("gen"):
                cands.append(("%s erzeugt" % t, C[t]["gen"]["ist_hz"], (t,)))
        for name, fsrc, conds in cands:
            res = []
            for t in conds:
                m = M.get(t)
                if not m or "f" not in m:
                    continue
                fa = alias(fsrc, m["fs"])
                if not testable(fa, m):
                    res.append("%s %.2f Hz n.p." % (t, fa)); continue
                ref_t = "I47" if t.endswith("47") else "I50"
                if t == ref_t:      # Ruhe gegen sich selbst nicht vergleichbar: nur Umgebung
                    _, loc = excess(m, m, fa)
                    res.append("%s %.2f Hz -/%s%s" % (t, fa, fmt(loc, 0), " SPITZE(Umgebung)" if loc >= 10 else ""))
                    continue
                rr, loc = excess(m, M.get(ref_t), fa)
                res.append("%s %.2f Hz %s/%s%s" % (t, fa, fmt(rr, 0), fmt(loc, 0), " SPITZE" if rr >= 4 and loc >= 10 else ""))
            line = "  %-15s %7.2f Hz -> %s" % (name, fsrc, " | ".join(res) or "keine Daten")
            L.append(line); S.append(line)
        if params:
            for name, f0 in [("Diretta-Zyklus", params["end0_hz"])] + ([(lan + "-Pakete", params["lan_hz"])] if lan else []):
                ivs = source_search(M, f0)
                fit_ = kal_fit(M, C)[1]
                def corr(fq, rms):
                    if not fit_:
                        return ""
                    hq = sinc_h(fq, fit_[0])
                    return ", korrigiert %s mV (H=%.2f, Modell)" % (fmt(rms / hq), hq) if hq >= 0.2 else ", H=%.2f zu klein fuer Korrektur" % hq
                line = "  Quellsuche %s +-25 Hz (Spitze in P47 und P50, oder P50 blind): %s" % (name, "; ".join(
                    "%.2f-%.2f Hz (Alias %.2f/%.2f Hz, rms %s/%.2f mV%s%s)" % (a, b, h[1], h[2], fmt(h[3]), h[4],
                    ", P50 blind" if math.isnan(h[3]) else "", corr(h[0], h[4])) for a, b, h in ivs) or "kein Treffer")
                L.append(line); S.append(line)
    # Varianzanteile (ohne Drift < 0,1 Hz)
    if all(t in M and "var_hf" in M[t] for t in ("I50", "P50")):
        vi, vp = M["I50"]["var_hf"], M["P50"]["var_hf"]
        L.append(""); S.append("")
        ui, up = M["I50"]["var_hf_se"], M["P50"]["var_hf_se"]
        head = "Zusatzvarianz >0,1 Hz Wiedergabe (P50-I50): %.2f +- %.2f mV^2 (%.2f -> %.2f; +- = 1 Standardfehler)" % (
            vp - vi, math.hypot(ui, up), vi, vp)
        L.append(head); S.append(head)
        rows = [("E50", "end0-Verkehr", None), ("U50", str(lan) + "-TX-Ersatz", None), ("C50", "CPU-Wecker", None),
                ("E50", "end0 ohne CPU-Anteil", "C50")]
        for t, lab, minus in rows:
            if t in M and "var_hf" in M[t] and vp > vi and (minus is None or minus in M):
                ref, uref = (M[minus]["var_hf"], M[minus]["var_hf_se"]) if minus else (vi, ui)
                ex = M[t]["var_hf"] - ref; uex = math.hypot(M[t]["var_hf_se"], uref)
                line = "  %-21s %6.2f +- %4.2f mV^2 = %5.0f +- %3.0f %% der Wiedergabe-Zusatzvarianz%s" % (
                    lab, ex, uex, 100 * ex / (vp - vi), 100 * uex / (vp - vi), "  (E50 - C50)" if minus else "")
                L.append(line); S.append(line)
    # Kalibrierung 1: Frequenzgang der Messkette (identische Rechtecklast, verschiedene Frequenzen)
    krow, fit = kal_fit(M, C)
    if krow:
        L.append(""); S.append("")
        hd = "Kalibrierung 1 - Frequenzgang Messkette (Rechtecklast CPU0, Grundwellen-rms am Alias, auf 50 % Tastverhaeltnis korrigiert):"
        L.append(hd); S.append(hd)
        a_ref = fit[1] if fit else (krow[0][3] if krow else float("nan"))
        for t, fk, fa, a, rr, ok, d in krow:
            line = "  %-5s %6.1f Hz -> Alias %5.2f Hz  rms %s mV (Tastv. %s %%)  x%s ggue. Ruhe  H=%s%s" % (
                t, fk, fa, fmt(a), fmt(100 * d, 0), fmt(rr, 0), fmt(a / a_ref if a_ref > 0 else float("nan")),
                "" if ok else "  (nicht signifikant, nicht im Fit)")
            L.append(line); S.append(line)
        if "K213b" in M and "f" in M["K213b"]:
            fa = alias(KAL_HI, M["K213b"]["fs"]); a = peak_rms(M["K213b"], fa)
            line = "  K213b %6.1f Hz @47 Hz -> Alias %5.2f Hz rms %s mV  H=%s (Gegenprobe zu K213)" % (
                KAL_HI, fa, fmt(a), fmt(a / a_ref if a_ref > 0 else float("nan")))
            L.append(line); S.append(line)
        if fit:
            T, a0, rel = fit
            line = "  Modell Mittelungsfenster T = %.2f ms (Restfehler %.0f %%)%s; H(500 Hz) = %.2f, H(1000 Hz) = %.2f (Modell, oberhalb 313 Hz extrapoliert)" % (
                1000 * T, 100 * rel, " - MODELL PASST SCHLECHT" if rel > 0.15 else "", sinc_h(500, T), sinc_h(1000, T))
            L.append(line); S.append(line)
            checks.append(("Kalibrierung: Frequenzgang-Modell passt (Restfehler %.0f %% <= 15 %%)" % (100 * rel), rel <= 0.15))
            # Kalibrierung 2: Lastempfindlichkeit aus der Grundwelle (Rechteck-Hub = Absenkung je Kern)
            hub = a0 / SQ_FUND
            line = "  Kalibrierung 2 - Lastempfindlichkeit: Hub %.1f mV je voll belastetem Kern (Gegenprobe hostmess-Laststufe 25,2 mV)" % hub
            L.append(line); S.append(line)
        else:
            line = "  Frequenzgang-Modell nicht bestimmbar (< 3 signifikante Stuetzstellen)"
            L.append(line); S.append(line)
        for t in ("P50", "I50"):
            if t in M and "det_rms" in M[t]:
                line = "  Nachweisgrenze Spitze 2-20 Hz %s: %s mV rms" % (t, fmt(M[t]["det_rms"]))
                L.append(line); S.append(line)
    # Kalibrierung 3: Drift-Klammerung (Ruhe am Anfang/Mitte/Ende)
    if sum(1 for t in IDLE_REF if t in M and "mean" in M[t]) >= 2:
        L.append(""); S.append("")
        hd = "Kalibrierung 3 - Drift: Ruhe-Referenzen " + ", ".join("%s %.3f mV (%s C)" % (t, M[t]["mean"], fmt(temp_of(C[t]), 1))
                                                                   for t in IDLE_REF if t in M and "mean" in M[t])
        L.append(hd); S.append(hd)
        line = "  Mittel relativ zur driftkorrigierten Ruhe [mV]: " + "; ".join(
            "%s %+.2f%s" % (t, M[t]["mean"] - b, "" if how == "interpoliert" else "*")
            for t in TAGS if t in M and "mean" in M[t] and t not in IDLE_REF
            for b, how in [baseline(M, C, (C[t]["c0"]["t"] + C[t]["c1"]["t"]) / 2)]) + "  (* = nicht interpoliert)"
        L.append(line); S.append(line)
    if all(t in M and "mean" in M[t] for t in ("I50", "I50b")):
        tm = lambda t: (C[t]["c0"]["t"] + C[t]["c1"]["t"]) / 2
        dmean = M["I50b"]["mean"] - M["I50"]["mean"]
        rate = 60.0 * dmean / (tm("I50b") - tm("I50")) if tm("I50b") > tm("I50") else float("nan")
        if "I50m" in M and "mean" in M["I50m"]:
            lin = M["I50"]["mean"] + dmean * (tm("I50m") - tm("I50")) / (tm("I50b") - tm("I50"))
            dev = M["I50m"]["mean"] - lin
            checks.append(("Drift gleichmaessig -> Korrektur gueltig (Drift %+.3f mV/min, I50m weicht %+.2f mV von der Geraden ab, <= 1,5 mV)" % (
                rate, dev), abs(dev) <= 1.5))
        rv = M["I50b"]["var_hf"] / M["I50"]["var_hf"] if M["I50"]["var_hf"] > 0 else float("nan")
        checks.append(("Ruhe-Schwankung reproduzierbar (Varianz I50b/I50 x%.2f, 0,75..1,33)" % rv, 0.75 <= rv <= 1.33))
    # Mechanismus: Prozessor oder Netzwerk-Hardware?
    mech = mechanism(M, C, params) if mode != "widerstand" else None
    if mech is not None:
        L.append(""); S.append("")
        hd = "Mechanismus 500-Hz-Linie (@47 Hz; Prozessor-Anteil ueber Positivkontrolle C47x skaliert):"
        L.append(hd); S.append(hd)
        P, L1, LX = mech["P"], mech["L1"], mech["LX"]
        def ln(x):
            return "n/a" if not x else "%s +- %s mV @%.2f Hz (x%s Ruhe, Nachweisgr. %s)%s" % (
                fmt(x["a"]), fmt(x["se"]), x["fa"], fmt(x["rr"], 0), fmt(x["det"]), "" if x["sig"] else " nicht signifikant")
        for lab, x in (("Diretta P47 500 Hz", P), ("Diretta P47 1000 Hz", mech["P2"]), ("C47  Wecker %.1f us" % mech["u1"], L1),
                       ("C47x Wecker %.1f us" % mech["ux"], LX), ("C47x 1000 Hz", mech["LX2"])):
            line = "  %-22s %s" % (lab, ln(x)); L.append(line); S.append(line)
        line = "  Diretta-Rechenzeit je Zyklus bei Wiedergabe: +%.1f us (P50-I50, %s)" % (mech["ut"], params.get("diretta_quelle", "?"))
        L.append(line); S.append(line)
        if params.get("diretta_threads"):
            L.append("    erfasste Threads: " + ", ".join(params["diretta_threads"]))
        if "pred" in mech:
            ok_x = bool(LX and LX["sig"])
            checks.append(("Mechanismus: Positivkontrolle C47x sichtbar (%s mV, Nachweisgrenze %s)" % (fmt(LX["a"]), fmt(LX["det"])), ok_x))
            checks.append(("Mechanismus: Diretta-Linie in P47 signifikant (%s mV)" % fmt(P["a"]), bool(P and P["sig"])))
            if L1["sig"]:
                lin = L1["a"] / mech["pred1"] if mech["pred1"] > 0 else float("nan")
                checks.append(("Mechanismus: C47 bestaetigt Linearitaet (gemessen %s / erwartet %s mV = x%s, 0,67..1,5)" % (
                    fmt(L1["a"]), fmt(mech["pred1"]), fmt(lin)), 0.67 <= lin <= 1.5))
            else:
                lim = L1["a"] + 3 * L1["se"] if math.isfinite(L1["se"]) else 1.5 * L1["det"]
                checks.append(("Mechanismus: C47 nicht signifikant, passt zur Erwartung (erwartet %s, gemessen %s +- %s mV)" % (
                    fmt(mech["pred1"]), fmt(L1["a"]), fmt(L1["se"])), mech["pred1"] <= lim))
            line = "  Erwartet allein aus Diretta-Rechenzeit: %s mV = %s +- %s %% der gemessenen Diretta-Linie (+- = 1 Standardfehler)" % (
                fmt(mech["pred"]), fmt(100 * mech["ratio"], 0), fmt(100 * mech["ratio_se"], 0))
            L.append(line); S.append(line)
            if "LE" in mech:
                net = mech["LE"]["a"] - mech["pred_e_cpu"]
                line = "  E47 (end0-Nachbildung): Linie %s mV, davon Prozessor ~%s mV (%.1f us je Paket) -> Netzwerk-Hardware ~%s mV (%s %% der Diretta-Linie)" % (
                    fmt(mech["LE"]["a"]), fmt(mech["pred_e_cpu"]), mech["ue"], fmt(net),
                    fmt(100 * net / P["a"], 0) if P and P["a"] > 0 else "n/a")
                L.append(line); S.append(line)
            q = mech["ratio"]
            if not (ok_x and P and P["sig"] and math.isfinite(q)):
                v_ = "nicht entscheidbar (Pruefung oben fehlgeschlagen)"
            elif not math.isfinite(mech["ratio_se"]) or mech["ratio_se"] > 0.2:
                v_ = "UNSICHER: Prozessor ~%.0f %% +- %.0f %% - Streuung zu gross, Messung mit --dur 360 wiederholen" % (
                    100 * q, 100 * mech["ratio_se"])
            elif q >= 0.7:
                v_ = "PROZESSOR: das Aufwachen der CPU erklaert den Grossteil (>= 70 %) der 500-Hz-Linie"
            elif q <= 0.3:
                v_ = "NETZWERK-HARDWARE: der Prozessor erklaert hoechstens ~30 %, Hauptanteil end0-Sender/PHY/DMA"
            else:
                v_ = "GEMISCHT: Prozessor ~%.0f %%, Rest Netzwerk-Hardware" % (100 * q)
            line = "  ERGEBNIS: " + v_; L.append(line); S.append(line)
            line = ("  Annahmen: gleiche Stromaufnahme je Rechenzeit (Python-Schleife vs. Diretta-Code); Amplituden mehrerer Quellen"
                    " addieren je nach Phase - Anteile sind Richtwerte.")
            L.append(line); S.append(line)
    # Zuleitungswiderstand
    if "R50" in C and "defekt" not in C["R50"]:
        ra = r_analysis(C["R50"])
        L.append(""); S.append("")
        if not ra or ra.get("n", 0) < 2:
            line = "Zuleitungswiderstand (R50): nicht auswertbar (%s Lastwechsel)" % (ra or {}).get("n", 0)
            L.append(line); S.append(line)
            checks.append(("R50: mindestens 4 Lastwechsel auswertbar (%d)" % (ra or {}).get("n", 0), False))
        else:
            rr_ = ra["r"]
            for line in ("Zuleitungswiderstand (R50: 1 Kern CPU0, 10 s an / 10 s aus, %d Lastwechsel):" % ra["n"],
                         "  Spannungshub %.2f +- %.2f mV | Leistungshub PMIC-Schienen %.3f +- %.3f W | EXT5V %.3f V" % (
                             ra["dv"], ra["dv_se"], ra["dp"], ra["dp_se"], ra["vin"]),
                         "  R (Netzteil + Kabel + Stecker + Adapter + Pi-Eingang) = %s mOhm bei Wirkungsgrad %.1f %% (Spanne %s-%s mOhm fuer %.0f-%.0f %%)" % (
                             fmt(1000 * rr_[ETA[1]], 1), 100 * ETA[1], fmt(1000 * rr_[ETA[0]], 1), fmt(1000 * rr_[ETA[2]], 1),
                             100 * ETA[0], 100 * ETA[2])):
                L.append(line); S.append(line)
            checks.append(("R50: mindestens 4 Lastwechsel auswertbar (%d)" % ra["n"], ra["n"] >= 4))
            zv = ra["dv"] / ra["dv_se"] if ra["dv_se"] > 0 else float("inf")
            zp = ra["dp"] / ra["dp_se"] if ra["dp_se"] > 0 else float("inf")
            checks.append(("R50: Spannungshub klar ueber Rauschen (%.1f mV = %.1f-fach Standardfehler, >= 10)" % (ra["dv"], zv),
                           ra["dv"] > 0 and zv >= 10))
            checks.append(("R50: Leistungshub klar und plausibel (%.3f W = %.1f-fach Standardfehler; 0,3..5 W)" % (ra["dp"], zp),
                           0.3 <= ra["dp"] <= 5.0 and zp >= 10))
            if fit:
                hub = fit[1] / SQ_FUND; q = ra["dv"] / hub if hub > 0 else float("nan")
                line = "  Gegenprobe: Hub je Kern aus Frequenzgang-Kalibrierung %.1f mV -> Laststufe/Kalibrierung x%.2f" % (hub, q)
                L.append(line); S.append(line)
                checks.append(("R50: Laststufe stimmt mit Frequenzgang-Kalibrierung ueberein (x%.2f, 0,75..1,33)" % q, 0.75 <= q <= 1.33))
            line = ("  Hinweis: Wirkungsgrad der PMIC-Wandler nicht messbar (Annahme %.0f-%.0f %%); fuer Vorher/Nachher-Vergleiche"
                    " am selben Pi kuerzt er sich heraus." % (100 * ETA[0], 100 * ETA[2]))
            L.append(line); S.append(line)
    # Aktivitaet (Vollbericht)
    L.append(""); L.append("Aktivitaet je Bedingung:")
    for t in TAGS:
        m = M.get(t)
        if not m or "busy" not in m:
            continue
        L.append("  %-5s busy %s | ctxt %.0f/s irq %.0f/s" % (t, " ".join("%s=%.2f" % (k[3:], v) for k, v in m["busy"].items()),
                 m["ctxt"], m["intr"]))
        L.append("        Netz: " + "; ".join("%s rx %.0f/%.0f tx %.0f/%.0f (Pak/s / kB/s)" % (
            k, v["rxp"], v["rxb"] / 1000, v["txp"], v["txb"] / 1000) for k, v in sorted(m["net"].items()) if v["rxp"] + v["txp"] > 0.5))
        L.append("        IRQ: " + "; ".join("%s %s %.0f/s [%s]" % (k, d, r, " ".join("%.0f" % x for x in per)) for r, k, d, per in m["irq"]))
        g = C[t].get("gen")
        if g:
            L.append("        Last %s: %.2f/%.2f Hz, Verzug p50 %.3f p99 %.3f max %.3f ms, >1 ms %.2f %%, neu aufgesetzt %d" % (
                g["label"], g["ist_hz"], g["soll_hz"], g["verzug_p50_ms"], g["verzug_p99_ms"], g["verzug_max_ms"],
                g.get("anteil_ueber_1ms", float("nan")), g.get("neu_aufgesetzt", 0)))
    nf = [x for x, ok in checks if not ok]
    L.append(""); L.append("Pruefungen: %d von %d bestanden" % (len(checks) - len(nf), len(checks)))
    L.extend("  [%s] %s" % ("PASS" if ok else "FAIL", x) for x, ok in checks)
    S.append(""); S.append("Pruefungen: %d von %d bestanden%s" % (len(checks) - len(nf), len(checks), "" if nf else " (alle)"))
    S.extend("  [FAIL] " + x for x in nf)
    return "\n".join(L) + "\n", "\n".join(S) + "\n"

def compare(a, b):
    out = ["Vergleich A=%s  B=%s" % (os.path.basename(a), os.path.basename(b)),
           "Bed.   sd_A   sd_B   Diff  | var>0,1Hz A/B [mV2] | mean_A    mean_B   | rms 2-10 Hz A/B"]
    for t in TAGS:
        ca, cb = load_cond(a, t), load_cond(b, t)
        if not ca or not cb or "defekt" in ca or "defekt" in cb:
            continue
        ma, mb = metrics(ca), metrics(cb)
        if "sd" not in ma or "sd" not in mb:
            continue
        out.append("%-5s %6.2f %6.2f %+6.2f | %6.2f / %6.2f | %8.4f %8.4f | %s/%s" % (t, ma["sd"], mb["sd"], mb["sd"] - ma["sd"],
                   ma["var_hf"], mb["var_hf"], ma["mean"] / 1000, mb["mean"] / 1000, fmt(ma["bands"][2]), fmt(mb["bands"][2])))
        if t == "R50":
            ra, rb = r_analysis(ca), r_analysis(cb)
            if ra and rb and "r" in ra and "r" in rb:
                e = ETA[1]
                out.append("      Zuleitung: Hub %.2f+-%.2f / %.2f+-%.2f mV je Kern | R %s / %s mOhm (Wirkungsgrad %.1f %% angenommen)" % (
                    ra["dv"], ra["dv_se"], rb["dv"], rb["dv_se"], fmt(1000 * ra["r"][e], 1), fmt(1000 * rb["r"][e], 1), 100 * e))
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
    sv = seg_var(w, fs); f_, p_ = welch(w, fs)
    vh = band_rms(f_, p_, VAR_FMIN, 1e9) ** 2
    chk(abs(H.mean(sv) - vh) < 0.02 * vh, "Segmentvarianz-Mittel = Welch-Varianz (%.3f / %.3f)" % (H.mean(sv), vh))
    s3 = [3.0 * math.sin(2 * math.pi * 7.3 * i / fs) + random.gauss(0, 0.5) for i in range(n)]
    f_, p_ = welch(s3, fs); pr = peak_rms({"f": f_, "psd": p_}, 7.3)
    chk(abs(pr - 3 / math.sqrt(2)) < 0.1, "peak_rms Sinus 7,3 Hz Amplitude 3 -> %.3f (Soll 2,121)" % pr)
    # Quellsuche: 505,1-Hz-Quelle, abgetastet mit 49,996 und 47 Hz, gegen Rauschreferenz
    def mk(fs, sig):
        v = [random.gauss(0, 1.0) + (2.0 * math.sin(2 * math.pi * 505.1 * i / fs) if sig else 0.0) for i in range(int(180 * fs))]
        f, p = welch(v, fs)
        return {"f": f, "psd": p, "fs": fs, "df": f[1] - f[0]}
    MM = {"P50": mk(49.996, True), "P47": mk(47.0, True), "I50": mk(49.996, False), "I47": mk(47.0, False)}
    iv = source_search(MM, 505.0)
    chk(any(a_ - 0.05 <= 505.1 <= b_ + 0.05 for a_, b_, _ in iv), "Quellsuche findet 505,1 Hz: %s" % [(round(a_, 2), round(b_, 2)) for a_, b_, _ in iv])
    chk(not testable(alias(500.0, 50.0), MM["P50"]) and testable(alias(505.1, 49.996), MM["P50"]), "Pruefbarkeit (0 Hz = n.p.)")
    a = adev([float(i % 2) for i in range(1000)], 50.0)
    chk(abs(a[0.04] - 0.0) < 1e-12, "ADEV (Periode 2 bei tau=2 Samples = 0)")
    for Tt in (0.0, 0.004, 0.010):
        fw = fit_window([(f, 4.5 * sinc_h(f, Tt) * (1 + random.gauss(0, 0.02))) for f in KAL_F])
        chk(fw and abs(fw[0] - Tt) < 0.0006 and abs(fw[1] - 4.5) < 0.2,
            "Fensterfit T=%.0f ms -> %.2f ms, A0 %.2f" % (1000 * Tt, 1000 * fw[0], fw[1]) if fw else "Fensterfit fehlgeschlagen")
    Mb = {"I50": {"mean": 10.0}, "I50m": {"mean": 12.0}, "I50b": {"mean": 14.0}}
    Cb = {"I50": {"c0": {"t": 0}, "c1": {"t": 10}}, "I50m": {"c0": {"t": 100}, "c1": {"t": 110}},
          "I50b": {"c0": {"t": 200}, "c1": {"t": 210}}}
    bl = baseline(Mb, Cb, 155.0)
    chk(abs(bl[0] - 13.0) < 1e-9 and bl[1] == "interpoliert" and baseline(Mb, Cb, 0.0)[1].startswith("vor"),
        "Drift-Basislinie linear interpoliert (13,0 bei t=155)")
    sg = SquareGen(5.0, "t"); sg.start(); time.sleep(1.0)
    states = set()
    for _ in range(20):
        st_ = H.rd("/proc/%d/stat" % sg.proc.pid).split(") ")[-1][:1]; states.add(st_); time.sleep(0.023)
    sg.stop_ev.set(); sg.join(10); gs = sg.stats()
    chk({"T"} <= states and (states & {"R", "S"}) and abs(gs["ist_hz"] - 10) < 1 and sg.proc.poll() is not None
        and 0.42 <= gs["tastverhaeltnis"] <= 0.58,
        "Rechtecklast 5 Hz: Zustaende %s, Umschaltrate %.1f/s, Tastverhaeltnis %.2f, beendet" % (
            sorted(states), gs["ist_hz"], gs["tastverhaeltnis"]))
    cnt = []
    pc = Pacer(500.0, lambda: cnt.append(1), "t"); pc.start(); time.sleep(1.0); pc.stop_ev.set(); pc.join()
    st = pc.stats()
    chk(abs(st["ist_hz"] - 500) < 15, "Pacer 500 Hz -> %.1f Hz, p99 Verzug %.3f ms" % (st["ist_hz"], st["verzug_p99_ms"]))
    # --- v1.3: PMIC, Widerstand, Mechanismus
    txt = ("   VDD_CORE_A current(7)=2.00000000A\n   3V3_SYS_A current(1)=0.10000000A\n   VDD_CORE_V volt(15)=0.90000000V\n"
           "   3V3_SYS_V volt(9)=3.30000000V\n   EXT5V_V volt(24)=5.10000000V\n   BATT_V volt(25)=0.00000000V\n")
    pw, u5, rl = parse_pmic(txt)
    chk(abs(pw - 2.13) < 1e-9 and abs(u5 - 5.1) < 1e-9 and set(rl) == {"VDD_CORE", "3V3_SYS"},
        "PMIC-Auswertung: %.3f W (Soll 2,130), EXT5V %.2f V" % (pw, u5))
    t0 = 1000.0; fs_ = 50.0; tt = [i / fs_ for i in range(int(180 * fs_))]
    tog = [(t0 - 3.0 + 10.0 * k, k % 2 == 1) for k in range(20)]     # t0-3: Last aus, t0+7: an, ...
    def on_at(ta):
        st = False
        for tk, o in tog:
            if tk <= ta:
                st = o
        return st
    vv = [5000.0 - 25.0 * on_at(t0 + x) + 0.6 * x / 60.0 + random.gauss(0, 0.8) for x in tt]
    rows = [{"t": t0 + 0.5 * k, "p": 3.0 + 1.5 * on_at(t0 + 0.5 * k) + random.gauss(0, 0.01), "ext5v": 5.0}
            for k in range(360)]
    ra = r_analysis({"gen": {"toggles": tog}, "pmic": rows, "logger_start": t0, "v": vv, "t": tt, "fs": fs_})
    rexp = 0.025 / (1.5 / (ETA[1] * 5.0))
    chk(ra and ra["n"] >= 7 and abs(ra["r"][ETA[1]] / rexp - 1) < 0.02 and abs(ra["dv"] - 25) < 0.5,
        "Widerstand synthetisch (25 mV, 1,5 W, Drift 0,6 mV/min): %s mOhm (Soll %.1f), %d Wechsel" % (
            fmt(1000 * ra["r"][ETA[1]], 1) if ra and "r" in ra else "n/a", 1000 * rexp, ra["n"] if ra else 0))
    def pulses(h, us, f, fs, dur=180.0, noise=0.5, extra=None):
        d = us * 1e-6 * f
        return [-(h if (i / fs * f) % 1.0 < d else 0.0) + random.gauss(0, noise) + (extra(i / fs) if extra else 0.0)
                for i in range(int(dur * fs))]
    def mkm(v, fs):
        f, p = welch(v, fs); df = f[1] - f[0]
        nb = sorted(x for fk, x in zip(f, p) if 2.0 <= fk <= 20.0)
        return {"f": f, "psd": p, "fs": fs, "df": df, "det_rms": math.sqrt(15 * nb[len(nb) // 2] * df), "v": v}
    fz = 500.03
    mp = mkm(pulses(24.0, 200.0, fz, 47.0), 47.0)
    a_th = math.sqrt(2) / math.pi * 24.0 * math.sin(math.pi * 200e-6 * fz)
    a_me = peak_rms(mp, alias(fz, 47.0))
    chk(abs(a_me / a_th - 1) < 0.05, "Pulsfolge 500 Hz @47 Hz: Grundwelle %.3f mV (Theorie %.3f)" % (a_me, a_th))
    MM = {"I47": mkm([random.gauss(0, 0.5) for _ in range(int(180 * 47))], 47.0),
          "C47": mkm(pulses(24.0, 40.0, 500.011, 47.0), 47.0), "C47x": mkm(pulses(24.0, 200.0, 500.011, 47.0), 47.0)}
    # 500,011 statt 500,000: genau 500 Hz waere mit 47 Hz kommensurabel (nur 47 Phasenlagen) - kuenstlicher Testfehler
    CC = {"C47": {"gen": {"cpu_us_je_aufruf": 40.0, "ist_hz": 500.011}}, "C47x": {"gen": {"cpu_us_je_aufruf": 200.0, "ist_hz": 500.011}}}
    for hp, soll, nz in ((24.0, 1.0, 0.1), (48.0, 0.5, 0.1), (24.0, 1.0, 0.8), (48.0, 0.5, 0.8)):
        # Abtastzeit-Streuung 0,1 ms wie beim echten Logger (sonst kuenstliche Phasenmuster bei fast kommensurablen Raten)
        jt = lambda: random.gauss(0, 1e-4)
        def pj(h, us, f):
            d = us * 1e-6 * f
            return [-(h if ((i / 47.0 + jt()) * f) % 1.0 < d else 0.0) + random.gauss(0, nz) for i in range(int(180 * 47))]
        MM = {"I47": mkm([random.gauss(0, nz) for _ in range(int(180 * 47))], 47.0), "P47": mkm(pj(hp, 40.0, fz), 47.0),
              "C47": mkm(pj(24.0, 40.0, 500.011), 47.0), "C47x": mkm(pj(24.0, 200.0, 500.011), 47.0)}
        me = mechanism(MM, CC, {"diretta_us": 40.0, "end0_hz": fz})
        q, qs = (me.get("ratio", float("nan")), me.get("ratio_se", float("nan"))) if me else (float("nan"),) * 2
        lin = me["L1"]["a"] / me["pred1"] if me and me.get("pred1") else float("nan")
        chk(abs(q - soll) <= max(0.06 * soll, 3 * qs) and math.isfinite(qs) and abs(lin - 1) < 0.25,
            "Mechanismus synthetisch (Rauschen %.1f mV): Diretta-Puls %.0f mV -> Prozessoranteil %.2f +- %.2f (Soll %.2f), Linearitaet x%.2f" % (
                nz, hp, q, qs, soll, lin))
    pc = Pacer(500.0, cpu_burst(100), "t"); pc.start(); time.sleep(1.0); pc.stop_ev.set(); pc.join(); st = pc.stats()
    chk(95 <= st["cpu_us_je_aufruf"] <= 300, "Pacer misst Rechenzeit je Aufruf: %.1f us (Schleife 100 us)" % st["cpu_us_je_aufruf"])
    bu, got = tune_burst(150.0, 500.0)
    chk(0.8 <= got / 150.0 <= 1.25, "Abgleich Rechenzeit: Soll 150 us -> gemessen %.1f us (Schleife %.1f us)" % (got, bu))
    pl = Pacer(0.1, lambda: None, "lang"); pl.start(); time.sleep(0.3); t_ = time.monotonic(); pl.stop_ev.set(); pl.join(3)
    chk(not pl.is_alive() and time.monotonic() - t_ < 1.0, "Pacer mit 10-s-Periode sofort abbrechbar")
    dr = diretta_rt()
    chk(isinstance(dr.get("ns"), int) and dr["ns"] >= 0, "Diretta-Rechenzeit lesbar (%s, %d Threads-Namen)" % (dr["quelle"], len(dr["threads"])))
    print("SELBSTTEST %s" % ("BESTANDEN" if ok else "NICHT BESTANDEN"))
    return 0 if ok else 1


# ------------------------------------------------------------------ Kommandozeile
def main():
    ap = argparse.ArgumentParser(prog="vschwank.py")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run"); r.add_argument("--dur", type=int, default=180); r.add_argument("--ohne-musik", action="store_true")
    r.add_argument("--kal-kurz", action="store_true", help="nur 2 Kalibrierfrequenzen (7,3 / 213 Hz) statt 6")
    w = sp.add_parser("widerstand"); w.add_argument("--dur", type=int, default=180)
    tg = sp.add_parser("target"); tg.add_argument("--dur", type=int, default=180)
    sp.add_parser("status"); sp.add_parser("stop"); sp.add_parser("selftest")
    a = sp.add_parser("analyse"); a.add_argument("ordner")
    v = sp.add_parser("vergleich"); v.add_argument("a"); v.add_argument("b")
    k = sp.add_parser("_child"); k.add_argument("dir"); k.add_argument("dur", type=int); k.add_argument("music", type=int)
    k.add_argument("lockfd", type=int); k.add_argument("kal_kurz", type=int); k.add_argument("mode")
    args = ap.parse_args()
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "analyse":
        print(analyse(os.path.abspath(args.ordner))[0], end=""); return 0
    if args.cmd == "vergleich":
        print(compare(os.path.abspath(args.a), os.path.abspath(args.b)), end=""); return 0
    if args.cmd == "_child":
        return child_main(args.dir, args.dur, bool(args.music), args.lockfd, bool(args.kal_kurz), args.mode)
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
    mode = {"widerstand": "widerstand", "target": "target"}.get(args.cmd, "voll")
    ohne = mode == "widerstand" or (mode == "voll" and args.ohne_musik)
    kurz = mode == "voll" and args.kal_kurz
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
                              str(0 if ohne else 1), str(lock.fileno()), str(int(kurz)), mode], stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock.fileno(),))
    lock.close()
    H.wjson(curp, {"dir": cdir, "pid": p.pid})
    if mode == "widerstand":
        n, nk, tot = 1, 0, args.dur + 30
    elif mode == "target":
        n, nk = 9, 4                        # P50 P47 I50 I47 C47 C47x I50m R50 I50b + K7 K113 K313 K213b
        tot = n * (args.dur + 25) + nk * (min(args.dur, KAL_DUR) + 25) + 2 * SETTLE_CHANGE + 6
    else:
        nk = 3 if kurz else 7
        n = 9 + (0 if ohne else 4)          # I50 I47 E50 E47 U50 C50 I50m R50 I50b (+ P50 P47 C47 C47x)
        tot = n * (args.dur + 25) + nk * (min(args.dur, KAL_DUR) + 25) + 2 * SETTLE_CHANGE + (0 if ohne else 6)
    print("%s gestartet: %d Bedingungen + %d Kalibrierungen, gesamt ca. %d min. Laeuft weiter, auch wenn die Verbindung abreisst." % (
        {"widerstand": "Widerstandsmessung", "target": "Target-Versuchsreihe"}.get(mode, "Versuchsreihe"), n, nk, round(tot / 60)))
    print("Fortschritt:  python3 vschwank.py status      Abbruch:  python3 vschwank.py stop\n")
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
        print("\n(Anzeige beendet - Messung laeuft weiter. Fortschritt: python3 vschwank.py status)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
