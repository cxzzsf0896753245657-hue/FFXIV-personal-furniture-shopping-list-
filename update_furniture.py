#!/usr/bin/env python3
"""
FF14 家具資料自動更新腳本
由 GitHub Actions 執行，負責：
1. 從 XIVAPI v2 抓取最新家具清單
2. 比對現有資料，找出新增/異動的家具
3. 下載新圖示 PNG
4. 更新 data/furniture.json 和 data/icon_map.json
"""

import json
import os
import time
import requests
from pathlib import Path

# ── 設定 ──────────────────────────────────────────────
XIVAPI      = "https://v2.xivapi.com/api"
LIMIT       = 100
SLEEP_SEC   = 0.5   # 每次請求間隔（避免被限速）
FORCE_FULL  = os.environ.get("FORCE_FULL", "false").lower() == "true"

DATA_DIR    = Path("data")
ICONS_DIR   = Path("icons")

FURNITURE_IDS = {
    57: "室內家具",
    64: "建設許可",
    65: "屋頂",
    66: "外牆",
    67: "窗戶",
    68: "門",
    69: "屋頂裝飾",
    70: "外牆裝飾",
    71: "門牌",
    72: "圍欄",
    73: "內牆",
    74: "地板",
    75: "天花板燈",
    76: "室外家具",
    77: "桌子",
    78: "桌上擺件",
    79: "壁掛",
    80: "地毯",
    95: "繪畫",
}

# ── 工具函式 ──────────────────────────────────────────
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def fetch_json(url, retries=3):
    for i in range(retries):
        try:
            resp = requests.get(url, timeout=30, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                return resp.json()
            log(f"  HTTP {resp.status_code}: {url}")
        except Exception as e:
            log(f"  請求失敗 ({i+1}/{retries}): {e}")
        time.sleep(2 ** i)
    return None

def build_icon_url(icon_obj):
    if not icon_obj:
        return ""
    path = icon_obj.get("path_hr1") or icon_obj.get("path") or ""
    if not path:
        return ""
    return f"https://v2.xivapi.com/api/asset?path={requests.utils.quote(path)}&format=png"

# ── 步驟1：抓取最新家具清單 ──────────────────────────
def fetch_all_furniture():
    log("開始從 XIVAPI v2 抓取家具清單...")
    furniture = {}
    after = 0
    page  = 0

    while True:
        page += 1
        url = (f"{XIVAPI}/sheet/Item"
               f"?fields={requests.utils.quote('Name,Name@lang(en),ItemUICategory,Icon')}"
               f"&limit={LIMIT}&after={after}&language=ja")

        data = fetch_json(url)
        if not data:
            log(f"  第 {page} 頁請求失敗，跳過")
            after += LIMIT
            if after > 50000:  # Item 表大約 4 萬筆，超過就停
                break
            continue

        rows = data.get("rows", [])
        if not rows:
            log(f"  第 {page} 頁回傳 0 筆，抓取完成")
            break

        for row in rows:
            row_id = row.get("row_id")
            fields = row.get("fields", {})

            # 取類別 ID
            cat_obj = fields.get("ItemUICategory", {})
            if isinstance(cat_obj, dict):
                cat_id = cat_obj.get("value") or cat_obj.get("row_id") or 0
            else:
                cat_id = int(cat_obj or 0)

            if cat_id not in FURNITURE_IDS:
                continue

            name_ja = fields.get("Name", "")
            name_en = fields.get("Name@lang(en)") or name_ja
            icon_obj = fields.get("Icon", {})
            icon_url = build_icon_url(icon_obj)

            if not name_ja or not name_ja.strip():
                continue

            furniture[str(row_id)] = {
                "id":       str(row_id),
                "name_ja":  name_ja,
                "name_en":  name_en,
                "category": FURNITURE_IDS[cat_id],
                "cat_id":   cat_id,
                "icon_url": icon_url,
            }

        last_id = rows[-1].get("row_id", after)
        after = last_id

        if page % 10 == 0:
            log(f"  已掃描 {page} 頁，目前 after={after}，找到 {len(furniture)} 筆家具")

        time.sleep(SLEEP_SEC)

    log(f"抓取完成，共 {len(furniture)} 筆家具")
    return furniture

# ── 步驟2：載入現有資料，找出差異 ────────────────────
def load_existing():
    path = DATA_DIR / "furniture.json"
    if path.exists() and not FORCE_FULL:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        return {item["id"]: item for item in items}
    return {}

def find_diff(existing, latest):
    new_ids     = set(latest.keys()) - set(existing.keys())
    removed_ids = set(existing.keys()) - set(latest.keys())
    changed_ids = {
        k for k in latest
        if k in existing and (
            existing[k].get("name_ja") != latest[k].get("name_ja") or
            existing[k].get("cat_id")  != latest[k].get("cat_id")
        )
    }
    return new_ids, removed_ids, changed_ids

# ── 步驟3：下載新圖示 ─────────────────────────────────
def download_icons(item_ids, furniture):
    ICONS_DIR.mkdir(exist_ok=True)
    downloaded = 0
    skipped    = 0
    failed     = 0

    for item_id in item_ids:
        icon_path = ICONS_DIR / f"{item_id}.png"

        # 已存在就跳過
        if icon_path.exists():
            skipped += 1
            continue

        item     = furniture.get(item_id)
        icon_url = item.get("icon_url") if item else ""

        if not icon_url:
            log(f"  無圖示 URL：{item_id}")
            failed += 1
            continue

        try:
            resp = requests.get(icon_url, timeout=15)
            if resp.status_code == 200:
                icon_path.write_bytes(resp.content)
                downloaded += 1
            else:
                log(f"  下載失敗 {item_id}：HTTP {resp.status_code}")
                failed += 1
        except Exception as e:
            log(f"  下載例外 {item_id}：{e}")
            failed += 1

        time.sleep(0.1)

        if (downloaded + skipped + failed) % 100 == 0:
            log(f"  圖示進度：下載 {downloaded}，跳過 {skipped}，失敗 {failed}")

    log(f"圖示下載完成：下載 {downloaded}，跳過 {skipped}，失敗 {failed}")

# ── 步驟4：儲存更新後的資料 ──────────────────────────
def save_data(furniture):
    DATA_DIR.mkdir(exist_ok=True)

    # furniture.json（陣列格式）
    items = list(furniture.values())
    items.sort(key=lambda x: int(x["id"]))
    with open(DATA_DIR / "furniture.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    log(f"已儲存 data/furniture.json（{len(items)} 筆）")

    # icon_map.json（id → url 對照）
    icon_map = {item["id"]: item["icon_url"] for item in items}
    with open(DATA_DIR / "icon_map.json", "w", encoding="utf-8") as f:
        json.dump(icon_map, f, ensure_ascii=False, indent=2)
    log("已儲存 data/icon_map.json")

# ── 主流程 ────────────────────────────────────────────
def main():
    log("=" * 50)
    log("FF14 家具資料自動更新")
    log(f"模式：{'強制全量更新' if FORCE_FULL else '增量更新'}")
    log("=" * 50)

    # 載入現有資料
    existing = load_existing()
    log(f"現有資料：{len(existing)} 筆")

    # 抓取最新資料
    latest = fetch_all_furniture()

    if not latest:
        log("❌ 未取得任何資料，放棄更新")
        return

    # 比對差異
    new_ids, removed_ids, changed_ids = find_diff(existing, latest)
    log(f"\n差異分析：")
    log(f"  新增：{len(new_ids)} 筆")
    log(f"  移除：{len(removed_ids)} 筆")
    log(f"  異動：{len(changed_ids)} 筆")

    if not new_ids and not removed_ids and not changed_ids:
        log("\n✅ 資料無變化，不需要更新")
        return

    # 下載新增家具的圖示
    if new_ids:
        log(f"\n下載 {len(new_ids)} 個新圖示...")
        download_icons(new_ids, latest)

    # 儲存更新後的資料
    save_data(latest)

    log("\n✅ 全部完成！")
    if new_ids:
        log(f"  新增家具：" + "、".join(
            latest[i]["name_ja"] for i in list(new_ids)[:5]
        ) + ("..." if len(new_ids) > 5 else ""))

if __name__ == "__main__":
    main()
