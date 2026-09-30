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

Naast de routes wordt per conversie het volledige **statuspad** opgebouwd: een
leesbare tekenreeks als `Mailjourney (No contact possible) → Re-entered →
Appointment`. Daarmee is op het dashboard te zien welke volgorde van stappen
daadwerkelijk omzet oplevert, in plaats van alleen het eerste signaal.

LET OP bij `byFlow`: een lead kan door meerdere journeys zijn aangeraakt en
telt dan bij élke journey mee. De omzet in `byFlow` is dus **meervoudig
toegerekend** en mag niet worden opgeteld of vergeleken met het totaal. Dat
staat ook als `byFlowNote` in de output. `byRoute`, `byReason` en `byPath`
zijn wél exclusief: daar telt elke order precies één keer.

Gebruik:
    SF_DOMAIN=... SF_CLIENT_ID=... SF_CLIENT_SECRET=... python scripts/export_reactivation_value.py

Draai dit ná export_salesforce_report.py en export_sfmc_tracking.py (die laatste
schrijft `exports/_reached_lead_ids.json`, nodig voor `byFlow`).
Schrijft `exports/reactivation_value.json`.
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
REACHED_PATH = "exports/_reached_lead_ids.json"
OUT_PATH = "exports/reactivation_value.json"

# Ruim terugkijken: een lead die maanden geleden instroomde kan nu pas tekenen.
LOOKBACK_DAYS = int(os.environ.get("VALUE_LOOKBACK_DAYS", "400"))

MARKET_BY_RECORD_TYPE = {
    "0127Q000000upr9QAA": "nl",
    "0127Q000000eERIQA2": "be",
    "012QD000002ylXZYAY": "fr",
    "012QD000002ylcPYAQ": "it",
}

S_MAILJOURNEY = "Mailjourney"
S_APPOINTMENT = "Appointment"

# De routes waarlangs een lead vanuit de mailflow terugkomt. De volgorde
# bepaalt welke route wint als een lead er meerdere raakt: de eerste telt.
REENTRY_ROUTES = {
    "Re-entered": "Re-entered",
    "Re-entered - Phone Number Changed": "Nummer gewijzigd",
    S_APPOINTMENT: "Direct afspraak",
}

# Alleen deze statussen komen ná de Mailjourney-instroom in het statuspad.
# Al het overige (Lost, Not Qualified, New, Converted Old SF, ...) wordt
# genegeerd; anders wordt het pad onleesbaar en valt er niets meer te groeperen.
PATH_STATUSES = {
    "Re-entered",
    "Re-entered - Phone Number Changed",
    S_APPOINTMENT,
    "Follow-up",
    "Brochure",
    "Not reached",
}

# Hoeveel stappen ná de Mailjourney-instroom maximaal getoond worden.
PATH_MAX_STEPS = 4
PATH_ARROW = " → "
PATH_MORE = "…"


def build_status_path(reason, steps, until=None):
    """Bouwt de leesbare statuspad-string voor één lead.

    `steps` is een op datum gesorteerde lijst van (dag, status) ná de
    instroom in Mailjourney. `until` is de conversiedatum: stappen daarná
    horen niet in het pad, want die zeggen niets over de order.

    Directe herhalingen worden samengevouwen (A → A wordt A) en het pad
    wordt afgekapt op PATH_MAX_STEPS stappen, met een afsluitend '…'.
    """
    head = f"{S_MAILJOURNEY} ({reason})" if reason else S_MAILJOURNEY

    kept = []
    for day, status in steps:
        if until and day > until:
            continue
        if kept and kept[-1] == status:
            continue
        kept.append(status)

    truncated = len(kept) > PATH_MAX_STEPS
    parts = [head] + kept[:PATH_MAX_STEPS]
    path = PATH_ARROW.join(parts)
    if truncated:
        path += PATH_ARROW + PATH_MORE
    return path


def reached_by_flow(path):
    """Lead-ID -> journeys die die lead gemaild hebben.

    Eén lead kan in meerdere journeys zitten; die worden allemaal bewaard.
    Ontbreekt het bestand, dan geven we None terug en slaan we byFlow over.
    """
    if not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"WAARSCHUWING: {path} onleesbaar ({exc}).")
        return None

    by_lead = defaultdict(set)
    for per_market in (data.get("markets") or {}).values():
        for journey, lead_ids in (per_market.get("by_journey") or {}).items():
            for lead in lead_ids or ():
                by_lead[lead].add(journey)
    return by_lead


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
    # RecordTypeId meenemen, zodat ook orders van leads zónder mailflow-historie
    # aan een markt te koppelen zijn. Anders zou de noemer alleen uit
    # mailflow-leads bestaan en zou elk percentage 100% worden.
    soql = (
        "SELECT Id, RecordTypeId, ConvertedDate, ConvertedOpportunity.Amount, "
        "ConvertedOpportunity.IsWon "
        "FROM Lead "
        f"WHERE IsConverted = true AND ConvertedDate = LAST_N_DAYS:{LOOKBACK_DAYS}"
    )
    out = {}
    for row in query_all(session, instance_url, soql):
        opp = row.get("ConvertedOpportunity") or {}
        out[row["Id"]] = {
            "date": (row.get("ConvertedDate") or "")[:10],
            "market": MARKET_BY_RECORD_TYPE.get(row.get("RecordTypeId")),
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
        entry_reason = ""
        route = None
        route_date = None
        appointment = None
        steps = []

        for row in rows:
            new = (row.get("New Value") or "").strip()
            day = (row.get("Edit Date") or "")[:10]

            if entered is None:
                if new == S_MAILJOURNEY:
                    entered = day
                    entry_reason = (row.get("Reason") or "").strip()
                continue

            if route is None and new in REENTRY_ROUTES:
                route = new
                route_date = day
            if appointment is None and new == S_APPOINTMENT:
                appointment = day
            # Alleen de betekenisvolle statussen bewaren; de string zelf
            # wordt pas bij de conversie opgebouwd, want dan pas weten we
            # tot welke datum het pad loopt.
            if new in PATH_STATUSES:
                steps.append((day, new))

        if entered is None:
            continue

        last = rows[-1]
        paths[lead] = {
            "market": (last.get("Market") or "").strip().lower(),
            "reason": (last.get("Reason") or "").strip(),
            # De reden zoals vastgelegd bij de instroom in de mailflow; die
            # beschrijft waaróm de lead de flow in ging. Valt terug op de
            # laatst bekende reden als het instroommoment er geen had.
            "entryReason": entry_reason or (last.get("Reason") or "").strip(),
            "entered": entered,
            "route": route,
            "routeDate": route_date,
            "appointment": appointment,
            "steps": steps,
        }
    return paths


def main():
    token, instance_url = get_token()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    converted = conversions(session, instance_url)
    paths = reactivation_paths(REPORT_PATH)
    flows = reached_by_flow(REACHED_PATH)
    print(f"Conversies in {LOOKBACK_DAYS} dagen: {len(converted)}")
    print(f"Leads met een mailflow-historie: {len(paths)}")

    warnings = []
    if flows is None:
        warnings.append(
            f"{REACHED_PATH} ontbreekt of is onleesbaar; byFlow is overgeslagen."
        )
        print(f"WAARSCHUWING: {warnings[-1]}")

    def empty_day():
        return {
            "orders": 0,
            "revenue": 0.0,
            "viaAutomationOrders": 0,
            "viaAutomationRevenue": 0.0,
            "byRoute": defaultdict(lambda: {"orders": 0, "revenue": 0.0}),
            "byReason": defaultdict(lambda: {"orders": 0, "revenue": 0.0}),
            "byPath": defaultdict(lambda: {"orders": 0, "revenue": 0.0}),
            "byRouteReason": defaultdict(
                lambda: defaultdict(lambda: {"orders": 0, "revenue": 0.0})
            ),
            "byFlow": defaultdict(lambda: {"orders": 0, "revenue": 0.0}),
        }

    days = defaultdict(lambda: defaultdict(empty_day))
    # Totalen over de hele terugkijkperiode, per markt en per statuspad.
    path_totals = defaultdict(
        lambda: defaultdict(lambda: {"orders": 0, "revenue": 0.0, "leads": set()})
    )
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
        # Markt bij voorkeur uit de statushistorie, anders uit het RecordType
        # van de Lead. Zo tellen ook orders mee van leads die nooit in de
        # mailflow zaten — die horen in de noemer thuis.
        market = (info or {}).get("market") or conv.get("market")
        if not market:
            continue

        bucket = days[conv["date"]][market]
        bucket["orders"] += 1
        bucket["revenue"] += conv["amount"]

        # Toerekenen aan automation vereist: eerst de mailflow in, daarna een
        # heractivatiesignaal, en de conversie ná dat signaal. Zonder die
        # volgorde is het toeval in dezelfde periode, geen heractivatie.
        # Leads zonder mailflow-historie tellen alleen in de noemer mee.
        route = (info or {}).get("route")
        if route and info.get("routeDate") and conv["date"] >= info["routeDate"]:
            bucket["viaAutomationOrders"] += 1
            bucket["viaAutomationRevenue"] += conv["amount"]
            bucket["byRoute"][route]["orders"] += 1
            bucket["byRoute"][route]["revenue"] += conv["amount"]
            reason = info.get("reason") or "(geen reden)"
            bucket["byReason"][reason]["orders"] += 1
            bucket["byReason"][reason]["revenue"] += conv["amount"]
            # Reden gekruist met route, zodat een route uit te klappen is
            # naar de Mailjourney-redenen waar hij vandaan komt.
            cross = bucket["byRouteReason"][route][reason]
            cross["orders"] += 1
            cross["revenue"] += conv["amount"]
            routes[market][route]["orders"] += 1
            routes[market][route]["revenue"] += conv["amount"]

            # Het statuspad tot en met de laatste stap vóór de conversie.
            path_str = build_status_path(
                info.get("entryReason") or "",
                info.get("steps") or [],
                until=conv["date"],
            )
            bucket["byPath"][path_str]["orders"] += 1
            bucket["byPath"][path_str]["revenue"] += conv["amount"]
            total = path_totals[market][path_str]
            total["orders"] += 1
            total["revenue"] += conv["amount"]
            total["leads"].add(lead)

            # Meervoudig toegerekend: een lead die door drie journeys is
            # aangeraakt telt bij alle drie mee. Nooit optellen.
            if flows is not None:
                for journey in sorted(flows.get(lead, ())):
                    flow = bucket["byFlow"][journey]
                    flow["orders"] += 1
                    flow["revenue"] += conv["amount"]

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
                "byPath": flatten(bucket["byPath"]),
                "byRouteReason": {
                    route: flatten(per_reason)
                    for route, per_reason in bucket["byRouteReason"].items()
                },
            }
            if flows is not None:
                out_days[day][market]["byFlow"] = flatten(bucket["byFlow"])

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback_days": LOOKBACK_DAYS,
        "routeLabels": REENTRY_ROUTES,
        "attribution": (
            "Statuspad: instroom in Mailjourney, daarna een heractivatiesignaal "
            "(Re-entered, Nummer gewijzigd of direct Appointment), en de conversie "
            "ná dat signaal. Toegerekend aan de conversiedatum."
        ),
        # byFlow telt een order bij élke journey die de lead raakte. Kolommen
        # daaruit nooit optellen of naast het totaal leggen.
        "byFlowNote": "meervoudig toegerekend",
        "warnings": warnings,
        "days": out_days,
        "paths": {
            market: {
                path_str: {
                    "orders": v["orders"],
                    "revenue": round(v["revenue"], 2),
                    "leads": len(v["leads"]),
                }
                for path_str, v in per_path.items()
            }
            for market, per_path in path_totals.items()
        },
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
