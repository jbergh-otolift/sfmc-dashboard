"""Export van e-mail tracking uit Salesforce Marketing Cloud per Business Unit.

Haalt per BU (markt) de tracking events op via de SOAP API (SentEvent, OpenEvent,
ClickEvent, BounceEvent, UnsubEvent) en de journey-definities via de REST API, en
schrijft alles geaggregeerd per DAG en per flow naar `exports/sfmc_tracking.json`.

Een flow is een JOURNEY, niet een losse e-mail: elke tracking-event wordt via zijn
TriggeredSendDefinition teruggeleid naar de journey die de send deed.

Het dashboard laat de lezer een WILLEKEURIGE begin- en einddatum kiezen en telt
zelf op. Dat kan alleen als elke metric optelbaar is over dagen, en dat is precies
wat verzendattributie oplevert: elke teller hangt aan de dag van de SENTEVENT, niet
aan de dag waarop de open/klik/bounce binnenkwam. Een (SubscriberKey, SendID)-paar
hoort bij precies een send, en die send gebeurde op precies een dag; een open op
20 september van een mail van 14 september telt dus in de bucket van 14 september.
Opens en kliks blijven UNIEKE (SubscriberKey, SendID)-paren - de deduplicatie zelf
verandert niet - maar omdat zo'n paar in exact een dagbucket valt, mogen de
dagtotalen nu wel opgeteld worden.

Per markt staat er onder `markets.<markt>`:

- `days.<YYYY-MM-DD>.flows.<journeynaam>` met `sent`, `delivered`, `opens`,
  `clicks`, `bounces` (alleen hard), `soft_bounces`, `unsubs`, `subscribers`,
  `firstTouch` en `emails.<mailnaam>` met dezelfde tellers. `all` is het
  markttotaal.
- `firstTouch` is het aantal mensen van wie de VROEGSTE send uit die flow op
  die dag viel. Iedere abonnee heeft per flow precies een vroegste dag, dus dit
  telt wel op over een bereik terwijl `subscribers` dat niet doet. "Vroegste"
  betekent eerlijk: vroegste BINNEN de retrieve - wie al voor het venster in de
  flow zat en er binnen het venster nog een mail uit kreeg, telt op die latere
  dag als eerste aanraking. Verder terugkijken doen we bewust niet. `all` wordt
  apart geteld over alle flows samen (vroegste send uit welke flow dan ook) en
  is dus NIET de som van de flows: iemand kan op twee dagen nieuw zijn in twee
  verschillende flows.
- `flowProfile.<journeynaam>`: over het HELE opgehaalde venster het aantal
  unieke mensen, het aantal sends en `mailsPerPerson`. Beschrijft hoe een flow
  ontworpen is, los van de gekozen periode.
- `unattributed`: engagement waarvan de bijbehorende send BUITEN het opgehaalde
  venster viel en die dus geen dagbucket heeft. Die events worden niet stilletjes
  weggegooid maar hier per eventtype geteld (met `eventTotals` als noemer).
- `reach.{7,30,90}`: unieke SubscriberKeys over het rollende venster dat op
  `endDate` eindigt. Bereik is niet optelbaar over dagen, dus het dashboard toont
  "mails per persoon" alleen voor deze drie presets.

Percentages (delivery, open, ctr, ctor, unsub, bounce) staan er BEWUST niet in:
de browser rekent ze uit de opgetelde tellers, zodat een voorberekend percentage
nooit kan botsen met een opgeteld percentage.

Onder `journeyStructures` staat per markt en per journey uit `flows` het
stroomschema van de live versie (knopen + edges), zodat het dashboard de
journey als diagram kan tekenen en de metrics eraan kan koppelen.

Gebruik:

    PERIOD_END=2026-09-16 RETRIEVE_DAYS=400 python3 scripts/export_sfmc_tracking.py

Environment variables:

- `SFMC_SUBDOMAIN`, `SFMC_CLIENT_ID`, `SFMC_CLIENT_SECRET` - credentials van het
  (read-only) installed package.
- `SFMC_MID_NL`, `SFMC_MID_BE`, en optioneel `SFMC_MID_FR`, `SFMC_MID_IT` - de MID
  (account_id) per BU. Een BU zonder MID wordt overgeslagen met een waarschuwing;
  FR en IT bestaan nog niet.
- `PERIOD_END` (default: morgen UTC), `YYYY-MM-DD`, exclusief.
- `RETRIEVE_DAYS` (default 400) - hoe ver de ene retrieve per BU terugkijkt vanaf
  `endDate`. Dat bepaalt hoe ver de datumkiezer in het dashboard terug kan.

Let op: dit is een retrieve over hondederden dagen (~380k events voor NL bij 400
dagen), reken op vele minuten per BU. De events worden per pagina meteen in de
dagbuckets verwerkt en daarna weer vrijgegeven, zodat het geheugengebruik niet
met de venstergrootte meegroeit.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MARKETS = ["nl", "be", "fr", "it"]
OUT_PATH = "exports/sfmc_tracking.json"
# De bereikte Lead-id's gaan naar een apart bestand dat NIET wordt gecommit
# (zie .gitignore). Het dient alleen als tussenstap: export_lead_consent.py
# snijdt het door met de leads die we hóren te mailen en bewaart enkel de
# aantallen. Zo staan er geen losse id-lijsten in een publieke repo.
REACHED_PATH = "exports/_reached_lead_ids.json"

# Coverage meet iets anders dan Email Health en heeft daarom een eigen,
# langer venster. De noemer (leads met status Mailjourney) is een
# momentopname zonder periode; meet je de teller over 19 dagen, dan telt
# iedereen in een nurture-flow met een cyclus van zes weken ten onrechte als
# "niet bereikt". 90 dagen omvat minstens één volledige cyclus.
COVERAGE_DAYS = int(os.environ.get("COVERAGE_DAYS", "90"))

# De rollende vensters waarvoor we bereik (`reach`) voorberekenen. Bereik is
# een distinct-count over SubscriberKeys en dus niet optelbaar over dagen; het
# dashboard toont "mails per persoon" daarom alleen voor deze drie presets.
REACH_PERIODS = [7, 30, 90]

# Hoe ver de ene retrieve per BU terugkijkt vanaf `endDate`. Ruim een jaar,
# zodat de datumkiezer in het dashboard ver terug kan en er weinig engagement
# overblijft waarvan de send buiten het venster valt (zie `unattributed`).
RETRIEVE_DAYS = int(os.environ.get("RETRIEVE_DAYS", "400"))

NS = {"p": "http://exacttarget.com/wsdl/partnerAPI"}
UNASSIGNED = "(niet toegewezen)"
TOKEN_TTL = 15 * 60
MAX_PAGES = 1200

TRACKING_OBJECTS = [
    ("SentEvent", ["SubscriberKey", "EventDate", "SendID", "TriggeredSendDefinitionObjectID"]),
    ("OpenEvent", ["SubscriberKey", "EventDate", "SendID", "TriggeredSendDefinitionObjectID"]),
    ("ClickEvent", ["SubscriberKey", "EventDate", "SendID", "TriggeredSendDefinitionObjectID"]),
    ("BounceEvent", ["SubscriberKey", "EventDate", "SendID", "TriggeredSendDefinitionObjectID", "BounceCategory"]),
    ("UnsubEvent", ["SubscriberKey", "EventDate", "SendID", "TriggeredSendDefinitionObjectID"]),
]

SOAP_ENVELOPE = """<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing">
<s:Header><a:Action s:mustUnderstand="1">Retrieve</a:Action><a:To s:mustUnderstand="1">{soap_url}</a:To>
<fueloauth xmlns="http://exacttarget.com">{token}</fueloauth></s:Header>
<s:Body xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<RetrieveRequestMsg xmlns="http://exacttarget.com/wsdl/partnerAPI"><RetrieveRequest>
<ObjectType>{obj}</ObjectType>
{properties}
{tail}
</RetrieveRequest></RetrieveRequestMsg></s:Body></s:Envelope>"""

DATE_FILTER = """<Filter xsi:type="ComplexFilterPart">
 <LeftOperand xsi:type="SimpleFilterPart"><Property>EventDate</Property><SimpleOperator>greaterThan</SimpleOperator><Value>{start}</Value></LeftOperand>
 <LogicalOperator>AND</LogicalOperator>
 <RightOperand xsi:type="SimpleFilterPart"><Property>EventDate</Property><SimpleOperator>lessThan</SimpleOperator><Value>{end}</Value></RightOperand>
</Filter>"""

WARNINGS = []


def warn(message):
    print(f"WAARSCHUWING: {message}")
    WARNINGS.append(message)


def make_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class Auth:
    """Houdt het token per MID bij en vernieuwt het bij 401 of na ~15 minuten."""

    def __init__(self, session, subdomain, client_id, client_secret, mid):
        self.session = session
        self.url = f"https://{subdomain}.auth.marketingcloudapis.com/v2/token"
        self.client_id = client_id
        self.client_secret = client_secret
        self.mid = mid
        self.token = None
        self.fetched_at = 0.0
        self.soap_url = None
        self.rest_url = None
        self.refresh()

    def refresh(self):
        resp = self.session.post(
            self.url,
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "account_id": self.mid,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        self.fetched_at = time.time()
        self.soap_url = data["soap_instance_url"].rstrip("/") + "/Service.asmx"
        self.rest_url = data["rest_instance_url"]
        return self.token

    def valid_token(self):
        if self.token is None or time.time() - self.fetched_at > TOKEN_TTL:
            self.refresh()
        return self.token


def strip_hex_suffix(value):
    """Haalt de door SFMC aangeplakte ` - <32 hex>` van een naam af."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    parts = text.rsplit(" - ", 1)
    if len(parts) == 2 and len(parts[1]) == 32:
        try:
            int(parts[1], 16)
            return parts[0].strip()
        except ValueError:
            pass
    return text


def activity_prefix(value):
    """Normaliseert een activiteit-/TSD-naam tot de prefix voor de hex-suffix."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    parts = text.rsplit(" - ", 1)
    if len(parts) == 2 and len(parts[1]) == 32:
        try:
            int(parts[1], 16)
            text = parts[0]
        except ValueError:
            pass
    return " ".join(text.lower().split())


def iter_interactions(session, auth, status=None):
    """Pagineert door de interactions-lijst; levert alle versies van elke journey."""
    url = f"{auth.rest_url.rstrip('/')}/interaction/v1/interactions"
    page = 1
    while page <= 200:
        params = {
            "extras": "activities",
            "mostRecentVersionOnly": "false",
            "$pageSize": 50,
            "$page": page,
        }
        if status:
            params["status"] = status
        resp = session.get(
            url,
            headers={"Authorization": f"Bearer {auth.valid_token()}"},
            params=params,
            timeout=120,
        )
        if resp.status_code == 401:
            auth.refresh()
            continue
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items") or []
        for item in items:
            yield item
        count = payload.get("count")
        if not items or count is None or page * (payload.get("pageSize") or 50) >= count:
            return
        page += 1


def fetch_journeys(session, auth):
    """Haalt alle journeyversies op en bouwt maps van send-identifier -> journeynaam.

    Attributie gebeurt per journey, niet per e-mail, dus we hebben ALLE versies
    nodig: sends in het rapportagevenster komen vaak uit een oudere versie met
    andere triggered sends. `mostRecentVersionOnly=false` levert die versies;
    `extras=activities` is genoeg en veel sneller dan `extras=all`.
    """
    seen_journeys = {}
    id_to_journey = {}
    email_to_journey = {}
    prefix_to_journey = {}
    # TSD-sleutel -> naam van de e-mailactiviteit in de journey-definitie; die
    # leest prettiger dan de (afgekapte) TSD-naam uit de SOAP-retrieve.
    id_to_email_name = {}
    for status in (None, "Deleted"):
        # Verwijderde journeys staan niet in de standaardlijst maar hebben in het
        # venster wel gestuurd; die nemen we alleen mee voor de mapping.
        for item in iter_interactions(session, auth, status):
            # Alle versies van een journey vallen onder een label: de naam zelf.
            name = item.get("name") or UNASSIGNED
            version = item.get("version") or 0
            current = seen_journeys.get(item.get("id"))
            if status is None and (current is None or version >= (current.get("version") or 0)):
                seen_journeys[item.get("id")] = {
                    "id": item.get("id"),
                    "name": name,
                    "version": version,
                    "status": item.get("status"),
                }
            for activity in item.get("activities") or []:
                if activity.get("type") != "EMAILV2":
                    continue
                args = activity.get("configurationArguments") or {}
                # Sleutels van deze activiteit verzamelen voor de naam-map.
                activity_keys = []
                activity_name = strip_hex_suffix(activity.get("name"))
                # Tier 1: `triggeredSendId` is de TSD die bij het draaien van deze
                # versie echt gebruikt is; die staat in de tracking events.
                runtime_id = args.get("triggeredSendId")
                if isinstance(runtime_id, str) and runtime_id.strip():
                    id_to_journey.setdefault(runtime_id.strip().lower(), name)
                    activity_keys.append(runtime_id.strip().lower())
                ts = args.get("triggeredSend")
                if not isinstance(ts, dict):
                    for key in activity_keys:
                        if activity_name:
                            id_to_email_name.setdefault(key, activity_name)
                    continue
                # Tier 1: de in de definitie geconfigureerde TSD ObjectID/CustomerKey.
                for key in ("objectId", "objectID", "id", "key", "customerKey"):
                    value = ts.get(key)
                    if isinstance(value, str) and value.strip():
                        id_to_journey.setdefault(value.strip().lower(), name)
                        activity_keys.append(value.strip().lower())
                # Tier 2: emailId, later gebrugd via de TSD-retrieve.
                email_id = ts.get("emailId")
                if email_id is not None and str(email_id).strip():
                    email_to_journey.setdefault(str(email_id).strip(), name)
                # Tier 2-fallback: naam zonder hex-suffix.
                for value in (ts.get("name"), activity.get("name")):
                    prefix = activity_prefix(value)
                    if prefix:
                        prefix_to_journey.setdefault(prefix, name)
                label = activity_name or strip_hex_suffix(ts.get("name"))
                if label:
                    for key in activity_keys:
                        id_to_email_name.setdefault(key, label)

    journeys = sorted(seen_journeys.values(), key=lambda j: (j["name"], j["id"] or ""))
    print(
        f"  journeys: {len(journeys)} (alle versies), tier1-sleutels: {len(id_to_journey)}, "
        f"emailId: {len(email_to_journey)}, naam-prefix: {len(prefix_to_journey)}"
    )
    return journeys, id_to_journey, email_to_journey, prefix_to_journey, id_to_email_name


def structure_type(raw_type):
    """Vertaalt het SFMC-activiteittype naar de vier types van het diagram."""
    text = (raw_type or "").upper()
    if text == "EMAILV2":
        return "EMAILV2"
    if text == "WAIT" or text.startswith("WAIT"):
        return "WAIT"
    if "DECISION" in text or "SPLIT" in text:
        return "DECISION"
    return "OTHER"


def wait_days(args):
    """Rekent waitDuration + waitUnit om naar dagen; None als het geen wacht is."""
    duration = args.get("waitDuration")
    if not isinstance(duration, (int, float)):
        return None
    unit = str(args.get("waitUnit") or "").upper()
    factor = {"MINUTES": 1 / 1440, "HOURS": 1 / 24, "DAYS": 1, "WEEKS": 7}.get(unit)
    if factor is None:
        return None
    days = duration * factor
    return int(days) if float(days).is_integer() else round(days, 4)


def activity_tsd_id(args):
    """De TSD ObjectID van een e-mailactiviteit, zoals in `emailBreakdown`."""
    runtime_id = args.get("triggeredSendId")
    if isinstance(runtime_id, str) and runtime_id.strip():
        return runtime_id.strip().lower()
    ts = args.get("triggeredSend")
    if isinstance(ts, dict):
        for key in ("objectId", "objectID", "id"):
            value = ts.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return None


def build_structure(item, journey_name):
    """Bouwt het knooppunt/edge-model van een journeyversie voor het diagram."""
    activities = item.get("activities") or []
    nodes = {}
    order = []
    for activity in activities:
        key = activity.get("key")
        if not isinstance(key, str) or not key:
            continue
        args = activity.get("configurationArguments") or {}
        node_type = structure_type(activity.get("type"))
        nxt = []
        for outcome in activity.get("outcomes") or []:
            target = outcome.get("next") if isinstance(outcome, dict) else None
            if isinstance(target, str) and target.strip():
                nxt.append(target.strip())
        nodes[key] = {
            "key": key,
            "type": node_type,
            "name": strip_hex_suffix(activity.get("name")) or key,
            "tsdId": activity_tsd_id(args) if node_type == "EMAILV2" else None,
            "waitDays": wait_days(args) if node_type == "WAIT" else None,
            "next": nxt,
            "depth": 0,
        }
        order.append(key)
    if not nodes:
        return None
    # Edges die naar een niet-bestaande sleutel wijzen zijn geen edges.
    for node in nodes.values():
        node["next"] = [k for k in node["next"] if k in nodes]

    incoming = {key: 0 for key in nodes}
    for node in nodes.values():
        for target in node["next"]:
            incoming[target] += 1
    roots = [key for key in order if incoming[key] == 0]

    # Voorkeurssleutels uit de definitie: `defaults.entry` en de trigger-outcomes.
    hints = []
    defaults = item.get("defaults") or {}
    if isinstance(defaults.get("entry"), str):
        hints.append(defaults["entry"].strip())
    for trigger in item.get("triggers") or []:
        for outcome in trigger.get("outcomes") or []:
            target = outcome.get("next") if isinstance(outcome, dict) else None
            if isinstance(target, str) and target.strip():
                hints.append(target.strip())
    preferred = next((h for h in hints if h in nodes), None)

    if not roots:
        # Alles heeft inkomende edges: de journey begint in een lus.
        entry = preferred or sorted(nodes)[0]
        warn(f"journeystructuur '{journey_name}': geen startknoop zonder inkomende edge, '{entry}' gekozen")
    elif len(roots) == 1:
        entry = roots[0]
    else:
        entry = preferred if preferred in roots else sorted(roots)[0]
        warn(f"journeystructuur '{journey_name}': {len(roots)} mogelijke startknopen ({', '.join(sorted(roots))}), '{entry}' gekozen")

    # Bereikbaarheid vanaf de entry; niet-bereikbare knopen vallen af.
    reachable = []
    seen = {entry}
    queue = [entry]
    while queue:
        key = queue.pop(0)
        reachable.append(key)
        for target in nodes[key]["next"]:
            if target not in seen:
                seen.add(target)
                queue.append(target)
    dropped = [key for key in order if key not in seen]
    if dropped:
        warn(f"journeystructuur '{journey_name}': {len(dropped)} knoop/knopen niet bereikbaar vanaf '{entry}', weggelaten ({', '.join(dropped)})")

    # Kahn over het bereikbare deel: wat overblijft zit in een cyclus.
    sub_in = {key: 0 for key in seen}
    for key in seen:
        for target in nodes[key]["next"]:
            sub_in[target] += 1
    depth = {key: 0 for key in seen}
    ready = [key for key in seen if sub_in[key] == 0] or [entry]
    resolved = set()
    guard = 0
    limit = len(seen) * len(seen) + len(seen) + 10
    while ready and guard < limit:
        guard += 1
        key = ready.pop(0)
        if key in resolved:
            continue
        resolved.add(key)
        for target in nodes[key]["next"]:
            depth[target] = max(depth[target], depth[key] + 1)
            sub_in[target] -= 1
            if sub_in[target] <= 0 and target not in resolved:
                ready.append(target)
    leftover = [key for key in reachable if key not in resolved]
    if leftover:
        # Cyclus: diepte van de rest via BFS-niveaus, zodat we niet blijven hangen.
        warn(f"journeystructuur '{journey_name}': cyclus gevonden over {len(leftover)} knoop/knopen ({', '.join(leftover[:6])})")
        queue = [k for k in reachable if k in resolved] or [entry]
        seen_bfs = set(queue)
        steps = 0
        while queue and steps < limit:
            steps += 1
            key = queue.pop(0)
            for target in nodes[key]["next"]:
                if target in seen_bfs:
                    continue
                seen_bfs.add(target)
                depth[target] = depth[key] + 1
                queue.append(target)
    depth[entry] = 0

    out_nodes = []
    for key in sorted(reachable, key=lambda k: (depth[k], order.index(k))):
        node = nodes[key]
        node["depth"] = depth[key]
        out_nodes.append(node)
    return {"entry": entry, "nodes": out_nodes}


def fetch_journey_structures(session, auth, journeys, flow_names):
    """Haalt per journey in `flows` de live versie op (`extras=all`) voor het diagram.

    De lijst-endpoint wordt met `extras=activities` opgehaald en levert dus geen
    `outcomes`; zonder edges valt er geen diagram te tekenen. Daarom per journey
    een losse GET, alleen voor de journeys die ook echt in de flows voorkomen.
    """
    base = f"{auth.rest_url.rstrip('/')}/interaction/v1/interactions"
    structures = {}
    for journey in journeys:
        name = journey.get("name")
        journey_id = journey.get("id")
        if not name or not journey_id or name not in flow_names or name in structures:
            continue
        try:
            resp = session.get(
                base + f"/{journey_id}",
                headers={"Authorization": f"Bearer {auth.valid_token()}"},
                params={"extras": "all"},
                timeout=120,
            )
            if resp.status_code == 401:
                auth.refresh()
                resp = session.get(
                    base + f"/{journey_id}",
                    headers={"Authorization": f"Bearer {auth.valid_token()}"},
                    params={"extras": "all"},
                    timeout=120,
                )
            resp.raise_for_status()
            item = resp.json()
        except Exception as exc:
            warn(f"journeystructuur '{name}' niet opgehaald: {exc}")
            continue
        structure = build_structure(item, name)
        if structure is None:
            warn(f"journeystructuur '{name}': versie {item.get('version')} heeft geen activiteiten")
            continue
        structures[name] = structure
    print(f"  journeystructuren: {len(structures)} van {len(flow_names)} flows")
    return structures


def soap_retrieve(session, auth, obj, properties, start=None, end=None, on_page=None):
    """Retrieve met verplichte ContinueRequest-paginering (2500 rijen per pagina).

    Zonder `on_page` komen alle rijen als lijst terug. Met `on_page` wordt elke
    pagina meteen doorgegeven en daarna vrijgegeven - dan is de returnwaarde
    alleen het aantal verwerkte rijen. Dat scheelt bij een retrieve over
    honderden dagen honderden megabytes.
    """
    props = "".join(f"<Properties>{escape(p)}</Properties>" for p in properties)
    tail = DATE_FILTER.format(start=escape(start), end=escape(end)) if start else ""
    rows = []
    seen = 0
    request_id = None
    for page in range(1, MAX_PAGES + 1):
        if request_id:
            tail = f"<ContinueRequest>{escape(request_id)}</ContinueRequest>"
        body = SOAP_ENVELOPE.format(
            soap_url=escape(auth.soap_url),
            token=escape(auth.valid_token()),
            obj=escape(obj),
            properties=props,
            tail=tail,
        )
        resp = session.post(
            auth.soap_url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "text/xml", "SOAPAction": "Retrieve"},
            timeout=300,
        )
        if resp.status_code == 401:
            auth.refresh()
            continue
        resp.raise_for_status()

        root = ET.fromstring(resp.content)
        status_el = root.find(".//p:OverallStatus", NS)
        status = status_el.text if status_el is not None else ""
        if "Error" in (status or ""):
            msg_el = root.find(".//p:StatusMessage", NS)
            raise RuntimeError(f"{obj} retrieve mislukt ({status}): {msg_el.text if msg_el is not None else '?'}")

        page_rows = []
        for result in root.findall(".//p:Results", NS):
            row = {}
            for prop in properties:
                # Properties met een punt (bv. `Email.ID`) komen genest terug.
                path = "/".join(f"p:{part}" for part in prop.split("."))
                el = result.find(path, NS)
                row[prop] = el.text if el is not None and el.text else ""
            page_rows.append(row)
        seen += len(page_rows)
        req_el = root.find(".//p:RequestID", NS)
        request_id = req_el.text if req_el is not None else None
        if on_page is not None:
            on_page(page_rows)
        else:
            rows.extend(page_rows)
        # De geparste XML en de ruwe rijen van deze pagina zijn nu klaar; bij
        # honderden pagina's scheelt expliciet vrijgeven veel geheugen.
        page_rows = None
        root = None
        print(f"    {obj} pagina {page}: {seen} rijen totaal ({status})", flush=True)
        if status != "MoreDataAvailable":
            return seen if on_page is not None else rows
        if not request_id:
            warn(f"{obj}: MoreDataAvailable zonder RequestID, stop na {seen} rijen")
            return seen if on_page is not None else rows
    warn(f"{obj}: paginalimiet ({MAX_PAGES}) bereikt na {seen} rijen")
    return seen if on_page is not None else rows


def fetch_tsd_map(session, auth, id_to_journey, email_to_journey, prefix_to_journey):
    """TSD ObjectID -> (journeynaam, tier). Gelaagd, betrouwbaarste laag eerst."""
    rows = soap_retrieve(
        session,
        auth,
        "TriggeredSendDefinition",
        ["ObjectID", "CustomerKey", "Name", "Description", "Email.ID"],
    )
    # Tier 1 staat los van de SOAP-retrieve: noemt een journey een ObjectID
    # direct, dan is dat genoeg, ook als de TSD niet meer retrievebaar is.
    tsd_map = {}
    tsd_names = {}
    for key, name in id_to_journey.items():
        if len(key) == 36 and key.count("-") == 4:
            tsd_map[key] = (name, 1)
    tiers = {1: len(tsd_map), 2: 0}
    for row in rows:
        object_id = (row.get("ObjectID") or "").strip().lower()
        if not object_id:
            continue
        key = (row.get("CustomerKey") or "").strip()
        name = (row.get("Name") or "").strip()
        email_id = (row.get("Email.ID") or "").strip()
        if name:
            tsd_names[object_id] = name

        # Tier 1: de journey noemde deze TSD bij ObjectID of CustomerKey.
        if object_id in tsd_map:
            continue
        label = id_to_journey.get(object_id) or (id_to_journey.get(key.lower()) if key else None)
        if label is not None:
            tsd_map[object_id] = (label, 1)
            tiers[1] += 1
            continue

        # Tier 2: brug via het e-mail-id van de TSD, anders via de naamprefix.
        label = email_to_journey.get(email_id) if email_id else None
        if label is None:
            for candidate in (name, row.get("Description") or ""):
                prefix = activity_prefix(candidate)
                if prefix and prefix in prefix_to_journey:
                    label = prefix_to_journey[prefix]
                    break
        if label is None:
            continue
        tsd_map[object_id] = (label, 2)
        tiers[2] += 1
    print(f"  TSD-map: {len(rows)} definities -> {len(tsd_map)} gekoppeld (tier1={tiers[1]}, tier2={tiers[2]})")
    return tsd_map, tsd_names


def flow_for(row, tsd_map, sendid_to_flow):
    """Geeft (journeynaam, tier, tsd-objectid). Tier 3 = niet toegewezen."""
    tsd = (row.get("TriggeredSendDefinitionObjectID") or "").strip().lower()
    if tsd:
        hit = tsd_map.get(tsd)
        if hit:
            return hit[0], hit[1], tsd
    send_id = (row.get("SendID") or "").strip()
    if send_id and send_id in sendid_to_flow:
        return sendid_to_flow[send_id][0], sendid_to_flow[send_id][1], tsd
    return UNASSIGNED, 3, tsd


def email_bucket():
    return {"sent": 0, "opens": set(), "clicks": set(), "hard": 0, "soft": 0,
            "unsubs": 0, "subs": set(), "first": "", "last": ""}


def merge_email_bucket(target, source):
    target["sent"] += source["sent"]
    target["opens"] |= source["opens"]
    target["clicks"] |= source["clicks"]
    target["hard"] += source["hard"]
    target["soft"] += source["soft"]
    target["unsubs"] += source["unsubs"]
    target["subs"] |= source["subs"]
    for field, pick in (("first", min), ("last", max)):
        if source[field]:
            target[field] = pick(target[field], source[field]) if target[field] else source[field]


def fold_late_engagement(per_tsd, names):
    """Vouwt TSD's zonder send in het venster samen met dezelfde e-mail die wel stuurde.

    Opens en kliks op sends van voor het venster komen binnen op de TSD van de
    journeyversie die toen draaide. Zonder samenvoegen levert dat lege regels op
    en telt de breakdown meer e-mails dan er verstuurd zijn; de aantallen moeten
    wel bewaard blijven, anders klopt de som met het journeytotaal niet meer.
    """
    target = {}
    for tsd, entry in per_tsd.items():
        if entry["sent"] <= 0:
            continue
        label = names.get(tsd, ("", ""))[0] or tsd
        if label not in target or entry["sent"] > per_tsd[target[label]]["sent"]:
            target[label] = tsd
    folded = {}
    for tsd, entry in per_tsd.items():
        label = names.get(tsd, ("", ""))[0] or tsd
        key = target.get(label, tsd) if entry["sent"] <= 0 else tsd
        if key == tsd:
            folded[tsd] = entry
        else:
            merge_email_bucket(folded.setdefault(key, email_bucket()), entry)
    return folded


def email_name(tsd, names):
    """De weergavenaam van een e-mail: journey-activiteitnaam > TSD-naam > id."""
    label, raw = names.get(tsd, ("", ""))
    return label or raw or tsd or UNASSIGNED


def window_dates(end_date, days):
    """Geeft (start, end) van het venster van `days` dagen dat op `end_date` eindigt."""
    end = datetime.strptime(end_date, "%Y-%m-%d")
    return (end - timedelta(days=days)).strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def flow_bucket():
    return {"sent": 0, "opens": set(), "clicks": set(), "hard": 0, "soft": 0,
            "unsubs": 0, "subs": set(), "per_tsd": {}}


class DayAggregator:
    """Telt alle events van een BU weg in dagbuckets op basis van de VERZENDDATUM.

    De SentEvents komen als eerste binnen en vullen `send_day`: een map van
    (SendID, SubscriberKey) naar de dag van de send. Elke open, klik, bounce en
    unsub wordt via die map in de bucket van zijn send gezet. Daardoor is elke
    teller optelbaar over dagen en kan het dashboard elk gewenst datumbereik
    sommeren. Engagement waarvan de send buiten de retrieve valt heeft geen
    bucket; die tellen we apart in `unattributed` in plaats van weg te gooien.
    """

    def __init__(self, market, tsd_map, tsd_names, period_end):
        self.market = market
        self.tsd_map = tsd_map
        self.tsd_names = tsd_names
        self.period_end = period_end
        # dag -> flow -> teller
        self.days = {}
        # (SendID, SubscriberKey) -> dag van de send
        self.send_day = {}
        # SendID -> TSD, zodat een open/klik altijd bij dezelfde e-mail landt als
        # de send; anders zou een (SubscriberKey, SendID)-paar over twee e-mails
        # gesplitst kunnen worden en telt de breakdown niet op.
        self.sendid_to_tsd = {}
        self.sendid_to_flow = {}
        self.tier_sent = {1: 0, 2: 0, 3: 0}
        self.unattributed = {}
        self.event_totals = {}
        # Bereik per rollend venster; niet optelbaar, dus voorberekend.
        self.reach_starts = {days: window_dates(period_end, days)[0] for days in REACH_PERIODS}
        self.reach = {days: set() for days in REACH_PERIODS}
        # Coverage: welke Lead-id's zijn in het coveragevenster geraakt, per journey.
        self.cov_start = window_dates(period_end, COVERAGE_DAYS)[0]
        self.cov_leads = set()
        self.cov_per_journey = {}
        # First touch: per flow (en per e-mail binnen die flow) de VROEGSTE
        # senddag per SubscriberKey. Omdat iedere abonnee precies een vroegste
        # dag heeft, is het aantal eerste aanrakingen per dag wel optelbaar over
        # een bereik - anders dan `subscribers`. `all` telt apart, over alle
        # flows samen: iemand kan op twee verschillende dagen nieuw zijn in twee
        # flows en telt dan bij beide flows maar een keer bij `all`.
        self.first_seen = {}
        self.first_seen_email = {}
        self.first_seen_all = {}
        # Hele-vensterprofiel per flow: sends en (via `first_seen`) unieke mensen.
        self.flow_sent = {}
        # dag -> flow -> {"flow": n, "emails": {naam: n}}, gevuld in `_first_touch`.
        self.first_touch = None

    def bucket(self, day, flow):
        return self.days.setdefault(day, {}).setdefault(flow, flow_bucket())

    def tsd_of(self, row):
        send_id = (row.get("SendID") or "").strip()
        return self.sendid_to_tsd.get(send_id) or (row.get("TriggeredSendDefinitionObjectID") or "").strip().lower()

    def add_sent(self, rows):
        self.event_totals["SentEvent"] = self.event_totals.get("SentEvent", 0) + len(rows)
        for row in rows:
            day = (row.get("EventDate") or "")[:10]
            send_id = (row.get("SendID") or "").strip()
            key = (row.get("SubscriberKey") or "").strip()
            tsd = (row.get("TriggeredSendDefinitionObjectID") or "").strip().lower()
            if not day:
                self.unattributed["SentEvent"] = self.unattributed.get("SentEvent", 0) + 1
                continue
            if send_id:
                self.sendid_to_tsd.setdefault(send_id, tsd)
                if send_id not in self.sendid_to_flow:
                    hit = self.tsd_map.get(tsd)
                    if hit:
                        self.sendid_to_flow[send_id] = hit
                self.send_day.setdefault((send_id, key), day)

            flow, tier, _ = flow_for(row, self.tsd_map, self.sendid_to_flow)
            self.tier_sent[tier] += 1
            entry = self.bucket(day, flow)
            entry["sent"] += 1
            mail = entry["per_tsd"].setdefault(self.tsd_of(row), email_bucket())
            mail["sent"] += 1
            mail["first"] = min(mail["first"], day) if mail["first"] else day
            mail["last"] = max(mail["last"], day) if mail["last"] else day
            self.flow_sent[flow] = self.flow_sent.get(flow, 0) + 1
            if key:
                entry["subs"].add(key)
                mail["subs"].add(key)
                # Vroegste senddag onthouden; pagina's komen niet op volgorde
                # binnen, dus we vergelijken in plaats van alleen te zetten.
                for store, store_key in (
                    (self.first_seen.setdefault(flow, {}), key),
                    (self.first_seen_all, key),
                    (self.first_seen_email.setdefault(
                        (flow, email_name(self.tsd_of(row), self.tsd_names)), {}), key),
                    (self.first_seen_email.setdefault(
                        ("all", email_name(self.tsd_of(row), self.tsd_names)), {}), key),
                ):
                    known = store.get(store_key)
                    if known is None or day < known:
                        store[store_key] = day
                for days, start in self.reach_starts.items():
                    if day >= start:
                        self.reach[days].add(key)
                if key.startswith("00Q") and day >= self.cov_start:
                    self.cov_leads.add(key)
                    self.cov_per_journey.setdefault(flow, set()).add(key)

    def add_engagement(self, obj, rows):
        """Plaatst opens/kliks/bounces/unsubs in de dagbucket van HUN SEND."""
        self.event_totals[obj] = self.event_totals.get(obj, 0) + len(rows)
        for row in rows:
            send_id = (row.get("SendID") or "").strip()
            key = (row.get("SubscriberKey") or "").strip()
            day = self.send_day.get((send_id, key))
            if day is None:
                # De send van dit event valt buiten de retrieve: geen dagbucket.
                self.unattributed[obj] = self.unattributed.get(obj, 0) + 1
                continue
            flow = flow_for(row, self.tsd_map, self.sendid_to_flow)[0]
            entry = self.bucket(day, flow)
            mail = entry["per_tsd"].setdefault(self.tsd_of(row), email_bucket())
            if obj == "OpenEvent":
                pair = (key, send_id)
                entry["opens"].add(pair)
                mail["opens"].add(pair)
            elif obj == "ClickEvent":
                pair = (key, send_id)
                entry["clicks"].add(pair)
                mail["clicks"].add(pair)
            elif obj == "BounceEvent":
                field = "hard" if "hard" in (row.get("BounceCategory") or "").lower() else "soft"
                entry[field] += 1
                mail[field] += 1
            elif obj == "UnsubEvent":
                entry["unsubs"] += 1
                mail["unsubs"] += 1

    def feed(self, obj, rows):
        if obj == "SentEvent":
            self.add_sent(rows)
        else:
            self.add_engagement(obj, rows)

    # -- uitvoer ---------------------------------------------------------

    def _counters(self, entry, first_touch=0):
        sent = entry["sent"]
        hard = entry["hard"]
        return {
            "sent": sent,
            "delivered": max(sent - hard, 0),
            "opens": len(entry["opens"]),
            "clicks": len(entry["clicks"]),
            "bounces": hard,
            "soft_bounces": entry["soft"],
            "unsubs": entry["unsubs"],
            # NIET optelbaar over dagen: iemand kan op twee dagen gemaild zijn.
            "subscribers": len(entry["subs"]),
            # WEL optelbaar: mensen die op deze dag voor het eerst (binnen de
            # retrieve) post uit deze flow kregen.
            "firstTouch": first_touch,
        }

    def _emails(self, entry, first_touch=None):
        """Per-e-mail regels, samengevoegd op weergavenaam zodat versies niet splitsen."""
        first_touch = first_touch or {}
        folded = fold_late_engagement(entry["per_tsd"], self.tsd_names)
        by_name = {}
        for tsd, mail in folded.items():
            merge_email_bucket(by_name.setdefault(email_name(tsd, self.tsd_names), email_bucket()), mail)
        return {name: self._counters(mail, first_touch.get(name, 0))
                for name, mail in sorted(by_name.items())}

    def _build_first_touch(self):
        """Zet de vroegste-senddag-maps om in tellers per dag, flow en e-mail.

        Dit kan pas als de hele SentEvent-pass klaar is: bij het streamen van
        pagina's is nog niet bekend of er verderop een vroegere send komt.
        """
        out = {}

        def add(day, flow, email, amount=1):
            slot = out.setdefault(day, {}).setdefault(flow, {"flow": 0, "emails": {}})
            if email is None:
                slot["flow"] += amount
            else:
                slot["emails"][email] = slot["emails"].get(email, 0) + amount

        for flow, per_key in self.first_seen.items():
            for day in per_key.values():
                add(day, flow, None)
        for day in self.first_seen_all.values():
            add(day, "all", None)
        for (flow, email), per_key in self.first_seen_email.items():
            for day in per_key.values():
                add(day, flow, email)
        self.first_touch = out

    def flow_profile(self):
        """Per flow over het HELE opgehaalde venster: mensen, sends, mails per persoon."""
        out = {}
        for flow, per_key in list(self.first_seen.items()) + [("all", self.first_seen_all)]:
            subs = len(per_key)
            sent = self.flow_sent.get(flow, 0) if flow != "all" else sum(self.flow_sent.values())
            out[flow] = {
                "subscribers": subs,
                "sent": sent,
                "mailsPerPerson": round(sent / subs, 1) if subs else None,
            }
        return out

    def payload(self):
        if self.first_touch is None:
            self._build_first_touch()
        out_days = {}
        for day in sorted(self.days):
            flows = self.days[day]
            total = flow_bucket()
            for entry in flows.values():
                total["sent"] += entry["sent"]
                total["opens"] |= entry["opens"]
                total["clicks"] |= entry["clicks"]
                total["hard"] += entry["hard"]
                total["soft"] += entry["soft"]
                total["unsubs"] += entry["unsubs"]
                total["subs"] |= entry["subs"]
                for tsd, mail in entry["per_tsd"].items():
                    merge_email_bucket(total["per_tsd"].setdefault(tsd, email_bucket()), mail)
            ft_day = self.first_touch.get(day, {})
            out_flows = {}
            for flow, entry in sorted(list(flows.items()) + [("all", total)]):
                ft = ft_day.get(flow) or {"flow": 0, "emails": {}}
                row = self._counters(entry, ft["flow"])
                row["emails"] = self._emails(entry, ft["emails"])
                out_flows[flow] = row
                self._check(day, flow, row)
            out_days[day] = {"flows": out_flows}
        return out_days

    def _check(self, day, flow, row):
        """Waarschuwt als de som van de e-mails niet gelijk is aan het flowtotaal."""
        for field in ("sent", "delivered", "opens", "clicks", "bounces", "soft_bounces", "unsubs"):
            summed = sum(mail[field] for mail in row["emails"].values())
            if summed != row[field]:
                warn(f"{self.market} {day} '{flow}': e-mails tellen niet op, {field} {summed} != {row[field]}")

    def reach_payload(self):
        out = {}
        for days in REACH_PERIODS:
            keys = self.reach[days]
            leads = sum(1 for k in keys if k.startswith("00Q"))
            contacts = sum(1 for k in keys if k.startswith("003"))
            out[str(days)] = {
                # SubscriberKey is een Salesforce-id: 00Q = Lead, 003 = Contact.
                # Beide krijgen mail. Coverage kan alleen op de Lead-helft
                # berekend worden, want het consent-veld is alleen op Lead leesbaar.
                "total": len(keys),
                "leads": leads,
                "contacts": contacts,
                "other": len(keys) - leads - contacts,
            }
        return out

    def flow_names(self):
        names = set()
        for flows in self.days.values():
            names |= {f for f in flows if f not in ("all", UNASSIGNED)}
        return names

    def report_tiers(self):
        sent_total = sum(self.tier_sent.values()) or 1
        warn(
            f"{self.market.upper()}: attributie van {sum(self.tier_sent.values())} sends - tier1 "
            f"{self.tier_sent[1]} ({self.tier_sent[1] / sent_total * 100:.1f}%), tier2 "
            f"{self.tier_sent[2]} ({self.tier_sent[2] / sent_total * 100:.1f}%), {UNASSIGNED} "
            f"{self.tier_sent[3]} ({self.tier_sent[3] / sent_total * 100:.1f}%)"
        )


def retrieve_and_aggregate(session, auth, agg, start, end):
    """Haalt elk tracking object op en verwerkt het per pagina in de dagbuckets.

    Per pagina aggregeren houdt het geheugengebruik vlak: de ruwe rijen worden
    meteen weer vrijgegeven. SentEvent staat bewust vooraan, want de map van
    (SendID, SubscriberKey) naar verzenddag moet gevuld zijn voordat de
    engagement-events geplaatst kunnen worden.
    """
    for obj, properties in TRACKING_OBJECTS:
        print(f"  {obj} ophalen...", flush=True)
        started = time.time()
        state = {"pages": 0}

        def on_page(rows, obj=obj, state=state):
            state["pages"] += 1
            agg.feed(obj, rows)

        try:
            count = soap_retrieve(session, auth, obj, properties, start, end, on_page=on_page)
        except RuntimeError as exc:
            if "TriggeredSendDefinitionObjectID" not in properties or state["pages"]:
                # Al verwerkte pagina's kunnen we niet terugdraaien; opnieuw
                # ophalen zou dubbel tellen.
                raise
            warn(f"{obj}: retrieve met TriggeredSendDefinitionObjectID mislukt ({exc}); opnieuw zonder die property, attributie via SendID")
            fallback = [p for p in properties if p != "TriggeredSendDefinitionObjectID"]
            count = soap_retrieve(session, auth, obj, fallback, start, end, on_page=on_page)
        print(f"  {obj}: {count} events verwerkt in {time.time() - started:.0f}s", flush=True)


def main():
    subdomain = os.environ["SFMC_SUBDOMAIN"]
    client_id = os.environ["SFMC_CLIENT_ID"]
    client_secret = os.environ["SFMC_CLIENT_SECRET"]

    # Einddatum waar alles vanaf terugrekent, exclusief. Default morgen UTC,
    # zodat de dag van vandaag volledig meetelt.
    period_end = os.environ.get("PERIOD_END") or (
        datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y-%m-%d")
    retrieve_start, _ = window_dates(period_end, RETRIEVE_DAYS)
    cov_window_start, _ = window_dates(period_end, COVERAGE_DAYS)

    session = make_session()
    markets = {}
    journeys_out = {}
    structures_out = {}
    reached_lead_ids = {}
    first_day = None

    for market in MARKETS:
        raw_mid = (os.environ.get(f"SFMC_MID_{market.upper()}") or "").strip()
        if not raw_mid:
            warn(f"BU {market.upper()} overgeslagen: SFMC_MID_{market.upper()} niet gezet")
            continue
        try:
            mid = int(raw_mid)
        except ValueError:
            warn(f"BU {market.upper()} overgeslagen: SFMC_MID_{market.upper()} is geen getal")
            continue

        print(f"[{market}] MID {mid}")
        try:
            auth = Auth(session, subdomain, client_id, client_secret, mid)
            journeys, id_to_journey, email_to_journey, prefix_to_journey, id_to_email_name = fetch_journeys(session, auth)
            tsd_map, raw_names = fetch_tsd_map(session, auth, id_to_journey, email_to_journey, prefix_to_journey)
            # De naam uit de journey-definitie gaat voor op de (afgekapte) TSD-naam.
            tsd_names = {tsd: (strip_hex_suffix(name), name) for tsd, name in raw_names.items()}
            for tsd, name in id_to_email_name.items():
                if name:
                    tsd_names[tsd] = (name, raw_names.get(tsd, ""))

            # EEN retrieve per BU over de volledige span; de dagbuckets worden
            # al tijdens het ophalen gevuld.
            print(f"  retrieve {retrieve_start} t/m {period_end} ({RETRIEVE_DAYS} dagen)")
            started = time.time()
            agg = DayAggregator(market, tsd_map, tsd_names, period_end)
            retrieve_and_aggregate(
                session, auth, agg,
                f"{retrieve_start}T00:00:00", f"{period_end}T00:00:00")
            print(f"  retrieve + aggregatie klaar in {time.time() - started:.0f}s")
            agg.report_tiers()

            days_out = agg.payload()
            flow_names = agg.flow_names()

            # Structuur (het stroomschema) van de journeys die in de dagbuckets
            # voorkomen, zodat het dashboard metrics aan het diagram kan koppelen.
            structures = fetch_journey_structures(
                session, auth, journeys, flow_names)

            reached_lead_ids[market] = {
                "all": sorted(agg.cov_leads),
                "by_journey": {k: sorted(v) for k, v in agg.cov_per_journey.items()},
            }
            print(f"  coverage: {len(agg.cov_leads)} unieke leads over {len(agg.cov_per_journey)} journeys"
                  f" in {COVERAGE_DAYS} dagen")

            unattributed = {obj: agg.unattributed.get(obj, 0) for obj, _ in TRACKING_OBJECTS}
            event_totals = {obj: agg.event_totals.get(obj, 0) for obj, _ in TRACKING_OBJECTS}
            reach = agg.reach_payload()
            markets[market] = {
                "days": days_out,
                # engagement waarvan de send buiten de retrieve viel; bewust
                # geteld in plaats van weggegooid
                "unattributed": unattributed,
                "eventTotals": event_totals,
                # bereik is niet optelbaar; alleen voor deze drie presets
                "reach": reach,
                # hoe een flow ONTWORPEN is: over het hele opgehaalde venster
                "flowProfile": agg.flow_profile(),
            }
            if days_out:
                day0 = min(days_out)
                first_day = day0 if first_day is None else min(first_day, day0)
            agg = None
        except Exception as exc:
            warn(f"BU {market.upper()} mislukt: {exc}")
            continue

        journeys_out[market] = journeys
        structures_out[market] = structures

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        # de datum waar alles vanaf terugrekent (exclusief), de vroegste dag met
        # een bucket en hoe ver de retrieve terugkeek
        "endDate": period_end,
        "firstDay": first_day,
        "retrieveDays": RETRIEVE_DAYS,
        # de rollende vensters waarvoor `reach` voorberekend is
        "reachPeriods": [str(d) for d in REACH_PERIODS],
        "markets": markets,
        "journeys": journeys_out,
        # stroomschema per journey (knopen + edges) voor de diagramweergave
        "journeyStructures": structures_out,
        "warnings": WARNINGS,
    }

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), OUT_PATH)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    reached_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), REACHED_PATH)
    with open(reached_path, "w", encoding="utf-8") as f:
        json.dump(
            {"coverage_days": COVERAGE_DAYS,
             "period": {"start": cov_window_start, "end": period_end},
             "markets": reached_lead_ids},
            f)
    print(f"Bereikte Lead-id's (niet gecommit) naar {REACHED_PATH}: "
          + ", ".join(f"{m}={len(v['all'])}" for m, v in reached_lead_ids.items()))

    print(f"\nGeschreven naar {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB)")
    for market, data in sorted(markets.items()):
        days = data["days"]
        print(f"  {market}: {len(days)} dagen, {min(days) if days else '-'} t/m "
              f"{max(days) if days else '-'}, bereik30={data['reach']['30']['total']}")
        for label, count in sorted(data["unattributed"].items()):
            total = data["eventTotals"].get(label) or 0
            if count:
                print(f"    unattributed {label}: {count}"
                      + (f" ({count / total * 100:.2f}%)" if total else ""))
    if WARNINGS:
        print(f"  {len(WARNINGS)} waarschuwing(en)")


if __name__ == "__main__":
    main()
