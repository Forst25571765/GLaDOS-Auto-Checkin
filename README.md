# 📌 GLaDOS 自动签到

通过 GitHub Actions 每天自动签到，支持多个账号，以及可选的微信和 Telegram 通知。

## 配置 Cookie

1. 登录 https://glados.cloud，打开浏览器开发者工具的 **Network**。
2. 刷新控制台，选中发往 `glados.cloud/api/user/status` 等已登录接口的请求。
3. 从 **Request Headers** 复制完整的 `Cookie` 值，不要包含 `Cookie:` 请求头名称。
4. 在自己的仓库打开 **Settings → Secrets and variables → Actions → Secrets**，添加或更新 `COOKIES`。

当前官网签发的会话字段示例：

```text
gld:sess=xxxxxx; gld:sess.sig=yyyyyy
```

脚本兼容 `gld:`、`koa:` 等前缀，要求 `sess` 与 `sess.sig` 同前缀成对且非空。请保留实际签发的字段名称和值。旧会话失效时需要重新登录获取 Cookie，不能通过给旧 Cookie 改名恢复登录。

复制时附带的首尾空白、引号或 `Cookie:` 前缀会自动清理。不要将真实 Cookie 写入代码、Issue 或运行日志。

## 微信通知：保留 SCT_SENDKEY

现有的 Server酱微信通知继续使用 **`SCT_SENDKEY`**，无需改成上游的 `SERVERCHAN_KEY`，也无需重新设置已有的有效密钥。

需要首次配置时，在 [Server酱](https://sct.ftqq.com/) 获取 SendKey，保存在仓库的 Actions Secret `SCT_SENDKEY` 中。

脚本仍使用 `serverchan-sdk` 的 `sc_send` 发送签到汇总，包括鉴权失败和 Cookie 缺失通知。推送失败只记录警告，不覆盖实际签到结果；推送成功也不会让签到失败变成成功。SDK 返回的通知读取密钥不会输出到日志。

## 可选：Telegram 通知

1. 在 Telegram 向 `@BotFather` 发送 `/newbot` 创建机器人，取得 Bot Token。
2. 向机器人发送一条消息，再从 Telegram Bot API 的 `getUpdates` 返回值取得 `chat.id`。
3. 在仓库的 Actions Secrets 中设置 `TG_BOT_TOKEN` 和 `TG_CHAT_ID`。

同时配置 Telegram 和 `SCT_SENDKEY` 时，两个渠道都会收到汇总。不配置通知也可以正常签到。

## 多账号

推荐每行放一个完整 Cookie，也支持 `|||` 或原有的 `&` 分隔方式：

```text
gld:sess=account1; gld:sess.sig=signature1
gld:sess=account2; gld:sess.sig=signature2
```

各账号分别发送自己的 Cookie，并在切换账号前清理请求会话中的残留 Cookie。某个账号失败后仍会继续处理其余账号。

## Actions 运行结果

计划每天 UTC 04:00（北京时间 12:00）运行，GitHub 实际调度可能延迟。也可以进入 **Actions → GLaDOS Auto Checkin → Run workflow** 手动运行。

签到结果以接口业务码为准：

| 返回值或情况 | 脚本结果 |
| --- | --- |
| `code: 0` | 签到成功 |
| `code: 1`，包括 `Today's observation logged...` | 已签到，正常结束 |
| `code: -2` 或 HTTP 401/403 | 鉴权失败，提示重新登录并更新 `COOKIES` |
| 其他业务错误、请求异常、非法响应、Cookie 缺失或格式错误 | 签到失败 |

**任一账号失败，脚本在发送汇总后以退出码 1 结束，Actions 显示失败。** 全部账号成功或已签到时退出码为 0。HTTP 200 本身不代表签到成功。

账号邮箱与剩余天数的查询属于附加信息，查询失败不会覆盖已经确认的签到结果，也不会重复累计失败次数。网络错误、HTTP 429 和服务端错误会有限重试，鉴权失败不会反复重试。

### 网络超时

签到请求的连接等待为 10 秒，读取响应等待为 60 秒；最多请求 3 次，重试间隔依次为 15 秒、30 秒。日志中的 `CHECKIN_ATTEMPT` 会显示当前次数和等待设置。附加状态查询与 Telegram 通知仍使用较短的 5 秒连接、15 秒读取等待。

`ReadTimeout` 表示等待服务响应超时，不能据此判断 Cookie 已失效。GitHub Actions 在云端运行，此时出问题的是云端执行器到 GLaDOS 的请求，不依赖本机 Edge 或本机网速。短时服务或链路故障可通过较长等待和有限重试缓解，但无法保证消除服务端故障。

读取超时时，服务端可能已经完成签到但响应没有到达；重试若返回 `code: 1`，按“已签到”正常结束。三次请求后仍无法确认结果时，会发送网络超时通知并以退出码 1 结束，可稍后手动重跑。

如果日志显示“没有权限”，请重新登录官网获取完整 Cookie，更新 Actions Secret `COOKIES` 后再手动运行验证；仅同步脚本无法恢复已失效的会话。

## 本次同步范围

参考上游 [MZRidaz/GLaDOS-Auto-Checkin](https://github.com/MZRidaz/GLaDOS-Auto-Checkin) 截至 `b51eb40` 的相关修复，包含 [PR #16](https://github.com/MZRidaz/GLaDOS-Auto-Checkin/pull/16) 的前缀无关 Cookie 校验和粘贴清理，以及业务码判断、有限重试、账号会话隔离和日志脱敏。

保留本仓库的 `SCT_SENDKEY` 与 Server酱 SDK，并将失败退出规则设为任一账号失败即失败。本次没有引入自动消耗积分的兑换功能。

## 验证

安装依赖后可以运行回归测试：

```sh
python -m pip install requests serverchan-sdk
python -m unittest discover -s tests -v
```

测试使用模拟响应，不需要真实 Cookie，不发送微信或 Telegram 消息。`Checkin regression tests` 工作流会在相关代码更新时自动执行。
