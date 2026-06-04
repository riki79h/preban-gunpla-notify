"""
preban.py — プレミアムバンダイ 本日のガンプラ「開始前」商品チェッカー

「予約開始前」「販売開始前」（=サイト分類上の「開始前」）に該当するガンプラ／ガンダム
関連商品を抽出し、商品ページURLを表示する。新着があれば ntfy.sh でプッシュ通知する。

仕組み:
- search.p-bandai.jp の C5=30（「開始前」フィルタ）を叩く。
  ※ p-bandai.jp/brand/... 側は Bot Manager で弾かれるため search サブドメインを使う。
- 取得結果からガンプラ／ガンダム関連商品名のみクライアント側で抽出。
- 商品名に「【抽選販売】」を含むものは「抽選販売 開始前」として分類表示。
- 既通知商品は seen_items.json に記録し、重複通知をスキップする（7日間保持）。
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import date
from pathlib import Path

from playwright.async_api import async_playwright

# ── 設定 ────────────────────────────────────────────────
SEARCH_URL = "https://search.p-bandai.jp/?lang=ja&C5=30&sort=new&n=60"

# 環境変数 NTFY_TOPIC があれば優先（GitHub Actions用）、なければハードコード
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "preban-riki79h")
NTFY_URL   = f"https://ntfy.sh/{NTFY_TOPIC}"

SEEN_FILE  = Path.home() / ".claude/skills/preban/seen_items.json"

# プラモデルのグレード名など（これに一致 → プラモ候補）
GUNPLA_KEYWORDS = [
    "ガンプラ",
    "HG ", "MG ", "RG ", "PG ", "EG ",
    "HGUC", "HGCE", "HGBF", "HGIBO", "HGAC", "HGAW", "HGBD",
    "MGEX", "MGSD",
    "BB戦士", "SDCS",
    "ベストメカコレクション",
    "RE/100",
]

# 非プラモ（アパレル・フィギュア等）の除外キーワード
EXCLUDE_KEYWORDS = [
    "STRICT-G",        # ガンダムブランドアパレル
    "METAL ROBOT魂",   # ダイキャストフィギュア
    "ROBOT魂",         # フィギュア
    "FW GUNDAM",       # GUNDAM FIX FIGURATION（フィギュア）
    "ガンダムアーティファクト",  # ガシャポン系フィギュア
    "ガンダムデカール",          # デカール（模型用シールだがプラモ本体ではない）
    "アロハシャツ", "Tシャツ", "パーカー", "ソックス", "レギンス",
]


# ── ユーティリティ ───────────────────────────────────────

def is_gunpla(name: str) -> bool:
    """プラモデル（ガンプラ）かどうかを判定する。
    グレード名などのキーワードに一致し、かつアパレル・フィギュア等の
    除外キーワードを含まない場合のみ True を返す。"""
    normalized = unicodedata.normalize("NFKC", name)
    if any(kw in normalized for kw in EXCLUDE_KEYWORDS):
        return False
    return any(kw in normalized for kw in GUNPLA_KEYWORDS)


# ── 既通知管理（重複スキップ） ───────────────────────────

def load_seen() -> dict:
    """seen_items.json を読み込む。7日以上前のエントリは自動削除。"""
    if not SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        cutoff = time.time() - 7 * 86400
        return {url: ts for url, ts in data.items() if ts > cutoff}
    except Exception:
        return {}


def save_seen(seen: dict, new_items: list) -> None:
    now = time.time()
    for item in new_items:
        if item["url"]:
            seen[item["url"]] = now
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


# ── ntfy 通知 ────────────────────────────────────────────

def in_notify_hours() -> bool:
    """通知を送ってよい時間帯か判定する（7:00〜23:00 のみ通知）。"""
    hour = time.localtime().tm_hour
    return 7 <= hour < 23


def _send_ntfy(title: str, body: str) -> None:
    """ntfy.sh へ1件送信する共通関数。"""
    try:
        req = urllib.request.Request(
            NTFY_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "shopping",
                "Content-Type": "text/plain; charset=utf-8",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"  [通知エラー] {e}", file=sys.stderr)


def notify(items: list) -> None:
    """新着ガンプラ開始前商品を ntfy.sh 経由でプッシュ通知する。
    深夜（23:00〜7:00）は通知せず、seen_items にも登録しない。"""
    for item in items:
        label = "🎲 抽選" if item["sub_kind"] == "抽選販売" else "🔔 通常"
        time_str  = f"  ⏰ {item['sale_time']}" if item["sale_time"] else ""
        price_str = f"  💴 {item['price']}"     if item["price"]     else ""
        body = f"{label} {item['name']}{time_str}{price_str}"
        if item["url"]:
            body += f"\n{item['url']}"
        kind = "Loterie" if item["sub_kind"] == "抽選販売" else "Normal"
        _send_ntfy(title=f"Preban [{kind}]", body=body)


# ── スクレイピング ───────────────────────────────────────

async def fetch_items(page) -> list:
    await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)
    html = await page.content()

    cards = await page.query_selector_all(".pb25Search-product-list__item")
    if not cards:
        return [{"__debug__": True, "html_snippet": html[:3000]}]

    items = []
    for card in cards:
        try:
            name_el = await card.query_selector(".pb25Search-product-name")
            name = (await name_el.inner_text()).strip() if name_el else ""
            if not name:
                continue

            price_el = await card.query_selector(".pb25Search-product-foot__price")
            price = re.sub(r"\s+", " ", (await price_el.inner_text()).strip()) if price_el else ""

            label_els = await card.query_selector_all(".pb25Search-product-label li")
            labels = [(await li.inner_text()).strip() for li in label_els]

            link_el = await card.query_selector("a[href]")
            url = ""
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    url = href if href.startswith("http") else f"https://p-bandai.jp{href}"

            sale_time = ""
            for lab in labels:
                if re.search(r"\d{1,2}月", lab) or "発送" in lab:
                    sale_time = lab
                    break

            sub_kind = "抽選販売" if "【抽選販売】" in name or "抽選販売" in name else "通常販売"

            items.append({
                "name": name,
                "labels": labels,
                "sub_kind": sub_kind,
                "sale_time": sale_time,
                "price": price,
                "url": url,
            })
        except Exception:
            continue

    return items


# ── 表示 ────────────────────────────────────────────────

def print_section(items: list) -> None:
    if not items:
        print("  （該当なし）")
        return
    for item in items:
        time_str  = f"  ⏰ {item['sale_time']}" if item["sale_time"] else ""
        price_str = f"  💴 {item['price']}"     if item["price"]     else ""
        print(f"  ・ {item['name']}{time_str}{price_str}")
        if item["url"]:
            print(f"     🔗 {item['url']}")


# ── メイン ───────────────────────────────────────────────

async def main(morning: bool = False):
    today = date.today().strftime("%Y年%m月%d日")
    print(f"\n{'='*50}")
    print(f"  🤖 プレミアムバンダイ ガンプラ 開始前チェック")
    print(f"  📅 {today}")
    print(f"{'='*50}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ja-JP",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        print("🔍 「開始前」商品（C5=30）を取得中...\n")
        # ネットワークエラー時は2分間隔で最大3回リトライ
        MAX_RETRY = 3
        RETRY_WAIT = 120  # 秒
        items = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                items = await fetch_items(page)
                break
            except Exception as e:
                err_str = str(e)
                is_network_err = any(kw in err_str for kw in [
                    "ERR_INTERNET_DISCONNECTED", "ERR_NAME_NOT_RESOLVED",
                    "ERR_CONNECTION_REFUSED", "net::", "timeout",
                ])
                if is_network_err and attempt < MAX_RETRY:
                    print(f"  ⚠️  ネットワークエラー（試行 {attempt}/{MAX_RETRY}）: {e}")
                    print(f"  ⏳ {RETRY_WAIT}秒後にリトライします...")
                    await asyncio.sleep(RETRY_WAIT)
                else:
                    print(f"❌ 取得エラー（試行 {attempt}/{MAX_RETRY}）: {e}")
                    await browser.close()
                    return
        await browser.close()

    if items and items[0].get("__debug__"):
        print("⚠️  商品カードのセレクタが見つかりませんでした。")
        print(items[0]["html_snippet"])
        return

    if not items:
        print("⚠️  開始前商品は0件でした。")
        return

    gunpla  = [i for i in items if is_gunpla(i["name"])]
    chusen  = [i for i in gunpla if i["sub_kind"] == "抽選販売"]
    normal  = [i for i in gunpla if i["sub_kind"] == "通常販売"]
    others  = [i for i in items  if not is_gunpla(i["name"])]

    print(f"📦 開始前商品 全{len(items)}件 / ガンプラ関連 {len(gunpla)}件\n")

    print("🎯 ガンプラ「開始前」商品（予約開始前 / 販売開始前）")
    print("-" * 50)
    if not gunpla:
        print("  （該当なし）")
    else:
        print("\n🔔 通常販売 開始前")
        print("-" * 40)
        print_section(normal)
        print("\n🎲 抽選販売 開始前")
        print("-" * 40)
        print_section(chusen)

    if others:
        print("\nℹ️  参考：ガンプラ以外の開始前商品")
        print("-" * 40)
        print_section(others)

    # ── 通知処理 ─────────────────────────────────────────────
    seen = load_seen()

    if morning:
        # 朝次モード: seen_items を無視して全ガンプラ開始前商品を通知
        # 0件の場合も「対象なし」を通知する
        targets = gunpla
        if targets:
            print(f"\n🌅 【朝次サマリー】開始前ガンプラ {len(targets)} 件を通知中...")
            notify(targets)
            save_seen(seen, targets)
            print("  通知送信完了 ✔")
        else:
            print("\n🌅 【朝次サマリー】本日の開始前ガンプラは0件 → 対象なし通知を送信中...")
            _send_ntfy(
                title="Preban [Morning]",
                body=f"本日の開始前ガンプラ: 0件（対象なし）\n{date.today().strftime('%Y/%m/%d')} 08:45 時点",
            )
            print("  通知送信完了 ✔")
    else:
        # 通常モード: 新着のみ・7:00〜23:00 限定
        new_gunpla = [i for i in gunpla if i["url"] and i["url"] not in seen]
        if new_gunpla:
            if in_notify_hours():
                print(f"\n📲 新着 {len(new_gunpla)} 件を ntfy に通知中...")
                notify(new_gunpla)
                save_seen(seen, new_gunpla)
                print("  通知送信完了 ✔")
            else:
                hour = time.localtime().tm_hour
                print(f"\n🌙 深夜時間帯（{hour:02d}時）のため通知スキップ。朝7時以降に再通知します。")
        else:
            print("\n📲 新着なし（通知スキップ）")

    print(f"\n{'='*50}")
    print("  チェック完了 ✔")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--morning",
        action="store_true",
        help="朝次サマリーモード: seen_items を無視して全件通知する（毎朝8:45定時実行用）",
    )
    parser.add_argument(
        "--seen-file",
        type=str,
        default=None,
        help="seen_items.json のパスを指定（GitHub Actions用）",
    )
    args = parser.parse_args()
    # --seen-file が指定された場合はグローバルの SEEN_FILE を上書き
    if args.seen_file:
        SEEN_FILE = Path(args.seen_file)
    asyncio.run(main(morning=args.morning))
