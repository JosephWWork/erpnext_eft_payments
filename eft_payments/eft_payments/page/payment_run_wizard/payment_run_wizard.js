frappe.pages['payment-run-wizard'].on_page_load = function(wrapper) {
    var page = frappe.ui.make_app_page({
        parent: wrapper,
        title: 'Payment Run Wizard',
        single_column: true
    });

    // ── Filter bar ────────────────────────────────────────────────
    let company_field = page.add_field({
        fieldname: 'company',
        label: 'Company',
        fieldtype: 'Link',
        options: 'Company',
        default: frappe.defaults.get_default('company'),
        change() { load_invoices(); }
    });

    let from_date_field = page.add_field({
        fieldname: 'from_date',
        label: 'From Date',
        fieldtype: 'Date',
        change() { load_invoices(); }
    });

    let to_date_field = page.add_field({
        fieldname: 'to_date',
        label: 'To Date',
        fieldtype: 'Date',
        change() { load_invoices(); }
    });

    let bank_account_field = page.add_field({
        fieldname: 'bank_account',
        label: 'Pay From Bank Account',
        fieldtype: 'Link',
        options: 'Bank Account'
    });

    let payment_date_field = page.add_field({
        fieldname: 'payment_date',
        label: 'Payment Date',
        fieldtype: 'Date',
        default: frappe.datetime.get_today()
    });

    page.add_button('Refresh', () => load_invoices(), { icon: 'refresh' });

    // ── Main container ────────────────────────────────────────────
    let $body = $(`
        <div class="payment-run-body" style="padding: 20px;">
            <div class="invoice-table-wrapper"></div>
            <div class="payment-run-footer" style="margin-top:16px; display:none;">
                <div class="selected-summary" style="margin-bottom:10px; font-weight:600;"></div>
                <button class="btn btn-primary btn-create-run">Create Payment Run</button>
            </div>
        </div>
    `).appendTo(page.main);

    let selected_invoices = {};
    let all_supplier_data = [];

    // ── Helpers ───────────────────────────────────────────────────
    function fmt(amount) {
        return '$ ' + parseFloat(amount || 0).toFixed(2);
    }

    function encode_inv(inv) {
        return encodeURIComponent(JSON.stringify(inv));
    }

    function decode_inv($el) {
        return JSON.parse(decodeURIComponent($el.attr('data-inv')));
    }

    // Composite key — reference_type + name — since the list now mixes
    // Purchase Invoices and Journal Entries, which don't share a name space.
    function inv_key(inv) {
        return (inv.reference_type || 'Purchase Invoice') + '::' + inv.name;
    }

    function doc_route(inv) {
        return `/app/${frappe.router.slug(inv.reference_type || 'Purchase Invoice')}/${inv.name}`;
    }

    function is_overdue(due_date) {
        if (!due_date) return false;
        return frappe.datetime.str_to_obj(due_date) < frappe.datetime.str_to_obj(frappe.datetime.get_today());
    }

    // ── Load invoices ─────────────────────────────────────────────
    function load_invoices() {
        let company   = company_field.get_value();
        let from_date = from_date_field.get_value();
        let to_date   = to_date_field.get_value();

        $body.find('.invoice-table-wrapper').html(
            '<p class="text-muted" style="padding:20px;">Loading...</p>'
        );

        frappe.call({
            method: 'eft_payments.eft_payments.page.payment_run_wizard.payment_run_wizard.get_outstanding_invoices',
            args: { company, from_date, to_date },
            callback: function(r) {
                all_supplier_data = r.message || [];
                selected_invoices = {};
                update_footer();
                render_table(all_supplier_data);
            }
        });
    }

    // ── Render grouped table ──────────────────────────────────────
    function render_table(suppliers) {
        if (!suppliers.length) {
            $body.find('.invoice-table-wrapper').html(
                '<p class="text-muted" style="padding:20px;">No outstanding payables found.</p>'
            );
            return;
        }

        let rows = '';

        suppliers.forEach(s => {
            rows += `
                <tr class="supplier-header-row"
                    style="background:#f0f4f8; cursor:pointer;"
                    data-supplier="${s.supplier}">
                    <td style="width:40px; text-align:center;">
                        <input type="checkbox"
                               class="supplier-select-all"
                               data-supplier="${s.supplier}"
                               title="Select all for ${s.supplier_name}" />
                    </td>
                    <td colspan="4">
                        <strong style="font-size:13px;">${s.supplier_name}</strong>
                        <span style="color:#888; font-size:11px; margin-left:8px;">${s.supplier}</span>
                    </td>
                    <td style="text-align:right;">
                        <span class="supplier-selected-total"
                              data-supplier="${s.supplier}"
                              style="font-size:12px; color:#888; margin-right:8px;"></span>
                        <strong style="font-size:13px;">Owing: ${fmt(s.total_outstanding)}</strong>
                    </td>
                    <td style="text-align:center; white-space:nowrap;">
                        <button class="btn btn-xs btn-default btn-pay-full"
                                data-supplier="${s.supplier}"
                                style="font-size:11px;">Full</button>
                        <button class="btn btn-xs btn-default btn-pay-clear"
                                data-supplier="${s.supplier}"
                                style="font-size:11px; margin-left:2px;">Clear</button>
                    </td>
                    <td style="width:30px; text-align:center;">
                        <span class="toggle-icon" data-supplier="${s.supplier}">▾</span>
                    </td>
                </tr>
            `;

            s.invoices.forEach(inv => {
                let encoded = encode_inv(inv);
                let overdue = is_overdue(inv.due_date);
                let is_je   = inv.reference_type === 'Journal Entry';
                rows += `
                    <tr class="invoice-row" data-supplier="${s.supplier}" data-invoice="${inv_key(inv)}">
                        <td style="width:40px; text-align:center;">
                            <input type="checkbox"
                                   class="inv-checkbox"
                                   data-supplier="${s.supplier}"
                                   data-inv="${encoded}" />
                        </td>
                        <td style="padding-left:30px;">
                            <a href="${doc_route(inv)}" target="_blank">${inv.name}</a>
                            ${is_je ? '<span class="badge" style="background:#e8e8f8; color:#4a4a8a; margin-left:6px; font-size:10px;">JE</span>' : ''}
                        </td>
                        <td style="text-align:center; color:#888; font-size:12px;">
                            ${frappe.datetime.str_to_user(inv.posting_date)}
                        </td>
                        <td style="text-align:center; color:${overdue ? '#e74c3c' : '#888'}; font-size:12px;">
                            ${inv.due_date ? frappe.datetime.str_to_user(inv.due_date) : '—'}
                            ${overdue ? '<span title="Overdue"> ⚠</span>' : ''}
                        </td>
                        <td style="text-align:right; color:#888; font-size:12px;">
                            ${fmt(inv.grand_total)}
                        </td>
                        <td style="text-align:right; color:#888; font-size:12px;">
                            ${fmt(inv.outstanding_amount)}
                        </td>
                        <td style="text-align:right;">
                            <div style="display:flex; align-items:center; justify-content:flex-end; gap:6px;">
                                <input type="number"
                                       class="form-control pay-amount-input"
                                       data-invoice="${inv_key(inv)}"
                                       data-supplier="${s.supplier}"
                                       data-max="${inv.outstanding_amount}"
                                       data-inv="${encoded}"
                                       value="${inv.outstanding_amount}"
                                       min="0.01"
                                       max="${inv.outstanding_amount}"
                                       step="0.01"
                                       style="width:110px; text-align:right; font-size:12px; display:none;" />
                                <span class="pay-amount-display"
                                      data-invoice="${inv_key(inv)}"
                                      style="font-weight:600;">
                                    ${fmt(inv.outstanding_amount)}
                                </span>
                            </div>
                        </td>
                        <td></td>
                    </tr>
                `;
            });
        });

        let table = `
            <table class="table table-bordered" style="font-size:13px;">
                <thead style="background:#e8e8e8;">
                    <tr>
                        <th style="width:40px; text-align:center;">
                            <input type="checkbox" id="select-all" title="Select All" />
                        </th>
                        <th>Invoice / Supplier</th>
                        <th style="text-align:center;">Date</th>
                        <th style="text-align:center;">Due Date</th>
                        <th style="text-align:right;">Invoice Total</th>
                        <th style="text-align:right;">Outstanding</th>
                        <th style="text-align:right;">Pay Amount</th>
                        <th style="width:80px;"></th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;

        $body.find('.invoice-table-wrapper').html(table);
        bind_events();
    }

    // ── Bind events ───────────────────────────────────────────────
    function bind_events() {

        // Collapse/expand
        $body.find('.supplier-header-row').on('click', function(e) {
            if ($(e.target).is('input, button')) return;
            let supplier = $(this).data('supplier');
            let $rows = $body.find(`.invoice-row[data-supplier="${supplier}"]`);
            let $icon = $body.find(`.toggle-icon[data-supplier="${supplier}"]`);
            $rows.toggle();
            $icon.text($rows.is(':visible') ? '▾' : '▸');
        });

        // Select all
        $body.find('#select-all').on('change', function() {
            let checked = $(this).is(':checked');
            $body.find('.inv-checkbox').each(function() {
                $(this).prop('checked', checked);
                toggle_invoice($(this), checked);
            });
            $body.find('.supplier-select-all').prop('checked', checked);
            // Show/hide all inputs
            $body.find('.inv-checkbox').each(function() {
                let inv = decode_inv($(this));
                show_input(inv_key(inv), checked);
            });
            update_all_supplier_totals();
            update_footer();
        });

        // Supplier select all
        $body.find('.supplier-select-all').on('change', function() {
            let supplier = $(this).data('supplier');
            let checked  = $(this).is(':checked');
            $body.find(`.inv-checkbox[data-supplier="${supplier}"]`).each(function() {
                $(this).prop('checked', checked);
                toggle_invoice($(this), checked);
                let inv = decode_inv($(this));
                show_input(inv_key(inv), checked);
            });
            update_supplier_total(supplier);
            sync_select_all();
            update_footer();
        });

        // Individual checkbox
        $body.find('.inv-checkbox').on('change', function() {
            let supplier = $(this).data('supplier');
            let checked  = $(this).is(':checked');
            let inv      = decode_inv($(this));

            toggle_invoice($(this), checked);
            show_input(inv_key(inv), checked);

            if (!checked) {
                // Reset input to full outstanding when unchecked
                $body.find(`.pay-amount-input[data-invoice="${inv_key(inv)}"]`).val(inv.outstanding_amount);
            }

            sync_supplier_checkbox(supplier);
            update_supplier_total(supplier);
            sync_select_all();
            update_footer();
        });

        // Pay amount input
        $body.find('.pay-amount-input').on('input change', function() {
            let invoice_key  = $(this).data('invoice');
            let supplier     = $(this).data('supplier');
            let max          = parseFloat($(this).data('max'));
            let val          = parseFloat($(this).val()) || 0;

            if (val > max) { val = max; $(this).val(max); }
            if (val < 0)   { val = 0;   $(this).val(0); }

            if (selected_invoices[invoice_key]) {
                selected_invoices[invoice_key].pay_amount = val;
            }

            update_supplier_total(supplier);
            update_footer();
        });

        // Full button
        $body.find('.btn-pay-full').on('click', function(e) {
            e.stopPropagation();
            let supplier = $(this).data('supplier');
            $body.find(`.pay-amount-input[data-supplier="${supplier}"]`).each(function() {
                let max = parseFloat($(this).data('max'));
                $(this).val(max);
                let inv_key_val = $(this).data('invoice');
                if (selected_invoices[inv_key_val]) selected_invoices[inv_key_val].pay_amount = max;
            });
            update_supplier_total(supplier);
            update_footer();
        });

        // Clear button
        $body.find('.btn-pay-clear').on('click', function(e) {
            e.stopPropagation();
            let supplier = $(this).data('supplier');
            $body.find(`.pay-amount-input[data-supplier="${supplier}"]`).each(function() {
                $(this).val('');
                let inv_key_val = $(this).data('invoice');
                if (selected_invoices[inv_key_val]) selected_invoices[inv_key_val].pay_amount = 0;
            });
            update_supplier_total(supplier);
            update_footer();
        });
    }

    // ── Show/hide amount input vs display span ────────────────────
    function show_input(invoice_name, show) {
        let $input   = $body.find(`.pay-amount-input[data-invoice="${invoice_name}"]`);
        let $display = $body.find(`.pay-amount-display[data-invoice="${invoice_name}"]`);
        if (show) {
            $display.hide();
            $input.show();
        } else {
            $input.hide();
            $display.show();
        }
    }

    // ── Toggle invoice in selected set ────────────────────────────
    function toggle_invoice($cb, checked) {
        let inv = decode_inv($cb);
        let key = inv_key(inv);
        if (checked) {
            let current_pay = parseFloat(
                $body.find(`.pay-amount-input[data-invoice="${key}"]`).val()
            ) || parseFloat(inv.outstanding_amount);
            selected_invoices[key] = Object.assign({}, inv, { pay_amount: current_pay });
        } else {
            delete selected_invoices[key];
        }
    }

    // ── Checkbox sync helpers ─────────────────────────────────────
    function sync_supplier_checkbox(supplier) {
        let total   = $body.find(`.inv-checkbox[data-supplier="${supplier}"]`).length;
        let checked = $body.find(`.inv-checkbox[data-supplier="${supplier}"]:checked`).length;
        let $cb = $body.find(`.supplier-select-all[data-supplier="${supplier}"]`);
        $cb.prop('indeterminate', checked > 0 && checked < total);
        $cb.prop('checked', checked === total && total > 0);
    }

    function sync_select_all() {
        let total   = $body.find('.inv-checkbox').length;
        let checked = $body.find('.inv-checkbox:checked').length;
        $body.find('#select-all').prop('indeterminate', checked > 0 && checked < total);
        $body.find('#select-all').prop('checked', checked === total && total > 0);
    }

    // ── Supplier subtotal ─────────────────────────────────────────
    function update_supplier_total(supplier) {
        let subtotal = 0;
        $body.find(`.inv-checkbox[data-supplier="${supplier}"]:checked`).each(function() {
            let key = inv_key(decode_inv($(this)));
            subtotal += parseFloat(
                $body.find(`.pay-amount-input[data-invoice="${key}"]`).val()
            ) || 0;
        });
        let $span = $body.find(`.supplier-selected-total[data-supplier="${supplier}"]`);
        $span.html(subtotal > 0 ? `Paying: <strong>${fmt(subtotal)}</strong> &mdash; ` : '');
    }

    function update_all_supplier_totals() {
        all_supplier_data.forEach(s => update_supplier_total(s.supplier));
    }

    // ── Footer ────────────────────────────────────────────────────
    function update_footer() {
        let items = Object.values(selected_invoices);
        if (!items.length) {
            $body.find('.payment-run-footer').hide();
            return;
        }

        let zero_amounts = items.filter(i => !i.pay_amount || i.pay_amount <= 0);
        let total        = items.reduce((sum, i) => sum + (parseFloat(i.pay_amount) || 0), 0);
        let suppliers    = new Set(items.map(i => i.supplier)).size;

        let warning = zero_amounts.length
            ? `<div style="color:#c0392b; font-size:12px; margin-top:4px;">
                 ⚠ ${zero_amounts.length} invoice(s) have a $0 pay amount —
                 update or deselect before proceeding.
               </div>`
            : '';

        $body.find('.selected-summary').html(`
            ${suppliers} supplier(s) &mdash; ${items.length} invoice(s) selected &mdash;
            Total: <strong>${fmt(total)}</strong>
            ${warning}
        `);
        $body.find('.btn-create-run').prop('disabled', zero_amounts.length > 0);
        $body.find('.payment-run-footer').show();
    }

    // ── Create payment run ────────────────────────────────────────
    $body.find('.btn-create-run').on('click', function() {
        let bank_account = bank_account_field.get_value();
        let payment_date = payment_date_field.get_value();
        let company      = company_field.get_value();

        if (!bank_account) { frappe.msgprint('Please select a Bank Account.');  return; }
        if (!payment_date) { frappe.msgprint('Please select a Payment Date.');  return; }
        if (!company)      { frappe.msgprint('Please select a Company.');        return; }

        let items          = Object.values(selected_invoices);
        let supplier_count = new Set(items.map(i => i.supplier)).size;

        // Final sync of pay_amount from inputs
        items.forEach(inv => {
            let val = parseFloat(
                $body.find(`.pay-amount-input[data-invoice="${inv_key(inv)}"]`).val()
            );
            if (!isNaN(val)) inv.pay_amount = val;
        });

        let total = items.reduce((sum, i) => sum + parseFloat(i.pay_amount || 0), 0);

        frappe.confirm(
            `Create a Payment Run for <b>${supplier_count} supplier(s)</b>
             covering <b>${items.length} invoice(s)</b>
             totalling <b>${fmt(total)}</b>?`,
            function() {
                frappe.call({
                    method: 'eft_payments.eft_payments.page.payment_run_wizard.payment_run_wizard.create_payment_run',
                    args: {
                        company,
                        bank_account,
                        payment_date,
                        selected_invoices: JSON.stringify(items)
                    },
                    callback: function(r) {
                        if (r.message) {
                            frappe.show_alert({
                                message: `Payment Run ${r.message} created!`,
                                indicator: 'green'
                            });
                            frappe.set_route('Form', 'Payment Run', r.message);
                        }
                    }
                });
            }
        );
    });

    load_invoices();
};