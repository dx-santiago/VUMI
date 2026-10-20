# VUMI — Video Upload Middleware Interface

[English version](README.md)

VUMI es una aplicación de escritorio para Windows que funciona como middleware entre grabadores Hikvision DVR/NVR y Backblaze B2. Detecta cámaras mediante ISAPI, descarga las grabaciones del rango de fecha y hora seleccionado, crea un MP4 por cámara y día laboral sin recodificar, y sube cada archivo mediante la API compatible con S3.

Las cargas son multipartes y reanudables. Antes de eliminar un video local terminado, VUMI valida el objeto remoto y crea a su lado un manifiesto JSON con su checksum, información multimedia y metadatos de la carga.

## ¿Para qué sirve?

- Transferir grabaciones de uno o varios canales Hikvision a almacenamiento privado en la nube.
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

1. Ingresa el host del DVR/NVR, los puertos HTTP y RTSP, el usuario, la contraseña y el desfase UTC. Para Colombia usa `-05:00`.
2. Selecciona **Probar NVR** y luego **Detectar cámaras**. Mueve a la lista derecha todas las cámaras que quieras procesar. También puedes agregarlas manualmente cuando el firmware no permita detectarlas.
3. Ingresa el endpoint S3 de Backblaze, el nombre del bucket, el Key ID y la Application Key; después selecciona **Probar Backblaze**.
4. Elige el rango de fechas, la franja horaria diaria, una carpeta temporal y la carpeta de FFmpeg si no está en `PATH`.
5. Selecciona **Estimar** para consultar el tamaño esperado y luego inicia la transferencia tras revisar el resumen.

Solo se procesan los días de lunes a viernes. Los festivos no se excluyen automáticamente.

Los objetos usan la siguiente estructura:

```text
ivms/<camara>/<AAAA>/<MM>/<DD>/<camara>_<AAAA-MM-DD>_<HHMM>-<HHMM>.mp4
```

Cada video tiene un manifiesto contiguo `.mp4.json`. El estado reanudable se guarda como `job_<id>.json` en la carpeta temporal seleccionada.

## Recuperación

- Reinicia el trabajo con los mismos parámetros para reutilizar segmentos descargados y partes multipartes ya cargadas.
- Si ya existe un objeto con el mismo tamaño y SHA-256, VUMI no vuelve a subirlo.
- Si existe otro contenido en la misma ruta, VUMI informa un conflicto y no lo sobrescribe.
- Los MP4 locales se conservan cuando falla la validación o la carga.
- Usa **Cancelar** en lugar de cerrar la ventana a la fuerza para mantener un estado de reanudación coherente.

## Seguridad y privacidad

- Las contraseñas del DVR/NVR y las credenciales de Backblaze se guardan mediante el Administrador de credenciales de Windows usando `keyring`.
- Las preferencias no secretas se guardan en `%APPDATA%\IVMSBackblazeDownloader\config.json` y no forman parte del repositorio.
- Los archivos de ejecución, logs, videos temporales, estados de trabajo, configuraciones locales y formatos comunes de credenciales están excluidos de Git.
- Usa una clave exclusiva de Backblaze, restringida al bucket de destino y con los permisos mínimos necesarios para listar, leer y escribir archivos. Mantén el bucket privado y nunca publiques endpoints, hosts, credenciales ni manifiestos generados.
- Revoca y reemplaza cualquier credencial que haya podido quedar expuesta antes de usar VUMI.

## Licencia

VUMI se distribuye bajo la [licencia MIT](LICENSE).
