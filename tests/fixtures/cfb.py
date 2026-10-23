"""Minimal CFB (OLE2, MS-CFB) writer for synthetic test documents (design M1 §7.4).

``write_cfb`` builds a valid version-3 compound file (512-byte sectors) that ``olefile`` can read:
header, FAT, directory, a mini FAT and mini stream (streams under 4096 bytes live there, as in real
files, because ``olefile`` decides by size which store to read from), and nested storages. Standard
library only; nothing here reads or writes a path.

Simplifications, none of which ``olefile`` or the HWP parser notice: the directory tree of each
storage is a balanced binary tree with every node coloured black (MS-CFB asks for a red-black
tree), all times and CLSIDs are zero, there is no DIFAT sector (so at most 109 FAT sectors, about
7 MiB of stream data), and nothing is ever written in transaction or version-4 form.
"""
from __future__ import annotations

import struct

SIGNATURE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
SECTOR_SIZE = 512
MINI_SECTOR_SIZE = 64
MINI_CUTOFF = 4096
FREE = 0xFFFFFFFF
END_OF_CHAIN = 0xFFFFFFFE
FAT_SECTOR = 0xFFFFFFFD
NO_STREAM = 0xFFFFFFFF
_FAT_PER_SECTOR = SECTOR_SIZE // 4
_ENTRIES_PER_SECTOR = SECTOR_SIZE // 128
_MAX_FAT_SECTORS = 109
_TYPE_STORAGE, _TYPE_STREAM, _TYPE_ROOT = 1, 2, 5


class _Node:
    def __init__(self, name: str, data: bytes | None):
        self.name = name
        self.data = data            # None for a storage
        self.children: dict[str, _Node] = {}
        self.index = 0
        self.left = self.right = self.child = NO_STREAM
        self.start = END_OF_CHAIN
        self.size = 0


def _tree(entries: dict) -> _Node:
    root = _Node('Root Entry', None)

    def add(parent: _Node, key: str, value) -> None:
        head, _, rest = key.partition('/')
        if not head or len(head) > 31 or any(c in head for c in ':!\\'):
            raise ValueError('invalid CFB entry name')
        if rest or isinstance(value, dict):
            node = parent.children.get(head)
            if node is None:
                node = parent.children[head] = _Node(head, None)
            elif node.data is not None:
                raise ValueError('CFB entry is both stream and storage')
            if rest:
                add(node, rest, value)
            else:
                for name, inner in value.items():
                    add(node, name, inner)
        else:
            if head in parent.children:
                raise ValueError('duplicate CFB entry')
            parent.children[head] = _Node(head, bytes(value))

    for key, value in entries.items():
        add(root, key, value)
    return root


def _sort_key(node: _Node):
    return len(node.name), node.name.upper()


def _number(root: _Node) -> list[_Node]:
    """Directory order (root first, children of a storage in tree order) and tree links."""
    order = [root]

    def link(nodes: list[_Node]) -> int:
        if not nodes:
            return NO_STREAM
        middle = len(nodes) // 2
        node = nodes[middle]
        node.left = link(nodes[:middle])
        node.right = link(nodes[middle + 1:])
        return node.index

    def visit(storage: _Node) -> None:
        children = sorted(storage.children.values(), key=_sort_key)
        for child in children:
            child.index = len(order)
            order.append(child)
        storage.child = link(children)
        for child in children:
            if child.data is None:
                visit(child)

    visit(root)
    return order


def _pad(data: bytes, unit: int) -> bytes:
    return data + b'\x00' * (-len(data) % unit)


def _entry(node: _Node | None, kind: int = 0) -> bytes:
    if node is None:
        return struct.pack('<64sHBBIII16sIQQIQ', b'', 0, 0, 0, NO_STREAM, NO_STREAM, NO_STREAM, b'', 0, 0, 0, 0, 0)
    name = node.name.encode('utf-16-le') + b'\x00\x00'
    return struct.pack('<64sHBBIII16sIQQIQ', name, len(name), kind, 1, node.left, node.right, node.child,
                       b'', 0, 0, 0, node.start, node.size)


def write_cfb(entries: dict) -> bytes:
    """A compound file holding ``entries``: ``{'name': bytes}`` for a stream, ``{'name': {...}}`` for a
    storage, and ``'Storage/Stream'`` keys for a path. Raises ValueError for bad names or too much data."""
    root = _tree(entries)
    order = _number(root)
    streams = [node for node in order if node.data is not None]

    # Mini stream: every stream under the cutoff, in 64-byte units chained through the mini FAT.
    mini_data, mini_fat = bytearray(), []
    for node in streams:
        node.size = len(node.data)
        if 0 < node.size < MINI_CUTOFF:
            node.start = len(mini_data) // MINI_SECTOR_SIZE
            count = -(-node.size // MINI_SECTOR_SIZE)
            mini_fat.extend(range(node.start + 1, node.start + count))
            mini_fat.append(END_OF_CHAIN)
            mini_data += _pad(node.data, MINI_SECTOR_SIZE)
    mini_fat_sectors = -(-len(mini_fat) * 4 // SECTOR_SIZE)
    mini_stream_sectors = -(-len(mini_data) // SECTOR_SIZE)
    big = [node for node in streams if node.size >= MINI_CUTOFF]
    big_sectors = [-(-node.size // SECTOR_SIZE) for node in big]
    directory_sectors = -(-len(order) // _ENTRIES_PER_SECTOR)

    used = directory_sectors + mini_fat_sectors + mini_stream_sectors + sum(big_sectors)
    fat_sectors = 0
    while fat_sectors * _FAT_PER_SECTOR < used + fat_sectors:
        fat_sectors += 1
    if fat_sectors > _MAX_FAT_SECTORS:
        raise ValueError('too much data for the minimal CFB writer')

    fat = [FREE] * (fat_sectors * _FAT_PER_SECTOR)

    def chain(first: int, count: int) -> None:
        for sector in range(first, first + count):
            fat[sector] = sector + 1 if sector + 1 < first + count else END_OF_CHAIN

    cursor = 0
    directory_start = cursor
    chain(cursor, directory_sectors)
    cursor += directory_sectors
    mini_fat_start = cursor if mini_fat_sectors else END_OF_CHAIN
    chain(cursor, mini_fat_sectors)
    cursor += mini_fat_sectors
    root.start = cursor if mini_stream_sectors else END_OF_CHAIN
    root.size = len(mini_data)
    chain(cursor, mini_stream_sectors)
    cursor += mini_stream_sectors
    for node, count in zip(big, big_sectors):
        node.start = cursor
        chain(cursor, count)
        cursor += count
    fat_start = cursor
    for sector in range(fat_start, fat_start + fat_sectors):
        fat[sector] = FAT_SECTOR

    header = struct.pack('<8s16sHHHHH6sIIIIIIIII', SIGNATURE, b'', 0x3E, 3, 0xFFFE, 9, 6, b'', 0, fat_sectors,
                         directory_start, 0, MINI_CUTOFF, mini_fat_start, mini_fat_sectors, END_OF_CHAIN, 0)
    difat = [fat_start + n for n in range(fat_sectors)] + [FREE] * (_MAX_FAT_SECTORS - fat_sectors)
    header += struct.pack('<109I', *difat)
    assert len(header) == SECTOR_SIZE

    directory = b''.join(_entry(node, _TYPE_ROOT if node is root else _TYPE_STORAGE if node.data is None
                                else _TYPE_STREAM) for node in order)
    directory += b''.join(_entry(None) for _ in range(directory_sectors * _ENTRIES_PER_SECTOR - len(order)))
    body = [directory, struct.pack(f'<{len(mini_fat)}I', *mini_fat).ljust(mini_fat_sectors * SECTOR_SIZE, b'\xff'),
            bytes(mini_data).ljust(mini_stream_sectors * SECTOR_SIZE, b'\x00')]
    body += [_pad(node.data, SECTOR_SIZE) for node in big]
    body.append(struct.pack(f'<{len(fat)}I', *fat))
    return header + b''.join(body)
