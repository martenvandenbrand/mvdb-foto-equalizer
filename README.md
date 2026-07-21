# Flesfoto-normalisator voor Shopify

Standaardiseert alle productfoto's naar één formaat: fles vrijgetrimd, geschaald
naar een vaste hoogte en gecentreerd op een **transparant 2048×2048 canvas** met
gelijke witruimte boven en onder. Transparante achtergrond blijft transparant
(uitvoer is PNG). Draait als GitHub Action.

## Repo-structuur
```
normaliseer_flesfotos.py
requirements.txt
.github/workflows/normaliseer-flesfotos.yml
```

## Eenmalige setup

**1. App aanmaken in de Dev Dashboard** — sinds 1 januari 2026 kun je in de Shopify-admin
geen custom apps met een permanent `shpat_`-token meer maken. Ga naar de Dev Dashboard
(dev.shopify.com/dashboard) → **Create app**. Stel de access scopes in: `read_products`,
`write_products`, `write_files`. Installeer de app in je eigen winkel. Onder **Settings**
vind je de **Client ID** en **Client Secret**. Het script ruilt die via de client
credentials grant automatisch in voor een tijdelijk access token (24u geldig).

**2. GitHub secrets** — Repo → Settings → Secrets and variables → Actions → New secret:
- `SHOP` = `jouwwinkel.myshopify.com` (de myshopify-URL, niet koperenkaraf.nl)
- `SHOPIFY_CLIENT_ID` = de Client ID uit de Dev Dashboard
- `SHOPIFY_CLIENT_SECRET` = de Client Secret uit de Dev Dashboard

## Draaien

Actions-tabblad → **Normaliseer flesfoto's** → **Run workflow**. Knoppen:

| Input | Standaard | Wat het doet |
|-------|-----------|--------------|
| `dry_run` | **aan** | Bewerkt alles maar schrijft niets naar Shopify; download het resultaat onderaan de run als artifact `bewerkte-flesfotos` |
| `delete_original` | uit | Verwijdert de oude foto nadat de nieuwe hoofdfoto geplaatst is |
| `first_image_only` | aan | Alleen de hoofdfoto per product (labeldetails blijven ongemoeid) |
| `filter_query` | leeg | Beperk tot een selectie, bv. `tag:rood` |
| `bottle_height` | 1600 | Fleshoogte in px op het 2048-canvas |

**Aanbevolen volgorde:** eerst met `dry_run` aan draaien, het artifact downloaden en
controleren, daarna opnieuw met `dry_run` uit. Laat `delete_original` bij de eerste
live-run uit; de nieuwe foto komt vooraan te staan en de originele blijft als vangnet.

---

## Tweede action: smaakfoto's genereren

`smaakfoto_generator.py` + workflow **Genereer smaakfoto's**. Per wijn worden de
smaken uit de productbeschrijving gehaald; elke smaak wordt los als fotorealistische
uitsnede (transparant) gegenereerd met OpenAI GPT Image en daarna deterministisch
naast de fles geplaatst: primair links, secundair rechts, organisch geclusterd per
type met wisselende groottes, lichte rotatie en een zachte contactschaduw voor diepte. De fles staat exact `BOTTLE_PX` (2000) hoog op een 2048x2048
canvas, scherp uit het originele bestand. Elke unieke smaak wordt gecacht en
hergebruikt, dus je betaalt per smaak maar één keer. Het resultaat komt als tweede
productfoto terug in Shopify.

**Extra secret:** `OPENAI_API_KEY` (uit platform.openai.com). Je organisatie moet
mogelijk eerst geverifieerd zijn in de OpenAI-console om GPT Image te mogen gebruiken.

### Kostenbeheersing
- **`batch_size`** (1 of 10): hoeveel flessen deze run. Zo houd je de kosten per run in de hand.
- **`handle`**: draai exact 1 gekozen fles (bv. `vignoble-nicolas-therez-cuvee-serres-moi-2024`) om te testen.
- Verwerkte producten krijgen de tag `smaakfoto`; volgende runs slaan die automatisch over, dus je betaalt nooit dubbel.
- **`image_model`** / **`image_quality`**: `gpt-image-1-mini` + `low` is het goedkoopst (~$0,005–0,01 per foto), `gpt-image-1.5` + `high` het mooist (tot ~$0,20). Edits met een referentiefoto tellen ook wat input-tokens mee.

> Let op: `dry_run` genereert wél de beelden (dus OpenAI-kosten) en levert ze als
> artifact `smaakfotos`, maar uploadt en tagt niet. Handig om eerst het resultaat
> te beoordelen; zet daarna `dry_run` uit voor dezelfde flessen.

### Aanbevolen eerste run
Zet `handle` op één wijn, `dry_run` aan. Beoordeel het artifact. Klopt het? Draai
dezelfde `handle` met `dry_run` uit. Daarna in batches van 10 door de rest.

### Stijl en indeling fijnafstemmen
- **`flavor_size`** (220/260/320 px): grootte van de smaken. Kleiner = subtieler.
- **`image_quality: high`** geeft de meest fotografische uitsnedes.
- **`BOTTLE_PX`** (env, standaard 2000) en **`FINAL_SIZE`** (2048): flesgrootte en canvas.
- **`COL_MARGIN`** (env, 0.16): hoe ver de kolommen van het midden staan.
- De cache staat in `flavor_cache/`. Bevalt een specifieke smaak niet? Verwijder dat
  ene bestand (of maak de cache leeg) en draai opnieuw; alleen die wordt opnieuw gemaakt.
### Fotorealisme: GPT vs. echte foto's
GPT's beeldmodel neigt voor sommige objecten (room, bloemen, noten) naar een
gerenderde/CGI-look. De prompt duwt daar hard tegenin, maar wil je gegarandeerd
fotorealisme, zet dan **`cutout_source: stock`**: dan worden echte foto's van
Pexels opgehaald en wordt de achtergrond weggeknipt (rembg).
- Extra (gratis) secret: `PEXELS_API_KEY` (via pexels.com/api).
- De workflow installeert `rembg` automatisch alleen in stock-modus.
- Cache is bron-bewust: bestanden heten `naam-gpt.png` of `naam-stock.png`, dus
  gpt en stock botsen niet en wisselen heeft meteen effect. Bevalt een uitsnede niet,
  verwijder dat ene bestand en draai opnieuw. Oude bestanden zonder achtervoegsel
  (van vóór deze wijziging) mag je weggooien.

### Smaken samenvoegen (goedkoper genereren)
`synonyms.json` mapt smaakvarianten naar één canonieke smaak, zodat bijna-identieke
smaken dezelfde uitsnede delen (minder generaties = lagere kosten). Twee secties:
- **`merge`**: altijd toegepast — meervoud/spelling, synoniemen (citrus→citroen,
  eiken→eikenhout), en abstracte sensaties (structuur, frisheid) die vervallen.
- **`merge_colors`**: alleen als `merge_colors` aan staat — kleur/rijpheid
  (rode+zwarte kers → kers, witte+gele perzik → perzik).

Het script canoniseert en ontdubbelt per wijn vóór het genereren. Wil je een
samenvoeging anders, pas `synonyms.json` aan. Voor deze catalogus: 381 → ~130
unieke smaken (veilig), ~111 met kleur erbij.

### Aroma-stijlen
Kies in de workflow met `aroma_stijl` uit zeven layouts:
- **kolommen** (huidig), **krans**, **explosie**, **geometrisch** — hergebruiken de
  bestaande smaak-cutouts of tekenen alles in Pillow: **0 extra OpenAI-calls**.
- **aromawolk**, **kleurverloop**, **rook** — gebruiken één herbruikbare sfeer-asset
  per wijnkleur (rood/wit/rosé, automatisch gedetecteerd uit de flesfoto). Max 3
  eenmalige generaties per stijl; daarna alles uit cache. Legenda's (stippen +
  smaaknamen) tekent Pillow gratis.
- **`overwrite`**: selecteert producten MET de tag en vervangt hun bestaande
  smaakfoto (herkend aan alt-tekst "Wat je proeft") — de oude wordt pas verwijderd
  nadat de nieuwe staat. Zo vervang je de hele catalogus veilig van stijl.

### Lettertype van de labels
De smaaklabels in de `aromawolk`-stijl gebruiken **Fraunces Italic** (SemiBold),
gebundeld in `fonts/FrauncesItalic.ttf`. Het verbindingslijntje wordt met
supersampling getekend (anti-aliased) en heeft een lichte, willekeurige boog per
smaak, zodat het zacht en handgeschreven oogt in plaats van hoekig. De fles wordt
vóór de labels getekend, zodat een label nooit door de capsule kan worden afgesneden.

### Batchgrootte
`batch_size` heeft nu ook 25, 50, 100 en **alle**. Bij "alle" doorbladert het
script automatisch (Shopify geeft max 250 producten per pagina), dus dat werkt
ook ruim voorbij je huidige catalogusgrootte. Combineer met `overwrite` om in
één run de hele catalogus opnieuw te genereren — let dan wel op de
kostenregel onderaan de run.

### Alle wijnen van 1 wijnhuis
`wijnhuis` (deelstring van de producentnaam, bv. `Marqués de Murrieta`) selecteert
alle wijnen van die producent, ongeacht `batch_size`. Zoekt in het `custom.wijnhuis_new`-
metaveld, hoofdletterongevoelig en op deelstring — `murrieta` vindt dus ook
"Marqués de Murrieta". Leeg laten = normaal gedrag via `batch_size`.

### Toestemming vóór elke OpenAI-uitgave
De workflow bestaat nu uit twee jobs:

1. **voorcalculatie** — draait altijd, kost niets. Bepaalt zonder ook maar 1 OpenAI-call welke
   smaak-extracties en smaak-afbeeldingen nog ontbreken in de cache, en print dat met reden en
   een kostenschatting in het log.
2. **genereren** — de echte run. Deze job wacht op goedkeuring via de `openai-approval`-environment
   voordat er ook maar iets wordt aangeroepen of uitgegeven.

**Eenmalige instelling** (per repo): Settings → Environments → New environment → naam
`openai-approval` → vink **Required reviewers** aan → voeg jezelf (of wie mag goedkeuren) toe →
Save protection rules.

Na het starten van de Action zie je eerst het log van `voorcalculatie` met wat er nodig is en
waarom. Wil je doorgaan, klik dan op **Review deployments → Approve and deploy** bij de
`genereren`-job. Zonder die klik gebeurt er niets — geen call, geen kosten.
