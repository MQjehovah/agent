#!/usr/bin/env python3
"""
GitLab 云端分支合并脚本
用法: python gitlab_merge.py <项目路径> <源分支> <目标分支>
示例: python gitlab_merge.py cloud/xz-data dev test

全程通过 GitLab REST API 云端操作，无需 clone 仓库。
"""

import urllib.request, urllib.parse, http.cookiejar, re, json, sys, os, time

# ===================== 参数解析 =====================
def parse_args():
    if len(sys.argv) < 4:
        print("用法: python gitlab_merge.py <项目路径> <源分支> <目标分支>")
        print("示例: python gitlab_merge.py cloud/xz-data dev test")
        sys.exit(1)
    return sys.argv[1], sys.argv[2], sys.argv[3]

PROJECT_PATH, SOURCE_BRANCH, TARGET_BRANCH = parse_args()

# ===================== 配置 =====================
GITLAB_URL = "http://gitlab.xzrobot.com"
USERNAME = "s_software"
PASSWORD = os.environ.get("IT_SYSTEM_PASSWORD", "E2CO2Xnv6ga9")

# ===================== 工具函数 =====================
results = []

def log(msg):
    results.append(msg)
    print(msg)

def save():
    results_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merge_result.txt")
    with open(results_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(results))

def api_get(path):
    resp = opener.open(f"{GITLAB_URL}/api/v4{path}")
    return json.loads(resp.read().decode('utf-8'))

def api_post(path, data):
    body = json.dumps(data).encode('utf-8')
    req = urllib.request.Request(
        f"{GITLAB_URL}/api/v4{path}",
        data=body,
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf_token}
    )
    resp = opener.open(req)
    return json.loads(resp.read().decode('utf-8'))

def api_put(path, data=None):
    body = json.dumps(data).encode('utf-8') if data else b''
    req = urllib.request.Request(
        f"{GITLAB_URL}/api/v4{path}",
        data=body,
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf_token}
    )
    req.method = 'PUT'
    resp = opener.open(req)
    return json.loads(resp.read().decode('utf-8'))

# ===================== Step 1: 认证 =====================
log("=== Step 1: 获取认证 ===")

cj = http.cookiejar.MozillaCookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

resp = opener.open(f"{GITLAB_URL}/users/sign_in")
html = resp.read().decode('utf-8', errors='ignore')
match = re.search(r'name="authenticity_token" value="([^"]+)"', html)
if not match:
    log("FAIL: 无法获取 CSRF token"); save(); sys.exit(1)
token = match.group(1)
log("CSRF token 获取成功")

login_url = f"{GITLAB_URL}/users/auth/ldapmain/callback"
data = urllib.parse.urlencode({
    'authenticity_token': token,
    'username': USERNAME,
    'password': PASSWORD
}).encode('utf-8')
req = urllib.request.Request(login_url, data=data)
try:
    opener.open(req)
    log("LDAP 登录成功")
except urllib.error.HTTPError as e:
    log(f"登录失败: HTTP {e.code}"); save(); sys.exit(1)

resp_csrf = opener.open(f"{GITLAB_URL}/")
html_csrf = resp_csrf.read().decode('utf-8', errors='ignore')
csrf_match = re.search(r'name="csrf-token" content="([^"]+)"', html_csrf)
if not csrf_match:
    csrf_match = re.search(r'name="authenticity_token" value="([^"]+)"', html_csrf)
csrf_token = csrf_match.group(1) if csrf_match else ""

user_info = api_get("/user")
if user_info.get("username") == USERNAME:
    log(f"认证成功! 用户: {USERNAME}")
else:
    log("认证失败!"); save(); sys.exit(1)

# ===================== Step 2: 获取项目 =====================
log("\n=== Step 2: 获取项目 ===")

encoded_path = urllib.parse.quote(PROJECT_PATH, safe='')
try:
    project = api_get(f"/projects/{encoded_path}")
    log(f"项目: {project['name_with_namespace']} (ID: {project['id']})")
except urllib.error.HTTPError:
    project_name = PROJECT_PATH.split('/')[-1]
    projects = api_get(f"/projects?search={project_name}&per_page=20")
    project = None
    for p in projects:
        if p['path_with_namespace'] == PROJECT_PATH:
            project = p
            break
    if not project:
        log(f"FAIL: 找不到项目 {PROJECT_PATH}"); save(); sys.exit(1)
    log(f"项目: {project['name_with_namespace']} (ID: {project['id']})")

PROJECT_ID = project['id']
WEB_URL = project['web_url']

# ===================== Step 3: 确认分支 =====================
log("\n=== Step 3: 确认分支存在 ===")

try:
    src = api_get(f"/projects/{PROJECT_ID}/repository/branches/{SOURCE_BRANCH}")
    log(f"源分支 {SOURCE_BRANCH}: {src['commit']['short_id']} - {src['commit']['title']}")
except urllib.error.HTTPError:
    log(f"FAIL: 源分支 {SOURCE_BRANCH} 不存在!"); save(); sys.exit(1)

try:
    tgt = api_get(f"/projects/{PROJECT_ID}/repository/branches/{TARGET_BRANCH}")
    log(f"目标分支 {TARGET_BRANCH}: {tgt['commit']['short_id']} - {tgt['commit']['title']}")
except urllib.error.HTTPError:
    log(f"FAIL: 目标分支 {TARGET_BRANCH} 不存在!"); save(); sys.exit(1)

# ===================== Step 4: 创建/复用 MR =====================
log("\n=== Step 4: 创建 Merge Request ===")

existing_mrs = api_get(
    f"/projects/{PROJECT_ID}/merge_requests"
    f"?state=opened&source_branch={SOURCE_BRANCH}&target_branch={TARGET_BRANCH}"
)

if existing_mrs:
    mr = existing_mrs[0]
    log(f"复用已有 MR: !{mr['iid']} (状态: {mr['merge_status']})")
else:
    try:
        mr = api_post(f"/projects/{PROJECT_ID}/merge_requests", {
            "source_branch": SOURCE_BRANCH,
            "target_branch": TARGET_BRANCH,
            "title": f"Merge branch {SOURCE_BRANCH} into {TARGET_BRANCH}",
            "remove_source_branch": False
        })
        log(f"MR 创建成功: !{mr['iid']}")
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8')
        log(f"创建 MR 失败: HTTP {e.code} - {err}"); save(); sys.exit(1)

MR_IID = mr['iid']
log(f"MR 链接: {WEB_URL}/-/merge_requests/{MR_IID}")

# ===================== Step 5: 等待合并状态检查 =====================
log("\n=== Step 5: 等待合并状态检查 ===")

merge_status = mr.get('merge_status', 'unchecked')
for attempt in range(1, 4):
    if merge_status in ('can_be_merged', 'cannot_be_merged'):
        break
    log(f"  检查 #{attempt}: {merge_status}，等待 5s...")
    time.sleep(5)
    mr_check = api_get(f"/projects/{PROJECT_ID}/merge_requests/{MR_IID}")
    merge_status = mr_check['merge_status']

if merge_status == 'cannot_be_merged':
    log("❌ 存在冲突，无法自动合并，需人工解决!")
    save(); sys.exit(1)
elif merge_status != 'can_be_merged':
    log(f"⚠️ 合并状态异常: {merge_status}，请人工检查!")
    save(); sys.exit(1)

log("✅ 合并检查通过，无冲突")

# ===================== Step 6: 执行合并 =====================
log("\n=== Step 6: 执行合并 ===")

try:
    result = api_put(f"/projects/{PROJECT_ID}/merge_requests/{MR_IID}/merge", {
        "merge_when_pipeline_succeeds": False
    })
    if result.get('state') == 'merged':
        log("✅ 合并成功!")
        log(f"合并提交: {result.get('merge_commit_sha', 'N/A')}")
        log(f"合并时间: {result.get('merged_at', 'N/A')}")
    else:
        log(f"合并状态异常: {result.get('state')} - {result.get('message', 'N/A')}")
        save(); sys.exit(1)
except urllib.error.HTTPError as e:
    err = e.read().decode('utf-8')
    log(f"合并失败: HTTP {e.code} - {err}"); save(); sys.exit(1)

# ===================== Step 7: 汇报 =====================
report = f"""
========================================
         合并完成报告
========================================
项目: {PROJECT_PATH} (ID: {PROJECT_ID})
源分支: {SOURCE_BRANCH}
目标分支: {TARGET_BRANCH}
MR: !{MR_IID}
MR链接: {WEB_URL}/-/merge_requests/{MR_IID}
========================================"""
log(report)
save()
