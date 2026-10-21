# VUMI — Video Upload Middleware Interface

[Versión en español](README.es.md)

VUMI is a Windows desktop middleware application that transfers Hikvision DVR/NVR recordings to Backblaze B2. It discovers cameras through ISAPI, downloads recordings for a selected date and time range, assembles one MP4 per camera and business day without re-encoding, and uploads each file through the S3-compatible API.

Uploads are multipart and resumable. Before removing a completed local video, VUMI validates the remote object and writes an adjacent JSON manifest containing its checksum, media information, and upload metadata.

## What it is for

- Keep multiple Hikvision DVR/NVR connection profiles in a local vault.
- Move recordings from cameras across one or more recorders in a single centralized batch.
- Process cameras sequentially to limit DVR/NVR, network, and disk load.
- Resume interrupted downloads and multipart uploads from local state.
- Avoid silent overwrites by comparing the object size and SHA-256 checksum.
- Keep failures isolated to a camera/date pair so the remaining batch can continue.

## Requirements

- Windows 10 or later.
- Python 3.10 or later.
- A Hikvision DVR/NVR account with remote playback access and ISAPI enabled.
- A private Backblaze B2 bucket and an application key restricted to that bucket.
- `ffmpeg.exe` and `ffprobe.exe`, either available in `PATH` or in a folder selected in the application.

Download FFmpeg from the [official FFmpeg website](https://ffmpeg.org/download.html).

## Installation and startup

Open PowerShell in the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python index.py
```

If PowerShell blocks virtual-environment activation, either allow locally signed scripts for your user or run the environment's Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe index.py
```

## Configuration and use

1. In **Baúl de DVRs**, create or select a recorder profile, give it a recognizable name, and enter its host, ports, username, password, and UTC offset. For Colombia, use `-05:00`.
2. Select **Guardar DVR**, **Probar NVR**, and then **Detectar cámaras**. Detection replaces that profile's catalog so cameras from another recorder are not retained accidentally.
3. Move the cameras required from that DVR into its right-hand list. Switch profiles and repeat for every recorder that must participate. Selections remain stored per DVR and the label shows the total centralized batch size.
4. Enter the Backblaze S3 endpoint, bucket name, Key ID, and Application Key, then select **Probar Backblaze**.
5. Choose the date range, daily time window, temporary working directory, and FFmpeg directory if it is not in `PATH`.
6. Select **Estimar** to query all selected DVRs, then start the unified transfer after reviewing the summary.

Only Monday through Friday are processed. Public holidays are not excluded automatically.

Objects use the following layout:

```text
ivms/<dvr>/<camera>/<YYYY>/<MM>/<DD>/<camera>_<YYYY-MM-DD>_<HHMM>-<HHMM>.mp4
```

The migrated original DVR keeps its previous `ivms/<camera>/...` layout to preserve resume compatibility and avoid duplicate uploads. New DVR profiles use the namespaced layout above. Each video has an adjacent `.mp4.json` manifest. Resumable job state is stored as `job_<id>.json` in the selected temporary directory.

DVRs and cameras are processed sequentially inside the same batch to keep network, disk, and recorder load controlled. An error from one source remains isolated and does not stop the remaining DVRs.

## Visual audit history

Open **4. Auditoría** to review the latest known result for every camera and recording date. The view combines the persistent activity log with resumable job state and provides:

- Totals for completed, empty, failed, and cancelled transfers.
- Search by camera, recording date, detail, or object path.
- A status filter and color-coded results.
- Size, Backblaze object path, SHA-256, error detail, update time, and local evidence source.
- Shortcuts to open the persistent log and the configured temporary directory.

The view refreshes when opened, when **Actualizar** is selected, and after a batch finishes. Audit metadata never includes DVR/NVR hosts, cloud endpoints, bucket names, usernames, or credentials.

## Recovery behavior

- Restart the same job parameters to reuse completed download segments and uploaded multipart parts.
- If an object with the same size and SHA-256 already exists, VUMI does not upload it again.
- If different content already exists at the same object key, VUMI reports a conflict and does not overwrite it.
- Local MP4 files are retained when validation or upload fails.
- Use **Cancelar** instead of force-closing the window so VUMI can preserve coherent resume state.

## Security and privacy

- Each DVR/NVR password and the Backblaze credentials are stored separately through Windows Credential Manager via Python `keyring`.
- Non-secret preferences are stored in `%APPDATA%\IVMSBackblazeDownloader\config.json` and are not part of the repository.
- Runtime files, logs, temporary videos, job state, local configuration, and common credential-file formats are excluded from Git.
- Use a dedicated Backblaze key restricted to the destination bucket, with only the permissions required to list, read, and write files. Keep the bucket private and never commit real endpoints, hostnames, credentials, or generated job manifests.
- Revoke and replace any credential that may have been exposed before using VUMI.

## License

VUMI is released under the [MIT License](LICENSE).
