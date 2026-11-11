from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Secuencia de los traslados de cancelación: CAN00001 → WH/CAN/00001."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['stock.warehouse'].search([('wb_cancel_picking_type_id', '!=', False)])._wb_fix_cancel_sequence()
