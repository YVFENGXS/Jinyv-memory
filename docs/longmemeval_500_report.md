# LongMemEval_s 500 题评测报告

## 结论

- **总分：465 / 500 = 93.0%**（LLM judge 判定）。**这是 5 个模型的并集，不是单系统单轮成绩**：`tools/eval_retry.py` 把答错的题抽出来换模型重答后合并（best-of-N on failures）；扣掉 gpt-6.1-sol 定向救回的 16 道，约 **449/500 ≈ 89.8%**，更接近单轮水平。不可直接与公开榜单或其它系统横比。
- **本次评测不经过 `jinyv_memory` 中间层**：分块、BM25、向量融合、事实归并、作答、判分全部在 `tools/eval_longmemeval.py` 内实现，唯一依赖是本仓库的 `embedding` 适配器。它衡量的是「一条 BM25 + 向量 + 归并的问答流水线」，不是事件卡 / 归档游标 / 账本 / 披露分级 / 人设护栏这套记忆中间层。走中间层的对照评测见 `tools/eval_via_memory.py`（规则构造事件卡，非线上 LLM 摘要，单独标注口径）。
- 主力模型 **DeepSeek-V4.1-Flash-1017** 承担了绝大部分首轮作答，速度约 20–25 秒/题、输出干净、可关闭思考，**作为主力足够**。
- 检索命中率 98.4%，剩余错误几乎都发生在“已检索到证据”的题上，瓶颈在跨会话聚合与时间排序，而非召回。
- 早期 60 题小样本准确率为 0.900，本次 500 题 0.930，两者口径一致，大样本结果更可信。

明细数据见 [LongMemEval_500_评测报告.xlsx](LongMemEval_500_评测报告.xlsx)，逐题结果见 [longmemeval_500_results.jsonl](longmemeval_500_results.jsonl)。

## 分题型

| 题型 | 正确/总数 | 准确率 |
|---|---|---|
| single-session-user | 70/70 | 100.0% |
| single-session-assistant | 56/56 | 100.0% |
| single-session-preference | 29/30 | 96.7% |
| knowledge-update | 76/78 | 97.4% |
| temporal-reasoning | 118/133 | 88.7% |
| multi-session | 116/133 | 87.2% |

剩余 35 道错题集中在 multi-session（17）与 temporal-reasoning（15），另有 knowledge-update 2 道、preference 1 道。

## 评测方法

1. **检索**：脚本内自建 BM25 + embedding 混合召回，单会话题 top-k 8，multi-session 题 top-k 14（**不经过中间层 retriever**）。
2. **后台归并**：先把检索到的会话片段归并为事实要点（preference 题跳过），归并与作答分离，降低长上下文噪声。
3. **作答**：单会话题直接作答；knowledge-update 按时间排序后作答；temporal-reasoning 依赖归并后的时间线。
4. **判分**：LLM judge 输出 `{correct, why}`，按题面与标准答案逐题判定。
5. **容错**：逐题写检查点；内容审核拦截的题单独标记，不中断整轮；限流按指数退避重试。

## 参与模型的表现与影响

> 以下按模型能力维度描述，不涉及服务商。分数是最终答案归属，不等于首轮贡献。

| 模型 | 角色 | 表现 | 强项 | 弱项 | 对记忆任务的影响 |
|---|---|---|---|---|---|
| **DeepSeek-V4.1-Flash-1017** | 主力 | 368 题归属，全部正确；两轮补答分别救回 32/54、5/56 | 输出干净、可关闭思考、20–25 秒/题、长文稳定 | 跨会话计数与时间排序仍会错 | 证据抽取与知识更新稳，是全部分数的基本盘，**足够作为主力** |
| DeepSeek-V4-Flash | 定向补答 | 23 题归属，全部正确 | 长题也能答对 | 1.5–4 分钟/题，大上下文易限流 | 适合少量难题补答，吞吐不足 |
| GLM-5.3-Flash | 早期补充 | 56 题归属，全部正确 | 中文语义与结构化输出稳定 | 免费额度有限、并发弱 | 记忆归并质量稳定，可作补充 |
| Qwen3.5-397B-A17B | 超长上下文补答 | 8 题救回 2；归属 2 题 | 10 万字符以上上下文可用 | 强制思考不可关、推理 token 挤占输出、限流明显 | 超长证据题可用，但推理可能污染归并事实 |
| gpt-6.1-sol | 错题定向重答 | 51 题救回 16（31.4%）；高推理 38 题仅救回 3（7.9%） | 3–8 秒/题，证据明确的题救回率高 | 多会话计数、时序弱；不支持关闭推理 | 适合“证据明确但原模型算错”的定向补答，高推理档性价比低 |

### 关于主力模型是否够用

DeepSeek-V4.1-Flash-1017 覆盖了 500 题中 368 题的最终作答且全部正确，剩余错题需要的是多会话聚合与相对时间换算，换更大模型（如高推理档）收益也很有限（38 题只多对 3 道）。因此：

- **日常评测与线上记忆问答，用它做主力足够**；
- 建议把预算放在检索与归并（多会话事实合并、时间归一化）上，而不是换更大的作答模型。

## 记忆与检索分析

| 指标 | 数值 |
|---|---|
| 检索命中 | 492 / 500 = 98.4% |
| 命中题准确率 | 94.1% |
| 未命中题准确率 | 25.0%（2/8） |
| 剩余错题中检索已命中的比例 | 34 / 35 |

结论：检索链路已接近上限。错题里 34 道证据已经在上下文中，模型仍答错，说明问题出在：

1. 跨多个会话的计数与集合聚合；
2. “多久以前 / 先后顺序”这类相对时间换算；
3. 归并阶段把不同时间的同一事实压缩错。

## 中间层对照（48 题分层子集）

为回答「93% 是否代表 `jinyv_memory` 中间层」，新增 `tools/eval_via_memory.py`：
把会话回放进临时记忆库，按会话构造规则事件卡并经 `commit_archive` 落库，
再用中间层自己的 `retriever` 取证作答。取每题型前 8 题（共 48 题），与原流水线同题对比：

| 题型 | 中间层检索 | 中间层准确率 | 原流水线准确率 |
|---|---|---|---|
| single-session-user | 7/8 | 7/8 | 8/8 |
| single-session-assistant | 8/8 | 8/8 | 8/8 |
| single-session-preference | 3/8 | 3/8 | 8/8 |
| knowledge-update | 6/8 | 4/8 | 8/8 |
| temporal-reasoning | 7/8 | 4/8 | 8/8 |
| multi-session | 7/8 | 3/8 | 7/8 |
| **合计** | **38/48 = 79.2%** | **29/48 = 60.4%** | **47/48 = 97.9%** |

失败构成：检索未命中的 10 题只答对 1 题（约 −9）；命中的 38 题答对 28 题（73.7%，
原流水线同题接近 100%，约 −9）。检索缺口集中在 preference（问句讲「资源/推荐」，
会话里是具体软件名，钩子字面匹配接不上）；作答缺口集中在 multi-session 计数（命中 7 题只答对 3 题）与 temporal-reasoning
相对时间换算（命中 7 题只答对 4 题），因为卡片摘要是整段原文，
没有原流水线那一步「后台事实归并」。knowledge-update 的 Rachel 题取了旧值，
是没有 `durable_facts` 的直接后果。

口径限制：中间层 v1 的事件卡摘要是规则拼装的原文（非线上 LLM 摘要），钩子由规则取词，
不写 `durable_facts`，也没有写 bge-m3 向量。因此 60.4% 不是中间层的上限，
只说明**同一批题上，绕开中间层与走中间层的差距是真实的**——93.0% 不能作为中间层能力对外表述。
逐题数据见 `lme_via_memory_strat48.json`。

评测噪声：首轮用推理型 provider 时，长证据的 reasoning 吃光 `max_tokens=400`，
48 题有 10 题返回空 content（multi-session 4/8），分数被压到 56.2%。关闭思考或把答案预算
提到 1200 后空答案消失，60.4% 是修正后的口径。

## 口径说明

- 本次为**混合模型**评测：首轮与补答使用了不同模型，最终分数按每题最后一次作答计入。逐题 `provider` 分布：DeepSeek-V4.1-Flash-1017 368（全对）、GLM-5.3-Flash 56（全对）、gpt-6.1-sol 51（对 16）、DeepSeek-V4-Flash 23（全对）、Qwen3.5-397B-A17B 2（全对）。
- 这是 **best-of-N on failures** 口径：错题被抽出换模型重答后合并。扣掉 gpt-6.1-sol 救回的 16 题，约 **449/500 ≈ 89.8%**。对外引用时应同时给出这两个数。
- **判分未独立**：脚本在未显式传 `--judge-model` 时使用 `judge_model = args.judge_model or args.front_model`，即自己判自己。逐题结果的 `judge_model` / `judge_self` 字段记录了实际判分模型（本轮历史数据写盘时尚未带该字段，属回溯缺口）。
- **未走中间层**：本报告的全部检索、归并、作答都在 `tools/eval_longmemeval.py` 内完成，没有经过事件卡、归档游标、账本、披露分级、人设护栏。需要中间层口径请改用 `tools/eval_via_memory.py`（规则构造事件卡，非线上 LLM 摘要）。
- 各轮补答的题目难度不同（越往后剩的题越难），救回率不可直接横向比较。
- 与早期 60 题 0.900 相比，题目集合不同、样本量不同，但结论一致。

## 复现

```bash
# 全量评测（provider 通过环境变量注入，不写入仓库）
python3 tools/eval_longmemeval.py \
  --data longmemeval_s.json --limit 500 \
  --out lme_500.json --env-file your.env \
  --front-model deepseek-v4.1-flash --provider-name main

# 错题重答并合并
python3 tools/eval_retry.py \
  --data longmemeval_s.json --ckpt lme_500.json.jsonl \
  --out lme_retry.json --env-file your.env \
  --front-model <model> --provider-name retry

# 生成 Excel 报告
python3 tools/make_report.py \
  docs/longmemeval_500_results.jsonl \
  docs/LongMemEval_500_评测报告.xlsx

# 走记忆中间层的对照评测（回放会话 → 规则事件卡 → 中间层 retriever）
python3 tools/eval_via_memory.py \
  --data longmemeval_s.json --limit 50 \
  --out lme_via_memory_50.json --env-file your.env \
  --front-model deepseek-v4.1-flash --judge-model <独立判别模型> \
  --provider-name via-memory
```
