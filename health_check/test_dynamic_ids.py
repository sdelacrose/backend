#!/usr/bin/env python3
"""
Test script to demonstrate the dynamic ID management system.

This script shows how the DynamicIDManager handles dependencies between endpoints
and automatically resolves IDs for related resources.
"""

import os
import sys
import django
from pathlib import Path

# Add the backend directory to the Python path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecpmpapp.settings.development')
django.setup()

from health_check.views import DynamicIDManager, get_admin_user


def test_dynamic_id_manager():
    """Test the DynamicIDManager functionality."""
    print("🧪 Testing Dynamic ID Management System")
    print("=" * 50)
    
    # Initialize the ID manager
    id_manager = DynamicIDManager()
    
    # Test payload with dependencies
    test_payload = {
        'milestone_payload': {
            'name': 'Test Milestone',
            'project': None,  # Will be filled dynamically
            'description': 'Test milestone',
            'status': 'open'
        },
        'action_items_payload': {
            'name': 'Test Action Item',
            'milestone': None,  # Will be filled dynamically
            'status': 'open'
        },
        'expense_card_payload': {
            'name': 'Test Expense Card',
            'project': None,  # Will be filled dynamically
            'category': 'milage'
        }
    }
    
    print("📋 Original payload:")
    print_payload(test_payload)
    
    # Update payload with dynamic IDs
    updated_payload = id_manager.update_payload_with_dynamic_ids(test_payload)
    
    print("\n🔄 Updated payload with dynamic IDs:")
    print_payload(updated_payload)
    
    # Test ID resolution for different endpoints
    print("\n🔍 Testing ID resolution:")
    test_endpoints = [
        'milestone',
        'project', 
        'action_items',
        'expense_card'
    ]
    
    for endpoint in test_endpoints:
        id_value = id_manager.get_id_for_endpoint('', endpoint)
        print(f"  {endpoint}: {id_value}")
    
    # Test endpoint type detection
    print("\n🎯 Testing endpoint type detection:")
    test_urls = [
        'http://localhost:8000/api/project_management/milestone/',
        'http://localhost:8000/api/project_management/milestone/123/',
        'http://localhost:8000/api/project_management/action_items/',
        'http://localhost:8000/api/project_management/expense-card/456/',
        'http://localhost:8000/api/companyprofileapis/notice-board/',
        'http://localhost:8000/api/userapis/events/'
    ]
    
    for url in test_urls:
        endpoint_type = id_manager._detect_endpoint_type(url)
        print(f"  {url} → {endpoint_type}")
    
    print("\n✅ Dynamic ID management test completed!")


def print_payload(payload):
    """Pretty print a payload dictionary."""
    import json
    print(json.dumps(payload, indent=2))


def test_admin_user():
    """Test admin user retrieval."""
    print("\n👤 Testing admin user retrieval:")
    try:
        admin_user = get_admin_user()
        print(f"  Admin user ID: {admin_user.id}")
        print(f"  Admin user email: {admin_user.email}")
        print(f"  Admin user company: {admin_user.company}")
    except Exception as e:
        print(f"  Error getting admin user: {e}")


if __name__ == "__main__":
    print("🚀 Starting Dynamic ID Management Tests")
    print("=" * 50)
    
    try:
        test_admin_user()
        test_dynamic_id_manager()
        print("\n🎉 All tests completed successfully!")
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc() 