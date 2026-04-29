# -*- coding: utf-8 -*-
{
    "name": "Monta Stock Sync",
    "version": "18.0.1.0.6",
    "summary": "Sync product stock quantities from Monta Portal NL to Odoo 18 every 24 hours.",
    "description": """
Monta Stock Sync
================
Fetches product stock levels from the Monta Portal NL API v6 every 24 hours
and updates the corresponding Odoo 18 product variants using safe inventory
adjustments (stock.quant / action_apply_inventory). Subscription products
are automatically excluded.

Features:
- 24-hour cron job with configurable schedule
- Product variant support via SKU / monta_sku field
- Subscription product exclusion (service + recurring)
- Full sync log (products synced, skipped, errors)
- Manual "Sync Now" button in the Monta menu
- Integrates with existing Monta Configuration (monta.config)
    """,
    "author": "Managemyweb.co",
    "website": "https://fairchain.org/monta-plugin-documentation/",
    "category": "Warehouse",
    "license": "LGPL-3",
    "images": ["static/description/banner.png"],
    "depends": [
        "stock",
        "product",
        "sale_management",
        "sale_subscription",
        "Monta-Odoo-Integration",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/monta_stock_sync_views.xml",
        "views/monta_stock_sync_menu.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    "support": "programmer.alihassan@gmail.com",
}
