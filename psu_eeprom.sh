#!/usr/bin/env bash
# psu_eeprom.sh v1.2 - PSU_MAX_CURRENT sicher setzen (Raspberry Pi 5)
# Bootloader-Version bleibt identisch; nur diese eine Zeile aendert sich.
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
MODE=${1:-check}

die(){ echo "ABBRUCH: $*" >&2; exit 1; }
ok(){ echo "OK    $*"; }
info(){ echo "INFO  $*"; }
hdr(){ echo; echo "== $*"; }
trap 'echo "ABBRUCH: unerwarteter Fehler in Zeile $LINENO (Exit $?) - nichts weiter ausgefuehrt" >&2' ERR

[ "$(id -u)" -eq 0 ] || die "mit sudo ausfuehren"
exec 9>"$LOCK"; flock -n 9 || die "laeuft bereits"
TMP=$(mktemp -d /tmp/psu_eeprom.XXXXXX); trap 'rm -rf "$TMP"' EXIT

img_ts(){ { grep -aoE 'BUILD_TIMESTAMP=[0-9]+' "$1" || true; } | awk -F= 'NR==1{print $2}'; }
dt_u32(){ if [ -r "$1" ]; then od -An -tu4 --endian=big "$1" | tr -d ' \n'; fi; return 0; }
psu_count(){ grep -cE "$KEY_RE" "$1" || true; }

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
  local model c p dts mopts
  model=$(tr -d '\0' <"$DT/model" 2>/dev/null || true)
  [[ $model == "Raspberry Pi 5"* ]] || die "kein Raspberry Pi 5: '$model'"
  ok "Modell: $model"
  for c in vcgencmd rpi-eeprom-config rpi-eeprom-update rpi-eeprom-digest python3 strings lspci \
           sha256sum flock findmnt df find realpath od diff awk tee cmp stat; do
    command -v "$c" >/dev/null || die "Befehl fehlt: $c  (nichts geaendert - Ausgabe melden)"
  done
  ok "Werkzeuge vorhanden"
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
  for c in pieeprom.upd pieeprom.sig; do [ -e "$BOOTFS/$c" ] && info "Restdatei $BOOTFS/$c von frueherem Update (harmlos, wird ueberschrieben)"; done

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
    info "flashrom vorhanden: rpi-eeprom-update schreibt das EEPROM ggf. SOFORT (Standard auf Pi 5) - danach trotzdem neu starten"
  else
    info "kein flashrom: Update wird auf der Boot-Partition vorbereitet und beim Neustart geschrieben"
  fi
  if systemctl is-enabled rpi-eeprom-update.service >/dev/null 2>&1; then
    info "rpi-eeprom-update.service ist aktiv - kann Bootloader beim Booten selbst aktualisieren"
  fi
}

list_images(){  # offizielle Paket-Images + optional manuell abgelegte in $ARCH/images
  local d out="$TMP/images.lst"
  : >"$out"
  for d in "${FWDIRS[@]}"; do
    if [ -d "$d" ]; then find -L "$d" -path '*bootloader-2712*' -name 'pieeprom-*.bin' -type f -exec realpath {} + >>"$out" 2>/dev/null || true; fi
  done
  if [ -d "$ARCH/images" ]; then find -L "$ARCH/images" -maxdepth 1 -name 'pieeprom-*.bin' -type f -exec realpath {} + >>"$out" 2>/dev/null || true; fi
  sort -u "$out"
}

find_image(){  # Image mit exakt gleichem BUILD_TIMESTAMP wie laufender Bootloader
  local f t list=() n
  while IFS= read -r f; do
    t=$(img_ts "$f")
    if [ "$t" = "$TS" ]; then list+=("$f"); fi
  done < <(list_images)
  if [ ${#list[@]} -eq 0 ]; then
    echo "Vorhandene Images (laufend: $TS):" >&2
    while IFS= read -r f; do echo "  $f  (timestamp $(img_ts "$f"))" >&2; done < <(list_images)
    die "kein Image passend zum laufenden Bootloader (timestamp $TS) - nichts geaendert. Passendes offizielles Image kann nach $ARCH/images/ gelegt werden"
  fi
  n=$(sha256sum "${list[@]}" | awk '{print $1}' | sort -u | wc -l)
  [ "$n" -eq 1 ] || die "abweichende Images mit gleichem Timestamp: ${list[*]}"
  IMG=${list[0]}
  ok "Basis-Image (= laufender Bootloader): $IMG"
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

test_image(){  # $1 cfg, $2 out, $3 basis-image: bauen, zuruecklesen, pruefen - ohne Flashen
  local rb="$TMP/readback.conf" t
  rpi-eeprom-config --config "$1" --out "$2" "$3" >/dev/null || die "Image-Erzeugung fehlgeschlagen"
  rpi-eeprom-config "$2" | tr -d '\000\r' >"$rb" || die "Zuruecklesen fehlgeschlagen"
  diff -B "$1" "$rb" >/dev/null || die "zurueckgelesene Konfiguration weicht ab"
  t=$(img_ts "$2"); [ "$t" = "$TS" ] || die "Timestamp im neuen Image ($t) != laufend ($TS)"
  [ "$(stat -c %s "$2")" -eq "$(stat -c %s "$3")" ] || die "Image-Groesse weicht vom Basis-Image ab"
  ok "Test-Image verifiziert: Konfiguration korrekt, Version und Groesse unveraendert"
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
    preflight; find_image; build_new
    if [ "$CURVAL" = "$WANT" ]; then hdr "Bereits gesetzt - nichts zu tun"; exit 0; fi
    test_image "$NEW" "$TMP/test.bin" "$IMG"
    hdr "CHECK BESTANDEN - nichts geaendert. Schreiben mit: sudo bash ~/psu_eeprom.sh apply"
    ;;
  apply)
    preflight; find_image; build_new
    if [ "$CURVAL" = "$WANT" ]; then hdr "Bereits gesetzt - nichts zu tun"; exit 0; fi
    test_image "$NEW" "$TMP/pieeprom-new.bin" "$IMG"
    echo; read -r -p "EEPROM jetzt schreiben? Zum Bestaetigen JA eingeben: " a || a=""
    [ "$a" = "JA" ] || die "nicht bestaetigt - nichts geschrieben"
    BK="$ARCH/eeprom-$(date +%Y%m%d_%H%M%S)-$$"
    mkdir -p "$BK"
    cp "$CUR" "$BK/current.conf"; cp "$NEW" "$BK/new.conf"; cp "$IMG" "$BK/base.bin"
    cp "$TMP/pieeprom-new.bin" "$BK/pieeprom-new.bin"
    printf '%s\n' "$BV" >"$BK/bootloader_version.txt"; echo "$TS" >"$BK/timestamp"; echo "$IMG" >"$BK/base_image_path"
    ( cd "$BK" && sha256sum current.conf new.conf base.bin pieeprom-new.bin >SHA256SUMS && sha256sum -c --quiet SHA256SUMS ) \
      || die "Backup konnte nicht korrekt geschrieben werden - nichts geflasht"
    cmp -s "$BK/base.bin" "$IMG" || die "Backup-Kopie des Basis-Images fehlerhaft - nichts geflasht"
    sync; ok "Backup: $BK"
    flash "$BK/pieeprom-new.bin" APPLIED
    ;;
  verify)
    hdr "Verifikation nach Neustart"
    p=$(reboot_pending); [ -z "$p" ] || die "noch kein Neustart erfolgt ($p)"
    LAST=$(find "$ARCH" -mindepth 2 -maxdepth 2 \( -name APPLIED -o -name ROLLED_BACK \) -path '*/eeprom-*' -printf '%T@ %h %f\n' 2>/dev/null | sort -n | tail -n1 || true)
    [ -n "$LAST" ] || die "kein angewendetes Backup gefunden"
    read -r _ BK KIND <<<"$LAST"
    ok "Referenz: $BK ($KIND)"
    SOLL="$BK/new.conf"
    if [ "$KIND" = ROLLED_BACK ]; then SOLL="$BK/current.conf"; ok "Rollback erkannt - Soll = urspruengliche Konfiguration"; fi
    FAIL=0
    BV=$(vcgencmd bootloader_version) || die "vcgencmd bootloader_version fehlgeschlagen"
    TS=$(awk '$1=="timestamp"{print $2; exit}' <<<"$BV")
    if [ "$TS" = "$(cat "$BK/timestamp")" ]; then ok "Bootloader-Version unveraendert ($TS)"
    else echo "WARN  Bootloader-Timestamp $TS != vorher $(cat "$BK/timestamp") (anderes Update?)"; fi
    read_cfg "$TMP/now.conf"
    if diff -B "$SOLL" "$TMP/now.conf" >/dev/null; then ok "EEPROM-Konfiguration entspricht Soll"
    else FAIL=1; echo "FEHL  Konfiguration weicht ab:"; diff "$SOLL" "$TMP/now.conf" || true; fi
    mc=$(dt_u32 "$DT/chosen/power/max_current")
    info "device-tree max_current: ${mc:-n/a} mA"
    if [ "$SOLL" = "$BK/new.conf" ] && [ -n "$mc" ] && [ "$mc" != "$WANT" ]; then echo "WARN  max_current ($mc) != $WANT - Netzteil/Anschluss pruefen, Neustart erfolgt?"; fi
    info "$(vcgencmd get_throttled || true)"
    if [ "$FAIL" -eq 0 ]; then hdr "VERIFY BESTANDEN"; else die "VERIFY NICHT BESTANDEN"; fi
    ;;
  rollback)
    BK=${2:-}; [ -d "$BK" ] || die "Backup-Verzeichnis angeben: rollback $ARCH/eeprom-..."
    ( cd "$BK" && sha256sum -c --quiet SHA256SUMS ) || die "Backup beschaedigt (Pruefsummen)"
    ok "Backup-Pruefsummen korrekt"
    preflight
    [ "$TS" = "$(cat "$BK/timestamp")" ] || die "laufende Bootloader-Version passt nicht zum Backup"
    test_image "$BK/current.conf" "$TMP/rollback.bin" "$BK/base.bin"
    echo "---- diff jetzt -> Backup"; diff "$CUR" "$BK/current.conf" || true
    read -r -p "Urspruengliche Konfiguration zurueckschreiben? JA eingeben: " a || a=""
    [ "$a" = "JA" ] || die "nicht bestaetigt - nichts geschrieben"
    cp "$TMP/rollback.bin" "$BK/pieeprom-rollback.bin"; flash "$BK/pieeprom-rollback.bin" ROLLED_BACK
    ;;
  *) die "Modus: check | apply | verify | rollback <backupdir>";;
esac
