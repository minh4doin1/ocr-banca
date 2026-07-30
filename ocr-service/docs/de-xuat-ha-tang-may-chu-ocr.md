                    CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM
                           Độc lập - Tự do - Hạnh phúc
                    ────────────────────────────────────

NGÂN HÀNG NÔNG NGHIỆP VÀ PHÁT TRIỂN NÔNG THÔN VIỆT NAM
(AGRIBANK)
……………………………                        ………, ngày …… tháng …… năm 20……

Số: ……/……-……
V/v: Đề nghị cấp phát máy chủ phục vụ hệ thống
     nhận dạng ký tự quang học (OCR)

                                         TỜ TRÌNH
                    Về việc đề nghị cấp phát máy chủ phục vụ hệ thống OCR

Kính gửi: ………………………………………………………………………………

---

## I. CĂN CỨ ĐỀ XUẤT

1. Nhu cầu triển khai hệ thống hỗ trợ số hóa dữ liệu từ tài liệu PDF, Word, Excel phục vụ nghiệp vụ tạo danh sách người dùng SSO gắn với hệ thống Banca; đồng thời hình thành nền tảng xử lý tài liệu dùng chung cho các nghiệp vụ có cùng tính chất trong lộ trình mở rộng của Agribank.

2. Đặc thù kỹ thuật của hệ thống: xử lý tài liệu gắn với bộ nhớ đồ họa (GPU) NVIDIA, bộ xử lý trung tâm (CPU), bộ nhớ trong (RAM) và ổ lưu trữ tốc độ cao; dữ liệu thuộc phạm vi thông tin nội bộ, xử lý trên hạ tầng của Ngân hàng.

3. Phương pháp xác định cấu hình: căn cứ nhu cầu tài nguyên vận hành; áp dụng hệ số dự phòng công suất **1,2** theo thông lệ thiết kế hệ thống có tải biến thiên và chu kỳ cao điểm.

---

## II. MỤC ĐÍCH SỬ DỤNG MÁY CHỦ

Máy chủ đề nghị cấp phát thực hiện các chức năng sau:

1. Tiếp nhận và xử lý tệp đầu vào định dạng PDF, Word (.docx), Excel (.xlsx/.xlsm).
2. Trích xuất dữ liệu bảng: đọc trực tiếp từ PDF có lớp văn bản; nhận dạng quang học đối với PDF dạng ảnh/scan.
3. Quản lý hàng đợi phiên xử lý khi có nhiều người dùng đồng thời.
4. Lưu trữ tạm thời tệp tải lên, ảnh trang, kết quả trung gian và tệp xuất.
5. Cung cấp dịch vụ xử lý tài liệu trong mạng nội bộ của Ngân hàng cho các đơn vị và hệ thống nghiệp vụ được phép kết nối.

---

## III. CƠ SỞ TÍNH TOÁN CẤU HÌNH

### 1. Nhu cầu tài nguyên theo thành phần xử lý

| Thành phần | Tài nguyên sử dụng chính | Yêu cầu kỹ thuật |
|---|---|---|
| Nhận dạng bố cục và bảng | Bộ nhớ GPU | Không thấp hơn 4 GB VRAM khi chạy pipeline đầy đủ |
| Nhận dạng chữ tiếng Việt | Bộ nhớ GPU | Chạy đồng thời với nhận dạng bố cục trên cùng GPU |
| Chuyển PDF sang ảnh trang | CPU, RAM, ổ đĩa | Xử lý song song với nhận dạng |
| Hàng đợi và giao diện lập trình ứng dụng (API) | CPU, RAM | Phục vụ nhiều phiên làm việc đồng thời |
| Lưu trữ phiên xử lý và tệp xuất | Ổ đĩa thể rắn | Ghi/đọc liên tục trong thời gian lưu trữ theo quy định |

Tổng nhu cầu bộ nhớ GPU khi vận hành đồng thời các thành phần nhận dạng trên một máy chủ không thấp hơn **8 GB VRAM**. Sau khi áp dụng hệ số dự phòng công suất 1,2 và tính đến nhiều phiên xử lý đồng thời, cấu hình đề nghị là **16 GB VRAM**.

### 2. Quy mô tải giai đoạn triển khai ban đầu

| Chỉ tiêu | Giá trị tính toán |
|---|---|
| Số người dùng đồng thời tại thời điểm cao điểm | 05 – 15 |
| Số phiên OCR đang xử lý đồng thời | 02 – 04 |
| Số trang tài liệu điển hình mỗi tệp | 03 – 20 trang |
| Thời gian lưu kết quả trên đĩa | 30 ngày |
| Số phiên xử lý tham chiếu để tính dung lượng | 100 phiên/ngày |

### 3. Quy mô định hướng khi mở rộng toàn hệ thống

| Chỉ tiêu | Giá trị định hướng |
|---|---|
| Số người dùng đồng thời tại thời điểm cao điểm | 50 – 150 |
| Số phiên OCR đang xử lý đồng thời | 10 – 30 |
| Số hệ thống nghiệp vụ sử dụng dịch vụ OCR dùng chung | Từ 02 hệ thống trở lên |
| Phương thức mở rộng công suất | Tăng số máy chủ worker GPU; tách lớp tiếp nhận API khỏi lớp nhận dạng |

Cấu hình máy chủ giai đoạn này đáp ứng tải triển khai ban đầu và bảo đảm khả năng tích hợp vào kiến trúc mở rộng nêu tại mục V.

### 4. Tính toán dung lượng lưu trữ dữ liệu

| Hạng mục | Cách tính | Kết quả |
|---|---|---|
| Phần mềm, mô hình nhận dạng, môi trường chạy | Theo dung lượng cài đặt | 40 GB |
| Một phiên xử lý (ảnh trang, kết quả, tệp xuất), tệp 10 trang | Theo đặc trưng vận hành | 50 MB – 150 MB |
| 100 phiên/ngày × 30 ngày | Theo cận trên 150 MB/phiên | 450 GB |
| Áp dụng hệ số dự phòng 1,2 | 450 GB × 1,2 | 540 GB |
| Dung lượng ổ dữ liệu đề nghị | Làm tròn theo chuẩn thiết bị lưu trữ | **1.000 GB (1 TB)** |

---

## IV. CẤU HÌNH MÁY CHỦ ĐỀ NGHỊ CẤP PHÁT

### 1. Thông số kỹ thuật

| Hạng mục | Thông số đề nghị | Cơ sở xác định |
|---|---|---|
| Số lượng | 01 máy chủ | Điểm xử lý tập trung giai đoạn triển khai ban đầu |
| Vai trò | Máy chủ xử lý OCR trong mạng nội bộ | Theo mục II |
| Bộ xử lý đồ họa (GPU) | NVIDIA; bộ nhớ video tối thiểu **16 GB**; hỗ trợ CUDA; compute capability không thấp hơn 7.0 | Nhu cầu nhận dạng đồng thời; hệ số dự phòng 1,2; nhiều phiên đồng thời |
| Bộ xử lý trung tâm (CPU) | Từ **12** đến **16** nhân vật lý | Chuyển đổi PDF, API và hàng đợi song song với GPU |
| Bộ nhớ trong (RAM) | **64 GB** | Ảnh trang trong bộ nhớ; nhiều phiên trong hàng đợi; hệ số dự phòng 1,2 |
| Ổ đĩa hệ thống | NVMe SSD dung lượng **512 GB** | Hệ điều hành, phần mềm, mô hình nhận dạng |
| Ổ đĩa dữ liệu | NVMe SSD dung lượng **1 TB** | Theo mục III.4 |
| Kết nối mạng | Cổng mạng **1 Gbps**; địa chỉ IP tĩnh nội bộ | Truyền tệp tài liệu; kết nối máy trạm và hệ thống nghiệp vụ |
| Nguồn điện và tản nhiệt | Đáp ứng vận hành liên tục với GPU chuyên dụng | Yêu cầu máy chủ gắn GPU |
| Hệ điều hành | Theo chuẩn hạ tầng máy chủ của Agribank | Phù hợp chính sách CNTT của Ngân hàng |

### 2. Yêu cầu kèm theo khi cấp phát và đưa vào sử dụng

1. Cài đặt driver NVIDIA và thư viện CUDA/cuDNN phù hợp môi trường nội bộ của Ngân hàng.
2. Bố trí máy chủ trong vùng mạng được kiểm soát; chỉ cho phép kết nối từ mạng nội bộ hoặc kênh truy cập đã được phê duyệt.
3. Cấp địa chỉ IP tĩnh và tên máy theo DNS nội bộ.
4. Thiết lập giám sát tài nguyên: CPU, GPU, RAM, dung lượng ổ đĩa, nhiệt độ GPU.
5. Thực hiện sao lưu thư mục dữ liệu kết quả theo quy định lưu trữ của đơn vị quản lý hệ thống.

---

## V. ĐỊNH HƯỚNG MỞ RỘNG CÔNG SUẤT

Khi số đơn vị sử dụng và số hệ thống nghiệp vụ kết nối tăng, công suất được mở rộng theo hướng tách lớp chức năng như sau:

| Giai đoạn | Phạm vi | Thành phần hạ tầng |
|---|---|---|
| Giai đoạn 1 | Triển khai ban đầu | 01 máy chủ theo cấu hình mục IV |
| Giai đoạn 2 | Mở rộng nhiều đơn vị; từ 02 hệ thống nghiệp vụ trở lên | Cụm máy chủ API (tối thiểu 02 máy) và tối thiểu 02 máy chủ worker GPU; kho lưu trữ dùng chung từ 2 TB trở lên |
| Giai đoạn 3 | Triển khai quy mô toàn quốc | Cụm API có dự phòng; số máy chủ worker GPU xác định theo độ sâu hàng đợi thực tế; cơ sở dữ liệu lưu metadata phiên xử lý; sao lưu và kiểm tra phục hồi theo quy định |

**Thông số định hướng mỗi máy chủ worker GPU (từ giai đoạn 2):**

| Hạng mục | Thông số |
|---|---|
| GPU | 01 card NVIDIA; bộ nhớ video từ 16 GB đến 24 GB |
| CPU | 16 nhân |
| RAM | Từ 64 GB đến 128 GB |
| Ổ đĩa local | NVMe 1 TB (lưu tạm), kết hợp kho lưu trữ dùng chung |
| Mạng | Tối thiểu 1 Gbps |

Số lượng worker GPU tại giai đoạn 2 và giai đoạn 3 được xác định trên cơ sở số liệu vận hành thực tế về độ sâu hàng đợi và thời gian xử lý trung bình của giai đoạn 1.

---

## VI. YÊU CẦU VỀ AN TOÀN THÔNG TIN VÀ VẬN HÀNH

1. Máy chủ đặt tại khu vực được kiểm soát vật lý và logic theo quy định an toàn hệ thống thông tin của Agribank.
2. Không công khai dịch vụ OCR ra môi trường Internet công cộng.
3. Quản lý quyền truy cập dịch vụ theo nguyên tắc đúng đối tượng, đúng chức năng được giao.
4. Nhật ký vận hành và kết quả xử lý được lưu trữ đủ thời gian theo quy định để phục vụ kiểm tra, đối soát khi có yêu cầu.
5. Tuân thủ các quy định hiện hành về an toàn hệ thống thông tin trong hoạt động ngân hàng.

---

## VII. NỘI DUNG ĐỀ NGHỊ

Kính đề nghị cấp có thẩm quyền xem xét, phê duyệt:

1. Cấp phát **01 máy chủ** theo đúng thông số kỹ thuật tại mục IV.1.
2. Hỗ trợ lắp đặt, kết nối mạng nội bộ, cấp địa chỉ IP tĩnh, cài đặt driver GPU và đưa máy chủ vào giám sát vận hành theo mục IV.2.
3. Ghi nhận định hướng mở rộng công suất tại mục V để chủ động quy hoạch điện năng, vị trí đặt thiết bị và kho lưu trữ trong kế hoạch hạ tầng các năm tiếp theo.

---

## VIII. KẾT LUẬN

Việc cấp phát máy chủ theo cấu hình nêu tại tờ trình này nhằm đưa hệ thống OCR vào vận hành tập trung, bảo đảm xử lý dữ liệu trên hạ tầng nội bộ của Ngân hàng, đáp ứng tải giai đoạn triển khai ban đầu và bảo đảm tính kế thừa khi mở rộng quy mô sử dụng trong toàn hệ thống.

Kính trình cấp có thẩm quyền xem xét, quyết định./.


Người lập                          Phụ trách đơn vị
(Ký, ghi rõ họ tên)                (Ký, ghi rõ họ tên)



                                     Ý KIẾN PHÊ DUYỆT
                               CỦA CẤP CÓ THẨM QUYỀN


                               ……………………………………
                               (Ký, ghi rõ họ tên, đóng dấu)


Nơi nhận:
- Như kính gửi;
- Lưu: VT, …

---

## PHỤ LỤC
### Bảng tổng hợp cấu hình đề nghị cấp phát

| Hạng mục | Thông số |
|---|---|
| Số lượng | 01 máy chủ |
| GPU | NVIDIA; bộ nhớ video tối thiểu 16 GB; CUDA; compute capability ≥ 7.0 |
| CPU | Từ 12 đến 16 nhân vật lý |
| RAM | 64 GB |
| Ổ hệ thống | NVMe SSD 512 GB |
| Ổ dữ liệu | NVMe SSD 1 TB |
| Mạng | 1 Gbps; IP tĩnh nội bộ |
| Hệ số dự phòng công suất đã áp dụng | 1,2 |
