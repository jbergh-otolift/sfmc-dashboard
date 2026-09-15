#!/usr/bin/env python3
"""
build_crm_metrics.py

Zet de Salesforce LeadHistory-export (`exports/report.csv`) om naar
dashboardmetrics: de her-activatie-funnel, de acquisitie-funnel en de
kern-KPI's die uit statusovergangen te berekenen zijn.

Gebruik:
    python scripts/build_crm_metrics.py

Env vars (optioneel, defaults gelijk aan het venster van de export):
    PERIOD_START / PERIOD_END   huidige periode, YYYY-MM-DD, end exclusief
    PREV_START   / PREV_END     vorige periode voor de period-over-period deltas

Schrijft `exports/crm_metrics.json`.

De export levert sinds kort een `Market`-kolom (afgeleid van
`Lead.RecordTypeId`), dus alles wordt per markt berekend. Rijen zonder markt —
uit een oudere export zonder die kolom — komen terecht onder de sleutel
`"onbekend"` en worden door het dashboard genegeerd.

Velden zonder bron blijven expliciet None (niet 0), zodat het dashboard het
verschil tussen "nul gemeten" en "niet aangesloten" kan tonen.
"""

import csv
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

IN_PATH = "exports/report.csv"
OUT_PATH = "exports/crm_metrics.json"

# De export loopt van 2026-08-01 t/m 2026-09-08. Om een eerlijke
# period-over-period vergelijking te hebben splitsen we dat venster in twee
# gelijke helften van 19 dagen in plaats van een kalendermaand te forceren
# (augustus zou anders vergeleken worden met 8 dagen september).
DEFAULT_PERIOD = ("2026-08-20", "2026-09-09")
DEFAULT_PREV = ("2026-08-01", "2026-08-20")

# Salesforce Lead-statuswaarden zoals ze werkelijk in de export voorkomen.
S_NEW = "New"
S_NOT_REACHED = "Not reached"
S_MAILJOURNEY = "Mailjourney"
S_REENTERED = "Re-entered"
S_PHONE_CHANGED = "Re-entered - Phone Number Changed"
S_APPOINTMENT = "Appointment"
S_FOLLOWUP = "Follow-up"
S_NOT_QUALIFIED = "Not Qualified"
S_LOST = "Lost"
S_BROCHURE = "Brochure"


MARKETS = ["nl", "be", "fr", "it"]


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Oudere exports hebben nog geen Market-kolom.
    if rows and "Market" not in rows[0]:
        print("WAARSCHUWING: report.csv heeft nog geen Market-kolom — "
              "draai scripts/export_salesforce_report.py opnieuw voor cijfers per markt.")
    return rows


def rows_for_market(rows, market):
    """Rijen van één markt. `market=None` geeft alles (alle markten samen)."""
    if market is None:
        return rows
    return [r for r in rows if (r.get("Market") or "").strip().lower() == market]


def in_window(edit_date, start, end):
    """Edit Date is ISO8601 met Z-suffix; stringvergelijking op de datum volstaat."""
    day = edit_date[:10]
    return start <= day < end


def transitions(rows, start, end):
    """Statusovergangen binnen het venster, plus per-lead de eerste en laatste stap."""
    sel = [r for r in rows if in_window(r["Edit Date"], start, end)]
    sel.sort(key=lambda r: r["Edit Date"])

    pairs = Counter()          # (old, new) -> aantal overgangen
    to_status = Counter()      # new -> aantal overgangen
    leads_to = defaultdict(set)  # new -> set van Lead ID's
    leads_from = defaultdict(set)
    for r in sel:
        old = (r["Old Value"] or "").strip()
        new = (r["New Value"] or "").strip()
        pairs[(old, new)] += 1
        to_status[new] += 1
        leads_to[new].add(r["Lead ID"])
        leads_from[old].add(r["Lead ID"])
    return {
        "rows": sel,
        "pairs": pairs,
        "to_status": to_status,
        "leads_to": leads_to,
        "leads_from": leads_from,
        "leads": {r["Lead ID"] for r in sel},
    }


def ratio(num, den):
    """Percentage met 2 decimalen, of None als de noemer 0 is."""
    if not den:
        return None
    return round(num / den * 100, 2)


def delta_pct(cur, prev):
    """Relatieve verandering in procenten, of None als er niets te vergelijken is."""
    if cur is None or prev in (None, 0):
        return None
    return round((cur - prev) / prev * 100, 1)


def compute(rows, start, end):
    """Alle uit LeadHistory afleidbare metrics voor één venster."""
    t = transitions(rows, start, end)
    pairs, to_status = t["pairs"], t["to_status"]

    # --- Her-activatie funnel ---------------------------------------------
    # Instroom = alle leads die in dit venster een statusstap maken. De
    # overgang New -> X telt alleen nieuwe leads; leads die al liepen horen
    # ook in de funnel, dus we nemen alle unieke leads met activiteit.
    intake = len(t["leads"])
    not_reached = to_status[S_NOT_REACHED]
    mailjourney = to_status[S_MAILJOURNEY]
    sql = to_status[S_REENTERED]

    # Aftakking: Not reached -> nummerverrijking -> terug in de hoofdstroom.
    # "Phone number enriched" is een eigen statuswaarde, geen los veld
    # (stappenplan §7, vraag 4).
    phone_changed = to_status[S_PHONE_CHANGED]

    # Van Not reached naar Mailjourney lopen twee routes: direct, of via
    # Follow-up. Beide tellen mee als doorstroom naar de mailjourney.
    nr_to_mj = pairs[(S_NOT_REACHED, S_MAILJOURNEY)] + pairs[(S_FOLLOWUP, S_MAILJOURNEY)]
    mj_to_sql = pairs[(S_MAILJOURNEY, S_REENTERED)]

    reactivation = {
        "leadIntake": {"abs": intake, "share": 100.0},
        "notContact": {
            "abs": not_reached,
            "share": ratio(not_reached, intake),
            "ratio": ratio(not_reached, intake),
        },
        "mailjourney": {
            "abs": mailjourney,
            "share": ratio(mailjourney, intake),
            "ratio": ratio(nr_to_mj, not_reached),
        },
        "sql": {
            "abs": sql,
            "share": ratio(sql, intake),
            "ratio": ratio(mj_to_sql, mailjourney),
        },
        "enrichment": {
            "notReachable": not_reached,
            "enriched": phone_changed,
            "enrichRatio": ratio(phone_changed, not_reached),
            "rejoined": phone_changed,
        },
        "recovered": {
            # Teruggewonnen leads = doorstroom Mailjourney -> Re-entered.
            "count": mj_to_sql,
            # CPL en daarmee teruggewonnen CAC hebben geen bron in dit
            # dataset (stappenplan §7, vraag 5).
            "cpl": None,
            "cac": None,
        },
    }

    # --- Acquisitie funnel ------------------------------------------------
    # MQL  = leads met statusactiviteit in de periode
    # SQL  = doorstroom naar Re-entered (sales-gekwalificeerd)
    # R1   = doorstroom naar Appointment (eerste afspraak / thuisadvies)
    # Order= staat op Opportunity, niet in LeadHistory -> geen bron
    #
    # LET OP: dit is géén strikte keten. Uit de transitiedata blijkt dat
    # Appointment vooral direct uit New (866x) en Not reached (280x) komt, en
    # maar zelden via Re-entered. R1 als percentage van SQL uitdrukken levert
    # dan onzin op (>100%). Elke fase wordt daarom uitgedrukt als aandeel van
    # MQL: een fase-aandeel, niet een opeenvolgende conversie.
    appointment = to_status[S_APPOINTMENT]
    acquisition = {
        "funnel": {
            "mql": {"abs": intake, "share": 100.0, "convDeltaPct": None},
            "sql": {"abs": sql, "share": ratio(sql, intake), "convDeltaPct": None},
            "r1": {"abs": appointment, "share": ratio(appointment, intake), "convDeltaPct": None},
            "order": {"abs": None, "share": None, "convDeltaPct": None},
        },
        # Kanalen en kosten hebben geen bron: geen ad-platform-API en geen
        # kostentabel bepaald (stappenplan §7, vraag 5).
        "channels": [],
        "totals": {"contacts": None, "avgCpa": None, "avgCpql": None},
    }

    # --- Kern KPI's -------------------------------------------------------
    kern = {
        # Coverage vraagt het consent-veld uit het CRM
        # (Customized_Product_Advice__c + HasOptedOutOfEmail); die kolommen
        # zitten niet in deze export.
        "coverage": {
            "value": None,
            "consentTotal": None,
            "toActivate": None,
            "deltaPct": None,
        },
        "databaseGrowth": {"value": None, "deltaPct": None},
        "reactivationRatio": {"value": ratio(mj_to_sql, mailjourney), "deltaPct": None},
        "mqlToSql": {"value": ratio(sql, intake), "deltaPct": None},
        "cpql": {"value": None, "deltaPct": None},
    }

    return {
        "kernKpis": kern,
        "reactivation": reactivation,
        "acquisition": acquisition,
        "_debug": {
            "uniqueLeads": intake,
            "transitionRows": len(t["rows"]),
            "statusCounts": dict(sorted(to_status.items())),
        },
    }


def fill_deltas(cur, prev):
    """Zet de period-over-period deltas op de huidige periode."""
    k, pk = cur["kernKpis"], prev["kernKpis"]
    for key in ("reactivationRatio", "mqlToSql", "coverage", "databaseGrowth", "cpql"):
        k[key]["deltaPct"] = delta_pct(k[key]["value"], pk[key]["value"])

    r, pr = cur["reactivation"], prev["reactivation"]
    for key in ("notContact", "mailjourney", "sql"):
        r[key]["deltaPct"] = delta_pct(r[key]["ratio"], pr[key]["ratio"])

    a, pa = cur["acquisition"]["funnel"], prev["acquisition"]["funnel"]
    for key in ("sql", "r1", "order"):
        a[key]["convDeltaPct"] = delta_pct(a[key]["share"], pa[key]["share"])


def main():
    period = (
        os.environ.get("PERIOD_START", DEFAULT_PERIOD[0]),
        os.environ.get("PERIOD_END", DEFAULT_PERIOD[1]),
    )
    prev = (
        os.environ.get("PREV_START", DEFAULT_PREV[0]),
        os.environ.get("PREV_END", DEFAULT_PREV[1]),
    )

    rows = read_rows(IN_PATH)
    print(f"Gelezen: {len(rows)} statusovergangen uit {IN_PATH}")

    cur = compute(rows, *period)
    pre = compute(rows, *prev)
    fill_deltas(cur, pre)

    # Per markt hetzelfde rekenwerk, plus "all" als totaal over alle markten.
    per_market = {}
    for market in MARKETS:
        subset = rows_for_market(rows, market)
        m_cur = compute(subset, *period)
        m_pre = compute(subset, *prev)
        fill_deltas(m_cur, m_pre)
        per_market[market] = {
            "rows": len(subset),
            "current": m_cur,
            "previous": m_pre,
        }

    has_market_column = bool(rows) and "Market" in rows[0]
    unknown = len([r for r in rows if not (r.get("Market") or "").strip()])

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": {"start": period[0], "end": period[1]},
        "prev_period": {"start": prev[0], "end": prev[1]},
        "source_rows": len(rows),
        "rows_without_market": unknown,
        "market_scope": "per_market" if has_market_column else "all",
        "current": cur,
        "previous": pre,
        "markets": per_market,
        "notes": [
            "Order-stap en alle kosten (CPA/CPQL/CPL) hebben geen bron in deze export.",
            "Coverage-noemer en database-groei komen uit exports/lead_consent.json.",
        ],
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print(f"Periode {period[0]} t/m {period[1]}  (scope: {out['market_scope']})")
    print(f"  {'markt':8} {'rijen':>7} {'instroom':>9} {'notreach':>9} {'mailjrn':>8} {'SQL':>6} {'ratio':>7}")
    for market in MARKETS + [None]:
        block = cur if market is None else per_market[market]["current"]
        label = "TOTAAL" if market is None else market
        rowcount = len(rows) if market is None else per_market[market]["rows"]
        r = block["reactivation"]
        ratio_val = block["kernKpis"]["reactivationRatio"]["value"]
        print(
            f"  {label:8} {rowcount:7} {r['leadIntake']['abs']:9} "
            f"{r['notContact']['abs']:9} {r['mailjourney']['abs']:8} "
            f"{r['sql']['abs']:6} {str(ratio_val):>7}"
        )
    if unknown:
        print(f"  {unknown} rijen zonder markt (oudere export)")
    print(f"Geschreven naar {OUT_PATH}")


if __name__ == "__main__":
    main()
