# 音箱与语音设备输入

连接路径：设备麦克风 → 上游语音识别（ASR）→ 最终转写 → 个人助理 `RawRecord` → 现有模型 → 待办、日程、画像 → 看板和微信提醒。

目前实现转写接收与后续处理。具体音箱需要提供最终转写，或由 Home Assistant、快捷指令等桥接；尚未接入品牌私有麦克风接口，也不执行设备控制或语音播报。

## 配对

打开本地看板 `http://127.0.0.1:8773/`，在“设备输入”中添加设备，保存生成的接入 URL 和 Bearer 凭据。凭据仅显示一次，数据库只存哈希。公网手机入口只接收输入，不提供设备管理。

设备创建时绑定当时所选的用户资料与时区；后续修改默认微信来源不会改变已有设备的归属。不同设备、不同语音会话分别保留上下文，但提取时可以看到同一账号的已有事项和画像。

默认说话者身份未确认。只有上游能够可靠识别本人时，才填写“本人说话者 ID”，并在对应转写中传同一 `speaker_id`。不要因为设备属于自己，就把所有输入标成自己。

## 提交最终转写

```http
POST /ingest/devices/<设备 ID>
Authorization: Bearer <配对凭据>
Content-Type: application/json

{
  "event_id": "原始语音事件的稳定 ID",
  "text": "明天下午三点我要和小李开项目会。",
  "occurred_at": "2026-11-05T18:00:00+08:00",
  "stream_id": "study-conversation-1",
  "speaker_id": "可靠的上游说话者 ID",
  "speaker_name": "说话者显示名称"
}
```

必填 `event_id`、`text`；其余字段可省略。`occurred_at` 是原话发生时间，可用带时区 ISO 日期或 Unix 秒数。未提供时仍保存原文，但不会以采集时间猜测“明天”的日期。`stream_id` 用于区分语音会话，默认 `default`；`speaker_name` 只作展示，不能证明身份。

首次接收返回 HTTP 202，表示原文已保存并排队，模型结果稍后出现在看板。相同设备的相同事件 ID、相同内容重试返回 200 和 `duplicate: true`，不再提取或创建事项；ID 相同而内容不同返回 409。不要提交识别过程中的临时片段：等最终转写后再提交，网络重试沿用同一 ID、文本和原始时间。

每批处理最早的 80 条新记录，另带最近 30 条已处理原文作为上下文。提取过程中收到的新输入会继续排队，不会被当前批次误标完成。模型失败仍保留原文，可在设备列表点击“重试未完成的整理”。“停止接收”使凭据失效，保留原文与派生结果。

## 小度音箱

小度联网不代表本机能读取其麦克风或语音历史。本仓库尚未包含小度品牌适配器；以下是基于官方文档的桥接路径，需要开发者权限与真机验证。

先在[官方开发者认证页](https://dueros.baidu.com/didp/register/registerinfo)完成账号要求的认证。自定义技能可能还需商务开通权限；认证通过不等于技能权限已开通。身份材料由账号所有者在官方页面提交，不保存在本仓库。

### 语音输入：自定义技能

在 [DuerOS 技能控制台](https://developer.dueros.baidu.com/dbp) 创建自定义技能，引用系统缺省意图 `ai.dueros.common.default_intent`。这样技能内未命中其他意图的原话也会转交给技能，无需为待办、日程或兴趣配置关键词和槽位；语义理解仍由个人助理的现有模型完成。

建议由平台代码编辑中的技能处理函数转发到设备配对 URL，使用配对 Bearer 凭据。平台托管技能，不需要在助理里再开放一个未认证的 DuerOS 回调；如果改用直接 HTTPS 回调，则必须遵循官方的请求验签协议。转发字段如下：

| 设备输入字段 | DuerOS 请求字段 |
| --- | --- |
| `event_id` | `request.requestId`，重试沿用原 ID |
| `text` | `request.query.original` |
| `occurred_at` | `request.timestamp` 转为 Unix 秒数 |
| `stream_id` | `session.sessionId` |

只转发 `IntentRequest` 中的原话。`LaunchRequest` 用于欢迎和打开聆听，`SessionEndedRequest` 用于退出，不生成待办。接收成功只能播报“已收到，正在整理”，不能在模型完成前声称日程已创建。网络失败不能播报成功；上游若重试，必须保留原事件 ID。

小度的账号 `userId` 和设备 ID 不能证明当前说话者就是本人，因此不要把它们映射为 `speaker_id` 或“本人说话者 ID”。这条路径默认保留未知身份，原话中的行动主体仍需模型按证据判断。

同一百度账号的音箱可通过“小度小度，打开调试技能模式”，再“打开〈技能调用名〉”进行真机调试，不必先公开发布。此路径只接收进入技能后的交互；目前未找到可读取所有日常语音记录的官方接口。

官方依据：[缺省意图](https://developer.dueros.baidu.com/doc/dueros-bot-platform/dbp-nlu/defaultIntent_markdown)、[标准请求](https://developer.dueros.baidu.com/doc/dueros-bot-platform/dbp-custom/standard-request_markdown)、[平台代码编辑](https://developer.dueros.baidu.com/doc/dueros-bot-platform/dbp-deploy/dbp-service_markdown)、[真机测试](https://developer.dueros.baidu.com/doc/dueros-bot-platform/dbp-test/real-test_markdown)。

### 在线设备与播报：官方 MCP

小度官方 MCP 的 `list_user_devices` 可以列出授权账号的在线设备；`xiaodu_speak` 可以让指定设备播报文字，可作为后续提醒输出。它不提供麦克风转写流，不能替代上述输入路径。

连接需要开发者应用和音箱所属百度账号的 OAuth 授权，并非只填局域网 IP。官方接入准备提到个人开发者认证，但授权介绍中仍有企业认证要求，实际开放资格需要在控制台确认。本仓库不包含应用凭据、OAuth 令牌或真机播报适配器。

官方依据：[设备能力](https://developer.dueros.baidu.com/doc/dueros-bot-platform/mcp-server/overview/xiaodu-speaker-mcp-server-overview_markdown)、[接入准备](https://developer.dueros.baidu.com/doc/dueros-bot-platform/mcp-server/prepare/prepare_markdown)、[接入授权](https://developer.dueros.baidu.com/doc/dueros-bot-platform/mcp-server/prepare/auth-intro_markdown)。

## iPhone 快捷指令

可创建“听写文本 → 获取 URL 内容”的快捷指令：听写完成后生成一次 UUID、取得当前日期（ISO 格式），保存为本次事件变量，再向配对 URL 发 JSON POST，请求头填 `Authorization: Bearer <凭据>`。JSON 中映射 `event_id`、`text` 和 `occurred_at`。如果执行重试，沿用这些变量，不重新生成 ID。

若这个快捷指令只由本人使用，可以在配对时填写固定说话者 ID，并由快捷指令提交相同 `speaker_id`；多人共用时保留未知身份。

## Home Assistant 转发

已有自动化或转写服务能拿到最终文字时，可以调用 [Home Assistant 官方 RESTful Command](https://www.home-assistant.io/integrations/rest_command/) 转发。在 `configuration.yaml` 中配置以下命令，将 URL 替换成配对结果；`secrets.yaml` 中保存完整的 `Bearer <凭据>` 为 `personal_assistant_authorization`：

```yaml
rest_command:
  personal_agent_transcript:
    url: "https://你的助理域名/ingest/devices/设备ID"
    method: POST
    headers:
      authorization: !secret personal_assistant_authorization
      user-agent: "Mozilla/5.0 HomeAssistant"
    content_type: "application/json"
    payload: >-
      {{ {"event_id": event_id, "text": text,
          "occurred_at": occurred_at, "stream_id": stream_id} | to_json }}
```

拿到最终转写的自动化调用 `rest_command.personal_agent_transcript`，在 `data` 中传上游稳定的 `event_id`、`text`、原始 `occurred_at` 与 `stream_id`。这是转发动作，不能直接取得所有音箱的麦克风；具体触发点需根据设备或语音识别集成接入。
