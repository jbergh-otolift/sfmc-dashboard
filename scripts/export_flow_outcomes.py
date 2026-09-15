#!/usr/bin/env python3
"""
export_flow_outcomes.py

Wat leveren de mailflows op? Koppelt per journey de geraakte leads aan wat die
leads daarna deden: doorstroom naar een afspraak, conversie, en de orderwaarde
van de bijbehorende Opportunity.

De keten:

    journey  ->  welke leads kregen mail        (exports/_reached_lead_ids.json)
             ->  wie ging er naar Appointment   (exports/report.csv, LeadHistory)
             ->  Lead.ConvertedOpportunityId
             ->  Opportunity.Amount + IsWon     (omzet)

Gebruik:
    SF_DOMAIN=... SF_CLIENT_ID=... SF_CLIENT_SECRET=... python scripts/export_flow_outcomes.py

Draai dit ná export_sfmc_tracking.py (die levert de geraakte leads per journey)
en ná export_salesforce_report.py (die levert de statusovergangen).

Schrijft `exports/flow_outcomes.json`.

LET OP — dit is touch-attributie, geen bewezen oorzaak. Een lead die mail kreeg
én converteerde, kan net zo goed door het telefoongesprek zijn overtuigd.
Zonder controlegroep blijft dit een verband, geen causaliteit. Het dashboard
labelt het daarom als "geraakt door flow X en geconverteerd", niet als
"opgeleverd door flow X".
"""

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

import requests

DOMAIN = os.environ["SF_DOMAIN"]
CLIENT_ID = os.environ["SF_CLIENT_ID"]
CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
API_VERSION = "v60.0"

REACHED_PATH = "exports/_reached_lead_ids.json"
REPORT_PATH = "exports/report.csv"
OUT_PATH = "exports/flow_outcomes.json"

APPOINTMENT_STATUS = "Appointment"
SQL_STATUS = "Re-entered"

# Terugkijkvenster voor conversies, gelijk aan dat van de coverage-teller.
LOOKBACK_DAYS = int(os.environ.get("COVERAGE_DAYS", "90"))


def get_token():
    resp = requests.post(
        f"https://{DOMAIN}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data["instance_url"]


def query_all(session, instance_url, soql):
    """Query met paginatie via nextRecordsUrl."""
    url = f"{instance_url}/services/data/{API_VERSION}/query"
    params = {"q": soql}
    records = []
    while True:
        resp = session.get(url, params=params, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data["records"])
        if data.get("done", True):
            break
        url = instance_url + data["nextRecordsUrl"]
        params = None
    return records


def conversions_by_lead(session, instance_url):
    """Geconverteerde leads in het venster, met de waarde van hun Opportunity.

    We halen alle conversies uit de periode op en snijden lokaal door met de
    geraakte leads. Dat scheelt honderden ID-batches in de SOQL.
    """
    soql = (
        "SELECT Id, ConvertedOpportunityId, ConvertedOpportunity.Amount, "
        "ConvertedOpportunity.IsWon, ConvertedOpportunity.StageName "
        "FROM Lead "
        f"WHERE IsConverted = true AND ConvertedDate = LAST_N_DAYS:{LOOKBACK_DAYS}"
    )
    out = {}
    for row in query_all(session, instance_url, soql):
        opp = row.get("ConvertedOpportunity") or {}
        out[row["Id"]] = {
            "won": bool(opp.get("IsWon")),
            "amount": opp.get("Amount") or 0,
        }
    return out


def transitions_by_lead(path):
    """Welke leads gingen naar Appointment of Re-entered, uit de LeadHistory-export."""
    to_appointment = set()
    to_sql = set()
    if not os.path.exists(path):
        print(f"WAARSCHUWING: {path} ontbreekt; doorstroomcijfers blijven leeg.")
        return to_appointment, to_sql

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            new = (row.get("New Value") or "").strip()
            if new == APPOINTMENT_STATUS:
                to_appointment.add(row["Lead ID"])
            elif new == SQL_STATUS:
                to_sql.add(row["Lead ID"])
    return to_appointment, to_sql


def main():
    if not os.path.exists(REACHED_PATH):
        raise SystemExit(
            f"{REACHED_PATH} ontbreekt. Draai eerst scripts/export_sfmc_tracking.py."
        )
    with open(REACHED_PATH, encoding="utf-8") as f:
        reached = json.load(f)

    token, instance_url = get_token()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    conversions = conversions_by_lead(session, instance_url)
    to_appointment, to_sql = transitions_by_lead(REPORT_PATH)
    print(
        f"Conversies in {LOOKBACK_DAYS} dagen: {len(conversions)} | "
        f"leads naar Appointment: {len(to_appointment)} | naar Re-entered: {len(to_sql)}"
    )

    markets = {}
    for market, block in (reached.get("markets") or {}).items():
        by_journey = block.get("by_journey") or {}
        flows = {}

        for journey, lead_ids in by_journey.items():
            leads = set(lead_ids)
            converted = leads & set(conversions)
            won = [conversions[i] for i in converted if conversions[i]["won"]]
            revenue = sum(c["amount"] for c in won)

            flows[journey] = {
                "touched": len(leads),
                "toAppointment": len(leads & to_appointment),
                "toSql": len(leads & to_sql),
                "converted": len(converted),
                "won": len(won),
                "revenue": round(revenue, 2),
                "revenuePerTouch": round(revenue / len(leads), 2) if leads else None,
                "appointmentRate": (
                    round(len(leads & to_appointment) / len(leads) * 100, 1) if leads else None
                ),
                "conversionRate": (
                    round(len(converted) / len(leads) * 100, 1) if leads else None
                ),
            }

        # Totaal over de markt: ontdubbeld, want een lead kan door meerdere
        # journeys geraakt zijn. Optellen van de flows zou dubbeltellen.
        all_leads = set(block.get("all") or [])
        all_converted = all_leads & set(conversions)
        all_won = [conversions[i] for i in all_converted if conversions[i]["won"]]
        all_revenue = sum(c["amount"] for c in all_won)
        flows["all"] = {
            "touched": len(all_leads),
            "toAppointment": len(all_leads & to_appointment),
            "toSql": len(all_leads & to_sql),
            "converted": len(all_converted),
            "won": len(all_won),
            "revenue": round(all_revenue, 2),
            "revenuePerTouch": (
                round(all_revenue / len(all_leads), 2) if all_leads else None
            ),
            "appointmentRate": (
                round(len(all_leads & to_appointment) / len(all_leads) * 100, 1)
                if all_leads else None
            ),
            "conversionRate": (
                round(len(all_converted) / len(all_leads) * 100, 1) if all_leads else None
            ),
        }
        markets[market] = flows

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback_days": LOOKBACK_DAYS,
        "attribution": (
            "touch-attributie: lead is geraakt door de flow en heeft daarna "
            "geconverteerd. Geen bewezen oorzaak, geen controlegroep."
        ),
        "markets": markets,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print(f"\nGeschreven naar {OUT_PATH}")
    for market, flows in sorted(markets.items()):
        print(f"\n  {market.upper()}  {'journey':40} {'geraakt':>8} {'afspr':>6} {'conv':>5} {'omzet':>12}")
        for journey, m in sorted(flows.items(), key=lambda x: -x[1]["revenue"]):
            print(
                f"        {journey[:40]:40} {m['touched']:8} {m['toAppointment']:6} "
                f"{m['won']:5} {m['revenue']:12,.0f}"
            )


if __name__ == "__main__":
    main()
