# DEV-006 · Windows 동시성 조사 이력 연결

조사 원본과 실패를 포함한 A/B 측정은 미병합 [PR #3](https://github.com/nuyhsp02/maison-de-gourmet-stamp/pull/3)에 보존합니다. 이 main 기반 구현 브랜치에 진단 코드 전체를 복사하지 않습니다.

- [조사 원본의 고정 커밋 cec6aef](https://github.com/nuyhsp02/maison-de-gourmet-stamp/blob/cec6aef3c0b0cca6eed02538d0ae7590ef1f35eb/docs/devlog/DEV-006-windows-concurrency-investigation.md)
- [운영 코드 B 구현 후보와 검증: DEV-007](DEV-007-transaction-write-scope.md)

A 실패에서 COMMIT/close가 보유 합계의 95.2%였고 동일 runner 비교는 A 7/8, 실험 B 8/8 완주였습니다. 이는 SQLite 내부 syscall/OS 원인을 확정하거나 현장 안정성을 보증한 결과가 아닙니다. PR #3의 원본 파일·커밋·실행·Draft 상태를 변경하지 않습니다.
