import frappe
from frappe import _
from frappe.model.document import Document


class PaymentRun(Document):

    def on_submit(self):
        from eft_payments.eft_payments.page.payment_run_wizard.payment_run_wizard import (
            _create_payment_entries
        )

        # Create one Payment Entry per supplier. Generating an ACH file is a
        # separate, explicit step (see export_ach_file) — not every Payment
        # Run is EFT, and even for the ones that are, submission shouldn't
        # silently attempt a bank export.
        _create_payment_entries(self)
        frappe.db.set_value("Payment Run", self.name, "status", "Submitted")
        frappe.db.commit()

    def before_cancel(self):
        payment_entries = frappe.get_all(
            "Payment Entry",
            filters={"payment_run": self.name, "docstatus": 1},
            fields=["name"]
        )

        # Clear back-links first
        frappe.db.sql("""
            UPDATE `tabPayment Run Item`
            SET payment_entry = NULL
            WHERE parent = %s
        """, self.name)

        for pe_ref in payment_entries:
            frappe.db.set_value(
                "Payment Entry", pe_ref["name"], "payment_run", None,
                update_modified=False
            )

        frappe.db.commit()

        failed = []
        for pe_ref in payment_entries:
            try:
                pe = frappe.get_doc("Payment Entry", pe_ref["name"])
                pe.cancel()
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Failed to cancel Payment Entry {pe_ref['name']}"
                )
                failed.append(pe_ref["name"])

        if failed:
            frappe.throw(_(
                "Could not cancel the following Payment Entries: {0}. "
                "Check the Error Log for details."
            ).format(", ".join(failed)))

    def on_cancel(self):
        frappe.db.set_value("Payment Run", self.name, "status", "Cancelled")