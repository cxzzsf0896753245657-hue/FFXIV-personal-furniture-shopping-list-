#!/usr/bin/env python3
"""
FF14 家具資料自動更新腳本
用途：從 XIVAPI v2 抓取家具清單與圖示，輸出：
  data/furniture.json   家具清單
  data/icon_map.json    ID→圖示URL
  data/hashes.json      ID→pHash（hex字串，前端直接用，不需在瀏覽器運算）
執行方式：python scripts/update_furniture.py [--force] [--no-icons] [--no-hashes]
"""

import json
import math
import struct
import time
import argparse
import urllib.request
import urllib.error
from pathlib import Path

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ── 設定 ──────────────────────────────────────────────────────────────────────
XIVAPI_BASE = "https://v2.xivapi.com/api"
LANGUAGE = "ja"  # tw 會報錯，改用 ja

FURNITURE_CATEGORY_IDS = {
    57, 64, 65, 66, 67, 68, 69, 70, 71, 72,
    73, 74, 75, 76, 77, 78, 79, 80, 95
}

OUT_DIR    = Path("data")
ICONS_DIR  = Path("icons")
FURNITURE_JSON = OUT_DIR / "furniture.json"
ICON_MAP_JSON  = OUT_DIR / "icon_map.json"
HASHES_JSON    = OUT_DIR / "hashes.json"

# pHash 參數（需與前端 JS 保持一致）
PHASH_SIZE      = 32   # 縮放到 32×32 再做 DCT
PHASH_HASH_SIZE = 8    # 取左上角 8×8 區塊 = 64 bits

FIELDS = "Name,ItemUICategory,Icon"
PAGE_SIZE = 500
RETRY_MAX = 5
RETRY_DELAY = 2      # seconds between retries
REQUEST_DELAY = 0.3  # 禮貌性 API 延遲


# ── HTTP helper ───────────────────────────────────────────────────────────────
def fetch_json(url: str, retries: int = RETRY_MAX) -> dict:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "FFXIV-Furniture-Updater/1.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as e:
            wait = RETRY_DELAY * (2 ** attempt)
            print(f"  [警告] 請求失敗（第{attempt+1}次）：{e}，{wait}s 後重試…")
            time.sleep(wait)
    raise RuntimeError(f"連續 {retries} 次失敗，放棄：{url}")


def fetch_image(url: str, dest: Path, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "FFXIV-Furniture-Updater/1.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                dest.write_bytes(resp.read())
            return True
        except Exception:
            if attempt < retries - 1:
                time.sleep(1)
    return False


# ── pHash 計算（Python / Pillow）─────────────────────────────────────────────
def _build_dct_matrix(n: int) -> list[list[float]]:
    """預建 DCT 係數矩陣，避免重複計算 cos"""
    mat = []
    for u in range(n):
        row = []
        for x in range(n):
            row.append(math.cos((2*x + 1) * u * math.pi / (2*n)))
        mat.append(row)
    return mat

_DCT_MAT = None  # lazy init

def compute_phash(path: Path, size: int = PHASH_SIZE, hash_size: int = PHASH_HASH_SIZE) -> str | None:
    """
    對一張圖計算 pHash，回傳 hex 字串（例如 'a3f2...'，16個字元 = 64 bits）。
    使用 Pillow 做高品質 resize，再跑 2D DCT。
    """
    global _DCT_MAT
    if not HAS_PIL:
        return None
    try:
        img = Image.open(path).convert("L")          # 轉灰階
        img = img.resize((size, size), Image.LANCZOS) # 高品質縮放
        pixels = list(img.getdata())                  # flat list, 0-255

        # 建 DCT 係數矩陣（只建一次）
        if _DCT_MAT is None:
            _DCT_MAT = _build_dct_matrix(size)

        # 2D DCT（行列分離法：先做行 DCT，再做列 DCT）
        # 行 DCT
        row_dct = []
        for row_idx in range(size):
            row = pixels[row_idx*size:(row_idx+1)*size]
            dct_row = []
            for u in range(size):
                val = sum(_DCT_MAT[u][x] * row[x] for x in range(size))
                cu = 1/math.sqrt(2) if u == 0 else 1.0
                dct_row.append(0.5 * cu * val)
            row_dct.append(dct_row)

        # 列 DCT
        dct2d = [[0.0]*size for _ in range(size)]
        for col_idx in range(size):
            col = [row_dct[r][col_idx] for r in range(size)]
            for v in range(size):
                val = sum(_DCT_MAT[v][y] * col[y] for y in range(size))
                cv = 1/math.sqrt(2) if v == 0 else 1.0
                dct2d[v][col_idx] = 0.5 * cv * val

        # 取左上角 hash_size×hash_size（跳過 DC 分量 [0][0]）
        vals = []
        for u in range(hash_size):
            for v in range(hash_size):
                vals.append(dct2d[u][v])

        avg = sum(vals) / len(vals)
        bits = [1 if v > avg else 0 for v in vals]

        # pack 64 bits → 8 bytes → hex
        byte_val = 0
        result_bytes = bytearray()
        for i, bit in enumerate(bits):
            byte_val = (byte_val << 1) | bit
            if (i + 1) % 8 == 0:
                result_bytes.append(byte_val)
                byte_val = 0

        return result_bytes.hex()

    except Exception as e:
        print(f"  [hash 失敗] {path}: {e}")
        return None


# ── 主流程 ────────────────────────────────────────────────────────────────────
def fetch_all_furniture(force: bool = False) -> list[dict]:
    """分頁拉取所有 Item，篩選家具類別。"""
    print("=== 開始抓取家具清單 ===")

    existing = {}
    if not force and FURNITURE_JSON.exists():
        try:
            with open(FURNITURE_JSON, encoding="utf-8") as f:
                loaded = json.load(f)
            # 過濾舊格式（沒有 iconUrl 欄位）：遇到就整批丟棄，強制全量重抓
            valid = [item for item in loaded if "iconUrl" in item]
            if len(valid) < len(loaded):
                print(f"  偵測到舊格式資料（{len(loaded)-len(valid)} 筆缺少 iconUrl），改用全量模式")
            else:
                for item in valid:
                    existing[item["id"]] = item
                print(f"  已有 {len(existing)} 筆資料（增量模式）")
        except Exception:
            print("  現有 JSON 讀取失敗，改用全量模式")

    furniture = dict(existing)
    after = None
    page = 0

    while True:
        page += 1
        url = (
            f"{XIVAPI_BASE}/sheet/Item"
            f"?fields={FIELDS}&language={LANGUAGE}&limit={PAGE_SIZE}"
        )
        if after is not None:
            url += f"&after={after}"

        print(f"  第 {page} 頁（after={after}）…", end=" ", flush=True)
        data = fetch_json(url)
        time.sleep(REQUEST_DELAY)

        rows = data.get("rows", [])
        if not rows:
            print("空，結束分頁")
            break

        added = 0
        for row in rows:
            row_id = row.get("row_id")
            fields = row.get("fields", {})

            cat = fields.get("ItemUICategory", {})
            cat_id = cat.get("value") if isinstance(cat, dict) else None
            if cat_id not in FURNITURE_CATEGORY_IDS:
                continue

            name = fields.get("Name", "").strip()
            if not name:
                continue

            icon_path = fields.get("Icon", "")
            icon_url = (
                f"{XIVAPI_BASE}/asset?path={icon_path}&format=png"
                if icon_path else ""
            )

            furniture[row_id] = {
                "id": row_id,
                "name": name,
                "category": _cat_name(cat_id),
                "categoryId": cat_id,
                "iconUrl": icon_url,
                "iconPath": icon_path,
            }
            added += 1

        last_id = rows[-1].get("row_id")
        print(f"取得 {len(rows)} 筆 → 家具 +{added}（累計 {len(furniture)}）")

        if len(rows) < PAGE_SIZE:
            print("  最後一頁，結束")
            break

        after = last_id

    print(f"\n家具總計：{len(furniture)} 筆")
    return list(furniture.values())


def _cat_name(cat_id: int) -> str:
    MAP = {
        57:'室內家具', 64:'建設許可', 65:'屋頂', 66:'外牆', 67:'窗戶',
        68:'門', 69:'屋頂裝飾', 70:'外牆裝飾', 71:'門牌', 72:'圍欄',
        73:'內牆', 74:'地板', 75:'天花板燈', 76:'室外家具', 77:'桌子',
        78:'桌上擺件', 79:'壁掛', 80:'地毯', 95:'繪畫'
    }
    return MAP.get(cat_id, f"類別{cat_id}")


def download_icons(furniture: list[dict], force: bool = False):
    """下載缺少的圖示到 icons/{id}.png"""
    print("\n=== 下載圖示 ===")
    ICONS_DIR.mkdir(exist_ok=True)

    total = len(furniture)
    ok = skip = fail = 0

    for i, item in enumerate(furniture, 1):
        dest = ICONS_DIR / f"{item['id']}.png"
        if dest.exists() and not force:
            skip += 1
            continue

        url = item.get("iconUrl", "")
        if not url:
            fail += 1
            continue

        success = fetch_image(url, dest)
        time.sleep(0.15)

        if success:
            ok += 1
        else:
            fail += 1
            print(f"  [失敗] ID={item['id']} {item['name']}")

        if i % 100 == 0 or i == total:
            print(f"  進度 {i}/{total}  ✓{ok} ↷{skip} ✗{fail}")

    print(f"\n圖示下載完成：成功 {ok}、跳過 {skip}、失敗 {fail}")


def build_hashes(furniture: list[dict], force: bool = False):
    """
    對 icons/ 裡的每張圖計算 pHash，輸出 data/hashes.json。
    格式：{"物品ID": "hex字串", ...}
    前端直接 fetch 這個 JSON，完全不需要在瀏覽器裡算 hash 或載入圖片。
    """
    if not HAS_PIL:
        print("\n[跳過 hash] 需要安裝 Pillow：pip install Pillow")
        print("  安裝後重新執行即可生成 hashes.json")
        return

    print("\n=== 計算 pHash ===")

    # 載入已有的 hash（增量模式）
    existing_hashes = {}
    if not force and HASHES_JSON.exists():
        try:
            with open(HASHES_JSON, encoding="utf-8") as f:
                existing_hashes = json.load(f)
            print(f"  已有 {len(existing_hashes)} 筆 hash（增量模式）")
        except Exception:
            print("  現有 hashes.json 讀取失敗，重新全算")

    hashes = dict(existing_hashes)
    total = len(furniture)
    ok = skip = fail = 0

    for i, item in enumerate(furniture, 1):
        item_id = str(item["id"])
        icon_path = ICONS_DIR / f"{item['id']}.png"

        # 已有且不強制重算 → 跳過
        if item_id in hashes and not force:
            skip += 1
            continue

        if not icon_path.exists():
            fail += 1
            continue

        h = compute_phash(icon_path)
        if h:
            hashes[item_id] = h
            ok += 1
        else:
            fail += 1

        if i % 200 == 0 or i == total:
            print(f"  進度 {i}/{total}  ✓{ok} ↷{skip} ✗{fail}")

    OUT_DIR.mkdir(exist_ok=True)
    with open(HASHES_JSON, "w", encoding="utf-8") as f:
        json.dump(hashes, f, ensure_ascii=False, separators=(',', ':'))

    size_kb = HASHES_JSON.stat().st_size / 1024
    print(f"\n已寫入 {HASHES_JSON}（{len(hashes)} 筆，{size_kb:.0f} KB）")


def save_outputs(furniture: list[dict]):
    OUT_DIR.mkdir(exist_ok=True)

    with open(FURNITURE_JSON, "w", encoding="utf-8") as f:
        json.dump(furniture, f, ensure_ascii=False, indent=2)
    print(f"已寫入 {FURNITURE_JSON}（{len(furniture)} 筆）")

    icon_map = {str(item["id"]): item.get("iconUrl", "") for item in furniture}
    with open(ICON_MAP_JSON, "w", encoding="utf-8") as f:
        json.dump(icon_map, f, ensure_ascii=False, indent=2)
    print(f"已寫入 {ICON_MAP_JSON}（{len(icon_map)} 筆）")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FF14 家具資料更新")
    parser.add_argument("--force",      action="store_true", help="強制全量重新抓取（含圖示與 hash）")
    parser.add_argument("--no-icons",   action="store_true", help="跳過圖示下載")
    parser.add_argument("--no-hashes",  action="store_true", help="跳過 pHash 計算")
    parser.add_argument("--only-hashes",action="store_true", help="只重算 hash，不抓資料")
    args = parser.parse_args()

    try:
        if args.only_hashes:
            # 只重算 hash（家具清單已存在的情況下使用）
            if not FURNITURE_JSON.exists():
                print("❌ furniture.json 不存在，請先執行完整更新")
                raise SystemExit(1)
            with open(FURNITURE_JSON, encoding="utf-8") as f:
                furniture = json.load(f)
            build_hashes(furniture, force=args.force)
        else:
            furniture = fetch_all_furniture(force=args.force)
            save_outputs(furniture)

            if not args.no_icons:
                download_icons(furniture, force=args.force)

            if not args.no_hashes:
                build_hashes(furniture, force=args.force)

        print("\n✅ 更新完成")
    except Exception as e:
        print(f"\n❌ 更新失敗：{e}")
        raise SystemExit(1)
