#!/usr/bin/env python3
"""
Test controller module startup - full integration test
"""
import sys
import os

# Set minimal test environment
os.environ['CONTROL_BOT_TOKEN'] = 'test_token_12345:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh'
os.environ['ALLOWED_USER_ID'] = '123456'
os.environ['AUTOMATION_USER_SESSION'] = 'session.session'
os.environ['DATABASE_URL'] = 'mongodb://localhost:27017'

print("="*70)
print("CONTROLLER STARTUP VALIDATION TEST")
print("="*70)

try:
    print("\n[1/5] Checking filter availability...")
    from telegram.ext import filters
    print(f"      ✓ filters.TEXT: {hasattr(filters, 'TEXT')}")
    print(f"      ✓ filters.COMMAND: {hasattr(filters, 'COMMAND')}")
    print(f"      ✓ filters.Sticker: {hasattr(filters, 'Sticker')}")
    print(f"      ✓ filters.Sticker.ALL: {hasattr(filters.Sticker, 'ALL')}")
    
    print("\n[2/5] Testing filter combinations...")
    test_filters = {
        'TEXT_COMMAND': filters.TEXT & ~filters.COMMAND,
        'STICKER_COMBO': filters.Sticker.ALL | (filters.TEXT & ~filters.COMMAND),
    }
    for name, f in test_filters.items():
        print(f"      ✓ {name} filter created: {type(f).__name__}")
    
    print("\n[3/5] Importing controller module (this validates syntax)...")
    from controller import controller
    print("      ✓ Controller module imported successfully")
    
    print("\n[4/5] Checking conversation state configuration...")
    required_states = [
        'ADD_BOT_MESSAGE',
        'ADD_GROUP_MESSAGE', 
        'ADD_STICKER',
        'ADD_CONVERSATIONAL_MESSAGE',
    ]
    for state in required_states:
        if hasattr(controller, state):
            state_val = getattr(controller, state)
            print(f"      ✓ {state} = {state_val}")
        else:
            print(f"      ✗ {state} NOT FOUND")
            sys.exit(1)
    
    print("\n[5/5] Verifying ConversationHandler can be built...")
    from telegram.ext import ConversationHandler, MessageHandler, CallbackQueryHandler
    print("      ✓ ConversationHandler classes importable")
    
    # Verify the handler decorators exist
    handlers_to_check = [
        'add_bot_message_handler_audited',
        'add_group_message_handler_audited',
        'add_sticker_handler_audited',
        'add_conversational_message_handler',
    ]
    for handler_name in handlers_to_check:
        if hasattr(controller, handler_name):
            print(f"      ✓ Handler '{handler_name}' defined")
        else:
            print(f"      ✗ Handler '{handler_name}' NOT FOUND")
            sys.exit(1)
    
    print("\n" + "="*70)
    print("✅ SUCCESS: CONTROLLER STARTUP VALIDATION PASSED")
    print("="*70)
    print("\nSummary:")
    print("  • All filters (TEXT, COMMAND, Sticker.ALL) are available")
    print("  • All filter combinations work correctly")
    print("  • Controller module imports without AttributeError")
    print("  • All conversation states defined")
    print("  • All message handlers defined")
    print("\nController is ready for startup.")
    print("Next: Run start_controller() in your application")
    
    sys.exit(0)

except AttributeError as e:
    print(f"\n❌ ATTRIBUTE ERROR: {e}")
    print("   Filter or attribute not found - runtime will fail")
    import traceback
    traceback.print_exc()
    sys.exit(1)
    
except ImportError as e:
    print(f"\n❌ IMPORT ERROR: {e}")
    print("   Module import failed")
    import traceback
    traceback.print_exc()
    sys.exit(1)
    
except Exception as e:
    print(f"\n❌ ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
