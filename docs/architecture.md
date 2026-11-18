# Kiến trúc hệ thống

[README](../README.md) có sơ đồ tổng quan; tài liệu này mô tả ranh giới sở hữu và ba luồng đang vận hành.

## Tài nguyên và quyền sở hữu

| Lớp | State / nơi khai báo | Vòng đời |
| --- | --- | --- |
| Bootstrap | `infra/terraform/bootstrap`, state local | Bucket Terraform state; sao lưu state ngoài Git |
| Persistent | `infra/terraform/persistent`, state S3 riêng | S3 datasets, model artifacts, backups và ECR; giữ khi dừng platform |
| Platform | `infra/terraform/platform`, state S3 riêng | VPC, EKS, node group system, IAM, add-ons và Pod Identity |
| Kubernetes nền | `deploy/platform`, `deploy/bootstrap` | Karpenter, Argo CD, MLflow, PostgreSQL/PVC, namespaces và RBAC |
| Serving GitOps | `deploy/gitops/inference` | Argo CD quản lý Deployment template, Rollout, hai Services, Ingress, AnalysisTemplates và HPA; image được pin digest |
| Monitoring | `deploy/monitoring` | Helm Prometheus/Grafana, k6 Job; chỉ ClusterIP |

Karpenter dùng Spot NodePool `training-spot` cho Job huấn luyện và On-Demand NodePool `inference-on-demand` cho FastAPI. Managed Node Group `system` chạy control workloads, MLflow, PostgreSQL và monitoring. Các tên này là định danh runtime, không đổi khi dọn repository.

## Luồng dữ liệu và model

NYC TLC Parquet được tải vào local cache với SHA256/metadata rồi sync lên S3 Datasets. Job trên Spot tải đúng input, chạy cùng pipeline `src/training/` như local, ghi metrics/schema/manifest và log run vào MLflow. PostgreSQL giữ Tracking/Registry metadata; MLflow proxy artifact lên S3 Model Artifacts. Validation gate chỉ chấp thuận candidate nếu so sánh được trên cùng validation sample; test MAE chỉ báo cáo. Approved version là đầu vào release, không tự động chọn latest.

## Luồng phát hành

Release nhận exact approved name/version qua reusable call từ Training, hoặc `workflow_dispatch` bảo trì trên `main`. GitHub OIDC lấy AWS role tạm thời. Runner tạm thêm đúng public IPv4 `/32` của mình vào EKS API CIDRs, dùng `kubectl port-forward` tới cloud MLflow, kiểm tra version READY/approved và tải artifacts. Bundle có model, schema, provenance và checksum; serving image được build/push lên ECR. Runner commit một lần cập nhật digest/metadata trong GitOps Deployment và expected smoke identity trên Rollout metadata. Argo CD sync tự động; runner so actual Pod imageID, readiness và prediction, rồi mới xác nhận alias `champion`. Bước `always()` khôi phục CIDRs gốc. Candidate bị Rollouts từ chối được runner guarded-revert rồi kiểm tra exact stable digest. Timeout/inconclusive hoặc lỗi verification sau promote không được coi là đã rollback; cần runbook xử lý.

Argo CD Application `inference-api` theo dõi chính xác `deploy/gitops/inference`. HPA sở hữu `Rollout.spec.replicas`; Application bỏ qua trường đó và dùng `RespectIgnoreDifferences=true`, `selfHeal=true`, `prune=false`.

## Luồng request và quan sát

Service `inference-api` là stable ClusterIP port 8000, `inference-api-canary` dành cho candidate. ALB internal dùng weighted target groups, còn gọi trực tiếp stable Service không đo được split. FastAPI validate input và timezone, dùng `training.features.preprocess_request` để tạo đúng năm features và LightGBM categorical dtypes, trả thời lượng phút cùng version. Model load một lần khi process bắt đầu; image không phụ thuộc MLflow/S3 trên mỗi request. `/metrics` xuất request count, latency và error metrics với labels cardinality thấp. Metrics Server cấp CPU cho HPA (1–3 Pods, target 60% CPU request). Prometheus scrape từng inference Pod, giữ dữ liệu 24 giờ trên `emptyDir`; Grafana lấy datasource qua Service DNS. Không có public inference/MLflow/monitoring Ingress.

## Progressive delivery và phạm vi còn lại

Phase 4 thêm ALB internal, Argo Rollouts (`Rollout` nhận Deployment qua `workloadRef`), AnalysisTemplate theo Prometheus và `git revert` khôi phục stable; xem [phase-04.md](phases/phase-04.md). Phase 5 chưa có drift detection hoặc retraining trigger. Một replica PostgreSQL, một NAT và metrics ephemeral chưa đáp ứng HA.

## Cloud training và orchestration

`ci.yml` giữ Source CI trên push/PR. Automatic caller chỉ chạy sau Final CI status success trên main push có identity hợp lệ và `AUTO_MLOPS_ENABLED=true`. Nó gọi `train.yml` tại cùng commit; fingerprint so Git inputs với checkpoint đã xác minh, không dựa riêng vào commit cuối. Intent artifact được lưu trước cloud; rejected là terminal decision hợp lệ, approved/READY mới truyền exact name/version/run/checksum sang `release.yml`. Không có latest-version lookup hoặc tự tăng version number.

Train và Release dùng OIDC roles riêng. Khóa outer `auto-mlops-<cluster>` giữ cả chain; khóa `eks-<cluster>-mutation` chỉ ở các jobs thực sự mở/khôi phục CIDRs. Hai khóa khác nhau tránh caller chờ child cùng lock. Exact checkout/current-main guards, fast-forward push và verifier revision guards chặn stale release. Checkpoint bounded/validated và blocked state hạn chế duplicate retry hoặc GitOps loops; không là exactly-once distributed transaction.

Release revalidate handoff với live Registry/artifacts, rồi dùng cùng bundle/build/GitOps/Argo/gates/guarded revert đã có. Job tạo traffic chỉ tới ALB internal,40RPS/20VUs/600s, trong namespace riêng có quota, không AWS identity; setup readiness trước GitOps commit. Host cần traffic Job active khi chờ rollout và trước champion update. Prometheus gates vẫn quyết định candidate, không biến4xx thành server error. EXIT/always cleanup workload và CIDRs; failure sau promotion hoặc khi runner/API mất phải được đối chiếu thực tế.

Auto mặc định tắt, manual training/release còn nguyên. Cần bootstrap thêm RBAC/namespace traffic trước khi bật; source mới chưa cloud verified. [Thiết kế, inputs, checkpoint và vận hành chi tiết](automated-pipeline.md).
