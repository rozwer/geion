#!/usr/bin/env python3
"""Configure Firebase Hosting and Cloud Run settings interactively."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
FIREBASE_JSON = ROOT / "firebase.json"
FIREBASE_RC = ROOT / ".firebaserc"


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{question}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("入力が必須です。もう一度入力してください。")


def update_firebase_json(service_id: str, region: str) -> None:
    data = read_json(FIREBASE_JSON)
    hosting = data.setdefault("hosting", {})
    rewrites = hosting.setdefault("rewrites", [])
    if not rewrites:
        rewrites.append({"source": "/api/**", "run": {}})

    for entry in rewrites:
        run_cfg = entry.get("run")
        if isinstance(run_cfg, dict):
            run_cfg["serviceId"] = service_id
            run_cfg["region"] = region

    write_json(FIREBASE_JSON, data)
    print(f"更新しました: {FIREBASE_JSON}")


def update_firebaserc(project_id: str) -> None:
    data = read_json(FIREBASE_RC)
    projects = data.setdefault("projects", {})
    projects["default"] = project_id
    write_json(FIREBASE_RC, data)
    print(f"更新しました: {FIREBASE_RC}")


def main() -> None:
    print("Firebase 設定ヘルパー\n====================")
    project_id = prompt("Firebase プロジェクト ID")
    service_id = prompt("Cloud Run サービス ID", "bandwith-scraper")
    region = prompt("Cloud Run リージョン", "asia-northeast1")

    update_firebaserc(project_id)
    update_firebase_json(service_id, region)

    print("\n次のコマンドを順番に実行してください:")
    print("  firebase login")
    print(f"  firebase use {project_id}")
    print("  firebase deploy --only hosting")


if __name__ == "__main__":
    main()
