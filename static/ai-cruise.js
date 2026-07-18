/**
 * AI巡航悬浮球与弹窗功能
 * 全局页面注入使用
 */
(function() {
    // 确保DOM加载完成后执行
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAICruise);
    } else {
        initAICruise();
    }

    function initAICruise() {
        // 如果已经初始化过了，不再执行
        if (document.getElementById('ai-cruise-container')) return;

        // 注入相关CSS样式
        const style = document.createElement('style');
        style.textContent = `
            /* 悬浮球容器 */
            #ai-cruise-container {
                position: fixed;
                right: 20px;
                bottom: 120px;
                width: 60px;
                height: 60px;
                border-radius: 50%;
                background: rgba(255, 255, 255, 0.4);
                backdrop-filter: blur(8px);
                -webkit-backdrop-filter: blur(8px);
                border: 4px solid rgba(255, 255, 255, 0.6);
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
                z-index: 99999;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
                transition: transform 0.2s, box-shadow 0.2s;
                user-select: none;
                touch-action: none;
            }
            #ai-cruise-container:active {
                transform: scale(0.95);
            }
            /* 内部图片 */
            #ai-cruise-img {
                width: 100%;
                height: 100%;
                border-radius: 50%;
                object-fit: cover;
                pointer-events: none;
            }

            /* 弹窗遮罩 */
            #ai-cruise-modal-overlay {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.5);
                backdrop-filter: blur(4px);
                -webkit-backdrop-filter: blur(4px);
                z-index: 100000;
                display: flex;
                align-items: center;
                justify-content: center;
                opacity: 0;
                visibility: hidden;
                pointer-events: none;
                transition: opacity 0.3s ease, visibility 0.3s ease;
            }
            #ai-cruise-modal-overlay.show {
                opacity: 1;
                visibility: visible;
                pointer-events: auto;
            }

            /* 弹窗内容 */
            #ai-cruise-modal {
                background: var(--bg-color, #fff);
                width: 85%;
                max-width: 400px;
                border-radius: 20px;
                padding: 24px;
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
                transform: translateY(20px);
                transition: transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
                position: relative;
                color: #333;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            }
            #ai-cruise-modal-overlay.show #ai-cruise-modal {
                transform: translateY(0);
            }

            /* 关闭按钮 */
            #ai-cruise-close {
                position: absolute;
                top: 15px;
                right: 15px;
                width: 30px;
                height: 30px;
                border-radius: 50%;
                background: #f0f0f0;
                display: flex;
                align-items: center;
                justify-content: center;
                cursor: pointer;
                font-weight: bold;
                color: #666;
                transition: background 0.2s;
            }
            #ai-cruise-close:hover {
                background: #e0e0e0;
            }

            /* 弹窗标题及标语 */
            #ai-cruise-title {
                font-size: 18px;
                font-weight: bold;
                margin-bottom: 15px;
                margin-top: 5px;
                color: var(--primary-color, #ffb6b9);
                text-align: center;
                line-height: 1.4;
            }
            #ai-cruise-desc {
                font-size: 15px;
                line-height: 1.6;
                color: #555;
                margin-bottom: 25px;
                text-align: justify;
            }

            /* 底部测试链接区域 */
            #ai-cruise-footer {
                background: rgba(0, 0, 0, 0.03);
                border-radius: 12px;
                padding: 15px;
                text-align: center;
                border: 1px dashed var(--primary-color, #ffb6b9);
            }
            #ai-cruise-link {
                display: inline-block;
                color: #fff;
                background: var(--primary-color, #ffb6b9);
                padding: 10px 20px;
                border-radius: 20px;
                text-decoration: none;
                font-weight: bold;
                margin-bottom: 12px;
                transition: opacity 0.2s, transform 0.2s;
                box-shadow: 0 4px 10px var(--primary-rgba-medium, rgba(255,182,185,0.3));
            }
            #ai-cruise-link:hover {
                opacity: 0.9;
                transform: translateY(-2px);
            }

            /* 复制区域样式 */
            #ai-cruise-copy-box {
                display: flex;
                align-items: center;
                background: #fff;
                border: 1px solid #eee;
                border-radius: 8px;
                padding: 5px 10px;
                margin-bottom: 10px;
                font-size: 13px;
            }
            #ai-cruise-url-text {
                flex: 1;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                color: #666;
                text-align: left;
            }
            #ai-cruise-copy-btn {
                color: var(--primary-color, #ffb6b9);
                cursor: pointer;
                padding: 2px 8px;
                font-weight: bold;
                border-left: 1px solid #eee;
                margin-left: 8px;
                transition: opacity 0.2s;
            }
            #ai-cruise-copy-btn:active {
                opacity: 0.6;
            }

            #ai-cruise-send-hint {
                font-size: 14px;
                color: #444;
                margin: 10px 0;
                font-weight: 500;
            }
            #ai-cruise-hint {
                font-size: 12px;
                color: #999;
                margin: 0;
            }
        `;
        document.head.appendChild(style);

        // 创建悬浮球
        const ball = document.createElement('div');
        ball.id = 'ai-cruise-container';

        const img = document.createElement('img');
        img.id = 'ai-cruise-img';
        img.src = '/static/cruise_icon.png';
        img.alt = 'AI Cruise';

        // 错误降级处理：如果没有图片，显示一个文字替代
        img.onerror = function() {
            this.style.display = 'none';
            ball.innerHTML = '<span style="font-size:12px;font-weight:bold;color:#666;">AI</span>';
        };

        ball.appendChild(img);
        document.body.appendChild(ball);

        // 创建弹窗
        const overlay = document.createElement('div');
        overlay.id = 'ai-cruise-modal-overlay';

        const modal = document.createElement('div');
        modal.id = 'ai-cruise-modal';

        const closeBtn = document.createElement('div');
        closeBtn.id = 'ai-cruise-close';
        closeBtn.innerHTML = '✕';

        const title = document.createElement('div');
        title.id = 'ai-cruise-title';
        title.innerHTML = '✨ AI巡航功能 初步上线！';

        const desc = document.createElement('div');
        desc.id = 'ai-cruise-desc';
        desc.innerHTML = '快来让你的专属角色和你一起，打破次元壁，探索千奇百怪的网站吧！';

        const footer = document.createElement('div');
        footer.id = 'ai-cruise-footer';

        const link = document.createElement('a');
        link.id = 'ai-cruise-link';
        link.href = 'https://sumikko-test.online';
        link.target = '_blank';
        link.innerHTML = '👉 直接参与测试';

        const sendHint = document.createElement('div');
        sendHint.id = 'ai-cruise-send-hint';
        sendHint.innerHTML = '复制网址发送给角色后，<br>点击右侧小机器人头像进行 AI 探索：';

        const copyBox = document.createElement('div');
        copyBox.id = 'ai-cruise-copy-box';

        const urlText = document.createElement('span');
        urlText.id = 'ai-cruise-url-text';
        urlText.innerHTML = 'https://sumikko-test.online';

        const copyBtn = document.createElement('span');
        copyBtn.id = 'ai-cruise-copy-btn';
        copyBtn.innerHTML = '复制';

        copyBox.appendChild(urlText);
        copyBox.appendChild(copyBtn);

        const hint = document.createElement('p');
        hint.id = 'ai-cruise-hint';
        hint.innerHTML = '（测试结束填邀请码可解锁图鉴哦）';

        footer.appendChild(link);
        footer.appendChild(sendHint);
        footer.appendChild(copyBox);
        footer.appendChild(hint);

        modal.appendChild(closeBtn);
        modal.appendChild(title);
        modal.appendChild(desc);
        modal.appendChild(footer);

        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        // 复制逻辑
        copyBtn.addEventListener('click', function() {
            const textToCopy = 'https://sumikko-test.online';
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(textToCopy).then(() => {
                    copyBtn.innerHTML = '已复制!';
                    setTimeout(() => { copyBtn.innerHTML = '复制'; }, 2000);
                });
            } else {
                // 兼容性降级
                const input = document.createElement('input');
                input.value = textToCopy;
                document.body.appendChild(input);
                input.select();
                document.execCommand('copy');
                document.body.removeChild(input);
                copyBtn.innerHTML = '已复制!';
                setTimeout(() => { copyBtn.innerHTML = '复制'; }, 2000);
            }
        });

        // 弹窗逻辑
        function openModal() {
            overlay.classList.add('show');
        }

        function closeModal() {
            overlay.classList.remove('show');
        }

        closeBtn.addEventListener('click', closeModal);
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) {
                closeModal();
            }
        });

        // 拖拽与吸附逻辑
        let isDragging = false;
        let hasMoved = false; // 判断是点击还是拖拽
        let ignoreClick = false; // 拖拽后忽略点击事件
        let startX, startY, initialX, initialY;

        // 获取窗口尺寸
        function getWindowSize() {
            return {
                w: window.innerWidth || document.documentElement.clientWidth,
                h: window.innerHeight || document.documentElement.clientHeight
            };
        }

        // 限制球不能拖出屏幕
        function clamp(val, min, max) {
            return Math.min(Math.max(val, min), max);
        }

        function handleStart(e) {
            // 如果是多点触控则忽略
            if (e.type === 'touchstart' && e.touches.length > 1) return;

            isDragging = true;
            hasMoved = false;
            ignoreClick = false;
            ball.style.transition = 'none'; // 拖拽时取消动画

            const clientX = e.type === 'touchstart' ? e.touches[0].clientX : e.clientX;
            const clientY = e.type === 'touchstart' ? e.touches[0].clientY : e.clientY;

            const rect = ball.getBoundingClientRect();
            // 记录点击相对于球体左上角的偏移量
            startX = clientX - rect.left;
            startY = clientY - rect.top;

            // 记录初始位置，用于判断是否发生移动
            initialX = clientX;
            initialY = clientY;
        }

        function handleMove(e) {
            if (!isDragging) return;

            const clientX = e.type === 'touchmove' ? e.touches[0].clientX : e.clientX;
            const clientY = e.type === 'touchmove' ? e.touches[0].clientY : e.clientY;

            // 手机端阈值设大一点，防止误触
            const threshold = e.type === 'touchmove' ? 10 : 5;

            // 如果移动距离超过阈值，认为是拖拽而非点击
            if (Math.abs(clientX - initialX) > threshold || Math.abs(clientY - initialY) > threshold) {
                if (!hasMoved) {
                    hasMoved = true;
                    ignoreClick = true; // 确认为拖动，后续不触发点击逻辑
                }
                e.preventDefault(); // 阻止滚动
            }

            if (hasMoved) {
                const winSize = getWindowSize();
                const ballSize = ball.offsetWidth;

                // 计算新位置，并确保不超出屏幕
                let newLeft = clientX - startX;
                let newTop = clientY - startY;

                newLeft = clamp(newLeft, 0, winSize.w - ballSize);
                newTop = clamp(newTop, 0, winSize.h - ballSize);

                // 覆盖初始的 right/bottom 样式
                ball.style.right = 'auto';
                ball.style.bottom = 'auto';
                ball.style.left = newLeft + 'px';
                ball.style.top = newTop + 'px';
            }
        }

        function handleEnd(e) {
            if (!isDragging) return;
            isDragging = false;

            if (hasMoved) {
                // 吸附逻辑
                ball.style.transition = 'left 0.3s ease-out, top 0.3s ease-out';

                const winSize = getWindowSize();
                const rect = ball.getBoundingClientRect();
                const ballSize = rect.width;
                const ballCenter = rect.left + ballSize / 2;

                // 判断是在屏幕左半边还是右半边
                let targetLeft;
                if (ballCenter < winSize.w / 2) {
                    targetLeft = 5; // 吸附到左侧
                } else {
                    targetLeft = winSize.w - ballSize - 5; // 吸附到右侧
                }

                ball.style.left = targetLeft + 'px';

                // 确保高度也不越界
                let currentTop = rect.top;
                currentTop = clamp(currentTop, 5, winSize.h - ballSize - 5);
                ball.style.top = currentTop + 'px';

                // 延迟一丢丢清除 ignoreClick，防止某些浏览器在 touchend 后立即触发 click
                setTimeout(() => { ignoreClick = false; }, 100);
            } else {
                // 没动，直接开弹窗 (针对某些不触发 click 的情况)
                openModal();
            }
        }

        // 绑定事件
        ball.addEventListener('mousedown', handleStart);
        document.addEventListener('mousemove', handleMove, { passive: false });
        document.addEventListener('mouseup', handleEnd);

        ball.addEventListener('touchstart', handleStart, { passive: false });
        document.addEventListener('touchmove', handleMove, { passive: false });
        document.addEventListener('touchend', handleEnd);

        // 额外的 click 监听作为兜底
        ball.addEventListener('click', function(e) {
            if (ignoreClick) return;
            openModal();
        });
    }
})();

// 管理员模拟登录状态条：只在服务器确认当前 Session 正在模拟用户时显示。
(function initAdminImpersonationBanner() {
    fetch('/api/admin/impersonation/status', { credentials: 'same-origin' })
        .then(function(response) {
            if (response.status === 440) {
                window.location.href = '/admin/dashboard';
                return null;
            }
            return response.ok ? response.json() : null;
        })
        .then(function(data) {
            if (!data || !data.active || document.getElementById('admin-impersonation-banner')) return;
            var banner = document.createElement('div');
            banner.id = 'admin-impersonation-banner';
            banner.setAttribute('role', 'alert');
            banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;gap:12px;padding:9px 12px;background:#b42318;color:#fff;font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.25);';
            var text = document.createElement('span');
            text.textContent = '管理员模拟登录中：用户 ' + data.user_id + '（15 分钟后自动退出）';
            var button = document.createElement('button');
            button.type = 'button';
            button.textContent = '退出模拟';
            button.style.cssText = 'border:1px solid rgba(255,255,255,.8);border-radius:6px;padding:4px 10px;background:#fff;color:#b42318;font-weight:600;cursor:pointer;';
            button.addEventListener('click', function() {
                button.disabled = true;
                fetch('/api/admin/impersonation/exit', { method: 'POST', credentials: 'same-origin' })
                    .then(function(response) { return response.json().then(function(body) { return { ok: response.ok, body: body }; }); })
                    .then(function(result) {
                        if (!result.ok) throw new Error(result.body.message || '退出失败');
                        window.location.href = result.body.redirect || '/admin/dashboard';
                    })
                    .catch(function(error) {
                        alert(error.message || '退出模拟失败');
                        button.disabled = false;
                    });
            });
            banner.appendChild(text);
            banner.appendChild(button);
            document.body.appendChild(banner);
        })
        .catch(function() {});
})();
