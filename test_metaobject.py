#!/usr/bin/env python3
"""
Test metaobject creation - debug version
"""

import os
import json
import requests
import sys

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
print("✅ Token ok")

headers = {
    "X-Shopify-Access-Token": token,
    "Content-Type": "application/json"
}

# Test 1: List existing metaobjects
print("\n📋 Test 1: List existing metaobjects...")
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

r = requests.post(API_URL, headers=headers, json={"query": query})
data = r.json()
print(json.dumps(data, indent=2))

# Test 2: Create one metaobject
print("\n🔨 Test 2: Create one metaobject...")
mutation = """
mutation {
  metaobjectCreate(metaobject: {
    type: "aroma"
    fields: [
      {
        key: "naam"
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
print(json.dumps(data, indent=2))

if data.get("data", {}).get("metaobjectCreate", {}).get("metaobject"):
    print("\n✅ Create success!")
else:
    print("\n❌ Create failed!")
    errors = data.get("data", {}).get("metaobjectCreate", {}).get("userErrors", [])
    if errors:
        print(f"Errors: {errors}")
