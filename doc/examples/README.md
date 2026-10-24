# MCP 클라이언트 설정 예시

`workmcp serve`(stdio)를 Claude Code와 Codex에 연결하는 프로젝트 범위 설정 템플릿이다. 근거는 `doc/planning/2026-10-24_M3-설계_v1.md` §10·§11.

| 템플릿 | 복사해 둘 위치 (프로젝트 루트 기준) | 읽는 클라이언트 |
|---|---|---|
| `mcp.json.example` | `.mcp.json` | Claude Code (터미널·데스크톱 Code 탭) |
| `codex-config.toml.example` | `.codex/config.toml` | Codex (CLI·데스크톱 앱) |

## 사용법

1. 템플릿을 위 위치로 복사한다(파일 이름의 `.example`은 뗀다). `.codex` 폴더가 없으면 만든다.
2. 자리표시자를 바꾼다.
   - `<USER>`: 이 PC의 Windows 사용자 이름.
   - `command`: 이 저장소의 `.venv\Scripts\python.exe` 절대 경로. 저장소를 다른 곳에 두었다면 `C:\Users\<USER>\Project\Motive_work` 부분도 맞춘다.
   - `WORKMCP_HOME`: 데이터 홈 절대 경로. 평상시 `C:\Users\<USER>\Motive_work`, 연결 시험 중에는 합성 자료만 든 시험 홈(예: `C:\Users\<USER>\Motive_work_m3test`).
3. `args`의 `-I`(파이썬 격리 모드)는 지우지 않는다: Claude 앱 안에서 뜬 셸·클라이언트는 다른 도구의 `PYTHONPATH`(다른 Python 버전의 패키지)를 물려받을 수 있고, 그대로면 serve가 `pydantic_core` import 오류로 시작하지 못한다(이 PC에서 확인, 검토 v1 M3-4). `-I`는 `PYTHONPATH`·사용자 site·작업 폴더를 무시하고 venv의 설치는 그대로 쓴다.
4. 경로 표기: JSON은 역슬래시를 `\`로 두 번 쓴다. TOML은 작은따옴표 문자열(`'...'`)이라 역슬래시를 한 번만 쓴다.
5. 확인은 `doc/validation/2026-10-24_M3-연결확인_v1.md`의 절차를 따른다. 터미널에서는 `uv run workmcp doctor`로 데이터 홈과 가상화 여부를 먼저 본다.

## `WORKMCP_HOME`을 AppData 밖에 두는 이유

Claude·Codex 데스크톱 앱은 MSIX 패키지 앱이라, 앱 안에서 도는 프로세스가 `%LOCALAPPDATA%`(AppData) 아래에 새로 만드는 파일은 `%LOCALAPPDATA%\Packages\<앱>\LocalCache\Local\...`로 돌려진다. 그러면:

- 앱 안 서버와 앱 밖 CLI(`workmcp scan`)가 **서로 다른 DB**를 보고, Claude 앱과 Codex 앱도 **각자 다른 DB**를 본다.
- 실제 AppData에 DB가 이미 있으면 SQLite의 `-wal`·`-shm`만 패키지 쪽에 생겨 WAL이 갈라질 수 있다(DB 손상 위험).
- 앱을 지우거나 초기화하면 패키지 쪽 데이터 홈(DB·모델)이 함께 사라진다.

그래서 두 클라이언트 설정의 `env`에 같은 `WORKMCP_HOME`을 AppData 밖(예: 사용자 프로필 바로 아래 `Motive_work`)으로 적는다. 홈 규칙(저장소 밖·동기화 폴더 아님·UNC 아님)은 `workmcp doctor`가 점검한다.

## 커밋하지 않는다

복사한 `.mcp.json`과 `.codex/config.toml`에는 사용자 이름·venv 경로·데이터 홈 같은 PC별 값이 들어가므로 `.gitignore`(`/.mcp.json`, `/.codex/`)로 제외한다. 저장소에 올라가는 것은 자리표시자만 든 `*.example`뿐이다. 비밀값은 어느 파일에도 없다(서버는 비밀을 쓰지 않는다).

## 피터가 직접 눌러야 하는 승인 2개

전역 설정(`~/.claude.json`, `~/.codex/config.toml`)은 편집하지 않는다. 대신 연결 확인 중 클라이언트가 묻는 다음 두 가지를 피터가 승인한다.

1. **Claude Code(Code 탭)**: 이 저장소 폴더로 세션을 열 때 뜨는 프로젝트 서버 `workmcp` 사용 승인 프롬프트.
2. **Codex 데스크톱 앱**: 이 저장소 폴더를 프로젝트로 열 때의 신뢰 승인(앱이 `~/.codex/config.toml`에 `projects` 항목을 스스로 기록한다).

Codex가 프로젝트 `.codex/config.toml`의 `mcp_servers`를 읽지 않으면 멈추고 피터에게 전역 설정 추가를 묻는다.
