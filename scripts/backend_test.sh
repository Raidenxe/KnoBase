cd "$(dirname "$0")/.." || exit 1
set -e
echo "############ P1/P2 后端系统测试 ############"
# 口令不硬编码入库, 通过环境变量注入(否则用占位符, 避免泄露真实口令)
ADMIN_USER="${RAG_ADMIN_USER:-admin}"
ADMIN_PASS="${RAG_ADMIN_PASSWORD:-__REPLACE_ME__}"
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
AUTH="Authorization: Bearer $TOKEN"
JSON="Content-Type: application/json"
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }

echo ""
echo "=== 1. 会话创建 ==="
CR=$(curl -s -X POST http://localhost:8000/api/v1/chat -H "$AUTH" -H "$JSON" -d '{"question":"P1会话测试：MySQL备份时间？"}')
CONV=$(echo "$CR" | python3 -c "import sys,json;print(json.load(sys.stdin)['conversation_id'])")
[ -n "$CONV" ] && ok "创建会话 conv=$CONV" || bad "创建会话"
echo ""
echo "=== 2. 会话列表(包含新建) ==="
L=$(curl -s -H "$AUTH" http://localhost:8000/api/v1/conversations)
echo "$L" | python3 -c "import sys,json;d=json.load(sys.stdin);cs=[c for c in d['conversations'] if c['id']=='$CONV'];print('  found:',len(cs)>0);assert len(cs)>0" && ok "列表可见" || bad "列表可见"
echo ""
echo "=== 3. 会话详情(含 messages + id) ==="
D=$(curl -s -H "$AUTH" http://localhost:8000/api/v1/conversations/$CONV)
echo "$D" | python3 -c "import sys,json;d=json.load(sys.stdin);msgs=d['messages'];asst=[m for m in msgs if m['role']=='assistant'];print('  msgs:',len(msgs),'asst_ids:',[m['id'] for m in asst]);assert asst" && ok "详情+message_id" || bad "详情+message_id"
ASST_ID=$(echo "$D" | python3 -c "import sys,json;d=json.load(sys.stdin);ms=[m for m in d['messages'] if m['role']=='assistant'];print(ms[0]['id'])")
echo ""
echo "=== 4. 重命名 ==="
R=$(curl -s -X PATCH http://localhost:8000/api/v1/conversations/$CONV -H "$AUTH" -H "$JSON" -d '{"title":"已重命名测试"}')
echo "$R" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  title:',d.get('title'));assert d.get('title')=='已重命名测试'" && ok "重命名" || bad "重命名"
echo ""
echo "=== 5. 反馈提交(up) ==="
F1=$(curl -s -X POST http://localhost:8000/api/v1/feedback -H "$AUTH" -H "$JSON" -d "{\"conversation_id\":\"$CONV\",\"message_id\":$ASST_ID,\"rating\":\"up\",\"comment\":\"测试好评\"}")
echo "$F1" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  fb:',d.get('rating'));assert d.get('rating')=='up'" && ok "反馈提交up" || bad "反馈提交up"
echo ""
echo "=== 6. 反馈覆盖(down) ==="
F2=$(curl -s -X POST http://localhost:8000/api/v1/feedback -H "$AUTH" -H "$JSON" -d "{\"conversation_id\":\"$CONV\",\"message_id\":$ASST_ID,\"rating\":\"down\",\"comment\":\"改判为差评\"}")
echo "$F2" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  fb:',d.get('rating'));assert d.get('rating')=='down'" && ok "反馈覆盖down" || bad "反馈覆盖down"
echo ""
echo "=== 7. 反馈统计 ==="
S=$(curl -s -H "$AUTH" http://localhost:8000/api/v1/feedback/stats)
echo "$S" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  stats:',d);assert d['down']==1" && ok "反馈统计" || bad "反馈统计"
echo ""
echo "=== 8. 版本筛选(带doc_version=3, 应拒答/正常不报错) ==="
V=$(curl -s -X POST http://localhost:8000/api/v1/chat -H "$AUTH" -H "$JSON" -d '{"question":"测试版本筛选","doc_version":3}')
echo "$V" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  answer:',d['answer'][:40]);assert 'answer' in d" && ok "版本筛选不报错" || bad "版本筛选"
echo ""
echo "=== 9. 不带版本检索(应召回引用) ==="
NV=$(curl -s -X POST http://localhost:8000/api/v1/chat -H "$AUTH" -H "$JSON" -d '{"question":"MySQL 备份时间怎么安排？"}')
echo "$NV" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  cites:',len(d['citations']));assert len(d['citations'])>0" && ok "默认检索召回" || bad "默认检索召回"
echo ""
echo "=== 10. 监控聚合(admin) ==="
MS=$(curl -s -H "$AUTH" http://localhost:8000/api/v1/conversations/admin/stats?limit=50)
echo "$MS" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  stats:',d['stats']);assert 'refusal_rate' in d['stats']" && ok "admin stats" || bad "admin stats"
echo ""
echo "=== 11. 权限校验(普通用户不能访问admin) ==="
# 建一个普通用户
DEMO_PASS="${RAG_DEMO_PASSWORD:-__REPLACE_ME__}"
curl -s -X POST http://localhost:8000/api/v1/auth/users -H "$AUTH" -H "$JSON" -d "{\"username\":\"p1test_user\",\"password\":\"$DEMO_PASS\",\"role\":\"member\"}" >/dev/null 2>&1 || true
UT=$(curl -s -X POST http://localhost:8000/api/v1/auth/login -H "$JSON" -d "{\"username\":\"p1test_user\",\"password\":\"$DEMO_PASS\"}" | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))")
if [ -n "$UT" ]; then
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $UT" http://localhost:8000/api/v1/conversations/admin/stats)
  echo "  admin/stats status for member: $CODE"
  [ "$CODE" = "403" ] && ok "RBAC拦截member" || bad "RBAC拦截member (got $CODE)"
else
  bad "创建普通用户"
fi
echo ""
echo "=== 12. 删除会话(清理) ==="
DL=$(curl -s -X DELETE http://localhost:8000/api/v1/conversations/$CONV -H "$AUTH")
echo "$DL" | python3 -c "import sys,json;print('  ',json.load(sys.stdin))" && ok "删除会话" || bad "删除会话"
echo ""
echo "############ RESULT: PASS=$PASS FAIL=$FAIL ############"
exit $FAIL