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
| 02.10.2026 | Host | Test `CycleTime=2000`→`4000` (FlexCycle=enable): ohne Wirkung (weiter 500 Pak/s, 1555 B) → **zurückgesetzt** aus `/opt/diretta-alsa/setting.inf.vor-cycle4000` | – |

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

## Mechanismus und Zuleitungswiderstand (vschwank.py 1.3, Host, 03.10.2026 10:07, 162/167 Pruefungen)

Kalibrierung: H = 0,98-1,00 (3,1-313 Hz), T = 0,35 ms (H(500 Hz) 0,95 Modell), Lastempfindlichkeit 24,9 mV/Kern. Drift: I50 4966,910 /
I50m 4967,039 / I50b 4966,573 mV (36,0-36,7 °C).

| Groesse | Wert |
|---|---|
| Diretta-Zyklus (Quellsuche) | 499,93-500,11 Hz, end0 500,0 Pak/s × 1513 B |
| Linie 500 Hz in P47 | 1,58 ± 0,07 mV rms (×29 Ruhe) |
| Linie 1000 Hz in P47 | 2,20 ± 0,05 mV rms (×27 Ruhe) |
| Diretta-Rechenzeit je Zyklus (P50−I50, schedstat) | +28,4 µs (gesamt 30,6 µs) |
| C47 (CPU0-Wecker 28,4 µs) | 0,50 ± 0,03 mV (erwartet aus C47x 0,56 → linear) |
| C47x Positivkontrolle (199,8 µs) | 3,87 ± 0,04 mV (×174 Ruhe) |
| **Prozessor-Anteil an der 500-Hz-Linie** | **35 ± 2 %** (Rest Netzwerk-Hardware; E47: Hardware ~1,22 mV ≈ 77 %) |
| R50 Spannungshub je Kern | 24,46 ± 0,05 mV (Gegenprobe Kalibrierung ×0,98) |
| R50 Leistungshub PMIC-Schienen | 0,821 ± 0,005 W |
| **Zuleitungswiderstand Host (Tomanek + Kabel + Stecker + Pi-Eingang)** | **129 mΩ** (118-140 mΩ für Wirkungsgrad 95-80 %) |

FAIL-Pruefungen: I47 3 Abtastluecken (max 47 ms); R50 Vollstaendigkeit/Rate/Luecken (Ursache: jede PMIC-Abfrage blockiert den
Logger ~40 ms, 360 Luecken bei 360 Abfragen; fuer Halbperioden-Mittel unschaedlich -> Pruefung in 1.3.1 angepasst);
Ruhe I50b/I50 Varianz ×1,44 (I50b nur 15 s nach der Laststufe R50 -> 1.3.1 beruhigt 60 s).
Varianzanteile @50 Hz (E50/C50) weiter durch Schwebung 0,05 Hz verfaelscht - nicht verwenden; 60-Hz-„SPITZE“ in P47 = 1000-Hz-Alias.

## Zuleitungswiderstand Target (vschwank.py 1.3.1 widerstand, 03.10.2026 11:14, 11/11 Pruefungen)

Target (Pi 5 Rev 1.0), iFi iPower Elite 5 V/5 A, fest verbautes DC-Kabel 1,5 m + Hohlstecker→USB-C-Adapter:
Spannungshub 22,37 ± 0,22 mV je Kern, Leistungshub **0,947 ± 0,008 W**, EXT5V 5,087 V →
**R = 105 mΩ** (96-114 mΩ für Wirkungsgrad 95-80 %). Keine Abtastluecken trotz PMIC-Abfrage (anders als Host).
Vergleich Host (Tomanek): 129 mΩ (118-140). Host-Tomanek-Thema beendet: Host erhaelt ebenfalls ein iFi Elite 5 V/5 A.

## Target-Ursachenanalyse (vschwank.py 1.4 target, 03.10.2026 16:18, 116/119 Pruefungen)

Target Pi 5 Rev 1.0, iFi iPower Elite 5 V/5 A, Diretta-Empfang end0 500,1 Pak/s × 1537 B.

| Bed. | sd mV | Varianz >0,1 Hz mV² | Mittel V |
|---|---|---|---|
| P50 Musik | 4,36 | 18,14 | 5,0941 |
| P47 Musik | 5,19 | 26,73 | 5,0931 |
| I50 Ruhe | 3,03 | 9,14 | 5,0986 |
| I47 Ruhe | 2,82 | 7,92 | 5,0984 |
| I50b Ruhe (Ende) | 2,68 | 6,53 | 5,0977 |

- **500-Hz-Linie klein: 0,43 ± 0,06 mV** (Host: 1,58). Diretta-Rechenzeit +18,2 µs/Zyklus; erwartet aus Positivkontrolle
  C47x 0,50 mV = 116 ± 17 % → **am Target erklaert das CPU-Aufwachen die 500-Hz-Linie vollstaendig**. 1000 Hz: 0,94 mV (nicht signifikant).
- Neue Linie nur bei Musik: **~9,6 Hz** (P47 9,64 Hz 1,96 mV ×30 Ruhe; P50 9,57/10,45 Hz) - gleiche Frequenz bei 50 und 47 Hz
  Abtastung → echte langsame Quelle, kein Alias. Hypothese (ungeprueft): ALSA-Periode am Target (96000/9,6 = 10000 Frames).
- In Ruhe dauerhaft Anteil nahe Vielfachen von 50 Hz (Band 0,1-0,5 Hz @50 Hz ~1,7-2,0 mV, @47 Hz nur 0,27 mV; I47 bei 13,0 Hz
  ×8 Umgebung) → Kandidat 1-kHz-Takt (USB-Rahmen), ungeprueft.
- **Frequenzgang der Versorgung steigt**: Antwort auf identische Laststufe K7 8,97 → K31 9,19 → K113 10,56 → K213b 14,30 →
  K313 22,06 mV (×2,5 von 7 auf 313 Hz). Am Host (Tomanek) flach (0,98-1,00). → Innenwiderstand iFi + Zuleitung waechst mit der
  Frequenz; Sensor-Modell daher nicht anwendbar (Restfehler 43 %, FAIL korrekt).
- Zuleitungswiderstand (quasi Gleichstrom) R50: Hub 19,66 ± 0,04 mV, 0,927 W → **94 mΩ** (86-103); Mittag (11:14, evtl. mit Musik) 105 mΩ.
- FAIL: Modell (s.o.); R50-Gegenprobe gegen Modell (in 1.4.1 auf tiefste Kalibrierfrequenz umgestellt: 19,9 mV → ×0,99);
  Ruhe-Varianz I50b/I50 ×0,71 (I50 direkt nach Musikstopp erhoeht).

## Frequenzgang der Versorgung am Target (vschwank.py 1.4.5 target, 03.10.2026 19:13, Kalibrierung gueltig)

Gemessene Antwort auf identische Laststufe (Bezug Gleichstrom R50 = 20,42 ± 0,13 mV/Kern, R = 98 mΩ; Logger-Zeitstreuung 0,03 ms):

| f [Hz] | 7,3 | 31 | 113 | 213 (@47: 1,57) | 313 | 513 | 1013 |
|---|---|---|---|---|---|---|---|
| Antwort | 1,00 ± 0,01 | 1,00 ± 0,02 | 1,17 | 1,59 | **2,34** | 1,21 | 1,00 |
| ≈ \|Z\| [mΩ] | 98 | 98 | 114 | 156 | **230** | 119 | 98 |

→ Innenwiderstand iFi + Zuleitung hat eine **Resonanzspitze zwischen ~300 und 500 Hz** (×2,3), bei 1 kHz wieder Gleichstromwert.
Antwort bei 500 Hz (interpoliert) 1,27. Ruhe: Linie 12,94 Hz @47 (×46) → Quelle nahe 1 kHz dauerhaft. Musik: 9,64 Hz (2,04 mV) reproduziert.
Musik-Teil dieses Laufs ungueltig (P50 nur 23 % Wiedergabe → Zyklus 117,7 statt 500 Pak/s erkannt, C47 entfallen);
ab 1.4.6 wird eine Teilmessung bei Zustandswechsel automatisch wiederholt.

## Target Musik-/Mechanismus-Teil (vschwank.py 1.4.6 target --ohne-kal, 04.10.2026 12:47, 66/66 Pruefungen)

Diretta-Empfang 500,1 Pak/s × 1537 B. P50 sd 5,85 mV / Varianz 28,66 mV²; I50 3,14 / 8,82 → **Zusatzvarianz Wiedergabe 19,8 ± 3,5 mV²**.
- 500-Hz-Linie (P47) 0,67 ± 0,08 mV (×6 Ruhe); Diretta-Rechenzeit +18,2 µs/Zyklus; erwartet aus C47x 0,53 mV = **78 ± 9 % → Prozessor
  dominiert** (03.10.: 116 ± 17 %). 1000 Hz 0,95 mV nicht signifikant. Kalibrierte Antwort bei 500 Hz 1,27 (Lauf 03.10. 19:13).
- **Neu: langsame Komponente 0,38 Hz nur bei Musik** (P50 0,39 Hz 2,80 mV, P47 0,37 Hz 1,20 mV; bei beiden Raten gleich → echt, ~2,6 s Periode).
- Linie P47 9,64 Hz (2,06 mV) + P50 20,41/19,63 Hz: Quellsuche findet **479,55-479,68 Hz** (Alias 20,38 @50 / 9,62 @47) →
  wahrscheinlich Quelle ~480 Hz bei Wiedergabe (Ursache offen; Kandidat ALSA-/USB-Periodenrhythmus: 96000/200 = 480).
- Ruhe: 12,94 Hz @47 (×37) wieder vorhanden (Quelle nahe 1 kHz).
- Gegenprobe ALSA am Target (96 kHz, laufend): period_size 480 Frames, buffer 1920 (4 Perioden) → **Periodenrhythmus 200 Hz**,
  nicht 480 Hz → Vermutung „480 Hz = ALSA-Periode“ **widerlegt**. Herkunft der ~480-Hz-Linie und der 0,38-Hz-Komponente offen
  (moeglich: Schwebung zwischen 200-Hz-ALSA-Takt (DAC-Takt) und 500-Hz-Diretta-Takt (Pi-Takt); pruefbar mit 48-kHz-Wiedergabe).

## Unterbrechungen Target (kernrausch.py 1.0, 04.10.2026 13:34, passiv; osnoise/timerlat im Kernel NICHT vorhanden)

| CPU | Ruhe IRQ/s (Hauptquellen) | Musik IRQ/s (Hauptquellen) | Musik belegt |
|---|---|---|---|
| 0 | 1545 (arch_timer 1070, IPI 454) | 1492 | 0,10 % |
| 1 | 1305 (arch_timer 843, IPI 461) | 1264 | 0,03 % |
| 2 | 123 (IPI-Resched 114, Tick 8) | **734 (IPI-Resched 730)** | 0,33 % |
| 3 | **517 (xhci-USB 500, end0 3)** + Softirq HI 500 | **3083 (xhci 2050, end0 1020)** + Softirq HI 2050, NET_RX 515 | 4,88 % |

- CPU2 (Diretta): Musik `diretta_app_target` FIFO99 500,3 Akt./s (Netz-Zyklus) + 200,0 Akt./s (ALSA-Periode 480 Frames) - Ruhe 100 Akt./s.
  Weckungen kommen per IPI von CPU3 (IRQ dort, Thread auf CPU2): 730/s.
- CPU3 (IRQs): USB-Controller xhci **auch ohne Musik 500 IRQ/s** (irq-Thread 6 ms/s) → erklaert die dauerhafte Ruhe-Linie nahe
  1 kHz (12,94 Hz @47 = Oberwelle 2×500) und den 0,1-0,5-Hz-Anteil @50 Hz. Bei Musik 2050 IRQ/s, 45 ms/s Rechenzeit.
- nohz_full wirkt: CPU2 Ruhe nur 8 Timer-IRQ/s.

## Unterbrechungen Host (kernrausch.py 1.0, 04.10.2026 13:54, passiv; osnoise/timerlat im Kernel NICHT vorhanden)

| CPU | Ruhe IRQ/s | Musik IRQ/s (Hauptquellen) | Musik belegt |
|---|---|---|---|
| 0 | 1552 | 1984 (arch_timer 1276, IPI 601, xhci 85) | 0,75 % |
| 1 | 1315 | 1747 | 0,52 % |
| 2 | **2** | **1518 (end0 1010, IPI-Resched 397, Tick 105)** + NET_RX 505 | 1,02 % |
| 3 | 102 (IPI 93) | 602 (IPI-Resched 597) | 0,22 % |

- CPU2 bei Musik: `irq/104-eth` FIFO90 990 Akt./s 3,4 ms/s + `syncAlsa` FIFO99 510 Akt./s 8,0 ms/s → zwei Echtzeit-Threads auf
  einem Kern → Timer-Tick kehrt zurueck (105/s statt 0) = Kandidat A/B „end0-IRQ auf CPU3“ (wie am Target getrennt).
- CPU3 bei Musik: `syncAlsa` RR10 500 Akt./s + FIFO80 100 Akt./s (auch in Ruhe 100/s); Weckungen per IPI 597/s.
- Ruhe: CPU2 praktisch still (2 IRQ/s), CPU3 nur syncAlsa 100/s.
