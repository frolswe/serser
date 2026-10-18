# serser

Concurrent multi-serial-port reader with per-port timestamped output. Reads from several serial ports asynchronously and prints lines as they arrive, grouping continuous data bursts under a single timestamp and emitting a fresh timestamp after any silence gap.

Cross-platform: Linux, macOS, Windows. Requires Python 3.10 or newer (`uv` fetches one if needed).

## Get Started

The only requirement is [uv](https://github.com/astral-sh/uv). Python, dependencies, and the virtual environment are handled automatically by `uv`.

```bash
uv tool install git+https://github.com/frolswe/serser
```

That's it — a `serser` command is now on your PATH. No pip, no system Python, no manual setup.

**Update**: `uv tool install git+https://github.com/frolswe/serser --force`  
**Uninstall**: `uv tool uninstall serser`

To run without installing:

```bash
uvx --from git+https://github.com/frolswe/serser serser /dev/ttyUSB0
```

This runs serser in a temporary isolated environment — nothing is left on your system afterwards.

## Usage

List available serial ports:

```bash
serser -l
```

Read from a single port (default baud 115200):

```bash
serser /dev/ttyUSB0     # Linux/macOS
serser COM3             # Windows
```

Read from multiple ports:

```bash
serser /dev/ttyUSB0 /dev/ttyUSB1
```

Per-port baud rate and framing (inline syntax `[NAME=]port[:baud[:framing]]`, framing = data bits, parity `N/E/O/M/S`, stop bits; default `8N1`):

```bash
serser /dev/ttyUSB0:9600 COM3:57600
serser /dev/ttyUSB0:115200:8n1
serser /dev/ttyUSB0 COM3 --baud 9600 --baud 57600
```

Override the default baud rate for all unspecified ports:

```bash
serser --default-baud 9600 /dev/ttyS0 /dev/ttyS1
```

### CLI Reference

| Argument | Short | Default | Description |
|----------|-------|---------|-------------|
| `--discover` | `-l` | — | List available serial ports and exit |
| `PORT...` | — | — | Serial port(s): `[NAME=]PORT[:BAUD[:FRAMING]]`, e.g. `DEV1=/dev/ttyUSB0:115200:8n1` |
| `--baud RATE` | `-b` *(repeatable)* | — | Baud rate for ports without inline baud, in order |
| `--default-baud NUM` | — | `115200` | Fallback baud rate when nothing else is specified |
| `--gap-ms MS` | — | `50` | Minimum silence that starts a new timestamped line (USB adapters deliver data in chunks) |
| `--output FILE` | `-o` | — | Save all traffic of all ports (including disabled ones) to `FILE` and to `FILE` with its extension replaced by `.json`; see below. Existing files are appended to |
| `--color auto\|always\|never` | — | `auto` | Piped output only: `always` prints the decoded device colors as ANSI; otherwise plain text. The TUI always shows colors |

## Output Format

Each line is prefixed with a timestamp and port name. Received lines use `>` and lines you sent use `<`. For display the timestamp is shortened: a date change prints the date on its own line, otherwise only the clock is shown, with leading components equal to the previous line's blanked out:

```
2026-06-02
14:32:10.123 ttyUSB0> sensor reading: temp=22.4
         205 ttyUSB1> heartbeat OK
      12.001 ttyUSB0< AT+STATUS
      15.678 ttyUSB0> sensor reading: temp=22.5
```

Port names are padded to the longest name so the data lines up (`ESP    > ...`, `ttyUSB1< ...`).

Internally every line keeps its full timestamp (used for ordering); only the display is shortened. Times are UTC.

### Timestamp Behavior

- **Continuous burst**: when data arrives at full rate (byte-to-byte gaps <= timeout), all characters accumulate on one line under the timestamp of the first byte.
- **Silence gap**: if no data arrives for `max(5 frame times, --gap-ms)` (default 50 ms), the current line is flushed and the next byte gets a new timestamp (measured from the previous chunk). The 5-frame-time term (`5 * 10 / baudrate` for 8N1, e.g. ~43 µs at 115200) is below what USB serial adapters can resolve: they buffer bytes and deliver them in chunks (e.g. FTDI's 16 ms latency timer), so the host only sees chunk arrival times. Lower `--gap-ms` for native UARTs (`/dev/ttyS*`) if you want finer gaps.
- **Line break in data**: when incoming data contains `\n` or `\r`, the accumulated buffer up to that point is printed immediately as one line, then resets for the next burst.
- **Several lines per chunk**: USB adapters deliver bytes in chunks, so each line's start is estimated by counting back from the chunk's arrival time, assuming back-to-back bytes (`bits-per-frame / baudrate` per byte; the chunk's last byte is assumed to have just finished, so the first byte's *start bit* is dated), and never earlier than the previous chunk. What remains is the adapter's buffering delay (e.g. FTDI's 16 ms latency timer), so the timestamp is an upper bound on when the first byte of the line really started.
- **Sent lines**: sent data is timestamped when it is handed to the port, then each line is moved forward by its byte position (`bits-per-frame / baudrate` per byte), so the timestamp is a lower bound on when its first byte left the UART. A send queued while a previous one is still transmitting starts when that one ends.
- **Directions**: each port tracks its own timestamps independently, and so does each direction of a port, each with its own burst/gap tracking.
- **Ordering**: because rx is back-dated and tx forward-dated, a line can arrive after lines with later timestamps. In the TUI lines are shown immediately and a late line is inserted at its timestamp position (the log is redrawn, last 1000 lines kept; if you were at the bottom it stays scrolled to the end, otherwise the same content stays in view). Piped output cannot be rewritten, so it holds lines for 250 ms and prints them sorted.

### Colors and control codes

Received and sent text is decoded by a per-port ANSI parser (`serser/ansi.py`) and re-rendered by serser, so the device's bytes never reach your terminal.

- Supported: SGR colors and attributes (16/256/truecolor, bold, dim, italic, underline, reverse, strike; blink/conceal ignored), `\b`, tab, and the in-line edits `ESC[nC/D/G`, `K`, `P`, `X`.
- Color state is kept per port and direction and persists across lines until reset (`ESC[0m`, `ESC c`) or reconnect. Sequences split across reads are handled.
- `\r`, `\n`, `\v` and `\f` each end a line. Further line breaks that follow within the silence gap (e.g. the `\n` of `\r\n`) add nothing to the display and are appended to the previous line's raw data in the JSON file. Cursor movement across lines, screen clears, OSC/DCS strings, private modes and other sequences are swallowed (they still count for timestamps). Any other control character is shown as `·`. Sequences and lines are length-capped. Foreground colors too close to the background are dropped for legibility.

### Output file

`-o FILE` always writes two files, `FILE` and the same name with its extension replaced by `.json` (`log.txt` gives `log.json`, `log` gives `log.json`), with everything from every port whether enabled or disabled, in both directions. Lines are written in timestamp order. Timestamps are UTC.

- `FILE`: plain text as shown in the output area (no colors), but each line has a full timestamp: `2026-10-18 19:21:11.081 A> hi`. Status and error lines are included.
- the `.json` file: an array of `{"timestamp", "port", "direction", "data"}` (`direction` is `rx` or `tx`). `data` is the raw text exactly as received or sent, including escape sequences and `\r`/`\n`; concatenating a port's entries per direction gives its stream. Invalid UTF-8 appears as U+FFFD. Status lines are not included. `FILE` itself must not end in `.json`.

Existing files are appended to, never overwritten: the text file gets the new lines at its end, and in an existing JSON file the closing `]` is removed and the new entries are added before a new one, so the array stays valid. If the JSON file does not contain an array (or is an unclosed array from a killed run, which is continued), serser exits with an error where it cannot continue. The array is closed when serser exits normally; after a kill it lacks the final `]`, which the next run repairs.

## Design

### Requirements

- Concurrent async reading from multiple serial ports given on the command line
- Cross-platform: Linux, macOS and Windows
- `uv` manages all Python dependencies automatically — the only user requirement is `uv` itself
- Per-port baud rate: positional ports may use inline baud or optional `--baud RATE` values (applied in order to ports without inline baud); default 115200
- Name ports with `NAME=PORT`, e.g. `serser DEV1=/dev/ttyUSB0 ISP=/dev/ttyUSB1:300`; the name replaces the device basename in the log prefix and port list (names must be unique)
- Inline baud/framing syntax also supported: `/dev/ttyUSB0:9600`, `/dev/ttyUSB0:115200:8n1`
- Silence timeout: `max(5 × bits-per-frame / baudrate, --gap-ms)` seconds (10 bits for 8N1; default floor 50 ms because USB adapters chunk data); each port tracks its own independently
- All output goes through a single shared async queue drained on the main thread by a Textual worker
- Interactive TUI via **Textual**: borderless scrolling serial log and an input box headed by the port line
- Log text is selectable: drag with the mouse, double-click a word or triple-click a line (dragging after either extends the selection by word or by line), then press `Ctrl+C` to copy; lines are not wrapped and scrollbars are hidden (scroll with the mouse wheel)
- Each port gets its own color (8-color palette, repeating). The active port is shown vibrant and bold with a leading `*`, the others in a muted version of their color with a leading space, so the width never changes when switching with Tab. In the log the `port>` / `port<` prefix always uses the vibrant color, regardless of the active port
- `<Tab>` cycles the active port forward through all connected ports
- `<Shift+Tab>` enables/disables the active port. A disabled port is marked `-` instead of `*`/space in the port list and its lines are hidden from the log, but its data is still received and stored (last 10000 lines); enabling it again shows the hidden lines at their timestamp positions. Sending to a disabled port still works; the sent line is stored but hidden like its received data
- The input box at the bottom: `<Enter>` sends the contents to the active serial port, one `\n`-terminated line per input line. `<Shift+Enter>` (or `<Ctrl+J>`, for terminals that send plain Enter for Shift+Enter) adds a line so several lines can be queued at once; the input grows up to 10 lines and shrinks back after sending
- Focus: the input box always keeps the cursor; clicking or selecting in the log never moves focus, so typing always goes to the input (selecting and `Ctrl+C` still copy from the log)
- Non-TTY fallback: when stdout is not a terminal (piped/redirected), the TUI is skipped and lines are printed to stdout in timestamp order (plain text unless `--color always`); there is no input, so nothing can be sent

### Terminal Layout

The borderless terminal has full-width rows: a scrolling serial I/O area that fills the available space, and an input box at the bottom: the port list (active one highlighted) on the left of the first input line, with the input area to its right growing downward with Shift+Enter.

### Data Flow

```
serial port hw
     │  bytes (asyncio Protocol.data_received callback)
     ▼
_run_reader()       ← one asyncio task per port
     │  LineAssembler (rx) → LogLine (put_nowait)      typed input → LineAssembler (tx)
     ▼
asyncio.Queue[LogLine]  ← single shared queue, all ports and both directions
     │  awaited by Textual worker on main thread
     ▼
OutputFile (all lines)  and  serial log widget (enabled ports only)
```

### Component Responsibilities

| Component | Responsibility |
|-----------|----------------|
| `SerialPort` | Device path, baud rate, silence timeout (`max(5 × frame bits / baudrate, --gap-ms)`), byte time |
| `PortManager` | List of ports, active index, `cycle_active()` on Tab |
| `LineAssembler` | One per port and direction (`>` rx, `<` tx); applies silence/line-break logic, decodes escapes and emits `LogLine`s (timestamp, port, text, style spans, raw data) |
| `AnsiParser` / `LineBuffer` (`ansi.py`) | Per-port escape decoding state machine and styled single-line buffer |
| `_run_reader()` | One asyncio task per port; Protocol feeds received bytes to its rx `LineAssembler` and registers the transport for sending |
| Shared output queue | Single `asyncio.Queue[LogLine]`; guarantees ordered, non-interleaved main-thread rendering |
| Textual drain worker | Awaits the queue on the main thread, stores every line, shows those of enabled ports in the log widget and hands every line to `OutputFile` |
| `OutputFile` | Writes the `-o` text and JSON files in timestamp order |
| Textual `TextArea` (`SendArea`) | Multi-line input; Tab cycles port, Enter sends all lines to active port's transport, Shift+Enter inserts a newline |

## Tests

```
python -m unittest discover -s tests
```
