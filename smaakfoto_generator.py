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
SYN_FILE       = pathlib.Path("synonyms.json")    # variant -> canonieke smaak (bespaart generaties)
MERGE_COLOR_VARIANTS = env_bool("MERGE_COLOR_VARIANTS", True)  # ook kleur/rijpheid samenvoegen (rode+zwarte kers -> kers)

# --- kosten (USD; beeld-tokens komen echt uit de API-respons "usage") ---
IMG_RATES = {   # per token: in = prompt/input, out = gegenereerd beeld (output)
    "gpt-image-1.5":    {"in": 5.0 / 1e6, "out": 32.0 / 1e6},
    "gpt-image-1-mini": {"in": 2.5 / 1e6, "out": 2.5 / 1e6},
    "gpt-image-2":      {"in": 5.0 / 1e6, "out": 32.0 / 1e6},
}
TXT_IN_RATE, TXT_OUT_RATE = 0.15 / 1e6, 0.60 / 1e6            # gpt-4o-mini
_cost = {"img": 0, "img_in": 0, "img_out": 0, "txt_calls": 0, "txt_in": 0, "txt_out": 0}

FINAL_SIZE     = env_int("FINAL_SIZE", 2048)     # canvas (vierkant)
BOTTLE_PX      = env_int("BOTTLE_PX", 2000)      # fleshoogte in px
FLAVOR_PX      = env_int("FLAVOR_PX", 260)       # max grootte van een smaak-uitsnede (subtiliteit)
COL_MARGIN     = float(env("COL_MARGIN", "0.16"))# kolomcenter t.o.v. canvasbreedte (links/rechts)

BATCH_SIZE     = env_int("BATCH_SIZE", 1)
HANDLE         = env("HANDLE", "")
DONE_TAG       = env("DONE_TAG", "smaakfoto")
OVERWRITE      = env_bool("OVERWRITE", False)   # True = selecteer producten MET de tag en vervang hun smaakfoto
AROMA_STYLE    = env("AROMA_STYLE", "kolommen") # kolommen|krans|explosie|geometrisch|aromawolk|kleurverloop|rook

# per stijl: fleshoogte + verticale verankering (center of bottom)
STYLES = {
    "kolommen":    {"bottle_px": 2000, "anchor": "center"},
    "krans":       {"bottle_px": 1200, "anchor": "center"},
    "explosie":    {"bottle_px": 1250, "anchor": "bottom"},
    "geometrisch": {"bottle_px": 1500, "anchor": "center"},
    "aromawolk":   {"bottle_px": 1250, "anchor": "bottom"},
    "kleurverloop":{"bottle_px": 1300, "anchor": "bottom"},
    "rook":        {"bottle_px": 1300, "anchor": "bottom"},
}

# kleuren per smaaktype (legenda's + geometrische stijl)
TYPE_COLORS = {
    "fruit": (140, 30, 50), "citrus": (212, 160, 40), "bloem": (200, 120, 140),
    "noot": (150, 100, 60), "hout": (110, 80, 50), "zuivel": (225, 214, 190),
    "kruid": (95, 115, 60), "overig": (120, 90, 90),
}
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
    nodes {
      id title handle description featuredImage { url }
      media(first: 20) { nodes { ... on MediaImage { id alt } } }
    }
  }
}"""

DELETE_MEDIA_M = """
mutation($productId: ID!, $mediaIds: [ID!]!) {
  productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
    deletedMediaIds mediaUserErrors { field message }
  }
}"""

SMAAK_ALT = "Wat je proeft"   # hieraan herkennen we de bestaande smaakfoto

def select_products():
    if HANDLE:
        q, n = f"handle:{HANDLE}", 1
    elif OVERWRITE:
        q, n = f"tag:{DONE_TAG}", BATCH_SIZE     # vervang bestaande smaakfoto's
    else:
        q, n = f"-tag:{DONE_TAG}", BATCH_SIZE    # alleen nog-niet-verwerkte
    nodes = gql(SELECT_Q, {"n": n, "q": q})["products"]["nodes"]
    return [p for p in nodes if p.get("featuredImage") and p["featuredImage"].get("url")]

def old_smaak_media_ids(p):
    return [m["id"] for m in p.get("media", {}).get("nodes", [])
            if m and m.get("alt") == SMAAK_ALT]

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

def upload_and_attach(product_id, png_bytes, handle, old_ids=None):
    fn = f"{handle}-smaak.png"
    t = gql(STAGED_M, {"input": [{"filename": fn, "mimeType": "image/png",
                                  "httpMethod": "POST", "resource": "IMAGE"}]})
    tgt = t["stagedUploadsCreate"]["stagedTargets"][0]
    form = {p["name"]: p["value"] for p in tgt["parameters"]}
    requests.post(tgt["url"], data=form, files={"file": (fn, png_bytes, "image/png")}).raise_for_status()
    d = gql(CREATE_M, {"productId": product_id,
                       "media": [{"mediaContentType": "IMAGE", "originalSource": tgt["resourceUrl"],
                                  "alt": SMAAK_ALT}]})
    if d["productCreateMedia"]["mediaUserErrors"]:
        raise RuntimeError(d["productCreateMedia"]["mediaUserErrors"])
    new_id = d["productCreateMedia"]["media"][0]["id"]
    gql(REORDER_M, {"id": product_id, "moves": [{"id": new_id, "newPosition": "1"}]})
    gql(TAG_M, {"id": product_id, "tags": [DONE_TAG]})
    if old_ids:                                   # oude smaakfoto('s) pas weg als de nieuwe staat
        gql(DELETE_MEDIA_M, {"productId": product_id, "mediaIds": old_ids})


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

_SYN = None
def _load_synonyms():
    if not SYN_FILE.exists():
        return {}
    d = json.loads(SYN_FILE.read_text(encoding="utf-8"))
    m = dict(d.get("merge", {}))
    if MERGE_COLOR_VARIANTS:
        m.update(d.get("merge_colors", {}))
    return m

def _canonical(naam):
    """Map een smaaknaam naar zijn canonieke vorm (''=laten vallen). Volgt ketens als 'zwarte kersen'->'kers'."""
    global _SYN
    if _SYN is None:
        _SYN = _load_synonyms()
    key = (naam or "").strip().lower()
    for _ in range(5):
        if key in _SYN:
            key = _SYN[key]
        else:
            break
    return key

def _prep_flavors(prim, sec):
    """Canoniseer namen, laat abstracte smaken vallen en ontdubbel per wijn (primair heeft voorrang)."""
    seen = set(); out_p = []; out_s = []
    for side, out in ((prim, out_p), (sec, out_s)):
        for it in side:
            c = _canonical(it.get("naam", ""))
            if not c or c in seen:
                continue
            seen.add(c); out.append({"naam": c, "type": it.get("type", "")})
    return out_p, out_s

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
    resp = r.json()
    u = resp.get("usage", {})
    _cost["txt_calls"] += 1
    _cost["txt_in"] += u.get("prompt_tokens", 0)
    _cost["txt_out"] += u.get("completion_tokens", 0)
    content = re.sub(r"^```(?:json)?|```$", "", resp["choices"][0]["message"]["content"].strip()).strip()
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
    resp = r.json()
    uu = resp.get("usage", {})
    _cost["img"] += 1
    _cost["img_in"] += uu.get("input_tokens", 0)
    _cost["img_out"] += uu.get("output_tokens", 0)
    img = Image.open(io.BytesIO(base64.b64decode(resp["data"][0]["b64_json"]))).convert("RGBA")
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

def _style_cfg():
    return STYLES.get(AROMA_STYLE, STYLES["kolommen"])

def _bottle_px():
    return env_int("BOTTLE_PX", _style_cfg()["bottle_px"])

def _paste_bottle(cv, bottle_img):
    """Echte fles bovenop; hoogte per stijl, center- of bottom-verankerd."""
    N = cv.size[0]
    bp = _bottle_px()
    b = _trim(bottle_img); w, h = b.size
    nw = max(1, round(w * bp / h))
    b = b.resize((nw, bp), Image.LANCZOS)
    y = (N - bp) // 2 if _style_cfg()["anchor"] == "center" else N - bp - 40
    cv.alpha_composite(b, ((N - nw) // 2, y))
    return ((N - nw) // 2, y, nw, bp)            # flespositie voor stijlen die eromheen werken

def _new_canvas():
    N = FINAL_SIZE
    fill = (_hex_rgb(BG_COLOR_HEX) + (255,)) if "image-2" in OPENAI_IMAGE_MODEL else (0, 0, 0, 0)
    return Image.new("RGBA", (N, N), fill)

def _place_cutout(cv, it, cx, cy, size, angle, shadow=True):
    cut = _fit(get_flavor_cutout(it["naam"], it.get("type", "")), size)
    if angle:
        cut = cut.rotate(angle, expand=True, resample=Image.BICUBIC)
    x, y = cx - cut.width // 2, cy - cut.height // 2
    if shadow:
        sh, pad = _drop_shadow(cut)
        cv.alpha_composite(sh, (x - pad, y - pad))
    cv.alpha_composite(cut, (x, y))

def _wine_color(bottle_img):
    """rood/wit/rose op basis van de gemiddelde kleur van het midden van de fles."""
    b = _trim(bottle_img).convert("RGBA")
    w, h = b.size
    box = b.crop((int(w*0.30), int(h*0.45), int(w*0.70), int(h*0.75)))
    px = [p for p in box.getdata() if p[3] > 200]
    if not px:
        return "rood"
    r = sum(p[0] for p in px)/len(px); g = sum(p[1] for p in px)/len(px); bl = sum(p[2] for p in px)/len(px)
    if r > 120 and g > 90 and bl < g:            # goud/geel -> wit
        return "wit"
    if r > 140 and g < 110 and bl < 130 and r - g > 60:
        return "rose" if g > 70 else "rood"
    return "rood"

ASSET_PROMPTS = {
    "aromawolk":   "Een zachte, dromerige aquarel-aromawolk die omhoog kringelt, dicht bij de basis en "
                   "uitwaaierend naar boven, in {palet}. Fijne pigmentnevel, organische randen.",
    "kleurverloop":"Sierlijke, gelaagde zijdeachtige rooklinten die omhoog stromen en uitwaaieren, "
                   "meerdere vloeiende banen naast elkaar in {palet}, elegant en luchtig.",
    "rook":        "IJle, elegante parfumrook: dunne kronkelende slierten die omhoog dansen en vervagen, "
                   "in {palet}, subtiel en verfijnd met fijne gouden spikkels.",
}
PALETTES = {
    "rood": "diep bordeauxrood, pruimpaars, amber en een vleug olijfgroen",
    "wit":  "goudgeel, zachtgroen, ivoor en licht amber",
    "rose": "zachtroze, framboos, perzik en licht goud",
}

def _style_asset(style, kleur):
    """Herbruikbare sfeer-asset (wolk/linten/rook) per stijl+wijnkleur; 1x genereren, daarna cache."""
    CACHE_DIR.mkdir(exist_ok=True)
    fp = CACHE_DIR / f"asset-{style}-{kleur}-gpt.png"
    if fp.exists() and not BYPASS_CACHE:
        return Image.open(fp).convert("RGBA")
    prompt = (ASSET_PROMPTS[style].format(palet=PALETTES[kleur]) +
              " Op een VOLLEDIG TRANSPARANTE achtergrond, niets anders in beeld: geen fles, geen tekst, "
              "geen objecten, geen ondergrond.")
    body = {"model": OPENAI_IMAGE_MODEL, "prompt": prompt, "size": "1024x1536",
            "background": "transparent", "output_format": "png", "quality": IMAGE_QUALITY, "n": 1}
    if "image-2" in OPENAI_IMAGE_MODEL:
        body["background"] = "opaque"
        body["prompt"] = body["prompt"].replace("VOLLEDIG TRANSPARANTE achtergrond",
                                                f"egale achtergrond in kleur {BG_COLOR_HEX}")
    r = _openai_post("https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        data=json.dumps(body), timeout=300)
    resp = r.json(); uu = resp.get("usage", {})
    _cost["img"] += 1; _cost["img_in"] += uu.get("input_tokens", 0); _cost["img_out"] += uu.get("output_tokens", 0)
    img = Image.open(io.BytesIO(base64.b64decode(resp["data"][0]["b64_json"]))).convert("RGBA")
    if "image-2" in OPENAI_IMAGE_MODEL:
        img = _remove_solid_bg(img, _hex_rgb(BG_COLOR_HEX))
    img.save(fp)
    return img

def _font(size, variant=""):
    base = "/usr/share/fonts/truetype/dejavu/DejaVuSans"
    try:
        from PIL import ImageFont
        return ImageFont.truetype(f"{base}{variant}.ttf", size)
    except Exception:
        from PIL import ImageFont
        return ImageFont.load_default()

def _legend(cv, items, x, top, bottom, dots=True):
    """Pillow-legenda: gekleurde stip + smaaknaam (gratis, geen AI)."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(cv)
    f = _font(46, "-Bold"); f2 = _font(34, "-Oblique")
    n = max(len(items), 1); step = (bottom - top) / n
    ink = (60, 45, 45, 255)
    for i, it in enumerate(items):
        cy = int(top + step * (i + 0.5))
        col = TYPE_COLORS.get(it.get("type", "overig"), TYPE_COLORS["overig"])
        tx = x
        if dots:
            d.ellipse([x, cy - 16, x + 32, cy + 16], fill=col + (255,))
            tx = x + 56
        d.text((tx, cy - 26), it["naam"].upper(), font=f, fill=ink)
        if not dots:
            d.text((tx, cy + 20), it.get("type", ""), font=f2, fill=(120, 100, 100, 255))

# --- stijl-composers ---
def _compose_kolommen(cv, bottle, prim, sec, seed):
    N = cv.size[0]
    cxL, cxR = int(COL_MARGIN * N), int((1 - COL_MARGIN) * N)
    _place_column(cv, prim, cxL, seed)
    _place_column(cv, sec, cxR, seed + 1)
    _paste_bottle(cv, bottle)

def _compose_krans(cv, bottle, prim, sec, seed):
    import math
    N = cv.size[0]; items = _by_type(prim + sec); n = max(len(items), 1)
    rnd = random.Random(seed)
    rx, ry = N * 0.36, N * 0.38
    for i, it in enumerate(items):                            # hoofdring
        a = -math.pi / 2 + 2 * math.pi * i / n
        cx = int(N / 2 + rx * math.cos(a)); cy = int(N / 2 + ry * math.sin(a))
        _place_cutout(cv, it, cx, cy, int(FLAVOR_PX * rnd.uniform(0.75, 1.0)), rnd.uniform(-14, 14))
    for i in range(n):                                        # verdichting: kleine herhalingen ertussen
        it = items[(i + 2) % n]                               # ander item dan de buren
        a = -math.pi / 2 + 2 * math.pi * (i + 0.5) / n
        f = rnd.uniform(0.88, 1.10)                           # afwisselend iets binnen/buiten de ring
        cx = int(N / 2 + rx * f * math.cos(a)); cy = int(N / 2 + ry * f * math.sin(a))
        _place_cutout(cv, it, cx, cy, int(FLAVOR_PX * rnd.uniform(0.38, 0.50)),
                      rnd.uniform(-25, 25), shadow=False)
    _paste_bottle(cv, bottle)

def _compose_explosie(cv, bottle, prim, sec, seed):
    import math
    N = cv.size[0]; items = prim + sec
    rnd = random.Random(seed)
    bp = _bottle_px()
    neck = (N // 2, N - 40 - bp + int(bp * 0.04))           # rond de flessenhals
    max_r = neck[1] - int(N * 0.05)                          # tot vlak onder de bovenrand
    placed = []                                              # (cx, cy, size) voor botsingscontrole

    def try_place(it, size, r_lo, r_hi):
        best = None; best_d = -1e18
        for _ in range(90):
            a = math.radians(rnd.uniform(195, 345))          # bovenste helft
            r = rnd.uniform(r_lo, r_hi) * max_r
            cx = int(neck[0] + r * math.cos(a)); cy = int(neck[1] + r * math.sin(a))
            cx = max(size // 2 + 20, min(N - size // 2 - 20, cx))
            cy = max(size // 2 + 20, min(neck[1] - size // 4, cy))
            d = min((math.hypot(cx - px, cy - py) - (size + ps) * 0.50
                     for px, py, ps in placed), default=1e9)
            if d >= 0:                                        # geen overlap -> meteen goed
                placed.append((cx, cy, size)); return cx, cy
            if d > best_d:
                best_d, best = d, (cx, cy)
        placed.append((best[0], best[1], size)); return best  # minst overlappende plek

    order = _by_type(items)
    for it in order:                                          # groot, dicht bij de hals
        size = int(FLAVOR_PX * rnd.uniform(0.85, 1.05))
        cx, cy = try_place(it, size, 0.18, 0.55)
        _place_cutout(cv, it, cx, cy, size, rnd.uniform(-25, 25))
    for it in order:                                          # klein, verder naar buiten
        size = int(FLAVOR_PX * rnd.uniform(0.40, 0.55))
        cx, cy = try_place(it, size, 0.55, 0.95)
        _place_cutout(cv, it, cx, cy, size, rnd.uniform(-35, 35), shadow=False)
    _paste_bottle(cv, bottle)

def _compose_geometrisch(cv, bottle, prim, sec, seed):
    from PIL import ImageDraw
    N = cv.size[0]; rnd = random.Random(seed)
    lay = Image.new("RGBA", (N, N), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
    gold = (196, 160, 90, 200)
    for _ in range(3):                                       # dunne gouden ringen
        r = rnd.randint(int(N*0.18), int(N*0.42)); cx = rnd.randint(int(N*0.25), int(N*0.75)); cy = rnd.randint(int(N*0.25), int(N*0.75))
        d.ellipse([cx-r, cy-r, cx+r, cy+r], outline=gold, width=4)
    for it in prim + sec:                                    # gekleurde cirkels per smaaktype
        col = TYPE_COLORS.get(it.get("type", "overig"), TYPE_COLORS["overig"])
        r = rnd.randint(80, 170)
        side = rnd.choice([rnd.uniform(0.08, 0.30), rnd.uniform(0.70, 0.92)])   # links of rechts van de fles
        cx = int(side * N); cy = rnd.randint(int(N*0.12), int(N*0.85))
        d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=col + (rnd.randint(170, 235),))
    for _ in range(4):                                       # kleine gouden accenten
        cx = rnd.randint(int(N*0.1), int(N*0.9)); cy = rnd.randint(int(N*0.1), int(N*0.9)); r = rnd.randint(10, 22)
        d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=gold)
    cv.alpha_composite(lay)
    _paste_bottle(cv, bottle)

def _compose_aromawolk(cv, bottle, prim, sec, seed):
    N = cv.size[0]; rnd = random.Random(seed)
    kleur = _wine_color(bottle)
    cloud = _fit(_style_asset("aromawolk", kleur), int(N * 0.62))
    bp = _bottle_px()
    # flesgeometrie vooraf, zodat cutouts de flescolom kunnen mijden
    b = _trim(bottle); bw, bh = b.size
    nw = max(1, round(bw * bp / bh))
    bottle_top = N - 40 - bp
    cloud_cx, cloud_cy = N // 2, max(cloud.height // 2 + 20, bottle_top - cloud.height // 2 + int(N*0.06))
    cv.alpha_composite(cloud, (cloud_cx - cloud.width // 2, cloud_cy - cloud.height // 2))
    items = _by_type(prim + sec)
    top = cloud_cy - int(cloud.height * 0.42)
    bottom = bottle_top + int(bp * 0.12)                    # tot net onder de flesmond
    band = max((bottom - top) / max(len(items), 1), 1)
    for i, it in enumerate(items):
        size = int(FLAVOR_PX * rnd.uniform(0.45, 0.62))
        side = -1 if i % 2 == 0 else 1                       # links/rechts afwisselen
        cy = int(top + band * (i + 0.5) + rnd.uniform(-band * 0.15, band * 0.15))
        cx = int(cloud_cx + side * rnd.uniform(0.16, 0.40) * cloud.width)
        if cy + size // 2 > bottle_top:                      # naast de hals? -> volledig buiten de flescolom
            min_off = nw // 2 + size // 2 + 24
            if abs(cx - N // 2) < min_off:
                cx = N // 2 + side * min_off
        _place_cutout(cv, it, cx, cy, size, rnd.uniform(-20, 20), shadow=False)
    _paste_bottle(cv, bottle)

def _compose_kleurverloop(cv, bottle, prim, sec, seed):
    N = cv.size[0]
    kleur = _wine_color(bottle)
    ribbons = _fit(_style_asset("kleurverloop", kleur), int(N * 0.66))
    bp = _bottle_px()
    cx = int(N * 0.40)                                       # fles + linten iets naar links, legenda rechts
    cv.alpha_composite(ribbons, (cx - ribbons.width // 2, max(10, N - 40 - bp - ribbons.height + int(bp*0.12))))
    _legend(cv, _by_type(prim + sec), int(N * 0.74), int(N * 0.16), int(N * 0.62), dots=True)
    b = _trim(bottle); w, h = b.size
    nw = max(1, round(w * bp / h)); b = b.resize((nw, bp), Image.LANCZOS)
    cv.alpha_composite(b, (cx - nw // 2, N - bp - 40))

def _compose_rook(cv, bottle, prim, sec, seed):
    N = cv.size[0]
    kleur = _wine_color(bottle)
    smoke = _fit(_style_asset("rook", kleur), int(N * 0.60))
    bp = _bottle_px()
    cx = int(N * 0.38)
    cv.alpha_composite(smoke, (cx - smoke.width // 3, max(10, N - 40 - bp - smoke.height + int(bp*0.10))))
    _legend(cv, _by_type(prim + sec), int(N * 0.72), int(N * 0.18), int(N * 0.70), dots=False)
    b = _trim(bottle); w, h = b.size
    nw = max(1, round(w * bp / h)); b = b.resize((nw, bp), Image.LANCZOS)
    cv.alpha_composite(b, (cx - nw // 2, N - bp - 40))

_COMPOSERS = {
    "kolommen": _compose_kolommen, "krans": _compose_krans, "explosie": _compose_explosie,
    "geometrisch": _compose_geometrisch, "aromawolk": _compose_aromawolk,
    "kleurverloop": _compose_kleurverloop, "rook": _compose_rook,
}

def compose(bottle_img, prim, sec, seed=0):
    cv = _new_canvas()
    _COMPOSERS.get(AROMA_STYLE, _compose_kolommen)(cv, bottle_img, prim, sec, seed)
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
    print(f"== {'DRY-RUN' if DRY_RUN else 'LIVE'} | stijl {AROMA_STYLE} | model {OPENAI_IMAGE_MODEL} ({IMAGE_QUALITY}) "
          f"| fles {_bottle_px()}px op {FINAL_SIZE} | overwrite: {OVERWRITE} | {len(products)} fles(sen) ==\n")
    if DRY_RUN:
        print("Let op: DRY-RUN genereert nieuwe smaken (OpenAI-kosten, maar gecacht), upload/tagt niet.\n")

    done = failed = 0
    for p in products:
        try:
            prim, sec = extract_flavors(p["handle"], p.get("description"))
            prim, sec = _prep_flavors(prim, sec)
            if not prim and not sec:
                print(f"[skip] {p['handle']}: geen (bruikbare) smaken in beschrijving"); continue
            prim, sec = _balance(prim, sec)
            raw = requests.get(p["featuredImage"]["url"], timeout=30).content
            final = compose(Image.open(io.BytesIO(raw)), prim, sec, seed=zlib.crc32(p["handle"].encode()))
            final.save(BACKUP_DIR / f"{p['handle']}-smaak.png", "PNG", optimize=True)
            names = lambda xs: ", ".join(x["naam"] for x in xs) or "-"
            print(f"[{'dry' if DRY_RUN else 'ok'}] {p['handle']}  | links: {names(prim)}  | rechts: {names(sec)}")
            if not DRY_RUN:
                buf = io.BytesIO(); final.save(buf, "PNG", optimize=True)
                upload_and_attach(p["id"], buf.getvalue(), p["handle"], old_ids=old_smaak_media_ids(p))
            done += 1
        except Exception as e:
            print(f"[ERR] {p.get('handle')}: {e}"); failed += 1

    print(f"\nKlaar. Verwerkt: {done} | fouten: {failed}")
    rates = IMG_RATES.get(OPENAI_IMAGE_MODEL, IMG_RATES["gpt-image-1.5"])
    img_cost = _cost["img_in"] * rates["in"] + _cost["img_out"] * rates["out"]
    txt_cost = _cost["txt_in"] * TXT_IN_RATE + _cost["txt_out"] * TXT_OUT_RATE
    print(f"OpenAI-kosten deze run: ~${img_cost + txt_cost:.2f} — "
          f"{_cost['img']} beelden ({_cost['img_in'] + _cost['img_out']:,} tokens), "
          f"{_cost['txt_calls']} extracties ({_cost['txt_in'] + _cost['txt_out']:,} tokens). "
          f"Cache-treffers zijn gratis.")
    print(f"Beelden in: {BACKUP_DIR.resolve()} | cache: {CACHE_DIR.resolve()}")

if __name__ == "__main__":
    main()
