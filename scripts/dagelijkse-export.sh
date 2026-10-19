#!/usr/bin/env bash
# Dagelijkse export vanaf een machine op het kantoornetwerk.
#
# Waarom dit bestaat: Marketing Cloud laat sinds 16 oktober 2026 alleen
# verzoeken toe vanaf IP-adressen die op een allowlist staan. Het kantoor
# staat daarop, de servers van GitHub niet, en die krijgen een wisselend
# adres per run. De GitHub Action faalt daardoor elke dag op de mailexport.
# Dit script doet hetzelfde werk vanaf een machine die wél toegang heeft.
#
# Eenmalig instellen: zie INSTELLEN-DAGELIJKSE-EXPORT.md
set -euo pipefail

REPO="${REPO:-$HOME/Desktop/sfmc-dashboard-live}"
GEHEIMEN="${GEHEIMEN:-$HOME/.config/otolift/sleutels.env}"
LOG="$HOME/Library/Logs/otolift-export.log"

zeg() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

mkdir -p "$(dirname "$LOG")"
zeg "=== start ==="

if [ ! -f "$GEHEIMEN" ]; then
  zeg "FOUT: $GEHEIMEN ontbreekt. Zie INSTELLEN-DAGELIJKSE-EXPORT.md."
  exit 1
fi
# shellcheck disable=SC1090
set -a; . "$GEHEIMEN"; set +a

cd "$REPO"

# Eerst kijken of Marketing Cloud ons binnenlaat. Zo niet, dan heeft de rest
# geen zin en weet je meteen dat deze machine op het verkeerde netwerk zit.
if ! python3 - <<'PY'
import os, sys, requests
r = requests.post(
    f"https://{os.environ['SFMC_SUBDOMAIN']}.auth.marketingcloudapis.com/v2/token",
    json={"grant_type": "client_credentials",
          "client_id": os.environ["SFMC_CLIENT_ID"],
          "client_secret": os.environ["SFMC_CLIENT_SECRET"]}, timeout=30)
if r.ok:
    sys.exit(0)
print(r.text[:200])
sys.exit(1)
PY
then
  zeg "AFGEBROKEN: Marketing Cloud weigert dit IP-adres."
  zeg "Deze machine zit niet op het kantoornetwerk, of het adres staat niet"
  zeg "op de allowlist in Marketing Cloud Administration."
  exit 1
fi
zeg "Marketing Cloud: toegang ok"

# Openstaande wijzigingen betekenen dat iemand hier met de hand aan het werk
# is. Dan niet stilletjes doorgaan: een rebase zou mislukken of werk opslokken.
if ! git diff --quiet || ! git diff --cached --quiet; then
  zeg "AFGEBROKEN: er staan niet-vastgelegde wijzigingen in de map."
  git status --short | tee -a "$LOG"
  exit 1
fi

git pull --rebase --quiet origin main
zeg "repo bijgewerkt"

for stap in \
  "export_salesforce_report.py:Salesforce-rapport" \
  "export_sfmc_tracking.py:Marketing Cloud tracking" \
  "export_flow_outcomes.py:opbrengst per flow" \
  "export_reactivation_value.py:omzet via heractivatie" \
  "build_crm_metrics.py:CRM-metrics" \
  "export_lead_consent.py:consent en databasegroei" \
  "build_data_json.py:data.json bouwen"
do
  script="${stap%%:*}"; naam="${stap##*:}"
  zeg "-> $naam"
  if ! python3 "scripts/$script" >>"$LOG" 2>&1; then
    zeg "FOUT in $script. Zie $LOG. Er is niets gepusht."
    exit 1
  fi
done

if git diff --quiet -- docs exports; then
  zeg "geen wijzigingen, niets te pushen"
else
  git add docs exports
  git -c user.name="Otolift dashboard" -c user.email="dashboard@otolift.com" \
      commit --quiet -m "Dagelijkse verversing (lokaal gedraaid)"
  git push --quiet origin main
  zeg "gepusht"
fi
zeg "=== klaar ==="
