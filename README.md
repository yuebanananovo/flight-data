# flight-data

中日机票行情网站的数据仓库。GitHub Actions 每天日本时间 10:17 运行 `scripts/collect.py`，
从 SerpApi 查单程、往返价格日历、官网 / 平台比价和 Google 价格记录，写入 `data/prices.json`。
网站从这个文件的 Raw 地址读数据。

- 密钥：Settings → Secrets and variables → Actions → Secrets → `SERPAPI_KEY`（也支持本仓库已有的 `FLY`）
- 档位：同一页面的 Variables → `PLAN` = `free` / `starter` / `full`（对应 SerpApi 的免费 / Starter / Developer 套餐）
- 手动运行：Actions → 每日采集机票价格 → Run workflow
- 回填历史报价：先完成一次正常采集，再手动勾选“回填”。free 需额外最多 120 次搜索，starter / full 最多 560 次。免费档按剩余额度预留日常采集所需次数，再为六个方向均分可回填的出发日，按小时限额分批执行。走势覆盖范围取决于实际返回值和提前购买天数。

这个仓库要设成 Public，网站才能读到数据（里面只有价格数据和脚本，没有密钥）。

首次采集前，prices.json 的 rows 为空，网页显示模拟数据。Secrets 中的密钥只在采集任务中使用。

免费档优先覆盖东京往返沈阳、北京、上海六个方向。常规排程最多 246 次/月。报价为整段行程价；航段通过同一机场且航班号与航司一致时标为同航班号经停，换航班、换机场及信息不足分别标注。同航班号不能保证乘客无需下机。

免费档每天更新六个方向的半年单程价格日历，每周一查询提前 30 天的各航司报价和往返日历。常规搜索每月最多 246 项。data/calendars 按出发月份保留实际观察日期的历史。单个出发日的人工深查和回填单独计数。

每次成功采集后，工作流运行 `python scripts/monthly_lows.py`，根据已有日历生成 `data/monthly-lows.json`。查票页的「本月最低价」展示当前方向三个城市的全部并列最低日期，首尾两个月按半年范围裁剪。统计不调用 SerpApi。
需要单独重算时，手动运行工作流并勾选「仅用已保存的日历重新统计」。

### 公开采集的来源和余额

公开日程使用 SerpApi 的余额和每小时余量，默认 `PLAN=free`。Actions 通过 Secrets 读取 `SERPAPI_KEY`（或 `FLY`）和 `SEARCHAPI_KEY`。已配置的两家账户在每日开跑前各检查一次余额。SearchApi 的赠送额度默认全部留给专项实测，公开日程每天的 SearchApi 搜索上限默认为零。未来若另行分配日常备用额度，可设置 `DAILY_SEARCHAPI_LIMIT`（有效上限 4），并用 `SEARCHAPI_RESERVE` 指定保留量（默认 87）。仅主接口不可用时，东京与沈阳、北京的四个公共方向各可补一次单程查询。

可用额低或接口不可用时，Actions 输出警告。网站读取 `data/collector-status.json` 的状态、东京观察时刻和简短提醒。公共定时查询的逐项记录写入 `data/quote-observations/YYYY-MM-DD.jsonl.gz`，每行含接口来源、观察时刻、单程或往返、去回日期、停留天数、报价类型、日元金额、结果状态和公共参数指纹。历史报价保持原格式和原始精度。新观察才带真实时分秒。

## 本机往返队列

在网站「查票」页选择东京往返沈阳、北京、上海或大连，填写去回日期及各自可调整的天数，导出待查队列。在本仓库目录内使用 Python 3.9 或更新版运行以下命令。示例中的队列路径按实际下载位置填写。

```sh
python3 scripts/round_trips.py ~/Downloads/queue.json \
  --output ~/Documents/机票报价/往返.json --max-searches 7 --reserve 10
```

程序从环境变量 `SEARCHAPI_KEY` 读取密钥，也可在终端隐藏输入。它先核对服务商余额及小时余量。示例最多发送 7 次 SearchApi 搜索，账户至少保留 10 次。队列和结果各自保存在公开仓库外。回到网站导入结果文件即可查看报价。公共 GitHub Actions 不读取本机队列。

- 每组请求先用 1 次搜索查日期组合日历，再选原日期及最多两组日历价最低的邻近日期逐组核实。每组最多 2 次，用去程选择标识取得对应回程的整组报价。默认查起报价最低的一个去程和接口返回的可配对回程。日历价与配对总价各自保存。`--alternatives 0` 只核实原日期。日历价并列时按去程、回程日期排序。
- `--sellers` 为每个有配对结果的日期组合增加至多 1 次 Skyscanner 搜索，逐段核对两程的航班号、机场、起降日期和时刻。`--google-sellers` 先执行同样的 Skyscanner 比价，仅给缺少明确航司直销证据的每组去回航班最多增加 1 次 Google 卖家搜索。预订标识仅在内存里保留，跨次补报同一日期组合的多组配对报价时，另需至多 2 次重新选航班。卖家名称、报价、来源、查询时刻和匹配结果分别保存。官网身份、行李、同票及退改以实际返回的字段为准。
- SerpApi 备用配对默认关闭。只有显式指定 `--serp-fallback N` 且设置了环境变量 `SERPAPI_KEY` 才使用它。N 是上述 SearchApi 上限之外的额外搜索上限，默认还给 SerpApi 保留 246 次，可用 `--serp-reserve` 调整。
- 私人日期在本机处理。每个本机结果文件有独立的 `.hmac-key` 文件，供本机计算参数指纹。公开工作流只接受公共日程或已固定的公共样本。去回配对各自保留营销航班号、接口给出的实际承运信息、各段当地起降日时、时长及转机证据；未确认的行李、同票、退改条件存为 `null`。
- 部分步骤未完成时，程序保留已取得的报价并以状态码 2 退出。当天使用同一个输出文件再次运行，会跳过已完成步骤；之后重新运行会另存当天的观察。成功执行但供应商没有返回的组合单独标记。双向城市机场匹配、日期窗口、预算与重试可用 `python3 -m unittest discover -s tests` 离线验证。

## 私有队列与付费日程

网站的「查这一天」队列通过 `private-queue.yml` 每日最多领取一项单程参考查询。设置 Actions Variable `HELPER_FLIGHT_QUEUE_URL` 为 HTTPS 的 `/api/flights/queue/worker`，Secret `HELPER_FLIGHT_QUEUE_KEY` 为服务端同一凭据，另需原有 SerpApi 密钥。可用 `QUEUE_SERP_RESERVE` 保留查询次数。队列日期和回执保存在受限端点；客户端网络重试使用当前 runner 临时目录内的回执。

付费代码供独立私有仓库使用。私库工作流可检出公共代码的固定提交，获得 `scripts/` 和公共 `data/history/` 基线。先用 `python3 scripts/paid_collect.py --estimate` 核算当月需求。私有计划由私库 Variable `PRIVATE_FLIGHT_PLAN` 提供，也兼容同名 Secret，JSON 支持以下字段：

- `holidays` 是假期数组，每项包含 `departure` 和 `returnDate`；`requests` 是要精确配对的私人原日期数组，还需要 `route`。
- `stays` 默认 `[7,14,21,30]`，`lowest_pairs` 默认 2 组。若整月全覆盖超过预算，普通窗口按日轮转。
- 每天先核实私有原日期，再给沈阳、北京、大连的 10 个重点航司与路线组合各核实一对去回航班，并核实最低日期候选。当天没有日历时，使用各航线最近仍有效的日历选日期，记录候选来源时刻。日报中的 `pair_coverage`、`candidate_gaps` 分别保存实查状态与缺少候选的目标。重点航司配对每天最多用 20 次，再加 `2 × lowest_pairs` 和原日期的实际配对用量。
- `monthly_limit` 默认 1000，`reserve` 默认 100，`daily_limit` 默认 40。每次查询还受实际账户月余量、小时余量及按剩余日数分配的预算限制。

需在私有仓库手动设置 Variable `ENABLE_PAID_FLIGHTS=true` 和对应 Secret `SEARCHAPI_KEY`，工作流会再次核对付费月度额度。结果只写入该私库的 `private-flight-data/`。按日期命名的 JSON 可导入网站；`one_way_calendar` 与 `filtered_calendar` 各自保留筛选口径，完整观察另按日压缩存档。网页当前支持的完整往返日历与配对沿用 `calendar`、`paired`。
