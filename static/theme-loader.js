/**
 * 跨页面主题加载脚本
 * 在所有页面的 <head> 中引入此脚本
 * <script src="/static/theme-loader.js"></script>
 */

(function() {
  // 辅助函数：生成浅色版本
  function lightenColor(hex, percent) {
    const num = parseInt(hex.replace('#', ''), 16);
    const r = Math.min(255, Math.floor((num >> 16) + (255 - (num >> 16)) * (percent / 100)));
    const g = Math.min(255, Math.floor(((num >> 8) & 255) + (255 - ((num >> 8) & 255)) * (percent / 100)));
    const b = Math.min(255, Math.floor((num & 255) + (255 - (num & 255)) * (percent / 100)));
    return '#' + [r, g, b].map(x => {
      const hex = x.toString(16);
      return hex.length === 1 ? '0' + hex : hex;
    }).join('').toUpperCase();
  }

  // 【提取出异步函数外部】定义全局 COS 转换函数，确保页面加载时立即可用
  window.getCosUrl = function(path) {
    if (!path) return '';
    // 如果路径不以 /、http、data: 开头，说明是相对路径，强制转为绝对路径防止 chat/xxx 页面解析错误
    if (!path.startsWith('/') && !path.startsWith('http') && !path.startsWith('data:')) {
      path = '/' + path;
    }
    // 忽略完整地址、DataURL、本地静态资源以及特定的动态路由
    if (path.startsWith('http') ||
        path.startsWith('data:') ||
        path.startsWith('/static/') ||
        path.startsWith('/char_assets/') ||
        path.startsWith('/user_avatar') ||
        path.startsWith('/sticker_uploads/')) {
      return path;
    }
    const baseUrl = window.GLOBAL_COS_BASE_URL || localStorage.getItem('cos_base_url');
    if (!baseUrl) return path; // 降级返回原路径

    let fullPath = path;

    // 自动转换逻辑改进
    // 1. 如果路径中不包含 / 且不是 http 开头，尝试补齐用户前缀
    if (!path.includes('/')) {
        const userPrefix = window.GLOBAL_USER_PREFIX || localStorage.getItem('user_path_prefix');
        if (userPrefix) {
            fullPath = `${userPrefix}/${path}`;
        }
    }
    // 2. 如果路径包含 characters/ 或 groups/ 但不包含 users/，补齐当前用户前缀
    else if ((path.includes('characters/') || path.includes('groups/')) && !path.startsWith('users/')) {
        const userPrefix = window.GLOBAL_USER_PREFIX || localStorage.getItem('user_path_prefix');
        if (userPrefix) {
            const cleanCharPath = path.startsWith('/') ? path.substring(1) : path;
            fullPath = `${userPrefix}/${cleanCharPath}`;
        }
    }

    // 清理路径中的前置斜杠
    const cleanPath = fullPath.startsWith('/') ? fullPath.substring(1) : fullPath;

    // 如果是 users/ 或 configs/ 开头的路径，转为 COS
    if (cleanPath.startsWith('users/') || cleanPath.startsWith('configs/')) {
      const connector = cleanPath.includes('?') ? '&' : '?';
      return `${baseUrl}/${cleanPath}${connector}t=${Date.now()}`;
    }
    return path;
  };

  // 应用保存的主题设置
  async function loadAndApplyTheme() {
    try {
      const res = await fetch('/api/theme/settings');
      const theme = await res.json();

      // 应用颜色主题
      const preset = theme.preset || 'pink';
      const colorSchemes = {
        'pink': { primary: '#ffb6b9', dark: '#f09598', bg: '#f2f4f8' },
        'blue': { primary: '#a8d8ea', dark: '#7dbfd3', bg: '#f0f4f8' },
        'purple': { primary: '#c8a8d8', dark: '#b390d3', bg: '#f5f0f8' },
        'green': { primary: '#a8d8a8', dark: '#90c890', bg: '#f0f8f0' },
        'orange': { primary: '#ffb366', dark: '#ff9944', bg: '#f8f4f0' }
      };

      const colors = colorSchemes[preset] || colorSchemes['pink'];
      document.documentElement.style.setProperty('--primary-color', colors.primary);
      document.documentElement.style.setProperty('--primary-dark', colors.dark);
      document.documentElement.style.setProperty('--bg-color', colors.bg);
      // 也设置 --primary 别名，兼容旧代码
      document.documentElement.style.setProperty('--primary', colors.primary);

      // 【新增】生成动态浅色版本
      const primaryLight = lightenColor(colors.primary, 50);
      const primaryLighter = lightenColor(colors.primary, 70);
      document.documentElement.style.setProperty('--primary-light', primaryLight);
      document.documentElement.style.setProperty('--primary-lighter', primaryLighter);

      // 【新增】动态生成透明度变体（模拟 rgba，用于阴影和背景）
      // 从十六进制颜色提取 RGB 值
      function hexToRgb(hex) {
        const result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
        return result ? `${parseInt(result[1], 16)}, ${parseInt(result[2], 16)}, ${parseInt(result[3], 16)}` : '255, 182, 185';
      }

      const rgb = hexToRgb(colors.primary);
      document.documentElement.style.setProperty('--primary-rgb', rgb);
      document.documentElement.style.setProperty('--primary-rgba-light', `rgba(${rgb}, 0.1)`);
      document.documentElement.style.setProperty('--primary-rgba-lighter', `rgba(${rgb}, 0.15)`);
      document.documentElement.style.setProperty('--primary-rgba-medium', `rgba(${rgb}, 0.2)`);
      document.documentElement.style.setProperty('--primary-rgba-strong', `rgba(${rgb}, 0.3)`);
      document.documentElement.style.setProperty('--primary-rgba-dark', `rgba(${rgb}, 0.4)`);
      document.documentElement.style.setProperty('--primary-rgba-darker', `rgba(${rgb}, 0.5)`);
      document.documentElement.style.setProperty('--primary-rgba-darkest', `rgba(${rgb}, 0.6)`);

      // 存储到localStorage供其他脚本使用
      localStorage.setItem('current_theme_preset', preset);
      localStorage.setItem('current_theme_bg', theme.default_chat_bg || 'none');
      if (theme.cos_base_url) {
        localStorage.setItem('cos_base_url', theme.cos_base_url);
      }
      if (theme.user_path_prefix) {
        localStorage.setItem('user_path_prefix', theme.user_path_prefix);
      }

      // 【修正】不再在这里应用全局背景，由 chat.html 自行处理

      // 【新增】加载完成后移除加载屏幕
      setTimeout(() => {
        const loadingScreen = document.getElementById('theme-loading-screen');
        if (loadingScreen) {
          loadingScreen.style.opacity = '0';
          loadingScreen.style.transition = 'opacity 0.3s ease-out';
          setTimeout(() => {
            if (loadingScreen.parentNode) {
              loadingScreen.parentNode.removeChild(loadingScreen);
            }
          }, 300);
        }
      }, 100);

    } catch(e) {
      console.warn('主题加载失败，使用默认主题:', e);
      // 即使加载失败也要移除加载屏幕
      const loadingScreen = document.getElementById('theme-loading-screen');
      if (loadingScreen) {
        loadingScreen.style.opacity = '0';
        loadingScreen.style.transition = 'opacity 0.3s ease-out';
        setTimeout(() => {
          if (loadingScreen.parentNode) {
            loadingScreen.parentNode.removeChild(loadingScreen);
          }
        }, 300);
      }
    }
  }

  // 页面加载时应用主题
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadAndApplyTheme);
  } else {
    loadAndApplyTheme();
  }

  // 监听主题更新事件
  window.addEventListener('storage', function(e) {
    if (e.key === 'theme_updated') {
      loadAndApplyTheme();
    }
  });
})();
