#!/usr/bin/env python3
"""
Publish all aroma metaobjects to Online Store
"""

import os
import json
import requests
import sys
import time

SHOP = os.getenv("SHOP", "koperenkaraf.myshopify.com")
CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
API_VERSION = "2026-01"

API_URL = f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json"
TOKEN_URL = f"https://{SHOP}/admin/oauth/access_token"

print("🔐 Getting token...")
r = requests.post(TOKEN_URL, timeout=30, data={
    "grant_type": "client_credentials",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET
})
r.raise_for_status()
token = r.json()["access_token"]
print("✅ Token ok\n")

headers = {
    "X-Shopify-Access-Token": token,
    "Content-Type": "application/json"
}

# Step 1: Get all aroma metaobjects
print("📋 Listing all aroma metaobjects...")
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

r = requests.post(API_URL, headers=headers, json={"query": query})
data = r.json()

edges = data.get("data", {}).get("metaobjects", {}).get("edges", [])
print(f"✅ Found {len(edges)} metaobjects\n")

if not edges:
    print("No metaobjects found!")
    sys.exit(1)

# Step 2: Publish each one
print(f"📤 Publishing {len(edges)} metaobjects to Online Store...\n")

published = 0
failed = 0
errors_list = []

for i, edge in enumerate(edges, 1):
    node = edge["node"]
    aroma_id = node["id"]
    aroma_name = ""
    
    for field in node.get("fields", []):
        if field.get("key") == "aroma":
            aroma_name = field.get("value", "?")
            break
    
    print(f"[{i:3d}/{len(edges)}] {aroma_name:30} ... ", end='', flush=True)
    
    # Publish mutation
    mutation = f"""
    mutation {{
      publishablePublish(
        id: "{aroma_id}"
        input: {{
          publicationIds: ["gid://shopify/Publication/1"]
        }}
      ) {{
        publishable {{
          id
        }}
        userErrors {{
          field
          message
        }}
      }}
    }}
    """
    
    r = requests.post(API_URL, headers=headers, json={"query": mutation})
    response = r.json()
    
    if "errors" in response:
        print(f"❌")
        failed += 1
        errors_list.append({
            "aroma": aroma_name,
            "error": response["errors"][0]["message"]
        })
        continue
    
    publish_result = response.get("data", {}).get("publishablePublish", {})
    user_errors = publish_result.get("userErrors", [])
    
    if user_errors:
        print(f"❌")
        failed += 1
        errors_list.append({
            "aroma": aroma_name,
            "error": user_errors[0]["message"]
        })
    else:
        print("✅")
        published += 1

print(f"\n{'='*70}")
print(f"PUBLISH VOLTOOID")
print(f"{'='*70}\n")

print(f"📊 Resultaten:")
print(f"  ✅ Published: {published}/{len(edges)}")
print(f"  ❌ Failed: {failed}/{len(edges)}")

if errors_list:
    print(f"\n⚠️  Errors:")
    for err in errors_list[:5]:
        print(f"  - {err['aroma']}: {err['error']}")

sys.exit(0 if failed == 0 else 1)
