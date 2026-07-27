#!/usr/bin/env python3
"""
Koper & Karaf - Shopify Aroma Metaobjects Sync
JSON array format for list.metaobject_reference
"""

import os
import json
import requests
import sys
import time
from pathlib import Path

def env(k, d=""):
    return os.environ.get(k, d)

def env_bool(k, d):
    return os.environ.get(k, str(d)).strip().lower() in ("1", "true", "yes", "ja")

SHOP = env("SHOP", "koperenkaraf.myshopify.com")
CLIENT_ID = env("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET = env("SHOPIFY_CLIENT_SECRET", "")
API_VERSION = env("API_VERSION", "2026-01")
DRY_RUN = env_bool("DRY_RUN", True)

API_URL = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
TOKEN_URL = f"https://{SHOP}/admin/oauth/access_token"
FLAVOR_FILE = Path("flavor_meta.json")

_access_token = None
_aroma_cache = {}

def get_access_token():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("❌ FOUT: Secrets niet ingesteld")
        sys.exit(1)
    
    try:
        r = requests.post(TOKEN_URL, timeout=30, data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        })
        r.raise_for_status()
        token = r.json()["access_token"]
        print(f"✅ OAuth token verkregen")
        return token
    except Exception as e:
        print(f"❌ Fout bij OAuth: {e}")
        sys.exit(1)

def gql(query, variables=None):
    headers = {
        "X-Shopify-Access-Token": _access_token,
        "Content-Type": "application/json"
    }
    
    for attempt in range(6):
        try:
            r = requests.post(
                API_URL,
                headers=headers,
                data=json.dumps({"query": query, "variables": variables or {}}),
                timeout=30
            )
            
            if r.status_code == 429:
                wait_time = int(r.headers.get("Retry-After", 2))
                time.sleep(wait_time)
                continue
            
            data = r.json()
            
            if "errors" in data and any("THROTTLED" in str(e) for e in data["errors"]):
                wait_time = 2 * (attempt + 1)
                time.sleep(wait_time)
                continue
            
            if "errors" in data:
                return None, data['errors']
            
            return data.get("data"), None
        
        except requests.exceptions.Timeout:
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            return None, str(e)
    
    return None, "Te vaak gethrottled/timeout"

def find_or_create_aroma_metaobject(aroma_naam):
    """Find or create aroma metaobject with inline input"""
    if aroma_naam in _aroma_cache:
        return _aroma_cache[aroma_naam]
    
    query = """
    {
      metaobjects(type: "aroma", first: 250) {
        edges {
          node {
            id
            fields {
              key
              value
            }
          }
        }
      }
    }
    """
    
    data, _ = gql(query)
    
    if data and data.get("metaobjects", {}).get("edges"):
        for edge in data["metaobjects"]["edges"]:
            node = edge["node"]
            for field in node.get("fields", []):
                if field.get("key") == "aroma" and field.get("value") == aroma_naam:
                    _aroma_cache[aroma_naam] = node["id"]
                    return node["id"]
    
    if DRY_RUN:
        fake_id = f"gid://shopify/Metaobject/aroma-{aroma_naam.lower().replace(' ', '-')}"
        _aroma_cache[aroma_naam] = fake_id
        return fake_id
    
    aroma_escaped = aroma_naam.replace('"', '\\"')
    
    mutation = f"""
    mutation {{
      metaobjectCreate(metaobject: {{
        type: "aroma"
        fields: [
          {{
            key: "aroma"
            value: "{aroma_escaped}"
          }}
        ]
      }}) {{
        metaobject {{
          id
        }}
        userErrors {{
          message
        }}
      }}
    }}
    """
    
    data, errors = gql(mutation)
    
    if errors:
        return None
    
    if data and data.get("metaobjectCreate", {}).get("metaobject"):
        aroma_id = data["metaobjectCreate"]["metaobject"]["id"]
        _aroma_cache[aroma_naam] = aroma_id
        return aroma_id
    
    user_errors = data.get("metaobjectCreate", {}).get("userErrors", []) if data else []
    if user_errors:
        return None
    
    return None

def find_product_by_handle(handle):
    """Find product ID by handle"""
    query = """
    query($handle: String!) {
      productByHandle(handle: $handle) {
        id
      }
    }
    """
    
    result, _ = gql(query, {"handle": handle})
    if result and "productByHandle" in result and result["productByHandle"]:
        return result["productByHandle"]["id"]
    
    return None

def escape_for_graphql(s):
    """Escape string for GraphQL - backslashes first, then quotes"""
    return s.replace('\\', '\\\\').replace('"', '\\"')

def set_aroma_references(product_id, aroma_ids):
    """Set aroma metaobject references on product as JSON array"""
    if not aroma_ids:
        return True, None
    
    # Create JSON array and escape for GraphQL
    references_json = json.dumps(aroma_ids)
    references_escaped = escape_for_graphql(references_json)
    
    mutation = f"""
    mutation {{
      metafieldsSet(
        metafields: [
          {{
            ownerId: "{product_id}"
            namespace: "custom"
            key: "aromas"
            type: "list.metaobject_reference"
            value: "{references_escaped}"
          }}
        ]
      ) {{
        metafields {{
          id
          key
        }}
        userErrors {{
          field
          message
        }}
      }}
    }}
    """
    
    if DRY_RUN:
        return True, None
    
    data, errors = gql(mutation)
    
    if errors:
        return False, str(errors)
    
    if not data:
        return False, "API error"
    
    metafield_result = data.get("metafieldsSet", {})
    user_errors = metafield_result.get("userErrors", [])
    
    if user_errors:
        return False, user_errors[0]["message"]
    
    if metafield_result.get("metafields"):
        return True, None
    
    return False, "Onbekende fout"

def main():
    global _access_token
    
    print("\n🍷 Shopify Aroma Metaobjects Sync")
    print("=" * 70)
    
    if DRY_RUN:
        print("⚠️  DRY RUN MODE\n")
    else:
        print("🔴 LIVE MODE\n")
    
    if not FLAVOR_FILE.exists():
        print(f"❌ FOUT: {FLAVOR_FILE} niet gevonden")
        sys.exit(1)
    
    print(f"📋 Laad {FLAVOR_FILE}...")
    with open(FLAVOR_FILE, 'r', encoding='utf-8') as f:
        flavor_data = json.load(f)
    
    print(f"✅ Geladen: {len(flavor_data)} producten\n")
    
    print("🔐 Verbind met Shopify...")
    _access_token = get_access_token()
    
    print("📝 Verzamel unieke aromas...")
    
    all_aroma_names = set()
    aroma_map = {}
    
    for handle, aromas in flavor_data.items():
        primary = aromas.get('primair', [])
        secondary = aromas.get('secundair', [])
        all_aromas = primary + secondary
        
        unique_aroma_names = []
        for aroma in all_aromas:
            naam = aroma.get('naam', '').strip()
            if naam:
                unique_aroma_names.append(naam)
                all_aroma_names.add(naam)
        
        aroma_map[handle] = unique_aroma_names
    
    print(f"✅ {len(all_aroma_names)} unieke aromas gevonden\n")
    
    print("🔨 Create/Find aroma metaobjects...")
    aroma_id_map = {}
    created = 0
    failed = 0
    
    for i, aroma_naam in enumerate(sorted(all_aroma_names), 1):
        print(f"   [{i:3d}/{len(all_aroma_names)}] {aroma_naam:30} ... ", end='', flush=True)
        
        aroma_id = find_or_create_aroma_metaobject(aroma_naam)
        if aroma_id:
            aroma_id_map[aroma_naam] = aroma_id
            created += 1
            print("✅")
        else:
            failed += 1
            print("❌")
    
    print(f"\n✅ {created}/{len(all_aroma_names)} aroma metaobjecten klaar")
    if failed > 0:
        print(f"⚠️  {failed} mislukt\n")
    else:
        print()
    
    print(f"🚀 Sync {len(aroma_map)} producten")
    if DRY_RUN:
        print("   (DRY RUN - geen echte updates)\n")
    else:
        print()
    
    successful = 0
    failed_sync = 0
    not_found = 0
    errors = []
    
    for i, (handle, aroma_names) in enumerate(aroma_map.items(), 1):
        print(f"[{i:3d}/{len(aroma_map)}] {handle[:50]:50} ... ", end='', flush=True)
        
        product_id = find_product_by_handle(handle)
        
        if not product_id:
            print("❌ Niet gevonden")
            not_found += 1
            continue
        
        aroma_ids = [aroma_id_map[naam] for naam in aroma_names if naam in aroma_id_map]
        
        if not aroma_ids:
            print("❌ Geen aromas")
            failed_sync += 1
            continue
        
        success, error = set_aroma_references(product_id, aroma_ids)
        
        if success:
            print(f"✅ ({len(aroma_ids)} aromas)")
            successful += 1
        else:
            print(f"❌ {error}")
            failed_sync += 1
            errors.append({"handle": handle, "error": error})
    
    print(f"\n{'=' * 70}")
    print(f"SYNC VOLTOOID")
    print(f"{'=' * 70}\n")
    
    print(f"📊 Resultaten:")
    print(f"  ✅ Producten gesyncet: {successful}/{len(aroma_map)}")
    print(f"  ❌ Fouten: {failed_sync}/{len(aroma_map)}")
    print(f"  ⏭️  Niet gevonden: {not_found}/{len(aroma_map)}")
    print(f"  🔗 Aroma metaobjecten: {len(aroma_id_map)}")
    
    if DRY_RUN:
        print(f"\n⚠️  DRY RUN - Geen echte updates")
    
    if errors:
        print(f"\n⚠️  Sync fouten:")
        for err in errors[:3]:
            print(f"  - {err['handle']}: {err['error']}")
        if len(errors) > 3:
            print(f"  ... en {len(errors) - 3} meer")
    
    results = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": DRY_RUN,
        "products_total": len(aroma_map),
        "products_synced": successful,
        "products_failed": failed_sync,
        "products_not_found": not_found,
        "aroma_metaobjects_total": len(aroma_id_map),
        "aroma_metaobjects_created": created,
        "aroma_metaobjects_failed": failed,
        "unique_aromas": len(all_aroma_names),
        "metaobject_type": "aroma",
        "metafield": {
            "namespace": "custom",
            "key": "aromas",
            "type": "list.metaobject_reference"
        },
        "errors": errors[:10]
    }
    
    results_file = Path(f"aroma_sync_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(results_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 Resultaten: {results_file}")
    print(f"\n✨ Setup:")
    print(f"   Metaobject type: aroma")
    print(f"   Metaobject field: aroma")
    print(f"   Metaveld: custom.aromas (list.metaobject_reference)")
    
    sys.exit(0 if (failed_sync == 0 and failed == 0) else 1)

if __name__ == "__main__":
    main()
