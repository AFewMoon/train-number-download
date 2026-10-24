# 中国铁路车次全量抓取（rail-china-crawl）

从 12306 公开接口抓取**当前图定全部客运车次**及其**逐站时刻表**，落库 SQLite，并导出 CSV / JSONL。
支持断点续跑、分批执行、失败重跑、限速与磁盘缓存。

## 0. 已抓取结果（2026-10-24 快照）

| 指标 | 数值 |
|---|---|
| 车次 | **15,978**（G 6,061 / D 3,438 / C 3,302 / K 1,363 / S 989 / Z 276 / T 182 / 数字普客 299 等） |
| 逐站记录 | **162,241** 条（平均 10.2 站/趟） |
| 车站 | 3,404 个（含电报码、全拼、简拼、城市） |
| 跨天经停 | 17,249 条；跨天开车 125 条（如 23:59 到、00:05 开，用 `depart_day_offset` 标识） |
| 全程里程 | **15,339 趟 / 96.0%**；逐站里程 **154,806 条 / 95.4%** |
| 车型 / 担当路局 | 车型 8,778 趟（54.9%）、路局 13,312 趟（83.3%） |
| 编组辆数 / 定员 | 7,384 趟（46.2%，由车型查字典得到的典型编组） |
| 未获取 | 12306 侧 1 趟（C4990 当日空时刻表）；第三方侧 200 趟无车次页（`failures.csv`） |
| 产物体积 | `rail.db` 30.8 MB、`stops.csv` 13.2 MB、`trains.jsonl` 48.4 MB、原始响应归档 43.9 MB |

字段非空率：车次等级 100%、始发/终到时刻 99.99%、全程历时 99.99%、站数 100%、全程里程 96.0%、
逐站里程 95.4%、担当路局 83.3%、车型 54.9%；**站台/股道 0%**（无任何公开来源）。
用 `rail report` 可随时重算。

## 1. 数据来源与口径

| 数据 | 来源 | 说明 |
|---|---|---|
| 车次目录（车次号 / 内部编号 / 起终点 / 站数） | `search.12306.cn/search/v1/train/search` | 单次最多返回 200 条，工具按前缀递归细分，直到每个分支不足 200 条 |
| 逐站到发时刻、站序、停留时长、车次等级 | `kyfw.12306.cn/otn/czxx/queryByTrainNo` | 需先访问 `otn/leftTicket/init` 获取 Cookie；无需登录 |
| 车站电报码 / 拼音 / 城市 | `kyfw.12306.cn/otn/resources/js/framework/station_name.js` | 约 3400 个车站 |
| 车型 / 编组辆数 / 定员 / 餐车 / 卧铺 / 空调 | `data/rolling_stock.json` 车型字典 + `data/train_formation_overrides.json` 人工校正 + 车次等级规则 | 字典为典型编组参考值，非逐车核实 |
| 全程 / 逐站里程、承运车型、担当路局 | `www.gaotie.com.cn/checi/{车次}.html`（provider `gaotie_com`） | 需走代理（配置 `http.proxy`）；里程按**站名**对齐 12306 站序并做单调校验 |
| 站台 / 股道 | 暂无可达数据源 | 字段保留但留空，不编造 |

**已知限制（务必先读）**

- `resources/js/query/train_list.js` 是 **2022-08 的历史快照**（16 个日期、约 20 万条），不能代表当前运行图，因此仅用于交叉校验，不作为枚举来源。
- 12306 不提供**里程、站台、股道、车型、编组、定员**。里程/车型/路局来自 `gaotie.com.cn` 车次页，编组辆数/定员由其车型型号查 `data/rolling_stock.json` 字典得到（典型编组，非逐车核实）。
- **旧域名 `www.gaotie.cn` 已停站**：首页、`shike.` 子域都返回 nginx `404 Not Found`（直连与代理都一样），因此 provider 改用 `www.gaotie.com.cn`；`huochepiao.com` 是 JS 反爬壳，`tielu.cn` 证书异常/空页，`12306.com` 直接 EOF。
- 该站点对**普速车次**（K/T/Z/数字）通常不给车型与担当路局，只给里程；这类车次的车型仍为 `NULL`，编组只按等级规则写布尔特征（`source=rule:train_class`，置信度 0.45），不伪造车型与定员。
- 站台/股道没有任何可用的公开来源，保持 `NULL`。

## 2. 安装

需要 Python 3.11+。

```powershell
python -m pip install -r requirements.txt
```

无需安装本项目即可运行（`run.py` 会自动把 `src` 加入 `sys.path`）。也可选择安装命令行入口：

```powershell
python -m pip install -e .
rail status
```

## 3. 快速开始

```powershell
# 0) 接口体检（生成 docs/api-probe.md）
python run.py probe

# 1) 车站库
python run.py stations

# 2) 车次枚举（全量）
python run.py trains

# 3) 逐站时刻表（全量；可随时 Ctrl+C，再执行同一命令自动续跑）
python run.py timetable

# 4) 里程 / 车型 / 担当路局补全（走第三方，需要代理）
$env:RAIL_PROXY = "http://127.0.0.1:7890"
python run.py enrich

# 5) 导出 CSV + 嵌套 JSONL
python run.py export

# 6) 质量报告与失败清单
python run.py report
```

一条命令跑完全部阶段（同样支持断点续跑）：

```powershell
python run.py all
```

小样本验证（约 30 趟，1 分钟内完成）：

```powershell
python run.py trains --prefix G --limit 30
python run.py timetable
```

## 4. 常用参数

| 参数 | 作用 |
|---|---|
| `--date 2026-10-25` | 指定快照日期（默认当天，需在 12306 预售期内） |
| `--prefix G,D` | 只处理指定车次前缀（可分批跑，降低单次时长） |
| `--limit 100` | 每个阶段最多处理多少趟 |
| `--retry-failed` | `trains`/`timetable` 阶段同时重跑失败任务 |
| `--no-providers` | 跳过第三方补全，只用字典与规则 |
| `-v` | 输出调试日志 |
| `--config path.yaml` | 使用自定义配置 |

断点续跑原理：每个阶段把工作项写入 `crawl_tasks(stage, task_key, status)`，执行时只取 `pending`（加 `--retry-failed` 时含 `failed`），已完成项自动跳过。`trains` 阶段把**前缀队列**也存进该表，因此被风控中断后重跑会从断点继续细分，不会重头再来；连续 3 个前缀失败即主动停下，等待下次续跑。

## 5. 输出物

```
output/
├── rail.db              # SQLite 主库（WAL）
├── report.md            # 覆盖率 / 校验 / 分布统计
├── failures.csv         # 失败任务清单（含错误摘要）
├── trains.jsonl         # 每行一趟车的嵌套 JSON（含 stops 数组）
├── csv/
│   ├── trains.csv       # 车次 + 编组（中文表头，UTF-8 BOM，Excel 可直接打开）
│   ├── stops.csv        # 逐站时刻（含城市、电报码）
│   ├── formation.csv    # 编组与车辆
│   └── stations.csv     # 车站库
└── raw/
    ├── timetable.jsonl  # 时刻表原始响应归档（与缓存一一对应）
    └── enrich.jsonl     # 第三方补全原始结果
cache/http/              # 按 URL sha1 缓存的响应，重跑零流量
logs/rail.log            # 轮转日志
```

### SQLite 表结构

| 表 | 主键 | 关键字段 |
|---|---|---|
| `stations` | `telecode` | 站名、全拼、简拼、城市、城市代码 |
| `trains` | `(train_no, station_train_code)` | 等级、始发/终到站及时刻、历时、站数、全程里程、电报码 |
| `train_stops` | `(train_no, station_train_code, station_no)` | 到达/开车、停留分钟、`day_offset`（到达跨天）、`depart_day_offset`（开车跨天，跨零点时比前者大 1）、里程、站台、始发/终到标记 |
| `train_formation` | `(train_no, station_train_code)` | 车型、编组辆数、定员、餐车/卧铺/空调、来源、置信度 |
| `crawl_tasks` | `(stage, task_key)` | 状态机（pending/done/failed/skipped）、尝试次数、错误 |
| `meta` | `key` | 快照日期、各阶段更新时间 |

同一 `train_no` 可能对应多个车次号（同一车底交路），因此主键包含 `station_train_code`。

### 字段来源标记

| 字段 | 取值 |
|---|---|
| `trains.source` | `12306:search`（枚举）/ `12306:czxx`（时刻表） |
| `trains.operator` | 来自 `thirdparty:gaotie_com`（原样保存「上海局集团有限公司」形式） |
| `trains.total_mileage_km`、`train_stops.mileage_km` | 来自 `thirdparty:gaotie_com`，首末站缺失时分别补 0 与全程 |
| `train_stops.source` | `12306:czxx` |
| `train_formation.source` | `override` / `dictionary+<provider>` / `rule:train_class` / `thirdparty:<host>` |
| `train_formation.confidence` | 0.95 人工校正 / 0.9 字典 / 0.8 第三方车次页 / 0.45 等级规则 |

## 6. 配置

编辑 `config/settings.yaml`：

```yaml
http:
  rate_per_sec: 1.3          # kyfw.12306.cn 令牌桶速率
  search_rate_per_sec: 0.35  # search.12306.cn 单独限速（风控更严）
  burst: 1
  workers: 3                 # 并发抓取线程（写库仍在主线程串行）
  max_attempts: 5
  pause_minutes: 5           # 连续被拦截 5 次后的整体冷却时间
  cache: true
  proxy: "http://127.0.0.1:7890"   # 仅第三方 provider 使用；12306 始终直连，也可用 RAIL_PROXY 覆盖
enrich:
  providers: ["gaotie_com"]
  provider_rate_per_sec: 2.0
```

**实测速率**（2026-10-24，本机家宽）：

| 阶段 | 请求量 | 实测速率 | 耗时 |
|---|---|---|---|
| `stations` | 1 | — | <1s |
| `trains`（枚举） | 327 | 0.35 req/s | 约 13 分钟（含 2 次 403 退避） |
| `timetable` | 15,977 | 约 1.4 req/s | 约 3 小时 40 分 |
| `enrich`（`gaotie.com.cn`，走代理） | 15,978 | 约 2.0 req/s | 约 2 小时 17 分 |
| `export` / `report` | 0 | — | 约 10 秒 |

**踩过的坑**：`search.12306.cn` 在 2 req/s 下约 40 次请求就会返回 403，且带同 IP 冷却（数分钟到十几分钟）；`kyfw.12306.cn` 在 1 req/s 下连续 300 次无异常。因此两者用独立令牌桶，`search` 慢 3 倍。遇到 403 会先单次退避（8s→16s→24s…），连续 5 次则整体冷却 `pause_minutes`。

第三方站点更麻烦：`www.gaotie.cn` 整个站已停（首页、`shike.` 子域都返回 nginx `404 Not Found`，直连/代理一致），真正可用的是 `www.gaotie.com.cn`，且它对本机直连 IP 不友好，**必须走代理**（`http.proxy` 或环境变量 `RAIL_PROXY`）。12306 请求始终直连，不经过代理。

**礼貌抓取**：默认限速、并发 2，不登录、不破解验证码。请勿把 `rate_per_sec` 调到 2 以上，否则大概率被风控。

## 7. 数据校验

`report.md` 包含：

- 字段非空率（车次时刻、历时、站数、里程、站台、车型、编组、定员、餐车/卧铺/空调）
- 校验项：单站车次、无经停车次、里程非单调递增序列、同站到达晚于开车、跨天经停数
- 分布：车次等级、车次号前缀、经停站数区间、车次最多的 Top 20 线路
- 任务状态汇总；`failures.csv` 给出失败明细

## 8. 目录结构

```
train-number-download/
├── run.py                  # 免安装入口
├── pyproject.toml
├── config/settings.yaml
├── data/rolling_stock.json
├── data/train_formation_overrides.json
├── scripts/probe.py
├── src/rail/
│   ├── cli.py              # 子命令
│   ├── config.py           # 配置
│   ├── http.py             # 限速 / 重试 / 缓存 / Cookie
│   ├── storage.py          # SQLite schema / upsert / 任务状态 / CSV
│   ├── parsers.py          # 纯函数解析（可单测）
│   ├── stations.py
│   ├── enumerate.py        # 车次枚举（前缀递归）
│   ├── timetable.py        # 逐站时刻表
│   ├── formation.py        # 编组：override → 字典 → 等级规则
│   ├── providers/          # 第三方 provider（当前实现 gaotie）
│   ├── enrich.py
│   ├── export.py
│   ├── report.py
│   └── probe.py
├── tests/                  # unittest，含 fixtures
├── docs/api-probe.md       # 体检结果（运行 probe 生成）
└── output/ cache/ logs/
```

## 9. 扩展

- **接入新的数据源**：在 `src/rail/providers/` 新增类，实现 `fetch(code, train_no) -> Enrichment | None`，失败时抛 `ProviderUnavailable`，然后在 `providers.build_providers` 注册、在 `settings.yaml` 的 `enrich.providers` 启用。
- **修正某趟车编组**：编辑 `data/train_formation_overrides.json`：

```json
{"trains": {"G1": {"train_type": "CR400AF-BZ", "formation_cars": 17, "seats_total": 1283}}}
```

- **补充车型字典**：`data/rolling_stock.json` 的 `types` 下增加型号即可，键名需与第三方/人工校正中的写法一致（自动转大写）。

## 10. 测试

```powershell
$env:PYTHONUTF8=1
python -m unittest discover -s tests -v
```

解析器测试使用 `tests/fixtures/` 中的脱敏样本，覆盖：车站库字段错位、车次列表两种形态、跨天时刻、空数组、非法响应、幂等 upsert、任务状态机、CSV BOM 编码、编组优先级。

## 11. 合规声明

仅访问 12306 公开页面与接口，不登录账号、不绕过验证码、不采集身份证/手机号等个人信息；频率控制在正常用户浏览量级。数据仅供研究与个人使用，不作为出行依据，请以 12306 官方为准。

## 12. 许可

本项目以 **GNU General Public License v3.0 or later**（GPL-3.0-or-later）发布。

- 版权：Copyright (C) 2026 AFewMoon
- 协议全文：[`LICENSE`](LICENSE)（GPLv3，与 <https://www.gnu.org/licenses/gpl-3.0.txt> 一致）
- `or later` 含义：可按 GPLv3 条款使用，也可选择任意更新的 GNU GPL 版本；若未指定，默认按 GPLv3 处理。
- 分发衍生作品时须保留本许可与版权声明，并以同一许可开放源代码。
