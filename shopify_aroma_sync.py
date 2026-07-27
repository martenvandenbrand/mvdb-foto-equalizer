#!/usr/bin/env python3
"""
Koper & Karaf - Shopify Aroma Metaobjects Sync
1. Creëert metaobject type "aroma" (naam field)
2. Creëert instances per unieke aroma
3. Zet references op products (list)
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

# ======================= CONFIGURATIE =======================
SHOP = env("SHOP", "koperenkaraf.myshopify.com")
CLIENT_ID = env("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET = env("SHOPIFY_CLIENT_SECRET", "")
API_VERSION = env("API_VERSION", "2026-01")
DRY_RUN = env_bool("DRY_RUN", True)

API_URL = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
TOKEN_URL = f"https://{SHOP}/admin/oauth/access_token"
FLAVOR_FILE = Path("flavor_meta.json")

_access_token = None

# ======================= SHOPIFY API =======================

def get_access_token():
    """Get OAuth access token"""
    if not CLIENT_ID or not CLIENT_SECRET:
        print("❌ FOUT: SHOPIFY_CLIENT_ID en SHOPIFY_CLIENT_SECRET niet ingesteld")
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
    """Execute GraphQL query"""
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
                print(f"  ⏱️  Rate limited, wacht {wait_time}s...")
                time.sleep(wait_time)
                continue
            
            data = r.json()
            
            if "errors" in data and any("THROTTLED" in str(e) for e in data["errors"]):
                wait_time = 2 * (attempt + 1)
                print(f"  ⏱️  Throttled, wacht {wait_time}s...")
                time.sleep(wait_time)
                continue
            
            if "errors" in data:
                return None, data['errors']
            
            return data.get("data"), None
        
        except requests.exceptions.Timeout:
            print(f"  ⏱️  Timeout, poging {attempt + 1}/6")
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            return None, str(e)
    
    return None, "Te vaak gethrottled/timeout"

def create_metaobject_type():
    """Create metaobject type 'aroma' if it doesn't exist"""
    print("📋 Check/Create metaobject type 'aroma'...")
    
    if DRY_RUN:
        print("   (DRY RUN - skip)")
        return True
    
    mutation = """
    mutation {
      metaobjectTypeDefinitionCreate(
        definition: {
          name: "Aroma"
          type: "aroma"
          fieldDefinitions: [
            {
              key: "naam"
              description: "Aroma naam"
              type: "single_line_text_field"
            }
          ]
        }
      ) {
        metaobjectTypeDefinition {
          type
          fieldDefinitions {
            key
            type
          }
        }
        userErrors {
          message
        }
      }
    }
    """
    
    data, errors = gql(mutation)
    
    if errors:
        if "already exists" in str(errors):
            print("   ✅ Type bestaat al")
            return True
        print(f"   ❌ Fout: {errors}")
        return False
    
    if data and data.get("metaobjectTypeDefinitionCreate", {}).get("metaobjectTypeDefinition"):
        print("   ✅ Type aangemaakt")
        return True
    
    user_errors = data.get("metaobjectTypeDefinitionCreate", {}).get("userErrors", [])
    if user_errors and "already exists" in str(user_errors):
        print("   ✅ Type bestaat al")
        return True
    
    print(f"   ⚠️  Onbekende response")
    return True

def find_or_create_aroma_metaobject(aroma_naam):
    """Find existing aroma metaobject or create new one"""
    # First try to find it
    query = """
    query($query: String!) {
      metaobjects(type: "aroma", first: 10, query: $query) {
        edges {
          node {
            id
            field(key: "naam") {
              value
            }
          }
        }
      }
    }
    """
    
    data, _ = gql(query, {"query": f'field:naam:{aroma_naam}'})
    
    if data and data.get("metaobjects", {}).get("edges"):
        for edge in data["metaobjects"]["edges"]:
            if edge["node"]["field"]["value"] == aroma_naam:
                return edge["node"]["id"]
    
    # Not found, create it
    if DRY_RUN:
        return f"gid://shopify/Metaobject/aroma-{aroma_naam}"
    
    mutation = """
    mutation($input: MetaobjectInput!) {
      metaobjectCreate(metaobject: $input) {
        metaobject {
          id
        }
        userErrors {
          message
        }
      }
    }
    """
    
    input_data = {
        "type": "aroma",
        "fields": [
            {
                "key": "naam",
                "value": aroma_naam
            }
        ]
    }
    
    data, errors = gql(mutation, {"input": input_data})
    
    if errors:
        print(f"    Error creating aroma '{aroma_naam}': {errors}")
        return None
    
    if data and data.get("metaobjectCreate", {}).get("metaobject"):
        return data["metaobjectCreate"]["metaobject"]["id"]
    
    return None

def find_product_by_handle(handle):
    """Find product ID by handle"""
    query = """
    query($handle: String!) {
      productByHandle(handle: $handle) {
        id
        handle
        title
      }
    }
    """
    
    result, _ = gql(query, {"handle": handle})
    if result and "productByHandle" in result and result["productByHandle"]:
        return result["productByHandle"]["id"], result["productByHandle"]["title"]
    
    return None, None

def set_aroma_references(product_id, aroma_ids):
    """Set aroma metaobject references on product"""
    if not aroma_ids:
        return True, None
    
    # Format: "gid://shopify/Metaobject/aroma-1\ngid://shopify/Metaobject/aroma-2"
    references_value = "\n".join(aroma_ids)
    
    mutation = f"""
    mutation {{
      metafieldsSet(
        metafields: [
          {{
            ownerId: "{product_id}"
            namespace: "custom"
            key: "aromas"
            type: "list.metaobject_reference"
            value: "{references_value}"
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

# ======================= MAIN =======================

def main():
    global _access_token
    
    print("\n🍷 Shopify Aroma Metaobjects Sync")
    print("=" * 70)
    
    if DRY_RUN:
        print("⚠️  DRY RUN MODE\n")
    
    if not FLAVOR_FILE.exists():
        print(f"❌ FOUT: {FLAVOR_FILE} niet gevonden")
        sys.exit(1)
    
    print(f"📋 Laad {FLAVOR_FILE}...")
    with open(FLAVOR_FILE, 'r', encoding='utf-8') as f:
        flavor_data = json.load(f)
    
    print(f"✅ Geladen: {len(flavor_data)} producten\n")
    
    print("🔐 Verbind met Shopify...")
    _access_token = get_access_token()
    
    # Step 1: Create metaobject type
    if not create_metaobject_type():
        print("❌ Kan metaobject type niet aanmaken")
        sys.exit(1)
    
    # Step 2: Collect all unique aromas
    print("📝 Verzamel unieke aromas...\n")
    
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
    
    print(f"📊 {len(all_aroma_names)} unieke aromas gevonden\n")
    
    # Step 3: Create/find aroma metaobjects
    print("🔨 Create/Find aroma metaobjects...")
    aroma_id_map = {}
    
    for i, aroma_naam in enumerate(sorted(all_aroma_names), 1):
        print(f"   [{i:3d}/{len(all_aroma_names)}] {aroma_naam:30} ... ", end='', flush=True)
        
        aroma_id = find_or_create_aroma_metaobject(aroma_naam)
        if aroma_id:
            aroma_id_map[aroma_naam] = aroma_id
            print("✅")
        else:
            print("❌")
    
    print(f"\n✅ {len(aroma_id_map)} aroma metaobjecten klaar\n")
    
    # Step 4: Set references on products
    print(f"🚀 Sync {len(aroma_map)} producten")
    if DRY_RUN:
        print("   (DRY RUN - geen echte updates)\n")
    else:
        print()
    
    successful = 0
    failed = 0
    not_found = 0
    errors = []
    
    for i, (handle, aroma_names) in enumerate(aroma_map.items(), 1):
        print(f"[{i:3d}/{len(aroma_map)}] {handle[:50]:50} ... ", end='', flush=True)
        
        product_id, _ = find_product_by_handle(handle)
        
        if not product_id:
            print("❌ Niet gevonden")
            not_found += 1
            continue
        
        # Get aroma IDs for this product
        aroma_ids = [aroma_id_map[naam] for naam in aroma_names if naam in aroma_id_map]
        
        if not aroma_ids:
            print("❌ Geen aromas")
            failed += 1
            continue
        
        success, error = set_aroma_references(product_id, aroma_ids)
        
        if success:
            print(f"✅ ({len(aroma_ids)} aromas)")
            successful += 1
        else:
            print(f"❌ {error}")
            failed += 1
            errors.append({"handle": handle, "error": error})
    
    # Summary
    print(f"\n{'=' * 70}")
    print(f"SYNC VOLTOOID")
    print(f"{'=' * 70}\n")
    
    print(f"📊 Resultaten:")
    print(f"  ✅ Succesvol: {successful}/{len(aroma_map)}")
    print(f"  ❌ Fouten: {failed}/{len(aroma_map)}")
    print(f"  ⏭️  Niet gevonden: {not_found}/{len(aroma_map)}")
    print(f"  🔗 Aroma metaobjecten: {len(aroma_id_map)}")
    
    if DRY_RUN:
        print(f"\n⚠️  DRY RUN - Geen echte updates")
    
    if errors:
        print(f"\n⚠️  Fouten:")
        for err in errors[:5]:
            print(f"  - {err['handle']}: {err['error']}")
        if len(errors) > 5:
            print(f"  ... en {len(errors) - 5} meer")
    
    # Save results
    results = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": DRY_RUN,
        "products_synced": successful,
        "products_failed": failed,
        "products_not_found": not_found,
        "aroma_metaobjects_created": len(aroma_id_map),
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
    print(f"\n✨ Metaobjecten Setup:")
    print(f"   Type: aroma")
    print(f"   Field: naam (single_line_text_field)")
    print(f"   Instances: {len(aroma_id_map)}")
    print(f"   Metafield: custom.aromas (list.metaobject_reference)")
    
    sys.exit(0 if failed == 0 else 1)

if __name__ == "__main__":
    main()
