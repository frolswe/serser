#!/usr/bin/env python3
"""serser - Concurrent multi-serial-port reader with interactive TUI."""

import argparse
import asyncio
import bisect
import codecs
import heapq
import os
import platform as _platform_mod
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    from textual.app import App, ComposeResult
    from rich.style import Style
    from rich.text import Text
    from textual.binding import Binding
    from textual.geometry import Offset
    from textual.selection import Selection
    from textual.containers import Horizontal
    from textual.message import Message
    from textual.widgets import Static, Log, TextArea
except ImportError:
    print("ERROR: textual is required. Install with: pip install textual")
    raise SystemExit(1)

from serser.ansi import MAX_LINE, AnsiParser, LineBuffer, Span, scrub


# ==== Domain model ============================================================

class SerialPort:
    """A serial port configuration."""
    __slots__ = ("device", "baudrate", "bytesize", "parity", "stopbits",
                 "silence_timeout", "byte_time", "alias", "enabled")

    def __init__(self, device: str, baudrate: int, bytesize: int = 8,
                 parity: str = "N", stopbits: int = 1, min_gap: float = 0.0,
                 alias: str | None = None) -> None:
        self.device = device
        self.alias = alias
        self.enabled = True
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        bits_per_frame = 1 + bytesize + (0 if parity == "N" else 1) + stopbits
        self.byte_time = bits_per_frame / baudrate
        # USB bridges deliver bytes in chunks, so gaps below min_gap are unobservable.
        self.silence_timeout = max(5.0 * self.byte_time, min_gap)

    @property
    def framing(self) -> str:
        return f"{self.bytesize}{self.parity}{self.stopbits}"

    @property
    def name(self) -> str:
        return self.alias or os.path.basename(self.device)


_PALETTE = ["#00d7ff", "#ff5fd7", "#ffd700", "#5fff5f", "#ff8700", "#5f87ff", "#ff5f5f", "#af87ff"]


def port_colors(index: int) -> tuple[str, str]:
    """(vibrant, muted) colors for the port at *index*."""
    vibrant = _PALETTE[index % len(_PALETTE)]
    rgb = [int(vibrant[i:i + 2], 16) for i in (1, 3, 5)]
    muted = "#" + "".join(f"{(c * 45 + 110 * 55) // 100:02x}" for c in rgb)
    return vibrant, muted


class PortManager:
    def __init__(self, ports: list[SerialPort]) -> None:
        self.active_index = 0
        self.ports = {p.name: p for p in ports}

    @property
    def active_port(self) -> SerialPort | None:
        if not self.ports:
            return None
        return list(self.ports.values())[self.active_index]

    def cycle_active(self) -> str | None:
        if not self.ports:
            return None
        names = list(self.ports.keys())
        self.active_index = (self.active_index + 1) % len(names)
        return names[self.active_index]

    def style_for(self, name: str) -> str | None:
        """Rich style for port *name*: vibrant if active, muted otherwise."""
        for i, n in enumerate(self.ports):
            if n == name:
                vibrant, muted = port_colors(i)
                return f"bold {vibrant}" if i == self.active_index else muted
        return None

    def vibrant_style_for(self, name: str) -> str | None:
        """Rich style for port *name* that ignores which port is active."""
        for i, n in enumerate(self.ports):
            if n == name:
                return f"bold {port_colors(i)[0]}"
        return None

    def format_list(self):
        """Port list as styled text: the active port is marked with a '*'."""
        from rich.text import Text as RichText
        out = RichText()
        for i, name in enumerate(self.ports):
            if i:
                out.append(" ")
            port = self.ports[name]
            marker = "-" if not port.enabled else "*" if i == self.active_index else " "
            out.append(f"{marker}{name}", style=self.style_for(name))
        if not self.ports:
            out.append("(no ports)", style="dim")
        return out


# ==== CLI =====================================================================

_ALIAS_RE = re.compile(r"^([^\s=/:]+)=(.+)$")
_FRAMING_RE = re.compile(r"^(.+):([5-8])([NEOMSneoms])([12])$")


def parse_ports(args: argparse.Namespace) -> list[SerialPort]:
    devices = args.ports or []
    bauds = args.baud or []

    result_alias: list[str | None] = []
    result_dev: list[str] = []
    result_ba: list[int | None] = []
    result_fr: list[tuple[int, str, int]] = []

    for dev in devices:
        alias = None
        am = _ALIAS_RE.match(dev)
        if am:
            alias, dev = am.groups()
        result_alias.append(alias)
        framing = (8, "N", 1)
        m = _FRAMING_RE.match(dev)
        if m:
            dev = m.group(1)
            framing = (int(m.group(2)), m.group(3).upper(), int(m.group(4)))
        result_fr.append(framing)
        if ":" in dev and dev.rsplit(":", 1)[1].isdigit():
            d, b = dev.rsplit(":", 1)
            result_dev.append(d)
            result_ba.append(int(b))
        else:
            result_dev.append(dev)
            result_ba.append(None)

    if bauds:
        idx = 0
        for i in range(len(result_ba)):
            if result_ba[i] is None and idx < len(bauds):
                result_ba[i] = bauds[idx]
                idx += 1

    default_baud = args.default_baud
    ports = [SerialPort(d, (b or default_baud), *fr, min_gap=args.gap_ms / 1000.0, alias=a)
             for d, b, fr, a in zip(result_dev, result_ba, result_fr, result_alias)]
    names = [p.name for p in ports]
    for n in set(names):
        if names.count(n) > 1:
            raise SystemExit(f"ERROR: port name '{n}' is used more than once; "
                             f"give each port a unique name with NAME=PORT")
    return ports


def _is_usable(device: str, hwid: str) -> bool:
    """True if *device* is accessible and, for Linux UARTs, has real hardware behind it."""
    if _platform_mod.system() != "Linux":
        return True
    if not os.access(device, os.R_OK | os.W_OK):
        return False
    if not os.path.basename(device).startswith("ttyS"):
        return True
    try:
        import fcntl
        import struct
        fd = os.open(device, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
        try:
            # struct serial_struct starts with `int type`; 0 == PORT_UNKNOWN (no UART).
            buf = fcntl.ioctl(fd, 0x541E, b"\0" * 64)  # TIOCGSERIAL
        finally:
            os.close(fd)
        return struct.unpack("i", buf[:4])[0] != 0
    except OSError:
        return False


def discover_ports(show_all: bool = False) -> None:
    try:
        from serial.tools.list_ports import comports
    except ImportError:
        print("pyserial not installed.")
        return

    devices = sorted(comports(), key=lambda d: d.device)
    if not show_all:
        devices = [d for d in devices if _is_usable(d.device, d.hwid or "")]
    if not devices:
        print("No usable serial ports found." + ("" if show_all else " Use --all to list everything."))
        return
    for dev in devices:
        desc = dev.description or "(no description)"
        print(f"  {dev.device:<20} {desc}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="serser", 
                                description="Concurrent multi-serial-port reader.")
    p.add_argument("--discover", "-l", action="store_true", help="List usable ports and exit.")
    p.add_argument("--all", "-a", action="store_true",
                   help="With --discover, also list unusable/phantom ports.")
    p.add_argument("ports", nargs="*", metavar="PORT",
                   help="Serial port(s): [NAME=]PORT[:BAUD[:FRAMING]], e.g. DEV1=/dev/ttyUSB0:115200:8n1")
    p.add_argument("--baud", "-b", action="append", type=int, metavar="RATE", default=None,
                   help="Baud for ports without inline baud, in order.")
    p.add_argument("--default-baud", type=int, default=115200,
                   help="Default baud rate (default: 115200).")
    p.add_argument("--gap-ms", type=float, default=50.0, metavar="MS",
                   help="Minimum silence (ms) that starts a new timestamped line. USB "
                        "serial adapters deliver data in chunks, so keep this above the "
                        "adapter's latency (default: 50).")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                   help="Colour for piped output: 'always' prints the decoded device "
                        "colours as ANSI; otherwise plain text. The TUI always uses colour.")
    return p.parse_intermixed_args()


# ==== Line assembly ==========================================================

@dataclass(frozen=True)
class LogLine:
    """One finished line: full timestamp, origin, plain text and style spans.

    Status and error messages have no timestamp (``ts is None``) and no port.
    """

    ts: datetime | None
    port: str
    arrow: str
    text: str
    spans: tuple[Span, ...] = ()

    @classmethod
    def status(cls, text: str, style: str | None = None) -> "LogLine":
        text = scrub(text)
        spans = ((0, len(text), Style.parse(style)),) if style and text else ()
        return cls(None, "", "", text, spans)


_UTC_MIN = datetime.min.replace(tzinfo=timezone.utc)


def _byte_offsets(text: str) -> tuple["range | list[int]", int]:
    """UTF-8 byte offset of every character, and the total byte count."""
    if text.isascii():
        return range(len(text)), len(text)
    offsets, pos = [], 0
    for ch in text:
        offsets.append(pos)
        pos += len(ch.encode("utf-8", "replace"))
    return offsets, pos


class LineAssembler:
    """Groups one direction of one port's traffic into timestamped lines.

    Text is decoded by an :class:`AnsiParser`, so colours, simple line edits and
    swallowed control sequences are handled before a line is emitted. A new
    timestamp starts after a silence gap or a line break; the line is flushed
    when a break arrives or when the next burst begins. ``\\r`` and ``\\n`` both
    end a line.
    """

    def __init__(self, port: SerialPort, arrow: str, emit) -> None:
        self.port_name = port.name
        self.arrow = arrow
        self.timeout = port.silence_timeout
        self._emit_cb = emit
        self.parser = AnsiParser()
        self.buf = LineBuffer()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._ts: datetime | None = None
        self._last: datetime | None = None
        self._busy_until: datetime | None = None

    def feed_bytes(self, raw: bytes, now: datetime | None = None,
                   byte_time: float = 0.0) -> None:
        """Feed received bytes (a character split across chunks is kept whole)."""
        now = now or datetime.now(timezone.utc)
        self.feed(self._decoder.decode(raw), now, byte_time)

    def feed(self, text: str, now: datetime | None = None, byte_time: float = 0.0,
             forward: bool = False) -> None:
        """Add *text* that arrived (or, with *forward*, is being sent) at *now*.

        With *byte_time* > 0 each line's start is estimated assuming back-to-back
        bytes (escape sequences count as bytes). Received data counts back from
        the chunk's arrival (an upper bound, never before the previous chunk).
        Sent data counts forward from the start of transmission (a lower bound),
        where a send queued behind an unfinished one starts when that one ends.
        """
        now = now or datetime.now(timezone.utc)
        offsets, n = _byte_offsets(text)
        if forward:
            start = max(now, self._busy_until) if self._busy_until else now
            self._busy_until = start + timedelta(seconds=n * byte_time)
        floor = self._last
        if floor is not None and (now - floor).total_seconds() > self.timeout:
            self.flush()
            self.parser.abort()
            self._decoder.reset()
        self._last = now
        for i, op, a, b in self.parser.feed(text):
            if op == "nl":
                self.flush()
                continue
            if self._ts is None:
                off = offsets[i]
                if forward:
                    self._ts = start + timedelta(seconds=off * byte_time)
                else:
                    ts = now - timedelta(seconds=(n - off) * byte_time)
                    self._ts = max(ts, floor) if floor else ts
            if op != "seq":
                self.buf.apply(op, a, b)
                if len(self.buf) >= MAX_LINE:
                    self.flush()

    def flush(self) -> None:
        if len(self.buf) and self._ts is not None:
            text, spans = self.buf.to_text()
            if text:
                self._emit_cb(LogLine(self._ts, self.port_name, self.arrow, text, spans))
        self.buf.clear()
        self._ts = None


async def ordered_lines(q: "asyncio.Queue[LogLine]", hold: float = 0.25):
    """Yield queued lines sorted by timestamp.

    Each line is held for *hold* seconds so that lines from other ports or
    directions with earlier timestamps (back-dated rx, forward-dated tx) can
    still overtake it. Lines without a timestamp sort by arrival.
    """
    heap: list[tuple[datetime, int, float, LogLine]] = []
    seq = 0
    loop = asyncio.get_running_loop()
    while True:
        timeout = None
        if heap:
            timeout = max(0.0, heap[0][2] + hold - loop.time())
        try:
            line = await asyncio.wait_for(q.get(), timeout)
        except asyncio.TimeoutError:
            line = None
        if line is not None:
            seq += 1
            key = line.ts or max((h[0] for h in heap), default=_UTC_MIN)
            heapq.heappush(heap, (key, seq, loop.time(), line))
        while heap and heap[0][2] + hold <= loop.time():
            yield heapq.heappop(heap)[3]


class TimeCompactor:
    """Shortens timestamps for display, after lines have been put in order.

    A date change prints the date on its own line; otherwise only the clock is
    shown, with leading components equal to the previous line's blanked out.
    """

    def __init__(self) -> None:
        self._date: str | None = None
        self._prev: tuple[str, ...] | None = None

    def stamp(self, ts: datetime) -> tuple[str | None, str]:
        """Return (date line or None, shortened clock text) for *ts*."""
        date = ts.strftime("%Y-%m-%d")
        parts = (ts.strftime("%H"), ts.strftime("%M"), ts.strftime("%S"),
                 f"{ts.microsecond // 1000:03d}")
        new_date = None
        if date != self._date:
            new_date, self._date, self._prev = date, date, None
        clock = f"{parts[0]}:{parts[1]}:{parts[2]}.{parts[3]}"
        blank = 0
        if self._prev is not None:
            for i in range(3):  # "HH:", "MM:", "SS."
                if parts[i] != self._prev[i]:
                    break
                blank += 3
        self._prev = parts
        return new_date, " " * blank + clock[blank:]


Row = tuple[str, list[Span]]


def render_rows(line: LogLine, compact: TimeCompactor, name_width: int,
                prefix_style: Style | None = None) -> list[Row]:
    """Display rows (text plus style spans) for *line*, advancing *compact*."""
    if line.ts is None:
        return [(line.text, list(line.spans))] if line.text else []
    date, clock = compact.stamp(line.ts)
    rows: list[Row] = []
    if date:
        rows.append((date, []))
    name = line.port.ljust(name_width)
    head = f"{clock} {name}{line.arrow} "
    spans: list[Span] = []
    if prefix_style is not None:
        begin = len(clock) + 1
        spans.append((begin, begin + len(name) + len(line.arrow), prefix_style))
    off = len(head)
    spans.extend((s + off, e + off, st) for s, e, st in line.spans)
    rows.append((head + line.text, spans))
    return rows


def row_to_ansi(console, text: str, spans: list[Span]) -> str:
    """Render one row with our own SGR sequences (never the device's bytes)."""
    rich_text = Text(text, no_wrap=True)
    for start, end, style in spans:
        rich_text.stylize(style, start, end)
    with console.capture() as capture:
        console.print(rich_text, end="", soft_wrap=True, highlight=False, markup=False)
    return capture.get()

# ==== Serial reader task ======================================================

async def _run_reader(
    port: SerialPort, 
    out_q: "asyncio.Queue[LogLine]",
    transports: dict[str, asyncio.BaseTransport] | None = None,
) -> asyncio.Transport | None:
    """Open *port*, push timestamped lines to *out_q* until cancelled.

    The open transport is registered in *transports* (keyed by port name) so
    callers can write to it. Returns the transport on success, else ``None``.
    """
    import serial_asyncio

    class _ReadProto(asyncio.Protocol):
        def __init__(self) -> None:
            self.transport: asyncio.BaseTransport | None = None
            self.rx = LineAssembler(port, ">", out_q.put_nowait)

        def connection_made(self, transport) -> None:  # type: ignore[override]
            self.transport = transport
            if transports is not None:
                transports[port.name] = transport
            gap_ms = port.silence_timeout * 1000
            out_q.put_nowait(LogLine.status(
                f"[INFO] {port.name} @ {port.baudrate} {port.framing} (gap:{gap_ms:.0f}ms)",
                "dim"))

        def data_received(self, raw: bytes) -> None:
            if raw:
                self.rx.feed_bytes(raw, byte_time=port.byte_time)

        def flush_remainder(self) -> None:
            """Flush buffer before disconnecting."""
            self.rx.flush()

    loop = asyncio.get_running_loop()

    try:
        _, proto_instance = await serial_asyncio.create_serial_connection(
            protocol_factory=lambda: _ReadProto(), url=port.device,
            baudrate=port.baudrate, bytesize=port.bytesize,
            parity=port.parity, stopbits=port.stopbits, loop=loop,
        )
    except Exception as exc:
        out_q.put_nowait(LogLine.status(f"[ERROR] {port.device}: {exc}", "red"))
        return None

    try:
        while True:
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        proto_instance.flush_remainder()  # type: ignore[attr-defined]
        if transports is not None:
            transports.pop(port.name, None)
        if proto_instance.transport and not proto_instance.transport.is_closing():
            proto_instance.transport.close()
        raise

    return proto_instance.transport  # type: ignore[return-value]


# ==== Non-TTY fallback ======================================================

async def _run_non_tty(ports_cfg: list[SerialPort], color: str = "auto") -> None:
    """Plain stdout mode when redirected/piped.

    *color* is ``always`` to print our own SGR colour sequences; ``auto`` and
    ``never`` print plain text (stdout is not a terminal here).
    """
    out_q: "asyncio.Queue[LogLine]" = asyncio.Queue()
    width = max((len(p.name) for p in ports_cfg), default=0)
    reader_tasks = [asyncio.create_task(_run_reader(p, out_q)) for p in ports_cfg]

    console = None
    prefix_styles: dict[str, Style] = {}
    if color == "always":
        from rich.console import Console
        console = Console(force_terminal=True, color_system="truecolor",
                          file=open(os.devnull, "w"), width=10_000)
        mgr = PortManager(ports_cfg)
        prefix_styles = {p.name: Style.parse(mgr.vibrant_style_for(p.name) or "")
                         for p in ports_cfg}

    async def _drain() -> None:
        compact = TimeCompactor()
        async for line in ordered_lines(out_q):
            for text, spans in render_rows(line, compact, width, prefix_styles.get(line.port)):
                if console is not None:
                    text = row_to_ansi(console, text, spans)
                print(text, flush=True)

    drain_t = asyncio.create_task(_drain())
    all_t = [*reader_tasks, drain_t]

    try:
        await asyncio.gather(*all_t)
    except KeyboardInterrupt:
        for t in all_t:
            t.cancel()


# ==== Textual TUI =============================================================

class SendArea(TextArea):
    """Multi-line input: Enter submits, Shift+Enter (or Ctrl+J) inserts a newline."""

    class Submitted(Message):
        pass

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted())
        elif event.key in ("shift+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
        else:
            await super()._on_key(event)


_MIN_CONTRAST = 40


def _luma(color) -> float:
    r, g, b = color.get_truecolor()
    return 0.299 * r + 0.587 * g + 0.114 * b


class _SpanHighlighter:
    """Applies the stored style spans of the row being rendered."""

    def __init__(self, log: "OutputLog") -> None:
        self._log = log

    def __call__(self, text):
        log = self._log
        y = log._render_y
        if y is None or not 0 <= y < len(log._row_spans):
            return text
        bg = log._bg_luma
        for start, end, style in log._row_spans[y]:
            if bg is not None and style.color is not None and not style.reverse \
                    and abs(_luma(style.color) - bg) < _MIN_CONTRAST:
                style = style + Style(color="default")
            text.stylize(style, start, end)
        return text


class OutputLog(Log):
    """Log that never takes focus, so typing always goes to the input."""

    can_focus = False

    _anchor: tuple[int, int, int, int] | None = None  # row, start, end, chain
    _chain = 1
    _selection: Selection | None = None
    _last_down: tuple[float, tuple[int, int]] | None = None
    _render_y: int | None = None
    _bg_luma: float | None = None

    def __init__(self, *args, **kwargs) -> None:
        kwargs["highlight"] = True
        super().__init__(*args, **kwargs)
        self._row_spans: list[list[Span]] = []
        self.highlighter = _SpanHighlighter(self)

    def write_rows(self, rows: list[Row]) -> None:
        """Append rows of (plain text, style spans)."""
        if not rows:
            return
        self.write_lines([t for t, _ in rows])
        self._row_spans.extend(sp for _, sp in rows)
        extra = len(self._row_spans) - len(self._lines)
        if extra > 0:
            del self._row_spans[:extra]

    def clear(self):
        self._row_spans.clear()
        return super().clear()

    def _render_line_strip(self, y, rich_style):
        self._render_y = y
        try:
            self._bg_luma = _luma(rich_style.bgcolor) if rich_style.bgcolor else None
        except Exception:
            self._bg_luma = None
        return super()._render_line_strip(y, rich_style)

    def _span(self, row: int, col: int, chain: int) -> tuple[int, int]:
        """Word (chain 2) or whole line (chain 3) around *col*; empty span on blanks."""
        line = self._lines[row]
        if chain >= 3:
            return 0, len(line)
        if col >= len(line) or line[col].isspace():
            return col, col
        is_word = lambda c: c.isalnum() or c in "_-."
        kind = is_word(line[col])
        start = end = col
        while start > 0 and not line[start - 1].isspace() and is_word(line[start - 1]) == kind:
            start -= 1
        while end < len(line) and not line[end].isspace() and is_word(line[end]) == kind:
            end += 1
        return start, end

    def _pos(self, event) -> tuple[int, int] | None:
        if not self._lines:
            return None
        row = min(max(self.scroll_offset.y + event.y, 0), len(self._lines) - 1)
        return row, max(self.scroll_offset.x + event.x, 0)

    def _select_span(self, row: int, start: int, end: int, row2: int, start2: int, end2: int) -> None:
        first, last = sorted([(row, start), (row2, start2)])[0], max((row, end), (row2, end2))
        self._selection = Selection(Offset(first[1], first[0]), Offset(last[1], last[0]))
        self.screen.selections = {self: self._selection}

    async def _on_mouse_down(self, event) -> None:
        self._anchor = None
        pos = self._pos(event)
        now = time.monotonic()
        # MouseDown carries no click count, so count repeated presses here.
        if pos and self._last_down and now - self._last_down[0] < 0.5 \
                and abs(pos[0] - self._last_down[1][0]) == 0 \
                and abs(pos[1] - self._last_down[1][1]) <= 1:
            self._chain = self._chain % 3 + 1
        else:
            self._chain = 1
        self._last_down = (now, pos) if pos else None
        if self._chain in (2, 3) and pos and self.screen.allow_select:
            row, col = pos
            chain = self._chain
            start, end = self._span(row, col, chain)
            self._anchor = (row, start, end, chain)
            # Take over from Textual's per-character drag selection.
            self.screen._select_state = None
            self.capture_mouse()
            self._select_span(row, start, end, row, start, end)
            event.stop()
            return
        await super()._on_mouse_down(event)

    async def _on_mouse_move(self, event) -> None:
        pos = self._pos(event) if self._anchor else None
        if pos is None:
            return
        row, start, end, chain = self._anchor
        row2, col = pos
        start2, end2 = self._span(row2, col, chain)
        self._select_span(row, start, end, row2, start2, end2)

    async def _on_mouse_up(self, event) -> None:
        await super()._on_mouse_up(event)
        if self._anchor:
            self.release_mouse()
            self._anchor = None
            # The screen clears the selection on a click without movement.
            self.screen.selections = {self: self._selection}

    async def _on_click(self, event) -> None:
        """Keep Textual's select-all on double/triple click out; see _on_mouse_down."""
        if event.chain in (2, 3):
            event.prevent_default()


class SerserTUI(App):
    """Interactive serial-port multiplexer app."""

    BINDINGS = [
        ("escape", "quit", "Quit"),
        Binding("tab", "next_port", "Next port", priority=True),
        Binding("shift+tab", "toggle_port", "Enable/disable port", priority=True),
    ]

    CSS = """\
Screen { layout: vertical; }

#serial-log {
    height: 1fr;
    border: none;
    padding: 0;
    scrollbar-size: 0 0;
}

#input-box {
    background: black;
    height: auto;
    max-height: 11;
    padding: 0;
    margin: 0;
}

#ports-label {
    background: black;
    height: 1;
    width: auto;
    padding: 0 1 0 0;
}

#send-input {
    height: auto;
    min-height: 1;
    max-height: 10;
    width: 1fr;
    background: black;
    border: none;
    padding: 0;
    margin: 0;
    scrollbar-size: 0 0;
}

#send-input:focus {
    background: black;
    border: none;
}

#send-input > .text-area--cursor-line {
    background: black;
}
"""

    def __init__(self, ports_config: list[SerialPort]) -> None:
        super().__init__()
        self.ports = list(ports_config)
        self.mgr = PortManager(self.ports)
        self.out_q: "asyncio.Queue[LogLine] | None" = None
        self._scroll_pending = False
        self.transport_map: dict[str, asyncio.BaseTransport] = {}
        self._entries: list[tuple[datetime, int, LogLine]] = []  # (sort key, seq, line)
        self._compact = TimeCompactor()
        self._last_vis: tuple[datetime, int, LogLine] | None = None
        self.name_width = max((len(p.name) for p in self.ports), default=0)
        self.tx = {p.name: LineAssembler(p, "<", self._emit_line) for p in self.ports}
        self._prefix = {p.name: Style.parse(self.mgr.vibrant_style_for(p.name) or "")
                        for p in self.ports}

    def _emit_line(self, line: LogLine) -> None:
        assert self.out_q is not None
        self.out_q.put_nowait(line)

    # layout
    def compose(self) -> ComposeResult:
        yield OutputLog(id="serial-log", max_lines=1000, auto_scroll=True, highlight=True)
        with Horizontal(id="input-box"):
            yield Static("", id="ports-label")
            yield SendArea(id="send-input", compact=True, soft_wrap=True,
                           show_line_numbers=False,
                           placeholder="Enter sends, Shift+Enter adds a line")

    # startup wiring  
    def on_mount(self) -> None:
        self.out_q = asyncio.Queue()
        self.query_one("#send-input").focus()
        
        import serial_asyncio as _  # prime the module
        
        for port in self.ports:
            task = asyncio.create_task(_run_reader(port, self.out_q, self.transport_map))

        self.run_worker(self._drain_output())
        self._refresh_footer()
        self.set_interval(0.5, self._refresh_footer)

    def _display_rows(self, compact: "TimeCompactor", line: LogLine) -> list[Row]:
        return render_rows(line, compact, self.name_width, self._prefix.get(line.port))

    def _is_visible(self, entry) -> bool:
        port = self.mgr.ports.get(entry[2].port)
        return port is None or port.enabled

    def _rows_for(self, entries) -> list[Row]:
        compact = TimeCompactor()
        return [r for e in entries if self._is_visible(e)
                for r in self._display_rows(compact, e[2])]

    def _redraw(self, keep_view: int | None = None) -> None:
        """Redraw the log from all stored lines of enabled ports.

        With *keep_view* (a scroll row) that position is kept; otherwise the
        view follows the end if it was at the end.
        """
        log = self.query_one("#serial-log", Log)
        at_end = log.scroll_y >= log.max_scroll_y
        rows = self._rows_for(self._entries)
        log.clear()
        log.write_rows(rows)
        self._compact = TimeCompactor()
        for e in self._entries:
            if self._is_visible(e):
                self._display_rows(self._compact, e[2])
        self._last_vis = next((e for e in reversed(self._entries) if self._is_visible(e)), None)
        if at_end or keep_view is None:
            self._scroll_pending = True
        else:
            log.scroll_to(y=max(0, keep_view), animate=False, immediate=True)

    async def _drain_output(self):
        """Store every line, show those of enabled ports as they arrive.

        A line stamped earlier than ones already shown is inserted at its place
        and the log is redrawn.
        """
        seq = 0
        while True:
            raw = await self.out_q.get()
            seq += 1
            log = self.query_one("#serial-log", Log)
            last = self._entries[-1][0] if self._entries else _UTC_MIN
            key = raw.ts or last
            entry = (key, seq, raw)
            visible = self._is_visible(entry)
            if self._last_vis is None or entry >= self._last_vis:
                bisect.insort(self._entries, entry)
                if visible:
                    self._last_vis = entry
                    log.write_rows(self._display_rows(self._compact, raw))
            else:
                old_y, old_count = log.scroll_y, len(log.lines)
                pos = bisect.bisect(self._entries, entry)
                bisect.insort(self._entries, entry)
                del self._entries[:-10000]
                if visible:
                    shift = len(self._rows_for(self._entries)) - old_count
                    before = len(self._rows_for(self._entries[:pos]))
                    self._redraw(keep_view=old_y + (shift if before <= old_y else 0))
            if self._scroll_pending:
                self._scroll_pending = False
                log.scroll_end(animate=False, immediate=True)

    def _refresh_footer(self) -> None:
        """Update port list display.""" 
        label = self.query_one("#ports-label", Static)
        text_obj = self.mgr.format_list()
        label.update(text_obj)

    def action_toggle_port(self) -> None:
        port = self.mgr.active_port
        if port is None:
            return
        port.enabled = not port.enabled
        self._refresh_footer()
        self._redraw()
        self.query_one("#serial-log", Log).scroll_end(animate=False, immediate=True)

    def action_next_port(self) -> None:
        self.mgr.cycle_active()
        self._refresh_footer()


    async def on_send_area_submitted(self, event: SendArea.Submitted) -> None:
        """Send the typed lines to the active port and echo them as sent data."""
        inp = self.query_one("#send-input", SendArea)
        self.query_one("#serial-log", Log).scroll_end(animate=False)
        text = inp.text.rstrip("\r\n")
        if not text.strip():
            return
        self._scroll_pending = True

        port = self.mgr.active_port
        transport = self.transport_map.get(port.name) if port else None
        if port is None or transport is None:
            self._emit_line(LogLine.status("[ERROR] no open port to send to", "red"))
            return

        payload = "".join(line + "\n" for line in text.splitlines())
        transport.write(payload.encode("utf-8"))  # type: ignore[attr-defined]
        self.tx[port.name].feed(payload, byte_time=port.byte_time, forward=True)
        inp.clear()


# ==== Entry point =============================================================

def main() -> None:
    """Main entry point."""
    args = parse_args()

    if args.discover:
        discover_ports(args.all)
        return

    if not args.ports:
        print("Error: at least one serial port is required. Use --discover to list ports.", file=sys.stderr)
        sys.exit(1)

    # Windows needs ProactorEventLoop for serial_asyncio
    if _platform_mod.system() == "Windows":
        try:
            loop = asyncio.ProactorEventLoop()  # type: ignore[no-untyped-call]
            asyncio.set_event_loop(loop)
        except Exception:
            pass

    ports_cfg = parse_ports(args)

    if sys.stdout.isatty():
        app = SerserTUI(ports_cfg)
        app.run()
    else:
        try:
            asyncio.run(_run_non_tty(ports_cfg, args.color))
        except KeyboardInterrupt:
            print("\nBye.", file=sys.stderr)


if __name__ == "__main__":
    main()
