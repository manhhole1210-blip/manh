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
nâng cấp dừng, chỉ rõ tài liệu chứa lỗi để đối chiếu. Nhật ký hoạt động, lịch sử nạp và thông báo cũ được giữ nguyên, không ép đổi các
trường lịch sử sang số nguyên. Không tự làm tròn hoặc xóa sổ cái. Riêng quy đổi crypto dùng Decimal và quy tắc
làm tròn nửa lên (ROUND_HALF_UP) đến một đồng; số tiền USD được lưu dạng chuỗi thập phân.

Nếu thông báo nâng cấp chỉ đến `data/users.json`, chạy lệnh chỉ đọc:

```bat
.venv\Scripts\python.exe manage_bot.py check-money
```

Lệnh chỉ in ID và giá trị số dư cần đối chiếu (lẻ, âm, vượt giới hạn hoặc không phải
số), không in username/token và không sửa dữ liệu. Đối chiếu lịch sử mua/nạp và backup
để xác định số dư đúng trước khi điều chỉnh; không xóa database hoặc thay toàn bộ
users.json bằng `{}`. Bot đã nhập dữ liệu vào SQLite thì sửa JSON cũ không thay đổi
số dư hiện tại.

### Điều chỉnh số dư lẻ khi chủ bot đã chấp thuận

Dừng bot/task. Lệnh `round-balances` yêu cầu khai báo rõ từng Discord ID và số dư cũ
đã được chấp thuận; không tự quét rồi làm tròn mọi người dùng. Ví dụ minh họa với ID
và số tiền giả (thay bằng kế hoạch thực đã được duyệt):

```bat
.venv\Scripts\python.exe manage_bot.py round-balances --expect "123=10.8,456=0.16" --confirm
```

Quy tắc ROUND_HALF_UP: 10.8 thành 11, 0.16 thành 0. Công cụ giữ khóa chống bot đang
chạy, kiểm tra số dư hiện tại khớp số dư khai báo, tạo backup đã kiểm tra integrity tại
`backups/pre-round-vnd-...db`, rồi cập nhật toàn bộ số dư và audit trong một transaction.
Nếu một số dư đã thay đổi hoặc audit không ghi được, không áp dụng một phần. Chạy lại
cùng kế hoạch đã hoàn tất không điều chỉnh lần nữa. Audit lưu số dư trước/sau, chênh
lệch, quy tắc và đường dẫn backup; không sửa đơn hàng hoặc lịch sử giao dịch gốc.

Sau khi hoàn tất, chạy `manage_bot.py check-money`; nếu hết lỗi mới khởi động bot.
Không dùng lệnh này thay việc đối chiếu số dư âm, sai định dạng hoặc tiền vượt giới hạn.

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
kiểm tra thay đổi mỗi 5 giây (không sửa tin nhắn nếu nội dung giữ nguyên). Không in toàn bộ nội dung/số tài khoản ngân hàng mỗi lần kiểm tra. API bank
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

## Không thấy slash command mới

Bản mới đồng bộ trực tiếp các lệnh vào server trong `allowed_guild_ids` khi đăng
nhập, sau đó vẫn đồng bộ bản toàn cục cho các lệnh dùng qua DM. Log phải có dòng
`[DISCORD] Synced ... commands to SERVER_ID; /pendingtopups registered.`.
Chỉ đồng bộ server đã cấu hình; không tự cấp thêm quyền hoặc tự thêm server khác.
Các lần reconnect không lặp lại đồng bộ scope đã thành công trong cùng tiến trình;
scope lỗi được thử lại khi reconnect hoặc khi khởi động lại.

Nếu không có dòng xác nhận này: xem cảnh báo server chưa cấu hình hoặc sync failed.
Khi báo lỗi quyền sync, kiểm tra bot được mời với scope `bot` và `applications.commands`,
đúng tài khoản ứng dụng và đúng server. Trong Discord, chạy lệnh ở kênh server,
không phải DM; tải lại Discord bằng Ctrl+R. Nếu vẫn bị ẩn, quản trị server cần kiểm tra
Server Settings > Integrations > Lemon > Commands và quyền Use Application Commands
của người dùng/kênh. Không xóa database để xử lý lỗi hiển thị slash command.

## Cảnh báo HTTP 429 khi sửa panel

Bản mới chỉ PATCH tin nhắn khi nội dung kho/giá/nút thay đổi, bỏ timestamp khỏi phép
so sánh, gộp các thay đổi nhanh với khoảng cách tối thiểu 10 giây và chỉ kiểm tra panel
còn tồn tại mỗi 5 phút khi nội dung giữ nguyên. Gặp lỗi sẽ chờ trước khi thử lại;
không tắt cơ chế rate limit của discord.py. Logger Discord dùng chung một handler,
tránh in hai lần cùng cảnh báo. Chỉ chạy một bản bot dùng cùng token, kể cả trên các
máy hoặc thư mục khác nhau; khóa local không ngăn được hai máy chạy chung token.

Lệnh quản trị chỉ đăng ký ở server đã cấp quyền. Bản toàn cục chỉ giữ các lệnh khách
hàng dùng trong DM của bot; không còn hiển thị hai bản cùng tên trong server. Sau khi
cập nhật main.py, khởi động lại và chờ cả sync server và sync global thành công, rồi
Ctrl+R trên Discord để làm mới danh sách lệnh cũ.


## Đặt trước trả đủ tiền

Dừng bot trước khi thay cả `main.py` và `sqlite_store.py` từ cùng bản tải mới;
giữ nguyên `data`, `operations.json` và token. Sao lưu bằng `/backup` trước khi dừng.
Khởi động lại để SQLite tự thêm bảng `preorders` và đồng bộ các lệnh mới.

- Khách bấm **Đặt trước** trên panel hoặc dùng `/preorder product quantity`.
  Chọn sản phẩm, số lượng, xem tổng tiền rồi bấm **Xác nhận và trừ tiền**.
  Không trừ tiền chỉ vì mở form. Tối đa 10 đơn chờ mỗi khách, 50.000 account/đơn.
- Bot trừ đủ tiền từ số dư, giữ nguyên đơn giá và số ngày bảo hành tại lúc xác nhận.
  Không cam kết ngày nhập hàng. Nếu số dư không đủ hoặc giá đổi trước xác nhận,
  bot từ chối và không trừ tiền. Nút xác nhận lặp không tạo khoản trừ thứ hai.
- `/mypreorders` xem tối đa 20 đơn chờ đầu tiên; đặt `waiting_only:False`
  để xem cả đơn đã phân hàng hoặc đã hủy. Mã PRE cũng là mã đơn hàng sau phân hàng.
- `/cancelpreorder preorder_id` hủy đơn chưa phân hàng và hoàn đủ tiền **vào số dư bot**.
  Không hoàn lại ngân hàng/Crypto. Đơn đã phân hàng (kể cả DM lỗi) không tự hoàn;
  dùng `/downloadorder` hoặc `/redeliver` để nhận hàng, không phải trả lần nữa.
- Admin dùng `/preorders` xem tối đa 20 đơn chờ đầu tiên trong server,
  `/cancelcustomerpreorder preorder_id` để hủy và hoàn tiền cho khách.
  `/botstatus` hiển thị số đơn chờ và tổng tiền đã thu của các đơn đó.
- Khi `/addstock` nhập hàng hoặc vòng cập nhật kho chạy, bot phân đủ số lượng cho từng
  đơn theo thứ tự xác nhận, riêng từng sản phẩm. Đơn lớn ở đầu hàng chờ phải được
  phân đủ trước; không chia nhỏ hoặc cho đơn nhỏ/mua thường vượt lên.
  Kho thiếu không trừ thêm tiền. Phân hàng, lưu đơn và hàng đợi gửi DM nằm trong một
  giao dịch SQLite. Gửi DM sử dụng lại cơ chế thử lại bền vững của bot.
- Nếu sản phẩm đã đủ hàng, đơn đặt trước có thể được phân và giao ngay sau xác nhận.
  Bảo hành bắt đầu khi phân hàng, không tính từ lúc trả tiền chờ hàng.
- Xóa sản phẩm bằng `/removeproduct` tự hoàn đủ tiền cho các đơn còn chờ của sản phẩm
  trong cùng giao dịch xóa. Đơn đã phân hàng được giữ. Khôi phục DB vẫn tạm dừng
  thanh toán, hoàn tiền và phân hàng cho đến khi đối chiếu xong.

Kiểm tra trên VPS bằng một đơn nhỏ: đặt lúc kho trống, xác nhận số dư giảm đúng,
nhập kho, kiểm tra nhận đúng hàng và không bị trừ lần nữa. Thử hủy một đơn khác
trước nhập kho để xác nhận hoàn đủ tiền một lần. Nếu chưa thấy nút mới, chờ panel
cập nhật; slash `/preorder` luôn là cách truy cập trực tiếp trong server được phép.
