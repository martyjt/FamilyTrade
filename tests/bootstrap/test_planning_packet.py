import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "docs" / "planning-manifest-v1.json"


def test_reviewed_packet_matches_its_manifest() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = manifest["files"]

    assert len(entries) == 37
    digest_lines: list[bytes] = []
    for entry in entries:
        content = (ROOT / entry["path"]).read_bytes()
        assert len(content) == entry["bytes"]
        actual_hash = hashlib.sha256(content).hexdigest()
        assert actual_hash == entry["sha256"]
        digest_lines.append(f"{entry['path']}\t{actual_hash}\t{len(content)}\n".encode())

    assert hashlib.sha256(b"".join(digest_lines)).hexdigest() == manifest["packet_sha256"]
