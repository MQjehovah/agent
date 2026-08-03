---
name: device-pull-bag
description: |
  通过云端 list_device_bags / find_bags_near_time（remote_operation）按时间匹配录包；
  再用 upload_bag_file 上传取 OSS url，有 ticket_id 时 create_ticket_attachment。
---

## 适用场景

- 用户要求查看/拉取某时间点附近的运行录包或任务录包
- 故障后需要定位故障时刻附近切片（工单 `happenTime` 或用户给出的时间）

## 前置条件

- 知道 `deviceId` + `productId`
- 知道目标时间点（可选；仅浏览列表时可不给）

## 流程

```
find_bags_near_time(deviceId, productId, happen_time)
  → 得到覆盖/邻近切片（file_path / file_name）
  → upload_bag_file(device_id, product_id, file_path=...)
  → 取返回 url
  → （有 ticket_id）create_ticket_attachment(ticket_id, name, url)
```

### 第一步：按时间匹配

```
find_bags_near_time(
  device_id=...,
  product_id=...,
  happen_time="2026-07-24T19:30:24+08:00",
  window_minutes=5,
  prefer_kind="all"
)
```

返回字段重点：`matched[]`、`file_name`/`file_path`、`covers_fault`、`out_of_range`、`hint`。

仅浏览列表：`list_device_bags(device_id, product_id, current, size)`。

### 第二步：解读匹配结果

1. `matched` 非空：覆盖时刻切片及前后片为候选
2. `out_of_range=true`：可能已滚动删除，不要假装已解析内容
3. 当前无 bag 内容自动解析 API

### 第三步：上传取链并挂附件

```
upload_bag_file(device_id, product_id, file_path=匹配到的路径)
```

成功时优先用返回的顶层 `url`。有 `ticket_id` 时：

```
create_ticket_attachment(ticket_id, name=文件名, url=OSS地址, type="")
```

上传成功但无 `url`：说明并附上 `data` 摘要，勿编造 URL。上传失败：汇报错误，可转人工从云端下载。

## 注意事项

- 挂工单附件最多 1～3 个 bag
- 故障过久匹配为空属正常，如实说明
