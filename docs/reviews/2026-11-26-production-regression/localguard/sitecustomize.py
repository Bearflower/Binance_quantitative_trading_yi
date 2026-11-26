"""回归仅允许本机假HTTP服务和临时PostgreSQL，阻断全部外部socket。"""
import socket
import os
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex

def _local(sock, address):
    if sock.family == socket.AF_UNIX:
        return isinstance(address, (str, bytes)) and os.fsdecode(address).startswith(('/tmp/', '/var/folders/', '/private/var/folders/'))
    return isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1', 'localhost')

def guarded_connect(self, address):
    if not _local(self, address):
        raise RuntimeError('回归禁止连接外部网络')
    return _connect(self, address)

def guarded_connect_ex(self, address):
    if not _local(self, address):
        raise RuntimeError('回归禁止连接外部网络')
    return _connect_ex(self, address)
socket.socket.connect = guarded_connect
socket.socket.connect_ex = guarded_connect_ex
