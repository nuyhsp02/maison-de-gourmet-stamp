# PROD 백업, 복원 승인과 만료

기존 Lightsail이 일상 백업을 만들고 지정한 외부 저장소에 보낸다. 보유 기한은 **원본 생성 시각 + 14일과 2027-03-31 00:00 KST 중 이른 시각**이다. 전송 재시도나 복원 장치로의 복사는 기산점을 연장하지 않는다. 고객·직원 서비스는 이 전송의 성공을 기다리지 않는다. [결정 이력](decisions/ADR-012-prod-privacy-erasure.md) · [현재 결정](decisions/ADR-013-single-server-privacy.md) · [서비스 인수](PROD_RUNTIME.md)

## 설치 전에 정할 보관처

| 위치 | 소유자와 용도 | 인수할 증거 |
|---|---|---|
| Lightsail | PROD 상태 디렉터리, 로컬 마감 기록과 전송 기록, 임시 작업 공간. CMS 암호화에는 수신 공개 인증서를 사용한다. AWS 서명용 클라이언트 키는 백업 전송 계정만 읽을 수 있는 별도 경로에 둔다. | 대상 인스턴스·승인 지문, 시간 동기화, 용량, 생성·재시도·만료 및 재부팅 |
| 외부 데이터 버킷 | 비공개 버전 관리 S3에 행사·인스턴스별 CMS 암호문 저장 | 소유 계정, 버전 ID, SHA-256, 크기, 삭제 후 전 버전 재조회 |
| 외부 증거 버킷 | 암호문과 분리한 수신·만료 영수증, 승인 원본과 익명 파기 상태. 백업 작업에는 증거 삭제 권한을 부여하지 않는다. | 서버 밖 책임자가 보관한 정책 지문, 객체 버전, 삭제·복원 검토 기록 |
| 지정 Mac | 암호화된 복호화 키 및 별도 복구 사본, 필요할 때만 복원 후보 검증·운영자 조회 | FileVault, 키 복구 훈련, 승인 서명 검증, 수동 복원 사본 목록과 폐기 영수증 |
| 수동 확인 보관처 | 이전 DB·복원·격리·임시 사본, 기기 백업, 휴지통, 복제·내보내기, 디스크와 공유 서버 이미지 | 각 보관처의 담당자, 처분 권한, 재열거 결과. 조사하지 못한 보관처는 미결로 둔다. |

### 서버 설정

1. 웹과 원본 DB는 `maison-prod`, AWS 전송은 `maison-backup`으로 실행한다. 두 계정 모두 로그인 셸·sudo가 필요 없다. DEMO와 그룹을 공유하지 않는다. 데이터 소유자의 원본 작업은 systemd가 여는 전용 소켓에서 요청을 받아 일관된 SQLite 스냅샷을 CMS로 암호화하고 로컬 보관 기록을 갱신한다. 전송 계정에는 읽기 전용 암호문 파일 디스크립터만 넘긴다. 원본 작업에는 외부 네트워크와 AWS 자격이 필요 없다. 서버 root는 양쪽 권한을 가진다. [원본 작업](../deploy/linux/prod_backup_source.py) · [소켓](../deploy/linux/maison-prod-backup-source.socket) · [전송 유닛](../deploy/linux/maison-prod-backup.service)
2. `/etc/maison-backup-job/job.env`는 root:root 0600으로 두고 systemd가 원본·전송 유닛에 읽어 준다. 두 유닛에 같은 `MAISON_PROD_BACKUP_JOB`, `MAISON_PROD_BACKUP_JOB_SHA256`, `MAISON_PROD_LIFECYCLE_APPROVAL_SHA256`을 제공한다. `job.json`과 그 부모는 root 소유로 두며 두 계정에 설정 읽기만 허용한다. JSON 필드는 `state_dir`, `event`, `event_id`, `recipient`, `certificate`, `cert_sha256`, `instance_id`, `spool`, `work`, `posix_receipts`, `data_bucket`, `evidence_bucket`, `aws_account`, `region`, `lightsail_instance`다. 승인한 행사 ID·인스턴스 ID 및 설정 원본 SHA-256을 대조한다. 기존 `prod.env`의 원본 보관·생명주기 설정은 원본 유닛에만 추가한다. `spool`·`posix_receipts`는 `maison-prod` 소유의 분리된 0700 디렉터리, `work`는 `/var/lib/maison-backup/work`에 `maison-backup` 소유 0700으로 둔다. 전송 기록은 암호문 파기 볼륨 밖에 둔다. 가동 이력이 있다면 기존 전송 SQLite와 보관 원장을 짝지어 검증하고 수신 증거를 대조한 뒤 이관한다. 기존 기록 소실 시 빈 전송 기록으로 가동하지 않는다.
3. `MAISON_PROD_BACKUP_ROOTS`와 `MAISON_PROD_BACKUP_ROOTS_SHA256`은 호스트별 승인 목록과 독립 보관한 지문이다. JSON은 `format: prod-backup-roots-v1`, `event_id`, `instance_id`, `volumes`, `manual_kinds`를 갖는다. 볼륨마다 절대 `path`, 중복 없는 32자리 소문자 16진 `volume_id`, `kinds`, 불리언 `service`를 둔다. 서비스 볼륨은 `temporary`만, 암호문 스풀은 `archive`와 `temporary`, Mac 복원 볼륨은 `restore`와 `quarantine`을 취급한다. 서비스 볼륨은 실제 DB 인스턴스와 대조한다. 다른 볼륨은 소유자 전용 `.maison-custody-volume`에서 ID를 확인한다. 서버 계획에는 상시 장착한 볼륨만 넣는다. 서버 밖의 `external`, `snapshot` 및 장치에서 따로 확인할 종류는 `manual_kinds`로 지정한다. 누락·오인 마운트, 미등록 파일, 삭제한 사본의 재출현은 수동 조사 대상이다. [보관처 검사](../deploy/linux/prod_backup_custody.py)
4. 전송 계정의 AWS CLI v2는 별도 프로필의 `credential_process`로 IAM Roles Anywhere 서명 도우미를 실행한다. 자체 CA 개인키와 CMS 복호화 키는 서버 밖에서 용도별로 관리한다. 클라이언트 키와 프로필은 `/etc/maison-backup`에 root:maison-backup 0440으로 보관하고 부모는 0750으로 제한한다. 도우미 출력을 표시하거나 임시 자격을 저장하지 않는다. [AWS 서명 도우미](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/credential-helper.html)

   지원되는 AWS CLI v2와 두 개의 서로 다른 비공개 버전 관리 버킷, 객체 소유 계정 확인을 준비한다. 작업 자격에는 해당 접두사의 버전 열거·검증·조건부 쓰기, **데이터 버전** 삭제, 진행 중인 멀티파트 목록 확인, 지정 리전의 Lightsail 인스턴스·스냅샷 읽기, 해당 CloudWatch 지표 쓰기만 허용한다. 증거 변경·삭제 권한과 공유 서버 스냅샷 삭제 권한을 분리한다. 다른 리전·계정·AWS Backup 등 별도 보관처의 조사·삭제는 지정 책임자의 몫이다. [클라우드 어댑터](../deploy/linux/prod_s3.py)
5. 공급자 보조 수명 주기 정책은 현재·과거 버전과 삭제 마커를 포함하도록 별도로 검토한다. 일자 단위 지연이 가능한 공급자 정책만으로 생성 시각 기준 14일이나 행사 정각 삭제를 확인할 수 없다. 애플리케이션의 복원 차단, 타이머의 버전별 삭제와 재열거, 행사 후 별도 전수 확인을 각각 인수한다.

### 정기 실행과 경보

[백업 타이머](../deploy/linux/maison-prod-backup.timer)는 15분마다, 재부팅 후에도 실행한다. KST 날짜별 한 회의 SQLite 온라인 스냅샷을 CMS로 암호화한다. 전송 전에 원래 생성 시각과 예약을 기록한다. 전송 시 동일 객체 재시도는 버전과 체크섬을 확인한다. 버전 ID·크기·SHA-256을 확인한 뒤 수신 영수증을 별도 저장한다. 전송 지연 중 14일이나 행사 마감을 넘긴 사본은 정상 복원용으로 전달하지 않는다. [백업·전송 작업](../deploy/linux/prod_object_backup.py) · [백업 생성](../deploy/linux/prod_backup.py)

- `prod_object_backup.py run`이 로컬 만료, 생성, 전달, 외부 재조회와 증거 출력을 담당한다. `health`는 저장소와 전송 상태를 대조한다. 최근 정상 수신의 원본 생성 시각이 26시간 이상 지났거나 영수증이 없으면 평상시 검사가 실패한다. 작업 파일이나 기록 분실 시 운영자가 서버 밖 수신·삭제 증거와 인벤토리를 대조하기 전 임의로 빈 기록으로 바꾸지 않는다.
- 검증된 주기만 CloudWatch의 `Maison/ProductionBackup` 네임스페이스, `VerifiedDeliveryAndExpiry` 지표, `Event=<행사 ID>` 차원에 1을 보낸다. 외부에 **missing data = breaching** 경보와 담당자 통보 채널을 별도로 준비한다. 설정 예시는 period 900초, 통계 Sum, 임계값 1 미만, 최근 3구간 중 3구간 위반, 누락값 breaching이다. 알림 대상을 연결한 다음 서버 정지·자격 만료·전송 불가를 각각 시연해 실제 통보를 받아야 한다. 서버 자체의 성공 로그는 서버가 꺼졌을 때 경보가 도착했음을 증명하지 않는다.
- 생성과 서버 내 만료는 저장소 접속 전에 처리한다. 회선 단절 동안 외부 삭제·미수신 확인은 지연된다. 오래된 버전·삭제 마커까지 삭제한 뒤 다시 열거해야 외부 삭제 영수증을 만든다. 잠금 정책, 미완료 멀티파트, 공유 스냅샷, 다른 리전으로의 복제 등 확인할 수 없는 사본은 미결로 남긴다. 연체분과 적체 용량을 운영자가 확인한다. 공유 DEMO 이미지나 스냅샷을 자동 삭제하지 않는다.
- 증거 버킷에 수신 `received/`, 삭제 `expired/`, 마감 후 접두사·지역 스냅샷 확인 `reconciled/`, 승인 원본 `policy/`, 개인 데이터 없는 상태 `lifecycle/`를 구분한다. 마지막 두 자료를 독립 보관하고 승인 담당자가 최신 상태와 대조한다. 개별 S3 접두사가 비었다는 증거와 모든 보관처의 파기 완료는 별도로 판정한다.

### 수동 복원과 종료 후 조사

1. 승인 책임자가 외부 수신 원본·실제 버전·암호문 지문과 마지막 생명주기 증거·정책·삭제 영수증을 독립적으로 대조한다. 승인 주체와 복원 장치 책임자를 정한다. 감사자료 처분 권한, 재발견 사본, 미결 보관처에는 별도 판단이 필요하다.
2. FileVault Mac에 방금 승인된 행사와 격리 목적지를 준비한다. 복원자와 별도로 관리하는 서명키의 Ed25519 공개키 DER를 `/etc/maison-restore/approver.der`에 설치한다. 관리자 소유이며 일반 운영자가 교체할 수 없어야 한다. 백업 복호화 키와 승인 서명키의 용도를 분리한다. 신규 복원대장의 첫 설정은 `prod_restore_approval.py init-register --register <새 경로> --lifecycle-policy <승인본> --policy-sha256 <별도 지문>`이다. 발급된 32자리 `register_id`를 서명 요청에 포함해 저장소를 새로 만든 뒤 승인을 재사용하는 경우를 차단한다. 대장을 보존하고 `MAISON_PROD_RESTORE_REGISTER`와 Mac 전용 보관 루트를 설정한다. 기존 승인 대장을 초기화해 복원 이력을 복구하지 않는다. [복원 승인 상세](../deploy/linux/prod_restore_approval.py)
3. 승인자는 외부 자료의 실제 바이트, 암호문·수신 인증서·행사 설정, 인스턴스·목적 볼륨 및 현재 처분 상태를 대조한다. `prod-restore-decision-v1` JSON 하나에 한 회의 복원 요청을 연결하고 최대 1시간 유효한 분리 서명을 발급한다. 결정 필드는 `format, request_id, register_id, event_id, instance_id, access_until, receipt_sha256, lifecycle_sha256, event_sha256, cert_sha256, destination_id, issued_at, valid_until, copy_status, disposition`이다. 목적지 ID는 볼륨 ID와 파일명 SHA-256 앞 32자리를 연결한다. 실제 수신·생명주기 JSON 파일은 해시를 내기 전에 재직렬화하지 않는다. 원본 생성 후의 생명주기 증거를 사용하고 보관처 책임자가 더 최근의 삭제·미해결 기록이 없는지 확인한다. 서명 예시는 `openssl pkeyutl -sign -rawin -inkey <승인자의 암호화된 키> -in <결정 JSON> -out <서명>`이다. 암호는 인수에 넣지 않는다.
4. `prod_backup.py restore --help`에서 공통 인자와 `--approval`, `--signature`, `--receipt`, `--lifecycle-evidence`, `--lifecycle-policy`, `--archive`, `--sha256`, `--identity`, `--destination`을 확인한다. 미승인·재사용·누락·불일치는 복호화를 거부한다. 종료 후에는 아직 미처분으로 등록된 사본과 마감 이후의 증거, 감사 처분 승인 및 훈련 근거를 갖춘 경우에만 격리 재파기가 가능하다. 후보에서 직원 자격을 폐기하고, 행사·키·감사·SQLite 무결성을 검사한다. 운영 경로로 승격하거나 공개 서비스를 기동하는 명령은 아니다.
5. 조사가 끝나면 임시 자료와 부수 사본을 처리한다. Mac에서 `prod_restore_approval.py expire-register --config <행사> --instance <식별값> --receipts <영수증 경로>`를 실행해 확인하고, 그 증거를 외부 책임자가 보관한다. 꺼진 Mac의 잔여 파일은 정각에 원격 삭제할 수 없다. 일상 백업은 Mac에 다운로드하지 않고, 수동 복원 후에는 일반 백업·동기화·휴지통을 포함한 잔여 사본을 조사한다. 종료 후 전체 보관처를 대조할 때 `prod_custody.py attest`와 `reconcile`은 검증한 외부 영수증과 지문을 요구한다. [인벤토리 확인 명령](../deploy/linux/prod_custody.py)

운영 종료 시 원본 파기 시각, 모든 보관처의 만료, 외부 증거 수신 및 미결 건 0을 대조한다. 담당자 인수 후에 백업 타이머·미수신 경보를 함께 종료하고 전송 자격을 회수한다. 그 전까지는 재시도와 경보를 유지한다.

### 비용과 운영 인수

별도 원장 서버의 월 고정비는 없다. 서울 S3 Standard의 첫 구간 예시는 저장 $0.025/GB·월, PUT/LIST 계열 $0.0045/1,000건, GET 계열 $0.00035/1,000건이다. 일일 암호문 10 MiB × 14개라면 안정기에 약 0.137 GiB다. 예시 요청량을 월 쓰기·목록 10만 건, 읽기 2만 건으로 두면 저장·요청 소계는 약 US$0.46이다. 같은 리전의 일반 사용자 지표 1개($0.30), 표준 경보 1개($0.10), 30일간 15분별 지표 API 2,880회($0.01/1,000회)를 더하면 **월 약 US$0.89**다. 무료 구간 차감 전 계산이며, 버전·영수증 누적, 통보·데이터 전송·세금과 환율은 포함하지 않았다. 실물 버킷·경보·IAM·삭제·복원 훈련 결과와 담당자 서명은 공개 전 별도 인수 항목이다. [서울 S3 공식 단가표](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/ap-northeast-2/index.json) · [서울 CloudWatch 공식 단가표](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonCloudWatch/current/ap-northeast-2/index.json)
