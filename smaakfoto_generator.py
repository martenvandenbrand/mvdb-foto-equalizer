#!/usr/bin/env python3
"""
Koper & Karaf - Smaakfoto-generator (deterministische compositie)
=================================================================
Per wijn: smaken uit de beschrijving halen, elke smaak los als fotorealistische
uitsnede (transparant) genereren met GPT Image, en die daarna zelf exact
uitgelijnd naast de fles plaatsen.

  - Fles: exact BOTTLE_PX hoog op een FINAL_SIZE x FINAL_SIZE canvas, scherp uit
    het originele bestand (niet opgeschaald).
  - Smaken: primair links, secundair rechts, per type geclusterd, op gelijke
    rijhoogtes links/rechts, met instelbare grootte (subtiliteit).
  - Cache: elke unieke smaak wordt 1x gegenereerd en hergebruikt -> lage kosten.

Kostenbeheersing: BATCH_SIZE (1/10), HANDLE (1 fles), DONE_TAG (nooit dubbel),
en de cache. Verwerkte producten krijgen DONE_TAG.
"""

import os, io, sys, json, time, base64, html, re, pathlib, random, zlib, requests
from PIL import Image, ImageFilter, ImageDraw

def env(k, d=""):   return os.environ.get(k, d)
def env_bool(k, d): return os.environ.get(k, str(d)).strip().lower() in ("1", "true", "yes", "ja")
def env_int(k, d):  return int(os.environ.get(k, d))

# ======================= CONFIGURATIE (via env / Action-inputs) =======================
SHOP           = env("SHOP", "jouwwinkel.myshopify.com")
CLIENT_ID      = env("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET  = env("SHOPIFY_CLIENT_SECRET", "")
API_VERSION    = env("API_VERSION", "2026-01")

OPENAI_API_KEY     = env("OPENAI_API_KEY", "")
OPENAI_IMAGE_MODEL = env("OPENAI_IMAGE_MODEL", "gpt-image-1.5")   # of "gpt-image-1-mini" (goedkoper)
OPENAI_TEXT_MODEL  = env("OPENAI_TEXT_MODEL", "gpt-4o-mini")
IMAGE_QUALITY      = env("IMAGE_QUALITY", "high")                 # low | medium | high

CUTOUT_SOURCE  = env("CUTOUT_SOURCE", "gpt")     # gpt = genereren | stock = echte foto's (Pexels) + achtergrond weg
PEXELS_API_KEY = env("PEXELS_API_KEY", "")       # alleen nodig bij CUTOUT_SOURCE=stock
BYPASS_CACHE   = env_bool("BYPASS_CACHE", False)  # True = cache negeren en verse uitsnede maken (ook bij gpt)
BG_COLOR_HEX   = env("BG_COLOR_HEX", "#EFF0F5")    # achtergrondkleur voor modellen zonder transparant (gpt-image-2), wordt weer weggeknipt

FINAL_SIZE     = env_int("FINAL_SIZE", 2048)     # canvas (vierkant)
BOTTLE_PX      = env_int("BOTTLE_PX", 2000)      # fleshoogte in px
FLAVOR_PX      = env_int("FLAVOR_PX", 260)       # max grootte van een smaak-uitsnede (subtiliteit)
COL_MARGIN     = float(env("COL_MARGIN", "0.16"))# kolomcenter t.o.v. canvasbreedte (links/rechts)

BATCH_SIZE     = env_int("BATCH_SIZE", 1)
HANDLE         = env("HANDLE", "")
DONE_TAG       = env("DONE_TAG", "smaakfoto")
DRY_RUN        = env_bool("DRY_RUN", True)       # genereert wel (OpenAI-kosten), upload/tagt niet
BACKUP_DIR     = pathlib.Path("backup_smaak")
CACHE_DIR      = pathlib.Path("flavor_cache")    # hergebruikte smaak-uitsnedes
META_FILE      = pathlib.Path("flavor_meta.json") # gecachete smaak-extractie (alle producten in 1 bestand)
_LEGACY_META_DIR = pathlib.Path("flavor_meta")    # oude losse bestanden (worden eenmalig ingelezen)
# ======================================================================================

API_URL   = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
TOKEN_URL = f"https://{SHOP}/admin/oauth/access_token"
_access_token = None


# ------------------------- Shopify auth + GraphQL -------------------------
def get_access_token():
    r = requests.post(TOKEN_URL, timeout=30, data={
        "grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    r.raise_for_status()
    return r.json()["access_token"]

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

SELECT_Q = """
query($n: Int!, $q: String) {
  products(first: $n, query: $q) {
    nodes { id title handle description featuredImage { url } }
  }
}"""

def select_products():
    if HANDLE:
        q, n = f"handle:{HANDLE}", 1
    else:
        q, n = f"-tag:{DONE_TAG}", BATCH_SIZE
    nodes = gql(SELECT_Q, {"n": n, "q": q})["products"]["nodes"]
    return [p for p in nodes if p.get("featuredImage") and p["featuredImage"].get("url")]

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
  productReorderMedia(id: $id, moves: $moves) { userErrors { field message } }
}"""
TAG_M = """
mutation($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
}"""

def upload_and_attach(product_id, png_bytes, handle):
    fn = f"{handle}-smaak.png"
    t = gql(STAGED_M, {"input": [{"filename": fn, "mimeType": "image/png",
                                  "httpMethod": "POST", "resource": "IMAGE"}]})
    tgt = t["stagedUploadsCreate"]["stagedTargets"][0]
    form = {p["name"]: p["value"] for p in tgt["parameters"]}
    requests.post(tgt["url"], data=form, files={"file": (fn, png_bytes, "image/png")}).raise_for_status()
    d = gql(CREATE_M, {"productId": product_id,
                       "media": [{"mediaContentType": "IMAGE", "originalSource": tgt["resourceUrl"],
                                  "alt": "Wat je proeft"}]})
    if d["productCreateMedia"]["mediaUserErrors"]:
        raise RuntimeError(d["productCreateMedia"]["mediaUserErrors"])
    new_id = d["productCreateMedia"]["media"][0]["id"]
    gql(REORDER_M, {"id": product_id, "moves": [{"id": new_id, "newPosition": "1"}]})
    gql(TAG_M, {"id": product_id, "tags": [DONE_TAG]})


# ------------------------- OpenAI -------------------------
def _openai_post(url, tries=5, **kwargs):
    r = None
    for attempt in range(tries):
        r = requests.post(url, **kwargs)
        if r.status_code < 400:
            return r
        body = r.text
        if r.status_code == 429 and "insufficient_quota" in body:
            raise RuntimeError("OpenAI weigert: geen tegoed/quota op deze API-sleutel. Stel billing in "
                               "en zet credits klaar in de OpenAI-console (en verifieer je organisatie).")
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("retry-after", 2 * (attempt + 1)))); continue
        raise RuntimeError(f"OpenAI {r.status_code}: {body[:400]}")
    raise RuntimeError(f"OpenAI bleef {r.status_code} geven na {tries} pogingen: {r.text[:300]}")

FLAVOR_SYS = (
    "Je bent sommelier. Analyseer de wijnbeschrijving en geef UITSLUITEND geldige JSON, geen uitleg, "
    "geen code-fences, in exact deze vorm:\n"
    '{"primair":[{"naam":"citroen","type":"fruit"}],"secundair":[{"naam":"vanille","type":"bloem"}]}\n'
    "Regels: 'primair' = aroma's uit de druif zelf (fruit, bloemen, citrus). 'secundair' = aroma's uit "
    "vinificatie/rijping (hout, vanille, brioche, boter, room, noten, toast). 'type' is een korte "
    "categorie (fruit, citrus, bloem, noot, hout, zuivel, kruid). Alleen concrete, fotografeerbare "
    "elementen. Maximaal 5 primair en 5 secundair."
)

def _load_meta():
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    merged = {}                                   # eenmalige migratie van oude losse bestanden
    if _LEGACY_META_DIR.exists():
        for f in _LEGACY_META_DIR.glob("*.json"):
            try:
                merged[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
    return merged

def _save_meta(meta):
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

def extract_flavors(handle, description):
    """Smaken per product; gecachet in flavor_meta.json (alle producten) -> rerun = geen tekst-call."""
    meta = _load_meta()
    if handle in meta and not BYPASS_CACHE:
        e = meta[handle]
        return e.get("primair", []), e.get("secundair", [])
    prim, sec = _extract_flavors_api(description)
    if prim or sec:
        meta = _load_meta()                        # herlaad vlak voor schrijven -> jouw edits blijven behouden
        meta[handle] = {"primair": prim, "secundair": sec}
        _save_meta(meta)
    return prim, sec

def _extract_flavors_api(description):
    text = html.unescape(re.sub(r"<[^>]+>", " ", description or "")).strip()[:2000]
    if not text:
        return [], []
    r = _openai_post("https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        data=json.dumps({"model": OPENAI_TEXT_MODEL, "temperature": 0.3,
                         "response_format": {"type": "json_object"},
                         "messages": [{"role": "system", "content": FLAVOR_SYS},
                                      {"role": "user", "content": text}]}), timeout=60)
    content = re.sub(r"^```(?:json)?|```$", "", r.json()["choices"][0]["message"]["content"].strip()).strip()
    try:
        data = json.loads(content)
        prim = [x for x in data.get("primair", []) if x.get("naam")][:5]
        sec  = [x for x in data.get("secundair", []) if x.get("naam")][:5]
    except Exception:
        words = [{"naam": w.strip(" ."), "type": "overig"} for w in re.split(r"[,\n]", content) if w.strip(" .")][:6]
        prim, sec = words[: (len(words)+1)//2], words[(len(words)+1)//2:]
    return prim, sec

def _balance(left, right):
    """Herverdeel zodat |links - rechts| <= 1 (bijv. 4/1 -> 3/2), met minimale verplaatsing."""
    left, right = list(left), list(right)
    while len(left) - len(right) >= 2:
        right.insert(0, left.pop())
    while len(right) - len(left) >= 2:
        left.append(right.pop(0))
    return left, right

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "smaak"

_fresh = set()   # smaken die deze run al opnieuw zijn gemaakt (voorkomt dubbel werk bij BYPASS_CACHE)

def get_flavor_cutout(naam, typ):
    """Transparante uitsnede per smaak; gecachet per naam+bron. Bron: GPT of echte stockfoto."""
    CACHE_DIR.mkdir(exist_ok=True)
    key = f"{_slug(naam)}-{CUTOUT_SOURCE}"
    fp = CACHE_DIR / f"{key}.png"
    if fp.exists() and (not BYPASS_CACHE or key in _fresh):
        return Image.open(fp).convert("RGBA")
    img = _stock_cutout(naam) if CUTOUT_SOURCE == "stock" else _gpt_cutout(naam, typ)
    img.save(fp); _fresh.add(key)
    return img

def _hex_rgb(h):
    h = h.lstrip("#"); return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _gpt_cutout(naam, typ):
    prompt = (
        f"Een professionele macro-foodfoto van {naam} ({typ}), gestyled als een klein, natuurlijk groepje "
        "van een paar hele stuks dicht bij elkaar, zoals premium food styling — dus niet één enkel "
        "geïsoleerd exemplaar. GEEN kruimels, korrels, poeder, zand of verstrooiing eromheen, GEEN "
        "ondergrond of oppervlak en GEEN schaduw op een ondergrond: alleen de schoon uitgesneden hele "
        "stuks. Geschoten met een DSLR en macro-objectief, zacht daglicht, ondiepe scherptediepte. Echte "
        "fotografie met natuurlijke textuur, poriën, glans en realistische kleur. Losjes gecentreerd. "
        "GEEN 3D-render, GEEN CGI, GEEN illustratie, GEEN klei of was, GEEN cartoon, GEEN glad plastic "
        "uiterlijk, GEEN tekst, GEEN verpakking."
    )
    solid = "image-2" in OPENAI_IMAGE_MODEL          # gpt-image-2 ondersteunt geen transparante achtergrond
    if solid:
        prompt += f" De achtergrond is één egale, effen kleur {BG_COLOR_HEX}, zonder objecten of schaduw."
    body = {"model": OPENAI_IMAGE_MODEL, "prompt": prompt, "size": "1024x1024",
            "background": "opaque" if solid else "transparent",
            "output_format": "png", "quality": IMAGE_QUALITY, "n": 1}
    r = _openai_post("https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        data=json.dumps(body), timeout=300)
    img = Image.open(io.BytesIO(base64.b64decode(r.json()["data"][0]["b64_json"]))).convert("RGBA")
    if solid:
        img = _remove_solid_bg(img, _hex_rgb(BG_COLOR_HEX))
    return img

def _remove_solid_bg(im, bg, tol=45):
    """Egale achtergrond wegknippen -> transparant. rembg indien beschikbaar, anders kleur-key vanaf de randen."""
    im = im.convert("RGBA")
    try:
        from rembg import remove
        return remove(im).convert("RGBA")
    except ImportError:
        w, h = im.size
        tmp = im.convert("RGB")
        SENT = (1, 254, 2)
        for c in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
            ImageDraw.floodfill(tmp, c, SENT, thresh=tol)
        amask = Image.frombytes("L", (w, h), bytes(0 if p == SENT else 255 for p in tmp.getdata()))
        amask = amask.filter(ImageFilter.MinFilter(3))    # trim 1px achtergrondrand
        im.putalpha(amask)
        return im

def _stock_cutout(naam):
    """Echte foto van Pexels ophalen en de achtergrond wegknippen (rembg) -> transparant."""
    if not PEXELS_API_KEY:
        raise RuntimeError("CUTOUT_SOURCE=stock vereist PEXELS_API_KEY (gratis via pexels.com/api).")
    r = requests.get("https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": f"{naam} isolated on white background", "per_page": 1,
                "orientation": "square"}, timeout=30)
    r.raise_for_status()
    photos = r.json().get("photos", [])
    if not photos:
        raise RuntimeError(f"geen stockfoto gevonden voor '{naam}'")
    src = photos[0]["src"]
    raw = requests.get(src.get("large2x") or src.get("large") or src["original"], timeout=30).content
    try:
        from rembg import remove
    except ImportError:
        raise RuntimeError("CUTOUT_SOURCE=stock vereist rembg (pip install rembg onnxruntime).")
    return remove(Image.open(io.BytesIO(raw)).convert("RGBA")).convert("RGBA")


# ------------------------- Compositie -------------------------
def _trim(im):
    im = im.convert("RGBA")
    bb = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    return im.crop(bb) if bb else im

def _fit(im, box):
    im = _trim(im); w, h = im.size; s = box / max(w, h)
    return im.resize((max(1, round(w*s)), max(1, round(h*s))), Image.LANCZOS)

def _drop_shadow(cut, blur=18, opacity=90, offset=(10, 22)):
    w, h = cut.size
    pad = blur * 3 + max(abs(offset[0]), abs(offset[1]))     # ruimte zodat blur/offset niet afkappen
    sh = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    sh.paste(Image.new("RGBA", (w, h), (0, 0, 0, opacity)),
             (pad + offset[0], pad + offset[1]), cut.getchannel("A"))
    return sh.filter(ImageFilter.GaussianBlur(blur)), pad

def _by_type(items):
    return sorted(items, key=lambda x: x.get("type", "zzz"))   # gelijke types bij elkaar

def _place_column(cv, items, cx, seed):
    """Organische kolom: wisselende grootte, lichte rotatie/jitter, contactschaduw."""
    if not items:
        return
    rnd = random.Random(seed)
    N = cv.size[1]
    top, bottom = int(N * 0.07), int(N * 0.93)
    band = (bottom - top) / len(items)
    for i, it in enumerate(_by_type(items)):
        size = int(FLAVOR_PX * rnd.uniform(0.82, 1.18))
        cut = _fit(get_flavor_cutout(it["naam"], it.get("type", "")), size)
        cut = cut.rotate(rnd.uniform(-11, 11), expand=True, resample=Image.BICUBIC)
        cy = int(top + band * (i + 0.5) + rnd.uniform(-band * 0.10, band * 0.10))
        x = int(cx + rnd.uniform(-N * 0.028, N * 0.028)) - cut.width // 2
        y = cy - cut.height // 2
        sh, pad = _drop_shadow(cut)
        cv.alpha_composite(sh, (x - pad, y - pad))
        cv.alpha_composite(cut, (x, y))

def compose(bottle_img, prim, sec, seed=0):
    N = FINAL_SIZE
    fill = (_hex_rgb(BG_COLOR_HEX) + (255,)) if "image-2" in OPENAI_IMAGE_MODEL else (0, 0, 0, 0)
    cv = Image.new("RGBA", (N, N), fill)      # gpt-image-2: hele achtergrond #EFF0F5; anders transparant
    cxL, cxR = int(COL_MARGIN * N), int((1 - COL_MARGIN) * N)
    _place_column(cv, prim, cxL, seed)         # primair links
    _place_column(cv, sec, cxR, seed + 1)      # secundair rechts
    b = _trim(bottle_img); w, h = b.size        # echte fles bovenop, exact BOTTLE_PX hoog
    nw = max(1, round(w * BOTTLE_PX / h))
    b = b.resize((nw, BOTTLE_PX), Image.LANCZOS)
    cv.alpha_composite(b, ((N - nw) // 2, (N - BOTTLE_PX) // 2))
    return cv


# ------------------------- Hoofdroutine -------------------------
def main():
    global _access_token
    if not CLIENT_ID or not CLIENT_SECRET or SHOP.startswith("jouwwinkel"):
        sys.exit("Ontbrekende SHOP / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET.")
    if not OPENAI_API_KEY:
        sys.exit("Ontbrekende OPENAI_API_KEY.")
    _access_token = get_access_token()
    BACKUP_DIR.mkdir(exist_ok=True)

    products = select_products()
    print(f"== {'DRY-RUN' if DRY_RUN else 'LIVE'} | model {OPENAI_IMAGE_MODEL} ({IMAGE_QUALITY}) "
          f"| fles {BOTTLE_PX}px op {FINAL_SIZE} | smaak {FLAVOR_PX}px | {len(products)} fles(sen) ==\n")
    if DRY_RUN:
        print("Let op: DRY-RUN genereert nieuwe smaken (OpenAI-kosten, maar gecacht), upload/tagt niet.\n")

    done = failed = 0
    for p in products:
        try:
            prim, sec = extract_flavors(p["handle"], p.get("description"))
            if not prim and not sec:
                print(f"[skip] {p['handle']}: geen smaken in beschrijving"); continue
            prim, sec = _balance(prim, sec)
            raw = requests.get(p["featuredImage"]["url"], timeout=30).content
            final = compose(Image.open(io.BytesIO(raw)), prim, sec, seed=zlib.crc32(p["handle"].encode()))
            final.save(BACKUP_DIR / f"{p['handle']}-smaak.png", "PNG", optimize=True)
            names = lambda xs: ", ".join(x["naam"] for x in xs) or "-"
            print(f"[{'dry' if DRY_RUN else 'ok'}] {p['handle']}  | links: {names(prim)}  | rechts: {names(sec)}")
            if not DRY_RUN:
                buf = io.BytesIO(); final.save(buf, "PNG", optimize=True)
                upload_and_attach(p["id"], buf.getvalue(), p["handle"])
            done += 1
        except Exception as e:
            print(f"[ERR] {p.get('handle')}: {e}"); failed += 1

    print(f"\nKlaar. Verwerkt: {done} | fouten: {failed}")
    print(f"Beelden in: {BACKUP_DIR.resolve()} | cache: {CACHE_DIR.resolve()}")

if __name__ == "__main__":
    main()
