# F.Angine là gì và cơ chế vận hành ra sao?
# vậy 5.Angine là gì?
- Một xưởng tự động lập trình web và game mini chạy trực tiếp trên Termux (Android), điều khiển qua giao diện Web.

2. Quy trình làm việc của 5 AI:
- Inquiry (Khảo sát): Rà soát đề bài, hỏi lại nếu thiếu thông số.
- Manager (Quản lý): Đọc code cũ, lập kiến trúc và chia nhỏ dự án thành nhiều giai đoạn (Task Mode).
- Dev (Lập trình): Viết code hoàn chỉnh 100%, tích hợp CSS/JS, tối ưu cảm ứng di động.
- Testing (Kiểm thử): Sandbox tự soát lỗi đóng thẻ HTML/JS và tự vá lỗi (Auto-Heal).
- Checker (Nghiệm thu): Chỉ duyệt (APPROVED) khi code chuẩn; nếu lỗi (REVISE) ép Dev sửa lại tại chỗ.

3. Điểm mạnh cốt lõi:
- Chạy ngầm bền bỉ: Tắt web, reload trang vẫn không ngắt tiến trình.
- Đa phiên độc lập (Multi-Session): Mỗi đoạn chat có mã nguồn và lịch sử riêng.
- Tự xoay 3 API Key: Không lo sập do cạn hạn mức (429) hay máy chủ bận (503).
- Xem Web trực tiếp: Có nút mở tab chơi game hoặc dùng web ngay lập tức.
# Vậy Cách vận Hành Như Nào?
Dưới đây là kiến trúc vận hành của hệ thống "5-AI Engine Multi-Session Studio" chạy trên Termux (mã nguồn: https://github.com/lequoctienbtm-maker/F.Angine/blob/main/server.py):

1. Luồng xử lý dữ liệu:
- Khi người dùng gửi yêu cầu qua giao diện Web, yêu cầu được đẩy vào hàng đợi ngầm (Queue) gắn liền với Session ID của phiên làm việc đó.
- Inquiry AI kiểm tra tính rõ ràng của đề bài; nếu thiếu thông số cốt lõi sẽ tạm dừng để hỏi người dùng làm rõ.
- Manager AI nạp mã nguồn cũ từ thư mục riêng của phiên (Codebase Memory), thiết kế kiến trúc State-Machine và chia dự án thành nhiều giai đoạn phát triển lũy tiến (nếu bật Task Mode).
- Dev AI lập trình toàn bộ mã nguồn gom trọn trong 1 khối code, ép buộc đóng kín toàn bộ các thẻ HTML/JS và hỗ trợ thao tác chạm mobile.
- Testing AI chạy kiểm thử Sandbox tự động (soát thẻ DOM, bắt stderr) kèm cơ chế Auto-Heal tự vá thẻ đóng nếu code bị chạm trần token.
- Checker AI đóng vai trò QA Lead, chỉ cấp phép "STATUS: APPROVED" khi code hoàn thiện. Nếu "STATUS: REVISE", hệ thống kích hoạt vòng lặp ép Dev sửa lại tối đa 3 lần trước khi bàn giao.

2. Tính năng kỹ thuật nền tảng:
- Đa phiên độc lập (Multi-Session): Mỗi phiên sở hữu thư mục lưu trữ riêng (workspaces/<sid>/) gồm file mã nguồn index.html và file lịch sử history.json độc lập.
- Chạy ngầm bền bỉ (Asynchronous Worker): Tiến trình Python chạy độc lập với trình duyệt, reload trang hoặc mất kết nối không làm gián đoạn luồng làm việc.
- Tự động xoay vòng Key & Model: Tự luân chuyển giữa 3 API Key từ 3 Gmail và 4 model Flash để vượt lỗi 429 (hết hạn mức) và 503 (server bận).
  # lệnh gửi AI cho Nó giải thích và phân tích
Hãy dựa vào cấu trúc trên để phân tích, giải thích hoặc hướng dẫn cách vận hành hệ thống này một cách trực quan, mạch lạc bằng tiếng Việt.
