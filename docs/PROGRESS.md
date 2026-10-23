# 진행·재개 기록

### 2026-10-23 — M1 착수 (수집·파싱·원문 결속)

- 피터가 M1 /goal 등록. 설계자: `doc/planning/2026-10-23_M1-설계_v1.md` — 의존성 추가는 pypdf·olefile 둘(OOXML·HWPX는 stdlib zip+XML), 스키마 v1 직접 수정, 수집 대상은 `config.toml` 기준·UNC 기본 거부, 원본 읽기 전용을 정적·런타임 감사로 고정, 합성 문서는 테스트 시점 생성(바이너리 커밋 0), 실행자 A·B·C 분할.
- M5로 넘긴 질문: 회사 OneDrive 폴더를 수집 대상으로 허용할지, 회사 파일 서버(UNC)를 넣을지.
- 진행: 0단계(공유 인터페이스·스텁·허용 목록·스키마) 실행자 A.

### 2026-10-23 — M0 완료 (기반·이식 설계)

- 설계자: `doc/planning/2026-10-23_M0-설계_v1.md` — 패키지 `workmcp`, 데이터 홈·금지 규칙, 스키마 v1, MCP 도구 계약, `Motive_vari` 이식 목록 18항, 네트워크 경계, 위험. 검토 반영 기록 §11·§11.1·§11.2, M1–M4 이관 부록 B.
- 실행자: `pyproject.toml`(mcp 2.2.0, setuptools 84.2.1 고정), uv 관리 Python 3.12.13 venv, `uv.lock`·`requirements.lock.txt`(해시), `src/workmcp/`(cli·paths·store·locator·models·tools·server·parsers), `tests/`(경로·네트워크 경계·스키마·locator·serve 감사 래퍼). `CLAUDE.md` Commands 채움.
- 검토자: v1 조건부(중간 5: 금지 목록 우회·loopback 정의·동기화 이름·assertions 상태 보호·versions 되돌림) → 수정 → v2 조건부(새 중간 1: `INSERT OR REPLACE` 우회) → 수정 → **v3 통과(미해결 높음·중간 0)**.
- 결과: `uv lock --check` 통과, `uv run pytest -q` 307 passed, `uv run workmcp doctor` exit 0(홈 규칙 통과, FTS5 trigram 사용 가능, 모델 미설치는 M2).
- 발견: 첫 `uv sync`가 Windows 애플리케이션 제어 정책(os error 4551)에 한 번 막힘(재시도 통과) — M5 위험. 이 PC OneDrive 폴더에는 Files On-Demand 속성 표식이 없음(이름·환경변수 규칙이 주된 판정).
- 다음: 피터가 PLAN §4의 M1 `/goal`을 등록하면 M1(수집·파싱·원문 결속) 시작.

### 2026-10-22 — 프로젝트 출범, M0 착수 전

- 피터 결정(2026-10-22, Motive_vari 브레인스토밍 세션): 원래 의도(회사 업무 정보 구조화)를 개인용 MCP로 별도 프로젝트에서 진행. 새 폴더·새 비공개 저장소, `Motive_vari`에서 필요한 것만 이식. 회사 PC의 Claude Code·Codex에 MCP로 붙여 사용. 저장·임베딩은 PC 로컬, 서버의 외부 AI 호출 없음. [출범 결정](../doc/decisions/2026-10-22_프로젝트-출범-결정_v1.md)
- 분기점: `Motive_vari` 태그 `g0-freeze-20261017`(db4d9b32), `kakao-p1-complete-20261022`(50aeb182). 카톡 개발은 `Motive_vari` 개발 세션에서 계속.
- 만든 것: `AGENTS.md`, `CLAUDE.md`, `.gitignore`, `docs/PLAN.md`, 이 파일, 문서목록, 출범 결정, 새 세션 메타 프롬프트. 코드 0.
- 다음: 새 세션에서 M0(설계자 설계 → 뼈대·venv·경계 테스트 → 검토).
