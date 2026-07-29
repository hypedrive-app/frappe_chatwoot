import frappe


def before_test():
    frappe.db.commit()
