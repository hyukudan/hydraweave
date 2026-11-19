# HydraWeave: cierre de implementación y segunda revisión

Fecha: 19 de noviembre de 2026. Rama: `audit/hydraweave-completeness-20261119`.

Este informe actualiza la [auditoría inicial](audit-2026-11-19.md) después de la autorización para corregir los
pendientes. La base de ambas fases es `main` en
[`54440e3ba3afbc0c979331d2336161c7684dcec4`](https://github.com/hyukudan/hydraweave/commit/54440e3ba3afbc0c979331d2336161c7684dcec4).
La primera tanda se publicó en `b0e0fb90bfc54702afd4d49782d8a3b765c4c18e`; esta segunda tanda continúa en la
[PR #34](https://github.com/hyukudan/hydraweave/pull/34). No se ha fusionado en `main` ni desplegado en los nodos.

## Resultado y alcance

Se han conectado los recorridos que estaban incompletos: actualización transaccional del agente, adquisición
de assets desde el navegador, verificación y copia directa entre nodos desde Library, consumo del contrato de
despliegue del engine y comprobación de la vigencia de las cifras usadas por el placement. Se han corregido
además carreras entre esos recorridos, reservas de capacidad duplicadas, limpieza de procesos tras crashes y
confirmaciones que podían sobrevivir a un cambio de configuración.

**Los perfiles gestionados usan los motores de inferencia propios del proyecto.** El repositorio contenía un
registro opcional y un constructor de argumentos de llama.cpp; eso no demostraba que los perfiles lo usasen.
Considerar su ampliación como requisito para completar HydraWeave fue un error de alcance de la revisión
inicial. La ampliación iniciada se retiró. Esta tanda no incorpora instalación, lanzamiento gestionado ni
dependencias nuevas de llama.cpp. Se conserva el registro previo y su validación ya publicada.

Los pins y los submódulos se conservan. Tampoco se habilitan presets que no tienen revisión o artefactos
calificados. Las correcciones de orquestación no certifican automáticamente todas las versiones de todos los
engines. No se han medido mejoras de tokens/s, latencia, ancho de banda USB4 ni consumo de GPU en esta sesión.

## Método de trabajo y revisión cruzada

Tres subagentes trabajaron en áreas separadas: lifecycle y updates; assets, Library y transferencias;
placement, identidad y calibración. La integración principal implementó el contrato de deployment, capacidad
del router y drain, y revisó los cruces de ownership entre procesos, jobs, leases y servicios. Después hubo
revisión cruzada entre responsables.

Esa segunda revisión encontró defectos concretos que también se corrigieron: reservas terminadas o
reemplazadas durante un sondeo, un runner por defecto que evitaba la nueva protección de procesos, apertura de
directorios después de comprobar symlinks, cancelación justo antes de registrar un asset, caducidad del owner
durante merge y una respuesta de copia residente que eludía la comprobación final común.

Las pruebas emplean archivos sintéticos, procesos propios, HTTP local, sockets de fixtures y JavaScript con
DOM simulado. Algunas usan procesos reales de transferencia y agentes HTTP en entornos separados. Los runners
de systemd y SSH se sustituyen por fixtures; no se ha ejecutado mantenimiento de los nodos del usuario.

## Estado actualizado de las capacidades

| Área | Estado del código después de la corrección | Límite o condición de uso |
|---|---|---|
| Configuración, perfiles y registro de features | Implementados; conservados | Guardar configuración no arranca motores. |
| Autenticación, discovery y pairing | Implementados; conservados | Descubrir un nodo no autoriza su ejecución. |
| Poll del portal, facts y sondeo de endpoints | Paralelismo con límites; monitor/poll publican por llegada | Facts reúne el conjunto antes de devolverlo; se conservan las exclusiones que protegen recursos. |
| Leases y servicios | Guardas compartidas con updates y trabajos de modelos | No se inicia trabajo nuevo durante una transacción o I/O incompatible. |
| Router local | Recuperación verificada y guard de lifetime inmediato en Linux | Necesita pidfd; no contiene procesos que deliberadamente abandonen el grupo. |
| Update del agente | Staging aparte, activación independiente, health y rollback | Managed root Linux con servicio de usuario; aceptación systemd real pendiente. |
| Migración del agente anterior | Recorrido concreto desde el protocolo existente | Necesita un servicio gestionado en ejecución para probar procedencia y rollback. |
| Releases del controlador | Transacción ya existente, correcciones de fase 1 conservadas | No se sustituyen por el updater del agente. |
| Deployment del engine propio | Integrado en perfiles, servicios, routers y planner | Opt-in explícito y release compatible instalada; no cambia pins. |
| Capacidad y drain | Contrato vivo, reservas coherentes y drain concurrente acotado | Los endpoints legacy conservan su compatibilidad documentada. |
| Assets web | Preparar, planificar, confirmar, descargar, verificar y registrar | Solo fuentes fijadas y destinos permitidos. |
| Library verify | Job real con cancelación, ownership y resultado persistente | Verifica la integridad declarada; no acredita rendimiento. |
| Library copy | Copia directa, hashes, reutilización, publicación y métricas | Fuente Linux gestionada y destino SSH Linux con clave fijada y ruta peer configurada. |
| Prefetch de copia | Lectura del siguiente bloque solapada con el envío | Dos bloques de payload de hasta 8 MiB, más buffer del lector; no constituye un scheduler global de residencia. |
| Placement heterogéneo | Evidencia actual vinculada a identidad y caducidad | Sin mediciones válidas ofrece ubicación sin predicción de rendimiento. |
| Solver y decode RTX experimental | Guardas y snapshots reforzados | Core externo y presets sin release siguen sin calificarse. |
| Chat y frontal | Correcciones de fase 1 conservadas y regresiones verificadas | Chat de texto devuelve resultado al terminar; no se añadió streaming incremental web. |
| llama.cpp opcional | Sin nueva integración gestionada | No es el engine de los perfiles ni un pendiente de este encargo. |

## 1. Updates: entorno aislado y éxito comprobable

### Problemas corregidos

La ruta anterior instalaba paquetes sobre el entorno del agente en ejecución y mantenía un bloqueo durante
operaciones lentas. Programar un restart tampoco demostraba que el nuevo agente hubiese arrancado. En un
fallo de pip o una caída del proceso podían quedar imports mezclados o descendientes de un build todavía vivos.

La nueva ruta prepara un venv único en `agent-releases/<transaction>`. El SHA-256 debe coincidir con el paquete
autenticado; se ejecutan `pip check` e imports con el intérprete aislado. El paquete debe cargar desde el
candidato, no desde el checkout o el entorno anterior. Ningún pip de esta ruta modifica el venv activo.

El agente solo mantiene el lock de admisión durante la comprobación y publicación del estado. Status, stop,
cancel y consulta/renovación de jobs siguen accesibles durante descarga y staging. Una mutación ya iniciada,
una lease, un turno no verificable, un job no terminal o el gate de I/O impiden actualizar.

El portal reserva únicamente la máquina seleccionada. Updates de nodos distintos pueden coexistir dentro del
límite de jobs, y la interfaz renueva su ownership, muestra progreso y permite cancelar cada espera por ID.
Un doble clic sobre el mismo nodo se rechaza incluso antes de recibir el primer ACK. Se comprueban máquina y
emparejamiento al ejecutar, y la conexión queda fijada privadamente durante la transacción: una readopción no
redirige una orden ni sus posteriores sondeos. Cancelar la espera deja al supervisor del nodo terminar la
activación o el rollback, protegido por el gate persistente.

Un worker independiente de systemd activa un drop-in reversible y verifica el listener autenticado: instancia
nueva, prefijo, transacción, versión y commit esperados. Si falla, restaura el drop-in anterior, reinicia el
entorno retenido y comprueba su salud. Una recuperación fallida mantiene la exclusión; no publica `ready`.
Los reintentos automáticos de rollback están limitados y queda una vía explícita de reintento tras reparar.

Hay un presupuesto absoluto de 1500 segundos para staging, con compare-and-publish bajo el lock compartido.
Un stager tardío no puede sobrescribir el fallo que ya declaró el supervisor. Los comandos de producción usan
`LocalChild`: terminan con su owner, se limpian sus descendientes al vencer el plazo y se drenan stdout/stderr
a logs acotados. La inyección de runners de pruebas no sustituye esa ruta en producción.

### Migración y exclusión entre transportes

Bootstrap también crea un entorno aparte para migrar un agente antiguo. Verifica MainPID, identidad de
arranque, argv, prefijo y propiedad del listener, además de la respuesta autenticada del protocolo anterior.
El rollback de una migración comprueba ese protocolo anterior; no presupone que el agente viejo tenga un
endpoint de salud nuevo. La instalación inicial comprueba la instancia que realmente sirve antes de dar éxito.

Agente HTTP, helper por SSH, servicios, leases y publicación de jobs comparten la exclusión de root. Publicar
un job en cola participa en el bloqueo: la ventana entre admisión y arranque del worker no permite introducir
una actualización concurrente. Se retienen los entornos previos; no se ha añadido poda automática.

Fuentes: [node_update.py](../hydraweave/node_update.py), [node_agent.py](../hydraweave/node_agent.py),
[bootstrap.py](../hydraweave/bootstrap.py), [contrato operativo](node-discovery.md),
[pruebas de transacción](../tests/test_node_update_transaction.py).

## 2. Lifetime y parada: conservar ownership hasta terminar

La fase 1 permitía recuperar un router Linux huérfano mediante una acción posterior. Ahora un shim inicia un
guard antes de ejecutar al hijo. Observa pidfds del controlador y del líder, permanece en el grupo del hijo y
lo termina si desaparece cualquiera de sus owners. Permanecer en el grupo evita señalizar un PGID reciclado.
La parada normal usa un canal privado hacia ese guard.

No se ejecuta `preexec_fn` Python en el controlador multihilo. Tampoco se ata la vida del hijo a la del hilo
que lo creó: finalizar ese hilo no mata al proceso si el controlador continúa. Se conservan Windows Job
Objects y la recuperación explícita de procesos legacy que no nacieron bajo el nuevo guard.

Antes de TERM se intenta drain de los líderes HTTP en paralelo, con una ventana común de 30 segundos. Una
respuesta `drained:false` se sondea hasta completarse o agotar ese plazo. Se conserva la parada leader-first
de MiniMax y el cierre de seguidores. Un fallo inesperado de la fase de drain no salta la limpieza de hijos:
se comunica después de ejecutar esa limpieza. Los endpoints antiguos no soportados siguen la parada existente.

La garantía cubre el grupo de procesos propiedad de HydraWeave. No se anuncia contención por cgroups ni captura
de procesos que se separen deliberadamente con una sesión nueva. Linux sin pidfd falla explícitamente.

Fuentes: [procs.py](../hydraweave/procs.py), [_child_guard.py](../hydraweave/_child_guard.py),
[lease y drain](../node/hydraweave_lease.py), [crashes reales](../tests/test_linux_child_guard.py).

## 3. Contrato del engine propio y corrección de la PR #11

Se ha portado y corregido la propuesta de la PR #11 sobre la base actual. Su head antiguo tiene conflictos y
no debe fusionarse por encima de esta implementación. El contrato se contrastó con el código del engine en
[`5c1298e85a314a95091ae5babe9a8a61ef3f8bf1`](https://github.com/hyukudan/strix-halo-engine-lab/tree/5c1298e85a314a95091ae5babe9a8a61ef3f8bf1).

La inspección verifica esquema, negativa explícita, topología, dispositivos medidos y contabilidad de cada
rank. Rechaza booleanos, valores no enteros, ranks repetidos, contradicciones entre inventario y rank, o owners
de memoria que no caben. Deben cumplirse conjuntamente:

```text
margen <= memoria libre medida
budget <= memoria libre medida - margen
required <= budget
free_after = budget - required
```

El caso detectado en la PR antigua —62 GiB requeridos, 62 GiB libres y 1 GiB de margen— ya se rechaza. También
se comprueba que el footprint completo sume weights, staging, KV, workspace y extras sin perder propietarios.

La opción de perfil `deploy.spec` es explícita. `auto` obtiene inventario local de cada nodo, reúne las
observaciones y reconsulta el par con un TOML por contenido. No se copia la memoria local como supuesto del
peer. Las inspecciones se solapan, con un máximo de ocho. Las topologías de lanzamiento se limitan a las
implementadas: una réplica de un rank o un par TP2.

### Identidad y capacidad durante el servicio

El ID del plan incluye observaciones de memoria libre: no equivale a identidad de proceso. Exigir que coincida
con una inspección anterior podía rechazar un arranque correcto. Ahora se valida el plan vivo y se fija su ID;
un cambio posterior bloquea admisión hasta recargar el router. Health y ownership siguen comprobando los
fallos de proceso que no modifican ese hash.

El plan se cachea para el ID vivo. Health y capacity se consultan en paralelo y cada nodo publica su resultado
al llegar. La ausencia legacy se vuelve a consultar después de 30 segundos, evitando repetir el mismo plan
completo o ausente en cada tick. Un contrato malformado no se transforma en fallback legacy.

La admisión utiliza slots ocupados, cola y capacidad KV. `kv_bytes_free:null` significa desconocido y conserva
la contabilidad por slots; cero conocido impide admisión nueva. Se capturan antes del sondeo `inflight` y el
contador monotónico `assigned` del pool. Al publicar se descuenta lo admitido durante la consulta y solo se
da crédito a reservas antiguas aún presentes. Así una finalización o sustitución de solicitudes no devuelve
capacidad dos veces. El límite nunca supera slots totales ni el máximo configurado.

La readiness de un par usa la credencial de rank 0, dueño del listener. Los helpers rechazan redirects con
credenciales y acotan salida, tiempo de inspección y limpieza de procesos. La estimación DS41 tampoco usa ya
93,09 GiB históricos como observación universal: acepta una medida explícita o etiqueta la predicción sin
medición actual.

Fuentes: [deployment.py](../hydraweave/deployment.py), [orquestación](../hydraweave/orchestrate.py),
[adaptador HTTP](../hydraweave/adapters/engine.py), [helper](../node/hydraweave_deploy.py),
[documentación y ejemplos](deployment.md), [regresiones](../tests/test_deployment_safety.py).

## 4. Assets y Library: recorridos conectados y transferencias directas

### Adquisición desde el navegador

Assets permite elegir un asset de perfil y una fuente fijada, preparar un directorio permitido, revisar tamaño,
destinos, hashes y espacio, confirmar el plan, observar el job y cancelarlo. El backend registra el asset y su
evidencia únicamente tras recibir un resultado íntegro y verificar su identidad final.

La confirmación vincula tanto el plan nativo como el perfil, transporte, fuente, destino y asset seleccionados.
Cambiar esos inputs invalida lo revisado. Se corrigió una huella inicialmente demasiado amplia que hacía fallar
la segunda de dos descargas independientes al registrar la primera. Ahora ambas pueden registrar sus assets
sin perder cambios ajenos; una prueba de barrera sobre dos jobs reales de la API verifica ese caso.
La cancelación se comprueba después de los stats remotos y dentro de la
transacción de registro. Una respuesta antigua del navegador tampoco puede actualizar la selección nueva.

Se reutiliza el catálogo existente. No se aceptan URLs, hashes o comandos arbitrarios del navegador. Los
artefactos derivados sin una fuente verificable permanecen no disponibles con su razón visible. Preparar un
destino no autoriza cualquier ruta posterior: plan y ejecución vuelven a validar las restricciones.

### Verificación y copia entre nodos

Library ahora ejecuta jobs de verificación y copia. La copia requiere un manifiesto completo y no ambiguo de
SHA-256, raíces configuradas, ruta peer explícita y host key de destino fijada. Los pesos viajan directamente
del nodo origen al destino por SSH. El controlador coordina y recibe progreso y recibos; no retransmite pesos.

La fuente se abre mediante dirfds y rechaza symlinks, y se comprueban tamaño e identidad antes y después de
leer. El destino también se abre por componentes con `O_NOFOLLOW`. Se verifica cada archivo durante el envío
y se publica solo después de comprobar tamaño/hash y recibir confirmación. La publicación no reemplaza una
unidad ajena; los fallos entre archivo y sidecar retiran únicamente los enlaces creados por ese job.

Si la unidad final ya es íntegra, se reutiliza sin enviar sus bytes. Los archivos completos y verificados de
staging también se reutilizan. Esto no promete reanudación a mitad de archivo. Se solapa la lectura del bloque
siguiente con el envío del actual mediante dos bloques de payload de hasta 8 MiB, más el buffer alineado del
lector y overhead; la memoria de prefetch está acotada, pero no se anuncia un máximo total de 16 MiB.

Copy, verify y download mantienen un owner con caducidad y un gate de I/O. La caducidad se comprueba también
durante hashing, merge y publicación. La observación pasiva de jobs no renueva ese owner. Al perder contacto o
cancelar, el controlador deja de renovar, intenta cancelar y conserva admisión durante el plazo de limpieza.
Si no observa el final, registra resultado no confirmado; el gate del nodo sigue protegiendo los recursos.

Los resultados distinguen bytes transferidos, reutilizados y bytes de pesos a través del controlador (cero).
Las pruebas demuestran esas propiedades sobre archivos de fixture. No establecen una cifra de rendimiento ni
demuestran que el sistema entero minimice el tráfico USB4: no hay un planificador global nuevo de residencia,
recomputación y prefetch de todos los modelos.

Fuentes: [assets web](../hydraweave/web/features/assets.py), [coordinador Library](../hydraweave/library_jobs.py),
[jobs de nodo](../node/hydraweave_model_jobs.py), [descargas](../node/hydraweave_download.py),
[contratos y límites](assets.md), [coordinación](../tests/test_library_job_coordination.py),
[recorridos JS](../tests/test_assets_library_ui_workflow.py).

## 5. Placement: predicciones ligadas a evidencia vigente

Se corrigió un fallo que ocultaba la identidad real del modelo: el lector buscaba por nombre lógico de asset,
mientras que el manifiesto estaba indexado por ruta absoluta. Ahora se usa esa ruta y se comprueban tamaño,
inode, dispositivo y mtime. La recolección pasiva no abre el contenido de los pesos.

Los costes y curvas solo se admiten con un recibo que coincida con revisión del engine, hashes del binario y
modelo, identidad física de GPU y driver, topología y capacidades aportadas por el probe, receta, parámetros, workload, contexto,
enlaces medidos y residencia. Se requieren método, número de ejecuciones, hashes de recibos fuente y fechas
explícitas. La calibración puede durar como máximo siete días; su probe y la caché de mediciones transfer/storage
deben ser actuales dentro de 24 horas. La caché legacy de hechos de disco conserva su ventana distinta de siete
días y no equivale a ese recibo de rendimiento. Fechas futuras, recibos ajenos o contenido modificado no
renuevan la evidencia.

Sin calibración se conservan ubicaciones factibles como `unpredicted`; no se inventa throughput. Un recibo
aportado pero inválido provoca una negativa explícita. Al guardar una selección se vuelven a comprobar
documentos, componentes, cachés y recibos. Las selecciones del contrato antiguo requieren solve nuevo y las
filas extrapoladas no pueden guardarse.

La caché de transferencia ya no completa identidad faltante del probe con datos de su propia clave antigua.
UUID ambiguo, driver desconocido, store ausente o hash desconocido producen miss. En AMD se recogen hechos
pasivos de sysfs y kernel/ROCm; un índice de tarjeta o una dirección PCI no se presenta como UUID físico.

La caché de disco incluye nanosegundos e inode, fusiona escritores bajo lock y publica atómicamente. Importar
una medición conserva su fecha original. El modelo heterogéneo histórico permanece identificado como tal;
las curvas se validan y sus extrapolaciones se marcan.

El recibo valida vinculación y procedencia declarada; no firma al productor ni demuestra por sí solo que se
ejecutó el benchmark. La aceptación nativa del solver necesita su core. No hay un `ts-recipe` compatible en
este entorno: las pruebas ejercitan la frontera Python/CLI con runner de fixture, no el cálculo nativo real.

Fuentes: [calibración](../hydraweave/recipes/calibration.py), [solver](../hydraweave/recipes/solver.py),
[mediciones](../hydraweave/recipes/measurements.py), [facts](../hydraweave/recipes/facts.py),
[contrato y ejemplo](performance-calibration.md), [regresiones](../tests/test_placement_calibration.py).

## Paralelismo, esperas y límites conservados

| Recorrido | Cambio comprobable | Límite conservado |
|---|---|---|
| Recogida de facts | Fan-out entre nodos | Hasta ocho; orden local de dependencias. |
| Router | Health/capacity en paralelo, publicación por nodo | Un nodo lento no retiene el resultado ya disponible de otro. |
| Plan de deployment | Caché por identidad viva | Se invalida al cambiar el ID; no se reutiliza una negativa como éxito. |
| Inspección del par | Inventarios y planes independientes solapados | La segunda fase espera el inventario completo porque lo necesita. |
| Drain | Líderes independientes en paralelo | Ventana común y orden de salida necesario para MiniMax. |
| Copia | Lectura anticipada mientras se envía | Dos bloques de payload más lector; cancelación y hashes permanecen activos. |
| Inventario Library | Scans independientes concurrentes | Hasta ocho nodos, resultados por llegada y fallos aislados. |
| Updates | I/O lento fuera del lock de admisión | Activación/rollback conservan exclusión de nuevas mutaciones. |
| Cachés | Escritura atómica y fusión bajo lock | El acceso no usa evidencia caducada ni mezcla sesiones. |

Las exclusiones entre un engine que ocupa recursos y un job pesado no se eliminan por parecer secuenciales.
Tampoco se especula con trabajo GPU, instalación o traslado de pesos desde una lectura pasiva del portal.
El siguiente paso para decidir más solapamiento es medir el recurso compartido y el resultado en hardware.

## Validación

El pase integrado de host terminó con **5.625 passed, 34 skipped, 6 deselected y 11 failed**, en 635,65 s.
Los once fallos se reprodujeron y correspondían a cuatro fixtures anteriores a los contratos nuevos:

| Fixture | Casos | Corrección sin relajar producción |
|---|---|---|
| Watchdog de medición | 2 | Aportar las tres métricas reales de transferencia y comprobar sus medianas; mantener las comprobaciones del watchdog. |
| Recorte de bootstrap shell | 6 | Ejecutar también la inicialización real de `root`; conservar guard de symlinks, SHA y limpieza. |
| Frontera del core | 1 | Esperar el probe efectivo de solve sin transferencias no medidas; conservar identidad, capas y argumentos. |
| Merge del starter | 2 | Aportar la ruta de parcial que contiene un plan real y usar el fixture portable de entrada; conservar hashes, retención y reintento. |

Tras esas correcciones y el cierre de registros disjuntos de Assets, la selección final de los cuatro archivos
afectados, Assets HTTP/JS y coordinación de Library terminó con **106 passed y 3 skipped**, en 3,34 s. Los tres
skips requieren el core externo. No quedó un fallo reproducido pendiente. El pase amplio anterior no se
presenta retroactivamente como una ejecución íntegramente verde del árbol final.

Comando del pase amplio:

```sh
python -m pytest -q -k 'not EnergyPagesInBrowser and not LibraryPageInBrowser'
```

Los seis casos excluidos son los mismos de la primera fase: Chromium no arranca en este host, con una
reproducción mínima de `about:blank` que termina en `socket() failed: Operation not permitted`. No se relajaron
sus assertions ni se convirtieron en aprobados. Los recorridos HTTP y el JavaScript real con fixtures DOM sí
se ejecutaron. Esto no equivale a validación visual del navegador.

Las selecciones finales por área incluyen **285 passed / 3 skipped** en placement; **90 passed** en cierre de
updates, admisión y UI; **60 passed** en workflows de modelos después de su revisión cruzada; y **36 passed**
en deployment, carreras de capacidad, drain e inventario de API. Se han comprobado además los nuevos casos de
registro concurrente en la selección final de 106. Estas selecciones se solapan con el pase amplio.

Los **41 archivos Python de producción** cambiados en esta fase aceptan gramática de Python 3.10; la ejecución
local fue con Python 3.12. Pasan `node --check` de las cuatro páginas JS modificadas y `git diff --check`.
`check_versions_lock.py` conserva el aviso preexistente de los dos pins de TensorMeld, destinados a usos
distintos según la auditoría inicial; no se han alineado ni actualizado por conveniencia de las pruebas.

Las selecciones dirigidas se solapan y no deben sumarse: deployment/capacidad/drain, update y lifetime,
transferencia/cancelación/UI y placement/calibración. Los casos de concurrencia emplean barreras y estados
observables; no se limitan a verificar que exista un thread pool. Los casos de integridad comparan hashes,
recibos y bytes reales, incluidos destino ya residente y fallo a mitad de publicación.

La CI de la primera tanda `b0e0fb9` terminó con los once jobs correctos: Ubuntu/Windows en Python 3.11, 3.12 y
3.13; herramienta de nodo en Python 3.10; dos recetas Debian y dos instalaciones. Ese resultado corresponde
solo a aquella revisión. La CI de la segunda tanda debe consultarse por su SHA en la PR.

## Lo que requiere aceptación en las máquinas de destino

| Prueba pendiente de hardware o entorno externo | Qué debe comprobarse |
|---|---|
| Migración y update systemd reales | Arranque inicial, update con trabajo bloqueado, instancia nueva, fallo inducido y rollback del protocolo anterior. |
| Engine y deployment por familia/revisión | Inspección, admisión nativa con presión real de memoria, health autenticado, capacidad, TP2 y drain. |
| USB4/red y modelos grandes | Throughput, bytes realmente movidos, residente sin retransmisión, cancelación, pérdida de peer y coste del hashing. |
| Placement heterogéneo | Medidas de las GPU/drivers actuales, residencia y enlaces; fidelidad, latencia y rendimiento sobre igual configuración. |
| Core nativo `ts-recipe` | Admisión y predicción con binario compatible y datos reales; no sustituir por los fixtures de frontera. |
| Rutas experimentales/artefactos derivados | Releases, modelos y manifiestos disponibles antes de habilitar presets. |

Estos puntos no se marcan como completados por haber pasado pruebas de host. La rama queda preparada para
revisión y CI; la fusión y el despliegue no se han realizado. El reporte inicial conserva el detalle de las
26 familias de defectos de la primera fase y este documento describe su estado posterior y los recorridos
adicionales implementados.
