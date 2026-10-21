# VUMI — Video Upload Middleware Interface

[Versión en español](README.es.md)

VUMI is a Windows desktop middleware application that transfers Hikvision DVR/NVR recordings to Backblaze B2. It discovers cameras through ISAPI, downloads recordings for a selected date and time range, assembles one MP4 per camera and business day without re-encoding, and uploads each file through the S3-compatible API.

Uploads are multipart and resumable. Before removing a completed local video, VUMI validates the remote object and writes an adjacent JSON manifest containing its checksum, media information, and upload metadata.

## What it is for

- Move recordings from one or more Hikvision channels to private cloud storage.
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

1. Enter the DVR/NVR host, HTTP and RTSP ports, username, password, and UTC offset. For Colombia, use `-05:00`.
2. Select **Probar NVR**, then **Detectar cámaras**. Move every camera to process into the right-hand list. Cameras can also be added manually when the firmware does not expose discovery.
3. Enter the Backblaze S3 endpoint, bucket name, Key ID, and Application Key, then select **Probar Backblaze**.
4. Choose the date range, the daily time window, a temporary working directory, and the FFmpeg directory if it is not in `PATH`.
5. Select **Estimar** to query the expected recording size, then start the transfer after reviewing the summary.

Only Monday through Friday are processed. Public holidays are not excluded automatically.

Objects use the following layout:

```text
ivms/<camera>/<YYYY>/<MM>/<DD>/<camera>_<YYYY-MM-DD>_<HHMM>-<HHMM>.mp4
```

Each video has an adjacent `.mp4.json` manifest. Resumable job state is stored as `job_<id>.json` in the selected temporary directory.

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

- DVR/NVR passwords and Backblaze credentials are stored through Windows Credential Manager via Python `keyring`.
- Non-secret preferences are stored in `%APPDATA%\IVMSBackblazeDownloader\config.json` and are not part of the repository.
- Runtime files, logs, temporary videos, job state, local configuration, and common credential-file formats are excluded from Git.
- Use a dedicated Backblaze key restricted to the destination bucket, with only the permissions required to list, read, and write files. Keep the bucket private and never commit real endpoints, hostnames, credentials, or generated job manifests.
- Revoke and replace any credential that may have been exposed before using VUMI.

## License

VUMI is released under the [MIT License](LICENSE).
