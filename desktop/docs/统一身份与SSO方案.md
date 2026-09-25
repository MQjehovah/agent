# 统一身份与 SSO 方案(自研)

| 项目     | 内容                                                       |
| -------- | ---------------------------------------------------------- |
| 文档版本 | v1.0                                                       |
| 编制日期 | 2026-09-05                                                 |
| 代码位置 | `E:\ai\sso`(授权服务)· `E:\ai\deploy`(目录/同步/编排)· `E:\ai\dashboard\server`(接入网关) |
| 决策记录 | 自研而非 Casdoor/Keycloak(可控性优先,已评审范围削减);AD/LDAP 方案演进为"钉钉→LDAP"三层;中台/OA 为平级接入方而非宿主 |

---

## 一、身份链路(架构定稿)

```
钉钉(人事事实源:部门/员工/离职)
   │ ① sync-dingtalk worker(自研,30 分钟全量对账;OA 审批 webhook 可即时触发)
   ▼
OpenLDAP(公司统一用户目录:工号/姓名/部门/手机号/aiStatus;密码存此处但由 LDAP ppolicy 管理)
   │ ② SSO 登录时按 dingtalkUserId / 工号 / 手机号 读取,校验在职状态
   ▼
自研 SSO 服务(仅 OIDC 授权码 + PKCE;认证双通道:钉钉扫码为主 + 账号密码为辅)
   │ ③ 标准 OIDC(id_token/access_token 均为 RS256 JWT,JWKS 公钥分发)
   ▼
dashboard 桌面端(经接入网关) / agent / rag(直连 LDAP 零改造) / market / router-admin / 数字中台·OA / 未来系统
   │ ④ 各系统以标准 OIDC 客户端库接入,按工号 JIT upsert 本地账号,签发各自原有会话
```

职责边界:

| 问题 | 归属 |
| ---- | ---- |
| 谁在职、在哪个部门 | 钉钉(HR 录入)→ 同步 → LDAP |
| 怎么证明身份 | 钉钉扫码 / LDAP 密码 bind(SSO 不存业务密码) |
| 怎么发凭证 | 自研 SSO(OIDC) |
| 谁能用什么功能 | 各业务系统自己的 RBAC(SSO 只发部门/角色原始 claim) |

## 二、自研 SSO 服务(`E:\ai\sso`)

### 2.1 范围三减法

1. **不做密码系统**:凭据校验委托钉钉扫码与 LDAP bind,SSO 代码内无密码哈希存储;
2. **不做管理界面**:用户在 LDAP(钉钉同步),客户端注册在 `clients.json` 静态配置;
3. **仅实现授权码 + PKCE(S256)一条协议路径**,无 refresh token / consent / SAML / CAS / 动态注册 / MFA。

### 2.2 端点面(标准 OIDC)

| 端点 | 说明 |
| ---- | ---- |
| `GET /.well-known/openid-configuration` | 发现 |
| `GET /authorize` | 授权端点:client/redirect 白名单、PKCE、SSO 会话检查 |
| `GET /login`、`POST /login/password` | 登录页(扫码/密码双 Tab)与密码通道 |
| `GET /dingtalk/start`、`GET /dingtalk/callback` | 钉钉扫码(state 绑定登录事务) |
| `POST /token` | code 换 id_token + access_token(Basic/Post 客户端认证) |
| `GET /userinfo`、`GET /.well-known/jwks.json` | 用户信息与公钥 |
| `GET/POST /logout` | 单点登出 |
| `GET /profile`、`POST /profile/password` | 密码激活/修改(扫码 10 分钟内免验当前密码) |
| `GET /healthz` | 健康检查 |

### 2.3 Token 设计

| 项 | 值 |
| -- | -- |
| id_token | RS256,10 分钟;claims:sub(工号)/name/dept/roles/nonce |
| access_token | RS256,1 小时;调 userinfo 用 |
| code | 不透明随机串,5 分钟,一次性,绑定 client/redirect/PKCE/nonce |
| refresh_token | 不做(内网 + SSO 会话滑动续期) |
| roles | 部门→角色映射展开(clients.json 每客户端配置,default_role 兜底) |

### 2.4 会话、登出与吊销

- SSO 会话:`sso_sid` HttpOnly Cookie,服务端 JSON 持久化,8h 滑动;
- 离职吊销三层:authorize 时实时查 LDAP 拒绝新登录 → access_token 1h 自然过期 → 接入方可回查 userinfo;OA 审批 webhook 即时同步可做到当天封号;
- 登出:清 SSO Cookie + 可选 post_logout_redirect_uri(白名单)。

## 三、目录与同步(`E:\ai\deploy`)

### 3.1 OpenLDAP(`deploy/ldap/`)

- 结构:`dc=company,dc=corp` 下 `ou=people`(人员)、`ou=groups`(部门)、`ou=system`(服务账号);
- 人员属性:employeeNumber(工号)/cn/departmentNumber/mobile/dingtalkUserId/aiStatus(自定义 schema `aiPerson`);
- 服务账号:sso-reader(只读)/ sso-writer(仅写 userPassword)/ sync-writer(写同步属性),ACL 见 `02-acl.ldif`;
- 密码策略:最少 8 位、失败 5 次锁 15 分钟(`03-ppolicy.ldif`)。

### 3.2 钉钉同步 worker(`deploy/sync-dingtalk/`)

- 拉取:部门树 + 逐部门员工分页(通讯录只读权限);
- 对账(纯函数 `reconcile.ts`,单元测试覆盖):新建(入职)/更新(调岗、改手机号)/禁用(离职置 aiStatus=disabled,不删除)/恢复(返聘);
- **绝不触碰 userPassword**;
- 运行模式:30 分钟定时 + `--once` 手动 + `--dry-run`;预留 `POST /sync` webhook(共享密钥),供 OA 审批通过后即时触发。

### 3.3 生命周期流程

```
入职:HR 钉钉录入 → ≤30 分钟 LDAP 建号(active、无密码)
     → 钉钉扫码登录全平台立即可用(各系统 JIT 建号,零人工开通)
     → 首次登录后引导设置密码 → 密码通道激活
调岗:钉钉改部门 → LDAP 更新 → 各系统角色/知识可见性随映射变化
离职:钉钉离职 → LDAP disabled → 拒绝新登录;存量 token ≤1h 失效;OA webhook 可即时封号
```

## 四、接入网关与桌面端(`E:\ai\dashboard`)

- 网关 OIDC 策略:302 到 SSO → callback 验签 id_token(JWKS)→ **按工号 JIT 开通 agent 账号**(网关持 admin 凭据,每次登录重置为一次性随机密码登录,凭据不落地)→ 网关会话;
- 会话命名空间隔离继续生效(agent 的 sessions 列表按 `u{工号}--` 前缀过滤);
- 桌面端:主进程起一次性 loopback 回调端口 → 系统浏览器完成扫码 → 网关 302 回 loopback 携带一次性 ticket(5 分钟)→ 桌面端换网关会话令牌;
- 网关保留 `local` 策略(agent 账号透传)供本地开发。

## 五、测试基线(全部自动化,已通过)

| 套件 | 覆盖 | 结果 |
| ---- | ---- | ---- |
| sso 烟测(`sso/test/run-smoke.mjs`) | mock 钉钉 + 文件目录 + **openid-client 标准客户端库**全流程:发现/JWKS/双通道/PKCE/code 一次性/单点/密码激活/禁用拒绝/登出 | 21/21 |
| 全链路 E2E(`dashboard/server/test/e2e-oidc.mjs`) | SSO → 网关 OIDC(JIT)→ mock agent → 命名空间隔离 → ticket 一次性 | 10/10 |
| 网关 local 策略回归 | agent 账号透传 + 会话隔离 | 3/3 |
| 同步对账(`sync-dingtalk/test/reconcile.test.mjs`) | 入职/调岗/离职/返聘/幂等 | 4/4 |

## 六、安全基线

- SSO 签发密钥 RSA-2048,私钥文件 600 权限,kid 机制支持轮换;
- redirect_uri 精确匹配、state 防 CSRF、nonce 防重放、PKCE S256、code 一次性、限流、审计(JSONL);
- LDAP ACL 最小授权;ppolicy 锁定;SSO 自身不存业务密码;
- 依赖:`jose` + `ldapts` + `openid-client`(仅测试),全部锁定版本;
- 保留逃生通道:协议面为标准 OIDC,未来切换成熟 IdP 时接入方零改动。

## 七、接入方清单与待办

| 接入方 | 模式 | 状态 |
| ------ | ---- | ---- |
| dashboard 网关+桌面端 | OIDC + loopback ticket | ✅ 已实现并测通 |
| rag | LDAP 直连(已内置,指向新目录即可) | ✅ 零改造 |
| agent / market / router-admin | 各自仓库加 OIDC 登录(文件级清单见 design.md) | ⏳ 逐仓库审批 |
| 数字中台 · OA | OIDC 客户端;审批 webhook 联动同步(P1) | ⏳ 试点 |

## 八、真实环境接入记录(xzrobot)

**已打通(2026-09-05)**:SSO 已对接公司现有 LDAP(192.168.31.252,Synology Directory Server)。
LDAP 布局与自建 OpenLDAP 不同,SSO 的目录集成已改造为全可配置:用户容器(`LDAP_PEOPLE_BASE`)、
属性映射(`LDAP_ATTR_SUB/NAME/DEPT/MOBILE/DINGTALK/STATUS`)、禁用标记(`LDAP_STATUS_DISABLED_FLAG`,
sambaAcctFlags 含 D 即禁用)均可通过环境变量适配;密码 bind 使用目录返回的条目 DN,任意布局通用。

当前状态(`sso/.env`):

| 项 | 值 |
| -- | -- |
| 目录 | ldap://192.168.31.252,cn=users,dc=xzrobot,dc=com |
| 密码通道 | ✅ 已验证(真实 bind,错误拒绝/正确签发) |
| 属性映射 | sub=uid,name=displayName(回退 cn),status=sambaAcctFlags(D=禁用) |
| 扫码通道 | ⏳ 待钉钉凭据 + dingtalkUserId 属性同步 |
| 部门属性 | Synology 默认无 departmentNumber,dept 为空 → default_role 兜底 |

**遗留适配**:
1. sync-dingtalk worker 写入的自定义属性(dingtalkUserId/aiStatus)需要在 Synology LDAP 扩展 schema,
   或把 worker 的属性名改为可配置复用现有属性;
2. 建议为 SSO 创建专用只读账号替代 uid=admin 做 bind(最小权限);
3. `.env` 已含真实凭据,务必保持 gitignore(已配置)。

## 九、部署记录

**已部署(2026-09-05)**:SSO 已 Docker 部署至 `192.168.31.45`(Ubuntu 18.04 / Docker 20.10)。

| 项 | 值 |
| -- | -- |
| 访问地址 | http://192.168.31.45:8091(局域网) |
| 部署方式 | docker build + docker run(restart=unless-stopped;服务器 compose 为 v1.17 老版本,不使用) |
| 代码位置 | /home/xzrobot/apps/sso(源码 + Dockerfile + .env;挂载 data/ 与 keys/) |
| 用户目录 | OpenLDAP(192.168.31.252,Synology),属性映射经环境变量适配 |
| 部署脚本 | `E:i\deploy\deploy_sso.py`(打包上传 → 基础镜像走 daocloud 镜像源 → 构建/启动/健康检查;含挂载目录 chown 1000 修复) |
| 已验证 | LAN 全流程 8/8:授权页 → 真实 LDAP 密码登录(错误拒绝/正确签发)→ code 换 token → JWKS 验签 → userinfo;容器重启后签名密钥持久(kid 不变) |

运维备忘:
- 更新版本:本机 `python deploy/deploy_sso.py` 一键重部(容器替换、密钥/会话保留);
- 日志:`docker logs sso`;审计:`/home/xzrobot/apps/sso/data/audit.jsonl`;
- 防火墙:8091 端口需对员工网段开放(当前未启防火墙规则,LAN 直达);
- 中台/各业务系统接入时,在 `clients.json` 注册对应 client 并重部(数据卷不受影响)。

**外部依赖(上线前必须)**:钉钉企业内部应用(AppKey/Secret + 通讯录只读)、生产 agent 地址。
