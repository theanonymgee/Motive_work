# workmcp (Motive_work)

A personal work-knowledge MCP server: a collector parses and embeds your own documents locally, and an MCP server answers Claude Code, Codex and other MCP clients with snippets that carry exact source locators. Single user, no external AI/API calls. Documentation is mainly in Korean.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

내 업무 문서를 근거가 붙은 검색 가능한 지식으로 만들어, 이미 쓰는 AI 도구(Claude Code, Codex 등)에 **MCP** 서버로 붙여 주는 개인용 도구입니다.

- 돌려주는 모든 조각과 사실에 **정확한 출처**(파일, 버전 해시, 쪽·시트·줄·글자 범위)가 붙습니다.
- 임베딩은 **CPU에서 로컬로**(BGE-M3 ONNX) 돌고, 수집기와 서버 어디에서도 외부 AI/API를 부르지 않습니다.
- 원본 파일은 읽기만 하고 고치지 않습니다.
- 한 사람이 쓰는 도구입니다. 여러 사용자, 판매용 서비스, 웹 화면은 없습니다.

## 구조

```
[문서가 있는 PC]                          [운영 서버 (내 VPS)]                  [MCP 클라이언트]
 원본 문서 ── workmcp scan / index        workmcp serve --http                  Claude Code, Codex …
             (파싱·청크·임베딩)  ── push ──▶ (청크·벡터·지식 저장,     ◀── HTTPS + 토큰 ──
             데이터 홈(SQLite)    HTTPS+토큰   질의 임베딩, MCP 제공)
```

- **수집기**: 문서 폴더를 증분 수집하고 파싱·청크·임베딩한 뒤 바뀐 것만 운영 서버로 올립니다. 원본 파일은 그 PC를 떠나지 않습니다. 수집기의 네트워크는 운영 서버와 명시적인 모델 다운로드뿐입니다.
- **운영 서버**: 같은 모델로 질의를 임베딩하고 MCP(Streamable HTTP)를 TLS와 토큰으로 제공합니다. 토큰은 SHA-256 해시로만 저장합니다.
- **로컬만 쓰기**: 서버 없이 한 PC에서 `workmcp serve`(stdio)로 바로 붙여 쓸 수도 있습니다.

지원 형식: TXT, Markdown, CSV, PDF, DOCX, PPTX, XLSX, EML/MSG, HWP/HWPX.

## MCP 도구 (15개)

| 구분 | 도구 |
|---|---|
| 읽기 | `search`(어휘+의미 혼합 검색), `get_snippet`, `list_documents`, `recent_changes`, `status` |
| 구조화 지식 | `record_entity`, `record_fact`, `record_decision`, `record_task`, `supersede`, `retract` |
| 지식 조회 | `find_entity`, `get_entity`, `list_knowledge`, `get_history` |

쓰기 도구는 근거 출처(문서 조각의 인용) 또는 "내 메모" 표시를 반드시 요구합니다. 사실·결정·할 일은 유효 시점과 기록 시점 두 축으로 이력이 남고, 철회·대체돼도 지워지지 않습니다. 같은 대상의 어긋난 사실은 충돌로 표시됩니다.

응답마다 `delivered_sources`에 이번 응답으로 나간 조각의 출처가 모두 적힙니다.

## 요구 사항

- Windows(수집기, 먼저 지원) 또는 Linux(운영 서버, Ubuntu 24.04에서 확인)
- Python 3.12
- 관리자 권한은 필요 없습니다(사용자 수준 venv).
- BGE-M3 ONNX 모델 파일(약 2.3 GB). 명시적인 다운로드 명령이나 오프라인 묶음으로만 받습니다.

## 설치

[uv](https://docs.astral.sh/uv/)를 쓰는 경우:

```bash
uv venv --python 3.12
uv sync --frozen
uv run workmcp doctor
```

uv 없이 pip만 쓰는 경우(해시 고정 설치):

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock.txt
.venv\Scripts\python.exe -m pip install --no-deps -e .
```

데이터 홈은 `WORKMCP_HOME`(기본 `%LOCALAPPDATA%\Motive_work`)입니다. 저장소 안, 동기화 폴더, UNC 경로, 상대 경로는 거부됩니다. Claude·Codex 데스크톱 앱과 함께 쓸 때는 AppData 밖에 두세요(이유: [doc/examples/README.md](doc/examples/README.md)).

## 사용

| 명령 | 하는 일 |
|---|---|
| `workmcp setup` | 데이터 홈, `config.toml`, 모델, 클라이언트 등록 준비 |
| `workmcp doctor` | 데이터 홈 규칙, Python, SQLite/FTS5, mcp, 모델 파일, 수집 폴더 점검 |
| `workmcp fetch-model` / `verify-model` | 모델 파일을 복사·검증(SHA-256 매니페스트) |
| `workmcp scan [--root PATH]` | 수집 폴더 증분 수집 |
| `workmcp index` | 수집된 버전을 청크·임베딩 |
| `workmcp search "질의"` | 터미널에서 출처가 붙은 혼합 검색 |
| `workmcp verify` | 저장된 단위가 현재 파일에서 다시 나오는지 결속 검사 |
| `workmcp sources` | 수집 폴더와 상태 |
| `workmcp serve` | stdio MCP 서버 (`--http`면 운영 서버) |
| `workmcp push` | 수집기의 변경분을 운영 서버로 전송 |
| `workmcp token` / `collectors` | 운영 서버: 토큰 발급·폐기·교체, 수집기 목록·은퇴 |

각 명령의 옵션은 `workmcp <명령> --help`로 봅니다.

MCP 클라이언트 설정 템플릿(로컬 stdio, 원격 HTTP)은 [doc/examples/](doc/examples/)에 있습니다. 운영 서버 배포 절차는 [doc/guides/](doc/guides/)에 있습니다. 문서의 `<VPS_HOST>`, `<VPS_IP>`, `<MCP_TOKEN>` 같은 자리표시자는 자기 값으로 바꿔 쓰세요.

## 테스트

```bash
uv run pytest -q
```

테스트는 합성 문서만 씁니다. 네트워크 경계(외부 연결 없음), 데이터 홈 규칙, 저장소에 비밀값이 없는지도 테스트로 고정돼 있습니다.

## 데이터와 보안 원칙

- 실제 업무 자료와 비밀값(토큰, 서버 주소, 계정)은 저장소에 넣지 않습니다. 개발과 테스트에는 합성·공개 문서만 씁니다.
- 원본 파일은 수집기 PC에만 있습니다. 추출 텍스트·벡터·지식은 수집기 데이터 홈과 내 운영 서버 두 곳에만 저장됩니다.
- MCP 클라이언트가 요청한 조각만 그 클라이언트로 나갑니다. 그 뒤 클라이언트 제공사(Anthropic, OpenAI 등)로 가는 것은 사용자의 선택입니다.
- 회사 자료에 쓸 때 회사의 AI 사용 규정을 지키는 것은 사용자의 책임입니다.

## 상태

개인 프로젝트이며 계속 바뀝니다. 계획과 진행은 [docs/PLAN.md](docs/PLAN.md)와 [docs/PROGRESS.md](docs/PROGRESS.md)에 있습니다. 이슈와 제안은 환영하지만 답이 늦을 수 있습니다.

## 라이선스

[MIT](LICENSE)
