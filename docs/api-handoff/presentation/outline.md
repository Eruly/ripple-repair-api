# RippleRepair API 소개자료 — 5장 초안

대상: 재무 리서치·플랫폼 담당자와 API 연동 개발자  
목적: Markdown 한 번의 호출로 영업이익 숫자와 재인용 결론을 함께 교정하는 흐름 설명

## Slide 1: Markdown을 넣으면 교정 Markdown이 나온다

- 입력: 컨센서스와 전망이 포함된 재무 리서치 Markdown
- 데모: `/forecast-correction`에서 파일 선택 또는 본문 붙여넣기
- 호출: `POST /api/forecasts/operating-profit/correct`
- 출력: `corrected_markdown` + 모든 교정 근거 + 수동 검토 목록
- 핵심 메시지: 별도 consensus 파라미터 없이 문서 안에서 추출 가능
- Visual: `markdown_text → /correct → corrected_markdown` 한 줄

## Slide 2: 왜 필요한가 — 자릿수 하나가 결론까지 번진다

- 예: 실제 기대치 `35조원`, 보고서 중간 전망 `3,500조원`
- 상·중·하 시나리오뿐 아니라 요약과 투자 판단도 같은 오류를 재인용
- 숫자 한 곳만 수정하면 문서 내부 모순이 남음
- Visual: 잘못된 숫자에서 시나리오·결론으로 퍼지는 ripple

## Slide 3: 숫자 교정 — 추출과 불변식을 함께 쓴다

- 최신 날짜의 영업이익 consensus를 표/dataframe에서 자동 추출
- 명시 대상 연도 우선, 원·억원·조원 환산 및 이중 열 교차검증
- 상·중·하 각 셀을 개별 판정: 지원 배율(최근접), 부호, 순서 검사
- 연간을 consensus로 고정한 뒤 `상반기 + 하반기 = 연간` ≤2% 재검증
- 음수 영업손실과 mixed-cell 오류 지원
- Visual: 추출된 35조 → 상·중 셀만 0.1배 → 하방 유지

## Slide 4: FactReasoner pin → cascade → 안전한 병합

- 확정된 old→new scale 변경을 원본 graph의 atom에 pin
- 같은 금액을 다른 단위로 쓴 결론은 제한적으로 즉시 교정
- 연결된 상·하류 문장은 FactReasoner cascade로 탐색
- 동일 금액 치환 + 유일 anchor + Markdown 구조 보존을 모두 통과한 결과만 자동 병합
- 새 계산값·정성 재작성·충돌은 `manual_review`로 보존
- Visual: `3,500조 atom → 35조 pin → 결론 35조`

## Slide 5: 연동·운영 체크리스트

- 데모 순서: 파일 선택 → consensus 자동 추출 → 전후/검토 확인 → 교정 Markdown 다운로드
- 권장: `/correct` 한 번, 기본 `graph_mode=llm`
- 숫자만: `/scale-correction`
- 고급 검토형: `/correction-preview` 또는 graph → judge → propagate
- 확인 필드: `corrected_markdown`, `applied_corrections`, `manual_review_required`, `error`
- 서버는 파일을 쓰지 않으며 호출자가 승인 후 저장
- 외부 배포: TLS, API gateway 인증, 보고서 본문 로그 마스킹, 감사 로그
- Visual: 세 가지 API 경로와 마지막 사람 승인 gate
