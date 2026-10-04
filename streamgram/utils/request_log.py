"""Request log (CSV) with monthly rotation.

`logs/request_logs.csv` always holds the current month. When the first request of a new month arrives,
the file is renamed to `request_logs_YYYY-MM.csv` (named after the month it contains) and a fresh one
is started, so no single file grows without bound and nothing is ever deleted.

`migrate()` does the same once for an old file that spans several months: it is split into one file
per month, byte for byte, with the newest month left in place. It is safe to run repeatedly.
"""
import csv
import logging
import os
import re
import threading

logger = logging.getLogger("streamgram.utils.request_log")

HEADER = ["Timestamp", "IP Address", "Endpoint"]
_HEADER_BYTES = b"Timestamp,IP Address,Endpoint\r\n"
_STAMP = re.compile(rb"^(\d{4}-\d{2})-\d{2}T")
_TAIL_BYTES = 128 * 1024


class RequestLog:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._cached = (None, None)  # (path, month of the data in the current file)

    # ---- helpers
    def archive_path(self, month):
        base, ext = os.path.splitext(self.path)
        return f"{base}_{month}{ext or '.csv'}"

    def _file_month(self):
        """Newest month (YYYY-MM) seen near the end of the current file, or None if it has no rows."""
        if self._cached[0] == self.path and self._cached[1]:
            return self._cached[1]
        month = None
        if os.path.exists(self.path):
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(f.tell() - _TAIL_BYTES, 0))
                found = re.findall(rb"(?m)^(\d{4}-\d{2})-\d{2}T", f.read())
            month = max(found).decode() if found else None  # newest month, so a late row cannot mislabel the file
        self._cached = (self.path, month)
        return month

    def _archive_current(self, month):
        """Move the current file to its monthly name (merging if that file already exists)."""
        target = self.archive_path(month)
        if os.path.exists(target):
            with open(self.path, "rb") as src, open(target, "ab") as dst:
                src.readline()  # skip the header
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    dst.write(chunk)
            os.remove(self.path)
        else:
            os.replace(self.path, target)
        logger.info(f"Request log rotated to {os.path.basename(target)}")

    # ---- public
    def ensure(self):
        """Create the file with its header if it is missing or empty."""
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
                with open(self.path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(HEADER)

    def append(self, row):
        """Add one request. `row[0]` is the ISO timestamp; a new month rotates the file first."""
        month = str(row[0])[:7]
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            exists = os.path.exists(self.path) and os.path.getsize(self.path) > 0
            if exists:
                file_month = self._file_month()
                if file_month and month > file_month:
                    self._archive_current(file_month)
                    exists = False
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not exists:
                    writer.writerow(HEADER)
                writer.writerow(row)
            self._cached = (self.path, month if not exists else (self._cached[1] or month))

    def migrate(self):
        """Split an old multi-month file into monthly files. Returns {month: rows} (empty if nothing to do)."""
        with self._lock:
            if not os.path.exists(self.path):
                return {}
            months = []
            with open(self.path, "rb") as f:
                f.readline()
                for line in f:
                    m = _STAMP.match(line)
                    if m and (not months or months[-1] != m.group(1).decode()):
                        months.append(m.group(1).decode())
            distinct = sorted(set(months))
            if len(distinct) <= 1:
                return {}
            newest, counts, outs = distinct[-1], {}, {}
            tmp = lambda month: f"{self.archive_path(month)}.part"
            try:
                current = distinct[0]  # lines before the first timestamp belong to the oldest month
                with open(self.path, "rb") as f:
                    f.readline()
                    for line in f:
                        m = _STAMP.match(line)
                        if m:
                            current = m.group(1).decode()
                        if current not in outs:
                            outs[current] = open(tmp(current), "wb")
                            outs[current].write(_HEADER_BYTES)
                            counts[current] = 0
                        outs[current].write(line)
                        counts[current] += bool(m)
            finally:
                for out in outs.values():
                    out.close()
            # every row must have been written before anything is moved
            original_rows = sum(counts.values())
            with open(self.path, "rb") as f:
                f.readline()
                if sum(1 for line in f if _STAMP.match(line)) != original_rows:
                    raise RuntimeError("request log split verification failed; nothing was changed")
            for month in distinct:
                if month == newest:
                    continue
                target = self.archive_path(month)
                if os.path.exists(target):
                    with open(tmp(month), "rb") as src, open(target, "ab") as dst:
                        src.readline()
                        dst.write(src.read())
                    os.remove(tmp(month))
                else:
                    os.replace(tmp(month), target)
            os.replace(tmp(newest), self.path)  # last: the original is only replaced once everything else is in place
            self._cached = (None, None)
            logger.info(f"Request log split into {len(distinct)} monthly files ({original_rows} rows)")
            return counts
