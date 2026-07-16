#!/usr/bin/env python3
"""
Koper & Karaf - Flesfoto normalisator voor Shopify (transparante achtergrond)
=============================================================================
Haalt productafbeeldingen op uit Shopify, trimt de fles vrij op het
alpha-kanaal, schaalt naar vaste hoogte, centreert op een TRANSPARANT
2048x2048 canvas (gelijke witruimte boven/onder) en zet het terug in Shopify.

Achtergrond blijft transparant -> uitvoer is altijd PNG.
Volledig GraphQL Admin API. Instellingen komen uit environment-variabelen
zodat het script ongewijzigd in een GitHub Action draait.
"""

import os, io, time, sys, json, pathlib, requests
from PIL import Image

# ---- helper: env var uitlezen ----
def env(key, default=""):        return os.environ.get(key, default)
def env_bool(key, default):      return os.environ.get(key, str(default)).strip().lower() in ("1", "true", "yes", "ja")
def env_int(key, default):       return int(os.environ.get(key, default))

# ======================= CONFIGURATIE (via env / Action-inputs) =======================
SHOP            = env("SHOP", "jouwwinkel.myshopify.com")   # de .myshopify.com URL
CLIENT_ID       = env("SHOPIFY_CLIENT_ID", "")             # uit Dev Dashboard -> Settings
CLIENT_SECRET   = env("SHOPIFY_CLIENT_SECRET", "")         # uit Dev Dashboard -> Settings
API_VERSION     = env("API_VERSION", "2026-01")

CANVAS          = env_int("CANVAS", 2048)
BOTTLE_HEIGHT   = env_int("BOTTLE_HEIGHT", 1600)   # witruimte boven/onder = (CANVAS - BOTTLE_HEIGHT) / 2
ALPHA_THRESHOLD = env_int("ALPHA_THRESHOLD", 8)    # pixels met alpha <= dit tellen als leeg (negeert zachte schaduwranden)

DRY_RUN          = env_bool("DRY_RUN", True)        # True = alleen bewerken + in ./backup, niets naar Shopify
FIRST_IMAGE_ONLY = env_bool("FIRST_IMAGE_ONLY", True)
DELETE_ORIGINAL  = env_bool("DELETE_ORIGINAL", False)
FILTER_QUERY     = env("FILTER_QUERY", "")          # bv. "tag:rood", leeg = alle producten
BACKUP_DIR       = pathlib.Path("backup")
# ======================================================================================

API_URL   = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
TOKEN_URL = f"https://{SHOP}/admin/oauth/access_token"

_access_token = None   # wordt in main() opgehaald via de client credentials grant


def get_access_token():
    """Ruil client_id + client_secret in voor een access token (24u geldig)."""
    r = requests.post(TOKEN_URL, timeout=30, data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    r.raise_for_status()
    return r.json()["access_token"]


# ------------------------- Beeldbewerking (RGBA / transparant) -------------------------
def normalize(im):
    im = im.convert("RGBA")
    mask = im.getchannel("A").point(lambda a: 255 if a > ALPHA_THRESHOLD else 0)
    bbox = mask.getbbox()
    if bbox:
        im = im.crop(bbox)
    w, h = im.size
    new_w = max(1, round(w * BOTTLE_HEIGHT / h))
    im = im.resize((new_w, BOTTLE_HEIGHT), Image.LANCZOS)
    out = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))   # transparant canvas
    out.paste(im, ((CANVAS - new_w) // 2, (CANVAS - BOTTLE_HEIGHT) // 2), im)
    return out


# ------------------------- Shopify GraphQL -------------------------
def gql(query, variables=None):
    headers = {"X-Shopify-Access-Token": _access_token, "Content-Type": "application/json"}
    for attempt in range(6):
        r = requests.post(API_URL, headers=headers,
                          data=json.dumps({"query": query, "variables": variables or {}}))
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 2))); continue
        data = r.json()
        if "errors" in data and any("THROTTLED" in str(e) for e in data["errors"]):
            time.sleep(2 * (attempt + 1)); continue
        if "errors" in data:
            raise RuntimeError(data["errors"])
        return data["data"]
    raise RuntimeError("Te vaak gethrottled")

PRODUCTS_Q = """
query($cursor: String, $q: String) {
  products(first: 25, after: $cursor, query: $q) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title handle
      media(first: 20) { nodes { ... on MediaImage { id image { url altText } } } }
    }
  }
}"""

def iter_products():
    cursor = None
    while True:
        d = gql(PRODUCTS_Q, {"cursor": cursor, "q": FILTER_QUERY or None})
        for p in d["products"]["nodes"]:
            yield p
        if not d["products"]["pageInfo"]["hasNextPage"]:
            break
        cursor = d["products"]["pageInfo"]["endCursor"]

STAGED_M = """
mutation($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}"""

CREATE_M = """
mutation($productId: ID!, $media: [CreateMediaInput!]!) {
  productCreateMedia(productId: $productId, media: $media) {
    media { ... on MediaImage { id } }
    mediaUserErrors { field message }
  }
}"""

REORDER_M = """
mutation($id: ID!, $moves: [MoveInput!]!) {
  productReorderMedia(id: $id, moves: $moves) { job { id } userErrors { field message } }
}"""

DELETE_M = """
mutation($productId: ID!, $mediaIds: [ID!]!) {
  productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
    deletedMediaIds mediaUserErrors { field message }
  }
}"""

def upload_to_shopify(img_bytes, filename):
    t = gql(STAGED_M, {"input": [{"filename": filename, "mimeType": "image/png",
                                  "httpMethod": "POST", "resource": "IMAGE"}]})
    tgt = t["stagedUploadsCreate"]["stagedTargets"][0]
    form = {p["name"]: p["value"] for p in tgt["parameters"]}
    resp = requests.post(tgt["url"], data=form,
                         files={"file": (filename, img_bytes, "image/png")})
    resp.raise_for_status()
    return tgt["resourceUrl"]

def create_media(product_id, resource_url, alt):
    d = gql(CREATE_M, {"productId": product_id,
                       "media": [{"mediaContentType": "IMAGE",
                                  "originalSource": resource_url, "alt": alt or ""}]})
    errs = d["productCreateMedia"]["mediaUserErrors"]
    if errs: raise RuntimeError(errs)
    return d["productCreateMedia"]["media"][0]["id"]


# ------------------------- Hoofdroutine -------------------------
def main():
    global _access_token
    if not CLIENT_ID or not CLIENT_SECRET or SHOP.startswith("jouwwinkel"):
        sys.exit("Ontbrekende SHOP / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET (zet ze als GitHub secrets).")
    _access_token = get_access_token()
    BACKUP_DIR.mkdir(exist_ok=True)
    print(f"== {'DRY-RUN' if DRY_RUN else 'LIVE'} | {SHOP} | canvas {CANVAS} | fleshoogte {BOTTLE_HEIGHT} "
          f"| eerste foto: {FIRST_IMAGE_ONLY} | verwijder origineel: {DELETE_ORIGINAL} ==\n")

    done = skipped = failed = 0
    for p in iter_products():
        imgs = [m for m in p["media"]["nodes"] if m.get("image")]
        if not imgs:
            skipped += 1; continue
        for m in (imgs[:1] if FIRST_IMAGE_ONLY else imgs):
            try:
                url = m["image"]["url"]; alt = m["image"].get("altText")
                raw = requests.get(url, timeout=30).content
                out = normalize(Image.open(io.BytesIO(raw)))

                buf = io.BytesIO(); out.save(buf, "PNG", optimize=True); buf.seek(0)
                handle = p["handle"]                       # = URL-slug, bv. vignoble-nicolas-therez-cuvee-serres-moi-2024
                out.save(BACKUP_DIR / f"{handle}.png", "PNG", optimize=True)

                if DRY_RUN:
                    print(f"[dry] {handle}"); done += 1; continue

                new_id = create_media(p["id"], upload_to_shopify(buf.getvalue(), f"{handle}.png"), alt)
                gql(REORDER_M, {"id": p["id"], "moves": [{"id": new_id, "newPosition": "0"}]})
                if DELETE_ORIGINAL:
                    gql(DELETE_M, {"productId": p["id"], "mediaIds": [m["id"]]})
                print(f"[ok]  {p['title']}"); done += 1
            except Exception as e:
                print(f"[ERR] {p['title']}: {e}"); failed += 1

    print(f"\nKlaar. Verwerkt: {done} | overgeslagen: {skipped} | fouten: {failed}")

if __name__ == "__main__":
    main()
