{
    'name': 'WMDs · Cancelaciones',
    'summary': 'Complemento de WMDs: stock de SO canceladas a Guardería (WH/CAN) y OUT retenido si la cancelación llega en pleno despacho',
    'description': 'Complemento de WMDs. Reubica a Guardería (WH/CAN) el stock varado en Packing Zone y Output al cancelar una SO, '
                   'y retiene el OUT cuando la SO se cancela en pleno despacho. Se activa en WMDS → Configuración → Ajustes. Ver README.md.',
    'version': '18.0.2.0.0',
    'category': 'Inventory/Inventory',
    'author': 'Wonderbrands',
    'license': 'LGPL-3',
    'depends': ['wmds', 'sale_stock'],
    'data': [
        'security/ir.model.access.csv',
        'views/stock_warehouse_views.xml',
        'views/dispatch_hold_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'application': False,
    'installable': True,
}
