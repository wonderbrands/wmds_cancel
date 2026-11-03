import logging
from collections import defaultdict

from odoo import _, fields, models
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)

# Campo de WMDs con las piezas ya recolectadas en una línea de PICK (módulo opcional).
PICKED_FIELD = 'wmds_picked_qty'


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    wb_dispatch_hold_count = fields.Integer(compute='_compute_wb_dispatch_hold_count')

    def _compute_wb_dispatch_hold_count(self):
        for so in self:
            so.wb_dispatch_hold_count = len(so._wb_held_pickings())

    def _action_cancel(self):
        # Solo SO confirmadas (con flujo de stock generado).
        to_relocate = self.filtered(lambda so: so.state == 'sale')
        # Las líneas del PICK (y su recolección) desaparecen al cancelar: hay que leerlas antes.
        in_progress = {}
        for so in to_relocate:
            try:
                in_progress[so.id] = so._wb_collect_picked_in_progress()
            except Exception:
                _logger.exception(
                    "wb_so_cancel_guarderia: fallo al leer recolección en curso de %s", so.name)
            # SO en despacho: su OUT se retiene (no se cancela) hasta que el despachador
            # indique qué se embarcó. Ver _wb_resolve_dispatch_hold().
            try:
                with self.env.cr.savepoint():
                    so._wb_hold_out_if_in_dispatch()
            except Exception:
                _logger.exception(
                    "wb_so_cancel_guarderia: fallo al retener el OUT de %s", so.name)
        # sale_stock cancela primero los pickings no 'done' (libera reservas) y
        # luego sale.order escribe state='cancel': al volver de super() el stock ya está libre.
        # El contexto evita que se cancelen los OUT retenidos.
        res = super(SaleOrder, self.with_context(wb_keep_dispatch_hold=True))._action_cancel()
        for so in to_relocate:
            so._wb_safe_relocate(in_progress.get(so.id))
        return res

    def _wb_safe_relocate(self, in_progress=None):
        """Reubica sin bloquear nunca: cualquier fallo queda en log y en el chatter."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self._wb_relocate_cancelled_stock(in_progress)
        except Exception:
            _logger.exception(
                "wb_so_cancel_guarderia: fallo al reubicar stock de %s", self.name)
            self._wb_post_safe(_(
                "ATENCIÓN: no se pudo generar el traslado de cancelación. "
                "Revise el log del servidor y reubique el stock manualmente."))

    def _wb_post_safe(self, body):
        try:
            with self.env.cr.savepoint():
                self.sudo().message_post(body=body)
        except Exception:
            # Ni siquiera el aviso debe bloquear la cancelación.
            _logger.exception("wb_so_cancel_guarderia: no se pudo dejar el aviso en %s", self.name)

    # ------------------------------------------------------------------
    # Despacho en curso: retención del OUT
    # ------------------------------------------------------------------
    def _wb_is_in_dispatch(self):
        """Gancho: ¿la SO está en proceso de despacho (paquetes posiblemente ya en el
        contenedor del carrier)? Este módulo no lo sabe; lo responde WMDs (u otro
        módulo) sobrescribiendo este método. Se usa getattr(super()) para que el orden
        de carga de los módulos no importe."""
        parent = getattr(super(), '_wb_is_in_dispatch', None)
        return parent() if parent else False

    def _wb_held_pickings(self):
        return self.sudo().picking_ids.filtered(
            lambda p: p.wb_dispatch_hold and p.state not in ('done', 'cancel'))

    def _wb_hold_out_if_in_dispatch(self):
        self.ensure_one()
        so = self.sudo()
        wh = so.warehouse_id
        if not wh or not so._wb_is_in_dispatch():
            return
        outs = so.picking_ids.filtered(
            lambda p: p.picking_type_id == wh.out_type_id and p.state not in ('done', 'cancel'))
        if not outs:
            return
        outs.write({'wb_dispatch_hold': True})
        self._wb_post_safe(_(
            "Cancelación durante el despacho: el OUT %s queda RETENIDO hasta que el "
            "despachador indique qué paquetes ya se embarcaron.", ", ".join(outs.mapped('name'))))

    def _wb_resolve_dispatch_hold(self, shipped=None):
        """Cierra la retención del OUT de una SO cancelada en despacho.

        :param shipped: {product_id: cantidad embarcada (UdM del producto)}. Vacío o
            None = no se embarcó nada.
        Valida el OUT solo por lo embarcado (sale al cliente como una venta normal),
        cancela el resto sin backorder y reubica a Guardería lo que quedó en Output.
        :return: True si había OUT retenido.
        """
        self.ensure_one()
        so = self.sudo()
        held = so._wb_held_pickings()
        if not held:
            return False
        remaining = defaultdict(float)
        for product_id, qty in (shipped or {}).items():
            remaining[int(product_id)] += qty
        delivered = []
        for picking in held:
            picking.wb_dispatch_hold = False
            has_qty = False
            for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
                product = move.product_id
                take = min(remaining[product.id], move.product_uom._compute_quantity(
                    move.product_uom_qty, product.uom_id, round=False))
                if float_compare(take, 0.0, precision_rounding=product.uom_id.rounding) > 0:
                    remaining[product.id] -= take
                    move.quantity = product.uom_id._compute_quantity(take, move.product_uom)
                    move.picked = True
                    has_qty = True
                    delivered.append("%s x %s" % (product.display_name, take))
                else:
                    move.quantity = 0
                    move.picked = False
            if has_qty:
                picking.with_context(
                    skip_backorder=True, picking_ids_not_to_backorder=picking.ids,
                    skip_sms=True, skip_immediate=True, wb_keep_dispatch_hold=False,
                ).button_validate()
            else:
                picking.action_cancel()
        extra = ["%s x %s" % (self.env['product.product'].browse(pid).display_name, qty)
                 for pid, qty in remaining.items() if qty > 0]
        if delivered:
            msg = _("Despacho tras cancelación: se entregó al carrier %s.", "; ".join(delivered))
        else:
            msg = _("Despacho tras cancelación: no se embarcó nada; el OUT se canceló.")
        if extra:
            msg += " " + _("ATENCIÓN: se reportó como embarcado más de lo que tenía el OUT: %s.",
                           "; ".join(extra))
        self._wb_post_safe(msg)
        # Lo que se quedó en Output (no embarcado) va a Guardería.
        so._wb_safe_relocate()
        return True

    def action_wb_open_dispatch_hold_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Resolver despacho retenido"),
            'res_model': 'wb.dispatch.hold.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_sale_order_id': self.id},
        }

    def _wb_collect_picked_in_progress(self):
        """Unidades ya recolectadas (según WMDs) en PICK sin validar.
        Devuelve {(location_id, product_id): qty en UdM del producto}."""
        self.ensure_one()
        so = self.sudo()
        wh = so.warehouse_id
        result = defaultdict(float)
        if not wh.wb_cancel_include_picked or PICKED_FIELD not in self.env['stock.move.line']._fields:
            return result
        picks = so.picking_ids.filtered(
            lambda p: p.picking_type_id == wh.pick_type_id and p.state not in ('done', 'cancel'))
        for line in picks.move_line_ids:
            picked = line[PICKED_FIELD]
            if picked <= 0 or line.location_id.usage != 'internal':
                continue
            # TODO lotes: incluir line.lot_id en la clave.
            result[(line.location_id.id, line.product_id.id)] += line.product_uom_id._compute_quantity(
                picked, line.product_id.uom_id, round=False)
        return result

    def _wb_auto_validate_picking(self, picking):
        """Valida el CAN; si falla, queda en 'Disponible' para validarlo a mano."""
        try:
            with self.env.cr.savepoint():
                picking.with_context(
                    skip_backorder=True, skip_sms=True, skip_immediate=True,
                ).button_validate()
        except Exception:
            _logger.exception(
                "wb_so_cancel_guarderia: no se pudo validar automáticamente %s", picking.name)

    def _wb_relocate_cancelled_stock(self, in_progress=None):
        self.ensure_one()
        so = self.sudo()
        wh = so.warehouse_id
        # Solo almacenes con entrega en 3 pasos: en otros no hay zonas intermedias.
        if not wh or wh.delivery_steps != 'pick_pack_ship':
            return
        can_type = wh.wb_cancel_picking_type_id
        guard = wh.wb_cancel_location_id
        if not (can_type and guard):
            self.message_post(body=_(
                "ATENCIÓN: el almacén %s no tiene configurados el tipo de operación "
                "de cancelación ni la ubicación Guardería. No se generó traslado.",
                wh.name))
            return

        pack_loc = wh.wh_pack_stock_loc_id
        out_loc = wh.wh_output_stock_loc_id
        watch = pack_loc | out_loc  # ubicaciones que se vigilan/reportan
        scope = (out_loc | pack_loc) if wh.wb_cancel_include_pack_zone else out_loc
        if so._wb_held_pickings():
            # El stock de Output pertenece al OUT retenido: se decide al cerrar el despacho.
            watch -= out_loc
            scope -= out_loc
        origin = "Cancelacion %s" % so.name  # no traducir: se usa para idempotencia

        # 1) Saldo neto por (ubicación, producto) dejado por el flujo de ESTA SO.
        # TODO lotes: cuando haya tracking, agregar lot_id a la clave del saldo.
        flow_types = wh.pick_type_id | wh.pack_type_id | wh.out_type_id
        done_lines = so.picking_ids.filtered(
            lambda p: p.picking_type_id in flow_types
        ).move_ids.filtered(lambda m: m.state == 'done').move_line_ids
        net = defaultdict(float)
        for line in done_lines:
            qty = line.product_uom_id._compute_quantity(
                line.quantity, line.product_id.uom_id, round=False)
            if line.location_dest_id in watch:
                net[(line.location_dest_id.id, line.product_id.id)] += qty
            if line.location_id in watch:
                net[(line.location_id.id, line.product_id.id)] -= qty

        # 2) Descontar CAN previos de esta SO (reapertura + nueva cancelación)
        prior_moves = self.env['stock.move'].sudo().search([
            ('picking_id.picking_type_id', '=', can_type.id),
            ('picking_id.origin', '=', origin),
            ('state', '!=', 'cancel'),
        ])
        for move in prior_moves:
            net[(move.location_id.id, move.product_id.id)] -= \
                move.product_uom._compute_quantity(
                    move.product_uom_qty, move.product_id.uom_id, round=False)

        # 3) Separar lo que se mueve (en alcance) de lo que solo se reporta
        to_move = defaultdict(list)  # location_id -> [(product, qty)]
        out_of_scope = []
        for (loc_id, prod_id), qty in net.items():
            product = self.env['product.product'].sudo().browse(prod_id)
            if not product.is_storable:
                continue
            if float_compare(qty, 0.0, precision_rounding=product.uom_id.rounding) <= 0:
                continue
            if loc_id in scope.ids:
                to_move[loc_id].append((product, qty))
            else:
                out_of_scope.append("%s x %s en %s" % (
                    product.display_name, qty,
                    self.env['stock.location'].sudo().browse(loc_id).complete_name))

        # 4) Un CAN por ubicación origen: crear → confirmar → reservar (misma transacción)
        created = self.env['stock.picking'].sudo()
        for loc_id, items in to_move.items():
            # No se asigna group_id de la SO: el CAN no debe contar como entrega de la SO.
            # TODO lotes: crear move_line_ids con lot_id en vez de depender de la reserva.
            picking = self.env['stock.picking'].sudo().create({
                'picking_type_id': can_type.id,
                'location_id': loc_id,
                'location_dest_id': guard.id,
                'origin': origin,
                'company_id': wh.company_id.id,
                'move_ids': [(0, 0, {
                    'name': origin,
                    'product_id': product.id,
                    'product_uom': product.uom_id.id,
                    'product_uom_qty': qty,
                    'location_id': loc_id,
                    'location_dest_id': guard.id,
                    'company_id': wh.company_id.id,
                }) for product, qty in items],
            })
            picking.action_confirm()
            picking.action_assign()
            if wh.wb_cancel_auto_validate and picking.state == 'assigned':
                self._wb_auto_validate_picking(picking)
            created |= picking

        # 4b) Recolección en curso (PICK sin validar): un solo CAN con una línea por rack de origen.
        # Se genera solo si hay saldo; las unidades salieron físicamente del rack pero el
        # sistema aún las tiene allí.
        picked_items = []
        for (loc_id, prod_id), qty in (in_progress or {}).items():
            product = self.env['product.product'].sudo().browse(prod_id)
            if product.is_storable and float_compare(
                    qty, 0.0, precision_rounding=product.uom_id.rounding) > 0:
                picked_items.append((loc_id, product, qty))
        if picked_items:
            picking = self.env['stock.picking'].sudo().create({
                'picking_type_id': can_type.id,
                'location_id': wh.lot_stock_id.id,
                'location_dest_id': guard.id,
                'origin': origin,
                'company_id': wh.company_id.id,
                'move_ids': [(0, 0, {
                    'name': origin,
                    'product_id': product.id,
                    'product_uom': product.uom_id.id,
                    'product_uom_qty': qty,
                    'location_id': loc_id,
                    'location_dest_id': guard.id,
                    'company_id': wh.company_id.id,
                }) for loc_id, product, qty in picked_items],
            })
            picking.action_confirm()
            picking.action_assign()
            if wh.wb_cancel_auto_validate and picking.state == 'assigned':
                self._wb_auto_validate_picking(picking)
            created |= picking

        # 5) Rastro en el chatter de la SO
        notes = []
        for picking in created:
            if picking.location_id == wh.lot_stock_id:
                src = "recolección en curso, desde: " + ", ".join(
                    sorted(set(picking.move_ids.location_id.mapped('complete_name'))))
            else:
                src = picking.location_id.complete_name
            txt = "%s (%s → %s)" % (picking.name, src, guard.complete_name)
            if picking.state == 'done':
                txt += " — validado automáticamente"
            elif picking.state != 'assigned':
                txt += " — RESERVA INCOMPLETA (estado: %s), revisar stock en origen" % picking.state
            notes.append(txt)
        if notes:
            self.message_post(body=_(
                "Traslado(s) de cancelación generado(s): %s", "; ".join(notes)))
        if out_of_scope:
            self.message_post(body=_(
                "ATENCIÓN: stock de esta SO sin reubicar (fuera de alcance de la automatización): %s",
                "; ".join(out_of_scope)))
