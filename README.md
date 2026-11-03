# WMDs · Cancelaciones (`wmds_cancel`)

Complemento de **WMDs** para Odoo 18 Enterprise que, al **cancelar una orden de venta (SO)**, traslada automáticamente el stock
que ya había salido de su ubicación a **`WH/Guarderia_Cancelacion`**, usando un tipo de operación propio
**`WH/CAN` («Traslados de Cancelación»)**.

> **Estado:** en uso, con tests automáticos. Depende de `wmds` y `sale_stock`; se activa desde
> **WMDS → Configuración → Ajustes → Complementos → Cancelaciones**.
>
> Antes se llamaba `wb_so_cancel_guarderia`. Para migrar una BD que ya lo tenía instalado, ver §5b.

---

## 1. ¿Qué problema resuelve?

En un almacén con entrega en 3 pasos (**PICK → PACK → OUT**), si una venta se cancela cuando el producto ya
salió del rack, Odoo lo deja "varado" en `Packing Zone`, en `Output` o en el carrito. Nadie lo ve, el inventario
no cuadra y el producto se pierde de vista. Este módulo lo manda a un lugar conocido (**Guardería**) mediante un
traslado que el operador valida con Barcode, dejando trazabilidad.

## 2. ¿Qué hace, según el momento de la cancelación?

| Momento de la cancelación | Dónde está el producto | Resultado |
|---|---|---|
| PICK sin validar, **nada recolectado** | En el rack | Nada: Odoo libera las reservas (nativo). |
| PICK sin validar, **con unidades ya recolectadas en WMDs** *(opcional)* | En el carrito/patín (el sistema aún lo ve en el rack) | CAN **rack(s) de origen → Guardería** con lo recolectado. |
| PICK validado, PACK sin validar | `Packing Zone` | CAN `Packing Zone → Guardería` *(configurable)*. |
| PACK validado, OUT pendiente | `Output` | CAN `Output → Guardería`. |
| OUT pendiente **y paquetes en despacho** (WMDs) | En el camión/contenedor o en staging de embarque | El OUT queda **retenido**; al cerrar el despacho sale al cliente lo embarcado y el resto va a Guardería (ver §3b). |
| OUT validado parcialmente | `Output` (remanente) | CAN solo por el remanente. |
| OUT validado completo | Con el cliente | Nada: es una devolución normal. |

Reglas generales: solo productos **almacenables**, solo SO **confirmadas** (borrador/enviada no hacen nada) y solo
almacenes con **entrega en 3 pasos**.

## 3. Cómo funciona (en palabras simples)

1. Se cancela la SO (cualquier usuario con permiso de ventas).
2. Odoo cancela lo pendiente y libera las reservas (comportamiento nativo).
3. El módulo calcula **cuánto dejó esa SO** en `Packing Zone` y `Output`: suma lo que entró y resta lo que salió,
   considerando solo los movimientos de **esa** SO. Así, si dos SO comparten el mismo producto y zona, cada
   cancelación mueve únicamente lo suyo.
4. Resta lo que ya se hubiera trasladado en CAN anteriores de la misma SO (si se reabre y se cancela otra vez, **no se duplica**).
5. Crea **un CAN por ubicación de origen**, lo confirma y lo reserva en la misma transacción.
6. Deja en el chatter de la SO los CAN generados y cualquier stock que quedó sin reubicar.

Para la recolección en curso, el módulo lee `wmds_picked_qty` **antes** de cancelar (cancelar el PICK borra esas
líneas) y genera un solo CAN con una línea por rack real de origen.

## 3b. Cancelación durante el despacho (OUT retenido)

Caso: la SO se cancela cuando sus paquetes (EI) ya están en el despacho y algunos ya entraron al contenedor del
carrier y no se pueden sacar.

1. **Al cancelar:** si la SO está "en despacho", su OUT **no se cancela**: queda *retenido* (`wb_dispatch_hold`).
   La SO sí queda cancelada (marketplace y facturación correctos). No se crea CAN de `Output` todavía.
   - "En despacho" (`sale.order._wb_is_in_dispatch()`): alguna EI de la SO está en una sesión de despacho activa de
     WMDs o ya se entregó a la paquetería.
2. **En el despacho (WMDs):** el modal de cancelados permite, por EI, **retirarla** (escanear, como antes) o marcar
   **«Ya se embarcó»**. Si la SO tiene varios productos, se elige el producto (1 EI = 1 pieza).
3. **Al cerrar el despacho** ("Entregar a paquetería") se llama `sale.order._wb_resolve_dispatch_hold({producto: piezas})`:
   - el OUT se valida **solo por lo embarcado** (sale al cliente como venta normal; Operaciones gestiona la devolución),
     el resto se cancela sin backorder;
   - lo que se quedó en `Output` va a Guardería con el CAN de siempre (cálculo de saldo neto).
4. **Si no se despacha:** al retirar todas las EI de la SO, o al cancelar/limpiar la sesión, se resuelve como
   "nada embarcado" (solo cuentan las EI que ya se habían entregado antes).
5. **Respaldo manual:** botón **«Resolver despacho retenido»** en la SO (usuarios de Inventario): indica las piezas
   embarcadas por producto. Necesario si había EI entregadas antes de la cancelación y la SO tiene varios productos
   (WMDs no sabe qué producto lleva cada EI).

Ejemplos: SO de 2 productos, se embarcó todo → OUT por 2, sin CAN. Se embarcó 1 y el otro se quedó en staging →
OUT por 1 y CAN `Output → Guardería` por 1.

## 4. Protecciones

- **Nunca bloquea la cancelación:** si algo falla, la SO se cancela igual; queda el error en el log y un aviso en el chatter.
- **Permisos:** las operaciones de stock se hacen con `sudo`; un usuario solo de Ventas puede cancelar.
- **Sin impacto contable:** el CAN es Internal→Internal; no crea capas de valoración (verificado con AVCO).
- **Trazabilidad:** origen `Cancelacion <SO>` + mensaje en el chatter. No se asigna el grupo de la SO al CAN, para no alterar los contadores de entregas de la SO.
- **Avisos:** almacén sin configurar o stock fuera de alcance → advertencia en el chatter.

## 5. Instalación y configuración

1. El módulo está en `odoo/addons-custom/wmds_cancel`. Activarlo en **WMDS → Configuración → Ajustes →
   Complementos → Cancelaciones** y guardar (equivale a instalar `wmds_cancel`). Desmarcarlo lo desinstala.
2. Al instalar, se configuran solos los almacenes con entrega en 3 pasos (ubicación Guardería + tipo CAN).
   Para otros almacenes: usar el botón **Configurar flujo de cancelación** (es idempotente).
3. Si ya estaba instalado: actualizar con `-u wmds_cancel` para ver los campos nuevos.
4. Antes de desinstalarlo, resolver los OUT retenidos: al desinstalar se borran sus campos (la ubicación Guardería y
   los movimientos hechos se conservan).

## 5b. Migrar desde `wb_so_cancel_guarderia`

Solo para BD que tenían instalado el módulo con su nombre anterior. Conserva Guardería, WH/CAN, la configuración por
almacén, los OUT retenidos y las decisiones por EI. Con Odoo detenido y un respaldo de la BD:

```bash
psql -d <bd> -f odoo/addons-custom/wmds_cancel/scripts/rename_from_wb_so_cancel_guarderia.sql
odoo -c odoo.conf -d <bd> -u wmds,wmds_cancel --stop-after-init
```

El script es idempotente: si no encuentra `wb_so_cancel_guarderia`, no cambia nada.

**Dónde se configura:** Inventario → Configuración → Almacenes → *(almacén)* → pestaña **Configuración del
almacén** → grupo **Cancelaciones de venta** (visible solo con entrega en 3 pasos).

| Campo | Qué hace | Por defecto |
|---|---|---|
| Ubicación Guardería | Destino de los traslados (interna, barcode `<WH>GUARDERIA`). | Se crea sola |
| Tipo de operación de cancelación | El tipo CAN (barcode `<WH>CAN`, reserva al confirmar, backorder siempre). | Se crea solo |
| Incluir Packing Zone | Reubica también lo que quedó en `Packing Zone`. | Sí |
| Incluir recolección en curso | Traslada lo ya recolectado en WMDs cuando el PICK no se validó. | No |
| Validar CAN automáticamente | Valida el CAN al instante si queda totalmente reservado. | No |

> ⚠️ **Validar CAN automáticamente** mueve el stock en el sistema sin que el operador lo lleve físicamente a
> Guardería: el inventario puede quedar desalineado con la mercancía. Úsalo solo si otro proceso hace el traslado físico.

Si `stock_barcode` está instalado, el CAN exige escanear ubicación origen y destino.

## 6. Operación diaria

Ver el SOP para operadores: [`docs/SOP_operador_cancelaciones.md`](docs/SOP_operador_cancelaciones.md)
(Barcode → tarjeta *Traslados de Cancelación* → escanear origen, producto, Guardería → validar).

## 7. Limitaciones conocidas

- **Lotes/series:** no soportados todavía (productos sin tracking). Hay `# TODO lotes` en los puntos exactos del código.
- **Resets de WMDs:** `cancel_batch` y `remove_picking_from_batch` ponen `wmds_picked_qty` en 0 sin cancelar la SO;
  si después se cancela la SO, esas unidades ya no se detectan.
- **Reservas compartidas:** `Packing Zone`/`Output` no pertenecen a ninguna SO; si otra operación reservó esas unidades, el
  CAN puede quedar con reserva incompleta (el chatter lo advierte).
- **Reapertura de SO:** Odoo 18 no regenera PICK/PACK/OUT al reabrir una SO cuyo flujo ya se hizo; el módulo solo garantiza que no se duplique el CAN.
- **Paquetes:** con *Poner en paquete* en PACK, el CAN reserva el paquete completo y **llega intacto a Guardería** (no se desempaca).
- **Salida de Guardería:** aún no hay procedimiento (ver fases).
- **Cancelación después de validar el OUT:** si la cancelación llega cuando `dispatch_packet` ya validó el OUT, no hay nada
  que retener: es una devolución normal.
- **Pedido cancelado antes de escanearse en despacho:** no queda retenido (su stock ya va a Guardería); el modal solo
  permite retirarlo.
- **OUT retenido sin resolver:** si nadie cierra el despacho ni usa el botón de la SO, el OUT se queda retenido (visible con
  un aviso en el OUT y el botón en la SO).

## 8. Diferencias respecto al diseño original (Odoo 18)

- `stock.location.return_location` **no existe** en Odoo 18: no se usa.
- Opciones de Barcode (`restrict_scan_source_location`, `restrict_scan_dest_location`): se activan solo si el campo existe.
- El aviso de fallo en el chatter también está protegido (un usuario sin email haría fallar `message_post` y bloquearía la cancelación).
- Escenario "reabrir y validar PICK/PACK otra vez" no es alcanzable en Odoo 18 (ver §7); el test verifica que no se duplique.

## 9. Pruebas

22 tests (`tests/test_cancel_relocation.py`) + 9 del despacho en WMDs (`tests/test_wmds_dispatch_hold.py`): flujo completo, PICK sin validar, Packing Zone incluida/excluida, dos SO
en la misma zona, reapertura sin duplicados, usuario de Ventas, fallo forzado, configuración idempotente, entrega parcial,
SO en borrador, almacén sin configurar, paquetes, validación automática, recolección en curso y OUT retenido por
despacho (retención, todo embarcado, parcial, nada embarcado, asistente manual; en WMDs: modal, retiro total, sesión
cancelada, EI entregadas antes de la cancelación). La recolección en curso se
prueba con un campo sustituto (`x_wmds_picked_qty`); en el ambiente real se validó con WMDs.

```bash
cd <carpeta-raiz-de-odoo>
PYTHONPATH=. .venv/bin/python3 setup/odoo -c odoo.conf -d wb_cancel_test \
  -i wb_sale_wholesale_approval,wmds,wmds_cancel --without-demo=all \
  --test-tags /wmds_cancel --stop-after-init --http-port=8099 --log-level=test
```
Usar siempre una BD desechable (`wb_cancel_test`), nunca la de producción. `wb_sale_wholesale_approval` se instala
explícitamente porque WMDs usa su campo `data_is_wholesale_sale` sin declararlo como dependencia.

## 10. Estructura

```
wmds_cancel/
├── models/stock_warehouse.py   # campos de configuración + _wb_setup_cancel_flow()
├── models/sale_order.py        # _action_cancel, reubicación y OUT retenido por despacho
├── models/stock_picking.py     # wb_dispatch_hold + no cancelar OUT retenidos
├── models/wmds_dispatch_hold.py   # decisión por EI, puntos de extensión del despacho y clave `cancel` en /wmds/v2/features
├── controllers/cancel_dispatch.py # ruta /wmds/v2/engine/post/mark_cancelled_shipped
├── wizard/dispatch_hold_wizard.py  # «Resolver despacho retenido» (respaldo manual)
├── security/ir.model.access.csv
├── views/stock_warehouse_views.xml
├── views/dispatch_hold_views.xml   # botón en la SO, aviso en el OUT, asistente
├── hooks.py                    # configura almacenes de 3 pasos al instalar
├── scripts/rename_from_wb_so_cancel_guarderia.sql  # migración del nombre anterior
├── tests/test_cancel_relocation.py
├── tests/test_wmds_dispatch_hold.py
└── docs/SOP_operador_cancelaciones.md
```

## 11. Siguientes fases (propuesta)

| Fase | Tema | Notas |
|---|---|---|
| ✅ | **Cancelación durante el despacho** | OUT retenido + decisión por EI en WMDs (§3b). |
| 2 | **Salida de Guardería** | Inspección → reingreso a `WH/Stock` o scrap, con su propio tipo de operación y SOP. |
| 3 | **Lotes/series** | Incluir `lot_id` en el saldo y crear los movimientos del CAN con el lote (`# TODO lotes`). |
| 4 | **Notificaciones** | Actividades/avisos a supervisores cuando hay CAN pendientes, reserva incompleta o stock sin reubicar. |
| 5 | **Reporte/tablero** | Stock actual en Guardería por SO de origen y antigüedad. |
| — | **Decisiones abiertas** | ¿Tipo de operación sin "escanear origen" para recolección en curso? ¿Comportamiento de paquetes en Guardería? ¿Auto-validación en algún escenario? |
