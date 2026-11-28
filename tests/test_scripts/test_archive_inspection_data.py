import hashlib
import json
from pathlib import Path
import tarfile

import pytest

from scripts.ops import archive_inspection_data as archive


def fixture(tmp_path, monkeypatch):
    first, second = tmp_path/'one', tmp_path/'two'
    for root in (first, second):
        (root/'sym').mkdir(parents=True)
        (root/'sym/data.csv').write_text('1,2,3\n')
    cache = tmp_path/'browser';cache.mkdir();(cache/'binary').write_bytes(b'cache')
    package = tmp_path/'package';package.mkdir()
    (package/'package.json').write_text('{"name":"playwright-core","version":"1.58.1"}')
    (package/'browsers.json').write_text(json.dumps({'browsers': [
        {'name': 'chromium', 'revision': '1208'},
        {'name': 'chromium-headless-shell', 'revision': '1208'},
        {'name': 'ffmpeg', 'revision': '1011'}]}))
    monkeypatch.setattr(archive, 'SOURCE_ROOTS', (first, second))
    monkeypatch.setattr(archive, 'CACHE_ROOTS', (cache,))
    monkeypatch.setattr(archive, 'PACKAGE_ROOT', package)
    monkeypatch.setattr(archive, 'check_references', lambda roots: None)
    rows=[{'path':'sym/data.csv','bytes':6,'sha256':hashlib.sha256(b'1,2,3\n').hexdigest()}]
    review={'identical_content':True,'collections':[{'root':str(p),'files':rows} for p in (first,second)]}
    return first,second,cache,review,tmp_path/'archive'


def test_prepare_keeps_sources_and_apply_requires_verified_local_copy(tmp_path, monkeypatch):
    first,second,cache,review,dest=fixture(tmp_path,monkeypatch)
    result=archive.prepare(review,dest)
    assert (first/'sym/data.csv').exists() and (cache/'binary').exists()
    with pytest.raises(ValueError):archive.apply(dest,'0'*64)
    archive.apply(dest,result['archive_sha256'])
    assert not first.exists() and not second.exists() and not cache.exists()
    assert (dest/'cash-carry-data_2y.tar.gz').exists()
    assert (dest/'RESTORE.md').exists()
    assert json.loads((dest/'applied.json').read_text())['removed_source_files']==2


def test_changed_source_blocks_all_removal(tmp_path, monkeypatch):
    first,second,cache,review,dest=fixture(tmp_path,monkeypatch)
    result=archive.prepare(review,dest)
    (second/'sym/data.csv').write_text('changed')
    with pytest.raises(ValueError):archive.apply(dest,result['archive_sha256'])
    assert first.exists() and cache.exists()


def test_changed_cache_blocks_all_removal(tmp_path, monkeypatch):
    first,second,cache,review,dest=fixture(tmp_path,monkeypatch)
    result=archive.prepare(review,dest)
    (cache/'new').write_text('unexpected')
    with pytest.raises(ValueError):archive.apply(dest,result['archive_sha256'])
    assert first.exists() and second.exists()


def test_corrupt_archive_blocks_removal(tmp_path, monkeypatch):
    first,second,cache,review,dest=fixture(tmp_path,monkeypatch)
    result=archive.prepare(review,dest)
    (dest/'cash-carry-data_2y.tar.gz').write_bytes(b'broken')
    with pytest.raises(ValueError):archive.apply(dest,result['archive_sha256'])
    assert first.exists() and cache.exists()


def test_unsafe_member_or_symlink_root_rejected(tmp_path, monkeypatch):
    first,second,cache,review,dest=fixture(tmp_path,monkeypatch)
    with pytest.raises(ValueError):archive.checked_relative('../other')
    with pytest.raises(ValueError):archive.checked_relative('/root/file')
    link=tmp_path/'link';link.symlink_to(first,target_is_directory=True)
    with pytest.raises(ValueError):archive.snapshot_tree(link)


def test_archive_member_content_is_verified_not_only_tar_checksum(tmp_path, monkeypatch):
    first,second,cache,review,dest=fixture(tmp_path,monkeypatch)
    result=archive.prepare(review,dest)
    fake=tmp_path/'fake';fake.mkdir();(fake/'wrong').write_text('bad')
    target=dest/'cash-carry-data_2y.tar.gz'
    with tarfile.open(target,'w:gz') as stream:stream.add(fake,arcname='data_2y')
    with pytest.raises(ValueError):archive.verify_archive(target,result['sources'][0]['snapshot'])


def test_reference_failure_prevents_prepare(tmp_path, monkeypatch):
    first,second,cache,review,dest=fixture(tmp_path,monkeypatch)
    def fail(roots):raise ValueError('挂载仍有引用')
    monkeypatch.setattr(archive,'check_references',fail)
    with pytest.raises(ValueError):archive.prepare(review,dest)
    assert not dest.exists() and first.exists()
