---
name: jenkins
description: jenkins操作技能，通过 Jenkins API 进行作业管理、构建触发、构建监控等运维操作。
---
# Jenkins 操作技能

**适用场景**: 通过 Jenkins API 进行作业管理、构建触发、构建监控等运维操作。

## REST API 核心操作

### 1. 获取 Jenkins 实例信息

获取所有作业列表：

```bash

curl -u [username]:[password or token]http://cloudci.xzrobot.com/api/json?pretty=true

```

### 2. 获取作业信息

- 所有作业: `http://cloudci.xzrobot.com/api/json?pretty=true`
- 单个作业: `http://cloudci.xzrobot.com/job/JOBNAME/api/json?pretty=true`
- 最后构建: `http://cloudci.xzrobot.com/job/JOBNAME/lastBuild/api/json?pretty=true`
- 最后稳定构建: `http://cloudci.xzrobot.com/job/JOBNAME/lastStableBuild/api/json?pretty=true`
- 最后成功构建: `http://cloudci.xzrobot.com/job/JOBNAME/lastSuccessfulBuild/api/json?pretty=true`
- 最后失败构建: `http://cloudci.xzrobot.com/job/JOBNAME/lastFailedBuild/api/json?pretty=true`

### 3. 获取构建队列信息

```bash

curl -u [username]:[password or token] http://cloudci.xzrobot.com/queue/api/json?pretty=true

```

### 4. 获取构建编号

```bash

curl -u [username]:[password or token] http://cloudci.xzrobot.com/job/JOBNAME/lastBuild/buildNumber

```

### 5. 触发构建（需要 Crumb）

获取 Crumb:

```bash

curl -s -f -u "[username]:[password or token]" \

  --cookie-jar cookie.jar \

  -s http://cloudci.xzrobot.com/crumbIssuer/api/xml?xpath=concat(//crumbRequestField,%22:%22,//crumb)

```

**无参构建**:

```bash

curl -u [username]:[password or token] \

  http://cloudci.xzrobot.com/job/JOBNAME/build \

  -X POST -H "Jenkins-Crumb:<CRUMB>"

```

**有参构建**:

```bash

curl -u [username]:[password or token] \

  http://cloudci.xzrobot.com/job/JOBNAME/buildWithParameters \

  -X POST -H "Jenkins-Crumb:<CRUMB>" \

  --data-urlencode json='{"param1":"value1","param2":"value2"}'

```

**通过 POST Body 触发**:

```bash

curl -u [username]:[password or token] \

  http://cloudci.xzrobot.com/job/ROBOT_JENKINS_TEST/api/json?tree="lastBuild[buildNumber]" \

  -X POST -H "Jenkins-Crumb:<CRUMB>" \

  -H 'Content-Type: application/x-www-form-urlencoded' \

  -d 'json={"parameter":[

    {"name":"ENVIRONMENT","value":"test"},

    {"name":"BRANCH","value":"master"}

  ]}'

```

### 6. 获取构建日志

```bash

curl -u [username]:[password or token] \

  http://cloudci.xzrobot.com/job/JOBNAME/BUILD_ID/logText/progressiveText?start=0

```

## Python jenkinsapi 使用示例

```python

from jenkinsapi.jenkins import Jenkins



class JenkinsClient:

    def __init__(self):

    self.jenkins = Jenkins(

    'http://cloudci.xzrobot.com',

    username='[username]',

    password='[password or token]',

    use_crumb=True

    )



    def get_all_jobs(self):

    """获取所有作业"""

    return self.jenkins.get_jobs_info()



    def get_job_info(self, job_name):

    """获取作业信息"""

    job = self.jenkins.get_job(job_name)

    return {

    'last_build': job.get_last_build().__str__(),

    'last_completed': job.get_last_completed_build().__str__(),

    'last_good': job.get_last_good_build().__str__(),

    'last_failed': job.get_last_failed_buildnumber(),

    'is_running': job.is_queued_or_running()

    }



    def trigger_build(self, job_name, params=None):

    """触发构建"""

    if params:

    self.jenkins.build_job(job_name, params=params)

    else:

    self.jenkins.build_job(job_name)



    def get_build_info(self, job_name, build_id=None):

    """获取构建信息"""

    job = self.jenkins.get_job(job_name)

    if build_id:

    build = job.get_build(build_id)

    return {

    'timestamp': build.get_timestamp(),

    'console': build.get_console(),

    'params': build.get_params(),

    'status': build.get_status(),

    'changeset': build.get_changeset_items()

    }

    return None



# 使用示例

client = JenkinsClient()

client.trigger_build('ROBOT_JENKINS_TEST', {'ENVIRONMENT': 'test', 'BRANCH': 'master'})

```

## 常用接口组合

### 环境检查

1. 获取作业列表确认目标作业存在
2. 检查作业状态（是否禁用、是否在运行）
3. 触发构建

### 构建监控

1. 检查作业是否在运行
2. 获取最新构建编号
3. 获取构建日志实时输出

### 构建清理

1. 获取历史构建列表
2. 删除指定构建（注意：可能影响历史记录）

## 注意事项

1. **跨站脚本伪造请求保护（CSRF）**：新版本 Jenkins 需要提供 Crumb 抬头
2. **API Token**：建议使用 API Token 而非密码
3. **权限控制**：确保账号有执行操作的权限
4. **参数构建**：有参构建需要提供正确的参数名称和值
5. **作业名称**：注意 Jenkins 作业名称可能包含特殊字符

## 状态标记

**待触发**: 构建ID需要从 `lastBuild/buildNumber` 获取后再查询日志

**运行中**: `is_queued_or_running()` 返回 `True`

**成功**: 构建状态为 `SUCCESS`

**失败**: 构建状态为 `FAILURE`
