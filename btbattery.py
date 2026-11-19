#!/usr/bin/env python3
"""Bluetooth Battery — panel/tray icons showing battery % of connected Bluetooth devices.

Reads org.bluez.Battery1 straight from BlueZ over D-Bus (no vendor software),
shows one StatusNotifierItem (tray icon) per device, and sends a desktop
notification when a device runs low.

    btbattery.py            run the tray app
    btbattery.py --list     print connected devices and exit
    btbattery.py --json     print JSON (for bar widgets / scripts) and exit
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

from PyQt6.QtCore import QMetaType, QObject, QRectF, QSettings, Qt, QTimer, QVariant, pyqtSlot
from PyQt6.QtDBus import QDBusArgument, QDBusConnection, QDBusMessage, QDBusVariant
from PyQt6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

APP_ID = "btbattery"
APP_NAME = "Bluetooth Battery"
BUS_NAME = "io.github.btbattery"

BLUEZ = "org.bluez"
DEV_IFACE = "org.bluez.Device1"
BAT_IFACE = "org.bluez.Battery1"

STYLES = {
    "icon_number": "Device icon + percentage",
    "number": "Percentage only",
    "icon_bar": "Device icon + level bar",
}
ICON_SIZES = (16, 22, 24, 32, 44, 48, 64)

# BlueZ "Icon" property -> fallback emoji/short label (used in --list and tooltips)
KIND_LABEL = {
    "input-mouse": "Mouse",
    "input-keyboard": "Keyboard",
    "input-gaming": "Controller",
    "input-tablet": "Tablet",
    "audio-headset": "Headset",
    "audio-headphones": "Headphones",
    "audio-card": "Speaker",
    "phone": "Phone",
    "computer": "Computer",
}


# --------------------------------------------------------------------------- data


@dataclass
class Device:
    path: str
    address: str
    name: str
    icon: str
    percentage: int
    source: str

    @property
    def kind(self) -> str:
        return KIND_LABEL.get(self.icon, "Device")


def _to_int(v) -> int:
    if isinstance(v, (bytes, bytearray)):
        return v[0] if v else 0
    return int(v)


def read_devices(bus: QDBusConnection) -> list[Device]:
    msg = QDBusMessage.createMethodCall(BLUEZ, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
    reply = bus.call(msg)
    if reply.type() != QDBusMessage.MessageType.ReplyMessage or not reply.arguments():
        return []
    devices = []
    for path, ifaces in reply.arguments()[0].items():
        dev, bat = ifaces.get(DEV_IFACE), ifaces.get(BAT_IFACE)
        if not dev or not bat or not dev.get("Connected"):
            continue
        try:
            pct = max(0, min(100, _to_int(bat.get("Percentage", 0))))
        except (TypeError, ValueError):
            continue
        devices.append(Device(
            path=path,
            address=str(dev.get("Address", "")),
            name=str(dev.get("Alias") or dev.get("Name") or dev.get("Address", "?")),
            icon=str(dev.get("Icon") or "bluetooth"),
            percentage=pct,
            source=str(bat.get("Source", "")),
        ))
    devices.sort(key=lambda d: d.name.lower())
    return devices


# --------------------------------------------------------------------------- icons


def level_color(pct: int, low: int) -> QColor:
    if pct <= low:
        return QColor("#e5484d")      # red
    if pct <= max(low + 15, 30):
        return QColor("#f5a524")      # amber
    return QColor("#30a46c")          # green


def _device_glyph(icon_name: str) -> QIcon:
    for name in (f"{icon_name}-symbolic", icon_name, "bluetooth-active-symbolic", "bluetooth"):
        ic = QIcon.fromTheme(name)
        if not ic.isNull():
            return ic
    return QIcon()


def _paint_glyph(p: QPainter, icon: QIcon, x: int, y: int, size: int, color: QColor):
    """Paint a theme icon recoloured to `color` (so it matches light/dark panels)."""
    if size <= 0 or icon.isNull():
        return
    src = icon.pixmap(size, size)
    tinted = QPixmap(src.size())
    tinted.setDevicePixelRatio(src.devicePixelRatio())
    tinted.fill(Qt.GlobalColor.transparent)
    tp = QPainter(tinted)
    tp.drawPixmap(0, 0, src)
    tp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    tp.fillRect(tinted.rect(), color)
    tp.end()
    p.drawPixmap(x, y, size, size, tinted)


def _fit_font(p: QPainter, text: str, rect: QRectF, bold=True) -> QFont:
    f = QFont(QApplication.font())
    f.setBold(bold)
    size = rect.height()
    while size > 4:
        f.setPixelSize(int(size))
        p.setFont(f)
        br = p.fontMetrics().tightBoundingRect(text)
        if br.width() <= rect.width() and br.height() <= rect.height():
            break
        size -= 0.5
    return f


def render_pixmap(dev: Device, size: int, style: str, low: int, fg: QColor) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    s = float(size)
    col = level_color(dev.percentage, low)
    text = str(dev.percentage)
    glyph = _device_glyph(dev.icon)

    def draw_text(rect: QRectF, color: QColor):
        p.setFont(_fit_font(p, text, rect))
        p.setPen(color)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    if style == "number":
        draw_text(QRectF(0, 0, s, s * 0.80), fg if dev.percentage > low else col)
        bar_h = max(2.0, s * 0.12)
        _bar(p, QRectF(s * 0.06, s - bar_h - s * 0.02, s * 0.88, bar_h), dev.percentage, col, fg)

    elif style == "icon_bar":
        gs = int(s * 0.78)
        _paint_glyph(p, glyph, 0, int((s - gs) / 2), gs, fg)
        bw = max(3.0, s * 0.16)
        _bar(p, QRectF(s - bw, s * 0.08, bw, s * 0.84), dev.percentage, col, fg, vertical=True)

    else:  # icon_number
        gs = int(s * 0.50)
        _paint_glyph(p, glyph, int((s - gs) / 2), 0, gs, fg)
        draw_text(QRectF(0, s * 0.50, s, s * 0.50), fg if dev.percentage > low else col)

    p.end()
    return pm


def _bar(p: QPainter, r: QRectF, pct: int, col: QColor, fg: QColor, vertical=False):
    radius = min(r.width(), r.height()) / 2.5
    track = QColor(fg)
    track.setAlphaF(0.25)
    path = QPainterPath()
    path.addRoundedRect(r, radius, radius)
    p.fillPath(path, track)
    frac = max(0.06, pct / 100.0)
    if vertical:
        fill = QRectF(r.x(), r.bottom() - r.height() * frac, r.width(), r.height() * frac)
    else:
        fill = QRectF(r.x(), r.y(), r.width() * frac, r.height())
    fp = QPainterPath()
    fp.addRoundedRect(fill, radius, radius)
    p.fillPath(fp, col)


def build_icon(dev: Device, style: str, low: int, fg: QColor) -> QIcon:
    ic = QIcon()
    for sz in ICON_SIZES:
        ic.addPixmap(render_pixmap(dev, sz, style, low, fg))
    return ic


# --------------------------------------------------------------------------- app


class BatteryTray(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.sys = QDBusConnection.systemBus()
        self.session = QDBusConnection.sessionBus()
        self.settings = QSettings(APP_ID, APP_ID)
        self.icons: dict[str, QSystemTrayIcon] = {}
        self.devices: list[Device] = []
        self.alerted: set[str] = set()
        self.last_sig: dict[str, tuple] = {}

        self.placeholder = QSystemTrayIcon(_device_glyph("bluetooth"), self)
        self.placeholder.setToolTip(f"{APP_NAME}: no connected devices reporting battery")
        self.placeholder.activated.connect(self._on_activated)
        self.menus: list[QMenu] = []
        self.placeholder.setContextMenu(self._new_menu())

        # Debounced rescan on any BlueZ change
        self.debounce = QTimer(self, singleShot=True, interval=300)
        self.debounce.timeout.connect(self.refresh)
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.refresh)
        self.poll.start(self.interval * 1000)

        for path, iface, name in (
            ("", "org.freedesktop.DBus.Properties", "PropertiesChanged"),
            ("/", "org.freedesktop.DBus.ObjectManager", "InterfacesAdded"),
            ("/", "org.freedesktop.DBus.ObjectManager", "InterfacesRemoved"),
        ):
            self.sys.connect(BLUEZ, path, iface, name, self._on_bluez_signal)

        app.paletteChanged.connect(lambda *_: self.refresh(force=True))
        self.refresh(force=True)

    # ---- settings
    @property
    def style(self) -> str:
        s = self.settings.value("style", "icon_number", str)
        return s if s in STYLES else "icon_number"

    @property
    def low(self) -> int:
        return self.settings.value("low_threshold", 15, int)

    @property
    def interval(self) -> int:
        return self.settings.value("interval", 60, int)

    @property
    def notify(self) -> bool:
        return self.settings.value("notify", True, bool)

    @property
    def hidden(self) -> set[str]:
        v = self.settings.value("hidden", [], list) or []
        return set(v if isinstance(v, list) else [v])

    def _set(self, key, value, force=True):
        self.settings.setValue(key, value)
        self.settings.sync()
        if key == "interval":
            self.poll.start(int(value) * 1000)
        self.refresh(force=force)

    # ---- dbus
    @pyqtSlot(QDBusMessage)
    def _on_bluez_signal(self, msg: QDBusMessage):
        self.debounce.start()

    # ---- core
    def fg_color(self) -> QColor:
        return self.app.palette().windowText().color()

    def refresh(self, force=False):
        self.devices = read_devices(self.sys)
        hidden, style, low, fg = self.hidden, self.style, self.low, self.fg_color()
        visible = {d.path: d for d in self.devices if d.address not in hidden}

        # Show before hiding: Qt ends the app when its last visible tray icon is hidden.
        if not visible:
            self.placeholder.show()

        for path, d in visible.items():
            tray = self.icons.get(path)
            if tray is None:
                tray = QSystemTrayIcon(self)
                tray.setContextMenu(self._new_menu())
                tray.activated.connect(self._on_activated)
                self.icons[path] = tray
            sig = (d.percentage, d.name, d.icon, style, low, fg.name())
            if force or self.last_sig.get(path) != sig:
                tray.setIcon(build_icon(d, style, low, fg))
                src = f"\n({d.source})" if d.source else ""
                tray.setToolTip(f"{d.name}\n{d.kind} · {d.percentage}%{src}")
                self.last_sig[path] = sig
            if not tray.isVisible():
                tray.show()
            self._check_alert(d)

        # Hide (never destroy) icons whose device went away; they are re-shown if it returns.
        for path, tray in self.icons.items():
            if path not in visible and tray.isVisible():
                tray.hide()
        if visible and self.placeholder.isVisible():
            self.placeholder.hide()

    def _check_alert(self, d: Device):
        low = self.low
        if d.percentage <= low and d.address not in self.alerted:
            self.alerted.add(d.address)
            if self.notify:
                self._notify(f"{d.name} battery low", f"{d.percentage}% remaining", d.icon)
        elif d.percentage > low + 10:
            self.alerted.discard(d.address)

    def _notify(self, summary: str, body: str, icon: str):
        msg = QDBusMessage.createMethodCall(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify")
        actions = QDBusArgument()
        actions.add([], QMetaType.Type.QStringList.value)
        replaces_id = QVariant(0)
        replaces_id.convert(QMetaType(QMetaType.Type.UInt.value))
        msg.setArguments([APP_NAME, replaces_id, icon or "battery-caution", summary, body,
                          actions, {"urgency": QDBusVariant(2)}, -1])
        self.session.asyncCall(msg)

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.refresh(force=True)

    # ---- menu
    def _new_menu(self) -> QMenu:
        # Every tray icon needs its OWN QMenu. Sharing one menu between several
        # icons makes KDE's StatusNotifierItem free it when any one icon is
        # hidden, and the next click on another icon segfaults.
        m = QMenu()
        m.aboutToShow.connect(lambda m=m: self._build_menu(m))
        self._build_menu(m)
        self.menus.append(m)
        return m

    def _build_menu(self, m: QMenu):
        m.clear()
        hidden = self.hidden
        all_devs = self.devices

        hdr = m.addAction("Devices")
        hdr.setEnabled(False)
        if not all_devs:
            a = m.addAction("  No connected devices report battery")
            a.setEnabled(False)
        for d in all_devs:
            a = m.addAction(_device_glyph(d.icon), f"{d.name} — {d.percentage}%")
            a.setCheckable(True)
            a.setChecked(d.address not in hidden)
            a.setToolTip("Show / hide this device's panel icon")
            a.toggled.connect(lambda on, addr=d.address: self._toggle_hidden(addr, not on))
        m.addSeparator()

        sm = m.addMenu("Icon style")
        g = QActionGroup(sm)
        for key, label in STYLES.items():
            a = sm.addAction(label)
            a.setCheckable(True)
            a.setChecked(key == self.style)
            g.addAction(a)
            a.triggered.connect(lambda _=False, k=key: self._set("style", k))

        lm = m.addMenu("Low battery alert at")
        g2 = QActionGroup(lm)
        for v in (5, 10, 15, 20, 25, 30, 40):
            a = lm.addAction(f"{v}%")
            a.setCheckable(True)
            a.setChecked(v == self.low)
            g2.addAction(a)
            a.triggered.connect(lambda _=False, val=v: self._set("low_threshold", val))
        lm.addSeparator()
        na = lm.addAction("Show notifications")
        na.setCheckable(True)
        na.setChecked(self.notify)
        na.toggled.connect(lambda on: self._set("notify", on, force=False))

        im = m.addMenu("Check battery every")
        g3 = QActionGroup(im)
        for v, label in ((15, "15 seconds"), (30, "30 seconds"), (60, "1 minute"), (120, "2 minutes"), (300, "5 minutes")):
            a = im.addAction(label)
            a.setCheckable(True)
            a.setChecked(v == self.interval)
            g3.addAction(a)
            a.triggered.connect(lambda _=False, val=v: self._set("interval", val, force=False))

        m.addAction(QIcon.fromTheme("view-refresh"), "Refresh now", lambda: self.refresh(force=True))
        m.addSeparator()
        au = m.addAction("Start on login")
        au.setCheckable(True)
        au.setChecked(os.path.exists(autostart_path()))
        au.toggled.connect(set_autostart)
        m.addAction(QIcon.fromTheme("application-exit"), "Quit", self.app.quit)

    def _toggle_hidden(self, addr: str, hide: bool):
        h = self.hidden
        (h.add if hide else h.discard)(addr)
        self._set("hidden", sorted(h))


# --------------------------------------------------------------------------- autostart


def autostart_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "autostart", f"{APP_ID}.desktop")


def set_autostart(on: bool):
    path = autostart_path()
    if on:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        exe = os.path.abspath(sys.argv[0])
        with open(path, "w") as f:
            f.write(
                "[Desktop Entry]\nType=Application\n"
                f"Name={APP_NAME}\nComment=Battery levels of Bluetooth devices in the panel\n"
                f"Exec={sys.executable} {exe}\nIcon=bluetooth\nTerminal=false\n"
                "X-GNOME-Autostart-enabled=true\nX-KDE-autostart-after=panel\n")
    elif os.path.exists(path):
        os.remove(path)


# --------------------------------------------------------------------------- main


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    if args and args[0] in ("--list", "--json"):
        from PyQt6.QtCore import QCoreApplication
        _ = QCoreApplication(sys.argv)
        devs = read_devices(QDBusConnection.systemBus())
        if args[0] == "--json":
            print(json.dumps([d.__dict__ | {"kind": d.kind} for d in devs], indent=2))
        else:
            for d in devs or []:
                print(f"{d.percentage:>3}%  {d.kind:<10}  {d.name}  [{d.address}]")
            if not devs:
                print("No connected Bluetooth devices are reporting battery.")
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setQuitOnLastWindowClosed(False)
    app.setQuitLockEnabled(False)  # hiding the last tray icon must not end the app

    session = QDBusConnection.sessionBus()
    if not session.registerService(BUS_NAME):
        print(f"{APP_NAME} is already running.", file=sys.stderr)
        return 1

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray / StatusNotifier host found.", file=sys.stderr)

    _tray = BatteryTray(app)  # noqa: F841 (keep a reference)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
