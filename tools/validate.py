#!/usr/bin/env python3
"""配信前チェック。アプリ側のパーサが受け付ける形になっているかを検証する。

アプリは壊れたJSONを掴んだら同梱スナップショットへフォールバックするので致命傷にはならないが、
「pushしたのに反映されない」を防ぐためにpush前・CIでここを通す。

    python3 tools/validate.py
"""
import json
import re
import sys
from pathlib import Path

V1 = Path(__file__).resolve().parent.parent / "v1"
CATALOG = V1 / "subscription_services.json"
CHANGES = V1 / "catalog_changes.json"

# アプリ側 enum と対応。ここを増やすときはアプリのリリースが先。
SUPPORTED_SCHEMA = 1
CATEGORIES = {"VIDEO", "MUSIC", "AI", "TOOLS", "STORAGE", "DEV", "DESIGN",
              "BUSINESS", "CARD", "FINANCE", "EDUCATION", "LIFE", "OTHER"}
CYCLES = {"MONTHLY", "YEARLY"}
CURRENCIES = {"JPY", "USD"}
KINDS = {"PRICE_UP", "PRICE_DOWN", "CURRENCY_CHANGED", "CYCLE_CHANGED",
         "PLAN_RENAMED", "PLAN_ADDED", "SERVICE_RENAMED", "SERVICE_REMOVED"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def check_header(root: dict, label: str) -> None:
    schema = root.get("schemaVersion", 1)
    if not isinstance(schema, int) or schema < 1:
        err(f"{label}: schemaVersion は 1 以上の整数であること (got {schema!r})")
    elif schema > SUPPORTED_SCHEMA:
        err(f"{label}: schemaVersion={schema} は未リリース。"
            f"アプリが対応済みなのは {SUPPORTED_SCHEMA} まで（このまま公開すると全端末が同梱版に落ちる）")
    if not isinstance(root.get("version"), int):
        err(f"{label}: version は整数であること (got {root.get('version')!r})")
    if not DATE.match(str(root.get("updatedAt", ""))):
        err(f"{label}: updatedAt は YYYY-MM-DD 形式であること (got {root.get('updatedAt')!r})")


def main() -> int:
    for path in (CATALOG, CHANGES):
        if not path.exists():
            err(f"{path} が無い")
    if errors:
        return report()

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    changes = json.loads(CHANGES.read_text(encoding="utf-8"))

    check_header(catalog, CATALOG.name)
    check_header(changes, CHANGES.name)

    if catalog.get("version") != changes.get("version"):
        err(f"version が食い違っている: "
            f"{CATALOG.name}={catalog.get('version')} / {CHANGES.name}={changes.get('version')}")

    # --- services ---
    services = catalog.get("services")
    if not isinstance(services, list) or not services:
        err(f"{CATALOG.name}: services が空")
        return report()

    seen_ids: set[str] = set()
    for i, s in enumerate(services):
        where = f"{CATALOG.name}: services[{i}]"
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            err(f"{where}: id が必須")
            continue
        where = f"{CATALOG.name}: {sid}"
        if sid in seen_ids:
            err(f"{where}: id が重複している")
        seen_ids.add(sid)
        if not s.get("name"):
            err(f"{where}: name が必須")
        if s.get("category") not in CATEGORIES:
            err(f"{where}: 未知の category {s.get('category')!r}（アプリでは OTHER 扱いになる）")
        plans = s.get("plans")
        if not isinstance(plans, list) or not plans:
            err(f"{where}: plans が空")
            continue
        for plan in plans:
            pname = plan.get("name")
            if not pname:
                err(f"{where}: plan の name が必須")
            price = plan.get("price")
            if not isinstance(price, int) or isinstance(price, bool):
                err(f"{where} / {pname}: price は最小単位の整数（円 or セント）であること (got {price!r})")
            elif price < 0:
                err(f"{where} / {pname}: price が負")
            if plan.get("cycle") not in CYCLES:
                err(f"{where} / {pname}: 未知の cycle {plan.get('cycle')!r}")
            currency = plan.get("currency", "JPY")
            if currency not in CURRENCIES:
                err(f"{where} / {pname}: 未知の currency {currency!r}")
            if currency == "USD" and isinstance(price, int) and price % 100 == 0 and price < 100:
                warn(f"{where} / {pname}: USD の price={price} はセント指定？（$1 なら 100）")

    # --- changes ---
    change_list = changes.get("changes")
    if not isinstance(change_list, list):
        err(f"{CHANGES.name}: changes は配列であること")
        return report()

    for i, c in enumerate(change_list):
        where = f"{CHANGES.name}: changes[{i}]"
        for key in ("serviceId", "serviceName", "category", "kind"):
            if not c.get(key):
                err(f"{where}: {key} が必須")
        kind = c.get("kind")
        if kind and kind not in KINDS:
            err(f"{where}: 未知の kind {kind!r}（アプリでは黙って読み飛ばされる）")
        if c.get("category") not in CATEGORIES:
            err(f"{where}: 未知の category {c.get('category')!r}")
        for key in ("currency", "oldCurrency"):
            if key in c and c[key] not in CURRENCIES:
                err(f"{where}: 未知の {key} {c[key]!r}")
        for key in ("cycle", "oldCycle"):
            if key in c and c[key] not in CYCLES:
                err(f"{where}: 未知の {key} {c[key]!r}")
        for key in ("old", "new"):
            if key in c and (not isinstance(c[key], int) or isinstance(c[key], bool)):
                err(f"{where}: {key} は整数であること (got {c[key]!r})")

        sid = c.get("serviceId")
        if sid and sid not in seen_ids and kind != "SERVICE_REMOVED":
            err(f"{where}: serviceId {sid!r} がカタログに無い")

        # 「登録済みサブスクにワンタップ反映」が効く条件（アプリの isApplicableTo 相当）。
        # 満たさない変更は通知には出るがワンタップ反映ボタンが出ない。
        if kind in ("PRICE_UP", "PRICE_DOWN", "CURRENCY_CHANGED", "CYCLE_CHANGED", "PLAN_RENAMED"):
            if c.get("new") is None:
                err(f"{where}: {kind} には new が必須（無いとワンタップ反映が出ない）")
            if c.get("old") is None:
                warn(f"{where}: {kind} に old が無い。"
                     f"登録済みユーザーへのワンタップ反映が出ない")
            if not c.get("plan") and not c.get("oldPlan"):
                warn(f"{where}: plan / oldPlan が無い。ワンタップ反映のプラン照合に失敗する")

        # 変更後の値がカタログ本体と一致しているか（一番やりがちな取りこぼし）
        if kind in ("PRICE_UP", "PRICE_DOWN") and c.get("new") is not None:
            svc = next((s for s in services if s.get("id") == sid), None)
            if svc:
                plan_name = c.get("plan")
                plan = next((p for p in svc.get("plans", []) if p.get("name") == plan_name), None)
                if plan is None:
                    warn(f"{where}: プラン {plan_name!r} が {sid} のカタログに無い")
                elif plan.get("price") != c.get("new"):
                    err(f"{where}: new={c.get('new')} だがカタログ本体は "
                        f"{plan.get('price')}。どちらかが更新漏れ")

    return report()


def report() -> int:
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    if errors:
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s) — 公開前に修正してください")
        return 1
    print(f"OK — {len(warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
