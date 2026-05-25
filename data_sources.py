import time
import random
import urllib.request
import pandas as pd
import requests
from mootdx.quotes import Quotes

UA = "Mozilla/5.0"

# 绕过系统代理
_session = requests.Session()
_session.trust_env = False


def _get_with_retry(url, **kwargs):
    """带重试的 GET 请求，断连时换新 session，指数退避+抖动"""
    params = kwargs.pop("params", None)
    timeout = kwargs.pop("timeout", 10)
    for attempt in range(4):
        try:
            r = _session.get(url, params=params, timeout=timeout, **kwargs)
            r.raise_for_status()
            return r
        except Exception:
            if attempt == 3:
                raise
            # 断连后换新 session，避免复用死连接
            if attempt >= 1:
                _new_session()
            time.sleep(2 ** attempt + random.uniform(0, 1))


def _new_session():
    global _session
    try:
        _session.close()
    except Exception:
        pass
    _session = requests.Session()
    _session.trust_env = False

_proxy_handler = urllib.request.ProxyHandler({})
_no_proxy_opener = urllib.request.build_opener(_proxy_handler)

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


# ==============================================================================
#                               工具函数
# ==============================================================================

def get_prefix(code: str) -> str:
    """6位代码 → 市场前缀"""
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    else:
        return "sz"


# ==============================================================================
#                          东财数据中心通用接口
# ==============================================================================

def eastmoney_datacenter(report_name: str, columns: str = "ALL",
                         filter_str: str = "", page_size: int = 50,
                         sort_columns: str = "", sort_types: str = "-1") -> list[dict]:
    """东财数据中心统一查询"""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    headers = {"User-Agent": UA, "Referer": "https://data.eastmoney.com/"}
    r = _get_with_retry(DATACENTER_URL, params=params, headers=headers, timeout=15) 
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ==============================================================================
#                            融资融券
# ==============================================================================

def margin_trading(code: str, page_size: int = 30) -> list[dict]:
    """
    融资融券明细（日级）。
    返回: [{date, rzye(融资余额), rzmre(融资买入), rqye(融券余额), ...}]
    """
    data = eastmoney_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{code}")',
        page_size=page_size,
        sort_columns="DATE", sort_types="-1",
    )
    rows = []
    for row in data:
        rows.append({
            "date": str(row.get("DATE", ""))[:10],
            "rzye": row.get("RZYE", 0),
            "rzmre": row.get("RZMRE", 0),
            "rzche": row.get("RZCHE", 0),
            "rqye": row.get("RQYE", 0),
            "rqmcl": row.get("RQMCL", 0),
            "rqchl": row.get("RQCHL", 0),
            "rzrqye": row.get("RZRQYE", 0),
        })
    return rows


# ==============================================================================
#                          东财个股基本面
# ==============================================================================

def eastmoney_stock_info(code: str) -> dict:
    """
    东财个股基本面信息。
    返回: {code, name, industry, total_shares, float_shares, mcap, float_mcap, list_date, price}
    """
    market_code = 1 if code.startswith(("6", "9")) else 0
    server_node = random.randint(1, 99)
    url = f"http://{server_node}.push2.eastmoney.com/api/qt/stock/get"
    params = {
        "fltt": "2", "invt": "2",
        "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
        "secid": f"{market_code}.{code}",
    }
    r = _get_with_retry(url, params=params, headers={"User-Agent": UA, "Referer": "http://quote.eastmoney.com/"})
    d = r.json().get("data", {})
    return {
        "code": d.get("f57", ""),
        "name": d.get("f58", ""),
        "industry": d.get("f127", ""),
        "total_shares": d.get("f84", 0),
        "float_shares": d.get("f85", 0),
        "mcap": d.get("f116", 0),
        "float_mcap": d.get("f117", 0),
        "list_date": str(d.get("f189", "")),
        "price": d.get("f43", 0),
    }


# ==============================================================================
#                          腾讯财经实时行情
# ==============================================================================

def tencent_quote(codes: list[str]) -> dict[str, dict]:
    """
    批量拉取腾讯财经实时行情。
    codes: ["688017", "300476", "002463"]
    也支持指数/ETF。
    返回: {code: {name, price, pe_ttm, pb, mcap, ...}}
    """
    prefixed = []
    for c in codes:
        if c.startswith(("6", "9")):
            prefixed.append(f"sh{c}")
        elif c.startswith("8"):
            prefixed.append(f"bj{c}")
        else:
            prefixed.append(f"sz{c}")

    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    resp = _no_proxy_opener.open(req, timeout=10)
    data = resp.read().decode("gbk")

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        result[code] = {
            "name":         vals[1],
            "price":        float(vals[3]) if vals[3] else 0,
            "last_close":   float(vals[4]) if vals[4] else 0,
            "open":         float(vals[5]) if vals[5] else 0,
            "change_amt":   float(vals[31]) if vals[31] else 0,
            "change_pct":   float(vals[32]) if vals[32] else 0,
            "high":         float(vals[33]) if vals[33] else 0,
            "low":          float(vals[34]) if vals[34] else 0,
            "amount_wan":   float(vals[37]) if vals[37] else 0,
            "turnover_pct": float(vals[38]) if vals[38] else 0,
            "pe_ttm":       float(vals[39]) if vals[39] else 0,
            "amplitude_pct":float(vals[43]) if vals[43] else 0,
            "mcap_yi":      float(vals[44]) if vals[44] else 0,
            "float_mcap_yi":float(vals[45]) if vals[45] else 0,
            "pb":           float(vals[46]) if vals[46] else 0,
            "limit_up":     float(vals[47]) if vals[47] else 0,
            "limit_down":   float(vals[48]) if vals[48] else 0,
            "vol_ratio":    float(vals[49]) if vals[49] else 0,
            "pe_static":    float(vals[52]) if vals[52] else 0,
        }
    return result


# ==============================================================================
#                          Mootdx 行情数据获取
# ==============================================================================

class MootdxDataFetcher:
    """Mootdx 标准行情数据获取器，管理连接并提供日线/报价/逐笔接口。"""

    def __init__(self):
        self.client = None
        try:
            self.client = Quotes.factory(market='std')
        except Exception as e:
            print(f"mootdx 连接失败: {e}")

    def get_daily_kline(self, code: str, offset: int = 2) -> pd.DataFrame:
        """
        获取单只股票日线，返回最近 offset 根 K 线。
        code 支持 6 位纯数字或 "600519.SH" 格式。
        """
        if not self.client:
            return pd.DataFrame()

        pure_code = code.split('.')[0]

        try:
            df = self.client.bars(symbol=pure_code, category=4, offset=offset)
            if df is None or df.empty:
                return pd.DataFrame()

            # mootdx 返回的 DataFrame 以 datetime 为索引，但可能同时存在同名列
            # 先删掉列中的 datetime（索引的副本），再 reset_index 避免列名冲突
            for c in list(df.columns):
                if c.lower() == "datetime":
                    del df[c]
            df = df.reset_index()
            rename_map = {}
            for c in df.columns:
                cl = c.lower()
                if cl == "datetime":
                    rename_map[c] = "date"
                elif cl == "vol":
                    rename_map[c] = "volume"
                elif cl == "amount":
                    rename_map[c] = "amt"
            df.rename(columns=rename_map, inplace=True)
            df["date"] = pd.to_datetime(df["date"])

            cols = ["date", "open", "high", "low", "close", "volume", "amt"]
            cols = [c for c in cols if c in df.columns]
            df = df[cols].copy()
            df["stock_code"] = code
            return df

        except Exception as e:
            print(f"获取 {code} K线失败: {e}")
            return pd.DataFrame()


# ==============================================================================
#                    对外适配函数（供 sync_stock_daily 调用）
# ==============================================================================

def fetch_mootdx_daily(fetcher: MootdxDataFetcher, code: str,
                       latest_date=None) -> pd.DataFrame:
    """获取 mootdx 日线，增量或全量，返回宽表 DataFrame"""
    is_new = (latest_date is None or latest_date is pd.NaT)
    offset = 800 if is_new else 30
    df = fetcher.get_daily_kline(code, offset=offset)
    if df.empty:
        return df
    df = df.sort_values("date")
    df["pct_chg"] = df["close"].pct_change() * 100
    if not is_new and latest_date is not None:
        df = df[df["date"] > pd.to_datetime(latest_date)]
    return df


def fetch_tencent_daily(codes: list[str], today_str: str) -> pd.DataFrame:
    """批量获取腾讯行情 → 宽表 DataFrame"""
    rows = []
    try:
        all_q = tencent_quote(codes)
        for code, q in all_q.items():
            row = {"date": today_str, "stock_code": code}
            skip = {"name", "open", "high", "low"}
            for k, v in q.items():
                if k not in skip:
                    row[k] = float(v) if v else None
            rows.append(row)
    except Exception as e:
        print(f"腾讯行情失败: {e}")
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def fetch_margin_daily(code: str) -> pd.DataFrame:
    """获取融资融券数据 → 宽表 DataFrame"""
    data = margin_trading(code)
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"])
    df["stock_code"] = code
    return df
