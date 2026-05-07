# -*- coding: utf-8 -*-
from odoo import fields, models

class MontaConfig(models.Model):
    _inherit = "monta.config"

    stock_sync_enabled = fields.Boolean(
        string="Enable Stock Sync",
        default=True,
        help="If disabled, the Monta → Odoo stock synchronization will skip all updates."
    )
