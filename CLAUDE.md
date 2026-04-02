# Coupang Price Manager — Claude Code 업무 매뉴얼

## 프로젝트 개요
- Cloud Run 서비스 (Python)
- 배포: GitHub push 후 대표님이 deploy-update.bat 수동 실행
- Claude Code가 직접 배포하지 않음

## 핵심 규칙
- 쿠팡 직접 스크래핑 금지 → Scrape.do API (super=true)
- 동시성: timestamp 기반 가드
- .bat 파일: 영문만 (한글 절대 금지)
- API 호출 코드는 실제 테스트 후에만 커밋
- 고정 IP: 34.22.68.152 (Coupang WING 등록)

## 배포
1. 코드 수정 & 테스트
2. git commit & push
3. 대표님에게 "deploy-update.bat 실행해주세요" 알림
