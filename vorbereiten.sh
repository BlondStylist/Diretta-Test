#!/usr/bin/env bash
# vorbereiten.sh - laeuft auf dem HOST (diretta-host), als normaler Benutzer.
# Laedt die offiziellen Raspberry-Pi-EEPROM-Werkzeuge und genau das Bootloader-Image,
# das auf dem Target laeuft (feste Upstream-Version, jede Datei per SHA256 geprueft),
# kopiert alles plus psu_eeprom.sh auf das Target und startet dort den Check.
# Der Check aendert nichts. Aufruf:  bash vorbereiten.sh
set -Eeuo pipefail
export LC_ALL=C
TARGET=${TARGET:-diretta-target}
readonly COMMIT=ded1e9332bd996f6adc46e10d22d9ef0298b1882
readonly UP="https://raw.githubusercontent.com/raspberrypi/rpi-eeprom/$COMMIT"
readonly MINE="https://raw.githubusercontent.com/BlondStylist/Diretta-Test/claude/zealous-ride-1fn94u"
readonly PSU_SHA=4b6df313161bd4877b6a5b3ce48c1f4150f68f6735fe54a5eded67e2c5863326
W="$HOME/rpi-eeprom-buendel"

die(){ echo; echo "ABBRUCH: $*" >&2; exit 1; }
ok(){ echo "OK    $*"; }
trap 'echo "ABBRUCH: unerwarteter Fehler in Zeile $LINENO - Ausgabe bitte melden" >&2' ERR

get(){  # $1 URL, $2 Ziel, $3 erwartete SHA256
  local i
  for i in 1 2 3 4; do
    if curl -fsSL --retry 2 -o "$2.part" "$1"; then break; fi
    [ "$i" -lt 4 ] || die "Download fehlgeschlagen: $1  (Internet am Host?)"
    sleep $((i * 2))
  done
  [ "$(sha256sum "$2.part" | cut -c1-64)" = "$3" ] || { rm -f "$2.part"; die "Pruefsumme falsch: $1"; }
  mv "$2.part" "$2"
}

echo "== Schritt 1/5: Werkzeuge am Host pruefen"
for c in curl ssh tar sha256sum awk; do command -v "$c" >/dev/null || die "Befehl fehlt am Host: $c"; done
ok "Host bereit"

echo "== Schritt 2/5: Bootloader-Version des Targets lesen (Passwort ggf. eingeben)"
TS=$(ssh "$TARGET" 'od -An -tu4 --endian=big /proc/device-tree/chosen/bootloader/build-timestamp 2>/dev/null | tr -d " \n"') \
  || die "keine SSH-Verbindung zu $TARGET"
[[ $TS =~ ^[0-9]+$ ]] || die "Bootloader-Version des Targets nicht lesbar (Ausgabe: '$TS')"
ok "Target-Bootloader timestamp $TS ($(date -u -d "@$TS" +%Y-%m-%d))"

MANIFEST=$(cat <<'EOF'
1790351949 firmware-2712/default/pieeprom-2026-09-25.bin 02c4daa25df5af66af50da63ee97dfd03b21a40ac5b495a1c447979a2aa00edd
1790260790 firmware-2712/latest/pieeprom-2026-09-24.bin 9c90b6a931529ca11b90923770dcf9de788d83390a9450533a2c35c80e6a34a3
1790165383 firmware-2712/latest/pieeprom-2026-09-23.bin 4635a1969e23f9e30494753ba8179fa684f4c21d5221231826cabb89160fc1ac
1789171628 firmware-2712/default/pieeprom-2026-09-12.bin b49adc90c380f3b9bec9e1ab21111571d7cfdbfe8f2dc5974f294bdb3eb7c39a
1788998818 firmware-2712/latest/pieeprom-2026-09-10.bin 8d0464096774f57ea06d18248396d2470f746654354389c4af92f538a80da66a
1786493216 firmware-2712/latest/pieeprom-2026-08-12.bin faf29252673a5d699a774a1953b95e4d8d714dd39b676e9db89a6208b14fe326
1785847198 firmware-2712/latest/pieeprom-2026-08-04.bin 77900e48575171af464a6121b5dfde60ccb76975e94a068f71979a1680789508
1782691621 firmware-2712/latest/pieeprom-2026-06-29.bin 15a1ce1576d8d3af9e8e36a17ff83ba309226c13090f3d40284049c9a07abe94
1781654813 firmware-2712/latest/pieeprom-2026-06-17.bin 52b34d500be307fb0bedd05bbad70927df8873e38ca071c1ab309637d222e98c
1779807685 firmware-2712/default/pieeprom-2026-05-26.bin fee8bee6a738a1a61004f0770f15534de7a48a2a199dc4c7af7ed73ab04f18dd
1779408415 firmware-2712/old/latest/pieeprom-2026-05-22.bin dcfb7c05ba44d8c895e1a543a25081a18a04d7d4c4e83859d4417214a2ea31b9
1778976445 firmware-2712/old/latest/pieeprom-2026-05-17.bin 20830733e17a60b3f357885bec569ea23ff6d8bd65c2f43182ae091e139b248d
1778671631 firmware-2712/old/latest/pieeprom-2026-05-13.bin 62d954d7cae5c136de7524459ceaa3e8937ebafdbc373240695ff7b0248fe201
1778498402 firmware-2712/old/default/pieeprom-2026-05-11.bin 4f22c444089becc3ef197902c88d5b4f50951a2a9373e27dd9c050c88b2a842e
1777551683 firmware-2712/old/latest/pieeprom-2026-04-30.bin 4dc4bb4dd7d194fd2f7582c9ad290442a061d8232c64c76564ccd0e75a956a1c
1777248418 firmware-2712/old/latest/pieeprom-2026-04-27.bin 58de98bf2f67dc32647f6d5a03c383499a9da22e13c8f5eba18749368a139928
1776201624 firmware-2712/old/latest/pieeprom-2026-04-14.bin 2ad1e314f5425e0362765a34220f841d4548a4711c77d10cb3988cc82af14495
1771840899 firmware-2712/old/latest/pieeprom-2026-02-23.bin 5df18e4233b24361d4f3144a7278bdd757193b8ad1dc2f423413ed1935645a69
1770388300 firmware-2712/old/latest/pieeprom-2026-02-06.bin ea9dcc01aab7b5854a4a3709ec94bd729db1e9223fe0bd48a32a8f7e0b120acb
1769002727 firmware-2712/old/latest/pieeprom-2026-01-21.bin 0d9a67f324ea47d4cb431c466bc7502b166c6ef1caf5a2c6e1b0b10e7a3f1d3b
1768585427 firmware-2712/old/latest/pieeprom-2026-01-16.bin a710de719c70940317e8c4e513837380ec3f3c6eb24189195a95b724adfbc9a0
1765222194 firmware-2712/old/default/pieeprom-2025-12-08.bin 3fcc73903807dfbebb50697c3e4f2a6f09d5a3c5348b6ba8d00a2f179050c022
1764250826 firmware-2712/old/latest/pieeprom-2025-11-27.bin 4d1849e7348608c053ff4b8273efb640613a55c13ceef77eac114ba00e27e891
1763732176 firmware-2712/old/latest/pieeprom-2025-11-21.bin 9ea2d07ce18da20a01efed6fa5d10ac3d883f5df8708ca1ab8c1a61dd58ad038
1762364238 firmware-2712/old/default/pieeprom-2025-11-05.bin 8906511b028eab7bbe40de8acbaa4a769c9a70560984e26a141591b55f076e44
1760694517 firmware-2712/old/latest/pieeprom-2025-10-17.bin 3c471e8eb63be2849b6db1a74f4ab503ffb4f36a26e340cc2b901bf282b0e29a
1759940358 firmware-2712/old/latest/pieeprom-2025-10-08.bin d0acc62a589ec0cee4857d850261a922596f40c06a7298b45dcd6af1c380b618
1758829114 firmware-2712/old/latest/pieeprom-2025-09-25.bin 3647630a2bc943c0ec0d507ae999e6ea4a4041d0628d2bde366eac0340bec764
1758625555 firmware-2712/old/latest/pieeprom-2025-09-23.bin c22054d277e1ba1b0f18d2dc19487d20b99c36342277f0963221a04ac03f76b1
1758541389 firmware-2712/old/latest/pieeprom-2025-09-22.bin 1a7efb589bf3908f042dd31a5af74b331f5142ca3f73c79d2afc2a868969252c
1756321307 firmware-2712/old/latest/pieeprom-2025-08-27.bin 87c31034f8edfbb8473f95a5051cb0b376eb235f187167bbba0b9697768f2d7e
1755703318 firmware-2712/old/latest/pieeprom-2025-08-20.bin 833ec6d465bfe4afbf19a856525ec86bdf7bbce9b361c361c8fd4632d6fcb555
1755094299 firmware-2712/old/latest/pieeprom-2025-08-13.bin c4f51728d284ae3cfc5a219036d6b423dab401a07a3288070cf26339c283dad1
1752769512 firmware-2712/old/latest/pieeprom-2025-07-17.bin b25ee2e1f0a5253f4068e75e6380906625bb62b6da2b9294829932c86d1e3453
1751539154 firmware-2712/old/latest/pieeprom-2025-07-03.bin 2e764dc67c09c9a8ac9333e8d062416178c11853ae62cf2981f150d85982902b
1751239011 firmware-2712/old/latest/pieeprom-2025-06-29.bin 5f72b19cbbe283790904c86c9fb4b500c6bb08ffe13b966e0821bd2f8223b86f
1750421313 firmware-2712/old/latest/pieeprom-2025-06-20.bin 4565f054b236f58f18d906927aa71b9307500748f409a828fa1f1a38be659fd9
1749807566 firmware-2712/old/latest/pieeprom-2025-06-13.bin b0abfdfa811d9f274c59a02c048f94bba34d5d690d9b9329a220eb4dbd066412
1749461452 firmware-2712/old/latest/pieeprom-2025-06-09.bin cf1ae77748a9c75db0cfc107356ae5101c0ab3035254024c8fc79ed4d12fa3ec
1746713597 firmware-2712/old/default/pieeprom-2025-05-08.bin 3184e970b6bcadf2052dbb9c17a81d69c0a564745458af3adf0fa09c0dbde870
1744067807 firmware-2712/old/latest/pieeprom-2025-04-07.bin 0f2443396754f82757f458c2aebff1e7ac8e7c99867b9cdcaa50cae2ec494555
1743034604 firmware-2712/old/latest/pieeprom-2025-03-27.bin a82936d8e5abe07a1b71d3e36f56ed90ae45711ca68e6b908de0253b952e2a36
1742391686 firmware-2712/old/latest/pieeprom-2025-03-19.bin a9651bcebb8525d341f2bc4b6db5e70f1ebaa0058e3a2ba70dabc522eb4915ae
1741626637 firmware-2712/old/default/pieeprom-2025-03-10.bin f7d6860df72bfb15fd916604217281fc940937db14849c33b775d387769c488b
1741014903 firmware-2712/old/latest/pieeprom-2025-03-03.bin d4a5b15dee8dd02c06a56b1e5cc08febc889e44480dc6faa3d847cd2f49c711c
1739357512 firmware-2712/old/default/pieeprom-2025-02-12.bin 2602459ed7777d5adc79909e70287dfb43937a45e14d8fe314884a4374565799
1739293519 firmware-2712/old/latest/pieeprom-2025-02-11.bin 6ae1dd0b518e407d10f8ff3cef69181a0c36495c2b709ab41c3296ee61216137
1737983339 firmware-2712/old/latest/pieeprom-2025-01-27.bin a9c65b9337e0098c703cacd9bf83db7e05df16998c69339f30756d304cf5025a
1737505011 firmware-2712/old/default/pieeprom-2025-01-22.bin 1c4f34e8e83a41eb1b0f4106b73763209f8414679c77a93fa66a2662faa1870a
1736813808 firmware-2712/old/latest/pieeprom-2025-01-14.bin 62843daff6cd0dd095a6ff466b6b135e75d8ba19837ebac9a4d65773626c3859
1736727407 firmware-2712/old/latest/pieeprom-2025-01-13.bin 54b623a18423c4ba23c239351b17071f19393bbb413dbff7cc694e644ad523aa
1736358768 firmware-2712/old/latest/pieeprom-2025-01-08.bin 5361635b7a548f4bddcede1fc6b0f1d64758a8936511a629db8e41e1e1eb2638
1736263931 firmware-2712/old/latest/pieeprom-2025-01-07.bin 5bc1fa1278d3c83f55e141054c029ad589ad3679e295dfcf99e7fdd8b09344d5
1736182835 firmware-2712/old/latest/pieeprom-2025-01-06.bin 52949b2b1db3a433c44885d6c2468424c4568e43feb1042b7b831cc471cf4714
1734609433 firmware-2712/old/latest/pieeprom-2024-12-19.bin 82c62722120e67d630b27420ffc648ea3e3f23db3d30ef319d7504d4718d1ba6
1734221810 firmware-2712/old/latest/pieeprom-2024-12-15.bin 6690d6129e111ab207813ee7a2f539cc2aeec24845cfeb24b4bd6fd4c661c04f
1733575343 firmware-2712/old/latest/pieeprom-2024-12-07.bin 35975f13f97086c1903d39da80c43a69955ac1dbbe8b22ad020acfbd5d73be63
1732717699 firmware-2712/old/latest/pieeprom-2024-11-27.bin a66fe555fff0803d9c3ac6fd8576e87e195095dc2c37fa9b1a24a155eb248851
1731427844 firmware-2712/old/default/pieeprom-2024-11-12.bin f68d3f52f66fa54769b5b723690600b10eed9e4b3610b3448a0346ee3b3558e1
1730810292 firmware-2712/old/latest/pieeprom-2024-11-05.bin 25786ee3b7a806a81e70e71261ee2ac0180efacffe65a0a420ee3152a6405868
1729520869 firmware-2712/old/latest/pieeprom-2024-10-21.bin 5814095134d5931f64f91450ac51bd1a2cde5f0dcc4e515755841ab42970a32f
1728517007 firmware-2712/old/latest/pieeprom-2024-10-10.bin d0f7e3d684193902033c8a21ee2fc8cd7008a1d4072f25ac543e4dbba528c74c
1727096576 firmware-2712/old/default/pieeprom-2024-09-23.bin fb5d7c6b76b90454d0b09ceb545d15bb7077448ec3f654af85ddff4adf31c17c
1725975630 firmware-2712/old/default/pieeprom-2024-09-10.bin 82261042d64bc358b591bc12f842654dea5e07599baf97fe80993a9042349093
1725562503 firmware-2712/old/latest/pieeprom-2024-09-05.bin 837b646df2570369235b04d6abb2e28fe56c98500d925e54a88ab9d4e8f3f3ef
1722349546 firmware-2712/old/default/pieeprom-2024-07-30.bin ca9a7863859019411fe75b65d40cf6751561e0f82598ba721028bc3c8bf7f2bf
1721921872 firmware-2712/old/latest/pieeprom-2024-07-25.bin fc9e5f39f3c887d1f365d47f0889bc8d892b10652a9112e1aa7875f8bd2cca13
1717602109 firmware-2712/old/default/pieeprom-2024-06-05.bin 33b49fad4c85aecbfa04cee65c394e8239301e633b8d0a8984c70fa3329888ea
1717489297 firmware-2712/old/latest/pieeprom-2024-06-04.bin 70e1eeadd5889607262cb58401744603728294461d520335bc045721eb54b31d
1715945383 firmware-2712/old/latest/pieeprom-2024-05-17.bin e74452aee5cc808668f17070e80adbf899adca87722b0e1b5fc729421d6815c2
1715613301 firmware-2712/old/latest/pieeprom-2024-05-13.bin 4aa6504733f56598aaffe7fd3e57dab9f2e053f86cda0183a2ca253ae3ec84a2
1713610410 firmware-2712/old/default/pieeprom-2024-04-20.bin 15f8a9b3ae42d244032e2b6add9714c955d311e3fd9379aad51a92aa20da2035
1713429900 firmware-2712/old/latest/pieeprom-2024-04-18.bin c09f6e2f8dd655281977247ffb895a57e900b77f6f70e7a9a2d4cf7a56ed0a90
1713358463 firmware-2712/old/default/pieeprom-2024-04-17.bin 4bd375e6df23a0cb5035d5d4bb7d8ebdf8fc3782b2bc48b326cbd4c0dca952fb
1712313679 firmware-2712/old/latest/pieeprom-2024-04-05.bin d705d376b212a807f7a1ad0ba6d713975c62556e6e7a88f1db97e3deb5ce0f50
1708097321 firmware-2712/old/default/pieeprom-2024-02-16.bin 17888d83f1a953617d2a576819441d021a4a8662ca1372a7d8d2dc007a579476
1707895062 firmware-2712/old/latest/pieeprom-2024-02-14.bin d208a04408c6169a027a45cf4a1bc8fae80d8055c7e6794a952e0832ed7975c9
1707392087 firmware-2712/old/latest/pieeprom-2024-02-08.bin 479ea40e912299393645b68f08a473a77eb974662d3293c039a61dd31be3ff0e
1707143914 firmware-2712/old/latest/pieeprom-2024-02-05.bin 0ae8fca8c375e7734dfa500fc6a667e322726f5dcba77279a51c8cee1a532010
1706098561 firmware-2712/old/latest/pieeprom-2024-01-24.bin e5b2b4b5d4a96011658b559e592c655b3644de37ca5417cd3ca48d479d0d71c3
1705934676 firmware-2712/old/latest/pieeprom-2024-01-22.bin 78f240887fd49a48ab31f22001463e82bc9763d9a6a98eb7cea19619d6d12955
1705345348 firmware-2712/old/latest/pieeprom-2024-01-15.bin f7b1a368e7b7c983670af2204075d3c11fc268b8d00e3759cbf0dd6c89b8618a
1704470260 firmware-2712/old/default/pieeprom-2024-01-05.bin bbf3360f1248ab0ae3b0e52b61c61bee0309346fb05cf2a9716325b2f85d3777
1702572205 firmware-2712/old/default/pieeprom-2023-12-14.bin fc87ba78d8988956102dbbb80fa1c24cc2f2efcfd6a9228fcd16f5df7d124984
1701887365 firmware-2712/old/default/pieeprom-2023-12-06.bin 675374a8f6709cc1c2440cfe3a3365b82713614f85b60bd73c60a8a34093354c
1700509217 firmware-2712/old/default/pieeprom-2023-11-20.bin eab15a0ce79c7c0c38131a2a705af8d5ed86f102f8e4232a47a7ffc26b5db753
1698684310 firmware-2712/old/default/pieeprom-2023-10-30.bin 4a712cf32366f28df6cff9d503d0b26a2644366a20486bc066b51a2c47557f52
1697650217 firmware-2712/old/default/pieeprom-2023-10-18.bin a014890880a4012d4f5a5c07bcdf4e2e2a915d2e1f35fbfe6a8c4d5351a198b5
1695896697 firmware-2712/old/default/pieeprom-2023-09-28.bin b43fe4d48c8b260a372597a14dcae57082b052dd3503d23c7f8a4e0a4a7bbf77
1695315523 firmware-2712/old/default/pieeprom-2023-09-21.bin 1d613d4d5b2bd88feeec2718921c5734c66d7e00a252720b09585242654ebae4
1694601426 firmware-2712/old/default/pieeprom-2023-09-13.bin 31d5c125dc89522dd72c76a41d9aceeafbeca512e0225bad7678b958598a4e27
EOF
)
LINE=$(awk -v t="$TS" '$1==t' <<<"$MANIFEST")
if [ -n "$LINE" ]; then
  ok "passendes offizielles Image gefunden: $(awk '{print $2}' <<<"$LINE")"
else
  LINE=$(head -n1 <<<"$MANIFEST")
  echo "WARN  Kein Image fuer diese Version in der Upstream-Liste."
  echo "WARN  Stattdessen wird das neueste Standard-Image geladen: $(awk '{print $2}' <<<"$LINE")"
  echo "WARN  psu_eeprom.sh fragt dann vor dem Schreiben ausdruecklich nach."
fi
read -r _ IMGPATH IMGSHA <<<"$LINE"

echo "== Schritt 3/5: Dateien laden und pruefen"
rm -rf "$W"; mkdir -p "$W/rpi-eeprom/firmware-2712/default"
B="$W/rpi-eeprom"
get "$UP/rpi-eeprom-config" "$B/rpi-eeprom-config" dfa8e2a819e0922fc7fd948ef4fa55f44066fa04ccf5da210d880f0b35ad9d1b
get "$UP/rpi-eeprom-update" "$B/rpi-eeprom-update" e4b20cfe24326b4cc42ede93e8fd95ee82f0ba7a2c6a93b99a8fdc16055b0d9c
get "$UP/rpi-eeprom-digest" "$B/rpi-eeprom-digest" 2885e3603f9d89995cf5b033f2a616bd1e932547c1d3cffff1ad98e427996a03
get "$UP/LICENSE" "$B/LICENSE" 594b7565fd3ccf8acd4711a2ec1b199181aafbc3426d0bacaa50ef40edbf7c4a
get "$UP/firmware-2712/default/recovery.bin" "$B/firmware-2712/default/recovery.bin" 29bbbbba21b532fbde8943adf6ad1e657c8ac0013b66e6d60f8212894d86b024
get "$UP/$IMGPATH" "$B/firmware-2712/default/$(basename "$IMGPATH")" "$IMGSHA"
chmod 755 "$B/rpi-eeprom-config" "$B/rpi-eeprom-update" "$B/rpi-eeprom-digest"
( cd "$B" && find . -type f -printf '%P\n' | sort | xargs sha256sum >"$W/SUMS.tmp" ) && mv "$W/SUMS.tmp" "$B/SHA256SUMS"
get "$MINE/psu_eeprom.sh" "$W/psu_eeprom.sh" "$PSU_SHA"
ok "alle Dateien geladen, Pruefsummen korrekt ($W)"

echo "== Schritt 4/5: auf das Target kopieren"
R=$(tar -C "$W" -cf - rpi-eeprom psu_eeprom.sh | ssh "$TARGET" 'rm -rf ~/rpi-eeprom && tar -C ~ -xf - && sha256sum ~/psu_eeprom.sh && cd ~/rpi-eeprom && sha256sum -c --quiet SHA256SUMS && echo BUENDEL-OK') \
  || die "Kopieren auf das Target fehlgeschlagen"
[[ $R == "$PSU_SHA "* && $R == *BUENDEL-OK* ]] || die "Kopie auf dem Target fehlerhaft: $R"
ok "auf $TARGET kopiert: ~/psu_eeprom.sh und ~/rpi-eeprom/"

echo "== Schritt 5/5: Check auf dem Target (aendert nichts, sudo-Passwort ggf. eingeben)"
ssh -t "$TARGET" 'sudo bash ~/psu_eeprom.sh check'
