"""Local immutable replay evidence. No database or exchange writes."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


def _json_value(value: Any):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported ledger value: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                       allow_nan=False, default=_json_value) + '\n').encode('utf-8')


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def source_decision(event: dict) -> dict:
    """Copy recorded identity only; missing historical fields stay unknown."""
    result = {key: event.get(key) for key in ('run_id', 'profile_id', 'symbol', 'scheduled_at')}
    result.update({key: event[key] for key in ('config_hash', 'git_commit') if key in event})
    return result


def write_trade_ledger(engine_result: dict, run_manifest: dict, output_dir: str) -> dict:
    meta = engine_result['engine_metadata']
    replay_id = meta['replay_run_id']
    if not isinstance(replay_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', replay_id):
        raise ValueError('Invalid replay_run_id for trade ledger')
    payload = {
        'schema_version': 1,
        'replay_run_id': replay_id,
        'engine_version': meta['engine_version'],
        'input_identity': meta.get('input_identity', {
            'manifest_sha256': meta.get('manifest_sha256'),
            'input_cutoff_at': meta.get('input_cutoff_at'),
        }),
        'run_manifest': run_manifest,
        'profiles': {pid: profile['trades'] for pid, profile in engine_result.items()
                     if pid != 'engine_metadata' and isinstance(profile, dict) and 'trades' in profile},
    }
    raw = canonical_bytes(payload)
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f'trades_{replay_id}.json'
    # Publish a fully fsynced inode atomically with link(2), which never replaces
    # an existing name. A crash before publication leaves only an ignored temp.
    fd, temporary = tempfile.mkstemp(prefix='.trade-ledger-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != raw:
                raise ValueError(f'Trade ledger conflict: {destination}')
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        os.unlink(temporary)
    return {'path': str(destination), 'sha256': hashlib.sha256(raw).hexdigest(),
            'schema_version': 1, 'replay_run_id': replay_id,
            'record_count': sum(len(rows) for rows in payload['profiles'].values())}


def read_trade_ledger(path: str | Path, expected_sha256: str) -> dict:
    """Verify the report's content hash before returning persisted evidence."""
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError(f'Trade ledger SHA-256 mismatch: {path}')
    payload = json.loads(raw)
    if payload.get('schema_version') != 1:
        raise ValueError('Unsupported trade ledger schema_version')
    return payload
