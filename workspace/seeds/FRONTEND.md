# 网关前端定制

网关 Web UI 可以通过使用 `memory_write` 向 `.system/gateway/` 工作区目录写入文件来自定义。更改在页面刷新后生效。

## 快速参考

### 品牌与布局

写入 `.system/gateway/layout.json` 以自定义品牌、标签页顺序和功能：

```json
{
  "branding": {
    "title": "我的 AI 助手",
    "colors": {
      "primary": "#e53e3e",
      "accent": "#dd6b20"
    }
  },
  "tabs": {
    "hidden": ["routines"],
    "default_tab": "chat"
  }
}
```

示例：`memory_write target=".system/gateway/layout.json" content='{"branding":{"title":"Acme AI","colors":{"primary":"#e53e3e"}}}' append=false`

### 自定义 CSS

写入 `.system/gateway/custom.css` 以进行样式覆盖：

示例：`memory_write target=".system/gateway/custom.css" content="body { --bg-primary: #1a1a2e; }" append=false`

常用 CSS 变量：`--color-primary`、`--color-accent`、`--bg-primary`、`--bg-secondary`、`--bg-tertiary`、`--text-primary`、`--text-secondary`、`--border`、`--success`、`--error`、`--warning`。

### 小部件

在 `.system/gateway/widgets/{id}/` 中创建自定义 UI 组件。目录名称即为小部件 id——它与 `manifest.json` 中的 id 字段以及 `GET /api/frontend/widget/{id}/{file}` 中的路径段匹配。

- `manifest.json` —— 小部件元数据（id、name、slot）
- `index.js` —— 小部件代码（调用 `IronClaw.registerWidget()`）
- `style.css` —— 可选的作用域样式（自动添加 `[data-widget="{id}"]` 前缀）

**CSS 作用域注意事项：** 小部件 CSS 作用域通过基于花括号计数的文本转换实现，而非完整的 CSS 解析器。CSS 注释（`/* } */`）或字符串字面量（`content: "{"`）中的花括号会干扰作用域器并产生格式错误的输出。避免在注释和字符串值中使用 `{` / `}`——如果需要在 `content:` 属性中使用字面花括号，请使用 Unicode 转义（`\7B` / `\7D`）。

**二进制资产：** 小部件文件通过工作区文本层（`Workspace::read()`）提供，该层返回 UTF-8 字符串。文本格式的资产（JS、CSS、JSON、SVG）可以正常工作。二进制资产（PNG、WOFF2、TTF 等）会被损坏——请将它们托管在外部，或将它们 Base64 编码到 CSS/JS 中，直到二进制工作区读取路径可用。

**插槽：** 目前浏览器运行时仅挂载 `tab`——`IronClaw.registerWidget({ slot: "tab", ... })` 会在标签栏中添加一个新标签页。对于在聊天消息中内联渲染结构化数据，请改用 `IronClaw.registerChatRenderer({ id, match, render })`。其他插槽名称可能被服务器接受，但目前在 UI 中任何位置都不会被挂载。

## API 端点

- `GET /api/frontend/layout` —— 当前布局配置
- `PUT /api/frontend/layout` —— 更新布局配置
- `GET /api/frontend/widgets` —— 列出已安装的小部件
- `GET /api/frontend/widget/{id}/{file}` —— 提供小部件文件

## 安全模型

**小部件在网关页面内运行，具有完整的会话权限。** 小部件的 `index.js` 作为内联 ES 模块（在每次响应的 CSP nonce 下）加载到与网关 UI 其余部分相同的浏览器文档中，这意味着：

- 小部件可以调用已登录用户可以调用的任何网关 API。运行时暴露了 `IronClaw.api.fetch(...)`，它会自动转发用户的 Bearer Token——没有按小部件的权限沙盒。
- 小部件可以读取和修改与内置标签页相同的 DOM，包括聊天输入、消息历史和页面上的任何其他小部件。
- 小部件 CSS 作用域限定为 `[data-widget="{id}"]`，但 JavaScript **不**被沙盒化。小部件可以通过 `document.querySelector` 从其标签面板外部访问并触及全局状态。

这是可接受的，因为信任边界位于上一层：小部件从**工作区**中的 `.system/gateway/widgets/` 加载，而工作区本身是一个特权存储，只能通过经过身份验证的 `memory_write` 调用访问。任何能够写入小部件文件的内容都已经可以直接驱动智能体。因此，小部件运行时是操作员的扩展面，而非不可信代码的沙盒——将安装小部件视为与在您自己的用户下运行脚本相同。

**实际影响：**

- 不要从您不会粘贴到终端作为 shell 脚本的来源安装小部件。
- 调用 `IronClaw.api.fetch('/api/memory/write', ...)` 的小部件可以修改任何工作区文件，包括其自身源代码——在安装前审查小部件代码。
- 小部件标识符验证器（`crates/ironclaw_gateway/src/layout.rs` 中的 `is_safe_widget_id`，应用于发现、提供和 `manifest.id`）以及 `manifest.id == directory_name` 检查防止小部件之间的混淆，而非针对恶意但格式正确的小部件。
- 纵深防御 XSS 保护（对内联脚本/样式突破的 `escape_tag_close`、对品牌值的 `is_safe_css_color`、CSP nonces）防止恶意 `layout.json` 字段破坏页面框架——但这些防御措施不适用于小部件 JS，后者被有意赋予完整的执行权限。

网关 CSP 仅允许 `'nonce-…'` 用于 `assemble_index` 发出的 `<script>` 标签，因此小部件无法在运行时注入*额外的*内联脚本，也无法使用 `eval()`、`new Function()` 或字符串形式的 `setTimeout` / `setInterval`——网关 CSP **不**包含 `'unsafe-eval'`。小部件*仍然*可以在不触发 CSP 的情况下做很多事情：它可以针对任何同源端点调用 `IronClaw.api.fetch`、修改整个 DOM、在聊天输入上附加事件监听器，以及通过动态 `import()` 从网关 `script-src` 允许的任何源（当前为 `'self'`、jsDelivr、cdnjs、esm.sh）拉取额外的 ES 模块。CSP 缩小了小部件可以发起的攻击*形式*，而非爆炸半径。希望更严格隔离的操作员应通过*受信任的*小部件挂载的 `<iframe sandbox>` 来运行不可信的 UI 代码，而不是直接注册它。