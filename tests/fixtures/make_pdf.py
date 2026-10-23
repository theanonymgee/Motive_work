"""Synthetic PDF generator (design M1 §7.3, appendix B). Standard library only.

Hand-written minimal PDFs: object offsets and the xref table are computed here. Two kinds of text
page: Helvetica with WinAnsiEncoding (ASCII only, so ``SENTINEL_ASCII`` instead of ``SENTINEL``)
and a Korean one, a Type0 font with ``/Encoding /Identity-H``, a ``CIDFontType2`` descendant with no
font program and a ``/ToUnicode`` CMap, which is what lets a text extractor recover the characters.
"""
from __future__ import annotations

from . import LINES_EN, LINES_KO, Expected


def _escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def _stream(body: bytes, extra: str = '') -> bytes:
    return f'<< /Length {len(body)}{extra} >>\nstream\n'.encode('ascii') + body + b'\nendstream'


def assemble(objects: list[bytes], trailer: str = '', header: bytes = b'%PDF-1.4\n') -> bytes:
    """``objects[i]`` is the body of object ``i + 1``; ``trailer`` is added inside the trailer dictionary."""
    out = bytearray(header)
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f'{number} 0 obj\n'.encode('ascii') + body + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n'.encode('ascii') + b'0000000000 65535 f \n'
    for offset in offsets:
        out += f'{offset:010d} 00000 n \n'.encode('ascii')
    out += f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R {trailer}>>\nstartxref\n{xref}\n%%EOF\n'.encode('ascii')
    return bytes(out)


def _page(parent: int, font: int, contents: int) -> bytes:
    return (f'<< /Type /Page /Parent {parent} 0 R /MediaBox [0 0 612 792] '
            f'/Resources << /Font << /F1 {font} 0 R >> >> /Contents {contents} 0 R >>').encode('ascii')


def _pages_tree(first_page: int, count: int) -> bytes:
    kids = ' '.join(f'{first_page + 2 * n} 0 R' for n in range(count))
    return f'<< /Type /Pages /Kids [{kids}] /Count {count} >>'.encode('ascii')


def latin_pdf(pages: list[list[str]], *, encrypt: bool = False) -> bytes:
    """One page per entry of ``pages`` (a list of ASCII lines; an empty list is a page with no text)."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', _pages_tree(4, len(pages)),
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>']
    for number, lines in enumerate(pages):
        body = ' 0 -16 Td '.join(f'({_escape(line)}) Tj' for line in lines)
        content = f'BT /F1 12 Tf 72 720 Td {body} ET' if lines else 'BT ET'
        objects.append(_page(2, 3, 5 + 2 * number))
        objects.append(_stream(content.encode('ascii')))
    trailer = ''
    if encrypt:  # a standard security handler dictionary; the file is not actually encrypted
        objects.append(('<< /Filter /Standard /V 1 /R 2 /Length 40 /P -1 '
                        f'/O <{"00" * 32}> /U <{"00" * 32}> >>').encode('ascii'))
        trailer = f'/Encrypt {len(objects)} 0 R /ID [<{"11" * 16}> <{"11" * 16}>] '
    return assemble(objects, trailer)


def korean_pdf(pages: list[list[str]]) -> bytes:
    """Like ``latin_pdf`` but the text may be any BMP characters, shown through Identity-H and ToUnicode."""
    characters = sorted({char for lines in pages for line in lines for char in line})
    cid = {char: number for number, char in enumerate(characters, 1)}
    blocks = []
    for start in range(0, len(characters), 100):
        chunk = characters[start:start + 100]
        rows = ''.join(f'<{cid[char]:04X}> <{ord(char):04X}>\n' for char in chunk)
        blocks.append(f'{len(chunk)} beginbfchar\n{rows}endbfchar\n')
    cmap = ('/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
            '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
            '/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n'
            '1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n' + ''.join(blocks) +
            'endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'', b'', b'', b'', b'']
    objects[2] = (b'<< /Type /Font /Subtype /Type0 /BaseFont /SyntheticSans /Encoding /Identity-H '
                  b'/DescendantFonts [4 0 R] /ToUnicode 6 0 R >>')
    objects[3] = (b'<< /Type /Font /Subtype /CIDFontType2 /BaseFont /SyntheticSans '
                  b'/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> '
                  b'/FontDescriptor 5 0 R /DW 1000 /CIDToGIDMap /Identity >>')
    objects[4] = (b'<< /Type /FontDescriptor /FontName /SyntheticSans /Flags 4 /FontBBox [0 -200 1000 800] '
                  b'/ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>')
    objects[5] = _stream(cmap.encode('ascii'))
    objects[1] = _pages_tree(7, len(pages))
    for number, lines in enumerate(pages):
        body = ' 0 -16 Td '.join('<' + ''.join(f'{cid[char]:04X}' for char in line) + '> Tj' for line in lines)
        content = f'BT /F1 12 Tf 72 720 Td {body} ET' if lines else 'BT ET'
        objects.append(_page(2, 3, 8 + 2 * number))
        objects.append(_stream(content.encode('ascii')))
    return assemble(objects)


# ---- resource cases (parser resource design §4, §6.3) -------------------------------------------

_HELVETICA = b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>'


def _flate(body: bytes) -> bytes:
    import zlib
    return _stream(zlib.compress(body, 9), ' /Filter /FlateDecode')


def form_chain_pdf(levels: int, fanout: int = 10, leaf_chars: int = 900) -> bytes:
    """One page that draws form 1 ``fanout`` times; form k draws form k + 1 ``fanout`` times and the last
    form shows ``leaf_chars`` letters: ``leaf_chars * fanout ** levels`` characters of text (review v3 M1-9)."""
    first_form = 6
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', _pages_tree(4, 1), _HELVETICA,
               f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> '
               f'/XObject << /X {first_form} 0 R >> >> /Contents 5 0 R >>'.encode('ascii'),
               _stream(b' '.join([b'q /X Do Q'] * fanout))]
    for level in range(1, levels + 1):
        if level < levels:
            body = b' '.join([b'q /X Do Q'] * fanout)
            inner = f'/XObject << /X {first_form + level} 0 R >> '
        else:
            body = b'BT /F1 12 Tf 72 720 Td (' + b'x' * leaf_chars + b') Tj ET'
            inner = ''
        objects.append(_stream(body, f' /Type /XObject /Subtype /Form /BBox [0 0 612 792] '
                                     f'/Resources << /Font << /F1 3 0 R >> {inner}>>'))
    return assemble(objects)


def kids_pdf(references: int, *, count: int = 5) -> bytes:
    """A page tree whose ``/Kids`` lists one page ``references`` times and claims ``/Count count``."""
    kids = ' '.join(['4 0 R'] * references)
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', f'<< /Type /Pages /Kids [{kids}] /Count {count} >>'.encode(),
               _HELVETICA, _page(2, 3, 5), _stream(b'BT /F1 12 Tf 72 720 Td (repeated page) Tj ET')]
    return assemble(objects)


def pages_cycle_pdf() -> bytes:
    """Two ``/Pages`` nodes that list each other (pypdf only checks a node listing itself)."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [4 0 R 6 0 R] /Count 2 >>',
               _HELVETICA, _page(2, 3, 5), _stream(b'BT /F1 12 Tf 72 720 Td (page) Tj ET'),
               b'<< /Type /Pages /Kids [2 0 R] /Count 1 >>']
    return assemble(objects)


def parent_loop_pdf(*, in_form: bool = False) -> bytes:
    """A page (or a form it draws) without ``/Resources`` whose ``/Parent`` links go round in a cycle:
    pypdf's lookup of inherited resources never ends there. A second, normal page has text."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [4 0 R 8 0 R] /Count 2 >>', _HELVETICA]
    if in_form:
        objects.append(b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
                       b'/Resources << /Font << /F1 3 0 R >> /XObject << /X 10 0 R >> >> /Contents 5 0 R >>')
    else:
        objects.append(b'<< /Type /Page /Parent 6 0 R /MediaBox [0 0 612 792] /Contents 5 0 R >>')
    objects += [_stream(b'q /X Do Q BT /F1 12 Tf 72 720 Td (lost) Tj ET'), b'<< /Parent 7 0 R >>',
                b'<< /Parent 6 0 R >>', _page(2, 3, 9), _stream(b'BT /F1 12 Tf 72 720 Td (kept page) Tj ET'),
                _stream(b'BT /F1 12 Tf (form text) Tj ET', ' /Type /XObject /Subtype /Form /BBox [0 0 612 792] '
                                                          '/Parent 6 0 R')]
    return assemble(objects)


def big_stream_pdf(pages: int, stream_bytes: int, *, text: str = 'page') -> bytes:
    """``pages`` pages, each with a Flate content stream of ``stream_bytes`` (mostly a comment) and one line."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', _pages_tree(4, pages), _HELVETICA]
    for number in range(pages):
        body = f'BT /F1 12 Tf 72 720 Td ({text} {number + 1}) Tj ET\n%'.encode('ascii')
        body += b'x' * (stream_bytes - len(body))
        objects.append(_page(2, 3, 5 + 2 * number))
        objects.append(_flate(body))
    return assemble(objects)


def long_page_pdf(chars: int) -> bytes:
    """One page showing ``chars`` letters in lines of 1,000 (no form XObject)."""
    lines, rest = divmod(chars, 1000)
    body = ' 0 -1 Td '.join(['(' + 'y' * 1000 + ') Tj'] * lines + ([f'({"y" * rest}) Tj'] if rest else []))
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', _pages_tree(4, 1), _HELVETICA, _page(2, 3, 5),
               _flate(f'BT /F1 1 Tf 72 720 Td {body} ET'.encode('ascii'))]
    return assemble(objects)


def cases() -> dict[str, tuple[bytes, Expected]]:
    """``{rel_path: (bytes, Expected)}``: the PDF documents of the synthetic bundle."""
    text_pages = [list(LINES_EN[:3]), [], list(LINES_EN[3:])]
    sample = {
        1: ('page', {'kind': 'page', 'page': 1}, '\n'.join(LINES_EN[:3])),
        2: ('page', {'kind': 'page', 'page': 3}, '\n'.join(LINES_EN[3:])),
    }
    korean = [list(LINES_KO[:2]), list(LINES_KO[2:])]
    korean_sample = {
        1: ('page', {'kind': 'page', 'page': 1}, '\n'.join(LINES_KO[:2])),
        2: ('page', {'kind': 'page', 'page': 2}, '\n'.join(LINES_KO[2:])),
    }
    return {
        'docs/pdf_text.pdf': (latin_pdf(text_pages),
                              Expected('docs/pdf_text.pdf', 'pdf', 'ok', None, 2, sample)),
        'docs/pdf_korean.pdf': (korean_pdf(korean),
                                Expected('docs/pdf_korean.pdf', 'pdf', 'ok', None, 2, korean_sample)),
        'edge/pdf_no_text.pdf': (latin_pdf([[], []]),
                                 Expected('edge/pdf_no_text.pdf', 'pdf', 'failed', 'pdf_no_text')),
        'edge/pdf_encrypted.pdf': (latin_pdf(text_pages, encrypt=True),
                                   Expected('edge/pdf_encrypted.pdf', 'pdf', 'encrypted', 'pdf_encrypted')),
        'edge/pdf_not_a_pdf.pdf': (b'This is not a PDF file.\n',
                                   Expected('edge/pdf_not_a_pdf.pdf', 'pdf', 'unsupported', 'pdf_bad_signature')),
        'edge/pdf_damaged.pdf': (b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog\n',
                                 Expected('edge/pdf_damaged.pdf', 'pdf', 'failed', 'pdf_read_error')),
    }
