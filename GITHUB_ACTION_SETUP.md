# GitHub Action - Shopify Aroma Sync Setup

Deze GitHub Action synchroniseert automatisch alle aromas uit `flavor_meta.json` naar je Shopify store.

## Setup Stappen

### 1. Voeg de workflow file toe aan je repo

Plaats het bestand `shopify-sync.yml` in je repository onder:
```
.github/workflows/shopify-sync.yml
```

(Maak de `.github/workflows/` folder aan als deze niet bestaat)

### 2. Voeg je Shopify API Token toe als GitHub Secret

1. Ga naar je GitHub repository
2. Settings → Secrets and variables → Actions
3. Klik "New repository secret"
4. Naam: `SHOPIFY_ACCESS_TOKEN`
5. Value: Je Shopify API token (van Admin → Apps → Develop apps)

### 3. (Optioneel) Voeg je store domain toe

1. Herhaal stap 2
2. Naam: `SHOPIFY_STORE`
3. Value: `koperenkaraf.myshopify.com` (of jouw domain)
   
Als je dit niet toevoegt, gebruikt de workflow de default domain.

### 4. Zorg dat `flavor_meta.json` in de root van je repo staat

```
mvdb-foto-equalizer/
├── flavor_meta.json
├── .github/
│   └── workflows/
│       └── shopify-sync.yml
└── ...
```

## Gebruiken

### Manier 1: Via GitHub UI (Gemakkelijk)

1. Ga naar je GitHub repo
2. Actions → Shopify Aroma Sync
3. Klik "Run workflow"
4. (Optioneel) Vink "Dry run" aan om eerst te testen
5. Klik "Run workflow"

### Manier 2: Via GitHub CLI

```bash
gh workflow run shopify-sync.yml -f dry_run=false
```

### Dry Run (Veilig testen)

De workflow heeft een "dry run" mode waarin je kunt zien wat zou gebeuren zonder werkelijke updates:

1. Actions → Shopify Aroma Sync
2. Run workflow
3. Vink "Dry run" aan
4. Run workflow
5. Check de logs - geen producten zullen worden geüpdatet

## Wat doet de workflow?

✅ Laadt alle aromas uit `flavor_meta.json`
✅ Merged primaire + secundaire aromas
✅ Verwijdert duplicaten per product
✅ Zoekt elk product in je Shopify store
✅ Updatet het `wine_profile.aromas` metafield
✅ Geeft een gedetailleerd rapport

## Output

Na elke run zie je in de GitHub Actions log:
- Progress per product
- Succesvol geüpdatet: X
- Fouten: Y
- Niet gevonden: Z

Plus een downloadbare artifact met details.

## Troubleshooting

### "SHOPIFY_ACCESS_TOKEN secret not set"
**Oplossing:** Voeg de secret toe in Settings → Secrets and variables → Actions

### "Not found" voor veel producten
**Oorzaak:** Product handles in `flavor_meta.json` matchen niet met Shopify handles
**Oplossing:** Controleer product URLs in Shopify Admin - het handle staat in de URL

### Workflow failed
**Controleer:** 
1. API token correct?
2. Token heeft `write_products` en `read_products` scopes?
3. `flavor_meta.json` bestaat in root?
4. Workflow YAML is correct geplaatst?

## Veiligheid

✅ Je API token is veilig in GitHub Secrets
✅ Token wordt NIET gelogd
✅ Workflow runs in geïsoleerde omgeving
✅ Alleen jij en collaborators kunnen het starten
✅ Geen code wordt naar extern uploaded

## Kosten

✅ GRATIS - GitHub Actions zijn kosteloos voor publieke repos
✅ Beperkt gratis gebruik voor private repos (2000 minuten/maand)

## Aanpassen

Wil je iets wijzigen? Edit de `.github/workflows/shopify-sync.yml` file:

- **Batch size:** Zoeken naar `first: 20` en aanpassen
- **Filtering:** Wijzig de product query in `find_product_by_handle()`
- **Logging:** Voeg meer print statements toe

## Statusbadge (Optioneel)

Voeg dit toe aan je README.md om sync status te tonen:

```markdown
![Shopify Aroma Sync](https://github.com/YOUR_USERNAME/mvdb-foto-equalizer/actions/workflows/shopify-sync.yml/badge.svg)
```

## Verdere Automation

Wil je dit automatisch laten runnen?

Voeg dit toe aan de workflow YAML:

```yaml
on:
  schedule:
    - cron: '0 2 * * 0'  # Elke zondag om 2 uur 's ochtends
  workflow_dispatch:
```

Dan run het automatisch weekly!

---

**Support:** Vragen? Check de GitHub Actions logs of contact info@koperenkaraf.nl
