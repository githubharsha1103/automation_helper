#!/usr/bin/env python3
from telegram.ext import filters

print("=== Available filter attributes ===")
filter_attrs = [attr for attr in dir(filters) if not attr.startswith('_')]
for attr in sorted(filter_attrs):
    print(f"  {attr}")

print("\n=== Checking for sticker-related filters ===")
sticker_related = [attr for attr in filter_attrs if 'sticker' in attr.lower()]
print(f"Sticker-related: {sticker_related if sticker_related else 'NONE FOUND'}")

print("\n=== Testing possible sticker filters ===")
test_names = ['STICKER', 'Sticker', 'sticker', 'document', 'Document', 'DOCUMENT']
for name in test_names:
    if hasattr(filters, name):
        print(f"✓ filters.{name} EXISTS")
    else:
        print(f"✗ filters.{name} NOT FOUND")

print("\n=== Checking for message content filters ===")
content_filters = [attr for attr in filter_attrs if 'message' in attr.lower() or 'content' in attr.lower()]
print(f"Content filters: {content_filters if content_filters else 'Check full list above'}")
