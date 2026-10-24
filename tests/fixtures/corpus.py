"""Extended synthetic corpus for search evaluation (design M2 §8). Stage 0: data classes only.

The M1 bundle repeats ``LINES_KO``/``LINES_EN`` in every document, so it cannot tell which
document answers a query; it stays the functional fixture (binding, increments). This corpus has
one topic card per document (60), built with the low-level writers of the existing generators,
and about 120 queries with gold units. The rules of ``fixtures/__init__.py`` (1-4) hold: no real
person, company, address or domain, ``SENTINEL`` in every document, reserved mail domains only.

Stage 1 (C) adds ``build(seed=7) -> Corpus``, ``write(corpus, root)`` and ``queries(corpus)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Query categories (design M2 §8.3); Q8/Q9 are recorded as behaviour, outside the metric tables.
CATEGORIES = ('Q1_phrase', 'Q2_spacing', 'Q3_acronym', 'Q4_synonym', 'Q5_number', 'Q6_mixed', 'Q7_question',
              'Q8_two_chars', 'Q9_absent')
FORMATS = ('txt', 'md', 'docx', 'xlsx', 'pptx', 'eml', 'pdf', 'hwpx', 'hwp')

# (rel_path, unit dict): a gold answer is a unit of one document, as its locator names it.
GoldUnit = tuple[str, dict]


@dataclass(frozen=True)
class Topic:
    """One topic card = one document (design M2 §8.2). Every value is synthetic."""
    number: int                          # 1..60
    fmt: str                             # one of FORMATS
    rel_path: str                        # where ``write`` puts the document under the corpus root
    project: str                         # '합성 프로젝트 N호'
    project_code: str                    # 'PRJ-0NN'
    customer: str                        # '가상 거래처 A'..'L'
    person: str                          # '담당자 갑'..'사'
    date_ko: str                         # '2031년 3월 4일'
    date_iso: str                        # '2031-03-04'
    amount: str                          # the one spelling used in the document ('1,234,000원', ...)
    acronym: str                         # 'MOU', 'NDA', ... (the document has this or its expansion)
    acronym_ko: str                      # '업무협약', ...
    acronym_in_doc: str                  # which of the two the document contains
    synonym_pair: tuple[str, str]        # ('일정', '스케줄'), ...
    synonym_in_doc: str                  # which side the document contains
    sentinel: str                        # '주제표지-NNN', only in this document
    body_sentences: tuple[str, ...]      # 6-12 topic sentences (80 % Korean, 20 % English)
    distractors: tuple[str, ...] = ()    # sentences of other cards mixed in as "참고"


@dataclass(frozen=True)
class Query:
    """One evaluation query (design M2 §8.3)."""
    id: str                              # 'Q1-001'
    category: str                        # one of CATEGORIES
    text: str
    gold: tuple[GoldUnit, ...] = ()      # empty for Q9
    gold_docs: tuple[str, ...] = ()      # rel_paths of the 1-3 answering documents


@dataclass(frozen=True)
class Corpus:
    seed: int
    topics: tuple[Topic, ...]
    queries: tuple[Query, ...] = ()
    units: dict[str, tuple[dict, ...]] = field(default_factory=dict)   # rel_path -> every unit dict written
