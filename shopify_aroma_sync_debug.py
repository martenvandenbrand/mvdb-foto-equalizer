#!/usr/bin/env python3
"""
DEBUG VERSION - Print alle errors!
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
        return r.json()["access_token"]
    except Exception as e:
        print(f"❌ OAuth error: {e}")
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
                time.sleep(int(r.headers.get("Retry-After", 2)))
                continue
            
            data = r.json()
            
            if "errors" in data:
                return None, data['errors']
            
            return data.get("data"), None
        
        except Exception as e:
            return None, str(e)
    
    return None, "Throttled"

def main():
    global _access_token
    
    print("\n🍷 SHOPIFY AROMA DEBUG")
    print("=" * 70)
    print(f"DRY_RUN: {DRY_RUN}\n")
    
    if not FLAVOR_FILE.exists():
        print(f"❌ {FLAVOR_FILE} niet gevonden")
        sys.exit(1)
    
    with open(FLAVOR_FILE, 'r', encoding='utf-8') as f:
        flavor_data = json.load(f)
    
    print(f"✅ Geladen: {len(flavor_data)} producten")
    
    print("🔐 Getting token...")
    _access_token = get_access_token()
    print("✅ Token ok\n")
    
    # Collect aromas
    print("📝 Collect aromas...")
    all_aroma_names = set()
    
    for handle, aromas in flavor_data.items():
        for aroma in aromas.get('primair', []) + aromas.get('secundair', []):
            naam = aroma.get('naam', '').strip()
            if naam:
                all_aroma_names.add(naam)
    
    print(f"✅ {len(all_aroma_names)} unique aromas\n")
    
    # Test create ONE aroma
    print("🔨 TEST: Try to create ONE aroma (appel)...")
    
    test_aroma = "appel"
    
    mutation = """
    mutation($input: MetaobjectInput!) {
      metaobjectCreate(metaobject: $input) {
        metaobject {
          id
          fields {
            key
            value
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    input_data = {
        "type": "aroma",
        "fields": [
            {
                "key": "aroma",
                "value": test_aroma
            }
        ]
    }
    
    print(f"\nSending mutation with input: {json.dumps(input_data, indent=2)}")
    
    if DRY_RUN:
        print("(DRY RUN - not really sending)")
    else:
        data, errors = gql(mutation, {"input": input_data})
        
        print(f"\nResponse:")
        if errors:
            print(f"❌ GraphQL Errors:")
            print(json.dumps(errors, indent=2))
        else:
            print(json.dumps(data, indent=2))
        
        # Parse result
        if data:
            result = data.get("metaobjectCreate", {})
            metaobject = result.get("metaobject")
            user_errors = result.get("userErrors", [])
            
            if metaobject:
                print(f"\n✅ SUCCESS! Created: {metaobject['id']}")
            elif user_errors:
                print(f"\n❌ User Errors:")
                for err in user_errors:
                    print(f"   Field: {err.get('field')}")
                    print(f"   Message: {err['message']}")
            else:
                print("\n❌ No metaobject, no errors - something weird")
    
    # Test find existing
    print("\n\n📋 TEST: List existing aroma metaobjects...")
    
    query = """
    {
      metaobjects(type: "aroma", first: 5) {
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
    
    data, errors = gql(query)
    
    if errors:
        print(f"❌ Error: {errors}")
    else:
        edges = data.get("metaobjects", {}).get("edges", [])
        print(f"Found: {len(edges)} metaobjects")
        for i, edge in enumerate(edges[:3], 1):
            node = edge["node"]
            print(f"\n  [{i}] {node['id']}")
            for field in node.get("fields", []):
                print(f"      {field['key']}: {field['value']}")

if __name__ == "__main__":
    main()
