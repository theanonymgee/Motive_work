"""Extended synthetic corpus for search evaluation (design M2 §8).

The M1 bundle repeats ``LINES_KO``/``LINES_EN`` in every document, so it cannot tell which
document answers a query; it stays the functional fixture (binding, increments). This corpus has
one topic card per document (60), built with the low-level writers of the existing generators
(``make_text``-style lines, ``make_eml`` headers, ``make_ooxml``, ``make_pdf.korean_pdf``,
``make_hwp``), and about 120 queries with gold units. The rules of ``fixtures/__init__.py`` (1-4)
hold: no real person, company, address or domain, ``SENTINEL`` in every document, reserved mail
domains only. Every name, number and date below is made up.

``build(seed=7) -> Corpus`` is pure and deterministic; ``write(corpus, root)`` writes the 60
documents below ``root`` (a temporary folder, never the repository); ``queries(corpus)`` lists the
queries. Gold answers are units, computed here from the unit rules of each format (design M1 §5.8)
in the order the sentences are written; ``tests/test_corpus.py`` checks them against the parsers.

Metrics for one query (``score_query``) and per category (``summarize``) are here too, so the
pytest evaluation and the manual probe (``tests/probes/measure_search.py``) compute them the same way.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from . import PEOPLE, SENTINEL, mail_address
from . import make_hwp, make_ooxml, make_pdf

# Query categories (design M2 §8.3); Q8/Q9 are recorded as behaviour, outside the metric tables.
CATEGORIES = ('Q1_phrase', 'Q2_spacing', 'Q3_acronym', 'Q4_synonym', 'Q5_number', 'Q6_mixed', 'Q7_question',
              'Q8_two_chars', 'Q9_absent')
METRIC_CATEGORIES = CATEGORIES[:7]
CATEGORY_COUNTS = {'Q1_phrase': 20, 'Q2_spacing': 15, 'Q3_acronym': 15, 'Q4_synonym': 15, 'Q5_number': 15,
                   'Q6_mixed': 10, 'Q7_question': 15, 'Q8_two_chars': 10, 'Q9_absent': 5}
FORMATS = ('txt', 'md', 'docx', 'xlsx', 'pptx', 'eml', 'pdf', 'hwpx', 'hwp')
FORMAT_COUNTS = {'txt': 10, 'md': 6, 'docx': 12, 'xlsx': 6, 'pptx': 6, 'eml': 8, 'pdf': 6, 'hwpx': 4, 'hwp': 2}
TOPIC_COUNT = 60
DEFAULT_SEED = 7

# (rel_path, unit dict): a gold answer is a unit of one document, as its locator names it.
GoldUnit = tuple[str, dict]

CUSTOMERS = tuple(f'가상 거래처 {letter}' for letter in 'ABCDEFGHIJKL')
PERSONS = tuple(f'담당자 {name}' for name in ('갑', '을', '병', '정', '무', '기', '경'))
PERSONS_EN = {person: f'Owner {name}' for person, name in zip(PERSONS, ('Gap', 'Eul', 'Byeong', 'Jeong', 'Mu',
                                                                         'Gi', 'Gyeong'))}
ACRONYMS = (('MOU', '업무협약'), ('NDA', '비밀유지계약'), ('PoC', '개념검증'), ('QA', '품질보증'),
            ('RFP', '제안요청'), ('SLA', '서비스수준'), ('KPI', '핵심지표'), ('BOM', '자재명세'))
SYNONYMS = (('일정', '스케줄'), ('거래처', '고객사'), ('견적', '가격제안'), ('회의록', '미팅노트'),
            ('지연', '연기'), ('검수', '인수검사'))
# One sentence per synonym pair; {w} is the side the document uses (both sides read the same way).
SYNONYM_TEMPLATES = {
    '일정': '{project} 세부 {w}은 {person} 담당으로 다시 짰다.',
    '거래처': '{code} 건의 {w} 응대 창구는 {person} 한 명으로 일원화한다.',
    '견적': '{customer}에 보낼 {w}은 수량을 줄여 다시 제출하기로 했다.',
    '회의록': '{project} 착수 {w} 작성은 {person} 담당이다.',
    '지연': '{project} 장비 납품이 부품 수급 문제로 두 주 {w}되었다.',
    '검수': '{customer} 납품분 {w} 결과 경미한 불량 두 건이 나왔다.',
}
# Query context per synonym pair: words of the sentence other than the synonym (no number).
SYNONYM_QUERIES = {
    '일정': '{project} 세부 {w} 재작성',
    '거래처': '{code} {w} 응대 창구',
    '견적': '{customer} {w} 재제출',
    '회의록': '{project} 착수 {w} 작성자',
    '지연': '{project} 장비 납품 {w}',
    '검수': '{customer} 납품분 {w} 불량',
}
KO_OPTIONAL = {
    'risk': '{project} 위험 항목은 {n1}건, 보류 항목은 {n2}건으로 집계됐다.',
    'visit': '{person}은 {customer} 방문 결과를 주간 보고에 정리했다.',
    'testenv': '{customer} 요청으로 시험 환경 장비를 {n1}대 추가 구성한다.',
    'budget': '{project} 예산 집행 현황 점검은 {person} 담당이다.',
    'request': '{customer} 측 요구사항 변경 요청서를 {n1}건 접수했다.',
    'pilot': '다음 단계로 {project} 시범 운영 범위를 {n1}개 부서로 정한다.',
}
EN_OPTIONAL = {
    'quote': 'Quote {qnum} for {code} was sent to {customer_en}.',
    'review': '{code} weekly review covered {n1} open issues.',
    'owner': '{person_en} will update the {code} test plan by Friday.',
}
DISTRACTOR_PREFIX = '참고: '
# Q7: natural questions on a sentence kind (paraphrases, not excerpts).
QUESTION_TEMPLATES = {
    'amount': '{project} 계약은 얼마에 확정됐어?',
    'date': '{project} 중간 점검은 언제 하나요?',
    'acr': '{customer}와 맺을 {acr_other} 검토는 누가 맡고 있지?',
    'code': '{code} 사양서는 누가 관리하나?',
    'visit': '{person}이 {customer}를 찾아간 결과는 어디에 적혔나?',
    'risk': '{project}에서 위험하거나 보류된 항목은 몇 개야?',
    'testenv': '{customer}가 시험용 장비를 몇 대 더 달라고 했나?',
    'budget': '{project} 돈 쓰는 현황은 누가 확인해?',
    'request': '{customer}에서 들어온 요구사항 변경은 몇 건이야?',
    'quote': '{code} 견적서는 어느 거래처로 보냈나?',
}
TWO_CHAR_QUERIES = ('견적', '일정', '회의', '납품', '예산', '시험', '위험', 'QA', '부품', '방문')
ABSENT_QUERIES = ('화성 탐사선 연료 보급 계획', '양자 암호 장비 반입 절차', '심해 케이블 인양 작업 지침',
                  '빙하 시추 표본 보관 규정', '성층권 기구 발사 허가 신청')


@dataclass(frozen=True)
class Topic:
    """One topic card = one document (design M2 §8.2). Every value is synthetic."""
    number: int                          # 1..60
    fmt: str                             # one of FORMATS
    rel_path: str                        # where ``write`` puts the document under the corpus root
    project: str                         # '합성 프로젝트 N호'
    project_code: str                    # 'PRJ-0NN'
    customer: str                        # '가상 거래처 A'..'L'
    person: str                          # '담당자 갑'..'경'
    date_ko: str                         # '2031년 3월 4일'
    date_iso: str                        # '2031-03-04'
    amount: str                          # the one spelling used in the document ('1,234,000원', ...)
    acronym: str                         # 'MOU', 'NDA', ... (the document has this or its expansion)
    acronym_ko: str                      # '업무협약', ...
    acronym_in_doc: str                  # which of the two the document contains
    synonym_pair: tuple[str, str]        # ('일정', '스케줄'), ...
    synonym_in_doc: str                  # which side the document contains
    sentinel: str                        # '주제표지-NNN', only in this document
    body_sentences: tuple[str, ...]      # 8-12 topic sentences (about 80 % Korean, 20 % English)
    distractors: tuple[str, ...] = ()    # sentences of other cards mixed in as "참고"
    date_in_doc: str = 'ko'              # 'ko' or 'iso': which date spelling the document has
    amount_forms: tuple[str, str, str] = ('', '', '')   # (comma, man/cheon, plain digits)
    kinds: tuple[str, ...] = ()          # sentence kind per body sentence ('sentinel', 'acr', ...)
    qnum: str = ''                       # 'Q-1017': the quote number of the English quote sentence


@dataclass(frozen=True)
class Query:
    """One evaluation query (design M2 §8.3)."""
    id: str                              # 'Q1-001'
    category: str                        # one of CATEGORIES
    text: str
    gold: tuple[GoldUnit, ...] = ()      # empty for Q9
    gold_docs: tuple[str, ...] = ()      # rel_paths of the answering documents (1-3; Q8 may have more)


@dataclass(frozen=True)
class Corpus:
    seed: int
    topics: tuple[Topic, ...]
    queries: tuple[Query, ...] = ()
    units: dict[str, tuple[dict, ...]] = field(default_factory=dict)   # rel_path -> every unit dict written
    unit_texts: dict[str, tuple[str, ...]] = field(default_factory=dict)  # rel_path -> text per unit


# ---- topic cards ----------------------------------------------------------------------------------

def _amount_forms(value: int) -> tuple[str, str, str]:
    man, cheon = divmod(value // 1000, 10)
    spoken = f'{man}만 {cheon}천원' if cheon else f'{man}만원'
    return f'{value:,}원', spoken, str(value)


def _topic(number: int, fmt: str, rng: random.Random) -> Topic:
    project, code = f'합성 프로젝트 {number}호', f'PRJ-{number:03d}'
    customer, person = rng.choice(CUSTOMERS), rng.choice(PERSONS)
    year, month, day = rng.randint(2029, 2032), rng.randint(1, 12), rng.randint(1, 28)
    date_ko, date_iso = f'{year}년 {month}월 {day}일', f'{year}-{month:02d}-{day:02d}'
    date_in_doc = rng.choice(('ko', 'iso'))
    forms = _amount_forms(rng.randrange(1000, 99999) * 1000)
    amount = rng.choice(forms)
    acronym, acronym_ko = rng.choice(ACRONYMS)
    acronym_in_doc = rng.choice((acronym, acronym_ko))
    pair = rng.choice(SYNONYMS)
    synonym_in_doc = rng.choice(pair)
    sentinel = f'주제표지-{number:03d}'
    qnum = f'Q-{1000 + 17 * number}'
    values = {'project': project, 'code': code, 'customer': customer, 'person': person, 'qnum': qnum,
              'customer_en': f'Fictional Customer {customer[-1]}', 'person_en': PERSONS_EN[person]}
    date = date_ko if date_in_doc == 'ko' else date_iso
    sentences = [
        ('sentinel', f'{SENTINEL} {sentinel} {project} 진행 기록이다.'),
        ('acr', f'{customer} 측과 {acronym_in_doc} 초안 검토는 {person} 담당이다.'),
        ('syn', SYNONYM_TEMPLATES[pair[0]].format(w=synonym_in_doc, **values)),
        ('amount', f'{project} 계약 금액은 {amount}으로 확정했다.'),
        ('date', f'{project} 중간 점검 회의는 {date}에 열린다.'),
        ('code', f'{code} 사양서 개정판은 {person} 담당으로 관리한다.'),
    ]
    for kind in rng.sample(sorted(KO_OPTIONAL), rng.randint(1, 4)):
        sentences.append((kind, KO_OPTIONAL[kind].format(n1=rng.randint(2, 9), n2=rng.randint(1, 5), **values)))
    for kind in rng.sample(sorted(EN_OPTIONAL), rng.randint(1, 2)):
        sentences.append((kind, EN_OPTIONAL[kind].format(n1=rng.randint(2, 9), **values)))
    head, rest = sentences[:1], sentences[1:]
    rng.shuffle(rest)
    sentences = head + rest
    ext = fmt
    return Topic(number=number, fmt=fmt, rel_path=f'{fmt}/topic{number:02d}.{ext}', project=project,
                 project_code=code, customer=customer, person=person, date_ko=date_ko, date_iso=date_iso,
                 amount=amount, acronym=acronym, acronym_ko=acronym_ko, acronym_in_doc=acronym_in_doc,
                 synonym_pair=pair, synonym_in_doc=synonym_in_doc, sentinel=sentinel,
                 body_sentences=tuple(text for _, text in sentences), date_in_doc=date_in_doc,
                 amount_forms=forms, kinds=tuple(kind for kind, _ in sentences), qnum=qnum)


def _with_distractors(topics: list[Topic], rng: random.Random) -> list[Topic]:
    """Each card's 1-2 sentences go into the documents of 2 other cards as "참고" lines."""
    extra: dict[int, list[str]] = {topic.number: [] for topic in topics}
    for topic in topics:
        candidates = [text for kind, text in zip(topic.kinds, topic.body_sentences) if kind != 'sentinel']
        picked = rng.sample(candidates, rng.randint(1, 2))
        hosts = rng.sample([other.number for other in topics if other.number != topic.number], 2)
        for host in hosts:
            extra[host].extend(DISTRACTOR_PREFIX + text for text in picked)
    out = []
    for topic in topics:
        mine = extra[topic.number]
        rng.shuffle(mine)
        out.append(Topic(**{**topic.__dict__, 'distractors': tuple(mine)}))
    return out


def lines_of(topic: Topic) -> list[str]:
    """The document's sentences in writing order: body, with the distractors spread after the first."""
    body = list(topic.body_sentences)
    for offset, line in enumerate(topic.distractors):
        body.insert(2 + 3 * offset, line)
    return body


# ---- documents (bytes + units, per format) --------------------------------------------------------

def _txt(topic: Topic, lines: list[str]):
    raw = ([f'# {topic.project} 메모'] + lines) if topic.fmt == 'md' else lines
    units = [({'kind': 'line', 'line': n}, line) for n, line in enumerate(raw, 1)]
    return '\n'.join(raw).encode('utf-8') + b'\n', units


def _docx(topic: Topic, lines: list[str]):
    body = ''.join(make_ooxml.w_p(make_ooxml.w_run(line)) for line in lines)
    units = [({'kind': 'paragraph', 'index': n}, line) for n, line in enumerate(lines, 1)]
    return make_ooxml.docx_package(body), units


def _xlsx(topic: Topic, lines: list[str]):
    sheet = f'기록{topic.number:02d}'
    rows = [f'<row r="{n}">' + make_ooxml._c(f'A{n}', 'inlineStr', f'<is>{make_ooxml._t(line)}</is>') + '</row>'
            for n, line in enumerate(lines, 1)]
    data = make_ooxml.xlsx_package([(sheet, 'xl/worksheets/sheet1.xml', make_ooxml._sheet(*rows))], None)
    units = [({'kind': 'sheet_cell', 'sheet': sheet, 'cell': f'A{n}'}, line) for n, line in enumerate(lines, 1)]
    return data, units


def _pptx(topic: Topic, lines: list[str]):
    slides, units = [], []
    for start in range(0, len(lines), 3):
        number = start // 3 + 1
        group = lines[start:start + 3]
        shapes = [make_ooxml.p_sp([make_ooxml.a_p(make_ooxml.a_r(line))], f'text{k}') for k, line in enumerate(group, 1)]
        slides.append((f'rId{number + 1}', make_ooxml._slide(*shapes)))
        units += [({'kind': 'slide', 'slide': number, 'shape': k}, line) for k, line in enumerate(group, 1)]
    return make_ooxml.pptx_package(slides, [rid for rid, _ in slides]), units


EML_DATE = 'Wed, 05 Mar 2031 09:30:00 +0900'


def _eml(topic: Topic, lines: list[str]):
    sender, recipient = PEOPLE[topic.number % 3], PEOPLE[(topic.number + 1) % 3]
    subject = f'{topic.project} 진행 공유'
    msg = EmailMessage()
    msg['From'] = f'{sender} <{mail_address(sender)}>'
    msg['To'] = f'{recipient} <{mail_address(recipient, "example.org")}>'
    msg['Subject'] = subject
    msg['Date'] = EML_DATE
    msg.set_content('\n'.join(lines) + '\n', charset='utf-8')
    header = '\n'.join([f'From: {sender} <{mail_address(sender)}>',
                        f'To: {recipient} <{mail_address(recipient, "example.org")}>',
                        f'Subject: {subject}', f'Date: {EML_DATE}'])
    units = [({'kind': 'mail_header'}, header)]
    units += [({'kind': 'mail', 'part': 1, 'line': n}, line) for n, line in enumerate(lines, 1)]
    return msg.as_bytes(), units


def _pdf(topic: Topic, lines: list[str]):
    pages = [lines[start:start + 3] for start in range(0, len(lines), 3)]
    units = [({'kind': 'page', 'page': n}, '\n'.join(page)) for n, page in enumerate(pages, 1)]
    return make_pdf.korean_pdf(pages), units


def _hwpx(topic: Topic, lines: list[str]):
    data = make_hwp.hwpx_bytes([make_hwp.hwpx_section([make_hwp.hp_para(line) for line in lines])])
    units = [({'kind': 'section_para', 'section': 1, 'index': n}, line) for n, line in enumerate(lines, 1)]
    return data, units


def _hwp(topic: Topic, lines: list[str]):
    data = make_hwp.hwp_bytes([list(lines)])
    units = [({'kind': 'section_para', 'section': 1, 'index': n}, line) for n, line in enumerate(lines, 1)]
    return data, units


WRITERS = {'txt': _txt, 'md': _txt, 'docx': _docx, 'xlsx': _xlsx, 'pptx': _pptx, 'eml': _eml, 'pdf': _pdf,
           'hwpx': _hwpx, 'hwp': _hwp}


def document(topic: Topic) -> tuple[bytes, list[tuple[dict, str]]]:
    """The bytes of a topic's document and its expected ``(unit, text)`` list in ordinal order."""
    return WRITERS[topic.fmt](topic, lines_of(topic))


# ---- queries ---------------------------------------------------------------------------------------

def _gold(corpus_units: dict[str, list[tuple[dict, str]]], needle: str) -> tuple[tuple[GoldUnit, ...], tuple[str, ...]]:
    """Every unit whose text contains ``needle`` (case-insensitive), and their documents."""
    folded = needle.casefold()
    gold = tuple((path, unit) for path in sorted(corpus_units)
                 for unit, text in corpus_units[path] if folded in text.casefold())
    return gold, tuple(sorted({path for path, _ in gold}))


def _sentence(topic: Topic, kind: str) -> str | None:
    return next((text for k, text in zip(topic.kinds, topic.body_sentences) if k == kind), None)


def _excerpt(sentence: str, rng: random.Random) -> str:
    """5-12 characters of consecutive whole words (cut at word edges, never mid-word)."""
    words = sentence.rstrip('.').split(' ')
    for _ in range(50):
        first = rng.randrange(len(words))
        for last in range(first, len(words)):
            text = ' '.join(words[first:last + 1])
            if len(text) >= 5:
                if len(text) <= 12:
                    return text
                break
    return sentence[:12]


def _merge_spaces(text: str, rng: random.Random) -> str:
    """Drop one space between two words (견적서 발송 -> 견적서발송)."""
    spaces = [i for i, char in enumerate(text) if char == ' ']
    if not spaces:
        return text
    cut = rng.choice(spaces)
    return text[:cut] + text[cut + 1:]


def _make_queries(topics: list[Topic], corpus_units, rng: random.Random) -> list[Query]:
    out: list[Query] = []
    by_number = {topic.number: topic for topic in topics}

    def add(category: str, text: str, needle: str | None) -> bool:
        gold, docs = _gold(corpus_units, needle) if needle else ((), ())
        if category not in ('Q8_two_chars', 'Q9_absent') and not 1 <= len(docs) <= 3:
            return False
        if any(query.text == text for query in out):
            return False
        number = sum(query.category == category for query in out) + 1
        out.append(Query(f'Q{CATEGORIES.index(category) + 1}-{number:03d}', category, text, gold, docs))
        return True

    def cards():
        order = [topic.number for topic in topics]
        while True:
            rng.shuffle(order)
            yield from (by_number[n] for n in order)

    pick = cards()

    def fill(category: str, make) -> None:
        tries = 0
        while sum(query.category == category for query in out) < CATEGORY_COUNTS[category]:
            tries += 1
            if tries > 2000:
                raise RuntimeError(f'cannot make enough {category} queries')
            made = make(next(pick))
            if made:
                add(category, *made)

    def phrase(topic):
        kind = rng.choice([k for k in topic.kinds if k not in ('sentinel',)])
        text = _excerpt(_sentence(topic, kind), rng)
        return text, text

    def spacing(topic):
        kind = rng.choice([k for k in topic.kinds if k in ('acr', 'syn', 'code', 'visit', 'testenv', 'request', 'pilot')])
        sentence = _sentence(topic, kind)
        words = sentence.rstrip('.').split(' ')
        start = rng.randrange(max(1, len(words) - 2))
        original = ' '.join(words[start:start + 3])
        if rng.random() < 0.7:
            changed = _merge_spaces(original, rng)
        else:  # split a word of 4+ characters in two
            long_words = [w for w in original.split(' ') if len(w) >= 4]
            if not long_words:
                return None
            word = rng.choice(long_words)
            changed = original.replace(word, word[:2] + ' ' + word[2:], 1)
        return (changed, sentence) if changed != original else None

    def acronym(topic):
        other = topic.acronym_ko if topic.acronym_in_doc == topic.acronym else topic.acronym
        return f'{topic.customer} {other} 초안 검토', _sentence(topic, 'acr')

    def synonym(topic):
        first, second = topic.synonym_pair
        other = second if topic.synonym_in_doc == first else first
        return SYNONYM_QUERIES[first].format(w=other, project=topic.project, code=topic.project_code,
                                             customer=topic.customer), _sentence(topic, 'syn')

    def number(topic):
        if rng.random() < 0.55:
            spellings = [form for form in topic.amount_forms if form != topic.amount]
            if rng.random() < 0.25:
                spellings = [topic.amount]   # the same spelling: lexical can find it
            return f'계약 금액 {rng.choice(spellings)}', _sentence(topic, 'amount')
        other = topic.date_iso if topic.date_in_doc == 'ko' else topic.date_ko
        same = topic.date_ko if topic.date_in_doc == 'ko' else topic.date_iso
        return f'{rng.choice((other, other, same))} 중간 점검 회의', _sentence(topic, 'date')

    def mixed(topic):
        if 'quote' in topic.kinds and rng.random() < 0.5:
            return f'{topic.qnum} quote 거래처', _sentence(topic, 'quote')
        return f'{topic.project_code} 사양서 개정', _sentence(topic, 'code')

    def question(topic):
        kinds = [k for k in topic.kinds if k in QUESTION_TEMPLATES]
        kind = rng.choice(kinds)
        other = topic.acronym_ko if topic.acronym_in_doc == topic.acronym else topic.acronym
        text = QUESTION_TEMPLATES[kind].format(project=topic.project, code=topic.project_code,
                                               customer=topic.customer, person=topic.person, acr_other=other)
        return text, _sentence(topic, kind)

    fill('Q1_phrase', phrase)
    fill('Q2_spacing', spacing)
    fill('Q3_acronym', acronym)
    fill('Q4_synonym', synonym)
    fill('Q5_number', number)
    fill('Q6_mixed', mixed)
    fill('Q7_question', question)
    for word in TWO_CHAR_QUERIES:
        add('Q8_two_chars', word, word)
    for text in ABSENT_QUERIES:
        add('Q9_absent', text, None)
    return out


# ---- public API ------------------------------------------------------------------------------------

def build(seed: int = DEFAULT_SEED) -> Corpus:
    """The corpus for ``seed``: 60 topic cards, their units and about 120 queries. Deterministic."""
    rng = random.Random(seed)
    formats = [fmt for fmt in FORMATS for _ in range(FORMAT_COUNTS[fmt])]
    rng.shuffle(formats)
    topics = [_topic(number, fmt, rng) for number, fmt in enumerate(formats, 1)]
    topics = _with_distractors(topics, rng)
    corpus_units = {topic.rel_path: document(topic)[1] for topic in topics}
    found = _make_queries(topics, corpus_units, rng)
    return Corpus(seed=seed, topics=tuple(topics), queries=tuple(found),
                  units={path: tuple(unit for unit, _ in units) for path, units in corpus_units.items()},
                  unit_texts={path: tuple(text for _, text in units) for path, units in corpus_units.items()})


def write(corpus: Corpus, root: Path) -> list[str]:
    """Write every document below ``root`` (a temporary folder); returns the rel_paths written."""
    written = []
    for topic in corpus.topics:
        data, _ = document(topic)
        target = Path(root)/topic.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(topic.rel_path)
    return written


def queries(corpus: Corpus) -> list[Query]:
    return list(corpus.queries)


# ---- metrics (design M2 §8.3) ----------------------------------------------------------------------

def hit_rel_path(hit: dict, root: Path) -> str | None:
    """The corpus rel_path of a search hit (``doc.path`` is the absolute path the scan stored)."""
    try:
        return Path(hit['doc']['path']).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None


def score_query(query: Query, hits: list[dict], root: Path) -> dict:
    """Document hit@1/5/10, unit hit@10 (gold unit among the top 10 hits' ``units[]``) and the
    reciprocal rank of the first gold document within 10 (MRR@10 averages it)."""
    paths = [hit_rel_path(hit, root) for hit in hits[:10]]
    gold_docs = set(query.gold_docs)
    first = next((rank for rank, path in enumerate(paths, 1) if path in gold_docs), None)
    gold_units = {(path, tuple(sorted(unit.items()))) for path, unit in query.gold}
    covered = {(path, tuple(sorted(item['unit'].items())))
               for path, hit in zip(paths, hits[:10]) for item in hit['units']}
    return {'doc@1': float(first == 1), 'doc@5': float(first is not None and first <= 5),
            'doc@10': float(first is not None), 'unit@10': float(bool(gold_units & covered)),
            'rr@10': 1.0 / first if first else 0.0}


METRICS = ('doc@1', 'doc@5', 'doc@10', 'unit@10', 'rr@10')


def summarize(rows: list[tuple[str, dict]]) -> dict[str, dict]:
    """``rows``: ``(category, score_query(...))``. Mean per metric category and ``'all'`` over the
    metric categories (Q8/Q9 are left out: behaviour only). ``n`` is the query count."""
    table: dict[str, dict] = {}
    for category in (*METRIC_CATEGORIES, 'all'):
        chosen = [scores for cat, scores in rows if (cat == category or (category == 'all' and cat in METRIC_CATEGORIES))]
        if chosen:
            table[category] = {'n': len(chosen), **{m: sum(s[m] for s in chosen) / len(chosen) for m in METRICS}}
    return table


def evaluate(run, corpus: Corpus, root: Path, modes=('lexical', 'semantic', 'hybrid')) -> dict:
    """Run every query in every mode. ``run(text, mode) -> search result`` (design M2 §5.7).

    Returns ``{mode: {'table': summarize(...), 'rows': [...], 'behaviour': {...}}}``: ``rows`` hold
    ``(query id, category, scores, top-1 rel_path)``; ``behaviour`` records Q8 (lexical engine,
    hit count) and Q9 (hit count, notices) outside the metric tables."""
    out = {}
    for mode in modes:
        rows, behaviour = [], {}
        for query in corpus.queries:
            result = run(query.text, mode)
            if query.category in ('Q8_two_chars', 'Q9_absent'):
                behaviour[query.id] = {'category': query.category, 'text': query.text,
                                       'engine': result['meta']['lexical']['engine'], 'hits': len(result['hits']),
                                       'notice': list(result['notice']),
                                       'doc@10': score_query(query, result['hits'], root)['doc@10']
                                       if query.gold else None}
                continue
            scores = score_query(query, result['hits'], root)
            top = hit_rel_path(result['hits'][0], root) if result['hits'] else None
            rows.append((query.id, query.category, scores, top))
        out[mode] = {'table': summarize([(category, scores) for _, category, scores, _ in rows]), 'rows': rows,
                     'behaviour': behaviour}
    return out
