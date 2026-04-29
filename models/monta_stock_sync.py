# -*- coding: utf-8 -*-
"""
monta_stock_sync.py
-------------------
Core model for the Monta → Odoo stock synchronisation.

Design principles
~~~~~~~~~~~~~~~~~
1. **Safe stock update**: Uses stock.quant + action_apply_inventory()
   with the ``inventory_mode=True`` context — identical to what a user
   does via Inventory → Inventory Adjustments. Generates proper stock
   moves and accounting entries. No raw SQL, no direct quant hacks.

2. **No Odoo defaults broken**: Only touches stock.quant. Does not
   write to stock.picking, sale.order, account.move or any workflow
   objects.

3. **Subscription product exclusion**: Skips products whose template
   has ``recurring_invoice=True`` (Odoo Subscriptions app) or whose
   type is 'service' and the product is explicitly tagged as a
   subscription product. Admin can also tag any product with the
   ``monta_sync_exclude`` tag to force exclusion.

4. **SKU resolution**: Reuses the same priority chain as the main
   Monta connector (monta_sku → default_code → supplier code →
   barcode → template.default_code). The reverse lookup uses the
   same priority to find the Odoo product for a given Monta SKU.

5. **Pagination**: Fetches all Monta stock pages until the API
   returns fewer rows than the page size.

6. **Idempotency**: Running the same cron twice has no side-effects
   beyond generating a second identical inventory adjustment move
   (net delta = 0 if stock hasn't changed).
"""
import logging
import time
from datetime import timedelta

import requests
from requests.auth import HTTPBasicAuth

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_MONTA_STOCK_PATH = "products"
_PAGE_SIZE = 250          # rows per page from Monta
_DEFAULT_TIMEOUT = 30     # seconds per HTTP request


class MontaStockSync(models.Model):
    _name = "monta.stock.sync"
    _description = "Monta Stock Sync"

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers: configuration
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _get_config(self):
        """Return the active monta.config singleton or raise."""
        cfg = self.env["monta.config"].sudo().get_singleton()
        if not cfg:
            raise ValueError("Monta configuration not found. Please configure the Monta plugin first.")
        if not cfg.username or not cfg.password:
            raise ValueError("Monta API credentials are not set. Check Monta → Configuration.")
        return cfg

    @api.model
    def _get_sync_location(self):
        """
        Return the stock location to adjust.
        Uses the main company's stock location (WH/Stock).
        Falls back to the first internal location found.
        """
        # Try to get the default warehouse's stock location for the current company
        warehouse = self.env["stock.warehouse"].sudo().search(
            [("company_id", "=", self.env.company.id)], limit=1
        )
        if warehouse and warehouse.lot_stock_id:
            return warehouse.lot_stock_id

        # Generic fallback: first internal location
        location = self.env["stock.location"].sudo().search(
            [("usage", "=", "internal"), ("company_id", "=", self.env.company.id)],
            limit=1,
        )
        if location:
            return location

        raise ValueError(
            "Could not determine stock location. "
            "Please ensure at least one internal stock location exists."
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers: subscription / exclusion detection
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _get_excluded_product_ids(self):
        """
        Return a set of product.product IDs that should NOT be synced.

        Exclusion criteria (any one is sufficient):
        - Template has ``recurring_invoice = True``  (Odoo Subscriptions)
        - Template ``type == 'service'``
        - Product has the tag ``monta_sync_exclude`` (case-insensitive)
        """
        excluded_ids = set()

        # 1. Subscription products (recurring_invoice field added by sale_subscription)
        try:
            subscription_products = self.env["product.product"].sudo().search([
                ("product_tmpl_id.recurring_invoice", "=", True),
            ])
            excluded_ids.update(subscription_products.ids)
        except Exception:
            # Field may not exist if sale_subscription is not installed
            pass

        # 2. Pure service products (type == 'service')
        service_products = self.env["product.product"].sudo().search([
            ("type", "=", "service"),
        ])
        excluded_ids.update(service_products.ids)

        # 3. Products tagged with monta_sync_exclude
        exclude_tag = self.env["product.tag"].sudo().search(
            [("name", "ilike", "monta_sync_exclude")], limit=1
        )
        if exclude_tag:
            tagged_products = self.env["product.product"].sudo().search([
                ("product_tag_ids", "in", [exclude_tag.id]),
            ])
            excluded_ids.update(tagged_products.ids)

        _logger.info("[MontaStockSync] Excluded product IDs count: %d", len(excluded_ids))
        return excluded_ids

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers: SKU → product lookup (reverse of resolve_sku)
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _build_sku_to_product_map(self, excluded_ids):
        """
        Build a dict mapping SKU string → product.product recordset.

        Priority (mirrors resolve_sku in utils/sku.py):
          1. product.monta_sku
          2. product.default_code
          3. first seller_ids.product_code
          4. product.barcode
          5. product_tmpl_id.default_code

        Only storable products (type == 'consu' or 'product') are included.
        Subscription / service products (excluded_ids) are skipped.
        """
        sku_map = {}  # sku_string → product.product record

        storable_products = self.env["product.product"].sudo().search([
            ("type", "in", ["consu", "product"]),
            ("id", "not in", list(excluded_ids)),
        ])

        _logger.info(
            "[MontaStockSync] Building SKU map for %d storable products.",
            len(storable_products),
        )

        for product in storable_products:
            sku = self._resolve_product_sku(product)
            if not sku:
                continue
            # First product wins (no duplicates — same logic as the main connector)
            if sku not in sku_map:
                sku_map[sku] = product

        _logger.info("[MontaStockSync] SKU map built with %d entries.", len(sku_map))
        return sku_map

    @staticmethod
    def _resolve_product_sku(product):
        """
        Resolve the effective Monta SKU for a product.product record.
        Returns the SKU string or empty string if not resolvable.
        """
        # 1. Explicit monta_sku
        sku = getattr(product, "monta_sku", False)
        if sku and str(sku).strip():
            return str(sku).strip()

        # 2. Internal reference (default_code)
        code = product.default_code
        if code and str(code).strip():
            return str(code).strip()

        # 3. First supplier product code
        seller = product.seller_ids[:1]
        if seller and getattr(seller, "product_code", False):
            sc = (seller.product_code or "").strip()
            if sc:
                return sc

        # 4. Barcode
        barcode = product.barcode
        if barcode and str(barcode).strip():
            return str(barcode).strip()

        # 5. Template default_code
        tmpl_code = product.product_tmpl_id.default_code
        if tmpl_code and str(tmpl_code).strip():
            return str(tmpl_code).strip()

        return ""

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers: Monta API fetch
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _fetch_monta_stock(self, cfg):
        """
        Fetch all product stock from Monta API v6.

        Returns a dict: {sku_string: quantity_float}
        Handles pagination automatically using page / pageSize parameters.

        Monta API endpoint: GET /product/stock
        Response schema (list):
          [
            {
              "sku": "PROD-001",
              "stock": 42,
              ...
            },
            ...
          ]
        """
        base_url = (cfg.base_url or "https://api-v6.monta.nl").rstrip("/")
        username = (cfg.username or "").strip()
        password = (cfg.password or "").strip()
        timeout = int(cfg.timeout or _DEFAULT_TIMEOUT)

        auth = HTTPBasicAuth(username, password)
        headers = {
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        stock_data = {}
        page = 0
        total_fetched = 0

        _logger.info("[MontaStockSync] Starting Monta API fetch from %s", base_url)

        while True:
            url = f"{base_url}/{_MONTA_STOCK_PATH}"
            params = {"page": page}

            try:
                resp = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    auth=auth,
                    timeout=timeout,
                )
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                err_body = e.response.text if (e.response is not None) else ""
                _logger.error(
                    "[MontaStockSync] HTTP error fetching stock page %d: %s | Body: %s",
                    page, e, err_body
                )
                raise Exception(f"Monta API HTTP Error: {e} | Response Body: {err_body}")
            except requests.exceptions.RequestException as e:
                _logger.error(
                    "[MontaStockSync] Network error fetching stock page %d: %s",
                    page, e
                )
                raise

            try:
                body = resp.json()
            except ValueError:
                _logger.error(
                    "[MontaStockSync] Invalid JSON response on page %d: %s",
                    page, resp.text[:500]
                )
                break

            # Handle both list and dict-wrapped responses
            if isinstance(body, dict):
                # /products returns {"Products": [{"Product": {...}}]}
                rows = body.get("Products") or body.get("content") or body.get("items") or body.get("data") or []
            elif isinstance(body, list):
                rows = body
            else:
                _logger.warning(
                    "[MontaStockSync] Unexpected response type on page %d: %s",
                    page, type(body)
                )
                break

            if not rows:
                _logger.info(
                    "[MontaStockSync] No more rows returned on page %d. Stopping.",
                    page
                )
                break

            for row in rows:
                if not isinstance(row, dict):
                    continue

                # /products wraps the payload in {"Product": {...}}
                p_data = row.get("Product") or row
                if not isinstance(p_data, dict):
                    continue

                sku = str(
                    p_data.get("Sku")
                    or p_data.get("sku")
                    or p_data.get("SKU")
                    or ""
                ).strip()
                if not sku:
                    continue

                stock_obj = p_data.get("Stock") or p_data.get("stock")
                qty = 0.0

                if isinstance(stock_obj, dict):
                    # Prefer StockAll (physical inventory), fallback to StockAvailable
                    qty = stock_obj.get("StockAll") or stock_obj.get("StockAvailable") or 0.0
                elif isinstance(stock_obj, (int, float, str)):
                    qty = stock_obj

                try:
                    qty = float(qty)
                except (TypeError, ValueError):
                    qty = 0.0

                stock_data[sku] = qty

            total_fetched += len(rows)
            _logger.info(
                "[MontaStockSync] Page %d: got %d rows (total so far: %d)",
                page, len(rows), total_fetched
            )

            # If we get an empty array, the while loop will break at the start of the next iteration
            page += 1

        _logger.info(
            "[MontaStockSync] Fetch complete: %d unique SKUs from Monta.",
            len(stock_data)
        )
        return stock_data

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers: Safe stock update
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _update_product_stock(self, product, location, new_qty):
        """
        Safely update the on-hand quantity of a single product.product at the
        given location using the Odoo inventory adjustment mechanism.

        This method:
        1. Finds or creates a stock.quant for (product, location).
        2. Sets inventory_quantity = new_qty.
        3. Calls action_apply_inventory() to commit the adjustment.

        This is identical to doing an Inventory Adjustment via the Odoo UI.
        Generates a stock.move for full traceability.
        """
        StockQuant = self.env["stock.quant"].sudo().with_context(inventory_mode=True)

        quant = StockQuant.search([
            ("product_id", "=", product.id),
            ("location_id", "=", location.id),
            ("lot_id", "=", False),       # non-lotted; extend if needed
            ("package_id", "=", False),
            ("owner_id", "=", False),
        ], limit=1)

        if quant:
            quant.write({"inventory_quantity": new_qty})
        else:
            quant = StockQuant.create({
                "product_id": product.id,
                "location_id": location.id,
                "inventory_quantity": new_qty,
            })

        # Commit the adjustment — this creates the stock.move
        quant.action_apply_inventory()

    # ──────────────────────────────────────────────────────────────────────────
    # Public: action_sync_now  (manual button)
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def action_sync_now(self):
        """
        Trigger a full Monta → Odoo stock sync immediately.
        Can be called from a button in the UI or from the cron job.
        Returns a dict summary: {synced, skipped, not_found, errors, state}.
        """
        t_start = time.time()
        counters = {
            "fetched": 0,
            "synced": 0,
            "skipped": 0,
            "not_found": 0,
            "errors": 0,
        }
        notes_lines = []
        final_state = "success"

        try:
            cfg = self._get_config()
            location = self._get_sync_location()

            _logger.info(
                "[MontaStockSync] Starting sync | location: %s | company: %s",
                location.display_name, self.env.company.name,
            )

            # 1. Fetch stock from Monta
            monta_stock = self._fetch_monta_stock(cfg)
            counters["fetched"] = len(monta_stock)

            if not monta_stock:
                notes_lines.append("Monta API returned no stock data.")
                final_state = "warning"
                self._write_log(counters, final_state, "\n".join(notes_lines), time.time() - t_start)
                return counters

            # 2. Build SKU → product map (excludes subscription/service products)
            excluded_ids = self._get_excluded_product_ids()
            sku_map = self._build_sku_to_product_map(excluded_ids)

            # 3. For each Monta SKU, update Odoo stock
            for sku, qty in monta_stock.items():
                product = sku_map.get(sku)

                if not product:
                    counters["not_found"] += 1
                    _logger.debug(
                        "[MontaStockSync] SKU '%s' not found in Odoo — skipping.", sku
                    )
                    continue

                # Skip if product is assigned to a different company than the Monta location
                if product.company_id and location.company_id and product.company_id.id != location.company_id.id:
                    counters["skipped"] += 1
                    _logger.warning(
                        "[MontaStockSync] Skipped [%s] %s (Company mismatch: Product=%s, Location=%s)",
                        sku, product.display_name, product.company_id.name, location.company_id.name
                    )
                    continue

                try:
                    self._update_product_stock(product, location, qty)
                    counters["synced"] += 1
                    _logger.info(
                        "[MontaStockSync] Updated [%s] %s → qty %.2f",
                        sku, product.display_name, qty,
                    )
                except Exception as exc:
                    counters["errors"] += 1
                    msg = f"Error updating [{sku}] {product.display_name}: {exc}"
                    notes_lines.append(msg)
                    _logger.error("[MontaStockSync] %s", msg, exc_info=True)

        except Exception as fatal:
            final_state = "error"
            msg = f"Fatal sync error: {fatal}"
            notes_lines.append(msg)
            _logger.error("[MontaStockSync] %s", msg, exc_info=True)
            counters["errors"] += 1

        # Determine overall state
        if final_state != "error":
            if counters["errors"] > 0:
                final_state = "warning"
            elif counters["synced"] == 0 and counters["fetched"] > 0:
                final_state = "warning"
            else:
                final_state = "success"

        duration = time.time() - t_start
        notes = "\n".join(notes_lines) if notes_lines else None

        _logger.info(
            "[MontaStockSync] Sync complete in %.2fs | "
            "fetched=%d synced=%d skipped=%d not_found=%d errors=%d state=%s",
            duration,
            counters["fetched"],
            counters["synced"],
            counters["skipped"],
            counters["not_found"],
            counters["errors"],
            final_state,
        )

        self._write_log(counters, final_state, notes, duration)
        return counters

    # ──────────────────────────────────────────────────────────────────────────
    # Internal: write sync log
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _write_log(self, counters, state, notes, duration):
        self.env["monta.sync.log"].sudo().create({
            "sync_date": fields.Datetime.now(),
            "products_fetched": counters.get("fetched", 0),
            "products_synced": counters.get("synced", 0),
            "products_skipped": counters.get("skipped", 0),
            "products_not_found": counters.get("not_found", 0),
            "errors": counters.get("errors", 0),
            "state": state,
            "duration_seconds": round(duration, 2),
            "notes": notes,
        })

    # ──────────────────────────────────────────────────────────────────────────
    # Public: cron entry point
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def _cron_sync_monta_stock(self):
        """
        Entry point called by the scheduled action every 24 hours.
        Wraps action_sync_now() with a top-level guard so the cron
        job itself is never deactivated by an unhandled exception.
        Also purges sync logs older than 30 days.
        """
        _logger.info("[MontaStockSync] Cron job triggered.")
        try:
            self.action_sync_now()
        except Exception:
            _logger.exception("[MontaStockSync] Unhandled exception in cron — cron job kept active.")

        # Cleanup old logs
        try:
            self.env["monta.sync.log"].sudo()._purge_old_logs(days=30)
        except Exception:
            _logger.warning("[MontaStockSync] Failed to purge old sync logs.", exc_info=True)

        _logger.info("[MontaStockSync] Cron job complete.")

    # ──────────────────────────────────────────────────────────────────────────
    # UI helper: open log list view
    # ──────────────────────────────────────────────────────────────────────────

    @api.model
    def action_open_logs(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Monta Stock Sync Logs"),
            "res_model": "monta.sync.log",
            "view_mode": "list,form",
            "target": "current",
        }
