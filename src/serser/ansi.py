"""Decoding of ANSI/VT100 escape sequences from serial text.

Pure classes with no I/O. ``AnsiParser`` is a byte-boundary-safe state machine
that turns text into operations, ``LineBuffer`` applies them to one line of
styled cells. Colour state lives in the parser, so each port keeps its own.

Everything that is not a colour or a simple line edit is swallowed, and
nothing here ever produces output towards the device.
"""

from __future__ import annotations

from rich.color import Color
from rich.style import Style

MAX_LINE = 4096        # cells per line; longer lines are split
MAX_STR = 2048         # characters swallowed in one OSC/DCS/... string
MAX_PARAMS = 32        # CSI parameters considered
MAX_PARAM_CHARS = 64   # CSI parameter characters kept
TAB_WIDTH = 8
REPLACEMENT = "\u00b7"

Span = tuple[int, int, Style]
Op = tuple[int, str, object, object]  # (start index in chunk, op, arg, arg)


def is_bad(ch: str) -> bool:
    """True for characters that must never reach the terminal or the log."""
    o = ord(ch)
    return (o < 0x20 or 0x7F <= o <= 0x9F or o in (0x2028, 0x2029)
            or 0x202A <= o <= 0x202E or 0x2066 <= o <= 0x2069)


def scrub(text: str) -> str:
    """Replace every unsafe character (including newlines and tabs)."""
    return "".join(REPLACEMENT if is_bad(c) else c for c in text)


def _num(token: str) -> int:
    return min(int(token), 65535) if token.isascii() and token.isdigit() else 0


# ---- colour state ----------------------------------------------------------

_ATTR_ON = {1: "bold", 2: "dim", 3: "italic", 4: "underline", 7: "reverse", 9: "strike"}
_ATTR_OFF = {23: "italic", 24: "underline", 27: "reverse", 29: "strike"}
_STYLE_CACHE: dict[tuple, Style] = {}


class _Sgr:
    """Current SGR (colour/attribute) state of one port."""

    __slots__ = ("fg", "bg", "bold", "dim", "italic", "underline", "strike", "reverse")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.fg: Color | None = None
        self.bg: Color | None = None
        self.bold = self.dim = self.italic = False
        self.underline = self.strike = self.reverse = False

    def style(self) -> Style | None:
        key = (self.fg, self.bg, self.bold, self.dim, self.italic,
               self.underline, self.strike, self.reverse)
        if key == (None, None, False, False, False, False, False, False):
            return None
        style = _STYLE_CACHE.get(key)
        if style is None:
            if len(_STYLE_CACHE) > 512:
                _STYLE_CACHE.clear()
            style = _STYLE_CACHE[key] = Style(
                color=self.fg, bgcolor=self.bg, bold=self.bold or None,
                dim=self.dim or None, italic=self.italic or None,
                underline=self.underline or None, strike=self.strike or None,
                reverse=self.reverse or None)
        return style

    def apply(self, tokens: list[str]) -> None:
        """Apply the ';'-separated parameters of one SGR sequence."""
        if not tokens:
            tokens = ["0"]
        i, count = 0, len(tokens)
        while i < count:
            tok = tokens[i]
            i += 1
            if ":" in tok:
                self._colon(tok)
                continue
            n = _num(tok)
            if n == 0:
                self.reset()
            elif n in _ATTR_ON:
                setattr(self, _ATTR_ON[n], True)
            elif n == 22:
                self.bold = self.dim = False
            elif n in _ATTR_OFF:
                setattr(self, _ATTR_OFF[n], False)
            elif 30 <= n <= 37:
                self.fg = Color.from_ansi(n - 30)
            elif 90 <= n <= 97:
                self.fg = Color.from_ansi(n - 90 + 8)
            elif n == 39:
                self.fg = None
            elif 40 <= n <= 47:
                self.bg = Color.from_ansi(n - 40)
            elif 100 <= n <= 107:
                self.bg = Color.from_ansi(n - 100 + 8)
            elif n == 49:
                self.bg = None
            elif n in (38, 48):
                rest = tokens[i:]
                color = None
                if rest and _num(rest[0]) == 5 and len(rest) >= 2:
                    color, i = Color.from_ansi(min(_num(rest[1]), 255)), i + 2
                elif rest and _num(rest[0]) == 2 and len(rest) >= 4:
                    r, g, b = (min(_num(x), 255) for x in rest[1:4])
                    color, i = Color.from_rgb(r, g, b), i + 4
                else:
                    i = count  # malformed: ignore the rest
                if color is not None:
                    if n == 38:
                        self.fg = color
                    else:
                        self.bg = color
            # blink, conceal and everything else are ignored

    def _colon(self, tok: str) -> None:
        """Sub-parameter forms such as 38:2::r:g:b, 38:5:n and 4:3."""
        sub = tok.split(":")
        code = _num(sub[0])
        color = None
        if code in (38, 48):
            if len(sub) >= 3 and sub[1] == "5":
                color = Color.from_ansi(min(_num(sub[2]), 255))
            elif len(sub) >= 5 and sub[1] == "2":
                r, g, b = (min(_num(x), 255) for x in sub[-3:])
                color = Color.from_rgb(r, g, b)
            if color is not None:
                if code == 38:
                    self.fg = color
                else:
                    self.bg = color
        elif code == 4:
            self.underline = len(sub) < 2 or _num(sub[1]) != 0
        else:
            self.apply([sub[0]])


# ---- parser -----------------------------------------------------------------

_GROUND, _ESC, _ESC_INTER, _CSI, _STR, _STR_ESC = range(6)
_CANCEL = "\x18\x1a"


class AnsiParser:
    """Incremental escape-sequence parser for one direction of one port.

    ``feed`` returns operations ``(index, op, a, b)``, where *index* is the
    position in the chunk where the token started:

    * ``put`` *char*, *style*   write a character with a style
    * ``nl``                    end the line (``\\n``, ``\\r``, VT and FF)
    * ``move`` *delta*          relative cursor move (BS, CUF, CUB)
    * ``col`` *n*               absolute cursor column (CHA)
    * ``tab``                   cursor to the next tab stop
    * ``el`` *mode*             erase in line
    * ``dch`` / ``ech`` *n*     delete / erase characters
    * ``seq``                   a swallowed sequence (only matters for timing)

    A sequence split across chunks continues where it stopped.
    """

    def __init__(self) -> None:
        self.sgr = _Sgr()
        self._state = _GROUND
        self._start = 0
        self._clear_sequence()

    def _clear_sequence(self) -> None:
        self._params: list[str] = []
        self._inter = 0
        self._bad = False
        self._strlen = 0

    def abort(self) -> None:
        """Drop a half-received sequence (e.g. after a silence); colours stay."""
        self._state = _GROUND
        self._clear_sequence()

    def feed(self, text: str) -> list[Op]:
        ops: list[Op] = []
        self._start = 0  # a sequence continued from an earlier chunk starts at 0
        for i, ch in enumerate(text):
            state = self._state
            if state == _GROUND:
                self._ground(i, ch, ops)
            elif state == _CSI:
                self._csi(i, ch, ops)
            elif state == _ESC:
                self._esc(i, ch, ops)
            elif state == _STR:
                self._string(i, ch, ops)
            elif state == _STR_ESC:
                if ch == "\\":
                    self._finish(ops)
                else:  # ESC inside a string starts a new sequence
                    self._state = _ESC
                    self._esc(i, ch, ops)
            else:  # _ESC_INTER
                self._esc_inter(i, ch, ops)
        return ops

    # -- states --

    def _finish(self, ops: list[Op]) -> None:
        ops.append((self._start, "seq", None, None))
        self._state = _GROUND
        self._clear_sequence()

    def _restart(self, i: int, ch: str, ops: list[Op]) -> None:
        """Abandon the sequence and handle *ch* as ordinary input."""
        self._state = _GROUND
        self._clear_sequence()
        self._ground(i, ch, ops)

    def _ground(self, i: int, ch: str, ops: list[Op]) -> None:
        if ch == "\x1b":
            self._state = _ESC
            self._start = i
        elif ch in "\n\r\v\f":
            ops.append((i, "nl", None, None))
        elif ch == "\b":
            ops.append((i, "move", -1, None))
        elif ch == "\t":
            ops.append((i, "tab", None, None))
        elif is_bad(ch):
            ops.append((i, "put", REPLACEMENT, self.sgr.style()))
        else:
            ops.append((i, "put", ch, self.sgr.style()))

    def _esc(self, i: int, ch: str, ops: list[Op]) -> None:
        o = ord(ch)
        if ch == "[":
            self._state = _CSI
            self._clear_sequence()
        elif ch in "]PX^_":
            self._state = _STR
            self._clear_sequence()
        elif 0x20 <= o <= 0x2F:
            self._state = _ESC_INTER
            self._clear_sequence()
        elif 0x30 <= o <= 0x7E:
            if ch == "c":
                self.sgr.reset()
            self._finish(ops)
        elif ch in _CANCEL:
            self.abort()
        else:
            self._restart(i, ch, ops)

    def _esc_inter(self, i: int, ch: str, ops: list[Op]) -> None:
        o = ord(ch)
        if 0x20 <= o <= 0x2F:
            return
        if 0x30 <= o <= 0x7E:
            self._finish(ops)
        elif ch in _CANCEL:
            self.abort()
        else:
            self._restart(i, ch, ops)

    def _string(self, i: int, ch: str, ops: list[Op]) -> None:
        if ch == "\x07":
            self._finish(ops)
        elif ch == "\x1b":
            self._state = _STR_ESC
        elif ch in _CANCEL:
            self.abort()
        elif ch in "\n\r":
            self._restart(i, ch, ops)
        else:
            self._strlen += 1
            if self._strlen > MAX_STR:
                self.abort()

    def _csi(self, i: int, ch: str, ops: list[Op]) -> None:
        o = ord(ch)
        if 0x30 <= o <= 0x3F:
            if len(self._params) < MAX_PARAM_CHARS:
                self._params.append(ch)
            else:
                self._bad = True
        elif 0x20 <= o <= 0x2F:
            self._inter += 1
        elif 0x40 <= o <= 0x7E:
            self._dispatch(ch, ops)
            self._finish_quiet()
        elif ch in _CANCEL:
            self.abort()
        else:
            self._restart(i, ch, ops)

    def _finish_quiet(self) -> None:
        self._state = _GROUND
        self._clear_sequence()

    def _dispatch(self, final: str, ops: list[Op]) -> None:
        start = self._start
        params = "".join(self._params)
        ops.append((start, "seq", None, None))
        if self._bad or self._inter or params[:1] in ("<", "=", ">", "?"):
            return
        tokens = params.split(";")[:MAX_PARAMS]
        first = _num(tokens[0])
        if final == "m":
            self.sgr.apply(tokens if params else [])
        elif final == "K":
            ops.append((start, "el", first, None))
        elif final == "C":
            ops.append((start, "move", min(first or 1, MAX_LINE), None))
        elif final == "D":
            ops.append((start, "move", -min(first or 1, MAX_LINE), None))
        elif final in "G`":
            ops.append((start, "col", min(max(first, 1), MAX_LINE) - 1, None))
        elif final == "P":
            ops.append((start, "dch", min(first or 1, MAX_LINE), None))
        elif final == "X":
            ops.append((start, "ech", min(first or 1, MAX_LINE), None))


# ---- line buffer ---------------------------------------------------------------

class LineBuffer:
    """One line of styled cells with a cursor (overwrite mode)."""

    def __init__(self) -> None:
        self.chars: list[str] = []
        self.styles: list[Style | None] = []
        self.col = 0

    def __len__(self) -> int:
        return len(self.chars)

    def clear(self) -> None:
        self.chars.clear()
        self.styles.clear()
        self.col = 0

    def apply(self, op: str, a: object, b: object) -> None:
        if op == "put":
            self._put(a, b)  # type: ignore[arg-type]
        elif op == "move":
            self.col = max(0, min(self.col + a, MAX_LINE))  # type: ignore[operator]
        elif op == "col":
            self.col = max(0, min(a, MAX_LINE))  # type: ignore[type-var]
        elif op == "tab":
            self.col = min((self.col // TAB_WIDTH + 1) * TAB_WIDTH, MAX_LINE)
        elif op == "el":
            self._erase_line(a)  # type: ignore[arg-type]
        elif op == "dch":
            del self.chars[self.col:self.col + a]  # type: ignore[operator]
            del self.styles[self.col:self.col + a]  # type: ignore[operator]
        elif op == "ech":
            for j in range(self.col, min(self.col + a, len(self.chars))):  # type: ignore[operator]
                self.chars[j], self.styles[j] = " ", None

    def _put(self, ch: str, style: Style | None) -> None:
        while len(self.chars) < self.col:
            self.chars.append(" ")
            self.styles.append(None)
        if self.col == len(self.chars):
            self.chars.append(ch)
            self.styles.append(style)
        else:
            self.chars[self.col], self.styles[self.col] = ch, style
        self.col += 1

    def _erase_line(self, mode: int) -> None:
        if mode == 0:
            del self.chars[self.col:]
            del self.styles[self.col:]
        elif mode == 1:
            for j in range(min(self.col + 1, len(self.chars))):
                self.chars[j], self.styles[j] = " ", None
        elif mode == 2:
            self.chars.clear()
            self.styles.clear()

    def to_text(self) -> tuple[str, tuple[Span, ...]]:
        """The line as plain text plus merged style spans."""
        spans: list[Span] = []
        start, current = 0, None
        for j, style in enumerate(self.styles + [None]):
            if style != current:
                if current is not None:
                    spans.append((start, j, current))
                start, current = j, style
        return "".join(self.chars), tuple(spans)
