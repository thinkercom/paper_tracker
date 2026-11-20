"""Persistent successful-paper IDs, scoped to the research profile."""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def paper_key(paper):
    parsed = urlsplit(paper["paper_url"])
    if parsed.hostname in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
        identifier = re.sub(r"^/(?:abs|pdf)/", "", parsed.path)
        identifier = re.sub(r"\.pdf$", "", identifier)
        identifier = re.sub(r"v\d+$", "", identifier)
        source = "arxiv:" + identifier
    else:
        source = paper["paper_url"].strip()
    profile = os.getenv("ANALYSIS_PROFILE", "v1") + "\n" + paper["research_interests"].strip()
    return hashlib.sha256((source + "\n" + profile).encode("utf-8")).hexdigest()


def load_state(path):
    if not path.exists():
        return {"version": 1, "completed": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("completed"), dict):
        raise ValueError("Invalid deduplication state; restore data/processed.json before running.")
    return state


def mark_completed(path, state, key, item):
    state["completed"][key] = {"scored_at": datetime.now(timezone.utc).isoformat(), **item}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class RunLock:
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ValueError(f"Another run holds {self.path.name}. If it crashed, verify it stopped before removing the lock.") from None
        with os.fdopen(handle, "w") as stream:
            stream.write(str(os.getpid()))
        return self

    def __exit__(self, *_):
        self.path.unlink(missing_ok=True)
