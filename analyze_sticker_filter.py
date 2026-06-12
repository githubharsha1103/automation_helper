#!/usr/bin/env python3
from telegram.ext import filters
import inspect

print("=== Sticker Filter Analysis ===\n")

print("1. Type of filters.Sticker:")
print(f"   {type(filters.Sticker)}")
print(f"   {filters.Sticker}")

print("\n2. Sticker filter signature/docstring:")
print(f"   {inspect.getsource(filters.Sticker)[:500]}")

print("\n3. How to create Sticker filter instance:")
sticker_filter = filters.Sticker()
print(f"   filters.Sticker() type: {type(sticker_filter)}")

print("\n4. Testing filter combination:")
try:
    combined = filters.Sticker() | (filters.TEXT & ~filters.COMMAND)
    print(f"   ✓ filters.Sticker() | (filters.TEXT & ~filters.COMMAND) works!")
    print(f"   Result type: {type(combined)}")
except Exception as e:
    print(f"   ✗ Error: {e}")

print("\n5. Alternative: Just TEXT for stickers")
try:
    simple = filters.TEXT & ~filters.COMMAND
    print(f"   ✓ Just using filters.TEXT & ~filters.COMMAND works")
    print(f"   Result type: {type(simple)}")
except Exception as e:
    print(f"   ✗ Error: {e}")

print("\n6. Checking if message.sticker is better approach:")
print("   Option 1: Use filters.Sticker() to match message with sticker")
print("   Option 2: Use filters.TEXT and check message.sticker in handler")
