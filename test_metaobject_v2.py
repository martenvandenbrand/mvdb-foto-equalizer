#!/usr/bin/env python3
"""
Test metaobject creation - v2 with field verification
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

# Step 1: Check field definition
print("📋 Step 1: Check aroma type field definition...")
query = """
{
  metaobjectTypeDefinitionByType(type: "aroma") {
    type
    name
    fieldDefinitions {
      key
      type
      name
    }
  }
}
"""

r = requests.post(API_URL, headers=headers, json={"query": query})
data = r.json()

if data.get("data", {}).get("metaobjectTypeDefinitionByType"):
    typedef = data["data"]["metaobjectTypeDefinitionByType"]
    print(f"✅ Type found: {typedef['name']}")
    print(f"\nFields:")
    for field in typedef.get("fieldDefinitions", []):
        print(f"  ✓ {field['key']} ({field['type']})")
    
    # Get first field key for testing
    test_field_key = typedef["fieldDefinitions"][0]["key"] if typedef.get("fieldDefinitions") else None
    
    if not test_field_key:
        print("\n❌ No fields found!")
        sys.exit(1)
else:
    print("❌ Type not found!")
    print(json.dumps(data, indent=2))
    sys.exit(1)

# Step 2: Create metaobject with correct field
print(f"\n🔨 Step 2: Create metaobject with field '{test_field_key}'...")
time.sleep(1)

mutation = f"""
mutation {{
  metaobjectCreate(metaobject: {{
    type: "aroma"
    fields: [
      {{
        key: "{test_field_key}"
        value: "test-appel-v2"
      }}
    ]
  }}) {{
    metaobject {{
      id
      type
      fields {{
        key
        value
      }}
    }}
    userErrors {{
      field
      message
    }}
  }}
}}
"""

r = requests.post(API_URL, headers=headers, json={"query": mutation})
data = r.json()

metaobject = data.get("data", {}).get("metaobjectCreate", {}).get("metaobject")
errors = data.get("data", {}).get("metaobjectCreate", {}).get("userErrors", [])

if metaobject:
    print(f"\n✅ SUCCESS! Metaobject created:")
    print(f"   ID: {metaobject['id']}")
    print(f"   Type: {metaobject['type']}")
    print(f"   Fields:")
    for field in metaobject.get("fields", []):
        print(f"     - {field['key']}: {field['value']}")
else:
    print(f"\n❌ Failed to create:")
    print(json.dumps(data, indent=2))
    if errors:
        for err in errors:
            print(f"\nError: {err['message']}")
