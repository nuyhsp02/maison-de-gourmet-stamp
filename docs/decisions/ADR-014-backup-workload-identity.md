# ADR-014 · 무인 백업의 AWS 신원과 권한 분리

2026-11-26 사용자는 기존 Lightsail 한 대에서 IAM Roles Anywhere로 무인 백업을 인증하기로 결정했다. 웹과 백업은 서로 다른 Linux 계정을 쓰고, 서버에는 장기 AWS Access Key를 두지 않는다. 외부 파기 원장이나 상시 가동 Mac 없이 백업하는 기존 구조를 따른다. 적용 대상은 PROD 후보이며 실제 클라우드 권한과 수신·복원은 운영 인수 단계에서 확인한다. [ADR-013](ADR-013-single-server-privacy.md) · [구현 및 검증](../devlog/DEV-022-backup-workload-identity.md)

## 누가 어떤 자격을 갖는가

| 주체 | 자격과 권한 | 선택 이유 |
|---|---|---|
| 웹과 로컬 원본 작업 | 웹은 `maison-prod`를 유지한다. 같은 UID로 실행하는 별도 원본 작업이 일관된 SQLite 스냅샷을 CMS로 암호화한다. 전용 UNIX 소켓으로 암호문과 보관 증거를 전달한다. | 고객 DB·프로필 키의 기존 소유자 보호를 유지하면서 웹 프로세스에서 AWS 키·프로필·전송 장부에 접근하는 경로를 닫는다. |
| 백업 전송 | `maison-backup`이 전용 클라이언트 인증서와 개인키를 사용한다. AWS CLI가 `credential_process`로 Signing Helper를 호출해 900초 목표의 임시 자격을 받는다. | 사람의 콘솔 세션을 서버에 복사하거나 장기 Access Key를 회전하는 방식은 무인 운영 또는 유출 대응에 맞지 않는다. 서버가 자체 요청에 서명하므로 Mac 전원과 무관하다. |
| CA 관리 | 사용자가 발급·폐기를 책임진다. CA 개인키는 지정 FileVault Mac에 별도로 암호화하고, 발급대장·폐기 상태와 함께 별도 암호화 오프라인 복구 사본을 보관한다. AWS에는 CA 공개 인증서와 서명한 CRL을 제시한다. | 서버용 클라이언트 키가 유출돼도 새 호스트 인증서를 발급할 권한은 분리된다. AWS Private CA 고정비를 피하는 대신 사용자가 교체 일정과 복구 훈련을 관리한다. |
| 수동 복원 | 지정 Mac의 담당자는 MFA를 강제하는 전용 사람 신원으로 읽기 전용 역할을 맡는다. 운영 정책이 승인한 암호문 한 개의 객체 키·버전·UTC 기한만 허용하며, 증거는 지정 버전을 읽을 수 있다. 데이터 삭제와 증거 수정·삭제 권한을 부여하지 않는다. | 복원 권한, 서버의 백업 전송 권한, 복호화 키, 1회 복원 승인 서명을 분리한다. 사람의 점검용 `AccountFullAccessRole`을 복원에 쓰지 않는다. root 장기 Access Key도 금지한다. |

클라이언트 키는 무인 서명에 쓸 수 있는 자격이므로 탈취되면 짧은 AWS 세션을 반복 발급할 수 있다. 서버 root와 전송 계정 침해는 남은 위험이다. 백업·연락처 복호화 개인키는 서버 밖에 둔다. 서명 도우미의 표준 출력에는 임시 자격이 포함되므로 CLI 프로세스만 이를 받고 운영 로그·파일에 저장하지 않는다. [전송 유닛](../../deploy/linux/maison-prod-backup.service) · [원본 작업](../../deploy/linux/prod_backup_source.py) · [AWS 서명 도우미](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/credential-helper.html)

## 인증서 1년과 AWS 세션 15분

- **클라이언트 인증서**는 1년 유효, 만료 30일 전 교체를 목표로 한다. 초기 검토의 90일 제안은 사용자의 이번 결정으로 대체한다. 자체 CA가 서버에서 생성한 새 키의 CSR과 승인한 호스트 신원을 확인해 서명한다. CA 공개 인증서를 고정하고, CA는 X.509 `CA:TRUE`와 인증서·CRL 서명 용도, 리프는 X.509 v3 `CA:FALSE`와 디지털 서명 용도, 서명 알고리즘은 SHA-256 이상을 사용한다. 리프의 유효기간을 발급 CA의 잔여기간 안에 둔다. [AWS 인증서 요건](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/trust-model.html)
- **1년은 프로젝트의 발급 기준**이다. 외부 CA가 리프를 발급하며, 공개된 AWS 신뢰 모델과 CreateSession 사양에는 리프 수명을 지정하는 API 인자나 365일을 배제하는 숫자 상한이 명시돼 있지 않다. 신뢰 체인·유효 시각·서명·폐기는 각각 검증한다. CA 만료나 실제 계정의 발급 정책이 더 짧으면 남은 유효기간으로 단축하거나 CA를 먼저 교체한 뒤 시험한다. AWS는 유출 영향을 줄이기 위해 짧은 수명의 인증서를 권장한다. [외부 CA 신뢰](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/public-key-infrastructure.html) · [AWS의 PKI 운영 권고](https://aws.amazon.com/blogs/security/best-practices-for-integrating-iam-roles-anywhere-with-your-pki/)
- **AWS 세션**은 Profile과 CreateSession 요청을 모두 900초로 설정한다. 두 API의 공개 범위는 900~43,200초다. 유효 수명은 프로필과 요청 중 작은 값이며 역할의 최대 세션 시간을 넘으면 오류가 난다. IAM 역할 자체의 `MaxSessionDuration`은 최소 3,600초이므로 15분 제한을 프로필과 서명 요청에 둔다. 실제 발급 시각과 응답의 만료 시각을 인수에서 대조한다. [CreateProfile 제약](https://docs.aws.amazon.com/rolesanywhere/latest/APIReference/API_CreateProfile.html) · [CreateSession 만료 계산](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/authentication-create-session.html) · [IAM 역할의 설정 범위](https://docs.aws.amazon.com/IAM/latest/APIReference/API_CreateRole.html)

## 최소 권한과 보관 비용

Trust Anchor·AWS 계정·리프 Subject CN·Issuer CN을 역할 신뢰 조건으로 묶고, 프로필에는 전용 백업 역할 하나를 허용한다. 전송기는 저장소에 접근하기 전에 STS로 승인 계정과 역할을 확인한다. 데이터 버킷에서는 행사·인스턴스 접두사 안의 버전 목록, 조건부 최초 업로드, 버전별 무결성 확인과 특정 버전 삭제를 허용한다. 증거 버킷에는 정해진 증거 경로의 조건부 쓰기·확인만 주고 삭제를 막는다. 버전별 HEAD에 필요한 읽기 권한은 암호문 본문 읽기에도 쓰일 수 있으므로 복호화 키는 외부에 둔다. IAM은 각 객체의 보유 기한을 계산하지 않으므로 조기 삭제 방지는 코드의 시각 검사와 외부 증거 대조에도 의존한다. [저장소 호출](../../deploy/linux/prod_s3.py) · [전송 및 만료](../../deploy/linux/prod_object_backup.py)

공유 서버 스냅샷은 서울 리전에서 읽기로 조사한다. 백업 작업은 `Maison/ProductionBackup` 네임스페이스에 성공 지표를 쓰고, 15분 구간 3회 누락을 CloudWatch 경보로 감시한다. 지표명과 행사 차원은 코드와 경보에서 고정한다. SNS의 장애·복구 알림 수신자는 구독을 확인하고 실제 통보를 받아 인수한다. 외부 통신이나 서명이 실패하면 타이머가 재시도하며 고객 요청은 백업 결과를 기다리지 않는다. 기존 서울 사용량 가정에서 S3·CloudWatch 월 추가 추산은 세전 약 US$0.89다. 자체 CA를 쓰는 Roles Anywhere 이용료는 추가되지 않는다. 인증서 관리 시간, 실제 요청 수와 추가 알림·전송은 별도다. [만료·비용 가정](../PROD_BACKUP_EXPIRY.md) · [Roles Anywhere 요금 안내](https://aws.amazon.com/about-aws/whats-new/2023/12/iam-roles-anywhere-additional-aws-regions/)

진행 중인 멀티파트 업로드 목록은 전용 데이터 버킷 전체에서 열거할 수 있다. 앱은 요청에 행사·인스턴스 접두사를 보내지만 `s3:ListBucketMultipartUploads`에는 IAM의 `s3:prefix` 조건이 적용되지 않는다. [S3 작업별 조건 키](https://docs.aws.amazon.com/service-authorization/latest/reference/list_s3.html)

역할 신뢰의 `aws:SourceArn`은 Trust Anchor를 가리킨다. 다른 프로필에 같은 역할을 등록해 세션 수명을 늘릴 수 있는 관리 권한도 별도로 통제해야 한다. Mac 복원 역할의 신뢰는 허가된 사람 신원을 식별한다. 지정 Mac에서 사용한다는 절차를 강제할 하드웨어 검증까지 제공하지는 않는다. [역할 신뢰 조건](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/trust-model.html)

## 교체 및 사고 대응

1. 만료 30일 전 담당자가 새 키의 CSR을 확인하고 다른 일련번호의 인증서를 발급한다. 키와 인증서의 공개키 일치, 체인·유효기간을 확인하고 진행 중인 작업 종료 후 한 쌍으로 교체한다. 새 신원으로 세션과 백업 수신을 확인한 뒤 구 일련번호를 폐기한다.
2. 유출 시 신규 발급을 먼저 격리한다. 전용 Profile과 Trust Anchor를 비활성화하고 필요하면 역할 신뢰를 차단한다. CA 발급대장에서 해당 일련번호를 폐기하고 서명한 최신 CRL을 AWS에 갱신한다. 가져온 CRL의 `nextUpdate`가 지나도 AWS가 자동으로 새 목록을 가져오지 않으므로 등록·활성 상태와 일련번호 거부를 확인한다. [AWS 신뢰·폐기 검사](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/trust-model.html)
3. 이미 발급한 세션 권한은 **별도 회수**한다. IAM 콘솔의 세션 취소는 역할에 `AWSRevokeOlderSessions` 정책을 붙여 전파를 고려한 약 30초 뒤의 시각 이전에 발급된 세션들까지 차단한다. 수동 대응에서는 `aws:TokenIssueTime` 조건의 명시적 Deny로 발급 기준 시각을 지정한다. 구 리프의 새 세션 발급 거부와 **기존 세션의 S3 요청 거부**를 각각 확인한다. STS 신원 조회만으로는 S3 권한을 판정할 수 없다. 원래 세션이 모두 만료될 때까지 회수 정책을 유지한다. [IAM 역할 세션 회수](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_revoke-sessions.html) · [Roles Anywhere 세션 무효화](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/security_iam_troubleshoot.html)
4. 서버를 신뢰할 수 있는 상태로 복구하고 새 클라이언트 키를 사용한다. CA가 안전하면 갱신된 CRL이 연결된 앵커와 승인 프로필을 통제된 시험에서 다시 활성화한다. 새 인증서는 허용되고 구 인증서는 계속 거부되는지 확인한다. CA 키가 유출됐으면 구 앵커를 비활성 상태로 유지하고 새 CA·앵커로 전환한다. 새 신원의 권한, 미수신 경보와 복구 통지를 확인한 뒤 정기 작업을 재개한다.

## Google 로그인에서 Mac 복원 역할로

우선 현재 Google 로그인이 개인 AWS 가입용 로그인인지, 조직이 관리하는 Google Workspace의 연동인지, AWS 계정에서 어떤 주체로 들어오는지 확인한다. AWS의 새 가입 방식인 경우 AWS가 관리하는 조직 정책·지원 서비스 제한이 있고, 지원 목록에서 Roles Anywhere의 사용 가능성을 확인할 수 없다. 이 경우 계정의 기능 유형·조직 정책과 필요 시 고급 기능 전환의 영향을 검토해 별도 승인을 받는다. [AWS 가입 유형과 권한 차이](https://docs.aws.amazon.com/accounts/latest/reference/sign-up-for-aws.html) · [AWS 지원 서비스 목록](https://docs.aws.amazon.com/accounts/latest/reference/supported-services-sign-up-new.html)

- 관리형 Google Workspace와 IAM SAML 연동을 선택하면 복원 담당자만 전용 Google 앱을 사용할 수 있게 하고 IdP에서 MFA를 강제한다. AWS는 승인 IdP의 서명·대상과 역할 매핑을 검증한다. 지정 Mac에서 SAML로 전용 사람 역할의 임시 자격을 얻고, 이 역할에는 읽기 전용 복원 역할에 대한 `sts:AssumeRole`만 허용한다. 실제 토큰과 SAML 응답을 로그에 남기지 않는다. [Google의 AWS SAML 연동](https://knowledge.workspace.google.com/admin/apps/integrations/amazon-web-services-cloud-app) · [AWS SAML API](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithSAML.html)
- 조직용 IAM Identity Center가 구성돼 있고 사용자가 관리할 수 있다면, Google Workspace를 외부 IdP로 연결하고 지정 그룹에 한정된 권한 세트를 할당한다. Mac의 전용 SSO 프로필에서 해당 사람 역할로 인증한 뒤 복원 역할을 맡는다. 단일 계정용 Identity Center 인스턴스는 계정 권한 세트를 지원하지 않는다. [Workspace 연동 절차](https://docs.aws.amazon.com/singlesignon/latest/userguide/gs-gwp.html) · [계정 인스턴스의 제약](https://docs.aws.amazon.com/singlesignon/latest/userguide/account-instances-identity-center.html)
- 연동 사용자의 MFA는 IdP 정책과 실제 비MFA 로그인 거부로 확인한다. IAM에서 인증 방식을 조건으로 쓰려면 사용자가 위조할 수 없는 IdP 발행 속성을 매핑해야 한다. `aws:MultiFactorAuthPresent`만으로 연동 사용자의 MFA를 증명하지 않는다. 현재 방식이나 MFA를 증명할 수 없으면 복원 역할의 신뢰 등록을 보류한다. [AWS MFA 조건의 적용 범위](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html#condition-keys-multifactorauthpresent)

사용자는 보관 책임, 유효기간 및 신원 분리 원칙을 결정했다. Codex는 런타임 변경과 합성 검증을 수행했다. 실제 계정 기능·신뢰 등록·경보·복원 인수와 담당자의 훈련은 후속 작업이다.
