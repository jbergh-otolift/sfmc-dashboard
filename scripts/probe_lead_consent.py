#!/usr/bin/env python3
"""
probe_lead_consent.py

Diagnose-script, géén export. Controleert of de consent-velden die Automation
Coverage en de database-groei nodig hebben daadwerkelijk leesbaar zijn voor de
integratiegebruiker, en hoeveel rijen ze opleveren.

Schrijft niets weg en verandert niets in Salesforce — alleen leesacties, de
uitvoer gaat naar de console (in GitHub Actions: naar de job-log).

Gebruik:
    SF_DOMAIN=... SF_CLIENT_ID=... SF_CLIENT_SECRET=... python scripts/probe_lead_consent.py

Wat het rapporteert:
  1. Onder welke gebruiker de integratie draait (de "Run As" van de Connected App)
  2. Of Customized_Product_Advice__c en HasOptedOutOfEmail zichtbaar zijn voor
     die gebruiker — dit is de field-level-securitycheck, zonder dat je in
     Setup hoeft te klikken
  3. Of RecordTypeId meekomt (nodig om per markt te kunnen splitsen)
  4. Hoeveel leads er met consent zijn, uitgesplitst per RecordType
"""

import os
import sys

import requests

DOMAIN = os.environ["SF_DOMAIN"]
CLIENT_ID = os.environ["SF_CLIENT_ID"]
CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
API_VERSION = "v60.0"

CONSENT_FIELD = "Customized_Product_Advice__c"
OPTOUT_FIELD = "HasOptedOutOfEmail"

# RecordTypeId's per markt, uit het stappenplan (vraag 6b).
LEAD_RECORD_TYPES = {
    "0127Q000000upr9QAA": "NL",
    "0127Q000000eERIQA2": "BE",
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


def whoami(token, instance_url):
    """De Run As-gebruiker van de Connected App, zonder in Setup te zoeken."""
    resp = requests.get(
        f"https://{DOMAIN}/services/oauth2/userinfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if not resp.ok:
        return None
    return resp.json()


def describe_lead(token, instance_url):
    """Alle Lead-velden die deze gebruiker mág zien. Een veld dat door
    field-level security is afgeschermd ontbreekt hier gewoon."""
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
    print(f"Verbonden met {instance_url}\n")

    # --- 1. Wie is de integratiegebruiker? --------------------------------
    info = whoami(token, instance_url)
    if info:
        print("Run As-gebruiker")
        print(f"  username : {info.get('preferred_username')}")
        print(f"  naam     : {info.get('name')}")
        print(f"  user id  : {info.get('user_id')}")
    else:
        print("Run As-gebruiker: kon userinfo niet ophalen (niet blokkerend)")
    print()

    # --- 2. Zijn de velden zichtbaar? -------------------------------------
    fields = describe_lead(token, instance_url)
    print(f"Lead-velden zichtbaar voor deze gebruiker: {len(fields)}")

    missing = []
    for name in (CONSENT_FIELD, OPTOUT_FIELD, "RecordTypeId", "CreatedDate"):
        field = fields.get(name)
        if field:
            print(f"  ✅ {name}  ({field['type']})")
        else:
            print(f"  ❌ {name}  — NIET zichtbaar voor dit profiel")
            missing.append(name)

    if missing:
        print()
        print("Field-level security blokkeert:", ", ".join(missing))
        print(
            "Zet die velden op Visible voor het profiel van bovenstaande gebruiker:\n"
            "  Setup -> Object Manager -> Lead -> Fields & Relationships -> <veld>\n"
            "  -> Set Field-Level Security -> Visible aan (Read-Only mag erbij)"
        )
        # Zonder het consent-veld heeft verder testen geen zin.
        if CONSENT_FIELD in missing:
            sys.exit(1)
    print()

    # --- 3. Hoeveel leads met consent, per markt? -------------------------
    where = f"{CONSENT_FIELD} = true AND {OPTOUT_FIELD} = false"

    total, err = query(token, instance_url, f"SELECT COUNT(Id) total FROM Lead WHERE {where}")
    if err:
        print(f"Telling mislukt: {err}")
        sys.exit(1)
    total_count = total["records"][0]["total"]
    print(f"Leads met consent ({where}): {total_count}")

    all_leads, err = query(token, instance_url, "SELECT COUNT(Id) total FROM Lead")
    if not err:
        everyone = all_leads["records"][0]["total"]
        share = f"{total_count / everyone * 100:.1f}%" if everyone else "n.v.t."
        print(f"Van in totaal {everyone} leads  ->  {share} heeft consent")
    print()

    # --- 4. Uitsplitsing per markt ----------------------------------------
    grouped, err = query(
        token,
        instance_url,
        f"SELECT RecordTypeId, COUNT(Id) total FROM Lead WHERE {where} GROUP BY RecordTypeId",
    )
    if err:
        print(f"Groepering per RecordTypeId mislukt: {err}")
        return

    print("Per markt (RecordTypeId):")
    for row in grouped["records"]:
        rtid = row["RecordTypeId"]
        market = LEAD_RECORD_TYPES.get(rtid, "onbekend")
        print(f"  {market:8} {rtid}  {row['total']}")
    print()
    print(
        "Als dit klopt, kan Automation Coverage gevuld worden: dit is de noemer "
        "(contacten met consent). De teller komt uit de SFMC-sends."
    )


if __name__ == "__main__":
    main()
