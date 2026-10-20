from __future__ import annotations

import queue
import shutil
import threading
import time
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    from tkcalendar import DateEntry
except ImportError:  # pragma: no cover
    DateEntry = None

from .cloud import B2MultipartClient, normalize_endpoint, validate_cameras
from .config import app_dir, camera_entries, load_config, load_secret, save_config, save_secret
from .isapi import IsapiClient
from .media import find_binary
from .models import CameraConfig, CloudConfig, DeviceConfig, JobConfig
from .runner import BatchJobRunner


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("iVMS → Backblaze B2")
        self.geometry("1180x920")
        self.minsize(1080, 700)
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._last_log_at: dict[str, float] = {}
        self._task_order: dict[str, int] = {}
        self.values: dict[str, tk.StringVar] = {}
        self.available_count = tk.StringVar(value="Disponibles (0)")
        self.selected_count = tk.StringVar(value="Para descargar (0)")
        self._active_camera_tree = None
        self._configure_styles()
        self._build()
        self._load()
        self.after(150, self._poll)

    def _var(self, name: str, default: str = "") -> tk.StringVar:
        variable = tk.StringVar(value=default)
        self.values[name] = variable
        return variable

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.configure("TLabel", font=("Segoe UI", 9))
        style.configure("TButton", padding=(9, 5))
        style.configure("TLabelframe.Label", font=("Segoe UI Semibold", 10))
        style.configure("Treeview", rowheight=27, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 18))
        style.configure("Subtitle.TLabel", foreground="#5b6573", font=("Segoe UI", 9))
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 9), padding=(14, 7))

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="Grabaciones iVMS a Backblaze", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Selecciona las cámaras, define el rango y transfiere las grabaciones con reanudación segura.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=1, column=0, sticky="nsew")
        self.camera_tab = ttk.Frame(self.notebook, padding=10)
        self.settings_tab = ttk.Frame(self.notebook, padding=10)
        self.progress_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.camera_tab, text="1. Cámaras")
        self.notebook.add(self.settings_tab, text="2. Destino y rango")
        self.notebook.add(self.progress_tab, text="3. Progreso")
        for tab in (self.camera_tab, self.settings_tab, self.progress_tab):
            tab.columnconfigure(0, weight=1)

        nvr = ttk.LabelFrame(self.camera_tab, text="Conexión al grabador", padding=10)
        nvr.grid(row=0, column=0, sticky="new")
        fields = [
            ("IP / host", "host", ""),
            ("Puerto HTTP", "http_port", "80"),
            ("Puerto RTSP", "rtsp_port", "554"),
            ("Usuario", "nvr_user", "admin"),
            ("Contraseña", "nvr_password", ""),
            ("Zona UTC", "utc_offset", "-05:00"),
        ]
        self._field_grid(nvr, fields, password_names={"nvr_password"})
        camera_box = ttk.LabelFrame(nvr, text="Selección de cámaras", padding=8)
        camera_box.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        camera_box.columnconfigure(0, weight=1)
        camera_box.columnconfigure(2, weight=1)
        ttk.Label(
            camera_box,
            text="Mueve a la columna derecha todas las cámaras cuyas grabaciones quieres descargar.",
            style="Subtitle.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 7))

        available_box = ttk.Frame(camera_box)
        available_box.grid(row=1, column=0, sticky="nsew")
        available_box.columnconfigure(0, weight=1)
        ttk.Label(available_box, textvariable=self.available_count, font=("Segoe UI Semibold", 9)).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.available_camera_tree = self._camera_tree(available_box)

        transfer = ttk.Frame(camera_box, padding=(10, 24, 10, 0))
        transfer.grid(row=1, column=1, sticky="ns")
        ttk.Button(transfer, text="Agregar →", command=self._move_selected_to_download, width=17).pack(fill="x")
        ttk.Button(transfer, text="Agregar todas ⇒", command=self._move_all_to_download, width=17).pack(
            fill="x", pady=(6, 16)
        )
        ttk.Button(transfer, text="← Quitar", command=self._move_selected_to_available, width=17).pack(fill="x")
        ttk.Button(transfer, text="⇐ Quitar todas", command=self._move_all_to_available, width=17).pack(
            fill="x", pady=(6, 0)
        )

        selected_box = ttk.Frame(camera_box)
        selected_box.grid(row=1, column=2, sticky="nsew")
        selected_box.columnconfigure(0, weight=1)
        ttk.Label(selected_box, textvariable=self.selected_count, font=("Segoe UI Semibold", 9)).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.selected_camera_tree = self._camera_tree(selected_box)
        self._active_camera_tree = self.available_camera_tree
        self.available_camera_tree.bind("<Double-1>", lambda _event: self._move_selected_to_download())
        self.selected_camera_tree.bind("<Double-1>", lambda _event: self._move_selected_to_available())
        self.available_camera_tree.bind("<FocusIn>", lambda _event: self._set_active_camera_tree(self.available_camera_tree))
        self.selected_camera_tree.bind("<FocusIn>", lambda _event: self._set_active_camera_tree(self.selected_camera_tree))

        camera_buttons = ttk.Frame(camera_box)
        camera_buttons.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(9, 0))
        ttk.Button(camera_buttons, text="Probar NVR", command=self._test_nvr).pack(side="left")
        self.detect_button = ttk.Button(camera_buttons, text="Detectar cámaras", command=self._detect_cameras)
        self.detect_button.pack(side="left", padx=(6, 0))
        ttk.Button(camera_buttons, text="Agregar manualmente", command=self._add_camera).pack(side="left", padx=(6, 0))
        ttk.Button(camera_buttons, text="Editar", command=self._edit_camera).pack(side="left", padx=(6, 0))
        ttk.Button(camera_buttons, text="Eliminar del catálogo", command=self._remove_cameras).pack(side="left", padx=(6, 0))
        ttk.Button(
            camera_buttons,
            text="Continuar a destino →",
            command=lambda: self.notebook.select(self.settings_tab),
            style="Primary.TButton",
        ).pack(side="right")
        ttk.Label(camera_buttons, text="Doble clic también mueve una cámara.", style="Subtitle.TLabel").pack(
            side="right", padx=(0, 10)
        )

        cloud = ttk.LabelFrame(self.settings_tab, text="Destino Backblaze B2", padding=10)
        cloud.grid(row=0, column=0, sticky="ew")
        cloud_fields = [
            ("Endpoint", "endpoint", ""),
            ("Bucket", "bucket", ""),
            ("Prefijo", "prefix", "ivms"),
            ("Key ID", "key_id", ""),
            ("Application Key", "application_key", ""),
        ]
        self._field_grid(cloud, cloud_fields, password_names={"key_id", "application_key"})
        ttk.Button(cloud, text="Probar Backblaze", command=self._test_cloud).grid(row=2, column=4, padx=5, pady=6)

        job = ttk.LabelFrame(self.settings_tab, text="Rango y archivos temporales", padding=10)
        job.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for column in range(8):
            job.columnconfigure(column, weight=1 if column in {1, 3, 5, 7} else 0)
        ttk.Label(job, text="Fecha inicial").grid(row=0, column=0, sticky="w")
        self.start_date = self._date_widget(job)
        self.start_date.grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(job, text="Fecha final").grid(row=0, column=2, sticky="w")
        self.end_date = self._date_widget(job)
        self.end_date.grid(row=0, column=3, sticky="ew", padx=5)
        ttk.Label(job, text="Hora inicial").grid(row=0, column=4, sticky="w")
        ttk.Entry(job, textvariable=self._var("start_time", "07:00"), width=8).grid(row=0, column=5, sticky="ew", padx=5)
        ttk.Label(job, text="Hora final").grid(row=0, column=6, sticky="w")
        ttk.Entry(job, textvariable=self._var("end_time", "18:10"), width=8).grid(row=0, column=7, sticky="ew", padx=5)

        ttk.Label(job, text="Carpeta temporal").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(job, textvariable=self._var("spool_dir", str(Path.cwd() / "spool"))).grid(
            row=1, column=1, columnspan=5, sticky="ew", padx=5, pady=(8, 0)
        )
        ttk.Button(job, text="Examinar", command=self._choose_spool).grid(row=1, column=6, pady=(8, 0))
        ttk.Label(job, text="FFmpeg (carpeta)").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(job, textvariable=self._var("ffmpeg_dir", "")).grid(
            row=2, column=1, columnspan=5, sticky="ew", padx=5, pady=(8, 0)
        )
        ttk.Button(job, text="Examinar", command=self._choose_ffmpeg).grid(row=2, column=6, pady=(8, 0))
        self.space_label = ttk.Label(job, text="")
        self.space_label.grid(row=1, column=7, sticky="e")
        self.estimate_label = ttk.Label(job, text="Estimado: —")
        self.estimate_label.grid(row=2, column=7, sticky="e")
        ttk.Label(job, text="Se procesan únicamente lunes a viernes").grid(
            row=3, column=0, columnspan=8, sticky="w", pady=(8, 0)
        )

        controls = ttk.Frame(self.settings_tab)
        controls.grid(row=2, column=0, sticky="ew", pady=10)
        self.start_button = ttk.Button(
            controls, text="Iniciar transferencia", command=self._start, style="Primary.TButton"
        )
        self.start_button.pack(side="left")
        self.estimate_button = ttk.Button(controls, text="Estimar", command=self._estimate)
        self.estimate_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(controls, text="Cancelar", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(controls, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.current_status = ttk.Label(self.progress_tab, text="Listo", anchor="w")
        self.current_status.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.tree = ttk.Treeview(
            self.progress_tab, columns=("camera", "date", "status", "detail"), show="headings", height=9
        )
        self.tree.heading("camera", text="Cámara")
        self.tree.heading("date", text="Fecha")
        self.tree.heading("status", text="Estado")
        self.tree.heading("detail", text="Detalle")
        self.tree.column("camera", width=180)
        self.tree.column("date", width=100, anchor="center")
        self.tree.column("status", width=120, anchor="center")
        self.tree.column("detail", width=550)
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.progress_tab.rowconfigure(1, weight=1)
        self.log = tk.Text(self.progress_tab, height=7, state="disabled", wrap="word")
        self.log.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _camera_tree(self, parent) -> ttk.Treeview:
        tree = ttk.Treeview(
            parent, columns=("channel", "name"), show="headings", selectmode="extended", height=5
        )
        tree.heading("channel", text="Canal")
        tree.heading("name", text="Nombre")
        tree.column("channel", width=65, anchor="center", stretch=False)
        tree.column("name", width=280)
        tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        return tree

    def _field_grid(self, parent, fields, password_names=frozenset()):
        for column in range(6):
            parent.columnconfigure(column, weight=1 if column % 2 else 0)
        for index, (label, name, default) in enumerate(fields):
            row, pair = divmod(index, 3)
            column = pair * 2
            ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 4), pady=4)
            entry = ttk.Entry(parent, textvariable=self._var(name, default), show="•" if name in password_names else "")
            entry.grid(row=row, column=column + 1, sticky="ew", padx=(0, 12), pady=4)

    def _date_widget(self, parent):
        if DateEntry:
            return DateEntry(parent, date_pattern="yyyy-mm-dd")
        entry = ttk.Entry(parent)
        entry.insert(0, date.today().isoformat())
        return entry

    def _get_date(self, widget) -> date:
        if DateEntry and isinstance(widget, DateEntry):
            return widget.get_date()
        return date.fromisoformat(widget.get().strip())

    def _set_date(self, widget, value: str) -> None:
        if not value:
            return
        if DateEntry and isinstance(widget, DateEntry):
            widget.set_date(date.fromisoformat(value))
        else:
            widget.delete(0, "end")
            widget.insert(0, value)

    def _load(self) -> None:
        data = load_config()
        for name, variable in self.values.items():
            if name in data:
                variable.set(str(data[name]))
        self._set_date(self.start_date, data.get("start_date", ""))
        self._set_date(self.end_date, data.get("end_date", ""))
        nvr = load_secret(f"nvr:{self.values['host'].get()}")
        b2 = load_secret(f"b2:{self.values['endpoint'].get()}:{self.values['bucket'].get()}")
        self.values["nvr_password"].set(nvr.get("password", ""))
        self.values["key_id"].set(b2.get("key_id", ""))
        self.values["application_key"].set(b2.get("application_key", ""))
        for item in camera_entries(data):
            try:
                channel = int(item["channel"])
                name = str(item.get("camera_name") or f"Camera_{channel}")
                iid = str(channel)
                target = self.selected_camera_tree if item.get("selected", False) else self.available_camera_tree
                target.insert("", "end", iid=iid, values=(channel, name))
            except (KeyError, TypeError, ValueError, tk.TclError):
                continue
        self._update_camera_counts()
        self._update_space()

    def _save(self) -> None:
        data = {name: variable.get().strip() for name, variable in self.values.items() if name not in {"nvr_password", "key_id", "application_key"}}
        data["start_date"] = self._get_date(self.start_date).isoformat()
        data["end_date"] = self._get_date(self.end_date).isoformat()
        data["cameras"] = self._camera_records()
        data.pop("channel", None)
        data.pop("camera", None)
        save_config(data)
        save_secret(f"nvr:{data['host']}", {"password": self.values["nvr_password"].get()})
        save_secret(
            f"b2:{data['endpoint']}:{data['bucket']}",
            {"key_id": self.values["key_id"].get(), "application_key": self.values["application_key"].get()},
        )

    def _device_config(self) -> DeviceConfig:
        device = DeviceConfig(
            host=self.values["host"].get().strip(),
            http_port=int(self.values["http_port"].get()),
            rtsp_port=int(self.values["rtsp_port"].get()),
            username=self.values["nvr_user"].get().strip(),
            utc_offset=self.values["utc_offset"].get().strip(),
        )
        if not all([device.host, device.username]):
            raise ValueError("Complete host y usuario")
        if not self.values["nvr_password"].get():
            raise ValueError("Complete la contraseña del NVR")
        return device

    def _selected_cameras(self) -> list[CameraConfig]:
        cameras: list[CameraConfig] = []
        for iid in self.selected_camera_tree.get_children():
            raw_channel, raw_name = self.selected_camera_tree.item(iid, "values")
            channel = int(raw_channel)
            name = str(raw_name).strip()
            if channel <= 0:
                raise ValueError("Los canales deben ser enteros positivos")
            if not name:
                raise ValueError(f"Complete el nombre de la cámara del canal {channel}")
            cameras.append(CameraConfig(channel, name))
        validate_cameras(cameras)
        return cameras

    def _cloud_config(self) -> CloudConfig:
        cloud = CloudConfig(
            endpoint=normalize_endpoint(self.values["endpoint"].get()),
            bucket=self.values["bucket"].get().strip(),
            prefix=self.values["prefix"].get().strip(),
        )
        if not cloud.bucket:
            raise ValueError("Complete el nombre del bucket")
        if not self.values["key_id"].get() or not self.values["application_key"].get():
            raise ValueError("Complete Key ID y Application Key")
        return cloud

    def _configs(self, require_media: bool = True):
        device = self._device_config()
        cameras = self._selected_cameras()
        cloud = self._cloud_config()
        start_time = datetime.strptime(self.values["start_time"].get().strip(), "%H:%M").time()
        end_time = datetime.strptime(self.values["end_time"].get().strip(), "%H:%M").time()
        common = {
            "device": device,
            "cloud": cloud,
            "start_date": self._get_date(self.start_date),
            "end_date": self._get_date(self.end_date),
            "start_time": start_time,
            "end_time": end_time,
            "spool_dir": Path(self.values["spool_dir"].get()).expanduser().resolve(),
        }
        jobs = [JobConfig(camera=camera, **common) for camera in cameras]
        jobs[0].windows()
        if require_media:
            find_binary("ffmpeg", self.values["ffmpeg_dir"].get().strip())
            find_binary("ffprobe", self.values["ffmpeg_dir"].get().strip())
        return device, cloud, jobs

    def _background(self, action) -> None:
        if self.worker and self.worker.is_alive():
            return
        def wrapped():
            try:
                result = action()
                self.events.put(("message", "Operación correcta", str(result)))
            except Exception as exc:
                self.events.put(("error", "Error", str(exc)))
        self.worker = threading.Thread(target=wrapped, daemon=True)
        self.worker.start()

    def _test_nvr(self) -> None:
        try:
            device = self._device_config()
            self._save()
        except Exception as exc:
            messagebox.showerror("Configuración", str(exc))
            return
        password = self.values["nvr_password"].get()
        self._background(lambda: IsapiClient(device, password).test_connection())

    def _detect_cameras(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            device = self._device_config()
            self._save()
        except Exception as exc:
            messagebox.showerror("Configuración", str(exc))
            return
        self.detect_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.estimate_button.configure(state="disabled")
        password = self.values["nvr_password"].get()

        def run():
            try:
                cameras = IsapiClient(device, password).list_cameras()
                self.events.put(("cameras_detected", cameras))
            except Exception as exc:
                self.events.put(("camera_detection_error", str(exc)))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _add_camera(self) -> None:
        channel = simpledialog.askinteger("Agregar cámara", "Canal:", parent=self, minvalue=1)
        if channel is None:
            return
        if self._tree_for_channel(channel) is not None:
            messagebox.showerror("Cámaras", f"El canal {channel} ya existe")
            return
        name = simpledialog.askstring("Agregar cámara", "Nombre:", initialvalue=f"Camera_{channel}", parent=self)
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showerror("Cámaras", "El nombre es obligatorio")
            return
        iid = str(channel)
        self.available_camera_tree.insert("", "end", iid=iid, values=(channel, name))
        self.available_camera_tree.selection_set(iid)
        self.available_camera_tree.focus_set()
        self._update_camera_counts()
        self._persist_camera_catalog()

    def _edit_camera(self) -> None:
        tree = self._active_camera_tree
        selection = tree.selection() if tree is not None else ()
        if len(selection) != 1:
            messagebox.showinfo("Cámaras", "Haz clic en una cámara y selecciona exactamente una fila para editar")
            return
        iid = selection[0]
        old_channel, old_name = tree.item(iid, "values")
        channel = simpledialog.askinteger(
            "Editar cámara", "Canal:", initialvalue=int(old_channel), parent=self, minvalue=1
        )
        if channel is None:
            return
        new_iid = str(channel)
        if new_iid != iid and self._tree_for_channel(channel) is not None:
            messagebox.showerror("Cámaras", f"El canal {channel} ya existe")
            return
        name = simpledialog.askstring("Editar cámara", "Nombre:", initialvalue=old_name, parent=self)
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showerror("Cámaras", "El nombre es obligatorio")
            return
        position = tree.index(iid)
        tree.delete(iid)
        tree.insert("", position, iid=new_iid, values=(channel, name))
        tree.selection_set(new_iid)
        self._update_camera_counts()
        self._persist_camera_catalog()

    def _remove_cameras(self) -> None:
        tree = self._active_camera_tree
        selection = tree.selection() if tree is not None else ()
        if not selection:
            messagebox.showinfo("Cámaras", "Selecciona una o varias cámaras para eliminarlas del catálogo")
            return
        for iid in selection:
            tree.delete(iid)
        self._update_camera_counts()
        self._persist_camera_catalog()

    def _set_active_camera_tree(self, tree: ttk.Treeview) -> None:
        self._active_camera_tree = tree

    def _tree_for_channel(self, channel: int) -> ttk.Treeview | None:
        iid = str(channel)
        for tree in (self.available_camera_tree, self.selected_camera_tree):
            if tree.exists(iid):
                return tree
        return None

    def _move_camera_items(self, source: ttk.Treeview, target: ttk.Treeview, iids) -> None:
        moved = []
        for iid in tuple(iids):
            if not source.exists(iid):
                continue
            values = source.item(iid, "values")
            source.delete(iid)
            target.insert("", "end", iid=iid, values=values)
            moved.append(iid)
        if moved:
            target.selection_set(moved)
            target.see(moved[-1])
            target.focus_set()
            self._active_camera_tree = target
        self._update_camera_counts()
        if moved:
            self._persist_camera_catalog()

    def _move_selected_to_download(self) -> None:
        self._move_camera_items(
            self.available_camera_tree, self.selected_camera_tree, self.available_camera_tree.selection()
        )

    def _move_all_to_download(self) -> None:
        self._move_camera_items(
            self.available_camera_tree, self.selected_camera_tree, self.available_camera_tree.get_children()
        )

    def _move_selected_to_available(self) -> None:
        self._move_camera_items(
            self.selected_camera_tree, self.available_camera_tree, self.selected_camera_tree.selection()
        )

    def _move_all_to_available(self) -> None:
        self._move_camera_items(
            self.selected_camera_tree, self.available_camera_tree, self.selected_camera_tree.get_children()
        )

    def _update_camera_counts(self) -> None:
        self.available_count.set(f"Disponibles ({len(self.available_camera_tree.get_children())})")
        self.selected_count.set(f"Para descargar ({len(self.selected_camera_tree.get_children())})")

    def _camera_records(self) -> list[dict]:
        records = []
        for tree, selected in ((self.available_camera_tree, False), (self.selected_camera_tree, True)):
            records.extend(
                {
                    "channel": int(tree.item(iid, "values")[0]),
                    "camera_name": str(tree.item(iid, "values")[1]),
                    "selected": selected,
                }
                for iid in tree.get_children()
            )
        return records

    def _persist_camera_catalog(self) -> None:
        data = load_config()
        data["cameras"] = self._camera_records()
        data.pop("channel", None)
        data.pop("camera", None)
        try:
            save_config(data)
        except OSError:
            pass

    def _apply_detected_cameras(self, detected: list[CameraConfig]) -> None:
        previous = {}
        for tree, selected in (
            (self.available_camera_tree, False),
            (self.selected_camera_tree, True),
        ):
            for iid in tree.get_children():
                channel, name = tree.item(iid, "values")
                previous[int(channel)] = (str(name), selected)
            tree.delete(*tree.get_children())
        detected_channels = set()
        for camera in detected:
            detected_channels.add(camera.channel)
            name, was_selected = previous.get(camera.channel, (camera.camera_name, False))
            target = self.selected_camera_tree if was_selected else self.available_camera_tree
            target.insert("", "end", iid=str(camera.channel), values=(camera.channel, name))
        for channel, (name, was_selected) in previous.items():
            if channel in detected_channels:
                continue
            target = self.selected_camera_tree if was_selected else self.available_camera_tree
            target.insert("", "end", iid=str(channel), values=(channel, name))
        self._update_camera_counts()

    @staticmethod
    def _task_iid(camera: CameraConfig, day: str) -> str:
        return f"{camera.channel}|{day}"

    def _test_cloud(self) -> None:
        try:
            cloud = self._cloud_config()
            self._save()
        except Exception as exc:
            messagebox.showerror("Configuración", str(exc))
            return
        key_id = self.values["key_id"].get()
        application_key = self.values["application_key"].get()
        self._background(lambda: B2MultipartClient(cloud, key_id, application_key).test_connection())

    def _estimate(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            device, _, jobs = self._configs(require_media=False)
            self._save()
        except Exception as exc:
            messagebox.showerror("Configuración", str(exc))
            return
        password = self.values["nvr_password"].get()
        self.notebook.select(self.progress_tab)
        self.tree.delete(*self.tree.get_children())
        self.estimate_button.configure(state="disabled")
        self.start_button.configure(state="disabled")

        def estimate_action():
            client = IsapiClient(device, password)
            total = 0
            for job in jobs:
                for window in job.windows():
                    segments = client.search_recordings(window.start, window.end, job.camera.channel)
                    reported = sum(item.size or 0 for item in segments)
                    fallback = int(sum((item.end - item.start).total_seconds() for item in segments) * 16_000_000 / 8)
                    size = reported or fallback
                    total += size
                    self.events.put(
                        ("estimate_row", job.camera, window.day.isoformat(), size, len(segments), not bool(reported) and bool(segments))
                    )
            self.events.put(("estimate_done", total))

        def wrapped():
            try:
                estimate_action()
            except Exception as exc:
                self.events.put(("estimate_error", str(exc)))
        self.worker = threading.Thread(target=wrapped, daemon=True)
        self.worker.start()

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            _, _, jobs = self._configs()
            self._save()
        except Exception as exc:
            messagebox.showerror("Configuración inválida", str(exc))
            return
        first = jobs[0]
        camera_names = ", ".join(f"{job.camera.camera_name} (canal {job.camera.channel})" for job in jobs)
        day_count = len(first.windows())
        text = (
            f"Cámaras ({len(jobs)}): {camera_names}\n"
            f"Rango: {first.start_date} a {first.end_date}, {first.start_time:%H:%M}-{first.end_time:%H:%M}\n"
            f"Días laborales: {day_count} (se omiten sábados y domingos)\n"
            f"Elementos: {len(jobs) * day_count}\n"
            f"Bucket: {first.cloud.bucket}\n\n¿Iniciar descarga y subida?"
        )
        if not messagebox.askyesno("Confirmar trabajo", text):
            return
        self.notebook.select(self.progress_tab)
        self.tree.delete(*self.tree.get_children())
        task_iids = []
        for job in jobs:
            for window in job.windows():
                day = window.day.isoformat()
                iid = self._task_iid(job.camera, day)
                task_iids.append(iid)
                self.tree.insert("", "end", iid=iid, values=(job.camera.camera_name, day, "pendiente", ""))
        self._task_order = {iid: index for index, iid in enumerate(task_iids)}
        self.cancel_event = threading.Event()
        nvr_password = self.values["nvr_password"].get()
        key_id = self.values["key_id"].get()
        application_key = self.values["application_key"].get()
        ffmpeg_dir = self.values["ffmpeg_dir"].get().strip()
        self.start_button.configure(state="disabled")
        self.estimate_button.configure(state="disabled")
        self.detect_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")

        def callback(camera, day, detail, value):
            iid = self._task_iid(camera, day)
            local_value = 0.0 if value is None else value
            overall = (self._task_order[iid] + local_value) / len(self._task_order)
            self.events.put(("progress", iid, camera.camera_name, day, detail, value, overall))

        def run():
            try:
                runner = BatchJobRunner(
                    jobs,
                    nvr_password,
                    key_id,
                    application_key,
                    ffmpeg_dir,
                    callback,
                    self.cancel_event,
                )
                result = runner.run()
                self.events.put(("done", result))
            except Exception as exc:
                self.events.put(("fatal", str(exc)))
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        self.cancel_event.set()
        self._append_log("Cancelación solicitada; se conservarán temporales y multipart.")

    def _choose_spool(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.values["spool_dir"].get() or str(Path.cwd()))
        if selected:
            self.values["spool_dir"].set(selected)
            self._update_space()

    def _choose_ffmpeg(self) -> None:
        selected = filedialog.askdirectory()
        if selected:
            self.values["ffmpeg_dir"].set(selected)

    def _update_space(self) -> None:
        try:
            path = Path(self.values["spool_dir"].get())
            probe = path if path.exists() else path.parent
            free = shutil.disk_usage(probe).free / 1024**3
            self.space_label.configure(text=f"Libre: {free:.2f} GiB")
        except Exception:
            self.space_label.configure(text="Libre: —")

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    _, iid, camera_name, day, detail, value, overall = event
                    status = detail.split(" ", 1)[0].lower()
                    if self.tree.exists(iid):
                        self.tree.item(iid, values=(camera_name, day, status, detail))
                    self.progress["value"] = overall * 100
                    self.current_status.configure(text=f"{camera_name} · {day}: {detail}")
                    now = time.monotonic()
                    if value in {None, 0.0, 1.0} or now - self._last_log_at.get(iid, 0) >= 30:
                        self._append_log(f"{camera_name} · {day}: {detail}")
                        self._last_log_at[iid] = now
                elif kind == "message":
                    messagebox.showinfo(event[1], event[2])
                elif kind == "error":
                    messagebox.showerror(event[1], event[2])
                elif kind == "done":
                    self.start_button.configure(state="normal")
                    self.estimate_button.configure(state="normal")
                    self.detect_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.current_status.configure(text="Trabajo finalizado")
                    messagebox.showinfo("Trabajo finalizado", self._result_summary(event[1]))
                elif kind == "fatal":
                    self.start_button.configure(state="normal")
                    self.estimate_button.configure(state="normal")
                    self.detect_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    messagebox.showerror("Error", event[1])
                elif kind == "estimate_row":
                    camera, day, size, count, fallback = event[1:]
                    estimate_type = "estimación conservadora" if fallback else "informado por NVR"
                    detail = f"{size / 1024**3:.2f} GiB en {count} segmento(s), {estimate_type}" if size else "Sin grabación"
                    iid = self._task_iid(camera, day)
                    values = (camera.camera_name, day, "estimado", detail)
                    if self.tree.exists(iid):
                        self.tree.item(iid, values=values)
                    else:
                        self.tree.insert("", "end", iid=iid, values=values)
                elif kind == "estimate_done":
                    total = event[1]
                    self.estimate_label.configure(text=f"Estimado: {total / 1024**3:.2f} GiB")
                    self.estimate_button.configure(state="normal")
                    self.start_button.configure(state="normal")
                elif kind == "estimate_error":
                    self.estimate_button.configure(state="normal")
                    self.start_button.configure(state="normal")
                    messagebox.showerror("Estimación", event[1])
                elif kind == "cameras_detected":
                    self.detect_button.configure(state="normal")
                    self.start_button.configure(state="normal")
                    self.estimate_button.configure(state="normal")
                    detected = event[1]
                    self._apply_detected_cameras(detected)
                    self._save()
                    self.current_status.configure(
                        text=f"Se detectaron {len(detected)} cámara(s). Muévelas a 'Para descargar'."
                    )
                elif kind == "camera_detection_error":
                    self.detect_button.configure(state="normal")
                    self.start_button.configure(state="normal")
                    self.estimate_button.configure(state="normal")
                    messagebox.showerror(
                        "Detección de cámaras",
                        event[1] + "\n\nPuede usar Agregar para registrar los canales manualmente.",
                    )
        except queue.Empty:
            pass
        self.after(150, self._poll)

    @staticmethod
    def _result_summary(result: dict) -> str:
        lines = [
            f"Completados: {result.get('completed', 0)}",
            f"Sin grabación: {result.get('empty', 0)}",
            f"Errores: {result.get('errors', 0)}",
            f"Cancelados: {result.get('cancelled', 0)}",
        ]
        for item in result.get("by_camera", {}).values():
            lines.append(
                f"{item.get('name', 'Cámara')}: {item.get('completed', 0)} completados, "
                f"{item.get('empty', 0)} sin grabación, {item.get('errors', 0)} errores, "
                f"{item.get('cancelled', 0)} cancelados"
            )
        return "\n".join(lines)

    def _append_log(self, text: str) -> None:
        timestamped = f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}"
        self.log.configure(state="normal")
        self.log.insert("end", timestamped + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        try:
            directory = app_dir()
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "activity.log").open("a", encoding="utf-8") as handle:
                handle.write(timestamped + "\n")
        except OSError:
            pass

    def _on_close(self) -> None:
        running = self.worker is not None and self.worker.is_alive() and str(self.start_button["state"]) == "disabled"
        if running:
            if not messagebox.askyesno(
                "Cancelar y cerrar",
                "Hay un trabajo activo. Se cancelará de forma segura y se conservarán los segmentos descargados. ¿Cerrar?",
            ):
                return
            self.cancel_event.set()
            self.current_status.configure(text="Cancelando; espere unos segundos…")
            self.after(500, self._wait_then_close)
            return
        self.destroy()

    def _wait_then_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.after(500, self._wait_then_close)
        else:
            self.destroy()


def main() -> None:
    mutex = None
    if __import__("os").name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        mutex = kernel32.CreateMutexW(None, False, "Local\\IVMSBackblazeDownloader")
        already_exists = ctypes.get_last_error() == 183 or kernel32.GetLastError() == 183
        if already_exists:
            found_window = False
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            @callback_type
            def find_window(hwnd, _lparam):
                nonlocal found_window
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                if title.value == "iVMS → Backblaze B2":
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    found_window = True
                    return False
                return True

            user32.EnumWindows(find_window, 0)
            if found_window:
                kernel32.CloseHandle(mutex)
                return
            # Un mutex sin una ventana visible suele pertenecer a un proceso
            # huérfano. No debe impedir que el usuario recupere la aplicación.
    try:
        App().mainloop()
    finally:
        if mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)
