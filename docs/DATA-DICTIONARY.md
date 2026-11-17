# 定义契约 gridlab

## 采样行

| 列 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|
| `sensor_id` | 字符串 | — | 否 | 契约表内的传感器编号，区分大小写 |
| `sampled_at` | ISO8601 | — | 否 | 设备时钟记录的绝对时刻，可带 `Z` 或 `+08:00` |
| `offset_seconds` | 整数 | 秒 | 是 | 设备自报的时钟偏移，只作对照；真值以偏移表为准 |
| `value` | 十进制 | 见单位列 | 是 | 允许千分位与全角数字；空值哨兵见下 |
| `unit` | 字符串 | — | 否 | 写法归一后必须等于契约单位 |
| `quality` | 枚举 | — | 是 | `good` / `suspect` / `bad`，缺省按 `good` |
| `ingest_seq` | 整数 | — | 是 | 到达顺序，缺省用原始行号 |

## 数值

- 一切用量走 `Decimal`，落库以字符串保存，最小刻度 `0.001`；对外 JSON 也是字符串，禁止 float 往返。
- 补齐值按 `0.001` 四舍五入（`ROUND_HALF_UP`），锚点值原样保留。
- 覆盖度保留 `0.001`，页面按百分比显示一位小数。

## 时间

- 存储与计算一律 UTC，秒级，格式 `YYYY-MM-DDTHH:MM:SSZ`。
- 页面显示 `+08:00`，导出 CSV 同时给 UTC 与 `+08:00` 两列。
- 分钟桶：`[t, t+1min)`；跨日不特殊处理，`23:59:30` 落到当日 `23:59` 桶。
- 15 分钟窗口：`[w, w+15min)`，右端开；端点 `00:15:00` 属于 `[00:15, 00:30)`。

## 偏移与质量

- 偏移表条目为 `[effective_from, effective_to)`；同一传感器同一时刻命中两段即 `OFFSET_AMBIGUOUS`，该行隔离。
- 无条目时按行内 `offset_seconds`（缺省 0）校正，并标 `OFFSET_ASSUMED`。
- 表内偏移与行内偏移不一致时以表为准，标 `OFFSET_CONFLICT`，不隔离。

## 空值三态

| 写法 | 含义 | 处理 |
|---|---|---|
| 字段缺失 / 空串 | 未采集 | 隔离，原因码 `SENTINEL_VALUE` |
| `-` `--` `N/A` `null` `未知` `空` `无` | 哨兵 | 隔离，原因码 `SENTINEL_VALUE` |
| `0` | 真实零值 | 采纳，进合计 |

哨兵清单只在 `app/config.py` 定义一次。

## 去重键

- 去重键为 `(sensor_id, 分钟桶)`，不是全行哈希：同一分钟内的两次采样代表同一物理时刻，必须收敛为一条。
- 择一顺序：质量位 `good > suspect > bad` → `ingest_seq` 较大者 → 原始行号较大者；全序且与文件顺序无关。
- 未保留的一条进重复表，保留双方行号与数值，供人工复核。

## 空态

| 输入 | 结论码 | 含义 |
|---|---|---|
| 0 字节 | `EMPTY_FILE` | 文件是空的 |
| 只有空行 | `ALL_BLANK` | 没有任何表头与数据 |
| 仅表头 | `HEADER_ONLY` | 声明了列但没有数据行 |

三种空态都会生成批次，四账按 0 对 0 判平，结论码显示在总览页。

## 网格状态

| 状态 | 含义 | 值来源 | 计入窗口实有 |
|---|---|---|---|
| `OBSERVED` | 原测点实采 | 本传感器采纳样本 | 是 |
| `FILLED` | 策略补齐（hold/linear，≤3 桶） | 推导插值，不回流采纳合计 | 否 |
| `GAP` | 缺口（超限或边缘无锚） | 无值 | 否 |
| `SUBSTITUTED` | 人工批准的邻表替测 | 邻表同分钟实采值，逐桶留来源 | 是 |

网格账：`应有桶 = OBSERVED + FILLED + GAP + SUBSTITUTED`。

## 替测（M8）

- 触发前提：目标桶当前必须是 `GAP`；`OBSERVED` / `FILLED` 桶拒绝替测（`SUBSTITUTE_NOT_GAP`）。
- 候选核对五项，任一不过即 `eligible=false`，不开放批准：
  1. 量程：契约 `low/high` 完全相同（`SUBSTITUTE_RANGE_MISMATCH`）；
  2. 单位：归一后相同（`SUBSTITUTE_UNIT_MISMATCH`）；
  3. 采样率：`rate_minutes` 相同（`SUBSTITUTE_RATE_MISMATCH`）；
  4. 时段重叠：每个缺口桶在邻表同一分钟桶都有采纳样本（`SUBSTITUTE_NO_DONOR`）；
  5. 读数差异：两表共同实采分钟桶的最大绝对差 ÷ 量程跨度 ≤ `SUBSTITUTE_RATE_TOLERANCE`（默认 `0.1`，`SUBSTITUTE_DIFF_TOO_LARGE`）。
- 批准落账：网格行 `sensor_id` 不变（原测点身份），状态转 `SUBSTITUTED`、原因码 `SUBSTITUTED_NEIGHBOR`，新增 `donor_sensor_id` 与 `donor_source_row`；`substitutions` 表逐桶记录决定号、邻表、来源行、批准时刻与批准前原因码 `prior_reason`。
- 决定号 `decision_id` 由 `(run_id, sensor_id, donor_sensor_id, 排序后桶集合)` 哈希派生；同决定重跑返回 `reused=true`，不新增网格行、留痕行或决定头。
- 整个批准在一个事务内：任一桶校验失败或落账影响行数为 0 即回滚，缺口与计数恢复原状。
- 撤销：按 `prior_reason` 把每个替测桶还原成原 `GAP`（值与邻表字段清空），删除留痕行，决定头置 `REVOKED` 保留审计；窗口与四账同步重算。
- 数值纪律：替测值合计 `substituted_sum` 单列，不进 `accepted_sum` / `observed_sum`，`grid_sum = observed_sum + filled_sum` 维持不变。
