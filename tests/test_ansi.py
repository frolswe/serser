import asyncio
import os
import pty
import random
import unittest

from serser.ansi import AnsiParser, LineBuffer, MAX_LINE, is_bad
from serser.main import LineAssembler, SerialPort


class FakePort:
    name = "P"
    silence_timeout = 0.05


def collect(chunks, parser=None):
    """Feed text chunks through one assembler; return emitted (text, spans)."""
    out = []
    asm = LineAssembler(FakePort(), ">", out.append)
    for c in chunks:
        asm.feed_bytes(c if isinstance(c, bytes) else c.encode())
    asm.finish()
    return [(l.text, tuple((s, e, str(st)) for s, e, st in l.spans)) for l in out]


class DecoderTests(unittest.TestCase):
    def test_split_equivalence(self):
        data = "\x1b[0;35m[C][x]: hi\x1b[0m\nab\bc\x1b[K\n\x1b[38;2;1;2;3mz\x1b]0;title\x07\n".encode()
        whole = collect([data])
        for i in range(1, len(data)):
            self.assertEqual(collect([data[:i], data[i:]]), whole, i)

    def test_color_persists_and_resets(self):
        r = collect(["\x1b[31mred\nstill\x1b[0m plain\n"])
        self.assertEqual(r[0][1][0][2], "color(1)")
        self.assertTrue(r[1][1])
        self.assertEqual(r[1][1][0][1], 5)

    def test_ports_isolated(self):
        a, b = AnsiParser(), AnsiParser()
        a.feed("\x1b[31m")
        buf = LineBuffer()
        for op in b.feed("x"):
            buf.apply(*op[1:])
        self.assertEqual(buf.to_text()[1], ())

    def test_cr_is_line_break(self):
        self.assertEqual([t for t, _ in collect(["a\rb\n"])], ["a", "b"])

    def test_edits(self):
        self.assertEqual(collect(["abc\b\bX\n"])[0][0], "aXc")
        self.assertEqual(collect(["abcd\b\b\x1b[K\n"])[0][0], "ab")

    def test_fuzz(self):
        rnd = random.Random(1)
        asm_out = []
        asm = LineAssembler(FakePort(), ">", asm_out.append)
        alphabet = b"\x1b[;0123456789mKHJ?]\x07\r\n\x9b\xc2\xff abc\x18\x1a"
        for _ in range(3000):
            n = rnd.randint(1, 40)
            raw = bytes(rnd.choice(alphabet) if rnd.random() < .9 else rnd.randrange(256)
                        for _ in range(n))
            asm.feed_bytes(raw)
        asm.feed_bytes(b"\x1b]" + b"x" * 100000 + b"\n" + b"y" * 20000 + b"\n")
        asm.finish()
        self.assertTrue(asm_out)
        for l in asm_out:
            self.assertLessEqual(len(l.text), MAX_LINE)
            self.assertFalse(any(is_bad(c) for c in l.text))
            self.assertTrue(l.text)
            for s, e, _ in l.spans:
                self.assertTrue(0 <= s <= e <= len(l.text))


class OutputFileTests(unittest.TestCase):
    def test_both_files(self):
        import json, tempfile
        from serser.main import OutputFile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "log.txt")
            f = OutputFile(path, 1, hold=0)
            asm = LineAssembler(FakePort(), ">", f.add, f.add_raw)
            asm.feed_bytes(b"\x1b[31ma\r\nb\n")
            asm.finish()
            f.close()
            with open(os.path.join(d, "log.json")) as fh:
                data = json.load(fh)
            self.assertEqual("".join(e["data"] for e in data), "\x1b[31ma\r\nb\n")
            self.assertEqual(data[0]["direction"], "rx")
            with open(path) as fh:
                lines = fh.read().splitlines()
            self.assertEqual([l.split(" ", 2)[2] for l in lines], ["P> a", "P> b"])
            # a second run appends, keeping the JSON array valid
            f = OutputFile(path, 1, hold=0)
            asm = LineAssembler(FakePort(), "<", f.add, f.add_raw)
            asm.feed_bytes(b"c\n")
            asm.finish()
            f.close()
            with open(os.path.join(d, "log.json")) as fh:
                data = json.load(fh)
            self.assertEqual([e["data"] for e in data], ["\x1b[31ma\r\n", "b\n", "c\n"])
            with open(path) as fh:
                self.assertEqual(len(fh.read().splitlines()), 3)
            # an empty run leaves an empty array; unclosed arrays are continued
            for body, n in (("", 1), ("[]", 1), ("[\n]\n", 1),
                            ('[\n  {"data": "x"}', 2), ('[\n  {"data": "x"}\n]\n', 2)):
                with open(os.path.join(d, "t.json"), "w") as fh:
                    fh.write(body)
                f = OutputFile(os.path.join(d, "t.txt"), 1, hold=0)
                asm = LineAssembler(FakePort(), ">", f.add, f.add_raw)
                asm.feed_bytes(b"c\n")
                asm.finish()
                f.close()
                with open(os.path.join(d, "t.json")) as fh:
                    self.assertEqual(len(json.load(fh)), n, body)
            with open(os.path.join(d, "t.json"), "w") as fh:
                fh.write("hello")
            with self.assertRaises(ValueError):
                OutputFile(os.path.join(d, "t.txt"), 1)
            with self.assertRaises(ValueError):
                OutputFile(os.path.join(d, "x.json"), 1)


class RawTailTests(unittest.TestCase):
    def raws(self, chunks, times=None):
        from datetime import datetime, timedelta, timezone
        out = []
        asm = LineAssembler(FakePort(), ">", lambda l: None, lambda ts, p, a, d: out.append(d))
        t = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for i, c in enumerate(chunks):
            asm.feed_bytes(c, now=t + timedelta(seconds=(times[i] if times else 0)))
        asm.finish()
        return out

    def test_trailing_breaks_join_previous_line(self):
        self.assertEqual(self.raws([b"ets\r", b"\n\r\nrst\r"]), ["ets\r\n\r\n", "rst\r"])
        self.assertEqual(self.raws([b"a\r\n\n\nb\n"]), ["a\r\n\n\n", "b\n"])

    def test_gap_starts_new_burst(self):
        self.assertEqual(self.raws([b"a\n", b"\nb\n"], [0, 1]), ["a\n", "\nb\n"])

    def test_escape_only_line_kept(self):
        self.assertEqual(self.raws([b"\x1b[2J\nx\n"]), ["\x1b[2J\n", "x\n"])


class TimingTests(unittest.TestCase):
    def test_escape_bytes_count_for_timestamp(self):
        from datetime import datetime, timezone
        out = []
        asm = LineAssembler(FakePort(), ">", out.append)
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        asm.feed_bytes(b"\x1b[31mA\n", now=now, byte_time=0.001)
        asm.finish()
        # 6 bytes before the newline: the line starts 6 bytes before 'now'.
        self.assertLess(out[0].ts, now)


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_colors_and_ports(self):
        import argparse
        from serser.main import SerserTUI, parse_ports
        m1, s1 = pty.openpty()
        m2, s2 = pty.openpty()
        args = argparse.Namespace(ports=[f"A={os.ttyname(s1)}", f"B={os.ttyname(s2)}"],
                                  baud=None, default_baud=115200, gap_ms=50.0)
        app = SerserTUI(parse_ports(args))
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause(0.3)
            os.write(m1, b"\x1b[31mred\n")
            os.write(m2, b"plain\x1b[2J\x1b[?25l\n")
            await pilot.pause(0.6)
            log = app.query_one("#serial-log")
            texts = list(log.lines)
            self.assertTrue(any(t.endswith("A> red") for t in texts))
            self.assertTrue(any(t.endswith("B> plain") for t in texts))
            self.assertFalse(any("\x1b" in t for t in texts))
            by = {t.split()[-2]: sp for t, sp in zip(texts, log._row_spans) if len(t.split()) > 2}
            self.assertTrue(any(str(s[2]).startswith("color(1)") or "color(1)" in str(s[2])
                                for s in by["A>"]))
            self.assertFalse(any("color(1)" in str(s[2]) for s in by["B>"]))
            # hiding and showing a port keeps colours
            app.mgr.ports["A"].enabled = False
            app._redraw()
            app.mgr.ports["A"].enabled = True
            app._redraw()
            self.assertTrue(any("color(1)" in str(s[2]) for sp in log._row_spans for s in sp))


if __name__ == "__main__":
    unittest.main()
