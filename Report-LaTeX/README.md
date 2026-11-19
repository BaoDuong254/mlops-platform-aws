# Báo cáo LaTeX — Quản lý và triển khai mô hình học máy trên AWS

`main.pdf` là bản đã biên dịch. Báo cáo dùng trực tiếp nội dung `../report.md`, với định dạng đối chiếu `../Report/main.tex`, `../Report/Sections/preamp.tex` và bản PDF mẫu. Bản Markdown gốc, ảnh gốc và template tham khảo được giữ nguyên.

## Biên dịch và chỉnh thông tin bìa

```bash
cd Report-LaTeX
make
make verify
```

Dùng **XeLaTeX** và `main.tex` làm tài liệu chính. `make` tạo `build/main.pdf` và cập nhật `main.pdf`. Các font đã nằm trong `Fonts/`, có thể tải toàn bộ thư mục hoặc ZIP lên Overleaf mà không cần cài font riêng. Nếu không có latexmk, chạy XeLaTeX ít nhất hai lần để cập nhật mục lục/tham chiếu.

Thông tin đề tài, GVHD Nguyễn Quang Hùng, lớp L01, nhóm 02 và bốn sinh viên đã được điền trong `metadata.tex`. Có thể sửa tại đây khi cần cập nhật, cùng với tháng báo cáo và năm học. Tiêu đề xuống hai dòng để cân đối phần chữ màu xanh.

## Định dạng áp dụng theo template

- Khổ A4 dọc, vùng chữ 160 × 240 mm, lề trái/trên 30 mm. Không xoay trang hoặc xoay hình.
- Bìa dùng khung đôi cùng tọa độ/nét với mẫu; logo ở kích thước tự nhiên khoảng 67,7 mm. Tên trường/khoa khoảng 14,4 pt, tiêu đề báo cáo/đề tài khoảng 17,3 pt, thông tin thực hiện và ngày tháng 12 pt. Đề tài màu xanh `blue`, thông tin thực hiện màu đen.
- Font **Computer Modern Unicode**, cùng họ Computer Modern/VNR của mẫu; thay font Times của bản trước. Font Roman/đậm/nghiêng và Typewriter được đóng gói trong `Fonts/`. Nguồn và giấy phép: [CTAN Computer Modern Unicode](https://ctan.org/pkg/cm-unicode), SIL Open Font License trong `Fonts/OFL.txt`. Checksum tại `Fonts/manifest.json`.
- Nội dung 13 pt, baseline 18 pt với giãn dòng 1,3; thụt đầu dòng 1 cm. Header/footer kiểu monospace như mẫu, số trang/tổng trang màu đỏ.
- Chương là **I–V** với số La Mã in hoa, đặt liên tục theo nội dung thay vì bắt buộc mở trang mới. Các mục và số hình/bảng giữ hệ đánh số tương ứng nguồn, ví dụ mục 4.1, Hình 4.1. Trang được đánh số liên tục sau bìa.
- Chỉ có mục lục; không in danh mục hình hoặc danh mục bảng.
- Cả 12 bảng có viền ngoài, đường kẻ dọc giữa các cột và đường ngang giữa các hàng. Chữ bảng 12 pt, caption đặt dưới bảng, nhãn đậm và phần mô tả nghiêng như mẫu.
- Hình/screenshot co theo tỷ lệ thực, rộng tối đa khoảng 94% vùng chữ; sơ đồ kiến trúc dùng hết chiều rộng. Không dùng `landscape`, không ép mỗi hình thành một trang riêng và không dùng `figure[H]`.

## Mapping hình và kiểm tra

| Hình | Nguồn trong `../images/` |
|---|---|
| 2.1 | `Screenshot From 2026-11-17 21-24-24.png` |
| 2.2 | **`img_4.png`** — ảnh sơ đồ vòng đời mô hình do người dùng cung cấp |
| 3.1 | **`img_3.png`** — ảnh sơ đồ mạng AWS/EKS do người dùng cung cấp |
| H01 / 4.1 | `img_1.png`, `Screenshot From 2026-11-19 12-28-34.png` |
| H02 / 4.2 | `Screenshot From 2026-11-19 13-31-01.png`, `Screenshot From 2026-11-19 13-30-15.png` |
| H03 / 4.3 | `Screenshot From 2026-11-19 12-39-12.png` |
| H04 / 4.4 | `Screenshot From 2026-11-19 13-50-18.png`, `Screenshot From 2026-11-19 12-45-29.png` |
| H05 / 4.5 | `Screenshot From 2026-11-19 12-35-12.png` |
| H06 / 4.6 | **`Screenshot From 2026-11-19 17-30-34.png`** — Monitoring & HPA |
| H07 / 4.7 | **`Screenshot From 2026-11-19 17-30-23.png`** — Failed Canary, Stable/Candidate v3; **`Screenshot From 2026-11-19 13-14-13.png`** — Git revert commit sau Failed/Abort |
| H08 / 4.8 | **`img_5.png`** — Successful Canary v2→v3 (ảnh mới) |
| H09 / 4.9 | `Screenshot From 2026-11-19 12-49-02.png`, `Screenshot From 2026-11-19 13-23-14.png` |

Ảnh được sao chép nguyên tệp vào `Pictures/`, có checksum tại `source/assets.json`. Crop chỉ dùng `trim,clip` lúc hiển thị, không sửa metrics/trạng thái hoặc pixel gốc. Các tọa độ crop và giới hạn kích thước nằm trong `scripts/convert_report.py`.

Hình 3.1 được chặn bằng `FloatBarrier` để nằm trong mục 3.1.1, trước tiêu đề 3.1.2. Các dòng dẫn chiếu riêng trước hình đã được bỏ; caption và nhãn hình giữ nguyên.

Hai khối Mermaid trong Markdown được thay bằng **ảnh có sẵn**, không sinh TikZ/SVG hoặc vẽ lại. Thư mục sơ đồ tự dựng của bản trước đã được loại khỏi source báo cáo. TikZ trong preamble chỉ phục vụ khung trang bìa. H06–H08 có ghi chú dữ liệu lịch sử tái hiện bằng Grafana TestData CSV Content; caption chính thức được giữ nguyên.

`make verify` cần Python 3, markdown-it-py, Pillow, pdftotext và pdfinfo. Công cụ kiểm tra 50 tiêu đề, 147 đoạn nội dung, 169 ô bảng; checksum ảnh/font; 12 caption/label; ánh xạ Grafana; ba ghi chú dữ liệu lịch sử; bảng có lưới; số chương La Mã; không có trang xoay, trang trống hoặc danh mục hình/bảng. Log được kiểm tra lỗi, thiếu ký tự và tràn dòng. Khoảng trắng, dấu nối ngắt dòng và ligature được chuẩn hóa khi đối chiếu văn bản.

- `source/report.md`: bản chụp nguyên vẹn của Markdown nguồn.
- `source/image-review.json`: khảo sát 27 PNG và hai tệp TXT minh chứng.
- `source/content-integrity.json`: ghi nhận cập nhật các phần III, 4.4.4 và V từ Markdown mới; đối chiếu các phần giữ nguyên và xác nhận định dạng được bảo toàn.
- `source/pdf-verification.json`: kết quả kiểm tra PDF cuối.

Không chạy `make regenerate` sau khi sửa nội dung trực tiếp trong các file chương: lệnh này sinh lại các chương từ `source/report.md`. Các quy tắc định dạng đã được cập nhật trong trình chuyển đổi để tái sinh đúng kiểu chương/bảng và sử dụng ảnh sơ đồ gốc; bìa được quản lý riêng trong `Sections/cover.tex`.
