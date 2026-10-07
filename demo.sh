#!/usr/bin/env bash
#
# ReefTally non-interactive end-to-end walkthrough.
#
# Run it with:  bash demo.sh
#
# It builds a fresh survey in a temporary directory, records quadrats, exports
# CSV, names species, corrects the last entry and prints the committed example
# survey. Nothing is prompted for, nothing touches the network, and every file it
# writes lives under the temporary directory, which is removed on exit.
#
set -uo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/reeftally-demo.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

step() { printf '\n=== %s ===\n\n' "$1"; }
run() { printf '$ %s\n' "$*"; "$@"; }

step "ReefTally demo — version"
run "$PYTHON" reeftally.py --version

step "1. Start a survey project (init writes the local JSON file)"
run "$PYTHON" reeftally.py init "$WORK/hornsby-2024-06-01.json" \
    --site "Hornsby Reef" --date 2024-06-01 --observer "R. Okafor" \
    --depth 6 --transect-length 10

JOURNAL="$WORK/hornsby-2024-06-01.json"

step "2. Record quadrats with tally flags (scriptable, no prompts)"
run "$PYTHON" reeftally.py add "$JOURNAL" \
    --substrate hard=12,sand=3 \
    --coral healthy=8,pale=1,bleached=3,dead=0 \
    --species Acro=6,Poci=4 \
    --notes "starter colonies, one turbo snail scar"
run "$PYTHON" reeftally.py add "$JOURNAL" \
    --substrate hard=9,rubble=6 \
    --coral healthy=2,pale=1,bleached=4,dead=1 \
    --species Acro=3,Monti=2
run "$PYTHON" reeftally.py add "$JOURNAL" --label "A-07" \
    --substrate hard=6,sand=8,rubble=2 \
    --coral healthy=0,pale=0,bleached=2,dead=4 \
    --species Monti=1

step "3. Name the species codes so the abundance table is readable"
run "$PYTHON" reeftally.py species set "$JOURNAL" Acro=Acropora
run "$PYTHON" reeftally.py species set "$JOURNAL" "Monti=Montipora cf. digitata"
run "$PYTHON" reeftally.py species list "$JOURNAL"

step "4. Print the transect report (header, substrate, bleaching, abundance)"
run "$PYTHON" reeftally.py report "$JOURNAL"

step "5. Export the same numbers to CSV"
run "$PYTHON" reeftally.py report "$JOURNAL" --csv "$WORK/export"
run cat "$WORK/export/bleaching.csv"
run cat "$WORK/export/species.csv"

step "6. Correct a mis-typed quadrat (remove the last entry, no hand-editing JSON)"
run "$PYTHON" reeftally.py remove "$JOURNAL" --yes
run "$PYTHON" reeftally.py report "$JOURNAL"

step "7. Bad input fails loudly, exits 2 and leaves the project file untouched"
printf '$ %s\n' "$PYTHON reeftally.py add $JOURNAL --substrate hard=-3"
"$PYTHON" reeftally.py add "$JOURNAL" --substrate hard=-3
printf 'exit status: %s (project file unchanged)\n' "$?"

printf '$ %s\n' "$PYTHON reeftally.py add $JOURNAL --substrate hrad=3"
"$PYTHON" reeftally.py add "$JOURNAL" --substrate hrad=3
printf 'exit status: %s\n' "$?"

step "8. The committed example survey (examples/hornsby-reef.json) in full"
run "$PYTHON" reeftally.py report examples/hornsby-reef.json

step "Demo complete"
printf 'The temporary survey lived in %s and has been removed.\n' "$WORK"
printf 'Re-run any command yourself, e.g.:\n'
printf '  %s reeftally.py report examples/hornsby-reef.json --csv out/\n' "$PYTHON"
exit 0
