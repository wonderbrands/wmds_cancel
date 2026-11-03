# -*- coding: utf-8 -*-
"""Cancelación de una SO mientras sus paquetes (EI) están en despacho de WMDs.

- El despachador decide por EI si el paquete se embarcó o se retiró.
- Al cerrar el despacho se llama `sale.order._wb_resolve_dispatch_hold(shipped)`.

Se engancha al núcleo de WMDs solo a través de sus puntos de extensión
(`wmds/models/wmds_extension_points.py`).
"""
import logging
from collections import defaultdict

from odoo import _, fields, models

_logger = logging.getLogger(__name__)


class SaleOrderEICancelHold(models.Model):
    _inherit = "sale.order.ei"

    wmds_cancel_decision = fields.Selection([
        ('shipped', 'Embarcada'),
        ('removed', 'Retirada'),
    ], string="Decisión por cancelación", copy=False,
        help="Decisión del despachador sobre esta EI cuando la orden se canceló en pleno despacho.")
    wmds_cancel_product_id = fields.Many2one(
        "product.product", string="Producto embarcado", copy=False,
        help="Producto de esta EI cuando la orden tiene varios productos (1 EI = 1 pieza).")

    def _wmds_process_cancel_holds(self, operator=None):
        """Resuelve las SO canceladas con OUT retenido incluidas en este despacho.

        :return: (tags resueltas, mensajes de error). Las EI de una SO retenida en la
            que falta una decisión NO se resuelven y se reportan como error.
        """
        handled = self.browse()
        errors = []
        for so in self.mapped('so_id'):
            if not so._wmds_is_cancel_held():
                continue
            so_tags = self.filtered(lambda t: t.so_id == so)
            undecided = so_tags.filtered(lambda t: t.wmds_cancel_decision != 'shipped')
            if undecided:
                errors.append(_(
                    "%(so)s: indica si las guías %(eis)s se embarcaron o se retiraron.",
                    so=so.name, eis=", ".join(undecided.mapped('display_name_custom'))))
                continue
            try:
                with self.env.cr.savepoint():
                    resolved = so._wmds_resolve_cancel_hold(operator=operator)
                if resolved:
                    handled |= so_tags
                else:
                    errors.append(_(
                        "%(so)s: no se pudo determinar qué producto llevaban las guías ya entregadas; "
                        "resuélvelo con el botón «Resolver despacho retenido» de la orden.", so=so.name))
            except Exception as e:
                _logger.exception("WMDs: fallo al resolver despacho retenido de %s", so.name)
                errors.append(_("%(so)s: no se pudo resolver el despacho retenido (%(err)s).",
                                so=so.name, err=e))
        return handled, errors

    # ------------------------------------------------ puntos de extensión del núcleo
    def _wmds_dispatch_prefilter(self, operator=None):
        tags, error = super()._wmds_dispatch_prefilter(operator=operator)
        if error:
            return tags, error
        # EI de órdenes canceladas ya resueltas en un intento anterior: nada que hacer.
        tags -= tags.filtered(lambda t: t.dispatched and t.so_id.state == 'cancel')
        held = tags.filtered(lambda t: t.so_id and t.so_id._wmds_is_cancel_held())
        undecided = held.filtered(lambda t: t.wmds_cancel_decision != 'shipped')
        if undecided:
            return tags, ("Indica si estas guías de pedidos cancelados se embarcaron o se retiraron: "
                          + ", ".join(undecided.mapped('display_name_custom')))
        return tags, False

    def _wmds_dispatch_handle(self, operator=None):
        """Órdenes canceladas en pleno despacho (OUT retenido): se valida el OUT solo por
        las EI embarcadas y lo demás va a Guardería."""
        res = super()._wmds_dispatch_handle(operator=operator)
        held = (self - res['consumed']).filtered(lambda t: t.so_id and t.so_id._wmds_is_cancel_held())
        if not held:
            return res
        handled, errors = held._wmds_process_cancel_holds(operator=operator)
        return dict(
            res,
            consumed=res['consumed'] | held,
            handled=res['handled'] | handled,
            errors=res['errors'] + errors,
            log=res['log'] + [f"Despacho de orden cancelada resuelto: {n}"
                              for n in handled.mapped('display_name_custom')],
        )

    def _wmds_scan_extra_values(self):
        res = super()._wmds_scan_extra_values()
        res.update({
            "cancel_hold": self.so_id._wmds_is_cancel_held() if self.so_id else False,
            "cancel_decision": self.wmds_cancel_decision or False,
        })
        return res


class SaleOrderCancelHold(models.Model):
    _inherit = "sale.order"

    def _wmds_dispatch_allows_cancelled(self):
        return super()._wmds_dispatch_allows_cancelled() or self._wmds_is_cancel_held()

    def _wmds_is_cancel_held(self):
        self.ensure_one()
        return self.state == 'cancel' and bool(self._wb_held_pickings())

    def _wmds_cancel_hold_products(self):
        """Productos del OUT retenido con las piezas aún sin asignar a una EI embarcada.
        :return: [{'id', 'name', 'remaining'}]"""
        self.ensure_one()
        pending = defaultdict(float)
        names = {}
        for move in self._wb_held_pickings().move_ids.filtered(
                lambda m: m.state not in ('done', 'cancel')):
            product = move.product_id
            names[product.id] = product.display_name
            pending[product.id] += move.product_uom._compute_quantity(
                move.product_uom_qty, product.uom_id, round=False)
        for tag in self.sudo().ei:
            if tag.wmds_cancel_decision == 'shipped' and tag.wmds_cancel_product_id:
                pending[tag.wmds_cancel_product_id.id] -= 1
        return [{'id': pid, 'name': names[pid], 'remaining': qty}
                for pid, qty in pending.items() if pid in names]

    def _wmds_cancel_shipped_quantities(self):
        """{product_id: piezas} embarcadas según las EI (1 EI = 1 pieza): las marcadas
        'Embarcada' + las ya entregadas a la paquetería antes de la cancelación.
        :return: dict, o None si alguna EI no tiene producto y la SO tiene varios."""
        self.ensure_one()
        products = self._wb_held_pickings().move_ids.mapped('product_id')
        shipped = defaultdict(float)
        for tag in self.sudo().ei:
            if not (tag.dispatched or tag.wmds_cancel_decision == 'shipped'):
                continue
            product = tag.wmds_cancel_product_id or (products if len(products) == 1 else False)
            if not product:
                return None
            shipped[product.id] += 1
        return dict(shipped)

    def _wmds_resolve_cancel_hold(self, operator=None):
        """Cierra la retención con lo que dicen las EI. Si no se puede saber qué producto
        se embarcó (EI despachadas antes, SO de varios productos), deja la retención
        para resolverla con el botón de la SO."""
        self.ensure_one()
        if not self._wmds_is_cancel_held():
            return False
        shipped = self._wmds_cancel_shipped_quantities()
        user = operator or self.env.user
        if shipped is None:
            self.env['wmds.log'].sudo().create({
                'sale': self.id, 'user': user.id,
                'log': "Despacho tras cancelación: no se pudo determinar el producto de las guías "
                       "ya entregadas. Resolver con el botón «Resolver despacho retenido» de la orden.",
            })
            return False
        shipped_tags = self.sudo().ei.filtered(
            lambda t: t.wmds_cancel_decision == 'shipped' and not t.dispatched)
        shipped_tags.write({'dispatched': True, 'on_dock': False, 'dock_id': False})
        self._wb_resolve_dispatch_hold(shipped)
        self.env['wmds.log'].sudo().create({
            'sale': self.id, 'user': user.id,
            'log': "Despacho tras cancelación resuelto. Embarcadas: %s." % (
                ", ".join(shipped_tags.mapped('display_name_custom')) or "ninguna nueva"),
        })
        return True

    def _wmds_release_cancel_hold(self, operator=None):
        """La orden ya no se despachará en esta sesión (sesión cancelada/limpiada o todas
        sus EI retiradas): lo no entregado se considera NO embarcado."""
        for so in self:
            if not so._wmds_is_cancel_held():
                continue
            so.sudo().ei.filtered(lambda t: t.wmds_cancel_decision == 'shipped' and not t.dispatched) \
                .write({'wmds_cancel_decision': False, 'wmds_cancel_product_id': False})
            try:
                with self.env.cr.savepoint():
                    so._wmds_resolve_cancel_hold(operator=operator)
            except Exception:
                _logger.exception("WMDs: fallo al liberar despacho retenido de %s", so.name)


class WmdsDispatchSessionCancelHold(models.Model):
    _inherit = 'wmds.dispatch.session'

    def _wmds_release_cancel_holds(self, lines=None):
        """Libera las SO retenidas de estas líneas (antes de cancelar/limpiar la sesión)."""
        lines = lines if lines is not None else self.line_ids
        names = [n for n in set(lines.mapped('so_name')) if n]
        if names:
            self.env['sale.order'].sudo().search([('name', 'in', names)]) \
                ._wmds_release_cancel_hold(operator=self.operator_id[:1])

    # ------------------------------------------------ puntos de extensión del núcleo
    def _wmds_after_line_removed(self, ei_name, so_name, cancelled_removal=False, operator=None):
        res = super()._wmds_after_line_removed(
            ei_name, so_name, cancelled_removal=cancelled_removal, operator=operator)
        if not cancelled_removal:
            return res
        tag = self.env["sale.order.ei"].sudo().search([('display_name_custom', '=', ei_name)], limit=1)
        if tag:
            tag.write({'wmds_cancel_decision': 'removed', 'wmds_cancel_product_id': False})
        # Si ya no queda ninguna EI de esa orden retenida en despacho, se resuelve:
        # lo retirado (y no entregado) va a Guardería.
        so = self.env['sale.order'].sudo().search([('name', '=', so_name)], limit=1) if so_name else False
        if so and so._wmds_is_cancel_held():
            remaining = self.env["wmds.dispatch.session.line"].sudo().search_count([
                ('so_name', '=', so_name),
                ('session_id.state', '=', 'active'),
            ])
            if not remaining:
                so._wmds_release_cancel_hold(operator=operator)
        return res

    def _wmds_on_session_released(self, lines):
        # Las órdenes canceladas que esperaban este despacho ya no se embarcarán.
        res = super()._wmds_on_session_released(lines)
        self._wmds_release_cancel_holds(lines)
        return res


class WmdsDispatchSessionLineCancelHold(models.Model):
    _inherit = 'wmds.dispatch.session.line'

    def _wmds_extra_values(self):
        res = super()._wmds_extra_values()
        names = [n for n in self.mapped('ei_name') if n]
        tags = self.env["sale.order.ei"].sudo().search([('display_name_custom', 'in', names)]) if names else []
        tag_by_name = {}
        for tag in tags:
            tag_by_name.setdefault(tag.display_name_custom, tag)
        held_by_so = {}
        for line in self:
            so_name = line.so_name
            if so_name not in held_by_so:
                so = self.env['sale.order'].sudo().search([('name', '=', so_name)], limit=1) if so_name else False
                held_by_so[so_name] = bool(so) and so._wmds_is_cancel_held()
            tag = tag_by_name.get(line.ei_name)
            res[line.id] = dict(res.get(line.id, {}),
                                cancel_hold=held_by_so[so_name],
                                cancel_decision=tag.wmds_cancel_decision if tag else False)
        return res


class WmdsFeaturesCancel(models.AbstractModel):
    _inherit = 'wmds.features'

    def _wmds_features(self):
        res = super()._wmds_features()
        res['cancel'] = True
        return res
