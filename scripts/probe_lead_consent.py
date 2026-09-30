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
    probe_mailjourney(token, instance_url)
    probe_opportunity(token, instance_url)
    probe_mailable(token, instance_url)
    probe_reasons(token, instance_url)
    probe_order_counting(token, instance_url)
    probe_quote_fields(token, instance_url)


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


RT_NAMES = {
    "0127Q000000upr9QAA": "NL",
    "0127Q000000eERIQA2": "BE",
    "012QD000002ylXZYAY": "FR",
    "012QD000002ylcPYAQ": "IT",
}


def probe_mailjourney(token, instance_url):
    """De echte coverage-noemer: leads die we hóren te mailen, oftewel
    status Mailjourney met consent. Niet de hele database."""
    print("\n--- Coverage-noemer: status Mailjourney + consent ---")
    for label, where in [
        ("Mailjourney + consent", f"Status = 'Mailjourney' AND {CONSENT_WHERE}"),
        ("Mailjourney (alle)", "Status = 'Mailjourney'"),
    ]:
        res, err = query(
            token, instance_url,
            f"SELECT RecordTypeId, COUNT(Id) total FROM Lead WHERE {where} GROUP BY RecordTypeId")
        if err:
            print(f"  {label}: mislukt {err}")
            continue
        parts = " ".join(
            f"{RT_NAMES.get(r['RecordTypeId'], '?')}={r['total']}" for r in res["records"])
        print(f"  {label:26} {parts}")

    print("\n  Statusverdeling van leads met consent:")
    res, err = query(
        token, instance_url,
        f"SELECT Status, COUNT(Id) total FROM Lead WHERE {CONSENT_WHERE} "
        "GROUP BY Status ORDER BY COUNT(Id) DESC")
    if not err:
        for row in res["records"][:12]:
            print(f"    {str(row['Status']):34} {row['total']:8}")


def probe_opportunity(token, instance_url):
    """Kan de opbrengst-kant erbij? Opportunity draagt de orderwaarde, en
    Lead.ConvertedOpportunityId legt de link tussen een lead en die order."""
    print("\n--- Opportunity (opbrengst per flow) ---")
    resp = requests.get(
        f"{instance_url}/services/data/{API_VERSION}/sobjects/Opportunity/describe",
        headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if not resp.ok:
        print(f"  ❌ geen leesrecht op Opportunity: {resp.status_code}")
        return
    fields = {f["name"] for f in resp.json()["fields"]}
    print(f"  Opportunity-velden zichtbaar: {len(fields)}")
    for name in ("Amount", "StageName", "CloseDate", "RecordTypeId", "IsWon", "IsClosed"):
        print(f"  {'✅' if name in fields else '❌'} {name}")

    lead_fields = describe_lead(token, instance_url)
    for name in ("ConvertedOpportunityId", "ConvertedDate", "ConvertedContactId", "IsConverted"):
        print(f"  {'✅' if name in lead_fields else '❌'} Lead.{name}")

    if "Amount" in fields:
        res, err = query(
            token, instance_url,
            "SELECT COUNT(Id) n, SUM(Amount) bedrag FROM Opportunity "
            "WHERE IsWon = true AND CloseDate = LAST_N_DAYS:90")
        if not err and res["records"]:
            r = res["records"][0]
            print(f"  Gewonnen opportunities laatste 90 dagen: {r['n']}, totaal {r['bedrag']}")

    res, err = query(
        token, instance_url,
        "SELECT COUNT(Id) n FROM Lead WHERE IsConverted = true AND ConvertedDate = LAST_N_DAYS:90")
    if not err and res["records"]:
        print(f"  Leads geconverteerd laatste 90 dagen: {res['records'][0]['n']}")


def probe_mailable(token, instance_url):
    """Wie kun je werkelijk mailen?

    De dashboardnoemer keek alleen naar consent, niet naar de aanwezigheid van
    een e-mailadres. Een lead met consent zonder adres kun je niet bereiken en
    hoort dus niet in de noemer. Deze probe splitst Mailjourney-leads in drie
    elkaar uitsluitende groepen, zoals de Salesforce-analyse dat ook doet.
    """
    print("\n--- Wie kunnen we echt mailen? (status Mailjourney) ---")
    RT = {"0127Q000000upr9QAA": "NL", "0127Q000000eERIQA2": "BE",
          "012QD000002ylXZYAY": "FR", "012QD000002ylcPYAQ": "IT"}
    base = "Status = 'Mailjourney'"
    groups = {
        "totaal": base,
        "zonder mailadres": f"{base} AND Email = null",
        "adres, geen consent": f"{base} AND Email != null AND {CONSENT_FIELD} = false",
        "adres, consent, afgemeld": (
            f"{base} AND Email != null AND {CONSENT_FIELD} = true "
            f"AND {OPTOUT_FIELD} = true"),
        "WEL MAILEN": (
            f"{base} AND Email != null AND {CONSENT_FIELD} = true "
            f"AND {OPTOUT_FIELD} = false"),
        # Wat het dashboard nu als noemer gebruikt: zonder adrescontrole.
        "huidige noemer": f"{base} AND {CONSENT_WHERE}",
    }
    rows = {}
    for label, where in groups.items():
        res, err = query(
            token, instance_url,
            f"SELECT RecordTypeId, COUNT(Id) total FROM Lead WHERE {where} "
            "GROUP BY RecordTypeId")
        if err:
            print(f"  {label}: mislukt {err}")
            continue
        rows[label] = {RT.get(r["RecordTypeId"], "?"): r["total"] for r in res["records"]}

    markets = ["NL", "BE", "FR", "IT"]
    print(f"  {'groep':26} " + " ".join(f"{m:>8}" for m in markets))
    for label in groups:
        if label not in rows:
            continue
        line = " ".join(f"{rows[label].get(m, 0):8}" for m in markets)
        print(f"  {label:26} {line}")

    huidig = rows.get("huidige noemer", {})
    echt = rows.get("WEL MAILEN", {})
    print()
    print("  Verschil huidige noemer t.o.v. werkelijk mailbaar:")
    for m in markets:
        h, e = huidig.get(m, 0), echt.get(m, 0)
        if h:
            print(f"    {m}: {h} -> {e}  ({e - h:+d}, {(e - h) / h * 100:+.1f}%)")


def probe_reasons(token, instance_url):
    """Welke redenvelden bestaan er op Lead, en welke waarden staan erin?

    De groep 'no contact possible' wordt sowieso gemaild, ook zonder consent.
    Daarvoor moeten we weten in welk veld die reden staat en hoe de waarden
    exact gespeld zijn.
    """
    print("\n--- Redenvelden op Lead ---")
    fields = describe_lead(token, instance_url)
    reason_fields = sorted(
        name for name in fields
        if "reason" in name.lower() or "redenen" in name.lower()
    )
    for name in reason_fields:
        f = fields[name]
        print(f"  {name}  ({f['type']})")
        for value in (f.get("picklistValues") or [])[:30]:
            if value.get("active"):
                print(f"      {value['value']!r}")

    if not reason_fields:
        print("  geen veld met 'reason' in de naam gevonden")
        return

    # Hoeveel Mailjourney-leads hebben een no-contact-reden, en hoeveel
    # daarvan zouden we door de consent-eis nu missen?
    for name in reason_fields:
        res, err = query(
            token, instance_url,
            f"SELECT {name} r, COUNT(Id) total FROM Lead "
            f"WHERE Status = 'Mailjourney' AND {name} != null "
            f"GROUP BY {name} ORDER BY COUNT(Id) DESC")
        if err or not res["records"]:
            continue
        print(f"\n  Verdeling van {name} binnen Mailjourney:")
        for row in res["records"][:15]:
            print(f"      {str(row['r'])[:52]:54} {row['total']:7}")


def probe_order_counting(token, instance_url):
    """Klopt de ordertelling van het dashboard?

    Het dashboard telt orders als: Lead.IsConverted met een gewonnen
    Opportunity, toegerekend aan Lead.ConvertedDate. Drie dingen kunnen daar
    misgaan, en die controleren we hier:

      1. Orders die niet uit een leadconversie komen worden helemaal gemist.
      2. ConvertedDate is niet de orderdatum; de Opportunity kan veel later
         gewonnen zijn. Dan staat de order in de verkeerde periode.
      3. Meerdere leads kunnen naar dezelfde Opportunity converteren
         (samengevoegde leads), en dan tellen we dubbel.
    """
    print("\n--- Klopt de ordertelling? ---")

    res, err = query(
        token, instance_url,
        "SELECT COUNT(Id) n FROM Opportunity "
        "WHERE IsWon = true AND CloseDate = LAST_N_DAYS:90")
    won_total = res["records"][0]["n"] if not err else None
    print(f"  Gewonnen opportunities, CloseDate laatste 90 dagen : {won_total}")

    res, err = query(
        token, instance_url,
        "SELECT COUNT(Id) n FROM Lead WHERE IsConverted = true "
        "AND ConvertedDate = LAST_N_DAYS:90 AND ConvertedOpportunity.IsWon = true")
    via_lead = res["records"][0]["n"] if not err else None
    print(f"  Daarvan via een leadconversie (ConvertedDate)      : {via_lead}")

    # Hoeveel gewonnen opportunities in dit venster hebben helemaal geen
    # bronlead? Die mist het dashboard per definitie.
    res, err = query(
        token, instance_url,
        "SELECT COUNT(Id) n FROM Opportunity WHERE IsWon = true "
        "AND CloseDate = LAST_N_DAYS:90 "
        "AND Id NOT IN (SELECT ConvertedOpportunityId FROM Lead WHERE IsConverted = true)")
    if err:
        print(f"  Zonder bronlead: query mislukt ({err[:80]})")
    else:
        zonder = res["records"][0]["n"]
        aandeel = f"{zonder / won_total * 100:.0f}%" if won_total else "?"
        print(f"  Zonder bronlead (mist het dashboard)              : {zonder}  ({aandeel})")

    # Verschil tussen conversiedatum en sluitdatum: hoe scheef is de
    # toerekening aan ConvertedDate?
    res, err = query(
        token, instance_url,
        "SELECT Id, ConvertedDate, ConvertedOpportunity.CloseDate FROM Lead "
        "WHERE IsConverted = true AND ConvertedDate = LAST_N_DAYS:90 "
        "AND ConvertedOpportunity.IsWon = true LIMIT 2000")
    if not err:
        from datetime import date
        gaps = []
        for row in res["records"]:
            opp = row.get("ConvertedOpportunity") or {}
            cd, close = row.get("ConvertedDate"), opp.get("CloseDate")
            if not cd or not close:
                continue
            a = date(*map(int, cd[:10].split("-")))
            b = date(*map(int, close[:10].split("-")))
            gaps.append((b - a).days)
        if gaps:
            gaps.sort()
            same = sum(1 for g in gaps if g == 0)
            print(f"  Dagen tussen conversie en sluiten (n={len(gaps)}):")
            print(f"    zelfde dag {same} ({same / len(gaps) * 100:.0f}%) · "
                  f"mediaan {gaps[len(gaps) // 2]} · "
                  f"90e percentiel {gaps[int(len(gaps) * 0.9)]} · max {gaps[-1]}")

    # Dubbeltelling: meer dan een lead naar dezelfde Opportunity?
    res, err = query(
        token, instance_url,
        "SELECT ConvertedOpportunityId oid, COUNT(Id) n FROM Lead "
        "WHERE IsConverted = true AND ConvertedDate = LAST_N_DAYS:90 "
        "GROUP BY ConvertedOpportunityId HAVING COUNT(Id) > 1")
    if err:
        print(f"  Dubbeltelling: query mislukt ({err[:80]})")
    else:
        dubbel = res["records"]
        extra = sum(r["n"] - 1 for r in dubbel)
        print(f"  Opportunities met meerdere bronleads              : {len(dubbel)}"
              f"  (dat zijn {extra} dubbeltellingen)")


def probe_quote_fields(token, instance_url):
    """Is er een veld met de datum waarop de offerte getekend is?

    CloseDate is in Salesforce vaak een verwachte sluitdatum die bij het
    aanmaken wordt gezet en niet wordt bijgewerkt bij het winnen. Voor de
    offerteflow hebben we het werkelijke tekenmoment nodig.
    """
    print("\n--- Datumvelden op Opportunity ---")
    resp = requests.get(
        f"{instance_url}/services/data/{API_VERSION}/sobjects/Opportunity/describe",
        headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if not resp.ok:
        print(f"  geen leesrecht: {resp.status_code}")
        return
    fields = resp.json()["fields"]

    interesting = [
        f for f in fields
        if f["type"] in ("date", "datetime")
        and any(w in (f["name"] + " " + (f.get("label") or "")).lower()
                for w in ("quote", "sign", "getekend", "offerte", "close", "won", "order"))
    ]
    print(f"  {'veld':44} {'type':9} label")
    for f in interesting:
        print(f"  {f['name'][:44]:44} {f['type']:9} {f.get('label')}")

    # Klopt CloseDate als winmoment? Vergelijk met LastModifiedDate.
    res, err = query(
        token, instance_url,
        "SELECT CloseDate, LastModifiedDate FROM Opportunity "
        "WHERE IsWon = true AND CloseDate = LAST_N_DAYS:90 LIMIT 500")
    if not err and res["records"]:
        from datetime import date
        vooruit = achter = gelijk = 0
        for row in res["records"]:
            cd = row.get("CloseDate"); lm = (row.get("LastModifiedDate") or "")[:10]
            if not cd or not lm:
                continue
            if cd == lm:
                gelijk += 1
            elif cd < lm:
                achter += 1
            else:
                vooruit += 1
        total = gelijk + achter + vooruit
        print(f"\n  CloseDate versus LastModifiedDate (n={total}):")
        print(f"    zelfde dag {gelijk} · CloseDate eerder {achter} · CloseDate later {vooruit}")


if __name__ == "__main__":
    main()
