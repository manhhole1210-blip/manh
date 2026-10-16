import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Modal, TextInput, View, Button

import asyncio
import logging
import time
from pathlib import Path
from money import vnd, crypto_to_vnd
from operations import RuntimePolicy, rotating_backup
import io
import json
import os
import re
import copy
import tempfile
import secrets
import hashlib
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError:  # Pillow is optional: without it the images are skipped
    Image = ImageDraw = ImageFont = ImageFilter = None


# =========================================================
# PATH
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INPUT_DIR = os.path.join(BASE_DIR, "input")
API_NAPTIEN_DIR = os.path.join(BASE_DIR, "apinaptien")
DATA_DIR = os.path.join(BASE_DIR, "data")
AUTOCRYPTO_DIR = os.path.join(BASE_DIR, "autocrypto")

os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(API_NAPTIEN_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(AUTOCRYPTO_DIR, exist_ok=True)


BOT_TOKEN_FILE = os.path.join(INPUT_DIR, "token.txt")
VCB_TOKEN_FILE = os.path.join(API_NAPTIEN_DIR, "token.txt")

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
TRANSACTIONS_FILE = os.path.join(DATA_DIR, "transactions.json")
CRYPTO_ORDERS_FILE = os.path.join(DATA_DIR, "crypto_orders.json")
STOCK_MESSAGE_FILE = os.path.join(DATA_DIR, "stock_message.json")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.txt")
SALES_FILE = os.path.join(DATA_DIR, "sales.json")
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
ORDER_ARCHIVE_DIR = os.path.join(DATA_DIR, "orders")
ORDER_RETENTION_DAYS = 14
WARRANTY_PERIOD_DAYS = 3
os.makedirs(ORDER_ARCHIVE_DIR, exist_ok=True)

# Persistent audit/history files
TOPUP_HISTORY_FILE = os.path.join(DATA_DIR, "topup_history.json")
ACTIVITY_LOG_FILE = os.path.join(DATA_DIR, "activity_logs.json")

CRYPTO_CONFIG_FILE = os.path.join(
    AUTOCRYPTO_DIR,
    "config.json"
)


# =========================================================
# DEFAULT CONFIG
# =========================================================

DEFAULT_CONFIG = {
    "stock_channel_id": 1551565270345195560,

    "product_name": "Clone Roblox Anti CaptchaLock",

    "price_tier_1": 2500,
    "price_tier_2": 2300,
    "price_tier_3": 2100,
    "price_tier_4": 2100,

    "vcb_bank_name": "Vietcombank",
    "vcb_bin": "970436",

    "vcb_account_number": "YOUR_VCB_ACCOUNT_NUMBER",
    "vcb_account_name": "LEMON SERVICE",

    "usd_to_vnd": 25000
}


DEFAULT_CRYPTO_CONFIG = {
    "merchant_id": "YOUR_MERCHANT_ID",
    "api_key": "YOUR_API_KEY"
}


# =========================================================
# JSON
# =========================================================

# SQLite transactions keep balance, stock and order updates atomic.
# The old redo journal reader is retained only for first-run migration.
# Hold the same cross-platform lock used by the SQLite maintenance CLI.
from sqlite_store import SQLiteStore, acquire_process_lock, read_legacy_json
try:
    _PROCESS_LOCK = acquire_process_lock(DATA_DIR)
except OSError as exc:
    raise RuntimeError("Cannot lock data directory; stop other bot processes and check permissions") from exc

_JOURNAL_FILE = os.path.join(DATA_DIR, "transaction_journal.json")


def _atomic_write(path, text):
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bot-write-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _delete_stock_file(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    if os.name == "posix":
        fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _recover_transaction():
    if not os.path.exists(_JOURNAL_FILE):
        return
    writes = read_legacy_json(_JOURNAL_FILE)
    if not isinstance(writes, dict):
        raise ValueError("Invalid transaction journal; restore from backup")
    # Validate the entire journal before applying any operation. null means delete.
    for path, text in writes.items():
        if os.path.commonpath([os.path.realpath(path), os.path.realpath(DATA_DIR)]) != os.path.realpath(DATA_DIR):
            raise ValueError("Transaction journal path outside data directory")
        if text is None:
            filename = os.path.basename(path)
            if not filename.startswith("stock_") or not filename.endswith(".txt") or os.path.islink(path):
                raise ValueError("Invalid stock deletion in transaction journal")
        elif not isinstance(text, str):
            raise ValueError("Invalid journal content")
    for path, text in writes.items():
        if text is None:
            _delete_stock_file(path)
        else:
            _atomic_write(path, text)
    os.unlink(_JOURNAL_FILE)


# Replay an old redo journal before importing the legacy ledger.
_recover_transaction()

POLICY = RuntimePolicy(BASE_DIR)
STORE = SQLiteStore(BASE_DIR)
_legacy_backup = STORE.migrate()
if _legacy_backup:
    print(f"[SQLITE] Legacy backup: {_legacy_backup}")
STORE.upgrade_vnd()


@contextmanager
def data_transaction():
    """One atomic SQLite commit. Never await inside this synchronous context."""
    snapshots = {name: copy.deepcopy(globals()[name])
                 for name in ("users", "transactions", "crypto_orders")
                 if name in globals()}
    try:
        with STORE.transaction():
            yield
    except BaseException:
        for name, snapshot in snapshots.items():
            current = globals()[name]
            current.clear()
            if isinstance(current, dict):
                current.update(snapshot)
            else:
                current.extend(snapshot)
        raise


def save_json(path, data):
    STORE.save_json(path, data)


def load_json(path, default):
    # Payment credentials remain in the existing private configuration file.
    if os.path.abspath(path) == os.path.abspath(CRYPTO_CONFIG_FILE):
        try:
            return read_legacy_json(path)
        except FileNotFoundError:
            return copy.deepcopy(default)
    return STORE.load_json(path, default)


def _write_text(path, text):
    STORE.write_stock(path, text)


# =========================================================
# CONFIG
# =========================================================

config = load_json(
    CONFIG_FILE,
    DEFAULT_CONFIG.copy()
)

if not isinstance(config, dict):
    config = DEFAULT_CONFIG.copy()

for key, value in DEFAULT_CONFIG.items():

    if key not in config:
        config[key] = value

# Official listed prices — always keep these values synchronized.
config["price_tier_1"] = 2500
config["price_tier_2"] = 2300
config["price_tier_3"] = 2100
config["price_tier_4"] = 2100

save_json(
    CONFIG_FILE,
    config
)


crypto_config = load_json(
    CRYPTO_CONFIG_FILE,
    DEFAULT_CRYPTO_CONFIG.copy()
)

if not isinstance(crypto_config, dict):
    crypto_config = DEFAULT_CRYPTO_CONFIG.copy()

for key, value in DEFAULT_CRYPTO_CONFIG.items():

    if key not in crypto_config:
        crypto_config[key] = value

# Keep payment credentials out of the database and its backups.
# Existing autocrypto/config.json remains the configuration source.


# =========================================================
# DATA
# =========================================================

users = load_json(
    USERS_FILE,
    {}
)

if not isinstance(users, dict):

    users = {}

    save_json(
        USERS_FILE,
        users
    )


# Serializes balance/stock-changing shop transactions.
# This prevents two simultaneous purchases from reading the same stock
# and/or balance before either transaction has been saved.
SHOP_TRANSACTION_LOCK = asyncio.Lock()


crypto_orders = load_json(
    CRYPTO_ORDERS_FILE,
    {}
)

if not isinstance(crypto_orders, dict):

    crypto_orders = {}

    save_json(
        CRYPTO_ORDERS_FILE,
        crypto_orders
    )


# =========================================================
# TRANSACTIONS
# =========================================================

def normalize_transaction_list(data):

    result = []

    if isinstance(data, list):

        for item in data:

            if item is None:
                continue

            item = str(item).strip()

            if item and item not in result:
                result.append(item)

        return result


    if isinstance(data, dict):

        for key, value in data.items():

            key = str(key).strip()

            if key and key not in result:
                result.append(key)

            if isinstance(value, str):

                value = value.strip()

                if value and value not in result:
                    result.append(value)

            elif isinstance(value, list):

                for item in value:

                    if item is None:
                        continue

                    item = str(item).strip()

                    if (
                        item
                        and
                        item not in result
                    ):

                        result.append(item)

        return result


    return []


transactions = load_json(
    TRANSACTIONS_FILE,
    []
)

transactions = normalize_transaction_list(
    transactions
)

save_json(
    TRANSACTIONS_FILE,
    transactions
)


def is_transaction_processed(transaction_id):
    return STORE.transaction_processed(transaction_id)


def mark_transaction_processed(transaction_id):
    transaction_id = str(transaction_id).strip()
    if not STORE.mark_transaction(transaction_id):
        return False
    transactions.append(transaction_id)
    return True


# =========================================================
# TOKEN
# =========================================================

def read_token(path):

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            return f.read().strip()

    except Exception as e:

        print(
            f"[TOKEN] Cannot read {path}: {e}"
        )

        return ""


BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN") or read_token(BOT_TOKEN_FILE)

VCB_API_TOKEN = os.environ.get("VCB_API_TOKEN") or read_token(VCB_TOKEN_FILE)


# =========================================================
# CONFIG VALUES
# =========================================================

STOCK_CHANNEL_ID = int(
    config["stock_channel_id"]
)

PRODUCT_NAME = str(
    config["product_name"]
)

PRICE_TIER_1 = int(
    config["price_tier_1"]
)

PRICE_TIER_2 = int(
    config["price_tier_2"]
)

PRICE_TIER_3 = int(
    config["price_tier_3"]
)

PRICE_TIER_4 = int(
    config["price_tier_4"]
)

# Logical SQLite document key for prices and warranty overrides.
PRICES_FILE = os.path.join(DATA_DIR, "prices.json")

MAX_PRICE_PER_ACCOUNT = 10_000_000
MAX_WARRANTY_DAYS = 365
MAX_TIERS = 10
MAX_TIER_QUANTITY = 1_000_000

# Tiers of the default product. A tier covers min_qty..max_qty accounts
# (max_qty None = no upper limit). Tiers may not overlap; gaps are allowed
# (a quantity that falls in a gap cannot be bought).
DEFAULT_TIERS = [
    {"min_qty": 1, "max_qty": 50, "price": PRICE_TIER_1},
    {"min_qty": 51, "max_qty": 1000, "price": PRICE_TIER_2},
    {"min_qty": 1001, "max_qty": None, "price": PRICE_TIER_3},
]

# Warranty length given to NEW orders of the default product.
DEFAULT_WARRANTY_DAYS = WARRANTY_PERIOD_DAYS


class TierLimitError(ValueError):
    pass


class TierOverlapError(ValueError):
    def __init__(self, tier):
        super().__init__("tier ranges overlap")
        self.tier = tier


def _tier_end(tier_item):
    end = tier_item.get("max_qty")
    return float("inf") if end is None else end


def tiers_overlap(a, b):
    return a["min_qty"] <= _tier_end(b) and b["min_qty"] <= _tier_end(a)


def clean_tiers(raw):
    """Validate a tier list and return it sorted. Raises ValueError if invalid.

    Older files only stored min_qty + price: those tiers last until the next
    tier starts, exactly like before.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("tiers must be a non-empty list")

    legacy = all("max_qty" not in item for item in raw)

    by_qty = {}
    for item in raw:
        qty = int(item["min_qty"])
        price = vnd(item["price"])
        if not 1 <= qty <= MAX_TIER_QUANTITY:
            raise ValueError(f"min_qty out of range: {qty}")
        if not 1 <= price <= MAX_PRICE_PER_ACCOUNT:
            raise ValueError(f"price out of range: {price}")

        max_qty = None
        if not legacy and item.get("max_qty") is not None:
            max_qty = int(item["max_qty"])
            if not qty <= max_qty <= MAX_TIER_QUANTITY:
                raise ValueError(f"max_qty out of range: {max_qty}")

        by_qty[qty] = {"min_qty": qty, "max_qty": max_qty, "price": price}

    tiers = [by_qty[q] for q in sorted(by_qty)]

    if len(tiers) > MAX_TIERS:
        raise ValueError("too many tiers")

    if legacy:
        for index in range(len(tiers) - 1):
            tiers[index]["max_qty"] = tiers[index + 1]["min_qty"] - 1
    else:
        for index in range(len(tiers) - 1):
            if tiers_overlap(tiers[index], tiers[index + 1]):
                raise ValueError("tier ranges overlap")

    return tiers


def price_for_quantity(tiers, quantity):
    if quantity < 1:
        return None

    for tier_item in tiers:
        if tier_item["min_qty"] <= quantity <= _tier_end(tier_item):
            return int(tier_item["price"])

    return None


def tier_gaps(tiers):
    """Quantity ranges without a price: [(low, high_or_None)]."""
    gaps = []
    cursor = 1

    for tier_item in tiers:
        if tier_item["min_qty"] > cursor:
            gaps.append((cursor, tier_item["min_qty"] - 1))
        end = tier_item.get("max_qty")
        if end is None:
            return gaps
        cursor = end + 1

    gaps.append((cursor, None))
    return gaps


def upsert_tier(tiers, min_qty, max_qty, price):
    """Add a tier, or edit the tier that starts at min_qty.

    Returns (new_tiers, old_tier). old_tier is None when a tier was added.
    Raises TierOverlapError / TierLimitError.
    """
    new = [dict(t) for t in tiers]
    candidate = {
        "min_qty": int(min_qty),
        "max_qty": None if max_qty is None else int(max_qty),
        "price": int(price),
    }

    target = None
    for tier_item in new:
        if tier_item["min_qty"] == candidate["min_qty"]:
            target = tier_item

    for tier_item in new:
        if tier_item is not target and tiers_overlap(tier_item, candidate):
            raise TierOverlapError(dict(tier_item))

    old = dict(target) if target is not None else None

    if target is not None:
        target.update(candidate)
    else:
        if len(new) >= MAX_TIERS:
            raise TierLimitError("tier limit reached")
        new.append(candidate)

    new.sort(key=lambda t: t["min_qty"])
    return new, old


def remove_tier(tiers, min_qty):
    """Return (status, new_tiers, removed_tier): status ok / missing / last."""
    removed = None
    for tier_item in tiers:
        if tier_item["min_qty"] == min_qty:
            removed = dict(tier_item)

    if removed is None:
        return "missing", tiers, None

    if len(tiers) <= 1:
        return "last", tiers, None

    return "ok", [dict(t) for t in tiers if t["min_qty"] != min_qty], removed


def load_price_overrides():
    global DEFAULT_TIERS, DEFAULT_WARRANTY_DAYS

    if not STORE.exists(PRICES_FILE):
        return

    try:
        saved = load_json(PRICES_FILE, {})

        if not isinstance(saved, dict):
            raise ValueError("not a JSON object")

    except Exception as e:
        # A broken prices file must never stop the bot: keep config prices.
        print(f"[PRICE] Ignoring invalid {PRICES_FILE}: {e}")
        return

    try:
        if "tiers" in saved:
            DEFAULT_TIERS = clean_tiers(saved["tiers"])
        else:
            # Oldest file format: three fixed tiers (1-50, 51-1000, 1001+).
            DEFAULT_TIERS = clean_tiers([
                {"min_qty": 1, "price": saved["price_tier_1"]},
                {"min_qty": 51, "price": saved["price_tier_2"]},
                {"min_qty": 1001, "price": saved["price_tier_3"]},
            ])
    except Exception as e:
        print(f"[PRICE] Ignoring invalid tiers in {PRICES_FILE}: {e}")

    try:
        if "warranty_days" in saved:
            days = int(saved["warranty_days"])
            if not 0 <= days <= MAX_WARRANTY_DAYS:
                raise ValueError(f"warranty_days out of range: {days}")
            DEFAULT_WARRANTY_DAYS = days
    except Exception as e:
        print(f"[PRICE] Ignoring invalid warranty in {PRICES_FILE}: {e}")


def save_price_overrides():
    save_json(
        PRICES_FILE,
        {
            "tiers": DEFAULT_TIERS,
            "warranty_days": DEFAULT_WARRANTY_DAYS
        }
    )


def set_default_tier(min_qty, max_qty, price):
    """Add/edit a tier of the default product. Returns the old tier or None."""
    global DEFAULT_TIERS

    new, old = upsert_tier(DEFAULT_TIERS, min_qty, max_qty, price)
    DEFAULT_TIERS = new
    save_price_overrides()
    return old


def remove_default_tier(min_qty):
    """Returns (status, removed_tier) with status ok / missing / last."""
    global DEFAULT_TIERS

    status, new, removed = remove_tier(DEFAULT_TIERS, min_qty)
    if status == "ok":
        DEFAULT_TIERS = new
        save_price_overrides()
    return status, removed


def set_default_warranty_days(days):
    """Change the warranty of the default product. Returns the old value."""
    global DEFAULT_WARRANTY_DAYS

    old = DEFAULT_WARRANTY_DAYS
    DEFAULT_WARRANTY_DAYS = int(days)
    save_price_overrides()
    return old


def range_text(low, high):
    """Canonical text used as a command value: '7-11' or '1001+'."""
    return f"{low}+" if high is None else f"{low}-{high}"


def format_range(low, high, lang="vi"):
    if high is None:
        return f"{fmt_num(low, lang)}+"
    if high == low:
        return fmt_num(low, lang)
    return f"{fmt_num(low, lang)} – {fmt_num(high, lang)}"


def tier_rows(tiers, lang="vi"):
    """[(label, price)] such as ('1 – 5', 2500), ('7 – 11', 2300), ('1.001+', 2100)."""
    return [
        (
            format_range(t["min_qty"], t.get("max_qty"), lang),
            int(t["price"])
        )
        for t in tiers
    ]


def tier_label(tiers, min_qty, lang="vi"):
    for tier_item in tiers:
        if tier_item["min_qty"] == min_qty:
            return format_range(min_qty, tier_item.get("max_qty"), lang)
    return str(min_qty)


def parse_tier_range(value):
    """Read a tier typed by the admin.

    '1-5' / '7 – 11'  -> (1, 5, 'range')
    '1001+'          -> (1001, None, 'open')
    '200'            -> (200, None, 'single')
    Returns None when no valid number is found.
    """
    text = str(value)
    numbers = [
        int(re.sub(r"[.,_]", "", m))
        for m in re.findall(r"\d+(?:[.,_]\d{3})*", text)
    ]

    if not numbers:
        return None

    low = numbers[0]

    if len(numbers) >= 2 and "+" not in text:
        high = numbers[1]
        if high < low:
            return None
        return low, high, "range"

    if "+" in text:
        return low, None, "open"

    return low, None, "single"


load_price_overrides()

VCB_BANK_NAME = str(
    config["vcb_bank_name"]
)

VCB_BIN = str(
    config["vcb_bin"]
)

VCB_ACCOUNT_NUMBER = str(
    config.get(
        "vcb_account_number",
        ""
    )
).strip()

VCB_ACCOUNT_NAME = str(
    config.get(
        "vcb_account_name",
        "LEMON SERVICE"
    )
).strip()

USD_TO_VND = Decimal(
    str(
        config.get(
            "usd_to_vnd",
            25000
        )
    )
)


# =========================================================
# PAYFJORD
# =========================================================

PAYFJORD_ADD_INVOICE = (
    "https://payfjord.com/api/AddInvoice"
)

PAYFJORD_STATUS = (
    "https://payfjord.com/api/GetInvoiceStatus"
)

MERCHANT_ID = str(
    crypto_config.get(
        "merchant_id",
        ""
    )
).strip()

PAYFJORD_API_KEY = str(
    crypto_config.get(
        "api_key",
        ""
    )
).strip()


# =========================================================
# PRICE
# =========================================================

def get_product_price(quantity):
    return price_for_quantity(DEFAULT_TIERS, quantity)


# =========================================================
# STOCK
# =========================================================

def load_stock():
    content = STORE.read_stock(ACCOUNTS_FILE).decode("utf-8")
    return [line.strip() for line in content.splitlines() if line.strip()]


def save_stock(stock):
    _write_text(ACCOUNTS_FILE, "".join(item + "\n" for item in stock))



# =========================================================
# PRODUCTS
# =========================================================

# The original product from config.json stays exactly as it was (tiered prices,
# logical accounts.txt stock key). Product metadata and stock are stored in SQLite.

PRODUCTS_FILE = os.path.join(DATA_DIR, "products.json")
DEFAULT_PRODUCT_ID = "default"
MAX_EXTRA_PRODUCTS = 24  # a select menu holds 25 options, 1 is the default product
MAX_PRODUCT_NAME_LENGTH = 60


def get_default_product():
    return {
        "id": DEFAULT_PRODUCT_ID,
        "stock_file": "accounts.txt",
        "name": PRODUCT_NAME,
        "tiered": True,
        "tiers": [dict(t) for t in DEFAULT_TIERS],
        "warranty_days": DEFAULT_WARRANTY_DAYS,
    }


def load_products_data():
    data = load_json(PRODUCTS_FILE, {"next_id": 1, "products": []})

    if not isinstance(data, dict):
        data = {}

    raw_products = data.get("products")
    if not isinstance(raw_products, list):
        raw_products = []

    products = []
    for item in raw_products:
        try:
            try:
                tiers = clean_tiers(item["tiers"])
            except (KeyError, TypeError, ValueError, AttributeError):
                tiers = clean_tiers([{"min_qty": 1, "max_qty": None, "price": item["price"]}])

            products.append({
                "id": str(item["id"]),
                "name": str(item["name"]),
                "price": tiers[0]["price"],
                "tiers": tiers,
                "warranty_days": int(item["warranty_days"]),
                "stock_file": os.path.basename(str(item["stock_file"])),
                "sold": int(item.get("sold", 0)),
                "created_at": str(item.get("created_at", "")),
            })
        except (KeyError, TypeError, ValueError, AttributeError):
            # A damaged entry must never break the shop: skip it.
            continue

    try:
        next_id = max(1, int(data.get("next_id", len(products) + 1)))
    except (TypeError, ValueError):
        next_id = len(products) + 1

    return {"next_id": next_id, "products": products,
            "default_deleted": data.get("default_deleted") is True}


def save_products_data(data):
    save_json(PRODUCTS_FILE, data)


def get_extra_products():
    products = []
    for item in load_products_data()["products"]:
        product = dict(item)
        product["tiered"] = False
        products.append(product)
    return products


def get_all_products():
    default = [] if load_products_data()["default_deleted"] else [get_default_product()]
    return default + get_extra_products()


def get_product_by_id(product_id):
    for product in get_all_products():
        if product["id"] == product_id:
            return product
    return None


def product_tiers(product):
    tiers = product.get("tiers")
    if isinstance(tiers, list) and tiers:
        return tiers
    return [{"min_qty": 1, "max_qty": None, "price": int(product.get("price", 0))}]


def product_price(product, quantity):
    return price_for_quantity(product_tiers(product), quantity)


def product_price_label(product, lang="vi"):
    prices = [int(t["price"]) for t in product_tiers(product)]
    low, high = min(prices), max(prices)
    if lang == "en":
        if low == high:
            return usd_unit_text(low)
        return f"{usd_unit_text(low)}–{usd_unit_text(high)}"
    if low == high:
        return f"{low:,} VND"
    return f"{low:,}–{high:,} VND"


def product_warranty_days(product):
    try:
        days = int(product.get("warranty_days", WARRANTY_PERIOD_DAYS))
    except (TypeError, ValueError):
        days = WARRANTY_PERIOD_DAYS
    # 0 is valid: it means "no warranty".
    return days if days >= 0 else WARRANTY_PERIOD_DAYS


def warranty_days_label(days, lang="vi"):
    if days <= 0:
        return pick(lang, "Không bảo hành", "No warranty")
    return pick(lang, f"{days} ngày", f"{days} days")


def warranty_footer(days, lang="vi"):
    if days <= 0:
        return pick(lang, "Không có bảo hành", "No warranty")
    return pick(
        lang,
        f"Thời hạn bảo hành: {days} ngày",
        f"Warranty period: {days} days"
    )


def product_stock_path(product):
    return os.path.join(DATA_DIR, product["stock_file"])


def load_product_stock(product):
    if product.get("tiered") or product["id"] == DEFAULT_PRODUCT_ID:
        return load_stock()
    return [line.strip() for line in STORE.read_stock(product_stock_path(product)).decode("utf-8").splitlines() if line.strip()]


def save_product_stock(product, stock):
    if product.get("tiered") or product["id"] == DEFAULT_PRODUCT_ID:
        save_stock(stock)
    else:
        _write_text(product_stock_path(product), "".join(item + "\n" for item in stock))



def increment_product_sold(product_id, quantity):
    data = load_products_data()

    for item in data["products"]:
        if item["id"] == product_id:
            item["sold"] = int(item.get("sold", 0)) + int(quantity)
            save_products_data(data)
            return


def new_product_stock_filename(name, data):
    ascii_name = unicodedata.normalize("NFKD", name.casefold().replace("đ", "d"))
    ascii_name = ascii_name.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")[:60] or "product"
    used = {item["stock_file"].casefold() for item in data["products"]}
    used.update(filename.casefold() for filename in os.listdir(DATA_DIR))
    filename = f"stock_{slug}.txt"
    suffix = 2
    while filename.casefold() in used:
        filename = f"stock_{slug}_{suffix}.txt"
        suffix += 1
    return filename


def product_stock_snapshot(product):
    """Read exact bytes: even whitespace or undecodable contents require confirmation."""
    filename = product["stock_file"]
    is_default = product["id"] == DEFAULT_PRODUCT_ID and filename == "accounts.txt"
    if not is_default and (filename != os.path.basename(filename) or not filename.startswith("stock_")
            or not filename.endswith(".txt")):
        raise ValueError("Invalid product stock filename")
    path = os.path.abspath(product_stock_path(product))
    if os.path.islink(path) or os.path.commonpath([os.path.realpath(path), os.path.realpath(DATA_DIR)]) != os.path.realpath(DATA_DIR):
        raise ValueError("Stock file must be a regular file inside data")
    content = STORE.read_stock(path)
    return path, content, hashlib.sha256(content).hexdigest()


def add_or_update_product(name, price, warranty_days):
    """
    Create a product, or update price/warranty when the name already exists.
    Returns (status, product, old_product) with status one of:
    "added", "updated", "default_name", "limit".
    """

    data = load_products_data()
    if not data["default_deleted"] and name.casefold() == PRODUCT_NAME.casefold():
        return "default_name", None, None

    for item in data["products"]:
        if item["name"].casefold() == name.casefold():
            old = dict(item)
            item["name"] = name
            item["price"] = int(price)
            item["tiers"] = [dict(t) for t in item["tiers"]]
            item["tiers"][0]["price"] = int(price)
            item["warranty_days"] = int(warranty_days)
            save_products_data(data)
            product = dict(item)
            product["tiered"] = False
            return "updated", product, old

    if len(data["products"]) >= MAX_EXTRA_PRODUCTS:
        return "limit", None, None

    existing_ids = {item["id"] for item in data["products"]}
    next_id = data["next_id"]
    while f"p{next_id}" in existing_ids:
        next_id += 1

    product_id = f"p{next_id}"

    item = {
        "id": product_id,
        "name": name,
        "price": int(price),
        "tiers": [{"min_qty": 1, "max_qty": None, "price": int(price)}],
        "warranty_days": int(warranty_days),
        "stock_file": new_product_stock_filename(name, data),
        "sold": 0,
        "created_at": datetime.now().isoformat(),
    }

    data["products"].append(item)
    data["next_id"] = next_id + 1
    product = dict(item)
    product["tiered"] = False
    with data_transaction():
        _write_text(product_stock_path(product), "")
        save_products_data(data)
    return "added", product, None


def set_product_tier_price(product_id, min_qty, max_qty, price):
    """Add/edit a tier. Returns (status, info).

    status ok        -> info is the old tier (None if a tier was added)
    status overlap   -> info is the existing tier that overlaps
    status limit / not_found -> info is None
    """

    try:
        if product_id == DEFAULT_PRODUCT_ID:
            return "ok", set_default_tier(min_qty, max_qty, price)

        data = load_products_data()

        for item in data["products"]:
            if item["id"] == product_id:
                new, old = upsert_tier(item["tiers"], min_qty, max_qty, price)
                item["tiers"] = new
                item["price"] = new[0]["price"]
                save_products_data(data)
                return "ok", old

        return "not_found", None

    except TierOverlapError as e:
        return "overlap", e.tier

    except TierLimitError:
        return "limit", None


def remove_product_tier(product_id, min_qty):
    """Remove a tier. Returns (status, removed_tier): ok/missing/last/not_found."""

    if product_id == DEFAULT_PRODUCT_ID:
        return remove_default_tier(min_qty)

    data = load_products_data()

    for item in data["products"]:
        if item["id"] == product_id:
            status, new, removed = remove_tier(item["tiers"], min_qty)
            if status in ("ok", "deferred"):
                item["tiers"] = new
                item["price"] = new[0]["price"]
                save_products_data(data)
            return status, removed

    return "not_found", None


def set_product_warranty(product_id, days):
    """Change a product's warranty for NEW orders. Returns the old days or None."""

    if product_id == DEFAULT_PRODUCT_ID:
        return set_default_warranty_days(days)

    data = load_products_data()

    for item in data["products"]:
        if item["id"] == product_id:
            old = int(item["warranty_days"])
            item["warranty_days"] = int(days)
            save_products_data(data)
            return old

    return None


def remove_product(product_id, confirmed_stock_digest=None):
    """Remove catalog + stock together; nonempty stock needs its exact digest confirmed."""
    data = load_products_data()
    if product_id == DEFAULT_PRODUCT_ID:
        if data["default_deleted"]:
            return "not_found", None
        product = get_default_product()
        if any(item["stock_file"].casefold() == "accounts.txt" for item in data["products"]):
            raise ValueError("Stock is shared by another product; removal blocked")
        path, content, digest = product_stock_snapshot(product)
        if content and confirmed_stock_digest != digest:
            return "stock_confirmation_required", product
        data["default_deleted"] = True
        with data_transaction():
            STORE.delete(path)
            save_products_data(data)
        return "removed", product
    for index, item in enumerate(data["products"]):
        if item["id"] != product_id:
            continue
        # Never delete a file referenced by another catalog entry.
        if any(other["id"] != product_id and other["stock_file"].casefold() == item["stock_file"].casefold()
               for other in data["products"]):
            raise ValueError("Stock file is shared by another product; removal blocked")
        path, content, digest = product_stock_snapshot(item)
        product = dict(item, tiered=False)
        if content and confirmed_stock_digest != digest:
            return "stock_confirmation_required", product
        data["products"].pop(index)
        with data_transaction():
            STORE.delete(path)
            save_products_data(data)
        return "removed", product
    return "not_found", None



# =========================================================
# USER
# =========================================================

def ensure_user(user):

    uid = str(
        user.id
    )

    changed = False

    if uid not in users:

        users[uid] = {
            "username": user.name,
            "balance": 0,
            "language": "vi"
        }

        changed = True
        log_activity(uid, user.name, "user_registered", {"display_name": user.display_name})

    else:

        if users[uid].get(
            "username"
        ) != user.name:

            users[uid]["username"] = (
                user.name
            )

            changed = True

        if "balance" not in users[uid]:

            users[uid]["balance"] = 0

            changed = True

        if users[uid].get("language") not in ("vi", "en"):
            users[uid]["language"] = "vi"
            changed = True

        if "language" not in users[uid]:
            users[uid]["language"] = "vi"
            changed = True

    if changed:

        save_json(
            USERS_FILE,
            users
        )

    return users[uid]


def get_user_balance(user_id):
    return vnd(users.get(str(user_id), {}).get("balance", 0))


# =========================================================
# TOPUP HISTORY + FULL USER ACTIVITY LOG
# =========================================================

def load_topup_history():
    data = load_json(TOPUP_HISTORY_FILE, [])
    return data if isinstance(data, list) else []


def save_topup_history(items):
    save_json(TOPUP_HISTORY_FILE, items)


def add_topup_history(user_id, username, amount_vnd, method,
                      transaction_id="", status="completed",
                      amount_usd=None, order_id=""):
    items = load_topup_history()
    items.append({
        "user_id": int(user_id),
        "username": str(username),
        "amount_vnd": vnd(amount_vnd),
        "method": str(method),
        "transaction_id": str(transaction_id),
        "order_id": str(order_id),
        "amount_usd": str(amount_usd) if amount_usd is not None else None,
        "status": str(status),
        "created_at": datetime.now().isoformat()
    })
    save_topup_history(items)


def load_activity_logs():
    data = load_json(ACTIVITY_LOG_FILE, {})
    return data if isinstance(data, dict) else {}


def log_activity(user_id, username, action, details=None,
                 interaction_type="system"):
    """Append an audit event. Never stores passwords, tokens, or account stock."""
    try:
        logs = load_activity_logs()
        uid = str(user_id)
        bucket = logs.setdefault(uid, [])
        bucket.append({
            "timestamp": datetime.now().isoformat(),
            "user_id": int(user_id),
            "username": str(username),
            "action": str(action),
            "details": details if isinstance(details, dict) else {"message": str(details or "")},
            "interaction_type": str(interaction_type)
        })
        # Keep every event for /checkusername; only cap a corrupted/extreme file.
        if len(bucket) > 10000:
            logs[uid] = bucket[-10000:]
        save_json(ACTIVITY_LOG_FILE, logs)
    except Exception as e:
        if STORE.db.in_transaction:
            raise
        print(f"[AUDIT] Log error ({type(e).__name__})")


def get_activity_for_username(username):
    username = str(username).strip().lower()
    matches = []
    for uid, data in users.items():
        saved = str(data.get("username", "")) if isinstance(data, dict) else ""
        if saved.lower() == username:
            matches.append(str(uid))
    logs = load_activity_logs()
    for uid, entries in logs.items():
        if any(str(x.get("username", "")).lower() == username for x in entries if isinstance(x, dict)):
            if uid not in matches:
                matches.append(uid)
    return matches


def get_user_topups(user_id, limit=50):
    items = [x for x in load_topup_history() if str(x.get("user_id")) == str(user_id)]
    items.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    return items[:limit]


# =========================================================
# NUMBER PARSER
# =========================================================

def parse_number(value):

    if value is None:
        return Decimal("0")

    if isinstance(value, bool):
        return Decimal("0")

    if isinstance(value, int):
        return Decimal(value)

    if isinstance(value, float):

        return Decimal(
            str(value)
        )

    text = str(
        value
    ).strip()

    if not text:
        return Decimal("0")

    text = re.sub(
        r"[^\d,.\-]",
        "",
        text
    )

    if not text:
        return Decimal("0")

    try:

        if (
            text.count(".") > 1
            and
            "," not in text
        ):

            return Decimal(
                text.replace(".", "")
            )


        if (
            text.count(",") > 1
            and
            "." not in text
        ):

            return Decimal(
                text.replace(",", "")
            )


        if (
            "." in text
            and
            "," in text
        ):

            last_dot = text.rfind(".")
            last_comma = text.rfind(",")

            if last_comma > last_dot:

                integer_part = text[:last_comma]
                decimal_part = text[last_comma + 1:]

            else:

                integer_part = text[:last_dot]
                decimal_part = text[last_dot + 1:]

            integer_part = re.sub(
                r"[.,]",
                "",
                integer_part
            )

            return Decimal(
                f"{integer_part}.{decimal_part}"
            )


        if "." in text:

            parts = text.split(".")

            if (
                len(parts) == 2
                and
                len(parts[1]) == 3
            ):

                return Decimal(
                    parts[0] + parts[1]
                )

            return Decimal(text)


        if "," in text:

            parts = text.split(",")

            if (
                len(parts) == 2
                and
                len(parts[1]) == 3
            ):

                return Decimal(
                    parts[0] + parts[1]
                )

            return Decimal(
                text.replace(",", ".")
            )


        return Decimal(text)

    except Exception:

        return Decimal("0")


# =========================================================
# VCB PARSER
# =========================================================

def normalize_key(value):

    return re.sub(
        r"[^a-z0-9]",
        "",
        str(value).lower()
    )


def first_value(item, keys):

    if not isinstance(
        item,
        dict
    ):

        return None

    normalized = {
        normalize_key(k): v
        for k, v in item.items()
    }

    for key in keys:

        key = normalize_key(
            key
        )

        if key in normalized:

            return normalized[key]

    return None


def transaction_score(item):

    if not isinstance(
        item,
        dict
    ):

        return 0

    keys = {
        normalize_key(k)
        for k in item.keys()
    }

    id_keys = {
        "transactionid",
        "transid",
        "tranid",
        "reference",
        "referenceno",
        "referenceid",
        "refno",
        "transactioncode",
        "trace",
        "traceid",
        "tid",
        "ftid",
        "id"
    }

    amount_keys = {
        "amount",
        "transactionamount",
        "tranamount",
        "creditamount",
        "debitamount",
        "amountin",
        "amountout",
        "credit",
        "debit",
        "sotien",
        "value"
    }

    description_keys = {
        "description",
        "transactiondescription",
        "transactiondesc",
        "transactioncontent",
        "content",
        "transfercontent",
        "transferdescription",
        "addinfo",
        "narration",
        "remark",
        "note",
        "comments",
        "noidung",
        "diengiai"
    }

    date_keys = {
        "date",
        "time",
        "transactiondate",
        "transactiontime",
        "datetime",
        "createdat",
        "createddate"
    }

    score = 0

    score += len(
        keys & id_keys
    ) * 4

    score += len(
        keys & amount_keys
    ) * 3

    score += len(
        keys & description_keys
    ) * 3

    score += len(
        keys & date_keys
    )

    return score


def extract_transactions(payload):

    candidates = []

    def walk(obj):

        if isinstance(
            obj,
            list
        ):

            dict_items = [
                x
                for x in obj
                if isinstance(x, dict)
            ]

            if dict_items:

                sample = dict_items[:20]

                avg_score = (
                    sum(
                        transaction_score(x)
                        for x in sample
                    )
                    /
                    len(sample)
                )

                candidates.append(
                    (
                        avg_score,
                        len(dict_items),
                        dict_items
                    )
                )

            for item in obj:
                walk(item)


        elif isinstance(
            obj,
            dict
        ):

            for value in obj.values():
                walk(value)


    walk(payload)

    if not candidates:
        return []

    candidates.sort(
        key=lambda x: (
            x[0],
            x[1]
        ),
        reverse=True
    )

    best = candidates[0]

    return best[2]


def parse_amount(item):

    credit = first_value(
        item,
        [
            "creditAmount",
            "amountIn",
            "credit",
            "moneyIn",
            "incomingAmount"
        ]
    )

    if credit is not None:

        amount = parse_number(
            credit
        )

        if amount > 0:
            return amount


    amount = first_value(
        item,
        [
            "amount",
            "transactionAmount",
            "tranAmount",
            "sotien",
            "value"
        ]
    )

    return parse_number(
        amount
    )


def parse_description(item):

    value = first_value(
        item,
        [
            "description",
            "transactionDescription",
            "transactionDesc",
            "transactionContent",
            "content",
            "transferContent",
            "transferDescription",
            "addInfo",
            "narration",
            "remark",
            "note",
            "comments",
            "noidung",
            "diengiai"
        ]
    )

    if value is None:
        return ""

    return str(
        value
    ).strip()


def parse_transaction_id(item):

    value = first_value(
        item,
        [
            "transactionId",
            "transId",
            "tranId",
            "reference",
            "referenceNo",
            "referenceId",
            "refNo",
            "transactionCode",
            "trace",
            "traceId",
            "tid",
            "ftid",
            "id"
        ]
    )

    if value is not None:

        return str(
            value
        ).strip()


    raw = json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True
    )

    import hashlib

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def is_outgoing_transaction(item):

    credit = first_value(
        item,
        [
            "credit",
            "creditAmount",
            "amountIn",
            "incomingAmount"
        ]
    )

    debit = first_value(
        item,
        [
            "debit",
            "debitAmount",
            "amountOut",
            "outgoingAmount"
        ]
    )

    if credit is not None:

        if parse_number(credit) > 0:
            return False

    if debit is not None:

        if parse_number(debit) > 0:
            return True


    direction = first_value(
        item,
        [
            "type",
            "direction",
            "transactionType",
            "creditDebit",
            "debitCredit",
            "loai"
        ]
    )

    if direction is None:
        return False

    direction = str(
        direction
    ).lower()

    outgoing_words = [
        "out",
        "outgoing",
        "debit",
        "withdraw",
        "withdrawal",
        "chi",
        "ra",
        "chuyen di",
        "chuyendi"
    ]

    return any(
        word in direction
        for word in outgoing_words
    )


# =========================================================
# FETCH VCB
# =========================================================

def fetch_vcb_history():
    if not VCB_API_TOKEN:
        return None
    try:
        response = requests.get(f"https://api.sieuthicode.net/historyapivcb/{VCB_API_TOKEN}", timeout=20)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            return None
        if isinstance(payload, dict) and str(payload.get('code', '00')).lower() not in ('00', '0', '200', 'success'):
            return None
        return payload
    except (requests.RequestException, ValueError):
        return None


def bank_transfer_code(description):
    match = re.search(r"\bLC([A-Za-z0-9_.-]+)", description, re.IGNORECASE)
    return match.group(1) if match else None


def credit_bank_payment(transaction_id, user_id, actor_id='bank', reason='automatic'):
    payment = STORE.bank_payment(transaction_id)
    user_id = str(user_id)
    if payment is None or payment['status'] != 'pending' or user_id not in users:
        raise ValueError('Payment is not pending or user does not exist')
    amount = vnd(payment['amount_vnd'])
    with data_transaction():
        # Native unique ID is claimed before the balance changes, in the same commit.
        if not mark_transaction_processed(transaction_id):
            raise ValueError('Payment was already processed')
        old = get_user_balance(user_id)
        new = vnd(old + amount)
        users[user_id]['balance'] = new
        save_json(USERS_FILE, users)
        STORE.settle_bank_payment(transaction_id, 'credited', user_id, actor_id)
        add_topup_history(user_id, users[user_id].get('username', ''), amount, 'Vietcombank',
                          transaction_id=transaction_id, status='completed')
        log_activity(user_id, users[user_id].get('username', ''), 'topup_completed', {
            'method': 'Vietcombank', 'amount_vnd': amount, 'transaction_id': transaction_id,
            'balance_before': old, 'balance_after': new, 'actor_id': str(actor_id), 'reason': reason[:200]
        }, 'bank')
    return new


async def process_bank_transactions():
    payload = await asyncio.to_thread(fetch_vcb_history)
    if payload is None:
        await service_result('bank', False, 'unavailable_or_invalid_response')
        return
    try:
        items = extract_transactions(payload)
    except (ValueError, TypeError, RecursionError):
        await service_result('bank', False, 'invalid_response_structure')
        return
    await service_result('bank', True)
    credited = 0
    pending = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            transaction_id = parse_transaction_id(item)
            if is_transaction_processed(transaction_id):
                continue
            if is_outgoing_transaction(item):
                async with SHOP_TRANSACTION_LOCK:
                    with data_transaction():
                        mark_transaction_processed(transaction_id)
                continue
            amount = vnd(parse_amount(item))
            if amount <= 0:
                continue
            description = parse_description(item)
            code = bank_transfer_code(description)
            # A fallback hash is retained for review, never for automatic credit.
            stable_id = first_value(item, ['transactionId','transId','tranId','reference','referenceNo',
                'referenceId','refNo','transactionCode','trace','traceId','tid','ftid','id'])
            matches = []
            if code:
                if code.isdigit():
                    matches = [code] if code in users else []
                else:
                    matches = [uid for uid, user in users.items() if str(user.get('username','')).casefold() == code.casefold()]
            reason = ('restore_paused' if STORE.payments_paused() else 'no_stable_id' if stable_id is None or not str(stable_id).strip() else
                      'no_code' if not code else 'user_not_found' if not matches else
                      'ambiguous_user' if len(matches) != 1 else 'legacy_username' if not code.isdigit() else 'matched')
            async with SHOP_TRANSACTION_LOCK:
                old = STORE.bank_payment(transaction_id)
                if old is not None:
                    continue  # Pending payments require explicit admin reconciliation.
                STORE.observe_bank_payment(transaction_id, amount, description, reason)
                if reason != 'matched':
                    pending += 1
                    continue
                new_balance = credit_bank_payment(transaction_id, matches[0])
            credited += 1
            try:
                recipient = await bot.fetch_user(int(matches[0]))
                await recipient.send(f"💰 +{amount:,} VND • Số dư / Balance: {new_balance:,} VND")
            except Exception:
                pass  # Credit is committed; never retry credit because notification failed.
        except (ValueError, TypeError, KeyError):
            logging.warning('[BANK] Invalid transaction requires review; no amount was credited')
        except Exception as exc:
            logging.error('[BANK] Processing failed (%s)', type(exc).__name__)
            await service_result('bank-processing', False, type(exc).__name__)
    if credited or pending:
        logging.info('[BANK] credited=%s new_pending=%s', credited, pending)
    if pending:
        await notify_admins(f"Có {pending} giao dịch ngân hàng mới cần đối soát. Dùng /pendingtopups.")


# =========================================================
# PAYFJORD
# =========================================================

def create_payfjord_invoice(
    username,
    amount_usd,
    request_id
):

    payload = {

        "merchant_id": MERCHANT_ID,

        "api_key": PAYFJORD_API_KEY,

        "name": (
            f"Lemon Service - {username}"
        ),

        "amount": str(
            amount_usd
        ),

        "request_id": request_id,

        "description": (
            f"Crypto topup - {username}"
        )
    }

    try:

        response = requests.post(
            PAYFJORD_ADD_INVOICE,
            data=payload,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        if isinstance(
            data,
            str
        ):

            data = json.loads(data)

        return data, None

    except Exception as e:

        return None, type(e).__name__


def get_payfjord_status(trans_id):

    payload = {

        "merchant_id": MERCHANT_ID,

        "api_key": PAYFJORD_API_KEY,

        "trans_id": trans_id
    }

    try:

        response = requests.post(
            PAYFJORD_STATUS,
            data=payload,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        if isinstance(
            data,
            str
        ):

            data = json.loads(data)

        return data, None

    except Exception as e:

        return None, type(e).__name__


# =========================================================
# CRYPTO PROCESS
# =========================================================

async def process_crypto_orders():

    if STORE.payments_paused():
        return
    if not crypto_orders:
        return

    for order_id in list(crypto_orders):
        order = crypto_orders[order_id]
        try:

            if order.get(
                "credited",
                False
            ):
                continue


            trans_id = order.get(
                "trans_id"
            )

            if not trans_id:
                continue


            if order.get("status") in ("expired", "cancelled", "failed"):
                continue
            if any(other.get("credited") and str(other.get("trans_id")) == str(trans_id)
                   for other in crypto_orders.values()):
                continue

            status_data, error = await asyncio.to_thread(
                get_payfjord_status,
                trans_id
            )

            if error:

                print(
                    f"[CRYPTO] Status error "
                    f"{order_id}: {error}"
                )

                await service_result('crypto', False, 'status_request_failed')
                continue

            await service_result('crypto', True)
            order = crypto_orders.get(order_id)
            if not order or order.get('credited') or str(order.get('trans_id')) != str(trans_id):
                continue
            data = status_data

            if isinstance(
                data,
                dict
            ):

                nested = data.get(
                    "data"
                )

                if isinstance(
                    nested,
                    dict
                ):

                    data = nested


            if not isinstance(
                data,
                dict
            ):
                continue


            status = str(
                data.get(
                    "status",
                    ""
                )
            ).lower()


            if status == "expired":

                order["status"] = "expired"

                save_json(
                    CRYPTO_ORDERS_FILE,
                    crypto_orders
                )

                continue


            if status != "completed":
                continue


            if order.get(
                "credited",
                False
            ):
                continue


            received = data.get(
                "received"
            )


            if received is None:

                received = data.get("amount")


            try:

                received_usd = Decimal(
                    str(received)
                )

            except (InvalidOperation, ValueError, TypeError):

                continue


            if not received_usd.is_finite() or received_usd <= 0 or received_usd > Decimal("1000000000"):
                continue


            credit_vnd = crypto_to_vnd(received_usd, USD_TO_VND)


            user_id = str(
                order["user_id"]
            )


            if user_id not in users:
                continue


            with data_transaction():
                if not STORE.claim_crypto_payment(trans_id):
                    continue
                old_balance = vnd(
                    users[user_id].get(
                        "balance",
                        0
                    )
                )


                new_balance = (
                    old_balance
                    +
                    credit_vnd
                )


                users[user_id]["balance"] = vnd(new_balance)


                order["credited"] = True
                order["status"] = "completed"
                order["received_usd"] = str(received_usd)
                order["credit_vnd"] = vnd(credit_vnd)
                order["completed_at"] = (
                    datetime.now().isoformat()
                )


                save_json(
                    USERS_FILE,
                    users
                )

                save_json(
                    CRYPTO_ORDERS_FILE,
                    crypto_orders
                )

                add_topup_history(
                    user_id,
                    order.get("username", "Unknown"),
                    credit_vnd,
                    "Crypto",
                    transaction_id=trans_id,
                    status="completed",
                    amount_usd=received_usd,
                    order_id=order_id
                )
                log_activity(
                    user_id,
                    order.get("username", "Unknown"),
                    "topup_completed",
                    {
                        "method": "Crypto",
                        "amount_usd": str(received_usd),
                        "amount_vnd": vnd(credit_vnd),
                        "transaction_id": str(trans_id),
                        "order_id": order_id,
                        "balance_before": vnd(old_balance),
                        "balance_after": vnd(new_balance)
                    },
                    "crypto"
                )

            print(
                "[CRYPTO] SUCCESS | "
                f"User={user_id} | "
                f"Received=${received_usd} | "
                f"Credit={credit_vnd} VND"
            )


            try:

                discord_user = await bot.fetch_user(
                    int(user_id)
                )

                dm_lang = get_user_language(user_id)

                await discord_user.send(
                    pick(
                        dm_lang,
                        "💰 **Nạp Crypto thành công**\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 Đã nhận: **${received_usd}**\n"
                        f"💵 Tổng cộng: **{credit_vnd:,.0f} VND**\n"
                        f"💳 Số dư mới: **{new_balance:,.0f} VND**\n"
                        f"🧾 Đơn hàng: `{order_id}`\n"
                        "━━━━━━━━━━━━━━━━━━",
                        "💰 **Crypto Top Up Success**\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 Received: **${received_usd}**\n"
                        f"💵 Total Amount: **{balance_money(credit_vnd, 'en')}**\n"
                        f"💳 New Balance: **{balance_money(new_balance, 'en')}**\n"
                        f"🧾 Order: `{order_id}`\n"
                        "━━━━━━━━━━━━━━━━━━"
                    )
                )

            except Exception as e:

                print(
                    "[CRYPTO] DM error: "
                    f"{e}"
                )


        except Exception as e:

            print(
                f"[CRYPTO] Order error "
                f"{order_id}: {e}"
            )


# =========================================================
# BOT
# =========================================================

intents = discord.Intents.default()

# Only non-privileged intents are used, so nothing has to be enabled in the
# Discord Developer Portal. Slash commands and buttons receive the member
# straight from the interaction, and the ticket system looks members up through
# the API (see get_guild_member) instead of relying on the members intent.
intents.guilds = True
intents.messages = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# commands.Bot already owns a command tree: shop and ticket commands share it.
tree = bot.tree


# =========================================================
# RUNTIME POLICY, HEALTH AND BACKUPS
# =========================================================

def can_manage(interaction):
    return POLICY.can_manage(interaction)


def admin_check():
    async def predicate(interaction):
        if not can_manage(interaction):
            raise app_commands.CheckFailure('Server or administrator is not authorized in operations.json')
        return True
    return app_commands.check(predicate)


async def ensure_allowed_interaction(interaction):
    guild = getattr(interaction, 'guild', None)
    if guild is None or POLICY.allowed_guild(guild.id):
        return True
    if not interaction.response.is_done():
        await interaction.response.send_message('❌ Bot không được cấu hình để hoạt động trong server này.', ephemeral=True)
    return False


async def notify_admins(message):
    channel_id = POLICY.config['alert_channel_id']
    if channel_id:
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            if POLICY.allowed_guild(channel.guild.id):
                await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
                return
        except Exception:
            pass
    for user_id in POLICY.config['admin_user_ids']:
        try:
            recipient = await bot.fetch_user(user_id)
            await recipient.send(message, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logging.warning('[ALERT] Could not notify configured administrator')


async def service_result(service, ok, detail=''):
    path = os.path.join(DATA_DIR, 'service_health.json')
    health = load_json(path, {})
    previous = health.get(service, {})
    failures = 0 if ok else int(previous.get('failures', 0)) + 1
    now = time.time()
    threshold = POLICY.config['bank_failure_threshold'] if service.startswith('bank') else 1
    alert = not ok and failures >= threshold and now - previous.get('last_alert', 0) >= POLICY.config['alert_repeat_seconds']
    recovered = ok and previous.get('alerted', False)
    health[service] = {
        'ok': ok, 'failures': failures, 'checked_at': now,
        'detail': detail, 'last_alert': now if alert else previous.get('last_alert', 0),
        'alerted': (previous.get('alerted', False) or alert) if not ok else False,
    }
    save_json(path, health)
    if alert:
        logging.error('[HEALTH] %s failed %s times (%s)', service, failures, detail)
        await notify_admins(f"⚠️ {service}: lỗi liên tiếp {failures} lần ({detail}). Kiểm tra /botstatus và nhật ký VPS.")
    elif recovered:
        logging.info('[HEALTH] %s recovered', service)
        await notify_admins(f"✅ {service} đã hoạt động trở lại.")


async def bootstrap_policy():
    if POLICY.config['allowed_guild_ids'] or not POLICY.config['bootstrap_from_stock_channel']:
        return
    try:
        channel = bot.get_channel(STOCK_CHANNEL_ID) or await bot.fetch_channel(STOCK_CHANNEL_ID)
        POLICY.config['allowed_guild_ids'] = [int(channel.guild.id)]
        POLICY.config['bootstrap_from_stock_channel'] = False
        POLICY.save()
        logging.info('[POLICY] Bound to the guild of the existing configured stock channel')
    except Exception:
        logging.error('[POLICY] Server not configured. Stop bot and run manage_bot.py configure --guild-id ID --admin-id ID')


@tasks.loop(minutes=1)
async def maintenance_loop():
    # A stalled asyncio loop stops this heartbeat; the Windows supervisor restarts it.
    Path(BASE_DIR, 'data', 'heartbeat').touch()
    last = load_json(os.path.join(DATA_DIR, 'backup_status.json'), {})
    if time.time() - last.get('completed_at', 0) < POLICY.config['backup_interval_hours'] * 3600:
        return
    try:
        target = await asyncio.to_thread(rotating_backup, BASE_DIR, POLICY.config['backup_keep'])
        save_json(os.path.join(DATA_DIR, 'backup_status.json'), {'completed_at': time.time(), 'file': target.name})
        await service_result('backup', True)
    except Exception as exc:
        await service_result('backup', False, type(exc).__name__)


async def _tree_interaction_check(interaction):
    if not await ensure_allowed_interaction(interaction):
        return False
    apply_locale_default(interaction)
    return True


tree.interaction_check = _tree_interaction_check


# =========================================================
# PREMIUM UI
# =========================================================

UI_GOLD = 0xF1C40F
UI_GREEN = 0x2ECC71
UI_RED = 0xE74C3C
UI_BLUE = 0x3498DB
UI_PURPLE = 0x9B59B6
UI_DARK = 0x2B2D31

SHOP_FOOTER = "🍋 Lemon Service • Secure Auto System"


def make_embed(
    title,
    description=None,
    color=UI_GOLD
):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now()
    )

    embed.set_footer(
        text=SHOP_FOOTER
    )

    return embed


def format_vnd(value):
    try:
        return f"{Decimal(str(value)):,.0f} VND"
    except Exception:
        return f"{value} VND"


# Below this many accounts the panel switches to the "low stock" look.
LOW_STOCK_THRESHOLD = 50

_PANEL_BANNER_CACHE = {}


def build_panel_banner(language="vi"):
    """
    Render the shop banner shown at the bottom of the panel embed.
    Returns PNG bytes (cached per language) or None if Pillow is unavailable.
    """

    lang = "en" if language == "en" else "vi"

    if lang in _PANEL_BANNER_CACHE:
        return _PANEL_BANNER_CACHE[lang]

    if Image is None:
        return None

    try:
        scale = 2
        width, height = 900, 280
        w, h = width * scale, height * scale

        def s(value):
            return int(value * scale)

        # Horizontal dark gradient
        img = Image.new("RGB", (w, h), (15, 23, 42))
        draw = ImageDraw.Draw(img)
        left, right = (15, 23, 42), (24, 52, 38)
        for x in range(w):
            t = x / (w - 1)
            color = tuple(int(left[i] + (right[i] - left[i]) * t) for i in range(3))
            draw.line([(x, 0), (x, h)], fill=color)

        # Soft yellow glow behind the logo
        glow = Image.new("RGB", (w, h), (0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.ellipse([s(10), s(10), s(290), s(250)], fill=(70, 62, 10))
        glow = glow.filter(ImageFilter.GaussianBlur(s(40))) if ImageFilter else glow
        img = Image.blend(img, Image.composite(glow, img, glow.convert("L")), 0.6)
        draw = ImageDraw.Draw(img)

        # Frame
        draw.rounded_rectangle(
            [s(8), s(8), s(width - 8), s(height - 8)],
            radius=s(26),
            outline=(250, 204, 21),
            width=s(3),
        )

        # ---- Lemon logo (drawn on its own layer, then tilted) ----------
        layer_w, layer_h = s(260), s(220)
        layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        lx, ly = layer_w // 2, layer_h // 2 + s(14)

        # tips
        ld.ellipse([lx - s(96), ly - s(14), lx - s(62), ly + s(14)], fill=(234, 179, 8))
        ld.ellipse([lx + s(62), ly - s(14), lx + s(96), ly + s(14)], fill=(234, 179, 8))
        # shadow + body
        ld.ellipse([lx - s(80), ly - s(52), lx + s(80), ly + s(64)], fill=(161, 128, 6))
        ld.ellipse([lx - s(80), ly - s(58), lx + s(80), ly + s(58)], fill=(250, 204, 21))
        # highlight
        ld.ellipse([lx - s(56), ly - s(42), lx - s(10), ly - s(14)], fill=(254, 240, 138))
        # leaf
        ld.polygon(
            [
                (lx + s(4), ly - s(56)),
                (lx + s(40), ly - s(86)),
                (lx + s(62), ly - s(62)),
                (lx + s(30), ly - s(50)),
            ],
            fill=(74, 222, 128),
        )
        ld.line(
            [(lx + s(8), ly - s(56)), (lx + s(50), ly - s(72))],
            fill=(22, 101, 52),
            width=s(3),
        )

        layer = layer.rotate(22, resample=Image.BICUBIC, expand=False)
        layer = layer.resize((int(layer_w * 0.82), int(layer_h * 0.82)), Image.LANCZOS)
        img.paste(
            layer,
            (s(128) - layer.width // 2, s(112) - layer.height // 2),
            layer
        )
        draw = ImageDraw.Draw(img)

        title_font = _load_font(_FONT_CANDIDATES_BOLD, s(64))
        sub_font = _load_font(_FONT_CANDIDATES_BOLD, s(24))
        pill_font = _load_font(_FONT_CANDIDATES_BOLD, s(21))

        # ---- Title ---------------------------------------------------
        title_x, title_y = s(250), s(36)
        draw.text((title_x, title_y), "LEMON", font=title_font, fill=(250, 204, 21))
        box = draw.textbbox((0, 0), "LEMON ", font=title_font)
        draw.text(
            (title_x + (box[2] - box[0]), title_y),
            "SERVICE",
            font=title_font,
            fill=(255, 255, 255),
        )

        subtitle = pick(
            lang,
            "CỬA HÀNG ACCOUNT ROBLOX",
            "ROBLOX ACCOUNT SHOP",
        )
        draw.text((title_x + s(4), s(118)), subtitle, font=sub_font, fill=(167, 243, 208))

        # ---- Feature pills ---------------------------------------------
        pills = [
            pick(lang, "GIAO TỰ ĐỘNG", "AUTO DELIVERY"),
            pick(
                lang,
                "BẢO HÀNH THEO SẢN PHẨM",
                "WARRANTY PER PRODUCT",
            ),
            "VCB • CRYPTO",
        ]
        pad_x, pill_h, gap = s(22), s(48), s(16)
        pill_size = 21
        # Shrink the pills until they fit comfortably inside the frame.
        while True:
            pill_font = _load_font(_FONT_CANDIDATES_BOLD, s(pill_size))
            sizes = []
            for label in pills:
                box = draw.textbbox((0, 0), label, font=pill_font)
                sizes.append(box[2] - box[0])
            total_w = sum(sizes) + pad_x * 2 * len(pills) + gap * (len(pills) - 1)
            if total_w <= w - s(70) or pill_size <= 14:
                break
            pill_size -= 1
            pad_x = max(s(12), pad_x - s(1))
        x = (w - total_w) // 2
        y = s(206)
        for label, text_w in zip(pills, sizes):
            pill_w = text_w + pad_x * 2
            draw.rounded_rectangle(
                [x, y, x + pill_w, y + pill_h],
                radius=pill_h // 2,
                fill=(20, 83, 65),
                outline=(52, 211, 153),
                width=s(2),
            )
            box = draw.textbbox((0, 0), label, font=pill_font)
            draw.text(
                (x + pad_x - box[0], y + (pill_h - (box[3] - box[1])) // 2 - box[1]),
                label,
                font=pill_font,
                fill=(209, 250, 229),
            )
            x += pill_w + gap

        img = img.resize((width, height), Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        _PANEL_BANNER_CACHE[lang] = data
        return data

    except Exception as e:
        print(f"[PANEL] Banner error: {e}")
        return None


def panel_banner_attach(language="vi"):
    """Keyword arguments that attach the banner to a send call ({} if unavailable)."""
    data = build_panel_banner(language)
    if not data:
        return {}
    return {
        "file": discord.File(
            io.BytesIO(data),
            filename="panel_banner.png"
        )
    }


# ---------- Panel / product UI helpers ----------

UI_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━"


def fmt_num(value, lang="vi"):
    text_value = f"{int(value):,}"
    return text_value.replace(",", ".") if lang == "vi" else text_value


def product_status(stock_count):
    """Returns (emoji, vi_label, en_label)."""
    if stock_count <= 0:
        return "🔴", "HẾT HÀNG", "OUT OF STOCK"
    if stock_count < LOW_STOCK_THRESHOLD:
        return "🟠", "SẮP HẾT HÀNG", "LOW STOCK"
    return "🟢", "CÒN HÀNG", "IN STOCK"


def _product_status_badge(stock_count, lang):
    emoji, vi_label, en_label = product_status(stock_count)
    return f"{emoji} **{pick(lang, vi_label, en_label)}**"


def product_price_text(product, lang="vi"):
    """Plain price text: '2.100 – 2.500 VND' (vi) or '$0.084 – $0.100' (en)."""
    prices = [int(t["price"]) for t in product_tiers(product)]
    low, high = min(prices), max(prices)
    if lang == "en":
        if low == high:
            return usd_unit_text(low)
        return f"{usd_unit_text(low)} – {usd_unit_text(high)}"
    if low == high:
        return f"{fmt_num(low, lang)} VND"
    return f"{fmt_num(low, lang)} – {fmt_num(high, lang)} VND"


def build_panel_embed(language="vi"):
    """Shop panel: clean product list only.
    Full details appear after pressing Buy and choosing a product."""

    lang = "en" if language == "en" else "vi"
    products = get_all_products()
    stock_counts = [len(load_product_stock(p)) for p in products]

    in_stock = sum(1 for c in stock_counts if c > 0)

    if in_stock == 0:
        color = UI_RED
    elif all(c < LOW_STOCK_THRESHOLD for c in stock_counts if c > 0):
        color = UI_GOLD
    else:
        color = UI_GREEN

    summary = pick(
        lang,
        f"📊 `{len(products)}` sản phẩm  •  🟢 `{in_stock}` đang còn hàng",
        f"📊 `{len(products)}` products  •  🟢 `{in_stock}` in stock"
    )

    embed = make_embed(
        text("panel_title", lang),
        (
            f"{UI_DIVIDER}\n"
            + pick(
                lang,
                "### 🛍️ DANH SÁCH SẢN PHẨM\n",
                "### 🛍️ PRODUCT LIST\n"
            )
            + f"{summary}\n"
            f"{UI_DIVIDER}\n"
            + pick(
                lang,
                "✦ Bấm **🛒 Mua hàng** bên dưới để chọn sản phẩm.\n"
                "✦ Giao tự động qua DM • Bảo hành rõ ràng.",
                "✦ Press **🛒 Buy** below to choose a product.\n"
                "✦ Automatic delivery via DM • Clear warranty.\n"
                f"✦ Prices shown in USD • 1 USD = {int(USD_TO_VND):,} VND"
            )
        ),
        color
    )

    for index, (product, stock_count) in enumerate(zip(products, stock_counts), start=1):
        days = product_warranty_days(product)
        label = pick(lang, "tài khoản", "accounts")

        embed.add_field(
            name=f"✨ {index}. {product['name']}"[:256],
            value=(
                f"> {_product_status_badge(stock_count, lang)}\n"
                f"> ┣ 💰 **{pick(lang, 'Giá', 'Price')}:** "
                f"`{product_price_text(product, lang)}` / acc\n"
                f"> ┣ 📦 **{pick(lang, 'Kho', 'Stock')}:** "
                f"`{fmt_num(stock_count, lang)}` {label}\n"
                f"> ┗ 🛡️ **{pick(lang, 'Bảo hành', 'Warranty')}:** "
                f"{warranty_days_label(days, lang)}"
            )[:1024],
            inline=False
        )

    embed.set_footer(
        text=SHOP_FOOTER + pick(
            lang,
            " • Tự cập nhật khi kho thay đổi",
            " • Updates when stock changes"
        )
    )

    if build_panel_banner(lang):
        embed.set_image(url="attachment://panel_banner.png")

    return embed


def build_choose_product_embed(language="vi"):
    lang = "en" if language == "en" else "vi"
    if not get_all_products():
        return make_embed(pick(lang, "📦 Chưa có sản phẩm", "📦 No Products Available"),
                          pick(lang, "Shop hiện chưa có sản phẩm để mua. Vui lòng quay lại sau.",
                               "There are no products to buy right now. Please check back later."), UI_GOLD)
    return make_embed(
        pick(lang, "🛍️ Chọn sản phẩm", "🛍️ Choose a Product"),
        (
            f"{UI_DIVIDER}\n"
            + pick(
                lang,
                "Chọn sản phẩm bạn muốn mua trong danh sách bên dưới.\n\n"
                "> 🟢 Còn hàng  •  🟠 Sắp hết  •  🔴 Hết hàng\n",
                "Pick the product you want to buy from the list below.\n\n"
                "> 🟢 In stock  •  🟠 Low stock  •  🔴 Sold out\n"
            )
            + UI_DIVIDER
        ),
        UI_BLUE
    )


def build_product_detail_embed(product, language="vi"):
    """Full product information, shown after the user picks a product."""

    lang = "en" if language == "en" else "vi"
    stock_count = len(load_product_stock(product))
    days = product_warranty_days(product)
    emoji, _, _ = product_status(stock_count)

    color = {"🔴": UI_RED, "🟠": UI_GOLD}.get(emoji, UI_GREEN)

    embed = make_embed(
        f"✨ {product['name']}"[:256],
        (
            f"{UI_DIVIDER}\n"
            f"{_product_status_badge(stock_count, lang)}  •  "
            f"`{fmt_num(stock_count, lang)}` "
            + pick(lang, "account sẵn sàng giao", "accounts ready to deliver")
            + f"\n{UI_DIVIDER}"
        ),
        color
    )

    tiers = product_tiers(product)

    if len(tiers) > 1:
        rows = tier_rows(tiers, lang)
        width = max(len(r[0]) for r in rows)
        table = "\n".join(
            f" {label.ljust(width)}  ➜  "
            + (usd_unit_text(price) if lang == "en" else f"{fmt_num(price, lang)} VND").rjust(9)
            + " / acc"
            for label, price in rows
        )
        embed.add_field(
            name=pick(lang, "💰 BẢNG GIÁ THEO SỐ LƯỢNG", "💰 VOLUME PRICING"),
            value=f"```\n{table}\n```"[:1024],
            inline=False
        )
    else:
        embed.add_field(
            name=pick(lang, "💰 Giá", "💰 Price"),
            value=f"`{product_price_text(product, lang)}` / acc",
            inline=True
        )

    if product.get("tiered"):
        embed.add_field(
            name=pick(lang, "📈 Đã bán", "📈 Sold"),
            value=f"`{fmt_num(get_total_sold(), lang)}` account",
            inline=True
        )

    embed.add_field(
        name=pick(lang, "📦 Kho hàng", "📦 Stock"),
        value=f"`{fmt_num(stock_count, lang)}` account",
        inline=True
    )
    embed.add_field(
        name=pick(lang, "🛡️ Bảo hành", "🛡️ Warranty"),
        value=warranty_days_label(days, lang),
        inline=True
    )
    embed.add_field(
        name=pick(lang, "⚡ Giao hàng", "⚡ Delivery"),
        value=pick(lang, "Tự động • Qua DM", "Automatic • Via DM"),
        inline=True
    )
    embed.add_field(
        name=pick(lang, "💳 Thanh toán", "💳 Payment"),
        value="Vietcombank • Crypto",
        inline=True
    )
    embed.add_field(
        name=pick(lang, "💱 Tỷ giá", "💱 Rate"),
        value=(
            f"`1 USD = {int(USD_TO_VND):,} VND`" if lang == "en"
            else f"`1 USD = {int(USD_TO_VND):,} VND`".replace(",", ".")
        ),
        inline=True
    )

    embed.set_footer(
        text=SHOP_FOOTER + pick(
            lang,
            " • Bấm Tiếp tục để nhập số lượng",
            " • Press Continue to enter quantity"
        )
    )

    return embed


def build_stock_embed(language="vi"):
    return build_panel_embed(language)


class ShopPanelView(View):
    """
    Persistent premium shop panel.
    All buttons are stateless so the view survives bot restarts.
    """

    async def interaction_check(self, interaction: discord.Interaction):
        if not await ensure_allowed_interaction(interaction):
            return False
        apply_locale_default(interaction)
        return True

    def __init__(self, language="vi"):
        super().__init__(timeout=None)
        self.language = "en" if language == "en" else "vi"
        labels = {
            "lemon:shop:buy": text("buy", self.language),
            "lemon:shop:profile": text("profile", self.language),
            "lemon:shop:topup": text("topup", self.language),
            "lemon:shop:crypto": text("crypto", self.language),
            "lemon:shop:stock": text("stock", self.language),
            "lemon:shop:orders": text("history", self.language),
            "lemon:shop:english": text("language", self.language),
        }
        for child in self.children:
            custom_id = getattr(child, "custom_id", None)
            if custom_id in labels:
                child.label = labels[custom_id]
                if custom_id == "lemon:shop:english":
                    child.emoji = "🇬🇧" if self.language == "vi" else "🇻🇳"

    @discord.ui.button(
        label="Buy",
        emoji="🛒",
        style=discord.ButtonStyle.success,
        custom_id="lemon:shop:buy",
        row=0
    )
    async def buy_button(
        self,
        interaction: discord.Interaction,
        button: Button
    ):
        if not await ensure_allowed_interaction(interaction):
            return False
        ensure_user(interaction.user)
        await open_buy_flow(interaction)

    @discord.ui.button(
        label="Profile",
        emoji="👤",
        style=discord.ButtonStyle.primary,
        custom_id="lemon:shop:profile",
        row=0
    )
    async def profile_button(
        self,
        interaction: discord.Interaction,
        button: Button
    ):
        if not await ensure_allowed_interaction(interaction):
            return False
        user = ensure_user(interaction.user)
        balance = parse_number(
            user.get("balance", 0)
        )

        lang = get_user_language(interaction.user.id)
        if lang == "en":
            embed = make_embed(
                "👤 Your Profile",
                f"Welcome back, **{interaction.user.display_name}**.\n\nHere is your current account overview.",
                UI_BLUE
            )
            username_label, id_label, balance_label = "👤 Username", "🆔 User ID", "💳 Balance"
        else:
            embed = make_embed(
                "👤 Hồ sơ của bạn",
                f"Chào mừng **{interaction.user.display_name}**.\n\nThông tin tài khoản của bạn:",
                UI_BLUE
            )
            username_label, id_label, balance_label = "👤 Tên người dùng", "🆔 User ID", "💳 Số dư"

        if interaction.user.avatar:
            embed.set_thumbnail(
                url=interaction.user.avatar.url
            )

        embed.add_field(
            name=username_label,
            value=f"`{interaction.user.name}`",
            inline=True
        )

        embed.add_field(
            name=id_label,
            value=f"`{interaction.user.id}`",
            inline=True
        )

        embed.add_field(
            name=balance_label,
            value=f"**{balance_money(balance, lang)}**",
            inline=False
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    @discord.ui.button(
        label="Topup Vietnamese Bank",
        emoji="💳",
        style=discord.ButtonStyle.primary,
        custom_id="lemon:shop:topup",
        row=1
    )
    async def topup_button(
        self,
        interaction: discord.Interaction,
        button: Button
    ):
        if not await ensure_allowed_interaction(interaction):
            return False
        ensure_user(interaction.user)
        await interaction.response.send_modal(
            TopupModal(get_user_language(interaction.user.id))
        )

    @discord.ui.button(
        label="Crypto",
        emoji="🪙",
        style=discord.ButtonStyle.secondary,
        custom_id="lemon:shop:crypto",
        row=1
    )
    async def crypto_button(
        self,
        interaction: discord.Interaction,
        button: Button
    ):
        if not await ensure_allowed_interaction(interaction):
            return False
        ensure_user(interaction.user)
        await interaction.response.send_modal(
            CryptoModal(get_user_language(interaction.user.id))
        )

    @discord.ui.button(
        label="Stock",
        emoji="📦",
        style=discord.ButtonStyle.secondary,
        custom_id="lemon:shop:stock",
        row=1
    )
    async def stock_button(
        self,
        interaction: discord.Interaction,
        button: Button
    ):
        if not await ensure_allowed_interaction(interaction):
            return False
        lang = get_user_language(interaction.user.id)
        await interaction.response.send_message(
            embed=build_stock_embed(lang),
            view=ShopPanelView(lang),
            ephemeral=True,
            **panel_banner_attach(lang)
        )

    @discord.ui.button(
        label="History",
        emoji="🧾",
        style=discord.ButtonStyle.secondary,
        custom_id="lemon:shop:orders",
        row=2
    )
    async def orders_button(
        self,
        interaction: discord.Interaction,
        button: Button
    ):
        if not await ensure_allowed_interaction(interaction):
            return False
        recent = get_user_orders(interaction.user.id, 10)
        topups = get_user_topups(interaction.user.id, 10)
        lang = get_user_language(interaction.user.id)
        if lang == "en":
            embed = make_embed("🧾 History", "Your top-up history and order history.", UI_PURPLE)
            topup_title, order_title = "💳 Top Up History", "🧾 Order History"
            no_topup, no_order = "No top-up history yet.", "No completed orders yet."
        else:
            embed = make_embed("🧾 Lịch sử", "Lịch sử nạp tiền và lịch sử đơn hàng của bạn.", UI_PURPLE)
            topup_title, order_title = "💳 Lịch sử nạp tiền", "🧾 Lịch sử đơn hàng"
            no_topup, no_order = "Chưa có lịch sử nạp tiền.", "Chưa có đơn hàng hoàn tất."
        if topups:
            embed.add_field(
                name=topup_title,
                value="\n".join(
                    f"`{x.get('created_at', '')[:19]}` • **{balance_money(x.get('amount_vnd', 0), lang)}** • {x.get('method', 'Unknown')} • `{x.get('transaction_id') or x.get('order_id') or '-'} `"
                    for x in topups[:10]
                ),
                inline=False
            )
        else:
            embed.add_field(name=topup_title, value=no_topup, inline=False)
        if recent:
            order_lines = []
            for oid, o in recent[:10]:
                # Convert the multi-line warranty status into a compact History entry.
                status = get_order_warranty_status(o)
                if status.get("no_warranty"):
                    warranty_line = (
                        "⚪ Warranty: **NONE**" if lang == "en"
                        else "⚪ Bảo hành: **KHÔNG CÓ**"
                    )
                elif status["active"]:
                    countdown = format_warranty_remaining(status["remaining"])
                    warranty_line = (
                        f"🛡️ Warranty: **{countdown}**" if lang == "en"
                        else f"🛡️ Bảo hành còn: **{countdown}**"
                    )
                else:
                    warranty_line = (
                        "🔴 Warranty: **EXPIRED**" if lang == "en"
                        else "🔴 Bảo hành: **ĐÃ HẾT HẠN**"
                    )
                order_lines.append(
                    f"`{oid}` • **{o.get('quantity', 0):,} acc** • **{balance_money(o.get('total', 0), lang)}**\n{warranty_line}"
                )
            embed.add_field(
                name=order_title,
                value="\n\n".join(order_lines)[:1024],
                inline=False
            )
        else:
            embed.add_field(name=order_title, value=no_order, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="English",
        emoji="🇬🇧",
        style=discord.ButtonStyle.primary,
        custom_id="lemon:shop:english",
        row=2
    )
    async def english_button(
        self,
        interaction: discord.Interaction,
        button: Button
    ):
        # Always determine the user's current language at click time.
        # Do not use self.language here because this persistent view is shared
        # by the public stock message and can be created with the default VI
        # language after a bot restart.
        if not await ensure_allowed_interaction(interaction):
            return False
        ensure_user(interaction.user)
        current = get_user_language(interaction.user.id)
        new_language = "en" if current != "en" else "vi"

        set_user_language(interaction.user.id, new_language)
        log_activity(
            interaction.user.id,
            interaction.user.name,
            "language_changed",
            {"from": current, "to": new_language},
            "button"
        )

        # Build a completely fresh localized panel for this user immediately.
        # This prevents the first click from only changing the stored language
        # while leaving the visible panel in the old language.
        localized_view = ShopPanelView(new_language)
        localized_embed = build_localized_panel_embed(new_language)

        await interaction.response.send_message(
            embed=localized_embed,
            view=localized_view,
            ephemeral=True,
            **panel_banner_attach(new_language)
        )


# =========================================================
# SALES / ORDERS
# =========================================================

def generate_order_id():
    import secrets
    return f"LC-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"


def load_sales():
    data = load_json(SALES_FILE, {"total_sold": 0})
    if not isinstance(data, dict):
        data = {"total_sold": 0}
    try:
        data["total_sold"] = int(data.get("total_sold", 0))
    except Exception:
        data["total_sold"] = 0
    return data


def get_total_sold():
    return load_sales().get("total_sold", 0)


def load_orders():
    data = load_json(ORDERS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_order(order_id, order):
    # Keep the central index for History / /order.
    orders = load_orders()
    orders[order_id] = order
    save_json(ORDERS_FILE, orders)
    if order.get('delivery_status') == 'pending':
        STORE.enqueue_delivery(order_id, order['user_id'])

    # Also save an archive document in SQLite so the order
    # can be checked easily for warranty purposes. This document contains
    # the exact purchased account list and order metadata.
    archive_path = os.path.join(
        ORDER_ARCHIVE_DIR,
        f"{order_id}.json"
    )
    save_json(archive_path, {
        "order_id": order_id,
        **order
    })


def get_order_archive_path(order_id):
    return os.path.join(
        ORDER_ARCHIVE_DIR,
        f"{order_id}.json"
    )


def cleanup_expired_orders():
    now = datetime.now()
    orders = load_orders()
    with data_transaction():
        for order_id, order in list(orders.items()):
            if STORE.delivery_pending(order_id):
                continue
            raw = order.get("created_at") if isinstance(order, dict) else None
            if not raw:
                continue
            try:
                created = datetime.fromisoformat(str(raw))
                expired = created <= now - timedelta(days=max(ORDER_RETENTION_DAYS, get_order_warranty_days(order)))
            except (TypeError, ValueError):
                continue
            if expired:
                orders.pop(order_id)
                STORE.delete(get_order_archive_path(order_id))
        save_json(ORDERS_FILE, orders)
        # Remove old orphaned database archives as well.
        rows = STORE.db.execute("SELECT path, payload FROM documents WHERE path LIKE 'data/orders/%.json'").fetchall()
        for path, payload in rows:
            if os.path.basename(path)[:-5] in orders or STORE.delivery_pending(os.path.basename(path)[:-5]):
                continue
            item = json.loads(payload)
            try:
                created = datetime.fromisoformat(str(item["created_at"]))
                expired = created <= now - timedelta(days=max(ORDER_RETENTION_DAYS, get_order_warranty_days(item)))
            except (KeyError, TypeError, ValueError):
                continue
            if expired:
                STORE.delete(os.path.join(BASE_DIR, path))


def find_order_by_account(account):
    """Find the active order containing an exact purchased account line."""
    account = account.strip()
    if not account:
        return None

    for order_id, order in load_orders().items():
        if not isinstance(order, dict):
            continue

        accounts = order.get("accounts", [])
        if account in accounts:
            return order_id, order

    return None


def get_order_warranty_days(order):
    """Warranty length saved with the order; older orders use the default."""
    try:
        days = int(order.get("warranty_days", WARRANTY_PERIOD_DAYS))
    except (AttributeError, TypeError, ValueError):
        days = WARRANTY_PERIOD_DAYS
    # 0 is valid: the order was sold without warranty.
    return days if days >= 0 else WARRANTY_PERIOD_DAYS


def get_order_warranty_status(order):
    """Return detailed warranty status for an order."""
    if not isinstance(order, dict):
        return {
            "active": False,
            "expired": True,
            "remaining": timedelta(0),
            "expires_at": None,
            "purchased_at": None,
            "error": "invalid_order"
        }

    created_raw = order.get("created_at")
    if not created_raw:
        return {
            "active": False,
            "expired": True,
            "remaining": timedelta(0),
            "expires_at": None,
            "purchased_at": None,
            "error": "missing_created_at"
        }

    try:
        created_at = datetime.fromisoformat(str(created_raw))
    except (TypeError, ValueError):
        return {
            "active": False,
            "expired": True,
            "remaining": timedelta(0),
            "expires_at": None,
            "purchased_at": None,
            "error": "invalid_created_at"
        }

    # Keep all calculations in local naive datetime, matching the existing
    # order timestamps generated by this bot.
    warranty_days = get_order_warranty_days(order)
    expires_at = created_at + timedelta(days=warranty_days)
    remaining = expires_at - datetime.now()
    active = remaining.total_seconds() > 0

    return {
        "active": active,
        "expired": not active,
        "remaining": remaining if active else timedelta(0),
        "expires_at": expires_at,
        "purchased_at": created_at,
        "no_warranty": warranty_days == 0,
        "error": None
    }


def format_warranty_remaining(remaining):
    """Format the remaining warranty countdown as days/hours/minutes/seconds."""
    total_seconds = max(0, int(remaining.total_seconds()))
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{days}d {hours:02d}h {minutes:02d}m {seconds:02d}s"


def warranty_status_text(order, lang="vi"):
    status = get_order_warranty_status(order)
    if status.get("no_warranty"):
        return "⚪ **NO WARRANTY**" if lang == "en" else "⚪ **KHÔNG BẢO HÀNH**"
    if status["active"]:
        remaining_text = format_warranty_remaining(status["remaining"])
        expires_text = status["expires_at"].strftime("%Y-%m-%d %H:%M:%S")
        if lang == "en":
            return (
                f"🟢 **ACTIVE**\n"
                f"⏳ Remaining: **{remaining_text}**\n"
                f"📅 Expires: `{expires_text}`"
            )
        return (
            f"🟢 **CÒN HIỆU LỰC**\n"
            f"⏳ Còn lại: **{remaining_text}**\n"
            f"📅 Hết hạn: `{expires_text}`"
        )

    if lang == "en":
        return (
            "🔴 **EXPIRED**\n"
            f"📅 Warranty expired: `{status['expires_at'].strftime('%Y-%m-%d %H:%M:%S') if status['expires_at'] else 'Unknown'}`"
        )
    return (
        "🔴 **ĐÃ HẾT HẠN**\n"
        f"📅 Bảo hành hết hạn: `{status['expires_at'].strftime('%Y-%m-%d %H:%M:%S') if status['expires_at'] else 'Không xác định'}`"
    )


def get_user_orders(user_id, limit=10):
    result = []
    for order_id, order in load_orders().items():
        if str(order.get("user_id")) == str(user_id):
            result.append((order_id, order))
    result.sort(key=lambda item: item[1].get("created_at", ""), reverse=True)
    return result[:limit]




# =========================================================
# LANGUAGE / VIETNAMESE DEFAULT + ENGLISH TOGGLE
# =========================================================

VI_TEXT = {
    "panel_title": "🍋 Lemon Service • Cửa Hàng Account",
    "in_stock": "🟢 **CÒN HÀNG**",
    "out_stock": "🔴 **HẾT HÀNG**",
    "pricing": "💰 **BẢNG GIÁ**",
    "exchange": "💱 Tỷ giá: `1 USD = 25.000 VND`",
    "refresh": "⚡ Stock cập nhật: **mỗi 5 giây**",
    "quick": "### 🛒 Mua nhanh",
    "quick_desc": "Sử dụng các nút bên dưới để mua account, xem profile, nạp tiền hoặc xem lịch sử.",
    "availability": "📦 Số lượng",
    "sold": "📈 Đã bán",
    "delivery": "⚡ Giao hàng",
    "payment": "🔐 Thanh toán",
    "automatic_dm": "Tự động • Gửi qua DM",
    "buy": "Mua hàng",
    "profile": "Hồ sơ",
    "topup": "Nạp tiền",
    "crypto": "Crypto",
    "stock": "Kho hàng",
    "history": "Lịch sử",
    "language": "English",
}

EN_TEXT = {
    "panel_title": "🍋 Lemon Service • Account Shop",
    "in_stock": "🟢 **IN STOCK**",
    "out_stock": "🔴 **OUT OF STOCK**",
    "pricing": "💰 **PRICING**",
    "exchange": "💱 Exchange Rate: `1 USD = 25,000 VND`",
    "refresh": "⚡ Stock refresh: **every 5 seconds**",
    "quick": "### 🛒 Quick Purchase",
    "quick_desc": "Use the buttons below to buy accounts, check your profile, add balance, or view history.",
    "availability": "📦 Availability",
    "sold": "📈 Accounts Sold",
    "delivery": "⚡ Delivery",
    "payment": "🔐 Payment",
    "automatic_dm": "Automatic • DM delivery",
    "buy": "Buy",
    "profile": "Profile",
    "topup": "Top Up",
    "crypto": "Crypto",
    "stock": "Stock",
    "history": "History",
    "language": "Tiếng Việt",
}


def set_user_language(user_id, language):
    uid = str(user_id)
    if uid not in users:
        users[uid] = {"username": "Unknown", "balance": 0, "language": "vi"}
    users[uid]["language"] = "en" if str(language).lower() == "en" else "vi"
    save_json(USERS_FILE, users)


def apply_locale_default(interaction):
    """First contact only: a new customer whose Discord client is not Vietnamese
    starts in English (USD prices). Existing customers keep their choice."""
    try:
        uid = str(interaction.user.id)
        if uid in users:
            return
        locale = str(getattr(interaction, "locale", "") or "").lower()
        language = "vi" if locale.startswith("vi") else "en"
        users[uid] = {
            "username": interaction.user.name,
            "balance": 0,
            "language": language
        }
        save_json(USERS_FILE, users)
        log_activity(
            uid,
            interaction.user.name,
            "user_registered",
            {"display_name": interaction.user.display_name, "locale": locale}
        )
    except Exception as e:
        print(f"[LANG] Locale default error: {e}")


def get_user_language(user_id):
    value = str(users.get(str(user_id), {}).get("language", "vi")).lower()
    return value if value in ("vi", "en") else "vi"


def text(key, language="vi"):
    return (EN_TEXT if language == "en" else VI_TEXT).get(key, key)


def pick(language, vi, en):
    """Return the Vietnamese or English string according to the user's panel language."""
    return en if language == "en" else vi


def build_localized_panel_embed(language="vi"):
    return build_panel_embed(language)


def build_english_panel_embed():
    return build_localized_panel_embed("en")


def build_price_table(language="vi", tiers=None):
    rows = tier_rows(tiers or DEFAULT_TIERS, language)
    icons = ["🟡", "🟢", "🔵", "🟣", "🟠", "🔴", "⚪", "🟤"]
    lines = []
    for index, (label, price) in enumerate(rows):
        price_text = (
            usd_unit_text(price) if language == "en" else f"{price:,} VND"
        )
        lines.append(f"{icons[index % len(icons)]} **{label}** → `{price_text} / acc`")
    return "\n".join(lines)[:1024]


# =========================================================
# STOCK EMBED
# =========================================================

_BANNER_UPGRADE_FAILED = False

# Every channel (in one or many servers) that shows the shop panel.
STOCK_PANELS_FILE = os.path.join(DATA_DIR, "stock_panels.json")

# The loop, purchases and /setstockmessage may update panels at the same time.
STOCK_PANEL_LOCK = asyncio.Lock()

# Channels whose last update failed, so a broken channel is logged once
# instead of every 5 seconds.
_PANEL_FAILURES = {}


def load_stock_panels():
    """Returns {channel_id: {"message_id": int or None, "banner": bool}}."""

    if STORE.exists(STOCK_PANELS_FILE):
        data = load_json(STOCK_PANELS_FILE, {})
        raw = data if isinstance(data, dict) else {}

    else:
        # First start after the update: keep the panel that already exists.
        raw = {}
        old = load_json(STOCK_MESSAGE_FILE, {})
        if isinstance(old, dict) and old.get("message_id"):
            raw[str(STOCK_CHANNEL_ID)] = {
                "message_id": old.get("message_id"),
                "banner": bool(old.get("banner"))
            }
        save_json(STOCK_PANELS_FILE, raw)

    panels = {}
    for key, value in raw.items():
        try:
            message_id = value.get("message_id")
            panels[str(int(key))] = {
                "message_id": int(message_id) if message_id else None,
                "banner": bool(value.get("banner"))
            }
        except (TypeError, ValueError, AttributeError):
            continue

    return panels


PANEL_MIN_EDIT_SECONDS = 10
PANEL_VERIFY_SECONDS = 300
_PANEL_RENDER_STATE = {}
_PANEL_RETRY_AFTER = {}


def panel_render_digest(embed, view):
    payload = embed.to_dict()
    payload.pop('timestamp', None)  # Rendering time is not a stock change.
    return hashlib.sha256(json.dumps({'embed': payload, 'view': view.to_components()},
                                    sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


async def update_stock_panel(channel_id, panels):
    """Only edit changed content; periodic GET verifies that the panel still exists."""
    global _BANNER_UPGRADE_FAILED
    channel_id = int(channel_id)
    now = time.monotonic()
    if now < _PANEL_RETRY_AFTER.get(channel_id, 0):
        return 'deferred'
    key = str(channel_id)
    saved = dict(panels.get(key) or {'message_id': None, 'banner': False})
    embed = build_stock_embed('vi')
    view = ShopPanelView('vi')
    digest = panel_render_digest(embed, view)
    message_id = saved.get('message_id')
    state = _PANEL_RENDER_STATE.get(channel_id)
    if state and state['message_id'] == message_id:
        if state['digest'] == digest and now - state['verified_at'] < PANEL_VERIFY_SECONDS:
            return 'ok'
        if now - state['edited_at'] < PANEL_MIN_EDIT_SECONDS:
            return 'deferred'  # Keep the old digest so the next poll applies the latest stock.
    else:
        state = None
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if not POLICY.allowed_guild(getattr(getattr(channel, 'guild', None), 'id', None)):
            return 'forbidden'
        message = None
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                state = None
        if message is not None:
            if state and state['digest'] == digest:
                state['verified_at'] = time.monotonic()
                return 'ok'
            if build_panel_banner('vi') and not saved.get('banner') and not _BANNER_UPGRADE_FAILED:
                attach = panel_banner_attach('vi')
                if attach:
                    await message.edit(embed=embed, view=view, attachments=[attach['file']])
                    saved['banner'] = True
                else:
                    await message.edit(embed=embed, view=view)
            else:
                await message.edit(embed=embed, view=view)
            panels[key] = saved
        else:
            message = await channel.send(embed=embed, view=view, **panel_banner_attach('vi'))
            panels[key] = {'message_id': message.id, 'banner': bool(build_panel_banner('vi'))}
        now = time.monotonic()
        _PANEL_RENDER_STATE[channel_id] = {'message_id': message.id, 'digest': digest,
                                         'edited_at': now, 'verified_at': now}
        _PANEL_RETRY_AFTER.pop(channel_id, None)
        return 'ok'
    except discord.NotFound:
        _PANEL_RENDER_STATE.pop(channel_id, None)
        return 'not_found'
    except discord.Forbidden:
        _PANEL_RETRY_AFTER[channel_id] = time.monotonic() + 60
        return 'forbidden'
    except Exception as exc:
        # discord.py handles ordinary 429 retries; back off too if it surfaces an error.
        cooldown = 60 if getattr(exc, 'status', None) == 429 else 30
        _PANEL_RETRY_AFTER[channel_id] = time.monotonic() + cooldown
        logging.warning('[STOCK] Panel update failed (%s); backing off %ss', type(exc).__name__, cooldown)
        return 'error'


async def update_stock_message():
    """Refresh the panel in every registered channel (and the default one)."""

    async with STOCK_PANEL_LOCK:

        panels = load_stock_panels()
        before = json.dumps(panels, sort_keys=True)

        channel_ids = [STOCK_CHANNEL_ID] + [
            int(key) for key in panels if int(key) != STOCK_CHANNEL_ID
        ]

        results = await asyncio.gather(
            *(update_stock_panel(cid, panels) for cid in channel_ids)
        )

        for channel_id, status in zip(channel_ids, results):

            if status in ("ok", "deferred"):
                _PANEL_FAILURES.pop(channel_id, None)
                continue

            if _PANEL_FAILURES.get(channel_id) != status:
                _PANEL_FAILURES[channel_id] = status
                print(
                    f"[STOCK] Panel update failed for channel "
                    f"{channel_id}: {status}"
                )

            # The channel was deleted: stop trying (the default one is kept).
            if status == "not_found" and channel_id != STOCK_CHANNEL_ID:
                panels.pop(str(channel_id), None)

        if json.dumps(panels, sort_keys=True) != before:
            save_json(STOCK_PANELS_FILE, panels)


# =========================================================
# BUY MODAL
# =========================================================

class BuyModal(Modal):

    def __init__(self, language="vi", product=None):
        lang = "en" if language == "en" else "vi"
        chosen = product or get_default_product()

        if chosen.get("tiered"):
            modal_title = pick(lang, "🛒 Mua Account", "🛒 Purchase Accounts")
        else:
            modal_title = f"🛒 {chosen['name']}"[:45]

        super().__init__(
            title=modal_title
        )

        self.product = chosen

        self.quantity = TextInput(
            label=pick(lang, "Số lượng", "Quantity"),
            placeholder=pick(
                lang,
                "Nhập số lượng account",
                "Enter the number of accounts"
            ),
            required=True,
            min_length=1,
            max_length=15
        )

        self.add_item(
            self.quantity
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        lang = get_user_language(interaction.user.id)

        try:
            quantity = int(
                self.quantity.value.strip()
            )

        except ValueError:
            await interaction.response.send_message(
                pick(
                    lang,
                    "❌ **Số lượng không hợp lệ**\n"
                    "Vui lòng nhập một số nguyên, ví dụ `10`.",
                    "❌ **Invalid quantity**\n"
                    "Please enter a whole number such as `10`."
                ),
                ephemeral=True
            )
            return

        if quantity < 1:
            await interaction.response.send_message(
                pick(
                    lang,
                    "❌ Số lượng phải **từ 1 trở lên**.",
                    "❌ Quantity must be **1 or greater**."
                ),
                ephemeral=True
            )
            return

        price = product_price(
            self.product,
            quantity
        )

        if price is None:
            available = "\n".join(
                f"• **{label}** → {unit_money(tier_price, lang)}"
                for label, tier_price in tier_rows(
                    product_tiers(self.product), lang
                )
            )
            await interaction.response.send_message(
                pick(
                    lang,
                    f"❌ Chưa có giá cho **{quantity:,}** account.\n"
                    f"Các mức đang bán:\n{available}",
                    f"❌ There is no price for **{quantity:,}** accounts.\n"
                    f"Available quantities:\n{available}"
                ),
                ephemeral=True
            )
            return

        stock = load_product_stock(self.product)

        if len(stock) < quantity:
            embed = make_embed(
                pick(lang, "📦 Không đủ hàng", "📦 Not Enough Stock"),
                pick(
                    lang,
                    f"Số lượng yêu cầu: **{quantity:,}**\n"
                    f"Còn trong kho: **{len(stock):,}**\n\n"
                    "Vui lòng chọn số lượng nhỏ hơn.",
                    f"Requested: **{quantity:,}**\n"
                    f"Available: **{len(stock):,}**\n\n"
                    "Please try a smaller quantity."
                ),
                UI_RED
            )

            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )
            return

        ensure_user(
            interaction.user
        )

        balance = Decimal(
            str(
                get_user_balance(
                    interaction.user.id
                )
            )
        )

        total = Decimal(
            price * quantity
        )

        if balance < total:

            embed = make_embed(
                pick(lang, "💳 Số dư không đủ", "💳 Insufficient Balance"),
                pick(
                    lang,
                    f"Số dư hiện tại: **{format_vnd(balance)}**\n"
                    f"Tổng đơn hàng: **{format_vnd(total)}**\n\n"
                    "Hãy dùng nút **Nạp tiền** để nạp thêm trước khi mua.",
                    f"Current balance: **{balance_money(balance, 'en')}**\n"
                    f"Order total: **{balance_money(total, 'en')}**\n\n"
                    "Use **Top Up** (🪙 Crypto) to add balance before purchasing."
                ),
                UI_RED
            )

            await interaction.response.send_message(
                embed=embed,
                view=ShopPanelView(lang),
                ephemeral=True
            )
            return

        embed = make_embed(
            pick(lang, "🧾 Xác nhận đơn hàng", "🧾 Confirm Your Order"),
            (
                pick(
                    lang,
                    "Vui lòng kiểm tra đơn hàng trước khi xác nhận.",
                    "Please review your order before confirming."
                )
                + "\n\n"
                f"**{self.product['name']}**"
            ),
            UI_GOLD
        )

        embed.add_field(
            name=pick(lang, "📦 Số lượng", "📦 Quantity"),
            value=pick(
                lang,
                f"**{quantity:,}** account",
                f"**{quantity:,}** accounts"
            ),
            inline=True
        )

        embed.add_field(
            name=pick(lang, "💰 Đơn giá", "💰 Unit Price"),
            value=f"**{unit_money(price, lang)}**",
            inline=True
        )

        embed.add_field(
            name=pick(lang, "💵 Tổng tiền", "💵 Total"),
            value=f"**{balance_money(total, lang)}**",
            inline=True
        )

        embed.add_field(
            name=pick(lang, "💳 Số dư của bạn", "💳 Your Balance"),
            value=f"**{balance_money(balance, lang)}**",
            inline=True
        )

        embed.add_field(
            name=pick(lang, "📦 Kho sau khi mua", "📦 Stock After Order"),
            value=f"**{len(stock) - quantity:,}**",
            inline=True
        )

        embed.add_field(
            name=pick(lang, "⚡ Giao hàng", "⚡ Delivery"),
            value=pick(lang, "Tự động qua DM", "Automatic via DM"),
            inline=True
        )

        warranty_days = product_warranty_days(self.product)

        embed.add_field(
            name=pick(lang, "🛡️ Bảo hành", "🛡️ Warranty"),
            value=warranty_days_label(warranty_days, lang),
            inline=True
        )

        buy_tiers = product_tiers(self.product)
        matched_tier = None
        for tier_item in buy_tiers:
            tier_end = tier_item.get("max_qty")
            if tier_item["min_qty"] <= quantity and (tier_end is None or quantity <= tier_end):
                matched_tier = tier_item

        if (
            len(buy_tiers) > 1
            and matched_tier is not None
            and matched_tier is not buy_tiers[0]
        ):
            embed.add_field(
                name=pick(lang, "🔥 Giá sỉ", "🔥 Bulk Price"),
                value=pick(
                    lang,
                    f"Bậc **{tier_label(buy_tiers, matched_tier['min_qty'], lang)}** account → "
                    f"**{unit_money(matched_tier['price'], lang)} / acc**",
                    f"Tier **{tier_label(buy_tiers, matched_tier['min_qty'], lang)}** accounts → "
                    f"**{unit_money(matched_tier['price'], lang)} / acc**"
                ),
                inline=False
            )

        view = ConfirmBuyView(
            interaction.user.id,
            quantity,
            price,
            lang,
            self.product
        )

        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )


# =========================================================
# PRODUCT CHOOSER
# =========================================================

class ProductSelect(discord.ui.Select):

    def __init__(self, language, products):
        lang = "en" if language == "en" else "vi"

        options = []
        for product in products:
            days = product_warranty_days(product)
            stock_count = len(load_product_stock(product))
            warranty_part = pick(
                lang,
                "Không BH" if days == 0 else f"BH {days} ngày",
                "No warranty" if days == 0 else f"{days}d warranty"
            )
            description = pick(
                lang,
                f"{product_price_label(product, lang)} • {warranty_part} • Kho {stock_count:,}",
                f"{product_price_label(product, lang)} • {warranty_part} • Stock {stock_count:,}"
            )
            options.append(
                discord.SelectOption(
                    label=product["name"][:100],
                    value=product["id"],
                    description=description[:100],
                    emoji=product_status(stock_count)[0]
                )
            )

        super().__init__(
            placeholder=pick(lang, "Chọn sản phẩm", "Choose a product"),
            min_values=1,
            max_values=1,
            options=options
        )

        self.language = lang

    async def callback(self, interaction: discord.Interaction):

        if not await ensure_allowed_interaction(interaction):
            return False
        product = get_product_by_id(self.values[0])

        if product is None:
            await interaction.response.send_message(
                pick(
                    self.language,
                    "❌ Sản phẩm này không còn khả dụng.",
                    "❌ This product is no longer available."
                ),
                ephemeral=True
            )
            return

        await interaction.response.edit_message(
            embed=build_product_detail_embed(product, self.language),
            view=ProductDetailView(
                interaction.user.id,
                self.language,
                product
            )
        )


class ProductDetailView(View):
    """Shows the full product info; Continue -> quantity form -> confirm."""

    def __init__(self, user_id, language, product):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.language = "en" if language == "en" else "vi"
        self.product = product

        self.continue_button.label = pick(
            self.language, "Tiếp tục mua", "Continue"
        )
        self.back_button.label = pick(
            self.language, "Chọn sản phẩm khác", "Other products"
        )

    async def interaction_check(self, interaction: discord.Interaction):
        if not await ensure_allowed_interaction(interaction):
            return False
        return interaction.user.id == self.user_id

    @discord.ui.button(
        label="Continue",
        emoji="🛒",
        style=discord.ButtonStyle.success,
        row=0
    )
    async def continue_button(
        self,
        interaction: discord.Interaction,
        button: Button
    ):
        # Re-read the product so price/stock are fresh.
        if not await ensure_allowed_interaction(interaction):
            return False
        product = get_product_by_id(self.product["id"])

        if product is None:
            await interaction.response.send_message(
                pick(
                    self.language,
                    "❌ Sản phẩm này không còn khả dụng.",
                    "❌ This product is no longer available."
                ),
                ephemeral=True
            )
            return

        await interaction.response.send_modal(
            BuyModal(self.language, product)
        )

    @discord.ui.button(
        label="Back",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        row=0
    )
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: Button
    ):
        if not await ensure_allowed_interaction(interaction):
            return False
        await interaction.response.edit_message(
            embed=build_choose_product_embed(self.language),
            view=ProductSelectView(
                interaction.user.id,
                self.language,
                get_all_products()
            )
        )


class ProductSelectView(View):

    def __init__(self, user_id, language, products):
        super().__init__(timeout=120)
        self.user_id = user_id
        if products:
            self.add_item(ProductSelect(language, products))

    async def interaction_check(self, interaction: discord.Interaction):
        if not await ensure_allowed_interaction(interaction):
            return False
        return interaction.user.id == self.user_id


async def open_buy_flow(interaction: discord.Interaction):
    """Buy button and /buy: straight to the quantity form, or pick a product first."""

    if STORE.payments_paused():
        await interaction.response.send_message('🛠️ Shop đang đối chiếu dữ liệu sau khôi phục. Vui lòng quay lại sau.', ephemeral=True)
        return
    lang = get_user_language(interaction.user.id)
    products = get_all_products()

    embed = build_choose_product_embed(lang)

    await interaction.response.send_message(
        embed=embed,
        view=ProductSelectView(interaction.user.id, lang, products),
        ephemeral=True
    )


# =========================================================
# CONFIRM BUY
# =========================================================

# =========================================================
# PURCHASE SUCCESSFUL IMAGE
# =========================================================

_FONT_CANDIDATES_BOLD = [
    "arialbd.ttf",
    "Arial Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]

_FONT_CANDIDATES_REGULAR = [
    "arial.ttf",
    "Arial.ttf",
    "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _load_font(candidates, size):
    """Load the first available TrueType font, else Pillow's built-in font."""
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def build_purchase_success_image(order_id, quantity, total_vnd, warranty_until=None, language="vi"):
    """
    Render the "Purchase Successful" card and return it as PNG bytes in a
    BytesIO (position 0), or None if Pillow is unavailable / rendering fails.
    The delivery flow must never depend on this image.
    """

    if Image is None:
        return None

    try:
        lang = "en" if language == "en" else "vi"
        labels = {
            "vi": {
                "title": "Mua Hàng Thành Công",
                "sub": "Account đã được gửi vào DM của bạn",
                "order": "MÃ ĐƠN",
                "qty": "SỐ LƯỢNG",
                "paid": "ĐÃ THANH TOÁN",
                "warranty": "Bảo hành đến",
                "no_warranty": "Không bảo hành",
            },
            "en": {
                "title": "Purchase Successful",
                "sub": "Your accounts have been delivered to your DM",
                "order": "ORDER ID",
                "qty": "QUANTITY",
                "paid": "TOTAL PAID",
                "warranty": "Warranty until",
                "no_warranty": "No warranty",
            },
        }[lang]

        scale = 2  # supersample for smooth edges
        width, height = 900, 520
        w, h = width * scale, height * scale

        def s(value):
            return int(value * scale)

        # Vertical dark gradient background
        img = Image.new("RGB", (w, h), (18, 24, 38))
        draw = ImageDraw.Draw(img)
        top, bottom = (17, 24, 39), (8, 47, 40)
        for y in range(h):
            t = y / (h - 1)
            color = tuple(
                int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)
            )
            draw.line([(0, y), (w, y)], fill=color)

        # Card
        draw.rounded_rectangle(
            [s(30), s(30), s(width - 30), s(height - 30)],
            radius=s(28),
            fill=(24, 33, 52),
            outline=(52, 211, 153),
            width=s(3),
        )

        # Check-mark badge
        cx, cy, r = s(width // 2), s(120), s(52)
        draw.ellipse(
            [cx - r - s(10), cy - r - s(10), cx + r + s(10), cy + r + s(10)],
            fill=(20, 83, 65),
        )
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(52, 211, 153))
        draw.line(
            [
                (cx - s(24), cy + s(2)),
                (cx - s(7), cy + s(20)),
                (cx + s(27), cy - s(18)),
            ],
            fill=(255, 255, 255),
            width=s(10),
            joint="curve",
        )
        for px, py in (
            (cx - s(24), cy + s(2)),
            (cx - s(7), cy + s(20)),
            (cx + s(27), cy - s(18)),
        ):
            draw.ellipse(
                [px - s(5), py - s(5), px + s(5), py + s(5)],
                fill=(255, 255, 255),
            )

        title_font = _load_font(_FONT_CANDIDATES_BOLD, s(46))
        sub_font = _load_font(_FONT_CANDIDATES_REGULAR, s(22))
        label_font = _load_font(_FONT_CANDIDATES_REGULAR, s(18))
        value_font = _load_font(_FONT_CANDIDATES_BOLD, s(26))

        def centered(text_value, y, font, fill):
            box = draw.textbbox((0, 0), text_value, font=font)
            draw.text(
                ((w - (box[2] - box[0])) // 2, y),
                text_value,
                font=font,
                fill=fill,
            )

        centered(labels["title"], s(196), title_font, (255, 255, 255))
        centered(
            labels["sub"],
            s(256),
            sub_font,
            (167, 243, 208),
        )

        # Divider
        draw.line(
            [(s(80), s(310)), (s(width - 80), s(310))],
            fill=(51, 65, 85),
            width=s(2),
        )

        # Three info columns: Order ID / Quantity / Total Paid
        columns = [
            (labels["order"], str(order_id)),
            (labels["qty"], f"{int(quantity):,}"),
            (
                labels["paid"],
                usd_text(total_vnd) if lang == "en" else f"{int(total_vnd):,} VND"
            ),
        ]
        # Wider middle space for the long order ID, narrower for quantity
        weights = [1.35, 0.75, 1.1]
        usable = w - s(160)
        cursor_x = s(80)
        for index, (label, value) in enumerate(columns):
            col_width = int(usable * weights[index] / sum(weights))
            center_x = cursor_x + col_width // 2
            cursor_x += col_width

            # Shrink the value font until it fits inside its column
            value_size = 26
            value_font = _load_font(_FONT_CANDIDATES_BOLD, s(value_size))
            while value_size > 14:
                box = draw.textbbox((0, 0), value, font=value_font)
                if (box[2] - box[0]) <= col_width - s(16):
                    break
                value_size -= 2
                value_font = _load_font(_FONT_CANDIDATES_BOLD, s(value_size))

            box = draw.textbbox((0, 0), label, font=label_font)
            draw.text(
                (center_x - (box[2] - box[0]) // 2, s(334)),
                label,
                font=label_font,
                fill=(148, 163, 184),
            )

            box = draw.textbbox((0, 0), value, font=value_font)
            draw.text(
                (center_x - (box[2] - box[0]) // 2, s(366)),
                value,
                font=value_font,
                fill=(255, 255, 255),
            )

        # Warranty pill: "Warranty until dd/mm/YYYY HH:MM" or "No warranty"
        pill_font = _load_font(_FONT_CANDIDATES_BOLD, s(24))
        pill_text = (
            labels["warranty"] + " " + warranty_until.strftime("%d/%m/%Y %H:%M")
            if warranty_until is not None
            else labels["no_warranty"]
        )
        box = draw.textbbox((0, 0), pill_text, font=pill_font)
        text_w, text_h = box[2] - box[0], box[3] - box[1]
        pad_x, pill_h = s(30), s(50)
        pill_left = (w - text_w) // 2 - pad_x
        pill_right = (w + text_w) // 2 + pad_x
        pill_top = s(424)
        draw.rounded_rectangle(
            [pill_left, pill_top, pill_right, pill_top + pill_h],
            radius=pill_h // 2,
            fill=(20, 83, 65),
            outline=(52, 211, 153),
            width=s(2),
        )
        draw.text(
            ((w - text_w) // 2 - box[0], pill_top + (pill_h - text_h) // 2 - box[1]),
            pill_text,
            font=pill_font,
            fill=(209, 250, 229),
        )

        img = img.resize((width, height), Image.LANCZOS)

        buffer = io.BytesIO()
        img.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        return buffer

    except Exception as e:
        print(f"[BUY] Success image error: {e}")
        return None


class ConfirmBuyView(View):

    def __init__(
        self,
        user_id,
        quantity,
        price,
        language="vi",
        product=None
    ):
        super().__init__(
            timeout=60
        )

        self.completed = False
        self.user_id = user_id
        self.quantity = quantity
        self.price = price
        self.product = product or get_default_product()
        self.language = "en" if language == "en" else "vi"

        # Button labels follow the customer's panel language.
        # Children keep the definition order: [0] = confirm, [1] = cancel.
        if len(self.children) >= 2:
            self.children[0].label = pick(
                self.language,
                "Xác nhận mua",
                "Confirm Purchase"
            )
            self.children[1].label = pick(
                self.language,
                "Hủy",
                "Cancel"
            )

    @discord.ui.button(
        label="Confirm Purchase",
        emoji="✅",
        style=discord.ButtonStyle.success
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        lang = get_user_language(interaction.user.id)

        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                pick(
                    lang,
                    "❌ Đơn hàng này thuộc về người dùng khác.",
                    "❌ This order belongs to another user."
                ),
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with SHOP_TRANSACTION_LOCK:
            if self.completed:
                await interaction.followup.send("Đơn hàng này đã được xử lý / Already processed.", ephemeral=True)
                return

            if STORE.payments_paused():
                await interaction.followup.send('🛠️ Shop đang đối chiếu dữ liệu sau khôi phục. Bạn chưa bị trừ tiền.', ephemeral=True)
                return

            # The product may have been removed after this order was opened.
            if get_product_by_id(self.product["id"]) is None:
                await interaction.edit_original_response(
                    content="",
                    embed=make_embed(
                        pick(lang, "🚫 Sản phẩm đã ngừng bán", "🚫 Product Unavailable"),
                        pick(
                            lang,
                            "Sản phẩm này không còn được bán. Bạn chưa bị trừ tiền.",
                            "This product is no longer sold. You have not been charged."
                        ),
                        UI_RED
                    ),
                    view=None
                )
                return

            stock = load_product_stock(self.product)

            if len(stock) < self.quantity:
                await interaction.edit_original_response(
                    content="",
                    embed=make_embed(
                        pick(lang, "📦 Kho hàng đã thay đổi", "📦 Stock Changed"),
                        pick(
                            lang,
                            "Số lượng bạn yêu cầu không còn đủ.",
                            "The requested quantity is no longer available."
                        ),
                        UI_RED
                    ),
                    view=None
                )
                return

            ensure_user(
                interaction.user
            )

            total = (
                self.quantity
                *
                self.price
            )

            balance = get_user_balance(
                interaction.user.id
            )

            if balance < total:
                await interaction.edit_original_response(
                    content="",
                    embed=make_embed(
                        pick(lang, "💳 Số dư đã thay đổi", "💳 Balance Changed"),
                        pick(
                            lang,
                            "Số dư hiện tại của bạn không còn đủ cho đơn hàng này.",
                            "Your current balance is no longer enough for this order."
                        ),
                        UI_RED
                    ),
                    view=None
                )
                return

            purchased = stock[:self.quantity]
            remaining = stock[self.quantity:]

            order_id = generate_order_id()
            orders = load_orders()
            while order_id in orders:
                order_id = generate_order_id()

            # Single timestamp shared by the saved order and the warranty expiry,
            # so the date shown to the customer matches /checkwarranty exactly.
            order_created_at = datetime.now()
            warranty_days = product_warranty_days(self.product)
            # 0 days = sold without warranty: there is no expiry date to show.
            warranty_until = (
                order_created_at + timedelta(days=warranty_days)
                if warranty_days > 0 else None
            )
            warranty_until_text = (
                warranty_until.strftime("%d/%m/%Y %H:%M")
                if warranty_until else None
            )

            with data_transaction():
                users[str(interaction.user.id)]["balance"] = balance - total

                save_product_stock(self.product, remaining)
                save_json(USERS_FILE, users)

                if self.product.get("tiered"):
                    sales = load_sales()
                    sales["total_sold"] = int(sales.get("total_sold", 0)) + self.quantity
                    save_json(SALES_FILE, sales)
                else:
                    increment_product_sold(self.product["id"], self.quantity)

                save_order(
                    order_id,
                    {
                        "user_id": interaction.user.id,
                        "username": interaction.user.name,
                        "product": self.product["name"],
                        "product_id": self.product["id"],
                        "warranty_days": warranty_days,
                        "quantity": self.quantity,
                        "unit_price": self.price,
                        "total": int(total),
                        "status": "completed",
                        "delivery_status": "pending",
                        "created_at": order_created_at.isoformat(),
                        "accounts": purchased
                    }
                )

                log_activity(
                    interaction.user.id,
                    interaction.user.name,
                    "order_completed",
                    {
                        "order_id": order_id,
                        "quantity": self.quantity,
                        "unit_price": self.price,
                        "total": int(total),
                        "product": self.product["name"]
                    },
                    "button"
                )

        self.completed = True
        self.stop()

        warranty_days_text = pick(
            lang,
            f"{warranty_days} ngày",
            f"{warranty_days} days"
        )

        if warranty_until:
            warranty_field_name = pick(lang, "🛡️ Bảo hành đến", "🛡️ Warranty until")
            warranty_field_value = f"`{warranty_until_text}` ({warranty_days_text})"
        else:
            warranty_field_name = pick(lang, "🛡️ Bảo hành", "🛡️ Warranty")
            warranty_field_value = warranty_days_label(0, lang)

        success_embed = make_embed(
            pick(lang, "✅ Mua hàng thành công", "✅ Purchase Completed"),
            pick(
                lang,
                "Đơn hàng của bạn đã hoàn tất.\n\n"
                "📦 Bot đang gửi account vào DM. Nếu chưa nhận được, dùng `/redeliver` hoặc `/downloadorder`.",
                "Your order has been completed successfully.\n\n"
                "📦 Delivery is being sent to your DMs. Use `/redeliver` or `/downloadorder` if needed."
            ),
            UI_GREEN
        )

        success_embed.add_field(
            name=pick(lang, "🧾 Mã đơn hàng", "🧾 Order ID"),
            value=f"`{order_id}`",
            inline=False
        )

        success_embed.add_field(
            name=pick(lang, "🛍️ Sản phẩm", "🛍️ Product"),
            value=f"**{self.product['name']}**",
            inline=False
        )

        success_embed.add_field(
            name=pick(lang, "📦 Số lượng", "📦 Quantity"),
            value=f"`{self.quantity:,}`",
            inline=True
        )

        success_embed.add_field(
            name=pick(lang, "💰 Đơn giá", "💰 Unit Price"),
            value=f"`{unit_money(self.price, lang)}`",
            inline=True
        )

        success_embed.add_field(
            name=pick(lang, "💵 Tổng thanh toán", "💵 Total Paid"),
            value=f"`{balance_money(total, lang)}`",
            inline=True
        )

        success_embed.add_field(
            name=pick(lang, "💳 Số dư còn lại", "💳 Remaining Balance"),
            value=f"`{balance_money(get_user_balance(interaction.user.id), lang)}`",
            inline=False
        )

        success_embed.add_field(
            name=warranty_field_name,
            value=warranty_field_value,
            inline=False
        )

        try:
            await interaction.edit_original_response(
                content="",
                embed=success_embed,
                view=None
            )

        except Exception:
            pass

        delivered, delivery_message = await deliver_order(order_id)
        if not delivered:
            await interaction.followup.send(delivery_message, ephemeral=True)

        await update_stock_message()

    @discord.ui.button(
        label="Cancel",
        emoji="✖️",
        style=discord.ButtonStyle.danger
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        lang = get_user_language(interaction.user.id)

        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                pick(
                    lang,
                    "❌ Đơn hàng này thuộc về người dùng khác.",
                    "❌ This order belongs to another user."
                ),
                ephemeral=True
            )
            return

        async with SHOP_TRANSACTION_LOCK:
            if self.completed:
                await interaction.response.send_message(
                    "Đơn hàng này đã được xử lý / Already processed.", ephemeral=True
                )
                return
            self.completed = True
            self.stop()
            await interaction.response.edit_message(
                content="",
                embed=make_embed(
                    pick(lang, "🛑 Đã hủy đơn hàng", "🛑 Order Cancelled"),
                    pick(
                        lang,
                        "Số dư và kho hàng không bị thay đổi.",
                        "No balance or stock was changed."
                    ),
                    UI_RED
                ),
                view=None
            )


# =========================================================
# TOPUP MODAL
# =========================================================

class TopupModal(Modal):

    def __init__(self, language="vi"):
        lang = "en" if language == "en" else "vi"

        super().__init__(
            title=pick(
                lang,
                "💳 Nạp tiền Vietcombank",
                "💳 Vietcombank Top Up"
            )
        )

        self.amount = TextInput(
            label=pick(lang, "Số tiền (VND)", "Amount (VND)"),
            placeholder=pick(lang, "Ví dụ: 10000", "Example: 10000"),
            required=True,
            min_length=1,
            max_length=15
        )

        self.add_item(
            self.amount
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        lang = get_user_language(interaction.user.id)

        ensure_user(
            interaction.user
        )

        raw_amount = (
            self.amount.value
            .strip()
        )

        cleaned_amount = (
            raw_amount
            .replace(",", "")
            .replace(".", "")
            .replace(" ", "")
            .replace("_", "")
        )

        if not cleaned_amount.isdigit():

            embed = make_embed(
                pick(lang, "❌ Số tiền không hợp lệ", "❌ Invalid Amount"),
                pick(
                    lang,
                    "Vui lòng nhập số tiền VND hợp lệ.\n\n"
                    "Ví dụ: `2000`, `10,000`, `10.000`, `10000`",
                    "Please enter a valid VND amount.\n\n"
                    "Examples: `2000`, `10,000`, `10.000`, `10000`"
                ),
                UI_RED
            )

            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )
            return

        amount = int(
            cleaned_amount
        )

        if amount < 2000:
            await interaction.response.send_message(
                embed=make_embed(
                    pick(lang, "❌ Số tiền nạp tối thiểu", "❌ Minimum Top Up"),
                    pick(
                        lang,
                        "Số tiền nạp tối thiểu là **2,000 VND**.",
                        f"The minimum top up amount is **2,000 VND** (≈ {usd_text(2000)}).\n"
                        "Bank transfer is in VND. Foreign buyers can top up in USD with 🪙 **Crypto**."
                    ),
                    UI_RED
                ),
                ephemeral=True
            )
            return

        if (
            not VCB_ACCOUNT_NUMBER
            or
            VCB_ACCOUNT_NUMBER
            == "YOUR_VCB_ACCOUNT_NUMBER"
        ):

            await interaction.response.send_message(
                embed=make_embed(
                    pick(
                        lang,
                        "⚠️ Chưa cấu hình thanh toán",
                        "⚠️ Payment Setup Required"
                    ),
                    pick(
                        lang,
                        "Shop chưa cấu hình tài khoản Vietcombank.",
                        "The shop has not configured its Vietcombank account yet."
                    ),
                    UI_RED
                ),
                ephemeral=True
            )

            print(
                "[TOPUP] VCB account number "
                "has not been configured."
            )

            return

        content = (
            f"LC{interaction.user.id}"
        )

        qr_url = (
            "https://img.vietqr.io/image/"
            f"{VCB_BIN}-"
            f"{VCB_ACCOUNT_NUMBER}-compact2.png"
            f"?amount={amount}"
            f"&addInfo={quote(content)}"
            f"&accountName="
            f"{quote(VCB_ACCOUNT_NAME)}"
        )

        embed = make_embed(
            pick(lang, "💳 Vietcombank • Nạp tiền", "💳 Vietcombank • Top Up"),
            pick(
                lang,
                "Quét mã QR hoặc chuyển khoản thủ công "
                "theo thông tin bên dưới.\n\n"
                "⚡ Số dư sẽ được cộng tự động "
                "sau khi hệ thống nhận được thanh toán.",
                "Scan the QR code or transfer manually "
                "using the details below.\n\n"
                "⚡ Your balance is credited automatically "
                "after the payment is detected."
            ),
            UI_GREEN
        )

        embed.add_field(
            name=pick(lang, "🏦 Ngân hàng", "🏦 Bank"),
            value=f"`{VCB_BANK_NAME}`",
            inline=True
        )

        embed.add_field(
            name=pick(lang, "💳 Số tài khoản", "💳 Account Number"),
            value=f"`{VCB_ACCOUNT_NUMBER}`",
            inline=True
        )

        embed.add_field(
            name=pick(lang, "👤 Tên tài khoản", "👤 Account Name"),
            value=f"`{VCB_ACCOUNT_NAME}`",
            inline=True
        )

        embed.add_field(
            name=pick(lang, "💰 Số tiền", "💰 Amount"),
            value=(
                f"**{amount:,} VND**"
                + (f"\n≈ {usd_text(amount)}" if lang == "en" else "")
            ),
            inline=True
        )

        embed.add_field(
            name=pick(lang, "📝 Nội dung chuyển khoản", "📝 Transfer Content"),
            value=f"`{content}`",
            inline=True
        )

        embed.add_field(
            name=pick(lang, "⚡ Xử lý", "⚡ Processing"),
            value=pick(lang, "Tự động", "Automatic"),
            inline=True
        )

        embed.set_image(
            url=qr_url
        )

        view = View(timeout=120)

        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )


# =========================================================
# CRYPTO MODAL
# =========================================================

class CryptoModal(Modal):

    def __init__(self, language="vi"):
        lang = "en" if language == "en" else "vi"

        super().__init__(
            title=pick(lang, "🪙 Nạp tiền Crypto", "🪙 Crypto Top Up")
        )

        self.amount = TextInput(
            label=pick(lang, "Số tiền (USD)", "Amount (USD)"),
            placeholder=pick(lang, "Ví dụ: 5", "Example: 5"),
            required=True,
            min_length=1,
            max_length=20
        )

        self.add_item(
            self.amount
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        lang = get_user_language(interaction.user.id)

        try:
            amount_usd = Decimal(
                self.amount.value.strip()
            )

        except InvalidOperation:
            await interaction.response.send_message(
                embed=make_embed(
                    pick(lang, "❌ Số tiền không hợp lệ", "❌ Invalid Amount"),
                    pick(
                        lang,
                        "Vui lòng nhập số tiền USD hợp lệ.",
                        "Please enter a valid USD amount."
                    ),
                    UI_RED
                ),
                ephemeral=True
            )
            return

        if not amount_usd.is_finite() or amount_usd <= 0 or amount_usd > Decimal("1000000000"):
            await interaction.response.send_message(
                embed=make_embed(
                    pick(lang, "❌ Số tiền không hợp lệ", "❌ Invalid Amount"),
                    pick(
                        lang,
                        "Số tiền phải hữu hạn, lớn hơn 0 và không quá 1 tỷ USD.",
                        "Amount must be finite, positive and at most 1 billion USD."
                    ),
                    UI_RED
                ),
                ephemeral=True
            )
            return

        request_id = (
            f"{interaction.user.id}-"
            + secrets.token_hex(12)
        )

        await interaction.response.defer(ephemeral=True, thinking=True)

        data, error = await asyncio.to_thread(
            create_payfjord_invoice,
            interaction.user.name,
            amount_usd,
            request_id
        )

        if error:
            await interaction.followup.send(
                embed=make_embed(
                    pick(lang, "❌ Lỗi tạo hóa đơn", "❌ Invoice Error"),
                    (
                        pick(
                            lang,
                            "Không thể tạo hóa đơn thanh toán.",
                            "Unable to create the payment invoice."
                        )
                        + "\n\n"
                        f"```{error}```"
                    ),
                    UI_RED
                ),
                ephemeral=True
            )
            return

        result = data

        if isinstance(
            result,
            dict
        ):
            nested = result.get(
                "data"
            )

            if isinstance(
                nested,
                dict
            ):
                result = nested

        if not isinstance(
            result,
            dict
        ):
            await interaction.followup.send(
                embed=make_embed(
                    pick(lang, "❌ Lỗi thanh toán", "❌ Payment Error"),
                    pick(
                        lang,
                        "AutoCrypto trả về dữ liệu thanh toán không hợp lệ.",
                        "AutoCrypto returned invalid payment data."
                    ),
                    UI_RED
                ),
                ephemeral=True
            )
            return

        trans_id = result.get(
            "trans_id"
        )

        payment_url = result.get(
            "url_payment"
        )

        if not trans_id:
            await interaction.followup.send(
                embed=make_embed(
                    pick(lang, "❌ Lỗi thanh toán", "❌ Payment Error"),
                    pick(
                        lang,
                        "Nhà cung cấp thanh toán không trả về mã giao dịch.",
                        "No transaction ID was returned by the payment provider."
                    ),
                    UI_RED
                ),
                ephemeral=True
            )
            return

        order_id = (
            f"LC-{request_id}"
        )

        crypto_orders[order_id] = {
            "user_id": interaction.user.id,
            "username": interaction.user.name,
            "amount_usd": str(amount_usd),
            "request_id": request_id,
            "trans_id": str(
                trans_id
            ),
            "status": "waiting",
            "credited": False,
            "created_at": (
                datetime.now().isoformat()
            )
        }

        save_json(
            CRYPTO_ORDERS_FILE,
            crypto_orders
        )

        embed = make_embed(
            pick(
                lang,
                "🪙 Crypto • Đã tạo thanh toán",
                "🪙 Crypto • Payment Created"
            ),
            pick(
                lang,
                "Hoàn tất thanh toán bằng nút bên dưới.\n\n"
                "Sau khi thanh toán được xác nhận, số dư "
                "sẽ được cộng tự động.",
                "Complete the payment using the button below.\n\n"
                "Once the payment is confirmed, your balance "
                "will be credited automatically."
            ),
            UI_PURPLE
        )

        embed.add_field(
            name=pick(lang, "💵 Số tiền", "💵 Amount"),
            value=f"**${amount_usd} USD**",
            inline=True
        )

        embed.add_field(
            name=pick(lang, "🧾 Đơn hàng", "🧾 Order"),
            value=f"`{order_id}`",
            inline=True
        )

        embed.add_field(
            name=pick(lang, "🔄 Trạng thái", "🔄 Status"),
            value=pick(lang, "🟡 **Đang chờ**", "🟡 **Waiting**"),
            inline=True
        )

        embed.add_field(
            name=pick(lang, "Mã giao dịch", "Transaction ID"),
            value=f"`{trans_id}`",
            inline=False
        )

        view = View(timeout=300)

        if payment_url:
            view.add_item(
                Button(
                    label=pick(lang, "Mở thanh toán", "Open Payment"),
                    emoji="💳",
                    style=discord.ButtonStyle.link,
                    url=str(payment_url)
                )
            )

        await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True
        )


# =========================================================
# DELIVERY OUTBOX AND RECONCILIATION
# =========================================================

DELIVERY_LOCKS = {}


def update_delivery_order(order_id, status):
    order = load_orders().get(order_id)
    if order:
        order['delivery_status'] = status
        save_order(order_id, order)


async def deliver_order(order_id, manual=False):
    lock = DELIVERY_LOCKS.setdefault(order_id, asyncio.Lock())
    async with lock:
        order = load_orders().get(order_id)
        if not order or not order.get('accounts'):
            return False, 'Không tìm thấy nội dung đơn hàng / Order contents unavailable.'
        row = STORE.delivery(order_id)
        now = time.time()
        if row and row['next_attempt_at'] > now:
            return row['status'] == 'sent', 'Vui lòng chờ ít nhất 60 giây trước khi gửi lại / Please wait before retrying.'
        if row and row['status'] == 'sent' and not manual:
            return True, 'Đơn đã giao / Already delivered.'
        if row and row['attempts'] >= 5 and not manual:
            return False, 'Đã dừng tự gửi sau 5 lần lỗi. Dùng /redeliver hoặc /downloadorder.'
        with data_transaction():
            STORE.enqueue_delivery(order_id, order['user_id'])
            STORE.db.execute("UPDATE deliveries SET status='sending',attempts=attempts+1,next_attempt_at=? WHERE order_id=?",
                             (now + 300, order_id))
        try:
            recipient = await bot.fetch_user(int(order['user_id']))
            lang = get_user_language(order['user_id'])
            embed = make_embed(pick(lang, '📦 Giao hàng account', '📦 Your Account Delivery'),
                f"{order_id}\n{discord.utils.escape_markdown(str(order.get('product', '')))}\n{warranty_status_text(order, lang)}", UI_GREEN)
            content = '\n'.join(order['accounts']).encode('utf-8')
            attachment = discord.File(io.BytesIO(content), filename=f'{order_id}.txt')
            files = [attachment]
            try:
                days = get_order_warranty_days(order)
                until = datetime.fromisoformat(order['created_at']) + timedelta(days=days) if days else None
                image = await asyncio.to_thread(build_purchase_success_image, order_id,
                    order.get('quantity', len(order['accounts'])), order.get('total', 0), until, lang)
                if image is not None:
                    files.append(discord.File(image, filename='purchase_successful.png'))
                    embed.set_image(url='attachment://purchase_successful.png')
            except Exception:
                pass  # Rendering must never prevent account delivery.
            message = await recipient.send(embed=embed, files=files, allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            # Exceptions may contain URLs/credentials; persist and log only the class.
            error = type(exc).__name__
            row = STORE.delivery(order_id)
            with data_transaction():
                STORE.db.execute("UPDATE deliveries SET status='failed',last_error=?,next_attempt_at=? WHERE order_id=?",
                                 (error, time.time() + min(3600, 60 * 2 ** min(row['attempts'], 5)), order_id))
                update_delivery_order(order_id, 'failed')
            if row['attempts'] in (1, 5):
                await notify_admins(f'⚠️ Đơn {order_id} chưa giao được ({error}). Khách có thể dùng /downloadorder hoặc /redeliver.')
            return False, 'Chưa gửi được DM. Bật nhận DM rồi dùng /redeliver, hoặc dùng /downloadorder để tải riêng tư.'
        with data_transaction():
            STORE.db.execute("UPDATE deliveries SET status='sent',last_error=NULL,message_id=?,next_attempt_at=? WHERE order_id=?",
                             (str(getattr(message, 'id', '')), time.time() + 60, order_id))
            update_delivery_order(order_id, 'sent')
        return True, '✅ Đã gửi đơn vào DM / Order sent to your DMs.'


@tasks.loop(minutes=1)
async def delivery_retry_loop():
    if STORE.payments_paused():
        return
    for order_id in STORE.due_deliveries(time.time()):
        try:
            await deliver_order(order_id)
        except Exception as exc:
            logging.error('[DELIVERY] Retry failed (%s)', type(exc).__name__)


@tree.command(name='redeliver', description='Send an existing order to its buyer again; no extra charge')
@app_commands.describe(order_id='Order ID')
async def redeliver(interaction: discord.Interaction, order_id: str):
    order_id = order_id.strip().upper()
    order = load_orders().get(order_id)
    if not order or (str(order.get('user_id')) != str(interaction.user.id) and not can_manage(interaction)):
        await interaction.response.send_message('❌ Không tìm thấy đơn của bạn hoặc bạn không có quyền.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    _, message = await deliver_order(order_id, manual=True)
    await interaction.followup.send(message, ephemeral=True)


@tree.command(name='downloadorder', description='Download your purchased accounts privately when DMs are unavailable')
@app_commands.describe(order_id='Your order ID')
async def downloadorder(interaction: discord.Interaction, order_id: str):
    order_id = order_id.strip().upper()
    order = load_orders().get(order_id)
    # Even administrators must not download another customer's credentials here.
    if not order or str(order.get('user_id')) != str(interaction.user.id) or not order.get('accounts'):
        await interaction.response.send_message('❌ Không tìm thấy nội dung đơn hàng của bạn.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    lock = DELIVERY_LOCKS.setdefault(order_id, asyncio.Lock())
    async with lock:
        attachment = discord.File(io.BytesIO('\n'.join(order['accounts']).encode('utf-8')), filename=f'{order_id}.txt')
        await interaction.followup.send('📦 Nội dung đơn hàng của bạn / Your order:', file=attachment, ephemeral=True)
        with data_transaction():
            STORE.enqueue_delivery(order_id, order['user_id'])
            STORE.db.execute("UPDATE deliveries SET status='sent',last_error=NULL,next_attempt_at=? WHERE order_id=?", (time.time() + 60, order_id))
            update_delivery_order(order_id, 'sent')


@tree.command(name='pendingtopups', description='Admin: list bank transfers requiring reconciliation')
@app_commands.guild_only()
@admin_check()
async def pendingtopups(interaction: discord.Interaction):
    rows = STORE.pending_bank_payments(10)
    embed = make_embed('💳 Giao dịch cần đối soát', '10 giao dịch cũ nhất. Dùng /reconciletopup hoặc /ignoretopup.', UI_GOLD)
    if not rows:
        embed.description = 'Không có giao dịch đang chờ đối soát.'
    for row in rows:
        embed.add_field(name=str(row['transaction_id'])[:256],
            value=f"{row['amount_vnd']:,} VND • {row['reason']}\n{discord.utils.escape_markdown(row['description'][:400])}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name='reconciletopup', description='Admin: credit one observed pending bank transfer to the verified buyer')
@app_commands.guild_only()
@admin_check()
@app_commands.describe(transaction_id='Exact ID from /pendingtopups', user='Verified recipient of this transfer', reason='Reason and evidence used for reconciliation')
async def reconciletopup(interaction: discord.Interaction, transaction_id: str, user: discord.Member, reason: str):
    transaction_id = transaction_id.strip()
    if not reason.strip() or len(reason) > 200:
        await interaction.response.send_message('❌ Nhập lý do đối soát từ 1 đến 200 ký tự.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        async with SHOP_TRANSACTION_LOCK:
            row = STORE.bank_payment(transaction_id)
            if not row or row['status'] != 'pending' or is_transaction_processed(transaction_id):
                raise ValueError('not_pending')
            ensure_user(user)
            new_balance = credit_bank_payment(transaction_id, user.id, interaction.user.id, reason.strip())
    except ValueError:
        await interaction.followup.send('❌ Giao dịch không tồn tại, đã xử lý hoặc dữ liệu cần kiểm tra. Không cộng tiền.', ephemeral=True)
        return
    await interaction.followup.send(f"✅ Đã cộng {row['amount_vnd']:,} VND cho ID {user.id}. Số dư: {new_balance:,} VND.", ephemeral=True)
    try:
        await user.send(f"💰 Đối soát nạp tiền: +{row['amount_vnd']:,} VND. Số dư: {new_balance:,} VND.")
    except Exception:
        pass


@tree.command(name='ignoretopup', description='Admin: dismiss an observed transfer without crediting a balance')
@app_commands.guild_only()
@admin_check()
async def ignoretopup(interaction: discord.Interaction, transaction_id: str, reason: str):
    transaction_id = transaction_id.strip()
    if not reason.strip() or len(reason) > 200:
        await interaction.response.send_message('❌ Nhập lý do từ 1 đến 200 ký tự.', ephemeral=True)
        return
    try:
        async with SHOP_TRANSACTION_LOCK:
            with data_transaction():
                row = STORE.bank_payment(transaction_id)
                if not row or row['status'] != 'pending' or not mark_transaction_processed(transaction_id):
                    raise ValueError('not_pending')
                STORE.settle_bank_payment(transaction_id, 'ignored', None, interaction.user.id)
                log_activity(interaction.user.id, interaction.user.name, 'bank_transfer_ignored',
                             {'transaction_id': transaction_id, 'reason': reason.strip()}, 'command')
    except ValueError:
        await interaction.response.send_message('❌ Không tìm thấy giao dịch đang chờ. Không có thay đổi.', ephemeral=True)
        return
    await interaction.response.send_message('✅ Đã bỏ qua giao dịch; không cộng tiền.', ephemeral=True)


@tree.command(name='backup', description='Admin: create and verify a local SQLite backup')
@app_commands.guild_only()
@admin_check()
async def backup_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        target = await asyncio.to_thread(rotating_backup, BASE_DIR, POLICY.config['backup_keep'])
        save_json(os.path.join(DATA_DIR, 'backup_status.json'), {'completed_at': time.time(), 'file': target.name})
        await service_result('backup', True)
    except Exception as exc:
        await service_result('backup', False, type(exc).__name__)
        await interaction.followup.send('❌ Sao lưu thất bại. Kiểm tra dung lượng/quyền ghi trên VPS.', ephemeral=True)
        return
    await interaction.followup.send(f'✅ Đã sao lưu và kiểm tra toàn vẹn: backups/sqlite/{target.name}', ephemeral=True)


@tree.command(name='botstatus', description='Admin: inspect database, backup, bank and delivery status')
@app_commands.guild_only()
@admin_check()
async def botstatus(interaction: discord.Interaction):
    health = load_json(os.path.join(DATA_DIR, 'service_health.json'), {})
    backup = load_json(os.path.join(DATA_DIR, 'backup_status.json'), {})
    pending = STORE.db.execute("SELECT count(*) FROM bank_payments WHERE status='pending'").fetchone()[0]
    undelivered = STORE.db.execute("SELECT count(*) FROM deliveries WHERE status!='sent'").fetchone()[0]
    integrity = STORE.db.execute('PRAGMA quick_check').fetchone()[0]
    message = (f"SQLite: {integrity}\nNgân hàng: {'OK' if health.get('bank', {}).get('ok') else 'chưa xác nhận / lỗi'}\n"
               f"Nạp cần đối soát: {pending}\nĐơn chưa giao: {undelivered}\n"
               f"Backup gần nhất: {backup.get('file', 'chưa có')}\nServer được phép: {len(POLICY.config['allowed_guild_ids'])}\n"
               f"Tạm dừng nạp sau restore: {STORE.payments_paused()}")
    for name in ('bank-processing', 'crypto', 'backup'):
        record = health.get(name)
        if record:
            message += f"\n{name}: {'OK' if record.get('ok') else 'LỖI'}"
    await interaction.response.send_message(message, ephemeral=True)


# =========================================================
# /HELP
# =========================================================

def build_help_embed(language="vi"):
    lang = "en" if language == "en" else "vi"

    if lang == "en":
        embed = make_embed(
            "🍋 Lemon Service • Help",
            "Here are all available commands and their functions.",
            UI_BLUE
        )

        embed.add_field(
            name="🛒 Customer Commands",
            value=(
                "`/help` — Show this help panel and all available commands.\n"
                "`/profile` — View your username, User ID and current balance.\n"
                "`/stock` — Check the current account stock and pricing.\n"
                "`/buy` — Open the purchase form to buy accounts.\n"
                "`/topup` — Open the Vietcombank balance top-up form.\n"
                "`/topupcrypto` — Open the crypto top-up form.\n"
                "`/history` — View your top-up and order history.\n"
                "`/order <order_id>` — View details of your order.\n"
                "`/checkwarranty <order_id>` — Check the warranty status of your own order.\n"
                "`/redeliver <order_id>` — Retry delivery without another charge.\n"
                "`/downloadorder <order_id>` — Download your accounts privately."
            ),
            inline=False
        )

        embed.add_field(
            name="🔐 Administrator Commands",
            value=(
                "`/addstock <product> <file>` — Upload UTF-8 TXT stock (admin only).\n"
                "`/removestock <product> <quantity>` — Remove accounts from the end of stock.\n"
                "`/addbalance <user> <amount>` — Add VND balance to a user.\n"
                "`/removebalance <user> <amount>` — Remove VND balance from a user.\n"
                "`/warranty <order_id>` — Check warranty details for an order as an administrator.\n"
                "`/checkusername <username>` — View the recorded activity history of a username.\n"
                "`/setstockmessage [channel_id]` — Create or refresh the public shop panel in a channel (authorized servers only).\n`/removestockmessage <channel_id>` — Stop and delete the shop panel of a channel."
            ),
            inline=False
        )

        embed.add_field(name="🛠️ Operations (authorized admins)",
            value="`/pendingtopups` • `/reconciletopup` • `/ignoretopup`\n`/backup` • `/botstatus`", inline=False)
        embed.add_field(
            name="💡 Quick Guide",
            value=(
                "1. Use `/profile` to check your balance.\n"
                "2. Use `/topup` or `/topupcrypto` to add balance.\n"
                "3. Use `/stock` to check availability.\n"
                "4. Use `/buy` to purchase accounts.\n"
                "5. Use `/history` or `/checkwarranty` to manage your orders."
            ),
            inline=False
        )

        embed.set_footer(
            text="🍋 Lemon Service • Use /help anytime to view this guide"
        )

    else:
        embed = make_embed(
            "🍋 Lemon Service • Trợ Giúp",
            "Danh sách tất cả lệnh hiện có và tác dụng của từng lệnh.",
            UI_BLUE
        )

        embed.add_field(
            name="🛒 Lệnh dành cho khách hàng",
            value=(
                "`/help` — Hiển thị bảng hướng dẫn và toàn bộ lệnh.\n"
                "`/profile` — Xem tên người dùng, User ID và số dư hiện tại.\n"
                "`/stock` — Kiểm tra kho account và bảng giá hiện tại.\n"
                "`/buy` — Mở form mua account.\n"
                "`/topup` — Mở form nạp tiền qua Vietcombank.\n"
                "`/topupcrypto` — Mở form nạp tiền bằng Crypto.\n"
                "`/history` — Xem lịch sử nạp tiền và đơn hàng.\n"
                "`/order <order_id>` — Xem thông tin chi tiết đơn hàng.\n"
                "`/checkwarranty <order_id>` — Kiểm tra bảo hành đơn hàng của chính bạn."
            ),
            inline=False
        )

        embed.add_field(
            name="🔐 Lệnh dành cho Administrator",
            value=(
                "`/addstock <product> <file>` — Chọn sản phẩm và nhập kho từ TXT.\n"
                "`/removestock <product> <quantity>` — Xóa account từ cuối kho.\n"
                "`/addbalance <user> <amount>` — Cộng số dư VND cho người dùng.\n"
                "`/removebalance <user> <amount>` — Trừ số dư VND của người dùng.\n"
                "`/warranty <order_id>` — Kiểm tra chi tiết bảo hành đơn hàng với quyền Admin.\n"
                "`/checkusername <username>` — Xem lịch sử hoạt động đã được ghi nhận của username.\n"
                "`/setstockmessage [channel_id]` — Tạo hoặc cập nhật panel cửa hàng công khai trong một kênh (server đã cấp quyền).\n`/removestockmessage <channel_id>` — Gỡ và xóa panel cửa hàng của một kênh."
            ),
            inline=False
        )

        embed.add_field(name="🛠️ Vận hành (quản trị được cấp quyền)",
            value="`/pendingtopups` • `/reconciletopup` • `/ignoretopup`\n`/backup` • `/botstatus`\n`/redeliver` • `/downloadorder`: giao lại hoặc tải đơn đã mua.", inline=False)
        embed.add_field(
            name="💡 Hướng dẫn nhanh",
            value=(
                "1. Dùng `/profile` để kiểm tra số dư.\n"
                "2. Dùng `/topup` hoặc `/topupcrypto` để nạp tiền.\n"
                "3. Dùng `/stock` để kiểm tra số lượng account.\n"
                "4. Dùng `/buy` để mua account.\n"
                "5. Dùng `/history` hoặc `/checkwarranty` để quản lý đơn hàng."
            ),
            inline=False
        )

        embed.set_footer(
            text="🍋 Lemon Service • Dùng /help bất cứ lúc nào để xem hướng dẫn"
        )

    return embed


class HelpPanelView(View):
    """Language switcher for the /help panel."""

    def __init__(self, language="vi"):
        super().__init__(timeout=None)
        self.language = "en" if language == "en" else "vi"

        language_button = Button(
            label="🇬🇧 English" if self.language == "vi" else "🇻🇳 Tiếng Việt",
            style=discord.ButtonStyle.primary,
            custom_id="lemon:help:language"
        )
        language_button.callback = self.language_button_callback
        self.add_item(language_button)

    async def language_button_callback(self, interaction: discord.Interaction):
        if not await ensure_allowed_interaction(interaction):
            return False
        ensure_user(interaction.user)

        current = get_user_language(interaction.user.id)
        new_language = "en" if current != "en" else "vi"

        set_user_language(interaction.user.id, new_language)
        log_activity(
            interaction.user.id,
            interaction.user.name,
            "language_changed",
            {"from": current, "to": new_language},
            "help_button"
        )

        await interaction.response.edit_message(
            embed=build_help_embed(new_language),
            view=HelpPanelView(new_language)
        )


@tree.command(
    name="help",
    description="Show all commands and what they do"
)
async def help_command(
    interaction: discord.Interaction
):
    ensure_user(interaction.user)

    lang = get_user_language(interaction.user.id)

    await interaction.response.send_message(
        embed=build_help_embed(lang),
        view=HelpPanelView(lang),
        ephemeral=True
    )


# =========================================================
# BALANCE CHANGE DM
# =========================================================

MAX_BALANCE_REASON_LENGTH = 200


def clean_reason(reason):
    """Collapse whitespace; empty -> None."""
    if not reason:
        return None
    reason = " ".join(str(reason).split())
    return reason or None


BALANCE_DM_FILE = os.path.join(DATA_DIR, "balance_dm.json")
BALANCE_DM_MAX_ENTRIES = 1000
BALANCE_DM_MAX_AGE_DAYS = 90


def _usd_value(vnd_amount):
    rate = Decimal(str(USD_TO_VND))
    if rate <= 0:
        rate = Decimal("25000")
    return Decimal(str(vnd_amount)) / rate


def usd_text(vnd_amount):
    """VND -> '$12.50'. Amounts under $1 keep up to 3 decimals ('$0.084')."""
    try:
        usd = _usd_value(vnd_amount)
        if abs(usd) < 1:
            text_value = f"{usd:,.3f}"
            if text_value.endswith("0"):
                text_value = text_value[:-1]
            return f"${text_value}"
        return f"${usd:,.2f}"
    except Exception:
        return "$0.00"


def usd_unit_text(vnd_amount):
    """Per-account prices: always 3 decimals under $1 so tiers stay distinct."""
    try:
        usd = _usd_value(vnd_amount)
        if abs(usd) < 1:
            return f"${usd:,.3f}"
        return f"${usd:,.2f}"
    except Exception:
        return "$0.000"


def balance_money(vnd_amount, lang):
    """Vietnamese shows VND; English shows USD ($)."""
    return usd_text(vnd_amount) if lang == "en" else format_vnd(vnd_amount)


def unit_money(vnd_amount, lang):
    """Per-account price: VND for Vietnamese, $ for English."""
    if lang == "en":
        return usd_unit_text(vnd_amount)
    try:
        return f"{int(vnd_amount):,} VND"
    except Exception:
        return f"{vnd_amount} VND"


def build_balance_dm_embed(kind, amount, old_balance, new_balance, reason, language):
    lang = "en" if language == "en" else "vi"
    adding = kind == "add"

    if adding:
        title = pick(lang, "💰 Số dư đã được cộng", "💰 Balance Added")
        description = pick(
            lang,
            "Quản trị viên vừa **cộng tiền** vào tài khoản của bạn.",
            "An administrator has **added funds** to your account."
        )
        amount_label = pick(lang, "➕ Số tiền cộng", "➕ Amount added")
        color = UI_GREEN
    else:
        title = pick(lang, "💸 Số dư đã bị trừ", "💸 Balance Deducted")
        description = pick(
            lang,
            "Quản trị viên vừa **trừ tiền** từ tài khoản của bạn.",
            "An administrator has **deducted funds** from your account."
        )
        amount_label = pick(lang, "➖ Số tiền trừ", "➖ Amount deducted")
        color = UI_GOLD

    embed = make_embed(
        title,
        f"{UI_DIVIDER}\n{description}\n{UI_DIVIDER}",
        color
    )
    embed.add_field(
        name=amount_label,
        value=f"**`{balance_money(amount, lang)}`**",
        inline=True
    )
    embed.add_field(
        name=pick(lang, "💳 Số dư trước", "💳 Previous balance"),
        value=f"`{balance_money(old_balance, lang)}`",
        inline=True
    )
    embed.add_field(
        name=pick(lang, "💳 Số dư mới", "💳 New balance"),
        value=f"**`{balance_money(new_balance, lang)}`**",
        inline=True
    )
    if reason:
        embed.add_field(
            name=pick(lang, "📝 Lý do", "📝 Reason"),
            value=str(reason)[:1024],
            inline=False
        )
    embed.add_field(
        name=pick(lang, "❓ Cần hỗ trợ?", "❓ Need help?"),
        value=pick(
            lang,
            "Nếu có thắc mắc về thay đổi này, hãy mở ticket để được hỗ trợ.",
            "If you have questions about this change, please open a ticket."
        ),
        inline=False
    )

    if lang == "en":
        embed.set_footer(
            text=f"{SHOP_FOOTER} • Rate: 1 USD = {int(USD_TO_VND):,} VND"
        )

    return embed


def load_balance_dm_payloads():
    data = load_json(BALANCE_DM_FILE, {})
    return data if isinstance(data, dict) else {}


def save_balance_dm_payload(message_id, payload):
    """Remember what a balance DM contained so its language button keeps
    working after a bot restart. Old entries are pruned."""
    data = load_balance_dm_payloads()
    data[str(message_id)] = payload

    cutoff = (datetime.now() - timedelta(days=BALANCE_DM_MAX_AGE_DAYS)).isoformat()
    data = {
        key: value for key, value in data.items()
        if isinstance(value, dict) and str(value.get("sent_at", "")) >= cutoff
    }

    if len(data) > BALANCE_DM_MAX_ENTRIES:
        newest = sorted(
            data.items(),
            key=lambda kv: str(kv[1].get("sent_at", "")),
            reverse=True
        )[:BALANCE_DM_MAX_ENTRIES]
        data = dict(newest)

    save_json(BALANCE_DM_FILE, data)


class BalanceDMView(View):
    """Persistent language toggle under a balance-change DM."""

    def __init__(self, language="vi"):
        super().__init__(timeout=None)
        self.language = "en" if language == "en" else "vi"

        # Showing Vietnamese -> offer English, and the other way round.
        if self.language == "vi":
            self.toggle_button.label = "English"
            self.toggle_button.emoji = "🇬🇧"
        else:
            self.toggle_button.label = "Tiếng Việt"
            self.toggle_button.emoji = "🇻🇳"

    @discord.ui.button(
        label="English",
        emoji="🇬🇧",
        style=discord.ButtonStyle.secondary,
        custom_id="lemon:baldm:toggle"
    )
    async def toggle_button(
        self,
        interaction: discord.Interaction,
        button: Button
    ):
        if not await ensure_allowed_interaction(interaction):
            return False
        message_id = str(interaction.message.id) if interaction.message else ""
        data = load_balance_dm_payloads()
        payload = data.get(message_id)

        if not isinstance(payload, dict):
            await interaction.response.send_message(
                "❌ Tin nhắn này đã quá cũ nên không thể đổi ngôn ngữ. / "
                "This message is too old to switch language.",
                ephemeral=True
            )
            return

        new_lang = "en" if payload.get("lang", "vi") == "vi" else "vi"

        try:
            embed = build_balance_dm_embed(
                payload["kind"],
                payload["amount"],
                payload["old_balance"],
                payload["new_balance"],
                payload.get("reason"),
                new_lang
            )
        except Exception as e:
            print(f"[BALANCE] Language switch error: {e}")
            await interaction.response.send_message(
                "❌ Không thể đổi ngôn ngữ. / Could not switch language.",
                ephemeral=True
            )
            return

        payload["lang"] = new_lang
        data[message_id] = payload
        save_json(BALANCE_DM_FILE, data)

        await interaction.response.edit_message(
            embed=embed,
            view=BalanceDMView(new_lang)
        )


async def notify_balance_change(
    user,
    kind,
    amount,
    old_balance,
    new_balance,
    reason=None
):
    """DM a customer when an admin adds or removes balance.

    The DM has a language button (Vietnamese <-> English). In English the
    amounts are shown in USD ($). kind: "add" or "remove". Returns True if the
    DM was delivered, False if the customer has DMs closed or Discord refused.
    Never raises.
    """

    lang = get_user_language(user.id)

    embed = build_balance_dm_embed(
        kind, amount, old_balance, new_balance, reason, lang
    )

    try:
        message = await asyncio.wait_for(
            user.send(embed=embed, view=BalanceDMView(lang)),
            timeout=8
        )
    except discord.Forbidden:
        print(f"[BALANCE] Cannot DM {user.id}: DMs are closed.")
        return False
    except asyncio.TimeoutError:
        print(f"[BALANCE] DM to {user.id} timed out.")
        return False
    except Exception as e:
        print(f"[BALANCE] DM error for {user.id}: {e}")
        return False

    # The DM is already delivered; remembering it is best-effort.
    try:
        save_balance_dm_payload(
            message.id,
            {
                "kind": kind,
                "amount": vnd(amount),
                "old_balance": vnd(old_balance),
                "new_balance": vnd(new_balance),
                "reason": reason,
                "lang": lang,
                "sent_at": datetime.now().isoformat()
            }
        )
    except Exception as e:
        print(f"[BALANCE] Could not store DM payload: {e}")

    return True


def dm_status_field(embed, dm_sent, lang):
    """Tell the admin whether the customer was notified."""
    embed.add_field(
        name=pick(lang, "📨 Thông báo DM", "📨 DM Notification"),
        value=(
            pick(lang, "✅ Đã gửi cho khách.", "✅ Sent to the customer.")
            if dm_sent else
            pick(
                lang,
                "⚠️ Không gửi được DM (khách đang tắt tin nhắn riêng).",
                "⚠️ Could not DM the customer (their DMs are closed)."
            )
        ),
        inline=False
    )


# =========================================================
# /ADDBALANCE
# =========================================================

@tree.command(
    name="addbalance",
    description="Admin: add balance to a user"
)
@admin_check()
@app_commands.describe(
    user="User who will receive the balance",
    amount="Amount in VND",
    reason="Optional reason, shown to the customer in the DM"
)
async def addbalance(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
    reason: str = None
):

    if amount <= 0:
        await interaction.response.send_message(
            "❌ Số tiền phải lớn hơn **0 VND**.",
            ephemeral=True
        )
        return

    reason = clean_reason(reason)

    if reason and len(reason) > MAX_BALANCE_REASON_LENGTH:
        await interaction.response.send_message(
            f"❌ Lý do tối đa **{MAX_BALANCE_REASON_LENGTH}** ký tự "
            f"(hiện tại {len(reason)}).",
            ephemeral=True
        )
        return

    async with SHOP_TRANSACTION_LOCK:

        target = ensure_user(user)
        with data_transaction():
            old_balance = Decimal(str(get_user_balance(user.id)))
            new_balance = old_balance + Decimal(amount)

            target["balance"] = vnd(new_balance)
            save_json(
                USERS_FILE,
                users
            )

            log_activity(
                interaction.user.id,
                interaction.user.name,
                "admin_add_balance",
                {
                    "target_user_id": user.id,
                    "target_username": user.name,
                    "amount": amount,
                    "balance_before": vnd(old_balance),
                    "balance_after": vnd(new_balance),
                    "reason": reason
                },
                "command"
            )

    embed = make_embed(
        "💰 Balance Added",
        (
            f"Đã cộng **{amount:,} VND** cho {user.mention}.\n\n"
            f"💳 Balance trước: **{format_vnd(old_balance)}**\n"
            f"💳 Balance sau: **{format_vnd(new_balance)}**"
        ),
        UI_GREEN
    )

    if reason:
        embed.add_field(
            name="📝 Lý do / Reason",
            value=reason[:1024],
            inline=False
        )

    # The DM can take a moment: acknowledge the command first so Discord's
    # 3-second interaction window never expires.
    await interaction.response.defer(ephemeral=True)

    # Notify the customer by DM (outside the lock; never blocks a purchase).
    dm_sent = await notify_balance_change(
        user, "add", amount, old_balance, new_balance, reason
    )
    dm_status_field(
        embed, dm_sent, get_user_language(interaction.user.id)
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /SETPRICE
# =========================================================

def find_product_by_query(query):
    query = " ".join(str(query or "").split())
    for item in get_all_products():
        if item["id"] == query or item["name"].casefold() == query.casefold():
            return item
    return None


def gap_warning_text(tiers, lang="vi"):
    """Text listing quantity ranges that have no price, or None."""
    gaps = tier_gaps(tiers)
    if not gaps:
        return None

    listed = ", ".join(format_range(low, high, lang) for low, high in gaps)
    return pick(
        lang,
        f"Các số lượng sau chưa có giá nên khách **không mua được**: {listed}",
        f"These quantities have no price, so customers **cannot buy** them: {listed}"
    )[:1024]


@tree.command(
    name="setprice",
    description="Admin: set a product's price per tier range and/or its warranty days"
)
@app_commands.default_permissions(administrator=True)
@admin_check()
@app_commands.describe(
    product="Product to change",
    price="New price per account in VND",
    tier="Tier range, e.g. 1-5, 7-11 or 1001+ (a new range creates a new tier)",
    warranty_days="New warranty length in days (0 = no warranty)"
)
async def setprice(
    interaction: discord.Interaction,
    product: str,
    price: int = None,
    tier: str = None,
    warranty_days: int = None
):

    lang = get_user_language(interaction.user.id)

    async def fail(vi, en):
        await interaction.response.send_message(
            pick(lang, vi, en),
            ephemeral=True
        )

    if price is None and warranty_days is None:
        await fail(
            "❌ Hãy nhập **price** và/hoặc **warranty_days** để thay đổi.",
            "❌ Enter **price** and/or **warranty_days** to change."
        )
        return

    if price is not None and not 1 <= price <= MAX_PRICE_PER_ACCOUNT:
        await fail(
            f"❌ Giá phải từ **1** đến **{MAX_PRICE_PER_ACCOUNT:,} VND** / account.",
            f"❌ Price must be between **1** and **{MAX_PRICE_PER_ACCOUNT:,} VND** per account."
        )
        return

    if warranty_days is not None and not 0 <= warranty_days <= MAX_WARRANTY_DAYS:
        await fail(
            f"❌ Thời gian bảo hành phải từ **0** đến **{MAX_WARRANTY_DAYS}** ngày (0 = không bảo hành).",
            f"❌ Warranty must be between **0** and **{MAX_WARRANTY_DAYS}** days (0 = no warranty)."
        )
        return

    if tier is not None and price is None:
        await fail(
            "❌ **tier** chỉ dùng cùng với **price**.",
            "❌ **tier** can only be used together with **price**."
        )
        return

    target = find_product_by_query(product)

    if target is None:
        await fail(
            "❌ Không tìm thấy sản phẩm. Hãy chọn từ danh sách gợi ý.",
            "❌ Product not found. Please pick one from the suggestions."
        )
        return

    min_qty = None
    max_qty = None
    tier_kind = None

    if price is not None:
        tiers = product_tiers(target)
        starts = {t["min_qty"] for t in tiers}

        if tier is not None:
            parsed = parse_tier_range(tier)

            if (
                parsed is None
                or not 1 <= parsed[0] <= MAX_TIER_QUANTITY
                or (parsed[1] is not None and parsed[1] > MAX_TIER_QUANTITY)
            ):
                await fail(
                    f"❌ Tier không hợp lệ. Nhập khoảng số lượng (tối đa {MAX_TIER_QUANTITY:,}), "
                    "ví dụ `1-5`, `7-11` hoặc `1001+`.",
                    f"❌ Invalid tier. Enter a quantity range (up to {MAX_TIER_QUANTITY:,}), "
                    "for example `1-5`, `7-11` or `1001+`."
                )
                return

            min_qty, max_qty, tier_kind = parsed

            if tier_kind == "single":
                # Just a number: keep the range of the tier that starts there,
                # otherwise it is a new open-ended tier ("N+").
                for tier_item in tiers:
                    if tier_item["min_qty"] == min_qty:
                        max_qty = tier_item.get("max_qty")

            if min_qty not in starts and len(tiers) >= MAX_TIERS:
                await fail(
                    f"❌ Mỗi sản phẩm tối đa **{MAX_TIERS}** bậc giá. Hãy dùng `/removetier` để xóa bớt.",
                    f"❌ A product can have at most **{MAX_TIERS}** tiers. Use `/removetier` to delete one."
                )
                return

        elif len(tiers) == 1:
            min_qty = tiers[0]["min_qty"]
            max_qty = tiers[0].get("max_qty")

        else:
            start_list = ", ".join(
                format_range(t["min_qty"], t.get("max_qty"), lang) for t in tiers
            )
            await fail(
                f"❌ **{target['name']}** có nhiều bậc giá ({start_list}). "
                "Hãy nhập **tier** muốn đổi giá.",
                f"❌ **{target['name']}** has several tiers ({start_list}). "
                "Please enter the **tier** you want to change."
            )
            return

    old_tier = None
    old_warranty = None

    # Serialized with purchases so a sale never reads a half-updated price.
    async with SHOP_TRANSACTION_LOCK:

        target = get_product_by_id(target["id"])

        if target is None:
            await fail(
                "❌ Sản phẩm này không còn tồn tại.",
                "❌ This product no longer exists."
            )
            return

        if price is not None:
            status, info = set_product_tier_price(
                target["id"], min_qty, max_qty, price
            )

            if status == "overlap":
                clash = format_range(info["min_qty"], info.get("max_qty"), lang)
                await fail(
                    f"❌ Khoảng **{format_range(min_qty, max_qty, lang)}** bị trùng với bậc "
                    f"**{clash}** đang có. Hãy chọn khoảng khác, hoặc xóa bậc đó bằng "
                    "`/removetier`. Muốn sửa một bậc, hãy nhập đúng số bắt đầu của nó.",
                    f"❌ The range **{format_range(min_qty, max_qty, lang)}** overlaps the existing tier "
                    f"**{clash}**. Choose another range, or delete that tier with "
                    "`/removetier`. To edit a tier, enter its exact starting number."
                )
                return

            if status != "ok":
                await fail(
                    f"❌ Không thể cập nhật giá (tối đa {MAX_TIERS} bậc hoặc sản phẩm không tồn tại).",
                    f"❌ Could not update the price (limit of {MAX_TIERS} tiers or product missing)."
                )
                return

            old_tier = info

        if warranty_days is not None:
            old_warranty = set_product_warranty(target["id"], warranty_days)

        log_activity(
            interaction.user.id,
            interaction.user.name,
            "admin_set_price",
            {
                "product_id": target["id"],
                "product": target["name"],
                "min_qty": min_qty,
                "max_qty": max_qty,
                "old_price": old_tier["price"] if old_tier else None,
                "old_max_qty": old_tier.get("max_qty") if old_tier else None,
                "new_price": price,
                "old_warranty_days": old_warranty,
                "new_warranty_days": warranty_days
            },
            "command"
        )

    updated = get_product_by_id(target["id"]) or target
    tiers = product_tiers(updated)

    embed = make_embed(
        pick(lang, "💰 Đã cập nhật sản phẩm", "💰 Product Updated"),
        f"**{updated['name']}**",
        UI_GREEN
    )

    if price is not None:
        label = tier_label(tiers, min_qty, lang)

        if old_tier is None:
            embed.add_field(
                name=pick(lang, "🆕 Bậc mới", "🆕 New tier"),
                value=pick(
                    lang,
                    f"**{label} account** • **`{price:,} VND`**",
                    f"**{label} accounts** • **`{price:,} VND`**"
                ),
                inline=False
            )
        else:
            embed.add_field(
                name=pick(lang, f"Bậc {label} • Giá cũ", f"Tier {label} • Old price"),
                value=f"`{old_tier['price']:,} VND`",
                inline=True
            )
            embed.add_field(
                name=pick(lang, "Giá mới", "New price"),
                value=f"**`{price:,} VND`**",
                inline=True
            )

            if old_tier.get("max_qty") != max_qty:
                embed.add_field(
                    name=pick(lang, "📏 Khoảng số lượng", "📏 Quantity range"),
                    value=(
                        f"{format_range(min_qty, old_tier.get('max_qty'), lang)} ➜ "
                        f"**{label}**"
                    ),
                    inline=False
                )

    if warranty_days is not None:
        embed.add_field(
            name=pick(lang, "🛡️ Bảo hành", "🛡️ Warranty"),
            value=(
                f"{warranty_days_label(old_warranty, lang)} ➜ "
                f"**{warranty_days_label(warranty_days, lang)}**"
            ),
            inline=False
        )

    embed.add_field(
        name=pick(lang, "📋 Bảng giá hiện tại", "📋 Current prices"),
        value=build_price_table(lang, tiers),
        inline=False
    )

    gap_text = gap_warning_text(tiers, lang)
    if gap_text:
        embed.add_field(
            name=pick(lang, "⚠️ Khoảng chưa có giá", "⚠️ Quantities without a price"),
            value=gap_text,
            inline=False
        )

    if any(
        tiers[i + 1]["price"] > tiers[i]["price"]
        for i in range(len(tiers) - 1)
    ):
        embed.add_field(
            name=pick(lang, "⚠️ Lưu ý", "⚠️ Heads up"),
            value=pick(
                lang,
                "Có bậc mua nhiều đang có giá cao hơn bậc mua ít. "
                "Hãy kiểm tra lại nếu đây không phải chủ ý.",
                "A larger-quantity tier now costs more per account than a smaller one. "
                "Double-check this is intended."
            ),
            inline=False
        )

    notes = []
    if tier_kind == "single" and old_tier is None:
        notes.append(pick(
            lang,
            f"Bạn chỉ nhập một số nên bậc mới là **{min_qty:,}+** (không giới hạn trên). "
            "Muốn giới hạn, nhập dạng `7-11`.",
            f"You entered a single number, so the new tier is **{min_qty:,}+** (no upper limit). "
            "Use a range like `7-11` to limit it."
        ))
    if price is not None:
        notes.append(pick(
            lang,
            "Đơn khách đã mở xác nhận trước khi đổi giá vẫn giữ giá khách đã thấy.",
            "Orders a customer already opened for confirmation keep the price they were shown."
        ))
    if warranty_days is not None:
        notes.append(pick(
            lang,
            "Bảo hành mới chỉ áp dụng cho đơn hàng mới; đơn cũ giữ nguyên thời hạn đã bán.",
            "The new warranty applies to new orders only; existing orders keep their own warranty."
        ))

    embed.add_field(
        name=pick(lang, "ℹ️ Ghi chú", "ℹ️ Notes"),
        value="\n".join(notes)[:1024],
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

    # Refresh the public panel right away instead of waiting for the loop.
    try:
        await update_stock_message()
    except Exception as e:
        print(f"[PRICE] Panel refresh error: {e}")


@setprice.autocomplete("product")
async def setprice_product_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    if not can_manage(interaction):
        return []
    current = current.strip().casefold()

    choices = []
    for item in get_all_products():
        if current and current not in item["name"].casefold() and current not in item["id"]:
            continue
        tier_count = len(product_tiers(item))
        choices.append(
            app_commands.Choice(
                name=f"{item['name']} ({tier_count} tier)"[:100],
                value=item["id"]
            )
        )

    return choices[:25]


@setprice.autocomplete("tier")
async def setprice_tier_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    if not can_manage(interaction):
        return []
    target = find_product_by_query(
        getattr(interaction.namespace, "product", "")
    )

    if target is None:
        return []

    tiers = product_tiers(target)
    choices = []

    # What the admin is typing right now (a new tier or a changed range).
    parsed = parse_tier_range(current) if current.strip() else None
    if parsed is not None and parsed[2] != "single":
        low, high, _ = parsed
        if 1 <= low <= MAX_TIER_QUANTITY and (high is None or high <= MAX_TIER_QUANTITY):
            exists = any(t["min_qty"] == low for t in tiers)
            prefix = "✏️ Edit tier" if exists else "➕ New tier"
            choices.append(
                app_commands.Choice(
                    name=f"{prefix}: {format_range(low, high, 'en')} accounts"[:100],
                    value=range_text(low, high)
                )
            )

    for tier_item in tiers:
        low, high = tier_item["min_qty"], tier_item.get("max_qty")
        choices.append(
            app_commands.Choice(
                name=f"{format_range(low, high, 'en')} • {int(tier_item['price']):,} VND"[:100],
                value=range_text(low, high)
            )
        )

    return choices[:25]


# =========================================================
# /REMOVETIER
# =========================================================

@tree.command(
    name="removetier",
    description="Admin: remove a quantity tier from a product"
)
@app_commands.default_permissions(administrator=True)
@admin_check()
@app_commands.describe(
    product="Product to change",
    tier="Tier to remove (pick one from the list or type its starting number)"
)
async def removetier(
    interaction: discord.Interaction,
    product: str,
    tier: str
):

    lang = get_user_language(interaction.user.id)

    async def fail(vi, en):
        await interaction.response.send_message(
            pick(lang, vi, en),
            ephemeral=True
        )

    target = find_product_by_query(product)

    if target is None:
        await fail(
            "❌ Không tìm thấy sản phẩm. Hãy chọn từ danh sách gợi ý.",
            "❌ Product not found. Please pick one from the suggestions."
        )
        return

    parsed = parse_tier_range(tier)

    if parsed is None:
        await fail(
            "❌ Tier không hợp lệ. Hãy chọn từ danh sách hoặc nhập số bắt đầu của bậc, ví dụ `7`.",
            "❌ Invalid tier. Pick one from the list or enter its starting number, for example `7`."
        )
        return

    min_qty = parsed[0]

    async with SHOP_TRANSACTION_LOCK:

        before = product_tiers(target)
        label = tier_label(before, min_qty, lang)

        status, removed = remove_product_tier(target["id"], min_qty)

        if status == "ok":
            log_activity(
                interaction.user.id,
                interaction.user.name,
                "admin_remove_tier",
                {
                    "product_id": target["id"],
                    "product": target["name"],
                    "min_qty": min_qty,
                    "max_qty": removed.get("max_qty"),
                    "removed_price": removed["price"]
                },
                "command"
            )

    if status == "last":
        await fail(
            "❌ Đây là bậc giá cuối cùng của sản phẩm nên không thể xóa. "
            "Hãy thêm bậc khác trước, hoặc dùng `/setprice` để sửa bậc này.",
            "❌ This is the product's last tier and cannot be removed. "
            "Add another tier first, or use `/setprice` to edit it."
        )
        return

    if status == "missing":
        await fail(
            f"❌ **{target['name']}** không có bậc nào bắt đầu từ **{min_qty:,}**.",
            f"❌ **{target['name']}** has no tier starting at **{min_qty:,}**."
        )
        return

    if status != "ok":
        await fail(
            "❌ Sản phẩm này không còn tồn tại.",
            "❌ This product no longer exists."
        )
        return

    updated = get_product_by_id(target["id"]) or target
    tiers = product_tiers(updated)

    embed = make_embed(
        pick(lang, "🗑️ Đã xóa bậc giá", "🗑️ Tier Removed"),
        f"**{updated['name']}**",
        UI_GREEN
    )
    embed.add_field(
        name=pick(lang, "Bậc đã xóa", "Removed tier"),
        value=f"{label} • `{removed['price']:,} VND`",
        inline=False
    )
    embed.add_field(
        name=pick(lang, "📋 Bảng giá hiện tại", "📋 Current prices"),
        value=build_price_table(lang, tiers),
        inline=False
    )

    gap_text = gap_warning_text(tiers, lang)
    if gap_text:
        embed.add_field(
            name=pick(lang, "⚠️ Khoảng chưa có giá", "⚠️ Quantities without a price"),
            value=gap_text,
            inline=False
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

    try:
        await update_stock_message()
    except Exception as e:
        print(f"[PRICE] Panel refresh error: {e}")


@removetier.autocomplete("product")
async def removetier_product_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    if not can_manage(interaction):
        return []
    return await setprice_product_autocomplete(interaction, current)


@removetier.autocomplete("tier")
async def removetier_tier_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    if not can_manage(interaction):
        return []
    target = find_product_by_query(
        getattr(interaction.namespace, "product", "")
    )

    if target is None:
        return []

    choices = []
    for tier_item in product_tiers(target):
        low, high = tier_item["min_qty"], tier_item.get("max_qty")
        choices.append(
            app_commands.Choice(
                name=f"{format_range(low, high, 'en')} • {int(tier_item['price']):,} VND"[:100],
                value=range_text(low, high)
            )
        )

    return choices[:25]


# =========================================================
# /ADDSTOCK
# =========================================================

MAX_STOCK_UPLOAD_LINES = 50_000


@tree.command(name="addstock", description="Admin: select a product and upload a TXT file to add stock")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@admin_check()
@app_commands.describe(
    product="Choose the product to replenish",
    file="UTF-8 .txt file, one account per line"
)
async def addstock(interaction: discord.Interaction, product: str, file: discord.Attachment):
    lang = get_user_language(interaction.user.id)

    async def fail(vi, en):
        message = pick(lang, vi, en)
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    target = find_product_by_query(product)
    if target is None:
        await fail("❌ Không tìm thấy sản phẩm. Hãy chọn lại trong danh sách.",
                   "❌ Product not found. Select a product from the list.")
        return
    if not file.filename.lower().endswith(".txt"):
        await fail("❌ Hãy gửi file có đuôi .txt, mỗi dòng một account.",
                   "❌ Upload a .txt file with one account per line.")
        return

    await interaction.response.defer(ephemeral=True)
    try:
        content = await file.read()
    except (discord.HTTPException, OSError):
        await fail("❌ Không tải được file. Kho chưa thay đổi; hãy thử lại.",
                   "❌ Could not download the file. Stock is unchanged; please retry.")
        return
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        await fail("❌ File không phải UTF-8. Hãy lưu lại bằng UTF-8 rồi gửi lại.",
                   "❌ The file is not UTF-8. Save it as UTF-8 and upload again.")
        return
    if "\x00" in decoded:
        await fail("❌ File chứa ký tự không hợp lệ. Hãy gửi file văn bản UTF-8.",
                   "❌ Invalid characters in the file. Upload a UTF-8 text file.")
        return
    lines = decoded.splitlines()
    if len(lines) > MAX_STOCK_UPLOAD_LINES:
        await fail("❌ Mỗi lần chỉ được nhập tối đa 50.000 dòng.",
                   "❌ Upload at most 50,000 lines at a time.")
        return
    accounts = [line.strip() for line in lines if line.strip()]
    if not accounts:
        await fail("❌ File trống hoặc chỉ chứa dòng trắng. Kho chưa thay đổi.",
                   "❌ The file is empty or contains only blank lines. Stock is unchanged.")
        return

    # Download outside the lock; reread the product and stock after acquiring it.
    # Purchases, removals and other uploads share this lock. No await in the commit.
    async with SHOP_TRANSACTION_LOCK:
        target = get_product_by_id(target["id"])
        if target is not None:
            with data_transaction():
                stock = load_product_stock(target)
                seen = set(stock)
                added = []
                for account in accounts:
                    if account not in seen:
                        seen.add(account)
                        added.append(account)
                duplicates = len(accounts) - len(added)
                if added:
                    save_product_stock(target, stock + added)
                    log_activity(interaction.user.id, interaction.user.name, "admin_add_stock", {
                        "product_id": target["id"], "quantity": len(added),
                        "duplicates_skipped": duplicates, "stock_before": len(stock),
                        "stock_after": len(stock) + len(added)
                    }, "command")
                total = len(stock) + len(added)
    if target is None:
        await fail("❌ Sản phẩm đã bị xóa trong lúc tải file. Kho chưa thay đổi.",
                   "❌ The product was removed while downloading. Stock is unchanged.")
        return

    embed = make_embed(
        pick(lang, "📦 Nhập kho hoàn tất", "📦 Stock Upload Complete"),
        discord.utils.escape_markdown(target["name"]), UI_GREEN
    )
    for name, value in (
        (pick(lang, "Đã thêm", "Added"), len(added)),
        (pick(lang, "Bỏ qua dòng trùng", "Duplicate lines skipped"), duplicates),
        (pick(lang, "Tổng tồn kho", "Total stock"), total)
    ):
        embed.add_field(name=name, value=f"{value:,}", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)
    if added:
        try:
            await update_stock_message()
        except Exception as exc:
            # Inventory is already committed; a panel failure must not trigger reimport.
            print(f"[STOCK] Upload panel refresh error ({type(exc).__name__})")


@addstock.autocomplete("product")
async def addstock_product_autocomplete(interaction: discord.Interaction, current: str):
    if not can_manage(interaction):
        return []
    if not can_manage(interaction):
        return []
    query = current.strip().casefold()
    return [app_commands.Choice(name=item["name"][:100], value=item["id"])
            for item in get_all_products()
            if not query or query in item["name"].casefold() or query in item["id"].casefold()][:25]


# =========================================================
# /REMOVESTOCK
# =========================================================

@tree.command(name="removestock", description="Admin: remove a quantity of accounts from the end of a product's stock")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@admin_check()
@app_commands.describe(product="Choose the product", quantity="Number of accounts to remove from the end of stock")
async def removestock(interaction: discord.Interaction, product: str, quantity: app_commands.Range[int, 1]):
    lang = get_user_language(interaction.user.id)
    target = find_product_by_query(product)
    if target is None or quantity <= 0:
        await interaction.response.send_message(
            pick(lang, "❌ Chọn sản phẩm hợp lệ và số lượng lớn hơn 0.",
                 "❌ Select a valid product and a quantity greater than zero."), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    error = None
    async with SHOP_TRANSACTION_LOCK:
        target = get_product_by_id(target["id"])
        if target is None:
            error = pick(lang, "❌ Sản phẩm không còn tồn tại. Kho chưa thay đổi.",
                         "❌ The product no longer exists. Stock is unchanged.")
        else:
            with data_transaction():
                stock = load_product_stock(target)
                if quantity > len(stock):
                    error = pick(lang, f"❌ Kho chỉ còn {len(stock):,} account. Chưa xóa account nào.",
                                 f"❌ Only {len(stock):,} accounts remain. No accounts were removed.")
                else:
                    remaining = stock[:-quantity]
                    save_product_stock(target, remaining)
                    log_activity(interaction.user.id, interaction.user.name, "admin_remove_stock", {
                        "product_id": target["id"], "quantity": quantity,
                        "stock_before": len(stock), "stock_after": len(remaining)
                    }, "command")
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    embed = make_embed(
        pick(lang, "📦 Đã xóa khỏi kho", "📦 Stock Removed"),
        discord.utils.escape_markdown(target["name"]), UI_GREEN)
    embed.add_field(name=pick(lang, "Đã xóa từ cuối kho", "Removed from end of stock"),
                    value=f"{quantity:,}", inline=True)
    embed.add_field(name=pick(lang, "Tồn kho còn lại", "Remaining stock"),
                    value=f"{len(remaining):,}", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)
    try:
        await update_stock_message()
    except Exception as exc:
        print(f"[STOCK] Removal panel refresh error ({type(exc).__name__})")


@removestock.autocomplete("product")
async def removestock_product_autocomplete(interaction: discord.Interaction, current: str):
    if not can_manage(interaction):
        return []
    return await addstock_product_autocomplete(interaction, current)


# =========================================================
# /ADDPRODUCT
# =========================================================

@tree.command(
    name="addproduct",
    description="Admin: add a product (or update its price and warranty)"
)
@app_commands.default_permissions(administrator=True)
@admin_check()
@app_commands.describe(
    name="Product name",
    price="Price per account in VND",
    warranty_days="Warranty length in days (0 = no warranty)"
)
async def addproduct(
    interaction: discord.Interaction,
    name: str,
    price: int,
    warranty_days: int
):

    lang = get_user_language(interaction.user.id)

    name = " ".join(name.split())

    if not name or len(name) > MAX_PRODUCT_NAME_LENGTH:
        await interaction.response.send_message(
            pick(
                lang,
                f"❌ Tên sản phẩm phải từ 1 đến {MAX_PRODUCT_NAME_LENGTH} ký tự.",
                f"❌ The product name must be 1 to {MAX_PRODUCT_NAME_LENGTH} characters."
            ),
            ephemeral=True
        )
        return

    if price < 1 or price > MAX_PRICE_PER_ACCOUNT:
        await interaction.response.send_message(
            pick(
                lang,
                f"❌ Giá phải từ **1** đến **{MAX_PRICE_PER_ACCOUNT:,} VND** / account.",
                f"❌ Price must be between **1** and **{MAX_PRICE_PER_ACCOUNT:,} VND** per account."
            ),
            ephemeral=True
        )
        return

    if warranty_days < 0 or warranty_days > MAX_WARRANTY_DAYS:
        await interaction.response.send_message(
            pick(
                lang,
                f"❌ Thời gian bảo hành phải từ **0** đến **{MAX_WARRANTY_DAYS}** ngày (0 = không bảo hành).",
                f"❌ Warranty must be between **0** and **{MAX_WARRANTY_DAYS}** days (0 = no warranty)."
            ),
            ephemeral=True
        )
        return

    # Serialized with purchases so a sale never reads a half-written catalog.
    async with SHOP_TRANSACTION_LOCK:
        status, product, old = add_or_update_product(name, price, warranty_days)

        if status in ("added", "updated"):
            log_activity(
                interaction.user.id,
                interaction.user.name,
                "admin_add_product" if status == "added" else "admin_update_product",
                {
                    "product_id": product["id"],
                    "name": product["name"],
                    "price": price,
                    "warranty_days": warranty_days,
                    "old_price": old["price"] if old else None,
                    "old_warranty_days": old["warranty_days"] if old else None,
                },
                "command"
            )

    if status == "default_name":
        await interaction.response.send_message(
            pick(
                lang,
                f"❌ **{PRODUCT_NAME}** là sản phẩm mặc định của shop. "
                "Dùng `/setprice` để đổi giá của sản phẩm này, hoặc chọn tên khác.",
                f"❌ **{PRODUCT_NAME}** is the shop's default product. "
                "Use `/setprice` to change its prices, or choose another name."
            ),
            ephemeral=True
        )
        return

    if status == "limit":
        await interaction.response.send_message(
            pick(
                lang,
                f"❌ Đã đạt giới hạn **{MAX_EXTRA_PRODUCTS}** sản phẩm thêm.",
                f"❌ The limit of **{MAX_EXTRA_PRODUCTS}** added products has been reached."
            ),
            ephemeral=True
        )
        return

    stock_count = len(load_product_stock(product))
    stock_path = f"{os.path.basename(DATA_DIR)}/{product['stock_file']}"

    embed = make_embed(
        pick(
            lang,
            "🆕 Đã thêm sản phẩm" if status == "added" else "✏️ Đã cập nhật sản phẩm",
            "🆕 Product Added" if status == "added" else "✏️ Product Updated"
        ),
        f"**{product['name']}**",
        UI_GREEN
    )

    embed.add_field(
        name=pick(lang, "💰 Giá", "💰 Price"),
        value=(
            f"`{price:,} VND` / account"
            if not old else
            f"`{old['price']:,}` ➜ **`{price:,} VND`**"
        ),
        inline=True
    )
    embed.add_field(
        name=pick(lang, "🛡️ Bảo hành", "🛡️ Warranty"),
        value=(
            warranty_days_label(warranty_days, lang)
            if not old else
            f"{warranty_days_label(old['warranty_days'], lang)} ➜ "
            f"**{warranty_days_label(warranty_days, lang)}**"
        ),
        inline=True
    )
    embed.add_field(
        name=pick(lang, "📦 Kho", "📦 Stock"),
        value=f"`{stock_count:,}` account",
        inline=True
    )
    embed.add_field(
        name="🆔 ID",
        value=f"`{product['id']}`",
        inline=True
    )
    embed.add_field(
        name=pick(lang, "📦 Nhập kho", "📦 Replenish stock"),
        value=(
            pick(
                lang,
                f"Dùng `/addstock`, chọn sản phẩm **{product['name']}** và gửi file `.txt` (mỗi dòng một account).",
                f"Use `/addstock`, select **{product['name']}** and upload a `.txt` file (one account per line)."
            )
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

    # Show the new product on the public panel right away.
    try:
        await update_stock_message()
    except Exception as e:
        print(f"[PRODUCT] Panel refresh error: {e}")


# =========================================================
# /REMOVEPRODUCT
# =========================================================

class RemoveProductConfirmView(View):
    def __init__(self, user_id, language, product, confirmed_stock_digest=None):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.language = "en" if language == "en" else "vi"
        self.product = product
        self.confirmed_stock_digest = confirmed_stock_digest
        self.completed = False
        self.confirm_button.label = pick(self.language, "Xác nhận xóa", "Confirm removal")
        if confirmed_stock_digest is not None:
            self.confirm_button.label = pick(self.language, "Xóa cả sản phẩm và nội dung kho", "Delete product and stock contents")
        self.cancel_button.label = pick(self.language, "Hủy", "Cancel")

    async def interaction_check(self, interaction: discord.Interaction):
        if not await ensure_allowed_interaction(interaction):
            return False
        if (interaction.user.id == self.user_id and can_manage(interaction)):
            return True
        await interaction.response.send_message(
            pick(self.language, "Bạn không có quyền xác nhận thao tác này.", "You cannot confirm this action."),
            ephemeral=True)
        return False

    @discord.ui.button(label="Confirm removal", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)
    async def confirm_button(self, interaction: discord.Interaction, button: Button):
        if not await ensure_allowed_interaction(interaction):
            return False
        if not await self.interaction_check(interaction):
            return
        lang = self.language
        await interaction.response.defer()
        async with SHOP_TRANSACTION_LOCK:
            if self.completed:
                await interaction.followup.send(pick(lang, "Thao tác đã kết thúc.", "This action has already ended."), ephemeral=True)
                return
            try:
                status, removed = remove_product(self.product["id"], self.confirmed_stock_digest)
            except (OSError, ValueError) as exc:
                print(f"[PRODUCT] Removal blocked ({type(exc).__name__})")
                await interaction.followup.send(pick(lang, "Không thể đọc hoặc xóa kho an toàn. Chưa xóa sản phẩm; hãy kiểm tra quyền tệp và thử lại.",
                                                          "Could not safely read or remove stock. Product kept; check file access and retry."), ephemeral=True)
                return
            if status == "stock_confirmation_required":
                # No await between observing the current bytes and remembering their digest.
                try:
                    _, content, digest = product_stock_snapshot(removed)
                except (OSError, ValueError):
                    await interaction.followup.send(pick(lang, "Không đọc được kho; chưa xóa dữ liệu.", "Cannot read stock; nothing removed."), ephemeral=True)
                    return
                # A new view has new button IDs: queued double-clicks from the first
                # prompt cannot count as confirmation of destructive stock deletion.
                next_view = RemoveProductConfirmView(self.user_id, lang, removed, digest)
                self.completed = True
                self.stop()
                count = sum(bool(line.strip()) for line in content.splitlines())
                embed = make_embed(pick(lang, "⚠️ Xác nhận lần 2: kho vẫn còn nội dung", "⚠️ Second confirmation: stock is not empty"),
                    pick(lang,
                         f"**{discord.utils.escape_markdown(removed['name'])}**\n\nKho vẫn còn {count:,} account.\n**Xóa sẽ loại bỏ toàn bộ số account này khỏi kho đang bán.**\nBấm xác nhận thêm một lần để xóa sản phẩm và kho. Chọn **Hủy** để giữ nguyên.",
                         f"**{discord.utils.escape_markdown(removed['name'])}**\n\nStock still contains {count:,} accounts.\n**Deletion removes all of these accounts from sale.**\nConfirm once more to delete the product and stock, or **Cancel** to keep them."), UI_RED)
                await interaction.edit_original_response(embed=embed, view=next_view)
                return
            self.completed = True
            self.stop()
        if status != "removed":
            await interaction.edit_original_response(embed=make_embed(
                pick(lang, "Không tìm thấy sản phẩm", "Product not found"),
                pick(lang, "Sản phẩm đã được xóa hoặc không tồn tại.", "The product was already removed or does not exist."), UI_RED), view=None)
            return
        log_activity(interaction.user.id, interaction.user.name, "admin_remove_product",
                     {"product_id": removed["id"], "name": removed["name"],
                      "stock_file": removed["stock_file"], "stock_file_deleted": True,
                      "extra_confirmation": self.confirmed_stock_digest is not None}, "command")
        embed = make_embed(pick(lang, "🗑️ Đã xóa sản phẩm và kho", "🗑️ Product and stock deleted"),
            f"**{discord.utils.escape_markdown(removed['name'])}**\n`data/{removed['stock_file']}`", UI_GREEN)
        await interaction.edit_original_response(embed=embed, view=None)
        try:
            await update_stock_message()
        except Exception as exc:
            print(f"[PRODUCT] Panel refresh error: {exc}")

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary, row=0)
    async def cancel_button(self, interaction: discord.Interaction, button: Button):
        if not await ensure_allowed_interaction(interaction):
            return False
        if not await self.interaction_check(interaction):
            return
        async with SHOP_TRANSACTION_LOCK:
            if self.completed:
                await interaction.response.send_message(pick(self.language, "Thao tác đã kết thúc.", "This action has already ended."), ephemeral=True)
                return
            self.completed = True
            self.stop()
        await interaction.response.edit_message(embed=make_embed(
            pick(self.language, "↩️ Đã hủy", "↩️ Cancelled"),
            pick(self.language, "Sản phẩm và tệp kho được giữ nguyên.", "The product and stock file have been kept."), UI_BLUE), view=None)

    async def on_timeout(self):
        self.completed = True
        self.stop()


@tree.command(
    name="removeproduct",
    description="Admin: remove a product, including the original default product"
)
@app_commands.default_permissions(administrator=True)
@admin_check()
@app_commands.describe(
    product="Product to remove"
)
async def removeproduct(
    interaction: discord.Interaction,
    product: str
):

    lang = get_user_language(interaction.user.id)

    query = " ".join(product.split())

    target = find_product_by_query(query)
    if target is None:
        await interaction.response.send_message(
            pick(lang, "❌ Không tìm thấy sản phẩm. Hãy chọn từ danh sách gợi ý.",
                 "❌ Product not found. Please pick one from the suggestions."), ephemeral=True)
        return

    stock_count = len(load_product_stock(target))

    embed = make_embed(
        pick(lang, "⚠️ Xác nhận xóa sản phẩm", "⚠️ Confirm Product Removal"),
        (
            f"**{target['name']}**\n\n"
            + pick(
                lang,
                "Sản phẩm sẽ biến mất khỏi panel và khách không thể mua nữa. "
                "Kho hàng cũng sẽ bị xóa. Nếu còn account, bot sẽ yêu cầu xác nhận thêm một lần.",
                "The product will disappear from the panel and can no longer "
                "be bought. Its stock will also be deleted; nonempty stock requires a second confirmation."
            )
        ),
        UI_GOLD
    )
    embed.add_field(
        name=pick(lang, "💰 Giá", "💰 Price"),
        value=f"`{product_price_text(target, lang)}` / acc",
        inline=True
    )
    embed.add_field(
        name=pick(lang, "🛡️ Bảo hành", "🛡️ Warranty"),
        value=warranty_days_label(product_warranty_days(target), lang),
        inline=True
    )
    embed.add_field(
        name=pick(lang, "📦 Kho", "📦 Stock"),
        value=f"`{stock_count:,}` account",
        inline=True
    )
    embed.add_field(
        name="🆔 ID",
        value=f"`{target['id']}`",
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        view=RemoveProductConfirmView(interaction.user.id, lang, target),
        ephemeral=True
    )


@removeproduct.autocomplete("product")
async def removeproduct_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    if not can_manage(interaction):
        return []
    current = current.strip().casefold()

    choices = []
    for item in get_all_products():
        if current and current not in item["name"].casefold() and current not in item["id"]:
            continue
        choices.append(
            app_commands.Choice(
                name=f"{item['name']} ({item['id']})"[:100],
                value=item["id"]
            )
        )

    return choices[:25]


# =========================================================
# /REMOVEBALANCE
# =========================================================

@tree.command(
    name="removebalance",
    description="Admin: remove balance from a user"
)
@admin_check()
@app_commands.describe(
    user="User whose balance will be removed",
    amount="Amount in VND",
    reason="Optional reason, shown to the customer in the DM"
)
async def removebalance(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int,
    reason: str = None
):

    if amount <= 0:
        await interaction.response.send_message(
            "❌ Số tiền phải lớn hơn **0 VND**.",
            ephemeral=True
        )
        return

    reason = clean_reason(reason)

    if reason and len(reason) > MAX_BALANCE_REASON_LENGTH:
        await interaction.response.send_message(
            f"❌ Lý do tối đa **{MAX_BALANCE_REASON_LENGTH}** ký tự "
            f"(hiện tại {len(reason)}).",
            ephemeral=True
        )
        return

    async with SHOP_TRANSACTION_LOCK:

        target = ensure_user(user)
        old_balance = Decimal(str(get_user_balance(user.id)))

        if old_balance < Decimal(amount):
            await interaction.response.send_message(
                embed=make_embed(
                    "❌ Insufficient Balance",
                    (
                        f"User: {user.mention}\n"
                        f"Current balance: **{format_vnd(old_balance)}**\n"
                        f"Requested removal: **{amount:,} VND**"
                    ),
                    UI_RED
                ),
                ephemeral=True
            )
            return

        with data_transaction():
            new_balance = old_balance - Decimal(amount)
            target["balance"] = vnd(new_balance)

            save_json(
                USERS_FILE,
                users
            )

            log_activity(
                interaction.user.id,
                interaction.user.name,
                "admin_remove_balance",
                {
                    "target_user_id": user.id,
                    "target_username": user.name,
                    "amount": amount,
                    "balance_before": vnd(old_balance),
                    "balance_after": vnd(new_balance),
                    "reason": reason
                },
                "command"
            )

    embed = make_embed(
        "💸 Balance Removed",
        (
            f"Đã trừ **{amount:,} VND** từ {user.mention}.\n\n"
            f"💳 Balance trước: **{format_vnd(old_balance)}**\n"
            f"💳 Balance sau: **{format_vnd(new_balance)}**"
        ),
        UI_GOLD
    )

    if reason:
        embed.add_field(
            name="📝 Lý do / Reason",
            value=reason[:1024],
            inline=False
        )

    # The DM can take a moment: acknowledge the command first so Discord's
    # 3-second interaction window never expires.
    await interaction.response.defer(ephemeral=True)

    # Notify the customer by DM (outside the lock; never blocks a purchase).
    dm_sent = await notify_balance_change(
        user, "remove", amount, old_balance, new_balance, reason
    )
    dm_status_field(
        embed, dm_sent, get_user_language(interaction.user.id)
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /PROFILE
# =========================================================

@tree.command(
    name="profile",
    description="View your profile"
)
async def profile(
    interaction: discord.Interaction
):

    user = ensure_user(
        interaction.user
    )

    lang = get_user_language(interaction.user.id)

    balance = parse_number(
        user.get(
            "balance",
            0
        )
    )

    embed = make_embed(
        pick(lang, "👤 Hồ sơ của bạn", "👤 Your Profile"),
        pick(
            lang,
            f"Chào mừng **{interaction.user.display_name}**.\n\n"
            "Thông tin tài khoản Lemon Service của bạn:",
            f"Welcome, **{interaction.user.display_name}**.\n\n"
            "Your Lemon Service account information is shown below."
        ),
        UI_BLUE
    )

    if interaction.user.avatar:
        embed.set_thumbnail(
            url=interaction.user.avatar.url
        )

    embed.add_field(
        name=pick(lang, "👤 Tên người dùng", "👤 Username"),
        value=f"`{interaction.user.name}`",
        inline=True
    )

    embed.add_field(
        name="🆔 User ID",
        value=f"`{interaction.user.id}`",
        inline=True
    )

    embed.add_field(
        name=pick(lang, "💳 Số dư", "💳 Balance"),
        value=f"**{balance_money(balance, lang)}**",
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        view=ShopPanelView(lang),
        ephemeral=True
    )


# =========================================================
# /STOCK
# =========================================================

@tree.command(
    name="stock",
    description="Check product stock"
)
async def stock(
    interaction: discord.Interaction
):

    await interaction.response.send_message(
        embed=build_stock_embed(get_user_language(interaction.user.id)),
        view=ShopPanelView(get_user_language(interaction.user.id)),
        ephemeral=True,
        **panel_banner_attach(get_user_language(interaction.user.id))
    )


# =========================================================
# /BUY
# =========================================================

@tree.command(
    name="buy",
    description="Buy product"
)
async def buy(
    interaction: discord.Interaction
):

    ensure_user(
        interaction.user
    )

    await open_buy_flow(interaction)


# =========================================================
# /HISTORY
# =========================================================

@tree.command(name="history", description="View your top-up and order history")
async def history(interaction: discord.Interaction):
    lang = get_user_language(interaction.user.id)
    recent = get_user_orders(interaction.user.id, 10)
    topups = get_user_topups(interaction.user.id, 10)
    embed = make_embed(
        pick(lang, "🧾 Lịch sử", "🧾 History"),
        pick(
            lang,
            "Lịch sử nạp tiền và lịch sử đơn hàng của bạn.",
            "Your top-up history and order history."
        ),
        UI_PURPLE
    )

    topup_title = pick(lang, "💳 Lịch sử nạp tiền", "💳 Top Up History")
    order_title = pick(lang, "🧾 Lịch sử đơn hàng", "🧾 Order History")

    if topups:
        embed.add_field(
            name=topup_title,
            value="\n".join(
                f"`{x.get('created_at', '')[:19]}` • **{balance_money(x.get('amount_vnd', 0), lang)}** • {x.get('method', 'Unknown')}"
                for x in topups[:10]
            )[:1024],
            inline=False
        )
    else:
        embed.add_field(
            name=topup_title,
            value=pick(lang, "Chưa có lịch sử nạp tiền.", "No top-up history yet."),
            inline=False
        )

    if recent:
        for oid, o in recent[:5]:
            status = get_order_warranty_status(o)
            if status.get("no_warranty"):
                warranty_line = pick(
                    lang,
                    "⚪ Bảo hành: **KHÔNG CÓ**",
                    "⚪ Warranty: **NONE**"
                )
            elif status["active"]:
                warranty_line = pick(
                    lang,
                    f"🛡️ Bảo hành còn: **{format_warranty_remaining(status['remaining'])}**",
                    f"🛡️ Warranty remaining: **{format_warranty_remaining(status['remaining'])}**"
                )
            else:
                warranty_line = pick(
                    lang,
                    "🔴 Bảo hành: **ĐÃ HẾT HẠN**",
                    "🔴 Warranty: **EXPIRED**"
                )
            embed.add_field(
                name=f"🧾 {oid}",
                value=(
                    pick(
                        lang,
                        f"📦 {o.get('quantity', 0):,} account • 💵 {balance_money(o.get('total', 0), lang)}\n"
                        f"🕒 {o.get('created_at', 'Không xác định')}\n",
                        f"📦 {o.get('quantity', 0):,} accounts • 💵 {balance_money(o.get('total', 0), lang)}\n"
                        f"🕒 {o.get('created_at', 'Unknown')}\n"
                    )
                    + f"{warranty_line}"
                ),
                inline=False
            )
    else:
        embed.add_field(
            name=order_title,
            value=pick(lang, "Bạn chưa có đơn hàng hoàn tất.", "You have no completed orders yet."),
            inline=False
        )

    await interaction.response.send_message(embed=embed, view=ShopPanelView(lang), ephemeral=True)


# =========================================================
# /ORDER
# =========================================================

@tree.command(name="order", description="Check an order by Order ID")
@app_commands.describe(order_id="Example: LC-20260923-A1B2C3")
async def order(interaction: discord.Interaction, order_id: str):
    lang = get_user_language(interaction.user.id)
    order_id = order_id.strip().upper()
    data = load_orders().get(order_id)
    if not data:
        await interaction.response.send_message(
            pick(
                lang,
                f"❌ Không tìm thấy đơn hàng `{order_id}`.",
                f"❌ Order `{order_id}` was not found."
            ),
            ephemeral=True
        )
        return
    if str(data.get("user_id")) != str(interaction.user.id):
        await interaction.response.send_message(
            pick(
                lang,
                "❌ Bạn chỉ có thể xem đơn hàng của chính mình.",
                "❌ You can only view your own orders."
            ),
            ephemeral=True
        )
        return
    embed = make_embed(
        f"🧾 {pick(lang, 'Đơn hàng', 'Order')} {order_id}",
        pick(lang, "Chi tiết đơn hàng", "Order details"),
        UI_PURPLE
    )
    embed.add_field(name=pick(lang, "📦 Sản phẩm", "📦 Product"), value=data.get("product", PRODUCT_NAME), inline=False)
    embed.add_field(
        name=pick(lang, "🔢 Số lượng", "🔢 Quantity"),
        value=pick(lang, f"{data.get('quantity', 0):,} account", f"{data.get('quantity', 0):,} accounts"),
        inline=True
    )
    embed.add_field(name=pick(lang, "💰 Đơn giá", "💰 Unit Price"), value=unit_money(data.get('unit_price', 0), lang), inline=True)
    embed.add_field(name=pick(lang, "💵 Tổng tiền", "💵 Total"), value=balance_money(data.get('total', 0), lang), inline=True)
    embed.add_field(
        name=pick(lang, "📌 Trạng thái", "📌 Status"),
        value=pick(lang, "🟢 Hoàn tất", "🟢 Completed"),
        inline=True
    )
    embed.add_field(
        name=pick(lang, "🕒 Thời gian tạo", "🕒 Created"),
        value=str(data.get("created_at", pick(lang, "Không xác định", "Unknown"))),
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =========================================================
# /WARRANTY
# =========================================================

@tree.command(name="warranty", description="Admin: check warranty information by Order ID")
@admin_check()
@app_commands.describe(order_id="Enter the Order ID, e.g. LC-20260923-A1B2C3")
async def warranty(interaction: discord.Interaction, order_id: str):
    order_id = order_id.strip().upper()
    data = load_orders().get(order_id)

    if not data:
        await interaction.response.send_message(
            "❌ Order not found or the order record has expired. "
            f"Order records are retained for {ORDER_RETENTION_DAYS} days.",
            ephemeral=True
        )
        return

    status = get_order_warranty_status(data)
    status_text = warranty_status_text(data, "vi")

    if status.get("no_warranty"):
        description = (
            "⚪ Đơn hàng này không có bảo hành.\n\n"
            f"{status_text}"
        )
        color = UI_GOLD
    elif status["active"]:
        description = (
            f"🟢 Đơn hàng vẫn còn trong thời gian bảo hành {get_order_warranty_days(data)} ngày.\n\n"
            f"{status_text}"
        )
        color = UI_GREEN
    else:
        description = (
            f"🔴 Đơn hàng đã hết thời hạn bảo hành {get_order_warranty_days(data)} ngày.\n\n"
            f"{status_text}"
        )
        color = UI_RED

    embed = make_embed(
        "🛡️ Warranty Lookup",
        description,
        color
    )
    embed.add_field(name="🧾 Order ID", value=f"`{order_id}`", inline=False)
    embed.add_field(name="👤 Customer", value=str(data.get("username", "Unknown")), inline=True)
    embed.add_field(name="🆔 User ID", value=f"`{data.get('user_id', 'Unknown')}`", inline=True)
    embed.add_field(name="🛍️ Product", value=str(data.get("product", PRODUCT_NAME)), inline=False)
    embed.add_field(name="📦 Quantity", value=f"{data.get('quantity', 0):,} accounts", inline=True)
    embed.add_field(name="💰 Unit Price", value=f"{data.get('unit_price', 0):,.0f} VND", inline=True)
    embed.add_field(name="💵 Total", value=f"{data.get('total', 0):,.0f} VND", inline=True)
    embed.add_field(name="🕒 Purchased", value=str(data.get("created_at", "Unknown")), inline=False)
    embed.add_field(name="🛡️ Warranty Status", value=status_text, inline=False)
    embed.set_footer(text=f"{warranty_footer(get_order_warranty_days(data), 'en')} • Order records: {ORDER_RETENTION_DAYS}+ days")

    accounts = data.get("accounts", [])
    if not isinstance(accounts, list):
        accounts = []

    files = []
    if accounts:
        account_content = "\n".join(str(account) for account in accounts)
        files.append(
            discord.File(
                io.BytesIO(account_content.encode("utf-8")),
                filename=f"{order_id}_accounts.txt"
            )
        )
        embed.add_field(
            name="📄 Accounts File",
            value=f"`{order_id}_accounts.txt` attached below.",
            inline=False
        )
    else:
        embed.add_field(
            name="📄 Accounts File",
            value="⚠️ No account list is stored for this order.",
            inline=False
        )

    log_activity(
        interaction.user.id,
        interaction.user.name,
        "warranty_check_admin",
        {"order_id": order_id, "active": status["active"]},
        "command"
    )

    await interaction.response.send_message(
        embed=embed,
        files=files,
        ephemeral=True
    )


# =========================================================
# /CHECKWARRANTY — CUSTOMER
# =========================================================

@tree.command(name="checkwarranty", description="Check your order warranty status")
@app_commands.describe(order_id="Enter your Order ID, e.g. LC-20260923-A1B2C3")
async def checkwarranty(interaction: discord.Interaction, order_id: str):
    ensure_user(interaction.user)
    order_id = order_id.strip().upper()
    data = load_orders().get(order_id)

    if not data:
        await interaction.response.send_message(
            pick(
                get_user_language(interaction.user.id),
                "❌ Không tìm thấy mã đơn hàng hoặc đơn hàng đã hết thời gian lưu trữ.",
                "❌ Order not found or the order record has expired."
            ),
            ephemeral=True
        )
        return

    # Customers can only inspect their own orders.
    if str(data.get("user_id")) != str(interaction.user.id):
        await interaction.response.send_message(
            pick(
                get_user_language(interaction.user.id),
                "❌ Bạn chỉ có thể kiểm tra bảo hành của đơn hàng do chính tài khoản này mua.",
                "❌ You can only check the warranty of orders placed by your own account."
            ),
            ephemeral=True
        )
        return

    lang = get_user_language(interaction.user.id)
    status = get_order_warranty_status(data)

    if status.get("no_warranty"):
        if lang == "en":
            title = "🛡️ No Warranty"
            description = f"Order `{order_id}` was sold **without warranty**."
        else:
            title = "🛡️ Không bảo hành"
            description = f"Đơn hàng `{order_id}` được bán **không kèm bảo hành**."
        color = UI_GOLD
    elif status["active"]:
        if lang == "en":
            title = "🛡️ Warranty Status"
            description = (
                f"Order `{order_id}` is **ACTIVE** for warranty.\n\n"
                f"⏳ Remaining: **{format_warranty_remaining(status['remaining'])}**\n"
                f"📅 Expires: `{status['expires_at'].strftime('%Y-%m-%d %H:%M:%S')}`"
            )
        else:
            title = "🛡️ Trạng thái bảo hành"
            description = (
                f"Đơn hàng `{order_id}` **CÒN HIỆU LỰC** bảo hành.\n\n"
                f"⏳ Còn lại: **{format_warranty_remaining(status['remaining'])}**\n"
                f"📅 Hết hạn: `{status['expires_at'].strftime('%Y-%m-%d %H:%M:%S')}`"
            )
        color = UI_GREEN
    else:
        if lang == "en":
            title = "🛡️ Warranty Status"
            description = (
                f"Order `{order_id}` warranty has **EXPIRED**.\n\n"
                f"📅 Expired: `{status['expires_at'].strftime('%Y-%m-%d %H:%M:%S') if status['expires_at'] else 'Unknown'}`"
            )
        else:
            title = "🛡️ Trạng thái bảo hành"
            description = (
                f"Đơn hàng `{order_id}` đã **HẾT HẠN BẢO HÀNH**.\n\n"
                f"📅 Hết hạn: `{status['expires_at'].strftime('%Y-%m-%d %H:%M:%S') if status['expires_at'] else 'Không xác định'}`"
            )
        color = UI_RED

    embed = make_embed(title, description, color)
    embed.add_field(
        name=pick(lang, "📦 Số lượng", "📦 Quantity"),
        value=pick(lang, f"{data.get('quantity', 0):,} account", f"{data.get('quantity', 0):,} accounts"),
        inline=True
    )
    embed.add_field(name=pick(lang, "💵 Tổng tiền", "💵 Total"), value=balance_money(data.get('total', 0), lang), inline=True)
    embed.add_field(
        name=pick(lang, "🕒 Thời gian mua", "🕒 Purchased"),
        value=str(data.get("created_at", pick(lang, "Không xác định", "Unknown"))),
        inline=False
    )
    embed.set_footer(
        text=pick(
            lang,
            warranty_footer(get_order_warranty_days(data), "vi"),
            warranty_footer(get_order_warranty_days(data), "en")
        )
    )

    log_activity(
        interaction.user.id,
        interaction.user.name,
        "warranty_check_customer",
        {"order_id": order_id, "active": status["active"]},
        "command"
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# =========================================================
# /CHECKUSERNAME — ADMIN ONLY
# =========================================================

@tree.command(
    name="checkusername",
    description="Admin: view the full activity log of a username"
)
@admin_check()
@app_commands.describe(username="Exact Discord username to inspect")
async def checkusername(interaction: discord.Interaction, username: str):
    username = username.strip().lstrip("@").strip()
    user_ids = get_activity_for_username(username)

    # Also match current Discord usernames case-insensitively.
    if not user_ids:
        for uid, data in users.items():
            if str(data.get("username", "")).lower() == username.lower():
                user_ids.append(uid)

    if not user_ids:
        log_activity(interaction.user.id, interaction.user.name, "admin_checkusername", {"searched_username": username}, "command")
        await interaction.response.send_message(
            embed=make_embed(
                "🔎 Username Activity",
                f"No stored activity was found for `{username}`.",
                UI_RED
            ),
            ephemeral=True
        )
        return

    logs = load_activity_logs()
    topups = load_topup_history()
    order_data = load_orders()
    embed = make_embed(
        f"🔎 Activity • {username}",
        "Administrator-only audit log. This includes recorded commands, button interactions, top-ups, orders, language changes, and account events.",
        UI_BLUE
    )

    total_events = 0
    lines = []
    for uid in user_ids:
        entries = [x for x in logs.get(str(uid), []) if isinstance(x, dict)]
        total_events += len(entries)
        for e in entries:
            details = e.get("details", {})
            if not isinstance(details, dict):
                details = {"message": str(details)}
            detail_text = ", ".join(
                f"{k}={str(v)[:80]}"
                for k, v in details.items()
                if k not in {"message"}
            )
            if not detail_text:
                detail_text = str(details.get("message", ""))[:100]
            lines.append(
                f"`{str(e.get('timestamp',''))[:19]}` • **{e.get('action','unknown')}** • {detail_text}"
            )

        user_orders = [
            (oid, o) for oid, o in order_data.items()
            if str(o.get("user_id")) == str(uid)
        ]
        user_topups = [x for x in topups if str(x.get("user_id")) == str(uid)]
        embed.add_field(
            name=f"👤 User {uid}",
            value=(
                f"Username: `{users.get(str(uid), {}).get('username', username)}`\n"
                f"Current balance: **{format_vnd(users.get(str(uid), {}).get('balance', 0))}**\n"
                f"Orders: **{len(user_orders)}** • Top-ups: **{len(user_topups)}**"
            ),
            inline=False
        )

    # Discord embeds have a description limit; show the newest recorded events.
    lines = sorted(lines, reverse=True)
    if lines:
        embed.add_field(
            name=f"📜 Activity Log • {total_events} recorded events",
            value="\n".join(lines[:40])[:1024],
            inline=False
        )
    else:
        embed.add_field(name="📜 Activity Log", value="No activity events recorded yet.", inline=False)

    log_activity(
        interaction.user.id,
        interaction.user.name,
        "admin_checkusername",
        {"searched_username": username, "matched_user_ids": user_ids},
        "command"
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# =========================================================
# /TOPUP
# =========================================================

@tree.command(
    name="topup",
    description="Top up VND using Vietcombank"
)
async def topup(
    interaction: discord.Interaction
):

    ensure_user(
        interaction.user
    )

    await interaction.response.send_modal(
        TopupModal(get_user_language(interaction.user.id))
    )


# =========================================================
# /TOPUPCRYPTO
# =========================================================

@tree.command(
    name="topupcrypto",
    description="Top up using crypto"
)
async def topupcrypto(
    interaction: discord.Interaction
):

    ensure_user(
        interaction.user
    )

    await interaction.response.send_modal(
        CryptoModal(get_user_language(interaction.user.id))
    )


# =========================================================
# /SETSTOCKMESSAGE
# =========================================================

async def resolve_panel_channel(channel_id):
    """Returns (channel, error_key). error_key: not_found / forbidden / error."""

    channel = bot.get_channel(channel_id)

    if channel is not None:
        if not POLICY.allowed_guild(getattr(getattr(channel, 'guild', None), 'id', None)):
            return None, 'forbidden'
        return channel, None

    try:
        channel = await bot.fetch_channel(channel_id)
        if not POLICY.allowed_guild(getattr(getattr(channel, 'guild', None), 'id', None)):
            return None, 'forbidden'
        return channel, None
    except discord.NotFound:
        return None, "not_found"
    except discord.Forbidden:
        return None, "forbidden"
    except Exception as e:
        print(f"[STOCK] Channel lookup error ({channel_id}): {e}")
        return None, "error"


def parse_channel_id(value):
    """Accepts an ID or a #channel mention. Returns an int or None."""
    match = re.search(r"\d{15,22}", str(value or ""))
    return int(match.group(0)) if match else None


@tree.command(
    name="setstockmessage",
    description="Create/update the shop panel in a channel (authorized servers only)"
)
@admin_check()
@app_commands.describe(
    channel_id="Channel ID to post the panel in (leave empty for the default channel)"
)
async def setstockmessage(
    interaction: discord.Interaction,
    channel_id: str = None
):

    lang = get_user_language(interaction.user.id)

    # Posting the panel calls Discord several times: acknowledge first.
    await interaction.response.defer(ephemeral=True)

    async def fail(vi, en):
        await interaction.followup.send(
            pick(lang, vi, en),
            ephemeral=True
        )

    explicit = bool(channel_id and channel_id.strip())
    target_id = STOCK_CHANNEL_ID

    if explicit:
        target_id = parse_channel_id(channel_id)

        if target_id is None:
            await fail(
                "❌ ID kênh không hợp lệ. Bấm chuột phải vào kênh → **Copy Channel ID**.",
                "❌ Invalid channel ID. Right-click the channel → **Copy Channel ID**."
            )
            return

    channel, error = await resolve_panel_channel(target_id)

    if channel is None:
        if error == "forbidden":
            await fail(
                f"❌ Bot không có quyền truy cập kênh `{target_id}`.",
                f"❌ The bot has no access to channel `{target_id}`."
            )
        else:
            await fail(
                f"❌ Không tìm thấy kênh `{target_id}`. Hãy kiểm tra ID và chắc chắn bot đã ở trong server đó.",
                f"❌ Channel `{target_id}` was not found. Check the ID and make sure the bot is in that server."
            )
        return

    if not isinstance(channel, discord.TextChannel):
        await fail(
            "❌ Kênh này không phải kênh văn bản (text channel).",
            "❌ That is not a text channel."
        )
        return

    # Only configured administrators may post in another explicitly allowed server.
    if explicit:
        same_server = (
            interaction.guild is not None
            and channel.guild.id == interaction.guild.id
        )

        if not same_server and interaction.user.id not in POLICY.config['admin_user_ids']:
            await fail(
                "❌ Bạn chỉ có thể đặt panel trong kênh của server này.",
                "❌ You can only post the panel in a channel of this server."
            )
            return

    me = channel.guild.me
    permissions = channel.permissions_for(me) if me else None

    if permissions is not None:
        missing = [
            name for name, allowed in (
                ("View Channel", permissions.view_channel),
                ("Send Messages", permissions.send_messages),
                ("Embed Links", permissions.embed_links),
                ("Attach Files", permissions.attach_files),
            ) if not allowed
        ]

        if missing:
            await fail(
                f"❌ Bot thiếu quyền trong {channel.mention}: **{', '.join(missing)}**.",
                f"❌ The bot is missing permissions in {channel.mention}: **{', '.join(missing)}**."
            )
            return

    async with STOCK_PANEL_LOCK:
        panels = load_stock_panels()
        status = await update_stock_panel(channel.id, panels)

        if status == "ok":
            save_json(STOCK_PANELS_FILE, panels)
            message_id = panels[str(channel.id)]["message_id"]
            total_panels = len(
                set(panels) | {str(STOCK_CHANNEL_ID)}
            )

    if status != "ok":
        await fail(
            f"❌ Không thể gửi panel vào {channel.mention} ({status}).",
            f"❌ Could not post the panel in {channel.mention} ({status})."
        )
        return

    log_activity(
        interaction.user.id,
        interaction.user.name,
        "admin_set_stock_message",
        {
            "channel_id": channel.id,
            "guild_id": channel.guild.id,
            "guild": channel.guild.name
        },
        "command"
    )

    embed = make_embed(
        pick(lang, "✅ Đã cập nhật panel cửa hàng", "✅ Shop Panel Updated"),
        pick(
            lang,
            "Panel đã được tạo hoặc làm mới và sẽ tự cập nhật mỗi 5 giây.",
            "The panel was created or refreshed and will update every 5 seconds."
        ),
        UI_GREEN
    )
    embed.add_field(
        name=pick(lang, "📍 Kênh", "📍 Channel"),
        value=f"{channel.mention}\n`{channel.id}`",
        inline=True
    )
    embed.add_field(
        name=pick(lang, "🌐 Server", "🌐 Server"),
        value=f"{channel.guild.name}",
        inline=True
    )
    embed.add_field(
        name=pick(lang, "📊 Tổng số panel", "📊 Active panels"),
        value=f"`{total_panels}`",
        inline=True
    )
    embed.add_field(
        name=pick(lang, "🔗 Tin nhắn", "🔗 Message"),
        value=f"https://discord.com/channels/{channel.guild.id}/{channel.id}/{message_id}",
        inline=False
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /REMOVESTOCKMESSAGE
# =========================================================

@tree.command(
    name="removestockmessage",
    description="Stop showing the shop panel in a channel and delete it"
)
@admin_check()
@app_commands.describe(
    channel_id="Channel ID whose panel should be removed"
)
async def removestockmessage(
    interaction: discord.Interaction,
    channel_id: str
):

    lang = get_user_language(interaction.user.id)

    await interaction.response.defer(ephemeral=True)

    async def fail(vi, en):
        await interaction.followup.send(
            pick(lang, vi, en),
            ephemeral=True
        )

    target_id = parse_channel_id(channel_id)

    if target_id is None:
        await fail(
            "❌ ID kênh không hợp lệ. Bấm chuột phải vào kênh → **Copy Channel ID**.",
            "❌ Invalid channel ID. Right-click the channel → **Copy Channel ID**."
        )
        return

    if target_id == STOCK_CHANNEL_ID:
        await fail(
            "❌ Đây là kênh panel mặc định nên không thể gỡ (panel sẽ tự được tạo lại).",
            "❌ This is the default panel channel and cannot be removed (the panel would be recreated)."
        )
        return

    channel, _ = await resolve_panel_channel(target_id)

    if channel is not None:
        same_server = (
            interaction.guild is not None
            and getattr(channel, "guild", None) is not None
            and channel.guild.id == interaction.guild.id
        )
        allowed = same_server or interaction.user.id in POLICY.config['admin_user_ids']
    else:
        allowed = interaction.user.id in POLICY.config['admin_user_ids']

    if not allowed:
        await fail(
            "❌ Bạn chỉ có thể gỡ panel trong server này.",
            "❌ You can only remove panels of this server."
        )
        return

    async with STOCK_PANEL_LOCK:
        panels = load_stock_panels()
        entry = panels.pop(str(target_id), None)

        if entry is not None:
            save_json(STOCK_PANELS_FILE, panels)

    if entry is None:
        await fail(
            f"❌ Kênh `{target_id}` chưa có panel nào.",
            f"❌ Channel `{target_id}` has no panel."
        )
        return

    # Best effort: also delete the panel message itself.
    deleted = False
    if channel is not None and entry.get("message_id"):
        try:
            message = await channel.fetch_message(int(entry["message_id"]))
            await message.delete()
            deleted = True
        except Exception as e:
            print(f"[STOCK] Could not delete panel message: {e}")

    log_activity(
        interaction.user.id,
        interaction.user.name,
        "admin_remove_stock_message",
        {"channel_id": target_id, "message_deleted": deleted},
        "command"
    )

    await interaction.followup.send(
        embed=make_embed(
            pick(lang, "🗑️ Đã gỡ panel", "🗑️ Panel Removed"),
            pick(
                lang,
                f"Kênh `{target_id}` sẽ không còn được cập nhật panel."
                + ("" if deleted else "\nBạn có thể tự xóa tin nhắn panel cũ."),
                f"Channel `{target_id}` will no longer receive panel updates."
                + ("" if deleted else "\nYou can delete the old panel message yourself.")
            ),
            UI_GREEN
        ),
        ephemeral=True
    )


# =========================================================
# BACKGROUND LOOP
# =========================================================

@tasks.loop(seconds=5)
async def main_loop():
    try:
        await update_stock_message()
    except Exception as exc:
        logging.warning('[STOCK] Panel refresh failed (%s)', type(exc).__name__)


@tasks.loop(seconds=30)
async def bank_loop():
    try:
        await process_bank_transactions()
    except Exception as exc:
        await service_result('bank', False, type(exc).__name__)


@tasks.loop(seconds=30)
async def crypto_loop():
    try:
        await process_crypto_orders()
    except Exception as exc:
        await service_result('crypto', False, type(exc).__name__)


# =========================================================
# ORDER RETENTION
# =========================================================

@tasks.loop(hours=1)
async def order_cleanup_loop():
    try:
        cleanup_expired_orders()
    except Exception as e:
        print(f"[ORDERS] Cleanup error: {e}")


# =========================================================
# FULL INTERACTION AUDIT
# =========================================================

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.guild is not None and not POLICY.allowed_guild(interaction.guild.id):
        return
    try:
        user = interaction.user
        data = interaction.data or {}
        interaction_type = str(interaction.type)
        action = data.get("custom_id") or data.get("name") or "interaction"
        details = {}
        if data.get("custom_id"):
            details["custom_id"] = str(data.get("custom_id"))
        if data.get("name"):
            details["command"] = str(data.get("name"))
        # Do not store modal values or account credentials.
        log_activity(
            user.id,
            user.name,
            f"interaction:{action}",
            details,
            interaction_type
        )
    except Exception as e:
        print(f"[AUDIT] Interaction error: {e}")


# =========================================================
# TICKET SYSTEM (merged from the Lemon Service ticket bot)
# =========================================================

# =========================================================
# CONFIG
# =========================================================

TICKETS_FILE = os.path.join(BASE_DIR, "tickets.json")

TICKET_CATEGORY_ID = 1551783945383710843
STAFF_ROLE_ID = 1551564751052738640

BOT_NAME = "Lemon Service"

BOT_COLOR = discord.Color.from_rgb(
    255,
    205,
    60
)

DELETE_CONFIRM_TIMEOUT = 30

# Slash commands that belong to the ticket system (own error messages).
TICKET_COMMAND_NAMES = {"ticketinfo", "ticketpanel"}


# =========================================================
# JSON
# =========================================================

def ticket_load_json(path, default):
    return load_json(path, default)


def ticket_save_json(path, data):
    save_json(path, data)



tickets = ticket_load_json(
    TICKETS_FILE,
    {}
)


# =========================================================
# LANGUAGE DATABASE
# =========================================================

user_languages = {}

LANGUAGE_FILE = os.path.join(BASE_DIR, "user_languages.json")


def load_languages():

    global user_languages

    user_languages = ticket_load_json(
        LANGUAGE_FILE,
        {}
    )


def save_languages():

    ticket_save_json(
        LANGUAGE_FILE,
        user_languages
    )


load_languages()


def get_language(
    user_id
):

    return user_languages.get(
        str(user_id),
        "vi"
    )


def set_language(
    user_id,
    language
):

    if language not in (
        "vi",
        "en"
    ):
        language = "vi"

    user_languages[str(user_id)] = language

    save_languages()


def get_ticket_language(
    ticket_data,
    user_id=None
):

    if ticket_data:

        language = ticket_data.get(
            "language"
        )

        if language in (
            "vi",
            "en"
        ):

            return language

    if user_id is not None:

        return get_language(
            user_id
        )

    return "vi"


# =========================================================
# TRANSLATIONS
# =========================================================

TEXT = {

    "vi": {

        "create_ticket": "Create Ticket",

        "english": "🇬🇧 English",

        "vietnamese": "🇻🇳 Tiếng Việt",

        "panel_title": "🍋 LEMON SERVICE",

        "panel_description": (
            "### 🎫 Support Center\n\n"

            "Cần hỗ trợ? Hãy tạo một ticket để "
            "liên hệ với đội ngũ "
            "**Lemon Service Staff**.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "🎫 **Create Ticket**\n"
            "Tạo một ticket riêng để được hỗ trợ.\n\n"

            "👤 **1 Ticket / User**\n"
            "Mỗi người chỉ được mở một ticket "
            "tại một thời điểm.\n\n"

            "🔒 **Private Support**\n"
            "Chỉ bạn và đội ngũ Staff có thể xem "
            "ticket của bạn.\n\n"

            "⚡ **Fast Support**\n"
            "Vui lòng mô tả vấn đề rõ ràng để "
            "Staff hỗ trợ nhanh hơn.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "📌 **Lưu ý**\n"
            "• Không spam ticket.\n"
            "• Không tag Staff liên tục.\n"
            "• Cung cấp đầy đủ thông tin khi "
            "cần hỗ trợ."
        ),

        "support": "🛡️ Support",

        "support_value": (
            "Lemon Service Staff\n"
            "Always here to help."
        ),

        "ticket": "🎫 Ticket",

        "ticket_value": (
            "Private Channel\n"
            "1 Ticket / User"
        ),

        "panel_footer": (
            "Lemon Service • Support Center"
        ),

        "language_changed_en": (
            "🇬🇧 Đã chuyển giao diện của bạn "
            "sang English."
        ),

        "language_changed_vi": (
            "🇻🇳 Đã chuyển giao diện của bạn "
            "sang Tiếng Việt."
        ),

        "private_panel_sent": (
            "📨 Đã gửi giao diện ngôn ngữ riêng "
            "cho bạn."
        ),

        "already_ticket": (
            "❌ Bạn đang có một ticket đang mở.\n\n"
            "🎫 Ticket: {channel}"
        ),

        "category_missing": (
            "❌ Không tìm thấy Ticket Category."
        ),

        "category_invalid": (
            "❌ TICKET_CATEGORY_ID không phải Category."
        ),

        "staff_role_missing": (
            "❌ Không tìm thấy Staff Role."
        ),

        "bot_create_permission": (
            "❌ Bot không có quyền tạo channel.\n"
            "Hãy cấp cho bot quyền **Manage Channels**."
        ),

        "create_error": (
            "❌ Không thể tạo ticket."
        ),

        "ticket_created": (
            "✅ **Ticket đã được tạo!**\n\n"
            "🎫 {channel}"
        ),

        "ticket_title": "🎫 Support Ticket",

        "ticket_description": (
            "Xin chào {user}!\n\n"

            "Vui lòng gửi nội dung cần hỗ trợ "
            "tại đây.\n"

            "Đội ngũ **Lemon Service Staff** sẽ "
            "phản hồi sớm nhất có thể.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "📌 **Hướng dẫn**\n"
            "• Mô tả vấn đề rõ ràng.\n"
            "• Gửi Order ID nếu liên quan đến "
            "đơn hàng.\n"
            "• Có thể gửi screenshot khi cần.\n"
            "• Không spam nhiều tin nhắn liên tục.\n\n"

            "Khi vấn đề đã được giải quyết, "
            "sử dụng nút **🔒 Close Ticket**."
        ),

        "customer": "👤 Customer",

        "status": "📌 Status",

        "staff": "👨‍💼 Staff",

        "created": "🕐 Created",

        "open": "🟢 Open",

        "closed": "🔴 Closed",

        "not_claimed": "Chưa có Staff nhận",

        "claim": "Claim",

        "close_ticket": "Close Ticket",

        "reopen_ticket": "Reopen Ticket",

        "delete_ticket": "Delete Ticket",

        "claim_not_staff": (
            "❌ Chỉ Staff mới có thể claim ticket."
        ),

        "claim_not_found": (
            "❌ Không tìm thấy dữ liệu ticket."
        ),

        "claim_closed": (
            "❌ Ticket này đã đóng."
        ),

        "claim_self": (
            "🙋 Bạn đã claim ticket này."
        ),

        "claim_other": (
            "❌ Ticket đã được claim bởi {staff}."
        ),

        "claimed_message": (
            "🙋 {staff} đã **claim ticket này**."
        ),

        "invalid_ticket": (
            "❌ Đây không phải ticket hợp lệ."
        ),

        "no_close_permission": (
            "❌ Bạn không có quyền đóng ticket."
        ),

        "already_closed": (
            "❌ Ticket này đã được đóng."
        ),

        "close_modal_title": (
            "🔒 Close Ticket"
        ),

        "close_reason_label": (
            "Lý do đóng ticket"
        ),

        "close_reason_placeholder": (
            "Ví dụ: Đã xử lý xong yêu cầu "
            "của khách hàng..."
        ),

        "close_reason_required": (
            "❌ Vui lòng nhập lý do đóng ticket."
        ),

        "closed_title": (
            "🔒 Ticket Closed"
        ),

        "closed_description": (
            "Ticket này đã được đóng.\n\n"

            "👤 **Customer:** {customer}\n"
            "👨‍💼 **Closed by:** {staff}\n\n"

            "📝 **Lý do đóng:**\n"
            "> {reason}\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "🔓 Staff có thể mở lại ticket nếu cần.\n"
            "🗑️ Ticket có thể được xóa sau khi "
            "hoàn tất."
        ),

        "closed_at": "🕐 Closed At",

        "close_reason": "📝 Close Reason",

        "closed_by": "👨‍💼 Closed By",

        "reopen_not_staff": (
            "❌ Chỉ Staff mới có thể mở lại ticket."
        ),

        "reopen_not_found": (
            "❌ Không tìm thấy dữ liệu ticket."
        ),

        "reopened_title": (
            "🔓 Ticket Reopened"
        ),

        "reopened_description": (
            "Ticket đã được mở lại.\n\n"

            "👤 **Customer:** {customer}\n"
            "🟢 **Status:** Open\n\n"

            "Customer có thể tiếp tục gửi "
            "tin nhắn."
        ),

        "delete_not_staff": (
            "❌ Chỉ Staff mới có thể xóa ticket."
        ),

        "delete_title": (
            "🗑️ Delete Ticket"
        ),

        "delete_description": (
            "### ⚠️ Xác nhận xóa ticket\n\n"

            "Bạn có chắc chắn muốn xóa ticket này?\n\n"

            "❌ Toàn bộ channel sẽ bị xóa.\n"
            "❌ Hành động này không thể hoàn tác.\n"
            "❌ Dữ liệu ticket sẽ bị xóa khỏi database."
        ),

        "confirm_delete": (
            "Confirm Delete"
        ),

        "cancel": (
            "Cancel"
        ),

        "deleting": (
            "🗑️ Đang xóa ticket..."
        ),

        "delete_cancelled": (
            "❌ Đã hủy xóa ticket."
        ),

        "ticket_info": (
            "ℹ️ Ticket Information"
        ),

        "ticket_channel": (
            "🎫 Channel"
        ),

        "claimed_by": (
            "🙋 Claimed By"
        ),

        "not_closed": (
            "Chưa đóng"
        ),

        "ticket_info_no_permission": (
            "❌ Bạn không có quyền xem thông tin ticket."
        ),

        "not_ticket": (
            "❌ Channel này không phải ticket."
        ),

        "only_server": (
            "❌ Lệnh này chỉ dùng trong server."
        ),

        "no_panel_permission": (
            "❌ Bạn không có quyền sử dụng lệnh này."
        ),

        "panel_sent": (
            "✅ Đã gửi **Lemon Service Ticket Panel**."
        ),

        "generic_error": (
            "❌ Có lỗi xảy ra khi thực hiện lệnh."
        )
    },


    "en": {

        "create_ticket": "Create Ticket",

        "english": "🇬🇧 English",

        "vietnamese": "🇻🇳 Vietnamese",

        "panel_title": "🍋 LEMON SERVICE",

        "panel_description": (
            "### 🎫 Support Center\n\n"

            "Need help? Create a ticket to contact "
            "the **Lemon Service Staff** team.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "🎫 **Create Ticket**\n"
            "Create a private support ticket.\n\n"

            "👤 **1 Ticket / User**\n"
            "Each user can only have one open ticket.\n\n"

            "🔒 **Private Support**\n"
            "Only you and the Staff team can view "
            "your ticket.\n\n"

            "⚡ **Fast Support**\n"
            "Please describe your issue clearly so "
            "Staff can assist you faster.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "📌 **Important**\n"
            "• Do not spam tickets.\n"
            "• Do not repeatedly ping Staff.\n"
            "• Provide all required information."
        ),

        "support": "🛡️ Support",

        "support_value": (
            "Lemon Service Staff\n"
            "Always here to help."
        ),

        "ticket": "🎫 Ticket",

        "ticket_value": (
            "Private Channel\n"
            "1 Ticket / User"
        ),

        "panel_footer": (
            "Lemon Service • Support Center"
        ),

        "language_changed_en": (
            "🇬🇧 Your interface has been changed "
            "to English."
        ),

        "language_changed_vi": (
            "🇻🇳 Your interface has been changed "
            "to Vietnamese."
        ),

        "private_panel_sent": (
            "📨 Your language-specific panel has "
            "been sent to you."
        ),

        "already_ticket": (
            "❌ You already have an open ticket.\n\n"
            "🎫 Ticket: {channel}"
        ),

        "category_missing": (
            "❌ Ticket Category was not found."
        ),

        "category_invalid": (
            "❌ TICKET_CATEGORY_ID is not a Category."
        ),

        "staff_role_missing": (
            "❌ Staff Role was not found."
        ),

        "bot_create_permission": (
            "❌ The bot does not have permission "
            "to create channels.\n"
            "Please give the bot **Manage Channels**."
        ),

        "create_error": (
            "❌ Unable to create the ticket."
        ),

        "ticket_created": (
            "✅ **Ticket created!**\n\n"
            "🎫 {channel}"
        ),

        "ticket_title": (
            "🎫 Support Ticket"
        ),

        "ticket_description": (
            "Hello {user}!\n\n"

            "Please describe your issue below.\n"

            "The **Lemon Service Staff** team will "
            "assist you as soon as possible.\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "📌 **Instructions**\n"
            "• Clearly describe your issue.\n"
            "• Provide your Order ID if applicable.\n"
            "• You may attach screenshots if needed.\n"
            "• Please do not spam messages.\n\n"

            "When your issue has been resolved, "
            "use the **🔒 Close Ticket** button."
        ),

        "customer": "👤 Customer",

        "status": "📌 Status",

        "staff": "👨‍💼 Staff",

        "created": "🕐 Created",

        "open": "🟢 Open",

        "closed": "🔴 Closed",

        "not_claimed": "Not claimed",

        "claim": "Claim",

        "close_ticket": "Close Ticket",

        "reopen_ticket": "Reopen Ticket",

        "delete_ticket": "Delete Ticket",

        "claim_not_staff": (
            "❌ Only Staff can claim tickets."
        ),

        "claim_not_found": (
            "❌ Ticket data could not be found."
        ),

        "claim_closed": (
            "❌ This ticket is closed."
        ),

        "claim_self": (
            "🙋 You have already claimed this ticket."
        ),

        "claim_other": (
            "❌ This ticket has already been claimed "
            "by {staff}."
        ),

        "claimed_message": (
            "🙋 {staff} has **claimed this ticket**."
        ),

        "invalid_ticket": (
            "❌ This is not a valid ticket."
        ),

        "no_close_permission": (
            "❌ You do not have permission to close "
            "this ticket."
        ),

        "already_closed": (
            "❌ This ticket is already closed."
        ),

        "close_modal_title": (
            "🔒 Close Ticket"
        ),

        "close_reason_label": (
            "Close Reason"
        ),

        "close_reason_placeholder": (
            "Example: Customer's issue has been "
            "successfully resolved..."
        ),

        "close_reason_required": (
            "❌ Please enter a reason for closing "
            "the ticket."
        ),

        "closed_title": (
            "🔒 Ticket Closed"
        ),

        "closed_description": (
            "This ticket has been closed.\n\n"

            "👤 **Customer:** {customer}\n"
            "👨‍💼 **Closed by:** {staff}\n\n"

            "📝 **Close Reason:**\n"
            "> {reason}\n\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            "🔓 Staff can reopen this ticket if needed.\n"
            "🗑️ The ticket can be deleted when finished."
        ),

        "closed_at": "🕐 Closed At",

        "close_reason": "📝 Close Reason",

        "closed_by": "👨‍💼 Closed By",

        "reopen_not_staff": (
            "❌ Only Staff can reopen tickets."
        ),

        "reopen_not_found": (
            "❌ Ticket data could not be found."
        ),

        "reopened_title": (
            "🔓 Ticket Reopened"
        ),

        "reopened_description": (
            "This ticket has been reopened.\n\n"

            "👤 **Customer:** {customer}\n"
            "🟢 **Status:** Open\n\n"

            "The customer can continue sending "
            "messages."
        ),

        "delete_not_staff": (
            "❌ Only Staff can delete tickets."
        ),

        "delete_title": (
            "🗑️ Delete Ticket"
        ),

        "delete_description": (
            "### ⚠️ Confirm Ticket Deletion\n\n"

            "Are you sure you want to delete this ticket?\n\n"

            "❌ The entire channel will be deleted.\n"
            "❌ This action cannot be undone.\n"
            "❌ Ticket data will be removed from the database."
        ),

        "confirm_delete": (
            "Confirm Delete"
        ),

        "cancel": (
            "Cancel"
        ),

        "deleting": (
            "🗑️ Deleting ticket..."
        ),

        "delete_cancelled": (
            "❌ Ticket deletion cancelled."
        ),

        "ticket_info": (
            "ℹ️ Ticket Information"
        ),

        "ticket_channel": (
            "🎫 Channel"
        ),

        "claimed_by": (
            "🙋 Claimed By"
        ),

        "not_closed": (
            "Not closed"
        ),

        "ticket_info_no_permission": (
            "❌ You do not have permission to view "
            "this ticket information."
        ),

        "not_ticket": (
            "❌ This channel is not a ticket."
        ),

        "only_server": (
            "❌ This command can only be used in a server."
        ),

        "no_panel_permission": (
            "❌ You do not have permission to use "
            "this command."
        ),

        "panel_sent": (
            "✅ **Lemon Service Ticket Panel sent.**"
        ),

        "generic_error": (
            "❌ An error occurred while executing "
            "the command."
        )
    }
}


def t(
    user_id,
    key,
    **kwargs
):

    language = get_language(
        user_id
    )

    return TEXT.get(
        language,
        TEXT["vi"]
    ).get(
        key,
        TEXT["vi"].get(
            key,
            key
        )
    ).format(
        **kwargs
    )


def ticket_t(
    ticket_data,
    key,
    **kwargs
):

    language = get_ticket_language(
        ticket_data
    )

    return TEXT.get(
        language,
        TEXT["vi"]
    ).get(
        key,
        TEXT["vi"].get(
            key,
            key
        )
    ).format(
        **kwargs
    )


# =========================================================
# HELPERS
# =========================================================

def get_staff_role(
    guild
):

    if guild is None:
        return None

    return guild.get_role(
        STAFF_ROLE_ID
    )


async def get_guild_member(guild, user_id):
    """
    Return a guild member without needing the privileged members intent:
    cache first, then the REST API. Returns None if the user left the server.
    """

    member = guild.get_member(user_id)

    if member is not None:
        return member

    try:
        return await guild.fetch_member(user_id)

    except Exception:
        return None


def is_staff(
    member
):

    if not isinstance(
        member,
        discord.Member
    ):

        return False

    if not POLICY.allowed_guild(member.guild.id):
        return False
    if member.guild_permissions.administrator and member.id in POLICY.config['admin_user_ids']:
        return True

    role = get_staff_role(
        member.guild
    )

    if role and role in member.roles:

        return True

    return False


def sanitize_channel_name(
    username
):

    username = username.lower()

    username = re.sub(
        r"[^a-z0-9-]",
        "-",
        username
    )

    username = re.sub(
        r"-+",
        "-",
        username
    )

    username = username.strip("-")

    if not username:

        username = "user"

    return username[:65]


def format_timestamp(
    timestamp
):

    try:

        dt = datetime.fromisoformat(
            timestamp
        )

        return (
            f"<t:{int(dt.timestamp())}:F>"
        )

    except Exception:

        return "Unknown"


def find_user_ticket(
    guild_id,
    user_id
):

    guild_id = str(
        guild_id
    )

    user_id = str(
        user_id
    )

    guild_tickets = tickets.get(
        guild_id,
        {}
    )

    for (
        ticket_id,
        ticket_data
    ) in guild_tickets.items():

        if str(
            ticket_data.get(
                "user_id"
            )
        ) != user_id:

            continue

        if ticket_data.get(
            "status"
        ) == "open":

            return (
                ticket_id,
                ticket_data
            )

    return (
        None,
        None
    )


def get_ticket_data(
    guild_id,
    channel_id
):

    guild_id = str(
        guild_id
    )

    channel_id = str(
        channel_id
    )

    guild_tickets = tickets.get(
        guild_id,
        {}
    )

    return guild_tickets.get(
        channel_id
    )


def save_ticket(
    guild_id,
    channel_id,
    data
):

    guild_id = str(
        guild_id
    )

    channel_id = str(
        channel_id
    )

    if guild_id not in tickets:

        tickets[guild_id] = {}

    tickets[guild_id][channel_id] = data

    ticket_save_json(
        TICKETS_FILE,
        tickets
    )


def remove_ticket(
    guild_id,
    channel_id
):

    guild_id = str(
        guild_id
    )

    channel_id = str(
        channel_id
    )

    if guild_id in tickets:

        tickets[guild_id].pop(
            channel_id,
            None
        )

        if not tickets[guild_id]:

            tickets.pop(
                guild_id,
                None
            )

    ticket_save_json(
        TICKETS_FILE,
        tickets
    )


def update_ticket(
    guild_id,
    channel_id,
    **updates
):

    ticket = get_ticket_data(
        guild_id,
        channel_id
    )

    if not ticket:

        return None

    ticket.update(
        updates
    )

    save_ticket(
        guild_id,
        channel_id,
        ticket
    )

    return ticket


# =========================================================
# EMBEDS
# =========================================================

def create_panel_embed(
    user_id=None
):

    language = (
        get_language(user_id)
        if user_id is not None
        else "vi"
    )

    data = TEXT[
        language
    ]

    embed = discord.Embed(
        title=data[
            "panel_title"
        ],
        description=data[
            "panel_description"
        ],
        color=BOT_COLOR
    )

    embed.add_field(
        name=data[
            "support"
        ],
        value=data[
            "support_value"
        ],
        inline=True
    )

    embed.add_field(
        name=data[
            "ticket"
        ],
        value=data[
            "ticket_value"
        ],
        inline=True
    )

    embed.set_footer(
        text=data[
            "panel_footer"
        ]
    )

    return embed


def create_ticket_embed(
    member,
    ticket_data
):

    language = get_ticket_language(
        ticket_data
    )

    data = TEXT[
        language
    ]

    status = (
        data["open"]
        if ticket_data.get(
            "status"
        ) == "open"
        else data["closed"]
    )

    claimed_by = ticket_data.get(
        "claimed_by"
    )

    claimed = (
        f"<@{claimed_by}>"
        if claimed_by
        else data["not_claimed"]
    )

    embed = discord.Embed(
        title=data[
            "ticket_title"
        ],
        description=data[
            "ticket_description"
        ].format(
            user=member.mention
        ),
        color=BOT_COLOR
    )

    embed.add_field(
        name=data[
            "customer"
        ],
        value=member.mention,
        inline=True
    )

    embed.add_field(
        name=data[
            "status"
        ],
        value=status,
        inline=True
    )

    embed.add_field(
        name=data[
            "staff"
        ],
        value=claimed,
        inline=True
    )

    embed.add_field(
        name=data[
            "created"
        ],
        value=format_timestamp(
            ticket_data.get(
                "created_at",
                ""
            )
        ),
        inline=True
    )

    embed.set_footer(
        text=(
            "Lemon Service Support"
        )
    )

    return embed


def create_closed_embed(
    ticket_data
):

    language = get_ticket_language(
        ticket_data
    )

    data = TEXT[
        language
    ]

    owner_id = ticket_data.get(
        "user_id"
    )

    closed_by = ticket_data.get(
        "closed_by"
    )

    reason = ticket_data.get(
        "close_reason"
    ) or "N/A"

    closed_at = ticket_data.get(
        "closed_at"
    )

    closed_by_text = (
        f"<@{closed_by}>"
        if closed_by
        else "Unknown"
    )

    closed_time = (
        format_timestamp(
            closed_at
        )
        if closed_at
        else "Unknown"
    )

    embed = discord.Embed(
        title=data[
            "closed_title"
        ],
        description=data[
            "closed_description"
        ].format(
            customer=f"<@{owner_id}>",
            staff=closed_by_text,
            reason=reason
        ),
        color=discord.Color.orange()
    )

    embed.add_field(
        name=data[
            "status"
        ],
        value=data[
            "closed"
        ],
        inline=True
    )

    embed.add_field(
        name=data[
            "closed_at"
        ],
        value=closed_time,
        inline=True
    )

    embed.set_footer(
        text="Lemon Service Support"
    )

    return embed


# =========================================================
# LANGUAGE VIEW
# =========================================================

class LanguageView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    @discord.ui.button(
        label="🇬🇧 English",
        style=discord.ButtonStyle.secondary,
        custom_id="lemon_cookie_language_english"
    )
    async def english(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        set_language(
            interaction.user.id,
            "en"
        )

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "language_changed_en"
            ),
            ephemeral=True
        )

        await interaction.followup.send(
            embed=create_panel_embed(
                interaction.user.id
            ),
            view=TicketPanelView(),
            ephemeral=True
        )

    @discord.ui.button(
        label="🇻🇳 Tiếng Việt",
        style=discord.ButtonStyle.secondary,
        custom_id="lemon_cookie_language_vietnamese"
    )
    async def vietnamese(
        self,
        interaction: discord.Interaction,
        button: discord.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        set_language(
            interaction.user.id,
            "vi"
        )

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "language_changed_vi"
            ),
            ephemeral=True
        )

        await interaction.followup.send(
            embed=create_panel_embed(
                interaction.user.id
            ),
            view=TicketPanelView(),
            ephemeral=True
        )


# =========================================================
# MAIN PANEL VIEW
# =========================================================

class TicketPanelView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    @discord.ui.button(
        label="🎫 Create Ticket",
        style=discord.ButtonStyle.success,
        custom_id="lemon_cookie_create_ticket"
    )
    async def create_ticket_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        await create_ticket_for_user(
            interaction
        )

    @discord.ui.button(
        label="🇬🇧 English",
        style=discord.ButtonStyle.secondary,
        custom_id="lemon_cookie_language_english_panel"
    )
    async def english_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        set_language(
            interaction.user.id,
            "en"
        )

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "language_changed_en"
            ),
            ephemeral=True
        )

        await interaction.followup.send(
            embed=create_panel_embed(
                interaction.user.id
            ),
            view=TicketPanelView(),
            ephemeral=True
        )

    @discord.ui.button(
        label="🇻🇳 Tiếng Việt",
        style=discord.ButtonStyle.secondary,
        custom_id="lemon_cookie_language_vietnamese_panel"
    )
    async def vietnamese_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        set_language(
            interaction.user.id,
            "vi"
        )

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "language_changed_vi"
            ),
            ephemeral=True
        )

        await interaction.followup.send(
            embed=create_panel_embed(
                interaction.user.id
            ),
            view=TicketPanelView(),
            ephemeral=True
        )


# =========================================================
# CREATE TICKET
# =========================================================

async def create_ticket_for_user(
    interaction
):

    guild = interaction.guild

    if guild is None:

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "only_server"
            ),
            ephemeral=True
        )

        return

    member = interaction.user

    language = get_language(
        member.id
    )

    # =============================================
    # EXISTING TICKET
    # =============================================

    existing_id, existing_data = find_user_ticket(
        guild.id,
        member.id
    )

    if existing_id:

        existing_channel = guild.get_channel(
            int(existing_id)
        )

        if existing_channel:

            await interaction.response.send_message(
                t(
                    member.id,
                    "already_ticket",
                    channel=existing_channel.mention
                ),
                ephemeral=True
            )

            return

        remove_ticket(
            guild.id,
            existing_id
        )

    # =============================================
    # CATEGORY
    # =============================================

    category = guild.get_channel(
        TICKET_CATEGORY_ID
    )

    if category is None:

        await interaction.response.send_message(
            t(
                member.id,
                "category_missing"
            ),
            ephemeral=True
        )

        return

    if not isinstance(
        category,
        discord.CategoryChannel
    ):

        await interaction.response.send_message(
            t(
                member.id,
                "category_invalid"
            ),
            ephemeral=True
        )

        return

    # =============================================
    # STAFF ROLE
    # =============================================

    staff_role = get_staff_role(
        guild
    )

    if staff_role is None:

        await interaction.response.send_message(
            t(
                member.id,
                "staff_role_missing"
            ),
            ephemeral=True
        )

        return

    await interaction.response.defer(
        ephemeral=True
    )

    # =============================================
    # CHANNEL NAME
    # =============================================

    channel_name = (
        "ticket-"
        + sanitize_channel_name(
            member.name
        )
    )

    existing_names = {
        channel.name
        for channel in category.channels
    }

    if channel_name in existing_names:

        channel_name = (
            channel_name[:60]
            + "-"
            + str(member.id)[-4:]
        )

    # =============================================
    # PERMISSIONS
    # =============================================

    overwrites = {

        guild.default_role:
            discord.PermissionOverwrite(
                view_channel=False
            ),

        member:
            discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True
            ),

        staff_role:
            discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                manage_messages=True
            )
    }

    if guild.me:

        overwrites[guild.me] = (
            discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True
            )
        )

    # =============================================
    # CREATE CHANNEL
    # =============================================

    try:

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=(
                f"Lemon Service Ticket | "
                f"User ID: {member.id}"
            ),
            reason=(
                f"Ticket created by "
                f"{member}"
            )
        )

    except discord.Forbidden:

        await interaction.followup.send(
            t(
                member.id,
                "bot_create_permission"
            ),
            ephemeral=True
        )

        return

    except Exception as e:

        print(
            f"[TICKET] Create error: {e}"
        )

        await interaction.followup.send(
            t(
                member.id,
                "create_error"
            ),
            ephemeral=True
        )

        return

    # =============================================
    # SAVE TICKET
    # =============================================

    created_at = (
        discord.utils.utcnow()
        .isoformat()
    )

    ticket_data = {

        "user_id": member.id,

        "channel_id": channel.id,

        "status": "open",

        "claimed_by": None,

        "created_at": created_at,

        "closed_at": None,

        "closed_by": None,

        "close_reason": None,

        "language": language
    }

    save_ticket(
        guild.id,
        channel.id,
        ticket_data
    )

    # =============================================
    # SEND TICKET
    # =============================================

    embed = create_ticket_embed(
        member,
        ticket_data
    )

    await channel.send(
        content=(
            f"{member.mention} "
            f"{staff_role.mention}"
        ),
        embed=embed,
        view=TicketControlView()
    )

    await interaction.followup.send(
        t(
            member.id,
            "ticket_created",
            channel=channel.mention
        ),
        ephemeral=True
    )


# =========================================================
# DELETE CONFIRM VIEW
# =========================================================

class DeleteConfirmView(
    discord.ui.View
):

    def __init__(
        self,
        channel_id
    ):

        super().__init__(
            timeout=DELETE_CONFIRM_TIMEOUT
        )

        self.channel_id = channel_id

    @discord.ui.button(
        label="🗑️ Confirm Delete",
        style=discord.ButtonStyle.danger,
        custom_id="lemon_cookie_confirm_delete"
    )
    async def confirm_delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        channel = interaction.channel

        if channel is None:

            return

        if not is_staff(
            interaction.user
        ):

            await interaction.response.send_message(
                t(
                    interaction.user.id,
                    "delete_not_staff"
                ),
                ephemeral=True
            )

            return

        ticket_data = get_ticket_data(
            interaction.guild.id,
            channel.id
        )

        language = get_ticket_language(
            ticket_data,
            interaction.user.id
        )

        if ticket_data:

            remove_ticket(
                interaction.guild.id,
                channel.id
            )

        await interaction.response.send_message(
            TEXT[
                language
            ][
                "deleting"
            ],
            ephemeral=True
        )

        await asyncio.sleep(
            1
        )

        try:

            await channel.delete(
                reason=(
                    f"Ticket deleted by "
                    f"{interaction.user}"
                )
            )

        except discord.Forbidden:

            pass

        except Exception as e:

            print(
                f"[TICKET] Delete error: {e}"
            )

    @discord.ui.button(
        label="❌ Cancel",
        style=discord.ButtonStyle.secondary,
        custom_id="lemon_cookie_cancel_delete"
    )
    async def cancel_delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        ticket_data = get_ticket_data(
            interaction.guild.id,
            self.channel_id
        )

        language = get_ticket_language(
            ticket_data,
            interaction.user.id
        )

        await interaction.response.edit_message(
            content=TEXT[
                language
            ][
                "delete_cancelled"
            ],
            embed=None,
            view=None
        )


# =========================================================
# CLOSE TICKET MODAL
# =========================================================

class CloseTicketModal(
    discord.ui.Modal
):

    def __init__(
        self,
        ticket_data
    ):

        language = get_ticket_language(
            ticket_data
        )

        self.language = language

        data = TEXT[
            language
        ]

        super().__init__(
            title=data[
                "close_modal_title"
            ]
        )

        self.reason = discord.ui.TextInput(
            label=data[
                "close_reason_label"
            ],
            placeholder=data[
                "close_reason_placeholder"
            ],
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=3,
            max_length=500
        )

        self.add_item(
            self.reason
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        guild = interaction.guild

        channel = interaction.channel

        if guild is None or channel is None:

            return

        ticket_data = get_ticket_data(
            guild.id,
            channel.id
        )

        if not ticket_data:

            await interaction.response.send_message(
                t(
                    interaction.user.id,
                    "invalid_ticket"
                ),
                ephemeral=True
            )

            return

        owner_id = int(
            ticket_data[
                "user_id"
            ]
        )

        language = get_ticket_language(
            ticket_data
        )

        data = TEXT[
            language
        ]

        # =============================================
        # PERMISSION
        # =============================================

        if (
            interaction.user.id != owner_id
            and not is_staff(
                interaction.user
            )
        ):

            await interaction.response.send_message(
                data[
                    "no_close_permission"
                ],
                ephemeral=True
            )

            return

        # =============================================
        # ALREADY CLOSED
        # =============================================

        if ticket_data.get(
            "status"
        ) == "closed":

            await interaction.response.send_message(
                data[
                    "already_closed"
                ],
                ephemeral=True
            )

            return

        reason = self.reason.value.strip()

        if not reason:

            await interaction.response.send_message(
                data[
                    "close_reason_required"
                ],
                ephemeral=True
            )

            return

        # =============================================
        # BLOCK CUSTOMER
        # =============================================

        owner = await get_guild_member(
            guild,
            owner_id
        )

        if owner:

            try:

                await channel.set_permissions(
                    owner,
                    view_channel=True,
                    send_messages=False,
                    read_message_history=True,
                    attach_files=False
                )

            except Exception as e:

                print(
                    f"[TICKET] Permission error: {e}"
                )

        # =============================================
        # SAVE
        # =============================================

        closed_at = (
            discord.utils.utcnow()
            .isoformat()
        )

        update_ticket(
            guild.id,
            channel.id,

            status="closed",

            closed_at=closed_at,

            closed_by=interaction.user.id,

            close_reason=reason
        )

        ticket_data = get_ticket_data(
            guild.id,
            channel.id
        )

        # =============================================
        # RENAME
        # =============================================

        try:

            if not channel.name.startswith(
                "closed-"
            ):

                new_name = channel.name

                if new_name.startswith(
                    "ticket-"
                ):

                    new_name = new_name[
                        len("ticket-"):
                    ]

                await channel.edit(
                    name=(
                        "closed-"
                        + new_name
                    )
                )

        except Exception as e:

            print(
                f"[TICKET] Rename error: {e}"
            )

        # =============================================
        # SEND CLOSED MESSAGE
        # =============================================

        embed = create_closed_embed(
            ticket_data
        )

        await interaction.response.send_message(
            embed=embed,
            view=ClosedTicketView()
        )


# =========================================================
# TICKET CONTROL VIEW
# =========================================================

class TicketControlView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    # =====================================================
    # CLAIM
    # =====================================================

    @discord.ui.button(
        label="🙋 Claim",
        style=discord.ButtonStyle.primary,
        custom_id="lemon_cookie_claim_ticket"
    )
    async def claim_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        guild = interaction.guild

        channel = interaction.channel

        if guild is None or channel is None:

            return

        ticket_data = get_ticket_data(
            guild.id,
            channel.id
        )

        if not ticket_data:

            await interaction.response.send_message(
                t(
                    interaction.user.id,
                    "claim_not_found"
                ),
                ephemeral=True
            )

            return

        language = get_ticket_language(
            ticket_data,
            interaction.user.id
        )

        data = TEXT[
            language
        ]

        if not is_staff(
            interaction.user
        ):

            await interaction.response.send_message(
                data[
                    "claim_not_staff"
                ],
                ephemeral=True
            )

            return

        if ticket_data.get(
            "status"
        ) != "open":

            await interaction.response.send_message(
                data[
                    "claim_closed"
                ],
                ephemeral=True
            )

            return

        current_claim = ticket_data.get(
            "claimed_by"
        )

        if current_claim:

            if int(
                current_claim
            ) == interaction.user.id:

                await interaction.response.send_message(
                    data[
                        "claim_self"
                    ],
                    ephemeral=True
                )

            else:

                await interaction.response.send_message(
                    data[
                        "claim_other"
                    ].format(
                        staff=f"<@{current_claim}>"
                    ),
                    ephemeral=True
                )

            return

        update_ticket(
            guild.id,
            channel.id,
            claimed_by=interaction.user.id
        )

        await interaction.response.send_message(
            data[
                "claimed_message"
            ].format(
                staff=interaction.user.mention
            )
        )

    # =====================================================
    # CLOSE
    # =====================================================

    @discord.ui.button(
        label="🔒 Close Ticket",
        style=discord.ButtonStyle.secondary,
        custom_id="lemon_cookie_close_ticket"
    )
    async def close_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        guild = interaction.guild

        channel = interaction.channel

        if guild is None or channel is None:

            return

        ticket_data = get_ticket_data(
            guild.id,
            channel.id
        )

        if not ticket_data:

            await interaction.response.send_message(
                t(
                    interaction.user.id,
                    "invalid_ticket"
                ),
                ephemeral=True
            )

            return

        language = get_ticket_language(
            ticket_data,
            interaction.user.id
        )

        data = TEXT[
            language
        ]

        owner_id = int(
            ticket_data[
                "user_id"
            ]
        )

        if (
            interaction.user.id != owner_id
            and not is_staff(
                interaction.user
            )
        ):

            await interaction.response.send_message(
                data[
                    "no_close_permission"
                ],
                ephemeral=True
            )

            return

        if ticket_data.get(
            "status"
        ) == "closed":

            await interaction.response.send_message(
                data[
                    "already_closed"
                ],
                ephemeral=True
            )

            return

        await interaction.response.send_modal(
            CloseTicketModal(
                ticket_data
            )
        )


# =========================================================
# CLOSED TICKET VIEW
# =========================================================

class ClosedTicketView(
    discord.ui.View
):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    # =====================================================
    # REOPEN
    # =====================================================

    @discord.ui.button(
        label="🔓 Reopen Ticket",
        style=discord.ButtonStyle.success,
        custom_id="lemon_cookie_reopen_ticket"
    )
    async def reopen_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        guild = interaction.guild

        channel = interaction.channel

        if guild is None or channel is None:

            return

        ticket_data = get_ticket_data(
            guild.id,
            channel.id
        )

        if not ticket_data:

            await interaction.response.send_message(
                t(
                    interaction.user.id,
                    "reopen_not_found"
                ),
                ephemeral=True
            )

            return

        language = get_ticket_language(
            ticket_data,
            interaction.user.id
        )

        data = TEXT[
            language
        ]

        if not is_staff(
            interaction.user
        ):

            await interaction.response.send_message(
                data[
                    "reopen_not_staff"
                ],
                ephemeral=True
            )

            return

        owner_id = int(
            ticket_data[
                "user_id"
            ]
        )

        owner = await get_guild_member(
            guild,
            owner_id
        )

        if owner:

            try:

                await channel.set_permissions(
                    owner,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True
                )

            except Exception as e:

                print(
                    f"[TICKET] Reopen permission error: {e}"
                )

        update_ticket(
            guild.id,
            channel.id,

            status="open",

            closed_at=None,

            closed_by=None,

            close_reason=None
        )

        # =============================================
        # RENAME
        # =============================================

        try:

            new_name = channel.name

            if new_name.startswith(
                "closed-"
            ):

                new_name = new_name[
                    len("closed-"):
                ]

            if not new_name.startswith(
                "ticket-"
            ):

                new_name = (
                    "ticket-"
                    + new_name
                )

            await channel.edit(
                name=new_name
            )

        except Exception as e:

            print(
                f"[TICKET] Rename error: {e}"
            )

        embed = discord.Embed(
            title=data[
                "reopened_title"
            ],
            description=data[
                "reopened_description"
            ].format(
                customer=f"<@{owner_id}>"
            ),
            color=discord.Color.green()
        )

        embed.set_footer(
            text="Lemon Service Support"
        )

        await interaction.response.send_message(
            embed=embed,
            view=TicketControlView()
        )

    # =====================================================
    # DELETE
    # =====================================================

    @discord.ui.button(
        label="🗑️ Delete Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="lemon_cookie_delete_ticket"
    )
    async def delete_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        ticket_data = get_ticket_data(
            interaction.guild.id,
            interaction.channel.id
        )

        language = get_ticket_language(
            ticket_data,
            interaction.user.id
        )

        data = TEXT[
            language
        ]

        if not is_staff(
            interaction.user
        ):

            await interaction.response.send_message(
                data[
                    "delete_not_staff"
                ],
                ephemeral=True
            )

            return

        embed = discord.Embed(
            title=data[
                "delete_title"
            ],
            description=data[
                "delete_description"
            ],
            color=discord.Color.red()
        )

        embed.set_footer(
            text="Lemon Service Support"
        )

        await interaction.response.send_message(
            embed=embed,
            view=DeleteConfirmView(
                interaction.channel.id
            ),
            ephemeral=True
        )


# =========================================================
# TICKET INFO
# =========================================================

@bot.tree.command(
    name="ticketinfo",
    description="Xem thông tin ticket hiện tại"
)
async def ticketinfo(
    interaction: discord.Interaction
):

    if interaction.guild is None:

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "only_server"
            ),
            ephemeral=True
        )

        return

    channel = interaction.channel

    if channel is None:

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "not_ticket"
            ),
            ephemeral=True
        )

        return

    ticket_data = get_ticket_data(
        interaction.guild.id,
        channel.id
    )

    if not ticket_data:

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "not_ticket"
            ),
            ephemeral=True
        )

        return

    owner_id = int(
        ticket_data[
            "user_id"
        ]
    )

    if (
        interaction.user.id != owner_id
        and not is_staff(
            interaction.user
        )
    ):

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "ticket_info_no_permission"
            ),
            ephemeral=True
        )

        return

    language = get_ticket_language(
        ticket_data,
        interaction.user.id
    )

    data = TEXT[
        language
    ]

    # =============================================
    # STATUS
    # =============================================

    if ticket_data.get(
        "status"
    ) == "open":

        status_text = data[
            "open"
        ]

    else:

        status_text = data[
            "closed"
        ]

    # =============================================
    # CLAIM
    # =============================================

    claimed_by = ticket_data.get(
        "claimed_by"
    )

    if claimed_by:

        claimed_text = (
            f"<@{claimed_by}>"
        )

    else:

        claimed_text = data[
            "not_claimed"
        ]

    # =============================================
    # EMBED
    # =============================================

    embed = discord.Embed(
        title=data[
            "ticket_info"
        ],
        color=BOT_COLOR
    )

    embed.add_field(
        name=data[
            "ticket_channel"
        ],
        value=channel.mention,
        inline=True
    )

    embed.add_field(
        name=data[
            "customer"
        ],
        value=f"<@{owner_id}>",
        inline=True
    )

    embed.add_field(
        name=data[
            "status"
        ],
        value=status_text,
        inline=True
    )

    embed.add_field(
        name=data[
            "claimed_by"
        ],
        value=claimed_text,
        inline=True
    )

    embed.add_field(
        name=data[
            "created"
        ],
        value=format_timestamp(
            ticket_data.get(
                "created_at",
                ""
            )
        ),
        inline=True
    )

    closed_at = ticket_data.get(
        "closed_at"
    )

    embed.add_field(
        name=data[
            "closed_at"
        ],
        value=(
            format_timestamp(
                closed_at
            )
            if closed_at
            else data[
                "not_closed"
            ]
        ),
        inline=True
    )

    close_reason = ticket_data.get(
        "close_reason"
    )

    if close_reason:

        embed.add_field(
            name=data[
                "close_reason"
            ],
            value=close_reason,
            inline=False
        )

    closed_by = ticket_data.get(
        "closed_by"
    )

    if closed_by:

        embed.add_field(
            name=data[
                "closed_by"
            ],
            value=f"<@{closed_by}>",
            inline=True
        )

    embed.set_footer(
        text="Lemon Service Support"
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# TICKET PANEL
# =========================================================

@bot.tree.command(
    name="ticketpanel",
    description="Gửi panel tạo ticket Lemon Service"
)
async def ticketpanel(
    interaction: discord.Interaction
):

    if interaction.guild is None:

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "only_server"
            ),
            ephemeral=True
        )

        return

    if not is_staff(
        interaction.user
    ):

        await interaction.response.send_message(
            t(
                interaction.user.id,
                "no_panel_permission"
            ),
            ephemeral=True
        )

        return

    embed = create_panel_embed(
        interaction.user.id
    )

    await interaction.channel.send(
        embed=embed,
        view=TicketPanelView()
    )

    await interaction.response.send_message(
        t(
            interaction.user.id,
            "panel_sent"
        ),
        ephemeral=True
    )


# =========================================================
# VERIFY + ANNOUNCEMENT SYSTEM (merged from the verify/announce bot)
# =========================================================

# =========================================================
# ANNOUNCEMENT / VERIFY CONFIG
# =========================================================

ANNOUNCE_CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


# =========================================================
# ADMIN USER IDS
# =========================================================

# Administrator IDs are configured in operations.json, not in application source.


# =========================================================
# VERIFIED ROLE ID
# =========================================================

VERIFIED_ROLE_ID = 1551567510334214259


# =========================================================
# COLORS
# =========================================================

MAIN_COLOR = discord.Color.from_rgb(
    255,
    193,
    7
)


# =========================================================
# CONFIG
# =========================================================

def announce_load_config():
    return load_json(ANNOUNCE_CONFIG_FILE, {})


def announce_save_config(config):
    save_json(ANNOUNCE_CONFIG_FILE, config)



# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(
    interaction: discord.Interaction
) -> bool:

    return can_manage(interaction)


# =========================================================
# SPLIT LONG CONTENT
# =========================================================

def split_text(
    text,
    max_length=3800
):

    if not text:
        return [""]

    paragraphs = text.split("\n")

    pages = []
    current = ""

    for paragraph in paragraphs:

        test = (
            current + "\n" + paragraph
            if current
            else paragraph
        )

        if len(test) <= max_length:

            current = test
            continue

        if current:
            pages.append(current)

        while len(paragraph) > max_length:

            pages.append(
                paragraph[:max_length]
            )

            paragraph = paragraph[max_length:]

        current = paragraph

    if current:
        pages.append(current)

    return pages or [""]


# =========================================================
# VERIFY VIEW
# =========================================================

class VerifyView(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=None
        )

    @discord.ui.button(
        label="Verify",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="lemon_cookie_verify"
    )
    async def verify_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ This button can only be used inside a server.",
                ephemeral=True
            )

            return

        guild = interaction.guild

        role = guild.get_role(
            VERIFIED_ROLE_ID
        )

        if role is None:

            await interaction.response.send_message(
                "❌ The Verified role could not be found.\n\n"
                "Please contact an administrator.",
                ephemeral=True
            )

            return

        if role in interaction.user.roles:

            await interaction.response.send_message(
                "✅ You are already verified.",
                ephemeral=True
            )

            return

        if guild.me is not None:

            if role >= guild.me.top_role:

                await interaction.response.send_message(
                    "❌ I cannot give you the Verified role.\n\n"
                    "Please move the bot's role above "
                    "the Verified role.",
                    ephemeral=True
                )

                return

        try:

            await interaction.user.add_roles(
                role,
                reason="LEMON SERVICE verification"
            )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ I don't have permission to give "
                "the Verified role.",
                ephemeral=True
            )

            return

        except Exception as error:

            print(
                f"[VERIFY ERROR] {error}"
            )

            await interaction.response.send_message(
                "❌ Verification failed.\n"
                "Please contact Staff.",
                ephemeral=True
            )

            return

        embed = discord.Embed(
            title="✅ Verification Successful",
            description=(
                f"Welcome to **{guild.name}**, "
                f"{interaction.user.mention}!\n\n"
                f"You have received the {role.mention} role."
            ),
            color=discord.Color.green()
        )

        if guild.icon:

            embed.set_thumbnail(
                url=guild.icon.url
            )

        embed.set_footer(
            text="🍋 LEMON SERVICE • Verification System"
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


# =========================================================
# ANNOUNCEMENT VIEW
# =========================================================

class AnnouncementView(discord.ui.View):

    def __init__(
        self,
        title,
        vietnamese,
        english,
        server_name,
        icon_url=None
    ):

        super().__init__(
            timeout=None
        )

        self.title = title
        self.server_name = server_name
        self.icon_url = icon_url

        self.vn_pages = split_text(
            vietnamese
        )

        self.en_pages = split_text(
            english
        )

        self.language = "vn"
        self.page = 0

        self.update_buttons()

    # =====================================================
    # CURRENT PAGES
    # =====================================================

    def current_pages(self):

        if self.language == "en":
            return self.en_pages

        return self.vn_pages

    # =====================================================
    # CREATE EMBED
    # =====================================================

    def create_embed(self):

        pages = self.current_pages()

        if not pages:
            pages = [""]

        if self.page >= len(pages):
            self.page = 0

        content = pages[self.page]

        if self.language == "en":

            embed = discord.Embed(
                title=f"🇺🇸 {self.title}",
                description=content,
                color=MAIN_COLOR,
                timestamp=discord.utils.utcnow()
            )

            footer = (
                f"🍋 {self.server_name} • "
                f"English • "
                f"Page {self.page + 1}/{len(pages)}"
            )

        else:

            embed = discord.Embed(
                title=f"🇻🇳 {self.title}",
                description=content,
                color=MAIN_COLOR,
                timestamp=discord.utils.utcnow()
            )

            footer = (
                f"🍋 {self.server_name} • "
                f"Vietnamese • "
                f"Page {self.page + 1}/{len(pages)}"
            )

        if self.icon_url:

            embed.set_thumbnail(
                url=self.icon_url
            )

        embed.set_footer(
            text=footer
        )

        return embed

    # =====================================================
    # UPDATE BUTTONS
    # =====================================================

    def update_buttons(self):

        pages = self.current_pages()

        self.previous_button.disabled = (
            self.page <= 0
        )

        self.next_button.disabled = (
            self.page >= len(pages) - 1
        )

        if self.language == "vn":

            self.vietnamese_button.disabled = True
            self.english_button.disabled = False

        else:

            self.vietnamese_button.disabled = False
            self.english_button.disabled = True

    # =====================================================
    # VIETNAMESE
    # =====================================================

    @discord.ui.button(
        label="Vietnamese",
        emoji="🇻🇳",
        style=discord.ButtonStyle.secondary,
        custom_id="announcement_vietnamese"
    )
    async def vietnamese_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        self.language = "vn"
        self.page = 0

        self.update_buttons()

        await interaction.response.edit_message(
            embed=self.create_embed(),
            view=self
        )

    # =====================================================
    # ENGLISH
    # =====================================================

    @discord.ui.button(
        label="English",
        emoji="🇺🇸",
        style=discord.ButtonStyle.primary,
        custom_id="announcement_english"
    )
    async def english_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        self.language = "en"
        self.page = 0

        self.update_buttons()

        await interaction.response.edit_message(
            embed=self.create_embed(),
            view=self
        )

    # =====================================================
    # PREVIOUS
    # =====================================================

    @discord.ui.button(
        label="Previous",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
        custom_id="announcement_previous"
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        if self.page > 0:
            self.page -= 1

        self.update_buttons()

        await interaction.response.edit_message(
            embed=self.create_embed(),
            view=self
        )

    # =====================================================
    # NEXT
    # =====================================================

    @discord.ui.button(
        label="Next",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
        custom_id="announcement_next"
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        pages = self.current_pages()

        if self.page < len(pages) - 1:
            self.page += 1

        self.update_buttons()

        await interaction.response.edit_message(
            embed=self.create_embed(),
            view=self
        )

    # =====================================================
    # EDIT
    # =====================================================

    @discord.ui.button(
        label="Edit",
        emoji="✏️",
        style=discord.ButtonStyle.primary,
        custom_id="announcement_edit"
    )
    async def edit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        if not is_admin(interaction):

            await interaction.response.send_message(
                "❌ Only administrators can edit announcements.",
                ephemeral=True
            )

            return

        await interaction.response.send_modal(
            EditAnnouncementModal(self)
        )


# =========================================================
# EDIT ANNOUNCEMENT MODAL
# =========================================================

class EditAnnouncementModal(
    discord.ui.Modal
):

    def __init__(
        self,
        view: AnnouncementView
    ):

        super().__init__(
            title="✏️ Edit Announcement"
        )

        self.view = view

        # -------------------------------------------------
        # TITLE
        # -------------------------------------------------

        self.title_input = discord.ui.TextInput(
            label="📝 Title",
            placeholder="Enter announcement title...",
            required=True,
            max_length=256,
            default=view.title
        )

        # -------------------------------------------------
        # VIETNAMESE
        # -------------------------------------------------

        self.vn_part1 = discord.ui.TextInput(
            label="🇻🇳 Vietnamese - Part 1",
            placeholder="Paste Vietnamese content...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000
        )

        self.vn_part2 = discord.ui.TextInput(
            label="🇻🇳 Vietnamese - Part 2",
            placeholder="Paste more Vietnamese content...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000
        )

        # -------------------------------------------------
        # ENGLISH
        # -------------------------------------------------

        self.en_part1 = discord.ui.TextInput(
            label="🇺🇸 English - Part 1",
            placeholder="Paste English content...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000
        )

        self.en_part2 = discord.ui.TextInput(
            label="🇺🇸 English - Part 2",
            placeholder="Paste more English content...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000
        )

        self.add_item(
            self.title_input
        )

        self.add_item(
            self.vn_part1
        )

        self.add_item(
            self.vn_part2
        )

        self.add_item(
            self.en_part1
        )

        self.add_item(
            self.en_part2
        )

    # =====================================================
    # SUBMIT
    # =====================================================

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if not await ensure_allowed_interaction(interaction):
            return False
        if not is_admin(interaction):

            await interaction.response.send_message(
                "❌ Only administrators can edit announcements.",
                ephemeral=True
            )

            return

        vietnamese = (
            self.vn_part1.value.strip()
            + "\n"
            + self.vn_part2.value.strip()
        ).strip()

        english = (
            self.en_part1.value.strip()
            + "\n"
            + self.en_part2.value.strip()
        ).strip()

        if not vietnamese and not english:

            await interaction.response.send_message(
                "❌ You must provide at least one language.",
                ephemeral=True
            )

            return

        self.view.title = (
            self.title_input.value
        )

        self.view.vn_pages = split_text(
            vietnamese
        )

        self.view.en_pages = split_text(
            english
        )

        self.view.language = "vn"
        self.view.page = 0

        self.view.update_buttons()

        try:

            await interaction.response.edit_message(
                embed=self.view.create_embed(),
                view=self.view
            )

        except Exception as error:

            print(
                f"[EDIT ERROR] {error}"
            )

            if not interaction.response.is_done():

                await interaction.response.send_message(
                    "❌ Failed to edit announcement.",
                    ephemeral=True
                )


# =========================================================
# ANNOUNCEMENT MODAL
# =========================================================

class AnnouncementModal(
    discord.ui.Modal
):

    def __init__(
        self,
        title,
        mention_type
    ):

        super().__init__(
            title="🍋 Create Announcement"
        )

        self.announcement_title = title
        self.mention_type = mention_type

        self.vn_part1 = discord.ui.TextInput(
            label="🇻🇳 Vietnamese - Part 1",
            placeholder="Paste Vietnamese content...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000
        )

        self.vn_part2 = discord.ui.TextInput(
            label="🇻🇳 Vietnamese - Part 2",
            placeholder="Paste more Vietnamese content...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000
        )

        self.en_part1 = discord.ui.TextInput(
            label="🇺🇸 English - Part 1",
            placeholder="Paste English content...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000
        )

        self.en_part2 = discord.ui.TextInput(
            label="🇺🇸 English - Part 2",
            placeholder="Paste more English content...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000
        )

        self.add_item(
            self.vn_part1
        )

        self.add_item(
            self.vn_part2
        )

        self.add_item(
            self.en_part1
        )

        self.add_item(
            self.en_part2
        )

    # =====================================================
    # SUBMIT
    # =====================================================

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):

        if not can_manage(interaction):
            await interaction.response.send_message('❌ Bạn không có quyền đăng thông báo.', ephemeral=True)
            return
        if not await ensure_allowed_interaction(interaction):
            return False
        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ This command can only be used inside a server.",
                ephemeral=True
            )

            return

        config = announce_load_config()

        guild_id = str(
            interaction.guild.id
        )

        if guild_id not in config:

            await interaction.response.send_message(
                "❌ No announcement channel has been configured.\n\n"
                "Use `/setchannel` first.",
                ephemeral=True
            )

            return

        channel_id = config[guild_id].get(
            "announcement_channel"
        )

        channel = interaction.guild.get_channel(
            channel_id
        )

        if channel is None:

            try:

                channel = await bot.fetch_channel(
                    channel_id
                )

            except Exception:

                await interaction.response.send_message(
                    "❌ The configured announcement channel "
                    "could not be found.",
                    ephemeral=True
                )

                return

        if getattr(getattr(channel, 'guild', None), 'id', None) != interaction.guild.id:
            await interaction.response.send_message('❌ Kênh thông báo không thuộc server này.', ephemeral=True)
            return

        vietnamese = (
            self.vn_part1.value.strip()
            + "\n"
            + self.vn_part2.value.strip()
        ).strip()

        english = (
            self.en_part1.value.strip()
            + "\n"
            + self.en_part2.value.strip()
        ).strip()

        if not vietnamese and not english:

            await interaction.response.send_message(
                "❌ You must provide at least one "
                "announcement content.",
                ephemeral=True
            )

            return

        icon_url = None

        if interaction.guild.icon:

            icon_url = interaction.guild.icon.url

        view = AnnouncementView(
            title=self.announcement_title,
            vietnamese=vietnamese,
            english=english,
            server_name=interaction.guild.name,
            icon_url=icon_url
        )

        embed = view.create_embed()

        content = None

        if self.mention_type == "everyone":

            content = "@everyone"

        elif self.mention_type == "here":

            content = "@here"

        allowed_mentions = discord.AllowedMentions(
            everyone=True
        )

        try:

            await channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=allowed_mentions
            )

        except discord.Forbidden:

            await interaction.response.send_message(
                "❌ I don't have permission to send "
                "messages in the announcement channel.",
                ephemeral=True
            )

            return

        except Exception as error:

            print(
                f"[ANNOUNCEMENT ERROR] {error}"
            )

            await interaction.response.send_message(
                "❌ Failed to send the announcement.",
                ephemeral=True
            )

            return

        success_embed = discord.Embed(
            title="✅ Announcement Sent",
            description=(
                f"Your announcement has been sent to "
                f"{channel.mention}."
            ),
            color=discord.Color.green()
        )

        await interaction.response.send_message(
            embed=success_embed,
            ephemeral=True
        )


# =========================================================
# /setchannel
# =========================================================

@bot.tree.command(
    name="setchannel",
    description="Set the announcement channel."
)
@app_commands.describe(
    channel="The announcement channel."
)
async def setchannel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "❌ You are not authorized to use this command.",
            ephemeral=True
        )

        return

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ This command can only be used inside a server.",
            ephemeral=True
        )

        return

    config = announce_load_config()

    guild_id = str(
        interaction.guild.id
    )

    config[guild_id] = {
        "announcement_channel": channel.id
    }

    announce_save_config(config)

    embed = discord.Embed(
        title="✅ Announcement Channel Updated",
        description=(
            f"Announcements will now be sent to "
            f"{channel.mention}."
        ),
        color=discord.Color.green()
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# =========================================================
# /announce
# =========================================================

@bot.tree.command(
    name="announce",
    description="Create a new announcement."
)
@app_commands.describe(
    title="Announcement title.",
    mention="Choose who should be mentioned."
)
@app_commands.choices(
    mention=[
        app_commands.Choice(
            name="No Mention",
            value="none"
        ),
        app_commands.Choice(
            name="@everyone",
            value="everyone"
        ),
        app_commands.Choice(
            name="@here",
            value="here"
        )
    ]
)
async def announce(
    interaction: discord.Interaction,
    title: str,
    mention: app_commands.Choice[str]
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "❌ You are not authorized to use this command.",
            ephemeral=True
        )

        return

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ This command can only be used inside a server.",
            ephemeral=True
        )

        return

    config = announce_load_config()

    guild_id = str(
        interaction.guild.id
    )

    if guild_id not in config:

        await interaction.response.send_message(
            "❌ No announcement channel has been configured.\n\n"
            "Please use `/setchannel` first.",
            ephemeral=True
        )

        return

    await interaction.response.send_modal(
        AnnouncementModal(
            title=title,
            mention_type=mention.value
        )
    )


# =========================================================
# /verify
# =========================================================

@bot.tree.command(
    name="verify",
    description="Create the server verification panel."
)
async def verify(
    interaction: discord.Interaction
):

    if not is_admin(interaction):

        await interaction.response.send_message(
            "❌ You are not authorized to use this command.",
            ephemeral=True
        )

        return

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ This command can only be used inside a server.",
            ephemeral=True
        )

        return

    guild = interaction.guild

    role = guild.get_role(
        VERIFIED_ROLE_ID
    )

    if role is None:

        await interaction.response.send_message(
            "❌ Verified role not found.\n\n"
            "Please set VERIFIED_ROLE_ID correctly.",
            ephemeral=True
        )

        return

    if guild.me is not None:

        if role >= guild.me.top_role:

            await interaction.response.send_message(
                "❌ The Verified role must be below "
                "the bot's highest role.",
                ephemeral=True
            )

            return

    embed = discord.Embed(
        title="🍋 LEMON SERVICE",
        description=(
            "## ✅ SERVER VERIFICATION\n\n"
            "Welcome to **LEMON SERVICE**!\n\n"
            "Click the button below to verify yourself "
            "and gain access to the server.\n\n"
            "> 🔐 **Verification Required**\n"
            "> Please verify your account before "
            "accessing verified channels."
        ),
        color=MAIN_COLOR
    )

    if guild.icon:

        embed.set_thumbnail(
            url=guild.icon.url
        )

    embed.add_field(
        name="🔐 Verification",
        value=(
            "Click **✅ Verify** below to receive "
            f"the {role.mention} role."
        ),
        inline=False
    )

    embed.add_field(
        name="📋 Information",
        value=(
            "By verifying, you confirm that you agree "
            "to follow the rules and Terms of Service "
            "of **LEMON SERVICE**."
        ),
        inline=False
    )

    embed.set_footer(
        text="🍋 LEMON SERVICE • Verification System"
    )

    try:

        await interaction.channel.send(
            embed=embed,
            view=VerifyView()
        )

    except discord.Forbidden:

        await interaction.response.send_message(
            "❌ I don't have permission to send "
            "messages in this channel.",
            ephemeral=True
        )

        return

    except Exception as error:

        print(
            f"[VERIFY PANEL ERROR] {error}"
        )

        await interaction.response.send_message(
            "❌ Failed to create verification panel.",
            ephemeral=True
        )

        return

    await interaction.response.send_message(
        "✅ Verification panel has been created.",
        ephemeral=True
    )


# Slash commands that belong to the verify/announce system (English error text).
ANNOUNCE_COMMAND_NAMES = {"setchannel", "announce", "verify"}


# =========================================================
# READY
# =========================================================

async def sync_application_commands():
    # Guild scopes contain all commands. The global scope contains DM-only
    # customer commands, so no name is offered twice inside a server.
    synced_scopes = getattr(bot, '_synced_command_scopes', set())
    scopes = [discord.Object(id=gid) for gid in POLICY.config['allowed_guild_ids']] + [None]
    if not POLICY.config['allowed_guild_ids']:
        print('[DISCORD] No authorized server configured. Stop bot and run manage_bot.py configure --guild-id ID --admin-id ID')
    for guild in scopes:
        scope_id = guild.id if guild is not None else 'global'
        if scope_id in synced_scopes:
            continue
        try:
            if guild is not None:
                tree.copy_global_to(guild=guild)
                synced = await tree.sync(guild=guild)
            else:
                originals = tree.get_commands()
                dm_names = {'help', 'profile', 'history', 'order', 'checkwarranty', 'redeliver', 'downloadorder'}
                tree.clear_commands(guild=None)
                try:
                    for command in originals:
                        if command.name in dm_names:
                            private = copy.copy(command)
                            private.allowed_contexts = app_commands.AppCommandContext(guild=False, dm_channel=True, private_channel=False)
                            tree.add_command(private)
                    synced = await tree.sync()
                finally:
                    tree.clear_commands(guild=None)
                    for command in originals:
                        tree.add_command(command)
            names = {command.name for command in synced}
            required = {'pendingtopups', 'reconciletopup', 'ignoretopup', 'botstatus'} if guild else dm_names
            if not required <= names:
                print(f'[DISCORD] Incomplete sync for {scope_id}; missing: {", ".join(sorted(required - names))}')
                continue
            synced_scopes.add(scope_id)
            print(f'[DISCORD] Synced {len(synced)} commands to {scope_id}; ' + ('/pendingtopups registered.' if guild else 'DM-only commands registered.'))
        except Exception as exc:
            print(f'[DISCORD] Command sync failed for {scope_id} ({type(exc).__name__}). Check bot invitation scope applications.commands and server access.')
    bot._synced_command_scopes = synced_scopes


@bot.event
async def on_ready():
    await bootstrap_policy()
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    await sync_application_commands()
    bank_loop.change_interval(seconds=POLICY.config['bank_poll_seconds'])
    crypto_loop.change_interval(seconds=POLICY.config['crypto_poll_seconds'])
    for background in (bank_loop, crypto_loop, maintenance_loop, delivery_retry_loop):
        if not background.is_running():
            background.start()

    # Register persistent shop buttons once.
    # This keeps the stock panel buttons working after bot reconnects/restarts.
    if not getattr(bot, "_shop_view_registered", False):

        bot.add_view(
            ShopPanelView()
        )

        bot._shop_view_registered = True

    # Register the persistent verify button once.
    if not getattr(bot, "_verify_view_registered", False):

        try:

            bot.add_view(VerifyView())

            bot._verify_view_registered = True

        except Exception as e:

            print(f"[BOT] Verify view load error: {e}")

    # Register persistent ticket views once (ticket buttons survive restarts).
    if not getattr(bot, "_ticket_views_registered", False):

        try:

            bot.add_view(TicketPanelView())
            bot.add_view(TicketControlView())
            bot.add_view(ClosedTicketView())
            bot.add_view(LanguageView())

            bot._ticket_views_registered = True

            print("[BOT] Persistent ticket views loaded.")

        except Exception as e:

            print(f"[BOT] Ticket view load error: {e}")

    # Register the language button used by balance-change DMs once.
    if not getattr(bot, "_balance_dm_view_registered", False):

        try:

            bot.add_view(BalanceDMView())

            bot._balance_dm_view_registered = True

        except Exception as e:

            print(f"[BOT] Balance DM view load error: {e}")

    # Remove order files older than 14 days immediately on startup.
    try:
        cleanup_expired_orders()
    except Exception as e:
        print(f"[ORDERS] Startup cleanup error: {e}")

    if not main_loop.is_running():

        main_loop.start()

    if not order_cleanup_loop.is_running():

        order_cleanup_loop.start()


# =========================================================
# ERROR HANDLER
# =========================================================

@tree.error
async def on_app_command_error(
    interaction,
    error
):

    err_lang = get_user_language(interaction.user.id)

    command_name = getattr(
        getattr(interaction, "command", None),
        "name",
        None
    )

    if command_name in ANNOUNCE_COMMAND_NAMES:

        print(
            f"[COMMAND ERROR] {error}"
        )

        message = "❌ An unexpected error occurred."

    elif command_name in TICKET_COMMAND_NAMES:

        print(
            f"[COMMAND ERROR] {error}"
        )

        message = t(
            interaction.user.id,
            "generic_error"
        )

    elif isinstance(
        error,
        app_commands.errors.CheckFailure
    ):

        message = pick(
            err_lang,
            "❌ Server hoặc Discord ID của bạn chưa được cấp quyền trong operations.json.",
            "❌ This server or your Discord ID is not authorized in operations.json."
        )

    else:

        print(
            f"[COMMAND ERROR] {error}"
        )

        message = pick(
            err_lang,
            "❌ Có lỗi xảy ra khi thực hiện command.",
            "❌ An error occurred while running the command."
        )


    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True
            )

    except Exception:

        pass


# =========================================================
# START
# =========================================================

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not BOT_TOKEN:
        raise FileNotFoundError("Set DISCORD_BOT_TOKEN or create input/token.txt")
    try:
        bot.run(BOT_TOKEN, log_handler=None)
    finally:
        STORE.close()


if __name__ == "__main__":
    main()
