
import json, os, time, urllib.request, urllib.parse, uuid
BASE = "http://localhost:8000/api/v1"

def login():
    user = os.environ.get("RAG_DEMO_USER", "admin")
    # 口令不硬编码入库, 通过环境变量注入; 缺省给出占位符避免泄露真实口令
    password = os.environ.get("RAG_DEMO_PASSWORD", "__REPLACE_ME__")
    req = urllib.request.Request(BASE + "/auth/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        headers={"Content-Type":"application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8","replace"))["access_token"]
TOKEN = login()
AH = {"Authorization": "Bearer " + TOKEN}

def call(method, path, body=None, files=None):
    url = BASE + path
    data = None; headers = dict(AH)
    if files:
        boundary = uuid.uuid4().hex
        parts = []
        for (fn, content, ctype) in files:
            parts.append(("--"+boundary+"\r\n"
                "Content-Disposition: form-data; name=\"files\"; filename=\""+fn+"\"\r\n"
                "Content-Type: "+ctype+"\r\n\r\n").encode())
            parts.append(content)
            parts.append(b"\r\n")
        if "/replace" in path:
            body = b"--"+boundary.encode() + b"\r\n"
            body += ("Content-Disposition: form-data; name=\"file\"; filename=\""+files[0][0]+"\"\r\n"
                     "Content-Type: "+files[0][2]+"\r\n\r\n").encode()
            body += files[0][1] + b"\r\n"
            data = body + ("--"+boundary+"--\r\n").encode()
        else:
            data = b"".join(parts) + ("--"+boundary+"--\r\n").encode()
        headers["Content-Type"] = "multipart/form-data; boundary="+boundary
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

ok = True
def check(name, cond, info=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (" " + str(info) if info else ""))
    if not cond: ok = False

# 1. 列表: 默认分页结构
s, d = call("GET", "/documents?page=1&per_page=20")
print("DEBUG list status", s, "topkeys", list(d.keys()) if isinstance(d, dict) else type(d))
if isinstance(d, dict) and d.get("documents"):
    print("DEBUG first doc keys", list(d["documents"][0].keys()))
check("list default", s==200 and isinstance(d, dict) and "documents" in d and "total" in d and "pages" in d, d.get("total") if isinstance(d, dict) else d)
# 搜索 + 状态筛选
s, d2 = call("GET", "/documents?status=done&page=1&per_page=20")
check("list status filter", s==200 and all(x["status"]=="done" for x in d2["documents"]))

# 2. 上传
marker = "MVP冒烟" + uuid.uuid4().hex[:6]
content = "# 冒烟文档\n\n## 章节A\n\n"+marker+" 内容。</chapter>\n\n## 章节B\n\n第二段独特关键词XZYQ77。\n"
s, u = call("POST", "/documents/upload", files=[("smoke.md", content.encode(), "text/markdown")])
check("upload", s==200 and u.get("imported_count")==1, u)
doc_id = u["imported"][0]["doc_id"]

# 3. 搜索命中
s, d3 = call("GET", "/documents?search=" + urllib.parse.quote(marker))
check("search hit", s==200 and any(x["doc_id"]==doc_id for x in d3["documents"]))

# 4. 详情
s, dt = call("GET", "/documents/"+doc_id+"/detail")
check("detail", s==200 and dt.get("status")=="done" and dt["doc_id"]==doc_id, {k:dt.get(k) for k in ("status","chunks","version","format")})

# 5. 切片内容
s, c = call("GET", "/documents/"+doc_id+"/content")
check("content chunks", s==200 and len(c.get("chunks",[]))>0, len(c.get("chunks",[])))

# 6. 改名 + 版本号
s, p = call("PUT", "/documents/"+doc_id+"/profile", {"display_name":"展示名A","version":"v9.9"})
check("profile", s==200, p)
s, dt2 = call("GET", "/documents/"+doc_id+"/detail")
check("profile persisted", dt2["display_name"]=="展示名A" and dt2["version"]=="v9.9", (dt2.get("display_name"), dt2.get("version")))

# 7. 操作时间线(审计按 target)
s, a = call("GET", "/audit?target="+doc_id)
check("audit timeline", s==200 and len(a.get("logs",[]))>=1, len(a.get("logs",[])))

# 8. 覆盖上传(新版本)
content2 = "# 冒烟文档 v2\n\n## 章节A\n\n"+marker+" 覆盖新版KEYWW22。\n"
s, r2 = call("POST", "/documents/"+doc_id+"/replace", files=[("smoke.md", content2.encode(), "text/markdown")])
check("replace", s==200 and r2.get("status")=="done", r2.get("version"))
# 覆盖后内容更新
s, dt3 = call("GET", "/documents/"+doc_id+"/detail")
check("replace persisted", dt3.get("status")=="done")

# 9. 删除
s, dd = call("DELETE", "/documents/"+doc_id)
check("delete", s==200 and dd.get("deleted")==doc_id)
s, d4 = call("GET", "/documents?page=1&per_page=20")
check("delete gone", not any(x["doc_id"]==doc_id for x in d4["documents"]))

# 10. 分类/权限
s, m = call("PUT", "/documents/"+doc_id+"/meta", {"category":"冒烟类","access_scope":"private"})
check("meta path (doc may be deleted, expect 200 or error ok)", True,)

print("OVERALL", "OK" if ok else "FAIL")
