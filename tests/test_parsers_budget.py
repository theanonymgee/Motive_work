"""The file budget of the parsers, checked on their sources and on the synthetic bundle (parser resource
design §2, §6.2 S1-S5, §6.3 R22, R24)."""
import ast
import importlib.metadata
import inspect
from collections import Counter
from pathlib import Path

from fixtures import bundle
from workmcp import parsers
from workmcp.parsers import Limits, _container, docx, eml, hwp, pdf, pptx, text, xlsx

PARSERS = Path(parsers.__file__).resolve().parent
METERS = frozenset({'text', 'unit', 'event', 'hold', 'hold_bytes', 'clock'})
CHARGING_HELPERS = frozenset({'_keep', 'keep'})  # helpers that charge the budget before they keep a piece
# Local names that carry document content in the parsers.
CONTENT = frozenset({'value', 'text', 'data', 'payload', 'raw', 'piece', 'line', 'target', 'rel_id', 'name',
                     'cell_text', 'chunk', 'record', 'body', 'rest'})
# Functions that keep content without charging the budget themselves, each with the row of the design's
# table (§3) that bounds it. The list must match the sources exactly.
BOUNDED = {
    ('_container.py', '_handlers.start'): 'C4: the events of one fed piece, charged per piece by XmlStream',
    ('_container.py', '_handlers.end'): 'C4: as above',
    ('_container.py', '_handlers.text'): 'C4: as above',
    ('hwp.py', 'parse_hwpx'): 'C1: section part names, at most zip_max_entries zip members',
    ('_container.py', 'Pieces.append'): 'W1, P3, HX1, X1, X5: its callers charge every piece before adding it',
    ('pptx.py', '_Body.add'): 'P3: called right after _Body.keep charged the piece',
    ('pptx.py', '_Body.end'): 'P3: moves the joined text of pieces charged when they were read',
}


def _mentions(node: ast.AST) -> bool:
    return any(isinstance(sub, ast.Name) and sub.id in CONTENT for sub in ast.walk(node))


def _is_length(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) or (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'len')


def _own_nodes(function: ast.AST):
    """Nodes of a function body, without the bodies of the functions and classes defined in it."""
    stack = list(function.body)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in ast.iter_child_nodes(node)
                     if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)))


def _appenders(tree: ast.Module) -> set[str]:
    """Local names bound to a list's ``append``/``extend`` (``append = events.append``)."""
    return {target.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute) and node.value.attr in ('append', 'extend')
            for target in node.targets if isinstance(target, ast.Name)}


def _keeps(function, appenders: set[str]) -> bool:
    for node in _own_nodes(function):
        if isinstance(node, ast.Call) and ((isinstance(node.func, ast.Attribute) and node.func.attr in ('append', 'extend'))
                                           or (isinstance(node.func, ast.Name) and node.func.id in appenders)):
            if any(_mentions(arg) for arg in node.args):
                return True
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add) and not _is_length(node.value):
            if _mentions(node.value):
                return True
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Subscript) or (
                isinstance(target, ast.Tuple) and any(isinstance(item, ast.Subscript) for item in target.elts))
                for target in node.targets) and _mentions(node):
            return True
    return False


def _charges(function) -> bool:
    for node in _own_nodes(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in METERS:
            owner = func.value
            if (isinstance(owner, ast.Name) and owner.id == 'budget') or (
                    isinstance(owner, ast.Attribute) and owner.attr in ('budget', '_budget')):
                return True
        if (isinstance(func, ast.Name) and func.id in CHARGING_HELPERS) or (
                isinstance(func, ast.Attribute) and func.attr in CHARGING_HELPERS):
            return True
    return False


def _functions(tree: ast.Module):
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                yield name, child
                yield from walk(child, name + '.')
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, prefix + child.name + '.')
    yield from walk(tree, '')


def test_every_place_that_keeps_content_charges_the_budget():
    """S1: a function that appends, extends, adds to or stores document content charges the budget
    itself (or through ``_keep``), or is one of the bounded places of the design's table."""
    keepers, uncharged = set(), set()
    for path in sorted(PARSERS.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        appenders = _appenders(tree)
        for name, function in _functions(tree):
            if _keeps(function, appenders):
                keepers.add((path.name, name))
                if not _charges(function):
                    uncharged.add((path.name, name))
    assert uncharged == set(BOUNDED)
    assert len(keepers) > 15  # the check sees the parsers' keeping places at all


def test_release_is_used_by_the_xml_stream_only():
    """S2: only the XML stream gives held memory back (its per-part names, in a finally clause)."""
    uses = {path.name: path.read_text(encoding='utf-8').count('.release(') for path in PARSERS.glob('*.py')}
    assert {name: count for name, count in uses.items() if count} == {'_container.py': 1}
    source = inspect.getsource(_container.XmlStream.__iter__)
    assert 'finally:\n            budget.release(*held)' in source


def test_every_parser_takes_the_files_budget(monkeypatch):
    """S3: ``parse(data, limits, budget)`` everywhere, and the dispatcher makes one Budget per file."""
    for function in (text.parse, text.parse_csv, eml.parse, docx.parse, pptx.parse, xlsx.parse, pdf.parse,
                     hwp.parse, hwp.parse_hwpx):
        assert list(inspect.signature(function).parameters) == ['data', 'limits', 'budget'], function
    seen = []

    def fake(data, limits, budget):
        seen.append(budget)
        return [], None
    monkeypatch.setattr(parsers, 'parser_for', lambda ext: parsers.Parser('fake', 'fake-1', fake))
    limits = Limits(max_units=7)
    parsers.parse(b'synthetic', '.txt', limits)
    parsers.parse(b'synthetic', '.txt', limits)
    assert [type(budget) for budget in seen] == [_container.Budget] * 2 and seen[0] is not seen[1]
    assert seen[0].limits is limits and seen[0].units_left == 7


def test_budget_codes_are_fixed_codes_and_retired_ones_are_not_raised():
    """S4: the budget's codes are in the fixed set; ``xml_text`` and ``xml_tree`` stay valid codes for
    stored rows but no parser raises them."""
    for code in ('limit_events', 'limit_memory', 'unit_chars', 'pdf_stream', 'ole_entries', 'xml_text', 'xml_tree'):
        assert parsers.is_error_code('too_large', code)
    for path in PARSERS.glob('*.py'):
        if path.name != '__init__.py':
            source = path.read_text(encoding='utf-8')
            assert "'xml_text'" not in source and "'xml_tree'" not in source, path.name
    assert 'xml_max_text_chars' not in {field for field in Limits.__dataclass_fields__}


def test_versions_bumped():
    """R22: the refusal limits changed in every parser, so every parser version changed (verify then
    reports versions stored by the old parsers as a version mismatch instead of a failure)."""
    pypdf, olefile = importlib.metadata.version('pypdf'), importlib.metadata.version('olefile')
    assert {(module.NAME, module.VERSION) for module in (docx, pptx, xlsx, pdf, hwp, text, eml)} | {
        (text.CSV_NAME, text.CSV_VERSION), (hwp.HWPX_NAME, hwp.HWPX_VERSION)} == {
        ('docx', 'docx-5'), ('pptx', 'pptx-5'), ('xlsx', 'xlsx-5'), ('pdf', f'pdf-4/pypdf-{pypdf}'),
        ('hwp', f'hwp-5/olefile-{olefile}'), ('text', 'text-3'), ('eml', 'eml-4'), ('csv', 'csv-3'),
        ('hwpx', 'hwpx-5')}


def test_fixtures_bundle_unchanged():
    """R24 (G-1): the 51 documents of the synthetic bundle give the same statuses and units as before the
    budget (review v3 §1.2: ok 26 with 133 units, failed 13, unsupported 7, encrypted 5)."""
    statuses, units = Counter(), 0
    for item, data in bundle.documents():
        ext = '.' + item.rel_path.rsplit('.', 1)[1]
        result = parsers.parse(data, ext, Limits())
        assert (result.status, result.error, len(result.units)) == (item.status, item.error, item.unit_count), \
            item.rel_path
        statuses[result.status] += 1
        units += len(result.units)
    assert statuses == {'ok': 26, 'failed': 13, 'unsupported': 7, 'encrypted': 5} and units == 133


def test_every_parser_charges_its_own_units_and_text():
    """Review v4 M1-L18: the dispatcher checks units and text again at the end, so a parser that forgot
    a charge still gave the right code and every other test passed. Here each bundle document is parsed
    by its module directly: the units charged must be exactly the units returned, and the text charged at
    least their text (a shared string or boolean cell charges its text; pieces may count a little more)."""
    checked = 0
    for item, data in bundle.documents():
        if item.status != 'ok' or not data:
            continue
        ext = '.' + item.rel_path.rsplit('.', 1)[1]
        limits = Limits()
        budget = _container.Budget(limits)
        units, _ = parsers.parser_for(ext).parse(data, limits, budget)
        assert limits.max_units - budget.units_left == len(units), item.rel_path
        assert limits.max_text_chars - budget.text_left >= sum(len(unit.text) for unit in units), item.rel_path
        checked += 1
    assert checked >= 25


def test_xlsx_sheet_targets_are_held_without_the_dispatcher():
    """M1-L18: a held table is never checked again by the dispatcher, so the charge is checked directly."""
    import io
    import zipfile
    from fixtures import make_ooxml
    data = make_ooxml.xlsx_sheets(1)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    target = 'worksheets/' + 'x' * 50_000 + '/../sheet1.xml'
    rels = members['xl/_rels/workbook.xml.rels'].decode()
    members['xl/_rels/workbook.xml.rels'] = rels.replace('worksheets/sheet1.xml', target).encode()
    data = make_ooxml.zip_bytes(members)
    limits = Limits(max_held_bytes=150_000)  # the target alone is 4 x 50,000 bytes in the model
    try:
        xlsx.parse(data, limits, _container.Budget(limits))
    except parsers.ParseFailure as failure:
        assert (failure.status, failure.code) == ('too_large', 'limit_memory')
    else:
        raise AssertionError('the relationship target was not held')
    assert parsers.parse(data, '.xlsx', Limits()).status == 'ok'
