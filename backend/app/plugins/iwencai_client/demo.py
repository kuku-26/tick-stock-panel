# -*- coding: utf-8 -*-
"""iwencai_client 用法示例: 复制到你的项目后, 修改 API_KEY 即可运行。"""
import logging
import os

from iwencai_client import IWencaiClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

# 推荐从环境变量读取; 临时用可改为 API_KEY = "sk-proj-xxxx"(勿提交 git)
# API_KEY = os.environ["IWENCAI_API_KEY"]
API_KEY = 'sk-proj-00-JJ_ktMiR9qPJ4HIizVW8UQvo4ObMHAkmRfhgBApMAz41Uj3J2GzNgWZ2nsYHZ8SZ5iitI98t0vDmWZdtWKJtUAyt8X8Czj6TrTL-id-q9u0OaOTz3g5MEV3ymz-vx301xrwGuQ'

if __name__ == "__main__":
    client = IWencaiClient()  # timeout/max_pages/retries 等连接级配置在实例化时设置

    df = client.get(
        query="dde大单净量>0%,10日的区间涨跌幅>10%,5日的区间涨跌幅>10%,竞价涨幅<10%且竞价涨幅>0%,量比>0,竞价涨幅>0%，主板，非st",
        api_key=API_KEY,        # 必填
        sort_key="dde大单净量[20260904]",   # 可选: 排序字段(用返回结果的实际列名, 可用列见 KeyError 提示)
        sort_order="desc",      # 可选: asc / desc
        page=1,                 # 可选: 保留参数(当前为全量拉取)
        perpage=100,            # 可选: 每页条数, 默认100, 上限100
    )

    print(f"\n共 {len(df)} 条, 列: {list(df.columns)}")
    print(df.head(10).to_string(index=False))
