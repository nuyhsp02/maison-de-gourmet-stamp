# 연락처와 조회 권한 · 합성 환경 운영 절차

이 브랜치는 운영 정책 검토를 위한 로컬 버전이다. 기본 기동에서 신규 가입은 닫히고 기존 카드 찾기와 기존 세션은 작동한다. 명시적 이관, 신뢰한 수신 공개키, 합성 검수 명단, 로컬 또는 예약된 시험용 주소가 모두 있을 때에만 지정된 합성 이름과 번호 쌍을 등록할 수 있다. 가입 요청은 화면에서 확인한 새 안내 버전도 보내야 한다. 기존 프로필의 안내 기록은 유지한다.

## 누가 무엇을 볼 수 있나

| 대상 | 입력과 결과 |
|---|---|
| 고객 | 가입 확인 화면에서 방금 입력한 번호를 재확인한다. 가입이 끝나면 고객 상태 응답에는 연락처 필드가 없다. 이름과 번호로 찾기는 기존 HMAC으로만 조회한다. |
| 직원 | 전체 번호를 검색란에 입력할 수 있다. 요청은 POST 본문으로 보내고 검색란을 비운다. 결과는 접두부·별표·끝 네 자리 또는 ‘연락처 미보관’이다. 고객 카드만으로 직원 API를 사용할 수 없다. |
| 지정 Mac 운영자 | 전용 SSH 신원, 고정한 서버 호스트 키, 카드 ID, 허용된 업무 용도와 Mac 개인키가 필요하다. 서버는 조회 감사를 커밋한 뒤 해당 카드의 암호문 한 개를 보내고 Mac에서 잠시 표시한다. |
| 서버 | 가입 처리 중 입력을 메모리에서 정규화한다. 카드 프로필에는 기존 HMAC과 이름을, 별도 연락처 행에는 암호문과 마스크를 저장한다. 서버에 연락처 복호화 개인키를 배치하지 않는다. |

## 저장·이관

기존 열, 고유 전화 HMAC, 제품·QR, 고객·직원 세션, 복구 링크, 적립·보상 및 취소 감사는 그대로 사용한다. 새 SQL은 기본 초기화 SQL과 별도인 [contact_schema.sql](../app/contact_schema.sql)에 있다.

| 추가 항목 | 용도 |
|---|---|
| customer_contacts | 프로필 ID가 기본키이자 외래키다. 카드당 암호문 하나, 형식 버전, 공개키 지문, 직원용 마스크, 수집 시각을 기록한다. |
| contact_access_audit | 요청 ID, 시각, 운영자 식별자, 대상 카드 ID, 용도와 결과를 기록한다. 마스크, 암호문, 전체 번호 및 HMAC은 기록 대상에서 제외한다. |
| metadata | contact_recipient_v1에 공개키 지문, Linux의 linux_demo_inventory_v2에 스키마·상품 QR 집합·행사·지문을 기존 프로필 키로 검증하는 값을 둔다. 기존 v1 메타데이터도 보존한다. |

주소와 연락수단은 실제 값 대신 아래 기호로 표시했다. 명령 대상은 검수용 신규 합성 환경이다. 서버용 명령은 앱에 고정한 의존성을 설치한 Python으로 실행한다.

1. 지정 Mac에서 암호화된 개인키와 공개 설정을 생성한다. 이름과 번호는 합성 검수 도구가 정의한 고정 조합만 허용한다. 개인키 파일·공개 설정·백업 파일 경로는 새 전용 디렉터리에 둔다. 파일 생성은 기존 이름을 덮어쓰지 않는다.
2. 서버 측 합성 환경에 공개 설정만 복사한다. 배치 전 Mac의 공개키 SHA-256 지문과 대조하고 서비스 계정이 설정을 바꿀 수 없도록 구성한다. DB 프로필 키, CMS 백업 수신 키, 연락처 키, SSH 신원을 각각 구분한다.
3. `MIGRATION_WORKSPACE`에는 아직 존재하지 않는 절대 경로를, `EVENT_ID`에는 승인된 합성 행사 설정의 ID를 지정한다. `prepare-migration`이 이관 목적과 경로·파일 식별·최초 인벤토리를 소유자 전용 확인 파일에 남기고, 새로운 합성 원본과 별도 빈 복사본을 만든다. 두 디렉터리를 이동하거나 복원본으로 덮어쓰면 다시 준비해야 한다.
4. 원본·대상의 행사·상품·QR·서명과 스키마 단계, 기존 고객·적립·세션·감사 데이터가 없는 상태를 확인한다. 쓰기 잠금 후 재검사를 통과한 대상에만 연락처 스키마와 서명을 한 트랜잭션으로 추가한다. 고객이 들어간 DB의 데이터 보존은 격리 테스트에서 검증한다. 공개 연습 명령은 해당 DB에서 중단한다.
5. 빈 v2 대상에서 동일 공개키로 재확인하려면 `--expected-schema contacts`를 지정한다. 정정까지 연습하려면 최초 준비 때 `--purpose contacts+corrections`를 선택하고, 정정 명령에 현재 스키마를 지정한다. 원본 DB나 실제 DEMO 상태는 이관 연습 대상으로 등록할 수 없다.
6. 합성 직원 자격을 설정한 뒤 기동 검사와 격리 복원 훈련을 한다.

~~~sh
python tools/contact_mac.py keygen --event "$EVENT_ID" --key "$MAC_CONTACT_KEY" --public-config "$PUBLIC_CONFIG"
python deploy/linux/demo.py prepare-migration --workspace "$MIGRATION_WORKSPACE" --purpose contacts --config "$EVENT_CONFIG" --origin https://fixture.example.invalid
SYNTH_STATE="$MIGRATION_WORKSPACE/target"
python deploy/linux/demo.py migrate-contacts --state-dir "$SYNTH_STATE" --source-state-dir "$MIGRATION_WORKSPACE/source" --expected-event-id "$EVENT_ID" --expected-schema base --config "$EVENT_CONFIG" --origin https://fixture.example.invalid --synthetic-config "$PUBLIC_CONFIG"
python app/manage.py --database "$SYNTH_STATE/collection.sqlite" admin-password --actor "$STAFF_ID"
python deploy/linux/demo.py check --state-dir "$SYNTH_STATE" --config "$EVENT_CONFIG" --origin https://fixture.example.invalid --synthetic-config "$PUBLIC_CONFIG"
~~~

신규 가입은 고객·프로필·연락처·세션을 한 트랜잭션에서 만든다. 정규화한 번호에 대한 기존 프로필 HMAC 고유 제약이 동시 가입에도 적용된다. 암호문에는 행사 ID·형식·카드 ID가 함께 들어가며 Mac 조회 시 요청 대상과 비교한다. 마스크는 평문 저장되는 부분 개인정보이므로 DB와 백업의 기존 접근 제한을 적용한다.

## 운영자 한 건 조회와 직원 비밀번호

검수용 SSH 계정에는 운영자별 공개키를 별도로 등록한다. sshd에서 공개키 인증만 허용하고 PTY·포워딩·에이전트 전달·사용자 rc·임의 명령을 제한한다. authorized_keys의 restrict와 강제 명령으로 서버 관리자가 고정한 상태 경로, 행사 설정과 actor를 넘긴다. 운영자 요청에는 카드 ID와 용도만 담긴다. 서버는 임의 SSH 명령을 거부한다. 서버의 래퍼와 코드·공개 설정은 SSH 사용자와 서비스 사용자가 수정할 수 없도록 한다. 서비스 계정으로 전환해야 한다면 고정 래퍼 하나만 실행하도록 제한하고 sshd가 설정한 SSH_CONNECTION 및 SSH_ORIGINAL_COMMAND의 검사를 유지한다. 운영자별 SSH 공개키를 폐기하면 해당 신원의 접근을 회수할 수 있다.

~~~sh
python deploy/linux/contact_ssh.py --state-dir "$SYNTH_STATE" --config "$EVENT_CONFIG" --actor "$FIXED_OPERATOR"
python tools/contact_mac.py show --ssh-config "$MAC_SSH_CONFIG" --host "$OPERATOR_HOST_ALIAS" --event "$EVENT_ID" --key "$MAC_CONTACT_KEY"
python app/manage.py --database "$SYNTH_DB" admin-password --actor "$STAFF_ID"
~~~

첫 줄은 **서버 강제 명령**의 형태다. Mac에서는 전용 SSH 신원과 사전에 대조한 known_hosts를 설정한다. 담당자와 복구 사본 보관자 접근을 분리한다. 허용 용도는 상품 준비·수령(pickup), 보상(reward), 고객 문의(inquiry)다. 대상 고객 ID로 한 건씩 열어 본다. 새 직원 비밀번호는 터미널 숨김 입력으로 두 번 받아 15~256자, 흔한 표현·반복·연속 패턴을 검사한다. 기존 검증 파일의 원자적 교체로 이전 직원 세션이 무효화된다.

운영 조회 결과는 unavailable(미보관), released(암호문 전달 승인), displayed(Mac 표시 통지 수신), decrypt_failed(복호화 실패 통지 수신)로 구분한다. 통신이 끊겨 released로 남은 기록만으로 실제 열람을 단정할 수 없다. 감사 INSERT 또는 커밋 실패 시 암호문을 송신하지 않는다. 조회용 Mac은 화면 녹화, 터미널 세션 기록, 화면 공유를 끈 대화형 콘솔을 사용한다. 화면 캡처나 단말 자체의 기록은 프로그램이 통제할 수 없다.

## 백업·복구·롤백

DB와 기존 프로필 키는 [기존 암호화 백업](DEMO_BACKUP.md)으로 보존한다. v2 백업의 DB에는 연락처와 조회 감사도 포함된다. 별도로 환경별 공개 설정과 그 신뢰 지문, 연락처 개인키의 암호화 복구 사본과 키별 독립 암호를 보관한다. 로컬 파일의 소유자 전용 권한을 유지하고, 복구 사본은 접근 통제가 다른 암호화 보관소에 둔다. Mac이 꺼져 백업 전달·복원 확인을 못 한 회차는 미완료다.

~~~sh
python tools/contact_mac.py rewrap --key "$MAC_CONTACT_KEY" --output "$NEW_OFFLINE_COPY"
python tools/contact_mac.py rewrap --key "$NEW_OFFLINE_COPY" --output "$NEW_RESTORED_KEY"
~~~

새 파일로 복원한 DB와 프로필 키를 검증하고 복구한 연락처 키로 합성 한 건을 열어 지문·카드·행사를 대조한다. CMS 개인키와 연락처 개인키의 복원 시험을 각각 수행한다. 새 수신 키로 기존 지문을 덮어쓰는 방식의 회전은 지원하지 않는다. 이전 암호문의 보유기간 동안 해당 개인키의 복구 수단을 유지한다.

- **이관 트랜잭션 실패** 시 기존 v1 상태를 다시 검사한다. v2 스키마 자동 생성이나 서명 재발급으로 우회하지 않는다.
- **이관 후 쓰기 전**에는 쓰기 중지와 신규 기록 0건을 확인한 경우에 한해, 별도 디렉터리에서 검증한 이관 전 백업과 구버전을 함께 사용할 수 있다.
- **이관 후 쓰기 발생** 시 최신 DB·프로필 키·공개 설정·연락처 키와 감사를 먼저 확보한다. 구버전은 추가 테이블을 거부한다. 최신 DB를 그대로 읽는 호환 수정 버전으로 복구한다. 과거 백업 덮어쓰기나 신규 테이블 삭제는 이후 적립·연락처·감사를 유실시킨다.

## 정식 운영 전 결정할 내용

상품 준비·수령, 보상 및 문의 대응 용도는 검토안이다. 광고·판촉은 허용 용도에서 제외했다. 필수 또는 선택 수집, 거부 시 영향, 정확한 보유기간과 기산점, 철회·삭제 및 백업 만료 절차, 최종 안내, 담당자와 복구 사본 보관자를 확정해야 한다. 기존 가입 안내를 새 연락 목적에 소급하지 않는다. 선택 수집으로 결정하면 연락처 보관에 동의하지 않아도 카드를 이용할 수 있는 경로와 철회 기록을 별도 설계한다. 현 버전에는 실제 고객을 위한 가입 개방 스위치가 없다.
