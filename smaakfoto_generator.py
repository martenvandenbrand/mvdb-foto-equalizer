#!/usr/bin/env python3
"""
Koper & Karaf - Smaakfoto-generator
===================================
Neemt per wijn de hoofdfoto (transparante fles) en genereert een nieuwe
afbeelding met de smaken/aroma's uit de productbeschrijving rondom de fles.

Werkwijze:
  1. Smaken uit de beschrijving halen met een goedkoop OpenAI-tekstmodel.
  2. Fles centraal op een canvas + masker (midden beschermd).
  3. GPT Image tekent de smaken rondom de fles (transparante achtergrond).
  4. De ECHTE fles wordt er weer overheen gecomposit -> etiket blijft perfect.
  5. Resultaat als extra productfoto terug in Shopify, product krijgt een tag.

Kostenbeheersing:
  - BATCH_SIZE (1 of 10) begrenst het aantal flessen per run.
  - HANDLE draait exact 1 gekozen fles (voor een testje).
  - Verwerkte producten krijgen DONE_TAG; volgende runs slaan die over,
    zodat je nooit dubbel betaalt.
"""

import os, io, sys, json, time, base64, html, re, pathlib, requests
from PIL import Image, ImageFilter

def env(k, d=""):       return os.environ.get(k, d)
def env_bool(k, d):     return os.environ.get(k, str(d)).strip().lower() in ("1", "true", "yes", "ja")
def env_int(k, d):      return int(os.environ.get(k, d))

# ======================= CONFIGURATIE (via env / Action-inputs) =======================
SHOP           = env("SHOP", "jouwwinkel.myshopify.com")
CLIENT_ID      = env("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET  = env("SHOPIFY_CLIENT_SECRET", "")
API_VERSION    = env("API_VERSION", "2026-01")

OPENAI_API_KEY   = env("OPENAI_API_KEY", "")
OPENAI_IMAGE_MODEL = env("OPENAI_IMAGE_MODEL", "gpt-image-1.5")   # of "gpt-image-1-mini" (goedkoper)
OPENAI_TEXT_MODEL  = env("OPENAI_TEXT_MODEL", "gpt-4o-mini")
IMAGE_QUALITY    = env("IMAGE_QUALITY", "medium")                 # low | medium | high
IMAGE_SIZE       = env("IMAGE_SIZE", "1024x1024")                 # generatieformaat
FINAL_SIZE       = env_int("FINAL_SIZE", 2048)                    # eindformaat voor de webshop
BOTTLE_FRACTION  = float(env("BOTTLE_FRACTION", "0.72"))          # fleshoogte t.o.v. canvas
SIDE_WIDTH       = float(env("SIDE_WIDTH", "0.32"))               # breedte van elke zijkolom (smaken links/rechts)

BATCH_SIZE     = env_int("BATCH_SIZE", 1)      # aantal flessen per run (1 of 10)
HANDLE         = env("HANDLE", "")             # 1 specifieke fles (URL-slug); leeg = automatische selectie
DONE_TAG       = env("DONE_TAG", "smaakfoto")  # tag die verwerkte producten krijgen
USE_MASK       = env_bool("USE_MASK", True)
DRY_RUN        = env_bool("DRY_RUN", True)     # let op: genereert wel (kost OpenAI-credits), maar upload/tagt niet
BACKUP_DIR     = pathlib.Path("backup_smaak")
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
        q = f"handle:{HANDLE}"; n = 1
    else:
        q = f"-tag:{DONE_TAG}"; n = BATCH_SIZE
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
    gql(REORDER_M, {"id": product_id, "moves": [{"id": new_id, "newPosition": "1"}]})  # als 2e foto
    gql(TAG_M, {"id": product_id, "tags": [DONE_TAG]})


# ------------------------- OpenAI -------------------------
FLAVOR_SYS = (
    "Je bent sommelier. Analyseer de wijnbeschrijving en geef UITSLUITEND geldige JSON, geen uitleg, "
    "geen code-fences, in exact deze vorm:\n"
    '{"primair":[{"naam":"citroen","type":"fruit"}],"secundair":[{"naam":"vanille","type":"bloem"}]}\n'
    "Regels: 'primair' = aroma's uit de druif zelf (fruit, bloemen, citrus). 'secundair' = aroma's uit "
    "vinificatie/rijping (hout, vanille, brioche, boter, room, noten, toast). 'type' is een korte "
    "categorie zoals fruit, citrus, bloem, noot, hout, zuivel, kruid. Alleen concrete, fotografeerbare "
    "elementen. Maximaal 7 items totaal, evenwichtig verdeeld waar mogelijk."
)

def _openai_post(url, tries=5, **kwargs):
    """POST met backoff op tijdelijke 429/5xx en heldere foutmeldingen."""
    r = None
    for attempt in range(tries):
        r = requests.post(url, **kwargs)
        if r.status_code < 400:
            return r
        body = r.text
        if r.status_code == 429 and "insufficient_quota" in body:
            raise RuntimeError("OpenAI weigert: geen tegoed/quota op deze API-sleutel. Stel billing in "
                               "en zet credits klaar in de OpenAI-console (en verifieer je organisatie "
                               "voor GPT Image).")
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("retry-after", 2 * (attempt + 1)))); continue
        raise RuntimeError(f"OpenAI {r.status_code}: {body[:400]}")
    raise RuntimeError(f"OpenAI bleef {r.status_code} geven na {tries} pogingen: {r.text[:300]}")

def extract_flavors(description):
    """Geeft (primair, secundair): elk een lijst van {'naam','type'}."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", description or "")).strip()[:2000]
    if not text:
        return [], []
    r = _openai_post("https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        data=json.dumps({"model": OPENAI_TEXT_MODEL, "temperature": 0.3,
                         "response_format": {"type": "json_object"},
                         "messages": [{"role": "system", "content": FLAVOR_SYS},
                                      {"role": "user", "content": text}]}), timeout=60)
    content = r.json()["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?|```$", "", content.strip()).strip()
    try:
        data = json.loads(content)
        prim = [x for x in data.get("primair", []) if x.get("naam")]
        sec  = [x for x in data.get("secundair", []) if x.get("naam")]
    except Exception:
        # terugval: platte woordenlijst, in tweeën gesplitst
        words = [{"naam": w.strip(" ."), "type": "overig"}
                 for w in re.split(r"[,\n]", content) if w.strip(" .")][:7]
        prim, sec = words[: (len(words) + 1) // 2], words[(len(words) + 1) // 2:]
    return prim[:5], sec[:5]

def _cluster_text(items):
    """Groepeer items per type: 'citroen en sinaasappel (citrus); walnoot (noot)'."""
    groups = {}
    for it in items:
        groups.setdefault(it.get("type", "overig"), []).append(it.get("naam", ""))
    return "; ".join(f"{' en '.join(v)} ({t})" for t, v in groups.items())

def generate_flavor_image(canvas_png, mask_png, primair, secundair):
    links = _cluster_text(primair) or "geen"
    rechts = _cluster_text(secundair) or "geen"
    prompt = (
        "Een set losse, FOTOREALISTISCHE productfoto's van voedingsmiddelen op een volledig transparante "
        "achtergrond, in de stijl van professionele studio-packshots: echte fotografie met natuurlijke "
        "texturen, scherpe details, realistische kleuren en een zachte natuurlijke slagschaduw. "
        "ABSOLUUT GEEN illustraties, tekeningen, 3D-renders, klei of cartoons.\n"
        f"Plaats aan de LINKERKANT, als één nette verticale cluster, de primaire aroma's: {links}. "
        f"Plaats aan de RECHTERKANT, als één nette verticale cluster, de secundaire aroma's: {rechts}. "
        "Groepeer gelijke types bij elkaar (fruit bij fruit, bloemen bij bloemen, noten bij noten). "
        "Houd het midden volledig leeg voor de fles; teken zelf geen fles, glas of wijn. "
        "Elk element even groot, mooi uitgelijnd, met ruimte ertussen."
    )
    files = {"image": ("bottle.png", canvas_png, "image/png")}
    if USE_MASK:
        files["mask"] = ("mask.png", mask_png, "image/png")
    data = {"model": OPENAI_IMAGE_MODEL, "prompt": prompt, "size": IMAGE_SIZE,
            "background": "transparent", "output_format": "png", "quality": IMAGE_QUALITY, "n": "1"}
    if "mini" not in OPENAI_IMAGE_MODEL:
        data["input_fidelity"] = "high"
    r = _openai_post("https://api.openai.com/v1/images/edits",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, data=data, files=files, timeout=300)
    return base64.b64decode(r.json()["data"][0]["b64_json"])


# ------------------------- Beeld: canvas, masker, terugcompositen -------------------------
def _canvas_dim():
    return int(IMAGE_SIZE.split("x")[0])

def fit_bottle(bottle):
    n = _canvas_dim()
    b = bottle.convert("RGBA")
    bbox = b.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if bbox: b = b.crop(bbox)
    th = int(n * BOTTLE_FRACTION); w, h = b.size; nw = max(1, round(w * th / h))
    b = b.resize((nw, th), Image.LANCZOS)
    cv = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    cv.paste(b, ((n - nw) // 2, (n - th) // 2), b)
    return cv

def build_mask(cv):
    """Alleen de linker- en rechterkolom bewerkbaar (transparant); midden beschermd (opaak)."""
    n = cv.size[0]
    side = int(SIDE_WIDTH * n)
    mask = Image.new("RGBA", (n, n), (255, 255, 255, 255))   # alles beschermd
    col = Image.new("RGBA", (side, n), (0, 0, 0, 0))         # bewerkbaar
    mask.paste(col, (0, 0))                                  # linkerkolom
    mask.paste(col, (n - side, 0))                           # rechterkolom
    return mask

def composite_back(generated, cv):
    out = Image.open(io.BytesIO(generated)).convert("RGBA").resize(cv.size)
    out.alpha_composite(cv)
    if FINAL_SIZE != out.size[0]:
        out = out.resize((FINAL_SIZE, FINAL_SIZE), Image.LANCZOS)
    return out


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
          f"| {len(products)} fles(sen) deze run ==\n")
    if DRY_RUN:
        print("Let op: DRY-RUN genereert wel beelden (OpenAI-kosten), maar upload/tagt niet.\n")

    done = failed = 0
    for p in products:
        try:
            prim, sec = extract_flavors(p.get("description"))
            if not prim and not sec:
                print(f"[skip] {p['handle']}: geen smaken in beschrijving"); continue
            raw = requests.get(p["featuredImage"]["url"], timeout=30).content
            cv = fit_bottle(Image.open(io.BytesIO(raw)))
            mask = build_mask(cv)
            mbuf = io.BytesIO(); mask.save(mbuf, "PNG"); mbuf.seek(0)
            cbuf = io.BytesIO(); cv.save(cbuf, "PNG"); cbuf.seek(0)

            gen = generate_flavor_image(cbuf.getvalue(), mbuf.getvalue(), prim, sec)
            final = composite_back(gen, cv)
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
    print(f"Beelden staan lokaal in: {BACKUP_DIR.resolve()}")

if __name__ == "__main__":
    main()
