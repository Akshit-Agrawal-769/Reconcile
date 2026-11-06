"""Where the pipeline gets GSTR-2B data from.

Today that is a file the user uploaded. Live access to the GST portal goes through a
licensed GST Suvidha Provider (GSP); the interface is here so that source can be added
without touching the worker, but it is deliberately not implemented.
"""
import json
from typing import Optional, Protocol

from api.db import ApiError


class DataSource(Protocol):
    def fetch_2b(self, period_id: int, upload_id: Optional[int] = None) -> dict: ...


FETCH_UPLOAD = ("SELECT content FROM upload WHERE period_id = %s AND kind = 'GSTR2B' "
                "AND (%s::bigint IS NULL OR upload_id = %s::bigint) "
                "ORDER BY upload_id DESC LIMIT 1")


class FileUploadSource:
    """Reads the uploaded file (the job's upload, or the latest for the period). Runs on
    the caller's connection, so the upload table's row-level security applies."""

    def __init__(self, conn):
        self.conn = conn

    def fetch_2b(self, period_id, upload_id=None):
        row = self.conn.execute(FETCH_UPLOAD, (period_id, upload_id, upload_id)).fetchone()
        if row is None:
            raise ApiError(404, "no GSTR-2B upload for this period")
        return json.loads(row[0])


class GspSandboxSource:
    def __init__(self, conn=None):
        self.conn = conn

    def fetch_2b(self, period_id, upload_id=None):
        raise NotImplementedError("GSP access needs a licensed provider")
