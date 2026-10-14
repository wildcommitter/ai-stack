#!/usr/bin/env bash
# E2E test runner: waits for the current agent response to finish (it is using
# the LLM), then exercises the full GPU handoff:
#   unload LLM -> ComfyUI generate (Qwen-Image-2.1) -> VLM describe -> free ComfyUI
set -u
LOG=/home/user/humanai/e2e_results.log
echo "=== E2E test started $(date) ===" > "$LOG"
sleep 90    # let the in-flight LLM response finish before we unload the model
echo "--- unloading LLM via llama-swap ---" >> "$LOG"
curl -s -X POST http://localhost:8080/api/models/unload >> "$LOG" 2>&1
for i in $(seq 1 90); do
  if ! curl -s --max-time 5 http://localhost:8080/running | grep -q '"model"'; then
    break
  fi
  sleep 2
done
echo "" >> "$LOG"
echo "--- copying updated tools into webui container ---" >> "$LOG"
podman cp /home/user/humanai/tools/comfyui_qwen21_t2i.py user-webui-1:/tmp/tool_t2i.py >> "$LOG" 2>&1
podman cp /home/user/humanai/tools/comfyui_qwen21_vlm.py user-webui-1:/tmp/tool_vlm.py >> "$LOG" 2>&1
podman cp /home/user/humanai/tools/e2e_test.py user-webui-1:/tmp/e2e_test.py >> "$LOG" 2>&1
echo "--- LLM unloaded, running e2e test in webui container ---" >> "$LOG"
podman exec -w /app/backend -e PYTHONPATH=/app/backend user-webui-1 python /tmp/e2e_test.py >> "$LOG" 2>&1
echo "=== E2E test finished $(date) (exit $?) ===" >> "$LOG"
