# Demo Matrix

Bảng tra cứu nhanh cho năm kịch bản. Thao tác chi tiết nằm ở [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md); định nghĩa máy đọc được nằm trong `demo/scenarios/*.yaml`, input theo bộ nằm trong `demo/presets/{verification,demo}.yaml`.

Lệnh chung: `uv run --locked python -m demo.runner run --preset <verification|demo> --scenario <S1..S5> [--execute]` (mặc định dry-run).

## Scenario → Input → Workflow

| Scenario | Input `verification` (Bộ A) | Input `demo` (Bộ B) | Trigger / Workflow | Expected |
|---|---|---|---|---|
| **S1** Training Rejected | `configs/demo-rejected.yaml` (`n_estimators=1`, `num_leaves=2`) | giống Bộ A | `train.yml` dispatch, `training_config=configs/demo-rejected.yaml` (manual = chỉ train) | `rejected` |
| **S2** Training Experiment / Auto Pipeline | `mode: preview`, `configs/demo-candidate.yaml` (`3000/255/mcs50`) | `mode: automatic`, cùng candidate | preview: local train + gate · automatic: PR đổi khối `model:` của `configs/training.yaml` → merge → `ci.yml` → `train.yml` → `release.yml` | `approved_or_rejected` (ghi đúng thực tế) |
| **S3** Successful Canary | `model_version: champion`, `canary_test_cpu=off` | giống Bộ A | `release.yml` dispatch | `promoted` |
| **S4** Failed Canary & Rollback | `model_version: champion`, `canary_test_cpu=100m` | giống Bộ A | `release.yml` dispatch | `aborted_reverted` |
| **S5** HPA & Monitoring | 1 profile: 20 RPS / 10 VUs / 120 s; quan sát thêm 420 s | 20 RPS / 10 VUs / 120 s → 100 RPS / 30 VUs / 300 s; quan sát thêm 480 s | `make monitoring-load-test M33_TARGET=service …` (k6 Job trong cluster) | `hpa_consistent` |

`model_version: champion` được resolve từ alias MLflow ở preflight. Preflight cũng kiểm tra version đó là `READY` + `approved`. Không hardcode số version hay kết quả.

## AWS / Kubernetes resources

| Scenario | Tạo / thay đổi | Đọc để kiểm chứng |
|---|---|---|
| S1 | ECR image training (tag `train-<run>-…`), Job `mlops/mlops-training-gh-<run>-<attempt>` trên Spot NodeClaim (Karpenter), EKS public CIDR tạm (workflow tự restore) | MLflow run, Registry versions, alias champion, Jobs còn active, CIDRs |
| S2 | Như S1; nếu approved thêm như S3. PR/branch `demo/s2-<stamp>` | Như S1 + S3, checkpoint `auto-mlops-checkpoint-*`, fingerprint |
| S3 | ECR image inference immutable, GitOps commit `m32: release m32-<run>-<attempt>`, ReplicaSet canary, AnalysisRuns (smoke + gates), Job `mlops-release-traffic/m32-traffic-*`, alias champion | Rollout/AnalysisRun/Argo Application, live image, m32-result.json, CIDRs |
| S4 | Như S3 + commit `Revert "m32: release …"` (do workflow tạo) | Như S3, candidate CPU 100m vs stable, revert commit, stable digest |
| S5 | Job `mlops/m33-k6-*` (runner xóa đúng Job của mình), Pods HPA, có thể thêm NodeClaim inference | HPA status, Pods, NodeClaims, Prometheus RPS |

## UI quan sát chính

| Scenario | GitHub Actions | MLflow | Argo CD | Grafana | AWS / kubectl |
|---|---|---|---|---|---|
| S1 | Run **Cloud Training**, summary `Validation decision: **rejected**` | Run mới FINISHED/rejected, không version mới | Không đổi | — | EKS Jobs, `kubectl get nodeclaims -w` |
| S2 | Source CI → **Automatic MLOps** (plan/train/release/checkpoint) | Run + version mới (nếu approved), alias champion | Commit release, canary | Canary Deployment & Performance | Jobs, NodeClaims |
| S3 | Run **Approved Model Release** | Alias champion = version release | Sync commit mới, cây Rollout 2 ReplicaSet | Canary Deployment & Performance | `make canary-status`, `make canary-split M42_WINDOW=45s` |
| S4 | Run kết thúc failure có chủ đích + summary `Canary rejected` | Champion không đổi | Commit release rồi commit Revert | P95 canary vượt gate | Pod CPU 100m, AnalysisRun Failed |
| S5 | — | — | — | **Inference Load & HPA** | `kubectl -n mlops get hpa inference-api -w`, EKS Nodes |

## Expected Results → Verdict của framework

| Scenario | PASSED khi | FAILED khi | INCONCLUSIVE / TIMEOUT khi |
|---|---|---|---|
| S1 | Workflow success; decision rejected; không version cho run; champion không đổi; release skipped | Candidate được approved; rejected nhưng có version; champion đổi; release chạy | Workflow failure (infra), thiếu artifact / quá hạn, cancelled |
| S2 | Rejected: release skipped, champion không đổi. Approved: version READY + promoted (hoặc canary bị gate từ chối và revert an toàn) | Release chạy sau rejected; invariant hỏng | Failure trước GitOps, thiếu evidence, PR không merge |
| S3 | Promoted; Rollout stable = hash mới; live image = digest; champion = version; AnalysisRun Successful | Canary bị gate thật từ chối; champion/digest lệch | Failure trước GitOps commit; analysis Inconclusive; timeout/network |
| S4 | Revert + `Canary rejected`; AnalysisRun Failed (hoặc smoke Failed); live = stable digest cũ; champion không đổi | Canary vẫn promote (fault không kích hoạt gate); abort mà không revert; champion đổi | Failure trước GitOps; revert không có marker của workflow; timeout |
| S5 | CPU vượt 60%×1.1 → scale-out và scale-in về min; hoặc CPU trong band → không scale (đúng) | CPU vượt band ≥2 mẫu nhưng desired không tăng | k6 fail, thiếu metrics, scale-in chưa xảy ra trong cửa sổ |

## Recovery

| Scenario | Kiểm tra tự động (`recover`) | Hành động khi NEEDS_ATTENTION |
|---|---|---|
| S1 | no_training_jobs, champion_unchanged, rollout_stable, gitops_unchanged | Xóa đúng Job còn sót; kiểm CIDRs; không cần restore config |
| S2 | no_training_jobs, no_traffic_jobs, rollout_stable, argocd_synced_main, champion_consistent; fingerprint so với trước demo | Rejected → quy trình restore (tắt auto → PR restore → fingerprint == → baseline SHA → bật auto); approved → giữ config mới |
| S3 | no_traffic_jobs, rollout_stable, argocd_synced_main, live_matches_git, champion_consistent, eks_cidrs_restored | Đọc run summary; không promote/alias bằng tay |
| S4 | như S3 + stable_digest_restored, champion_unchanged | Không có revert → `git revert <release-commit>` qua PR; không sửa Rollout bằng tay |
| S5 | no_load_jobs, rollout_stable | Chờ thêm stabilization 300 s rồi chạy lại `kubectl get hpa`; xóa đúng Job k6 còn sót |

## Thứ tự chạy (không chồng chéo)

`demo`: S1 → S2 → S3 → S4 → S5. `verification`: S1 → S3 → S4 → S5 (S2 preview chạy local bất kỳ lúc nào). Mỗi bước chỉ bắt đầu khi bước trước báo `Recovery: VERIFIED`. Preflight `no_active_workflows`/`no_active_jobs` chặn chạy chồng. Lock local `artifacts/demo/.lock` chặn hai runner song song.
