# Lemon Cookie — bot Discord dùng SQLite

Bản chuyển đổi từ `main.py` được cung cấp. Giữ các lệnh shop, thanh toán,
ticket và thông báo. Database được tạo tại `data/bot.db`; dùng `sqlite3` có
sẵn trong Python, không cần cài máy chủ SQLite hay thư viện database riêng.

## Cài đặt và chuyển dữ liệu (Windows/Linux, Python 3.10+)

1. **Dừng bot cũ** và sao lưu toàn bộ thư mục đang chạy, kể cả token/config riêng.
2. Đặt `main.py`, `sqlite_store.py`, `requirements.txt` cạnh nhau **trong thư mục
   bot cũ**. Giữ nguyên `data/`, `tickets.json`, `user_languages.json`, `config.json`,
   `input/`, `apinaptien/`, `autocrypto/` hiện có. Không chép database mẫu vào máy thật.
3. Cài và chạy:

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python main.py
   ```

Trên Windows CMD, vào thư mục bot rồi dùng các lệnh sau thay cho lệnh Linux:

```bat
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

Với các lệnh công cụ ở dưới, thay `.venv/bin/python` bằng
`.venv\Scripts\python.exe` trên Windows. Khóa tiến trình dùng `msvcrt` trên
Windows và `fcntl` trên Linux.

Nếu báo lỗi JSON, kiểm tra dữ liệu mà không sửa tệp hoặc tạo database:

```bat
.venv\Scripts\python.exe check_data.py
```

Công cụ in tên tệp lỗi và số dòng/cột, không in nội dung hoặc token. Sao lưu tệp
được báo lỗi trước khi sửa cú pháp JSON; không thay tệp số dư/đơn hàng bằng `{}`
hoặc `[]`. Có hỗ trợ UTF-8 BOM thường gặp khi lưu tệp trên Windows. JSON sai cú
pháp vẫn bị từ chối để bảo toàn dữ liệu. Đừng gửi token/API key hoặc thông tin
account khi nhờ kiểm tra lỗi.

Token Discord đọc từ biến `DISCORD_BOT_TOKEN`, hoặc `input/token.txt` như trước.
Token ngân hàng đọc từ `VCB_API_TOKEN`, hoặc `apinaptien/token.txt`.
Cấu hình merchant/API key crypto tiếp tục đọc từ `autocrypto/config.json`;
không nhập các khóa này vào database. Các ID role/category/admin trong bot gốc
vẫn cần đúng với server của bạn.

Lần chạy đầu tự nhập JSON, kho TXT và các bản lưu đơn hàng vào SQLite, tạo bản
sao nguyên trạng tại `backups/legacy-<thời điểm>/`, rồi đánh dấu đã chuyển.
Nếu JSON lỗi hoặc cấu trúc sổ cái không hợp lệ, quá trình dừng để xử lý dữ liệu,
không thay bằng sổ cái trống. Journal chưa hoàn tất của bot cũ được phục hồi trước.
Những lần chạy sau **chỉ dùng database**; JSON/TXT cũ không tự nhập lại và không
bị xóa. Không chỉnh tệp JSON/TXT cũ để thay số dư hoặc nhập thêm kho sau chuyển đổi.

Để kiểm tra/chuyển dữ liệu mà không đăng nhập Discord (sau khi dừng bot):

```bash
.venv/bin/python sqlite_store.py
```

Nếu công cụ báo còn journal cũ, dùng `main.py` để phục hồi trước. Không tự xóa journal.
Trước khi cho khách mua lại, kiểm tra số dư, tồn kho, lịch sử đơn và cấu hình ngân hàng
qua bot. Tệp được tải lên không chứa dữ liệu vận hành thực tế nên phần đối chiếu này
phải thực hiện trên máy đang có dữ liệu của bạn.

## Nhập thêm kho

### Trên Discord, không cần dừng bot

Dùng `/addstock`, chọn `product` trong danh sách sản phẩm (gồm sản phẩm mặc định),
rồi tải file `.txt` ở tham số `file`. Lệnh chỉ dùng trong server và yêu cầu quyền
Administrator. Nếu là sản phẩm mới, tạo bằng `/addproduct` trước.

Mỗi dòng là một account; file phải là UTF-8 (có hoặc không BOM), tối đa
50.000 dòng. Bot không đặt giới hạn MB riêng; dung lượng tải lên theo giới hạn Discord. Bot bỏ dòng trắng, khoảng trắng đầu/cuối và các dòng trùng với kho
hiện có hoặc trùng trong chính file. Bot chỉ nối thêm vào sản phẩm đã chọn, không
thay kho cũ. Kết quả riêng tư báo số đã thêm, số dòng trùng và tổng tồn kho; panel
shop được cập nhật sau khi lưu thành công. File lỗi không được nhập một phần.
Nhập kho dùng cùng khóa với mua hàng và xóa sản phẩm, đồng thời commit bằng
transaction SQLite. Nhật ký chỉ chứa ID sản phẩm và số lượng, không chứa account.

Sau khi cập nhật `main.py`, khởi động lại bot và chờ đồng bộ lệnh để thấy `/addstock`.

### Xóa bớt tồn kho trên Discord

Dùng `/removestock`, chọn `product` và nhập `quantity` là số account cần xóa.
Lệnh dành cho Administrator trong server, xóa từ **cuối kho** để giữ nguyên thứ tự
những account đầu kho đang chờ bán. Muốn xóa hết, nhập số lượng bằng tồn kho hiện tại.
Nếu số lượng vượt tồn kho hoặc sản phẩm không còn tồn tại, không xóa account nào.
Lệnh không xóa sản phẩm, không đổi số dư, đơn đã bán hoặc tổng số đã bán. Kết quả
riêng tư báo số đã xóa và số còn lại; panel được cập nhật sau khi lưu SQLite thành công.
Xóa kho dùng cùng khóa với mua hàng/nhập kho và có rollback khi lỗi ghi dữ liệu.

### Qua dòng lệnh

Dừng bot. Đặt mỗi account trên một dòng trong `input/new_accounts.txt`, sau đó:

```bash
# Sản phẩm mặc định
.venv/bin/python sqlite_store.py --import-stock input/new_accounts.txt

# Sản phẩm thêm bằng /addproduct; lấy ID trong thông báo của lệnh, ví dụ p1
.venv/bin/python sqlite_store.py --import-stock input/new_accounts.txt --product p1
```

Lệnh nối thêm vào kho, không thay kho hiện có. Tệp rỗng, dòng trùng trong tệp,
account đã có trong kho sản phẩm hoặc ID không tồn tại sẽ bị từ chối.
Sau khi nhập, di chuyển/xóa tệp nhập khỏi nơi chia sẻ và khởi động bot lại.
Công cụ không cho nhập kho hoặc chạy migration trong khi bot giữ khóa dữ liệu.
Chỉ chạy một bot trên mỗi thư mục dữ liệu.

## Sao lưu và khôi phục

Có thể tạo bản sao nhất quán trong lúc bot chạy bằng SQLite Backup API:

```bash
.venv/bin/python sqlite_store.py --backup backups/bot-2026-10-16.db
```

Dùng tên mới cho mỗi bản sao; công cụ không ghi đè bản sao đã tồn tại. Để khôi phục:
dừng bot, giữ một bản sao nguyên trạng của thư mục `data/` hiện tại để có thể quay lại,
rồi đặt bản sao SQLite đã chọn vào **thư mục `data/` mới**, tên `bot.db`. Không ghép
`bot.db` được khôi phục với các tệp `bot.db-wal` hoặc `bot.db-shm` của database cũ.
Khởi động bot và đối chiếu số dư/kho trước khi phục vụ khách.
Sao lưu riêng token/config crypto vì chúng không được đưa vào database.

Không xóa `bot.db`, và không sao chép riêng tệp này khi bot đang chạy: các giao dịch
mới có thể còn trong WAL. Database và bản sao lưu chứa số dư và thông tin account;
chúng được loại khỏi Git.

## Cách lưu trữ

- `users`, `orders`, `crypto_orders`: mỗi người dùng/đơn là một hàng, khóa chính là ID;
  thông tin đầy đủ được giữ trong cột JSON `payload` để tương thích dữ liệu cũ.
- `processed_transactions`: mã giao dịch ngân hàng là khóa chính, chống trùng ID.
- `stock`: nội dung kho theo sản phẩm, giữ nguyên byte khi chuyển từ TXT.
- `documents`: cấu hình, giá, sản phẩm, ticket, lịch sử, ngôn ngữ và bản lưu đơn.
- `metadata`: đánh dấu migration đã hoàn tất.

Mua hàng và cộng tiền sử dụng `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`, WAL và
`synchronous=FULL`. Khi lỗi, số dư/kho/đơn không được lưu dở dang; trạng thái tài chính
trong bộ nhớ cũng được phục hồi. Giao hàng Discord diễn ra sau khi commit. Cơ chế
xóa đơn hết hạn bảo hành/14 ngày được giữ và áp dụng lên database.

Đây là bước chuyển lưu trữ, chưa chuẩn hóa mọi trường thành cột SQL riêng: cấu trúc
JSON nghiệp vụ và cách biểu diễn số dư của bot cũ được giữ. Khi lưu một tập người dùng
hoặc đơn, adapter vẫn cập nhật cả tập; phù hợp một tiến trình, chưa tối ưu cho kho lớn
hoặc nhiều bot cùng truy cập. Không sửa database trực tiếp trong lúc bot đang chạy vì
bot còn giữ số dư và thanh toán trong bộ nhớ. Các vấn đề phân quyền nhiều server của
bot gốc không nằm trong thay đổi này.

## Kiểm thử

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Có kiểm thử migration, giữ bản sao dữ liệu gốc, khởi động lại, rollback, backup WAL,
nhập/xóa kho, mua hàng thật qua callback và chống cộng trùng nạp ngân hàng/crypto.
Discord và API thanh toán được giả lập, không tạo giao dịch hoặc gửi tin nhắn thật.
