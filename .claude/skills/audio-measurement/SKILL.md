---
name: audio-measurement
description: Messen und Auswerten von Latenz, Jitter, Systemrauschen, Spannungsschwankung und ALSA-Underruns auf den Diretta-Pis (Raspberry Pi 5, AudioLinux, RT-Kernel) - cyclictest, rtla timerlat/osnoise, perf, ftrace/trace-cmd, /proc/asound, sowie die Repo-Werkzeuge ext5v, hostmess.py und vschwank.py. Verwenden, wenn Messdaten erhoben, verglichen oder gedeutet werden sollen oder ein Tuning-Schritt belegt werden muss.
---

# audio-measurement

Grundsaetze (verbindlich):
- **Nur gemessene Werte berichten**, mit Werkzeug, Dauer, Bedingung (Musik an/aus, Rate) und Datum. Keine Schaetzungen als Messwerte.
- **Vorher/Nachher unter identischen Bedingungen**, gleiche Dauer, Ruhe-Wiederholung als Stabilitaetskontrolle.
- **Messwerkzeug-Eigenlast ausweisen**; Werkzeuge auf CPU0/1, nie unbemerkt auf den Audio-Kernen 2/3.
- **Unterschiede nur deuten, wenn groesser als der Standardfehler**; Korrelation ist kein Ursachennachweis.
- Waehrend Messfenstern nicht auf die SD-Karte schreiben (Repo-Werkzeuge nutzen `/dev/shm`).
- **Lastgeneratoren, die auf derselben CPU wie ihre Steuerung laufen, mit `chrt -i 0` (SCHED_IDLE) starten** - sonst verzoegert
  die Last ihr eigenes Abschalten (gemessen: Tastverhaeltnis 70 % statt 50 % bei 113 Hz) und verfaelscht Kalibrierungen.

## 1. Repo-Werkzeuge (bevorzugt, getestet)

| Werkzeug | Zweck | Aufruf |
|---|---|---|
| `~/ext5v/ext5v_run.sh` + `ext5v_stats.py` | EXT5V-Spannung 50 Hz, Logger auf CPU1 | `EXT5V_CPU=1 ./ext5v_run.sh 50 300 TAG` |
| `hostmess.py` | Kampagne Ruhe/Wiedergabe/Laststufe + Systemaktivitaet (/proc) | `python3 hostmess.py run [--idle 0] [--step-mit-musik]` |
| `vschwank.py` | Ursache der Schwankung: Alias-Test 50/47 Hz, Nachbildung end0/USB/CPU, Mechanismus CPU vs. Netz, Zuleitungswiderstand, Kalibrierung | `python3 vschwank.py run`, `widerstand`, `vergleich A B` |
| `diagnose.sh` | Konfigurations-Bestandsaufnahme | `sudo bash diagnose.sh` |
Ergebnisse: `~/ext5v/results/...` mit `SHA256SUMS`, Kurzbericht `kurz.txt`. Alle Werte in `Messprotokoll.md` uebernehmen.

Wichtige Grenzen der Spannungsmessung: ADC-Raster 1,34 mV, Absolutfehler ca. +-1,5 %, 50 Hz Abtastung
(Ereignisse < 20 ms unsichtbar, Quellen nahe Vielfachen von 50 Hz fallen auf 0 Hz).

Kalibrierungen in `vschwank.py` (v1.3, geraetespezifisch, bei jeder Reihe neu):
1. **Frequenzgang** `K3..K313` (+ `K213b` @47 Hz): identische Rechtecklast CPU0 bei 3,1-313 Hz -> Mittelungsfenster T des
   PMIC-Sensors (Boxcar-Modell), H(f). Gemessene Linien-Amplituden werden mit 1/H(f) korrigiert (nur H >= 0,2, als Modell markiert).
   Erst danach ein „keine Spitze" als „keine Stoerung" deuten.
2. **Lastempfindlichkeit**: Hub je voll belastetem Kern aus der Grundwelle (Gegenprobe zur hostmess-Laststufe).
3. **Drift-Klammerung** `I50 / I50m / I50b`: Mittelwerte relativ zur zeitlich interpolierten Ruhe; Spanne der Klammern <= 3 mV gefordert.
4. **Netzbrumm/Fremdfrequenzen** 50/100/150 Hz (Alias nur bei 47 Hz sichtbar) sowie 60/120 Hz (bei 50 und 47 Hz sichtbar).
5. **Mechanismus** `C47/C47x` (v1.3): CPU0-Wecker mit Diretta-Takt @47 Hz. Soll-Rechenzeit je Zyklus = Diretta-Threads +
   end0-IRQ-Threads + ksoftirqd (schedstat) P50 minus I50; Schleife vor Ort auf die *gemessene* Thread-Rechenzeit abgeglichen.
   `C47x` (~10 % Tastverhaeltnis) ist Positivkontrolle und Skala (Grundwelle ~ sin(pi*d)); `C47` prueft die Linearitaet.
   Ergebnis: Prozessor-Anteil an der 500-Hz-Linie aus P47 mit Block-Standardfehler; Rest = Netzwerk-Hardware (end0/PHY/DMA).
6. **Zuleitungswiderstand** `R50` bzw. `python3 vschwank.py widerstand` (auch am Target, Musik egal): Laststufe 10 s/10 s,
   PMIC-Schienenleistung parallel (`vcgencmd pmic_read_adc`), R = dV / (dP / (eta * U)), eta 80-95 % angenommen
   (kuerzt sich im Vorher/Nachher am selben Pi heraus). Bewertet Netzteil, Kabel, Stecker, Adapter, Zwischenfilter.
   Was diese Kette **nicht** sieht: HF-Stoerungen (kHz-MHz) - Wirkung von Netzfiltern/Ferriten ist damit nicht messbar.
Fehlerangaben: Varianz > 0,1 Hz mit Segment-Standardfehler, Nachweisgrenze fuer Spitzen. Absolutwert nur mit externem
Multimeter kalibrierbar (nicht automatisiert); fuer Relativvergleiche nicht noetig.

## 2. Latenz und Jitter (Kernel/Scheduler)

Nur bei gestoppter Musik oder mit Hinweis im Protokoll (RT-Messthreads konkurrieren mit Diretta).
```bash
command -v cyclictest rtla perf trace-cmd          # vorhanden? (rt-tests, rtla, perf, trace-cmd)
sudo cyclictest -m -p 80 -i 200 -D 5m -t 2 -a 2,3 -h 400 -q > ct.txt   # Histogramm je Kern, Max-Latenz in us
sudo rtla timerlat top -c 2,3 -d 5m                 # Timer-Latenz IRQ/Thread je Kern
sudo rtla osnoise top -c 2,3 -d 5m                  # Betriebssystem-Rauschen (Unterbrechungen) je Kern
```
Auswertung: Max- und 99,9-%-Latenz je Kern; `osnoise` zeigt Anzahl/Dauer der Unterbrechungen nach Quelle
(IRQ, Softirq, Thread). Vergleich immer Kern 2/3 gegen 0/1.

## 3. Wer unterbricht? (perf / ftrace)

```bash
sudo perf stat -a -A -e context-switches,cpu-migrations,irq:irq_handler_entry -- sleep 30
sudo perf sched record -- sleep 10 && sudo perf sched timehist -S | tail -40
sudo trace-cmd record -M 4 -e sched_switch -e irq_handler_entry -e softirq_entry -- sleep 10   # -M 4 = nur CPU2
sudo trace-cmd report | awk '{print $4}' | sort | uniq -c | sort -rn | head
```
Ohne perf/trace-cmd: `hostmess.py` liefert Weckvorgaenge je Thread (`voluntary_ctxt_switches`), Interrupts je
Quelle und Kern, Softirqs und Threads auf CPU2/3 - ohne Zusatzpakete und ohne root.

## 4. ALSA-Underruns

```bash
cat /proc/asound/card*/pcm*p/sub*/status            # state: RUNNING / XRUN, hw_ptr/appl_ptr
cat /proc/asound/card*/pcm*p/sub*/hw_params         # Rate, Format, period/buffer
cat /proc/asound/card*/stream0                      # USB-DAC am Target: Status, Momentary freq
dmesg | grep -iE 'xrun|underrun'
journalctl -b -u diretta_alsa      # Host;  Target: -u diretta_alsa_target
```
Falls der Kernel `CONFIG_SND_PCM_XRUN_DEBUG` hat: `echo 1 | sudo tee /proc/asound/card0/pcm0p/xrun_debug`
(Meldungen in dmesg; danach wieder `echo 0`). Underruns zaehlen: Status-Datei sekuendlich abfragen und
Wechsel auf `XRUN` zaehlen; Diretta-seitig `alsaUnderrun=enable` in `setting.inf`.

## 5. Bekannte Messwerte als Referenz (Host, Tomanek allein, 02.10.2026)

Ruhe 4,9699 V / sd 2,07 mV; Wiedergabe 96/32 4,9620 V / sd 4,36 mV; Laststufe ein Kern -25,2 mV,
Unterschwinger -11,7 mV; Ruhe 3561 Kontextwechsel/s, 3181 IRQ/s; Wiedergabe 8445/s, 6210/s;
end0 505 Pak/s, enu1 361 Pak/s. Target iFi: Ruhe 5,019 V / 3,30 mV, Wiedergabe 6,54 mV.
Vollstaendig: `Messprotokoll.md`.
