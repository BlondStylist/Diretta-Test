# Messprotokoll 5-V-Schiene (EXT5V_V)

Werkzeug `~/ext5v/` (ext5v_log 2.0, `ext5v_stats.py` v2.0), 50 Hz, Logger auf Core 1, SCHED_IDLE, ohne künstliche Last.
ADC-Auflösung 1,34 mV, Absolutfehler ca. ±1,5 % (Vergleiche zwischen Läufen am selben Gerät sind genauer als Absolutwerte).
„Wiedergabe" = echte Musik 96 kHz über Diretta. Alle Qualitätsprüfungen PASS, sofern nicht anders vermerkt.

## Übersicht aller Läufe

| Datum | Gerät | Netzteil / Aufbau | Lauf (Tag) | Dauer | Mittel [V] | sd [mV] | P0,1 [V] | P99,9 [V] | min [V] | max [V] | throttled |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 28.09. | Target | altes NT (~5,10 V) | s01-idle-a | 600 s | 5,09717 | 4,57 | 5,07190 | 5,10272 | 4,99954 | 5,10540 | 0x0 |
| 28.09. | Target | altes NT | p3-idle | 600 s | 5,10186 | 4,30 | 5,07860 | 5,10808 | 5,06386 | 5,11076 | 0x0 |
| 28.09. | Target | altes NT | p2-play | 600 s | 5,08301 | 7,93 | 5,04241 | 5,09468 | 4,94192 | 5,09736 | 0x0 |
| 30.09. | Target | iFi iPower Elite 5 V/5 A, 1,5 m Kabel + Adapter, PSU_MAX_CURRENT=5000 | ifi-idle | 300 s | 5,01938 | 3,30 | 5,00088 | 5,02232 | 4,98882 | 5,02500 | 0x50000¹ |
| 30.09. | Target | wie oben | ifi-play | 300 s | 5,00858 | 6,54 | 4,97408 | 5,01696 | 4,95264 | 5,01964 | 0x50000¹ |
| 30.09. | Host | Tomanek, geteilt mit OptiLink | tom-shared-idle | 300 s | 4,96340 | 1,87 | 4,94058 | 4,96872 | 4,90842 | 4,96872 | 0x0 |
| 30.09. | Host | Tomanek, geteilt mit OptiLink | tom-shared-play | 300 s | 4,96240 | 4,49 | 4,90842 | 4,96872 | 4,88966 | 4,96872 | 0x0 |
| 30.09. | Host | Tomanek allein (OptiLink jetzt am alten Target-NT, 5,11 V, über passiven Barrel→USB-C-Adapter) | tom-solo-idle | 300 s | 4,96709 | 2,05 | 4,95264 | 4,97140 | 4,92986 | 4,97140 | 0x0 |
| 30.09. | Host | wie oben | tom-solo-play | 300 s | 4,96173 | 4,24 | 4,91110 | 4,96470 | 4,89636 | 4,96604 | 0x0 |
| 02.10. | Host | Tomanek allein, hostmess.py | hm-idle | 600 s | 4,96986 | 2,07 | – | – | 4,92986 | 4,97408 | 0x0 |
| 02.10. | Host | wie oben, Wiedergabe 96 kHz/32 Bit | hm-play | 600 s | 4,96203 | 4,36 | 4,90842 | 4,96872 | 4,88698 | 4,97006 | 0x0 |
| 02.10. | Host | wie oben, Laststufen CPU0 bei Wiedergabe | hm-step | 300 s | 4,95483 | 13,29 | 4,90842 | 4,97140 | 4,88698 | 4,97140 | 0x0 |

¹ Merker vom Kaltstart (Einschaltstrom der USB-Kette bei ~6 s), kein Einbruch im Betrieb.

Weitere Kennwerte (ifi/Target): Spitze-Spitze Ruhe 36,2 mV / Wiedergabe 67,0 mV (alt 46,9 / 155,4); ADEV Wiedergabe τ=1,28 s 2,08 mV (alt 4,34); ADEV Ruhe τ=10 s 0,78 mV (alt 2,18).

Schnellmessung 30.09. (vcgencmd, 300 Werte à 0,2 s, Target, iFi vor EEPROM-Änderung): Ruhe 5,012 V / sd 4,7 mV; Wiedergabe 5,004 V / sd 7,6 mV, min 4,974 V.
Einzelwert `diagnose.sh` Host 30.09. 15:08: 4,907 V (Momentwert, nicht repräsentativ).

## Bewertungen

- **Target, altes NT → iFi:** Ruhe-sd −23 %, Wiedergabe-sd −18 %, tiefster Einbruch −60 %, langsame Eigenschwingung verschwunden → iFi behalten.
- **Host, Tomanek geteilt → allein:** Unterschiede im Rahmen der Messstreuung (Mittel +3,7 mV Ruhe, sd Wiedergabe 4,49 → 4,24 mV, P0,1 Ruhe +12 mV). Der OptiLink war nur eine kleine Last. Nutzen der Trennung liegt in der entfallenen gemeinsamen Versorgung von Host und Audio-Seite hinter der optischen Trennung – mit diesem Werkzeug nicht messbar. Tomanek regelt sehr gut (beste Ruhe-sd im System), Absolutspannung ~4,96 V niedrig, aber > 300 mV über der Unterspannungsschwelle.

## Aktueller Aufbau (Stand 30.09.2026, 16:10)

- Target: iFi iPower Elite 5 V/5 A → 1,5 m fest verbautes Kabel → passiver Barrel→USB-C-Adapter → Pi 5. EEPROM `PSU_MAX_CURRENT=5000`.
- Host: Tomanek-Netzteil (Sonderanfertigung für Pi 4) allein.
- iFi OptiLink (Ausgangsseite): altes Target-Netzteil, 5,11 V, passiver Barrel→USB-C-Adapter.
- Kette Target → DAC: Target USB-A → iFi Pulsar USB-C-Kabel → OptiLink (optisch getrennt) → iFi OMNI USB Switch → Topping HS-02 → Audiolab 8300CDQ (USB-B).
- Host↔Target `end0`: absichtlich 10 Mbit/s, Wiedergabe nur 48/96 kHz.
- Geplant: zweites iFi iPower Elite 5 V/5 A (Zuordnung Host oder OptiLink per Messung entscheiden).

## Änderungsprotokoll (Konfiguration)

| Datum | Gerät | Änderung | Rückgängig |
|---|---|---|---|
| 30.09.2026 | Target | EEPROM `PSU_MAX_CURRENT=5000` (psu_eeprom.sh v1.3, Backup `/root/config-archiv/eeprom-20260930_132830-4651`) | `sudo bash ~/psu_eeprom.sh rollback /root/config-archiv/eeprom-20260930_132830-4651` |
| 30.09.2026 | Host | `/boot/config.txt`: `camera_auto_detect=1` → `0` (Sicherung `/boot/config.txt.vor-kamera`) | `sudo cp /boot/config.txt.vor-kamera /boot/config.txt` + Reboot |
| 30.09.2026 | Host | `serial-getty@ttyAMA10.service` maskiert (serielle Anmeldung an GPIO aus) | `sudo systemctl unmask serial-getty@ttyAMA10.service && sudo systemctl start serial-getty@ttyAMA10.service` |
| 30.09.2026 | Host | Bewusst **nicht** geändert: `display_auto_detect`, `avahi-daemon` (.local-Namen werden genutzt) | – |
| 30.09.2026 | Netzteile | Tomanek nur noch Host; OptiLink an altes Target-NT (5,11 V) | umstecken |

## NUC8i7BEH (Roon ROCK) – Bestandsaufnahme 30.09.2026

- ROCK 1.0 (Build 259), Roon Server 2.73 (Build 1694), alle Status OK. IP 192.168.178.26 per DHCP.
- Datenträger: Datenbank auf internem M.2 (458 GB), Samsung SSD 860 1 TB (Musik, kaum genutzt). Quellen praktisch nur Qobuz und HRA, kein Roon-DSP.
- Netzteil: Keces P8 **Doppelausgang** (9/12 V + 18/19 V, je 4 A, Überstromabschaltung 4,2 A, gemeinsamer Ringkerntrafo, getrennte Masse je Schiene). Schiene 19 V → NUC (max. ~80 W, NUC-Spitze ca. 60–65 W → Reserve, mit Turbo aus deutlich mehr); Schiene 12 V → FritzBox 7590 (Bedarf ca. 10–15 W, Original-NT 12 V/2,5 A). OLED-Anzeige zeigt Spannung und Strom.
- Passives, lüfterloses Gehäuse. Angeschlossen nur LAN + DC.
- BIOS noch nicht geändert. Geplant (Werte vorher notieren): WLAN, Bluetooth, HD Audio, Mikrofon, Card Reader, Consumer IR, ggf. Thunderbolt aus; Turbo Boost aus; LEDs aus; After Power Failure = Last State/Power On; C-States/SpeedStep/Hyper-Threading unverändert; CPU-Temperatur vorher/nachher ablesen.

## Host-Aktivitaet (hostmess.py, 02.10.2026, im Messfenster)

| Groesse | Ruhe | Wiedergabe 96/32 |
|---|---|---|
| CPU-busy CPU0/1/2/3 [%] | 0,07 / 0,04 / 0,00 / 0,02 | 0,47 / 0,33 / 1,01 / 0,21 |
| Kontextwechsel/s | 3561 | 8445 |
| Interrupts/s | 3181 | 6210 |
| neue Prozesse in 600 s | 3 | 3 |
| SD-Schreibvorgaenge/s | 0,018 | 0,017 |
| Netz end0 / enu1 [Pak/s] | 0,9 / 7,4 | 505 / 361 |
| CPU2/3 haeufigste Wecker | syncAlsa 100/s (cpu3) | irq/104-eth 978/s (cpu2), syncAlsa 510/s (cpu2) + 500/s (cpu3) |
| CPU0/1 haeufigste Wecker | rcuog/2 468/s, ktimers/1 344/s, ktimers/0 257/s, ksoftirqd/0+1 je ~250/s | rcuog/2 459/s, RAATServer 375/s |

Laststufe CPU0 (sha256sum, 10 s an/aus, 14 Zyklen, bei Wiedergabe): Absenkung **25,2 mV** je voll belastetem Kern, Unterschwinger beim Einschalten zusaetzlich −11,7 mV, Ueberschwinger 2,4 mV, Flankenverzug ~0 ms (Zeitausrichtung bestaetigt). Korrelation Sekundenmittel V zu CPU0-Last r = −1,00.

Bewertung: System sehr ruhig (3 neue Prozesse/10 min, ~1 SD-Schreibvorgang/min). Wiedergabe verdoppelt die Spannungsschwankung (2,07 → 4,36 mV) ohne messbare CPU-Lastkorrelation je Sekunde → Ursache im Sub-Sekunden-Bereich (Netz-/USB-Aktivitaet). Kandidat fuer Test: USB-Netzwerkadapter ASIX AX88179B laeuft am USB-3-Port (5 Gbit/s) → Test am USB-2-Port.

## Ursachenanalyse Spannungsschwankung (vschwank.py 1.2, Host, 02.10.2026 20:53, 138/138 Pruefungen bestanden)

Kalibrierung: Frequenzgang H = 0,97-1,01 von 3,1 bis 313 Hz (K213b @47 Hz: 1,00), Sensorfenster T = 0,00 ms (Rest 1 %)
-> Sensor tastet momentan ab, 500-Hz-Effekte werden ungedaempft erfasst. Lastempfindlichkeit 24,3 mV/Kern (hostmess: 25,2).
Nachweisgrenze Spitzen: P50 1,18 mV, I50 0,52 mV. Drift: I50 4965,266 / I50m 4965,239 / I50b 4965,498 mV (~38 °C) - vernachlaessigbar.

| Bedingung | sd mV | Varianz >0,1 Hz mV² | Mittel rel. Ruhe mV |
|---|---|---|---|
| I50 Ruhe @50 | 2,23 | 4,86 | 0 |
| I47 Ruhe @47 | 2,48 | 6,13 | – |
| P50 Musik @50 | 4,93 | 24,11 | −5,45 |
| P47 Musik @47 | 6,68 | 44,19 | −4,67 |
| E50 end0-Nachbildung @50 | 4,81 | 18,87 | −3,67 |
| E47 end0-Nachbildung @47 | 5,01 | 24,93 | −3,80 |
| U50 USB-Netz TX @50 | 3,83 | 14,41 | −0,47 |
| C50 CPU-Wecker 500/s @50 | 4,72 | 21,76 | −1,00 |

Befunde:
- Diretta-Zyklus bestaetigt: end0 500,0 Pak/s × 1389 B (Zyklus 500,03 Hz). P47-Spitzen 16,98 Hz (= 500 Hz, 1,80 mV eff.) und
  13,04 Hz (= 1000 Hz), dazu 21,02/22,03 Hz. Quellsuche 499,93-500,10 Hz: 1,80 mV eff. (korrigiert 1,80, H = 1).
- E47 (kuenstlicher end0-Verkehr gleicher Rate/Groesse, ohne Musik) erzeugt dieselben Spitzen (16,98/13,04/22,21/20,84 Hz)
  -> der 500-Hz-Netzwerkzyklus selbst ist eine Hauptquelle. E47−I47 = 18,80 mV² ≈ 49 % von P47−I47 = 38,06 mV².
- 50/60/100/120/150 Hz: nicht vorhanden (Markierungen nur in P47, fallen exakt auf Diretta-Oberwellen-Aliase; P50 bei 10/20 Hz ohne Spitze).
- Einschraenkung: E50/C50 bei 500,04 Hz falten bei 50-Hz-Abtastung auf 0,04 Hz -> Schwebung im Band 0,1-0,5 Hz
  (2,78/3,49 mV gegen 0,33 mV Ruhe) blaeht deren Anteile auf (end0 73±19 %, CPU 88±43 %). Aussagekraeftig ist der 47-Hz-Vergleich.
- enu1 (305,6 Pak/s): keine Spitze; USB-Sendelast als Ersatz ~50±3 % Varianzanteil (breitbandig, keine Linie).
