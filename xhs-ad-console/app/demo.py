import csv
import io
from datetime import date, timedelta

from app.db import connect, password_hash
from app.reports import import_report

DEMO_DATE = date(2026, 11, 20)
PASSWORD = "DemoConsole!2026"
DOCS = [
    ("素材点击率下降排查","素材 SOP §2","点击率 CTR 下降时，先按素材拆分曝光和点击。区分素材疲劳、人群扩张和投放位置变化，再用同定向做封面对照。"),
    ("线索下降与落地页自查","线索复盘 §1","点击正常但线索减少时，先实测落地页表单、跳转和私信按钮。对比各计划点击转线索率，记录承接话术和页面变更。"),
    ("预算与曝光掉量的排查顺序","预算 SOP §1","曝光掉量先查预算撞线、暂停和审核状态，再看 CPM、出价和定向变化。确认配置后小范围调整预算。"),
    ("成交与订单归因核对","电商周报 SOP","成交下降时核对订单同步和归因窗口。查看商品库存、价格变更和从点击到成交的数据。"),
    ("报表口径检查","日报 SOP §1","同账户同日期同窗口对账。读取计划明细后排除合计行。用加总后的点击和曝光算 CTR。核对是否包含自然流量。"),
]


def sample_csv(offset=0):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["日期","计划名称","消费","展现量","点击量","直播间留资量","成交订单数","支付金额","互动量"])
    for i in range(28):
        day = DEMO_DATE - timedelta(days=27-i)
        for j, plan in enumerate(("上新种草","店铺引流","关键词触达")):
            impressions = 16000 + offset + 1100*j + 250*(i%7)
            clicks = impressions*(13+j)//1000
            if i == 27 and offset == 0:
                clicks //= 2
            leads = clicks//(17+j)
            orders = leads//3
            writer.writerow([day,plan,round((2000+155*j+17*(i%5))/10,2),
                             impressions,clicks,leads,orders,orders*159,clicks//4])
    return output.getvalue().encode("utf-8-sig")


def seed():
    with connect() as db:
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise SystemExit("请在空数据库中初始化演示数据")
        for tenant, account, name in [("demo-a","account-a","账户 A"),("demo-b","account-b","账户 B")]:
            db.execute("INSERT INTO tenants(id,name) VALUES(?,?)",(tenant,f"演示租户 {name[-1]}"))
            db.execute("INSERT INTO accounts VALUES(?,?,?)",(account,tenant,name))
        for name,tenant,role,account in [
            ("demo-manager","demo-a","manager","account-a"),("demo-operator","demo-a","operator","account-a"),
            ("demo-client","demo-a","client","account-a"),("demo-other","demo-b","manager","account-b")
        ]:
            user_id = db.execute("INSERT INTO users(name,password,tenant_id,role) VALUES(?,?,?,?)",
                                 (name,password_hash(PASSWORD),tenant,role)).lastrowid
            db.execute("INSERT INTO access VALUES(?,?)",(user_id,account))
        for tenant in ("demo-a","demo-b"):
            db.executemany("INSERT INTO documents(tenant_id,title,source,body) VALUES(?,?,?,?)",
                           [(tenant,*doc) for doc in DOCS])
    for name,account,offset in [("demo-manager","account-a",0),("demo-other","account-b",1800)]:
        with connect() as db:
            user = db.execute("SELECT * FROM users WHERE name=?",(name,)).fetchone()
            import_report(db,user,account,sample_csv(offset),"聚光")
