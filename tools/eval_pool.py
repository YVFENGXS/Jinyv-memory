#!/usr/bin/env python3
"""多 provider 轮换评测：一个额度/容量失败，自动换下一个。

适配器逐题写 <out>.jsonl 检查点；换 provider 后用同一个 --out 续跑，
已完成的题不会重跑。

providers.json:
{
  "embed": {"base": "https://api.example.com/v1",
            "model": "bge-m3", "key": "sk-..."},
  "providers": [
    {"name": "provider-a", "base": "https://api-a.example.com/v1",
     "key": "sk-...", "model": "model-a"},
    {"name": "provider-b", "base": "https://api-b.example.com/v1",
     "key": "rc-...", "model": "model-b"}
  ]
}

用法:
  python3 tools/eval_pool.py --data longmemeval_s.json --out lme_500.json \
      --providers providers.json --limit 500
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTER = os.path.join(HERE, "eval_longmemeval.py")


def load_providers(path):
    obj = json.load(open(path, encoding="utf-8"))
    if isinstance(obj, list):
        return {}, obj
    return obj.get("embed") or {}, obj.get("providers") or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--providers", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--topk-multi", type=int, default=14)
    ap.add_argument("--max-retries", type=int, default=1,
                    help="单个 provider 内 429/5xx 重试次数；用完就换下一个")
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--no-back", action="store_true")
    args = ap.parse_args()

    embed, providers = load_providers(args.providers)
    if not providers:
        sys.exit("providers.json 里没有 providers")

    for p in providers:
        name = p.get("name") or p.get("model") or "provider"
        env = {
            "RP_FRONT_KEY": p["key"],
            "RP_BACK_KEY": p.get("back_key") or p["key"],
            "RP_LLM_BASE": p["base"],
            "RP_FRONT_BASE": p["base"],
            "RP_FRONT_MODEL": p["model"],
            "RP_BACK_MODEL": p.get("back_model") or p["model"],
        }
        if embed and not args.no_embed:
            env.update({
                "RP_EMBED": "1",
                "RP_EMBED_BASE": embed.get("base", "https://api.example.com/v1"),
                "RP_EMBED_MODEL": embed.get("model", "BAAI/bge-m3"),
                "RP_EMBED_KEY": embed.get("key") or p["key"],
            })
        else:
            env["RP_EMBED"] = "0"

        fd, env_path = tempfile.mkstemp(prefix="pool_", suffix=".env")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for k, v in env.items():
                f.write(f"{k}={v}\n")

        cmd = [sys.executable, ADAPTER,
               "--data", args.data, "--out", args.out,
               "--env-file", env_path,
               "--front-model", p["model"],
               "--provider-name", name,
               "--max-retries", str(max(0, args.max_retries)),
               "--topk", str(args.topk), "--topk-multi", str(args.topk_multi)]
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        if args.no_embed:
            cmd += ["--no-embed"]
        if args.no_back:
            cmd += ["--no-back"]

        ckpt = args.out + ".jsonl"
        done = 0
        if os.path.exists(ckpt):
            with open(ckpt, encoding="utf-8") as f:
                done = sum(1 for line in f if line.strip())
        print(f"\n[pool] -> {name} model={p['model']} done={done}", flush=True)
        rc = subprocess.call(cmd)
        try:
            os.unlink(env_path)
        except OSError:
            pass
        if rc == 0:
            print(f"[pool] 完成 provider={name}", flush=True)
            return
        print(f"[pool] {name} 失败 exit={rc}，换下一个", flush=True)

    print("[pool] 所有 provider 都失败或额度用完；检查点已保留，可稍后续跑", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
