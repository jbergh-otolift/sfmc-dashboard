#!/usr/bin/env python3
"""
export_lead_consent.py

Haalt per markt op hoeveel leads er zijn, hoeveel daarvan marketing-consent
hebben, en hoeveel er in de periode bij zijn gekomen. Dat levert twee dingen
voor het dashboard:

  - de **noemer** van Automation Coverage (contacten met consent per land)
  - de **database-groei** (nieuwe contacten met consent in de periode)

De teller van coverage — hoeveel van die contacten ook daadwerkelijk een
touchpoint hadden — komt uit de Marketing Cloud sends, niet hieruit.

Gebruik:
    SF_DOMAIN=... SF_CLIENT_ID=... SF_CLIENT_SECRET=... python scripts/export_lead_consent.py

Env vars (optioneel):
    PERIOD_START / PERIOD_END   huidige periode, YYYY-MM-DD, end exclusief
    PREV_START   / PREV_END     vorige periode, voor de groei-delta

Schrijft `exports/lead_consent.json`.

Consent is gedefinieerd als `Customized_Product_Advice__c = true` EN
`HasOptedOutOfEmail = false` — beide condities, niet één van de twee.
"""

import json
import os
from datetime import datetime, timezone

import requests

DOMAIN = os.environ["SF_DOMAIN"]
CLIENT_ID = os.environ["SF_CLIENT_ID"]
CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
API_VERSION = "v60.0"

OUT_PATH = "exports/lead_consent.json"
# Geschreven door export_sfmc_tracking.py, niet gecommit. Bevat de Lead-id's
# die in de periode daadwerkelijk een e-mail kregen.
REACHED_PATH = "exports/_reached_lead_ids.json"

# De coverage-noemer is niet "iedereen met consent" maar "iedereen die we
# horen te mailen": leads die in de mailflow staan én consent hebben.
MAILJOURNEY_STATUS = "Mailjourney"

CONSENT_FIELD = "Customized_Product_Advice__c"
OPTOUT_FIELD = "HasOptedOutOfEmail"
CONSENT_WHERE = f"{CONSENT_FIELD} = true AND {OPTOUT_FIELD} = false"

# Alle vier de Lead-RecordTypes zijn "Particulier <land>"; er bestaan geen
# zakelijke Lead-RecordTypes, dus dit is de volledige set.
MARKET_BY_RECORD_TYPE = {
    "0127Q000000upr9QAA": "nl",
    "0127Q000000eERIQA2": "be",
    "012QD000002ylXZYAY": "fr",
    "012QD000002ylcPYAQ": "it",
}

DEFAULT_PERIOD = ("2026-08-20", "2026-09-09")
DEFAULT_PREV = ("2026-08-01", "2026-08-20")


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


def query(session, instance_url, soql):
    resp = session.get(
        f"{instance_url}/services/data/{API_VERSION}/query",
        params={"q": soql},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["records"]


def counts_by_market(session, instance_url, where):
    """Aantallen per markt voor een WHERE-clause, als {markt: aantal}."""
    soql = "SELECT RecordTypeId, COUNT(Id) total FROM Lead"
    if where:
        soql += f" WHERE {where}"
    soql += " GROUP BY RecordTypeId"

    out = {market: 0 for market in MARKET_BY_RECORD_TYPE.values()}
    unmapped = {}
    for row in query(session, instance_url, soql):
        market = MARKET_BY_RECORD_TYPE.get(row["RecordTypeId"])
        if market:
            out[market] = row["total"]
        else:
            unmapped[row["RecordTypeId"]] = row["total"]
    return out, unmapped


def lead_ids_in_mailjourney(session, instance_url):
    """Alle Lead-id's met status Mailjourney én consent, per markt.

    Dit is de coverage-noemer: de mensen die we horen te mailen. Gebruikt de
    gewone query-API met paginatie via nextRecordsUrl (circa 13.000 rijen).
    """
    soql = (
        f"SELECT Id, RecordTypeId FROM Lead "
        f"WHERE Status = '{MAILJOURNEY_STATUS}' AND {CONSENT_WHERE}"
    )
    by_market = {market: set() for market in MARKET_BY_RECORD_TYPE.values()}
    url = f"{instance_url}/services/data/{API_VERSION}/query"
    params = {"q": soql}

    while True:
        resp = session.get(url, params=params, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        for row in data["records"]:
            market = MARKET_BY_RECORD_TYPE.get(row["RecordTypeId"])
            if market:
                by_market[market].add(row["Id"])
        if data.get("done", True):
            break
        url = instance_url + data["nextRecordsUrl"]
        params = None

    return by_market


def load_reached():
    """Bereikte Lead-id's per markt, uit de SFMC-export. Ontbreekt dat bestand
    (los gedraaid, of SFMC-stap overgeslagen), dan blijft de doorsnede None."""
    if not os.path.exists(REACHED_PATH):
        return None
    with open(REACHED_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {market: set(ids) for market, ids in (data.get("markets") or {}).items()}


def main():
    period = (
        os.environ.get("PERIOD_START", DEFAULT_PERIOD[0]),
        os.environ.get("PERIOD_END", DEFAULT_PERIOD[1]),
    )
    prev = (
        os.environ.get("PREV_START", DEFAULT_PREV[0]),
        os.environ.get("PREV_END", DEFAULT_PREV[1]),
    )

    token, instance_url = get_token()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    warnings = []

    total, unmapped = counts_by_market(session, instance_url, None)
    if unmapped:
        warnings.append(f"Onbekende RecordTypeId's overgeslagen: {unmapped}")

    consent, _ = counts_by_market(session, instance_url, CONSENT_WHERE)

    # Groei: nieuwe contacten mét consent, aangemaakt binnen de periode.
    def created_between(start, end):
        where = (
            f"{CONSENT_WHERE} "
            f"AND CreatedDate >= {start}T00:00:00Z AND CreatedDate < {end}T00:00:00Z"
        )
        rows, _ = counts_by_market(session, instance_url, where)
        return rows

    growth = created_between(*period)
    growth_prev = created_between(*prev)

    # Coverage: van de leads die we horen te mailen, hoeveel kregen er mail?
    mailjourney = lead_ids_in_mailjourney(session, instance_url)
    reached = load_reached()
    if reached is None:
        warnings.append(
            f"{REACHED_PATH} ontbreekt; coverage niet berekend. "
            "Draai eerst scripts/export_sfmc_tracking.py.")

    markets = {}
    for market in MARKET_BY_RECORD_TYPE.values():
        con, tot = consent[market], total[market]
        cur_growth, prv_growth = growth[market], growth_prev[market]
        should_mail = mailjourney.get(market, set())
        reached_here = (reached or {}).get(market)
        covered = len(should_mail & reached_here) if reached_here is not None else None

        markets[market] = {
            "leadsTotal": tot,
            "consentTotal": con,
            # Coverage-noemer en -teller: in de mailflow met consent, en
            # daarvan degenen die in de periode echt een e-mail kregen.
            "shouldMail": len(should_mail),
            "reachedOfShouldMail": covered,
            "coverage": (
                round(covered / len(should_mail) * 100, 1)
                if covered is not None and should_mail else None
            ),
            "consentShare": round(con / tot * 100, 2) if tot else None,
            "growth": cur_growth,
            "growthPrev": prv_growth,
            "growthDeltaPct": (
                round((cur_growth - prv_growth) / prv_growth * 100, 1)
                if prv_growth
                else None
            ),
        }

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": {"start": period[0], "end": period[1]},
        "prev_period": {"start": prev[0], "end": prev[1]},
        "consent_definition": CONSENT_WHERE,
        "markets": markets,
        "warnings": warnings,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print(f"Geschreven naar {OUT_PATH}")
    print(f"  {'markt':6} {'totaal':>9} {'consent':>9} {'temailen':>9} {'bereikt':>8} {'coverage':>9} {'groei':>7}")
    for market, m in markets.items():
        cov = f"{m['coverage']}%" if m["coverage"] is not None else "-"
        reached_txt = m["reachedOfShouldMail"] if m["reachedOfShouldMail"] is not None else "-"
        print(
            f"  {market:6} {m['leadsTotal']:9} {m['consentTotal']:9} "
            f"{m['shouldMail']:9} {str(reached_txt):>8} {cov:>9} {m['growth']:7}"
        )
    for w in warnings:
        print(f"  WAARSCHUWING: {w}")


if __name__ == "__main__":
    main()
