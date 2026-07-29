import frappe

try:
    from frappe.tests import IntegrationTestCase
except ImportError:
    from frappe.tests.utils import FrappeTestCase as IntegrationTestCase


def before_test():
    frappe.db.commit()
