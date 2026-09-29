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

import csv
import json
import os
from datetime import datetime, timedelta, timezone

import requests

DOMAIN = os.environ["SF_DOMAIN"]
CLIENT_ID = os.environ["SF_CLIENT_ID"]
CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
API_VERSION = "v60.0"

OUT_PATH = "exports/lead_consent.json"
# Geschreven door export_sfmc_tracking.py, niet gecommit. Bevat de Lead-id's
# die in de periode daadwerkelijk een e-mail kregen.
REACHED_PATH = "exports/_reached_lead_ids.json"
# Statushistorie, voor de vraag wie er *in de periode* in de mailflow kwam.
REPORT_PATH = "exports/report.csv"

# Periodes waarover het dashboard cohorten toont.
PERIOD_DAYS = [7, 30, 90]

# De coverage-noemer is niet "iedereen met consent" maar "iedereen die we
# horen te mailen": leads die in de mailflow staan én consent hebben.
MAILJOURNEY_STATUS = "Mailjourney"

CONSENT_FIELD = "Customized_Product_Advice__c"
OPTOUT_FIELD = "HasOptedOutOfEmail"
CONSENT_WHERE = f"{CONSENT_FIELD} = true AND {OPTOUT_FIELD} = false"

# Consent alleen is niet genoeg om iemand te kunnen mailen: zonder adres lukt
# het hoe dan ook niet. Die groep hoort niet in de coverage-noemer — voor NL
# ging het om 807 leads, 6,9% van de noemer.
# Uitzondering: leads met een 'no contact possible'-reden worden sowieso
# gemaild, ook zonder marketing-consent — dat is de enige manier om ze nog te
# bereiken. Afgemeld blijft wel een harde stop: HasOptedOutOfEmail = true is
# een uitschrijving, iets anders dan het ontbreken van een opt-in.
REASON_FIELD = "Reason_Mailjourney__c"
NO_CONTACT_REASONS = [
    "No Contact Possible - number correct",
    "No Contact Possible - number incorrect",
    "No contact possible",
]
_reasons = ", ".join(f"'{r}'" for r in NO_CONTACT_REASONS)
NO_CONTACT_WHERE = f"{REASON_FIELD} IN ({_reasons})"
# SOQL wil "veld NOT IN (...)", niet "NOT veld IN (...)".
NOT_NO_CONTACT_WHERE = f"({REASON_FIELD} = null OR {REASON_FIELD} NOT IN ({_reasons}))"

# Mailbaar = een adres, niet afgemeld, en ofwel consent ofwel een
# no-contact-possible-reden.
MAILABLE_WHERE = (
    f"Email != null AND {OPTOUT_FIELD} = false "
    f"AND ({CONSENT_FIELD} = true OR {NO_CONTACT_WHERE})"
)

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
    """Alle Lead-id's die we werkelijk kunnen mailen, per markt.

    De coverage-noemer: status Mailjourney, een e-mailadres, consent, en niet
    afgemeld — alle vier de voorwaarden. Een lead met consent zonder adres kun
    je niet bereiken en telt dus niet mee. Paginatie via nextRecordsUrl.
    """
    soql = (
        f"SELECT Id, RecordTypeId FROM Lead "
        f"WHERE Status = '{MAILJOURNEY_STATUS}' AND {MAILABLE_WHERE}"
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


def cohort_entered(path, end_date, days):
    """Lead-id's die binnen het venster in de mailflow zijn beland, per markt.

    Dit is de instroom van de periode, niet de staande voorraad. Een noemer
    van 11.000 leads voor een maand klopt niet: dat is iedereen die ooit in
    Mailjourney is gezet en daar nog staat. De echte maandinstroom ligt rond
    de 1.450 voor NL.
    """
    end = datetime.strptime(end_date, "%Y-%m-%d")
    start = (end - timedelta(days=days)).strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    by_market = {market: set() for market in MARKET_BY_RECORD_TYPE.values()}
    if not os.path.exists(path):
        return by_market

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("New Value") or "").strip() != MAILJOURNEY_STATUS:
                continue
            day = (row.get("Edit Date") or "")[:10]
            if not (start <= day < end_str):
                continue
            market = (row.get("Market") or "").strip().lower()
            if market in by_market:
                by_market[market].add(row["Lead ID"])
    return by_market


def mailable_lead_ids(session, instance_url):
    """Alle mailbare Lead-id's, ongeacht hun huidige status.

    Voor een cohort is de huidige status niet relevant: een lead die in de
    periode in de mailflow kwam en inmiddels een afspraak heeft, hoort gewoon
    in de noemer. Filteren op status Mailjourney zou juist de leads wegstrepen
    waar het goed ging.
    """
    soql = f"SELECT Id, RecordTypeId FROM Lead WHERE {MAILABLE_WHERE}"
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


def cohort_stats(entered, mailable, reached):
    """Instroom van een periode, afgezet tegen wie mailbaar was en bereikt is."""
    mailable_in = entered & mailable
    reached_in = (mailable_in & reached) if reached is not None else None
    return {
        "entered": len(entered),
        "mailable": len(mailable_in),
        "reached": len(reached_in) if reached_in is not None else None,
        "coverage": (
            round(len(reached_in) / len(mailable_in) * 100, 1)
            if reached_in is not None and mailable_in else None
        ),
    }


def load_reached():
    """Bereikte Lead-id's per markt, uit de SFMC-export. Ontbreekt dat bestand
    (los gedraaid, of SFMC-stap overgeslagen), dan blijft de doorsnede None."""
    if not os.path.exists(REACHED_PATH):
        return None
    with open(REACHED_PATH, encoding="utf-8") as f:
        data = json.load(f)
    # Het bestand bevat per markt {"all": [...], "by_journey": {...}}; voor
    # coverage is alleen de ontdubbelde "all" nodig.
    out = {}
    for market, block in (data.get("markets") or {}).items():
        ids = block.get("all") if isinstance(block, dict) else block
        out[market] = set(ids or [])
    out["_days"] = data.get("coverage_days")
    return out


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

    # Waarom valt een Mailjourney-lead af? Drie elkaar uitsluitende groepen,
    # zodat het dashboard het gat kan verklaren in plaats van alleen te tonen.
    mj = f"Status = '{MAILJOURNEY_STATUS}'"
    mj_total, _ = counts_by_market(session, instance_url, mj)
    mj_no_email, _ = counts_by_market(session, instance_url, f"{mj} AND Email = null")
    # SOQL accepteert NOT (A AND B) niet; uitgeschreven volgens De Morgan.
    # Afvallers: wel een adres, maar geen consent en ook geen
    # no-contact-possible-reden, of afgemeld.
    mj_no_consent, _ = counts_by_market(
        session, instance_url,
        f"{mj} AND Email != null "
        f"AND ({OPTOUT_FIELD} = true "
        f"OR ({CONSENT_FIELD} = false AND {NOT_NO_CONTACT_WHERE}))")

    # Hoeveel leads komen er dankzij de uitzondering bij?
    mj_no_contact, _ = counts_by_market(
        session, instance_url,
        f"{mj} AND Email != null AND {OPTOUT_FIELD} = false "
        f"AND {CONSENT_FIELD} = false AND {NO_CONTACT_WHERE}")

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
    coverage_days = (reached or {}).pop("_days", None) if reached else None
    if reached is None:
        warnings.append(
            f"{REACHED_PATH} ontbreekt; coverage niet berekend. "
            "Draai eerst scripts/export_sfmc_tracking.py.")

    # Cohort per periode: wie kwam er in de mailflow, en hebben we die gemaild?
    end_date = os.environ.get(
        "PERIOD_END",
        (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d"),
    )
    cohorts = {
        str(days): cohort_entered(REPORT_PATH, end_date, days)
        for days in PERIOD_DAYS
    }
    mailable_any_status = mailable_lead_ids(session, instance_url)

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
            # Uitsplitsing van de Mailjourney-populatie: waarom valt iemand af?
            "mailjourneyTotal": mj_total[market],
            "mailjourneyNoEmail": mj_no_email[market],
            "mailjourneyNoConsent": mj_no_consent[market],
            # Zonder consent, maar toch mailbaar via de no-contact-uitzondering.
            "mailjourneyNoContactException": mj_no_contact[market],
            # Per periode: van de leads die in die periode in de mailflow
            # kwamen en mailbaar zijn, hoeveel kregen er mail?
            "cohorts": {
                period: cohort_stats(
                    entered.get(market, set()),
                    mailable_any_status.get(market, set()),
                    reached_here,
                )
                for period, entered in cohorts.items()
            },
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
        # Coverage heeft een eigen, langer venster dan de rest van het
        # dashboard: de noemer is een momentopname, dus een kort venster zou
        # iedereen in een trage nurture-flow onterecht als gemist tellen.
        "coverage_days": coverage_days,
        "coverage_definition": (
            f"Status = '{MAILJOURNEY_STATUS}' AND {MAILABLE_WHERE}; "
            f"bereikt = minstens een e-mail in de laatste {coverage_days} dagen"
        ),
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
