# FactReasoner correction workflow

`BASE_URL=http://localhost:8200`을 기준으로 한 고급 검토형 3단계 예시입니다. 일반 연동은
`/api/forecasts/operating-profit/correct` 한 번이면 충분합니다. 아래 흐름은 atom 선택과
cascade 승인 범위를 호출자가 직접 제어할 때 사용합니다. Scale correction 응답의
`corrected_text`를 다음 단계의 `markdown_text`에 넣습니다.

## 1. Atom graph 만들기

```bash
curl -sS -X POST "$BASE_URL/api/fact-graph-preview" \
  -H 'Content-Type: application/json' \
  -d '{"markdown_text":"<SCALE_CORRECTED_TEXT>","graph_mode":"llm"}' \
  > graph.json
```

`graph.json`의 `fact_atom_graph.nodes[]`, `fact_atom_graph.edges[]`를 다음 요청에 그대로 전달합니다.
영업이익 전망 atom의 `id`를 `TARGET_NODE_ID`로 선택합니다.

## 2. 대상 atom 판정

```bash
curl -sS -X POST "$BASE_URL/api/fact-graph/judge-atom" \
  -H 'Content-Type: application/json' \
  -d '{
    "markdown_text":"<SCALE_CORRECTED_TEXT>",
    "nodes": <GRAPH_NODES>,
    "edges": <GRAPH_EDGES>,
    "target_node_id":"<TARGET_NODE_ID>"
  }' > atom-judgement.json
```

`changed: true`이고 `needs_review`가 없을 때만 `corrected_statement`를 다음 단계에 사용합니다.
검토자가 값을 확정했다면 그 문장을 직접 넣어 LLM 판정을 건너뛸 수 있습니다.

## 3. 영향 문장 전파

```bash
curl -sS -X POST "$BASE_URL/api/fact-graph/propagate-correction" \
  -H 'Content-Type: application/json' \
  -d '{
    "markdown_text":"<SCALE_CORRECTED_TEXT>",
    "nodes": <GRAPH_NODES>,
    "edges": <GRAPH_EDGES>,
    "target_node_id":"<TARGET_NODE_ID>",
    "corrected_statement":"<APPROVED_CORRECTED_STATEMENT>",
    "max_depth": 3
  }' > propagation.json
```

`target.suggested_quote`와 `propagations[].suggested_text`는 **제안**입니다. 각 항목의
`affected`, `needs_manual`, `reason`을 확인한 뒤 승인된 변경만 문서에 반영합니다.
