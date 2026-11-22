# flight-data

中日机票行情网站的数据仓库。GitHub Actions 每天日本时间 10:17 运行 `scripts/collect.py`，
从 SerpApi 查单程、往返价格日历、官网 / 平台比价和 Google 价格记录，写入 `data/prices.json`。
网站从这个文件的 Raw 地址读数据。

- 密钥：Settings → Secrets and variables → Actions → Secrets → `SERPAPI_KEY`
- 档位：同一页面的 Variables → `PLAN` = `free` / `starter` / `full`（对应 SerpApi 的免费 / Starter / Developer 套餐）
- 手动运行：Actions → 每日采集机票价格 → Run workflow
- 回填历史报价：先完成一次正常采集，再手动勾选“回填”。free 需额外最多 80 次搜索，starter / full 最多 560 次。脚本读取账户剩余额度，按小时限额分批执行。走势覆盖范围取决于实际返回值和提前购买天数。

这个仓库要设成 Public，网站才能读到数据（里面只有价格数据和脚本，没有密钥）。

首次采集前，prices.json 的 rows 为空，网页显示模拟数据。Secrets 中的密钥只在采集任务中使用。
