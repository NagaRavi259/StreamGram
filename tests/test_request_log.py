"""Offline tests for the monthly request-log rotation (no network, temporary directories only).

Run:  python tests/test_request_log.py   (or: pytest tests)
"""
import csv
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from streamgram.utils.request_log import RequestLog, HEADER  # noqa: E402

H = b"Timestamp,IP Address,Endpoint\r\n"


def rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def body(path):
    with open(path, "rb") as f:
        f.readline()
        return f.read()


def test_request_log():
    # ---- migrate: an old file spanning several months is split byte for byte
    d = tempfile.mkdtemp()
    path = os.path.join(d, "request_logs.csv")
    june = b"2025-06-13T11:08:00.493262,1.1.1.1,http://x/a\r\n2025-06-30T23:59:59.000001,1.1.1.1,http://x/b\r\n"
    odd = b'2025-07-02T00:00:00.000001,2.2.2.2,"http://x/c\r\nHost: injected"\r\n'  # one row spanning two lines
    july = b"2025-07-31T12:00:00.000000,2.2.2.2,http://x/d\r\n"
    cur = b"2026-10-01T00:00:00.000000,3.3.3.3,http://x/e\r\n2026-10-04T00:07:30.134208,3.3.3.3,http://x/f\r\n"
    open(path, "wb").write(H + june + odd + july + cur)
    original = open(path, "rb").read()
    log = RequestLog(path)
    counts = log.migrate()
    assert counts == {"2025-06": 2, "2025-07": 2, "2026-10": 2}, counts
    assert open(log.archive_path("2025-06"), "rb").read() == H + june
    assert open(log.archive_path("2025-07"), "rb").read() == H + odd + july      # multi-line row kept whole
    assert open(path, "rb").read() == H + cur                                     # newest month stays in place
    joined = body(log.archive_path("2025-06")) + body(log.archive_path("2025-07")) + body(path)
    assert joined == original[len(H):], "no row may be lost or altered"
    assert not [f for f in os.listdir(d) if f.endswith(".part")]
    assert log.migrate() == {}                                                     # idempotent
    assert sorted(os.listdir(d)) == ["request_logs.csv", "request_logs_2025-06.csv", "request_logs_2025-07.csv"]

    # ---- migrate: nothing to do for a single-month, empty or missing file
    for content in (H + cur, H, b""):
        d2 = tempfile.mkdtemp(); p2 = os.path.join(d2, "request_logs.csv"); open(p2, "wb").write(content)
        assert RequestLog(p2).migrate() == {} and open(p2, "rb").read() == content and os.listdir(d2) == ["request_logs.csv"]
    assert RequestLog(os.path.join(tempfile.mkdtemp(), "nope.csv")).migrate() == {}

    # ---- append: same month appends, a new month rotates, header always present
    d3 = tempfile.mkdtemp(); p3 = os.path.join(d3, "request_logs.csv"); log3 = RequestLog(p3)
    log3.append(["2026-09-29T10:00:00.000000", "9.9.9.9", "http://x/1"])
    log3.append(["2026-09-30T23:59:59.999999", "9.9.9.9", "http://x/2"])
    assert rows(p3) == [HEADER, ["2026-09-29T10:00:00.000000", "9.9.9.9", "http://x/1"], ["2026-09-30T23:59:59.999999", "9.9.9.9", "http://x/2"]]
    log3.append(["2026-10-01T00:00:00.000000", "8.8.8.8", "http://x/3"])           # first request of October
    assert len(rows(log3.archive_path("2026-09"))) == 3 and rows(p3) == [HEADER, ["2026-10-01T00:00:00.000000", "8.8.8.8", "http://x/3"]]
    log3.append(["2026-10-02T00:00:00.000000", "8.8.8.8", "http://x/4"])
    assert len(rows(p3)) == 3 and len(rows(log3.archive_path("2026-09"))) == 3
    log3.append(["2026-09-30T23:59:59.000000", "7.7.7.7", "http://x/late"])         # a late row never rotates backwards
    assert len(rows(p3)) == 4

    # ---- a restart in a later month rotates what an earlier process wrote
    log3b = RequestLog(p3)
    log3b.append(["2026-11-05T00:00:00.000000", "6.6.6.6", "http://x/5"])
    assert len(rows(log3b.archive_path("2026-10"))) == 4 and rows(p3)[1][1] == "6.6.6.6"
    assert sorted(f for f in os.listdir(d3)) == ["request_logs.csv", "request_logs_2026-09.csv", "request_logs_2026-10.csv"]

    # ---- rotating into an archive that already exists merges instead of overwriting
    d4 = tempfile.mkdtemp(); p4 = os.path.join(d4, "request_logs.csv"); log4 = RequestLog(p4)
    open(log4.archive_path("2026-09"), "wb").write(H + b"2026-09-01T00:00:00.000000,1.1.1.1,http://x/old\r\n")
    open(p4, "wb").write(H + b"2026-09-20T00:00:00.000000,2.2.2.2,http://x/newer\r\n")
    log4.append(["2026-10-01T00:00:00.000000", "3.3.3.3", "http://x/oct"])
    merged = rows(log4.archive_path("2026-09"))
    assert [r[2] for r in merged[1:]] == ["http://x/old", "http://x/newer"] and merged[0] == HEADER

    # ---- ensure(): header on a new or empty file, existing data untouched
    d5 = tempfile.mkdtemp(); p5 = os.path.join(d5, "sub", "request_logs.csv"); log5 = RequestLog(p5)
    log5.ensure(); assert open(p5, "rb").read() == H
    log5.append(["2026-10-04T00:00:00.000000", "1.1.1.1", "http://x/z"]); log5.ensure()
    assert len(rows(p5)) == 2

    # ---- commas, quotes and unicode in the URL survive the round trip
    nasty = ["2026-10-04T00:00:01.000000", "1.2.3.4", 'http://x/a,b"c?q=é€']
    log5.append(nasty); assert rows(p5)[-1] == nasty

    # ---- a big file is processed as a stream and stays exact
    d6 = tempfile.mkdtemp(); p6 = os.path.join(d6, "request_logs.csv")
    with open(p6, "wb") as f:
        f.write(H)
        for i in range(60000):
            f.write(f"2026-{1 + i % 9:02d}-15T00:00:00.{i:06d},1.1.1.1,http://x/{i}\r\n".encode())
    before = open(p6, "rb").read()
    counts = RequestLog(p6).migrate()
    assert sum(counts.values()) == 60000 and len(counts) == 9
    assert sorted(os.listdir(d6)) == ["request_logs.csv"] + [f"request_logs_2026-0{m}.csv" for m in range(1, 9)]
    total = sum(len(rows(os.path.join(d6, f))) - 1 for f in os.listdir(d6))
    assert total == 60000
    print("request log ok: split, rotate, merge, header, unicode, 60k-row stream")


if __name__ == "__main__":
    test_request_log()
    print("REQUEST LOG TESTS PASS")
