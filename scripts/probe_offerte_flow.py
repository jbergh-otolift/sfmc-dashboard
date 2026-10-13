#!/usr/bin/env python3
"""
probe_offerte_flow.py

Diagnose, geen export. Schrijft niets weg en commit niets: de uitslag lees je
in de job-log.

De vraag: levert de offerteflow orders op, en langs welke weg is dat te zien?

Waarom dit nodig is. De hele toerekening van het dashboard werkt op leads:
SubscriberKeys die met `00Q` beginnen, de statushistorie uit LeadHistory, en
`Lead.ConvertedOpportunityId`. Een meting op de SFMC-tracking liet zien dat de
offerteflow in 60 dagen 120 unieke ontvangers had, en dat die allemaal met
`003` beginnen — Contacts, geen Leads. Dat is ook logisch: ligt er een
offerte, dan is de lead al geconverteerd en bestaat hij verder als Contact.
De flow valt daardoor buiten elke telling.

Deze probe meet welke brug terug naar een order werkt, en hoeveel die oplevert:

    A. Quote.ContactId            mail -> Contact -> Quote (kortst, als het veld bestaat)
    B. Lead.ConvertedContactId    mail -> Contact -> Lead -> Opportunity -> Quote
    C. OpportunityContactRole     mail -> Contact -> Opportunity -> Quote

Gebruik (draait in de Action, die de secrets heeft):
    SFMC_SUBDOMAIN=... SFMC_CLIENT_ID=... SFMC_CLIENT_SECRET=... SFMC_MID_NL=...
    SF_DOMAIN=... SF_CLIENT_ID=... SF_CLIENT_SECRET=... python scripts/probe_offerte_flow.py
"""

import os
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_sfmc_tracking as T  # noqa: E402

SF_DOMAIN = os.environ["SF_DOMAIN"]
SF_CLIENT_ID = os.environ["SF_CLIENT_ID"]
SF_CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
API_VERSION = "v60.0"

DAGEN = int(os.environ.get("PROBE_DAYS", "90"))
# Welke flows we als offerteflow beschouwen. Zelfde patroon als de export.
OFFERTE_PATRONEN = ("offerte",)

# Salesforce accepteert maar een beperkt aantal id's per IN-clausule.
BATCH = 180


def sf_token():
    resp = requests.post(
        f"https://{SF_DOMAIN}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": SF_CLIENT_ID,
            "client_secret": SF_CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data["instance_url"]


def query_all(session, instance_url, soql):
    url = f"{instance_url}/services/data/{API_VERSION}/query"
    params = {"q": soql}
    rows = []
    while True:
        resp = session.get(url, params=params, timeout=120)
        if not resp.ok:
            raise RuntimeError(f"SOQL mislukt ({resp.status_code}): {resp.text[:400]}\n{soql[:300]}")
        data = resp.json()
        rows.extend(data["records"])
        if data.get("done", True):
            break
        url = instance_url + data["nextRecordsUrl"]
        params = None
    return rows


def velden(session, instance_url, sobject):
    """Welke velden heeft dit object? Zo weten we of een route bestaat vóór
    we hem proberen, in plaats van een SOQL-fout te moeten lezen."""
    url = f"{instance_url}/services/data/{API_VERSION}/sobjects/{sobject}/describe"
    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    return {f["name"]: f["type"] for f in resp.json()["fields"]}


def in_batches(ids):
    ids = list(ids)
    for i in range(0, len(ids), BATCH):
        yield ids[i:i + BATCH]


def quoted(ids):
    return ", ".join("'" + i.replace("'", "") + "'" for i in ids)


def ontvangers_offerteflow():
    """De SubscriberKeys die de offerteflow in deze periode gemaild heeft."""
    subdomain = os.environ["SFMC_SUBDOMAIN"]
    client_id = os.environ["SFMC_CLIENT_ID"]
    client_secret = os.environ["SFMC_CLIENT_SECRET"]
    mid = os.environ["SFMC_MID_NL"]

    eind = date.today() + timedelta(days=1)
    start = eind - timedelta(days=DAGEN)

    session = T.make_session()
    auth = T.Auth(session, subdomain, client_id, client_secret, mid)
    _journeys, id_to_journey, email_to_journey, prefix_to_journey, _namen = T.fetch_journeys(
        session, auth)
    tsd_map, _names = T.fetch_tsd_map(
        session, auth, id_to_journey, email_to_journey, prefix_to_journey)

    keys = defaultdict(set)

    def on_page(rows):
        for r in rows:
            naam = tsd_map.get(r.get("TriggeredSendDefinitionObjectID") or "")
            if isinstance(naam, tuple):
                naam = naam[0]
            if not naam or not any(p in naam.lower() for p in OFFERTE_PATRONEN):
                continue
            key = (r.get("SubscriberKey") or "").strip()
            if key:
                keys[naam].add(key)

    T.soap_retrieve(
        session, auth, "SentEvent",
        ["SubscriberKey", "EventDate", "SendID", "TriggeredSendDefinitionObjectID"],
        start=start.isoformat() + "T00:00:00", end=eind.isoformat() + "T00:00:00",
        on_page=on_page)
    return keys


def main():
    print("=" * 72)
    print(f"PROBE OFFERTEFLOW · laatste {DAGEN} dagen")
    print("=" * 72)

    per_flow = ontvangers_offerteflow()
    alle = set().union(*per_flow.values()) if per_flow else set()
    print("\n1. Wie is er gemaild")
    for flow, keys in sorted(per_flow.items()):
        pre = Counter(k[:3] for k in keys)
        print(f"   {flow}: {len(keys)} unieke ontvangers · prefixen {dict(pre)}")
    if not alle:
        print("   GEEN ontvangers gevonden. Zonder ontvangers valt er niets te koppelen.")
        return
    contacten = {k for k in alle if k.startswith("003")}
    leads = {k for k in alle if k.startswith("00Q")}
    print(f"   totaal {len(alle)} · waarvan {len(contacten)} contacten en {len(leads)} leads")

    token, instance_url = sf_token()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    print("\n2. Welke velden bestaan er")
    qv = velden(session, instance_url, "Quote")
    for naam in ("ContactId", "OpportunityId", "AccountId", "Date_Quote_signed__c", "Status"):
        print(f"   Quote.{naam:24} {'ja · ' + qv[naam] if naam in qv else 'BESTAAT NIET'}")
    lv = velden(session, instance_url, "Lead")
    for naam in ("ConvertedContactId", "ConvertedOpportunityId", "ConvertedAccountId"):
        print(f"   Lead.{naam:25} {'ja · ' + lv[naam] if naam in lv else 'BESTAAT NIET'}")

    print(f"\n3. Getekende offertes in {DAGEN} dagen, totaal")
    getekend = query_all(
        session, instance_url,
        "SELECT Id, OpportunityId, Date_Quote_signed__c"
        + (", ContactId" if "ContactId" in qv else "")
        + " FROM Quote WHERE Date_Quote_signed__c != null "
        f"AND Date_Quote_signed__c = LAST_N_DAYS:{DAGEN}")
    print(f"   {len(getekend)} getekende offertes")
    met_opp = sum(1 for q in getekend if q.get("OpportunityId"))
    print(f"   waarvan met een Opportunity: {met_opp}")
    if "ContactId" in qv:
        met_con = sum(1 for q in getekend if q.get("ContactId"))
        print(f"   waarvan met een Contact:     {met_con}")

    # ---- route A: rechtstreeks via Quote.ContactId ----------------------
    print("\n4. Route A · Quote.ContactId")
    if "ContactId" not in qv:
        print("   niet mogelijk, het veld bestaat niet")
        treffers_a = set()
    else:
        treffers_a = {q["ContactId"] for q in getekend
                      if q.get("ContactId") in contacten}
        print(f"   {len(treffers_a)} van de {len(contacten)} gemailde contacten "
              f"heeft een getekende offerte")

    # ---- route B: via de lead waaruit het contact is ontstaan -----------
    print("\n5. Route B · Lead.ConvertedContactId → ConvertedOpportunityId → Quote")
    opp_van_contact = {}
    if "ConvertedContactId" in lv:
        for groep in in_batches(contacten):
            for row in query_all(
                session, instance_url,
                "SELECT Id, ConvertedContactId, ConvertedOpportunityId FROM Lead "
                f"WHERE ConvertedContactId IN ({quoted(groep)})"
            ):
                if row.get("ConvertedOpportunityId"):
                    opp_van_contact[row["ConvertedContactId"]] = row["ConvertedOpportunityId"]
        print(f"   {len(opp_van_contact)} van de {len(contacten)} contacten is terug te "
              f"voeren op een lead met een opportunity")
        opps = set(opp_van_contact.values())
        treffers_b = {c for c, o in opp_van_contact.items()
                      if o in {q.get("OpportunityId") for q in getekend}}
        print(f"   {len(treffers_b)} daarvan heeft een getekende offerte")
    else:
        treffers_b = set()
        print("   niet mogelijk, Lead.ConvertedContactId bestaat niet")

    # ---- route C: via de rol van het contact op de opportunity ----------
    print("\n6. Route C · OpportunityContactRole")
    try:
        rollen = defaultdict(set)
        for groep in in_batches(contacten):
            for row in query_all(
                session, instance_url,
                "SELECT ContactId, OpportunityId FROM OpportunityContactRole "
                f"WHERE ContactId IN ({quoted(groep)})"
            ):
                rollen[row["ContactId"]].add(row["OpportunityId"])
        getekende_opps = {q.get("OpportunityId") for q in getekend}
        treffers_c = {c for c, os_ in rollen.items() if os_ & getekende_opps}
        print(f"   {len(rollen)} contacten hebben een rol op een opportunity")
        print(f"   {len(treffers_c)} daarvan op een opportunity met getekende offerte")
    except Exception as e:  # noqa: BLE001
        treffers_c = set()
        print(f"   niet bruikbaar: {str(e)[:160]}")

    # ---- uitslag --------------------------------------------------------
    samen = treffers_a | treffers_b | treffers_c
    print("\n" + "=" * 72)
    print("UITSLAG")
    print("=" * 72)
    print(f"   gemailde contacten            {len(contacten)}")
    print(f"   route A · Quote.ContactId     {len(treffers_a)}")
    print(f"   route B · via de lead         {len(treffers_b)}")
    print(f"   route C · contactrol          {len(treffers_c)}")
    print(f"   samen, ontdubbeld             {len(samen)}"
          f"  ({len(samen)/len(contacten)*100:.0f}% van de gemailde contacten)")
    if samen:
        beste = max([("A", treffers_a), ("B", treffers_b), ("C", treffers_c)],
                    key=lambda x: len(x[1]))
        print(f"\n   Beste enkele route: {beste[0]} met {len(beste[1])} treffers.")
        extra = len(samen) - len(beste[1])
        if extra:
            print(f"   De andere routes voegen daar nog {extra} aan toe; "
                  "samen gebruiken levert dus meer op.")
    else:
        print("\n   Geen enkele route koppelt een gemailde ontvanger aan een "
              "getekende offerte. Dan valt er niets toe te rekenen en moet de "
              "koppeling uit Marketing Cloud zelf komen.")


if __name__ == "__main__":
    main()
