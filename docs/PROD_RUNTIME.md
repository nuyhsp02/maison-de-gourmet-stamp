# PROD 실행·가입·복원 인수 절차

기존 DEMO Lightsail에 PROD를 추가할 때 사용하는 Linux 운영 절차다. 대표 주소는 `https://gourmetstamp.kr`로 확정했다. Nginx와 공인 IP를 공유하고 서비스 계정·앱·DB·키·백업은 환경별로 분리한다. 고객 화면 공개와 가입 정책 승인은 별도로 인수한다. [설계 결정](decisions/ADR-011-prod-environment-and-enrollment.md)

## 실행 호스트와 행사

| 위치 | 보관 내용과 책임 |
|---|---|
| `/opt/maison-prod` | 검토한 단일 커밋의 런타임과 전용 가상환경. 운영자 소유, 서비스 읽기 전용. [패키지 제작 도구](../tools/build_prod_package.py) |
| `/etc/maison-prod` | 운영자가 제공한 행사 JSON, 연락처 수신 공개 정보와 `prod.env`의 `PUBLIC_URL`. 설정은 운영자 소유, 서비스 계정에는 읽기만 허용한다. |
| `/var/lib/maison-prod` | 로컬 영속 디스크, 서비스 소유 0700. `instance/prod.sqlite`, 같은 이름 뒤에 붙는 `.profile.key`, `.admin.key`, `.manager.key`는 0600. |
| 지정 운영자 Mac | 별도의 PROD SSH 신원, 암호화한 연락처 개인키와 독립된 복구 사본. 개인키는 서버나 서버 백업에 복사하지 않는다. |
| 별도 PROD 백업 보관소 | 회차별 암호문, 신뢰하는 경로로 전달한 회차 지문, 정책에 따른 만료·파기 기록. 복호화에 쓰는 백업 개인키는 서버 밖에 보관한다. |

서비스 시작 예정일은 2026-12-01 KST이다. [배포 불가능한 행사 초안](../deploy/linux/prod-event.example.json)을 저장소 밖에 복사하고 시작 시각, 전용 행사 ID(`prod-` 접두사), HTTPS 원본, 매장명·제목·행사 설명·보상과 최종 상품 정보를 확정한다. 날짜에는 시간대를 명시한다. 적립은 2027-02-01 00:00 KST, 보상은 2027-03-01 00:00 KST에 끝난다. 고객 접근 종료와 개인정보 파기의 기준은 2027-03-31 00:00 KST다. 정각부터 서버가 고객·직원·운영자의 카드 및 연락처 접근을 닫는다. DB 파기와 백업 삭제는 독립 보관 기록과 각각 대조한다. [파기 범위와 보존 검토](decisions/ADR-012-prod-privacy-erasure.md)

상품 10개 각각에 `id`, 정식 이름 `name`, 간략명 `short_name`, `description`, `category`, `qr_product_id`, `image`가 필요하다. `qr_product_id`는 해당 상품 ID로 명시한다. 기존 시즌 4개·지정 6개 분류를 유지한다. 이번 버전은 사진을 사용하지 않는 `image: null`만 지원한다. DEMO 상품 JSON, QR 파일 또는 기존 DB를 복사해 준비하는 경로는 없다.

## 준비, 확인, 기동

다음은 추후 운영 승인 시의 순서다. 이전 단계에서 설치한 서버 버전은 `444b828`이며 서비스는 정지·비활성으로 인수했다. 이번 단일 서버 설계 변경은 로컬 후보에만 적용했다.

1. 지정 Mac에서 키를 준비한다. 연락처 도구의 `keygen-prod --key <새 암호화 파일> --public-config <새 공개 파일> --event <PROD 행사 ID>`는 대화형 숨김 입력을 사용한다. 기존 DEMO `keygen`은 합성용이다. `rewrap`으로 별도 보호 사본을 만들고 복원한 키의 일치를 확인한다. PROD 키 생성과 암호화 키 사본 출력은 저장소 밖의 소유자 전용 디렉터리(0700)에서만 허용한다. 서버로는 공개 파일만 전달한다.
2. Linux의 전용 서비스 계정으로 빈 상태 디렉터리를 준비한다. `python deploy/linux/prod.py prepare`에 `--state-dir`, `--config`, `--origin`, `--recipient`를 지정한다. 성공하면 검증된 `instance`가 한 번만 배치된다. 다시 실행해 장애를 복구하는 절차는 없다.
3. `python app/manage.py --database <절대 PROD DB> prod-staff-password --actor <직원 ID>`와 `prod-manager-password --actor <지정 관리자 ID>`에서 각 비밀번호를 터미널로 입력한다. 계정과 비밀번호를 나누며 관리자 한 명이 자기 자격만 사용한다.
4. 아래 절차로 로컬 마감 기록과 분리 보관한 승인 자료, 일상 백업 정책·감사 처분·복원 연습 근거를 준비한다. 같은 네 인자로 `prod.py check`를 수행한 뒤 `serve`한다. [systemd 유닛](../deploy/linux/maison-prod.service)은 초기화를 실행하지 않고, 검증 후 Waitress 단일 프로세스를 loopback 8876에 바인딩한다. 배포 시 실제 SQLite가 수정된 WAL 런타임인지 선행 검사한다. 의존성은 저장소의 잠금 파일을 사용한다. [의존성 잠금](../requirements-windows.lock)
5. 행사 JSON의 `public_origin`, `prod.env`의 `PUBLIC_URL`, 실행기와 QR 내보내기의 `--origin`에 모두 `https://gourmetstamp.kr`를 사용한다. 아래 순서로 공유 Nginx에 PROD를 연결한다.

## 공유 Nginx와 DNS 전환

| 호스트 | 일반 HTTP·HTTPS 요청 | 앱과 인증 |
|---|---|---|
| `gourmetstamp.kr` | HTTP는 같은 경로·쿼리의 대표 HTTPS로 308, HTTPS는 고객 서비스 | `127.0.0.1:8876`. PROD에서 상위 Basic Auth 상속을 해제한다. 직원·관리자 기능은 앱이 권한을 검증한다. |
| `www.gourmetstamp.kr` | HTTP·HTTPS 모두 같은 경로·쿼리의 `https://gourmetstamp.kr`로 308 | 별칭은 리다이렉트만 담당한다. |
| `demo.gourmetstamp.kr` | 기존 DEMO HTTP·HTTPS 블록과 인증서 유지 | `127.0.0.1:8875`. 기존 입장 Basic Auth 유지. |

2026-11-26 사전 점검 기준으로 루트와 `www`의 A 레코드는 기존 주소를 가리키며, PROD용 DNS 전환과 인증서 발급은 아직 이루어지지 않았다. 다음은 별도 배포 승인 후의 절차다. [PROD Nginx 설정](../deploy/linux/nginx-prod.conf)

1. 기존 80·443의 IPv4·IPv6 기본 서버를 그대로 두고, 두 PROD 이름에 대한 명시적 가상 호스트만 추가한다. DNS 전환 전에 양쪽 호스트를 격리 환경에서 함께 검사한다. `nginx -t` 결과와 이름 충돌 경고, DEMO의 401 Basic 챌린지, PROD의 익명 접근과 직원 API의 앱 인증을 확인한다. 개인정보가 들어갈 수 있는 URI·헤더·쿠키·요청 본문은 접근 로그에 기록하지 않는다.
2. 최초 발급 때는 PROD용 HTTP 블록을 먼저 준비한다. 두 이름 모두 `/.well-known/acme-challenge/`의 검증 파일을 전용 webroot `/var/lib/maison-prod-acme`에서 제공해야 한다. 인증서가 준비되기 전에는 PROD TLS 블록을 활성화하지 않는다. DNS 전환에 앞서 기존 두 A의 값과 TTL, 설정 사본을 기록하고 서비스 전환 시간을 정한다.
3. ITEASY에서 기존 루트 A와 `www` A의 대상만 기존 Lightsail 고정 IPv4로 변경한다. `demo` A와 MX·TXT·CAA·NS 등 다른 레코드는 그대로 둔다. 두 이름의 DNS 응답과 HTTP-01 경로를 확인한 뒤 두 이름을 SAN에 포함하는 별도 PROD 인증서를 발급한다. DNS가 새 IP를 반환하기 시작하면 PROD 인증서를 설치할 때까지 해당 고객의 HTTPS 연결은 거부된다. `www`의 308도 HTTPS 연결이 성립한 뒤에 전달되므로 이 구간을 점검 시간에 포함한다.
4. PROD 인증서의 `/etc/letsencrypt/live/gourmetstamp.kr/fullchain.pem`과 같은 디렉터리의 `privkey.pem`에 연결한 뒤 통합 설정을 검사하고 승인된 시간에 Nginx를 다시 읽힌다. DEMO의 인증서와 갱신 후크는 보존하고 PROD 인증서의 갱신·재적용을 별도로 검증한다. 외부에서 두 PROD 이름의 체인·리다이렉트·가입 잠금과 DEMO의 인증·내부 포트 분리를 확인한다.
5. 전환에 실패하면 변경 전 기록에 따라 두 A를 복귀시키고 추가한 PROD 가상 호스트만 되돌린다. DNS 캐시에 남아 있는 새 주소의 응답도 확인한다. 공유 서버 재부팅 시험은 DEMO 이용자에게도 영향을 주므로 별도 시간에 승인받는다.

## 사이트 공개와 가입 승인

로컬 마감·보관 정책이 확인되어 서비스가 기동한 뒤에도 기본 유닛에는 가입 정책 경로나 승인 지문이 없다. 접근 종료 전 이 상태에서 가입은 `403 registration_closed`, 기존 카드 찾기는 이름·전화번호가 일치할 때만 세션을 발급한다. 승인본 확보 후 별도 승인된 설정에서만 `serve --policy <승인 문서> --approved-policy-sha256 <별도 기록한 SHA-256>`를 사용한다. 승인된 안내의 형식과 항목은 [ProductionEnrollment](../app/contact_privacy.py)에 정의한다. 지문은 승인 시점에 독립적으로 기록한다. 현재 파일의 지문을 기동 스크립트가 자동으로 계산해 채우면 승인 절차가 사라진다. 승인본 삭제·내용 변경은 새 가입을 닫는다. 정책 교체는 새 승인과 재시작이 필요하다.

## 운영자 조회, QR, 백업

- 직원은 전체 번호로 한 건을 검색하고 마스킹 결과만 본다. 지정 운영자의 별도 SSH 키는 `restrict`와 고정 명령으로만 허용한다. 서버 명령은 `prod_contact_ssh.py --state-dir ... --config ... --recipient ... --actor <고정 운영자 ID>`다. 파일은 서비스 소유 0700/0600이므로 고정 명령만 서비스 계정 권한으로 실행되도록 설정한다. 일반 셸·포트 전달·직원 비밀번호를 통한 실행 권한을 주지 않는다. Mac의 `contact_mac.py show`에서 카드 한 건과 목적을 입력한다. 서버는 감사 저장 성공 뒤 암호문을 내보낸다. 터미널 화면 촬영과 운영자의 목적 외 사용은 기술만으로 막을 수 없다.
- `prod.py qr-export`는 같은 네 인자와 별도 비공개 디렉터리의 새 `--out`을 요구한다. 기존 DB를 읽어 일반 입장 SVG 1개와 상품 SVG 10개, 상품 ID 대응 manifest를 쓴다. 실제 QR URL·출력물은 Git과 공개 보고서 밖에서 관리한다.
- [PROD 백업 도구](../deploy/linux/prod_backup.py)의 `create`에 `--state-dir --config --recipient --recipient-cert --cert-sha256 --output`을 제공한다. 복원 담당자가 별도 확인한 **백업 인증서 DER 지문**을 사용한다. 서비스 소유 디렉터리에서만 평문 스냅샷과 프로필 키를 준비하고, 외부 보관소에는 암호문만 게시한다. 회차별 **암호문 SHA-256**과 익명 검증 요약을 독립된 기록에 남긴다. 연락처 수신키와 백업 수신키는 용도가 다르다.
- 보관 장치의 `restore`에 암호문·수신자·키·후보 경로와 `--approval --signature --receipt --lifecycle-evidence --lifecycle-policy`를 지정한다. 서버 밖 승인자의 한시적 서명과 실제 수신·처분 증거가 필요하다. 기존 서비스 디렉터리를 덮어쓰지 않고 새 격리 후보만 생성한다. DB·프로필 키, 행사·상품·QR, 카드·세션·스탬프·보상·정정·연락처 접근 감사 건수와 지문을 대조한다. 직원과 관리자 자격은 복원 후보에서 폐기하므로 두 역할 모두 재발급하고 이전 쿠키가 거부되는지 확인한다. 종료 전 인계에서는 최신 원본과 후보를 인벤토리에 등록하고 분리한다. 종료 후에는 과거 사본을 재파기한 후보만 검증할 수 있다.
- 승인된 보관처별 암호문·복원 후보·임시 파일·이전 버전과 제공자 스냅샷의 만료·대조 방법은 [백업 만료 인수 절차](PROD_BACKUP_EXPIRY.md)에 있다. 외부 장치 연결·정상 전송, 전체 출처 열거 및 실기기 인수는 운영 책임자가 확인해야 한다.

## 단일 서버의 시각·종료 상태와 3월 마감

1. 새로 준비한 행사 ID와 DB의 `metadata.prod_instance_id`를 확인한다. 별도의 데이터 경로 `/var/lib/maison-prod-lifecycle`은 서비스 소유 0700으로 두고 DB 스냅샷·복원 대상에서 제외한다. Linux 호스트에서 시간 동기화를 활성화하고 실제 커널 동기 상태와 재부팅 후 동기화까지의 대기 시간을 시험한다. 커널 질의 실패나 동기화 상실 시 서비스가 개인 정보를 제공할 수 없다.
2. root 소유이며 일반 계정이 쓸 수 없는 디렉터리와 승인 파일을 사용한다. root가 관리하는 환경에 `MAISON_PROD_LIFECYCLE_APPROVAL`과 독립적으로 확인한 `MAISON_PROD_LIFECYCLE_APPROVAL_SHA256`을 지정한다. 승인 JSON은 `format=prod-lifecycle-approval-v1`, `event_id`, `instance_id`, `access_until`(고정 2027-03-31 00:00 KST의 정수 epoch), `policy`를 가진다. 정책에는 `retention_days=14`와 `retention_approval`, `inventory_approval`, `audit_deletion_approval`, `restore_drill`의 근거 SHA-256을 사용한다. 미확정 근거를 시험값으로 채우지 않는다. 초기 훈련용과 실제 승인 시점의 문서·지문은 별도 보관한다.
3. 빈 전용 경로에서 [로컬 통제 도구](../deploy/linux/prod_local_lifecycle.py)의 `init`을 한 번 실행한다. 기본 `MAISON_PROD_LIFECYCLE` 경로는 `/var/lib/maison-prod-lifecycle/lifecycle.sqlite`이다. 누락한 승인을 채울 때는 운영 책임자가 원본과 지문을 다시 확인하고 `approve`를 실행한다. 기동 실패를 해결하려고 기록을 초기화하지 않는다. 웹과 운영자 SSH 고정 명령은 같은 root 관리 환경에서 이 로컬 기록, 승인 지문과 활성 루트(`MAISON_PROD_LIVE_STATE`, 기본 `/var/lib/maison-prod`)를 사용한다. SSH 호출자의 환경으로 경로를 교체할 수 없게 한다.
4. 실제 파기 유닛을 준비할 때 `PROD_EVENT_ID`를 설정한다. [지속 타이머](../deploy/linux/maison-prod-purge.timer)와 [서비스](../deploy/linux/maison-prod-purge.service)는 마감에 웹 프로세스를 중지하고 로컬 파기를 수행한다. 재부팅·다음 앱 기동도 마감을 검사한다. 장기 WAL 읽기, 미분류 파일, 미승인 감사 처분 때문에 실패하면 접근을 닫은 채 원인 제거 후 재시도한다. `evidence` 출력의 원본 파기 시각과 전체 사본 완료 여부를 따로 읽고 별도 보관한다.
5. 전체 호스트 복원 때는 서비스 자동 시작과 고객 인입을 먼저 격리한다. 신뢰할 수 있는 현재 시각, 고정된 코드 버전·컷오프, 인스턴스·키, 외부 수신·복원 승인·파기 증거를 대조한 뒤에만 인계한다. 서버 이미지가 로컬 이력과 시계까지 되돌리면 앱 단독으로 이를 증명할 수 없다. [현재 설계와 이전 보호 차이](decisions/ADR-013-single-server-privacy.md)

## 기존 Lightsail의 정지 런타임 교체 조건

코드 리뷰와 별도 커밋·배포 승인을 받은 뒤 `python tools/build_prod_package.py --commit <승인한 전체 SHA> --output <저장소 밖의 새 ZIP>`으로 해당 SHA의 패키지를 만든다. 이전 패키지로 이번 변경을 재현할 수 없다. 기존 `444b828` 패키지와 유닛의 지문, DEMO 복구 영수증과 서비스를 먼저 대조한다. PROD가 정지한 상태에서 새 후보 경로의 manifest·의존성·고정 SQLite와 systemd 유닛을 검증한다. PROD 코드와 유닛만 교체하고 DEMO 서비스나 공유 Nginx를 재시작하지 않는다. 현재 설치 단계에서는 PROD DB가 없다. 나중에 DB가 생기면 코드 롤백과 DB 복원을 분리하고 과거 백업으로 운영 상태를 덮어쓰지 않는다.

시각 장애·재부팅·객체 저장소 장애, 외부 수신과 삭제 증거, 수동 복원을 실제 승인 자원에서 다시 인수한다. DNS·인증서 전환, 가입 정책 승인·직원 자격 발급·실제 개방은 각각 별도 단계다. [객체 보관과 알림](PROD_BACKUP_EXPIRY.md)
