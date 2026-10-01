#!/usr/bin/env python3
"""
export_reactivation_value.py

Hoeveel omzet komt er binnen via marketing automation, afgelezen aan het
statuspad van de lead?

Dit is een wezenlijk andere toerekening dan "deze lead kreeg een mail en
converteerde ook". Daar kan de order net zo goed vóór de mail zijn gesloten.
Hier telt alleen mee wat aantoonbaar door de mailflow is heropgeleefd, in deze
volgorde:

    Mailjourney  ->  een heractivatiesignaal  ->  (eventueel Appointment)  ->  Order

Het heractivatiesignaal is niet één status: een lead kan op verschillende
manieren terugkomen. Elke route wordt apart geteld, zodat zichtbaar is welke
het meeste oplevert:

  - Re-entered                          lead meldt zich weer / wordt bereikt
  - Re-entered - Phone Number Changed   pas bereikbaar na nummerverrijking
  - Appointment rechtstreeks            vanuit de mailflow direct een afspraak

Een afspraak is niet verplicht: sommige leads gaan van heractivatie naar order
zonder dat er een Appointment-status is vastgelegd. Of die stap gezet is,
wordt wel apart bijgehouden.

De omzet wordt toegerekend aan de **conversiedatum**, zodat de dagcijfers
optelbaar zijn en elk gekozen bereik exact klopt. Dit zegt dus: van de orders
die in deze periode binnenkwamen, kwam dit deel via automation — niet: van de
leads die instroomden leverde dit deel op.

Naast de routes wordt per conversie het volledige **statuspad** opgebouwd: een
leesbare tekenreeks als `Mailjourney (No contact possible) → Re-entered →
Appointment`. Daarmee is op het dashboard te zien welke volgorde van stappen
daadwerkelijk omzet oplevert, in plaats van alleen het eerste signaal.

LET OP bij `byFlow`: een lead kan door meerdere journeys zijn aangeraakt en
telt dan bij élke journey mee. De omzet in `byFlow` is dus **meervoudig
toegerekend** en mag niet worden opgeteld of vergeleken met het totaal. Dat
staat ook als `byFlowNote` in de output. `byRoute`, `byReason` en `byPath`
zijn wél exclusief: daar telt elke order precies één keer.

Gebruik:
    SF_DOMAIN=... SF_CLIENT_ID=... SF_CLIENT_SECRET=... python scripts/export_reactivation_value.py

Draai dit ná export_salesforce_report.py en export_sfmc_tracking.py (die laatste
schrijft `exports/_reached_lead_ids.json`, nodig voor `byFlow`).
Schrijft `exports/reactivation_value.json`.
"""

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

import requests

DOMAIN = os.environ["SF_DOMAIN"]
CLIENT_ID = os.environ["SF_CLIENT_ID"]
CLIENT_SECRET = os.environ["SF_CLIENT_SECRET"]
API_VERSION = "v60.0"

REPORT_PATH = "exports/report.csv"
GOALS_PATH = "config/flow_goals.json"
REACHED_PATH = "exports/_reached_lead_ids.json"
OUT_PATH = "exports/reactivation_value.json"

# Ruim terugkijken: een lead die maanden geleden instroomde kan nu pas tekenen.
LOOKBACK_DAYS = int(os.environ.get("VALUE_LOOKBACK_DAYS", "400"))

MARKET_BY_RECORD_TYPE = {
    "0127Q000000upr9QAA": "nl",
    "0127Q000000eERIQA2": "be",
    "012QD000002ylXZYAY": "fr",
    "012QD000002ylcPYAQ": "it",
}

S_MAILJOURNEY = "Mailjourney"
S_APPOINTMENT = "Appointment"
S_PHONE_CHANGED = "Re-entered - Phone Number Changed"

# De routes waarlangs een lead vanuit de mailflow terugkomt. De volgorde
# bepaalt welke route wint als een lead er meerdere raakt: de eerste telt.
REENTRY_ROUTES = {
    "Re-entered": "Re-entered",
    "Re-entered - Phone Number Changed": "Nummer gewijzigd",
    S_APPOINTMENT: "Direct afspraak",
}

# Alleen deze statussen komen ná de Mailjourney-instroom in het statuspad.
# Al het overige (Lost, Not Qualified, New, Converted Old SF, ...) wordt
# genegeerd; anders wordt het pad onleesbaar en valt er niets meer te groeperen.
PATH_STATUSES = {
    "Re-entered",
    "Re-entered - Phone Number Changed",
    S_APPOINTMENT,
    "Follow-up",
    "Brochure",
    "Not reached",
}

# De twee vaste statuspaden die als trechter op het dashboard komen. Elke
# trechter is een strikte volgorde: een lead moet de stappen in deze volgorde
# zetten, anders telt hij vanaf de eerste misser niet verder mee.
#
#   notReached   New -> Not reached -> Re-entered -> Appointment -> Order
#   mailjourney  New -> Mailjourney -> Re-entered -> Appointment -> Order
#
# De tweede status bepaalt welke trechter het is; de rest is voor beide gelijk.
PATH_FUNNELS = {
    "notReached": "Not reached",
    "mailjourney": S_MAILJOURNEY,
}

# Stap 3 kent twee smaken: een lead die zich gewoon weer meldt en een lead die
# pas bereikbaar werd na nummerverrijking. Allebei tellen als heractivatie.
FUNNEL_REENTRY = ("Re-entered", "Re-entered - Phone Number Changed")

# De twee statussen waarmee een lead in de mailflow terechtkomt.
FUNNEL_ENTRY = tuple(PATH_FUNNELS.values())

# De laatste stap vóór de afspraak bepaalt de route. Dezelfde indeling als bij
# de orders, zodat het afsprakenblok en het ordersblok dezelfde taal spreken.
APPOINTMENT_ROUTES = {
    S_PHONE_CHANGED: "Nummer gewijzigd",
    "Re-entered": "Re-entered",
    S_MAILJOURNEY: "Direct afspraak",
    "Not reached": "Direct afspraak",
}

FUNNEL_STAGES = ("new", "stage2", "reentered", "appointment", "order")

# Dezelfde twee stappen, maar alleen voor de leads die pas terugkwamen nadat
# hun telefoonnummer verrijkt was. Die staan op het dashboard als aftakking
# onder hun eigen route, en worden van de hoofdlijn afgetrokken: zo telt elke
# afspraak één keer en zie je meteen wat nummerverrijking oplevert.
FUNNEL_ENRICHED = ("reenteredEnriched", "appointmentEnriched")

# Hoeveel stappen ná de Mailjourney-instroom maximaal getoond worden.
PATH_MAX_STEPS = 4
PATH_ARROW = " → "
PATH_MORE = "…"


def build_status_path(reason, steps, until=None):
    """Bouwt de leesbare statuspad-string voor één lead.

    `steps` is een op datum gesorteerde lijst van (dag, status) ná de
    instroom in Mailjourney. `until` is de conversiedatum: stappen daarná
    horen niet in het pad, want die zeggen niets over de order.

    Directe herhalingen worden samengevouwen (A → A wordt A) en het pad
    wordt afgekapt op PATH_MAX_STEPS stappen, met een afsluitend '…'.
    """
    head = f"{S_MAILJOURNEY} ({reason})" if reason else S_MAILJOURNEY

    kept = []
    for day, status in steps:
        if until and day > until:
            continue
        if kept and kept[-1] == status:
            continue
        kept.append(status)

    truncated = len(kept) > PATH_MAX_STEPS
    parts = [head] + kept[:PATH_MAX_STEPS]
    path = PATH_ARROW.join(parts)
    if truncated:
        path += PATH_ARROW + PATH_MORE
    return path


# De doelgroep van de toerekening is niet "alle marketingflows", maar de
# leads die daadwerkelijk door automation bewerkt worden:
#
#   1. leads met een ingevulde Reason - Mailjourney (geparkeerd in de mailflow)
#   2. leads uit de offerteflow (openstaande offerte)
#   3. leads uit de nurture lane
#
# Bevestigingsmails en testjourneys vallen daarbuiten: die raken iedereen die
# binnenkomt en zouden vrijwel alle omzet opeisen.
FLOW_PATTERNS = ("offerte", "nurturelane", "nurture lane")

# Flows die mikken op leads met een al bestaande Opportunity. Daar is de
# leadconversie niet de uitkomst — die is al gebeurd — maar het winnen van de
# offerte. De volgorde wordt voor deze flows dus op de sluitdatum getoetst.
OPPORTUNITY_FLOW_PATTERNS = ("offerte",)


def targets_open_opportunity(name):
    lowered = (name or "").lower()
    return any(p in lowered for p in OPPORTUNITY_FLOW_PATTERNS)


def active_reasons(path):
    """Instroomredenen waar een actieve mailflow op zit.

    De status Mailjourney zegt alleen dat een lead geparkeerd staat, niet dat
    er een journey op aangesloten is. Zonder dit onderscheid krijgen redenen
    zonder flow toch orders toegerekend.
    """
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    reasons = config.get("mailjourneyReasons") or {}
    return {reason for reason, active in reasons.items() if active}


def is_scoped_flow(name):
    """Hoort deze journey bij de offerteflow of de nurture lane?"""
    lowered = (name or "").lower()
    if "test" in lowered:
        return False
    return any(pattern in lowered for pattern in FLOW_PATTERNS)


def first_send_dates(path):
    """Per lead de eerste verzenddatum, en per journey hetzelfde.

    Nodig voor de bredere toerekening: een order telt alleen mee als de mail
    ervóór lag. Zonder die volgorde is het geen toerekening maar toeval.
    """
    if not os.path.exists(path):
        return {}, {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    overall = {}
    per_flow = {}
    for market, block in (data.get("markets") or {}).items():
        if not isinstance(block, dict):
            continue
        for lead, day in (block.get("first_send") or {}).items():
            if lead not in overall or day < overall[lead]:
                overall[lead] = day
        for flow, per_key in (block.get("first_send_by_journey") or {}).items():
            target = per_flow.setdefault(flow, {})
            for lead, day in per_key.items():
                if lead not in target or day < target[lead]:
                    target[lead] = day
    return overall, per_flow


def reached_by_flow(path):
    """Lead-ID -> journeys die die lead gemaild hebben.

    Eén lead kan in meerdere journeys zitten; die worden allemaal bewaard.
    Ontbreekt het bestand, dan geven we None terug en slaan we byFlow over.
    """
    if not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"WAARSCHUWING: {path} onleesbaar ({exc}).")
        return None

    by_lead = defaultdict(set)
    for per_market in (data.get("markets") or {}).values():
        for journey, lead_ids in (per_market.get("by_journey") or {}).items():
            for lead in lead_ids or ():
                by_lead[lead].add(journey)
    return by_lead


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


def query_all(session, instance_url, soql):
    url = f"{instance_url}/services/data/{API_VERSION}/query"
    params = {"q": soql}
    records = []
    while True:
        resp = session.get(url, params=params, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data["records"])
        if data.get("done", True):
            break
        url = instance_url + data["nextRecordsUrl"]
        params = None
    return records


def conversions(session, instance_url):
    """Gewonnen orders met hun bronlead, op sluitdatum.

    We filteren en bucketen op `Opportunity.CloseDate`, niet op
    `Lead.ConvertedDate`. Twee redenen:

      - De sluitdatum is het moment dat de order er werkelijk was. Tussen
        conversie en sluiten zit mediaan 7 dagen en bij 10% meer dan drie
        weken, dus met de conversiedatum staat een order al snel in de
        verkeerde dag of week.
      - Filteren op conversiedatum mist orders van leads die langer geleden
        converteerden maar nu pas tekenen.

    Voor de toerekening aan een flow maakt het niets uit: de sluitdatum ligt
    altijd ná de conversiedatum, dus als de mail vóór het een lag, lag hij
    ook vóór het ander.
    """
    # RecordTypeId meenemen, zodat ook orders van leads zónder mailflow-historie
    # aan een markt te koppelen zijn. Anders zou de noemer alleen uit
    # mailflow-leads bestaan en zou elk percentage 100% worden.
    soql = (
        "SELECT Id, RecordTypeId, ConvertedDate, ConvertedOpportunityId, "
        "ConvertedOpportunity.Amount, "
        "ConvertedOpportunity.IsWon, ConvertedOpportunity.CloseDate "
        "FROM Lead "
        "WHERE IsConverted = true AND ConvertedOpportunity.IsWon = true "
        f"AND ConvertedOpportunity.CloseDate = LAST_N_DAYS:{LOOKBACK_DAYS}"
    )
    out = {}
    for row in query_all(session, instance_url, soql):
        opp = row.get("ConvertedOpportunity") or {}
        out[row["Id"]] = {
            # Sluitdatum is de orderdatum; conversiedatum bewaren we alleen
            # om te controleren dat de mail ervoor lag.
            "date": (opp.get("CloseDate") or "")[:10],
            "convertedDate": (row.get("ConvertedDate") or "")[:10],
            "opportunityId": row.get("ConvertedOpportunityId"),
            "market": MARKET_BY_RECORD_TYPE.get(row.get("RecordTypeId")),
            "won": bool(opp.get("IsWon")),
            "amount": opp.get("Amount") or 0,
        }
    return out


def signed_quotes(session, instance_url):
    """Per Opportunity de datum waarop de offerte getekend is.

    Voor de offerteflow is dit het juiste signaal. CloseDate op de Opportunity
    blijkt een verwachte datum die niet wordt bijgewerkt — bij 436 van de 500
    gewonnen opportunities ligt die vóór de laatste wijziging. Date - Quote
    signed op het Quote-object is wél het werkelijke moment.
    """
    soql = (
        "SELECT OpportunityId, Date_Quote_signed__c FROM Quote "
        "WHERE Date_Quote_signed__c != null "
        f"AND Date_Quote_signed__c = LAST_N_DAYS:{LOOKBACK_DAYS}"
    )
    out = {}
    for row in query_all(session, instance_url, soql):
        opp = row.get("OpportunityId")
        day = (row.get("Date_Quote_signed__c") or "")[:10]
        if not opp or not day:
            continue
        # Meerdere offertes per opportunity: de eerste ondertekening telt.
        if opp not in out or day < out[opp]:
            out[opp] = day
    return out


def load_transitions(path):
    """Alle statusovergangen uit het rapport, gegroepeerd per lead.

    Eén keer inlezen, twee keer gebruiken: zowel de routetelling als de
    trechters lopen over dezelfde per-lead lijstjes. Het rapport telt tienduizenden
    regels en groeit nog, dus we houden niets méér vast dan die lijstjes.
    """
    if not os.path.exists(path):
        print(f"WAARSCHUWING: {path} ontbreekt.")
        return {}

    by_lead = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_lead[row["Lead ID"]].append(row)
    return by_lead


def reactivation_paths(path, by_lead=None):
    """Per lead: kwam die via de mailflow terug, en langs welke route?

    De statusovergangen worden op datum doorlopen. Pas ná een instroom in
    Mailjourney telt een heractivatiesignaal mee — dat is precies wat deze
    toerekening onderscheidt van 'heeft toevallig ook een mail gehad'.

    `by_lead` mag een al ingelezen rapport zijn (zie `load_transitions`), zodat
    het bestand niet twee keer over de schijf hoeft.
    """
    if by_lead is None:
        by_lead = load_transitions(path)
    if not by_lead:
        return {}

    paths = {}
    for lead, rows in by_lead.items():
        rows.sort(key=lambda r: r.get("Edit Date") or "")

        entered = None
        entry_reason = ""
        route = None
        route_date = None
        appointment = None
        steps = []

        for row in rows:
            new = (row.get("New Value") or "").strip()
            day = (row.get("Edit Date") or "")[:10]

            if entered is None:
                if new == S_MAILJOURNEY:
                    entered = day
                    entry_reason = (row.get("Reason") or "").strip()
                continue

            if route is None and new in REENTRY_ROUTES:
                route = new
                route_date = day
            if appointment is None and new == S_APPOINTMENT:
                appointment = day
            # Alleen de betekenisvolle statussen bewaren; de string zelf
            # wordt pas bij de conversie opgebouwd, want dan pas weten we
            # tot welke datum het pad loopt.
            if new in PATH_STATUSES:
                steps.append((day, new))

        if entered is None:
            continue

        last = rows[-1]
        paths[lead] = {
            "market": (last.get("Market") or "").strip().lower(),
            "reason": (last.get("Reason") or "").strip(),
            # De reden zoals vastgelegd bij de instroom in de mailflow; die
            # beschrijft waaróm de lead de flow in ging. Valt terug op de
            # laatst bekende reden als het instroommoment er geen had.
            "entryReason": entry_reason or (last.get("Reason") or "").strip(),
            "entered": entered,
            "route": route,
            "routeDate": route_date,
            "appointment": appointment,
            "steps": steps,
        }
    return paths


def appointment_split(by_lead, active):
    """Elke afspraak uit de mailflow, verdeeld over elkaar uitsluitende groepen.

    De twee trechters hierboven vertellen niet het hele verhaal: samen dekken
    ze maar een deel van de afspraken die uit de mailflow komen. De rest valt
    buiten hun strikte volgorde en bleef daardoor onzichtbaar, waardoor de
    deelgetallen nooit optelden tot een herkenbaar totaal.

    Deze functie telt daarom élke afspraak van een lead die eerder met een
    actieve reden in `Not reached` of `Mailjourney` stond, en legt hem in
    precies één bak:

        notReached   volgde de volledige route via Not reached
        mailjourney  volgde de volledige route via Mailjourney
        direct       ging rechtstreeks van de mailflow naar de afspraak,
                     zonder tussenliggende heractivatiestatus
        other        kwam er langs een andere weg, bijvoorbeeld een lead die
                     niet vanuit New de flow in kwam

    De vier bakken tellen op tot het totaal, en de eerste twee zijn precies de
    trechters hierboven. Alleen de eerste afspraak van een lead telt mee.
    """
    days = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    totals = defaultdict(lambda: defaultdict(int))
    routes = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    route_totals = defaultdict(lambda: defaultdict(int))
    paths = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    path_totals = defaultdict(lambda: defaultdict(int))
    route_reason = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    )

    for lead, rows in by_lead.items():
        rows.sort(key=lambda r: r.get("Edit Date") or "")
        market = (rows[-1].get("Market") or "").strip().lower()
        if not market:
            continue
        fallback_reason = (rows[-1].get("Reason") or "").strip()

        # De reden van de láátste instroom telt, niet van de eerste. Een lead
        # die eerst onder een actieve reden geparkeerd stond en daarna onder
        # een reden zonder flow opnieuw, krijgt geen mail meer; zijn afspraak
        # hoort hier dan niet bij. Andersom ook: wie later alsnog in een
        # actieve flow komt, telt wel.
        in_flow = False
        day = previous = None
        for row in rows:
            new = (row.get("New Value") or "").strip()
            when = (row.get("Edit Date") or "")[:10]
            if not when:
                continue
            if new in FUNNEL_ENTRY:
                reason = (row.get("Reason") or "").strip() or fallback_reason
                in_flow = reason in active
                if in_flow:
                    # Noemer voor de groepen die buiten de twee vaste routes
                    # vallen: elke instroom in de mailflow, ongeacht of de
                    # lead daarvoor op New stond.
                    days[when][market]["entry"] += 1
                    totals[market]["entry"] += 1
            if new == S_APPOINTMENT and in_flow:
                day, previous = when, (row.get("Old Value") or "").strip()
                break
        if not day:
            continue

        bucket = None
        for funnel, second in PATH_FUNNELS.items():
            stages = _walk_funnel(rows, second, fallback_reason, active)
            if stages and stages.get("appointment") == day:
                bucket = funnel
                break
        if bucket is None:
            bucket = "direct" if previous in FUNNEL_ENTRY else "other"

        days[day][market][bucket] += 1
        totals[market][bucket] += 1
        if bucket == "direct":
            # Uit welke van de twee statussen kwam de directe afspraak?
            key = "directNotReached" if previous == "Not reached" else "directMailjourney"
            days[day][market][key] += 1
            totals[market][key] += 1

        # Langs welke laatste stap kwam de afspraak binnen? Dezelfde indeling
        # als bij de orders, zodat beide blokken dezelfde taal spreken.
        route = APPOINTMENT_ROUTES.get(previous, "Overig")
        routes[day][market][route] += 1
        route_totals[market][route] += 1

        # En het volledige statuspad erheen, voor de uitklaplijst.
        entry_reason, steps = _appointment_steps(rows, day, fallback_reason)
        path = build_status_path(entry_reason, steps, until=day)
        paths[day][market][path] += 1
        path_totals[market][path] += 1
        route_reason[day][market][route][entry_reason or "(geen reden)"] += 1

    return {
        "days": days,
        "totals": totals,
        "routes": routes,
        "routeTotals": route_totals,
        "paths": paths,
        "pathTotals": path_totals,
        "routeReason": route_reason,
    }


def _appointment_steps(rows, until, fallback_reason):
    """De instroomreden en de statusstappen tot aan de afspraak.

    Geeft hetzelfde soort pad terug als bij de orders: de mailflow-instroom
    met zijn reden als kop, daarna elke relevante status tot en met de
    afspraak. Stappen ná de afspraakdag blijven buiten beeld.
    """
    entry_reason = None
    steps = []
    started = False
    for row in rows:
        new = (row.get("New Value") or "").strip()
        day = (row.get("Edit Date") or "")[:10]
        if not day or day > until:
            continue
        if new in FUNNEL_ENTRY and not started:
            entry_reason = (row.get("Reason") or "").strip() or fallback_reason
            started = True
            continue
        if started and new in PATH_STATUSES:
            steps.append((day, new))
        if started and new == S_APPOINTMENT and day == until:
            break
    return entry_reason, steps


def path_funnels(by_lead, converted, active):
    """Dagtellingen per markt, per trechter en per trechterstap.

    Twee vaste paden (zie PATH_FUNNELS) worden als strikte volgorde door de
    statushistorie van elke lead gelopen:

        new -> stage2 -> reentered -> appointment -> order

    Regels:

      - Stap 1 en 2 zijn dezelfde overgang: Old Value `New` naar de tweede
        status van de trechter (`Not reached` of `Mailjourney`). Die telt dus
        op dezelfde dag voor `new` én `stage2`. De eerste zo'n overgang telt;
        een lead die later nog eens op New wordt gezet begint geen tweede keer.
      - Stap 3 is de eerstvolgende overgang náár `Re-entered` of
        `Re-entered - Phone Number Changed`.
      - Stap 4 is de eerstvolgende overgang náár `Appointment`.
      - Stap 5 is een gewonnen Opportunity uit `converted`, geteld op de
        sluitdatum (`date`), en alleen als die op of ná de afspraakdag ligt.
      - Bij de eerste stap die niet lukt stopt de lead: wie wel heractiveert
        maar nooit een afspraak krijgt, telt in stap 1 t/m 3 en verder niet.

    Alleen leads met een actieve instroomreden tellen mee (`active`, uit
    `config/flow_goals.json`). De reden wordt van de instroomovergang zelf
    gelezen; die is vaak leeg, dan valt hij terug op de laatst bekende reden
    van de lead.

    LET OP 1: een lead kan in béide trechters zitten als zijn historie allebei
    de routes bevat (bijvoorbeeld eerst New -> Not reached en later, na een
    nieuwe New, New -> Mailjourney). Zo'n lead telt in allebei mee; de twee
    trechters zijn dus niet exclusief en mogen niet bij elkaar opgeteld worden.

    LET OP 2: elke stap wordt geteld op de dag waaróp die stap gebeurde, niet
    op de instroomdag. Daardoor is elk datumbereik optelbaar, maar beschrijft
    een bereik géén cohort: stap 4 in september kan bij een lead horen die in
    juni zijn stap 1 zette. De trechter leest dus als 'wat gebeurde er deze
    periode per stap', niet als 'van deze instroom haalde zoveel procent het'.
    """
    days = defaultdict(
        lambda: defaultdict(
            lambda: {
                funnel: {stage: 0 for stage in FUNNEL_STAGES + FUNNEL_ENRICHED}
                for funnel in PATH_FUNNELS
            }
        )
    )
    totals = defaultdict(
        lambda: {
            funnel: {stage: 0 for stage in FUNNEL_STAGES + FUNNEL_ENRICHED}
            for funnel in PATH_FUNNELS
        }
    )
    both = 0

    for lead, rows in by_lead.items():
        rows.sort(key=lambda r: r.get("Edit Date") or "")
        last = rows[-1]
        market = (last.get("Market") or "").strip().lower()
        if not market:
            continue
        fallback_reason = (last.get("Reason") or "").strip()

        matched = 0
        for funnel, second in PATH_FUNNELS.items():
            stages = _walk_funnel(rows, second, fallback_reason, active)
            if not stages:
                continue
            matched += 1
            conv = converted.get(lead) or {}
            if (
                stages.get("appointment")
                and conv.get("won")
                and conv.get("date")
                and conv["date"] >= stages["appointment"]
            ):
                stages["order"] = conv["date"]

            for stage in FUNNEL_STAGES + FUNNEL_ENRICHED:
                day = stages.get(stage)
                if not day:
                    continue
                days[day][market][funnel][stage] += 1
                totals[market][funnel][stage] += 1
        if matched > 1:
            both += 1

    return days, totals, both


def _walk_funnel(rows, second, fallback_reason, active):
    """De dagen waarop deze lead de stappen van één trechter zette.

    Geeft een dict stap -> dag terug, of None als de lead niet eens instroomt
    of zijn reden niet actief is. `order` wordt hier niet gevuld: die hangt aan
    de Opportunity, niet aan de statushistorie.
    """
    stages = {}
    for row in rows:
        old = (row.get("Old Value") or "").strip()
        new = (row.get("New Value") or "").strip()
        day = (row.get("Edit Date") or "")[:10]
        if not day:
            continue

        if "new" not in stages:
            if old == "New" and new == second:
                reason = (row.get("Reason") or "").strip() or fallback_reason
                if reason not in active:
                    return None
                # Stap 1 en 2 zijn dezelfde overgang en delen dus hun dag.
                stages["new"] = day
                stages["stage2"] = day
            continue

        # Onderweg opnieuw geparkeerd onder een reden zonder actieve flow?
        # Dan stopt de route hier: vanaf dat moment krijgt de lead geen mail
        # meer, dus wat daarna gebeurt is niet aan de flow toe te rekenen.
        if new in FUNNEL_ENTRY:
            reason = (row.get("Reason") or "").strip() or fallback_reason
            if reason not in active:
                # De stappen die hij wél zette blijven staan; alleen wat erna
                # komt telt niet meer mee.
                return stages

        if "reentered" not in stages:
            if new in FUNNEL_REENTRY:
                stages["reentered"] = day
                # Kwam deze lead pas terug ná nummerverrijking? Dan loopt hij
                # verderop als aftakking mee in plaats van op de hoofdlijn.
                if new == S_PHONE_CHANGED:
                    stages["reenteredEnriched"] = day
            continue

        if "appointment" not in stages:
            if new == S_APPOINTMENT:
                stages["appointment"] = day
                if "reenteredEnriched" in stages:
                    stages["appointmentEnriched"] = day
            continue

        break

    return stages or None


def main():
    token, instance_url = get_token()
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    converted = conversions(session, instance_url)
    signed = signed_quotes(session, instance_url)
    print(f"Getekende offertes in {LOOKBACK_DAYS} dagen: {len(signed)}")
    transitions = load_transitions(REPORT_PATH)
    paths = reactivation_paths(REPORT_PATH, transitions)
    flows = reached_by_flow(REACHED_PATH)
    first_send_all, first_send_flow = first_send_dates(REACHED_PATH)

    # Offerteflow en nurture lane blijven op journeynaam; die leads hebben
    # geen Reason - Mailjourney.
    first_send_flow = {
        flow: per_lead for flow, per_lead in first_send_flow.items()
        if is_scoped_flow(flow)
    }
    print(f"Flows op naam meegeteld: {sorted(first_send_flow)}")

    # Iedereen die in de mailflow is beland hoort bij de doelgroep. Filteren
    # op een ingevulde reden zou 1.640 leads laten vallen die wel degelijk
    # bewerkt worden — en dan zou het strenge cijfer hoger uitvallen dan het
    # brede, terwijl het er een deelverzameling van hoort te zijn. De reden is
    # een uitsplitsing, geen toegangseis.
    warnings = []

    active = active_reasons(GOALS_PATH)
    if active is None:
        active = set()
        warnings.append(
            f"{GOALS_PATH} ontbreekt; geen enkele instroomreden telt mee.")
    parked = {
        lead for lead, info in paths.items()
        if (info.get("entryReason") or info.get("reason") or "").strip() in active
    }
    print(f"Leads in de mailflow met een actieve reden: {len(parked)} "
          f"(van {len(paths)}), {len(active)} redenen actief")

    # De twee vaste trechters, per dag en per stap. Staat los van de
    # omzettoerekening hieronder: dit telt leads, geen euro's.
    funnel_days, funnel_totals, funnel_both = path_funnels(
        transitions, converted, active)
    print(f"Leads in beide trechters tegelijk: {funnel_both}")

    # Alle afspraken uit de mailflow, verdeeld over elkaar uitsluitende bakken
    # die optellen tot het totaal.
    split = appointment_split(transitions, active)
    split_days, split_totals = split["days"], split["totals"]
    for market, buckets in sorted(split_totals.items()):
        total = sum(buckets[k] for k in ("notReached", "mailjourney", "direct", "other"))
        print(f"  {market}: {total} afspraken uit de mailflow = "
              + " + ".join(f"{buckets[k]} {k}"
                           for k in ("notReached", "mailjourney", "direct", "other")))

    # Eerste mail per lead binnen de doelgroep. Voor geparkeerde leads telt
    # elke automation-mail; voor de offerte- en nurtureflows alleen die flows.
    first_send = {}
    for per_lead in first_send_flow.values():
        for lead, day in per_lead.items():
            if lead not in first_send or day < first_send[lead]:
                first_send[lead] = day
    for lead, day in first_send_all.items():
        if lead in parked and (lead not in first_send or day < first_send[lead]):
            first_send[lead] = day
    print(f"Conversies in {LOOKBACK_DAYS} dagen: {len(converted)}")
    print(f"Leads met een mailflow-historie: {len(paths)}")

    if flows is None:
        warnings.append(
            f"{REACHED_PATH} ontbreekt of is onleesbaar; byFlow is overgeslagen."
        )
        print(f"WAARSCHUWING: {warnings[-1]}")

    def empty_day():
        return {
            "orders": 0,
            "revenue": 0.0,
            "viaAutomationOrders": 0,
            "viaAutomationRevenue": 0.0,
            # Breder: elke lead die door een automation-flow gemaild is en
            # daarna converteerde. Vangt ook flows zonder her-activatiepad,
            # zoals de offerteflow naar leads met een openstaande offerte.
            "mailedOrders": 0,
            "mailedRevenue": 0.0,
            "byRoute": defaultdict(lambda: {"orders": 0, "revenue": 0.0}),
            "byReason": defaultdict(lambda: {"orders": 0, "revenue": 0.0}),
            "byPath": defaultdict(lambda: {"orders": 0, "revenue": 0.0}),
            "byRouteReason": defaultdict(
                lambda: defaultdict(lambda: {"orders": 0, "revenue": 0.0})
            ),
            "byFlow": defaultdict(lambda: {"orders": 0, "revenue": 0.0}),
        }

    days = defaultdict(lambda: defaultdict(empty_day))
    # Totalen over de hele terugkijkperiode, per markt en per statuspad.
    path_totals = defaultdict(
        lambda: defaultdict(lambda: {"orders": 0, "revenue": 0.0, "leads": set()})
    )
    routes = defaultdict(lambda: defaultdict(lambda: {
        "reached": 0, "withAppointment": 0, "orders": 0, "revenue": 0.0,
    }))

    # Hoeveel leads raakten elke route, los van of ze converteerden.
    for info in paths.values():
        if not info["market"] or not info["route"]:
            continue
        bucket = routes[info["market"]][info["route"]]
        bucket["reached"] += 1
        if info["appointment"]:
            bucket["withAppointment"] += 1

    for lead, conv in converted.items():
        if not conv["won"] or not conv["date"]:
            continue
        info = paths.get(lead)
        # Markt bij voorkeur uit de statushistorie, anders uit het RecordType
        # van de Lead. Zo tellen ook orders mee van leads die nooit in de
        # mailflow zaten — die horen in de noemer thuis.
        market = (info or {}).get("market") or conv.get("market")
        if not market:
            continue

        bucket = days[conv["date"]][market]
        bucket["orders"] += 1
        bucket["revenue"] += conv["amount"]

        # Brede toerekening: gemaild door een automation-flow, en de order
        # kwam daarna. Dit is de noemer die het dashboard toont, want alle
        # orders in het CRM zeggen hier niets.
        mailed_on = first_send.get(lead)
        # Volgorde toetsen op de conversiedatum, niet op de sluitdatum: een
        # mail die pas ná de conversie uitging heeft de order niet veroorzaakt.
        outcome = conv.get("convertedDate") or conv["date"]

        # Voor flows die mikken op een openstaande offerte ligt de conversie
        # al achter ons; daar telt het moment van tekenen.
        signed_on = signed.get(conv.get("opportunityId"))
        opp_outcome = signed_on or conv["date"]
        opp_mailed_on = None
        for flow, per_lead in first_send_flow.items():
            if not targets_open_opportunity(flow):
                continue
            day = per_lead.get(lead)
            if day and (opp_mailed_on is None or day < opp_mailed_on):
                opp_mailed_on = day

        in_scope = bool(
            (mailed_on and outcome >= mailed_on)
            or (opp_mailed_on and opp_outcome >= opp_mailed_on)
        )
        if in_scope:
            bucket["mailedOrders"] += 1
            bucket["mailedRevenue"] += conv["amount"]
            for flow, per_lead in first_send_flow.items():
                day = per_lead.get(lead)
                # Per flow de juiste uitkomstdatum: sluitdatum voor
                # offerteflows, conversiedatum voor de rest.
                deadline = opp_outcome if targets_open_opportunity(flow) else outcome
                if day and deadline >= day:
                    box = bucket["byFlow"].setdefault(
                        flow, {"orders": 0, "revenue": 0.0})
                    box["orders"] += 1
                    box["revenue"] += conv["amount"]

        # Toerekenen aan automation vereist: eerst de mailflow in, daarna een
        # heractivatiesignaal, en de conversie ná dat signaal. Zonder die
        # volgorde is het toeval in dezelfde periode, geen heractivatie.
        # Leads zonder mailflow-historie tellen alleen in de noemer mee.
        # Het strenge cijfer is per definitie een deelverzameling van het
        # brede: eerst gemaild, en daarbovenop een aantoonbaar heractivatiepad.
        route = (info or {}).get("route")
        if (
            in_scope
            and route
            and info.get("routeDate")
            and outcome >= info["routeDate"]
        ):
            bucket["viaAutomationOrders"] += 1
            bucket["viaAutomationRevenue"] += conv["amount"]
            bucket["byRoute"][route]["orders"] += 1
            bucket["byRoute"][route]["revenue"] += conv["amount"]
            reason = info.get("reason") or "(geen reden)"
            bucket["byReason"][reason]["orders"] += 1
            bucket["byReason"][reason]["revenue"] += conv["amount"]
            # Reden gekruist met route, zodat een route uit te klappen is
            # naar de Mailjourney-redenen waar hij vandaan komt.
            cross = bucket["byRouteReason"][route][reason]
            cross["orders"] += 1
            cross["revenue"] += conv["amount"]
            routes[market][route]["orders"] += 1
            routes[market][route]["revenue"] += conv["amount"]

            # Het statuspad tot en met de laatste stap vóór de conversie.
            path_str = build_status_path(
                info.get("entryReason") or "",
                info.get("steps") or [],
                until=conv["date"],
            )
            bucket["byPath"][path_str]["orders"] += 1
            bucket["byPath"][path_str]["revenue"] += conv["amount"]
            total = path_totals[market][path_str]
            total["orders"] += 1
            total["revenue"] += conv["amount"]
            total["leads"].add(lead)

            # Meervoudig toegerekend: een lead die door drie journeys is
            # aangeraakt telt bij alle drie mee. Nooit optellen.

    def flatten(nested):
        return {
            key: {"orders": v["orders"], "revenue": round(v["revenue"], 2)}
            for key, v in nested.items()
        }

    out_days = {}
    for day, per_market in days.items():
        out_days[day] = {}
        for market, bucket in per_market.items():
            out_days[day][market] = {
                "orders": bucket["orders"],
                "revenue": round(bucket["revenue"], 2),
                "viaAutomationOrders": bucket["viaAutomationOrders"],
                "viaAutomationRevenue": round(bucket["viaAutomationRevenue"], 2),
                "mailedOrders": bucket["mailedOrders"],
                "mailedRevenue": round(bucket["mailedRevenue"], 2),
                "byRoute": flatten(bucket["byRoute"]),
                "byReason": flatten(bucket["byReason"]),
                "byPath": flatten(bucket["byPath"]),
                "byRouteReason": {
                    route: flatten(per_reason)
                    for route, per_reason in bucket["byRouteReason"].items()
                },
            }
            if bucket["byFlow"]:
                out_days[day][market]["byFlow"] = flatten(bucket["byFlow"])

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback_days": LOOKBACK_DAYS,
        "routeLabels": REENTRY_ROUTES,
        "attribution": (
            "Statuspad: instroom in Mailjourney, daarna een heractivatiesignaal "
            "(Re-entered, Nummer gewijzigd of direct Appointment), en de conversie "
            "ná dat signaal. Toegerekend aan de conversiedatum."
        ),
        # byFlow telt een order bij élke journey die de lead raakte. Kolommen
        # daaruit nooit optellen of naast het totaal leggen.
        "byFlowNote": "meervoudig toegerekend",
        # Elke trechterstap is geteld op de dag van die stap, dus elk bereik is
        # optelbaar maar beschrijft geen cohort. Een lead kan in beide
        # trechters zitten; niet bij elkaar optellen.
        "pathFunnelNote": (
            "Per stap geteld op de dag van die stap, niet op de instroomdag: "
            "optelbaar over elk bereik, maar géén cohort. Een lead met beide "
            "routes in zijn historie telt in beide trechters."
        ),
        "pathFunnels": {
            day: {
                market: {
                    funnel: dict(stages)
                    for funnel, stages in per_funnel.items()
                }
                for market, per_funnel in per_market.items()
            }
            for day, per_market in funnel_days.items()
        },
        # Alle afspraken uit de mailflow, in elkaar uitsluitende bakken die
        # optellen tot het totaal. Zie appointment_split().
        "appointmentSplit": {
            day: {market: dict(buckets) for market, buckets in per_market.items()}
            for day, per_market in split_days.items()
        },
        "appointmentSplitTotals": {
            market: dict(buckets) for market, buckets in split_totals.items()
        },
        # Dezelfde afspraken, nu langs de route waarlangs ze binnenkwamen en
        # langs het volledige statuspad. Voedt het uitklapblok onder de funnel.
        "appointmentRoutes": {
            day: {market: dict(r) for market, r in per_market.items()}
            for day, per_market in split["routes"].items()
        },
        "appointmentPaths": {
            day: {market: dict(p) for market, p in per_market.items()}
            for day, per_market in split["paths"].items()
        },
        "appointmentRouteReason": {
            day: {
                market: {route: dict(reasons) for route, reasons in per_route.items()}
                for market, per_route in per_market.items()
            }
            for day, per_market in split["routeReason"].items()
        },
        "pathFunnelTotals": {
            market: {funnel: dict(stages) for funnel, stages in per_funnel.items()}
            for market, per_funnel in funnel_totals.items()
        },
        "warnings": warnings,
        "days": out_days,
        "paths": {
            market: {
                path_str: {
                    "orders": v["orders"],
                    "revenue": round(v["revenue"], 2),
                    "leads": len(v["leads"]),
                }
                for path_str, v in per_path.items()
            }
            for market, per_path in path_totals.items()
        },
        "routes": {
            market: {
                route: {**v, "revenue": round(v["revenue"], 2)}
                for route, v in per_route.items()
            }
            for market, per_route in routes.items()
        },
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print(f"\nGeschreven naar {OUT_PATH}")
    for market, per_route in sorted(out["routes"].items()):
        print(f"\n  {market.upper()}  {'route':34} {'leads':>7} {'afspr':>7} {'orders':>7} {'omzet':>13}")
        for route, v in sorted(per_route.items(), key=lambda x: -x[1]["revenue"]):
            label = REENTRY_ROUTES.get(route, route)
            print(
                f"        {label:34} {v['reached']:7} {v['withAppointment']:7} "
                f"{v['orders']:7} {v['revenue']:13,.0f}"
            )


if __name__ == "__main__":
    main()
