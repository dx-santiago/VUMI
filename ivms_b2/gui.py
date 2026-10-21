from __future__ import annotations

import os
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

from .audit import AuditRecord, load_audit_records
from .cloud import B2MultipartClient, normalize_endpoint, validate_cameras
from .config import app_dir, delete_secret, load_config, load_secret, save_config, save_secret
from .isapi import IsapiClient
from .media import find_binary
from .models import CameraConfig, CloudConfig, DeviceConfig, JobConfig
from .profiles import load_profiles, new_profile, public_profiles
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
        self.dvr_profiles: list[dict] = []
        self.active_profile_id = ""
        self.profile_choice = tk.StringVar()
        self.profile_name = tk.StringVar()
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
        self.audit_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.camera_tab, text="1. Cámaras")
        self.notebook.add(self.settings_tab, text="2. Destino y rango")
        self.notebook.add(self.progress_tab, text="3. Progreso")
        self.notebook.add(self.audit_tab, text="4. Auditoría")
        for tab in (self.camera_tab, self.settings_tab, self.progress_tab, self.audit_tab):
            tab.columnconfigure(0, weight=1)

        nvr = ttk.LabelFrame(self.camera_tab, text="Baúl de DVRs", padding=10)
        nvr.grid(row=0, column=0, sticky="new")
        nvr.columnconfigure(0, weight=1)

        vault = ttk.Frame(nvr)
        vault.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        vault.columnconfigure(1, weight=1)
        ttk.Label(vault, text="DVR guardado").grid(row=0, column=0, sticky="w")
        self.profile_combo = ttk.Combobox(vault, textvariable=self.profile_choice, state="readonly", width=28)
        self.profile_combo.grid(row=0, column=1, sticky="ew", padx=(6, 12))
        self.profile_combo.bind("<<ComboboxSelected>>", self._switch_profile)
        ttk.Label(vault, text="Nombre").grid(row=0, column=2, sticky="w")
        ttk.Entry(vault, textvariable=self.profile_name, width=24).grid(row=0, column=3, padx=(6, 12))
        ttk.Button(vault, text="Nuevo", command=self._new_profile).grid(row=0, column=4)
        ttk.Button(vault, text="Guardar DVR", command=self._save_profile).grid(row=0, column=5, padx=(6, 0))
        ttk.Button(vault, text="Eliminar", command=self._delete_profile).grid(row=0, column=6, padx=(6, 0))

        connection = ttk.Frame(nvr)
        connection.grid(row=1, column=0, sticky="ew")
        fields = [
            ("IP / host", "host", ""),
            ("Puerto HTTP", "http_port", "80"),
            ("Puerto RTSP", "rtsp_port", "554"),
            ("Usuario", "nvr_user", "admin"),
            ("Contraseña", "nvr_password", ""),
            ("Zona UTC", "utc_offset", "-05:00"),
        ]
        self._field_grid(connection, fields, password_names={"nvr_password"})
        camera_box = ttk.LabelFrame(nvr, text="Selección de cámaras", padding=8)
        camera_box.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        camera_box.columnconfigure(0, weight=1)
        camera_box.columnconfigure(2, weight=1)
        ttk.Label(
            camera_box,
            text="Selecciona cámaras de este DVR. La selección de los demás perfiles se conserva para el lote centralizado.",
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
        self._build_audit_tab()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_audit_tab(self) -> None:
        filters = ttk.LabelFrame(self.audit_tab, text="Consulta de transferencias", padding=9)
        filters.grid(row=0, column=0, sticky="ew")
        filters.columnconfigure(1, weight=1)

        self.audit_search = tk.StringVar()
        self.audit_status = tk.StringVar(value="Todos")
        ttk.Label(filters, text="Buscar").grid(row=0, column=0, sticky="w")
        search = ttk.Entry(filters, textvariable=self.audit_search)
        search.grid(row=0, column=1, sticky="ew", padx=(6, 12))
        ttk.Label(filters, text="Estado").grid(row=0, column=2, sticky="w")
        status = ttk.Combobox(
            filters,
            textvariable=self.audit_status,
            values=("Todos", "Completado", "Sin grabación", "Error", "Cancelado", "Pendiente", "En proceso"),
            width=17,
            state="readonly",
        )
        status.grid(row=0, column=3, padx=(6, 12))
        ttk.Button(filters, text="Actualizar", command=self._refresh_audit).grid(row=0, column=4)
        ttk.Button(filters, text="Abrir registro", command=self._open_activity_log).grid(row=0, column=5, padx=(6, 0))
        ttk.Button(filters, text="Abrir temporales", command=self._open_spool_dir).grid(row=0, column=6, padx=(6, 0))

        self.audit_summary = tk.StringVar(value="Sin registros cargados")
        ttk.Label(self.audit_tab, textvariable=self.audit_summary, style="Subtitle.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 5)
        )

        table = ttk.Frame(self.audit_tab)
        table.grid(row=2, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.audit_tree = ttk.Treeview(
            table,
            columns=("timestamp", "camera", "day", "status", "size", "detail"),
            show="headings",
            selectmode="browse",
        )
        headings = {
            "timestamp": "Última actualización",
            "camera": "Cámara",
            "day": "Fecha grabada",
            "status": "Estado",
            "size": "Tamaño",
            "detail": "Detalle",
        }
        for name, label in headings.items():
            self.audit_tree.heading(name, text=label)
        self.audit_tree.column("timestamp", width=150, anchor="center", stretch=False)
        self.audit_tree.column("camera", width=150)
        self.audit_tree.column("day", width=105, anchor="center", stretch=False)
        self.audit_tree.column("status", width=115, anchor="center", stretch=False)
        self.audit_tree.column("size", width=85, anchor="e", stretch=False)
        self.audit_tree.column("detail", width=410)
        self.audit_tree.tag_configure("Completado", foreground="#18733b")
        self.audit_tree.tag_configure("Sin grabación", foreground="#5b6573")
        self.audit_tree.tag_configure("Error", foreground="#b42318")
        self.audit_tree.tag_configure("Cancelado", foreground="#a15c00")
        self.audit_tree.tag_configure("Pendiente", foreground="#5b6573")
        self.audit_tree.tag_configure("En proceso", foreground="#005ea8")
        self.audit_tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.audit_tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.audit_tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.audit_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)

        detail_box = ttk.LabelFrame(self.audit_tab, text="Detalle del registro", padding=7)
        detail_box.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        detail_box.columnconfigure(0, weight=1)
        self.audit_detail = tk.Text(detail_box, height=7, state="disabled", wrap="word")
        self.audit_detail.grid(row=0, column=0, sticky="ew")
        self.audit_tab.rowconfigure(2, weight=1)
        self.audit_records: list[AuditRecord] = []
        self.audit_rows: dict[str, AuditRecord] = {}
        self.audit_search.trace_add("write", lambda *_args: self._apply_audit_filters())
        status.bind("<<ComboboxSelected>>", lambda _event: self._apply_audit_filters())
        self.audit_tree.bind("<<TreeviewSelect>>", self._show_audit_detail)
        self.notebook.bind("<<NotebookTabChanged>>", self._audit_tab_selected, add=True)

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
            if name in data and name not in {"host", "http_port", "rtsp_port", "nvr_user", "nvr_password", "utc_offset"}:
                variable.set(str(data[name]))
        self._set_date(self.start_date, data.get("start_date", ""))
        self._set_date(self.end_date, data.get("end_date", ""))
        b2 = load_secret(f"b2:{self.values['endpoint'].get()}:{self.values['bucket'].get()}")
        self.values["key_id"].set(b2.get("key_id", ""))
        self.values["application_key"].set(b2.get("application_key", ""))
        self.dvr_profiles = load_profiles(data)
        requested = str(data.get("active_dvr_profile") or "")
        self.active_profile_id = requested if any(item["id"] == requested for item in self.dvr_profiles) else self.dvr_profiles[0]["id"]
        self._refresh_profile_combo()
        self._load_profile(self.active_profile_id)
        self._update_space()
        self._refresh_audit()

    def _profile(self, profile_id: str | None = None) -> dict:
        target = profile_id or self.active_profile_id
        for profile in self.dvr_profiles:
            if profile["id"] == target:
                return profile
        raise ValueError("Seleccione un DVR guardado")

    def _refresh_profile_combo(self) -> None:
        names = [profile["name"] for profile in self.dvr_profiles]
        self.profile_combo.configure(values=names)
        if self.active_profile_id:
            self.profile_choice.set(self._profile()["name"])

    def _load_profile(self, profile_id: str) -> None:
        profile = self._profile(profile_id)
        self.active_profile_id = profile["id"]
        self.profile_choice.set(profile["name"])
        self.profile_name.set(profile["name"])
        values = {
            "host": profile["host"],
            "http_port": profile["http_port"],
            "rtsp_port": profile["rtsp_port"],
            "nvr_user": profile["username"],
            "utc_offset": profile["utc_offset"],
        }
        for name, value in values.items():
            self.values[name].set(str(value))
        secret = load_secret(f"dvr:{profile['id']}")
        if not secret and profile["host"]:
            secret = load_secret(f"nvr:{profile['host']}")
        self.values["nvr_password"].set(secret.get("password", ""))
        self.available_camera_tree.delete(*self.available_camera_tree.get_children())
        self.selected_camera_tree.delete(*self.selected_camera_tree.get_children())
        for item in profile.get("cameras", []):
            channel = int(item["channel"])
            target = self.selected_camera_tree if item.get("selected", False) else self.available_camera_tree
            target.insert("", "end", iid=str(channel), values=(channel, item["camera_name"]))
        self._update_camera_counts()

    def _sync_active_cameras(self) -> None:
        if not self.active_profile_id:
            return
        self._profile()["cameras"] = self._camera_records()

    def _capture_active_profile(self, validate: bool = False, save_password: bool = True) -> dict:
        profile = self._profile()
        self._sync_active_cameras()
        name = self.profile_name.get().strip()
        if not name:
            raise ValueError("Asigne un nombre al DVR")
        if any(item["id"] != profile["id"] and item["name"].casefold() == name.casefold() for item in self.dvr_profiles):
            raise ValueError("Ya existe un DVR guardado con ese nombre")
        if profile.get("use_namespace", True) and not profile.get("storage_name"):
            profile["storage_name"] = name
        profile.update(
            {
                "name": name,
                "host": self.values["host"].get().strip(),
                "http_port": self.values["http_port"].get().strip(),
                "rtsp_port": self.values["rtsp_port"].get().strip(),
                "username": self.values["nvr_user"].get().strip(),
                "utc_offset": self.values["utc_offset"].get().strip(),
            }
        )
        if validate:
            self._device_from_profile(profile)
            if not self.values["nvr_password"].get():
                raise ValueError(f"Complete la contraseña de {name}")
        if save_password and self.values["nvr_password"].get():
            save_secret(f"dvr:{profile['id']}", {"password": self.values["nvr_password"].get()})
        self._refresh_profile_combo()
        return profile

    def _persist_profiles(self) -> None:
        data = load_config()
        data["dvr_profiles"] = public_profiles(self.dvr_profiles)
        data["active_dvr_profile"] = self.active_profile_id
        data.pop("cameras", None)
        data.pop("channel", None)
        data.pop("camera", None)
        save_config(data)

    def _switch_profile(self, _event=None) -> None:
        if self.worker and self.worker.is_alive():
            self._refresh_profile_combo()
            messagebox.showinfo("Baúl de DVRs", "Espera a que termine la operación activa para cambiar de DVR.")
            return
        selected_name = self.profile_choice.get()
        target = next((item for item in self.dvr_profiles if item["name"] == selected_name), None)
        if target is None or target["id"] == self.active_profile_id:
            return
        try:
            self._capture_active_profile(save_password=True)
            self._persist_profiles()
            self._load_profile(target["id"])
            self._persist_profiles()
        except Exception as exc:
            self._refresh_profile_combo()
            messagebox.showerror("Baúl de DVRs", str(exc))

    def _new_profile(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Baúl de DVRs", "Espera a que termine la operación activa para crear otro DVR.")
            return
        try:
            self._capture_active_profile(save_password=True)
        except Exception:
            self._sync_active_cameras()
        existing = {item["name"].casefold() for item in self.dvr_profiles}
        number = 1
        while f"dvr {number}" in existing:
            number += 1
        profile = new_profile(f"DVR {number}")
        self.dvr_profiles.append(profile)
        self.active_profile_id = profile["id"]
        self._refresh_profile_combo()
        self._load_profile(profile["id"])
        self._persist_profiles()

    def _save_profile(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Baúl de DVRs", "Espera a que termine la operación activa para guardar cambios.")
            return
        try:
            profile = self._capture_active_profile(validate=True, save_password=True)
            self._persist_profiles()
            messagebox.showinfo("Baúl de DVRs", f"{profile['name']} quedó guardado.")
        except Exception as exc:
            messagebox.showerror("Baúl de DVRs", str(exc))

    def _delete_profile(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Baúl de DVRs", "Espera a que termine la operación activa para eliminar un DVR.")
            return
        profile = self._profile()
        if not messagebox.askyesno("Eliminar DVR", f"¿Eliminar '{profile['name']}' y su catálogo de cámaras?"):
            return
        delete_secret(f"dvr:{profile['id']}")
        self.dvr_profiles = [item for item in self.dvr_profiles if item["id"] != profile["id"]]
        if not self.dvr_profiles:
            self.dvr_profiles.append(new_profile("DVR 1"))
        self.active_profile_id = self.dvr_profiles[0]["id"]
        self._refresh_profile_combo()
        self._load_profile(self.active_profile_id)
        self._persist_profiles()

    def _audit_paths(self) -> tuple[Path, Path]:
        log_path = app_dir() / "activity.log"
        spool_value = self.values.get("spool_dir")
        spool_dir = Path(spool_value.get()).expanduser() if spool_value and spool_value.get().strip() else Path.cwd() / "spool"
        return log_path, spool_dir

    def _refresh_audit(self) -> None:
        try:
            log_path, spool_dir = self._audit_paths()
            self.audit_records = load_audit_records(log_path, spool_dir)
            self._apply_audit_filters()
        except Exception as exc:
            self.audit_records = []
            self._apply_audit_filters()
            self._set_audit_detail(f"No fue posible cargar la auditoría:\n{exc}")

    def _apply_audit_filters(self) -> None:
        if not hasattr(self, "audit_tree"):
            return
        query = self.audit_search.get().strip().casefold()
        status = self.audit_status.get()
        visible = []
        for record in self.audit_records:
            searchable = " ".join(
                (record.camera, record.day, record.status, record.detail, record.object_key, record.error)
            ).casefold()
            if query and query not in searchable:
                continue
            if status != "Todos" and record.status != status:
                continue
            visible.append(record)

        self.audit_tree.delete(*self.audit_tree.get_children())
        self.audit_rows.clear()
        for index, record in enumerate(visible):
            iid = f"audit-{index}"
            self.audit_rows[iid] = record
            self.audit_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    record.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    record.camera,
                    record.day,
                    record.status,
                    record.size_text,
                    record.detail,
                ),
                tags=(record.status,),
            )

        counts = {name: sum(item.status == name for item in visible) for name in ("Completado", "Sin grabación", "Error", "Cancelado")}
        latest = visible[0].timestamp.strftime("%Y-%m-%d %H:%M:%S") if visible else "—"
        self.audit_summary.set(
            f"Mostrados: {len(visible)}  ·  Completados: {counts['Completado']}  ·  "
            f"Sin grabación: {counts['Sin grabación']}  ·  Errores: {counts['Error']}  ·  "
            f"Cancelados: {counts['Cancelado']}  ·  Último registro: {latest}"
        )
        if visible:
            first = self.audit_tree.get_children()[0]
            self.audit_tree.selection_set(first)
            self.audit_tree.focus(first)
            self._show_audit_detail()
        else:
            self._set_audit_detail("No hay registros que coincidan con los filtros.")

    def _show_audit_detail(self, _event=None) -> None:
        selection = self.audit_tree.selection()
        if not selection:
            return
        record = self.audit_rows.get(selection[0])
        if record is None:
            return
        lines = [
            f"Última actualización: {record.timestamp:%Y-%m-%d %H:%M:%S}",
            f"Cámara: {record.camera}",
            f"Fecha grabada: {record.day}",
            f"Estado: {record.status}",
            f"Tamaño: {record.size_text}",
            f"Detalle: {record.detail or '—'}",
            f"Ruta del objeto: {record.object_key or '—'}",
            f"SHA-256: {record.sha256 or '—'}",
            f"Error: {record.error or '—'}",
            f"Fuente local: {record.source or '—'}",
        ]
        self._set_audit_detail("\n".join(lines))

    def _set_audit_detail(self, value: str) -> None:
        self.audit_detail.configure(state="normal")
        self.audit_detail.delete("1.0", "end")
        self.audit_detail.insert("1.0", value)
        self.audit_detail.configure(state="disabled")

    def _audit_tab_selected(self, _event=None) -> None:
        if self.notebook.select() == str(self.audit_tab):
            self._refresh_audit()

    def _open_activity_log(self) -> None:
        log_path, _ = self._audit_paths()
        if not log_path.is_file():
            messagebox.showinfo("Auditoría", "Todavía no existe un registro de actividad.")
            return
        try:
            os.startfile(log_path)
        except OSError as exc:
            messagebox.showerror("Auditoría", f"No fue posible abrir el registro:\n{exc}")

    def _open_spool_dir(self) -> None:
        _, spool_dir = self._audit_paths()
        if not spool_dir.is_dir():
            messagebox.showinfo("Auditoría", "La carpeta temporal configurada no existe.")
            return
        try:
            os.startfile(spool_dir)
        except OSError as exc:
            messagebox.showerror("Auditoría", f"No fue posible abrir la carpeta temporal:\n{exc}")

    def _save(self, validate_active: bool = True) -> None:
        self._capture_active_profile(validate=validate_active, save_password=True)
        device_fields = {"host", "http_port", "rtsp_port", "nvr_user", "nvr_password", "utc_offset"}
        data = {
            name: variable.get().strip()
            for name, variable in self.values.items()
            if name not in device_fields | {"key_id", "application_key"}
        }
        data["start_date"] = self._get_date(self.start_date).isoformat()
        data["end_date"] = self._get_date(self.end_date).isoformat()
        data["dvr_profiles"] = public_profiles(self.dvr_profiles)
        data["active_dvr_profile"] = self.active_profile_id
        save_config(data)
        save_secret(
            f"b2:{data['endpoint']}:{data['bucket']}",
            {"key_id": self.values["key_id"].get(), "application_key": self.values["application_key"].get()},
        )

    def _device_config(self) -> DeviceConfig:
        profile = self._capture_active_profile(validate=False, save_password=False)
        device = self._device_from_profile(profile)
        if not self.values["nvr_password"].get():
            raise ValueError(f"Complete la contraseña de {profile['name']}")
        return device

    @staticmethod
    def _device_from_profile(profile: dict) -> DeviceConfig:
        device = DeviceConfig(
            host=str(profile["host"]).strip(),
            http_port=int(profile["http_port"]),
            rtsp_port=int(profile["rtsp_port"]),
            username=str(profile["username"]).strip(),
            utc_offset=str(profile["utc_offset"]).strip(),
        )
        if not all([device.host, device.username]):
            raise ValueError(f"Complete host y usuario de {profile['name']}")
        return device

    def _selected_cameras(self) -> list[CameraConfig]:
        self._sync_active_cameras()
        cameras: list[CameraConfig] = []
        for profile in self.dvr_profiles:
            for item in profile.get("cameras", []):
                if not item.get("selected", False):
                    continue
                channel = int(item["channel"])
                name = str(item["camera_name"]).strip()
                if channel <= 0:
                    raise ValueError("Los canales deben ser enteros positivos")
                if not name:
                    raise ValueError(f"Complete el nombre de la cámara del canal {channel}")
                storage_source = profile.get("storage_name", "") if profile.get("use_namespace", True) else ""
                cameras.append(CameraConfig(channel, name, profile["id"], profile["name"], storage_source))
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
        self._capture_active_profile(validate=False, save_password=True)
        cameras = self._selected_cameras()
        cloud = self._cloud_config()
        start_time = datetime.strptime(self.values["start_time"].get().strip(), "%H:%M").time()
        end_time = datetime.strptime(self.values["end_time"].get().strip(), "%H:%M").time()
        common = {
            "cloud": cloud,
            "start_date": self._get_date(self.start_date),
            "end_date": self._get_date(self.end_date),
            "start_time": start_time,
            "end_time": end_time,
            "spool_dir": Path(self.values["spool_dir"].get()).expanduser().resolve(),
        }
        profiles = {item["id"]: item for item in self.dvr_profiles}
        passwords: dict[str, str] = {}
        jobs = []
        for camera in cameras:
            profile = profiles[camera.source_id]
            device = self._device_from_profile(profile)
            secret = load_secret(f"dvr:{profile['id']}")
            if not secret and profile["host"]:
                secret = load_secret(f"nvr:{profile['host']}")
            password = secret.get("password", "")
            if not password:
                raise ValueError(f"Falta la contraseña guardada para {profile['name']}")
            passwords[profile["id"]] = password
            jobs.append(JobConfig(device=device, camera=camera, **common))
        jobs[0].windows()
        if require_media:
            find_binary("ffmpeg", self.values["ffmpeg_dir"].get().strip())
            find_binary("ffprobe", self.values["ffmpeg_dir"].get().strip())
        return cloud, jobs, passwords

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
        profile_id = self.active_profile_id

        def run():
            try:
                cameras = IsapiClient(device, password).list_cameras()
                self.events.put(("cameras_detected", profile_id, cameras))
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
        self._sync_active_cameras()
        global_selected = sum(
            1 for profile in self.dvr_profiles for camera in profile.get("cameras", []) if camera.get("selected", False)
        )
        self.available_count.set(f"Disponibles ({len(self.available_camera_tree.get_children())})")
        self.selected_count.set(
            f"Este DVR ({len(self.selected_camera_tree.get_children())}) · Lote total ({global_selected})"
        )

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
        try:
            self._sync_active_cameras()
            self._persist_profiles()
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
        for camera in detected:
            name, was_selected = previous.get(camera.channel, (camera.camera_name, False))
            target = self.selected_camera_tree if was_selected else self.available_camera_tree
            target.insert("", "end", iid=str(camera.channel), values=(camera.channel, name))
        self._update_camera_counts()
        self._persist_camera_catalog()

    def _apply_detected_to_profile(self, profile_id: str, detected: list[CameraConfig]) -> None:
        if profile_id == self.active_profile_id:
            self._apply_detected_cameras(detected)
            return
        profile = self._profile(profile_id)
        previous = {int(item["channel"]): item for item in profile.get("cameras", [])}
        profile["cameras"] = [
            {
                "channel": camera.channel,
                "camera_name": previous.get(camera.channel, {}).get("camera_name", camera.camera_name),
                "selected": bool(previous.get(camera.channel, {}).get("selected", False)),
            }
            for camera in detected
        ]
        self._persist_profiles()

    @staticmethod
    def _task_iid(camera: CameraConfig, day: str) -> str:
        return f"{camera.source_id}|{camera.channel}|{day}"

    def _test_cloud(self) -> None:
        try:
            cloud = self._cloud_config()
            self._save(validate_active=False)
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
            _, jobs, passwords = self._configs(require_media=False)
            self._save(validate_active=False)
        except Exception as exc:
            messagebox.showerror("Configuración", str(exc))
            return
        self.notebook.select(self.progress_tab)
        self.tree.delete(*self.tree.get_children())
        self.estimate_button.configure(state="disabled")
        self.start_button.configure(state="disabled")

        def estimate_action():
            total = 0
            for job in jobs:
                client = IsapiClient(job.device, passwords[job.camera.source_id])
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
            _, jobs, passwords = self._configs()
            self._save(validate_active=False)
        except Exception as exc:
            messagebox.showerror("Configuración inválida", str(exc))
            return
        first = jobs[0]
        camera_names = ", ".join(f"{job.camera.display_name} (canal {job.camera.channel})" for job in jobs)
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
                self.tree.insert("", "end", iid=iid, values=(job.camera.display_name, day, "pendiente", ""))
        self._task_order = {iid: index for index, iid in enumerate(task_iids)}
        self.cancel_event = threading.Event()
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
            self.events.put(("progress", iid, camera.display_name, day, detail, value, overall))

        def run():
            try:
                runner = BatchJobRunner(
                    jobs,
                    passwords,
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
                    self._refresh_audit()
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
                    values = (camera.display_name, day, "estimado", detail)
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
                    profile_id, detected = event[1], event[2]
                    self._apply_detected_to_profile(profile_id, detected)
                    self.current_status.configure(
                        text=f"Se detectaron {len(detected)} cámara(s) en {self._profile(profile_id)['name']}."
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
