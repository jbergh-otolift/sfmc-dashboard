#!/usr/bin/env python3
"""
export_reactivation_value.py

Hoeveel omzet komt er binnen via marketing automation, afgelezen aan het
statuspad van de lead?

Dit is een wezenlijk andere toerekening dan "deze lead kreeg een mail en
converteerde ook". Daar kan de order net zo goed vóór de mail zijn gesloten.
Hier telt alleen mee wat aantoonbaar door de mailflow is heropgeleefd, in deze
volgorde:

    Mailjourney  ->  een heractivatiesignaal  ->  (eventueel Appointment)  ->  Order

Het heractivatiesignaal is niet één status: een lead kan op verschillende
manieren terugkomen. Elke route wordt apart geteld, zodat zichtbaar is welke
het meeste oplevert:

  - Re-entered                          lead meldt zich weer / wordt bereikt
  - Re-entered - Phone Number Changed   pas bereikbaar na nummerverrijking
  - Appointment rechtstreeks            vanuit de mailflow direct een afspraak

Een afspraak is niet verplicht: sommige leads gaan van heractivatie naar order
zonder dat er een Appointment-status is vastgelegd. Of die stap gezet is,
wordt wel apart bijgehouden.

De omzet wordt toegerekend aan de **conversiedatum**, zodat de dagcijfers
optelbaar zijn en elk gekozen bereik exact klopt. Dit zegt dus: van de orders
die in deze periode binnenkwamen, kwam dit deel via automation — niet: van de
leads die instroomden leverde dit deel op.

Gebruik:
    SF_DOMAIN=... SF_CLIENT_ID=... SF_CLIENT_SECRET=... python scripts/export_reactivation_value.py

Draai dit ná export_salesforce_report.py. Schrijft `exports/reactivation_value.json`.
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

REPORT_PATH = "exports/report.csv"
OUT_PATH = "exports/reactivation_value.json"

# Ruim terugkijken: een lead die maanden geleden instroomde kan nu pas tekenen.
LOOKBACK_DAYS = int(os.environ.get("VALUE_LOOKBACK_DAYS", "400"))

S_MAILJOURNEY = "Mailjourney"
S_APPOINTMENT = "Appointment"

# De routes waarlangs een lead vanuit de mailflow terugkomt. De volgorde
# bepaalt welke route wint als een lead er meerdere raakt: de eerste telt.
REENTRY_ROUTES = {
    "Re-entered": "Re-entered",
    "Re-entered - Phone Number Changed": "Nummer gewijzigd",
    S_APPOINTMENT: "Direct afspraak",
}


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


def conversions(session, instance_url):
    """Geconverteerde leads met de waarde van hun order."""
    soql = (
        "SELECT Id, ConvertedDate, ConvertedOpportunity.Amount, "
        "ConvertedOpportunity.IsWon "
        "FROM Lead "
        f"WHERE IsConverted = true AND ConvertedDate = LAST_N_DAYS:{LOOKBACK_DAYS}"
    )
    out = {}
    for row in query_all(session, instance_url, soql):
        opp = row.get("ConvertedOpportunity") or {}
        out[row["Id"]] = {
            "date": (row.get("ConvertedDate") or "")[:10],
            "won": bool(opp.get("IsWon")),
            "amount": opp.get("Amount") or 0,
        }
    return out


def reactivation_paths(path):
    """Per lead: kwam die via de mailflow terug, en langs welke route?

    De statusovergangen worden op datum doorlopen. Pas ná een instroom in
    Mailjourney telt een heractivatiesignaal mee — dat is precies wat deze
    toerekening onderscheidt van 'heeft toevallig ook een mail gehad'.
    """
    if not os.path.exists(path):
        print(f"WAARSCHUWING: {path} ontbreekt.")
        return {}

    by_lead = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_lead[row["Lead ID"]].append(row)

    paths = {}
    for lead, rows in by_lead.items():
        rows.sort(key=lambda r: r.get("Edit Date") or "")

        entered = None
        route = None
        route_date = None
        appointment = None

        for row in rows:
            new = (row.get("New Value") or "").strip()
            day = (row.get("Edit Date") or "")[:10]

            if entered is None:
                if new == S_MAILJOURNEY:
                    entered = day
                continue

            if route is None and new in REENTRY_ROUTES:
                route = new
                route_date = day
            if appointment is None and new == S_APPOINTMENT:
                appointment = day

        if entered is None:
            continue

        last = rows[-1]
        paths[lead] = {
            "market": (last.get("Market") or "").strip().lower(),
            "reason": (last.get("Reason") or "").strip(),
            "entered": entered,
            "route": route,
            "routeDate": route_date,
            "appointment": appointment,
        }
    return paths


def main():
    token, instance_url = get_token()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    converted = conversions(session, instance_url)
    paths = reactivation_paths(REPORT_PATH)
    print(f"Conversies in {LOOKBACK_DAYS} dagen: {len(converted)}")
    print(f"Leads met een mailflow-historie: {len(paths)}")

    def empty_day():
        return {
            "orders": 0,
            "revenue": 0.0,
            "viaAutomationOrders": 0,
            "viaAutomationRevenue": 0.0,
            "byRoute": defaultdict(lambda: {"orders": 0, "revenue": 0.0}),
            "byReason": defaultdict(lambda: {"orders": 0, "revenue": 0.0}),
        }

    days = defaultdict(lambda: defaultdict(empty_day))
    routes = defaultdict(lambda: defaultdict(lambda: {
        "reached": 0, "withAppointment": 0, "orders": 0, "revenue": 0.0,
    }))

    # Hoeveel leads raakten elke route, los van of ze converteerden.
    for info in paths.values():
        if not info["market"] or not info["route"]:
            continue
        bucket = routes[info["market"]][info["route"]]
        bucket["reached"] += 1
        if info["appointment"]:
            bucket["withAppointment"] += 1

    for lead, conv in converted.items():
        if not conv["won"] or not conv["date"]:
            continue
        info = paths.get(lead)
        market = (info or {}).get("market")
        if not market:
            continue

        bucket = days[conv["date"]][market]
        bucket["orders"] += 1
        bucket["revenue"] += conv["amount"]

        # Toerekenen aan automation vereist: eerst de mailflow in, daarna een
        # heractivatiesignaal, en de conversie ná dat signaal. Zonder die
        # volgorde is het toeval in dezelfde periode, geen heractivatie.
        route = info.get("route")
        if route and info.get("routeDate") and conv["date"] >= info["routeDate"]:
            bucket["viaAutomationOrders"] += 1
            bucket["viaAutomationRevenue"] += conv["amount"]
            bucket["byRoute"][route]["orders"] += 1
            bucket["byRoute"][route]["revenue"] += conv["amount"]
            reason = info.get("reason") or "(geen reden)"
            bucket["byReason"][reason]["orders"] += 1
            bucket["byReason"][reason]["revenue"] += conv["amount"]
            routes[market][route]["orders"] += 1
            routes[market][route]["revenue"] += conv["amount"]

    def flatten(nested):
        return {
            key: {"orders": v["orders"], "revenue": round(v["revenue"], 2)}
            for key, v in nested.items()
        }

    out_days = {}
    for day, per_market in days.items():
        out_days[day] = {}
        for market, bucket in per_market.items():
            out_days[day][market] = {
                "orders": bucket["orders"],
                "revenue": round(bucket["revenue"], 2),
                "viaAutomationOrders": bucket["viaAutomationOrders"],
                "viaAutomationRevenue": round(bucket["viaAutomationRevenue"], 2),
                "byRoute": flatten(bucket["byRoute"]),
                "byReason": flatten(bucket["byReason"]),
            }

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback_days": LOOKBACK_DAYS,
        "routeLabels": REENTRY_ROUTES,
        "attribution": (
            "Statuspad: instroom in Mailjourney, daarna een heractivatiesignaal "
            "(Re-entered, Nummer gewijzigd of direct Appointment), en de conversie "
            "ná dat signaal. Toegerekend aan de conversiedatum."
        ),
        "days": out_days,
        "routes": {
            market: {
                route: {**v, "revenue": round(v["revenue"], 2)}
                for route, v in per_route.items()
            }
            for market, per_route in routes.items()
        },
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print(f"\nGeschreven naar {OUT_PATH}")
    for market, per_route in sorted(out["routes"].items()):
        print(f"\n  {market.upper()}  {'route':34} {'leads':>7} {'afspr':>7} {'orders':>7} {'omzet':>13}")
        for route, v in sorted(per_route.items(), key=lambda x: -x[1]["revenue"]):
            label = REENTRY_ROUTES.get(route, route)
            print(
                f"        {label:34} {v['reached']:7} {v['withAppointment']:7} "
                f"{v['orders']:7} {v['revenue']:13,.0f}"
            )


if __name__ == "__main__":
    main()
