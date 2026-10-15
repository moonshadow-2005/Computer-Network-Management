from pathlib import Path
import re

from app.main import app


def test_openapi_operations_match_contract_table():
    contract = (Path(__file__).resolve().parents[1] / "接口与数据合同.md").read_text(
        encoding="utf-8"
    )
    api_section = contract.split("## API 路径", 1)[1].split("\n## ", 1)[0]
    documented_rows = re.findall(
        r"^\|\s*([A-Z]+)\s*\|\s*`([^`]+)`", api_section, re.MULTILINE
    )
    documented = set(documented_rows)
    openapi = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1/")
        for method in operations
        if method.lower() in {
            "get", "post", "put", "patch", "delete", "head", "options", "trace"
        }
    }

    assert len(documented_rows) == len(documented), "接口合同表包含重复方法/路径行。"
    assert documented == openapi, (
        f"合同与 OpenAPI 不一致：未记录={sorted(openapi - documented)}；"
        f"合同多余={sorted(documented - openapi)}"
    )
