# 12306 接口体检结果

- 检查时间：2026-10-24T16:03:40
- 数据日期：2026-10-24

## 结论

| 接口 | 结果 | 说明 |
|---|---|---|
| station_name.js | 可用 | 解析车站 3404 个 |
| search/v1/train/search | 可用 | keyword=G1 命中 200 条 |
| train_list.js（历史快照） | 可用 | 快照日期 ['2022-08-17', '2022-08-18', '2022-08-19', '2022-08-20', '2022-08-21', '2022-08-22', '2022-08-23', '2022-08-24', '2022-08-25', '2022-08-26', '2022-08-27', '2022-08-28', '2022-08-29', '2022-08-30', '2022-08-31', '2022-09-01']，共 199904 条（已冻结，仅作交叉校验） |
| czxx/queryByTrainNo | 可用 | G1 解析 7 站，历时 294 分钟 |
| thirdparty:gaotie | 不可用 | HTTP 403，响应长度 138 |

## 抓取口径说明

1. **车次枚举**：`train_list.js` 已冻结在 2022-08-29，不能作为当前图定车次来源；改用 `search/v1/train/search` 按前缀递归细分（单次上限 200 条），得到当前在售车次。
2. **时刻表**：`otn/czxx/queryByTrainNo` 只需 `train_no` 与日期，`from_station_telecode` / `to_station_telecode` 可留空，返回全部经停站的到发时刻与等级。
3. **里程 / 站台**：12306 不提供，只能依赖第三方页面；第三方不可达时字段留空并在报告中计入覆盖率，不做推测。
4. **Cookie**：访问 `otn/leftTicket/init` 获取 JSESSIONID 后即可查询时刻表；不需要登录。

## 响应样本（截断）

### station_name.js

```
var station_names ='@bjb|北京北|VAP|beijingbei|bjb|0|0357|北京|||@bjd|北京东|BOP|beijingdong|bjd|1|0357|北京|||@bji|北京|BJP|beijing|bj|2|0357|北京|||@bjn|北京南|VNP|beijingnan|bjn|3|0357|北京|||@bjx|北京西|BXP|beijingxi|b
```

### search/v1/train/search

```
[{"date": "20261024", "station_train_code": "G1", "train_no": "24000000G10L", "start_station": "北京南", "end_station": "上海虹桥", "station_count": 7}, {"date": "20261024", "station_train_code": "G10", "train_no": "5l00000G1010", "start_station": "上海虹桥", "end_station": "北京南", "station_count": 4}]
```

### train_list.js（历史快照）

```
var train_list ={"2022-08-29":{"D":[{"station_train_code":"D27(天津西-哈尔滨西)","train_no":"2500000D2703"},{"station_train_code":"D28(哈尔滨西-天津西)","train_no":"0h00000D280R"},{"station_train_code":"D55(张掖西-乌鲁木
```

### czxx/queryByTrainNo

```
{"train_class": "高速", "start": "北京南", "end": "上海虹桥", "first": {"station_no": 1, "station_name": "北京南", "arrive_time": null, "depart_time": "06:30", "stop_minutes": null, "day_offset": 0, "is_start": true, "is_end": false}, "last": {"station_no": 7, "station_name": "上海虹桥", "arrive_time": "11:24", "depart_time": "11:24", "stop_minutes": null, "day_offset": 0, "is_start": false, "is_end": true}}
```

### thirdparty:gaotie

```
<html>
<head><title>404 Not Found</title></head>
<body>
<center><h1>404 Not Found</h1></center>
<hr><center>nginx</center>
</body>
</html>
```
