# Diretta-Kette: Abschluss Testserie 1–3

Stand: 06.10.2026 · Quelle aller Zahlen: `Messprotokoll.md` (Abschnitte Test 1–3) · Branch `claude/zealous-ride-1fn94u`

Host 192.168.178.71, Target 172.20.0.2 (Raspberry Pi 5), DAC Audiolab 8300CD (USB, ASYNC).

## Test 1: USB-Unterbrechungen am Target (04.10.2026)

- Ursache: Der Audiolab meldet sich zusätzlich als Tastatur (Medientasten). Der Kernel öffnet Interface 4 und fragt es alle 2,000 ms ab (500/s, Inhalt immer `00`).
- Maßnahme: udev-Regel `/etc/udev/rules.d/90-audiolab-ohne-hid.rules` setzt beim `add`-Ereignis `authorized=0` für Interface 4 (VID 2622, PID 0041).
- Die erste Fassung (`bind` + unbind) griff nur bei einem von zwei Neustarts, weil usbhid beim Booten teils vor udevd bindet. Die `add`-Variante wirkt unabhängig davon und ist nach Neustart geprüft.
- Rückweg: Datei löschen, `echo 1 > /sys/bus/usb/devices/3-2.3.1.4:1.4/authorized`.

| Zustand | xhci ohne Musik | xhci mit Musik |
|---|---|---|
| vorher | 500/s | 2053/s |
| HID-Interface 4 gesperrt | **0/s** | 2053/s |
| zusätzlich `snd_usb_audio lowlatency=0` | 0/s | 2053/s (keine Wirkung, zurückgesetzt) |

Mit Musik bringt die Sperre nichts, da die HID-Abfragen ohnehin in vorhandene Interrupts fallen. Die Bündelgröße der Audio-URBs (1200/s) ist durch das 1-ms-Feedback des DAC und die ALSA-Periode (480 Frames) festgelegt. Das theoretische Minimum liegt bei ca. 1140/s.

## Test 2: end0-IRQ 104 am Host auf CPU3 (05.10.2026, Musik 96 kHz)

Ausgangslage: Affinität `2-3`, effektiv CPU2 (dort läuft auch syncAlsa FF99, CpuSend=2). Variante: Affinität `3`.

| IRQ 104 | CPU2 | CPU3 | Summe CPU2+3 |
|---|---|---|---|
| 2-3 (CPU2) | 1518/s | 602/s | 2120/s |
| 3 | 507/s | 1613/s | 2120/s |

Paketabstand am Target (tcpdump, ABAB je 20 s, ca. 9950 Abstände je Block):

| IRQ 104 | SD [µs] | mittl. Abw. von 2000 µs [µs] |
|---|---|---|
| 3 | 54,34 | 7,01 |
| 2-3 | 52,68 | 6,92 |
| 3 | 50,28 | 6,72 |
| 2-3 | 53,25 | 6,90 |

Ergebnis: Die IRQs werden nur verschoben, die Summe bleibt gleich, das Paket-Timing liegt innerhalb der Blockstreuung. Keine messbare Wirkung, deshalb bleibt `2-3`.

## Test 3: 48 kHz gegen 96 kHz am Target (05.10.2026, vschwank.py 1.4.6, 65/66 Prüfungen)

Alle Takte sind zeitbasiert und unabhängig von der Abtastrate (ALSA-Periode 5 ms / 200 Hz, Zo 1200/s, Zi 1000/s, xhci 2054/s, Diretta 500 Paket/s). Nur die Nutzlast halbiert sich.

| Größe | 96 kHz (04.10.) | 48 kHz (05.10.) |
|---|---|---|
| Ruhe I50 sd / Varianz | 3,14 mV / 8,82 mV² | 1,51 mV / 2,09 mV² |
| Wiedergabe P50 sd / Varianz | 5,85 mV / 28,66 mV² | 6,74 mV / 32,62 mV² |
| Zusatzvarianz Wiedergabe | 19,8 ± 3,5 mV² | 30,5 ± 3,6 mV² |
| 500-Hz-Linie (P47) | 0,67 ± 0,08 mV | 0,99 ± 0,10 mV |
| Diretta-Rechenzeit je Zyklus | +18,2 µs | +17,5 µs |
| Prozessor-Anteil (über C47x) | 78 ± 9 % | 74 ± 7 % |
| Linie 9,64 Hz @47 | 2,06 mV | 2,23 mV |
| langsame Komponente 0,1–0,5 Hz (P47) | 1,20 mV (0,37 Hz) | 1,16 mV |

Bewertung:
- Halbe Nutzlast senkt die 500-Hz-Linie nicht. Das Aufwachen der CPU je Diretta-Zyklus (500 Hz) bestimmt sie, nicht die Datenmenge. Die Absolutwerte der beiden Tage sind nicht direkt vergleichbar (Ruhe war am 05.10. halb so unruhig).
- Prüfung gescheitert: C47-Linearität ×0,65 (Grenze 0,67). Der Prozessor-Anteil ist daher nur als Bereich 47–74 % belastbar, nicht als "dominiert".
- Die 9,64-Hz-Linie hat bei 48 und 96 kHz dieselbe Frequenz und hängt nicht am DAC-Takt. Die ALSA-Periode (200 Hz) ist als Quelle ausgeschlossen. Die Quellfrequenz (ca. 479,6 Hz) ist nicht eindeutig, die Herkunft bleibt offen.
- Die langsame Komponente (ca. 1,2 mV bei P47) ist bei 48 und 96 kHz gleich, die Ursache ist offen. Die P50-Anteile von 0,1–0,5 Hz sind überwiegend Alias der Diretta-Oberwellen.

## Fazit und nächster Schritt

- Wirksam war nur die HID-Sperre (Ruhe-Interrupts 500/s → 0/s). IRQ-Affinität und `lowlatency` bringen nichts.
- Für die 500-Hz-Linie gibt es auf Pi-Seite keinen weiteren belegten Hebel.
- Nächster Schritt: Vorher/Nachher-Messung am Host, sobald dort das iFi Elite 5 V/5 A angeschlossen ist.

Werkzeuge: `vschwank.py` 1.4.6, `kernrausch.py` 1.0, `hostmess.py`. PRs: #2 (gemerged, Test 1), #3 (Draft, Tests 1–3).
