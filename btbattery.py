#!/usr/bin/env python3
"""Bluetooth Battery — panel/tray icons showing battery % of connected Bluetooth devices.

Reads org.bluez.Battery1 straight from BlueZ over D-Bus (no vendor software),
shows one StatusNotifierItem (tray icon) per device, and sends a desktop
notification when a device runs low.

    btbattery.py            run the tray app
    btbattery.py --list     print connected devices and exit
    btbattery.py --json     print JSON (for bar widgets / scripts) and exit
    btbattery.py --history  print recorded battery history estimates and exit
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass

from PyQt6.QtCore import QMetaType, QObject, QPointF, QRectF, QSettings, Qt, QTimer, QVariant, pyqtSlot
from PyQt6.QtDBus import QDBusArgument, QDBusConnection, QDBusMessage, QDBusVariant
from PyQt6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QGridLayout, QHBoxLayout, QLabel, QMenu,
                             QMessageBox, QPushButton, QSystemTrayIcon, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

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


# --------------------------------------------------------------------------- history
#
# history.tsv, one line per sample:   unix_time \t address \t percent [\t name]
# A sample is written when the level changes, when a device (re)appears, and
# otherwise once an hour while connected, so gaps in the file mean "not seen".

HEARTBEAT = 3600            # write an unchanged level at most once an hour
GAP = 2 * 3600 + 600        # samples further apart than this = device wasn't seen
CHARGE_RISE = 8             # a rise of this many points (in one climb) = it was charged
KEEP_DAYS = 365


def history_path() -> str:
    base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return os.path.join(base, APP_ID, "history.tsv")


class History:
    def __init__(self, path: str | None = None):
        self.path = path or history_path()
        self.samples: dict[str, list[tuple[int, int]]] = {}
        self.names: dict[str, str] = {}
        self.present: set[str] = set()       # addresses seen on the previous record()
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                lines = f.readlines()
        except OSError:
            return
        cutoff = time.time() - KEEP_DAYS * 86400
        stale = False
        for line in lines:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            try:
                ts, addr, pct = int(parts[0]), parts[1], int(parts[2])
            except ValueError:
                continue
            if ts < cutoff:
                stale = True
                continue
            self.samples.setdefault(addr, []).append((ts, pct))
            if len(parts) > 3 and parts[3]:
                self.names[addr] = parts[3]
        for s in self.samples.values():
            s.sort()
        if stale:
            self._rewrite()

    def _rewrite(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            for addr, samples in self.samples.items():
                for i, (ts, pct) in enumerate(samples):
                    name = self.names.get(addr, "") if i == 0 else ""
                    f.write(f"{ts}\t{addr}\t{pct}\t{name}\n")
        os.replace(tmp, self.path)

    def record(self, devices: list[Device], now: int | None = None) -> bool:
        """Append samples for the connected devices. Returns True if anything was written."""
        now = int(now if now is not None else time.time())
        out = []
        for d in devices:
            s = self.samples.setdefault(d.address, [])
            last = s[-1] if s else None
            new_name = self.names.get(d.address) != d.name
            if (last is None or last[1] != d.percentage or now - last[0] >= HEARTBEAT
                    or d.address not in self.present or new_name):
                s.append((now, d.percentage))
                out.append(f"{now}\t{d.address}\t{d.percentage}\t{d.name if new_name else ''}\n")
                self.names[d.address] = d.name
        self.present = {d.address for d in devices}
        if out:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a") as f:
                    f.writelines(out)
            except OSError as e:
                print(f"history: cannot write {self.path}: {e}", file=sys.stderr)
        return bool(out)

    def clear(self, addr: str):
        self.samples.pop(addr, None)
        self.names.pop(addr, None)
        self._rewrite()

    def addresses(self) -> list[str]:
        return sorted(self.samples, key=lambda a: self.names.get(a, a).lower())


@dataclass
class Session:
    """One discharge: from a charge (or the start of history) to the next charge (or now)."""
    start_ts: int
    start_pct: int
    end_ts: int
    end_pct: int
    after_charge: bool       # False when history began mid-discharge
    ongoing: bool            # True for the current, not yet recharged, session

    @property
    def duration(self) -> int:
        return self.end_ts - self.start_ts

    @property
    def drop(self) -> int:
        return self.start_pct - self.end_pct

    @property
    def rate(self) -> float | None:
        """Percent per day, or None when there's too little to go on."""
        if self.duration < 6 * 3600 or self.drop < 5:
            return None
        return self.drop / (self.duration / 86400)


@dataclass
class Analysis:
    sessions: list[Session]
    current_pct: int | None
    rate: float | None           # %/day used for estimates
    rate_from_past: bool         # True when the current session is too short and past ones were used
    time_left: float | None      # seconds
    full_charge: float | None    # seconds a 100 -> 0 discharge takes at `rate`
    last_charge_ts: int | None


def analyze(samples: list[tuple[int, int]], now: int | None = None) -> Analysis:
    now = int(now if now is not None else time.time())
    if not samples:
        return Analysis([], None, None, False, None, None, None)

    # Find charge events: a climb (consecutive non-decreasing samples) of >= CHARGE_RISE points.
    # Smaller wobbles are measurement noise. A session starts at the top of each climb.
    sessions: list[Session] = []
    start, after_charge, i, n = 0, False, 1, len(samples)
    while i < n:
        if samples[i][1] > samples[i - 1][1]:
            j = i
            while j + 1 < n and samples[j + 1][1] >= samples[j][1]:
                j += 1
            if samples[j][1] - samples[i - 1][1] >= CHARGE_RISE:
                a, b = samples[start], samples[i - 1]
                if i - 1 > start or after_charge:
                    sessions.append(Session(a[0], a[1], b[0], b[1], after_charge, False))
                start, after_charge = j, True
            i = j + 1
        else:
            i += 1
    a, b = samples[start], samples[-1]
    sessions.append(Session(a[0], a[1], b[0], b[1], after_charge, True))

    cur = sessions[-1]
    rate, from_past = cur.rate, False
    if rate is None:
        past = [s for s in sessions[:-1] if s.rate is not None]
        if past:
            rate = sum(s.drop for s in past) / (sum(s.duration for s in past) / 86400)
            from_past = True
    time_left = full = None
    if rate and rate > 0:
        # count down from the last reading, which may be a little while ago
        time_left = max(0.0, cur.end_pct / rate * 86400 - (now - cur.end_ts))
        full = 100 / rate * 86400
    last_charge = cur.start_ts if cur.after_charge else None
    return Analysis(sessions, samples[-1][1], rate, from_past, time_left, full, last_charge)


def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d >= 10:
        return f"{d} days"
    if d:
        return f"{d} d {h} h" if h else f"{d} days" if d > 1 else "1 day"
    if h:
        return f"{h} h {m} min" if m and h < 10 else f"{h} h"
    return f"{max(m, 1)} min"


def fmt_ago(ts: int | None, now: int | None = None) -> str:
    if ts is None:
        return "—"
    return fmt_duration((now or time.time()) - ts) + " ago"


def fmt_rate(rate: float | None) -> str:
    if rate is None:
        return "—"
    return f"~{rate:.0f}% per day" if rate >= 2 else f"~{rate * 7:.0f}% per week"


# --------------------------------------------------------------------------- history window


RANGES = (("24 hours", 86400), ("7 days", 7 * 86400), ("30 days", 30 * 86400), ("All", 0))


class HistoryChart(QWidget):
    """Battery level over time. Breaks the line where the device wasn't seen; hover for values."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.samples: list[tuple[int, int]] = []
        self.charges: list[int] = []
        self.t0 = self.t1 = 0
        self.low = 15
        self.hover: tuple[int, int] | None = None
        self.setMinimumHeight(220)
        self.setMouseTracking(True)

    def set_data(self, samples, charges, t0, t1, low):
        self.samples, self.charges, self.t0, self.t1, self.low = samples, charges, t0, t1, low
        self.hover = None
        self.update()

    def _plot_rect(self) -> QRectF:
        fm = self.fontMetrics()
        left = fm.horizontalAdvance("100%") + 10
        return QRectF(left, 8, self.width() - left - 12, self.height() - fm.height() - 20)

    def _x(self, r: QRectF, ts: int) -> float:
        return r.left() + (ts - self.t0) / max(1, self.t1 - self.t0) * r.width()

    @staticmethod
    def _y(r: QRectF, pct: int) -> float:
        return r.bottom() - pct / 100 * r.height()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        fg, accent = pal.windowText().color(), pal.highlight().color()
        faint = QColor(fg)
        faint.setAlphaF(0.12)
        dim = QColor(fg)
        dim.setAlphaF(0.6)
        r = self._plot_rect()
        fm = self.fontMetrics()

        # grid + y labels
        for pct in (0, 25, 50, 75, 100):
            y = self._y(r, pct)
            p.setPen(QPen(faint, 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(dim)
            p.drawText(QRectF(0, y - fm.height() / 2, r.left() - 6, fm.height()),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{pct}%")

        # x labels
        span = self.t1 - self.t0
        fmt = "%H:%M" if span <= 2 * 86400 else "%a %d" if span <= 10 * 86400 else "%b %d"
        ticks = max(2, min(7, int(r.width() // 110)))
        for k in range(ticks + 1):
            ts = self.t0 + span * k / ticks
            x = self._x(r, ts)
            label = time.strftime(fmt, time.localtime(ts))
            w = fm.horizontalAdvance(label)
            p.setPen(dim)
            p.drawText(QPointF(min(max(x - w / 2, r.left() - 4), r.right() - w + 4), r.bottom() + fm.ascent() + 6), label)

        # low threshold
        low_c = QColor("#e5484d")
        low_c.setAlphaF(0.55)
        p.setPen(QPen(low_c, 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(r.left(), self._y(r, self.low)), QPointF(r.right(), self._y(r, self.low)))

        pts = [(ts, pct) for ts, pct in self.samples if self.t0 - GAP <= ts <= self.t1]
        if not pts:
            p.setPen(dim)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "No readings in this period yet")
            p.end()
            return

        # split into runs where the device was continuously seen
        runs, run = [], [pts[0]]
        for prev, cur in zip(pts, pts[1:]):
            if cur[0] - prev[0] > GAP:
                runs.append(run)
                run = []
            run.append(cur)
        runs.append(run)

        p.save()
        p.setClipRect(r.adjusted(-2, -2, 2, 2))
        fill = QColor(accent)
        fill.setAlphaF(0.16)
        for run in runs:
            # Levels are step-wise: hold each value until the next reading.
            path = QPainterPath(QPointF(self._x(r, run[0][0]), self._y(r, run[0][1])))
            for (_, pa), (tb, pb) in zip(run, run[1:]):
                path.lineTo(self._x(r, tb), self._y(r, pa))
                path.lineTo(self._x(r, tb), self._y(r, pb))
            end_ts = min(self.t1, run[-1][0] + (60 if run is not runs[-1] else 0))
            path.lineTo(self._x(r, end_ts), self._y(r, run[-1][1]))
            area = QPainterPath(path)
            area.lineTo(self._x(r, end_ts), r.bottom())
            area.lineTo(self._x(r, run[0][0]), r.bottom())
            area.closeSubpath()
            p.fillPath(area, fill)
            p.setPen(QPen(accent, 2))
            p.drawPath(path)
            if len(run) == 1:
                p.setBrush(accent)
                p.drawEllipse(QPointF(self._x(r, run[0][0]), self._y(r, run[0][1])), 2.5, 2.5)
                p.setBrush(Qt.BrushStyle.NoBrush)

        # charge markers
        green = QColor("#30a46c")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(green)
        for ts in self.charges:
            if self.t0 <= ts <= self.t1:
                x = self._x(r, ts)
                tri = QPainterPath()
                tri.moveTo(x, r.top() + 1)
                tri.lineTo(x - 5, r.top() + 9)
                tri.lineTo(x + 5, r.top() + 9)
                tri.closeSubpath()
                p.drawPath(tri)
        p.restore()

        # hover readout
        if self.hover:
            ts, pct = self.hover
            x, y = self._x(r, ts), self._y(r, pct)
            p.setPen(QPen(dim, 1, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QPen(accent, 2))
            p.setBrush(pal.window().color())
            p.drawEllipse(QPointF(x, y), 4, 4)
            label = f"{pct}%  ·  {time.strftime('%a %d %b, %H:%M', time.localtime(ts))}"
            w, h = fm.horizontalAdvance(label) + 14, fm.height() + 8
            bx = min(max(x - w / 2, r.left()), r.right() - w)
            box = QRectF(bx, r.top() + 12, w, h)
            p.setPen(QPen(faint, 1))
            p.setBrush(pal.toolTipBase().color())
            p.drawRoundedRect(box, 4, 4)
            p.setPen(pal.toolTipText().color())
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        p.end()

    def mouseMoveEvent(self, e):
        r = self._plot_rect()
        pts = [s for s in self.samples if self.t0 <= s[0] <= self.t1]
        if not pts or not r.contains(e.position()):
            self.hover = None
        else:
            x = e.position().x()
            self.hover = min(pts, key=lambda s: abs(self._x(r, s[0]) - x))
        self.update()

    def leaveEvent(self, _):
        self.hover = None
        self.update()


class HistoryWindow(QWidget):
    def __init__(self, tray: "BatteryTray"):
        super().__init__()
        self.tray = tray
        self.setWindowTitle(f"Battery history — {APP_NAME}")
        self.setWindowIcon(QIcon.fromTheme("battery"))
        self.resize(720, 560)

        self.device = QComboBox()
        self.device.currentIndexChanged.connect(self.reload)
        self.ranges = QButtonGroup(self)
        range_box = QHBoxLayout()
        range_box.setSpacing(0)
        for i, (label, _) in enumerate(RANGES):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(i == 1)
            self.ranges.addButton(b, i)
            range_box.addWidget(b)
        self.ranges.idClicked.connect(lambda _: self.reload())

        top = QHBoxLayout()
        top.addWidget(self.device, 1)
        top.addLayout(range_box)

        self.stats = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        for col, (key, title) in enumerate((("now", "Now"), ("left", "Time left"), ("rate", "Drain"),
                                             ("full", "Full charge lasts"), ("charged", "Last charged"))):
            t = QLabel(title)
            t.setEnabled(False)
            v = QLabel("—")
            f = v.font()
            f.setPointSizeF(f.pointSizeF() * 1.35)
            f.setBold(True)
            v.setFont(f)
            grid.addWidget(t, 0, col)
            grid.addWidget(v, 1, col)
            self.stats[key] = v
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setEnabled(False)

        self.chart = HistoryChart()

        self.table = QTreeWidget()
        self.table.setHeaderLabels(["Charged", "Level", "Lasted", "Drain"])
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.header().setStretchLastSection(True)

        clear = QPushButton(QIcon.fromTheme("edit-clear-history"), "Clear this device's history…")
        clear.clicked.connect(self._clear)
        bottom = QHBoxLayout()
        self.where = QLabel()
        self.where.setEnabled(False)
        bottom.addWidget(self.where, 1)
        bottom.addWidget(clear)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addSpacing(6)
        lay.addLayout(grid)
        lay.addWidget(self.note)
        lay.addWidget(self.chart, 3)
        sessions_title = QLabel("Charges")
        f = sessions_title.font()
        f.setBold(True)
        sessions_title.setFont(f)
        lay.addWidget(sessions_title)
        lay.addWidget(self.table, 2)
        lay.addLayout(bottom)

    def open_for(self, addr: str | None):
        self._fill_devices(addr)
        self.reload()
        self.show()
        self.raise_()
        self.activateWindow()

    def _fill_devices(self, select: str | None = None):
        cur = select or self.device.currentData()
        hist = self.tray.history
        addrs = hist.addresses()
        for d in self.tray.devices:
            if d.address not in addrs:
                addrs.append(d.address)
        self.device.blockSignals(True)
        self.device.clear()
        connected = {d.address: d for d in self.tray.devices}
        for a in addrs:
            d = connected.get(a)
            name = d.name if d else hist.names.get(a, a)
            icon = _device_glyph(d.icon) if d else QIcon.fromTheme("bluetooth")
            self.device.addItem(icon, name + ("" if d else "  (not connected)"), a)
        idx = self.device.findData(cur)
        self.device.setCurrentIndex(max(0, idx))
        self.device.blockSignals(False)

    def reload(self):
        if self.device.count() != len(set(self.tray.history.addresses()) | {d.address for d in self.tray.devices}):
            self._fill_devices()
        addr = self.device.currentData()
        samples = self.tray.history.samples.get(addr, []) if addr else []
        now = int(time.time())
        a = analyze(samples, now)
        connected = any(d.address == addr for d in self.tray.devices)

        self.stats["now"].setText(f"{a.current_pct}%" if a.current_pct is not None else "—")
        self.stats["left"].setText(("~" + fmt_duration(a.time_left)) if a.time_left is not None and connected else "—")
        self.stats["rate"].setText(fmt_rate(a.rate))
        self.stats["full"].setText(("~" + fmt_duration(a.full_charge)) if a.full_charge else "—")
        self.stats["charged"].setText(fmt_ago(a.last_charge_ts, now))
        if not samples:
            note = "No readings yet. History is recorded while this app is running and the device is connected."
        elif a.rate is None:
            note = ("Not enough data for estimates yet. They appear once the level has dropped "
                    "a few percent over at least 6 hours.")
        elif a.rate_from_past:
            note = "The current charge is too recent to measure, so estimates use earlier charges."
        else:
            note = "Estimates are based on the current charge. Devices that report in coarse steps give rougher numbers."
        if not connected and samples:
            note = f"Not connected — last seen {fmt_ago(samples[-1][0], now)}. " + note
        self.note.setText(note)

        span = RANGES[max(0, self.ranges.checkedId())][1]
        t0 = now - span if span else (samples[0][0] if samples else now - 86400)
        if t0 >= now:
            t0 = now - 3600
        charges = [s.start_ts for s in a.sessions if s.after_charge]
        self.chart.set_data(samples, charges, t0, now, self.tray.low)

        self.table.clear()
        for s in reversed(a.sessions):
            when = time.strftime("%a %d %b %Y, %H:%M", time.localtime(s.start_ts)) if s.after_charge \
                else "before history began"
            level = f"{s.start_pct}% → {s.end_pct}%" + ("  (now)" if s.ongoing else "")
            lasted = fmt_duration(s.duration) + (" so far" if s.ongoing else "")
            QTreeWidgetItem(self.table, [when, level, lasted, fmt_rate(s.rate)])
        for c in range(3):
            self.table.resizeColumnToContents(c)
        self.where.setText(f"Saved in {self.tray.history.path}")

    def _clear(self):
        addr = self.device.currentData()
        if not addr:
            return
        name = self.device.currentText()
        if QMessageBox.question(self, "Clear history",
                                f"Delete all recorded battery history for {name}?") \
                == QMessageBox.StandardButton.Yes:
            self.tray.history.clear(addr)
            self._fill_devices()
            self.reload()


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
        self.history = History()
        self.history_window: HistoryWindow | None = None

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
        wrote = self.history.record(self.devices)
        hidden, style, low, fg = self.hidden, self.style, self.low, self.fg_color()
        visible = {d.path: d for d in self.devices if d.address not in hidden}

        # Show before hiding: Qt ends the app when its last visible tray icon is hidden.
        if not visible:
            self.placeholder.show()

        for path, d in visible.items():
            tray = self.icons.get(path)
            if tray is None:
                tray = QSystemTrayIcon(self)
                tray.setContextMenu(self._new_menu(d.address))
                tray.activated.connect(self._on_activated)
                self.icons[path] = tray
            sig = (d.percentage, d.name, d.icon, style, low, fg.name())
            if force or self.last_sig.get(path) != sig:
                tray.setIcon(build_icon(d, style, low, fg))
                self.last_sig[path] = sig
            tray.setToolTip(self._tooltip(d))
            if not tray.isVisible():
                tray.show()
            self._check_alert(d)

        # Hide (never destroy) icons whose device went away; they are re-shown if it returns.
        for path, tray in self.icons.items():
            if path not in visible and tray.isVisible():
                tray.hide()
        if visible and self.placeholder.isVisible():
            self.placeholder.hide()

        if self.history_window and self.history_window.isVisible() and (wrote or force):
            self.history_window.reload()

    def _tooltip(self, d: Device) -> str:
        lines = [d.name, f"{d.kind} · {d.percentage}%"]
        a = analyze(self.history.samples.get(d.address, []))
        if a.time_left is not None:
            lines.append(f"~{fmt_duration(a.time_left)} left ({fmt_rate(a.rate)})")
        return "\n".join(lines)

    def show_history(self, addr: str | None = None):
        if self.history_window is None:
            self.history_window = HistoryWindow(self)
        self.history_window.open_for(addr)

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
    def _new_menu(self, owner: str | None = None) -> QMenu:
        # Every tray icon needs its OWN QMenu. Sharing one menu between several
        # icons makes KDE's StatusNotifierItem free it when any one icon is
        # hidden, and the next click on another icon segfaults.
        m = QMenu()
        m.aboutToShow.connect(lambda m=m, o=owner: self._build_menu(m, o))
        self._build_menu(m, owner)
        self.menus.append(m)
        return m

    def _build_menu(self, m: QMenu, owner: str | None = None):
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
        m.addAction(QIcon.fromTheme("view-history"), "Battery history…", lambda: self.show_history(owner))
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
        au.setChecked(autostart_enabled())
        au.toggled.connect(set_autostart)
        m.addAction(QIcon.fromTheme("application-exit"), "Quit", self.app.quit)

    def _toggle_hidden(self, addr: str, hide: bool):
        h = self.hidden
        (h.add if hide else h.discard)(addr)
        self._set("hidden", sorted(h))


# --------------------------------------------------------------------------- autostart


SERVICE = f"{APP_ID}.service"


def _config_dir() -> str:
    return os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))


def autostart_path() -> str:
    return os.path.join(_config_dir(), "autostart", f"{APP_ID}.desktop")


def _has_service() -> bool:
    return os.path.exists(os.path.join(_config_dir(), "systemd", "user", SERVICE))


def autostart_enabled() -> bool:
    if _has_service():
        r = subprocess.run(["systemctl", "--user", "is-enabled", SERVICE],
                           capture_output=True, text=True)
        return r.stdout.strip() == "enabled"
    return os.path.exists(autostart_path())


def set_autostart(on: bool):
    # Installed via install.sh -> a systemd user service; otherwise an XDG autostart entry.
    if _has_service():
        subprocess.run(["systemctl", "--user", "enable" if on else "disable", SERVICE],
                       capture_output=True)
        return
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

    if args and args[0] == "--history":
        h = History()
        if not h.samples:
            print(f"No history recorded yet ({h.path}).")
        for addr in h.addresses():
            a = analyze(h.samples[addr])
            print(f"{h.names.get(addr, addr)}  [{addr}]")
            approx = lambda sec: "~" + fmt_duration(sec) if sec is not None else "—"
            print(f"  now {a.current_pct}%   drain {fmt_rate(a.rate)}   time left {approx(a.time_left)}"
                  f"   full charge {approx(a.full_charge)}   last charged {fmt_ago(a.last_charge_ts)}")
            print(f"  {len(h.samples[addr])} readings since "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(h.samples[addr][0][0]))}")
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
