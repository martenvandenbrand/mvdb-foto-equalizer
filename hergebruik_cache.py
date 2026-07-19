#!/usr/bin/env python3
"""
Koper & Karaf - Cutouts hergebruiken
====================================
Vult ontbrekende CANONIEKE cutouts in flavor_cache/ door een bestaande,
gelijkwaardige cutout te kopieren naar de canonieke bestandsnaam. Zo hoeft
de smaakfoto-run ze niet opnieuw (betaald) te genereren.

Bijv. kersen-gpt.png -> kers-gpt.png, mokka-gpt.png -> koffie-gpt.png.
Bestaande bestanden worden NOOIT overschreven. Gebruikt jouw synonyms.json.
"""

import json, pathlib, re, shutil, os

CACHE = pathlib.Path("flavor_cache")
SYN   = json.loads(pathlib.Path("synonyms.json").read_text(encoding="utf-8"))
META  = json.loads(pathlib.Path("flavor_meta.json").read_text(encoding="utf-8"))
SOURCE_TAG = os.environ.get("CUTOUT_SOURCE", "gpt")   # welke bron-cutouts (gpt/stock)

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

def main():
    suf = f"-{SOURCE_TAG}.png"
    cut = {f.name[:-len(suf)]: f for f in CACHE.glob(f"*{suf}")}   # basename -> pad

    needed = set()
    for e in META.values():
        for side in ("primair", "secundair"):
            for it in e.get(side, []):
                for color in (True, False):
                    c = canon(it.get("naam", ""), color)
                    if c:
                        needed.add(c)

    plain = lambda b: (len(b.split("-")), len(b))
    copied = skipped = 0
    for c in sorted(needed):
        tgt = slug(c)
        if tgt in cut:
            continue                                   # canonieke cutout bestaat al
        cands = [b for b in cut
                 if canon(b.replace("-", " "), True) == c or canon(b.replace("-", " "), False) == c]
        if not cands:
            continue                                   # geen bestaande gelijke -> later genereren
        best = sorted(cands, key=plain)[0]
        dst = CACHE / f"{tgt}{suf}"
        if dst.exists():
            skipped += 1; continue
        shutil.copyfile(cut[best], dst)
        print(f"[kopie] {best}{suf}  ->  {tgt}{suf}")
        copied += 1

    print(f"\nKlaar. Hergebruikt (gekopieerd): {copied} | al aanwezig overgeslagen: {skipped}")
    print("De smaakfoto-run genereert nu alleen nog wat geen bestaande gelijke had.")

if __name__ == "__main__":
    main()
