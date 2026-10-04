# Übergabe: Projekt „Netzteil und Spannungen"

Stand: 29.09.2026 · Ablösung/Überarbeitung von `uebergabe-diretta-psu.md` (v1.1) · Sprache: Deutsch, copy-paste-fertige Befehle.

## 1. Ziel

Für **jedes** Gerät der Wiedergabe- und Netzwerkkette die optimale Konfiguration und bestmögliche Spannungsstabilität – bis ins Detail. Jede Änderung wird **vor/nach** gemessen; ohne Messung gilt sie nur als Erwartung.

## 2. System

| Gerät | Rolle | Stand |
|---|---|---|
| `diretta-host` (192.168.178.66) | Raspberry Pi 5, AudioLinux, Diretta Host | Netzteil-Messung vorhanden (Ref.) |
| `diretta-target` | Raspberry Pi 5, AudioLinux, Diretta Target → DAC (Eastern Electric Minimax Supreme) | **Schwachpunkt**, iFi iPower Elite 5 V/5 A geplant |
| Intel NUC8i7BEH | Roon Server, ROCK OS | **noch nicht erfasst** (Netzteil, BIOS, Messung offen) |
| Netzwerkgeräte | Router/Switch etc. | **nicht inventarisiert** |

- `end0` Host↔Target: direkte Punkt-zu-Punkt-Verbindung (galvanisch getrennt durch PHY-Übertrager).
- Kernaufteilung Target (nicht ändern): Core 2 = Audio + USB-IRQ 134, Core 3 = Netzwerk-IRQ 104, Core 0/1 = Rest. Services `end0-irq-core3.service` und `diretta-thread-pin.service` **nie entfernen**. Diretta: `ThredMode=16`, `CycleTime=2000`, `InfoCycle=200000`, `CpuSend=2`, `CpuOther=3` (nach jedem Purist-Toggle prüfen).
- Kernel `7.1.8-1-rpi RT LTO`, Snyder-QA 94/94.

## 3. Messdaten 5-V-Schiene (EXT5V_V, eigene Messung, Werkzeug `~/ext5v/`, ADC ±1,5 %)

| Größe | Host | Target |
|---|---|---|
| Mittel Idle | 4,967 V | 5,090–5,100 V |
| sd Idle | 2,0–2,1 mV | 4,3–5,1 mV |
| 1-Kern-Last | −25 mV | −76…−88 mV |
| Burst sd | 12,7 mV | 38,9 mV |
| Überschwinger | keiner | +13 mV |
| Wiedergabe 96 kHz/32 Bit | ±0,4 mV | Mittel −19 mV, **Min −122 mV** |

Schluss: Target-Netzteil regelt 2–3× schlechter, Nachschwingen, langsame Eigenschwingung. Modell/Hersteller des Target-Netzteils: **nie genannt – offen**.

## 4. Entscheidungen

- iFi iPower Elite → **Target** (Messdaten, Diretta-Beschreibung „player with analog part").
- Anschluss: **passives** Barrel→USB-C-Kabel (nur VBUS/GND, 5,5×2,1 mm, Mitte +) + `PSU_MAX_CURRENT=5000` im EEPROM. GPIO und PD-Adapter verworfen.
- Vorbehalt: iFi-„<1 µV Rauschen" ist Ripple, nicht Lastregelung → Erfolg nur per Nachmessung belegt.

## 5. Skripte `vorbereiten.sh` + `psu_eeprom.sh` v1.3

**Befund 29.09.2026 (echte Hardware):** `check` brach ab mit „Befehl fehlt: rpi-eeprom-config" – AudioLinux hat das Raspberry-Pi-Paket `rpi-eeprom` nicht installiert. Das Target ist nur über den Host erreichbar (172.20.0.2).

**Lösung v1.3:**
- `vorbereiten.sh` läuft auf dem **Host**: liest die Bootloader-Version des Targets, lädt die offiziellen Werkzeuge (`rpi-eeprom-config/-update/-digest`, `recovery.bin`) und **genau das passende Bootloader-Image** von `raspberrypi/rpi-eeprom` (feste Version `ded1e93`, 91 Pi-5-Images 2023–2026, jede Datei per SHA256 geprüft), kopiert alles nach `~/rpi-eeprom/` auf dem Target und startet dort `check`.
- `psu_eeprom.sh` nutzt dieses Bündel automatisch (prüft dessen Prüfsummen), meldet alle fehlenden Programme auf einmal, ersetzt `lspci` (nur Pi 4 relevant) und ggf. `strings` (über python3).
- Ist die laufende Version nicht in der Liste, wird das neueste Standard-Image genommen und **vor dem Schreiben ausdrücklich** auf das zusätzliche Bootloader-Update hingewiesen.

**Getestet** in einem nachgebauten Pi 5 (Device-Tree, Boot-Partition, vcgencmd simuliert; Werkzeuge und Images echt): vorbereiten → check → apply → Neustart-Simulation → verify (5000 mA) → rollback → verify: alles bestanden. Ebenso: fehlendes Bündel, manipuliertes Bündel, Version nicht in Liste (Update-Pfad), „Neustart ausstehend". shellcheck: 0 Befunde. Auf der realen Hardware nur bis zum Werkzeug-Abbruch gelaufen.

SHA256 `psu_eeprom.sh`: `4b6df313161bd4877b6a5b3ce48c1f4150f68f6735fe54a5eded67e2c5863326` (wird von `vorbereiten.sh` automatisch geprüft).

### Ablauf (auf dem Host, Wiedergabe vorher stoppen)

```bash
cd ~ && curl -fsSLO https://raw.githubusercontent.com/BlondStylist/Diretta-Test/claude/zealous-ride-1fn94u/vorbereiten.sh && bash vorbereiten.sh
# nur bei "CHECK BESTANDEN":
ssh -t diretta-target 'sudo bash ~/psu_eeprom.sh apply'   # JA eingeben
ssh diretta-target 'sudo reboot'                          # ~2 Min, Strom nicht trennen
ssh -t diretta-target 'sudo bash ~/psu_eeprom.sh verify'  # VERIFY BESTANDEN, max_current 5000 mA
```

Ohne `flashrom` (wahrscheinlich bei AudioLinux) wird das Update auf `/boot` abgelegt und beim Neustart vom Bootloader geschrieben; danach liegt `RECOVERY.000` auf `/boot` (harmlos).
Rollback: `sudo bash ~/psu_eeprom.sh rollback /root/config-archiv/eeprom-<Zeit>-<PID>`.

## 6. Ergebnisse 30.09.2026 (echte Hardware)

- iFi iPower Elite 5 V/5 A am Target, passives Barrel→USB-C-Kabel.
- `vorbereiten.sh`: Target-Bootloader 2026-09-12 (1789171628), exaktes Image gefunden, `check` bestanden. Kein `flashrom` → Update über `/boot`.
- `apply` → Reboot → `verify`: **BESTANDEN**, Bootloader-Version unverändert, device-tree `max_current` = **5000 mA**. Backup: `/root/config-archiv/eeprom-20260930_132830-4651`.
- Schnellmessung mit `vcgencmd pmic_read_adc EXT5V_V` (300 Werte à 0,2 s, **vor** EEPROM-Änderung; grober als `ext5v`, kurze Einbrüche können fehlen):

| | altes Netzteil (ext5v) | iFi Ruhe | iFi Wiedergabe |
|---|---|---|---|
| Mittel | 5,090–5,100 V | 5,012 V | 5,004 V |
| sd | 4,3–5,1 mV | 4,7 mV | 7,6 mV |
| Minimum rel. Ruhe-Mittel | −122 mV (Wiedergabe) | −18 mV | −38 mV |
| `get_throttled` | – | 0x0 | 0x0 |

- **Unterspannung beim Boot nach EEPROM-Flash:** `throttled=0x50000`, dmesg „Undervoltage detected" bei 5,94 s, „normalised" bei 7,99 s. **Normaler Reboot danach: `0x0`, kein Eintrag** → einmalig, vermutlich Flash-Boot. Beobachten: Kaltstart (Strom aus/an) noch nicht geprüft. iFi liefert 5,0 V (altes NT 5,1 V) → weniger Reserve; Kabellänge/-querschnitt ist der Hebel, falls es wiederkehrt.

### ext5v-Messreihe 30.09.2026 (50 Hz, Logger auf Core 1, ohne künstliche Last, PSU_MAX_CURRENT=5000)

Referenz altes Netzteil = Paar `p3-idle`/`p2-play` vom 28.09. (600 s); iFi = `ifi-idle`/`ifi-play` (300 s). Auswertung mit `ext5v_stats.py` v2.0, alle Qualitätsprüfungen PASS (`throttled=0x50000` = Boot-Merker, s. o.).

| Größe | alt Ruhe | iFi Ruhe | alt Wiedergabe | iFi Wiedergabe |
|---|---|---|---|---|
| Mittel | 5,1019 V | 5,0194 V | 5,0830 V | 5,0086 V |
| sd | 4,30 mV | **3,30 mV** | 7,93 mV | **6,54 mV** |
| P99,9−P0,1 | 29,5 mV | **21,4 mV** | 52,3 mV | **42,9 mV** |
| Spitze-Spitze | 46,9 mV | 36,2 mV | 155,4 mV | **67,0 mV** |
| Mittel-Absenkung ggü. Ruhe | – | – | −18,9 mV | **−10,8 mV** |
| P0,1 ggü. Ruhe-Mittel | – | – | −59,5 mV | **−45,3 mV** |
| Minimum ggü. Ruhe-Mittel | – | – | −159,9 mV | **−66,7 mV** |
| ADEV τ=1,28 s | 1,67 mV | 1,78 mV | 4,34 mV | **2,08 mV** |
| ADEV τ=10 s | 2,18 mV | **0,78 mV** | 2,25 mV | 2,34 mV |

**Bewertung:** iFi in allen audio-relevanten Größen besser: Ruhe-sd −23 % (Ziel < 4 mV erfüllt), Wiedergabe-sd −18 %, tiefster Einbruch bei Wiedergabe −60 %, langsame Eigenschwingung (τ 2,5–10 s) in Ruhe weitgehend verschwunden. Ziel „Einbruch ≤ −50 mV" für 99,9 % der Werte erfüllt (−45 mV), Einzelminimum −67 mV knapp darüber. Absolutspannung ~80 mV niedriger (iFi fest 5,0 V), mit > 300 mV Abstand zur Unterspannungsschwelle unkritisch. **Entscheidung: iFi am Target behalten.** Kabel (1,5 m + Barrel/USB-C-Adapter) fest verbaut, nicht tauschbar.

## 7. Nächste Schritte

1. Kaltstart geklärt: Unterspannung bei ~6 s nach jedem Kaltstart durch Einschaltstrom der USB-Kette (iFi OptiLink am USB-A); ohne USB-Kette `0x0`, Warmstart `0x0`. Nutzerentscheidung: so belassen (kein Einfluss auf Wiedergabe). Echte Betriebs-Einbrüche über `dmesg | grep -i undervoltage` erkennen (nur Einträge bei 6–8 s = Boot).
2. Erledigt: ext5v-Messreihe mit iFi (siehe oben) → iFi behalten.
3. Optional: 600-s-Läufe für exakte Laufzeit-Gleichheit mit der Referenz.
4. Modell des bisherigen Target-Netzteils erfragen.

## 8. Geräteweise Detailoptimierung (Kandidaten, jeweils vorher/nachher messen)

**Beide Pi 5 (Host und Target):**
- `vcgencmd get_throttled` (soll `0x0`), `vcgencmd pmic_read_adc` (EXT5V_V, Grundlage `ext5v`).
- `config.txt`: unbenutzte Funktionen aus (`dtoverlay=disable-wifi`, `dtoverlay=disable-bt`, HDMI/Audio-Overlays, LEDs), `usb_max_current_enable` nach Ergebnis von `verify` und DAC-Bus-Bedarf bewerten.
- CPU-Governor fest (kein Taktwechsel = keine Lastsprünge) – gegen Messung prüfen.
- EEPROM-Restwerte (`rpi-eeprom-config`) auf überflüssige Einträge prüfen (`BOOT_UART`, `NET_INSTALL_AT_POWER_ON` etc.).

**Intel NUC8i7BEH (Roon ROCK):** offen. Zu klären: Netzteil (Original 19 V) und Steckdosenumfeld; BIOS: unbenutzte Geräte (WLAN/BT, HDMI-Audio, LEDs) aus, Energiesparzustände bewusst wählen. ROCK ist ein Appliance-OS – nur BIOS/Hardware ändern, kein Eingriff ins System. Kein Messwert vorhanden.

**Netzwerk:** Netzteile aller aktiven Geräte (Router/Switch/Medienkonverter) inventarisieren; nur Geräte im Diretta-Pfad priorisieren. Direktstrecke Host↔Target bleibt.

## 9. Unsicher / unbestätigt

- Skript nicht auf echter Hardware gelaufen; AudioLinux-Besonderheiten (Paket `rpi-eeprom`, `flashrom`, `strings`, `lspci`) unverifiziert – `check` prüft das zuerst.
- DAC bus-power-unkritisch: Annahme.
- iFi verbessert Lastregelung: Erwartung, kein Beleg.
- Zeitliche Feinstruktur der Target-Last nie direkt gemessen.
- Ob `max_current` im Device-Tree den `PSU_MAX_CURRENT`-Wert exakt widerspiegelt, ist nicht belegt (daher nur Warnung, kein Fehler).
