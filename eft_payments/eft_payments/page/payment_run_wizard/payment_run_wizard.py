import frappe
from frappe import _
from collections import defaultdict

from erpnext.accounts.doctype.payment_entry.payment_entry import (
    get_outstanding_reference_documents,
    get_reference_details,
    get_outstanding_on_journal_entry,
)


@frappe.whitelist()
def get_outstanding_invoices(company=None, from_date=None, to_date=None, mode_of_payment=None):
    """
    Outstanding payables for every supplier in the company — Purchase Invoices,
    Journal Entries credited to a payable account, *and* any credit against
    the supplier (a debit/credit note or a credit Journal Entry) — via
    ERPNext's own reconciliation helper, so this matches what Accounts
    Payable reports show. Credits come back with a negative outstanding_amount
    so they can be applied against what's being paid.

    Filtering by mode_of_payment matters for performance, not just correctness:
    it narrows the supplier list *before* the expensive per-supplier lookups
    below run, instead of after. Without it, every supplier with any
    outstanding balance gets checked, regardless of how they're actually paid.
    """
    if not company:
        frappe.throw(_("Please select a Company."))

    filters = {"company": company}
    join = ""
    if mode_of_payment:
        join = "INNER JOIN `tabSupplier` s ON s.name = ple.party"
        filters["mode_of_payment"] = mode_of_payment

    suppliers_with_balance = frappe.db.sql(f"""
        SELECT ple.party AS party
        FROM `tabPayment Ledger Entry` ple
        {join}
        WHERE ple.party_type = 'Supplier'
          AND ple.account_type = 'Payable'
          AND ple.company = %(company)s
          AND ple.delinked = 0
          {"AND s.default_mode_of_payment = %(mode_of_payment)s" if mode_of_payment else ""}
        GROUP BY ple.party
        HAVING ABS(SUM(ple.amount_in_account_currency)) > 0.005
    """, filters, as_dict=True)

    supplier_map = {}

    for row in suppliers_with_balance:
        supplier = row["party"]
        party_account = get_party_account(supplier, company)

        docs = get_outstanding_reference_documents({
            "party_type": "Supplier",
            "party": supplier,
            "party_account": party_account,
            "company": company,
            "get_outstanding_invoices": True,
            "from_posting_date": from_date,
            "to_posting_date": to_date,
        }, validate=True) or []

        # ERPNext's helper only looks for negative-outstanding *Purchase
        # Invoices* (debit/credit notes) — it never returns a Journal Entry
        # with a credit balance, even though that's a real credit against the
        # supplier and shows up in Accounts Payable. Find those ourselves.
        docs = list(docs) + get_negative_outstanding_journal_entries(
            supplier, party_account, company
        )

        if not docs:
            continue

        supplier_name, default_currency = frappe.db.get_value(
            "Supplier", supplier, ["supplier_name", "default_currency"]
        )

        entry = supplier_map.setdefault(supplier, {
            "supplier": supplier,
            "supplier_name": supplier_name,
            "currency": default_currency,
            "invoice_count": 0,
            "total_outstanding": 0.0,
            "invoices": []
        })

        for d in docs:
            outstanding = float(d.get("outstanding_amount") or 0)
            if abs(outstanding) <= 0.005:
                continue

            entry["invoice_count"] += 1
            entry["total_outstanding"] += outstanding
            entry["invoices"].append({
                "name":               d.get("voucher_no"),
                "reference_type":     d.get("voucher_type"),
                "supplier":           supplier,
                "supplier_name":      supplier_name,
                "posting_date":       d.get("posting_date"),
                "due_date":           d.get("due_date"),
                "grand_total":        float(d.get("invoice_amount") or 0),
                "outstanding_amount": outstanding,
                "currency":           d.get("currency") or entry["currency"],
            })

    return list(supplier_map.values())


def get_negative_outstanding_journal_entries(party, party_account, company):
    """
    ERPNext's get_outstanding_reference_documents() doesn't return Journal
    Entries with a credit balance (its negative-outstanding lookup is
    hardcoded to Purchase Invoice) — so find candidate JEs from the Payment
    Ledger Entry ourselves, then get the authoritative outstanding amount for
    each from the same helper ERPNext uses when a JE is actually applied as a
    Payment Entry reference, so the two stay consistent.
    """
    candidates = frappe.db.sql("""
        SELECT against_voucher_no AS voucher_no
        FROM `tabPayment Ledger Entry`
        WHERE party_type = 'Supplier'
          AND party = %(party)s
          AND account = %(account)s
          AND account_type = 'Payable'
          AND voucher_type = 'Journal Entry'
          AND against_voucher_type = 'Journal Entry'
          AND company = %(company)s
          AND delinked = 0
        GROUP BY against_voucher_no
        HAVING SUM(amount_in_account_currency) < -0.005
    """, {"party": party, "account": party_account, "company": company}, as_dict=True)

    results = []
    for c in candidates:
        voucher_no = c["voucher_no"]
        outstanding_amount, total_amount = get_outstanding_on_journal_entry(
            voucher_no, "Supplier", party
        )
        outstanding_amount = float(outstanding_amount or 0)
        if outstanding_amount >= -0.005:
            continue

        je = frappe.db.get_value(
            "Journal Entry", voucher_no, ["posting_date", "due_date"], as_dict=True
        )
        results.append({
            "voucher_no":         voucher_no,
            "voucher_type":       "Journal Entry",
            "posting_date":       je.posting_date if je else None,
            "due_date":           je.due_date if je else None,
            "invoice_amount":     float(total_amount or 0),
            "outstanding_amount": outstanding_amount,
            "currency":           None,
        })

    return results


@frappe.whitelist()
def create_payment_run(company, bank_account, payment_date, selected_invoices):
    import json
    if isinstance(selected_invoices, str):
        selected_invoices = json.loads(selected_invoices)

    # Validate and group by supplier first — a credit that fully offsets (or
    # exceeds) one supplier's selected invoices should exclude just that
    # supplier from the run, not fail the whole request.
    by_supplier = defaultdict(list)
    for inv in selected_invoices:
        outstanding = float(inv.get("outstanding_amount") or 0)
        raw_amount = inv.get("pay_amount")
        amount = float(raw_amount) if raw_amount not in (None, "") else outstanding

        if outstanding == 0 or amount == 0:
            continue

        # A credit row (negative outstanding) must be applied as a negative
        # amount, and vice versa — no inventing a positive payment from a
        # credit or a negative one from an invoice.
        if (outstanding > 0) != (amount > 0):
            frappe.throw(_(
                "Invalid pay amount for {0}: it must be applied in the same "
                "direction as its outstanding balance."
            ).format(inv.get("name")))

        if abs(amount) > abs(outstanding) + 0.005:
            frappe.throw(_(
                "Pay amount for {0} exceeds its outstanding balance."
            ).format(inv.get("name")))

        by_supplier[inv["supplier"]].append({
            "reference_type": inv.get("reference_type") or "Purchase Invoice",
            "reference_name": inv["name"],
            "amount":         amount,
        })

    pr = frappe.new_doc("Payment Run")
    pr.company = company
    pr.bank_account = bank_account
    pr.payment_date = payment_date
    pr.status = "Draft"

    total = 0
    skipped_suppliers = []

    for supplier, items in by_supplier.items():
        supplier_total = sum(i["amount"] for i in items)
        if supplier_total <= 0.005:
            skipped_suppliers.append(supplier)
            continue

        for item in items:
            pr.append("items", {
                "reference_type": item["reference_type"],
                "reference_name": item["reference_name"],
                "supplier":       supplier,
                "amount":         item["amount"]
            })
            total += item["amount"]

    if not pr.items:
        if skipped_suppliers:
            frappe.throw(_(
                "No suppliers to pay: the selected credits fully offset (or "
                "exceeded) the invoices chosen for {0}."
            ).format(", ".join(skipped_suppliers)))
        frappe.throw(_("No valid invoice amounts to process."))

    pr.total_amount = total
    pr.insert()
    frappe.db.commit()

    return {
        "name": pr.name,
        "skipped_suppliers": skipped_suppliers,
    }

# @frappe.whitelist()
# def submit_payment_run(payment_run_name):
#     pr = frappe.get_doc("Payment Run", payment_run_name)

#     if pr.status != "Draft":
#         frappe.throw(_("Payment Run {0} is not in Draft status.").format(payment_run_name))

#     # Get the GL account linked to the paying bank account
#     bank_account_doc = frappe.get_doc("Bank Account", pr.bank_account)
#     paid_from_account = bank_account_doc.account
#     if not paid_from_account:
#         frappe.throw(_("Bank Account {0} has no linked GL Account.").format(pr.bank_account))

#     # Get company currency
#     company_currency = frappe.get_cached_value("Company", pr.company, "default_currency")

#     # Group items by supplier
#     supplier_invoices = defaultdict(list)
#     for item in pr.items:
#         if item.reference_type == "Purchase Invoice":
#             supplier_invoices[item.supplier].append(item)

#     if not supplier_invoices:
#         frappe.throw(_("No Purchase Invoice items found in this Payment Run."))

#     created_entries = []

#     for supplier, items in supplier_invoices.items():
#         try:
#             # Get supplier details
#             supplier_doc = frappe.get_doc("Supplier", supplier)

#             # Get the payable account for this supplier
#             paid_to_account = get_party_account(supplier, pr.company)

#             # Get account currencies
#             paid_from_currency = frappe.get_cached_value(
#                 "Account", paid_from_account, "account_currency"
#             ) or company_currency
#             paid_to_currency = frappe.get_cached_value(
#                 "Account", paid_to_account, "account_currency"
#             ) or company_currency

#             total_amount = sum(float(i.amount) for i in items)

#             # Build Payment Entry from scratch with all required fields
#             pe = frappe.new_doc("Payment Entry")
#             pe.payment_type             = "Pay"
#             pe.posting_date             = pr.payment_date
#             pe.company                  = pr.company
#             pe.mode_of_payment          = "EFT"
#             pe.party_type               = "Supplier"
#             pe.party                    = supplier
#             pe.party_name               = supplier_doc.supplier_name
#             pe.party_account            = paid_to_account
#             pe.party_account_currency   = paid_to_currency
#             pe.paid_from                = paid_from_account
#             pe.paid_from_account_currency = paid_from_currency
#             pe.paid_to                  = paid_to_account
#             pe.paid_to_account_currency = paid_to_currency
#             pe.paid_amount              = total_amount
#             pe.received_amount          = total_amount
#             pe.source_exchange_rate     = 1.0
#             pe.target_exchange_rate     = 1.0
#             pe.base_paid_amount         = total_amount
#             pe.base_received_amount     = total_amount
#             pe.reference_no             = pr.name
#             pe.reference_date           = pr.payment_date
#             pe.remarks                  = f"EFT Payment Run {pr.name}"
#             pe.payment_run              = pr.name

#             # Add invoice references
#             for item in items:
#                 inv = frappe.get_doc("Purchase Invoice", item.reference_name)
#                 pe.append("references", {
#                     "reference_doctype":  "Purchase Invoice",
#                     "reference_name":     item.reference_name,
#                     "due_date":           inv.due_date,
#                     "total_amount":       inv.grand_total,
#                     "outstanding_amount": inv.outstanding_amount,
#                     "allocated_amount":   float(item.amount),
#                 })

#             pe.insert(ignore_permissions=True)
#             pe.submit()
#             created_entries.append(pe.name)

#             # Link payment entry back to Payment Run items
#             for item in items:
#                 item.payment_entry = pe.name

#         except Exception:
#             frappe.log_error(frappe.get_traceback(),
#                 f"Payment Entry creation failed for supplier {supplier}")
#             frappe.throw(_(
#                 "Failed to create Payment Entry for supplier {0}. "
#                 "Check Error Log for details."
#             ).format(supplier))

#     pr.status = "Submitted"
#     pr.save()
#     frappe.db.commit()

#     # Auto-generate and attach the ACH file
#     try:
#         ach_result = _generate_ach_file(payment_run_name)
#         frappe.db.set_value("Payment Run", payment_run_name, {
#             "status": "Exported",
#             "docstatus": 1
#         }, update_modified=True)
#         frappe.db.commit()
#     except Exception:
#         frappe.log_error(frappe.get_traceback(), "ACH file generation failed")
#         ach_result = None

#     return {
#         "status": "success",
#         "payment_entries": len(created_entries),
#         "names": created_entries,
#         "ach_file": ach_result
#     }

@frappe.whitelist()
def submit_payment_run(payment_run_name):
    """
    Legacy whitelisted method — kept for backwards compatibility.
    Native submit via the form is now the preferred flow.
    """
    pr = frappe.get_doc("Payment Run", payment_run_name)
    if pr.docstatus == 0:
        pr.submit()  # triggers on_submit which does everything
    return {"status": "success"}


def _create_payment_entries(pr):
    """
    Internal helper — creates one Payment Entry per supplier for all
    Purchase Invoice items in the Payment Run. Called from on_submit.
    """
    from collections import defaultdict

    bank_account_doc = frappe.get_doc("Bank Account", pr.bank_account)
    paid_from_account = bank_account_doc.account
    if not paid_from_account:
        frappe.throw(_("Bank Account {0} has no linked GL Account.").format(pr.bank_account))

    company_currency = frappe.get_cached_value("Company", pr.company, "default_currency")

    # Group items by supplier
    supplier_invoices = defaultdict(list)
    for item in pr.items:
        if item.reference_type in ("Purchase Invoice", "Journal Entry"):
            supplier_invoices[item.supplier].append(item)

    if not supplier_invoices:
        frappe.throw(_("No valid Purchase Invoice or Journal Entry items found in this Payment Run."))

    for supplier, items in supplier_invoices.items():
        try:
            supplier_doc = frappe.get_doc("Supplier", supplier)
            mode_of_payment = supplier_doc.get("default_mode_of_payment")
            if not mode_of_payment:
                frappe.throw(_(
                    "Supplier {0} has no Default Mode of Payment set. "
                    "Set one on the Supplier record before including them in a Payment Run."
                ).format(supplier))

            paid_to_account = get_party_account(supplier, pr.company)

            paid_from_currency = frappe.get_cached_value(
                "Account", paid_from_account, "account_currency"
            ) or company_currency
            paid_to_currency = frappe.get_cached_value(
                "Account", paid_to_account, "account_currency"
            ) or company_currency

            total_amount = sum(float(i.amount) for i in items)
            if total_amount <= 0:
                frappe.throw(_(
                    "Net amount for supplier {0} is {1} — a credit applied against "
                    "this supplier's invoices must not bring the total to zero or "
                    "below. Adjust the Payment Run items before submitting."
                ).format(supplier, total_amount))

            pe = frappe.new_doc("Payment Entry")
            pe.payment_type             = "Pay"
            pe.posting_date             = pr.payment_date
            pe.company                  = pr.company
            pe.mode_of_payment          = mode_of_payment
            pe.party_type               = "Supplier"
            pe.party                    = supplier
            pe.party_name               = supplier_doc.supplier_name
            pe.party_account            = paid_to_account
            pe.party_account_currency   = paid_to_currency
            pe.paid_from                = paid_from_account
            pe.paid_from_account_currency = paid_from_currency
            pe.paid_to                  = paid_to_account
            pe.paid_to_account_currency = paid_to_currency
            pe.paid_amount              = total_amount
            pe.received_amount          = total_amount
            pe.source_exchange_rate     = 1.0
            pe.target_exchange_rate     = 1.0
            pe.base_paid_amount         = total_amount
            pe.base_received_amount     = total_amount
            pe.reference_no             = pr.name
            pe.reference_date           = pr.payment_date
            pe.remarks                  = f"EFT Payment Run {pr.name}"
            pe.payment_run              = pr.name

            for item in items:
                ref_details = get_reference_details(
                    item.reference_type, item.reference_name, paid_to_currency,
                    party_type="Supplier", party=supplier
                )
                pe.append("references", {
                    "reference_doctype":  item.reference_type,
                    "reference_name":     item.reference_name,
                    "due_date":           ref_details.due_date,
                    "total_amount":       ref_details.total_amount,
                    "outstanding_amount": ref_details.outstanding_amount,
                    "allocated_amount":   float(item.amount),
                })

            pe.insert(ignore_permissions=True)
            pe.submit()

            for item in items:
                item.payment_entry = pe.name

        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Payment Entry creation failed for supplier {supplier}"
            )
            frappe.throw(_(
                "Failed to create Payment Entry for supplier {0}. "
                "Check Error Log for details."
            ).format(supplier))

    # Save the payment_entry links back onto the Payment Run items
    for item in pr.items:
        frappe.db.set_value("Payment Run Item", item.name, 
                            "payment_entry", item.payment_entry)

def get_party_account(supplier, company):
    """Get the default payable account for a supplier."""
    supplier_doc = frappe.get_doc("Supplier", supplier)
    for account in supplier_doc.get("accounts", []):
        if account.company == company:
            return account.account

    default = frappe.get_cached_value("Company", company, "default_payable_account")
    if not default:
        frappe.throw(_(
            "No default payable account set for company {0}."
        ).format(company))
    return default

@frappe.whitelist()
def export_ach_file(payment_run_name):
    """
    Generate (or re-generate) the CPA 005 ACH file for a Payment Run.
    Always an explicit, manually-triggered action — never automatic on
    submit — since a Payment Run may include suppliers paid by other means
    (Wire, etc), and even a purely-EFT run shouldn't silently attempt a bank
    export the moment it's submitted.
    """
    pr = frappe.get_doc("Payment Run", payment_run_name)
    if pr.status not in ("Submitted", "Exported"):
        frappe.throw(_("Payment Run must be Submitted before exporting."))
    result = _generate_ach_file(payment_run_name)
    frappe.db.set_value("Payment Run", payment_run_name, "status", "Exported")
    frappe.db.commit()
    return result

def _generate_ach_file(payment_run_name):
    pr = frappe.get_doc("Payment Run", payment_run_name)
    settings = frappe.get_single("EFT Settings")

    if not settings.originator_id:
        frappe.throw(_("Please configure EFT Settings before exporting."))

    if not settings.mode_of_payment:
        frappe.throw(_(
            "Please set the EFT Mode of Payment in EFT Settings before exporting."
        ))

    # Increment file creation number — uncomment when done testing
    fcn = int(settings.file_creation_number or 0) + 1
    if fcn > 9999:
        fcn = 1
    frappe.db.set_value("EFT Settings", "EFT Settings", "file_creation_number", fcn)
    # fcn = 0  # unused while testing
    fcn_str         = str(fcn).zfill(4)
    # fcn_str = "TEST"

    # Only Payment Entries actually on the EFT rail go into this file — a
    # Payment Run can mix suppliers paid by different modes, and this export
    # only ever produces a CPA 005 / RBC-format ACH file.
    payment_entries = frappe.get_all(
        "Payment Entry",
        filters={
            "payment_run": payment_run_name,
            "docstatus": 1,
            "mode_of_payment": settings.mode_of_payment,
        },
        fields=["name", "party", "party_name", "paid_amount", "posting_date"]
    )

    if not payment_entries:
        frappe.throw(_(
            "No submitted Payment Entries with Mode of Payment {0} were found "
            "for this Payment Run."
        ).format(settings.mode_of_payment))

    today         = pr.payment_date
    julian_date   = "0" + today.strftime("%y%j")   # 0YYDDD (6 chars)
    filename_date = today.strftime("%Y%m%d")

    originator_id   = settings.originator_id.strip().ljust(10)[:10]   # AN, left justified
    orig_short_name = (settings.originator_short_name or "").strip().upper().ljust(15)[:15]
    orig_long_name  = (settings.originator_long_name or "").strip().ljust(30)[:30]
    data_centre     = (settings.data_centre or "").strip().zfill(5)[:5]  # N, zero filled
    
    lines              = []
    total_amount_cents = 0
    record_count       = 0   # counts C records (one per payment)

    # ── A Record (File Header) ────────────────────────────────────
    # Field 01: pos 01-01  (1)  AN  Record Type        "A"
    # Field 02: pos 02-10  (9)  N   Record Count       000000001
    # Field 03: pos 11-20  (10) AN  Client Number      left justified
    # Field 04: pos 21-24  (4)  AN  File Creation No.  "TEST" or 4-digit number
    # Field 05: pos 25-30  (6)  N   File Creation Date 0YYDDD
    # Field 06: pos 31-35  (5)  N   RB Processing Ctr  e.g. 00390 for Calgary
    # Field 07: pos 36-55  (20) AN  Reserved           blank
    # Field 08: pos 56-58  (3)  AN  Currency           CAD
    # Field 09: pos 59-1464 (1406) AN Filler           blank
    a_record = (
        "A"               # (1)
        + "000000001"     # (9)
        + originator_id   # (10)
        + fcn_str         # (4)
        + julian_date     # (6)
        + data_centre     # (5)
        + " " * 20        # reserved (20)
        + "CAD"           # (3)
        + " " * 1406      # filler (1406)
    )
    assert len(a_record) == 1464, f"A record length {len(a_record)} != 1464"
    lines.append(a_record)

    # ── C Records ────────────────────────────────────────────────
    # One C record per payment transaction.
    # Each C record = 24-byte header + 1 transaction segment (240 bytes)
    #                 + 5 empty segments (5 × 240 = 1200 bytes zero-filled)
    #               = 1464 bytes total
    #
    # Header (24 bytes):
    # Field 01: pos 01-01  (1)  AN  Record Type        "C"
    # Field 02: pos 02-10  (9)  N   Record Count       increment from 2
    # Field 03: pos 11-20  (10) AN  Client Number
    # Field 04: pos 21-24  (4)  AN  File Creation No.
    #
    # Segment One (240 bytes, pos 25-264):
    # Field 05: pos 25-27  (3)  AN  Transaction Code   e.g. 460
    # Field 06: pos 28-37  (10) N   Amount             $$$$$$$$¢¢
    # Field 07: pos 38-43  (6)  N   Payment Date       0YYDDD
    # Field 08: pos 44-52  (9)  N   Routing            bank(4) + transit(5)
    # Field 09: pos 53-64  (12) AN  Account Number     left justified, NO zero fill
    # Field 10: pos 65-86  (22) N   Reserved           zero fill
    # Field 11: pos 87-89  (3)  N   Reserved           zero fill
    # Field 12: pos 90-104 (15) AN  Client Short Name
    # Field 13: pos 105-134(30) AN  Customer Name      payee legal name
    # Field 14: pos 135-164(30) AN  Client Name        originator legal name
    # Field 15: pos 165-174(10) AN  Client Number
    # Field 16: pos 175-193(19) AN  Customer Number    blank (unused)
    # Field 17: pos 194-202(9)  N   Reserved           zero fill
    # Field 18: pos 203-214(12) AN  Reserved           blank
    # Field 19: pos 215-229(15) AN  Client Sundry Info blank (optional)
    # Field 20: pos 230-251(22) AN  Reserved           blank
    # Field 21: pos 252-253(2)  AN  Reserved           blank
    # Field 22: pos 254-264(11) AN  Reserved           blank
    #
    # Segments 2-6: zero-filled (unused)

    SEGMENT_SIZE  = 240   # one transaction segment
    EMPTY_SEGMENT = " " * SEGMENT_SIZE  # unused segments: zero fill per spec

    for pe in payment_entries:
        supplier_bank = get_supplier_bank_details(pe["party"])
        if not supplier_bank:
            frappe.throw(_(
                "Supplier {0} has no bank account configured. "
                "Please add bank details before exporting."
            ).format(pe["party"]))

        record_count       += 1
        seq_num             = str(record_count + 1).zfill(9)  # A is record 1
        amount_cents        = int(round(float(pe["paid_amount"]) * 100))
        total_amount_cents += amount_cents

        payee_name     = (pe["party_name"] or pe["party"]).ljust(30)[:30]
        routing        = supplier_bank["routing"]   # bank(4) + transit(5) = 9 digits
        account_number = (supplier_bank["account_number"] or "").strip().ljust(12)[:12]
        amount_str     = str(amount_cents).zfill(10)
        return_fi      = (settings.return_fi_number or "").strip().zfill(9)[:9]
        return_account = (settings.return_account or "").strip().ljust(12)[:12]

        segment_one = (
            "460"             # field 05: transaction code (3) AN
            + amount_str      # field 06: amount (10) N
            + julian_date     # field 07: payment date (6) N
            + routing         # field 08: routing (9) N
            + account_number  # field 09: account (12) AN
            + "0" * 22        # field 10: reserved N — zero fill
            + "0" * 3         # field 11: reserved N — zero fill
            + orig_short_name # field 12: client short name (15) AN
            + payee_name      # field 13: customer name (30) AN
            + orig_long_name  # field 14: client name (30) AN
            + originator_id   # field 15: client number (10) AN
            + (pe["party"] or "").strip().ljust(19)[:19]  # field 16: customer number (19) AN — supplier ID 
            + return_fi        # field 20: return FI (9) N
            + return_account   # field 21: return account (12) AN
            + "0" * 15         # field 22: sundry info (15) — zeros per working example
            + " " * 22         # field 23: stored trace number (22) AN — blank
            + " " * 2          # field 24: settlement code (2) AN — blank
            + "0" * 11         # field 25: invalid data element (11) N — all zeros
        )
        assert len(segment_one) == SEGMENT_SIZE, \
            f"Segment one length {len(segment_one)} != {SEGMENT_SIZE}"

        c_record = (
            "C"                          # (1)
            + seq_num                    # (9)
            + originator_id             # (10)
            + fcn_str                   # (4)
            + segment_one               # segment 1 (240)
            + EMPTY_SEGMENT * 5         # segments 2-6 zero-filled (1200)
        )
        assert len(c_record) == 1464, f"C record length {len(c_record)} != 1464"
        lines.append(c_record)

    # ── Z Record (File Trailer) ───────────────────────────────────
    # Field 01: pos 01-01  (1)  AN  Record Type        "Z"
    # Field 02: pos 02-10  (9)  N   Record Count       total records in file
    # Field 03: pos 11-20  (10) AN  Client Number
    # Field 04: pos 21-24  (4)  AN  File Creation No.
    # Field 05: pos 25-38  (14) N   Reserved           zero fill
    # Field 06: pos 39-46  (8)  N   Reserved           zero fill
    # Field 07: pos 47-60  (14) N   Total Credit Amt   $$$$$$$$$$$$¢¢
    # Field 08: pos 61-68  (8)  N   Total Credit Count number of C records
    # Field 09: pos 69-1464(1396)N  Filler             zero fill

    total_records = str(record_count + 2).zfill(9)  # A + C records + Z

    z_record = (
        "Z"                                          # (1)
        + total_records                              # (9)
        + originator_id                             # (10)
        + fcn_str                                   # (4)
        + "0" * 14                                  # field 05 reserved (14)
        + "0" * 8                                   # field 06 reserved (8)
        + str(total_amount_cents).zfill(14)         # field 07 total amount (14)
        + str(record_count).zfill(8)                # field 08 credit count (8)
        + "0" * 14                                   # field 09: value of err corr E (14) — zero
        + "0" * 8                                    # field 10: number of err corr E (8) — zero
        + "0" * 14                                   # field 11: value of err corr F (14) — zero
        + "0" * 8                                    # field 12: number of err corr F (8) — zero
        + " " * 1352                                 # field 13: filler blank (1352)                               # field 09 filler (1396)
    )
    assert len(z_record) == 1464, f"Z record length {len(z_record)} != 1464"
    lines.append(z_record)

    # ── Write file ────────────────────────────────────────────────
    file_content = "\n".join(lines)
    filename = f"EFT_{payment_run_name}_{filename_date}_{fcn_str}.txt"

    existing = frappe.get_all("File", filters={
        "attached_to_doctype": "Payment Run",
        "attached_to_name": payment_run_name,
        "file_name": ["like", "EFT_%"]
    }, fields=["name"])
    for f in existing:
        frappe.delete_doc("File", f["name"], ignore_permissions=True)

    file_doc = frappe.get_doc({
        "doctype": "File",
        "file_name": filename,
        "attached_to_doctype": "Payment Run",
        "attached_to_name": payment_run_name,
        "content": file_content,
        "is_private": 1
    })
    file_doc.insert(ignore_permissions=True)

    return {
        "status": "success",
        "filename": filename,
        "file_url": file_doc.file_url
    }


def get_supplier_bank_details(supplier):
    """
    Pull bank details from the Bank Account linked to the supplier.
    Routing = bank institution number (4 digits) + branch transit (5 digits)
    Per CPA 005 spec field 08: 0999 bank number + 99999 branch transit = 9 digits
    """
    bank_accounts = frappe.get_all(
        "Bank Account",
        filters={"party_type": "Supplier", "party": supplier},
        fields=["name", "bank", "branch_code", "bank_account_no"],
        limit=1
    )

    if not bank_accounts:
        return None

    ba = bank_accounts[0]

    transit = (ba.get("branch_code") or "").strip().zfill(5)[:5]

    # Institution number: 3-digit Canadian bank code, zero-padded to 4 digits
    # e.g. RBC=003 → 0003, TD=004 → 0004, BMO=001 → 0001
    institution = ""
    if ba.get("bank"):
        raw = frappe.db.get_value("Bank", ba["bank"], "swift_number") or ""
        # Take only digits, use first 3, pad to 4
        digits = ''.join(filter(str.isdigit, raw))[:3]
        institution = digits.zfill(4)
    else:
        institution = "0000"

    routing = institution + transit  # 4 + 5 = 9 digits

    return {
        "routing": routing,
        "account_number": ba.get("bank_account_no") or ""
    }

@frappe.whitelist()
def cancel_payment_run(payment_run_name):
    pr = frappe.get_doc("Payment Run", payment_run_name)

    if pr.status == "Draft":
        frappe.throw(_("Payment Run is already in Draft status."))

    if pr.status == "Exported":
        frappe.throw(_(
            "This Payment Run has already been exported to an ACH file. "
            "Please contact your bank before cancelling."
        ))

    # Get all submitted Payment Entries for this run
    payment_entries = frappe.get_all(
        "Payment Entry",
        filters={"payment_run": payment_run_name, "docstatus": 1},
        fields=["name"]
    )

    # Clear ALL back-links before attempting any cancellation:
    # 1. Clear payment_entry on every Payment Run Item row
    frappe.db.sql("""
        UPDATE `tabPayment Run Item`
        SET payment_entry = NULL
        WHERE parent = %s
    """, payment_run_name)

    # 2. Clear payment_run on each Payment Entry
    for pe_ref in payment_entries:
        frappe.db.set_value(
            "Payment Entry", pe_ref["name"], "payment_run", None,
            update_modified=False
        )

    frappe.db.commit()

    # Now cancel each Payment Entry — no back-links remain
    cancelled = []
    failed = []

    for pe_ref in payment_entries:
        try:
            pe = frappe.get_doc("Payment Entry", pe_ref["name"])
            pe.cancel()
            cancelled.append(pe_ref["name"])
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

    # Bypass ERPNext's submittable doc restrictions by writing directly to DB
    frappe.db.set_value("Payment Run", payment_run_name, {
        "status": "Draft",
        "docstatus": 0
    }, update_modified=True)
    frappe.db.commit()

    return {
        "status": "success",
        "cancelled": len(cancelled)
    }

@frappe.whitelist()
def send_remittance_emails(payment_run_name, print_format="EFT Remittance Advice"):
    pr = frappe.get_doc("Payment Run", payment_run_name)

    if pr.status not in ("Submitted", "Exported"):
        frappe.throw(_("Payment Run must be Submitted or Exported before sending remittances."))

    payment_entries = frappe.get_all(
        "Payment Entry",
        filters={"payment_run": payment_run_name, "docstatus": 1},
        fields=["name", "party", "party_name", "paid_amount"]
    )

    if not payment_entries:
        frappe.throw(_("No submitted Payment Entries found for this Payment Run."))

    sent     = []
    failed   = []
    no_email = []

    for pe_ref in payment_entries:
        try:
            # Get supplier's primary email
            supplier_email = get_supplier_email(pe_ref["party"])

            if not supplier_email:
                no_email.append(pe_ref["party_name"] or pe_ref["party"])
                continue

            # Render the print format as PDF
            pdf = frappe.get_print(
                doctype="Payment Entry",
                name=pe_ref["name"],
                print_format=print_format,
                as_pdf=True
            )

            pdf_filename = f"Remittance_{pe_ref['name']}_{pr.payment_date}.pdf"

            # Send with PDF as attachment, plain text body
            frappe.sendmail(
                recipients=[supplier_email],
                subject=f"Payment Notification — {pr.company} — {frappe.format(pr.payment_date, {'fieldtype': 'Date'})}",
                message=f"""
                    <p>Dear {pe_ref['party_name']},</p>
                    <p>Please find attached your remittance advice for a recent EFT payment from {pr.company}.</p>
                    <p>If you have any questions please don't hesitate to contact us.</p>
                    <p>Regards,<br>{pr.company}</p>
                """,
                attachments=[{
                    "fname": pdf_filename,
                    "fcontent": pdf
                }],
                reference_doctype="Payment Entry",
                reference_name=pe_ref["name"],
                now=True
            )

            sent.append(pe_ref["name"])

        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Remittance email failed for {pe_ref['party']}"
            )
            failed.append(pe_ref["party_name"] or pe_ref["party"])

    # Build result summary
    result = {
        "status": "success",
        "sent": len(sent),
        "failed": failed,
        "no_email": no_email
    }

    return result


def get_supplier_email(supplier):
    """Get the primary email address for a supplier via their linked contacts."""
    # Try the supplier's primary contact first
    contact_name = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Supplier", "link_name": supplier, "parenttype": "Contact"},
        "parent"
    )

    if contact_name:
        email = frappe.db.get_value("Contact", contact_name, "email_id")
        if email:
            return email

    # Fall back to any contact email linked to this supplier
    contacts = frappe.get_all(
        "Contact",
        filters={"email_id": ["!=", ""]},
        fields=["name", "email_id"],
        limit=1
    )

    # Last resort: check supplier record itself
    supplier_doc = frappe.get_doc("Supplier", supplier)
    if hasattr(supplier_doc, "email_id") and supplier_doc.email_id:
        return supplier_doc.email_id

    return None