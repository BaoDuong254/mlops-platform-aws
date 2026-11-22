# Báo cáo kiểm thử local — Live Demo & Verification Framework

Ngày 22/11/2026. Phạm vi: **local validated**. Không có lệnh nào chạm AWS/GitHub trong quá trình phát triển (không push, merge, dispatch, tạo Job hay đổi Stable/Champion). Chưa có kết quả **cloud verified** cho framework mới.

## 1. Đánh giá source trước khi triển khai

| Năng lực | Có sẵn trong source | Framework làm gì |
|---|---|---|
| Cloud Training manual (chỉ train, không release) | `train.yml` `workflow_dispatch` + `training_config` (phải là `configs/*.yaml` đã commit trên `main`) | Reuse, dispatch bằng `gh`, đọc artifact `train-result-*` |
| Guard evaluation identity | `training_result.py validate-config` | Gọi trực tiếp, cùng hàm cloud chạy |
| Gate decision | `training.gate.passes_gate`, ngưỡng `configs/mlflow.yaml` (1 %) | Preview offline dùng đúng hàm/ngưỡng |
| Gate preview với Registry thật | `mlops-train validate-run` (`preview_gate`, read-only) | Giữ nguyên; runbook chỉ cách dùng |
| Auto pipeline CI → Train → Release | `ci.yml` → `train.yml` → `release.yml`, fingerprint `auto_mlops.py` | Trigger bằng PR đổi `configs/training.yaml`; dùng `fingerprint()` cho recovery |
| Release + canary + revert | `release.yml`, `m32_release.sh`, `m32_verify.py` (exit 3 = canary rejected), guarded `git revert` | Reuse, phân loại outcome từ artifacts/log/Git/Argo |
| Fault injection | `canary_test_cpu=100m` (`m32_gitops.update_manifest`) | Reuse, test render Pod template |
| Tải HPA | `m33.sh load-test` (k6 Job, giới hạn VUs ≤ 50, RPS ≤ 200, 30–900 s) | Reuse qua `make monitoring-load-test`; thêm sampler HPA/Pods/NodeClaims/RPS |
| Quan sát canary | `make canary-status`, `make canary-split` | Đưa vào runbook |

**Điểm không tương thích và phương án ít can thiệp nhất**

1. *Demo config trong `configs/training*.yaml` sẽ kích hoạt automatic training ngoài ý muốn* (fingerprint bao gồm pattern đó). → Đặt tên `configs/demo-*.yaml`: vẫn qua `workflow_guard` + `validate-config`, dispatch được, nằm ngoài fingerprint. Không sửa core.
2. *Automatic pipeline cố định `configs/training.yaml` trong `ci.yml`.* → S2 automatic dùng PR chỉ đổi khối `model:`. Recovery theo quy trình đã dùng ngày 18/11 (tắt auto → restore → kiểm fingerprint → baseline → bật auto). Framework in sẵn lệnh và tự kiểm fingerprint.
3. *`scripts/image_identity.py` (chưa commit) băm toàn bộ `configs/`.* → Merge demo configs làm đổi fingerprint image training của đường `make cloud-train`/`cloud-deploy`, cần build/push lại image đó một lần nếu dùng. `train.yml` không bị ảnh hưởng. Được ghi rõ trong runbook, không sửa module đang dang dở.
4. *gh CLI 2.46 không có `gh variable get` và không có trường JSON `attempt`.* → Dùng `gh variable list --json name,value` và REST `run_attempt`. Có contract test khóa danh sách trường.
5. Không đổi workflow, thresholds, gate, AnalysisTemplate hay `ci.yml`. Đổi `ci.yml` sẽ làm đổi training fingerprint, nên test framework không được thêm vào Source CI; chúng chạy bằng `make check` / `make demo-test`.

## 2. Kiểm thử đã chạy

| Lệnh | Kết quả |
|---|---|
| `uv run --locked pytest -q` (toàn bộ repo) | **320 passed** (222 có sẵn + 98 mới), 0 failed, 0 skipped |
| `uv run --locked pytest -q tests/unit/test_demo_framework.py` | **98 passed** |
| `make demo-test` (tests + `validate`) | OK: 5 scenarios, 2 presets, 2 demo configs |
| Dry-run 2 presets × 5 scenarios (`run`, `show`) | 10/10 render đúng lệnh; test khẳng định không tạo shell |
| `preview --preset verification --scenario S2 --train --reference-mae 3.6445432012` | Chạy thật end-to-end local: MAE 3.613921 → `preview_rejected` |
| Mutation check (3 lỗi cố ý vào classifier/preflight) | Cả 3 bị test bắt (lỗi thứ nhất ban đầu lọt; đã bổ sung case "revert không có marker") |

Mapping với yêu cầu kiểm thử:

| # | Yêu cầu | Tests (trong `tests/unit/test_demo_framework.py`) |
|---|---|---|
| 1 | Scenario schema/config validation | `test_shipped_scenarios_and_presets_validate`, `test_invalid_preset_inputs_are_rejected` (13 case), `test_broken_scenario_definitions_are_reported`, `test_new_scenario_needs_only_a_yaml_file`, `test_values_cannot_inject_arguments_or_shell_syntax` |
| 2 | Training config compatibility | `test_demo_configs_keep_evaluation_identity_and_match_presets`, `test_demo_configs_do_not_trigger_automatic_training`, `test_identity_change_is_rejected_by_the_cloud_guard`, `test_apply_candidate_changes_only_the_model_block`, `test_semantic_check_detects_drift_on_main` |
| 3 | Logic Approved/Rejected | `test_preview_uses_the_cloud_gate_threshold` (biên đúng 1 %), `test_training_rejection_classification` (10 case), `test_pipeline_reports_the_real_decision_either_way` |
| 4 | Release input, `off/100m` | `test_framework_release_rules_equal_the_workflow_guard` (so trực tiếp với `workflow_guard.validate` và options của `release.yml`) |
| 5 | GitOps render / Pod template | `test_fault_injection_changes_only_the_candidate_pod_template` (dùng `m32_gitops` thật: chỉ CPU candidate đổi, selector/strategy giữ, rollout.yaml không đổi, release kế tiếp khôi phục CPU) |
| 6 | Successful / Failed / Inconclusive / Timeout | `test_release_verdicts_separate_canary_rejection_from_failures` (15 case), `test_hpa_verdict_follows_measured_metrics` (6 case), `test_hpa_measurements_are_reported_per_profile` |
| 7 | Preflight failure, cleanup/recovery | `test_preflight_failures_block_the_trigger` (6 case, xác nhận không dispatch và lock được nhả), `test_missing_tools_and_crashing_probes_fail_closed`, `test_lock_prevents_parallel_scenarios`, `test_recovery_is_verified_only_from_read_back_state`, `test_load_scenario_deletes_only_its_own_k6_jobs_even_on_failure` |
| 8 | Dry-run toàn bộ | `test_dry_run_renders_exact_commands_without_executing` (10 case, so lệnh golden), `test_load_profiles_render_per_preset`, `test_s2_modes_and_git_safety`, `test_s1_execute_end_to_end_on_fixtures`, `test_scope_labels_never_overclaim`, `test_gh_json_fields_exist_in_gh_2_46` |

`test_s1_execute_end_to_end_on_fixtures` chạy toàn bộ đường `run --execute` trên fixture (preflight → dispatch → locate run → artifact → MLflow → verdict → recovery → report). Test khẳng định report ghi `local validated (fixture data …)`, **không** ghi `cloud verified`.

## 3. Training candidate (S2) — kết quả local thật

Local tái lập champion v3: config chuẩn (1500 trees/127 leaves) cho validation MAE **3.644543201181831**, khớp cloud `3.6445432012` (cùng dataset/seed, `deterministic: true`, 37 s). Validation dataset key local: `c48a6e66…56d4`. Preflight sẽ so khóa này với tag của champion khi có MLflow.

Gate: candidate ≤ 3.6445432012 × 0.99 = **3.608098**. Các cấu hình chỉ đổi khối `model:`:

| Cấu hình (thay đổi so với chuẩn) | Validation MAE | Cải thiện | Preview |
|---|---|---|---|
| 1 tree / 2 leaves (S1) | ≈ 7.86 (cloud 18/11: 7.8621684646) | — | rejected |
| 255 leaves, 3000 trees | 3.621783 | 0.62 % | rejected |
| **255 leaves, 3000 trees, mcs 50 (candidate hiện tại)** | **3.613921** | **0.84 %** | **rejected** |
| 511 leaves, 3000 trees | 3.620018 | 0.67 % | rejected |
| 255 leaves, colsample 0.8 | 3.627051 | 0.48 % | rejected |
| 1023 leaves, mcs 100 | 3.632040 | 0.34 % | rejected |
| 127 leaves, lr 0.03, 3000 trees | 3.635421 | 0.25 % | rejected |
| 255 leaves, mcs 100, 4000 trees | 3.624626 | 0.55 % | rejected |
| 255 leaves, mcs 200 | 3.636369 | 0.23 % | rejected |
| 511 leaves, mcs 100 | 3.626023 | 0.51 % | rejected |
| 255 leaves, mcs 100, lr 0.03 | 3.613735 | 0.84 % | rejected |
| 255 leaves, mcs 100, λ 5 | 3.618128 | 0.72 % | rejected |
| 383 leaves, mcs 150, λ 10 | 3.637605 | 0.19 % | rejected |
| 255 leaves, mcs 50, lr 0.03, 6000 trees | 3.612027 | 0.89 % | rejected |
| 255 leaves, mcs 40, lr 0.03, 6000 trees | 3.613330 | 0.86 % | rejected |
| 383 leaves, mcs 50, lr 0.03, 6000 trees | 3.609817 | 0.95 % | rejected |
| 255 leaves, mcs 50, lr 0.02, 8000 trees | 3.608300 | 0.994 % | rejected (thiếu 0.0002 phút) |

Kết luận: 16 cấu hình chỉ đổi khối `model:` đều không vượt ngưỡng 1 % so với champion v3; gần nhất là lr 0.02/8000 trees (3.608300 so với yêu cầu ≤ 3.608098, thiếu ~0.0002 phút, train ~4 phút). Hạ learning rate thêm có thể vượt ngưỡng nhưng chỉ bằng mức chênh không đáng kể. Không hạ ngưỡng, không đổi dataset để tạo Approved. Candidate được chọn (`3000/255/mcs50`) có thời gian train hợp lý (~1,5 phút local) và preview **rejected**. Vì vậy S2 automatic trên AWS dự kiến cho `rejected`. Framework sẽ báo đúng kết quả thật nếu cloud khác. Luồng Approved → Release → Canary → Champion được chứng minh bằng S3.

## 4. Giới hạn — chỉ xác minh được trên AWS

- Toàn bộ verdict S1–S5 trên cloud (Spot Job, MLflow run thật, ALB weights, AnalysisRun, revert, HPA/Karpenter).
- Các probe live (`kubectl`/`gh`/MLflow/Prometheus port-forward) chỉ được kiểm bằng fixture và contract (trường JSON gh 2.46). Lần chạy `preflight` đầu tiên trên cluster là bước xác minh tích hợp; nên chạy Bộ A trước ngày demo.
- Phát hiện run dispatch dựa trên `createdAt` ≥ thời điểm trigger (gh 2.46 không trả run ID). Nếu có người khác dispatch cùng lúc, preflight `no_active_workflows` chặn trước; vẫn nên kiểm URL run trong report.
- Verdict S4 dựa trên marker log `Canary rejected` của `m32_release.sh`, commit revert và AnalysisRun. Đổi wording trong script đó cần cập nhật `verdict.CANARY_REJECTED_MARKER`.
- Timeout/network/terminal-Inconclusive trên cloud vẫn chưa được thử thật (như ghi chú 18/11). Framework phân loại chúng là INCONCLUSIVE/TIMEOUT, không phải rollback.
- S5: verdict phụ thuộc mẫu 15 s. Spike ngắn giữa hai mẫu có thể bị bỏ sót (report ghi chú khi scale-out mà CPU mẫu không vượt band).
