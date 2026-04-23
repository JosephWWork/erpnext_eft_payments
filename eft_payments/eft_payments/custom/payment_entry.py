import frappe
from frappe import _


def on_cancel(doc, method):
    """
    Block direct cancellation of a Payment Entry that belongs to a Payment Run.
    Force the user to cancel via the Payment Run instead.
    """
    if doc.payment_run:
        pr_status = frappe.db.get_value("Payment Run", doc.payment_run, "status")
        if pr_status == "Exported":
            frappe.throw(_(
                "Payment Entry {0} belongs to Payment Run {1} which has already "
                "been exported. Please contact your bank before cancelling."
            ).format(doc.name, doc.payment_run))

        frappe.throw(_(
            "Payment Entry {0} belongs to Payment Run {1}. "
            "Please cancel the Payment Run instead, which will cancel all "
            "its Payment Entries together."
        ).format(doc.name, doc.payment_run))