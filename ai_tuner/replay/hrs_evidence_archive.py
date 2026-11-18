"""Bounded, local-only HRS receipt archive (v2).

A seal proves integrity, never source authenticity. Each source independently
supplies its expected COUNT, query/account scope and time bounds. Whole original
JSON envelopes and original payload hashes survive recursive content-addressed
container deduplication. Receipt shards are externally sorted through a private
SQLite index; neither writer.finish nor readers flatten the capture into a list.
SQLite is disposable, outside the archive, and never part of copied/sealed files.

Writer: declare_source -> write_row (or await add) -> finish. Reader.open verifies
all files, receipts, independent counts, ordered digests and reachable CAS closure
before exposing indexed iterators. Callers should bind expected_manifest_sha256
from their retained descriptor. No network calls or source-DB access occur here; the caller owns source
collection, credential handling and authorization.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile

from .snapshot_sealer import _has_symlink_ancestor, _repo_destination_is_ignored

VERSION = 'hrs-evidence-archive-v2'
INVENTORY_NAME = 'hrs_evidence_archive_inventory.json'
MAX_RECEIPTS = 2_000_000
MAX_LOGICAL_BYTES = 256 * 1024**3
MAX_PHYSICAL_BYTES = 32 * 1024**3
MAX_SHARDS = 8192
MAX_SHARD_RECEIPTS = 50_000
MAX_SHARD_LOGICAL_BYTES = 64 * 1024**2
MAX_OBJECT_PACK_BYTES = 64 * 1024**2
MAX_INVENTORY_BYTES = 8 * 1024**2
MAX_DEPTH = 128
LIST_BLOCK_ITEMS = 256
MIN_FREE_DISK_BYTES = 256 * 1024**2
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_FAMILY = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MIN_TIME = -(2**63)


class ArchiveError(ValueError):
    """The unsealed capture or archive cannot meet its integrity contract."""


@dataclass(frozen=True)
class ArchiveLimits:
    max_receipts: int = MAX_RECEIPTS
    max_logical_bytes: int = MAX_LOGICAL_BYTES
    max_physical_bytes: int = MAX_PHYSICAL_BYTES
    max_shards: int = MAX_SHARDS
    shard_receipts: int = MAX_SHARD_RECEIPTS
    shard_logical_bytes: int = MAX_SHARD_LOGICAL_BYTES
    object_pack_bytes: int = MAX_OBJECT_PACK_BYTES
    inventory_bytes: int = MAX_INVENTORY_BYTES
    max_depth: int = MAX_DEPTH

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            cap = field.default
            if type(value) is not int or not 0 < value <= cap:
                raise ArchiveError('archive_limit_invalid:' + field.name)


def _require(condition, reason):
    if not condition:
        raise ArchiveError(reason)


def _value(value, depth=0):
    _require(depth <= MAX_DEPTH, 'archive_depth_limit')
    if isinstance(value, datetime):
        _require(value.tzinfo is not None and value.utcoffset() is not None, 'aware_timestamp_required')
        return value.isoformat()
    if isinstance(value, Decimal):
        _require(value.is_finite(), 'nonfinite_archive_value')
        return str(value)
    if isinstance(value, dict):
        _require(all(isinstance(key, str) for key in value), 'json_object_key_invalid')
        return {key: _value(item, depth+1) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_value(item, depth+1) for item in value]
    _require(value is None or isinstance(value, (str, bool, int, float)), 'json_value_invalid')
    return value


def _bytes(value, *, ascii=False):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=ascii, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ArchiveError('json_canonical_invalid') from exc


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _loads(data):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    def constant(_):
        raise ArchiveError('nonfinite_archive_value')
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ArchiveError('archive_json_invalid') from exc


def _time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = datetime.fromtimestamp(value / 1000, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
             'aware_timestamp_required')
    return value.astimezone(timezone.utc)


def _micros(value):
    delta = _time(value) - _EPOCH
    return (delta.days*86400+delta.seconds)*1_000_000+delta.microseconds


def _body(row):
    value = row.get('payload')
    return _loads(value) if isinstance(value, str) else value


def _bar_time(bar):
    return _micros(bar.get('open_time', bar.get('openTime'))) if isinstance(bar, dict) else _micros(bar[0])


def _source(metadata):
    _require(isinstance(metadata, dict), 'source_declaration_invalid')
    result = _value(metadata)
    _require(type(result.get('expected_count')) is int and 0 <= result['expected_count'] <= MAX_RECEIPTS,
             'source_expected_count_invalid')
    _require('account_id' in result and (result['account_id'] is None or isinstance(result['account_id'], str) and result['account_id']),
             'source_account_scope_missing')
    _require(isinstance(result.get('query_scope'), (dict, str)) and bool(result['query_scope']), 'source_query_scope_missing')
    _require('window_start' in result and 'window_end' in result,'source_time_bounds_missing')
    _require(_micros(result['window_start']) <= _micros(result['window_end']), 'source_time_bounds_invalid')
    if 'expected_item_count' in result:
        _require(type(result['expected_item_count']) is int and result['expected_item_count'] >= 0
                 and isinstance(result.get('item_field'), str), 'source_item_count_invalid')
    return result


def _metadata(family, row, source):
    _require(isinstance(row, dict), 'receipt_envelope_invalid')
    body = _body(row) if 'payload' in row else None
    if 'payload_sha256' in row:
        target = body if 'payload' in row else row.get('rows')
        _require(target is not None and isinstance(row['payload_sha256'], str)
                 and _HASH.fullmatch(row['payload_sha256']) and _digest(_bytes(target)) == row['payload_sha256'],
                 'original_payload_hash_mismatch')
    if family in ('equity_evidence','hrs_funding_observations'):
        _require(body is not None and isinstance(row.get('evidence_sha256'), str)
                 and _HASH.fullmatch(row['evidence_sha256'])
                 and _digest(_bytes(body, ascii=True)) == row['evidence_sha256'], 'original_equity_hash_mismatch')
    if family=='hrs_funding_observations':
        _require(set(row)=={'account_id','evidence_sha256','available_at','payload'} and
            isinstance(source['account_id'],str) and row['account_id']==source['account_id'],'funding_source_account_or_shape_invalid')
    if row.get('account_id') is not None and source['account_id'] is not None:
        _require(row['account_id'] == source['account_id'], 'source_account_mismatch')
    event = row.get('event_time')
    available = row.get('available_at')
    if family == 'decision_evidence':
        _require(all(row.get(key) for key in ('evidence_id', 'run_id', 'kind', 'event_time', 'available_at')),
                 'decision_receipt_identity_missing')
        identity = row['evidence_id']
    elif family in ('equity_evidence','hrs_funding_observations'):
        identity = row['evidence_sha256']
        event = row.get('available_at') if family=='hrs_funding_observations' else row.get('as_of', body.get('as_of') if isinstance(body, dict) else None)
        available = row.get('available_at', body.get('available_at') if isinstance(body, dict) else None)
    elif family == 'equity_checkpoints':
        event = row.get('as_of')
        available = row.get('available_at', row.get('created_at'))
        identity = row.get('checkpoint_id') or _bytes([row.get('account_id'), row.get('segment_id'), event, available]).decode()
    elif family == 'funding_observations':
        event = row.get('source_time')
        available = row.get('available_at', row.get('received_at'))
        identity = _bytes([row.get('attempt_id'), row.get('symbol')]).decode() if row.get('attempt_id') else _bytes([row.get('symbol'), event, available, row.get('next_funding_time')]).decode()
    elif family == 'market_bars':
        bars = row.get('rows', [row['bar']] if 'bar' in row else [])
        _require(isinstance(bars, list) and bool(bars), 'market_bar_receipt_empty')
        minimum = maximum = _bar_time(bars[0])
        for bar in bars:
            at = _bar_time(bar)
            minimum, maximum = min(minimum, at), max(maximum, at)
            _require(_micros(source['window_start']) <= at <= _micros(source['window_end']), 'source_time_scope_mismatch')
        event = _EPOCH + timedelta(microseconds=minimum)
        identity = row.get('evidence_id') or row.get('receipt_id') or _bytes([row.get('symbol'), row.get('interval'), row.get('source'), minimum, maximum,row.get('event_time'),available]).decode()
    else:
        identity = row.get('evidence_id', row.get('id'))
        if identity is None:identity=_digest(_bytes(row))
    identity = str(identity)
    _require(bool(identity), 'receipt_identity_missing')
    e = _micros(event) if event is not None else None
    a = _micros(available) if available is not None else None
    if family in ('decision_evidence', 'equity_evidence', 'equity_checkpoints', 'funding_observations','hrs_funding_observations'):
        _require(e is not None, 'source_event_time_missing')
    if e is not None:
        _require(_micros(source['window_start']) <= e <= _micros(source['window_end']), 'source_time_scope_mismatch')
    if e is not None and a is not None:
        _require(e <= a, 'receipt_availability_before_event')
    nested = source.get('item_field')
    items = len(row[nested]) if nested and isinstance(row.get(nested), list) else 0
    if nested:
        _require(isinstance(row.get(nested), list), 'source_item_field_missing')
    api_path = body.get('path') if isinstance(body, dict) else None
    return dict(identity=identity, event=e, available=a, run_id=row.get('run_id'), kind=row.get('kind'),
                path=api_path or row.get('interval'), symbol=row.get('symbol'), item_count=items)


def _key(metadata):
    return [metadata['event'] if metadata['event'] is not None else _MIN_TIME,
            metadata['available'] if metadata['available'] is not None else _MIN_TIME, metadata['identity']]


def _ordered(hasher, frame):
    hasher.update(_bytes([frame['identity'], frame['root'], frame['key']])+b'\n')


class _Index:
    def __init__(self):
        _require(shutil.disk_usage(tempfile.gettempdir()).free>=MIN_FREE_DISK_BYTES,'archive_free_disk_budget_exceeded')
        self.temp = tempfile.TemporaryDirectory(prefix='hrs-archive-index-')
        os.chmod(self.temp.name, 0o700)
        self.path = Path(self.temp.name)/'index.sqlite'
        self.db = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self.db.executescript('''PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE; PRAGMA cache_size=-8192;
            CREATE TABLE objects(h TEXT PRIMARY KEY, file TEXT, off INTEGER, size INTEGER, verified INTEGER DEFAULT 0, raw_sha TEXT);
            CREATE TABLE receipts(family TEXT, identity TEXT, root TEXT, bytes INTEGER, event INTEGER, available INTEGER,
                run_id TEXT, kind TEXT, path TEXT, symbol TEXT, item_count INTEGER, ordinal INTEGER,
                PRIMARY KEY(family,identity));
            CREATE INDEX receipt_order ON receipts(family,event,available,identity);
            CREATE INDEX receipt_available ON receipts(family,available,event,identity);
            CREATE INDEX receipt_runs ON receipts(family,run_id,kind,event,available,identity);
            CREATE INDEX receipt_kind ON receipts(family,kind,event,available,identity);
            CREATE UNIQUE INDEX receipt_ordinal ON receipts(ordinal);
            CREATE TABLE runs(run_id TEXT PRIMARY KEY, first INTEGER);
            CREATE INDEX run_order ON runs(first,run_id);
        ''')

    def disk_bytes(self):
        return self.db.execute('PRAGMA page_count').fetchone()[0]*self.db.execute('PRAGMA page_size').fetchone()[0]

    def limit_bytes(self,remaining):
        page=self.db.execute('PRAGMA page_size').fetchone()[0]
        _require(self.disk_bytes()<=remaining,'archive_physical_budget_exceeded')
        self.db.execute('PRAGMA max_page_count='+str(max(1,remaining//page)))

    def close(self):
        self.db.close()
        self.temp.cleanup()


class EvidenceArchiveWriter:
    """Single-writer streaming archive; limits may only lower fixed hard caps."""
    def __init__(self, root, *, source_metadata=None, limits=None):
        self.root = Path(root).absolute()
        self.inventory_path = self.root/INVENTORY_NAME
        self.limits = limits or ArchiveLimits()
        _require(not _has_symlink_ancestor(self.root), 'archive_symlink_path')
        _require(_repo_destination_is_ignored(self.root), 'archive_destination_not_private')
        _require(not self.root.exists() or not any(self.root.iterdir()), 'archive_destination_not_empty')
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        _require(shutil.disk_usage(self.root).free>=MIN_FREE_DISK_BYTES,'archive_free_disk_budget_exceeded')
        self._index = _Index()
        self._db = self._index.db
        self._sources = {}
        self._files = []
        self._file_hashes = {}
        self._pack = None
        self._pack_entry = None
        self._physical = self._logical = self._count = self._objects = 0
        self._failed = self._finished = self._closed = False
        self._descriptor = None
        try:
            self._budget()
            for family, metadata in (source_metadata or {}).items():
                self.declare_source(family, metadata)
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if not self._closed:
            if self._pack is not None:
                self._pack.close()
            self._index.close()
            self._closed = True

    def _budget(self, extra=0):
        remaining=self.limits.max_physical_bytes-self._physical-extra
        free=min(shutil.disk_usage(self.root).free,shutil.disk_usage(self._index.path.parent).free)
        pending=max(0,self._index.disk_bytes()-self._index.path.stat().st_size)+64*1024
        _require(free>=MIN_FREE_DISK_BYTES+pending+extra,'archive_free_disk_budget_exceeded')
        remaining=min(remaining,self._index.disk_bytes()+free-MIN_FREE_DISK_BYTES-pending-extra)
        self._index.limit_bytes(remaining)

    def declare_source(self, family, metadata):
        _require(not self._closed and not self._failed and not self._finished, 'archive_writer_not_open')
        _require(isinstance(family, str) and _FAMILY.fullmatch(family), 'source_family_invalid')
        _require(family not in self._sources, 'source_already_declared')
        self._sources[family] = _source(metadata)
        _require(len(_bytes(self._sources)) <= self.limits.inventory_bytes, 'source_inventory_limit')

    def _new_file(self, kind, name):
        _require(len(self._files) < self.limits.max_shards, 'archive_shard_budget_exceeded')
        handle = (self.root/name).open('xb')
        os.chmod(self.root/name, 0o600)
        entry = dict(path=name, kind=kind, row_count=0, physical_bytes=0, first_key=None, last_key=None)
        self._files.append(entry)
        self._file_hashes[name]=hashlib.sha256()
        return handle, entry

    def _append(self, handle, entry, data, key):
        self._budget(len(data))
        off = handle.tell()
        handle.write(data)
        self._file_hashes[entry['path']].update(data)
        self._physical += len(data)
        entry['physical_bytes'] += len(data)
        entry['row_count'] += 1
        if entry['first_key'] is None:
            entry['first_key'] = key
        entry['last_key'] = key
        return off

    def _encode(self, value, depth=0):
        _require(depth <= self.limits.max_depth, 'archive_depth_limit')
        if not isinstance(value, (dict, list)) and not (isinstance(value, str) and len(value) >= 4096):
            return [0, value]
        canonical = _bytes(value)
        digest = _digest(canonical)
        if self._db.execute('SELECT 1 FROM objects WHERE h=?', (digest,)).fetchone():
            return [1, digest]
        if isinstance(value, dict):
            node = ['d', [[key, self._encode(value[key], depth+1)] for key in sorted(value)]]
        elif isinstance(value, list):
            if len(value)>LIST_BLOCK_ITEMS:
                node=['L',dict(length=len(value),chunks=[self._encode(value[left:left+LIST_BLOCK_ITEMS],depth) for left in range(0,len(value),LIST_BLOCK_ITEMS)])]
            else:
                node = ['l', [self._encode(item, depth+1) for item in value]]
        else:
            node = ['s', value]
        encoded = _bytes(dict(h=digest, b=len(canonical), n=node))+b'\n'
        _require(len(encoded) <= self.limits.object_pack_bytes, 'archive_object_pack_limit')
        if self._pack is None or self._pack_entry['physical_bytes']+len(encoded) > self.limits.object_pack_bytes:
            if self._pack is not None:
                self._pack.close()
            self._pack, self._pack_entry = self._new_file('objects', f'hrs_archive_objects_{len(self._files):06d}.jsonl')
        off = self._append(self._pack, self._pack_entry, encoded, digest)
        self._db.execute('INSERT INTO objects(h,file,off,size) VALUES(?,?,?,?)', (digest,self._pack_entry['path'],off,len(encoded)))
        self._objects += 1
        return [1, digest]

    def write_row(self, family, row):
        _require(not self._closed and not self._failed and not self._finished, 'archive_writer_not_open')
        try:
            _require(family in self._sources, 'source_not_declared')
            row = _value(row)
            data = _bytes(row)
            _require(len(data) <= self.limits.shard_logical_bytes, 'receipt_logical_shard_limit')
            _require(self._count+1 <= self.limits.max_receipts, 'archive_receipt_budget_exceeded')
            _require(self._logical+len(data) <= self.limits.max_logical_bytes, 'archive_logical_budget_exceeded')
            metadata = _metadata(family, row, self._sources[family])
            _require(not self._db.execute('SELECT 1 FROM receipts WHERE family=? AND identity=?', (family,metadata['identity'])).fetchone(),
                     'duplicate_receipt_identity_conflict')
            term = self._encode(row)
            _require(term[0] == 1, 'receipt_root_invalid')
            self._db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (family,metadata['identity'],term[1],len(data),metadata['event'],metadata['available'],metadata['run_id'],metadata['kind'],metadata['path'],metadata['symbol'],metadata['item_count'],self._count))
            if family=='decision_evidence' and metadata['run_id'] is not None:
                self._db.execute('INSERT INTO runs VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET first=min(first,excluded.first)',(metadata['run_id'],metadata['event']))
            self._count += 1
            self._logical += len(data)
            if self._count % 1000 == 0:
                self._budget()
        except Exception as exc:
            self._failed = True
            if isinstance(exc, ArchiveError):
                raise
            raise ArchiveError('archive_write_failed:'+type(exc).__name__) from exc

    async def add(self, family, row):
        self.write_row(family, row)

    def finish(self):
        _require(not self._closed and not self._failed and not self._finished, 'archive_writer_not_open')
        try:
            _require(self._sources, 'source_declarations_missing')
            if self._pack is not None:
                self._pack.close()
                self._pack = None
            families = {}
            for family, source in sorted(self._sources.items()):
                count, items = self._db.execute('SELECT count(*),coalesce(sum(item_count),0) FROM receipts WHERE family=?',(family,)).fetchone()
                _require(count == source['expected_count'], 'source_count_mismatch:'+family)
                if 'expected_item_count' in source:
                    _require(items == source['expected_item_count'], 'source_item_count_mismatch:'+family)
                digest = hashlib.sha256()
                first = last = None
                logical = 0
                handle = entry = None
                try:
                    query = 'SELECT identity,root,bytes,event,available,run_id,kind,path,symbol,item_count,ordinal FROM receipts WHERE family=? ORDER BY event,available,identity'
                    for identity,root,size,event,available,run,kind,path,symbol,item_count,ordinal in self._db.execute(query,(family,)):
                        metadata = dict(identity=identity,event=event,available=available,run_id=run,kind=kind,path=path,symbol=symbol,item_count=item_count)
                        key = _key(metadata)
                        frame = dict(family=family,root=root,canonical_sha256=root,canonical_bytes=size,key=key,ordinal=ordinal,**metadata)
                        if handle is None or entry['row_count'] >= self.limits.shard_receipts or entry['logical_bytes']+size > self.limits.shard_logical_bytes:
                            if handle is not None:handle.close()
                            handle, entry = self._new_file('receipts', f'hrs_archive_receipts_{family}_{len(self._files):06d}.jsonl')
                            entry.update(family=family,logical_bytes=0)
                        self._append(handle,entry,_bytes(frame)+b'\n',key)
                        entry['logical_bytes'] += size
                        logical += size
                        if first is None:first=key
                        last=key
                        _ordered(digest,frame)
                finally:
                    if handle is not None:handle.close()
                families[family] = dict(row_count=count,item_count=items,logical_bytes=logical,first_key=first,last_key=last,ordered_sha256=digest.hexdigest())
            for entry in self._files:
                entry['sha256'] = _file_hash(self.root/entry['path'])
                _require(entry['sha256']==self._file_hashes[entry['path']].hexdigest(),'archive_file_changed_before_seal')
            _require({path.name for path in self.root.iterdir()}=={entry['path'] for entry in self._files},'archive_unlisted_file')
            manifest = dict(version=VERSION,source_attested=False,order_version='event_available_identity_v1',
                source_metadata=self._sources,families=families,files=self._files,receipt_count=self._count,
                logical_bytes=self._logical,physical_bytes=self._physical,object_count=self._objects)
            encoded = _bytes(manifest)
            _require(len(encoded) <= self.limits.inventory_bytes, 'archive_inventory_limit')
            self._budget(len(encoded))
            temporary=self.root/'.hrs_evidence_archive_inventory.partial'
            with temporary.open('xb') as handle:
                os.chmod(temporary,0o600)
                handle.write(encoded);handle.flush();os.fsync(handle.fileno())
            os.replace(temporary,self.inventory_path)
            self._finished = True
            self._descriptor = dict(version=VERSION,path=str(self.inventory_path),manifest_sha256=_digest(encoded),
                counts={family:value['row_count'] for family,value in families.items()},logical_bytes=self._logical,
                physical_bytes=self._physical+len(encoded),source_attested=False)
            return copy.deepcopy(self._descriptor)
        except Exception as exc:
            self._failed = True
            if self.inventory_path.exists():self.inventory_path.unlink()
            if isinstance(exc, ArchiveError):raise
            raise ArchiveError('archive_finish_failed:'+type(exc).__name__) from exc

    def descriptor(self, *, relative_to=None):
        _require(self._finished, 'archive_not_sealed')
        return _descriptor(self._descriptor,self.inventory_path,relative_to)


def _descriptor(value, inventory, relative_to):
    result=copy.deepcopy(value)
    if relative_to is not None:
        result['path']=inventory.relative_to(Path(relative_to).absolute()).as_posix()
    return result


def _file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


class EvidenceArchive:
    """Verified archive with a disposable indexed reader, never a payload cache."""
    @classmethod
    def open(cls, root, *, expected_manifest_sha256=None, limits=None):
        self=cls.__new__(cls)
        self.limits=limits or ArchiveLimits()
        path=Path(root).absolute()
        self.inventory_path=path/INVENTORY_NAME if path.is_dir() else path
        self.root=self.inventory_path.parent
        self._closed=False
        self._handles=OrderedDict()
        self._index=None
        try:
            _require(not _has_symlink_ancestor(self.inventory_path),'archive_symlink_path')
            _require(self.inventory_path.is_file() and self.inventory_path.stat().st_size <= self.limits.inventory_bytes,'archive_inventory_missing_or_limit')
            encoded=self.inventory_path.read_bytes()
            self._manifest_sha256=_digest(encoded)
            if expected_manifest_sha256 is not None:
                _require(self._manifest_sha256==expected_manifest_sha256,'archive_inventory_hash_mismatch')
            self._manifest=_loads(encoded)
            _require(self._manifest.get('version')==VERSION,'archive_version_unsupported')
            _require(shutil.disk_usage(self.root).free>=MIN_FREE_DISK_BYTES,'archive_free_disk_budget_exceeded')
            self._index=_Index()
            self._db=self._index.db
            self._verify()
            self._db.commit()
            self._db.execute('PRAGMA query_only=ON')
            return self
        except Exception as exc:
            self.close()
            if isinstance(exc,ArchiveError):raise
            raise ArchiveError('archive_verify_failed:'+type(exc).__name__) from exc

    def __enter__(self):return self
    def __exit__(self,*_):self.close()

    def close(self):
        if not self._closed:
            for handle in self._handles.values():handle.close()
            if self._index is not None:self._index.close()
            self._closed=True

    @property
    def manifest(self):return copy.deepcopy(self._manifest)
    @property
    def counts(self):return {family:value['row_count'] for family,value in self._manifest['families'].items()}
    @property
    def source_metadata(self):return copy.deepcopy(self._manifest['source_metadata'])

    def _path(self,name):
        relative=PurePosixPath(name)
        _require(not relative.is_absolute() and len(relative.parts)==1 and name not in ('.','..',INVENTORY_NAME),'archive_file_path_invalid')
        path=self.root/name
        _require(not _has_symlink_ancestor(path) and path.is_file(),'archive_file_missing_or_symlink')
        return path

    def files(self):
        _require(not self._closed,'archive_reader_closed')
        yield self.inventory_path
        for entry in self._manifest['files']:yield self._path(entry['path'])

    def descriptor(self,*,relative_to=None):
        manifest=self._manifest
        result=dict(version=VERSION,path=str(self.inventory_path),manifest_sha256=self._manifest_sha256,counts=self.counts,
                    logical_bytes=manifest['logical_bytes'],physical_bytes=manifest['physical_bytes']+self.inventory_path.stat().st_size,source_attested=False)
        return _descriptor(result,self.inventory_path,relative_to)

    def verify(self):
        _require(not self._closed,'archive_reader_closed')
        _require(_file_hash(self.inventory_path)==self._manifest_sha256,'archive_inventory_hash_mismatch')
        for entry in self._manifest['files']:
            _require(_file_hash(self._path(entry['path']))==entry['sha256'],'archive_file_hash_mismatch')
        return True

    def _handle(self,name):
        if name in self._handles:
            self._handles.move_to_end(name)
            return self._handles[name]
        # Do not serve previously buffered bytes after an on-disk mutation.
        handle=self._path(name).open('rb',buffering=0)
        self._handles[name]=handle
        if len(self._handles)>8:self._handles.popitem(last=False)[1].close()
        return handle

    def _decode(self,digest,active=None,depth=0):
        _require(depth<=self.limits.max_depth,'archive_depth_limit')
        active=set() if active is None else active
        _require(digest not in active,'archive_cas_cycle')
        found=self._db.execute('SELECT file,off,size,verified,raw_sha FROM objects WHERE h=?',(digest,)).fetchone()
        _require(found is not None,'archive_cas_reference_missing')
        name,off,size,verified,raw_sha=found
        handle=self._handle(name);handle.seek(off)
        encoded=handle.read(size)
        _require(len(encoded)==size,'archive_cas_truncated')
        _require(_digest(encoded)==raw_sha,'archive_cas_record_changed')
        record=_loads(encoded)
        _require(set(record)=={'h','b','n'} and record['h']==digest,'archive_cas_identity_invalid')
        node=record['n'];active.add(digest)
        try:
            def term(value):
                _require(depth+1<=self.limits.max_depth,'archive_depth_limit')
                _require(isinstance(value,list) and len(value)==2 and value[0] in (0,1),'archive_cas_term_invalid')
                if value[0]==1:
                    _require(isinstance(value[1],str) and _HASH.fullmatch(value[1]),'archive_cas_reference_invalid')
                    return self._decode(value[1],active,depth+1)
                _require(not isinstance(value[1],(dict,list)),'archive_cas_inline_container')
                return value[1]
            _require(isinstance(node,list) and len(node)==2,'archive_cas_node_invalid')
            if node[0]=='d':
                value={}
                for key,item in node[1]:
                    _require(isinstance(key,str) and key not in value,'archive_cas_dict_key_invalid')
                    value[key]=term(item)
            elif node[0]=='l':value=[term(item) for item in node[1]]
            elif node[0]=='L':
                block=node[1]
                _require(isinstance(block,dict) and set(block)=={'length','chunks'} and type(block['length']) is int and block['length']>LIST_BLOCK_ITEMS and isinstance(block['chunks'],list),'archive_cas_list_shape_invalid')
                _require(len(block['chunks'])==(block['length']+LIST_BLOCK_ITEMS-1)//LIST_BLOCK_ITEMS,'archive_cas_list_length_invalid')
                value=[]
                for index,reference in enumerate(block['chunks']):
                    _require(isinstance(reference,list) and len(reference)==2 and reference[0]==1 and isinstance(reference[1],str) and _HASH.fullmatch(reference[1]),'archive_cas_list_reference_invalid')
                    chunk=self._decode(reference[1],active,depth)
                    expected=min(LIST_BLOCK_ITEMS,block['length']-index*LIST_BLOCK_ITEMS)
                    _require(isinstance(chunk,list) and len(chunk)==expected,'archive_cas_list_block_length_invalid')
                    value.extend(chunk)
            elif node[0]=='s':
                _require(isinstance(node[1],str),'archive_cas_string_invalid');value=node[1]
            else:raise ArchiveError('archive_cas_node_type_invalid')
            if not verified:
                canonical=_bytes(value)
                _require(len(canonical)==record['b'] and _digest(canonical)==digest,'archive_cas_content_hash_mismatch')
                self._db.execute('UPDATE objects SET verified=1 WHERE h=?',(digest,))
            return value
        finally:active.remove(digest)

    def _verify(self):
        manifest=self._manifest
        _require(manifest.get('source_attested') is False,'archive_source_attestation_unsupported')
        _require(manifest.get('order_version')=='event_available_identity_v1','archive_order_version_invalid')
        entries=manifest.get('files');sources=manifest.get('source_metadata');families=manifest.get('families')
        _require(isinstance(entries,list) and len(entries)<=self.limits.max_shards,'archive_shard_budget_exceeded')
        _require(isinstance(sources,dict) and sources and isinstance(families,dict) and set(sources)==set(families),'archive_source_inventory_invalid')
        for family,source in sources.items():
            _require(_FAMILY.fullmatch(family),'source_family_invalid');_source(source)
        _require(type(manifest.get('receipt_count')) is int and 0<=manifest['receipt_count']<=self.limits.max_receipts,'archive_receipt_budget_exceeded')
        _require(type(manifest.get('logical_bytes')) is int and 0<=manifest['logical_bytes']<=self.limits.max_logical_bytes,'archive_logical_budget_exceeded')
        names=[entry.get('path') for entry in entries]
        _require(len(names)==len(set(names)),'archive_duplicate_file')
        _require({path.name for path in self.root.iterdir()}==set(names)|{INVENTORY_NAME},'archive_unlisted_file')
        physical=0
        for entry in entries:
            path=self._path(entry['path'])
            _require(path.stat().st_size==entry['physical_bytes'] and _file_hash(path)==entry['sha256'],'archive_file_hash_mismatch')
            physical+=path.stat().st_size
            _require(physical+self._index.disk_bytes()+self.inventory_path.stat().st_size<=self.limits.max_physical_bytes,'archive_physical_budget_exceeded')
        _require(physical==manifest['physical_bytes'],'archive_physical_count_mismatch')
        remaining=self.limits.max_physical_bytes-physical-self.inventory_path.stat().st_size
        free=shutil.disk_usage(self._index.path.parent).free
        _require(free>=MIN_FREE_DISK_BYTES,'archive_free_disk_budget_exceeded')
        self._index.limit_bytes(min(remaining,self._index.disk_bytes()+free-MIN_FREE_DISK_BYTES-64*1024))
        object_count=0
        # Index objects before receipts. No object body is retained in SQLite.
        for entry in entries:
            if entry['kind']!='objects':continue
            _require(entry['physical_bytes']<=self.limits.object_pack_bytes,'archive_object_pack_limit')
            count=0;first=last=None
            with self._path(entry['path']).open('rb') as handle:
                while True:
                    off=handle.tell();line=handle.readline(self.limits.object_pack_bytes+1)
                    if not line:break
                    _require(len(line)<=self.limits.object_pack_bytes and line.endswith(b'\n'),'archive_object_line_limit')
                    record=_loads(line)
                    _require(isinstance(record,dict) and set(record)=={'h','b','n'} and isinstance(record['h'],str) and _HASH.fullmatch(record['h']),'archive_cas_identity_invalid')
                    _require(type(record['b']) is int and 0<record['b']<=self.limits.shard_logical_bytes,'archive_cas_logical_limit')
                    try:self._db.execute('INSERT INTO objects(h,file,off,size,raw_sha) VALUES(?,?,?,?,?)',(record['h'],entry['path'],off,len(line),_digest(line)))
                    except sqlite3.IntegrityError as exc:raise ArchiveError('archive_cas_duplicate_identity') from exc
                    if first is None:first=record['h']
                    last=record['h'];count+=1
            _require((count,first,last)==(entry['row_count'],entry['first_key'],entry['last_key']),'archive_object_inventory_mismatch')
            object_count+=count
        _require(object_count==manifest['object_count'],'archive_object_count_mismatch')
        stats={family:dict(count=0,items=0,bytes=0,first=None,last=None,digest=hashlib.sha256()) for family in sources}
        count=logical=0
        for entry in entries:
            if entry['kind']=='objects':continue
            _require(entry['kind']=='receipts' and entry.get('family') in sources,'archive_file_kind_invalid')
            family=entry['family'];stat=stats[family]
            part_count=part_bytes=0;first=last=None
            with self._path(entry['path']).open('rb') as handle:
                while True:
                    line=handle.readline(self.limits.object_pack_bytes+1)
                    if not line:break
                    _require(line.endswith(b'\n') and len(line)<=self.limits.object_pack_bytes,'archive_receipt_line_limit')
                    frame=_loads(line)
                    _require(frame.get('family')==family and frame.get('root')==frame.get('canonical_sha256'),'archive_receipt_frame_invalid')
                    row=self._decode(frame['root'])
                    data=_bytes(row);meta=_metadata(family,row,sources[family]);key=_key(meta)
                    _require(frame['canonical_bytes']==len(data) and _digest(data)==frame['root'],'archive_original_receipt_hash_mismatch')
                    _require(all(frame.get(field)==value for field,value in meta.items()) and frame['key']==key,'archive_receipt_index_mismatch')
                    _require(stat['last'] is None or stat['last']<key,'archive_receipt_order_or_identity_conflict')
                    _require(type(frame.get('ordinal')) is int and 0<=frame['ordinal']<manifest['receipt_count'],'archive_receipt_ordinal_invalid')
                    try:self._db.execute('INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (family,meta['identity'],frame['root'],len(data),meta['event'],meta['available'],meta['run_id'],meta['kind'],meta['path'],meta['symbol'],meta['item_count'],frame['ordinal']))
                    except sqlite3.IntegrityError as exc:raise ArchiveError('duplicate_receipt_identity_conflict') from exc
                    if family=='decision_evidence' and meta['run_id'] is not None:
                        self._db.execute('INSERT INTO runs VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET first=min(first,excluded.first)',(meta['run_id'],meta['event']))
                    if stat['first'] is None:stat['first']=key
                    if first is None:first=key
                    stat['last']=last=key;stat['count']+=1;stat['items']+=meta['item_count'];stat['bytes']+=len(data)
                    _ordered(stat['digest'],frame)
                    part_count+=1;count+=1;part_bytes+=len(data);logical+=len(data)
                    _require(part_count<=self.limits.shard_receipts and part_bytes<=self.limits.shard_logical_bytes,'archive_receipt_shard_limit')
                    _require(count<=self.limits.max_receipts and logical<=self.limits.max_logical_bytes,'archive_receipt_or_logical_budget_exceeded')
            _require((part_count,part_bytes,first,last)==(entry['row_count'],entry['logical_bytes'],entry['first_key'],entry['last_key']),'archive_receipt_inventory_mismatch')
        for family,stat in stats.items():
            expected=families[family];source=sources[family]
            _require(stat['count']==expected['row_count']==source['expected_count'],'archive_source_count_mismatch')
            _require(stat['items']==expected['item_count'] and ('expected_item_count' not in source or stat['items']==source['expected_item_count']),'archive_source_item_count_mismatch')
            _require((stat['bytes'],stat['first'],stat['last'],stat['digest'].hexdigest())==(expected['logical_bytes'],expected['first_key'],expected['last_key'],expected['ordered_sha256']),'archive_family_ordered_digest_mismatch')
        _require((count,logical)==(manifest['receipt_count'],manifest['logical_bytes']),'archive_global_count_mismatch')
        _require(self._db.execute('SELECT count(*) FROM objects WHERE verified=0').fetchone()[0]==0,'archive_cas_unreachable_objects')
        _require(physical+self._index.disk_bytes()+self.inventory_path.stat().st_size<=self.limits.max_physical_bytes,'archive_physical_budget_exceeded')

    def iter_records(self,family='decision_evidence',*,kind=None,run_id=None,path=None,symbol=None,
                     event_start=None,event_end=None,available_before=None,ordered=True,order_by_available=False):
        _require(not self._closed and family in self._manifest['source_metadata'],'archive_reader_or_family_invalid')
        clauses=['family=?'];params=[family]
        for name,value in (('kind',kind),('run_id',run_id),('path',path),('symbol',symbol)):
            if value is not None:clauses.append(name+'=?');params.append(value)
        for name,operator,value in (('event','>=',event_start),('event','<',event_end),('available','<=',available_before)):
            if value is not None:clauses.append(name+operator+'?');params.append(_micros(value))
        order='available,event,identity' if order_by_available else ('event,available,identity' if ordered else 'ordinal')
        cursor=self._db.execute('SELECT root FROM receipts WHERE '+' AND '.join(clauses)+' ORDER BY '+order,params)
        for (digest,) in cursor:
            row=self._decode(digest)
            _require(_digest(_bytes(row))==digest,'archive_original_receipt_hash_mismatch')
            yield row

    def get_record(self,family,identity):
        _require(not self._closed,'archive_reader_closed')
        found=self._db.execute('SELECT root FROM receipts WHERE family=? AND identity=?',(family,str(identity))).fetchone()
        if found is None:return None
        row=self._decode(found[0]);_require(_digest(_bytes(row))==found[0],'archive_original_receipt_hash_mismatch')
        return row

    def count_records(self,family,*,available_before=None):
        _require(not self._closed and family in self._manifest['source_metadata'],'archive_reader_or_family_invalid')
        query='SELECT count(*) FROM receipts WHERE family=?';args=[family]
        if available_before is not None:query+=' AND available<=?';args.append(_micros(available_before))
        return self._db.execute(query,args).fetchone()[0]

    def iter_runs(self):
        _require(not self._closed,'archive_reader_closed')
        cursor=self._db.execute('SELECT run_id,first FROM runs ORDER BY first,run_id')
        for run,_ in cursor:yield run
