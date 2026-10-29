# 진행·재개 기록

### 2026-10-29 — M6 완료 (회사 PC 수집기 설치형)

- 설계 `doc/planning/2026-10-29_M6-설계_v1.md`(설계자), 실행자 A~K, 검토 v1 조건부 → v2 통과 → v3 통과(높음·중간 0).
- 코드: 검토 v3 낮음(V3-2·3·5·6·7) 수정, `workmcp setup` 구현, `tools/make_bundle.py`(오프라인 묶음, 빌드 백엔드 해시 고정 `requirements-build.lock.txt`), `tools/scan_history.py`(공개 전 이력 검사), 서버 `serve --http`가 리스닝 먼저·모델 백그라운드 로드·정지 3 s 제한, 버전 0.1.0.
- 확인: 새 빈 폴더 오프라인 설치→setup→scan→index→push 성공, 두 번째 push 0(`doc/validation/2026-10-29_M6-오프라인설치확인_v1.md`). 서버 업데이트 2회(피터 승인), Codex·Claude Code 동시 검색, 합성 수집기 2개 retire·시험 사실 철회(`doc/validation/2026-10-29_M6-운영확인_v1.md`). 전체 테스트 2280 passed.
- 공개 준비: MIT, README 재작성(사용자용), 호스트·사용자 이름 치환, 이력 검사(토큰·비밀·회사 자료 0). 저장소 공개 전환 완료(2026-10-29 피터 승인, GitHub `public`, 라이선스 MIT 인식).
- 다음(피터): 회사 PC 실제 설치(안내서; 그 PC용 push 토큰을 서버에서 먼저 발급), 프록시 확인, SSH 키 정리. 남은 낮음은 PLAN §5 M6 "남은 것".

### 2026-10-29 — M5 완료 (원격 운영 서버 전환)

- VPS 배포(피터 단계별 승인): C — 최신 코드 재배포, VPS 테스트 2108 passed·22 skipped, BGE-M3 SHA-256 4/4, `config.toml`, 토큰 2개(평문은 이 PC 저장소 밖 파일로만, 첫 시도 경로 오류로 버려진 2개는 폐기) → D — systemd 유닛(loopback, `IPAddressDeny` 실효 확인), certbot(webroot, 이메일 없이 약관 동의 — 피터), workshop 80 설정에 ACME include 1줄, 443 새 서버 블록 → E — 백업 타이머 1회 ok → F — 합성 수집기 51문서 push(두 번째 전송 0, 추가 1 증분), `workmcp-remote` 사용자 범위 등록.
- 원격 호출: 피터가 시작한 새 Code 탭 세션에서 status·search·get_snippet·record_entity·record_fact·get_entity 성공, 서버 `tool_calls`·`deliveries`와 대조 — `doc/validation/2026-10-29_M5-원격연결확인_v1.md`.
- 보안 점검 17/17 통과 — `doc/validation/2026-10-27_M5-보안점검결과_v1.md`. 15번: 첫 의미 질의 83 s(모델 첫 로드) → `model_preload = true`로 167 ms. 17번(회사 규정) 피터 "확인함".
- 결과: 이 PC 2130 passed, 도구·원격 테스트 156 passed, 검토 v3 통과.
- 남은 것(M6): 검토 v3 낮음 7(특히 V3-3 거부 문서가 sweep에 쓸림, V3-5 push.lock 교체 경쟁 — 회사 자료 전), `workmcp setup` 구현, 저장소 공개 전환, 회사 PC 설치·Codex, 서버의 합성 수집기 retire와 합성 지식 행 정리, 배포용 SSH 키 정리(피터), 443 기본 서버 블록(낮음).

### 2026-10-27 — M5 진행 (원격 운영 서버 전환) — 단계 C 승인 대기

- 구조 변경(피터 2026-10-26): 회사 PC 수집기가 파싱·청크·임베딩(BGE-M3) 후 변경분을 Hostinger KVM 2의 운영 서버로 push, 서버가 같은 모델로 질의를 임베딩하고 MCP를 HTTPS+토큰으로 제공. 설계 `M5-원격구조설계_v1`(반영 기록 1–32번이 본문보다 우선).
- 구현: 1단계 `8f3e5fe`(Streamable HTTP 서버·토큰·push·receive·배포 조각), 2단계 `7526dee`(서버/push/클라이언트 e2e, 감사 두 모드 동시, 측정 — `M5-원격e2e_v1`).
- 검토: v1 조건부(중간 2: 수집기 홈 재생성 미수렴, 큰 버전 조립 메모리 +236 MiB) → `562faac` → v2 조건부(중간 2: 재생성 전 삭제 파일이 active로 남음, 긴 단위 하나의 메모리·정지) → `199547f` → **v3 통과**(`44e7f07`, 새 낮음 7 — M6에서 회사 자료 전 처리: 거부 문서가 sweep에 쓸림 V3-3, push.lock 6시간 교체 경쟁 V3-5 우선).
- 결과: 이 PC 2130 passed. VPS(Ubuntu 24.04, Python 3.12.3)에서 `1c00dbe` 기준 2031 passed·22 skipped(Windows 전용·실모델).
- VPS 단계: A 조사(읽기 전용 `tools/vps_survey.sh`, 출력은 저장소 밖) 완료, B 설치(사용자 `workmcp`, venv, LF 아카이브 — `git -c core.autocrlf=false archive`, `.gitattributes`) 완료.
- 발견: 시스템 gitconfig `core.autocrlf=true` 때문에 기본 `git archive`가 CRLF로 내보내 리눅스에서 스크립트·테스트 16개 실패 → LF 고정. VPS의 Bun 앱이 *:3000에 열려 있음(손대지 않음, 피터에게 알림).
- 다음: 피터 승인 순서대로 C(최신 커밋 재배포·모델 다운로드·config·토큰 2개) → D(systemd·nginx 443·certbot, 변경 내용 먼저 보임) → E(백업 타이머) → F(이 PC 합성 수집기 push, Claude Code 원격 MCP 검색·쓰기) → 보안 점검표 실행 → PLAN §5 완료 근거.

### 2026-10-26 — M4 완료 (구조화 지식·시간 이력)

- 새 Code 탭 세션에서 쓰기 도구 15개가 보여 완료 조건 ⑥ 수행: 시험 홈에서 record_entity → record_fact(근거) → get_entity → record_fact(내 메모, 충돌) → list_knowledge(충돌) → retract(충돌 해소) → supersede(유효 기간 끊기) → get_history → 시점 조회 2축 → record_task·완료 대체 → status, 셸에서 읽기 전용 DB 대조·`verify`·원본 해시 전후 동일. 모두 통과 — `doc/validation/2026-10-25_M4-연결확인_v1.md`.
- 결과: 1660 passed. PLAN §5 M4 완료 근거, §4를 M5로.
- 발견: 이 세션 셸이 다른 도구(hermes)의 `PYTHONPATH`·`VIRTUAL_ENV`를 물려받아 `uv run pytest`가 numpy import 오류, `doctor`가 mcp 버전을 잘못 표시 — 두 변수를 비우면 정상. M5 설치·진단 절차에 격리 실행 반영.
- 피터 질문(2026-10-26): "외부 PC에서 깃허브 주소를 주며 설치해 달라고 하면 설치되나?" → 아직은 아니다(비공개 저장소 인증, README·설치 명령 없음, 모델 2.3 GB 오프라인 반입, scan·index 필요) — M5 범위.
- 다음: 피터가 PLAN §4의 M5 `/goal`을 등록하면 M5 시작(설계 → 설치 명령·README·오프라인 묶음·보안 점검표 → 회사 PC 설치는 피터 승인 후).

### 2026-10-25 — M4 진행 (구조화 지식·시간 이력) — 실제 호출 대기

- 설계 `M4-설계_v1`(`4005b60`, 반영 기록 포함) → 0단계 `35f1d7d`(스키마 v3·서명·도구 10개 껍데기) → 1단계 병렬 A `55ec13f`·`6c705b8`(쓰기·결속·대체·철회·충돌), B `8b91551`·`a6b016b`(시점 질의·이력·출처 상태), C `a3e3097`·`7464ac6`(서버 쓰기 경로·두 프로세스·serve 감사 15개 도구) → 검토 v1 조건부(중간 2: reparse 뒤 근거 불일치 미표시, 재대체 시 이력 갈라짐) → `93bd351` → v2 조건부(중간 2: 근거 재확인 전체 스캔, reparse 뒤 verify 영구 실패) → `43a1e30`(마이그레이션 v4 인덱스, `stale after reparse` 경고 분리) → **v3 통과**(`ff78a68`).
- 총괄 결정: `as_of_valid` 기본 거르지 않음, 쓰기 응답 인용도 `delivered_sources`, `supersede`의 중간 행은 기록 시점에서 열어 둠, 대체 관계는 그래프(`continued_by_ids`·`branches`).
- 결과: 1660 passed, 원격 푸시 `43a1e30`.
- 남은 것: 완료 조건 ⑥ — Code 탭 세션의 도구 목록이 세션 시작 때 고정돼 새 쓰기 도구가 안 보임(서버 재시작으로 schema 4는 확인). 피터가 `/mcp`에서 `workmcp` 재연결 또는 새 Code 탭 세션을 연 뒤 쓰기→조회→대체→철회 호출 → 연결 확인 문서·PLAN 완료.
- 남은 한계 후보(M5): 검토 v3 L-e(위조 근거가 재파싱 경고로 분류될 수 있음 — 직접 SQL 전제), L-f(v4가 필요한 홈에서 다른 프로세스가 쓰기 잠금 중이면 serve exit 3).

### 2026-10-24 — M3 완료 (MCP 서버 v1)

- 1단계(A `83926b5`, B `e4bb9e9`, C `2b475d1`) → 2단계 통합 `c40775a`(hit `units[]` 압축으로 응답 최대 약 31,700자, 없는 정보 신호 R5 채택, 실모델 시험 홈 `C:\Users\<USER>\Motive_work_m3test`) → 검토 v1 조건부(중간 4) → `bbcf2c7` → v2 조건부(중간 1) → `6d92726` → **v3 통과**(`87cec83`).
- 실연결: 피터가 Code 탭에서 `.mcp.json` 서버 승인 → 이 세션에서 도구 5개 호출 성공, `tool_calls.client='claude-code' 2.1.299`, 전달 조각·`deliveries` 일치, 질의 기록 0, `verify` 893/893 (`2835e92`, `doc/validation/2026-10-24_M3-연결확인_v1.md`).
- Codex: 신뢰 전 프로젝트 설정 미인식, 번들 CLI 우회 실패(모델 거부·code-mode host 없음) → 피터 결정 "codex 일단보류하고 넘어가자" → M5.
- 피터 결정(설치·실행 형태): 회사 PC의 Claude/Codex에 일반 MCP처럼 설치, 설치 출처만 외부, 서버(로컬 stdio)·데이터는 회사 PC. PLAN §5 M5·§6에 기록.
- 결과: 1395 passed, `uv lock --check` 통과. PLAN §4를 M4로.
- 다음: 피터가 PLAN §4의 M4 `/goal`을 등록하면 M4(구조화 지식·시간 이력) 시작.

### 2026-10-24 — M3 진행 (MCP 서버 v1, 1단계)

- 설계(`M3-설계_v1`, `ccb6fd9`): 읽기 도구 5개(search·get_snippet·list_documents·recent_changes·status), 응답마다 `delivered_sources`와 로컬 `tool_calls`/`deliveries` 기록, 응답 크기 상한 40,000자, 오래 사는 서버의 모델 수명(lifespan ticker), 앱 가상화 탐침·doctor 경고, 클라이언트 설정과 연결 확인 절차(§11.4). 0단계(스키마 v2·도구 봉투·5개 도구 서버 껍데기) `b7217cb`, 1324 passed.
- 1단계 병렬: A 서버·도구·기록·감사, B 검색(N1·N2·접기·신호)·`get_snippet`·신호 측정, C 설정 템플릿·연결 확인 틀(`doc/examples/`, `doc/validation/2026-10-24_M3-연결확인_v1.md`, `2b475d1`).
- 발견(이 PC): Claude 데스크톱 앱 안 프로세스의 `%LOCALAPPDATA%\Motive_work` 쓰기가 `Packages\Claude_…\LocalCache\Local\Motive_work`로 돌려짐(탐침 확인). Codex 앱도 MSIX라 다른 홈을 볼 수 있음 → 두 클라이언트 설정 `env`에 AppData 밖 `WORKMCP_HOME` 명시.
- 설정 방침: 프로젝트 범위 파일(`.mcp.json`, `.codex/config.toml`)만, 커밋하지 않음(`.gitignore`), 전역 설정 편집 없음. 피터가 누를 승인: Claude Code 프로젝트 서버 승인, Codex 폴더 신뢰 승인.
- 다음: A·B 완료 → 2단계(통합·감사) → 총괄이 연결 확인(⑤) → 검토.

### 2026-10-24 — M2 완료 (색인·검색)

- 설계(`M2-설계_v1`, §14 반영 기록) → 0단계(의존성·스키마·서명) → 1단계 병렬(A 청크·색인, B 임베더·오프라인 반입, C 검색·코퍼스·평가) → 2단계(통합·실측) → 검토 v1 조건부(중간 2: 오래 사는 엔진의 옛 버전 hit, FTS 불일치 미검출) → 수정 → **검토 v2 통과**.
- 결과: 1257 passed, 실모델 bge-m3를 기본 데이터 홈에 반입·검증(SHA-256), 실측 문서 `doc/validation/2026-10-24_M2-검색실측_v1.md`.
- 발견: Claude 데스크톱 앱 안 프로세스의 `%LOCALAPPDATA%` 쓰기는 앱 패키지 가상화 폴더로 돌려진다 — 앱 밖 MCP 클라이언트가 다른 데이터 홈을 볼 수 있어 M3 연결 때 확인.
- 다음: 피터가 PLAN §4의 M3 `/goal`을 등록하면 M3(MCP 서버 v1, Claude Code·Codex 실제 연결) 시작.

### 2026-10-24 — M1 완료 (수집·파싱·원문 결속)

- 피터 결정(2026-10-24, "a"): B1f(압축하지 않은 88 MB PDF가 +1,388 MiB)는 한계로 인정 — M1 설계 §13 #17, M5 점검표에서 실제 문서로 재판단.
- 완료 조건 ①~⑦ 충족: 설계 2건, 51개 합성 묶음 증분 수집(되돌림·복원 포함, 원본 쓰기 0), 9개 형식 파서, 결속 100%(150/150), 1032 passed, 검토 v5 미해결 높음·중간 0. PLAN §5에 완료 근거, §4를 M2로.
- 다음: 피터가 PLAN §4의 M2 `/goal`을 등록하면 M2(색인·검색) 시작. 이 PC에 이미 받은 BGE-M3 모델(`../Motive_vari/data/kakao-demo/models/bge-m3/...`)을 SHA256 검증 후 재사용.

### 2026-10-24 — M1 진행 보고(40턴 도달, 멈춤)

- 완료 조건 상태: ① 설계(`M1-설계_v1`, `M1-파서자원설계_v1`) ② 증분 수집(51개 합성 묶음: 추가·변경·삭제·되돌림 A→B→A·복원, 원본 쓰기 0) ③ 9개 형식 파서 ④ 결속 100%(150/150) ⑤ 전체 테스트 1032 passed — 충족. ⑥ 검토: v1~v4 조건부(미해결 중간 2→2→5→2), v4 지적은 `9f10d6f`로 반영했고 **5차 검토 전**. ⑦ PLAN 전환·최종 기록 미완.
- 경과: 파서 자원 상한이 세 번 연속 우회돼 등급 3으로 재설계(파일당 단일 Budget, 위협 모델 "우연한 이상 파일 + 저비용 적대 파일", 보장 G1~G4). 탐침 124건 측정(`doc/validation/2026-10-24_M1-파서자원-탐침_v1.md`는 109건 판).
- 남은 결정(피터): **B1f** — 압축하지 않은 88 MB PDF 하나가 +1,388 MiB로 G1 초과. 선택지: (a) 한계로 인정 (b) PDF 비스트림 구문 사전 어휘 분석(MB당 약 1초) (c) 프로세스 격리(프로세스 생성 금지 규칙 변경 필요).
- 다음: 피터 결정 반영 → 검토자 5차 검토(v5) → 미해결 높음·중간 0이면 PLAN §5 완료 근거·§4 M2 전환·푸시.
- 커밋: `60b6e80`(0단계) `4635993`(1단계) `0c08885`(2·3단계) `8342e92` `5373557` `86c0b6c` `01902ac` `7765ff7` `9f10d6f`(검토 반영).

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
