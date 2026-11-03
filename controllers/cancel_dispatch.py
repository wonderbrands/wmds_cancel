# -*- coding: utf-8 -*-
"""Rutas de WMDs para la cancelación durante el despacho."""
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class CancelDispatchController(http.Controller):

    # ─────────────────────────────────────────────
    # MARK a cancelled EI as already shipped (in the carrier's container)
    # ─────────────────────────────────────────────
    @http.route('/wmds/v2/engine/post/mark_cancelled_shipped', type='json', auth='user', methods=['POST'], csrf=True)
    def mark_cancelled_shipped(self, **kw):
        """
        El despachador indica que la EI de una orden cancelada YA está dentro del
        contenedor/carrier y no se puede sacar. Si la orden tiene varios productos,
        debe indicarse cuál (product_id); si falta, se devuelve la lista de opciones.
        """
        try:
            operator_login = kw.get("operator_login")
            ei_name = kw.get("ei_name")
            product_id = kw.get("product_id")
            if not operator_login or not ei_name:
                return {"ok": False, "message": "Datos incompletos"}

            tag = request.env["sale.order.ei"].sudo().search([('display_name_custom', '=', ei_name)], limit=1)
            if not tag or not tag.so_id:
                return {"ok": False, "message": f"La guía {ei_name} no existe."}
            so = tag.so_id
            if not so._wmds_is_cancel_held():
                return {"ok": False, "message": f"La orden {so.name} no admite marcar paquetes como embarcados: "
                                              f"se canceló antes de entrar al despacho. Retírala físicamente."}

            products = so._wmds_cancel_hold_products()
            # Si ya tenía producto asignado, liberar esa pieza para recalcular
            if tag.wmds_cancel_decision == 'shipped' and tag.wmds_cancel_product_id:
                for p in products:
                    if p['id'] == tag.wmds_cancel_product_id.id:
                        p['remaining'] += 1
            available = [p for p in products if p['remaining'] >= 1]

            if len({p['id'] for p in products}) > 1:
                if not product_id:
                    return {"ok": True, "need_product": True, "products": available}
                if int(product_id) not in [p['id'] for p in available]:
                    return {"ok": False, "message": "El producto seleccionado no tiene piezas pendientes en esta orden.",
                            "need_product": True, "products": available}
                product = int(product_id)
            else:
                if not available:
                    return {"ok": False, "message": "Ya se marcaron como embarcadas todas las piezas de esta orden."}
                product = available[0]['id']

            tag.write({'wmds_cancel_decision': 'shipped', 'wmds_cancel_product_id': product})
            operator = request.env["res.users"].sudo().search([('login', '=', operator_login)], limit=1)
            request.env["wmds.log"].sudo().create({
                "sale": so.id,
                "user": operator.id if operator else request.env.user.id,
                "log": f"Operador {operator.name if operator else operator_login} confirmó que el paquete "
                       f"{ei_name} de la orden cancelada ya está embarcado en el carrier.",
            })
            return {"ok": True, "need_product": False}

        except Exception as e:
            _logger.error(f"Error en mark_cancelled_shipped: {e}")
            return {"ok": False, "message": str(e)}
