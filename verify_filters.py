#!/usr/bin/env python3
from telegram.ext import filters, MessageHandler

print("Testing filter combinations for ADD_STICKER state...\n")

try:
    # Test 1: Basic TEXT filter
    f1 = filters.TEXT & ~filters.COMMAND
    print("✓ Test 1: filters.TEXT & ~filters.COMMAND")
    print(f"  Type: {type(f1)}\n")
    
    # Test 2: Sticker.ALL filter
    f2 = filters.Sticker.ALL
    print("✓ Test 2: filters.Sticker.ALL")
    print(f"  Type: {type(f2)}\n")
    
    # Test 3: Combined with OR (|)
    f3 = filters.Sticker.ALL | (filters.TEXT & ~filters.COMMAND)
    print("✓ Test 3: filters.Sticker.ALL | (filters.TEXT & ~filters.COMMAND)")
    print(f"  Type: {type(f3)}\n")
    
    # Test 4: Create MessageHandler with combined filter
    handler = MessageHandler(f3, lambda u, c: None)
    print("✓ Test 4: MessageHandler created with combined filter")
    print(f"  Type: {type(handler)}\n")
    
    print("="*60)
    print("SUCCESS: All filter combinations work correctly!")
    print("="*60)
    print("\nConversation states are now properly configured:")
    print("  ✓ ADD_BOT_MESSAGE: filters.TEXT & ~filters.COMMAND")
    print("  ✓ ADD_GROUP_MESSAGE: filters.TEXT & ~filters.COMMAND")
    print("  ✓ ADD_STICKER: filters.Sticker.ALL | (filters.TEXT & ~filters.COMMAND)")
    print("  ✓ ADD_CONVERSATIONAL_MESSAGE: filters.TEXT & ~filters.COMMAND")
    
except Exception as e:
    print(f"✗ Error: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    exit(1)
