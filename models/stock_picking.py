from odoo import fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    wb_dispatch_hold = fields.Boolean(
        string='Retenido por despacho', copy=False, readonly=True,
        help="OUT de una SO cancelada mientras sus paquetes estaban en despacho. No se "
             "cancela hasta que se indique qué se embarcó (WMDs o botón en la SO).")

    def action_cancel(self):
        if self.env.context.get('wb_keep_dispatch_hold'):
            self = self.filtered(lambda p: not p.wb_dispatch_hold)
        return super(StockPicking, self).action_cancel()
