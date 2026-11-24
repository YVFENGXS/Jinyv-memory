#!/usr/bin/env python3
"""把主检查点里答错的题抽出来，用指定 provider 重答，并合并回主检查点。

用法:
  python3 tools/eval_retry.py --data longmemeval_s.json \
    --ckpt lme_500.json.jsonl --out lme_retry.json \
    --env-file your.env --front-model model-a \
    --provider-name retry-a

流程:
  1. 读主 ckpt，收集 correct=False（可选 filtered）的 question_id；
  2. 从完整 data 里抽出这些题，写到 <out>.data.json；
  3. 调 eval_longmemeval.py 重答子集（独立检查点 <out>.jsonl）；
  4. 用重答结果替换主 ckpt 里对应行；
  5. 打印合并前后的分数和分题型。
"""
import argparse
import json
import os
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTER = os.path.join(HERE, "eval_longmemeval.py")


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--ckpt", required=True, help="主检查点 JSONL")
    ap.add_argument("--out", required=True, help="重答结果前缀")
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--front-model", required=True)
    ap.add_argument("--provider-name", default="retry")
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--topk-multi", type=int, default=14)
    ap.add_argument("--include-filtered", action="store_true")
    args = ap.parse_args()

    rows = load_jsonl(args.ckpt)
    if not rows:
        sys.exit("主检查点为空")
    bad_ids = {
        r["question_id"] for r in rows
        if (not r.get("correct"))
        or (args.include_filtered and r.get("filtered"))
    }
    print(f"主检查点 {len(rows)} 题，待重答 {len(bad_ids)} 题", flush=True)
    if not bad_ids:
        print("没有需要重答的题")
        return

    data = json.load(open(args.data, encoding="utf-8"))
    subset = [x for x in data if x["question_id"] in bad_ids]
    data_file = args.out + ".data.json"
    json.dump(subset, open(data_file, "w", encoding="utf-8"),
              ensure_ascii=False)
    print(f"子集 {len(subset)} 题 -> {data_file}", flush=True)

    cmd = [sys.executable, ADAPTER,
           "--data", data_file, "--out", args.out,
           "--env-file", args.env_file,
           "--front-model", args.front_model,
           "--provider-name", args.provider_name,
           "--max-retries", str(max(0, args.max_retries)),
           "--topk", str(args.topk), "--topk-multi", str(args.topk_multi)]
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(f"重答进程失败 exit={rc}；主检查点未改动")

    retry = {r["question_id"]: r for r in load_jsonl(args.out + ".jsonl")}
    before = sum(1 for r in rows if r.get("correct"))
    merged = [retry.get(r["question_id"], r) for r in rows]
    after = sum(1 for r in merged if r.get("correct"))

    tmp = args.ckpt + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in merged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, args.ckpt)

    print(f"\n合并完成：{before}/{len(rows)} -> {after}/{len(merged)}"
          f" = {after / max(1, len(merged)):.3f}")
    for t, n in Counter(r["question_type"] for r in merged).items():
        rs = [r for r in merged if r["question_type"] == t]
        print(f"  {t:26s} {sum(1 for r in rs if r.get('correct'))}/{n}")


if __name__ == "__main__":
    main()
