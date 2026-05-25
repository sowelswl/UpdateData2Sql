import time
import pandas as pd
import sqlalchemy
from datetime import datetime
from utils import (
    connect_to_database,
    load_holidays,
    get_latest_dates,
    update_latest_dates,
    get_source_info,
    is_trading,
)
from data_sources import (
    MootdxDataFetcher,
    eastmoney_stock_info,
    fetch_mootdx_daily,
    fetch_tencent_daily,
    fetch_margin_daily,
)


# ==============================================================================
#                               建表
# ==============================================================================

CREATE_INFO = """
CREATE TABLE IF NOT EXISTS `stock_info_config` (
    `code`         VARCHAR(20) NOT NULL,
    `data_type`    VARCHAR(30) NOT NULL,
    `updated_date` DATE DEFAULT NULL,
    PRIMARY KEY (`code`, `data_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

CREATE_DATA = """
CREATE TABLE IF NOT EXISTS `stock_daily` (
    `date`            DATE NOT NULL,
    `stock_code`      VARCHAR(20) NOT NULL,
    -- mootdx 日线 OHLCV
    `open`            DOUBLE DEFAULT NULL,
    `high`            DOUBLE DEFAULT NULL,
    `low`             DOUBLE DEFAULT NULL,
    `close`           DOUBLE DEFAULT NULL,
    `volume`          DOUBLE DEFAULT NULL,
    `amt`             DOUBLE DEFAULT NULL,
    `pct_chg`         DOUBLE DEFAULT NULL,
    -- 腾讯行情（估值/市值/涨跌）
    `price`           DOUBLE DEFAULT NULL,
    `last_close`      DOUBLE DEFAULT NULL,
    `change_amt`      DOUBLE DEFAULT NULL,
    `change_pct`      DOUBLE DEFAULT NULL,
    `amount_wan`      DOUBLE DEFAULT NULL,
    `turnover_pct`    DOUBLE DEFAULT NULL,
    `pe_ttm`          DOUBLE DEFAULT NULL,
    `pe_static`       DOUBLE DEFAULT NULL,
    `pb`              DOUBLE DEFAULT NULL,
    `amplitude_pct`   DOUBLE DEFAULT NULL,
    `mcap_yi`         DOUBLE DEFAULT NULL,
    `float_mcap_yi`   DOUBLE DEFAULT NULL,
    `limit_up`        DOUBLE DEFAULT NULL,
    `limit_down`      DOUBLE DEFAULT NULL,
    `vol_ratio`       DOUBLE DEFAULT NULL,
    -- 融资融券
    `rzye`            DOUBLE DEFAULT NULL,
    `rzmre`           DOUBLE DEFAULT NULL,
    `rzche`           DOUBLE DEFAULT NULL,
    `rqye`            DOUBLE DEFAULT NULL,
    `rqmcl`           DOUBLE DEFAULT NULL,
    `rqchl`           DOUBLE DEFAULT NULL,
    `rzrqye`          DOUBLE DEFAULT NULL,
    PRIMARY KEY (`date`, `stock_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

CREATE_BASIC = """
CREATE TABLE IF NOT EXISTS `stock_basic_info` (
    `code`          VARCHAR(20) NOT NULL PRIMARY KEY,
    `name`          VARCHAR(100) DEFAULT NULL,
    `industry`      VARCHAR(100) DEFAULT NULL,
    `total_shares`  BIGINT DEFAULT 0,
    `float_shares`  BIGINT DEFAULT 0,
    `mcap`          BIGINT DEFAULT 0,
    `float_mcap`    BIGINT DEFAULT 0,
    `list_date`     VARCHAR(20) DEFAULT NULL,
    `price`         DOUBLE DEFAULT 0,
    `updated_date`  DATE DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def init_tables(engine):
    with engine.connect() as conn:
        with conn.begin():
            for sql in [CREATE_INFO, CREATE_DATA, CREATE_BASIC]:
                conn.execute(sqlalchemy.text(sql))


def seed_config(engine, codes):
    """首次运行：写入默认标的到 stock_info_config"""
    with engine.connect() as conn:
        with conn.begin():
            for code in codes:
                for dtype in ["mootdx_klines", "tencent_quote", "margin_trading", "stock_info"]:
                    try:
                        conn.execute(sqlalchemy.text(
                            "INSERT IGNORE INTO stock_info_config (code, data_type) VALUES (:c, :d)"
                        ), {"c": code, "d": dtype})
                    except Exception:
                        pass


# ==============================================================================
#                               主程序入口
# ==============================================================================

def main():
    """程序的主执行函数"""

    # ---- 默认标的（首次运行时自动写入 info 表，之后从 info 表读） ----
    DEFAULT_CODES = ["600519", "000858", "688017", "300476","601991"]

    # ---- 定义表名 ----
    table_name = "stock_daily"
    info_name = "stock_info_config"

    # ---- 交易日判断 ----
    holiday_path = "Chinese_special_holiday.txt"
    holidays = load_holidays(holiday_path)

    today_str = datetime.now().strftime("%Y-%m-%d")

    # ---- 执行数据处理流程 ----
    engine = connect_to_database()
    init_tables(engine)

    # 首次运行：种子数据写入 info 表
    seed_config(engine, DEFAULT_CODES)

    # 1. 从 data 表获取各标的已有最新日期
    latest_dates_df = get_latest_dates(engine, table_name, group_by_field="stock_code")

    # 2. 同步到 info 表
    if not latest_dates_df.empty:
        latest_dates_df.rename(columns={"stock_code": "code"}, inplace=True)
        update_latest_dates(engine, latest_dates_df, info_name)

    # 3. 读取 info 表，获取标的列表
    info_df = get_source_info(engine, info_name)
    if info_df.empty:
        print(f"警告: {info_name} 表为空，请检查 seed_config")
        return
    info_df["updated_date"] = pd.to_datetime(info_df["updated_date"])

    codes = sorted(info_df["code"].unique())
    print(f"从 {info_name} 读取到 {len(codes)} 个标的: {codes}")

    latest_dates_dict = info_df.set_index("code")["updated_date"].to_dict()

    # ---- 初始化数据源 ----
    fetcher = MootdxDataFetcher()

    # ---- 腾讯行情（批量拉一次） ----
    print("\n--- 腾讯实时行情 ---")
    tencent_df = fetch_tencent_daily(codes, today_str)
    print(f"  获取到 {len(tencent_df)} 条")

    # ---- 逐标的获取数据，按数据源分开收集 ----
    all_mootdx = []
    all_margin = []
    all_basic_rows = []

    for code in codes:
        print(f"\n--- 处理 {code} ---")
        latest_date = latest_dates_dict.get(code)

        # mootdx 日线
        try:
            klines = fetch_mootdx_daily(fetcher, code, latest_date)
            if not klines.empty:
                all_mootdx.append(klines)
                print(f"  mootdx 日线: {len(klines)} 行")
        except Exception as e:
            print(f"  mootdx 日线失败: {e}")

        # 融资融券
        try:
            margin_df = fetch_margin_daily(code)
            if not margin_df.empty:
                all_margin.append(margin_df)
                print(f"  融资融券: {len(margin_df)} 行")
        except Exception as e:
            print(f"  融资融券失败: {e}")

        # 基本面
        try:
            info = eastmoney_stock_info(code)
            info["updated_date"] = today_str
            all_basic_rows.append(info)
            print(f"  基本面: {info['name']} {info['industry']}")
        except Exception as e:
            print(f"  基本面失败: {e}")

        time.sleep(0.5)

    # ---- 各数据源独立 upsert，互不踩踏 ----
    def clean_df(df: pd.DataFrame) -> pd.DataFrame:
        """清洗 DataFrame:去重列、过滤非交易日、NaN→None"""
        df = df.loc[:, ~df.columns.duplicated()]
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[df["date"].apply(lambda d: is_trading(d, holidays))]
        return df

    def upsert_columns(engine, table: str, df: pd.DataFrame, keys=("date", "stock_code")):
        """将 DataFrame 按列 upsert 到表，只更新 DataFrame 中存在的列"""
        if df.empty:
            return
        cols = [c for c in df.columns if c not in keys]
        cols_str = ", ".join(f"`{c}`" for c in df.columns)
        placeholders = ", ".join(f":{c}" for c in df.columns)
        update_clause = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in cols)
        sql = f"INSERT INTO `{table}` ({cols_str}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_clause}"
        data = df.astype(object).where(df.notna(), None).to_dict("records")
        with engine.connect() as conn:
            with conn.begin():
                conn.execute(sqlalchemy.text(sql), data)

    # 写入 mootdx 日线
    if all_mootdx:
        mootdx_df = clean_df(pd.concat(all_mootdx, ignore_index=True))
        print(f"\n--- 写入 mootdx 日线: {len(mootdx_df)} 行 ---")
        upsert_columns(engine, table_name, mootdx_df)

    # 写入融资融券
    if all_margin:
        margin_df = clean_df(pd.concat(all_margin, ignore_index=True))
        print(f"--- 写入融资融券: {len(margin_df)} 行 ---")
        upsert_columns(engine, table_name, margin_df)

    # 写入腾讯行情
    if not tencent_df.empty:
        tencent_df = clean_df(tencent_df)
        print(f"--- 写入腾讯行情: {len(tencent_df)} 行 ---")
        upsert_columns(engine, table_name, tencent_df)

    # ---- 写入 stock_basic_info ----
    if all_basic_rows:
        basic_df = pd.DataFrame(all_basic_rows)
        with engine.connect() as conn:
            for _, row in basic_df.iterrows():
                row_dict = row.astype(object).where(row.notna(), None).to_dict()
                conn.execute(sqlalchemy.text("""
                    INSERT INTO stock_basic_info
                        (code, name, industry, total_shares, float_shares,
                         mcap, float_mcap, list_date, price, updated_date)
                    VALUES (:code, :name, :industry, :total_shares, :float_shares,
                            :mcap, :float_mcap, :list_date, :price, :updated_date)
                    ON DUPLICATE KEY UPDATE
                        name=VALUES(name), industry=VALUES(industry),
                        total_shares=VALUES(total_shares),
                        float_shares=VALUES(float_shares),
                        mcap=VALUES(mcap), float_mcap=VALUES(float_mcap),
                        list_date=VALUES(list_date), price=VALUES(price),
                        updated_date=VALUES(updated_date)
                """), row_dict)
            conn.commit()
        print("stock_basic_info 写入完成。")

    # ---- 最后再同步一次更新日期 ----
    latest_dates_df = get_latest_dates(engine, table_name, group_by_field="stock_code")
    if not latest_dates_df.empty:
        latest_dates_df.rename(columns={"stock_code": "code"}, inplace=True)
        update_latest_dates(engine, latest_dates_df, info_name)


if __name__ == "__main__":
    main()
