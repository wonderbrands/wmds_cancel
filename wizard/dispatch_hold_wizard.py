from odoo import _, api, fields, models
from odoo.exceptions import UserError


class DispatchHoldWizard(models.TransientModel):
    _name = 'wb.dispatch.hold.wizard'
    _description = 'Resolver OUT retenido por cancelación en despacho'

    sale_order_id = fields.Many2one('sale.order', required=True, readonly=True)
    line_ids = fields.One2many('wb.dispatch.hold.wizard.line', 'wizard_id')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        so = self.env['sale.order'].browse(res.get('sale_order_id'))
        if so:
            pending = {}
            for move in so._wb_held_pickings().move_ids.filtered(
                    lambda m: m.state not in ('done', 'cancel')):
                product = move.product_id
                pending[product] = pending.get(product, 0.0) + move.product_uom._compute_quantity(
                    move.product_uom_qty, product.uom_id, round=False)
            res['line_ids'] = [(0, 0, {
                'product_id': product.id, 'qty_out': qty, 'qty_shipped': 0.0,
            }) for product, qty in pending.items()]
        return res

    def action_confirm(self):
        self.ensure_one()
        for line in self.line_ids:
            if line.qty_shipped < 0 or line.qty_shipped > line.qty_out:
                raise UserError(_(
                    "La cantidad embarcada de %s debe estar entre 0 y %s.",
                    line.product_id.display_name, line.qty_out))
        shipped = {line.product_id.id: line.qty_shipped for line in self.line_ids if line.qty_shipped}
        if not self.sale_order_id._wb_resolve_dispatch_hold(shipped):
            raise UserError(_("Esta orden ya no tiene un OUT retenido."))
        return {'type': 'ir.actions.act_window_close'}


class DispatchHoldWizardLine(models.TransientModel):
    _name = 'wb.dispatch.hold.wizard.line'
    _description = 'Línea: cantidad embarcada por producto'

    wizard_id = fields.Many2one('wb.dispatch.hold.wizard', required=True, ondelete='cascade')
    product_id = fields.Many2one('product.product', required=True, readonly=True)
    qty_out = fields.Float('En el OUT', readonly=True, digits='Product Unit of Measure')
    qty_shipped = fields.Float('Embarcado', digits='Product Unit of Measure')
