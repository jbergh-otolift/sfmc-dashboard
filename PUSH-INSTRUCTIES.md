# Pushen

Deze map is een volledige clone van `jbergh-otolift/sfmc-dashboard` met één
extra commit op de branch `feat/live-dashboard`. Je kunt hier direct uit pushen.

```sh
cd ~/Desktop/sfmc-dashboard-live
git log --oneline -1          # → Dashboard live op echte SFMC- en CRM-data
git push -u origin feat/live-dashboard
```

Open daarna de PR: <https://github.com/jbergh-otolift/sfmc-dashboard/compare/feat/live-dashboard>

Vraagt hij om een wachtwoord: gebruik een Personal Access Token (Settings →
Developer settings → Personal access tokens → Fine-grained, scope `Contents:
write` op deze repo) als wachtwoord, niet je GitHub-wachtwoord. Of installeer
de CLI: `brew install gh && gh auth login`, dan werkt `git push` zonder prompt.

## Voordat de workflow kan draaien

De dagelijkse refresh heeft vier nieuwe secrets nodig (Settings → Secrets and
variables → Actions → New repository secret). De waarden staan lokaal in
`Mail Library/Dashboard/bu_config.json`:

| Secret | Waarde uit bu_config.json |
|---|---|
| `SFMC_SUBDOMAIN` | `subdomain` |
| `SFMC_CLIENT_ID` | `client_id` |
| `SFMC_CLIENT_SECRET` | `client_secret` |
| `SFMC_MID_NL` | `account_id` van de NL business unit |
| `SFMC_MID_BE` | `account_id` van de BE business unit |

`SF_DOMAIN`, `SF_CLIENT_ID` en `SF_CLIENT_SECRET` bestaan al.

Zet `bu_config.json` zelf **niet** in de repo — dat bestand staat in
`.gitignore` en dat moet zo blijven.

## GitHub Pages aanzetten

Settings → Pages → Source: **Deploy from a branch**, branch `main`, map
`/docs`. Het dashboard komt dan op
<https://jbergh-otolift.github.io/sfmc-dashboard/>.

De repo is publiek, dus die pagina is voor iedereen leesbaar. Zie de paragraaf
"Zichtbaarheid" in de README.

## Lokaal bekijken zonder te pushen

```sh
cd ~/Desktop/sfmc-dashboard-live
python3 -m http.server 8000 --directory docs
```

Dan <http://localhost:8000> openen. `docs/data.json` zit al in de commit, dus
je hebt geen credentials nodig om het dashboard met echte data te zien.
