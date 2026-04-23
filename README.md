# EFT Payments

A Frappe/ERPNext app for processing batch EFT/ACH payment runs for Canadian suppliers via RBC using the CPA 005 file format.

## Features
- Payment Run Wizard to select outstanding supplier invoices
- Partial payment support
- Automatic Payment Entry creation on submission
- CPA 005 ACH file generation for RBC
- Remittance advice emails with PDF attachment

## Requirements
- ERPNext v15
- Frappe v15

## Installation

```bash
bench get-app https://github.com/yourcompany/eft_payments
bench --site your-site install-app eft_payments
bench --site your-site migrate
```

## Setup
1. Go to **EFT Settings** and fill in your RBC Originator ID, short/long name and processing centre
2. Ensure suppliers have Bank Accounts linked with branch code and institution number
3. Open **Payment Run Wizard** from the search bar

## Usage
1. Open Payment Run Wizard, select invoices, set amounts
2. Create Payment Run → opens the draft
3. Click Submit → creates Payment Entries + generates ACH file
4. Upload ACH file to RBC
5. Click Actions → Send Remittance Emails