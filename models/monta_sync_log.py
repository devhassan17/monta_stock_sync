# -*- coding: utf-8 -*-
"""
monta_sync_log.py
-----------------
Persistent log model that records every Monta Stock Sync run.
Admins can review sync history from Monta → Stock Sync Logs.
Auto-cleanup: records older than 30 days are purged by the cron job.
"""
from odoo import api, fields, models


class MontaSyncLog(models.Model):
    _name = "monta.sync.log"
    _description = "Monta Stock Sync Log"
    _order = "sync_date desc"
    _rec_name = "sync_date"

    # ── Timestamps ────────────────────────────────────────────────────────────
    sync_date = fields.Datetime(
        string="Sync Date",
        default=fields.Datetime.now,
        readonly=True,
        required=True,
    )

    # ── Counters ──────────────────────────────────────────────────────────────
    products_fetched = fields.Integer(
        string="Products Fetched from Monta",
        readonly=True,
        help="Number of SKU/quantity pairs returned by the Monta API.",
    )
    products_synced = fields.Integer(
        string="Products Synced",
        readonly=True,
        help="Number of Odoo product variants whose stock was updated.",
    )
    products_skipped = fields.Integer(
        string="Products Skipped",
        readonly=True,
        help="Products skipped because they are subscription/service products or have no matching SKU.",
    )
    products_not_found = fields.Integer(
        string="SKUs Not Found in Odoo",
        readonly=True,
        help="Monta SKUs that could not be matched to any Odoo product variant.",
    )
    errors = fields.Integer(
        string="Errors",
        readonly=True,
        help="Number of products that could not be updated due to an error.",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    state = fields.Selection(
        selection=[
            ("success", "Success"),
            ("warning", "Warning"),
            ("error", "Error"),
        ],
        string="Status",
        default="success",
        readonly=True,
    )
    duration_seconds = fields.Float(
        string="Duration (s)",
        readonly=True,
        digits=(10, 2),
    )

    # ── Detail ────────────────────────────────────────────────────────────────
    notes = fields.Text(
        string="Notes / Error Details",
        readonly=True,
        help="Human-readable summary of what happened during this sync run.",
    )

    # ── Cleanup ───────────────────────────────────────────────────────────────
    @api.model
    def _purge_old_logs(self, days=30):
        """Remove sync log entries older than `days` days."""
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        old = self.sudo().search([("sync_date", "<", cutoff)])
        if old:
            old.unlink()
