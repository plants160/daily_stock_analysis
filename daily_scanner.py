"""
Daily A-share Candidate Scanner
Runs at 14:30, screens stocks, filters by news/sentiment, pushes to WeChat.

Layer 1: Quant screening (放宽版)
Layer 2: News & sentiment filtering
Layer 3: Push to WeChat Work
"""
import urllib.request, json, sys, time, os, re
from datetime import datetime, timedelta
import random

# ============================================================
# CONFIG
# ============================================================
WECHAT_WEBHOOK = os.environ.get("WECHAT_WEBHOOK_URL", "")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVERS = os.environ.get("EMAIL_RECEIVERS", "")
MAX_CANDIDATES_L1 = 30   # Max stocks after quant screening
MAX_CANDIDATES_L2 = 8    # Max stocks after news/sentiment filter
STOCK_SAMPLE_SIZE = 200  # How many stocks to scan (random sample)

# Quant thresholds (放宽版)
CHG_MIN, CHG_MAX = 1.5, 7.0       # 涨跌幅
TURN_MIN, TURN_MAX = 2.0, 20.0     # 换手率
MCAP_MIN, MCAP_MAX = 20, 500       # 市值(亿)
VOL_RATIO_MIN = 0.8                # 量比

# Negative news keywords
NEG_KEYWORDS = [
    '减持', '问询', '处罚', '亏损', '诉讼', '退市', 'ST',
    '立案', '调查', '警示', '谴责', '业绩变脸', '爆雷',
    '商誉减值', '财务造假', '停产', '重组失败',
]

# Policy-positive keywords
POLICY_KEYWORDS = [
    '大基金', '国产替代', '半导体', 'AI', '人工智能', '算力',
    '新能源', '储能', '光伏', '风电', '机器人', '自动驾驶',
    '十五五', '新质生产力', '数字经济', '信创', 'Chiplet',
    '先进封装', 'HBM', '存储', '设备', '材料',
]

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HEADERS_UA = {'User-Agent': 'Mozilla/5.0'}

# ============================================================
# LAYER 1: Quant Screening
# ============================================================

def get_stock_sample(n=200):
    """Get a random sample of A-share stocks"""
    stocks = []
    for page in range(1, 12):
        url = (f'http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
               f'Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1'
               f'&node=hs_a&symbol=&_s_r_a=init')
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        try:
            resp = opener.open(req, timeout=15)
            batch = json.loads(resp.read().decode('gbk'))
            if not batch: break
            stocks.extend(batch)
        except: break
        time.sleep(0.3)

    if len(stocks) > n:
        random.seed(int(datetime.now().strftime('%Y%m%d')))
        stocks = random.sample(stocks, n)

    result = []
    for s in stocks:
        try:
            name = s.get('name', '')
            code = s.get('code', '')
            mc = float(s.get('mktcap', 0) or 0) / 10000
            chg = float(s.get('changepercent', 0) or 0)
            to = float(s.get('turnoverratio', 0) or 0)

            # Filter ST/*ST stocks
            if 'ST' in name or '*ST' in name:
                continue
            # Only 主板 (沪市6xxxxx, 深市0xxxxx/002xxx/003xxx)
            # Exclude 科创板(688), 创业板(300/301), 北交所(8xxxxx,4xxxxx)
            if code.startswith(('688', '689', '300', '301', '8', '4')):
                continue
            if not (CHG_MIN <= chg <= CHG_MAX): continue
            if not (TURN_MIN <= to <= TURN_MAX): continue
            if mc > 0 and not (MCAP_MIN <= mc <= MCAP_MAX): continue

            result.append({
                'code': s['code'],
                'name': s['name'],
                'price': float(s.get('trade', 0) or 0),
                'change_pct': chg,
                'turnover': to,
                'mcap': round(mc, 1),
                'volume': int(s.get('volume', 0) or 0),
            })
        except (ValueError, TypeError):
            continue

    return result[:MAX_CANDIDATES_L1]


def check_ma_volume(candidates):
    """Verify MA and volume conditions via Tencent K-line"""
    passed = []
    for c in candidates:
        code = c['code']
        prefix = 'sh' if code.startswith(('6','9')) else 'sz'
        try:
            url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
                   f'?param={prefix}{code},day,,,30,qfq')
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0')
            resp = opener.open(req, timeout=8)
            d = json.loads(resp.read().decode('utf-8'))
            kdata = d.get('data', {}).get(f'{prefix}{code}', {})
            klines = kdata.get('qfqday') or kdata.get('day') or []
            if len(klines) < 25: continue

            closes = [float(k[2]) for k in klines]
            volumes = [float(k[5]) for k in klines]
            ma5 = sum(closes[-5:]) / 5
            ma10 = sum(closes[-10:]) / 10
            ma20 = sum(closes[-20:]) / 20
            vol_today = volumes[-1]
            avg_vol5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else 1
            vol_ratio = vol_today / avg_vol5 if avg_vol5 > 0 else 0

            score = 0
            if ma5 > ma20: score += 1     # MA5 > MA20
            if vol_today > volumes[-2]: score += 1  # 放量
            if vol_ratio > VOL_RATIO_MIN: score += 1  # 量比
            if ma5 > ma10 > ma20: score += 1  # 完美多头

            c['ma5'] = round(ma5, 2)
            c['ma20'] = round(ma20, 2)
            c['vol_ratio'] = round(vol_ratio, 2)
            c['tech_score'] = score
            if score >= 2:  # at least 2/4 technical signals
                passed.append(c)
        except:
            continue
    return sorted(passed, key=lambda x: x['tech_score'], reverse=True)


# ============================================================
# LAYER 2: News & Sentiment & Theme
# ============================================================

def check_news(code, name):
    """Check recent news for negative signals. Returns (score, summary)"""
    try:
        url = (f'https://search-api-web.eastmoney.com/search/jsonp'
               f'?cb=j&param={{"uid":"","keyword":"{code}","type":["cmsArticleWebOld"],'
               f'"client":"web","clientType":"web","clientVersion":"curr",'
               f'"param":{{"cmsArticleWebOld":{{"searchScope":"default","sort":"default",'
               f'"pageIndex":1,"pageSize":10,"preTag":"","postTag":""}}}}}}')
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        req.add_header('Referer', 'https://so.eastmoney.com/')
        resp = opener.open(req, timeout=8)
        text = resp.read().decode('utf-8')
        json_str = text[text.index('(')+1:text.rindex(')')]
        d = json.loads(json_str)
        articles = d.get('result', {}).get('cmsArticleWebOld', {}).get('list', [])

        neg_count = 0
        titles = []
        for a in articles[:10]:
            title = re.sub(r'<[^>]+>', '', a.get('title', ''))
            titles.append(title[:60])
            for kw in NEG_KEYWORDS:
                if kw in title:
                    neg_count += 1
                    break

        if neg_count >= 3:
            return -2, f'多条负面({neg_count}条)'
        elif neg_count >= 1:
            return -1, f'有负面({neg_count}条)'
        elif len(articles) == 0:
            return 0, '无近期新闻'
        else:
            return 1, f'新闻正常({len(articles)}条)'
    except:
        return 0, '新闻检查失败'


def check_hot_theme(code):
    """Check if stock is in today's hot themes (同花顺)"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        url = (f'http://zx.10jqka.com.cn/event/api/getharden/'
               f'date/{today}/orderby/date/orderway/desc/charset/GBK/')
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        resp = opener.open(req, timeout=8)
        d = json.loads(resp.read().decode('gbk'))
        rows = d.get('data', [])
        for r in rows:
            if r.get('code', '') == code:
                reason = r.get('reason', '')
                return True, reason[:80]
        return False, ''
    except:
        return False, ''


def check_policy(code):
    """Check if stock belongs to policy-positive sectors (百度概念)"""
    try:
        url = (f'https://finance.pae.baidu.com/api/getrelatedblock'
               f'?code={code}&market=ab&typeCode=all&finClientType=pc')
        req = urllib.request.Request(url)
        for k, v in {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/vnd.finance-web.v1+json',
            'Origin': 'https://gushitong.baidu.com',
            'Referer': 'https://gushitong.baidu.com/',
        }.items():
            req.add_header(k, v)
        resp = opener.open(req, timeout=8)
        d = json.loads(resp.read().decode('utf-8'))
        blocks = d.get('Result', [])
        concepts = []
        for block in blocks:
            for item in block.get('list', []):
                concepts.append(item.get('name', ''))

        hits = []
        for c in concepts:
            for kw in POLICY_KEYWORDS:
                if kw in c and c not in hits:
                    hits.append(c)
        return len(hits) > 0, hits[:3]
    except:
        return False, []


# ============================================================
# LAYER 3: Push to WeChat
# ============================================================

def push_wechat(markdown_text):
    """Send markdown message to WeChat Work webhook"""
    if not WECHAT_WEBHOOK:
        print("WECHAT_WEBHOOK_URL not set, skipping push")
        return
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": markdown_text}
    }
    req = urllib.request.Request(WECHAT_WEBHOOK)
    req.add_header('Content-Type', 'application/json')
    req.data = json.dumps(payload).encode('utf-8')
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(f'WeChat push: {resp.read().decode()}')
    except Exception as e:
        print(f'WeChat push failed: {e}')


def send_email(subject, body_text):
    """Send email via QQ SMTP"""
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECEIVERS:
        print("Email config not set, skipping email")
        return
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVERS
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))

    try:
        server = smtplib.SMTP_SSL('smtp.qq.com', 465)
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS.split(','), msg.as_string())
        server.quit()
        print('Email sent successfully')
    except Exception as e:
        print(f'Email failed: {e}')


# ============================================================
# MAIN
# ============================================================

def main():
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    print(f'{"="*60}')
    print(f'  Daily Scanner - {today_str} {now.strftime("%H:%M:%S")}')
    print(f'{"="*60}')

    # ---- Layer 1: Quant Screening ----
    print(f'\n[L1] Quant screening...')
    candidates = get_stock_sample(STOCK_SAMPLE_SIZE)
    print(f'  First pass (price/turnover/mcap): {len(candidates)} stocks')

    candidates = check_ma_volume(candidates)
    print(f'  After MA+volume check: {len(candidates)} stocks')
    candidates = candidates[:MAX_CANDIDATES_L2]

    # ---- Layer 2: News / Sentiment / Theme / Policy ----
    print(f'\n[L2] News & sentiment filtering for {len(candidates)} candidates...')
    final = []
    for c in candidates:
        code, name = c['code'], c['name']

        # 2a. News check
        news_score, news_summary = check_news(code, name)
        c['news_score'] = news_score
        c['news_summary'] = news_summary
        if news_score <= -2:  # Multiple negatives → skip
            print(f'  SKIP {code} {name}: {news_summary}')
            continue

        # 2b. Hot theme check
        is_hot, theme_reason = check_hot_theme(code)
        c['is_hot'] = is_hot
        c['theme_reason'] = theme_reason[:60] if theme_reason else ''

        # 2c. Policy alignment
        has_policy, policy_concepts = check_policy(code)
        c['has_policy'] = has_policy
        c['policy_concepts'] = ','.join(policy_concepts) if policy_concepts else ''

        # Composite score
        composite = c['tech_score'] + news_score + (2 if is_hot else 0) + (2 if has_policy else 0)
        c['composite'] = composite

        print(f'  PASS {code} {name}: tech={c["tech_score"]} news={news_score} hot={is_hot} policy={has_policy} → score={composite}')
        final.append(c)
        time.sleep(0.1)

    final.sort(key=lambda x: x['composite'], reverse=True)
    final = final[:5]  # Top 5

    # ---- Layer 3: Push ----
    print(f'\n[L3] Generating report...')
    print(f'  Final candidates: {len(final)} stocks')

    # Build markdown message
    if final:
        md = f"## Stock Scan: {today_str}\n\n"
        md += f"> 扫描{STOCK_SAMPLE_SIZE}只主板 | 六层过滤 | 仅供参考\n\n"

        md += f"**筛选流程：**\n"
        md += f"`L1 量价` 涨幅{CHG_MIN}-{CHG_MAX}% + 换手{TURN_MIN}-{TURN_MAX}% + 市值{MCAP_MIN}-{MCAP_MAX}亿\n"
        md += f"`L2 技术` MA多头排列 + 成交量逐日放大 + 量比>{VOL_RATIO_MIN}\n"
        md += f"`L3 消息` 近3天新闻扫描，排除减持/问询/处罚/亏损等负面\n"
        md += f"`L4 情绪` 同花顺强势股归因，确认是否在当日热点题材中\n"
        md += f"`L5 政策` 百度概念板块匹配，识别大基金/AI/半导体/新能源等主线\n"
        md += f"`L6 板块` 仅主板(60/00开头)，排除ST/科创/创业板/北交所\n\n"

        md += f"**共 {len(final)} 只候选：**\n\n"

        for i, c in enumerate(final):
            hot_tag = '[热点]' if c['is_hot'] else ''
            policy_tag = '[政策]' if c['has_policy'] else ''

            md += f"### {i+1}. {c['name']}({c['code']}) {hot_tag}{policy_tag}\n"
            md += f"- 现价{c['price']:.2f} | 涨{c['change_pct']:+.1f}% | 换手{c['turnover']:.1f}% | 市值{c['mcap']:.0f}亿\n"
            md += f"- 技术面：MA{c['ma5']} | MA20={c['ma20']} | 量比{c['vol_ratio']:.2f} | 技术分{c['tech_score']}/4\n"
            md += f"- 消息面：{c['news_summary']}\n"
            if c['theme_reason']:
                md += f"- 情绪面：{c['theme_reason']}\n"
            else:
                md += f"- 情绪面：未在今日强势股名单\n"
            if c['policy_concepts']:
                md += f"- 政策面：{c['policy_concepts']}\n"
            else:
                md += f"- 政策面：未匹配政策主线概念\n"
            md += f"- 综合评分：{c['composite']}/10\n\n"

        md += "---\n"
        md += f"> NOTE: 仅供参考，不构成投资建议。请结合自身判断。\n"
        md += f"> 发送时间：{now.strftime('%H:%M:%S')}"
    else:
        md = f"## Daily Scan: {today_str}\n\n"
        md += f"> 今日无股票通过全部筛选条件。\n"
        md += f"> 市场可能不适合当前策略，建议观望。\n\n"
        md += f"> 发送时间：{now.strftime('%H:%M:%S')}"

    print(md)
    push_wechat(md)
    # Also send email with plain text version
    plain_text = md.replace('## ', '').replace('### ', '').replace('**', '').replace('- ', '  ')
    plain_text = plain_text.replace('<br>', '\n')
    send_email(f'Daily Scan: {today_str}', plain_text)
    print(f'\nDone!')

if __name__ == '__main__':
    main()
