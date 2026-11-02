"""In-memory stand-ins for the Google Drive / Sheets clients and a Playwright page."""

import re
from datetime import datetime, timezone


class FakeExec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


class FakeDriveFiles:
    """Keeps files in a dict; supports the queries screenshot_runner builds."""

    def __init__(self):
        self.files = {}  # id -> {"name", "parents", "mimeType"}
        self.calls = []
        self._next = 1
        self.create_failures = []  # exceptions raised by the next create() calls
        self.create_lands_then_fails = False
        self.page_size_cap = None

    def add(self, name, parent, mime="image/png", created=None):
        fid = f"f{self._next}"
        self._next += 1
        self.files[fid] = {
            "name": name,
            "parents": [parent],
            "mimeType": mime,
            "createdTime": created or datetime.now(timezone.utc),
        }
        return fid

    def _match(self, q, f):
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        if parent not in f["parents"]:
            return False
        m = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        if m and f["name"] != m.group(1).replace("\\'", "'").replace("\\\\", "\\"):
            return False
        m = re.search(r"name contains '((?:[^'\\]|\\.)*)'", q)
        if m and m.group(1) not in f["name"]:
            return False
        m = re.search(r"createdTime >= '([^']+)'", q)
        if m and f["createdTime"] < datetime.fromisoformat(m.group(1).replace("Z", "+00:00")):
            return False
        if "mimeType = 'application/vnd.google-apps.folder'" in q and f["mimeType"] != "application/vnd.google-apps.folder":
            return False
        return True

    def list(self, **kw):
        self.calls.append(("list", kw))

        def run():
            hits = [{"id": i, "name": f["name"]} for i, f in self.files.items() if self._match(kw["q"], f)]
            size = kw.get("pageSize", 100)
            if self.page_size_cap:
                size = min(size, self.page_size_cap)
            start = int(kw.get("pageToken") or 0)
            page = hits[start:start + size]
            res = {"files": page}
            if start + size < len(hits):
                res["nextPageToken"] = str(start + size)
            return res

        return FakeExec(run)

    def create(self, body, fields=None, supportsAllDrives=None, media_body=None):
        self.calls.append(("create", body))

        def run():
            if self.create_failures:
                exc = self.create_failures.pop(0)
                if self.create_lands_then_fails:
                    self.add(body["name"], body["parents"][0], body.get("mimeType", "image/png"))
                raise exc
            fid = self.add(body["name"], body["parents"][0], body.get("mimeType", "image/png"))
            return {"id": fid}

        return FakeExec(run)

    def delete(self, fileId, supportsAllDrives=None):
        self.calls.append(("delete", fileId))

        def run():
            if fileId not in self.files:
                from googleapiclient.errors import HttpError

                class Resp(dict):
                    status = 404
                    reason = "Not Found"

                raise HttpError(Resp(), b'{"error": {"code": 404}}')
            del self.files[fileId]
            return {}

        return FakeExec(run)


class FakeDrive:
    def __init__(self):
        self._files = FakeDriveFiles()

    def files(self):
        return self._files


class FakeValues:
    def __init__(self, ranges):
        self._ranges = ranges

    def get(self, spreadsheetId, range):
        def run():
            key = (spreadsheetId, range)
            if key not in self._ranges:
                raise RuntimeError(f"no range {key}")
            return {"values": self._ranges[key]}

        return FakeExec(run)


class FakeSpreadsheets:
    def __init__(self, ranges, titles):
        self._values = FakeValues(ranges)
        self._titles = titles

    def values(self):
        return self._values

    def get(self, spreadsheetId, fields=None):
        return FakeExec(lambda: {"properties": {"title": self._titles[spreadsheetId]}})


class FakeSheets:
    def __init__(self, ranges, titles=None):
        self._s = FakeSpreadsheets(ranges, titles or {})

    def spreadsheets(self):
        return self._s
