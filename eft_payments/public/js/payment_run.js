frappe.ui.form.on('Payment Run', {
    on_submit(frm) {
        // on_submit hook updates status in DB after the form renders —
        // reload to pick up the new status and show the correct buttons
        setTimeout(() => frm.reload_doc(), 1000);
    },
    
    refresh(frm) {

        // ── Re-export ACH (already exported runs) ─────────────────
        if (frm.doc.status === 'Exported' && frm.doc.docstatus === 1) {
            frm.add_custom_button(__('Re-export ACH File'), function() {
                frappe.confirm(
                    'Re-generate and download the CPA 005 ACH file?',
                    function() {
                        frappe.call({
                            method: 'eft_payments.eft_payments.page.payment_run_wizard.payment_run_wizard.export_ach_file',
                            args: { payment_run_name: frm.doc.name },
                            freeze: true,
                            freeze_message: 'Generating ACH file...',
                            callback: function(r) {
                                if (r.message && r.message.status === 'success') {
                                    frappe.show_alert({
                                        message: `ACH file generated: ${r.message.filename}`,
                                        indicator: 'green'
                                    });
                                    window.open(r.message.file_url, '_blank');
                                    frm.reload_doc();
                                }
                            }
                        });
                    }
                );
            }, __('Actions'));
        }

        // ── Send Remittance Emails ────────────────────────────────
        if (frm.doc.status === 'Exported' && frm.doc.docstatus === 1) {
            frm.add_custom_button(__('Send Remittance Emails'), function() {

                // Let them choose the print format
                let d = new frappe.ui.Dialog({
                    title: 'Send Remittance Advices',
                    fields: [
                        {
                            fieldname: 'print_format',
                            label: 'Print Format',
                            fieldtype: 'Link',
                            options: 'Print Format',
                            default: 'EFT Remittance Advice',
                            get_query: () => ({
                                filters: { doc_type: 'Payment Entry' }
                            }),
                            reqd: 1
                        },
                        {
                            fieldtype: 'HTML',
                            options: `<div style="color:#888; font-size:12px; padding:6px 0;">
                                This will send a remittance advice email to each supplier
                                in this Payment Run using the selected print format.
                            </div>`
                        }
                    ],
                    primary_action_label: 'Send Emails',
                    primary_action(values) {
                        d.hide();
                        frappe.call({
                            method: 'eft_payments.eft_payments.page.payment_run_wizard.payment_run_wizard.send_remittance_emails',
                            args: {
                                payment_run_name: frm.doc.name,
                                print_format: values.print_format
                            },
                            freeze: true,
                            freeze_message: 'Sending remittance emails...',
                            callback: function(r) {
                                if (!r.message) return;
                                let msg  = r.message;
                                let html = `<b>${msg.sent}</b> email(s) sent successfully.`;

                                if (msg.failed && msg.failed.length) {
                                    html += `<br><span style="color:#c0392b;">
                                        Failed: ${msg.failed.join(', ')}
                                    </span>`;
                                }
                                if (msg.no_email && msg.no_email.length) {
                                    html += `<br><span style="color:#e67e22;">
                                        No email on file: ${msg.no_email.join(', ')}
                                    </span>`;
                                }

                                frappe.msgprint({
                                    title: 'Remittance Emails',
                                    message: html,
                                    indicator: msg.failed.length ? 'orange' : 'green'
                                });
                            }
                        });
                    }
                });
                d.show();

            }, __('Actions'));
        }

        // ── Intercept native Cancel for Exported runs ─────────────
        if (frm.doc.status === 'Exported' && frm.doc.docstatus === 1) {
            frm.page.btn_secondary.hide();

            frm.add_custom_button(__('Cancel'), function() {
                frappe.confirm(
                    `<div style="color:#c0392b; font-weight:600; font-size:14px; margin-bottom:10px;">
                        ⚠ Warning: ACH File Already Exported
                     </div>
                     <p>This Payment Run has already been exported and the ACH file
                     may have been submitted to RBC.</p>
                     <p><strong>Are you sure you want to proceed?</strong></p>`,
                    function() {
                        frappe.confirm(
                            `<div style="color:#c0392b; font-weight:600; font-size:14px; margin-bottom:10px;">
                                ⚠ Final Warning
                             </div>
                             <p>Cancelling this Payment Run will cancel
                             <strong>all associated Payment Entries</strong>.</p>
                             <p>If this file was already processed by RBC, cancelling
                             here <strong>will not reverse the actual bank transfers</strong>.
                             You must contact RBC directly to reverse any payments.</p>
                             <p>Do you want to continue?</p>`,
                            function() {
                                frappe.call({
                                    method: 'frappe.desk.form.save.cancel',
                                    args: {
                                        doctype: frm.doc.doctype,
                                        name: frm.doc.name
                                    },
                                    freeze: true,
                                    freeze_message: 'Cancelling Payment Run...',
                                    callback: function() {
                                        frm.reload_doc();
                                    }
                                });
                            }
                        );
                    }
                );
            }).addClass('btn-danger');
        }

        // ── Status indicator ──────────────────────────────────────
        const status_colors = {
            'Draft':     'orange',
            'Submitted': 'green',
            'Exported':  'blue',
            'Cancelled': 'red'
        };
        frm.page.set_indicator(
            frm.doc.status,
            status_colors[frm.doc.status] || 'grey'
        );

        // ── Linked Payment Entries section ────────────────────────
        if (!frm.is_new() && frm.doc.docstatus !== 0) {
            frappe.call({
                method: 'frappe.client.get_list',
                args: {
                    doctype: 'Payment Entry',
                    filters: { payment_run: frm.doc.name },
                    fields: ['name', 'party', 'party_name', 'paid_amount',
                             'posting_date', 'docstatus'],
                    order_by: 'party_name asc'
                },
                callback: function(r) {
                    render_payment_entries(frm, r.message || []);
                }
            });
        }
    }
});

function render_payment_entries(frm, entries) {
    $(frm.wrapper).find('.pe-linked-section').remove();
    if (!entries.length) return;

    const status_badge = (docstatus) => {
        if (docstatus === 1) return '<span class="badge" style="background:#d4edda;color:#155724;">Submitted</span>';
        if (docstatus === 2) return '<span class="badge" style="background:#f8d7da;color:#721c24;">Cancelled</span>';
        return '<span class="badge" style="background:#fff3cd;color:#856404;">Draft</span>';
    };

    let rows = entries.map(pe => `
        <tr>
            <td>
                <a href="/app/payment-entry/${pe.name}" target="_blank">${pe.name}</a>
            </td>
            <td>${pe.party_name || pe.party}</td>
            <td style="text-align:right;">
                ${frappe.format(pe.paid_amount, { fieldtype: 'Currency' })}
            </td>
            <td style="text-align:center;">
                ${frappe.datetime.str_to_user(pe.posting_date)}
            </td>
            <td style="text-align:center;">${status_badge(pe.docstatus)}</td>
        </tr>
    `).join('');

    let total = entries.reduce((sum, pe) => sum + (pe.paid_amount || 0), 0);

    let $section = $(`
        <div class="pe-linked-section form-section"
             style="padding:16px 20px; border-top:1px solid #eee; margin-top:16px;">
            <div style="font-weight:600; font-size:13px; margin-bottom:10px; color:#333;">
                Payment Entries
                <span class="badge badge-pill"
                      style="background:#e8f4fd; color:#1a73e8; margin-left:6px;">
                    ${entries.length}
                </span>
            </div>
            <table class="table table-bordered" style="font-size:12px; margin-bottom:6px;">
                <thead style="background:#f5f5f5;">
                    <tr>
                        <th>Payment Entry</th>
                        <th>Supplier</th>
                        <th style="text-align:right;">Amount</th>
                        <th style="text-align:center;">Date</th>
                        <th style="text-align:center;">Status</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
                <tfoot style="background:#f9f9f9; font-weight:600;">
                    <tr>
                        <td colspan="2">Total</td>
                        <td style="text-align:right;">
                            ${frappe.format(total, { fieldtype: 'Currency' })}
                        </td>
                        <td colspan="2"></td>
                    </tr>
                </tfoot>
            </table>
        </div>
    `);

    $(frm.wrapper).find('.form-page').append($section);
}