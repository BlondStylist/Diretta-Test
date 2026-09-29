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

## 5. Skript `psu_eeprom.sh` v1.2 (Review-Ergebnis)

Datei: `psu_eeprom.sh` in diesem Repo. **SHA256:** `5844d868353633c7f60e3bd79252303dea0c546c0673ecea19a8909048174511`

Gegen den echten Upstream-Code (`raspberrypi/rpi-eeprom`, Stand 09/2026) und ein echtes Pi-5-Image (`pieeprom-2026-09-25.bin`) geprüft: `-d -i -f` ist die von Upstream selbst genutzte Aufrufform; shellcheck 0 Befunde; 5 Konfigurationsvarianten (mit/ohne `[all]`, vorhandener Wert, UTF-8, nur bedingte Sektion) im Roundtrip mit dem echten `rpi-eeprom-config` bestanden. **Weiterhin nie auf der realen Hardware gelaufen.**

Korrekturen gegenüber v1.1:

1. **UTF-8 in Kommentaren** (z. B. „ü") brach ab, weil die Binärprüfung unter `LC_ALL=C` jedes Byte ≥ 0x80 ablehnte → jetzt nur echte Steuerzeichen.
2. **`findmnt | tr | grep -q` unter `pipefail`**: potenzielles SIGPIPE-Fehlurteil („read-only") → ohne Pipe.
3. **`verify`**: Referenz-Backup war „alphabetisch letztes mit APPLIED"; ein Rollback wurde nur im selben Ordner erkannt → jetzt neuester Marker (APPLIED/ROLLED_BACK) über alle Backups nach Zeitstempel.
4. **`verify`**: warnt jetzt, wenn Device-Tree `max_current` ≠ 5000 mA.
5. **Sofort-Update**: Upstream schreibt auf dem Pi 5 per `flashrom` **sofort**, wenn `flashrom` installiert ist (Standard `RPI_EEPROM_IMMEDIATE_UPDATE=1`). Das Skript meldet jetzt, welcher Modus greift; Neustart bleibt in beiden Fällen nötig.
6. **Image-Mangel**: Bei fehlendem passendem Image werden alle vorhandenen Images samt Timestamp gelistet. Hinweis: Das Paket enthält aktuell Images bis **2026-09-25**; das „Juni-2024-Image" der Vorversion war nur eine Vermutung – der laufende Bootloader des Targets ist unbekannt, `check` zeigt ihn.

### Ablauf (Wiedergabe vorher stoppen)

```bash
scp psu_eeprom.sh diretta-target:psu_eeprom.sh
ssh diretta-target 'sha256sum ~/psu_eeprom.sh'      # muss die Summe oben zeigen, sonst STOP
ssh -t diretta-target 'sudo bash ~/psu_eeprom.sh check'   # ändert nichts
# nur bei "CHECK BESTANDEN":
ssh -t diretta-target 'sudo bash ~/psu_eeprom.sh apply'   # fragt JA ab
ssh diretta-target 'sudo reboot'                          # ~2 Min, Strom nicht trennen
ssh -t diretta-target 'sudo bash ~/psu_eeprom.sh verify'  # erwartet VERIFY BESTANDEN, max_current 5000 mA
```

Rollback: `sudo bash ~/psu_eeprom.sh rollback /root/config-archiv/eeprom-<Zeit>-<PID>`.
Bei `ABBRUCH` (besonders „kein Image passend"): komplette Ausgabe melden – es wurde nichts geändert.

## 6. Nächste Schritte

1. `check` auf dem Target ausführen, Ausgabe auswerten.
2. `apply` → Reboot → `verify`.
3. iFi über passives Kabel anschließen.
4. Messbatterie aus Abschnitt 3 wiederholen (Idle 300 s, 1-Kern, Burst, echte Wiedergabe).
5. Behalten nur bei: Idle-sd < 4 mV, Einbruch bei Wiedergabe > −50 mV, kein Überschwinger. Sonst iFi am Host testen oder zurückbauen.
6. Modell des bisherigen Target-Netzteils erfragen.

## 7. Geräteweise Detailoptimierung (Kandidaten, jeweils vorher/nachher messen)

**Beide Pi 5 (Host und Target):**
- `vcgencmd get_throttled` (soll `0x0`), `vcgencmd pmic_read_adc` (EXT5V_V, Grundlage `ext5v`).
- `config.txt`: unbenutzte Funktionen aus (`dtoverlay=disable-wifi`, `dtoverlay=disable-bt`, HDMI/Audio-Overlays, LEDs), `usb_max_current_enable` nach Ergebnis von `verify` und DAC-Bus-Bedarf bewerten.
- CPU-Governor fest (kein Taktwechsel = keine Lastsprünge) – gegen Messung prüfen.
- EEPROM-Restwerte (`rpi-eeprom-config`) auf überflüssige Einträge prüfen (`BOOT_UART`, `NET_INSTALL_AT_POWER_ON` etc.).

**Intel NUC8i7BEH (Roon ROCK):** offen. Zu klären: Netzteil (Original 19 V) und Steckdosenumfeld; BIOS: unbenutzte Geräte (WLAN/BT, HDMI-Audio, LEDs) aus, Energiesparzustände bewusst wählen. ROCK ist ein Appliance-OS – nur BIOS/Hardware ändern, kein Eingriff ins System. Kein Messwert vorhanden.

**Netzwerk:** Netzteile aller aktiven Geräte (Router/Switch/Medienkonverter) inventarisieren; nur Geräte im Diretta-Pfad priorisieren. Direktstrecke Host↔Target bleibt.

## 8. Unsicher / unbestätigt

- Skript nicht auf echter Hardware gelaufen; AudioLinux-Besonderheiten (Paket `rpi-eeprom`, `flashrom`, `strings`, `lspci`) unverifiziert – `check` prüft das zuerst.
- DAC bus-power-unkritisch: Annahme.
- iFi verbessert Lastregelung: Erwartung, kein Beleg.
- Zeitliche Feinstruktur der Target-Last nie direkt gemessen.
- Ob `max_current` im Device-Tree den `PSU_MAX_CURRENT`-Wert exakt widerspiegelt, ist nicht belegt (daher nur Warnung, kein Fehler).
