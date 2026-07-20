#!/usr/bin/env python3
"""
Koper & Karaf - Opschonen
=========================
Ruimt de repo veilig op:
  1. Seeden: ontbrekende CANONIEKE cutouts vullen vanuit een bestaande
     gelijkwaardige (zodat er geen beeld verloren gaat).
  2. Cache snoeien: alles in flavor_cache/ weg dat geen canonieke -gpt.png is
     (stockfoto's, losse bestanden zonder suffix, verweesde pre-synonym namen).
  3. Stale flavor_meta/ map verwijderen (vervangen door flavor_meta.json).
  4. Afgeronde eenmalige acties verwijderen (migreer- + hergebruik-workflow/script).

Gebruikt jouw synonyms.json + flavor_meta.json. Draait veilig meerdere keren.
"""

import json, pathlib, re, shutil, os

CACHE = pathlib.Path("flavor_cache")
SYN   = json.loads(pathlib.Path("synonyms.json").read_text(encoding="utf-8"))
META  = json.loads(pathlib.Path("flavor_meta.json").read_text(encoding="utf-8"))
TAG   = os.environ.get("CUTOUT_SOURCE", "gpt")

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "smaak"

def canon(name, color):
    m = dict(SYN.get("merge", {}))
    if color:
        m.update(SYN.get("merge_colors", {}))
    k = (name or "").strip().lower()
    for _ in range(6):
        if k in m:
            k = m[k]
        else:
            break
    return k

def needed_canon():
    out = set()
    for e in META.values():
        for side in ("primair", "secundair"):
            for it in e.get(side, []):
                for color in (True, False):
                    c = canon(it.get("naam", ""), color)
                    if c:
                        out.add(c)
    return out

def main():
    suf = f"-{TAG}.png"
    needed = needed_canon()

    # 1. seeden (hergebruik) — nooit overschrijven
    cut = {f.name[:-len(suf)]: f for f in CACHE.glob(f"*{suf}")}
    plain = lambda b: (len(b.split("-")), len(b))
    seeded = 0
    for c in sorted(needed):
        tgt = slug(c)
        if tgt in cut:
            continue
        cands = [b for b in cut
                 if canon(b.replace("-", " "), True) == c or canon(b.replace("-", " "), False) == c]
        if not cands:
            continue
        shutil.copyfile(cut[sorted(cands, key=plain)[0]], CACHE / f"{tgt}{suf}")
        seeded += 1

    # 2. cache snoeien: behoud alleen canonieke -gpt/-<tag>.png
    keep = {f"{slug(c)}{suf}" for c in needed}
    keep |= {f.name for f in CACHE.glob("asset-*.png")}   # sfeer-assets (wolk/linten/rook) behouden
    removed = 0
    for f in sorted(CACHE.glob("*.png")):
        if f.name not in keep:
            f.unlink(); removed += 1
    print(f"cache: geseed {seeded} | verwijderd {removed} | behouden {len(list(CACHE.glob('*.png')))}")

    # 3. stale flavor_meta/ map
    legacy = pathlib.Path("flavor_meta")
    if legacy.is_dir():
        n = len(list(legacy.glob("*.json")))
        shutil.rmtree(legacy)
        print(f"stale flavor_meta/ map verwijderd ({n} bestanden)")

    # 4. afgeronde eenmalige acties
    for p in ["hergebruik_cache.py",
              ".github/workflows/migreer-cache.yml",
              ".github/workflows/hergebruik-cache.yml"]:
        fp = pathlib.Path(p)
        if fp.exists():
            fp.unlink(); print(f"verwijderd: {p}")

    print("\nKlaar. Verwijder na deze run desgewenst nog opschonen.py + opschonen.yml handmatig.")

if __name__ == "__main__":
    main()
