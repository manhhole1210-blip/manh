# Chạy bot trên VPS Windows 24/24

Mã đã có supervisor, tự chạy lại khi lỗi, giám sát heartbeat và log xoay vòng.
Task Scheduler chạy bằng SYSTEM, tự khởi động khi Windows bật, không phụ thuộc phiên
Remote Desktop. Cần cài trực tiếp trên VPS; mã trong GitHub chưa tự triển khai lên VPS.
Các script PowerShell chưa được chạy trên máy Windows thật trong môi trường kiểm thử Linux.

## Nâng cấp từ bản SQLite cũ

1. Dừng bot CMD bằng Ctrl+C. Nếu đã cài task: mở PowerShell **Run as Administrator**, chạy
   `Stop-ScheduledTask -TaskName LemonCookieBot` và bảo đảm không còn bot chạy.
2. Sao lưu bằng bản cũ: `.venv\Scripts\python.exe sqlite_store.py --backup backups\before-upgrade.db`.
   Giữ thêm bản sao thư mục cấu hình/token ở nơi riêng tư.
3. Giải nén mã mới bên ngoài thư mục bot. Chép toàn bộ tệp mã `.py`, `.ps1`,
   `requirements.txt` và tài liệu vào thư mục bot hiện tại. **Không ghi đè `data`,
   `operations.json`, token/config riêng hoặc `backups`.** Không chép `.venv` từ máy khác.
4. Cài thư viện và chạy thử trong CMD tại thư mục bot:

   ```bat
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   .venv\Scripts\python.exe main.py
   ```

Bản nâng cấp tự sao lưu trước khi chuyển các trường tiền VND sang số nguyên. Số dư
cũ như `10000.0` chuyển chính xác thành `10000`; số dư có phần lẻ như `10000.5` khiến
nâng cấp dừng, chỉ rõ tài liệu chứa lỗi để đối chiếu. Không tự làm tròn hoặc xóa sổ cái. Riêng quy đổi crypto dùng Decimal và quy tắc
làm tròn nửa lên (ROUND_HALF_UP) đến một đồng; số tiền USD được lưu dạng chuỗi thập phân.

## Cấu hình quyền và cảnh báo

Bật Developer Mode trong Discord, nhấp phải server/người dùng/kênh để Copy ID.
Dừng bot trước khi chạy (thay các ID mẫu bằng ID thực):

```bat
.venv\Scripts\python.exe manage_bot.py configure --guild-id SERVER_ID --admin-id YOUR_DISCORD_ID --alert-channel-id PRIVATE_ADMIN_CHANNEL_ID
```

Thêm quản trị khác bằng cách lặp lại `--admin-id`. Người dùng phải vừa nằm trong
`admin_user_ids`, vừa có quyền Administrator trong server nằm trong `allowed_guild_ids`.
Mọi lệnh quản lý tiền, kho, sản phẩm, đối soát và thông báo áp dụng chính sách này;
nút xác nhận cũng kiểm tra lại quyền. Staff ticket vẫn có thể thao tác theo role staff
đã cấu hình trong server được phép. Lệnh và nút trong server khác bị từ chối.

Nếu chưa có `operations.json`, lần chạy đầu tạo cấu hình từ `operations.example.json`.
Hai ID quản trị mặc định là các ID vốn có trong bot gốc. Nếu danh sách server trống,
bot chỉ tự gắn vào server sở hữu `stock_channel_id` hiện có khi đăng nhập thành công.
Nếu không tìm được kênh này, quyền vẫn bị khóa; dùng lệnh configure ở trên. Không tự
lấy server/người dùng đầu tiên gọi lệnh để cấp quyền. Nên cấu hình rõ ID của bạn trước
khi đưa vào hoạt động. Cấu hình local được đọc khi khởi động lại.

Cảnh báo gửi vào kênh quản trị thuộc server được phép. Nếu không gửi được vào kênh,
bot thử DM các ID quản trị đã cấu hình. Không cần webhook hoặc token bổ sung.

## Cài chạy tự động

1. Cài Python 3.12 từ python.org, chọn Add Python to PATH.
2. Đặt bot tại thư mục lưu lâu dài, ví dụ `C:\BOT`. Chuẩn bị token Discord trong
   `input\token.txt`; Task SYSTEM không dùng biến môi trường riêng của tài khoản
   Remote Desktop. Token VCB ở `apinaptien\token.txt`, crypto ở `autocrypto\config.json`
   như trước. Không đưa những tệp này lên GitHub hoặc gửi chúng trong chat.
3. Chạy thử bot, kiểm tra số dư/kho/đơn, sau đó Ctrl+C để dừng.
4. Mở PowerShell **Run as Administrator**, vào thư mục bot và chạy:

   ```powershell
   Set-Location -LiteralPath 'C:\BOT'
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1
   ```

Script cài thư viện nếu cần, đăng ký task `LemonCookieBot`, chạy ngay và chạy khi máy
khởi động. Nếu task đã tồn tại, script dừng thay vì ghi đè. Sau khi cập nhật mã, dùng
`Start-ScheduledTask -TaskName LemonCookieBot` để chạy lại task đã có.

Supervisor chạy `main.py` bằng đúng Python trong `.venv`, ghi log tại `logs\bot.log`
(tối đa 10 MB/tệp, giữ 5 tệp cũ). Khi tiến trình lỗi, thử chạy lại sau 5–60 giây.
Nếu heartbeat không cập nhật trong 3 phút sau thời gian khởi động chờ 5 phút,
supervisor dừng tiến trình bị treo và chạy lại. Khóa tiến trình ngăn hai bot/supervisor
cùng dùng một thư mục. Heartbeat chỉ phát hiện event loop bị treo, không chứng minh
Discord hoặc API ngân hàng đang hoạt động; xem thêm `/botstatus`.

Xem trạng thái và log:

```powershell
Get-ScheduledTask -TaskName LemonCookieBot | Get-ScheduledTaskInfo
Get-Content -LiteralPath 'C:\BOT\logs\bot.log' -Tail 60 -Wait
```

Đóng Remote Desktop/Disconnect được. VPS vẫn phải còn hoạt động và có mạng. Việc
VPS bị tắt hoặc nhà cung cấp ngừng dịch vụ không thể được sửa bởi bot. Chỉ chạy một
bản bot thật; tắt bản trên máy cũ trước khi mở VPS. Không chia sẻ thư mục bot qua web.

Gỡ task (giữ toàn bộ dữ liệu):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall-windows.ps1
```

## Giao lại đơn và đối soát

- `/redeliver order_id`: khách gửi lại đơn của chính mình; admin được phép có thể
  yêu cầu gửi lại nhưng người nhận luôn là chủ đơn, không phải admin. Có thời gian chờ
  giữa các lần gửi. Không trừ thêm tiền, không lấy thêm kho.
- `/downloadorder order_id`: chỉ chủ đơn tải nội dung qua phản hồi riêng tư, kể cả
  khi DM bị chặn. Admin không tải được account của khách qua lệnh này.
- Đơn mua mới lưu trạng thái giao hàng cùng giao dịch trừ tiền/kho. Bot tự thử lại
  tối đa 5 lần, có tăng khoảng chờ và báo admin khi lỗi. Đơn chưa giao được giữ lại
  qua lần khởi động và không bị dọn theo hạn 14 ngày. Đơn cũ đã có từ bản trước không
  tự bị gửi lại hàng loạt; chủ đơn có thể yêu cầu lại nếu dữ liệu đơn còn.
- Nếu Discord đã nhận DM nhưng bot mất điện trước khi ghi trạng thái, cùng nội dung
  đơn có thể được gửi lại. Không có lần trừ tiền hoặc xuất thêm kho thứ hai.
- `/pendingtopups`: 10 giao dịch ngân hàng cũ nhất chưa xác định được khách hoặc
  thiếu mã/ID ổn định. Giao dịch dùng username cũ luôn cần đối soát; chỉ mã LC kèm Discord ID mới được
  tự cộng khi khớp. Giao dịch đã chờ không tự cộng khi một username trùng xuất hiện.
- `/reconciletopup transaction_id user reason`: sau khi xác minh chứng từ/chủ tiền,
  chọn đúng khách; số tiền lấy từ giao dịch ngân hàng đã ghi nhận, không nhập tùy ý.
  Ghi nhận ID và cộng tiền trong cùng transaction, không cộng hai lần.
- `/ignoretopup transaction_id reason`: bỏ qua lãi ngân hàng/chuyển tiền không phải
  của khách, ghi nhật ký và không cộng tiền. Không có lệnh bỏ qua rồi cộng lại tùy tiện.
- `/botstatus`: kiểm tra SQLite, tình trạng ngân hàng, backup, đơn chưa giao và tạm
  dừng thanh toán. Backup và đối soát chỉ dành cho admin được cấp quyền.

Bank và crypto chạy các vòng kiểm tra riêng (mặc định 30 giây); panel vẫn cập nhật
mỗi 5 giây. Không in toàn bộ nội dung/số tài khoản ngân hàng mỗi lần kiểm tra. API bank
lỗi 3 lần liên tiếp sẽ cảnh báo, nhắc lại tối đa mỗi giờ và báo khi phục hồi. Ngưỡng và
chu kỳ có thể chỉnh trong `operations.json`, khởi động lại để áp dụng.

## Backup và khôi phục

Tự sao lưu khi bot chạy mỗi 6 giờ, kiểm tra toàn vẹn trước khi giữ bản sao và chỉ dọn
các backup tự động cũ; mặc định giữ 28 bản tại `backups\sqlite`. `/backup` tạo ngay một
bản sao trên VPS, **không gửi database chứa account lên Discord**. Có thể sao lưu local:

```bat
.venv\Scripts\python.exe manage_bot.py backup
```

Cần chép bản backup định kỳ sang một nơi lưu riêng mà bạn quản lý; backup cùng ổ
đĩa không bảo vệ khi mất cả VPS. Token, crypto config và `operations.json` cần sao lưu
riêng. Bot không tự tải dữ liệu khách hàng lên dịch vụ bên ngoài.

Để khôi phục, dừng bot/task trước:

```bat
.venv\Scripts\python.exe manage_bot.py restore "backups\sqlite\TEN_BACKUP.db"
```

Công cụ từ chối database không hợp lệ hoặc khi bot còn giữ khóa; kiểm tra integrity,
giữ bản sao database hiện tại tại `backups\pre-restore-...db`, rồi thay database một
cách nguyên tử. Không ghép các tệp WAL/SHM của hai database khác nhau.

**Restore đưa dữ liệu về thời điểm cũ**, nên bot tự tạm dừng mua hàng, giao lại tự động và cộng tiền ngân hàng/crypto
để tránh cộng lại giao dịch phát sinh sau bản backup. Đối chiếu tồn kho đã bán, số dư,
đơn và thanh toán trong khoảng mất dữ liệu trước khi mở bán lại. Sau khi đối chiếu,
dừng bot và bật lại mua hàng và thanh toán tự động bằng:

```bat
.venv\Scripts\python.exe manage_bot.py resume-payments --confirm-reconciled
```

Các giao dịch đã vào danh sách chờ vẫn cần đối soát bằng lệnh riêng. Không dùng restore
như thao tác sửa giá hoặc hoàn tiền thông thường.
