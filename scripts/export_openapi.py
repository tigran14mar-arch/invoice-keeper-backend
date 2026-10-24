"""
Выгрузка спецификации OpenAPI в файлы openapi.json и openapi.yaml.

К решению кейса спецификация прикладывается файлом, поэтому команда должна быть
простой и повторяемой:

    python scripts/export_openapi.py

Подключение к базе не требуется: описание строится из кода приложения.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        import yaml
        from app.main import app
    except ImportError as exc:
        print(f"Не хватает зависимости: {exc}. Выполните: pip install -r requirements.txt")
        return 1

    spec = app.openapi()
    (ROOT / "openapi.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "openapi.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    paths = len(spec.get("paths", {}))
    print(f"Готово: openapi.json и openapi.yaml ({paths} адресов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
