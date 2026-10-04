#!/usr/bin/env bash
# diagnose.sh v1.0 - reine Bestandsaufnahme (aendert NICHTS) fuer Raspberry Pi 5 mit AudioLinux.
# Aufruf: sudo bash diagnose.sh     Ausgabe zusaetzlich in ~/diagnose_<host>_<zeit>.txt
export LC_ALL=C
OUT="${SUDO_USER:+/home/$SUDO_USER}"; OUT="${OUT:-$HOME}/diagnose_$(hostname)_$(date +%Y%m%d_%H%M%S).txt"
h(){ echo; echo "== $*"; }
has(){ command -v "$1" >/dev/null 2>&1; }
vc(){ has vcgencmd && vcgencmd "$@" 2>&1 | tr '\n' ' '; echo; }
dt32(){ [ -r "$1" ] && od -An -tu4 --endian=big "$1" | tr -d ' \n'; echo; }
cfg_clean(){ [ -f "$1" ] && grep -vE '^[[:space:]]*(#|$)' "$1"; }
{
echo "diagnose.sh v1.0  $(date -Is)  host=$(hostname)  user=${SUDO_USER:-$USER}"
[ "$(id -u)" -eq 0 ] || echo "HINWEIS: ohne sudo gestartet - einige Werte fehlen"

h "System"
echo "model=$(tr -d '\0' </proc/device-tree/model 2>/dev/null)"
echo "kernel=$(uname -r)  os=$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-?}")"
[ -f /etc/audiolinux-release ] && echo "audiolinux=$(head -n1 /etc/audiolinux-release)"
echo "uptime=$(uptime -p 2>/dev/null)  load=$(cut -d' ' -f1-3 /proc/loadavg)"
echo "cmdline=$(cat /proc/cmdline)"
echo "isolated=$(cat /sys/devices/system/cpu/isolated 2>/dev/null)  nohz_full=$(cat /sys/devices/system/cpu/nohz_full 2>/dev/null)"

h "Strom und Temperatur"
echo "throttled=$(vc get_throttled)"
echo "EXT5V=$(vc pmic_read_adc EXT5V_V)"
echo "max_current_dt=$(dt32 /proc/device-tree/chosen/power/max_current) mA"
echo "usb_max_current_enable=$(vc get_config usb_max_current_enable)"
echo "temp=$(vc measure_temp)  pmic=$(vc measure_temp pmic)"
dmesg 2>/dev/null | grep -iE 'undervoltage|voltage normalised' | tail -n 5

h "CPU-Takt und Governor"
for c in /sys/devices/system/cpu/cpu[0-9]*; do
  f=$c/cpufreq; [ -d "$f" ] || continue
  echo "$(basename "$c"): gov=$(cat $f/scaling_governor 2>/dev/null) cur=$(cat $f/scaling_cur_freq 2>/dev/null) min=$(cat $f/scaling_min_freq 2>/dev/null) max=$(cat $f/scaling_max_freq 2>/dev/null)"
done
echo "arm_freq=$(vc get_config arm_freq)  measure_clock_arm=$(vc measure_clock arm)"
echo "cpuidle=$(cat /sys/devices/system/cpu/cpuidle/current_driver 2>/dev/null) states=$(ls /sys/devices/system/cpu/cpu0/cpuidle 2>/dev/null | tr '\n' ' ')"

h "Bootloader / EEPROM"
echo "$(vc bootloader_version)"
echo "-- EEPROM-Konfiguration:"; has vcgencmd && vcgencmd bootloader_config 2>&1 | grep -vE '^[[:space:]]*(#|$)'

h "config.txt"
for f in /boot/config.txt /boot/firmware/config.txt; do [ -f "$f" ] && { echo "-- $f"; cfg_clean "$f"; }; done
for f in /boot/cmdline.txt /boot/firmware/cmdline.txt; do [ -f "$f" ] && echo "-- $f: $(cat "$f")"; done

h "Funk (WLAN/Bluetooth)"
ip -br link 2>/dev/null
has rfkill && rfkill list 2>/dev/null
for s in bluetooth iwd wpa_supplicant NetworkManager avahi-daemon; do
  st=$(systemctl is-active "$s" 2>/dev/null); en=$(systemctl is-enabled "$s" 2>/dev/null)
  echo "service $s: active=${st:-?} enabled=${en:-?}"
done
lsmod 2>/dev/null | awk 'NR>1 && $1 ~ /^(brcmfmac|brcmutil|cfg80211|bluetooth|btbcm|hci_uart|btsdio)$/ {print "modul geladen: "$1}'

h "HDMI / Anzeige / Audio-Geraete"
for d in /sys/class/drm/card*-*; do [ -e "$d/status" ] && echo "$(basename "$d"): $(cat "$d/status")"; done
[ -r /proc/asound/cards ] && cat /proc/asound/cards

h "Netzwerk"
ip -br addr 2>/dev/null
for p in /sys/class/net/*; do i=${p##*/}
  [ "$i" = lo ] && continue
  echo "$i: speed=$(cat /sys/class/net/$i/speed 2>/dev/null) duplex=$(cat /sys/class/net/$i/duplex 2>/dev/null) mtu=$(cat /sys/class/net/$i/mtu 2>/dev/null)"
  if has ethtool; then
    ethtool --show-eee "$i" 2>/dev/null | grep -iE 'EEE status' | sed "s/^/  $i /"
    ethtool -c "$i" 2>/dev/null | grep -E '^(rx-usecs|tx-usecs|adaptive-rx):' | tr '\n' ' ' | sed "s/^/  $i coalesce: /"; echo
  fi
done

h "Interrupts (Netzwerk/USB) und Kern-Zuordnung"
grep -iE 'eth|end0|xhci|usb|dwc' /proc/interrupts 2>/dev/null | while read -r l; do
  n=${l%%:*}; n=${n// /}; echo "$l | affinity=$(cat /proc/irq/$n/smp_affinity_list 2>/dev/null)"
done

h "USB-Geraete"
for d in /sys/bus/usb/devices/*; do
  [ -f "$d/idVendor" ] || continue
  echo "$(basename "$d"): $(cat "$d/idVendor"):$(cat "$d/idProduct") $(cat "$d/manufacturer" 2>/dev/null) $(cat "$d/product" 2>/dev/null) speed=$(cat "$d/speed" 2>/dev/null) maxpower=$(cat "$d/bMaxPower" 2>/dev/null)"
done

h "Laufende Dienste"
systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null | awk '{print $1}' | tr '\n' ' '; echo
h "Aktivierte Timer"
systemctl list-timers --no-legend --plain 2>/dev/null | awk '{print $(NF-1)}' | tr '\n' ' '; echo

h "Speicher und Datentraeger"
free -m 2>/dev/null | head -n 2
findmnt -n -o TARGET,SOURCE,FSTYPE,OPTIONS / /boot 2>/dev/null
echo "swap: $(swapon --show=NAME,SIZE --noheadings 2>/dev/null | tr '\n' ' ')"

h "Diretta"
for f in /opt/diretta*/*.ini /opt/diretta*/setting* /etc/diretta*; do
  [ -f "$f" ] && { echo "-- $f"; grep -vE '^[[:space:]]*(#|;|$)' "$f" | head -n 40; }
done
systemctl list-units --all --no-legend --plain 2>/dev/null | awk '/diretta|end0-irq|thread-pin/ {print $1, $3, $4}'
echo; echo "== ENDE diagnose.sh"
} 2>&1 | tee "$OUT"
echo "(gespeichert in $OUT)"
