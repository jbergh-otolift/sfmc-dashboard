# sfmc-dashboard

Periodieke export van Lead-statuswijzigingen (LeadHistory, veld `Status`, vanaf 2026-08-01) naar `exports/report.csv`, automatisch bijgewerkt via GitHub Actions. Haalt data op via Salesforce Bulk API 2.0 (SOQL), zonder rijenlimiet.

## Eenmalige setup

### 1. Salesforce Connected App (External Client App)

In Salesforce Setup → App Manager → **New External Client App**:

1. Basic Information invullen, **Create**.
2. Onder OAuth Settings: **Enable OAuth**, Callback URL `https://login.salesforce.com/services/oauth2/callback` (verplicht veld, niet gebruikt bij deze flow), OAuth Scope **"Manage user data via APIs (api)"**. Save.
3. Op de app zelf → tabblad **Policies** → **Enable Client Credentials Flow** aanvinken → **Run As (Username)**: een integratiegebruiker met alleen leesrechten op Leads (geen persoonlijk/admin-account). **IP Relaxation**: zet op **"Relax IP restrictions"** (GitHub Actions heeft geen vast IP-adres). Save.
4. Noteer Consumer Key en Consumer Secret (via "View" op de app, Consumer Details).
5. Noteer je My Domain URL, bv. `otolift.my.salesforce.com` (zonder `https://`, zonder `-setup`).

De Run As-gebruiker heeft leestoegang tot het Lead-object nodig (Bulk API query op `LeadHistory` leunt op leesrechten op Lead).

### 2. GitHub secrets

Settings → Secrets and variables → Actions → New repository secret:

- `SF_DOMAIN`
- `SF_CLIENT_ID`
- `SF_CLIENT_SECRET`

### 3. Klaar

De workflow (`.github/workflows/salesforce-export.yml`) draait dagelijks om 06:00 UTC en commit wijzigingen naar `exports/report.csv`. Pas de cron-regel aan voor een andere frequentie, of trigger handmatig via de Actions-tab ("Run workflow").

## Aanpassen

- **Tijdvenster / gevolgd veld**: pas `HISTORY_START_DATE` en de `Field = 'Status'`-filter aan in [scripts/export_salesforce_report.py](scripts/export_salesforce_report.py).
- **Ander object dan LeadHistory**: pas de `SOQL_QUERY` en kolommapping in hetzelfde script aan.
