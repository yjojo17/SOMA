"""
Raw archive for intercepted API responses.

One file per session. Each captured response is appended as a JSON line.
Finalised file is gzipped on session close. Path is recorded in sessions.raw_archive_path.

Layout:
    /home/yjojo/audit/data/raw/{account_id}/{session_id}.jsonl.gz
"""
import gzip
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional


RAW_ARCHIVE_ROOT = Path('/home/yjojo/audit/data/raw')


class RawArchive:
    def __init__(self, account_id: str, session_id: str):
        self.account_id = account_id
        self.session_id = session_id
        self.dir = RAW_ARCHIVE_ROOT / account_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tmp_path = self.dir / f'{session_id}.jsonl'
        self.final_path = self.dir / f'{session_id}.jsonl.gz'
        self._fh = self.tmp_path.open('a', encoding='utf-8')

    def append(self, url: str, response_body: dict) -> None:
        """Append one captured response. response_body is the parsed JSON dict."""
        record = {
            'ts': datetime.now().isoformat(),
            'url': url,
            'body': response_body,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False) + '\n')
        self._fh.flush()

    def close(self) -> Optional[str]:
        """Close file, gzip it, remove plaintext. Returns path to gzipped archive."""
        self._fh.close()
        if not self.tmp_path.exists() or self.tmp_path.stat().st_size == 0:
            if self.tmp_path.exists():
                self.tmp_path.unlink()
            return None

        with self.tmp_path.open('rb') as src, gzip.open(self.final_path, 'wb', compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        self.tmp_path.unlink()
        return str(self.final_path)