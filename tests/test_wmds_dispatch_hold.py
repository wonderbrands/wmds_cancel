# -*- coding: utf-8 -*-
"""Cancelación de una SO en pleno despacho de WMDs (rutas y flujo de despacho)."""
import json
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

import odoo.addons.wmds_cancel.controllers.cancel_dispatch as cancel_ctrl
import odoo.addons.wmds.controllers.dispatch as dispatch_ctrl
import odoo.addons.wmds.controllers.dispatch_session_controller as session_ctrl
import odoo.addons.wmds.controllers.dock_n_bin as dock_n_bin_ctrl


@tagged('post_install', '-at_install')
class TestCancelDispatchHold(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.wh = cls.env['stock.warehouse'].search([('company_id', '=', cls.env.company.id)], limit=1)
        cls.wh.delivery_steps = 'pick_pack_ship'
        cls.wh._wb_setup_cancel_flow()
        cls.p1 = cls.env['product.product'].create({'name': 'Despacho P1', 'is_storable': True})
        cls.p2 = cls.env['product.product'].create({'name': 'Despacho P2', 'is_storable': True})
        for product in (cls.p1, cls.p2):
            cls.env['stock.quant']._update_available_quantity(product, cls.wh.lot_stock_id, 50)
        cls.partner = cls.env['res.partner'].create({'name': 'Cliente despacho'})
        cls.operator = cls.env['res.users'].create({
            'name': 'Despachador prueba', 'login': 'despachador_cancel_test',
            'email': 'despachador_cancel_test@example.com',
            'groups_id': [(6, 0, [cls.env.ref('stock.group_stock_user').id])],
        })
        cls.session = cls.env['wmds.dispatch.session'].create({'operator_id': cls.operator.id})

    def setUp(self):
        super().setUp()
        mock_request = MagicMock()
        mock_request.env = self.env
        self.enterContext(patch.object(dispatch_ctrl, 'request', mock_request))
        self.enterContext(patch.object(session_ctrl, 'request', mock_request))
        self.enterContext(patch.object(cancel_ctrl, 'request', mock_request))
        self.enterContext(patch.object(dock_n_bin_ctrl, 'request', mock_request))
        self.session_api = session_ctrl.DispatchSessionController()
        self.cancel_api = cancel_ctrl.CancelDispatchController()
        self.dispatch_api = dispatch_ctrl.DispatchController() \
            if hasattr(dispatch_ctrl, 'DispatchController') else self._find_controller(dispatch_ctrl)

    @staticmethod
    def _find_controller(module):
        for value in vars(module).values():
            if isinstance(value, type) and hasattr(value, 'dispatch_packet'):
                return value()
        raise AssertionError("No se encontró el controlador de despacho")

    # ---------------------------------------------------------------- helpers
    def _validate(self, so, ptype):
        picking = so.picking_ids.filtered(
            lambda p: p.picking_type_id == ptype and p.state not in ('done', 'cancel'))
        picking.action_assign()
        picking.with_context(skip_backorder=True, skip_sms=True, skip_immediate=True).button_validate()

    def _so_on_dock(self, lines, in_session=True):
        """SO con PICK/PACK validados, una EI por pieza en DOCK y (opcional) escaneadas en la sesión."""
        so = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'warehouse_id': self.wh.id,
            'order_line': [(0, 0, {'product_id': p.id, 'product_uom_qty': q}) for p, q in lines],
        })
        so.action_confirm()
        self._validate(so, self.wh.pick_type_id)
        self._validate(so, self.wh.pack_type_id)
        total = int(sum(q for _p, q in lines))
        tags = self.env['sale.order.ei'].create([{'so_id': so.id, 'on_dock': True} for _i in range(total)])
        if in_session:
            for tag in tags:
                self.env['wmds.dispatch.session.line'].create({
                    'session_id': self.session.id,
                    'ei_name': tag.display_name_custom,
                    'so_name': so.name,
                })
        return so, tags.sorted('sequence_number')

    def _cancel(self, so):
        so.with_context(disable_cancel_warning=True).action_cancel()

    def _out(self, so):
        return so.picking_ids.filtered(lambda p: p.picking_type_id == self.wh.out_type_id)

    def _cans(self, so):
        return self.env['stock.picking'].search([
            ('origin', '=', 'Cancelacion %s' % so.name), ('state', '!=', 'cancel')])

    def _ship(self, tag, product=None):
        return self.cancel_api.mark_cancelled_shipped(
            operator_login=self.operator.login, ei_name=tag.display_name_custom,
            product_id=product.id if product else None)

    def _remove(self, tag):
        return self.session_api.remove_dispatch_session_line(
            operator_login=self.operator.login, ei_name=tag.display_name_custom, cancelled_removal=True)

    def _dispatch(self, tags):
        return self.dispatch_api.dispatch_packet(
            picks_ids=tags.mapped('display_name_custom'), operator_login=self.operator.login)

    # ------------------------------------------------------------------ tests
    def test_w1_cancel_in_session_holds_out(self):
        so, _tags = self._so_on_dock([(self.p1, 2)])
        self.assertTrue(so._wb_is_in_dispatch())
        self._cancel(so)
        self.assertEqual(so.state, 'cancel')
        self.assertTrue(self._out(so).wb_dispatch_hold)
        self.assertEqual(self._out(so).state, 'assigned')
        self.assertFalse(self._cans(so))

    def test_w2_not_in_session_normal_cancel(self):
        so, _tags = self._so_on_dock([(self.p1, 2)], in_session=False)
        self.assertFalse(so._wb_is_in_dispatch())
        self._cancel(so)
        self.assertEqual(self._out(so).state, 'cancel')
        self.assertEqual(self._cans(so).move_ids.product_uom_qty, 2)
        # Un pedido que no quedó retenido no admite "Embarcada"
        self.assertFalse(self._ship(_tags[0])['ok'])

    def test_w3_partial_shipped_multi_product(self):
        """Caso b): 2 productos, se embarcó 1 y el otro se retiró."""
        so, tags = self._so_on_dock([(self.p1, 1), (self.p2, 1)])
        self._cancel(so)
        res = self._ship(tags[0])
        self.assertTrue(res['need_product'], "Con varios productos se debe pedir el producto")
        self.assertEqual({p['id'] for p in res['products']}, {self.p1.id, self.p2.id})
        self.assertTrue(self._ship(tags[0], self.p1)['ok'])
        self.assertTrue(self._remove(tags[1])['ok'])
        self.assertTrue(self._out(so).wb_dispatch_hold, "Aún queda una EI en la sesión")
        res = self._dispatch(tags[0])
        self.assertEqual(res['status'], 'success', res)
        out = self._out(so)
        self.assertEqual(out.state, 'done')
        done = out.move_ids.filtered(lambda m: m.state == 'done')
        self.assertEqual((done.product_id, done.quantity), (self.p1, 1))
        can = self._cans(so)
        self.assertEqual((can.move_ids.product_id, can.move_ids.product_uom_qty), (self.p2, 1))
        self.assertTrue(tags[0].dispatched)
        self.assertEqual(tags[1].wmds_cancel_decision, 'removed')

    def test_w4_all_shipped_single_product(self):
        """Caso a): se embarcó todo; producto único, no se pregunta el producto."""
        so, tags = self._so_on_dock([(self.p1, 2)])
        self._cancel(so)
        for tag in tags:
            res = self._ship(tag)
            self.assertTrue(res['ok'] and not res['need_product'], res)
        self.assertTrue(self._ship(tags[0])['ok'], "Re-marcar la misma EI es idempotente")
        res = self._dispatch(tags)
        self.assertEqual(res['status'], 'success', res)
        out = self._out(so)
        self.assertEqual(out.state, 'done')
        self.assertEqual(out.move_ids.quantity, 2)
        self.assertFalse(self._cans(so))

    def test_w5_all_removed_releases_hold(self):
        so, tags = self._so_on_dock([(self.p1, 2)])
        self._cancel(so)
        self._remove(tags[0])
        self.assertTrue(self._out(so).wb_dispatch_hold)
        self._remove(tags[1])
        self.assertEqual(self._out(so).state, 'cancel')
        self.assertEqual(self._cans(so).move_ids.product_uom_qty, 2)

    def test_w6_undecided_blocks_dispatch(self):
        so, tags = self._so_on_dock([(self.p1, 1)])
        self._cancel(so)
        res = self._dispatch(tags)
        self.assertEqual(res['status'], 'error')
        self.assertTrue(self._out(so).wb_dispatch_hold)
        self.assertFalse(tags.dispatched)

    def test_w7_cancel_session_releases_hold(self):
        so, tags = self._so_on_dock([(self.p1, 2)])
        self._cancel(so)
        self._ship(tags[0])
        self.session_api.cancel_dispatch_session(operator_login=self.operator.login)
        self.assertEqual(self._out(so).state, 'cancel', "Sin despacho, nada se embarcó")
        self.assertEqual(self._cans(so).move_ids.product_uom_qty, 2)
        self.assertFalse(tags[0].wmds_cancel_decision)

    def test_w8_previously_dispatched_counts_as_shipped(self):
        """Entrega parcial previa: EI 1 ya entregada (OUT pendiente), EI 2 en sesión y se retira."""
        so, tags = self._so_on_dock([(self.p1, 2)], in_session=False)
        tags[0].write({'dispatched': True, 'on_dock': False})
        self.env['wmds.dispatch.session.line'].create({
            'session_id': self.session.id, 'ei_name': tags[1].display_name_custom, 'so_name': so.name})
        self._cancel(so)
        self.assertTrue(self._out(so).wb_dispatch_hold)
        self._remove(tags[1])
        out = self._out(so)
        self.assertEqual(out.state, 'done')
        self.assertEqual(out.move_ids.filtered(lambda m: m.state == 'done').quantity, 1)
        self.assertEqual(self._cans(so).move_ids.product_uom_qty, 1)

    def test_w9_session_lines_report_hold(self):
        so, tags = self._so_on_dock([(self.p1, 1)])
        self._cancel(so)
        self._ship(tags[0])
        res = self.session_api.get_dispatch_session(operator_login=self.operator.login)
        line = next(l for l in res['lines'] if l['ei_name'] == tags[0].display_name_custom)
        self.assertTrue(line['cancel_hold'])
        self.assertEqual(line['cancel_decision'], 'shipped')

    def test_w10_feature_registered(self):
        self.assertTrue(self.env['wmds.features']._wmds_features().get('cancel'))

    def test_w11_queued_dispatch_resolves_hold(self):
        """Despacho grande (>10 guías) por la cola: la orden retenida se resuelve igual."""
        so, tags = self._so_on_dock([(self.p1, 1), (self.p2, 1)])
        self._cancel(so)
        self.assertTrue(self._ship(tags[0], self.p1)['ok'])
        self.assertTrue(self._remove(tags[1])['ok'])
        params = {'picks_ids': [tags[0].display_name_custom], 'operator_login': self.operator.login}
        task = self.env['wmds.queued_tasks'].create({
            'task_type': 'dispatch_package', 'params': json.dumps(params), 'operator_login': self.operator.login,
        })
        # La cola hace commit, prohibido dentro de una prueba
        with patch.object(self.env.cr, 'commit', lambda: None):
            has_errors, summary = task._execute_dispatch_package(params)
        self.assertFalse(has_errors, summary)
        out = self._out(so)
        self.assertEqual(out.state, 'done')
        done = out.move_ids.filtered(lambda m: m.state == 'done')
        self.assertEqual((done.product_id, done.quantity), (self.p1, 1))
        self.assertEqual(self._cans(so).move_ids.product_id, self.p2)
        self.assertTrue(tags[0].dispatched)
        self.assertIn('Despacho de orden cancelada resuelto', task.info_message)

    def test_w12_late_scan_of_held_order(self):
        """Guía escaneada después de cancelar una orden retenida: el escaneo informa la
        retención y, ya en la sesión, mantiene la retención hasta despacharla."""
        so, tags = self._so_on_dock([(self.p1, 2)], in_session=False)
        self.env['wmds.dispatch.session.line'].create({
            'session_id': self.session.id, 'ei_name': tags[0].display_name_custom, 'so_name': so.name})
        self._cancel(so)
        scan = dock_n_bin_ctrl.DockNBin().validate_attachment_guide(attachment_id=tags[1].display_name_custom)
        self.assertEqual((scan['so_state'], scan['cancel_hold']), ('cancel', True))
        saved = self.session_api.save_dispatch_session_line(
            operator_login=self.operator.login, ei_name=scan['name'], so_name=scan['so'],
            total=scan['total'], current=scan['current'])
        self.assertTrue(saved['ok'], saved)
        self.assertTrue(self._ship(tags[1])['ok'])
        self._remove(tags[0])
        self.assertTrue(self._out(so).wb_dispatch_hold, "La guía tardía sigue en la sesión")
        res = self._dispatch(tags[1])
        self.assertEqual(res['status'], 'success', res)
        out = self._out(so)
        self.assertEqual((out.state, out.move_ids.filtered(lambda m: m.state == 'done').quantity), ('done', 1))
        self.assertEqual(self._cans(so).move_ids.product_uom_qty, 1)

    def test_w13_row_removal_of_cancelled_order_releases_hold(self):
        """La guía se quita con el botón de la fila (sin cancelled_removal) porque la cancelación
        llegó después de escanearla: la retención se resuelve igual."""
        so, tags = self._so_on_dock([(self.p1, 1)])
        self._cancel(so)
        self.session_api.remove_dispatch_session_line(
            operator_login=self.operator.login, ei_name=tags[0].display_name_custom)
        self.assertEqual(tags[0].wmds_cancel_decision, 'removed')
        self.assertEqual(self._out(so).state, 'cancel')
        self.assertEqual(self._cans(so).move_ids.product_uom_qty, 1)

    def test_w14_queued_dispatch_undecided_reports_error(self):
        """En la cola, una guía retenida sin decisión deja la tarea con errores, no 'completada'."""
        so, tags = self._so_on_dock([(self.p1, 1)])
        self._cancel(so)
        params = {'picks_ids': [tags[0].display_name_custom], 'operator_login': self.operator.login}
        task = self.env['wmds.queued_tasks'].create({
            'task_type': 'dispatch_package', 'params': json.dumps(params), 'operator_login': self.operator.login,
        })
        with patch.object(self.env.cr, 'commit', lambda: None):
            has_errors, summary = task._execute_dispatch_package(params)
        self.assertTrue(has_errors)
        self.assertIn(so.name, summary)
        self.assertTrue(self._out(so).wb_dispatch_hold, "Sin decisión la orden sigue retenida")
        self.assertFalse(tags[0].dispatched)
