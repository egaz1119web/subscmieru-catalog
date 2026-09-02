#!/usr/bin/env python3
"""料金改定を1操作でカタログへ反映する。

やること(全部まとめて、途中で失敗したら何も書かない):
  1. v1/subscription_services.json の価格を新しい値に書き換える
  2. v1/catalog_changes.json に変更履歴を追記する(アプリの「変更のお知らせ」になる)
  3. 両ファイルの version をインクリメントし updatedAt を当日にする
  4. 古い変更履歴を間引く
  5. PR説明文(Markdown)を出力する

入力は JSON 配列。stdin か --input で渡す。1件あたり必要なのは4つだけ:

    [
      {
        "serviceId": "netflix",
        "plan": "広告つきスタンダード",
        "new": 990,
        "source": "https://help.netflix.com/ja/node/24926"
      }
    ]

serviceName / category / domain / currency / cycle / old / kind はカタログ本体から
自動で埋める。手で書かないこと(食い違いの元になる)。

任意で指定できるもの:
  newPlan   プラン名が変わったとき(PLAN_RENAMED になる)
  kind      自動判定を上書きしたいとき
  note      PR説明文に添える補足(適用日など)

使い方:
    python3 tools/add_change.py --dry-run < changes.json   # 確認だけ
    python3 tools/add_change.py < changes.json             # 実際に書き換える
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import date, timedelta
from pathlib import Path

V1 = Path(__file__).resolve().parent.parent / "v1"
CATALOG = V1 / "subscription_services.json"
CHANGES = V1 / "catalog_changes.json"

# since を持たない既存エントリの追加日とみなす日付(version 6 の updatedAt)。
BASELINE_SINCE = "2026-08-29"

KIND_LABEL = {
    "PRICE_UP": "値上げ",
    "PRICE_DOWN": "値下げ",
    "CURRENCY_CHANGED": "通貨変更",
    "CYCLE_CHANGED": "支払い周期変更",
    "PLAN_RENAMED": "プラン名変更",
}
# このスクリプトが扱うのは既存プランの改定のみ。
# プラン追加・提供終了・サービス改名は影響範囲が違うので手作業で行う。
SUPPORTED_KINDS = set(KIND_LABEL)


class InputError(Exception):
    pass


def load(path: Path) -> collections.OrderedDict:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=collections.OrderedDict)


def save(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def money(currency: str, minor: int) -> str:
    if currency == "USD":
        return f"${minor / 100:,.2f}"
    return f"¥{minor:,}"


def resolve(entry: dict, services: list) -> collections.OrderedDict:
    """入力1件を、カタログを引いて完全な変更エントリに膨らませる。"""
    for key in ("serviceId", "plan", "new", "source"):
        if not entry.get(key) and entry.get(key) != 0:
            raise InputError(f"{key} は必須です: {entry}")

    sid = entry["serviceId"]
    service = next((s for s in services if s["id"] == sid), None)
    if service is None:
        raise InputError(f"serviceId {sid!r} がカタログにありません")

    plan_name = entry["plan"]
    plan = next((p for p in service["plans"] if p["name"] == plan_name), None)
    if plan is None:
        available = " / ".join(p["name"] for p in service["plans"])
        raise InputError(
            f"{sid}: プラン {plan_name!r} がありません。存在するのは: {available}"
        )

    new_price = entry["new"]
    if not isinstance(new_price, int) or isinstance(new_price, bool) or new_price < 0:
        raise InputError(f"{sid}/{plan_name}: new は0以上の整数(円 or セント)であること: {new_price!r}")

    old_price = plan["price"]
    currency = plan.get("currency", "JPY")
    cycle = plan["cycle"]
    new_plan_name = entry.get("newPlan")

    kind = entry.get("kind")
    if kind is None:
        if new_plan_name and new_plan_name != plan_name:
            kind = "PLAN_RENAMED"
        elif new_price > old_price:
            kind = "PRICE_UP"
        elif new_price < old_price:
            kind = "PRICE_DOWN"
        else:
            raise InputError(
                f"{sid}/{plan_name}: 価格もプラン名も変わっていません(現在 {old_price})。"
                f"変更が無いなら入力から外してください"
            )
    if kind not in SUPPORTED_KINDS:
        raise InputError(
            f"{sid}/{plan_name}: kind {kind!r} はこのスクリプトの対象外です。"
            f"扱えるのは {' / '.join(sorted(SUPPORTED_KINDS))}"
        )

    change = collections.OrderedDict(
        [
            ("serviceId", sid),
            ("kind", kind),
            ("plan", new_plan_name or plan_name),
            ("old", old_price),
            ("new", new_price),
            ("oldCurrency", currency),
            ("currency", currency),
            ("oldCycle", cycle),
            ("cycle", cycle),
            ("serviceName", service["name"]),
            ("category", service["category"]),
        ]
    )
    if new_plan_name and new_plan_name != plan_name:
        change["oldPlan"] = plan_name
    if service.get("domain"):
        change["domain"] = service["domain"]

    # source と note は配信JSONには入れない(アプリが使わないためファイルを太らせるだけ)。
    # PR説明文の組み立てにだけ使う。
    return change, {
        "source": entry["source"],
        "note": entry.get("note"),
        "oldPlan": plan_name,
        "planRef": plan,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="料金改定をカタログへ反映する")
    ap.add_argument("--input", type=Path, help="変更のJSON配列(省略時は stdin)")
    ap.add_argument("--dry-run", action="store_true", help="書き込まず、結果だけ表示する")
    ap.add_argument("--keep-days", type=int, default=180,
                    help="変更履歴を保持する日数(既定180日)")
    ap.add_argument("--report", type=Path,
                    help="PR説明文の書き出し先(省略時は標準出力)")
    args = ap.parse_args()

    raw = args.input.read_text(encoding="utf-8") if args.input else sys.stdin.read()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR 入力がJSONとして読めません: {e}", file=sys.stderr)
        return 1
    if not isinstance(entries, list) or not entries:
        print("ERROR 入力は1件以上のJSON配列であること", file=sys.stderr)
        return 1

    catalog = load(CATALOG)
    changes_doc = load(CHANGES)
    services = catalog["services"]

    # 先に全件を検証する。1件でも駄目ならファイルには一切触らない。
    resolved = []
    errors = []
    for entry in entries:
        try:
            resolved.append(resolve(entry, services))
        except InputError as e:
            errors.append(str(e))
    if errors:
        for e in errors:
            print(f"ERROR {e}", file=sys.stderr)
        print(f"\n{len(errors)} 件の入力エラー。ファイルは変更していません。", file=sys.stderr)
        return 1

    seen = set()
    for change, _ in resolved:
        key = (change["serviceId"], change.get("oldPlan") or change["plan"])
        if key in seen:
            print(f"ERROR 同じプランを2回指定しています: {key}", file=sys.stderr)
            return 1
        seen.add(key)

    today = date.today().isoformat()
    new_version = catalog["version"] + 1

    # --- 1. カタログ本体へ価格を反映 ---
    for change, meta in resolved:
        meta["planRef"]["price"] = change["new"]
        meta["planRef"]["name"] = change["plan"]

    # --- 2. 変更履歴の追記と間引き ---
    cutoff = (date.today() - timedelta(days=args.keep_days)).isoformat()
    old_changes = changes_doc["changes"]
    kept = [c for c in old_changes if c.get("since", BASELINE_SINCE) >= cutoff]
    pruned = len(old_changes) - len(kept)
    # 同じプランの古い履歴は新しいもので置き換える(未反映の利用者が二重に案内されないように)
    superseded = 0
    filtered = []
    for c in kept:
        if (c["serviceId"], c.get("plan")) in {(ch["serviceId"], ch["plan"]) for ch, _ in resolved}:
            superseded += 1
        else:
            filtered.append(c)
    new_entries = []
    for change, _ in resolved:
        change["since"] = today
        new_entries.append(change)
    changes_doc["changes"] = new_entries + filtered

    # --- 3. version / updatedAt ---
    for doc in (catalog, changes_doc):
        doc["version"] = new_version
        doc["updatedAt"] = today

    # --- 4. PR説明文 ---
    lines = [
        f"## 料金カタログ更新 v{new_version} ({today})",
        "",
        f"配信中の v{new_version - 1} から **{len(resolved)} 件** の料金改定を反映します。",
        "",
        "| サービス | プラン | 変更 | 種別 | ソース |",
        "| --- | --- | --- | --- | --- |",
    ]
    for change, meta in resolved:
        cur = change["currency"]
        plan_cell = change["plan"]
        if change.get("oldPlan"):
            plan_cell = f"{change['oldPlan']} → {change['plan']}"
        if change["old"] == change["new"]:
            # 改名だけで価格が動いていないときに「¥2,000 → ¥2,000」と出さない
            delta = f"{money(cur, change['new'])} (据え置き)"
        else:
            delta = f"{money(cur, change['old'])} → **{money(cur, change['new'])}**"
            if change["old"]:
                pct = (change["new"] - change["old"]) / change["old"] * 100
                delta += f" ({pct:+.1f}%)"
        note = f" {meta['note']}" if meta["note"] else ""
        lines.append(
            f"| {change['serviceName']} | {plan_cell} | {delta} | "
            f"{KIND_LABEL[change['kind']]}{note} | [出典]({meta['source']}) |"
        )
    lines += [
        "",
        "### 履歴の増減",
        "",
        f"- 追加: {len(resolved)} 件",
        f"- 同じプランの古い履歴を置き換え: {superseded} 件",
        f"- {args.keep_days}日より古いエントリを間引き: {pruned} 件",
        f"- 変更履歴の合計: {len(changes_doc['changes'])} 件",
        "",
        "### 影響",
        "",
        "- マージ後、数分で GitHub Pages に反映されます",
        "- 利用者にはアプリを開いたタイミング(最大12時間後)または日次同期で届きます",
        "- 登録済みサブスクの金額は自動では変わりません。"
        "アプリの「変更のお知らせ」から利用者が選んで反映します",
    ]
    report = "\n".join(lines) + "\n"

    if args.dry_run:
        print("--- dry-run: ファイルは変更していません ---\n", file=sys.stderr)
    else:
        save(CATALOG, catalog)
        save(CHANGES, changes_doc)
        print(
            f"更新しました: v{new_version - 1} -> v{new_version} ({today}) / "
            f"変更 {len(resolved)} 件 / 置換 {superseded} 件 / 間引き {pruned} 件",
            file=sys.stderr,
        )

    if args.report and not args.dry_run:
        args.report.write_text(report, encoding="utf-8")
        print(f"PR説明文を書き出しました: {args.report}", file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
