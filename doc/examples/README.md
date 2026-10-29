# MCP 클라이언트 설정 예시

`workmcp serve`(stdio)를 Claude Code와 Codex에 연결하는 프로젝트 범위 설정 템플릿이다. 근거는 `doc/planning/2026-10-24_M3-설계_v1.md` §10·§11.

| 템플릿 | 복사해 둘 위치 (프로젝트 루트 기준) | 읽는 클라이언트 |
|---|---|---|
| `mcp.json.example` | `.mcp.json` | Claude Code (터미널·데스크톱 Code 탭) |
| `codex-config.toml.example` | `.codex/config.toml` | Codex (CLI·데스크톱 앱) |
| `mcp-remote.json.example` | `.mcp.json` (원격 운영 서버에 붙일 때. 위 로컬 stdio 템플릿과 **둘 중 하나**) | Claude Code — 아래 "원격 연결" 절 |
| `codex-config-remote.toml.example` | `%USERPROFILE%\.codex\config.toml`에 블록을 덧붙임(사용자 범위) | Codex — 아래 "Codex" 절 |

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

## 원격 연결 (workmcp 운영 서버, M5)

운영 서버는 VPS에서 돌고, 클라이언트는 HTTPS로 붙는다(설계 `doc/planning/2026-10-26_M5-원격구조설계_v1.md` §9.6). 이때 이 PC에는 venv·`WORKMCP_HOME`·데이터 홈이 필요 없다(데이터는 서버에 있다). 배포 자체는 `doc/guides/2026-10-26_VPS-배포절차서_v1.md`.

### 토큰

- 서버가 발급하는 **mcp 토큰**을 쓴다(`workmcp token issue --kind mcp --name ...`, 배포 절차서 C-4). **push 토큰은 MCP 경로에서 거부된다**(종류가 다르다). 토큰은 발급 때 한 번만 보이고 서버에는 해시만 있다.
- 토큰 평문을 파일·채팅·문서·git에 적지 않는다. 템플릿의 `${WORKMCP_MCP_TOKEN}`은 자리표시자이고, 값은 이 PC의 사용자 환경변수 `WORKMCP_MCP_TOKEN`에 피터가 직접 넣는다(Windows 설정의 환경 변수 화면). 환경변수 확장 `${VAR}`을 `.mcp.json`의 `headers`에서 지원하는지는 **[추정]** — 연결이 401이면 확장이 안 되는 것이므로 아래 `claude mcp add` 방식을 쓴다.
- 토큰을 바꿀 때(`workmcp token rotate`): 환경변수(또는 등록)를 새 값으로 갱신한다. 옛 토큰은 즉시 무효가 된다.

### Claude Code

1. **프로젝트 범위(이 저장소 폴더에서만):** `mcp-remote.json.example`을 `.mcp.json`으로 복사한다. 이 파일은 로컬 stdio용 `mcp.json.example`과 같은 이름 자리를 쓰므로 둘 중 하나만 둔다. `.mcp.json`은 `.gitignore`로 제외되어 커밋되지 않는다.
2. **사용자 범위(어느 폴더에서나, 설계 §9.6의 기본):** 피터가 자기 터미널에서 실행한다(에이전트는 토큰을 보지 않는다).

   ```
   claude mcp add --scope user --transport http workmcp https://<VPS_HOST>/workmcp/mcp --header "Authorization: Bearer <MCP_TOKEN>"
   claude mcp list
   ```

   **[사실]** 이 명령형은 M5 F-1(2026-10-29)에서 실제로 동작했다(`doc/validation/2026-10-29_M5-원격연결확인_v1.md`). `claude`가 PATH에 없는 PC에서는 Claude 앱에 동봉된 `claude.exe`의 전체 경로로 실행한다. 토큰이 입력한 명령줄에 들어가므로 PowerShell 기록에 남는다. 등록이 끝나면 기록을 지우되, 아래 주의를 읽는다.

   > **주의:** `Remove-Item (Get-PSReadLineOption).HistorySavePath`는 그 한 줄이 아니라 **PowerShell 명령 기록 파일 전체**를 지운다(이전에 쓴 명령도 모두 사라짐). 토큰이 남는 것이 싫으면 위 1번의 환경변수 방식(`${WORKMCP_MCP_TOKEN}`)을 쓴다.
3. 확인: `claude mcp list`에서 `workmcp`가 연결 상태인지, Code 탭에서 `status` 도구가 `role: server`를 돌려주는지 본다. 첫 의미 검색은 서버가 모델을 읽느라 수십 초 걸릴 수 있다.

### Codex

- **[사실]** `codex mcp add workmcp --url https://<VPS_HOST>/workmcp/mcp --bearer-token-env-var WORKMCP_MCP_TOKEN`. 옵션 `--url`, `--bearer-token-env-var`(streamable HTTP 서버에만 유효)는 `codex-cli 0.160.0`의 `codex mcp add --help`와 공식 문서로 확인했다(접근 2026-10-29: https://learn.chatgpt.com/docs/extend/mcp?surface=cli , https://learn.chatgpt.com/docs/config-file/config-reference ). 토큰은 명령줄에도 파일에도 없고 사용자 환경변수 `WORKMCP_MCP_TOKEN`에만 있다.
- `codex-config-remote.toml.example`의 `[mcp_servers.workmcp]` 블록을 `%USERPROFILE%\.codex\config.toml`에 넣는다. 명령으로 만든 블록에는 `startup_timeout_sec = 30`, `tool_timeout_sec = 120` 두 줄을 손으로 더한다(기본 10초·60초는 첫 의미 검색에 빠듯하다).
- 프로젝트 범위 `.codex/config.toml`은 신뢰한 프로젝트에서만 읽히므로 사용자 범위를 기본으로 한다. 환경변수를 새로 만들거나 바꾼 뒤에는 Codex를 다시 시작한다.

### 로컬 stdio와 헷갈리지 않기

| | 로컬 stdio (위쪽 절) | 원격 (이 절) |
|---|---|---|
| 서버가 도는 곳 | 클라이언트가 이 PC에서 띄움 | VPS (systemd 서비스) |
| 데이터 | 이 PC의 데이터 홈 | VPS (수집기가 `push`로 올린 텍스트·벡터) |
| 인증 | 없음(프로세스 직접 실행) | mcp 토큰(Bearer) |
| 설정에 필요한 것 | venv 경로·`WORKMCP_HOME` | 서버 URL·토큰 |

두 방식을 같은 이름 `workmcp`로 동시에 등록하지 않는다(어느 쪽이 답하는지 흐려진다). 원격으로 옮길 때는 로컬 등록을 지운다.
