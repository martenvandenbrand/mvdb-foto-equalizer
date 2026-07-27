#!/usr/bin/env python3
"""
Simple metaobject creation test with correct field key "aroma"
"""

import os
import json
import requests

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

# Test 1: List existing aroma metaobjects
print("📋 Test 1: Check if any aroma metaobjects exist...")
query = """
{
  metaobjects(type: "aroma", first: 10) {
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

if data.get("data", {}).get("metaobjects", {}).get("edges"):
    edges = data["data"]["metaobjects"]["edges"]
    print(f"✅ Found {len(edges)} existing metaobjects")
    if len(edges) > 0:
        print(f"\nFirst metaobject:")
        print(f"   ID: {edges[0]['node']['id']}")
        print(f"   Fields:")
        for field in edges[0]['node'].get('fields', []):
            print(f"     - {field['key']}: {field['value']}")
else:
    print("⚠️  No metaobjects found yet")

# Test 2: Try to create with field "aroma"
print("\n🔨 Test 2: Try creating metaobject with field 'aroma'...")
mutation = """
mutation {
  metaobjectCreate(metaobject: {
    type: "aroma"
    fields: [
      {
        key: "aroma"
        value: "test-appel"
      }
    ]
  }) {
    metaobject {
      id
      type
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

r = requests.post(API_URL, headers=headers, json={"query": mutation})
data = r.json()

print("\nFull response:")
print(json.dumps(data, indent=2))

# Check result
metaobject = data.get("data", {}).get("metaobjectCreate", {}).get("metaobject")
errors = data.get("data", {}).get("metaobjectCreate", {}).get("userErrors", [])
api_errors = data.get("errors", [])

if metaobject:
    print(f"\n✅ SUCCESS! Metaobject created with ID: {metaobject['id']}")
elif errors:
    print(f"\n❌ User errors:")
    for err in errors:
        print(f"   Field: {err.get('field')}")
        print(f"   Message: {err['message']}")
elif api_errors:
    print(f"\n❌ API errors:")
    for err in api_errors:
        print(f"   {err['message']}")
else:
    print("\n❌ Unknown error - no metaobject, no errors")
