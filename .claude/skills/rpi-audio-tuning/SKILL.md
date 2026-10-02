---
name: rpi-audio-tuning
description: Tuning von Raspberry Pi 5 als Diretta-Host/-Target unter AudioLinux (RT-Kernel) - IRQ-Affinitaet, isolcpus/nohz_full/rcu_nocbs, CPU-Governor, RT-Prioritaeten, Diretta-Thread-Zuordnung, Netzteil/EEPROM und die bekannten Fallstricke dieses Setups. Verwenden bei jeder Aenderung an Kernel-Parametern, config.txt, IRQ-/Thread-Zuordnung, Diretta-setting.inf, Netzwerkschnittstellen oder Stromversorgung der Pis.
---

# rpi-audio-tuning

Ziel: minimale, gleichmaessige Last und Unterbrechungen auf den Audio-Kernen, stabile 5-V-Versorgung.
Grundregel: **eine Aenderung zur Zeit, vorher und nachher mit demselben Werkzeug messen** (siehe Skill
`audio-measurement`), jede Aenderung mit Rueckweg ins `Messprotokoll.md` (Abschnitt Aenderungsprotokoll).

## 1. Ist-Zustand zuerst erheben (nichts aendern)

```bash
sudo bash ~/diagnose.sh          # Repo-Skript: Strom, Governor, EEPROM, config.txt, Funk, Netz, IRQs, Dienste, Diretta
```
Gemessener Stand 30.09./02.10.2026 (Host `diretta-host`, Pi 5 Rev 1.1):
- `cmdline`: `irqaffinity=0,1 nohz_full=2,3 rcu_nocbs=2,3 rcu_nocb_poll` (kein `isolcpus`, `isolated` leer)
- Governor `performance`, alle Kerne fest 2,4 GHz (`arm_boost=1`), `cpuidle` = none
- IRQ 104 `end0` Affinitaet 2-3 (laeuft auf CPU2), xhci 129/134 auf 0-1
- Diretta Host `setting.inf`: `ThredMode=17 CycleTime=2000 FlexCycle=enable InfoCycle=200000 CpuSend=2 CpuOther=3`
- `end0` 10 Mbit/s (Super Purist, absichtlich), EEE aus, Coalescing 0 (`end0-coalesce-zero.service`), MTU 9000
Target `diretta-target`: Core 2 = Audiothread + USB-IRQ 134, Core 3 = Netz-IRQ 104, Core 0/1 = Rest,
`ThredMode=16 CycleTime=2000 InfoCycle=200000 CpuSend=2 CpuOther=3`.

## 2. Stellschrauben (Befehle, alle umkehrbar)

| Thema | Lesen | Aendern (root) | Dauerhaft |
|---|---|---|---|
| IRQ-Affinitaet | `cat /proc/irq/N/smp_affinity_list`, `grep -E 'end0|xhci' /proc/interrupts` | `echo 3 > /proc/irq/N/smp_affinity_list` | eigener systemd-Dienst (wie `end0-irq-core3.service`) |
| Standard-IRQ-Kerne | `cat /proc/cmdline` | `irqaffinity=0,1` in `/boot/cmdline.txt` | Reboot |
| Kernisolation | `cat /sys/devices/system/cpu/{isolated,nohz_full}` | `nohz_full=` / `rcu_nocbs=` / `isolcpus=` in cmdline | Reboot |
| Governor/Takt | `cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_{governor,cur_freq}` | `echo performance > .../scaling_governor` | AudioLinux-Menue bzw. Dienst |
| RT-Prioritaet | `ps -eLo pid,tid,cls,rtprio,psr,comm | grep -iE 'diretta|sync|irq/'` | `chrt -f -p 80 TID` | Dienst/Unit `CPUSchedulingPriority=` |
| Thread-Kern | `taskset -cp TID` | `taskset -cp 2 TID` (root) | `diretta-thread-pin.service`, `CpuSend/CpuOther` |
| Netz | `ethtool end0`, `ethtool --show-eee end0`, `ethtool -c end0` | `ethtool -s end0 ...`, `ethtool -C end0 rx-usecs 0` | networkd/Dienst |

Unterschiede kurz: `isolcpus` nimmt Kerne aus dem Scheduler (nur explizit gebundene Tasks laufen dort);
`nohz_full` stoppt den Timer-Tick auf Kernen mit genau einem Task; `rcu_nocbs` verlagert RCU-Callbacks auf
andere Kerne, `rcu_nocb_poll` laesst die `rcuog`-Threads pollen statt geweckt zu werden (entlastet die
Audio-Kerne, erzeugt aber Weckvorgaenge auf CPU0/1 - gemessen `rcuog/2` ~460/s).

## 3. Bekannte Fallstricke dieses Setups (alle real aufgetreten oder gemessen)

1. **Purist-Toggle setzt `ThredMode` zurueck** (Web-UI) - danach `ThredMode=16` (Target) pruefen.
2. **Eigene Dienste nie als Altlast entfernen:** `end0-irq-core3.service`, `diretta-thread-pin.service`, `end0-coalesce-zero.service`.
3. **Normale Benutzerprozesse duerfen nicht auf CPU2/3** (`taskset -c 2` schlaegt fehl) - Mess-/Lastwerkzeuge auf CPU0/1 oder als root.
4. **USB-Netzwerkadapter-Name haengt von der Buchse ab** (`enu1` = untere blaue USB-3-Buchse am Host). Umstecken => anderer Name =>
   networkd/avahi greifen nicht => Host unerreichbar. Vor einem Umstecken die `.network`-Datei auf `MACAddress=` umstellen (mit Sicherung).
5. **AudioLinux hat kein `rpi-eeprom`:** EEPROM nur ueber `vorbereiten.sh` + `psu_eeprom.sh` aus diesem Repo aendern (check -> apply -> reboot -> verify).
6. **`PSU_MAX_CURRENT=5000` erlaubt 1,6 A an USB:** am Target fuehrt der Einschaltstrom der USB-Kette (OptiLink) beim Kaltstart zu
   Unterspannung bei ~6 s (`throttled=0x50000`, dmesg „Undervoltage detected"). Warmstart ohne. Im Betrieb kein Einbruch.
7. **`get_throttled` Bits 16-19 sind „seit Boot"** - echte Betriebs-Einbrueche nur ueber `dmesg | grep -i undervoltage` (Zeitstempel) beurteilen.
8. **10 Mbit/s auf `end0` begrenzt die Formate** (96 kHz/32 Bit ~6 Mbit/s passt, 192 kHz/32 nicht).
9. **`syncAlsa` (Diretta) laeuft auch ohne Musik** (100/s auf CPU3), bei Wiedergabe ~500/s je Audio-Kern; `irq/104-eth` ~980/s auf CPU2.
10. **Host-Netzteil darf nicht geteilt werden** (Tomanek speiste frueher zusaetzlich den OptiLink).
11. **`config.txt`/`cmdline.txt` wirken erst nach Reboot**, vorher Sicherung (`cp /boot/config.txt /boot/config.txt.vor-<thema>`).
12. Kamera-Erkennung ist am Host aus, Display-Erkennung und `avahi-daemon` bewusst an (.local-Namen werden genutzt), `serial-getty@ttyAMA10` maskiert.

## 4. Vorgehen bei jeder Aenderung

1. Ist-Wert lesen und notieren (Tabelle oben).
2. Referenzmessung: `python3 ~/hostmess.py run` (oder gezielt `python3 ~/vschwank.py run`).
3. Genau eine Aenderung, Sicherung, Rueckweg notieren.
4. Gleiche Messung wiederholen, `python3 ~/vschwank.py vergleich A B` bzw. `kurz.txt` vergleichen.
5. Nur behalten, wenn die Verbesserung groesser als der Standardfehler ist; sonst zurueck.
