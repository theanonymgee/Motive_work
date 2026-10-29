# workmcp (Motive_work)

A personal work-knowledge MCP server: a collector parses and embeds your own documents locally and pushes the changes to your own server, which answers Claude Code, Codex and other MCP clients with snippets that carry exact source locators. Single user, no external AI/API calls. Documentation is mainly in Korean.

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

- **수집기**: 문서 폴더를 증분 수집하고 파싱·청크·임베딩한 뒤 바뀐 것만 운영 서버로 올립니다(`push`). 수집기가 쓰는 네트워크는 운영 서버(HTTPS)와, 명시적으로 실행하는 모델 다운로드뿐입니다.
- **운영 서버**: 같은 모델로 질의를 임베딩하고 MCP(Streamable HTTP)를 TLS와 토큰으로 제공합니다. 토큰은 SHA-256 해시로만 저장합니다. 토큰 없는 요청은 받지 않습니다.
- **로컬만 쓰기**: 서버 없이 한 PC에서 `workmcp serve`(stdio)로 바로 붙여 쓸 수도 있습니다.

지원 형식: TXT, Markdown, CSV, PDF, DOCX, PPTX, XLSX, EML/MSG, HWP/HWPX.

## 데이터와 보안 원칙

- 실제 업무 자료와 비밀값(토큰, 서버 주소, 계정)은 저장소에 넣지 않습니다. 개발과 테스트에는 합성·공개 문서만 씁니다. 문서의 `<VPS_HOST>`, `<MCP_TOKEN>` 같은 자리표시자는 자기 값으로 바꿔 쓰세요.
- 원본 파일은 수집기 PC에만 있습니다. 추출 텍스트·벡터·지식은 수집기 데이터 홈과 내 운영 서버 두 곳에만 저장됩니다.
- MCP 클라이언트가 요청한 조각만 그 클라이언트로 나갑니다. 그 뒤 클라이언트 제공사(Anthropic, OpenAI 등)로 가는 것은 사용자의 선택입니다.
- 회사 자료에 쓸 때 회사의 AI 사용 규정을 지키는 것은 사용자의 책임입니다.

## MCP 도구 (15개)

| 구분 | 도구 |
|---|---|
| 읽기 | `search`(어휘+의미 혼합 검색), `get_snippet`, `list_documents`, `recent_changes`, `status` |
| 구조화 지식 | `record_entity`, `record_fact`, `record_decision`, `record_task`, `supersede`, `retract` |
| 지식 조회 | `find_entity`, `get_entity`, `list_knowledge`, `get_history` |

응답마다 `delivered_sources`에 이번 응답으로 나간 조각의 출처가 모두 적힙니다.

쓰기 도구는 근거 출처(문서 조각의 인용) 또는 "내 메모" 표시를 반드시 요구합니다. 사실·결정·할 일은 유효 시점과 기록 시점 두 축으로 이력이 남고, 철회·대체돼도 지워지지 않습니다. 같은 대상의 어긋난 사실은 충돌로 표시됩니다.

## 요구 사항

- 수집기: Windows 10/11 x64, Python 3.12(python.org 사용자 단위 설치기로 충분), 디스크 약 3 GB(모델 약 2.3 GB), 색인 중 메모리 약 2 GiB. **관리자 권한은 필요 없습니다.**
- 운영 서버: Ubuntu 24.04 VPS 한 대(배포 절차는 [doc/guides/](doc/guides/)).
- BGE-M3 ONNX 모델 파일은 명시적인 다운로드 명령이나 오프라인 묶음으로만 받습니다.

## 수집기 설치

아래는 PowerShell 기준입니다. 상세한 단계와 막혔을 때의 대처는 [회사 PC 설치 안내서](doc/guides/2026-10-29_회사PC-설치안내서_v1.md)에 있습니다. 데이터 홈은 `WORKMCP_HOME`(기본 `%USERPROFILE%\Motive_work`)이며, 저장소 안, 동기화 폴더(OneDrive 등), UNC 경로, 상대 경로, AppData 아래는 거부됩니다(Claude·Codex 데스크톱 앱과 함께 쓸 때 AppData 밖에 두는 이유: [doc/examples/README.md](doc/examples/README.md)).

### 온라인 (주 경로)

```powershell
git clone <저장소 URL> workmcp ; cd workmcp
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock.txt
.venv\Scripts\python.exe -I -m pip install --no-deps .
.venv\Scripts\python.exe -I -m workmcp setup --home "$env:USERPROFILE\Motive_work" --model download --push-url https://<VPS_HOST>/workmcp
```

### 오프라인 묶음 (예비 경로)

회사 네트워크가 저장소·PyPI·모델 다운로드를 막을 때 씁니다. 묶음은 다른 PC에서 `tools/make_bundle.py`로 만듭니다(휠, `requirements.lock.txt` 사본, `SHA256SUMS`, 검증 스크립트, 모델은 별도 폴더). 묶음 폴더 안의 `INSTALL.txt`가 설치 명령 전부입니다. 요지는 다음과 같습니다.

```powershell
certutil -hashfile SHA256SUMS SHA256              # 묶음을 만든 PC에서 make가 출력한 값(묶음과 따로 적어 온 값)과 같아야 함
py -3.12 -I verify_bundle.py .                    # ok N mismatch 0 missing 0 unlisted 0 이어야 함
py -3.12 -m venv "$env:USERPROFILE\workmcp\.venv"
$py = "$env:USERPROFILE\workmcp\.venv\Scripts\python.exe"
& $py -I -m pip install --no-index --find-links wheels --require-hashes -r requirements.lock.txt
& $py -I -m pip install --no-index --find-links wheels --force-reinstall --no-deps workmcp==<버전>
& $py -I -m workmcp setup --home "$env:USERPROFILE\Motive_work" --model-from <모델 묶음 폴더> --push-url https://<VPS_HOST>/workmcp
```

해시 검사(`--require-hashes`, `verify_bundle.py`)는 끄지 않습니다. `<버전>`은 묶음 폴더 이름(`workmcp-bundle-<버전>-...`)의 버전입니다. `--force-reinstall --no-deps`는 workmcp 휠만 다시 설치해, 업데이트 때 같은 버전 번호가 이미 설치돼 있어도 코드가 확실히 바뀌게 합니다.

### setup

`setup`은 묻지 않고 인자로만 동작하며, 토큰을 읽거나 만들거나 출력하지 않고, 클라이언트 등록은 명령줄만 출력합니다(사용자가 직접 실행).

```
workmcp setup --home PATH
     (--model download | --model none | --model-from DIR)       # --upgrade 때는 생략 가능
     [--source PATH ...] [--push-url https://<VPS_HOST>/workmcp]
     [--clients claude,codex | --clients codex | --clients none] [--scope user|project] [--project DIR]
     [--scan] [--index] [--upgrade] [--yes] [--dry-run]
```

`--dry-run`으로 무엇을 할지 먼저 봅니다. `--push-url`은 https만 받고, MCP 주소는 `<push-url>/mcp`입니다. push 토큰은 서버에서 한 번 발급받아 `<데이터 홈>\push.token`에 한 줄로 적거나 환경변수 `WORKMCP_PUSH_TOKEN`에 둡니다.

`--home`이 기본(`%USERPROFILE%\Motive_work`)이 아니면 이후의 `scan`·`index`·`push`와 작업 스케줄러는 그 홈을 사용자 환경변수 `WORKMCP_HOME`으로만 찾습니다. `setup`이 그 경우 아래 줄을 출력하니 한 번 실행하고 새 PowerShell 창을 엽니다(관리자 권한 불필요; 또는 "계정의 환경 변수 편집"). `doctor`의 `data home:` 줄 끝에 홈의 출처(`from WORKMCP_HOME` / `default, WORKMCP_HOME not set`)가 나옵니다.

```powershell
[Environment]::SetEnvironmentVariable('WORKMCP_HOME', 'D:\Motive_work', 'User')
```

## 사용 (수집기)

```powershell
$wm = ".venv\Scripts\python.exe"
& $wm -I -m workmcp doctor                 # 환경 점검
& $wm -I -m workmcp scan                   # config.toml의 [[sources]] 폴더를 증분 수집
& $wm -I -m workmcp index                  # 청크·임베딩
& $wm -I -m workmcp verify                 # 저장된 단위가 현재 파일에서 다시 나오는지 확인
& $wm -I -m workmcp push --dry-run         # 서버와 비교만 (무엇이 나갈지 확인)
& $wm -I -m workmcp push                   # 변경분 전송
```

| 명령 | 하는 일 | 종료 코드 |
|---|---|---|
| `setup` | 데이터 홈, `config.toml`, 모델, 클라이언트 등록 명령, 업그레이드 | 0 완료, 1 단계 실패, 2 홈 거부·스키마 체크섬 변경, 3 잠금, 4 인자·소스·설정 |
| `doctor` | 데이터 홈 규칙, Python, SQLite/FTS5, mcp, 모델 파일, 수집 폴더, 역할 점검 | 0, 2 |
| `scan [--root PATH] [--retry-failed] [--reparse] [--rehash]` | 수집 폴더 증분 수집 | 0, 2, 3 스캔 잠금, 4 |
| `index [--limit N] [--gc]` | 수집된 버전을 청크·임베딩 | 0, 2, 3, 4 |
| `push [--dry-run] [--max-mb N] [--limit-docs N]` | 변경분을 운영 서버로 전송 | 0, 1, 2, 3, 4 스키마 불일치 포함, 6 서버 연결 불가, 7 TLS 인증서 검증 실패 |
| `verify [--root PATH] [--doc ID]` | 저장된 단위와 현재 파일의 결속 검사 | 0, 1 불일치, 2, 4 |
| `search "질의"` | 터미널에서 출처가 붙은 혼합 검색 | 0, 2, 4 |
| `serve` | 한 PC에서 쓰는 stdio MCP 서버 (`--http`면 운영 서버) | 0, 2, 3, 4 |
| `sources` | 수집 폴더와 상태 | 0, 2 |
| `fetch-model` / `verify-model` | 모델 파일 복사·검증(SHA-256 매니페스트) | 0, 1 |

운영 서버 쪽 명령: `serve --http 127.0.0.1:8765`(loopback만; nginx/Caddy 뒤), `token issue|list|revoke|rotate`, `collectors [retire NAME]`, `verify --stored`. 각 명령의 옵션은 `workmcp <명령> --help`로 봅니다.

## 클라이언트 연결

운영 서버의 **mcp 토큰**을 씁니다(push 토큰은 MCP 경로에서 거부됩니다). 먼저 토큰을 사용자 환경변수 `WORKMCP_MCP_TOKEN`에 넣고("계정의 환경 변수 편집", 관리자 권한 불필요) 새 PowerShell 창을 엽니다. `setup`이 아래와 같은 명령을 출력하면 사용자가 자기 터미널에서 실행합니다.

```powershell
# Claude Code: PowerShell이 실행 때 $env:WORKMCP_MCP_TOKEN 을 펼치므로 셸 기록에는 변수 이름만 남습니다
claude mcp add --transport http --scope user workmcp https://<VPS_HOST>/workmcp/mcp --header "Authorization: Bearer $env:WORKMCP_MCP_TOKEN"

# Codex: 토큰은 환경변수에만 있고 Codex가 실행 때 읽습니다
codex mcp add workmcp --url https://<VPS_HOST>/workmcp/mcp --bearer-token-env-var WORKMCP_MCP_TOKEN
# 그리고 %USERPROFILE%\.codex\config.toml 의 [mcp_servers.workmcp] 에 startup_timeout_sec = 30, tool_timeout_sec = 120 추가
```

Claude Code는 등록 때 받은 헤더 값(토큰 평문)을 `%USERPROFILE%\.claude.json`에 저장합니다. Claude Code의 동작이므로, 그 파일을 공유·동기화하지 마세요.

설정 템플릿(로컬 stdio, 원격 HTTP, Codex)은 [doc/examples/](doc/examples/)에 있습니다. 연결한 뒤 각 클라이언트에서 `status` 도구를 불러 `role: server`를 확인합니다.

## 업데이트

**서버 먼저, 수집기 나중.** 프로토콜·스키마·청크 버전이 다르면 `push`가 안전하게 거절됩니다(종료 코드 4, "update the server first").

1. 서버: 새 코드 반입 → `pip install --no-deps` → `systemctl stop workmcp` → `workmcp setup --home <홈> --upgrade`(백업·마이그레이션·doctor) → `systemctl start workmcp`. 절차는 [VPS 배포 절차서](doc/guides/2026-10-26_VPS-배포절차서_v1.md).
2. 수집기: `scan`·`index`·`push`가 돌고 있지 않은지 확인 → `git pull` 또는 새 묶음(`certutil` 대조 → `verify_bundle.py`) → 의존성이 바뀌었으면 `pip install --require-hashes -r requirements.lock.txt` → 온라인 `pip install --no-deps .` / 오프라인 `pip install --no-index --find-links wheels --force-reinstall --no-deps workmcp==<버전>` → `pip show workmcp`로 버전 확인 → `workmcp setup --home <홈> --upgrade` → `push --dry-run`.
   릴리스마다 `pyproject.toml`의 `version`과 `workmcp.__version__`을 함께 올립니다(테스트가 같은 값인지 확인).
3. 서버를 재시작했으면 MCP 클라이언트 세션을 새로 엽니다(도구 목록은 세션 시작 때 정해집니다).

## 테스트

```powershell
.venv\Scripts\python.exe -I -m pytest -q
```

`requirements.lock.txt`에 `pytest`가 들어 있어 설치한 PC에서 같은 테스트를 진단으로 돌릴 수 있습니다. 테스트는 합성 문서만 쓰며, 네트워크 경계(수집기는 운영 서버와 모델 다운로드 모듈 외 연결 없음), 데이터 홈 규칙, 저장소에 비밀값·개인 경로가 없는지도 테스트로 고정돼 있습니다. 개발 환경(uv)은 [CLAUDE.md](CLAUDE.md)를 보세요.

## 문서

- [AGENTS.md](AGENTS.md): 프로젝트 맥락과 경계 원칙(Codex와 Claude Code 공통)
- [docs/PLAN.md](docs/PLAN.md), [docs/PROGRESS.md](docs/PROGRESS.md): 계획과 진행
- `doc/`: 설계, 검토, 안내서(`doc/guides/`), 클라이언트 설정 예시(`doc/examples/`)

## AI 에이전트에게 설치를 맡길 때

에이전트에게 이 README와 [설치 안내서](doc/guides/2026-10-29_회사PC-설치안내서_v1.md)를 읽히고 아래 지점에서 반드시 멈추게 하세요.

- 데이터 홈 위치와 `[[sources]]` 폴더를 정하기 전.
- 토큰을 만들거나 파일·환경변수에 적기 전(에이전트는 토큰 값을 읽지도 쓰지도 않습니다).
- `claude mcp add`, `codex mcp add`, 환경변수 설정, 전역 설정 파일 수정 같은 등록 명령을 실행하기 전(명령은 출력만 하고 사용자가 실행).
- 첫 `push`(그 전 `push --dry-run`의 문서 수를 사용자가 확인).
- 해시 불일치, 보안 소프트웨어의 차단, 프록시 필요 같은 오류를 만났을 때(우회하지 말고 멈춰서 알림).

## 상태

개인 프로젝트이며 계속 바뀝니다. 계획과 진행은 [docs/PLAN.md](docs/PLAN.md)와 [docs/PROGRESS.md](docs/PROGRESS.md)에 있습니다. 이슈와 제안은 환영하지만 답이 늦을 수 있습니다.

## 라이선스

[MIT](LICENSE)
