# Image identities và realtime monitoring

## Image selection

Lỗi cũ có hai nguyên nhân: một hash dùng chung gồm cả training config, và shell glob/sort phụ thuộc locale. `scripts/image_identity.py` dùng danh sách đường dẫn sắp theo byte UTF-8, hash nội dung, tên file và executable bit. Không đọc cache `__pycache__`. Các đầu vào:

| Image | Build inputs |
|---|---|
| Training | `deploy/training/Dockerfile`, toàn bộ `src/`, `configs/`, `pyproject.toml`, `uv.lock`, `.dockerignore` |
| MLflow | `deploy/mlflow/Dockerfile`, `deploy/mlflow/server.py`, `pyproject.toml`, `uv.lock`, `.dockerignore` |
| Backup | `deploy/backup/Dockerfile`, `deploy/backup/backup.py`, `.dockerignore` |

Training Dockerfile thực tế COPY cả `src/` nên fingerprint bao gồm cả package inference. MLflow vẫn cài shared lock; thay đổi lock có thể làm đổi MLflow image, nhưng đổi training config không làm đổi MLflow/Backup. Fingerprint định danh build inputs; digest ECR định danh artifact thực tế. GitHub Training vẫn dùng tag theo run và truyền provenance SHA riêng; chỉ chuẩn hóa sort bằng `LC_ALL=C`. Inference Release vẫn pin digest theo cơ chế cũ.

```bash
make cloud-image-tags                         # không AWS, không Docker build
make cloud-images-build                       # build cả ba với tag độc lập
make cloud-images-push                        # push và ghi digest lock
make cloud-images-build IMAGE_PURPOSE=backup  # hoặc training / mlflow
make cloud-images-push IMAGE_PURPOSE=backup
```

Push lưu các refs immutable tại `artifacts/images/m23-image-lock.json` (Git-ignored); có thể đặt `IMAGE_LOCK_OUTPUT` để lưu nơi khác. Luồng mặc định tự tra ECR bằng fingerprint, không phụ thuộc file này; chỉ sao lưu nếu muốn dùng override lock. Push một purpose chỉ cập nhật entry đó.

Override tùy chọn có thứ tự ưu tiên cho deploy/job/train: `TRAINING_IMAGE_REF` / `MLFLOW_IMAGE_REF` / `BACKUP_IMAGE_REF` → `IMAGE_TAG` legacy → `IMAGE_LOCK_FILE` được chỉ định → tag fingerprint riêng `m23-<purpose>-<hash>`. Lock không được tự chọn ngầm. Không fallback sang latest, image mới nhất hoặc image ngẫu nhiên. Mọi ref phải là private ECR có tag cụ thể hoặc `@sha256:<digest>`; kiểm tra tồn tại và chuyển thành digest trước khi chạy Pod. Override từng ref có ưu tiên cao nhất; bỏ `*_IMAGE_REF` và `IMAGE_LOCK_FILE` khi publish images.

## Migration một lần và tự tìm image trên ECR

Luồng mặc định không cần nhập tag, digest hoặc file lock: script tính tag `m23-<purpose>-<fingerprint>`, tìm đúng tag trên ECR rồi chuyển sang digest. Nó không dùng latest hoặc chọn theo thời gian push. `make cloud-images-resolve` kiểm tra cả ba image mà không deploy hay build.

Với images M23/CI cũ chưa có tag fingerprint mới:

```bash
make cloud-images-migrate-plan  # chỉ đọc ECR/Git; xem artifact migration-report.json
make cloud-images-migrate       # thêm tag vào manifest cũ, giữ nguyên digest và tag cũ
make cloud-images-resolve       # tự tính và tự tìm, không cần override
```

Migration đối chiếu toàn bộ build inputs với Git history đáng tin cậy. Với tag M23 cũ, script tái tính hash legacy theo locale từng dùng để nhận diện source tương đương. Với Training CI, đối chiếu commit SHA trong tag và nội dung source/config/dependencies. Thay đổi Docker ignore policy chỉ được chấp nhận nếu không thay đổi các COPY inputs. Khác nội dung hoặc có nhiều digest phù hợp thì dừng, không tự lấy image mới nhất. Git history phải đầy đủ; tìm kiếm được giới hạn 512 commits. Đây là đối chiếu theo hợp đồng publish của repository/ECR đang được tin cậy, không thay thế chữ ký supply-chain của bên thứ ba.

Sau migration, các thao tác bình thường dùng cùng default automatic lookup, kể cả trên cluster mới:

```bash
BACKUP_KEY=$(cat backups/cloud-last-key.txt) # key của backup đã kiểm chứng
read -rsp 'New MLflow DB password: ' MLFLOW_DB_PASSWORD; echo
export MLFLOW_DB_PASSWORD
RESTORE_KEY="$BACKUP_KEY" make cloud-deploy && make cloud-verify
unset MLFLOW_DB_PASSWORD

make cloud-backup
make cloud-restore-check BACKUP_KEY='postgres/<backup>.dump'
make m23-restore BACKUP_KEY='postgres/<backup>.dump' # primary restore yêu cầu MLflow đã dừng
make tf-platform-down                            # có xác nhận; backup + verify trước xóa PVC
```

Deploy kiểm tra MLflow và Backup trên ECR trước khi tạo PostgreSQL khi có RESTORE_KEY. Không bỏ qua checksum/metadata hoặc tạo DB rỗng nếu S3 đang có backup. Dữ liệu model artifacts và PostgreSQL backups tiếp tục dùng buckets persistent cũ.

Các biến `*_IMAGE_REF`, `IMAGE_TAG` legacy và `IMAGE_LOCK_FILE` vẫn là override tùy chọn phục vụ recovery đặc biệt, không phải bước bắt buộc. File lock do push tạo chỉ là bản ghi artifact tùy chọn; không cần export file này trong luồng mặc định. Khi đổi build inputs thực sự, build/push đúng purpose để tạo phiên bản mới. Inference Release không thay đổi cách pin digest.

## Hai dashboard realtime

| Dashboard / UID | Panels và nguồn dữ liệu |
|---|---|
| Inference Load & HPA / `m33-load-hpa` | RPS, P95 (ms), app HTTP 5xx từ FastAPI `/metrics`; Current/Desired replicas và CPU utilization/target từ kube-state-metrics |
| Canary Deployment & Performance / `m33-canary` | RPS, P95 (ms) và app 5xx theo `rollouts_pod_template_hash` từ FastAPI |

Bố cục giữ cách đọc của ảnh CSV trong báo cáo: load → CPU/replicas và các biểu đồ so sánh ReplicaSet theo thời gian. Failed và Successful là hai kịch bản trên dashboard Canary, không nhân đôi cùng một realtime dashboard. Các bản CSV lịch sử giữ riêng.

Prometheus chart `29.37.1` đã có dependency kube-state-metrics `8.6.0` / app `2.20.0`; chỉ bật collectors HPA và Pods trong `mlops`, dùng Role/RoleBinding list/watch tại namespace đó, không thêm ClusterRole hay node/cAdvisor scraping. CPU lấy trực tiếp HPA status `averageUtilization`, target từ spec; không suy từ request rate, không thay Metrics Server hay HPA controller. CPU là % CPU requests, có thể lớn hơn 100%.

Chưa có exporter ánh xạ Rollout roles, nên legend ghi đúng RS hash. Đọc `make canary-status` để đối chiếu `stableRS` và `currentPodHash`; Promotion có thể đổi vai trò của cùng hash. Không gắn nhãn Stable/Canary dựa vào model version hoặc thứ tự series.

RPS/error dùng rate `[1m]`, P95 `[2m]` như AnalysisTemplate; scrape và refresh 15 giây, mặc định xem 15 phút. Ngưỡng tham chiếu candidate: RPS ≥ 0.2, P95 < 500 ms, app 5xx < 1%; gate tương đối vẫn là P95 < 1.5 × stable P95 + 50 ms. Dashboard không quyết định Promotion/Abort và không sửa AnalysisTemplate. Missing data không chuyển thành zero; zero error chỉ hiển thị nếu có request quan sát được. Idle không có request sẽ không có error rate hợp lệ. Throughput không phải offered load của k6; app metrics không bao gồm lỗi do ALB tự sinh. Dashboard realtime chưa có ALB Traffic Weights vì chưa thu thập cấu hình listener rules từ AWS. Muốn biểu đồ như historical cần một collector chỉ đọc `elbv2:DescribeRules`, ánh xạ TargetGroup ARN sang Service bằng TargetGroupBinding và xuất weights cho Prometheus; không suy weights từ request rate hay dùng annotation Ingress để khẳng định trạng thái thực tế trên ALB.

## Deploy, migration và kiểm chứng

```bash
# Đặt mật khẩu ngoài Git như trước. Không cần load test hoặc release.
read -rsp 'Grafana admin password: ' GRAFANA_ADMIN_PASSWORD; echo
export GRAFANA_ADMIN_PASSWORD
make monitoring-deploy
unset GRAFANA_ADMIN_PASSWORD
make monitoring-verify
```

`monitoring-deploy` (và cleanup khi Grafana còn tồn tại) xuất các dashboard TestData/CSV hiện có và TestData datasources trước khi thay Secret/recreate Grafana; API lỗi sẽ dừng trước Helm upgrade. Archive nằm ở `artifacts/monitoring/historical/` (Git-ignored). ConfigMaps `m33-historical-dashboards` và `m33-historical-datasources` provision lại CSV tại folder `MLOps Historical`; không thay CSV/query/units. Giữ archive này khi chuyển máy. Giới hạn ConfigMap được kiểm tra trước upgrade; export quá lớn phải provision riêng.

ConfigMap realtime `m33-inference-dashboard` chứa đúng hai JSON mới. Provider `inference` cho phép xóa file-provisioned UID cũ `m33-inference`; provider historical giữ `disableDeletion: true`. Không xóa dashboard lịch sử thủ công trên UI. Lưu ý Grafana/Prometheus vẫn ephemeral như thiết kế cũ; việc giữ archive ngoài cluster là cần thiết khi teardown.

Kiểm tra offline:

```bash
make check
make m33-test
make workflows-check
uv run --locked python scripts/check_grafana_provisioning.py # local Docker, không AWS
uv run --locked python scripts/check_dashboard_metrics.py
uv run --locked python scripts/check_canary_metrics.py
```

Promtool chạy queries thật từ hai JSON với fixtures synthetic chỉ cho unit tests, kiểm tra RS khác nhau, zero lỗi theo RS, idle/missing data và HPA replicas. Fixtures không provision vào Grafana. Helm render kiểm tra namespace/RBAC, ConfigMap mounts và Prometheus config. `monitoring-verify` khi có cluster kiểm tra datasource health, hai UID được provision, mọi PromQL target trả success, kube-state-metrics UP, current/desired HPA có dữ liệu và UID realtime cũ đã biến mất; panel không có dữ liệu được báo rõ, không giả định pass cho metric chưa có mẫu.

Cần endpoint EKS truy cập được từ IP quản trị để deploy/verify. Static checks hoặc Grafana local không chứng minh metrics đã được scrape trên EKS. Không tự chạy load test hay Training/Release E2E để tạo mẫu. Việc xác minh CPU thay đổi theo tải, cả Stable/Canary cùng hoạt động và Promotion/Abort cần một trial được cho phép riêng.
