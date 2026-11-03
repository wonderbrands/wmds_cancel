-- Renombra el módulo wb_so_cancel_guarderia → wmds_cancel conservando sus datos
-- (Guardería, WH/CAN, configuración por almacén, OUT retenidos y decisiones por EI).
--
-- Solo para BD que ya tenían instalado wb_so_cancel_guarderia. Uso, con Odoo detenido:
--   psql -d <bd> -f rename_from_wb_so_cancel_guarderia.sql
--   odoo -c odoo.conf -d <bd> -u wmds,wmds_cancel --stop-after-init
-- Es idempotente: si no hay nada que renombrar, no cambia nada.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ir_module_module WHERE name = 'wb_so_cancel_guarderia') THEN
        RAISE NOTICE 'wb_so_cancel_guarderia no está en esta BD: nada que renombrar.';
        RETURN;
    END IF;
    -- Si Odoo ya registró la carpeta wmds_cancel (lista de aplicaciones) queda como
    -- 'uninstalled' y sin datos: ese registro vacío se quita para poder renombrar.
    IF EXISTS (SELECT 1 FROM ir_module_module WHERE name = 'wmds_cancel' AND state <> 'uninstalled') THEN
        RAISE EXCEPTION 'wmds_cancel ya está instalado junto a wb_so_cancel_guarderia; revisar a mano.';
    END IF;
    DELETE FROM ir_model_data
     WHERE module = 'base' AND model = 'ir.module.module' AND name = 'module_wmds_cancel';
    DELETE FROM ir_module_module WHERE name = 'wmds_cancel' AND state = 'uninstalled';

    UPDATE ir_module_module SET name = 'wmds_cancel' WHERE name = 'wb_so_cancel_guarderia';
    UPDATE ir_module_module_dependency SET name = 'wmds_cancel' WHERE name = 'wb_so_cancel_guarderia';
    UPDATE ir_model_data SET name = 'module_wmds_cancel'
     WHERE module = 'base' AND model = 'ir.module.module' AND name = 'module_wb_so_cancel_guarderia';
    UPDATE ir_model_data SET module = 'wmds_cancel' WHERE module = 'wb_so_cancel_guarderia';

    -- Campos de la EI que vivían en wmds y ahora define wmds_cancel: sin esto, -u wmds
    -- los borraría (y sus columnas) antes de que wmds_cancel los vuelva a crear vacíos.
    UPDATE ir_model_data SET module = 'wmds_cancel'
     WHERE module = 'wmds' AND name IN (
        'field_sale_order_ei__wmds_cancel_decision',
        'field_sale_order_ei__wmds_cancel_product_id',
        'selection__sale_order_ei__wmds_cancel_decision__shipped',
        'selection__sale_order_ei__wmds_cancel_decision__removed');

    RAISE NOTICE 'Renombrado a wmds_cancel. Ahora: odoo -u wmds,wmds_cancel';
END
$$;

COMMIT;
