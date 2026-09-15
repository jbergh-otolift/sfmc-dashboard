import csv
import io
import os
import time
from datetime import datetime, timedelta, timezone

import requests

DOMAIN = os.environ["SF_DOMAIN"]
CLIENT_ID = os.environ["SF_CLIENT_ID"]
CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
API_VERSION = "v60.0"

# Hoe ver terug we statushistorie ophalen. Het dashboard laat de lezer kiezen
# tussen 7, 30 en 90 dagen, en vergelijkt elke periode met de even lange
# periode ervoor — voor 90 dagen is dus 180 dagen historie nodig. LeadHistory
# kent geen rijlimiet via de Bulk API, dus dit bepaalt alleen hoe ver we kijken.
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "200"))
HISTORY_START_DATE = (
    datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
).strftime("%Y-%m-%dT00:00:00Z")

# Lead.RecordTypeId bepaalt de markt van een lead; zonder die kolom zijn de
# funnelcijfers niet per land te splitsen. De relatie-traversal naar de
# bovenliggende Lead werkt in Bulk API 2.0 (geverifieerd).
SOQL_QUERY = f"""
SELECT CreatedDate, LeadId, Lead.RecordTypeId, Field, OldValue, NewValue
FROM LeadHistory
WHERE Field = 'Status' AND CreatedDate >= {HISTORY_START_DATE}
ORDER BY CreatedDate DESC
""".strip()

# RecordTypeId -> markt. Alle vier zijn "Particulier <land>"; er zijn geen
# zakelijke Lead-RecordTypes.
MARKET_BY_RECORD_TYPE = {
    "0127Q000000upr9QAA": "NL",
    "0127Q000000eERIQA2": "BE",
    "012QD000002ylXZYAY": "FR",
    "012QD000002ylcPYAQ": "IT",
}

OUT_COLUMNS = ["Edit Date", "Lead ID", "Market", "Record Type ID", "Field / Event", "Old Value", "New Value"]
OUT_PATH = "exports/report.csv"


def get_access_token():
    resp = requests.post(
        f"https://{DOMAIN}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data["instance_url"]


def create_query_job(access_token, instance_url):
    resp = requests.post(
        f"{instance_url}/services/data/{API_VERSION}/jobs/query",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"operation": "query", "query": SOQL_QUERY},
    )
    resp.raise_for_status()
    return resp.json()["id"]


def wait_for_job(access_token, instance_url, job_id):
    url = f"{instance_url}/services/data/{API_VERSION}/jobs/query/{job_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    while True:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        job = resp.json()
        state = job["state"]
        if state == "JobComplete":
            return job
        if state in ("Failed", "Aborted"):
            raise RuntimeError(f"Bulk query job {job_id} ended in state {state}: {job}")
        time.sleep(5)


def fetch_all_rows(access_token, instance_url, job_id):
    url = f"{instance_url}/services/data/{API_VERSION}/jobs/query/{job_id}/results"
    headers = {"Authorization": f"Bearer {access_token}"}
    rows = []
    header = None
    locator = None

    while True:
        params = {"maxRecords": 50000}
        if locator:
            params["locator"] = locator
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()

        reader = csv.reader(io.StringIO(resp.text))
        chunk = list(reader)
        if not chunk:
            break
        if header is None:
            header = chunk[0]
        rows.extend(chunk[1:])

        locator = resp.headers.get("Sforce-Locator")
        if not locator or locator == "null":
            break

    return header, rows


def write_csv(header, rows, out_path):
    idx = {name: header.index(name) for name in ("CreatedDate", "LeadId", "Field", "OldValue", "NewValue")}

    # Bulk API levert de relatiekolom als "Lead.RecordTypeId". Ontbreekt die
    # (oudere export, of geen leesrecht), dan blijft de marktkolom leeg in
    # plaats van dat de export omvalt.
    rt_idx = next((i for i, name in enumerate(header) if name.replace("_", ".") == "Lead.RecordTypeId"), None)
    if rt_idx is None:
        print("WAARSCHUWING: Lead.RecordTypeId ontbreekt in de response; marktkolom blijft leeg.")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    unmapped = set()
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(OUT_COLUMNS)
        for row in rows:
            rtid = row[rt_idx] if rt_idx is not None else ""
            market = MARKET_BY_RECORD_TYPE.get(rtid, "")
            if rtid and not market:
                unmapped.add(rtid)
            writer.writerow([
                row[idx["CreatedDate"]], row[idx["LeadId"]], market, rtid,
                row[idx["Field"]], row[idx["OldValue"]], row[idx["NewValue"]],
            ])

    if unmapped:
        print(f"WAARSCHUWING: onbekende RecordTypeId's (niet in MARKET_BY_RECORD_TYPE): {sorted(unmapped)}")


def main():
    access_token, instance_url = get_access_token()
    job_id = create_query_job(access_token, instance_url)
    wait_for_job(access_token, instance_url, job_id)
    header, rows = fetch_all_rows(access_token, instance_url, job_id)
    write_csv(header, rows, OUT_PATH)
    print(f"Exported {len(rows)} rows to {OUT_PATH}")


if __name__ == "__main__":
    main()
