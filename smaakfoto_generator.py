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

Kostenbeheersing: BATCH_SIZE (1/10/25/50/100/alle), HANDLE (1 fles), DONE_TAG (nooit dubbel),
en de cache. Verwerkte producten krijgen DONE_TAG.
"""

import os, io, sys, json, time, base64, html, re, pathlib, random, zlib, requests
try:
    sys.stdout.reconfigure(line_buffering=True)   # zonder dit blijft output hangen tot de buffer vol is/het script stopt
except Exception:
    pass
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

BATCH_SIZE     = env("BATCH_SIZE", "1")         # "1"|"10"|"25"|"50"|"100"|"alle"
HANDLE         = env("HANDLE", "")
WIJNHUIS       = env("WIJNHUIS", "")            # producentnaam (deelstring, hoofdletterongevoelig); overstemt BATCH_SIZE
DONE_TAG       = env("DONE_TAG", "smaakfoto")
OVERWRITE      = env_bool("OVERWRITE", False)   # True = selecteer producten MET de tag en vervang hun smaakfoto
AROMA_STYLE    = env("AROMA_STYLE", "kolommen") # kolommen|krans|explosie|geometrisch|aromawolk|kleurverloop|rook

# per stijl: fleshoogte + verticale verankering (center of bottom)
STYLES = {
    "kolommen":    {"bottle_px": 2000, "anchor": "center"},
    "krans":       {"bottle_px": 1200, "anchor": "center"},
    "explosie":    {"bottle_px": 1250, "anchor": "bottom"},
    "geometrisch": {"bottle_px": 1500, "anchor": "center"},
    "constellatie":{"bottle_px": 1500, "anchor": "center"},
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
query($n: Int!, $q: String, $cursor: String) {
  products(first: $n, after: $cursor, query: $q) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title handle description featuredImage { url } tags
      metafield(namespace: "shopify", key: "wine-variety") {
        references(first: 1) { nodes { ... on Metaobject { field(key: "label") { value } } } }
      }
      wijnhuis: metafield(namespace: "custom", key: "wijnhuis_new") {
        reference { ... on Metaobject { field(key: "bibi_graetz") { value } } }
      }
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

def _norm_wijnhuis(s):
    """Ongevoelig voor streepjes/underscores i.p.v. spaties (zoals een URL-slug) en voor accenten
    (São -> Sao), zodat 'quinta-sao-giao' matcht met de echte naam 'Quinta Sao Giao'."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()

def _select_all(q):
    """Paginering: Shopify geeft max 250 producten per pagina, dus doorbladeren voor 'alle'."""
    cursor = None; out = []
    while True:
        d = gql(SELECT_Q, {"n": 100, "q": q, "cursor": cursor})["products"]
        out.extend(d["nodes"])
        if not d["pageInfo"]["hasNextPage"]:
            break
        cursor = d["pageInfo"]["endCursor"]
    return out

def select_products():
    if HANDLE:
        nodes = gql(SELECT_Q, {"n": 1, "q": f"handle:{HANDLE}", "cursor": None})["products"]["nodes"]
    elif WIJNHUIS.strip():
        needle = _norm_wijnhuis(WIJNHUIS)
        def _wijnhuis_naam(p):
            mf = p.get("wijnhuis") or {}
            ref = mf.get("reference") or {}
            fld = ref.get("field") or {}
            return _norm_wijnhuis(fld.get("value") or "")
        alle_van_producent = [p for p in _select_all(None) if needle in _wijnhuis_naam(p)]   # los van de tag
        nodes = [p for p in alle_van_producent
                if (DONE_TAG in (p.get("tags") or [])) == OVERWRITE]                         # dan pas de tag toepassen
        if alle_van_producent and not nodes:
            klaar = sum(1 for p in alle_van_producent if DONE_TAG in (p.get("tags") or []))
            print(f"[info] {len(alle_van_producent)} wijn(en) gevonden voor '{WIJNHUIS}', maar allemaal al "
                  f"verwerkt ({klaar}/{len(alle_van_producent)} hebben de tag '{DONE_TAG}'). "
                  f"Zet 'overwrite' aan om ze opnieuw te genereren.")
    else:
        q = f"tag:{DONE_TAG}" if OVERWRITE else f"-tag:{DONE_TAG}"    # vervang bestaande / alleen nog-niet-verwerkte
        if BATCH_SIZE.strip().lower() == "alle":
            nodes = _select_all(q)
        else:
            nodes = gql(SELECT_Q, {"n": int(BATCH_SIZE), "q": q, "cursor": None})["products"]["nodes"]
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

def _bottle_rect(N):
    """Het rechthoekige gebied dat de fles inneemt (in canvas-coördinaten), voor botsingscontrole."""
    bp = _bottle_px()
    y0 = (N - bp) // 2 if _style_cfg()["anchor"] == "center" else N - bp - 40
    return (y0, y0 + bp)                                       # (top, bottom); horizontaal = rond het midden

def _avoid_bottle(cx, cy, size, N, nw, bottle_top, bottle_bottom, side=None, margin=24):
    """Laatste, universele veiligheidscheck: duwt een item radiaal weg van het midden als het
    (met een rotatiebestendige marge) alsnog over de fles zou vallen. Werkt voor elke stijl,
    ongeacht via welk pad (cx,cy) gevonden is."""
    half_extent = int(size * 0.66)                              # dekt rotatie tot ~20-25 graden
    if bottle_top - half_extent < cy < bottle_bottom + half_extent:
        min_off = nw // 2 + half_extent + margin
        if abs(cx - N // 2) < min_off:
            richting = side if side else (1 if cx >= N // 2 else -1)
            cx = N // 2 + richting * min_off
    return cx

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

def _place_cutout(cv, it, cx, cy, size, angle, shadow=True, shadow_blur=18, shadow_opacity=90, shadow_offset=(10, 22)):
    cut = _fit(get_flavor_cutout(it["naam"], it.get("type", "")), size)
    if angle:
        cut = cut.rotate(angle, expand=True, resample=Image.BICUBIC)
    x, y = cx - cut.width // 2, cy - cut.height // 2
    if shadow:
        sh, pad = _drop_shadow(cut, blur=shadow_blur, opacity=shadow_opacity, offset=shadow_offset)
        cv.alpha_composite(sh, (x - pad, y - pad))
    cv.alpha_composite(cut, (x, y))

def _product_wine_color(p):
    """Wijnkleur uit Shopify's eigen 'wine-variety'-metaveld (betrouwbaar); None als het ontbreekt
    of een echt onbekende waarde heeft -> aanroeper valt dan terug op pixels. Dekt de volledige
    taxonomie die in deze winkel gebruikt wordt: Rood, Wit, Rosé, Oranje, Mousserend."""
    try:
        nodes = p["metafield"]["references"]["nodes"]
        label = nodes[0]["field"]["value"].strip().lower()
    except (KeyError, IndexError, TypeError):
        return None
    return {
        "rood": "rood", "red": "rood",
        "wit": "wit", "white": "wit",
        "rose": "rose", "rosé": "rose", "rosee": "rose",
        "mousserend": "wit", "sparkling": "wit", "champagne": "wit",   # meestal witte druiven -> witte wolk
        "oranje": "rose", "orange": "rose",                            # amber/schilcontact -> dichtst bij rose-palet
        "versterkt": "rood", "fortified": "rood",
    }.get(label)

def _wine_color(bottle_img):
    """rood/wit/rose, bepaald door de GLADSTE band te kiezen (laagste lokale variantie).
    Een bedrukt etiket (tekst/randen/logo's) heeft veel lokale variatie; het wijnglas zelf
    is een vloeiende, egale kleurband. Zo mijden we per ongeluk het etiket bemonsteren."""
    import statistics
    b = _trim(bottle_img).convert("RGB")
    w, h = b.size
    band_h = max(4, int(h * 0.025))
    candidates = []
    for pct in range(8, 92, 4):
        y = int(h * pct / 100)
        y0, y1 = max(0, y - band_h // 2), min(h, y + band_h // 2)
        strip = b.crop((int(w * 0.30), y0, int(w * 0.70), y1))
        px = list(strip.getdata())
        if not px:
            continue
        lums = [0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2] for p in px]
        var = statistics.pvariance(lums)
        mean = (sum(p[0] for p in px) / len(px), sum(p[1] for p in px) / len(px), sum(p[2] for p in px) / len(px))
        candidates.append((var, mean))
    if not candidates:
        return "rood"
    candidates.sort(key=lambda c: c[0])           # laagste variantie eerst = meest waarschijnlijk kaal glas
    top = candidates[:5]                            # mediaan van de 5 gladste banden, ruis-robuust
    r = sum(c[1][0] for c in top) / len(top)
    g = sum(c[1][1] for c in top) / len(top)
    bl = sum(c[1][2] for c in top) / len(top)
    if r > 120 and g > 90 and bl < g:              # goud/geel -> wit
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

def _script_font(size):
    """Elegant lettertype voor labels (Fraunces Thin Italic, gebundeld in fonts/)."""
    from PIL import ImageFont
    path = pathlib.Path(__file__).parent / "fonts" / "FrauncesItalic.ttf"
    try:
        f = ImageFont.truetype(str(path), size)
        try:
            f.set_variation_by_name("Thin Italic")
        except Exception:
            pass
        return f
    except Exception:
        return _font(size, "-Oblique")

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
def _compose_kolommen(cv, bottle, prim, sec, seed, kleur_override=None):
    N = cv.size[0]
    cxL, cxR = int(COL_MARGIN * N), int((1 - COL_MARGIN) * N)
    _place_column(cv, prim, cxL, seed)
    _place_column(cv, sec, cxR, seed + 1)
    _paste_bottle(cv, bottle)

def _compose_krans(cv, bottle, prim, sec, seed, kleur_override=None):
    import math
    N = cv.size[0]; items = _by_type(prim + sec); n = max(len(items), 1)
    rnd = random.Random(seed)
    rx, ry = N * 0.36, N * 0.38
    b = _trim(bottle); bw, bh = b.size; bp = _bottle_px()
    nw = max(1, round(bw * bp / bh))
    bottle_top, bottle_bottom = _bottle_rect(N)
    for i, it in enumerate(items):                            # hoofdring
        a = -math.pi / 2 + 2 * math.pi * i / n
        cx = int(N / 2 + rx * math.cos(a)); cy = int(N / 2 + ry * math.sin(a))
        size = int(FLAVOR_PX * rnd.uniform(0.75, 1.0))
        cx = _avoid_bottle(cx, cy, size, N, nw, bottle_top, bottle_bottom)
        _place_cutout(cv, it, cx, cy, size, rnd.uniform(-14, 14))
    for i in range(n):                                        # verdichting: kleine herhalingen ertussen
        it = items[(i + 2) % n]                               # ander item dan de buren
        a = -math.pi / 2 + 2 * math.pi * (i + 0.5) / n
        f = rnd.uniform(0.88, 1.10)                           # afwisselend iets binnen/buiten de ring
        cx = int(N / 2 + rx * f * math.cos(a)); cy = int(N / 2 + ry * f * math.sin(a))
        size = int(FLAVOR_PX * rnd.uniform(0.38, 0.50))
        cx = _avoid_bottle(cx, cy, size, N, nw, bottle_top, bottle_bottom)
        _place_cutout(cv, it, cx, cy, size, rnd.uniform(-25, 25), shadow=False)
    _paste_bottle(cv, bottle)

def _compose_explosie(cv, bottle, prim, sec, seed, kleur_override=None):
    import math
    N = cv.size[0]; items = prim + sec
    rnd = random.Random(seed)
    bp = _bottle_px()
    bottle_top, _bottom = _bottle_rect(N)
    neck = (N // 2, bottle_top + int(bp * 0.04))             # rond de flessenhals (voor hoek/straal-berekening)
    max_r = neck[1] - int(N * 0.05)                          # tot vlak onder de bovenrand
    placed = []                                              # (cx, cy, size) voor botsingscontrole

    def try_place(it, size, r_lo, r_hi, max_angle):
        half_extent = int(size * 0.70)                        # rotatiebestendige marge (tot ~35-40 graden)
        cy_cap = bottle_top - half_extent - 10                # nooit dichter bij de fles dan dit, ongeacht rotatie
        best = None; best_d = -1e18
        for _ in range(90):
            a = math.radians(rnd.uniform(195, 345))          # bovenste helft
            r = rnd.uniform(r_lo, r_hi) * max_r
            cx = int(neck[0] + r * math.cos(a)); cy = int(neck[1] + r * math.sin(a))
            cx = max(size // 2 + 20, min(N - size // 2 - 20, cx))
            cy = max(size // 2 + 20, min(cy_cap, cy))
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
        cx, cy = try_place(it, size, 0.18, 0.55, 25)
        _place_cutout(cv, it, cx, cy, size, rnd.uniform(-25, 25))
    for it in order:                                          # klein, verder naar buiten
        size = int(FLAVOR_PX * rnd.uniform(0.40, 0.55))
        cx, cy = try_place(it, size, 0.55, 0.95, 35)
        _place_cutout(cv, it, cx, cy, size, rnd.uniform(-35, 35), shadow=False)
    _paste_bottle(cv, bottle)

def _compose_geometrisch(cv, bottle, prim, sec, seed, kleur_override=None):
    from PIL import ImageDraw
    N = cv.size[0]; rnd = random.Random(seed)
    b = _trim(bottle); bw, bh = b.size; bp = _bottle_px()
    nw = max(1, round(bw * bp / bh))
    bottle_top, bottle_bottom = _bottle_rect(N)
    lay = Image.new("RGBA", (N, N), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
    gold = (196, 160, 90, 200)
    for _ in range(3):                                       # dunne gouden ringen
        r = rnd.randint(int(N*0.18), int(N*0.42)); cx = rnd.randint(int(N*0.25), int(N*0.75)); cy = rnd.randint(int(N*0.25), int(N*0.75))
        d.ellipse([cx-r, cy-r, cx+r, cy+r], outline=gold, width=4)
    for it in prim + sec:                                    # gekleurde cirkels per smaaktype
        col = TYPE_COLORS.get(it.get("type", "overig"), TYPE_COLORS["overig"])
        r = rnd.randint(80, 170)
        side = -1 if rnd.random() < 0.5 else 1                # links of rechts van de fles
        cx = int(side * rnd.uniform(0.08, 0.30) * N + (N if side > 0 else 0))
        cy = rnd.randint(int(N*0.12), int(N*0.85))
        cx = _avoid_bottle(cx, cy, r * 2, N, nw, bottle_top, bottle_bottom, side=side, margin=10)
        d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=col + (rnd.randint(170, 235),))
    for _ in range(4):                                       # kleine gouden accenten
        cx = rnd.randint(int(N*0.1), int(N*0.9)); cy = rnd.randint(int(N*0.1), int(N*0.9)); r = rnd.randint(10, 22)
        d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=gold)
    cv.alpha_composite(lay)
    _paste_bottle(cv, bottle)

def _cloud_variant(cloud, seed):
    """Per wijn een unieke variant van dezelfde wolk: spiegeling, lichte schaal en rotatie."""
    rnd = random.Random(seed)
    if rnd.random() < 0.5:
        cloud = cloud.transpose(Image.FLIP_LEFT_RIGHT)
    scale = rnd.uniform(0.93, 1.08)
    w, h = cloud.size
    cloud = cloud.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    angle = rnd.uniform(-6, 6)
    if angle:
        cloud = cloud.rotate(angle, expand=True, resample=Image.BICUBIC)
    return cloud

def _alpha_at(img, x, y):
    if 0 <= x < img.width and 0 <= y < img.height:
        return img.getpixel((x, y))[3]
    return 0

def _gold_speckles(cv, cloud, cloud_pos, seed, kleur="rood", n=16):
    from PIL import ImageDraw
    rnd = random.Random(seed + 999)
    d = ImageDraw.Draw(cv)
    a = cloud.getchannel("A")
    ox, oy = cloud_pos
    # kleurmatig contrast: op een lichte wolk (wit/rose) valt warm goud vrijwel weg -> donkerder koper/brons
    color = {"wit": (150, 108, 48), "rose": (90, 45, 40)}.get(kleur, (248, 214, 120))
    tries = 0; placed = 0
    while placed < n and tries < n * 12:
        tries += 1
        lx = rnd.randint(0, cloud.width - 1); ly = rnd.randint(0, cloud.height - 1)
        if a.getpixel((lx, ly)) < 25:                # net buiten/aan de rand van de nevel
            continue
        x, y = ox + lx, oy + ly
        r = rnd.uniform(2.5, 6.5)
        op = rnd.randint(120, 220)
        d.ellipse([x - r, y - r, x + r, y + r], fill=color + (op,))
        placed += 1

def _nearest_paint(cloud, cloud_pos, cx_guess, cy_guess, threshold=90, step=10, max_r=None, excl_x=None):
    """Absolute allerlaatste redmiddel: dichtstbijzijnde punt met echte wolk-verf, rondom een gok.
    excl_x (lokale x-range) wordt overgeslagen, zodat dit ook de flesuitsluiting respecteert."""
    a = cloud.getchannel("A")
    lx0, ly0 = cx_guess - cloud_pos[0], cy_guess - cloud_pos[1]
    max_r = max_r or max(cloud.size)
    def ok(lx):
        return not (excl_x and excl_x[0] < lx < excl_x[1])
    for r in range(0, max_r, step):
        for dx in range(-r, r + 1, step):
            for dy in (-r, r) if r else (0,):
                lx, ly = lx0 + dx, ly0 + dy
                if 0 <= lx < cloud.width and 0 <= ly < cloud.height and ok(lx) and a.getpixel((lx, ly)) >= threshold:
                    return cloud_pos[0] + lx, cloud_pos[1] + ly
            for dy in range(-r, r + 1, step):
                for dx in (-r, r) if r else (0,):
                    lx, ly = lx0 + dx, ly0 + dy
                    if 0 <= lx < cloud.width and 0 <= ly < cloud.height and ok(lx) and a.getpixel((lx, ly)) >= threshold:
                        return cloud_pos[0] + lx, cloud_pos[1] + ly
    return None                                                # nergens (buiten de uitsluiting) wolk gevonden

def _find_cloud_point(cloud, rnd, ly_target, side, half, excl, threshold=90, x_step=4,
                      y_radii=(0, 8, 20, 40, 80, 150, 260), gap_fracs=(0.18, 0.10, 0.05, 0.0),
                      max_y_radius=None):
    """Zoekt een ECHT geverifieerd wolk-pixel (x,y samen, geen los venster) bij een gewenste hoogte.
    gap_fracs dwingt eerst een duidelijke afstand tot het midden af (zichtbaar links/rechts),
    en versoepelt pas als de wolk daar simpelweg geen verf heeft. max_y_radius begrenst hoe ver
    de zoektocht verticaal mag afdwalen (bijv. tot het eigen vak), zodat een item nooit het vak
    van zijn buurman binnenloopt."""
    a = cloud.getchannel("A")
    radii = [r for r in y_radii if max_y_radius is None or r <= max_y_radius]
    for gap_frac in gap_fracs:
        gap = int(cloud.width * gap_frac)
        for r in radii:
            for ly in ({ly_target} if r == 0 else {ly_target - r, ly_target + r}):
                if not (0 <= ly < cloud.height):
                    continue
                row = [lx for lx in range(0, cloud.width, x_step) if a.getpixel((lx, ly)) >= threshold]
                if not row:
                    continue
                def ok(c):
                    if excl and excl[0] < c < excl[1]:
                        return False
                    return (c < half - gap) if side < 0 else (c > half + gap)
                pref = [c for c in row if ok(c)]
                if pref:
                    return (min(pref) if side < 0 else max(pref)), ly
    return None, ly_target

def _smooth_line(cv, p1, p2, color, width=1.3, curve=0.12, supersample=4):
    """Dun, ANTI-GEALIASED lijntje met een zachte boog (PIL's eigen lijnen zijn niet
    anti-aliased en ogen daardoor gekarteld; hier supersamplen we en schalen terug)."""
    minx, maxx = min(p1[0], p2[0]) - 20, max(p1[0], p2[0]) + 20
    miny, maxy = min(p1[1], p2[1]) - 20, max(p1[1], p2[1]) + 20
    w, h = int(maxx - minx), int(maxy - miny)
    if w <= 0 or h <= 0:
        return
    ss = supersample
    layer = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = max((dx ** 2 + dy ** 2) ** 0.5, 1)
    nx, ny = -dy / length, dx / length            # loodrecht op de lijn, voor een zachte boog
    cxp, cyp = mx + nx * curve * length, my + ny * curve * length
    steps = 20
    pts = []
    for i in range(steps + 1):
        t = i / steps
        bx = (1 - t) ** 2 * p1[0] + 2 * (1 - t) * t * cxp + t ** 2 * p2[0]
        by = (1 - t) ** 2 * p1[1] + 2 * (1 - t) * t * cyp + t ** 2 * p2[1]
        pts.append(((bx - minx) * ss, (by - miny) * ss))
    d.line(pts, fill=color, width=max(1, int(round(width * ss))), joint="curve")
    layer = layer.resize((w, h), Image.LANCZOS)
    cv.alpha_composite(layer, (int(minx), int(miny)))

def _draw_labels(cv, placements, rnd):
    """Tekent alle labels in één pas, met botsingscontrole: op dezelfde zijde krijgt elk
    volgend label een minimale verticale afstand tot het vorige, zodat tekst bij wijnen
    met veel smaken (7+) nooit over elkaar heen valt."""
    d = ImageDraw.Draw(cv)
    f = _script_font(52)
    pad = 10
    N = cv.size[0]
    min_gap = 16                                             # minimale ruimte tussen twee labels op dezelfde zijde
    last_bottom = {-1: -1e9, 1: -1e9}
    for cx, cy, size, side, naam in sorted(placements, key=lambda p: (p[3], p[1])):  # per zijde, van boven naar beneden
        label = naam.capitalize()
        l, t, r, b = d.textbbox((0, 0), label, font=f)
        tw, th = r - l, b - t
        length = rnd.uniform(70, 110)
        sx, sy = cx + side * (size // 2 + pad), cy + rnd.uniform(-6, 6)
        ey = sy + rnd.uniform(-14, 14)
        needed = last_bottom[side] + min_gap + th / 2         # label-midden moet hier minstens op zitten
        if ey < needed:
            ey = needed
        ex = sx + side * length
        _smooth_line(cv, (sx, sy), (ex, ey), (95, 75, 60, 150), width=1.3, curve=rnd.uniform(-0.14, 0.14))
        tx = ex + pad if side > 0 else ex - pad - tw
        ty = ey - t - th / 2
        tx = max(14, min(N - 14 - tw, tx))
        d.text((tx, ty), label, font=f, fill=(95, 75, 60, 235))
        last_bottom[side] = ey + th / 2

def _usable_range(cloud, side, half, bottle_top_local, nw, N, cloud_pos, max_half_extent,
                  threshold=90, y_step=6, x_step=6, margin=24):
    """Scant de ECHTE wolk-pixels (geen gok) om te bepalen welk verticaal bereik op deze kant
    bruikbaar is: waar zit verf, mét de flesuitsluiting al verwerkt (conservatief -- geldig voor
    élk item, klein of groot, in deze run). Basis voor een eerlijke, volledige verdeling."""
    a = cloud.getchannel("A")
    xs = range(0, half, x_step) if side < 0 else range(half, cloud.width, x_step)
    lo = hi = None
    for ly in range(0, cloud.height, y_step):
        gy = cloud_pos[1] + ly
        excl = None
        if gy + max_half_extent > bottle_top_local:
            min_off = nw // 2 + max_half_extent + margin
            excl = (N // 2 - min_off - cloud_pos[0], N // 2 + min_off - cloud_pos[0])
        for lx in xs:
            if excl and excl[0] < lx < excl[1]:
                continue
            if a.getpixel((lx, ly)) >= threshold:
                if lo is None:
                    lo = ly
                hi = ly
                break
    return (lo, hi) if lo is not None else None

def _compose_aromawolk(cv, bottle, prim, sec, seed, kleur_override=None):
    N = cv.size[0]; rnd = random.Random(seed)
    kleur = kleur_override or _wine_color(bottle)
    base_cloud = _fit(_style_asset("aromawolk", kleur), int(N * 0.62))
    cloud = _cloud_variant(base_cloud, seed)               # per wijn unieke worp van dezelfde asset
    bp = _bottle_px()
    b = _trim(bottle); bw, bh = b.size
    nw = max(1, round(bw * bp / bh))
    bottle_top = N - 40 - bp
    cloud_cx = N // 2
    cloud_cy = max(cloud.height // 2 + 20, bottle_top - int(cloud.height * 0.44) + int(N * 0.06))
    cloud_pos = (cloud_cx - cloud.width // 2, cloud_cy - cloud.height // 2)
    cv.alpha_composite(cloud, cloud_pos)
    _gold_speckles(cv, cloud, cloud_pos, seed, kleur=kleur)

    items = _by_type(prim + sec)
    half = cloud.width // 2
    left_items  = [it for i, it in enumerate(items) if i % 2 == 0]
    right_items = [it for i, it in enumerate(items) if i % 2 == 1]
    range_half_extent = int(FLAVOR_PX * 0.50 * 0.66) + 16    # milde marge vóór het meten (echte veiligheid zit per item)

    # per kant het ECHTE bruikbare verticale bereik opmeten (i.p.v. een gegokt top/bottom)
    ranges = {}
    for side, side_items in ((-1, left_items), (1, right_items)):
        if not side_items:
            continue
        r = _usable_range(cloud, side, half, bottle_top, nw, N, cloud_pos, range_half_extent)
        ranges[side] = r or (int(cloud.height * 0.06), int(cloud.height * 0.94))  # nooddeksel, komt normaal niet voor

    bottle_top_r, bottle_bottom_r = _bottle_rect(N)
    placements = []                                          # (cx, cy, size, side, naam) -> labels na de fles tekenen
    for side, side_items in ((-1, left_items), (1, right_items)):
        n_side = len(side_items)
        if n_side == 0:
            continue
        lo, hi = ranges[side]
        span = max(hi - lo, 1)
        slot = span / n_side                                  # het ECHTE bruikbare bereik in gelijke delen
        for j, it in enumerate(side_items):
            size = int(FLAVOR_PX * rnd.uniform(0.45, 0.62))
            ly_target = int(lo + slot * (j + 0.5))
            half_extent = int(size * 0.66)
            excl = None
            gy_target = cloud_pos[1] + ly_target
            if gy_target + half_extent > bottle_top:
                min_off = nw // 2 + half_extent + 24
                excl = (cloud_cx - min_off - cloud_pos[0], cloud_cx + min_off - cloud_pos[0])
            lx, ly = _find_cloud_point(cloud, rnd, ly_target, side, half, excl,
                                       max_y_radius=max(slot * 0.60, 40))   # 1e voorkeur: blijf binnen het eigen vak
            if lx is None:
                lx, ly = _find_cloud_point(cloud, rnd, ly_target, side, half, excl)  # eigen vak leeg -> verder zoeken
            if lx is not None:
                cx, cy = cloud_pos[0] + lx, cloud_pos[1] + ly    # gegarandeerd op de wolk EN weg van de fles
            else:
                gok_cx = cloud_cx + side * int(0.20 * cloud.width)
                found = _nearest_paint(cloud, cloud_pos, cx_guess=gok_cx, cy_guess=cloud_pos[1] + ly_target,
                                       excl_x=(excl[0] + cloud_pos[0], excl[1] + cloud_pos[0]) if excl else None)
                if found is None:
                    found = _nearest_paint(cloud, cloud_pos, cx_guess=gok_cx, cy_guess=cloud_pos[1] + ly_target)
                cx, cy = found if found else (gok_cx, cloud_pos[1] + ly_target)
            # lichte correctie: raakt het toch net de fles (zeldzaam, diepste rijen), duw dan een klein
            # stukje weg -- maar alleen als die duw ook echt nog op de wolk landt, anders liever een
            # miniem randoverlapje dan alsnog los komen te zweven
            he = int(size * 0.66)
            if bottle_top_r - he < cy < bottle_bottom_r + he:
                min_off = nw // 2 + he + 24
                if abs(cx - N // 2) < min_off:
                    ncx = N // 2 + side * min_off
                    nlx, nly = ncx - cloud_pos[0], cy - cloud_pos[1]
                    if 0 <= nlx < cloud.width and 0 <= nly < cloud.height and \
                       cloud.getchannel("A").getpixel((nlx, nly)) >= 80:
                        cx = ncx
            cx = max(size // 2 + 10, min(N - size // 2 - 10, cx))
            _place_cutout(cv, it, cx, cy, size, rnd.uniform(-20, 20),
                          shadow=True, shadow_blur=10, shadow_opacity=45, shadow_offset=(4, 9))
            placements.append((cx, cy, size, side, it["naam"]))
    _paste_bottle(cv, bottle)                                   # fles eerst, labels daarna -> nooit afgesneden
    _draw_labels(cv, placements, rnd)

def _compose_kleurverloop(cv, bottle, prim, sec, seed, kleur_override=None):
    N = cv.size[0]
    kleur = kleur_override or _wine_color(bottle)
    ribbons = _fit(_style_asset("kleurverloop", kleur), int(N * 0.66))
    bp = _bottle_px()
    cx = int(N * 0.40)                                       # fles + linten iets naar links, legenda rechts
    cv.alpha_composite(ribbons, (cx - ribbons.width // 2, max(10, N - 40 - bp - ribbons.height + int(bp*0.12))))
    _legend(cv, _by_type(prim + sec), int(N * 0.74), int(N * 0.16), int(N * 0.62), dots=True)
    b = _trim(bottle); w, h = b.size
    nw = max(1, round(w * bp / h)); b = b.resize((nw, bp), Image.LANCZOS)
    cv.alpha_composite(b, (cx - nw // 2, N - bp - 40))

def _compose_rook(cv, bottle, prim, sec, seed, kleur_override=None):
    N = cv.size[0]
    kleur = kleur_override or _wine_color(bottle)
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
    "constellatie": None,   # hieronder gezet (functie volgt)
}

INK = (95, 75, 60)   # sepia-inkt

def _sketch(im, ink=INK, blur_frac=0.02, boost=2.6):
    """Foto-cutout -> lijntekening, puur Pillow (difference-of-gaussians). Gratis en deterministisch."""
    from PIL import ImageOps, ImageChops
    im = im.convert("RGBA")
    alpha = im.getchannel("A")
    gray = im.convert("L")
    r = max(2, int(min(im.size) * blur_frac))
    blur = gray.filter(ImageFilter.GaussianBlur(r))
    lines = ImageChops.add(ImageChops.subtract(blur, gray), ImageChops.subtract(gray, blur))
    lines = lines.point(lambda p: min(255, int(p * boost))).filter(ImageFilter.SMOOTH)
    line_alpha = ImageChops.multiply(lines, alpha)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out = Image.composite(Image.new("RGBA", im.size, ink + (255,)), out, line_alpha)
    out.putalpha(line_alpha)
    return out

def _dotted_line(d, p1, p2, color, dot=5, gap=26):
    import math
    dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    steps = max(int(dist / gap), 1)
    for s in range(steps + 1):
        t = s / steps
        x = p1[0] + (p2[0] - p1[0]) * t; y = p1[1] + (p2[1] - p1[1]) * t
        d.ellipse([x - dot/2, y - dot/2, x + dot/2, y + dot/2], fill=color)

def _compose_constellatie(cv, bottle, prim, sec, seed, kleur_override=None):
    from PIL import ImageDraw
    N = cv.size[0]; rnd = random.Random(seed)
    items = _by_type(prim + sec)
    bp = _bottle_px()
    b = _trim(bottle); nw = max(1, round(b.size[0] * bp / b.size[1]))
    col_l, col_r = N // 2 - nw // 2 - 40, N // 2 + nw // 2 + 40
    d = ImageDraw.Draw(cv)
    ink = INK + (230,); gold = (196, 160, 90, 220)
    f = _font(40, "-Bold")
    size = int(FLAVOR_PX * 0.95)
    # posities: links/rechts afwisselend, verticaal verdeeld, buiten de flescolom
    pts = []
    top, bottom = int(N * 0.10), int(N * 0.88)
    band = (bottom - top) / max(len(items), 1)
    for i, it in enumerate(items):
        side = -1 if i % 2 == 0 else 1
        cy = int(top + band * (i + 0.5) + rnd.uniform(-band * 0.12, band * 0.12))
        off = rnd.uniform(0.20, 0.36) * N
        cx = int(N / 2 + side * off)
        cx = min(col_l - size // 2, cx) if side < 0 else max(col_r + size // 2, cx)
        cx = max(size // 2 + 30, min(N - size // 2 - 30, cx))
        pts.append((it, cx, cy))
    # sterrenlijnen: per kant een ketting (kruist de fles niet)
    for side_pts in ([p for i, p in enumerate(pts) if i % 2 == 0],
                     [p for i, p in enumerate(pts) if i % 2 == 1]):
        for (_, x1, y1), (_, x2, y2) in zip(side_pts, side_pts[1:]):
            _dotted_line(d, (x1, y1), (x2, y2), gold)
    for _ in range(10):                                       # losse sterretjes
        sx = rnd.randint(int(N*0.06), int(N*0.94)); sy = rnd.randint(int(N*0.06), int(N*0.90))
        if col_l - 30 < sx < col_r + 30: continue
        r = rnd.randint(4, 9)
        d.ellipse([sx-r, sy-r, sx+r, sy+r], fill=gold)
    # iconen (geschetste cutouts) + label eronder
    for it, cx, cy in pts:
        icon = _fit(_sketch(get_flavor_cutout(it["naam"], it.get("type", ""))), size)
        cv.alpha_composite(icon, (cx - icon.width // 2, cy - icon.height // 2))
        label = it["naam"].upper()
        tw = d.textlength(label, font=f)
        d.text((cx - tw / 2, cy + icon.height // 2 + 14), label, font=f, fill=ink)
    _paste_bottle(cv, bottle)

_COMPOSERS["constellatie"] = _compose_constellatie

def compose(bottle_img, prim, sec, seed=0, kleur_override=None):
    cv = _new_canvas()
    _COMPOSERS.get(AROMA_STYLE, _compose_kolommen)(cv, bottle_img, prim, sec, seed, kleur_override)
    return cv


# ------------------------- Hoofdroutine -------------------------
def _estimate_image_cost(model, quality):
    """Vaste output-tokens per kwaliteitsniveau (OpenAI's beeld-API), dus vooraf te berekenen
    zonder ook maar 1 call te doen. Ruwe schatting; input-tokens (prompt) tellen licht mee."""
    OUT_TOKENS = {"low": 272, "medium": 1056, "high": 4160}
    rates = IMG_RATES.get(model, IMG_RATES["gpt-image-1.5"])
    out_t = OUT_TOKENS.get(quality, 1056)
    return out_t * rates["out"] + 120 * rates["in"]           # ~120 input-tokens voor een gemiddelde prompt

def preflight():
    """Bepaalt ZONDER ook maar 1 OpenAI-call wat een run zou kosten: welke smaak-extracties en
    welke smaak-uitsnedes ontbreken nog in de cache. Print dat duidelijk en stopt daarna."""
    global _access_token
    if not CLIENT_ID or not CLIENT_SECRET or SHOP.startswith("jouwwinkel"):
        sys.exit("Ontbrekende SHOP / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET.")
    _access_token = get_access_token()

    products = select_products()
    meta = _load_meta()
    nieuwe_extracties = [p["handle"] for p in products if p["handle"] not in meta]

    nodige_smaken = {}                                          # canonieke naam -> voorbeeld-item (voor het type)
    for p in products:
        if p["handle"] in meta:
            e = meta[p["handle"]]
            prim, sec = e.get("primair", []), e.get("secundair", [])
        else:
            prim, sec = [], []                                  # nog te extraheren; smaken pas na extractie bekend
        prim, sec = _prep_flavors(prim, sec)
        for it in prim + sec:
            nodige_smaken.setdefault(it["naam"], it)

    suf = f"-{CUTOUT_SOURCE}.png"
    ontbrekende_cutouts = [naam for naam in nodige_smaken if not (CACHE_DIR / f"{_slug(naam)}{suf}").exists()]

    per_extractie = 2000 * TXT_IN_RATE + 150 * TXT_OUT_RATE     # ruwe schatting per beschrijving
    per_beeld = _estimate_image_cost(OPENAI_IMAGE_MODEL, IMAGE_QUALITY)
    kosten_extractie = len(nieuwe_extracties) * per_extractie
    kosten_beelden = len(ontbrekende_cutouts) * per_beeld
    totaal = kosten_extractie + kosten_beelden

    print(f"== VOORCALCULATIE | {len(products)} fles(sen) geselecteerd | stijl {AROMA_STYLE} | "
          f"model {OPENAI_IMAGE_MODEL} ({IMAGE_QUALITY}) ==\n")
    if not nieuwe_extracties and not ontbrekende_cutouts:
        print("Geen enkele OpenAI-call nodig -- alles komt uit de cache. Veilig om direct te genereren.")
        return
    print("Er zijn OpenAI-calls nodig om deze run te voltooien:\n")
    if nieuwe_extracties:
        print(f"  - {len(nieuwe_extracties)}x smaak-extractie (tekstmodel {OPENAI_TEXT_MODEL})")
        print(f"    Waarom: voor deze wijn(en) staan de smaken nog niet in flavor_meta.json:")
        print(f"    {', '.join(nieuwe_extracties[:15])}" + (" ..." if len(nieuwe_extracties) > 15 else ""))
    if ontbrekende_cutouts:
        print(f"\n  - {len(ontbrekende_cutouts)}x nieuwe smaakafbeelding ({OPENAI_IMAGE_MODEL}, {IMAGE_QUALITY})")
        print(f"    Waarom: deze smaken staan nog niet als uitsnede in flavor_cache/:")
        print(f"    {', '.join(sorted(ontbrekende_cutouts)[:15])}" + (" ..." if len(ontbrekende_cutouts) > 15 else ""))
    print(f"\nGeschatte totale kosten: ~${totaal:.2f} "
          f"(extractie ~${kosten_extractie:.2f} + beelden ~${kosten_beelden:.2f})")
    print("\nGeef toestemming door de volgende job ('genereren') goed te keuren in de Actions-run "
          "(Review deployments -> Approve). Zonder goedkeuring wordt er niets aangeroepen of uitgegeven.")


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
          f"| fles {_bottle_px()}px op {FINAL_SIZE} | overwrite: {OVERWRITE} "
          f"| wijnhuis: {WIJNHUIS or '-'} | {len(products)} fles(sen) ==\n")
    if DRY_RUN:
        print("Let op: DRY-RUN genereert nieuwe smaken (OpenAI-kosten, maar gecacht), upload/tagt niet.\n")

    done = failed = 0
    start = time.time()
    n_total = len(products)
    for i, p in enumerate(products, 1):
        t0 = time.time()
        try:
            prim, sec = extract_flavors(p["handle"], p.get("description"))
            prim, sec = _prep_flavors(prim, sec)
            if not prim and not sec:
                print(f"[{i}/{n_total}] [skip] {p['handle']}: geen (bruikbare) smaken in beschrijving"); continue
            prim, sec = _balance(prim, sec)
            raw = requests.get(p["featuredImage"]["url"], timeout=30).content
            kleur_override = _product_wine_color(p)          # betrouwbaar Shopify-veld; None = val terug op pixels
            final = compose(Image.open(io.BytesIO(raw)), prim, sec,
                            seed=zlib.crc32(p["handle"].encode()), kleur_override=kleur_override)
            final.save(BACKUP_DIR / f"{p['handle']}-smaak.png", "PNG", optimize=True)
            names = lambda xs: ", ".join(x["naam"] for x in xs) or "-"
            dt = time.time() - t0
            print(f"[{i}/{n_total}] [{'dry' if DRY_RUN else 'ok'}] {p['handle']}  ({dt:.1f}s)  "
                  f"| links: {names(prim)}  | rechts: {names(sec)}")
            if not DRY_RUN:
                buf = io.BytesIO(); final.save(buf, "PNG", optimize=True)
                upload_and_attach(p["id"], buf.getvalue(), p["handle"], old_ids=old_smaak_media_ids(p))
            done += 1
        except Exception as e:
            print(f"[{i}/{n_total}] [ERR] {p.get('handle')}: {e}"); failed += 1

        if i % 10 == 0 or i == n_total:                       # tussentijdse voortgang bij grote batches
            elapsed = time.time() - start
            rates = IMG_RATES.get(OPENAI_IMAGE_MODEL, IMG_RATES["gpt-image-1.5"])
            lopend = (_cost["img_in"] * rates["in"] + _cost["img_out"] * rates["out"]
                     + _cost["txt_in"] * TXT_IN_RATE + _cost["txt_out"] * TXT_OUT_RATE)
            print(f"   -- voortgang: {i}/{n_total} | {done} ok, {failed} fout | "
                  f"{elapsed:.0f}s verstreken | kosten tot nu toe: ~${lopend:.2f} --")

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
    if env_bool("PREFLIGHT_ONLY", False):
        preflight()
    else:
        main()
