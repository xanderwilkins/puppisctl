#!/usr/bin/env python3
"""
Puppis S1 Tkinter Config Tool

One-file GUI for talking directly to a PrismXR Puppis S1 over its local TCP
configuration protocol.

Tested protocol:
  - Device IP defaults to 192.168.137.254
  - TCP port defaults to 10081
  - Packet = aa + one-byte total length + crc32(type + 00 + json) + type + 00 + json
  - Request type = 0x01
  - JSON format = {"fun":"getDevice","args":{}}

No third-party packages required.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import traceback
import zlib
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


APP_TITLE = "Puppis S1 Config Tool"

DEFAULT_DEVICE_IP = "192.168.137.254"
DEFAULT_PORT = 10081
DEFAULT_TIMEOUT = 5.0

TESTED_GETTERS = [
    "getNetRf",
    "getDevice",
    "getCountryCode",
    "getChInfo",
    "getLanIPInfo",
    "getDhcpInfo",
    "getProductInfo",
    "getUpgrade",
    "getNetIPInfo",
    "getNetPtInfo",
    "get5GHotspot",
    "get2GHotspot",
]

RAW_FUNCTIONS = TESTED_GETTERS + [
    "set5GHotspot",
    "set2GHotspot",
    # Exposed by the official app; not all were tested in this conversation.
    "setCountryCode",
    "setDhcpInfo",
    "setMode",
    "setBoost",
    "setLed",
    "setFactory",
]


def redact(obj):
    """Return a copy of obj with sensitive fields removed."""
    sensitive = {"pwd", "password", "psk", "key", "token"}
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if k.lower() in sensitive else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def pretty(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


class PuppisProtocolError(Exception):
    pass


class PuppisClient:
    def __init__(
        self,
        device_ip: str = DEFAULT_DEVICE_IP,
        port: int = DEFAULT_PORT,
        local_ip: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.device_ip = device_ip.strip()
        self.port = int(port)
        self.local_ip = local_ip.strip()
        self.timeout = float(timeout)

    @staticmethod
    def make_packet(fun: str, args=None, msg_type: int = 0x01) -> bytes:
        if args is None:
            args = {}

        payload = json.dumps(
            {"fun": fun, "args": args},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        # Official app uses: type + 00 + JSON
        body = bytes([msg_type, 0x00]) + payload
        total_length = len(payload) + 8

        if total_length > 255:
            raise PuppisProtocolError(
                f"Packet too long for the known 1-byte length field: {total_length} bytes"
            )

        crc = zlib.crc32(body).to_bytes(4, "big")
        return bytes([0xAA, total_length]) + crc + body

    @staticmethod
    def recv_exact(sock: socket.socket, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                break
            out += chunk
        return out

    def call(self, fun: str, args=None):
        packet = self.make_packet(fun, args)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)

            if self.local_ip:
                sock.bind((self.local_ip, 0))

            sock.connect((self.device_ip, self.port))
            sock.sendall(packet)

            header = self.recv_exact(sock, 8)
            if len(header) != 8:
                raise PuppisProtocolError(
                    "No full response header received. Close PrismXR Desktop if it is open."
                )

            if header[0] != 0xAA:
                raise PuppisProtocolError(f"Bad magic byte in response: {header.hex(' ')}")

            total_length = header[1]
            if total_length < 8:
                raise PuppisProtocolError(f"Bad response length: {total_length}")

            data = self.recv_exact(sock, total_length - 8)
            if len(data) != total_length - 8:
                raise PuppisProtocolError(
                    f"Short response: expected {total_length - 8} bytes, got {len(data)}"
                )

            text = data.decode("utf-8", errors="replace").strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text, "_header": header.hex(" ")}


class ToolTip:
    """Tiny tooltip helper for tkinter widgets."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = ttk.Label(
            tw,
            text=self.text,
            relief="solid",
            borderwidth=1,
            padding=(8, 4),
        )
        label.pack()

    def hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


class PuppisApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1060x760")
        self.minsize(900, 620)

        self.last_data = {}
        self.raw_last_response = None

        self.device_ip_var = tk.StringVar(value=DEFAULT_DEVICE_IP)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.local_ip_var = tk.StringVar(value="")
        self.timeout_var = tk.StringVar(value=str(DEFAULT_TIMEOUT))
        self.status_var = tk.StringVar(value="Ready. Close PrismXR Desktop before using this.")

        self.hotspot_vars = {
            "5G": self.make_hotspot_vars(),
            "2G": self.make_hotspot_vars(),
        }
        self.dhcp_vars = {
            "startIp": tk.StringVar(),
            "endIp": tk.StringVar(),
            "gIp": tk.StringVar(),
            "dns1": tk.StringVar(),
            "dns2": tk.StringVar(),
            "lease": tk.StringVar(),
            "mode": tk.StringVar(),
        }

        self.raw_fun_var = tk.StringVar(value="getDevice")

        self.build_ui()

    @staticmethod
    def make_hotspot_vars():
        return {
            "ssid": tk.StringVar(),
            "pwd": tk.StringVar(),
            "pt": tk.StringVar(),
            "ch": tk.StringVar(),
            "encrypt": tk.StringVar(),
            "en": tk.StringVar(),
            "code": tk.StringVar(),
            "bw": tk.StringVar(),
            "_show_pwd": tk.BooleanVar(value=False),
        }

    def build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        self.build_connection_bar(root)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, pady=(10, 8))

        self.overview_text = self.make_text_tab("Overview")
        self.build_wifi_tab()
        self.build_network_tab()
        self.build_raw_tab()
        self.log_text = self.make_text_tab("Log")

        status = ttk.Label(root, textvariable=self.status_var, anchor="w")
        status.pack(fill="x")

    def build_connection_bar(self, parent):
        frame = ttk.LabelFrame(parent, text="Connection", padding=10)
        frame.pack(fill="x")

        ttk.Label(frame, text="Device IP").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.device_ip_var, width=18).grid(row=0, column=1, padx=(4, 12))

        ttk.Label(frame, text="Port").grid(row=0, column=2, sticky="w")
        ttk.Entry(frame, textvariable=self.port_var, width=8).grid(row=0, column=3, padx=(4, 12))

        ttk.Label(frame, text="Local bind IP").grid(row=0, column=4, sticky="w")
        local_entry = ttk.Entry(frame, textvariable=self.local_ip_var, width=18)
        local_entry.grid(row=0, column=5, padx=(4, 12))
        ToolTip(local_entry, "Usually leave blank. Use 192.168.137.100 only if auto-routing fails.")

        ttk.Label(frame, text="Timeout").grid(row=0, column=6, sticky="w")
        ttk.Entry(frame, textvariable=self.timeout_var, width=8).grid(row=0, column=7, padx=(4, 12))

        ttk.Button(frame, text="Test", command=self.test_connection).grid(row=0, column=8, padx=4)
        ttk.Button(frame, text="Refresh All", command=self.refresh_all).grid(row=0, column=9, padx=4)
        ttk.Button(frame, text="Load Wi-Fi", command=self.load_wifi).grid(row=0, column=10, padx=4)
        ttk.Button(frame, text="Export Redacted Backup", command=self.export_redacted_backup).grid(row=0, column=11, padx=4)

        for i in range(12):
            frame.grid_columnconfigure(i, weight=0)
        frame.grid_columnconfigure(12, weight=1)

    def make_text_tab(self, name):
        frame = ttk.Frame(self.notebook, padding=8)
        text = scrolledtext.ScrolledText(frame, wrap="word", font=("Consolas", 10))
        text.pack(fill="both", expand=True)
        self.notebook.add(frame, text=name)
        return text

    def build_wifi_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        self.build_hotspot_frame(frame, "5G", 0)
        self.build_hotspot_frame(frame, "2G", 1)

        note = ttk.Label(
            frame,
            text=(
                "Setter behavior: this tool loads the full current hotspot object, "
                "modifies fields in the UI, then sends the full object back."
            ),
            wraplength=900,
        )
        note.grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))

        self.notebook.add(frame, text="Wi-Fi")

    def build_hotspot_frame(self, parent, band, col):
        vars_ = self.hotspot_vars[band]
        box = ttk.LabelFrame(parent, text=f"{band} Hotspot", padding=10)
        box.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 8 if col == 0 else 0))
        box.columnconfigure(1, weight=1)

        fields = [
            ("SSID", "ssid"),
            ("Password", "pwd"),
            ("pt", "pt"),
            ("Channel", "ch"),
            ("Encrypt", "encrypt"),
            ("Enabled/en", "en"),
            ("Country code", "code"),
            ("Bandwidth", "bw"),
        ]

        for r, (label, key) in enumerate(fields):
            ttk.Label(box, text=label).grid(row=r, column=0, sticky="w", pady=3)
            show = "*" if key == "pwd" and not vars_["_show_pwd"].get() else ""
            entry = ttk.Entry(box, textvariable=vars_[key], show=show)
            entry.grid(row=r, column=1, sticky="ew", padx=(8, 0), pady=3)
            if key == "pwd":
                vars_["_pwd_entry"] = entry

        show_btn = ttk.Checkbutton(
            box,
            text="Show password",
            variable=vars_["_show_pwd"],
            command=lambda b=band: self.toggle_password_visibility(b),
        )
        show_btn.grid(row=len(fields), column=1, sticky="w", pady=(4, 8))

        btn_frame = ttk.Frame(box)
        btn_frame.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="ew")
        ttk.Button(btn_frame, text=f"Load {band}", command=lambda b=band: self.load_hotspot(b)).pack(side="left")
        ttk.Button(btn_frame, text=f"Apply {band}", command=lambda b=band: self.apply_hotspot(b)).pack(side="left", padx=8)

    def build_network_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        lan_box = ttk.LabelFrame(frame, text="LAN / Device Network Info", padding=10)
        lan_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self.lan_text = scrolledtext.ScrolledText(lan_box, height=14, wrap="word", font=("Consolas", 10))
        self.lan_text.pack(fill="both", expand=True)
        ttk.Button(lan_box, text="Refresh Network Info", command=self.refresh_network).pack(anchor="w", pady=(8, 0))

        dhcp_box = ttk.LabelFrame(frame, text="DHCP Settings", padding=10)
        dhcp_box.grid(row=0, column=1, sticky="nsew")
        dhcp_box.columnconfigure(1, weight=1)

        labels = [
            ("Start IP", "startIp"),
            ("End IP", "endIp"),
            ("Gateway IP", "gIp"),
            ("DNS 1", "dns1"),
            ("DNS 2", "dns2"),
            ("Lease minutes", "lease"),
            ("Mode", "mode"),
        ]

        for r, (label, key) in enumerate(labels):
            ttk.Label(dhcp_box, text=label).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(dhcp_box, textvariable=self.dhcp_vars[key]).grid(
                row=r, column=1, sticky="ew", padx=(8, 0), pady=3
            )

        ttk.Button(dhcp_box, text="Load DHCP", command=self.load_dhcp).grid(row=len(labels), column=0, pady=(10, 0), sticky="w")
        apply_btn = ttk.Button(dhcp_box, text="Apply DHCP", command=self.apply_dhcp)
        apply_btn.grid(row=len(labels), column=1, pady=(10, 0), sticky="w", padx=(8, 0))
        ToolTip(apply_btn, "Careful: changing DHCP settings can break your connection until reset/fixed.")

        warning = ttk.Label(
            frame,
            text="DHCP writes are implemented from the same discovered official-app API, but be careful: bad DHCP values can make the device annoying to reach.",
            wraplength=900,
        )
        warning.grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))

        self.notebook.add(frame, text="Network")

    def build_raw_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(3, weight=1)

        ttk.Label(frame, text="Function").grid(row=0, column=0, sticky="w")
        fun_combo = ttk.Combobox(frame, textvariable=self.raw_fun_var, values=RAW_FUNCTIONS)
        fun_combo.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(frame, text="Args JSON").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        self.raw_args_text = scrolledtext.ScrolledText(frame, height=8, wrap="word", font=("Consolas", 10))
        self.raw_args_text.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.raw_args_text.insert("1.0", "{}")

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=1, sticky="w", pady=8, padx=(8, 0))
        ttk.Button(btn_frame, text="Send Raw Call", command=self.send_raw).pack(side="left")
        ttk.Button(btn_frame, text="Use selected getter args {}", command=self.raw_set_empty_args).pack(side="left", padx=8)

        ttk.Label(frame, text="Response").grid(row=3, column=0, sticky="nw")
        self.raw_output_text = scrolledtext.ScrolledText(frame, wrap="word", font=("Consolas", 10))
        self.raw_output_text.grid(row=3, column=1, sticky="nsew", padx=(8, 0))

        self.notebook.add(frame, text="Raw API")

    def client(self):
        return PuppisClient(
            device_ip=self.device_ip_var.get(),
            port=int(self.port_var.get()),
            local_ip=self.local_ip_var.get(),
            timeout=float(self.timeout_var.get()),
        )

    def log(self, msg):
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] {msg}\n")
        self.log_text.see("end")

    def set_status(self, msg):
        self.status_var.set(msg)
        self.log(msg)

    def run_bg(self, label, work, done=None, fail=None):
        self.set_status(f"{label}...")

        def target():
            try:
                result = work()
            except Exception as exc:
                tb = traceback.format_exc()
                self.after(0, lambda: self.bg_fail(label, exc, tb, fail))
                return
            self.after(0, lambda: self.bg_done(label, result, done))

        threading.Thread(target=target, daemon=True).start()

    def bg_done(self, label, result, done):
        self.set_status(f"{label}: done")
        if done:
            done(result)

    def bg_fail(self, label, exc, tb, fail):
        self.set_status(f"{label}: failed: {exc}")
        self.log(tb)
        if fail:
            fail(exc)
        else:
            messagebox.showerror(APP_TITLE, f"{label} failed:\n\n{exc}")

    def test_connection(self):
        def work():
            return self.client().call("getDevice")

        def done(res):
            self.raw_last_response = res
            self.overview_text.delete("1.0", "end")
            self.overview_text.insert("end", pretty(redact(res)))
            if res.get("status") == "ok":
                messagebox.showinfo(APP_TITLE, "Puppis S1 responded successfully.")
            else:
                messagebox.showwarning(APP_TITLE, "Device responded, but status was not ok.")

        self.run_bg("Testing connection", work, done)

    def refresh_all(self):
        def work():
            c = self.client()
            out = {}
            for fun in TESTED_GETTERS:
                try:
                    out[fun] = c.call(fun)
                except Exception as e:
                    out[fun] = {"error": str(e)}
            return out

        def done(out):
            self.last_data.update(out)
            self.overview_text.delete("1.0", "end")
            self.overview_text.insert("end", pretty(redact(out)))
            self.fill_from_last_data(out)

        self.run_bg("Refreshing all tested getters", work, done)

    def refresh_network(self):
        def work():
            c = self.client()
            out = {}
            for fun in ["getLanIPInfo", "getDhcpInfo", "getNetIPInfo", "getNetPtInfo", "getChInfo", "getNetRf"]:
                try:
                    out[fun] = c.call(fun)
                except Exception as e:
                    out[fun] = {"error": str(e)}
            return out

        def done(out):
            self.last_data.update(out)
            self.lan_text.delete("1.0", "end")
            self.lan_text.insert("end", pretty(redact(out)))
            self.fill_dhcp_from_response(out.get("getDhcpInfo"))

        self.run_bg("Refreshing network info", work, done)

    def load_wifi(self):
        def work():
            c = self.client()
            return {
                "get5GHotspot": c.call("get5GHotspot"),
                "get2GHotspot": c.call("get2GHotspot"),
            }

        def done(out):
            self.last_data.update(out)
            self.fill_hotspot_from_response("5G", out.get("get5GHotspot"))
            self.fill_hotspot_from_response("2G", out.get("get2GHotspot"))
            self.raw_output_text.delete("1.0", "end")
            self.raw_output_text.insert("end", pretty(redact(out)))

        self.run_bg("Loading Wi-Fi config", work, done)

    def load_hotspot(self, band):
        fun = "get5GHotspot" if band == "5G" else "get2GHotspot"

        def work():
            return self.client().call(fun)

        def done(res):
            self.last_data[fun] = res
            self.fill_hotspot_from_response(band, res)
            self.raw_output_text.delete("1.0", "end")
            self.raw_output_text.insert("end", pretty(redact(res)))

        self.run_bg(f"Loading {band} hotspot", work, done)

    def fill_from_last_data(self, out):
        self.fill_hotspot_from_response("5G", out.get("get5GHotspot"))
        self.fill_hotspot_from_response("2G", out.get("get2GHotspot"))
        self.fill_dhcp_from_response(out.get("getDhcpInfo"))

    def fill_hotspot_from_response(self, band, res):
        if not isinstance(res, dict) or "data" not in res:
            return
        data = res["data"]
        vars_ = self.hotspot_vars[band]
        for key in ["ssid", "pwd", "pt", "ch", "encrypt", "en", "code", "bw"]:
            if key in data:
                vars_[key].set(str(data[key]))

    def fill_dhcp_from_response(self, res):
        if not isinstance(res, dict) or "data" not in res:
            return
        data = res["data"]
        for key, var in self.dhcp_vars.items():
            if key in data:
                var.set(str(data[key]))

    def toggle_password_visibility(self, band):
        vars_ = self.hotspot_vars[band]
        entry = vars_.get("_pwd_entry")
        if entry:
            entry.configure(show="" if vars_["_show_pwd"].get() else "*")

    def hotspot_args_from_ui(self, band):
        vars_ = self.hotspot_vars[band]
        out = {}
        for key in ["ssid", "pwd", "pt", "ch", "encrypt", "en", "code", "bw"]:
            value = vars_[key].get()
            if value != "":
                out[key] = value
        return out

    def apply_hotspot(self, band):
        fun = "set5GHotspot" if band == "5G" else "set2GHotspot"
        get_fun = "get5GHotspot" if band == "5G" else "get2GHotspot"
        args = self.hotspot_args_from_ui(band)

        if not args.get("ssid"):
            messagebox.showerror(APP_TITLE, f"{band} SSID is empty.")
            return

        if not args.get("pwd"):
            if not messagebox.askyesno(
                APP_TITLE,
                f"{band} password is empty. Continue anyway?",
            ):
                return

        if not messagebox.askyesno(
            APP_TITLE,
            f"Apply {band} hotspot settings?\n\nDevices may disconnect/reconnect.",
        ):
            return

        def work():
            c = self.client()
            res = c.call(fun, args)
            check = c.call(get_fun)
            return {"set": res, "check": check}

        def done(out):
            self.last_data[get_fun] = out["check"]
            self.fill_hotspot_from_response(band, out["check"])
            self.raw_output_text.delete("1.0", "end")
            self.raw_output_text.insert("end", pretty(redact(out)))
            status = out["set"].get("status")
            if status == "ok":
                messagebox.showinfo(APP_TITLE, f"{band} settings applied.")
            else:
                messagebox.showwarning(APP_TITLE, f"{band} set returned: {status}")

        self.run_bg(f"Applying {band} hotspot", work, done)

    def load_dhcp(self):
        def work():
            return self.client().call("getDhcpInfo")

        def done(res):
            self.last_data["getDhcpInfo"] = res
            self.fill_dhcp_from_response(res)
            self.raw_output_text.delete("1.0", "end")
            self.raw_output_text.insert("end", pretty(redact(res)))

        self.run_bg("Loading DHCP", work, done)

    def dhcp_args_from_ui(self):
        return {k: v.get() for k, v in self.dhcp_vars.items() if v.get() != ""}

    def apply_dhcp(self):
        args = self.dhcp_args_from_ui()
        needed = ["startIp", "endIp", "gIp", "dns1", "dns2", "lease", "mode"]
        missing = [k for k in needed if not args.get(k)]

        if missing:
            messagebox.showerror(APP_TITLE, f"Missing DHCP fields: {', '.join(missing)}")
            return

        if not messagebox.askyesno(
            APP_TITLE,
            "Apply DHCP settings?\n\nBad DHCP values can make the Puppis hard to reach. Continue?",
        ):
            return

        def work():
            c = self.client()
            res = c.call("setDhcpInfo", args)
            check = c.call("getDhcpInfo")
            return {"set": res, "check": check}

        def done(out):
            self.last_data["getDhcpInfo"] = out["check"]
            self.fill_dhcp_from_response(out["check"])
            self.raw_output_text.delete("1.0", "end")
            self.raw_output_text.insert("end", pretty(redact(out)))

        self.run_bg("Applying DHCP", work, done)

    def raw_set_empty_args(self):
        self.raw_args_text.delete("1.0", "end")
        self.raw_args_text.insert("1.0", "{}")

    def send_raw(self):
        fun = self.raw_fun_var.get().strip()
        if not fun:
            messagebox.showerror(APP_TITLE, "Function name is empty.")
            return

        args_text = self.raw_args_text.get("1.0", "end").strip() or "{}"
        try:
            args = json.loads(args_text)
        except json.JSONDecodeError as e:
            messagebox.showerror(APP_TITLE, f"Args JSON is invalid:\n\n{e}")
            return

        if fun.startswith("set"):
            if not messagebox.askyesno(
                APP_TITLE,
                f"Send raw setter '{fun}'?\n\nThis may change device configuration.",
            ):
                return

        def work():
            return self.client().call(fun, args)

        def done(res):
            self.raw_last_response = res
            self.raw_output_text.delete("1.0", "end")
            self.raw_output_text.insert("end", pretty(redact(res)))

        self.run_bg(f"Raw call {fun}", work, done)

    def export_redacted_backup(self):
        data = {
            "created_by": APP_TITLE,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "device_ip": self.device_ip_var.get(),
            "data": redact(self.last_data),
        }

        if not self.last_data:
            if not messagebox.askyesno(
                APP_TITLE,
                "No cached data yet. Export an empty backup anyway?",
            ):
                return

        path = filedialog.asksaveasfilename(
            title="Export redacted backup",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile="puppis_s1_redacted_backup.json",
        )
        if not path:
            return

        Path(path).write_text(pretty(data), encoding="utf-8")
        self.set_status(f"Exported redacted backup: {path}")
        messagebox.showinfo(APP_TITLE, f"Saved:\n{path}")


def main():
    app = PuppisApp()
    app.mainloop()


if __name__ == "__main__":
    main()
