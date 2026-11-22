# flight-data

中日机票行情网站的数据仓库。GitHub Actions 每天日本时间 10:17 运行 `scripts/collect.py`，
从 SerpApi 查单程、往返价格日历、官网 / 平台比价和 Google 价格记录，写入 `data/prices.json`。
网站从这个文件的 Raw 地址读数据。

- 密钥：Settings → Secrets and variables → Actions → Secrets → `SERPAPI_KEY`（也支持本仓库已有的 `FLY`）
- 档位：同一页面的 Variables → `PLAN` = `free` / `starter` / `full`（对应 SerpApi 的免费 / Starter / Developer 套餐）
- 手动运行：Actions → 每日采集机票价格 → Run workflow
- 回填历史报价：先完成一次正常采集，再手动勾选“回填”。free 需额外最多 120 次搜索，starter / full 最多 560 次。脚本读取账户剩余额度，按小时限额分批执行。走势覆盖范围取决于实际返回值和提前购买天数。

这个仓库要设成 Public，网站才能读到数据（里面只有价格数据和脚本，没有密钥）。

首次采集前，prices.json 的 rows 为空，网页显示模拟数据。Secrets 中的密钥只在采集任务中使用。

免费档优先覆盖东京往返沈阳、北京、上海六个方向。常规排程最多 246 次/月。报价为整段行程价；航段通过同一机场且航班号与航司一致时标为同航班号经停，换航班、换机场及信息不足分别标注。同航班号不能保证乘客无需下机。
