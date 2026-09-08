#!/usr/bin/env python3
"""
build_data_json.py

Voegt de twee exports samen tot het bestand dat het dashboard ophaalt:

    exports/sfmc_tracking.json   (Marketing Cloud, per Business Unit/markt)
    exports/crm_metrics.json     (Salesforce LeadHistory, markt-overstijgend)
        -> docs/data.json

Gebruik:
    python scripts/build_data_json.py

Contract van docs/data.json:

    {
      "generated_at": ..., "period": {...}, "prev_period": {...},
      "markets": {
        "nl": { "marketLabel", "available", "_sources",
                "kernKpis", "reactivation", "acquisition", "emailHealth" },
        "be": {...}, "fr": {...}, "it": {...}
      }
    }

`_sources` zegt per sectie of de data live is ("live"), deels ("partial") of
nog niet aangesloten ("not_connected"). Het dashboard toont dat als badge, zodat
een leeg veld nooit voor een gemeten nul wordt aangezien.
"""

import json
import os
import re
from datetime import datetime, timezone

TRACKING_PATH = "exports/sfmc_tracking.json"
CRM_PATH = "exports/crm_metrics.json"
OUT_PATH = "docs/data.json"

MARKETS = {
    "nl": "Nederland",
    "be": "België",
    "fr": "Frankrijk",
    "it": "Italië",
}

# Metrics waarvoor het dashboard een period-over-period delta toont.
HEALTH_METRICS = ["delivery", "ctor", "ctr", "open", "unsub", "bounce", "spam", "sent"]

# Minimum aantal sends voordat een flow een eigen filterchip krijgt. Onder deze
# drempel zijn de percentages ruis en zou het dashboard 56 chips tonen voor NL.
MIN_SENT_FOR_CHIP = int(os.environ.get("MIN_SENT_FOR_CHIP", "50"))


# Triggered-send-definities die Journey Builder aanmaakt krijgen een hex-suffix
# achter de naam ("NL - Nurturemail 2 - 86a9f2d110e7407780c49a3f7c28ed12").
# Dat is ruis in een filterchip.
GUID_SUFFIX = re.compile(r"\s*[-–]\s*[0-9a-fA-F]{8,32}\s*$")


def clean_label(label):
    if not label:
        return label
    cleaned = GUID_SUFFIX.sub("", label).strip(" -–")
    return cleaned or label


def load(path):
    if not os.path.exists(path):
        print(f"WAARSCHUWING: {path} ontbreekt — die sectie blijft leeg.")
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def rel_delta(cur, prev):
    """Relatieve verandering in procenten, of None als vergelijken niet kan."""
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / prev * 100, 1)


def build_email_health(market_block):
    """Zet de flows uit de tracking-export om naar het emailHealth-contract,
    met per metric een delta t.o.v. de vorige periode."""
    if not market_block:
        return {"all": {"label": "Alle flows"}}

    flows = market_block.get("flows") or {}
    prev = market_block.get("prev_flows") or {}
    out = {}

    for key, flow in flows.items():
        # Flows onder de drempel krijgen geen eigen filterchip. Bij een handvol
        # sends zijn de ratio's ruis: events kunnen binnen het venster vallen
        # terwijl de bijbehorende send ervoor lag, waardoor je open rates boven
        # 100% ziet. Ze blijven wel meetellen in "all".
        if key != "all" and (flow.get("sent") or 0) < MIN_SENT_FOR_CHIP:
            continue

        entry = {"label": clean_label(flow.get("label")) or key}
        for metric in HEALTH_METRICS:
            entry[metric] = flow.get(metric)
        # Volumes meenemen zodat het cijfer navolgbaar is in de devtools.
        for extra in ("delivered", "opens", "clicks", "bounces", "soft_bounces", "unsubs"):
            entry[extra] = flow.get(extra)

        prev_flow = prev.get(key) or {}
        entry["deltas"] = {
            metric: rel_delta(flow.get(metric), prev_flow.get(metric))
            for metric in HEALTH_METRICS
        }
        out[key] = entry

    return out or {"all": {"label": "Alle flows"}}


def source_state(email_health, has_crm):
    """Bepaalt de badge per sectie."""
    live_email = bool(
        email_health
        and any(f.get("sent") for f in email_health.values() if isinstance(f, dict))
    )
    return {
        "emailHealth": "live" if live_email else "not_connected",
        # Her-activatie komt volledig uit LeadHistory.
        "reactivation": "live" if has_crm else "not_connected",
        # Kern KPI's: heractivatie-ratio en MQL->SQL zijn live, coverage /
        # database-groei / CPQL nog niet.
        "kernKpis": "partial" if has_crm else "not_connected",
        # Acquisitie: funnel live, kanalen en kosten niet.
        "acquisition": "partial" if has_crm else "not_connected",
    }


def main():
    tracking = load(TRACKING_PATH)
    crm = load(CRM_PATH)

    crm_current = (crm or {}).get("current") or {}
    has_crm = bool(crm_current)

    # De periode van de tracking-export is leidend; die van het CRM hoort
    # hetzelfde te zijn (beide worden door de workflow gelijk gezet).
    period = (tracking or {}).get("period") or (crm or {}).get("period")
    prev_period = (tracking or {}).get("prev_period") or (crm or {}).get("prev_period")

    if tracking and crm and tracking.get("period") != crm.get("period"):
        print(
            "WAARSCHUWING: tracking- en CRM-export dekken niet dezelfde periode "
            f"({tracking.get('period')} vs {crm.get('period')})."
        )

    tracking_markets = (tracking or {}).get("markets") or {}
    markets = {}

    for key, label in MARKETS.items():
        block = tracking_markets.get(key)
        email_health = build_email_health(block)
        available = bool(block) or has_crm

        markets[key] = {
            "marketLabel": label,
            "available": available,
            "_sources": source_state(email_health, has_crm),
            # CRM-cijfers zijn markt-overstijgend zolang de export geen
            # RecordTypeId meelevert; ze staan daarom identiek onder elke markt
            # die bestaat. Het dashboard vermeldt dat expliciet.
            "kernKpis": crm_current.get("kernKpis", {}),
            "reactivation": crm_current.get("reactivation", {}),
            "acquisition": crm_current.get("acquisition", {}),
            "emailHealth": email_health,
        }

    # FR en IT bestaan nog niet: geen MID in Marketing Cloud en geen
    # RecordType in het CRM. Expliciet op niet-beschikbaar zetten.
    for key in ("fr", "it"):
        if key not in tracking_markets:
            markets[key]["available"] = False
            markets[key]["_sources"] = {
                k: "not_connected" for k in markets[key]["_sources"]
            }

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": period,
        "prev_period": prev_period,
        "crm_market_scope": (crm or {}).get("market_scope", "all"),
        "notes": (crm or {}).get("notes", []),
        "markets": markets,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print(f"Geschreven naar {OUT_PATH}")
    print(f"  periode: {period}  vorige: {prev_period}")
    for key, market in markets.items():
        flows = market["emailHealth"]
        sent = (flows.get("all") or {}).get("sent")
        print(
            f"  {key}: available={market['available']} "
            f"flows={len(flows)} sent(all)={sent} "
            f"bronnen={market['_sources']}"
        )


if __name__ == "__main__":
    main()
