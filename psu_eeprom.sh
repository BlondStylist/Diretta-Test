#!/usr/bin/env bash
# psu_eeprom.sh v1.3 - PSU_MAX_CURRENT sicher setzen (Raspberry Pi 5)
# Nur die Zeile PSU_MAX_CURRENT aendert sich; Bootloader-Version bleibt gleich,
# sofern das passende Image vorliegt (sonst Hinweis und Rueckfrage).
# Werkzeuge: System-Paket rpi-eeprom ODER Buendel ~/rpi-eeprom (von vorbereiten.sh).
# Modi: check (Standard, aendert nichts) | apply | verify | rollback <backupdir>
set -Eeuo pipefail
umask 077
export LC_ALL=C
readonly WANT=5000
readonly ARCH=/root/config-archiv
readonly RUNMARK=/run/psu_eeprom.reboot-pending
readonly UPSTREAM_PENDING=/run/rpi-eeprom-update-pending
readonly DT=/proc/device-tree
readonly FWDIRS=(/lib/firmware /usr/lib/firmware)
readonly LOCK=/run/psu_eeprom.lock
readonly KEY_RE='^[[:space:]]*PSU_MAX_CURRENT[[:space:]]*='
SELF_DIR=$(cd "$(dirname "$0")" && pwd)
readonly BUNDLE=${PSU_BUNDLE:-$SELF_DIR/rpi-eeprom}
MODE=${1:-check}

die(){ echo "ABBRUCH: $*" >&2; exit 1; }
ok(){ echo "OK    $*"; }
info(){ echo "INFO  $*"; }
warn(){ echo "WARN  $*"; }
hdr(){ echo; echo "== $*"; }
trap 'echo "ABBRUCH: unerwarteter Fehler in Zeile $LINENO (Exit $?) - nichts weiter ausgefuehrt" >&2' ERR

[ "$(id -u)" -eq 0 ] || die "mit sudo ausfuehren"
exec 9>"$LOCK"; flock -n 9 || die "laeuft bereits"
TMP=$(mktemp -d /tmp/psu_eeprom.XXXXXX); trap 'rm -rf "$TMP"' EXIT

img_ts(){ { grep -aoE 'BUILD_TIMESTAMP=[0-9]+' "$1" || true; } | awk -F= 'NR==1{print $2}'; }
dt_u32(){ if [ -r "$1" ]; then od -An -tu4 --endian=big "$1" | tr -d ' \n'; fi; return 0; }
psu_count(){ grep -cE "$KEY_RE" "$1" || true; }
ts_date(){ date -u -d "@$1" +%Y-%m-%d 2>/dev/null || echo "?"; }

setup_tools(){  # Buendel einbinden, fehlende Hilfsprogramme melden bzw. ersetzen
  local c miss=() shim="$TMP/shim"
  mkdir -p "$shim"
  USING_BUNDLE=0
  if [ -x "$BUNDLE/rpi-eeprom-config" ] && [ -x "$BUNDLE/rpi-eeprom-update" ] && [ -x "$BUNDLE/rpi-eeprom-digest" ]; then
    [ -f "$BUNDLE/SHA256SUMS" ] || die "Buendel $BUNDLE ohne SHA256SUMS - vorbereiten.sh erneut ausfuehren"
    ( cd "$BUNDLE" && sha256sum -c --quiet SHA256SUMS ) >/dev/null 2>&1 || die "Buendel $BUNDLE beschaedigt (Pruefsummen) - vorbereiten.sh erneut ausfuehren"
    PATH="$BUNDLE:$PATH"
    export FIRMWARE_ROOT="$BUNDLE/firmware"
    USING_BUNDLE=1
  fi
  if ! command -v lspci >/dev/null; then  # wird von rpi-eeprom-update nur fuer den VL805 (Pi 4) benutzt
    printf '#!/bin/sh\nexit 0\n' >"$shim/lspci"; chmod 755 "$shim/lspci"
    info "lspci fehlt - Platzhalter verwendet (nur fuer Pi 4 relevant)"
  fi
  if ! command -v strings >/dev/null && command -v python3 >/dev/null; then
    cat >"$shim/strings" <<'PY'
#!/usr/bin/env python3
import re, sys
for p in (sys.argv[1:] or ['-']):
    d = sys.stdin.buffer.read() if p == '-' else open(p, 'rb').read()
    for m in re.finditer(rb'[\x20-\x7e\t]{4,}', d):
        sys.stdout.write(m.group().decode() + '\n')
PY
    chmod 755 "$shim/strings"
    info "strings fehlt - Ersatz ueber python3 verwendet"
  fi
  PATH="$shim:$PATH"; export PATH
  for c in vcgencmd rpi-eeprom-config rpi-eeprom-update rpi-eeprom-digest python3 strings lspci \
           sha256sum flock findmnt df find realpath od diff awk tee cmp stat blkid; do
    command -v "$c" >/dev/null || miss+=("$c")
  done
  if [ ${#miss[@]} -gt 0 ]; then
    case " ${miss[*]} " in *" rpi-eeprom-"*)
      echo "Hinweis: rpi-eeprom-Werkzeuge fehlen. Auf dem HOST ausfuehren: bash vorbereiten.sh" >&2;; esac
    case " ${miss[*]} " in *" python3 "*)
      echo "Hinweis: python3 fehlt. Auf dem Target: sudo pacman -S python" >&2;; esac
    die "Befehle fehlen: ${miss[*]}  (nichts geaendert)"
  fi
  if [ "$USING_BUNDLE" = 1 ]; then ok "Werkzeuge vorhanden (rpi-eeprom aus Buendel $BUNDLE)"; else ok "Werkzeuge vorhanden (System)"; fi
  if [ -f /etc/default/rpi-eeprom-update ]; then info "/etc/default/rpi-eeprom-update vorhanden - Inhalt: $(grep -v '^#' /etc/default/rpi-eeprom-update | tr '\n' ' ')"; fi
}

read_cfg(){  # aktuelle EEPROM-Konfiguration, bereinigt und validiert
  local raw="$TMP/raw.conf"
  rpi-eeprom-config >"$raw" || die "aktuelle Konfiguration nicht lesbar"
  tr -d '\000\r' <"$raw" >"$1"
  if [ -n "$(tr -d '[:print:][:space:]\200-\377' <"$1")" ]; then die "Konfiguration enthaelt Steuerzeichen - manuell pruefen"; fi
  [ -s "$1" ] || die "aktuelle Konfiguration leer"
  return 0
}

reboot_pending(){
  [ -e "$RUNMARK" ] && { echo "$RUNMARK"; return 0; }
  [ -e "$UPSTREAM_PENDING" ] && { echo "$UPSTREAM_PENDING ($(cat "$UPSTREAM_PENDING" 2>/dev/null))"; return 0; }
  return 0
}

detect_bootfs(){
  local d
  d=$(rpi-eeprom-update -b </dev/null 2>/dev/null | tail -n1 || true)
  if [ -z "$d" ] || [ ! -d "$d" ]; then
    for d in /boot/firmware /boot; do [ -f "$d/config.txt" ] && break; done
  fi
  if [ ! -d "$d" ] || [ ! -f "$d/config.txt" ]; then die "Boot-Partition nicht gefunden"; fi
  BOOTFS=$d
}

preflight(){
  hdr "Vorpruefung"
  local model p dts mopts c
  model=$(tr -d '\0' <"$DT/model" 2>/dev/null || true)
  [[ $model == "Raspberry Pi 5"* ]] || die "kein Raspberry Pi 5: '$model'"
  ok "Modell: $model"
  setup_tools
  rpi-eeprom-update -l </dev/null >"$TMP/upd_l.txt" 2>&1 || { cat "$TMP/upd_l.txt" >&2; die "rpi-eeprom-update Selbsttest (-l) fehlgeschlagen"; }
  ok "rpi-eeprom-update Selbsttest bestanden"
  p=$(reboot_pending)
  [ -z "$p" ] || die "EEPROM-Aenderung wartet auf Neustart: $p  - erst neu starten"
  ok "kein Neustart ausstehend"

  detect_bootfs
  [ ! -e "$BOOTFS/recovery.bin" ] || die "$BOOTFS/recovery.bin vorhanden (ungewoehnlich) - manuell pruefen"
  mopts=$(findmnt -n -o OPTIONS -T "$BOOTFS") || die "Mount-Optionen von $BOOTFS nicht lesbar"
  [[ ",$mopts," == *,rw,* ]] || die "$BOOTFS ist read-only"
  [ "$(df -Pk "$BOOTFS" | awk 'NR==2{print $4}')" -ge 8192 ] || die "zu wenig Platz auf $BOOTFS"
  ok "Boot-Partition $BOOTFS beschreibbar"
  for c in pieeprom.upd pieeprom.sig; do if [ -e "$BOOTFS/$c" ]; then info "Restdatei $BOOTFS/$c von frueherem Update (harmlos, wird ueberschrieben)"; fi; done

  BV=$(vcgencmd bootloader_version) || die "vcgencmd bootloader_version fehlgeschlagen"
  TS=$(awk '$1=="timestamp"{print $2; exit}' <<<"$BV")
  [[ ${TS:-} =~ ^[0-9]+$ ]] || die "Bootloader-Timestamp nicht lesbar"
  dts=$(dt_u32 "$DT/chosen/bootloader/build-timestamp")
  if [ -n "$dts" ] && [ "$dts" != "$TS" ]; then die "Device-Tree-Timestamp ($dts) != vcgencmd ($TS) - manuell pruefen"; fi
  ok "Bootloader: $(head -n1 <<<"$BV")  (timestamp $TS)"

  CUR="$TMP/current.conf"; read_cfg "$CUR"
  ok "aktuelle Konfiguration gelesen ($(wc -l <"$CUR") Zeilen)"

  UPD_ARGS=(-d -f)
  if grep -q "'-i'" "$(command -v rpi-eeprom-config)"; then UPD_ARGS=(-d -i -f); fi
  if command -v flashrom >/dev/null; then
    info "flashrom vorhanden: EEPROM wird ggf. SOFORT geschrieben - danach trotzdem neu starten"
  else
    info "kein flashrom: Update wird auf $BOOTFS vorbereitet und beim naechsten Neustart geschrieben"
  fi
  if systemctl is-enabled rpi-eeprom-update.service >/dev/null 2>&1; then
    info "rpi-eeprom-update.service ist aktiv - kann Bootloader beim Booten selbst aktualisieren"
  fi
}

list_images(){  # Paket-Images, Buendel-Images, optional manuell abgelegte in $ARCH/images
  local d out="$TMP/images.lst"
  : >"$out"
  for d in "${FWDIRS[@]}"; do
    if [ -d "$d" ]; then find -L "$d" -path '*bootloader-2712*' -name 'pieeprom-*.bin' -type f -exec realpath {} + >>"$out" 2>/dev/null || true; fi
  done
  if [ -d "$BUNDLE/firmware-2712" ]; then find -L "$BUNDLE/firmware-2712" -name 'pieeprom-*.bin' -type f -exec realpath {} + >>"$out" 2>/dev/null || true; fi
  if [ -d "$ARCH/images" ]; then find -L "$ARCH/images" -maxdepth 1 -name 'pieeprom-*.bin' -type f -exec realpath {} + >>"$out" 2>/dev/null || true; fi
  sort -u "$out"
}

find_image(){  # bevorzugt: Image mit gleichem BUILD_TIMESTAMP wie laufender Bootloader
  local f t list=() n best="" bts=0
  while IFS= read -r f; do
    t=$(img_ts "$f")
    [[ $t =~ ^[0-9]+$ ]] || continue
    if [ "$t" = "$TS" ]; then list+=("$f"); fi
    if [ "$t" -gt "$bts" ]; then bts=$t; best=$f; fi
  done < <(list_images)
  UPGRADE=0
  if [ ${#list[@]} -gt 0 ]; then
    n=$(sha256sum "${list[@]}" | awk '{print $1}' | sort -u | wc -l)
    [ "$n" -eq 1 ] || die "abweichende Images mit gleichem Timestamp: ${list[*]}"
    IMG=${list[0]}; BASE_TS=$TS
    ok "Basis-Image (= laufender Bootloader $(ts_date "$TS")): $IMG"
    return 0
  fi
  echo "Vorhandene Images (laufend: $TS = $(ts_date "$TS")):" >&2
  while IFS= read -r f; do echo "  $f  (timestamp $(img_ts "$f"))" >&2; done < <(list_images)
  if [ -n "$best" ] && [ "$bts" -gt "$TS" ]; then
    IMG=$best; BASE_TS=$bts; UPGRADE=1
    warn "kein Image der laufenden Version vorhanden."
    warn "Stattdessen wird der Bootloader auf $(ts_date "$bts") (offizielles Image) AKTUALISIERT: $IMG"
    return 0
  fi
  die "kein passendes Image (timestamp $TS) - nichts geaendert. Auf dem HOST: bash vorbereiten.sh"
}

build_new(){
  local n sec
  n=$(psu_count "$CUR")
  [ "$n" -le 1 ] || die "PSU_MAX_CURRENT mehrfach vorhanden - manuell pruefen"
  CURVAL="(nicht gesetzt)"
  if [ "$n" -eq 1 ]; then
    sec=$(awk -v re="$KEY_RE" '/^[[:space:]]*\[/{s=tolower($0); gsub(/[[:space:]]/,"",s)} $0~re{print (s==""?"[all]":s)}' "$CUR")
    [ "$sec" = "[all]" ] || die "PSU_MAX_CURRENT steht in bedingter Sektion $sec - manuell pruefen"
    CURVAL=$(awk -F= -v re="$KEY_RE" '$0~re{v=$2; gsub(/[[:space:]]/,"",v); print v}' "$CUR")
  fi
  ok "PSU_MAX_CURRENT aktuell: $CURVAL"

  NEW="$TMP/new.conf"
  awk -v re="$KEY_RE" -v add="PSU_MAX_CURRENT=$WANT" '
    $0~re {next}
    !done && $0 !~ /^[[:space:]]*(#|$)/ {
      t=tolower($0); gsub(/[[:space:]]/,"",t)
      if (t=="[all]") {print; print add; done=1; next}
      print add; done=1
    }
    {print}
    END{if(!done) print add}' "$CUR" >"$NEW"
  diff <(grep -vE "$KEY_RE" "$CUR" || true) <(grep -vE "$KEY_RE" "$NEW" || true) >/dev/null \
    || die "unerwartete Aenderung ausser PSU_MAX_CURRENT"
  [ "$(grep -cx "PSU_MAX_CURRENT=$WANT" "$NEW" || true)" -eq 1 ] || die "neue Zeile nicht genau einmal vorhanden"
  ok "neue Konfiguration: nur PSU_MAX_CURRENT geaendert"
  echo "---- diff alt -> neu"; diff "$CUR" "$NEW" || true
}

test_image(){  # $1 cfg, $2 out, $3 basis-image, $4 erwarteter timestamp: bauen, zuruecklesen, pruefen - ohne Flashen
  local rb="$TMP/readback.conf" t
  rpi-eeprom-config --config "$1" --out "$2" "$3" >/dev/null || die "Image-Erzeugung fehlgeschlagen"
  rpi-eeprom-config "$2" | tr -d '\000\r' >"$rb" || die "Zuruecklesen fehlgeschlagen"
  diff -B "$1" "$rb" >/dev/null || die "zurueckgelesene Konfiguration weicht ab"
  t=$(img_ts "$2"); [ "$t" = "$4" ] || die "Timestamp im neuen Image ($t) != erwartet ($4)"
  [ "$(stat -c %s "$2")" -eq "$(stat -c %s "$3")" ] || die "Image-Groesse weicht vom Basis-Image ab"
  ok "Test-Image verifiziert: Konfiguration korrekt, Version und Groesse wie Basis-Image"
}

flash(){  # $1 = Image, $2 = Marker (APPLIED | ROLLED_BACK)
  hdr "Schreibe EEPROM:  rpi-eeprom-update ${UPD_ARGS[*]} $1"
  local rc=0
  rpi-eeprom-update "${UPD_ARGS[@]}" "$1" </dev/null 2>&1 | tee "$(dirname "$1")/rpi-eeprom-update.log" || rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then
    die "rpi-eeprom-update Exit $rc - NICHT neu starten. Ausgabe melden (Log: $(dirname "$1")/rpi-eeprom-update.log)"
  fi
  echo "$1" >"$RUNMARK"; touch "$(dirname "$1")/$2"; sync
  ok "rpi-eeprom-update erfolgreich"
  echo
  echo "Naechster Schritt:  sudo reboot"
  echo "Beim Neustart Strom NICHT trennen (ggf. zwei Neustarts, bis 2 Min)."
  echo "Danach:             sudo bash ~/psu_eeprom.sh verify"
}

case $MODE in
  check)
    preflight; build_new
    if [ "$CURVAL" = "$WANT" ]; then hdr "Bereits gesetzt - nichts zu tun"; exit 0; fi
    find_image
    test_image "$NEW" "$TMP/test.bin" "$IMG" "$BASE_TS"
    if [ "$UPGRADE" = 1 ]; then warn "apply aktualisiert zusaetzlich den Bootloader auf $(ts_date "$BASE_TS")"; fi
    hdr "CHECK BESTANDEN - nichts geaendert. Schreiben mit: sudo bash ~/psu_eeprom.sh apply"
    ;;
  apply)
    preflight; build_new
    if [ "$CURVAL" = "$WANT" ]; then hdr "Bereits gesetzt - nichts zu tun"; exit 0; fi
    find_image
    test_image "$NEW" "$TMP/pieeprom-new.bin" "$IMG" "$BASE_TS"
    if [ "$UPGRADE" = 1 ]; then warn "Bootloader wird zusaetzlich von $(ts_date "$TS") auf $(ts_date "$BASE_TS") aktualisiert"; fi
    echo; read -r -p "EEPROM jetzt schreiben? Zum Bestaetigen JA eingeben: " a || a=""
    [ "$a" = "JA" ] || die "nicht bestaetigt - nichts geschrieben"
    BK="$ARCH/eeprom-$(date +%Y%m%d_%H%M%S)-$$"
    mkdir -p "$BK"
    cp "$CUR" "$BK/current.conf"; cp "$NEW" "$BK/new.conf"; cp "$IMG" "$BK/base.bin"
    cp "$TMP/pieeprom-new.bin" "$BK/pieeprom-new.bin"
    printf '%s\n' "$BV" >"$BK/bootloader_version.txt"; echo "$BASE_TS" >"$BK/timestamp"; echo "$TS" >"$BK/timestamp_vorher"; echo "$IMG" >"$BK/base_image_path"
    ( cd "$BK" && sha256sum current.conf new.conf base.bin pieeprom-new.bin >SHA256SUMS && sha256sum -c --quiet SHA256SUMS ) \
      || die "Backup konnte nicht korrekt geschrieben werden - nichts geflasht"
    cmp -s "$BK/base.bin" "$IMG" || die "Backup-Kopie des Basis-Images fehlerhaft - nichts geflasht"
    sync; ok "Backup: $BK"
    flash "$BK/pieeprom-new.bin" APPLIED
    ;;
  verify)
    hdr "Verifikation nach Neustart"
    p=$(reboot_pending); [ -z "$p" ] || die "noch kein Neustart erfolgt ($p)"
    setup_tools
    LAST=$(find "$ARCH" -mindepth 2 -maxdepth 2 \( -name APPLIED -o -name ROLLED_BACK \) -path '*/eeprom-*' -printf '%T@ %h %f\n' 2>/dev/null | sort -n | tail -n1 || true)
    [ -n "$LAST" ] || die "kein angewendetes Backup gefunden"
    read -r _ BK KIND <<<"$LAST"
    ok "Referenz: $BK ($KIND)"
    SOLL="$BK/new.conf"
    if [ "$KIND" = ROLLED_BACK ]; then SOLL="$BK/current.conf"; ok "Rollback erkannt - Soll = urspruengliche Konfiguration"; fi
    FAIL=0
    BV=$(vcgencmd bootloader_version) || die "vcgencmd bootloader_version fehlgeschlagen"
    TS=$(awk '$1=="timestamp"{print $2; exit}' <<<"$BV")
    if [ "$TS" = "$(cat "$BK/timestamp")" ]; then ok "Bootloader-Version wie erwartet ($TS = $(ts_date "$TS"))"
    else FAIL=1; echo "FEHL  Bootloader-Timestamp $TS != erwartet $(cat "$BK/timestamp") - EEPROM wurde nicht geschrieben?"; fi
    read_cfg "$TMP/now.conf"
    if diff -B "$SOLL" "$TMP/now.conf" >/dev/null; then ok "EEPROM-Konfiguration entspricht Soll"
    else FAIL=1; echo "FEHL  Konfiguration weicht ab:"; diff "$SOLL" "$TMP/now.conf" || true; fi
    mc=$(dt_u32 "$DT/chosen/power/max_current")
    info "device-tree max_current: ${mc:-n/a} mA"
    if [ "$SOLL" = "$BK/new.conf" ] && [ -n "$mc" ] && [ "$mc" != "$WANT" ]; then warn "max_current ($mc) != $WANT - Netzteil/Anschluss pruefen"; fi
    info "$(vcgencmd get_throttled || true)"
    if [ "$FAIL" -eq 0 ]; then hdr "VERIFY BESTANDEN"; else die "VERIFY NICHT BESTANDEN"; fi
    ;;
  rollback)
    BK=${2:-}; [ -d "$BK" ] || die "Backup-Verzeichnis angeben: rollback $ARCH/eeprom-..."
    ( cd "$BK" && sha256sum -c --quiet SHA256SUMS ) || die "Backup beschaedigt (Pruefsummen)"
    ok "Backup-Pruefsummen korrekt"
    preflight
    [ "$TS" = "$(cat "$BK/timestamp")" ] || die "laufende Bootloader-Version passt nicht zum Backup"
    test_image "$BK/current.conf" "$TMP/rollback.bin" "$BK/base.bin" "$TS"
    echo "---- diff jetzt -> Backup"; diff "$CUR" "$BK/current.conf" || true
    read -r -p "Urspruengliche Konfiguration zurueckschreiben? JA eingeben: " a || a=""
    [ "$a" = "JA" ] || die "nicht bestaetigt - nichts geschrieben"
    cp "$TMP/rollback.bin" "$BK/pieeprom-rollback.bin"; flash "$BK/pieeprom-rollback.bin" ROLLED_BACK
    ;;
  *) die "Modus: check | apply | verify | rollback <backupdir>";;
esac
