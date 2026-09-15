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
CONSENT_PATH = "exports/lead_consent.json"
OUTCOMES_PATH = "exports/flow_outcomes.json"
GOALS_PATH = "config/flow_goals.json"

# Welke metric hoort bij welk doeltype, en hoe die heet op het dashboard.
GOAL_METRICS = {
    "sql": ("toSql", "MQL → SQL"),
    "appointment": ("toAppointment", "Afspraken"),
    "won": ("won", "Gewonnen orders"),
    "revenue": ("revenue", "Omzet"),
}
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


def build_email_health(market_block, outcomes=None, goals=None, goal_defaults=None):
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
        for extra in ("delivered", "opens", "clicks", "bounces", "soft_bounces", "unsubs", "emails"):
            entry[extra] = flow.get(extra)

        # Opbrengst per journey: afspraak, conversie en omzet van de leads die
        # deze flow geraakt heeft.
        outcome = (outcomes or {}).get(entry["label"]) or (outcomes or {}).get(key)
        if outcome:
            # Omzet in euro's blijft uit het dashboard: de toerekening is niet
            # causaal genoeg om een bedrag aan een flow te hangen.
            outcome = {k: v for k, v in outcome.items()
                       if k not in ("revenue", "revenuePerTouch")}
        entry["outcome"] = outcome
        entry["goal"] = build_goal(
            entry["outcome"], (goals or {}).get(entry["label"]), goal_defaults)

        prev_flow = prev.get(key) or {}
        entry["deltas"] = {
            metric: rel_delta(flow.get(metric), prev_flow.get(metric))
            for metric in HEALTH_METRICS
        }
        out[key] = entry

    return out or {"all": {"label": "Alle flows"}}


def build_goal(outcome, goal_config, defaults):
    """Rekent een flow af op zijn eigen doel.

    Een nurture-lane hoort leads te kwalificeren, een geen-gehoor-flow hoort
    afspraken op te leveren. Afrekenen op een gedeelde omzetkolom doet beide
    tekort, dus elke flow krijgt de metric die bij zijn opdracht past.
    """
    if not outcome:
        return None

    config = dict(defaults or {})
    config.update(goal_config or {})

    flow_type = config.get("type", "marketing")
    # Transactionele flows (bevestigingen, herinneringen, orderbevestigingen)
    # horen te werken, niet te presteren. Die krijgen geen doelstelling.
    if flow_type == "transactional":
        return {
            "type": "transactional",
            "note": config.get("note"),
            "configured": bool(goal_config),
        }

    goal = config.get("goal")
    if goal not in GOAL_METRICS:
        return None

    field, label = GOAL_METRICS[goal]
    actual = outcome.get(field)
    touched = outcome.get("touched") or 0

    # Doel mag absoluut zijn of als percentage van de geraakte leads.
    target = config.get("target")
    target_rate = config.get("targetRate")
    if target is None and target_rate is not None and touched:
        target = round(touched * target_rate / 100, 1)

    return {
        "type": "marketing",
        "goal": goal,
        "label": label,
        "actual": actual,
        "target": target,
        "targetRate": target_rate,
        "rate": round(actual / touched * 100, 1) if touched and actual is not None else None,
        "attainment": (
            round(actual / target * 100, 0) if target and actual is not None else None
        ),
        "isRevenue": goal == "revenue",
        "note": config.get("note"),
        # Geen configuratie voor deze flow: het dashboard toont dan het
        # standaarddoel, maar moet duidelijk maken dat dat niet is afgesproken.
        "configured": bool(goal_config),
    }


def build_coverage(consent_market, market_block):
    """Automation Coverage per markt.

    Noemer = de leads die we hóren te mailen: status Mailjourney met consent.
    Teller = daarvan degenen die in de periode echt een e-mail kregen. Beide
    komen uit export_lead_consent.py, dat de exacte doorsnede van de twee
    id-verzamelingen maakt — een ruwe deling zou fout zijn, want een deel van
    de bereikte leads staat op een andere status en hoort niet in de noemer.

    Zonder Business Unit in Marketing Cloud is de teller er niet; coverage
    blijft dan None ("nog niet meetbaar"), uitdrukkelijk geen 0%.
    """
    if not consent_market:
        return {"value": None, "shouldMail": None, "reached": None,
                "consentTotal": None, "toActivate": None, "deltaPct": None,
                "measurable": False, "reason": "no_consent_data"}

    should_mail = consent_market.get("shouldMail")
    reached = consent_market.get("reachedOfShouldMail")
    value = consent_market.get("coverage")

    if value is None or not should_mail:
        if not should_mail:
            reason = "nobody_to_mail"
        elif not market_block:
            reason = "no_business_unit"
        else:
            reason = "numerator_missing"
        return {
            "value": None,
            "shouldMail": should_mail,
            "reached": reached,
            "consentTotal": consent_market.get("consentTotal"),
            "toActivate": None,
            "deltaPct": None,
            "measurable": False,
            "reason": reason,
        }

    return {
        "value": value,
        "shouldMail": should_mail,
        "reached": reached,
        "consentTotal": consent_market.get("consentTotal"),
        "toActivate": max(should_mail - reached, 0),
        "deltaPct": None,
        "measurable": True,
        "reason": None,
    }


def source_state(email_health, has_crm, has_outcomes=False):
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
        "outcomes": "live" if has_outcomes else "not_connected",
    }


def main():
    tracking = load(TRACKING_PATH)
    crm = load(CRM_PATH)
    consent = load(CONSENT_PATH)
    outcomes = load(OUTCOMES_PATH)
    outcome_markets = (outcomes or {}).get("markets") or {}
    goals = load(GOALS_PATH) or {}
    goal_defaults = goals.get("defaults") or {}

    crm_current = (crm or {}).get("current") or {}
    crm_markets = (crm or {}).get("markets") or {}
    consent_markets = (consent or {}).get("markets") or {}
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
        market_outcomes = outcome_markets.get(key) or {}
        email_health = build_email_health(
            block, market_outcomes, goals.get(key) or {}, goal_defaults)

        # CRM-cijfers zijn nu echt per markt (report.csv heeft een Market-kolom).
        # Ontbreekt die markt, val dan terug op het totaal.
        market_crm = (crm_markets.get(key) or {}).get("current") or {}
        if not market_crm:
            market_crm = crm_current

        kern = dict(market_crm.get("kernKpis") or {})
        consent_market = consent_markets.get(key)
        kern["coverage"] = build_coverage(consent_market, block)
        growth = (consent_market or {}).get("growth")
        growth_prev = (consent_market or {}).get("growthPrev")
        kern["databaseGrowth"] = {
            "value": growth,
            # Het verschil met de vorige periode, niet het aantal zelf — anders
            # staat er "940 ▼ -940".
            "deltaAbs": (
                growth - growth_prev
                if growth is not None and growth_prev is not None
                else None
            ),
            "deltaPct": (consent_market or {}).get("growthDeltaPct"),
        }

        available = bool(block) or bool(market_crm) or bool(consent_market)

        markets[key] = {
            "marketLabel": label,
            "available": available,
            "_sources": source_state(email_health, bool(market_crm), bool(market_outcomes)),
            "kernKpis": kern,
            "reactivation": market_crm.get("reactivation", {}),
            "acquisition": market_crm.get("acquisition", {}),
            "emailHealth": email_health,
            "consent": consent_market,
            # Ontdubbeld markttotaal van de opbrengst.
            "outcomeTotal": (
                {k: v for k, v in (market_outcomes.get("all") or {}).items()
                 if k not in ("revenue", "revenuePerTouch")}
                if market_outcomes.get("all") else None
            ),
        }

    # FR en IT hebben wél CRM-data (Particulier FR/IT bestaan als RecordType),
    # maar nog geen Business Unit in Marketing Cloud. Email Health blijft daar
    # dus leeg, de rest niet.
    for key in ("fr", "it"):
        if key not in tracking_markets:
            markets[key]["_sources"]["emailHealth"] = "not_connected"

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": period,
        "prev_period": prev_period,
        "crm_market_scope": (crm or {}).get("market_scope", "all"),
        "consent_definition": (consent or {}).get("consent_definition"),
        "coverage_days": (consent or {}).get("coverage_days"),
        "outcome_lookback_days": (outcomes or {}).get("lookback_days"),
        "outcome_attribution": (outcomes or {}).get("attribution"),
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
