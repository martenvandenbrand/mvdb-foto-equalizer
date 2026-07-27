#!/usr/bin/env python3
"""
Koper & Karaf - Shopify Aroma Sync
1. Creeert metafield definition (wine_profile.aroma_list)
2. Synced alle aromas naar Shopify
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
    """Get OAuth access token using client credentials"""
    if not CLIENT_ID or not CLIENT_SECRET:
        print("❌ FOUT: SHOPIFY_CLIENT_ID en SHOPIFY_CLIENT_SECRET niet ingesteld")
        print("\nZet deze in GitHub Secrets:")
        print("  Settings → Secrets and variables → Actions")
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
            
            # Handle rate limiting
            if r.status_code == 429:
                wait_time = int(r.headers.get("Retry-After", 2))
                print(f"  ⏱️  Rate limited, wacht {wait_time}s...")
                time.sleep(wait_time)
                continue
            
            data = r.json()
            
            # Handle throttling
            if "errors" in data and any("THROTTLED" in str(e) for e in data["errors"]):
                wait_time = 2 * (attempt + 1)
                print(f"  ⏱️  Throttled, wacht {wait_time}s...")
                time.sleep(wait_time)
                continue
            
            # Handle other errors
            if "errors" in data:
                return None, data['errors']
            
            return data.get("data"), None
        
        except requests.exceptions.Timeout:
            print(f"  ⏱️  Timeout, poging {attempt + 1}/6")
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            print(f"  ❌ Fout: {e}")
            return None, str(e)
    
    return None, "Te vaak gethrottled/timeout"

def create_metafield_definition():
    """Create metafield definition if it doesn't exist"""
    print("📋 Check/Create metafield definition...")
    
    mutation = """
    mutation {
      metafieldDefinitionCreate(
        definition: {
          namespace: "wine_profile"
          key: "aroma_list"
          type: "list.single_line_text_field"
          name: "Aroma List"
          description: "List of wine aromas (searchable)"
        }
      ) {
        metafieldDefinition {
          id
          namespace
          key
          type
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    if DRY_RUN:
        print("   (DRY RUN - skip definition create)")
        return True
    
    data, errors = gql(mutation)
    
    if errors:
        # Check if it's "already exists" error (that's fine)
        if "already exists" in str(errors):
            print("   ✅ Definition bestaat al")
            return True
        print(f"   ❌ Fout: {errors}")
        return False
    
    if data and data.get("metafieldDefinitionCreate", {}).get("metafieldDefinition"):
        print("   ✅ Definition aangemaakt")
        return True
    
    user_errors = data.get("metafieldDefinitionCreate", {}).get("userErrors", [])
    if user_errors:
        if "already exists" in str(user_errors):
            print("   ✅ Definition bestaat al")
            return True
        print(f"   ❌ {user_errors}")
        return False
    
    print("   ⚠️  Onbekende response")
    return True  # Continue anyway

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

def set_aromas(product_id, aroma_names):
    """Set aromas metafield"""
    # JSON array format
    aroma_list_json = json.dumps(aroma_names)
    aroma_list_escaped = aroma_list_json.replace('"', '\\"')
    
    mutation = f"""
    mutation {{
      metafieldsSet(
        metafields: [
          {{
            ownerId: "{product_id}"
            namespace: "wine_profile"
            key: "aroma_list"
            type: "list.single_line_text_field"
            value: "{aroma_list_escaped}"
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
    
    print("\n🍷 Shopify Aroma Sync")
    print("=" * 70)
    
    if DRY_RUN:
        print("⚠️  DRY RUN MODE - Geen updates zullen worden gedaan\n")
    
    # Load flavor data
    if not FLAVOR_FILE.exists():
        print(f"❌ FOUT: {FLAVOR_FILE} niet gevonden")
        sys.exit(1)
    
    print(f"📋 Laad {FLAVOR_FILE}...")
    with open(FLAVOR_FILE, 'r', encoding='utf-8') as f:
        flavor_data = json.load(f)
    
    print(f"✅ Geladen: {len(flavor_data)} producten\n")
    
    # Get access token
    print("🔐 Verbind met Shopify...")
    _access_token = get_access_token()
    
    # Create metafield definition
    if not create_metafield_definition():
        print("❌ Kan definition niet aanmaken")
        sys.exit(1)
    
    # Prepare aroma data
    print("📝 Bereid aromas voor...\n")
    
    aroma_map = {}
    for handle, aromas in flavor_data.items():
        primary = aromas.get('primair', [])
        secondary = aromas.get('secundair', [])
        all_aromas = primary + secondary
        
        # Extract ONLY the "naam" values and remove duplicates
        seen_names = set()
        unique_aroma_names = []
        for aroma in all_aromas:
            naam = aroma.get('naam', '').strip()
            if naam and naam not in seen_names:
                unique_aroma_names.append(naam)
                seen_names.add(naam)
        
        aroma_map[handle] = unique_aroma_names
    
    # Process products
    handles = list(aroma_map.keys())
    total = len(handles)
    
    print(f"🚀 Sync {total} producten")
    if DRY_RUN:
        print("   (DRY RUN - geen echte updates)\n")
    else:
        print()
    
    successful = 0
    failed = 0
    not_found = 0
    errors = []
    total_aromas = 0
    
    for i, handle in enumerate(handles, 1):
        # Progress
        print(f"[{i:3d}/{total}] {handle[:50]:50} ... ", end='', flush=True)
        
        # Find product
        product_id, title = find_product_by_handle(handle)
        
        if not product_id:
            print("❌ Niet gevonden")
            not_found += 1
            continue
        
        # Set aromas
        aroma_names = aroma_map[handle]
        success, error = set_aromas(product_id, aroma_names)
        
        if success:
            print(f"✅ ({len(aroma_names)} aromas)")
            successful += 1
            total_aromas += len(aroma_names)
        else:
            print(f"❌ {error}")
            failed += 1
            errors.append({"handle": handle, "error": error})
    
    # Summary
    print(f"\n{'=' * 70}")
    print(f"SYNC VOLTOOID")
    print(f"{'=' * 70}\n")
    
    print(f"📊 Resultaten:")
    print(f"  ✅ Succesvol: {successful}/{total}")
    print(f"  ❌ Fouten: {failed}/{total}")
    print(f"  ⏭️  Niet gevonden: {not_found}/{total}")
    print(f"  📈 Totale aromas gesyncet: {total_aromas}")
    
    if DRY_RUN:
        print(f"\n⚠️  DRY RUN - Geen echte updates gedaan")
    
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
        "total": total,
        "successful": successful,
        "failed": failed,
        "not_found": not_found,
        "total_aromas_synced": total_aromas,
        "metafield": {
            "namespace": "wine_profile",
            "key": "aroma_list",
            "type": "list.single_line_text_field"
        },
        "errors": errors[:10]
    }
    
    results_file = Path(f"aroma_sync_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(results_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 Resultaten: {results_file}")
    print(f"\n✨ Metaveld:")
    print(f"   Namespace: wine_profile")
    print(f"   Key: aroma_list")
    print(f"   Type: list.single_line_text_field")
    
    # Exit code
    sys.exit(0 if failed == 0 else 1)

if __name__ == "__main__":
    main()
