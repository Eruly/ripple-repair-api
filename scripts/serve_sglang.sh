#!/usr/bin/env bash
# 로컬 SGLang 서버 (Qwen3.8-27B NVFP4). 사례 보고서 LLM 대조·베이스라인용.
# 사용: bash scripts/serve_sglang.sh   (로그: $HOME/qwen38-nvfp4-sglang/logs/sglang.serve.log)
#   SPEC=1 이면 DFlash2 초안 모델로 추측 디코딩(메모리 +3.4GB). 기본은 끔(32GB 카드에서 상태 캐시 확보).
set -euo pipefail
SGLANG_ROOT="${SGLANG_ROOT:-$HOME/qwen38-nvfp4-sglang}"
VENV="$SGLANG_ROOT/.venv"
TARGET="${TARGET_PATH:-dfischermittwald/Qwen3.8-27B-NVFP4-DFlash2}"
DRAFT="${DRAFT_PATH:-incoai/Qwen3.8-27B-DFlash2}"
CONTEXT_LENGTH="${CONTEXT_LENGTH:-16384}"
MAX_RUNNING="${MAX_RUNNING:-1}"
SPEC="${SPEC:-0}"
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-12.8}"
export PATH="$VENV/bin:$CUDA_HOME/bin:${PATH:-}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-1}"
export FLASHINFER_CUDA_ARCH_LIST="${FLASHINFER_CUDA_ARCH_LIST:-12.0a}"
export TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-12.0}"
mkdir -p "$SGLANG_ROOT/logs"
SPEC_ARGS=()
if [ "$SPEC" = "1" ]; then
  SPEC_ARGS=(--speculative-algorithm DFLASH --speculative-draft-model-path "$DRAFT" --speculative-num-draft-tokens 8 --speculative-draft-kv-cache-dtype fp8_e4m3)
fi
exec "$VENV/bin/sglang" serve --trust-remote-code --model-path "$TARGET" --tp-size 1 \
  --kv-cache-dtype fp8_e4m3 --mem-fraction-static "${MEM_FRACTION:-0.92}" --context-length "$CONTEXT_LENGTH" --allow-auto-truncate \
  --attention-backend flashinfer --max-running-requests "$MAX_RUNNING" --cuda-graph-max-bs-decode 1 --cuda-graph-backend-prefill disabled \
  --reasoning-parser qwen3 --tool-call-parser qwen3_coder --mamba-full-memory-ratio "${MAMBA_RATIO:-10}" --host 0.0.0.0 --port 30000 \
  "${SPEC_ARGS[@]}" --chunked-prefill-size 1024 --mamba-radix-cache-strategy extra_buffer --mamba-ssm-dtype bfloat16
