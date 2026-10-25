# Neko Draw 架构

插件按「入口 / 流程 / 服务 / 数据」四层组织。原则是**同一件事只有一份实现**：
消息处理流程只做编排，具体动作全部由独立模块提供，配置保存时整体替换运行时。

## 入口层

- `main.py`：AstrBot 插件声明（`Star`、`/nc` 指令、`on_message`）。
  约 700 行，其中一半是旧接口的兼容转发，业务逻辑为零。
- `webui/__init__.py`：WebUI 桥接 API（配置、提供商测试、上传、历史、工作流解析）。

## 流程层

- `core/flow.py`：一条消息的完整处理流程。
  分流顺序：APNG → 本插件斜杠命令 → `/触发词` 归一 → 开头 @ 剥离 → 扩写/提取 → 生图。
  它**不实现**任何具体能力，只负责“什么时候调谁、结果怎么发”。
- `core/commands.py`：`/nc` 各子指令的具体实现（main 里只声明路由）。

## 服务层

| 模块 | 职责 |
|---|---|
| `core/app.py` | 装配全部服务、配置重载、调用历史写入 |
| `core/handler.py` | 生图：选模型、拼参数、提交、下载（唯一实现） |
| `core/generation.py` | ~~已删除~~（曾与 handler 重复，已合并回 handler） |
| `core/text_tools.py` | 扩写 / 提取形象：取图 → 调模型 → 返回纯文本；出图前的文字预处理也走这里 |
| `core/llm_gateway.py` | 对话模型的统一调用（提供商解析、超时、多模态） |
| `core/image_collector.py` | 取图唯一入口：消息 / 引用 / 合并转发 / @头像 |
| `core/image_sender.py` | 发图唯一出口：合并转发 / 外显金句 / 普通图片 |
| `core/apng.py` | APNG 制作与输入原图历史 |
| `core/pending_delivery.py` | 排队超时的后台补发、历史找回 |

## 数据层

- `core/history.py`、`core/apng_history.py`、`core/llm_history.py`：三类历史库
- `core/prompt_manager.py`、`core/command_presets.py`：提示词预设、扩写/提取条目
- `core/objects.py`：领域对象与纯函数（请求快照、清理工具）
- `core/config_store.py`、`core/storage.py`、`core/uploads.py`：配置与文件读写

## 关键规则

1. **取图只走 `ImageCollector`**：按「采图优先级」挑出一个来源，再把这个来源里的图全部取回。
   以前生图、扩写、APNG 各有一套取图代码，合并转发经常“取到一半就停”。
2. **发图只走 `ImageSender`**：合并转发 → 外显金句 → 普通图片，失败逐级回退。
3. **生图只有 `core/handler.py` 一份实现**，流程层不重复写选模型/调接口。
4. **配置重载是整体替换**：新运行时全部创建成功后才接管请求，失败则保留旧的。
5. **旧接口转发**：`plugin._command_llm` 这类老名字仍然可用，赋值会同步进
   `app.overrides`，主流程读取同一份，避免出现两套状态。
6. **取图替身只有一张清单**：`ImageCollector.overrides_from()` 统一挑出
   `_extract_image_urls` / `_extract_quoted_image_urls` / `_extract_all_image_urls`
   等覆盖项。生图、扩写/提取、APNG 三处都调它，不再各自维护一份“该替换哪几个”。
7. **提示文案跟着实际开关走**：取不到图时用 `collector.missing_hint()` 生成建议，
   只列当前真正开启的来源；没开 @头像 就不会让用户去 @ 人。
8. **同类调用形状一致**：`wrap_apng(路径, 开关, 参考图)` 在即时发送、后台补发、
   历史「找回」三处都用三个参数（不需要参考图时传 `None`），由
   `tests/test_api_compat.py` 用 AST 守住。
9. **文字类回复统一 `_text_reply()`**：扩写/提取、APNG 参数错误、限流提示都走它，
   “是否 @ 发送者”只有 `command_end_at_sender` 一处规则（图片另有 `enable_at_sender`）。
10. **出图前的文字预处理只走「扩写」**：`TextToolService.preprocess()` 复用 `_invoke()`，
    与用户在群里发的「扩写」是同一份实现、同一份模板。预设通过 `tag_prompt`（存扩写触发词）
    指定用哪条；`should_preprocess()` 只按「预设三态 → 全局 `llm_tag_enable`」决定跑不跑。
    预处理写进 `llm_history.db` 时 `source='bot'`，用户指令是 `source='user'`。
11. **升级迁移只跑一次**：`core/app.py::reset_preprocess_once()` 用数据目录下的
    `preprocess_reset.json` 作标记，把总开关与各预设的预处理设置清一次，
    之后不再覆盖用户在界面上的选择。

## 测试结构

- `tests/_stubs.py`：只 mock 宿主（`astrbot.*`），业务代码原样运行；
  `install_astrbot_stubs()` 幂等，重复安装会重新定义组件类、导致 `isinstance` 全失配。
- 业务测试按模块拆分，每个文件 ≤ 300 行；`test_api_compat.py` 只做跨模块签名自查。
- `test_audit_fixes.py`：审查阶段修掉的缺陷各一条回归用例。

## 与旧版本的关系

- 配置键名、数据文件路径、指令名、WebUI 接口路径**全部不变**，可直接覆盖升级。
- 旧版本写在插件类上的 60 多个方法已搬到对应模块，插件类保留同名转发。
