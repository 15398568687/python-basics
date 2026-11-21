import argparse
import getpass

from app.db import connect, initialize, password_hash
from app.demo import seed
from app.server import serve

parser = argparse.ArgumentParser(description="小红书投放报表与诊断平台")
sub = parser.add_subparsers(dest="command", required=True)
sub.add_parser("seed-demo")
server = sub.add_parser("serve")
server.add_argument("--host", default="127.0.0.1")
server.add_argument("--port", default=8080, type=int)
admin = sub.add_parser("create-user")
admin.add_argument("tenant")
admin.add_argument("username")
admin.add_argument("role", choices=["manager", "operator", "client"])
admin.add_argument("accounts", nargs="*")
args = parser.parse_args()
initialize()

if args.command == "seed-demo":
    seed()
    print("已生成合成数据。主管 demo-manager / DemoConsole!2026")
elif args.command == "serve":
    serve(args.host, args.port)
elif args.command == "create-user":
    password = getpass.getpass("设置登录密码（至少 12 字符）：")
    if not 12 <= len(password) <= 256:
        parser.error("密码长度须为 12–256 字符")
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO tenants(id,name) VALUES(?,?)",(args.tenant,args.tenant))
        user = db.execute("INSERT INTO users(name,password,tenant_id,role) VALUES(?,?,?,?)",
                          (args.username,password_hash(password),args.tenant,args.role)).lastrowid
        for account in args.accounts:
            db.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,?)",(account,args.tenant,account))
            belongs = db.execute("SELECT 1 FROM accounts WHERE id=? AND tenant_id=?",(account,args.tenant)).fetchone()
            if not belongs:
                parser.error("账户 ID 已被其他租户使用")
            db.execute("INSERT INTO access VALUES(?,?)",(user,account))
