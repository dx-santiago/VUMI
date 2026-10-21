# VUMI — Video Upload Middleware Interface

[English version](README.md)

VUMI es una aplicación de escritorio para Windows que funciona como middleware entre grabadores Hikvision DVR/NVR y Backblaze B2. Detecta cámaras mediante ISAPI, descarga las grabaciones del rango de fecha y hora seleccionado, crea un MP4 por cámara y día laboral sin recodificar, y sube cada archivo mediante la API compatible con S3.

Las cargas son multipartes y reanudables. Antes de eliminar un video local terminado, VUMI valida el objeto remoto y crea a su lado un manifiesto JSON con su checksum, información multimedia y metadatos de la carga.

## ¿Para qué sirve?

- Guardar varios perfiles de conexión Hikvision DVR/NVR en un baúl local.
- Transferir cámaras de uno o varios grabadores en un único lote centralizado.
- Procesar las cámaras secuencialmente para limitar la carga del DVR/NVR, la red y el disco.
- Reanudar descargas y cargas multipartes interrumpidas usando el estado local.
- Evitar sobrescrituras silenciosas comparando el tamaño y el SHA-256 del objeto.
- Aislar los errores por combinación de cámara y fecha para que el resto del lote continúe.

## Requisitos

- Windows 10 o posterior.
- Python 3.10 o posterior.
- Una cuenta del DVR/NVR Hikvision con acceso de reproducción remota e ISAPI habilitado.
- Un bucket privado de Backblaze B2 y una Application Key restringida a ese bucket.
- `ffmpeg.exe` y `ffprobe.exe`, disponibles en `PATH` o dentro de una carpeta seleccionada en la aplicación.

Descarga FFmpeg desde el [sitio oficial de FFmpeg](https://ffmpeg.org/download.html).

## Instalación y ejecución

Abre PowerShell dentro de la carpeta del proyecto:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python index.py
```

Si PowerShell bloquea la activación del entorno virtual, permite scripts firmados localmente para tu usuario o ejecuta directamente el Python del entorno:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe index.py
```

## Configuración y uso

1. En **Baúl de DVRs**, crea o selecciona un perfil, asígnale un nombre reconocible e ingresa su host, puertos, usuario, contraseña y desfase UTC. Para Colombia usa `-05:00`.
2. Selecciona **Guardar DVR**, **Probar NVR** y luego **Detectar cámaras**. La detección reemplaza el catálogo de ese perfil para no conservar accidentalmente cámaras de otro grabador.
3. Mueve a la lista derecha las cámaras requeridas de ese DVR. Cambia de perfil y repite el proceso para cada grabador que participará. La selección se conserva por DVR y la etiqueta muestra el total del lote centralizado.
4. Ingresa el endpoint S3 de Backblaze, el bucket, el Key ID y la Application Key; después selecciona **Probar Backblaze**.
5. Elige el rango de fechas, la franja diaria, una carpeta temporal y la carpeta de FFmpeg si no está en `PATH`.
6. Selecciona **Estimar** para consultar todos los DVR seleccionados y luego inicia la transferencia unificada tras revisar el resumen.

Solo se procesan los días de lunes a viernes. Los festivos no se excluyen automáticamente.

Los objetos usan la siguiente estructura:

```text
ivms/<dvr>/<camara>/<AAAA>/<MM>/<DD>/<camara>_<AAAA-MM-DD>_<HHMM>-<HHMM>.mp4
```

El DVR original migrado conserva su estructura anterior `ivms/<camara>/...` para mantener la reanudación y evitar cargas duplicadas. Los perfiles nuevos usan la estructura con DVR mostrada arriba. Cada video tiene un manifiesto contiguo `.mp4.json`. El estado reanudable se guarda como `job_<id>.json` en la carpeta temporal seleccionada.

Los DVR y sus cámaras se procesan secuencialmente dentro del mismo lote para controlar el uso de red, disco y grabadores. Un error en un origen queda aislado y no detiene los demás DVR.

## Historial visual de auditoría

Abre **4. Auditoría** para consultar el último resultado conocido de cada cámara y fecha grabada. La vista combina el registro persistente con los estados reanudables y permite:

- Ver totales de transferencias completadas, sin grabación, con error o canceladas.
- Buscar por cámara, fecha grabada, detalle o ruta del objeto.
- Filtrar por estado y reconocer resultados mediante colores.
- Consultar tamaño, ruta en Backblaze, SHA-256, error, fecha de actualización y fuente local de evidencia.
- Abrir directamente el registro persistente o la carpeta temporal configurada.

La vista se actualiza al abrirla, al seleccionar **Actualizar** y después de terminar un lote. Los metadatos de auditoría nunca incluyen hosts del DVR/NVR, endpoints, nombres de bucket, usuarios ni credenciales.

## Recuperación

- Reinicia el trabajo con los mismos parámetros para reutilizar segmentos descargados y partes multipartes ya cargadas.
- Si ya existe un objeto con el mismo tamaño y SHA-256, VUMI no vuelve a subirlo.
- Si existe otro contenido en la misma ruta, VUMI informa un conflicto y no lo sobrescribe.
- Los MP4 locales se conservan cuando falla la validación o la carga.
- Usa **Cancelar** en lugar de cerrar la ventana a la fuerza para mantener un estado de reanudación coherente.

## Seguridad y privacidad

- La contraseña de cada DVR/NVR y las credenciales de Backblaze se guardan por separado mediante el Administrador de credenciales de Windows usando `keyring`.
- Las preferencias no secretas se guardan en `%APPDATA%\IVMSBackblazeDownloader\config.json` y no forman parte del repositorio.
- Los archivos de ejecución, logs, videos temporales, estados de trabajo, configuraciones locales y formatos comunes de credenciales están excluidos de Git.
- Usa una clave exclusiva de Backblaze, restringida al bucket de destino y con los permisos mínimos necesarios para listar, leer y escribir archivos. Mantén el bucket privado y nunca publiques endpoints, hosts, credenciales ni manifiestos generados.
- Revoca y reemplaza cualquier credencial que haya podido quedar expuesta antes de usar VUMI.

## Licencia

VUMI se distribuye bajo la [licencia MIT](LICENSE).
