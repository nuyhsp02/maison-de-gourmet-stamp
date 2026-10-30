# ADR-005 · Windows 노트북 서버 및 백업 설계

- 상태: 기존 운영 패키지/자동 테스트 확인 / 사후 기록. 최초 결정일: **미확인**.
- 기준: [V1.4.0](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f), [설치 설명서](../WINDOWS_INSTALL.md).

## 1. 해결하려던 문제

직원이 노트북 전원을 켜는 운영을 목표로 설치·시작·재시도·상태 확인을 준비해야 합니다. 프로그램 업데이트와 고객 DB/실제 설정을 분리하고 장애나 실수에 대비해 안전하게 백업/복구해야 합니다.

## 2. 고려한 대안과 기록의 한계

작업 스케줄러, PowerShell, 기존 스택 유지 조건은 확인됩니다. Python 서버를 별도 Windows 서비스로 감싸는 방식, 매번 수동 실행, 다른 OS/원격 서버 운영을 당시 비교한 기록은 없습니다. 백업도 단순 파일 복사와 다른 저장 방식을 실제 비교 실험했다고 주장하지 않습니다.

## 3. 최종 선택

SYSTEM 예약 작업·Waitress 감독 프로세스로 부팅 시작/재시도를 준비합니다. app/config/data/backups/logs/전용 가상환경을 분리합니다. 매일 SQLite 백업 API로 검증된 새 사본을 만들고 일일 14일·주간 4주 보존 정책을 적용합니다. 외부 경로를 선택할 수 있으며 복원은 별도 후보 DB 검사부터 진행합니다.

## 4. 기술적 선택 근거

업데이트와 기록 보존을 분리하고, DB/WAL을 단순 복사하는 대신 DB API로 사본을 만듭니다. 기존 DB와 다른 이벤트 조합은 거부합니다. 실제 복원 활성화는 확인·서버 중지·현재 DB 백업/원본 분리를 거치게 구성합니다. 세부 운영 명령은 [백업/복원 문서](../BACKUP_RECOVERY.md)를 단일 안내로 사용합니다.

## 5. 단점과 위험

예약/서비스/SYSTEM 권한과 NTFS ACL의 현장 동작은 별도 검증이 필요합니다. 절전·전원/네트워크 중단·보안 업데이트 상태도 운영에 영향을 줍니다. 같은 디스크의 백업은 디스크 고장 대비가 부족하고 외부 경로도 분리/권한/공간 문제로 실패할 수 있습니다. 과거 DB 복원은 백업 이후 실제 지급과 불일치를 만들 수 있습니다.

## 6. 실제 코드 및 테스트 근거

- [Register-Autostart.ps1](../../scripts/Register-Autostart.ps1), [Run-Server.ps1](../../scripts/Run-Server.ps1), [Common.ps1](../../scripts/Common.ps1): 예약/재시도/중복/권한 구성.
- [ops.py](../../scripts/ops.py): `snapshot`, `backup`, `prune`, `stage_restore`, `activate_restore`.
- [test_windows_ops.py](../../tests/test_windows_ops.py): `test_safe_snapshot_live_data_and_existing_target_refused`, `test_retention_exact_boundaries_and_iso_year`, `test_external_failure_keeps_local_and_no_prune`, `test_disk_full_does_not_delete_old_backups`, `test_restore_publish_failure_rolls_back_original`.
- [백업 개발 로그](../devlog/DEV-004-windows-package-and-backups.md): 자동 검사와 실제 예약 작업/장비 검증을 구분.

## 7. 향후 개선 가능성

실제 Windows 10에서 재부팅·로그인 전 네트워크·작업 재시도·외부 매체 백업/별도 복원을 확인합니다. 복구 소요 시간과 허용 데이터 손실 범위를 운영자가 정하고 측정할 수 있습니다. 실제 14일/4주 자동 백업 운영이나 복구 목표 달성 실적은 아직 기록하지 않습니다.
