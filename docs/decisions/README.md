# 기술적 의사결정 기록 (ADR)

기존 V1.4.0 구현을 근거로 정리한 **사후 기록**입니다. ADR 문서가 당시 존재했거나 별도 회의에서 승인됐다는 뜻은 아닙니다. 공통 기준 커밋은 [1284c52](https://github.com/nuyhsp02/maison-de-gourmet-stamp/commit/1284c52e04e5e323af4a7fb182d78a2b27a01b8f)이며, 최초 결정일은 확인되지 않았습니다.

| ADR | 핵심 선택 | 확인 한계 |
|---|---|---|
| [001 · SQLite와 영속성](ADR-001-sqlite-persistence.md) | SQLite/WAL·고유 제약·코드/DB 분리 | 실제 장비의 처리량/한 달 운영 검증 아님 |
| [002 · 익명 고객 식별](ADR-002-anonymous-identity.md) | 보안 쿠키의 불투명 토큰 + 선택적 복구 링크 | 쿠키/브라우저 분리, 링크 소지 위험 |
| [003 · 고정 QR와 중복 방지](ADR-003-fixed-qr-and-deduplication.md) | 상품별 고정 키·GET 읽기 전용·DB 복합 기본키 | QR 소지는 구매 증명이 아님 |
| [004 · 웹 서버와 외부 접속](ADR-004-flask-waitress-tunnel.md) | Flask + loopback Waitress + HTTPS Tunnel | 실제 도메인/터널 연결 증거 없음 |
| [005 · Windows와 백업](ADR-005-windows-and-backup.md) | 작업 스케줄러·감독·SQLite 백업·별도 복구 검증 | 현장 부팅/권한/장애 복구 미확인 |
| [006 · 인증 없는 지급 완료](ADR-006-unauthenticated-redemption.md) | 실물 지급 후 버튼 1회·감사 기록·경고 | 고객 임의 조작/실물 지급 진위 검증 불가 |

'대안' 절은 당시 검토가 확인된 범위와 사후 비교 후보를 구분합니다. 후보를 나열한 사실만으로 실제 비교 실험을 했다고 해석하지 않습니다. 상태는 **구현 확인**이며 운영 검증 완료나 별도 위험 승인 서명을 뜻하지 않습니다.

작성/수정 원칙은 [포트폴리오 기록 원칙](../PORTFOLIO_RECORDS.md), 문제 해결 과정은 [개발 로그](../devlog/README.md)를 참고하세요. 이후 선택을 바꾸면 새 번호를 사용하고 원래 문서의 배경을 보존합니다.
