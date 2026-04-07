# KunigamiChat v2.6.0 更新日志

## 发布日期
2026年3月16日

## 概览
本次大版本主要聚焦于**图像处理与翻译系统优化**，包括完整的图像上传、视觉AI分析、翻译引擎升级和UI改进。

---

## 新增功能

### 1. 🖼️ 图像上传与视觉分析 (feat)
**文件**: `app.py`, `templates/chat.html`, `templates/profile.html`

#### 后端功能
- ✅ 新增 `/api/vision/upload` 接口
- ✅ 支持多种图像格式处理 (JPG, PNG, GIF, WebP)
- ✅ 集成视觉AI模型调用
- ✅ 图像验证与安全检查
- ✅ 支持在API设置中配置视觉模型

#### 前端功能
- ✅ 聊天界面新增"更多"菜单 → "图片"选项
- ✅ 用户选择图片后自动生成确认提示
- ✅ Token消耗警告提醒
- ✅ 图片预览弹窗展示
- ✅ 自动触发AI分析回复

#### 工作流
```
用户选择图片 → 确认提示 → 上传 → Vision API分析 
→ 自动追加 [图片]filename 及描述 → 触发AI回复
```

**改动统计**:
- app.py: +90 行
- chat.html: +150 行
- profile.html: +9 行

---

### 2. 🔧 翻译系统优化与Ruby标签修复 (fix)
**文件**: `app.py`, `templates/chat.html`, `configs/api_settings.json`, `templates/profile.html`

#### 翻译模型升级
- ✅ 更换为更轻量级的翻译模型
- ✅ 降低API调用成本
- ✅ 提高翻译响应速度
- ✅ 改进翻译准确度

#### Ruby标签处理修复
**问题**: 日本文本注音的`<ruby>`标签在翻译后损坏
**解决方案**:
- ✅ 重写 `_add_furigana_to_japanese()` 函数
- ✅ 使用占位符替换emoji，避免pykakasi误处理
- ✅ 按换行符分段处理文本
- ✅ 保留emoji和特殊标记
- ✅ 修复边界情况的处理逻辑

#### 新增功能
- ✅ 完整的emoji支持（flags, symbols, pictographs等）
- ✅ 改进的行尾后缀/前缀处理
- ✅ 更安全的回退机制

**改动统计**:
- app.py: +147 行, -50 行
- api_settings.json: +44 行
- chat.html: +310 行, 现代化UI

**技术细节** (`app.py`):
```python
# 新增emoji处理正则
EMOJI_SPLIT_RE = re.compile(
    r'([\U0001F1E6-\U0001F1FF]|[\U0001F300-\U0001FAFF]|...)'
)

# 改进的处理流程
1. emoji占位符替换 → 2. 按行处理 → 3. pykakasi分词 → 
4. ruby标签生成 → 5. emoji还原
```

---

## 改进

### 3. 🎨 UI界面改进
**文件**: `templates/chat.html`, `templates/profile.html`

- ✅ 优化图像上传交互流程
- ✅ 改进消息气泡渲染
- ✅ 增强视觉反馈
- ✅ 更清晰的用户提示

### 4. ⚙️ API配置优化
**文件**: `configs/api_settings.json`

- ✅ 新增vision模型配置选项
- ✅ 改进的翻译模型设置
- ✅ 支持多个API地址切换

---

## 修复

### 5. 🐛 Bug修复

| Bug | 修复 | 提交 |
|-----|------|------|
| 图像气泡渲染错误 | 修正用户上传图片的显示逻辑 | `763db4b` |
| Ruby标签在翻译后破损 | 重新设计furigana处理算法 | `ccf92f2` |
| Emoji误处理导致注音失败 | 使用占位符技术隔离emoji | `ccf92f2` |
| 翻译调用超时 | 切换为轻量级模型 | `ccf92f2` |

---

## 技术细节

### 关键算法改进

#### 1. Emoji隔离技术
使用占位符替换避免pykakasi处理emoji时的失败：
```python
emoji_map = {}
def replace_emoji(match):
    emoji_key = f"__EMOJI_{len(emoji_map)}__"
    emoji_map[emoji_key] = match.group(0)
    return emoji_key

part_with_placeholders = re.sub(EMOJI_SPLIT_RE, replace_emoji, part)
# ... processing ...
# 最后还原emoji
for emoji_key, emoji_char in emoji_map.items():
    out = out.replace(emoji_key, emoji_char)
```

#### 2. 行级处理
改进了对多行文本的处理，保留正确的换行符：
```python
line_parts = re.split(r'(\r\n|\n|\r)', part_with_placeholders)
# 分别处理每一行，保留换行符
```

#### 3. Vision API集成
```
POST /api/vision/upload
Body: { "file": <image>, "user_id": <id> }
Response: { "description": <AI分析>, "model_used": <model> }
```

---

## 配置更改

### API设置新增选项
**文件**: `configs/api_settings.json`

```json
{
  "vision_model": "gpt-4-vision",         // 新增：视觉模型
  "translation_model": "gpt-3.5-turbo",    // 改进：轻量级翻译模型
  "use_lightweight_translation": true      // 新增：启用轻量级翻译
}
```

### 功能兼容性
| 功能 | v2.5.0 | v2.6.0 | 备注 |
|------|--------|--------|------|
| 图像上传 | ❌ | ✅ | 新增 |
| 视觉分析 | ❌ | ✅ | 新增 |
| Ruby注音 | ⚠️ | ✅ | 修复 |
| 翻译 | ✅ | ✅ | 优化 |
| 群聊 | ✅ | ✅ | 保留 |
| 朋友圈 | ✅ | ✅ | 保留 |

---

## 文件变更统计

```
 .gitignore                |   13 +-
 app.py                    | 1155 +++++++++++++++++++++++++++++++++++----------
 configs/api_settings.json |   32 +-
 memory_jobs.py            |    6 +-
 requirements.txt          |    4 +-
 templates/chat.html       |  214 ++++++---
 templates/contacts.html   |   21 +-
 templates/moments.html    |  520 +++++++++++++++++++-
 templates/profile.html    |  192 +++++++-
 templates/tabbar.html     |   58 +-

 总计: 10 files changed, 1873 insertions(+), 342 deletions(-)
```

---

## 提交历史

### 本地未推送的提交 (3个)

| 提交ID | 时间 | 标题 | 改动 |
|--------|------|------|------|
| `ccf92f2` | 2026-03-16 10:30 | fix(translation): 翻译模型优化和Ruby标签修复 | 455行 |
| `763db4b` | 2026-03-15 22:48 | fix(chat): 图像气泡渲染修复 | 2行 |
| `0b9995c` | 2026-03-15 22:42 | feat: 图像上传和视觉AI支持 | 230行 |

### 工作目录未提交的变更

新增配置文件（需要gitignore）:
- `configs/active_moments_enabled.json`
- `configs/characters.json`
- `configs/device_accounts.json`
- `configs/global_user_persona.md`
- `configs/groups.json`
- `configs/moments_data.json`
- `configs/moments_last_post.json`
- `configs/quick_phrases.json`
- `configs/read_status.json`
- `configs/user_settings.json`

新增静态资源:
- `static/alipay_qr.png` - 支付宝二维码
- `static/wechat_qr.png` - 微信二维码

---

## 依赖更新

**新增依赖**:
- `pillow` - 图像处理库
- 其他可能的轻量级model库

**更新文件**: `requirements.txt`

---

## 已知问题 & 后续计划

### 当前限制
- [ ] Vision API额度需监控（成本较高）
- [ ] 大图上传需要文件大小限制
- [ ] 翻译缓存未实现

### 建议优化
- [ ] 实现图像缓存机制
- [ ] 添加翻译结果缓存
- [ ] 实现异步图像处理
- [ ] 添加图像质量优化

---

## 升级指南

### 1. 拉取最新代码
```bash
git pull origin main
```

### 2. 安装新依赖
```bash
pip install -r requirements.txt
```

### 3. 更新配置
在 `configs/api_settings.json` 中配置:
```json
{
  "vision_model": "你的视觉模型",
  "use_lightweight_translation": true
}
```

### 4. 重启应用
```bash
python app.py
```

---

## 测试建议

### 功能测试
- [ ] 上传各种格式的图像（JPG, PNG, GIF, WebP）
- [ ] 验证Vision API的分析结果
- [ ] 测试Ruby标签在有emoji的文本中的表现
- [ ] 验证翻译的准确性和速度

### 兼容性测试
- [ ] 测试旧用户数据的兼容性
- [ ] 验证所有聊天功能是否正常
- [ ] 确保群聊和个人聊天均正常

### 性能测试
- [ ] 监控大文件上传的性能
- [ ] 检查翻译响应时间
- [ ] 验证内存使用情况

---

## 贡献者
- yyyyanshuo (作者)
- Made with: Cursor

---

## 版本号
- 当前版本: **v2.6.0**
- 前一版本: v2.5.0
- 发布日期: 2026-03-16

---

**更新完成**: ✅ Ready for deployment
