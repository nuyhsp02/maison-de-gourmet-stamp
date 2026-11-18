# Linux DEMO 백업과 격리 복원

시연 데이터 담당자가 시연 전·후와 복구 연습 전에 실행한다. 서비스 계정은 `maison-demo`, 원본은 `/var/lib/maison-demo/collection.sqlite`와 같은 위치의 `collection.sqlite.profile.key`다. DB에는 프로필, 고객 세션, 상품 QR 키 10개, 적립, 지급 및 취소 감사 기록이 있다. 복원할 때 같은 행사 설정과 같은 릴리스의 코드가 필요하다. 배포 시 행사 설정 원본과 코드 커밋을 보관자에게 별도로 전달한다.

## 준비

- 보관자는 시연 서버와 물리적으로 분리된 장치, 권한 0700의 보관 디렉터리, 디스크 암호화, 해당 장치로 연결할 SSH 계정을 준비한다. 복호화 키의 암호와 비상 복구 사본을 보관할 담당자를 정한다.
- 서버와 복원 장치 양쪽에 OpenSSL 3 CLI 및 이 릴리스의 Python 환경을 준비한다. 서비스에 전용 SQLite 라이브러리를 지정했다면 수동 실행 환경에도 같은 `LD_LIBRARY_PATH`를 적용하고 `tools/check_sqlite_runtime.py --require-fixed`를 확인한다. 백업·복원 중에는 DB 크기의 몇 배에 해당하는 비공개 임시 공간이 필요하다. 암호화된 파일시스템을 사용한다. 전원 차단 시 남은 `.demo-backup-*` 또는 `.verify-*`에는 평문 DB와 프로필 키가 있을 수 있다.
- Nginx 게이트 자격증명, TLS 키, 직원 비밀번호 검증 파일은 담당자가 별도로 관리한다. 복구 시 직원 비밀번호는 다시 설정할 수 있다. **프로필 키는 원래 바이트를 복구해야 한다.** 새로운 키를 만들면 기존 전화번호 검색 결과와 맞지 않는다.

보관 장치에서 최초 1회, 새 전용 디렉터리에서 실행한다. OpenSSL의 개인키 암호 입력창을 사용한다.

```sh
umask 077
mkdir -m 0700 maison-demo-recovery
cd maison-demo-recovery
openssl req -x509 -newkey rsa:3072 -days 365 -subj '/CN=Maison DEMO backup recipient' \
  -keyout recovery-private.pem -out recovery-public.pem
chmod 0600 recovery-private.pem
openssl x509 -in recovery-public.pem -noout -fingerprint -sha256
```

공개 인증서 `recovery-public.pem`을 시연 서버의 `/etc/maison-demo/backup-recipient.pem`에 root 소유 0644로 설치한다. 보관자와 서버 담당자가 SHA-256 지문을 대조한다. 개인키는 보관 장치에 둔다. 새 수신 인증서로 교체한 뒤에도 이전 암호문 보존 기간 동안 해당 개인키가 필요하다. 암호화 방식과 키 전송 옵션은 OpenSSL 3의 CMS 명령을 사용한다. [OpenSSL CMS 문서](https://docs.openssl.org/3.0/man1/openssl-cms/)

## 실행 중 스냅샷과 서버 외 보관

시연 서버의 담당자 셸에서 실행한다. 최종 이름은 매번 새로 만든다. 아래 `demo-vault`는 보관자가 사전에 SSH 호스트 키를 확인해 등록한 별칭이다. 수신 계정의 홈에 0700 `incoming` 디렉터리를 미리 준비한다.

```sh
set -eu
umask 077
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 6)"
sudo install -d -m 0700 -o maison-demo -g maison-demo /var/backups/maison-demo
sudo -u maison-demo /opt/maison-demo/.venv/bin/python /opt/maison-demo/deploy/linux/backup.py create \
  --state-dir /var/lib/maison-demo --event /etc/maison-demo/event.json \
  --recipient-cert /etc/maison-demo/backup-recipient.pem --output "/var/backups/maison-demo/$run_id.cms"

transfer_dir="$(mktemp -d)"
sudo -u maison-demo cat "/var/backups/maison-demo/$run_id.cms" > "$transfer_dir/snapshot.cms"
(cd "$transfer_dir" && sha256sum snapshot.cms > SHA256SUMS)
ssh -o StrictHostKeyChecking=yes demo-vault "umask 077 && mkdir 'incoming/$run_id'"
scp -p -o StrictHostKeyChecking=yes "$transfer_dir/snapshot.cms" "$transfer_dir/SHA256SUMS" \
  "demo-vault:incoming/$run_id/"
ssh -o StrictHostKeyChecking=yes demo-vault "cd 'incoming/$run_id' && sha256sum -c SHA256SUMS"
```

`create`는 DEMO 표시, 행사 ID와 상품 목록, PR2 스키마와 원래 QR 집합에 연결된 HMAC을 확인한다. 기존 `scripts/ops.py`의 SQLite 온라인 백업 API가 읽기 전용 연결에서 커밋된 내용의 스냅샷을 만든다. 스냅샷에 대해 전체 무결성·외래키와 프로필 키를 다시 검사한다. DB와 키 두 파일만 ZIP에 넣고, AES-256-GCM과 수신자의 RSA-OAEP/SHA-256으로 암호화한 단일 CMS 파일을 0600으로 게시한다. 전송 디렉터리에는 암호문과 그 해시만 남는다.

담당자는 생성 시각, 코드 커밋, 행사 설정 버전, 반환된 건수와 서버에서 계산한 암호문 SHA-256을 독립된 운영 기록에 남긴다. 보관자는 수신본의 해시를 그 기록과 대조하고 복원 연습 결과를 남긴다. 수신지 검증 실패 시 서버의 원본·암호문과 마지막 성공 백업을 보존하고 새 회차로 다시 시도한다. 회수·폐기일과 백업 간격은 시연 담당자가 정한다. 보관자 계정은 수신 완료본을 발신 계정에서 수정할 수 없는 위치에 보관한다.

## 보관 장치에서 복원 연습

보관 담당자가 같은 코드와 Python 환경, 합의한 행사 설정, 백업 시 선택한 인증서·개인키를 준비한다. 아래 예시는 이 릴리스가 `/opt/maison-demo`에 설치되어 있고, 현재 디렉터리는 위의 `maison-demo-recovery`라고 가정한다. `run_id`는 수신한 회차명으로 지정한다.

```sh
set -eu
umask 077
(cd "$HOME/incoming/$run_id" && sha256sum -c SHA256SUMS)
mkdir -p -m 0700 drills
/opt/maison-demo/.venv/bin/python /opt/maison-demo/deploy/linux/backup.py restore \
  --archive "$HOME/incoming/$run_id/snapshot.cms" --event ./event.json \
  --recipient-cert ./recovery-public.pem --identity ./recovery-private.pem \
  --destination "./drills/$run_id"
```

종료 코드 0과 `status=ok`를 확인하고, 생성 시 집계한 상품·카드·프로필·적립·수령 감사·취소 감사 건수를 각각 대조한다. 복원 도구는 새 전용 디렉터리에 지정한 두 파일만 만들고 DB 무결성, 외래키, 행사/QR 집합, PR2 스키마, 프로필 키를 확인한 뒤 후보 DB를 서비스용 WAL 모드로 전환한다. 이전 복원 후보 및 현재 서비스 경로에는 덮어쓸 수 없다. 중단된 후보는 격리하고 다른 새 이름으로 복구한다. 검증 로그에는 키·QR·계정·프로필 행을 출력할 필요가 없다.

휴대폰 시연을 재개해야 한다면 보관자가 **검증한 후보의 닫힌 DB와 프로필 키**를 승인된 SSH 채널로 DEMO 호스트의 새 0700 후보 디렉터리에 전달한다. 행사 설정과 앱 커밋을 맞추고 파일 소유자를 `maison-demo:maison-demo`, 권한을 0600으로 설정한다. 아래 명령에서 `candidate`에는 새 DEMO 후보 디렉터리 절대 경로, `demo_actor`에는 시연 직원 계정 ID, `demo_origin`에는 승인된 HTTPS 원본을 지정한다. 직원 비밀번호는 담당자가 대화형으로 다시 설정한다.

```sh
sudo -u maison-demo /opt/maison-demo/.venv/bin/python /opt/maison-demo/app/manage.py \
  --database "$candidate/collection.sqlite" admin-password --actor "$demo_actor"
sudo -u maison-demo /opt/maison-demo/.venv/bin/python /opt/maison-demo/deploy/linux/demo.py check \
  --state-dir "$candidate" --config /etc/maison-demo/event.json --origin "$demo_origin"
```

담당자는 서비스 중지 후 기존 상태 디렉터리 전체(DB, WAL/SHM, 키 포함)를 새 보존 이름으로 옮기고, 같은 파일시스템에서 검증된 후보 디렉터리를 서비스 경로로 옮긴다. 새로 만든 기기에서는 빈 서비스 경로를 확인한다. 기존 DEMO 접속 게이트·TLS 자격증명을 준비하고 서비스를 시작한다. 두 기기에서 합성 고객의 찾기·스탬프 상태와 직원 로그인을 점검한다. 백업 후 발생한 적립·취소·보상은 해당 회차에 없으므로 시연 기록과 대조한 뒤 재개한다. 점검 실패 시 서비스를 중지하고 복구 후보를 별도 이름으로 보존한 후 이전 상태 디렉터리로 되돌린다.
