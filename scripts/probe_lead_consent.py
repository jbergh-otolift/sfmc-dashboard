#!/usr/bin/env python3
"""
probe_lead_consent.py

Diagnose-script, géén export. Brengt in kaart welke Lead-RecordTypes er zijn,
hoe ze heten, en hoeveel leads met consent er per RecordType zijn — de basis
voor Automation Coverage per land.

Schrijft niets weg en verandert niets in Salesforce; alleen leesacties, de
uitvoer gaat naar de console (in GitHub Actions: naar de job-log).

Gebruik:
    SF_DOMAIN=... SF_CLIENT_ID=... SF_CLIENT_SECRET=... python scripts/probe_lead_consent.py
"""

import os
import sys
from collections import defaultdict

import requests

DOMAIN = os.environ["SF_DOMAIN"]
CLIENT_ID = os.environ["SF_CLIENT_ID"]
CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
API_VERSION = "v60.0"

CONSENT_FIELD = "Customized_Product_Advice__c"
OPTOUT_FIELD = "HasOptedOutOfEmail"
CONSENT_WHERE = f"{CONSENT_FIELD} = true AND {OPTOUT_FIELD} = false"


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


def whoami(token):
    resp = requests.get(
        f"https://{DOMAIN}/services/oauth2/userinfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return resp.json() if resp.ok else None


def describe_lead(token, instance_url):
    resp = requests.get(
        f"{instance_url}/services/data/{API_VERSION}/sobjects/Lead/describe",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return {f["name"]: f for f in resp.json()["fields"]}


def query(token, instance_url, soql):
    resp = requests.get(
        f"{instance_url}/services/data/{API_VERSION}/query",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": soql},
        timeout=60,
    )
    if not resp.ok:
        return None, f"{resp.status_code} {resp.text[:400]}"
    return resp.json(), None


def main():
    token, instance_url = get_token()

    info = whoami(token)
    if info:
        print(f"Run As: {info.get('preferred_username')} ({info.get('name')})\n")

    # --- Veldzichtbaarheid -------------------------------------------------
    fields = describe_lead(token, instance_url)
    missing = [
        name
        for name in (CONSENT_FIELD, OPTOUT_FIELD, "RecordTypeId", "CreatedDate")
        if name not in fields
    ]
    if missing:
        print("Niet zichtbaar voor dit profiel:", ", ".join(missing))
        sys.exit(1)
    print("Alle benodigde velden zichtbaar.\n")

    # --- Alle Lead-RecordTypes met hun namen -------------------------------
    # Dit is de kern: welke RecordTypes bestaan er, hoe heten ze, en welke
    # zijn 'particulier' per land (tegenover bv. zakelijk).
    rts, err = query(
        token,
        instance_url,
        "SELECT Id, Name, DeveloperName, IsActive FROM RecordType "
        "WHERE SobjectType = 'Lead' ORDER BY Name",
    )
    if err:
        print(f"RecordType-query mislukt: {err}")
        sys.exit(1)

    record_types = {r["Id"]: r for r in rts["records"]}
    print(f"Lead-RecordTypes ({len(record_types)}):")
    print(f"  {'Id':20} {'actief':7} {'Name':38} DeveloperName")
    for r in rts["records"]:
        active = "ja" if r["IsActive"] else "NEE"
        print(f"  {r['Id']:20} {active:7} {r['Name']:38} {r['DeveloperName']}")
    print()

    # --- Tellingen per RecordType: totaal en met consent -------------------
    totals = defaultdict(int)
    consent = defaultdict(int)

    all_rows, err = query(
        token, instance_url,
        "SELECT RecordTypeId, COUNT(Id) total FROM Lead GROUP BY RecordTypeId",
    )
    if err:
        print(f"Totaaltelling mislukt: {err}")
    else:
        for row in all_rows["records"]:
            totals[row["RecordTypeId"]] = row["total"]

    consent_rows, err = query(
        token, instance_url,
        f"SELECT RecordTypeId, COUNT(Id) total FROM Lead WHERE {CONSENT_WHERE} "
        "GROUP BY RecordTypeId",
    )
    if err:
        print(f"Consenttelling mislukt: {err}")
        sys.exit(1)
    for row in consent_rows["records"]:
        consent[row["RecordTypeId"]] = row["total"]

    print("Per RecordType — totaal, met consent, en het aandeel:")
    print(f"  {'Name':40} {'totaal':>9} {'consent':>9} {'aandeel':>8}")
    for rtid in sorted(totals, key=lambda k: -totals[k]):
        rt = record_types.get(rtid)
        name = rt["Name"] if rt else f"(onbekend {rtid})"
        tot, con = totals[rtid], consent.get(rtid, 0)
        share = f"{con / tot * 100:.1f}%" if tot else "-"
        print(f"  {name:40} {tot:9} {con:9} {share:>8}")

    print()
    print(f"  {'TOTAAL':40} {sum(totals.values()):9} {sum(consent.values()):9}")

    # --- Groei: nieuwe leads met consent in de laatste 30 dagen ------------
    growth, err = query(
        token, instance_url,
        f"SELECT RecordTypeId, COUNT(Id) total FROM Lead "
        f"WHERE {CONSENT_WHERE} AND CreatedDate = LAST_N_DAYS:30 GROUP BY RecordTypeId",
    )
    if not err:
        print("\nNieuw met consent, laatste 30 dagen:")
        for row in growth["records"]:
            rt = record_types.get(row["RecordTypeId"])
            name = rt["Name"] if rt else f"(onbekend {row['RecordTypeId']})"
            print(f"  {name:40} {row['total']:9}")

    probe_history_recordtype(token, instance_url)
    probe_contact(token, instance_url)


def probe_history_recordtype(token, instance_url):
    """Kan de LeadHistory-query het RecordType van de bovenliggende Lead
    meenemen? Zo ja, dan kan de her-activatiefunnel per markt gesplitst worden
    zonder een tweede query."""
    print("\n--- LeadHistory met Lead.RecordTypeId ---")
    soql = (
        "SELECT CreatedDate, LeadId, Lead.RecordTypeId, Field, OldValue, NewValue "
        "FROM LeadHistory WHERE Field = 'Status' "
        "AND CreatedDate >= 2026-09-01T00:00:00Z LIMIT 3"
    )
    rows, err = query(token, instance_url, soql)
    if err:
        print(f"  ❌ traversal werkt NIET: {err}")
        print("  -> dan is een aparte Lead-query nodig om LeadId aan markt te koppelen")
        return
    print("  ✅ traversal werkt")
    for r in rows["records"]:
        lead = r.get("Lead") or {}
        print(f"     {r['LeadId']}  RecordTypeId={lead.get('RecordTypeId')}  {r['OldValue']} -> {r['NewValue']}")


def probe_contact(token, instance_url):
    """SFMC SubscriberKeys beginnen met 003 = Contact, niet 00Q = Lead. De
    teller van Automation Coverage gaat dus over Contacts, en de noemer moet
    dat ook doen. Bestaan de consent-velden daar?"""
    print("\n--- Contact (SubscriberKey = 003... = Contact) ---")
    resp = requests.get(
        f"{instance_url}/services/data/{API_VERSION}/sobjects/Contact/describe",
        headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if not resp.ok:
        print(f"  ❌ geen leesrecht op Contact: {resp.status_code}")
        return
    fields = {f["name"] for f in resp.json()["fields"]}
    print(f"  Contact-velden zichtbaar: {len(fields)}")
    for name in (CONSENT_FIELD, OPTOUT_FIELD, "RecordTypeId", "CreatedDate"):
        print(f"  {'✅' if name in fields else '❌'} {name}")

    if CONSENT_FIELD not in fields:
        print("  -> consent-veld bestaat niet op Contact; coverage-noemer moet anders")
        return

    tot, err = query(token, instance_url, "SELECT COUNT(Id) total FROM Contact")
    if not err:
        print(f"  Contacts totaal: {tot['records'][0]['total']}")
    con, err = query(token, instance_url,
                     f"SELECT COUNT(Id) total FROM Contact WHERE {CONSENT_WHERE}")
    if not err:
        print(f"  Contacts met consent: {con['records'][0]['total']}")

    if "RecordTypeId" in fields:
        rts, err = query(token, instance_url,
            "SELECT Id, Name FROM RecordType WHERE SobjectType = 'Contact' ORDER BY Name")
        if not err:
            names = {r["Id"]: r["Name"] for r in rts["records"]}
            grouped, err = query(token, instance_url,
                f"SELECT RecordTypeId, COUNT(Id) total FROM Contact "
                f"WHERE {CONSENT_WHERE} GROUP BY RecordTypeId")
            if not err:
                print("  Met consent per Contact-RecordType:")
                for row in grouped["records"]:
                    label = names.get(row["RecordTypeId"], f"(onbekend {row['RecordTypeId']})")
                    print(f"    {label:40} {row['total']:9}")


if __name__ == "__main__":
    main()
