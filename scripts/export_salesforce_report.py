import os
import csv

import requests

DOMAIN = os.environ["SF_DOMAIN"]
CLIENT_ID = os.environ["SF_CLIENT_ID"]
CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
REPORT_ID = os.environ["SF_REPORT_ID"]
API_VERSION = "v60.0"


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


def fetch_report(access_token, instance_url):
    resp = requests.get(
        f"{instance_url}/services/data/{API_VERSION}/analytics/reports/{REPORT_ID}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"includeDetails": "true"},
    )
    resp.raise_for_status()
    return resp.json()


def write_csv(report_data, out_path):
    columns = report_data["reportMetadata"]["detailColumns"]
    column_info = report_data["reportExtendedMetadata"]["detailColumnInfo"]
    labels = [column_info[c]["label"] for c in columns]
    rows = report_data["factMap"]["T!T"]["rows"]

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(labels)
        for row in rows:
            writer.writerow([cell["label"] for cell in row["dataCells"]])

    return len(rows)


def main():
    access_token, instance_url = get_access_token()
    report_data = fetch_report(access_token, instance_url)
    row_count = write_csv(report_data, "exports/report.csv")
    print(f"Exported {row_count} rows to exports/report.csv")
    if row_count >= 2000:
        print(
            "Warning: row count is at or near the synchronous Reports API limit "
            "(~2000 rows). Some rows may be missing; switch to the async "
            "report instances API if this report is larger than that."
        )


if __name__ == "__main__":
    main()
