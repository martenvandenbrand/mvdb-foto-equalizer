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

**1. Shopify custom app** — Settings → Apps and sales channels → Develop apps →
Create an app. Scopes: `read_products`, `write_products`, `write_files`.
Installeer en kopieer de Admin API access token (`shpat_…`).

**2. GitHub secrets** — Repo → Settings → Secrets and variables → Actions → New secret:
- `SHOP` = `jouwwinkel.myshopify.com` (de myshopify-URL, niet koperenkaraf.nl)
- `SHOPIFY_ADMIN_TOKEN` = je `shpat_…` token

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
