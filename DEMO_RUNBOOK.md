# Live Demo Runbook

Một framework thống nhất (`demo/`) điều khiển năm kịch bản S1–S5. Mỗi lần chạy: **chọn bộ input → chọn scenario → preflight → kích hoạt → quan sát → verify → recovery**. Mọi lệnh mặc định là dry-run. Chỉ `--execute` (có xác nhận) mới chạm AWS/GitHub.

- Bộ A `verification`: kiểm thử trước ngày demo (tải nhẹ, S2 chỉ preview).
- Bộ B `demo`: trình diễn với giảng viên (S2 automatic, HPA hai mức tải).
- Bảng tổng hợp: [DEMO_MATRIX.md](DEMO_MATRIX.md). Báo cáo kiểm thử local: [docs/demo-local-test-report.md](docs/demo-local-test-report.md).

> Nhãn kết quả. `local validated` = logic/config đã kiểm thử local hoặc trên fixture. `cloud verified` = framework đọc trạng thái thật trên AWS, verdict `PASSED` **và** recovery `VERIFIED`. Report không bao giờ gán `cloud verified` cho dữ liệu fixture, preview hoặc dry-run.

## 0. Chuẩn bị một lần (trước ngày demo)

1. **Đưa framework lên `main` qua PR.** Gồm `demo/`, `configs/demo-*.yaml`, `tests/unit/test_demo_framework.py`, `DEMO_*.md`, `docs/demo-local-test-report.md` và block `demo-*` trong `Makefile`. `train.yml` chỉ dispatch config đã commit trên `main`. Các file này nằm **ngoài** training fingerprint (có test kiểm), nên merge sẽ không kích hoạt automatic training (`no-training-change`).
   - Lưu ý: `scripts/image_identity.py` (đang chưa commit) băm mọi file trong `configs/`. Sau khi merge, fingerprint image *training* của đường `make cloud-train`/`cloud-deploy` đổi, nên cần `make cloud-images-build cloud-images-push IMAGE_PURPOSE=training` nếu dùng đường đó. `train.yml` (đường demo dùng) tự build image mỗi run nên không bị ảnh hưởng.
2. Kiểm tra local: `make demo-test` (unit tests + validate) và `make demo-sequence DEMO_PRESET=demo`.
3. Hạ tầng đã dựng sẵn. Xác nhận credentials: `export AWS_PROFILE=mlops-dev AWS_REGION=ap-southeast-1`, `aws eks update-kubeconfig --name mlops-aws-dev`, `gh auth status`.
4. Chạy Bộ A theo thứ tự S1 → S3 → S4 → S5 (mỗi bước chờ `Recovery: VERIFIED`) để có cloud verification trước ngày demo.
5. S2: chạy `make demo-preview DEMO_PRESET=demo` khi đã mở được MLflow (so với champion thật). Kết quả local hiện tại ở mục S2.

## Mở các giao diện (terminal riêng, giữ chạy suốt buổi)

```bash
kubectl -n mlops port-forward service/mlflow 5000:5000            # MLflow  http://127.0.0.1:5000
kubectl -n argocd port-forward service/argocd-server 8080:443      # Argo CD https://127.0.0.1:8080
kubectl -n monitoring port-forward service/m33-grafana 3000:80     # Grafana http://127.0.0.1:3000 (user admin)
```

- GitHub Actions: `https://github.com/HaiXon231/mlops-platform-aws/actions`.
- Grafana dashboards: **Inference Load & HPA** (`/d/m33-load-hpa`) và **Canary Deployment & Performance** (`/d/m33-canary`).
- AWS Console → EKS → `mlops-aws-dev` → Compute/Resources.
- Mật khẩu Grafana/Argo CD lấy theo [operations](docs/operations.md). Không ghi mật khẩu vào repo.

Runner tự mở port-forward riêng trên cổng ngẫu nhiên (MLflow/Prometheus) khi cần đọc dữ liệu, nên không xung đột với các cổng trên.

## Vòng lặp chuẩn cho mọi scenario

```bash
P=demo S=S3                                                     # chọn bộ input + scenario
uv run --locked python -m demo.runner show      --preset $P --scenario $S   # runbook card
uv run --locked python -m demo.runner run       --preset $P --scenario $S   # dry-run: lệnh/tham số chính xác
uv run --locked python -m demo.runner preflight --preset $P --scenario $S   # read-only + lưu snapshot trước chạy
uv run --locked python -m demo.runner run       --preset $P --scenario $S --execute   # kích hoạt (gõ S3 để xác nhận)
uv run --locked python -m demo.runner recover   --preset $P --scenario $S   # đọc lại trạng thái cuối
```

Tương đương với Make: `make demo-show|demo-run|demo-preflight|demo-execute|demo-recover DEMO_PRESET=demo DEMO_SCENARIO=S3`.

**Kích hoạt từ GitHub UI (đẹp hơn khi trình diễn).** Chạy `preflight` trước để lưu snapshot. Bấm **Run workflow** với các giá trị trong bảng của từng scenario, lấy run ID từ URL, rồi:

```bash
uv run --locked python -m demo.runner verify --preset demo --scenario S3 --run-id <RUN_ID>
```

Mỗi lần chạy ghi evidence vào `artifacts/demo/<UTC>_<preset>_<scenario>/` gồm `report.md` (timestamps, giá trị đo, verdict, lý do, link run/commit/AnalysisRun) và `evidence.json` (cộng `hpa-samples.csv` với S5).

Exit code: `0` = PASSED + VERIFIED · `2` = preflight/cấu hình lỗi, chưa kích hoạt gì · `3` = đã chạy nhưng verdict khác PASSED hoặc recovery cần xử lý.

---

## S1 — Training Rejected (~25 phút)

1. **Mục tiêu.** Candidate kém chất lượng chạy Cloud Training thật (Spot Job + MLflow run) và bị Validation Gate từ chối. Không có version mới, không release, champion giữ nguyên.
2. **Initial State.** Argo CD Synced/Healthy; Rollout Healthy. Alias `champion` (preflight ghi lại version + MAE). `configs/demo-rejected.yaml` có trên `main` (preflight `demo_config_on_main`).
3. **Input Change.** Chỉ đổi khối `model:`: `n_estimators: 1`, `num_leaves: 2`. Dataset/splits/seed/sample/gate 1% không đổi (cùng guard `validate-config` mà cloud chạy).
4. **Trigger.**
   - CLI: `uv run --locked python -m demo.runner run --preset demo --scenario S1 --execute`
   - GitHub UI: **Actions → Cloud Training → Run workflow**, branch `main`, `training_config` = `configs/demo-rejected.yaml`.
5. **Live Observation.**
   - GitHub Actions: `plan` → **MLOps Cloud Training** (build image, Spot Job, verify), `release` skipped. Summary: `Validation decision: **rejected**`.
   - EKS: `kubectl -n mlops get jobs -w`, `kubectl get nodeclaims -w` (Spot node xuất hiện rồi bị thu hồi).
   - MLflow: experiment `nyc-taxi-trip-duration` → run mới FINISHED, tag `validation_status=rejected`, validation MAE ≈ 7.86 (lần 18/11) so với reference champion.
   - MLflow Models: không có version mới; alias `champion` không đổi.
6. **Acceptance Criteria.** Workflow `success` (rejected là quyết định hợp lệ); `training-result.json` có `validation_status=rejected`, `model_version=null`; MLflow run FINISHED/rejected; không version nào gắn run ID; champion trước = sau.
7. **Recovery.** `recover` kiểm tra không còn training Job, champion không đổi, Rollout stable, GitOps template không đổi. Không cần restore config.

## S2 — Training Experiment / Automatic Pipeline (~60 phút ở mode automatic)

1. **Mục tiêu.** Thử hyperparameters, preview Validation Gate tại local, rồi (mode automatic) để CI → Spot Training → Gate → Release → Canary → Champion tự chạy. Kết quả Approved hay Rejected đều được báo đúng thực tế.
2. **Initial State.** `AUTO_MLOPS_ENABLED=true`, baseline SHA đã review, checkpoint trước không `blocked` (preflight `auto_mode_enabled`). Preflight còn lưu `configs/training.yaml` và training fingerprint trước demo để restore.
3. **Input Change.** Candidate `configs/demo-candidate.yaml` (render từ preset): `n_estimators: 3000`, `num_leaves: 255`, `min_child_samples: 50`. Mode automatic tạo PR chỉ đổi đúng các dòng này trong `configs/training.yaml`.
4. **Trigger.**
   - Preview local (không AWS ngoài việc đọc champion): `make demo-preview DEMO_PRESET=demo`. Offline (không cluster): `uv run --locked python -m demo.runner preview --preset demo --scenario S2 --train --reference-mae <MAE champion>`.
   - Automatic: `uv run --locked python -m demo.runner run --preset demo --scenario S2 --execute`. Runner tạo git worktree tách biệt từ `origin/main` (không đụng working tree của bạn), commit một file, push branch `demo/s2-<stamp>`, mở PR, rồi **chờ bạn merge PR trên GitHub UI**. Sau đó tự tìm Source CI run của merge commit và theo dõi.
5. **Live Observation.** Source CI → job **Automatic MLOps** (`plan` reason `training-inputs-changed` → train → release nếu approved → checkpoint). MLflow: run mới; nếu approved thì version mới READY, sau release alias `champion` = version mới. Argo CD/Grafana như S3.
6. **Acceptance Criteria.**
   - Rejected: release skipped, không version mới, champion không đổi, checkpoint `completed`.
   - Approved: version READY/approved, Release success, GitOps commit, Rollout promoted, champion = version mới. Approved nhưng canary bị gate thật từ chối và revert an toàn cũng là kết quả nhất quán.
   - Report ghi cạnh nhau quyết định preview và quyết định cloud.
7. **Recovery.** `recover --scenario S2` so fingerprint hiện tại với trước demo.
   - Nếu **Rejected**, runner in quy trình restore: tắt auto → PR restore `configs/training.yaml` (lấy từ snapshot) → merge → `recover` phải báo fingerprint `==` → đặt `AUTO_MLOPS_BASELINE_SHA` = commit restore → bật auto. Đây đúng là quy trình đã dùng sau negative test 18/11.
   - Nếu **Approved + promoted**, config mới trở thành chuẩn; không restore.

**Kết quả preview local (đã chạy).** Local tái lập chính xác champion v3: validation MAE `3.644543201181831` (cloud `3.6445432012`, cùng data/seed). Gate yêu cầu ≤ **3.608098**. Đã thử 16 cấu hình chỉ đổi khối `model:` (chi tiết trong báo cáo kiểm thử local); tốt nhất đạt **0,994 %** (3.608300, thiếu ~0.0002 phút) nên **preview = Rejected** với champion hiện tại. Vì vậy:
- Không giả định S2 sẽ Approved. Nếu chạy automatic với candidate này, kết quả dự kiến là `rejected` (pipeline vẫn chứng minh được CI → Training → Gate dừng Release). Luồng Release/Canary/Champion được trình diễn bằng S3.
- Muốn có nhánh Approved, cần cải thiện thật (feature/data mới), không hạ threshold. Thêm bộ tham số mới vào preset, `render-configs --write`, rồi `preview --train`.

## S3 — Successful Canary (~25 phút)

1. **Mục tiêu.** Release một Approved Model Version: Docker build, ECR push, GitOps commit, Candidate ReplicaSet/Pod, smoke, 10% → 50%, AnalysisRun Successful, promotion, champion.
2. **Initial State.** Rollout Healthy; Argo CD Synced/Healthy tại HEAD `main`; live template = Git. Version cần release là READY/approved (`model_version: champion` được resolve ở preflight). Prometheus target UP.
3. **Input Change.** Không sửa file. Dispatch `canary_test_cpu=off`.
4. **Trigger.**
   - CLI: `uv run --locked python -m demo.runner run --preset demo --scenario S3 --execute`
   - GitHub UI: **Actions → Approved Model Release → Run workflow**, `main`, `model_name` = `nyc-taxi-trip-duration`, `model_version` = *(version preflight in ra, ví dụ `3`)*, `canary_test_cpu` = `off`.
5. **Live Observation.**
   - GitHub Actions: step *Export, build, deploy, verify and promote one release*: docker build/push → commit `m32: release m32-<run>-<attempt> by digest` → verifier chờ promoted → champion.
   - Argo CD: Application sync commit mới; Rollout có ReplicaSet mới cạnh stable.
   - Terminal: `make canary-status`; `make canary-split M42_WINDOW=45s` trong bước 10% và 50%.
   - Grafana **Canary Deployment & Performance**: request rate tách theo ReplicaSet, P95, 5xx.
   - MLflow: alias `champion` đọc lại = version đã release.
6. **Acceptance Criteria.** Workflow success; `m32-result.json` có `champion` = `promoted`/`already-promoted`; một release commit, không revert; AnalysisRun Successful; stable hash = hash mới; live image = digest trong `m32-expected.json`; champion = version.
7. **Recovery.** Không còn traffic Job; Rollout stable; Argo Synced tại `main`; live = Git; champion khớp model-version trong Git; EKS CIDRs bằng danh sách trước chạy.

## S4 — Failed Canary & Rollback (~25 phút)

1. **Mục tiêu.** Fault injection có sẵn giới hạn CPU riêng Candidate. Gates P95 làm AnalysisRun Failed → abort → guarded Git revert → stable khôi phục; champion giữ nguyên.
2. **Initial State.** Như S3. Preflight lưu stable digest/hash + champion.
3. **Input Change.** `canary_test_cpu=100m`: `m32_gitops` chỉ đổi requests/limits CPU của Pod template mới và ghi annotation `test-original-cpu-*`. Stable giữ 250m/1 CPU.
4. **Trigger.**
   - CLI: `uv run --locked python -m demo.runner run --preset demo --scenario S4 --execute`
   - GitHub UI: như S3 nhưng `canary_test_cpu` = `100m`.
5. **Live Observation.**
   - CPU từng Pod: `kubectl -n mlops get pods -l app=inference-api -o custom-columns=NAME:.metadata.name,HASH:.metadata.labels.rollouts-pod-template-hash,CPU:.spec.containers[0].resources.limits.cpu`
   - Grafana Canary: P95 của canary vượt `0.5 s` hoặc `1.5×stable + 0.05 s`.
   - `make canary-status`: AnalysisRun Failed; Rollout Degraded/aborted.
   - Argo CD: commit release rồi commit `Revert "m32: release …"`; Synced/Healthy về stable.
   - GitHub Actions: kết thúc **failure có chủ đích**; summary `Canary rejected … champion alias was NOT changed`.
6. **Acceptance Criteria.** Có release commit + revert commit + marker `Canary rejected`; AnalysisRun Failed (hoặc smoke Failed); candidate CPU = 100m; live digest = stable trước chạy; champion không đổi.
   - **Không đánh đồng:** failure trước GitOps commit → `INCONCLUSIVE failed_before_gitops`. Failure sau commit mà không có verdict canary → `INCONCLUSIVE workflow_failure_after_gitops`. Workflow cancel/timeout → `TIMEOUT`. Revert không có marker của workflow → không tính là gate rollback. Canary vẫn promote → `FAILED` (fault không kích hoạt gate).
7. **Recovery.** Như S3, thêm `stable_digest_restored` và `champion_unchanged`. Nếu không có revert (main đã di chuyển/push bị từ chối): `git revert <release-commit>` qua PR. Không sửa Rollout bằng tay (Argo CD selfHeal sẽ ghi đè).

## S5 — HPA Autoscaling & Monitoring (~15–20 phút)

1. **Mục tiêu.** Chạy profile k6 trong cluster; lấy mẫu RPS, CPU utilization, HPA desired/current, Pods, NodeClaims mỗi 15 s; đánh giá scale-out/scale-in theo số liệu thật.
2. **Initial State.** HPA có CPU metric, replicas = min (1); không có Job k6/traffic/training; Prometheus target UP.
3. **Input Change.** Bộ A: 20 RPS/10 VUs/120 s. Bộ B: 20 RPS/10 VUs/120 s rồi 100 RPS/30 VUs/300 s; quan sát thêm 420/480 s sau tải (HPA `stabilizationWindowSeconds=300`).
4. **Trigger.**
   - CLI: `uv run --locked python -m demo.runner run --preset demo --scenario S5 --execute`. Runner gọi `make monitoring-load-test M33_TARGET=service M33_VUS=… M33_RATE=… M33_DURATION_SECONDS=…` cho từng profile (giới hạn có sẵn của `m33.sh`).
   - Không có GitHub UI.
5. **Live Observation.** Grafana **Inference Load & HPA**: Request Throughput, CPU Utilization vs HPA Target (60 %), HPA Replicas Current/Desired, P95. Terminal: `kubectl -n mlops get hpa inference-api -w`, `kubectl get nodeclaims -w`. EKS console → Nodes.
6. **Acceptance Criteria** (không ép 1→3→1):
   - CPU vượt `60 % × 1.1` ở ≥ 2 mẫu → desired phải tăng.
   - CPU nằm trong band → không scale là đúng (`no_scale_needed`).
   - Đã scale-out → phải về `minReplicas` trong cửa sổ quan sát (`scaled_out_and_in`). Nếu chưa về → `INCONCLUSIVE scaled_out_scale_in_pending`, không gán PASSED.
   - k6 lỗi → `INCONCLUSIVE load_failed`.
7. **Recovery.** Runner chỉ xóa các Job `m33-k6-*` do chính nó tạo (kể cả khi lỗi). Không chạy `make monitoring-cleanup` (lệnh đó gỡ cả monitoring). `recover` kiểm tra không còn Job k6 active và Rollout stable.

---

## Kịch bản tổng thể cho buổi demo (Bộ B)

`uv run --locked python -m demo.runner sequence --preset demo` in thứ tự và lệnh. Chạy **tuần tự**, chỉ sang bước tiếp khi `Recovery: VERIFIED`:

| # | Scenario | Thời lượng | Ghi chú |
|---|---|---|---|
| 0 | Preflight S1, mở UI | 5' | `preflight` mọi scenario để thấy trạng thái ban đầu |
| 1 | S1 Training Rejected | ~25' | Kích hoạt từ GitHub UI, giải thích gate trong lúc chờ Spot Job |
| 2 | S2 preview (+ automatic nếu đủ thời gian) | 2' / ~60' | Preview hiện Rejected; automatic chứng minh CI → Training → Gate dừng Release |
| 3 | S3 Successful Canary | ~25' | Argo CD + Grafana canary + `make canary-split` |
| 4 | S4 Failed Canary & Rollback | ~25' | Cùng màn hình với S3 để so sánh; chỉ ra commit Revert |
| 5 | S5 HPA | ~16' | Chỉ chạy khi không còn Release/traffic Job |
| 6 | `recover` cho S2 (nếu đã chạy automatic và Rejected) | 10' | Quy trình restore config + baseline |

Xung đột cần tránh: Training và Release dùng chung khóa `eks-<cluster>-mutation` trên GitHub. Không chạy S5 trong lúc canary (traffic Job + HPA trộn tín hiệu). Không dispatch S3/S4 khi S2 automatic còn đang chạy.

## Thêm một scenario mới

1. Tạo `demo/scenarios/s6-<tên>.yaml`: `engine` (training|pipeline|release|load), `inputs` có kiểu, `preflight`, `trigger` (argv template), `verify.expect`, `recovery.checks`.
2. Thêm input cho `S6` vào hai preset.
3. `make demo-validate` rồi `make demo-run DEMO_SCENARIO=S6`.

Chỉ cần thêm Python khi có engine hoặc expectation mới (`demo/runner/verdict.py`).

## Giới hạn

- Framework không tự thay đổi Champion, Stable image hay GitOps. Các thay đổi đó chỉ xảy ra qua workflow hiện có khi bạn `--execute` một scenario đã xác nhận.
- Report cloud chỉ đọc trạng thái mà công cụ truy cập được (gh, kubectl, MLflow qua port-forward, Prometheus). Logs `gh run view --log` cần run đã hoàn tất.
- S2 automatic cần merge PR vào `main` (hành động của người vận hành). Rejected cần quy trình restore. Không giảm threshold để có Approved.
