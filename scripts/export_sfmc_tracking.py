"""Export van e-mail tracking uit Salesforce Marketing Cloud per Business Unit.

Haalt per BU (markt) de tracking events op via de SOAP API (SentEvent, OpenEvent,
ClickEvent, BounceEvent, UnsubEvent) en de journey-definities via de REST API, en
schrijft alles geaggregeerd per flow naar `exports/sfmc_tracking.json`.

Gebruik:

    PERIOD_START=2026-08-20 PERIOD_END=2026-09-09 python3 scripts/export_sfmc_tracking.py

Environment variables:

- `SFMC_SUBDOMAIN`, `SFMC_CLIENT_ID`, `SFMC_CLIENT_SECRET` - credentials van het
  (read-only) installed package.
- `SFMC_MID_NL`, `SFMC_MID_BE`, en optioneel `SFMC_MID_FR`, `SFMC_MID_IT` - de MID
  (account_id) per BU. Een BU zonder MID wordt overgeslagen met een waarschuwing;
  FR en IT bestaan nog niet.
- `PERIOD_START` (default `2026-08-01`) en `PERIOD_END` (default `2026-09-09`),
  beide `YYYY-MM-DD`, einddatum exclusief.
- `PREV_START` / `PREV_END` - optioneel. Zijn ze gezet, dan wordt de hele
  aggregatie twee keer gedraaid zodat verschillen per periode berekend kunnen
  worden.

Let op: dit is een grote retrieve, reken op enkele minuten per BU.
"""

import json
import os
import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MARKETS = ["nl", "be", "fr", "it"]
OUT_PATH = "exports/sfmc_tracking.json"
NS = {"p": "http://exacttarget.com/wsdl/partnerAPI"}
UNASSIGNED = "(niet toegewezen)"
TOKEN_TTL = 15 * 60
MAX_PAGES = 400

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
    page = 1
    while page <= 200:
        url = f"{auth.rest_url.rstrip('/')}/interaction/v1/interactions"
        resp = session.get(
            url,
            headers={"Authorization": f"Bearer {auth.valid_token()}"},
            params={
                "extras": "activities",
                "mostRecentVersionOnly": "false",
                "$pageSize": 50,
                "$page": page,
            },
            timeout=120,
        )
        if resp.status_code == 401:
            auth.refresh()
            continue
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items") or []
        for item in items:
            # Alle versies van een journey vallen onder een label: de naam zelf.
            name = item.get("name") or UNASSIGNED
            version = item.get("version") or 0
            current = seen_journeys.get(item.get("id"))
            if current is None or (version or 0) >= (current.get("version") or 0):
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
                ts = args.get("triggeredSend")
                if not isinstance(ts, dict):
                    continue
                # Tier 1: directe TSD ObjectID en CustomerKey.
                for key in ("objectId", "objectID", "id", "key", "customerKey"):
                    value = ts.get(key)
                    if isinstance(value, str) and value.strip():
                        id_to_journey.setdefault(value.strip().lower(), name)
                # Tier 2: emailId, later gebrugd via de TSD-retrieve.
                email_id = ts.get("emailId")
                if email_id is not None and str(email_id).strip():
                    email_to_journey.setdefault(str(email_id).strip(), name)
                # Tier 2-fallback: naam zonder hex-suffix.
                for value in (ts.get("name"), activity.get("name")):
                    prefix = activity_prefix(value)
                    if prefix:
                        prefix_to_journey.setdefault(prefix, name)

        count = payload.get("count")
        page_size = payload.get("pageSize") or 50
        if not items or count is None or page * page_size >= count:
            break
        page += 1

    journeys = sorted(seen_journeys.values(), key=lambda j: (j["name"], j["id"] or ""))
    print(
        f"  journeys: {len(journeys)} (alle versies), tier1-sleutels: {len(id_to_journey)}, "
        f"emailId: {len(email_to_journey)}, naam-prefix: {len(prefix_to_journey)}"
    )
    return journeys, id_to_journey, email_to_journey, prefix_to_journey


def soap_retrieve(session, auth, obj, properties, start=None, end=None):
    """Retrieve met verplichte ContinueRequest-paginering (2500 rijen per pagina)."""
    props = "".join(f"<Properties>{escape(p)}</Properties>" for p in properties)
    tail = DATE_FILTER.format(start=escape(start), end=escape(end)) if start else ""
    rows = []
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

        for result in root.findall(".//p:Results", NS):
            row = {}
            for prop in properties:
                # Properties met een punt (bv. `Email.ID`) komen genest terug.
                path = "/".join(f"p:{part}" for part in prop.split("."))
                el = result.find(path, NS)
                row[prop] = el.text if el is not None and el.text else ""
            rows.append(row)

        req_el = root.find(".//p:RequestID", NS)
        request_id = req_el.text if req_el is not None else None
        print(f"    {obj} pagina {page}: {len(rows)} rijen totaal ({status})")
        if status != "MoreDataAvailable":
            return rows
        if not request_id:
            warn(f"{obj}: MoreDataAvailable zonder RequestID, stop na {len(rows)} rijen")
            return rows
    warn(f"{obj}: paginalimiet ({MAX_PAGES}) bereikt na {len(rows)} rijen")
    return rows


def retrieve_events(session, auth, start, end):
    """Haalt alle tracking objects op; valt terug op SendID-only bij een TSD-fout."""
    events = {}
    for obj, properties in TRACKING_OBJECTS:
        print(f"  {obj} ophalen...")
        try:
            events[obj] = soap_retrieve(session, auth, obj, properties, start, end)
        except RuntimeError as exc:
            if "TriggeredSendDefinitionObjectID" not in properties:
                raise
            warn(f"{obj}: retrieve met TriggeredSendDefinitionObjectID mislukt ({exc}); opnieuw zonder die property, attributie via SendID")
            fallback = [p for p in properties if p != "TriggeredSendDefinitionObjectID"]
            events[obj] = soap_retrieve(session, auth, obj, fallback, start, end)
    return events


def fetch_tsd_map(session, auth, id_to_journey, email_to_journey, prefix_to_journey):
    """TSD ObjectID -> (journeynaam, tier). Gelaagd, betrouwbaarste laag eerst."""
    rows = soap_retrieve(
        session,
        auth,
        "TriggeredSendDefinition",
        ["ObjectID", "CustomerKey", "Name", "Description", "Email.ID"],
    )
    tsd_map = {}
    tiers = {1: 0, 2: 0}
    for row in rows:
        object_id = (row.get("ObjectID") or "").strip().lower()
        if not object_id:
            continue
        key = (row.get("CustomerKey") or "").strip()
        name = (row.get("Name") or "").strip()
        email_id = (row.get("Email.ID") or "").strip()

        # Tier 1: de journey noemde deze TSD bij ObjectID of CustomerKey.
        label = id_to_journey.get(object_id) or (id_to_journey.get(key.lower()) if key else None)
        tier = 1
        if label is None:
            # Tier 2: brug via het e-mail-id van de TSD.
            tier = 2
            if email_id:
                label = email_to_journey.get(email_id)
            if label is None:
                for candidate in (name, (row.get("Description") or "")):
                    prefix = activity_prefix(candidate)
                    if prefix and prefix in prefix_to_journey:
                        label = prefix_to_journey[prefix]
                        break
        if label is None:
            continue
        tsd_map[object_id] = (label, tier)
        tiers[tier] += 1
    print(f"  TSD-map: {len(rows)} definities -> {len(tsd_map)} gekoppeld (tier1={tiers[1]}, tier2={tiers[2]})")
    return tsd_map


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


def rate(numerator, denominator):
    if not denominator:
        return None
    return round(numerator / denominator * 100, 2)


def aggregate(events, tsd_map, window_label):
    sendid_to_flow = {}
    for row in events.get("SentEvent", []):
        send_id = (row.get("SendID") or "").strip()
        tsd = (row.get("TriggeredSendDefinitionObjectID") or "").strip().lower()
        if not send_id or send_id in sendid_to_flow or not tsd:
            continue
        hit = tsd_map.get(tsd)
        if hit:
            sendid_to_flow[send_id] = hit

    counters = {}
    tier_sent = {1: 0, 2: 0, 3: 0}

    def bucket(flow):
        return counters.setdefault(
            flow,
            {"sent": 0, "opens": set(), "clicks": set(), "hard": 0, "soft": 0, "unsubs": 0, "tsds": set()},
        )

    def label_of(row):
        return flow_for(row, tsd_map, sendid_to_flow)[0]

    for row in events.get("SentEvent", []):
        flow, tier, tsd = flow_for(row, tsd_map, sendid_to_flow)
        entry = bucket(flow)
        entry["sent"] += 1
        if tsd:
            entry["tsds"].add(tsd)
        tier_sent[tier] += 1
    for row in events.get("OpenEvent", []):
        bucket(label_of(row))["opens"].add((row.get("SubscriberKey", ""), row.get("SendID", "")))
    for row in events.get("ClickEvent", []):
        bucket(label_of(row))["clicks"].add((row.get("SubscriberKey", ""), row.get("SendID", "")))
    for row in events.get("BounceEvent", []):
        entry = bucket(label_of(row))
        if "hard" in (row.get("BounceCategory") or "").lower():
            entry["hard"] += 1
        else:
            entry["soft"] += 1
    for row in events.get("UnsubEvent", []):
        bucket(label_of(row))["unsubs"] += 1

    total = {"sent": 0, "opens": set(), "clicks": set(), "hard": 0, "soft": 0, "unsubs": 0, "tsds": set()}
    for entry in counters.values():
        total["sent"] += entry["sent"]
        total["opens"] |= entry["opens"]
        total["clicks"] |= entry["clicks"]
        total["hard"] += entry["hard"]
        total["soft"] += entry["soft"]
        total["unsubs"] += entry["unsubs"]
        total["tsds"] |= entry["tsds"]

    sent_total = total["sent"] or 1
    warn(
        f"{window_label}: attributie van {total['sent']} sends - tier1 "
        f"{tier_sent[1]} ({tier_sent[1] / sent_total * 100:.1f}%), tier2 "
        f"{tier_sent[2]} ({tier_sent[2] / sent_total * 100:.1f}%), {UNASSIGNED} "
        f"{tier_sent[3]} ({tier_sent[3] / sent_total * 100:.1f}%)"
    )

    flows = {}
    for flow, entry in list(counters.items()) + [("all", total)]:
        sent = entry["sent"]
        hard = entry["hard"]
        delivered = max(sent - hard, 0)
        opens = len(entry["opens"])
        clicks = len(entry["clicks"])
        flows[flow] = {
            "label": "Alle flows" if flow == "all" else flow,
            "sent": sent,
            "delivered": delivered,
            "opens": opens,
            "clicks": clicks,
            "bounces": hard,
            "soft_bounces": entry["soft"],
            "unsubs": entry["unsubs"],
            # aantal losse triggered sends (e-mails) dat onder deze journey valt
            "emails": len(entry["tsds"]),
            "delivery": rate(delivered, sent),
            "open": rate(opens, delivered),
            "ctr": rate(clicks, delivered),
            "ctor": rate(clicks, opens),
            "unsub": rate(entry["unsubs"], delivered),
            "bounce": rate(hard, sent),
            # spam blijft null: ComplaintEvent wordt niet opgehaald, 0 zou misleiden.
            "spam": None,
        }
    return flows


def run_window(session, auth, tsd_map, market, start, end):
    print(f"  venster {start} t/m {end} (exclusief)")
    events = retrieve_events(session, auth, f"{start}T00:00:00", f"{end}T00:00:00")
    return aggregate(events, tsd_map, f"{market.upper()} {start}..{end}")


def main():
    subdomain = os.environ["SFMC_SUBDOMAIN"]
    client_id = os.environ["SFMC_CLIENT_ID"]
    client_secret = os.environ["SFMC_CLIENT_SECRET"]

    period_start = os.environ.get("PERIOD_START") or "2026-08-01"
    period_end = os.environ.get("PERIOD_END") or "2026-09-09"
    prev_start = os.environ.get("PREV_START") or ""
    prev_end = os.environ.get("PREV_END") or ""
    prev_period = {"start": prev_start, "end": prev_end} if prev_start and prev_end else None

    session = make_session()
    markets = {}
    journeys_out = {}

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
            journeys, id_to_journey, email_to_journey, prefix_to_journey = fetch_journeys(session, auth)
            tsd_map = fetch_tsd_map(session, auth, id_to_journey, email_to_journey, prefix_to_journey)
            flows = run_window(session, auth, tsd_map, market, period_start, period_end)
            prev_flows = None
            if prev_period:
                prev_flows = run_window(session, auth, tsd_map, market, prev_start, prev_end)
        except Exception as exc:
            warn(f"BU {market.upper()} mislukt: {exc}")
            continue

        journeys_out[market] = journeys
        markets[market] = {"flows": flows, "prev_flows": prev_flows}

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "period": {"start": period_start, "end": period_end},
        "prev_period": prev_period,
        "markets": markets,
        "journeys": journeys_out,
        "warnings": WARNINGS,
    }

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), OUT_PATH)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print(f"\nGeschreven naar {out_path}")
    for market, data in sorted(markets.items()):
        for label, flows in (("huidig", data["flows"]), ("vorig", data["prev_flows"])):
            if not flows:
                continue
            a = flows["all"]
            print(
                f"  {market} {label}: sent={a['sent']} delivered={a['delivered']} "
                f"open={a['open']}% ctr={a['ctr']}% ctor={a['ctor']}%"
            )
    if WARNINGS:
        print(f"  {len(WARNINGS)} waarschuwing(en)")


if __name__ == "__main__":
    main()
