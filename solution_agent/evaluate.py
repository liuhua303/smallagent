"""Summarize human annotations, never substitute quote matches for support."""

import argparse
import json
import os
import sys
from pathlib import Path


def summarize(review):
    claims = review["claims"]
    if any(c["human_supported"] is not None and type(c["human_supported"]) is not bool for c in claims):
        raise ValueError("human_supported 必须为 true、false 或 null")
    constraints = review["constraints"]
    field = "human_constraint_violation_in_recommendation"
    if any(c[field] is not None and type(c[field]) is not bool for c in constraints):
        raise ValueError("约束违反标记必须为 true、false 或 null")
    utility = review.get("utility_1_to_5")
    if utility is not None and (type(utility) is not int or not 1 <= utility <= 5):
        raise ValueError("utility_1_to_5 必须为1至5的整数或 null")
    labeled = [c for c in claims if type(c["human_supported"]) is bool]
    checked = [c for c in constraints if type(c[field]) is bool]
    return {"mode": review["mode"], "claims_total": len(claims), "claims_reviewed": len(labeled),
            "claim_review_coverage": len(labeled) / len(claims) if claims else None,
            "human_support_rate_on_reviewed": sum(c["human_supported"] for c in labeled) / len(labeled) if labeled else None,
            "constraints_reviewed": len(checked),
            "constraint_violations": sum(c[field] for c in checked) if checked else None,
            "utility_1_to_5": utility,
            "note": "未标注为 null，不自动当作通过；此为自定义人工评审统计，不是完整ALCE指标。"}


def main():
    if os.name == "nt" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="汇总人工填写的 review.json，不调用模型。")
    parser.add_argument("reviews", type=Path, nargs="+")
    args = parser.parse_args()
    results = []
    for path in args.reviews:
        result = summarize(json.loads(path.read_text(encoding="utf-8-sig")))
        results.append(dict(file=str(path), **result))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
