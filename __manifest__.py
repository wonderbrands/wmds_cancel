{
    'name': 'Cancelaciones de venta → Guardería',
    'summary': 'Al cancelar una SO, reubica el stock varado en Packing Zone/Output hacia Guardería (WH/CAN)',
    'description': 'Reubica a Guardería (WH/CAN) el stock varado en Packing Zone y Output al cancelar una SO. Ver README.md.',
    'version': '18.0.1.1.0',
    'category': 'Inventory/Inventory',
    'author': 'Wonderbrands',
    'license': 'LGPL-3',
    'depends': ['sale_stock'],
    'data': [
        'security/ir.model.access.csv',
        'views/stock_warehouse_views.xml',
        'views/dispatch_hold_views.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'application': False,
    'installable': True,
}
