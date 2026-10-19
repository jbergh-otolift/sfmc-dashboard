# Dagelijkse export instellen

Voor: beheerder van deze repo, op een Mac die op het kantoornetwerk zit.

## Waarom dit nodig is

Marketing Cloud laat sinds 16 oktober 2026 alleen API-verzoeken toe vanaf
IP-adressen die op een allowlist staan. Het kantoor staat daarop
(159.100.82.228–231, Otto Ooms B.V.), de servers van GitHub niet. Die krijgen
bovendien elke run een ander adres, dus toevoegen aan de lijst kan niet.

Gevolg: de GitHub Action `Refresh dashboard data` faalt elke dag op de
mailexport. Salesforce werkt wel; alleen Marketing Cloud blokkeert.

Dit script doet hetzelfde werk vanaf een machine die wél toegang heeft.

## Eenmalig instellen

**1. Sleutels wegzetten**

```bash
mkdir -p ~/.config/otolift
cat > ~/.config/otolift/sleutels.env <<'EOF'
SFMC_SUBDOMAIN=...
SFMC_CLIENT_ID=...
SFMC_CLIENT_SECRET=...
SFMC_MID_NL=...
SFMC_MID_BE=...
SF_DOMAIN=...
SF_CLIENT_ID=...
SF_CLIENT_SECRET=...
SF_REPORT_ID=...
EOF
chmod 600 ~/.config/otolift/sleutels.env
```

De waarden staan in de repo-instellingen op GitHub onder Settings → Secrets
and variables → Actions. Secrets zijn daar niet uit te lezen; haal ze uit de
bron waar ze vandaan kwamen, of maak nieuwe aan.

**2. Eén keer met de hand draaien**

```bash
~/Desktop/sfmc-dashboard-live/scripts/dagelijkse-export.sh
```

Hij controleert eerst of Marketing Cloud dit IP-adres accepteert en stopt met
een duidelijke melding als dat niet zo is. Duurt ongeveer tien minuten.

**3. Dagelijks laten draaien**

```bash
cat > ~/Library/LaunchAgents/com.otolift.dashboard-export.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.otolift.dashboard-export</string>
  <key>ProgramArguments</key>
  <array><string>/Users/JOUWNAAM/Desktop/sfmc-dashboard-live/scripts/dagelijkse-export.sh</string></array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer>
  </dict>
  <key>StandardErrorPath</key><string>/tmp/otolift-export.err</string>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/com.otolift.dashboard-export.plist
```

Vervang `JOUWNAAM` door je gebruikersnaam.

## Wat je moet weten

De Mac moet **aan staan en op kantoor zijn**. Thuis is het IP-adres anders en
weigert Marketing Cloud het verzoek; het script stopt dan netjes zonder iets
te pushen. In het weekend draait er niets.

Het logboek staat in `~/Library/Logs/otolift-export.log`.

Het script pusht alleen als alle stappen slaagden. Faalt er één, dan blijft
de vorige versie van het dashboard staan.

## De echte oplossing

Dit is een tussenoplossing die afhangt van één laptop. Structureel is er één
vast IP-adres nodig op de allowlist in Marketing Cloud Administration, van
een machine die altijd aan staat. Er staan al vier adressen van Otto Ooms op
die lijst, dus het is een kleine uitbreiding — geen verzoek om GitHub toe te
laten.
