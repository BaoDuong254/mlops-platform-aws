# Phase 4 — Canary Deployment & Automated Rollback

**Trạng thái: đã triển khai và kiểm chứng trên dev bằng canary thật, gồm hai lần chạy release workflow trên `main`: một lần thành công và một lần canary bị từ chối rồi tự `git revert`.** Thiết kế và quyết định nằm ở [phase-04-design.md](phase-04-design.md); số liệu ở [Evidence Phase 4](../evidence/phase-04.md).

## 1. Mục tiêu
Thử model candidate với một phần traffic thật và tự khôi phục stable nếu metric vận hành không đạt, đồng thời giữ GitOps nhất quán với stable sau rollback.

## 2. Scope
AWS Load Balancer Controller, ALB internal, Argo Rollouts, Prometheus AnalysisTemplate, traffic shifting, abort/rollback và công cụ quan sát. Không thay đổi validation gate của Phase 2.

## 3. Kiến trúc và thành phần
`Rollout inference-api` dùng `workloadRef` tới `Deployment inference-api` nên release giữ Deployment làm nguồn template; source hiện tại sửa thêm expected identity trên Rollout metadata trong cùng một commit. Argo Rollouts quản lý ReplicaSet stable/canary, Service `inference-api` (stable) và `inference-api-canary`, và annotation action của Ingress ALB internal (`target-type: ip`). HPA trỏ vào Rollout. Prometheus giữ label `rollouts_pod_template_hash` để tách canary khỏi stable. Terraform thêm IAM/Pod Identity cho LB Controller và nâng node `system` lên 2 node.

| Bước | Hành động | Cổng |
| --- | --- | --- |
| 0 | canary 1 Pod, weight 0 | smoke Job gọi thẳng `inference-api-canary` |
| 1 | 10% traffic, pause 2 phút | AnalysisRun nền |
| 2 | 50% traffic, pause 2 phút | AnalysisRun nền |
| 3 | 100% | Rollout `Healthy`, canary thành stable |

Gate (`deploy/gitops/inference/analysis-template.yaml`): tỷ lệ lỗi phục vụ **5xx** `< 1%`; p95 `< 0,5 s`; p95 `< 1,5 × p95 stable + 50 ms`; tốc độ request canary `>= 0,2 req/s`. Không có mẫu thì kết quả là inconclusive, không phải đạt.

## 4. Đã triển khai thực tế
Cluster dev chạy LB Controller `v3.6.2`, Argo Rollouts `v1.10.2` và Rollout nêu trên. Đã quan sát: canary khỏe tự lên 100% (T2); canary chậm bị gate p95 từ chối ở 10% và Rollout tự abort (T3); canary không Ready bị chặn trước khi nhận traffic và `Degraded` sau `ProgressDeadlineExceeded` (T4); `git revert` đưa Git và cluster về stable trong khoảng 10 giây. Phân phối đo được: 8,7–9,4% ở bước 10% và 50,7–51,4% ở bước 50%. Release workflow thật (run `42011637577`) build image mới, qua canary khỏe, `champion` ra `already-promoted`, và CIDR EKS được khôi phục. Run thứ hai (`42016449106`) cho k6 gửi request không hợp lệ: gate `error-rate` fail, Rollout abort, workflow `git revert` commit release, Argo CD về stable và `champion` không đổi.

## 5. Luồng hoạt động
Commit đổi template Pod vào nhánh mà Application theo dõi → Argo CD đồng bộ → Rollouts dựng canary → smoke → 10% → phân tích → 50% → phân tích → 100%. Nếu gate fail hoặc Pod không Ready, Rollout `Degraded`, weight canary về 0 và stable tiếp tục phục vụ. Với release chính thức, `scripts/m32_release.sh` chờ kết quả: promote thì verify API rồi xác nhận alias `champion`; bị từ chối thì `git revert` commit release, chờ Argo CD về stable và **không đổi `champion`**.

## 6. Cấu hình và thực thi
Thứ tự: `terraform apply` platform (IAM, node `system` ×2; nâng `desired` bằng `aws eks update-nodegroup-config` trước vì module bỏ qua `desired_size`) → `make canary-addons-deploy` và `canary-addons-verify` → merge manifest. **Không merge manifest Phase 4 vào nhánh Argo CD đang theo dõi trước khi add-on được cài**, nếu không sync lỗi vì thiếu CRD. Quan sát: `make canary-status`, `make canary-split M42_WINDOW=45s`, `make monitoring-load-test M33_TARGET=alb`.

## 7. Cách kiểm chứng
Tải k6 từ trong cluster vào ALB internal (biến `LOAD_*`, tốc độ cố định); `canary-split` đo tỷ lệ request theo ReplicaSet; `kubectl -n mlops get analysisrun.argoproj.io` xem từng gate; lỗi cố ý bằng commit thay `requests`/`limits` CPU hoặc annotation trên nhánh thử, khôi phục bằng `git revert`. Chi tiết và cách lặp lại ở [evidence](../evidence/phase-04.md).

## 8. Kết quả và bằng chứng
[Evidence Phase 4](../evidence/phase-04.md) ghi baseline p95 32 ms ở 40 req/s, phân phối 90/10 và 50/50, T2/T3/T4, thời gian khôi phục Git và các lỗi tìm thấy khi kiểm chứng (xung đột biến k6, gate lỗi `NaN`, `abort` trống khi quá hạn tiến độ).

## 9. Hạn chế và lưu ý
Nhánh từ chối của release workflow mới được thử bằng request không hợp lệ (4xx), chưa bằng lỗi 5xx hay một model lỗi thật, và chỉ một lần. Nhánh smoke fail chưa từng thực thi. Khi tự `git revert`, cluster chỉ về stable sau khi Argo CD hỏi Git (tối đa khoảng 3 phút), trong lúc đó stable phục vụ 100%.


## 10. Thay đổi chất lượng sau evidence 05/11

Source hiện hành chuẩn hóa `ci.yml`/`train.yml`/`release.yml`; smoke so exact candidate identity trước traffic, 4xx không còn kích hoạt error gate, rollback xác minh previous stable digest/Ready Pods, alias version được chuẩn hóa int/str. Release vẫn một commit nhưng sửa Deployment và Rollout expected identity. Tùy chọn CPU fault riêng candidate mặc định tắt dành cho E2E tiếp theo. Các thay đổi này có regression tests local/CI, chưa được coi là đã chạy lại toàn bộ bộ thí nghiệm AWS Phase 1–4. Các số liệu/422 rollback trong evidence là lịch sử của source cũ.

## Bổ sung source: orchestration tự động

Source hiện bổ sung reusable CI→Training→Release và bounded internal ALB traffic; manual dispatch vẫn giữ. Auto mode mặc định tắt. Chi tiết/trạng thái verification mới ở [automated pipeline](../automated-pipeline.md); các kết quả E2E trước đây vẫn là lịch sử. Happy path auto đã được kiểm chứng riêng ngày 18/11/2026 ([run 44220667678](https://github.com/HaiXon231/mlops-platform-aws/actions/runs/44220667678)), nhưng không thay thế toàn bộ thí nghiệm Phase 1–4 hoặc kiểm chứng mới các nhánh rollback. Metric thresholds và rollback boundaries hiện hữu được giữ nguyên.

Negativeverification18/11: [automaticTrainingRejected](https://github.com/HaiXon231/mlops-platform-aws/actions/runs/44240966510) không release/noapprovedversion, checkpointcompleted; [manualsharedReleasev3/CPU100m](https://github.com/HaiXon231/mlops-platform-aws/actions/runs/44256392109) có metricFailed/abort/automaticGitrevert/stable-restored. Đây là negativeexperiments riêng, không fullautomaticchainTestB hoặc chạy lại toàn bộPhase1–4; giới hạn timeout/network/HA còn giữ.
