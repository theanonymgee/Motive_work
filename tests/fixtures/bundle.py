"""The whole synthetic bundle (design M1 §7.5): every generator, the OLE container cases and noise.

``write_bundle(root)`` writes all nine parsers' documents (``make_text``, ``make_eml``,
``make_ooxml``, ``make_pdf``, ``make_hwp``) below ``root`` in ``docs/``, ``mail/``, ``sheets/`` and
``edge/``, plus the OLE container cases built here with ``cfb.write_cfb`` (an encrypted OOXML
package and old binary Office documents renamed to an OOXML extension), and returns the
``Expected`` list. Two generators writing the same path is an error, not an overwrite.

Noise the collector must leave alone (``NOISE``): an excluded folder (``node_modules/``), a
``.git/`` folder, an Office lock file (``~$...``), a file with the hidden attribute (Windows only)
and an unsupported extension. ``noise_counts()`` gives the ``excluded``/``ignored`` counts a scan
of the bundle reports. ``totals()`` sums the ``Expected`` list the way a scan summary counts.

``root`` is always a temporary folder (``tmp_path``), never inside the repository.
"""
from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import replace
from pathlib import Path

from . import SENTINEL, SENTINEL_ASCII, Expected, cfb, make_eml, make_hwp, make_ooxml, make_pdf, make_text

FILE_ATTRIBUTE_HIDDEN = 0x2


def _ole(entries: dict) -> bytes:
    return cfb.write_cfb(entries)


def ole_cases() -> list[tuple[Expected, bytes]]:
    """OLE compound files behind OOXML extensions (design §5.2, §7.3, §7.4)."""
    marker = f'{SENTINEL} {SENTINEL_ASCII}'.encode('utf-16-le')
    # An encrypted OOXML package is an OLE file with EncryptionInfo and EncryptedPackage streams.
    # The "ciphertext" is synthetic filler; EncryptedPackage is over 4096 bytes (regular sectors),
    # EncryptionInfo under it (mini stream), so both CFB storage paths are exercised.
    info = b'\x04\x00\x04\x00\x40\x00\x00\x00' + marker
    package = (len(marker).to_bytes(8, 'little') + marker).ljust(5000, b'\x5a')
    encrypted = _ole({'EncryptionInfo': info, 'EncryptedPackage': package})
    encrypted_xlsx = _ole({'EncryptionInfo': info, 'EncryptedPackage': package[:1500]})
    # Old binary Office documents saved with a new extension: no EncryptedPackage stream.
    word = _ole({'WordDocument': b'\xec\xa5' + marker, '1Table': b'\x00' * 64, 'CompObj': b'\x01\x00'})
    excel = _ole({'Workbook': b'\x09\x08' + marker})
    powerpoint = _ole({'PowerPoint Document': marker.ljust(4500, b'\x00'), 'Current User': b'\x00' * 20})
    damaged = (cfb.SIGNATURE + b'\x00' * 504 + marker).ljust(1024, b'\x00')  # header without FAT or directory
    return [
        (Expected('edge/ooxml_encrypted.docx', 'docx', 'encrypted', 'ooxml_encrypted'), encrypted),
        (Expected('edge/ooxml_encrypted.xlsx', 'xlsx', 'encrypted', 'ooxml_encrypted'), encrypted_xlsx),
        (Expected('edge/legacy_word.docx', 'docx', 'unsupported', 'legacy_ole'), word),
        (Expected('edge/legacy_excel.xlsx', 'xlsx', 'unsupported', 'legacy_ole'), excel),
        (Expected('edge/legacy_powerpoint.pptx', 'pptx', 'unsupported', 'legacy_ole'), powerpoint),
        (Expected('edge/legacy_hwpx.hwpx', 'hwpx', 'unsupported', 'legacy_ole'), word),
        # The OLE signature followed by nothing readable: not an OLE file at all.
        (Expected('edge/ole_damaged.docx', 'docx', 'failed', 'ole_invalid'), damaged),
    ]


COPY_OF = 'docs/notes_utf8.txt'
COPY = 'docs/사본/회의록 사본.txt'


def documents() -> list[tuple[Expected, bytes]]:
    """Every ``(Expected, bytes)`` pair of the bundle, in a fixed order."""
    pairs = [*make_text.build(), *make_eml.build(), *make_ooxml.build()]
    for cases in (make_pdf.cases(), make_hwp.cases()):
        for rel_path, (data, item) in cases.items():
            if item.rel_path != rel_path:
                raise ValueError(f'generator key {rel_path!r} differs from its Expected {item.rel_path!r}')
            pairs.append((item, data))
    pairs += ole_cases()
    # A copy under a Korean folder and file name: same bytes as an earlier document, so its units are reused.
    original, data = next((item, data) for item, data in pairs if item.rel_path == COPY_OF)
    pairs.append((replace(original, rel_path=COPY), data))
    seen = Counter(item.rel_path.casefold() for item, _ in pairs)  # Windows paths ignore case
    if clashes := sorted(path for path, n in seen.items() if n > 1):
        raise ValueError(f'two generators write the same path: {clashes}')
    return pairs


# Noise: (relative path, bytes, why the collector skips it). The hidden file gets its attribute in write_bundle.
NOISE = (
    ('node_modules/x.txt', f'{SENTINEL} excluded folder\n'.encode(), 'excluded'),
    ('.git/config', f'[core]\n\tsynthetic = {SENTINEL_ASCII}\n'.encode(), 'excluded'),
    ('docs/~$lock.docx', f'{SENTINEL_ASCII} office lock'.encode(), 'excluded'),
    ('docs/hidden_note.txt', f'{SENTINEL} hidden attribute\n'.encode(), 'excluded'),
    ('docs/image.png', b'\x89PNG\r\n\x1a\n' + SENTINEL_ASCII.encode(), 'ignored'),
)
HIDDEN = 'docs/hidden_note.txt'


def hidden_supported() -> bool:
    return os.name == 'nt'


def _set_hidden(path: Path) -> None:
    import ctypes
    if not ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN):
        raise OSError('could not set the hidden attribute')


def noise_paths() -> list[str]:
    """Relative paths of the noise files ``write_bundle`` writes on this platform."""
    return [rel_path for rel_path, _, _ in NOISE if rel_path != HIDDEN or hidden_supported()]


def write_bundle(root: Path, *, with_edges: bool = True, noise: bool = True) -> list[Expected]:
    """Write the bundle below ``root``; return the ``Expected`` of every supported document written."""
    root = Path(root)
    expected = []
    for item, data in documents():
        if not with_edges and item.rel_path.startswith('edge/'):
            continue
        target = root/item.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        expected.append(item)
    if noise:
        for rel_path, data, _ in NOISE:
            if rel_path not in noise_paths():
                continue  # no hidden attribute on this platform: the file would be collected
            target = root/rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if rel_path == HIDDEN:
                _set_hidden(target)
    return expected


def noise_counts() -> dict[str, int]:
    """``excluded``/``ignored`` counts of a scan over ``write_bundle(root)`` (folders count once)."""
    counts = Counter(why for rel_path, _, why in NOISE if rel_path in noise_paths())
    return {'excluded': counts['excluded'], 'ignored': counts['ignored']}


def status_key(item: Expected) -> str:
    """``'ok'`` or ``'<status>/<code>'``, as the audit wrapper groups ``versions`` rows."""
    return item.status if item.error is None else f'{item.status}/{item.error}'


def totals(pairs) -> dict:
    """What a first scan over these documents must report.

    ``pairs`` is ``documents()`` (or a subset). Units of an ok document whose bytes equal an earlier
    ok document of the same parser are copied, not parsed, so they count as ``reused``.
    """
    pairs = list(pairs)
    first_seen, reused = set(), 0
    for item, data in pairs:
        if item.status == 'ok':
            key = (item.parser, hashlib.sha256(data).hexdigest())
            reused += key in first_seen
            first_seen.add(key)
    oks = sum(item.status == 'ok' for item, _ in pairs)
    by_status = Counter(item.status for item, _ in pairs if item.status != 'ok')
    by_parser: dict[str, dict] = {}
    for item, _ in pairs:
        entry = by_parser.setdefault(item.parser, {'files': 0, 'units': 0, 'statuses': Counter()})
        entry['files'] += 1
        entry['units'] += item.unit_count
        entry['statuses'][status_key(item)] += 1
    return {
        'files': len(pairs), 'units': sum(item.unit_count for item, _ in pairs), 'ok': oks,
        'parsed_ok': oks - reused, 'reused': reused, 'failed': by_status['failed'],
        'unsupported': by_status['unsupported'], 'encrypted': by_status['encrypted'],
        'too_large': by_status['too_large'], 'statuses': Counter(status_key(item) for item, _ in pairs),
        'by_parser': {name: {**entry, 'statuses': dict(entry['statuses'])}
                      for name, entry in sorted(by_parser.items())},
    }
