#!/usr/bin/env bash
# Cut the adapter test fixtures from full deliveries: a few real features per
# layer, in the delivery's own format, so the adapters meet the real schema.
#
#   tests/fixtures/build.sh <turrutebasen .gdb> <stedsnavn .gdb> <teig .gdb, Luster> <kulturminner .gdb>
#
# Needs GDAL 3.6 or newer (it writes FileGDB). Rebuild when a source changes its
# schema, then update the counts and ids in tests/test_adapters.py.
# Data: Turrutebasen, Stedsnavn and Eiendomskart Teig, (c) Kartverket, CC BY 4.0.
# Kulturminner, (c) Riksantikvaren, NLOD.
set -euo pipefail

tur=$1
ssr=$2
teig=$3
kul=$4
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cut() {  # cut <source gdb> <target gdb> <layer> <where>
    if [ -e "$2" ]; then
        ogr2ogr -update "$2" "$1" "$3" -where "$4"
    else
        ogr2ogr -f OpenFileGDB "$2" "$1" "$3" -where "$4"
    fi
}

pack() {  # pack <gdb> <zip>
    rm -f "$2"
    (cd "$(dirname "$1")" && python3 -m zipfile -c "$2" "$(basename "$1")")
}

# Turrutebasen: routes bre18, bre19 and bre20, which meet at Arentzbu in
# Breheimen. Several segments carry more than one route.
segments="'d200b35a-a8a1-4fd3-b4c2-d1f7f7c9fab3','dab5141a-4bd5-4539-a3f4-72bffcf4c964',
'6ce2c484-2173-4fa7-8978-24f9e42946f0','74a241da-554e-492a-8e16-226732854ece',
'19fa211c-e4e1-4634-aa5d-299803f37d3f','a0ad5dbc-659b-4d69-a3e5-a95412de6c7d',
'5070c59a-f2d1-45a6-a788-f17275fe0f39','840abaaf-a6a3-4dab-8db3-a784ecf96c63',
'7883e674-3f55-4805-bf36-9352e87cc3b0','ce4908aa-d039-413e-996d-96b4440a1dd0',
'ead0b80d-d851-428f-b845-9a5fb04c4f9c'"
points="'20cb5bc2-a1f3-466a-a6d8-df43f796bcb9','2cc94277-de3a-4598-b5de-b936f9a33b65',
'51b615ac-7f65-48a6-8592-9780ee3395db','6a003517-a380-4350-bb1f-0b28e5b5e284'"
cut "$tur" "$work/tur.gdb" fotrute_senterlinje "lokalid IN ($segments)"
cut "$tur" "$work/tur.gdb" fotruteinfo_tabell "fotrute_fk IN ($segments)"
cut "$tur" "$work/tur.gdb" ruteinfopunkt_posisjon "lokalid IN ($points)"
pack "$work/tur.gdb" "$here/turrutebasen.zip"

# Stedsnavn: seven places in the same area, covering every geometry layer, a
# place with two names, other spellings, two municipalities and a Sami name.
places="799507,131486,373618,27269,555559,1052910,1077751"
for layer in sted_posisjon sted_multipunkt sted_senterlinje sted_omrade; do
    cut "$ssr" "$work/ssr.gdb" "$layer" "stedsnummer IN ($places)"
done
cut "$ssr" "$work/ssr.gdb" stedsnavn "sted_fk IN ($places)"
cut "$ssr" "$work/ssr.gdb" kommune "sted_fk IN ($places)"
names=$(ogr2ogr -f CSV /vsistdout/ "$work/ssr.gdb" -sql "SELECT objid FROM stedsnavn" \
    | tail -n +2 | paste -sd, -)
cut "$ssr" "$work/ssr.gdb" skrivemate "stedsnavn_fk IN ($names)"
cut "$ssr" "$work/ssr.gdb" annen_skrivemate "stedsnavn_fk IN ($names)"
pack "$work/ssr.gdb" "$here/stedsnavn.zip"

# Teig: five parcels in Luster. Two have several property units, three come in
# several pieces (one row per piece), one of them with area remarks.
parcels="'239751175','368518980','230985469','52558758','239563645'"
cut "$teig" "$work/teig.gdb" teig "teigid IN ($parcels)"
cut "$teig" "$work/teig.gdb" matrikkelenhet "teig_fk IN ($parcels)"
cut "$teig" "$work/teig.gdb" teig_arealmerknad "teig_fk IN ($parcels)"
pack "$work/teig.gdb" "$here/teig.zip"

# Kulturminner: a site in two pieces with its two monuments, a site and
# monument whose capture date is not a valid date (year 0), and a protection
# zone in two pieces.
cut "$kul" "$work/kul.gdb" lokalitet "lokalid IN ('57986','303680-0')"
cut "$kul" "$work/kul.gdb" enkeltminne "lokalid IN ('57986-2','57986-3','303680-0')"
cut "$kul" "$work/kul.gdb" sikringssone "lokalid IN ('19488')"
pack "$work/kul.gdb" "$here/kulturminner.zip"

ls -l "$here"/*.zip
