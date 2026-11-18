# SakiGlobal 浏览器 Agent 部署

本版本由已登录的 `/api/run_agent` 接收任务，`run_agent.py` 单独执行。Nginx 继续使用新站现有的 8010 反向代理。不启动旧版 `run_agent_api.py`，不使用 5001 端口，不更改旧站 Agent。

界面提示和运行提示词支持日文、英文、简体中文，按发起任务时的用户界面语言选择。角色回复按角色语言 → 默认聊天语言 → 用户语言，支持任意语言。改变设置后重新发起任务即可使用新设置。

使用角色当前的聊天线路和模型，会产生该模型的调用费用。网页快照及操作提示会作为聊天消息保存在当前角色的聊天记录中；页面内的启动确认会说明网页内容与聊天上下文发送给所选模型。

每台服务器同时最多一个浏览任务，最多 50 步、15 分钟（包括等待用户的时间）。浏览器没有用户的网页登录状态，禁止访问私网、服务器本机或非 HTTP/HTTPS 地址，仅允许标准 80/443 端口。下载、弹出窗口、Service Worker 不启用；需要登录、验证码、弹窗或复杂交互的网站不保证可用。内部应用会话只用于调用新站聊天接口，不传入外部浏览器。

## 上传

先备份服务器现有的对应源码，再上传。更新包只包含下面列出的源码、服务配置和说明文件，不包含密钥或数据。

下列路径均相对于本地项目根目录，上传到 `/var/www/sakiglobal/` 对应路径：

- `agent-test/agent_universal.js`
- `agent-test/agent_prompts.js`
- `agent-test/package.json`、`agent-test/package-lock.json`
- `core/web_agent.py`
- `services/agent_proxy.py`
- `run_agent.py`
- `blueprints/chat.py`
- `templates/chat.html`
- `static/locales/ui.json`
- `deploy/sakiglobal-agent.service`

不要从电脑上传 node_modules、虚拟环境、.env 或用户数据。

生产依赖只保留 Playwright。目录内旧版 `server.js` 演示服务器已不再受支持，Express 及其传递依赖已移除；正式服务始终通过 systemd 启动，不使用 `npm start`。

## 安装与只读启动检查

以 ubuntu 登录执行（不要 sudo npm）：

```bash
cd /var/www/sakiglobal/agent-test
npm ci --omit=dev --ignore-scripts
./node_modules/.bin/playwright --version
cd /var/www/sakiglobal
/var/www/sakiglobal/venv/bin/python -u run_agent.py --check
```

检查会短暂启动浏览器并验证本机网页请求被拦截；不调用模型、不创建聊天记录，也不启动常驻服务。若缺少浏览器，先在 agent-test 目录执行 `./node_modules/.bin/playwright install chromium` 再检查。已有缓存可复用；不删除旧缓存，也不链接旧站 node_modules。

## 检查通过后启动

检查通过后安装服务：

```bash
sudo install -o root -g root -m 0644 /var/www/sakiglobal/deploy/sakiglobal-agent.service /etc/systemd/system/sakiglobal-agent.service
sudo systemctl daemon-reload
sudo systemctl restart sakiglobal.service
sudo systemctl enable --now sakiglobal-agent.service
sudo systemctl show sakiglobal.service sakiglobal-agent.service --no-pager -p Id -p ActiveState -p SubState -p NRestarts -p MemoryCurrent
sudo journalctl -u sakiglobal-agent.service -n 40 --no-pager -o cat
```

服务启动前会再次在 systemd 的实际权限限制下执行浏览器检查。若失败，将检查日志发回排查，不要放开桶权限或复制旧站服务配置。

## 页面验收

刷新新站聊天页面，选择一个普通公开网页发起浏览。确认角色在当前聊天中回复，ASK/WAIT 后可继续，停止按钮能结束任务。分别检查日文、英文、中文界面；将角色语言设为与界面不同的语言，确认自然语言回复遵循角色设置。最后确认旧站仍正常，查看 `free -h` 与新 Agent 的 MemoryCurrent。

后台没有公开 HTTP 端口。运行时队列在 `/var/lib/sakiglobal/.agent-runtime`，包含短暂存放的会话 cookie，必须保持私有，不要上传或贴出内容。日志只记录步骤与结束状态，不记录会话、页面正文或用户输入。停止、出错、超时及后台重启会清理任务状态。服务限制内存 768 MiB、CPU 单核；内存不足时任务可能中断，需要观察真实页面的资源消耗。

本地测试包含模拟浏览器的完整任务流程、三语提示、接口账号隔离、队列恢复、代理转发与内网拒绝。服务器真实 Chromium 启动和实际模型浏览效果需要按上述步骤验收。
