# sfmc-dashboard

Periodieke export van een Salesforce-rapport naar `exports/report.csv`, automatisch bijgewerkt via GitHub Actions.

## Eenmalige setup

### 1. Salesforce Connected App

In Salesforce Setup → App Manager → **New Connected App**:

1. Enable OAuth Settings.
2. Callback URL: `https://login.salesforce.com/services/oauth2/callback` (verplicht veld, niet gebruikt bij deze flow).
3. OAuth Scope: `Manage user data via APIs (api)`.
4. Enable **Client Credentials Flow**.
5. Na ~10 min: Manage → Edit Policies → stel bij "Client Credentials Flow" een **Run As**-gebruiker in (integratiegebruiker met alleen leesrechten op het rapport).
6. Noteer Consumer Key en Consumer Secret (Manage Consumer Details).
7. Noteer je My Domain URL, bv. `otolift.my.salesforce.com` (zonder `https://`).

### 2. Rapport-ID

Open het rapport in Salesforce en kopieer het ID uit de URL:
`.../lightning/r/Report/<REPORT_ID>/view`

### 3. GitHub secrets

Settings → Secrets and variables → Actions → New repository secret:

- `SF_DOMAIN`
- `SF_CLIENT_ID`
- `SF_CLIENT_SECRET`
- `SF_REPORT_ID`

### 4. Klaar

De workflow (`.github/workflows/salesforce-export.yml`) draait dagelijks om 06:00 UTC en commit wijzigingen naar `exports/report.csv`. Pas de cron-regel aan voor een andere frequentie, of trigger handmatig via de Actions-tab ("Run workflow").

## Let op

De synchrone Reports API geeft standaard max ~2000 rijen terug. Bij grotere rapporten moet de asynchrone report-instances API gebruikt worden — het script logt een waarschuwing als dit limiet bereikt wordt.
