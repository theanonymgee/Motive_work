"""Synthetic DOCX, XLSX and PPTX packages (design M1 §7.3), written with ``zipfile`` and text templates.

Each package holds only the parts its parser reads plus the content-type and relationship files
that make it a plausible package. ``build()`` returns ``(Expected, bytes)`` pairs parsed with the
default ``Limits``; ``write(root)`` writes them below ``root`` (``docs/``, ``sheets/``, ``edge/``).
Cases that need a small ``Limits`` (zip member size, slide and sheet counts) or hand-damaged bytes
are built by the named functions below and checked by the parser tests only.

Text comes from ``fixtures.LINES_KO``/``LINES_EN`` and made-up values; every document carries
``SENTINEL``. No bytes are committed: documents exist only while a test runs.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import LINES_EN, LINES_KO, PROJECTS, SENTINEL, Expected

XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
NS_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
NS_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
NS_MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
NS_WPS = 'http://schemas.microsoft.com/office/word/2010/wordprocessingShape'
NS_V = 'urn:schemas-microsoft-com:vml'
NS_S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
NS_P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
NS_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
NS_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
NS_CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
REL_TYPE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def zip_bytes(parts: dict[str, str | bytes]) -> bytes:
    """A deflated zip with ``parts`` in insertion order."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, (2031, 3, 4, 10, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return out.getvalue()


def without_member(data: bytes, name: str) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    return zip_bytes({member: source.read(member) for member in source.namelist() if member != name})


def _content_types(*overrides: tuple[str, str]) -> str:
    items = ''.join(f'<Override PartName="/{name}" ContentType="{kind}"/>' for name, kind in overrides)
    return (f'{XML_HEAD}<Types xmlns="{NS_CT}"><Default Extension="rels" '
            f'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            f'<Default Extension="xml" ContentType="application/xml"/>{items}</Types>')


def _relationships(*rels: tuple[str, str, str]) -> str:
    items = ''.join(f'<Relationship Id="{rid}" Type="{REL_TYPE}/{kind}" Target="{target}"/>' for rid, kind, target in rels)
    return f'{XML_HEAD}<Relationships xmlns="{NS_REL}">{items}</Relationships>'


# ---- DOCX -------------------------------------------------------------------------------------

def w_run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def w_p(*runs: str) -> str:
    return '<w:p>' + ''.join(runs) + '</w:p>'


def w_table(rows: list[list[str]]) -> str:
    body = ''.join('<w:tr>' + ''.join(f'<w:tc>{w_p(w_run(cell))}</w:tc>' for cell in row) + '</w:tr>' for row in rows)
    return f'<w:tbl><w:tblPr/><w:tblGrid/>{body}</w:tbl>'


def docx_package(body: str, *, extra: dict | None = None, drop_document: bool = False) -> bytes:
    document = (f'{XML_HEAD}<w:document xmlns:w="{NS_W}" xmlns:r="{NS_R}" xmlns:v="{NS_V}" xmlns:mc="{NS_MC}" '
                f'xmlns:wps="{NS_WPS}"><w:body>{body}</w:body></w:document>')
    parts = {
        '[Content_Types].xml': _content_types((
            'word/document.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml')),
        '_rels/.rels': _relationships(('rId1', 'officeDocument', 'word/document.xml')),
        'word/document.xml': document,
    }
    parts.update(extra or {})
    if drop_document:
        del parts['word/document.xml']
    return zip_bytes(parts)


def docx_normal() -> tuple[Expected, bytes]:
    """15 ``w:p`` in all (see the comments); 12 of them carry text."""
    textbox = ('<w:r><w:pict><v:shape><v:textbox><w:txbxContent>'
               + w_p(w_run('글상자 안 문장')) + '</w:txbxContent></v:textbox></v:shape></w:pict></w:r>')
    alternate = (
        '<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:wsp><wps:txbx><w:txbxContent>'
        + w_p(w_run('대체 콘텐츠 상자'))
        + '</w:txbxContent></wps:txbx></wps:wsp></w:drawing></mc:Choice>'
        '<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>' + w_p(w_run('대체 콘텐츠 상자'))
        + '</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>')
    body = ''.join([
        w_p(w_run(LINES_KO[0])),                                                                  # 1
        w_p(w_run(LINES_EN[1])),                                                                  # 2
        w_p(w_run('담당자 갑'), '<w:r><w:tab/></w:r>', w_run('가상 거래처 A'), '<w:r><w:br/></w:r>',
            w_run('다음 줄')),                                                                     # 3
        w_p(),                                                                                    # 4 (empty)
        w_p(w_run('삭제 표시 뒤에도 남는 문장'),
            '<w:del><w:r><w:delText>삭제된 문장</w:delText></w:r></w:del>'),                       # 5
        w_table([['항목', '금액'], ['견적', '1,234,000원']]),                                     # 6-9
        w_p(w_run('상자 앞 문장 '), textbox, w_run(' 상자 뒤 문장')),                              # 10 (11 inside)
        w_p(alternate),                                                                           # 12 (13 Choice, 14 Fallback)
        w_p(w_run(LINES_KO[2])),                                                                  # 15
    ])

    def para(index, text, **cell):
        return ('paragraph', {'kind': 'paragraph', 'index': index, **cell}, text)

    cells = [
        para(1, LINES_KO[0]), para(2, LINES_EN[1]), para(3, '담당자 갑\t가상 거래처 A\n다음 줄'),
        para(5, '삭제 표시 뒤에도 남는 문장'),
        para(6, '항목', table=1, row=1, column=1), para(7, '금액', table=1, row=1, column=2),
        para(8, '견적', table=1, row=2, column=1), para(9, '1,234,000원', table=1, row=2, column=2),
        para(10, '상자 앞 문장  상자 뒤 문장'), para(11, '글상자 안 문장'), para(13, '대체 콘텐츠 상자'),
        para(15, LINES_KO[2])]
    sample = dict(enumerate(cells, 1))
    return Expected('docs/report.docx', 'docx', 'ok', None, len(sample), sample), docx_package(body)


def docx_nested_tables() -> bytes:
    """A table in a cell of another table: the inner one is table 2 and its cell uses inner coordinates."""
    outer = ('<w:tbl><w:tr><w:tc>' + w_p(w_run('바깥 칸')) + w_table([['안쪽 칸']]) + '</w:tc><w:tc>'
             + w_p(w_run('바깥 둘째')) + '</w:tc></w:tr></w:tbl>')
    return docx_package(outer + w_p(w_run(SENTINEL)))


def docx_bad_zip() -> bytes:
    return b'PK\x03\x04' + '쓰레기 바이트'.encode() * 10


def docx_missing_document() -> bytes:
    return docx_package('', drop_document=True)


def docx_with_dtd() -> bytes:
    document = (f'{XML_HEAD}<!DOCTYPE w:document [<!ENTITY e "합성">]><w:document xmlns:w="{NS_W}"><w:body>'
                f'{w_p(w_run("&e;"))}</w:body></w:document>')
    return docx_package('', extra={'word/document.xml': document})


def docx_with_external_entity() -> bytes:
    document = (f'{XML_HEAD}<!DOCTYPE w:document [<!ENTITY e SYSTEM "file:///synthetic.invalid/none">]>'
                f'<w:document xmlns:w="{NS_W}"><w:body>{w_p(w_run("&e;"))}</w:body></w:document>')
    return docx_package('', extra={'word/document.xml': document})


def docx_invalid_xml() -> bytes:
    return docx_package('', extra={'word/document.xml': f'{XML_HEAD}<w:document xmlns:w="{NS_W}"><w:body><w:p>'})


def docx_big_member(size: int = 2 * 2**20) -> bytes:
    """A document whose ``word/document.xml`` unpacks to ``size`` bytes (use ``zip_max_part_bytes`` below it)."""
    document = (f'{XML_HEAD}<w:document xmlns:w="{NS_W}"><w:body>{w_p(w_run(SENTINEL))}</w:body>'
                f'</w:document>{" " * size}')
    return docx_package('', extra={'word/document.xml': document})


# ---- XLSX -------------------------------------------------------------------------------------

def _sheet(*rows: str) -> str:
    return f'{XML_HEAD}<worksheet xmlns="{NS_S}"><sheetData>{"".join(rows)}</sheetData></worksheet>'


def xlsx_package(sheets: list[tuple[str, str, str]], shared: str | None) -> bytes:
    """``sheets``: ``(sheet name, member name, sheet xml)`` in workbook order."""
    listed = ''.join(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n + 1}"/>'
                     for n, (name, _, _) in enumerate(sheets, 1))
    rels = [(f'rId{n + 1}', 'worksheet', member.removeprefix('xl/')) for n, (_, member, _) in enumerate(sheets, 1)]
    parts = {
        '[Content_Types].xml': _content_types((
            'xl/workbook.xml', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')),
        '_rels/.rels': _relationships(('rId1', 'officeDocument', 'xl/workbook.xml')),
        'xl/workbook.xml': f'{XML_HEAD}<workbook xmlns="{NS_S}" xmlns:r="{NS_R}"><sheets>{listed}</sheets></workbook>',
        'xl/_rels/workbook.xml.rels': _relationships(*rels),
    }
    if shared is not None:
        parts['xl/sharedStrings.xml'] = shared
    for _, member, xml in sheets:
        parts[member] = xml
    return zip_bytes(parts)


def _c(address: str | None, kind: str | None, inner: str) -> str:
    attrs = (f' r="{address}"' if address else '') + (f' t="{kind}"' if kind else '')
    return f'<c{attrs}>{inner}</c>'


def _t(text: str) -> str:
    return f'<t xml:space="preserve">{escape(text)}</t>'


SHARED_STRINGS = (
    f'{XML_HEAD}<sst xmlns="{NS_S}" count="5" uniqueCount="4">'
    f'<si>{_t(f"{SENTINEL} {PROJECTS[0]}")}</si>'
    f'<si>{_t("견적")}</si>'
    f'<si><r>{_t("가상 ")}</r><r>{_t("거래처 A")}</r><rPh sb="0" eb="2">{_t("발음표기")}</rPh></si>'
    f'<si>{_t(" 공백 포함 ")}</si></sst>')


def xlsx_normal() -> tuple[Expected, bytes]:
    """Two sheets (the first listed one is stored in ``sheet2.xml``); 14 cells carry a value."""
    first = _sheet(
        '<row r="1">' + _c('A1', 's', '<v>0</v>') + _c('B1', 's', '<v>2</v>') + '</row>',
        '<row r="2">' + _c('A2', 'inlineStr', f'<is>{_t("인라인 문자열")}</is>') + _c('B2', None, '<v>1234000</v>')
        + _c('C2', 'b', '<v>1</v>') + _c('D2', 'b', '<v>0</v>') + '</row>',
        '<row r="3">' + _c('A3', None, '<f>SUM(B2:B2)</f><v>3</v>') + _c('B3', None, '<f>NOW()</f>')
        + _c('C3', None, '') + _c('D3', 'str', '<f>"x"</f><v>문자열 수식</v>') + '</row>',
        '<row r="4">' + _c('A4', 'e', '<v>#DIV/0!</v>') + '</row>',
        '<row r="5">' + _c(None, 'inlineStr', f'<is>{_t("주소 없는 셀")}</is>') + _c(None, None, '<v>7</v>') + '</row>')
    second = _sheet(
        '<row r="1">' + _c('A1', 's', '<v>1</v>') + '</row>',
        '<row r="2">' + _c('B2', 'inlineStr', f'<is><r>{_t("A")}</r><r>{_t("B")}</r></is>') + '</row>',
        '<row r="3">' + _c('C3', 's', '<v>3</v>') + '</row>')

    def cell(sheet, address, text):
        return ('sheet_cell', {'kind': 'sheet_cell', 'sheet': sheet, 'cell': address}, text)

    s1, s2 = '합성 시트', 'Sheet2'
    cells = [
        cell(s1, 'A1', f'{SENTINEL} {PROJECTS[0]}'), cell(s1, 'B1', '가상 거래처 A'), cell(s1, 'A2', '인라인 문자열'),
        cell(s1, 'B2', '1234000'), cell(s1, 'C2', 'TRUE'), cell(s1, 'D2', 'FALSE'), cell(s1, 'A3', '3'),
        cell(s1, 'D3', '문자열 수식'), cell(s1, 'A4', '#DIV/0!'), cell(s1, 'A5', '주소 없는 셀'), cell(s1, 'B5', '7'),
        cell(s2, 'A1', '견적'), cell(s2, 'B2', 'AB'), cell(s2, 'C3', '공백 포함')]
    sample = dict(enumerate(cells, 1))
    data = xlsx_package([(s1, 'xl/worksheets/sheet2.xml', first), (s2, 'xl/worksheets/sheet1.xml', second)],
                        SHARED_STRINGS)
    return Expected('sheets/book.xlsx', 'xlsx', 'ok', None, len(sample), sample), data


def xlsx_without_shared_strings() -> bytes:
    sheet = _sheet('<row r="1">' + _c('A1', 'inlineStr', f'<is>{_t(SENTINEL)}</is>') + _c('B1', None, '<v>5</v>')
                   + '</row>')
    return xlsx_package([('시트', 'xl/worksheets/sheet1.xml', sheet)], None)


def xlsx_sheets(count: int) -> bytes:
    sheet = _sheet('<row r="1">' + _c('A1', 'inlineStr', f'<is>{_t(SENTINEL)}</is>') + '</row>')
    return xlsx_package([(f'시트{n}', f'xl/worksheets/sheet{n}.xml', sheet) for n in range(1, count + 1)], None)


def xlsx_formula_only() -> bytes:
    sheet = _sheet('<row r="1">' + _c('A1', None, '<f>1+1</f>') + _c('B1', None, '') + '</row>')
    return xlsx_package([('시트', 'xl/worksheets/sheet1.xml', sheet)], SHARED_STRINGS)


def xlsx_with_dtd() -> bytes:
    sheet = f'{XML_HEAD}<!DOCTYPE worksheet [<!ENTITY e "합성">]><worksheet xmlns="{NS_S}"><sheetData/></worksheet>'
    return xlsx_package([('시트', 'xl/worksheets/sheet1.xml', sheet)], None)


def xlsx_missing_sheet() -> bytes:
    return without_member(xlsx_package([('시트', 'xl/worksheets/sheet1.xml', _sheet())], None),
                          'xl/worksheets/sheet1.xml')


# ---- PPTX -------------------------------------------------------------------------------------

def a_p(*runs: str) -> str:
    return '<a:p>' + ''.join(runs) + '</a:p>'


def a_r(text: str) -> str:
    return f'<a:r><a:t>{escape(text)}</a:t></a:r>'


def p_sp(text_paragraphs: list[str] | None, name: str = 'shape') -> str:
    body = '' if text_paragraphs is None else '<p:txBody><a:bodyPr/>' + ''.join(text_paragraphs) + '</p:txBody>'
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="1" name="{name}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/>'
            f'{body}</p:sp>')


def p_pic() -> str:
    return ('<p:pic><p:nvPicPr><p:cNvPr id="2" name="pic"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill/>'
            '<p:spPr/></p:pic>')


def p_table(rows: list[list[str]]) -> str:
    def cell(text):
        return f'<a:tc><a:txBody><a:bodyPr/>{a_p(a_r(text)) if text else "<a:p/>"}</a:txBody></a:tc>'

    body = ''.join('<a:tr h="0">' + ''.join(cell(text) for text in row) + '</a:tr>' for row in rows)
    return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3" name="table"/><p:cNvGraphicFramePr/><p:nvPr/>'
            f'</p:nvGraphicFramePr><p:xfrm/><a:graphic><a:graphicData uri="{NS_A}/table"><a:tbl><a:tblPr/>'
            f'<a:tblGrid/>{body}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')


def p_group(*children: str) -> str:
    return (f'<p:grpSp><p:nvGrpSpPr><p:cNvPr id="4" name="group"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            f'<p:grpSpPr/>{"".join(children)}</p:grpSp>')


def _slide(*shapes: str) -> str:
    return (f'{XML_HEAD}<p:sld xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}"><p:cSld><p:spTree>'
            f'<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
            f'{"".join(shapes)}</p:spTree></p:cSld></p:sld>')


def pptx_package(slides: list[tuple[str, str]], order: list[str]) -> bytes:
    """``slides``: ``(rId, slide xml)`` stored as ``ppt/slides/slide<n>.xml`` in this order;
    ``order``: the rIds in ``p:sldIdLst`` (the presentation order)."""
    ids = ''.join(f'<p:sldId id="{256 + n}" r:id="{rid}"/>' for n, rid in enumerate(order))
    rels = [(rid, 'slide', f'slides/slide{n}.xml') for n, (rid, _) in enumerate(slides, 1)]
    parts = {
        '[Content_Types].xml': _content_types((
            'ppt/presentation.xml',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml')),
        '_rels/.rels': _relationships(('rId1', 'officeDocument', 'ppt/presentation.xml')),
        'ppt/presentation.xml': (f'{XML_HEAD}<p:presentation xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}">'
                                 f'<p:sldIdLst>{ids}</p:sldIdLst></p:presentation>'),
        'ppt/_rels/presentation.xml.rels': _relationships(*rels),
    }
    for n, (_, xml) in enumerate(slides, 1):
        parts[f'ppt/slides/slide{n}.xml'] = xml
    return zip_bytes(parts)


def pptx_normal() -> tuple[Expected, bytes]:
    """Presentation slide 1 is ``slide2.xml`` (title, body) and slide 2 is ``slide1.xml`` (table, groups)."""
    title_body = _slide(
        p_sp([a_p(a_r(f'{PROJECTS[0]} 주간 보고 {SENTINEL}'))], 'title'),                         # shape 1
        p_sp([a_p(a_r(LINES_KO[1])), a_p(a_r('첫 줄'), '<a:br/>', a_r('둘째 줄'))], 'body'),        # shape 2
        p_pic(),                                                                                  # shape 3
        p_sp(None),                                                                               # shape 4 (no text)
        p_sp([a_p(a_r('마지막 도형'))]))                                                           # shape 5
    table_groups = _slide(
        p_table([['구분', '값'], ['건수', '7건'], ['', '']]),                                     # shape 1
        p_group(p_sp([a_p(a_r('그룹 안 도형'))]),                                                  # group 2, shape 3
                p_group(p_sp([a_p(a_r('중첩 그룹 도형'))]))),                                      # group 4, shape 5
        p_sp([a_p(a_r('그룹 밖 도형'))]))                                                          # shape 6

    def shape(slide, number, text):
        return ('slide', {'kind': 'slide', 'slide': slide, 'shape': number}, text)

    def cell(row, column, text):
        return ('slide_cell', {'kind': 'slide_cell', 'slide': 2, 'shape': 1, 'row': row, 'column': column}, text)

    items = [
        shape(1, 1, f'{PROJECTS[0]} 주간 보고 {SENTINEL}'), shape(1, 2, f'{LINES_KO[1]}\n첫 줄\n둘째 줄'),
        shape(1, 5, '마지막 도형'),
        cell(1, 1, '구분'), cell(1, 2, '값'), cell(2, 1, '건수'), cell(2, 2, '7건'),
        shape(2, 3, '그룹 안 도형'), shape(2, 5, '중첩 그룹 도형'), shape(2, 6, '그룹 밖 도형')]
    sample = dict(enumerate(items, 1))
    data = pptx_package([('rId2', table_groups), ('rId3', title_body)], ['rId3', 'rId2'])
    return Expected('docs/deck.pptx', 'pptx', 'ok', None, len(sample), sample), data


def pptx_empty_slide() -> bytes:
    return pptx_package([('rId2', _slide(p_pic(), p_sp(None)))], ['rId2'])


def pptx_slides(count: int) -> bytes:
    slides = [(f'rId{n + 1}', _slide(p_sp([a_p(a_r(SENTINEL))]))) for n in range(1, count + 1)]
    return pptx_package(slides, [rid for rid, _ in slides])


def pptx_missing_slide() -> bytes:
    data = pptx_package([('rId2', _slide(p_sp([a_p(a_r(SENTINEL))])))], ['rId2'])
    return without_member(data, 'ppt/slides/slide1.xml')


def pptx_with_dtd() -> bytes:
    slide = _slide(p_sp([a_p(a_r(SENTINEL))])).replace('<p:sld ', '<!DOCTYPE p:sld [<!ENTITY e "합성">]><p:sld ', 1)
    return pptx_package([('rId2', slide)], ['rId2'])


# ---- bundle -----------------------------------------------------------------------------------

def build() -> list[tuple[Expected, bytes]]:
    return [
        docx_normal(),
        xlsx_normal(),
        pptx_normal(),
        (Expected('edge/bad_zip.docx', 'docx', 'failed', 'bad_zip'), docx_bad_zip()),
        (Expected('edge/missing_part.docx', 'docx', 'failed', 'missing_part'), docx_missing_document()),
        (Expected('edge/dtd.docx', 'docx', 'failed', 'xml_dtd'), docx_with_dtd()),
        (Expected('edge/invalid_xml.docx', 'docx', 'failed', 'xml_invalid'), docx_invalid_xml()),
        (Expected('edge/empty_slide.pptx', 'pptx', 'ok'), pptx_empty_slide()),
        (Expected('edge/formula_only.xlsx', 'xlsx', 'ok'), xlsx_formula_only()),
    ]


def write(root: Path) -> list[Expected]:
    expected = []
    for item, data in build():
        target = Path(root) / item.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        expected.append(item)
    return expected
