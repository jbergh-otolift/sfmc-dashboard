# sfmc-dashboard

Het Otolift Marketing Automation Dashboard, gevoed met echte data uit
Salesforce Marketing Cloud en Salesforce CRM.

> ⚠️ **Deze repo is publiek.** Zie [Zichtbaarheid](#zichtbaarheid) — er staat
> bedrijfsdata in (Lead-ID's, journeynamen, e-mailvolumes). Zet de repo op
> private voordat je hier verder op bouwt.

## Architectuur

Geen enkele API-call vanuit de browser: secrets blijven server-side in GitHub
Actions, het dashboard is puur statisch.

```
GitHub Actions (secrets veilig, server-side)
  ├─ scripts/export_salesforce_report.py  → exports/report.csv          (LeadHistory, Bulk API 2.0)
  ├─ scripts/export_sfmc_tracking.py      → exports/sfmc_tracking.json  (SOAP tracking events per BU)
  ├─ scripts/build_crm_metrics.py         → exports/crm_metrics.json    (funnel uit report.csv)
  └─ scripts/build_data_json.py           → docs/data.json              (samengevoegd contract)

GitHub Pages (statisch)
  docs/index.html + docs/data-layer.js  →  fetch('data.json')
```

## Wat is live en wat niet

| Dashboardsectie | Bron | Status |
|---|---|---|
| Email Health | SFMC `SentEvent`/`OpenEvent`/`ClickEvent`/`BounceEvent`/`UnsubEvent` per journey, per BU | ✅ Live, per markt |
| Her-activatie funnel | Salesforce `LeadHistory` op `Lead.Status` | ✅ Live, markt-overstijgend |
| Lead funnel (MQL/SQL/R1) | Salesforce `LeadHistory` | ✅ Live, markt-overstijgend |
| Order-stap | Opportunity | ❌ Niet ontsloten |
| Automation coverage, database-groei | CRM consent-velden (`Customized_Product_Advice__c` + `HasOptedOutOfEmail`) | ❌ Niet in de export |
| CPA / CPQL / CPL, werving per kanaal | Advertentiekosten + kanaal-attributie | ❌ Geen bron bepaald |
| Spam complaint rate | SFMC `ComplaintEvent` | ❌ Niet opgevraagd |

Velden zonder bron staan in het dashboard op `–` met de class `.nodata`, nooit
op nul. Elke sectie heeft een badge (`Live data` / `Deels aangesloten` /
`Niet aangesloten`) zodat een leeg veld niet voor een meting doorgaat.

### Bekende beperkingen

- **De CRM-cijfers zijn markt-overstijgend.** `exports/report.csv` bevat geen
  `RecordTypeId`, dus de markt van een Lead is er niet uit af te leiden.
  Her-activatie en lead funnel veranderen daarom niet als je in het dashboard
  van land wisselt; Email Health is wél echt per markt (per Business Unit
  opgehaald). Voeg `RecordTypeId` toe aan de `SOQL_QUERY` in
  `export_salesforce_report.py` om dit per markt te kunnen splitsen — de
  RecordTypeId's per markt staan in het stappenplan.
- **De lead funnel is geen strikte keten.** Uit de transitiedata blijkt dat
  `Appointment` vooral direct uit `New` en `Not reached` komt, niet via
  `Re-entered`. Elke fase is daarom een *aandeel van MQL*, geen opeenvolgende
  conversie. R1 als percentage van SQL levert onzin op (>100%).
- **Open rate is sterk MPP-vertekend** (~69%). Sinds Apple Mail Privacy
  Protection worden opens automatisch geregistreerd. CTOR is de leidende KPI.
- **Flow-labels komen meestal van de TriggeredSendDefinition, niet van de
  journey.** De `triggeredSend`-config in de interactions-payload draagt
  per-versie keys die niet matchen met de live TSD CustomerKeys, dus de
  journeynaam-join raakt maar een handvol flows. De TSD-namen zijn 1-op-1 met
  de journey-e-mails en goed leesbaar, maar SFMC kapt ze af op 30 tekens
  ("NL - Bevestiging service afsp"). Flows onder 50 sends krijgen geen eigen
  chip (`MIN_SENT_FOR_CHIP`); ze tellen wel mee in "Alle flows".
- **FR en IT staan leeg.** Geen Lead-RecordType in het CRM en geen eigen MID in
  Marketing Cloud. Zodra die er zijn: `SFMC_MID_FR`/`SFMC_MID_IT` als secret
  toevoegen, de rest werkt dan automatisch mee.

## Periode

De workflow gebruikt standaard een rollend venster van 19 dagen, vergeleken met
de 19 dagen daarvoor — twee gelijke helften, dus een eerlijke period-over-period
vergelijking. Handmatig een andere periode draaien kan via de Actions-tab
("Run workflow") met `period_start`/`period_end`/`prev_start`/`prev_end`.

De huidige `docs/data.json` dekt **2026-08-20 t/m 2026-09-08**, vergeleken met
**2026-08-01 t/m 2026-08-19** — de twee helften van het venster dat de
Salesforce-export op dit moment bestrijkt.

## Setup

### 1. GitHub secrets

Settings → Secrets and variables → Actions:

| Secret | Waarvoor |
|---|---|
| `SF_DOMAIN`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET` | Salesforce CRM (bestond al) |
| `SFMC_SUBDOMAIN`, `SFMC_CLIENT_ID`, `SFMC_CLIENT_SECRET` | Marketing Cloud Installed Package |
| `SFMC_MID_NL`, `SFMC_MID_BE` | Business Unit ID's (MID) per markt |
| `SFMC_MID_FR`, `SFMC_MID_IT` | Optioneel; ontbrekende BU's worden overgeslagen |

De SFMC-waarden staan lokaal in `Mail Library/Dashboard/bu_config.json`. Dat
bestand staat in `.gitignore` en hoort daar te blijven.

Eén Installed Package op de parent-BU met multi-business-unit scope volstaat
voor alle markten; per BU wordt alleen de MID meegegeven bij het ophalen van het
token. Benodigde scopes (read-only): `tracking_events_read`, `journeys_read`,
`email_read`.

### 2. Salesforce Connected App

Zie de instructies onder [Salesforce Connected App](#salesforce-connected-app-external-client-app)
hieronder — die setup was er al voor de LeadHistory-export.

### 3. GitHub Pages

Settings → Pages → Source: **Deploy from a branch**, branch `main`, map
`/docs`. Het dashboard staat daarna op
`https://jbergh-otolift.github.io/sfmc-dashboard/`.

> Op een publieke repo is die pagina wereldwijd leesbaar. Zie
> [Zichtbaarheid](#zichtbaarheid).

## Lokaal draaien

```sh
pip install requests

export SFMC_SUBDOMAIN=... SFMC_CLIENT_ID=... SFMC_CLIENT_SECRET=...
export SFMC_MID_NL=... SFMC_MID_BE=...
export PERIOD_START=2026-08-20 PERIOD_END=2026-09-09
export PREV_START=2026-08-01   PREV_END=2026-08-20

python scripts/export_sfmc_tracking.py
python scripts/build_crm_metrics.py
python scripts/build_data_json.py

python -m http.server 8000 --directory docs   # → http://localhost:8000
```

`build_crm_metrics.py` en `build_data_json.py` hebben geen credentials nodig;
die werken op de al gecommitte exports.

## Zichtbaarheid

Deze repo staat op **public**. Daarmee is publiek leesbaar:

- `exports/report.csv` — 8.000+ Salesforce Lead-ID's met hun volledige
  statusverloop (stond er al vóór deze wijziging)
- `exports/sfmc_tracking.json` en `docs/data.json` — journeynamen,
  e-mailvolumes, bounce- en unsubscribe-cijfers per flow
- de GitHub Pages-site zelf, als die aan staat

Er staan **geen credentials** in de repo (gecontroleerd; secrets komen
uitsluitend uit de GitHub-secretsstore). Maar bovenstaande is bedrijfsdata en
de Lead-ID's zijn persoonsgegeven-adjacent. **Advies: zet de repo op private**
(Settings → General → Danger Zone → Change visibility). GitHub Pages werkt dan
alleen nog op GitHub Enterprise; het alternatief is het dashboard intern
serveren of achter Cloudflare Access zetten.

---

## Salesforce Connected App (External Client App)

In Salesforce Setup → App Manager → **New External Client App**:

1. Basic Information invullen, **Create**.
2. Onder OAuth Settings: **Enable OAuth**, Callback URL `https://login.salesforce.com/services/oauth2/callback` (verplicht veld, niet gebruikt bij deze flow), OAuth Scope **"Manage user data via APIs (api)"**. Save.
3. Op de app zelf → tabblad **Policies** → **Enable Client Credentials Flow** aanvinken → **Run As (Username)**: een integratiegebruiker met alleen leesrechten op Leads (geen persoonlijk/admin-account). **IP Relaxation**: zet op **"Relax IP restrictions"** (GitHub Actions heeft geen vast IP-adres). Save.
4. Noteer Consumer Key en Consumer Secret (via "View" op de app, Consumer Details).
5. Noteer je My Domain URL, bv. `otolift.my.salesforce.com` (zonder `https://`, zonder `-setup`).

De Run As-gebruiker heeft leestoegang tot het Lead-object nodig (Bulk API query
op `LeadHistory` leunt op leesrechten op Lead).

## Aanpassen

- **Tijdvenster / gevolgd veld**: `HISTORY_START_DATE` en de `Field = 'Status'`-filter in [scripts/export_salesforce_report.py](scripts/export_salesforce_report.py).
- **Statuswaarden van de funnel**: de `S_*`-constanten in [scripts/build_crm_metrics.py](scripts/build_crm_metrics.py).
- **Benchmarkdrempels** van de Email Health-badges: `thresholds` in [docs/data-layer.js](docs/data-layer.js).
- **Drempel voor flow-chips**: `MIN_SENT_FOR_CHIP` in [scripts/build_data_json.py](scripts/build_data_json.py).
