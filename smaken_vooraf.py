#!/usr/bin/env python3
"""
Koper & Karaf - Smaken vooraf genereren
=======================================
Loopt door ALLE producten en maakt per product het smaak-bestand
flavor_meta/<handle>.json aan (alleen de tekst-extractie, geen beeldgeneratie,
geen Shopify-uploads). Zo kun je de smaken vooraf nalopen en aanpassen voordat
je de smaakfoto's genereert.

Hergebruikt de Shopify-auth en extractie uit smaakfoto_generator.py.
Reeds bestaande JSON's worden overgeslagen, tenzij BYPASS_CACHE=true.
"""

import sys
import smaakfoto_generator as sg

ALL_Q = """
query($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { handle description }
  }
}"""

def iter_all_products():
    cursor = None
    while True:
        d = sg.gql(ALL_Q, {"cursor": cursor})["products"]
        for n in d["nodes"]:
            yield n["handle"], n.get("description")
        if not d["pageInfo"]["hasNextPage"]:
            break
        cursor = d["pageInfo"]["endCursor"]

def main():
    if not sg.CLIENT_ID or not sg.CLIENT_SECRET or sg.SHOP.startswith("jouwwinkel"):
        sys.exit("Ontbrekende SHOP / SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET.")
    if not sg.OPENAI_API_KEY:
        sys.exit("Ontbrekende OPENAI_API_KEY.")
    sg._access_token = sg.get_access_token()

    print(f"== Smaken vooraf | {sg.SHOP} | opnieuw genereren: {sg.BYPASS_CACHE} ==\n")
    done = empty = failed = 0
    for handle, desc in iter_all_products():
        try:
            prim, sec = sg.extract_flavors(handle, desc)
            if not prim and not sec:
                print(f"[leeg] {handle}: geen smaken in beschrijving"); empty += 1; continue
            names = lambda xs: ", ".join(x["naam"] for x in xs) or "-"
            print(f"[ok]  {handle}  | links: {names(prim)}  | rechts: {names(sec)}")
            done += 1
        except Exception as e:
            print(f"[ERR] {handle}: {e}"); failed += 1

    print(f"\nKlaar. Bestanden: {done} | zonder smaken: {empty} | fouten: {failed}")
    print(f"Smaken staan in: {sg.META_FILE.resolve()}")

if __name__ == "__main__":
    main()
